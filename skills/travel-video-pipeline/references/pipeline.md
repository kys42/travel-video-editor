# Pipeline orchestration

This reference is the operational backbone. Run commands from the travel-video-editor repository root unless a command says otherwise.

## 1. Establish roots and scope

Resolve these values before reading media:

```bash
export TRAVEL_VIDEO_PROJECT_ROOT=/absolute/path/to/travel-video-editor
export SOURCE_MEDIA_ROOT=/absolute/path/to/immutable/camera-media
export WORKING_MEDIA_ROOT=/absolute/path/to/regenerable/work-media
```

Read `AGENTS.md` and the media-location document it names. Run the skill preflight:

```bash
python3 "$TRAVEL_VIDEO_PROJECT_ROOT/skills/travel-video-pipeline/scripts/check_environment.py" \
  --project-root "$TRAVEL_VIDEO_PROJECT_ROOT" \
  --source-root "$SOURCE_MEDIA_ROOT" \
  --working-root "$WORKING_MEDIA_ROOT" \
  --require-idle-proxy
```

Do not proceed if the source is missing, source and working roots overlap, the project CLI cannot be found, or another proxy batch targets the same working tree. This repository does not yet have a dedicated inventory command. If the user supplied explicit files, record that fact. If discovery is needed, use read-only `find`/`rg --files` or the proxy command's discovery, exclude `._*`, and write an inventory outside the source tree.

For a pilot, select one to three explicit videos. Record each original relative path, size, mtime, and quick fingerprint before processing. Reject stubs and files that `ffprobe` cannot decode.

## 2. Optional proxies

Use completed proxies for analysis when available and preserve source time coordinates. Do not use `--limit` as pilot selection: it takes the first sorted archive entries, not the user's chosen clips. The current `proxy-batch` command is appropriate only for an explicitly authorized root batch. Start it through the skill's advisory-lock wrapper so cooperating sessions cannot write the same output root concurrently:

```bash
python3 skills/travel-video-pipeline/scripts/run_proxy_batch_locked.py \
  --project-root "$TRAVEL_VIDEO_PROJECT_ROOT" \
  --source-root "$SOURCE_MEDIA_ROOT" \
  --output-root "$WORKING_MEDIA_ROOT/proxies/1080p-h264"
```

For a 1–3 clip pilot, select explicit originals first and look up their completed proxy paths. If a selected proxy is missing, either wait for the authorized root batch or analyze that original read-only. Do not pretend `--limit` selects the requested files. The CLI does not yet expose an explicit include manifest.

The proxy manifest is an append-only event log. Reduce it to the latest event per relative path and report `built`, `reused`, and `failed` separately. Do not delete stale partials automatically.

The current CLI does not inject proxy-to-original linkage into the Phase 1 timeline. Before analyzing a proxy, create a sidecar that verifies duration and records both fingerprints:

```bash
python3 skills/travel-video-pipeline/scripts/create_lineage_manifest.py \
  --source-root "$SOURCE_MEDIA_ROOT" \
  --original /absolute/path/to/original.mp4 \
  --processing-input /absolute/path/to/proxy.mp4 \
  --working-root "$WORKING_MEDIA_ROOT" \
  --project-root "$TRAVEL_VIDEO_PROJECT_ROOT" \
  --output "$TRAVEL_VIDEO_PROJECT_ROOT/work/lineage/clip-id.json"
```

Do not use a proxy if this check fails. Treat its source coordinates as an identity time mapping only after duration validation. Keep the lineage path in every run report and use it to relink final editing to the original.

Before combining a scene timeline with Apple/MLX transcript evidence, verify they all belong to the lineage:

```bash
python3 skills/travel-video-pipeline/scripts/validate_artifact_alignment.py \
  source-lineage.json timeline.context-reviewed.json \
  work/apple-speech/clip-id/transcript.apple.json
```

This closes the current CLI's cross-wire gap at orchestration time. Do not build a reconciliation packet when alignment fails.

## 3. Machine scene timeline

Use explicit source or validated proxy files:

```bash
uv sync
uv run travel-video phase1 /absolute/path/to/input.mp4 \
  --output-root work/phase1 \
  --sample-interval 5 \
  --max-segment 30 \
  --min-segment 5 \
  --stt off
```

Phase 1 performs machine segmentation and frame extraction. Its quick fingerprint hashes size plus the first and last 1 MiB, so a cache hit is useful but not cryptographic proof. Before accepting reuse, verify the timeline schema, source path/fingerprint, referenced frames, and expected config. Use a new output directory when changing inputs or parameters.

Keep Phase 1 visual-only (`--stt off`) in this workflow. Running Phase 1 again with a different inline MLX STT configuration can reuse the visual run identity and replace timeline/review packet files. Run Apple/adaptive STT as separate stages instead.

Expected per-asset outputs include `run.json`, `timeline.machine.json`, frame images, contact sheets, and a review packet. Keep them all.

## 4. Review gates

The model-authored files are decisions, not replacements for machine evidence:

