"""The crane. Works both directions on the track.

Discharge pulls inbound units off the train. Loading builds the outbound
train. Same machine, opposite precedence rules, and getting them backwards
is a real mistake:

  Discharge: the top of a double stack well comes off before the bottom.
  A unit sitting in Blocked_By is not liftable until its blocker is gone.

  Loading: the reverse. The bottom of the well has to be set before the top
  goes on over it.

The default sweep is tops first across the whole track, then back for the
bottoms, which lets the ground crew pull cone locks off the bottoms while
the crane keeps working. The cost is a second trip down the train. Set
YMS_SWEEP_MODE=well_by_well to work one well at a time instead and pay a
lock wait instead of the travel. Which one wins depends on whether lock
removal is slower than crane travel. benchmark_sweep.py measures it.

Two ways a lifted unit leaves the crane. Coupled means a hostler already
backed a chassis under it, so it goes to Rendezvous_Wait and that hostler
takes it. Decoupled means it lands on a chassis staged trackside and sits
in Buffer_Hold until somebody comes for it.
"""
import os
import random
import time

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from config import get_table, query_status
from flow import is_railbound
import train

table = get_table()

# Crane physics telemetry (simulated clock)
STATS = {
    "lifts": 0,
    "simulated_crane_seconds": 0.0,
    "travel_seconds": 0.0,
    "lock_wait_seconds": 0.0,
    "hoist_seconds": 0.0,
}

def reset_stats():
    STATS["lifts"] = 0
    STATS["simulated_crane_seconds"] = 0.0
    STATS["travel_seconds"] = 0.0
    STATS["lock_wait_seconds"] = 0.0
    STATS["hoist_seconds"] = 0.0

active_cranes = ["CRANE-01", "CRANE-02", "CRANE-03", "CRANE-04"]

def claim_strategy():
    return os.environ.get("YMS_CLAIM", "head").lower()

def is_unsafe():
    return os.environ.get("YMS_UNSAFE", "false").lower() == "true"

def uses_atomic_claim():
    return claim_strategy() in ("dispatch", "adaptive")

def sweep_mode():
    return os.environ.get("YMS_SWEEP_MODE", "tops_first").lower()

def crane_travel_delay():
    return float(os.environ.get("CRANE_TRAVEL_DELAY", "0.0"))

def cone_lock_delay():
    return float(os.environ.get("CONE_LOCK_DELAY", "0.0"))

