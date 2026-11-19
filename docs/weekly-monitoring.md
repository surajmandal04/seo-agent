# Weekly monitoring

Re-audits the pages and sites you "watch" **every Monday at 9:00 (local time)** and alerts you if a score drops by 5 points or more.

Checks call the measuring tools directly, with **no AI**, because scores come from a fixed formula. That makes them fast and free.

## Add a watch

Terminal:

```bash
.venv/bin/python monitor.py add https://mysite.com                    # one page (+ page speed if key set)
.venv/bin/python monitor.py add https://mysite.com --site --pages 30  # whole site
.venv/bin/python monitor.py list
.venv/bin/python monitor.py remove 3
```

Web UI: open a page or site in **Score history** and switch on **Watch weekly**.

## How the schedule runs

- The web server (`./run_web.sh`) starts a background scheduler.
- Every 10 minutes it runs any watch that hasn't been checked since the most recent Monday 9:00.
- So a check missed while the server was off **catches up** as soon as the server starts again.
- A lock ensures two checks never run at the same time, and one broken site can't stop the others.

Run checks by hand:

```bash
.venv/bin/python monitor.py run          # only the ones that are due
.venv/bin/python monitor.py run --all    # re-check everything now
```

## What each check does

| Watch | Tools run |
|---|---|
| Page | `audit_page`, plus `page_speed` (mobile) if `PAGESPEED_API_KEY` is set |
| Site | `crawl_site` with the saved page limit |

Results go to the [score history](score-history.md). Each run also re-indexes any [RAG](rag.md) site index that is more than 7 days old.

## Alerts

If the new score is **5+ points lower** than the previous one, you get a macOS notification such as "SEO score dropped: example.com: 92 → 84". The notification text is passed to `osascript` as arguments, never pasted into the script, so a page title can't break it. On Linux, checks still run and are logged, but there's no notification.

## API

```
GET    /api/watches
POST   /api/watches        {"mode": "page"|"site", "url": "...", "max_pages": 30}
DELETE /api/watches/{id}
```
