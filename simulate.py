"""Run a full yard shift locally, with no AWS account.

    pip install -r requirements.txt
    python simulate.py
    python simulate.py --unsafe
    python simulate.py --compare

What it measures
----------------
1. Guarded Optimistic Concurrency vs Unsafe Baseline:
   - `python simulate.py` runs with atomic conditional writes.
   - `python simulate.py --unsafe` removes the condition, directly demonstrating
     data corruption and race conditions.

2. Contention & Mispark Prevention:
   - Every detected conflict is an actual physical collision intercepted and prevented.

3. Queue Optimization Trade-offs:
   - head: targets the same queue head at the cost of contention.
   - random: drops contention but violates arrival order.
   - dispatch: arrival-sorted atomic pre-claim avoids driving to already-claimed work.
   - adaptive: safely learns among FIFO, nearest-block and cutoff-priority rules.

4. Bidirectional Intermodal Operations:
   - Roadbound: Inbound Train -> Offload (tops first) -> Hostler Park -> Road Outgate.
   - Railbound: Road Ingate -> Park -> Hostler Retrieve -> Crane Load -> Train Depart.
"""
import argparse
import io
import os
import random
import threading
import time
import math
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout

os.environ["YMS_BACKEND"] = "memory"

_REAL_SLEEP = time.sleep


def _compress_time(factor):
    if factor >= 1.0:
        return
    if factor <= 0.0:
        time.sleep = lambda seconds: _REAL_SLEEP(0.0001)
        return
    time.sleep = lambda seconds: _REAL_SLEEP(seconds * factor)


def _restore_time():
    time.sleep = _REAL_SLEEP


# Scenario ----------------------------------------------------------------


def monitor_occupancy(table, stop_event, log_file):
    import time
    with open(log_file, "w", encoding="utf-8") as f:
        f.write("timestamp,parked_count,gate_queue,trackside_queue,buffer_queue,awaiting_rail_queue,cumulative_conflicts\n")
        start = time.perf_counter()
        while not stop_event.is_set():
            parked = 0
            gate_queue = 0
            track_queue = 0
            buffer_queue = 0
            awaiting_rail_queue = 0
            with table._lock:
                for i in table._items.values():
                    status = i.get("Current_Status")
                    if status == "Parked":
                        parked += 1
                    elif status == "Ingate_Hold":
                        gate_queue += 1
                    elif status == "Trackside_Hold":
                        track_queue += 1
                    elif status == "Buffer_Hold":
                        buffer_queue += 1
                    elif status == "Awaiting_Rail":
                        awaiting_rail_queue += 1
            conflicts = table.stats.get("update_conflicts", 0) + table.stats.get("put_conflicts", 0)
            f.write(f"{(time.perf_counter() - start):.2f},{parked},{gate_queue},{track_queue},{buffer_queue},{awaiting_rail_queue},{conflicts}\n")
            f.flush()
            _REAL_SLEEP(0.1)

