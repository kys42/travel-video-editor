from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from travel_video.cli import build_parser
from travel_video.scene_dialogue import (
    BOUNDARY_PROPOSAL_SCHEMA,
    DIALOGUE_PRESERVATION_POLICY,
    REVIEWED_DIALOGUE_SCHEMA,
    SCENE_DIALOGUE_PACKET_SCHEMA,
    build_no_candidate_scene_dialogue_review,
    build_scene_dialogue_packet,
    merge_scene_dialogue_review,
    merge_scene_dialogue_review_shards,
    slice_scene_dialogue_packet,
    validate_boundary_proposals,
    validate_scene_dialogue_review,
)
from travel_video.transcript_reconcile import _locale_fragment
from travel_video.web import render_timeline_web


def _timeline(tmp_path: Path) -> dict:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"frame")
    return {
        "schema_version": "phase1-video-summarized-timeline/v1",
        "asset_id": "clip--abc123",
        "source": {
            "name": "clip.mp4",
            "path": "/Volumes/T7/clip.mp4",
            "quick_fingerprint": "abc123",
        },
        "media": {
            "duration": 12.0,
            "video": {"width": 1920, "height": 1080, "codec": "h264"},
        },
        "samples": [
            {
                "sample_id": "F0001",
                "time": 2.0,
                "timecode": "00:02.000",
                "frame": str(frame),
            }
        ],
        "segments": [
            {
                "segment_id": "S001",
                "start": 0.0,
                "end": 6.0,
                "machine": {"flags": ["handheld"]},
                "review": {
                    "visual_summary": "기차표를 확인한다.",
                    "actions": ["표 확인"],
                    "importance": 0.8,
                },
            },
            {
                "segment_id": "S002",
                "start": 6.0,
                "end": 12.0,
                "machine": {"flags": []},
                "review": {
                    "visual_summary": "플랫폼으로 이동한다.",
                    "actions": ["이동"],
                    "importance": 0.7,
                },
            },
        ],
        "context_groups": [
            {
                "group_id": "G001",
                "label": "표 확인",
                "start": 0.0,
                "end": 6.0,
                "segment_ids": ["S001"],
                "context_review": {
                    "narrative_summary": "출발 전에 기차표를 확인한다.",
                    "dialogue_summary": "기차를 타러 가야 한다고 말한다.",
                    "representative_sample_id": "F0001",
                    "representative_reason": "표가 보인다.",
                    "key_moments": [{"sample_id": "F0001", "role": "ticket"}],
                    "notable_moments": [],
                },
            },
            {
                "group_id": "G002",
                "label": "플랫폼 이동",
                "start": 6.0,
                "end": 12.0,
                "segment_ids": ["S002"],
                "context_review": {
                    "narrative_summary": "플랫폼으로 걸어간다.",
                    "dialogue_summary": "영어 안내가 들린다.",
                    "representative_sample_id": "F0001",
                    "representative_reason": "이동 장면을 대표한다.",
                    "key_moments": [],
                    "notable_moments": [],
                },
            },
        ],
        "keep_me": {"unchanged": True},
    }


def _candidate(
    locale: str,
    candidate_id: str,
    text: str,
    start: float,
    end: float,
) -> dict:
    return {
        "utterance_id": candidate_id,
        "requested_locale": locale,
        "start": start,
        "end": end,
        "text": text,
        "mean_confidence": 0.9,
        "spans": [
            {
                "start": start,
                "end": end,
                "text": text,
                "confidence": 0.9,
            }
        ],
    }


def test_locale_fragment_drops_sub_millisecond_boundary_phantom() -> None:
    candidate = _candidate(
        "ko-KR",
        "APPLE-ko-KR-T0001",
        "다음 창에 온전히 들어가는 발화",
        29.22,
        30.42,
    )

    assert _locale_fragment(
        [candidate], "ko-KR", 22.68, 29.2200003
    ) is None
    next_fragment = _locale_fragment(
        [candidate], "ko-KR", 29.2200003, 37.02
    )

    assert next_fragment is not None
    assert next_fragment["text"] == "다음 창에 온전히 들어가는 발화"
    assert next_fragment["evidence_spans"][0]["start"] == 29.22
    assert next_fragment["evidence_spans"][0]["end"] == 30.42


def _apple_transcript() -> dict:
    return {
        "schema_version": "apple-stt/v1",
        "source": {
            "name": "clip.mp4",
            "path": "/Volumes/T7/clip.mp4",
            "quick_fingerprint": "abc123",
        },
        "locales": ["ko-KR", "en-US"],
        "paths": {"raw": {"ko-KR": "/tmp/ko.json", "en-US": "/tmp/en.json"}},
        "activity_intervals": [
            {"activity_id": "A001", "start": 1.0, "end": 3.0},
            {"activity_id": "A002", "start": 5.5, "end": 7.0},
            {"activity_id": "A003", "start": 8.0, "end": 9.0},
        ],
        "candidates": [
            _candidate("ko-KR", "APPLE-ko-KR-T0001", "기차를 타러 가야 해", 1.2, 2.8),
            _candidate("en-US", "APPLE-en-US-T0001", "Catch a train", 1.2, 2.8),
            _candidate("en-US", "APPLE-en-US-T0002", "This way please", 5.5, 6.8),
            _candidate("ko-KR", "APPLE-ko-KR-T0003", "불명확", 8.1, 8.8),
        ],
    }


def _quick_timeline(tmp_path: Path) -> dict:
    timeline = _timeline(tmp_path)
    timeline["schema_version"] = "phase1-reviewed-timeline/v1"
    timeline["reviewed_groups"] = [
        {
            "group_id": group["group_id"],
            "label": group["label"],
            "summary": group["context_review"]["narrative_summary"],
            "segment_ids": group["segment_ids"],
        }
        for group in timeline.pop("context_groups")
    ]
    return timeline


