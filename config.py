"""Shared settings and the two ways the engines read the table.

query_status() is what the engines actually use. It goes through the
StatusIndex GSI, so a worker looking for Ingate_Hold reads only the units
in Ingate_Hold instead of the whole table.

scan_all() is still here because the yard-wide reads need it, but it is the
expensive path. A filtered Scan reads every row and pays for every row no
matter how few match, which is why the engines moved off it.
"""
import os

import boto3

#Central config: every script reads from here.
#Override with environment variables when needed (e.g. a test table).
AWS_REGION = os.environ.get("YMS_REGION", "us-west-2")
TABLE_NAME = os.environ.get("YMS_TABLE", "Yard_Inventory_Sim")

#Yard layout
MIN_SPOT = 1000
MAX_SPOT = 5000
YARD_CAPACITY = MAX_SPOT - MIN_SPOT + 1  # 4001 numbered spots

#Backend selection. Default is real DynamoDB; set YMS_BACKEND=memory to run the
#engines against the in-memory table in mock_dynamo.py instead. The engines never
#see the difference: that is deliberate, so the numbers in the README come from
#the same code paths that would run against AWS.
BACKEND = os.environ.get("YMS_BACKEND", "aws").lower()


def get_table():
    if BACKEND == "memory":
        from mock_dynamo import shared_table
        return shared_table()
    dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    return dynamodb.Table(TABLE_NAME)


def scan_all(table, **scan_kwargs):
    """Reads the whole table, following LastEvaluatedKey so nothing is
    dropped once the table passes DynamoDB's 1 MB page limit.

    Cost grows with the size of the table, not the size of the answer.
    Use query_status() unless you genuinely need every row.
    """
    items = []
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        scan_kwargs["ExclusiveStartKey"] = last_key

from boto3.dynamodb.conditions import Key

def query_status(table, statuses):
    """Queries the StatusIndex for one or more statuses.
    DynamoDB requires a single equality check for partition keys, so we map multiple
    statuses into separate queries and aggregate the result. 
    This entirely eliminates full-table scans."""
    items = []
    for status in statuses:
        kwargs = {
            'IndexName': 'StatusIndex',
            'KeyConditionExpression': Key('Current_Status').eq(status)
        }
        while True:
            resp = table.query(**kwargs)
            items.extend(resp.get('Items', []))
            last_key = resp.get('LastEvaluatedKey')
            if not last_key:
                break
            kwargs['ExclusiveStartKey'] = last_key
    return items
