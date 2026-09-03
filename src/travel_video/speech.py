from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .phase1 import atomic_json, format_time, probe_media, quick_fingerprint, run

ADAPTIVE_STT_SCHEMA = "adaptive-stt/v1"
ADAPTIVE_STT_PIPELINE = "vad-language-routing/v3"
SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")


@dataclass(frozen=True)
class AdaptiveSTTConfig:
    model: str = "mlx-community/whisper-small-mlx"
    expected_languages: tuple[str, ...] = ("ko", "en")
    silence_db: int = -35
    silence_duration: float = 0.6
    speech_padding: float = 0.15
    merge_gap: float = 0.25
    min_chunk: float = 1.0
    max_chunk: float = 15.0
    min_language_probability: float = 0.80
    min_language_margin: float = 0.20
    min_language_duration: float = 3.0
    fallback_on_uncertain: bool = True


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _config_digest(config: AdaptiveSTTConfig) -> str:
    payload = {"pipeline": ADAPTIVE_STT_PIPELINE, "config": asdict(config)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:12]


def detect_energy_silences(
    source: Path,
    duration: float,
    config: AdaptiveSTTConfig,
) -> list[dict[str, float]]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-vn",
        "-af",
        f"silencedetect=noise={config.silence_db}dB:d={config.silence_duration}",
        "-f",
        "null",
        "-",
    ]
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "FFmpeg silence detection failed")
    starts = [float(value) for value in SILENCE_START.findall(completed.stderr)]
    ends = [float(value) for value in SILENCE_END.findall(completed.stderr)]
    return [
        {
            "start": round(max(0.0, start), 3),
            "end": round(min(duration, ends[index] if index < len(ends) else duration), 3),
        }
        for index, start in enumerate(starts)
    ]


def build_vad_chunks(
    silences: list[dict[str, float]],
    duration: float,
    config: AdaptiveSTTConfig,
) -> list[dict[str, Any]]:
    """Turn silence intervals into bounded speech-analysis chunks.

    The current backend is energy based. Long regions with continuous ambience are split
    into equal-size windows so language detection is never decided from a whole video.
    """

    clipped_silences: list[tuple[float, float]] = []
    for interval in sorted(silences, key=lambda item: float(item["start"])):
        start = max(0.0, min(duration, float(interval["start"])))
        end = max(start, min(duration, float(interval["end"])))
        if end > start:
            clipped_silences.append((start, end))

    raw_regions: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in clipped_silences:
        if start > cursor:
            raw_regions.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        raw_regions.append((cursor, duration))

    padded: list[tuple[float, float]] = []
    for start, end in raw_regions:
        start = max(0.0, start - config.speech_padding)
        end = min(duration, end + config.speech_padding)
        if not padded or start - padded[-1][1] > config.merge_gap:
            padded.append((start, end))
        else:
            padded[-1] = (padded[-1][0], max(padded[-1][1], end))

    pieces: list[dict[str, Any]] = []
    for source_index, (start, end) in enumerate(padded, start=1):
        span = end - start
        if span < config.min_chunk:
            continue
        piece_count = max(1, math.ceil(span / config.max_chunk))
        piece_duration = span / piece_count
        for piece_index in range(piece_count):
            piece_start = start + piece_index * piece_duration
            piece_end = end if piece_index == piece_count - 1 else start + (piece_index + 1) * piece_duration
            pieces.append(
                {
                    "chunk_id": f"C{len(pieces) + 1:04d}",
                    "start": round(piece_start, 3),
                    "end": round(piece_end, 3),
                    "start_timecode": format_time(piece_start),
                    "end_timecode": format_time(piece_end),
                    "source_region_id": f"VAD{source_index:04d}",
                    "split_reason": "max_duration" if piece_count > 1 else "vad_region",
                }
            )
    return pieces


