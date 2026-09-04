# Golden 03 — 에이전트와 함께 검토·편집하는 Edit Desk

상태: **규범 문서**

입력 정본:

- [Golden 01 — 원본 영상에서 편집 근거까지](01-raw-to-editorial-evidence.md)
- [Golden 02 — 편집 근거에서 하이라이트까지](02-editorial-evidence-to-highlight.md)

실행 계약:
[Editor Agent Contract](../contracts/editor-agent.v1.json),
[Agent SSE Event Contract](../contracts/agent-events.v1.schema.json)

범위: Review Workspace, Rough Cut, AI Editor, 편집 revision과 검토용 프록시

이 문서는 Golden 01의 편집 근거와 Golden 02의 편집 결정을 사람이 웹에서
살펴보고 에이전트와 함께 수정하는 제품 계약이다. 특정 모델, SDK, HTML 구조나
현재 프로토타입에 종속되지 않으며, 앞으로 웹과 에이전트 런타임을 교체해도
유지해야 할 사용자 경험과 안전 경계를 정의한다.

## 1. 목표

Edit Desk는 대용량 여행 영상을 다시 전수 시청하지 않고도 다음 질문에 답하고
행동할 수 있어야 한다.

- 현재 프로젝트에 어떤 영상과 사건이 있는가?
- 특정 행동, 대화, 반응과 장소는 어느 원본의 몇 초에 있는가?
- 에이전트는 어떤 근거로 장면을 골랐고 무엇을 확인하지 못했는가?
- 여러 영상의 장면을 어떤 순서와 길이로 사용할 것인가?
- 변경 전후 편집안을 어떻게 비교하고 이전 상태로 돌아갈 수 있는가?
- 선택한 컷을 프록시에서 확인한 뒤 같은 좌표를 4K 원본에 적용할 수 있는가?

이 제품의 결과는 자유 텍스트 추천이 아니다. 모든 편집 결과는 검토 가능한
장면 근거와 `asset_id + source_in + source_out`에 연결된 구조화 revision이다.

## 2. 정본과 책임 경계

문서와 런타임의 책임을 다음처럼 나눈다.

| 정본 | 소유하는 내용 |
|---|---|
| Golden 01 | 원본에서 장면·행동·대화·프레임 근거를 만드는 방법 |
| Golden 02 | 좋은 편집 단위, 이야기 구조, 대화 경계와 하이라이트 품질 |
| Golden 03 | 사람·웹·에이전트가 근거와 편집 결정을 주고받는 방식 |
| `editor-agent.v1.json` | 현재 실행 가능한 컨텍스트, 도구, 정책과 권한의 정확한 스키마 |
| `agent-events.v1.schema.json` | 브라우저로 전달되는 SSE 이벤트의 wire contract |
| `video-edit-plan/v1` | 렌더러로 넘기는 source-linked 편집 계획 |

충돌할 때는 주제별 정본을 따른다.

- 근거 데이터의 의미는 Golden 01이 우선한다.
- 컷 선택과 대화 완결성은 Golden 02가 우선한다.
- 웹·에이전트 사용자 흐름은 Golden 03이 우선한다.
- 실제 도구 이름, 입력 필드와 이벤트 형식은 versioned JSON 계약이 우선한다.

기능·권한·도구·컨텍스트·이벤트를 바꿀 때 Golden 문구만 고치지 않는다.
실행 계약을 먼저 변경하고 서버, 에이전트 어댑터, 웹, 테스트와 `/guide`를 같은
변경에서 갱신한다.

## 3. 제품 불변 원칙

### 하나의 근거 화면을 공유한다

Review와 Rough Cut은 영상 목록과 장면 상세를 복제하지 않는다. 같은 Review
Workspace 인스턴스를 공유하고, 모드에 따라 주변 도구만 달라진다. 장면 설명,
세부 행동, STT, 대표 프레임을 고치면 두 모드에 동시에 반영돼야 한다.

### 포커스와 편집 선택은 다르다

- 포커스 장면은 지금 보고 있는 탐색 상태다.
- 명시적 선택은 편집 후보로 지정한 하나 이상의 장면이다.
- 선택은 여러 영상에 걸칠 수 있고 원본 촬영 순서를 보존한다.
- 에이전트는 포커스 하나를 여러 장면의 편집 범위로 확대해 해석하지 않는다.

