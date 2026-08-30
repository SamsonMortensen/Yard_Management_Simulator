"""Run reproducible yard-scale and adaptive-learning experiments.

Every strategy receives the same seeds at each volume. The adaptive policy keeps
learning across its runs, while the other strategies remain fixed baselines. Hard
lifecycle, reservation, train, and TAS checks must pass before a run is counted as
correct.

The default train has at most 33 wells. That caps its theoretical position count at
99, which stays inside the simulator's 100-action atomic departure boundary. Larger
yard runs therefore create realistic rollover pressure instead of pretending one
transaction can depart an unlimited consist.
"""
from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean

import simulate


DEFAULT_SIZES = (100, 200, 400)
DEFAULT_STRATEGIES = ("head", "random", "dispatch", "adaptive")


def _crew_for(volume):
    """Scale labor gradually so volume, not a fixed crew, drives the experiment."""
    return {
        "hostlers": min(12, max(4, math.ceil(volume / 50))),
        "cranes": min(6, max(2, math.ceil(volume / 100))),
        "outgates": min(6, max(2, math.ceil(volume / 100))),
    }


def _run_row(volume, strategy, run_number, seed, policy_file, well_capacity):
    crew = _crew_for(volume)
    result = simulate.run_shift(
        containers=volume,
        hostlers=crew["hostlers"],
        cranes=crew["cranes"],
        outgates=crew["outgates"],
        claim=strategy,
        speed=0.0,
        seed=seed,
        well_capacity=well_capacity,
        max_tiers=3,
        policy_file=str(policy_file),
    )
    hostler = result["physical_metrics"]["hostler"]
    crane = result["physical_metrics"]["crane"]
    outgate = result["physical_metrics"]["outgate"]
    departed = sum(result["departure_modes"].values())
    labor_seconds = (
        hostler["simulated_hostler_seconds"]
        + crane["simulated_crane_seconds"]
        + outgate["simulated_outgate_seconds"]
    )
    correct = bool(
        result["exactly_once"]
        and result["standing_population"]
        and result["tas_verified"]
    )
    return {
        "containers": volume,
        "strategy": strategy,
        "run": run_number,
        "seed": seed,
        **crew,
        "well_capacity": well_capacity,
        "correct": correct,
        "departed": departed,
        "rolled_railbound": result["train"]["rolled_railbound"],
        "elapsed_seconds": round(result["elapsed_seconds"], 4),
        "park_conflicts": result["park_conflicts"],
        "total_conflicts": result["total_conflicts"],
        "block_hops": hostler["block_hops"],
        "rehandles": hostler["rehandles"] + outgate["rehandles"],
        "simulated_labor_hours": round(labor_seconds / 3600.0, 4),
        "units_per_labor_hour": round(volume * 3600.0 / labor_seconds, 4)
        if labor_seconds else 0.0,
        "learning_decisions": (
            result["learning"]["total_decisions"] if result.get("learning") else 0
        ),
    }


def run_experiment(sizes=DEFAULT_SIZES, runs=3, strategies=DEFAULT_STRATEGIES,
                   seed=1000, policy_file="scale_policy.json", well_capacity=33,
                   output="scale_experiment.csv"):
    """Run the matrix, write detailed rows, and return rows plus summaries."""
    policy_path = Path(policy_file)
    output_path = Path(output)
    rows = []
    with output_path.open("w", newline="", encoding="utf-8") as stream:
        writer = None
        for volume in sizes:
            for run_number in range(1, runs + 1):
                run_seed = seed + volume * 10 + run_number
                for strategy in strategies:
                    row = _run_row(
                        volume, strategy, run_number, run_seed,
                        policy_path, well_capacity,
                    )
                    rows.append(row)
                    if writer is None:
                        writer = csv.DictWriter(stream, fieldnames=list(row))
                        writer.writeheader()
                    writer.writerow(row)
                    stream.flush()
                    status = "PASS" if row["correct"] else "FAIL"
                    print(
                        f"{status} volume={volume:4d} run={run_number:2d} "
                        f"strategy={strategy:8s} hops={row['block_hops']:6d} "
                        f"rehandles={row['rehandles']:4d} "
                        f"rolls={row['rolled_railbound']:4d} "
                        f"wall={row['elapsed_seconds']:.2f}s",
                        flush=True,
                    )

    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["containers"], row["strategy"])].append(row)
    summaries = []
    for (volume, strategy), group in sorted(grouped.items()):
        summaries.append({
            "containers": volume,
            "strategy": strategy,
            "runs": len(group),
            "all_correct": all(row["correct"] for row in group),
            "mean_wall_seconds": mean(row["elapsed_seconds"] for row in group),
            "mean_conflicts": mean(row["total_conflicts"] for row in group),
            "hops_per_unit": mean(row["block_hops"] / volume for row in group),
            "rehandles_per_100": mean(row["rehandles"] * 100 / volume for row in group),
            "mean_rollovers": mean(row["rolled_railbound"] for row in group),
            "mean_units_per_labor_hour": mean(
                row["units_per_labor_hour"] for row in group
            ),
        })
    return rows, summaries


def _print_summary(summaries):
    print("\nScale experiment summary")
    print("=" * 112)
    print(
        f"{'Units':>5}  {'Strategy':<8}  {'Runs':>4}  {'Correct':>7}  "
        f"{'Wall s':>8}  {'Conflicts':>9}  {'Hops/unit':>9}  "
        f"{'RH/100':>7}  {'Rolls':>7}  {'Units/labor hr':>14}"
    )
    for row in summaries:
        print(
            f"{row['containers']:5d}  {row['strategy']:<8}  {row['runs']:4d}  "
            f"{str(row['all_correct']):>7}  {row['mean_wall_seconds']:8.2f}  "
            f"{row['mean_conflicts']:9.1f}  {row['hops_per_unit']:9.2f}  "
            f"{row['rehandles_per_100']:7.2f}  {row['mean_rollovers']:7.1f}  "
            f"{row['mean_units_per_labor_hour']:14.2f}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=list(DEFAULT_SIZES))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--strategies", nargs="+", choices=DEFAULT_STRATEGIES,
                        default=list(DEFAULT_STRATEGIES))
    parser.add_argument("--seed", type=int, default=1000)
    parser.add_argument("--well-capacity", type=int, default=33)
    parser.add_argument("--policy-file", default="scale_policy.json")
    parser.add_argument("--reset-policy", action="store_true",
                        help="start this experiment with an empty adaptive policy")
    parser.add_argument("--output", default="scale_experiment.csv")
    args = parser.parse_args()
    if args.reset_policy:
        from adaptive_policy import reset_policy_cache
        policy_path = Path(args.policy_file)
        if policy_path.exists():
            policy_path.unlink()
        reset_policy_cache()
    _, summaries = run_experiment(
        sizes=args.sizes,
        runs=args.runs,
        strategies=args.strategies,
        seed=args.seed,
        policy_file=args.policy_file,
        well_capacity=args.well_capacity,
        output=args.output,
    )
    _print_summary(summaries)
    if not all(row["all_correct"] for row in summaries):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
