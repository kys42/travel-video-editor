from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .phase1 import atomic_json, validate_coverage


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_review(machine: dict[str, Any], review: dict[str, Any]) -> None:
    if review.get("schema_version") != "phase1-review/v1":
        raise ValueError("Unsupported review schema")
    if review.get("asset_id") != machine.get("asset_id"):
        raise ValueError("Review asset_id does not match machine timeline")
    machine_ids = [segment["segment_id"] for segment in machine["segments"]]
    reviewed_segments = review.get("segments", [])
    reviewed_ids = [segment.get("segment_id") for segment in reviewed_segments]
    if reviewed_ids != machine_ids:
        raise ValueError("Reviewed segments must match machine segment order exactly")
    for segment in reviewed_segments:
        if not str(segment.get("visual_summary", "")).strip():
            raise ValueError(f"Missing visual_summary for {segment.get('segment_id')}")

    grouped_ids: list[str] = []
    for group in review.get("groups", []):
        segment_ids = group.get("segment_ids", [])
        if not segment_ids:
            raise ValueError(f"Empty reviewed group: {group.get('group_id')}")
        grouped_ids.extend(segment_ids)
    if grouped_ids != machine_ids:
        raise ValueError("Reviewed groups must be contiguous and cover every segment exactly once")
    validate_coverage(machine["segments"], float(machine["media"]["duration"]))


def merge_review(machine_path: Path, review_path: Path, output_path: Path) -> dict[str, Any]:
    machine = load_json(machine_path)
    review = load_json(review_path)
    validate_review(machine, review)
    annotations = {item["segment_id"]: item for item in review["segments"]}
    merged_segments: list[dict[str, Any]] = []
    for segment in machine["segments"]:
        merged = dict(segment)
        merged["review"] = annotations[segment["segment_id"]]
        merged_segments.append(merged)
    final = {
        **machine,
        "schema_version": "phase1-reviewed-timeline/v1",
        "segments": merged_segments,
        "reviewed_groups": review["groups"],
        "review_metadata": {
            "reviewer": review.get("reviewer"),
            "method": review.get("method"),
            "notes": review.get("notes"),
        },
    }
    atomic_json(output_path, final)
    return final
