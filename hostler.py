"""Hostlers. The yard drivers, working both directions.

Inbound leg: pick up whatever the crane put down, or a box an outside driver
dropped at the gate, and park it in its spot.

Outbound leg: go get an export off its spot and drop it trackside as
Awaiting_Rail so the crane can load it on the train. The SPOT# lock is
released on the way out, which is what puts that spot back in circulation.

Dual cycling is the part that matters for throughput. After dropping an
import at a spot, the hostler looks for an export sitting in the same block
(spot // 100) instead of driving back to the track empty. Same trip, two
moves.

How a hostler picks its next unit:

  head      take the front of the queue. Every hostler picks the same one,
            so they collide constantly. One lost race per container.
  random    draw from anywhere in the queue. Collisions mostly disappear
            and so does arrival order.
  dispatch  claim the unit before driving to it. A lost race costs a retry
            instead of a wasted trip, which is the whole point.

Read from the environment on every call, not once at import, so a harness
comparing all three in one process gets the right one each time.
"""
import os
import random
import time

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from config import get_table, query_status

table = get_table()

active_hostlers = ["EMP-104", "EMP-227", "EMP-309", "EMP-412"]

def claim_strategy():
    return os.environ.get("YMS_CLAIM", "head").lower()

def is_unsafe():
    return os.environ.get("YMS_UNSAFE", "false").lower() == "true"

def find_dual_cycle_export(table, drop_spot, exclude_cid=None):
    parked = query_status(table, ['Parked'])
    exports = [item for item in parked if item.get('Direction') == 'Export' and item.get('Container_ID') != exclude_cid]
    if not exports:
        return None
    
    target_block = drop_spot // 100
    block_matches = [item for item in exports if (item.get('Assigned_Spot', 0) // 100) == target_block]
    if block_matches:
        return block_matches[0]
    return exports[0]

def move_container():
    driver = random.choice(active_hostlers)
    strategy = claim_strategy()
    unsafe = is_unsafe()

    # 1. Inbound Queues (Gate arrivals & Crane discharge)
    gate_items = query_status(table, ['Ingate_Hold', 'Buffer_Hold', 'Rendezvous_Wait'])

    if gate_items:
        if strategy == "dispatch" and not unsafe:
            container_id = None
            assigned_spot = None
            for candidate in gate_items:
                candidate_id = candidate['Container_ID']
                try:
                    table.update_item(
                        Key={'Container_ID': candidate_id},
                        UpdateExpression="set Current_Status = :s, Claimed_By = :e",
                        ExpressionAttributeValues={':s': 'Claimed', ':e': driver},
                        ConditionExpression=Attr('Current_Status').eq(candidate['Current_Status'])
                    )
                    container_id = candidate_id
                    assigned_spot = candidate['Assigned_Spot']
                    break
                except ClientError as e:
                    if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                        continue
                    raise

            if not container_id:
                return True

            time.sleep(2)

            table.update_item(
                Key={'Container_ID': container_id},
                UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
                ExpressionAttributeValues={':s': 'Parked', ':e': driver},
                ConditionExpression=Attr('Current_Status').eq('Claimed')
            )

            # Dual cycling opportunity
            export_candidate = find_dual_cycle_export(table, assigned_spot, exclude_cid=container_id)
            if export_candidate:
                exp_id = export_candidate['Container_ID']
                exp_spot = export_candidate['Assigned_Spot']
                try:
                    table.update_item(
                        Key={'Container_ID': exp_id},
                        UpdateExpression="set Current_Status = :s, Claimed_By = :e",
                        ExpressionAttributeValues={':s': 'Claimed', ':e': driver},
                        ConditionExpression=Attr('Current_Status').eq('Parked')
                    )
                    time.sleep(2)
                    table.update_item(
                        Key={'Container_ID': exp_id},
                        UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
                        ExpressionAttributeValues={':s': 'Awaiting_Rail', ':e': driver},
                        ConditionExpression=Attr('Current_Status').eq('Claimed')
                    )
                    table.delete_item(Key={'Container_ID': f"SPOT#{exp_spot}"})
                except ClientError as e:
                    if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                        raise
            return True

        container = random.choice(gate_items) if strategy == "random" else gate_items[0]
        container_id = container['Container_ID']
        assigned_spot = container['Assigned_Spot']

        time.sleep(2)

        if unsafe:
            table.update_item(
                Key={'Container_ID': container_id},
                UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
                ExpressionAttributeValues={':s': 'Parked', ':e': driver}
            )
            return True

        try:
            table.update_item(
                Key={'Container_ID': container_id},
                UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
                ExpressionAttributeValues={':s': 'Parked', ':e': driver},
                ConditionExpression=Attr('Current_Status').eq(container['Current_Status'])
            )
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return True
            raise

        return True

    # 2. Outbound Retrieval Queue (Parked units with Direction == 'Export')
    parked_items = query_status(table, ['Parked'])
    export_items = [item for item in parked_items if item.get('Direction') == 'Export']

    if export_items:
        if strategy == "dispatch" and not unsafe:
            container_id = None
            assigned_spot = None
            for candidate in export_items:
                candidate_id = candidate['Container_ID']
                try:
                    table.update_item(
                        Key={'Container_ID': candidate_id},
                        UpdateExpression="set Current_Status = :s, Claimed_By = :e",
                        ExpressionAttributeValues={':s': 'Claimed', ':e': driver},
                        ConditionExpression=Attr('Current_Status').eq('Parked')
                    )
                    container_id = candidate_id
                    assigned_spot = candidate['Assigned_Spot']
                    break
                except ClientError as e:
                    if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                        continue
                    raise

            if not container_id:
                return True

            time.sleep(2)

            table.update_item(
                Key={'Container_ID': container_id},
                UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
                ExpressionAttributeValues={':s': 'Awaiting_Rail', ':e': driver},
                ConditionExpression=Attr('Current_Status').eq('Claimed')
            )
            table.delete_item(Key={'Container_ID': f"SPOT#{assigned_spot}"})
            return True

        container = random.choice(export_items) if strategy == "random" else export_items[0]
        container_id = container['Container_ID']
        assigned_spot = container['Assigned_Spot']

        time.sleep(2)

        if unsafe:
            table.update_item(
                Key={'Container_ID': container_id},
                UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
                ExpressionAttributeValues={':s': 'Awaiting_Rail', ':e': driver}
            )
            table.delete_item(Key={'Container_ID': f"SPOT#{assigned_spot}"})
            return True

        try:
            table.update_item(
                Key={'Container_ID': container_id},
                UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
                ExpressionAttributeValues={':s': 'Awaiting_Rail', ':e': driver},
                ConditionExpression=Attr('Current_Status').eq('Parked')
            )
            table.delete_item(Key={'Container_ID': f"SPOT#{assigned_spot}"})
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return True
            raise

        return True

    return False

def run_shift():
    empty_passes = 0
    try:
        while empty_passes < 10:
            moved = move_container()
            if not moved:
                if query_status(table, ['Trackside_Hold', 'Buffer_Hold', 'Rendezvous_Wait', 'Ingate_Hold', 'Claimed']):
                    time.sleep(0.05)
                    continue
                parked = query_status(table, ['Parked'])
                if any(item.get('Direction') == 'Export' for item in parked):
                    time.sleep(0.05)
                    continue
                empty_passes += 1
                time.sleep(0.05)
            else:
                empty_passes = 0
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    run_shift()
