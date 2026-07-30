# CHANGELOG

이 저장소의 주요 변경사항을 날짜별로 기록합니다. 최신 항목이 위에 오도록 추가합니다.

## 2026-07-30

### 수정

- **`bedrock-history-api`(`src/history_api/lambda_function.py`) 날짜 필터가 KST가 아닌 UTC로
  해석되던 버그 수정.** `/users`, `/stats` 엔드포인트의 `start_date`/`end_date` 쿼리 파라미터는
  운영자가 KST 기준 날짜(예: `2026-07-30`)로 입력하지만, 코드가 이를 그대로
  `'{date}T00:00:00Z'`로 만들어 UTC 자정으로 취급하고 있었습니다. 그 결과 실제 조회 구간이
  KST 기준 하루보다 9시간 뒤로 밀려 있었습니다(예: "7월 30일" 요청 시 실제로는 KST 7/30 09:00 ~
  7/31 09:00 구간이 조회됨).
  - `_kst_date_to_utc_iso()` 헬퍼를 추가해 입력 날짜를 KST 자정으로 파싱한 뒤 UTC로 변환하도록
    수정.
  - `BedrockAthenaLogQueryLambda`(`src/athena_log_query/lambda_function.py`)의 월 집계 로직은
    이미 `datetime(..., tzinfo=KST).astimezone(timezone.utc)` 방식으로 정상 처리되고 있어
    수정 대상에서 제외됨. `BedrocBlockAlertLambda`, `BedrockBudget-GetMyUsage`의
    `datetime.now(KST)` 사용도 정상이라 별도 수정 없음.

## 기록 방법

- 새 변경사항은 이 파일 최상단에 `## YYYY-MM-DD` 섹션을 추가하고, 그 아래 `### 추가` / `### 수정` /
  `### 삭제` 하위 섹션으로 정리합니다.
- 무엇을 왜 바꿨는지(원인/증상 포함)를 남겨, 나중에 같은 문제를 다시 조사하지 않도록 합니다.
- 코드 변경이 있다면 관련 파일 경로를 함께 적습니다.
