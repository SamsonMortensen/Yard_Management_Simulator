import random
import sys
from datetime import datetime, timezone

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

import config
from config import get_table, scan_all

#Initialize the DynamoDB connection
table = get_table()

#Define our equipment types
equipment_types = ["53_Dry_Van", "40_High_Cube", "20_Standard", "Chassis_Bare"]

def get_occupied_spots():
    #A spot is only free once its container has departed
    items = scan_all(
        table,
        FilterExpression=Attr('Current_Status').ne('Departed'),
        ProjectionExpression='Assigned_Spot'
    )
    return {int(item['Assigned_Spot']) for item in items}

def generate_arrival(occupied_spots):
    # Create a dummy container ID
    prefix = random.choice(["MSKU", "JBHT", "SCHN", "EMCU"])
    container_id = f"{prefix}{random.randint(1000000, 9999999)}"

    #Assign a spot no other active container holds
    free_spots = set(range(config.MIN_SPOT, config.MAX_SPOT + 1)) - occupied_spots
    if not free_spots:
        raise RuntimeError("Yard is full: no free spots left to assign.")
    assigned_spot = random.choice(sorted(free_spots))
    occupied_spots.add(assigned_spot)

    #Build the payload
    item = {
        'Container_ID': container_id,
        'Equipment_Type': random.choice(equipment_types),
        'Current_Status': 'Ingate_Hold',
        'Assigned_Spot': assigned_spot,
        'Arrival_Time': datetime.now(timezone.utc).isoformat(),
    }
    return item

def push_to_cloud(num_containers):
    print(f"Generating {num_containers} new arrivals at the gate...")
    occupied_spots = get_occupied_spots()

    for _ in range(num_containers):
        new_container = generate_arrival(occupied_spots)

        try:
            # Push to DynamoDB: refuse to overwrite an existing container record
            table.put_item(
                Item=new_container,
                ConditionExpression='attribute_not_exists(Container_ID)'
            )
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                print(f"ID collision on {new_container['Container_ID']}: skipping this arrival.")
                continue
            raise

        print(f"Arrived: {new_container['Container_ID']} | Spot: {new_container['Assigned_Spot']}")

if __name__ == "__main__":
    # Simulate trucks pulling up to the gate (default 5)
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    push_to_cloud(count)
