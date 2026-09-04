from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "scripts"
SCRIPT = SCRIPT_DIR / "finish_golden_v3_batch.py"


def load_module():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("finish_golden_v3_batch", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def make_prepared_asset(
    batch,
    tmp_path: Path,
    asset_id: str,
    *,
    story_day: str = "2026-08-20",
) -> dict[str, object]:
    prepared_root = tmp_path / "prepared" / "assets" / asset_id
    inputs_root = tmp_path / "inputs" / asset_id
    source = {
        "name": f"{asset_id}.mp4",
        "path": str(tmp_path / "proxies" / f"{asset_id}.mp4"),
        "quick_fingerprint": f"fingerprint-{asset_id}",
    }
    media = {"duration": 10.0}
    timeline_path = inputs_root / "timeline.reviewed.json"
    timeline = {
        "schema_version": "phase1-reviewed-timeline/v1",
        "asset_id": asset_id,
        "source": source,
        "media": media,
        "segments": [],
        "samples": [],
        "reviewed_groups": [],
    }
    write_json(timeline_path, timeline)
    apple = inputs_root / "transcript.apple.json"
    context = inputs_root / "context-review-packet.json"
    boundaries = inputs_root / "proposals.json"
    moments = inputs_root / "visual-moments.json"
    validation = inputs_root / "validation.json"
    for path, payload in (
        (apple, {"schema_version": "apple-stt/v1"}),
        (context, {"schema_version": "phase1-context-review-packet/v1"}),
        (boundaries, {"schema_version": "boundary-proposal/v1"}),
        (moments, {"schema_version": "visual-moment/v1"}),
        (validation, {"status": "pass"}),
    ):
        write_json(path, payload)
    packet_path = prepared_root / "scene-dialogue" / "review-packet.json"
    packet = {
        "schema_version": "scene-dialogue-review-packet/v1",
        "asset_id": asset_id,
        "source": source,
        "media": media,
        "inputs": {
            "apple_transcript": str(apple.resolve()),
            "timeline": str(timeline_path.resolve()),
            "visual_packet": str(context.resolve()),
            "boundary_proposals": str(boundaries.resolve()),
            "visual_moments": str(moments.resolve()),
        },
        "policy": {
            "policy_version": "dialogue-preservation/v1",
            "canonical_for_editing": True,
            "integrated_visual_review": True,
            "editorial_beats_required": True,
            "boundary_proposals_supplied": True,
            "visual_moments_supplied": True,
            "visual_moments_available": True,
        },
        "summary": {"visual_moment_count": 1},
        "scenes": [
            {
                "group_id": "G001",
                "visual_context": {
                    "visual_moments": [
                        {
                            "moment_id": "VM0001",
                            "representative_sample_id": "VMF0001",
                        }
                    ]
                },
            }
        ],
    }
    write_json(packet_path, packet)
    packet_inputs = {
        "apple_transcript": batch.file_record(apple),
        "timeline_reviewed": batch.file_record(timeline_path),
        "context_packet": batch.file_record(context),
        "boundary_proposals": batch.file_record(boundaries),
        "visual_moments": batch.file_record(moments),
        "evidence_validation": batch.file_record(validation),
    }
    state_path = prepared_root / "state.json"
    state = {
        "schema_version": "golden-v3-preparation-asset-state/v1",
        "asset_id": asset_id,
        "source_relative_path": f"cam/{asset_id}.MP4",
        "story_day": story_day,
        "status": "completed",
        "inputs": {"timeline_reviewed": batch.file_record(timeline_path)},
        "stages": {
            "scene_dialogue_packet": {
                "status": "completed",
                "inputs": packet_inputs,
                "outputs": {"review_packet": batch.file_record(packet_path)},
            }
        },
        "current_outputs": {
            "scene_dialogue_review_packet": str(packet_path.resolve()),
        },
    }
    write_json(state_path, state)
    return {
        "asset_id": asset_id,
        "source_relative_path": f"cam/{asset_id}.MP4",
        "story_day": story_day,
        "status": "completed",
        "state": str(state_path.resolve()),
        "outputs": {
            "scene_dialogue_review_packet": str(packet_path.resolve()),
        },
    }


def make_preparation_manifest(batch, tmp_path: Path, records: list[dict]) -> Path:
    source_manifest = tmp_path / "story-days.json"
    write_json(
        source_manifest,
        {
            "schema_version": "travel-video-story-day-preflight/v1",
            "source_root": str((tmp_path / "source").resolve()),
            "proxy_root": str((tmp_path / "proxies").resolve()),
        },
    )
    preparation = tmp_path / "prepared" / "manifest.json"
    write_json(
        preparation,
        {
            "schema_version": "golden-v3-preparation-manifest/v1",
            "story_day_manifest": batch.file_record(source_manifest),
            "records": records,
        },
    )
    return preparation


def write_scene_review(
    review_root: Path, asset_id: str, *, assign: bool = True
) -> Path:
    review_path = review_root / asset_id / "scene-dialogue" / "review.json"
    write_json(
        review_path,
        {
            "schema_version": "scene-dialogue-review/v1",
            "reviewer": "test-agent",
            "scenes": [
                {
                    "group_id": "G001",
                    "window_decisions": [],
                    "utterances": [],
                    "captions": [],
                    "editorial_beats": [
                        {
                            "beat_id": "EB001",
                            "source_visual_moment_ids": ["VM0001"] if assign else [],
                            "source_sample_ids": ["VMF0001"],
                        }
                    ],
                }
            ],
        },
    )
    return review_path


def write_summary(review_root: Path, asset_id: str) -> Path:
    path = review_root / asset_id / "summary" / "video-summary.json"
    write_json(
        path,
        {
            "schema_version": "phase1-video-summary/v1",
            "asset_id": asset_id,
            "title": "Title",
            "one_line_summary": "Summary",
            "narrative_summary": "Narrative",
            "chronological_events": [],
            "highlight_group_ids": [],
            "representative_sample_id": "VMF0001",
            "tags": ["travel"],
        },
    )
    return path


def make_fake_runner(calls: list[list[str]], *, fail_asset: str | None = None):
    lock = threading.Lock()

    def runner(command: list[str], cwd: Path) -> dict[str, str]:
        assert cwd == ROOT
        with lock:
            calls.append(command)
        action = command[3]
        if fail_asset and fail_asset in " ".join(command):
            raise RuntimeError(f"intentional failure for {fail_asset}")
        if action == "merge-scene-dialogue-review":
            timeline = json.loads(Path(command[4]).read_text(encoding="utf-8"))
            review = json.loads(Path(command[6]).read_text(encoding="utf-8"))
            beats = [
                {**beat, "group_id": scene["group_id"]}
                for scene in review["scenes"]
                for beat in scene.get("editorial_beats", [])
            ]
            output = Path(command[command.index("--output") + 1])
            write_json(
                output,
                {
                    **timeline,
                    "schema_version": "phase1-context-reviewed-timeline/v1",
                    "reviewed_dialogue": {
                        "policy": {"policy_version": "dialogue-preservation/v1"},
                        "policy_audit": {
                            "status": "pass",
                            "caption_coverage_ratio": 1.0,
                            "uncaptioned_lexical_utterance_ids": [],
                        },
                        "editorial_beats": beats,
                    },
                },
            )
        elif action == "build-video-summary-packet":
            timeline_path = Path(command[4]).resolve()
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
            output = Path(command[command.index("--output") + 1])
            write_json(
                output,
                {
                    "schema_version": "phase1-video-summary-packet/v1",
                    "asset_id": timeline["asset_id"],
                    "source": timeline["source"],
                    "timeline": str(timeline_path),
                    "groups": [],
                },
            )
        elif action == "merge-video-summary":
            timeline = json.loads(Path(command[4]).read_text(encoding="utf-8"))
            summary = json.loads(Path(command[6]).read_text(encoding="utf-8"))
            output = Path(command[command.index("--output") + 1])
            write_json(
                output,
                {
                    **timeline,
                    "schema_version": "phase1-video-summarized-timeline/v1",
                    "video_summary": summary,
                },
            )
        elif action == "render-web":
            output_dir = Path(command[command.index("--output-dir") + 1])
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "index.html").write_text("<html></html>", encoding="utf-8")
            write_json(output_dir / "manifest.json", {"assets": "relative"})
        return {"stdout": f"{action} ok", "stderr": ""}

    return runner


