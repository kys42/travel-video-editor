from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT))
import run_scene_library_queue as queue


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def config(tmp_path: Path, n: int = 2) -> Path:
    manifest = tmp_path / "prepared.json"
    packet = tmp_path / "packet.json"
    write(packet, {"scenes": []})
    write(manifest, {"records": [{"asset_id": f"a{i}", "story_day": "2026-08-21",
                                   "source_relative_path": f"x/{i}.mp4", "state": str(tmp_path / f"state{i}.json")} for i in range(n)]})
    for i in range(n):
        write(tmp_path / f"state{i}.json", {"stages": {"scene_dialogue_packet": {"outputs": {"review_packet": {"path": str(packet)}}}}})
    value = {"schema_version": "scene-library-queue/v1", "state_root": str(tmp_path / "queue"),
             "max_attempts": 2, "concurrency": 2, "worker_timeout_seconds": 2,
             "worker_command": [sys.executable, "-c", "pass", "{asset_id}"],
             "days": [{"story_day": "2026-08-21", "preparation_manifest": str(manifest),
                       "review_root": str(tmp_path / "reviews"), "output_root": str(tmp_path / "out"),
                       "library_root": str(tmp_path / "library")}]}
    path = tmp_path / "config.json"
    write(path, value)
    return path


def test_plan_is_side_effect_free_and_lists_jobs(tmp_path: Path) -> None:
    result = queue.run_queue(config(tmp_path))
    assert result["mode"] == "plan"
    assert result["model_calls"] == 0
    assert len(result["state"]["jobs"]) == 2
    assert not (tmp_path / "queue").exists()


def test_default_worker_requests_fresh_luna_medium_context() -> None:
    cmd = queue.command_for(None, {"job_id": "d:a", "story_day": "2026-08-21", "asset_id": "a",
                                   "review_path": "/tmp/reviews/a/scene-dialogue/review.json"})
    assert cmd[:2] == ["codex", "exec"]
    assert "gpt-5.6-luna" in cmd
    assert 'model_reasoning_effort="medium"' in cmd
    assert "--json" in cmd
    assert "--ephemeral" in cmd
    assert "No nested agents" in cmd[-1]


def test_exit_zero_without_contract_is_not_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The finisher reports no completed records, simulating a worker that lied
    # via exit 0 or only wrote an incomplete response.
    fake = types.ModuleType("complete_scene_library_day")
    fake.complete_day = lambda *args, **kwargs: {"status": "in_progress", "records": [],
        "work_queue": [{"asset_id": "a0", "action": "extract_review", "reason": "missing review"},
                        {"asset_id": "a1", "action": "extract_review", "reason": "missing review"}]}
    monkeypatch.setitem(sys.modules, "complete_scene_library_day", fake)
    result = queue.run_queue(config(tmp_path), execute=True)
    statuses = {j["status"] for j in result["state"]["jobs"].values()}
    assert statuses == {"needs_attention"}


def test_retry_cap_and_timeout_are_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("complete_scene_library_day")
    fake.complete_day = lambda *args, **kwargs: {"status": "in_progress", "records": [],
        "work_queue": [{"asset_id": "a0", "action": "extract_review", "reason": "missing review"}]}
    monkeypatch.setitem(sys.modules, "complete_scene_library_day", fake)
    calls = []
    def runner(cmd, timeout, cwd):
        calls.append(cmd)
        return {"ok": False, "kind": "timeout", "error": "worker timeout"}
    result = queue.run_queue(config(tmp_path, 1), execute=True, process_runner=runner)
    assert len(calls) == 2
    assert next(iter(result["state"]["jobs"].values()))["status"] == "needs_attention"
    # A restart consumes the bounded second attempt and moves to attention.
    result = queue.run_queue(config(tmp_path, 1), execute=True, process_runner=runner)
    job = next(iter(result["state"]["jobs"].values()))
    assert job["attempts"] == 2
    assert job["status"] == "needs_attention"


def test_completed_asset_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "out"
    done = tmp_path / "done.json"
    done.write_text("{}")
    write(out / "completion.json", {"records": [{"asset_id": "a0", "status": "completed",
                                                  "outputs": {"web_index": str(done)}}]})
    fake = types.ModuleType("complete_scene_library_day")
    fake.complete_day = lambda *args, **kwargs: {"status": "complete", "records": [{"asset_id": "a0", "status": "completed"}], "work_queue": []}
    monkeypatch.setitem(sys.modules, "complete_scene_library_day", fake)
    calls = []
    result = queue.run_queue(config(tmp_path, 1), execute=True,
                             process_runner=lambda *args: calls.append(args) or {"ok": True})
    assert len(calls) == 0
    assert result["state"]["jobs"]["2026-08-21:a0"]["status"] == "completed"