### 모델은 근거를 단계적으로 읽는다

프로젝트 개요와 현재 작업 범위는 매 턴 작은 컨텍스트로 제공한다. 전체 타임라인,
모든 원시 STT와 이미지 바이트는 기본 프롬프트에 넣지 않는다. 상세 행동·대화·
프레임은 shortlist에 든 장면에만 조회한다. 대사나 컷 경계가 모호하면 선택한
source range에 한해 검토 자막, 앞뒤 대화, 명시적으로 요청한 원시 STT와 최대
12프레임 contact sheet를 만든다. 기존 분석 프레임을 우선 재사용하고 부족할 때만
프록시에서 캐시 가능한 시트를 생성한다.

### 보지 않은 것을 봤다고 하지 않는다

에이전트가 분석 JSON만 읽었다면 “원본 영상을 봤다”고 표현하지 않는다. 장면
설명, 대화와 품질 판단은 성공적으로 조회한 근거 안에서만 말하고 누락된 사건이나
화자를 추측하지 않는다.

### 대화 계획이 durable action보다 먼저다

새 편집과 큰 구조 변경은 사용할 장면, 순서, 길이, 주요 행동·대화와 불확실성을
검증된 `video-edit-proposal/v1`로 먼저 설명한다. 사용자는 후보별로 검토하거나
일부만 선택할 수 있고, 동의하거나 수정한 다음 revision을 만든다. 사용자가
명시적으로 즉시 실행을 요청했거나 현재 edit의 작은 변경만 정확히 지정한 경우만
예외로 한다. 기존 edit을 대상으로 한 proposal은 후보가 기존 컷 전체를 교체하는
최종 순서인지(`replace_all`), 기존 컷 뒤에 추가할 순서인지(`append`)를 명시한다.

### 제안은 현재 맥락에서 에이전트가 만든다

30초·60초·90초는 제품의 고정 선택지가 아니다. 사용자가 길이를 지정하지 않으면
에이전트가 목적, 공개 매체, 선택 범위, 사건 수, 대화 완결성과 근거 밀도를 보고
적절한 길이나 확인 질문을 제시한다.

- 런타임 `suggestions`는 현재 응답과 작업 상태에서 에이전트가 생성한다.
- UI의 정적 quick prompt는 일반 기능 예시일 뿐 개인화된 추천처럼 표시하지 않는다.
- 정적 예시는 특정 길이를 기본 정답처럼 반복하지 않는다.
- Demo backend의 고정 응답은 반드시 `DEMO · RULES`로 표시한다.

### 원본과 이전 revision은 변경하지 않는다

프록시는 분석과 검토를 위한 대체 미디어일 뿐 시간 좌표의 정본이 아니다. 편집은
새 revision을 만들고 4K 원본과 과거 revision을 덮어쓰지 않는다.

## 4. 웹 작업공간 계약

### Review 모드

Review는 원본 근거를 빠르게 탐색하고 검증하는 화면이다.

```text
Review Workspace
├─ Footage: 촬영 시각순 영상 목록과 검색
├─ Timeline: 영상 요약과 세로 장면 목록
│  └─ Scene Detail
│     ├─ 사건·행동·대화 종합
│     ├─ 특이 포인트와 편집 힌트
│     ├─ 세부 source 구간별 행동과 STT
│     └─ 대표 프레임과 원시 근거
└─ Source Monitor: 현재 장면의 프록시 구간 재생
```

장면을 펼치면 편집자가 “무슨 일이 있었는가”, “무슨 말이 오갔는가”, “어디서
잘라야 하는가”를 같은 깊이에서 판단할 수 있어야 한다.

### Rough Cut 모드

Rough Cut은 같은 근거 화면 옆에서 편집 상태와 대화를 다룬다.

```text
Shared Review Workspace | Revision + Program Preview | AI Editor
```

- 왼쪽 Review Workspace는 Review 모드와 동일한 DOM, 데이터와 동작을 사용한다.
- 가운데 Revision은 활성 edit, revision 번호, 전체 길이와 순서 있는 clip을
  보여 준다.
