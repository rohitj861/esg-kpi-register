# ESG KPI Register

Builds a company KPI register from primary sources: **financial figures from SEC
filings, environmental figures from sustainability reports**, with every value
traced back to the page it was read from.

Output goes to the console, CSV, JSON, a standalone HTML page, and a Supabase
database.

```
python esg_register.py GOOGL AAPL AMZN NVDA --to-supabase --html register.html
```

---

## Why every value carries a page number

The point of this tool is not to produce numbers — it is to produce numbers you
can check. A figure in a compliance workbook is worth very little if nobody can
say which document and which page it came from, so `page`, `source_document`,
and `source_url` travel with every value all the way into the database.

The same principle drives the extraction rules. Where a company does not publish
a metric, the cell is left **empty** rather than filled with the nearest similar
number. Apple's "waste diverted from landfill" is not "waste generated"; Apple's
supply-chain renewable electricity is ten times its own. A blank cell is
recoverable. A wrong number that looks plausible is not.

---

## Setup

Requires **Python 3.10+**. One third-party dependency:

```
python -m pip install pypdf
```

Everything else uses the standard library.

### Configuration

Two files, split by secrecy. Run `python config.py` at any time to see what
resolves (secrets are redacted).

| File | Contains |
|---|---|
| `.env` (this folder) | Supabase URL, project ref, publishable key, SEC user-agent |
| `%USERPROFILE%\.config\company-agent\.env` | `SUPABASE_SERVICE_KEY` only |

**The split is not cosmetic.** This project folder is inside OneDrive, so
anything written here is uploaded to Microsoft's cloud and retained in version
history. The Supabase service key is a full-admin credential that bypasses row
level security, so it lives outside OneDrive. `config.py` warns if it ever finds
a secret in the synced file.

Real environment variables override both files.

To get the service key: Supabase dashboard → project → Project Settings →
API Keys → reveal `service_role`, or create a secret key (`sb_secret_…`).

### SEC user-agent

The SEC rate-limits or blocks clients that do not identify themselves. Set
`SEC_USER_AGENT` to `Your Name your@email.com`.

---

## The scripts

| File | Does |
|---|---|
| `annual_report.py` | Resolve a company to its latest 10-K on EDGAR; extract revenue, net income, total assets, headcount |
| `sustainability_report.py` | Locate and download a sustainability report; extract emissions, water, waste, energy |
| `esg_register.py` | Run both, join them, write CSV / JSON / HTML, push to Supabase |
| `supabase_sync.py` | The database writer; run alone as a connection test |
| `esg_common.py` | Shared parsing: number reading, year-column alignment, unit conversion |
| `config.py` | Credential loading and inspection |

Each of the first four has its own CLI and can be used on its own.

---

## Usage

### Financial only

```bash
python annual_report.py "Alphabet"                  # find the filing
python annual_report.py NVDA --metrics              # extract the KPIs
python annual_report.py AAPL --text --dir reports   # save the filing as text
python annual_report.py MSFT --metrics --json       # machine-readable
```

Accepts a ticker or a company name — names are fuzzy-matched against EDGAR's
full company list, ignoring suffixes like "Inc." and "Corp".

### Environmental only

Sustainability reports have **no EDGAR equivalent** — no registry, no API, and
URLs that change every year. So there are four ways to point at one:

```bash
python sustainability_report.py GOOGL                          # built-in registry
python sustainability_report.py ACME --url https://…/report.pdf
python sustainability_report.py ACME --file ./downloaded.pdf
python sustainability_report.py ACME --page https://acme.com/sustainability
```

`--page` scrapes a landing page for candidate report PDFs and picks the
best-looking one.

The built-in registry covers **GOOGL, GOOG, AAPL, AMZN, NVDA**.

#### The registry heals itself

Publishers move these URLs every year, so the registry is a hint, not a fact.
Before each download, `resolve_registry_entry()` works through three steps:

1. **Look for a newer edition** by bumping the year in the URL and probing it —
   `google-2025-…` becomes `google-2026-…` with no code change.
2. **Verify the recorded URL** still returns a PDF.
3. **Recover from the landing page** if it does not. Candidates are ranked by
   how closely their filename resembles the dead one, with digits flattened, so
   `2026-amazon-sustainability-report.pdf` wins over `2025-aws-summary.pdf`
   sitting on the same page.

Only if all three fail does it ask you to supply `--url`. When healing happens,
a note is printed to stderr so a silent substitution never goes unnoticed.

Audit every entry at once:

```bash
python sustainability_report.py --check-registry
```

This is worth running periodically — it is how Google's 2026 report was found,
which had moved host as well as year (`gstatic.com` → `sustainability.google`),
a change no amount of year-bumping alone would have caught.

### The full register

