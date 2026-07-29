# Bedrock Claude 사내 배포 + 비용 통제 시스템

Amazon Bedrock 기반 Claude Code를 사내에 배포하고, 사용자별 API 호출 비용을 5분 주기로 집계하여
월간 예산 초과 시 자동으로 Bedrock 접근을 차단하는 시스템입니다. 어느 AWS 계정에서도 `aws cli`만으로
동일하게 프로비저닝할 수 있도록 구성했습니다.

이 저장소는 `bedrock/` 폴더의 운영 문서(콘솔에서 수동 구축된 기존 시스템의 동작 기록)를 바탕으로
소스코드와 IaC(CloudFormation/SAM)를 재구성한 것입니다. `bedrock/` 폴더는 참고용 원본 문서로 유지됩니다.

## 아키텍처

```
EventBridge Scheduler (5분 주기, KST)
        │
        ▼
BedrockAthenaLogQueryLambda ──(Athena로 당월 호출 로그 집계)──▶ DynamoDB (월별/일별 비용)
        │
        └──(비동기 호출)──▶ BedrocBlockAlertLambda
                                 │  예산 초과자 판정
                                 └──▶ IAM Deny 정책 갱신 (BedrockBudgetDeny_1~9)
                                 └──▶ (선택) SES 알림

API Gateway (bedrock-budget-api)       API Gateway (bedrock-compliance-api)
  /usage    → GetMyUsage                 /users         → history-api
  /budget   → ManageUserBudget           /stats         → history-api
  /dailylog → GetDailyCostLog            /conversations → history-api
```

Bedrock 호출 로그는 Model Invocation Logging을 통해 S3에 적재되고, Glue Data Catalog + Athena로
파티션 프로젝션 기반 조회를 수행합니다(파티션 자동 인식, `MSCK REPAIR TABLE` 불필요).

## 디렉터리 구조

```
infra/template.yaml   - SAM/CloudFormation 템플릿 (전체 리소스 정의)
src/                   - Lambda 함수별 소스코드 (6개)
scripts/               - aws cli 배포/설정 스크립트
bedrock/               - 원본 운영 문서 (참고용)
```

## 사전조건

- `aws configure` 로 자격증명이 설정되어 있고, 다음 리소스를 생성할 권한이 있어야 합니다:
  CloudFormation, IAM(역할/관리형 정책), Lambda, S3, DynamoDB, Glue, Athena,
  EventBridge Scheduler, API Gateway
- Bedrock에서 사용할 리전에 원하는 Claude 모델(또는 Application Inference Profile)에 대한
  모델 액세스가 활성화되어 있어야 합니다.
- SAM CLI는 필요 없습니다 — `aws cloudformation package/deploy` 로 SAM Transform을 처리합니다.

## 배포 절차

### 0. 저장소 클론

```bash
git clone git@github.com:max3874456/setup_bedrock.git
cd setup_bedrock
```

### 1. 인프라 스택 배포

```bash
./scripts/deploy.sh \
  --region ap-northeast-2 \
  --stack-name bedrock-budget \
  --budget-limit 60 \
  --ses-sender-email ""
```

주요 옵션:

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--region` | `ap-northeast-2` | 배포 리전 |
| `--stack-name` | `bedrock-budget` | CloudFormation 스택 이름 |
| `--project-prefix` | `bedrock-budget` | 리소스 이름 접두사 (S3 버킷, DynamoDB 테이블 등) |
| `--budget-limit` | `60` | 개인별 한도가 없는 사용자에게 적용되는 기본 월 예산(USD) |
| `--ses-sender-email` | (없음) | 이메일 알림용 SES 발신자. 비우면 알림 비활성화 |
| `--trusted-principal-arns` | (없음) | `ClaudeCodeAccess` 역할을 assume 할 수 있는 principal ARN (콤마 구분). 비우면 계정 root + MFA 조건으로 기본 설정 |
| `--managed-policy-max-users` | `150` | Deny 정책 1개당 최대 차단 인원 |
| `--max-managed-policies` | `9` | Deny 정책 개수 (최대 차단 인원 = 이 값 × `--managed-policy-max-users`) |

배포가 끝나면 API Gateway 엔드포인트, `ClaudeCodeAccess` 역할 ARN, DynamoDB 테이블 이름 등이 출력됩니다.

### 2. Bedrock Model Invocation Logging 활성화

CloudFormation은 이 설정(계정 단위 리소스)을 지원하지 않으므로 별도 스크립트로 처리합니다.

```bash
./scripts/enable-bedrock-logging.sh \
  --region ap-northeast-2 \
  --log-bucket <deploy.sh 출력의 LogBucketName>
