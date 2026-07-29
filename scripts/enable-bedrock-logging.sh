#!/usr/bin/env bash
# Bedrock Model Invocation Logging을 S3로 활성화한다.
# CloudFormation은 이 설정을 지원하지 않으므로(계정 단위 리소스) aws cli로 별도 처리한다.
#
# 사용법:
#   ./scripts/enable-bedrock-logging.sh --region ap-northeast-2 --log-bucket bedrock-budget-data-<account>-<region>

set -euo pipefail

REGION="ap-northeast-2"
LOG_BUCKET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --log-bucket) LOG_BUCKET="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "${LOG_BUCKET}" ]]; then
  echo "Usage: $0 --region <region> --log-bucket <bucket-name>" >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text --region "${REGION}")"

CONFIG=$(cat <<JSON
{
  "s3Config": {
    "bucketName": "${LOG_BUCKET}",
    "keyPrefix": "AWSLogs/${ACCOUNT_ID}/BedrockModelInvocationLogs"
  },
  "textDataDeliveryEnabled": true,
  "embeddingDataDeliveryEnabled": false,
  "imageDataDeliveryEnabled": false
}
JSON
)

echo "== Bedrock Model Invocation Logging 활성화 (bucket=${LOG_BUCKET}) =="
aws bedrock put-model-invocation-logging-configuration \
  --region "${REGION}" \
  --logging-config "${CONFIG}"

echo "== 확인 =="
aws bedrock get-model-invocation-logging-configuration --region "${REGION}"
