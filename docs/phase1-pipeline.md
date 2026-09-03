# Phase 1 — 장면 타임라인과 그룹

## 목적

Phase 1은 원본 영상을 편집하지 않습니다. 원본의 전체 시간축을 작은 대표 자료로 바꾸고, 코덱스가 원본 전체를 시청하지 않아도 장면을 설명하고 사건 단위로 그룹화할 수 있게 합니다.

```text
원본 MP4
  → ffprobe 메타데이터
  → 5초 간격 640px 대표 프레임
  → 밝기·선명도·시각 변화량·평균 해시
  → 빈 구간 없는 기계 세그먼트
  → 로컬 MLX Whisper 한국어·영어 병렬 전사 + 환각 필터
  → 12개 세그먼트당 연락판 1장
  → 코덱스 1차 리뷰: 장면 설명과 사건 그룹
  → 사건별 최대 12장 스토리보드
  → 코덱스 2차 리뷰: 맥락·대화 종합·대표 이미지·특이 포인트 선정
  → 장면별 결과만으로 영상 전체 요약·시간순 사건 합성
  → 펼쳐보는 로컬 웹 타임라인
  → 여러 영상의 촬영 시각순 라이브러리
```

## 구현 상태

CLI는 기계 처리, 두 단계 리뷰와 검증 명령을 제공합니다.

```bash
uv run travel-video phase1 VIDEO.mp4 --stt mlx \
  --stt-model mlx-community/whisper-small-mlx \
  --stt-languages ko,en

uv run travel-video validate-review timeline.machine.json timeline.review.json

uv run travel-video merge-review timeline.machine.json timeline.review.json \
  --output timeline.reviewed.json

uv run travel-video build-context-packet timeline.reviewed.json \
  --output-dir context --max-frames 8

uv run travel-video validate-context-review \
  context/context-review-packet.json context/context-review.json

uv run travel-video merge-context-review \
  timeline.reviewed.json context/context-review-packet.json context/context-review.json \
  --output timeline.context-reviewed.json

uv run travel-video build-video-summary-packet timeline.context-reviewed.json \
  --output summary/video-summary-packet.json

uv run travel-video validate-video-summary \
  summary/video-summary-packet.json summary/video-summary.json

uv run travel-video merge-video-summary \
  timeline.context-reviewed.json \
  summary/video-summary-packet.json summary/video-summary.json \
  --output timeline.summarized.json

uv run travel-video render-web timeline.summarized.json \
  --output-dir web

uv run travel-video render-library clip-*/timeline.summarized.json \
  --output-dir work/library --title "여행 날짜 또는 장소"
```

원본은 읽기만 합니다. 파생 자료는 기본적으로 `work/phase1` 아래에 생성되고 Git에서 제외됩니다.

## 목표 단계 데이터 구조

현재 산출물에 더해 STT와 후속 판단은 다음처럼 손실 없는 단계 구조로 확장합니다. 각 디렉터리는 앞 단계 파일을 덮어쓰지 않으며, 아래 단계가 사용한 상위 레코드 ID를 기록합니다.

```text
transcript/
├── raw/<provider>/<run-id>/       # 공급자가 반환한 원시 응답과 실행 설정
├── normalized/                    # 공통 스키마로 변환한 모든 후보
├── aligned/                       # VAD 구간과 시간 중첩으로 묶은 KO/EN/자동 후보
├── decisions/                     # 채택·제외, 점수와 제외 이유
├── reconciled/                    # 원 발화 언어를 보존한 종합 대본
├── corrections/                   # 사람의 수정 이력, 원래 값과 수정 값
└── exports/                       # SRT/VTT/읽기용 대본처럼 재생성 가능한 표현
```

장면 분석도 같은 원칙으로 `machine → review packet → model review → validated merge → human correction`을 별도 파일로 유지합니다. 웹 HTML과 SRT/VTT는 원천 데이터가 아니라 언제든 다시 만들 수 있는 표시 계층입니다.

작은 JSON/JSONL과 판단 계보는 적극적으로 보존합니다. WAV·프록시·프레임처럼 큰 파일은 캐시로 정리할 수 있지만, 어떤 원본과 설정으로 만들었는지 적힌 manifest는 보존합니다.

