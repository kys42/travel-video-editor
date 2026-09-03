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
