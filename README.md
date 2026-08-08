# ESG KPI Register

Builds a company KPI register from primary sources: **financial figures from SEC
filings, environmental figures from sustainability reports**, with every value
traced back to the page it was read from.

Output goes to the console, CSV, JSON, a standalone HTML page, and a Supabase
database.

```
python esg_register.py GOOGL MSFT AAPL AMZN NVDA --to-supabase --html register.html
```

---

## Why every value carries a page number

The point of this tool is not to produce numbers — it is to produce numbers you
can check. A figure in a compliance workbook is worth very little if nobody can
say which document and which page it came from, so `page`, `source_document`,
and `source_url` travel with every value all the way into the database.

The same principle drives the extraction rules. Where a company does not publish
a metric, the cell is left **empty** rather than filled with the nearest similar
number. Apple's supply-chain renewable electricity is ten times its own;
Microsoft's non-renewable fuel is not its energy consumption. A blank cell is
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
| `sustainability_report.py` | Locate and download a sustainability report; extract emissions, energy, water, renewable share |
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

The built-in registry covers **GOOGL, GOOG, MSFT, AAPL, AMZN, NVDA**.

Microsoft is the one entry that does not point at the headline report. It
publishes two documents — a narrative *Environmental Sustainability Report* and
an *Environmental Data Fact Sheet* — and only the fact sheet carries extractable
tables; the narrative PDF yields no KPI rows at all. Its landing page builds the
download links in JavaScript, so step 3 below has nothing to scrape and the
recorded URL plus the year-bump are the only routes to a new edition.

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
python esg_register.py GOOGL MSFT AAPL AMZN NVDA \
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
| Energy consumption | MWh |
| Water withdrawal | m³ |
| Renewable energy share | % |

Units are normalised on ingest. Publishers disagree wildly — Google reports
water in million gallons, NVIDIA emissions on a fiscal year, Amazon emissions in
MMT CO₂e — so conversions are applied and recorded in the value's `note` field
(`converted from Million gallons`).

The register publishes these seven and nothing else. Extraction still finds more
— renewable electricity in MWh is read only to derive a share — but
`esg_common.REGISTER_METRICS` is the single list every writer filters through,
so nothing reaches the CSV, the JSON, the database or the page without being on
it.

Scope 2 appears on **both bases**. Market-based reflects the electricity a
company contracted for, location-based what the grid it drew from actually
emitted, and the gap between them is the whole argument about corporate
renewable procurement — Microsoft's FY25 figures are 2.7 MtCO2e market-based
against 12.0 MtCO2e location-based. Most publishers report only one.

### Renewable energy share

Taken as a percentage the company itself states — "Renewable electricity
percentage", "Electricity procured from renewable sources %". Where a report
prints only megawatt-hours, the share falls back to renewable electricity ÷
energy consumption **for the same reporting year**, and the value carries a note
saying so. That fallback is a floor rather than an exact figure, because the
numerator is electricity while the denominator may include fuels; the note names
both source pages so it can be checked. Where neither is available the cell
stays empty — Amazon states its renewable share in prose and not in a data
table, so Amazon has no value here.

---

## Database

Four tables plus two views in the Supabase project **Company Data**.

| Object | Holds |
|---|---|
| `companies` | Issuer, ticker, CIK |
| `metrics` | Controlled vocabulary of KPIs, so names cannot drift between runs |
| `metrics.active` | False for a metric the register has retired. Both views filter on it |
| `source_documents` | The exact document, its fiscal year, URL, filing date |
| `kpi_values` | Value, unit, reporting year, **page**, extraction note |
| `esg_register` | Flattened view of everything, including superseded editions |
| `esg_register_current` | Most recent reading of each metric — **use this for reporting** |

Writes are upserts keyed on `(company, document, metric)`, so re-running an
extraction refreshes values in place. A **new report edition creates a new
`source_documents` row**, which means last year's figures survive rather than
being overwritten — Apple's 2025 and 2026 reports both sit in the database.
Retiring a metric is a flag rather than a delete, for the same reason: a
`kpi_values` row is traced to a page of one report edition and cannot be
recreated without re-running the extraction, so `active = false` hides it from
both views and setting the flag back brings the history with it.

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

**Sustainability report discovery is the weak point.** Microsoft's reports hub
builds its links in JavaScript and scrapes to nothing, so its registry entry
carries a hard-coded fact-sheet URL; Meta's site exposes no PDF links; Tesla
returns 403 to automated requests. These need a manual `--url`.

**Apple's CDN rate-limits repeated downloads.** Its report is 23 MB, and after
several fetches in a session `apple.com` starts returning 403 to both HEAD and
GET — resolution then fails and the run reports it rather than inventing a
figure. It clears on its own; re-run later, or pass `--file` against the copy in
`reports/`. Do not work around it by disguising the user agent.

**Some companies simply do not publish some metrics.** Amazon discloses water
and its renewable share only as narrative prose, never as a table row — verified
across its 2024 and 2025 reports. No parser change fixes that.

**Google's renewable electricity in MWh** exists as an unlabelled *column* in a
renewable/non-renewable split rather than a year-keyed row, so it is not
extracted. Its renewable *share* is read from a different row and is unaffected.

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
