"""
gsc.py — your REAL Google data from Google Search Console (free).

Search Console shows, for a site you own: which searches showed your site,
how many people clicked, and your average position in Google. This is the
free way to "track rankings" for your own site.

One-time setup (see SETUP steps in the README / chat):
  1. Put the OAuth client file from Google Cloud at  data/gsc_client_secret.json
  2. Run:  .venv/bin/python gsc.py login     (a browser opens; YOU sign in to Google)
     -> saves data/gsc_token.json, so you won't need to sign in again.
  3. Check:  .venv/bin/python gsc.py sites

Access is READ-ONLY (webmasters.readonly scope): this code can never change
anything in your Search Console.
"""

import datetime as dt
import sys
from pathlib import Path
from urllib.parse import urlparse

DATA = Path(__file__).parent / "data"
CLIENT_SECRET = DATA / "gsc_client_secret.json"
TOKEN = DATA / "gsc_token.json"
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
DATA_DELAY_DAYS = 3  # Search Console data arrives ~2-3 days late


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------
def status() -> dict:
    return {"client_secret_found": CLIENT_SECRET.exists(), "logged_in": TOKEN.exists()}


def login() -> None:
    """Opens the browser so you can sign in with Google; saves a token file."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    if not CLIENT_SECRET.exists():
        sys.exit(f"Missing {CLIENT_SECRET}. Download the OAuth client JSON from Google Cloud first.")
    creds = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES).run_local_server(port=0)
    DATA.mkdir(exist_ok=True)
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)  # only your user can read it
    print("Signed in. Token saved to", TOKEN)


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    if not TOKEN.exists():
        raise RuntimeError("Not signed in to Search Console. Run: .venv/bin/python gsc.py login")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())  # access tokens last 1 hour; refresh silently
        TOKEN.write_text(creds.to_json())
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def list_sites() -> list[str]:
    sites = _service().sites().list().execute().get("siteEntry", [])
    return [s["siteUrl"] for s in sites if s.get("permissionLevel") != "siteUnverifiedUser"]


def _match_site(site: str) -> str:
    """Accept 'mysite.com' or 'https://mysite.com' and find the matching property."""
    available = list_sites()
    if site in available:
        return site
    host = (urlparse(site).netloc or site).removeprefix("www.").strip("/")
    for s in available:  # domain property (sc-domain:) or URL-prefix property
        if s == f"sc-domain:{host}" or urlparse(s).netloc.removeprefix("www.") == host:
            return s
    raise RuntimeError(f"No Search Console property for '{site}'. You have: {available or 'none'}")


# ---------------------------------------------------------------------------
# Fetch + analyse
# ---------------------------------------------------------------------------
def _query(svc, site, start, end, dimension, limit):
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(),
            "dimensions": [dimension], "rowLimit": limit}
    rows = svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
    return [{"key": r["keys"][0], "clicks": r["clicks"], "impressions": r["impressions"],
             "ctr": r["ctr"], "position": r["position"]} for r in rows]


def _site_totals(svc, site, start, end) -> dict:
    """Whole-site totals (no dimension). Includes rare searches Google hides per-query for privacy."""
    body = {"startDate": start.isoformat(), "endDate": end.isoformat()}
    rows = svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
    if not rows:
        return {"clicks": 0, "impressions": 0, "ctr_percent": 0, "avg_position": 0}
    r = rows[0]
    return {"clicks": r["clicks"], "impressions": r["impressions"],
            "ctr_percent": round(100 * r["ctr"], 2), "avg_position": round(r["position"], 1)}


def analyze(q_now, q_prev, p_now, p_prev) -> dict:
    """Pure analysis of query/page rows for two periods (no network - easy to test)."""
    def totals(rows):
        clicks = sum(r["clicks"] for r in rows)
        impr = sum(r["impressions"] for r in rows)
        pos = sum(r["position"] * r["impressions"] for r in rows) / impr if impr else 0
        return {"clicks": clicks, "impressions": impr,
                "ctr_percent": round(100 * clicks / impr, 2) if impr else 0,
                "avg_position": round(pos, 1)}

    def slim(r):
        return {"query": r["key"], "clicks": r["clicks"], "impressions": r["impressions"],
                "ctr_percent": round(100 * r["ctr"], 1), "position": round(r["position"], 1)}

    prev_clicks = {r["key"]: r["clicks"] for r in q_prev}
    prev_page_clicks = {r["key"]: r["clicks"] for r in p_prev}
    imp_floor = max(10, sorted((r["impressions"] for r in q_now), reverse=True)[min(49, len(q_now) - 1)]
                    if q_now else 10)  # roughly: the top-50 queries by impressions

    return {
        "totals_this_period": totals(q_now),
        "totals_previous_period": totals(q_prev),
        "top_queries_by_clicks": [slim(r) for r in sorted(q_now, key=lambda r: -r["clicks"])[:15]],
        # Page 2 of Google (positions 8-20) with real impressions: small improvements = big gains
        "striking_distance": [slim(r) for r in sorted(
            (r for r in q_now if 8 <= r["position"] <= 20 and r["impressions"] >= imp_floor),
            key=lambda r: -r["impressions"])[:15]],
        # Seen often but rarely clicked while ranking on page 1: rewrite title / meta description
        "low_ctr_on_page_one": [slim(r) for r in sorted(
            (r for r in q_now if r["position"] <= 7 and r["impressions"] >= imp_floor and r["ctr"] < 0.02),
            key=lambda r: -r["impressions"])[:10]],
        "queries_losing_clicks": [x for x in sorted(
            ({"query": k, "clicks_before": c, "clicks_now": next((r["clicks"] for r in q_now if r["key"] == k), 0)}
             for k, c in prev_clicks.items()),
            key=lambda x: x["clicks_now"] - x["clicks_before"]) if x["clicks_now"] < x["clicks_before"]][:10],
        "top_pages": [{"page": r["key"], "clicks": r["clicks"], "impressions": r["impressions"],
                       "position": round(r["position"], 1),
                       "clicks_change": r["clicks"] - prev_page_clicks.get(r["key"], 0)}
                      for r in sorted(p_now, key=lambda r: -r["clicks"])[:15]],
    }


def search_console(site: str, days: int = 28) -> dict:
    """Last `days` days of Google Search data for a site you own, vs the period before."""
    days = max(7, min(int(days), 90))
    svc, prop = _service(), _match_site(site)
    end = dt.date.today() - dt.timedelta(days=DATA_DELAY_DAYS)
    start = end - dt.timedelta(days=days - 1)
    prev_end, prev_start = start - dt.timedelta(days=1), start - dt.timedelta(days=days)
    result = analyze(
        _query(svc, prop, start, end, "query", 1000), _query(svc, prop, prev_start, prev_end, "query", 1000),
        _query(svc, prop, start, end, "page", 200), _query(svc, prop, prev_start, prev_end, "page", 200))
    result["totals_this_period"] = _site_totals(svc, prop, start, end)
    result["totals_previous_period"] = _site_totals(svc, prop, prev_start, prev_end)
    result.update(property=prop, period=f"{start} to {end}", previous_period=f"{prev_start} to {prev_end}",
                  note="Real Google data. Positions are averages; data is ~3 days behind.")
    return result


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "login":
        login()
    elif cmd == "sites":
        print("\n".join(list_sites()) or "No verified sites on this Google account.")
    else:
        print(status())