def _review(packet: dict) -> dict:
    scene_one, scene_two = packet["scenes"]
    ko_evidence = scene_one["windows"][0]["apple_candidates"]["ko-KR"][
        "evidence_spans"
    ][0]["evidence_id"]
    en_evidence = scene_two["windows"][0]["apple_candidates"]["en-US"][
        "evidence_spans"
    ][0]["evidence_id"]
    uncertain_evidence = scene_two["windows"][1]["apple_candidates"]["ko-KR"][
        "evidence_spans"
    ][0]["evidence_id"]
    return {
        "schema_version": "scene-dialogue-review/v1",
        "reviewer": "luna",
        "method": "one-pass scene review",
        "scenes": [
            {
                "group_id": "G001",
                "window_decisions": [
                    {
                        "window_id": "RW0001",
                        "status": "resolved",
                        "source_utterance_ids": ["G001-U001"],
                    }
                ],
                "utterances": [
                    {
                        "utterance_id": "G001-U001",
                        "start": 1.2,
                        "end": 2.8,
                        "language": "ko",
                        "original_text": "기차를 타러 가야 해",
                        "translations": {"en": "We need to catch the train."},
                        "confidence": 0.92,
                        "review_status": "reviewed",
                        "source_evidence_ids": [ko_evidence],
                        "source_window_ids": ["RW0001"],
                        "source_candidate_ids": ["APPLE-ko-KR-T0001"],
                    }
                ],
                "captions": [
                    {
                        "caption_id": "G001-C001",
                        "start": 1.1,
                        "end": 3.2,
                        "language": "ko",
                        "display_text": "기차를 타러 가야 해.",
                        "edit_type": "normalized",
                        "confidence": 0.92,
                        "review_status": "reviewed",
                        "source_utterance_ids": ["G001-U001"],
                        "source_window_ids": ["RW0001"],
                        "source_candidate_ids": ["APPLE-ko-KR-T0001"],
                    }
                ],
            },
            {
                "group_id": "G002",
                "window_decisions": [
                    {
                        "window_id": "RW0002",
                        "status": "resolved",
                        "source_utterance_ids": ["G002-U001"],
                    },
                    {
                        "window_id": "RW0003",
                        "status": "uncertain",
                        "source_utterance_ids": ["G002-U002"],
                        "notes": "두 후보가 모두 불명확함",
                    },
                ],
                "utterances": [
                    {
                        "utterance_id": "G002-U001",
                        "start": 5.8,
                        "end": 6.8,
                        "language": "en",
                        "original_text": "This way please",
                        "translations": {"ko": "이쪽으로 오세요."},
                        "confidence": 0.88,
                        "review_status": "reviewed",
                        "source_evidence_ids": [en_evidence],
                        "source_window_ids": ["RW0002"],
                        "source_candidate_ids": ["APPLE-en-US-T0002"],
                    },
                    {
                        "utterance_id": "G002-U002",
                        "start": 8.1,
                        "end": 8.8,
                        "language": "uncertain",
                        "original_text": "",
                        "translations": {},
                        "confidence": 0.25,
                        "review_status": "reviewed",
                        "notes": "단어를 확정할 수 없음",
                        "source_evidence_ids": [uncertain_evidence],
                        "source_window_ids": ["RW0003"],
                        "source_candidate_ids": ["APPLE-ko-KR-T0003"],
                    },
                ],
                "captions": [
                    {
                        "caption_id": "G002-C001",
                        "start": 5.7,
                        "end": 7.0,
                        "language": "en",
                        "display_text": "This way, please.",
                        "edit_type": "normalized",
                        "confidence": 0.88,
                        "review_status": "verified",
                        "source_utterance_ids": ["G002-U001"],
                        "source_window_ids": ["RW0002"],
                        "source_candidate_ids": ["APPLE-en-US-T0002"],
                    }
                ],
            },
        ],
    }


def _visual_packet(tmp_path: Path) -> Path:
    context_dir = tmp_path / "context"
    storyboards = context_dir / "storyboards"
    storyboards.mkdir(parents=True)
    for group_id in ("G001", "G002"):
        (storyboards / f"{group_id}.jpg").write_bytes(b"storyboard")
    packet = {
        "schema_version": "phase1-context-packet/v1",
        "asset_id": "clip--abc123",
        "groups": [
            {
                "group_id": "G001",
                "label": "표 확인",
                "prior_summary": "표를 확인한다.",
                "start": 0.0,
                "end": 6.0,
                "segment_ids": ["S001"],
                "segment_summaries": [
                    {"segment_id": "S001", "summary": "기차표를 확인한다."}
                ],
                "storyboard": "storyboards/G001.jpg",
                "candidate_frames": [
                    {
                        "sample_id": "F0001",
                        "time": 2.0,
                        "timecode": "00:02.000",
                        "frame": str(tmp_path / "frame.jpg"),
                    }
                ],
            },
            {
                "group_id": "G002",
                "label": "플랫폼 이동",
                "prior_summary": "플랫폼으로 이동한다.",
                "start": 6.0,
                "end": 12.0,
                "segment_ids": ["S002"],
                "segment_summaries": [
                    {"segment_id": "S002", "summary": "플랫폼으로 이동한다."}
                ],
                "storyboard": "storyboards/G002.jpg",
                "candidate_frames": [
                    {
                        "sample_id": "F0001",
                        "time": 2.0,
                        "timecode": "00:02.000",
                        "frame": str(tmp_path / "frame.jpg"),
                    }
                ],
            },
        ],
    }
    path = context_dir / "context-review-packet.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    return path


