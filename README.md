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

### 6. Claude Code 사용자 설정 (organization-managed)

사용자별 Claude Code가 어떤 모델을 어떤 Bedrock 리소스로 호출할지 조직 차원에서 강제하려면
**managed-settings.json**을 배포하세요. 이 파일은 사용자 홈(`~/.claude/settings.json`)이나
프로젝트(`.claude/settings.json`) 설정보다 항상 우선하며, 사용자가 자신의 설정 파일을 고쳐도
덮어쓸 수 없습니다.

#### 파일 위치 (관리자 권한으로 배포, OS별 고정 경로)

| OS | 경로 |
|----|------|
| macOS | `/Library/Application Support/ClaudeCode/managed-settings.json` |
| Linux / WSL | `/etc/claude-code/managed-settings.json` |
| Windows | `C:\Program Files\ClaudeCode\managed-settings.json` |

설정 우선순위(높음 → 낮음): **Managed** > 커맨드라인 인자 > Local(`.claude/settings.local.json`)
> Project(`.claude/settings.json`) > User(`~/.claude/settings.json`).

#### 예시로 흔히 잘못 작성되는 필드들

아래와 같은 형태를 종종 보게 되는데, **실제로는 존재하지 않거나 잘못된 필드가 섞여 있어
의도한 통제가 조용히 무시됩니다**:

```jsonc
{
  "model": "opus",
  "availableModels": ["opus", "sonnet", "haiku"],
  "modelOverrides": { "...": "..." },
  "models": {                              // ❌ 이런 중첩 "models" 객체는 존재하지 않음
    "allowed": ["..."],                    // ❌ 존재하지 않는 필드 (무시됨)
    "default": "...",                      // ❌ 존재하지 않는 필드 (무시됨)
    "enforceAllowedOnly": true              // ❌ 존재하지 않는 필드 (무시됨)
  },
  "version": "1.0",                         // ❌ settings.json 스키마에 없는 필드
  "skipDangerousModePermissionPrompt": true, // ❌ 존재하지 않는 필드. 위험 작업 승인을
                                              //    조직 차원에서 스킵시키는 공식 필드는 없음
  "env": {
    "ANTHROPIC_SMALL_FAST_MODEL": "..."      // ⚠️ deprecated. ANTHROPIC_DEFAULT_HAIKU_MODEL 사용
  }
}
```

#### 통제 목적에 맞는 올바른 형태

모델 선택을 3개(Opus/Sonnet/Haiku)로 제한하고, 각 별칭을 계정 소유의 Application Inference
Profile ARN에 고정하려면 다음과 같이 작성합니다. `<ACCOUNT_ID>`는 이 스택을 배포한 본인 계정
ID로 채우세요:

```json
{
  "env": {
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "AWS_REGION": "ap-northeast-2",
    "AWS_PROFILE": "<your-profile>",
    "ANTHROPIC_MODEL": "us.anthropic.claude-sonnet-5",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "us.anthropic.claude-sonnet-5",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "us.anthropic.claude-opus-4-8",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "us.anthropic.claude-haiku-4-5-20251001-v1:0"
  },
  "model": "sonnet",
  "availableModels": ["opus", "sonnet", "haiku"],
  "enforceAvailableModels": true,
  "modelOverrides": {
    "claude-sonnet-5": "arn:aws:bedrock:ap-northeast-2:<ACCOUNT_ID>:application-inference-profile/<sonnet-profile-id>",
    "claude-opus-4-8": "arn:aws:bedrock:ap-northeast-2:<ACCOUNT_ID>:application-inference-profile/<opus-profile-id>",
    "claude-haiku-4-5-20251001-v1:0": "arn:aws:bedrock:ap-northeast-2:<ACCOUNT_ID>:application-inference-profile/<haiku-profile-id>"
  },
  "allowManagedPermissionRulesOnly": false
}
```

필드 설명:

| 필드 | 역할 |
|------|------|
| `env` | 모든 세션/서브프로세스에 강제 적용되는 환경변수. `AWS_PROFILE`을 여기 넣으면 사용자가 셸에서 다른 프로필로 덮어써도 Claude Code 실행 시에는 이 값이 적용됨 |
| `model` | 세션 시작 시 기본 모델 별칭(`opus`/`sonnet`/`haiku`) |
| `availableModels` + `enforceAvailableModels` | `/model` 피커에 노출되는 모델을 지정한 목록으로만 제한. `enforceAvailableModels: true`가 없으면 Default 옵션은 이 제한을 받지 않음 |
| `modelOverrides` | 별칭/모델ID를 실제 Application Inference Profile ARN에 고정. 사용자가 ARN을 직접 지정하지 못하게 막는 핵심 필드 (이 값은 관리자만 관리하며, Claude Code는 시작 시 이 pin을 갱신하라는 알림도 띄우지 않음) |
| `allowManagedPermissionRulesOnly` | (managed-settings 전용) `true`로 설정하면 사용자/프로젝트 설정이 `permissions.allow/ask/deny`를 추가로 정의하지 못하게 막음 |

