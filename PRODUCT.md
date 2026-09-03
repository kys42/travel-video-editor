# Travel Video Editor — Product Truth

## Product

- **Category:** local-first travel-footage indexing, review, and edit-planning tool.
- **Primary platform:** desktop web UI generated from local analysis artifacts on macOS.
- **Primary user:** the footage owner/editor reviewing many days of action-camera footage.
- **Core job:** understand what happened, what was said, and which source ranges are worth editing without watching every frame or sending full video through an AI model.
- **Output contexts:** personal archive, YouTube edit planning, later export into a 4K source-media editing workflow.

## Operating context

- The immutable 4K/camera originals live under `/Volumes/T7/시애틀알래스카`; the current post-deduplication working set is 400 video candidates (about 370.64 GiB).
- Regenerable 1080p H.264 working proxies live under `/Volumes/ExternalSSD/travel-video-editor/proxies/1080p-h264` and mirror source-relative paths.
- Machine processing creates metadata, VAD/STT candidates, scene boundaries, sampled frames, storyboards, and low-cost packets before model review.
- The review UI can work without proxy video. When a proxy is connected, its player seeks to the selected source range while every edit remains anchored to original 4K timecodes.
- Every derived stage should remain inspectable so improved models can re-interpret prior outputs without repeating expensive extraction.

## Confirmed capabilities

- Chronological multi-video library.
- Per-video narrative summary, scene groups, action descriptions, dialogue summaries, notable moments, edit hints, and confidence.
- Sampled source frames and denser storyboards for context review.
- Korean and English STT candidates retained separately, then interpreted together.
- Static, reusable HTML reports with local image assets.

## Product principles

1. **Source time is the spine.** Every explanation, quote, image, and edit hint must resolve to a source range.
2. **Scan first, inspect second.** A user should find an interesting scene quickly, then expand into evidence and dialogue without losing position.
3. **Processing is visible.** Distinguish machine observation, model interpretation, confidence, missing proxy, and unverified transcript.
4. **Dense is useful when structured.** Desktop space should carry footage, time, action, dialogue, and edit judgment—not decorative whitespace.
5. **Originals stay untouched.** Analysis and proxies are derived artifacts; final edit decisions reference original media.
6. **AI is an assistant, not the surface style.** The interface should feel like a dependable media tool, not an AI product landing page.

## Language and tone

- Primary UI language is Korean; filenames, codecs, timecodes, and standard edit terms can remain technical.
- Labels should be short and operational: `구간 재생`, `대화`, `편집 포인트`, `확신도`, `프록시 없음`.
- Avoid promotional claims, cinematic marketing language, and unexplained English decoration.

## Open product decisions

- Final editing-system integration and export format (EDL, FCPXML, Premiere markers, or another format).
- Proxy generation policy and whether the viewer is persistent right-side, top-right PiP, or dockable.
- Speaker identification beyond language-level transcript separation.
- Persistent annotations, ratings, selections, and edit bins.

## Evidence

- `docs/token-efficient-scene-timeline.md`
- `docs/phase1-pipeline.md`
- `docs/video-library.md`
- `docs/media-locations.md`
- `src/travel_video/templates/library.html`
- `src/travel_video/templates/timeline.html`
- `work/food-sequence/library/manifest.json` (generated, machine-local)
