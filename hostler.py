"""Move containers between gate, track, and multi-tier ground stacks.

The hostler first services units waiting at the gate or under the crane. After
parking one, it looks for railbound work near that drop location so the return
trip can become a dual cycle. A buried pickup is not allowed to disappear
through its blockers: blockers are relocated top-down, each relocation changes
its ground reservation atomically, and the extra travel is measured.

Four selection modes share the same physical rules. ``head`` and ``random``
demonstrate worker contention, ``dispatch`` claims arrival-sorted work before
travel, and ``adaptive`` uses the same safe claim while an online learner ranks
valid railbound candidates.
"""
import os
import random
import threading
import time

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from adaptive_policy import get_policy
from atomic_ops import transition_and_release
from config import get_table, query_status
from flow import is_railbound
from yard_topology import (
    block_hops,
    gate_block,
    rehandle_for_access,
    track_block,
    yard_block,
)


table = get_table()
active_hostlers = ["EMP-104", "EMP-227", "EMP-309", "EMP-412"]
_stats_lock = threading.Lock()
STATS = {}


def reset_stats():
    global STATS
    with _stats_lock:
        STATS = {
            "ground_moves": 0,
            "railbound_retrievals": 0,
            "dual_cycles": 0,
            "rehandles": 0,
            "block_hops": 0,
            "simulated_hostler_seconds": 0.0,
        }


reset_stats()


def claim_strategy():
    return os.environ.get("YMS_CLAIM", "head").lower()


def is_unsafe():
    return os.environ.get("YMS_UNSAFE", "false").lower() == "true"


def _uses_atomic_claim(strategy):
    return strategy in {"dispatch", "adaptive"}


def _is_claim_conflict(error):
    return error.response['Error']['Code'] in {
        'ConditionalCheckFailedException', 'TransactionCanceledException'
    }


def _arrival_order(item):
    return item.get('Arrival_Time', ''), item.get('Container_ID', '')


def _record_move(hops, rehandles=0, railbound=False, dual_cycle=False):
    seconds_per_hop = float(os.environ.get("HOSTLER_SECONDS_PER_BLOCK", "12"))
    seconds_per_rehandle = float(os.environ.get("REHANDLE_SECONDS", "90"))
    simulated_seconds = hops * seconds_per_hop + rehandles * seconds_per_rehandle
    with _stats_lock:
        STATS["ground_moves"] += 1
        STATS["railbound_retrievals"] += int(railbound)
        STATS["dual_cycles"] += int(dual_cycle)
        STATS["rehandles"] += rehandles
        STATS["block_hops"] += hops
        STATS["simulated_hostler_seconds"] += simulated_seconds
    time.sleep(simulated_seconds)


def _claim_candidate(candidates, driver, expected_status=None, only_first=False):
    """Claim work, then return its authoritative location after the write."""
    for candidate in candidates[:1] if only_first else candidates:
        status = expected_status or candidate['Current_Status']
        try:
            table.update_item(
                Key={'Container_ID': candidate['Container_ID']},
                UpdateExpression="set Current_Status = :s, Claimed_By = :e",
                ExpressionAttributeValues={':s': 'Claimed', ':e': driver},
                ConditionExpression=Attr('Current_Status').eq(status),
            )
            # A blocker may have moved after the queue scan but before this claim.
            # Once the claim lands it cannot move again, so reread the exact ground
            # reservation that the eventual transition must release.
            return table.get_item(
                Key={'Container_ID': candidate['Container_ID']}
            )['Item']
        except ClientError as error:
            if not _is_claim_conflict(error):
                raise
    return None


def _release_claim(container_id):
    """Return abandoned work to the queue without overwriting a completed move."""
    try:
        table.update_item(
            Key={'Container_ID': container_id},
            UpdateExpression="set Current_Status = :s",
            ExpressionAttributeValues={':s': 'Parked'},
            ConditionExpression=Attr('Current_Status').eq('Claimed'),
        )
    except ClientError as error:
        if not _is_claim_conflict(error):
            raise


