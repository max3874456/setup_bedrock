"""BedrocBlockAlertLambda

BedrockAthenaLogQueryLambda 에서 비동기로 호출된다.
당월 사용자별 비용을 예산 한도와 비교하여 초과자를 IAM Deny 정책으로 차단하고,
신규 차단자에게 (설정 시) SES 이메일을 발송한다. CloudWatch 커스텀 메트릭도 발행한다.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "bedrock-budget-user-costs")
BUDGET_TABLE = os.environ.get("BUDGET_TABLE", "bedrock-budget-user-budget")
BUDGET_LIMIT = float(os.environ.get("BUDGET_LIMIT", "60"))
SES_SENDER_EMAIL = os.environ.get("SES_SENDER_EMAIL", "")
DENY_POLICY_PREFIX = os.environ.get("DENY_POLICY_PREFIX", "BedrockBudgetDeny")
MANAGED_POLICY_MAX_USERS = int(os.environ.get("MANAGED_POLICY_MAX_USERS", "150"))
MAX_MANAGED_POLICIES = int(os.environ.get("MAX_MANAGED_POLICIES", "9"))
NEAR_LIMIT_RATIO = float(os.environ.get("NEAR_LIMIT_RATIO", "0.8"))

KST = timezone(timedelta(hours=9))

dynamodb = boto3.resource("dynamodb")
iam_client = boto3.client("iam")
cloudwatch = boto3.client("cloudwatch")
ses_client = boto3.client("ses")

MODEL_DISPLAY_NAMES = {
    "opus-4-8": "Claude Opus 4.8",
    "opus-4-7": "Claude Opus 4.7",
    "opus-4-6": "Claude Opus 4.6",
    "opus-4-5": "Claude Opus 4.5",
    "sonnet-5": "Claude Sonnet 5",
    "sonnet-4-6": "Claude Sonnet 4.6",
    "sonnet-4-5": "Claude Sonnet 4.5",
    "sonnet-4": "Claude Sonnet 4",
    "haiku-4-5": "Claude Haiku 4.5",
}


def _decimal_to_float(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _decimal_to_float(v) for k, v in value.items()}
    return value


def get_current_month():
    return datetime.now(KST).strftime("%Y-%m")


def load_user_costs_from_dynamodb(year_month):
    table = dynamodb.Table(DYNAMODB_TABLE)
    items = []
    kwargs = {"KeyConditionExpression": boto3.dynamodb.conditions.Key("month").eq(year_month)}
    while True:
        response = table.query(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    user_costs = {}
    for item in items:
        arn = item["identity_arn"]
        user_costs[arn] = {
            "cost": _decimal_to_float(item.get("cost", 0)),
            "calls": int(item.get("calls", 0)),
            "model_costs": _decimal_to_float(item.get("model_costs", {})),
        }
    return user_costs


def get_user_budget():
    table = dynamodb.Table(BUDGET_TABLE)
    items = []
    kwargs = {}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    latest_by_user = {}
    for item in items:
        arn = item["identity_arn"]
        month = item["month"]
        existing = latest_by_user.get(arn)
        if existing is None or month > existing["month"]:
            latest_by_user[arn] = {"month": month, "budget_limit": _decimal_to_float(item.get("budget_limit", BUDGET_LIMIT))}

    return {arn: v for arn, v in latest_by_user.items()}


def _session_name_from_arn(identity_arn):
    # arn:aws:sts::<account>:assumed-role/<role>/<session-name>
    return identity_arn.rsplit("/", 1)[-1]


def determine_blocked_users(user_costs, user_budget_table):
    blocked = {}
    for arn, info in user_costs.items():
        limit = user_budget_table.get(arn, {}).get("budget_limit", BUDGET_LIMIT)
        if info["cost"] > float(limit):
            blocked[arn] = {"cost": info["cost"], "limit": float(limit), "model_costs": info.get("model_costs", {})}
    return blocked


def get_existing_blocked_sessions():
    existing = set()
    for i in range(1, MAX_MANAGED_POLICIES + 1):
        policy_name = f"{DENY_POLICY_PREFIX}_{i}"
        try:
            account_id = boto3.client("sts").get_caller_identity()["Account"]
            policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"
            version = iam_client.get_policy(PolicyArn=policy_arn)["Policy"]["DefaultVersionId"]
            doc = iam_client.get_policy_version(PolicyArn=policy_arn, VersionId=version)["PolicyVersion"]["Document"]
            for statement in doc.get("Statement", []):
                users = statement.get("Condition", {}).get("StringLike", {}).get("aws:userid", [])
                for u in users:
                    session = u.split(":")[-1]
                    if session != "nobody":
                        existing.add(session)
        except ClientError as exc:
            print(f"[WARN] could not read {policy_name}: {exc}")
    return existing


def _build_deny_document(session_names):
    if not session_names:
        session_names = ["nobody"]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyBedrockForBudgetExceededUsers",
                "Effect": "Deny",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                "Resource": "*",
                "Condition": {"StringLike": {"aws:userid": [f"*:{s}" for s in session_names]}},
            }
        ],
    }


def distribute_and_apply_deny_policies(blocked_session_names, account_id):
    sorted_sessions = sorted(blocked_session_names)
    chunks = [
        sorted_sessions[i : i + MANAGED_POLICY_MAX_USERS]
        for i in range(0, len(sorted_sessions), MANAGED_POLICY_MAX_USERS)
    ]
    chunks += [[] for _ in range(MAX_MANAGED_POLICIES - len(chunks))]
    chunks = chunks[:MAX_MANAGED_POLICIES]

    for idx, chunk in enumerate(chunks, start=1):
        policy_name = f"{DENY_POLICY_PREFIX}_{idx}"
        policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"
        document = _build_deny_document(chunk)

        try:
            versions = iam_client.list_policy_versions(PolicyArn=policy_arn)["Versions"]
            if len(versions) >= 5:
                oldest = sorted(versions, key=lambda v: v["CreateDate"])[0]
                if not oldest["IsDefaultVersion"]:
                    iam_client.delete_policy_version(PolicyArn=policy_arn, VersionId=oldest["VersionId"])
            iam_client.create_policy_version(
                PolicyArn=policy_arn, PolicyDocument=json.dumps(document), SetAsDefault=True
            )
        except ClientError as exc:
            print(f"[ERROR] failed to update {policy_name}: {exc}")


def update_cloudwatch_metrics(user_costs, user_budget_table, exceeded_count):
    total_cost = sum(info["cost"] for info in user_costs.values())
    active_users = len(user_costs)
    near_limit_count = 0

    metric_data = [
        {"MetricName": "TotalMonthlyCost", "Value": total_cost, "Unit": "None"},
        {"MetricName": "ActiveUserCount", "Value": active_users, "Unit": "Count"},
        {"MetricName": "ExceededUserCount", "Value": exceeded_count, "Unit": "Count"},
    ]

    for arn, info in user_costs.items():
        limit = float(user_budget_table.get(arn, {}).get("budget_limit", BUDGET_LIMIT))
        usage_percent = (info["cost"] / limit * 100) if limit > 0 else 0
        if usage_percent >= NEAR_LIMIT_RATIO * 100:
            near_limit_count += 1

        dims = [{"Name": "UserARN", "Value": arn}]
        metric_data.extend(
            [
                {"MetricName": "UserMonthlyCost", "Value": info["cost"], "Unit": "None", "Dimensions": dims},
                {"MetricName": "UsagePercent", "Value": usage_percent, "Unit": "Percent", "Dimensions": dims},
                {"MetricName": "BudgetLimit", "Value": limit, "Unit": "None", "Dimensions": dims},
                {"MetricName": "UserCalls", "Value": info["calls"], "Unit": "Count", "Dimensions": dims},
            ]
        )

    metric_data.append({"MetricName": "NearLimitUserCount", "Value": near_limit_count, "Unit": "Count"})

    for i in range(0, len(metric_data), 20):
        cloudwatch.put_metric_data(Namespace="Bedrock/Budget", MetricData=metric_data[i : i + 20])


def send_block_notification(session_name, info):
    if not SES_SENDER_EMAIL:
        return

    model_lines = "\n".join(
        f"  - {MODEL_DISPLAY_NAMES.get(tier, tier)}: ${cost:.2f}"
        for tier, cost in info.get("model_costs", {}).items()
    )
    body = (
        f"사용자 {session_name} 님이 예산 한도를 초과하여 Bedrock 접근이 차단되었습니다.\n\n"
        f"현재 비용: ${info['cost']:.2f}\n"
        f"예산 한도: ${info['limit']:.2f}\n\n"
        f"모델별 사용 내역:\n{model_lines}\n"
    )

    try:
        ses_client.send_email(
            Source=SES_SENDER_EMAIL,
            Destination={"ToAddresses": [SES_SENDER_EMAIL]},
            Message={
                "Subject": {"Data": f"[Bedrock Budget] {session_name} 예산 초과 차단"},
                "Body": {"Text": {"Data": body}},
            },
        )
    except ClientError as exc:
        print(f"[ERROR] SES send failed for {session_name}: {exc}")


def lambda_handler(event, context):
    year_month = get_current_month()
    account_id = boto3.client("sts").get_caller_identity()["Account"]

    user_costs = load_user_costs_from_dynamodb(year_month)
    user_budget_table = get_user_budget()

    blocked = determine_blocked_users(user_costs, user_budget_table)
    blocked_session_names = {_session_name_from_arn(arn): info for arn, info in blocked.items()}

    existing_blocked = get_existing_blocked_sessions()
    new_blocks = set(blocked_session_names.keys()) - existing_blocked

    distribute_and_apply_deny_policies(set(blocked_session_names.keys()), account_id)

    for session_name in new_blocks:
        print(f"[NEW BLOCK] {session_name} (${blocked_session_names[session_name]['cost']:.2f})")
        send_block_notification(session_name, blocked_session_names[session_name])

    print(f"[INFO] Currently blocked sessions: {len(blocked_session_names)}")

    update_cloudwatch_metrics(user_costs, user_budget_table, len(blocked))

    return {
        "statusCode": 200,
        "body": json.dumps({"blocked": len(blocked), "new_blocks": len(new_blocks)}),
    }
