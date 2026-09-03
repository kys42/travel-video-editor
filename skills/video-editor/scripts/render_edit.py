#!/usr/bin/env python3
"""Render a reproducible local video edit from a video-edit-plan/v1 JSON file."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

SCHEMA_VERSION = "video-edit-plan/v1"
RENDER_SCHEMA_VERSION = "video-edit-render/v1"
UTC = timezone.utc  # noqa: UP017 -- keep the standalone skill compatible with Python 3.9
DEFAULT_FONT_CANDIDATES = (
    Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
    Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
)


class PlanError(ValueError):
    pass


def _run_json(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise PlanError(f"Command failed ({completed.returncode}): {detail}")
    return json.loads(completed.stdout)


def probe_media(path: Path) -> dict[str, Any]:
    payload = _run_json(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,size,format_name:"
                "stream=index,codec_type,codec_name,width,height,avg_frame_rate,"
                "sample_rate,channels"
            ),
            "-of",
            "json",
            str(path),
        ]
    )
    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not video:
        raise PlanError(f"No video stream: {path}")
    fmt = payload.get("format", {})
    duration = float(fmt.get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise PlanError(f"Invalid duration: {path}")
    return {
        "path": str(path),
        "duration": duration,
        "size_bytes": int(fmt.get("size") or path.stat().st_size),
        "format_name": fmt.get("format_name"),
        "video": {
            "codec": video.get("codec_name"),
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "frame_rate": video.get("avg_frame_rate"),
        },
        "audio": (
            {
                "codec": audio.get("codec_name"),
                "sample_rate": int(audio.get("sample_rate") or 0),
                "channels": int(audio.get("channels") or 0),
            }
            if audio
            else None
        ),
    }


def _positive_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{label} must be a number") from exc
    if not math.isfinite(number) or number <= 0:
        raise PlanError(f"{label} must be positive")
    return number


def _time_value(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PlanError(f"{label} must be a number") from exc
    if not math.isfinite(number) or number < 0:
        raise PlanError(f"{label} must be zero or positive")
    return number


def _fraction(value: Any, label: str) -> Fraction:
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise PlanError(f"{label} must be a positive number or fraction") from exc
    if result <= 0:
        raise PlanError(f"{label} must be positive")
    return result


def _encoder_available(name: str) -> bool:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0 and name in completed.stdout


def _resolve_font(configured: str | None) -> Path:
    candidates = (
        (Path(configured).expanduser(),) if configured else DEFAULT_FONT_CANDIDATES
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise PlanError("No usable font found; set output.font_file in the edit plan")


def _choose_media(clip: dict[str, Any], media_mode: str) -> Path:
    source = Path(str(clip.get("source", ""))).expanduser()
    proxy_value = clip.get("proxy")
    proxy = Path(str(proxy_value)).expanduser() if proxy_value else None
    if media_mode == "source":
        selected = source
    elif media_mode == "proxy":
        if proxy is None or not proxy.is_file():
            raise PlanError(
                f"Proxy is required but missing for clip {clip.get('id')}: {proxy}"
            )
        selected = proxy
    else:
        selected = proxy if proxy is not None and proxy.is_file() else source
    if not selected.is_file():
        raise PlanError(f"Media is missing for clip {clip.get('id')}: {selected}")
    return selected.resolve()


def normalize_plan(
    plan: dict[str, Any], plan_path: Path, output_path: Path, media_mode: str
) -> dict[str, Any]:
    if plan.get("schema_version") != SCHEMA_VERSION:
        raise PlanError(f"schema_version must be {SCHEMA_VERSION}")
    clips = plan.get("clips")
    if not isinstance(clips, list) or not clips:
        raise PlanError("clips must be a non-empty list")

    output_config = dict(plan.get("output") or {})
    width = int(output_config.get("width", 1920))
    height = int(output_config.get("height", 1080))
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise PlanError("output width and height must be positive even integers")
    fps = _fraction(output_config.get("fps", "30000/1001"), "output.fps")
    loudness = float(output_config.get("loudness_lufs", -16))
    if not -70 <= loudness <= -5:
        raise PlanError("output.loudness_lufs must be between -70 and -5")
    requested_encoder = output_config.get("video_encoder")
    if requested_encoder:
        encoder = str(requested_encoder)
        if not _encoder_available(encoder):
            raise PlanError(f"Requested video encoder is unavailable: {encoder}")
    else:
        encoder = (
            "h264_videotoolbox"
            if _encoder_available("h264_videotoolbox")
            else "libx264"
        )

    normalized_output = {
        "width": width,
        "height": height,
        "fps": str(fps),
        "video_encoder": encoder,
        "video_bitrate": str(output_config.get("video_bitrate", "8M")),
        "crf": int(output_config.get("crf", 20)),
        "audio_bitrate": str(output_config.get("audio_bitrate", "192k")),
        "loudness_lufs": loudness,
        "fade_in": _time_value(output_config.get("fade_in", 0.25), "output.fade_in"),
        "fade_out": _time_value(output_config.get("fade_out", 0.5), "output.fade_out"),
        "font_file": str(_resolve_font(output_config.get("font_file"))),
        "caption_font_size": int(
            output_config.get("caption_font_size", round(height * 0.044))
        ),
        "title_font_size": int(
            output_config.get("title_font_size", round(height * 0.065))
        ),
        "label_font_size": int(
            output_config.get("label_font_size", round(height * 0.034))
        ),
    }
    for key in ("caption_font_size", "title_font_size", "label_font_size"):
        if normalized_output[key] <= 0:
            raise PlanError(f"output.{key} must be positive")

    probe_cache: dict[Path, dict[str, Any]] = {}

    def cached_probe(path: Path) -> dict[str, Any]:
        if path not in probe_cache:
            probe_cache[path] = probe_media(path)
        return probe_cache[path]

    normalized_clips: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    cursor = 0.0
    resolved_output = output_path.resolve()
    for index, raw in enumerate(clips, start=1):
        if not isinstance(raw, dict):
            raise PlanError(f"clip {index} must be an object")
        clip_id = str(raw.get("id") or f"C{index:03d}")
        if clip_id in seen_ids:
            raise PlanError(f"Duplicate clip id: {clip_id}")
        seen_ids.add(clip_id)
        source_value = raw.get("source")
        if not source_value:
            raise PlanError(f"clip {clip_id} is missing source")
        source = Path(str(source_value)).expanduser()
        if not source.is_file():
            raise PlanError(f"Source is missing for clip {clip_id}: {source}")
        source = source.resolve()
        if source == resolved_output:
            raise PlanError(f"Output would overwrite source: {source}")
        selected = _choose_media(raw, media_mode)
        if selected == resolved_output:
            raise PlanError(f"Output would overwrite selected media: {selected}")
        source_probe = cached_probe(source)
        selected_probe = cached_probe(selected)
        source_in = _time_value(raw.get("source_in", 0), f"{clip_id}.source_in")
        source_out = _positive_number(raw.get("source_out"), f"{clip_id}.source_out")
        if source_out <= source_in:
            raise PlanError(f"{clip_id}.source_out must be greater than source_in")
        if source_out > source_probe["duration"] + 0.2:
            raise PlanError(
                f"{clip_id} range ends after source duration "
                f"({source_out:.3f} > {source_probe['duration']:.3f})"
            )
        if source_out > selected_probe["duration"] + 0.2:
            raise PlanError(
                f"{clip_id} range ends after selected media duration "
                f"({source_out:.3f} > {selected_probe['duration']:.3f})"
            )
        speed = float(raw.get("speed", 1.0))
        if not math.isfinite(speed) or not 0.5 <= speed <= 2.0:
            raise PlanError(f"{clip_id}.speed must be between 0.5 and 2.0")
        volume_db = float(raw.get("volume_db", 0.0))
        if not math.isfinite(volume_db) or not -60 <= volume_db <= 24:
            raise PlanError(f"{clip_id}.volume_db must be between -60 and 24")
        source_duration = source_out - source_in
        output_duration = source_duration / speed
        normalized_clips.append(
            {
                "id": clip_id,
                "source": str(source),
                "proxy": str(Path(str(raw["proxy"])).expanduser())
                if raw.get("proxy")
                else None,
                "selected_media": str(selected),
                "media_mode": "proxy" if selected != source else "source",
                "source_in": round(source_in, 6),
                "source_out": round(source_out, 6),
                "source_duration": round(source_duration, 6),
                "output_in": round(cursor, 6),
                "output_out": round(cursor + output_duration, 6),
                "output_duration": round(output_duration, 6),
                "speed": speed,
                "volume_db": volume_db,
                "label": str(raw.get("label") or ""),
                "reason": str(raw.get("reason") or ""),
                "metadata": dict(raw.get("metadata") or {}),
                "source_probe": source_probe,
                "selected_probe": selected_probe,
            }
        )
        cursor += output_duration

    total_duration = round(cursor, 6)

    def normalize_events(items: Any, event_type: str) -> list[dict[str, Any]]:
        if items is None:
            return []
        if not isinstance(items, list):
            raise PlanError(f"{event_type} must be a list")
        result: list[dict[str, Any]] = []
        for index, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                raise PlanError(f"{event_type} item {index} must be an object")
            start = _time_value(raw.get("start"), f"{event_type}[{index}].start")
            end = _positive_number(raw.get("end"), f"{event_type}[{index}].end")
            text = str(raw.get("text") or "").strip()
            if not text:
                raise PlanError(f"{event_type}[{index}].text is required")
            if end <= start or end > total_duration + 0.2:
                raise PlanError(
                    f"Invalid {event_type} range {start:.3f}-{end:.3f}; "
                    f"timeline is {total_duration:.3f}s"
                )
            style = (
                "subtitle"
                if event_type == "captions"
                else str(raw.get("style", "label"))
            )
            if style not in {"title", "label", "subtitle"}:
                raise PlanError(f"Unsupported overlay style: {style}")
            event = {
                "start": round(start, 6),
                "end": round(end, 6),
                "text": text,
                "style": style,
            }
            if event_type == "captions":
                event["kind"] = str(raw.get("kind", "stt"))
            result.append(event)
        return result

    captions = normalize_events(plan.get("captions"), "captions")
    overlays = normalize_events(plan.get("overlays"), "overlays")
    if normalized_output["fade_in"] > total_duration:
        raise PlanError("fade_in exceeds timeline duration")
    if normalized_output["fade_out"] > total_duration:
        raise PlanError("fade_out exceeds timeline duration")

    return {
        "schema_version": RENDER_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "plan_path": str(plan_path.resolve()),
        "plan_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "title": str(plan.get("title") or output_path.stem),
        "description": str(plan.get("description") or ""),
        "media_mode": media_mode,
        "output_path": str(output_path.resolve()),
        "output": normalized_output,
        "timeline_duration": total_duration,
        "clips": normalized_clips,
        "captions": captions,
        "overlays": overlays,
        "notes": list(plan.get("notes") or []),
        "provenance": dict(plan.get("provenance") or {}),
    }


def _wrap_text(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> str:
    lines: list[str] = []
    for paragraph in text.splitlines() or [text]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if (
                draw.textbbox((0, 0), candidate, font=font, stroke_width=2)[2]
                <= max_width
            ):
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return "\n".join(lines)


def render_overlay_image(
    event: dict[str, Any], output: dict[str, Any], path: Path
) -> None:
    width, height = output["width"], output["height"]
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    style = event["style"]
    if style == "title":
        size = output["title_font_size"]
        max_width = round(width * 0.78)
        x, y = round(width * 0.055), round(height * 0.075)
        box_color = (12, 15, 18, 218)
        accent = True
    elif style == "label":
        size = output["label_font_size"]
        max_width = round(width * 0.72)
        x, y = round(width * 0.045), round(height * 0.07)
        box_color = (10, 12, 14, 188)
        accent = True
    else:
        size = output["caption_font_size"]
        max_width = round(width * 0.82)
        x, y = 0, 0
        box_color = (5, 7, 9, 205)
        accent = False
    font = ImageFont.truetype(output["font_file"], size)
    wrapped = _wrap_text(draw, event["text"], font, max_width)
    spacing = max(8, round(size * 0.22))
    bbox = draw.multiline_textbbox(
        (0, 0), wrapped, font=font, spacing=spacing, stroke_width=2
    )
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    pad_x = round(size * 0.58)
    pad_y = round(size * 0.36)
    if style == "subtitle":
        x = round((width - text_width) / 2)
        y = height - round(height * 0.085) - text_height - pad_y
    box = (
        x - pad_x,
        y - pad_y,
        x + text_width + pad_x,
        y + text_height + pad_y,
    )
    radius = max(10, round(size * 0.3))
    draw.rounded_rectangle(box, radius=radius, fill=box_color)
    if accent:
        bar_width = max(6, round(size * 0.11))
        draw.rounded_rectangle(
            (box[0], box[1], box[0] + bar_width, box[3]),
            radius=max(3, bar_width // 2),
            fill=(255, 123, 53, 255),
        )
    draw.multiline_text(
        (x, y - bbox[1]),
        wrapped,
        font=font,
        fill=(255, 255, 255, 255),
        spacing=spacing,
        stroke_width=2,
        stroke_fill=(0, 0, 0, 230),
    )
    canvas.save(path)


def _atempo(speed: float) -> str:
    return f"atempo={speed:.8f}"


def build_command(
    normalized: dict[str, Any], partial: Path, overlay_paths: list[Path]
) -> list[str]:
    clips = normalized["clips"]
    output = normalized["output"]
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    for clip in clips:
        command.extend(
            [
                "-ss",
                f"{clip['source_in']:.6f}",
                "-t",
                f"{clip['source_duration']:.6f}",
                "-i",
                clip["selected_media"],
            ]
        )
    for path in overlay_paths:
        command.extend(["-loop", "1", "-framerate", output["fps"], "-i", str(path)])

    graph: list[str] = []
    concat_inputs: list[str] = []
    width, height, fps = output["width"], output["height"], output["fps"]
    for index, clip in enumerate(clips):
        duration = clip["source_duration"]
        speed = clip["speed"]
        video_filters = [
            f"trim=duration={duration:.6f}",
            (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
                "flags=lanczos"
            ),
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
            f"fps={fps}",
            "setsar=1",
            f"setpts=(PTS-STARTPTS)/{speed:.8f}",
            "format=yuv420p",
        ]
        graph.append(f"[{index}:v:0]{','.join(video_filters)}[v{index}]")
        if clip["selected_probe"]["audio"]:
            audio_filters = [
                f"atrim=duration={duration:.6f}",
                "aresample=48000:async=1:first_pts=0",
                "aformat=sample_rates=48000:channel_layouts=stereo",
                "asetpts=PTS-STARTPTS",
            ]
            if speed != 1.0:
                audio_filters.append(_atempo(speed))
            if clip["volume_db"] != 0.0:
                audio_filters.append(f"volume={clip['volume_db']:.3f}dB")
            graph.append(f"[{index}:a:0]{','.join(audio_filters)}[a{index}]")
        else:
            graph.append(
                "anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=duration={clip['output_duration']:.6f},"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
        concat_inputs.append(f"[v{index}][a{index}]")
    graph.append(f"{''.join(concat_inputs)}concat=n={len(clips)}:v=1:a=1[vcat][acat]")

    current_video = "vcat"
    all_events = normalized["overlays"] + normalized["captions"]
    image_start_index = len(clips)
    for index, event in enumerate(all_events):
        image_input = image_start_index + index
        graph.append(f"[{image_input}:v]format=rgba[overlay{index}]")
        next_video = f"vov{index}"
        graph.append(
            f"[{current_video}][overlay{index}]overlay=0:0:format=auto:"
            "eof_action=repeat:shortest=0:"
            f"enable=between(t\\,{event['start']:.6f}\\,{event['end']:.6f})"
            f"[{next_video}]"
        )
        current_video = next_video

    total = normalized["timeline_duration"]
    video_tail: list[str] = []
    if output["fade_in"] > 0:
        video_tail.append(f"fade=t=in:st=0:d={output['fade_in']:.6f}")
    if output["fade_out"] > 0:
        video_tail.append(
            f"fade=t=out:st={max(0.0, total - output['fade_out']):.6f}:"
            f"d={output['fade_out']:.6f}"
        )
    video_tail.append("format=yuv420p")
    graph.append(f"[{current_video}]{','.join(video_tail)}[vout]")

    audio_tail = [
        f"loudnorm=I={output['loudness_lufs']}:TP=-1.5:LRA=11",
        "aresample=48000",
    ]
    if output["fade_in"] > 0:
        audio_tail.append(f"afade=t=in:st=0:d={output['fade_in']:.6f}")
    if output["fade_out"] > 0:
        audio_tail.append(
            f"afade=t=out:st={max(0.0, total - output['fade_out']):.6f}:"
            f"d={output['fade_out']:.6f}"
        )
    graph.append(f"[acat]{','.join(audio_tail)}[aout]")

    command.extend(
        [
            "-filter_complex",
            ";".join(graph),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-write_tmcd",
            "0",
        ]
    )
    encoder = output["video_encoder"]
    command.extend(["-c:v", encoder, "-pix_fmt", "yuv420p"])
    if encoder == "libx264":
        command.extend(["-preset", "medium", "-crf", str(output["crf"])])
    else:
        command.extend(
            [
                "-profile:v",
                "high",
                "-b:v",
                output["video_bitrate"],
                "-maxrate",
                output["video_bitrate"],
                "-bufsize",
                output["video_bitrate"],
            ]
        )
    command.extend(
        [
            "-c:a",
            "aac",
            "-b:a",
            output["audio_bitrate"],
            "-movflags",
            "+faststart",
            "-t",
            f"{total:.6f}",
            str(partial),
        ]
    )
    return command


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(captions: list[dict[str, Any]], path: Path) -> None:
    blocks = []
    for index, caption in enumerate(captions, start=1):
        blocks.append(
            f"{index}\n{_srt_time(caption['start'])} --> {_srt_time(caption['end'])}\n"
            f"{caption['text']}"
        )
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")


def validate_output(path: Path, normalized: dict[str, Any]) -> dict[str, Any]:
    probe = probe_media(path)
    expected = normalized["output"]
    errors: list[str] = []
    if probe["video"]["codec"] != "h264":
        errors.append("video codec is not H.264")
    if (probe["video"]["width"], probe["video"]["height"]) != (
        expected["width"],
        expected["height"],
    ):
        errors.append("output dimensions do not match the plan")
    if not probe["audio"]:
        errors.append("output has no audio stream")
    duration_error = abs(probe["duration"] - normalized["timeline_duration"])
    if duration_error > max(0.6, normalized["timeline_duration"] * 0.01):
        errors.append(
            f"duration differs by {duration_error:.3f}s "
            f"({probe['duration']:.3f}s rendered)"
        )
    if path.stat().st_size < 10_000:
        errors.append("output file is unexpectedly small")
    if errors:
        raise PlanError("Output validation failed: " + "; ".join(errors))
    return probe


def _sidecar(output: Path, suffix: str) -> Path:
    return output.with_name(f"{output.stem}{suffix}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--media-mode", choices=("auto", "proxy", "source"), default="auto"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--keep-work", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan_path = args.plan.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    if not plan_path.is_file():
        print(f"Plan not found: {plan_path}", file=sys.stderr)
        return 2
    if output_path.suffix.lower() not in {".mp4", ".mov"}:
        print("Output must use .mp4 or .mov", file=sys.stderr)
        return 2
    output_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output_path.with_name(f"{output_path.stem}.partial{output_path.suffix}")
    srt_path = _sidecar(output_path, ".srt")
    manifest_path = _sidecar(output_path, ".render.json")
    log_path = _sidecar(output_path, ".render.log")
    protected = (output_path, partial, srt_path, manifest_path, log_path)
    existing = [path for path in protected if path.exists()]
    if existing and not args.overwrite and not args.dry_run:
        print(
            "Refusing existing output artifacts without --overwrite:", file=sys.stderr
        )
        for path in existing:
            print(f"  {path}", file=sys.stderr)
        return 2
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        normalized = normalize_plan(plan, plan_path, output_path, args.media_mode)
    except (json.JSONDecodeError, OSError, PlanError, ValueError) as exc:
        print(f"Plan validation failed: {exc}", file=sys.stderr)
        return 2

    work_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_path.stem}-", dir=str(output_path.parent))
    )
    all_events = normalized["overlays"] + normalized["captions"]
    overlay_paths: list[Path] = []
    try:
        for index, event in enumerate(all_events):
            path = work_dir / f"overlay-{index:03d}.png"
            render_overlay_image(event, normalized["output"], path)
            overlay_paths.append(path)
        command = build_command(normalized, partial, overlay_paths)
        normalized["ffmpeg_command"] = command
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "title": normalized["title"],
                        "timeline_duration": normalized["timeline_duration"],
                        "clip_count": len(normalized["clips"]),
                        "caption_count": len(normalized["captions"]),
                        "overlay_count": len(normalized["overlays"]),
                        "media_modes": sorted(
                            {clip["media_mode"] for clip in normalized["clips"]}
                        ),
                        "output": normalized["output"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            print("\n" + shlex.join(command))
            return 0
        if partial.exists():
            partial.unlink()
        print(
            f"Rendering {len(normalized['clips'])} clips / "
            f"{normalized['timeline_duration']:.3f}s -> {output_path}",
            flush=True,
        )
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                command, check=False, stdout=log, stderr=subprocess.STDOUT
            )
        if completed.returncode:
            raise PlanError(
                f"FFmpeg failed with exit code {completed.returncode}; see {log_path}"
            )
        output_probe = validate_output(partial, normalized)
        output_probe["path"] = str(output_path)
        normalized["output_probe"] = output_probe
        normalized["completed_at"] = datetime.now(UTC).isoformat()
        normalized["status"] = "completed"
        write_srt(normalized["captions"], srt_path)
        manifest_path.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(partial, output_path)
        print(
            f"Completed: {output_path} "
            f"({output_probe['duration']:.3f}s, {output_probe['size_bytes']} bytes)",
            flush=True,
        )
        print(f"Captions: {srt_path}", flush=True)
        print(f"Manifest: {manifest_path}", flush=True)
        return 0
    except (OSError, PlanError) as exc:
        print(f"Render failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.keep_work:
            print(f"Work directory retained: {work_dir}", flush=True)
        else:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
