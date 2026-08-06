"""Download a company's sustainability report and extract Scope 1/2/3 emissions.

Unlike financial filings, sustainability reports have no central registry — there
is no EDGAR for them. So this module supports three ways of locating one:

    python sustainability_report.py GOOGL                     # built-in registry
    python sustainability_report.py ACME --url https://.../report.pdf
    python sustainability_report.py ACME --file ./report.pdf
    python sustainability_report.py ACME --page https://acme.com/sustainability

Usage:
    python sustainability_report.py GOOGL
    python sustainability_report.py AAPL --json
"""

import argparse
import difflib
import html.parser
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from esg_common import Metric, looks_like_prose, pick_latest, row_values, year_columns

USER_AGENT = os.environ.get(
    "ESG_USER_AGENT",
    "Mozilla/5.0 (compatible; esg-kpi-register/1.0; +mailto:contact@example.com)",
)

# Sustainability reports are published on corporate sites at URLs that rotate
# each year. Every entry below was confirmed reachable on 2026-08-07; if one
# 404s, pass --url or --page instead of trusting this table.
# Publishers move these URLs every year, so the registry is treated as a hint
# rather than a fact. Each entry also names a landing page, and resolution
# verifies the URL, looks for a newer edition, and falls back to scraping the
# landing page before giving up. See resolve_registry_entry().
REPORT_REGISTRY = {
    "GOOGL": {
        # Google moved hosts between editions — the 2025 report sits on gstatic,
        # the 2026 on sustainability.google — which is exactly why the landing
        # page matters as much as the recorded URL.
        "url": "https://sustainability.google/files/google-2026-environmental-report.pdf",
        "title": "Google {year} Environmental Report",
        "page": "https://sustainability.google/",
    },
    "AAPL": {
        "url": "https://www.apple.com/environment/pdf/Apple_Environmental_Progress_Report_2026.pdf",
        "title": "Apple Environmental Progress Report {year}",
        "page": "https://www.apple.com/environment/",
    },
    "AMZN": {
        "url": "https://sustainability.aboutamazon.com/2025-amazon-sustainability-report.pdf",
        "title": "Amazon {year} Sustainability Report",
        "page": "https://sustainability.aboutamazon.com/reporting",
    },
    "NVDA": {
        "url": "https://images.nvidia.com/aem-dam/Solutions/documents/"
               "NVIDIA-Sustainability-Report-Fiscal-Year-2026.pdf",
        "title": "NVIDIA FY{year} Sustainability Report",
        "page": "https://www.nvidia.com/en-us/sustainability/",
    },
}
REPORT_REGISTRY["GOOG"] = REPORT_REGISTRY["GOOGL"]

# Row labels we look for, in preference order. The first pattern that yields a
# well-formed table row wins.
EMISSION_ROWS = {
    "Scope 1": [
        r"^\s*Scope\s*1\b(?!.*\+)(?!.*(?:location|market))",
        r"^\s*(?:Total\s+)?Scope\s*1\s+emissions\b",
        # Amazon-style: "Emissions from Direct Operations (Scope 1)"
        r"^[^()]{0,70}\(\s*Scope\s*1\s*\)",
    ],
    # NVIDIA writes "Scope 2, market-based"; others use parentheses or nothing.
    "Scope 2 (market-based)": [
        r"^\s*Scope\s*2\s*[,(]?\s*market[-\s]based",
        r"^\s*Market[-\s]based\s+scope\s*2",
        r"^[^()]{0,70}\(\s*Scope\s*2\s*\)",
    ],
    "Scope 2 (location-based)": [
        r"^\s*Scope\s*2\s*[,(]?\s*location[-\s]based",
        r"^\s*Location[-\s]based\s+scope\s*2",
    ],
    # "Scope 3" is not one number across publishers: Google reports a single
    # "Scope 3 (total)", Apple splits corporate from product and only the
    # combined row is comparable. Match the total first and treat a bare
    # "Scope 3" row as the last resort.
    "Scope 3": [
        r"^\s*Total\s+(?:gross\s+)?scope\s*3\b",
        r"^\s*Scope\s*3\s*\(?\s*total\s*\)?",
        r"^\s*Gross\s+emissions\s*\(\s*scope\s*3\s*\)",
        r"^\s*Scope\s*3\s+emissions\s+\(total\)",
        r"^\s*Scope\s*3\b(?!.*Category)(?!.*\()",
        r"^[^()]{0,70}\(\s*Scope\s*3\s*\)",
    ],
}

