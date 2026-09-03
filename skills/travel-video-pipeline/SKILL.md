---
name: travel-video-pipeline
description: Run a local-first, token-efficient action-camera and travel-video workflow for source discovery, audio/STT, scene timelines and storyboards, mixed Korean-English transcript reconciliation, summaries, and review HTML. Use when processing or continuing a travel-video project; keep final NLE edits and publishing outside scope unless explicitly requested.
---

# Travel Video Pipeline

Use deterministic local processing to reduce video into traceable evidence, then spend model context only on compact review packets. Preserve raw outputs and every later judgment as separate artifacts.

## Start safely

1. Locate the project root. Prefer `TRAVEL_VIDEO_PROJECT_ROOT`, then the current repository, then `/Users/kys/projects/travel-video-editor` when it exists.
2. Read the repository `AGENTS.md` and its referenced media-location document before touching media.
3. Run `python3 scripts/check_environment.py --project-root <root>` from this skill directory when the environment or mounted volumes are uncertain.
4. Treat source-camera media as immutable. Resolve actual Unicode paths from the filesystem or manifests; do not type a normalized Korean path and assume it matches.
5. When processing a proxy, create a lineage sidecar with `scripts/create_lineage_manifest.py` before Phase 1. Create a new run directory or reuse only when its manifest proves the source fingerprint and config match.

## Route by requested outcome

- For source discovery, phase execution, caching, and exact CLI commands, read [references/pipeline.md](references/pipeline.md).
- For Apple/MLX STT, mixed-language decisions, or subtitle scripts, read [references/transcript-reconciliation.md](references/transcript-reconciliation.md).
- For contact sheets, scene descriptions, storyboard review, whole-video summaries, or HTML libraries, read [references/visual-review.md](references/visual-review.md).
- For an end-to-end request, read all three in that order and stop at the user's requested output; do not infer permission to render final edits or publish.

## Invariants

- Never rename, move, overwrite, or delete source media.
- Keep `source relative path + source_in + source_out` on every scene, transcript, and edit decision. Analyze proxies if available, but retain original coordinates.
- Do not make Codex or a subagent watch the full original sequentially. Use machine boundaries, low-resolution frames, contact sheets, storyboards, aligned STT, and scene metadata.
- Preserve `raw → normalized → aligned → decisions → reconciled → corrections → exports`. A later stage never replaces an earlier one.
- Keep original-language transcript text separate from translations. A wrong-locale STT candidate is evidence, not a translation and not ground truth.
- Validate every model-authored JSON before merging it. Reject invented IDs, out-of-window timestamps, missing coverage, and representative frames not present in the packet.
- Prefer proxy/audio/frame caches on the configured working-media volume. Keep code, manifests, small JSON, prompts, and decision history in or alongside the project.

## Subagent policy

Use subagents only for bounded packet review, not media discovery or destructive operations.

- For transcript reconciliation, shard non-overlapping `window_id` ranges across fast low-cost agents such as Luna when available. Give each agent only its packet slice, exact schema, and a unique output path.
- Do not run acoustic language detection by default. A mixed window may contain several language turns. Let the review agent emit multiple utterances first; use MLX `detect_language` only for unresolved audio windows.
- Merge shards with `python3 scripts/merge_reconciliation_shards.py ...`, then run the project validator.
- Escalate only low-confidence, conflicting, or context-dependent windows to a stronger agent such as Terra when available. Codex reviews the remaining exceptions and the merged summary, not every clear window.
- For visual review, shard by complete scene groups. Never split a group between agents, and require all frame/sample IDs to come from the packet.

## Completion

Report the source, run directory, completed stages, counts, validation results, and clickable paths to the most useful intermediate and final artifacts. Call out uncertain transcript corrections and any unrelated test failures separately. Do not claim ground truth unless audio/video was actually verified against a human reference.
