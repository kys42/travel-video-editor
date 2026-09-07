from __future__ import annotations

import hashlib
import html
import json
import os
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .clip_evidence import build_candidate_library
from .phase1 import atomic_json, format_time
from .review import load_json
from .video_summary import VIDEO_SUMMARIZED_TIMELINE_SCHEMA
from .web import ImageAssetResolver

LIBRARY_SCHEMA = "phase1-video-library/v1"


def _escape(value: Any) -> str:
    return html.escape(str(value))


def _duration_text(seconds: float) -> str:
    minutes, secs = divmod(round(seconds), 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}시간 {minutes}분"
    if minutes:
        return f"{minutes}분 {secs}초"
    return f"{secs}초"


def _capture_parts(value: str | None) -> tuple[str, str]:
    if not value:
        return "시간 미상", "촬영 메타데이터 없음"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value, "원본 메타데이터"
    offset = parsed.utcoffset()
    timezone = " UTC" if offset is not None and offset.total_seconds() == 0 else ""
    return f"{parsed:%H:%M}{timezone}", f"{parsed:%Y.%m.%d} · 원본 메타데이터"


def _capture_range_labels(first: str | None, last: str | None) -> tuple[str, str]:
    first_time, first_date_detail = _capture_parts(first)
    last_time, last_date_detail = _capture_parts(last)
    first_date = first_date_detail.split(" · ", 1)[0]
    last_date = last_date_detail.split(" · ", 1)[0]
    if first_date != last_date:
        return (
            f"{first_date} — {last_date}",
            f"{first_date[5:]} {first_time} — {last_date[5:]} {last_time}",
        )
    return first_date, f"{first_time} — {last_time}"


def _detail_url(timeline_path: Path, output_dir: Path) -> str | None:
    detail = timeline_path.parent / "web" / "index.html"
    if not detail.is_file():
        return None
    relative = os.path.relpath(detail.resolve(), output_dir.resolve())
    return quote(relative.replace(os.sep, "/"), safe="/.:@-_")


def _discover_proxies(proxy_root: Path | None) -> dict[str, Path]:
    if proxy_root is None or not proxy_root.is_dir():
        return {}
    candidates: dict[str, list[Path]] = {}
    for path in proxy_root.rglob("*"):
        if (
            path.is_file()
            and path.suffix.casefold() == ".mp4"
            and not path.stem.casefold().endswith(".partial")
        ):
            candidates.setdefault(path.name.casefold(), []).append(path.resolve())
    return {name: paths[0] for name, paths in candidates.items() if len(paths) == 1}


def _link_proxy(proxy: Path, output_dir: Path, asset_id: str) -> str:
    media_dir = output_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    link = media_dir / f"{asset_id}.mp4"
    target = proxy.resolve()
    if os.path.lexists(link):
        if not link.is_symlink():
            raise FileExistsError(
                f"Refusing to replace non-symlink proxy asset: {link}"
            )
        if link.resolve(strict=False) != target:
            temporary = link.with_suffix(".next.mp4")
            if os.path.lexists(temporary):
                temporary.unlink()
            temporary.symlink_to(target)
            os.replace(temporary, link)
    else:
        link.symlink_to(target)
    return quote(f"media/{link.name}", safe="/.:@-_")


def _short_time(seconds: float) -> str:
    return format_time(seconds).split(".", 1)[0]


def _group_segments(
    timeline: dict[str, Any], group: dict[str, Any]
) -> list[dict[str, Any]]:
    segment_map = {segment["segment_id"]: segment for segment in timeline["segments"]}
    return [
        segment_map[segment_id]
        for segment_id in group.get("segment_ids", [])
        if segment_id in segment_map
    ]


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


def _segment_dialogue_items(
    segment: dict[str, Any],
) -> tuple[list[dict[str, Any]], str]:
    if "caption_lines" in segment:
        return segment.get("caption_lines", []), "display_text"
    return (
        segment.get("dialogue_lines")
        or segment.get("reviewed_utterances")
        or segment.get("reconciled_utterances", [])
    ), "original_text"


