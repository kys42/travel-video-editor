from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import (
    Image,
    ImageChops,
    ImageDraw,
    ImageFilter,
    ImageFont,
    ImageOps,
    ImageStat,
)

SCHEMA_VERSION = "phase1-machine/v1"


@dataclass(frozen=True)
class Phase1Config:
    sample_interval: float = 5.0
    max_segment: float = 30.0
    min_segment: float = 5.0
    change_threshold: float = 0.19
    group_threshold: float = 0.24
    thumbnail_width: int = 640
    sheet_columns: int = 3
    sheet_rows: int = 4
    silence_db: int = -35
    silence_duration: float = 1.2
    hwaccel: str = "auto"
    stt: str = "off"
    stt_model: str = "mlx-community/whisper-tiny"
    stt_languages: tuple[str, ...] = ("ko", "en")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def format_time(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{minutes:02d}:{secs:02d}.{millis:03d}"


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def run(command: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def require_tools() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise RuntimeError(f"Required tools not found: {', '.join(missing)}")


def quick_fingerprint(path: Path, chunk_size: int = 1024 * 1024) -> str:
    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as source:
        digest.update(source.read(chunk_size))
        if size > chunk_size:
            source.seek(max(0, size - chunk_size))
            digest.update(source.read(chunk_size))
    return digest.hexdigest()


def probe_media(path: Path) -> dict[str, Any]:
    completed = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
    )
    raw = json.loads(completed.stdout)
    video = next((item for item in raw.get("streams", []) if item.get("codec_type") == "video"), None)
    audio = next((item for item in raw.get("streams", []) if item.get("codec_type") == "audio"), None)
    if not video or not raw.get("format", {}).get("duration"):
        raise RuntimeError(f"No playable video stream: {path}")
    tags = {**raw.get("format", {}).get("tags", {}), **video.get("tags", {})}
    return {
        "duration": float(raw["format"]["duration"]),
        "size_bytes": int(raw["format"].get("size", path.stat().st_size)),
        "format_name": raw["format"].get("format_name"),
        "creation_time": tags.get("creation_time"),
        "encoder": tags.get("encoder") or raw["format"].get("tags", {}).get("encoder"),
        "video": {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "pixel_format": video.get("pix_fmt"),
            "frame_rate": video.get("avg_frame_rate") or video.get("r_frame_rate"),
        },
        "audio": None
        if audio is None
        else {
            "codec": audio.get("codec_name"),
            "channels": audio.get("channels"),
            "sample_rate": int(audio["sample_rate"]) if audio.get("sample_rate") else None,
        },
    }


def config_digest(config: Phase1Config) -> str:
    visual_config = {
        key: value
        for key, value in asdict(config).items()
        if key != "stt" and not key.startswith("stt_")
    }
    encoded = json.dumps(visual_config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:10]


def stt_config_digest(config: Phase1Config) -> str:
    payload = {
        "stt": config.stt,
        "model": config.stt_model,
        "languages": config.stt_languages,
        "policy": "parallel-language-candidates/v1",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:10]


def asset_key(path: Path, fingerprint: str) -> str:
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "-", path.stem).strip("-")
    return f"{safe_stem}--{fingerprint[:10]}"


def _ffmpeg_extract_command(
    source: Path,
    destination_pattern: Path,
    config: Phase1Config,
    *,
    use_hwaccel: bool,
) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if use_hwaccel:
        command.extend(["-hwaccel", "videotoolbox"])
    command.extend(
        [
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-vf",
            (
                f"fps=fps=1/{config.sample_interval}:start_time=0,"
                f"scale={config.thumbnail_width}:-2:flags=lanczos"
            ),
            "-q:v",
            "4",
            str(destination_pattern),
        ]
    )
    return command