## 출력 구조

```text
work/phase1/<asset-id>/<visual-config-hash>/
├── frames/                    # 640px 저빈도 대표 프레임
├── contact_sheets/            # 한 장에 최대 12개 세그먼트
├── transcript/<model>/ko/     # 한국어 강제 전사 원본 JSON
├── transcript/<model>/en/     # 영어 강제 전사 원본 JSON
├── context/storyboards/       # 검토 그룹별 최대 12장 스토리보드
├── context/context-review-packet.json
├── context/context-review.json
├── run.json                   # 실행 상태와 개수
├── timeline.machine.json      # 기계 신호와 전수 시간축
├── review-packet.json         # 코덱스에 전달할 압축 입력
├── timeline.review.json       # 코덱스/사람의 구조화 판단
├── timeline.reviewed.json     # 검증 후 병합 결과
├── timeline.reviewed.html     # 설명·검토 그룹이 표시된 최종 타임라인
├── timeline.context-reviewed.json
├── timeline.context-reviewed.html # 사건 맥락·대화·대표 이미지 최종 결과
├── summary/video-summary-packet.json # 검토 완료 장면만 담은 영상 종합 입력
├── summary/video-summary.json        # 영상 제목·전체 서사·시간순 사건
├── timeline.summarized.json          # 영상 단위 요약까지 병합한 최종 데이터
├── web/index.html              # 재사용 인터랙티브 타임라인
├── web/manifest.json           # 입력과 렌더 설정
└── timeline.html              # 로컬 브라우저 검토 화면
```

## 토큰 절약 장치

1. 코덱스에는 원본 영상이나 모든 프레임을 주지 않습니다.
2. 한 번의 저해상도 프레임 추출로 밝기, 선명도, 변화량, 대표 프레임 선택을 모두 처리합니다.
3. 1차 연락판으로 사건 그룹을 만든 뒤, 그룹 내부에서 시간 분포·화면 차이·화질을 반영한 최대 12장만 2차 스토리보드로 만듭니다. 현재 5초 샘플이 12장 이하면 모두 보여 줍니다.
4. 코덱스는 스토리보드 전체 맥락을 본 뒤 대표 이미지를 직접 고릅니다. 기계가 고른 단일 이미지는 최종값이 아닙니다.
5. 한국어·영어 STT 원본은 둘 다 로컬에 보존하되 리뷰 패킷에는 장면과 겹치는 짧은 후보만 넣습니다.
6. Whisper의 `avg_logprob`, `no_speech_prob`, `compression_ratio`와 연속 중복으로 낮은 신뢰도 전사를 거르지만, 두 언어 중 하나를 점수만으로 영구 삭제하지 않습니다.
7. 시각 단계 캐시 키와 언어별 STT 캐시를 분리했습니다. STT 전략만 바꿀 때 프레임을 다시 추출하지 않습니다.
8. 코덱스가 세그먼트 ID나 타임코드를 바꿀 수 없고, 리뷰 그룹과 대표 프레임 ID를 각각 검증합니다.
9. 특이 포인트는 같은 2차 스토리보드 리뷰에서 그룹당 0~3개만 고르므로 별도의 전수 영상 시청이나 모델 패스가 없습니다.
10. 영상 전체 요약 단계는 최종 장면 설명·대화·특이 포인트와 후보 프레임 ID만 읽습니다. 원본, 연락판과 스토리보드를 다시 모델에 넣지 않습니다.
11. 다중 영상 페이지는 `timeline.summarized.json`들을 로컬에서 정렬·렌더할 뿐 추가 모델 호출이 없습니다.

## 파일럿 결과

2026-09-02에 T7의 `cam2-0825`에서 서로 다른 두 영상을 처리했습니다.

| 원본 | 특징 | 길이 | 샘플 프레임 | 기계 세그먼트 | 기계 그룹 | 검토 그룹 |
|---|---|---:|---:|---:|---:|---:|
| `DJI_20260826073937_0009_D.MP4` | 푸드코트·음료 주문, 2.7K/30fps | 200.5초 | 40 | 31 | 30 | 5 |
| `DJI_20260826111739_0034_D.MP4` | 빙하·헬기·셀카, 4K/60fps | 109.5초 | 22 | 11 | 9 | 4 |

