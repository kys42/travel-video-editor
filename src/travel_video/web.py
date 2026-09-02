from __future__ import annotations

import html
import os
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .context import select_storyboard_samples
from .phase1 import atomic_json, format_time
from .review import load_json

WEB_SCHEMA = "phase1-web-timeline/v1"


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


def _path_url(path: str | Path, output_dir: Path) -> str:
    relative = os.path.relpath(Path(path).resolve(), output_dir.resolve())
    return quote(relative.replace(os.sep, "/"), safe="/.:@-_")


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


def _render_frame_strip(
    samples: list[dict[str, Any]],
    context: dict[str, Any],
    output_dir: Path,
    *,
    seek_enabled: bool,
) -> str:
    key_moments = {
        item["sample_id"]: item.get("role", "핵심 순간")
        for item in context.get("key_moments", [])
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
        tiles.append(
            f"""
            <button class="{" ".join(classes)}" type="button"
                    data-seek="{float(sample["time"]):.3f}"
                    aria-label="{_escape(sample["timecode"])} {frame_action}"
                    {disabled}>
              <span class="frame-image-wrap">
                <img loading="lazy" src="{_path_url(sample["frame"], output_dir)}"
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


def _render_segment_notes(segments: list[dict[str, Any]], *, seek_enabled: bool) -> str:
    notes: list[str] = []
    disabled = "" if seek_enabled else "disabled"
    for segment in segments:
        review = segment.get("review", {})
        actions = " · ".join(str(item) for item in review.get("actions", []))
        notes.append(
            f"""
            <li>
              <button type="button" data-seek="{float(segment["start"]):.3f}"
                      {disabled}>
                <span class="segment-time">{_escape(_short_time(float(segment["start"])))}</span>
                <span class="segment-copy">
                  <strong>{_escape(review.get("visual_summary", "설명 없음"))}</strong>
                  <small>{_escape(actions or review.get("scene_type", ""))}</small>
                </span>
              </button>
            </li>
            """
        )
    return "".join(notes)


def _render_player(
    media_url: str | None, group: dict[str, Any], poster_url: str
) -> str:
    if not media_url:
        return """
        <div class="player-unavailable">
          <span>PROXY PREVIEW / RESERVED</span>
          <strong>영상 미리보기 영역</strong>
          <p>저화질 프록시가 준비되면 이 자리에 원본 기준 타임코드로 구간 재생을 연결합니다.</p>
        </div>
        """
    start = float(group["start"])
    end = float(group["end"])
    return f"""
    <div class="player-stage">
      <video class="scene-video" preload="metadata" playsinline
             poster="{_escape(poster_url)}" src="{_escape(media_url)}"></video>
      <div class="player-poster-copy" aria-hidden="true">
        <span>SCENE {int(str(group["group_id"]).lstrip("G") or 0):02d}</span>
        <strong>{_escape(_short_time(start))} — {_escape(_short_time(end))}</strong>
      </div>
      <div class="player-controls">
        <button type="button" class="play-scene" data-start="{start:.3f}" data-end="{end:.3f}">
          <span aria-hidden="true">▶</span> 이 장면 재생
        </button>
        <button type="button" data-nudge="-5">−5초</button>
        <button type="button" data-nudge="5">+5초</button>
        <span class="player-clock">{_escape(_short_time(start))} / {_escape(_short_time(end))}</span>
      </div>
    </div>
    """


def _render_group(
    timeline: dict[str, Any],
    group: dict[str, Any],
    index: int,
    output_dir: Path,
    media_url: str | None,
    *,
    frame_limit: int,
) -> str:
    context = group["context_review"]
    start = float(group["start"])
    end = float(group["end"])
    samples = _group_samples(timeline, start, end, frame_limit=frame_limit)
    segments = _group_segments(timeline, group)
    transcripts = _transcript_candidates(segments)
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
    summary_search = " ".join(
        [
            str(group.get("label", "")),
            str(context.get("narrative_summary", "")),
            str(context.get("dialogue_summary", "")),
        ]
    ).lower()
    return f"""
    <details class="scene" id="scene-{_escape(group["group_id"])}"
             data-search="{_escape(summary_search)}" {"open" if index == 1 else ""}>
      <summary class="scene-summary">
        <span class="scene-index" aria-hidden="true">{index:02d}</span>
        <span class="scene-time-block">
          <strong>{_escape(_short_time(start))}</strong>
          <span>{_escape(_duration_text(duration))}</span>
        </span>
        <span class="scene-thumb">
          <img src="{_path_url(representative["frame"], output_dir)}"
               alt="{_escape(group["label"])} 대표 프레임">
          <span class="thumb-time">{_escape(representative["timecode"])}</span>
        </span>
        <span class="scene-copy">
          <span class="scene-kicker">SCENE {_escape(group["group_id"])}</span>
          <strong class="scene-title">{_escape(group["label"])}</strong>
          <span class="scene-description">{_escape(context["narrative_summary"])}</span>
        </span>
        <span class="scene-action">
          <span class="confidence">{round(confidence * 100)}% 맥락 확신</span>
          <span class="detail-label"><span>자세히 보기</span><i aria-hidden="true"></i></span>
        </span>
      </summary>

      <div class="scene-detail">
        <div class="scene-detail-grid">
          <section class="media-panel" aria-label="장면 영상 플레이어">
            {_render_player(media_url, group, _path_url(representative["frame"], output_dir))}
          </section>

          <aside class="scene-brief">
            <span class="eyebrow">WHAT HAPPENED</span>
            <h3>이 장면에서 일어난 일</h3>
            <p>{_escape(context["narrative_summary"])}</p>
            <dl>
              <div><dt>구간</dt><dd>{_escape(_short_time(start))} — {_escape(_short_time(end))}</dd></div>
              <div><dt>길이</dt><dd>{_escape(_duration_text(duration))}</dd></div>
              <div><dt>대표 장면</dt><dd>{_escape(context["representative_sample_id"])}</dd></div>
            </dl>
            <p class="representative-reason">{_escape(context.get("representative_reason", ""))}</p>
          </aside>
        </div>

        <section class="detail-section visual-sequence">
          <div class="section-heading">
            <div><span class="eyebrow">VISUAL SEQUENCE</span><h3>장면 흐름</h3></div>
            <p>{_escape(frame_help)} 최대 {frame_limit}장까지 표시합니다.</p>
          </div>
          <div class="frame-strip">{_render_frame_strip(samples, context, output_dir, seek_enabled=media_url is not None)}</div>
        </section>

        <section class="detail-section dialogue-section">
          <div class="section-heading">
            <div><span class="eyebrow">DIALOGUE</span><h3>오간 대화</h3></div>
            <span class="evidence-note">화면 맥락 + 이중 STT 종합</span>
          </div>
          <blockquote>{_escape(context.get("dialogue_summary", "확인 가능한 대화가 없습니다."))}</blockquote>
          <details class="evidence-drawer">
            <summary>STT 원문 후보와 타임코드 보기 <span>검증 전 참고 신호</span></summary>
            <div class="transcript-grid">
              {_render_transcripts(transcripts, context.get("dialogue_evidence", []), seek_enabled=media_url is not None)}
            </div>
          </details>
        </section>

        <section class="detail-section segment-section">
          <div class="section-heading">
            <div><span class="eyebrow">SHOT NOTES</span><h3>세부 구간 기록</h3></div>
            <span>{len(segments)}개 분석 구간</span>
          </div>
          <ol class="segment-list">{_render_segment_notes(segments, seek_enabled=media_url is not None)}</ol>
        </section>
      </div>
    </details>
    """


def render_timeline_web(
    timeline_path: Path,
    output_dir: Path,
    *,
    frame_limit: int = 16,
) -> Path:
    if frame_limit < 4:
        raise ValueError("frame limit must be at least 4")
    timeline = load_json(timeline_path)
    if timeline.get("schema_version") != "phase1-context-reviewed-timeline/v1":
        raise ValueError("render-web requires a context-reviewed timeline")
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    media_url = None

    groups = timeline["context_groups"]
    duration = float(timeline["media"]["duration"])
    source_name = str(timeline["source"]["name"])
    cards = "".join(
        _render_group(
            timeline,
            group,
            index,
            output_dir,
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
        "frame_limit": frame_limit,
        "scene_count": len(groups),
    }
    atomic_json(output_dir / "manifest.json", manifest)
    return output_path
