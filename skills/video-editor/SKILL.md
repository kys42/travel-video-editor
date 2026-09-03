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
4. Validate before rendering:

   ```bash
   python3 ~/.codex/skills/video-editor/scripts/render_edit.py plan.json --output preview.mp4 --dry-run
   ```

5. Render from proxies for routine iteration when matching proxies exist. Render from originals only when the requested deliverable needs it or no proxy exists:

   ```bash
   python3 ~/.codex/skills/video-editor/scripts/render_edit.py plan.json --output preview.mp4 --media-mode auto
   python3 ~/.codex/skills/video-editor/scripts/render_edit.py plan.json --output final.mp4 --media-mode source
   ```

6. Check the output probe, duration, dimensions, audio, captions, and at least a few frames around cut boundaries. Use the generated `.render.json` manifest and `.srt` sidecar as the audit trail.

Use the Python environment that provides Pillow. The renderer also requires `ffmpeg` and `ffprobe` on `PATH`.

## Editing decisions

- Default to hard cuts. Add transitions only when they express time or place changes rather than hiding weak selections.
- Preserve original audio around dialogue and reactions. Use loudness normalization for assembled pieces, but do not invent replacement dialogue.
- Use short on-screen context labels for uncertain or summarized speech. Do not present an STT paraphrase as a verbatim quote.
- Keep titles and subtitles inside safe margins. Prefer readable contrast over decorative styling.
- Never overwrite a source. The renderer refuses source/output collisions and refuses an existing output unless `--overwrite` is explicit.
- Do not download music, upload, publish, delete source media, or replace an existing master unless the user has separately authorized that action.

## Supported operations

The bundled renderer supports ordered trims from multiple files, proxy/source selection, speed and per-clip gain, aspect-preserving scale and pad, hard cuts, start/end fades, stereo audio normalization, title/label overlays, burned captions, SRT export, atomic output replacement, and a render manifest.

For crop/reframe, crossfades, stabilization, color treatment, ducked background music, voice-over, still images, or NLE exports, extend the edit plan deliberately and preserve the same source-range provenance. Use rights-cleared audio supplied by the user; never infer permission to fetch or publish media.