def test_waits_for_review_without_running_commands(tmp_path: Path) -> None:
    batch = load_module()
    record = make_prepared_asset(batch, tmp_path, "asset-a")
    preparation = make_preparation_manifest(batch, tmp_path, [record])
    calls: list[list[str]] = []

    result = batch.finish_batch(
        preparation,
        tmp_path / "reviews",
        tmp_path / "finished",
        runner=make_fake_runner(calls),
    )

    assert calls == []
    assert result["summary"]["awaiting_review"] == 1
    assert result["records"][0]["expected_review"].endswith(
        "asset-a/scene-dialogue/review.json"
    )


def test_accepts_one_day_preparation_manifest(tmp_path: Path) -> None:
    batch = load_module()
    record = make_prepared_asset(batch, tmp_path, "asset-a")
    root_manifest = make_preparation_manifest(batch, tmp_path, [record])
    root = json.loads(root_manifest.read_text(encoding="utf-8"))
    day_manifest = tmp_path / "prepared" / "days" / "2026-08-20" / "manifest.json"
    write_json(
        day_manifest,
        {
            "schema_version": "golden-v3-preparation-day-manifest/v1",
            "story_day": "2026-08-20",
            "source_manifest": root["story_day_manifest"],
            "records": [record],
        },
    )

    result = batch.finish_batch(
        day_manifest,
        tmp_path / "reviews",
        tmp_path / "finished",
        runner=make_fake_runner([]),
    )

    assert result["summary"]["awaiting_review"] == 1
    assert result["selection"]["story_days"] == ["2026-08-20"]


