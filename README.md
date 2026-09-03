# Travel Video Editor

액션캠으로 촬영한 장기간 여행 영상을 로컬 우선 방식으로 정리·검색·선별하고, 개인 소장용과 공개용 편집안을 함께 만드는 프로젝트입니다.

이 프로젝트의 핵심은 원본 영상을 코덱스가 순차 시청하거나 한 번에 AI에 업로드하는 것이 아니라 다음 파생 자료를 먼저 만드는 것입니다.

- 주문형 저해상도 프록시
- 타임스탬프가 포함된 음성 전사
- 장면 및 주기별 대표 프레임
- 촬영 시각·카메라·GPS 같은 메타데이터
- 흔들림·노출·음량 등 품질 지표
- 원본 파일과 타임코드로 역추적할 수 있는 편집 계획

## 현재 상태

- 2026-09-02: Phase 1 CLI 구현 및 실제 Osmo Pocket 3 영상 다섯 개 파일럿 완료
- 음식 장면 전후 4개 영상을 하나의 시간순 라이브러리로 연결하고, 각 영상의 전체 요약·사건 순서·대표 프레임 생성
- 음식 시퀀스 5분 51초를 12개 사건과 12개 특이 포인트로 정리한 데스크톱 웹 인덱스 생성
- 초기 두 영상 2.40 GiB·310초 원본을 62개 대표 프레임과 연락판 4장, 약 4.0 MiB 파생 자료로 변환
- 구조화 리뷰 검증을 거쳐 음식 트럭 영상 5개, 빙하 영상 4개 사건 그룹 생성
- 한국어·영어 전체 이중 STT와 사건별 최대 12장 스토리보드 2차 리뷰 구현
- VAD 경계·10초 상한 구간별 언어 확률을 계산하고, 확실한 언어만 단일 전사하며 불확실한 경계만 KO/EN 후보를 남기는 적응형 STT 실험 구현
- macOS 26 Apple `SpeechDetector` + 한국어·영어 `SpeechTranscriber` 이중 패스 구현; 원시 응답과 구절별 시간·신뢰도 보존
- 코덱스가 장면 맥락을 보고 대표 프레임을 다시 선택하며 최종 결과는 약 5.7 MiB
- 리모트에서도 이미지가 깨지지 않는 단일 HTML 웹 리포트와 공용 미리보기 덱 구현
- 화면·행동·이중 STT·품질 신호·편집 제안을 원본 타임코드별로 합친 편집 판단 타임라인 구현
- 웃긴 반응·돌발 상황·그림 되는 컷·살릴 대사 같은 특이 포인트를 별도 편집 비트로 기록하고 표시
- 장면별 검토 결과만 다시 읽어 영상 전체를 종합하는 `video_summary` 단계와 촬영 시각순 다중 영상 템플릿 구현
- 2026-09-02: 토큰 효율적인 전수 장면 타임라인 설계 추가
- 2026-09-03: 문서 계약을 Codex SDK, FastAPI 도구 게이트웨이, SQLite 불변 편집 리비전, Edit Desk AI 채팅이 함께 사용하는 프로덕션형 수직 슬라이스 구현
- T7의 4K·카메라 원본 400개(약 370.64 GiB)를 읽기 전용 소스로 확정하고, ExternalSSD에 검증 가능한 1080p 작업 프록시 배치 생성 중
- 현재 Mac에서 Apple Silicon, FFmpeg 8.1.2, `ffprobe`, `uv` 사용 가능 확인
- `uvx`로 MLX Whisper를 로컬 실행했으며 small 모델 파일럿 완료; 일반적인 NLE 앱은 표준 경로에서 확인되지 않음
- 대용량 원본은 T7, 분석·검토용 1080p 프록시는 고정형 ExternalSSD, 코드와 작은 메타데이터는 프로젝트 저장소로 분리

## 원칙

1. 원본은 읽기 전용으로 다루고 수정·이름 변경·삭제하지 않는다.
2. 모든 추천 클립은 `asset_id + source_in + source_out`으로 원본까지 추적 가능해야 한다.
3. STT만 사용하지 않고 음성·영상·메타데이터를 함께 색인한다.
4. 대규모 처리 전에 하루치 또는 30~60분 분량으로 파일럿을 수행한다.
5. 자동 편집 결과는 검토 가능한 미리보기와 편집 계획을 먼저 만들고, 승인 후 최종 렌더한다.
6. MCP는 초기 분석의 필수 요소가 아니라 반복 작업 또는 NLE 제어가 필요할 때 선택적으로 연결한다.
7. 전수 타임라인과 편집 후보를 분리하고, 모델 호출은 중요도·중복도에 따라 계층화한다.
8. 사실 요약과 재미있는 편집 비트를 분리하고, 특이 포인트는 실제 근거가 있을 때만 기록한다.
9. 영상 전체 요약은 이미 검토한 장면 데이터에서만 합성하고 원본 영상이나 이미지 시트를 다시 읽지 않는다.

## 문서

