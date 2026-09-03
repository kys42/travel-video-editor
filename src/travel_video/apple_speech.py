from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .phase1 import atomic_json, format_time, quick_fingerprint, run

APPLE_STT_SCHEMA = "apple-stt/v1"
APPLE_STT_PIPELINE = "apple-speech-detector+dual-locale-transcriber/v1"


@dataclass(frozen=True)
class AppleSTTConfig:
    locales: tuple[str, ...] = ("ko-KR", "en-US")
    detector_sensitivity: str = "medium"
    sample_rate: int = 16_000
    activity_padding: float = 0.12
    activity_merge_gap: float = 0.35
    swift_package: Path | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _config_digest(config: AppleSTTConfig) -> str:
    payload = asdict(config)
    payload["swift_package"] = (
        str(config.swift_package.expanduser().resolve())
        if config.swift_package is not None
        else None
    )
    encoded = json.dumps(
        {"pipeline": APPLE_STT_PIPELINE, "config": payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:12]


def _probe_audio(source: Path) -> dict[str, Any]:
    completed = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(source),
        ]
    )
    raw = json.loads(completed.stdout)
    audio = next(
        (
            stream
            for stream in raw.get("streams", [])
            if stream.get("codec_type") == "audio"
        ),
        None,
    )
    duration = raw.get("format", {}).get("duration")
    if audio is None or duration is None:
        raise ValueError(f"Source has no readable audio stream: {source}")
    return {
        "duration": float(duration),
        "size_bytes": int(raw.get("format", {}).get("size", source.stat().st_size)),
        "format_name": raw.get("format", {}).get("format_name"),
        "audio": {
            "codec": audio.get("codec_name"),
            "channels": audio.get("channels"),
            "sample_rate": int(audio["sample_rate"])
            if audio.get("sample_rate")
            else None,
        },
    }


def _extract_audio(source: Path, destination: Path, sample_rate: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )


def _default_swift_package() -> Path:
    return Path(__file__).resolve().parents[2] / "apple-speech"


def _apple_speech_binary(package: Path) -> Path:
    binary = package / ".build" / "release" / "apple-speech"
    sources = [package / "Package.swift", *package.glob("Sources/**/*.swift")]
    needs_build = not binary.exists() or any(
        source.exists() and source.stat().st_mtime > binary.stat().st_mtime
        for source in sources
    )
    if needs_build:
        if shutil.which("swift") is None:
            raise RuntimeError("Apple STT requires the Swift toolchain")
        run(["swift", "build", "--package-path", str(package), "-c", "release"])
    if not binary.is_file():
        raise RuntimeError(f"Apple Speech worker was not built: {binary}")
    return binary