두 원본은 합계 약 2.40 GiB이며 약 310초 동안 12,500장 이상의 원본 프레임이 있습니다. Phase 1은 62장의 640px JPEG와 4장의 1차 연락판으로 압축했습니다. 2차 검토에서는 기존 JPEG를 재사용해 사건 스토리보드 9장을 추가했습니다. 한국어·영어 전사, JSON과 HTML을 모두 포함한 파생 자료는 약 5.7 MiB였습니다.

코덱스는 먼저 네 장의 연락판으로 사건을 나눈 뒤 아홉 장의 사건별 스토리보드와 양쪽 STT 후보를 검토했습니다. 음식 트럭 영상은 다음 사건으로 정리됐습니다.

1. 푸드코트 탐색
2. 노란 트럭 메뉴 고르기
3. 주문과 음료 수령
4. 음료를 들고 자리 이동
5. 화로 테이블에서 휴식

빙하 영상은 다음 네 사건으로 정리됐습니다.

1. 빙하와 헬기 전경
2. 빙하 위 이동과 카메라 조작
3. 빙하 위 동행자 셀카
4. 헬기 쪽으로 이동

같은 2차 리뷰에서 일반 사건 요약과 분리해 음식 영상 5개, 빙하 영상 4개의 특이 포인트를 기록했습니다. 예를 들어 7.5달러 음료와 팁 걱정, 렌즈를 덮은 손, 더위 대사와 화로의 아이러니, 빙하 위에서 90도로 돌아간 카메라, 셀카 직전의 익살스러운 표정이 포함됩니다.

같은 날 음식 영상의 직전 1개와 직후 2개를 추가 처리해 `0008`–`0011` 네 파일을 하나의 시퀀스로 만들었습니다.

| 순서 | 원본 | 길이 | 최종 사건 | 영상 전체 요약 제목 |
|---:|---|---:|---:|---|
| 1 | `DJI_20260826073820_0008_D.MP4` | 58.1초 | 2 | 불꽃 테이블에 자리 잡기 |
| 2 | `DJI_20260826073937_0009_D.MP4` | 200.5초 | 5 | 푸드트럭에서 알래스카 소다 사기 |
| 3 | `DJI_20260826074332_0010_D.MP4` | 35.5초 | 2 | 첫 모금과 권태기 농담 |
| 4 | `DJI_20260826074453_0011_D.MP4` | 56.7초 | 3 | 소스 고르고 피시 타코 공개 |

전체 5분 51초가 12개 사건과 12개 특이 포인트로 연결됐습니다. 새로 처리한 세 파일은 24개 저빈도 세그먼트와 7개 사건 그룹으로 압축했습니다. 각 영상의 제목, 한 줄 설명, 시작부터 끝까지의 서사와 모든 사건을 순서대로 합성한 뒤, MP4 `creation_time` 메타데이터와 파일명으로 안정 정렬했습니다. 메타데이터에 UTC 오프셋이 있으므로 웹에는 임의로 현지 시각으로 바꾸지 않고 `UTC`라고 명시합니다.

## STT 파일럿에서 배운 점

Whisper tiny는 음악과 주변 소리를 반복 한국어 문장으로 잘못 전사했습니다. 또한 파일 전체를 한국어로 판정하자 영어 직원의 말을 어색한 한국어로 바꿨습니다. 단일 언어 원문을 그대로 리뷰 입력에 넣는 것은 판단 오염이었습니다.

파일럿에서는 `mlx-community/whisper-small-mlx`로 승격하고 전체 음성을 한국어와 영어로 각각 전사합니다. 두 후보를 타임코드별로 나란히 보존하고 코덱스가 화면 맥락과 함께 종합합니다. 기계 점수는 우선순위 힌트일 뿐입니다.

음식 트럭의 80~130초 주문 구간에서 한국어 패스는 영어 대화를 부정확하게 옮겼지만 영어 패스는 `recommend flavor`, `mandarin`, `grapefruit`, `7.35`와 감사 인사를 복원했습니다. 반대로 후반 휴식 구간의 “더워, 너무 더운데”는 한국어 후보가 유효했습니다.

