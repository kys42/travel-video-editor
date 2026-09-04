from __future__ import annotations

from pathlib import Path

import pytest

from travel_video.boundary_proposals import (
    BoundaryProposalConfig,
    _parse_ffmpeg_metadata,
    _validate_inputs,
    cluster_boundary_events,
    extract_apple_stt_signals,
    extract_vision_signals,
)
from travel_video.cli import build_parser
from travel_video.phase1 import quick_fingerprint


def _source() -> dict:
    return {
        "path": "/Volumes/ExternalSSD/proxies/clip.mp4",
        "name": "clip.mp4",
        "quick_fingerprint": "abc123",
    }


def _event(timestamp: float, kind: str, confidence: float, source_id: str) -> dict:
    return {
        "timestamp": timestamp,
        "kind": kind,
        "confidence": confidence,
        "source_id": source_id,
        "details": {},
    }


def test_apple_stt_adapter_preserves_activity_and_candidate_sources() -> None:
    transcript = {
        "activity_intervals": [
            {"activity_id": "A1", "start": 1.0, "end": 3.0, "source": "union"},
            {"activity_id": "A2", "start": 5.0, "end": 8.0, "source": "union"},
        ],
        "candidates": [
            {
                "utterance_id": "KO1",
                "requested_locale": "ko-KR",
                "start": 1.0,
                "end": 2.8,
                "mean_confidence": 0.9,
            },
            {
                "utterance_id": "EN1",
                "requested_locale": "en-US",
                "start": 1.0,
                "end": 2.82,
                "mean_confidence": 0.7,
            },
        ],
    }
    result = extract_apple_stt_signals(transcript, source=_source(), duration=10.0)
    kinds = [item["kind"] for item in result["events"]]

    assert kinds.count("speech_start") == 2
    assert kinds.count("speech_end") == 2
    assert kinds.count("silence_gap") == 1
    assert kinds.count("speech_segment_boundary") == 2
    raw_boundaries = [
        item for item in result["events"] if item["kind"] == "speech_segment_boundary"
    ]
    assert {item["source_id"] for item in raw_boundaries} == {"KO1-END", "EN1-END"}
    assert raw_boundaries[0]["details"]["semantics"].startswith("raw_transcriber")


def test_ffmpeg_metadata_parser_pairs_timestamp_and_value() -> None:
    output = """
frame:0 pts:30 pts_time:1
lavfi.scene_score=0.41
frame:1 pts:60 pts_time:2
lavfi.scene_score=0.62
"""
    assert _parse_ffmpeg_metadata(output, "lavfi.scene_score") == [
        (1.0, 0.41),
        (2.0, 0.62),
    ]


def test_vision_adapter_emits_feature_people_quality_and_ocr_changes() -> None:
    raw = {
        "schemaVersion": "apple-vision-boundary-signals/v1",
        "input": _source()["path"],
        "sourceDurationSeconds": 10.0,
        "visualIntervalSeconds": 1 / 3,
        "ocrIntervalSeconds": 1.0,
        "visualSamples": [
            {
                "timestamp": 0.33,
                "featureDistanceFromPrevious": 0.02,
                "aestheticsScore": 0.1,
                "isUtility": False,
                "faceCount": 0,
                "personCount": 0,
            },
            {
                "timestamp": 0.66,
                "featureDistanceFromPrevious": 0.03,
                "aestheticsScore": 0.1,
                "isUtility": False,
                "faceCount": 0,
                "personCount": 0,
            },
            {
                "timestamp": 1.0,
                "featureDistanceFromPrevious": 0.03,
                "aestheticsScore": 0.1,
                "isUtility": False,
                "faceCount": 0,
                "personCount": 0,
            },
            {
                "timestamp": 2.0,
                "featureDistanceFromPrevious": 0.8,
                "aestheticsScore": 0.6,
                "isUtility": True,
                "faceCount": 1,
                "personCount": 1,
            },
            {
                "timestamp": 2.33,
                "featureDistanceFromPrevious": 0.04,
                "aestheticsScore": 0.6,
                "isUtility": True,
                "faceCount": 1,
                "personCount": 1,
            },
            {
                "timestamp": 2.66,
                "featureDistanceFromPrevious": 0.04,
                "aestheticsScore": 0.6,
                "isUtility": True,
                "faceCount": 1,
                "personCount": 1,
            },
        ],
        "ocrSamples": [
            {"timestamp": 1.0, "lines": []},
            {
                "timestamp": 2.0,
                "lines": [{"text": "Glacier Bay", "confidence": 0.95}],
            },
            {
                "timestamp": 3.0,
                "lines": [{"text": "Glacier Bay", "confidence": 0.94}],
            },
        ],
    }
    result = extract_vision_signals(
        raw,
        source=_source(),
        duration=10.0,
        feature_distance_floor=0.12,
        feature_distance_quantile=0.75,
    )
    kinds = {item["kind"] for item in result["events"]}

    assert {
        "visual_change",
        "face_presence_change",
        "person_presence_change",
        "quality_change",
        "ocr_change",
    } <= kinds


def test_clustering_uses_exact_primary_timestamp_and_keeps_short_speech_pair() -> None:
    proposals = cluster_boundary_events(
        [
            _event(4.20, "visual_change", 0.7, "V1"),
            _event(4.25, "hard_cut", 0.8, "F1"),
            _event(7.00, "speech_start", 0.8, "S1"),
            _event(7.25, "speech_end", 0.8, "S2"),
        ],
        duration=10.0,
        tolerance=0.55,
    )

    assert len(proposals) == 3
    assert proposals[0]["timestamp"] == 4.25
    assert proposals[0]["primary_kind"] == "hard_cut"
    assert proposals[1]["primary_kind"] == "speech_start"
    assert proposals[2]["primary_kind"] == "speech_end"


def test_input_validation_requires_proxy_lineage_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_path = tmp_path / "clip.mp4"
    source_path.write_bytes(b"not media but fingerprintable")
    source = {
        "path": str(source_path),
        "quick_fingerprint": quick_fingerprint(source_path),
    }
    timeline = {
        "schema_version": "phase1-reviewed-timeline/v1",
        "source": source,
        "media": {"duration": 10.0},
    }
    transcript = {"schema_version": "apple-stt/v1", "source": source}
    lineage = {
        "schema_version": "travel-video-source-lineage/v1",
        "original": {"path": "/original.mp4", "quick_fingerprint": "original"},
        "processing_input": {"path": "/wrong.mp4", "quick_fingerprint": "wrong"},
        "time_mapping": {"kind": "identity", "source_in_offset_seconds": 0},
    }
    monkeypatch.setattr(
        "travel_video.boundary_proposals.probe_media",
        lambda _: {"duration": 10.0},
    )

    with pytest.raises(ValueError, match="Lineage does not contain"):
        _validate_inputs(source_path, timeline, transcript, lineage)


def test_boundary_cli_defaults_to_three_vision_samples_per_second() -> None:
    args = build_parser().parse_args(
        [
            "build-boundary-proposals",
            "proxy.mp4",
            "timeline.json",
            "transcript.apple.json",
            "--output-dir",
            "boundaries",
        ]
    )

    assert args.vision_interval == pytest.approx(1 / 3)
    assert args.ocr_interval == 1.0
    assert args.motion_interval == pytest.approx(1 / 3)
    assert BoundaryProposalConfig().cluster_tolerance == 0.55