def _park_inbound(driver, strategy, unsafe):
    waiting = query_status(table, ['Ingate_Hold', 'Buffer_Hold', 'Rendezvous_Wait'])
    waiting.sort(key=_arrival_order)
    if not waiting:
        return None

    if _uses_atomic_claim(strategy) and not unsafe:
        container = _claim_candidate(waiting, driver)
        if container is None:
            return False
        expected_status = 'Claimed'
    else:
        container = random.choice(waiting) if strategy == 'random' else waiting[0]
        expected_status = container['Current_Status']

    source = track_block() if container.get('Arrival_Mode') == 'Rail' else gate_block()
    hops = abs(source - yard_block(container['Assigned_Spot']))
    _record_move(hops)

    values = {':s': 'Parked', ':e': driver}
    if unsafe:
        table.update_item(
            Key={'Container_ID': container['Container_ID']},
            UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
            ExpressionAttributeValues=values,
        )
        return container
    try:
        table.update_item(
            Key={'Container_ID': container['Container_ID']},
            UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
            ExpressionAttributeValues=values,
            ConditionExpression=Attr('Current_Status').eq(expected_status),
        )
        return container
    except ClientError as error:
        if _is_claim_conflict(error):
            return False
        raise


def _select_railbound(candidates, strategy, origin_spot):
    candidates.sort(key=_arrival_order)
    if strategy == 'adaptive':
        return get_policy().choose(candidates, current_spot=origin_spot)
    if strategy == 'random':
        return random.choice(candidates), None
    if origin_spot is not None:
        origin_block = yard_block(origin_spot)
        nearby = [item for item in candidates if yard_block(item['Assigned_Spot']) == origin_block]
        if nearby:
            return nearby[0], None
    return candidates[0], None


def _retrieve_railbound(driver, strategy, unsafe, origin_spot=None):
    candidates = [item for item in query_status(table, ['Parked']) if is_railbound(item)]
    if not candidates:
        return False

    selected, decision = _select_railbound(candidates, strategy, origin_spot)
    if _uses_atomic_claim(strategy) and not unsafe:
        selected = _claim_candidate(
            [selected] if strategy == 'adaptive' else candidates,
            driver,
            expected_status='Parked',
            only_first=strategy == 'adaptive',
        )
        if selected is None:
            if strategy == 'adaptive':
                get_policy().observe(decision, completed=False)
            return True
        expected_status = 'Claimed'
    else:
        expected_status = 'Parked'

    try:
        rehandle = rehandle_for_access(table, selected, driver)
        start_block = yard_block(origin_spot) if origin_spot is not None else track_block()
        pickup_block = yard_block(selected['Assigned_Spot'])
        travel_hops = abs(start_block - pickup_block) + abs(pickup_block - track_block())
        total_hops = travel_hops + rehandle['block_hops']
        _record_move(
            total_hops,
            rehandles=rehandle['rehandles'],
            railbound=True,
            dual_cycle=origin_spot is not None,
        )

        values = {
            ':s': 'Awaiting_Rail',
            ':e': driver,
            ':r': int(selected.get('Rehandle_Count', 0)) + rehandle['rehandles'],
        }
        update = "set Current_Status = :s, Parked_By_Employee = :e, Rehandle_Count = :r"
        if unsafe:
            table.update_item(
                Key={'Container_ID': selected['Container_ID']},
                UpdateExpression=update,
                ExpressionAttributeValues=values,
            )
            table.delete_item(Key={'Container_ID': selected.get(
                'Ground_Reservation_ID', f"SPOT#{selected['Assigned_Spot']}"
            )})
        else:
            transition_and_release(
                table,
                selected['Container_ID'],
                selected['Assigned_Spot'],
                update,
                values,
                expected_status=expected_status,
                reservation_key=selected.get('Ground_Reservation_ID'),
                expected_reservation=selected.get('Ground_Reservation_ID'),
            )

        if strategy == 'adaptive':
            decision.update({
                'observed_block_hops': total_hops,
                'rehandles': rehandle['rehandles'],
            })
            get_policy().observe(decision, completed=True)
        return True
    except ClientError as error:
        if expected_status == 'Claimed':
            _release_claim(selected['Container_ID'])
        if strategy == 'adaptive':
            get_policy().observe(decision, completed=False)
        if _is_claim_conflict(error):
            return True
        raise


def move_container():
    driver = random.choice(active_hostlers)
    strategy = claim_strategy()
    unsafe = is_unsafe()

    parked = _park_inbound(driver, strategy, unsafe)
    if parked is not None:
        if parked is not False:
            _retrieve_railbound(
                driver,
                strategy,
                unsafe,
                origin_spot=parked['Assigned_Spot'],
            )
        return True
    return _retrieve_railbound(driver, strategy, unsafe)


def run_shift():
    empty_passes = 0
    try:
        while empty_passes < 10:
            if move_container():
                empty_passes = 0
            elif query_status(table, [
                'Trackside_Hold', 'Buffer_Hold', 'Rendezvous_Wait',
                'Ingate_Hold', 'Claimed',
            ]):
                pass
            elif any(is_railbound(item) for item in query_status(table, ['Parked'])):
                pass
            else:
                empty_passes += 1
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run_shift()
