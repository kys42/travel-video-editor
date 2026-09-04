from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_golden_v3_batch.py"


def load_batch_module():
    spec = importlib.util.spec_from_file_location("run_golden_v3_batch", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_asset(
    tmp_path: Path,
    name: str,
    *,
    story_day: str = "2026-08-20",
    eligible: bool = True,
    legacy_grouping: bool = True,
) -> tuple[dict[str, object], Path]:
    asset_id = f"{name}--asset"
    source_root = tmp_path / "source"
    proxy_root = tmp_path / "proxies"
    original = source_root / "cam" / f"{name}.MP4"
    proxy = proxy_root / "cam" / f"{name}.mp4"
    original.parent.mkdir(parents=True, exist_ok=True)
    proxy.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"original")
    proxy.write_bytes(b"proxy")
    phase1 = tmp_path / "analysis" / name / "phase1"
    machine_path = phase1 / "timeline.machine.json"
    reviewed_path = phase1 / "timeline.reviewed.json"
    lineage_path = tmp_path / "analysis" / name / "lineage.json"
    apple_path = tmp_path / "analysis" / name / "apple" / "transcript.apple.json"
    source = {
        "name": proxy.name,
        "path": str(proxy.resolve()),
        "quick_fingerprint": f"proxy-fingerprint-{name}",
    }
    media = {"duration": 10.0, "video": {"width": 1920, "height": 1080}}
    machine_segments = [
        {
            "segment_id": "S001",
            "start": 0.0,
            "end": 5.0,
            "start_timecode": "00:00.000",
            "end_timecode": "00:05.000",
            "boundary_reason": "asset_start",
            "representative_sample_id": "F001",
            "representative_frame": str(phase1 / "frames" / "F001.jpg"),
            "representative_hash": "hash-1",
            "machine": {"quality": 0.8},
            "machine_group_id": "MG001",
            "contact_sheet": "sheet.jpg",
            "contact_cell": "A1",
        },
        {
            "segment_id": "S002",
            "start": 5.0,
            "end": 10.0,
            "start_timecode": "00:05.000",
            "end_timecode": "00:10.000",
            "boundary_reason": "max_duration",
            "representative_sample_id": "F002",
            "representative_frame": str(phase1 / "frames" / "F002.jpg"),
            "representative_hash": "hash-2",
            "machine": {"quality": 0.9},
            "machine_group_id": "MG001",
            "contact_sheet": "sheet.jpg",
            "contact_cell": "A2",
        },
    ]
    machine = {
        "schema_version": "phase1-machine/v1",
        "asset_id": asset_id,
        "source": source,
        "media": media,
        "segments": machine_segments,
    }
    reviewed = {
        **machine,
        "schema_version": "phase1-reviewed-timeline/v1",
        "segments": [
            {**segment, "review": {"visual_summary": "scene"}}
            for segment in machine_segments
        ],
        "reviewed_groups": [
            {
                "group_id": "G001",
                "label": "group",
                "summary": "summary",
                "segment_ids": ["S001", "S002"],
            }
        ],
    }
    write_json(machine_path, machine)
    if legacy_grouping:
        write_json(reviewed_path, reviewed)
    write_json(lineage_path, {"schema_version": "travel-video-source-lineage/v1"})
    write_json(
        apple_path,
        {"schema_version": "apple-stt/v1", "source": source, "candidates": []},
    )
    asset = {
        "source_relative_path": f"cam/{name}.MP4",
        "story_day": story_day,
        "local_capture": f"{story_day}T10:00:00-07:00",
        "duration_seconds": 10.0,
        "source_quick_fingerprint": f"source-fingerprint-{name}",
        "proxy_quick_fingerprint": f"proxy-fingerprint-{name}",
        "eligible_for_golden_v3_extraction": eligible,
        "missing_prerequisites": [] if eligible else ["proxy"],
        "legacy_downstream_cache": {"quick_group_review": legacy_grouping},
        "paths": {
            "original": str(original.resolve()),
            "proxy": str(proxy.resolve()),
            "lineage": str(lineage_path.resolve()) if eligible else None,
            "phase1_run_dir": str(phase1.resolve()) if eligible else None,
            "phase1_machine": str(machine_path.resolve()) if eligible else None,
            "apple_transcript": str(apple_path.resolve()) if eligible else None,
        },
    }
    return asset, reviewed_path


