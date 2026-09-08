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
  음식·대화에서의 검토 사례이며 완성 승인 기준은 아니다. 과거
  `parent-quality-review.json`은 새 run의 입력·승인·재검토 근거로 사용하지 않는다.
- `work/highlights/faceless-discovery-v1/source-inventory.json`: 과거 편집용 위치 색인.
  새 모델 리뷰에 이전 답안을 넣기 위한 자료가 아니다.

파일럿의 `prepare.py`, `parent_finalize.py` 같은 로컬 스크립트는 특정 자산과 경로에
묶인 실험 기록이다. 날짜만 바꿔 전체를 처리하지 말고 아래 저장소 runner를 사용한다.
기존 packet은 생성 당시 프롬프트를 담으므로 코드만 업데이트해 재사용하면 새 지침이
반영되지 않는다. 신규 run에서 현재 코드로 packet을 다시 만든다.

## 부모 모델 없이 여러 날짜 실행

준비 manifest가 있는 새 배치는 [자동 작업 큐 가이드](scene-library-queue.md)를 사용한다.
로컬 큐가 fresh worker를 배정하고 실제 validator 결과로 요약·재시도·후보·웹까지 이어간다.
아래 수동 명령은 준비·문제 진단용이며 부모가 asset마다 반복 호출하는 운영 루프로 사용하지 않는다.
기존 완료 데이터를 새 모델 배치에 다시 넣지 않는다.

## 1. 날짜 선택과 로컬 근거 준비

프로젝트 루트에서 실행한다. 아래 날짜는 예시이며 사용자 지정 범위로 바꾼다.
manifest와 결과는 날짜별로 유지하되, 실행은 날짜 완료를 기다리지 않는다. 사용 가능한
worker 슬롯에 여러 날짜의 독립된 asset 또는 완전한 coarse group을 병렬 배정한다.
한 묶음이 끝나면 다음 미배정 묶음으로 바로 넘기며, 이미 담당 중인 근거를 중복 배정하지 않는다.

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

## 3. 자동 완료·요약·라이브러리 생성

```sh
.venv/bin/python scripts/complete_scene_library_day.py \
  "$LIBRARY_RUN_ROOT/prepared/manifest.json" \
  --review-root "$LIBRARY_RUN_ROOT/reviews" \
  --output-root "$LIBRARY_RUN_ROOT/finished" \
  --library-root "$LIBRARY_RUN_ROOT/automatic-library" \
  --story-day "$LIBRARY_DAY" --jobs 2
```

이 명령은 기존 finisher를 호출한 뒤 성공 자산의 라이브러리를 자동 생성한다. 완료 결과는
`finished/completion.json`, `finished/work-queue.json`, `finished/original-relink-index.json`과
`automatic-library/`에 남는다. 이전 원본과 정본은 보존한다.

worker는 장면 리뷰만으로 끝나지 않는다. `timeline.dialogue-reviewed.json`을 만든 뒤 실제
`video-summary-packet.json`을 읽어 `reviews/<asset_id>/summary/video-summary.json`을 작성하고
`validate-video-summary`까지 통과시킨다. 이것이 fresh extract와 장면 리뷰 validator를 포함한
정상 자산 완료 조건이다. 부모 승인이나 날짜별 모델 QA는 없다.

`completion.json`은 완료·진행·예외를 구분한다. 누락 review 또는 summary는 해당 worker 작업으로
`work-queue.json`에 넣는다. `quality-notes.json`의 긴 비트·반복·짧은 설명 같은 신호는 기록용이며
차단하지 않는다. 예외 경로는 `reviews/<asset_id>/review-exceptions.json`뿐이며
`scene-review-exceptions/v1`의 `items[{status: open|resolved, reason: ...}]`로 명시된 근거 충돌 또는
validator 실패만 넣는다. 종료 코드만으로 완료를 판단하지 말고 completion과 queue의 상태·누락·실패를
읽고, 성공 record의 `outputs`로 정본·후보·감사 결과를 찾는다.

## 4. 자동 생성 라이브러리 열기

완료 자산만 `automatic-library/`에 자동 포함된다. 진행 또는 예외 자산은 completion과 queue에 남으며,
성공 자산의 부분 라이브러리는 즉시 사용할 수 있다. 전체 eligible 자산이 끝나고 열린 작업이 없어야
날짜 상태가 `complete`가 된다.

```sh
.venv/bin/python -m travel_video.cli serve-editor \
  "$LIBRARY_RUN_ROOT/automatic-library/manifest.json" \
  --state-dir "$LIBRARY_RUN_ROOT/editor-state" --port 8795 --agent-backend demo
```

사용 중인 포트가 있으면 다른 포트를 고른다. 위 서버는 비용 없는 검토용 demo다.
웹은 프레임·시간·설명·대사·대상·검토 상태를 행에 표시하고, 클립을 펼치면
근거·인물·원음·품질이 즉시 보인다. 재생 UI를 변경했을 때만 후보·맥락 재생을 검증하며,
데이터 배치마다 부모의 재생 검수 단계를 추가하지 않는다.
후보 담기·revision 직접 조립은 별도 기능이며 데이터 생성 완료 조건에 섞지 않는다.

## 5. 하루 상태와 인계

- `completion.json`으로 eligible·완료·진행·예외 수와 자동 validator 결과를 확인한다.
- 완료는 worker의 fresh extraction, 장면 리뷰/whole-summary validator, 그리고 자동 library 생성이다.
- 불확실성은 quality notes에 보존한다. 재작업을 막는 경우는 명시된 증거 충돌 또는 validator 실패뿐이다.
- `work-queue.json`의 누락 review/summary와 열린 `review-exceptions.json`만 후속 worker에 배정한다.
- 기존 날짜 정본·원본·비교 자료는 보존하고, 실행 경로·상태별 수·재시작 명령을 `work/scene-library-runs/`에 남긴다.

## 새 세션에 줄 요청 예시

> travel-video-pipeline 스킬과 프로젝트의 docs/reusable-scene-library-runbook.md를 읽고,
> 지정한 여행일의 재사용 장면 라이브러리 데이터를 만들어줘. 기존 raw STT·프레임·grouping
> 캐시를 활용하되 --clip-evidence로 새 통합 리뷰를 하고, 의미 단위 클립과 구체적인
> 설명·대화·인물·볼거리를 풍부하게 보존해줘. 날짜와 완전한 그룹별로 병렬 추출하고,
> worker 요약과 필수 자동 검증이 통과하면 후보 JSON·편집 웹까지 자동 완료해줘.
> 부모 모델 최종검수 없이 실제 검증 오류·명시적 근거 충돌만 필요한 부분을 수정하고,
> 날짜별 재개 가능한 manifest를 남겨줘. 기존 정본은 보존해줘.