def _boundary_proposals(tmp_path: Path) -> Path:
    payload = {
        "schema_version": BOUNDARY_PROPOSAL_SCHEMA,
        "asset_id": "clip--abc123",
        "source": {
            "name": "clip.mp4",
            "path": "/Volumes/T7/clip.mp4",
            "quick_fingerprint": "abc123",
        },
        "media": {"duration": 12.0},
        "policy": {
            "cluster_tolerance_seconds": 0.55,
            "neighbor_context_seconds": 5.0,
            "default_fine_pass": False,
            "vision_sample_interval_seconds": 0.333333,
        },
        "summary": {"proposal_count": 2},
        "proposals": [
            {
                "proposal_id": "BP0001",
                "timestamp": 4.25,
                "timecode": "00:00:04.250",
                "confidence": 0.82,
                "primary_kind": "visual_change",
                "evidence": [
                    {
                        "timestamp": 4.2,
                        "kind": "visual_change",
                        "confidence": 0.82,
                        "source_id": "FFMPEG-SCENE-0001",
                        "details": {"score": 0.41},
                    }
                ],
            },
            {
                "proposal_id": "BP0002",
                "timestamp": 6.75,
                "timecode": "00:00:06.750",
                "confidence": 0.9,
                "primary_kind": "speech_end",
                "evidence": [
                    {
                        "timestamp": 6.8,
                        "kind": "speech_end",
                        "confidence": 0.9,
                        "source_id": "RW0002:end",
                        "details": {"source": "apple_stt"},
                    }
                ],
            },
        ],
    }
    path = tmp_path / "boundary-proposals.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _visual_moments(tmp_path: Path) -> Path:
    frame_dir = tmp_path / "visual-moment-frames"
    frame_dir.mkdir(exist_ok=True)
    moments = []
    for index, (start, end, timestamp, role, speech_free) in enumerate(
        (
            (1.0, 5.0, 4.0, "visual", True),
            (6.0, 10.0, 8.0, "action", False),
        ),
        start=1,
    ):
        moment_id = f"VM{index:04d}"
        frame = frame_dir / f"{moment_id}.jpg"
        frame.write_bytes(b"frame")
        moments.append(
            {
                "moment_id": moment_id,
                "start": start,
                "end": end,
                "timecode": f"00:0{int(start)}.000-00:{int(end):02d}.000",
                "representative_timestamp": timestamp,
                "representative_timecode": f"00:0{int(timestamp)}.000",
                "representative_sample_id": f"{moment_id}-FRAME",
                "representative_frame": str(frame.resolve()),
                "primary_role": role,
                "roles": [role],
                "score": 0.85,
                "confidence": 0.9,
                "speech_overlap_seconds": 0.0 if speech_free else 1.0,
                "speech_free": speech_free,
                "evidence": [
                    {
                        "source_id": f"VISION-SAMPLE-{index:06d}",
                        "kind": "apple_vision_sample",
                        "timestamp": timestamp,
                        "details": {},
                    }
                ],
                "attributes": {},
            }
        )
    payload = {
        "schema_version": "visual-moment/v1",
        "asset_id": "clip--abc123",
        "source": {
            "name": "clip.mp4",
            "path": "/Volumes/T7/clip.mp4",
            "quick_fingerprint": "abc123",
        },
        "media": {"duration": 12.0},
        "policy": {"speech_is_selection_filter": False},
        "summary": {"moment_count": 2, "speech_free_moment_count": 1},
        "moments": moments,
    }
    path = tmp_path / "visual-moments.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _integrated_review(packet: dict) -> dict:
    review = _review(packet)
    for scene in review["scenes"]:
        group_id = scene["group_id"]
        packet_scene = next(
            item for item in packet["scenes"] if item["group_id"] == group_id
        )
        scene["scene_understanding"] = {
            "label": packet_scene["visual_context"]["label"],
            "narrative_summary": "화면 행동과 대사를 함께 검토했다.",
            "actions": ["확인", "이동"],
            "representative_sample_id": "F0001",
            "notable_moments": [],
            "confidence": 0.9,
            "boundary_notes": [],
        }
    review["scenes"][0]["editorial_beats"] = [
        {
            "beat_id": "G001-B001",
            "beat_type": "mixed",
            "start": 0.0,
            "end": 6.0,
            "title": "표 확인",
            "summary": "표를 확인하며 기차를 타러 가야 한다고 말한다.",
            "source_segment_ids": ["S001"],
            "source_window_ids": ["RW0001"],
            "source_utterance_ids": ["G001-U001"],
            "source_caption_ids": ["G001-C001"],
            "representative_sample_ids": ["F0001"],
            "dialogue_closure": "closed",
            "boundary_adjustment": "none",
            "boundary_reason": "한 문장이 끝나는 그룹 경계다.",
            "confidence": 0.9,
        }
    ]
    review["scenes"][1]["editorial_beats"] = [
        {
            "beat_id": "G002-B001",
            "beat_type": "mixed",
            "start": 5.5,
            "end": 12.0,
            "title": "플랫폼 이동",
            "summary": "영어 안내를 들으며 플랫폼으로 이동한다.",
            "source_segment_ids": ["S002"],
            "source_window_ids": ["RW0002", "RW0003"],
            "source_utterance_ids": ["G002-U001", "G002-U002"],
            "source_caption_ids": ["G002-C001"],
            "representative_sample_ids": ["F0001"],
            "dialogue_closure": "closed",
            "boundary_adjustment": "extend_before",
            "boundary_reason": "첫 음성 window가 거친 그룹 시작보다 앞서 시작한다.",
            "confidence": 0.84,
        }
    ]
    return review


