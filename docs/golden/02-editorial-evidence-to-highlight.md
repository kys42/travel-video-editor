# Golden 02 — 편집 근거에서 하이라이트까지

상태: **규범 문서**

입력 정본: [Golden 01 — 원본 영상에서 편집 근거까지](01-raw-to-editorial-evidence.md)

범위: story day 구성, 후보 생성, 선택, 편집 계획, 자막, 프록시 검토본과 QA

이 문서는 “좋은 화면을 고르고 짧게 이어 붙이는” 작업을 “영상·행동·대화의
완결된 비트를 골라 하루의 이야기로 구성하는” 작업으로 정의한다.

## 1. 이번 실패에서 확인된 현재 상태

기존 설계 원칙은 화면·행동·STT를 함께 판단하도록 되어 있었다. 그러나 실제
날짜별 하이라이트 prototype인
`work/highlights/local-days/build_local_day_highlights.py`는 다음 방식이었다.

1. `EDITS` 배열에 source range를 대부분 10~14초로 수동 고정
2. `group_id`, 시각 설명과 선택 이유로 순서 구성
3. 그 범위가 정해진 뒤에 자막을 attach
4. 렌더 직전에 개별 자막 overlap만 검사

따라서 전사 데이터는 존재했지만 **클립을 고르는 입력 계약**에는 강제되지
않았다. `selection_basis`에 transcript를 썼다고 기록돼 있어도, 실제로는
인접 발화, 대화의 시작과 결말, 질문에 대한 응답을 검증하는 단계가 없었다.

Glacier Bay 핫텁 예시는 이 결함을 그대로 보여 준다.

- 선택된 원본 범위: `330–344초`
- 표시된 발화: `328.65–338.67초`
- 끝 문장: `“'South Korea?' 하고 나서…”`
- 같은 장면의 후속 발화: `348.51초`, `354.79초`, `362.33초`, `367.82초`
- 결과: 시각적으로는 좋은 14초지만 대화는 setup에서 끊김

이 문제는 자막 개수나 STT confidence가 아니라 **선택 단위와 경계 검증의
문제**다.

## 2. 목표

하이라이트 선택기는 다음을 동시에 만족해야 한다.

- 하루의 주요 장소와 사건을 시간순으로 이해할 수 있음
- 시각적으로 강한 장면, 재미있는 행동과 감정 반응을 살림
- 대화 장면은 의미가 닫히는 단위로 보존
- 목표 길이는 지키되 선택한 대화를 중간에서 자르지 않음
- 불확실한 음성을 그럴듯한 자막으로 발명하지 않음
- 모든 컷과 자막을 원본 ID와 시간으로 역추적 가능
- 모델은 전체 원본이 아니라 compact joint evidence packet만 읽음

## 3. 전체 흐름

```text
[story-day manifest]
          +
[영상별 editorial evidence JSON]
          │
          ▼
[Day Editor Packet]
사건 순서 + 시각 후보 + 행동 + 대화 + 앞뒤 맥락 + 품질
          │
          ▼
[하루 story outline]
도입 → 이동/발견 → 핵심 사건 → 반응/결말
          │
          ▼
[Stage 1 editorial beat 수집·보정]
visual shot / dialogue beat / action beat / transition
          │
          ▼
[후보 평가와 선택]
가치 점수 + 중복 제거 + 목적별 제약
          │
          ▼
[경계 확정]
문장·행동·반응이 닫히는 source range
          │
          ▼
[전체 길이 최적화]
문장을 자르지 않고 낮은 가치 비트 자체를 제거
          │
          ▼
[구조화 edit decision → video-edit-plan/v1]
          │
          ▼
[boundary lint + caption attach + dry-run]
          │
          ▼
[프록시 렌더 → 시각/음성/자막 QA]
          │
          ▼
[승인 후 같은 좌표로 원본 렌더]
```

## 4. Day Editor Packet

좋은 화면을 고르는 모델은 연락판만 받지 않는다. 하루치 asset을 시간순으로
정렬한 뒤, 각 후보에 아래 정보를 **한 레코드로 결합**해서 받는다.
기본 후보는 Golden 01의 그룹별 통합 리뷰가 만든 `editorial_beats`이며,
2단계는 하루 전체 맥락에서 이를 병합·제외하거나 경계를 보수적으로 다듬는다.

