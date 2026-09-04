# [QA] Unresolved reference can create an unsolicited edit revision

## Severity

HIGH

## Reproduction

1. Start a fresh AI Editor chat with no active edit, no focused scene, and no selected scenes.
2. Send `이거 좀 더 재밌게 해줘.`
3. Observe the SSE tools and final response.

## Expected behavior

The agent explains that `이거` / `더` cannot be resolved and asks the user to choose an existing edit, selected scenes, a current video, or a project-wide scope and target duration. It must not invoke a durable-action tool.

## Actual behavior

The real Codex SDK backend invoked:

```text
search_scenes
get_scene_evidence
create_edit
apply_edit_operations
show_edit_revision
```

It created a 35-second four-clip edit and reported it as complete.

## Impact

Original media remains safe because edits are immutable drafts, but the agent invents scope for a durable action. Users cannot tell whether a revision reflects their intended active edit or a project-wide creative assumption.

## Recommended change

Add a fail-closed unresolved-reference rule to `editor-agent.v1.json` and enforce it before durable tool dispatch. Comparative/deictic requests such as `이거`, `더`, `아까 것`, and `방금 편집` require a resolvable focused scene, explicit selection, or active edit. Otherwise return a clarification without mutation.

Add a contract-driven end-to-end regression case using the exact reproduction prompt.

## Resolution — 2026-09-04

Editor Agent Contract revision 2 adds a lightweight conversational gate rather than a persisted proposal or server approval object. For normal new edits and substantial restructuring, the agent inspects relevant scene evidence, explains the intended scene order, source ranges, durations, dialogue/actions, and uncertainty, then ends the turn without a durable call. A later user confirmation creates the revision. Explicit “skip confirmation and execute now” wording and precise small changes to an active edit remain intentional shortcuts.

The exact reproduction prompt now returns a clarification with no query or durable tool calls and no edit ID. A separate real-Codex test confirmed that a selected two-scene request produced an evidence-grounded plan with no edit, then created the exact 18-second revision only after “그대로 만들어줘.”

## Evidence

- QA report: `docs/qa/qa-report-2026-09-03-agent.md`
- Raw SSE trace: `work/qa-agent-2026-09-03/results/08-ambiguous-request.json`
- Contract: `docs/contracts/editor-agent.v1.json`, revision 1
- Resolution contract: `docs/contracts/editor-agent.v1.json`, revision 2
