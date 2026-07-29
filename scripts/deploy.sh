#!/usr/bin/env bash
# 어느 AWS 계정에서든 aws cli만으로 이 스택을 프로비저닝한다.
# SAM CLI는 필요 없다 — aws cloudformation package/deploy 가 Serverless Transform을 처리한다.
#
# 사용법:
#   ./scripts/deploy.sh [--region ap-northeast-2] [--stack-name bedrock-budget] \
#     [--budget-limit 60] [--ses-sender-email ""] [--trusted-principal-arns "arn:aws:iam::111111111111:root"]
#
# 사전조건:
#   - aws configure 로 자격증명 설정 완료
#   - 배포자에게 CloudFormation, IAM(역할/정책 생성), Lambda, S3, DynamoDB,
#     Glue, Athena, EventBridge Scheduler, API Gateway 생성 권한 필요

set -euo pipefail

REGION="ap-northeast-2"
STACK_NAME="bedrock-budget"
PROJECT_PREFIX="bedrock-budget"
BUDGET_LIMIT="60"
SES_SENDER_EMAIL=""
TRUSTED_PRINCIPAL_ARNS=""
MANAGED_POLICY_MAX_USERS="150"
MAX_MANAGED_POLICIES="9"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --project-prefix) PROJECT_PREFIX="$2"; shift 2 ;;
    --budget-limit) BUDGET_LIMIT="$2"; shift 2 ;;
    --ses-sender-email) SES_SENDER_EMAIL="$2"; shift 2 ;;
    --trusted-principal-arns) TRUSTED_PRINCIPAL_ARNS="$2"; shift 2 ;;
    --managed-policy-max-users) MANAGED_POLICY_MAX_USERS="$2"; shift 2 ;;
    --max-managed-policies) MAX_MANAGED_POLICIES="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
INFRA_DIR="${REPO_ROOT}/infra"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "${REGION}")"
ARTIFACT_BUCKET="${PROJECT_PREFIX}-sam-artifacts-${ACCOUNT_ID}-${REGION}"

echo "== 계정: ${ACCOUNT_ID} / 리전: ${REGION} / 스택: ${STACK_NAME} =="

echo "== 배포 아티팩트 버킷 준비: ${ARTIFACT_BUCKET} =="
if ! aws s3api head-bucket --bucket "${ARTIFACT_BUCKET}" --region "${REGION}" 2>/dev/null; then
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${ARTIFACT_BUCKET}" --region "${REGION}"
  else
    aws s3api create-bucket --bucket "${ARTIFACT_BUCKET}" --region "${REGION}" \
      --create-bucket-configuration LocationConstraint="${REGION}"
  fi
  aws s3api put-bucket-encryption --bucket "${ARTIFACT_BUCKET}" --region "${REGION}" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
  aws s3api put-public-access-block --bucket "${ARTIFACT_BUCKET}" --region "${REGION}" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

PACKAGED_TEMPLATE="$(mktemp -t bedrock-budget-packaged.XXXXXX.yaml)"

echo "== Lambda 코드 패키징 및 업로드 =="
aws cloudformation package \
  --template-file "${INFRA_DIR}/template.yaml" \
  --s3-bucket "${ARTIFACT_BUCKET}" \
  --s3-prefix "packages" \
  --region "${REGION}" \
  --output-template-file "${PACKAGED_TEMPLATE}"

echo "== CloudFormation 스택 배포 =="
aws cloudformation deploy \
  --template-file "${PACKAGED_TEMPLATE}" \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    ProjectPrefix="${PROJECT_PREFIX}" \
    DefaultBudgetLimitUsd="${BUDGET_LIMIT}" \
    SesSenderEmail="${SES_SENDER_EMAIL}" \
    TrustedPrincipalArns="${TRUSTED_PRINCIPAL_ARNS}" \
    ManagedPolicyMaxUsers="${MANAGED_POLICY_MAX_USERS}" \
    MaxManagedPolicies="${MAX_MANAGED_POLICIES}"

rm -f "${PACKAGED_TEMPLATE}"

echo "== 스택 출력값 =="
aws cloudformation describe-stacks \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --query "Stacks[0].Outputs" \
  --output table

LOG_BUCKET="$(aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" \
  --query "Stacks[0].Outputs[?OutputKey=='LogBucketName'].OutputValue" --output text)"

echo
echo "== 다음 단계 =="
echo "1) Bedrock Model Invocation Logging 활성화:"
echo "   ./scripts/enable-bedrock-logging.sh --region ${REGION} --log-bucket ${LOG_BUCKET}"
echo "2) IAM Identity Center 권한 세트를 ClaudeCodeAccess 역할과 연동하려면 README.md의 'SSO 연동' 절 참고"
echo "3) 필요 시 SES 발신 이메일 인증 (README.md 'SES 알림 설정' 절 참고)"
