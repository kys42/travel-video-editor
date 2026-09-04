from __future__ import annotations

import base64
import html
import mimetypes
import os
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .context import select_storyboard_samples
from .phase1 import atomic_json, format_time
from .review import load_json

WEB_SCHEMA = "phase1-web-timeline/v1"
NOTABLE_LABELS = {
    "candid": "솔직한 순간",
    "dialogue": "살릴 대사",
    "unexpected": "돌발 순간",
    "visual": "그림 되는 컷",
    "travel_detail": "여행 디테일",
}


class ImageAssetResolver:
    def __init__(self, output_dir: Path, mode: str) -> None:
        if mode not in {"embed", "relative"}:
            raise ValueError(f"Unsupported asset mode: {mode}")
        self.output_dir = output_dir.resolve()
        self.mode = mode
        self._cache: dict[Path, str] = {}
        self.embedded_bytes = 0

    def url(self, path: str | Path) -> str:
        source = Path(path).resolve()
        if self.mode == "relative":
            relative = os.path.relpath(source, self.output_dir)
            return quote(relative.replace(os.sep, "/"), safe="/.:@-_")
        if source not in self._cache:
            if not source.is_file():
                raise FileNotFoundError(f"Timeline image is unavailable: {source}")
            payload = source.read_bytes()
            mime = mimetypes.guess_type(source.name)[0] or "image/jpeg"
            encoded = base64.b64encode(payload).decode("ascii")
            self._cache[source] = f"data:{mime};base64,{encoded}"
            self.embedded_bytes += len(payload)
        return self._cache[source]

    @property
    def unique_count(self) -> int:
        return len(self._cache)


def _escape(value: Any) -> str:
    return html.escape(str(value))


def _short_time(seconds: float) -> str:
    return format_time(seconds).split(".", maxsplit=1)[0]


