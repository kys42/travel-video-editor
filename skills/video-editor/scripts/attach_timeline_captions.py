#!/usr/bin/env python3
"""Attach reviewed dialogue captions to a video-edit-plan output timeline."""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any


ALLOWED_LANGUAGES = {"ko", "en", "mixed"}
REVIEWED_CAPTION_STATUSES = {"reviewed", "verified"}
REVIEWED_DIALOGUE_SOURCE = "reviewed_dialogue.captions"
DIALOGUE_SCRIPT_SOURCE = "dialogue_script.lines"
RECONCILED_TRANSCRIPT_SOURCE = "reconciled_transcript.utterances"
DIALOGUE_PRESERVATION_POLICY = "dialogue-preservation/v1"
REVIEWED_BOUNDARY_TOLERANCE_SECONDS = 0.05


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected an object: {path}")
    return value


def split_text(text: str, max_chars: int) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = word if not current else f"{current} {word}"
        if current and len(candidate) > max_chars:
            parts.append(current)
            current = word
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts or [text]


def overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return min(a_end, b_end) - max(a_start, b_start) > 0.2


def transition_duration(clip: dict[str, Any]) -> float:
    transition = clip.get("transition_after")
    if transition is None or transition == "none":
        return 0.0
    if isinstance(transition, str):
        return 0.35
    if not isinstance(transition, dict):
        raise ValueError("transition_after must be an object or type string")
    if transition.get("type") == "none":
        return 0.0
    duration = float(transition.get("duration", 0.35))
    if duration <= 0:
        raise ValueError("transition_after.duration must be positive")
    return duration


