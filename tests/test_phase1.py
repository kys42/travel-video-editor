import json
from pathlib import Path

import pytest

from travel_video.apple_speech import (
    derive_activity_ranges,
    normalize_apple_transcripts,
)
from travel_video.context import select_storyboard_samples, validate_context_review
from travel_video.phase1 import (
    Phase1Config,
    attach_transcript_candidates,
    build_machine_groups,
    build_segments,
    filter_transcript,
    format_time,
    validate_coverage,
)
from travel_video.review import validate_review
from travel_video.speech import (
    AdaptiveSTTConfig,
    build_vad_chunks,
    decide_language_route,
    expand_contextual_fallbacks,
    resolve_candidate_language,
)
from travel_video.transcript_reconcile import validate_reconciliation_review
from travel_video.web import render_timeline_web


def sample(index: int, time: float, change: float, visual_hash: str) -> dict:
    return {
        "sample_id": f"F{index:04d}",
        "time": time,
        "timecode": format_time(time),
        "frame": str(Path("frames") / f"{index}.jpg"),
        "brightness": 0.5,
        "contrast": 0.2,
        "sharpness": 0.1,
        "visual_change": change,
        "average_hash": visual_hash,
        "quality": 0.8,
        "flags": [],
    }


def test_format_time() -> None:
    assert format_time(0) == "00:00.000"
    assert format_time(65.432) == "01:05.432"
    assert format_time(3661.2) == "01:01:01.200"


def test_apple_transcripts_preserve_locale_candidates_and_confidence() -> None:
    raw = {
        "ko-KR": {
            "selected_locale": "ko_KR",
            "transcripts": [
                {
                    "start": 1.0,
                    "end": 2.2,
                    "text": " 만다린 주세요 ",
                    "alternatives": ["만다린으로 주세요"],
                    "spans": [
                        {
                            "start": 1.0,
                            "end": 1.6,
                            "text": " 만다린",
                            "confidence": 0.8,
                        },
                        {
                            "start": 1.6,
                            "end": 2.2,
                            "text": " 주세요",
                            "confidence": 0.6,
                        },
                    ],
                    "is_final": True,
                }
            ],
        },
        "en-US": {
            "selected_locale": "en_US",
            "transcripts": [
                {
                    "start": 1.1,
                    "end": 2.1,
                    "text": "Mandarin, please.",
                    "alternatives": [],
                    "spans": [],
                    "is_final": True,
                }
            ],
        },
    }
    candidates = normalize_apple_transcripts(raw)
    assert [item["requested_locale"] for item in candidates] == ["ko-KR", "en-US"]
    assert candidates[0]["text"] == "만다린 주세요"
    assert candidates[0]["mean_confidence"] == 0.7
    assert candidates[1]["text"] == "Mandarin, please."


def test_apple_activity_ranges_are_labeled_as_derived_detector_evidence() -> None:
    candidates = [
        {"start": 1.0, "end": 2.0, "text": "hello"},
        {"start": 1.1, "end": 2.1, "text": "안녕"},
        {"start": 4.0, "end": 5.0, "text": "again"},
    ]
    activity = derive_activity_ranges(
        candidates,
        duration=6.0,
        padding=0.1,
        merge_gap=0.2,
    )
    assert [(item["start"], item["end"]) for item in activity] == [
        (0.9, 2.2),
        (3.9, 5.1),
    ]
    assert all(
        item["source"] == "detector_gated_transcriber_time_union"
        for item in activity
    )


def test_transcript_reconciliation_requires_exact_windows_and_source_ids() -> None:
    packet = {
        "windows": [
            {
                "window_id": "RW0001",
                "start": 1.0,
                "end": 3.0,
                "apple_candidates": {
                    "en-US": {"source_candidate_ids": ["APPLE-en-US-T0001"]}
                },
            }
        ]
    }
    review = {
        "schema_version": "transcript-reconciliation-review/v1",
        "windows": [
            {
                "window_id": "RW0001",
                "utterances": [
                    {
                        "start": 1.2,
                        "end": 2.8,
                        "language": "en",
                        "original_text": "Can I get one Coke?",
                        "translations": {"ko": "콜라 하나 주세요."},
                        "source_candidate_ids": ["APPLE-en-US-T0001"],
                    }
                ],
            }
        ],
    }
    validate_reconciliation_review(packet, review)
    review["windows"][0]["utterances"][0]["source_candidate_ids"] = ["invented"]
    with pytest.raises(ValueError, match="source candidate IDs"):
        validate_reconciliation_review(packet, review)