def test_summary_action_uses_actual_packet_and_preserves_review(tmp_path: Path,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    packet = tmp_path / "actual-summary-packet.json"
    packet.write_text("{}")
    review = str(tmp_path / "reviews/a0/scene-dialogue/review.json")
    calls = []
    validations = [0]
    fake = types.ModuleType("complete_scene_library_day")
    def finish(*args, **kwargs):
        validations[0] += 1
        if validations[0] == 1:
            return {"status": "in_progress", "records": [], "work_queue":
                    [{"asset_id": "a0", "action": "author_summary",
                      "reason": "summary missing", "summary_packet": str(packet)}]}
        return {"status": "complete", "records": [{"asset_id": "a0", "status": "completed"}], "work_queue": []}
    fake.complete_day = finish
    monkeypatch.setitem(sys.modules, "complete_scene_library_day", fake)
    cfg = config(tmp_path, 1)
    value = json.loads(cfg.read_text())
    value["worker_command"] = ["worker", "{prompt}"]
    cfg.write_text(json.dumps(value))
    result = queue.run_queue(cfg, execute=True,
                             process_runner=lambda cmd, timeout, cwd: calls.append(cmd) or {"ok": True})
    assert result["state"]["jobs"]["2026-08-21:a0"]["status"] == "completed"
    assert len(calls) == 1
    assert "author_summary" in calls[0][-1]
    assert str(packet) in calls[0][-1]
    assert review not in calls[0][-1] or "summary only" in calls[0][-1]


def test_nonzero_worker_is_recorded_and_returns_attention_after_cap(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("complete_scene_library_day")
    fake.complete_day = lambda *args, **kwargs: {"status": "in_progress", "records": [],
        "work_queue": [{"asset_id": "a0", "action": "extract_review", "reason": "missing"}]}
    monkeypatch.setitem(sys.modules, "complete_scene_library_day", fake)
    result = queue.run_queue(config(tmp_path, 1), execute=True,
                             process_runner=lambda *args: {"ok": False, "kind": "exit", "returncode": 7,
                                                           "usage": None})
    job = result["state"]["jobs"]["2026-08-21:a0"]
    assert job["status"] == "needs_attention"
    assert job["attempts"] == 2
    assert job["last_result"]["returncode"] == 7


def test_subprocess_adapter_collects_usage_and_kills_timeout() -> None:
    usage_cmd = [sys.executable, "-c",
                 "import sys; print('{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":4,\"output_tokens\":2}}')"]
    result = queue.run_process(usage_cmd, 2)
    assert result["ok"] is True
    assert result["usage"] == {"input_tokens": 4, "output_tokens": 2}
    timeout_cmd = [sys.executable, "-c", "import time; time.sleep(3)"]
    result = queue.run_process(timeout_cmd, 0.05)
    assert result["ok"] is False
    assert result["kind"] == "timeout"


def test_dates_overlap_and_validation_waits_for_writers(tmp_path, monkeypatch):
    import threading
    first = config(tmp_path / 'one', 1)
    second = config(tmp_path / 'two', 1)
    value = json.loads(first.read_text())
    day2 = json.loads(second.read_text())['days'][0]
    day2['story_day'] = '2026-08-22'
    manifest = Path(day2['preparation_manifest'])
    rows = json.loads(manifest.read_text())
    rows['records'][0]['story_day'] = day2['story_day']
    write(manifest, rows)
    value['days'].append(day2)
    value['worker_command'] = ['worker', '{story_day}']
    write(first, value)
    active, done = set(), set()
    guard, barrier = threading.Lock(), threading.Barrier(2)
    def finish(*args, story_day, **kwargs):
        with guard:
            assert story_day not in active
            if story_day in done:
                return {'status': 'complete', 'records': [{'asset_id': 'a0', 'status': 'completed'}], 'work_queue': []}
        return {'status': 'in_progress', 'records': [], 'work_queue': [{'asset_id': 'a0', 'action': 'extract_review'}]}
    def runner(cmd, *args):
        with guard:
            active.add(cmd[1])
        barrier.wait(timeout=2)
        with guard:
            done.add(cmd[1])
            active.remove(cmd[1])
        return {'ok': True}
    fake = types.ModuleType('complete_scene_library_day')
    fake.complete_day = finish
    monkeypatch.setitem(sys.modules, 'complete_scene_library_day', fake)
    result = queue.run_queue(first, execute=True, process_runner=runner)
    assert result['state']['status'] == 'complete'
    assert result['model_calls'] == 2


def test_shared_review_lock_blocks_other_controller(tmp_path, monkeypatch):
    cfg = config(tmp_path, 1)
    with queue._locked(tmp_path / 'reviews/.scene-library-worker.lock'):
        with pytest.raises(BlockingIOError):
            queue.run_queue(cfg, execute=True, process_runner=lambda *args: pytest.fail('duplicate writer'))


def test_orphan_watchdog_expires_after_controller_dies(tmp_path):
    import subprocess
    import time
    lock = tmp_path / 'owner.lock'
    marker = tmp_path / 'started'
    # This process acts as a controller and exits immediately after dispatching
    # an independent supervisor with its ownership fd inherited.
    code = """
import fcntl, subprocess, sys
h=open(sys.argv[1], 'w');fcntl.flock(h,fcntl.LOCK_EX)
subprocess.Popen([sys.executable,sys.argv[3],'--supervise','0.3',sys.executable,'-c',
    'from pathlib import Path; import time; Path('+repr(sys.argv[2])+').touch(); time.sleep(30)'],
    start_new_session=True, pass_fds=(h.fileno(),), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
"""
    subprocess.run([sys.executable, '-c', code, str(lock), str(marker), str(SCRIPT / 'run_scene_library_queue.py')], check=True)
    deadline = time.monotonic() + 3
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert marker.exists()
    with pytest.raises(BlockingIOError):
        queue._locked(lock)
    while time.monotonic() < deadline:
        try:
            handle = queue._locked(lock)
            handle.close()
            break
        except BlockingIOError:
            time.sleep(.02)
    else:
        pytest.fail('orphan kept ownership beyond watchdog timeout')
