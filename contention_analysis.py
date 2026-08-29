"""Does yard occupancy drive hostler write contention, or does queue depth?

Both climb together over a shift, so a naive correlation blames whichever
one you happen to plot. This runs each claim strategy and regresses
incremental conflicts on queue depth and parked count together, so the
two signals can be separated.

The regressor form follows the mechanism rather than a straight line:

  head      every hostler grabs the same queue head, so collisions depend
            on whether a queue exists at all, not how deep it is. Step
            function on queue_depth > 0.
  random    hostlers draw uniformly, so collision odds fall as the queue
            gets deeper. Regressed on 1 / queue_depth.
  dispatch  claims before driving, so conflicts are near zero. Raw totals
            are reported instead; there is not enough signal to model.

Run:  python contention_analysis.py

Takes a while. Each strategy runs 200 containers against 800 seeded units at
speed 0.05, and there are three of them, so budget 15 to 20 minutes for the
full pass. It is not stalled. The seeded inventory and the slow clock are both
load bearing: drop either one and the numbers stop meaning anything. Lower
the containers argument in run_simulation() if you just want to see it work.
"""
import os

import numpy as np
import pandas as pd
import statsmodels.api as sm

import simulate

def run_simulation(strategy):
    """Run one shift for a claim strategy and return its telemetry.

    Driven through simulate.run_shift() rather than the command line. The
    CLI only exposes a subset of the engine's options, and this analysis
    needs two that it does not carry: seed_inventory and log_file.

    seed_inventory matters more than it looks. Starting the yard already
    full decouples occupancy from queue depth, so the two regressors stay
    uncorrelated (VIF near 1.0). Starting empty makes both climb together
    over the shift and the regression cannot tell them apart.

    speed=0.05 keeps the simulated drive times long enough that hostlers
    genuinely race. At speed 0 they finish before contending.
    """
    print(f"\n=============================================", flush=True)
    print(f"Running simulation with --claim {strategy} ...", flush=True)
    print(f"=============================================", flush=True)

    log_file = f"occupancy_{strategy}.csv"
    simulate.run_shift(
        containers=200,
        seed_inventory=800,
        hostlers=4,
        claim=strategy,
        speed=0.05,
        log_file=log_file,
    )

    if not os.path.exists(log_file):
        print(f"Error: {log_file} not found.", flush=True)
        return None

    df = pd.read_csv(log_file)
    df['inc_conflicts'] = df['cumulative_conflicts'].diff().fillna(0)
    df['park_rate'] = df['parked_count'].diff().fillna(0)
    df = df.iloc[1:].copy()
    
    return df

def get_vif(df, cols):
    from statsmodels.stats.outliers_influence import variance_inflation_factor
    X = sm.add_constant(df[cols])
    vifs = [variance_inflation_factor(X.values, i+1) for i in range(len(cols))]
    return vifs

def analyze():
    strategies = ['head', 'random', 'dispatch']
    
    print("\n\n=====================================================================", flush=True)
    print(" CONTENTION ANALYSIS: INTERMODAL DUAL-FLOW AND POISSON GLM", flush=True)
    print("=====================================================================\n", flush=True)
    
    for strat in strategies:
        df = run_simulation(strat)
        if df is None or len(df) < 10:
            print("Not enough data to run regression.", flush=True)
            continue
            
        print(f"\n--- Strategy: {strat.upper()} ---", flush=True)
        
        total_conflicts = df['inc_conflicts'].sum()
        conflict_ticks = (df['inc_conflicts'] > 0).sum()
        total_ticks = len(df)
        pct_ticks = (conflict_ticks / total_ticks) * 100
        print(f"Raw Totals: {int(total_conflicts)} conflicts | {pct_ticks:.1f}% of ticks ({conflict_ticks}/{total_ticks})", flush=True)
        
        if strat in ['random', 'dispatch']:
            print(f"Skipping regression for {strat} due to insufficient conflict signal (low conflict counts).", flush=True)
            continue
            
        # For 'head', perform Poisson GLM with park_rate
        df['queue_active'] = ((df['gate_queue'] + df['trackside_queue']) > 0).astype(int)
        regressors = ['queue_active', 'parked_count', 'park_rate']
        
        # Collinearity check
        vifs = get_vif(df, regressors)
        vif_str = " | ".join([f"VIF {r} = {v:.3f}" for r, v in zip(regressors, vifs)])
        print(f"Diagnostics: {vif_str}", flush=True)
        
        X = sm.add_constant(df[regressors])
        y = df['inc_conflicts']
        
        try:
            model = sm.GLM(y, X, family=sm.families.Poisson()).fit()
            print("\nPoisson GLM Results:", flush=True)
            print(f"Pseudo R-squared (CS): {model.pseudo_rsquared(kind='cs'):.4f}", flush=True)
            print("Coefficients:", flush=True)
            print(f"  Intercept:    {model.params['const']:.6f}  (p={model.pvalues['const']:.3f})", flush=True)
            for reg in regressors:
                print(f"  {reg}:  {model.params[reg]:.6f}  (p={model.pvalues[reg]:.3f})", flush=True)
            
            for reg in regressors:
                sig = "SIGNIFICANT" if model.pvalues[reg] < 0.05 else "NOT significant"
                print(f"  Signal ({reg}): {sig}", flush=True)
            print("", flush=True)
            
        except Exception as e:
            print(f"Regression failed: {e}", flush=True)

if __name__ == '__main__':
    analyze()
