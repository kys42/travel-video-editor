# 채팅 기반 AI 영상 편집 플랫폼 설계

상태: 제안안 2026-09-03

대상: Travel Video Editor의 로컬 우선 데스크톱 웹

목표: 자연어로 여행 영상을 찾고, 검토 가능한 편집 초안을 만들고, 프록시로 반복한 뒤 4K 원본으로 재연결한다.

## 결론

구현 가능하다. 다만 “채팅창에서 Codex에게 셸을 열어준다”가 제품 구조가 되어서는 안 된다. 권장 구조는 다음과 같다.

1. 현재 Python 파이프라인과 `video-edit-plan/v1`을 서비스 API로 감싼다.
2. 에이전트는 원본 경로나 FFmpeg 명령이 아니라 제한된 도메인 도구만 호출한다.
3. 모든 편집은 원본을 건드리지 않는 불변 revision으로 저장하고 즉시 되돌릴 수 있게 한다.
4. 분석과 렌더는 Mac mini의 로컬 작업 큐가 담당한다.
5. 웹 채팅은 텍스트, 장면 카드, 편집 revision, 작업 상태와 UI 액션을 SSE로 받는다.
6. 에이전트 런타임은 교체 가능하게 둔다. 내부 MVP에서는 Codex SDK를 활용할 수 있고, 제품형 런타임은 Responses API 같은 function-calling 모델을 사용할 수 있다.

핵심은 에이전트 모델이 아니라 **도구 계약, 편집 revision, 로컬 미디어 워커**다. 이 셋이 안정적이면 Codex, OpenAI API, 다른 모델 또는 MCP 클라이언트가 같은 편집 시스템을 사용할 수 있다.

## 설계 원칙

- 원본 4K는 계속 읽기 전용이다.
- 모델에는 영상 전체나 거대한 JSON을 보내지 않는다.
- 검색은 인덱스, 판단은 선택된 장면 근거, 렌더는 결정적 도구가 담당한다.
- 모든 클립은 `asset_id + source_in + source_out`으로 원본까지 추적한다.
- 채팅 내역은 편집 상태의 원본이 아니다. 활성 edit revision이 유일한 편집 상태다.
- 에이전트의 변경은 새 revision을 만들며 기존 revision을 덮어쓰지 않는다.
- 모델이 임의 경로, 셸 명령, FFmpeg 필터 문자열을 제출할 수 없게 한다.
- 오래 걸리는 분석과 렌더는 동기 도구 호출로 기다리지 않고 job으로 분리한다.
- 프록시 미리보기와 원본 최종 렌더가 같은 소스 타임코드를 공유해야 한다.

## 사용자 경험

### Edit Desk 확장

현재 3열 Edit Desk를 유지하고 오른쪽 inspector에 탭을 추가한다.

```text
┌ Footage ──────┬ Review / Edit Timeline ───────────┬ Monitor | AI Editor ┐
│ 날짜·클립     │ 장면 행 또는 편집 클립 트랙       │ 소스 모니터          │
│ 검색·필터     │ 상세 행동·대화·특이 포인트        │ 채팅                  │
│ 분석 상태     │ revision diff / undo             │ 장면·클립·job 카드    │
└───────────────┴────────────────────────────────────┴──────────────────────┘
```

- 오른쪽 기본 탭은 `Monitor`, 옆 탭은 `AI Editor`다.
- inspector 폭은 360–520px 사이에서 조절할 수 있게 한다.
- 가운데는 `Review`와 `Edit` 두 모드를 가진다.
- 채팅에서 장면 카드를 클릭하면 Monitor가 해당 원본 구간으로 이동한다.
- 채팅에서 편집 초안을 만들면 가운데가 Edit 모드로 바뀌고 새 revision을 강조한다.
- 사용자는 생성된 클립을 드래그해 순서를 바꾸거나 trim할 수 있고, 이 변경도 같은 revision API를 사용한다.
- 채팅 카드와 타임라인은 같은 `scene_id`, `edit_clip_id`, `revision_id`를 공유한다.

### 채팅에 표시할 카드

`scene_ref_card`

