#!/usr/bin/env python3
"""Build non-destructive readable dialogue timelines for an analysis archive."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from travel_video.dialogue_script import attach_dialogue_script
from travel_video.phase1 import atomic_json


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


def main() -> int:
    args = parse_args()
    if args.max_gap < 0 or args.max_duration <= 0 or args.max_chars < 12:
        raise SystemExit("Invalid grouping limits")
    sources = sorted(args.source_root.expanduser().resolve().glob(args.pattern))
    if not sources:
        raise SystemExit("No timeline.final.json files matched")

    records: list[dict[str, Any]] = []
    for source in sources:
        timeline = load_json(source)
        asset_id = str(timeline.get("asset_id") or source.parent.parent.name)
        output = args.output_root.expanduser().resolve() / asset_id / "timeline.scripted.json"
        status = "reused"
        if args.overwrite or not output.is_file():
            attach_dialogue_script(
                source,
                output,
                max_gap=args.max_gap,
                max_duration=args.max_duration,
                max_chars=args.max_chars,
            )
            status = "completed"
        scripted = load_json(output)
        script = scripted["dialogue_script"]
        records.append(
            {
                "asset_id": asset_id,
                "source": str(source),
                "output": str(output),
                "status": status,
                "source_utterance_count": script["source_utterance_count"],
                "line_count": script["line_count"],
            }
        )

    summary = {
        "schema_version": "dialogue-script-batch/v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_root": str(args.source_root.expanduser().resolve()),
        "output_root": str(args.output_root.expanduser().resolve()),
        "policy": {
            "max_gap": args.max_gap,
            "max_duration": args.max_duration,
            "max_chars": args.max_chars,
        },
        "video_count": len(records),
        "source_utterance_count": sum(
            item["source_utterance_count"] for item in records
        ),
        "line_count": sum(item["line_count"] for item in records),
        "records": records,
    }
    atomic_json(args.output_root.expanduser().resolve() / "manifest.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
