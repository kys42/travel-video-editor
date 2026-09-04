## 2026-09-04 19:00 KST - 에이전트 편집 제안과 심층 장면 증거

### 작업 내용

- 앱 서버 범위는 보류하고 기존 로컬 Editor Service와 Codex SDK adapter 안에서
  에이전트가 조사·계획·승인·revision 생성까지 수행하는 기능을 구현했다.
- 새 편집이나 큰 구조 변경은 바로 revision을 만들지 않고, 실제 장면 범위와
  선택 이유가 담긴 `video-edit-proposal/v1`을 먼저 보여주게 했다.
- 필요한 장면만 기존 분석 프레임 또는 프록시 contact sheet, 검토 자막, 원문
  utterance와 선택적 KO/EN raw STT로 더 깊게 읽는 도구를 추가했다.
- 후보 체크 해제와 부분 적용, 기존 edit의 전체 교체/뒤에 추가, 정확한 source 구간
  재생을 Edit Desk의 proposal card와 revision panel에 연결했다.

### 주요 변경사항

- `src/travel_video/editor/evidence.py`: 한 reviewed scene 안에서 최대 180초·12프레임,
  raw STT 80개로 제한된 심층 evidence와 원자적 contact-sheet cache를 구현했다.
- `src/travel_video/editor/store.py`: session-owned proposal, 부분 적용, 저장 순서 보존,
  `replace_all`/`append`, optimistic revision과 단일 SQLite transaction을 구현했다.
- `src/travel_video/editor/contracts.py`, `docs/contracts/editor-agent.v1.json`: strict tool
  input 검증과 계약 revision 6의 proposal/evidence 도구·정책을 정본화했다.
- `src/travel_video/editor/agent.py`: Codex `LocalImageInput` 전달, artifact 중복 억제,
  proposal-first prompt와 fail-closed demo 승인을 추가했다.
- `src/travel_video/editor/server.py`: proposal CRUD/apply, contact sheet endpoint,
  finite-number REST 모델과 session ownership 검증을 추가했다.
- `src/travel_video/templates/library.html`: 선택 가능한 proposal, 심층 자막·이미지 근거,
  선택 구간 source monitor 재생과 적용 결과 revision 갱신을 추가했다.
- Golden 03, 기술 문서, agent guide와 실제 에이전트 QA 스크립트를 계약 revision 6에
  맞췄다.

### 중요 결정사항

- 모델에 전체 영상이나 임의 FFmpeg 권한을 주지 않는다. 서버가 검증한 한 scene의
  제한된 구간만 고정 FFmpeg recipe로 contact sheet로 만들고 이미지 한 장만 보낸다.
- 검토 자막은 권위 있는 표시 대화이고 raw STT는 `미검토 후보` provenance를 유지한
  보조 근거다. 각 단계의 데이터는 덮어쓰지 않고 재사용 가능하게 남긴다.
- 30·60·90초 같은 고정 선택지 대신 에이전트가 실제 후보 장면과 대화 완결성에서
  목표 길이와 순서를 제안한다.
- proposal의 후보 ID는 subset membership일 뿐 순서 명령이 아니다. 최종 컷은 저장된
  proposal 순서를 보존하며, 기존 edit 전체 교체는 old clip별 remove로 확장하지 않고
  snapshot의 clip sequence를 원자적으로 바꾼다.
- proposal 조회·적용은 이를 만든 같은 chat session에 한정한다. 카드 버튼 적용도
  session ID를 서버에 보내며 교차 세션은 거부한다.

### 문제 해결

- 문제: 직접 REST 입력의 `NaN`이 범위 비교를 우회해 유효한 revision으로 저장될 수
  있었다.
- 해결: Pydantic candidate 모델과 store 양쪽에서 finite number를 검사하고 revision·
  proposal JSON을 `allow_nan=False`로 저장하며 validation error도 strict JSON으로
  반환한다.
- 문제: demo 승인 키워드가 “적용하지 마/적용은 말고”를 승인으로 오인했다.
- 해결: 데모는 정규화한 전체 문장이 명시적 승인 패턴과 정확히 맞을 때만 허용해
  부정·보류·선행 수정 표현을 fail closed한다.