def test_builds_review_merge_and_summary_packet_then_waits(tmp_path: Path) -> None:
    batch = load_module()
    record = make_prepared_asset(batch, tmp_path, "asset-a")
    preparation = make_preparation_manifest(batch, tmp_path, [record])
    review_root = tmp_path / "reviews"
    write_scene_review(review_root, "asset-a")
    calls: list[list[str]] = []
    runner = make_fake_runner(calls)

    first = batch.finish_batch(
        preparation,
        review_root,
        tmp_path / "finished",
        runner=runner,
    )

    assert [command[3] for command in calls] == [
        "validate-scene-dialogue-review",
        "merge-scene-dialogue-review",
        "build-video-summary-packet",
    ]
    assert first["summary"]["awaiting_summary"] == 1
    record_result = first["records"][0]
    assert Path(record_result["summary_packet"]).is_file()
    state = json.loads(Path(record_result["state"]).read_text(encoding="utf-8"))
    audit_path = Path(state["current_outputs"]["scene_dialogue_audit"])
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["dialogue_policy"]["caption_coverage_ratio"] == 1.0
    assert audit["visual_moment_assignment"]["expected_count"] == 1

    calls.clear()
    second = batch.finish_batch(
        preparation,
        review_root,
        tmp_path / "finished",
        runner=runner,
    )
    assert calls == []
    assert second["summary"]["awaiting_summary"] == 1


