# Captions from reviewed travel-video transcripts

Use this workflow when a `video-edit-plan/v1` clip includes
`metadata.timeline_final` pointing to a reviewed Travel Video Pipeline timeline.

## Build captions

```bash
python3 ~/.codex/skills/video-editor/scripts/attach_timeline_captions.py \
  rough-cut.edit-plan.json \
  --output rough-cut.subtitled.edit-plan.json \
  --min-confidence 0.82 \
  --max-chars 34
```

When an older plan still points to legacy `timeline.final.json` files, resolve
the corresponding fresh scene-dialogue timelines without editing every clip:

```bash
python3 ~/.codex/skills/video-editor/scripts/attach_timeline_captions.py \
  rough-cut.edit-plan.json \
  --output rough-cut.subtitled.edit-plan.json \
  --timeline-root work/unified-dialogue-0827/assets \
  --replace-existing \
  --overwrite
```

This requires exactly one
`<source-stem>--*/scene-dialogue/timeline.dialogue-reviewed.summarized.json` per
clip, writes the resolved path into the output plan, and fails closed on missing
or ambiguous matches. `--replace-existing` is appropriate when the prior plan's
captions came from the legacy STT path; omit it to preserve hand-authored lines.

The helper uses this source precedence from the exact referenced timeline:

1. `reviewed_dialogue.captions`
2. `dialogue_script.lines`
3. `reconciled_transcript.utterances`

When `reviewed_dialogue.captions` is present, it is authoritative even when the
list is empty. This prevents captions rejected during unified review from being
silently resurrected from an older stage. It maps each retained source item
through the clip's `source_in`/`source_out`, `speed`, and `transition_after`
overlap onto the assembled output timeline. It preserves existing hand-authored
captions and skips generated captions that overlap them.

For `dialogue-preservation/v1` timelines, the helper fails closed unless
`reviewed_dialogue.policy_audit.status=pass`, `caption_coverage_ratio=1.0`, and
`uncaptioned_lexical_utterance_ids` is empty. Legacy reviewed timelines without a
policy version remain readable for compatibility, but new production edits should
use the audited policy. It also requires `metadata.source_relative_path` and
`metadata.timeline_quick_fingerprint`, verifies the relative suffix and filename
against the timeline's normalized `source.path`/`source.name`, and matches the
expected fingerprint to the reviewed source fingerprint. Media duration must also
cover the clip. A manually pointed `metadata.timeline_final` from another asset is
an error, even if its timestamps happen to fit.

Caption attachment validates individual overlaps; it does not prove that a clip
contains a complete conversational thought. Before fixing dialogue-led source
ranges, inspect the reviewed captions just outside both boundaries. Expand to the
setup and response, or remove low-information gaps only at natural pauses. Do not
leave a scene ending on a connective, ellipsis, or unanswered question merely to
meet a short target runtime.

## Unified reviewed-dialogue contract

Each `reviewed_dialogue.captions` item uses source-timeline seconds and has this
minimum shape:

```json
{
  "start": 21.8,
  "end": 25.8,
  "display_text": "화이트 패스 열차가 들어옵니다.",
  "language": "ko",
  "confidence": 0.91,
  "review_status": "verified",
  "edit_type": "normalized",
  "source_utterance_ids": ["U0042"]
}
```

- Only `review_status=reviewed` or `review_status=verified` is usable. Candidate,
  machine-grouped, uncertain, and rejected items are excluded. An optional
  `usable=false` also excludes an item.
- `display_text` must remain in the spoken language identified by `language`.
  Translation fields are evidence or UI data and are never used as captions.
- `edit_type` is copied unchanged to the edit-plan caption `kind`; common values
  are `verbatim`, `normalized`, and `paraphrase`. If omitted, it defaults to
  `verified`.
- `caption_id`, `source_ids`, and `source_*_id`/`source_*_ids` fields are retained
  in the generated edit plan so the wording remains traceable to review input.

Treat the generated plan as a caption candidate, not a final deliverable.
Read every candidate in sequence before rendering, remove garbled or
context-conflicting text, and mark any cleaned wording as `kind=paraphrase`.
A high STT confidence score is not proof that a sentence is semantically
correct.

## Default evidence policy

- Prefer authoritative `reviewed_dialogue.captions`, falling back to
  `dialogue_script.lines` and `reconciled_transcript.utterances` only for older
  timelines. Do not infer a different analysis run by filename.
- Keep non-empty `ko`, `en`, and `mixed` original-language text. Do not show
  translations as if they were spoken dialogue.
- For unified reviewed dialogue, use `display_text` only after the review-status
  gate. A reviewed caption has already passed the actual-speech and
  caption-readiness decision, so do not reject it again using the machine STT
  confidence score. For legacy sources, use `original_text` and keep treating it
  as an STT candidate subject to `--min-confidence`.
- Exclude `uncertain`, `non_speech`, unreviewed unified candidates, and items
  below the requested confidence threshold in legacy sources.
- Require a unified reviewed caption to remain 100% inside the selected source
  range, allowing only 50 ms of timestamp tolerance. Any genuine partial overlap
  fails closed so the edit range must be revised. The configurable
  `--min-coverage` threshold applies only to legacy caption sources.
- Split long retained text into consecutive captions without rewriting it.
  A human-authored shortening must use `kind=paraphrase`, not `kind=stt`.
- Keep captions as plan entries. The renderer burns them into the picture and
  also writes the SRT sidecar.

## Final QA

After rendering, check all of the following:

1. The renderer reports a plausible non-zero `caption_count` for dialogue-led
   edits.
2. The `.render.json` caption list and `.srt` sidecar agree.
3. A frame sampled inside at least one caption interval visibly contains the
   caption within safe margins.
4. Low-confidence wording, names, prices, and mixed-language turns remain
   absent or clearly marked for manual review.
5. Every retained caption has passed a sequential transcript/context review;
   confidence filtering alone is not sufficient.

Scenic edits may intentionally have zero dialogue captions. State that choice
in the edit plan rather than assuming the SRT sidecar will supply them later.