def test_segments_cover_asset_without_gaps() -> None:
    config = Phase1Config(
        sample_interval=5, min_segment=5, max_segment=15, change_threshold=0.2
    )
    samples = [
        sample(1, 0, 0.0, "0000000000000000"),
        sample(2, 5, 0.05, "0000000000000000"),
        sample(3, 10, 0.30, "ffffffffffffffff"),
        sample(4, 15, 0.05, "ffffffffffffffff"),
        sample(5, 20, 0.05, "ffffffffffffffff"),
        sample(6, 25, 0.05, "ffffffffffffffff"),
    ]
    segments = build_segments(samples, 27.0, config)
    validate_coverage(segments, 27.0)
    assert [(item["start"], item["end"]) for item in segments] == [
        (0.0, 10),
        (10, 27.0),
    ]


def test_machine_groups_cover_segments() -> None:
    config = Phase1Config(group_threshold=0.2)
    segments = [
        {
            "segment_id": "S001",
            "start": 0,
            "end": 10,
            "boundary_reason": "asset_start",
            "representative_hash": "0" * 16,
        },
        {
            "segment_id": "S002",
            "start": 10,
            "end": 20,
            "boundary_reason": "max_duration",
            "representative_hash": "0" * 16,
        },
        {
            "segment_id": "S003",
            "start": 20,
            "end": 30,
            "boundary_reason": "visual_change",
            "representative_hash": "f" * 16,
        },
    ]
    groups = build_machine_groups(segments, config)
    assert [group["segment_ids"] for group in groups] == [["S001", "S002"], ["S003"]]


def test_review_requires_exact_contiguous_coverage() -> None:
    machine = {
        "asset_id": "asset-1",
        "media": {"duration": 20.0},
        "segments": [
            {"segment_id": "S001", "start": 0.0, "end": 10.0},
            {"segment_id": "S002", "start": 10.0, "end": 20.0},
        ],
    }
    review = {
        "schema_version": "phase1-review/v1",
        "asset_id": "asset-1",
        "segments": [
            {"segment_id": "S001", "visual_summary": "first"},
            {"segment_id": "S002", "visual_summary": "second"},
        ],
        "groups": [{"group_id": "G001", "segment_ids": ["S001", "S002"]}],
    }
    validate_review(machine, review)
    review["groups"][0]["segment_ids"] = ["S002", "S001"]
    with pytest.raises(ValueError, match="contiguous"):
        validate_review(machine, review)


def test_transcript_filter_drops_repetition_and_low_confidence() -> None:
    transcript = {
        "segments": [
            {
                "start": 0,
                "end": 2,
                "text": "실제 대화",
                "avg_logprob": -0.4,
                "no_speech_prob": 0.1,
                "compression_ratio": 1.1,
            },
            {
                "start": 2,
                "end": 5,
                "text": "네 네 네 네",
                "avg_logprob": -0.2,
                "no_speech_prob": 0.1,
                "compression_ratio": 8.0,
            },
            {
                "start": 5,
                "end": 7,
                "text": "불명확",
                "avg_logprob": -1.4,
                "no_speech_prob": 0.1,
                "compression_ratio": 1.0,
            },
        ]
    }
    accepted, stats = filter_transcript(transcript)
    assert [item["text"] for item in accepted] == ["실제 대화"]
    assert stats["accepted_segments"] == 1
    assert stats["rejected_segments"] == 2


def test_parallel_transcript_candidates_are_both_preserved() -> None:
    segments = [{"segment_id": "S001", "start": 0.0, "end": 10.0}]
    common = {"start": 1, "end": 4, "no_speech_prob": 0.1, "compression_ratio": 1.0}
    transcripts = {
        "ko": {
            "segments": [
                {**common, "text": "만다린 맛으로 주세요", "avg_logprob": -0.35}
            ]
        },
        "en": {
            "segments": [
                {**common, "text": "Can I get mandarin?", "avg_logprob": -0.45}
            ]
        },
    }
    stats = attach_transcript_candidates(segments, transcripts)
    assert (
        segments[0]["transcript_candidates"]["ko"][0]["text"] == "만다린 맛으로 주세요"
    )
    assert (
        segments[0]["transcript_candidates"]["en"][0]["text"] == "Can I get mandarin?"
    )
    assert segments[0]["transcript_machine_preference"] == "mixed_or_uncertain"
    assert set(stats) == {"ko", "en"}


