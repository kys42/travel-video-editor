# 도구·MCP·스킬 조사

조사일: 2026-09-01

후속 조사: 편집 기능, 자동 QC, Apple Vision, Auto-Editor, OTIO, 오디오 정리와 모션 그래픽 후보의 상세 비교는 [비디오 편집 스킬 확장 조사](video-editing-skill-expansion-research.md)를 참조합니다.

## 결론

초기 구현은 `FFmpeg/ffprobe + 로컬 STT + 대표 프레임 색인 + OpenTimelineIO` 조합을 권장합니다. 이 방식은 대용량 원본을 외부 서비스에 업로드하지 않고도 인벤토리, 검색, 러프컷, 자막, 편집기용 타임라인을 만들 수 있습니다.

MCP는 다음 두 경우에만 우선순위가 높습니다.

- 반복적인 FFmpeg 작업을 자연어 도구 호출로 표준화하려는 경우
- DaVinci Resolve나 Premiere Pro의 실제 타임라인을 에이전트가 제어해야 하는 경우

## 로컬 기본 도구

### FFmpeg와 ffprobe

역할:

- 코덱·해상도·프레임레이트·오디오 등 미디어 정보 수집
- 하드웨어 가속 프록시 생성
- 음성 추출
- 프레임·연락판 생성
- 트림·합치기·크롭·자막·음량 평준화
- 러프컷 미리보기 렌더

FFmpeg는 `silencedetect`, `silenceremove`, `loudnorm`, `drawtext`, `crop`, `deshake` 등 필요한 필터를 제공합니다.

- 공식 문서: https://ffmpeg.org/ffmpeg-filters.html
- 현재 환경: `/opt/homebrew/bin/ffmpeg`, 버전 8.1.2

### MLX Whisper

Apple Silicon에서 로컬 음성 전사를 수행하는 기본 후보입니다. 단어 타임스탬프 출력을 지원하므로 SRT 생성과 발화 기준 컷 후보 추출에 유용합니다.

- 저장소: https://github.com/ml-explore/mlx-examples/tree/main/whisper
- 문서: https://github.com/ml-explore/mlx-examples/blob/main/whisper/README.md
- 장점: 로컬 처리, 원본 음성 비공개, Apple Silicon 적합
- 주의: 아직 현재 환경에 설치하지 않음

### WhisperX

단어 단위 강제 정렬과 화자 분리가 필요한 경우의 고급 후보입니다.

- 저장소: https://github.com/m-bain/whisperX
- 장점: 정밀한 단어 타임스탬프, VAD, 화자 분리
- 주의: 모델과 PyTorch/pyannote 의존성이 무겁고 Apple Silicon 환경에서 사전 검증 필요

### OpenAI 음성 전사 API

로컬 STT 결과의 정확도를 보완하거나 화자 분리가 필요할 때 선택적으로 사용합니다.

- 공식 문서: https://developers.openai.com/api/docs/guides/speech-to-text
- 일반 전사 권장 모델: `gpt-transcribe`
- 화자 분리: `gpt-4o-transcribe-diarize`와 `diarized_json`
- 단어/구간 타임스탬프와 기존 SRT/VTT 의존 워크플로: `whisper-1`
- 파일 크기 제한: 파일당 25 MB이므로 긴 음성은 문장 경계를 고려해 분할해야 함
- 정책 제안: 기본은 로컬, 사용자가 승인한 프록시 음성만 API 전송

### PySceneDetect

장면 전환 탐지와 장면 대표 이미지 생성을 담당합니다.

- 문서: https://www.scenedetect.com/docs/latest/
- 저장소: https://github.com/Breakthrough/PySceneDetect
- 주의: 액션캠의 긴 원테이크는 내부 컷이 적기 때문에 장면 탐지만으로 충분하지 않음
- 권장 보완: 5~15초 주기 샘플링, 움직임 변화, 음성 시작/끝, GPS 위치 변화 병행

### OpenTimelineIO

AI가 만든 편집 계획을 특정 편집기에 종속되지 않는 형태로 저장합니다.

- 문서: https://opentimelineio.readthedocs.io/en/latest/
- 저장소: https://github.com/AcademySoftwareFoundation/OpenTimelineIO
- 포함 정보: 클립, 트랙, 타이밍, 전환, 마커, 메타데이터
- 역할: 내부 `edit-plan.json`을 OTIO로 변환하고 필요 시 EDL/FCPXML 등으로 어댑트

### GoPro GPMF