- 대표 프레임
- 촬영 날짜와 원본 타임코드
- 장면 설명, 특이 포인트, 짧은 대화 근거
- 분석 신뢰도와 분석 수준
- `구간 재생`, `상세 보기`, `초안에 추가`

`clip_candidate_stack`

- 에이전트가 고른 후보를 하나의 묶음으로 표시
- 클립별 길이, 선택 이유, 시작/끝 여유, 대사 보존 여부
- 합계 길이와 목표 길이 차이
- 개별 제외, 고정, 대체 후보 요청

`edit_revision_card`

- “revision 4 · 7개 클립 · 01:27”처럼 표시
- 이전 revision과 추가/삭제/순서/trim diff
- `타임라인 열기`, `되돌리기`, `프록시 미리보기 렌더`

`job_card`

- `queued → running → validating → completed | failed | cancelled`
- 분석 중이면 현재 단계와 처리한 asset 수
- 렌더 중이면 프리셋, revision, 진행률, 예상 결과 위치
- 완료 시 `재생`, `파일 보기`, `원본 렌더` 버튼

### 대표 대화 흐름

사용자: “8월 25일 하이라이트 90초짜리 만들어줘. 음식 주문과 웃긴 반응은 살려줘.”

1. 에이전트가 날짜 범위의 분석 상태와 장면을 조회한다.
2. 분석이 비어 있는 asset만 `ensure_analysis` job으로 요청한다.
3. 이미 분석된 장면은 제목·특이 포인트·대화·품질 점수로 좁힌다.
4. 애매한 상위 후보만 상세 STT와 6–9장 맥락 프레임을 읽는다.
5. 새 edit을 만들고 source range, 순서, 이유를 batch operation으로 적용한다.
6. 채팅에 후보 클립 묶음과 revision diff가 나오고 가운데 Edit timeline이 열린다.
7. 사용자가 “주문은 줄이고 마지막 표정 더 길게”라고 말하면 현재 revision 기준으로 새 revision을 만든다.
8. “미리보기 만들어줘” 또는 버튼 클릭으로 프록시 render job을 시작한다.
9. 승인 후 같은 edit plan을 `media_mode=source`로 4K 원본에 재연결한다.

## 전체 아키텍처

```text
[React Edit Desk]
  │ same-origin REST + SSE
  ▼
[Editor API / FastAPI]
  ├─ Project · Asset · Scene · Transcript query
  ├─ Edit · Revision · Patch ownership
  ├─ Media Range / thumbnails
  ├─ Agent chat proxy
  └─ Job API + durable event log
       │
       ├──────────────► [Agent Runtime]
       │                 ├─ CodexBackend (internal MVP)
       │                 └─ ResponsesBackend (product)
       │                        │ typed tools only
       │                        ▼
       │                    [Editor API]
       │
       └──────────────► [Local Media Worker on Mac mini]
                         ├─ Phase 1 / STT / storyboard
                         ├─ proxy generation
                         ├─ render_edit.py dry-run
                         └─ proxy preview / source master render

[SQLite catalog + FTS5] ── indexes ──► [immutable JSON artifacts]
[T7 originals: read-only]
[ExternalSSD: proxies, jobs, previews, exports]
```

### 왜 브라우저가 에이전트를 직접 호출하면 안 되는가

- API 키와 로컬 미디어 권한이 브라우저에 노출된다.
- 모델이 만든 action과 실제 편집 mutation을 구분하기 어렵다.
- 연결이 끊기면 대화, tool call, job 상태가 서로 어긋난다.
- same-origin Editor API를 경유해야 사용자 인증, 권한, revision 충돌, 감사 로그를 한곳에서 처리할 수 있다.

이 구조는 `agent-web-guide`의 `브라우저 → same-origin proxy → agent server → service API` 패턴을 유지한다. 영상 서비스에서는 여기에 `durable edit state`와 `local job worker`를 추가한다.

## 에이전트 런타임 선택

### 안 A — Codex SDK를 로컬 에이전트로 사용

장점:

- 현재 `travel-video-pipeline`과 `video-editor` 스킬을 그대로 활용하기 쉽다.
- 로컬 Codex thread를 시작·재개할 수 있어 내부 도구를 빠르게 연결할 수 있다.
- Mac mini에서 저장소와 편집 산출물을 함께 다루는 단일 사용자 MVP에 적합하다.
- 복잡한 예외 상황에서 코드와 artifact를 직접 조사하는 능력이 강하다.