```bash
uv run travel-video validate-review timeline.machine.json timeline.review.json
uv run travel-video merge-review timeline.machine.json timeline.review.json \
  --output timeline.reviewed.json

uv run travel-video build-context-packet timeline.reviewed.json \
  --output-dir context --max-frames 8
```

The first review is deliberately cheap: it creates coarse model-call units rather
than final edit clips. The context command creates dense storyboard evidence; in the
Golden path, do not author or merge a separate detailed visual review yet. Pass the
fresh packet together with raw Apple STT to the integrated scene-dialogue command in
the next section. That one group-level pass produces detailed scene understanding,
actual utterances, captions, and finer editorial beats.

Use `validate-context-review` and `merge-context-review` only for a legacy
visual-only run. Read `visual-review.md` before authoring either kind of review.
Never reuse a context output directory for a different input because generated
evidence can be overwritten.

## 5. Speech and transcript

Read `transcript-reconciliation.md`. Apple dual-locale STT is the preferred local path on supported macOS. Adaptive MLX is optional evidence for unresolved windows. Keep every raw locale output, normalized file, packet, shard, merged review, reconciled JSON, and reconciliation HTML.

For the Golden edit-evidence path, build and review the integrated packet directly
from the quick grouped timeline plus the fresh dense storyboard packet:

```bash
uv run travel-video build-scene-dialogue-review-packet \
  work/apple-speech/clip-id/transcript.apple.json \
  timeline.reviewed.json \
  --visual-packet context/context-review-packet.json \
  --max-window 8 \
  --output scene-dialogue/review-packet.json

uv run travel-video validate-scene-dialogue-review \
  scene-dialogue/review-packet.json scene-dialogue/review.json
uv run travel-video merge-scene-dialogue-review \
  timeline.reviewed.json scene-dialogue/review-packet.json \
  scene-dialogue/review.json --output timeline.dialogue-reviewed.json
```

The merge promotes the quick timeline to a context-reviewed artifact and preserves
the coarse groups while attaching detailed visual review, actual utterances,
caption-ready lines, and editorial beats. A beat may refine a boundary but may not
cut a spoken utterance. Keep the separate transcript-only reconciliation route only
for compatibility jobs that explicitly need a standalone transcript.

This is the production `dialogue-preservation/v1` path. Before continuing, verify
that the merged artifact reports `reviewed_dialogue.policy_audit.status=pass`,
`caption_coverage_ratio=1.0`, and no uncaptioned lexical utterance IDs. An uncertain
window with captioned speech is a successful recovery, not a validation failure.

Keep `transcript.reconciled.json` as the canonical standalone transcript, then attach
it losslessly to the final timeline before rendering. The join verifies the Unicode-
normalized source path, quick fingerprint, utterance duration bounds, chronological
order, and full segment/group coverage. It preserves the timeline schema and writes a
new file; it never modifies either input.

## 6. Whole-video summary and views

Build the summary only after integrated scene-dialogue review:

```bash
uv run travel-video build-video-summary-packet timeline.dialogue-reviewed.json \
  --output summary/video-summary-packet.json
uv run travel-video validate-video-summary \
  summary/video-summary-packet.json summary/video-summary.json
uv run travel-video merge-video-summary timeline.dialogue-reviewed.json \
  summary/video-summary-packet.json summary/video-summary.json \
  --output timeline.dialogue-reviewed.summarized.json

uv run travel-video render-web timeline.dialogue-reviewed.summarized.json \
  --output-dir web --assets embed
uv run travel-video render-library \
  'work/phase1-pilot/*/*/timeline.dialogue-reviewed.summarized.json' \
  --output-dir library --title "Trip library" --assets embed \
  --proxy-root "$WORKING_MEDIA_ROOT/proxies/1080p-h264"
```

The integrated timeline keeps authoritative `reviewed_dialogue.utterances`,
`reviewed_dialogue.captions`, and `reviewed_dialogue.editorial_beats` at the top
level, with group and segment attachments. Compatibility jobs may still attach a
standalone legacy reconciliation to a summarized timeline, but new edits must prefer
the unified reviewed dialogue whenever it is present. `--assets embed` embeds preview images in the main page.
`--assets relative` is smaller but requires the frame tree to remain available.
`render-library --proxy-root` discovers uniquely named MP4 proxies and symlinks them
under the library output so the single top preview player can seek to a selected
scene. Per-video `render-web` still has no player. A library with proxy symlinks or
detail links outside its output directory is a local linked view, not a portable
package. HTML is a view; JSON remains the source of truth.

## Run manifest and completion report

For every run, preserve:

- original and processing-input paths, and whether processing used original or proxy;
- source relative path plus source time coordinates;
- tool versions, model/package identifier, locale list, and every non-default flag;
- stage status: `complete`, `reused`, `recomputed`, `failed`, or `skipped`;
- packet/review/merge paths and validation results;
- unresolved windows, detector semantics, and transcript-attachment validation.

Do not silently retry a whole 10-day archive. Prove the pipeline on a small pilot, inspect the artifacts, then expand in resumable batches.
