import boto3
from botocore.exceptions import ClientError

from config import AWS_REGION, TABLE_NAME

def create_yard_table():
    client = boto3.client('dynamodb', region_name=AWS_REGION)
    print(f"Creating table {TABLE_NAME} in {AWS_REGION}...")

    try:
        client.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{'AttributeName': 'Container_ID', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'Container_ID', 'AttributeType': 'S'},
                {'AttributeName': 'Current_Status', 'AttributeType': 'S'}
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'StatusIndex',
                    'KeySchema': [{'AttributeName': 'Current_Status', 'KeyType': 'HASH'}],
                    'Projection': {'ProjectionType': 'ALL'}
                }
            ],
            BillingMode='PAY_PER_REQUEST'
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ResourceInUseException':
            print(f"{TABLE_NAME} already exists: nothing to do.")
            return
        raise

    client.get_waiter('table_exists').wait(TableName=TABLE_NAME)
    print(f"{TABLE_NAME} is active and ready.")

if __name__ == "__main__":
    create_yard_table()
