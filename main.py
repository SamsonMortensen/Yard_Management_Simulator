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
    # A spot is occupied if an active container holds it OR if a SPOT# record exists
    items = scan_all(table, ProjectionExpression='Assigned_Spot, Container_ID, Current_Status')
    occupied = set()
    for item in items:
        cid = item.get('Container_ID', '')
        if cid.startswith('SPOT#'):
            occupied.add(int(cid.split('#')[1]))
        elif item.get('Current_Status') != 'Departed' and 'Assigned_Spot' in item:
            occupied.add(int(item['Assigned_Spot']))
    return occupied

def generate_arrival(assigned_spot):
    prefix = random.choice(["MSKU", "JBHT", "SCHN", "EMCU"])
    container_id = f"{prefix}{random.randint(1000000, 9999999)}"
    return {
        'Container_ID': container_id,
        'Equipment_Type': random.choice(equipment_types),
        'Current_Status': 'Ingate_Hold',
        'Assigned_Spot': assigned_spot,
        'Arrival_Time': datetime.now(timezone.utc).isoformat(),
    }

def push_to_cloud(num_containers):
    print(f"Generating {num_containers} new arrivals at the gate...")

    for _ in range(num_containers):
        while True:
            occupied_spots = get_occupied_spots()
            free_spots = set(range(config.MIN_SPOT, config.MAX_SPOT + 1)) - occupied_spots
            if not free_spots:
                raise RuntimeError("Yard is full: no free spots left to assign.")
            
            assigned_spot = random.choice(sorted(free_spots))
            
            try:
                # Atomically claim the spot
                table.put_item(
                    Item={'Container_ID': f"SPOT#{assigned_spot}", 'Type': 'Spot_Reservation'},
                    ConditionExpression='attribute_not_exists(Container_ID)'
                )
            except ClientError as e:
                if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                    continue
                raise
            
            new_container = generate_arrival(assigned_spot)
            
            try:
                table.put_item(
                    Item=new_container,
                    ConditionExpression='attribute_not_exists(Container_ID)'
                )
                print(f"Arrived: {new_container['Container_ID']} | Spot: {new_container['Assigned_Spot']}")
                break
            except ClientError as e:
                if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                    # Roll back spot claim if container ID collides (rare)
                    table.delete_item(Key={'Container_ID': f"SPOT#{assigned_spot}"})
                    print(f"ID collision on {new_container['Container_ID']}: skipping this arrival.")
                    continue
                raise

if __name__ == "__main__":
    # Simulate trucks pulling up to the gate (default 5)
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    push_to_cloud(count)
