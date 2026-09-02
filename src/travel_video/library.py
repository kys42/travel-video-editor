from __future__ import annotations

import html
import os
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import quote

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


def _render_video(
    timeline: dict[str, Any],
    timeline_path: Path,
    index: int,
    output_dir: Path,
    assets: ImageAssetResolver,
) -> str:
    summary = timeline["video_summary"]
    sample_map = {sample["sample_id"]: sample for sample in timeline["samples"]}
    group_map = {group["group_id"]: group for group in timeline["context_groups"]}
    representative = sample_map[summary["representative_sample_id"]]
    capture_time, capture_date = _capture_parts(timeline["media"].get("creation_time"))
    highlight_ids = set(summary.get("highlight_group_ids", []))
    events: list[str] = []
    for event in summary["chronological_events"]:
        group = group_map[event["group_id"]]
        highlighted = event["group_id"] in highlight_ids
        events.append(
            f"""
            <li class="event{" is-highlight" if highlighted else ""}">
              <span class="event-time">{_escape(format_time(float(group["start"])).split(".", 1)[0])}</span>
              <span class="event-marker" aria-hidden="true"></span>
              <div>
                <span class="event-id">{_escape(event["group_id"])}{" · HIGHLIGHT" if highlighted else ""}</span>
                <strong>{_escape(event["headline"])}</strong>
                <p>{_escape(event["description"])}</p>
              </div>
            </li>
            """
        )

    notable_items: list[str] = []
    for group in timeline["context_groups"]:
        for notable in group["context_review"].get("notable_moments", []):
            sample = sample_map[notable["sample_id"]]
            notable_items.append(
                f"""
                <li>
                  <span>{_escape(sample["timecode"])}</span>
                  <strong>{_escape(notable["title"])}</strong>
                  <small>{_escape(notable["edit_hint"])}</small>
                </li>
                """
            )
    notable_html = (
        f'<aside class="clip-notables"><span class="section-label">NOTABLE BEATS</span><ul>{"".join(notable_items)}</ul></aside>'
        if notable_items
        else ""
    )
    tags = "".join(f"<span>{_escape(tag)}</span>" for tag in summary["tags"])
    detail_url = _detail_url(timeline_path, output_dir)
    detail_link = (
        f'<a class="detail-link" href="{detail_url}">개별 장면 타임라인 열기 <span>↗</span></a>'
        if detail_url
        else '<span class="detail-link is-disabled">개별 타임라인 미생성</span>'
    )
    duration = float(timeline["media"]["duration"])
    notable_count = len(notable_items)
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
    return f"""
    <article class="clip" id="clip-{_escape(timeline["asset_id"])}" data-search="{_escape(search_text)}">
      <div class="clip-sequence">
        <span>{index:02d}</span>
        <strong>{_escape(capture_time)}</strong>
        <small>{_escape(capture_date)}</small>
      </div>
      <div class="clip-body">
        <header class="clip-head">
          <figure>
            <img loading="lazy" src="{assets.url(representative["frame"])}" alt="{_escape(summary["title"])} 대표 프레임">
            <figcaption>{_escape(representative["timecode"])} · {_escape(summary["representative_sample_id"])}</figcaption>
          </figure>
          <div class="clip-intro">
            <div class="clip-kicker"><span>VIDEO {index:02d}</span><span>{_escape(timeline["source"]["name"])}</span></div>
            <h2>{_escape(summary["title"])}</h2>
            <p class="one-line">{_escape(summary["one_line_summary"])}</p>
            <p class="synopsis">{_escape(summary["narrative_summary"])}</p>
            <div class="tag-row">{tags}</div>
          </div>
          <dl class="clip-stats">
            <div><dt>길이</dt><dd>{_escape(_duration_text(duration))}</dd></div>
            <div><dt>사건</dt><dd>{len(timeline["context_groups"])}</dd></div>
            <div><dt>특이 포인트</dt><dd>{notable_count}</dd></div>
          </dl>
        </header>
        <div class="clip-detail">
          <section class="clip-events">
            <div class="clip-section-head"><span class="section-label">WHAT HAPPENED</span><strong>시간순 사건</strong></div>
            <ol>{"".join(events)}</ol>
          </section>
          {notable_html}
        </div>
        <footer>{detail_link}</footer>
      </div>
    </article>
    """


def render_video_library(
    timeline_paths: list[Path],
    output_dir: Path,
    *,
    title: str = "Travel video field log",
    asset_mode: str = "embed",
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
    clips = "".join(
        _render_video(timeline, timeline_path, index, output_dir, assets)
        for index, (timeline_path, timeline) in enumerate(loaded, start=1)
    )
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
    run_lines = "".join(
        f"<li><span>{index:02d}</span><strong>{_escape(timeline['video_summary']['title'])}</strong><small>{_escape(timeline['video_summary']['one_line_summary'])}</small></li>"
        for index, (_, timeline) in enumerate(loaded, start=1)
    )
    template = (
        files("travel_video.templates").joinpath("library.html").read_text("utf-8")
    )
    replacements = {
        "__DOCUMENT_TITLE__": _escape(title),
        "__TITLE__": _escape(title),
        "__VIDEO_COUNT__": str(len(loaded)),
        "__TOTAL_DURATION__": _escape(_duration_text(total_duration)),
        "__TOTAL_SCENES__": str(total_scenes),
        "__TOTAL_NOTABLES__": str(total_notables),
        "__DATE_LABEL__": _escape(date_label),
        "__TIME_RANGE__": _escape(time_range),
        "__RUN_LINES__": run_lines,
        "__CLIPS__": clips,
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
        "asset_mode": asset_mode,
        "embedded_asset_count": assets.unique_count,
        "embedded_source_bytes": assets.embedded_bytes,
        "video_count": len(loaded),
        "scene_count": total_scenes,
        "notable_moment_count": total_notables,
        "timelines": [str(path) for path, _ in loaded],
    }
    atomic_json(output_dir / "manifest.json", manifest)
    return output_path