def test_summary_resume_validates_merges_and_renders_relative_web(
    tmp_path: Path,
) -> None:
    batch = load_module()
    record = make_prepared_asset(batch, tmp_path, "asset-a")
    preparation = make_preparation_manifest(batch, tmp_path, [record])
    review_root = tmp_path / "reviews"
    write_scene_review(review_root, "asset-a")
    calls: list[list[str]] = []
    runner = make_fake_runner(calls)
    output_root = tmp_path / "finished"
    batch.finish_batch(preparation, review_root, output_root, runner=runner)
    write_summary(review_root, "asset-a")
    calls.clear()

    completed = batch.finish_batch(preparation, review_root, output_root, runner=runner)

    assert [command[3] for command in calls] == [
        "validate-video-summary",
        "merge-video-summary",
        "render-web",
    ]
    render = calls[-1]
    assert render[render.index("--assets") + 1] == "relative"
    assert completed["summary"]["completed"] == 1
    outputs = completed["records"][0]["outputs"]
    assert Path(outputs["timeline_dialogue_reviewed_summarized"]).is_file()
    assert Path(outputs["web_index"]).is_file()

    calls.clear()
    resumed = batch.finish_batch(preparation, review_root, output_root, runner=runner)
    assert calls == []
    assert resumed["summary"]["reused"] == 1


def test_rejects_tampered_prepared_packet_before_model_validation(
    tmp_path: Path,
) -> None:
    batch = load_module()
    record = make_prepared_asset(batch, tmp_path, "asset-a")
    preparation = make_preparation_manifest(batch, tmp_path, [record])
    review_root = tmp_path / "reviews"
    write_scene_review(review_root, "asset-a")
    packet_path = Path(str(record["outputs"]["scene_dialogue_review_packet"]))
    write_json(packet_path, {"tampered": True})
    calls: list[list[str]] = []

    result = batch.finish_batch(
        preparation,
        review_root,
        tmp_path / "finished",
        runner=make_fake_runner(calls),
    )

    assert calls == []
    assert result["summary"]["failed"] == 1
    assert "content hash changed" in result["records"][0]["error"]["message"]


def test_defense_in_depth_rejects_missing_visual_moment_assignment(
    tmp_path: Path,
) -> None:
    batch = load_module()
    record = make_prepared_asset(batch, tmp_path, "asset-a")
    preparation = make_preparation_manifest(batch, tmp_path, [record])
    review_root = tmp_path / "reviews"
    write_scene_review(review_root, "asset-a", assign=False)

    result = batch.finish_batch(
        preparation,
        review_root,
        tmp_path / "finished",
        runner=make_fake_runner([]),
    )

    assert result["summary"]["failed"] == 1
    assert (
        "every visual moment exactly once" in result["records"][0]["error"]["message"]
    )


def test_failure_isolated_between_assets(tmp_path: Path) -> None:
    batch = load_module()
    good = make_prepared_asset(batch, tmp_path, "good")
    bad = make_prepared_asset(batch, tmp_path, "bad")
    preparation = make_preparation_manifest(batch, tmp_path, [good, bad])
    review_root = tmp_path / "reviews"
    write_scene_review(review_root, "good")
    write_scene_review(review_root, "bad")

    result = batch.finish_batch(
        preparation,
        review_root,
        tmp_path / "finished",
        jobs=2,
        runner=make_fake_runner([], fail_asset="bad"),
    )

    assert result["summary"]["failed"] == 1
    assert result["summary"]["awaiting_summary"] == 1
    statuses = {item["asset_id"]: item["status"] for item in result["records"]}
    assert statuses == {"bad": "failed", "good": "awaiting_summary"}


def test_held_asset_stage_lock_returns_locked_without_commands(tmp_path: Path) -> None:
    batch = load_module()
    record = make_prepared_asset(batch, tmp_path, "asset-a")
    preparation = make_preparation_manifest(batch, tmp_path, [record])
    review_root = tmp_path / "reviews"
    write_scene_review(review_root, "asset-a")
    output_root = tmp_path / "finished"
    lock_path = output_root / "assets" / "asset-a" / "locks" / "review_merge.lock"
    calls: list[list[str]] = []

    with batch.nonblocking_lock(lock_path):
        result = batch.finish_batch(
            preparation,
            review_root,
            output_root,
            runner=make_fake_runner(calls),
        )

    assert calls == []
    assert result["summary"]["locked"] == 1
