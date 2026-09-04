from __future__ import annotations

import json
from pathlib import Path

import pytest

from travel_video.library import render_video_library
from travel_video.video_summary import (
    build_video_summary_packet,
    merge_video_summary,
    validate_video_summary,
)
from travel_video.web import render_timeline_web


def context_timeline(
    tmp_path: Path, *, asset_id: str, creation_time: str, source_name: str
) -> tuple[Path, Path]:
    frame = tmp_path / f"{asset_id}.jpg"
    frame.write_bytes(f"image-{asset_id}".encode())
    timeline = {
        "schema_version": "phase1-context-reviewed-timeline/v1",
        "asset_id": asset_id,
        "source": {"name": source_name, "path": str(tmp_path / source_name)},
        "media": {
            "duration": 20.0,
            "creation_time": creation_time,
            "video": {"width": 3840, "height": 2160, "codec": "hevc"},
        },
        "samples": [
            {
                "sample_id": "F0001",
                "time": 0.0,
                "timecode": "00:00.000",
                "frame": str(frame),
                "quality": 1.0,
                "visual_change": 0.0,
                "average_hash": "0" * 16,
            }
        ],
        "segments": [
            {
                "segment_id": "S001",
                "start": 0.0,
                "end": 20.0,
                "representative_frame": str(frame),
                "review": {
                    "visual_summary": "시장 입구에서 가게를 둘러본다.",
                    "actions": ["도착", "탐색"],
                },
                "transcript_candidates": {
                    "ko": [
                        {
                            "start": 2.0,
                            "end": 3.0,
                            "text": "무엇을 먹을까",
                            "avg_logprob": -0.4,
                        }
                    ],
                    "en": [
                        {
                            "start": 2.0,
                            "end": 3.0,
                            "text": "What should we eat?",
                            "avg_logprob": -0.7,
                        }
                    ],
                },
            }
        ],
        "context_groups": [
            {
                "group_id": "G001",
                "label": "시장 도착",
                "start": 0.0,
                "end": 20.0,
                "timecode": "00:00.000-00:20.000",
                "segment_ids": ["S001"],
                "context_review": {
                    "narrative_summary": "시장에 도착해 가게를 둘러본다.",
                    "dialogue_summary": "무엇을 먹을지 이야기한다.",
                    "representative_sample_id": "F0001",
                    "key_moments": [{"sample_id": "F0001", "role": "도착"}],
                    "notable_moments": [
                        {
                            "sample_id": "F0001",
                            "category": "reaction",
                            "title": "뜻밖의 웃긴 표정",
                            "description": "카메라를 보고 즉흥적으로 표정을 짓는다.",
                            "edit_hint": "짧은 리액션 컷으로 유지",
                        }
                    ],
                },
            }
        ],
    }
    timeline_path = tmp_path / f"{asset_id}.context.json"
    timeline_path.write_text(json.dumps(timeline), encoding="utf-8")
    return timeline_path, frame


def summary_review(asset_id: str, title: str) -> dict:
    return {
        "schema_version": "phase1-video-summary/v1",
        "asset_id": asset_id,
        "title": title,
        "one_line_summary": "시장에 도착해 먹을 곳을 찾는다.",
        "narrative_summary": "시장 입구에 도착한 뒤 가게를 둘러보며 먹을 것을 정한다.",
        "chronological_events": [
            {
                "group_id": "G001",
                "headline": "시장 도착과 탐색",
                "description": "가게를 훑으며 다음 행동을 정한다.",
            }
        ],
        "highlight_group_ids": ["G001"],
        "representative_sample_id": "F0001",
        "tags": ["시장", "음식"],
    }


