"""Find the latest annual report (10-K / 20-F / 40-F) for a company, via SEC EDGAR.

Usage:
    python annual_report.py "Apple"
    python annual_report.py NVDA --download
    python annual_report.py "toyota" --json

SEC asks that automated clients identify themselves. Set SEC_USER_AGENT to
"Your Name your@email.com" or edit the default below.
"""

import argparse
import difflib
import html.parser
import json
import os
import re
import sys
import urllib.error
import urllib.request

from esg_common import Metric, detect_scale, pick_latest, row_values, year_columns

USER_AGENT = os.environ.get("SEC_USER_AGENT", "annual-report-lookup contact@example.com")

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"

# Annual report form types, in order of preference.
ANNUAL_FORMS = ("10-K", "20-F", "40-F")


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _normalize(name):
    """Strip punctuation and corporate suffixes so 'Apple Inc.' ~ 'apple'."""
    name = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    name = re.sub(
        r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|"
        r"lp|sa|nv|ag|holdings?|group|the)\b",
        " ",
        name,
    )
    return " ".join(name.split())


def find_company(query):
    """Resolve a company name or ticker to {'cik', 'name', 'ticker'}."""
    companies = json.loads(_get(TICKERS_URL)).values()

    q_upper = query.strip().upper()
    for c in companies:  # exact ticker match wins
        if c["ticker"] == q_upper:
            return {"cik": str(c["cik_str"]).zfill(10), "name": c["title"], "ticker": c["ticker"]}

    q = _normalize(query)
    if not q:
        raise ValueError(f"Could not parse a company name from {query!r}")

    scored = []
    for c in companies:
        title = _normalize(c["title"])
        if title == q:
            score = 1.0
        elif title.startswith(q + " ") or q == title.split(" ")[0]:
            score = 0.95
        elif q in title:
            score = 0.9 - 0.001 * len(title)
        else:
            score = difflib.SequenceMatcher(None, q, title).ratio()
        scored.append((score, c))

    score, best = max(scored, key=lambda pair: pair[0])
    if score < 0.6:
        raise LookupError(f"No company on EDGAR matched {query!r} (best guess: {best['title']})")
    return {"cik": str(best["cik_str"]).zfill(10), "name": best["title"], "ticker": best["ticker"]}


def latest_annual_report(query, include_amendments=False):
    """Return metadata and a URL for the company's most recent annual report."""
    company = find_company(query)
    data = json.loads(_get(SUBMISSIONS_URL.format(cik=company["cik"])))
    recent = data["filings"]["recent"]

    rows = zip(
        recent["form"],
        recent["filingDate"],
        recent["reportDate"],
        recent["accessionNumber"],
        recent["primaryDocument"],
    )

    best = None
    for form, filed, period, accession, doc in rows:
        base = form[:-2] if form.endswith("/A") else form
        if base not in ANNUAL_FORMS:
            continue
        if form.endswith("/A") and not include_amendments:
            continue
        # `recent` is newest-first, so the first hit is the latest.
        best = (form, filed, period, accession, doc)
        break

    if best is None:
        raise LookupError(
            f"No {'/'.join(ANNUAL_FORMS)} filing found for {company['name']}. "
            "Foreign or private companies may not file with the SEC."
        )

    form, filed, period, accession, doc = best
    cik_short = str(int(company["cik"]))
    acc_nodash = accession.replace("-", "")
    return {
        "company": data.get("name", company["name"]),
        "ticker": company["ticker"],
        "cik": company["cik"],
        "form": form,
        "filing_date": filed,
        "period_of_report": period,
        "accession_number": accession,
        "url": ARCHIVE_URL.format(cik=cik_short, accession=acc_nodash, doc=doc),
        "filing_index": (
            f"https://www.sec.gov/Archives/edgar/data/{cik_short}/{acc_nodash}/"
            f"{accession}-index.htm"
        ),
    }