GoPro 촬영물이라면 MP4 내부 GPS·자이로·가속도·카메라 정보를 추출할 수 있습니다.

- 공식 저장소: https://github.com/gopro/gpmf-parser
- 활용: 지도 경로, 속도·고도 오버레이, 위치 기반 장면 묶기, 흔들림 분석 보조
- 주의: 카메라 모델별 GPS 센서와 메타데이터 차이 확인 필요

## MCP와 외부 서비스

### VideoDB Skill/MCP

VideoDB는 업로드, 음성·장면·객체·OCR 분석, 자연어 검색, 클립·자막·오버레이 생성과 스트리밍을 제공하는 서버 측 영상 플랫폼입니다.

- Skill: https://github.com/video-db/skills
- MCP Toolkit: https://github.com/video-db/agent-toolkit
- 적합한 경우: 빠르게 전체 기능을 시험하거나, 여러 장치에서 검색 가능한 클라우드 영상 카탈로그가 필요한 경우
- 부적합한 경우: 원본을 외부에 둘 수 없거나, 업로드 대역폭·저장 비용·서비스 종속성을 피해야 하는 경우
- 권장 시험: 원본 전체가 아닌 하루치 720p 프록시

### FFmpeg MCP Video Editor

스키마가 있는 로컬 편집 도구, 비동기 작업 큐, 얼굴 추적 크롭, 자막, 장면 탐지와 타임라인 렌더를 표방합니다.

- 저장소: https://github.com/AbyAbyss/ffmpeg-mcp-video-editor
- 장점: 셸 명령 직접 생성을 줄이고 경로 제한·작업 상태·취소 기능을 제공
- 위험: 조사 시점 기준 신생 커뮤니티 프로젝트이므로 기능과 테스트 수는 README의 주장으로 간주해야 함
- 도입 조건: 코드 감사, 허용 경로 제한, 소형 복사본으로 검증, 원본 디렉터리 쓰기 금지

FFmpeg CLI를 이미 직접 사용할 수 있으므로 초기 파일럿의 필수 의존성은 아닙니다.

### DaVinci Resolve MCP

Resolve의 미디어 풀, 편집, 컬러, Fusion, Fairlight, 렌더링 제어를 표방하는 비공식 MCP입니다.

- 저장소: https://github.com/CiprianSpiridon/davinci-resolve-mcp
- 장점: 러프컷 이후 실제 NLE 타임라인 반영 가능성
- 위험: 조사 시점 기준 초기 단계의 비공식 프로젝트이며 Resolve 버전·에디션별 API 차이가 큼
- 도입 조건: Resolve 선택 후, 복제 프로젝트에서만 사용하고 저장·삭제·렌더 동작에 승인 게이트 적용

### Premiere Pro 연동

현재 확인한 공개 Premiere MCP들은 초기 단계이고 구현 범위에 비해 검증 정보가 부족했습니다. Premiere를 최종 편집기로 확정한다면 비공식 MCP보다 Adobe 공식 UXP 기반의 작은 브리지를 구축하는 경로를 우선 검토합니다.

- Adobe Premiere 개발자 문서: https://developer.adobe.com/premiere-pro/
- UXP 소개: https://developer.adobe.com/premiere-pro/uxp/introduction/

### 설치 가능한 일반 플러그인

- Runway: 생성형 연결 장면, 스타일 실험 등 선택적 후반 작업 후보. 수집·색인의 핵심 도구는 아님
- Dropbox: 대용량 파일 전달과 검토본 공유 후보. 영상 이해나 편집 엔진은 아님
- 현재 설치된 ImageGen 스킬: 유튜브 썸네일, 타이틀 카드, 앨범 커버 같은 파생 이미지 제작에 활용 가능

플러그인은 아직 설치하거나 연결하지 않았습니다. 실제 노출 도구와 권한을 확인한 뒤 최소 범위로 연결해야 합니다.

## 추천 우선순위

1. FFmpeg/ffprobe로 읽기 전용 인벤토리와 프록시 생성
2. MLX Whisper로 로컬 STT 및 단어 타임스탬프 생성
3. PySceneDetect와 주기 샘플링으로 대표 프레임 생성
4. SQLite/JSONL 기반 검색 카탈로그 구축
5. FFmpeg 러프컷과 OpenTimelineIO 출력
6. 필요할 때만 Resolve MCP 또는 VideoDB 프록시 파일럿
7. 반복 워크플로가 안정화되면 프로젝트 전용 Codex 스킬 제작
