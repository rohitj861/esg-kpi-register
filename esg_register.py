"""Build an ESG KPI register: financial KPIs from the 10-K, emissions from the
sustainability report, every figure traced back to a page in its source document.

    python esg_register.py GOOGL AAPL AMZN --html register.html --csv register.csv

Financial and environmental figures come from two different documents published
on two different cycles, so each value carries its own reporting year. Compare
across companies with that in mind — the years will not always line up.
"""

import argparse
import csv
import html
import json
import os
import sys

import annual_report
import sustainability_report
from esg_common import full

FINANCIAL_METRICS = ["Revenue", "Net income", "Total assets", "Employees"]
ENVIRONMENTAL_METRICS = [
    "Scope 1", "Scope 2 (market-based)", "Scope 3",
    "Water withdrawal", "Water consumption",
    "Waste generated", "Waste diverted", "Waste diversion rate",
    "Energy consumption", "Renewable electricity",
]


def build_row(company, sustainability_url=None, sustainability_file=None,
              sustainability_page=None, cache_dir="reports"):
    """Collect every KPI for one company. Never raises — failures land in `errors`."""
    row = {
        "query": company,
        "company": company,
        "ticker": "",
        "cik": "",
        "metrics": {},
        "sources": {},
        "errors": [],
    }

    try:
        report, financials = annual_report.fetch_financials(company)
        row["company"] = report["company"]
        row["ticker"] = report["ticker"]
        row["cik"] = report["cik"]
        row["sources"]["financial"] = {
            "title": f"Form {report['form']} FY{report['period_of_report'][:4]}",
            "url": report["url"],
            "doc_type": report["form"],
            "fiscal_year": int(report["period_of_report"][:4]),
            "period_of_report": report["period_of_report"],
            "filed_date": report["filing_date"],
            "accession_number": report["accession_number"],
        }
        row["metrics"].update(financials)
    except Exception as exc:
        row["errors"].append(f"10-K: {exc}")

    ticker = row["ticker"] or company
    try:
        label, source_url, emissions = sustainability_report.fetch_emissions(
            ticker,
            url=sustainability_url,
            file=sustainability_file,
            page=sustainability_page,
            cache_dir=cache_dir,
        )
        years = [m.year for m in emissions.values() if m.year]
        row["sources"]["environmental"] = {
            "title": label,
            "url": source_url,
            "doc_type": "sustainability",
            "fiscal_year": max(years) if years else None,
        }
        row["metrics"].update(emissions)
    except Exception as exc:
        row["errors"].append(f"sustainability report: {exc}")

    return row


def page_link(metric, source):
    """Deep link to the page a value came from, when the source allows it."""
    if not source or not source.get("url") or metric.page is None:
        return None
    url = source["url"]
    return f"{url}#page={metric.page}" if url.lower().endswith(".pdf") else url


def print_console(rows):
    for row in rows:
        title = f"{row['company']}" + (f" ({row['ticker']})" if row["ticker"] else "")
        print(f"\n{title}")
        for key in ("financial", "environmental"):
            src = row["sources"].get(key)
            if src:
                print(f"  {key:<14} {src['title']}")
        for label in FINANCIAL_METRICS + ENVIRONMENTAL_METRICS:
            m = row["metrics"].get(label)
            if m is None:
                print(f"    {label + ':':<26} —")
                continue
            page = f"p.{m.page}" if m.page else ""
            flag = "  ⚠ " + m.note if m.note else ""
            print(f"    {label + ':':<26} {full(m.value):>18} {m.unit:<10} {m.year or '':<6} {page}{flag}")
        for err in row["errors"]:
            print(f"    ! {err}")


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["company", "ticker", "metric", "value", "unit", "reporting_year",
             "source_document", "page", "source_url", "note"]
        )
        for row in rows:
            for label in FINANCIAL_METRICS + ENVIRONMENTAL_METRICS:
                m = row["metrics"].get(label)
                if m is None:
                    continue
                key = "financial" if label in FINANCIAL_METRICS else "environmental"
                src = row["sources"].get(key, {})
                writer.writerow([
                    row["company"], row["ticker"], label,
                    int(m.value) if m.value is not None and float(m.value).is_integer() else m.value,
                    m.unit, m.year or "", src.get("title", ""), m.page or "",
                    src.get("url", ""), m.note,
                ])
    return path


def write_json(rows, path):
    payload = []
    for row in rows:
        payload.append({
            "company": row["company"],
            "ticker": row["ticker"],
            "sources": row["sources"],
            "metrics": {label: m.as_row() for label, m in row["metrics"].items()},
            "errors": row["errors"],
        })
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


CSS = """
:root { --ink:#111; --muted:#6b7280; --line:#e5e7eb; --bg:#fff; --accent:#1d4ed8; }
@media (prefers-color-scheme: dark) {
  :root { --ink:#e8e8ea; --muted:#9aa1ab; --line:#2c2f36; --bg:#0f1115; --accent:#7aa2ff; }
}
* { box-sizing: border-box; }
body { margin:0; padding:2.5rem 1.5rem; background:var(--bg); color:var(--ink);
       font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width:1180px; margin:0 auto; }
.eyebrow { font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); }
h1 { font-size:2rem; margin:.4rem 0 .5rem; letter-spacing:-.02em; }
.lede { color:var(--muted); margin:0 0 1.75rem; max-width:60ch; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; min-width:900px; }
th, td { text-align:left; padding:.7rem .8rem; border-bottom:1px solid var(--line);
         vertical-align:top; }
thead .group th { font-size:11px; letter-spacing:.12em; text-transform:uppercase;
                  color:var(--muted); border-bottom:none; padding-bottom:0; }
thead .cols th { font-size:12px; font-weight:600; color:var(--muted); }
td.num { font-variant-numeric:tabular-nums; white-space:nowrap; }
.val { font-weight:600; }
.unit { color:var(--muted); font-size:12px; }
.prov { color:var(--muted); font-size:11px; margin-top:.15rem; }
.prov a { color:var(--accent); text-decoration:none; }
.prov a:hover { text-decoration:underline; }
.co { font-weight:600; }
.co small { display:block; font-weight:400; color:var(--muted); font-size:11px; margin-top:.2rem; }
.miss { color:var(--muted); }
.flag { color:#b45309; font-size:11px; margin-top:.15rem; }
@media (prefers-color-scheme: dark) { .flag { color:#fbbf24; } }
.note { margin-top:1.5rem; color:var(--muted); font-size:12.5px; max-width:75ch; }
.err { color:#b91c1c; font-size:12px; }
@media (prefers-color-scheme: dark) { .err { color:#f87171; } }
"""


