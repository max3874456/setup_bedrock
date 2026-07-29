"""bedrock-history-api (컴플라이언스)

API Gateway
GET /users         - 사용자별 최초/최종 호출 시각 + 호출 수 (기간 내)
GET /stats          - 일자별 호출 횟수 시계열 (선택적 arn 필터)
GET /conversations  - 특정 사용자 대화 내용 조회 (S3 원본 로그, 최대 200건)
"""

import gzip
import json
import os
import time

import boto3

ATHENA_DB = os.environ.get("ATHENA_DB", "bedrock_logs")
ATHENA_TABLE = os.environ.get("ATHENA_TABLE", "invocations")
ATHENA_OUTPUT = os.environ["ATHENA_OUTPUT"]
ATHENA_WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")
LOG_BUCKET = os.environ["LOG_BUCKET"]
LOG_PREFIX = os.environ.get("LOG_PREFIX", "AWSLogs")
LOG_REGION = os.environ.get("LOG_REGION", "ap-northeast-2")

MAX_CONVERSATION_RECORDS = 200

athena_client = boto3.client("athena", region_name=LOG_REGION)
s3_client = boto3.client("s3", region_name=LOG_REGION)


def run_athena_query(query_string):
    response = athena_client.start_query_execution(
        QueryString=query_string,
        QueryExecutionContext={"Database": ATHENA_DB},
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT},
        WorkGroup=ATHENA_WORKGROUP,
    )
    qid = response["QueryExecutionId"]

    for _ in range(50):
        result = athena_client.get_query_execution(QueryExecutionId=qid)
        state = result["QueryExecution"]["Status"]["State"]
        if state == "SUCCEEDED":
            return qid
        if state in ("FAILED", "CANCELLED"):
            reason = result["QueryExecution"]["Status"].get("StateChangeReason", "unknown")
            raise RuntimeError(f"Athena query {state}: {reason}")
        time.sleep(3)

    raise TimeoutError(f"Athena query {qid} timed out")


def get_athena_results(qid):
    paginator = athena_client.get_paginator("get_query_results")
    rows_out = []
    columns = None
    for page in paginator.paginate(QueryExecutionId=qid):
        for row in page["ResultSet"]["Rows"]:
            values = [col.get("VarCharValue") for col in row.get("Data", [])]
            if columns is None:
                columns = values
                continue
            rows_out.append(dict(zip(columns, values)))
    return rows_out


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def handle_users(params):
    start_date = params.get("start_date")
    end_date = params.get("end_date")

    conditions = []
    if start_date:
        conditions.append(f"\"timestamp\" >= '{start_date}T00:00:00Z'")
    if end_date:
        conditions.append(f"\"timestamp\" < '{end_date}T00:00:00Z'")
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    query = f"""
    SELECT
        identity.arn AS identity_arn,
        MIN("timestamp") AS first_call,
        MAX("timestamp") AS last_call,
        COUNT(*) AS invocations
    FROM {ATHENA_TABLE}
    {where_clause}
    GROUP BY identity.arn
    ORDER BY invocations DESC
    """
    qid = run_athena_query(query)
    return _response(200, {"users": get_athena_results(qid)})


def handle_stats(params):
    arn_filter = params.get("arn")
    where_clauses = []
    if arn_filter:
        where_clauses.append(f"identity.arn = '{arn_filter}'")
    where_clause = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    query = f"""
    SELECT
        dt AS date,
        COUNT(*) AS invocations
    FROM {ATHENA_TABLE}
    {where_clause}
    GROUP BY dt
    ORDER BY dt
    """
    qid = run_athena_query(query)
    return _response(200, {"stats": get_athena_results(qid)})


def handle_conversations(params):
    identity_arn = params.get("arn")
    date = params.get("date")

    if not identity_arn or not date:
        return _response(400, {"error": "arn and date query parameters are required"})

    prefix = f"{LOG_PREFIX}/{date.replace('-', '/')}"
    paginator = s3_client.get_paginator("list_objects_v2")

    records = []
    for page in paginator.paginate(Bucket=LOG_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            if len(records) >= MAX_CONVERSATION_RECORDS:
                break
            body = s3_client.get_object(Bucket=LOG_BUCKET, Key=obj["Key"])["Body"].read()
            try:
                raw = gzip.decompress(body)
            except OSError:
                raw = body

            for line in raw.decode("utf-8", errors="ignore").splitlines():
                if len(records) >= MAX_CONVERSATION_RECORDS:
                    break
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("identity", {}).get("arn") != identity_arn:
                    continue
                records.append(
                    {
                        "timestamp": record.get("timestamp"),
                        "modelId": record.get("modelId"),
                        "input": record.get("input"),
                        "output": record.get("output"),
                    }
                )
        if len(records) >= MAX_CONVERSATION_RECORDS:
            break

    return _response(200, {"conversations": records, "truncated": len(records) >= MAX_CONVERSATION_RECORDS})


def lambda_handler(event, context):
    path = event.get("resource") or event.get("path", "")
    params = event.get("queryStringParameters") or {}

    if path.endswith("/users"):
        return handle_users(params)
    if path.endswith("/stats"):
        return handle_stats(params)
    if path.endswith("/conversations"):
        return handle_conversations(params)

    return _response(404, {"error": f"unknown route: {path}"})
