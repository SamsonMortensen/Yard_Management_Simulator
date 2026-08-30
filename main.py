"""Create roadbound train arrivals and railbound gate arrivals.

Arrival records describe where a unit came from and how it is expected to
leave. Ground space is planned separately: each container receives a block,
base spot, and tier, then a conditional reservation protects that exact
location from concurrent gate clerks.

Roadbound units arrive on an inbound consist. Their ``Blocked_By`` relationship
preserves top-before-bottom discharge. Railbound units arrive through the road
gate without an assigned well; the outbound crane and train planner choose a
compatible slot when the unit is actually loaded.
"""
import random
import sys
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from config import get_table, scan_all
from flow import flow_label
from yard_topology import choose_ground_location


table = get_table()
DESTINATION_BLOCKS = ("BLOCK_A", "BLOCK_B", "BLOCK_C", "BLOCK_D")


def _check_digit(container_id_10):
    values = {
        'A': 10, 'B': 12, 'C': 13, 'D': 14, 'E': 15, 'F': 16, 'G': 17,
        'H': 18, 'I': 19, 'J': 20, 'K': 21, 'L': 23, 'M': 24, 'N': 25,
        'O': 26, 'P': 27, 'Q': 28, 'R': 29, 'S': 30, 'T': 31, 'U': 32,
        'V': 34, 'W': 35, 'X': 36, 'Y': 37, 'Z': 38,
    }
    values.update({str(i): i for i in range(10)})
    return str((sum(values[c] * (2 ** i) for i, c in enumerate(container_id_10)) % 11) % 10)


def _gross_weight(equipment_type):
    ranges = {
        "20_Standard": (18_000, 52_000),
        "40_High_Cube": (22_000, 66_000),
        "53_Dry_Van": (24_000, 67_000),
    }
    low, high = ranges[equipment_type]
    return random.randint(low, high)


def generate_arrival(planned_departure_mode='Road'):
    prefix = random.choice(["MSKU", "HLCU", "JBHU", "SCHU"])
    serial = f"{random.randint(100000, 999999)}"
    container_id_10 = f"{prefix}{serial}"
    equipment = (
        "53_Dry_Van" if prefix in {"JBHU", "SCHU"}
        else random.choice(["40_High_Cube", "20_Standard"])
    )
    target_dwell = (
        min(random.expovariate(1 / 60.0), 336.0)
        if planned_departure_mode == 'Road'
        else random.uniform(2.0, 6.0)
    )
    return {
        'Container_ID': f"{container_id_10}{_check_digit(container_id_10)}",
        'Equipment_Type': equipment,
        'Gross_Weight_Lbs': _gross_weight(equipment),
        'Current_Status': 'Trackside_Hold' if planned_departure_mode == 'Road' else 'Ingate_Hold',
        'Planned_Departure_Mode': planned_departure_mode,
        'Destination_Block': random.choice(DESTINATION_BLOCKS) if planned_departure_mode == 'Rail' else 'ROAD_CUSTOMER',
        'Arrival_Time': datetime.now(timezone.utc).isoformat(),
        'Target_Dwell_Hours': str(round(target_dwell, 4)),
    }


def _reserve_location(container):
    """Plan and atomically reserve one ground tier, retrying claim races."""
    while True:
        location = choose_ground_location(scan_all(table), container)
        try:
            table.put_item(
                Item={
                    'Container_ID': location['Ground_Reservation_ID'],
                    'Type': 'Ground_Reservation',
                    'Assigned_Spot': location['Assigned_Spot'],
                    'Ground_Tier': location['Ground_Tier'],
                    'Yard_Block': location['Yard_Block'],
                },
                ConditionExpression='attribute_not_exists(Container_ID)',
            )
            container.update(location)
            return
        except ClientError as error:
            if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                raise


def seed_yard_inventory(count):
    if count <= 0:
        return
    print(f"Seeding yard with {count} pre-existing roadbound containers...")
    now = datetime.now(timezone.utc)
    for _ in range(count):
        container = generate_arrival(planned_departure_mode='Road')
        target_dwell = float(container['Target_Dwell_Hours'])
        container.update({
            'Arrival_Time': (now - timedelta(hours=random.uniform(0, target_dwell * 1.5))).isoformat(),
            'Current_Status': 'Parked',
            'Parked_By_Employee': 'SEED_SYSTEM',
            'Arrival_Mode': 'Rail',
        })
        _reserve_location(container)
        table.put_item(Item=container, ConditionExpression='attribute_not_exists(Container_ID)')