def test_build_validate_and_merge_one_pass_scene_dialogue(tmp_path: Path) -> None:
    timeline_path = tmp_path / "timeline.summarized.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "scene-dialogue.packet.json"
    review_path = tmp_path / "scene-dialogue.review.json"
    output_path = tmp_path / "timeline.dialogue-reviewed.json"
    timeline = _timeline(tmp_path)
    timeline_path.write_text(json.dumps(timeline), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")

    build_scene_dialogue_packet(apple_path, timeline_path, packet_path)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))

    assert packet["schema_version"] == SCENE_DIALOGUE_PACKET_SCHEMA
    assert [scene["window_ids"] for scene in packet["scenes"]] == [
        ["RW0001"],
        ["RW0002", "RW0003"],
    ]
    assert (
        packet["scenes"][0]["visual_context"]["segments"][0]["visual_summary"]
        == "기차표를 확인한다."
    )
    assert packet["scenes"][0]["visual_context"]["samples"][0]["sample_id"] == "F0001"
    assert packet["summary"]["evidence_span_count"] == 4
    assert packet["fresh_reconciliation"]["summary"]["missing_candidate_ids"] == []
    assert packet["review_contract"]["caption"]["coverage"].startswith("every")
    assert "dialogue_summary" not in packet["scenes"][0]["visual_context"]
    assert "scene_context" not in packet["scenes"][0]["windows"][0]

    review = _review(packet)
    validate_scene_dialogue_review(packet, review)
    review_path.write_text(json.dumps(review), encoding="utf-8")
    merge_scene_dialogue_review(timeline_path, packet_path, review_path, output_path)
    merged = json.loads(output_path.read_text(encoding="utf-8"))

    assert merged["schema_version"] == timeline["schema_version"]
    assert merged["keep_me"] == timeline["keep_me"]
    assert "reviewed_dialogue" not in timeline
    assert merged["reviewed_dialogue"]["schema_version"] == REVIEWED_DIALOGUE_SCHEMA
    assert merged["reviewed_dialogue"]["summary"] == {
        "scene_count": 2,
        "window_count": 3,
        "utterance_count": 3,
        "caption_count": 2,
        "uncertain_utterance_count": 1,
    }
    caption = merged["reviewed_dialogue"]["captions"][0]
    assert caption["display_text"] == "기차를 타러 가야 해."
    assert caption["source_window_ids"] == ["RW0001"]
    assert caption["review_status"] == "reviewed"
    assert (
        merged["context_groups"][1]["reviewed_dialogue"]["captions"][0]["caption_id"]
        == "G002-C001"
    )
    crossing = next(
        item
        for item in merged["context_groups"][0]["reviewed_dialogue"]["utterances"]
        if item["utterance_id"] == "G002-U001"
    )
    assert crossing["source_start"] == 5.8
    assert crossing["overlap_end"] == 6.0
    assert merged["segments"][0]["caption_lines"][0]["source_start"] == 1.1

    web_path = render_timeline_web(output_path, tmp_path / "web", frame_limit=4)
    document = web_path.read_text(encoding="utf-8")
    assert "검수 자막 대본" in document
    assert "기차를 타러 가야 해." in document
    assert "기차를 타러 가야 한다고 말한다." not in document
    assert "영어 안내가 들린다." not in document