# Publishers state units either on the row or in a caption above the block:
# "tCO2e", "(MMT CO2e)", "metric tons CO2e", "million metric tons".
UNIT_PATTERN = re.compile(
    r"(?i)\b(MMT\s*CO2e|million\s+metric\s+tons?(?:\s+(?:of\s+)?CO2e)?|"
    r"tCO\s?2?\s?e|mtCO\s?2?\s?e|MT\s*CO2e|metric\s+tons?(?:\s+(?:of\s+)?CO2e)?|"
    r"tonnes?\s*(?:of\s+)?CO2e)\b"
)

SUBSCRIPTS = str.maketrans({"₂": "2", "₃": "3", " ": " ", " ": " "})


# Resource-use KPIs. Unlike GHG scopes these are barely standardised, so each
# metric lists the exact total rows worth trusting. A near-miss is deliberately
# not accepted: "waste diverted from landfill" is not "waste generated", and
# "renewable electricity used" is not total energy consumption. Where a company
# publishes no matching total the value stays empty rather than becoming wrong.
WATER_UNITS = [
    (r"(?i)megalit(?:er|re)s", 1_000.0),
    (r"(?i)cubic\s+met(?:er|re)s|\bm3\b", 1.0),
    (r"(?i)\bMgal\b", 3_785.411784),
    (r"(?i)\bgallons?\b", 0.003785411784),
    (r"(?i)\blit(?:er|re)s\b", 0.001),
]

# Publishers often print the magnitude and the base unit as separate words, and
# PDF extraction can pull them to opposite ends of the row — Google's water table
# renders as "Million  Water  withdrawal  …  gallons". Detecting the two parts
# independently avoids reading 11,011 million gallons as 11,011 gallons.
MAGNITUDES = [
    (r"(?i)\bbillions?\b", 1_000_000_000.0),
    (r"(?i)\bmillions?\b", 1_000_000.0),
    (r"(?i)\bthousands?\b", 1_000.0),
]
WASTE_UNITS = [
    (r"(?i)metric\s+tons?|\btonnes?\b", 1.0),
    (r"(?i)short\s+tons?", 0.90718474),
    (r"(?i)\bpounds?\b|\blbs?\b", 0.00045359237),
    (r"(?i)kilograms?|\bkg\b", 0.001),
]
ENERGY_UNITS = [
    (r"(?i)\bTWh\b|terawatt[-\s]hours?", 1_000_000.0),
    (r"(?i)\bGWh\b|gigawatt[-\s]hours?", 1_000.0),
    (r"(?i)\bMWh\b|megawatt[-\s]hours?", 1.0),
    (r"(?i)\bkWh\b|kilowatt[-\s]hours?", 0.001),
    (r"(?i)\bGJ\b|gigajoules?", 0.2777778),
]

PERCENT_UNITS = [(r"(?i)%|\bpercent\b", 1.0)]