def caption_source(timeline: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Choose the newest available caption source without reviving rejected text."""
    reviewed_dialogue = timeline.get("reviewed_dialogue")
    if isinstance(reviewed_dialogue, dict) and "captions" in reviewed_dialogue:
        policy_version = reviewed_dialogue.get("policy", {}).get("policy_version")
        if policy_version == DIALOGUE_PRESERVATION_POLICY:
            audit = reviewed_dialogue.get("policy_audit")
            if (
                not isinstance(audit, dict)
                or audit.get("status") != "pass"
                or float(audit.get("caption_coverage_ratio", 0.0)) != 1.0
                or audit.get("uncaptioned_lexical_utterance_ids")
            ):
                raise ValueError(
                    "dialogue-preservation/v1 captions require a passing policy audit"
                )
        captions = reviewed_dialogue["captions"]
        if not isinstance(captions, list):
            raise ValueError(f"{REVIEWED_DIALOGUE_SOURCE} must be a list")
        return REVIEWED_DIALOGUE_SOURCE, captions

    dialogue_script = timeline.get("dialogue_script")
    if isinstance(dialogue_script, dict) and "lines" in dialogue_script:
        lines = dialogue_script["lines"]
        if not isinstance(lines, list):
            raise ValueError(f"{DIALOGUE_SCRIPT_SOURCE} must be a list")
        return DIALOGUE_SCRIPT_SOURCE, lines

    reconciled_transcript = timeline.get("reconciled_transcript")
    if isinstance(reconciled_transcript, dict):
        utterances = reconciled_transcript.get("utterances", [])
        if not isinstance(utterances, list):
            raise ValueError(f"{RECONCILED_TRANSCRIPT_SOURCE} must be a list")
        return RECONCILED_TRANSCRIPT_SOURCE, utterances
    return RECONCILED_TRANSCRIPT_SOURCE, []


def source_text(item: dict[str, Any], source: str) -> str:
    field = "display_text" if source == REVIEWED_DIALOGUE_SOURCE else "original_text"
    return str(item.get(field) or "").strip()


def source_kind(item: dict[str, Any], source: str) -> str:
    if source == REVIEWED_DIALOGUE_SOURCE:
        return str(item.get("edit_type") or "verified")
    return "stt"


def reviewed_caption_is_usable(item: dict[str, Any]) -> bool:
    status = str(item.get("review_status") or "").strip().lower()
    return status in REVIEWED_CAPTION_STATUSES and item.get("usable") is not False


def _normalized_name(value: Any) -> str:
    normalized = unicodedata.normalize(
        "NFC", os.path.normpath(os.path.expanduser(str(value)))
    )
    return Path(normalized).name.casefold()


def _normalized_parts(value: Any) -> tuple[str, ...]:
    normalized = unicodedata.normalize(
        "NFC", os.path.normpath(os.path.expanduser(str(value)))
    )
    return tuple(part.casefold() for part in Path(normalized).parts if part != os.sep)


def _ends_with_parts(value: Any, suffix: Any) -> bool:
    value_parts = _normalized_parts(value)
    suffix_parts = _normalized_parts(suffix)
    return bool(suffix_parts) and value_parts[-len(suffix_parts) :] == suffix_parts


def validate_timeline_identity(
    clip: dict[str, Any],
    metadata: dict[str, Any],
    timeline: dict[str, Any],
    timeline_path: Path,
    source_stage: str,
) -> None:
    """Fail closed when a clip is paired with another asset's timeline."""
    policy_version = (
        timeline.get("reviewed_dialogue", {}).get("policy", {}).get("policy_version")
    )
    strict = (
        source_stage == REVIEWED_DIALOGUE_SOURCE
        and policy_version == DIALOGUE_PRESERVATION_POLICY
    )
    timeline_source = timeline.get("source")
    if not isinstance(timeline_source, dict):
        if strict:
            raise ValueError(
                f"Reviewed timeline is missing source identity: {timeline_path}"
            )
        return

    timeline_names = {
        _normalized_name(value)
        for value in (timeline_source.get("path"), timeline_source.get("name"))
        if value
    }
    clip_names = {
        _normalized_name(value)
        for value in (
            metadata.get("source_relative_path"),
            clip.get("source"),
            clip.get("proxy"),
        )
        if value
    }
    if not timeline_names or not clip_names:
        if strict:
            raise ValueError(
                f"Reviewed clip/timeline source identity is incomplete: {timeline_path}"
            )
    elif timeline_names.isdisjoint(clip_names):
        raise ValueError(
            "Clip source does not match reviewed timeline source: "
            f"clip={sorted(clip_names)}, timeline={sorted(timeline_names)}"
        )

    source_relative_path = metadata.get("source_relative_path")
    timeline_source_path = timeline_source.get("path")
    clip_source_path = clip.get("source")
    if strict and not source_relative_path:
        raise ValueError(
            "dialogue-preservation/v1 clips require metadata.source_relative_path"
        )
    if source_relative_path:
        if not timeline_source_path or not _ends_with_parts(
            timeline_source_path, source_relative_path
        ):
            raise ValueError(
                "Clip relative path does not match reviewed timeline source: "
                f"{source_relative_path}"
            )
        if clip_source_path and not _ends_with_parts(
            clip_source_path, source_relative_path
        ):
            raise ValueError(
                "Clip source does not match its source_relative_path: "
                f"{source_relative_path}"
            )

    timeline_fingerprint = timeline_source.get("quick_fingerprint")
    expected_fingerprint = metadata.get("timeline_quick_fingerprint")
    if strict and not expected_fingerprint:
        raise ValueError(
            "dialogue-preservation/v1 clips require metadata.timeline_quick_fingerprint"
        )
    if expected_fingerprint and str(expected_fingerprint) != str(timeline_fingerprint):
        raise ValueError(
            f"Clip metadata fingerprint does not match reviewed timeline: {timeline_path}"
        )
    if strict and not timeline_fingerprint:
        raise ValueError(
            f"Reviewed timeline is missing source.quick_fingerprint: {timeline_path}"
        )

    duration = float(timeline.get("media", {}).get("duration", 0.0))
    if strict and duration <= 0:
        raise ValueError(
            f"Reviewed timeline is missing media duration: {timeline_path}"
        )
    if duration > 0 and float(clip["source_out"]) > duration + 0.05:
        raise ValueError(
            f"Clip range exceeds reviewed timeline duration: {timeline_path}"
        )


def resolve_timeline_path(
    clip: dict[str, Any], metadata: dict[str, Any], timeline_root: Path | None
) -> Path | None:
    """Resolve a newly reviewed timeline without rewriting an older edit plan first."""
    if timeline_root is None:
        value = metadata.get("timeline_final")
        return Path(value) if value else None

    source_value = metadata.get("source_relative_path") or clip.get("source")
    if not source_value:
        raise ValueError("--timeline-root requires clip source or source_relative_path")
    source_stem = Path(str(source_value)).stem
    matches = sorted(
        timeline_root.glob(
            f"{source_stem}--*/scene-dialogue/timeline.dialogue-reviewed.summarized.json"
        )
    )
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one reviewed timeline for {source_stem} under "
            f"{timeline_root}; found {len(matches)}"
        )
    return matches[0]