> `models.allowed`, `models.enforceAllowedOnly`, `skipDangerousModePermissionPrompt`,
> 최상위 `version` 필드는 공식 스키마에 없으므로 **작성해도 조용히 무시됩니다.** 위험 작업
> 승인(permission mode)을 조직 차원에서 통제하려면 `allowManagedPermissionRulesOnly`와
> `permissions.allow/ask/deny`를 조합하세요. 최신 필드 목록은 항상
> [공식 Settings 문서](https://code.claude.com/docs/en/settings)를 기준으로 확인하세요.

## ⚠️ 비용 차단이 적용되는 범위 (중요)

이 시스템은 **비용 집계**와 **차단**의 적용 범위가 다릅니다. 이 차이를 모르고 배포하면
"대시보드에는 비용이 잡히는데 예산을 초과해도 차단이 안 된다" 는 상황이 발생합니다.

### 비용 집계는 모든 호출을 잡는다

`BedrockAthenaLogQueryLambda`는 Bedrock Model Invocation Logging이 기록한 **모든 호출**을
`identity.arn` 기준으로 집계합니다. 어떤 IAM 사용자/역할로 호출했는지와 무관하게
DynamoDB(`bedrock-budget-user-costs`)와 CloudWatch 대시보드에는 전부 반영됩니다.

### 차단은 `ClaudeCodeAccessRole`을 assume한 경우에만 적용된다

`BedrocBlockAlertLambda`가 예산 초과자를 차단하는 방식은, `BedrockBudgetDeny_1`~`_9` 관리형
정책을 **특정 IAM 역할에 attach** 하는 것입니다. `infra/template.yaml`을 그대로 배포하면
이 Deny 정책들은 **`ClaudeCodeAccessRole` 하나에만** 연결됩니다 (`setup-sso-integration.sh`를
실행했다면 지정한 SSO 권한 세트 역할에도 추가로 연결됨).

즉, 사용자가 Bedrock을 호출할 때 사용하는 자격증명의 **경로**에 따라 결과가 달라집니다:

| 호출 경로 | 비용 집계 | 예산 초과 시 차단 |
|-----------|-----------|-------------------|
| `ClaudeCodeAccessRole` assume (README 기본 안내 방식) | ✅ | ✅ |
| `setup-sso-integration.sh`로 연동한 SSO 권한 세트 | ✅ | ✅ |
| 그 외 IAM 역할/사용자로 `bedrock:InvokeModel` 권한을 별도로 가진 경우 (예: AdministratorAccess, 다른 커스텀 정책, 다른 SSO 권한 세트) | ✅ (로그는 항상 남음) | ❌ (Deny 정책이 그 역할/사용자에 없으므로 차단되지 않음) |
| 루트 계정 | ✅ | ❌ |

**Deny는 Allow보다 항상 우선하지만, 애초에 그 사용자의 IAM 주체에 Deny 정책 자체가
연결되어 있어야 효력이 있습니다.** 다른 경로로 Bedrock 호출 권한을 가진 사용자는
Deny 정책이 존재하는지 자체를 모르기 때문에 예산을 초과해도 차단되지 않습니다.

### 확실하게 차단을 강제하려면

1. **Bedrock 호출 권한을 `ClaudeCodeAccessRole` (또는 SSO 연동된 권한 세트) 하나로만 제한**하세요.
   다른 IAM 역할/사용자/그룹 정책에 `bedrock:InvokeModel`, `bedrock:InvokeModelWithResponseStream`
   권한을 별도로 부여하지 않아야 이 시스템의 차단이 실제로 유효합니다.
2. 팀에서 이미 사용 중인 다른 역할로 Bedrock을 호출해야 한다면, 그 역할에도
   `BedrockBudgetDeny_1`~`_9` 정책을 수동으로 attach 하세요:
   ```bash
   for i in $(seq 1 9); do
     aws iam attach-role-policy \
       --role-name <다른-역할-이름> \
       --policy-arn arn:aws:iam::<계정ID>:policy/BedrockBudgetDeny_$i
   done
   ```
3. 정기적으로 계정 내 `bedrock:InvokeModel` 권한을 가진 모든 IAM 주체를
   [IAM Access Analyzer](https://docs.aws.amazon.com/IAM/latest/UserGuide/what-is-access-analyzer.html)
   나 `aws accessanalyzer` / `iam simulate-principal-policy` 로 점검해서, 의도치 않게
   차단을 우회할 수 있는 경로가 늘어나지 않았는지 확인하세요.

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

## 변경 이력

이 저장소의 버그 수정/기능 변경 내역은 날짜별로 [CHANGELOG.md](./CHANGELOG.md)에 기록됩니다.
