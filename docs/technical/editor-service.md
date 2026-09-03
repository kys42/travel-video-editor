# Editor Service — executable technical contract

Status: vertical slice implemented, 2026-09-03
Owners: Editor API, Agent Runtime, Edit Desk UI

## Product boundary

The Editor Service lets a user search reviewed travel-video evidence and create reversible rough-cut revisions through a right-hand AI editor. It does not grant an agent arbitrary shell, filesystem, FFmpeg, or source-media mutation access.

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
| [`editor-tools.v1.json`](../contracts/editor-tools.v1.json) | Codex prompt, Tool Gateway, API docs | every catalog entry has exactly one registered handler |
| [`agent-events.v1.schema.json`](../contracts/agent-events.v1.schema.json) | SSE server and Edit Desk client | event names and payload types are closed sets |
| FastAPI `/openapi.json` | HTTP clients and tests | generated from request models and route definitions |
| `video-edit-plan/v1` | edit store and FFmpeg renderer | source ranges are validated before a revision is committed |

Changes start in the contracts. A tool name, category, approval rule, or event shape must not be changed in only the prompt or only the frontend.

The tool catalog also owns the user-facing identity, capability workflows, example requests, and safety boundaries. The agent prompt, `/api/contracts/tools`, and `/guide` consume that same document so operational guidance cannot drift independently from the executable catalog. See the [AI Editor capability guide](agent-capabilities.md).

## Runtime topology

```text
Edit Desk browser
  └─ POST /api/agent/chat (same origin, SSE response)
       └─ AgentOrchestrator
            ├─ CodexBackend (structured output, read-only, deny approvals)
            ├─ DemoBackend (deterministic local development)
            └─ ToolGateway
                 ├─ TimelineCatalog (immutable reviewed JSON)
                 └─ EditStore (SQLite, immutable revisions)
```

The Codex backend uses `openai-codex` on the server. It creates or resumes a Codex thread, supplies the compact tool catalog and current page/edit context, and requires a JSON response matching the agent decision schema. Codex proposes typed tool calls. The `ToolGateway` validates and executes them; results are returned to the same thread until it produces a final response or the turn limit is reached.

Codex is intentionally started with:

- `Sandbox.read_only`;
- `ApprovalMode.deny_all`;
- project root as `cwd`;
- a developer instruction forbidding shell/file tools for editor state;
- the machine-readable tool catalog as the only application capability list.

This is defense in depth. The actual edit authority exists only in `ToolGateway`.

## Read path and token budget

1. `list_assets` returns one compact row per video.
2. `search_scenes` searches summaries, tags, notable moments, and short original-language dialogue excerpts.
3. `get_scene_evidence` is allowed only for a shortlist and returns one scene at a time.
4. Frame references remain IDs/paths. Image bytes are never inserted into the agent prompt by default.
5. Full timeline JSON and full raw STT are not returned by any agent tool.

The server logs each tool duration and a compact result summary, not the full transcript payload.

## Workspace context envelope

The browser sends only lightweight interaction state: current asset ID, focused scene ID, explicitly selected scene IDs, visible asset IDs, search query, and the monitor range. The server validates those IDs against `TimelineCatalog` and expands them into `editor-ui-context/v1` before the model sees them.

The resolved snapshot always includes project counts and compact current-asset metadata. A focused scene describes navigation/monitor state; it is not an edit selection. Explicit selection may span multiple scenes and multiple videos and is passed in capture-time order with title, source range, short scene summary, short dialogue excerpt, notable titles, and highlight state. This lets the agent answer ordinary “this video/scene/these selections” questions without a redundant tool call. Detailed STT, segment actions, frames, and edit state remain on-demand tool data.

Context references are resolved as follows:

| User reference | Snapshot field | Default behavior |
|---|---|---|
| “이 장면” | `workspace.focused_scene` | Answer from compact context; fetch evidence only for missing detail |
| “이 영상” | `workspace.current_asset` | Use the current video summary |
| “선택한 장면들”, “이것들” | `workspace.selected_scenes` | Treat all explicitly selected scenes as the candidate edit scope |
| Project-wide wording | `project` | Use project counts; call catalog tools when actual rows are needed |

The raw browser envelope is limited to 16 KB, explicit selection to 24 scenes, and visible assets to 100 IDs. Unknown IDs fail closed with a client error instead of entering the prompt.

## Write path and concurrency

`create_edit` creates revision 1. `apply_edit_operations` requires `expected_revision_id`; a stale caller receives `revision_conflict` rather than silently overwriting a newer edit. A successful operation batch is applied in a single SQLite transaction and creates one new snapshot.

Supported operations are `add_scene`, `trim_clip`, `move_clip`, `remove_clip`, `set_title`, and `set_target_duration`. Ranges are checked against the reviewed scene and source duration. Each clip stores:

- stable clip ID;
- asset and scene ID;
- authoritative source path and optional proxy path;
- source in/out and computed duration;
- label, selection reason, and analysis provenance.

## HTTP and stream protocol

- `GET /health` — process, backend, catalog, and database readiness.
- `GET /api/project` — compact project metrics and backend mode.
- `GET /api/assets` — compact assets.
- `GET /api/scenes/search` — deterministic scene search.
- `GET /api/scenes/{scene_id}` — one evidence packet.
- `POST /api/edits` — create an empty draft.
- `GET /api/edits/{edit_id}` — read edit and revision history.
- `POST /api/edits/{edit_id}/operations` — optimistic-concurrency mutation.
- `POST /api/agent/chat` — SSE stream using the seven event types in the event contract.
- `GET /guide` — live operations manual rendered from the current tool contract and runtime health.

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
- stale revision writes are rejected;
- every added clip is traceable to source and reviewed scene coordinates;
- demo backend completes an end-to-end search → cards → draft revision flow;
- Codex backend uses structured output and can resume the stored session thread;
- the Edit Desk supports cancelable SSE, scene cards, semantic actions, and persistent session IDs;
- existing Phase 1 tests and new editor tests pass.

## Deferred production work

- authenticated multi-user/project authorization;
- background analysis/render worker and durable job events;
- FTS5 rebuild/import command for hundreds of assets;
- proxy/source render approval UI;
- image-evidence tool with bounded contact-sheet generation;
- evaluation fixtures for common edit briefs and model/tool-call regressions.