RESOURCE_ROWS = {
    "Water withdrawal": {
        # Apple labels its withdrawal row "Freshwater" (with a footnote digit
        # glued on), and separately reports "Freshwater saved", which is a
        # saving rather than a withdrawal and must not be picked up.
        "patterns": [
            r"Total\s+water\s+withdraw\w*",
            r"\bWater\s+withdrawal\b",
            r"^\s*Freshwater\d*\b(?!.*\bsav)",
        ],
        "units": WATER_UNITS,
        "canonical": "m3",
    },
    "Water consumption": {
        # Google splits the label across the row: "Water  Million  … consumption gallons".
        "patterns": [r"Total\s+water\s+consum\w*", r"\bWater\b[^\n]*\bconsumption\b"],
        "units": WATER_UNITS,
        "canonical": "m3",
    },
    "Waste diverted": {
        # Deliberately strict: Google prints per-site "Waste diverted" rows with no
        # company total, and matching those would report a fraction as the whole.
        "patterns": [r"Total\s+waste\s+diverted\b", r"Waste\s+diverted\s+from\s+landfill\b"],
        "units": WASTE_UNITS,
        "canonical": "metric tons",
    },
    "Waste diversion rate": {
        "patterns": [r"Total\s+waste\s+diversion\s+rate\b", r"Waste\s+diversion\s+rate\b"],
        "units": PERCENT_UNITS,
        "canonical": "%",
    },
    "Renewable electricity": {
        "patterns": [r"Total\s+renewable\s+electricity\b", r"Renewable\s+electricity\s+used\b"],
        "units": ENERGY_UNITS,
        "canonical": "MWh",
    },
    "Waste generated": {
        "patterns": [
            r"^\s*Total\s+waste\s+generated\b",
            r"^\s*Total\s+waste\b(?!.*divert)",
            r"^\s*Waste\s+generated\b",
        ],
        "units": WASTE_UNITS,
        "canonical": "metric tons",
    },
    "Energy consumption": {
        "patterns": [
            r"^\s*Total\s+energy\s+consumption\b",
            r"^\s*Total\s+electricity\s+consumption\b",
        ],
        "units": ENERGY_UNITS,
        "canonical": "MWh",
    },
}


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


class _PdfLinkFinder(html.parser.HTMLParser):
    """Collect PDF links (and their anchor text) from a landing page."""

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            self._href = href if ".pdf" in href.lower() else None
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            # Deep links carry a #page= fragment; strip it so the same document
            # linked from several places collapses to one candidate.
            resolved = urllib.parse.urljoin(self.base, self._href).split("#", 1)[0]
            self.links.append((resolved, " ".join(self._text).strip()))
            self._href = None


def discover_from_page(page_url):
    """Scrape a sustainability landing page for candidate report PDFs.

    Ranks links whose URL or anchor text mentions a report-ish word, newest first.
    """
    finder = _PdfLinkFinder(page_url)
    finder.feed(_get(page_url).decode("utf-8", errors="replace"))

    def score(link):
        url, text = link
        blob = f"{url} {text}".lower()
        points = sum(
            w in blob
            for w in ("sustainability", "environment", "esg", "climate", "impact", "responsibility")
        )
        years = [int(y) for y in re.findall(r"(20\d{2})", blob)]
        return (points, max(years) if years else 0)

    return sorted(set(finder.links), key=score, reverse=True)


