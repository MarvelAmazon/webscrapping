"""
Full-catalog Disc Brake Caliper scraper for dns.mypartfinder.com.

Iterates Year (descending, 2027 -> 1917) x Make x Model. Selects the
"All Engines" aggregate per (year, make, model) so each combination is
a single query. Writes one CSV per decade in ./output/ and resumes via
checkpoint.json after each Make completes.

Usage:
    .venv/bin/python scrape_full_caliper.py [--headful]
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import (sync_playwright,
                                  TimeoutError as PWTimeout,
                                  Error as PWError)

from scrape_mypartfinder import (MyPartFinderScraper, CaliperRow,
                                  CASCADE_TIMEOUT_MS)

PRODUCT = "Disc Brake Caliper"
ALL_ENGINES_VALUE = "____"
ALL_ENGINES_LABEL = "All Engines"

DEFAULT_DELAY_SEC = 2.0
DEFAULT_YEAR_LOWER = 1917
DEFAULT_YEAR_UPPER = 2027

OUTPUT_DIR = Path("output")


def setup_logger(log_file: Path, name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler(log_file, mode="a", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


def decade_path(year: str) -> Path:
    floor = (int(year) // 10) * 10
    return OUTPUT_DIR / f"disc_brake_caliper_{floor}-{floor + 9}.csv"


def load_checkpoint(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text())
    return {tuple(p) for p in data.get("completed", [])}


def save_checkpoint(path: Path, completed: set[tuple[str, str]]) -> None:
    path.write_text(json.dumps({
        "completed": sorted(list(completed)),
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))


class DecadeWriter:
    FIELDS = list(CaliperRow.__dataclass_fields__.keys())

    def __init__(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._writers: dict[Path, csv.DictWriter] = {}
        self._files: list = []

    def write(self, row: CaliperRow) -> None:
        path = decade_path(row.year)
        if path not in self._writers:
            new = (not path.exists()) or path.stat().st_size == 0
            f = path.open("a", newline="", encoding="utf-8")
            self._files.append(f)
            w = csv.DictWriter(f, fieldnames=self.FIELDS)
            if new:
                w.writeheader()
            self._writers[path] = w
        self._writers[path].writerow(asdict(row))

    def flush(self) -> None:
        for f in self._files:
            f.flush()

    def close(self) -> None:
        for f in self._files:
            try:
                f.close()
            except Exception:
                pass
        self._files.clear()
        self._writers.clear()


def query_combination(scraper: MyPartFinderScraper, log: logging.Logger,
                      year: str, make_label: str,
                      model_val: str, model_label: str,
                      writer: DecadeWriter, stats: dict,
                      allow_retry: bool = True) -> None:
    """Run a single (year, make, model, "All Engines") query and stream rows.

    On Playwright timeout/error, retry once before recording a failure.
    """
    try:
        if not scraper._select(scraper.SEL_MODEL, model_val):
            raise PWError("model select vanished")
        if not scraper._wait_populated(scraper.SEL_ENGINE):
            raise PWError("engine select never populated")
        if not scraper._select(scraper.SEL_ENGINE, ALL_ENGINES_VALUE):
            raise PWError("All Engines option not selectable")
        scraper.page.wait_for_selector(
            'h3:has-text("Total Results Found")',
            timeout=CASCADE_TIMEOUT_MS,
        )
        produced = 0
        for row in scraper._extract_section(year, make_label, model_label,
                                            ALL_ENGINES_LABEL):
            writer.write(row)
            produced += 1
        stats["combinations"] += 1
        stats["rows"] += produced
        if produced == 0:
            log.info("%s | %s | %s -> 0 rows", year, make_label, model_label)
        else:
            log.info("%s | %s | %s -> %d rows",
                     year, make_label, model_label, produced)
    except (PWTimeout, PWError) as e:
        if allow_retry:
            log.warning("Retrying %s/%s/%s after error: %s",
                        year, make_label, model_label, e)
            time.sleep(1.0)
            query_combination(scraper, log, year, make_label,
                              model_val, model_label, writer, stats,
                              allow_retry=False)
        else:
            stats["failures"].append(
                f"{year}/{make_label}/{model_label} — {e}"
            )
            log.warning("FAILED %s/%s/%s: %s",
                        year, make_label, model_label, e)


def crawl(scraper: MyPartFinderScraper, log: logging.Logger,
          completed: set[tuple[str, str]], writer: DecadeWriter,
          stats: dict, checkpoint_path: Path,
          year_lower: int, year_upper: int,
          delay_sec: float) -> None:
    scraper.open()

    all_years = [v for v, _ in scraper._options(scraper.SEL_YEAR)]
    years = sorted(
        [y for y in all_years
         if y.isdigit() and year_lower <= int(y) <= year_upper],
        key=lambda y: -int(y),
    )
    log.info("Years to crawl: %d (%s..%s)",
             len(years),
             years[0] if years else "-",
             years[-1] if years else "-")

    for y in years:
        if not scraper._select(scraper.SEL_YEAR, y):
            log.warning("Year select missing for %s; skipping", y)
            continue
        if not scraper._wait_populated(scraper.SEL_MAKE):
            log.warning("No makes for year %s", y)
            continue

        makes = scraper._options(scraper.SEL_MAKE)
        for mk_val, mk_label in makes:
            if (y, mk_label) in completed:
                continue
            if not scraper._select(scraper.SEL_MAKE, mk_val):
                log.warning("Make select vanished for %s/%s; skipping",
                            y, mk_label)
                continue
            if not scraper._wait_populated(scraper.SEL_MODEL):
                log.warning("No models for %s/%s", y, mk_label)
                completed.add((y, mk_label))
                save_checkpoint(checkpoint_path, completed)
                continue

            for md_val, md_label in scraper._options(scraper.SEL_MODEL):
                query_combination(scraper, log, y, mk_label, md_val,
                                  md_label, writer, stats)
                time.sleep(delay_sec)

            completed.add((y, mk_label))
            save_checkpoint(checkpoint_path, completed)
            writer.flush()
            log.info("Make complete: %s / %s "
                     "(combos=%d rows=%d failures=%d)",
                     y, mk_label, stats["combinations"],
                     stats["rows"], len(stats["failures"]))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--headful", action="store_true",
                   help="Show the browser window (debugging)")
    p.add_argument("--year-min", type=int, default=DEFAULT_YEAR_LOWER,
                   help="Lowest year to crawl (inclusive)")
    p.add_argument("--year-max", type=int, default=DEFAULT_YEAR_UPPER,
                   help="Highest year to crawl (inclusive)")
    p.add_argument("--delay", type=float, default=DEFAULT_DELAY_SEC,
                   help="Sleep seconds after each query")
    p.add_argument("--checkpoint", default="checkpoint.json",
                   help="Checkpoint file path")
    p.add_argument("--log", default="scrape_full.log",
                   help="Log file path")
    p.add_argument("--name", default="full_caliper",
                   help="Logger name (used to disambiguate workers)")
    args = p.parse_args()

    checkpoint_path = Path(args.checkpoint)
    log_path = Path(args.log)
    log = setup_logger(log_path, args.name)
    completed = load_checkpoint(checkpoint_path)
    writer = DecadeWriter()
    stats = {"combinations": 0, "rows": 0, "failures": []}

    started = datetime.now()
    log.info("=" * 60)
    log.info("Starting %s | years=%d..%d | delay=%.2fs | resume=%d pairs",
             args.name, args.year_min, args.year_max, args.delay,
             len(completed))

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headful)
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"),
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()
            scraper = MyPartFinderScraper(page, log, PRODUCT)
            try:
                crawl(scraper, log, completed, writer, stats,
                      checkpoint_path, args.year_min, args.year_max,
                      args.delay)
            finally:
                browser.close()
    finally:
        writer.close()

    duration = datetime.now() - started
    hms = str(timedelta(seconds=int(duration.total_seconds())))
    summary = [
        "",
        "=" * 60,
        "FULL CALIPER SCRAPE — SUMMARY",
        "=" * 60,
        f"Combinations queried : {stats['combinations']}",
        f"Rows extracted       : {stats['rows']}",
        f"Duration             : {hms}",
        f"Failures             : {len(stats['failures'])}",
    ]
    for f in stats["failures"]:
        summary.append(f"  - {f}")
    text = "\n".join(summary)
    print(text)
    log.info(text)


if __name__ == "__main__":
    main()
