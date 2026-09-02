from __future__ import annotations

import html
import math
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageOps

from .phase1 import _font, atomic_json, format_time, hash_distance
from .review import load_json

CONTEXT_PACKET_SCHEMA = "phase1-context-packet/v1"
CONTEXT_REVIEW_SCHEMA = "phase1-context-review/v1"


def _group_range(
    timeline: dict[str, Any], group: dict[str, Any]
) -> tuple[float, float]:
    segments = {item["segment_id"]: item for item in timeline["segments"]}
    members = [segments[segment_id] for segment_id in group["segment_ids"]]
    return float(members[0]["start"]), float(members[-1]["end"])


def select_storyboard_samples(
    samples: list[dict[str, Any]],
    start: float,
    end: float,
    max_frames: int = 8,
) -> list[dict[str, Any]]:
    if max_frames < 1:
        raise ValueError("max_frames must be at least 1")
    candidates = [sample for sample in samples if start <= float(sample["time"]) < end]
    if not candidates:
        return []
    if len(candidates) <= max_frames:
        return candidates

    span = max(1.0, end - start)
    midpoint = (start + end) / 2.0
    first = max(
        candidates,
        key=lambda item: (
            float(item["quality"]) - 0.12 * abs(float(item["time"]) - midpoint) / span
        ),
    )
    selected = [first]
    remaining = [item for item in candidates if item["sample_id"] != first["sample_id"]]
    while remaining and len(selected) < max_frames:

        def diversity_score(item: dict[str, Any]) -> float:
            time_distance = (
                min(
                    abs(float(item["time"]) - float(other["time"]))
                    for other in selected
                )
                / span
            )
            visual_distance = min(
                hash_distance(item["average_hash"], other["average_hash"])
                for other in selected
            )
            return (
                time_distance * 0.55
                + visual_distance * 0.35
                + float(item["quality"]) * 0.10
            )

        chosen = max(remaining, key=diversity_score)
        selected.append(chosen)
        remaining = [
            item for item in remaining if item["sample_id"] != chosen["sample_id"]
        ]
    return sorted(selected, key=lambda item: float(item["time"]))


def _create_group_storyboard(
    group: dict[str, Any],
    selected: list[dict[str, Any]],
    output_path: Path,
) -> list[dict[str, Any]]:
    columns = 4
    tile_width, image_height, label_height = 320, 180, 42
    rows = max(1, math.ceil(len(selected) / columns))
    canvas = Image.new(
        "RGB", (columns * tile_width, rows * (image_height + label_height)), "#101319"
    )
    draw = ImageDraw.Draw(canvas)
    label_font = _font(16)
    detail_font = _font(13)
    cells: list[dict[str, Any]] = []
    for index, sample in enumerate(selected):
        row, column = divmod(index, columns)
        x, y = column * tile_width, row * (image_height + label_height)
        with Image.open(sample["frame"]) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            fitted = ImageOps.pad(image, (tile_width, image_height), color="#000000")
        canvas.paste(fitted, (x, y))
        cell = f"{chr(65 + row)}{column + 1}"
        draw.rectangle(
            (x, y + image_height, x + tile_width, y + image_height + label_height),
            fill="#1a202a",
        )
        draw.text(
            (x + 8, y + image_height + 3),
            f"{cell}  {sample['sample_id']}  {sample['timecode']}",
            font=label_font,
            fill="#f4f7fb",
        )
        draw.text(
            (x + 8, y + image_height + 23),
            f"quality={sample['quality']:.2f}  change={sample['visual_change']:.2f}",
            font=detail_font,
            fill="#aab6c7",
        )
        cells.append(
            {
                "cell": cell,
                "sample_id": sample["sample_id"],
                "time": sample["time"],
                "timecode": sample["timecode"],
                "frame": sample["frame"],
                "quality": sample["quality"],
                "visual_change": sample["visual_change"],
            }
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, quality=90, optimize=True)
    return cells


