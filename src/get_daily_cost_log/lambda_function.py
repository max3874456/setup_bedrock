"""BedrockBudget-GetDailyCostLog

API Gateway GET /dailylog?date=YYYY-MM-DD
날짜 파라미터가 없으면 전체 Scan.
"""

import json
import os
from decimal import Decimal

import boto3

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "bedrock-budget-user-costs-log")

dynamodb = boto3.resource("dynamodb")


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def lambda_handler(event, context):
    table = dynamodb.Table(DYNAMODB_TABLE)
    params = event.get("queryStringParameters") or {}
    date = params.get("date")

    if date:
        response = table.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("date").eq(date))
        items = response.get("Items", [])
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
        "body": json.dumps({"logs": items}, cls=DecimalEncoder),
    }
