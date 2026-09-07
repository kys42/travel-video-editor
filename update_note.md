## 2026-09-07 - Golden 대조와 도입 검증

- Golden 01/02의 요구사항별 확인 범위를 파일럿 문서에 정리했다. 근거·시간 연결, 익명 인물 관찰, 재사용 가능한 후보 데이터는 개선됐으며 최종 영상 재미/인식 정확도는 별도 평가가 필요하다.
- Golden 02에서 요구한 앞뒤 자막/발화 제공이 부족해 `dialogue_context`를 추가했다. 후보와 겹치는 대사, 다른 그룹에 속한 이전/다음 2개를 보존하며 기본 사용 범위는 바꾸지 않는다.
- 전체 테스트 127개, Ruff와 추가 독립 코드 리뷰 통과. 기존 397개 전체 후보에도 문맥 정보가 제공된다.
- 사용자가 개선 효과를 확인하면 push/PR 진행하도록 승인했다. 기존 원격 업로드 보류 조건은 이 승인으로 해소됐으며, main 전면 적용 대신 선택적 모드의 PR로 도입한다.

---

## 2026-09-07 - 편집용 추출 근거와 클립 후보 연결

- `--clip-evidence`로 기존 통합 LLM 리뷰에 사건·핵심 구간·화면 관찰/발화 언급·익명 인물·화자 연결·원음 추정·촬영 상태 계약을 추가했다. 원시 근거와 모델 해석을 분리하고 shard/merge에서 유지한다.
- Apple Vision의 얼굴/사람 검출 위치·실제 시각·신뢰도를 보존했다. 화면 밖 박스의 가시 교집합, 빈 결과 오류 처리를 추가했다. 검출은 신원 또는 얼굴 부재 확인이 아니다.
- 원본 구간·후보 ID·버전·근거·검토 상태를 내보내는 CLI와 Golden finisher 자동 후보 출력/재개를 연결했다. 옵션 변경 때 packet만 다시 만드는 회귀 검사를 추가했다.
- 실제 58초 프록시의 2개 완전한 그룹에서 발화/자막 9개와 후보 8개를 생성·검증·병합했다. 열린 대화/불명확 자막이 있는 3개는 검토 필요 상태다. 익명 인물 관찰과 신원 불확실성을 혼동하지 않도록 계약을 보완했다.
- 기존 397개 → 2,200개 후보 변환 오류 0, 추가 모델 호출 0. 전체 테스트 125개, Ruff, Swift 컴파일과 실제 Vision 실행 통과. 독립 리뷰 지적 1건 수정 후 재검토 통과.
- [계약](docs/clip-editorial-evidence.md), [실험과 한계](docs/benchmarks/clip-evidence-pilot.md)를 정본으로 추가했다. 뷰/AI 편집 연결, 실제 환경음 청취, 얼굴 제외 최종 검수는 후속이다.
- 이슈 #20, 브랜치 `codex/clip-evidence-library`. 원격 push/PR 생성은 자동 승인 검토에서 코드 외부 공유의 명시적 승인 부족으로 거부됐다. 로컬 커밋과 검증 결과를 보존하며 업로드 승인 전 재시도하지 않는다. 기존 main/4K 작업과 원본은 보존했다.

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

## 2026-09-07 — 편집 웹에 후보 구간 연결

- PR #21의 후보 생성 함수를 공용 Review/Rough Cut 장면 라이브러리에 연결했다.
  큰 장면 아래에 후보 제목·요약·대사·대표 프레임·검토 이유·인물 미확인 상태를 표시한다.
- 후보 구간과 앞뒤 대화 맥락 구간을 원본 시간으로 재생한다. Review의 명시적
  source_in/out 처리와 details toggle에 의한 재생 범위 초기화 문제를 보완했다.
- 실제 58초 파일럿의 2개 장면/8개 후보를 로컬 편집 서버에서 렌더하고
  Review 및 Rough Cut의 후보 재생을 브라우저로 확인했다. 별도 LLM 호출 없음.
- 구형 타임라인 fingerprint 누락 호환성은 독립 코드 리뷰 지적을 반영했다.
- 후보를 revision에 직접 넣는 UI/API는 아직 후속 범위다. PR 미병합;
  기존 운영 HTML 자동 갱신 없음. 열린 PR 때문에 worktree를 보존한다.

