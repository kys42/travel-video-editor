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

- In this repository, treat `docs/golden/01-raw-to-editorial-evidence.md` as the
  normative extraction contract and `docs/golden/02-editorial-evidence-to-highlight.md`
  as the normative highlight-selection contract. The references below provide the
  commands and compatibility routes that implement those contracts.
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
- Validate every model-authored JSON before merging it. Reject invented IDs,
  out-of-window timestamps, missing evidence-time coverage, and representative
  frames whose ID, time, timecode, or path differs from the source timeline.
- Prefer proxy/audio/frame caches on the configured working-media volume. Keep code, manifests, small JSON, prompts, and decision history in or alongside the project.

## Subagent policy

Use subagents only for bounded packet review, not media discovery or destructive operations.

- For dialogue that will feed readable HTML or burned captions, prefer the unified scene-dialogue flow in [references/transcript-reconciliation.md](references/transcript-reconciliation.md). With a fresh visual packet, one model pass per complete coarse group emits scene understanding, evidence-grounded utterances, caption-ready lines, and finer editorial beats as separate linked outputs. Never feed it an older reconciliation/review/final timeline.
- For every new editing-oriented run, use `dialogue-preservation/v1` as the
  canonical policy. Require the merged timeline's `reviewed_dialogue.policy_audit`
  to pass before summary, highlight selection, or caption rendering.
- Parallelize across complete scene packets or assets. Give each agent only the fresh packet, its embedded exact contract, and a unique output path. Do not split a context group between agents.
- Keep the older window-shard reconciliation flow only for transcript-only jobs or compatibility with an existing run.
- Do not run acoustic language detection by default. A mixed window may contain several language turns. Let the review agent emit multiple utterances first; use MLX `detect_language` only for unresolved audio windows.
- Always run the matching project validator before merge. A syntactically valid model answer is not an accepted transcript or caption script.
- When a caption cites multiple utterances, require its display interval to
  overlap every cited utterance; the broad first-to-last envelope is not enough.
- Escalate only low-confidence, conflicting, or context-dependent windows to a stronger agent such as Terra when available. Codex reviews the remaining exceptions and the merged summary, not every clear window.
- For visual review, shard by complete scene groups. Never split a group between agents, and require all frame/sample IDs to come from the packet.
- Treat coarse groups as cheap review units, not edit clips. Use sparse sheets plus speech-boundary hints to group quickly; do the detailed visual/action/dialogue interpretation once in the dense group review. Preserve and resolve any speech window that crosses a coarse boundary.
- Optimize dialogue review for recall of plausible speech. Low ASR confidence does
  not justify dropping a turn: recover useful ko/en/mixed wording, use partial or
  context-supported normalized captions where appropriate, and reserve
  `language=uncertain` for residue with no usable lexical content. Drop only clear
  non-speech or meaningless fragments.

## Completion

Report the source, run directory, completed stages, counts, validation results, and clickable paths to the most useful intermediate and final artifacts. Call out uncertain transcript corrections and any unrelated test failures separately. Do not claim ground truth unless audio/video was actually verified against a human reference.
