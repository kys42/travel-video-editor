# Token-efficient visual review

## Evidence ladder

Do not start by streaming or sampling every frame into Codex. Use this ladder:

1. Machine cuts, motion/change scores, audio activity, and sparse thumbnails.
2. One contact sheet per provisional scene group.
3. Quick scene grouping from those sheets plus compact speech-boundary hints.
4. Denser multi-frame storyboard only for each coarse group.
5. Optional local multimodal boundary proposals, reduced to the current group and its neighbors.
6. One integrated group review of visual context, dialogue, captions, and editorial beats.
7. Exception inspection of individual frames or short proxy ranges.

This keeps the expensive semantic pass compact while still allowing more images where actions or transitions matter.

## First-pass contact-sheet review

Read the generated review packet and every referenced contact sheet. Author
`timeline.review.json` without changing machine IDs or time ranges. Keep this pass
cheap: write only enough observable detail to form safe coarse groups rather than
attempting final scene understanding. Describe observable action rather than generic
labels:

- who/what is visible and what changes;
- camera movement and setting;
- edit value, transition, or notable/funny moment;
- uncertainty when a detail is too small or occluded.

Validate and merge before continuing:

```bash
uv run travel-video validate-review timeline.machine.json timeline.review.json
uv run travel-video merge-review timeline.machine.json timeline.review.json \
  --output timeline.reviewed.json
```

The validator requires exact segment order and contiguous coverage. Never invent frame IDs.

## Dense storyboard evidence and integrated review

Build up to eight frames per reviewed group by default:

```bash
uv run travel-video build-context-packet timeline.reviewed.json \
  --output-dir context --max-frames 8
```

For the Golden path, do not author a separate detailed visual answer here. Pass the
fresh `context-review-packet.json` to `build-scene-dialogue-review-packet
--visual-packet`; when available, also pass a validated `boundary-proposal/v1` file
with `--boundary-proposals`. One group-level model pass then adds:

- a chronological `narrative_summary` describing what actually happens;
- a representative sample ID from the candidate list;
- notable moments with source-relative timestamps and candidate frame IDs;
- confidence and explicit uncertainty;
- actual utterances, readable original-language captions, and all-window decisions;
- finer `editorial_beats` linked to visual and dialogue evidence.

Boundary proposals remain suggestions. Keep the approximately 3fps Vision score
index and other raw signal streams local; send only group-local proposals plus the
nearest previous/next proposal. If a beat accepts a proposed timestamp, record its
ID in `source_boundary_proposal_ids`. Do not run dense full-frame refinement by
default.

This pass follows `dialogue-preservation/v1`: visual context may support a careful
normalization, but low ASR confidence alone may not remove plausible speech. Keep
the uncertainty on the window or caption note while preserving every usable
original-language turn.

Use the separate `phase1-context-review/v1` shape below only when continuing a
legacy visual-only run. Do not treat its older `dialogue_summary` as evidence for a
fresh integrated review.

Use denser sampling when a group contains a quick action, reaction, handoff, ordering exchange, or large camera move. Use fewer frames for static shots. If the sheet is ambiguous, inspect the listed individual frames before asking for more extraction.

For parallel review, shard by complete groups. Give each agent only its groups and candidate frames; never split one group across agents. Merge only after checking exact coverage and IDs.

Use these model-authored shapes; repeat entries in exact packet order:

```json
{
  "schema_version": "phase1-review/v1",
  "asset_id": "<packet asset_id>",
  "reviewer": "codex",
  "method": "contact-sheet review",
  "notes": "evidence inspected and limits",
  "segments": [{"segment_id": "S001", "visual_summary": "…", "actions": [], "subjects": [], "scene_type": "…", "importance": 0.5, "confidence": 0.8}],
  "groups": [{"group_id": "G001", "label": "…", "summary": "…", "segment_ids": ["S001"]}]
}
```

```json
{
  "schema_version": "phase1-context-review/v1",
  "asset_id": "<packet asset_id>",
  "reviewer": "codex",
  "method": "group storyboard review",
  "groups": [{
    "group_id": "G001",
    "narrative_summary": "…",
    "dialogue_summary": "…",
    "dialogue_evidence": ["ko", "en"],
    "representative_sample_id": "<candidate sample_id>",
    "representative_reason": "…",
    "key_moments": [{"sample_id": "<candidate sample_id>", "role": "…"}],
    "notable_moments": [{"sample_id": "<candidate sample_id>", "category": "dialogue", "title": "…", "description": "…", "edit_hint": "…"}],
    "confidence": 0.8
  }]
}
```

Allowed notable categories are `candid`, `dialogue`, `unexpected`, `visual`, and `travel_detail`; use no more than three per group.

```bash
uv run travel-video validate-context-review \
  context/context-review-packet.json context/context-review.json
uv run travel-video merge-context-review \
  timeline.reviewed.json context/context-review-packet.json \
  context/context-review.json --output timeline.context-reviewed.json
```

## Whole-video synthesis

After every group has the integrated visual/dialogue review, create the compact
video-summary packet. The synthesis agent should read group summaries, editorial
beats, captions, and representative frames rather than all source frames.

The final summary should state the clip's setting, progression, outcome, best moments, likely edit use, and uncertainty. Keep events chronological. Highlights must reference eligible groups/frames from the packet.

```json
{
  "schema_version": "phase1-video-summary/v1",
  "asset_id": "<packet asset_id>",
  "title": "…",
  "one_line_summary": "…",
  "narrative_summary": "…",
  "chronological_events": [{"group_id": "G001", "headline": "…", "description": "…"}],
  "highlight_group_ids": ["G001"],
  "representative_sample_id": "<packet candidate>",
  "tags": ["tag"]
}
```

There must be exactly one chronological event for every packet group in packet order, at most three unique highlights, and one to eight non-empty tags.

```bash
uv run travel-video build-video-summary-packet timeline.dialogue-reviewed.json \
  --output summary/video-summary-packet.json
uv run travel-video validate-video-summary \
  summary/video-summary-packet.json summary/video-summary.json
uv run travel-video merge-video-summary timeline.dialogue-reviewed.json \
  summary/video-summary-packet.json summary/video-summary.json \
  --output timeline.dialogue-reviewed.summarized.json
```

## Template rendering

Use embedded assets for a portable desktop-first review page:

```bash
uv run travel-video render-web timeline.dialogue-reviewed.summarized.json \
  --output-dir web --assets embed
uv run travel-video render-library \
  'work/phase1-pilot/*/*/timeline.dialogue-reviewed.summarized.json' \
  --output-dir library --assets embed \
  --proxy-root "$WORKING_MEDIA_ROOT/proxies/1080p-h264"
```

The scene timeline should expose overview, action interpretation, dialogue summary,
notable moments, original source ranges, and reconciled original-language utterances
when attached. Raw STT candidates remain visible as comparison evidence. The library
should list summarized videos in capture-time order and can use a single top preview
player when `--proxy-root` finds an unambiguous filename match. Proxy media is
symlinked and detail pages may live outside the library output, so call this a local
linked library rather than a portable package. The per-video web renderer has no
player.

## Model context budget

- First pass: one contact sheet plus compact metadata per group.
- Second pass: one storyboard of up to eight frames per group.
- Exceptions: individual frames or a short proxy interval only when required.
- Never send raw 4K frames when a 640-1280 px review image is sufficient.
- Never resend unchanged evidence to a second agent; send only disputed groups/windows and the minimal adjacent context.
