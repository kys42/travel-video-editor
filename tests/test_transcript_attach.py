from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from travel_video.library import render_video_library
from travel_video.dialogue_script import attach_dialogue_script
from travel_video.transcript_attach import (
    attach_reconciled_transcript,
    validate_transcript_timeline_alignment,
)
from travel_video.web import render_timeline_web


def _timeline(tmp_path: Path) -> dict:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    source_path = unicodedata.normalize("NFD", "/Volumes/T7/시애틀/clip.mp4")
    return {
        "schema_version": "phase1-video-summarized-timeline/v1",
        "asset_id": "clip--fingerprint",
        "source": {
            "name": "clip.mp4",
            "path": source_path,
            "quick_fingerprint": "abc123",
        },
        "media": {
            "duration": 10.0,
            "creation_time": "2026-08-25T22:00:00Z",
            "video": {"width": 1920, "height": 1080, "codec": "h264"},
        },
        "samples": [
            {
                "sample_id": "F0001",
                "time": 1.0,
                "timecode": "00:01.000",
                "frame": str(frame),
            }
        ],
        "segments": [
            {
                "segment_id": "S001",
                "start": 0.0,
                "end": 5.0,
                "representative_frame": str(frame),
                "machine": {"quality": 1.0, "flags": []},
                "review": {
                    "visual_summary": "가게 앞에서 주문한다.",
                    "actions": ["주문"],
                    "importance": 0.8,
                },
                "transcript_candidates": {
                    "ko": [
                        {
                            "start": 1.0,
                            "end": 2.0,
                            "text": "안녕 하 세 요",
                            "avg_logprob": -0.6,
                        }
                    ]
                },
            },
            {
                "segment_id": "S002",
                "start": 5.0,
                "end": 10.0,
                "representative_frame": str(frame),
                "machine": {"quality": 0.9, "flags": []},
                "review": {
                    "visual_summary": "음료를 받는다.",
                    "actions": ["수령"],
                    "importance": 0.7,
                },
                "transcript_candidates": {},
            },
        ],
        "context_groups": [
            {
                "group_id": "G001",
                "label": "음료 주문",
                "start": 0.0,
                "end": 10.0,
                "timecode": "00:00.000-00:10.000",
                "segment_ids": ["S001", "S002"],
                "context_review": {
                    "narrative_summary": "가게에서 음료를 주문하고 받는다.",
                    "dialogue_summary": "음료를 주문한다.",
                    "dialogue_evidence": ["ko", "en"],
                    "representative_sample_id": "F0001",
                    "representative_reason": "주문 장면이 보인다.",
                    "key_moments": [{"sample_id": "F0001", "role": "주문"}],
                    "confidence": 0.9,
                },
            }
        ],
        "video_summary": {
            "schema_version": "phase1-video-summary/v1",
            "asset_id": "clip--fingerprint",
            "title": "음료 주문",
            "one_line_summary": "가게에서 음료를 주문한다.",
            "narrative_summary": "인사를 건넨 뒤 영어로 음료를 주문하고 받는다.",
            "chronological_events": [
                {
                    "group_id": "G001",
                    "headline": "음료 주문",
                    "description": "인사하고 음료를 고른다.",
                }
            ],
            "highlight_group_ids": ["G001"],
            "representative_sample_id": "F0001",
            "tags": ["음료", "주문"],
        },
    }


def _transcript() -> dict:
    return {
        "schema_version": "reconciled-transcript/v1",
        "source": {
            "name": "clip.mp4",
            "path": unicodedata.normalize("NFC", "/Volumes/T7/시애틀/clip.mp4"),
            "quick_fingerprint": "abc123",
        },
        "inputs": {"reconciliation_packet": "/tmp/packet.json"},
        "summary": {"utterance_count": 2},
        "utterances": [
            {
                "utterance_id": "U0001",
                "window_id": "RW0001",
                "start": 1.0,
                "end": 2.0,
                "language": "ko",
                "original_text": "안녕하세요",
                "translations": {"en": "Hello"},
                "confidence": 0.92,
                "source_candidate_ids": ["APPLE-ko-KR-T0001"],
            },
            {
                "utterance_id": "U0002",
                "window_id": "RW0002",
                "start": 4.5,
                "end": 5.5,
                "language": "en",
                "original_text": "One Coke, please.",
                "translations": {"ko": "콜라 하나 주세요."},
                "confidence": 0.84,
                "source_candidate_ids": ["APPLE-en-US-T0002"],
            },
        ],
    }


