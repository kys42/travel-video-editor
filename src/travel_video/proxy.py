from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from .phase1 import atomic_json, probe_media, quick_fingerprint

PROXY_PIPELINE_VERSION = "h264-videotoolbox-proxy/v1"
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v"}


@dataclass(frozen=True)
class ProxyConfig:
    max_dimension: int = 1920
    bitrate_kbps: int = 6000
    high_fps_bitrate_kbps: int = 8000
    high_fps_threshold: float = 45.0
    keyframe_interval: float = 2.0
    encoder: str = "h264_videotoolbox"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _frame_rate(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    return float(Fraction(value))


def discover_videos(source_root: Path) -> list[Path]:
    return sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file()
        and path.suffix.lower() in VIDEO_SUFFIXES
        and not path.name.startswith("._")
    )


def proxy_destination(source: Path, source_root: Path, output_root: Path) -> Path:
    return (output_root / source.relative_to(source_root)).with_suffix(".mp4")


def build_ffmpeg_proxy_command(
    source: Path,
    destination: Path,
    config: ProxyConfig,
    source_probe: dict[str, Any],
) -> list[str]:
    fps = _frame_rate(source_probe.get("video", {}).get("frame_rate")) or 30.0
    bitrate = (
        config.high_fps_bitrate_kbps
        if fps >= config.high_fps_threshold
        else config.bitrate_kbps
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-sn",
        "-vf",
        f"scale='min({config.max_dimension},iw)':-2:flags=lanczos",
        "-c:v",
        config.encoder,
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-b:v",
        f"{bitrate}k",
        "-maxrate",
        f"{round(bitrate * 1.35)}k",
        "-bufsize",
        f"{bitrate * 2}k",
        "-force_key_frames",
        f"expr:gte(t,n_forced*{config.keyframe_interval:g})",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart+use_metadata_tags",
        str(destination),
    ]


def validate_proxy(
    source_probe: dict[str, Any],
    proxy_probe: dict[str, Any],
    config: ProxyConfig,
) -> list[str]:
    errors: list[str] = []
    video = proxy_probe.get("video") or {}
    if video.get("codec") != "h264":
        errors.append("proxy_video_codec_is_not_h264")
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if not width or not height or max(width, height) > config.max_dimension:
        errors.append("proxy_dimensions_invalid")
    source_duration = float(source_probe["duration"])
    proxy_duration = float(proxy_probe["duration"])
    if abs(source_duration - proxy_duration) > max(0.5, source_duration * 0.001):
        errors.append("duration_mismatch")
    source_fps = _frame_rate(source_probe.get("video", {}).get("frame_rate"))
    proxy_fps = _frame_rate(video.get("frame_rate"))
    if source_fps and proxy_fps and abs(source_fps - proxy_fps) > 0.02:
        errors.append("frame_rate_mismatch")
    if bool(source_probe.get("audio")) != bool(proxy_probe.get("audio")):
        errors.append("audio_stream_mismatch")
    return errors


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_summary(
    path: Path,
    *,
    source_root: Path,
    output_root: Path,
    config: ProxyConfig,
    total: int,
    completed: int,
    reused: int,
    failed: int,
    invalid: int,
    source_bytes: int,
    proxy_bytes: int,
    current_source: Path | None,
) -> None:
    atomic_json(
        path,
        {
            "schema_version": "proxy-batch-summary/v1",
            "pipeline_version": PROXY_PIPELINE_VERSION,
            "updated_at": _now(),
            "source_root": str(source_root),
            "output_root": str(output_root),
            "config": asdict(config),
            "total_files": total,
            "completed_files": completed,
            "reused_files": reused,
            "failed_files": failed,
            "invalid_source_files": invalid,
            "processed_files": completed + reused + failed + invalid,
            "source_bytes_completed": source_bytes,
            "proxy_bytes_completed": proxy_bytes,
            "compression_ratio": round(proxy_bytes / source_bytes, 4)
            if source_bytes
            else None,
            "current_source": str(current_source) if current_source else None,
        },
    )


