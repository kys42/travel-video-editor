# Apple Speech worker

macOS 26의 온디바이스 `SpeechAnalyzer` API를 Python 파이프라인에서 호출하기 위한 작은 Swift 실행 파일입니다.

```bash
swift build --package-path apple-speech -c release

apple-speech/.build/release/apple-speech capabilities \
  --output work/apple-speech/capabilities.json

apple-speech/.build/release/apple-speech transcribe \
  --input /absolute/path/to/16k-mono.wav \
  --locale ko-KR \
  --detector-sensitivity medium \
  --output work/apple-speech/ko-KR.json
```

`transcribe`는 [`SpeechDetector`](https://developer.apple.com/documentation/speech/speechdetector)와 지정 로케일의 `SpeechTranscriber`를 같은 분석기에 넣습니다. 출력에는 Detector 설정, 전사 결과와 대안, 구절별 타임스탬프와 신뢰도가 포함됩니다. 현재 macOS 26에서 Detector의 결과 스트림은 raw VAD 경계를 제공하지 않으므로 `detector_results`가 비어 있을 수 있지만, Detector는 Transcriber 게이팅에 사용됩니다.

일반 사용자는 이 worker를 직접 호출하기보다 저장소 루트의 `travel-video stt-apple` 명령을 사용합니다. Python 단계가 음성 추출, 한국어·영어 실행, 원시 응답 보존과 공통 스키마 정규화를 담당합니다.

## 첫 사용: 모델 점검과 설치

빌드 후 `capabilities`로 기기·언어 지원을 확인하고, `assets`로 실제 전사 모듈과 Detector가 사용할 자산 상태를 확인한다. 기본 호출은 읽기 전용이며 음성을 처리하거나 다운로드하지 않는다.

```bash
apple-speech/.build/release/apple-speech assets --locale ko-KR
apple-speech/.build/release/apple-speech assets --locale en-US

# 설치가 필요하고 사용자가 설치를 요청/승인한 경우에만 실행
apple-speech/.build/release/apple-speech assets --locale ko-KR --install
apple-speech/.build/release/apple-speech assets --locale en-US --install
```

`status_after: installed`가 준비 완료다. `--install` 실행 뒤에도 설치되지 않았으면 종료 코드1을 반환한다. 상태 점검은 `--install` 없이 실행하므로 미설치 상태도 JSON으로 보고하며 종료 코드0을 반환할 수 있다. 호출자는 종료 코드만 보지 말고 상태를 읽어야 한다.

자산은 [Apple AssetInventory](https://developer.apple.com/documentation/speech/assetinventory)가 다운로드·관리하며 앱 간 공유될 수 있다. `transcribe`는 기존 동작대로 필요한 자산을 설치할 수 있으므로, 단순 준비 상태 점검에는 `assets`를 사용한다.