def _manifest_container(row):
    arrives_by_rail = row.get('Arrival_Mode') == 'Rail'
    planned_mode = 'Road' if arrives_by_rail else 'Rail'
    equipment = row.get('Equipment_Type', '40_High_Cube')
    generated = generate_arrival(planned_departure_mode=planned_mode)
    generated.update({
        'Container_ID': row['Container_ID'],
        'Equipment_Type': equipment,
        'Gross_Weight_Lbs': int(row.get('Gross_Weight_Lbs') or _gross_weight(equipment)),
        'Current_Status': 'Trackside_Hold' if arrives_by_rail else 'Ingate_Hold',
        'Arrival_Time': row.get('Arrival_Time') or datetime.now(timezone.utc).isoformat(),
        'Arrival_Mode': row.get('Arrival_Mode', 'Gate'),
        'Railcar_ID': row.get('Railcar_ID', 'None') if arrives_by_rail else 'None',
        'Well_Position': row.get('Well_Position', 'None') if arrives_by_rail else 'None',
        'Blocked_By': row.get('Blocked_By', 'None') if arrives_by_rail else 'None',
        'Outbound_Train_ID': 'None' if arrives_by_rail else row.get('Outbound_Train_ID', 'TR-OUT-01'),
        'Destination_Block': row.get('Destination_Block') or (
            random.choice(DESTINATION_BLOCKS) if planned_mode == 'Rail' else 'ROAD_CUSTOMER'
        ),
    })
    return generated


def push_to_cloud(num_containers, data_file=None):
    print(f"Generating {num_containers} new arrivals...")
    manifest = None
    if data_file:
        import csv
        with open(data_file, "r", encoding="utf-8") as source:
            manifest = list(csv.DictReader(source))
        if num_containers > 0:
            manifest = manifest[:num_containers]
        else:
            num_containers = len(manifest)

    total = num_containers if manifest is None else len(manifest)
    inbound_count = 0
    inbound_top_by_car = {}

    for index in range(total):
        while True:
            if manifest is not None:
                container = _manifest_container(manifest[index])
            else:
                arrives_by_rail = index % 2 == 0
                planned_mode = 'Road' if arrives_by_rail else 'Rail'
                container = generate_arrival(planned_departure_mode=planned_mode)
                container['Arrival_Mode'] = 'Rail' if arrives_by_rail else 'Gate'

                if arrives_by_rail:
                    car_index = (inbound_count // 2) + 1
                    position = 'Top' if inbound_count % 2 == 0 else 'Bottom'
                    inbound_count += 1
                    car_id = f"TTZX{car_index:05d}"
                    container.update({
                        'Railcar_ID': car_id,
                        'Well_Position': position,
                        'Blocked_By': 'None' if position == 'Top' else inbound_top_by_car[car_id],
                        'Outbound_Train_ID': 'None',
                    })
                    if position == 'Top':
                        inbound_top_by_car[car_id] = container['Container_ID']
                else:
                    # Railbound wells are assigned at load time after compatibility,
                    # weight, and destination-block checks have passed.
                    container.update({
                        'Railcar_ID': 'None',
                        'Well_Position': 'None',
                        'Blocked_By': 'None',
                        'Outbound_Train_ID': 'TR-OUT-01',
                    })

            _reserve_location(container)
            try:
                table.put_item(
                    Item=container,
                    ConditionExpression='attribute_not_exists(Container_ID)',
                )
                print(
                    f"Arrived: {container['Container_ID']} | {flow_label(container)} | "
                    f"Arrival: {container['Arrival_Mode']} | "
                    f"Ground: B{container['Yard_Block']}-S{container['Assigned_Spot']}-T{container['Ground_Tier']}"
                )
                break
            except ClientError as error:
                table.delete_item(Key={'Container_ID': container['Ground_Reservation_ID']})
                if error.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    raise
                print(f"ID collision on {container['Container_ID']}: regenerating arrival.")


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    push_to_cloud(count)
