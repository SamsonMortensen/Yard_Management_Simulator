import os
import sys

# Ensure in-memory mock DynamoDB backend
os.environ["YMS_BACKEND"] = "memory"

import mock_dynamo
import config
from config import get_table, query_status
import crane

def evaluate_sweep(sweep_mode, travel_delay, lock_delay, data_file="historical_manifest.csv"):
    os.environ["YMS_SWEEP_MODE"] = sweep_mode
    os.environ["CRANE_TRAVEL_DELAY"] = str(travel_delay)
    os.environ["CONE_LOCK_DELAY"] = str(lock_delay)
    os.environ["YMS_CLAIM"] = "dispatch"
    os.environ["YMS_SWEEP_BENCHMARK"] = "true"

    table = mock_dynamo.reset_shared_table()
    crane.reset_stats()
    crane.table = table

    # Ingate rail containers from manifest to Trackside_Hold
    import csv
    with open(data_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rail_items = [row for row in reader if row.get("Arrival_Mode") == "Rail"][:60]

    for item in rail_items:
        table.put_item(Item={
            "Container_ID": item["Container_ID"],
            "Equipment_Type": item.get("Equipment_Type", "40_High_Cube"),
            "Current_Status": "Trackside_Hold",
            "Assigned_Spot": 1000,
            "Arrival_Time": item.get("Arrival_Time", "2026-08-27T04:00:00+00:00"),
            "Arrival_Mode": "Rail",
            "Railcar_ID": item.get("Railcar_ID", "None"),
            "Well_Position": item.get("Well_Position", "None"),
            "Blocked_By": item.get("Blocked_By", "None")
        })

    # Run crane until all trackside units are lifted
    while True:
        moved = crane.move_container()
        if not moved:
            break

    lifts = crane.STATS["lifts"]
    sim_seconds = crane.STATS["simulated_crane_seconds"]
    sim_hours = sim_seconds / 3600.0 if sim_seconds > 0 else 1.0
    lifts_per_hour = lifts / sim_hours if sim_hours > 0 else 0.0

    return lifts, sim_seconds, lifts_per_hour

def run_benchmark():
    print("=" * 80)
    print("  CRANE SWEEP STRATEGY OPERATIONS RESEARCH BENCHMARK")
    print("=" * 80)
    print("  Evaluates 'tops_first' (Tukwila Baseline) vs 'well_by_well'")
    print("  Physics Model: 45s Hoist Cycle, 15s Inter-Well Travel, Variable Cone Unlocking")
    print("  Metric: Simulated Crane Lifts per Hour (lifts ÷ simulated_hours)")
    print("=" * 80)
    print()

    travel_time = 15.0
    cone_lock_times = [5.0, 10.0, 14.0, 15.0, 16.0, 20.0, 30.0]

    print(f"  Fixed Crane Travel Delay: {travel_time:.1f}s")
    print(f"  {'Cone Lock Delay':<18} | {'Tops-First LPH':<16} | {'Well-By-Well LPH':<18} | {'Winner':<16} | {'Advantage'}")
    print("  " + "-" * 80)

    for lock in cone_lock_times:
        tf_lifts, tf_sec, tf_lph = evaluate_sweep("tops_first", travel_time, lock)
        ww_lifts, ww_sec, ww_lph = evaluate_sweep("well_by_well", travel_time, lock)

        diff = ww_lph - tf_lph
        if abs(diff) < 0.1:
            winner = "TIE (Crossover)"
            advantage = "0.0%"
        elif diff > 0:
            winner = "Well-By-Well"
            advantage = f"+{(diff / tf_lph) * 100:.1f}%"
        else:
            winner = "Tops-First"
            advantage = f"+{(abs(diff) / ww_lph) * 100:.1f}%"

        print(f"  {lock:<18.1f} | {tf_lph:<16.2f} | {ww_lph:<18.2f} | {winner:<16} | {advantage}")

    print()
    print("  " + "-" * 80)
    print("  Operations Research Takeaway:")
    print("  - When Cone-Lock removal is fast (< 15s travel time), well-by-well wins")
    print("    by eliminating the second crane travel pass down the track.")
    print("  - When Cone-Lock removal is slow (> 15s travel time), tops-first wins")
    print("    because ground crews unlock bottom containers asynchronously while the crane")
    print("    is busy sweeping tops on pass 1, avoiding crane idling.")
    print("  - Exact Crossover Point: Lock Time = Crane Travel Time (15.0s).")
    print("=" * 80)
    print()

if __name__ == "__main__":
    run_benchmark()
