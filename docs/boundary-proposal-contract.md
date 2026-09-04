# 멀티모달 편집 경계 제안 계약

상태: **P0 producer·입력 계약·Golden 전달 구현됨**

관련 규범:

- [Golden 01 — 원본 영상에서 편집 근거까지](golden/01-raw-to-editorial-evidence.md)
- [Golden 02 — 편집 근거에서 하이라이트까지](golden/02-editorial-evidence-to-highlight.md)

## 목적

Phase 1의 약 5초 샘플과 5~30초 기계 세그먼트는 전체 시간축을 싸게 덮는
색인이다. 최종 편집 경계가 아니다. 실제 행동·반응·대화 주제의 시작과 끝은
STT, 음성 활동, FFmpeg 컷·움직임, Apple Vision 변화 신호를 source-relative
timestamp로 모은 `boundary-proposal/v1`에서 제안한다.

제안은 자동 컷이 아니다. Golden 통합 장면-대화 리뷰가 조밀한 storyboard,
원시 한·영 STT와 함께 제안을 한 번만 보고, 완결된 `editorial_beats`의 경계로
채택하거나 무시한다.

## 최소 JSON 계약

```json
{
  "schema_version": "boundary-proposal/v1",
  "asset_id": "clip--abc123",
  "source": {
    "path": "/Volumes/T7/trip/clip.mp4",
    "quick_fingerprint": "abc123"
  },
  "media": {"duration": 12.0},
  "policy": {
    "cluster_tolerance_seconds": 0.55,
    "neighbor_context_seconds": 5.0,
    "default_fine_pass": false,
    "vision_sample_interval_seconds": 0.333333
  },
  "summary": {"proposal_count": 1},
  "proposals": [
    {
      "proposal_id": "BP0001",
      "timestamp": 4.25,
      "timecode": "00:00:04.250",
      "confidence": 0.82,
      "primary_kind": "visual_change",
      "evidence": [
        {
          "timestamp": 4.2,
          "kind": "visual_change",
          "confidence": 0.82,
          "source_id": "FFMPEG-SCENE-0001",
          "details": {"score": 0.41}
        }
      ]
    }
  ]
}
```

`kind`는 현재 도구에 종속된 enum으로 고정하지 않는다. 권장 값은
`hard_cut`, `visual_change`, `motion_change`, `quality_change`, `speech_start`,
`speech_end`, `silence_gap`, `speaker_change`, `topic_change`, `ocr_change`,
`max_duration_guard`다. 새 신호도 ID·시각·confidence를 보존하면 추가할 수 있다.

## 검증 규칙

`validate-boundary-proposals`는 다음을 fail-closed로 검사한다.

- timeline과 `asset_id`, Unicode 정규화 source path, quick fingerprint, duration 일치
- proposal ID와 timestamp가 고유하고 시간순이며 source duration 안에 있음
- 모든 proposal에 하나 이상의 evidence와 `primary_kind` 근거가 있음
- evidence timestamp가 같은 proposal의 clustering 허용 범위 안에 있음
- 장면 packet의 앞·뒤 proposal은 coarse 경계에서 기본 5초 이내만 전달됨
- confidence가 `0..1`, source ID가 proposal 안에서 고유함
- `summary.proposal_count`가 실제 배열 길이와 일치함

```bash
uv run travel-video validate-boundary-proposals \
  boundaries/proposals.json timeline.reviewed.json
```

raw signal JSON과 fusion 결과는 서로 덮어쓰지 않는다. 생성기는 사용한
도구·버전·설정과 source ID를 `details` 또는 별도 run manifest에 남긴다.

## 실제 producer

시각 Phase 1과 Apple STT가 끝난 뒤 다음 명령이 로컬 신호 추출부터 proposal
생성까지 실행한다. 프록시를 쓸 때는 identity time mapping을 검증한 lineage를
반드시 함께 넘긴다.

```bash
uv run travel-video build-boundary-proposals \
  /absolute/path/to/proxy.mp4 \
  timeline.reviewed.json \
  transcript.apple.json \
  --lineage /absolute/path/to/source-lineage.json \
  --output-dir boundaries
```

기본 출력은 다음처럼 단계별로 보존한다.

```text
boundaries/
├── run-intent.json
├── run.json
├── signals/
│   ├── apple-stt.json
│   ├── ffmpeg.json
│   ├── apple-vision.raw.json
│   └── apple-vision.json
└── proposals.json
```

- Apple STT adapter는 detector-gated transcriber activity 시작·끝, 무음 구간,
  서로 다른 locale이 동의한 raw segment 경계를 보존한다. raw locale 문장은
  여전히 정답이 아니며 실제 발화·주제는 통합 리뷰가 결정한다.
