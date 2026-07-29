"""BedrockBudget-ManageUserBudget

API Gateway
GET  /budget            - 전체 예산 한도 조회
GET  /budget?identity_arn=... - 특정 사용자 예산 한도 조회
PUT  /budget             - { month, identity_arn, budget_limit } 형태로 예산 설정
"""

import json
import os
from decimal import Decimal

import boto3

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "bedrock-budget-user-budget")

dynamodb = boto3.resource("dynamodb")


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def _handle_get(event):
    table = dynamodb.Table(DYNAMODB_TABLE)
    params = event.get("queryStringParameters") or {}
    identity_arn = params.get("identity_arn")

    if identity_arn:
        # identity_arn is the sort key (not partition key), so a table-wide
        # filtered scan is required to look it up across all months.
        items = []
        kwargs = {"FilterExpression": boto3.dynamodb.conditions.Attr("identity_arn").eq(identity_arn)}
        while True:
            response = table.scan(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    else:
        items = []
        kwargs = {}
        while True:
            response = table.scan(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"budgets": items}, cls=DecimalEncoder),
    }


def _handle_put(event):
    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return {"statusCode": 400, "body": json.dumps({"error": "invalid JSON body"})}

    month = payload.get("month")
    identity_arn = payload.get("identity_arn")
    budget_limit = payload.get("budget_limit")

    if not month or not identity_arn or budget_limit is None:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "month, identity_arn, budget_limit are required"}),
        }

    table = dynamodb.Table(DYNAMODB_TABLE)
    table.put_item(
        Item={
            "month": month,
            "identity_arn": identity_arn,
            "budget_limit": Decimal(str(budget_limit)),
        }
    )

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"month": month, "identity_arn": identity_arn, "budget_limit": budget_limit}),
    }


def lambda_handler(event, context):
    method = event.get("httpMethod", "GET")
    if method == "PUT":
        return _handle_put(event)
    return _handle_get(event)
