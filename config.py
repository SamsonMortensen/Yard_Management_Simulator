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
    """Scans the whole table, following LastEvaluatedKey so results
    stay complete once the table passes DynamoDB's 1 MB page limit."""
    items = []
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            return items
        scan_kwargs["ExclusiveStartKey"] = last_key