- revision clip을 선택하면 왼쪽의 대응 영상과 장면 상세가 열린다.
- Program Preview는 선택 clip의 프록시를 `source_in`부터 `source_out`까지만
  재생한다.
- 오른쪽 AI Editor는 현재 웹 맥락과 활성 edit을 바탕으로 검색, 계획, 변경과
  설명을 수행한다.

현재 Program Preview는 **개별 clip 검토기**다. revision의 모든 clip을 이어
붙인 조립 프리뷰가 만들어졌다고 표시하면 안 된다. 조립된 프록시 영상은 별도
렌더 결과와 검증 상태를 가진 뒤 같은 패널에 연결한다.

### 공유 상태

모드를 오가도 가능한 한 다음 상태를 유지한다.

- 현재 영상과 포커스 장면
- 명시적으로 선택한 장면들
- 검색어와 보이는 영상 범위
- 활성 edit와 검증된 revision reference
- 재생할 source range

브라우저 캐시는 편의 상태일 뿐 정본이 아니다. revision을 복원할 때 프로젝트
지문, 서버의 edit/revision 존재 여부와 현재 장면 ID를 다시 검증한다.

## 5. 에이전트 컨텍스트 계약

브라우저는 작은 interaction ID만 서버에 보낸다. 이 값은 신뢰하지 않으며 서버가
Timeline Catalog와 Edit Store를 기준으로 검증·확장한 뒤 에이전트에 전달한다.

| 단계 | 전달 방식 | 내용 |
|---|---|---|
| L0 Project | 항상 | 날짜 범위, 총 길이, 영상·장면 수, 날짜별 rollup, 태그, compact asset index |
| L1 Workspace | 항상 | 현재 영상, 인접 영상, 포커스 장면, 명시적 선택, 검색·재생 상태 |
| L2 Evidence | 도구 조회 | shortlist 한 장면의 세부 행동, 검토 대화, 요청된 원시 STT, 앞뒤 맥락, 프레임 참조와 bounded contact sheet |
| L3 Media | 모델 밖 | 4K·프록시 바이트, FFmpeg 실행, 최종 렌더와 업로드 |

해석 우선순위는 다음과 같다.

1. “선택한 장면들”, “이것들”은 명시적 다중 선택이다.
2. “이 장면”은 포커스 장면이다.
3. “이 영상”은 현재 영상이다.
4. “이번 여행”, “전체”는 프로젝트 개요에서 시작한다.
5. “이거 더”, “방금 것”이 포커스·선택·활성 edit 중 어디에도 연결되지 않으면
   범위를 묻고 변경하지 않는다.

## 6. 현재 에이전트 기능

정확한 도구 입력과 출력은 실행 계약을 따른다. 사용자 관점의 기능은 다음과 같다.

| 의도 | 에이전트 행동 | 대표 도구 | 웹 결과 | 상태 변경 |
|---|---|---|---|---|
| 촬영분 파악 | 영상과 사건을 시간순으로 설명 | `list_assets` | 텍스트 | 없음 |
| 장면·대화 찾기 | compact 검색 후 필요한 후보만 상세 조회 | `search_scenes`, `get_scene_evidence`, `show_scene_refs` | scene card | 없음 |
| 모호한 구간 심층 검토 | 선택 범위만 이미지 시트·자막·원시 STT로 재검토 | `inspect_scene_range` | evidence card | 캐시만 생성 |
| 원본 맥락 검토 | 장면을 펼치거나 source 구간 재생 | `focus_scene`, `play_source_range`, `open_inspector_tab` | 포커스·플레이어 | 없음 |
| 새 러프컷 계획 | 목적에 맞는 장면·순서·길이와 근거 제안 | 조회 도구, `create_edit_proposal` | 선택 가능한 proposal card | proposal 저장 |
| 러프컷 생성 | 확인된 proposal 전체 또는 선택 후보를 source-linked clip으로 저장 | `get_edit_proposal`, `apply_edit_proposal`, `show_edit_revision` | revision card·panel | 새 revision |
| 기존 컷 수정 | head 확인 후 trim·이동·추가·제거 | `get_edit`, `apply_edit_operations`, `show_edit_revision` | 새 revision | 새 revision |
| 선택 근거 설명 | 실제 revision과 장면 근거를 연결해 설명 | `get_edit`, evidence 도구 | 텍스트·scene card | 없음 |

