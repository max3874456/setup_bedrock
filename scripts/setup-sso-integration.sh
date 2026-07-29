#!/usr/bin/env bash
# IAM Identity Center(SSO) 권한 세트가 만드는 역할에 Bedrock 예산 Deny 정책(BedrockBudgetDeny_1~N)을
# 부착한다. SSO 구성은 계정마다 다르므로, 이미 존재하는 권한 세트 이름을 인자로 받는다.
#
# 사용법:
#   ./scripts/setup-sso-integration.sh --region ap-northeast-2 \
#     --permission-set-name ClaudeCodeAccess --instance-arn arn:aws:sso:::instance/ssoins-xxxx \
#     [--max-managed-policies 9]
#
# 사전조건: IAM Identity Center가 이미 활성화되어 있고, 대상 권한 세트가 존재해야 한다.

set -euo pipefail

REGION="ap-northeast-2"
PERMISSION_SET_NAME=""
INSTANCE_ARN=""
MAX_MANAGED_POLICIES="9"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --permission-set-name) PERMISSION_SET_NAME="$2"; shift 2 ;;
    --instance-arn) INSTANCE_ARN="$2"; shift 2 ;;
    --max-managed-policies) MAX_MANAGED_POLICIES="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "${PERMISSION_SET_NAME}" || -z "${INSTANCE_ARN}" ]]; then
  echo "Usage: $0 --permission-set-name <name> --instance-arn <arn> [--region <region>]" >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "${REGION}")"

PERMISSION_SET_ARN="$(aws sso-admin list-permission-sets --instance-arn "${INSTANCE_ARN}" --region "${REGION}" \
  --query "PermissionSets" --output text | tr '\t' '\n' | while read -r arn; do
    name=$(aws sso-admin describe-permission-set --instance-arn "${INSTANCE_ARN}" --permission-set-arn "${arn}" \
      --region "${REGION}" --query "PermissionSet.Name" --output text)
    if [[ "${name}" == "${PERMISSION_SET_NAME}" ]]; then
      echo "${arn}"
      break
    fi
  done)"

if [[ -z "${PERMISSION_SET_ARN}" ]]; then
  echo "권한 세트 '${PERMISSION_SET_NAME}' 를 찾지 못했습니다." >&2
  exit 1
fi

echo "== 권한 세트 발견: ${PERMISSION_SET_ARN} =="

for i in $(seq 1 "${MAX_MANAGED_POLICIES}"); do
  POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/BedrockBudgetDeny_${i}"
  echo "-- BedrockBudgetDeny_${i} 를 권한 세트에 연결 --"
  aws sso-admin attach-managed-policy-to-permission-set \
    --instance-arn "${INSTANCE_ARN}" \
    --permission-set-arn "${PERMISSION_SET_ARN}" \
    --managed-policy-arn "${POLICY_ARN}" \
    --region "${REGION}" || echo "  (이미 연결되어 있을 수 있음, 계속 진행)"
done

echo "== 계정에 권한 세트 프로비저닝 재적용 (변경사항 반영) =="
aws sso-admin provision-permission-set \
  --instance-arn "${INSTANCE_ARN}" \
  --permission-set-arn "${PERMISSION_SET_ARN}" \
  --target-id "${ACCOUNT_ID}" \
  --target-type AWS_ACCOUNT \
  --region "${REGION}"

echo "완료. 사용자가 재로그인하면 새 세션에 Deny 정책이 반영됩니다."
