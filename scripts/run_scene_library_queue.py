#!/usr/bin/env python3
"""Bounded local scene-library scheduler. Default plan never writes or calls models."""
from __future__ import annotations

import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import ExitStack
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

SCHEMA = 'scene-library-queue/v1'
PROJECT = Path(__file__).resolve().parents[1]
ACTIONS = {'extract_review', 'author_summary', 'repair_validation', 'extract_groups'}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.replace(temp, path)


def _locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


def records_for_day(manifest, day):
    data = load(manifest)
    return [r for r in data['records'] if r.get('story_day') == day and r.get('status') != 'excluded']


def completion_ids(path):
    # Snapshot only: execute always runs the deterministic validator first.
    report = load(Path(path) / 'completion.json')
    if report is None:
        report = load(Path(path) / 'manifest.json', {})
    return {r['asset_id'] for r in report.get('records', [])
            if r.get('status') in {'completed', 'reused'}}


def prompt_for(job):
    contract = {k: job.get(k) for k in ('asset_id', 'story_day', 'action', 'reason',
        'source_relative_path', 'preparation_state', 'review_packet', 'summary_packet',
        'review_path', 'summary_path', 'group_ids', 'group_output_root')}
    return ("Process ONLY this asset and action. Read docs/media-locations.md and the exact packet contract. "
        "Camera originals and prepared inputs are immutable. Use proxy-derived evidence images and original source coordinates. "
        "No nested agents, no parent QA, no full-original watching, no web browsing. "
        "extract_review: read review_packet, inspect its evidence images, write complete review only. "
        "author_summary: read the ACTUAL summary_packet and write summary only; do not rewrite review. "
        "extract_groups: write only the listed complete group_ids into group_output_root as distinct *.review.json files; "
        "preserve existing groups and never split a coarse group. "
        "repair_validation: fix ONLY the supplied actual validator error using canonical packet evidence; preserve a backup "
        "of any edited review/summary. Never change IDs/times arbitrarily to satisfy a validator. "
        "Write JSON via a temporary file and atomic rename. Do not run day finishers or change prepared/finished/library files. "
        "The controller runs validators, merge, summary packet creation and web delivery. "
        "A file inventory or promised future work is not extraction. Finish the assigned output now.\n" +
        json.dumps(contract, ensure_ascii=False, indent=2))


def command_for(template, job):
    prompt = prompt_for(job)
    if template:
        values = {k: str(v) for k, v in job.items()}
        return [prompt if arg == '{prompt}' else arg.format_map(values) for arg in template]
    return [job.get('codex_binary', 'codex'), 'exec', '--json', '--ephemeral', '--ignore-user-config', '--model', 'gpt-5.6-luna',
            '-c', 'model_reasoning_effort="medium"', '-c', 'approval_policy="never"', '-s', 'workspace-write',
            '--add-dir', str(Path(job['review_path']).parent.parent), prompt]


def run_process(cmd, timeout, cwd=None, *, lock_fds=()):
    started = time.monotonic()
    # Files bound memory even if a worker prints large packets. Children inherit
    # ownership locks: a controller crash cannot permit an overlapping writer.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--supervise', str(timeout), *cmd], cwd=cwd, start_new_session=True,
                                       stdout=out, stderr=err, pass_fds=lock_fds)
        except OSError as exc:
            return {'ok': False, 'kind': 'spawn', 'error': str(exc), 'usage': None}
        timed_out = False
        try:
            process.wait(timeout=timeout + 3)
        except BaseException as exc:
            timed_out = isinstance(exc, subprocess.TimeoutExpired)
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                pass
            if not timed_out:
                raise
        out.seek(0)
        usage, turn_count, tail = Counter(), 0, ''
        for line in out:
            text = line.decode('utf-8', errors='replace')
            tail = (tail + text)[-2000:]
            try:
                event = json.loads(text)
            except ValueError:
                continue
            if event.get('type') == 'turn.completed' and isinstance(event.get('usage'), dict):
                turn_count += 1
                usage.update({k: v for k, v in event['usage'].items() if isinstance(v, int)})
        err.seek(0, 2)
        err.seek(max(0, err.tell() - 2000))
        return {'ok': process.returncode == 0 and not timed_out,
                'kind': 'timeout' if timed_out or process.returncode == 124 else 'exit', 'returncode': process.returncode,
                'elapsed_seconds': round(time.monotonic() - started, 3),
                'stdout_tail': tail, 'stderr_tail': err.read().decode('utf-8', errors='replace'),
                'usage': dict(usage) if turn_count else None, 'reported_turns': turn_count}