def test_integrated_visual_dialogue_review_emits_valid_editorial_beats(
    tmp_path: Path,
) -> None:
    timeline_path = tmp_path / "timeline.summarized.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "scene-dialogue.integrated.packet.json"
    review_path = tmp_path / "scene-dialogue.integrated.review.json"
    output_path = tmp_path / "timeline.integrated-reviewed.json"
    quick_timeline = _quick_timeline(tmp_path)
    quick_timeline["reviewed_groups"][0]["context_review"] = {
        "key_moments": [{"sample_id": "F0001", "role": "ticket"}]
    }
    timeline_path.write_text(json.dumps(quick_timeline), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")

    build_scene_dialogue_packet(
        apple_path,
        timeline_path,
        packet_path,
        visual_packet_path=_visual_packet(tmp_path),
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["policy"]["integrated_visual_review"] is True
    assert packet["policy"]["editorial_beats_required"] is True
    assert packet["policy"]["preservation_mode"] == ("recall_first_plausible_speech")
    assert packet["policy"]["policy_version"] == DIALOGUE_PRESERVATION_POLICY
    assert packet["policy"]["canonical_for_editing"] is True
    assert packet["policy"]["uncertain_window_does_not_block_captions"] is True
    assert (
        "low ASR confidence"
        in packet["review_contract"]["utterance"]["preservation_rule"]
    )
    assert packet["scenes"][0]["visual_context"]["review_stage"] == (
        "quick_group_plus_dense_storyboard"
    )
    assert packet["scenes"][0]["visual_context"]["storyboard"].endswith(
        "storyboards/G001.jpg"
    )
    assert packet["scenes"][1]["boundary_context"]["crossing_windows"] == [
        {
            "window_id": "RW0002",
            "start": 5.5,
            "end": 7.0,
            "assigned_group_id": "G002",
        }
    ]

    review = _integrated_review(packet)
    validate_scene_dialogue_review(packet, review)
    review_path.write_text(json.dumps(review), encoding="utf-8")
    merge_scene_dialogue_review(timeline_path, packet_path, review_path, output_path)
    merged = json.loads(output_path.read_text(encoding="utf-8"))
    assert merged["schema_version"] == "phase1-context-reviewed-timeline/v1"
    assert merged["reviewed_dialogue"]["summary"]["editorial_beat_count"] == 2
    assert merged["reviewed_dialogue"]["policy_audit"] == {
        "policy_version": DIALOGUE_PRESERVATION_POLICY,
        "status": "pass",
        "window_status_counts": {
            "non_speech": 0,
            "resolved": 2,
            "uncertain": 1,
        },
        "lexical_utterance_count": 2,
        "captioned_lexical_utterance_count": 2,
        "uncaptioned_lexical_utterance_ids": [],
        "caption_coverage_ratio": 1.0,
        "uncertain_windows_with_captioned_speech": 0,
        "uncertain_residue_utterance_count": 1,
    }
    assert len(merged["reviewed_dialogue"]["editorial_beats"]) == 2
    assert (
        merged["context_groups"][1]["editorial_beats"][0]["boundary_adjustment"]
        == "extend_before"
    )
    assert (
        merged["context_groups"][0]["context_review"]["representative_sample_id"]
        == "F0001"
    )
    assert merged["context_groups"][0]["context_review"]["key_moments"] == [
        {"sample_id": "F0001", "role": "ticket"}
    ]

    invalid = _integrated_review(packet)
    invalid["scenes"][1]["editorial_beats"][0]["start"] = 6.0
    with pytest.raises(ValueError, match="cuts utterance"):
        validate_scene_dialogue_review(packet, invalid)


def test_boundary_proposals_anchor_non_grid_editorial_beats(tmp_path: Path) -> None:
    timeline_path = tmp_path / "timeline.summarized.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "scene-dialogue.integrated.packet.json"
    review_path = tmp_path / "scene-dialogue.integrated.review.json"
    output_path = tmp_path / "timeline.integrated-reviewed.json"
    timeline_path.write_text(json.dumps(_quick_timeline(tmp_path)), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")

    build_scene_dialogue_packet(
        apple_path,
        timeline_path,
        packet_path,
        visual_packet_path=_visual_packet(tmp_path),
        boundary_proposals_path=_boundary_proposals(tmp_path),
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["policy"]["boundary_proposals_available"] is True
    assert packet["summary"]["boundary_proposal_count"] == 2
    assert [
        item["proposal_id"]
        for item in packet["scenes"][0]["boundary_context"][
            "candidate_proposals"
        ]
    ] == ["BP0001"]
    assert (
        packet["scenes"][0]["boundary_context"]["next_proposal"]["proposal_id"]
        == "BP0002"
    )

    review = _integrated_review(packet)
    for scene in review["scenes"]:
        for beat in scene["editorial_beats"]:
            beat["source_boundary_proposal_ids"] = []
    first_beat = review["scenes"][0]["editorial_beats"][0]
    first_beat["end"] = 4.25
    first_beat["source_boundary_proposal_ids"] = ["BP0001"]
    review["scenes"][0]["editorial_beats"].append(
        {
            "beat_id": "G001-B002",
            "beat_type": "visual",
            "start": 4.25,
            "end": 6.0,
            "title": "이동 준비",
            "summary": "표 확인을 마치고 이동을 준비한다.",
            "source_segment_ids": ["S001"],
            "source_window_ids": [],
            "source_utterance_ids": [],
            "source_caption_ids": [],
            "representative_sample_ids": ["F0001"],
            "dialogue_closure": "not_applicable",
            "boundary_adjustment": "none",
            "boundary_reason": "화면 변화 후보에서 행동이 전환된다.",
            "confidence": 0.82,
            "source_boundary_proposal_ids": ["BP0001"],
        }
    )
    validate_scene_dialogue_review(packet, review)

    review_path.write_text(json.dumps(review), encoding="utf-8")
    merge_scene_dialogue_review(timeline_path, packet_path, review_path, output_path)
    merged = json.loads(output_path.read_text(encoding="utf-8"))
    merged_beat = merged["reviewed_dialogue"]["editorial_beats"][0]
    assert merged_beat["end"] == 4.25
    assert merged_beat["source_boundary_proposal_ids"] == ["BP0001"]
    assert merged["reviewed_dialogue"]["inputs"]["boundary_proposals"].endswith(
        "boundary-proposals.json"
    )

    missing_citation = copy.deepcopy(review)
    missing_citation["scenes"][0]["editorial_beats"][0][
        "source_boundary_proposal_ids"
    ] = []
    with pytest.raises(ValueError, match="must cite the proposal"):
        validate_scene_dialogue_review(packet, missing_citation)


def test_boundary_proposals_must_match_timeline_lineage(tmp_path: Path) -> None:
    timeline_path = tmp_path / "timeline.summarized.json"
    apple_path = tmp_path / "transcript.apple.json"
    proposals_path = _boundary_proposals(tmp_path)
    timeline_path.write_text(json.dumps(_quick_timeline(tmp_path)), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")
    payload = json.loads(proposals_path.read_text(encoding="utf-8"))
    payload["source"]["quick_fingerprint"] = "wrong"
    proposals_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="fingerprint does not match"):
        build_scene_dialogue_packet(
            apple_path,
            timeline_path,
            tmp_path / "packet.json",
            visual_packet_path=_visual_packet(tmp_path),
            boundary_proposals_path=proposals_path,
        )


def test_boundary_proposal_validator_and_scene_slice(tmp_path: Path) -> None:
    timeline_path = tmp_path / "timeline.summarized.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "packet.json"
    slice_path = tmp_path / "packet.part.json"
    timeline_path.write_text(json.dumps(_quick_timeline(tmp_path)), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")
    proposals_path = _boundary_proposals(tmp_path)

    validate_boundary_proposals(proposals_path, timeline_path)
    build_scene_dialogue_packet(
        apple_path,
        timeline_path,
        packet_path,
        visual_packet_path=_visual_packet(tmp_path),
        boundary_proposals_path=proposals_path,
    )
    slice_scene_dialogue_packet(packet_path, ["G001"], slice_path)
    sliced = json.loads(slice_path.read_text(encoding="utf-8"))
    assert sliced["summary"]["boundary_proposal_count"] == 2
    assert sliced["boundary_evidence"]["proposal_count"] == 2

    payload = json.loads(proposals_path.read_text(encoding="utf-8"))
    payload["proposals"][0]["timecode"] = "00:00:05.000"
    proposals_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="timecode does not match"):
        validate_boundary_proposals(proposals_path, timeline_path)


def test_scene_slice_with_only_neighbor_boundary_proposal_is_valid(
    tmp_path: Path,
) -> None:
    timeline_path = tmp_path / "timeline.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "packet.json"
    slice_path = tmp_path / "packet.part.json"
    timeline_path.write_text(json.dumps(_quick_timeline(tmp_path)), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")
    proposals_path = _boundary_proposals(tmp_path)
    payload = json.loads(proposals_path.read_text(encoding="utf-8"))
    payload["proposals"] = payload["proposals"][:1]
    payload["summary"]["proposal_count"] = 1
    proposals_path.write_text(json.dumps(payload), encoding="utf-8")

    build_scene_dialogue_packet(
        apple_path,
        timeline_path,
        packet_path,
        visual_packet_path=_visual_packet(tmp_path),
        boundary_proposals_path=proposals_path,
    )
    slice_scene_dialogue_packet(packet_path, ["G002"], slice_path)
    sliced = json.loads(slice_path.read_text(encoding="utf-8"))

    assert sliced["scenes"][0]["boundary_context"]["candidate_proposals"] == []
    assert (
        sliced["scenes"][0]["boundary_context"]["previous_proposal"]["proposal_id"]
        == "BP0001"
    )
    assert sliced["policy"]["boundary_proposals_available"] is True
    assert sliced["summary"]["boundary_proposal_count"] == 1
    assert sliced["boundary_evidence"]["proposal_count"] == 1


def test_visual_moments_are_added_to_group_evidence_and_require_beat_coverage(
    tmp_path: Path,
) -> None:
    timeline_path = tmp_path / "timeline.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "packet.json"
    timeline_path.write_text(json.dumps(_quick_timeline(tmp_path)), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")

    build_scene_dialogue_packet(
        apple_path,
        timeline_path,
        packet_path,
        visual_packet_path=_visual_packet(tmp_path),
        visual_moments_path=_visual_moments(tmp_path),
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["summary"]["visual_moment_count"] == 2
    assert packet["summary"]["speech_free_visual_moment_count"] == 1
    assert packet["scenes"][0]["visual_context"]["visual_moments"][0][
        "moment_id"
    ] == "VM0001"
    assert packet["scenes"][0]["visual_context"]["candidate_frames"][-1][
        "sample_id"
    ] == "VM0001-FRAME"

    review = _integrated_review(packet)
    for scene, moment_id in zip(review["scenes"], ("VM0001", "VM0002"), strict=True):
        beat = scene["editorial_beats"][0]
        beat["source_visual_moment_ids"] = [moment_id]
        beat["representative_sample_ids"].append(f"{moment_id}-FRAME")
    validate_scene_dialogue_review(packet, review)

    review["scenes"][0]["editorial_beats"][0]["source_visual_moment_ids"] = []
    with pytest.raises(ValueError, match="exactly cover visual moments"):
        validate_scene_dialogue_review(packet, review)


def test_scene_slice_recomputes_visual_moment_availability(tmp_path: Path) -> None:
    timeline_path = tmp_path / "timeline.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "packet.json"
    slice_path = tmp_path / "packet.part.json"
    timeline_path.write_text(json.dumps(_quick_timeline(tmp_path)), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")
    moments_path = _visual_moments(tmp_path)
    moments = json.loads(moments_path.read_text(encoding="utf-8"))
    moments["moments"] = moments["moments"][:1]
    moments["summary"] = {"moment_count": 1, "speech_free_moment_count": 1}
    moments_path.write_text(json.dumps(moments), encoding="utf-8")

    build_scene_dialogue_packet(
        apple_path,
        timeline_path,
        packet_path,
        visual_packet_path=_visual_packet(tmp_path),
        visual_moments_path=moments_path,
    )
    slice_scene_dialogue_packet(packet_path, ["G002"], slice_path)
    sliced = json.loads(slice_path.read_text(encoding="utf-8"))

    assert sliced["policy"]["visual_moments_supplied"] is True
    assert sliced["policy"]["visual_moments_available"] is False
    assert sliced["summary"]["visual_moment_count"] == 0
    assert "source_visual_moment_ids" not in sliced["review_contract"][
        "editorial_beat"
    ]["required"]


def test_empty_boundary_proposal_input_keeps_integrated_review_compatible(
    tmp_path: Path,
) -> None:
    timeline_path = tmp_path / "timeline.summarized.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "packet.json"
    timeline_path.write_text(json.dumps(_quick_timeline(tmp_path)), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")
    proposals_path = _boundary_proposals(tmp_path)
    payload = json.loads(proposals_path.read_text(encoding="utf-8"))
    payload["proposals"] = []
    payload["summary"]["proposal_count"] = 0
    proposals_path.write_text(json.dumps(payload), encoding="utf-8")

    build_scene_dialogue_packet(
        apple_path,
        timeline_path,
        packet_path,
        visual_packet_path=_visual_packet(tmp_path),
        boundary_proposals_path=proposals_path,
    )
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["policy"]["boundary_proposals_supplied"] is True
    assert packet["policy"]["boundary_proposals_available"] is False
    validate_scene_dialogue_review(packet, _integrated_review(packet))


def test_integrated_visual_packet_must_preserve_sample_coordinates(
    tmp_path: Path,
) -> None:
    timeline_path = tmp_path / "timeline.json"
    apple_path = tmp_path / "transcript.apple.json"
    visual_path = _visual_packet(tmp_path)
    timeline_path.write_text(json.dumps(_quick_timeline(tmp_path)), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")
    visual = json.loads(visual_path.read_text(encoding="utf-8"))
    visual["groups"][0]["candidate_frames"][0]["time"] = 3.0
    visual_path.write_text(json.dumps(visual), encoding="utf-8")

    with pytest.raises(ValueError, match="changed sample F0001 time"):
        build_scene_dialogue_packet(
            apple_path,
            timeline_path,
            tmp_path / "packet.json",
            visual_packet_path=visual_path,
        )


def test_caption_must_overlap_every_cited_utterance(tmp_path: Path) -> None:
    timeline_path = tmp_path / "timeline.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "packet.json"
    timeline_path.write_text(json.dumps(_timeline(tmp_path)), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")
    build_scene_dialogue_packet(apple_path, timeline_path, packet_path)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    review = _review(packet)
    scene = review["scenes"][1]
    scene["utterances"][1].update(
        {"language": "en", "original_text": "Second phrase", "notes": ""}
    )
    scene["captions"] = [
        {
            "caption_id": "G002-C001",
            "start": 7.2,
            "end": 7.5,
            "language": "en",
            "display_text": "This way, please. Second phrase.",
            "edit_type": "normalized",
            "confidence": 0.8,
            "review_status": "reviewed",
            "source_utterance_ids": ["G002-U001", "G002-U002"],
            "source_window_ids": ["RW0002", "RW0003"],
            "source_candidate_ids": [
                "APPLE-en-US-T0002",
                "APPLE-ko-KR-T0003",
            ],
        }
    ]

    with pytest.raises(ValueError, match="overlap every cited source utterance"):
        validate_scene_dialogue_review(packet, review)


def test_scene_dialogue_validator_rejects_untraceable_or_uncaptioned_text(
    tmp_path: Path,
) -> None:
    timeline_path = tmp_path / "timeline.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "packet.json"
    timeline_path.write_text(json.dumps(_timeline(tmp_path)), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")
    build_scene_dialogue_packet(apple_path, timeline_path, packet_path)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))

    invalid_evidence = _review(packet)
    invalid_evidence["scenes"][0]["utterances"][0]["source_evidence_ids"] = [
        "INVENTED-EVIDENCE"
    ]
    with pytest.raises(ValueError, match="invalid evidence IDs"):
        validate_scene_dialogue_review(packet, invalid_evidence)

    missing_caption = _review(packet)
    missing_caption["scenes"][0]["captions"] = []
    with pytest.raises(ValueError, match="without captions"):
        validate_scene_dialogue_review(packet, missing_caption)

    missing_window = _review(packet)
    missing_window["scenes"][1]["window_decisions"].pop()
    with pytest.raises(ValueError, match="exactly cover"):
        validate_scene_dialogue_review(packet, missing_window)


def test_uncertain_window_can_preserve_caption_ready_turns(tmp_path: Path) -> None:
    timeline_path = tmp_path / "timeline.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "packet.json"
    review_path = tmp_path / "review.json"
    output_path = tmp_path / "timeline.dialogue-reviewed.json"
    timeline_path.write_text(json.dumps(_timeline(tmp_path)), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")
    build_scene_dialogue_packet(apple_path, timeline_path, packet_path)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))

    review = _review(packet)
    decision = review["scenes"][1]["window_decisions"][0]
    decision["status"] = "uncertain"
    decision["notes"] = (
        "창 일부는 불명확하지만 확인 가능한 영어 안내는 자막으로 보존한다."
    )

    validate_scene_dialogue_review(packet, review)
    review_path.write_text(json.dumps(review), encoding="utf-8")
    merge_scene_dialogue_review(timeline_path, packet_path, review_path, output_path)
    merged = json.loads(output_path.read_text(encoding="utf-8"))
    audit = merged["reviewed_dialogue"]["policy_audit"]
    assert audit["status"] == "pass"
    assert audit["caption_coverage_ratio"] == 1.0
    assert audit["uncertain_windows_with_captioned_speech"] == 1


def test_scene_dialogue_cli_commands_are_exposed() -> None:
    parser = build_parser()
    assert (
        parser.parse_args(
            [
                "build-scene-dialogue-review-packet",
                "transcript.apple.json",
                "timeline.json",
                "--visual-packet",
                "context-packet.json",
                "--boundary-proposals",
                "boundary-proposals.json",
                "--visual-moments",
                "visual-moments.json",
                "--output",
                "packet.json",
            ]
        ).boundary_proposals
        == Path("boundary-proposals.json")
    )
    assert (
        parser.parse_args(
            [
                "validate-visual-moments",
                "visual-moments.json",
                "timeline.json",
            ]
        ).command
        == "validate-visual-moments"
    )
    assert (
        parser.parse_args(
            [
                "build-scene-dialogue-review-packet",
                "transcript.apple.json",
                "timeline.json",
                "--output",
                "packet.json",
            ]
        ).command
        == "build-scene-dialogue-review-packet"
    )
    assert (
        parser.parse_args(
            [
                "validate-boundary-proposals",
                "boundary-proposals.json",
                "timeline.json",
            ]
        ).command
        == "validate-boundary-proposals"
    )
    assert (
        parser.parse_args(
            ["validate-scene-dialogue-review", "packet.json", "review.json"]
        ).command
        == "validate-scene-dialogue-review"
    )
    assert (
        parser.parse_args(
            [
                "build-no-candidate-scene-dialogue-review",
                "packet.json",
                "--output",
                "review.json",
            ]
        ).command
        == "build-no-candidate-scene-dialogue-review"
    )
    assert (
        parser.parse_args(
            [
                "slice-scene-dialogue-review-packet",
                "packet.json",
                "--groups",
                "G001,G002",
                "--output",
                "slice.json",
            ]
        ).command
        == "slice-scene-dialogue-review-packet"
    )
    assert (
        parser.parse_args(
            [
                "merge-scene-dialogue-review-shards",
                "packet.json",
                "review-1.json",
                "review-2.json",
                "--output",
                "review.json",
            ]
        ).command
        == "merge-scene-dialogue-review-shards"
    )
    assert (
        parser.parse_args(
            [
                "merge-scene-dialogue-review",
                "timeline.json",
                "packet.json",
                "review.json",
                "--output",
                "merged.json",
            ]
        ).command
        == "merge-scene-dialogue-review"
    )


def test_empty_apple_events_produce_mergeable_empty_review(tmp_path: Path) -> None:
    timeline_path = tmp_path / "timeline.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "packet.json"
    review_path = tmp_path / "review.json"
    output_path = tmp_path / "timeline.reviewed.json"
    timeline_path.write_text(json.dumps(_timeline(tmp_path)), encoding="utf-8")
    apple = _apple_transcript()
    apple["activity_intervals"] = []
    apple["candidates"] = [
        {
            "utterance_id": "APPLE-en-US-T-EMPTY",
            "requested_locale": "en-US",
            "start": 0.0,
            "end": 0.0,
            "text": "",
            "spans": [],
        }
    ]
    apple_path.write_text(json.dumps(apple), encoding="utf-8")

    build_scene_dialogue_packet(apple_path, timeline_path, packet_path)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert [scene["window_ids"] for scene in packet["scenes"]] == [[], []]
    assert packet["fresh_reconciliation"]["summary"]["ignored_empty_candidate_ids"] == [
        "APPLE-en-US-T-EMPTY"
    ]

    build_no_candidate_scene_dialogue_review(packet_path, review_path)
    merge_scene_dialogue_review(timeline_path, packet_path, review_path, output_path)
    merged = json.loads(output_path.read_text(encoding="utf-8"))
    assert merged["reviewed_dialogue"]["captions"] == []
    assert merged["reviewed_dialogue"]["summary"]["window_count"] == 0


def test_merge_refuses_to_overwrite_an_input(tmp_path: Path) -> None:
    packet = {"schema_version": SCENE_DIALOGUE_PACKET_SCHEMA}
    for path in (
        tmp_path / "timeline.json",
        tmp_path / "packet.json",
        tmp_path / "review.json",
    ):
        path.write_text(json.dumps(packet), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a new file"):
        merge_scene_dialogue_review(
            tmp_path / "timeline.json",
            tmp_path / "packet.json",
            tmp_path / "review.json",
            tmp_path / "timeline.json",
        )


def test_slice_and_merge_scene_dialogue_review_shards(tmp_path: Path) -> None:
    timeline_path = tmp_path / "timeline.json"
    apple_path = tmp_path / "transcript.apple.json"
    packet_path = tmp_path / "packet.json"
    slice_one_path = tmp_path / "packet.g001.json"
    slice_two_path = tmp_path / "packet.g002.json"
    review_one_path = tmp_path / "review.g001.json"
    review_two_path = tmp_path / "review.g002.json"
    merged_review_path = tmp_path / "review.merged.json"
    timeline_path.write_text(json.dumps(_timeline(tmp_path)), encoding="utf-8")
    apple_path.write_text(json.dumps(_apple_transcript()), encoding="utf-8")
    build_scene_dialogue_packet(apple_path, timeline_path, packet_path)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    complete_review = _review(packet)

    slice_scene_dialogue_packet(packet_path, ["G001"], slice_one_path)
    slice_scene_dialogue_packet(packet_path, ["G002"], slice_two_path)
    assert (
        json.loads(slice_one_path.read_text(encoding="utf-8"))["summary"]["scene_count"]
        == 1
    )

    review_one = {
        **complete_review,
        "reviewer": "agent-one",
        "scenes": complete_review["scenes"][:1],
    }
    review_two = {
        **complete_review,
        "reviewer": "agent-two",
        "scenes": complete_review["scenes"][1:],
    }
    # Model-authored shards may restart IDs locally. The merger must namespace
    # them by scene before validating the full review.
    scene_two = review_two["scenes"][0]
    local_ids = {
        utterance["utterance_id"]: utterance["utterance_id"].removeprefix("G002-")
        for utterance in scene_two["utterances"]
    }
    for utterance in scene_two["utterances"]:
        utterance["utterance_id"] = local_ids[utterance["utterance_id"]]
    for decision in scene_two["window_decisions"]:
        decision["source_utterance_ids"] = [
            local_ids[item] for item in decision["source_utterance_ids"]
        ]
    for caption in scene_two["captions"]:
        caption["caption_id"] = caption["caption_id"].removeprefix("G002-")
        caption["source_utterance_ids"] = [
            local_ids[item] for item in caption["source_utterance_ids"]
        ]
    review_one_path.write_text(json.dumps(review_one), encoding="utf-8")
    review_two_path.write_text(json.dumps(review_two), encoding="utf-8")
    merge_scene_dialogue_review_shards(
        packet_path,
        [review_two_path, review_one_path],
        merged_review_path,
    )
    merged = json.loads(merged_review_path.read_text(encoding="utf-8"))
    assert [scene["group_id"] for scene in merged["scenes"]] == ["G001", "G002"]
    assert merged["reviewer"] == "agent-two + agent-one"
    assert all(
        item["utterance_id"].startswith("G002-")
        for item in merged["scenes"][1]["utterances"]
    )
    validate_scene_dialogue_review(packet, merged)

    with pytest.raises(ValueError, match="do not cover the full packet"):
        merge_scene_dialogue_review_shards(
            packet_path,
            [review_one_path],
            tmp_path / "review.incomplete.json",
        )
