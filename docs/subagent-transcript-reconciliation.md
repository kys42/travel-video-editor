# 서브에이전트 대본 종합 실험

## 목적

전체 영상을 모델이 순차 시청하지 않고도 Apple 한국어·영어 STT 원시 후보와 타임스탬프 근거만으로 편집용 원문 대본을 만들 수 있는지 확인했다. 대상은 음식 주문 영상의 `RW0010`–`RW0018` 구간이다.

## 실험 산출물

- 입력 패킷: `work/apple-speech/0009-full-v1/reconciliation/packet.json`
- Luna A: `work/apple-speech/0009-full-v1/reconciliation/experiments/luna-a.json`
- Luna B: `work/apple-speech/0009-full-v1/reconciliation/experiments/luna-b.json`
- 타임 근거 보강 후 Luna: `work/apple-speech/0009-full-v1/reconciliation/experiments/luna-timed.json`
- 기계 비교: `work/apple-speech/0009-full-v1/reconciliation/experiments/comparison.json`

세 실행 모두 같은 9개 창만 받았으며 원본 영상을 보지 않았다. 앞의 두 실행은 창 단위로 합쳐진 후보 문장과 장면 맥락을 사용했다. 첫 실행에서 `RW0010`의 한국어와 영어 발화 순서가 뒤집혔다. 이를 계기로 패킷의 모든 후보에 `evidence_spans`를 추가해 원시 토큰/구절의 시작·종료 시각, 신뢰도, 원본 후보 ID를 전달했다.

보강 후 실행은 모든 발화를 창 안에 배치했고, `RW0010`에서 영어 주문 뒤 한국어 메뉴 대화가 이어지는 순서를 유지했다. `RW0016`의 `COVID`/`Coke`처럼 음향 근거만으로 안전하게 고칠 수 없는 단어는 억지로 정정하지 않고 `uncertain`으로 남겼다. `RW0017`의 영어 음차와 한국어 발화도 별도 turn으로 나눴다.

## 해석

저가 서브에이전트는 대량의 명확한 창을 1차 정리하기에 충분하다. 단, 장면 맥락을 강하게 사용한 실행은 더 읽기 좋은 문장을 만들지만 `cook → Coke`, `grape cream → grapefruit` 같은 편집적 추정을 원문처럼 확정할 위험이 있었다. 보수적인 실행은 근거 충실도가 높지만 읽기 좋은 대본으로 만들려면 예외 검토가 더 필요하다.

기계 비교에서 9개 창 중 언어 turn 배열이 완전히 같은 창은 1개뿐이었다. 이것은 모든 창이 실패했다는 뜻이 아니라, 분할 granularity와 `uncertain` 정책이 모델마다 달랐다는 뜻이다. 따라서 다수결로 전체 문장을 고르는 방식보다, 창별 불일치와 낮은 신뢰도만 강한 모델이나 Codex가 재검토하는 방식이 비용 대비 낫다.

## 권장 운영안

1. Apple Detector 게이팅과 `ko-KR`, `en-US` 전사를 한 번씩 수행해 원시 후보를 보존한다.
2. 최대 8초 reconciliation 창을 만들고 반드시 `evidence_spans`를 포함한다.
3. 창을 겹치지 않는 연속 범위로 나눠 Luna급 서브에이전트에 병렬 할당한다.
4. 각 결과를 스키마·창 범위·source candidate ID·시간 경계로 자동 검증한다.
5. 전체 shard를 패킷 순서대로 병합한다.
6. 다음 창만 Terra급 또는 Codex에 올린다: 중요 발화 신뢰도 0.65 미만, 언어/순서 불일치, 코드스위칭, 맥락 기반 정정, 가격·고유명사·메뉴명 충돌.
7. 자동 병합본과 보정 로그를 모두 남기고, 최종 원문과 번역은 별도 필드로 유지한다.

실제 shard 스키마, 프롬프트, 병합/비교 명령은 독립 스킬의 `references/transcript-reconciliation.md`에 정리했다.