def make_manifest(tmp_path: Path, assets: list[dict[str, object]]) -> Path:
    grouped: dict[str, list[dict[str, object]]] = {}
    for asset in assets:
        grouped.setdefault(str(asset["story_day"]), []).append(asset)
    days = []
    for day, members in sorted(grouped.items()):
        eligible = [
            str(item["source_relative_path"])
            for item in members
            if item["eligible_for_golden_v3_extraction"] is True
        ]
        days.append(
            {
                "story_day": day,
                "label": f"Day {day}",
                "asset_count": len(members),
                "eligible_asset_count": len(eligible),
                "eligible_assets": eligible,
            }
        )
    path = tmp_path / "preflight" / "story-days.json"
    write_json(
        path,
        {
            "schema_version": "travel-video-story-day-preflight/v1",
            "source_root": str((tmp_path / "source").resolve()),
            "proxy_root": str((tmp_path / "proxies").resolve()),
            "story_days": days,
            "assets": assets,
        },
    )
    return path


def make_fake_runner(calls: list[list[str]], *, fail_asset: str | None = None):
    lock = threading.Lock()

    def fake_runner(command: list[str], cwd: Path) -> dict[str, str]:
        assert cwd == ROOT
        with lock:
            calls.append(command)
        action = command[3]
        if fail_asset and fail_asset in " ".join(command):
            raise RuntimeError(f"intentional failure for {fail_asset}")
        if action == "build-boundary-proposals":
            output_dir = Path(command[command.index("--output-dir") + 1])
            for name in (
                "run-intent.json",
                "run.json",
                "proposals.json",
                "visual-moments.json",
            ):
                write_json(output_dir / name, {"artifact": name})
            for name in (
                "apple-stt.json",
                "ffmpeg.json",
                "apple-vision.raw.json",
                "apple-vision.json",
            ):
                write_json(output_dir / "signals" / name, {"artifact": name})
            frame = output_dir / "visual-moment-frames" / "VM0001.jpg"
            frame.parent.mkdir(parents=True, exist_ok=True)
            frame.write_bytes(b"frame")
        elif action == "build-context-packet":
            output_dir = Path(command[command.index("--output-dir") + 1])
            write_json(output_dir / "context-review-packet.json", {"groups": []})
            storyboard = output_dir / "storyboards" / "G001.jpg"
            storyboard.parent.mkdir(parents=True, exist_ok=True)
            storyboard.write_bytes(b"storyboard")
        elif action == "build-scene-dialogue-review-packet":
            output = Path(command[command.index("--output") + 1])
            write_json(output, {"schema_version": "scene-dialogue-review-packet/v1"})
        return {"stdout": f"{action} ok", "stderr": ""}

    return fake_runner


def test_practical_ready_story_day_manifest_alias_is_supported(tmp_path: Path) -> None:
    batch = load_batch_module()
    asset, _ = make_asset(tmp_path, "practical-ready")
    asset["practical_ready"] = asset.pop("eligible_for_golden_v3_extraction")
    manifest = make_manifest(
        tmp_path,
        [{**asset, "eligible_for_golden_v3_extraction": True}],
    )
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["schema_version"] = "travel-video-story-day-preflight/v2"
    value["assets"][0].pop("eligible_for_golden_v3_extraction")
    value["assets"][0]["practical_ready"] = True
    day = value["story_days"][0]
    day["practical_ready_assets"] = day.pop("eligible_assets")
    day.pop("eligible_asset_count")
    write_json(manifest, value)

    result = batch.prepare_batch(
        manifest,
        tmp_path / "golden-v3",
        jobs=1,
        runner=make_fake_runner([]),
    )

    assert result["summary"]["completed"] == 1
    assert result["summary"]["excluded"] == 0


