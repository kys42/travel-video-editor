# 새 세션에서 날짜별 장면 라이브러리 만들기

현재 목표는 여러 편집 목적에 다시 쓸 **구체적인 의미 단위 클립과 풍부한 정보**다.
하이라이트를 미리 압축하거나 모든 영상을 5초씩 자르는 과정이 아니다.
계약은 [Golden 01](golden/01-raw-to-editorial-evidence.md),
[추가 근거 계약](clip-editorial-evidence.md), 표시 방식은 [장면 웹](video-library.md)을 따른다.

## 시작점과 저장 위치

- 프로젝트: `/Users/kys/projects/travel-video-editor`의 최신 `main`.
- 설치 스킬: `/Users/kys/.codex/skills/travel-video-pipeline/SKILL.md`.
  현재 Codex 스킬 폴더는 `/Users/kys/.claude/skills/travel-video-pipeline`로 연결돼 있다.
- 스킬 Git 저장본: `skills/travel-video-pipeline/`. 설치본과 함께 갱신한다.
- 날짜·원본·프록시·raw 캐시 목록:
  `work/golden-v3-preflight/story-days/story-days.json`.
- 검증된 coarse grouping 추가 목록: `work/golden-v3-preflight/grouping-overrides.json`.
- 기존 전체 분석: `/Volumes/ExternalSSD/travel-video-editor/analysis/`와 `work/full-batch/`.
  개별 경로는 위 manifest의 `assets[*].paths`를 사용한다.
- 신규 결과 권장 루트:
  `/Volumes/ExternalSSD/travel-video-editor/analysis/reusable-scene-library-v1/<story-day>/`.
  프로젝트 `work/scene-library-runs/`에는 날짜별 결과 manifest 경로와 검수 상태를 기록한다.

먼저 `AGENTS.md`와 `docs/media-locations.md`를 읽는다. 원본·프록시는 수정하지 않는다.
파일명 날짜, UTC 촬영일, 여행일은 다를 수 있으므로 `story_day` 분류를 재사용한다.
마운트가 달라졌으면 manifest의 lineage/fingerprint를 검증해 연결하고 경로만 추측해 대체하지 않는다.

2026-09-08 인계 점검에서는 대상 397개 모두 프록시·lineage·기계 timeline·Apple STT
파일이 존재하고 기존 grouping 검증을 통과했다. 기록은 프로젝트의
`work/scene-library-handoff-readiness.json`이다. 이는 당시 입력 점검이며 새 분석 완료나
원본 바이트 전수 재검증이 아니다. 실행할 때도 runner의 현재 입력 검증을 통과해야 한다.

## 기존 실험의 의미

- `work/day-comparison-20260820/`: 21개 자산의 기존/새 계약 비교. 분할과 설명이
  너무 거칠었던 초기 결과이므로 최신 라이브러리 추출 완료본으로 계산하지 않는다.
- `work/scene-library-rich-pilot/`: 2개 자산, 35개 후보의 최신 세분화 파일럿.
  화면 http://127.0.0.1:8794/ 및 `parent-quality-review.json`을 참고한다.
  음식·대화에서의 검토 사례이며 빙하·열차·동물 등 모든 유형의 품질 검증은 아니다.
- `work/highlights/faceless-discovery-v1/source-inventory.json`: 과거 편집용 위치 색인.
  새 모델 리뷰에 이전 답안을 넣기 위한 자료가 아니다.

파일럿의 `prepare.py`, `parent_finalize.py` 같은 로컬 스크립트는 특정 자산과 경로에
묶인 실험 기록이다. 날짜만 바꿔 전체를 처리하지 말고 아래 저장소 runner를 사용한다.
기존 packet은 생성 당시 프롬프트를 담으므로 코드만 업데이트해 재사용하면 새 지침이
반영되지 않는다. 신규 run에서 현재 코드로 packet을 다시 만든다.

## 1. 날짜 선택과 로컬 근거 준비

프로젝트 루트에서 실행한다. 아래 날짜는 예시이며 사용자 지정 범위로 바꾼다.
전체 여행을 맡았더라도 하루씩 완성하면 중단 후 이어가기와 검수가 쉽다.

