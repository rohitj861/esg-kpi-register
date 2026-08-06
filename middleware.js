// Password-gates the whole site, including /api.
//
// Vercel's own Deployment Protection only covers the production URL on a Pro
// plan, so on Hobby this middleware does the same job: every request must carry
// HTTP Basic credentials matching SITE_USER / SITE_PASSWORD, which are stored as
// Vercel environment variables.
//
// It fails closed. If SITE_PASSWORD is missing the site is locked rather than
// served openly, so a misconfiguration can never silently publish the register.

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

export default function middleware(request) {
  const expectedUser = process.env.SITE_USER || "admin";
  const expectedPassword = process.env.SITE_PASSWORD;

  if (!expectedPassword) {
    return new Response("Site is not configured for access.", {
      status: 503,
      headers: { "Content-Type": "text/plain" },
    });
  }

  const header = request.headers.get("authorization") || "";
  if (header.startsWith("Basic ")) {
    let decoded = "";
    try {
      decoded = atob(header.slice(6));
    } catch {
      decoded = "";
    }
    const separator = decoded.indexOf(":");
    const user = separator === -1 ? "" : decoded.slice(0, separator);
    const password = separator === -1 ? "" : decoded.slice(separator + 1);
    if (safeEqual(user, expectedUser) && safeEqual(password, expectedPassword)) {
      return; // authenticated — continue to the page or API route
    }
  }

  return new Response("Authentication required.", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="ESG KPI Register", charset="UTF-8"',
      "Content-Type": "text/plain",
    },
  });
}

// Compare in constant time so a wrong password cannot be recovered by timing
// how long the rejection takes.
function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}
