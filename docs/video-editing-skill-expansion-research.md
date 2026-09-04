# 비디오 편집 스킬 확장 조사

조사일: 2026-09-03

범위: 현재 `video-edit-plan/v1`과 FFmpeg 렌더러를 유지하면서 여행·액션캠 편집의 자동화 수준과 결과 품질을 높일 수 있는 로컬 도구, 오픈소스 코드베이스, Agent Skill을 검토한다.

## 결론

지금 필요한 것은 효과 목록을 계속 늘리는 것보다 다음 세 층을 분리해 강화하는 일이다.

1. **기계 신호층**: 장면 경계, 음성·무음, 움직임, 인물, 시각적 품질, 음악 박자를 저비용으로 추출한다.
2. **편집 의미층**: J/L 컷, B-roll, 스피드 램프, 음악과 대화의 관계를 소스 범위가 보존되는 edit plan으로 표현한다.
3. **렌더·교환층**: FFmpeg 프리뷰와 마스터를 만들고, OTIO/FCPXML/Resolve로 같은 결정을 넘기며 자동 QC한다.

우선 구현할 조합은 **현재 FFmpeg 기능 확장 + Apple Vision 보조 분석기 + OpenTimelineIO 내보내기**다. 모두 로컬에서 실행할 수 있고, 모델에 전체 영상을 보여주지 않아도 된다. PySceneDetect와 Auto-Editor는 렌더 엔진이 아니라 편집 후보 신호를 추가하는 선택적 어댑터로 쓰는 것이 좋다.

## 현재 환경과 코드 기준선

- Mac mini: Apple Silicon, macOS 26.6.2, Swift 6.2.3.
- FFmpeg 8.1.2에는 `sidechaincompress`, `loudnorm`, `arnndn`, `afftdn`, `anlmdn`, `deshake`, `blackdetect`, `blurdetect`, `freezedetect`, `signalstats`, `libvmaf`, `ssim`, `lut3d`, `tonemap`, `overlay`, `zoompan`이 이미 포함되어 있다.
- `gyroflow`, `auto-editor`, `scenedetect`, `exiftool`, `mpv` CLI는 현재 설치되어 있지 않다. 이번 조사에서는 설치하지 않았다.
- 현재 Phase 1은 5초 간격 저해상도 프레임, 평균 해시, 밝기·대비·선명도 휴리스틱으로 구간과 대표 프레임을 만든다. 빠르고 토큰 효율적이지만 인물 표정, 구도, 시선 집중 영역, 실제 컷에는 둔감하다.
- 확인한 원본 표본은 `DJI OsmoPocket3`이며 `djmd`, `dbgi`, timecode 데이터 트랙을 포함한다. 프록시에 없는 이 메타데이터는 반드시 원본에서 별도로 색인해야 한다.

## 기능 우선순위

### P0 — 외부 의존성 없이 바로 추가

#### 1. BGM 트랙과 자동 더킹

- edit plan에 사용자가 제공한 음악 파일, 음악 in/out, 루프, 페이드, 기본 게인을 기록한다.
- 대화/VAD 구간 또는 음성 트랙을 sidechain으로 사용해 `sidechaincompress`로 BGM을 자동으로 낮춘다.
- 대화가 없을 때만 음악이 회복되도록 attack/release를 프리셋화한다.
- 최종 출력은 2-pass `loudnorm` 또는 측정값을 manifest에 보존하는 결정적 정규화를 사용한다.

여행 영상의 체감 완성도를 가장 빨리 높이는 기능이고 현재 FFmpeg만으로 구현 가능하다.

#### 2. J-cut/L-cut과 독립 오디오 편집

- 현재처럼 영상과 오디오가 항상 같은 clip 경계를 공유하면 자연스러운 대화 편집에 한계가 있다.
- `video_in/out`과 `audio_in/out`, `audio_offset`, `audio_transition`을 독립적으로 표현한다.
- 다음 장면 소리를 먼저 들려주는 J-cut, 이전 장면 소리를 다음 화면까지 유지하는 L-cut을 지원한다.
- 이 기능부터는 단일 clip 배열보다 video/audio track을 가진 `video-edit-plan/v2`가 더 안전하다.