제약:

- 코딩 에이전트의 workspace 권한은 제품용 편집 도구보다 넓다.
- 채팅 카드, revision 이벤트, job 이벤트를 제품 프로토콜로 변환하는 adapter가 필요하다.
- 에이전트가 CLI를 직접 조합하게 두면 도구 계약과 감사 가능성이 약해진다.
- 다중 사용자 서비스로 확대할 때 세션·권한·리소스 격리가 복잡하다.

사용한다면 브라우저가 Codex를 직접 실행하지 않고 `CodexBackend`가 서버 측 thread를 관리한다. sandbox는 프로젝트 작업 디렉터리와 허용된 working-media root로 제한하고, 편집 동작은 가능하면 동일한 Editor API 또는 MCP 도구를 호출하게 한다.

### 안 B — 전용 Editor Agent + function calling

장점:

- `search_scenes`, `apply_edit_operations` 같은 엄격한 JSON 도구만 노출할 수 있다.
- 임의 셸과 파일 경로를 모델에게 주지 않아 권한 경계가 명확하다.
- 모델 교체, 비용 라우팅, 평가와 다중 사용자 운영이 쉽다.
- SSE 카드와 UI 액션을 도메인 이벤트로 바로 만들기 좋다.

제약:

- 기존 스킬의 규칙을 시스템 프롬프트, 도구 description, JSON 검증으로 옮겨야 한다.
- 예상하지 못한 artifact 문제를 스스로 디버깅하는 능력은 Codex 방식보다 제한된다.
- 초기 구현량이 더 크다.

### 권장 — 교체 가능한 Hybrid

```python
class AgentBackend(Protocol):
    async def stream_turn(
        self, session_id: str, message: str, context: ChatContext
    ) -> AsyncIterator[AgentEvent]: ...
```

- 1차 내부 MVP: `CodexBackend`
- 장기 기본: `ResponsesBackend`
- 회귀 테스트: `ScriptedMockBackend`
- Editor API와 Tool Gateway는 세 backend가 공유
- 필요하면 같은 Tool Gateway 위에 MCP adapter를 추가

즉, **별도의 에이전트 서비스는 두되 처음부터 별도의 모델에 종속될 필요는 없다.**

## 데이터 계층

### 저장 원칙

| 데이터 | 권장 저장소 | 역할 |
|---|---|---|
| 원본 영상 | T7 | 읽기 전용 authoritative media |
| 프록시·WAV·프레임·렌더 | ExternalSSD | 재생성 가능한 작업 미디어 |
| 현재 Phase 1/STT JSON | immutable artifact files | 분석 결과와 계보의 원본 |
| asset/scene/transcript 검색 | SQLite + FTS5 | 빠른 조회용 파생 인덱스 |
| edit revision | immutable JSON + SQLite metadata | 편집 상태의 원본 |
| chat/tool/job event | append-only SQLite tables | 복구와 감사 |

SQLite는 기존 artifact를 대체하지 않는다. 파일을 색인하고 ID와 경로, 지문, schema version을 기록한다. 인덱스를 지워도 artifact에서 다시 만들 수 있어야 한다.

### 핵심 엔터티

- `Project`: 미디어 루트, 날짜 범위, 기본 프리셋
- `Asset`: 안정적 asset ID, 원본 상대 경로, proxy 상태, 촬영 시각
- `AnalysisArtifact`: schema/version, 입력 지문, stage, 파일 위치, 상태
- `Scene`: asset ID, source in/out, 요약, 태그, notable score, frame refs
- `Utterance`: 원문 언어, 원문, 별도 번역, source in/out, provenance
- `Edit`: 사용자 brief와 목표 형식
- `EditRevision`: immutable plan, parent revision, 생성 주체, diff
- `EditClip`: source range, 목적, 이유, scene/utterance/notable IDs
- `RenderJob`: revision과 media mode에 고정된 비동기 작업
- `ChatSession`: 대화와 현재 project/edit 참조
- `ToolRun`: 입력, 출력 요약, 소요 시간, 오류, 관련 revision/job

