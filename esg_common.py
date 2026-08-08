"""Shared helpers for the ESG KPI register: number parsing, year-column mapping, provenance."""

import dataclasses
import re

NUMBER = r"\(?-?\$?\s?\d{1,3}(?:,\d{3})+(?:\.\d+)?\)?|\(?-?\$?\s?\d+\.\d+\)?|\b\d{4,}\b"
YEAR = re.compile(r"\b(19|20)\d{2}\b")

# The register's metric vocabulary, in display order. Extraction may find more
# than this — renewable electricity in MWh is read only to derive a share — so
# the list lives here rather than in any one writer and every consumer filters
# through it.
FINANCIAL_METRICS = ["Revenue", "Net income", "Total assets", "Employees"]
ENVIRONMENTAL_METRICS = [
    "Scope 1", "Scope 2 (market-based)", "Scope 2 (location-based)", "Scope 3",
    "Energy consumption", "Water withdrawal", "Renewable energy share",
]
REGISTER_METRICS = FINANCIAL_METRICS + ENVIRONMENTAL_METRICS


def kind_of(label):
    """Which half of the register a metric belongs to."""
    return "financial" if label in FINANCIAL_METRICS else "environmental"

# Scale words that may appear in a statement header, e.g. "(in millions)".
SCALES = (
    (re.compile(r"(?i)\bin\s+billions\b"), 1_000_000_000),
    (re.compile(r"(?i)\bin\s+millions\b"), 1_000_000),
    (re.compile(r"(?i)\bin\s+thousands\b"), 1_000),
)


@dataclasses.dataclass
class Metric:
    """One extracted value plus everything needed to trace it back to the page."""

    name: str
    value: float | None
    unit: str
    year: int | None = None
    page: int | None = None
    source: str = ""
    note: str = ""

    def as_row(self):
        return {
            "metric": self.name,
            "value": self.value,
            "unit": self.unit,
            "year": self.year,
            "page": self.page,
            "source": self.source,
            "note": self.note,
        }


def parse_number(token):
    """'$ 1,234.5' -> 1234.5;  '(99)' -> -99.0;  returns None if unparseable."""
    t = token.strip()
    negative = t.startswith("(") and t.endswith(")")
    t = re.sub(r"[()$\s,]", "", t)
    if not t or not re.fullmatch(r"-?\d+(?:\.\d+)?", t):
        return None
    val = float(t)
    return -val if negative else val


def _tokens(line, pattern):
    """Numeric tokens on a line, skipping bare years.

    Reports quote years constantly in prose and footnotes — "our 2025 carbon
    footprint" — and an unpunctuated four-digit year is indistinguishable from a
    value unless we exclude the calendar range. Real figures in these tables are
    comma-grouped ("9,822"), so this costs nothing and stops an endnote page
    being read as a data table.
    """
    values = []
    for match in re.finditer(pattern, line):
        token = match.group().strip()
        value = parse_number(token)
        if value is None:
            continue
        if re.fullmatch(r"\d{4}", token) and 1900 <= value <= 2100:
            continue
        values.append(value)
    return values


def numbers_in(line):
    """All numeric tokens on a line, in order."""
    return _tokens(line, NUMBER)


# Some publishers head their columns with fiscal-year shorthand — NVIDIA writes
# "Metric FY26 FY25 FY24" — so a calendar-year-only reader finds no header at all
# and silently falls back to guessing the column.
FISCAL_YEAR = re.compile(r"\bFY\s?(\d{4}|\d{2})\b", re.IGNORECASE)


# Some tables label their columns only with a span in the caption — Amazon heads
# its footprint table "Amazon's 2019–2025 Carbon Footprint" and prints no year
# row at all. Read literally that is two columns for seven values.
YEAR_RANGE = re.compile(r"\b((?:19|20)\d{2})\s*[–—−-]\s*((?:19|20)\d{2})\b")


def years_in(line):
    """Years on a line, in order, whether written 2026, FY26, or as a span."""
    span = YEAR_RANGE.search(line)
    if span:
        start, end = int(span.group(1)), int(span.group(2))
        if 0 < end - start <= 12:
            return list(range(start, end + 1))

    found, spans = [], []
    for match in YEAR.finditer(line):
        year = int(match.group())
        if 1990 <= year <= 2100:
            found.append((match.start(), year))
            spans.append((match.start(), match.end()))

    for match in FISCAL_YEAR.finditer(line):
        digits = match.group(1)
        year = int(digits) if len(digits) == 4 else 2000 + int(digits)
        if not 1990 <= year <= 2100:
            continue
        # "FY2026" is caught by both readers; skip only the genuine overlap.
        # Proximity is not overlap — a header reads "FY26 FY25 FY24", and those
        # sit five characters apart.
        if any(match.start() < end and start < match.end() for start, end in spans):
            continue
        found.append((match.start(), year))

    return [year for _, year in sorted(found)]


def find_year_header(lines, index, lookback=25):
    """Walk backwards from `index` for a column header listing >= 2 years.

    Financial statements and ESG data tables both label columns by year; using
    that header is what lets us pick the latest column rather than assuming
    left-to-right ordering.
    """
    for i in range(index - 1, max(-1, index - lookback) - 1, -1):
        ys = years_in(lines[i])
        if len(ys) >= 2 and len(set(ys)) == len(ys):
            return ys
    return []


