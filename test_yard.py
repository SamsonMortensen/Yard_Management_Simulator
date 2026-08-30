"""Tests for the in-memory DynamoDB stand-in and the yard simulator.

Runs the test suite without an AWS account:

    pip install -r requirements.txt
    python test_yard.py
"""
import os
import threading
from datetime import datetime, timedelta, timezone

# Force memory backend for tests
os.environ["YMS_BACKEND"] = "memory"

import mock_dynamo
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError


PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")


def _container(cid="MSKU1234567", status="Ingate_Hold", spot=1001,
               equip="53_Dry_Van", arrival="2026-04-12T08:00:00+00:00", direction="Import"):
    return {
        "Container_ID": cid,
        "Current_Status": status,
        "Assigned_Spot": spot,
        "Equipment_Type": equip,
        "Arrival_Time": arrival,
        "Direction": direction,
        "Planned_Departure_Mode": "Rail" if direction == "Export" else "Road",
    }


# Condition operators ----------------------------------------------------

def test_condition_operators():
    item = {"Current_Status": "Ingate_Hold", "Assigned_Spot": 1500,
            "Equipment_Type": "53_Dry_Van", "Arrival_Time": "2026-04-12"}

    check("eq matches", mock_dynamo.matches(item, Attr("Current_Status").eq("Ingate_Hold")))
    check("eq rejects", not mock_dynamo.matches(item, Attr("Current_Status").eq("Parked")))
    check("ne matches", mock_dynamo.matches(item, Attr("Current_Status").ne("Departed")))
    check("lt/gt on numbers",
          mock_dynamo.matches(item, Attr("Assigned_Spot").gt(1000))
          and mock_dynamo.matches(item, Attr("Assigned_Spot").lt(2000)))
    check("between", mock_dynamo.matches(item, Attr("Assigned_Spot").between(1000, 2000)))
    check("begins_with", mock_dynamo.matches(item, Attr("Equipment_Type").begins_with("53")))
    check("attribute_exists", mock_dynamo.matches(item, Attr("Assigned_Spot").exists()))
    check("attribute_not_exists on a missing field",
          mock_dynamo.matches(item, Attr("Dwell_Time_Hours").not_exists()))
    check("is_in matches when present",
          mock_dynamo.matches(item, Attr("Current_Status").is_in(["Ingate_Hold", "Buffer_Hold"])))
    check("is_in rejects when absent",
          not mock_dynamo.matches(item, Attr("Current_Status").is_in(["Parked", "Departed"])))
    check("is_in on missing attribute is False",
          not mock_dynamo.matches(item, Attr("Missing_Attr").is_in(["A", "B"])))
    check("a None condition matches everything", mock_dynamo.matches(item, None))


def test_missing_attribute_is_not_a_match():
    item = {"Container_ID": "MSKU1234567"}
    check("comparison against an absent attribute is False",
          not mock_dynamo.matches(item, Attr("Assigned_Spot").gt(1000)))


def test_unsupported_operator_raises():
    class FakeCondition:
        def get_expression(self):
            return {"operator": "quantum_entangled", "values": []}

    try:
        mock_dynamo.matches({}, FakeCondition())
        check("an unimplemented operator raises rather than matching", False,
              "should have raised NotImplementedError")
    except NotImplementedError:
        check("an unimplemented operator raises rather than matching", True)


# Update expressions ------------------------------------------------------

def test_apply_update():
    item = {"Container_ID": "MSKU1234567", "Current_Status": "Ingate_Hold"}
    mock_dynamo._apply_set_expression(
        item,
        "set Current_Status = :s, Parked_By_Employee = :e",
        {":s": "Parked", ":e": "EMP-309"},
    )
    check("SET applies every assignment",
          item["Current_Status"] == "Parked"
          and item["Parked_By_Employee"] == "EMP-309")

    try:
        mock_dynamo._apply_set_expression(item, "REMOVE Current_Status", {})
        check("a non-SET update raises rather than being ignored", False)
    except NotImplementedError:
        check("a non-SET update raises rather than being ignored", True)

    try:
        mock_dynamo._apply_set_expression(item, "set Current_Status = :missing", {})
        check("a missing ExpressionAttributeValue raises", False)
    except KeyError:
        check("a missing ExpressionAttributeValue raises", True)