### 편집 revision

기존 `video-edit-plan/v1`을 렌더 경계로 유지한다. 서비스 내부 revision에는 다음을 추가한다.

```json
{
  "schema_version": "video-edit-revision/v1",
  "edit_id": "edit_01...",
  "revision_id": "rev_04...",
  "parent_revision_id": "rev_03...",
  "created_by": {"type": "agent", "session_id": "chat_..."},
  "brief": "8월 25일 음식과 반응 중심 90초",
  "plan": {"schema_version": "video-edit-plan/v1", "clips": []},
  "change_summary": [],
  "validation": {"status": "valid", "warnings": []}
}
```

렌더 직전에는 revision 안의 plan을 현재 `render_edit.py --dry-run`으로 다시 검증한다. 실제 렌더 결과의 `.render.json`이 revision ID와 plan hash를 보존해야 한다.

## 토큰 효율적인 조회

에이전트에게 “모든 데이터를 읽을 수 있다”는 것은 모든 데이터를 매 턴 프롬프트에 넣는다는 뜻이 아니다.

1. `search_scenes`는 SQLite FTS/필터 결과의 짧은 summary와 ID만 반환한다.
2. 에이전트가 고른 상위 후보에만 `get_scene_evidence`를 호출한다.
3. 대화는 전체 STT 대신 관련 utterance ID와 짧은 원문 범위를 가져온다.
4. 시각 판단이 필요한 후보에만 맥락 프레임 또는 연락판을 최대 6–9장 반환한다.
5. 기존 video/day summary는 검색 recall과 첫 응답에 재사용한다.
6. 과거 채팅은 raw history 전체보다 사용자 선호와 확정된 편집 결정을 compact state로 유지한다.
7. tool output은 원시 artifact가 아니라 제한된 DTO를 반환한다.

권장 검색 단계:

```text
date / tag / FTS filter
  → scene candidate 20개
  → metadata + notable + transcript rerank
  → evidence detail 5–8개
  → 필요할 때만 contact sheet vision review
  → edit ranges
```

전체 영상을 모델이 순서대로 보는 작업은 기본 경로에 없다. 분석이 없는 구간이나 상위 후보의 맥락이 불명확할 때만 도구가 필요한 프레임을 추가 추출한다.

## 도구 계약

모델에는 절대경로 대신 ID만 받는 도구를 제공한다. 서비스가 ID를 허용된 루트 안의 실제 파일로 해석한다.

### Data Query

| 도구 | 용도 | 반환 크기 원칙 |
|---|---|---|
| `list_capture_dates` | 촬영 날짜, asset 수, 분석 준비율 조회 | 날짜별 집계만 |
| `search_scenes` | 날짜·검색어·태그·대화·특이점으로 장면 검색 | 기본 20개 이하 |
| `get_scene_evidence` | 선택 장면의 상세 행동, STT, 프레임 근거 조회 | 요청한 ID만 |
| `get_asset_detail` | 한 영상의 흐름, proxy/analysis 상태 조회 | 장면 summary 위주 |
| `get_edit` | 활성 revision과 클립 목록 조회 | 렌더 명령 제외 |
| `get_job` | 분석/렌더 작업 상태 조회 | 상태와 artifact refs |

`search_scenes` 예시:

```json
{
  "project_id": "trip_seattle_alaska",
  "query": "음식 주문 웃긴 반응",
  "captured_from": "2026-08-25T00:00:00",
  "captured_to": "2026-08-26T00:00:00",
  "filters": {
    "has_dialogue": true,
    "notable_types": ["reaction", "dialogue", "food"],
    "analysis_level": "reviewed"
  },
  "limit": 20
}
```

반환 항목은 `scene_id`, `asset_id`, `source_in/out`, `title`, 짧은 summary, notable, transcript excerpt refs, thumbnail ref와 score만 가진다.

### Durable Action

이 도구들은 프론트 DOM을 조작하는 action이 아니라 서비스 상태를 변경한다.

