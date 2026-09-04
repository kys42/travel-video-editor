from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from .phase1 import atomic_json, format_time

RECONCILIATION_PACKET_SCHEMA = "transcript-reconciliation-packet/v1"
RECONCILIATION_REVIEW_SCHEMA = "transcript-reconciliation-review/v1"
RECONCILED_TRANSCRIPT_SCHEMA = "reconciled-transcript/v1"


def _load(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    return json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))


def _compact_text(parts: list[str]) -> str:
    text = " ".join(part.strip() for part in parts if part.strip())
    text = re.sub(r"\s+([,.!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_activity(
    start: float,
    end: float,
    max_window: float,
    boundaries: list[float],
    atomic_intervals: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    protected: list[list[float]] = []
    for interval_start, interval_end in sorted(atomic_intervals):
        if interval_end <= interval_start:
            continue
        if protected and interval_start < protected[-1][1] - 0.001:
            protected[-1][1] = max(protected[-1][1], interval_end)
        else:
            protected.append([interval_start, interval_end])

    def cuts_evidence(value: float) -> bool:
        return any(
            interval_start + 0.001 < value < interval_end - 0.001
            for interval_start, interval_end in protected
        )

    windows: list[tuple[float, float]] = []
    cursor = start
    while end - cursor > max_window:
        target = cursor + max_window
        usable = [
            boundary
            for boundary in boundaries
            if cursor + max_window * 0.5 <= boundary <= cursor + max_window * 1.5
            and end - boundary >= 1.0
            and not cuts_evidence(boundary)
        ]
        if usable:
            boundary = min(usable, key=lambda value: abs(value - target))
        else:
            covering = [
                interval_end
                for interval_start, interval_end in protected
                if interval_start < target < interval_end
            ]
            boundary = max(covering) if covering else target
        if boundary >= end or end - boundary < 1.0:
            break
        windows.append((cursor, boundary))
        cursor = boundary
    if end - cursor >= 0.05:
        windows.append((cursor, end))
    return windows


def _evidence_interval(
    candidate: dict[str, Any], span: dict[str, Any] | None
) -> tuple[float, float, str]:
    if (
        span is not None
        and span.get("start") is not None
        and span.get("end") is not None
        and float(span["end"]) > float(span["start"])
    ):
        return float(span["start"]), float(span["end"]), "span"
    return (
        float(candidate["start"]),
        float(candidate["end"]),
        "candidate_interval_fallback",
    )


def _locale_fragment(
    candidates: list[dict[str, Any]],
    locale: str,
    start: float,
    end: float,
) -> dict[str, Any] | None:
    parts: list[str] = []
    confidences: list[float] = []
    source_ids: list[str] = []
    evidence_spans: list[dict[str, Any]] = []
    for candidate in candidates:
        if candidate["requested_locale"] != locale:
            continue
        if float(candidate["start"]) >= end or float(candidate["end"]) <= start:
            continue
        spans = candidate.get("spans", [])
        selected_spans = []
        for span_index, span in enumerate(spans):
            span_start, span_end, timing_source = _evidence_interval(candidate, span)
            if span_start < end and span_end > start:
                selected_spans.append(
                    (span_index, span, span_start, span_end, timing_source)
                )
        if selected_spans:
            parts.extend(
                str(span.get("text", ""))
                for _, span, span_start, span_end, _ in selected_spans
                if start <= (span_start + span_end) / 2 < end
            )
            confidences.extend(
                float(span["confidence"])
                for _, span, _, _, _ in selected_spans
                if span.get("confidence") is not None
            )
            source_ids.append(str(candidate["utterance_id"]))
            evidence_spans.extend(
                {
                    "source_candidate_id": str(candidate["utterance_id"]),
                    "source_span_index": span_index,
                    "source_start": round(span_start, 3),
                    "source_end": round(span_end, 3),
                    "start": round(max(start, span_start), 3),
                    "end": round(min(end, span_end), 3),
                    "text": (
                        str(span.get("text", ""))
                        if start <= (span_start + span_end) / 2 < end
                        else ""
                    ),
                    "text_owner": start <= (span_start + span_end) / 2 < end,
                    "timing_source": timing_source,
                    "confidence": float(span["confidence"])
                    if span.get("confidence") is not None
                    else None,
                }
                for span_index, span, span_start, span_end, timing_source in selected_spans
            )
        elif (
            not spans
            and float(candidate["start"]) < end
            and float(candidate["end"]) > start
        ):
            candidate_start, candidate_end, timing_source = _evidence_interval(
                candidate, None
            )
            owns_text = start <= (candidate_start + candidate_end) / 2 < end
            if owns_text:
                parts.append(str(candidate.get("text", "")))
            if candidate.get("mean_confidence") is not None:
                confidences.append(float(candidate["mean_confidence"]))
            source_ids.append(str(candidate["utterance_id"]))
            evidence_spans.append(
                {
                    "source_candidate_id": str(candidate["utterance_id"]),
                    "source_span_index": None,
                    "source_start": round(candidate_start, 3),
                    "source_end": round(candidate_end, 3),
                    "start": round(max(start, candidate_start), 3),
                    "end": round(min(end, candidate_end), 3),
                    "text": str(candidate.get("text", "")) if owns_text else "",
                    "text_owner": owns_text,
                    "timing_source": timing_source,
                    "confidence": candidate.get("mean_confidence"),
                }
            )
    text = _compact_text(parts)
    if not text and not source_ids:
        return None
    return {
        "locale": locale,
        "text": text,
        "mean_confidence": round(sum(confidences) / len(confidences), 4)
        if confidences
        else None,
        "source_candidate_ids": list(dict.fromkeys(source_ids)),
        "evidence_spans": evidence_spans,
    }


def _covers_interval(
    source_start: float,
    source_end: float,
    fragments: list[tuple[float, float]],
    *,
    tolerance: float = 0.002,
) -> bool:
    """Return whether overlapping window fragments cover a source interval."""
    cursor = source_start
    for start, end in sorted(fragments):
        clipped_start = max(source_start, start)
        clipped_end = min(source_end, end)
        if clipped_end <= clipped_start:
            continue
        if clipped_start > cursor + tolerance:
            return False
        cursor = max(cursor, clipped_end)
    return cursor >= source_end - tolerance


def _mlx_hints(
    mlx: dict[str, Any] | None,
    start: float,
    end: float,
) -> list[dict[str, Any]]:
    if mlx is None:
        return []
    hints: list[dict[str, Any]] = []
    for chunk in mlx.get("chunks", []):
        if float(chunk["start"]) >= end or float(chunk["end"]) <= start:
            continue
        hints.append(
            {
                "chunk_id": chunk["chunk_id"],
                "start": chunk["start"],
                "end": chunk["end"],
                "selected_language": chunk.get("selected_language"),
                "top_probability": chunk.get("top_probability"),
                "margin": chunk.get("margin"),
                "uncertain": chunk.get("uncertain"),
                "resolved_language": chunk.get("resolution", {}).get("spoken_language"),
            }
        )
    return hints


def _scene_context(
    timeline: dict[str, Any] | None,
    start: float,
    end: float,
) -> list[dict[str, Any]]:
    if timeline is None:
        return []
    contexts: list[dict[str, Any]] = []
    for group in timeline.get("context_groups", []):
        if float(group["start"]) >= end or float(group["end"]) <= start:
            continue
        review = group.get("context_review", {})
        contexts.append(
            {
                "group_id": group["group_id"],
                "label": group.get("label"),
                "narrative_summary": review.get("narrative_summary"),
                "dialogue_summary": review.get("dialogue_summary"),
            }
        )
    return contexts


def _machine_language_hint(
    candidate_map: dict[str, dict[str, Any]],
    mlx_hints: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(candidate_map) == 1:
        locale = next(iter(candidate_map))
        return {
            "language": "ko" if locale.lower().startswith("ko") else "en",
            "confidence": "weak",
            "reason": "only_one_apple_locale_produced_text",
        }
    confident_mlx = [
        hint
        for hint in mlx_hints
        if not hint.get("uncertain")
        and float(hint.get("top_probability") or 0) >= 0.8
        and float(hint.get("margin") or 0) >= 0.3
    ]
    languages = {str(hint.get("selected_language")) for hint in confident_mlx}
    if len(languages) == 1:
        return {
            "language": languages.pop(),
            "confidence": "medium",
            "reason": "strong_mlx_audio_language_hint",
        }
    return {
        "language": "uncertain",
        "confidence": "none",
        "reason": "dual_candidates_require_contextual_reconciliation",
    }


def _candidate_activity_intervals(
    apple: dict[str, Any], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rebuild activity coverage from raw candidates, not a stale packet."""
    ranges: list[tuple[float, float]] = []
    for activity in apple.get("activity_intervals", []):
        ranges.append((float(activity["start"]), float(activity["end"])))
    for candidate in candidates:
        ranges.append((float(candidate["start"]), float(candidate["end"])))
        ranges.extend(
            (float(span["start"]), float(span["end"]))
            for span in candidate.get("spans", [])
            if span.get("start") is not None and span.get("end") is not None
        )
    valid = sorted((start, end) for start, end in ranges if start >= 0 and end > start)
    merged: list[list[float]] = []
    for start, end in valid:
        if not merged or start > merged[-1][1] + 0.35:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [
        {
            "activity_id": f"FRESH-ACTIVITY-{index:04d}",
            "start": round(start, 3),
            "end": round(end, 3),
        }
        for index, (start, end) in enumerate(merged, start=1)
    ]


def create_reconciliation_packet(
    apple_transcript_path: Path,
    *,
    mlx_normalized_path: Path | None = None,
    timeline_path: Path | None = None,
    max_window: float = 6.0,
) -> dict[str, Any]:
    if max_window <= 0:
        raise ValueError("max_window must be positive")
    apple_transcript_path = apple_transcript_path.expanduser().resolve()
    apple = _load(apple_transcript_path)
    mlx = _load(mlx_normalized_path)
    timeline = _load(timeline_path)
    assert apple is not None
    if apple.get("schema_version") != "apple-stt/v1":
        raise ValueError(
            "Fresh reconciliation requires transcript.apple.json (apple-stt/v1); "
            "a prior reconciliation or review artifact is not accepted"
        )
    candidates = apple.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("Apple transcript candidates must be a list")
    candidate_ids = [str(candidate.get("utterance_id", "")) for candidate in candidates]
    if any(not item for item in candidate_ids) or len(candidate_ids) != len(
        set(candidate_ids)
    ):
        raise ValueError("Apple candidate IDs must be present and unique")
    locales = list(
        dict.fromkeys(
            [str(item) for item in apple.get("locales", [])]
            + [str(candidate.get("requested_locale", "")) for candidate in candidates]
        )
    )
    if any(not str(candidate.get("requested_locale", "")) for candidate in candidates):
        raise ValueError(
            "Apple transcript must contain candidates with requested locales"
        )
    for candidate in candidates:
        if not str(candidate.get("text", "")).strip() and not candidate.get("spans"):
            continue
        if float(candidate.get("start", -1)) < 0 or float(
            candidate.get("end", -1)
        ) <= float(candidate.get("start", -1)):
            raise ValueError(
                f"Apple candidate has an invalid interval: {candidate['utterance_id']}"
            )
    lexical_candidates = [
        candidate
        for candidate in candidates
        if str(candidate.get("text", "")).strip() or candidate.get("spans")
    ]
    ignored_empty_candidate_ids = [
        str(candidate["utterance_id"])
        for candidate in candidates
        if not str(candidate.get("text", "")).strip() and not candidate.get("spans")
    ]
    activities = _candidate_activity_intervals(apple, lexical_candidates)
    atomic_evidence_intervals = [
        _evidence_interval(candidate, span)[0:2]
        for candidate in lexical_candidates
        for span in (candidate.get("spans") or [None])
    ]
    windows: list[dict[str, Any]] = []
    for activity in activities:
        activity_start = float(activity["start"])
        activity_end = float(activity["end"])
        boundaries = sorted(
            {
                value
                for candidate in lexical_candidates
                for value in (
                    float(candidate["start"]),
                    float(candidate["end"]),
                    *(
                        coordinate
                        for span in candidate.get("spans", [])
                        if span.get("start") is not None and span.get("end") is not None
                        for coordinate in (float(span["start"]), float(span["end"]))
                    ),
                )
                if activity_start < value < activity_end
            }
        )
        for start, end in _split_activity(
            activity_start,
            activity_end,
            max_window,
            boundaries,
            [
                (span_start, span_end)
                for span_start, span_end in atomic_evidence_intervals
                if span_start < activity_end and span_end > activity_start
            ],
        ):
            candidate_map = {
                locale: fragment
                for locale in locales
                if (
                    fragment := _locale_fragment(lexical_candidates, locale, start, end)
                )
                is not None
            }
            if not candidate_map:
                continue
            mlx_hints = _mlx_hints(mlx, start, end)
            windows.append(
                {
                    "window_id": f"RW{len(windows) + 1:04d}",
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "timecode": f"{format_time(start)}-{format_time(end)}",
                    "activity_id": activity["activity_id"],
                    "apple_candidates": candidate_map,
                    "mlx_language_hints": mlx_hints,
                    "scene_context": _scene_context(timeline, start, end),
                    "machine_language_hint": _machine_language_hint(
                        candidate_map, mlx_hints
                    ),
                }
            )

    expected_evidence: dict[tuple[str, int | None], tuple[float, float, str]] = {}
    for candidate in lexical_candidates:
        spans = candidate.get("spans", [])
        if spans:
            for span_index, span in enumerate(spans):
                source_start, source_end, _ = _evidence_interval(candidate, span)
                expected_evidence[(str(candidate["utterance_id"]), span_index)] = (
                    source_start,
                    source_end,
                    str(span.get("text", "")).strip(),
                )
        else:
            expected_evidence[(str(candidate["utterance_id"]), None)] = (
                float(candidate["start"]),
                float(candidate["end"]),
                str(candidate.get("text", "")).strip(),
            )
    included_evidence = {
        (str(span["source_candidate_id"]), span.get("source_span_index"))
        for window in windows
        for candidate in window["apple_candidates"].values()
        for span in candidate["evidence_spans"]
    }
    missing_evidence = set(expected_evidence) - included_evidence
    evidence_fragments: dict[tuple[str, int | None], list[tuple[float, float]]] = {}
    for window in windows:
        for candidate in window["apple_candidates"].values():
            for span in candidate["evidence_spans"]:
                key = (
                    str(span["source_candidate_id"]),
                    span.get("source_span_index"),
                )
                evidence_fragments.setdefault(key, []).append(
                    (float(span["start"]), float(span["end"]))
                )
    incomplete_evidence = {
        key
        for key, (source_start, source_end, _) in expected_evidence.items()
        if not _covers_interval(
            source_start,
            source_end,
            evidence_fragments.get(key, []),
        )
    }
    evidence_text_owner_counts: dict[tuple[str, int | None], int] = {}
    for window in windows:
        for candidate in window["apple_candidates"].values():
            for span in candidate["evidence_spans"]:
                if str(span.get("text", "")).strip():
                    key = (
                        str(span["source_candidate_id"]),
                        span.get("source_span_index"),
                    )
                    evidence_text_owner_counts[key] = (
                        evidence_text_owner_counts.get(key, 0) + 1
                    )
    invalid_text_ownership = {
        key
        for key, (_, _, text) in expected_evidence.items()
        if text and evidence_text_owner_counts.get(key, 0) != 1
    }
    included_candidate_ids = {
        str(candidate_id)
        for window in windows
        for candidate in window["apple_candidates"].values()
        for candidate_id in candidate["source_candidate_ids"]
    }
    lexical_candidate_ids = {
        str(candidate["utterance_id"]) for candidate in lexical_candidates
    }
    missing_candidate_ids = lexical_candidate_ids - included_candidate_ids
    if (
        missing_candidate_ids
        or missing_evidence
        or incomplete_evidence
        or invalid_text_ownership
    ):
        details: list[str] = []
        if missing_candidate_ids:
            details.append("candidate IDs: " + ", ".join(sorted(missing_candidate_ids)))
        if missing_evidence:
            details.append(f"evidence spans: {len(missing_evidence)}")
        if incomplete_evidence:
            details.append(
                f"incompletely covered evidence spans: {len(incomplete_evidence)}"
            )
        if invalid_text_ownership:
            details.append(
                f"evidence spans without exactly one text owner: {len(invalid_text_ownership)}"
            )
        raise ValueError(
            "Fresh reconciliation packet lost Apple evidence ("
            + "; ".join(details)
            + ")"
        )

    return {
        "schema_version": RECONCILIATION_PACKET_SCHEMA,
        "source": apple.get("source"),
        "inputs": {
            "apple_transcript": str(apple_transcript_path),
            "apple_raw": apple.get("paths", {}).get("raw", {}),
            "mlx_normalized": str(mlx_normalized_path.expanduser().resolve())
            if mlx_normalized_path is not None
            else None,
            "timeline": str(timeline_path.expanduser().resolve())
            if timeline_path is not None
            else None,
        },
        "detector": apple.get("detector"),
        "policy": {
            "max_window_seconds": max_window,
            "max_window_is_soft_to_preserve_atomic_evidence": True,
            "activity_source": "fresh_union_of_detector_activity_and_all_apple_candidates",
            "preserve_original_language": True,
            "translations_are_separate_fields": True,
            "machine_language_hints_are_non_authoritative": True,
            "all_apple_candidates_and_evidence_are_preserved": True,
            "evidence_text_is_owned_by_exactly_one_window": True,
        },
        "instructions": [
            "Use Apple candidates as evidence, not as ground truth.",
            "Choose the actual spoken language per utterance; split mixed turns.",
            "Keep original_text in the spoken language and script.",
            "Put translations only in translations; never replace original_text.",
            "Use uncertain when evidence is insufficient and explain why.",
        ],
        "summary": {
            "window_count": len(windows),
            "apple_candidate_count": len(candidates),
            "lexical_candidate_count": len(lexical_candidates),
            "ignored_empty_candidate_count": len(ignored_empty_candidate_ids),
            "ignored_empty_candidate_ids": ignored_empty_candidate_ids,
            "activity_interval_count": len(apple.get("activity_intervals", [])),
            "source_activity_interval_count": len(apple.get("activity_intervals", [])),
            "fresh_activity_interval_count": len(activities),
            "apple_evidence_span_count": len(expected_evidence),
            "included_candidate_count": len(included_candidate_ids),
            "included_evidence_span_count": len(included_evidence),
            "missing_candidate_ids": [],
            "missing_evidence_span_count": 0,
            "incompletely_covered_evidence_span_count": 0,
            "invalid_evidence_text_owner_count": 0,
        },
        "windows": windows,
    }


def build_reconciliation_packet(
    apple_transcript_path: Path,
    output_path: Path,
    *,
    mlx_normalized_path: Path | None = None,
    timeline_path: Path | None = None,
    max_window: float = 6.0,
) -> Path:
    output_path = output_path.expanduser().resolve()
    packet = create_reconciliation_packet(
        apple_transcript_path,
        mlx_normalized_path=mlx_normalized_path,
        timeline_path=timeline_path,
        max_window=max_window,
    )
    atomic_json(output_path, packet)
    return output_path


def validate_reconciliation_review(
    packet: dict[str, Any], review: dict[str, Any]
) -> None:
    if review.get("schema_version") != RECONCILIATION_REVIEW_SCHEMA:
        raise ValueError("Unsupported transcript reconciliation review schema")
    expected_ids = [window["window_id"] for window in packet.get("windows", [])]
    reviewed_ids = [window["window_id"] for window in review.get("windows", [])]
    if reviewed_ids != expected_ids:
        raise ValueError("Review windows must exactly match packet order and coverage")
    packet_by_id = {window["window_id"]: window for window in packet["windows"]}
    valid_languages = {"ko", "en", "mixed", "uncertain", "non_speech"}
    for reviewed_window in review["windows"]:
        source_window = packet_by_id[reviewed_window["window_id"]]
        allowed_ids = {
            candidate_id
            for candidate in source_window["apple_candidates"].values()
            for candidate_id in candidate["source_candidate_ids"]
        }
        for utterance in reviewed_window.get("utterances", []):
            if utterance.get("language") not in valid_languages:
                raise ValueError(f"Invalid language in {reviewed_window['window_id']}")
            start = float(utterance["start"])
            end = float(utterance["end"])
            if (
                start < float(source_window["start"]) - 0.001
                or end > float(source_window["end"]) + 0.001
                or end <= start
            ):
                raise ValueError(
                    f"Utterance outside source window {reviewed_window['window_id']}"
                )
            source_ids = set(utterance.get("source_candidate_ids", []))
            if not source_ids or not source_ids <= allowed_ids:
                raise ValueError(
                    f"Invalid source candidate IDs in {reviewed_window['window_id']}"
                )
            if (
                utterance.get("language") not in {"uncertain", "non_speech"}
                and not str(utterance.get("original_text", "")).strip()
            ):
                raise ValueError(
                    f"Missing original text in {reviewed_window['window_id']}"
                )


def merge_reconciliation(
    packet_path: Path,
    review_path: Path,
    output_path: Path,
) -> Path:
    packet = _load(packet_path)
    review = _load(review_path)
    assert packet is not None and review is not None
    validate_reconciliation_review(packet, review)
    utterances: list[dict[str, Any]] = []
    for reviewed_window in review["windows"]:
        for utterance in reviewed_window.get("utterances", []):
            utterances.append(
                {
                    **utterance,
                    "utterance_id": f"U{len(utterances) + 1:04d}",
                    "window_id": reviewed_window["window_id"],
                    "start_timecode": format_time(float(utterance["start"])),
                    "end_timecode": format_time(float(utterance["end"])),
                }
            )
    payload = {
        "schema_version": RECONCILED_TRANSCRIPT_SCHEMA,
        "source": packet.get("source"),
        "inputs": {
            **packet.get("inputs", {}),
            "reconciliation_packet": str(packet_path.expanduser().resolve()),
            "reconciliation_review": str(review_path.expanduser().resolve()),
        },
        "policy": packet.get("policy"),
        "summary": {
            "window_count": len(packet.get("windows", [])),
            "utterance_count": len(utterances),
            "language_counts": {
                language: sum(item["language"] == language for item in utterances)
                for language in sorted({item["language"] for item in utterances})
            },
        },
        "utterances": utterances,
    }
    atomic_json(output_path.expanduser().resolve(), payload)
    return output_path.expanduser().resolve()


def render_reconciliation_html(
    packet_path: Path,
    output_path: Path,
    *,
    review_path: Path | None = None,
) -> Path:
    packet = _load(packet_path)
    review = _load(review_path)
    assert packet is not None
    reviewed = {item["window_id"]: item for item in (review or {}).get("windows", [])}
    cards: list[str] = []
    for window in packet["windows"]:
        candidates = "".join(
            "<section class='candidate'>"
            f"<div class='candidate-head'>{html.escape(locale)}"
            f" <span>{candidate.get('mean_confidence', '—')}</span></div>"
            f"<p>{html.escape(candidate['text'])}</p>"
            "</section>"
            for locale, candidate in window["apple_candidates"].items()
        )
        scene = (
            " / ".join(
                str(item.get("label")) for item in window.get("scene_context", [])
            )
            or "장면 맥락 없음"
        )
        mlx = (
            ", ".join(
                f"{hint.get('selected_language')} {hint.get('top_probability')}"
                for hint in window.get("mlx_language_hints", [])
            )
            or "—"
        )
        reviewed_window = reviewed.get(window["window_id"])
        resolved_items = (reviewed_window or {}).get("utterances", [])
        if resolved_items:
            resolved = "".join(
                "<div class='utterance'>"
                f"<strong>{html.escape(str(item['language']))}</strong> "
                f"{html.escape(str(item.get('original_text', '')))}"
                f"<small>KO: {html.escape(str(item.get('translations', {}).get('ko', '—')))}"
                f" · EN: {html.escape(str(item.get('translations', {}).get('en', '—')))}</small>"
                "</div>"
                for item in resolved_items
            )
        elif reviewed_window is not None:
            resolved = "<div class='pending'>판독 불가 — 원시 후보만 보존</div>"
        else:
            resolved = "<div class='pending'>아직 종합되지 않음</div>"
        cards.append(
            "<article>"
            f"<header><span>{window['window_id']}</span>"
            f"<time>{html.escape(window['timecode'])}</time></header>"
            f"<div class='context'>{html.escape(scene)} · MLX: {html.escape(mlx)}</div>"
            f"<div class='candidates'>{candidates}</div>"
            f"<section class='resolved'><h3>종합 원문 / 별도 번역</h3>{resolved}</section>"
            "</article>"
        )
    document = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>음식 주문 영상 · 대화 종합</title>
<style>
:root{{--bg:#0d1015;--panel:#161b22;--line:#2a3340;--muted:#93a1b2;--text:#ecf2f8;--accent:#f4b860}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{width:min(1380px,calc(100% - 56px));margin:36px auto 80px}} h1{{font-size:30px;margin:0 0 8px}} .lede{{color:var(--muted);margin:0 0 28px}}
.summary{{display:flex;gap:24px;padding:16px 20px;border:1px solid var(--line);border-radius:12px;margin-bottom:20px;background:#11161d}}
article{{border-top:1px solid var(--line);padding:22px 0}} article header{{display:flex;align-items:baseline;gap:14px}} article header span{{color:var(--accent);font-weight:750}} time{{font-variant-numeric:tabular-nums;font-size:18px}}
.context{{color:var(--muted);font-size:13px;margin:5px 0 12px}} .candidates{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.candidate,.resolved{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}} .candidate-head{{font-weight:700;color:#bdd7ff}} .candidate-head span{{float:right;color:var(--muted);font-weight:500}} .candidate p{{margin:8px 0 0;font-size:16px}}
.resolved{{margin-top:12px;border-color:#55462f}} .resolved h3{{font-size:12px;color:var(--accent);letter-spacing:.06em;text-transform:uppercase;margin:0 0 8px}}
.utterance{{padding:7px 0;border-top:1px solid #252c36}} .utterance:first-of-type{{border:0}} .utterance small{{display:block;color:var(--muted);margin-left:25px}} .pending{{color:var(--muted)}}
@media(max-width:760px){{main{{width:min(100% - 24px,1380px)}}.candidates{{grid-template-columns:1fr}}.summary{{display:block}}}}
</style></head><body><main><h1>음식 주문 영상 · 대화 종합</h1>
<p class="lede">Apple Detector 게이팅 → 한국어/영어 원시 후보 → MLX 언어 힌트 → 원문 보존 종합 → 번역 분리</p>
<div class="summary"><span>대조 창 <strong>{len(packet["windows"])}</strong></span><span>원시 후보 <strong>{packet["summary"]["apple_candidate_count"]}</strong></span><span>Detector <strong>{html.escape(str(packet.get("detector", {}).get("sensitivity", "—")))}</strong></span></div>
{"".join(cards)}</main></body></html>"""
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document, encoding="utf-8")
    return output_path
