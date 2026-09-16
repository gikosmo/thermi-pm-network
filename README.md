# Thermi PM network — PurpleAir sensor QC agent (v2)

Downloads a rolling window of PM data for every active PurpleAir sensor in
the Thermi network (selected from your own station spreadsheet, not a map
bounding box), checks each sensor's two internal channels (A and B) against
each other with a linear regression (slope, intercept, R², RMSE), flags
sensors whose channels disagree or that have gone offline, and publishes the
results as a small static dashboard.

Every PurpleAir sensor has two independent laser particle counters (channel A
and channel B) measuring the same air. Healthy sensors: A ≈ B, so slope ≈ 1
and R² close to 1. A sensor with a fouled/failing laser, a hardware fault, or
a firmware issue will show the two channels drifting apart — that's the
signal this pipeline is built to catch. Offline sensors are caught
separately via PurpleAir's `last_seen` timestamp.

## How it works

```
scripts/build_station_list.py -> reads your station spreadsheet (CSV export)
                                  and writes the list of currently-active
                                  station IDs/coordinates/S/N into config.json
                                  ("Uninstalled" column blank or "-" = active)

scripts/fetch_data.py  -> for every active station, pulls current metadata
                           + 10-min history from the PurpleAir API as a
                           genuine ROLLING window ending at the moment the
                           script runs (not UTC midnight): one 24h slice per
                           day, saved to data/raw/slice_0 .. slice_<N-1>/

scripts/analyze.py     -> pools the slices into two views — "Last day"
                           (slice_0 alone) and "Last N days" (all slices) —
                           computes A-vs-B regression stats per sensor,
                           classifies each sensor as OK / WARNING / FAULTY /
                           OFFLINE, and writes site/data/latest.json and
                           site/data/last7.json

scripts/backfill.py    -> single daily entry point: runs fetch_data.py then
                           analyze.py. This is what you actually run/schedule.

site/index.html         -> static dashboard: map + status table + per-sensor
                           scatter (A vs B) and time-series charts (this
                           sensor + nearest neighbors). Reads the JSON files
                           above via fetch(), no backend needed.
```

Because both windows are always a fixed, complete rolling span ending at run
time, "Last day" and "Last 7 days" never show partial/stale data — run it at
any time of day and both windows simply end there. Run it again tomorrow and
they shift forward automatically.

## 1. Get a PurpleAir API key

Sign in at <https://develop.purpleair.com/>, go to the "Keys" tab, and
create a **read** key. Free for reasonable non-commercial use; PurpleAir may
ask about your use case.

## 2. Point it at your station list

Export your network's station spreadsheet as CSV (columns include `SN`,
`ID` — the PurpleAir sensor index — `Name`, `latitude`, `longitude`, and
`Uninstalled`). Then run:

```bash
python3 scripts/build_station_list.py path/to/your_station_list.csv
```

This writes `station_ids` / `stations` into `config.json`. A row counts as
an **active** station only if it has a numeric PurpleAir `ID` *and* its
`Uninstalled` column is blank or `-` — anything else (a date, `?`,
`archived`, ...) excludes it. Re-run this whenever the spreadsheet changes
(new station installed, one decommissioned, etc.).

Also review the `thresholds` block in `config.json` — the defaults (R² <
0.90 = warning, < 0.70 = fault; slope outside 0.85–1.15 = warning, outside
0.5–2.0 = fault; offline after 2 hours of silence) are reasonable starting
points but you should tune them against a few weeks of your own network's
normal behavior.

## 3. Run it locally

```bash
cd purpleair_agent
pip install -r requirements.txt
export PURPLEAIR_API_KEY=your_read_key_here

python3 scripts/backfill.py            # rolling 7-day window, ending now
python3 scripts/backfill.py --days 14  # or a different window length

# view the dashboard
python3 -m http.server 8000 --directory site
# open http://localhost:8000
```

## 4. Automate it — two options

**Option A: your own machine + cron/Task Scheduler** — use
`scripts/run_daily.sh`:

```cron
0 4 * * *  PURPLEAIR_API_KEY=xxxx /path/to/purpleair_agent/scripts/run_daily.sh >> /path/to/purpleair_agent/logs/daily.log 2>&1
```

**Option B: GitHub Actions + GitHub Pages** (no machine to keep running, and
gives you a public URL for free):

1. Push this folder to a GitHub repo (e.g. `thermi-pm-network`).
2. Repo Settings → Secrets and variables → Actions → New repository secret:
   name `PURPLEAIR_API_KEY`, value = your key.
3. Repo Settings → Pages → Source: "GitHub Actions".
4. The included workflow (`.github/workflows/daily.yml`) runs once a day,
   fetches + analyzes the rolling window via `scripts/backfill.py`, commits
   the refreshed `site/data/*.json`, and deploys `site/` to GitHub Pages.
   You can also trigger it manually from the Actions tab ("Run workflow"),
   optionally overriding the window length.
5. Your dashboard will be live at
   `https://<your-username>.github.io/<repo-name>/`.

## 5. Reading the dashboard

- **Map**: sensors colored by status (green=OK, amber=WARNING, red=FAULTY,
  gray=OFFLINE). Click a marker for detail.
- **View toggle**: "Last day" (rolling 24h) vs "Last 7 days" (rolling
  window, length set by `--days`).
- **Table**: worst-first. Click a row for the per-sensor scatter plot (A vs
  B, with the 1:1 reference line), PM time series, and — compared against
  its two nearest neighboring sensors — a combined time series and a
  cross-correlation explorer.

## Extending this

- **Alerting**: `analyze.py`'s per-window `counts` and each sensor's
  `status`/`reasons` are easy to pipe into a Slack webhook, email, or
  Telegram bot — add a step at the end of `build_window_summary()` or a
  small script that reads `site/data/latest.json`.
- **Different averaging window**: `config.json → history.average_minutes`
  (PurpleAir supports 0/10/30/60/360/1440-minute averages).
- **Indoor sensors / different pollutants**: the PurpleAir API also exposes
  PM1.0, PM10, and other fields — add them to `HISTORY_FIELDS` in
  `scripts/purpleair_client.py` if you want them in the QC or the dashboard.

## Notes / limitations

- PurpleAir's history endpoint caps how much time you can request in a
  single call (3 days at this project's 10-minute average) — `fetch_data.py`
  works around this by fetching one 24h slice per day regardless of window
  length.
- `last_seen`-based offline detection depends on your `offline_hours`
  threshold — too short and normal brief WiFi hiccups will flag as
  "offline"; too long and a genuinely dead sensor sits unnoticed longer.
- The A-vs-B check catches disagreement *between a sensor's own two
  channels* — it does not by itself confirm absolute accuracy against a
  reference monitor. For that you'd want an occasional co-location
  comparison against a regulatory-grade station, which is a separate
  (valuable) exercise from this daily automated QC.