- [도구·MCP·스킬 조사](docs/research-and-tooling.md)
- [권장 아키텍처](docs/architecture.md)
- [토큰 효율적인 전수 장면 타임라인](docs/token-efficient-scene-timeline.md)
- [Phase 1 구현과 파일럿 결과](docs/phase1-pipeline.md)
- [영상 단위 요약과 시간순 다중 영상 라이브러리](docs/video-library.md)
- [채팅 기반 AI 영상 편집 플랫폼 설계](docs/agentic-editor-platform.md)
- [Editor Service 실행 가능한 기술 계약](docs/technical/editor-service.md)
- [에이전트 도구 카탈로그](docs/contracts/editor-tools.v1.json)
- [SSE 이벤트 계약](docs/contracts/agent-events.v1.schema.json)
- [`kyungdoc/video-summary` 검토](docs/upstream-video-summary-review.md)
- [T7 원본 사전 점검](docs/source-assessment-2026-09-02.md)
- [소스 미디어와 작업 미디어 위치](docs/media-locations.md)
- [파일럿 실행 계획](docs/pilot-plan.md)
- [결정 기록과 미결 사항](docs/decisions.md)

## 예상 프로젝트 구조

실제 구현 단계에서는 아래 구조를 기준으로 하되 원본 영상은 프로젝트 밖에 둡니다.

```text
travel-video-editor/
├── README.md
├── docs/
├── src/travel_video/   # Phase 1 처리·검증 CLI
├── tests/
├── config/             # 카메라, 언어, 출력 프로필 설정
├── schemas/            # asset/edit-plan JSON 스키마
├── scripts/            # 인벤토리, 프록시, STT, 색인, 내보내기
├── catalog/            # JSONL/SQLite 색인과 연락판
├── work/               # 재생성 가능한 임시 산출물
└── exports/            # 미리보기, 자막, OTIO/FCPXML/EDL
```

현재 4K 원본은 T7, 1080p 작업 프록시는 ExternalSSD에 둡니다. 정확한 절대 경로, 현재 파일 수와 원본/프록시의 역할은 [미디어 위치 문서](docs/media-locations.md)를 기준으로 합니다. 이 저장소에는 코드·설정·작은 메타데이터만 보관합니다.

## 영상 편집 스킬

[`video-editor`](skills/video-editor/SKILL.md) 스킬은 검토된 장면과 원본 타임코드를 `video-edit-plan/v1` JSON으로 만든 뒤 FFmpeg로 실제 영상을 조립합니다. 여러 파일의 구간 trim과 재정렬, 프록시/원본 재연결, 속도·볼륨 조절, 제목·라벨·번인 자막, SRT 출력, 오디오 정규화와 결과 검증을 지원합니다.

저장소에는 재현 가능한 스킬 원본을 함께 보존하고, 현재 Mac의 전역 설치본은 `~/.codex/skills/video-editor`에서 어디서든 호출할 수 있습니다. 샘플 편집안은 [`examples/skagway-foodcourt-highlight-v1.edit-plan.json`](examples/skagway-foodcourt-highlight-v1.edit-plan.json)에 보존합니다.

```bash
python3 ~/.codex/skills/video-editor/scripts/render_edit.py \
  examples/skagway-foodcourt-highlight-v1.edit-plan.json \
  --output /Volumes/ExternalSSD/travel-video-editor/exports/food-sequence-highlight/skagway-foodcourt-highlight-v1.mp4 \
  --media-mode source
```

## Phase 1 실행

```bash
uv sync
uv run travel-video phase1 /absolute/path/to/video.mp4 \
  --stt mlx \
  --stt-model mlx-community/whisper-small-mlx \
  --stt-languages ko,en
```

결과는 기본적으로 `work/phase1`에 생성됩니다. 자세한 출력과 리뷰 절차는 [Phase 1 문서](docs/phase1-pipeline.md)를 참고합니다.

혼합 언어 음성만 독립적으로 분석하려면 단계별 산출물을 보존하는 적응형 STT 명령을 사용할 수 있습니다.

```bash
uv run travel-video stt-adaptive /absolute/path/to/video.mp4 \
  --output-dir work/stt-adaptive/sample \
  --model mlx-community/whisper-small-mlx \
  --expected-languages ko,en \
  --max-chunk 10
```

이 명령은 VAD 구간, 언어별 전체 확률, 라우팅 판단, 언어별 원시 전사와 정규화 결과를 각각 보존합니다. 현재 VAD는 FFmpeg 에너지 기반 구현이므로 지속적인 배경 소음에서는 최대 길이 창으로 보완합니다.

macOS 26 이상에서는 Apple 온디바이스 STT를 Detector와 함께 실행할 수 있습니다.

```bash
uv run travel-video stt-apple /absolute/path/to/video.mp4 \
  --output-dir work/stt-apple/sample \
  --locales ko-KR,en-US \
  --detector-sensitivity medium
```