`create_edit_proposal`은 revision을 만들지 않는 가역적 계획 상태다. Durable
action인 `apply_edit_proposal`, `create_edit`, `apply_edit_operations`만 편집
revision을 만든다. Display와 UI action은 사용자가 볼 화면을 바꾸지만 편집
데이터나 원본을 변경하지 않는다.

## 7. 표준 대화 흐름

### 7.1 조회와 설명

현재 컨텍스트로 충분하면 불필요한 도구 호출 없이 답한다. 검색이 필요하면 compact
결과로 후보를 좁히고, 실제 대사나 컷 경계가 중요할 때만 상세 evidence를 읽는다.

### 7.2 새 편집 또는 큰 구조 변경

```text
목적·범위 파악
  → 후보 검색
  → shortlist evidence 확인
  → 장면 순서·예상 source range·역할·대화·불확실성을 proposal로 저장·표시
  → 후보별 검토·선택과 “이 구성으로 진행할까요?”
  → 사용자 동의 또는 수정
  → proposal 전체 또는 선택 후보 적용
  → 새 revision 카드와 검토 위치 표시
```

계획 턴에서는 `create_edit_proposal`까지만 호출하고 `create_edit`,
`apply_edit_proposal`, `apply_edit_operations`는 호출하지 않는다. 다음 턴의 명확한
동의는 ID로 연결된 방금 proposal만 승인한다. 사용자가 후보를 해제하면 선택 후보만
적용하고, 구성 수정 의견을 주면 새 proposal에 먼저 반영한다. proposal 조회와
적용은 이를 만든 같은 채팅 세션에서만 허용하며, 명시적 부정·보류 표현은 승인으로
취급하지 않는다.

proposal 자체와 후보 범위·상태 전이는 서버가 검증하지만, “좋아”라는 문장이 실제
사용자 확인인지 판단하는 절차는 Codex backend가 실행 계약을 따르는 대화 정책이다.
서버가 별도 고위험 approval token을 발급해 강제하는 경계는 아니다.
되돌릴 수 있는 JSON revision까지만 허용되는 현재 범위에서는 이 방식을 사용하되,
렌더·외부 전송·게시처럼 비용이나 외부 효과가 생기는 기능은 서버가 검증하는 별도
승인 상태 없이는 추가하지 않는다. Deterministic demo는 프로토콜 점검용이므로 이
대화 정책의 수용 테스트로 간주하지 않는다.

### 7.3 길이가 지정되지 않은 요청

길이를 기계적으로 60초로 보정하지 않는다. 다음 순서로 판단한다.

1. 사용자 목적과 공개 매체가 명시됐는지 확인
2. 현재 선택 범위와 사건 수, 핵심 dialogue/action beat 길이 확인
3. 의미를 닫은 채 만들 수 있는 목표 길이와 범위를 제안
4. 서로 다른 결과가 될 정도로 선택지가 갈리면 사용자에게 묻기

예를 들어 “30/60/90초 중 골라주세요”보다 “주문–건배–첫 반응을 모두 살리면
약 42초가 자연스럽고, 20초 이하에서는 주문 대화를 빼야 합니다”처럼 편집 근거와
trade-off를 설명한다.

### 7.4 즉시 실행

사용자가 계획 확인을 생략하고 바로 만들라고 명시하면 같은 턴에 실행할 수 있다.
그래도 어떤 장면과 기준을 사용할지 짧게 밝히고 근거 조회와 범위 검증은 생략하지
않는다.

### 7.5 작은 후속 수정

“두 번째 clip을 2초 줄여줘”처럼 대상과 변경이 명확한 요청은 현재 head를 읽고
바로 새 revision으로 적용할 수 있다. 여러 장면을 다시 고르거나 전체 리듬을 바꾸는
요청은 다시 계획을 제시한다.

## 8. 에이전트 런타임 교체 계약

브라우저는 Codex, 특정 SDK나 CLI의 이벤트를 직접 이해하지 않는다. SSE는
**Editor Service가 소유하는 프론트엔드 계약**이다.

