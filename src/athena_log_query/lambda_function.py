"""BedrockAthenaLogQueryLambda

5분 주기(EventBridge Scheduler)로 실행되어 당월 Bedrock 호출 로그를 Athena로 집계하고,
사용자별 비용을 계산하여 DynamoDB에 저장한 뒤, 예산 차단 판정 Lambda를 비동기로 호출한다.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3

ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "bedrock_logs")
ATHENA_TABLE = os.environ.get("ATHENA_TABLE", "invocations")
ATHENA_OUTPUT_S3 = os.environ["ATHENA_OUTPUT_S3"]
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "bedrock-budget-user-costs")
DYNAMODB_LOG_TABLE = os.environ.get("DYNAMODB_LOG_TABLE", "bedrock-budget-user-costs-log")
BLOCK_ALERT_FUNCTION_NAME = os.environ.get("BLOCK_ALERT_FUNCTION_NAME", "BedrocBlockAlertLambda")

KST = timezone(timedelta(hours=9))

athena_client = boto3.client("athena")
dynamodb = boto3.resource("dynamodb")
bedrock_client = boto3.client("bedrock")
lambda_client = boto3.client("lambda")

# USD per 1,000,000 tokens
PRICING = {
    # Opus 계열
    "opus-4-8": {"input": 5.0, "output": 25.0, "cache_write": 6.250, "cache_read": 0.50},
    "opus-4-7": {"input": 5.0, "output": 25.0, "cache_write": 6.250, "cache_read": 0.50},
    "opus-4-6": {"input": 5.0, "output": 25.0, "cache_write": 6.250, "cache_read": 0.50},
    "opus-4-5": {"input": 5.0, "output": 25.0, "cache_write": 6.250, "cache_read": 0.50},
    # Sonnet 5
    "sonnet-5": {"input": 2.0, "output": 10.0, "cache_write": 2.500, "cache_read": 0.20},
    # Sonnet 4.x 계열
    "sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.750, "cache_read": 0.30},
    "sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_write": 3.750, "cache_read": 0.30},
    "sonnet-4": {"input": 3.0, "output": 15.0, "cache_write": 3.750, "cache_read": 0.30},
    # Haiku
    "haiku-4-5": {"input": 1.0, "output": 5.0, "cache_write": 1.250, "cache_read": 0.10},
}

DEFAULT_TIER = "sonnet-4-6"

# Application Inference Profile ID -> 실제 모델 ARN 매핑. 콜드 스타트 시 1회 빌드.
PROFILE_MAP = {}


def build_profile_mapping():
    mapping = {}
    try:
        paginator = bedrock_client.get_paginator("list_inference_profiles")
        for page in paginator.paginate(typeEquals="APPLICATION"):
            for profile in page.get("inferenceProfileSummaries", []):
                profile_id = profile.get("inferenceProfileId") or profile.get("inferenceProfileArn")
                models = profile.get("models", [])
                if profile_id and models:
                    mapping[profile_id] = models[0].get("modelArn", "")
                    mapping[profile.get("inferenceProfileArn", "")] = models[0].get("modelArn", "")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] build_profile_mapping failed: {exc}")
        return {}
    return mapping


PROFILE_MAP = build_profile_mapping()


def get_model_tier(model_id):
    m = (model_id or "").lower()

    if m in PROFILE_MAP:
        m = PROFILE_MAP[m].lower()

    if "opus-4-8" in m:
        return "opus-4-8"
    if "opus-4-7" in m:
        return "opus-4-7"
    if "opus-4-6" in m:
        return "opus-4-6"
    if "opus-4-5" in m:
        return "opus-4-5"
    if "opus" in m:
        return "opus-4-8"
    if "sonnet-5" in m:
        return "sonnet-5"
    if "sonnet-4-6" in m:
        return "sonnet-4-6"
    if "sonnet-4-5" in m:
        return "sonnet-4-5"
    if "sonnet-4" in m:
        return "sonnet-4"
    if "haiku" in m:
        return "haiku-4-5"
    return DEFAULT_TIER


def get_current_date():
    now_kst = datetime.now(KST)
    return now_kst.strftime("%Y-%m"), now_kst.strftime("%Y-%m-%d")


def run_athena_query(query_string):
    response = athena_client.start_query_execution(
        QueryString=query_string,
        QueryExecutionContext={"Database": ATHENA_DATABASE},
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT_S3},
        WorkGroup=ATHENA_WORKGROUP,
    )
    query_execution_id = response["QueryExecutionId"]

    for _ in range(50):
        result = athena_client.get_query_execution(QueryExecutionId=query_execution_id)
        state = result["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            return query_execution_id
        if state in ("FAILED", "CANCELLED"):
            reason = result["QueryExecution"]["Status"].get("StateChangeReason", "unknown")
            raise RuntimeError(f"Athena query {state}: {reason}")
        time.sleep(3)

    raise TimeoutError(f"Athena query {query_execution_id} timed out after 150s")


def get_athena_results(qid):
    paginator = athena_client.get_paginator("get_query_results")
    rows_out = []
    columns = None
    for page in paginator.paginate(QueryExecutionId=qid):
        rows = page["ResultSet"]["Rows"]
        for row in rows:
            values = [col.get("VarCharValue") for col in row.get("Data", [])]
            if columns is None:
                columns = values
                continue
            rows_out.append(dict(zip(columns, values)))
    return rows_out


def _boundary_partitions(year, month):
    first_of_month = datetime(year, month, 1)
    prev_last_day = first_of_month - timedelta(days=1)

    if month == 12:
        next_first_day = datetime(year + 1, 1, 1)
    else:
        next_first_day = datetime(year, month + 1, 1)

    return {
        "prev": prev_last_day.strftime("%Y/%m/%d"),
        "current_prefix": f"{year:04d}/{month:02d}",
        "next": next_first_day.strftime("%Y/%m/%d"),
    }


def query_bedrock_logs(year, month):
    partitions = _boundary_partitions(year, month)

    start_kst = datetime(year, month, 1, tzinfo=KST)
    if month == 12:
        end_kst = datetime(year + 1, 1, 1, tzinfo=KST)
    else:
        end_kst = datetime(year, month + 1, 1, tzinfo=KST)

    kst_start = start_kst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kst_end = end_kst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    query = f"""
    WITH deduped AS (
        SELECT
            identity.arn AS identity_arn,
            modelid AS modelId,
            input.inputTokenCount AS input_tokens,
            COALESCE(input.cacheReadInputTokenCount, 0) AS cache_read_tokens,
            COALESCE(input.cacheWriteInputTokenCount, 0) AS cache_write_tokens,
            output.outputTokenCount AS output_tokens,
            ROW_NUMBER() OVER (
                PARTITION BY requestid
                ORDER BY (
                    COALESCE(input.inputTokenCount, 0)
                    + COALESCE(input.cacheReadInputTokenCount, 0)
                    + COALESCE(input.cacheWriteInputTokenCount, 0)
                    + COALESCE(output.outputTokenCount, 0)
                ) DESC
            ) AS rn
        FROM {ATHENA_TABLE}
        WHERE (
                dt LIKE '{partitions["current_prefix"]}%'
                OR dt = '{partitions["prev"]}'
                OR dt = '{partitions["next"]}'
              )
          AND "timestamp" >= '{kst_start}'
          AND "timestamp" < '{kst_end}'
    )
    SELECT
        identity_arn,
        modelId,
        SUM(input_tokens) AS input_tokens,
        SUM(cache_read_tokens) AS cache_read_tokens,
        SUM(cache_write_tokens) AS cache_write_tokens,
        SUM(output_tokens) AS output_tokens,
        COUNT(*) AS invocations
    FROM deduped
    WHERE rn = 1
    GROUP BY identity_arn, modelId
    """

    qid = run_athena_query(query)
    return get_athena_results(qid)


def calculate_costs(rows, is_long_context=False):
    user_costs = {}

    for row in rows:
        identity_arn = row.get("identity_arn")
        if not identity_arn:
            continue

        tier = get_model_tier(row.get("modelid", ""))
        price = PRICING.get(tier, PRICING[DEFAULT_TIER])

        input_tokens = int(row.get("input_tokens") or 0)
        cache_read_tokens = int(row.get("cache_read_tokens") or 0)
        cache_write_tokens = int(row.get("cache_write_tokens") or 0)
        output_tokens = int(row.get("output_tokens") or 0)
        calls = int(row.get("invocations") or 0)

        cost = (
            input_tokens * price["input"] / 1_000_000
            + cache_read_tokens * price["cache_read"] / 1_000_000
            + cache_write_tokens * price["cache_write"] / 1_000_000
            + output_tokens * price["output"] / 1_000_000
        )

        bucket = user_costs.setdefault(
            identity_arn,
            {
                "cost": 0.0,
                "calls": 0,
                "model_costs": {},
                "standard_cost": 0.0,
                "long_context_cost": 0.0,
                "input_token": {},
                "output_tokens": {},
                "cache_read_tokens": {},
                "cache_write_tokens": {},
            },
        )

        bucket["cost"] += cost
        bucket["calls"] += calls
        bucket["model_costs"][tier] = bucket["model_costs"].get(tier, 0.0) + cost
        bucket["input_token"][tier] = bucket["input_token"].get(tier, 0) + input_tokens
        bucket["output_tokens"][tier] = bucket["output_tokens"].get(tier, 0) + output_tokens
        bucket["cache_read_tokens"][tier] = bucket["cache_read_tokens"].get(tier, 0) + cache_read_tokens
        bucket["cache_write_tokens"][tier] = bucket["cache_write_tokens"].get(tier, 0) + cache_write_tokens

        if is_long_context:
            bucket["long_context_cost"] += cost
        else:
            bucket["standard_cost"] += cost

    return user_costs


def _to_decimal(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_decimal(v) for k, v in value.items()}
    return value


def save_to_dynamodb(year_month, user_costs):
    table = dynamodb.Table(DYNAMODB_TABLE)
    for identity_arn, info in user_costs.items():
        item = {
            "month": year_month,
            "identity_arn": identity_arn,
            "cost": _to_decimal(info["cost"]),
            "calls": info["calls"],
            "model_costs": _to_decimal(info["model_costs"]),
            "standard_cost": _to_decimal(info["standard_cost"]),
            "long_context_cost": _to_decimal(info["long_context_cost"]),
        }
        table.put_item(Item=item)
    print(f"[INFO] Saved {len(user_costs)} items to {DYNAMODB_TABLE}")


def save_log_to_dynamoDB(user_costs, date):
    table = dynamodb.Table(DYNAMODB_LOG_TABLE)
    for identity_arn, info in user_costs.items():
        item = {
            "date": date,
            "identity_arn": identity_arn,
            "cost": _to_decimal(info["cost"]),
            "calls": info["calls"],
            "model_costs": _to_decimal(info["model_costs"]),
            "input_token": _to_decimal(info["input_token"]),
            "output_tokens": _to_decimal(info["output_tokens"]),
            "cache_read_tokens": _to_decimal(info["cache_read_tokens"]),
            "cache_write_tokens": _to_decimal(info["cache_write_tokens"]),
        }
        table.put_item(Item=item)
    print(f"[INFO] Saved {len(user_costs)} items to {DYNAMODB_LOG_TABLE}")


def lambda_handler(event, context):
    year_month, today = get_current_date()
    year, month = int(year_month[:4]), int(year_month[5:7])

    rows = query_bedrock_logs(year, month)
    user_costs = calculate_costs(rows, is_long_context=False)

    save_to_dynamodb(year_month, user_costs)
    save_log_to_dynamoDB(user_costs, today)

    lambda_client.invoke(
        FunctionName=BLOCK_ALERT_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps({}),
    )

    return {"statusCode": 200, "body": json.dumps({"month": year_month, "users": len(user_costs)})}