```sh
cd /Users/kys/projects/travel-video-editor
export PYTHONPATH="$PWD/src"
export WORKING_MEDIA_ROOT=/Volumes/ExternalSSD/travel-video-editor
export LIBRARY_DAY=2026-08-21
export LIBRARY_RUN_ROOT="$WORKING_MEDIA_ROOT/analysis/reusable-scene-library-v1/$LIBRARY_DAY"

.venv/bin/python scripts/run_golden_v3_batch.py \
  work/golden-v3-preflight/story-days/story-days.json \
  --grouping-overrides work/golden-v3-preflight/grouping-overrides.json \
  --story-day "$LIBRARY_DAY" --clip-evidence \
  --output-root "$LIBRARY_RUN_ROOT/prepared" --jobs 2 --max-frames 16
```

runner는 raw STT·검증된 grouping을 재사용하고, 경계·시각 순간·스토리보드와
**새 통합 리뷰 packet**까지 준비한다. `--clip-evidence`를 빼면 인물·대상·원음 등의
추가 계약이 강제되지 않는다. Vision은 기본 약 3fps이고 편집 경계는 5초 격자가 아니다.
인물 양성 검출이 없는 구형 Vision 캐시는 필요하면 다시 생성한다. 전체 기계 분석이
항상 무비용으로 재사용되는 것은 아니며 입력·설정·코드 해시가 맞을 때만 재사용한다.

결과 `prepared/manifest.json`의 `records`와 각 `state`를 읽는다. 준비 단계의
`completed/reused`는 LLM 리뷰가 끝났다는 뜻이 아니다. `blocked/failed/locked`를
확인하고 필요한 자산만 해결한다. 같은 output root에 두 배치를 동시에 돌리지 않는다.
검증에 실패한 grouping을 무조건 accepted로 바꾸거나 오류를 무시하지 않는다.

## 2. 같은 그룹에서 장면·대화·클립을 함께 리뷰

준비 manifest가 가리키는 새 packet과 그 근거 프레임만 모델에 제공한다.
기존 review, 최종 자막, 이전 요약을 정답으로 제공하지 않는다.
완전한 coarse group 또는 asset 단위로 분담하며 한 그룹을 임의의 시간 조각으로 나누지 않는다.
모델 답안 경로는 다음과 같다.

```text
<LIBRARY_RUN_ROOT>/reviews/<asset_id>/scene-dialogue/review.json
<LIBRARY_RUN_ROOT>/reviews/<asset_id>/summary/video-summary.json
```

packet에 내장된 계약과 `dialogue-preservation/v1`을 따른다.
행동·대상·대화 주제·상호작용 단계·반응 변화에 근거해 비트를 세분화한다.
말이 없는 볼거리, 평범한 경험, 음식·동물·교통·쇼·풍경도 보존한다.
제목만 바꾼 반복 설명, STT 복사로 채운 관찰, 근거 없는 지명과 인물 역할을 만들지 않는다.

장면 설명에는 환경·대상·행동 순서·대화 변화·반응을 구체적으로 남긴다.
후보에는 `clip_evidence`의 근거·원본 시간·참조를 붙이고, 모르는 필드는 비워둔다.
등장인물은 비트 내부 익명 관찰이며 ‘우리’의 신원·같은 사람·얼굴 부재를 확정하지 않는다.
원음을 직접 듣지 않았다면 환경음·음악·웃음은 관찰 사실로 단정하지 않는다.

## 3. 검증·요약·후보 생성

```sh
.venv/bin/python scripts/finish_golden_v3_batch.py \
  "$LIBRARY_RUN_ROOT/prepared/manifest.json" \
  --review-root "$LIBRARY_RUN_ROOT/reviews" \
  --output-root "$LIBRARY_RUN_ROOT/finished" --jobs 2
```

finisher는 모델 답안을 작성하지 않는다. 없으면 `awaiting_review`, 장면 리뷰가
검증됐지만 영상 요약이 없으면 `awaiting_summary`를 남긴다. 후자의 record가 가리키는
`summary_packet`만 읽어 영상 요약을 위 규약 경로에 작성한 뒤 같은 명령을 재실행한다.
요약 대기 중에도 검증된 `clip-candidates.json`은 생성된다.
리뷰를 고치면 이전 답안과 변경 이유를 보존하고 finisher로 파생물을 다시 만든다.

