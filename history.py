"""
history.py — remembers every audit so you can see scores change over time.

Uses SQLite: a whole database in one file (data/history.db), built into Python,
no server to install. Every time audit_page or crawl_site runs (from either
engine), tools.run_tool() calls record() with the score and key numbers.

See your history in the terminal:
    .venv/bin/python history.py
"""

import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

# SEO_AGENT_DB lets tests use a throwaway database instead of your real one
DB_PATH = Path(os.environ.get("SEO_AGENT_DB", Path(__file__).parent / "data" / "history.db"))


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL    NOT NULL,   -- unix time
            mode       TEXT    NOT NULL,   -- 'page', 'site' or 'speed'
            target     TEXT    NOT NULL,   -- page URL, or site host
            score      INTEGER NOT NULL,   -- 0-100 from the fixed formula
            metrics    TEXT    NOT NULL    -- JSON with the key numbers
        )""")
    return conn


def target_key(mode: str, url: str) -> str:
    """Pages are tracked by URL (without trailing slash); sites by their host name."""
    if mode == "site":
        return urlparse(url).netloc.removeprefix("www.")
    return url.split("#")[0].rstrip("/")


def record(mode: str, url: str, score: int, metrics: dict) -> None:
    with _connect() as conn:
        conn.execute("INSERT INTO runs (created_at, mode, target, score, metrics) VALUES (?,?,?,?,?)",
                     (time.time(), mode, target_key(mode, url), score, json.dumps(metrics)))


def record_tool_result(name: str, result: dict) -> None:
    """Called after a tool runs. Saves a snapshot for audit_page and crawl_site."""
    if name == "audit_page":
        metrics = {k: result.get(k) for k in (
            "title_length", "meta_description_length", "h1_count", "word_count",
            "images_missing_alt_count", "internal_link_count")}
        metrics["deductions"] = result.get("score_deductions", [])
        record("page", result["final_url"], result["health_score"], metrics)
    elif name == "page_speed":
        raw = result.get("_raw_ms", {})
        metrics = {
            "lcp_s": round(raw["lcp"] / 1000, 2) if raw.get("lcp") else None,
            "fcp_s": round(raw["fcp"] / 1000, 2) if raw.get("fcp") else None,
            "tbt_ms": round(raw["tbt"]) if raw.get("tbt") is not None else None,
            "cls": round(raw["cls"], 3) if raw.get("cls") is not None else None,
            "deductions": [f"{o['fix']} (saves ~{o['saves_ms']} ms)" for o in result["top_opportunities"][:5]],
        }
        url = result["url"] if result["strategy"] == "mobile" else f"{result['url']} (desktop)"
        record("speed", url, result["performance_score"], metrics)
    elif name == "crawl_site" and result.get("pages_crawled"):
        f = result["findings"]
        metrics = {
            "pages_crawled": result["pages_crawled"],
            "page_limit": result["page_limit"],
            "average_page_score": result["score_breakdown"]["average_page_score"],
            "broken_pages": len(f["broken_pages"]),
            "duplicate_titles": len(f["duplicate_titles"]),
            "duplicate_meta_descriptions": len(f["duplicate_meta_descriptions"]),
            "missing_meta_description": len(f["missing_meta_description"]),
            "no_h1": len(f["no_h1"]),
            "thin_pages": len(f["thin_content_under_300_words"]),
            "title_too_long": len(f["title_too_long_over_60"]),
            "images_missing_alt": sum(f["pages_with_images_missing_alt"].values()),
            "noindex_pages": len(f["noindex_pages"]),
            "deductions": result["score_breakdown"]["site_wide_deductions"],
        }
        record("site", result["start_url"], result["health_score"], metrics)


def list_targets() -> list[dict]:
    """Every tracked page/site with its latest score, previous score and run count."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT mode, target, COUNT(*) AS runs, MAX(created_at) AS last_run
            FROM runs GROUP BY mode, target ORDER BY last_run DESC""").fetchall()
        out = []
        for r in rows:
            scores = [s["score"] for s in conn.execute(
                "SELECT score FROM runs WHERE mode=? AND target=? ORDER BY created_at DESC LIMIT 2",
                (r["mode"], r["target"]))]
            out.append({**dict(r), "latest_score": scores[0],
                        "previous_score": scores[1] if len(scores) > 1 else None})
        return out


def get_runs(mode: str, target: str) -> list[dict]:
    """All runs for one page/site, oldest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, score, metrics FROM runs WHERE mode=? AND target=? ORDER BY created_at",
            (mode, target)).fetchall()
    return [{**dict(r), "metrics": json.loads(r["metrics"])} for r in rows]


if __name__ == "__main__":
    targets = list_targets()
    if not targets:
        print("No audits recorded yet.")
    for t in targets:
        change = "" if t["previous_score"] is None else f" ({t['latest_score'] - t['previous_score']:+d})"
        print(f"\n{t['mode']:4}  {t['target']}  latest {t['latest_score']}{change}  · {t['runs']} runs")
        for run in get_runs(t["mode"], t["target"]):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(run["created_at"]))
            print(f"      {when}  score {run['score']}")