| 도구 | 동작 | 안전장치 |
|---|---|---|
| `ensure_analysis` | 누락된 분석 artifact job 생성 | 기존 완료 artifact 재사용 |
| `create_edit` | 비어 있는 edit와 revision 0 생성 | 원본 미변경 |
| `apply_edit_operations` | batch operation을 원자적으로 적용해 새 revision 생성 | base revision 충돌 검사 |
| `start_preview_render` | proxy 기반 preview job 생성 | revision hash 고정 |
| `cancel_job` | 실행 중 작업 취소 요청 | 부분 파일은 publish하지 않음 |
| `request_source_render` | 원본 재연결 master job 준비 | 명시적 승인 필요 |

`apply_edit_operations`의 operation은 제한된 union이다.

```json
{
  "edit_id": "edit_01...",
  "base_revision_id": "rev_03...",
  "intent": "주문은 짧게 하고 마지막 반응을 더 길게",
  "operations": [
    {"type": "trim_clip", "clip_id": "ec_03", "source_in": 93.2, "source_out": 102.7},
    {"type": "move_clip", "clip_id": "ec_07", "after_clip_id": "ec_03"},
    {
      "type": "add_source_range",
      "asset_id": "DJI_...",
      "source_in": 47.0,
      "source_out": 55.4,
      "after_clip_id": "ec_07",
      "label": "동행자의 마지막 표정",
      "reason": "웃긴 반응을 더 길게 보존",
      "evidence_ids": ["scene_G002", "notable_N003"]
    }
  ],
  "idempotency_key": "chat_turn_18_patch_1"
}
```

지원 operation의 초기 범위:

- `add_source_range`
- `remove_clip`
- `trim_clip`
- `move_clip`
- `set_clip_speed`
- `set_clip_volume`
- `set_overlay`
- `set_caption`

`replace_all`, arbitrary filter graph, arbitrary shell command는 초기 도구에 넣지 않는다.

### Display

Display 도구는 상태를 바꾸지 않고 chat SSE의 `card`를 만든다.

- `show_scene_refs(scene_ids)`
- `show_clip_candidate_stack(scene_ids, title)`
- `show_edit_revision(edit_id, revision_id)`
- `show_job(job_id)`
- `show_suggestion_buttons(items)`

가능하면 카드 본문 전체를 모델이 복제하지 않고 ID reference만 보낸다. 프론트가 Editor API에서 최신 데이터를 가져와 렌더링하면 토큰과 stale data를 줄일 수 있다.

### UI Action

UI action은 서비스 상태를 바꾸지 않는다. CSS selector를 모델에 허용하지 않고 semantic ID를 사용한다.

- `open_inspector_tab(tab: monitor | ai)`
- `open_workspace_mode(mode: review | edit)`
- `play_source_range(asset_id, source_in, source_out)`
- `focus_scene(scene_id)`
- `focus_edit_clip(edit_clip_id)`
- `compare_revisions(before_revision_id, after_revision_id)`
- `show_notification(level, message)`

### 절대 제공하지 않을 도구

- `run_shell(command)`
- `run_ffmpeg(args)`
- `read_file(path)` / `write_file(path)`
- 모델이 지정한 URL fetch
- 원본 삭제·이동·이름 변경
- 기존 export 덮어쓰기

필요한 기능은 모두 ID 기반의 작은 도메인 도구로 승격한다.

## 분석이 없을 때의 처리

`ensure_analysis`는 동기 분석기가 아니다.

```json
{
  "project_id": "trip_seattle_alaska",
  "asset_ids": ["asset_1", "asset_2"],
  "required_artifacts": ["scene_timeline", "transcript_reconciled", "storyboard"],
  "profile": "interactive",
  "priority": "user_waiting"
}
```

반환:

```json
{
  "job_id": "job_01...",
  "ready_asset_ids": ["asset_1"],
  "queued_asset_ids": ["asset_2"],
  "workflow_state": "waiting_for_analysis"
}
```

에이전트 턴은 여기서 `job_card`를 보여주고 종료해도 된다. job 완료 이벤트가 오면 다음 중 하나를 선택한다.

- MVP: “분석 완료, 계속하기” 버튼으로 같은 intent를 다시 실행
- 이후: durable workflow가 자동으로 agent continuation을 생성