```text
Codex SDK / Codex CLI / App Server / 다른 에이전트
                         │
                  Backend Adapter
                         │
                  AgentDecision
                         │
              Orchestrator + ToolGateway
                         │
       status/card/action/text/suggestions/done/error
                         │ SSE
                     Edit Desk
```

새 backend는 다음 조건만 만족하면 웹을 바꾸지 않고 교체할 수 있어야 한다.

- 동일한 resolved UI context와 tool observation을 입력으로 받는다.
- 응답 텍스트, 선언된 tool call, 동적 suggestion과 완료 상태를 구조화해 반환한다.
- 모든 application action은 ToolGateway를 통과한다.
- runtime 고유 JSONL, JSON-RPC, WebSocket이나 SDK stream을 서버에서 표준 이벤트로
  변환한다.
- 브라우저에 runtime 고유 명령, shell, selector나 실행 코드를 보내지 않는다.
- 실제 backend와 deterministic demo를 화면에서 명확히 구분한다.

## 9. SSE와 웹 반응

| 이벤트 | 의미 | 웹 반응 |
|---|---|---|
| `status` | 검색·조회·revision 처리 진행 | 현재 작업 상태 표시 |
| `card` | scene 또는 revision의 구조화 참조 | 대화 안에 검토 카드 렌더 |
| `action` | 재생·포커스·탭 전환 | 닫힌 dispatcher로 UI 상태 변경 |
| `text` | 근거 설명, 계획과 결과 | AI 메시지에 누적 표시 |
| `suggestions` | 현재 맥락의 후속 작업 | 에이전트 생성 선택지 표시 |
| `done` | 정상 완료와 session/edit 참조 | 입력 복구, 완료 상태 확정 |
| `error` | 코드와 재시도 가능 여부가 있는 실패 | 실패 상태와 안전한 재시도 안내 |

스트림이 `done` 없이 끊기면 완료로 간주하지 않는다. 이미 서버가 확인한 durable
revision은 보존하되, UI는 성공을 추측하지 않고 다시 조회한다.

## 10. Revision과 동시성

- proposal은 후보 ID, 장면·원본 범위, 순서, 역할, 이유, 가정과 불확실성을 보존한다.
- 기존 edit proposal은 `replace_all` 또는 `append` 적용 의미를 보존하고,
  선택 후보 ID의 요청 순서와 무관하게 저장된 proposal 순서로 컷을 만든다.
- proposal 조회·적용은 소유 채팅 세션이 일치해야 한다.
- draft proposal만 한 번 적용할 수 있으며 일부 후보만 적용한 사실도 기록한다.
- `create_edit`는 빈 첫 revision을 만든다.
- 모든 편집 operation 묶음은 하나의 새 immutable snapshot을 만든다.
- 기존 edit을 바꾸기 전에 현재 head revision을 읽는다.
- 쓰기는 `expected_revision_id`가 현재 head와 같을 때만 성공한다.
- stale write는 기존 결과를 덮어쓰지 않고 conflict로 중단한다.
- clip은 source path, proxy path, asset·scene ID, source in/out, 선택 이유와 근거
  provenance를 보존한다.
- revision card와 클라이언트 캐시는 참조 표현이며 SQLite revision이 정본이다.

현재 지원하는 기본 변경은 clip 추가, source range trim, 순서 이동, 제거, 제목과
목표 길이 변경이다. 효과, 자막, 속도와 오디오 변경을 추가할 때도 자유 형식 명령이
아니라 versioned operation과 validator를 사용한다.

## 11. 프록시, 조립 프리뷰와 최종 렌더

```text
4K 원본 ───────────────┐
  │ 동일 시간 좌표      │ 승인된 최종 렌더
  ▼                     │
1080p 프록시            │
  ├─ 장면 분석          │
  ├─ source clip 검토   │
  └─ 조립 rough preview ┘
```

단계별 상태를 혼동하지 않는다.

1. **Revision ready**: source-linked 편집 계획이 저장됨
2. **Source preview ready**: 선택 clip을 프록시에서 개별 재생 가능
3. **Rough render ready**: 전체 revision을 조립한 프록시 영상과 QA manifest 존재
4. **Final render approved**: 사용자가 승인한 정확한 revision을 4K에 적용
5. **Published**: 별도 공개 권한과 업로드 결과가 확인됨

