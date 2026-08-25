"""Tests for the in-memory DynamoDB stand-in and the yard simulator.

Runs the test suite without an AWS account:

    pip install -r requirements.txt
    python test_yard.py
"""
import os
import threading

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
               equip="53_Dry_Van", arrival="2026-04-12T08:00:00+00:00"):
    return {
        "Container_ID": cid,
        "Current_Status": status,
        "Assigned_Spot": spot,
        "Equipment_Type": equip,
        "Arrival_Time": arrival,
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

    table.put_item(Item=_container("GGGG5555555", status="Parked", spot=1234))
    table.put_item(Item=_container("HHHH6666666", status="Ingate_Hold"))
    table.put_item(Item=_container("JJJJ8888888", status="Claimed"))
    table.put_item(Item=_container("IIII7777777", status="Departed"))

    with redirect_stdout(io.StringIO()):
        approved = dispatch_check.check_appointment("GGGG5555555")
        on_wheels = dispatch_check.check_appointment("HHHH6666666")
        moving = dispatch_check.check_appointment("JJJJ8888888")
        gone = dispatch_check.check_appointment("IIII7777777")
        missing = dispatch_check.check_appointment("ZZZZ9999999")

    check("a grounded unit is approved", approved)
    check("a unit still on wheels is pending", not on_wheels)
    check("a unit actively moving/claimed is pending", not moving)
    check("an already-departed unit is denied", not gone)
    check("a unit that was never here is denied (the dry run)", not missing)


# End to end --------------------------------------------------------------

def test_full_shift_is_exactly_once():
    """Every container parked once and departed once, with real thread contention."""
    import simulate
    result = simulate.run_shift(containers=20, hostlers=3, outgates=1,
                                claim="head", speed=0.0, seed=7)
    check("every unit ended up departed",
          result["statuses"].get("Departed") == 20, f"{result['statuses']}")
    check("exactly two successful writes per container under head strategy",
          result["successful_writes"] == 40,
          f"{result['successful_writes']} (expected 40)")
    check("contention actually occurred", result["park_conflicts"] > 0,
          f"{result['park_conflicts']}")
    check("the TAS denied every dry-run attempt",
          result["dry_runs_denied"] == result["dry_run_attempts"],
          f"{result['dry_runs_denied']}/{result['dry_run_attempts']}")
    check("the TAS verified grounded units before departure",
          result["tas_verified"])


def test_unsafe_mode_causes_data_corruption():
    """Without conditional writes, blind updates overwrite records and cause double-parking."""
    import simulate
    result = simulate.run_shift(containers=20, hostlers=3, outgates=1,
                                claim="head", unsafe=True, speed=0.0, seed=7)
    check("unsafe run executed duplicate park updates",
          result["park_writes"] > 20, f"park writes: {result['park_writes']} expected: 20")
    check("unsafe run reported 0 conflicts because writes were blind",
          result["park_conflicts"] == 0, f"conflicts: {result['park_conflicts']}")


def test_claim_strategy_changes_contention():
    """Claiming head of queue preserves FIFO at the cost of contention."""
    import simulate
    head = simulate.repeat_shifts(runs=3, containers=20, hostlers=3,
                                  claim="head", speed=0.0, seed=11)
    rand = simulate.repeat_shifts(runs=3, containers=20, hostlers=3,
                                  claim="random", speed=0.0, seed=11)
    check("claiming the head produces substantially more conflicts",
          head["mean_conflicts"] > rand["mean_conflicts"] * 2,
          f"head {head['mean_conflicts']:.1f} vs random {rand['mean_conflicts']:.1f}")
    check("correctness holds under both strategies",
          head["all_correct"] and rand["all_correct"])


def test_dispatch_strategy_eliminates_parking_conflicts():
    """Centralized dispatch reserves moves atomically before the drive."""
    import simulate
    disp = simulate.run_shift(containers=20, hostlers=3, outgates=1,
                              claim="dispatch", speed=0.0, seed=15)
    check("dispatch strategy achieves zero parking conflicts",
          disp["park_conflicts"] == 0, f"{disp['park_conflicts']}")
    check("dispatch strategy maintains 100% departure correctness",
          disp["all_departed"] and disp["exactly_once"])


def test_concurrent_gate_clerks_spot_collision():
    """Empirically verifies that conditional spot reservation prevents duplicate
    spot allocation under concurrent gate clerks."""
    table = mock_dynamo.reset_shared_table()
    import main as ingate_engine
    import threading, sys, os

    def run_clerk():
        old_stdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')
        try:
            ingate_engine.push_to_cloud(15)
        finally:
            sys.stdout.close()
            sys.stdout = old_stdout

    t1 = threading.Thread(target=run_clerk)
    t2 = threading.Thread(target=run_clerk)
    
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    items = [i for i in table.all_items() if not i.get("Container_ID", "").startswith("SPOT#")]
    spots = [i["Assigned_Spot"] for i in items]
    unique_spots = set(spots)
    collisions = len(spots) - len(unique_spots)
    check("conditional spot allocation strictly prevents collisions under concurrent clerks",
          collisions == 0, f"{collisions} duplicate spot assignments across {len(items)} containers")


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
        ("Scans", [test_scan_paginates_and_filters, test_scan_projection,
                   test_scan_counts_rows_read_before_filtering]),
        ("Concurrency", [test_no_container_is_parked_twice_under_contention]),
        ("Terminal Appointment System", [test_appointment_rules]),
        ("End to end", [test_full_shift_is_exactly_once,
                        test_unsafe_mode_causes_data_corruption,
                        test_claim_strategy_changes_contention,
                        test_dispatch_strategy_eliminates_parking_conflicts,
                        test_concurrent_gate_clerks_spot_collision]),
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
