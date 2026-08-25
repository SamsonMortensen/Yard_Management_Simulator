import os
import random
import time

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from config import get_table, scan_all

#Connect to AWS
table = get_table()

#Shift Roster (employee IDs)
active_hostlers = ["EMP-104", "EMP-227", "EMP-309", "EMP-412"]

def claim_strategy():
    return os.environ.get("YMS_CLAIM", "head").lower()

def is_unsafe():
    return os.environ.get("YMS_UNSAFE", "false").lower() == "true"

def move_container():
    # Scan the database for units at the gate
    gate_items = scan_all(
        table,
        FilterExpression=Attr('Current_Status').eq('Ingate_Hold')
    )

    if not gate_items:
        print("Yard is clear. No containers waiting at the gate.")
        return False

    driver = random.choice(active_hostlers)
    strategy = claim_strategy()
    unsafe = is_unsafe()

    if strategy == "dispatch" and not unsafe:
        #Centralized Task Assignment: claim before driving
        container_id = None
        assigned_spot = None
        for candidate in gate_items:
            candidate_id = candidate['Container_ID']
            try:
                table.update_item(
                    Key={'Container_ID': candidate_id},
                    UpdateExpression="set Current_Status = :s, Claimed_By = :e",
                    ExpressionAttributeValues={':s': 'Claimed', ':e': driver},
                    ConditionExpression=Attr('Current_Status').eq('Ingate_Hold')
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

        print(f"Hostler dispatch assigned {container_id} to {driver} (pre-claimed).")
        time.sleep(2) # Simulating physical drive time

        table.update_item(
            Key={'Container_ID': container_id},
            UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
            ExpressionAttributeValues={':s': 'Parked', ':e': driver},
            ConditionExpression=Attr('Current_Status').eq('Claimed')
        )
        print(f"Dropped {container_id} at parking spot {assigned_spot}\n")
        return True

    #Grab container per head or random strategy
    container = random.choice(gate_items) if strategy == "random" else gate_items[0]
    container_id = container['Container_ID']
    assigned_spot = container['Assigned_Spot']

    print(f"Hostler dispatching to Gate... Grabbing {container_id}")
    time.sleep(2) # Simulating physical drive time

    if unsafe:
        #UNSAFE CONTROL MODE: blind update without ConditionExpression.
        #Demonstrates race conditions: both hostlers write to the same record.
        table.update_item(
            Key={'Container_ID': container_id},
            UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
            ExpressionAttributeValues={':s': 'Parked', ':e': driver}
        )
        print(f"[UNSAFE] Dropped {container_id} at spot {assigned_spot} (blind overwrite by {driver})\n")
        return True

    try:
        #GUARDED MODE: Conditional write ensures atomic transition.
        table.update_item(
            Key={'Container_ID': container_id},
            UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
            ExpressionAttributeValues={':s': 'Parked', ':e': driver},
            ConditionExpression=Attr('Current_Status').eq('Ingate_Hold')
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            print(f"{container_id} was already handled by another hostler. Rescanning...\n")
            return True
        raise

    print(f"Dropped {container_id} at parking spot {assigned_spot}\n")
    return True

def run_shift():
    # Run the Hostler Shift
    print("Starting Hostler Shift..")
    try:
        while True:
            moved = move_container()
            if not moved:
                break # Clock out if the gate is empty
            time.sleep(3) # Short break between moves
    except KeyboardInterrupt:
        print("\nShift ended early: hostler clocking out.")


if __name__ == "__main__":
    run_shift()
