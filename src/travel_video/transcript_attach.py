from __future__ import annotations

import copy
import os
import unicodedata
from pathlib import Path
from typing import Any

from .phase1 import atomic_json, format_time
from .review import load_json
from .transcript_reconcile import RECONCILED_TRANSCRIPT_SCHEMA

ATTACHMENT_SCHEMA = "timeline-reconciled-transcript-attachment/v1"
SUPPORTED_TIMELINE_SCHEMAS = {
    "phase1-context-reviewed-timeline/v1",
    "phase1-video-summarized-timeline/v1",
}


def _normalized_source_path(value: Any) -> str:
    path = os.path.normpath(os.path.expanduser(str(value)))
    return unicodedata.normalize("NFC", path)


def _validate_source_identity(
    timeline: dict[str, Any], transcript: dict[str, Any]
) -> None:
    timeline_source = timeline.get("source", {})
    transcript_source = transcript.get("source", {})
    timeline_path = timeline_source.get("path")
    transcript_path = transcript_source.get("path")
    if not timeline_path or not transcript_path:
        raise ValueError("Timeline and transcript must both record source.path")
    if _normalized_source_path(timeline_path) != _normalized_source_path(
        transcript_path
    ):
        raise ValueError("Transcript source path does not match timeline source path")

    timeline_fingerprint = timeline_source.get("quick_fingerprint")
    transcript_fingerprint = transcript_source.get("quick_fingerprint")
    if not timeline_fingerprint or not transcript_fingerprint:
        raise ValueError(
            "Timeline and transcript must both record source.quick_fingerprint"
        )
    if str(timeline_fingerprint) != str(transcript_fingerprint):
        raise ValueError("Transcript fingerprint does not match timeline fingerprint")


def validate_transcript_timeline_alignment(
    timeline: dict[str, Any], transcript: dict[str, Any]
) -> None:
    if timeline.get("schema_version") not in SUPPORTED_TIMELINE_SCHEMAS:
        raise ValueError(
            "Transcript attachment requires a context-reviewed or summarized timeline"
        )
    if transcript.get("schema_version") != RECONCILED_TRANSCRIPT_SCHEMA:
        raise ValueError("Transcript attachment requires reconciled-transcript/v1")
    _validate_source_identity(timeline, transcript)

    duration = float(timeline.get("media", {}).get("duration", 0.0))
    if duration <= 0:
        raise ValueError("Timeline media duration must be positive")

    seen_ids: set[str] = set()
    previous_start = -1.0
    for utterance in transcript.get("utterances", []):
        utterance_id = str(utterance.get("utterance_id", "")).strip()
        if not utterance_id or utterance_id in seen_ids:
            raise ValueError("Reconciled utterance IDs must be present and unique")
        seen_ids.add(utterance_id)
        start = float(utterance["start"])
        end = float(utterance["end"])
        if start < 0 or end <= start or end > duration + 0.001:
            raise ValueError(
                f"Reconciled utterance {utterance_id} is outside timeline duration"
            )
        if start < previous_start:
            raise ValueError("Reconciled utterances must be in chronological order")
        previous_start = start
        if not str(utterance.get("language", "")).strip():
            raise ValueError(f"Reconciled utterance {utterance_id} has no language")
        if not isinstance(utterance.get("translations", {}), dict):
            raise ValueError(
                f"Reconciled utterance {utterance_id} translations must be an object"
            )
        source_ids = utterance.get("source_candidate_ids")
        if not isinstance(source_ids, list) or not source_ids:
            raise ValueError(
                f"Reconciled utterance {utterance_id} has no source candidate IDs"
            )


def _overlaps(item: dict[str, Any], start: float, end: float) -> bool:
    return float(item["start"]) < end and float(item["end"]) > start


def _attached_copy(
    utterance: dict[str, Any], interval_start: float, interval_end: float
) -> dict[str, Any]:
    source_start = float(utterance["start"])
    source_end = float(utterance["end"])
    return {
        **copy.deepcopy(utterance),
        "source_start": source_start,
        "source_end": source_end,
        "source_start_timecode": utterance.get("start_timecode")
        or format_time(source_start),
        "source_end_timecode": utterance.get("end_timecode")
        or format_time(source_end),
        "overlap_start": max(source_start, interval_start),
        "overlap_end": min(source_end, interval_end),
    }


def attach_reconciled_transcript(
    timeline_path: Path, transcript_path: Path, output_path: Path
) -> Path:
    timeline_path = timeline_path.expanduser().resolve()
    transcript_path = transcript_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path in {timeline_path, transcript_path}:
        raise ValueError("Transcript attachment output must be a new file")
    timeline = load_json(timeline_path)
    transcript = load_json(transcript_path)
    validate_transcript_timeline_alignment(timeline, transcript)

    result = copy.deepcopy(timeline)
    utterances = transcript.get("utterances", [])
    assigned_segments: set[str] = set()
    assigned_groups: set[str] = set()

    for segment in result.get("segments", []):
        attached = [
            _attached_copy(item, float(segment["start"]), float(segment["end"]))
            for item in utterances
            if _overlaps(item, float(segment["start"]), float(segment["end"]))
        ]
        segment["reconciled_utterances"] = attached
        assigned_segments.update(item["utterance_id"] for item in attached)

    for group in result.get("context_groups", []):
        attached = [
            _attached_copy(item, float(group["start"]), float(group["end"]))
            for item in utterances
            if _overlaps(item, float(group["start"]), float(group["end"]))
        ]
        group["reconciled_utterances"] = attached
        assigned_groups.update(item["utterance_id"] for item in attached)

    expected_ids = {str(item["utterance_id"]) for item in utterances}
    missing_segments = sorted(expected_ids - assigned_segments)
    missing_groups = sorted(expected_ids - assigned_groups)
    if missing_segments or missing_groups:
        details = []
        if missing_segments:
            details.append(f"segments: {', '.join(missing_segments)}")
        if missing_groups:
            details.append(f"groups: {', '.join(missing_groups)}")
        raise ValueError(
            "Timeline does not cover every reconciled utterance (" + "; ".join(details) + ")"
        )

    result["reconciled_transcript"] = {
        **copy.deepcopy(transcript),
        "attachment": {
            "schema_version": ATTACHMENT_SCHEMA,
            "timeline_input": str(timeline_path),
            "transcript_input": str(transcript_path),
            "source_timebase": "seconds_from_source_start",
            "utterance_count": len(utterances),
            "segment_attachment_count": sum(
                len(item["reconciled_utterances"])
                for item in result.get("segments", [])
            ),
            "group_attachment_count": sum(
                len(item["reconciled_utterances"])
                for item in result.get("context_groups", [])
            ),
        },
    }
    atomic_json(output_path, result)
    return output_path
