# AI Editor 기능 및 운영 가이드

Status: implemented, 2026-09-04

Audience: 영상 소유자, 편집자, 에이전트·서버·UI 개발자

## 이 에이전트의 역할

AI Editor는 범용 챗봇이나 원본 영상 파일을 직접 다루는 자동화 봇이 아니다. 분석·검토가 끝난 여행 영상 데이터와 Edit Desk 사이에서 다음 작업을 수행하는 비파괴 편집 오퍼레이터다.

1. 촬영분의 범위와 영상별 내용을 파악한다.
2. 장면 설명, 행동, 특이 포인트와 원문 대화를 검색한다.
3. 필요한 후보만 상세 근거를 읽는다.
4. 정확한 판단이 필요하면 선택 구간만 이미지 시트·자막·원시 STT로 심층 검토한다.
5. 사용할 장면, 순서, 길이와 이유를 선택 가능한 편집 proposal로 먼저 설명한다.
6. 사용자가 동의한 전체 또는 일부 후보를 immutable revision으로 만든다.
7. 결과 장면을 카드로 보여주고 소스 모니터와 상세 타임라인을 제어한다.

실행 중인 서비스에서는 [`/guide`](http://127.0.0.1:8765/guide)에서 현재 기능과 도구 계약을 볼 수 있다.

## 단일 진실 공급원

컨텍스트 주입, 기능, 권한, 도구 입력, 판단·revision 정책과 변경 절차의 유일한 기준은 [`editor-agent.v1.json`](../contracts/editor-agent.v1.json)이다. 이 계약은 다음 소비자가 함께 사용한다.

- Codex SDK 에이전트 프롬프트
- 서버의 컨텍스트 한도와 `ToolGateway` 시작 시 검증
- `/api/contracts/agent` API
- `/guide` 운영 가이드 UI
- 계약·컨텍스트·SSE·revision 회귀 테스트

기능을 추가할 때 HTML이나 프롬프트에만 설명을 추가하지 않는다. 계약을 먼저 변경하고 서버, 에이전트, 웹, 테스트와 가이드를 같은 변경에서 갱신한다. `/api/contracts/tools`는 이전 클라이언트를 위한 호환 별칭이며 동일한 계약을 반환한다.

## 현재 할 수 있는 일

### 1. 촬영분 파악

- 촬영 시각순 영상 목록 확인
- 영상별 한 줄 요약, 전체 흐름과 분석 상태 확인
- 특정 날짜 또는 현재 라이브러리 범위 설명

예시 요청:

> 이 네 영상에서 무슨 일이 있었는지 시간순으로 요약해줘.

### 2. 장면과 대화 검색

- 장면 제목·서술·행동·태그 검색
- 웃긴 반응, 돌발 상황, 그림 되는 컷과 살릴 대사 검색
- 한국어·영어 원문 발화와 장면 맥락을 함께 검색
- 검색 후보 중 필요한 장면만 상세 근거 조회

예시 요청:

> 음식 주문하는 장면과 그 직후 웃긴 반응을 찾아서 대화까지 보여줘.

### 3. 러프컷 초안 생성

- 사용자 brief와 목표 길이에 맞는 scene 후보 조사
- 필요한 후보의 행동 구간, 대사 타임코드와 특이 포인트 확인
- 사용할 장면, 순서, 예상 source in/out, 길이와 선택 이유를 구조화 proposal로 표시
- 후보별 source 구간 재생, 선택 해제와 부분 적용
- 사용자가 계획에 동의하거나 수정 의견을 준 다음 새 edit 생성
- 검토된 scene의 source in/out으로 컷 추가
- 컷마다 선택 이유와 분석 provenance 보존
- 컷 수, 전체 길이와 revision ID를 결과 카드로 표시

예시 요청:

> 이 영상들로 음식과 사람 반응 중심의 60초 여행 하이라이트 초안을 만들어줘.

일반적인 새 편집 요청은 첫 턴에 revision을 만들지 않는다. 에이전트는 조사한 근거로 짧은 계획을 제시하고 “이 구성으로 만들어볼까요?”라고 묻는다. 사용자가 “좋아, 그대로 만들어줘”라고 답하면 다음 턴에 revision을 만든다. 사용자가 처음부터 “계획 확인은 생략하고 바로 만들어줘”라고 명시하면 계획을 짧게 알린 뒤 같은 턴에 실행할 수 있다.

계획은 채팅 텍스트에만 머물지 않는다. 서버가 scene ID와 source range를 검증한
`video-edit-proposal/v1`로 저장하고, UI는 각 후보를 체크박스로 보여준다. 사용자는
전체를 승인하거나 일부 후보만 선택해 적용할 수 있다. proposal 생성은 편집
revision을 만들지 않으며 적용된 proposal은 다시 적용할 수 없다. 현재 edit을 크게
바꾸는 제안은 후보가 최종 컷 전체인지(`replace_all`), 기존 컷 뒤에 더할
장면인지(`append`)도 명시한다. 조회와 적용은 proposal을 만든 같은 채팅 세션만
할 수 있다.

사용자가 길이를 정하지 않았으면 30·60·90초 중 하나를 고르게 하지 않는다. 목적,
매체, 선택 범위, 사건 수와 대화 완결성을 보고 자연스러운 길이 하나와 trade-off를
제안하며, 결과가 크게 갈릴 때만 추가로 묻는다. 응답 뒤 suggestion도 고정 문구가
아니라 방금 읽은 근거와 현재 edit 상태에서 생성한다. 첫 화면의 quick prompt는
개인화 추천이 아닌 일반 기능 예시다.

### 4. 장면 구간 심층 검토

- 한 reviewed scene 안에서 필요한 source in/out만 지정
- 검토 자막을 우선하고 원문 utterance와 앞뒤 대화 맥락을 분리
- 요청할 때만 KO/EN 원시 STT를 `미검토 후보` provenance로 표시
- 기존 분석 프레임을 재사용하고 부족하면 프록시에서 최대 12프레임 시트 생성
- 같은 contact sheet를 웹 카드와 Codex 이미지 입력에 연결

원시 STT는 검토 자막을 덮어쓰지 않는다. 프록시가 없거나 시트 생성에 실패해도
텍스트 근거와 원본 타임코드는 유지한다. 이미지 artifact는 입력 조건으로 해시되어
캐시되며 같은 Codex 세션에는 한 번만 첨부한다.

### 5. 기존 편집 수정

- 장면 추가
- clip source in/out trim
- clip 순서 이동
- clip 제거
- 제목과 목표 길이 변경

한 번의 trim·이동·제거처럼 범위가 명확한 소규모 변경은 현재 head revision ID를 확인한 다음 바로 적용할 수 있다. 여러 장면을 다시 고르거나 전체 흐름을 바꾸는 큰 변경은 현재 revision을 읽고 변경 계획을 먼저 설명한다. 동시에 다른 변경이 적용돼 head가 달라졌으면 덮어쓰지 않고 `revision_conflict`로 중단한다.

예시 요청:

> 주문 설명은 3초 줄이고 동행자가 웃는 장면을 더 길게 한 다음 전체를 45초로 맞춰줘.

### 6. Edit Desk 검토 지원

- 선택한 scene을 시간순 타임라인에서 펼치기
- 해당 영상과 source range를 공용 소스 모니터에서 재생
- Monitor와 AI Editor 탭 전환
- scene reference와 edit revision 카드 표시

브라우저는 에이전트가 생성한 JavaScript나 selector를 실행하지 않는다. 서버와 UI가 함께 아는 닫힌 action 타입만 처리한다.

### 7. 선택 근거 설명

- 장면의 행동과 사건 흐름
- 원문 발화와 대화 요약
- 특이 포인트와 편집 힌트
- 현재 revision에서의 clip 순서와 길이

에이전트는 위 근거를 조합해 선택 이유를 설명할 수 있다. 분석 데이터에 없는 사건이나 실제로 확인하지 않은 화면은 추측하지 않는다.

## 요청 처리 순서

```text
사용자 요청과 현재 UI 맥락
  → compact asset/scene 검색
  → 모호한 소수 후보만 상세 evidence 또는 bounded contact sheet 조회
  → 사용자 목적에 맞는 장면 선택
  → 검증된 proposal로 장면 순서·예상 길이·선택 이유 표시
  → 사용자 동의, 후보 부분 선택 또는 수정 의견
  → 확인된 proposal을 edit operation으로 적용
  → 새 immutable revision 저장
  → 카드·설명·UI action을 SSE로 전달
```

전체 4K 영상을 Codex에 업로드하거나 모든 원시 STT를 프롬프트에 넣지 않는다. 기본 검색 결과에는 scene ID, source range, 짧은 설명, 특이 포인트, 짧은 대화 발췌만 포함된다. 상세 행동·원문 발화는 shortlist에만 요청하고, 실제 이미지 입력은 선택한 한 구간의 contact sheet 한 장으로 제한한다.

## 권한 경계

| 구분 | 허용 범위 |
|---|---|
| 읽기 | 분석된 영상·장면·대화, 선택 구간 시각 근거, 현재 및 과거 edit revision |
| 계획 상태 | 검증된 proposal 생성·조회, 후보 전체 또는 일부 선택 |
| 초안 변경 | 확인된 proposal 적용, 새 edit, 컷 추가·trim·이동·제거, 제목과 목표 길이 |
| UI 제어 | scene 포커스, source range 재생, inspector tab 전환 |
| 금지 | T7 원본 삭제·이동·덮어쓰기, 임의 shell·filesystem·FFmpeg, 임의 웹 검색 |
| 별도 단계 | 최종 4K 렌더, YouTube 업로드, NLE 동기화, 공개·외부 전송 |

Codex SDK는 `Sandbox.read_only`와 `ApprovalMode.deny_all`로 시작한다. 이는 추가 방어선이며, 실제 편집 권한은 계약과 입력 검증을 통과한 `ToolGateway`에만 있다.

## 도구 분류

### Data Query

- `list_assets`
- `search_scenes`
- `get_scene_evidence`
- `inspect_scene_range`
- `get_edit_proposal`
- `get_edit`

데이터를 읽어 에이전트 판단으로 돌려보낸다. `inspect_scene_range`만 계약 한도 안에서
검토 자막, 요청된 원시 STT와 contact sheet를 반환한다. 원시 영상 바이트와 전체
timeline JSON은 반환하지 않는다.

### Proposal Action

- `create_edit_proposal`

검증된 후보 계획을 저장하지만 edit revision은 만들지 않는다. 사용자가 후보별로
검토하고 선택할 수 있는 proposal card를 함께 보낸다.

### Durable Action

- `create_edit`
- `apply_edit_proposal`
- `apply_edit_operations`

서비스 상태를 변경하지만 원본 영상은 변경하지 않는다. 모든 성공한 작업은 새 revision을 만든다.

### Display

- `show_scene_refs`
- `show_edit_revision`

선택된 결과만 사용자가 검토하기 좋은 카드 이벤트로 보낸다.

### UI Action

- `play_source_range`
- `focus_scene`
- `open_inspector_tab`

브라우저의 현재 검토 맥락만 변경한다. 프로젝트 데이터나 원본 파일은 변경하지 않는다.

정확한 컨텍스트, 정책, 파라미터와 출력은 계약 JSON 또는 실행 중인 `/api/contracts/agent`를 기준으로 한다.

## 사용자에게 전달되는 이벤트

| SSE event | 의미 |
|---|---|
| `status` | 검색·근거 조회·revision 적용 진행 상황 |
| `card` | scene 후보, 심층 evidence, edit proposal 또는 revision 참조 |
| `action` | 재생·포커스·inspector 전환 |
| `text` | 에이전트 설명과 결과 요약 |
| `suggestions` | 현재 결과에 맞는 후속 요청 |
| `done` | 정상 종료와 session/edit 참조 |
| `error` | 오류 코드와 재시도 가능 여부 |

정상 스트림은 반드시 `done`으로 끝난다. `done` 없이 연결이 종료되면 UI는 완료로 간주하지 않는다.

## 아직 지원하지 않는 것

- 분석되지 않은 대용량 촬영분을 대화 도중 자동으로 전처리하는 background job
- 완성본 4K source-relinked render 승인 화면
- Final Cut Pro, Premiere Pro, DaVinci Resolve timeline 동기화
- YouTube 업로드, 제목·설명·썸네일 게시
- 다중 사용자 권한과 프로젝트별 인증

이 기능들은 에이전트에게 범용 shell 권한을 주는 방식으로 추가하지 않는다. 각각 입력·출력·승인·진행 상태가 명확한 서비스 도구와 background job으로 구현한다.

## 관련 문서

- [Editor Service 실행 가능한 기술 계약](editor-service.md)
- [Editor Agent 단일 계약](../contracts/editor-agent.v1.json)
- [SSE 이벤트 계약](../contracts/agent-events.v1.schema.json)
- [채팅 기반 AI 영상 편집 플랫폼 설계](../agentic-editor-platform.md)
- [소스 미디어와 작업 미디어 위치](../media-locations.md)