def test_video_summary_packet_validation_and_merge(tmp_path: Path) -> None:
    timeline_path, _ = context_timeline(
        tmp_path,
        asset_id="asset-1",
        creation_time="2026-08-25T22:38:21Z",
        source_name="clip-1.mp4",
    )
    packet_path = tmp_path / "summary-packet.json"
    packet = build_video_summary_packet(timeline_path, packet_path)
    review = summary_review("asset-1", "시장 입구 탐색")
    validate_video_summary(packet, review)

    review_path = tmp_path / "summary.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    output_path = tmp_path / "summarized.json"
    merged = merge_video_summary(timeline_path, packet_path, review_path, output_path)
    assert merged["schema_version"] == "phase1-video-summarized-timeline/v1"
    assert merged["video_summary"]["title"] == "시장 입구 탐색"
    web_output = render_timeline_web(output_path, tmp_path / "video-web")
    web_document = web_output.read_text(encoding="utf-8")
    assert "VIDEO SYNOPSIS" in web_document
    assert "시장에 도착해 먹을 곳을 찾는다." in web_document

    review["chronological_events"][0]["group_id"] = "G999"
    with pytest.raises(ValueError, match="group order"):
        validate_video_summary(packet, review)


def test_library_sorts_videos_by_capture_time(tmp_path: Path) -> None:
    later_path, _ = context_timeline(
        tmp_path,
        asset_id="later",
        creation_time="2026-08-26T22:44:54Z",
        source_name="later.mp4",
    )
    earlier_path, _ = context_timeline(
        tmp_path,
        asset_id="earlier",
        creation_time="2026-08-25T22:38:21Z",
        source_name="earlier.mp4",
    )
    summarized_paths: list[Path] = []
    for timeline_path, asset_id, title in [
        (later_path, "later", "나중 영상"),
        (earlier_path, "earlier", "먼저 영상"),
    ]:
        packet_path = tmp_path / f"{asset_id}.packet.json"
        review_path = tmp_path / f"{asset_id}.summary.json"
        output_path = tmp_path / f"{asset_id}.summarized.json"
        build_video_summary_packet(timeline_path, packet_path)
        review_path.write_text(
            json.dumps(summary_review(asset_id, title)), encoding="utf-8"
        )
        merge_video_summary(timeline_path, packet_path, review_path, output_path)
        summarized_paths.append(output_path)

    proxy_root = tmp_path / "proxies"
    (proxy_root / "day-1").mkdir(parents=True)
    earlier_proxy = proxy_root / "day-1" / "earlier.mp4"
    later_proxy = proxy_root / "day-1" / "later.mp4"
    earlier_proxy.write_bytes(b"earlier proxy")
    later_proxy.write_bytes(b"later proxy")
    (proxy_root / "day-1" / "ignored.partial.mp4").write_bytes(b"partial")
    output = render_video_library(
        summarized_paths,
        tmp_path / "library",
        title="아침 음식 산책",
        proxy_root=proxy_root,
    )
    document = output.read_text(encoding="utf-8")
    manifest = json.loads((output.parent / "manifest.json").read_text())
    assert document.index("먼저 영상") < document.index("나중 영상")
    assert "아침 음식 산책" in document
    assert "data:image/jpeg;base64," in document
    assert "2026.08.25 — 2026.08.26" in document
    assert "뜻밖의 웃긴 표정" in document
    assert 'data-search="' in document
    assert 'data-select-clip="earlier"' in document
    assert "세부 구간" in document
    assert "STT 원문 후보" in document
    assert "무엇을 먹을까" in document
    assert 'data-media-url="media/earlier.mp4"' in document
    assert "data-preview-video" in document
    assert document.count('data-review-workspace') == 1
    assert 'data-workspace-mode="review"' in document
    assert 'data-workspace-mode="rough-cut"' in document
    assert 'data-revision-panel' in document
    assert 'data-rough-clips' in document
    assert "workspace.classList.toggle('is-rough-cut'" in document
    assert (output.parent / "media" / "earlier.mp4").is_symlink()
    assert (output.parent / "media" / "earlier.mp4").resolve() == earlier_proxy
    assert manifest["video_count"] == 2
    assert manifest["proxy_video_count"] == 2
    assert manifest["proxy_root"] == str(proxy_root.resolve())
    assert manifest["timelines"][0].endswith("earlier.summarized.json")
