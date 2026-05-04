"""
DNS MyPartFinder Scraper - Disc Brake Caliper Data Extractor
=============================================================

Extracts caliper part information from https://dns.mypartfinder.com/

LEGAL / TOS NOTICE
------------------
JNPSoft's PartCat terms state that search results may not be reformatted,
copied or redisplayed. Use this script only for personal lookups or where
you have explicit permission. Respect the site (rate-limit, no parallel
abuse). You are responsible for your usage.

REQUIREMENTS
------------
    pip install playwright pandas
    playwright install chromium

USAGE
-----
    python scrape_mypartfinder.py
    python scrape_mypartfinder.py --headful           # see the browser
    python scrape_mypartfinder.py --product "Disc Brake Caliper"
    python scrape_mypartfinder.py --makes Audi BMW    # limit to specific makes
    python scrape_mypartfinder.py --years 2018 2019 2020

OUTPUT
------
    disc_brake_caliper.csv     -- all extracted rows
    scrape_log.txt             -- run log (errors, skips, timings)
"""

from __future__ import annotations

import argparse
import csv
import logging
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator

from playwright.sync_api import (sync_playwright, Page,
                                  TimeoutError as PWTimeout,
                                  Error as PWError)


BASE_URL = "https://dns.mypartfinder.com/"
DEFAULT_PRODUCT = "Disc Brake Caliper"
OUTPUT_CSV = Path("disc_brake_caliper.csv")
LOG_FILE = Path("scrape_log.txt")

DELAY_BETWEEN_QUERIES_SEC = 1.0
PAGE_LOAD_TIMEOUT_MS = 30_000
CASCADE_TIMEOUT_MS = 10_000


@dataclass
class CaliperRow:
    year: str
    make: str
    model: str
    engine: str
    product_line: str
    part_number: str
    engine_size: str
    position: str
    vehicle_options: str
    application_notes: str


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("mypartfinder")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    fh = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    return logger


