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

- 2026-09-02: Phase 1 CLI 구현 및 실제 Osmo Pocket 3 영상 두 개 파일럿 완료
- 2.40 GiB·310초 원본을 62개 대표 프레임과 연락판 4장, 약 4.0 MiB 파생 자료로 변환
- 구조화 리뷰 검증을 거쳐 음식 트럭 영상 5개, 빙하 영상 4개 사건 그룹 생성
- 2026-09-02: 토큰 효율적인 전수 장면 타임라인 설계 추가
- 외장 T7의 Osmo 추정 원본을 읽기 전용으로 탐색했으며 상세 인벤토리는 아직 생성하지 않음
- 현재 Mac에서 Apple Silicon, FFmpeg 8.1.2, `ffprobe`, `uv` 사용 가능 확인
- `uvx`로 MLX Whisper를 로컬 실행했으며 small 모델 파일럿 완료; 일반적인 NLE 앱은 표준 경로에서 확인되지 않음
- 내부 데이터 볼륨 가용 공간은 약 50 GiB, 원본 T7은 약 253 GiB이므로 전체 프록시보다 주문형 파생 자료가 적합

## 원칙

1. 원본은 읽기 전용으로 다루고 수정·이름 변경·삭제하지 않는다.
2. 모든 추천 클립은 `asset_id + source_in + source_out`으로 원본까지 추적 가능해야 한다.
3. STT만 사용하지 않고 음성·영상·메타데이터를 함께 색인한다.
4. 대규모 처리 전에 하루치 또는 30~60분 분량으로 파일럿을 수행한다.
5. 자동 편집 결과는 검토 가능한 미리보기와 편집 계획을 먼저 만들고, 승인 후 최종 렌더한다.
6. MCP는 초기 분석의 필수 요소가 아니라 반복 작업 또는 NLE 제어가 필요할 때 선택적으로 연결한다.
7. 전수 타임라인과 편집 후보를 분리하고, 모델 호출은 중요도·중복도에 따라 계층화한다.

## 문서

- [도구·MCP·스킬 조사](docs/research-and-tooling.md)
- [권장 아키텍처](docs/architecture.md)
- [토큰 효율적인 전수 장면 타임라인](docs/token-efficient-scene-timeline.md)
- [Phase 1 구현과 파일럿 결과](docs/phase1-pipeline.md)
- [`kyungdoc/video-summary` 검토](docs/upstream-video-summary-review.md)
- [T7 원본 사전 점검](docs/source-assessment-2026-09-02.md)
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

원본과 대용량 프록시는 외장 SSD 등 별도 미디어 루트에 두고, 이 저장소에는 코드·설정·작은 메타데이터만 보관하는 구성을 권장합니다.

## Phase 1 실행

```bash
uv sync
uv run travel-video phase1 /absolute/path/to/video.mp4 \
  --stt mlx \
  --stt-model mlx-community/whisper-small-mlx
```

결과는 기본적으로 `work/phase1`에 생성됩니다. 자세한 출력과 리뷰 절차는 [Phase 1 문서](docs/phase1-pipeline.md)를 참고합니다.

## 다음 단계

- 로컬 시각 임베딩으로 같은 장소·대상을 의미 기반으로 묶어 과분할 감소
- 저비용 1차 샘플과 중요 구간 2차 샘플을 분리한 적응형 샘플링
- SQLite 작업 큐와 재시작 가능한 배치 처리
- HTML 리뷰 화면에 그룹 합치기·나누기·별점 저장 기능 추가
- `cam2-0825`의 43개·1.51시간으로 Phase 1 확대
- 최종 편집기와 개인 소장용/YouTube/Shorts의 우선순위 결정