```json
{
  "candidate_id": "DAY27-CAND-0042",
  "asset_id": "…",
  "source_relative_path": "0827/…MP4",
  "timeline_quick_fingerprint": "…",
  "group_id": "G002",
  "segment_ids": ["S…"],
  "candidate_range": {"start": 328.3, "end": 376.5},
  "visual": {
    "summary": "두 사람이 온수풀에서 카메라를 향해 이야기한다.",
    "actions": ["휴대전화를 보여 줌", "서로 반응하며 웃음"],
    "representative_frames": ["F…"],
    "notable_moments": []
  },
  "dialogue": {
    "beat_id": "DB0042",
    "captions": ["…"],
    "utterance_ids": ["SRU…"],
    "previous_captions": ["…"],
    "next_captions": ["…"],
    "closure": "closed",
    "uncertain_windows": []
  },
  "boundary": {
    "source_proposal_ids": ["BP0041", "BP0044"],
    "start_reason": "대화 setup 시작",
    "end_reason": "응답과 웃음 반응 종료"
  },
  "quality": {
    "sharpness": 0.0,
    "motion": 0.0,
    "occlusion": false,
    "talking_face": true
  },
  "roles": ["dialogue", "candid", "payoff"],
  "risks": []
}
```

필수 원칙:

- 후보 내부 자막만 주지 않고 바로 앞뒤 자막과 발화를 함께 준다.
- 시각 후보 범위와 대화 beat 범위를 둘 다 제공한다.
- 말하는 얼굴이 보이는데 caption-ready 문장이 없다면
  `talking_face_without_caption` 위험을 기록한다.
- 모든 문장과 프레임은 source ID를 가져야 한다.
- Golden 01의 proposal 시각을 유지했다면 boundary proposal ID도 전달한다.
- 모델은 packet에 없는 타임코드를 새로 만들 수 없다.

## 5. 편집 단위

클립 길이를 먼저 정하지 않는다. 먼저 무엇을 보존할지에 따라 단위를 정한다.
Stage 1 beat는 후보이지 최종 컷이 아니므로, 하루 전체 서사와 인접 그룹을
본 coordinator가 같은 사건의 beat를 합치거나 낮은 가치 beat를 제외할 수
있다. 다만 근거에 없는 새 시간이나 대사를 만들 수는 없다.

### 5.1 Visual shot

풍경, 간판, 음식 공개처럼 화면 자체가 목적이다.

- 변화가 적으면 3~8초, 카메라 이동이나 reveal이 있으면 행동이 끝날 때까지
- 의미 있는 발화가 겹치면 dialogue beat로 승격하거나, 발화가 없는 subrange를
  고르거나, 현장음으로만 쓸 이유를 명시
- 같은 구도·같은 피사체의 반복은 대표 한두 개만 유지

### 5.2 Dialogue beat

대화의 setup, 핵심 문장, 상대 반응 또는 결말을 묶은 단위다. scene group과
동일하지 않으며, 한 scene 안에 여러 dialogue beat가 있을 수 있다.

형성 규칙:

- 같은 주제·화자 흐름·시각 상황의 연속 발화를 묶음
- 짧은 무음만으로 자동 분리하지 않음
- 긴 무음, 명확한 주제 전환, 장소 전환, 행동 전환을 후보 경계로 사용
- gap 기반 기계 초안 뒤 모델이 문맥으로 `open/closed`를 판정

경계 규칙:

- 첫 발화 0.3~1.0초 전부터 시작해 표정과 호흡을 보존
- 마지막 응답·웃음·행동 반응 뒤 0.5~1.5초까지 유지
- 발화 span 내부에서 자르지 않음
- 마지막 문장이 쉼표, 연결 표현, 말줄임표, 미완성 인용이면 끝낼 수 없음
- 질문을 살렸으면 답 또는 의도적인 무응답 반응까지 포함
- 앞 문맥 없이는 이해되지 않는 대명사·응답으로 시작하지 않음

긴 대화를 줄일 때:

- 목표 길이를 맞추려고 문장 중간을 자르지 않음
- 낮은 정보의 침묵, 반복 설명, 같은 의미의 재진술을 자연스러운 pause에서 제거
- 같은 원본을 여러 source range로 나눠도 `beat_id`는 하나로 유지
- 축약으로 의미가 달라지면 전체 beat를 빼거나 별도 요약 overlay로 처리하고,
  실제 발화 자막인 것처럼 만들지 않음

### 5.3 Action beat

준비 → 행동 → 결과/반응이 있는 활동 단위다.

- 예: 음료 선택 → 주문 → 수령, 포즈 준비 → 촬영 → 웃음
- 행동의 핵심 동작만 남기더라도 결과나 반응을 잘라내지 않음
- 대화가 행동 이해에 필수이면 dialogue beat 규칙을 함께 적용

### 5.4 Transition / ambience

시간과 장소를 넘기는 짧은 호흡이다.

- 대화 내용을 전달하는 역할을 맡기지 않음
- 사람 입 모양과 음성이 뚜렷한데 자막이 없으면 단순 B-roll로 간주하지 않음
- 필요하면 해당 구간의 원음을 낮추거나 speech-free subrange를 선택하되,
  이런 오디오 결정은 plan에 명시

## 6. 선택 로직

