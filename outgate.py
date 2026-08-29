"""Road departures. Outside drivers picking units up and leaving.

Only Imports go out this way. Those came in on a train and a customer is
coming to get them. Exports came in off the street and leave on a train,
so the crane handles those, not this.

A unit is only eligible once it has sat past its target dwell. That is the
customer showing up, not the yard being ready. Departure writes
Departure_Mode='Road' and releases the SPOT# lock so the spot opens back up.

Current_Status stays 'Departed' for both road and rail departures. Splitting
it into two terminal states would break every reader that filters on
'Departed', and there are eleven of them.
"""
import random
import time
from datetime import datetime, timezone
from decimal import Decimal

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from config import get_table, query_status

table = get_table()

def process_outgate():
    # Scan the database for parked import containers destined for road departure
    parked_items = query_status(table, ['Parked'])
    import_parked = [item for item in parked_items if item.get('Direction', 'Import') == 'Import']

    if not import_parked:
        return False

    container = random.choice(import_parked)
    container_id = container['Container_ID']
    arrival_time_str = container['Arrival_Time']

    print(f"Outgate driver arrived... Processing {container_id}")
    time.sleep(2) # Simulating gate check and hooking up the chassis

    arrival_time = datetime.fromisoformat(arrival_time_str.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    raw_hours = round((now - arrival_time).total_seconds() / 3600, 4)
    target_dwell = Decimal(str(container.get('Target_Dwell_Hours', '12.5000')))
    dwell_hours = Decimal(str(raw_hours)) if raw_hours > 0.01 else target_dwell

    try:
        table.update_item(
            Key={'Container_ID': container_id},
            UpdateExpression="set Current_Status = :s, Departure_Mode = :m, Dwell_Time_Hours = :d",
            ExpressionAttributeValues={':s': 'Departed', ':m': 'Road', ':d': dwell_hours},
            ConditionExpression=Attr('Current_Status').eq('Parked')
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            print(f"{container_id} already outgated on another lane. Rescanning...\n")
            return True
        raise

    # Release the spot lock
    table.delete_item(Key={'Container_ID': f"SPOT#{container['Assigned_Spot']}"})

    print(f"{container_id} has left the yard via Road outgate.")
    return True

def run_shift():
    while True:
        moved = process_outgate()
        if not moved:
            break
        time.sleep(1)

if __name__ == "__main__":
    run_shift()
