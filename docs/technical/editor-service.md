# Editor Service — executable technical contract

Status: vertical slice implemented, 2026-09-04
Owners: Editor API, Agent Runtime, Edit Desk UI

## Product boundary

The Editor Service lets a user search reviewed travel-video evidence, inspect a bounded source range, review a structured edit proposal, and create reversible rough-cut revisions through a right-hand AI editor. It does not grant the model arbitrary shell, filesystem, FFmpeg, or source-media mutation access.

The authoritative boundaries are:

- reviewed timeline JSON is immutable analysis evidence;
- SQLite is the durable service state and audit log;
- every edit write creates a `video-edit-revision/v1` snapshot;
- `video-edit-plan/v1` remains the renderer handoff format;
- T7 source media is read-only and every clip retains source coordinates;
- browsers receive semantic cards and actions, never executable code.

## Shared contracts

| Contract | Consumer | Runtime assertion |
|---|---|---|
| [`editor-agent.v1.json`](../contracts/editor-agent.v1.json) | Context resolver, Codex prompt, Tool Gateway, API docs, live guide | contract metadata and policy are required; every tool has exactly one handler |
| [`agent-events.v1.schema.json`](../contracts/agent-events.v1.schema.json) | SSE server and Edit Desk client | event names and payload types are closed sets |
| FastAPI `/openapi.json` | HTTP clients and tests | generated from request models and route definitions |
| `video-edit-plan/v1` | edit store and FFmpeg renderer | source ranges are validated before a revision is committed |

Changes start in the unified Editor Agent Contract. Context limits, reference resolution, tool authority, approval rules, runtime boundaries, or event behavior must not be changed only in code, only in the prompt, or only in the frontend.

The contract also owns the user-facing identity, capability workflows, examples, safety boundaries, context policy, decision policy, runtime policy, event names, and change process. The context resolver, agent prompt, Tool Gateway, `/api/contracts/agent`, and `/guide` consume that same document so operational guidance cannot drift from executable behavior. See the [AI Editor capability guide](agent-capabilities.md).

## Runtime topology

```text
Edit Desk browser
  └─ POST /api/agent/chat (same origin, SSE response)
       └─ AgentOrchestrator
            ├─ CodexBackend (structured output, read-only, deny approvals)
            ├─ DemoBackend (deterministic local development)
            └─ ToolGateway
                 ├─ TimelineCatalog (immutable reviewed JSON)
                 ├─ SceneEvidenceService (bounded, cached contact sheets)
                 └─ EditStore (SQLite, proposals + immutable revisions)
```

The Codex backend uses `openai-codex` on the server. It creates or resumes a Codex thread, supplies the compact tool catalog and current page/edit context, and requires a JSON response matching the agent decision schema. Codex proposes typed tool calls. The `ToolGateway` validates and executes them; results are returned to the same thread until it produces a final response or the turn limit is reached. Contract revision 6 represents the plan as a server-validated, session-owned proposal before revision mutation and makes existing-edit replacement versus append semantics explicit. The proposal and candidate ranges are durable, inspectable state; interpreting a later conversational confirmation remains model policy rather than a high-risk server approval token. Explicit immediate execution and precise small active-edit changes remain documented exceptions.

Codex is intentionally started with:

- `Sandbox.read_only`;
- `ApprovalMode.deny_all`;
- project root as `cwd`;
- a developer instruction forbidding shell/file tools for editor state;
- the machine-readable tool catalog as the only application capability list.

This is defense in depth. The actual edit authority exists only in `ToolGateway`.

## Read path and token budget

1. `list_assets` returns one compact row per video.
2. `search_scenes` searches summaries, tags, notable moments, and short corrected
   `reviewed_dialogue.captions[].display_text` excerpts. When that authoritative
   field exists, legacy dialogue summaries and raw STT do not override or pollute
   discovery.
3. `get_scene_evidence` is allowed only for a shortlist and returns one scene at a
   time. Corrected caption lines are the primary dialogue payload; legacy
   utterances are returned only when reviewed captions are unavailable.
4. `inspect_scene_range` is reserved for exact dialogue, cut-boundary, or visual ambiguity. It returns reviewed captions, source utterances, optional raw STT with explicit provenance, nearby dialogue context, and at most 12 frame cells inside one reviewed scene.
5. The evidence service reuses analyzed frames first. If they are too sparse and a proxy exists, it invokes a fixed FFmpeg extraction internally and atomically caches one contact sheet. The sheet is attached once to the Codex turn as `LocalImageInput`; its path is not copied into the textual prompt.
6. Full timeline JSON, full raw STT, proxy bytes, and 4K source bytes are never returned to the model.

The server logs each tool duration and a compact result summary, not the full transcript payload.

## Workspace context envelope

The browser sends only lightweight interaction state: current asset ID, focused scene ID, explicitly selected scene IDs, visible asset IDs, search query, and the monitor range. The server validates those IDs against `TimelineCatalog` and expands them into `editor-ui-context/v1` before the model sees them.

The resolved snapshot always includes a project overview: capture-date range, total duration, top tags, per-day rollups, and a compact chronological asset index. The index includes up to 40 videos; longer libraries retain complete day-level orientation and mark the asset index as truncated. Up to five videos around the current asset are included with slightly richer summaries.

A focused scene describes navigation/monitor state; it is not an edit selection. Explicit selection may span multiple scenes and multiple videos and is passed in capture-time order with title, source range, short scene summary, short dialogue excerpt, notable titles, and highlight state. This lets the agent answer ordinary project/this-video/this-scene/these-selections questions without a redundant tool call. Detailed STT, segment actions, frames, exact rows outside a truncated index, and edit state remain on-demand tool data.