# Conditional writes ------------------------------------------------------

def test_put_refuses_to_overwrite():
    table = mock_dynamo.reset_shared_table()
    table.put_item(Item=_container("MSKU1234567"))

    try:
        table.put_item(
            Item=_container("MSKU1234567", spot=2000),
            ConditionExpression="attribute_not_exists(Container_ID)",
        )
        check("a duplicate container ID is refused", False)
    except ClientError as e:
        check("a duplicate container ID is refused",
              e.response["Error"]["Code"] == "ConditionalCheckFailedException")

    check("the original record is untouched",
          table.get_item(Key={"Container_ID": "MSKU1234567"})["Item"]["Assigned_Spot"] == 1001)
    check("the conflict is counted", table.stats["put_conflicts"] == 1)


def test_update_requires_the_expected_status():
    table = mock_dynamo.reset_shared_table()
    table.put_item(Item=_container("MSKU1234567", status="Ingate_Hold"))

    table.update_item(
        Key={"Container_ID": "MSKU1234567"},
        UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
        ExpressionAttributeValues={":s": "Parked", ":e": "EMP-309"},
        ConditionExpression=Attr("Current_Status").eq("Ingate_Hold"),
    )
    check("a valid transition lands",
          table.get_item(Key={"Container_ID": "MSKU1234567"})["Item"]["Current_Status"] == "Parked")

    try:
        table.update_item(
            Key={"Container_ID": "MSKU1234567"},
            UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
            ExpressionAttributeValues={":s": "Parked", ":e": "EMP-412"},
            ConditionExpression=Attr("Current_Status").eq("Ingate_Hold"),
        )
        check("parking an already-parked unit is refused", False)
    except ClientError as e:
        check("parking an already-parked unit is refused",
              e.response["Error"]["Code"] == "ConditionalCheckFailedException")

    check("the first hostler keeps the credit",
          table.get_item(Key={"Container_ID": "MSKU1234567"})["Item"]["Parked_By_Employee"] == "EMP-309")


def test_update_on_a_missing_item_is_refused():
    table = mock_dynamo.reset_shared_table()
    try:
        table.update_item(
            Key={"Container_ID": "DOESNOTEXIST"},
            UpdateExpression="set Current_Status = :s",
            ExpressionAttributeValues={":s": "Parked"},
            ConditionExpression=Attr("Current_Status").eq("Ingate_Hold"),
        )
        check("updating a container that does not exist is refused", False)
    except ClientError as e:
        check("updating a container that does not exist is refused",
              e.response["Error"]["Code"] == "ConditionalCheckFailedException")


# Scans and pagination ----------------------------------------------------

def test_scan_paginates_and_filters():
    table = mock_dynamo.reset_shared_table(page_size=5)
    for i in range(12):
        status = "Ingate_Hold" if i % 2 == 0 else "Parked"
        table.put_item(Item=_container(f"AAAA{i:07d}", status=status))

    from config import scan_all

    all_holding = scan_all(table, FilterExpression=Attr("Current_Status").eq("Ingate_Hold"))
    check("pagination returns every row", len(all_holding) == 6, f"got {len(all_holding)}")
    check("pagination actually spanned multiple pages", table.stats["scan_pages"] > 1,
          f"{table.stats['scan_pages']} pages")
    check("the filter selects the right rows",
          all(item["Current_Status"] == "Ingate_Hold" for item in all_holding))
    check("no row is returned twice or skipped",
          len({item["Container_ID"] for item in all_holding}) == 6)


def test_scan_projection():
    table = mock_dynamo.reset_shared_table()
    table.put_item(Item=_container("BBBB1111111", spot=1234))
    res = table.scan(ProjectionExpression="Assigned_Spot")
    check("projection returns only the requested field",
          res["Items"] == [{"Assigned_Spot": 1234}], f"{res['Items']}")