def source_id_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Retain source identifiers in the editable plan for auditability."""
    retained: dict[str, Any] = {}
    for key, value in item.items():
        if (
            key in {"id", "caption_id", "source_ids"}
            or key.startswith("source_")
            and (key.endswith("_id") or key.endswith("_ids"))
        ):
            retained[key] = value
    return retained


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-confidence", type=float, default=0.82)
    parser.add_argument("--min-coverage", type=float, default=0.65)
    parser.add_argument("--max-chars", type=int, default=34)
    parser.add_argument(
        "--timeline-root",
        type=Path,
        help=(
            "Resolve each clip to <source-stem>--*/scene-dialogue/"
            "timeline.dialogue-reviewed.summarized.json under this directory"
        ),
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Discard existing plan captions before attaching reviewed dialogue",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.output.exists() and not args.overwrite:
        raise SystemExit(f"Output exists; pass --overwrite: {args.output}")
    if not 0 <= args.min_confidence <= 1:
        raise SystemExit("--min-confidence must be between 0 and 1")
    if not 0 < args.min_coverage <= 1:
        raise SystemExit("--min-coverage must be greater than 0 and at most 1")
    if args.max_chars < 12:
        raise SystemExit("--max-chars must be at least 12")

    plan = load_json(args.plan)
    clips = plan.get("clips")
    if not isinstance(clips, list) or not clips:
        raise SystemExit("Plan must contain a non-empty clips list")
    existing_captions = list(plan.get("captions") or [])
    retained = [] if args.replace_existing else existing_captions
    generated: list[dict[str, Any]] = []
    timelines: set[str] = set()
    source_item_counts: dict[str, int] = {}
    accepted_source_item_counts: dict[str, int] = {}
    cursor = 0.0

    for clip in clips:
        source_in = float(clip["source_in"])
        source_out = float(clip["source_out"])
        speed = float(clip.get("speed", 1.0))
        output_duration = (source_out - source_in) / speed
        clip_output_start = cursor
        cursor += output_duration - transition_duration(clip)
        metadata = dict(clip.get("metadata") or {})
        timeline_path = resolve_timeline_path(clip, metadata, args.timeline_root)
        if timeline_path is None:
            continue
        metadata["timeline_final"] = str(timeline_path)
        clip["metadata"] = metadata
        timeline = load_json(timeline_path)
        timelines.add(str(timeline_path))
        source, source_items = caption_source(timeline)
        validate_timeline_identity(
            clip,
            metadata,
            timeline,
            timeline_path,
            source,
        )
        source_item_counts[source] = source_item_counts.get(source, 0) + len(
            source_items
        )
        for item in source_items:
            if not isinstance(item, dict):
                continue
            if source == REVIEWED_DIALOGUE_SOURCE and not reviewed_caption_is_usable(
                item
            ):
                continue
            text = source_text(item, source)
            language = item.get("language")
            confidence = float(item.get("confidence") or 0.0)
            if not text or language not in ALLOWED_LANGUAGES:
                continue
            # Unified dialogue captions have already passed a semantic review that
            # decides both whether speech is real and whether it is caption-ready.
            # Reapplying an acoustic STT threshold here silently drops reviewed
            # speech in windy, distant, or mixed-language scenes. Keep the
            # threshold only for legacy, unreviewed caption sources.
            if source != REVIEWED_DIALOGUE_SOURCE and confidence < args.min_confidence:
                continue
            utterance_start = float(item["start"])
            utterance_end = float(item["end"])
            utterance_duration = utterance_end - utterance_start
            overlap_start = max(source_in, utterance_start)
            overlap_end = min(source_out, utterance_end)
            overlap_duration = overlap_end - overlap_start
            if utterance_duration <= 0:
                continue
            if source == REVIEWED_DIALOGUE_SOURCE:
                if overlap_duration <= 0:
                    continue
                if (
                    utterance_start < source_in - REVIEWED_BOUNDARY_TOLERANCE_SECONDS
                    or utterance_end > source_out + REVIEWED_BOUNDARY_TOLERANCE_SECONDS
                ):
                    caption_id = item.get("caption_id") or "<missing caption_id>"
                    raise ValueError(
                        f"Reviewed caption {caption_id} is only partially included "
                        f"by clip {clip.get('id', '<missing clip id>')}"
                    )
            elif overlap_duration < 0.6:
                continue
            if (
                source != REVIEWED_DIALOGUE_SOURCE
                and overlap_duration / utterance_duration < args.min_coverage
            ):
                continue
            output_start = clip_output_start + (overlap_start - source_in) / speed
            output_end = clip_output_start + (overlap_end - source_in) / speed
            if any(
                overlaps(
                    output_start, output_end, float(item["start"]), float(item["end"])
                )
                for item in retained
            ):
                continue
            accepted_source_item_counts[source] = (
                accepted_source_item_counts.get(source, 0) + 1
            )
            pieces = split_text(text, args.max_chars)
            total_chars = sum(max(1, len(piece)) for piece in pieces)
            piece_cursor = output_start
            kind = source_kind(item, source)
            identifiers = source_id_fields(item)
            for piece_index, piece in enumerate(pieces):
                if piece_index == len(pieces) - 1:
                    piece_end = output_end
                else:
                    share = max(1, len(piece)) / total_chars
                    piece_end = piece_cursor + (output_end - output_start) * share
                caption = {
                    "start": round(piece_cursor, 3),
                    "end": round(piece_end, 3),
                    "text": piece,
                    "kind": kind,
                    "language": language,
                    "source_stage": source,
                    "source_timeline": str(timeline_path),
                    **identifiers,
                }
                if source == REVIEWED_DIALOGUE_SOURCE:
                    caption["review_status"] = str(item["review_status"])
                generated.append(caption)
                piece_cursor = piece_end
    plan["captions"] = sorted(
        retained + generated, key=lambda item: (item["start"], item["end"])
    )
    provenance = dict(plan.get("provenance") or {})
    provenance["caption_generation"] = {
        "method": (
            "reviewed dialogue captions (preferred), dialogue script, or reconciled "
            "transcript overlap mapped from source to output timeline"
        ),
        "source_timelines": sorted(timelines),
        "source_precedence": [
            REVIEWED_DIALOGUE_SOURCE,
            DIALOGUE_SCRIPT_SOURCE,
            RECONCILED_TRANSCRIPT_SOURCE,
        ],
        "accepted_review_statuses": sorted(REVIEWED_CAPTION_STATUSES),
        "confidence_policy": (
            "reviewed_dialogue captions bypass the machine-confidence threshold; "
            "the threshold applies only to legacy dialogue-script and reconciled-"
            "transcript sources"
        ),
        "source_item_counts": source_item_counts,
        "accepted_source_item_counts": accepted_source_item_counts,
        "min_confidence": args.min_confidence,
        "min_coverage": args.min_coverage,
        "max_chars": args.max_chars,
        "existing_caption_count": len(retained),
        "replaced_existing_caption_count": (
            len(existing_captions) if args.replace_existing else 0
        ),
        "generated_caption_count": len(generated),
    }
    if args.timeline_root is not None:
        provenance["caption_generation"]["timeline_root"] = str(args.timeline_root)
    plan["provenance"] = provenance
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "clips": len(clips),
                "existing_captions": len(retained),
                "generated_captions": len(generated),
                "total_captions": len(plan["captions"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