def _group_dialogue_items(group: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    reviewed = group.get("reviewed_dialogue")
    if isinstance(reviewed, dict) and "captions" in reviewed:
        return reviewed.get("captions", []), "display_text"
    return (
        group.get("dialogue_lines") or group.get("reconciled_utterances", [])
    ), "original_text"


def _render_segment_transcripts(segment: dict[str, Any]) -> str:
    reconciled, text_field = _segment_dialogue_items(segment)
    if reconciled:
        return (
            "".join(
                f"""
            <p class="stt-line stt-line--{_reconciled_quality(item)}">
              <span class="stt-language">{_escape(str(item.get("language", "—")).upper())}</span>
              <span class="stt-time">{_escape(_short_time(float(item.get("source_start", item["start"]))))}</span>
              <span>{_escape(item.get(text_field, ""))}</span>
            </p>
            """
                for item in reconciled
                if str(item.get(text_field, "")).strip()
            )
            or '<p class="no-stt">종합된 발화 없음</p>'
        )
    if "caption_lines" in segment:
        return '<p class="no-stt">검수된 대사 없음</p>'
    candidates = segment.get("transcript_candidates", {})
    lines: list[str] = []
    for language in ("ko", "en"):
        for item in candidates.get(language, []):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            lines.append(
                f"""
                <p class="stt-line stt-line--{_transcript_quality(item)}">
                  <span class="stt-language">{_escape(language)}</span>
                  <span class="stt-time">{_escape(_short_time(float(item.get("start", segment["start"]))))}</span>
                  <span>{_escape(text)}</span>
                </p>
                """
            )
    for language in sorted(set(candidates) - {"ko", "en"}):
        for item in candidates.get(language, []):
            text = str(item.get("text", "")).strip()
            if text:
                lines.append(
                    f"""
                    <p class="stt-line stt-line--{_transcript_quality(item)}">
                      <span class="stt-language">{_escape(language)}</span>
                      <span class="stt-time">{_escape(_short_time(float(item.get("start", segment["start"]))))}</span>
                      <span>{_escape(text)}</span>
                    </p>
                    """
                )
    return "".join(lines) or '<p class="no-stt">유효한 STT 후보 없음</p>'


def _render_segment_rows(
    segments: list[dict[str, Any]], assets: ImageAssetResolver
) -> str:
    if not segments:
        return '<p class="empty-detail">세부 분석 구간이 아직 없습니다.</p>'
    rows: list[str] = []
    for segment in segments:
        start = float(segment["start"])
        end = float(segment["end"])
        review = segment.get("review") or {}
        frame = segment.get("representative_frame")
        frame_html = (
            f'<img loading="lazy" src="{assets.url(frame)}" alt="{_escape(segment["segment_id"])} 대표 프레임">'
            if frame
            else '<span class="missing-frame">NO FRAME</span>'
        )
        actions = "".join(
            f"<span>{_escape(action)}</span>" for action in review.get("actions", [])
        )
        transcript_label = (
            "보정 자막"
            if "caption_lines" in segment
            else "정리 대본"
            if segment.get("dialogue_lines")
            else "종합 원문 대본"
            if segment.get("reconciled_utterances")
            else "STT 원문 후보 · 미검증"
        )
        rows.append(
            f"""
            <article class="segment-row">
              <div class="segment-time">
                <strong>{_escape(_short_time(start))}</strong>
                <span>— {_escape(_short_time(end))}</span>
                <small>{_escape(segment["segment_id"])}</small>
              </div>
              <div class="segment-frame">{frame_html}</div>
              <div class="segment-action">
                <span class="cell-label">화면 · 행동</span>
                <p>{_escape(review.get("visual_summary", "화면 설명 없음"))}</p>
                <div class="action-tags">{actions}</div>
              </div>
              <div class="segment-stt">
                <span class="cell-label">{transcript_label}</span>
                {_render_segment_transcripts(segment)}
              </div>
            </article>
            """
        )
    return "".join(rows)


def _render_storyboard(
    context: dict[str, Any],
    sample_map: dict[str, dict[str, Any]],
    assets: ImageAssetResolver,
) -> str:
    frames: list[str] = []
    representative_id = context["representative_sample_id"]
    for moment in context.get("key_moments", []):
        sample = sample_map.get(moment["sample_id"])
        if not sample:
            continue
        classes = (
            "story-frame is-representative"
            if sample["sample_id"] == representative_id
            else "story-frame"
        )
        frames.append(
            f"""
            <figure class="{classes}">
              <img loading="lazy" src="{assets.url(sample["frame"])}" alt="{_escape(moment.get("role", "핵심 장면"))}">
              <figcaption><b>{_escape(sample["timecode"])}</b><span>{_escape(moment.get("role", "핵심 장면"))}</span></figcaption>
            </figure>
            """
        )
    return (
        "".join(frames) or '<p class="empty-detail">선정된 맥락 프레임이 없습니다.</p>'
    )


def _render_notables(
    context: dict[str, Any], sample_map: dict[str, dict[str, Any]]
) -> str:
    items: list[str] = []
    for notable in context.get("notable_moments", []):
        sample = sample_map.get(notable["sample_id"], {})
        items.append(
            f"""
            <article class="notable-item">
              <span>{_escape(sample.get("timecode", "—"))}</span>
              <div><strong>{_escape(notable["title"])}</strong><p>{_escape(notable["description"])}</p><small>편집: {_escape(notable["edit_hint"])}</small></div>
            </article>
            """
        )
    return "".join(items) or '<p class="empty-detail">별도 특이 포인트 없음</p>'


def _reconciled_dialogue(group: dict[str, Any], *, limit: int | None = None) -> str:
    dialogue_items, text_field = _group_dialogue_items(group)
    items = [item for item in dialogue_items if str(item.get(text_field, "")).strip()]
    if limit is not None:
        items = items[:limit]
    return " ".join(str(item[text_field]).strip() for item in items)


def _render_group_reconciled(group: dict[str, Any]) -> str:
    items, text_field = _group_dialogue_items(group)
    lines = [
        f'<p class="stt-line stt-line--{_reconciled_quality(item)}"><span class="stt-language">{_escape(str(item.get("language", "—")).upper())}</span><span class="stt-time">{_escape(_short_time(float(item.get("source_start", item["start"]))))}</span><span>{_escape(item.get(text_field, ""))}</span></p>'
        for item in items
        if str(item.get(text_field, "")).strip()
    ]
    if not lines:
        return ""
    reviewed = group.get("reviewed_dialogue")
    label = (
        "보정 자막"
        if isinstance(reviewed, dict) and "captions" in reviewed
        else "정리 대본"
        if group.get("dialogue_lines")
        else "종합 원문"
    )
    return f'<div class="scene-reconciled"><b>{label}</b>' + "".join(lines) + "</div>"


def _render_group_source_transcript(group: dict[str, Any]) -> str:
    reviewed = group.get("reviewed_dialogue")
    if not isinstance(reviewed, dict) or "captions" not in reviewed:
        return ""
    utterances = [
        item
        for item in reviewed.get("utterances", [])
        if isinstance(item, dict) and str(item.get("original_text", "")).strip()
    ]
    if not utterances:
        return ""
    lines = "".join(
        f'<p class="stt-line stt-line--{_reconciled_quality(item)}"><span class="stt-language">{_escape(str(item.get("language", "—")).upper())}</span><span class="stt-time">{_escape(_short_time(float(item.get("source_start", item["start"]))))}</span><span>{_escape(item.get("original_text", ""))}</span></p>'
        for item in utterances
    )
    return (
        '<details class="source-transcript">'
        f"<summary><span>원문 보기</span><small>검수 전 발화 {len(utterances)}개</small></summary>"
        f'<div class="source-transcript__body">{lines}</div>'
        "</details>"
    )


_REVIEW_REASON_LABELS = {
    "editorial_evidence_missing": "상세 근거 없음",
    "event_closure_unconfirmed": "행동 마무리 확인 필요",
    "neighbor_boundary_review": "앞뒤 경계 확인 필요",
    "dialogue_open": "대사가 이어질 수 있음",
    "caption_uncertainty": "자막 불확실",
    "speech_boundary_cut": "발화 중간 경계",
}


def _render_candidates(
    candidates: list[dict], sample_map: dict, assets: ImageAssetResolver
) -> str:
    if not candidates:
        return ""
    rows = []
    for candidate in candidates:
        start, end = (candidate["recommended_range"][key] for key in ("start", "end"))
        context = candidate["context_range"]
        samples = [
            sample_map[sid]
            for sid in candidate["references"].get("representative_sample_ids", [])
            if sid in sample_map and start <= float(sample_map[sid]["time"]) < end
        ]
        thumbnail = (
            f'<img loading="lazy" src="{assets.url(samples[0]["frame"])}" alt="구간 내 대표 프레임">'
            if samples
            else '<span class="candidate-no-frame">프레임 없음</span>'
        )
        reasons = " · ".join(
            _REVIEW_REASON_LABELS.get(reason, reason)
            for reason in candidate["review_reasons"]
        )
        state = (
            "검토 필요" if candidate["readiness"] == "needs_review" else "구조화 후보"
        )
        people = (candidate.get("evidence") or {}).get("people", [])
        people_label = f"인물 관찰 {len(people)}건" if people else "인물 정보 미확인"
        rows.append(f"""
          <article class="candidate-row" data-candidate-id="{_escape(candidate["candidate_id"])}">
            <div class="candidate-frame">{thumbnail}</div>
            <div class="candidate-content"><strong>{_escape(candidate["title"])}</strong>
              <p>{_escape(candidate["summary"])}</p>
              {f'<p class="candidate-dialogue">대사 · {_escape(candidate["dialogue"])}</p>' if candidate["dialogue"] else ""}
              <small>{_escape(people_label)} · 우리 얼굴 제외 여부 미확인</small>
              <small class="candidate-state">{state}{" · " + _escape(reasons) if reasons else ""}</small>
            </div>
            <div class="candidate-controls">
              <span>{_escape(format_time(start))} — {_escape(format_time(end))} · {end - start:.1f}s</span>
              <button type="button" data-play-candidate data-source-in="{start:.3f}" data-source-out="{end:.3f}" data-range-title="{_escape(candidate["title"])}">후보 재생</button>
              <button type="button" data-play-candidate data-source-in="{context["start"]:.3f}" data-source-out="{context["end"]:.3f}" data-range-title="{_escape(candidate["title"])} · 앞뒤 맥락">앞뒤 맥락 재생</button>
            </div>
          </article>""")
    return (
        '<section class="candidate-section"><header><strong>장면별 세부 구간</strong>'
        f"<span>{len(candidates)}개 · 행동·대화 단위 편집 후보 · 재생 후 검토</span></header>"
        + "".join(rows)
        + "</section>"
    )


def _render_scene(
    timeline: dict[str, Any],
    group: dict[str, Any],
    event: dict[str, Any],
    index: int,
    assets: ImageAssetResolver,
    *,
    clip_candidates: list[dict],
    highlighted: bool,
    initially_open: bool,
) -> str:
    context = group["context_review"]
    sample_map = {sample["sample_id"]: sample for sample in timeline["samples"]}
    representative = sample_map[context["representative_sample_id"]]
    segments = _group_segments(timeline, group)
    start = float(group["start"])
    end = float(group["end"])
    confidence = float(context.get("confidence", 0.0))
    languages = context.get("dialogue_evidence", [])
    language_label = " + ".join(str(item).upper() for item in languages) or "현장음"
    reconciled_summary = _reconciled_dialogue(group, limit=2)
    reviewed = group.get("reviewed_dialogue")
    has_reviewed_dialogue = isinstance(reviewed, dict) and "captions" in reviewed
    notable_count = len(context.get("notable_moments", []))
    transcript_comparison = (
        "보정 자막을"
        if has_reviewed_dialogue
        else "정리 대본을"
        if group.get("dialogue_lines")
        else "종합 원문 대본을"
        if group.get("reconciled_utterances")
        else "원시 STT 후보를"
    )
    search_parts = [
        str(group.get("label", "")),
        str(event.get("headline", "")),
        str(event.get("description", "")),
        str(context.get("narrative_summary", "")),
        "" if has_reviewed_dialogue else str(context.get("dialogue_summary", "")),
    ]
    for candidate in clip_candidates:
        search_parts.extend(
            [candidate["title"], candidate["summary"], candidate["dialogue"]]
        )
    for segment in segments:
        review = segment.get("review") or {}
        search_parts.append(str(review.get("visual_summary", "")))
        search_parts.extend(str(item) for item in review.get("actions", []))
        if not has_reviewed_dialogue:
            for candidates in segment.get("transcript_candidates", {}).values():
                search_parts.extend(str(item.get("text", "")) for item in candidates)
    dialogue_items, text_field = _group_dialogue_items(group)
    for utterance in dialogue_items:
        search_parts.append(str(utterance.get(text_field, "")))
        search_parts.extend(
            str(value) for value in utterance.get("translations", {}).values()
        )
    for notable in context.get("notable_moments", []):
        search_parts.extend(
            str(notable.get(key, "")) for key in ("title", "description", "edit_hint")
        )
    preview_url = assets.url(representative["frame"])
    dialogue_detail_label = (
        "대화 · 보정 자막"
        if has_reviewed_dialogue
        else "대화 종합 · " + _escape(language_label)
    )
    dialogue_detail = _render_group_reconciled(group)
    if not dialogue_detail:
        empty_dialogue = (
            "보정된 대사 없음"
            if has_reviewed_dialogue
            else context.get("dialogue_summary", "확인 가능한 대화가 없습니다.")
        )
        dialogue_detail = f"<p>{_escape(empty_dialogue)}</p>"
    source_transcript = _render_group_source_transcript(group)
    return f"""
      <details class="scene" id="scene-{_escape(timeline["asset_id"])}-{_escape(group["group_id"])}"
               data-scene data-scene-id="{_escape(timeline["asset_id"])}:{_escape(group["group_id"])}" data-search="{_escape(" ".join(search_parts).lower())}"
               data-preview-title="{_escape(event.get("headline", group["label"]))}"
               data-preview-time="{_escape(_short_time(start))} — {_escape(_short_time(end))}"
               data-preview-timecode="{_escape(representative["timecode"])}"
               data-preview-start="{start:.3f}"
               data-preview-end="{end:.3f}"
               {"open" if initially_open else ""}>
        <summary class="scene-summary">
          <span class="scene-sequence">{index:02d}</span>
          <span class="scene-time"><strong>{_escape(_short_time(start))}</strong><small>+{round(end - start)}s</small></span>
          <span class="scene-thumb"><img loading="lazy" src="{preview_url}" alt="{_escape(group["label"])} 대표 프레임"><i>{_escape(representative["timecode"])}</i></span>
          <span class="scene-primary"><strong>{_escape(event.get("headline", group["label"]))}</strong><p>{_escape(context["narrative_summary"])}</p></span>
          <span class="scene-dialogue"><b>{"보정 자막" if has_reviewed_dialogue else "정리 대본" if group.get("dialogue_lines") else "종합 원문" if reconciled_summary else "대화 · " + _escape(language_label)}</b><p>{_escape(reconciled_summary or ("보정된 대사 없음" if has_reviewed_dialogue else context.get("dialogue_summary", "유효한 대화 없음")))}</p></span>
          <span class="scene-state">{'<b class="highlight-state">HIGHLIGHT</b>' if highlighted else "<b>SCENE</b>"}{f"<small>{notable_count} notable</small>" if notable_count else ""}<small>{round(confidence * 100)}%</small><i aria-hidden="true"></i></span>
        </summary>
        <div class="scene-depth">
          {_render_candidates(clip_candidates, sample_map, assets)}
          {'<details class="analysis-evidence" data-analysis-evidence><summary>분석 근거 보기 · 기계 구간과 원시 추출 정보</summary><p class="evidence-notice">아래 기계 구간은 추출용 샘플입니다. 장면별 편집 구간이 아니며 같은 장면 설명이 반복될 수 있습니다.</p>' if clip_candidates else ""}
          <div class="analysis-grid">
            <section><span class="depth-label">장면 해석</span><h3>{_escape(group["label"])}</h3><p>{_escape(context["narrative_summary"])}</p></section>
            <section><span class="depth-label depth-label--audio">{dialogue_detail_label}</span>{dialogue_detail}{source_transcript}</section>
            <section><span class="depth-label depth-label--edit">특이 포인트 / 편집 가치</span>{_render_notables(context, sample_map)}</section>
          </div>
          <section class="segment-section">
            <header><div><strong>{"기계 샘플 구간 · 편집 장면 아님" if clip_candidates else "세부 구간"}</strong><span>행동 해석과 {transcript_comparison} 같은 시간축으로 비교</span></div><span>{len(segments)} segments</span></header>
            <div class="segment-list">{_render_segment_rows(segments, assets)}</div>
          </section>
          <section class="storyboard-section">
            <header><div><strong>맥락 프레임</strong><span>{_escape(context.get("representative_reason", ""))}</span></div><span>{len(context.get("key_moments", []))} frames</span></header>
            <div class="story-strip">{_render_storyboard(context, sample_map, assets)}</div>
          </section>
          {"</details>" if clip_candidates else ""}
        </div>
      </details>
    """


def _render_video(
    timeline: dict[str, Any],
    timeline_path: Path,
    index: int,
    output_dir: Path,
    assets: ImageAssetResolver,
    proxy_url: str | None,
) -> tuple[str, str]:
    summary = timeline["video_summary"]
    sample_map = {sample["sample_id"]: sample for sample in timeline["samples"]}
    group_map = {group["group_id"]: group for group in timeline["context_groups"]}
    representative = sample_map[summary["representative_sample_id"]]
    capture_time, capture_date = _capture_parts(timeline["media"].get("creation_time"))
    highlight_ids = set(summary.get("highlight_group_ids", []))
    has_beats = timeline.get("reviewed_dialogue", {}).get("editorial_beats") or any(
        group.get("editorial_beats") for group in timeline["context_groups"]
    )
    source = timeline.get("source", {})
    has_candidate_identity = bool(
        source.get("path") and source.get("quick_fingerprint")
    )
    candidates = (
        build_candidate_library(timeline)["candidates"]
        if has_beats and has_candidate_identity
        else []
    )
    scenes: list[str] = []
    for scene_index, event in enumerate(summary["chronological_events"], start=1):
        group = group_map[event["group_id"]]
        highlighted = event["group_id"] in highlight_ids
        scenes.append(
            _render_scene(
                timeline,
                group,
                event,
                scene_index,
                assets,
                clip_candidates=[
                    c for c in candidates if c["group_id"] == group["group_id"]
                ],
                highlighted=highlighted,
                initially_open=index == 1 and scene_index == 1,
            )
        )
    notable_count = sum(
        len(group["context_review"].get("notable_moments", []))
        for group in timeline["context_groups"]
    )
    tags = "".join(f"<span>{_escape(tag)}</span>" for tag in summary["tags"])
    detail_url = _detail_url(timeline_path, output_dir)
    detail_link = (
        f'<a class="detail-link" href="{detail_url}">개별 장면 타임라인 열기 <span>↗</span></a>'
        if detail_url
        else '<span class="detail-link is-disabled">개별 타임라인 미생성</span>'
    )
    duration = float(timeline["media"]["duration"])
    search_text = " ".join(
        [
            summary["title"],
            summary["one_line_summary"],
            summary["narrative_summary"],
            *summary["tags"],
            *(event["headline"] for event in summary["chronological_events"]),
            *(
                notable["title"]
                for group in timeline["context_groups"]
                for notable in group["context_review"].get("notable_moments", [])
            ),
        ]
    ).lower()
    preview_url = assets.url(representative["frame"])
    nav = f"""
      <li class="footage-item" data-search="{_escape(search_text)}">
        <button type="button" data-select-clip="{_escape(timeline["asset_id"])}" aria-current="{"true" if index == 1 else "false"}">
          <img loading="lazy" src="{preview_url}" alt="">
          <span><strong>{_escape(summary["title"])}</strong><small>{_escape(timeline["source"]["name"])}</small><i><b>{index:02d} · {_escape(capture_time)}</b><b>{_escape(_short_time(duration))}</b></i></span>
        </button>
      </li>
    """
    ruler = "".join(
        f'<button type="button" data-jump-scene="scene-{_escape(timeline["asset_id"])}-{_escape(group["group_id"])}" style="flex:{max(8.0, float(group["end"]) - float(group["start"])):.3f}"><b>{_escape(group["group_id"])}</b><span>{_escape(_short_time(float(group["start"])))}</span></button>'
        for group in timeline["context_groups"]
    )
    panel = f"""
      <article class="clip-panel" data-clip-panel="{_escape(timeline["asset_id"])}" data-search="{_escape(search_text)}" data-media-url="{_escape(proxy_url or "")}" {"" if index == 1 else "hidden"}>
        <header class="clip-header">
          <div class="clip-title"><span>VIDEO {index:02d} · {_escape(capture_date)}</span><h1>{_escape(summary["title"])}</h1><p>{_escape(summary["one_line_summary"])}</p></div>
          <dl><div><dt>촬영</dt><dd>{_escape(capture_time)}</dd></div><div><dt>길이</dt><dd>{_escape(_short_time(duration))}</dd></div><div><dt>장면</dt><dd>{len(timeline["context_groups"])}</dd></div><div><dt>특이</dt><dd>{notable_count}</dd></div></dl>
        </header>
        <div class="video-summary"><p>{_escape(summary["narrative_summary"])}</p><div class="tag-row">{tags}</div>{detail_link}</div>
        <nav class="scene-ruler" aria-label="{_escape(summary["title"])} 구간 탐색">{ruler}</nav>
        <section class="scene-list" aria-label="시간순 장면 목록">{"".join(scenes)}</section>
      </article>
    """
    return nav, panel


def render_video_library(
    timeline_paths: list[Path],
    output_dir: Path,
    *,
    title: str = "Travel video field log",
    asset_mode: str = "embed",
    proxy_root: Path | None = None,
) -> Path:
    if not timeline_paths:
        raise ValueError("render-library requires at least one summarized timeline")
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for timeline_path in timeline_paths:
        resolved = timeline_path.resolve()
        timeline = load_json(resolved)
        if timeline.get("schema_version") != VIDEO_SUMMARIZED_TIMELINE_SCHEMA:
            raise ValueError(
                f"render-library requires summarized timelines: {resolved}"
            )
        loaded.append((resolved, timeline))
    loaded.sort(
        key=lambda item: (
            item[1]["media"].get("creation_time") or "9999",
            item[1]["source"]["name"],
        )
    )

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    assets = ImageAssetResolver(output_dir, asset_mode)
    resolved_proxy_root = proxy_root.expanduser().resolve() if proxy_root else None
    proxies = _discover_proxies(resolved_proxy_root)
    proxy_urls: dict[str, str] = {}
    for _, timeline in loaded:
        proxy = proxies.get(str(timeline["source"]["name"]).casefold())
        if proxy:
            proxy_urls[timeline["asset_id"]] = _link_proxy(
                proxy, output_dir, timeline["asset_id"]
            )
    rendered = [
        _render_video(
            timeline,
            timeline_path,
            index,
            output_dir,
            assets,
            proxy_urls.get(timeline["asset_id"]),
        )
        for index, (timeline_path, timeline) in enumerate(loaded, start=1)
    ]
    footage_items = "".join(item[0] for item in rendered)
    clip_panels = "".join(item[1] for item in rendered)
    total_duration = sum(float(item[1]["media"]["duration"]) for item in loaded)
    total_scenes = sum(len(item[1]["context_groups"]) for item in loaded)
    total_notables = sum(
        len(group["context_review"].get("notable_moments", []))
        for _, timeline in loaded
        for group in timeline["context_groups"]
    )
    date_label, time_range = _capture_range_labels(
        loaded[0][1]["media"].get("creation_time"),
        loaded[-1][1]["media"].get("creation_time"),
    )
    initial_timeline = loaded[0][1]
    initial_group = initial_timeline["context_groups"][0]
    initial_context = initial_group["context_review"]
    initial_sample = next(
        sample
        for sample in initial_timeline["samples"]
        if sample["sample_id"] == initial_context["representative_sample_id"]
    )
    template = (
        files("travel_video.templates").joinpath("library.html").read_text("utf-8")
    )
    library_identity = json.dumps(
        [
            {
                "schema_version": timeline["schema_version"],
                "asset_id": timeline["asset_id"],
                "source_name": timeline["source"]["name"],
                "creation_time": timeline["media"].get("creation_time"),
                "duration": timeline["media"]["duration"],
                "scenes": [
                    {
                        "group_id": group["group_id"],
                        "source_in": group["start"],
                        "source_out": group["end"],
                    }
                    for group in timeline["context_groups"]
                ],
            }
            for _, timeline in loaded
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    library_id = hashlib.sha256(library_identity.encode("utf-8")).hexdigest()[:16]
    replacements = {
        "__LIBRARY_ID__": library_id,
        "__DOCUMENT_TITLE__": _escape(title),
        "__TITLE__": _escape(title),
        "__VIDEO_COUNT__": str(len(loaded)),
        "__TOTAL_DURATION__": _escape(_duration_text(total_duration)),
        "__TOTAL_SCENES__": str(total_scenes),
        "__TOTAL_NOTABLES__": str(total_notables),
        "__DATE_LABEL__": _escape(date_label),
        "__TIME_RANGE__": _escape(time_range),
        "__FOOTAGE_ITEMS__": footage_items,
        "__CLIP_PANELS__": clip_panels,
        "__INITIAL_PREVIEW_URL__": assets.url(initial_sample["frame"]),
        "__INITIAL_PREVIEW_TITLE__": _escape(initial_group["label"]),
        "__INITIAL_PREVIEW_TIME__": _escape(
            f"{_short_time(float(initial_group['start']))} — {_short_time(float(initial_group['end']))}"
        ),
        "__INITIAL_PREVIEW_TIMECODE__": _escape(initial_sample["timecode"]),
    }
    document = template
    for token, value in replacements.items():
        document = document.replace(token, value)
    output_path = output_dir / "index.html"
    output_path.write_text(document, encoding="utf-8")
    manifest = {
        "schema_version": LIBRARY_SCHEMA,
        "output": str(output_path),
        "title": title,
        "library_id": library_id,
        "asset_mode": asset_mode,
        "embedded_asset_count": assets.unique_count,
        "embedded_source_bytes": assets.embedded_bytes,
        "video_count": len(loaded),
        "scene_count": total_scenes,
        "notable_moment_count": total_notables,
        "proxy_root": str(resolved_proxy_root) if resolved_proxy_root else None,
        "proxy_video_count": len(proxy_urls),
        "reconciled_transcript_video_count": sum(
            "reconciled_transcript" in timeline for _, timeline in loaded
        ),
        "reviewed_dialogue_video_count": sum(
            "reviewed_dialogue" in timeline for _, timeline in loaded
        ),
        "dialogue_script_video_count": sum(
            "dialogue_script" in timeline for _, timeline in loaded
        ),
        "timelines": [str(path) for path, _ in loaded],
    }
    atomic_json(output_dir / "manifest.json", manifest)
    return output_path
