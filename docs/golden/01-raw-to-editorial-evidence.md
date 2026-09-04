# Golden 01 — 원본 영상에서 편집 근거까지

상태: **규범 문서**

정식 대화 정책: **`dialogue-preservation/v1`**

범위: 원본 미디어 발견부터 영상별 통합 편집 근거 생성까지

다음 단계: [Golden 02 — 편집 근거에서 하이라이트까지](02-editorial-evidence-to-highlight.md)

이 문서는 여행 영상 파이프라인의 1단계 정본이다. 기존의 아키텍처,
Phase 1, STT, 시각 리뷰 문서는 배경 설명과 운영 명령을 제공하지만, 서로
충돌할 때는 이 문서의 데이터 흐름과 완료 조건을 우선한다.

## 1. 목표

1단계의 결과는 짧은 영상이나 단순 요약이 아니다. 각 원본의 모든 시간대를
다음 단계가 다시 영상을 전수 시청하지 않고도 판단할 수 있는 **편집 근거
패키지(editorial evidence package)** 로 바꾸는 것이다.

반드시 다음 질문에 답할 수 있어야 한다.

- 어느 원본의 몇 초 구간인가?
- 화면에 누가·무엇이 보이고 어떤 행동과 변화가 있는가?
- 해당 시각에 실제로 어떤 말이 오갔는가?
- 대화가 확정됐는가, 불확실한가, 비음성인가?
- 자막으로 바로 쓸 수 있는 원문은 무엇인가?
- 재미, 감정, 사건, 여행 정보, 시각적 가치가 있는 순간은 어디인가?
- 흔들림, 가림, 노출, 중복 같은 편집 위험은 무엇인가?
- 대표 프레임과 원시 STT까지 어떻게 역추적하는가?

## 2. 핵심 원칙

### 원본과 좌표는 불변이다

- 카메라 원본은 읽기 전용이다.
- 프록시를 분석해도 모든 결과는 원본과 같은 `source_in/source_out` 초를 쓴다.
- 모든 산출물은 원본 상대 경로, 지문, 길이와 lineage를 보존한다.
- 후속 단계는 앞 단계 파일을 덮어쓰지 않는다.

### 모델은 원본 전체를 순차 시청하지 않는다

- 기계 경계와 640px 대표 프레임으로 전체 시간축을 먼저 압축한다.
- 1차 연락판으로 의미 그룹을 만들고, 그룹별 최대 8장 정도의 조밀한
  스토리보드로 행동과 맥락을 재검토한다.
- 음성은 16kHz mono와 시간 정렬된 후보로 모델에 전달한다.
- 애매한 구간만 개별 프레임이나 짧은 프록시를 추가 확인한다.

### 화면과 음성은 분리 추출하고 통합 판단한다

시각 분석과 STT는 독립적으로 보존하지만, 최종 장면 판단에는 두 근거를
같이 넣는다. 화면 설명만 있는 장면 목록과 전사만 있는 대본을 따로 만든 뒤
끝내면 안 된다.

### 실제 발화와 표시용 자막은 다른 데이터다

- `utterances`: 실제로 무엇이 발화됐다고 판단했는지 기록한다.
- `captions`: 그중 화면에 읽기 좋게 표시할 수 있는 원언어 문장이다.
- 발화 존재가 명확하면 낮은 ASR 신뢰도만으로 버리지 않는다. 언어와 유효한
  문구를 복원할 수 있는 부분은 `captions`로 남기고, 불확실한 단어만 생략·
  말줄임표·문맥 교정으로 표시한다.
- `language=uncertain`은 실제 발화 여부가 아니라 어떤 언어의 어떤 문구인지
  전혀 쓸 수 없는 잔여 파편에만 사용한다. 음악·소음 또는 의미 없는 파편만
  최종 자막에서 제외한다.
- 번역은 별도 필드이며 실제 발화처럼 표시하지 않는다.

## 3. 전체 흐름

