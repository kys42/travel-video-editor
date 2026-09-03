# UI concept exploration — desktop review workspace

Status: concept A selected on 2026-09-02 and promoted to the production library template.

## Intent

Turn the generated report into a compact desktop program for answering three questions:

1. Which video or source range matters?
2. What visibly happens and what is said there?
3. How could that range be used in an edit?

The concepts deliberately remove the current landing-page grammar: no hero, oversized editorial title, decorative texture, wide empty gutters, or repeated summary cards.

## Domain vocabulary used

- footage bin / video list
- source timecode and duration
- scene groups and selected range
- source monitor / future proxy viewer
- storyboard strip
- action analysis
- dialogue and language evidence
- notable moment and edit hint
- confidence and processing state
- edit selection / in-out range

## Shared visual world

- graphite and slate for application chrome
- neutral paper or canvas for long reading
- amber for active playhead and edit attention
- cyan for dialogue/audio evidence
- red only for errors or unreliable/missing states
- one compact Korean-capable sans-serif stack; monospaced numerals only for timecodes and measurements

## Signature element

A synchronized source-time spine connects the selected clip, scene row, inspector, and future player. Time is a navigation control, not decorative metadata.

## Rejected defaults

- landing-page hero and promotional copy
- equal-size feature/metric cards
- oversized serif titles between work areas
- nested rounded cards and decorative gradients
- color used as ornament rather than selection or state

## Concepts

### A — Edit Desk

Three-pane dark workspace: footage bin, chronological scene list, and persistent inspector. Best fit for repeated professional review because context remains visible while moving through scenes.

**Selected implementation:** the right inspector was narrowed to visual/source-monitor duty. Interpretation, dialogue synthesis, raw STT, segment actions, storyboards, and edit notes expand one level beneath the selected scene row so evidence stays attached to its source range.

### B — Review Ledger

Light, table-forward log with denser side-by-side columns for action, dialogue, and edit value. Best fit for comparing many scenes quickly and exporting review decisions.

### C — Cut Console

Dialogue- and event-first dark console. The scene list behaves like a transcript cut sheet, with visual evidence docked beside the selected range. Best fit when conversations drive the edit.

## Prototype scope

- Uses the four existing food-sequence summaries and frame paths.
- Switches concepts without regenerating data.
- Supports clip selection, scene selection, search, inspector tabs, and keyboard shortcuts `1`–`3`.
- The player is an honest placeholder until proxy media exists; source ranges and future playback placement are represented.
- The incumbent templates remain unchanged until one concept is selected.
