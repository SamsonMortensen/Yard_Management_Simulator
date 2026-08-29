"""Supervised and unsupervised learning on real US container-port volume.

Data
----
BTS `iahn-a7j4`, "TEU Handled by Select U.S. Container Ports": monthly TEU for
nine ports, 2020-01 to 2023-08. The `ny_nj` column is the port the yard
simulator models, so the forecast feeds the same facility the rest of this repo
simulates.

The hard constraint
-------------------
44 monthly observations per port. After 12 lags that leaves 32 usable rows, so a
model with a dozen features fitted on one port is memorising, not learning. The
panel is the way out: nine ports x 32 rows = 288 training examples. Training one
GLOBAL model across all ports and comparing it against a LOCAL per-port model is
therefore not a workaround -- it is the experiment.

Because ports differ in size by an order of magnitude, the global model learns
on series scaled by each port's own TRAINING mean, so it fits shape rather than
level, and predictions are rescaled back before scoring.

Evaluation
----------
Rolling-origin, one step ahead: fit on everything before month t, predict t,
advance. Never shuffled -- shuffling a time series leaks the future into the
training set and produces flattering nonsense. Every model sees identical folds.

Note this is a strictly easier task than the six-month-ahead horizon quoted in
the README, so the error figures here are not comparable to that number.
"""
import io
import json
import urllib.request

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, IsolationForest, RandomForestRegressor
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

BTS_URL = "https://data.bts.gov/resource/iahn-a7j4.json?$limit=5000"
TARGET_PORT = "ny_nj"
LAGS = (1, 2, 3, 12)
HOLDOUT = 12          # rolling-origin evaluation months
MIN_TRAIN = 18        # never fit on fewer rows than this


# --- data ----------------------------------------------------------------

def load_panel(url=BTS_URL):
    """Return a tidy (date, port, teu) frame of monthly port volume."""
    raw = json.loads(urllib.request.urlopen(url, timeout=60).read().decode())
    frame = pd.DataFrame(raw).rename(columns={"unnamed_column": "date"})
    frame["date"] = pd.to_datetime(frame["date"])
    ports = [c for c in frame.columns if c not in ("date", "total")]
    long = frame.melt(id_vars="date", value_vars=ports,
                      var_name="port", value_name="teu")
    long["teu"] = pd.to_numeric(long["teu"])
    return long.dropna(subset=["teu"]).sort_values(["port", "date"]).reset_index(drop=True)


# --- supervised ----------------------------------------------------------

def build_features(panel):
    """Lag/seasonal design matrix. Every feature uses only prior months.

    Rolling means are computed on already-lagged values rather than on the raw
    series: a rolling window that includes month t would leak the target into
    its own feature.
    """
    out = []
    for port, group in panel.groupby("port", sort=True):
        g = group.sort_values("date").copy()
        for lag in LAGS:
            g[f"lag_{lag}"] = g["teu"].shift(lag)
        g["roll_3"] = g["teu"].shift(1).rolling(3).mean()
        g["roll_12"] = g["teu"].shift(1).rolling(12).mean()
        g["yoy_ratio"] = g["lag_1"] / g["lag_12"]
        month = g["date"].dt.month
        g["month_sin"] = np.sin(2 * np.pi * month / 12)
        g["month_cos"] = np.cos(2 * np.pi * month / 12)
        g["t"] = np.arange(len(g))
        out.append(g)
    feats = pd.concat(out, ignore_index=True)
    return feats.dropna().reset_index(drop=True)


FEATURE_COLS = ([f"lag_{l}" for l in LAGS]
                + ["roll_3", "roll_12", "yoy_ratio", "month_sin", "month_cos", "t"])


def _fit_predict(model, X_tr, y_tr, X_te, scale=True):
    if scale:
        sc = StandardScaler().fit(X_tr)
        X_tr, X_te = sc.transform(X_tr), sc.transform(X_te)
    model.fit(X_tr, y_tr)
    return model.predict(X_te)


def rolling_origin_backtest(feats, port=TARGET_PORT, holdout=HOLDOUT):
    """One-step-ahead rolling origin. Returns per-model prediction series."""
    target = feats[feats["port"] == port].sort_values("date").reset_index(drop=True)
    dates = target["date"].tolist()[-holdout:]

    preds = {name: [] for name in
             ("naive_seasonal", "ridge_local", "ridge_global", "gbr_global", "rf_global")}
    actuals = []

    for cutoff in dates:
        tr_all = feats[feats["date"] < cutoff]
        te_row = target[target["date"] == cutoff]
        if tr_all[tr_all["port"] == port].shape[0] < MIN_TRAIN:
            continue
        actuals.append(float(te_row["teu"].iloc[0]))

        # Baseline: same month last year, i.e. the lag_12 feature itself.
        preds["naive_seasonal"].append(float(te_row["lag_12"].iloc[0]))

        # LOCAL: this port only.
        tr_loc = tr_all[tr_all["port"] == port]
        preds["ridge_local"].append(float(_fit_predict(
            Ridge(alpha=1.0), tr_loc[FEATURE_COLS], tr_loc["teu"],
            te_row[FEATURE_COLS])[0]))

        # GLOBAL: all ports, each scaled by its own training mean so the model
        # learns shape rather than magnitude, then rescaled for scoring.
        scale_by = tr_all.groupby("port")["teu"].mean()
        tr_g = tr_all.copy()
        tr_g["scale"] = tr_g["port"].map(scale_by)
        Xg = tr_g[FEATURE_COLS].div(tr_g["scale"], axis=0)
        Xg["month_sin"], Xg["month_cos"] = tr_g["month_sin"], tr_g["month_cos"]
        Xg["yoy_ratio"], Xg["t"] = tr_g["yoy_ratio"], tr_g["t"]
        yg = tr_g["teu"] / tr_g["scale"]

        s = float(scale_by[port])
        Xt = te_row[FEATURE_COLS].div(s)
        Xt["month_sin"], Xt["month_cos"] = te_row["month_sin"], te_row["month_cos"]
        Xt["yoy_ratio"], Xt["t"] = te_row["yoy_ratio"], te_row["t"]

        for name, model in (("ridge_global", Ridge(alpha=1.0)),
                            ("gbr_global", GradientBoostingRegressor(
                                n_estimators=200, max_depth=2,
                                learning_rate=0.05, random_state=0)),
                            ("rf_global", RandomForestRegressor(
                                n_estimators=300, max_depth=4,
                                random_state=0, n_jobs=-1))):
            scale_it = name.startswith("ridge")
            preds[name].append(float(_fit_predict(model, Xg, yg, Xt, scale=scale_it)[0]) * s)

    return np.array(actuals), {k: np.array(v) for k, v in preds.items()}, dates