이 경로는 한 번 추출한 16 kHz mono 음성에 대해 `SpeechDetector`가 각 언어별 `SpeechTranscriber`를 게이팅하게 합니다. 한국어와 영어 원시 결과를 각각 보존하고 구절별 시간·신뢰도, 정규화 후보와 확인용 음성 활동 범위를 생성합니다. 현재 Apple API는 Detector의 독립적인 VAD 경계를 결과로 제공하지 않으므로, 표시용 범위는 Detector를 통과한 양쪽 Transcriber 타임스탬프의 합집합임을 데이터에 명시합니다.

원문과 번역을 분리한 종합 대본은 다음 세 단계로 만듭니다.

```bash
uv run travel-video build-transcript-reconciliation-packet \
  work/stt-apple/sample/transcript.apple.json \
  --mlx-normalized work/stt-adaptive/sample/normalized/chunks.json \
  --timeline work/phase1/sample/timeline.context-reviewed.json \
  --max-window 8 \
  --output work/stt-apple/sample/reconciliation/packet.json

uv run travel-video validate-transcript-reconciliation \
  work/stt-apple/sample/reconciliation/packet.json \
  work/stt-apple/sample/reconciliation/review.json

uv run travel-video merge-transcript-reconciliation \
  work/stt-apple/sample/reconciliation/packet.json \
  work/stt-apple/sample/reconciliation/review.json \
  --output work/stt-apple/sample/reconciliation/transcript.reconciled.json
```

패킷은 Detector가 통과시킨 Apple 한·영 후보를 최대 8초 대조 창으로 정렬하고 MLX 언어 확률과 장면 맥락을 보조 근거로 붙입니다. 리뷰는 한 창 안에서도 화자 전환과 한·영 전환을 여러 발화로 나눌 수 있습니다. 최종 파일의 `original_text`는 실제 발화 언어를 유지하고 번역은 `translations`에만 기록합니다.

검토가 끝난 타임라인은 영상 인코딩 없이 인터랙티브 웹으로 만들 수 있습니다.

```bash
uv run travel-video render-web timeline.context-reviewed.json \
  --output-dir web
```

기본값은 프레임을 `index.html`에 내장해 파일 하나만 열어도 동작합니다. 영상은 넣지 않고 상단 공용 미리보기 덱만 남기며, 향후 저화질 프록시 하나를 이 영역에 연결합니다. 같은 로컬 파일 구조에서만 쓸 때는 `--assets relative`로 HTML 크기를 줄일 수 있습니다. [웹 타임라인 설계](docs/web-timeline.md)에 재사용 구조와 프록시 연결 방식을 기록했습니다.

장면별 맥락 검토 뒤에는 원본을 다시 보지 않고 영상 하나의 전체 흐름을 합성하고, 여러 영상을 촬영 시각순 인덱스로 묶을 수 있습니다.

```bash
uv run travel-video build-video-summary-packet timeline.context-reviewed.json \
  --output summary/video-summary-packet.json

# packet을 근거로 summary/video-summary.json을 작성한 뒤 검증·병합
uv run travel-video validate-video-summary \
  summary/video-summary-packet.json summary/video-summary.json
uv run travel-video merge-video-summary \
  timeline.context-reviewed.json \
  summary/video-summary-packet.json summary/video-summary.json \
  --output timeline.summarized.json

uv run travel-video render-library \
  clip-a/timeline.summarized.json clip-b/timeline.summarized.json \
  --output-dir work/library \
  --title "여행 날짜 또는 장소"
```

상세 계약과 실제 음식 시퀀스 결과는 [다중 영상 라이브러리 문서](docs/video-library.md)에 있습니다.

## Codex SDK 편집 서비스

검토 라이브러리를 로컬 API와 함께 열면 오른쪽 `AI Editor`에서 장면 검색, 근거 조회, 편집 초안 생성과 수정 요청을 할 수 있습니다. 에이전트는 원본 파일이나 셸에 편집 권한을 받지 않고, 문서화된 도구만 제안합니다. 서버가 도구 입력을 검증하고 모든 편집 변경을 새 SQLite revision으로 기록합니다.

```bash
uv sync --group dev
uv run travel-video serve-editor \
  work/food-sequence/library/manifest.json \
  --state-dir work/editor-state \
  --agent-backend codex \
  --host 127.0.0.1 --port 8765
```

`http://127.0.0.1:8765`에서 Edit Desk를 엽니다. 모델 비용 없이 전체 UI·SSE·도구·revision 흐름을 검증하려면 `--agent-backend demo`를 사용합니다. API 계약은 `/openapi.json`, 실행 중인 도구 카탈로그는 `/api/contracts/tools`에서 확인할 수 있습니다.

## 다음 단계

- 로컬 시각 임베딩으로 같은 장소·대상을 의미 기반으로 묶어 과분할 감소
- 저비용 1차 샘플과 중요 구간 2차 샘플을 분리한 적응형 샘플링
- SQLite 작업 큐와 재시작 가능한 배치 처리
- HTML 리뷰 화면에 그룹 합치기·나누기·별점 저장 기능 추가
- `cam2-0825`의 43개·1.51시간으로 Phase 1 확대
- 최종 편집기와 개인 소장용/YouTube/Shorts의 우선순위 결정
