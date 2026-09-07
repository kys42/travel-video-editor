# 영상 단위 요약과 다중 영상 라이브러리

## 목적

장면 분석이 끝난 뒤 결과를 파일 단위로 다시 종합하고, 여러 파일을 촬영 시각순으로 훑을 수 있게 합니다. 이 단계의 입력은 이미 검토된 장면 데이터뿐입니다. 원본 영상, 연락판과 스토리보드를 다시 열지 않으므로 영상 수가 늘어도 모델에 전달하는 정보량은 장면 수에 비례합니다.

```text
timeline.context-reviewed.json
  → 장면 설명·대화·특이 포인트만 요약 패킷으로 축소
  → 영상 제목·한 줄 설명·전체 서사·시간순 사건 생성
  → ID와 사건 순서를 기계 검증
  → timeline.summarized.json
  → 촬영 메타데이터순 다중 영상 웹 라이브러리
```

## 영상 요약 계약

`build-video-summary-packet`은 다음 정보만 남깁니다.

- 영상의 `asset_id`, 원본 이름, 길이와 촬영 시각
- 각 그룹의 원본 시작·종료 시각
- 검토가 끝난 장면 설명과 대화 요약
- 핵심 순간과 특이 포인트
- 장면 검토에서 이미 허용된 대표 프레임 후보 ID

작성할 `video-summary.json`의 핵심 필드는 다음과 같습니다.

```json
{
  "schema_version": "phase1-video-summary/v1",
  "asset_id": "...",
  "title": "영상 전체를 구분하는 짧은 제목",
  "one_line_summary": "한 문장 요약",
  "narrative_summary": "시작·진행·마무리를 담은 전체 서사",
  "chronological_events": [
    {
      "group_id": "G001",
      "headline": "사건 제목",
      "description": "이 장면에서 실제로 일어난 일"
    }
  ],
  "highlight_group_ids": ["G001"],
  "representative_sample_id": "F0001",
  "tags": ["장소", "행동"]
}
```

검증기는 모든 장면 그룹이 정확히 한 번, 원래 순서대로 들어갔는지 확인합니다. 대표 이미지는 기존 장면 검토에서 확인한 대표·핵심·특이 프레임만 선택할 수 있고, 하이라이트는 최대 세 그룹, 태그는 1–8개로 제한합니다. 자유롭게 만든 타임코드나 존재하지 않는 프레임은 병합되지 않습니다.

## 실행 순서

```bash
uv run travel-video build-video-summary-packet \
  timeline.context-reviewed.json \
  --output summary/video-summary-packet.json

# Codex 또는 사람이 packet만 읽고 summary/video-summary.json 작성
uv run travel-video validate-video-summary \
  summary/video-summary-packet.json \
  summary/video-summary.json

uv run travel-video merge-video-summary \
  timeline.context-reviewed.json \
  summary/video-summary-packet.json \
  summary/video-summary.json \
  --output timeline.summarized.json

uv run travel-video render-web timeline.summarized.json \
  --output-dir web
```

기존 개별 웹 타임라인에는 영상 전체 요약 밴드가 추가됩니다. 장면을 펼치기 전에 영상의 한 줄 설명, 전체 서사, 태그와 모든 사건의 순서를 먼저 파악할 수 있습니다.

## 여러 영상 렌더

```bash
uv run travel-video render-library \
  clip-01/timeline.summarized.json \
  clip-02/timeline.summarized.json \
  clip-03/timeline.summarized.json \
  --output-dir work/library \
  --title "여행 날짜 또는 장소" \
  --assets embed
```

완료된 작업 프록시가 있으면 `--proxy-root`를 함께 지정합니다. 라이브러리는 원본 파일명과 정확히 일치하고 중복되지 않는 `.mp4`만 찾아 출력 폴더의 `media/` 아래에 심볼릭 링크로 연결합니다. 영상 파일 자체를 저장소로 복사하지 않습니다.

```bash
uv run travel-video render-library \
  work/food-sequence/*/timeline.summarized.json \
  --output-dir work/food-sequence/library \
  --proxy-root /Volumes/ExternalSSD/travel-video-editor/proxies/1080p-h264
```

프록시가 연결된 클립은 오른쪽 Source Monitor에서 전체 영상을 탐색할 수 있고, 타임라인의 장면을 선택한 뒤 전용 재생 버튼을 누르면 그 장면의 시작–끝 구간만 재생합니다. 프록시가 없거나 외장 SSD가 분리된 경우에는 대표 프레임으로 안전하게 폴백합니다.

라이브러리는 다음 정보를 데스크톱 중심으로 표시합니다.

- 촬영 시각순 영상 목록과 하루의 빠른 순서표
- 파일마다 대표 프레임, 제목, 한 줄 설명과 전체 서사
- 영상 내부의 모든 사건과 원본 기준 시작 시각
- 하이라이트 사건과 특이 포인트·편집 활용도
- 제목·서사·사건·태그·특이 포인트의 즉시 검색
- 기존 개별 장면 타임라인으로 이동하는 링크