class MyPartFinderScraper:
    """
    The site is a React/MUI SPA backed by api.mypartfinder.com. Year/Make/
    Model/Engine are real <select> elements with cascading population:

        #year-select -> #make-select -> #model-select -> #engine-select

    A separate #product-line-select acts as a UI filter; we don't need to
    set it because we extract by section heading anyway.

    Each product line renders its own MUI Paper with a real <table>:
        <h6>Disc Brake Caliper</h6>
        <table>
            <thead><tr><th>Product Line</th><th>Part #</th>...</tr></thead>
            <tbody><tr><td>...</td>...</tr></tbody>
        </table>

    Skip values: the engine select uses "____" as an "All Engines" sentinel
    that we must not pass back as a real engine.
    """

    SEL_YEAR    = "#year-select"
    SEL_MAKE    = "#make-select"
    SEL_MODEL   = "#model-select"
    SEL_ENGINE  = "#engine-select"
    SENTINELS   = {"", "____", "All Engines", "All Products"}

    def __init__(self, page: Page, log: logging.Logger, product: str):
        self.page = page
        self.log = log
        self.product = product

    def open(self) -> None:
        self.log.info("Opening %s", BASE_URL)
        self.page.goto(BASE_URL, timeout=PAGE_LOAD_TIMEOUT_MS,
                       wait_until="networkidle")
        self.page.wait_for_selector(self.SEL_YEAR, timeout=CASCADE_TIMEOUT_MS)

    def _recover(self, year: str, make_val: str) -> bool:
        """Reload the page and re-cascade through year+make.

        After long sessions the React tree occasionally enters a state where
        children of the cascading selects fail to mount, and every subsequent
        `_select` returns False. Re-navigating fixes it. Returns True if we
        end up back at a state where the model select is populated.
        """
        try:
            self.log.info("Recovering: reload + reselect %s/%s", year, make_val)
            self.page.goto(BASE_URL, timeout=PAGE_LOAD_TIMEOUT_MS,
                           wait_until="networkidle")
            self.page.wait_for_selector(self.SEL_YEAR, timeout=CASCADE_TIMEOUT_MS)
        except (PWTimeout, PWError) as e:
            self.log.warning("Recovery navigate failed: %s", e)
            return False
        if not self._select(self.SEL_YEAR, year):
            return False
        if not self._wait_populated(self.SEL_MAKE):
            return False
        if not self._select(self.SEL_MAKE, make_val):
            return False
        return self._wait_populated(self.SEL_MODEL)

    def _options(self, selector: str) -> list[tuple[str, str]]:
        """Return [(value, label), ...] for the given <select>, skipping sentinels."""
        raw = self.page.eval_on_selector(
            selector,
            "el => Array.from(el.options).map(o => [o.value, o.text])",
        )
        return [(v, t) for v, t in raw
                if v not in self.SENTINELS and t.strip() not in self.SENTINELS]

    def _select(self, selector: str, value: str) -> bool:
        """Set a MUI NativeSelect to `value` and fire a React-friendly change.

        Returns False if the element is not present or detached mid-call —
        the React tree occasionally re-renders the cascading selects, which
        causes a stale-element race. Callers should treat False as a skip.
        """
        try:
            self.page.wait_for_selector(selector, timeout=CASCADE_TIMEOUT_MS,
                                        state="attached")
            self.page.eval_on_selector(
                selector,
                """(el, value) => {
                    Array.from(el.options).forEach(o => o.selected = (o.value === value));
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }""",
                value,
            )
            return True
        except (PWTimeout, PWError):
            return False

    def _wait_populated(self, selector: str, prev_count: int = 0) -> bool:
        """Wait until <select> is enabled and has more than `prev_count` options."""
        try:
            self.page.wait_for_function(
                """({sel, prev}) => {
                    const el = document.querySelector(sel);
                    return el && !el.disabled && el.options.length > prev;
                }""",
                arg={"sel": selector, "prev": prev_count},
                timeout=CASCADE_TIMEOUT_MS,
            )
            return True
        except PWTimeout:
            return False

    def _extract_section(self, year: str, make: str,
                         model: str, engine: str) -> Iterator[CaliperRow]:
        """Find the product's section by <h6> and parse its <table>."""
        section = self.page.locator(
            f'xpath=//h6[normalize-space()="{self.product}"]'
            f'/ancestor::*[contains(@class,"MuiPaper-root")][1]'
        )
        if section.count() == 0:
            return
        table = section.first.locator("table").first
        headers = [h.strip() for h in table.locator("thead th").all_inner_texts()]
        for tr in table.locator("tbody tr").all():
            cells = [c.strip() for c in tr.locator("td").all_inner_texts()]
            row = dict(zip(headers, cells))
            yield CaliperRow(
                year=year, make=make, model=model, engine=engine,
                product_line      = row.get("Product Line", ""),
                part_number       = row.get("Part #", ""),
                engine_size       = row.get("Engine Size", ""),
                position          = row.get("Position", ""),
                vehicle_options   = row.get("Vehicle options", ""),
                application_notes = row.get("Application Notes", ""),
            )

    def crawl(self, makes_filter: list[str] | None = None,
              years_filter: list[str] | None = None) -> Iterator[CaliperRow]:
        self.open()

        years = [v for v, _ in self._options(self.SEL_YEAR)]
        if years_filter:
            wanted = set(years_filter)
            years = [y for y in years if y in wanted]
        self.log.info("Crawling %d years", len(years))

        for y in years:
            if not self._select(self.SEL_YEAR, y):
                self.log.warning("Year select missing for %s; skipping", y)
                continue
            if not self._wait_populated(self.SEL_MAKE):
                self.log.warning("No makes for year %s", y)
                continue

            makes = self._options(self.SEL_MAKE)
            if makes_filter:
                wanted = set(makes_filter)
                makes = [(v, t) for v, t in makes if v in wanted]

            for mk_val, mk_label in makes:
                if not self._select(self.SEL_MAKE, mk_val):
                    self.log.warning("Make select vanished for %s/%s; skipping",
                                     y, mk_label)
                    continue
                # When a previous make populated the model list, the child
                # may stay non-empty briefly; wait for the cascade XHR to
                # complete so we read the new list, not the stale one.
                if not self._wait_populated(self.SEL_MODEL):
                    self.log.warning("No models for %s/%s", y, mk_label)
                    continue

                consecutive_failures = 0
                FAILURE_BUDGET = 3
                for md_val, md_label in self._options(self.SEL_MODEL):
                    if not self._select(self.SEL_MODEL, md_val):
                        self.log.warning("Model select vanished for %s/%s/%s; "
                                         "attempting recovery",
                                         y, mk_label, md_label)
                        if (self._recover(y, mk_val)
                                and self._select(self.SEL_MODEL, md_val)):
                            consecutive_failures = 0
                            self.log.info("Recovered for %s/%s/%s",
                                          y, mk_label, md_label)
                        else:
                            consecutive_failures += 1
                            self.log.warning("Recovery failed for %s/%s/%s "
                                             "(%d/%d); skipping",
                                             y, mk_label, md_label,
                                             consecutive_failures,
                                             FAILURE_BUDGET)
                            if consecutive_failures >= FAILURE_BUDGET:
                                self.log.warning("Abandoning %s/%s after %d "
                                                 "consecutive failures",
                                                 y, mk_label, consecutive_failures)
                                break
                            continue
                    if not self._wait_populated(self.SEL_ENGINE):
                        self.log.warning("No engines for %s/%s/%s",
                                         y, mk_label, md_label)
                        continue

                    for en_val, en_label in self._options(self.SEL_ENGINE):
                        if not self._select(self.SEL_ENGINE, en_val):
                            self.log.warning("Engine select vanished for %s/%s/%s/%s; skipping",
                                             y, mk_label, md_label, en_label)
                            continue
                        try:
                            self.page.wait_for_selector(
                                'h3:has-text("Total Results Found")',
                                timeout=CASCADE_TIMEOUT_MS,
                            )
                        except PWTimeout:
                            self.log.warning("Results never rendered for %s/%s/%s/%s",
                                             y, mk_label, md_label, en_label)
                            continue

                        produced = 0
                        try:
                            for row in self._extract_section(y, mk_label,
                                                             md_label, en_label):
                                produced += 1
                                yield row
                        except PWError as e:
                            self.log.warning("Extract failed for %s/%s/%s/%s: %s",
                                             y, mk_label, md_label, en_label, e)
                        self.log.info("%s | %s | %s | %s -> %d rows",
                                      y, mk_label, md_label, en_label, produced)
                        time.sleep(DELAY_BETWEEN_QUERIES_SEC)