def _aggregate_transcript_candidates(
    segments: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    languages = {
        language
        for segment in segments
        for language in segment.get("transcript_candidates", {})
    }
    result: dict[str, dict[str, Any]] = {}
    for language in sorted(languages):
        seen: set[tuple[float, float, str]] = set()
        items: list[dict[str, Any]] = []
        for segment in segments:
            for item in segment.get("transcript_candidates", {}).get(language, []):
                key = (float(item["start"]), float(item["end"]), str(item["text"]))
                if key not in seen:
                    seen.add(key)
                    items.append(item)
        items.sort(key=lambda item: (float(item["start"]), float(item["end"])))
        result[language] = {
            "text": " ".join(item["text"] for item in items)[:1600],
            "items": items,
        }
    return result


def build_context_packet(
    reviewed_path: Path,
    output_dir: Path,
    *,
    max_frames: int = 8,
) -> tuple[dict[str, Any], Path]:
    timeline = load_json(reviewed_path)
    output_dir = output_dir.resolve()
    segment_map = {segment["segment_id"]: segment for segment in timeline["segments"]}
    groups: list[dict[str, Any]] = []
    for group in timeline["reviewed_groups"]:
        start, end = _group_range(timeline, group)
        selected = select_storyboard_samples(
            timeline["samples"], start, end, max_frames=max_frames
        )
        storyboard_path = output_dir / "storyboards" / f"{group['group_id']}.jpg"
        cells = _create_group_storyboard(group, selected, storyboard_path)
        member_segments = [
            segment_map[segment_id] for segment_id in group["segment_ids"]
        ]
        groups.append(
            {
                "group_id": group["group_id"],
                "label": group.get("label"),
                "prior_summary": group.get("summary"),
                "start": start,
                "end": end,
                "timecode": f"{format_time(start)}-{format_time(end)}",
                "segment_ids": group["segment_ids"],
                "segment_summaries": [
                    {
                        "segment_id": segment["segment_id"],
                        "summary": segment.get("review", {}).get("visual_summary"),
                    }
                    for segment in member_segments
                ],
                "storyboard": os.path.relpath(storyboard_path, output_dir),
                "candidate_frames": cells,
                "transcript_candidates": _aggregate_transcript_candidates(
                    member_segments
                ),
            }
        )
    packet = {
        "schema_version": CONTEXT_PACKET_SCHEMA,
        "asset_id": timeline["asset_id"],
        "source_name": timeline["source"]["name"],
        "reviewed_timeline": str(reviewed_path.resolve()),
        "instructions": [
            "Inspect every group storyboard before choosing its representative frame.",
            "Synthesize Korean and English transcript candidates using visual context; retain uncertainty.",
            "Choose representative_sample_id only from candidate_frames for that group.",
            "Describe the event across the whole storyboard, not only the selected representative frame.",
        ],
        "groups": groups,
    }
    packet_path = output_dir / "context-review-packet.json"
    atomic_json(packet_path, packet)
    return packet, packet_path


def validate_context_review(packet: dict[str, Any], review: dict[str, Any]) -> None:
    if packet.get("schema_version") != CONTEXT_PACKET_SCHEMA:
        raise ValueError("Unsupported context packet schema")
    if review.get("schema_version") != CONTEXT_REVIEW_SCHEMA:
        raise ValueError("Unsupported context review schema")
    if packet.get("asset_id") != review.get("asset_id"):
        raise ValueError("Context review asset_id does not match packet")
    packet_ids = [group["group_id"] for group in packet["groups"]]
    review_ids = [group.get("group_id") for group in review.get("groups", [])]
    if review_ids != packet_ids:
        raise ValueError("Context review groups must match packet order exactly")
    for source_group, reviewed_group in zip(
        packet["groups"], review["groups"], strict=True
    ):
        candidate_ids = {item["sample_id"] for item in source_group["candidate_frames"]}
        representative = reviewed_group.get("representative_sample_id")
        if representative not in candidate_ids:
            raise ValueError(
                f"Invalid representative frame for {source_group['group_id']}"
            )
        if not str(reviewed_group.get("narrative_summary", "")).strip():
            raise ValueError(
                f"Missing narrative_summary for {source_group['group_id']}"
            )
        for key_moment in reviewed_group.get("key_moments", []):
            if key_moment.get("sample_id") not in candidate_ids:
                raise ValueError(f"Invalid key moment for {source_group['group_id']}")


def create_context_html(timeline: dict[str, Any], output_path: Path) -> None:
    cards: list[str] = []
    for group in timeline["context_groups"]:
        context = group["context_review"]
        storyboard = os.path.relpath(group["storyboard"], output_path.parent)
        cards.append(
            f"""
            <article>
              <img src="{html.escape(storyboard)}" alt="{html.escape(group["group_id"])}">
              <h2>{html.escape(group["label"])}</h2>
              <div class="time">{html.escape(group["timecode"])}</div>
              <p>{html.escape(context["narrative_summary"])}</p>
              <p class="dialogue"><strong>대화:</strong> {html.escape(context.get("dialogue_summary", "—"))}</p>
              <p class="muted">대표 프레임: {html.escape(context["representative_sample_id"])} · {html.escape(context.get("representative_reason", ""))}</p>
            </article>
            """
        )
    document = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(timeline["source"]["name"])} scene context</title><style>
body {{ margin:0; padding:28px; background:#0d1015; color:#edf2f8; font:15px/1.55 -apple-system,BlinkMacSystemFont,sans-serif; }}
main {{ display:grid; gap:24px; max-width:1280px; margin:auto; }} article {{ background:#171c24; border:1px solid #2a3342; border-radius:12px; overflow:hidden; padding-bottom:18px; }}
article img {{ width:100%; display:block; background:#000; }} h1,h2,p,.time {{ margin-left:18px; margin-right:18px; }} h2 {{ margin-bottom:4px; }} .time,.muted {{ color:#9ba8ba; }} .dialogue {{ color:#c9e7ff; }}
</style></head><body><main><h1>{html.escape(timeline["source"]["name"])}</h1>{"".join(cards)}</main></body></html>"""
    output_path.write_text(document, encoding="utf-8")


def merge_context_review(
    reviewed_path: Path,
    packet_path: Path,
    review_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    timeline = load_json(reviewed_path)
    packet = load_json(packet_path)
    review = load_json(review_path)
    validate_context_review(packet, review)
    review_map = {group["group_id"]: group for group in review["groups"]}
    packet_map = {group["group_id"]: group for group in packet["groups"]}
    sample_map = {sample["sample_id"]: sample for sample in timeline["samples"]}
    context_groups: list[dict[str, Any]] = []
    for group in timeline["reviewed_groups"]:
        packet_group = packet_map[group["group_id"]]
        context_review = review_map[group["group_id"]]
        representative_id = context_review["representative_sample_id"]
        context_groups.append(
            {
                **group,
                "start": packet_group["start"],
                "end": packet_group["end"],
                "timecode": packet_group["timecode"],
                "storyboard": str(packet_path.parent / packet_group["storyboard"]),
                "representative_sample_id": representative_id,
                "representative_frame": sample_map[representative_id]["frame"],
                "context_review": context_review,
            }
        )
    final = {
        **timeline,
        "schema_version": "phase1-context-reviewed-timeline/v1",
        "context_groups": context_groups,
    }
    atomic_json(output_path, final)
    create_context_html(final, output_path.with_suffix(".html"))
    return final
