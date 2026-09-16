"""
Single daily entry point: fetches a rolling window of raw history
(fetch_data.py) then rebuilds both dashboard views (analyze.py) from it.

Run this once a day, any time you like -- both "Last day" and "Last N
days" will always end exactly at the moment you ran it, not at a calendar
boundary. Run it at 10:00 today and "Last day" covers 10:00 yesterday ->
10:00 today; run it again tomorrow at 10:00 and it shifts to 10:00 today
-> 10:00 tomorrow, automatically.

Usage:
    PURPLEAIR_API_KEY=xxxx python backfill.py            # last 7 days
    PURPLEAIR_API_KEY=xxxx python backfill.py --days 14  # last 14 days
"""
from __future__ import annotations

import argparse
import logging

import fetch_data
import analyze

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="Rolling window length in days (default 7)")
    args = ap.parse_args()

    fetch_data.main(days=args.days)
    analyze.main(days=args.days)


if __name__ == "__main__":
    main()