```text
[읽기 전용 원본]
      │
      ├─ 인벤토리 · ffprobe · 지문 · 촬영시각
      ├─ 원본↔프록시 lineage
      ▼
[검증된 프록시/음성 캐시]
      │
      ├───────────────┐
      ▼               ▼
[시각 Phase 1]      [Apple 이중 STT]
기계 세그먼트        SpeechDetector
희소 프레임          ko-KR + en-US 원시 후보
품질·변화 신호       span별 시각·신뢰도
      └───────┬───────┘
              ▼
[빠른 공동 그룹화]
희소 연락판 + 음성 활동/STT 힌트
작업 단위와 경계 위험만 확정
              │
              ▼
[그룹별 조밀 스토리보드 생성]
              │
              ▼
[그룹별 통합 리뷰 — 한 번의 모델 패스]
화면 행동·맥락·특이점
window 판정 + 실제 발화 + 자막 대본
편집 가능한 editorial beat
              │
              ▼
[영상 전체 종합]
사건 순서 · 제목 · 요약 · 대표 장면
              │
              ▼
[영상별 편집 근거 패키지]
JSON 정본 + HTML 검토 화면 + 검색 인덱스
```

## 4. 단계별 계약

### 4.1 인벤토리와 lineage

입력:

- 원본 절대 경로와 원본 루트 기준 상대 경로
- 선택적 프록시 경로

처리:

- `ffprobe`로 길이, 코덱, 해상도, 회전, FPS, 오디오와 `creation_time` 수집
- 크기와 앞뒤 바이트를 포함한 빠른 지문 계산
- 프록시와 원본의 길이 차이 및 identity time mapping 검증
- 손상 파일, stub, 중복 키와 미완료 프록시 분리

주요 산출물:

- asset inventory
- proxy manifest
- asset별 lineage JSON
- 재개 가능한 batch state

게이트:

- 원본과 작업 루트가 겹치면 실패
- 프록시 길이가 원본 좌표를 보존하지 않으면 분석 입력으로 쓰지 않음
- 경로·지문·길이가 다른 산출물을 같은 asset으로 합치지 않음

### 4.2 시각 Phase 1

기본 운영값:

- 시각 샘플 간격 약 5초
- 기계 세그먼트 5~30초
- 썸네일 폭 640px
- 이 단계의 inline STT는 끄고(`--stt off`) 음성 파이프라인과 캐시를 분리

처리:

- 전체 시간을 빈틈없는 기계 세그먼트로 분할
- 밝기, 선명도, 시각 변화, 평균 해시 등 저비용 신호 계산
- 희소 프레임과 연락판 생성

보존 산출물:

- `run.json`
- `timeline.machine.json`
- `review-packet.json`
- `frames/`
- `contact_sheets/`

이 단계는 “무슨 일이 벌어지는가”를 확정하지 않는다. 전체 시간축과 검토
근거를 빠짐없이 만드는 역할만 한다.

### 4.3 빠른 공동 의미 그룹화

모델 입력:

- 기계 세그먼트 ID와 시간
- 희소 연락판
- 품질·변화 신호
- 음성 활동 구간과 짧은 ko/en STT 힌트

모델 출력:

- `group_id`, 시작·종료 시각과 포함된 기계 세그먼트
- `복도 셀프 토크`, `동행 합류` 수준의 임시 라벨과 한 줄 요약
- 시각 전환, 행동 전환, 대화 흐름을 근거로 한 분리 이유
- `speech_crosses_boundary` 같은 경계 위험

이 단계는 세부 행동, 실제 대본, 대표 장면과 하이라이트를 확정하지 않는다.
목적은 후속 모델이 독립적으로 검토할 수 있는 작고 완전한 작업 단위를 싸고
빠르게 만드는 것이다. raw STT 문구는 그룹 경계의 힌트이지 정답이 아니다.

검증:

- 모든 세그먼트가 원래 순서로 정확히 한 번 포함돼야 함
- 모델이 새 타임코드나 프레임 ID를 만들 수 없음
- 가능하면 speech window나 명백한 행동을 가로질러 경계를 만들지 않음
- 피할 수 없는 crossing은 삭제하지 않고 양쪽 그룹 packet에 경고로 제공
- `validate-review` 통과 후에만 `timeline.reviewed.json`으로 병합

### 4.4 조밀한 그룹 스토리보드 생성

의미 그룹마다 시간 분포와 화면 차이를 반영한 프레임을 모은다. 기본은 최대
8장이지만, 1분을 넘거나 대화·행동이 밀집한 그룹은 16~24장까지 늘린다.
빠른 행동이나 반응은 더 촘촘히, 정적인 풍경은 더 적게 본다.

이 단계는 모델 리뷰가 아니라 다음 통합 리뷰를 위한 evidence packet 생성이다.
packet에는 다음을 넣는다.

