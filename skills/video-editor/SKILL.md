---
name: video-editor
description: Plan, trim, assemble, caption, and validate local video edits with FFmpeg. Use for highlight reels, rough cuts, time-range extraction, burned subtitles, title overlays, proxy previews, or source-relinked renders; do not use merely to analyze footage without producing or revising an edit.
---

# Video Editor

Turn explicit source ranges into a reproducible local video edit. Preserve source media, keep edit decisions inspectable, and make a preview that can be relinked to originals without changing timecodes.

## Workflow

1. Inspect the source and any reviewed timeline, transcript, markers, or user selections. Prefer already-reviewed metadata over re-watching full media.
2. Write a `video-edit-plan/v1` JSON file. Read [references/edit-plan.md](references/edit-plan.md) when authoring or changing a plan.
3. Keep every clip tied to a source path, source in/out time, purpose, and selection reason. Treat STT text as uncertain unless it was verified; label paraphrased captions in the plan.
4. When the requested edit should show dialogue and each clip carries a reviewed `metadata.timeline_final`, read [references/transcript-captions.md](references/transcript-captions.md). Prefer audited `dialogue-preservation/v1` `reviewed_dialogue.captions`, falling back to older reviewed dialogue, `dialogue_script.lines`, and then reconciled utterances only for compatibility. Build output-timeline captions before rendering and review their sequence; an exported SRT alone does not make captions visible in the video.
   The helper must verify `metadata.source_relative_path` and
   `metadata.timeline_quick_fingerprint` against the normalized timeline source,
   and every selected reviewed caption must fit wholly inside the clip. A partial
   overlap is an edit-boundary error, not permission to truncate the caption text.
5. Validate before rendering:

   ```bash
   python3 ~/.codex/skills/video-editor/scripts/render_edit.py plan.json --output preview.mp4 --dry-run
   ```

6. Render from proxies for routine iteration when matching proxies exist. Render from originals only when the requested deliverable needs it or no proxy exists:

   ```bash
   python3 ~/.codex/skills/video-editor/scripts/render_edit.py plan.json --output preview.mp4 --media-mode auto
   python3 ~/.codex/skills/video-editor/scripts/render_edit.py plan.json --output final.mp4 --media-mode source
   ```

7. Check the output probe, duration, dimensions, audio, burned-caption visibility, and at least a few frames around cut boundaries. Use the generated `.render.json` manifest and `.srt` sidecar as the audit trail.

Use the Python environment that provides Pillow. The renderer also requires `ffmpeg` and `ffprobe` on `PATH`.

## Editing decisions

- Default to hard cuts. Add short transitions only when they express time or place changes rather than hiding weak selections. Remember that a transition overlaps adjacent clips and changes output-timeline caption timing.
- Use `speed` for compressing low-information movement or emphasizing a reaction; keep dialogue near 1× unless the user wants an intentional effect. The renderer supports pitch-preserving 0.25–8× audio.
- Use `reframe.mode=cover` with an explicit anchor for vertical/social crops. Keep `contain` when preserving the whole action or signage matters.
- Preserve original audio around dialogue and reactions. Use loudness normalization for assembled pieces, but do not invent replacement dialogue.
- Choose dialogue-led ranges by complete conversational beats, not a fixed clip
  length. Include the setup, a complete thought, and the immediate response or
  payoff; never end merely because the duration target was reached when the last
  line is a dangling clause, connective, ellipsis, or unanswered question.
- To shorten a long conversation, remove low-information gaps or repetitions at
  natural pauses and represent the retained passages as separate source ranges.
  Inspect the reviewed captions immediately before and after every proposed
  dialogue cut. Treat the total runtime target as secondary to semantic closure.
- Use short on-screen context labels for uncertain or summarized speech. Do not present an STT paraphrase as a verbatim quote.
- Treat burned captions and SRT as two outputs of the same plan. If dialogue is expected, verify that `caption_count` is plausible and inspect at least one frame during a caption interval; a non-empty `.srt` file by itself is not sufficient visual QA.
- Keep titles and subtitles inside safe margins. Prefer readable contrast over decorative styling.
- Never overwrite a source. The renderer refuses source/output collisions and refuses an existing output unless `--overwrite` is explicit.
- Do not download music, upload, publish, delete source media, or replace an existing master unless the user has separately authorized that action.

## Supported operations

The bundled renderer supports ordered trims from multiple files, proxy/source selection, pitch-preserving 0.25–8× speed, per-clip gain/mute/audio fades, contain/cover reframing with crop anchors, 90-degree rotation, hard cuts and selected A/V transitions, start/end fades, stereo audio normalization, title/label overlays, burned captions, SRT export, atomic output replacement, and a render manifest.

For stabilization, color treatment, ducked background music, voice-over, still images, speed ramps inside a clip, or NLE exports, extend the edit plan deliberately and preserve the same source-range provenance. Use rights-cleared audio supplied by the user; never infer permission to fetch or publish media.
