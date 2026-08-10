import random
import time

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from config import get_table, scan_all

#Connect to AWS
table = get_table()

#Shift Roster (employee IDs)
active_hostlers = ["EMP-104", "EMP-227", "EMP-309", "EMP-412"]

def move_container():
    # Scan the database for units at the gate
    gate_items = scan_all(
        table,
        FilterExpression=Attr('Current_Status').eq('Ingate_Hold')
    )

    if not gate_items:
        print("Yard is clear. No containers waiting at the gate.")
        return False

    #Grab the first container in line
    container = gate_items[0]
    container_id = container['Container_ID']
    assigned_spot = container['Assigned_Spot']

    #Assign a driver
    driver = random.choice(active_hostlers)

    print(f"Hostler dispatching to Gate... Grabbing {container_id}")
    time.sleep(2) # Simulating the physical drive time

    try:
        #Conditional write: only lands if the unit is still at the gate.
        #If another hostler parked it during our drive, DynamoDB rejects this.
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
        print("\nShift ended early — hostler clocking out.")


if __name__ == "__main__":
    run_shift()