- storyboard와 모든 candidate sample ID·시각
- 거친 그룹 라벨·한 줄 요약
- 포함된 세그먼트와 시간·짧은 시각 요약
- 그룹에 배정된 모든 STT window와 경계 crossing 문맥

세부 `narrative_summary`, 대표 프레임, `notable_moments`와 대화 내용은 아직
확정하지 않는다.

### 4.5 Apple 이중 언어 STT

지원되는 macOS에서는 같은 16kHz mono 음성에 다음을 수행한다.

- `SpeechDetector`로 음성 분석을 게이팅
- `SpeechTranscriber`를 `ko-KR`, `en-US`로 각각 실행
- locale별 원시 결과와 단어/구절 span, 신뢰도, 실행 설정을 모두 보존

주의:

- 같은 한국어 음성이 영어처럼, 같은 영어가 한국어처럼 인식될 수 있다.
- 두 결과가 비슷한 뜻이라고 해서 번역된 것이 아니다.
- 기계 점수만으로 locale 하나를 삭제하지 않는다.
- 짧거나 혼합 언어인 창은 scene-level 리뷰가 여러 발화로 나눌 수 있다.

보존 산출물:

- 추출 WAV
- `transcript.apple.json`
- locale별 raw/normalized 결과
- detector를 통과한 활동 범위와 생성 설정

### 4.6 그룹별 통합 장면-대화 리뷰

편집과 자막에 사용할 기본 경로다. 입력 packet은 반드시 다음을 한 장면
단위로 함께 제공한다.

- 빠르게 나눈 의미 그룹과 조밀한 스토리보드
- 해당 그룹과 겹치는 ko/en 원시 후보
- 시간순 evidence span과 source candidate ID
- 이전·다음 장면 경계와 crossing window

review window 경계는 Apple evidence span 내부를 자르지 않는다. 긴 span이면
설정된 max window보다 window를 늘려 하나의 atomic evidence로 유지한다. 경계
허용오차 등으로 한 span의 timing fragment가 둘 이상 생기더라도 validator는
조각 합집합이 원래 `source_start/source_end`를 빈틈없이 덮는지 확인하고,
원문 문자열은 midpoint가 속한 단 하나의 window만 소유하게 해 장면·shard
경계의 중복 대사를 막는다. span 자체에 시간이 없으면 candidate interval을
fallback으로 명시해 atomic하게 보존한다. 경계를 횡단해 확정된 발화와 자막은
정본 병합 때 시간상 겹치는 모든 context group에 첨부한다. 연락판의
`candidate_frames`도 sample ID뿐 아니라 원본 timeline의 time, timecode와 frame
경로가 정확히 일치해야 한다.

하나의 모델 패스가 서로 연결되지만 구분된 다섯 결과를 낸다.

1. `scene_understanding`: 시간순 행동, 맥락, 대표 프레임과 특이점
2. `window_decisions`: 모든 창을 `resolved`, `uncertain`, `non_speech` 중
   하나로 판정
3. `utterances`: 실제 발화라고 판단한 원언어 문장과 시간, 근거 ID
4. `captions`: 그중 읽기 좋은 표시 문장과 `edit_type`, source utterance ID
5. `editorial_beats`: 거친 그룹 안을 화면·행동·대화의 완결된 단위로 더
   잘게 나눈 편집 후보

`editorial_beats`는 새 top-level scene group이 아니다. 그룹은 모델 호출과
병렬화 단위로 유지하고, beat가 실제 하이라이트 후보의 세밀한 시간 단위가
된다. 각 beat는 `visual`, `action`, `dialogue`, `mixed`, `transition`,
`ambience` 중 하나이며 다음을 보존한다.

- source start/end와 segment/window/utterance/caption/sample ID
- 제목, 요약, 대표 프레임과 신뢰도
- 대화의 `closed/open/not_applicable`
- 거친 그룹 밖으로 확장하거나 이웃과 합쳐야 하는 `boundary_adjustment`

모든 window, utterance와 caption은 정확히 하나의 beat에 연결돼야 한다.
beat 경계는 기존 segment, frame, window, utterance 또는 caption 시각에
고정하며 실제 발화 중간을 자를 수 없다. 여러 utterance를 묶은 caption은
인용한 각 utterance와 실제로 겹쳐야 하며, 첫 발화와 마지막 발화 사이의 빈
구간에만 놓인 caption은 source envelope 안에 있더라도 거부한다.

