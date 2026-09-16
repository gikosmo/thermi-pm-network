"""
Downloads a rolling window of history for every sensor in config.json's
station_ids and saves it to data/raw/.

This is a genuine ROLLING window, anchored to the moment you run this
script -- NOT calendar days / UTC midnight. Running it at 10:00 local on
2026-09-16 fetches, for each sensor, one 24-hour slice per day going back:

    slice_0 = [now-24h,  now)        <- "Last day" view reads this alone
    slice_1 = [now-48h,  now-24h)
    slice_2 = [now-72h,  now-48h)
    ...
    slice_6 = [now-168h, now-144h)   <- all 7 slices pooled = "Last 7 days"

Run it again tomorrow at 10:00 and every slice shifts forward by exactly
one day, automatically -- there's no calendar-date bookkeeping to get out
of sync.

PurpleAir's history endpoint caps how much time you can request in a
single call (3 days max at this project's 10-minute average) -- fetching
in 24-hour slices stays comfortably under that no matter how many days
you ask for.

Coordinates: sensor latitude/longitude are overridden with the network's
own station-spreadsheet columns (config.json's `stations` list) rather
than trusting whatever PurpleAir itself reports.

Usage:
    PURPLEAIR_API_KEY=xxxx python fetch_data.py [--days 7]
"""
from __future__ import annotations

import json
import argparse
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd

from purpleair_client import get_sensors_by_ids, get_sensor_history

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fetch_data")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())


def main(days: int = 7):
    now = datetime.now(timezone.utc)
    log.info("Fetching a rolling %d-day window ending now (%s UTC)", days, now.strftime("%Y-%m-%d %H:%M:%S"))

    raw_dir = ROOT / CONFIG["output"]["raw_dir"]
    raw_dir.mkdir(parents=True, exist_ok=True)

    station_ids = CONFIG.get("station_ids") or []
    if not station_ids:
        log.warning("station_ids is empty in config.json — nothing to fetch. "
                     "Run scripts/build_station_list.py against the station spreadsheet first.")
        return

    sensors = get_sensors_by_ids(station_ids)
    if not sensors:
        log.warning("PurpleAir returned no sensors for the configured station_ids — check config.json.")
        return

    # Override PurpleAir's own reported latitude/longitude (and attach the
    # spreadsheet S/N) using config.json's `stations` list -- the network's
    # own station spreadsheet is the source of truth for where a station
    # actually is, not PurpleAir's own device-reported GPS.
    coords_by_id = {s["sensor_index"]: (s.get("latitude"), s.get("longitude")) for s in CONFIG.get("stations", [])}
    sn_by_id = {s["sensor_index"]: s.get("sn") for s in CONFIG.get("stations", [])}
    overridden, kept_purpleair = 0, 0
    for s in sensors:
        lat, lon = coords_by_id.get(s.get("sensor_index"), (None, None))
        if lat is not None and lon is not None:
            s["latitude"], s["longitude"] = lat, lon
            overridden += 1
        else:
            kept_purpleair += 1
        s["sn"] = sn_by_id.get(s.get("sensor_index"))
    log.info("Coordinates: %d sensor(s) using spreadsheet latitude/longitude, %d falling back to PurpleAir's own location",
              overridden, kept_purpleair)

    # Metadata (last_seen, rssi, channel_flags, ...) is inherently a
    # *current* snapshot from PurpleAir -- there's no historical metadata
    # API -- so it's saved once per run, shared by both the "Last day" and
    # "Last 7 days" views.
    meta_df = pd.DataFrame(sensors)
    meta_df.to_csv(raw_dir / "sensors_meta.csv", index=False)
    log.info("Saved metadata for %d sensors -> %s", len(sensors), raw_dir / "sensors_meta.csv")

    avg_min = CONFIG["history"]["average_minutes"]
    ok_total, failed_total = 0, 0
    for i in range(days):
        slice_end = now - timedelta(days=i)
        slice_start = slice_end - timedelta(days=1)
        slice_dir = raw_dir / f"slice_{i}"
        slice_dir.mkdir(parents=True, exist_ok=True)
        log.info("Slice %d/%d: %s -> %s UTC", i + 1, days,
                 slice_start.strftime("%Y-%m-%d %H:%M"), slice_end.strftime("%Y-%m-%d %H:%M"))

        ok, failed = 0, []
        for s in sensors:
            idx = s["sensor_index"]
            name = s.get("name", f"sensor_{idx}")
            try:
                rows = get_sensor_history(
                    idx, int(slice_start.timestamp()), int(slice_end.timestamp()), average_minutes=avg_min
                )
                if not rows:
                    log.warning("No history rows for sensor %s (%s) in slice %d", idx, name, i)
                    continue
                df = pd.DataFrame(rows)
                if "time_stamp" in df.columns:
                    df.insert(1, "datetime_utc", pd.to_datetime(df["time_stamp"], unit="s", utc=True))
                df.to_csv(slice_dir / f"sensor_{idx}.csv", index=False)
                ok += 1
            except Exception as exc:
                log.error("Failed to fetch history for sensor %s (%s) in slice %d: %s", idx, name, i, exc)
                failed.append(idx)
        log.info("Slice %d done: %d/%d sensors fetched. Failures: %s", i, ok, len(sensors), failed)
        ok_total += ok
        failed_total += len(failed)

    log.info("Rolling fetch done: %d slice-sensor fetches ok, %d failed, across %d slice(s).",
              ok_total, failed_total, days)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="How many rolling 24h slices to fetch (default 7)")
    args = ap.parse_args()
    main(days=args.days)
