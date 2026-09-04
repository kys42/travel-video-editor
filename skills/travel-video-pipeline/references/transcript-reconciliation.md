# Mixed Korean-English transcript reconciliation

## Canonical policy

Use Apple `SpeechDetector` plus `SpeechTranscriber` for both `ko-KR` and `en-US` on supported macOS. The same sound can produce plausible text in both recognizers; neither locale candidate is automatically a translation or ground truth. Reconciliation selects the likely spoken-language turns while preserving both raw candidates.

The Apple worker's displayed activity intervals are a padded/merged union of transcriber timestamps. They are detector-gated activity, not raw neural VAD boundaries. State that distinction in reports.

Use adaptive MLX only as optional evidence when Apple candidates remain ambiguous. MLX language detection is weak for short or code-switched windows and must not force one language across a mixed turn.

## Preflight and recognition

Build and inspect Apple capabilities before a batch:

```bash
swift build --package-path apple-speech -c release
apple-speech/.build/release/apple-speech capabilities \
  --output work/apple-speech/capabilities.json

uv run travel-video stt-apple /absolute/path/to/video.mp4 \
  --output-dir work/apple-speech/clip-id \
  --locales ko-KR,en-US \
  --detector-sensitivity medium
```

Apple STT requires macOS 26+ and availability of requested locales/assets. Record the capabilities output. Keep `transcript.apple.json`, extracted WAV, and raw locale reports.

Optional adaptive MLX evidence:

```bash
uv run travel-video stt-adaptive /absolute/path/to/video.mp4 \
  --output-dir work/stt-adaptive/clip-id \
  --model mlx-community/whisper-small-mlx \
  --expected-languages ko,en --max-chunk 10
```

Record the model and package revisions. Dynamic `uv --with mlx-whisper` resolution is not reproducible unless versions are recorded or pinned.

## Unified scene dialogue and caption review (default for editing)

The canonical policy is `dialogue-preservation/v1`. Treat this as the production
path for every new editing-oriented asset, not an experimental prompt variant.

When the result will drive the review HTML or burned video subtitles, make the
actual-utterance decision and the readable caption script in one scene-level model
pass. The two outputs remain separate and share evidence IDs; this avoids a second
model pass that can silently change the selected language or invent wording.

Build the packet directly from preserved Apple output and the quick grouped timeline.
For the Golden integrated flow, also provide the fresh dense visual packet created
from that same timeline:

```bash
uv run travel-video build-scene-dialogue-review-packet \
  work/apple-speech/clip-id/transcript.apple.json \
  /absolute/path/to/timeline.reviewed.json \
  --visual-packet /absolute/path/to/context/context-review-packet.json \
  --max-window 8 \
  --output work/dialogue/clip-id/scene-dialogue/review-packet.json
```

This command accepts only `apple-stt/v1`; do not substitute an old reconciliation,
review, or final timeline. It creates fresh windows internally, checks complete
lexical candidate/span coverage, records ignored empty Apple final events, preserves
source span coordinates, and clips only the per-window usable range. It deliberately
omits prior `dialogue_summary` fields so an independent rerun cannot copy an older
language decision. The packet contains its exact `scene-dialogue-review/v1`
`review_contract` and compact example.

With `--visual-packet`, the packet discards prior detailed visual-review conclusions
and supplies the quick group, dense storyboard candidates, raw bilingual evidence,
and crossing-window boundary context together. Give one complete packet to a review
agent. In the same review JSON, require:

- `scene_understanding`: detailed chronological action, context, representative
  samples, notable moments, and coarse-boundary notes;

- `utterances`: what was actually spoken, in the original language, with source
  evidence/window/candidate IDs and grounded timing;
- `captions`: readable display lines with `display_text`, `edit_type`, and source
  utterance IDs;
- `window_decisions`: exactly one `resolved`, `uncertain`, or `non_speech` decision
  for every packet window.
- `editorial_beats`: finer visual/action/dialogue units inside the coarse group, each
  linked to its segment/window/utterance/caption/sample IDs, dialogue closure, and
  any boundary adjustment.

Every window, utterance, and caption must belong to exactly one editorial beat. Beat
ranges must be ordered, non-overlapping, and anchored to packet evidence. A beat may
cross a coarse group boundary only when its cited evidence crosses it and the review
records `extend_before`, `extend_after`, `merge_previous`, or `merge_next`. Coarse
groups remain model-call and parallelization units; editorial beats become the
downstream highlight candidates.

When a long asset does not fit comfortably in one agent context, split only at
complete context-group boundaries, review slices in parallel, and merge them back
through the full packet validator:

```bash
uv run travel-video slice-scene-dialogue-review-packet review-packet.json \
  --groups G001,G002 --output packet.part-01.json
uv run travel-video slice-scene-dialogue-review-packet review-packet.json \
  --groups G003,G004 --output packet.part-02.json

uv run travel-video merge-scene-dialogue-review-shards review-packet.json \
  review.part-01.json review.part-02.json --output review.json
```

Never divide a context group or let two agents review the same group. The shard
merge rejects duplicates, gaps, invalid evidence, and changed group order.

Translations never replace `original_text` or `display_text`. Review is
preservation-first: low ASR confidence alone must not delete a real turn. Assign
`ko`, `en`, or `mixed` whenever a useful original-language wording can be recovered,
and feed every such utterance into at least one caption. An uncertain window may
still contain several caption-ready turns. For clipped speech, keep the reliable
portion with an ellipsis or use `edit_type=normalized` for a context-supported
cleanup. Reserve `language=uncertain` for residue with no usable lexical wording;
only that residue and clear non-speech stay out of captions. Validate and merge into
a new artifact:

```bash
uv run travel-video validate-scene-dialogue-review \
  review-packet.json review.json
uv run travel-video merge-scene-dialogue-review \
  timeline.reviewed.json review-packet.json review.json \
  --output timeline.dialogue-reviewed.json
```

With `--visual-packet`, merge promotes the quick grouped timeline into a compatible
context-reviewed timeline while attaching the integrated answer. Without that flag,
the compatibility route still expects an already context-reviewed timeline.

After merge, require `reviewed_dialogue.policy_audit.status=pass` and
`caption_coverage_ratio=1.0`. Inspect its window status counts,
`uncertain_windows_with_captioned_speech`, and
`uncertain_residue_utterance_count`; these distinguish preserved low-confidence
speech from genuinely unusable residue. Do not continue to summary or highlight
selection when `uncaptioned_lexical_utterance_ids` is non-empty.

For a zero-window packet, do not spend a model call:

```bash
uv run travel-video build-no-candidate-scene-dialogue-review \
  review-packet.json --output review.json
```

For a prepared day inventory, generate fresh, resumable packets in parallel:

```bash
uv run python scripts/build_scene_dialogue_batch.py inventory.json \
  --output-root work/dialogue/YYYY-MM-DD/assets --jobs 4
```

The Video Editor caption helper treats top-level `reviewed_dialogue.captions` as
authoritative, including an explicitly empty list. This prevents rejected legacy STT
from reappearing in the rendered video.

## Build the compact packet

Use the following older transcript-only flow when a separate caption script is not
needed or when continuing an existing reconciliation run.

```bash
uv run travel-video build-transcript-reconciliation-packet \
  work/apple-speech/clip-id/transcript.apple.json \
  --timeline /absolute/path/to/timeline.context-reviewed.json \
  --mlx-normalized work/stt-adaptive/clip-id/normalized/chunks.json \
  --max-window 8 \
  --output work/apple-speech/clip-id/reconciliation/packet.json
```

Omit `--mlx-normalized` when no MLX run exists. A useful packet window contains:

- `window_id`, `start`, and `end`;
- both Apple locale candidates;
- each candidate's ordered `evidence_spans` with timestamps and source IDs;
- optional scene context and MLX hints.

Do not delegate from an older packet that lacks `evidence_spans`. In the pilot, agents given only window-level aggregate text reversed the English/Korean order inside a mixed window. Span timestamps fixed this failure mode.

## Parallel subagent protocol

Shard only after the deterministic packet exists. Use non-overlapping contiguous window ranges such as `RW0001-RW0009`, `RW0010-RW0018`, and `RW0019-RW0027`. Use Luna-class agents for the first pass when available.

Each agent receives only:

1. the exact packet path and assigned IDs;
2. this policy and output schema;
3. a unique output path;
4. an instruction not to read prior reviews or invent wording from scene context.

Required shard format:

```json
{
  "schema_version": "transcript-reconciliation-shard/v1",
  "reviewer": "luna",
  "packet": "/absolute/path/to/packet.json",
  "windows": [
    {
      "window_id": "RW0001",
      "utterances": [
        {
          "start": 1.91,
          "end": 2.63,
          "language": "ko",
          "original_text": "…",
          "translations": {"en": "…"},
          "confidence": 0.71,
          "source_candidate_ids": ["APPLE-ko-KR-T0001"],
          "notes": "optional evidence note"
        }
      ]
    }
  ]
}
```

Rules for the agent:

- Traverse `evidence_spans` by time; split code-switched or overlapping turns.
- Keep `original_text` in the spoken language and script. Put translations only under `translations`.
- Use only source candidate IDs present in that packet window.
- Keep timestamps inside the window and grounded in evidence spans. If a span crosses a boundary, clip and note it.
- Scene context may disambiguate a known item but must not create unheard words.
- Preserve a useful low-confidence turn rather than discarding it. Use `uncertain`
  only for words that cannot be responsibly recovered; record context-supported
  normalization in notes instead of silently changing the evidence.