def extract_sample_frames(source: Path, frame_dir: Path, config: Phase1Config) -> list[Path]:
    frame_dir.mkdir(parents=True, exist_ok=True)
    destination = frame_dir / "frame_%06d.jpg"
    use_hwaccel = config.hwaccel in {"auto", "videotoolbox"} and sys.platform == "darwin"
    command = _ffmpeg_extract_command(source, destination, config, use_hwaccel=use_hwaccel)
    log(f"  extracting low-rate frames every {config.sample_interval:g}s")
    try:
        run(command)
    except subprocess.CalledProcessError as exc:
        if not use_hwaccel or config.hwaccel == "videotoolbox":
            raise RuntimeError(exc.stderr.strip() or "FFmpeg frame extraction failed") from exc
        log("  VideoToolbox failed; retrying with software decode")
        run(_ffmpeg_extract_command(source, destination, config, use_hwaccel=False))
    frames = sorted(frame_dir.glob("frame_*.jpg"))
    if not frames:
        raise RuntimeError(f"No sample frames extracted from {source}")
    return frames


def _grayscale_pixels(image: Image.Image, size: tuple[int, int] = (64, 36)) -> Image.Image:
    return ImageOps.grayscale(image).resize(size, Image.Resampling.BILINEAR)


def average_hash(image: Image.Image) -> str:
    small = ImageOps.grayscale(image).resize((8, 8), Image.Resampling.BILINEAR)
    pixels = list(small.getdata())
    mean = sum(pixels) / len(pixels)
    value = 0
    for pixel in pixels:
        value = (value << 1) | int(pixel >= mean)
    return f"{value:016x}"


def hash_distance(left: str, right: str) -> float:
    return (int(left, 16) ^ int(right, 16)).bit_count() / 64.0