def test_vad_chunks_preserve_speech_regions_and_bound_long_audio() -> None:
    config = AdaptiveSTTConfig(
        speech_padding=0,
        merge_gap=0,
        min_chunk=1,
        max_chunk=10,
    )
    chunks = build_vad_chunks(
        [{"start": 5, "end": 8}, {"start": 18, "end": 20}],
        duration=40,
        config=config,
    )
    assert [(item["start"], item["end"]) for item in chunks] == [
        (0.0, 5.0),
        (8.0, 18.0),
        (20.0, 30.0),
        (30.0, 40),
    ]


def test_language_route_keeps_confident_result_and_expands_uncertain_result() -> None:
    config = AdaptiveSTTConfig(
        expected_languages=("ko", "en"),
        min_language_probability=0.65,
        min_language_margin=0.2,
    )
    confident = decide_language_route({"ko": 0.88, "en": 0.08, "ja": 0.04}, 12, config)
    assert confident["selected_language"] == "ko"
    assert confident["uncertain"] is False
    assert confident["candidate_languages"] == ["ko"]

    uncertain = decide_language_route({"ko": 0.48, "en": 0.44, "ja": 0.08}, 12, config)
    assert uncertain["uncertain"] is True
    assert uncertain["candidate_languages"] == ["ko", "en"]
    assert set(uncertain["uncertainty_reasons"]) == {
        "low_language_probability",
        "low_language_margin",
    }


def test_contextual_fallback_expands_neighbors_without_cascading() -> None:
    config = AdaptiveSTTConfig(expected_languages=("ko", "en"))
    routes = [
        {
            "selected_language": "ko",
            "uncertain": False,
            "uncertainty_reasons": [],
            "candidate_languages": ["ko"],
        },
        {
            "selected_language": "ko",
            "uncertain": True,
            "uncertainty_reasons": ["low_language_probability"],
            "candidate_languages": ["ko", "en"],
        },
        {
            "selected_language": "ko",
            "uncertain": False,
            "uncertainty_reasons": [],
            "candidate_languages": ["ko"],
        },
        {
            "selected_language": "ko",
            "uncertain": False,
            "uncertainty_reasons": [],
            "candidate_languages": ["ko"],
        },
    ]
    expand_contextual_fallbacks(routes, config)
    assert [route["uncertain"] for route in routes] == [True, True, True, False]
    assert routes[0]["candidate_languages"] == ["ko", "en"]
    assert routes[3]["candidate_languages"] == ["ko"]


def test_candidate_resolution_marks_multiple_usable_languages_for_review() -> None:
    candidates = {
        "ko": {
            "segments": [
                {"accepted": True, "text": "만다린으로 할까", "avg_logprob": -0.5}
            ]
        },
        "en": {
            "segments": [
                {"accepted": True, "text": "Mandarin, please", "avg_logprob": -0.4}
            ]
        },
    }
    resolution = resolve_candidate_language(candidates, "ko")
    assert resolution["spoken_language"] == "mixed_or_uncertain"
    assert resolution["provisional_language"] == "ko"
    assert resolution["needs_reconciliation"] is True

    strong = resolve_candidate_language(
        candidates,
        "ko",
        language_probability=0.98,
        language_margin=0.9,
    )
    assert strong["spoken_language"] == "ko"
    assert strong["needs_reconciliation"] is False

    candidates["ko"]["segments"][0]["accepted"] = False
    resolution = resolve_candidate_language(candidates, "ko")
    assert resolution["spoken_language"] == "en"
    assert resolution["provisional_language"] == "en"
    assert resolution["needs_reconciliation"] is False


def test_storyboard_selection_is_diverse_and_bounded() -> None:
    samples = [sample(index, (index - 1) * 5, 0.05, "0" * 16) for index in range(1, 10)]
    samples[4]["average_hash"] = "f" * 16
    selected = select_storyboard_samples(samples, 0, 45, max_frames=4)
    assert len(selected) == 4
    assert [item["time"] for item in selected] == sorted(
        item["time"] for item in selected
    )
    assert "F0005" in {item["sample_id"] for item in selected}


