// Serves the KPI register to the browser.
//
// The query runs here rather than in the page so the Supabase key stays in a
// Vercel environment variable. The publishable key is safe to expose by design,
// but keeping it server-side means the deployed site and the public repo hold
// no project identifiers at all.

export default async function handler(req, res) {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_PUBLISHABLE_KEY;

  if (!url || !key) {
    res.status(500).json({ error: "SUPABASE_URL / SUPABASE_PUBLISHABLE_KEY not configured" });
    return;
  }

  const endpoint =
    `${url}/rest/v1/esg_register_current` +
    `?select=company,ticker,metric,kind,value,unit,reporting_year,page,note,` +
    `source_document,source_url,display_order` +
    `&order=display_order.asc`;

  try {
    const upstream = await fetch(endpoint, {
      headers: { apikey: key, Authorization: `Bearer ${key}` },
    });

    if (!upstream.ok) {
      res.status(upstream.status).json({ error: `Supabase returned ${upstream.status}` });
      return;
    }

    const rows = await upstream.json();
    // Cache at the edge for a minute: the register changes only when the
    // ingest pipeline runs, so serving a slightly stale copy is fine and
    // keeps repeated views off the database.
    res.setHeader("Cache-Control", "public, s-maxage=60, stale-while-revalidate=300");
    res.status(200).json({ rows, fetchedAt: new Date().toISOString() });
  } catch (err) {
    res.status(502).json({ error: String(err) });
  }
}
