"""
Builds the dashboard's two summaries from the rolling-window raw data
fetched by fetch_data.py (data/raw/sensors_meta.csv + data/raw/slice_0 ..
slice_<N-1>, each a genuine 24-hour slice ending N*24h, (N-1)*24h, ...,
24h, 0h before the moment fetch_data.py was run):

  - site/data/latest.json ("Last day" view)  = slice_0 alone: exactly the
    last 24 hours, ending when you ran fetch_data.py.
  - site/data/last7.json  ("Last N days" view) = every available slice
    pooled together: exactly the last N*24 hours.

Both windows are always "complete" by construction -- they end at the
fetch moment, not at a calendar-day boundary -- so a sensor's
completeness% is always out of a fixed, full slot count. There's no
"today is still in progress" special case to worry about here, unlike the
old calendar-day-bucketed design.

For each sensor: channel A vs channel B agreement (slope, intercept, R2,
RMSE, MAE, mean bias), data completeness, offline status (from
PurpleAir's live last_seen), PurpleAir's own channel_flags/channel_state,
and an overall OK / WARNING / FAULTY / OFFLINE status.

Usage:
    python analyze.py [--days 7]
"""
from __future__ import annotations

import json
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("analyze")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
TH = CONFIG["thresholds"]

# PurpleAir's cf_1 (correction factor 1) channel fields, per size fraction.
# PM2.5 is the primary fraction used for overall status classification
# (that's what the health-based thresholds in config.json are tuned for);
# PM1 and PM10 are computed and shown alongside for extra diagnostic value.
SIZE_FRACTIONS = {
    "pm1_0": ("pm1.0_cf_1_a", "pm1.0_cf_1_b"),
    "pm2_5": ("pm2.5_cf_1_a", "pm2.5_cf_1_b"),
    "pm10_0": ("pm10.0_cf_1_a", "pm10.0_cf_1_b"),
}
PRIMARY_FRACTION = "pm2_5"


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance between two lat/lon points, in km."""
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlmb = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return float(2 * r * np.arcsin(np.sqrt(a)))


def attach_nearest_neighbors(results: list[dict], k: int = 2) -> None:
    """
    Mutates each result dict in place, adding a 'nearest_neighbors' list of
    the k geographically closest OTHER sensors, purely by straight-line
    distance. This does NOT filter by the neighbor's own QC status — a
    sensor that is itself WARNING/FAULTY/OFFLINE is still eligible if it's
    genuinely the closest. Each neighbor entry includes its own status so
    the dashboard can show whether you're comparing against a healthy
    reference sensor or another flagged one.
    """
    def has_location(r) -> bool:
        lat, lon = r.get("latitude"), r.get("longitude")
        if lat in (None, "") or lon in (None, ""):
            return False
        try:
            return not (pd.isna(lat) or pd.isna(lon))
        except (TypeError, ValueError):
            return True

    located = [r for r in results if has_location(r)]
    by_index = {r["sensor_index"]: r for r in located}

    for r in results:
        r["nearest_neighbors"] = []
        if r["sensor_index"] not in by_index:
            continue
        dists = []
        for idx, other in by_index.items():
            if idx == r["sensor_index"]:
                continue
            d = haversine_km(r["latitude"], r["longitude"], other["latitude"], other["longitude"])
            dists.append((d, other))
        dists.sort(key=lambda x: x[0])
        r["nearest_neighbors"] = [
            {
                "sensor_index": o["sensor_index"],
                "name": o["name"],
                "distance_km": round(d, 2),
                "status": o.get("status"),
            }
            for d, o in dists[:k]
        ]


def pair_stats(a: np.ndarray, b: np.ndarray) -> dict:
    """Regression + agreement stats between two simultaneous channel readings."""
    n = len(a)
    if n < 2:
        return {"n_paired": n, "slope": None, "intercept": None, "r2": None,
                "rmse": None, "mae": None, "mean_bias": None, "pct_within_20pct": None}

    # Ordinary least squares b ~ slope*a + intercept (A is the reference axis)
    slope, intercept = np.polyfit(a, b, 1)
    pred = slope * a + intercept
    ss_res = np.sum((b - pred) ** 2)
    ss_tot = np.sum((b - np.mean(b)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else (1.0 if ss_res == 0 else 0.0)

    rmse = float(np.sqrt(np.mean((a - b) ** 2)))
    mae = float(np.mean(np.abs(a - b)))
    mean_bias = float(np.mean(b - a))

    # % of paired points where the two channels agree within 20% of their mean
    denom = np.where((a + b) / 2 == 0, np.nan, (a + b) / 2)
    pct_diff = np.abs(a - b) / denom
    pct_within_20 = float(np.nanmean(pct_diff <= 0.20) * 100)

    return {
        "n_paired": int(n),
        "slope": round(float(slope), 4),
        "intercept": round(float(intercept), 4),
        "r2": round(float(r2), 4),
        "rmse": round(rmse, 4),
        "mae": round(mae, 4),
        "mean_bias": round(mean_bias, 4),
        "pct_within_20pct": round(pct_within_20, 1),
    }


def classify(stats: dict, is_offline: bool, channel_flags, completeness: float) -> tuple[str, list[str]]:
    """Return (status, reasons[]) — status in OK / WARNING / FAULTY / OFFLINE."""
    reasons = []

    if is_offline:
        return "OFFLINE", ["No data received within offline threshold (WiFi/power likely down)"]

    try:
        channel_flags = int(channel_flags)
    except (TypeError, ValueError):
        channel_flags = 0
    if channel_flags in (1, 2, 3):
        label = {1: "Channel A", 2: "Channel B", 3: "Both channels"}[channel_flags]
        reasons.append(f"PurpleAir flags {label} as downgraded")

    if stats["n_paired"] < TH["min_paired_points"]:
        reasons.append(f"Too few paired A/B readings today ({stats['n_paired']}) to trust QC stats")
        # Not enough data to say much beyond "insufficient data"
        status = "WARNING" if not reasons[:-1] else "FAULTY"
        return status, reasons

    r2, slope = stats["r2"], stats["slope"]
    fault = False
    warn = False

    if r2 is not None and r2 < TH["r2_fault"]:
        reasons.append(f"R²={r2} between A and B is very low (< {TH['r2_fault']})")
        fault = True
    elif r2 is not None and r2 < TH["r2_warning"]:
        reasons.append(f"R²={r2} between A and B is degraded (< {TH['r2_warning']})")
        warn = True

    if slope is not None and (slope < TH["slope_fault_low"] or slope > TH["slope_fault_high"]):
        reasons.append(f"Slope={slope} between A and B is far from 1")
        fault = True
    elif slope is not None and (slope < TH["slope_warning_low"] or slope > TH["slope_warning_high"]):
        reasons.append(f"Slope={slope} between A and B is drifting from 1")
        warn = True

    if channel_flags in (1, 2, 3):
        fault = True

    if completeness < 50:
        reasons.append(f"Only {completeness:.0f}% of expected readings received today")
        warn = True

    if fault:
        return "FAULTY", reasons
    if warn:
        return "WARNING", reasons
    return "OK", ["A and B channels agree well"]


def _load_pooled_history(raw_dir: Path, slice_indices: list[int], sensor_index: int) -> pd.DataFrame:
    frames = []
    for i in slice_indices:
        fpath = raw_dir / f"slice_{i}" / f"sensor_{sensor_index}.csv"
        if fpath.exists():
            frames.append(pd.read_csv(fpath))
    if not frames:
        return pd.DataFrame()
    hist = pd.concat(frames, ignore_index=True)
    if "time_stamp" in hist.columns:
        hist = hist.sort_values("time_stamp")
    return hist


def build_window_summary(slice_indices: list[int], label: str) -> dict:
    """Build one summary (for either the "Last day" or "Last N days" view)
    by pooling the given slice indices' raw history per sensor."""
    raw_dir = ROOT / CONFIG["output"]["raw_dir"]
    meta_path = raw_dir / "sensors_meta.csv"
    if not meta_path.exists():
        raise FileNotFoundError(f"No raw data found at {meta_path}. Run fetch_data.py first.")
    meta = pd.read_csv(meta_path)

    now = datetime.now(timezone.utc)
    offline_cutoff_s = TH["offline_hours"] * 3600
    avg_min = CONFIG["history"]["average_minutes"]
    expected_slots_per_slice = int(24 * 60 / avg_min)
    expected_slots = expected_slots_per_slice * max(len(slice_indices), 1)

    results = []
    for _, row in meta.iterrows():
        idx = int(row["sensor_index"])
        name = str(row.get("name", f"sensor_{idx}"))

        last_seen = row.get("last_seen")
        is_offline = True
        if pd.notna(last_seen):
            age_s = now.timestamp() - float(last_seen)
            is_offline = age_s > offline_cutoff_s

        hist = _load_pooled_history(raw_dir, slice_indices, idx)

        completeness = 100.0 * len(hist) / expected_slots if expected_slots else 0.0
        stats_by_fraction = {frac: pair_stats(np.array([]), np.array([])) for frac in SIZE_FRACTIONS}
        timeseries = {"timestamps": []}
        for frac in SIZE_FRACTIONS:
            timeseries[f"{frac}_a"] = []
            timeseries[f"{frac}_b"] = []

        if not hist.empty:
            for frac, (col_a, col_b) in SIZE_FRACTIONS.items():
                if col_a in hist.columns and col_b in hist.columns:
                    paired = hist.dropna(subset=[col_a, col_b])
                    a = paired[col_a].to_numpy(dtype=float)
                    b = paired[col_b].to_numpy(dtype=float)
                    stats_by_fraction[frac] = pair_stats(a, b)
            if "time_stamp" in hist.columns:
                timeseries["timestamps"] = hist["time_stamp"].tolist()
                for frac, (col_a, col_b) in SIZE_FRACTIONS.items():
                    if col_a in hist.columns and col_b in hist.columns:
                        timeseries[f"{frac}_a"] = hist[col_a].tolist()
                        timeseries[f"{frac}_b"] = hist[col_b].tolist()

        primary_stats = stats_by_fraction[PRIMARY_FRACTION]
        status, reasons = classify(primary_stats, is_offline, row.get("channel_flags"), completeness)

        mean_by_fraction = {}
        for frac in SIZE_FRACTIONS:
            fa, fb = timeseries.get(f"{frac}_a"), timeseries.get(f"{frac}_b")
            mean_by_fraction[frac] = {"mean": None, "mean_a": None, "mean_b": None}
            if fa:
                arr_a = np.array(fa, dtype=float)
                arr_a = arr_a[~np.isnan(arr_a)]
                if len(arr_a):
                    mean_by_fraction[frac]["mean_a"] = round(float(np.mean(arr_a)), 2)
            if fb:
                arr_b = np.array(fb, dtype=float)
                arr_b = arr_b[~np.isnan(arr_b)]
                if len(arr_b):
                    mean_by_fraction[frac]["mean_b"] = round(float(np.mean(arr_b)), 2)
            if fa and fb:
                combined = np.array(fa + fb, dtype=float)
                combined = combined[~np.isnan(combined)]
                if len(combined):
                    mean_by_fraction[frac]["mean"] = round(float(np.mean(combined)), 2)

        sn_raw = row.get("sn")
        if pd.isna(sn_raw):
            sn_val = None
        elif isinstance(sn_raw, float) and sn_raw.is_integer():
            sn_val = str(int(sn_raw))  # pandas reads a mixed-with-blank "sn" column as float64
        else:
            sn_val = str(sn_raw)

        results.append({
            "sensor_index": idx,
            "sn": sn_val,
            "name": name,
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
            "last_seen": int(last_seen) if pd.notna(last_seen) else None,
            "date_created": int(row["date_created"]) if pd.notna(row.get("date_created")) else None,
            "rssi": row.get("rssi"),
            "channel_state": row.get("channel_state"),
            "channel_flags": row.get("channel_flags"),
            "completeness_pct": round(completeness, 1),
            "mean_by_fraction": mean_by_fraction,
            "stats": primary_stats,
            "stats_by_fraction": stats_by_fraction,
            "status": status,
            "reasons": reasons,
            "timeseries": timeseries,
        })

    attach_nearest_neighbors(results, k=2)

    window_hours = 24 * max(len(slice_indices), 1)
    window_end = now
    window_start = now - timedelta(hours=window_hours)

    return {
        "label": label,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "window_hours": window_hours,
        "days_included": len(slice_indices),
        "generated_at": now.isoformat(),
        "n_sensors": len(results),
        "counts": {
            s: sum(1 for r in results if r["status"] == s)
            for s in ["OK", "WARNING", "FAULTY", "OFFLINE"]
        },
        "sensors": results,
    }


def main(days: int = 7):
    site_data_dir = ROOT / CONFIG["output"]["site_data_dir"]
    site_data_dir.mkdir(parents=True, exist_ok=True)

    last_day = build_window_summary([0], "last_day")
    (site_data_dir / "latest.json").write_text(json.dumps(last_day, default=str))
    log.info("Last day (%s -> %s): %s", last_day["window_start"], last_day["window_end"], last_day["counts"])

    raw_dir = ROOT / CONFIG["output"]["raw_dir"]
    available_slices = [i for i in range(days) if (raw_dir / f"slice_{i}").exists()]
    if not available_slices:
        raise FileNotFoundError(f"No slice_0..slice_{days-1} raw data found under {raw_dir}. Run fetch_data.py first.")
    last_n = build_window_summary(available_slices, f"last_{days}_days")
    (site_data_dir / "last7.json").write_text(json.dumps(last_n, default=str))
    log.info("Last %d days (%s -> %s): %s", days, last_n["window_start"], last_n["window_end"], last_n["counts"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="Rolling window length in days for the 'Last N days' view (default 7)")
    args = ap.parse_args()
    main(days=args.days)