def test_context_review_representative_must_come_from_storyboard() -> None:
    packet = {
        "schema_version": "phase1-context-packet/v1",
        "asset_id": "asset-1",
        "groups": [
            {
                "group_id": "G001",
                "candidate_frames": [{"sample_id": "F0001"}, {"sample_id": "F0002"}],
            }
        ],
    }
    review = {
        "schema_version": "phase1-context-review/v1",
        "asset_id": "asset-1",
        "groups": [
            {
                "group_id": "G001",
                "narrative_summary": "빙하와 헬기가 보인다",
                "representative_sample_id": "F0002",
                "key_moments": [{"sample_id": "F0001", "role": "도입"}],
                "notable_moments": [
                    {
                        "sample_id": "F0002",
                        "category": "visual",
                        "title": "빙하 위 노란 헬기",
                        "description": "회색 빙하와 노란 기체의 대비가 강하다.",
                        "edit_hint": "장소를 각인시키는 와이드 컷으로 쓴다.",
                    }
                ],
            }
        ],
    }
    validate_context_review(packet, review)
    review["groups"][0]["representative_sample_id"] = "F9999"
    with pytest.raises(ValueError, match="representative"):
        validate_context_review(packet, review)
    review["groups"][0]["representative_sample_id"] = "F0002"
    review["groups"][0]["notable_moments"][0]["sample_id"] = "F9999"
    with pytest.raises(ValueError, match="notable moment sample"):
        validate_context_review(packet, review)


def test_web_timeline_renders_details_without_video(tmp_path: Path) -> None:
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"portable-image")
    timeline = {
        "schema_version": "phase1-context-reviewed-timeline/v1",
        "asset_id": "asset-1",
        "source": {"name": "trip.mp4", "path": str(tmp_path / "missing.mp4")},
        "media": {
            "duration": 12.0,
            "video": {"width": 3840, "height": 2160, "codec": "hevc"},
        },
        "samples": [sample(1, 0, 0.0, "0" * 16) | {"frame": str(frame)}],
        "segments": [
            {
                "segment_id": "S001",
                "start": 0.0,
                "end": 12.0,
                "review": {
                    "visual_summary": "빙하에 도착한다.",
                    "actions": ["도착"],
                },
                "transcript_candidates": {
                    "ko": [
                        {
                            "start": 1.0,
                            "end": 2.0,
                            "text": "도착했다",
                            "avg_logprob": -0.3,
                        }
                    ],
                    "en": [],
                },
            }
        ],
        "context_groups": [
            {
                "group_id": "G001",
                "label": "빙하 도착",
                "start": 0.0,
                "end": 12.0,
                "segment_ids": ["S001"],
                "context_review": {
                    "narrative_summary": "헬기에서 내려 빙하를 둘러본다.",
                    "dialogue_summary": "도착했다고 말한다.",
                    "dialogue_evidence": ["ko"],
                    "representative_sample_id": "F0001",
                    "representative_reason": "빙하 전경이 잘 보인다.",
                    "key_moments": [{"sample_id": "F0001", "role": "도착"}],
                    "notable_moments": [
                        {
                            "sample_id": "F0001",
                            "category": "visual",
                            "title": "빙하 위 노란 헬기",
                            "description": "회색 빙하와 노란 기체의 대비가 강하다.",
                            "edit_hint": "장소를 각인시키는 도입 컷으로 쓴다.",
                        }
                    ],
                    "confidence": 0.9,
                },
            }
        ],
    }
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps(timeline), encoding="utf-8")

    output = render_timeline_web(timeline_path, tmp_path / "web")
    document = output.read_text(encoding="utf-8")
    manifest = json.loads((output.parent / "manifest.json").read_text(encoding="utf-8"))

    assert "헬기에서 내려 빙하를 둘러본다." in document
    assert "도착했다고 말한다." in document
    assert "GLOBAL PREVIEW DECK" in document
    assert "편집 판단 타임라인" in document
    assert "화면 · 행동" in document
    assert "음성 · 대화" in document
    assert "NOTABLE BEATS" in document
    assert "빙하 위 노란 헬기" in document
    assert "장소를 각인시키는 도입 컷으로 쓴다." in document
    assert "data:image/jpeg;base64," in document
    assert "<video" not in document
    assert "__MEDIA_STATUS__" not in document
    assert "__VIDEO_SYNOPSIS__" not in document
    assert manifest["video_mode"] == "none"
    assert manifest["asset_mode"] == "embed"
    assert manifest["embedded_asset_count"] == 1
    assert manifest["notable_moment_count"] == 1

    relative_output = render_timeline_web(
        timeline_path, tmp_path / "web-relative", asset_mode="relative"
    )
    relative_document = relative_output.read_text(encoding="utf-8")
    relative_manifest = json.loads(
        (relative_output.parent / "manifest.json").read_text(encoding="utf-8")
    )
    assert 'src="../frame.jpg"' in relative_document
    assert "data:image/jpeg;base64," not in relative_document
    assert relative_manifest["asset_mode"] == "relative"
    assert relative_manifest["embedded_asset_count"] == 0