def move_container():
    driver = random.choice(active_cranes)
    strategy = claim_strategy()
    unsafe = is_unsafe()
    smode = sweep_mode()

    # 1. DISCHARGE INBOUND TRAIN (Trackside_Hold -> Buffer/Rendezvous)
    track_items = query_status(table, ['Trackside_Hold'])
    if track_items:
        claimed_items = query_status(table, ['Claimed'])
        track_ids = {item['Container_ID'] for item in track_items + claimed_items}
        
        eligible_items = []
        for item in track_items:
            blocker = item.get('Blocked_By', 'None')
            if blocker == 'None' or blocker not in track_ids:
                eligible_items.append(item)
                
        if eligible_items:
            if smode == "tops_first":
                eligible_items.sort(key=lambda x: 0 if x.get('Well_Position') in ['Top', 'Single'] else 1)
            else:
                eligible_items.sort(key=lambda x: (x.get('Railcar_ID', ''), 0 if x.get('Well_Position') in ['Top', 'Single'] else 1))
            
            container = None
            container_id = None
            assigned_spot = None

            if uses_atomic_claim() and not unsafe:
                for candidate in eligible_items:
                    candidate_id = candidate['Container_ID']
                    try:
                        table.update_item(
                            Key={'Container_ID': candidate_id},
                            UpdateExpression="set Current_Status = :s, Claimed_By = :e",
                            ExpressionAttributeValues={':s': 'Claimed', ':e': driver},
                            ConditionExpression=Attr('Current_Status').eq('Trackside_Hold')
                        )
                        container_id = candidate_id
                        assigned_spot = candidate['Assigned_Spot']
                        container = candidate
                        break
                    except ClientError as e:
                        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                            continue
                        raise

                if not container_id:
                    return True
            else:
                container = random.choice(eligible_items) if strategy == "random" else eligible_items[0]
                container_id = container['Container_ID']
                assigned_spot = container['Assigned_Spot']

            pos = container.get('Well_Position', 'Single')
            hoist_time = 45.0
            travel_time = 0.0
            lock_wait_time = 0.0

            if pos in ['Top', 'Single']:
                travel_time = crane_travel_delay()
            else:
                if smode == "tops_first":
                    travel_time = crane_travel_delay()
                else:
                    lock_wait_time = cone_lock_delay()

            cycle_time = travel_time + lock_wait_time + hoist_time
            STATS["lifts"] += 1
            STATS["travel_seconds"] += travel_time
            STATS["lock_wait_seconds"] += lock_wait_time
            STATS["hoist_seconds"] += hoist_time
            STATS["simulated_crane_seconds"] += cycle_time

            if cycle_time > 0 and os.environ.get('YMS_SWEEP_BENCHMARK') != 'true':
                time.sleep(cycle_time)

            is_coupled = (random.random() < 0.5) if os.environ.get('YMS_SWEEP_BENCHMARK') != 'true' else False
            next_status = 'Rendezvous_Wait' if is_coupled else 'Buffer_Hold'
            expected_status = 'Claimed' if uses_atomic_claim() else 'Trackside_Hold'

            if unsafe:
                table.update_item(
                    Key={'Container_ID': container_id},
                    UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
                    ExpressionAttributeValues={':s': next_status, ':e': driver}
                )
                return True

            try:
                table.update_item(
                    Key={'Container_ID': container_id},
                    UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
                    ConditionExpression=Attr('Current_Status').eq(expected_status),
                    ExpressionAttributeValues={':s': next_status, ':e': driver}
                )
            except ClientError as e:
                if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                    return True
                raise

            return True

    # 2. LOAD OUTBOUND TRAIN (Awaiting_Rail -> Loaded_Rail)
    awaiting_items = query_status(table, ['Awaiting_Rail'])
    if awaiting_items:
        outbound_train = train.get_outbound_train()
        
        # The train planner filters by well length, stack weight, destination
        # block, and bottom/top support before the crane claims a unit.
        eligible_for_loading = []
        for item in awaiting_items:
            plan = outbound_train.find_load_plan(item)
            if plan:
                eligible_for_loading.append((item, plan))

        if not eligible_for_loading:
            return False

        eligible_for_loading.sort(
            key=lambda entry: 1 if entry[1][1] == 'Top' else 0
        )

        container = None
        container_id = None

        if uses_atomic_claim() and not unsafe:
            for candidate, _ in eligible_for_loading:
                candidate_id = candidate['Container_ID']
                try:
                    table.update_item(
                        Key={'Container_ID': candidate_id},
                        UpdateExpression="set Current_Status = :s, Claimed_By = :e",
                        ExpressionAttributeValues={':s': 'Claimed', ':e': driver},
                        ConditionExpression=Attr('Current_Status').eq('Awaiting_Rail')
                    )
                    container_id = candidate_id
                    container = candidate
                    break
                except ClientError as e:
                    if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                        continue
                    raise

            if not container_id:
                return True
        else:
            container = (
                random.choice(eligible_for_loading)[0]
                if strategy == "random" else eligible_for_loading[0][0]
            )
            container_id = container['Container_ID']

        plan = outbound_train.find_load_plan(container)
        if plan is None:
            if uses_atomic_claim():
                table.update_item(
                    Key={'Container_ID': container_id},
                    UpdateExpression="set Current_Status = :s",
                    ExpressionAttributeValues={':s': 'Awaiting_Rail'},
                    ConditionExpression=Attr('Current_Status').eq('Claimed')
                )
            return True
        car_id, pos = plan

        hoist_time = 45.0
        cycle_time = hoist_time + crane_travel_delay()
        STATS["lifts"] += 1
        STATS["hoist_seconds"] += hoist_time
        STATS["simulated_crane_seconds"] += cycle_time

        if cycle_time > 0 and os.environ.get('YMS_SWEEP_BENCHMARK') != 'true':
            time.sleep(cycle_time)

        expected_status = 'Claimed' if uses_atomic_claim() else 'Awaiting_Rail'

        if unsafe:
            try:
                outbound_train.load_container(
                    car_id, pos, container_id,
                    container.get('Equipment_Type'),
                    container.get('Gross_Weight_Lbs'),
                    container.get('Destination_Block'),
                )
            except ValueError:
                return True
            table.update_item(
                Key={'Container_ID': container_id},
                UpdateExpression=(
                    "set Current_Status = :s, Parked_By_Employee = :e, "
                    "Railcar_ID = :c, Well_Position = :p"
                ),
                ExpressionAttributeValues={
                    ':s': 'Loaded_Rail', ':e': driver, ':c': car_id, ':p': pos,
                }
            )
            print(f"[UNSAFE] Loaded railbound unit {container_id} onto railcar {car_id} ({pos})\n")
            return True

        slot_reserved = False
        try:
            outbound_train.load_container(
                car_id, pos, container_id,
                container.get('Equipment_Type'),
                container.get('Gross_Weight_Lbs'),
                container.get('Destination_Block'),
            )
            slot_reserved = True
            table.update_item(
                Key={'Container_ID': container_id},
                UpdateExpression=(
                    "set Current_Status = :s, Parked_By_Employee = :e, "
                    "Railcar_ID = :c, Well_Position = :p"
                ),
                ConditionExpression=Attr('Current_Status').eq(expected_status),
                ExpressionAttributeValues={
                    ':s': 'Loaded_Rail', ':e': driver, ':c': car_id, ':p': pos,
                }
            )
            print(f"Loaded railbound unit {container_id} onto railcar {car_id} ({pos}) by {driver}\n")
        except ClientError as e:
            if slot_reserved:
                outbound_train.unload_container(car_id, pos, container_id)
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return True
            raise
        except ValueError:
            # Another crane reserved this physical slot first. The claimed unit
            # is returned to Awaiting_Rail so a later valid slot can take it.
            if uses_atomic_claim():
                table.update_item(
                    Key={'Container_ID': container_id},
                    UpdateExpression="set Current_Status = :s",
                    ExpressionAttributeValues={':s': 'Awaiting_Rail'},
                    ConditionExpression=Attr('Current_Status').eq('Claimed')
                )
            return True

        return True

    return False

def run_shift():
    empty_passes = 0
    while empty_passes < 10:
        moved = move_container()
        if not moved:
            active = query_status(table, ['Trackside_Hold', 'Buffer_Hold', 'Rendezvous_Wait', 'Ingate_Hold', 'Claimed'])
            awaiting = query_status(table, ['Awaiting_Rail'])
            outbound_train = train.get_outbound_train()
            loadable = any(outbound_train.find_load_plan(item) for item in awaiting)
            if active or loadable:
                time.sleep(0.05)
                continue
            parked = query_status(table, ['Parked'])
            if any(is_railbound(item) for item in parked):
                time.sleep(0.05)
                continue
            empty_passes += 1
            time.sleep(0.05)
        else:
            empty_passes = 0
            time.sleep(0.05)

if __name__ == "__main__":
    run_shift()
