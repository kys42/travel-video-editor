# Multimodal boundary producer pilot

Date: 2026-09-04 (Asia/Seoul)

This pilot verifies the executable producer path, not only the JSON contract. It ran
against the existing 1080p H.264 processing proxy for
`DJI_20260826073820_0008_D.mp4` and its matching reviewed timeline, Apple dual-locale
transcript, and identity proxy lineage.

## Result

| Measure | Result |
| --- | ---: |
| Source duration | 58.058 s |
| Apple Vision cadence | ~3 fps |
| Apple Vision samples | 175 |
| OCR cadence / samples | 1 fps / 59 |
| Apple Vision wall time reported by helper | 2.906 s |
| End-to-end producer wall time | 7.58 s |
| Peak resident memory | ~128 MB |
| Apple STT normalized events | 19 |
| FFmpeg normalized events | 14 |
| Apple Vision normalized events | 23 |
| Visual events after cross-signal NMS | 20 |
| Fused boundary proposals | 33 |
| Golden coarse groups / proposals delivered | 2 / 20 + 13 |

The first uncalibrated smoke produced 60 proposals because unstable OCR strings and
per-frame person counts were promoted. The retained raw evidence exposed this
immediately. The production pass adds two-sample OCR persistence, roughly 0.9-second
person/face presence hysteresis, cross-signal visual NMS, cross-locale agreement for
raw transcriber segment ends, and a minimum fused-confidence gate. That reduced the
same input to 33 proposals without discarding any raw signal file.

## Preserved local smoke artifacts

The generated run is intentionally outside Git:

```text
/Volumes/ExternalSSD/travel-video-editor/analysis/boundaries/
  cam2-0825/DJI_20260826073820_0008_D--002d9ac8cd/producer-v1-smoke-final/
```

It contains `run-intent.json`, `run.json`, four raw/normalized signal files,
`proposals.json`, and the resulting `golden-review-packet.json`. The proposal
validator passed before packet creation. No original or proxy media was modified.

## Interpretation

This is a runtime and plumbing proof, not a precision/recall benchmark. The clip is a
single continuous travel shot, so zero FFmpeg hard cuts is plausible; most visual
proposals came from FeaturePrint or motion changes, and speech activity start/end
provided exact audio anchors. Food ordering, fast reactions, and static nature clips
still need human-reviewed boundary recall measurements before global thresholds are
treated as calibrated.