```bash
python esg_register.py GOOGL AAPL AMZN NVDA \
    --csv register.csv --json register.json --html register.html --to-supabase

# a company not in the registry
python esg_register.py TSLA --sustainability-url https://…/impact-report.pdf --to-supabase
```

`--sustainability-*` options apply to one company at a time.

---

## Metrics

**Financial** (from the 10-K): Revenue, Net income, Total assets, Employees.

**Environmental** (from the sustainability report):

| Metric | Canonical unit |
|---|---|
| Scope 1, Scope 2 (market-based), Scope 2 (location-based), Scope 3 | tCO2e |
| Water withdrawal, Water consumption | m³ |
| Waste generated, Waste diverted | metric tons |
| Waste diversion rate | % |
| Energy consumption, Renewable electricity | MWh |

Units are normalised on ingest. Publishers disagree wildly — Google reports
water in million gallons, Apple waste in pounds, Amazon emissions in MMT CO₂e —
so conversions are applied and recorded in the value's `note` field
(`converted from Million gallons`).

Scope 2 is reported on both bases where a company publishes both. Most publish
only one.

---

## Database

Four tables plus two views in the Supabase project **Company Data**.

| Object | Holds |
|---|---|
| `companies` | Issuer, ticker, CIK |
| `metrics` | Controlled vocabulary of KPIs, so names cannot drift between runs |
| `source_documents` | The exact document, its fiscal year, URL, filing date |
| `kpi_values` | Value, unit, reporting year, **page**, extraction note |
| `esg_register` | Flattened view of everything, including superseded editions |
| `esg_register_current` | Most recent reading of each metric — **use this for reporting** |

Writes are upserts keyed on `(company, document, metric)`, so re-running an
extraction refreshes values in place. A **new report edition creates a new
`source_documents` row**, which means last year's figures survive rather than
being overwritten — Apple's 2025 and 2026 reports both sit in the database.
That is why `esg_register_current` exists: a plain query over `kpi_values` would
mix editions.

Row level security is on. The publishable key grants `SELECT` only; writes need
the service key.

---

## Reading the results

### The years do not line up, and they never will

Financial figures come from the 10-K, environmental figures from a separate
report published on a different cycle. Google's FY2025 revenue sits beside its
FY2024 emissions because that is the most recent emissions data Google has
published. **Every value carries its own `reporting_year`** — a row is not a
single period, and should not be presented as one.

### Check the `note` field

Values with a non-empty `note` need a human glance:

```sql
select ticker, metric, value, page, note
from esg_register_current where note <> '';
```

Notes you will see:

- `column alignment uncertain` — the row's cells did not map cleanly onto the
  year header, usually because the PDF mangled a figure. The value is a best
  effort; verify against the cited page.
- `converted from X` — units were rescaled.
- `N rows on this page matched this label` — the publisher used one label for
  two different reporting boundaries. Confirm which one you want.
- `no page footer detected; located in Item 8` — the filer prints no page
  numbers, so the citation falls back to the filing's Item.

---

## Known limits

**Sustainability report discovery is the weak point.** Microsoft publishes no
discoverable PDF (its reports hub links only pre-2021 device reports); Meta's
site exposes no PDF links; Tesla returns 403 to automated requests. These need a
manual `--url`.

**Some companies simply do not publish some metrics.** Amazon discloses water
and waste only as narrative percentages, never absolute figures — verified
across its 2024 and 2025 reports. No parser change fixes that.

**Google's renewable electricity** exists as an unlabelled *column* in a
renewable/non-renewable split rather than a year-keyed row, so it is not
extracted. Reading it would require column-header awareness.

**Scanned or image-only PDFs** will not work; extraction is text-based, with no OCR.

**US filers only.** The financial half depends on SEC EDGAR, which covers US
public companies plus foreign issuers listed on US exchanges. Private and
foreign-domestic-only companies are out of scope.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `missing configuration: SUPABASE_SERVICE_KEY` | Key not saved, or saved in the wrong `.env`. Run `python config.py`. |
| `HTTP 403` from SEC | `SEC_USER_AGENT` not set, or requests too fast. |
| `No sustainability report on file for 'XYZ'` | Not in the registry — pass `--url`, `--file`, or `--page`. |
| All metrics `not found` on a report | PDF may be image-only, or the publisher uses labels not yet covered. Check with `pypdf` whether the page yields text. |
| A figure looks wrong by a factor of ~10⁶ | A unit conversion went astray. Check the `note` field and the cited page. |
| A value cites a page far from the other metrics | Likely a narrative or endnote page misread as a table. Compare the cited page against the data-table page the other scopes came from. |

When adding a publisher, look at the actual row text first — label wording,
column order (Apple runs newest-first, Google oldest-first), fiscal-year notation
(NVIDIA writes `FY26`), and where the unit is declared. Those four things vary
more than anything else.