def read_config(path):
    config = load(path)
    if not isinstance(config, dict) or config.get('schema_version') != SCHEMA:
        raise ValueError(f'config schema must be {SCHEMA}')
    for name, default in [('concurrency', 3), ('max_attempts', 4), ('worker_timeout_seconds', 1800)]:
        value = config.setdefault(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f'{name} must be a positive integer')
    days = config.get('days')
    if not isinstance(days, list) or not days:
        raise ValueError('days must be nonempty')
    for field in ['story_day', 'review_root', 'output_root', 'library_root']:
        values = [d[field] if field == 'story_day' else str(Path(d[field]).expanduser().resolve()) for d in days]
        if len(set(values)) != len(values):
            raise ValueError(f'duplicate {field}')
    for day in days:
        for key in ['preparation_manifest', 'review_root', 'output_root', 'library_root']:
            day[key] = str(Path(day[key]).expanduser().resolve())
        if not Path(day['preparation_manifest']).is_file():
            raise ValueError('preparation manifest missing')
    return config


def run_queue(config_path, *, execute=False, process_runner=None):
    config_path = Path(config_path).resolve()
    config = read_config(config_path)
    root = Path(config.get('state_root', config_path.parent / '.scene-library-queue')).expanduser().resolve()
    days = {d['story_day']: d for d in config['days']}
    jobs = {}
    for name, day in days.items():
        done = completion_ids(Path(day['output_root']))
        for record in records_for_day(Path(day['preparation_manifest']), name):
            asset = record['asset_id']
            if not asset or Path(asset).name != asset or asset in {'.', '..'}:
                raise ValueError('unsafe asset_id')
            key = f'{name}:{asset}'
            if key in jobs:
                raise ValueError('duplicate asset')
            review = Path(day['review_root']) / asset
            state = load(record.get('state', ''), {}) if record.get('state') else {}
            packet = state.get('stages', {}).get('scene_dialogue_packet', {}).get('outputs', {}).get('review_packet', {}).get('path')
            jobs[key] = dict(job_id=key, story_day=name, asset_id=asset, attempts=0,
                status='completed' if asset in done else 'queued', source_relative_path=record.get('source_relative_path'),
                preparation_state=record.get('state'), review_packet=packet,
                review_path=str(review / 'scene-dialogue/review.json'), summary_path=str(review / 'summary/video-summary.json'),
                group_output_root=str(review / 'group-reviews'))
    state = {'schema_version': SCHEMA, 'jobs': jobs, 'days': {}, 'updated_at': now()}
    if not execute:
        return {'mode': 'plan', 'state': state, 'model_calls': 0, 'validation': 'saved snapshot only'}
    from complete_scene_library_day import complete_day
    if process_runner is None and not config.get('worker_command'):
        bundled = Path('/Applications/Codex.app/Contents/Resources/codex')
        binary = config.get('codex_binary') or (str(bundled) if bundled.is_file() else shutil.which('codex'))
        if not binary:
            raise ValueError('Codex CLI missing; set codex_binary')
        probe = subprocess.run([binary, 'exec', '--help'], capture_output=True, text=True, timeout=10)
        required = ['--json', '--ephemeral', '--ignore-user-config', '--add-dir']
        if probe.returncode or any(flag not in probe.stdout for flag in required):
            raise ValueError('Codex CLI incompatible; set codex_binary to a working current installation')
        for job in jobs.values():
            job['codex_binary'] = binary
    with ExitStack() as stack:
        stack.enter_context(_locked(root / 'controller.lock'))
        locks = [stack.enter_context(_locked(Path(d['review_root']) / '.scene-library-worker.lock')) for d in days.values()]
        previous = load(root / 'state.json', {})
        state['usage'] = previous.get('usage', {'reported_tokens': {}, 'workers_without_usage': 0, 'worker_calls': 0})
        # Do not apply a prior queue's budget or outputs to a changed assignment.
        for key, job in jobs.items():
            old = previous.get('jobs', {}).get(key, {})
            if old and any(old.get(k) != job.get(k) for k in ['preparation_state', 'review_path', 'summary_path']):
                raise ValueError('queue assignment changed; use a new state_root')
            job['attempts'] = old.get('attempts', 0)
            if old.get('last_result'):
                job['last_result'] = old['last_result']
        runner = process_runner or (lambda cmd, timeout, cwd: run_process(cmd, timeout, cwd, lock_fds=tuple(h.fileno() for h in locks)))
        def save():
            state['updated_at'] = now()
            atomic(root / 'state.json', state)
        def event(kind, **fields):
            with (root / 'events.jsonl').open('a') as handle:
                handle.write(json.dumps({'at': now(), 'event': kind, **fields}, ensure_ascii=False) + '\n')
        def validate(name):
            day = days[name]
            day_jobs = [j for j in jobs.values() if j['story_day'] == name]
            try:
                result = complete_day(Path(day['preparation_manifest']), Path(day['review_root']),
                    Path(day['output_root']), Path(day['library_root']), story_day=name, jobs=int(day.get('finish_jobs', 2)))
                state['days'][name] = {k: result.get(k) for k in ['status', 'expected_assets', 'completed_assets']}
                state['days'][name]['work_queue'] = result.get('work_queue', [])
                completed = {r['asset_id'] for r in result.get('records', []) if r.get('status') in {'completed', 'reused'}}
                queued = {r['asset_id']: r for r in result.get('work_queue', []) if r.get('asset_id')}
                for job in day_jobs:
                    job['owner'] = None
                    if job['asset_id'] in completed and job['asset_id'] not in queued:
                        job['status'] = 'completed'
                        continue
                    item = queued.get(job['asset_id'], {'action': 'needs_attention', 'reason': 'No completion record or work item'})
                    job.update({k: item.get(k) for k in ['action', 'reason', 'summary_packet', 'group_ids']})
                    job['status'] = ('queued' if job['action'] in ACTIONS and job['attempts'] < config['max_attempts'] else 'needs_attention')
                    if not job.get('review_packet') or (job['action'] == 'author_summary' and not job.get('summary_packet')):
                        job.update(status='needs_attention', reason='Canonical packet path missing')
            except Exception as exc:
                state['days'][name] = {'status': 'needs_attention', 'error': str(exc)}
                for job in day_jobs:
                    job.update(status='needs_attention', reason=f'completion failed: {exc}', owner=None)
            event('day_validated', story_day=name, status=state['days'][name]['status'])
            save()
        # Mandatory validation before model dispatch also imports historic finishers.
        for name in days:
            validate(name)
        model_calls = 0
        active = {}
        rotation = deque(days)
        with ThreadPoolExecutor(max_workers=config['concurrency']) as pool:
            while True:
                # Round robin across dates; several assets of one date can run when slots remain.
                while len(active) < config['concurrency']:
                    chosen = None
                    for _ in range(len(rotation)):
                        name = rotation[0]
                        rotation.rotate(-1)
                        chosen = next((j for j in jobs.values() if j['story_day'] == name and j['status'] == 'queued'), None)
                        if chosen:
                            break
                    if not chosen:
                        break
                    Path(chosen['review_path']).parent.parent.mkdir(parents=True, exist_ok=True)
                    chosen.update(status='running', attempts=chosen['attempts'] + 1, owner=os.getpid())
                    save()
                    event('worker_started', job_id=chosen['job_id'], attempt=chosen['attempts'],
                          requested_model='gpt-5.6-luna' if not config.get('worker_command') else 'custom', requested_effort='medium')
                    future = pool.submit(runner, command_for(config.get('worker_command'), chosen),
                                         config['worker_timeout_seconds'], Path(days[chosen['story_day']].get('working_directory', PROJECT)))
                    active[future] = chosen
                    model_calls += 1
                    state['usage']['worker_calls'] += 1
                if not active:
                    break
                finished, _ = wait(active, return_when=FIRST_COMPLETED)
                touched = set()
                for future in finished:
                    job = active.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {'ok': False, 'error': str(exc), 'usage': None}
                    job.update(status='awaiting_validation', last_result=result, owner=None)
                    if isinstance(result.get('usage'), dict):
                        aggregate = Counter(state['usage']['reported_tokens'])
                        aggregate.update(result['usage'])
                        state['usage']['reported_tokens'] = dict(aggregate)
                    else:
                        state['usage']['workers_without_usage'] += 1
                    touched.add(job['story_day'])
                    event('worker_finished', job_id=job['job_id'], result=result)
                # Never inspect a date while any worker can be writing that date.
                for name in touched:
                    if not any(j['story_day'] == name for j in active.values()):
                        validate(name)
                save()
        complete = all(d.get('status') == 'complete' for d in state['days'].values()) and all(j['status'] == 'completed' for j in jobs.values())
        state['status'] = 'complete' if complete else 'needs_attention'
        save()
        event('queue_finished', status=state['status'], model_calls=model_calls)
        return {'mode': 'execute', 'state': state, 'model_calls': model_calls}


def supervise(timeout, command):
    # Independent watchdog survives controller SIGKILL. Its inherited review
    # locks remain held until the entire worker process group is stopped.
    signal.signal(signal.SIGTERM, lambda *_: None)
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 127
    try:
        return child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgrp(), signal.SIGTERM)
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgrp(), signal.SIGKILL)
        return 124


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = run_queue(args.config, execute=args.execute)
    except Exception as exc:
        print(f'queue error: {exc}', file=sys.stderr)
        return 2
    print(json.dumps({'mode': result['mode'], 'model_calls': result['model_calls'],
        'status': result['state'].get('status', 'snapshot'),
        'counts': dict(Counter(j['status'] for j in result['state']['jobs'].values()))}))
    return 0 if not args.execute or result['state']['status'] == 'complete' else 1


if __name__ == '__main__':
    if len(sys.argv) > 3 and sys.argv[1] == '--supervise':
        raise SystemExit(supervise(float(sys.argv[2]), sys.argv[3:]))
    raise SystemExit(main())