각 언어 후보 내부에서는 다음 전사를 제외합니다.

- 평균 로그 확률이 `-1.0` 미만
- 무음 확률이 `0.6` 초과
- 압축률이 `2.4` 초과인 반복 문구
- 바로 인접한 동일 문구

필터를 통과해도 두 언어 후보는 직역, 번역 또는 음차일 수 있습니다. 최종 결과에는 원문을 확정 자막처럼 복사하지 않고 “무슨 대화를 했는지”를 맥락 요약하며 불확실성을 유지합니다.

### VAD·언어 라우팅 실험

`stt-adaptive`는 음성 처리만 독립적으로 다음 순서로 실행합니다.

```text
에너지 기반 VAD와 무음 원시 구간
  → 긴 발화 영역을 최대 10초 분석 창으로 분할
  → 각 창의 Whisper 전체 언어 확률 저장
  → 확률·1/2위 차이·길이로 언어 라우팅 계획 생성
  → 확실한 창은 선택 언어만 전사
  → 불확실하거나 언어 경계에 인접한 창은 KO/EN 모두 전사
  → 모든 원시 후보를 공통 타임코드로 정규화
  → 단일 후보만 유효하면 자동 채택, 복수 후보가 유효하면 종합 대기
```

2026-09-02에 음식 트럭 `0009` 전체 200.5초를 10초 상한으로 실행했습니다. 배경 음악과 푸드코트 소음이 계속되어 FFmpeg 에너지 VAD는 무음 경계를 찾지 못했고, 파이프라인은 21개 균등 창으로 보완했습니다. 전체 파일 자동 감지는 한국어 하나로 고정됐지만 구간별 라우팅은 영어 주문의 `Can you recommend flavor?`, `mandarin or grapefruit`, `anything else for you?`, `7.35`를 영어 후보로 복원했습니다.

21개 창의 보수적 최종 상태는 한국어 11개, 영어 3개, 혼합 또는 불확실 6개, 유효 후보 없음 1개였습니다. 7개 미확정 창은 한 언어를 억지로 선택하지 않고 후속 저비용 자막 종합 입력으로 남겼습니다. 이 결과는 VAD·구간별 언어 탐지가 전체 파일 단일 언어 감지보다 낫지만, 시끄러운 장소에서 실제 화자 전환을 분리하려면 신경망 VAD 또는 화자 턴 탐지가 추가로 필요함을 보여 줍니다.

실행 산출물은 `work/stt-adaptive/0009-10s-p080-v3`에 있으며 다음을 각각 보존합니다.

- `vad/silences.json`, `vad/chunks.json`: 검출 원본과 분석 창
- `language/raw/*.json`: 언어별 전체 확률
- `routing/plan.json`: 선택 언어, 신뢰도와 이중 전사 이유
- `transcript/raw/<chunk>/<language>.json`: 언어별 MLX 원시 응답
- `normalized/chunks.json`: 원본 타임코드를 복원한 모든 후보와 판정
- `transcript.routed.json`: 단일 언어 결과와 종합 필요 표시

### Apple SpeechDetector·이중 로케일 실험

macOS 26.6.2에서 새 `SpeechAnalyzer` API를 사용한 `stt-apple` 경로를 추가했습니다. 한 번 추출한 16 kHz mono WAV에 Detector와 Transcriber를 같은 분석 모듈 배열로 넣고, 로케일이 필요한 Transcriber만 `ko-KR`, `en-US`로 각각 실행합니다. Detector 감도 기본값은 Apple 권장값인 `medium`입니다.

```text
원본 오디오 1회 추출
  → SpeechDetector(medium) + SpeechTranscriber(ko-KR)
  → SpeechDetector(medium) + SpeechTranscriber(en-US)
  → 언어별 원시 JSON 보존
  → 구절별 시작·끝·신뢰도를 공통 후보로 정규화
  → 양쪽 타임스탬프 합집합으로 확인용 음성 활동 범위 생성
  → 저비용 대본 종합 단계의 입력으로 전달
```