def write_html(rows, path):
    def cell(row, label):
        m = row["metrics"].get(label)
        if m is None:
            return '<td class="num miss">—</td>'
        key = "financial" if label in FINANCIAL_METRICS else "environmental"
        src = row["sources"].get(key)
        link = page_link(m, src)
        page = f"p.{m.page}" if m.page else "source"
        prov = f'<a href="{html.escape(link)}" target="_blank" rel="noopener">{page} ↗</a>' if link else page
        year = m.year or "—"
        flag = f'<div class="flag">⚠ {html.escape(m.note)}</div>' if m.note else ""
        return (
            f'<td class="num"><span class="val">{full(m.value)}</span>'
            f'<div class="unit">{html.escape(m.unit)}</div>'
            f'<div class="prov">{year} · {prov}</div>{flag}</td>'
        )

    body = []
    for row in rows:
        name = html.escape(row["company"])
        ticker = html.escape(row["ticker"])
        errs = "".join(f'<div class="err">{html.escape(e)}</div>' for e in row["errors"])
        cells = "".join(cell(row, l) for l in FINANCIAL_METRICS + ENVIRONMENTAL_METRICS)
        body.append(
            f'<tr><td class="co">{name}<small>{ticker}</small>{errs}</td>{cells}</tr>'
        )

    fin_headers = "".join(f"<th>{html.escape(l)}</th>" for l in FINANCIAL_METRICS)
    env_headers = "".join(f"<th>{html.escape(l)}</th>" for l in ENVIRONMENTAL_METRICS)

    doc = f"""<title>ESG KPI Register</title>
<style>{CSS}</style>
<div class="wrap">
  <div class="eyebrow">Primary-source data</div>
  <h1>Sustainability KPIs, traced to the filing.</h1>
  <p class="lede">Every figure is extracted from the company's own annual and
  sustainability reports. {len(rows)} companies, {len(FINANCIAL_METRICS) + len(ENVIRONMENTAL_METRICS)} metrics.
  Each value links to its source page.</p>
  <div class="scroll">
  <table>
    <thead>
      <tr class="group"><th></th><th colspan="{len(FINANCIAL_METRICS)}">Financial</th>
          <th colspan="{len(ENVIRONMENTAL_METRICS)}">Environmental</th></tr>
      <tr class="cols"><th>Company</th>{fin_headers}{env_headers}</tr>
    </thead>
    <tbody>{"".join(body)}</tbody>
  </table>
  </div>
  <p class="note"><strong>Reading the years.</strong> Financial figures come from the
  Form 10-K and environmental figures from a separate sustainability report. The two
  are published on different cycles, so a company's emissions year usually trails its
  financial year. Each cell states the year the figure belongs to; do not read a row
  as a single period. Scope 2 is market-based where the company reports both.</p>
</div>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return path


def main():
    parser = argparse.ArgumentParser(description="Build an ESG KPI register from primary sources.")
    parser.add_argument("companies", nargs="+", help="names or tickers")
    parser.add_argument("--sustainability-url", help="PDF URL (single company only)")
    parser.add_argument("--sustainability-file", help="local PDF path (single company only)")
    parser.add_argument("--sustainability-page", help="landing page to scrape (single company only)")
    parser.add_argument("--dir", default="reports", help="cache directory for downloaded PDFs")
    parser.add_argument("--csv", help="write a CSV register to this path")
    parser.add_argument("--json", help="write a JSON register to this path")
    parser.add_argument("--html", help="write an HTML register to this path")
    parser.add_argument("--to-supabase", action="store_true",
                        help="upsert the register into the Supabase project")
    args = parser.parse_args()

    overrides = (args.sustainability_url, args.sustainability_file, args.sustainability_page)
    if any(overrides) and len(args.companies) > 1:
        sys.exit("error: --sustainability-* options apply to a single company at a time")

    rows = []
    for company in args.companies:
        print(f"… {company}", file=sys.stderr)
        rows.append(build_row(
            company,
            sustainability_url=args.sustainability_url,
            sustainability_file=args.sustainability_file,
            sustainability_page=args.sustainability_page,
            cache_dir=args.dir,
        ))

    print_console(rows)
    for flag, writer in ((args.csv, write_csv), (args.json, write_json), (args.html, write_html)):
        if flag:
            print(f"\nwrote {writer(rows, flag)}")

    if args.to_supabase:
        import supabase_sync

        results, failures = supabase_sync.push(rows)
        print("\nSupabase:")
        for r in results:
            print(f"  {r['ticker']:<6} {r['documents']} document(s), {r['values']} value(s) upserted")
        for company, error in failures:
            print(f"  ! {company}: {error}")


if __name__ == "__main__":
    main()
