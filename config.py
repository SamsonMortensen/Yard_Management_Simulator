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


def get_table():
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