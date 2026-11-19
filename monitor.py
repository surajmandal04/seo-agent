"""
monitor.py — automatic weekly re-audits (Monday 9:00, local time).

A "watch" is a page or site you want re-checked. Each check runs the measuring
tools directly - no AI needed, because the score comes from a fixed formula -
so it's fast and free. Results land in the score history (history.py), and if a
score drops by 5+ points you get a Mac notification.

The web server (app.py) runs the scheduler in the background: every 10 minutes
it runs any watch that hasn't been checked since the last Monday 9:00. So a
check you missed while the server was off runs as soon as it starts again.

Terminal usage:
    .venv/bin/python monitor.py add https://mysite.com             # watch one page
    .venv/bin/python monitor.py add https://mysite.com --site --pages 30
    .venv/bin/python monitor.py list
    .venv/bin/python monitor.py remove 3
    .venv/bin/python monitor.py run          # run the checks that are due now
    .venv/bin/python monitor.py run --all    # re-check everything now
"""

import argparse
import datetime as dt
import os
import subprocess
import threading
import time

import history
import rag
from tools import run_tool

RUN_WEEKDAY, RUN_HOUR = 0, 9   # Monday (0) at 9:00
ALERT_DROP = 5                 # notify when a score falls by this many points or more
_lock = threading.Lock()       # never run two checks at once


def _db():
    conn = history._connect()  # same SQLite file as the score history
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watches (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mode        TEXT NOT NULL,          -- 'page' or 'site'
            url         TEXT NOT NULL,
            max_pages   INTEGER DEFAULT 30,
            created_at  REAL NOT NULL,
            last_run_at REAL,
            UNIQUE(mode, url)
        )""")
    return conn


def last_scheduled_time(now: dt.datetime | None = None) -> dt.datetime:
    """The most recent Monday 9:00 at or before now."""
    now = now or dt.datetime.now()
    slot = now.replace(hour=RUN_HOUR, minute=0, second=0, microsecond=0)
    slot -= dt.timedelta(days=(now.weekday() - RUN_WEEKDAY) % 7)
    return slot if slot <= now else slot - dt.timedelta(days=7)


def next_scheduled_time(now: dt.datetime | None = None) -> dt.datetime:
    return last_scheduled_time(now) + dt.timedelta(days=7)


def add_watch(mode: str, url: str, max_pages: int = 30) -> int:
    with _db() as conn:
        conn.execute("INSERT OR IGNORE INTO watches (mode, url, max_pages, created_at) VALUES (?,?,?,?)",
                     (mode, url, max_pages, time.time()))
        return conn.execute("SELECT id FROM watches WHERE mode=? AND url=?", (mode, url)).fetchone()["id"]


def remove_watch(watch_id: int) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM watches WHERE id=?", (watch_id,))


def list_watches() -> list[dict]:
    with _db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM watches ORDER BY created_at")]
    for r in rows:
        r["target"] = history.target_key(r["mode"], r["url"])  # matches the history panel
        r["next_run"] = next_scheduled_time().isoformat(timespec="minutes")
    return rows


def due_watches() -> list[dict]:
    cutoff = last_scheduled_time().timestamp()
    return [w for w in list_watches() if not w["last_run_at"] or w["last_run_at"] < cutoff]


def _notify(title: str, message: str) -> None:
    """Show a macOS notification (best effort)."""
    # Text is passed as arguments (argv), never pasted into the script, so quotes can't break it
    script = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
    subprocess.run(["osascript", "-e", script, title, message], capture_output=True)


def run_check(watch: dict, log=print) -> None:
    """Re-measure one watch. run_tool() saves the new score to the history."""
    mode, url = watch["mode"], watch["url"]
    target = history.target_key(mode, url)
    before = next((t["latest_score"] for t in history.list_targets()
                   if t["mode"] == mode and t["target"] == target), None)

    if mode == "site":
        _, err = run_tool("crawl_site", {"url": url, "max_pages": watch["max_pages"]})
    else:
        _, err = run_tool("audit_page", {"url": url})
        if os.environ.get("PAGESPEED_API_KEY"):
            run_tool("page_speed", {"url": url, "strategy": "mobile"})

    after = next((t["latest_score"] for t in history.list_targets()
                  if t["mode"] == mode and t["target"] == target), None)
    with _db() as conn:
        conn.execute("UPDATE watches SET last_run_at=? WHERE id=?", (time.time(), watch["id"]))
    log(f"[monitor] {mode} {url}: {before} -> {after}{' (error)' if err else ''}")
    if before is not None and after is not None and before - after >= ALERT_DROP:
        _notify("SEO score dropped", f"{target}: {before} → {after}")


def run_due(run_all: bool = False, log=print) -> int:
    """Run every due watch (or all of them). Returns how many ran."""
    if not _lock.acquire(blocking=False):
        return 0  # a run is already in progress
    try:
        todo = list_watches() if run_all else due_watches()
        for w in todo:
            try:
                run_check(w, log)
            except Exception as e:  # one broken site must not stop the others
                log(f"[monitor] {w['url']} failed: {e}")
        rag.refresh_stale(7, log)  # keep RAG indexes at most a week old
        return len(todo)
    finally:
        _lock.release()


def start_scheduler(interval_s: int = 600) -> None:
    """Background thread for the web server: check for due watches every 10 minutes."""
    def loop():
        while True:
            run_due()
            time.sleep(interval_s)
    threading.Thread(target=loop, daemon=True, name="seo-monitor").start()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Weekly SEO re-audits")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("url"); a.add_argument("--site", action="store_true")
    a.add_argument("--pages", type=int, default=30)
    sub.add_parser("list")
    r = sub.add_parser("remove"); r.add_argument("id", type=int)
    run = sub.add_parser("run"); run.add_argument("--all", action="store_true")
    args = p.parse_args()

    if args.cmd == "add":
        url = args.url if args.url.startswith("http") else "https://" + args.url
        wid = add_watch("site" if args.site else "page", url, args.pages)
        print(f"Watching #{wid}. Next scheduled check: {next_scheduled_time():%a %d %b %H:%M}")
    elif args.cmd == "list":
        for w in list_watches():
            last = dt.datetime.fromtimestamp(w["last_run_at"]).strftime("%d %b %H:%M") if w["last_run_at"] else "never"
            print(f"#{w['id']}  {w['mode']:4}  {w['url']}  last run: {last}")
        print(f"Next scheduled check: {next_scheduled_time():%a %d %b %H:%M}")
    elif args.cmd == "remove":
        remove_watch(args.id); print("Removed.")
    elif args.cmd == "run":
        print(f"Ran {run_due(run_all=args.all)} check(s).")