자동 continuation에는 최초 사용자 메시지, 준비된 artifact ID와 현재 edit revision만 전달한다. 이전의 거대한 tool output을 다시 넣지 않는다.

## SSE와 작업 이벤트

### 채팅 스트림

기본 `agent-web-guide`의 7종을 유지한다.

- `text`: 답변 delta
- `status`: 현재 tool/workflow 상태
- `card`: scene, candidate, revision, job reference
- `action`: semantic UI action
- `suggestions`: 후속 요청
- `error`: 복구 가능한 오류 정보
- `done`: 이 agent turn 종료

모든 이벤트에는 다음 envelope를 붙인다.

```json
{
  "schema_version": "editor-agent-event/v1",
  "session_id": "chat_...",
  "turn_id": "turn_...",
  "sequence": 14,
  "correlation_id": "tool_...",
  "payload": {}
}
```

채팅 스트림은 FFmpeg가 끝날 때까지 열어두지 않는다. `start_preview_render`가 `job_id`를 반환하면 agent turn은 종료된다.

### Job 스트림

별도 `GET /api/jobs/{job_id}/events` SSE를 사용한다.

- 이벤트를 SQLite에 먼저 append한 뒤 스트림으로 전송
- SSE `id`에 durable sequence 사용
- `Last-Event-ID`로 재접속
- 여러 채팅/화면이 같은 job을 구독 가능
- job 완료 후 결과 artifact reference를 edit에 연결

## API 표면

초기 REST/SSE API:

```text
GET  /api/projects
GET  /api/projects/{id}/capture-dates
GET  /api/assets?project_id=&date=&analysis_status=
GET  /api/assets/{id}
GET  /api/assets/{id}/transcript
GET  /api/scenes/search
GET  /api/scenes/{id}/evidence

POST /api/analysis/jobs
GET  /api/jobs/{id}
GET  /api/jobs/{id}/events
POST /api/jobs/{id}/cancel

POST /api/edits
GET  /api/edits/{id}
GET  /api/edits/{id}/revisions/{revision_id}
POST /api/edits/{id}/operations
POST /api/edits/{id}/renders

GET  /api/media/proxy/{asset_id}
GET  /api/media/frame/{frame_id}
GET  /api/renders/{render_id}/media

POST /api/agent/chat
```

영상 endpoint는 HTTP Range를 지원해야 하며, asset ID를 서버 측 allowlisted 경로로 해석한다. 요청에서 실제 파일 경로를 받지 않는다.

## Mac mini 배치

초기 단일 사용자 환경에서는 복잡한 클라우드 큐가 필요 없다.

```text
launchd
  ├─ travel-video-api      FastAPI + 정적 React 파일
  ├─ travel-video-agent    agent adapter/runtime
  └─ travel-video-worker   SQLite job claim + subprocess
```

- VideoToolbox 접근 때문에 미디어 worker는 Mac 호스트에서 native process로 실행하는 편이 안전하다.
- SQLite의 `BEGIN IMMEDIATE`와 lease/heartbeat로 한 job을 한 worker만 claim한다.
- subprocess PID, log, partial path, plan hash를 job record에 둔다.
- worker 재시작 시 lease가 만료된 job을 복구한다.
- 외부에서 볼 때는 Tailscale 또는 인증된 reverse proxy를 사용하고 원본 경로는 노출하지 않는다.

규모가 커질 때만 PostgreSQL/Redis와 원격 object storage를 고려한다.

## 권한과 승인

| 작업 | 기본 처리 |
|---|---|
| 검색·상세 조회·구간 재생 | 자동 |
| 분석 artifact 생성 | 명시된 프로젝트 범위에서 자동, 예상 비용/시간 표시 |
| draft revision 생성·trim·재정렬 | 자동, 항상 undo 가능 |
| proxy preview 렌더 | 사용자 요청 또는 버튼 클릭 시 실행 |
| 4K source master 렌더 | 명시적 승인 |
| 게시·업로드·공유 | 별도 승인과 별도 integration |
| 원본 삭제·이동·덮어쓰기 | 지원하지 않음 |

도구 실행마다 `session_id`, `turn_id`, `user_intent`, `tool_args_hash`, 결과 revision/job ID를 남긴다. 동일 `idempotency_key`는 같은 mutation을 두 번 적용하지 않는다.