- Use `non_speech` only when evidence supports no lexical utterance.

Suggested task text:

```text
Read $travel-video-pipeline and reconcile only <START>-<END> from <PACKET>.
Use evidence_spans for timing and order. Preserve original-language text; keep
translations separate. Scene context is non-authoritative. Do not read any prior
review. Write transcript-reconciliation-shard/v1 to <UNIQUE_OUTPUT> and validate
IDs, coverage, and timestamp bounds before returning.
```

Merge complete, non-overlapping shards:

```bash
python3 skills/travel-video-pipeline/scripts/merge_reconciliation_shards.py \
  packet.json shard-01.json shard-02.json shard-03.json \
  --output review.parallel.json
uv run travel-video validate-transcript-reconciliation \
  packet.json review.parallel.json
```

The merge helper requires exact packet coverage and order. It rejects duplicates, missing windows, invalid IDs, and out-of-window utterances.

## Exception review and final outputs

Compare independent runs when testing a prompt or model:

```bash
python3 skills/travel-video-pipeline/scripts/compare_reconciliation_runs.py \
  run-a.json run-b.json --output comparison.json
```

Escalate a window to Terra-class review or direct Codex inspection when any of these applies:

- confidence below 0.65 on a semantically important turn;
- independent runs disagree on language, order, or number of utterances;
- Korean and English overlap or code-switch within a short window;
- a correction depends mainly on scene context rather than acoustic evidence;
- the proposed timing cannot be derived from `evidence_spans`;
- names, prices, menu items, or other edit-critical details conflict.

After resolving exceptions:

```bash
uv run travel-video merge-transcript-reconciliation \
  packet.json review.final.json --output transcript.reconciled.json
uv run travel-video render-transcript-reconciliation \
  packet.json --review review.final.json --output index.html
```

After the video summary is merged, attach the reconciled transcript to the final
timeline used by the web and library renderers:

```bash
uv run travel-video attach-reconciled-transcript \
  timeline.summarized.json transcript.reconciled.json \
  --output timeline.final.json
```

The join preserves the complete reconciled transcript at the top level and copies
each utterance onto every overlapping segment and scene group with source-relative
timestamps, original text, translations, confidence, and candidate IDs. It rejects
source path/fingerprint mismatches, out-of-duration timestamps, duplicate IDs,
nonchronological input, and timeline coverage gaps. Never bypass this validation by
copying dialogue fields manually.

For a readable review page or dialogue-led edit, derive a separate scripted
timeline after attachment:

```bash
uv run travel-video build-dialogue-script timeline.final.json \
  --output dialogue/timeline.scripted.json
```

This deterministic stage groups adjacent same-language fragments without rewriting
their words. It preserves `reconciled_transcript` unchanged, records all source
utterance/window/candidate IDs, and attaches `dialogue_lines` to overlapping
segments and scene groups. Use `timeline.scripted.json` for the web/library renderer
and as `metadata.timeline_final` in an edit plan when caption generation should
prefer readable dialogue blocks. Treat it as machine-grouped evidence: review and
mark any cleaned or shortened caption as a paraphrase before final rendering.

For a date or story-day batch in the project repository:

```bash
uv run python scripts/build_dialogue_script_batch.py /absolute/phase1/day-root \
  --output-root work/full-batch/dialogue-script/YYYY-MM-DD
```

The batch writes one new scripted timeline per asset plus a manifest with source
utterance and output line counts. It never modifies final timelines in place.
Each asset also has `state.json` containing the input SHA-256, grouping policy,
and output SHA-256. Reuse occurs only when all three still match; a changed input,
changed grouping limit, missing state, or modified output rebuilds that asset.
`asset_id` is restricted to a single safe path component and duplicate IDs abort
the batch before they can share an output directory.

Keep the pre-escalation review and an explicit correction log. A polished transcript is still an evidence-based editorial transcript, not a measured ground truth unless a person verifies it against the audio.

## Pilot finding

Two Luna-class agents could synthesize usable turns from the same compact packet without watching the full video. The strongest run split `Can I get one Coke?` from the following Korean menu discussion, retained low-confidence fragments as uncertain, and used later Korean evidence to interpret the flavor discussion. The run without span timestamps reversed one mixed window's order. A timed rerun stayed within every window, used valid source IDs, and preserved uncertain wording. The recommended default is therefore deterministic span-rich packets, one low-cost pass per non-overlapping shard, and strong-model review only for disagreement/low-confidence exceptions.