def head_ok(url, timeout=20):
    """True when the URL responds and looks like a PDF."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            return resp.status == 200 and ("pdf" in ctype.lower() or url.lower().endswith(".pdf"))
    except Exception:
        return False


def filename_shape(url):
    """The URL's filename with digits flattened, for comparing editions.

    '2025-amazon-sustainability-report.pdf' and the 2026 equivalent collapse to
    the same shape, while '2025-aws-summary.pdf' stays clearly different.
    """
    name = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1].lower()
    return re.sub(r"\d+", "#", name)


def year_in(text):
    """The most recent 4-digit year mentioned, or None."""
    years = [int(y) for y in re.findall(r"(?:19|20)\d{2}", text or "")]
    return max(years) if years else None


def newer_edition(url, ahead=2):
    """Look for a later annual edition by bumping the year in the URL.

    Publishers keep their filename scheme and change the year, so
    google-2025-environmental-report.pdf becomes google-2026-… . Checking a
    couple of years ahead means a new release is picked up without an edit.
    """
    current = year_in(url)
    if not current:
        return None
    for candidate in range(current + ahead, current, -1):
        probe = re.sub(rf"\b{current}\b", str(candidate), url)
        if probe != url and head_ok(probe):
            return probe
    return None


def resolve_registry_entry(entry):
    """Turn a registry hint into a live URL, healing it if the publisher moved on.

    Order: a newer edition if one exists, else the recorded URL if it still
    works, else whatever the landing page currently advertises.
    """
    recorded = entry["url"]

    upgraded = newer_edition(recorded)
    if upgraded:
        return upgraded, entry["title"].format(year=year_in(upgraded) or ""), "newer edition found"

    if head_ok(recorded):
        return recorded, entry["title"].format(year=year_in(recorded) or ""), ""

    landing = entry.get("page")
    if landing:
        try:
            candidates = discover_from_page(landing)
        except Exception:
            candidates = []
        # Rank by resemblance to the filename we used to fetch. A landing page
        # lists many PDFs — Amazon's carries an AWS summary and several
        # methodology notes — and only the one shaped like last year's report is
        # the same publication in a new edition.
        target = filename_shape(recorded)
        ranked = sorted(
            candidates,
            key=lambda c: difflib.SequenceMatcher(None, target, filename_shape(c[0])).ratio(),
            reverse=True,
        )
        for candidate, _text in ranked:
            if head_ok(candidate):
                return (
                    candidate,
                    entry["title"].format(year=year_in(candidate) or ""),
                    f"registry URL is dead; recovered from {landing}",
                )

    raise LookupError(
        f"The recorded report URL no longer works and nothing usable was found on "
        f"{entry.get('page') or 'the landing page'}. Pass --url with the current "
        "report and update REPORT_REGISTRY."
    )


def resolve_report(ticker, url=None, file=None, page=None):
    """Work out where the report lives. Returns (label, source_url, pdf_bytes)."""
    if file:
        with open(file, "rb") as fh:
            return os.path.basename(file), None, fh.read()
    if url:
        return url, url, _get(url)
    if page:
        candidates = discover_from_page(page)
        if not candidates:
            raise LookupError(f"No PDF links found on {page}")
        best = candidates[0][0]
        return best, best, _get(best)

    key = ticker.strip().upper()
    if key not in REPORT_REGISTRY:
        raise LookupError(
            f"No sustainability report on file for {ticker!r}. There is no EDGAR "
            "equivalent for these reports, so pass --url <pdf>, --file <path>, or "
            "--page <company sustainability page>."
        )
    entry_url, title, healing = resolve_registry_entry(REPORT_REGISTRY[key])
    if healing:
        print(f"  note: {healing} -> {entry_url}", file=sys.stderr)
    return title, entry_url, _get(entry_url)


def pdf_pages(data, cache_path=None):
    """Yield (page_number, text) for each page. Page numbers are 1-based PDF pages."""
    from pypdf import PdfReader

    if cache_path:
        with open(cache_path, "wb") as fh:
            fh.write(data)
        reader = PdfReader(cache_path)
    else:
        import io

        reader = PdfReader(io.BytesIO(data))

    for i, page in enumerate(reader.pages, start=1):
        try:
            yield i, (page.extract_text() or "")
        except Exception:
            yield i, ""


def extract_emissions(pages, source=""):
    """Find Scope 1/2/3 rows in a GHG inventory table, newest reporting year.

    Scans page by page so a year header from one page can never be applied to a
    row on another.
    """
    candidates = {label: [] for label in list(EMISSION_ROWS) + list(RESOURCE_ROWS)}

    for page_number, text in pages:
        text = text.translate(SUBSCRIPTS)
        lines = text.split("\n")

        if re.search(r"(?i)\b(water|waste|energy|electricity)\b", text):
            for entry in extract_resources(lines, page_number, source):
                candidates[entry[-1].name].append(entry)

        if not re.search(r"(?i)scope\s*[123]", text):
            continue

        for label, patterns in EMISSION_ROWS.items():
            for rank, pattern in enumerate(patterns):
                matched_this_pattern = False
                for i, line in enumerate(lines):
                    if not re.search(pattern, line, re.IGNORECASE) or looks_like_prose(line):
                        continue
                    years = year_columns(lines, i, lookback=40)
                    values = row_values(lines, i, expect=len(years) or None)
                    # A sentence that merely mentions a scope carries at most one
                    # number; an inventory row carries one per reporting year.
                    if len(values) < 2 or not years:
                        continue
                    value, year = pick_latest(values, years)
                    if value is None:
                        continue
                    value, unit, unit_note = normalise_units(value, lines, i)
                    aligned = len(values) == len(years)
                    candidates[label].append(
                        (
                            -rank,          # earlier pattern = more specific label
                            aligned,
                            year or 0,
                            Metric(
                                name=label,
                                value=value,
                                unit=unit,
                                year=year,
                                page=page_number,
                                source=source,
                                note="; ".join(
                                    n
                                    for n in (
                                        ""
                                        if aligned
                                        else "column alignment uncertain — verify against source page",
                                        unit_note,
                                    )
                                    if n
                                ),
                            ),
                        )
                    )
                    matched_this_pattern = True
                if matched_this_pattern:
                    break

    return {
        label: max(found, key=lambda c: c[:-1])[-1]
        for label, found in candidates.items()
        if found
    }


def convert_resource(value, lines, index, unit_table, lookback=3):
    """Rescale a resource figure to its canonical unit.

    Returns (value, matched_unit_text) or (None, None) when no unit is stated.
    The unit sits on the row itself or in the caption just above it, and the
    lookback is deliberately short: borrowing a unit from a distant table would
    silently produce a figure that is wrong by orders of magnitude.
    """
    for j in range(index, max(-1, index - lookback) - 1, -1):
        for pattern, factor in unit_table:
            match = re.search(pattern, lines[j])
            if not match:
                continue
            label = match.group(0)
            for magnitude_pattern, multiplier in MAGNITUDES:
                magnitude = re.search(magnitude_pattern, lines[j])
                if magnitude:
                    return value * factor * multiplier, f"{magnitude.group(0)} {label}"
            return value * factor, label
    return None, None


OWN_OPERATIONS = re.compile(
    r"(?i)\b(corporate\s+facilit|own\s+operations|direct\s+operations|our\s+facilit)"
)
SUPPLY_CHAIN = re.compile(r"(?i)\b(supply\s*chain|supplier\s+facilit|suppliers?\b)")


def supplier_context(lines, index, lookback=12):
    """1 when the row belongs to a supply-chain section, else 0.

    Apple prints "Renewable electricity used" twice on one page — once under
    "Corporate facilities" (3,737,000 MWh) and once under "Supply chain"
    (38,300,000 MWh). A company KPI register means own operations, and the two
    differ tenfold, so this is not a rounding error.

    Only the NEAREST heading counts. An earlier, unrelated "Supplier facilities"
    subsection sits above the corporate row, so scanning for any mention at all
    would wrongly tar both rows with the same brush.
    """
    for j in range(index - 1, max(-1, index - lookback) - 1, -1):
        if SUPPLY_CHAIN.search(lines[j]):
            return 1
        if OWN_OPERATIONS.search(lines[j]):
            return 0
    return 0


def extract_resources(lines, page_number, source):
    """Find water, waste, and energy totals on one page of a report."""
    found = []
    for label, spec in RESOURCE_ROWS.items():
        for rank, pattern in enumerate(spec["patterns"]):
            matched = []
            for i, line in enumerate(lines):
                if not re.search(pattern, line, re.IGNORECASE) or looks_like_prose(line):
                    continue
                years = year_columns(lines, i, lookback=40)
                values = row_values(
                    lines, i,
                    relaxed=spec["canonical"] == "%",
                    expect=len(years) or None,
                )
                if len(values) < 2 or not years:
                    continue
                value, year = pick_latest(values, years)
                if value is None:
                    continue
                converted, unit_text = convert_resource(value, lines, i, spec["units"])
                if converted is None:
                    continue
                aligned = len(values) == len(years)
                note = "" if aligned else "column alignment uncertain — verify against source page"
                if unit_text and unit_text.strip().lower() not in spec["canonical"].lower():
                    note = "; ".join(
                        n for n in (note, f"converted from {unit_text.strip()}") if n
                    )
                matched.append((
                    -rank, -supplier_context(lines, i), aligned, year or 0,
                    Metric(
                        name=label, value=converted, unit=spec["canonical"],
                        year=year, page=page_number, source=source, note=note,
                    ),
                ))
            if matched:
                # Apple prints "Renewable electricity used" twice on one page — its
                # own operations and its supply chain. Same label, different scope,
                # so say so rather than silently taking whichever came first.
                distinct = {entry[-1].value for entry in matched}
                if len(distinct) > 1:
                    for entry in matched:
                        entry[-1].note = "; ".join(
                            n for n in (
                                entry[-1].note,
                                f"{len(distinct)} rows on this page matched this label "
                                "— confirm which reporting boundary you want",
                            ) if n
                        )
                found.extend(matched)
                break
    return found


def normalise_units(value, lines, index, lookback=12):
    """Convert a row's figure to tCO2e. Returns (value, unit, note).

    The unit is often declared in a caption above the block rather than on the
    row itself (Amazon writes "Emissions Category (MMT CO2e)" once, then lists
    figures like 15.13), so we scan upward for the nearest declaration.
    """
    for j in range(index, max(-1, index - lookback) - 1, -1):
        match = UNIT_PATTERN.search(lines[j])
        if not match:
            continue
        raw = re.sub(r"\s+", " ", match.group(1)).lower()
        if raw.startswith("mmt") or "million" in raw:
            return value * 1_000_000, "tCO2e", ""
        return value, "tCO2e", ""

    # No declared unit. A corporate inventory in tonnes is never a small
    # decimal, so flag rather than silently reporting an implausible figure.
    if value < 1000:
        return value, "tCO2e", "unit not stated in source — value may be in millions of tonnes"
    return value, "tCO2e", ""


def fetch_emissions(ticker, url=None, file=None, page=None, cache_dir=None):
    """One-call pipeline: ticker -> report PDF -> page-traced Scope 1/2/3 metrics.

    Returns (label, source_url, {label: Metric}).
    """
    label, source_url, data = resolve_report(ticker, url=url, file=file, page=page)
    cache_path = None
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9]+", "_", ticker).strip("_") or "report"
        cache_path = os.path.join(cache_dir, f"{safe}_sustainability.pdf")
    return label, source_url, extract_emissions(pdf_pages(data, cache_path), source=label)


def main():
    parser = argparse.ArgumentParser(description="Extract Scope 1/2/3 emissions from a sustainability report.")
    parser.add_argument("company", nargs="?", help="ticker, e.g. GOOGL")
    parser.add_argument("--check-registry", action="store_true",
                        help="verify every registry URL and report what has moved")
    parser.add_argument("--url", help="direct PDF URL (overrides the registry)")
    parser.add_argument("--file", help="local PDF path")
    parser.add_argument("--page", help="landing page to scrape for the report PDF")
    parser.add_argument("--dir", default="reports", help="cache directory for downloads")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    args = parser.parse_args()

    if args.check_registry:
        seen = set()
        stale = 0
        for ticker, entry in REPORT_REGISTRY.items():
            if id(entry) in seen:
                continue
            seen.add(id(entry))
            try:
                resolved, title, healing = resolve_registry_entry(entry)
            except LookupError as exc:
                stale += 1
                print(f"{ticker:<6} BROKEN   {exc}")
                continue
            if resolved == entry["url"] and not healing:
                print(f"{ticker:<6} ok       {title}")
            else:
                stale += 1
                print(f"{ticker:<6} MOVED    {title}\n{'':<14}{healing}\n{'':<14}{resolved}")
        print(f"\n{len(seen)} entries checked, {stale} need attention.")
        if stale:
            print("Update REPORT_REGISTRY in this file with the resolved URLs above.")
        return

    if not args.company:
        parser.error("a company is required unless --check-registry is used")

    try:
        source, _source_url, metrics = fetch_emissions(
            args.company, url=args.url, file=args.file, page=args.page, cache_dir=args.dir
        )
    except (LookupError, OSError) as exc:
        sys.exit(f"error: {exc}")
    except urllib.error.HTTPError as exc:
        sys.exit(f"error: HTTP {exc.code} fetching the report")

    if args.json:
        print(json.dumps([m.as_row() for m in metrics.values()], indent=2))
        return

    print(f"{args.company.upper()} — {source}")
    if not metrics:
        print("  no environmental data tables found")
    for label in ("Scope 1", "Scope 2 (market-based)", "Scope 2 (location-based)", "Scope 3",
                  "Water withdrawal", "Water consumption", "Waste generated", "Waste diverted",
                  "Waste diversion rate", "Energy consumption", "Renewable electricity"):
        m = metrics.get(label)
        if m is None:
            print(f"  {label + ':':<26} not found")
        else:
            print(f"  {label + ':':<26} {m.value:>14,.0f} {m.unit:<8} {m.year or ''} p.{m.page}")


if __name__ == "__main__":
    main()
