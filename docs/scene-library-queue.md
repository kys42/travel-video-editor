# 장면 라이브러리 자동 작업 큐

부모 모델은 실행 루프에 참여하지 않는다. 로컬 실행기가 여러 날짜의 미완료 asset을 배정하고, worker가 작성한 결과를 필수 validator로 확인한 뒤 요약·후보·웹까지 연결한다. 부모의 영상 재검수나 승인 파일은 정상 완료 조건이 아니다.

2026-09-08 비용 감사에서 부모 모델이 Standard API 비교액의 73.64%를 차지했다. [감사 보고서](audits/scene-library-usage-2026-09-08.md)와 [후속 이슈 #19](https://github.com/kys42/travel-video-editor/issues/19)에 측정 범위와 한계를 기록했다.

## 운영 계약

- 기존 준비 manifest가 입력 정본이다. 미디어 준비·원본 재처리·하이라이트 렌더는 이 큐의 작업이 아니다.
- 병렬 단위는 완전한 asset이다. 날짜 완료를 기다리지 않고 서로 다른 날짜를 같은 worker pool에서 처리한다. 이미 작성된 complete-group 조각은 완료 실행기가 병합한다. 같은 asset의 그룹을 여러 worker가 동시에 쓰는 자동 분할은 이번 구현 범위가 아니다.
- worker는 새 Codex 작업으로 시작하며 기본 모델은 Luna, reasoning은 medium이다. 이전 대화 resume이나 부모 문맥 전달을 하지 않는다. 상위 모델 자동 승격과 중첩 subagent는 사용하지 않는다.
- 입력 packet에 담긴 정확한 계약과 근거 이미지로 추출한다. review 검증·병합 후 생성된 실제 summary packet을 읽어 요약한다. 파일 목록 확인이나 완료 메시지만 남기고 종료해도 성공이 되지 않는다.
- 완료 실행기는 schema, 근거 ID, 시간 범위, 대화 보존, 요약, 원본 상대 경로/동일 시간 매핑, 후보와 웹 카탈로그 수를 확인한다. 경고나 일반 품질 메모는 추가 모델 검수 사유가 아니다.
- 실제 validator 오류만 제한된 수정 대상으로 배정한다. 명시적인 근거 충돌·입력 준비 오류·재시도 한도 초과는 `needs_attention`으로 남긴다. 의미나 타임코드를 임의 보정해 검증만 통과시키지 않는다.
- 정상 작업의 packet·이미지·상세 로그를 부모 모델에 전달하지 않는다. 상태 JSON과 이벤트 로그가 진행 상황의 정본이다.

## 검증 범위

기존 완료 영상에 신규 유료 모델 호출을 하지 않는다. 실패·재개·병렬성과 완료 판정은 가짜 worker를 사용하는 회귀 테스트로 확인하고, 보존된 실제 완료 데이터는 읽기 전용 계획으로 확인한다. 새 실영상 배치를 끝까지 실행한 비용 절감률은 아직 측정하지 않았다. 형식·근거 연결 검증은 내용 전체의 의미적 정답 보증과 다르다.

## 실행 설정

준비가 끝난 새 날짜마다 다음 항목을 config의 `days` 배열에 추가한다. 아래 경로는 예시이며 실제 준비 manifest와 출력 경로를 지정한다. 날짜마다 review/output/library 루트를 분리한다. 기존 완성 라이브러리를 새 실험 출력 경로로 지정하지 않는다.

```json
{
  "schema_version": "scene-library-queue/v1",
  "state_root": "/Volumes/ExternalSSD/travel-video-editor/analysis/queue-state/new-batch",
  "concurrency": 3,
  "max_attempts": 4,
  "worker_timeout_seconds": 1800,
  "days": [
    {
      "story_day": "2026-08-30",
      "preparation_manifest": "/Volumes/ExternalSSD/travel-video-editor/analysis/new-batch/2026-08-30/prepared/manifest.json",
      "review_root": "/Volumes/ExternalSSD/travel-video-editor/analysis/new-batch/2026-08-30/reviews",
      "output_root": "/Volumes/ExternalSSD/travel-video-editor/analysis/new-batch/2026-08-30/finished",
      "library_root": "/Volumes/ExternalSSD/travel-video-editor/analysis/new-batch/2026-08-30/library",
      "working_directory": "/Users/kys/projects/travel-video-editor"
    }
  ]
}
```

프로젝트 루트의 Python 환경에 의존성을 설치하고 Codex CLI 로그인 상태를 준비한다. config는 예를 들어 `work/scene-library-queue.json`으로 저장한다.

```sh
# 읽기 전용 계획: 모델을 호출하거나 완료 파생물을 다시 만들지 않는다.
PYTHONPATH=src .venv/bin/python scripts/run_scene_library_queue.py work/scene-library-queue.json

# 실제 추출: 완료 또는 처리할 수 없는 예외가 남을 때까지 로컬 큐가 진행한다.
PYTHONPATH=src .venv/bin/python scripts/run_scene_library_queue.py work/scene-library-queue.json --execute
```

완료된 영상 수만 보고 새 모델 작업을 시작하지 않는다. 계획은 저장된 상태의 관찰이고, 실행 시 검증 결과가 최종 완료 조건이다. 같은 설정·상태 경로로 재실행해 이어간다. 정상 실행 중에는 상태 파일을 삭제하거나 worker 출력을 수동 편집하지 않는다.


## 재시도·사용량·예외 읽기

`state_root/state.json`에 asset별 action, attempts, status, 마지막 결과와 날짜별 미해결 work_queue가 저장된다. `events.jsonl`에는 시작·종료·날짜 검증·최종 결과만 추가한다. CLI 종료 코드는 완료 0, 예외 잔존 1, 잘못된 설정/잠금 실패 2다.

`max_attempts`는 **asset 전체**의 worker 실행 상한이다. 기본 4회 안에 review 작성, 실제 summary 작성, 필요한 수정이 포함된다. 재시작해도 누적 횟수가 보존된다. 무한한 재설명이나 모델 승격은 없다. 한 worker의 시간 제한은 기본 1,800초이며 초과하면 process group을 종료한다. 동일 리뷰 루트를 다른 state_root로 동시에 실행해도 잠금으로 거부한다. 실행기가 죽었지만 worker가 살아 있으면 상속된 잠금이 중복 배정을 막는다. 별도 watchdog 프로세스가 실행기 종료 후에도 시간 제한을 지킨다.

`usage.reported_tokens`는 worker JSON 이벤트가 보고한 사용량의 합계다. 누락된 사용량은 `workers_without_usage`로 구분하며 0으로 추정하지 않는다. 입력 캐시와 reasoning은 해당 입력·출력에 포함되므로 별도로 더하지 않는다. 요청 모델/effort는 이벤트에 기록하지만 실제 서버 모델 식별이 보고되지 않으면 검증된 실제 모델이라고 주장하지 않는다. CLI 호출 수와 시간 상한은 비용의 직접적인 달러 상한이 아니다.

기본 adapter는 사용자가 저장한 Codex config를 상속하지 않고 인증만 재사용한다. macOS에서는 앱에 포함된 CLI를 우선 사용하며 `codex_binary`로 실행 파일을 지정할 수 있다. 실행 전에 `exec --help`로 필요한 옵션을 확인한다. 현재 CLI에는 `--full-auto`가 없어 workspace-write 및 approval_policy 옵션을 명시한다. 리뷰 출력 디렉터리는 추가 쓰기 경로로 지정한다.

`needs_attention`이면 날짜의 work_queue.reason과 worker 마지막 오류만 확인한다. 입력 누락은 준비 단계에서 해결하고, 증거 충돌은 해당 asset만 다룬다. 시도 상한에 도달한 작업은 원인을 해결한 뒤 새 state_root로 **그 실패 날짜만** 다시 실행할 수 있다. 실행 초기 validator가 완료 asset을 제외하므로 완성본을 재분석하지 않는다. 단순 사용량 절감을 위해 validator를 끄거나 기존 정본을 삭제하지 않는다.

## 2026-09-08 검증 기록

- 9일치 실제 완료 manifest를 읽기 전용 계획으로 확인: 376개 완료, 모델 호출 0회.
- 점검 전후 기존 JSON 2,727개의 SHA256 비교: 변경 0개.
- 기존 완료·finisher 회귀 테스트 21개와 새 큐 테스트 11개 통과. 날짜 동시 실행, summary 작업 배정, 실패·재시도 상한, 중복 잠금, 실행기 종료 후 watchdog, usage 집계를 검증했다.
- 전체 저장소 테스트: 158개 통과. 기존 의존성 deprecation 경고 2개만 남았다.
- 실제 신규 영상에 대한 유료 worker 호출은 하지 않았다. 이번 변경의 실측 비용 절감률과 새 배치의 내용 품질은 미측정이다.
