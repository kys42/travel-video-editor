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

## Build the compact packet

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
- Prefer `uncertain` over silently repairing low-confidence recognition.
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

Keep the pre-escalation review and an explicit correction log. A polished transcript is still an evidence-based editorial transcript, not a measured ground truth unless a person verifies it against the audio.

## Pilot finding

Two Luna-class agents could synthesize usable turns from the same compact packet without watching the full video. The strongest run split `Can I get one Coke?` from the following Korean menu discussion, retained low-confidence fragments as uncertain, and used later Korean evidence to interpret the flavor discussion. The run without span timestamps reversed one mixed window's order. A timed rerun stayed within every window, used valid source IDs, and preserved uncertain wording. The recommended default is therefore deterministic span-rich packets, one low-cost pass per non-overlapping shard, and strong-model review only for disagreement/low-confidence exceptions.
