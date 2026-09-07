# 편집용 정보 추출과 재사용 클립 후보

상태: 선택 적용 가능한 v1. 기존 Golden 출력과 호환하며, `--clip-evidence`를 켠 신규 통합 리뷰에 추가 계약을 요구한다. 이 기능은 추출·검증·후보 JSON까지 구현한다. 라이브러리의 담기/조립 UI, PR #17의 후보 선택 도구, 얼굴 제외 최종 검수와 BGM 자동 믹서는 별도 소비 단계다.

## 역할과 실행 흐름

1. FFmpeg/Apple STT와 Apple Vision이 원본 좌표의 근거를 만든다.
2. 그룹별 통합 LLM 리뷰 **한 번**에 발화·자막·비트와 `clip_evidence`를 함께 작성한다.
3. validator가 ID, 시간, 발화 절단, 근거 모달리티와 인물 참조를 검사한다.
4. 병합 정본에서 `clip-candidate-library/v1`을 로컬 코드로 내보낸다. 이 단계에는 LLM 호출이 없다.

LLM 없이도 기존 비트를 후보로 펼칠 수 있지만, 사건 의미·인물 역할·상호작용·핵심 구간을 새로 판단한 것은 아니다. 기존 397개를 전수 재추출할 필요는 없다. 기존 후보에서 필요한 구간을 선택하고 새 추출 packet을 만들 때 원시 STT/프레임 캐시를 재사용한다. 기존 리뷰/자막을 새 모델 리뷰의 정답 입력으로 넣지 않는다.

## Apple Vision: 관찰만 기록

기존 약 3fps 경계 producer는 FeaturePrint·미학·얼굴 수·사람 수·OCR 외에 양성 얼굴/사람 검출의 실제 시각, 신뢰도, 박스를 보존한다.

