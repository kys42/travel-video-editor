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
) -> list[tuple[float, float]]:
    windows: list[tuple[float, float]] = []
    cursor = start
    while end - cursor > max_window:
        target = cursor + max_window
        usable = [
            boundary
            for boundary in boundaries
            if cursor + max_window * 0.5 <= boundary <= cursor + max_window * 1.5
            and end - boundary >= 1.0
        ]
        boundary = min(usable, key=lambda value: abs(value - target)) if usable else target
        windows.append((cursor, boundary))
        cursor = boundary
    if end - cursor >= 0.05:
        windows.append((cursor, end))
    return windows


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
        selected_spans = [
            span
            for span in spans
            if span.get("start") is not None
            and span.get("end") is not None
            and start <= (float(span["start"]) + float(span["end"])) / 2 < end
        ]
        if selected_spans:
            parts.extend(str(span.get("text", "")) for span in selected_spans)
            confidences.extend(
                float(span["confidence"])
                for span in selected_spans
                if span.get("confidence") is not None
            )
            source_ids.append(str(candidate["utterance_id"]))
            evidence_spans.extend(
                {
                    "source_candidate_id": str(candidate["utterance_id"]),
                    "start": round(float(span["start"]), 3),
                    "end": round(float(span["end"]), 3),
                    "text": str(span.get("text", "")),
                    "confidence": float(span["confidence"])
                    if span.get("confidence") is not None
                    else None,
                }
                for span in selected_spans
            )
        elif not spans:
            parts.append(str(candidate.get("text", "")))
            if candidate.get("mean_confidence") is not None:
                confidences.append(float(candidate["mean_confidence"]))
            source_ids.append(str(candidate["utterance_id"]))
            evidence_spans.append(
                {
                    "source_candidate_id": str(candidate["utterance_id"]),
                    "start": round(float(candidate["start"]), 3),
                    "end": round(float(candidate["end"]), 3),
                    "text": str(candidate.get("text", "")),
                    "confidence": candidate.get("mean_confidence"),
                }
            )
    text = _compact_text(parts)
    if not text:
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
                "resolved_language": chunk.get("resolution", {}).get(
                    "spoken_language"
                ),
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


def build_reconciliation_packet(
    apple_transcript_path: Path,
    output_path: Path,
    *,
    mlx_normalized_path: Path | None = None,
    timeline_path: Path | None = None,
    max_window: float = 6.0,
) -> Path:
    if max_window <= 0:
        raise ValueError("max_window must be positive")
    apple_transcript_path = apple_transcript_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    apple = _load(apple_transcript_path)
    mlx = _load(mlx_normalized_path)
    timeline = _load(timeline_path)
    assert apple is not None
    candidates = apple.get("candidates", [])
    locales = list(apple.get("locales", []))
    windows: list[dict[str, Any]] = []
    for activity in apple.get("activity_intervals", []):
        activity_start = float(activity["start"])
        activity_end = float(activity["end"])
        boundaries = sorted(
            {
                float(candidate[key])
                for candidate in candidates
                for key in ("start", "end")
                if activity_start < float(candidate[key]) < activity_end
            }
        )
        for start, end in _split_activity(
            activity_start,
            activity_end,
            max_window,
            boundaries,
        ):
            candidate_map = {
                locale: fragment
                for locale in locales
                if (
                    fragment := _locale_fragment(
                        candidates, locale, start, end
                    )
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
    packet = {
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
            "preserve_original_language": True,
            "translations_are_separate_fields": True,
            "machine_language_hints_are_non_authoritative": True,
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
            "activity_interval_count": len(apple.get("activity_intervals", [])),
        },
        "windows": windows,
    }
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
                raise ValueError(
                    f"Invalid language in {reviewed_window['window_id']}"
                )
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
            if utterance.get("language") not in {"uncertain", "non_speech"} and not str(
                utterance.get("original_text", "")
            ).strip():
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
    reviewed = {
        item["window_id"]: item for item in (review or {}).get("windows", [])
    }
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
        scene = " / ".join(
            str(item.get("label")) for item in window.get("scene_context", [])
        ) or "장면 맥락 없음"
        mlx = ", ".join(
            f"{hint.get('selected_language')} {hint.get('top_probability')}"
            for hint in window.get("mlx_language_hints", [])
        ) or "—"
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
<div class="summary"><span>대조 창 <strong>{len(packet['windows'])}</strong></span><span>원시 후보 <strong>{packet['summary']['apple_candidate_count']}</strong></span><span>Detector <strong>{html.escape(str(packet.get('detector', {}).get('sensitivity', '—')))}</strong></span></div>
{''.join(cards)}</main></body></html>"""
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(document, encoding="utf-8")
    return output_path