#### 3. 구간 내부 스피드 램프와 프리즈 프레임

- `speed_segments`를 source-relative 구간 목록으로 저장한다.
- 각 구간의 속도, easing 의도, 피치 보존 여부를 명시하고 출력 시간 매핑을 manifest에 남긴다.
- `freeze_frame`은 소스 timestamp와 출력 duration을 기록한다.
- 자막과 오디오 이벤트는 렌더 전에 source-to-output time map을 통해 다시 배치한다.

#### 4. B-roll, PIP, 이미지와 Ken Burns

- 주 트랙 위에 B-roll을 얹되 원래 대화 오디오는 유지할 수 있어야 한다.
- 이미지·지도·메뉴판·티켓 같은 still asset에 `zoompan` 기반 Ken Burns를 적용한다.
- PIP는 위치, 크기, 모서리, 그림자, 안전 영역을 프리셋으로 제한한다.
- 자유로운 FFmpeg filter 문자열 대신 허용된 typed operation만 edit plan에 넣는다.

#### 5. 색상·전달 프리셋

- clip별 `lut3d`, 노출/대비/채도, 화이트밸런스 보정을 허용된 범위로 제공한다.
- HLG/HDR 입력은 색공간 메타데이터를 먼저 검사하고 명시적인 SDR tone map 경로를 사용한다.
- `youtube-4k`, `youtube-1080`, `shorts-1080x1920`, `review-small`, `prores-master` 같은 출력 프리셋을 둔다.
- 원본의 color primaries, transfer, matrix, range를 probe와 render manifest에 보존한다.

#### 6. 자동 렌더 QC

현재 설치된 FFmpeg 필터만으로 다음 검사를 만들 수 있다.

- 시작·끝의 의도하지 않은 검은 프레임: `blackdetect`
- 멈춘 프레임: `freezedetect`
- 과도한 흐림: `blurdetect`
- 장시간 무음과 오디오 클리핑: `silencedetect`, `astats`
- 목표 음량과 true peak: `ebur128`, `loudnorm` 측정값
- 노출·색 범위 이상: `signalstats`
- 프록시/원본 파생물의 회귀 비교: `libvmaf`, `ssim`, `psnr`
- 출력 duration, 해상도, fps, audio stream, 자막 수, 컷 경계 샘플 프레임 검증

QC 결과는 `video-edit-qc/v1` JSON으로 보존하고, 실패가 있으면 원본 마스터 승격만 차단한다.

## P1 — 로컬 분석 품질을 크게 올리는 도구

### Apple Vision 보조 분석기

현재 Mac에 가장 잘 맞는 신규 구성 요소다. Swift helper가 저해상도 샘플 프레임을 읽고 작은 JSON만 반환하게 한다.

- `VNCalculateImageAestheticsScoresRequest`: 대표 프레임 후보의 미학 점수와 utility 여부.
- `VNGenerateAttentionBasedSaliencyImageRequest`: 세로 크롭의 초기 anchor와 중요한 피사체 영역.
- 얼굴·사람 사각형/landmark/pose: 사람이 화면에 있는 구간, 얼굴 크기와 중앙 이탈, 큰 몸동작 후보.
- face capture quality: 표정이 또렷하게 잡힌 프레임의 보조 점수.
- `VNGenerateImageFeaturePrintRequest`: 비슷한 프레임·중복 장면 거리 계산.
- OCR: 간판, 메뉴, 장소명과 가격을 검색 색인에 추가.

권장 출력은 프레임별 원시 landmark가 아니라 `aesthetic`, `utility`, `saliency_box`, `faces`, `people`, `feature_cluster`, `ocr_excerpt` 같은 compact signal이다. Codex는 상위 후보 contact sheet만 본다.

### PySceneDetect 0.7.x

현재 고정 간격 샘플을 대체하지 않고 실제 컷과 페이드를 보충한다.

- `AdaptiveDetector`는 주변 프레임의 rolling average를 사용해 빠른 카메라 움직임에서 거짓 컷을 줄인다.
- Content, histogram, perceptual hash, threshold detector를 조합할 수 있다.
- 통계 CSV를 캐시할 수 있고 OTIO/FCP 출력도 제공한다.