def test_attach_reconciled_transcript_and_render_views(tmp_path: Path) -> None:
    timeline = _timeline(tmp_path)
    transcript = _transcript()
    timeline_path = tmp_path / "timeline.summarized.json"
    transcript_path = tmp_path / "transcript.reconciled.json"
    output_path = tmp_path / "timeline.transcript-attached.json"
    timeline_path.write_text(json.dumps(timeline), encoding="utf-8")
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")

    attach_reconciled_transcript(timeline_path, transcript_path, output_path)
    attached = json.loads(output_path.read_text(encoding="utf-8"))

    assert attached["schema_version"] == timeline["schema_version"]
    assert attached["video_summary"] == timeline["video_summary"]
    assert [item["utterance_id"] for item in attached["segments"][0]["reconciled_utterances"]] == [
        "U0001",
        "U0002",
    ]
    assert [item["utterance_id"] for item in attached["segments"][1]["reconciled_utterances"]] == [
        "U0002"
    ]
    assert attached["segments"][1]["reconciled_utterances"][0]["overlap_start"] == 5.0
    assert len(attached["context_groups"][0]["reconciled_utterances"]) == 2
    assert attached["reconciled_transcript"]["utterances"] == transcript["utterances"]
    assert (
        attached["reconciled_transcript"]["attachment"]["source_timebase"]
        == "seconds_from_source_start"
    )

    web_path = render_timeline_web(output_path, tmp_path / "web")
    web = web_path.read_text(encoding="utf-8")
    assert "종합 대본과 타임코드 보기" in web
    assert "안녕하세요" in web
    assert "One Coke, please." in web
    assert "EN: Hello" in web
    assert "안녕 하 세 요" in web
    web_manifest = json.loads(
        (web_path.parent / "manifest.json").read_text(encoding="utf-8")
    )
    assert web_manifest["has_reconciled_transcript"] is True

    library_path = render_video_library([output_path], tmp_path / "library")
    library = library_path.read_text(encoding="utf-8")
    assert "종합 원문" in library
    assert "안녕하세요" in library
    assert "One Coke, please." in library
    library_manifest = json.loads(
        (library_path.parent / "manifest.json").read_text(encoding="utf-8")
    )
    assert library_manifest["reconciled_transcript_video_count"] == 1


def test_attachment_rejects_mismatched_source_or_duration(tmp_path: Path) -> None:
    timeline = _timeline(tmp_path)
    transcript = _transcript()
    transcript["source"]["quick_fingerprint"] = "different"
    with pytest.raises(ValueError, match="fingerprint"):
        validate_transcript_timeline_alignment(timeline, transcript)

    transcript["source"]["quick_fingerprint"] = "abc123"
    transcript["utterances"][1]["end"] = 10.5
    with pytest.raises(ValueError, match="outside timeline duration"):
        validate_transcript_timeline_alignment(timeline, transcript)


def test_dialogue_script_groups_fragments_and_drives_renderers(tmp_path: Path) -> None:
    timeline = _timeline(tmp_path)
    transcript = _transcript()
    transcript["utterances"] = [
        {
            **transcript["utterances"][0],
            "start": 1.0,
            "end": 1.8,
            "original_text": "기차를 타러 가야 되는데",
            "translations": {"en": "We need to catch the train,"},
        },
        {
            **transcript["utterances"][0],
            "utterance_id": "U0002",
            "window_id": "RW0002",
            "start": 2.2,
            "end": 3.4,
            "original_text": "준비하는 데 시간이 좀 걸렸어요.",
            "translations": {"en": "but getting ready took a while."},
        },
        {
            **transcript["utterances"][1],
            "utterance_id": "U0003",
            "window_id": "RW0003",
            "start": 6.0,
            "end": 7.0,
        },
    ]
    timeline_path = tmp_path / "timeline.summarized.json"
    transcript_path = tmp_path / "transcript.reconciled.json"
    attached_path = tmp_path / "timeline.final.json"
    scripted_path = tmp_path / "dialogue" / "timeline.scripted.json"
    timeline_path.write_text(json.dumps(timeline), encoding="utf-8")
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")

    attach_reconciled_transcript(timeline_path, transcript_path, attached_path)
    attach_dialogue_script(attached_path, scripted_path)
    scripted = json.loads(scripted_path.read_text(encoding="utf-8"))

    assert scripted["dialogue_script"]["source_utterance_count"] == 3
    assert scripted["dialogue_script"]["line_count"] == 2
    first = scripted["dialogue_script"]["lines"][0]
    assert first["original_text"] == (
        "기차를 타러 가야 되는데 준비하는 데 시간이 좀 걸렸어요."
    )
    assert first["source_utterance_ids"] == ["U0001", "U0002"]
    assert scripted["reconciled_transcript"]["utterances"] == transcript["utterances"]
    assert scripted["segments"][0]["dialogue_lines"][0]["source_start"] == 1.0

    web_path = render_timeline_web(scripted_path, tmp_path / "scripted-web")
    web = web_path.read_text(encoding="utf-8")
    assert "정리 대본과 타임코드 보기" in web
    assert "2개 대사 블록" in web
    web_manifest = json.loads(
        (web_path.parent / "manifest.json").read_text(encoding="utf-8")
    )
    assert web_manifest["has_dialogue_script"] is True

    library_path = render_video_library([scripted_path], tmp_path / "scripted-library")
    library = library_path.read_text(encoding="utf-8")
    assert "정리 대본" in library
    assert "기차를 타러 가야 되는데 준비하는 데 시간이 좀 걸렸어요." in library
    library_manifest = json.loads(
        (library_path.parent / "manifest.json").read_text(encoding="utf-8")
    )
    assert library_manifest["dialogue_script_video_count"] == 1