긴 영상은 **완전한 context group** 단위로만 나눠 병렬 처리한다. 하나의
그룹을 두 에이전트가 나눠 판단하거나 이전 reconciliation 결과를 새 packet의
입력으로 사용하지 않는다.

검증 후 `timeline.dialogue-reviewed.json`으로 병합한다. 모든 ko/en/mixed
발화는 자막으로 연결돼야 한다. window 전체가 uncertain이어도 그 안의
쓸 수 있는 실제 발화는 원언어를 정하고 자막으로 보존한다. 완전히 의미를
복원할 수 없는 잔여 파편만 `language=uncertain`으로 남고 자막에서 제외된다.
거친 그룹 경계를 발화가 가로지르면 `extend_before/after` 또는
`merge_previous/next`가 없이는 통과시키지 않는다.

### 4.7 영상 전체 종합

통합 대화까지 병합된 그룹 정보만으로 영상 전체를 합성한다.

- 제목과 한 줄 요약
- 시작부터 끝까지의 서사
- 모든 의미 그룹의 시간순 사건
- 대표 장면과 하이라이트 후보 그룹
- 검색 태그와 불확실성

대화 리뷰가 변경되면 전체 요약도 다시 생성하거나 최소한 대화 관련 요약을
갱신해야 한다. 오래된 visual-only 요약을 그대로 붙이면 안 된다.

### 4.8 정식 기본 정책과 교차검증

`dialogue-preservation/v1`을 신규 편집 근거 생성의 정식 기본 정책으로 쓴다.
이는 “확신이 낮으면 삭제”가 아니라 다음 순서로 처리한다.

1. 음성 evidence에서 실제 발화 여부를 먼저 판정한다.
2. ko/en/mixed 중 복원 가능한 원언어 발화는 신뢰도가 낮아도 보존한다.
3. Apple window 경계에서 갈라진 문장은 같은 장면의 시간순 evidence로 다시
   묶는다.
4. 읽기 좋은 자막은 실제 발화와 별도 객체로 만들되 source ID를 잃지 않는다.
5. `uncertain` window 안에도 쓸 수 있는 발화가 있으면 자막화한다.
6. 완전히 의미 없는 파편과 명백한 비음성만 자막에서 제외한다.

2026-09-04에 성격이 다른 두 영상으로 이 계약을 교차검증했다.

| 샘플 | 내용 | window | 실제 발화 | 자막 | beat | 결과 |
|---|---|---:|---:|---:|---:|---|
| 0039 | 복도 셀프 촬영·한영 혼합 대화 | 14 | 23 | 22 | 5 | 통과 |
| 0054 | 음식 후기 독백·커플 농담·셀프 엔딩 | 18 | 28 | 28 | 6 | 통과 |

0039에서는 70초 거친 그룹 경계를 `66.48–74.52초` window가 가로지르는
것을 검출하고 `merge_next`/`extend_before`로 대화를 보존했다. 보존 우선
재검토 후 자막은 17개에서 22개, 자막 발화 시간은 59.12초에서 79.82초로
증가했으며 의미를 복원할 수 없는 잔여 파편 1개만 제외했다.

0054에서는 한국어 evidence span 150개를 모두 발화 근거로 사용했고,
불명확한 2개 window도 판정은 `uncertain`으로 유지하면서 문맥상 복원 가능한
대사를 자막으로 살렸다. 서로 다른 대화 유형에서 같은 결과가 재현됐으므로
이 흐름을 파일럿이 아닌 신규 asset의 기본 경로로 채택한다.

## 5. 1단계 정본 산출물

현재 구현에서 새 편집이 우선 사용해야 하는 정본은 다음 형태다.

```text
<asset>/scene-dialogue/
├── review-packet.json
├── review.json
├── timeline.dialogue-reviewed.summarized.json  # 편집 근거 JSON 정본
└── web/
    ├── index.html
    └── manifest.json
```

과거의 `timeline.final.json`은 호환·이력용일 수 있으므로, unified dialogue가
있는 asset에서는 새 하이라이트 선택의 기본 입력으로 사용하지 않는다.

정본 JSON은 최소한 다음을 포함해야 한다.