def test_scan_counts_rows_read_before_filtering():
    table = mock_dynamo.reset_shared_table()
    for i in range(10):
        table.put_item(Item=_container(f"CCCC{i:07d}", status="Departed" if i < 9 else "Parked"))

    from config import scan_all
    table.stats["items_read_by_scans"] = 0
    parked = scan_all(table, FilterExpression=Attr("Current_Status").eq("Parked"))
    check("one row matched but the whole table was read",
          len(parked) == 1 and table.stats["items_read_by_scans"] == 10,
          f"matched={len(parked)} read={table.stats['items_read_by_scans']}")


# Concurrency -------------------------------------------------------------

def test_no_container_is_parked_twice_under_contention():
    table = mock_dynamo.reset_shared_table()
    table.put_item(Item=_container("FFFF4444444", status="Ingate_Hold"))

    winners = []
    losers = []

    def _hostler(emp):
        try:
            table.update_item(
                Key={"Container_ID": "FFFF4444444"},
                UpdateExpression="set Current_Status = :s, Parked_By_Employee = :e",
                ExpressionAttributeValues={":s": "Parked", ":e": emp},
                ConditionExpression=Attr("Current_Status").eq("Ingate_Hold"),
            )
            winners.append(emp)
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                losers.append(emp)
            else:
                raise

    threads = [threading.Thread(target=_hostler, args=(f"EMP-{i:03d}",)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    check("exactly one thread parked the container", len(winners) == 1, f"{winners}")
    check("every other thread got a conditional failure", len(losers) == 11, f"{len(losers)}")
    check("the stored record names the winner",
          table.get_item(Key={"Container_ID": "FFFF4444444"})["Item"]["Parked_By_Employee"]
          == winners[0])
    check("the conflicts were counted", table.stats["update_conflicts"] == 11,
          f"{table.stats['update_conflicts']}")


# Terminal Appointment System ---------------------------------------------

def test_appointment_rules():
    table = mock_dynamo.reset_shared_table()
    import dispatch_check
    import io
    from contextlib import redirect_stdout

    table.put_item(Item=_container("GGGG5555555", status="Parked", spot=1234, direction="Import"))
    table.put_item(Item=_container("EXPT1111111", status="Parked", spot=1235, direction="Export"))
    table.put_item(Item=_container("HHHH6666666", status="Ingate_Hold", direction="Import"))
    table.put_item(Item=_container("JJJJ8888888", status="Claimed", direction="Import"))
    table.put_item(Item=_container("IIII7777777", status="Departed", direction="Import"))

    with redirect_stdout(io.StringIO()):
        approved_roadbound = dispatch_check.check_appointment("GGGG5555555")
        denied_railbound = dispatch_check.check_appointment("EXPT1111111")
        on_wheels = dispatch_check.check_appointment("HHHH6666666")
        moving = dispatch_check.check_appointment("JJJJ8888888")
        gone = dispatch_check.check_appointment("IIII7777777")
        missing = dispatch_check.check_appointment("ZZZZ9999999")

    check("a grounded roadbound unit is approved", approved_roadbound)
    check("a railbound unit staged for train is denied road pickup", not denied_railbound)
    check("a unit still on wheels is pending", not on_wheels)
    check("a unit actively moving/claimed is pending", not moving)
    check("an already-departed unit is denied", not gone)
    check("a unit that was never here is denied (the dry run)", not missing)


# End to end --------------------------------------------------------------

def test_full_shift_is_exactly_once():
    """Every container processed through full bidirectional lifecycle under dual-flow model."""
    import simulate
    result = simulate.run_shift(containers=20, hostlers=3, cranes=2, outgates=1,
                                claim="head", speed=0.0, seed=7)
    check("yard holds standing population",
          result["standing_population"], f"{result['statuses']}")
    check("every transition recorded exactly once under dual-flow model",
          result["exactly_once"],
          f"{result['lifecycle_audit']['problems']}")
    check("contention actually occurred", result["park_conflicts"] > 0,
          f"{result['park_conflicts']}")
    check("the TAS denied every dry-run attempt",
          result["dry_runs_denied"] == result["dry_run_attempts"],
          f"{result['dry_runs_denied']}/{result['dry_run_attempts']}")
    check("the TAS verified grounded units before departure",
          result["tas_verified"])


def test_unsafe_mode_causes_data_corruption():
    """Without conditional writes, blind updates overwrite records and cause race conditions."""
    import simulate
    result = simulate.run_shift(containers=20, hostlers=3, cranes=2, outgates=1,
                                claim="head", unsafe=True, speed=0.0, seed=7)
    check("blind updates create duplicate parking writes",
          result["double_park_writes"] > 0,
          f"duplicate writes: {result['double_park_writes']}")


def test_claim_strategy_changes_contention():
    """Claiming the same sorted queue head increases contention."""
    import simulate
    head = simulate.repeat_shifts(runs=3, containers=20, hostlers=3, cranes=2, outgates=1,
                                  claim="head", speed=0.0, seed=11)
    rand = simulate.repeat_shifts(runs=3, containers=20, hostlers=3, cranes=2, outgates=1,
                                  claim="random", speed=0.0, seed=11)
    check("claiming the head produces substantially more conflicts",
          head["mean_conflicts"] > rand["mean_conflicts"] * 2,
          f"head {head['mean_conflicts']:.1f} vs random {rand['mean_conflicts']:.1f}")
    check("correctness holds under both strategies",
          head["all_correct"] and rand["all_correct"])


def test_dispatch_strategy_eliminates_parking_conflicts():
    """Arrival-sorted dispatch reserves moves atomically before the drive."""
    import simulate
    disp = simulate.run_shift(containers=20, hostlers=3, cranes=2, outgates=1,
                              claim="dispatch", speed=0.0, seed=15)
    check("dispatch strategy achieves zero parking conflicts",
          disp["park_conflicts"] == 0, f"{disp['park_conflicts']}")
    check("dispatch strategy maintains standing population and exactly-once execution",
          disp["standing_population"] and disp["exactly_once"])


def test_concurrent_gate_clerks_spot_collision():
    """Concurrent clerks must never create two live claims on one ground tier."""
    table = mock_dynamo.reset_shared_table()
    import main as ingate_engine
    import threading, sys, os

    def run_clerk():
        ingate_engine.push_to_cloud(15)

    old_stdout = sys.stdout
    sys.stdout = open(os.devnull, 'w')
    
    t1 = threading.Thread(target=run_clerk)
    t2 = threading.Thread(target=run_clerk)
    try:
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    finally:
        sys.stdout.close()
        sys.stdout = old_stdout

    from yard_topology import is_reservation_id
    all_items = table.all_items()
    items = [i for i in all_items if not is_reservation_id(i.get("Container_ID", ""))]
    live_reservations = {
        i["Container_ID"] for i in all_items
        if i.get("Type") == "Ground_Reservation"
    }
    claimed_reservations = {
        i.get("Ground_Reservation_ID") for i in items
        if i.get("Ground_Reservation_ID") and i.get("Current_Status") != "Departed"
    }
    tiers_valid = all(1 <= int(i.get("Ground_Tier", 1)) <= 3 for i in items)
    check("conditional ground allocation prevents duplicate spot-tier claims",
          claimed_reservations == live_reservations and tiers_valid,
          f"containers={len(claimed_reservations)}, reservations={len(live_reservations)}")


def test_stack_access_rehandles_blockers():
    """Retrieving a buried unit moves its blocker and preserves both claims."""
    from yard_topology import rehandle_for_access, reservation_id

    table = mock_dynamo.reset_shared_table()
    target = {
        **_container("STACK000001", status="Claimed", spot=1500),
        "Ground_Tier": 1,
        "Ground_Reservation_ID": reservation_id(1500, 1),
        "Target_Dwell_Hours": 4,
    }
    blocker = {
        **_container("STACK000002", status="Parked", spot=1500),
        "Ground_Tier": 2,
        "Ground_Reservation_ID": reservation_id(1500, 2),
        "Target_Dwell_Hours": 20,
    }
    for tier in (1, 2):
        table.put_item(Item={
            "Container_ID": reservation_id(1500, tier),
            "Type": "Ground_Reservation",
            "Assigned_Spot": 1500,
            "Ground_Tier": tier,
            "Yard_Block": 15,
        })
    table.put_item(Item=target)
    table.put_item(Item=blocker)

    movement = rehandle_for_access(table, target, "HOSTLER-TEST")
    moved = table.get_item(Key={"Container_ID": blocker["Container_ID"]})["Item"]
    reservation_ids = {
        item["Container_ID"] for item in table.all_items()
        if item.get("Type") == "Ground_Reservation"
    }
    check("a buried retrieval performs one physical rehandle", movement["rehandles"] == 1)
    check("the blocker moves clear of the retrieval stack", moved["Assigned_Spot"] != 1500)
    check("the rehandle exchanges ground claims atomically",
          moved["Ground_Reservation_ID"] in reservation_ids
          and reservation_id(1500, 2) not in reservation_ids)


def test_claim_refreshes_a_rehandled_location():
    """A claim must release the reservation created after its original queue scan."""
    import hostler
    from atomic_ops import relocate_ground_unit, transition_and_release
    from yard_topology import reservation_id

    table = mock_dynamo.reset_shared_table()
    hostler.table = table
    stale = {
        **_container("MOVED000001", status="Parked", spot=1500, direction="Export"),
        "Ground_Tier": 2,
        "Ground_Reservation_ID": reservation_id(1500, 2),
    }
    table.put_item(Item={
        "Container_ID": stale["Ground_Reservation_ID"],
        "Type": "Ground_Reservation", "Assigned_Spot": 1500,
        "Ground_Tier": 2, "Yard_Block": 15,
    })
    table.put_item(Item=stale)
    new_location = {
        "Assigned_Spot": 1600, "Ground_Tier": 1, "Yard_Block": 16,
        "Ground_Reservation_ID": reservation_id(1600, 1),
    }
    relocate_ground_unit(table, stale, new_location, "HOSTLER-REHANDLE")

    claimed = hostler._claim_candidate([stale], "HOSTLER-CLAIM", expected_status="Parked")
    check("a claim rereads a blocker location changed after the queue scan",
          claimed["Ground_Reservation_ID"] == new_location["Ground_Reservation_ID"])
    transition_and_release(
        table, claimed["Container_ID"], claimed["Assigned_Spot"],
        "set Current_Status = :s", {':s': 'Awaiting_Rail'},
        expected_status="Claimed", reservation_key=claimed["Ground_Reservation_ID"],
    )
    reservations = [
        item for item in table.all_items() if item.get("Type") == "Ground_Reservation"
    ]
    check("the claimed move releases the refreshed ground reservation", not reservations)


def test_stale_unclaimed_move_preserves_the_new_reservation():
    """A racing retrieval cannot delete a reservation from its stale queue row."""
    from atomic_ops import relocate_ground_unit, transition_and_release
    from yard_topology import reservation_id

    table = mock_dynamo.reset_shared_table()
    stale = {
        **_container("RACING00001", status="Parked", spot=1500, direction="Export"),
        "Ground_Tier": 2,
        "Ground_Reservation_ID": reservation_id(1500, 2),
    }
    table.put_item(Item={
        "Container_ID": stale["Ground_Reservation_ID"],
        "Type": "Ground_Reservation", "Assigned_Spot": 1500,
        "Ground_Tier": 2, "Yard_Block": 15,
    })
    table.put_item(Item=stale)
    new_location = {
        "Assigned_Spot": 1600, "Ground_Tier": 1, "Yard_Block": 16,
        "Ground_Reservation_ID": reservation_id(1600, 1),
    }
    relocate_ground_unit(table, stale, new_location, "HOSTLER-REHANDLE")

    refused = False
    try:
        transition_and_release(
            table, stale["Container_ID"], stale["Assigned_Spot"],
            "set Current_Status = :s", {':s': 'Awaiting_Rail'},
            expected_status="Parked",
            reservation_key=stale["Ground_Reservation_ID"],
            expected_reservation=stale["Ground_Reservation_ID"],
        )
    except ClientError:
        refused = True
    current = table.get_item(Key={"Container_ID": stale["Container_ID"]})["Item"]
    reservations = {
        item["Container_ID"] for item in table.all_items()
        if item.get("Type") == "Ground_Reservation"
    }
    check("a stale unclaimed retrieval is rejected", refused)
    check("the relocated unit keeps its authoritative reservation",
          current["Current_Status"] == "Parked"
          and new_location["Ground_Reservation_ID"] in reservations)


def test_stale_rehandle_cannot_leave_two_reservations():
    """Two crews relocating one blocker must not create two live ground claims."""
    from atomic_ops import relocate_ground_unit
    from yard_topology import reservation_id

    table = mock_dynamo.reset_shared_table()
    blocker = {
        **_container("BLOCKER0001", status="Parked", spot=1500),
        "Ground_Tier": 2,
        "Ground_Reservation_ID": reservation_id(1500, 2),
    }
    table.put_item(Item={
        "Container_ID": blocker["Ground_Reservation_ID"],
        "Type": "Ground_Reservation", "Assigned_Spot": 1500,
        "Ground_Tier": 2, "Yard_Block": 15,
    })
    table.put_item(Item=blocker)
    first = {
        "Assigned_Spot": 1600, "Ground_Tier": 1, "Yard_Block": 16,
        "Ground_Reservation_ID": reservation_id(1600, 1),
    }
    second = {
        "Assigned_Spot": 1700, "Ground_Tier": 1, "Yard_Block": 17,
        "Ground_Reservation_ID": reservation_id(1700, 1),
    }
    relocate_ground_unit(table, blocker, first, "CREW-ONE")
    refused = False
    try:
        relocate_ground_unit(table, blocker, second, "CREW-TWO")
    except ClientError:
        refused = True
    reservations = [
        item for item in table.all_items() if item.get("Type") == "Ground_Reservation"
    ]
    current = table.get_item(Key={"Container_ID": blocker["Container_ID"]})["Item"]
    check("a second crew cannot relocate a blocker from a stale location", refused)
    check("one blocker retains exactly one authoritative ground reservation",
          len(reservations) == 1
          and reservations[0]["Container_ID"] == first["Ground_Reservation_ID"]
          and current["Ground_Reservation_ID"] == first["Ground_Reservation_ID"])


def test_train_enforces_physical_loading_rules():
    """Well length, stack foundation, weight, and destination govern loading."""
    from train import OutboundTrain

    train = OutboundTrain(well_capacity=2, cutoff_minutes=60, well_lengths=[40, 53])
    cars = list(train.wells)
    ok_53_in_40, _ = train.can_load(
        cars[0], "Bottom", "LONG0000001", equipment_type="53_Dry_Van"
    )
    overweight, _ = train.can_load(
        cars[1], "Bottom", "HEAVY000001", equipment_type="40_High_Cube",
        gross_weight_lbs=70_000,
    )
    train.load_container(
        cars[1], "Bottom", "BASE0000001", equipment_type="40_High_Cube",
        gross_weight_lbs=45_000, destination_block="BLOCK_A",
    )
    wrong_destination, _ = train.can_load(
        cars[1], "Top", "DEST0000001", equipment_type="40_High_Cube",
        gross_weight_lbs=40_000, destination_block="BLOCK_B",
    )
    valid_top, _ = train.can_load(
        cars[1], "Top", "TOP00000001", equipment_type="40_High_Cube",
        gross_weight_lbs=40_000, destination_block="BLOCK_A",
    )
    check("53-foot equipment is rejected by a 40-foot well", not ok_53_in_40)
    check("a container above the single-unit weight limit is rejected", not overweight)
    check("one well cannot mix destination blocks", not wrong_destination)
    check("a compatible top load is accepted over its foundation", valid_top)


def test_gsi_query_by_status():
    """StatusIndex GSI allows O(K) targeted lookups by status."""
    table = mock_dynamo.reset_shared_table()
    table.put_item(Item=_container("AAAA1111111", status="Trackside_Hold"))
    table.put_item(Item=_container("BBBB2222222", status="Trackside_Hold"))
    table.put_item(Item=_container("CCCC3333333", status="Parked"))
    from config import query_status
    items = query_status(table, ["Trackside_Hold"])
    check("query_status returns only matching items via GSI",
          len(items) == 2 and {i["Container_ID"] for i in items} == {"AAAA1111111", "BBBB2222222"})


def test_inbound_bottom_references_actual_top_container():
    table = mock_dynamo.reset_shared_table()
    import main as ingate_engine
    ingate_engine.table = table
    ingate_engine.push_to_cloud(4)
    rail = [item for item in table.all_items() if item.get("Arrival_Mode") == "Rail"]
    top = next(item for item in rail if item["Well_Position"] == "Top")
    bottom = next(item for item in rail if item["Well_Position"] == "Bottom")
    check("a bottom unit is blocked by the actual top container ID",
          bottom["Blocked_By"] == top["Container_ID"], bottom["Blocked_By"])


def test_train_cutoff_and_atomic_departure():
    import train
    train.reset_trains()
    table = mock_dynamo.reset_shared_table()
    consist = train.get_outbound_train("TEST-TRAIN", well_capacity=1, cutoff_minutes=60)
    for cid, pos in (("RAIL0000001", "Bottom"), ("RAIL0000002", "Top")):
        table.put_item(Item=_container(cid, status="Loaded_Rail", direction="Export"))
        consist.load_container("TTZX00001", pos, cid)
    check("train cannot depart before cutoff", consist.depart(table, now=consist.created_at) == 0)
    before = [table.get_item(Key={"Container_ID": cid})["Item"]["Current_Status"]
              for cid in ("RAIL0000001", "RAIL0000002")]
    check("pre-cutoff attempt changes no container", before == ["Loaded_Rail", "Loaded_Rail"])
    check("whole loaded consist departs at cutoff",
          consist.depart(table, now=consist.cutoff_time) == 2)
    after = [table.get_item(Key={"Container_ID": cid})["Item"]["Current_Status"]
             for cid in ("RAIL0000001", "RAIL0000002")]
    check("atomic departure updates every loaded unit", after == ["Departed", "Departed"])


def test_outgate_enforces_target_dwell():
    import outgate
    table = mock_dynamo.reset_shared_table()
    outgate.table = table
    now = datetime.now(timezone.utc)
    item = _container("ROAD0000001", status="Parked", arrival=now.isoformat(), direction="Import")
    item["Target_Dwell_Hours"] = "12"
    table.put_item(Item=item)
    table.put_item(Item={"Container_ID": "SPOT#1001", "Type": "Spot_Reservation"})
    original_sleep = outgate.time.sleep
    outgate.time.sleep = lambda _: None
    try:
        check("roadbound unit cannot outgate before target dwell",
              not outgate.process_outgate(now=now))
        check("roadbound unit becomes eligible after target dwell",
              outgate.process_outgate(now=now + timedelta(hours=12, seconds=1)))
    finally:
        outgate.time.sleep = original_sleep


def test_binding_capacity_records_rollovers():
    import simulate
    result = simulate.run_shift(containers=20, hostlers=3, cranes=2, outgates=1,
                                claim="dispatch", speed=0.0, seed=21, well_capacity=4)
    check("binding train constraints roll railbound units",
          result["train"]["rolled_railbound"] > 0
          and result["train"]["loaded_departed"] + result["train"]["rolled_railbound"] == 10,
          str(result["train"]))
    check("rollovers remain valid audited lifecycles", result["exactly_once"],
          str(result["lifecycle_audit"]["problems"]))


def test_adaptive_policy_persists_learning():
    from adaptive_policy import OnlineDispatchPolicy
    from pathlib import Path
    path = Path(__file__).with_name(".test_adaptive_policy.json")
    if path.exists():
        path.unlink()
    policy = OnlineDispatchPolicy(path=path)
    candidates = [
        _container("LEARN000001", status="Parked", spot=1100, direction="Export"),
        _container("LEARN000002", status="Parked", spot=1900, direction="Export"),
    ]
    chosen, decision = policy.choose(candidates, current_spot=1101)
    try:
        policy.observe(decision, completed=True)
        reloaded = OnlineDispatchPolicy(path=path)
        check("adaptive policy writes readable persistent state", path.exists())
        check("adaptive policy continues learning across shifts",
              reloaded.total_decisions == 1 and sum(reloaded.counts.values()) == 1)
    finally:
        if path.exists():
            path.unlink()


def test_adaptive_policy_cache_is_thread_safe():
    """Concurrent hostlers must share one policy object and one file lock."""
    import adaptive_policy
    from pathlib import Path

    path = Path(__file__).with_name(".test_concurrent_policy.json")
    old_path = os.environ.get("YMS_POLICY_PATH")
    os.environ["YMS_POLICY_PATH"] = str(path)
    adaptive_policy.reset_policy_cache()
    policies = []
    workers = [
        threading.Thread(target=lambda: policies.append(adaptive_policy.get_policy()))
        for _ in range(20)
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        check("concurrent hostlers receive one shared adaptive policy",
              len(policies) == 20 and len({id(policy) for policy in policies}) == 1)
    finally:
        if old_path is None:
            os.environ.pop("YMS_POLICY_PATH", None)
        else:
            os.environ["YMS_POLICY_PATH"] = old_path
        adaptive_policy.reset_policy_cache()
        if path.exists():
            path.unlink()


def main():
    print()
    print("=" * 70)
    print("  YARD MANAGEMENT SIMULATOR - TEST SUITE")
    print("=" * 70)
    print()

    groups = [
        ("Condition evaluation", [test_condition_operators,
                                  test_missing_attribute_is_not_a_match,
                                  test_unsupported_operator_raises]),
        ("Update expressions", [test_apply_update]),
        ("Conditional writes", [test_put_refuses_to_overwrite,
                                test_update_requires_the_expected_status,
                                test_update_on_a_missing_item_is_refused]),
        ("Scans & GSI Queries", [test_scan_paginates_and_filters, test_scan_projection,
                                 test_scan_counts_rows_read_before_filtering,
                                 test_gsi_query_by_status]),
        ("Concurrency", [test_no_container_is_parked_twice_under_contention]),
        ("Terminal Appointment System", [test_appointment_rules]),
        ("End to end", [test_full_shift_is_exactly_once,
                        test_unsafe_mode_causes_data_corruption,
                        test_claim_strategy_changes_contention,
                        test_dispatch_strategy_eliminates_parking_conflicts,
                        test_concurrent_gate_clerks_spot_collision,
                        test_stack_access_rehandles_blockers,
                        test_claim_refreshes_a_rehandled_location,
                        test_stale_unclaimed_move_preserves_the_new_reservation,
                        test_stale_rehandle_cannot_leave_two_reservations,
                        test_train_enforces_physical_loading_rules,
                        test_inbound_bottom_references_actual_top_container,
                        test_train_cutoff_and_atomic_departure,
                        test_outgate_enforces_target_dwell,
                        test_binding_capacity_records_rollovers,
                        test_adaptive_policy_persists_learning,
                        test_adaptive_policy_cache_is_thread_safe]),
    ]

    for title, tests in groups:
        print(f"  {title}")
        print("  " + "-" * 66)
        for test in tests:
            test()
        print()

    print("=" * 70)
    print(f"  {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print()
        for name, detail in FAILED:
            print(f"  FAILED: {name}  {detail}")
    print("=" * 70)
    print()
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