Context references are resolved as follows:

| User reference | Snapshot field | Default behavior |
|---|---|---|
| “이 장면” | `workspace.focused_scene` | Answer from compact context; fetch evidence only for missing detail |
| “이 영상” | `workspace.current_asset` | Use the current video summary |
| “선택한 장면들”, “이것들” | `workspace.selected_scenes` | Treat all explicitly selected scenes as the candidate edit scope |
| Project-wide wording | `project.day_rollups`, `project.asset_index` | Use the overview first; query only for exact rows outside a truncated index |

The raw browser envelope is limited to 16 KB, explicit selection to 24 scenes, and visible assets to 100 IDs. Unknown IDs fail closed with a client error instead of entering the prompt.

## Write path and concurrency

`create_edit_proposal` validates one to 24 finite candidate ranges against reviewed scenes and stores an immutable proposal payload with a draft/application status. It does not create an edit revision. The UI can apply all or a selected subset; a proposal cannot be applied twice or read/applied from a different chat session. Proposal persistence and the session's active-proposal pointer are committed atomically.

`apply_edit_proposal` converts the selected candidates into source-linked clips in the proposal's stored order. For an existing edit, `replace_all` atomically replaces the clip sequence without expanding the old timeline into one remove operation per clip; `append` adds candidates after the current clips. `create_edit` creates revision 1. `apply_edit_operations` requires `expected_revision_id`; a stale caller receives `revision_conflict` rather than silently overwriting a newer edit. A successful operation batch creates one new snapshot.

Supported operations are `add_scene`, `trim_clip`, `move_clip`, `remove_clip`, `set_title`, and `set_target_duration`. Ranges are checked against the reviewed scene and source duration. Each clip stores:

- stable clip ID;
- asset and scene ID;
- authoritative source path and optional proxy path;
- source in/out and computed duration;
- label, selection reason, and analysis provenance.

## HTTP and stream protocol

- `GET /health` — process, backend, catalog, and database readiness.
- `GET /api/project` — compact project metrics and backend mode.
- `GET /api/contracts/agent` — authoritative live Editor Agent Contract.
- `GET /api/contracts/tools` — compatibility alias returning the same contract.
- `GET /api/assets` — compact assets.
- `GET /api/scenes/search` — deterministic scene search.
- `GET /api/scenes/{scene_id}` — one evidence packet.
- `GET /api/evidence/contact-sheets/{artifact_id}.jpg` — immutable cached sheet produced by a successful bounded inspection.
- `POST /api/proposals` — validate and persist a reviewable edit proposal.
- `GET /api/proposals/{proposal_id}?session_id=…` — read same-session proposal state and candidate ranges.
- `POST /api/proposals/{proposal_id}/apply` — apply all or selected candidates as a revision, with the owning `session_id` in the body.
- `POST /api/edits` — create an empty draft.
- `GET /api/edits/{edit_id}` — read edit and revision history.
- `POST /api/edits/{edit_id}/operations` — optimistic-concurrency mutation.
- `POST /api/agent/chat` — SSE stream using the seven event types in the event contract.
- `GET /guide` — live operations manual rendered from the current tool contract and runtime health.
- `GET /rough-cut` — serves the same generated Review Workspace as `/`; the client switches only the revision/chat companion layout.

The normal SSE order is `status* → card/action* → text* → suggestions? → done`. An `error` terminates the stream. The UI treats a transport close without `done` as an incomplete turn.

## Operating modes

`--agent-backend auto` selects Codex when the SDK package is installed, otherwise it uses the deterministic demo backend and exposes that fact in health/UI. Authentication or runtime failures remain visible instead of silently changing the backend. `codex` fails closed if the SDK is unavailable. `demo` exercises the full server, tool, card, action, and revision path without model cost.

Example:

```bash
uv run travel-video serve-editor \
  work/food-sequence/library/manifest.json \
  --state-dir work/editor-state \
  --agent-backend codex \
  --host :: --port 8765
```

Open `http://localhost:8765`. Do not open the generated page with `file://` when testing agent chat; the static page remains useful for review and presents a clear server-required message for chat.

## Acceptance criteria for this slice

- contracts and registered tool handlers match at startup;
- all four food-sequence timelines are searchable without loading images into prompts;
- deep inspection stays inside one reviewed source range, caps transcript and frame evidence, distinguishes raw STT provenance, and caches the contact sheet;
- each evidence sheet is attached to a Codex session only once unless a new artifact is requested;
- a planning turn creates a validated proposal but no edit ID, and a confirmed whole or partial application creates the source-linked revision;
- stale revision writes are rejected;
- the live contract exposes the plan-first conversational mutation policy;
- every added clip is traceable to source and reviewed scene coordinates;
- demo backend completes an end-to-end search → proposal → confirmation → draft revision flow;
- Codex backend uses structured output and can resume the stored session thread;
- the Edit Desk supports cancelable SSE, scene cards, semantic actions, and persistent session IDs;
- Review and Rough Cut contain one shared footage/scene workspace implementation, and revision clips navigate back to their authoritative scene evidence;
- revision cards expose source in/out coordinates, the visible Rough Cut program monitor plays that bounded proxy range, and cached UI references are scoped by library fingerprint and validated against the server;
- existing Phase 1 tests and new editor tests pass.

## Deferred production work

- authenticated multi-user/project authorization;
- background analysis/render worker and durable job events;
- FTS5 rebuild/import command for hundreds of assets;
- proxy/source render approval UI;
- production media-worker isolation for contact-sheet FFmpeg jobs;
- evaluation fixtures for common edit briefs and model/tool-call regressions.