```json
{
  "asset_id": "…",
  "source": {"path": "…", "quick_fingerprint": "…"},
  "media": {"duration": 0, "creation_time": "…"},
  "segments": [],
  "context_groups": [
    {
      "group_id": "G001",
      "start": 0,
      "end": 0,
      "summary": "…",
      "context_review": {},
      "reviewed_dialogue": {
        "window_decisions": [],
        "utterances": [],
        "captions": []
      }
    }
  ],
  "reviewed_dialogue": {
    "utterances": [],
    "captions": [],
    "summary": {}
  },
  "video_summary": {}
}
```

## 6. 다음 단계에 넘기는 정보

2단계는 프레임만 보고 “좋은 화면”을 고르면 안 된다. asset별 정본에서
다음 자료를 함께 읽을 수 있어야 한다.

- 시각: 그룹·세그먼트 설명, 행동, 대표 프레임, storyboard, notable moment
- 음성: 발화와 자막의 원문·시간·언어·근거·불확실성
- 편집 단위: 그룹 내부 editorial beat, 완결성, 경계 조정과 근거 ID
- 경계: 바로 앞뒤 발화와 장면, 음성 활동과 무음 간격
- 품질: 흔들림, 가림, 노출, 중복, 프레이밍
- 서사: 영상 요약, 사건 순서, 촬영 시각, story day
- 계보: original/proxy 경로, asset/group/segment/utterance/caption ID

핵심은 **대사 텍스트만 같이 주는 것**이 아니라, 후보 구간 밖의 인접 발화와
대화가 닫혔는지까지 같이 주는 것이다.

## 7. 토큰 예산

- 전 영상 공통: 메타데이터, 기계 세그먼트, 희소 프레임
- 장면 1차: 연락판, 짧은 세그먼트 메타데이터와 STT 경계 힌트만 전달
- 장면 2차: 그룹 길이·밀도에 따라 8~24장 storyboard와 span-rich 후보 전달
- 화면 이해·대화·beat는 같은 그룹 리뷰에서 한 번에 확정
- 종합: 검토 완료 그룹 텍스트와 대표 ID만 전달
- 예외: 불명확한 경계만 개별 프레임 또는 짧은 프록시 확인

원본 프레임 수나 영상 길이에 비례해 모델 토큰이 증가하지 않고, 의미 그룹과
음성 후보 수에 비례하도록 유지한다.

## 8. 완료 조건

asset 하나가 1단계를 완료하려면 다음을 모두 만족해야 한다.

- lineage의 source path, fingerprint, duration이 시각·음성·정본 JSON에서 일치
- 전체 시간이 세그먼트로 덮이고 frame/contact sheet 참조가 존재
- 빠른 grouping validator와 통합 group review validator 통과
- ko/en raw STT와 모든 후보/span 보존
- 통합 scene-dialogue packet의 모든 window가 판정됨
- utterance/caption ID, 시간, 언어, source evidence가 검증됨
- `reviewed_dialogue.policy.policy_version`이 `dialogue-preservation/v1`임
- `reviewed_dialogue.policy_audit.status`가 `pass`이고
  `caption_coverage_ratio`가 1.0임
- context group에 화면·행동·대화가 함께 연결됨
- 모든 window/utterance/caption이 정확히 하나의 editorial beat에 연결됨
- 열린 대화와 crossing group boundary에 명시적인 조정 결정이 존재
- 대화 반영 후 video summary가 존재
- JSON 정본과 HTML 검토 화면이 재생성 가능
- 낮은 신뢰도의 실제 발화도 쓸 수 있는 문구는 caption으로 보존되고, 의미
  없는 잔여 파편만 uncertain으로 분리됨

## 9. 현재 알려진 공백

- 화자 분리는 아직 일반화되지 않았다.
- 일부 과거 video summary는 unified dialogue보다 먼저 만들어졌다.
- `timeline.final.json`과 `timeline.dialogue-reviewed.summarized.json`이 함께
  존재해 소비자가 잘못된 정본을 선택할 수 있다.
- 기존 빠른 grouping validator는 기계 세그먼트를 통째로 배정하므로 음성
  window에 맞춰 그룹 경계를 자동 재스냅하는 기능은 아직 없다.
- 기존 전체 아카이브 중 일부는 아직 `dialogue-preservation/v1`로
  backfill되지 않아 새 정본과 과거 `timeline.final.json`이 함께 존재한다.

이 공백은 원본 재분석 없이 기존 frame·Apple STT 캐시에서 새 통합 packet을
생성해 해결한다.
