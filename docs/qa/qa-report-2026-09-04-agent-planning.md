# QA Report — conversational edit planning

**Date:** 2026-09-04

**Scope:** Editor Agent Contract revision 2 plan-first policy

**Runtime:** real Codex SDK backend using an isolated SQLite state and port 8766

## Result

PASS. The lightweight prompt/contract policy produced the intended three behaviors without adding a proposal table, approval API, or new SSE event.

## 1. Normal new edit request

Given two explicitly selected scenes, the user requested an 18-second rough cut with two 9-second clips.

The agent first called `get_scene_evidence` for only the two selected scenes. It then returned this grounded plan and stopped with `edit_id: null`:

1. `동행자의 즉석 표정 놀이`, source 35.000–44.000, 9 seconds
2. `두 병 음료 건배와 시음`, source 6.180–15.180, 9 seconds

The response described the reaction, toast, first-taste beat, and uncertain dialogue, then asked whether to create the cut. Neither `create_edit` nor `apply_edit_operations` ran in the planning turn.

## 2. Later conversational confirmation

In the same session, the user replied `좋아, 방금 설명한 구성 그대로 만들어줘.`

Only this second turn invoked:

```text
create_edit
apply_edit_operations
show_edit_revision
```

The resulting revision matched the proposed scene order and exact source ranges. It contained two clips of 9.0 seconds each, totaled 18.0 seconds, stayed inside the explicit selection, and passed edit-plan validation without warnings.

## 3. Unresolved reference

In a fresh session with no focused scene, selection, or active edit, the previous failing prompt `이거 좀 더 재밌게 해줘.` now returned a clarification. It invoked no tools and completed with `edit_id: null`.

The response explicitly said the scope of `이거` could not be resolved and asked the user to select a scene/edit or request a project-wide highlight.

## 4. Explicit immediate-execution bypass

The user requested: `편집계획 확인은 생략하고 바로 만들어줘` for the selected relationship-joke scene.

The agent inspected the one selected scene and created the revision in the same turn. The result contained only that scene at source 20.000–32.000, totaled exactly 12.0 seconds, and preserved the requested dialogue beat.

## Verification

```text
ruff: passed
tests/editor: 6 passed
contract JSON: valid
live contract revision: 2
```

## Remaining limitation

This is intentionally a conversational prompt policy, not a server-enforced approval boundary. It is appropriate while edits are reversible JSON revisions and the agent cannot render, publish, or mutate source media. Any future render, upload, external transfer, or destructive operation still requires a separate server-enforced approval mechanism.
