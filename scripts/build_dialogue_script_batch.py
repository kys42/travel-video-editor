#!/usr/bin/env python3
"""Build non-destructive readable dialogue timelines for an analysis archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from travel_video.dialogue_script import attach_dialogue_script
from travel_video.phase1 import atomic_json


STATE_SCHEMA = "dialogue-script-batch-asset-state/v1"
SAFE_ASSET_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--pattern", default="*/*/timeline.final.json")
    parser.add_argument("--max-gap", type=float, default=2.2)
    parser.add_argument("--max-duration", type=float, default=20.0)
    parser.add_argument("--max-chars", type=int, default=160)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def output_paths(output_root: Path, asset_id: str) -> tuple[Path, Path]:
    if not SAFE_ASSET_ID.fullmatch(asset_id):
        raise ValueError(f"Unsafe asset_id: {asset_id!r}")
    output_root = output_root.expanduser().resolve()
    asset_root = (output_root / asset_id).resolve()
    if asset_root.parent != output_root:
        raise ValueError(f"Asset output escapes output root: {asset_id!r}")
    return asset_root / "timeline.scripted.json", asset_root / "state.json"


def reusable_output(
    output: Path,
    state_path: Path,
    *,
    input_record: dict[str, str],
    policy: dict[str, float | int],
) -> bool:
    if not output.is_file() or not state_path.is_file():
        return False
    try:
        state = load_json(state_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        state.get("schema_version") == STATE_SCHEMA
        and state.get("status") == "completed"
        and state.get("input") == input_record
        and state.get("policy") == policy
        and state.get("output") == {"path": str(output), "sha256": sha256_file(output)}
    )


def main() -> int:
    args = parse_args()
    if args.max_gap < 0 or args.max_duration <= 0 or args.max_chars < 12:
        raise SystemExit("Invalid grouping limits")
    sources = sorted(args.source_root.expanduser().resolve().glob(args.pattern))
    if not sources:
        raise SystemExit("No timeline.final.json files matched")

    output_root = args.output_root.expanduser().resolve()
    policy: dict[str, float | int] = {
        "max_gap": args.max_gap,
        "max_duration": args.max_duration,
        "max_chars": args.max_chars,
    }
    records: list[dict[str, Any]] = []
    seen_asset_ids: set[str] = set()
    for source in sources:
        timeline = load_json(source)
        asset_id = str(timeline.get("asset_id") or source.parent.parent.name)
        if asset_id in seen_asset_ids:
            raise ValueError(f"Duplicate asset_id: {asset_id}")
        seen_asset_ids.add(asset_id)
        output, state_path = output_paths(output_root, asset_id)
        input_record = {"path": str(source), "sha256": sha256_file(source)}
        status = "reused"
        if args.overwrite or not reusable_output(
            output,
            state_path,
            input_record=input_record,
            policy=policy,
        ):
            attach_dialogue_script(
                source,
                output,
                max_gap=args.max_gap,
                max_duration=args.max_duration,
                max_chars=args.max_chars,
            )
            status = "completed"
            atomic_json(
                state_path,
                {
                    "schema_version": STATE_SCHEMA,
                    "asset_id": asset_id,
                    "status": "completed",
                    "completed_at": datetime.now(UTC).isoformat(),
                    "input": input_record,
                    "policy": policy,
                    "output": {
                        "path": str(output),
                        "sha256": sha256_file(output),
                    },
                },
            )
        scripted = load_json(output)
        script = scripted["dialogue_script"]
        records.append(
            {
                "asset_id": asset_id,
                "source": str(source),
                "source_sha256": input_record["sha256"],
                "output": str(output),
                "output_sha256": sha256_file(output),
                "status": status,
                "source_utterance_count": script["source_utterance_count"],
                "line_count": script["line_count"],
            }
        )

    summary = {
        "schema_version": "dialogue-script-batch/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_root": str(args.source_root.expanduser().resolve()),
        "output_root": str(output_root),
        "policy": policy,
        "video_count": len(records),
        "source_utterance_count": sum(
            item["source_utterance_count"] for item in records
        ),
        "line_count": sum(item["line_count"] for item in records),
        "records": records,
    }
    atomic_json(output_root / "manifest.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
