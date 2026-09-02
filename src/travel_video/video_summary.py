from __future__ import annotations

from pathlib import Path
from typing import Any

from .phase1 import atomic_json
from .review import load_json

VIDEO_SUMMARY_PACKET_SCHEMA = "phase1-video-summary-packet/v1"
VIDEO_SUMMARY_REVIEW_SCHEMA = "phase1-video-summary/v1"
VIDEO_SUMMARIZED_TIMELINE_SCHEMA = "phase1-video-summarized-timeline/v1"


def _summary_candidates(timeline: dict[str, Any]) -> list[str]:
    candidate_ids: list[str] = []
    for group in timeline["context_groups"]:
        context = group["context_review"]
        for sample_id in [
            context["representative_sample_id"],
            *(item["sample_id"] for item in context.get("key_moments", [])),
            *(item["sample_id"] for item in context.get("notable_moments", [])),
        ]:
            if sample_id not in candidate_ids:
                candidate_ids.append(sample_id)
    return candidate_ids


def build_video_summary_packet(
    timeline_path: Path, output_path: Path
) -> dict[str, Any]:
    timeline = load_json(timeline_path)
    if timeline.get("schema_version") != "phase1-context-reviewed-timeline/v1":
        raise ValueError("video summary requires a context-reviewed timeline")

    groups: list[dict[str, Any]] = []
    for group in timeline["context_groups"]:
        context = group["context_review"]
        groups.append(
            {
                "group_id": group["group_id"],
                "label": group["label"],
                "start": group["start"],
                "end": group["end"],
                "timecode": group["timecode"],
                "narrative_summary": context["narrative_summary"],
                "dialogue_summary": context.get("dialogue_summary"),
                "representative_sample_id": context["representative_sample_id"],
                "key_moments": context.get("key_moments", []),
                "notable_moments": context.get("notable_moments", []),
            }
        )

    packet = {
        "schema_version": VIDEO_SUMMARY_PACKET_SCHEMA,
        "asset_id": timeline["asset_id"],
        "source": timeline["source"],
        "media": timeline["media"],
        "timeline": str(timeline_path.resolve()),
        "instructions": [
            "Synthesize the whole video only from the already-reviewed chronological scene groups; do not re-watch the source video.",
            "Write a concise one-line summary and a fuller narrative that explains the beginning, progression, and ending state.",
            "Return one chronological event for every group in exactly the same order, preserving group_id.",
            "Choose the video representative only from representative, key-moment, or notable sample candidates listed in this packet.",
            "Use highlight_group_ids for at most three groups that best explain why this video is worth revisiting or editing.",
        ],
        "representative_candidates": _summary_candidates(timeline),
        "groups": groups,
    }
    atomic_json(output_path, packet)
    return packet


def validate_video_summary(packet: dict[str, Any], review: dict[str, Any]) -> None:
    if packet.get("schema_version") != VIDEO_SUMMARY_PACKET_SCHEMA:
        raise ValueError("Unsupported video summary packet schema")
    if review.get("schema_version") != VIDEO_SUMMARY_REVIEW_SCHEMA:
        raise ValueError("Unsupported video summary review schema")
    if packet.get("asset_id") != review.get("asset_id"):
        raise ValueError("Video summary asset_id does not match packet")

    for field in ("title", "one_line_summary", "narrative_summary"):
        if not str(review.get(field, "")).strip():
            raise ValueError(f"Missing video summary {field}")

    representative = review.get("representative_sample_id")
    if representative not in packet.get("representative_candidates", []):
        raise ValueError("Invalid video summary representative sample")

    packet_group_ids = [group["group_id"] for group in packet["groups"]]
    events = review.get("chronological_events", [])
    if [event.get("group_id") for event in events] != packet_group_ids:
        raise ValueError("Video summary events must match group order exactly")
    for event in events:
        for field in ("headline", "description"):
            if not str(event.get(field, "")).strip():
                raise ValueError(
                    f"Missing video summary event {field} for {event.get('group_id')}"
                )

    highlights = review.get("highlight_group_ids", [])
    if (
        not isinstance(highlights, list)
        or len(highlights) > 3
        or len(highlights) != len(set(highlights))
        or any(group_id not in packet_group_ids for group_id in highlights)
    ):
        raise ValueError("Invalid video summary highlight_group_ids")

    tags = review.get("tags", [])
    if (
        not isinstance(tags, list)
        or not 1 <= len(tags) <= 8
        or any(not str(tag).strip() for tag in tags)
    ):
        raise ValueError("Video summary tags must contain 1 to 8 non-empty items")


def merge_video_summary(
    timeline_path: Path,
    packet_path: Path,
    review_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    timeline = load_json(timeline_path)
    packet = load_json(packet_path)
    review = load_json(review_path)
    validate_video_summary(packet, review)
    if packet.get("timeline") != str(timeline_path.resolve()):
        raise ValueError("Video summary packet points to a different timeline")
    final = {
        **timeline,
        "schema_version": VIDEO_SUMMARIZED_TIMELINE_SCHEMA,
        "video_summary": review,
    }
    atomic_json(output_path, final)
    return final