- 원시 검출: `signals/apple-vision.raw.json`의 `faceObservations`, `personObservations`.
- 정규화: `proposals.json.person_observations` (`sampled-person-observations/v1`).
- 좌표는 [왼쪽 x, 아래 y, 너비, 높이]이며 화면 크기로 정규화한다. 원점은 왼쪽 아래다. [Apple 좌표 계약](https://developer.apple.com/documentation/vision/boundingboxproviding/boundingbox).
- 화면 밖으로 나간 박스는 보이는 교집합과 `frame_edge_truncated`를 남긴다. 원본 박스는 raw JSON에 보존한다.
- packet에는 제공된 근거 프레임의 샘플 간격 이내 검출만 전달한다. 전체 검출은 producer 산출물에 남아 있다.
- 검출 ID는 샘플의 근거 ID다. 추적 ID나 인물 신원이 아니다. 같은 사람도 여러 번 기록된다.
- 검출 없음/기존 count-only/샘플 사이 시간은 **얼굴 없음의 증거가 아니다**. 모든 출력의 coverage는 `sampled_only`다.
- 양성 검출은 관찰의 실제 timestamp가 없으면 실패한다. 전체 Vision 결과가 0프레임이면 정상 완료로 처리하지 않는다.

## LLM 출력 계약

정확한 필드·허용값과 validator는 [clip_evidence.py](../src/travel_video/clip_evidence.py)의 `contract()`와 `validate_clip_evidence()`가 소유하며 packet에도 내장된다.

각 비트의 `clip_evidence`는 다음을 포함한다.

| 필드 | 의미 |
|---|---|
| `core_range`, `event_closure` | 핵심 구간과 사건의 완결 여부. 기본 사용 구간은 원래 비트 전체다. 핵심 구간을 자동으로 짧은 컷으로 승인하지 않는다. |
| `steps` | setup/action/reaction/payoff/transition의 시간순 사건 구조 |
| `subjects` | 동물·장소·음식·활동·물체가 보이거나 언급되는 구간 |
| `people` | 비트 내부 인물 ID, 문맥상 역할, 프레임·검출 근거 |
| `speakers` | 발화 묶음, 화면 인물과의 불확실 연결. locale를 화자 ID로 간주하지 않는다. |
| `interactions` | 주문·안내·질문/응답 등 대화와 참여자 참조 |
| `audio` | 발화로 확인되는 음성 또는 명시적 문맥 추정 |
| `quality` | 샘플에서 관찰한 안정성·흔들림·가림·초점·노출 상태 |

주장마다 `visual`(프레임 관찰), `speech`(발화 내용), `inferred`(문맥 추정)를 구분한다. 화면에서 동물이 보인다는 주장에는 해당 시간대 프레임이 필요하고, 언급만 있는 것은 speech다. 검증은 근거 참조의 구조를 확인하며 이미지 속 실제 대상의 정답 여부까지 증명하지 않는다.

현재 packet은 시각 자료와 STT를 제공하므로 웃음·현장 음악·바람 소리 등을 직접 들었다고 확정할 근거가 없다. 이런 항목은 inferred 또는 빈 목록으로 남긴다. 실제 청취 근거를 제공하는 후속 경로 전에는 환경음 관찰로 승격하지 않는다. 사람 역할은 문맥 추정이며, ‘우리’라는 신원이나 서로 다른 영상의 동일 인물을 자동 확정하지 않는다.

빈 배열은 미평가/알 수 없음이다. 새로운 정보가 없는 것과 부재를 구분한다. 등장인물 단위 ID는 **비트 내부**에서만 유효하고, 상위 시스템은 candidate ID와 함께 사용한다.

신원을 모른다는 이유로 화면 인물 관찰까지 비우지 않는다. 편집에 관련 있고 분명히 보이는 사람은 익명 ID와 `role=unknown`으로 기록할 수 있다. 누가 말하는지 모르면 speaker 연결만 unknown으로 남긴다. 반대로 검출 박스 수를 곧바로 서로 다른 사람 수로 해석해서는 안 된다.

## 실행

기존 경계·시각 packet과 raw Apple STT를 재사용하여 새 packet을 만든다.

```bash
uv run travel-video build-scene-dialogue-review-packet \
  /path/to/transcript.apple.json /path/to/timeline.reviewed.json \
  --visual-packet /path/to/context-review-packet.json \
  --boundary-proposals /path/to/proposals.json \
  --visual-moments /path/to/visual-moments.json \
  --clip-evidence --output /new/run/review-packet.json

# 이 packet의 완전한 그룹을 한 번 리뷰한 후
uv run travel-video validate-scene-dialogue-review \
  /new/run/review-packet.json /new/run/review.json
uv run travel-video merge-scene-dialogue-review \
  /path/to/timeline.reviewed.json /new/run/review-packet.json \
  /new/run/review.json --output /new/run/timeline.dialogue-reviewed.json
uv run travel-video export-clip-candidates \
  /new/run/timeline.dialogue-reviewed.json --output /new/run/clip-candidates.json
```

Golden 배치 preparation에도 `--clip-evidence`를 전달할 수 있다. 해당 옵션은 packet 캐시 키에 반영된다. finisher는 enriched packet의 리뷰를 검증·병합한 뒤 `clip-candidates.json`을 자동 생성하고 state outputs에 경로·해시를 기록한다. 요약 모델 완료를 기다리는 상태에서도 후보 파일을 사용할 수 있다. 실패/변경 시 기존 실행 산출물을 덮어쓰지 않는 배치 경로를 따른다.

옵션을 생략하면 기존 계약으로 처리한다. 기존 JSON에 새 필드가 없다고 전수 재분석하거나 이전 결과를 새 리뷰로 위장하지 않는다. 기존 넓은 implementation digest의 단계별 분리는 이번 변경에서 수행하지 않았다.

## 라이브러리/AI 공통 소비 계약

- `candidate_id`: asset ID + beat ID 기반. `revision`: 후보 내용·근거·정책 상태의 지문.
- `source`, `recommended_range`, `context_range`, `core_range`, `references`: 원본과 시간/근거를 보존.
- `dialogue_context`: 후보와 겹치는 자막/발화, 바로 이전 2개와 다음 2개를 시간순으로 포함한다. 다른 coarse group에 속해도 제공하며, 경계를 횡단하는 발화는 overlapping에 남긴다. 미리보기 context 범위는 이 근거까지 포함하지만 기본 사용 범위는 바꾸지 않는다. 인접 대사가 수정되면 후보 revision도 바뀐다.
- `evidence`: 검증된 LLM 추가 정보 또는 `null`. 제목·설명·대화는 독립 검색 가능.
- `readiness`: `needs_review` 또는 `structured_candidate`. 후자는 구조화가 됐다는 의미이며 ‘좋은 장면’ 또는 ‘얼굴 제외 사용 가능’의 승인이 아니다.
- `review_reasons`: 사건 미완결, 이웃 경계 검토, 발화 절단, 추가 정보 없음 등.
- `privacy.owners`, `privacy.no_faces`: 항상 unknown으로 시작. 별도 인물 매핑/선택 구간 검수 후 소비 시스템이 확인 상태를 관리한다.
- `audio_policy`: unassessed. AI의 환경음 추정을 렌더 볼륨 정책에 곧바로 적용하지 않는다.

장면 묶음 아래 후보를 보여주고 기본 사용 범위만 재생한다. 사람이 담기와 AI가 고른 결과는 같은 candidate ID·revision·실제 선택 범위를 편집안에 고정해야 한다. 후보를 다시 추출해도 기존 편집안이 조용히 바뀌면 안 된다. 긴 대화나 반응은 완결성을 유지하고, 5초 고정 조각을 라이브러리의 편집 단위로 만들지 않는다.

## 다음 검증

12–20개 대표 장면에서 사건 회수율, 별도 트리밍 없이 담을 수 있는 비율, 잘린 질문·반응, 인물/화자 오연결, 검토 시간과 모델 입력량을 비교한다. 선택된 후보의 실제 청취·얼굴 노출 검수, 짧은/긴 변형 승인, UI/PR #17 연결은 이 공통 계약 위에 추가한다.