- 문제: 기존 edit proposal이 후보를 뒤에 추가만 했고, 100개 기존 clip 전체 교체는
  operation 제한을 넘었다.
- 해결: `replace_all`과 `append` 의미를 계약에 넣고, replace는 후보 sequence로 직접
  교체해 기존 clip 수와 무관하게 한 revision으로 처리한다.
- 독립 리뷰에서 비유한 좌표, 교차 세션 적용, 부분 선택 순서 반전, legacy proposal
  기본값 불일치, 이미지 중복 첨부와 캐시 metadata 누락을 찾아 모두 회귀 테스트로
  고정했다.

### 테스트 및 검증

- 전체 `pytest`: 98 passed (기존 Starlette/httpx deprecation warning 2개)
- Ruff, `git diff --check`, agent contract JSON, 인라인 JavaScript 문법: 통과
- 실제 외장 SSD 프록시의 8초 범위에서 6프레임 contact sheet 생성·cache hit 확인
- 실제 API에서 deep evidence → proposal → 후보 부분 적용 → revision 생성 확인
- 1280×720 데스크톱 브라우저에서 proposal 선택, 정확 구간 재생, 심층 evidence,
  3패널 레이아웃과 콘솔 오류 없음 확인

### 다음 단계

- [ ] Golden 03 PR #14 병합 후 기능 PR #17의 base를 `main`으로 변경
- [ ] 실제 Codex backend로 대표 시나리오 QA 스크립트 실행 및 응답 품질 평가
- [ ] revision을 Video Editor 렌더 plan으로 넘기는 렌더 job·progress·artifact 계약 설계
- [ ] 장기 실행 FFmpeg를 별도 media worker로 격리하는 운영 단계 구현

### 관련 작업

- GitHub Issue: #16
- GitHub PR: #17 (stacked on #14)

---

## 2026-09-04 13:45 KST - 여행 영상 분석·편집 파이프라인 정식화

### 작업 내용

- 10일치 액션캠 아카이브를 원본 순차 시청 없이 처리할 수 있도록 프록시,
  장면 프레임, Apple 한·영 STT, 의미 그룹과 HTML 검토 화면을 연결했다.
- 빠른 의미 그룹 뒤에 조밀한 storyboard와 Apple 원시 후보를 함께 검토하는
  통합 scene-dialogue 단계를 추가했다.
- 실제 발화, 읽기 좋은 원언어 자막, 모든 STT window 판정과 편집용
  `editorial_beats`를 한 모델 패스의 분리된 결과로 보존했다.
- 과도하게 보수적이던 자막 정책을 `dialogue-preservation/v1`로 바꾸고,
  복원 가능한 저신뢰 발화를 최대한 살리도록 정식 기본값으로 승격했다.
- 하이라이트가 대화 중간에서 끊기지 않도록 개별 짧은 클립 대신 완결된
  대화 beat와 경계 조정을 편집 근거로 사용하게 했다.
- Video Editor에 검수 자막 부착, SRT·번인 일치, 속도·오디오 fade/mute,
  contain/cover 리프레임, 회전과 A/V 전환 기능을 추가했다.

### 주요 변경사항

- `src/travel_video/scene_dialogue.py`: fresh Apple evidence 기반 통합 packet,
  review validator, group shard 병합, timeline attachment와 보존 감사를 추가했다.
- `src/travel_video/dialogue_script.py`: 과거 reconciliation을 읽기 좋은 대사
  블록으로 변환하는 호환 단계를 추가했다.
- `src/travel_video/transcript_reconcile.py`: span-rich 후보와 source identity,
  coverage 검증을 강화했다.
- `src/travel_video/library.py`, `src/travel_video/web.py`: 검수 자막 대본을
  기존 STT 후보보다 우선 표시하고 대사 없는 검수 결과도 구분한다.
- `scripts/build_scene_dialogue_batch.py`, `scripts/build_dialogue_script_batch.py`:
  날짜/자산 단위 재개 가능한 packet·대본 batch를 추가했다.
- `skills/video-editor/scripts/attach_timeline_captions.py`: 검수 자막을 편집
  출력 시간축으로 매핑하고 `dialogue-preservation/v1` 감사 실패 시 중단한다.
