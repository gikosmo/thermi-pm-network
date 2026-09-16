"""
Regenerates config.json's `station_ids` list from the municipality's
station spreadsheet (data/thermi_station_list.csv — a plain CSV export of
the "ThermiStationList" workbook: SN, ID, Name, PAir Name, Installation,
Uninstalled, MacAdress, Lat, long, Lat Fake, Long Fake).

A station counts as "active" (included) when its Uninstalled column is
blank or "-". Any other value (a date, "archived", "???") marks it retired
and it's excluded. Rows with no MAC address (test/placeholder rows like
"GET_Test_07") are also excluded.

Coordinates come from the spreadsheet's real "Lat"/"long" columns (never
the "Lat Fake"/"Long Fake" columns) and are written into config.json's
`stations` list, keyed by sensor_index. fetch_data.py uses these to
override whatever location PurpleAir itself reports for each sensor, so
the dashboard map always reflects your spreadsheet, not PurpleAir's own
(sometimes stale or approximate) location metadata.

Run this whenever the spreadsheet changes, then commit the updated
config.json:

    python scripts/build_station_list.py
    python scripts/build_station_list.py --csv path/to/NewExport.csv
    python scripts/build_station_list.py --dry-run   # just print, don't write config.json
"""
from __future__ import annotations

import csv
import json
import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_station_list")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "data" / "thermi_station_list.csv"
CONFIG_PATH = ROOT / "config.json"


def load_stations(csv_path: Path) -> tuple[list[dict], list[dict]]:
    active, inactive = [], []
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sn = (row.get("SN") or "").strip()
            if not sn:
                continue

            sensor_id = (row.get("ID") or "").strip()
            mac = (row.get("MacAdress") or "").strip()
            lat = (row.get("Lat ") or row.get("Lat") or "").strip()
            lon = (row.get("long") or "").strip()
            uninstalled = (row.get("Uninstalled") or "").strip()

            entry = {
                "sensor_index": int(sensor_id) if sensor_id.isdigit() else sensor_id,
                "name": (row.get("Name") or "").strip(),
                "pair_name": (row.get("PAir Name") or "").strip(),
                "installed": (row.get("Installation") or "").strip(),
                "uninstalled": uninstalled,
                "mac": mac,
                "latitude": lat or None,
                "longitude": lon or None,
            }

            # A row with no ID or no MAC is a test/placeholder entry (e.g. "GET_Test_07"),
            # not a real deployed sensor -- skip it outright. Missing lat/long alone does
            # NOT disqualify a station (e.g. Thermi-Mandritsa has no coordinates on file
            # but is a real active sensor) -- analyze.py already handles sensors with no
            # location by simply excluding them from the nearest-neighbor comparison.
            is_placeholder = (not sensor_id.isdigit()) or mac in ("", "-")
            is_active = uninstalled in ("", "-")

            if is_placeholder:
                log.info("Skipping placeholder/test row: %s (%s)", entry["name"], sn)
                continue
            if is_active and (lat in (None, "", "-") or lon in (None, "", "-")):
                log.warning("Active station %s (%s) has no coordinates on file -- "
                            "it will still be monitored but won't appear on the map "
                            "or in nearest-neighbor comparisons.", entry["name"], sn)
            (active if is_active else inactive).append(entry)

    return active, inactive


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Path to the station-list CSV")
    ap.add_argument("--dry-run", action="store_true", help="Print the result, don't write config.json")
    args = ap.parse_args()

    active, inactive = load_stations(args.csv)
    log.info("%d active station(s), %d inactive/uninstalled excluded", len(active), len(inactive))
    for e in active:
        log.info("  %s  %s  (lat=%s, lon=%s)", e["sensor_index"], e["name"], e["latitude"], e["longitude"])

    station_ids = [e["sensor_index"] for e in active]
    stations = [
        {
            "sensor_index": e["sensor_index"],
            "name": e["name"],
            "latitude": float(e["latitude"]) if e["latitude"] not in (None, "") else None,
            "longitude": float(e["longitude"]) if e["longitude"] not in (None, "") else None,
        }
        for e in active
    ]

    if args.dry_run:
        print(json.dumps({"station_ids": station_ids, "stations": stations}, indent=2))
        return

    config = json.loads(CONFIG_PATH.read_text())
    config["station_ids"] = station_ids
    config["stations"] = stations
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    log.info("Wrote %d station_ids + coordinates -> %s", len(station_ids), CONFIG_PATH)


if __name__ == "__main__":
    main()