def mape(actual, pred):
    return float(np.mean(np.abs((actual - pred) / actual)) * 100)


def mae(actual, pred):
    return float(np.mean(np.abs(actual - pred)))


# --- unsupervised --------------------------------------------------------

def common_factor(panel):
    """PCA across ports. PC1 is the national demand signal every port shares."""
    wide = panel.pivot(index="date", columns="port", values="teu").dropna()
    z = StandardScaler().fit_transform(wide.values)
    pca = PCA(n_components=3).fit(z)
    scores = pca.transform(z)[:, 0]
    return wide, pca.explained_variance_ratio_, pd.Series(scores, index=wide.index)


def cluster_seasonal_shape(panel, k=3):
    """Group ports by the SHAPE of their annual cycle, not their size."""
    wide = panel.pivot(index="date", columns="port", values="teu").dropna()
    shape = wide.groupby(wide.index.month).mean()
    shape = (shape / shape.mean()).T                       # normalise out port size
    labels = KMeans(n_clusters=k, n_init=20, random_state=0).fit_predict(shape.values)
    return pd.Series(labels, index=shape.index).sort_values()


def detect_regime_break(panel, port=TARGET_PORT):
    """Flag anomalous months without being told when COVID happened."""
    s = panel[panel["port"] == port].set_index("date")["teu"].sort_index()
    feats = pd.DataFrame({"mom": s.pct_change(), "yoy": s.pct_change(12)}).dropna()
    flags = IsolationForest(contamination=0.15, random_state=0).fit_predict(feats.values)
    return feats.assign(anomaly=flags == -1)


# --- report --------------------------------------------------------------

def main():
    print("Fetching BTS iahn-a7j4 (monthly TEU, select US container ports)...")
    panel = load_panel()
    span = panel["date"]
    print(f"  {panel['port'].nunique()} ports | {span.min():%Y-%m} .. {span.max():%Y-%m} "
          f"| {len(panel)} observations")

    feats = build_features(panel)
    n_local = (feats['port'] == TARGET_PORT).sum()
    print(f"  after lag construction: {len(feats)} usable rows global, {n_local} local ({TARGET_PORT})")

    print(f"\nSUPERVISED -- rolling origin, 1 step ahead, last {HOLDOUT} months")
    print("=" * 72)
    actual, preds, dates = rolling_origin_backtest(feats)
    print(f"  evaluated on {len(actual)} folds: {dates[0]:%Y-%m} .. {dates[-1]:%Y-%m}\n")
    print(f"  {'model':<18}{'MAPE':>9}{'MAE (TEU)':>13}   {'vs naive':>9}")
    print("  " + "-" * 54)
    base = mape(actual, preds["naive_seasonal"])
    for name in ("naive_seasonal", "ridge_local", "ridge_global", "gbr_global", "rf_global"):
        m = mape(actual, preds[name])
        delta = "baseline" if name == "naive_seasonal" else f"{(base - m) / base * 100:+.0f}%"
        print(f"  {name:<18}{m:>8.1f}%{mae(actual, preds[name]):>13,.0f}   {delta:>9}")

    print("\nUNSUPERVISED")
    print("=" * 72)
    wide, var, pc1 = common_factor(panel)
    print(f"  PCA across {wide.shape[1]} ports -- variance explained: "
          f"PC1 {var[0]*100:.0f}%, PC2 {var[1]*100:.0f}%, PC3 {var[2]*100:.0f}%")
    print(f"    PC1 peaks {pc1.idxmax():%Y-%m}, troughs {pc1.idxmin():%Y-%m}")

    clusters = cluster_seasonal_shape(panel)
    print("\n  KMeans on normalised seasonal shape:")
    for c in sorted(clusters.unique()):
        print(f"    cluster {c}: {', '.join(clusters[clusters == c].index)}")

    anom = detect_regime_break(panel)
    flagged = anom[anom["anomaly"]]
    print(f"\n  IsolationForest on {TARGET_PORT} month/year-over-year change "
          f"-- {len(flagged)} anomalous months:")
    for ts, row in flagged.iterrows():
        print(f"    {ts:%Y-%m}   MoM {row['mom']*100:+6.1f}%   YoY {row['yoy']*100:+7.1f}%")


if __name__ == "__main__":
    main()