```

### 3. (선택) IAM Identity Center(SSO) 연동

이미 IAM Identity Center를 사용 중이고, 사내 사용자가 SSO 권한 세트를 통해 `ClaudeCodeAccess` 역할이
아닌 **자체 SSO 역할**로 Bedrock을 호출하는 구조라면, Deny 정책을 해당 권한 세트에도 연결해야 합니다.

```bash
./scripts/setup-sso-integration.sh \
  --region ap-northeast-2 \
  --permission-set-name ClaudeCodeAccess \
  --instance-arn arn:aws:sso:::instance/ssoins-xxxxxxxxxxxxxxxx
```

SSO를 쓰지 않는다면 이 단계는 생략하고, `deploy.sh`가 생성한 `ClaudeCodeAccess` IAM 역할을
사용자가 직접 assume 하도록 안내하면 됩니다 (`--trusted-principal-arns`로 assume 가능한 주체를 지정).

### 4. 배포 확인

5분 정도 지나면 EventBridge Scheduler가 `BedrockAthenaLogQueryLambda`를 자동 실행합니다.

- CloudWatch Logs > `/aws/lambda/BedrockAthenaLogQueryLambda` 에서 `[INFO] Saved ... items` 로그 확인
- DynamoDB `<prefix>-user-costs` 테이블에 당월(`month`) 항목이 생성되는지 확인

### 5. (선택) SES 알림 활성화

1. SES 콘솔 > Verified identities 에서 발신 이메일 인증
2. 샌드박스 상태라면 AWS Support에 프로덕션 액세스 요청
3. `--ses-sender-email` 옵션으로 스택을 재배포(`deploy.sh` 재실행)

## 운영

### 사용자별 예산 한도 변경

```bash
aws apigateway ... # 또는 아래처럼 SigV4 서명된 요청으로 직접 호출
curl --request PUT \
  --aws-sigv4 "aws:amz:ap-northeast-2:execute-api" \
  --url "<BudgetApiEndpoint>/budget" \
  --data '{"month":"2026-07","identity_arn":"arn:aws:sts::<account>:assumed-role/ClaudeCodeAccess/user@example.com","budget_limit":100}'
```

또는 DynamoDB `<prefix>-user-budget` 테이블에 항목을 직접 추가/수정합니다.

### 모델 가격/신규 모델 추가

`src/athena_log_query/lambda_function.py` 의 `PRICING` dict, `get_model_tier()` 함수를 수정한 뒤
`./scripts/deploy.sh` 를 다시 실행하면 반영됩니다. (기존 `bedrock/03_운영_가이드.md` 문서에
더 상세한 절차가 남아 있습니다.)

### 차단 해제 / 용량 확장 / 트러블슈팅

`bedrock/03_운영_가이드.md` 문서에 원본 시스템 기준의 상세 절차가 있으며, 리소스 이름 접두사만
`--project-prefix` 값으로 바꿔서 그대로 적용할 수 있습니다.

## 재배포 / 변경사항 반영

Lambda 코드나 템플릿을 수정한 뒤에는 `./scripts/deploy.sh` 를 동일한 `--stack-name`으로 다시
실행하면 됩니다. `aws cloudformation package`가 변경된 코드만 새로 업로드하고, `deploy`가 변경분만
업데이트합니다.

## 삭제

```bash
aws cloudformation delete-stack --stack-name bedrock-budget --region ap-northeast-2
```

S3 로그 버킷에 객체가 남아있으면 스택 삭제가 실패할 수 있습니다. 필요 시 버킷을 먼저 비우세요.
Bedrock Model Invocation Logging 설정은 스택과 별개이므로, 더 이상 필요 없다면
`aws bedrock delete-model-invocation-logging-configuration` 으로 직접 해제해야 합니다.
