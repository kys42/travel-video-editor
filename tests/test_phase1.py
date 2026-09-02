from pathlib import Path

import pytest

from travel_video.phase1 import (
    Phase1Config,
    build_machine_groups,
    build_segments,
    filter_transcript,
    format_time,
    validate_coverage,
)
from travel_video.review import validate_review


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


def test_segments_cover_asset_without_gaps() -> None:
    config = Phase1Config(sample_interval=5, min_segment=5, max_segment=15, change_threshold=0.2)
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
    assert [(item["start"], item["end"]) for item in segments] == [(0.0, 10), (10, 27.0)]


def test_machine_groups_cover_segments() -> None:
    config = Phase1Config(group_threshold=0.2)
    segments = [
        {"segment_id": "S001", "start": 0, "end": 10, "boundary_reason": "asset_start", "representative_hash": "0" * 16},
        {"segment_id": "S002", "start": 10, "end": 20, "boundary_reason": "max_duration", "representative_hash": "0" * 16},
        {"segment_id": "S003", "start": 20, "end": 30, "boundary_reason": "visual_change", "representative_hash": "f" * 16},
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
            {"start": 0, "end": 2, "text": "실제 대화", "avg_logprob": -0.4, "no_speech_prob": 0.1, "compression_ratio": 1.1},
            {"start": 2, "end": 5, "text": "네 네 네 네", "avg_logprob": -0.2, "no_speech_prob": 0.1, "compression_ratio": 8.0},
            {"start": 5, "end": 7, "text": "불명확", "avg_logprob": -1.4, "no_speech_prob": 0.1, "compression_ratio": 1.0},
        ]
    }
    accepted, stats = filter_transcript(transcript)
    assert [item["text"] for item in accepted] == ["실제 대화"]
    assert stats["accepted_segments"] == 1
    assert stats["rejected_segments"] == 2