- 최종 검증: 전체 128개 테스트, 변경 Python 린트, JS 구문 검사 통과. 독립 리뷰 수정 확인 완료.

## 2026-09-07 — 의미 단위 후보를 장면 상세의 기본으로 표시

- 사용자가 새 결과에서도 5초 구간과 반복 설명을 본 원인은 기존 기계 샘플 목록의 노출이었다.
- 후보를 `장면별 세부 구간`으로 표시하고 원시 분석·기계 구간·스토리보드는 접힌 분석 근거로 옮겼다. 구형 데이터는 기존 표시를 유지한다.
- 하루 비교 HTML을 재생성했다. 21개 자산 검증 통과, 데이터 재분석이나 추가 LLM 호출 없음.
- 브라우저에서 후보 15.010–31.730초가 모니터에 그대로 적용되는 것을 확인했다. 전체 128개 테스트와 독립 코드 리뷰 통과.
- 후보 자체의 거친 분할은 남아 있으며 이번 변경은 표시 혼동을 고친 것이다.

## 2026-09-07 — 장면 상세 정보의 과도한 접기 수정

- 장면 해석·전체 보정 대화·편집 포인트를 기계 샘플 접기 밖으로 복원했다.
- 하루 비교의 실제 회귀: 단일 후보 그룹 28→46/66, narrative_summary 평균 100.8→47.2자. UI 복원은 후보 분할과 설명 자체의 품질 회귀를 해결하지 않는다.
- 상세 해석이 접힌 근거 밖에 있는지 회귀 검증을 추가했다.

## 2026-09-07 — 재사용 라이브러리 우선 추출

- 행동·대상·대화 주제·반응 변화에 따른 세분화와 구체적 설명을 통합 리뷰 지침에 추가했다. 고정 간격이나 최소 후보 개수를 강제하지 않는다.
- 후보 JSON의 library_audit는 긴 단일 후보, 반복, 짧은 설명을 검토 신호로 남긴다. 품질 합격 판정이나 자동 분할이 아니다. 배치 캐시 digest에 검사 모듈을 포함했다.
- 웹에서 행동·대상·상호작용·원음·품질·익명 인물을 표시하고 설명과 역할 검색을 연결했다. 관찰/발화/추정 및 시간 근거를 유지한다.
- 독립 리뷰의 역할 검색 누락을 수정했다. malformed evidence는 기존 export validator에서 거부하며 UI에서 조용히 숨기지 않는다.
- Golden 계약과 travel-video-pipeline 참고문서를 갱신했다. 스킬 검증과 전체 132개 테스트 통과. 실제 의미 품질은 별도 파일럿에서 검토한다.
- 기존 PR #21 유지. Notion 보드 갱신 대기.

- 첫 세분화 파일럿의 템플릿 설명을 검수에서 보류하고 구체 내용 재검토를 요청했다. 근접 중복 설명·근거 반복 검토 신호와 회귀 테스트를 추가했다.

- 2자산(총 약4분30초) 세분화 파일럿: 직전6/3→25/10후보. 최종 부모검수에서 근거없는지명·템플릿문구 제거, raw/정본/후보 구조 일치 검증. 모든 경계·원음의 전수검수는 아니며 하루 전체 자동대체 보류. 결과 root work/scene-library-rich-pilot, 웹8794.

## 2026-09-08 — 세로 나열을 클립 탐색 작업대로 개편

- 왼쪽 썸네일 목록과 길이 비율 타임라인으로 후보 선택, 오른쪽에는 선택한 후보 한 개만 표시한다. 모바일은 가로 썸네일이다.
- 미평가 상태 반복 제거, 관찰 근거와 전체 장면 설명을 접기로 이동했다. 샘플 품질 주의만 요약 표시한다.
- 후보 선택은 자동 재생 없이 정확한 원본 범위로 이동한다. 기존 재생/맥락 버튼 유지, 모니터 시간 라벨도 선택 범위로 맞췄다.
- 10개 후보 장면 브라우저 검증: 51.950–59.990초 선택/모니터 동기화, 한 패널 표시와 화면 레이아웃 확인.

- 전체132개 테스트·JS 구문 검사·변경 Python 린트 통과. 독립 UI 리뷰의 정보 발견성 의견은 검토했으며, 이번 사용자 요청에 따라 명시적 상세 접기를 유지했다.
