from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "complete_scene_library_day.py"


def load_module():
    import sys
    if str(SCRIPT.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("complete_scene_library_day", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def setup_day(tmp_path: Path, *, summary: bool = True, validation: str = "pass", exception: str | None = None):
    prep = tmp_path / "prepared.json"
    asset = "asset-001"
    state = tmp_path / "prepared" / "assets" / asset / "state.json"
    packet = tmp_path / "prepared" / "assets" / asset / "packet.json"
    source = {"source_relative_path": "0826/clip.MP4", "story_day": "2026-08-26"}
    write_json(packet, {"schema_version": "scene-dialogue-review-packet/v1", "asset_id": asset, "scenes": []})
    write_json(state, {"asset_id": asset, **source, "status": "completed", "stages": {"scene_dialogue_packet": {"status": "completed", "outputs": {"review_packet": {"path": str(packet)}}}}})
    source_manifest = write_json(tmp_path / "source-manifest.json", {"schema_version": "travel-video-story-day-preflight/v1", "source_root": str(tmp_path / "source"), "proxy_root": str(tmp_path / "proxies"), "assets": [{"asset_id": asset, "practical_ready": True, "paths": {"original": str(tmp_path / "source" / source["source_relative_path"]), "proxy": str(tmp_path / "proxies" / "clip.mp4"), "lineage": str(tmp_path / "lineage.json")}, "source_quick_fingerprint": "source-fp", "proxy_quick_fingerprint": "proxy-fp", "duration_seconds": 10.0, **source}]})
    source_bytes = source_manifest.read_bytes()
    source_record = {"kind": "file", "path": str(source_manifest), "size_bytes": len(source_bytes), "sha256": hashlib.sha256(source_bytes).hexdigest()}
    write_json(prep, {"schema_version": "golden-v3-preparation-manifest/v1", "story_day_manifest": source_record, "records": [{"asset_id": asset, "state": str(state), **source}]})
    review = tmp_path / "reviews" / asset / "scene-dialogue" / "review.json"
    write_json(review, {"schema_version": "scene-dialogue-review/v1", "reviewer": "test", "scenes": []})
    if summary:
        write_json(tmp_path / "reviews" / asset / "summary" / "video-summary.json", {"schema_version": "phase1-video-summary/v1", "asset_id": asset})
    if exception:
        write_json(tmp_path / "reviews" / asset / "review-exceptions.json", {"schema_version": "scene-review-exceptions/v1", "items": [{"status": exception, "reason": "test"}]})
    return prep, asset


def patch_finish(monkeypatch, mod, tmp_path, *, status="completed", catalog="valid"):
    timeline = tmp_path / "timeline.json"
    write_json(timeline, {})
    def fake_finish(*args, **kwargs):
        return {"records": [{"asset_id": "asset-001", "source_relative_path": "0826/clip.MP4", "status": status, "outputs": {"timeline_dialogue_reviewed_summarized": str(timeline)}}]}
    monkeypatch.setattr(mod, "finish_batch", fake_finish)
    monkeypatch.setattr(mod, "build_relink_entry", lambda record, origin: {"asset_id": record["asset_id"], "candidates": []})
    def fake_render(_timelines, library_root, **_kwargs):
        if catalog != "missing":
            count = 1 if catalog == "valid" else 0
            write_json(Path(library_root) / "manifest.json", {"video_count": count, "proxy_video_count": count})
    monkeypatch.setattr(mod, "render_video_library", fake_render)


def test_no_parent_approval_needed_and_writes_completion(tmp_path, monkeypatch):
    mod = load_module()
    prep, asset = setup_day(tmp_path)
    patch_finish(monkeypatch, mod, tmp_path)
    report = mod.complete_day(prep, tmp_path / "reviews", tmp_path / "out", tmp_path / "library", story_day="2026-08-26")
    assert report["status"] == "complete"
    assert report["expected_assets"] == 1
    assert report["library_manifest"]
    assert (tmp_path / "out" / "completion.json").exists()
    assert (tmp_path / "out" / "work-queue.json").exists()


def test_recorded_preflight_exclusion_does_not_block_day(tmp_path, monkeypatch):
    mod = load_module()
    prep, _ = setup_day(tmp_path)
    patch_finish(monkeypatch, mod, tmp_path)
    source_path = tmp_path / "source-manifest.json"
    source = json.loads(source_path.read_text())
    excluded = {"asset_id": "excluded", "story_day": "2026-08-26",
                "source_relative_path": "0826/excluded.MP4", "practical_ready": False}
    source["assets"].append(excluded)
    write_json(source_path, source)
    data = json.loads(prep.read_text())
    raw = source_path.read_bytes()
    data["story_day_manifest"].update(size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    data["records"].append({**excluded, "status": "excluded"})
    write_json(prep, data)
    original_finish = mod.finish_batch
    def finish_with_excluded(*args, **kwargs):
        result = original_finish(*args, **kwargs)
        result["records"].append({**excluded, "status": "skipped_preparation"})
        return result
    monkeypatch.setattr(mod, "finish_batch", finish_with_excluded)
    report = mod.complete_day(prep, tmp_path / "reviews", tmp_path / "out", tmp_path / "library", story_day="2026-08-26")
    assert report["status"] == "complete"
    assert report["expected_assets"] == report["completed_assets"] == 1
    assert report["work_queue"] == []


def test_unknown_excluded_path_is_still_rejected(tmp_path, monkeypatch):
    import pytest
    mod = load_module()
    prep, _ = setup_day(tmp_path)
    patch_finish(monkeypatch, mod, tmp_path)
    data = json.loads(prep.read_text())
    data["records"].append({"asset_id": "unknown", "story_day": "2026-08-26",
                            "source_relative_path": "0826/unknown.MP4", "status": "excluded"})
    write_json(prep, data)
    with pytest.raises(ValueError, match="unexpected source paths"):
        mod.complete_day(prep, tmp_path / "reviews", tmp_path / "out", tmp_path / "library", story_day="2026-08-26")


def test_missing_summary_resumes_as_author_summary(tmp_path, monkeypatch):
    mod = load_module()
    prep, asset = setup_day(tmp_path, summary=False)
    patch_finish(monkeypatch, mod, tmp_path, status="awaiting_summary")
    report = mod.complete_day(prep, tmp_path / "reviews", tmp_path / "out", tmp_path / "library", story_day="2026-08-26")
    assert report["status"] == "in_progress"
    assert report["work_queue"][0]["action"] == "author_summary"


def test_validation_failure_and_open_exception_stay_out_of_library(tmp_path, monkeypatch):
    mod = load_module()
    prep, asset = setup_day(tmp_path, validation="fail", exception="open")
    patch_finish(monkeypatch, mod, tmp_path, status="failed")
    report = mod.complete_day(prep, tmp_path / "reviews", tmp_path / "out", tmp_path / "library", story_day="2026-08-26")
    assert report["status"] == "needs_attention"
    assert report["work_queue"][0]["action"] == "needs_model_review"
    assert report["library_manifest"] is None


def test_failed_without_explicit_exception_is_repair_validation(tmp_path, monkeypatch):
    mod = load_module()
    prep, asset = setup_day(tmp_path)
    patch_finish(monkeypatch, mod, tmp_path, status="failed")
    report = mod.complete_day(prep, tmp_path / "reviews", tmp_path / "out", tmp_path / "library", story_day="2026-08-26")
    assert report["status"] == "needs_attention"
    assert report["work_queue"][0]["action"] == "repair_validation"
    assert report["library_manifest"] is None


def test_quality_notes_warnings_do_not_block_completion(tmp_path, monkeypatch):
    mod = load_module()
    prep, asset = setup_day(tmp_path)
    patch_finish(monkeypatch, mod, tmp_path)
    write_json(tmp_path / "reviews" / asset / "quality-notes.json", {"warning_count": 99, "warnings": [{"signal": "long_beat"}]})
    report = mod.complete_day(prep, tmp_path / "reviews", tmp_path / "out", tmp_path / "library", story_day="2026-08-26")
    assert report["status"] == "complete"


def test_missing_summary_resumes_to_complete_on_second_call(tmp_path, monkeypatch):
    mod = load_module()
    prep, asset = setup_day(tmp_path, summary=False)
    patch_finish(monkeypatch, mod, tmp_path, status="awaiting_summary")
    first = mod.complete_day(prep, tmp_path / "reviews", tmp_path / "out", tmp_path / "library", story_day="2026-08-26")
    assert first["status"] == "in_progress"
    assert first["work_queue"][0]["action"] == "author_summary"
    write_json(tmp_path / "reviews" / asset / "summary" / "video-summary.json", {"schema_version": "phase1-video-summary/v1", "asset_id": asset})
    patch_finish(monkeypatch, mod, tmp_path, status="completed")
    second = mod.complete_day(prep, tmp_path / "reviews", tmp_path / "out", tmp_path / "library", story_day="2026-08-26")
    assert second["status"] == "complete"
    assert second["work_queue"] == []


def test_completed_open_exception_blocks_until_resolved_and_parent_review_is_ignored(tmp_path, monkeypatch):
    mod = load_module()
    prep, asset = setup_day(tmp_path)
    patch_finish(monkeypatch, mod, tmp_path, status="completed")
    write_json(tmp_path / "reviews" / asset / "parent-quality-review.json", {"rejected": True, "do_not_publish": True})
    write_json(tmp_path / "reviews" / asset / "review-exceptions.json", {"schema_version": "scene-review-exceptions/v1", "items": [{"status": "open", "reason": "needs model review"}]})
    blocked = mod.complete_day(prep, tmp_path / "reviews", tmp_path / "out", tmp_path / "library", story_day="2026-08-26")
    assert blocked["status"] == "needs_attention"
    assert blocked["work_queue"][0]["action"] == "needs_model_review"
    write_json(tmp_path / "reviews" / asset / "review-exceptions.json", {"schema_version": "scene-review-exceptions/v1", "items": [{"status": "resolved", "reason": "needs model review"}]})
    complete = mod.complete_day(prep, tmp_path / "reviews", tmp_path / "out", tmp_path / "library", story_day="2026-08-26")
    assert complete["status"] == "complete"


def test_rendered_catalog_must_match_delivered_asset_count(tmp_path, monkeypatch):
    for mode in ("missing", "mismatch"):
        case = tmp_path / mode
        mod = load_module()
        prep, _ = setup_day(case)
        patch_finish(monkeypatch, mod, case, catalog=mode)
        report = mod.complete_day(prep, case / "reviews", case / "out", case / "library", story_day="2026-08-26")
        assert report["status"] == "needs_attention"
        assert report["library_manifest"] is None
        assert report["work_queue"][-1]["action"] == "repair_delivery"


def test_subset_preparation_cannot_claim_whole_day(tmp_path, monkeypatch):
    mod = load_module()
    prep, asset = setup_day(tmp_path)
    patch_finish(monkeypatch, mod, tmp_path)
    source_path = tmp_path / "source-manifest.json"
    source = json.loads(source_path.read_text())
    source["assets"].append({"asset_id": "asset-002", "practical_ready": True, "source_relative_path": "0826/other.MP4", "story_day": "2026-08-26"})
    write_json(source_path, source)
    prep_data = json.loads(prep.read_text())
    raw = source_path.read_bytes()
    prep_data["story_day_manifest"].update({"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    write_json(prep, prep_data)
    report = mod.complete_day(prep, tmp_path / "reviews", tmp_path / "out", tmp_path / "library", story_day="2026-08-26")
    assert report["status"] != "complete"
