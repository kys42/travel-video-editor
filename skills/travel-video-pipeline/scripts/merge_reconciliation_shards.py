#!/usr/bin/env python3
"""Merge validated, non-overlapping transcript reconciliation shards."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


SHARD_SCHEMA = "transcript-reconciliation-shard/v1"
REVIEW_SCHEMA = "transcript-reconciliation-review/v1"
VALID_LANGUAGES = {"ko", "en", "mixed", "uncertain", "non_speech"}


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def validate_utterance(window: dict[str, Any], utterance: dict[str, Any]) -> None:
    window_id = window["window_id"]
    language = utterance.get("language")
    if language not in VALID_LANGUAGES:
        raise ValueError(f"{window_id}: invalid language {language!r}")
    start = float(utterance["start"])
    end = float(utterance["end"])
    if start < float(window["start"]) - 0.001 or end > float(window["end"]) + 0.001:
        raise ValueError(f"{window_id}: utterance is outside its window")
    if end <= start:
        raise ValueError(f"{window_id}: utterance end must be after start")
    allowed_ids = {
        candidate_id
        for candidate in window.get("apple_candidates", {}).values()
        for candidate_id in candidate.get("source_candidate_ids", [])
    }
    source_ids = utterance.get("source_candidate_ids")
    if not isinstance(source_ids, list) or not source_ids:
        raise ValueError(f"{window_id}: source_candidate_ids must be non-empty")
    if not set(source_ids) <= allowed_ids:
        raise ValueError(f"{window_id}: contains source candidate IDs outside packet")
    if language not in {"uncertain", "non_speech"} and not str(
        utterance.get("original_text", "")
    ).strip():
        raise ValueError(f"{window_id}: missing original_text")


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path)
    parser.add_argument("shards", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    packet_path = args.packet.expanduser().resolve()
    packet = load(packet_path)
    packet_windows = packet.get("windows", [])
    packet_by_id = {window["window_id"]: window for window in packet_windows}
    expected_ids = [window["window_id"] for window in packet_windows]
    merged: dict[str, dict[str, Any]] = {}
    provenance: list[dict[str, Any]] = []

    for shard_path_arg in args.shards:
        shard_path = shard_path_arg.expanduser().resolve()
        shard = load(shard_path)
        if shard.get("schema_version") != SHARD_SCHEMA:
            raise ValueError(f"Unsupported shard schema: {shard_path}")
        declared_packet = shard.get("packet")
        if declared_packet and Path(declared_packet).expanduser().resolve() != packet_path:
            raise ValueError(f"Shard references a different packet: {shard_path}")
        shard_ids: list[str] = []
        for reviewed_window in shard.get("windows", []):
            window_id = reviewed_window.get("window_id")
            if window_id not in packet_by_id:
                raise ValueError(f"Unknown window {window_id!r} in {shard_path}")
            if window_id in merged:
                raise ValueError(f"Duplicate window across shards: {window_id}")
            for utterance in reviewed_window.get("utterances", []):
                validate_utterance(packet_by_id[window_id], utterance)
            merged[window_id] = reviewed_window
            shard_ids.append(window_id)
        provenance.append(
            {
                "path": str(shard_path),
                "reviewer": shard.get("reviewer"),
                "window_ids": shard_ids,
            }
        )

    missing = [window_id for window_id in expected_ids if window_id not in merged]
    if missing:
        raise ValueError(f"Missing packet windows: {', '.join(missing)}")
    review = {
        "schema_version": REVIEW_SCHEMA,
        "reviewer": "parallel-subagents",
        "packet": str(packet_path),
        "provenance": provenance,
        "windows": [merged[window_id] for window_id in expected_ids],
    }
    atomic_write(args.output, review)
    print(
        json.dumps(
            {
                "output": str(args.output.expanduser().resolve()),
                "window_count": len(expected_ids),
                "shard_count": len(args.shards),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
