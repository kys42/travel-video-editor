from pathlib import Path

from travel_video.proxy import (
    ProxyConfig,
    build_ffmpeg_proxy_command,
    proxy_destination,
    validate_proxy,
)


def media_probe(*, duration: float = 10.0, fps: str = "30000/1001") -> dict:
    return {
        "duration": duration,
        "video": {
            "codec": "hevc",
            "width": 3840,
            "height": 2160,
            "frame_rate": fps,
        },
        "audio": {"codec": "aac"},
    }


def test_proxy_destination_preserves_relative_tree() -> None:
    source_root = Path("/source")
    source = source_root / "0829" / "clip.MOV"
    assert proxy_destination(source, source_root, Path("/proxy")) == Path(
        "/proxy/0829/clip.mp4"
    )


def test_proxy_command_uses_higher_bitrate_for_high_frame_rate() -> None:
    command = build_ffmpeg_proxy_command(
        Path("input.mp4"),
        Path("output.mp4"),
        ProxyConfig(),
        media_probe(fps="60000/1001"),
    )
    assert "h264_videotoolbox" in command
    assert "8000k" in command
    assert (
        "scale=w='min(1920,iw)':h='min(1920,ih)'"
        ":force_original_aspect_ratio=decrease"
        ":force_divisible_by=2:flags=lanczos"
    ) in command
    assert "+faststart+use_metadata_tags" in command


def test_proxy_command_constrains_both_dimensions_for_portrait_video() -> None:
    command = build_ffmpeg_proxy_command(
        Path("portrait.mp4"),
        Path("portrait-proxy.mp4"),
        ProxyConfig(),
        media_probe(),
    )

    scale_filter = command[command.index("-vf") + 1]
    assert "min(1920,iw)" in scale_filter
    assert "min(1920,ih)" in scale_filter
    assert "force_original_aspect_ratio=decrease" in scale_filter
    assert "force_divisible_by=2" in scale_filter


def test_proxy_validation_accepts_matching_1080p_h264() -> None:
    source = media_probe()
    proxy = {
        "duration": 10.05,
        "video": {
            "codec": "h264",
            "width": 1920,
            "height": 1080,
            "frame_rate": "30000/1001",
        },
        "audio": {"codec": "aac"},
    }
    assert validate_proxy(source, proxy, ProxyConfig()) == []


def test_proxy_validation_rejects_drift_and_wrong_dimensions() -> None:
    source = media_probe()
    proxy = {
        "duration": 8.0,
        "video": {
            "codec": "h264",
            "width": 2560,
            "height": 1440,
            "frame_rate": "24/1",
        },
        "audio": None,
    }
    assert set(validate_proxy(source, proxy, ProxyConfig())) == {
        "proxy_dimensions_invalid",
        "duration_mismatch",
        "frame_rate_mismatch",
        "audio_stream_mismatch",
    }