def _duration_text(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


def _group_samples(
    timeline: dict[str, Any],
    start: float,
    end: float,
    *,
    frame_limit: int,
) -> list[dict[str, Any]]:
    samples = [
        sample for sample in timeline["samples"] if start <= float(sample["time"]) < end
    ]
    if len(samples) <= frame_limit:
        return samples
    return select_storyboard_samples(samples, start, end, max_frames=frame_limit)


def _group_segments(
    timeline: dict[str, Any], group: dict[str, Any]
) -> list[dict[str, Any]]:
    segment_map = {segment["segment_id"]: segment for segment in timeline["segments"]}
    return [segment_map[segment_id] for segment_id in group["segment_ids"]]


def _transcript_candidates(
    segments: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    languages = {
        language
        for segment in segments
        for language in segment.get("transcript_candidates", {})
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for language in sorted(languages):
        seen: set[tuple[float, float, str]] = set()
        items: list[dict[str, Any]] = []
        for segment in segments:
            for item in segment.get("transcript_candidates", {}).get(language, []):
                key = (
                    float(item["start"]),
                    float(item["end"]),
                    str(item["text"]),
                )
                if key not in seen:
                    seen.add(key)
                    items.append(item)
        result[language] = sorted(
            items, key=lambda item: (float(item["start"]), float(item["end"]))
        )
    return result


def _transcript_quality(item: dict[str, Any]) -> str:
    score = float(item.get("avg_logprob", -2.0))
    if score >= -0.5:
        return "high"
    if score >= -0.85:
        return "medium"
    return "low"


def _reconciled_quality(item: dict[str, Any]) -> str:
    score = float(item.get("confidence", 0.0))
    if score >= 0.8:
        return "high"
    if score >= 0.65:
        return "medium"
    return "low"


def _render_frame_strip(
    samples: list[dict[str, Any]],
    context: dict[str, Any],
    assets: ImageAssetResolver,
    *,
    seek_enabled: bool,
) -> str:
    key_moments = {
        item["sample_id"]: item.get("role", "핵심 순간")
        for item in context.get("key_moments", [])
    }
    notable_moments = {
        item["sample_id"]: item.get("title", "특이 포인트")
        for item in context.get("notable_moments", [])
    }
    representative = context["representative_sample_id"]
    tiles: list[str] = []
    frame_action = "프레임부터 재생" if seek_enabled else "프레임"
    disabled = "" if seek_enabled else "disabled"
    for sample in samples:
        sample_id = sample["sample_id"]
        role = key_moments.get(sample_id)
        classes = ["frame-tile"]
        if role:
            classes.append("is-key")
        if sample_id == representative:
            classes.append("is-representative")
        badges = []
        if sample_id == representative:
            badges.append('<span class="frame-badge frame-badge--rep">대표</span>')
        if role:
            badges.append(f'<span class="frame-badge">{_escape(role)}</span>')
        if sample_id in notable_moments:
            badges.append(
                f'<span class="frame-badge frame-badge--notable" title="{_escape(notable_moments[sample_id])}">특이 포인트</span>'
            )
        tiles.append(
            f"""
            <button class="{" ".join(classes)}" type="button"
                    data-seek="{float(sample["time"]):.3f}"
                    aria-label="{_escape(sample["timecode"])} {frame_action}"
                    {disabled}>
              <span class="frame-image-wrap">
                <img loading="lazy" src="{assets.url(sample["frame"])}"
                     alt="{_escape(sample_id)} · {_escape(sample["timecode"])}">
                <span class="frame-badges">{"".join(badges)}</span>
                <span class="frame-play" aria-hidden="true">▶</span>
              </span>
              <span class="frame-caption">
                <strong>{_escape(sample["timecode"])}</strong>
                <span>{_escape(sample_id)}</span>
              </span>
            </button>
            """
        )
    return "".join(tiles)


def _render_notable_moments(
    timeline: dict[str, Any],
    context: dict[str, Any],
    assets: ImageAssetResolver,
    *,
    seek_enabled: bool,
) -> str:
    moments = context.get("notable_moments", [])
    if not moments:
        return ""
    sample_map = {sample["sample_id"]: sample for sample in timeline["samples"]}
    disabled = "" if seek_enabled else "disabled"
    cards: list[str] = []
    for moment in moments:
        sample = sample_map[moment["sample_id"]]
        category = str(moment.get("category", "travel_detail"))
        category_label = NOTABLE_LABELS.get(category, "특이 포인트")
        cards.append(
            f"""
            <article class="notable-card notable-card--{_escape(category)}">
              <button class="notable-frame" type="button" data-seek="{float(sample["time"]):.3f}" {disabled}>
                <img loading="lazy" src="{assets.url(sample["frame"])}"
                     alt="{_escape(moment["title"])}">
                <span>{_escape(sample["timecode"])}</span>
              </button>
              <div class="notable-copy">
                <span>{_escape(category_label)}</span>
                <h4>{_escape(moment["title"])}</h4>
                <p>{_escape(moment["description"])}</p>
                <small><b>EDIT USE</b>{_escape(moment["edit_hint"])}</small>
              </div>
            </article>
            """
        )
    return f"""
        <section class="detail-section notable-section">
          <div class="section-heading">
            <div><span class="eyebrow">NOTABLE BEATS</span><h3>특이 포인트</h3></div>
            <p>평범한 요약에서 빠지기 쉬운 표정·돌발 상황·대사·여행 디테일입니다.</p>
          </div>
          <div class="notable-grid">{"".join(cards)}</div>
        </section>
    """


def _render_video_synopsis(timeline: dict[str, Any]) -> str:
    summary = timeline.get("video_summary")
    if not summary:
        return ""
    group_map = {group["group_id"]: group for group in timeline["context_groups"]}
    highlights = set(summary.get("highlight_group_ids", []))
    events = "".join(
        f"""
        <li class="video-event{" is-highlight" if event["group_id"] in highlights else ""}">
          <span>{_escape(_short_time(float(group_map[event["group_id"]]["start"])))}</span>
          <strong>{_escape(event["headline"])}</strong>
        </li>
        """
        for event in summary["chronological_events"]
    )
    tags = "".join(f"<span>{_escape(tag)}</span>" for tag in summary.get("tags", []))
    return f"""
    <section class="video-synopsis" aria-label="영상 전체 요약">
      <div class="video-synopsis-inner">
        <div class="video-synopsis-copy">
          <span class="eyebrow">VIDEO SYNOPSIS</span>
          <h2>{_escape(summary["title"])}</h2>
          <strong>{_escape(summary["one_line_summary"])}</strong>
          <p>{_escape(summary["narrative_summary"])}</p>
          <div class="video-tags">{tags}</div>
        </div>
        <div class="video-event-run">
          <span>WHAT HAPPENED / 시간순</span>
          <ol>{events}</ol>
        </div>
      </div>
    </section>
    """


def _render_transcripts(
    transcripts: dict[str, list[dict[str, Any]]],
    evidence: list[str],
    *,
    seek_enabled: bool,
) -> str:
    labels = {"ko": "한국어 후보", "en": "영어 후보"}
    columns: list[str] = []
    disabled = "" if seek_enabled else "disabled"
    for language, items in transcripts.items():
        primary = language in evidence
        snippets: list[str] = []
        for item in items:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            snippets.append(
                f"""
                <button class="transcript-line" type="button" data-seek="{float(item["start"]):.3f}"
                        {disabled}>
                  <span class="transcript-time">{_escape(_short_time(float(item["start"])))}</span>
                  <span class="transcript-text">{_escape(text)}</span>
                  <span class="signal signal--{_transcript_quality(item)}" title="STT 상대 신뢰도"></span>
                </button>
                """
            )
        empty = '<p class="empty-copy">이 구간에서 필터를 통과한 후보가 없습니다.</p>'
        columns.append(
            f"""
            <section class="transcript-column{" is-evidence" if primary else ""}">
              <div class="transcript-heading">
                <h4>{_escape(labels.get(language, language.upper()))}</h4>
                <span>{"종합에 사용" if primary else "비교 후보"}</span>
              </div>
              <div class="transcript-list">{"".join(snippets) if snippets else empty}</div>
            </section>
            """
        )
    if not columns:
        return '<p class="empty-copy">STT 자료가 없습니다.</p>'
    return "".join(columns)


def _render_reconciled_transcripts(
    utterances: list[dict[str, Any]], *, seek_enabled: bool
) -> str:
    disabled = "" if seek_enabled else "disabled"
    snippets: list[str] = []
    for item in utterances:
        text = str(
            item.get("display_text") or item.get("original_text", "")
        ).strip()
        if not text:
            continue
        translations = " · ".join(
            f"{str(language).upper()}: {str(value)}"
            for language, value in item.get("translations", {}).items()
            if str(value).strip()
        )
        translated = (
            f'<small class="transcript-translation">{_escape(translations)}</small>'
            if translations
            else ""
        )
        snippets.append(
            f"""
            <button class="transcript-line" type="button" data-seek="{float(item.get("source_start", item["start"])):.3f}"
                    {disabled}>
              <span class="transcript-time">{_escape(_short_time(float(item.get("source_start", item["start"]))))}</span>
              <span class="transcript-text"><b>{_escape(str(item.get("language", "—")).upper())}</b> {_escape(text)}{translated}</span>
              <span class="signal signal--{_reconciled_quality(item)}" title="종합 대본 신뢰도"></span>
            </button>
            """
        )
    return "".join(snippets) or '<p class="empty-copy">종합된 발화가 없습니다.</p>'


def _silence_ratio(
    segment: dict[str, Any], silence_intervals: list[dict[str, float]]
) -> float:
    start = float(segment["start"])
    end = float(segment["end"])
    duration = max(0.001, end - start)
    overlap = sum(
        max(0.0, min(end, float(item["end"])) - max(start, float(item["start"])))
        for item in silence_intervals
    )
    return min(1.0, overlap / duration)


def _segment_transcript_lines(
    segment: dict[str, Any], evidence_languages: list[str]
) -> list[tuple[str, str]]:
    reconciled = (
        segment.get("caption_lines", [])
        if "caption_lines" in segment
        else segment.get("dialogue_lines")
        or segment.get("reviewed_utterances")
        or segment.get("reconciled_utterances", [])
    )
    if reconciled:
        return [
            (
                str(item.get("language", "—")),
                str(item.get("display_text") or item.get("original_text", "")).strip(),
            )
            for item in reconciled
            if str(item.get("display_text") or item.get("original_text", "")).strip()
        ]
    candidates = segment.get("transcript_candidates", {})
    ordered_languages = evidence_languages + [
        language
        for language in sorted(candidates)
        if language not in evidence_languages
    ]
    result: list[tuple[str, str]] = []
    for language in ordered_languages:
        seen: set[str] = set()
        texts: list[str] = []
        for item in candidates.get(language, []):
            text = str(item.get("text", "")).strip()
            if text and text not in seen:
                seen.add(text)
                texts.append(text)
        if texts:
            compact = " ".join(texts)
            if len(compact) > 150:
                compact = compact[:147].rstrip() + "…"
            result.append((language, compact))
    return result


def _edit_recommendation(
    segment: dict[str, Any], *, overlaps_key_moment: bool
) -> tuple[str, str, str]:
    review = segment.get("review", {})
    importance = float(review.get("importance", 0.5))
    scene_type = str(review.get("scene_type", ""))
    flags = set(segment.get("machine", {}).get("flags", []))
    if overlaps_key_moment:
        return "keep", "우선 유지", "코덱스가 고른 핵심 순간과 겹칩니다."
    if (
        importance <= 0.25
        or scene_type == "camera_handling"
        or flags & {"very_dark", "very_blurry"}
    ):
        return "trim", "축약·제외", "카메라 조작이나 품질 저하 가능성이 큽니다."
    if importance >= 0.72:
        return "keep", "우선 유지", "행동이나 대상이 선명한 주요 구간입니다."
    if scene_type == "transition" or importance < 0.5:
        return "bridge", "짧게 연결", "맥락 연결용으로 길이를 줄여 사용할 수 있습니다."
    return "review", "선별 사용", "앞뒤 흐름과 대화를 확인해 길이를 조절합니다."


def _render_edit_map(
    timeline: dict[str, Any],
    segments: list[dict[str, Any]],
    context: dict[str, Any],
    assets: ImageAssetResolver,
    *,
    seek_enabled: bool,
) -> str:
    sample_times = {
        sample["sample_id"]: float(sample["time"]) for sample in timeline["samples"]
    }
    key_times = [
        sample_times[item["sample_id"]]
        for item in context.get("key_moments", [])
        if item.get("sample_id") in sample_times
    ]
    evidence_languages = [str(item) for item in context.get("dialogue_evidence", [])]
    notable_moments = context.get("notable_moments", [])
    silence_intervals = timeline.get("audio_analysis", {}).get("silence_intervals", [])
    disabled = "" if seek_enabled else "disabled"
    rows: list[str] = []
    for segment in segments:
        start = float(segment["start"])
        end = float(segment["end"])
        representative_frame = segment.get("representative_frame")
        if not representative_frame:
            midpoint = (start + end) / 2
            representative_frame = min(
                timeline["samples"],
                key=lambda sample: abs(float(sample["time"]) - midpoint),
            )["frame"]
        review = segment.get("review", {})
        transcript_lines = _segment_transcript_lines(segment, evidence_languages)
        silence = _silence_ratio(segment, silence_intervals)
        if "caption_lines" in segment:
            audio_state = (
                "검수 자막 대본"
                if segment.get("caption_lines")
                else "검수 완료 · 대사 없음"
            )
        elif segment.get("dialogue_lines"):
            audio_state = "정리 대본"
        elif segment.get("reconciled_utterances"):
            audio_state = "종합 원문 대본"
        elif transcript_lines:
            audio_state = "음성 후보 감지"
        elif silence >= 0.5:
            audio_state = "무음·저음량"
        else:
            audio_state = "현장음 / 불명확"
        audio_lines = (
            "".join(
                f"<p><b>{_escape(language.upper())}</b><span>{_escape(text)}</span></p>"
                for language, text in transcript_lines
            )
            or '<p class="no-dialogue">유효한 STT 후보 없음</p>'
        )
        overlaps_key = any(start <= time < end for time in key_times)
        status_class, status_label, status_reason = _edit_recommendation(
            segment, overlaps_key_moment=overlaps_key
        )
        segment_notables = [
            item
            for item in notable_moments
            if item.get("sample_id") in sample_times
            and start <= sample_times[item["sample_id"]] < end
        ]
        notable_note = "".join(
            f'<div class="row-notable"><b>특이 포인트</b><span>{_escape(item["title"])}</span></div>'
            for item in segment_notables
        )
        actions = "".join(
            f"<span>{_escape(action)}</span>" for action in review.get("actions", [])
        )
        quality = round(float(segment.get("machine", {}).get("quality", 0.0)) * 100)
        flags = segment.get("machine", {}).get("flags", [])
        quality_note = (
            " · ".join(str(flag) for flag in flags) if flags else "품질 경고 없음"
        )
        rows.append(
            f"""
            <article class="edit-row edit-row--{status_class}">
              <button class="edit-time" type="button" data-seek="{start:.3f}" {disabled}>
                <strong>{_escape(_short_time(start))}</strong>
                <span>— {_escape(_short_time(end))}</span>
                <small>{_escape(_duration_text(end - start))}</small>
              </button>
              <div class="edit-frame">
                <img loading="lazy" src="{assets.url(representative_frame)}"
                     alt="{_escape(segment["segment_id"])} 대표 프레임">
                <span>{_escape(segment["segment_id"])}</span>
              </div>
              <div class="edit-visual">
                <span class="edit-cell-label">화면 · 행동</span>
                <strong>{_escape(review.get("visual_summary", "설명 없음"))}</strong>
                {notable_note}
                <div class="action-tags">{actions}</div>
                <small>화질 {quality}% · {_escape(quality_note)}</small>
              </div>
              <div class="edit-audio">
                <span class="edit-cell-label">음성 · 대화</span>
                <strong>{_escape(audio_state)}</strong>
                <div class="edit-transcripts">{audio_lines}</div>
              </div>
              <div class="edit-decision">
                <span class="edit-status">{_escape(status_label)}</span>
                <p>{_escape(status_reason)}</p>
                <small>중요도 {round(float(review.get("importance", 0.5)) * 100)}%</small>
              </div>
            </article>
            """
        )
    return "".join(rows)


def _render_group(
    timeline: dict[str, Any],
    group: dict[str, Any],
    index: int,
    assets: ImageAssetResolver,
    media_url: str | None,
    *,
    frame_limit: int,
) -> str:
    context = group["context_review"]
    notable_moments = context.get("notable_moments", [])
    start = float(group["start"])
    end = float(group["end"])
    samples = _group_samples(timeline, start, end, frame_limit=frame_limit)
    segments = _group_segments(timeline, group)
    transcripts = _transcript_candidates(segments)
    reviewed_dialogue = group.get("reviewed_dialogue") or {}
    has_reviewed_captions = (
        isinstance(reviewed_dialogue, dict) and "captions" in reviewed_dialogue
    )
    reviewed_captions = (
        reviewed_dialogue.get("captions", [])
        if isinstance(reviewed_dialogue, dict)
        else []
    )
    reconciled = (
        reviewed_captions
        if has_reviewed_captions
        else group.get("dialogue_lines") or group.get("reconciled_utterances", [])
    )
    dialogue_summary = (
        " ".join(
            str(item.get("display_text", "")).strip()
            for item in reviewed_captions
            if str(item.get("display_text", "")).strip()
        )
        or "검수된 대사 없음"
        if has_reviewed_captions
        else str(context.get("dialogue_summary", "확인 가능한 대화가 없습니다."))
    )
    representative = next(
        sample
        for sample in timeline["samples"]
        if sample["sample_id"] == context["representative_sample_id"]
    )
    duration = end - start
    confidence = float(context.get("confidence", 0.0))
    frame_help = (
        "프레임을 누르면 해당 시점부터 영상이 재생됩니다."
        if media_url
        else "현재는 정적 프레임만 표시합니다. 프록시 연결 후 같은 타임코드로 재생됩니다."
    )
    search_parts = [
        str(group.get("label", "")),
        str(context.get("narrative_summary", "")),
        dialogue_summary,
    ]
    for moment in notable_moments:
        search_parts.extend(
            [
                str(moment.get("title", "")),
                str(moment.get("description", "")),
                str(moment.get("edit_hint", "")),
            ]
        )
    for segment in segments:
        review = segment.get("review", {})
        search_parts.append(str(review.get("visual_summary", "")))
        search_parts.extend(str(action) for action in review.get("actions", []))
        for items in segment.get("transcript_candidates", {}).values():
            search_parts.extend(str(item.get("text", "")) for item in items)
    for utterance in reconciled:
        search_parts.append(
            str(utterance.get("display_text") or utterance.get("original_text", ""))
        )
        search_parts.extend(
            str(value) for value in utterance.get("translations", {}).values()
        )
    summary_search = " ".join(search_parts).lower()
    representative_url = assets.url(representative["frame"])
    play_disabled = "" if media_url else "disabled"
    play_label = "이 구간 재생" if media_url else "프록시 연결 후 재생"
    notable_badge = (
        f'<span class="notable-count">★ {len(notable_moments)} 특이 포인트</span>'
        if notable_moments
        else ""
    )
    if reconciled:
        evidence_note = "원문 언어 보존 · 번역 별도"
        if reviewed_captions:
            transcript_label = "검수 자막 대본"
            transcript_heading = "화면 표시 대사"
            transcript_unit = "자막 줄"
        elif group.get("dialogue_lines"):
            transcript_label = "정리 대본"
            transcript_heading = "정리 대본"
            transcript_unit = "대사 블록"
        else:
            transcript_label = "종합 대본"
            transcript_heading = "종합 원문"
            transcript_unit = "발화"
        evidence_drawer = f"""
          <details class="evidence-drawer" open>
            <summary>{transcript_label}과 타임코드 보기 <span>Apple 후보 + 장면 맥락 검토</span></summary>
            <div class="transcript-grid">
              <section class="transcript-column is-evidence">
                <div class="transcript-heading"><h4>{transcript_heading}</h4><span>{len(reconciled)}개 {transcript_unit}</span></div>
                <div class="transcript-list">{_render_reconciled_transcripts(reconciled, seek_enabled=media_url is not None)}</div>
              </section>
              {_render_transcripts(transcripts, [], seek_enabled=media_url is not None)}
            </div>
          </details>
        """
    else:
        evidence_note = (
            "검수 완료 · 대사 없음"
            if has_reviewed_captions
            else "화면 맥락 + 이중 STT 종합"
        )
        evidence_drawer = f"""
          <details class="evidence-drawer">
            <summary>STT 원문 후보와 타임코드 보기 <span>검증 전 참고 신호</span></summary>
            <div class="transcript-grid">
              {_render_transcripts(transcripts, context.get("dialogue_evidence", []), seek_enabled=media_url is not None)}
            </div>
          </details>
        """
    return f"""
    <details class="scene" id="scene-{_escape(group["group_id"])}"
             data-search="{_escape(summary_search)}"
             data-preview-title="{_escape(group["label"])}"
             data-preview-time="{_escape(_short_time(start))} — {_escape(_short_time(end))}"
             data-preview-poster="{representative_url}"
             {"open" if index == 1 else ""}>
      <summary class="scene-summary">
        <span class="scene-index" aria-hidden="true">{index:02d}</span>
        <span class="scene-time-block">
          <strong>{_escape(_short_time(start))}</strong>
          <span>{_escape(_duration_text(duration))}</span>
        </span>
        <span class="scene-thumb">
          <img src="{representative_url}"
               alt="{_escape(group["label"])} 대표 프레임">
          <span class="thumb-time">{_escape(representative["timecode"])}</span>
        </span>
        <span class="scene-copy">
          <span class="scene-kicker">SCENE {_escape(group["group_id"])}</span>
          <strong class="scene-title">{_escape(group["label"])}</strong>
          <span class="scene-description">{_escape(context["narrative_summary"])}</span>
        </span>
        <span class="scene-action">
          <span class="scene-signals">{notable_badge}<span class="confidence">{round(confidence * 100)}% 맥락 확신</span></span>
          <span class="detail-label"><span>자세히 보기</span><i aria-hidden="true"></i></span>
        </span>
      </summary>

      <div class="scene-detail">
        <section class="scene-overview">
          <div class="scene-brief">
            <span class="eyebrow">WHAT HAPPENED</span>
            <h3>이 장면에서 일어난 일</h3>
            <p>{_escape(context["narrative_summary"])}</p>
          </div>
          <dl class="scene-facts">
            <div><dt>구간</dt><dd>{_escape(_short_time(start))} — {_escape(_short_time(end))}</dd></div>
            <div><dt>길이</dt><dd>{_escape(_duration_text(duration))}</dd></div>
            <div><dt>대표 장면</dt><dd>{_escape(context["representative_sample_id"])}</dd></div>
            <div><dt>분석 구간</dt><dd>{len(segments)}개</dd></div>
          </dl>
          <div class="scene-quick-action">
            <button class="queue-scene" type="button" data-start="{start:.3f}" data-end="{end:.3f}"
                    data-title="{_escape(group["label"])}" {play_disabled}>
              <span aria-hidden="true">▶</span> {_escape(play_label)}
            </button>
            <p>{_escape(context.get("representative_reason", ""))}</p>
          </div>
        </section>

        {_render_notable_moments(timeline, context, assets, seek_enabled=media_url is not None)}

        <section class="detail-section edit-map-section">
          <div class="section-heading">
            <div><span class="eyebrow">EDIT DECISION MAP</span><h3>편집 판단 타임라인</h3></div>
            <span class="decision-note">화면·행동·음성·품질을 같은 시간축에서 비교 · 자동 컷 확정 아님</span>
          </div>
          <div class="edit-map">
            {_render_edit_map(timeline, segments, context, assets, seek_enabled=media_url is not None)}
          </div>
        </section>

        <section class="detail-section visual-sequence">
          <div class="section-heading">
            <div><span class="eyebrow">VISUAL SEQUENCE</span><h3>장면 흐름</h3></div>
            <p>{_escape(frame_help)} 최대 {frame_limit}장까지 표시합니다.</p>
          </div>
          <div class="frame-strip">{_render_frame_strip(samples, context, assets, seek_enabled=media_url is not None)}</div>
        </section>

        <section class="detail-section dialogue-section">
          <div class="section-heading">
            <div><span class="eyebrow">DIALOGUE</span><h3>오간 대화</h3></div>
            <span class="evidence-note">{_escape(evidence_note)}</span>
          </div>
          <blockquote>{_escape(dialogue_summary)}</blockquote>
          {evidence_drawer}
        </section>

      </div>
    </details>
    """


def render_timeline_web(
    timeline_path: Path,
    output_dir: Path,
    *,
    frame_limit: int = 16,
    asset_mode: str = "embed",
) -> Path:
    if frame_limit < 4:
        raise ValueError("frame limit must be at least 4")
    timeline = load_json(timeline_path)
    if timeline.get("schema_version") not in {
        "phase1-context-reviewed-timeline/v1",
        "phase1-video-summarized-timeline/v1",
    }:
        raise ValueError("render-web requires a context-reviewed timeline")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    assets = ImageAssetResolver(output_dir, asset_mode)

    media_url = None

    groups = timeline["context_groups"]
    if not groups:
        raise ValueError("render-web requires at least one context group")
    duration = float(timeline["media"]["duration"])
    source_name = str(timeline["source"]["name"])
    cards = "".join(
        _render_group(
            timeline,
            group,
            index,
            assets,
            media_url,
            frame_limit=frame_limit,
        )
        for index, group in enumerate(groups, start=1)
    )
    navigation = "".join(
        f'<a href="#scene-{_escape(group["group_id"])}"><span>{index:02d}</span>{_escape(group["label"])}</a>'
        for index, group in enumerate(groups, start=1)
    )
    video = timeline["media"]["video"]
    sample_map = {sample["sample_id"]: sample for sample in timeline["samples"]}
    first_group = groups[0]
    first_context = first_group["context_review"]
    first_sample = sample_map[first_context["representative_sample_id"]]
    template = (
        files("travel_video.templates").joinpath("timeline.html").read_text("utf-8")
    )
    replacements = {
        "__DOCUMENT_TITLE__": _escape(f"{source_name} · Scene Index"),
        "__SOURCE_NAME__": _escape(source_name),
        "__DURATION__": _escape(_duration_text(duration)),
        "__SCENE_COUNT__": str(len(groups)),
        "__SAMPLE_COUNT__": str(len(timeline["samples"])),
        "__RESOLUTION__": _escape(f"{video['width']}×{video['height']}"),
        "__CODEC__": _escape(str(video.get("codec", "—")).upper()),
        "__NAVIGATION__": navigation,
        "__SCENE_CARDS__": cards,
        "__ORIGINAL_URL__": "#",
        "__ORIGINAL_STATE__": "is-disabled",
        "__MEDIA_STATUS__": "미디어 미연결",
        "__PREVIEW_POSTER__": assets.url(first_sample["frame"]),
        "__PREVIEW_TITLE__": _escape(first_group["label"]),
        "__PREVIEW_TIMECODE__": _escape(
            f"{_short_time(float(first_group['start']))} — {_short_time(float(first_group['end']))}"
        ),
        "__VIDEO_SYNOPSIS__": _render_video_synopsis(timeline),
    }
    document = template
    for token, value in replacements.items():
        document = document.replace(token, value)
    output_path = output_dir / "index.html"
    output_path.write_text(document, encoding="utf-8")
    manifest = {
        "schema_version": WEB_SCHEMA,
        "timeline": str(timeline_path.resolve()),
        "output": str(output_path),
        "video_mode": "none",
        "proxy": None,
        "asset_mode": asset_mode,
        "embedded_asset_count": assets.unique_count,
        "embedded_source_bytes": assets.embedded_bytes,
        "frame_limit": frame_limit,
        "scene_count": len(groups),
        "notable_moment_count": sum(
            len(group["context_review"].get("notable_moments", [])) for group in groups
        ),
        "has_video_summary": "video_summary" in timeline,
        "has_reconciled_transcript": (
            "reviewed_dialogue" in timeline or "reconciled_transcript" in timeline
        ),
        "has_dialogue_script": "dialogue_script" in timeline,
    }
    atomic_json(output_dir / "manifest.json", manifest)
    return output_path
