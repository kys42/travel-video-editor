from __future__ import annotations

import importlib.util
import json
import subprocess
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build_scene_dialogue_batch.py"


def load_batch_module():
    spec = importlib.util.spec_from_file_location("build_scene_dialogue_batch", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_asset(tmp_path: Path, asset_id: str) -> dict[str, object]:
    asset_root = tmp_path / "inputs" / asset_id
    apple = asset_root / "apple" / "transcript.apple.json"
    timeline = asset_root / "phase1" / "timeline.context-reviewed.json"
    old_packet = asset_root / "old" / "reconciliation" / "packet.json"
    old_review = asset_root / "old" / "reconciliation" / "review.json"
    old_final = asset_root / "old" / "timeline.final.json"
    for path, label in (
        (apple, "raw apple"),
        (timeline, "context reviewed"),
        (old_packet, "old packet"),
        (old_review, "old review"),
        (old_final, "old final"),
    ):
        write_json(path, {"label": label, "asset_id": asset_id})
    return {
        "asset_id": asset_id,
        "source_relative_path": f"0827/{asset_id}.MP4",
        "apple_transcript_path": str(apple),
        "timeline_context_reviewed_path": str(timeline),
        "existing_reconciliation_packet_path": str(old_packet),
        "existing_reconciliation_review_path": str(old_review),
        "timeline_final_prior_pipeline_path": str(old_final),
    }


def make_inventory(tmp_path: Path, assets: list[dict[str, object]]) -> Path:
    path = tmp_path / "inventory.json"
    write_json(
        path,
        {
            "schema_version": "unified-dialogue-inventory/v1",
            "assets": assets,
        },
    )
    return path


def make_integrated_asset(tmp_path: Path, asset_id: str) -> dict[str, object]:
    asset_root = tmp_path / "inputs" / asset_id
    apple = asset_root / "apple" / "transcript.apple.json"
    timeline = asset_root / "phase1" / "timeline.reviewed.json"
    write_json(apple, {"label": "raw apple", "asset_id": asset_id})
    write_json(timeline, {"label": "quick grouped", "asset_id": asset_id})
    return {
        "asset_id": asset_id,
        "source_relative_path": f"0827/{asset_id}.MP4",
        "apple_transcript_path": str(apple),
        "timeline_reviewed_path": str(timeline),
    }


def test_batch_builds_fresh_packets_and_resumes_by_fingerprint(tmp_path: Path) -> None:
    batch = load_batch_module()
    assets = [make_asset(tmp_path, "asset-a"), make_asset(tmp_path, "asset-b")]
    inventory = make_inventory(tmp_path, assets)
    output_root = tmp_path / "prepared"
    calls: list[list[str]] = []
    lock = threading.Lock()

    def fake_runner(command: list[str], cwd: Path) -> dict[str, str]:
        assert cwd == ROOT
        with lock:
            calls.append(command)
        output = Path(command[command.index("--output") + 1])
        write_json(output, {"command": command[3], "input": command[4]})
        return {"stdout": str(output), "stderr": ""}

    first = batch.build_batch(
        inventory,
        output_root,
        jobs=2,
        runner=fake_runner,
    )

    assert first["summary"] == {
        "asset_count": 2,
        "completed": 2,
        "reused": 0,
        "failed": 0,
    }
    assert len(calls) == 2
    flattened_commands = "\n".join(" ".join(command) for command in calls)
    assert all("build-scene-dialogue-review-packet" in command for command in calls)
    assert "build-transcript-reconciliation-packet" not in flattened_commands
    assert "transcript.apple.json" in flattened_commands
    assert "timeline.context-reviewed.json" in flattened_commands
    assert "/old/" not in flattened_commands
    assert "timeline.final.json" not in flattened_commands
    assert "merge-scene-dialogue-review" not in flattened_commands
    assert "validate-scene-dialogue-review" not in flattened_commands
    assert all(record["status"] == "completed" for record in first["records"])
    assert json.loads((output_root / "errors.json").read_text())["error_count"] == 0

    calls.clear()
    second = batch.build_batch(
        inventory,
        output_root,
        jobs=2,
        runner=fake_runner,
    )
    assert calls == []
    assert second["summary"]["reused"] == 2

    apple_a = Path(str(assets[0]["apple_transcript_path"]))
    write_json(apple_a, {"label": "raw apple changed"})
    calls.clear()
    third = batch.build_batch(
        inventory,
        output_root,
        jobs=2,
        runner=fake_runner,
    )
    assert len(calls) == 1
    assert all("asset-a" in " ".join(command) for command in calls)
    assert third["summary"]["completed"] == 1
    assert third["summary"]["reused"] == 1


def test_batch_builds_dense_visual_packet_for_golden_input(tmp_path: Path) -> None:
    batch = load_batch_module()
    asset = make_integrated_asset(tmp_path, "asset-golden")
    inventory = make_inventory(tmp_path, [asset])
    output_root = tmp_path / "prepared"
    calls: list[list[str]] = []

    def fake_runner(command: list[str], cwd: Path) -> dict[str, str]:
        assert cwd == ROOT
        calls.append(command)
        if "build-context-packet" in command:
            output_dir = Path(command[command.index("--output-dir") + 1])
            write_json(output_dir / "context-review-packet.json", {"groups": []})
        else:
            output = Path(command[command.index("--output") + 1])
            write_json(output, {"command": "build-scene-dialogue-review-packet"})
        return {"stdout": "ok", "stderr": ""}

    manifest = batch.build_batch(
        inventory,
        output_root,
        jobs=1,
        max_frames=16,
        max_window=8,
        runner=fake_runner,
    )

    assert manifest["summary"]["completed"] == 1
    assert len(calls) == 2
    context_call, scene_call = calls
    assert "build-context-packet" in context_call
    assert any(value.endswith("timeline.reviewed.json") for value in context_call)
    assert context_call[context_call.index("--max-frames") + 1] == "16"
    assert "build-scene-dialogue-review-packet" in scene_call
    assert scene_call[scene_call.index("--max-window") + 1] == "8"
    assert scene_call[scene_call.index("--visual-packet") + 1].endswith(
        "context/context-review-packet.json"
    )
    assert "timeline.context-reviewed.json" not in " ".join(scene_call)
    state = json.loads(
        (output_root / "asset-golden" / "state.json").read_text(encoding="utf-8")
    )
    assert state["policy"]["integrated_visual_packet"] is True
    assert set(state["outputs"]) == {"context_packet", "scene_dialogue_packet"}

    calls.clear()
    resumed = batch.build_batch(
        inventory,
        output_root,
        jobs=1,
        max_frames=16,
        max_window=8,
        runner=fake_runner,
    )
    assert calls == []
    assert resumed["summary"]["reused"] == 1


def test_batch_filters_shards_and_records_per_asset_errors(tmp_path: Path) -> None:
    batch = load_batch_module()
    assets = [make_asset(tmp_path, f"asset-{index}") for index in range(1, 5)]
    inventory = make_inventory(tmp_path, assets)
    output_root = tmp_path / "prepared"

    def failing_runner(command: list[str], cwd: Path) -> dict[str, str]:
        raise subprocess.CalledProcessError(
            1,
            command,
            output="",
            stderr="fresh packet failed",
        )

    manifest = batch.build_batch(
        inventory,
        output_root,
        asset_regex=r"asset-[13]",
        shard="1/2",
        jobs=3,
        runner=failing_runner,
    )

    assert manifest["selection"]["selected_asset_count"] == 1
    assert [record["asset_id"] for record in manifest["records"]] == ["asset-1"]
    assert manifest["summary"]["failed"] == 1
    errors = json.loads((output_root / "errors.json").read_text(encoding="utf-8"))
    assert errors["error_count"] == 1
    assert errors["errors"][0]["asset_id"] == "asset-1"
    assert "fresh packet failed" in errors["errors"][0]["error"]["message"]
    state = json.loads(
        (output_root / "asset-1" / "state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "failed"


def test_canonical_inputs_reject_prior_derivative_paths(tmp_path: Path) -> None:
    batch = load_batch_module()
    old_transcript = tmp_path / "transcript.reconciled.json"
    old_timeline = tmp_path / "timeline.final.json"
    write_json(old_transcript, {})
    write_json(old_timeline, {})
    asset = {
        "asset_id": "asset-a",
        "apple_transcript_path": str(old_transcript),
        "timeline_context_reviewed_path": str(old_timeline),
    }

    with pytest.raises(ValueError, match="transcript.apple.json"):
        batch.canonical_inputs(asset)

    apple = tmp_path / "transcript.apple.json"
    write_json(apple, {})
    asset["apple_transcript_path"] = str(apple)
    with pytest.raises(ValueError, match="timeline.context-reviewed.json"):
        batch.canonical_inputs(asset)


@pytest.mark.parametrize("value", ["0/3", "4/3", "1:3", "one/three"])
def test_parse_shard_rejects_invalid_values(value: str) -> None:
    batch = load_batch_module()

    with pytest.raises(ValueError, match="shard"):
        batch.parse_shard(value)