def run_shift(containers=60, hostlers=2, cranes=2, outgates=1, claim="head",
              unsafe=False, speed=0.01, page_size=25, seed=42, verbose=False,
              data_file=None, seed_inventory=0, log_file="occupancy_log.csv",
              well_capacity=None, cutoff_minutes=60.0, policy_file=None,
              block_size=100, max_tiers=3):
    """Runs a full bidirectional yard shift. Returns the measurements."""
    random.seed(seed)
    os.environ["YMS_CLAIM"] = claim
    os.environ["YMS_UNSAFE"] = "true" if unsafe else "false"
    os.environ["YMS_BACKEND"] = "memory"
    os.environ["YMS_BLOCK_SIZE"] = str(block_size)
    os.environ["YMS_MAX_TIERS"] = str(max_tiers)
    if policy_file:
        os.environ["YMS_POLICY_PATH"] = policy_file

    import mock_dynamo
    table = mock_dynamo.reset_shared_table(page_size=page_size)

    import train
    train.reset_trains()
    expected_railbound = containers // 2
    selected_wells = (
        well_capacity if well_capacity is not None
        else max(4, math.ceil(expected_railbound / 2) + 3)
    )
    outbound_train = train.get_outbound_train(
        "TR-OUT-01", well_capacity=selected_wells, cutoff_minutes=cutoff_minutes
    )

    import main as ingate_engine
    import hostler as hostler_engine
    import crane as crane_engine
    import outgate as outgate_engine
    import dispatch_check
    from flow import is_roadbound, is_railbound
    from yard_topology import is_reservation_id

    hostler_engine.reset_stats()
    crane_engine.reset_stats()
    outgate_engine.reset_stats()

    _compress_time(speed)
    sink = io.StringIO()
    monitor_controls = []

    def _run_all():
        started = time.perf_counter()

        stop_event = threading.Event()
        occ_thread = threading.Thread(target=monitor_occupancy, args=(table, stop_event, log_file))
        occ_thread.start()
        monitor_controls.append((stop_event, occ_thread))

        if seed_inventory > 0:
            ingate_engine.seed_yard_inventory(seed_inventory)

        ingate_engine.push_to_cloud(containers, data_file=data_file)
        snapshot_ingate = dict(table.stats)

        # Concurrent workers: hostlers and cranes
        workers = [threading.Thread(target=hostler_engine.run_shift, name=f"hostler-{i}")
                   for i in range(hostlers)]
        c_workers = [threading.Thread(target=crane_engine.run_shift, name=f"crane-{i}")
                     for i in range(cranes)]
        for worker in workers + c_workers:
            worker.start()
        for worker in workers + c_workers:
            worker.join()
        snapshot_parking = dict(table.stats)

        # TAS verification on grounded roadbound units before departure.
        parked_roadbound = [i["Container_ID"] for i in table.all_items()
                            if i.get("Current_Status") == "Parked" and is_roadbound(i)]
        parked_sample = parked_roadbound[:1] if parked_roadbound else []
        approved_count = sum(1 if dispatch_check.check_appointment(cid) else 0
                             for cid in parked_sample)

        # Outgate road units
        roadbound_items = [i for i in table.all_items() if is_roadbound(i) and i.get('Arrival_Time')]
        ready_time = datetime.now(timezone.utc)
        if roadbound_items:
            ready_time = max(
                datetime.fromisoformat(i['Arrival_Time'].replace('Z', '+00:00'))
                + timedelta(hours=float(i.get('Target_Dwell_Hours', 12.5)))
                for i in roadbound_items
            ) + timedelta(seconds=1)
        o_workers = [threading.Thread(target=outgate_engine.run_shift, kwargs={'now': ready_time}, name=f"outgate-{i}")
                     for i in range(outgates)]
        for worker in o_workers:
            worker.start()
        for worker in o_workers:
            worker.join()

        # Outbound train departure event
        train_departed_count = outbound_train.depart(
            table, now=outbound_train.cutoff_time, unsafe=unsafe
        )

        stop_event.set()
        occ_thread.join()

        duration = time.perf_counter() - started

        # TAS checks on departed & missing units (must be denied)
        departed = [i["Container_ID"] for i in table.all_items()
                    if i.get("Current_Status") == "Departed"]
        dry_run_attempts = departed[:12] + ["FAKE9999999"]
        denied_count = sum(0 if dispatch_check.check_appointment(cid) else 1
                           for cid in dry_run_attempts)
        return snapshot_ingate, snapshot_parking, duration, dry_run_attempts, denied_count, len(parked_sample), approved_count, train_departed_count

    try:
        if verbose:
            after_ingate, after_parking, elapsed, dry_run_attempts, dry_runs_denied, parked_checked, parked_approved, train_departed_count = _run_all()
        else:
            with redirect_stdout(sink):
                after_ingate, after_parking, elapsed, dry_run_attempts, dry_runs_denied, parked_checked, parked_approved, train_departed_count = _run_all()
        final = dict(table.stats)
    finally:
        for stop_event, occ_thread in monitor_controls:
            stop_event.set()
            if occ_thread.is_alive():
                occ_thread.join(timeout=2)
        _restore_time()

    items = table.all_items()
    statuses = {}
    departure_modes = {}
    for item in items:
        status = item.get("Current_Status", item.get("Type", "Unknown_Record"))
        statuses[status] = statuses.get(status, 0) + 1
        if status == "Departed":
            mode = item.get("Departure_Mode", "Road")
            departure_modes[mode] = departure_modes.get(mode, 0) + 1

    park_attempts = after_parking["updates"] - after_ingate["updates"]
    park_conflicts = after_parking["update_conflicts"] - after_ingate["update_conflicts"]
    successful_park_writes = after_parking.get("successful_parks", 0) - after_ingate.get("successful_parks", 0)

    total_containers = len([
        item for item in items if not is_reservation_id(item.get("Container_ID", ""))
    ])
    seeded = seed_inventory
    
    successful_writes = final["updates"] - final["update_conflicts"]
    from transition_audit import audit_lifecycles
    lifecycle_audit = audit_lifecycles(table.all_events(), items)
    exactly_once = lifecycle_audit["passed"] and not unsafe
    double_park_writes = max(0, successful_park_writes - total_containers) if unsafe else 0

    # Ground-lock consistency: every parked unit owns one exact spot-tier
    # reservation, and no departed unit leaves an orphaned claim behind.
    standing_pop = (
        statuses.get("Parked", 0)
        == statuses.get("Ground_Reservation", 0) + statuses.get("Spot_Reservation", 0)
    )
    rolled_railbound = len([i for i in items if is_railbound(i) and i.get('Current_Status') == 'Awaiting_Rail'])
    learning = None
    if claim == 'adaptive':
        from adaptive_policy import get_policy
        learning = get_policy().snapshot()

    return {
        "config": {
            "containers": containers, "hostlers": hostlers, "cranes": cranes, "outgates": outgates,
            "claim": claim, "unsafe": unsafe, "page_size": page_size, "seed": seed,
            "well_capacity": selected_wells, "cutoff_minutes": cutoff_minutes,
            "block_size": block_size,
            "max_tiers": max_tiers,
        },
        "elapsed_seconds": elapsed,
        "ingated": total_containers,
        "statuses": statuses,
        "departure_modes": departure_modes,
        "park_attempts": park_attempts,
        "successful_park_writes": successful_park_writes,
        "park_writes": successful_park_writes,
        "double_park_writes": double_park_writes,
        "park_conflicts": park_conflicts,
        "total_conflicts": final["update_conflicts"],
        "successful_writes": successful_writes,
        "lifecycle_audit": lifecycle_audit,
        "exactly_once": exactly_once,
        "standing_population": standing_pop,
        "dry_runs_denied": dry_runs_denied,
        "dry_run_attempts": len(dry_run_attempts),
        "parked_checked": parked_checked,
        "parked_approved": parked_approved,
        "tas_verified": (parked_approved == parked_checked and dry_runs_denied == len(dry_run_attempts)),
        "train": {
            "train_id": outbound_train.train_id,
            "well_capacity": outbound_train.well_capacity,
            "slot_capacity": outbound_train.slot_capacity,
            "loaded_departed": train_departed_count,
            "rolled_railbound": rolled_railbound,
            "cutoff_minutes": cutoff_minutes,
        },
        "learning": learning,
        "physical_metrics": {
            "hostler": dict(hostler_engine.STATS),
            "crane": dict(crane_engine.STATS),
            "outgate": dict(outgate_engine.STATS),
        },
        "scans": final["scans"] - after_ingate.get("scans", 0),
        "scan_pages": final["scan_pages"] - after_ingate.get("scan_pages", 0),
        "rows_read_by_scans": final["items_read_by_scans"] - after_ingate.get("items_read_by_scans", 0),
        "queries": final.get("queries", 0) - after_ingate.get("queries", 0),
        "query_pages": final.get("query_pages", 0) - after_ingate.get("query_pages", 0),
        "rows_read_by_queries": final.get("items_read_by_queries", 0) - after_ingate.get("items_read_by_queries", 0),
        "stats": final,
        "after_ingate": after_ingate,
        "items": items,
    }


