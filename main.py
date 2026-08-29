"""Arrivals. Puts units on the ground, by rail and by road.

Two ways a container shows up at the terminal, and they are not the same job:

  Rail arrivals land on a train and start in Trackside_Hold. They are still
  sitting on the railcar. A crane has to lift them off before anybody can
  touch them. These are Imports, headed out the gate to a customer.

  Gate arrivals are outside drivers dropping a box off, so they start in
  Ingate_Hold. These are Exports, headed out on a train.

Every unit gets a parking spot at arrival, and the spot is claimed with a
conditional put on a SPOT#<n> record. Without that claim two gate clerks
running at the same time can hand the same spot to two different containers,
because the table is keyed on Container_ID and knows nothing about spots.

Rail arrivals also carry Railcar_ID and Well_Position. A double stack well
holds a bottom and a top, and the top has to come off first, so the crane
reads Blocked_By to know what it is allowed to lift.
"""
import random
import sys
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

import config
from config import get_table, scan_all

# Initialize the DynamoDB connection
table = get_table()

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

def generate_arrival(assigned_spot, direction='Import'):
    char_map = {'A':10,'B':12,'C':13,'D':14,'E':15,'F':16,'G':17,'H':18,'I':19,'J':20,'K':21,'L':23,'M':24,'N':25,'O':26,'P':27,'Q':28,'R':29,'S':30,'T':31,'U':32,'V':34,'W':35,'X':36,'Y':37,'Z':38}
    for i in range(10): char_map[str(i)] = i
    
    prefix = random.choice(["MSKU", "HLCU", "JBHU", "SCHU"])
    serial = f"{random.randint(100000, 999999)}"
    cid10 = f"{prefix}{serial}"
    check_digit = str((sum(char_map[c] * (2**i) for i, c in enumerate(cid10)) % 11) % 10)
    
    container_id = f"{cid10}{check_digit}"
    eq = "53_Dry_Van" if prefix in ["JBHU", "SCHU"] else random.choice(["40_High_Cube", "20_Standard"])
    
    if direction == 'Import':
        target_dwell = min(random.expovariate(1 / 60.0), 336.0)
    else:
        # Export dwell represents scheduled train build/cutoff window
        target_dwell = round(random.uniform(2.0, 6.0), 4)
    
    return {
        'Container_ID': container_id,
        'Equipment_Type': eq,
        'Current_Status': 'Trackside_Hold' if direction == 'Import' else 'Ingate_Hold',
        'Assigned_Spot': assigned_spot,
        'Direction': direction,
        'Arrival_Time': datetime.now(timezone.utc).isoformat(),
        'Target_Dwell_Hours': str(round(target_dwell, 4))
    }

def seed_yard_inventory(count):
    if count <= 0:
        return
    print(f"Seeding yard with {count} pre-existing parked containers...")
    occupied_spots = get_occupied_spots()
    free_spots = sorted(list(set(range(config.MIN_SPOT, config.MAX_SPOT + 1)) - occupied_spots))
    
    if len(free_spots) < count:
        raise RuntimeError("Not enough free spots to seed inventory.")
        
    now = datetime.now(timezone.utc)
    for i in range(count):
        assigned_spot = free_spots[i]
        try:
            table.put_item(
                Item={'Container_ID': f"SPOT#{assigned_spot}", 'Type': 'Spot_Reservation'},
                ConditionExpression='attribute_not_exists(Container_ID)'
            )
        except ClientError:
            continue

        arr = generate_arrival(assigned_spot, direction='Import')
        target_dwell = float(arr['Target_Dwell_Hours'])
        elapsed = random.uniform(0, target_dwell * 1.5)
        
        arr['Arrival_Time'] = (now - timedelta(hours=elapsed)).isoformat()
        arr['Current_Status'] = 'Parked'
        arr['Parked_By_Employee'] = 'SEED_SYSTEM'
        arr['Arrival_Mode'] = 'Gate'
        arr['Direction'] = 'Import'
        
        try:
            table.put_item(Item=arr)
        except ClientError:
            continue

def push_to_cloud(num_containers, data_file=None):
    print(f"Generating {num_containers} new arrivals...")

    manifest = None
    if data_file:
        import csv
        with open(data_file, "r", encoding="utf-8") as f:
            manifest = list(csv.DictReader(f))
        if num_containers > 0 and len(manifest) > num_containers:
            manifest = manifest[:num_containers]
        elif num_containers == 0:
            num_containers = len(manifest)

    total = num_containers if manifest is None else len(manifest)
    rail_count = 0
    gate_count = 0

    for i in range(total):
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
            
            if manifest:
                row = manifest[i]
                is_rail = row.get('Arrival_Mode') == 'Rail'
                direction = 'Import' if is_rail else 'Export'
                target_dwell = min(random.expovariate(1 / 60.0), 336.0) if direction == 'Import' else round(random.uniform(2.0, 6.0), 4)
                new_container = {
                    'Container_ID': row['Container_ID'],
                    'Equipment_Type': row['Equipment_Type'],
                    'Current_Status': 'Trackside_Hold' if is_rail else 'Ingate_Hold',
                    'Assigned_Spot': assigned_spot,
                    'Arrival_Time': row.get('Arrival_Time') or datetime.now(timezone.utc).isoformat(),
                    'Arrival_Mode': row.get('Arrival_Mode', 'Gate'),
                    'Direction': direction,
                    'Target_Dwell_Hours': str(round(target_dwell, 4)),
                    'Railcar_ID': row.get('Railcar_ID', 'None'),
                    'Well_Position': row.get('Well_Position', 'None'),
                    'Blocked_By': row.get('Blocked_By', 'None'),
                    'Outbound_Train_ID': row.get('Outbound_Train_ID', 'TR-OUT-01' if direction == 'Export' else 'None')
                }
            else:
                is_rail = (i % 2 == 0)
                direction = 'Import' if is_rail else 'Export'
                new_container = generate_arrival(assigned_spot, direction=direction)
                new_container['Arrival_Mode'] = 'Rail' if is_rail else 'Gate'
                new_container['Current_Status'] = 'Trackside_Hold' if is_rail else 'Ingate_Hold'
                
                if is_rail:
                    car_idx = (rail_count // 2) + 1
                    pos = 'Top' if (rail_count % 2 == 0) else 'Bottom'
                    rail_count += 1
                    new_container['Railcar_ID'] = f"TTZX{car_idx:05d}"
                    new_container['Well_Position'] = pos
                    new_container['Blocked_By'] = 'None' if pos == 'Top' else f"TTZX{car_idx:05d}_Top"
                    new_container['Outbound_Train_ID'] = 'None'
                else:
                    car_idx = (gate_count // 2) + 1
                    pos = 'Bottom' if (gate_count % 2 == 0) else 'Top'
                    gate_count += 1
                    new_container['Railcar_ID'] = f"TTZX{car_idx:05d}"
                    new_container['Well_Position'] = pos
                    new_container['Blocked_By'] = 'None'
                    new_container['Outbound_Train_ID'] = 'TR-OUT-01'
            
            try:
                table.put_item(
                    Item=new_container,
                    ConditionExpression='attribute_not_exists(Container_ID)'
                )
                print(f"Arrived: {new_container['Container_ID']} | Mode: {new_container.get('Arrival_Mode')} | Spot: {new_container['Assigned_Spot']}")
                break
            except ClientError as e:
                if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                    table.delete_item(Key={'Container_ID': f"SPOT#{assigned_spot}"})
                    print(f"ID collision on {new_container['Container_ID']}: skipping this arrival.")
                    continue
                raise

if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    push_to_cloud(count)