### 6.1 목적 프로필을 먼저 고정

- 개인 소장용: 동행자, 실제 대화, 시간순 기억과 여유 있는 호흡 우선
- YouTube 본편: 초반 hook, 사건 진행, 반복 압축, 설명 가능한 대화 우선
- 자연 전용: 사람 없는 프레임과 풍경 변화, 음성은 환경음 또는 안내방송만
- Shorts/Reels: 하나의 독립된 setup/payoff와 세로 crop 가능성 우선

같은 evidence에서 프로필만 바꿔 다른 결과를 만든다. 1단계 분석을 다시 하지
않는다.

### 6.2 사건 outline

개별 예쁜 컷을 고르기 전에 하루의 사건을 4~8개 정도로 요약한다.

- 도입/장소 설정
- 이동 또는 기대
- 핵심 경험
- 예상 밖의 행동·대화·반응
- 결과와 하루 마무리

각 사건마다 대표 beat를 고르고, 같은 역할의 중복 후보를 제거한다.

### 6.3 후보 평가

점수는 shortlist를 위한 보조 수단이며 최종 편집 규칙이 아니다.

긍정 요소:

- 시각 품질과 구도
- 하루 서사 기여도
- 재미·감정·자연스러운 반응
- 독특한 장소·행동·여행 정보
- 대화의 명료도와 의미 완결성
- 다른 후보와의 차별성

감점 요소:

- 중복 구도와 반복 발화
- 렌즈 가림, 과도한 흔들림, 노출 실패
- 앞뒤가 없으면 이해되지 않는 문장
- 말하는 사람이 보이지만 자막/음성이 불명확한 구간
- 질문·연결어·말줄임표에서 끝나는 경계
- privacy 또는 공개용 부적합 위험

### 6.4 길이 최적화

목표 러닝타임은 하루 전체의 soft budget이다. clip마다 10초를 할당하는 규칙이
아니다.

권장 순서:

1. 핵심 dialogue/action beat는 완결된 상태로 잠금
2. 정적인 visual shot 길이를 줄임
3. 중복 visual beat를 제거
4. 덜 중요한 사건 beat 전체를 제거
5. 그래도 길면 대화의 반복 문장만 natural pause에서 축약

선택한 대화의 앞이나 뒤를 잘라 목표 시간을 맞추는 것은 금지한다.

## 7. 구조화 편집 결정

모델 출력은 자유 텍스트가 아니라 다음 정보를 가진 결정 목록이어야 한다.

```json
{
  "decision_id": "HD0012",
  "candidate_id": "DAY27-CAND-0042",
  "unit_type": "dialogue_beat",
  "beat_id": "DB0042",
  "source_in": 328.3,
  "source_out": 376.5,
  "keep_subranges": [
    {"start": 328.3, "end": 339.3},
    {"start": 348.2, "end": 357.6},
    {"start": 362.0, "end": 376.5}
  ],
  "evidence_ids": ["F…", "SRU…", "SRC…", "BP…"],
  "story_role": "comic_payoff",
  "selection_reason": "…",
  "boundary_reason": "setup과 사과 반응까지 보존",
  "dialogue_closure": "closed"
}
```

`video-edit-plan/v1`은 이 결정의 렌더 표현이다. plan의 각 clip metadata에는
최소한 `candidate_id`, `unit_type`, `beat_id`, `group_id`, evidence ID와
boundary reason을 보존한다.

## 8. 렌더 전 필수 validator

### 8.1 계보와 범위

- source path/fingerprint가 evidence와 일치
- source range가 media duration 내부
- proxy와 original의 시간 mapping이 identity
- 사용한 frame/segment/group/utterance/caption ID가 packet에 존재
- proposal 기반 source_in/source_out이면 인용한 boundary proposal ID와 시각이 일치

### 8.2 대화 경계 lint

dialogue beat마다 다음을 검사한다.

- 선택된 utterance와 caption span을 100% 포함
- 경계와 겹치지만 100% 포함되지 않은 reviewed caption이 없음
- 같은 beat의 직전·직후 caption이 제외됐다면 제외 이유 존재
- 마지막 표시 문장이 쉼표, `…`, `근데`, `그래서`, `하고 나서`, `왜냐하면`
  같은 열린 형태가 아님
- 질문은 응답 또는 의도적인 reaction까지 포함
- `dialogue_closure=closed`
- hard cut 양쪽의 오디오가 문장 내부가 아님

위 조건을 만족하지 않으면 자동으로 범위를 늘리는 대신 fail closed하고 편집
결정을 다시 요청한다.

### 8.3 무자막 대화 위험

선택 구간에 speech activity와 talking face가 함께 있는데 caption-ready line이
없다면 warning이 아니라 검토 항목으로 승격한다.

가능한 해결은 세 가지뿐이다.

