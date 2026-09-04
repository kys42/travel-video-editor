from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build_dialogue_script_batch.py"


def load_batch_module():
    spec = importlib.util.spec_from_file_location("build_dialogue_script_batch", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_timeline(path: Path, *, asset_id: str, text: str = "안녕하세요") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "phase1-video-summarized-timeline/v1",
                "asset_id": asset_id,
                "segments": [],
                "context_groups": [],
                "reconciled_transcript": {
                    "utterances": [
                        {
                            "utterance_id": "U0001",
                            "window_id": "RW0001",
                            "start": 1.0,
                            "end": 2.0,
                            "language": "ko",
                            "original_text": text,
                            "confidence": 0.9,
                            "source_candidate_ids": ["APPLE-ko-KR-T0001"],
                        }
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def run_batch(
    monkeypatch: pytest.MonkeyPatch,
    batch,
    source_root: Path,
    output_root: Path,
    *extra: str,
) -> dict:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            str(source_root),
            "--output-root",
            str(output_root),
            *extra,
        ],
    )
    assert batch.main() == 0
    return json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))


def test_batch_reuses_only_matching_input_and_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    batch = load_batch_module()
    source_root = tmp_path / "source"
    output_root = tmp_path / "output"
    timeline = source_root / "0827" / "asset" / "timeline.final.json"
    write_timeline(timeline, asset_id="asset-safe")

    first = run_batch(monkeypatch, batch, source_root, output_root)
    assert first["records"][0]["status"] == "completed"
    first_hash = first["records"][0]["output_sha256"]

    second = run_batch(monkeypatch, batch, source_root, output_root)
    assert second["records"][0]["status"] == "reused"
    assert second["records"][0]["output_sha256"] == first_hash

    changed_policy = run_batch(
        monkeypatch,
        batch,
        source_root,
        output_root,
        "--max-gap",
        "1.1",
    )
    assert changed_policy["records"][0]["status"] == "completed"

    write_timeline(timeline, asset_id="asset-safe", text="다시 안녕하세요")
    changed_input = run_batch(
        monkeypatch,
        batch,
        source_root,
        output_root,
        "--max-gap",
        "1.1",
    )
    assert changed_input["records"][0]["status"] == "completed"
    state = json.loads(
        (output_root / "asset-safe" / "state.json").read_text(encoding="utf-8")
    )
    assert state["input"]["sha256"] == changed_input["records"][0]["source_sha256"]


@pytest.mark.parametrize("asset_id", ["../escape", "/tmp/escape", "asset/subdir"])
def test_batch_rejects_asset_ids_that_escape_output_root(
    tmp_path: Path, asset_id: str
) -> None:
    batch = load_batch_module()

    with pytest.raises(ValueError, match="Unsafe asset_id"):
        batch.output_paths(tmp_path / "output", asset_id)