액션캠의 긴 원테이크에서는 컷이 거의 없으므로 PySceneDetect 결과만으로 scene을 만들지 않는다. `실제 컷 + VAD + 움직임 변화 + 최대 구간 길이`를 결합한 boundary proposal로 사용한다.

### Auto-Editor

Auto-Editor의 좋은 부분은 렌더보다 **시간축 label 모델**이다.

- audio, motion, black frame, subtitle word/regex 신호를 boolean expression으로 조합한다.
- 구간을 최대 255개 label로 분류하고 label별 keep/cut/speed 같은 action을 둘 수 있다.
- Resolve, Final Cut Pro, Premiere, Kdenlive, Shotcut export를 제공한다.
- 공식 Agent Skill도 제공한다.

우리 파이프라인에서는 Auto-Editor가 만든 active/inactive/label ranges를 `candidate_intervals.json`으로 받아 Phase 1 신호와 합치는 편이 안전하다. 최종 source range와 렌더는 기존 edit plan이 담당한다. 코드가 Unlicense이지만 최신 배포판의 렌더 제한과 구현 언어 Nim을 고려하면 전체를 vendor하기보다 CLI 어댑터 또는 개념 재구현이 낫다.

### DJI timed metadata와 ExifTool

현재 원본에 실제로 `djmd`와 `dbgi`가 있다. ExifTool은 DJI protobuf timed metadata에서 Osmo Pocket 3용 `dvtm_PP-101`을 인식한다.

가능한 활용:

- 촬영 시각·timecode 정합성과 split clip 연속성 검증
- 카메라 자세·짐벌 변화가 추출되면 pan/tilt/정지 구간 신호화
- 노출·초점·카메라 설정을 장면 품질 판단과 색보정 경고에 사용
- 프록시 생성 시 사라지는 데이터 트랙의 존재와 digest를 원본 manifest에 보존

먼저 ExifTool JSON 출력에서 Pocket 3 파일에 실제로 어떤 timed tag가 나오는지 2~3개 파일로 POC해야 한다. `dbgi` 전체를 Git이나 모델 입력으로 복사하지 않는다.

### OpenTimelineIO

`video-edit-plan`을 편집기 독립적인 타임라인으로 전달하는 기준 포맷으로 사용한다.

- clip, source range, track, transition, marker, metadata를 표현한다.
- OTIO core는 `.otio`를 lossless canonical interchange로 취급하며, FCPXML·AAF·CMX EDL 등은 별도 adapter plugin이다.
- 다른 포맷은 표현력이 달라 손실될 수 있으므로 export report에 누락된 speed ramp, overlay, caption style을 기록한다.

권장 구조는 `edit plan -> OTIO -> 목적별 adapter`이며, FFmpeg 렌더는 계속 edit plan에서 직접 수행한다.

## P2 — 특정 결과물에 선택적으로 사용

### DeepFilterNet과 FFmpeg 음성 정리

- 1차는 현재 포함된 `highpass + afftdn/anlmdn` 프리셋으로 바람·저주파·지속 잡음을 약하게 줄인다.
- FFmpeg의 `arnndn`은 RNNoise 모델 파일이 별도로 필요하지만 저비용 로컬 음성 정리 후보가 된다.
- 대화가 정말 중요한 선택 구간에만 DeepFilterNet을 적용한다. 전체 10일치 음성을 먼저 처리하지 않는다.
- 원본 오디오와 처리본을 모두 유지하고 denoise 강도를 manifest에 남긴다.

DeepFilterNet은 MIT/Apache-2.0이고 CLI와 Rust/Python 구현이 있다. Demucs는 음악 stem 분리에 강하지만 현재 upstream이 사실상 유지보수 중단 상태이며 여행 대화 잡음 제거의 기본 도구로는 과하다.

### Librosa 음악 박자 분석

- 사용자가 제공한 BGM에서 onset envelope, BPM, beat timestamp를 추출한다.
- 컷을 무조건 박자에 강제하지 않고, 이미 선택된 컷을 허용 범위 안에서 가까운 beat로 snap한다.
- beat grid와 snap delta를 artifact로 보존한다.