현재 웹이 1~2단계까지만 지원한다면 “영상 완성”이나 “내보내기 완료”라고 표현하지
않는다.

## 12. 실패와 안전 처리

| 상황 | 필수 동작 |
|---|---|
| 브라우저가 알 수 없는 asset/scene ID 전달 | 서버에서 거부하고 모델 컨텍스트에 넣지 않음 |
| “이거 더”의 대상이 없음 | clarification, durable action 없음 |
| 프록시가 없음 | 스틸 근거와 타임코드는 유지하고 재생 불가를 표시 |
| contact sheet 생성 실패 | 기존 텍스트 근거는 유지하고 영상 확인 실패를 명시 |
| STT가 불확실함 | 원문 후보와 불확실성을 보존하고 대사를 발명하지 않음 |
| source range가 장면 밖임 | operation 거부 |
| revision head가 바뀜 | conflict 후 최신 edit 재조회 |
| agent stream 중단 | 미완료 표시, durable state 재조회 |
| demo backend | 규칙 기반임을 지속적으로 노출 |
| 렌더·업로드 요청 | 별도 도구·검증·승인 경로가 없으면 실행하지 않음 |

에이전트에는 원본 삭제·이동·덮어쓰기, 임의 shell·filesystem, 임의 웹 검색이나
무승인 공개 권한을 주지 않는다.

## 13. 현재 구현과 목표 상태

이 표는 2026-09-04의 vertical slice를 기준으로 하며, 정확한 최신 상태는 실행
계약과 테스트를 함께 확인한다.

| 영역 | 상태 | 설명 |
|---|---|---|
| 공용 Review Workspace | 구현 | Review와 Rough Cut이 같은 footage·scene detail 사용 |
| 장면·대화 검색 | 구현 | compact 검색과 shortlist evidence 조회 |
| 선택 구간 심층 근거 | 구현 | 분석 프레임 재사용 또는 프록시 contact sheet, 검토 자막·요청된 원시 STT를 제한된 범위로 제공 |
| 다중 장면 컨텍스트 | 구현 | 여러 영상의 명시적 선택을 시간순 전달 |
| 구조화 proposal 후 revision 생성 | 구현 | 서버가 후보 범위를 검증·저장하고 UI에서 전체/부분 적용; 대화 확인의 의미 판단은 prompt policy |
| 작은 후속 수정 | 구현 | trim·이동·추가·제거와 새 revision |
| scene/revision 카드와 UI action | 구현 | 닫힌 SSE event와 dispatcher 사용 |
| 개별 source clip 프리뷰 | 구현 | 가운데 Program Preview에서 bounded proxy playback |
| 응답 후 동적 suggestion | 구현 | 실제 backend의 structured decision과 실행 계약의 맥락 기반 정책으로 생성 |
| 첫 화면 quick prompt | 구현 | 길이를 미리 정하지 않는 일반 기능 예시이며 agent suggestion과 구분 |
| Backend abstraction | 부분 구현 | `AgentBackend` seam과 표준 SSE는 있으나 현재 지원 런타임은 Codex SDK와 demo뿐 |
| Codex CLI·App Server adapter | 미구현 | runtime stream을 `AgentDecision`으로 바꾸는 adapter와 호환성 테스트 필요 |
| Golden 02 수준의 dialogue/action beat 선택 | 부분 구현 | scene 근거는 사용하지만 day packet·closure lint 전체는 미연결 |
| 조립된 revision 프록시 프리뷰 | 미구현 | edit plan → background render → player 연결 필요 |
| 승인된 4K source-relinked render | 미구현 | 별도 승인·렌더 job 필요 |
| NLE 동기화·YouTube 게시 | 미구현 | 별도 export·외부 권한 계약 필요 |
| 다중 사용자 인증·프로젝트 권한 | 미구현 | 로컬 단일 사용자 vertical slice 범위 밖 |

## 14. 대표 수용 시나리오

### 프로젝트 이해

현재 프로젝트에 무엇이 있는지 물으면 preloaded project context로 먼저 답하고,
이미 있는 정보를 다시 조회하기 위한 도구 호출을 하지 않는다.