## 기존 video-editor 스킬과의 연결

`video-editor` 스킬에서 유지할 핵심 계약:

- 입력은 `video-edit-plan/v1`
- clip마다 source/proxy, source in/out, label, reason, provenance
- proxy 반복 후 source 재연결
- dry-run 검증
- partial 출력 후 검증 성공 시 원자적 publish
- SRT와 `.render.json` sidecar

서비스에서는 모델이 `render_edit.py`를 직접 실행하지 않는다.

```text
agent tool
  → validated edit operation
  → immutable edit revision
  → render job
  → worker builds plan file
  → render_edit.py --dry-run
  → render_edit.py --media-mode proxy|source
  → output validation + render manifest
```

장기적으로 렌더러 핵심을 `src/travel_video/editing/` 패키지로 승격하고, 스킬 script와 worker가 같은 Python API를 공유하면 중복을 줄일 수 있다. 스킬은 Codex/CLI 인터페이스로 계속 유지한다.

## MCP의 역할

MCP는 데이터베이스나 작업 큐를 대체하지 않는다. Editor API의 도구 표면을 재노출하는 adapter로 둔다.

```text
Editor API / Tool Gateway
  ├─ function-calling adapter → web Editor Agent
  ├─ MCP adapter              → Codex / ChatGPT / 다른 클라이언트
  └─ Python client            → skills / automation
```

이렇게 하면 웹 채팅과 현재 Codex 세션이 같은 scene 검색, edit revision, render job을 볼 수 있다. MCP에서도 mutation 도구는 revision과 approval 규칙을 그대로 따라야 한다.

## 제안 프로젝트 구조

```text
travel-video-editor/
├── apps/editor-web/                 # React + TypeScript Edit Desk
│   └── src/
│       ├── features/review/
│       ├── features/edit-timeline/
│       ├── features/agent-chat/
│       └── lib/agent-events/
├── services/editor-api/             # FastAPI REST/SSE, auth, media Range
├── services/agent-runtime/          # AgentBackend adapters + prompts + tools
├── workers/media-worker/            # durable job runner
├── src/travel_video/
│   ├── catalog/                     # artifact index + FTS
│   ├── editing/                     # revision/schema/render bridge
│   └── jobs/                        # SQLite queue primitives
├── mcp/travel-video-editor/         # optional MCP adapter
├── skills/                          # Codex-facing workflows
└── docs/
```

FastAPI를 선택하는 이유는 현재 분석 파이프라인과 renderer가 Python이기 때문이다. 프론트는 복잡한 selection, revision diff, drag reorder와 SSE 상태 동기화 때문에 기존 정적 HTML과 별도의 React/TypeScript 앱이 유리하다. 기존 HTML은 공유 가능한 읽기 전용 report로 계속 보존한다.

## 구현 단계

### Phase A — API foundation

- 기존 JSON artifact를 읽는 catalog importer
- SQLite asset/scene/utterance/FTS 인덱스
- ID 기반 frame/proxy media endpoint
- edit/revision schema와 `video-edit-plan/v1` 변환
- job/event tables와 로컬 worker

완료 기준: 모델 없이 API만으로 장면을 검색하고 edit revision을 만들어 dry-run할 수 있음.

### Phase B — Read-only AI Editor

- 오른쪽 AI Editor 탭
- same-origin `POST /api/agent/chat`
- SSE parser와 semantic action dispatcher
- `list_capture_dates`, `search_scenes`, `get_scene_evidence`
- scene/ref/candidate 카드
- scripted mock backend E2E

완료 기준: “8월 25일 음식과 웃긴 장면 찾아줘”가 실제 장면 카드와 Source Monitor 재생으로 이어짐.

### Phase C — Draft editing

- `create_edit`, `apply_edit_operations`, optimistic revision
- 가운데 Edit timeline과 chat revision card
- undo/redo, drag reorder와 agent mutation의 단일 API화
- tool audit와 idempotency

완료 기준: 자연어 수정과 직접 조작을 번갈아 해도 revision이 일관됨.

### Phase D — Analysis/render jobs

