"""Push the ESG KPI register into Supabase.

Writes through the PostgREST API using the service key, so no database driver
is needed. Every write is an upsert keyed on the natural business key, which
means re-running an extraction refreshes values in place rather than piling up
duplicate rows — while a new report edition creates a new source_document and
therefore keeps last year's figures intact.

    python supabase_sync.py            # smoke-test the connection
"""

import json
import urllib.error
import urllib.request

import config
from esg_common import REGISTER_METRICS, kind_of


def _request(method, path, payload=None, prefer=None, params=""):
    cfg = config.require("SUPABASE_URL", "SUPABASE_SERVICE_KEY")
    url = f"{cfg['SUPABASE_URL']}/rest/v1/{path}{params}"
    headers = {
        "apikey": cfg["SUPABASE_SERVICE_KEY"],
        "Authorization": f"Bearer {cfg['SUPABASE_SERVICE_KEY']}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer

    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else []
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:600]
        raise RuntimeError(f"Supabase {method} {path} -> HTTP {exc.code}: {detail}") from None


def upsert(table, rows, on_conflict):
    """Insert or update `rows`, matching existing records on `on_conflict`."""
    if not rows:
        return []
    return _request(
        "POST",
        table,
        payload=rows,
        prefer="resolution=merge-duplicates,return=representation",
        params=f"?on_conflict={on_conflict}",
    )


def select(path, params=""):
    return _request("GET", path, params=params)


def push_row(row):
    """Write one company's register entry. Returns a short summary dict."""
    if not row.get("ticker"):
        raise ValueError(f"{row['company']}: no ticker resolved, cannot key the company record")

    company = upsert(
        "companies",
        [{"ticker": row["ticker"], "cik": row.get("cik") or None, "name": row["company"]}],
        on_conflict="ticker",
    )[0]

    documents = {}
    for kind, src in row["sources"].items():
        if not src.get("title"):
            continue
        record = {
            "company_id": company["id"],
            "doc_type": src.get("doc_type", "sustainability"),
            "title": src["title"],
            "url": src.get("url"),
            "fiscal_year": src.get("fiscal_year"),
            "period_of_report": src.get("period_of_report"),
            "filed_date": src.get("filed_date"),
            "accession_number": src.get("accession_number"),
        }
        documents[kind] = upsert(
            "source_documents", [record], on_conflict="company_id,doc_type,title"
        )[0]

    values = []
    for label, metric in row["metrics"].items():
        # Extraction finds more than the register publishes — renewable
        # electricity in megawatt-hours is read only to derive a share. Those are
        # working values, not KPIs, and writing them would put them straight
        # back on the page, which reads the database rather than this list.
        if label not in REGISTER_METRICS:
            continue
        document = documents.get(kind_of(label))
        if document is None or metric.value is None:
            continue
        values.append({
            "company_id": company["id"],
            "document_id": document["id"],
            "metric_code": label,
            "value": metric.value,
            "unit": metric.unit,
            "reporting_year": metric.year,
            "page": metric.page,
            "note": metric.note or "",
        })

    written = upsert(
        "kpi_values", values, on_conflict="company_id,document_id,metric_code"
    )
    return {
        "company": row["company"],
        "ticker": row["ticker"],
        "documents": len(documents),
        "values": len(written),
    }


def push(rows):
    """Write every company. Failures are collected, not raised, so one bad
    company cannot abandon the rest of the batch."""
    results, failures = [], []
    for row in rows:
        try:
            results.append(push_row(row))
        except Exception as exc:
            failures.append((row.get("company", "?"), str(exc)))
    return results, failures


def main():
    cfg = config.require("SUPABASE_URL", "SUPABASE_SERVICE_KEY")
    print(f"connecting to {cfg['SUPABASE_URL']} …")
    metrics = select("metrics", "?select=code,kind&order=display_order")
    print(f"ok — {len(metrics)} metrics defined:")
    for m in metrics:
        print(f"  {m['kind']:<14} {m['code']}")
    counts = {
        table: len(select(table, "?select=id"))
        for table in ("companies", "source_documents", "kpi_values")
    }
    print("\nrow counts:", ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