def frame_metrics(path: Path, previous_path: Path | None) -> dict[str, Any]:
    with Image.open(path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        gray = _grayscale_pixels(image)
        stats = ImageStat.Stat(gray)
        brightness = stats.mean[0] / 255.0
        contrast = stats.stddev[0] / 255.0
        edges = gray.filter(ImageFilter.FIND_EDGES)
        sharpness = ImageStat.Stat(edges).stddev[0] / 255.0
        visual_hash = average_hash(image)
        change = 0.0
        if previous_path is not None:
            with Image.open(previous_path) as previous_opened:
                previous = _grayscale_pixels(ImageOps.exif_transpose(previous_opened).convert("RGB"))
                difference = ImageChops.difference(gray, previous)
                change = ImageStat.Stat(difference).mean[0] / 255.0
    exposure_penalty = max(0.0, 0.10 - brightness) * 3.0 + max(0.0, brightness - 0.92) * 3.0
    quality = max(0.0, min(1.0, 0.55 + sharpness * 1.8 + contrast * 0.5 - exposure_penalty))
    flags: list[str] = []
    if brightness < 0.10:
        flags.append("dark")
    elif brightness > 0.92:
        flags.append("overexposed")
    if sharpness < 0.035:
        flags.append("low_detail")
    return {
        "brightness": round(brightness, 4),
        "contrast": round(contrast, 4),
        "sharpness": round(sharpness, 4),
        "visual_change": round(change, 4),
        "average_hash": visual_hash,
        "quality": round(quality, 4),
        "flags": flags,
    }


def analyze_frames(frame_paths: list[Path], duration: float, config: Phase1Config) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for index, frame_path in enumerate(frame_paths):
        timestamp = min(index * config.sample_interval, max(0.0, duration - 0.001))
        metrics = frame_metrics(frame_path, frame_paths[index - 1] if index else None)
        samples.append(
            {
                "sample_id": f"F{index + 1:04d}",
                "time": round(timestamp, 3),
                "timecode": format_time(timestamp),
                "frame": str(frame_path),
                **metrics,
            }
        )
    return samples


def build_segments(samples: list[dict[str, Any]], duration: float, config: Phase1Config) -> list[dict[str, Any]]:
    boundaries: list[tuple[float, str]] = [(0.0, "asset_start")]
    last_boundary = 0.0
    for sample in samples[1:]:
        timestamp = float(sample["time"])
        elapsed = timestamp - last_boundary
        reason: str | None = None
        if elapsed >= config.min_segment and sample["visual_change"] >= config.change_threshold:
            reason = "visual_change"
        elif elapsed >= config.max_segment:
            reason = "max_duration"
        if reason and timestamp < duration:
            boundaries.append((timestamp, reason))
            last_boundary = timestamp
    if duration - boundaries[-1][0] < config.min_segment and len(boundaries) > 1:
        boundaries.pop()
    boundaries.append((duration, "asset_end"))

    segments: list[dict[str, Any]] = []
    for index in range(len(boundaries) - 1):
        start, start_reason = boundaries[index]
        end, _ = boundaries[index + 1]
        members = [sample for sample in samples if start <= sample["time"] < end]
        if not members:
            members = [min(samples, key=lambda sample: abs(sample["time"] - (start + end) / 2))]
        midpoint = (start + end) / 2
        span = max(1.0, end - start)
        representative = max(
            members,
            key=lambda sample: sample["quality"] - 0.12 * abs(sample["time"] - midpoint) / span,
        )
        mean_change = sum(sample["visual_change"] for sample in members) / len(members)
        flags = sorted({flag for sample in members for flag in sample["flags"]})
        segments.append(
            {
                "segment_id": f"S{index + 1:03d}",
                "start": round(start, 3),
                "end": round(end, 3),
                "start_timecode": format_time(start),
                "end_timecode": format_time(end),
                "boundary_reason": start_reason,
                "representative_sample_id": representative["sample_id"],
                "representative_frame": representative["frame"],
                "representative_hash": representative["average_hash"],
                "machine": {
                    "mean_visual_change": round(mean_change, 4),
                    "quality": representative["quality"],
                    "brightness": representative["brightness"],
                    "sharpness": representative["sharpness"],
                    "flags": flags,
                },
                "transcript": [],
            }
        )
    validate_coverage(segments, duration)
    return segments


def validate_coverage(segments: list[dict[str, Any]], duration: float, tolerance: float = 0.002) -> None:
    if not segments:
        raise ValueError("Timeline has no segments")
    if abs(float(segments[0]["start"])) > tolerance:
        raise ValueError("Timeline does not start at zero")
    previous_end = 0.0
    for segment in segments:
        start = float(segment["start"])
        end = float(segment["end"])
        if abs(start - previous_end) > tolerance:
            raise ValueError(f"Timeline gap or overlap before {segment['segment_id']}")
        if end <= start:
            raise ValueError(f"Invalid range for {segment['segment_id']}")
        previous_end = end
    if abs(previous_end - duration) > tolerance:
        raise ValueError("Timeline does not reach asset end")


def build_machine_groups(segments: list[dict[str, Any]], config: Phase1Config) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for segment in segments:
        if not current:
            current = [segment]
            continue
        previous = current[-1]
        distance = hash_distance(previous["representative_hash"], segment["representative_hash"])
        group_span = float(segment["end"]) - float(current[0]["start"])
        hard_change = segment["boundary_reason"] == "visual_change" and distance > config.group_threshold
        if hard_change or group_span > 90.0:
            groups.append(current)
            current = [segment]
        else:
            current.append(segment)
    if current:
        groups.append(current)

    payload: list[dict[str, Any]] = []
    for index, members in enumerate(groups):
        group_id = f"MG{index + 1:03d}"
        for segment in members:
            segment["machine_group_id"] = group_id
        payload.append(
            {
                "group_id": group_id,
                "start": members[0]["start"],
                "end": members[-1]["end"],
                "segment_ids": [member["segment_id"] for member in members],
                "basis": "adjacent_visual_similarity",
            }
        )
    return payload


SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")


def detect_silence(source: Path, duration: float, config: Phase1Config) -> list[dict[str, float]]:
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
    completed = subprocess.run(command, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    starts = [float(value) for value in SILENCE_START.findall(completed.stderr)]
    ends = [float(value) for value in SILENCE_END.findall(completed.stderr)]
    intervals: list[dict[str, float]] = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else duration
        intervals.append({"start": round(start, 3), "end": round(min(end, duration), 3)})
    return intervals


def transcribe_mlx(
    source: Path,
    transcript_dir: Path,
    config: Phase1Config,
    *,
    language: str,
) -> dict[str, Any]:
    transcript_dir.mkdir(parents=True, exist_ok=True)
    output = transcript_dir / "transcript.json"
    if output.exists():
        return json.loads(output.read_text(encoding="utf-8"))
    if shutil.which("mlx_whisper"):
        command = ["mlx_whisper"]
    elif shutil.which("uvx"):
        command = ["uvx", "--from", "mlx-whisper", "mlx_whisper"]
    else:
        raise RuntimeError("MLX Whisper requires either mlx_whisper or uvx")
    command.extend(
        [
            str(source),
            "--model",
            config.stt_model,
            "--output-name",
            "transcript",
            "--output-dir",
            str(transcript_dir),
            "--output-format",
            "json",
            "--verbose",
            "False",
            "--condition-on-previous-text",
            "False",
        ]
    )
    command.extend(["--language", language])
    log(f"  transcribing {language} candidate with {config.stt_model}")
    run(command, capture=False)
    if not output.exists():
        raise RuntimeError(f"MLX Whisper did not create {output}")
    return json.loads(output.read_text(encoding="utf-8"))


def transcribe_language_candidates(
    source: Path,
    transcript_root: Path,
    config: Phase1Config,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    transcripts: dict[str, dict[str, Any]] = {}
    paths: dict[str, str] = {}
    for language in config.stt_languages:
        language_dir = transcript_root / language
        transcripts[language] = transcribe_mlx(source, language_dir, config, language=language)
        paths[language] = str(language_dir / "transcript.json")
    return transcripts, paths


def filter_transcript(transcript: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    previous_text = ""
    previous_end = -10.0
    for speech in transcript.get("segments", []):
        text_value = " ".join(str(speech.get("text", "")).split())
        normalized = re.sub(r"[^0-9A-Za-z가-힣]+", "", text_value).lower()
        reasons: list[str] = []
        avg_logprob = float(speech.get("avg_logprob", 0.0))
        no_speech_prob = float(speech.get("no_speech_prob", 0.0))
        compression_ratio = float(speech.get("compression_ratio", 0.0))
        start = float(speech.get("start", 0.0))
        end = float(speech.get("end", start))
        if not normalized:
            reasons.append("empty")
        if avg_logprob < -1.0:
            reasons.append("low_logprob")
        if no_speech_prob > 0.6:
            reasons.append("likely_no_speech")
        if compression_ratio > 2.4:
            reasons.append("high_repetition")
        if normalized == previous_text and start - previous_end <= 2.0:
            reasons.append("duplicate_phrase")
        item = {
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text_value,
            "avg_logprob": round(avg_logprob, 4),
            "no_speech_prob": round(no_speech_prob, 4),
            "compression_ratio": round(compression_ratio, 4),
        }
        if reasons:
            rejected.append({**item, "reasons": reasons})
        else:
            accepted.append(item)
            previous_text = normalized
            previous_end = end
    return accepted, {
        "accepted_segments": len(accepted),
        "rejected_segments": len(rejected),
        "accepted_characters": sum(len(item["text"]) for item in accepted),
        "rejected_characters": sum(len(item["text"]) for item in rejected),
        "rejection_reasons": sorted({reason for item in rejected for reason in item["reasons"]}),
        "policy": {
            "min_avg_logprob": -1.0,
            "max_no_speech_prob": 0.6,
            "max_compression_ratio": 2.4,
            "drop_adjacent_duplicate_phrases": True,
        },
    }


def attach_transcript(segments: list[dict[str, Any]], transcript: dict[str, Any]) -> dict[str, Any]:
    transcript_segments, stats = filter_transcript(transcript)
    for segment in segments:
        segment["transcript"] = []
    for segment in segments:
        overlaps: list[dict[str, Any]] = []
        for speech in transcript_segments:
            start = float(speech.get("start", 0.0))
            end = float(speech.get("end", start))
            if start < segment["end"] and end > segment["start"]:
                overlaps.append(speech)
        segment["transcript"] = overlaps
    return stats


def transcript_candidate_score(items: list[dict[str, Any]]) -> float | None:
    if not items:
        return None
    weighted_score = 0.0
    total_duration = 0.0
    for item in items:
        duration = max(0.25, float(item["end"]) - float(item["start"]))
        score = float(item.get("avg_logprob", -2.0))
        score -= max(0.0, float(item.get("no_speech_prob", 0.0)) - 0.4)
        score -= max(0.0, float(item.get("compression_ratio", 0.0)) - 2.0) * 0.15
        weighted_score += score * duration
        total_duration += duration
    return round(weighted_score / total_duration, 4)


def attach_transcript_candidates(
    segments: list[dict[str, Any]],
    transcripts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    filtered: dict[str, list[dict[str, Any]]] = {}
    stats: dict[str, Any] = {}
    for language, transcript in transcripts.items():
        filtered[language], stats[language] = filter_transcript(transcript)

    for segment in segments:
        candidates: dict[str, list[dict[str, Any]]] = {}
        scores: dict[str, float | None] = {}
        for language, speech_items in filtered.items():
            overlaps = [
                speech
                for speech in speech_items
                if float(speech["start"]) < float(segment["end"])
                and float(speech["end"]) > float(segment["start"])
            ]
            candidates[language] = overlaps
            scores[language] = transcript_candidate_score(overlaps)
        ranked = sorted(
            ((score, language) for language, score in scores.items() if score is not None),
            reverse=True,
        )
        if not ranked:
            preference = "none"
        elif len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.15:
            preference = ranked[0][1]
        else:
            preference = "mixed_or_uncertain"
        segment["transcript"] = []
        segment["transcript_candidates"] = candidates
        segment["transcript_candidate_scores"] = scores
        segment["transcript_machine_preference"] = preference
    return stats


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def create_contact_sheets(
    segments: list[dict[str, Any]],
    output_dir: Path,
    asset_key_value: str,
    config: Phase1Config,
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    tile_width, image_height, label_height = 320, 180, 42
    tile_height = image_height + label_height
    page_size = config.sheet_columns * config.sheet_rows
    sheets: list[dict[str, Any]] = []
    font = _font(16)
    small_font = _font(13)
    for page_index in range(math.ceil(len(segments) / page_size)):
        members = segments[page_index * page_size : (page_index + 1) * page_size]
        canvas = Image.new(
            "RGB",
            (config.sheet_columns * tile_width, config.sheet_rows * tile_height),
            "#111318",
        )
        draw = ImageDraw.Draw(canvas)
        cells: list[dict[str, str]] = []
        for cell_index, segment in enumerate(members):
            row, column = divmod(cell_index, config.sheet_columns)
            x, y = column * tile_width, row * tile_height
            with Image.open(segment["representative_frame"]) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                fitted = ImageOps.pad(image, (tile_width, image_height), color="#000000")
            canvas.paste(fitted, (x, y))
            cell = f"{chr(65 + row)}{column + 1}"
            label = f"{cell}  {segment['segment_id']}  {segment['start_timecode']}-{segment['end_timecode']}"
            flags = ",".join(segment["machine"]["flags"]) or "ok"
            detail = f"{segment['machine_group_id']}  q={segment['machine']['quality']:.2f}  {flags}"
            draw.rectangle((x, y + image_height, x + tile_width, y + tile_height), fill="#191d25")
            draw.text((x + 8, y + image_height + 3), label, font=font, fill="#f5f7fa")
            draw.text((x + 8, y + image_height + 23), detail, font=small_font, fill="#aab4c3")
            segment["contact_sheet"] = f"sheet_{page_index + 1:03d}.jpg"
            segment["contact_cell"] = cell
            cells.append({"cell": cell, "segment_id": segment["segment_id"]})
        sheet_path = output_dir / f"sheet_{page_index + 1:03d}.jpg"
        canvas.save(sheet_path, quality=88, optimize=True)
        sheets.append(
            {
                "sheet_id": f"{asset_key_value}-sheet-{page_index + 1:03d}",
                "path": str(sheet_path),
                "cells": cells,
            }
        )
    return sheets


def build_review_packet(machine: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    compact_segments: list[dict[str, Any]] = []
    for segment in machine["segments"]:
        transcript_candidates = {
            language: {
                "text": " ".join(item["text"] for item in items)[:500],
                "score": segment.get("transcript_candidate_scores", {}).get(language),
            }
            for language, items in segment.get("transcript_candidates", {}).items()
        }
        compact_segments.append(
            {
                "segment_id": segment["segment_id"],
                "start": segment["start"],
                "end": segment["end"],
                "timecode": f"{segment['start_timecode']}-{segment['end_timecode']}",
                "sheet": str(Path("contact_sheets") / segment["contact_sheet"]),
                "cell": segment["contact_cell"],
                "machine_group_id": segment["machine_group_id"],
                "quality": segment["machine"]["quality"],
                "flags": segment["machine"]["flags"],
                "transcript_candidates": transcript_candidates,
                "transcript_machine_preference": segment.get("transcript_machine_preference", "none"),
            }
        )
    return {
        "schema_version": "phase1-review-packet/v1",
        "asset_id": machine["asset_id"],
        "source_name": machine["source"]["name"],
        "duration": machine["media"]["duration"],
        "run_dir": str(run_dir),
        "instructions": [
            "Inspect contact sheets before opening individual frames.",
            "Keep input segment IDs and time ranges unchanged.",
            "Create contiguous reviewed groups that cover every segment exactly once.",
            "Compare Korean and English transcript candidates; do not trust machine preference alone.",
            "Use individual frames only when a sheet cell is ambiguous.",
        ],
        "segments": compact_segments,
    }


def apply_stt_candidates(
    machine: dict[str, Any],
    source: Path,
    run_dir: Path,
    config: Phase1Config,
) -> None:
    model_key = re.sub(r"[^A-Za-z0-9._-]+", "-", config.stt_model).strip("-")
    transcript_root = run_dir / "transcript" / model_key
    transcripts, transcript_paths = transcribe_language_candidates(source, transcript_root, config)
    filter_stats = attach_transcript_candidates(machine["segments"], transcripts)
    machine["config"] = asdict(config)
    machine["audio_analysis"].update(
        {
            "transcript_strategy": "parallel-language-candidates/v1",
            "transcript_strategy_digest": stt_config_digest(config),
            "transcript_paths": transcript_paths,
            "transcript_model": config.stt_model,
            "transcript_languages": list(config.stt_languages),
            "transcript_filter": filter_stats,
        }
    )


def create_html(machine: dict[str, Any], output_path: Path) -> None:
    cards: list[str] = []
    run_dir = output_path.parent
    reviewed_group_labels = {
        segment_id: group.get("label", group["group_id"])
        for group in machine.get("reviewed_groups", [])
        for segment_id in group.get("segment_ids", [])
    }
    for segment in machine["segments"]:
        frame_relative = os.path.relpath(segment["representative_frame"], run_dir)
        transcript_candidates = {
            language: " ".join(item["text"] for item in items) or "—"
            for language, items in segment.get("transcript_candidates", {}).items()
        }
        transcript_html = "".join(
            f'<p class="transcript"><strong>{html.escape(language)}</strong>: {html.escape(text)}</p>'
            for language, text in transcript_candidates.items()
        ) or '<p class="transcript">STT: —</p>'
        flags = ", ".join(segment["machine"]["flags"]) or "ok"
        review = segment.get("review", {})
        summary = review.get("visual_summary", "기계 분석만 완료 — 장면 설명 검토 전")
        group_label = reviewed_group_labels.get(segment["segment_id"], segment["machine_group_id"])
        actions = ", ".join(review.get("actions", [])) or "—"
        cards.append(
            f"""
            <article class="segment">
              <img src="{html.escape(frame_relative)}" loading="lazy" alt="{segment['segment_id']}">
              <div><strong>{segment['segment_id']}</strong> · {segment['start_timecode']}–{segment['end_timecode']}</div>
              <div class="group">{html.escape(group_label)}</div>
              <p>{html.escape(str(summary))}</p>
              <div class="muted">actions: {html.escape(actions)}</div>
              <div class="muted">quality {segment['machine']['quality']:.2f} · {html.escape(flags)}</div>
              {transcript_html}
            </article>
            """
        )
    timeline_kind = "reviewed" if machine.get("reviewed_groups") else "machine"
    document = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(machine['source']['name'])} timeline</title>
<style>
body {{ margin: 0; padding: 28px; font: 15px/1.5 -apple-system, BlinkMacSystemFont, sans-serif; color: #eef2f8; background: #0d1015; }}
h1 {{ font-size: 24px; }} .meta,.muted {{ color: #9ba8ba; }}
.timeline {{ display: grid; grid-template-columns: repeat(auto-fill,minmax(320px,1fr)); gap: 18px; }}
.segment {{ background: #171c24; border: 1px solid #293140; border-radius: 10px; overflow: hidden; padding-bottom: 12px; }}
.segment img {{ display: block; width: 100%; aspect-ratio: 16/9; object-fit: contain; background: black; }}
.segment div,.segment p {{ margin: 8px 12px 0; }}
.segment .group {{ color: #7dd3fc; font-weight: 650; }}
.segment .transcript {{ color: #c5ceda; font-size: 13px; }}
</style></head><body>
<h1>{html.escape(machine['source']['name'])}</h1>
<p class="meta">{format_time(machine['media']['duration'])} · {len(machine['segments'])} segments · {timeline_kind} timeline</p>
<main class="timeline">{''.join(cards)}</main></body></html>
"""
    output_path.write_text(document, encoding="utf-8")


def process_asset(source: Path, output_root: Path, config: Phase1Config) -> Path:
    require_tools()
    source = source.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    fingerprint = quick_fingerprint(source)
    key = asset_key(source, fingerprint)
    run_dir = output_root.expanduser().resolve() / key / config_digest(config)
    timeline_path = run_dir / "timeline.machine.json"
    if timeline_path.exists():
        machine = json.loads(timeline_path.read_text(encoding="utf-8"))
        selected_strategy = machine.get("audio_analysis", {}).get("transcript_strategy_digest")
        if config.stt == "mlx" and selected_strategy != stt_config_digest(config):
            apply_stt_candidates(machine, source, run_dir, config)
            atomic_json(timeline_path, machine)
            atomic_json(run_dir / "review-packet.json", build_review_packet(machine, run_dir))
            create_html(machine, run_dir / "timeline.html")
            log(f"updated transcript cache: {source.name} -> {run_dir}")
        else:
            log(f"cached visual stages: {source.name} -> {run_dir}")
        return run_dir

    log(f"processing: {source.name}")
    media = probe_media(source)
    run_dir.mkdir(parents=True, exist_ok=True)
    frame_paths = extract_sample_frames(source, run_dir / "frames", config)
    samples = analyze_frames(frame_paths, media["duration"], config)
    segments = build_segments(samples, media["duration"], config)
    groups = build_machine_groups(segments, config)
    silences = detect_silence(source, media["duration"], config) if media["audio"] else []
    sheets = create_contact_sheets(segments, run_dir / "contact_sheets", key, config)

    machine = {
        "schema_version": SCHEMA_VERSION,
        "asset_id": key,
        "source": {
            "name": source.name,
            "path": str(source),
            "quick_fingerprint": fingerprint,
        },
        "config": asdict(config),
        "media": media,
        "audio_analysis": {
            "silence_intervals": silences,
        },
        "samples": samples,
        "segments": segments,
        "machine_groups": groups,
        "contact_sheets": sheets,
    }
    if config.stt == "mlx":
        apply_stt_candidates(machine, source, run_dir, config)
    atomic_json(timeline_path, machine)
    atomic_json(run_dir / "review-packet.json", build_review_packet(machine, run_dir))
    create_html(machine, run_dir / "timeline.html")
    atomic_json(
        run_dir / "run.json",
        {
            "status": "complete",
            "asset_id": key,
            "config_digest": config_digest(config),
            "segments": len(segments),
            "sheets": len(sheets),
            "sample_frames": len(samples),
        },
    )
    log(f"complete: {len(segments)} segments, {len(sheets)} sheets -> {run_dir}")
    return run_dir


def process_assets(sources: Iterable[Path], output_root: Path, config: Phase1Config) -> list[Path]:
    return [process_asset(source, output_root, config) for source in sources]