- `ensure_analysis`, `start_preview_render`, `cancel_job`
- job SSE 재접속과 진행 카드
- proxy preview 재생
- 실패 복구와 artifact provenance

완료 기준: 분석이 없는 asset을 자동 준비하고, 선택한 revision의 preview를 생성·검증함.

### Phase E — Source relink/export

- 명시적 승인 UI
- 4K source master render
- 자막/manifest 다운로드
- 이후 필요 시 FCPXML/OTIO export

완료 기준: 동일 revision을 프록시와 원본에서 렌더하고 source range가 일치함.

## MVP에서 하지 않을 것

- 여러 전문 에이전트가 서로 토론하는 구조
- 전체 원본 영상을 매 요청마다 vision 모델에 전달
- 임의 셸/FFmpeg 도구
- collaborative multi-user timeline
- cloud upload와 공개 게시
- 자동 음악 다운로드
- 색보정·안정화·멀티캠 등 NLE 전체 기능

처음에는 **단일 orchestrator + 결정적 도구 + 로컬 worker**가 가장 검증하기 쉽다. 저렴한 보조 모델이나 병렬 에이전트는 날짜별 요약 또는 후보 rerank가 실제 병목으로 확인된 뒤 추가한다.

## 초기 평가 시나리오

1. “8월 25일 음식 관련 장면을 시간순으로 보여줘.”
2. “대화가 확실한 주문 장면만 골라줘.”
3. “웃긴 반응을 포함해 60초 초안을 만들어줘.”
4. “두 번째 클립을 2초 줄이고 마지막 반응을 앞으로 옮겨줘.”
5. “현재 revision과 이전 revision 차이를 보여줘.”
6. “프록시 미리보기 렌더해줘.”
7. 분석이 없는 날짜에 같은 요청을 했을 때 job 생성과 재개가 되는지 확인.
8. 외장 SSD 분리, stale revision, 렌더 실패, SSE 재접속을 각각 검증.

평가 지표:

- 선택된 range가 존재하는 asset과 유효한 source duration 안에 있는 비율
- 사용자가 선택을 유지한 비율과 제거한 비율
- 목표 길이 오차
- 대화/반응 cut boundary 만족도
- 평균 tool call 수와 agent 입력 토큰
- 분석 cache hit율
- render 성공/재시도율
- revision 충돌 또는 중복 mutation 0건

## 바로 다음 구현 권장 범위

첫 구현은 렌더보다 **read-only AI Editor + edit revision API**가 좋다.

1. 현재 4개 음식 영상을 SQLite catalog로 가져온다.
2. `search_scenes`, `get_scene_evidence`, `play_source_range` 세 도구를 만든다.
3. 오른쪽에 AI Editor 탭과 scene reference 카드를 붙인다.
4. `create_edit`, `apply_edit_operations`로 JSON revision만 만든다.
5. 현재 `render_edit.py --dry-run`을 revision validation에 연결한다.

이 다섯 단계만으로도 채팅에서 장면을 찾고 클립을 조합해 편집 초안을 만드는 핵심 경험을 검증할 수 있다. 렌더 job은 이 상태 모델이 안정된 뒤 붙이는 것이 좋다.

## 참고한 기존 패턴과 공식 문서

- 로컬 `agent-web-guide`: same-origin proxy, Agent Server, service API, SSE text/card/action 분리
- `ys-homepage`: Cloudflare proxy → Python agent server → API tool → frontend action dispatcher의 실제 구현
- 로컬 `video-editor` 스킬: `video-edit-plan/v1`, proxy/source relink, dry-run, atomic render, sidecar manifest
- [OpenAI Codex SDK](https://developers.openai.com/codex/sdk): 서버 측 앱에서 로컬 Codex thread를 시작·계속·재개하는 SDK와 sandbox preset
- [OpenAI Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create): multi-turn conversation과 custom function tool 제공
- [OpenAI Function calling](https://developers.openai.com/api/docs/guides/function-calling): 모델 tool call을 애플리케이션이 실행하고 결과를 다시 모델에 전달하는 흐름
- [OpenAI Background mode](https://developers.openai.com/api/docs/guides/background): 오래 걸리는 모델 응답의 비동기 실행과 재연결 가능한 스트리밍
