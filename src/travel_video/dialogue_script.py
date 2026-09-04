from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .phase1 import atomic_json, format_time
from .review import load_json


DIALOGUE_SCRIPT_SCHEMA = "dialogue-script/v1"
SUPPORTED_TIMELINE_SCHEMAS = {
    "phase1-context-reviewed-timeline/v1",
    "phase1-video-summarized-timeline/v1",
}
SENTENCE_ENDINGS = (".", "?", "!", "。", "？", "！")


def _text(item: dict[str, Any]) -> str:
    return " ".join(str(item.get("original_text", "")).split())


def _lexical_utterances(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    transcript = timeline.get("reconciled_transcript") or {}
    utterances = transcript.get("utterances") or []
    result = []
    for item in utterances:
        language = str(item.get("language", "")).strip().lower()
        if _text(item) and language != "non_speech":
            result.append(copy.deepcopy(item))
    return result


def _merge_allowed(
    current: list[dict[str, Any]],
    following: dict[str, Any],
    *,
    max_gap: float,
    max_duration: float,
    max_chars: int,
) -> bool:
    previous = current[-1]
    previous_language = str(previous.get("language", "")).lower()
    following_language = str(following.get("language", "")).lower()
    if previous_language != following_language:
        return False
    if previous_language in {"uncertain", "mixed", ""}:
        return False
    gap = float(following["start"]) - float(previous["end"])
    if gap < -0.05 or gap > max_gap:
        return False
    duration = float(following["end"]) - float(current[0]["start"])
    if duration > max_duration:
        return False
    combined_chars = sum(len(_text(item)) for item in current) + len(_text(following))
    if combined_chars + len(current) > max_chars:
        return False
    if _text(previous).endswith(SENTENCE_ENDINGS) and gap > 0.55:
        return False
    return True


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _make_line(index: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    start = float(items[0]["start"])
    end = float(items[-1]["end"])
    durations = [max(0.001, float(item["end"]) - float(item["start"])) for item in items]
    confidence = sum(
        float(item.get("confidence", 0.0)) * duration
        for item, duration in zip(items, durations, strict=True)
    ) / sum(durations)
    translation_languages = _unique(
        [language for item in items for language in item.get("translations", {})]
    )
    translations = {
        language: " ".join(
            str(item.get("translations", {}).get(language, "")).strip()
            for item in items
            if str(item.get("translations", {}).get(language, "")).strip()
        )
        for language in translation_languages
    }
    return {
        "line_id": f"DL{index:04d}",
        "start": start,
        "end": end,
        "start_timecode": format_time(start),
        "end_timecode": format_time(end),
        "language": str(items[0].get("language", "uncertain")),
        "speaker": "unknown",
        "original_text": " ".join(_text(item) for item in items),
        "translations": translations,
        "confidence": round(confidence, 4),
        "review_status": "machine_grouped",
        "source_utterance_ids": _unique(
            [str(item.get("utterance_id", "")) for item in items]
        ),
        "source_window_ids": _unique(
            [str(item.get("window_id", "")) for item in items]
        ),
        "source_candidate_ids": _unique(
            [
                str(candidate_id)
                for item in items
                for candidate_id in item.get("source_candidate_ids", [])
            ]
        ),
    }


def build_dialogue_lines(
    timeline: dict[str, Any],
    *,
    max_gap: float = 2.2,
    max_duration: float = 20.0,
    max_chars: int = 160,
) -> list[dict[str, Any]]:
    if timeline.get("schema_version") not in SUPPORTED_TIMELINE_SCHEMAS:
        raise ValueError("Dialogue script requires a reviewed or summarized timeline")
    utterances = _lexical_utterances(timeline)
    groups: list[list[dict[str, Any]]] = []
    for utterance in utterances:
        if groups and _merge_allowed(
            groups[-1],
            utterance,
            max_gap=max_gap,
            max_duration=max_duration,
            max_chars=max_chars,
        ):
            groups[-1].append(utterance)
        else:
            groups.append([utterance])
    return [_make_line(index, items) for index, items in enumerate(groups, start=1)]


def _overlaps(item: dict[str, Any], start: float, end: float) -> bool:
    return float(item["start"]) < end and float(item["end"]) > start


def _attached_line(
    line: dict[str, Any], interval_start: float, interval_end: float
) -> dict[str, Any]:
    start = float(line["start"])
    end = float(line["end"])
    return {
        **copy.deepcopy(line),
        "source_start": start,
        "source_end": end,
        "overlap_start": max(start, interval_start),
        "overlap_end": min(end, interval_end),
    }


def attach_dialogue_script(
    timeline_path: Path,
    output_path: Path,
    *,
    max_gap: float = 2.2,
    max_duration: float = 20.0,
    max_chars: int = 160,
) -> Path:
    timeline_path = timeline_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if output_path == timeline_path:
        raise ValueError("Dialogue script output must be a new file")
    timeline = load_json(timeline_path)
    if not timeline.get("reconciled_transcript"):
        raise ValueError("Dialogue script requires an attached reconciled transcript")
    lines = build_dialogue_lines(
        timeline,
        max_gap=max_gap,
        max_duration=max_duration,
        max_chars=max_chars,
    )
    result = copy.deepcopy(timeline)
    for segment in result.get("segments", []):
        segment["dialogue_lines"] = [
            _attached_line(line, float(segment["start"]), float(segment["end"]))
            for line in lines
            if _overlaps(line, float(segment["start"]), float(segment["end"]))
        ]
    for group in result.get("context_groups", []):
        group["dialogue_lines"] = [
            _attached_line(line, float(group["start"]), float(group["end"]))
            for line in lines
            if _overlaps(line, float(group["start"]), float(group["end"]))
        ]
    result["dialogue_script"] = {
        "schema_version": DIALOGUE_SCRIPT_SCHEMA,
        "timeline_input": str(timeline_path),
        "method": "deterministic temporal-language grouping",
        "policy": {
            "max_gap": max_gap,
            "max_duration": max_duration,
            "max_chars": max_chars,
            "speaker_diarization": False,
            "text_rewriting": False,
        },
        "source_utterance_count": len(_lexical_utterances(timeline)),
        "line_count": len(lines),
        "lines": lines,
    }
    atomic_json(output_path, result)
    return output_path