Librosa는 ISC 라이선스다. Aubio도 기능은 맞지만 GPL-3.0이고 릴리스 주기가 오래되어 우선순위가 낮다.

### LosslessCut 연동

- 분석된 장면을 LosslessCut CSV/`.llc` 프로젝트로 내보내 사람이 초고속으로 selects를 검토한다.
- 승인된 범위를 무손실로 별도 보관하거나 전달할 때 유용하다.
- 핵심 편집 렌더러로 합치지는 않는다. keyframe 밖의 정확한 컷, 효과, 크롭, 자막에는 재인코딩이 필요하다.

LosslessCut은 GPL-2.0이므로 private repo 안에 코드를 복사하지 않고 CSV/HTTP/CLI 경계를 통한 외부 도구로만 연동한다.

### Remotion과 공식 Agent Skills

Remotion은 여행 영상의 컷 편집 엔진보다 다음 용도에 적합하다.

- 여행 경로 지도 애니메이션
- 오프닝, 날짜·장소 카드, lower third
- 키네틱 자막과 소셜용 모션 그래픽
- HTML/React 기반 편집 가능한 템플릿

공식 skills에는 create, markup, studio, render, maps, captions가 있다. 개인 또는 3인 이하 조직은 무료 범위가 있지만 source-available 특수 라이선스이므로 서비스화 전에 다시 확인한다. 별도의 `motion-graphics` companion skill과 alpha overlay 출력으로 두고 FFmpeg 편집 스킬에 직접 결합하지 않는 편이 낫다.

### 다른 video-editor Agent Skill에서 차용할 아이디어

`lainshao/video-editor`의 다음 설계는 개념 차용 가치가 있다.

- cue plan 텍스트 검토 -> HTML spec preview -> 최종 렌더의 단계별 review gate
- integrated MP4와 ProRes 4444 alpha overlay를 같은 cue에서 이중 출력
- 테마 토큰, safe zone, cue density를 스킬 reference로 분리
- libass가 없어도 Pillow PNG overlay로 자막을 만드는 방식

현재 우리 스킬의 inspectable edit plan과 결합하면 렌더 전에 비용이 싼 단계에서 스타일과 타이밍을 승인할 수 있다. 코드를 가져올 때는 MIT 고지와 제3자 의존성 라이선스를 별도 검토한다.

### Gyroflow

Gyroflow는 gyro 기반 안정화, rolling shutter 보정, adaptive zoom, CLI/render queue를 제공한다. 다만 공식 DJI 지원 표에는 Action 2/4/5/6 등이 있고 **Osmo Pocket 3는 없다**. 현재 아카이브는 Pocket 3 표본이므로 우선 도입 대상이 아니다. 향후 지원 카메라 원본이 들어오면 외부 CLI adapter로 연결하고 GPLv3 코드는 repo에 vendor하지 않는다.

## 보류하거나 피할 선택

- **Ultralytics YOLO 직접 내장**: tracking 기능은 좋지만 기본 AGPL-3.0이다. private 제품 코드에 바로 넣지 않고 Apple Vision/MediaPipe를 우선한다. 꼭 필요하면 별도 프로세스·라이선스 검토가 선행되어야 한다.
- **GPL 코드 복사**: LosslessCut, Gyroflow, Aubio의 아이디어는 참고할 수 있지만 코드 vendor는 피하고 선택적 외부 CLI adapter로 격리한다.
- **모든 프레임에 무거운 모델 실행**: 분석 비용과 캐시가 커진다. 1차 저비용 신호로 후보를 줄인 뒤 선택 구간만 촘촘히 분석한다.
- **Remotion을 메인 컷 편집기로 교체**: 모션 그래픽에는 강하지만 현재 source time provenance와 원본 relink 구조를 버릴 이유가 없다.

## 권장 edit plan 진화

### 호환 추가가 가능한 v1 기능

- `speed_segments`
- `freeze_frames`
- top-level `music`
- clip `color`와 `denoise`
- output `preset`, `color_pipeline`, `qc_profile`