### 명시적 다중 선택

두 장면을 선택해 18초 편집을 요청하면 선택 밖 장면을 사용하지 않고, 각 source
range와 역할을 설명한 proposal을 먼저 제시한다. 계획 턴에는 proposal ID만 생기고
edit ID는 생기지 않는다.

### 대화 확인 후 생성

같은 세션에서 “좋아, 그대로 만들어줘”라고 답하면 계획과 같은 장면 순서·범위로
새 revision을 만들고 카드와 Revision 패널에 표시한다.

### 심층 구간 검토

한 장면의 정확한 대사나 컷 경계를 물으면 전체 원본을 모델에 넣지 않는다. 선택
범위의 검토 자막과 앞뒤 맥락을 우선 보여주고, 요청된 경우 원시 KO/EN STT를
미검토 후보로 구분한다. 이미지가 더 필요하면 최대 12프레임 contact sheet 한 장을
생성해 웹과 모델에 같은 artifact로 전달한다.

### 동적 길이 제안

길이를 지정하지 않은 하이라이트 요청에는 고정 30/60/90초 버튼을 반복하지 않고,
현재 사건과 완결된 대화 길이를 근거로 하나의 권장안과 trade-off를 설명하거나
필요한 목적을 질문한다.

### 모호한 참조

포커스, 선택과 활성 edit이 없는 새 세션에서 “이거 더 재밌게”라고 하면 대상을
물으며 검색이나 revision 생성을 임의로 시작하지 않는다.

### Revision 검토

revision clip을 누르면 같은 asset의 대응 scene detail로 이동하고 Program
Preview가 정확한 source in/out에서 재생된다. 사용자는 행동과 대화 근거를 동시에
볼 수 있다.

### Backend 교체

CLI나 다른 에이전트 어댑터를 추가한 뒤 Codex SDK에서 해당 backend로 바꿔도
브라우저의 이벤트 타입, 카드, revision semantics와 ToolGateway 권한 경계가
바뀌지 않는다. 현재는 adapter seam의 수용 기준이며 CLI·App Server 지원 완료를
뜻하지 않는다.

## 15. 변경 절차와 완료 조건

제품 기능 변경은 다음 순서를 따른다.

1. Golden 03에서 사용자 경험과 불변 원칙의 변경 여부 확인
2. `editor-agent.v1.json`의 capability, context, policy와 tool 계약 변경
3. 서버 resolver, ToolGateway, store와 backend adapter 구현
4. 웹 카드, action dispatcher, 상태와 runtime disclosure 구현
5. 계약 테스트, deterministic demo와 실제 agent 대표 시나리오 검증
6. `/guide`가 실행 계약에서 최신 내용을 렌더하는지 확인

Agentic Edit Desk 변경은 다음을 만족해야 완료다.

- 사용자가 현재 보고 있고 선택한 범위가 에이전트 컨텍스트와 일치함
- 모델이 읽은 근거와 읽지 않은 근거가 구분됨
- 심층 근거가 선택 source range와 계약 한도 안에서만 만들어지고 캐시됨
- proposal 후보가 검증된 장면 좌표를 가지며 사용자가 전체 또는 일부를 적용할 수 있음
- 모든 durable edit이 새 revision이며 원본 좌표로 역추적 가능함
- 장면·대화 근거, revision clip과 프록시 재생이 같은 source range를 가리킴
- 동적 suggestion과 길이 제안이 현재 맥락에 근거하고 정적 기본값으로 위장하지 않음
- backend를 바꿔도 SSE와 ToolGateway application contract가 유지됨
- 미구현 렌더·게시 기능을 완료된 것처럼 표현하지 않음
- 원본, 과거 revision과 외부 공개 상태를 무승인으로 변경하지 않음

관련 구현 설명과 운영 가이드는
[Editor Service 기술 계약](../technical/editor-service.md),
[AI Editor 기능 가이드](../technical/agent-capabilities.md),
[웹 타임라인 설계](../web-timeline.md)에서 관리한다. 이 문서들은 Golden 03을
구현하는 방법을 설명하며, 제품 불변 원칙을 대체하지 않는다.