def _safe_locale(locale: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", locale)


def normalize_apple_transcripts(
    raw_reports: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for requested_locale, report in raw_reports.items():
        selected_locale = str(report["selected_locale"])
        for index, transcript in enumerate(report.get("transcripts", []), start=1):
            spans = transcript.get("spans", [])
            confidences = [
                float(span["confidence"])
                for span in spans
                if span.get("confidence") is not None
            ]
            start = float(transcript["start"])
            end = float(transcript["end"])
            candidates.append(
                {
                    "utterance_id": (
                        f"APPLE-{_safe_locale(requested_locale)}-T{index:04d}"
                    ),
                    "provider": "apple-speech",
                    "requested_locale": requested_locale,
                    "selected_locale": selected_locale,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "start_timecode": format_time(start),
                    "end_timecode": format_time(end),
                    "text": str(transcript.get("text", "")).strip(),
                    "alternatives": transcript.get("alternatives", []),
                    "spans": spans,
                    "mean_confidence": (
                        round(sum(confidences) / len(confidences), 4)
                        if confidences
                        else None
                    ),
                    "is_final": bool(transcript.get("is_final", True)),
                }
            )
    return sorted(
        candidates,
        key=lambda item: (item["start"], item["end"], item["requested_locale"]),
    )


def derive_activity_ranges(
    candidates: list[dict[str, Any]],
    duration: float,
    *,
    padding: float = 0.12,
    merge_gap: float = 0.35,
) -> list[dict[str, Any]]:
    """Derive inspectable ranges from detector-gated transcriber timestamps.

    macOS 26 uses SpeechDetector to gate co-located transcribers, but its public result
    sequence currently reports errors rather than raw VAD boundaries. These ranges are
    therefore evidence derived from all locale transcribers, not direct detector output.
    """

    intervals = sorted(
        (
            max(0.0, float(candidate["start"]) - padding),
            min(duration, float(candidate["end"]) + padding),
        )
        for candidate in candidates
        if str(candidate.get("text", "")).strip()
        and float(candidate["end"]) > float(candidate["start"])
    )
    merged: list[list[float]] = []
    for start, end in intervals:
        if not merged or start - merged[-1][1] > merge_gap:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [
        {
            "activity_id": f"APPLE-VAD-{index:04d}",
            "start": round(start, 3),
            "end": round(end, 3),
            "start_timecode": format_time(start),
            "end_timecode": format_time(end),
            "source": "detector_gated_transcriber_time_union",
        }
        for index, (start, end) in enumerate(merged, start=1)
    ]


def process_apple_stt(
    source: Path,
    output_dir: Path,
    config: AppleSTTConfig,
) -> Path:
    source = source.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if not config.locales:
        raise ValueError("At least one Apple Speech locale is required")
    if config.detector_sensitivity not in {"low", "medium", "high"}:
        raise ValueError("Detector sensitivity must be low, medium, or high")
    if config.sample_rate <= 0:
        raise ValueError("Sample rate must be positive")

    media = _probe_audio(source)
    fingerprint = quick_fingerprint(source)
    config_digest = _config_digest(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_path = output_dir / "run.json"
    transcript_path = output_dir / "transcript.apple.json"
    if run_path.exists():
        previous = json.loads(run_path.read_text(encoding="utf-8"))
        same_run = (
            previous.get("source", {}).get("quick_fingerprint") == fingerprint
            and previous.get("config_digest") == config_digest
        )
        if same_run and previous.get("status") == "complete" and transcript_path.exists():
            return transcript_path
        if not same_run:
            raise RuntimeError("Output directory already contains a different Apple STT run")

    started_at = _now()
    run_record: dict[str, Any] = {
        "schema_version": "apple-stt-run/v1",
        "status": "running",
        "started_at": started_at,
        "source": {
            "path": str(source),
            "name": source.name,
            "quick_fingerprint": fingerprint,
        },
        "media": media,
        "config": {
            **asdict(config),
            "swift_package": str(config.swift_package)
            if config.swift_package is not None
            else None,
        },
        "config_digest": config_digest,
        "pipeline_version": APPLE_STT_PIPELINE,
        "host": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
    }
    atomic_json(run_path, run_record)

    try:
        audio_path = output_dir / "audio" / "source-16k-mono.wav"
        _extract_audio(source, audio_path, config.sample_rate)
        package = (
            config.swift_package.expanduser().resolve()
            if config.swift_package is not None
            else _default_swift_package()
        )
        binary = _apple_speech_binary(package)
        raw_reports: dict[str, dict[str, Any]] = {}
        raw_paths: dict[str, str] = {}
        for locale in config.locales:
            raw_path = output_dir / "raw" / f"{_safe_locale(locale)}.json"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            run(
                [
                    str(binary),
                    "transcribe",
                    "--input",
                    str(audio_path),
                    "--locale",
                    locale,
                    "--output",
                    str(raw_path),
                    "--detector-sensitivity",
                    config.detector_sensitivity,
                ]
            )
            raw_reports[locale] = json.loads(raw_path.read_text(encoding="utf-8"))
            raw_paths[locale] = str(raw_path)

        candidates = normalize_apple_transcripts(raw_reports)
        activity = derive_activity_ranges(
            candidates,
            float(media["duration"]),
            padding=config.activity_padding,
            merge_gap=config.activity_merge_gap,
        )
        normalized_path = output_dir / "normalized" / "candidates.json"
        activity_path = output_dir / "vad" / "activity.json"
        atomic_json(
            normalized_path,
            {
                "schema_version": "apple-stt-candidates/v1",
                "source_fingerprint": fingerprint,
                "candidates": candidates,
            },
        )
        atomic_json(
            activity_path,
            {
                "schema_version": "apple-speech-activity/v1",
                "backend": "SpeechDetector+SpeechTranscriber",
                "detector_sensitivity": config.detector_sensitivity,
                "range_semantics": "detector_gated_transcriber_time_union",
                "direct_detector_intervals_available": False,
                "source_fingerprint": fingerprint,
                "intervals": activity,
            },
        )
        atomic_json(
            transcript_path,
            {
                "schema_version": APPLE_STT_SCHEMA,
                "source": run_record["source"],
                "strategy": APPLE_STT_PIPELINE,
                "detector": {
                    "enabled": True,
                    "backend": "Apple SpeechDetector",
                    "sensitivity": config.detector_sensitivity,
                    "role": "vad_gating_for_each_locale_transcriber",
                    "direct_interval_results_available": False,
                },
                "locales": list(config.locales),
                "paths": {
                    "audio": str(audio_path),
                    "raw": raw_paths,
                    "normalized": str(normalized_path),
                    "activity": str(activity_path),
                },
                "summary": {
                    "candidate_count": len(candidates),
                    "activity_interval_count": len(activity),
                    "candidate_counts_by_locale": {
                        locale: sum(
                            candidate["requested_locale"] == locale
                            for candidate in candidates
                        )
                        for locale in config.locales
                    },
                },
                "candidates": candidates,
                "activity_intervals": activity,
            },
        )
        run_record.update(
            {
                "status": "complete",
                "completed_at": _now(),
                "outputs": {
                    "transcript": str(transcript_path),
                    "normalized": str(normalized_path),
                    "activity": str(activity_path),
                    "raw": raw_paths,
                },
            }
        )
        atomic_json(run_path, run_record)
        return transcript_path
    except Exception as exc:
        run_record.update({"status": "failed", "failed_at": _now(), "error": str(exc)})
        atomic_json(run_path, run_record)
        raise