def write_csv(rows: Iterator[CaliperRow], path: Path,
              log: logging.Logger, append: bool = False) -> int:
    count = 0
    fieldnames = list(CaliperRow.__dataclass_fields__.keys())
    mode = "a" if append and path.exists() else "w"
    write_header = mode == "w"
    with path.open(mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))
            f.flush()
            count += 1
            if count % 50 == 0:
                log.info("Wrote %d rows so far...", count)
    return count


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--headful", action="store_true",
                   help="Show the browser window (debugging)")
    p.add_argument("--product", default=DEFAULT_PRODUCT,
                   help=f"Product line filter (default: {DEFAULT_PRODUCT!r})")
    p.add_argument("--makes", nargs="*", default=None,
                   help="Limit to these makes (space-separated)")
    p.add_argument("--years", nargs="*", default=None,
                   help="Limit to these years (space-separated)")
    p.add_argument("--out", default=str(OUTPUT_CSV),
                   help="Output CSV path")
    p.add_argument("--append", action="store_true",
                   help="Append to the output CSV instead of overwriting "
                        "(omits header if the file already exists). Use this "
                        "when resuming after a crash.")
    args = p.parse_args()

    log = setup_logger()
    log.info("Starting scrape | product=%r | makes=%s | years=%s",
             args.product, args.makes or "ALL", args.years or "ALL")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headful)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()

        scraper = MyPartFinderScraper(page, log, args.product)
        try:
            n = write_csv(scraper.crawl(args.makes, args.years),
                          Path(args.out), log, append=args.append)
            log.info("DONE -- %d rows written to %s", n, args.out)
        except Exception as e:
            log.exception("Scrape failed: %s", e)
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