음식 트럭 `0009`의 원본 80–140초 구간에서 60초 파일럿을 수행했습니다. 한국어 패스는 `자몽이나 감귤 맛 추천한대`, `만다린 먹어볼까?`, `비싸`, `땡큐` 같은 한국어 맥락을 살렸고, 영어 패스는 `Can you recommend flavor?`, `I like mandarin or grapefruit`, `anything else for you?`, `7.35 for the two`, `Thank you`를 더 잘 복원했습니다. 어느 한쪽도 혼합 발화를 완전히 처리하지 못하므로 두 원시 후보를 유지한 뒤 맥락 기반으로 종합하는 기존 원칙이 여전히 필요합니다.

통합 실행 결과는 `work/apple-speech/80-140-pipeline`에 있습니다.

- `raw/ko-KR.json`, `raw/en-US.json`: Apple 원시 응답, Detector 설정, 구절별 시간·신뢰도
- `normalized/candidates.json`: 양쪽 결과를 공통 타임코드로 정규화한 51개 후보
- `vad/activity.json`: 7개 확인용 활동 범위와 범위 생성 방식
- `transcript.apple.json`: Detector 사용 여부, 모든 후보와 산출물 경로를 묶은 최종 manifest
- `run.json`: 입력 지문, 설정, 호스트와 실행 상태

Apple의 현재 macOS 26 API에서 `SpeechDetector.results`는 VAD 시작·끝을 외부에 제공하는 용도가 아니라 오류 보고만 지원합니다. 따라서 `detector_results`가 비어 있어도 Detector는 Transcriber 앞에서 실제 게이팅에 참여합니다. 외부에서 검사 가능한 활동 범위는 이를 직접 검출한 값으로 사칭하지 않고 `detector_gated_transcriber_time_union`으로 표시합니다. 독립적인 raw VAD 경계가 필요하면 신경망 VAD를 병렬 신호로 유지해야 합니다.

이후 `0009` 전체 200.5초를 같은 설정으로 실행해 19개 활동 범위, Apple 한국어 후보 57개와 영어 후보 39개를 얻었습니다. 구절별 시간을 사용해 이를 27개의 최대 8초 대조 창으로 줄이고 기존 MLX 언어 확률과 장면 그룹을 보조 근거로 연결했습니다. 코덱스 맥락 검토에서는 한 창 안의 언어 전환도 별도 발화로 나눠 한국어 21개, 영어 13개, 불명확 1개로 구성된 35개 발화 대본을 만들었습니다.

최종 대본은 번역처럼 보이는 반대 로케일 후보를 그대로 복사하지 않습니다. `original_text`에는 실제 발화 언어와 문자를 유지하고, 다른 언어 번역은 `translations.ko` 또는 `translations.en`에만 둡니다. 두 Apple 후보가 모두 깨진 구간은 빈 창 또는 `uncertain` 발화로 보존합니다. 모든 확정 문장은 사용한 `APPLE-<locale>-T####` 원시 후보 ID를 가지며 검증기가 존재하지 않는 ID나 원래 창을 벗어난 타임코드를 거부합니다.

전체 실행의 단계별 산출물은 `work/apple-speech/0009-full-v1`에 있습니다.

- `raw/*.json`: Apple 공급자 원문
- `normalized/candidates.json`: 공통 스키마 후보 96개
- `vad/activity.json`: Detector 게이팅 이후 확인용 활동 범위 19개
- `reconciliation/packet.json`: 최대 8초 한·영 대조 창 27개
- `reconciliation/review.json`: 언어·원문·별도 번역에 대한 구조화 판단
- `reconciliation/transcript.reconciled.json`: 검증·병합된 발화 35개
- `reconciliation/index.html`: 원시 후보와 종합 결과를 나란히 보는 중간 산출물 UI

## 2차 스토리보드에서 배운 점

기존에는 세그먼트 중간과 화질 점수로 대표 이미지 한 장을 골랐습니다. 빙하 이동 장면에서는 이 방식이 바닥을 향한 프레임을 대표로 골라 노란 헬기를 놓쳤습니다.

2차 단계는 검토 그룹 안에서 다음 요소로 최대 12장을 고릅니다.

- 시간적으로 서로 떨어진 정도
- 평균 해시가 다른 정도
- 선명도·노출 기반 품질

