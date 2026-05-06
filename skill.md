# Full-Catalog Disc Brake Caliper Scrape

Execute a full catalog scrape of https://dns.mypartfinder.com for the
Disc Brake Caliper product line. Reuse the cascading-select infra in
`scrape_mypartfinder.py` (Playwright wiring, recovery, table parsing) —
don't rebuild it.

## Iteration strategy
- **Year**: descend 2027 → 1917, using only values present in
  `#year-select`. Skip absent years silently.
- **Make**: every option in `#make-select` for that year.
- **Model**: every option in `#model-select` for that (year, make).
- **Engine**: select the **"All Engines"** aggregate (the
  `<option value="____">`) — do NOT iterate engine sizes. One query per
  `(year, make, model)`.
- **Product Line**: fixed at `"Disc Brake Caliper"`; extract only rows
  under that section's `<h6>`/table.

## Output
One CSV file per 10-year bucket, written to `output/`. Bucket a year `Y`
by its decade floor — e.g. 2020–2029 → `disc_brake_caliper_2020-2029.csv`,
2010–2019 → `disc_brake_caliper_2010-2019.csv`, etc. Each file holds
every row whose `year` falls in that bucket (one row per part). Each
file gets its own header.

Columns in order:

1. Parent context: `year`, `make`, `model`, `engine` (literal `"All Engines"`)
2. Result table: `product_line`, `part_number`, `engine_size`,
   `position`, `vehicle_options`, `application_notes`

The site's table header is `"Vehicle options"` (lowercase 'o'); read it
that way, emit as `vehicle_options`.

## Operational requirements
- **Rate**: 2-second sleep after each query.
- **Resume**: after each Make finishes, write `checkpoint.json` with the
  set of completed `(year, make)` pairs. On startup, load the checkpoint,
  skip completed pairs, and **append** to the matching decade CSV (don't
  overwrite or rewrite the header).
- **Zero results**: log at INFO (`year | make | model -> 0 rows`) and
  continue — no retry, no error.
- **Timeout**: retry the same query once. If it fails again, log a
  warning, record in failures, and move on.

## End-of-run summary (stdout)
- Total `(year, make, model)` combinations queried
- Total rows extracted
- Wall-clock duration (`HH:MM:SS`)
- Failures list: `year/make/model — reason` (may be empty)