def build_proxy_batch(
    source_root: Path,
    output_root: Path,
    config: ProxyConfig,
    *,
    limit: int | None = None,
) -> Path:
    source_root = source_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if not source_root.is_dir():
        raise NotADirectoryError(source_root)
    if source_root == output_root or source_root in output_root.parents:
        raise ValueError("Proxy output must not be inside the source tree")
    if config.max_dimension <= 0 or config.bitrate_kbps <= 0:
        raise ValueError("Proxy dimensions and bitrate must be positive")

    sources = discover_videos(source_root)
    if limit is not None:
        sources = sources[:limit]
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "proxy-manifest.jsonl"
    summary_path = output_root / "proxy-summary.json"
    logs_root = output_root / "logs"
    completed = reused = failed = invalid = 0
    source_bytes = proxy_bytes = 0

    for index, source in enumerate(sources, start=1):
        destination = proxy_destination(source, source_root, output_root)
        relative = str(source.relative_to(source_root))
        print(f"[{index}/{len(sources)}] {relative}", file=sys.stderr, flush=True)
        _write_summary(
            summary_path,
            source_root=source_root,
            output_root=output_root,
            config=config,
            total=len(sources),
            completed=completed,
            reused=reused,
            failed=failed,
            invalid=invalid,
            source_bytes=source_bytes,
            proxy_bytes=proxy_bytes,
            current_source=source,
        )
        try:
            source_probe = probe_media(source)
        except Exception as exc:  # noqa: BLE001 - batch records invalid inputs and continues.
            invalid += 1
            _append_event(
                manifest_path,
                {
                    "event": "invalid_source",
                    "time": _now(),
                    "source": str(source),
                    "relative_path": relative,
                    "error": str(exc),
                },
            )
            continue

        if destination.exists():
            try:
                existing_probe = probe_media(destination)
                validation_errors = validate_proxy(source_probe, existing_probe, config)
            except Exception as exc:  # noqa: BLE001 - existing output is never overwritten silently.
                raise RuntimeError(f"Existing proxy is unreadable: {destination}: {exc}") from exc
            if validation_errors:
                raise RuntimeError(
                    f"Existing proxy failed validation: {destination}: {validation_errors}"
                )
            reused += 1
            source_bytes += source.stat().st_size
            proxy_bytes += destination.stat().st_size
            _append_event(
                manifest_path,
                {
                    "event": "reused",
                    "time": _now(),
                    "source": str(source),
                    "proxy": str(destination),
                    "relative_path": relative,
                    "source_probe": source_probe,
                    "proxy_probe": existing_probe,
                },
            )
            continue

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f"{destination.stem}.partial{destination.suffix}")
        log_path = logs_root / Path(relative).with_suffix(".log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fingerprint = quick_fingerprint(source)
        started_at = _now()
        command = build_ffmpeg_proxy_command(source, temporary, config, source_probe)
        _append_event(
            manifest_path,
            {
                "event": "started",
                "time": started_at,
                "source": str(source),
                "proxy": str(destination),
                "temporary": str(temporary),
                "relative_path": relative,
                "source_quick_fingerprint": fingerprint,
                "source_probe": source_probe,
                "command": command,
                "pipeline_version": PROXY_PIPELINE_VERSION,
                "config": asdict(config),
            },
        )
        try:
            with log_path.open("w", encoding="utf-8") as log_stream:
                completed_process = subprocess.run(
                    command,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                )
            if completed_process.returncode:
                raise RuntimeError(f"ffmpeg exited with {completed_process.returncode}")
            proxy_probe = probe_media(temporary)
            validation_errors = validate_proxy(source_probe, proxy_probe, config)
            if validation_errors:
                raise RuntimeError(f"proxy validation failed: {validation_errors}")
            os.replace(temporary, destination)
            completed += 1
            source_bytes += source.stat().st_size
            proxy_bytes += destination.stat().st_size
            _append_event(
                manifest_path,
                {
                    "event": "completed",
                    "time": _now(),
                    "started_at": started_at,
                    "source": str(source),
                    "proxy": str(destination),
                    "relative_path": relative,
                    "source_quick_fingerprint": fingerprint,
                    "proxy_quick_fingerprint": quick_fingerprint(destination),
                    "source_probe": source_probe,
                    "proxy_probe": proxy_probe,
                    "source_bytes": source.stat().st_size,
                    "proxy_bytes": destination.stat().st_size,
                    "log": str(log_path),
                },
            )
        except Exception as exc:  # noqa: BLE001 - a batch must continue after one corrupt asset.
            failed += 1
            _append_event(
                manifest_path,
                {
                    "event": "failed",
                    "time": _now(),
                    "started_at": started_at,
                    "source": str(source),
                    "proxy": str(destination),
                    "temporary": str(temporary),
                    "relative_path": relative,
                    "error": str(exc),
                    "log": str(log_path),
                },
            )
            print(f"  failed: {exc}", file=sys.stderr, flush=True)

    _write_summary(
        summary_path,
        source_root=source_root,
        output_root=output_root,
        config=config,
        total=len(sources),
        completed=completed,
        reused=reused,
        failed=failed,
        invalid=invalid,
        source_bytes=source_bytes,
        proxy_bytes=proxy_bytes,
        current_source=None,
    )
    return summary_path