- `skills/video-editor/scripts/render_edit.py`: 확장 속도, 피치 유지, 오디오
  제어, 리프레임, 회전, 전환과 렌더 manifest 검증을 추가했다.
- `docs/golden/01-raw-to-editorial-evidence.md`: 원본에서 편집 근거까지의
  정본 계약과 보존 우선 완료 게이트를 기록했다.
- `docs/golden/02-editorial-evidence-to-highlight.md`: 하루 story 구성과 완결된
  대화 단위 선택, boundary lint, 자막·렌더 QA 계약을 기록했다.

### 중요 결정사항

- 원본 영상과 4K 파일은 읽기 전용이며 모든 판단은 원본 상대 경로와
  `source_in/source_out`으로 역추적한다.
- 시각 Phase 1과 Apple STT는 독립 캐시로 보존하고, 모델은 연락판과 조밀한
  storyboard, 시간 정렬된 STT evidence만 본다.
- `uncertain window`는 삭제 지시가 아니다. 쓸 수 있는 실제 말은
  ko/en/mixed 발화와 자막으로 남기고, 의미 없는 잔여 파편만 제외한다.
- 신규 편집 입력은 `timeline.dialogue-reviewed.summarized.json`을 우선하며
  과거 `timeline.final.json`은 호환·이력용으로 유지한다.
- 자막 소비자는 `reviewed_dialogue.captions`를 권위 있는 입력으로 사용하고,
  정식 정책 산출물은 `policy_audit.status=pass`와 100% lexical caption coverage를
  만족해야 한다.

### 문제 해결

- 문제: 낮은 STT confidence와 한·영 locale 충돌 때문에 실제 대화가 검수에서
  과도하게 사라졌다.
- 해결: 실제 발화 선택과 표시용 자막을 분리하되 같은 group review에서 함께
  만들고, source evidence ID와 uncertain 상태를 유지하면서 문맥상 복원 가능한
  문장을 살렸다.
- 문제: 좋은 화면을 10~14초 고정 길이로 먼저 선택한 뒤 자막을 붙여 대화가
  setup이나 접속사에서 끊겼다.
- 해결: 그룹 내부 `editorial_beats`, `dialogue_closure`, 경계 확장/병합 결정을
  먼저 만들고 길이 최적화는 완결된 beat 단위로 수행하게 했다.
- 독립 PR 리뷰에서 긴 STT span의 window 분할 손실, partial caption 허용,
  clip↔timeline 오연결, 다중 발화 caption의 빈 구간 허용을 발견했다.
- 해결: 모든 겹치는 window에 span 조각을 보존하고 시간 합집합을 검증하며,
  정식 reviewed caption은 100% 포함과 source identity를 강제하고, caption이
  인용한 각 utterance와 실제로 겹치도록 validator를 강화했다.
- 기존 key moment 덮어쓰기, storyboard frame 좌표 변조, 대본 batch의 stale
  cache와 경로 탈출도 각각 보존·exact match·SHA 상태·safe ID로 차단했다.

### 테스트 및 검증

- 수정 범위 회귀 테스트: 42 passed
- 전체 `pytest`: 72 passed (기존 dependency deprecation warning 2개)
- Ruff lint와 변경 파일 format: 통과
- 저장소 전체 format check: 기존 미포맷 파일 21개를 보고했으며 이번 변경에서
  무관한 파일을 일괄 재포맷하지 않음
- Ruff: 통과
- `git diff --check`: 통과
- travel-video-pipeline/video-editor skill validator: 통과
- 0039 한영 혼합 대화: lexical 발화 22/22 자막 연결, policy audit 통과
- 0054 음식 후기·커플 농담: lexical 발화 28/28 자막 연결, policy audit 통과
- 원본·프록시·Apple raw STT 변경 없음

### 다음 단계

- [ ] 기존 전체 아카이브를 `dialogue-preservation/v1`로 재검토·backfill
- [ ] 새 정본으로 날짜별 Day Editor Packet과 하이라이트를 재생성
- [ ] 공개용 렌더 전 고유명사·숫자·문맥 교정 단어를 원음 spot-check
- [ ] 일반화된 화자 분리와 대화 검색 인덱스 개선

### 관련 작업

- GitHub Issue: #8

---
