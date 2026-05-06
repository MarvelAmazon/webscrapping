# Full-Catalog Disc Brake Caliper Scrape

Scrape every Disc Brake Caliper part from https://dns.mypartfinder.com
into one CSV per decade in `output/`. The driver is `scrape_full_caliper.py`,
which reuses the cascading-select infra in `scrape_mypartfinder.py`
(Playwright wiring, recovery, table parsing). **Do not rebuild that
infra** — extend the existing scripts.

## Prerequisites
- `.venv/` activated with `playwright` installed and `playwright install chromium` already run.
- Both scripts in repo root: `scrape_mypartfinder.py` (low-level cascading-select driver) and `scrape_full_caliper.py` (full-catalog harness with CLI args).

## Iteration strategy
- **Year**: descend 2027 → 1917, using only values present in `#year-select`. Skip absent years silently.
- **Make**: every option in `#make-select` for that year.
- **Model**: every option in `#model-select` for that (year, make).
- **Engine**: select the **"All Engines"** aggregate (`<option value="____">`) — do NOT iterate engine sizes. One query per `(year, make, model)`.
- **Product Line**: fixed at `"Disc Brake Caliper"`; extract only rows under that section's `<h6>` / `<table>`.

## Output
One CSV per 10-year bucket in `output/`, named `disc_brake_caliper_<floor>-<floor+9>.csv` where `floor = (year // 10) * 10`. Each file gets its own header. Decades whose every query returns 0 rows produce no file (e.g. 1917-1959 — disc brake calipers postdate that era).

Columns in order:
1. Parent context: `year`, `make`, `model`, `engine` (literal `"All Engines"`)
2. Result table: `product_line`, `part_number`, `engine_size`, `position`, `vehicle_options`, `application_notes`

The site's table header is `"Vehicle options"` (lowercase 'o'); read it that way, emit as `vehicle_options`.

## CLI

`scrape_full_caliper.py` accepts:
- `--year-min INT` / `--year-max INT` — inclusive year range (defaults 1917..2027)
- `--delay FLOAT` — sleep seconds between queries (default 2.0)
- `--checkpoint PATH` — checkpoint file (default `checkpoint.json`)
- `--log PATH` — log file (default `scrape_full.log`)
- `--name STR` — logger name to disambiguate workers (default `full_caliper`)
- `--headful` — show browser (debugging only)

## Run modes

### Mode A — single-stream (polite, ~16-22h, default 2s delay)
```bash
nohup .venv/bin/python -u scrape_full_caliper.py \
    > scrape_full.stdout.log 2>&1 &
```

### Mode B — 4 parallel workers (~9h, decade-aligned shards, 0.5s delay)
**Critical rule**: shard along decade boundaries so workers never write to the same per-decade CSV concurrently. Each worker also uses its own checkpoint + log file.

```bash
nohup .venv/bin/python -u scrape_full_caliper.py \
    --year-min 2000 --year-max 2014 --delay 0.5 \
    --checkpoint checkpoint_w1.json --log scrape_w1.log --name w1 \
    > scrape_w1.stdout.log 2>&1 &

nohup .venv/bin/python -u scrape_full_caliper.py \
    --year-min 1980 --year-max 1999 --delay 0.5 \
    --checkpoint checkpoint_w2.json --log scrape_w2.log --name w2 \
    > scrape_w2.stdout.log 2>&1 &

nohup .venv/bin/python -u scrape_full_caliper.py \
    --year-min 1960 --year-max 1979 --delay 0.5 \
    --checkpoint checkpoint_w3.json --log scrape_w3.log --name w3 \
    > scrape_w3.stdout.log 2>&1 &

nohup .venv/bin/python -u scrape_full_caliper.py \
    --year-min 1917 --year-max 1959 --delay 0.5 \
    --checkpoint checkpoint_w4.json --log scrape_w4.log --name w4 \
    > scrape_w4.stdout.log 2>&1 &
```

Adjust the year-range expansion across workers to cover 2015-2027 if starting fresh (e.g. let w1 own 2010-2027). Do **not** run more than 4 parallel workers — the site is small/regional and aggressive hammering will earn a 429 or IP-block.

## Operational requirements
- **Rate**: 2.0s default; 0.5s when running 4 parallel workers. Don't go below 0.5s.
- **Resume**: each `--checkpoint` file accumulates completed `(year, make)` pairs and is flushed after every Make. On startup the worker loads it and skips completed pairs. CSV writes are append-mode with header-once semantics, so resume is safe.
- **Zero results**: log INFO `year | make | model -> 0 rows` and continue. No retry, no error.
- **Timeout**: retry the same query once. If it fails again, log a warning, record in `stats["failures"]`, and move on.
- **Expected failure rate**: ~5-10 failures across a ~60k-combo full run (transient `engine select never populated` on individual models). Each failed combination is `year/make/model — reason` in the SUMMARY.

## Monitoring (during a run)
```bash
ps -p <PID1> <PID2> <PID3> <PID4> -o pid,etime,rss,command
tail -5 scrape_w{1,2,3,4}.log
wc -l output/*.csv
grep -c FAILED scrape_w*.log
```

Watch for: `etime` advancing but `wc -l` flat for many minutes (worker stalled), or a sudden FAILED spike (site rate-limiting). Either case → kill the affected worker, wait a few minutes, relaunch with the same args (it resumes from checkpoint).

## End-of-run
Each worker prints (and logs) a SUMMARY block:
- Total `(year, make, model)` combinations queried
- Total rows extracted
- Wall-clock duration (`HH:MM:SS`)
- Failures list (`year/make/model — reason`, may be empty)

After all workers exit cleanly:

1. **Unified summary**: concatenate the four SUMMARY blocks (`grep -A10 "FULL CALIPER SCRAPE" scrape_w*.stdout.log`) and the row-per-decade totals (`wc -l output/*.csv`).
2. **Commit + push**:
   ```bash
   git add output/*.csv checkpoint*.json scrape_*.log scrape_*.stdout.log scrape_full_caliper.py skill.md
   git commit -m "..."   # include row total, decade range, runtime
   git push origin main  # never --force, never amend
   ```

## Reference run (sanity check)
A complete 2027→1917 scrape produces ~237k rows across 7 CSVs (1960s-2020s; 1917-1959 yields 0 rows). Anything dramatically smaller indicates an early failure or a misconfigured year-range; anything dramatically larger suggests duplicate writes (workers sharing a decade file).