이미지는 기본적으로 HTML에 내장되어 리모트나 다른 경로에서도 깨지지 않습니다. `--assets relative`는 같은 로컬 디렉터리 구조가 유지될 때만 사용합니다. 라이브러리 렌더 단계는 영상을 복사하거나 인코딩하지 않으며, 이미 완성된 프록시를 링크만 합니다.

정렬 키는 MP4의 `creation_time`, 그다음 원본 파일명입니다. 오프셋이 `+00:00`이면 화면에 `UTC`를 명시하며, 별도의 시간대 근거 없이 파일명의 숫자나 여행지를 보고 현지 시각으로 변환하지 않습니다. 날짜가 다른 파일도 같은 규칙으로 연속 표시할 수 있습니다.

## 음식 시퀀스 파일럿

`cam2-0825`의 음식 영상과 바로 앞뒤 파일을 함께 처리했습니다.

| 순서 | 영상 전체 요약 | 길이 | 사건 | 특이 포인트 |
|---:|---|---:|---:|---:|
| 1 | 불꽃 테이블에 자리 잡기 | 58초 | 2 | 2 |
| 2 | 푸드트럭에서 알래스카 소다 사기 | 3분 21초 | 5 | 5 |
| 3 | 첫 모금과 권태기 농담 | 36초 | 2 | 2 |
| 4 | 소스 고르고 피시 타코 공개 | 57초 | 3 | 3 |

합계는 영상 4개, 5분 51초, 사건 12개, 특이 포인트 12개입니다. 흐름은 화로가 있는 자리를 발견하는 장면에서 시작해 음료 탐색과 구매, 첫 시음과 농담, 소스 선택과 피시 타코 공개로 이어집니다. 대표 프레임 4장만 다중 영상 인덱스에 내장하고, 더 자세한 프레임·행동·대화는 개별 타임라인에서 주문형으로 봅니다.

## 재사용 경계

라이브러리 HTML은 읽기 전용 결과물이고 진실의 원천은 `timeline.summarized.json`입니다. 이후 Codex 스킬은 다음을 오케스트레이션하는 얇은 계층으로 만듭니다.

1. 각 영상의 Phase 1 캐시 확인 또는 생성
2. 장면별 1·2차 리뷰와 검증
3. 장면 결과만 이용한 영상 단위 요약과 검증
4. 선택한 영상들을 시간순 라이브러리로 렌더
5. 결과 링크, 처리량과 불확실성 보고

날짜 전체를 하나의 여행 서사로 다시 쓰거나 파일 사이 중복·연결 컷을 판단하는 작업은 별도 상위 단계로 둡니다. 지금 구현은 파일 경계를 보존하면서도 편집자가 실제 사건 흐름을 빠르게 파악하는 데 집중합니다.

### 편집 후보 표시

원본 경로·fingerprint와 editorial beats가 있는 요약 타임라인은 큰 장면을 펼치면
`장면별 세부 구간` 목록을 표시한다. 별도 LLM 호출 없이 `clip-candidate-library/v1`과
같은 생성 함수를 사용한다. 제목·요약·보정 대사·구간 내 대표 프레임·검토 이유를
보여주며 후보 내용도 장면 검색에 포함한다. 구간 안의 대표 프레임이 없으면
다른 시점의 이미지를 대신 넣지 않고 `프레임 없음`으로 표시한다.

- `후보 재생`: recommended_range를 원본 시간축으로 재생한다.
- `앞뒤 맥락 재생`: 이웃 대사를 포함한 context_range를 재생한다.
- Review의 Source Monitor와 Rough Cut의 미리보기에서 같은 구간을 사용한다.
- `구조화 후보`는 좋은 컷으로 승인됐다는 뜻이 아니다. 인물 관찰 건수도
  고유 인원 수가 아니며 우리 얼굴 제외 여부는 계속 미확인으로 표시한다.
- 후보 버튼은 검토용이다. 기존 큰 장면 선택 체크박스와 AI 편집 입력은 아직
  group 단위이며 후보 ID를 직접 골라 revision에 조립하는 연결은 후속 작업이다.

기존에 생성해 둔 HTML은 `render-library`로 다시 생성해야 새 목록이 나타난다.
beats 또는 원본 식별 정보가 없는 구형 타임라인은 기존 장면 UI를 유지한다.
프록시가 없으면 스틸과 안내를 표시하며 원본 영상은 변경하지 않는다.

후보가 있는 장면은 기계 샘플 구간·스토리보드를 기본적으로 접힌
`분석 근거 보기`에 둔다. 기계 샘플은 편집 장면이 아니며 설명이 반복될 수 있음을
명시한다. 이 표시 변경은 후보 경계를 다시 분석하거나 분할 품질을 보증하지 않는다.

장면 해석·보정 대화 전체·특이 포인트는 분석 근거 접기와 독립적으로 항상 표시한다.
