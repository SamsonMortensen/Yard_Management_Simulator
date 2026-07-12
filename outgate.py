import random
import time
from datetime import datetime, timezone
from decimal import Decimal

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from config import get_table, scan_all

#Connect to AWS
table = get_table()

def process_outgate():
    #Scan the database ONLY for parked containers
    parked_items = scan_all(
        table,
        FilterExpression=Attr('Current_Status').eq('Parked')
    )

    if not parked_items:
        print("No parked containers ready for outgate.")
        return False

    # Randomly select a driver picking up a container
    container = random.choice(parked_items)
    container_id = container['Container_ID']
    arrival_time_str = container['Arrival_Time']

    print(f"Outgate driver arrived... Searching for {container_id}")
    time.sleep(2) # Simulating gate check and hooking up the chassis

    # Calculate the Dwell Time
    arrival_time = datetime.fromisoformat(arrival_time_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    raw_hours = round((now - arrival_time).total_seconds() / 3600, 4)
    dwell_hours = Decimal(str(raw_hours))

    try:
        #Conditional write: only departs a unit that is still parked
        table.update_item(
            Key={'Container_ID': container_id},
            UpdateExpression="set Current_Status = :s, Dwell_Time_Hours = :d",
            ExpressionAttributeValues={':s': 'Departed', ':d': dwell_hours},
            ConditionExpression=Attr('Current_Status').eq('Parked')
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            print(f"{container_id} already outgated on another lane. Rescanning...\n")
            return True
        raise

    print(f"{container_id} has left the yard.")
    print(f"Final Dwell Time logged: {dwell_hours} hours.\n")
    return True

# Run the Outgate Shift
print("Starting Outgate Shift...")
try:
    while True:
        moved = process_outgate()
        if not moved:
            break # Clock out if the yard is empty
        time.sleep(4) # break before the next truck arrives
except KeyboardInterrupt:
    print("\nShift ended early — outgate clocking out.")