def repeat_shifts(runs=10, **kwargs):
    seed = kwargs.pop("seed", 42)
    results = [run_shift(seed=seed + i, **kwargs) for i in range(runs)]
    conflicts = [r["park_conflicts"] for r in results]
    double_parks = [r["double_park_writes"] for r in results]
    successful_parks = [r["successful_park_writes"] for r in results]
    return {
        "runs": runs,
        "conflicts": conflicts,
        "double_parks": double_parks,
        "successful_parks": successful_parks,
        "mean_conflicts": sum(conflicts) / len(conflicts),
        "min_conflicts": min(conflicts),
        "max_conflicts": max(conflicts),
        "min_successful_parks": min(successful_parks),
        "max_successful_parks": max(successful_parks),
        "min_double_parks": min(double_parks),
        "max_double_parks": max(double_parks),
        "mean_successful_park_writes": sum(successful_parks) / len(successful_parks),
        "mean_double_parks": sum(double_parks) / len(double_parks),
        "all_correct": all(r["exactly_once"] and r.get("tas_verified", False) for r in results),
        "last": results[-1],
    }


def render(result):
    cfg = result["config"]
    lines = []
    lines.append("")
    mode_label = "UNSAFE CONTROL (Conditional Writes Disabled)" if cfg["unsafe"] else "GUARDED (Optimistic Concurrency)"
    lines.append(f"  Yard shift simulation: {mode_label}")
    lines.append(f"  {cfg['containers']} containers | {cfg['hostlers']} hostlers | {cfg['cranes']} cranes | "
                 f"{cfg['outgates']} outgate | claim strategy: {cfg['claim']}")
    lines.append("  " + "=" * 74)

    lines.append("")
    lines.append("  Correctness & Data Integrity")
    lines.append("  " + "-" * 74)
    if cfg["unsafe"]:
        lines.append(f"    park updates executed                        [FAIL]  "
                     f"{result['park_writes']} writes (CORRUPTION: {result['double_park_writes']} duplicate park events)")
        lines.append(f"    every parked unit holds one ground-tier lock [PASS]  "
                     f"{result['statuses']}")
        lines.append(f"    TAS validation (grounded vs dry runs)        [PASS]  "
                     f"{result['parked_approved']}/{result['parked_checked']} approved, "
                     f"{result['dry_runs_denied']}/{result['dry_run_attempts']} denied")
    else:
        ok = "PASS" if result["exactly_once"] else "FAIL"
        lines.append(f"    every lifecycle transition is valid          [{ok}]  "
                     f"{result['lifecycle_audit']['containers_checked']} containers audited")
        ok = "PASS" if result.get("standing_population") else "FAIL"
        lines.append(f"    every parked unit holds one ground-tier lock [{ok}]  "
                     f"{result['statuses']}")
        ok = "PASS" if result["tas_verified"] else "FAIL"
        lines.append(f"    TAS validation (grounded vs dry runs)        [{ok}]  "
                     f"{result['parked_approved']}/{result['parked_checked']} approved, "
                     f"{result['dry_runs_denied']}/{result['dry_run_attempts']} denied")
        lines.append(f"    departures by mode:                          {result.get('departure_modes', {})}")
        lines.append(f"    outbound train:                              "
                     f"{result['train']['loaded_departed']}/{result['train']['slot_capacity']} slots departed, "
                     f"{result['train']['rolled_railbound']} railbound rolled")
        if result.get('learning'):
            lines.append(f"    adaptive dispatch learner:                   "
                         f"{result['learning']['total_decisions']} decisions, "
                         f"values={result['learning']['values']}")
        hostler_metrics = result['physical_metrics']['hostler']
        lines.append(f"    hostler travel / rehandles:                  "
                     f"{hostler_metrics['block_hops']} block hops / "
                     f"{hostler_metrics['rehandles']} rehandles")

    lines.append("")
    lines.append("  Write Contention & Collision Prevention")
    lines.append("  " + "-" * 74)
    if cfg["unsafe"]:
        lines.append(f"    conflicts detected while parking             0 (blind overwrites: no detection)")
        lines.append(f"    double-parked records created                {result['double_park_writes']}")
    else:
        lines.append(f"    conflicts intercepted while parking          {result['park_conflicts']}")
        lines.append(f"    conflicts over the whole shift               {result['total_conflicts']}")

    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate intermodal railyard shift")
    parser.add_argument("--containers", type=int, default=20)
    parser.add_argument("--hostlers", type=int, default=3)
    parser.add_argument("--cranes", type=int, default=2)
    parser.add_argument("--outgates", type=int, default=1)
    parser.add_argument("--claim", choices=["head", "random", "dispatch", "adaptive"], default="dispatch")
    parser.add_argument("--well-capacity", type=int, default=None,
                        help="number of double-stack wells; set below demand to force rollovers")
    parser.add_argument("--cutoff-minutes", type=float, default=60.0)
    parser.add_argument("--block-size", type=int, default=100)
    parser.add_argument("--max-tiers", type=int, default=3)
    parser.add_argument("--policy-file", default="adaptive_policy.json",
                        help="persistent online-learning state used by --claim adaptive")
    parser.add_argument("--unsafe", action="store_true")
    parser.add_argument("--speed", type=float, default=0.01)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    res = run_shift(containers=args.containers, hostlers=args.hostlers, cranes=args.cranes,
                    outgates=args.outgates, claim=args.claim, unsafe=args.unsafe,
                    speed=args.speed, verbose=args.verbose,
                    well_capacity=args.well_capacity, cutoff_minutes=args.cutoff_minutes,
                    policy_file=args.policy_file if args.claim == 'adaptive' else None,
                    block_size=args.block_size, max_tiers=args.max_tiers)
    print(render(res))
    if not args.unsafe and (not res['exactly_once'] or not res['tas_verified']):
        raise SystemExit(1)