코덱스가 이 스토리보드를 보고 장면 전체 설명, 핵심 순간과 최종 대표 프레임을 고릅니다. 동시에 솔직한 표정, 돌발 상황, 시각적으로 강한 컷, 살릴 대사와 여행 디테일이 실제로 있을 때만 `notable_moments`에 기록합니다. 각 항목은 후보 프레임 ID, 설명과 편집 활용도를 가져야 하며 그룹당 최대 3개입니다. 실제 파일럿에서 빙하 이동 장면의 대표는 바닥 프레임에서 `F0006`의 노란 헬기 전경으로, 음식 주문 장면은 메뉴판에서 `F0026`의 음료 수령 순간으로 교체됐습니다.

긴 음식 메뉴 선택 그룹은 11장, 빙하 이동 그룹도 11장으로 다시 생성해 현재 5초 샘플을 빠짐없이 확인했습니다. 더 촘촘한 2초 프레임은 전체 영상에 적용하지 않고, 긴 그룹이나 시각 변화가 큰 그룹에만 후속 추출하는 적응형 단계로 남깁니다.

## 웹 타임라인

`render-web`은 최종 JSON을 동일한 UX의 정적 웹으로 변환합니다. 세로 사건 요약에서 장면을 펼치면 다음 정보를 볼 수 있습니다.

- 사건 전체 설명과 대표 프레임 선정 이유
- 일반 요약에서 빠지기 쉬운 특이 포인트와 구체적인 편집 활용도
- 화면·행동·음성·품질·편집 제안을 같은 원본 타임코드에 정렬한 판단 행
- 최대 16장의 시간순 프레임과 핵심 순간 표시
- 화면과 이중 STT를 종합한 대화 요약
- 한국어·영어 STT 원문 후보와 개별 타임코드
- 선택한 사건을 표시하고 향후 저화질 프록시를 연결할 상단 공용 미리보기 덱

현재 `render-web`은 미디어 입력을 받지 않습니다. 따라서 웹 생성은 즉시 끝나고 영상 파일을 복사하거나 인코딩하지 않습니다. 검토 프레임은 기본적으로 HTML에 내장되어 Codex 리모트에서도 별도 파일 경로 없이 보입니다. `--assets relative`를 선택하면 같은 로컬 폴더 구조를 유지하는 대신 HTML 크기를 줄일 수 있습니다. 상세 설계는 [웹 타임라인 문서](web-timeline.md)를 참고합니다.

화면은 1280px 이상 데스크톱 편집 환경을 주 사용 시나리오로 설계합니다. 모바일 반응형은 결과를 간단히 확인할 수 있는 호환 계층이며, 정보 밀도와 최종 UX 판단은 데스크톱을 기준으로 합니다.

## 현재 한계

- 손으로 들고 이동하는 영상에서는 단순 시각 변화량이 의미 장면보다 카메라 움직임에 민감해 과분할됩니다.
- 평균 해시는 같은 장소를 다른 각도로 촬영한 구간을 잘 묶지 못합니다.
- 현재 HTML은 읽기 전용이며 별점·그룹 수정 UI는 없습니다.
- 언어 강제 전사는 다른 언어 발화를 번역하거나 음차할 수 있으므로 두 후보와 영상 맥락을 함께 보아야 합니다.
- 5초 사이에 일어난 매우 짧은 사건은 대표 프레임에 잡히지 않을 수 있습니다.

과분할은 시간축 누락보다 안전하며 이번 파일럿에서는 코덱스 리뷰가 이를 보정했습니다. 전체 18시간으로 확대하기 전에는 로컬 시각 임베딩으로 같은 장소·대상을 묶고, 의미 변화가 큰 구간만 연락판에 남기는 Phase 1.1이 필요합니다.

## 다음 구현 우선순위

1. 로컬 시각 임베딩을 이용한 의미 기반 인접 그룹
2. 음성 구간 검출과 화자 구분을 추가해 무음·음악의 이중 STT 연산 감소
3. SQLite 단계별 작업 큐와 실패 재시작
4. 웹에서 그룹 합치기·나누기·제외·별점 저장
5. 여러 영상 사이의 연결 장면·중복·누락을 판단하는 날짜 단위 상위 서사