### 별도 v2가 필요한 기능

- video/audio 다중 track
- J/L cut과 독립 audio source range
- B-roll·PIP·alpha overlay
- nested sequence
- track-level gain, mute, ducking, automation curve

v2도 모든 media item에 `asset_id`, authoritative source path, source in/out, proxy ref, evidence IDs를 유지한다. v2에서 단순 일렬 편집은 v1으로 내려갈 수 있게 converter를 둔다.

## 토큰·연산 최적화 원칙

1. 도구 출력은 원시 프레임이나 landmark가 아니라 시간 구간과 compact score로 저장한다.
2. 모든 분석 artifact는 `source fingerprint + tool/version + parameters`로 캐시한다.
3. 저해상도 프록시와 1~5fps 이하 분석으로 후보를 만들고, 최종 후보만 원본 또는 고밀도 프레임으로 재확인한다.
4. Codex는 상위 5~8개 장면의 contact sheet와 대화 근거만 본다.
5. 자동 선택은 최종 판단이 아니라 `reason`, score component, source range가 있는 후보로 저장한다.
6. 렌더 결과보다 edit plan, time map, QC report, 사용한 tool versions를 장기 보존한다.

## 제안 구현 순서

1. FFmpeg 자동 QC와 output preset을 추가한다.
2. BGM + 대화 기반 자동 ducking을 추가한다.
3. Apple Vision helper로 aesthetics, saliency, face/person, feature print를 추출하고 대표 프레임·세로 anchor에 반영한다.
4. `video-edit-plan/v2` 초안을 만들고 J/L cut, B-roll/PIP를 구현한다.
5. speed ramp와 freeze frame을 source-to-output time map에 통합한다.
6. OTIO export와 export-loss report를 구현한다.
7. PySceneDetect와 Auto-Editor를 선택적 candidate-signal adapter로 A/B 테스트한다.
8. 선택 장면용 DeepFilterNet, BGM beat snap, LosslessCut export를 추가한다.
9. 모션 그래픽 요구가 생기면 Remotion companion skill을 별도 패키지로 만든다.

## POC 성공 기준

- 같은 영상에서 현재 대표 프레임 대비 사람이 눈을 감거나 흐린 프레임이 줄어든다.
- 세로 변환 시 주요 인물/음식/간판이 crop 밖으로 벗어나는 비율이 감소한다.
- 대화가 있는 장면에서 BGM이 자동으로 내려가고 전체 출력 loudness/peak가 목표 범위에 든다.
- J/L cut과 speed ramp 뒤에도 자막 source timestamp가 정확히 출력 timeline에 매핑된다.
- FFmpeg 프리뷰와 OTIO/Resolve import의 clip 순서·source in/out이 일치한다.
- Codex에 보내는 이미지 수는 늘지 않거나 줄고, 선택 이유를 compact artifact만으로 재현할 수 있다.

## 참고 자료

- FFmpeg filters: https://ffmpeg.org/ffmpeg-filters.html
- Apple Vision API: https://developer.apple.com/documentation/vision
- PySceneDetect: https://github.com/Breakthrough/PySceneDetect
- Auto-Editor: https://github.com/WyattBlue/auto-editor
- Auto-Editor Agent Skill: https://github.com/WyattBlue/auto-editor/blob/master/skills/auto-editor/SKILL.md
- OpenTimelineIO: https://github.com/AcademySoftwareFoundation/OpenTimelineIO
- ExifTool DJI tags: https://exiftool.org/TagNames/DJI.html
- DeepFilterNet: https://github.com/Rikorose/DeepFilterNet
- RNNoise: https://github.com/xiph/rnnoise
- Librosa: https://github.com/librosa/librosa
- LosslessCut: https://github.com/mifi/lossless-cut
- Gyroflow: https://github.com/gyroflow/gyroflow
- Gyroflow DJI support: https://github.com/gyroflow/docs.gyroflow.xyz/blob/main/getting-started/supported-cameras/dji.md
- Remotion Agent Skills: https://github.com/remotion-dev/remotion/tree/main/packages/skills
- 참고 video-editor Agent Skill: https://github.com/lainshao/video-editor