def pick_latest(values, years):
    """Align a row of values to year columns and return (value, year) for the newest year.

    Column order varies by publisher: Alphabet runs oldest-to-newest, Apple runs
    newest-first. When the counts line up we index by the year header. When they
    don't — a zero or a mangled figure can drop a cell — we fall back to the end
    of the row that the header says is newest, rather than assuming an order.
    """
    if not values:
        return None, None
    if not years:
        return values[-1], None
    if len(years) == len(values):
        newest = max(range(len(years)), key=lambda i: years[i])
        return values[newest], years[newest]
    newest_first = years[0] > years[-1]
    return (values[0] if newest_first else values[-1]), max(years)


# Bare one-to-three digit integers are excluded from NUMBER because filings are
# full of footnote markers and note references. Percentage rows are the one place
# they are genuinely the data, so those callers opt in explicitly.
RELAXED_NUMBER = NUMBER + r"|\b\d{1,3}\b"


def numbers_in_relaxed(line):
    return _tokens(line, RELAXED_NUMBER)


# Disclosure tables often end a row with the framework index it maps to, e.g.
# "Scope 2, market-based 568 0 40,555 GRI 305-2". Those codes are navigation,
# never data, and their digits would corrupt a relaxed numeric read.
REFERENCE_CODE = re.compile(
    r"(?i)\b(?:GRI|SASB|TCFD|UNGC|CDP|IFRS|ESRS|SDG)\s+[A-Za-z0-9][A-Za-z0-9\-\.]*"
)


def clean_row(line):
    return REFERENCE_CODE.sub(" ", line)


def looks_like_prose(line, max_words=14):
    """True for narrative sentences, which must not be mined as table rows.

    Endnote and commentary pages quote figures constantly. A data row is a short
    label followed by columns; a sentence is not.
    """
    return len(re.findall(r"[A-Za-z]{2,}", line)) > max_words


def row_values(lines, index, max_span=18, relaxed=False, expect=None):
    """The numbers belonging to the table row that starts at `index`.

    Most filers put a whole row on one line. Some (Microsoft) render every cell
    on its own line, so when the label line holds no numbers we walk forward
    taking numeric cells until the next line containing words — the next label.

    `expect` is the number of year columns. When the strict read comes up short —
    a genuine small value such as NVIDIA's 568 tCO2e is indistinguishable from a
    footnote marker — we retry permissively and accept that read only if it fills
    the columns exactly. A partial match stays rejected rather than guessed.
    """
    def below(extract):
        """Numeric cells on the lines under the label, up to the next label."""
        values = []
        for j in range(index + 1, min(len(lines), index + max_span)):
            line = lines[j].strip()
            if not line:
                continue
            if re.search(r"[A-Za-z]{2,}", line):
                break
            values.extend(extract(line))
        return values

    def gather(extract):
        return extract(clean_row(lines[index])) or below(extract)

    if relaxed:
        values = gather(numbers_in_relaxed)
        # A permissive read counts a footnote marker printed after the label.
        # Microsoft's share row is a split layout whose label line is
        # "renewable electricity 1", so the marker is the only thing found
        # inline and the six real cells sit below it. One number is not a row,
        # so when the inline read cannot fill the year columns, prefer whatever
        # the lines beneath actually hold.
        if expect and len(values) < expect:
            beneath = below(numbers_in_relaxed)
            if len(beneath) > len(values):
                return beneath
        return values

    strict = gather(numbers_in)
    # Only retry permissively for a line that already looks like a data row.
    # Without this, a sentence such as "scope 1 emissions represent less than 1%
    # of our total 2025 carbon footprint" reads as a three-column table.
    if expect and strict and len(strict) < expect:
        permissive = gather(numbers_in_relaxed)
        if len(permissive) >= expect:
            # The label can contribute its own digits — "Scope 2, market-based"
            # yields a stray 2 ahead of the data. Value columns are right-aligned
            # in a table row, so the trailing `expect` numbers are the data.
            return permissive[-expect:]
    return strict


def year_columns(lines, index, lookback=60, inline_lookback=80):
    """Year header for the row at `index`, whether inline or one year per line.

    The inline search stays narrow so a nearby table's header can't be borrowed.
    The split-layout search may reach much further, because a document that puts
    one cell per line pushes its header hundreds of lines above the row — it is
    still safe, since the scan stops at the first non-year line once it has
    started collecting.
    """
    inline = find_year_header(lines, index, min(lookback, inline_lookback))
    if inline:
        return inline
    years = []
    for j in range(index - 1, max(-1, index - lookback) - 1, -1):
        line = lines[j].strip()
        if not line:
            continue
        if re.fullmatch(r"(19|20)\d{2}", line):
            years.insert(0, int(line))
            continue
        if years:
            break
    return years


def detect_scale(lines, index, lookback=40):
    """Find '(in millions)' style scaling above a statement line. Defaults to 1."""
    for i in range(index, max(-1, index - lookback) - 1, -1):
        for pattern, factor in SCALES:
            if pattern.search(lines[i]):
                return factor
    return 1


def human(value, unit=""):
    """Compact display: 402836000000 -> '402.84B'."""
    if value is None:
        return "—"
    for cutoff, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= cutoff:
            return f"{value / cutoff:,.2f}{suffix}"
    return f"{value:,.0f}"


def full(value, unit=""):
    """Absolute figure with thousands separators, as shown in the KPI register.

    Shares are the exception: rounding a renewable energy share of 64.4% to a
    whole number throws away the only precision the figure has.
    """
    if value is None:
        return "—"
    return f"{value:,.1f}" if unit == "%" else f"{value:,.0f}"