1. 그 짧은 원음 구간을 다시 검토해 확정 자막 생성
2. 발화가 명확한 다른 subrange 선택
3. 대사가 의미 전달 대상이 아닌 ambience임을 plan에 명시

불확실 STT를 그대로 자막으로 승격하는 것은 해결책이 아니다.

### 8.4 시간과 자막

- 편집 결정이 확정된 뒤 source captions를 output timeline으로 매핑
- unified `reviewed_dialogue.captions`는 이미 검수됐으므로 raw STT confidence를
  다시 적용하지 않음
- SRT와 번인 자막은 같은 plan에서 생성
- caption count, 시간 겹침, safe margin과 실제 영상 프레임을 확인

## 9. 모델과 도구의 역할

### 결정적 로컬 도구

- story-day asset 정렬
- joint packet 생성
- 인접 caption/utterance 조회
- gap과 후보 beat 초안 생성
- 점수 계산, 중복 탐지, source/output 시간 매핑
- ID·coverage·boundary validator
- caption attach, FFmpeg render, probe와 QA manifest

### 모델/서브에이전트

- 사건 outline과 편집 리듬 설계
- 시각·행동·대화를 함께 보고 후보 역할 판단
- 기계가 제안한 dialogue/action beat의 의미 완결성 판정
- 재미있는 setup/payoff와 중복 제거
- 경계 validator가 올린 예외 수정

여러 asset의 후보 검토는 병렬화할 수 있지만, 하루 전체의 사건 순서와 길이
최적화는 하나의 coordinator가 수행한다.

## 10. 토큰 효율적인 2단계 조회

1. story day의 영상 요약과 사건 목록만 읽어 outline 작성
2. 사건별 상위 후보의 짧은 metadata와 대표 frame만 조회
3. 실제 선택 후보에만 storyboard와 dialogue beat 전체를 조회
4. 후보 내부뿐 아니라 앞뒤 2~3개 caption을 포함
5. boundary 위험이 있는 구간만 짧은 proxy/audio를 추가 확인
6. 렌더 QA는 선택된 컷 경계와 caption frame만 확인

전체 하루의 모든 이미지나 원시 STT를 다시 모델에 넣지 않는다.

## 11. 표준 산출물

아래 이름은 목표 계약이며 아직 모두 CLI로 구현된 것은 아니다.

```text
work/highlights/<story-day>/<edit-id>/
├── day-editor-packet.json          # 결합된 선택 입력
├── story-outline.json              # 하루 사건 구조
├── highlight-decisions.json        # 근거가 있는 편집 단위 선택
├── boundary-audit.json             # 대화/행동 완결성 검사
├── highlight.edit-plan.json        # source/proxy 좌표와 효과
├── highlight.captioned.edit-plan.json
├── highlight.proxy.mp4
├── highlight.proxy.srt
├── highlight.proxy.render.json
└── qa/
    ├── overview-contact-sheet.jpg
    ├── cut-boundaries.jpg
    └── caption-proof.jpg
```

현재 `build_local_day_highlights.py`의 hard-coded `EDITS`는 prototype 이력으로
남기되 새 골든 2단계 구현의 선택기로 간주하지 않는다.

## 12. 완료 조건

하이라이트 하나가 완료되려면 다음을 모두 만족해야 한다.

- story day와 목적 프로필이 명시됨
- outline의 핵심 사건이 선택 beat와 연결됨
- 모든 clip이 joint evidence candidate에서 파생됨
- 시각 후보 선택 시 해당 구간의 대화와 인접 대화를 함께 검토함
- dialogue/action clip이 의미적으로 닫힘
- 목표 시간 때문에 문장이나 반응을 중간 절단하지 않음
- talking face + speech + no caption 위험이 모두 처리됨
- boundary audit와 edit-plan dry-run 통과
- 자막 plan/SRT/render manifest가 일치
- 프록시에서 전체 흐름, 컷 전후, 자막 번인을 검토함
- 승인 전에는 4K 원본 렌더나 외부 업로드를 하지 않음

## 13. 구현 우선순위

1. `day-editor-packet` 생성기: 시각·행동·대화·인접 문장·품질을 한 후보에 결합
2. dialogue/action beat 초안 생성기와 `closure` 필드
3. `highlight-decisions` 스키마와 validator
4. 대화 경계 lint 및 talking-face-without-caption 검사
5. 결정 목록에서 `video-edit-plan/v1`을 만드는 deterministic compiler
6. 기존 caption attach와 renderer 연결
7. Glacier Bay 핫텁 장면을 고정 회귀 테스트로 추가

이 순서가 완성되기 전에는 “정보를 함께 보고 자동으로 하이라이트를 골랐다”고
표현하지 않는다. 현재 가능한 것은 분석 근거를 사람이/모델이 참고해 수동으로
구간을 정한 prototype이다.
