from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def load_runner() -> ModuleType:
    path = ROOT / "scripts/process_proxy_archive.py"
    spec = importlib.util.spec_from_file_location("process_proxy_archive", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_inventory_requires_latest_complete_event_and_maps_extension(tmp_path: Path) -> None:
    runner = load_runner()
    source_root = tmp_path / "source"
    proxy_root = tmp_path / "proxy"
    source_root.mkdir()
    proxy_root.mkdir()
    (source_root / "day").mkdir()
    (proxy_root / "day").mkdir()
    original = source_root / "day/CLIP.MP4"
    proxy = proxy_root / "day/CLIP.mp4"
    original.write_bytes(b"original")
    proxy.write_bytes(b"proxy")
    waiting_original = source_root / "day/WAIT.MOV"
    waiting_proxy = proxy_root / "day/WAIT.mp4"
    waiting_original.write_bytes(b"waiting-original")
    waiting_proxy.write_bytes(b"waiting-proxy")
    events = [
        {
            "event": "completed",
            "relative_path": "day/CLIP.MP4",
            "proxy": str(proxy),
            "proxy_bytes": proxy.stat().st_size,
        },
        {
            "event": "completed",
            "relative_path": "day/WAIT.MOV",
            "proxy": str(waiting_proxy),
        },
        {
            "event": "started",
            "relative_path": "day/WAIT.MOV",
            "proxy": str(waiting_proxy),
        },
    ]
    manifest = proxy_root / "proxy-manifest.jsonl"
    manifest.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")

    assets, excluded, warnings = runner.inventory_completed_proxies(
        source_root, proxy_root, manifest
    )

    assert warnings == []
    assert [asset.relative_path for asset in assets] == ["day/CLIP.MP4"]
    assert assets[0].original == original
    assert {item["relative_path"]: item["reason"] for item in excluded} == {
        "day/WAIT.MOV": "latest_event_started"
    }


def test_inventory_rejects_partial_sibling(tmp_path: Path) -> None:
    runner = load_runner()
    source_root = tmp_path / "source"
    proxy_root = tmp_path / "proxy"
    source_root.mkdir()
    proxy_root.mkdir()
    original = source_root / "CLIP.MP4"
    proxy = proxy_root / "CLIP.mp4"
    original.write_bytes(b"original")
    proxy.write_bytes(b"proxy")
    proxy.with_name("CLIP.partial.mp4").write_bytes(b"partial")
    manifest = proxy_root / "proxy-manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "event": "completed",
                "relative_path": "CLIP.MP4",
                "proxy": str(proxy),
            }
        ),
        encoding="utf-8",
    )

    assets, excluded, _ = runner.inventory_completed_proxies(
        source_root, proxy_root, manifest
    )

    assert assets == []
    assert excluded == [
        {"relative_path": "CLIP.MP4", "reason": "partial_sibling_present"}
    ]


def test_phase1_reuse_requires_all_evidence_files(tmp_path: Path) -> None:
    runner = load_runner()
    original = tmp_path / "source.MP4"
    proxy = tmp_path / "proxy.mp4"
    original.write_bytes(b"original")
    proxy.write_bytes(b"proxy")
    asset = runner.ProxyAsset("asset", "source.MP4", original, proxy, {})
    run_dir = tmp_path / "run"
    frame = run_dir / "frames/frame.jpg"
    sheet = run_dir / "contact_sheets/sheet.jpg"
    frame.parent.mkdir(parents=True)
    sheet.parent.mkdir(parents=True)
    frame.write_bytes(b"frame")
    sheet.write_bytes(b"sheet")
    write_json(run_dir / "run.json", {"status": "complete"})
    write_json(run_dir / "review-packet.json", {})
    (run_dir / "timeline.html").write_text("ok", encoding="utf-8")
    write_json(
        run_dir / "timeline.machine.json",
        {
            "schema_version": "phase1-machine/v1",
            "source": {
                "path": str(proxy),
                "quick_fingerprint": runner.quick_fingerprint(proxy),
            },
            "config": runner.PHASE1_CONFIG,
            "samples": [{"frame": str(frame)}],
            "segments": [{"representative_frame": str(frame)}],
            "contact_sheets": [{"path": str(sheet)}],
        },
    )

    assert runner.verify_phase1(run_dir, asset) == (True, "verified")
    sheet.unlink()
    assert runner.verify_phase1(run_dir, asset)[0] is False


def test_apple_reuse_requires_raw_and_normalized_intermediates(tmp_path: Path) -> None:
    runner = load_runner()
    original = tmp_path / "source.MP4"
    proxy = tmp_path / "proxy.mp4"
    original.write_bytes(b"original")
    proxy.write_bytes(b"proxy")
    asset = runner.ProxyAsset("asset", "source.MP4", original, proxy, {})
    output = tmp_path / "apple"
    fingerprint = runner.quick_fingerprint(proxy)
    paths = {
        "audio": str(output / "audio/source.wav"),
        "normalized": str(output / "normalized/candidates.json"),
        "activity": str(output / "vad/activity.json"),
        "raw": {
            "ko-KR": str(output / "raw/ko-KR.json"),
            "en-US": str(output / "raw/en-US.json"),
        },
    }
    for path in [paths["audio"], paths["normalized"], paths["activity"], *paths["raw"].values()]:
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("{}", encoding="utf-8")
    write_json(
        output / "run.json",
        {"status": "complete", "source": {"quick_fingerprint": fingerprint}},
    )
    write_json(
        output / "transcript.apple.json",
        {
            "source": {"quick_fingerprint": fingerprint},
            "locales": ["ko-KR", "en-US"],
            "detector": {"enabled": True, "sensitivity": "medium"},
            "paths": paths,
        },
    )

    assert runner.verify_apple_stt(output, asset) == (True, "verified")
    Path(paths["raw"]["en-US"]).unlink()
    assert runner.verify_apple_stt(output, asset)[0] is False