def test_prepare_orders_multimodal_stages_and_resumes_by_content_hash(
    tmp_path: Path,
) -> None:
    batch = load_batch_module()
    asset, _ = make_asset(tmp_path, "clip-a")
    manifest = make_manifest(tmp_path, [asset])
    output_root = tmp_path / "golden-v3"
    calls: list[list[str]] = []
    runner = make_fake_runner(calls)

    first = batch.prepare_batch(manifest, output_root, jobs=1, runner=runner)

    assert first["summary"] == {
        "selected_asset_count": 1,
        "completed": 1,
        "reused": 0,
        "blocked": 0,
        "excluded": 0,
        "locked": 0,
        "failed": 0,
    }
    assert [command[3] for command in calls] == [
        "build-boundary-proposals",
        "validate-boundary-proposals",
        "validate-visual-moments",
        "build-context-packet",
        "build-scene-dialogue-review-packet",
    ]
    packet_command = calls[-1]
    assert "--boundary-proposals" in packet_command
    assert "--visual-moments" in packet_command
    assert "--visual-packet" in packet_command
    assert first["prepare_only"] is True
    state_path = Path(first["records"][0]["state"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["policy"] == {
        "prepare_only": True,
        "model_review_performed": False,
        "review_merge_performed": False,
        "edit_render_performed": False,
        "source_or_proxy_media_modified": False,
    }
    assert len(state["implementation"]["digest"]) == 64
    assert len(state["implementation"]["orchestrator_digest"]) == 64
    assert state["implementation"]["orchestrator"]["component"] == (
        "scripts/run_golden_v3_batch.py"
    )
    assert len(state["config_digest"]) == 64
    assert all(
        len(stage["signature"]) == 64 and len(stage["config_digest"]) == 64
        for stage in state["stages"].values()
    )

    calls.clear()
    second = batch.prepare_batch(manifest, output_root, jobs=1, runner=runner)
    assert calls == []
    assert second["summary"]["reused"] == 1
    assert set(second["records"][0]["stage_statuses"].values()) == {"reused"}

    apple_path = Path(str(asset["paths"]["apple_transcript"]))
    write_json(apple_path, {"schema_version": "apple-stt/v1", "changed": True})
    calls.clear()
    third = batch.prepare_batch(manifest, output_root, jobs=1, runner=runner)
    assert third["summary"]["completed"] == 1
    assert [command[3] for command in calls] == [
        "build-boundary-proposals",
        "validate-boundary-proposals",
        "validate-visual-moments",
        "build-context-packet",
        "build-scene-dialogue-review-packet",
    ]


def test_selects_story_days_and_excludes_preflight_blocked_assets(
    tmp_path: Path,
) -> None:
    batch = load_batch_module()
    ready, _ = make_asset(tmp_path, "ready", story_day="2026-08-20")
    excluded, _ = make_asset(
        tmp_path,
        "excluded",
        story_day="2026-08-20",
        eligible=False,
        legacy_grouping=False,
    )
    other, _ = make_asset(tmp_path, "other", story_day="2026-08-21")
    manifest = make_manifest(tmp_path, [ready, excluded, other])
    calls: list[list[str]] = []

    result = batch.prepare_batch(
        manifest,
        tmp_path / "golden-v3",
        requested_days=["2026-08-20"],
        jobs=2,
        runner=make_fake_runner(calls),
    )

    assert result["selection"]["story_days"] == ["2026-08-20"]
    assert result["summary"]["completed"] == 1
    assert result["summary"]["excluded"] == 1
    assert {item["source_relative_path"] for item in result["records"]} == {
        "cam/ready.MP4",
        "cam/excluded.MP4",
    }
    assert all("other" not in " ".join(command) for command in calls)


def test_missing_grouping_is_blocked_but_accepted_override_runs(tmp_path: Path) -> None:
    batch = load_batch_module()
    asset, reviewed_path = make_asset(tmp_path, "needs-grouping", legacy_grouping=False)
    # The grouping product exists, but preflight will not trust it without an
    # explicit accepted override.
    machine = json.loads(
        Path(str(asset["paths"]["phase1_machine"])).read_text(encoding="utf-8")
    )
    write_json(
        reviewed_path,
        {
            **machine,
            "schema_version": "phase1-reviewed-timeline/v1",
            "segments": [
                {**segment, "review": {"visual_summary": "reviewed"}}
                for segment in machine["segments"]
            ],
            "reviewed_groups": [
                {
                    "group_id": "G001",
                    "label": "accepted",
                    "summary": "accepted grouping",
                    "segment_ids": ["S001", "S002"],
                }
            ],
        },
    )
    manifest = make_manifest(tmp_path, [asset])
    blocked = batch.prepare_batch(
        manifest,
        tmp_path / "blocked-run",
        runner=make_fake_runner([]),
    )
    assert blocked["summary"]["blocked"] == 1
    assert blocked["records"][0]["reason"] == "accepted_quick_grouping_unavailable"

    overrides = tmp_path / "overrides.json"
    write_json(
        overrides,
        {
            "schema_version": "golden-v3-grouping-overrides/v1",
            "assets": [
                {
                    "source_relative_path": asset["source_relative_path"],
                    "timeline_reviewed_path": str(reviewed_path),
                    "timeline_sha256": batch.sha256_file(reviewed_path),
                    "accepted": True,
                }
            ],
        },
    )
    calls: list[list[str]] = []
    prepared = batch.prepare_batch(
        manifest,
        tmp_path / "accepted-run",
        grouping_overrides_path=overrides,
        runner=make_fake_runner(calls),
    )
    assert prepared["summary"]["completed"] == 1
    assert prepared["records"][0]["grouping_origin"] == "golden_v3_accepted_override"
    assert calls


def test_per_asset_failure_is_isolated_and_written_to_day_manifest(
    tmp_path: Path,
) -> None:
    batch = load_batch_module()
    good, _ = make_asset(tmp_path, "good")
    bad, _ = make_asset(tmp_path, "bad")
    manifest = make_manifest(tmp_path, [good, bad])
    calls: list[list[str]] = []

    result = batch.prepare_batch(
        manifest,
        tmp_path / "golden-v3",
        jobs=2,
        runner=make_fake_runner(calls, fail_asset="bad--asset"),
    )

    assert result["summary"]["completed"] == 1
    assert result["summary"]["failed"] == 1
    failed = next(item for item in result["records"] if item["status"] == "failed")
    assert failed["source_relative_path"] == "cam/bad.MP4"
    assert "intentional failure" in failed["error"]["message"]
    day_manifest = json.loads(
        Path(result["day_manifests"]["2026-08-20"]).read_text(encoding="utf-8")
    )
    assert day_manifest["summary"]["failed"] == 1
    assert day_manifest["summary"]["completed"] == 1


def test_held_stage_lock_returns_immediately_without_running(tmp_path: Path) -> None:
    batch = load_batch_module()
    asset, _ = make_asset(tmp_path, "locked")
    manifest = make_manifest(tmp_path, [asset])
    output_root = tmp_path / "golden-v3"
    asset_id = batch.asset_id_from_machine(asset)
    lock_path = output_root / "assets" / asset_id / "locks" / "boundaries.lock"
    calls: list[list[str]] = []

    with batch.nonblocking_lock(lock_path):
        result = batch.prepare_batch(
            manifest,
            output_root,
            runner=make_fake_runner(calls),
        )

    assert result["summary"]["locked"] == 1
    assert calls == []
    assert "already held" in result["records"][0]["error"]["message"]


def test_rejects_output_inside_source_or_proxy_tree(tmp_path: Path) -> None:
    batch = load_batch_module()
    asset, _ = make_asset(tmp_path, "clip")
    manifest = make_manifest(tmp_path, [asset])

    with pytest.raises(ValueError, match="source_root"):
        batch.prepare_batch(manifest, tmp_path / "source" / "analysis")
    with pytest.raises(ValueError, match="proxy_root"):
        batch.prepare_batch(manifest, tmp_path / "proxies" / "analysis")