- FFmpeg adapter는 native scene timestamp와 약 3fps difference-motion 시계열을
  저장하고, adaptive threshold와 NMS로 hard-cut·motion-change 후보를 만든다.
- Apple Vision helper는 기본 약 3fps에서 인접 FeaturePrint 거리, 미학,
  얼굴·인물 존재를 수집하고 OCR은 1fps로 제한한다. OCR와 인물 신호에는
  지속성/hysteresis를 적용하되 `apple-vision.raw.json`은 손대지 않는다.
- fusion은 시각 신호끼리 cross-signal NMS를 한 뒤 0.55초 이내 evidence를
  묶는다. hard cut과 STT의 정확한 timestamp를 primary anchor로 우선하고,
  약한 단일 evidence는 proposal로 승격하지 않는다.
- `run-intent.json`은 입력 fingerprint, 설정과 producer implementation digest를
  캐시 키로 기록하며 다른 조건의 output directory 재사용을 fail-closed한다.

## Golden 통합 리뷰 연결

```bash
uv run travel-video build-scene-dialogue-review-packet \
  transcript.apple.json timeline.reviewed.json \
  --visual-packet context/context-review-packet.json \
  --boundary-proposals boundaries/proposals.json \
  --output scene-dialogue/review-packet.json
```

packet은 각 coarse group 내부 후보와 바로 앞·뒤 후보만 넣는다. 전체 신호
시계열을 모델에 반복 전달하지 않는다. 리뷰가 제안 시각을 beat 시작 또는 끝으로
사용하면 `source_boundary_proposal_ids`에 정확한 ID를 기록해야 한다. validator는
다음을 보장한다.

- proposal 기반 비격자 시각이 유효한 beat anchor가 됨
- 인용한 proposal이 실제 beat 시작 또는 끝과 일치함
- proposal 시각을 쓰면서 ID를 누락할 수 없음
- 모든 proposal을 컷으로 채택할 필요는 없음
- 기존 utterance/caption coverage와 대화 완결성 검사는 그대로 유지됨

병렬 리뷰는 이전과 같이 완전한 coarse group 단위로만 나눈다. proposal은 그룹을
더 많이 만드는 입력이 아니라, 한 번의 dense review가 더 정확한 beat를 만들기
위한 작은 숫자·ID 근거다.

## Vision 밀도와 역할

운영 기본 목표는 약 **3fps**(`0.333333초`)다. 기존 벤치마크는 2fps와 4fps를
측정했으며 정확한 3fps 수치는 아직 측정하지 않았다. 따라서 3fps는 측정값을
꾸며낸 결론이 아니라 다음 역할 분리에 따른 운영 정책이다.

- FFmpeg와 STT span이 컷·음성의 정밀 timestamp를 제공
- Vision은 FeaturePrint, 미학, 인물·얼굴, OCR 같은 의미·품질 변화 후보를 제공
- 모델은 후보 주변의 선택된 frame/storyboard만 봄
- 후보 주변 원본 FPS 정밀 재검사는 기본 비활성이고, 근거가 충돌한 소수 예외만 허용

프로덕션 기본값을 확정하기 전에 음식 주문, 빠른 인물 반응, 정적 자연 풍경 최소
세 종류에서 3fps 처리량과 경계 recall을 기록한다.

## 계층 용어 정리

별도 `shot/event/chapter` 정본을 Golden 01에 중복 도입하지 않는다.

- `shot`: hard cut·구도 변화 같은 boundary evidence 또는 `visual` beat
- `event`: Golden 01의 `editorial_beat`
- `chapter`: Golden 02가 여러 beat를 묶어 만드는 story outline 구간

이 매핑으로 영상별 정본은 계속
`timeline.dialogue-reviewed.summarized.json` 하나를 유지하고, 하이라이트 단계가
같은 beat/utterance/caption ID를 그대로 소비한다.

## 구현 범위와 남은 보정

현재 P0는 Apple STT/FFmpeg/Apple Vision signal producer, timestamp
clustering/NMS, lineage validator, packet 전달과 beat citation 검증까지
연결한다. 남은 작업은 계약 구현이 아니라 calibration이다.

1. 음식 주문·빠른 인물 반응·정적 자연 풍경 3종 benchmark 확대
2. hard-cut, motion, FeaturePrint threshold의 장르별 precision/recall 기록
3. 실제 통합 리뷰에서 채택/기각된 proposal ID를 이용한 threshold feedback
