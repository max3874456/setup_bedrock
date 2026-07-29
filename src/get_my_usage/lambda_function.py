"""BedrockBudget-GetMyUsage

API Gateway GET /usage
호출자 본인(IAM SigV4) 의 당월 Bedrock 사용량과 예산 한도를 조회한다.
쿼리스트링 ?user=all_users 로 전체 사용자 조회도 지원한다.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import boto3

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "bedrock-budget-user-costs")
BUDGET_TABLE = os.environ.get("BUDGET_TABLE", "bedrock-budget-user-budget")
BUDGET_LIMIT = float(os.environ.get("BUDGET_LIMIT", "60"))
IDENTITY_ARN_TEMPLATE = os.environ.get("IDENTITY_ARN_TEMPLATE", "")

KST = timezone(timedelta(hours=9))
dynamodb = boto3.resource("dynamodb")


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def _current_month():
    return datetime.now(KST).strftime("%Y-%m")


def _budget_for(identity_arn):
    # identity_arn is the sort key (month is partition key), so a filtered
    # scan is required; keep the latest month's budget_limit if several exist.
    table = dynamodb.Table(BUDGET_TABLE)
    items = []
    kwargs = {"FilterExpression": boto3.dynamodb.conditions.Attr("identity_arn").eq(identity_arn)}
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    if not items:
        return BUDGET_LIMIT
    latest = max(items, key=lambda i: i["month"])
    return float(latest.get("budget_limit", BUDGET_LIMIT))


def _usage_for(identity_arn, year_month):
    table = dynamodb.Table(DYNAMODB_TABLE)
    response = table.get_item(Key={"month": year_month, "identity_arn": identity_arn})
    item = response.get("Item")
    if not item:
        return {"cost": 0, "calls": 0, "model_costs": {}}
    return item


def _resolve_identity_arn(event):
    request_context = event.get("requestContext", {})
    identity = request_context.get("identity", {})
    user_arn = identity.get("userArn") or identity.get("caller")

    if IDENTITY_ARN_TEMPLATE and user_arn:
        session_name = user_arn.rsplit("/", 1)[-1]
        return IDENTITY_ARN_TEMPLATE.format(session_name=session_name)

    return user_arn


def lambda_handler(event, context):
    query_params = event.get("queryStringParameters") or {}
    year_month = _current_month()

    if query_params.get("user") == "all_users":
        table = dynamodb.Table(DYNAMODB_TABLE)
        response = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("month").eq(year_month)
        )
        items = response.get("Items", [])
        body = {"month": year_month, "users": items}
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(body, cls=DecimalEncoder),
        }

    identity_arn = _resolve_identity_arn(event)
    if not identity_arn:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "could not resolve caller identity"}),
        }

    usage = _usage_for(identity_arn, year_month)
    budget_limit = _budget_for(identity_arn)

    body = {
        "identity_arn": identity_arn,
        "month": year_month,
        "cost": usage.get("cost", 0),
        "calls": usage.get("calls", 0),
        "model_costs": usage.get("model_costs", {}),
        "budget_limit": budget_limit,
    }

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, cls=DecimalEncoder),
    }
