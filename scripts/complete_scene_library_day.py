#!/usr/bin/env python3
"""Deliver validated worker extraction without a parent/model approval gate.

Worker-authored reviews and summaries are immutable inputs. Existing finisher
validators enforce the contract; this command assembles complete group outputs,
materializes candidates, checks original coordinates and renders the day library.
Only concrete failures and explicit open exceptions enter the work queue.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from finish_golden_v3_batch import (
    finish_batch, preparation_source_manifest, output_root_is_safe,
    select_records, atomic_json, file_record, load_json, nonblocking_lock, utc_now,
)
from travel_video.library import render_video_library
from travel_video.phase1 import quick_fingerprint
from travel_video.scene_dialogue import merge_scene_dialogue_review_shards


def open_exceptions(review_dir: Path) -> list[str]:
    path = review_dir / 'review-exceptions.json'
    if not path.exists():
        return []
    data = load_json(path)
    if data.get('schema_version') != 'scene-review-exceptions/v1' or not isinstance(data.get('items'), list):
        raise ValueError('Invalid review-exceptions.json contract')
    reasons = []
    for item in data['items']:
        if not isinstance(item, dict) or item.get('status') not in {'open', 'resolved'} or not isinstance(item.get('reason'), str) or not item['reason'].strip():
            raise ValueError('Exception items need status open/resolved and a reason')
        if item['status'] == 'open':
            reasons.append(item['reason'])
    return reasons


def assemble_groups(record: dict, review_root: Path) -> list[str]:
    """Return missing group IDs; never invent content or overwrite manual edits."""
    root = review_root / record['asset_id']
    parts = sorted((root / 'group-reviews').glob('*.review.json'))
    if not parts:
        return []
    out = root / 'scene-dialogue/review.json'
    provenance_path = out.with_name('assembly.json')
    if out.exists() and not provenance_path.exists():
        # An independently authored whole review takes precedence.
        return []
    state = load_json(Path(record['state']))
    packet = Path(state['stages']['scene_dialogue_packet']['outputs']['review_packet']['path'])
    expected = [s['group_id'] for s in load_json(packet)['scenes']]
    seen: set[str] = set()
    for part in parts:
        for scene in load_json(part)['scenes']:
            gid = scene['group_id']
            if gid in seen or gid not in expected:
                raise ValueError(f'Duplicate or unexpected group: {gid}')
            seen.add(gid)
    missing = [gid for gid in expected if gid not in seen]
    if missing:
        return missing
    inputs = [file_record(packet), *(file_record(p) for p in parts)]
    previous = load_json(provenance_path) if provenance_path.exists() else None
    if previous and out.exists():
        current = file_record(out)
        if current['sha256'] != previous['output']['sha256']:
            raise ValueError('Assembled review was edited independently; resolve assembly conflict')
        if inputs == previous['inputs']:
            return []
        backup = out.with_name(f"review.before-assembly-{current['sha256'][:16]}.json")
        if not backup.exists():
            backup.write_bytes(out.read_bytes())
    merge_scene_dialogue_review_shards(packet, parts, out)
    atomic_json(provenance_path, {'inputs': inputs, 'output': file_record(out), 'generated_at': utc_now()})
    return []


def build_relink_entry(record: dict, origin: dict) -> dict:
    """Verify current proxy identity and saved original coordinates; no source I/O."""
    lineage_path = Path(origin['paths']['lineage'])
    lineage = load_json(lineage_path)
    proxy = Path(origin['paths']['proxy'])
    candidates = load_json(Path(record['outputs']['clip_candidates']))
    mapping = lineage['time_mapping']
    checks = [
        lineage['source_relative_path'] == record['source_relative_path'],
        lineage['original']['quick_fingerprint'] == origin['source_quick_fingerprint'],
        lineage['processing_input']['quick_fingerprint'] == origin['proxy_quick_fingerprint'] == candidates['source']['quick_fingerprint'] == quick_fingerprint(proxy),
        Path(lineage['processing_input']['path']).resolve() == proxy.resolve(),
        mapping['kind'] == 'identity', mapping['source_in_offset_seconds'] == 0,
        abs(mapping['duration_delta_seconds']) <= .05,
    ]
    if not all(checks):
        raise ValueError('Proxy/original lineage or identity time mapping mismatch')
    rows = []
    for candidate in candidates['candidates']:
        start, end = candidate['recommended_range']['start'], candidate['recommended_range']['end']
        if not 0 <= start < end <= origin['duration_seconds'] + .05:
            raise ValueError(f"Invalid original range: {candidate['candidate_id']}")
        rows.append({
            'candidate_id': candidate['candidate_id'], 'group_id': candidate['group_id'],
            'beat_id': candidate['beat_id'], 'source_relative_path': record['source_relative_path'],
            'source_quick_fingerprint': origin['source_quick_fingerprint'],
            'source_in': start, 'source_out': end,
            'core_range': candidate['core_range'], 'context_range': candidate['context_range'],
        })
    return {'asset_id': record['asset_id'], 'source_relative_path': record['source_relative_path'],
            'original_recorded_path': origin['paths']['original'], 'lineage': str(lineage_path),
            'proxy_path': str(proxy), 'time_mapping': mapping, 'candidates': rows}


def complete_day(preparation_manifest_path: Path, review_root: Path, output_root: Path,
                 library_root: Path, *, story_day: str, jobs: int = 2) -> dict[str, Any]:
    preparation_manifest_path = preparation_manifest_path.expanduser().resolve()
    review_root, output_root, library_root = (p.expanduser().resolve() for p in (review_root, output_root, library_root))
    preparation = load_json(preparation_manifest_path)
    source, _ = preparation_source_manifest(preparation, preparation_manifest_path)
    output_root_is_safe(source, output_root)
    output_root_is_safe(source, library_root)
    selected = select_records(preparation, [story_day], None)
    excluded_paths = {a['source_relative_path'] for a in source['assets']
                      if a['story_day'] == story_day and not a.get('practical_ready')}
    # Preparation manifests retain explicit preflight exclusions for provenance.
    # They are not missing work, but unknown paths must still fail below.
    selected = [r for r in selected if not (
        r.get('status') == 'excluded' and r['source_relative_path'] in excluded_paths)]
    origins = {a['source_relative_path']: a for a in source['assets']
               if a['story_day'] == story_day and a.get('practical_ready')}
    if not origins:
        raise ValueError('No eligible source assets for this day')
    selected_paths = [r['source_relative_path'] for r in selected]
    if len(set(selected_paths)) != len(selected_paths) or set(selected_paths) - set(origins):
        raise ValueError('Preparation selection has duplicate or unexpected source paths')
    output_root.mkdir(parents=True, exist_ok=True)
    with nonblocking_lock(output_root / 'locks/completion.lock'):
        queue: list[dict] = []
        blocked: set[str] = set()
        def enqueue(record: dict, action: str, reason: str, **extra: Any) -> None:
            queue.append({'asset_id': record.get('asset_id'),
                          'source_relative_path': record['source_relative_path'],
                          'action': action, 'reason': reason, **extra})
            if record.get('asset_id'):
                blocked.add(record['asset_id'])
        for rel in sorted(set(origins) - set(selected_paths)):
            enqueue({'source_relative_path': rel}, 'prepare_input', 'Eligible asset missing from preparation manifest')
        for record in selected:
            try:
                reasons = open_exceptions(review_root / record['asset_id'])
                if reasons:
                    enqueue(record, 'needs_model_review', '; '.join(reasons))
                    continue
                missing = assemble_groups(record, review_root)
                if missing:
                    enqueue(record, 'extract_groups', 'Complete groups still missing', group_ids=missing)
            except (ValueError, KeyError, TypeError, OSError) as error:
                enqueue(record, 'repair_validation', str(error))
        finished = finish_batch(preparation_manifest_path, review_root, output_root,
                                story_days=[story_day], jobs=jobs)
        delivered, relinks = [], []
        actions = {'awaiting_review': 'extract_review', 'awaiting_summary': 'author_summary',
                   'failed': 'repair_validation', 'locked': 'retry', 'skipped_preparation': 'prepare_input'}
        by_id = {r['asset_id']: r for r in selected}
        for record in finished['records']:
            if record['source_relative_path'] in excluded_paths and record['asset_id'] not in by_id:
                continue
            if record['asset_id'] in blocked:
                continue
            if record['status'] not in {'completed', 'reused'}:
                enqueue(record, actions.get(record['status'], 'repair_validation'),
                        record.get('error', {}).get('message') or record['status'],
                        preparation_state=by_id[record['asset_id']].get('state'),
                        summary_packet=record.get('summary_packet'),
                        review_path=str(review_root / record['asset_id'] / 'scene-dialogue/review.json'),
                        summary_path=str(review_root / record['asset_id'] / 'summary/video-summary.json'))
                continue
            try:
                relinks.append(build_relink_entry(record, origins[record['source_relative_path']]))
                delivered.append(record)
            except (ValueError, KeyError, TypeError, OSError) as error:
                enqueue(record, 'repair_validation', str(error))
        complete = len(delivered) == len(origins) and not queue
        attention = any(q['action'] in {'repair_validation', 'needs_model_review', 'prepare_input'} for q in queue)
        status = 'complete' if complete else 'needs_attention' if attention else 'in_progress'
        library_manifest = None
        if delivered:
            try:
                title = f'{story_day} 장면 라이브러리' + ('' if complete else f' (부분 결과 {len(delivered)}/{len(origins)})')
                render_video_library([Path(r['outputs']['timeline_dialogue_reviewed_summarized']) for r in delivered],
                                     library_root, title=title, proxy_root=Path(source['proxy_root']))
                published = load_json(library_root / 'manifest.json')
                if published.get('video_count') != len(delivered) or published.get('proxy_video_count') != len(delivered):
                    raise ValueError('Rendered catalog or proxy count differs from completed assets')
                library_manifest = str(library_root / 'manifest.json')
            except Exception as error:
                queue.append({'asset_id': None, 'action': 'repair_delivery', 'reason': str(error)})
                status = 'needs_attention'
        report = {
            'schema_version': 'scene-library-day-completion/v1', 'generated_at': utc_now(),
            'story_day': story_day, 'status': status, 'expected_assets': len(origins),
            'completed_assets': len(delivered), 'candidate_count': sum(len(x['candidates']) for x in relinks),
            'library_manifest': library_manifest, 'records': delivered, 'work_queue': queue,
            'policy': {'parent_approval_required': False, 'model_reinspection_required': False,
                       'normal_completion': 'validated worker review and summary plus deterministic delivery',
                       'uncertainty_notes_block_completion': False, 'source_media_modified': False},
        }
        atomic_json(output_root / 'original-relink-index.json', {
            'schema_version': 'scene-library-original-relink-index/v1', 'story_day': story_day,
            'verification': 'Current proxy fingerprint and preserved identity mapping; camera originals not accessed',
            'records': relinks})
        atomic_json(output_root / 'work-queue.json', {'story_day': story_day, 'items': queue})
        atomic_json(output_root / 'completion.json', report)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('preparation_manifest', type=Path)
    parser.add_argument('--review-root', type=Path, required=True)
    parser.add_argument('--output-root', type=Path, required=True)
    parser.add_argument('--library-root', type=Path, required=True)
    parser.add_argument('--story-day', required=True)
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    try:
        report = complete_day(args.preparation_manifest, args.review_root, args.output_root,
                              args.library_root, story_day=args.story_day, jobs=args.jobs)
    except Exception as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(json.dumps({k: v for k, v in report.items() if k not in {'records', 'work_queue'}}, ensure_ascii=False, indent=2))
    # Incomplete extraction is resumable, but is never a successful whole-day exit.
    return 0 if report['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
