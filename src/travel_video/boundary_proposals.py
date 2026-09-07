from __future__ import annotations

from .clip_evidence import person_observations

import hashlib
import math
import os
import re
import shutil
import subprocess
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .phase1 import atomic_json, format_time, probe_media, quick_fingerprint
from .review import load_json
from .scene_dialogue import (
    SUPPORTED_TIMELINE_SCHEMAS,
    validate_boundary_proposals,
    validate_visual_moments,
)

SIGNAL_SCHEMA = "local-boundary-signals/v1"
RUN_SCHEMA = "boundary-proposal-run/v1"
VISION_RAW_SCHEMA = "apple-vision-boundary-signals/v1"
BOUNDARY_SCHEMA = "boundary-proposal/v1"
VISUAL_MOMENT_SCHEMA = "visual-moment/v1"
PRODUCER_REVISION = "multimodal-boundary-producer/v2"
EPSILON = 0.001


@dataclass(frozen=True)
class BoundaryProposalConfig:
    vision_interval: float = 1.0 / 3.0
    ocr_interval: float = 1.0
    ffmpeg_scene_threshold: float = 0.30
    motion_interval: float = 1.0 / 3.0
    cluster_tolerance: float = 0.55
    neighbor_context: float = 5.0
    feature_distance_floor: float = 0.12
    feature_distance_quantile: float = 0.90
    motion_delta_quantile: float = 0.92
    visual_moment_window: float = 6.0
    visual_moment_nms: float = 3.0
    visual_moments_per_minute: float = 6.0
    visual_moment_thumbnail_width: int = 960

    def validate(self) -> None:
        positive = {
            "vision_interval": self.vision_interval,
            "ocr_interval": self.ocr_interval,
            "motion_interval": self.motion_interval,
            "cluster_tolerance": self.cluster_tolerance,
            "visual_moment_window": self.visual_moment_window,
            "visual_moment_nms": self.visual_moment_nms,
            "visual_moments_per_minute": self.visual_moments_per_minute,
        }
        for name, value in positive.items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not 0 < self.ffmpeg_scene_threshold <= 1:
            raise ValueError("ffmpeg_scene_threshold must be in (0, 1]")
        if self.neighbor_context < 0:
            raise ValueError("neighbor_context must be non-negative")
        if self.feature_distance_floor < 0:
            raise ValueError("feature_distance_floor must be non-negative")
        if self.visual_moment_thumbnail_width < 160:
            raise ValueError("visual_moment_thumbnail_width must be at least 160")
        for name, value in {
            "feature_distance_quantile": self.feature_distance_quantile,
            "motion_delta_quantile": self.motion_delta_quantile,
        }.items():
            if not 0 < value < 1:
                raise ValueError(f"{name} must be in (0, 1)")


def _normalized_path(value: str | Path) -> str:
    return unicodedata.normalize(
        "NFC", os.path.normpath(os.path.expanduser(str(value)))
    )


def _same_source(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        bool(left.get("path"))
        and bool(right.get("path"))
        and _normalized_path(left["path"]) == _normalized_path(right["path"])
        and bool(left.get("quick_fingerprint"))
        and str(left["quick_fingerprint"]) == str(right.get("quick_fingerprint"))
    )


def _source_record(path: Path) -> dict[str, Any]:
    return {
        "path": _normalized_path(path),
        "name": path.name,
        "quick_fingerprint": quick_fingerprint(path),
    }