종료 코드 0만으로 완료 판정하지 않는다. `finished/manifest.json`과
`finished/days/<story-day>/manifest.json`의 상태·누락·실패 개수를 읽는다.
각 성공 record의 `outputs`가 정본, 후보, 감사 결과와 개별 웹의 실제 위치다.
과거 `timeline.final.json`이나 파일명의 최신 수정 시각으로 정본을 선택하지 않는다.

## 4. 날짜별 편집 웹 만들기

완료 record만 원본 순서대로 모아 공용 라이브러리를 만든다. 아래 예시는 미완료가
남아 있으면 멈춘다. 일부만 미리 보여줄 때는 별도 출력에 ‘부분 결과’로 명시한다.

```sh
.venv/bin/python - <<'PY'
import json, os
from pathlib import Path
from travel_video.library import render_video_library
root = Path(os.environ['LIBRARY_RUN_ROOT'])
day = os.environ['LIBRARY_DAY']
data = json.loads((root / 'finished/days' / day / 'manifest.json').read_text())
records = data['records']
if not records or any(r['status'] not in ('completed', 'reused') for r in records):
    raise SystemExit('미완료 record를 먼저 확인하세요.')
paths = [Path(r['outputs']['timeline_dialogue_reviewed_summarized']) for r in records]
render_video_library(paths, root / 'library', title=f'{day} 장면 라이브러리',
    proxy_root=Path(os.environ['WORKING_MEDIA_ROOT']) / 'proxies/1080p-h264')
PY

.venv/bin/python -m travel_video.cli serve-editor \
  "$LIBRARY_RUN_ROOT/library/manifest.json" \
  --state-dir "$LIBRARY_RUN_ROOT/editor-state" --port 8795 --agent-backend demo
```

사용 중인 포트가 있으면 다른 포트를 고른다. 위 서버는 비용 없는 검토용 demo다.
웹은 프레임·시간·설명·대사·대상·검토 상태를 행에 표시하고, 클립을 펼치면
근거·인물·원음·품질이 즉시 보인다. 후보 재생과 앞뒤 맥락 재생을 확인한다.
후보 담기·revision 직접 조립은 별도 기능이며 데이터 생성 완료 조건에 섞지 않는다.

## 5. 하루 완료 판정과 인계

- 계획한 날짜의 eligible 자산 수와 준비·리뷰·요약·최종 결과 수를 대조한다.
- `policy_audit.status=pass`, 자막·발화·visual moment 보존, 원본 시간/ID 검증을 확인한다.
- `library_audit`의 긴 단일 후보·짧은 설명·반복 근거 신호를 확인한다. 신호가 있는
  그룹과 대표적인 조용한 볼거리·대화·빠른 반응을 프레임/짧은 재생으로 대조한다.
  후보 개수 증가나 검증기 통과만으로 의미 품질 합격을 선언하지 않는다.
- 검토 메모에 살릴 단위가 보존됐는지, 질문·반응이 잘리지 않았는지, 설명에 새로운
  구체 정보가 있는지, 남은 불확실성을 적는다. 문제 그룹만 재리뷰한다.
- 기존 날짜 정본과 비교 자료는 보존한다. 새 결과를 생성하는 것과 이전 결과를
  공식 교체하는 것은 따로 기록한다.
- 프로젝트 `work/scene-library-runs/`에 실행 코드 SHA, 옵션, 날짜, 입력 manifest,
  prepared/reviews/finished/library 경로, 상태별 수, 검수 메모와 재시작 명령을 남긴다.
  실제 토큰·비용 기록이 없으면 파일 크기로 비용을 추측하지 않는다.

## 새 세션에 줄 요청 예시

> travel-video-pipeline 스킬과 프로젝트의 docs/reusable-scene-library-runbook.md를 읽고,
> 지정한 여행일의 재사용 장면 라이브러리 데이터를 만들어줘. 기존 raw STT·프레임·grouping
> 캐시를 활용하되 --clip-evidence로 새 통합 리뷰를 하고, 의미 단위 클립과 구체적인
> 설명·대화·인물·볼거리를 풍부하게 보존해줘. 하루씩 검증·예외 검토·요약·후보 JSON·편집
> 웹까지 완성하고 재개 가능한 manifest를 남겨줘. 기존 정본은 보존해줘.
