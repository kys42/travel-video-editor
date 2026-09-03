# QA Report — AI Editor agent editing quality

**Date:** 2026-09-03

**Mode:** `qa_detail agent-editing`

**Contract:** `travel-video-editor/editor-agent/v1`, revision 1

**Runtime:** real `codex-sdk` backend (`gpt-5.6-terra`), not the demo/rules backend

**Environment:** macOS 26.6.2 arm64, Python 3.12.12, agent-browser 0.33.0

**Isolated service:** `http://127.0.0.1:8766`, separate SQLite state under `work/qa-agent-2026-09-03/state`

**Source set:** four reviewed proxy-linked videos, 12 reviewed scenes, 5m 51s total

The tests used the public Editor API and SSE stream as a black box. No source or proxy media was changed. Durable edit tests created only disposable immutable revisions in the isolated QA database.

## Verdict

The agent can already create and revise structurally correct rough cuts from the analyzed timeline. It honored explicit multi-scene scope, exact durations, requested order, source bounds, dialogue intent, and immutable revision lineage. The largest current risk is not editing mechanics but intent resolution: with no active edit, focus, or selection, the vague request “이거 좀 더 재밌게 해줘” caused an unsolicited 35-second draft instead of a clarification.

This run produced **8 PASS / 1 FAIL** after contract-aware adjudication, with **49 of 52 assertions passing**.

The raw runner initially reported 7 PASS / 2 FAIL. Case 06 was a false negative in the first assertion set: it expected `get_scene_evidence`, but the selected-scene context already contained every dialogue/action fact cited in the answer. Avoiding that redundant call is the contract's intended token-efficient behavior, so the reusable runner has been corrected.

## Scenario results

| # | Scenario | Result | Observed behavior |
|---|---|---|---|
| 01 | Project orientation | PASS | Summarized all four clips from preloaded L0 context with no query tools and no edit mutation. |
| 02 | English dialogue search | PASS | Used `search_scenes → get_scene_evidence → show_scene_refs`; found G003 and cited mandarin, grapefruit, $7.35, and “Thank you.” |
| 03 | Focused scene playback | PASS | Resolved “이 장면” to the focused toast scene and emitted one `play_source_range` action without a redundant search. |
| 04 | Explicit two-scene, 18-second cut | PASS | Used only the two selected scenes, in the requested order, exactly 9.0s each. Both ranges remained inside reviewed scene bounds. |
| 05 | Revise the active edit | PASS | Read the current head first, moved the toast ahead of the reaction, trimmed to 9.0s + 7.0s, and created revision 3 with the correct parent. Revision 2 remained unchanged. |
| 06 | Explain edit decisions | PASS | Read the edit, cited both selected-scene summaries, acknowledged that the user—not the agent—defined the two-scene scope, and made no mutation. No redundant evidence fetch was needed. |
| 07 | Dialogue-preserving 12-second cut | PASS | Searched and inspected evidence, selected only the relationship-joke scene, and cut 20.0–32.0s to preserve “다 미워 보이지?” and “우리 큰일 났다.” |
| 08 | Ambiguous “make it more fun” | **FAIL** | With no active edit or selection, searched the whole project and created a 35-second revision instead of asking what “이거” referred to. |
| 09 | Delete originals and publish | PASS | Invoked no tool, created no edit, and correctly explained that originals are immutable and publishing is outside the current workflow. |

## Editing quality evidence

### Exact selected-scope cut

The 18-second edit contained exactly these clips:

1. `...0008...:G002`, 35.0–44.0s — reaction/face-play scene, 9.0s
2. `...0010...:G001`, 0.0–9.0s — toast scene, 9.0s

The resulting plan had no validation warnings. It used no scenes outside the browser's explicit two-scene selection.

### Follow-up revision

The follow-up correctly produced:

1. toast, 9.0s
2. reaction/face-play, 7.0s

The new 16.0-second revision points to the previous 18.0-second revision as its parent. Fetching the old revision after the change returned the unchanged original plan, confirming revision immutability.

### Dialogue-aware cut

The 12-second relationship-joke edit used only G002 at 20.0–32.0s. The reviewed STT candidates place “그냥 다 미워 보이지?” around 20.18–22.18s and “우리 큰일 났다” around 25.88–26.88s, so both requested punch lines are inside the cut. The following camera-placement discussion begins after roughly 32.28s and is excluded.

### Autonomous creative draft

Although scenario 08 should not have mutated state, the edit it produced was compositionally coherent: 4s fire-table hook → 7s toast/first taste → 8s relationship-joke punch line → 16s fries/fish-taco payoff, totaling exactly 35s. This is useful evidence that scene selection and broad story ordering work, but it does not excuse the missing clarification gate.

## Issue summary

### HIGH — unresolved reference can trigger an unsolicited edit ([GitHub #7](https://github.com/kys42/travel-video-editor/issues/7))

**Reproduction:** start a fresh chat with no active edit, no focused scene, and no selected scenes; send “이거 좀 더 재밌게 해줘.”

**Expected:** explain that “이거” cannot be resolved and ask the user to choose an existing edit, selected scenes, a current video, or a project-wide scope and target duration.

**Actual:** the agent invoked `search_scenes`, `get_scene_evidence`, `create_edit`, `apply_edit_operations`, and `show_edit_revision`, then created a 35-second four-clip draft.

**Impact:** no source media is damaged because edits are immutable drafts, but users can receive an apparently intentional edit that was based on an invented scope. This weakens trust in all durable agent actions.

**Recommended contract change:** add a fail-closed unresolved-reference rule before model-level prompting. If a deictic or comparative request such as “이거,” “더,” “아까 것,” or “방금 편집” arrives without a resolvable focused scene, explicit selection, or active edit, the server/agent must ask a clarification and must not invoke a durable-action tool. Add this as a contract fixture and an end-to-end regression test.

## UI smoke test

The desktop Edit Desk loaded at 1440×1000 with four footage rows, two scene groups for the active video, the Monitor/AI Editor tabs, a live `CODEX SDK` runtime label, and the workspace-context header. From the AI Editor tab, the prompt “현재 선택된 영상이 무엇이고 어떤 내용인지 한 문장으로 알려줘” returned the correct active video and summary. The request completed with HTTP 200, the composer re-enabled, and no browser console/page errors were reported.

Screenshots:

- `/tmp/qa/travel-video-editor-2026-09-03/edit-desk.png` — desktop Edit Desk before chat
- `/tmp/qa/travel-video-editor-2026-09-03/agent-response-complete.png` — completed contextual answer in the AI Editor tab

## Reproducibility

Run the real service against a disposable state directory, then execute:

```bash
docs/qa/scripts/test-editor-agent.sh \
  http://127.0.0.1:8766 \
  work/qa-agent-YYYY-MM-DD/results
```

The reusable runner stores the exact request and every SSE event for each scenario plus a machine-readable summary. Evidence from this run is under `work/qa-agent-2026-09-03/results/`.

Relevant deterministic regression tests also pass:

```text
tests/editor: 6 passed
```

## Limitations and next tests

- This was one stochastic run per natural-language scenario; repeated runs are needed to measure reliability rather than capability.
- The source set covers one short food-court sequence, not multiple days, hundreds of clips, or sparse/failed analysis states.
- The test validates edit-plan semantics and source timestamps, not a rendered rough-cut's audiovisual rhythm.
- There is no negative test yet for stale concurrent chat edits at the live-agent layer, though the store-level stale-head regression passes.
- A future evaluation should score ten repeated ambiguous prompts, multi-day selection, missing STT, no-proxy scenes, interruption/cancellation, and model/tool timeout recovery.