class _TextExtractor(html.parser.HTMLParser):
    """Collapse an EDGAR HTML/inline-XBRL filing down to readable text."""

    SKIP = {"script", "style", "head"}
    BREAK = {"p", "div", "br", "table", "h1", "h2", "h3", "h4", "li"}
    CELL = {"td", "th"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip_depth = 0
        self._cell_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "tr":
            self._cell_depth = 0
            self.parts.append("\n")
        elif tag in self.CELL:
            self._cell_depth += 1
            self.parts.append("\t")
        elif tag in self.BREAK:
            # Filers nest <p>/<div> inside table cells. Breaking there would put
            # every cell on its own line and destroy the label-to-value alignment
            # that the row-based extraction depends on.
            self.parts.append(" " if self._cell_depth else "\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "tr":
            self._cell_depth = 0
            self.parts.append("\n")
        elif tag in self.CELL:
            self._cell_depth = max(0, self._cell_depth - 1)
        elif tag in self.BREAK:
            self.parts.append(" " if self._cell_depth else "\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)

    def text(self):
        out = "".join(self.parts).replace("\xa0", " ")
        out = re.sub(r"[ \t]+", " ", out)
        out = re.sub(r" ?\n ?", "\n", out)
        return re.sub(r"\n{3,}", "\n\n", out).strip()


def to_text(raw):
    parser = _TextExtractor()
    parser.feed(raw.decode("utf-8", errors="replace"))
    text = parser.text()
    # Inline-XBRL filings open with a hidden block of concatenated context tags.
    # It carries no prose but is long enough to swallow regex matches.
    return "\n".join(
        line for line in text.split("\n") if not (len(line) > 400 and " " not in line)
    ).strip()


def detect_page_markers(lines):
    """Find the filing's page-footer lines without knowing the filer's format.

    Filers footer their pages differently — Alphabet prints a bare "49.", Apple
    prints "Apple Inc. | 2025 Form 10-K | 24". Rather than hardcode each, we look
    for the repeating line shape whose trailing number climbs through the
    document, which is what a page footer does and little else does.

    Returns [(line_index, page_number)] for the winning shape.
    """
    shapes = {}
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or len(line) > 80:
            continue
        trailing = re.search(r"(\d{1,4})\s*\.?\s*$", line)
        if not trailing:
            continue
        shape = re.sub(r"\d+", "#", line)
        shapes.setdefault(shape, []).append((i, int(trailing.group(1))))

    best, best_score = [], 0
    for occurrences in shapes.values():
        if len(occurrences) < 8:
            continue
        numbers = [n for _, n in occurrences]
        rising = sum(1 for a, b in zip(numbers, numbers[1:]) if b > a)
        # A real footer sequence climbs almost monotonically and starts low.
        if rising < 0.85 * (len(numbers) - 1) or min(numbers) > 5:
            continue
        score = len(occurrences) * (rising / max(1, len(numbers) - 1))
        if score > best_score:
            best, best_score = occurrences, score
    return best


def page_map(lines):
    """Map each line index to the filing's own printed page number.

    A line belongs to the page whose footer follows it.
    """
    pages = [None] * len(lines)
    markers = detect_page_markers(lines)
    if not markers:
        return pages
    lookup = dict(markers)
    current = None
    for i in range(len(lines) - 1, -1, -1):
        if i in lookup:
            current = lookup[i]
        pages[i] = current
    return pages


# Label -> row patterns, ordered by preference. Covers the common income-statement
# and balance-sheet wordings across US filers (Alphabet, Apple, Amazon, ...).
FINANCIAL_ROWS = {
    "Revenue": [
        r"^\s*Total\s+revenues?\s*(?:[\$\d]|$)",
        r"^\s*Total\s+net\s+sales\s*(?:[\$\d]|$)",
        r"^\s*Revenues?\s*(?:[\$\d]|$)",
        r"^\s*Net\s+sales\s*(?:[\$\d]|$)",
    ],
    "Net income": [
        r"^\s*Net\s+income\s*\(loss\)\s*(?:[\$\d]|$)",
        r"^\s*Net\s+income\s*(?:[\$\d]|$)",
    ],
    "Total assets": [
        r"^\s*Total\s+assets\s*(?:[\$\d]|$)",
    ],
}

# Headcount is prose, not a table row, and the wording varies by filer
# ("employees", "people", "full-time equivalent employees").
EMPLOYEE_PATTERNS = [
    r"(?:had|have|employed|employ)\s+(?:approximately\s+|about\s+|roughly\s+)?"
    r"([\d,]{4,})\s+(?:full[-\s]time\s+)?(?:equivalent\s+)?"
    r"(?:and\s+part[-\s]time\s+)?(?:employees|people|team\s+members)",
    r"(?:approximately|about|roughly|totaled)\s+([\d,]{4,})\s+"
    r"(?:full[-\s]time\s+)?(?:equivalent\s+)?(?:employees|people|team\s+members)",
    r"([\d,]{4,})\s+full[-\s]time\s+(?:equivalent\s+)?(?:employees|people)",
    r"(?:headcount|workforce|employees)\b[^.]{0,40}?\b(?:was|were|of|totaled|totalled)\s+"
    r"(?:approximately\s+|about\s+)?([\d,]{4,})",
]


def section_map(lines):
    """Map each line to the Item heading it falls under (e.g. 'Item 8').

    Used as the provenance fallback when a filer's page footers can't be
    detected, so a value is still traceable to a place in the document.
    """
    heading = re.compile(r"^\s*ITEM\s+(\d{1,2}[A-C]?)\s*[.:]", re.IGNORECASE)
    sections = [None] * len(lines)
    current = None
    for i, line in enumerate(lines):
        m = heading.match(line)
        if m:
            current = f"Item {m.group(1).upper()}"
        sections[i] = current
    return sections


def extract_financials(text):
    """Pull revenue, net income, total assets, and headcount with page provenance."""
    lines = text.split("\n")
    pages = page_map(lines)
    sections = section_map(lines)
    out = {}

    for label, patterns in FINANCIAL_ROWS.items():
        candidates = []
        for pattern in patterns:
            for i, line in enumerate(lines):
                if not re.search(pattern, line):
                    continue
                if re.search(r"(?i)per\s+share", line):
                    continue
                values = row_values(lines, i)
                if not values:
                    continue
                years = year_columns(lines, i, lookback=250)
                value, year = pick_latest(values, years)
                if value is None:
                    continue
                # An audited statement row has one value per year column. A summary
                # table in MD&A carries extra "$ Change" / "% Change" columns, so an
                # exact match is the signal that we are reading a real statement.
                aligned = bool(years) and len(values) == len(years)
                candidates.append((aligned, len(values), i, value, year))

        if not candidates:
            continue
        # Prefer aligned rows, then the widest year range (income statements show
        # three years where MD&A tables show two); ties go to the first occurrence.
        aligned, _, i, value, year = max(candidates, key=lambda c: (c[0], c[1], -c[2]))
        notes = [] if aligned else ["column alignment uncertain — verify against source"]
        if pages[i] is None and sections[i]:
            notes.append(f"no page footer detected; located in {sections[i]}")
        out[label] = Metric(
            name=label,
            value=value * detect_scale(lines, i, lookback=300),
            unit="USD",
            year=year,
            page=pages[i],
            source="Form 10-K",
            note="; ".join(notes),
        )

    for pattern in EMPLOYEE_PATTERNS:
        match = re.search(pattern, text)
        if not match:
            continue
        count = match.group(1).replace(",", "")
        if not count.isdigit():
            continue
        line_index = text[: match.start()].count("\n")
        context = text[max(0, match.start() - 160) : match.start()]
        year_match = re.findall(r"(?:19|20)\d{2}", context)
        out["Employees"] = Metric(
            name="Employees",
            value=float(count),
            unit="employees",
            year=int(year_match[-1]) if year_match else None,
            page=pages[line_index],
            source="Form 10-K",
            note="" if pages[line_index] else (
                f"no page footer detected; located in {sections[line_index]}"
                if sections[line_index] else "no page footer detected"
            ),
        )
        break

    return out


def download(report, directory=".", as_text=False):
    """Save the report's primary document; returns the local path.

    With as_text, the filing's HTML is stripped to plain text first.
    """
    raw = _get(report["url"])
    ext = ".txt" if as_text else (os.path.splitext(report["url"])[1] or ".htm")
    safe = re.sub(r"[^A-Za-z0-9]+", "_", report["company"]).strip("_")
    stem = f"{safe}_{report['form'].replace('/', '')}_{report['period_of_report']}"
    path = os.path.join(directory, stem + ext)

    if as_text:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(to_text(raw))
    else:
        with open(path, "wb") as fh:
            fh.write(raw)
    return path


def fetch_financials(query, include_amendments=False):
    """One-call pipeline: name/ticker -> latest 10-K -> page-traced financial metrics.

    Returns (report_metadata, {label: Metric}).
    """
    report = latest_annual_report(query, include_amendments=include_amendments)
    text = to_text(_get(report["url"]))
    return report, extract_financials(text)


def main():
    parser = argparse.ArgumentParser(description="Fetch a company's latest annual report from SEC EDGAR.")
    parser.add_argument("company", help="company name or ticker, e.g. 'Apple' or NVDA")
    parser.add_argument("--download", action="store_true", help="also save the document locally")
    parser.add_argument("--text", action="store_true", help="save as plain text instead of HTML (implies --download)")
    parser.add_argument("--metrics", action="store_true", help="extract revenue, net income, total assets, employees")
    parser.add_argument("--dir", default=".", help="download directory (default: current)")
    parser.add_argument("--amendments", action="store_true", help="allow amended filings (10-K/A)")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    args = parser.parse_args()

    try:
        report = latest_annual_report(args.company, include_amendments=args.amendments)
    except (LookupError, ValueError) as exc:
        sys.exit(f"error: {exc}")
    except urllib.error.HTTPError as exc:
        sys.exit(f"error: SEC returned HTTP {exc.code} — check your SEC_USER_AGENT and try again")
    except urllib.error.URLError as exc:
        sys.exit(f"error: network problem — {exc.reason}")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{report['company']} ({report['ticker']}) — CIK {report['cik']}")
        print(f"  form:     {report['form']}")
        print(f"  period:   {report['period_of_report']}")
        print(f"  filed:    {report['filing_date']}")
        print(f"  document: {report['url']}")
        print(f"  index:    {report['filing_index']}")

    if args.metrics:
        metrics = extract_financials(to_text(_get(report["url"])))
        if args.json:
            print(json.dumps([m.as_row() for m in metrics.values()], indent=2))
        else:
            print("  --- financial KPIs ---")
            for label in ("Revenue", "Net income", "Total assets", "Employees"):
                m = metrics.get(label)
                if m is None:
                    print(f"  {label + ':':<15} not found")
                else:
                    page = f"p.{m.page}" if m.page else "p.?"
                    print(f"  {label + ':':<15} {m.value:>20,.0f} {m.unit:<10} {m.year or ''} {page}")

    if args.download or args.text:
        print(f"  saved to: {download(report, args.dir, as_text=args.text)}")


if __name__ == "__main__":
    main()