def decide_language_route(
    probabilities: dict[str, float],
    duration: float,
    config: AdaptiveSTTConfig,
) -> dict[str, Any]:
    if not probabilities:
        raise ValueError("Language detection returned no probabilities")
    ranked = sorted(
        ((str(language), float(probability)) for language, probability in probabilities.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    selected_language, top_probability = ranked[0]
    second_probability = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = top_probability - second_probability
    reasons: list[str] = []
    if top_probability < config.min_language_probability:
        reasons.append("low_language_probability")
    if margin < config.min_language_margin:
        reasons.append("low_language_margin")
    if duration < config.min_language_duration:
        reasons.append("short_chunk")
    if selected_language not in config.expected_languages:
        reasons.append("outside_expected_languages")
    uncertain = bool(reasons)
    candidate_languages = [selected_language]
    if uncertain and config.fallback_on_uncertain:
        candidate_languages.extend(config.expected_languages)
    candidate_languages = list(dict.fromkeys(candidate_languages))
    return {
        "selected_language": selected_language,
        "top_probability": round(top_probability, 6),
        "second_probability": round(second_probability, 6),
        "margin": round(margin, 6),
        "uncertain": uncertain,
        "uncertainty_reasons": reasons,
        "candidate_languages": candidate_languages,
        "top_languages": [
            {"language": language, "probability": round(probability, 6)}
            for language, probability in ranked[:5]
        ],
    }


def expand_contextual_fallbacks(
    routes: list[dict[str, Any]],
    config: AdaptiveSTTConfig,
) -> None:
    """Expand dual decoding around a possible language boundary without cascading."""

    if not config.fallback_on_uncertain:
        return
    initially_uncertain = [bool(route["uncertain"]) for route in routes]
    initially_selected = [str(route["selected_language"]) for route in routes]
    for index, route in enumerate(routes):
        neighbor_indices = [
            neighbor
            for neighbor in (index - 1, index + 1)
            if 0 <= neighbor < len(routes)
        ]
        adjacent_uncertain = any(initially_uncertain[neighbor] for neighbor in neighbor_indices)
        adjacent_language_change = any(
            initially_selected[neighbor] != initially_selected[index]
            for neighbor in neighbor_indices
        )
        if not adjacent_uncertain and not adjacent_language_change:
            continue
        route["uncertain"] = True
        if "adjacent_uncertain_or_language_change" not in route["uncertainty_reasons"]:
            route["uncertainty_reasons"].append("adjacent_uncertain_or_language_change")
        route["candidate_languages"] = list(
            dict.fromkeys([*route["candidate_languages"], *config.expected_languages])
        )


def resolve_candidate_language(
    candidates: dict[str, dict[str, Any]],
    detected_language: str,
    *,
    language_probability: float = 0.0,
    language_margin: float = 0.0,
) -> dict[str, Any]:
    metrics: dict[str, dict[str, Any]] = {}
    for language, candidate in candidates.items():
        accepted = [segment for segment in candidate["segments"] if segment["accepted"]]
        metrics[language] = {
            "accepted_segments": len(accepted),
            "accepted_characters": sum(len(segment["text"]) for segment in accepted),
            "mean_avg_logprob": round(
                sum(float(segment["avg_logprob"]) for segment in accepted) / len(accepted),
                4,
            )
            if accepted
            else None,
        }
    usable = [
        language
        for language, metric in metrics.items()
        if int(metric["accepted_segments"]) > 0 and int(metric["accepted_characters"]) > 0
    ]
    if not usable:
        spoken_language = "unknown"
        provisional_language = detected_language
        reason = "no_accepted_candidate"
    elif len(usable) == 1:
        spoken_language = usable[0]
        provisional_language = usable[0]
        reason = "only_usable_candidate"
    elif (
        detected_language in usable
        and language_probability >= 0.95
        and language_margin >= 0.50
    ):
        spoken_language = detected_language
        provisional_language = detected_language
        reason = "strong_language_detection"
    else:
        spoken_language = "mixed_or_uncertain"
        provisional_language = (
            detected_language
            if detected_language in usable
            else max(usable, key=lambda language: int(metrics[language]["accepted_characters"]))
        )
        reason = "multiple_usable_language_candidates"
    return {
        "spoken_language": spoken_language,
        "provisional_language": provisional_language,
        "needs_reconciliation": spoken_language in {"unknown", "mixed_or_uncertain"},
        "reason": reason,
        "candidate_metrics": metrics,
    }


def _extract_chunk(source: Path, destination: Path, start: float, end: float) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{end - start:.3f}",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )


def _worker_command(mode: str, manifest: Path, model: str) -> list[str]:
    worker = Path(__file__).with_name("mlx_adaptive_worker.py")
    arguments = [str(worker), mode, "--manifest", str(manifest), "--model", model]
    if importlib.util.find_spec("mlx_whisper") is not None:
        return [sys.executable, *arguments]
    if shutil.which("uv"):
        return ["uv", "run", "--with", "mlx-whisper", "python", *arguments]
    raise RuntimeError("Adaptive MLX STT requires mlx_whisper or uv")


def _normalize_segments(
    raw: dict[str, Any],
    chunk: dict[str, Any],
    language: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    previous_text = ""
    previous_end = -10.0
    for index, segment in enumerate(raw.get("segments", []), start=1):
        text = " ".join(str(segment.get("text", "")).split())
        comparable = re.sub(r"[^0-9A-Za-z가-힣]+", "", text).lower()
        local_start = float(segment.get("start", 0.0))
        local_end = float(segment.get("end", local_start))
        avg_logprob = float(segment.get("avg_logprob", 0.0))
        no_speech_prob = float(segment.get("no_speech_prob", 0.0))
        compression_ratio = float(segment.get("compression_ratio", 0.0))
        reasons: list[str] = []
        if not comparable:
            reasons.append("empty")
        if avg_logprob < -1.0:
            reasons.append("low_logprob")
        if no_speech_prob > 0.6:
            reasons.append("likely_no_speech")
        if compression_ratio > 2.4:
            reasons.append("high_repetition")
        if comparable == previous_text and local_start - previous_end <= 2.0:
            reasons.append("duplicate_phrase")
        normalized.append(
            {
                "utterance_id": f"{chunk['chunk_id']}-{language}-T{index:03d}",
                "chunk_id": chunk["chunk_id"],
                "language": language,
                "start": round(float(chunk["start"]) + local_start, 3),
                "end": round(float(chunk["start"]) + local_end, 3),
                "local_start": round(local_start, 3),
                "local_end": round(local_end, 3),
                "text": text,
                "avg_logprob": round(avg_logprob, 4),
                "no_speech_prob": round(no_speech_prob, 4),
                "compression_ratio": round(compression_ratio, 4),
                "accepted": not reasons,
                "rejection_reasons": reasons,
            }
        )
        if not reasons:
            previous_text = comparable
            previous_end = local_end
    return normalized


def process_adaptive_stt(
    source: Path,
    output_dir: Path,
    config: AdaptiveSTTConfig,
) -> Path:
    source = source.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if config.max_chunk <= 0 or config.min_chunk <= 0:
        raise ValueError("Chunk durations must be positive")
    if config.max_chunk < config.min_chunk:
        raise ValueError("max_chunk must be greater than or equal to min_chunk")
    if not config.expected_languages:
        raise ValueError("At least one expected language is required")

    media = probe_media(source)
    if media.get("audio") is None:
        raise ValueError(f"Source has no audio stream: {source}")
    fingerprint = quick_fingerprint(source)
    config_digest = _config_digest(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_path = output_dir / "run.json"
    transcript_path = output_dir / "transcript.routed.json"
    if run_path.exists():
        previous = json.loads(run_path.read_text(encoding="utf-8"))
        same_run = (
            previous.get("source", {}).get("quick_fingerprint") == fingerprint
            and previous.get("config_digest") == config_digest
        )
        if same_run and previous.get("status") == "complete" and transcript_path.exists():
            return transcript_path
        if not same_run:
            raise RuntimeError("Output directory already contains a different adaptive STT run")

    started_at = _now()
    run_record: dict[str, Any] = {
        "schema_version": "adaptive-stt-run/v1",
        "status": "running",
        "started_at": started_at,
        "source": {
            "path": str(source),
            "name": source.name,
            "quick_fingerprint": fingerprint,
        },
        "media": media,
        "config": asdict(config),
        "config_digest": config_digest,
        "pipeline_version": ADAPTIVE_STT_PIPELINE,
    }
    atomic_json(run_path, run_record)

    try:
        duration = float(media["duration"])
        silences = detect_energy_silences(source, duration, config)
        vad_dir = output_dir / "vad"
        atomic_json(
            vad_dir / "silences.json",
            {
                "schema_version": "energy-silences/v1",
                "backend": "ffmpeg-silencedetect",
                "source_fingerprint": fingerprint,
                "config": {
                    "silence_db": config.silence_db,
                    "silence_duration": config.silence_duration,
                },
                "intervals": silences,
            },
        )
        chunks = build_vad_chunks(silences, duration, config)
        if not chunks:
            raise RuntimeError("VAD produced no speech-analysis chunks")
        for chunk in chunks:
            audio_path = output_dir / "audio" / f"{chunk['chunk_id']}.wav"
            _extract_chunk(source, audio_path, float(chunk["start"]), float(chunk["end"]))
            chunk["audio_path"] = str(audio_path)
        atomic_json(
            vad_dir / "chunks.json",
            {
                "schema_version": "vad-chunks/v1",
                "backend": "ffmpeg-silencedetect+bounded-windows",
                "source_fingerprint": fingerprint,
                "chunks": chunks,
            },
        )

        detection_input = output_dir / "language" / "detection-input.json"
        detection_jobs = [
            {
                "chunk_id": chunk["chunk_id"],
                "audio_path": chunk["audio_path"],
                "output_path": str(output_dir / "language" / "raw" / f"{chunk['chunk_id']}.json"),
            }
            for chunk in chunks
        ]
        atomic_json(
            detection_input,
            {
                "schema_version": "mlx-language-detection-input/v1",
                "jobs": detection_jobs,
            },
        )
        run(_worker_command("detect", detection_input, config.model), capture=False)

        routes: list[dict[str, Any]] = []
        for chunk, job in zip(chunks, detection_jobs, strict=True):
            detection_path = Path(job["output_path"])
            detection = json.loads(detection_path.read_text(encoding="utf-8"))
            route = decide_language_route(
                detection["language_probabilities"],
                float(chunk["end"]) - float(chunk["start"]),
                config,
            )
            routes.append(
                {
                    **chunk,
                    **route,
                    "detection_path": str(detection_path),
                }
            )

        expand_contextual_fallbacks(routes, config)
        for route in routes:
            route["transcript_paths"] = {
                language: str(
                    output_dir
                    / "transcript"
                    / "raw"
                    / route["chunk_id"]
                    / f"{language}.json"
                )
                for language in route["candidate_languages"]
            }

        routing_path = output_dir / "routing" / "plan.json"
        atomic_json(
            routing_path,
            {
                "schema_version": "adaptive-language-routing/v1",
                "source_fingerprint": fingerprint,
                "policy": {
                    "expected_languages": config.expected_languages,
                    "min_language_probability": config.min_language_probability,
                    "min_language_margin": config.min_language_margin,
                    "min_language_duration": config.min_language_duration,
                    "fallback_on_uncertain": config.fallback_on_uncertain,
                },
                "routes": routes,
            },
        )
        run(_worker_command("transcribe", routing_path, config.model), capture=False)

        normalized_chunks: list[dict[str, Any]] = []
        routed_segments: list[dict[str, Any]] = []
        for route in routes:
            candidates: dict[str, Any] = {}
            for language, raw_path_value in route["transcript_paths"].items():
                raw_path = Path(raw_path_value)
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
                segments = _normalize_segments(raw, route, language)
                candidates[language] = {
                    "raw_path": str(raw_path),
                    "segments": segments,
                }
            resolution = resolve_candidate_language(
                candidates,
                str(route["selected_language"]),
                language_probability=float(route["top_probability"]),
                language_margin=float(route["margin"]),
            )
            provisional = candidates.get(resolution["provisional_language"], {}).get(
                "segments", []
            )
            routed_segments.extend(
                {
                    **segment,
                    "spoken_language": resolution["spoken_language"],
                    "needs_reconciliation": resolution["needs_reconciliation"],
                }
                for segment in provisional
            )
            normalized_chunks.append(
                {
                    key: value
                    for key, value in route.items()
                    if key not in {"audio_path", "transcript_paths"}
                }
                | {
                    "audio_path": route["audio_path"],
                    "candidates": candidates,
                    "resolution": resolution,
                }
            )

        normalized_path = output_dir / "normalized" / "chunks.json"
        atomic_json(
            normalized_path,
            {
                "schema_version": "adaptive-stt-normalized/v1",
                "source_fingerprint": fingerprint,
                "chunks": normalized_chunks,
            },
        )
        language_counts: dict[str, int] = {}
        for route in routes:
            language = str(route["selected_language"])
            language_counts[language] = language_counts.get(language, 0) + 1
        atomic_json(
            transcript_path,
            {
                "schema_version": ADAPTIVE_STT_SCHEMA,
                "source": run_record["source"],
                "model": config.model,
                "strategy": ADAPTIVE_STT_PIPELINE,
                "paths": {
                    "silences": str(vad_dir / "silences.json"),
                    "chunks": str(vad_dir / "chunks.json"),
                    "routing": str(routing_path),
                    "normalized": str(normalized_path),
                },
                "summary": {
                    "chunk_count": len(routes),
                    "uncertain_chunk_count": sum(bool(route["uncertain"]) for route in routes),
                    "selected_language_counts": language_counts,
                    "resolved_language_counts": {
                        language: sum(
                            chunk["resolution"]["spoken_language"] == language
                            for chunk in normalized_chunks
                        )
                        for language in sorted(
                            {
                                chunk["resolution"]["spoken_language"]
                                for chunk in normalized_chunks
                            }
                        )
                    },
                    "needs_reconciliation_chunk_count": sum(
                        bool(chunk["resolution"]["needs_reconciliation"])
                        for chunk in normalized_chunks
                    ),
                },
                "segments": sorted(routed_segments, key=lambda item: (item["start"], item["end"])),
            },
        )
        run_record.update(
            {
                "status": "complete",
                "completed_at": _now(),
                "outputs": {
                    "transcript": str(transcript_path),
                    "normalized": str(normalized_path),
                    "routing": str(routing_path),
                },
            }
        )
        atomic_json(run_path, run_record)
        return transcript_path
    except Exception as exc:
        run_record.update({"status": "failed", "failed_at": _now(), "error": str(exc)})
        atomic_json(run_path, run_record)
        raise
