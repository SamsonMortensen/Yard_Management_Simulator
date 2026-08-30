"""Road departures. Outside drivers picking units up and leaving.

Only roadbound units go out this way. They arrived on a train and a customer
is coming to get them. Railbound units entered from the street and leave on
a train, so the crane handles those, not this.

A unit is only eligible once it has sat past its target dwell. That is the
customer showing up, not the yard being ready. Departure writes
Departure_Mode='Road' and releases the exact ground-tier reservation.

Current_Status stays 'Departed' for both road and rail departures. Splitting
it into two terminal states would break every reader that filters on
'Departed', and there are eleven of them.
"""
import time
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from config import get_table, query_status
from atomic_ops import transition_and_release
from flow import is_roadbound
from yard_topology import gate_block, rehandle_for_access, yard_block

table = get_table()
STATS = {"road_departures": 0, "rehandles": 0, "block_hops": 0,
         "simulated_outgate_seconds": 0.0}


def reset_stats():
    for key in STATS:
        STATS[key] = 0 if key != "simulated_outgate_seconds" else 0.0

def _ready_at(item):
    arrival = datetime.fromisoformat(item['Arrival_Time'].replace("Z", "+00:00"))
    return arrival + timedelta(hours=float(item.get('Target_Dwell_Hours', '12.5')))


def process_outgate(now=None):
    # Roadbound units arrived by train and leave through the road outgate.
    now = now or datetime.now(timezone.utc)
    parked_items = query_status(table, ['Parked'])
    ready_roadbound = [item for item in parked_items
                       if is_roadbound(item) and _ready_at(item) <= now]

    if not ready_roadbound:
        return False

    container = min(ready_roadbound, key=_ready_at)
    container_id = container['Container_ID']
    arrival_time_str = container['Arrival_Time']

    try:
        table.update_item(
            Key={'Container_ID': container_id},
            UpdateExpression="set Current_Status = :s",
            ExpressionAttributeValues={':s': 'Claimed'},
            ConditionExpression=Attr('Current_Status').eq('Parked'),
        )
    except ClientError as error:
        if error.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return True
        raise

    # The unit may have been rehandled after the queue scan but before this claim.
    # The claim now prevents another relocation, so use the authoritative location.
    container = table.get_item(Key={'Container_ID': container_id})['Item']

    print(f"Outgate driver arrived... Processing {container_id}")
    try:
        rehandle = rehandle_for_access(table, container, "ROAD_GATE")
    except ClientError as error:
        # Another worker may already be moving a blocker from this stack.
        # Release the pickup claim and let the next queue pass re-evaluate it.
        try:
            table.update_item(
                Key={'Container_ID': container_id},
                UpdateExpression="set Current_Status = :s",
                ExpressionAttributeValues={':s': 'Parked'},
                ConditionExpression=Attr('Current_Status').eq('Claimed'),
            )
        except ClientError as release_error:
            if release_error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise
        if error.response['Error']['Code'] in {
            'ConditionalCheckFailedException', 'TransactionCanceledException'
        }:
            return True
        raise
    hops = 2 * abs(gate_block() - yard_block(container['Assigned_Spot'])) + rehandle['block_hops']
    seconds = (
        hops * float(os.environ.get("HOSTLER_SECONDS_PER_BLOCK", "12"))
        + rehandle['rehandles'] * float(os.environ.get("REHANDLE_SECONDS", "90"))
    )
    time.sleep(seconds)

    arrival_time = datetime.fromisoformat(arrival_time_str.replace("Z", "+00:00"))
    raw_hours = round((now - arrival_time).total_seconds() / 3600, 4)
    dwell_hours = Decimal(str(max(raw_hours, 0.0)))

    try:
        transition_and_release(
            table, container_id, container['Assigned_Spot'],
            "set Current_Status = :s, Departure_Mode = :m, Dwell_Time_Hours = :d, Rehandle_Count = :r",
            {':s': 'Departed', ':m': 'Road', ':d': dwell_hours,
             ':r': int(container.get('Rehandle_Count', 0)) + rehandle['rehandles']},
            expected_status='Claimed',
            reservation_key=container.get('Ground_Reservation_ID'),
            expected_reservation=container.get('Ground_Reservation_ID'),
        )
    except ClientError as e:
        if e.response['Error']['Code'] in {'ConditionalCheckFailedException', 'TransactionCanceledException'}:
            print(f"{container_id} already outgated on another lane. Rescanning...\n")
            return True
        raise

    print(f"{container_id} has left the yard via Road outgate.")
    STATS["road_departures"] += 1
    STATS["rehandles"] += rehandle['rehandles']
    STATS["block_hops"] += hops
    STATS["simulated_outgate_seconds"] += seconds
    return True

def run_shift(now=None):
    while True:
        moved = process_outgate(now=now)
        if not moved:
            break
        time.sleep(1)

if __name__ == "__main__":
    run_shift()