def _validate_inputs(
    source: Path,
    timeline: dict[str, Any],
    apple_transcript: dict[str, Any],
    lineage: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not source.is_file():
        raise ValueError(f"Processing input does not exist: {source}")
    if timeline.get("schema_version") not in SUPPORTED_TIMELINE_SCHEMAS:
        raise ValueError("Boundary proposals require a reviewed timeline")
    if apple_transcript.get("schema_version") != "apple-stt/v1":
        raise ValueError("Expected apple-stt/v1 transcript")
    timeline_source = timeline.get("source", {})
    transcript_source = apple_transcript.get("source", {})
    if not _same_source(timeline_source, transcript_source):
        raise ValueError("Apple transcript source does not match timeline")

    actual_source = _source_record(source)
    if not _same_source(timeline_source, actual_source):
        raise ValueError("Processing input source does not match timeline")
    media = probe_media(source)
    duration = float(timeline.get("media", {}).get("duration", 0))
    if duration <= 0 or abs(float(media["duration"]) - duration) > 0.25:
        raise ValueError("Processing input duration does not match timeline")

    if lineage is not None:
        if lineage.get("schema_version") != "travel-video-source-lineage/v1":
            raise ValueError("Expected travel-video-source-lineage/v1")
        lineage_sources = [
            lineage.get("original", {}),
            lineage.get("processing_input", {}),
        ]
        if not any(_same_source(actual_source, item) for item in lineage_sources):
            raise ValueError("Lineage does not contain the processing input")
        mapping = lineage.get("time_mapping", {})
        if mapping.get("kind") != "identity":
            raise ValueError("Only identity proxy time mapping is supported")
        if abs(float(mapping.get("source_in_offset_seconds", 0))) > EPSILON:
            raise ValueError("Processing proxy must use zero source offset")
    return actual_source, media


def _generated_at() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _event(
    timestamp: float,
    kind: str,
    confidence: float,
    source_id: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "timestamp": round(float(timestamp), 6),
        "kind": kind,
        "confidence": round(max(0.0, min(1.0, float(confidence))), 4),
        "source_id": source_id,
        "details": details,
    }


def _within_media(timestamp: float, duration: float) -> bool:
    return EPSILON < timestamp < duration - EPSILON


def extract_apple_stt_signals(
    transcript: dict[str, Any], *, source: dict[str, Any], duration: float
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    intervals = sorted(
        transcript.get("activity_intervals", []), key=lambda item: float(item["start"])
    )
    for interval in intervals:
        activity_id = str(interval["activity_id"])
        start = float(interval["start"])
        end = float(interval["end"])
        details = {
            "activity_id": activity_id,
            "activity_source": interval.get("source"),
        }
        if _within_media(start, duration):
            events.append(
                _event(start, "speech_start", 0.74, f"{activity_id}-START", details)
            )
        if _within_media(end, duration):
            events.append(_event(end, "speech_end", 0.74, f"{activity_id}-END", details))

    for index, (left, right) in enumerate(zip(intervals, intervals[1:]), start=1):
        gap_start = float(left["end"])
        gap_end = float(right["start"])
        gap = gap_end - gap_start
        if gap >= 1.2:
            timestamp = (gap_start + gap_end) / 2
            events.append(
                _event(
                    timestamp,
                    "silence_gap",
                    min(0.92, 0.60 + gap / 20),
                    f"APPLE-SILENCE-{index:04d}",
                    {
                        "start": round(gap_start, 6),
                        "end": round(gap_end, 6),
                        "duration": round(gap, 6),
                        "left_activity_id": left["activity_id"],
                        "right_activity_id": right["activity_id"],
                    },
                )
            )

    candidate_boundaries: list[dict[str, Any]] = []
    for candidate in transcript.get("candidates", []):
        timestamp = float(candidate.get("end", 0))
        if not _within_media(timestamp, duration):
            continue
        confidence = float(candidate.get("mean_confidence") or 0.5)
        candidate_boundaries.append(
            _event(
                timestamp,
                "speech_segment_boundary",
                0.35 + 0.25 * confidence,
                f"{candidate['utterance_id']}-END",
                {
                    "candidate_id": candidate["utterance_id"],
                    "requested_locale": candidate.get("requested_locale"),
                    "mean_confidence": candidate.get("mean_confidence"),
                    "semantics": "raw_transcriber_segment_end_not_verified_topic_change",
                },
            )
        )
    # A raw transcriber segment end is only promoted when another locale independently
    # places a boundary at nearly the same time. This avoids treating every fragment
    # from one recognizer as a topic boundary.
    consensus_boundaries = [
        event
        for event in candidate_boundaries
        if any(
            other["source_id"] != event["source_id"]
            and other["details"].get("requested_locale")
            != event["details"].get("requested_locale")
            and abs(float(other["timestamp"]) - float(event["timestamp"])) <= 0.35
            for other in candidate_boundaries
        )
    ]
    events.extend(consensus_boundaries)
    events.sort(key=lambda item: (item["timestamp"], item["source_id"]))
    return {
        "schema_version": SIGNAL_SCHEMA,
        "signal_family": "apple-stt",
        "source": source,
        "media": {"duration": duration},
        "producer": {
            "name": "travel-video Apple STT adapter",
            "semantics": (
                "activity intervals are detector-gated transcriber time unions; "
                "raw locale candidates are evidence, not resolved language"
            ),
        },
        "summary": {"event_count": len(events)},
        "events": events,
    }


def _parse_ffmpeg_metadata(output: str, key: str) -> list[tuple[float, float]]:
    samples: list[tuple[float, float]] = []
    timestamp: float | None = None
    for line in output.splitlines():
        match = re.search(r"pts_time:([-+0-9.eE]+)", line)
        if match:
            timestamp = float(match.group(1))
            continue
        if timestamp is not None and line.startswith(f"{key}="):
            samples.append((timestamp, float(line.split("=", 1)[1])))
            timestamp = None
    return samples


def _run_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _quantile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def extract_ffmpeg_signals(
    source_path: Path,
    *,
    source: dict[str, Any],
    duration: float,
    scene_threshold: float,
    motion_interval: float,
    motion_delta_quantile: float,
) -> dict[str, Any]:
    scene_filter = (
        f"select='gt(scene,{scene_threshold})',metadata=print:file=-"
    )
    scene_result = _run_capture(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            str(source_path),
            "-vf",
            scene_filter,
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    scene_samples = _parse_ffmpeg_metadata(
        scene_result.stdout + "\n" + scene_result.stderr, "lavfi.scene_score"
    )

    motion_rate = 1.0 / motion_interval
    motion_filter = (
        f"fps={motion_rate:.9f},tblend=all_mode=difference,signalstats,"
        "metadata=mode=print:key=lavfi.signalstats.YAVG:file=-"
    )
    motion_result = _run_capture(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-i",
            str(source_path),
            "-vf",
            motion_filter,
            "-an",
            "-f",
            "null",
            "-",
        ]
    )
    motion_samples = _parse_ffmpeg_metadata(
        motion_result.stdout + "\n" + motion_result.stderr,
        "lavfi.signalstats.YAVG",
    )
    motion_deltas = [
        abs(current[1] - previous[1])
        for previous, current in zip(motion_samples[1:], motion_samples[2:])
    ]
    motion_threshold = max(8.0, _quantile(motion_deltas, motion_delta_quantile))

    events = [
        _event(
            timestamp,
            "hard_cut",
            min(0.99, 0.55 + score * 0.6),
            f"FFMPEG-SCENE-{index:04d}",
            {"scene_score": round(score, 6), "threshold": scene_threshold},
        )
        for index, (timestamp, score) in enumerate(scene_samples, start=1)
        if _within_media(timestamp, duration)
    ]
    motion_events: list[dict[str, Any]] = []
    for index, (previous, current) in enumerate(
        zip(motion_samples[1:], motion_samples[2:]), start=1
    ):
        delta = abs(current[1] - previous[1])
        if delta < motion_threshold or not _within_media(current[0], duration):
            continue
        motion_events.append(
            _event(
                current[0],
                "motion_change",
                min(0.92, 0.5 + 0.4 * delta / max(motion_threshold, 1.0)),
                f"FFMPEG-MOTION-{index:04d}",
                {
                    "previous_yavg": round(previous[1], 6),
                    "current_yavg": round(current[1], 6),
                    "absolute_delta": round(delta, 6),
                    "adaptive_threshold": round(motion_threshold, 6),
                },
            )
        )
    events.extend(_nms(motion_events, tolerance=motion_interval * 2, by_kind=True))
    events.sort(key=lambda item: (item["timestamp"], item["source_id"]))
    return {
        "schema_version": SIGNAL_SCHEMA,
        "signal_family": "ffmpeg",
        "source": source,
        "media": {"duration": duration},
        "producer": {
            "name": "ffmpeg",
            "scene_threshold": scene_threshold,
            "motion_interval_seconds": motion_interval,
            "motion_delta_quantile": motion_delta_quantile,
            "motion_delta_threshold": round(motion_threshold, 6),
        },
        "raw": {
            "scene_samples": [
                {"timestamp": round(timestamp, 6), "score": round(score, 6)}
                for timestamp, score in scene_samples
            ],
            "motion_samples": [
                {"timestamp": round(timestamp, 6), "yavg": round(value, 6)}
                for timestamp, value in motion_samples
            ],
        },
        "summary": {"event_count": len(events)},
        "events": events,
    }


def _stable_count_changes(
    samples: list[dict[str, Any]],
    field: str,
    kind: str,
    prefix: str,
    *,
    minimum_samples: int,
    nms_tolerance: float,
) -> list[dict[str, Any]]:
    if not samples:
        return []
    runs: list[list[dict[str, Any]]] = []
    for sample in samples:
        presence = int(int(sample.get(field, 0)) > 0)
        annotated = {**sample, "_presence": presence}
        if not runs or int(runs[-1][-1]["_presence"]) != presence:
            runs.append([annotated])
        else:
            runs[-1].append(annotated)
    stable = [run for run in runs if len(run) >= minimum_samples]
    events: list[dict[str, Any]] = []
    for index, (previous, current) in enumerate(zip(stable, stable[1:]), start=1):
        previous_presence = int(previous[-1]["_presence"])
        current_presence = int(current[0]["_presence"])
        if previous_presence == current_presence:
            continue
        previous_count = int(previous[-1].get(field, 0))
        current_count = int(current[0].get(field, 0))
        events.append(
            _event(
                current[0]["timestamp"],
                kind,
                0.68,
                f"{prefix}-{index:04d}",
                {
                    "previous_count": previous_count,
                    "current_count": current_count,
                    "previous_presence": bool(previous_presence),
                    "current_presence": bool(current_presence),
                    "persistence_samples": len(current),
                },
            )
        )
    return _nms(events, tolerance=nms_tolerance, by_kind=True)


def _normalized_ocr(lines: list[dict[str, Any]]) -> set[str]:
    words: set[str] = set()
    for line in lines:
        if float(line.get("confidence", 0)) < 0.35:
            continue
        words.update(
            item
            for item in re.findall(r"[\w]+", str(line.get("text", "")).casefold())
            if len(item) >= 2
        )
    return words


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def extract_vision_signals(
    raw: dict[str, Any],
    *,
    source: dict[str, Any],
    duration: float,
    feature_distance_floor: float,
    feature_distance_quantile: float,
) -> dict[str, Any]:
    if raw.get("schemaVersion") != VISION_RAW_SCHEMA:
        raise ValueError(f"Expected {VISION_RAW_SCHEMA}")
    if _normalized_path(raw.get("input", "")) != _normalized_path(source["path"]):
        raise ValueError("Apple Vision raw input does not match processing source")
    raw_duration = float(raw.get("sourceDurationSeconds", 0))
    if abs(raw_duration - duration) > 0.25:
        raise ValueError("Apple Vision raw duration does not match timeline")

    samples = sorted(raw.get("visualSamples", []), key=lambda item: item["timestamp"])
    distances = [
        float(item["featureDistanceFromPrevious"])
        for item in samples
        if item.get("featureDistanceFromPrevious") is not None
    ]
    feature_threshold = max(
        feature_distance_floor,
        _quantile(distances, feature_distance_quantile),
    )
    events: list[dict[str, Any]] = []
    for index, sample in enumerate(samples, start=1):
        timestamp = float(sample["timestamp"])
        distance = sample.get("featureDistanceFromPrevious")
        if (
            distance is not None
            and float(distance) >= feature_threshold
            and _within_media(timestamp, duration)
        ):
            events.append(
                _event(
                    timestamp,
                    "visual_change",
                    min(0.96, 0.52 + 0.35 * float(distance) / max(feature_threshold, 0.01)),
                    f"VISION-FEATURE-{index:04d}",
                    {
                        "feature_distance": round(float(distance), 6),
                        "adaptive_threshold": round(feature_threshold, 6),
                    },
                )
            )
    events = _nms(
        events,
        tolerance=float(raw.get("visualIntervalSeconds", 1 / 3)) * 2,
        by_kind=True,
    )
    visual_interval = float(raw.get("visualIntervalSeconds", 1 / 3))
    minimum_presence_samples = max(2, math.ceil(0.9 / visual_interval))
    events.extend(
        _stable_count_changes(
            samples,
            "faceCount",
            "face_presence_change",
            "VISION-FACE",
            minimum_samples=minimum_presence_samples,
            nms_tolerance=2.0,
        )
    )
    events.extend(
        _stable_count_changes(
            samples,
            "personCount",
            "person_presence_change",
            "VISION-PERSON",
            minimum_samples=minimum_presence_samples,
            nms_tolerance=2.0,
        )
    )

    utility_events: list[dict[str, Any]] = []
    for index, (previous, current) in enumerate(zip(samples, samples[1:]), start=1):
        previous_score = previous.get("aestheticsScore")
        current_score = current.get("aestheticsScore")
        utility_changed = previous.get("isUtility") != current.get("isUtility")
        score_delta = (
            abs(float(current_score) - float(previous_score))
            if previous_score is not None and current_score is not None
            else 0
        )
        if not utility_changed and score_delta < 0.25:
            continue
        utility_events.append(
            _event(
                current["timestamp"],
                "quality_change",
                min(0.85, 0.55 + score_delta * 0.6),
                f"VISION-QUALITY-{index:04d}",
                {
                    "previous_score": previous_score,
                    "current_score": current_score,
                    "previous_utility": previous.get("isUtility"),
                    "current_utility": current.get("isUtility"),
                },
            )
        )
    events.extend(
        _nms(
            utility_events,
            tolerance=float(raw.get("visualIntervalSeconds", 1 / 3)) * 2,
            by_kind=True,
        )
    )

    ocr_events: list[dict[str, Any]] = []
    ocr_samples = sorted(raw.get("ocrSamples", []), key=lambda item: item["timestamp"])
    raw_ocr_words = [_normalized_ocr(item.get("lines", [])) for item in ocr_samples]
    persistent_ocr_words = [
        current & following
        for current, following in zip(raw_ocr_words, raw_ocr_words[1:])
    ]
    for index, (previous_words, current_words) in enumerate(
        zip(persistent_ocr_words, persistent_ocr_words[1:]), start=1
    ):
        similarity = _jaccard(previous_words, current_words)
        if similarity >= 0.35 or (not previous_words and not current_words):
            continue
        ocr_events.append(
            _event(
                ocr_samples[index]["timestamp"],
                "ocr_change",
                min(0.87, 0.58 + (1 - similarity) * 0.25),
                f"VISION-OCR-{index:04d}",
                {
                    "previous_tokens": sorted(previous_words)[:20],
                    "current_tokens": sorted(current_words)[:20],
                    "jaccard_similarity": round(similarity, 4),
                },
            )
        )
    events.extend(
        _nms(
            ocr_events,
            tolerance=max(2.0, float(raw.get("ocrIntervalSeconds", 1.0)) * 2),
            by_kind=True,
        )
    )
    events = [item for item in events if _within_media(item["timestamp"], duration)]
    events.sort(key=lambda item: (item["timestamp"], item["source_id"]))
    return {
        "schema_version": SIGNAL_SCHEMA,
        "signal_family": "apple-vision",
        "source": source,
        "media": {"duration": duration},
        "producer": {
            "name": "Apple Vision",
            "raw_schema": VISION_RAW_SCHEMA,
            "visual_interval_seconds": raw.get("visualIntervalSeconds"),
            "ocr_interval_seconds": raw.get("ocrIntervalSeconds"),
            "feature_distance_floor": feature_distance_floor,
            "feature_distance_quantile": feature_distance_quantile,
            "feature_distance_threshold": round(feature_threshold, 6),
        },
        "summary": {"event_count": len(events)},
        "events": events,
    }


def _percentile_rank(ordered: list[float], value: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1 or ordered[-1] - ordered[0] <= EPSILON:
        return 0.5
    return max(0.0, min(1.0, bisect_right(ordered, value) / len(ordered)))


def _nearest_sample(
    samples: list[dict[str, Any]], timestamps: list[float], timestamp: float
) -> tuple[int, dict[str, Any]] | None:
    if not samples:
        return None
    insertion = bisect_left(timestamps, timestamp)
    candidates = {
        max(0, min(len(samples) - 1, insertion - 1)),
        max(0, min(len(samples) - 1, insertion)),
    }
    index = min(
        candidates, key=lambda item: abs(float(samples[item]["timestamp"]) - timestamp)
    )
    return index, samples[index]


def _speech_overlap(
    start: float, end: float, activity_intervals: list[dict[str, Any]]
) -> float:
    return min(
        end - start,
        sum(
            max(
                0.0,
                min(end, float(interval["end"]))
                - max(start, float(interval["start"])),
            )
            for interval in activity_intervals
        ),
    )


def derive_visual_moments(
    vision_raw: dict[str, Any],
    ffmpeg_signals: dict[str, Any],
    apple_transcript: dict[str, Any],
    *,
    source: dict[str, Any],
    asset_id: str,
    duration: float,
    config: BoundaryProposalConfig,
) -> dict[str, Any]:
    """Create visual/action discovery intervals without using speech as a filter."""
    samples = sorted(
        vision_raw.get("visualSamples", []), key=lambda item: float(item["timestamp"])
    )
    motion_samples = sorted(
        ffmpeg_signals.get("raw", {}).get("motion_samples", []),
        key=lambda item: float(item["timestamp"]),
    )
    motion_timestamps = [float(item["timestamp"]) for item in motion_samples]
    if not samples:
        return {
            "schema_version": VISUAL_MOMENT_SCHEMA,
            "asset_id": asset_id,
            "source": source,
            "media": {"duration": duration},
            "policy": {
                "speech_is_selection_filter": False,
                "window_seconds": config.visual_moment_window,
                "nms_seconds": config.visual_moment_nms,
                "max_moments_per_minute": config.visual_moments_per_minute,
            },
            "summary": {"moment_count": 0, "speech_free_moment_count": 0},
            "moments": [],
        }

    aesthetic_values = sorted(
        float(item["aestheticsScore"])
        for item in samples
        if item.get("aestheticsScore") is not None
    )
    feature_values = sorted(
        float(item["featureDistanceFromPrevious"])
        for item in samples
        if item.get("featureDistanceFromPrevious") is not None
    )
    motion_values = sorted(float(item["yavg"]) for item in motion_samples)
    aesthetic_threshold = max(0.12, _quantile(aesthetic_values, 0.82))
    feature_threshold = max(0.08, _quantile(feature_values, 0.88))
    motion_threshold = max(8.0, _quantile(motion_values, 0.88))

    seeds: list[dict[str, Any]] = []
    for index, sample in enumerate(samples, start=1):
        timestamp = float(sample["timestamp"])
        if not _within_media(timestamp, duration):
            continue
        aesthetic = (
            float(sample["aestheticsScore"])
            if sample.get("aestheticsScore") is not None
            else None
        )
        feature = (
            float(sample["featureDistanceFromPrevious"])
            if sample.get("featureDistanceFromPrevious") is not None
            else None
        )
        nearest_motion = _nearest_sample(motion_samples, motion_timestamps, timestamp)
        motion_sample = nearest_motion[1] if nearest_motion is not None else None
        motion = float(motion_sample["yavg"]) if motion_sample is not None else None
        aesthetic_rank = (
            _percentile_rank(aesthetic_values, aesthetic)
            if aesthetic is not None
            else 0.0
        )
        feature_rank = (
            _percentile_rank(feature_values, feature) if feature is not None else 0.0
        )
        motion_rank = (
            _percentile_rank(motion_values, motion) if motion is not None else 0.0
        )
        aesthetic_peak = aesthetic is not None and aesthetic >= aesthetic_threshold
        feature_peak = feature is not None and feature >= feature_threshold
        motion_peak = motion is not None and motion >= motion_threshold
        utility = bool(sample.get("isUtility"))
        person_count = int(sample.get("personCount", 0))
        face_count = int(sample.get("faceCount", 0))
        candid_candidate = bool(person_count or face_count) and aesthetic_rank >= 0.62
        if not any(
            (aesthetic_peak, feature_peak, motion_peak, utility, candid_candidate)
        ):
            continue

        action_score = 0.52 * motion_rank + 0.28 * feature_rank + 0.20 * aesthetic_rank
        candid_score = (
            0.42 * aesthetic_rank
            + 0.23 * motion_rank
            + 0.15 * feature_rank
            + (0.20 if person_count or face_count else 0.0)
        )
        visual_score = (
            0.58 * aesthetic_rank
            + 0.27 * feature_rank
            + 0.15 * (1.0 - motion_rank)
        )
        role_scores = {
            "action": action_score if motion_peak else 0.0,
            "candid": candid_score if candid_candidate else 0.0,
            "visual": visual_score,
        }
        roles = [
            role
            for role, qualifies in (
                ("visual", aesthetic_peak or feature_peak or utility),
                ("action", motion_peak),
                ("candid", candid_candidate),
            )
            if qualifies
        ]
        primary_role = max(roles, key=lambda role: role_scores[role])
        score = max(role_scores.values())
        if utility:
            score = max(score, 0.72)
        evidence = [
            {
                "source_id": f"VISION-SAMPLE-{index:06d}",
                "kind": "apple_vision_sample",
                "timestamp": round(timestamp, 6),
                "details": {
                    "aesthetics_score": aesthetic,
                    "aesthetics_percentile": round(aesthetic_rank, 4),
                    "feature_distance": feature,
                    "feature_distance_percentile": round(feature_rank, 4),
                    "is_utility": utility,
                    "face_count": face_count,
                    "person_count": person_count,
                },
            }
        ]
        if nearest_motion is not None:
            motion_index, motion_sample = nearest_motion
            evidence.append(
                {
                    "source_id": f"FFMPEG-MOTION-SAMPLE-{motion_index + 1:06d}",
                    "kind": "ffmpeg_difference_motion",
                    "timestamp": round(float(motion_sample["timestamp"]), 6),
                    "details": {
                        "yavg": motion,
                        "motion_percentile": round(motion_rank, 4),
                    },
                }
            )
        seeds.append(
            {
                "timestamp": timestamp,
                "score": max(0.0, min(1.0, score)),
                "primary_role": primary_role,
                "roles": roles,
                "evidence": evidence,
                "attributes": {
                    "aesthetics_score": aesthetic,
                    "aesthetics_percentile": round(aesthetic_rank, 4),
                    "feature_distance": feature,
                    "feature_distance_percentile": round(feature_rank, 4),
                    "motion_yavg": motion,
                    "motion_percentile": round(motion_rank, 4),
                    "is_utility": utility,
                    "face_count": face_count,
                    "person_count": person_count,
                },
            }
        )

    # Always retain at least one visual index point for a decodable clip. This keeps
    # quiet, slowly changing landscapes discoverable even if all absolute thresholds
    # are below the genre-wide defaults.
    if not seeds:
        fallback = max(
            enumerate(samples, start=1),
            key=lambda item: (
                float(
                    item[1]["aestheticsScore"]
                    if item[1].get("aestheticsScore") is not None
                    else -1.0
                ),
                -float(item[1]["timestamp"]),
            ),
        )
        index, sample = fallback
        timestamp = min(duration - EPSILON, max(EPSILON, float(sample["timestamp"])))
        seeds.append(
            {
                "timestamp": timestamp,
                "score": 0.5,
                "primary_role": "visual",
                "roles": ["visual"],
                "evidence": [
                    {
                        "source_id": f"VISION-SAMPLE-{index:06d}",
                        "kind": "apple_vision_fallback_sample",
                        "timestamp": round(timestamp, 6),
                        "details": {
                            "aesthetics_score": sample.get("aestheticsScore"),
                            "reason": "no_sample_crossed_absolute_candidate_thresholds",
                        },
                    }
                ],
                "attributes": {
                    "aesthetics_score": sample.get("aestheticsScore"),
                    "aesthetics_percentile": 0.5,
                    "feature_distance": sample.get("featureDistanceFromPrevious"),
                    "feature_distance_percentile": 0.5,
                    "motion_yavg": None,
                    "motion_percentile": 0.0,
                    "is_utility": bool(sample.get("isUtility")),
                    "face_count": int(sample.get("faceCount", 0)),
                    "person_count": int(sample.get("personCount", 0)),
                },
            }
        )

    max_moments = max(
        1, math.ceil(duration / 60.0 * config.visual_moments_per_minute)
    )
    selected: list[dict[str, Any]] = []
    for seed in sorted(seeds, key=lambda item: (-item["score"], item["timestamp"])):
        if any(
            abs(float(seed["timestamp"]) - float(other["timestamp"]))
            < config.visual_moment_nms
            for other in selected
        ):
            continue
        selected.append(seed)
        if len(selected) >= max_moments:
            break

    half_window = config.visual_moment_window / 2.0
    activities = apple_transcript.get("activity_intervals", [])
    moments: list[dict[str, Any]] = []
    for index, seed in enumerate(
        sorted(selected, key=lambda item: item["timestamp"]), start=1
    ):
        timestamp = float(seed["timestamp"])
        start = max(0.0, timestamp - half_window)
        end = min(duration, timestamp + half_window)
        overlap = _speech_overlap(start, end, activities)
        moment_id = f"VM{index:04d}"
        moments.append(
            {
                "moment_id": moment_id,
                "start": round(start, 6),
                "end": round(end, 6),
                "timecode": f"{format_time(start)}-{format_time(end)}",
                "representative_timestamp": round(timestamp, 6),
                "representative_timecode": format_time(timestamp),
                "representative_sample_id": f"{moment_id}-FRAME",
                "representative_frame": None,
                "primary_role": seed["primary_role"],
                "roles": seed["roles"],
                "score": round(float(seed["score"]), 4),
                "confidence": round(0.55 + 0.4 * float(seed["score"]), 4),
                "speech_overlap_seconds": round(overlap, 6),
                "speech_free": overlap <= 0.25,
                "evidence": seed["evidence"],
                "attributes": seed["attributes"],
            }
        )
    return {
        "schema_version": VISUAL_MOMENT_SCHEMA,
        "asset_id": asset_id,
        "source": source,
        "media": {"duration": duration},
        "policy": {
            "speech_is_selection_filter": False,
            "speech_free_max_overlap_seconds": 0.25,
            "window_seconds": config.visual_moment_window,
            "nms_seconds": config.visual_moment_nms,
            "max_moments_per_minute": config.visual_moments_per_minute,
            "candidate_thresholds": {
                "aesthetics_score": round(aesthetic_threshold, 6),
                "feature_distance": round(feature_threshold, 6),
                "motion_yavg": round(motion_threshold, 6),
            },
            "roles": ["visual", "action", "candid"],
        },
        "summary": {
            "moment_count": len(moments),
            "speech_free_moment_count": sum(
                1 for moment in moments if moment["speech_free"]
            ),
            "role_counts": {
                role: sum(
                    1 for moment in moments if moment["primary_role"] == role
                )
                for role in ("visual", "action", "candid")
            },
        },
        "moments": moments,
    }


def _extract_visual_moment_frames(
    source_path: Path,
    moments: dict[str, Any],
    frame_dir: Path,
    *,
    width: int,
) -> None:
    frame_dir.mkdir(parents=True, exist_ok=True)
    for moment in moments["moments"]:
        destination = (frame_dir / f"{moment['moment_id']}.jpg").resolve()
        _run_capture(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(moment["representative_timestamp"]),
                "-i",
                str(source_path),
                "-frames:v",
                "1",
                "-vf",
                f"scale='min({width},iw)':-2",
                "-q:v",
                "3",
                "-y",
                str(destination),
            ]
        )
        if not destination.is_file() or destination.stat().st_size == 0:
            raise RuntimeError(
                f"FFmpeg did not create visual moment frame: {destination}"
            )
        moment["representative_frame"] = str(destination)


def _nms(
    events: list[dict[str, Any]], *, tolerance: float, by_kind: bool
) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    ranked = sorted(events, key=lambda item: (-item["confidence"], item["timestamp"]))
    for event in ranked:
        if any(
            abs(float(event["timestamp"]) - float(other["timestamp"])) <= tolerance
            and (not by_kind or event["kind"] == other["kind"])
            for other in kept
        ):
            continue
        kept.append(event)
    return sorted(kept, key=lambda item: (item["timestamp"], item["source_id"]))


_KIND_WEIGHTS = {
    "hard_cut": 1.25,
    "speech_start": 1.12,
    "speech_end": 1.12,
    "silence_gap": 1.08,
    "visual_change": 1.0,
    "motion_change": 0.92,
    "face_presence_change": 0.9,
    "person_presence_change": 0.9,
    "ocr_change": 0.88,
    "quality_change": 0.75,
    "speech_segment_boundary": 0.72,
}


def _weighted_nms(events: list[dict[str, Any]], *, tolerance: float) -> list[dict[str, Any]]:
    kept: list[dict[str, Any]] = []
    ranked = sorted(
        events,
        key=lambda item: (
            -float(item["confidence"]) * _KIND_WEIGHTS.get(item["kind"], 0.8),
            item["timestamp"],
        ),
    )
    for event in ranked:
        if any(
            abs(float(event["timestamp"]) - float(other["timestamp"])) <= tolerance
            for other in kept
        ):
            continue
        kept.append(event)
    return sorted(kept, key=lambda item: (item["timestamp"], item["source_id"]))


def _opposed_speech(cluster: list[dict[str, Any]], event: dict[str, Any]) -> bool:
    kinds = {item["kind"] for item in cluster}
    return (event["kind"] == "speech_start" and "speech_end" in kinds) or (
        event["kind"] == "speech_end" and "speech_start" in kinds
    )


def cluster_boundary_events(
    events: list[dict[str, Any]], *, duration: float, tolerance: float
) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        source_id = str(event["source_id"])
        if source_id in unique:
            raise ValueError(f"Duplicate boundary evidence source ID: {source_id}")
        if _within_media(float(event["timestamp"]), duration):
            unique[source_id] = event
    ordered = sorted(unique.values(), key=lambda item: (item["timestamp"], item["source_id"]))
    clusters: list[list[dict[str, Any]]] = []
    for event in ordered:
        if (
            not clusters
            or float(event["timestamp"]) - float(clusters[-1][0]["timestamp"]) > tolerance
            or _opposed_speech(clusters[-1], event)
        ):
            clusters.append([event])
        else:
            clusters[-1].append(event)

    proposals: list[dict[str, Any]] = []
    used_timestamps: set[float] = set()
    for cluster in clusters:
        primary = max(
            cluster,
            key=lambda item: (
                float(item["confidence"]) * _KIND_WEIGHTS.get(item["kind"], 0.8),
                -float(item["timestamp"]),
            ),
        )
        timestamp = round(float(primary["timestamp"]), 6)
        while timestamp in used_timestamps:
            timestamp = round(timestamp + EPSILON, 6)
        used_timestamps.add(timestamp)
        confidence = 1.0
        for evidence in cluster:
            weighted = min(
                0.95,
                float(evidence["confidence"])
                * min(1.0, _KIND_WEIGHTS.get(evidence["kind"], 0.8)),
            )
            confidence *= 1 - weighted
        proposal_confidence = round(min(0.99, 1 - confidence), 4)
        if proposal_confidence < 0.60:
            continue
        proposals.append(
            {
                "proposal_id": "",
                "timestamp": timestamp,
                "timecode": format_time(timestamp),
                "confidence": proposal_confidence,
                "primary_kind": primary["kind"],
                "evidence": sorted(
                    cluster, key=lambda item: (item["timestamp"], item["source_id"])
                ),
            }
        )
    proposals.sort(key=lambda item: item["timestamp"])
    for index, proposal in enumerate(proposals, start=1):
        proposal["proposal_id"] = f"BP{index:04d}"
    return proposals


def _tool_version(command: list[str]) -> str:
    result = _run_capture(command)
    return (result.stdout or result.stderr).splitlines()[0].strip()


def _vision_binary() -> tuple[Path, Path]:
    project_root = Path(__file__).resolve().parents[2]
    source = project_root / "scripts" / "apple_vision_boundary_signals.swift"
    binary = project_root / ".build" / "apple-vision-boundary-signals"
    if not source.is_file():
        raise RuntimeError(f"Apple Vision helper source is missing: {source}")
    if not binary.is_file() or binary.stat().st_mtime < source.stat().st_mtime:
        binary.parent.mkdir(parents=True, exist_ok=True)
        temporary = binary.with_suffix(".partial")
        subprocess.run(
            ["swiftc", "-parse-as-library", "-O", str(source), "-o", str(temporary)],
            check=True,
        )
        os.replace(temporary, binary)
    return source, binary


def _implementation_digest() -> str:
    python_source = Path(__file__).resolve()
    swift_source = python_source.parents[2] / "scripts" / "apple_vision_boundary_signals.swift"
    digest = hashlib.sha256()
    for path in (python_source, swift_source, python_source.with_name("clip_evidence.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_vision(
    source_path: Path,
    output_path: Path,
    *,
    visual_interval: float,
    ocr_interval: float,
) -> dict[str, Any]:
    _, binary = _vision_binary()
    subprocess.run(
        [
            str(binary),
            "--input",
            str(source_path),
            "--output",
            str(output_path),
            "--visual-interval",
            str(visual_interval),
            "--ocr-interval",
            str(ocr_interval),
        ],
        check=True,
    )
    return load_json(output_path)


def build_boundary_proposals(
    processing_input: Path,
    timeline_path: Path,
    apple_transcript_path: Path,
    output_dir: Path,
    *,
    lineage_path: Path | None = None,
    config: BoundaryProposalConfig | None = None,
) -> Path:
    config = config or BoundaryProposalConfig()
    config.validate()
    processing_input = processing_input.expanduser().resolve()
    timeline_path = timeline_path.expanduser().resolve()
    apple_transcript_path = apple_transcript_path.expanduser().resolve()
    lineage_path = lineage_path.expanduser().resolve() if lineage_path else None
    output_dir = output_dir.expanduser().resolve()
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg and ffprobe are required")
    if not shutil.which("swiftc"):
        raise RuntimeError("swiftc is required for Apple Vision extraction")

    timeline = load_json(timeline_path)
    apple_transcript = load_json(apple_transcript_path)
    lineage = load_json(lineage_path) if lineage_path else None
    source, probed_media = _validate_inputs(
        processing_input, timeline, apple_transcript, lineage
    )
    duration = float(timeline["media"]["duration"])
    intent = {
        "schema_version": "boundary-proposal-intent/v1",
        "producer_revision": PRODUCER_REVISION,
        "implementation_digest": _implementation_digest(),
        "processing_input": source,
        "timeline": {
            "path": str(timeline_path),
            "sha256": _file_sha256(timeline_path),
        },
        "apple_transcript": {
            "path": str(apple_transcript_path),
            "sha256": _file_sha256(apple_transcript_path),
        },
        "lineage": (
            {"path": str(lineage_path), "sha256": _file_sha256(lineage_path)}
            if lineage_path
            else None
        ),
        "config": asdict(config),
    }
    intent_path = output_dir / "run-intent.json"
    if intent_path.exists() and load_json(intent_path) != intent:
        raise ValueError("Output directory belongs to a different input or configuration")
    atomic_json(intent_path, intent)

    proposal_path = output_dir / "proposals.json"
    run_path = output_dir / "run.json"
    visual_moment_path = output_dir / "visual-moments.json"
    if proposal_path.exists() and run_path.exists():
        validate_boundary_proposals(proposal_path, timeline_path)
        if not visual_moment_path.is_file():
            raise ValueError(
                "Boundary run is incomplete: visual-moments.json is missing"
            )
        validate_visual_moments(visual_moment_path, timeline_path)
        return proposal_path
    if proposal_path.exists() and not run_path.exists():
        raise ValueError("Boundary run is incomplete: proposals.json exists without run.json")

    signals_dir = output_dir / "signals"
    apple_signal_path = signals_dir / "apple-stt.json"
    ffmpeg_signal_path = signals_dir / "ffmpeg.json"
    vision_raw_path = signals_dir / "apple-vision.raw.json"
    vision_signal_path = signals_dir / "apple-vision.json"
    visual_moment_frame_dir = output_dir / "visual-moment-frames"

    apple_signals = extract_apple_stt_signals(
        apple_transcript, source=source, duration=duration
    )
    atomic_json(apple_signal_path, apple_signals)
    ffmpeg_signals = extract_ffmpeg_signals(
        processing_input,
        source=source,
        duration=duration,
        scene_threshold=config.ffmpeg_scene_threshold,
        motion_interval=config.motion_interval,
        motion_delta_quantile=config.motion_delta_quantile,
    )
    atomic_json(ffmpeg_signal_path, ffmpeg_signals)
    vision_raw = _run_vision(
        processing_input,
        vision_raw_path,
        visual_interval=config.vision_interval,
        ocr_interval=config.ocr_interval,
    )
    vision_signals = extract_vision_signals(
        vision_raw,
        source=source,
        duration=duration,
        feature_distance_floor=config.feature_distance_floor,
        feature_distance_quantile=config.feature_distance_quantile,
    )
    atomic_json(vision_signal_path, vision_signals)
    visual_moments = derive_visual_moments(
        vision_raw,
        ffmpeg_signals,
        apple_transcript,
        source=source,
        asset_id=str(timeline["asset_id"]),
        duration=duration,
        config=config,
    )
    _extract_visual_moment_frames(
        processing_input,
        visual_moments,
        visual_moment_frame_dir,
        width=config.visual_moment_thumbnail_width,
    )
    atomic_json(visual_moment_path, visual_moments)
    validate_visual_moments(visual_moment_path, timeline_path)

    local_visual_events = _weighted_nms(
        [*ffmpeg_signals["events"], *vision_signals["events"]],
        tolerance=1.0,
    )
    all_events = [*apple_signals["events"], *local_visual_events]
    proposals = cluster_boundary_events(
        all_events, duration=duration, tolerance=config.cluster_tolerance
    )
    payload = {
        "schema_version": BOUNDARY_SCHEMA,
        "asset_id": timeline["asset_id"],
        "source": timeline["source"],
        "media": {"duration": duration},
        "inputs": {
            "processing_input": str(processing_input),
            "timeline": str(timeline_path),
            "apple_transcript": str(apple_transcript_path),
            "lineage": str(lineage_path) if lineage_path else None,
            "visual_moments": str(visual_moment_path),
            "signals": [
                str(apple_signal_path),
                str(ffmpeg_signal_path),
                str(vision_raw_path),
                str(vision_signal_path),
            ],
        },
        "policy": {
            "cluster_tolerance_seconds": config.cluster_tolerance,
            "neighbor_context_seconds": config.neighbor_context,
            "default_fine_pass": False,
            "vision_sample_interval_seconds": config.vision_interval,
            "ocr_sample_interval_seconds": config.ocr_interval,
            "ffmpeg_motion_interval_seconds": config.motion_interval,
            "ffmpeg_scene_threshold": config.ffmpeg_scene_threshold,
            "fusion": "confidence-weighted-primary-timestamp+per-kind-nms/v1",
            "visual_cross_signal_nms_seconds": 1.0,
        },
        "summary": {
            "proposal_count": len(proposals),
            "normalized_event_count": sum(
                len(item["events"])
                for item in (apple_signals, ffmpeg_signals, vision_signals)
            ),
            "fusion_input_event_count": len(all_events),
            "signal_event_counts": {
                "apple_stt": len(apple_signals["events"]),
                "ffmpeg": len(ffmpeg_signals["events"]),
                "apple_vision": len(vision_signals["events"]),
            },
            "visual_events_after_cross_signal_nms": len(local_visual_events),
            "visual_moment_count": len(visual_moments["moments"]),
            "speech_free_visual_moment_count": visual_moments["summary"][
                "speech_free_moment_count"
            ],
        },
        "proposals": proposals,
        "person_observations": person_observations(vision_raw, duration),
    }
    atomic_json(proposal_path, payload)
    validate_boundary_proposals(proposal_path, timeline_path)

    swift_source, _ = _vision_binary()
    run_payload = {
        "schema_version": RUN_SCHEMA,
        "status": "complete",
        "generated_at": _generated_at(),
        "inputs": intent,
        "media_probe": probed_media,
        "tools": {
            "ffmpeg": _tool_version(["ffmpeg", "-version"]),
            "swift": _tool_version(["swift", "--version"]),
            "apple_vision_source": str(swift_source),
        },
        "outputs": {
            "apple_stt_signals": str(apple_signal_path),
            "ffmpeg_signals": str(ffmpeg_signal_path),
            "apple_vision_raw": str(vision_raw_path),
            "apple_vision_signals": str(vision_signal_path),
            "visual_moments": str(visual_moment_path),
            "proposals": str(proposal_path),
        },
        "summary": payload["summary"],
    }
    atomic_json(run_path, run_payload)
    return proposal_path
