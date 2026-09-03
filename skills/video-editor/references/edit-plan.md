# Video edit plan v1

Use a JSON document so clip selection, captions, and render settings remain reviewable independently of the rendered file.

## Minimal plan

```json
{
  "schema_version": "video-edit-plan/v1",
  "title": "Example highlight",
  "output": {
    "width": 1920,
    "height": 1080,
    "fps": "30000/1001",
    "video_bitrate": "8M",
    "audio_bitrate": "192k",
    "loudness_lufs": -16
  },
  "clips": [
    {
      "id": "C001",
      "source": "/absolute/path/original.mp4",
      "proxy": "/absolute/path/proxy.mp4",
      "source_in": 12.5,
      "source_out": 18.0,
      "label": "Reaction",
      "reason": "Reviewed candid moment",
      "speed": 1.0,
      "volume_db": 0.0
    }
  ],
  "captions": [
    {
      "start": 0.4,
      "end": 3.0,
      "text": "Caption on the output timeline",
      "kind": "verified"
    }
  ],
  "overlays": [
    {
      "start": 0.2,
      "end": 2.8,
      "text": "Location · Day 1",
      "style": "title"
    }
  ]
}
```

## Semantics

- `source` is the authoritative media path. It is required and must never be the output path.
- `proxy` is optional. `--media-mode auto` prefers it when it exists; `proxy` requires it; `source` ignores it.
- `source_in` and `source_out` use seconds on the authoritative source timeline. The renderer verifies the range against the selected file and writes computed output ranges to the render manifest.
- `speed` defaults to `1.0` and accepts `0.5` through `2.0`. Output duration is `(source_out - source_in) / speed`.
- `volume_db` defaults to `0.0` and is applied before final loudness normalization.
- `metadata` may carry reviewed scene IDs, notable-moment IDs, or other selection provenance. It is copied to the render manifest.
- `captions` use seconds on the assembled output timeline and are both burned into the picture and exported to SRT.
- Caption `kind` is provenance metadata: use `verified`, `stt`, or `paraphrase`. The renderer preserves it in the normalized manifest but does not change visual style automatically.
- `overlays` use the assembled output timeline. Supported styles are `title`, `label`, and `subtitle`.
- Top-level `provenance` may point to input timelines, transcript revisions, or an approval record. It is preserved unchanged in the render manifest.

## Output settings

`output` may contain:

- `width`, `height`: even positive integers; defaults to 1920×1080.
- `fps`: positive number or fraction; defaults to `30000/1001`.
- `video_encoder`: defaults to `h264_videotoolbox` when available, otherwise `libx264`.
- `video_bitrate`: defaults to `8M` for VideoToolbox.
- `crf`: defaults to 20 for libx264.
- `audio_bitrate`: defaults to `192k`.
- `loudness_lufs`: defaults to -16.
- `fade_in`, `fade_out`: assembled timeline fade durations; defaults to 0.25 and 0.5 seconds.
- `font_file`: optional TrueType/OpenType/TTC path. On macOS the renderer prefers Apple SD Gothic Neo for Korean text.
- `caption_font_size`, `title_font_size`, `label_font_size`: optional pixel sizes.

## Generated files

For `highlight.mp4`, the renderer writes:

- `highlight.mp4`: validated H.264/AAC output.
- `highlight.srt`: caption sidecar, even when captions are burned in.
- `highlight.render.json`: normalized clip timeline, source probes, render command, output probe, and plan provenance.

The video is first written as `highlight.partial.mp4` and moved into place only after validation succeeds.
