"""
app.py — the web server.

Start it with:
    .venv/bin/uvicorn app:app --port 8000
then open http://localhost:8000

How it works:
  - The page sends POST /api/audit  -> we start the agent in a background thread
    and immediately return a job id (an audit can take a few minutes).
  - The page asks GET /api/jobs/<id> every 2 seconds -> progress steps, then the
    finished report as HTML.
  - Finished reports are saved in reports/ and listed under "Past reports".
"""

import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

import gsc
import history
import monitor
from export_pdf import report_to_pdf
from main import normalize_url, report_name, run_job
from tools import MARKETS

BASE = Path(__file__).parent
REPORTS = BASE / "reports"
REPORTS.mkdir(exist_ok=True)

# html=False: any raw HTML in the report is shown as text, never executed.
# (Report text comes from other people's websites, so we don't trust it.)
md = MarkdownIt("commonmark", {"html": False}).enable("table")

app = FastAPI(title="SEO Agent")
jobs: dict[str, dict] = {}  # job_id -> status, progress, report (kept in memory)


@app.on_event("startup")
def start_monitor():
    monitor.start_scheduler()  # weekly re-audits run in the background while the server is up


class WatchRequest(BaseModel):
    mode: str = Field(pattern="^(page|site)$")
    url: str = Field(min_length=4, max_length=500)
    max_pages: int = Field(default=30, ge=1, le=100)


@app.get("/api/gsc/status")
def gsc_status():
    """Is Search Console set up? (client file present, signed in)"""
    return gsc.status()


@app.get("/api/watches")
def get_watches():
    return {"watches": monitor.list_watches(),
            "next_run": monitor.next_scheduled_time().isoformat(timespec="minutes")}


@app.post("/api/watches")
def add_watch(req: WatchRequest):
    url = normalize_url(req.url.strip())
    if not _looks_like_url(url):
        raise HTTPException(400, "That doesn't look like a website address.")
    watch_id = monitor.add_watch(req.mode, url, req.max_pages)
    # First check right away (gives a starting point), in the background
    threading.Thread(target=monitor.run_due, daemon=True).start()
    return {"id": watch_id}


@app.delete("/api/watches/{watch_id}")
def delete_watch(watch_id: int):
    monitor.remove_watch(watch_id)
    return {"ok": True}


class AuditRequest(BaseModel):
    target: str = Field(min_length=2, max_length=500)  # URL, or keyword in keywords/article mode
    mode: str = Field(default="page", pattern="^(page|site|keywords|article|compare|gsc)$")
    competitors: list[str] = Field(default=[], max_length=3)        # compare mode
    keyword: str = Field(default="", max_length=80)                  # compare mode
    length: int = Field(default=1200, ge=300, le=3000)               # article mode
    tone: str = Field(default="friendly and expert", max_length=60)  # article mode
    max_pages: int = Field(default=30, ge=1, le=100)
    engine: str = Field(default="free", pattern="^(free|api)$")
    country: str = Field(default="us", pattern="^(" + "|".join(MARKETS) + ")$")
    site: str | None = Field(default=None, max_length=500)  # optional, keywords/article mode


def _looks_like_url(url: str) -> bool:
    host = urlparse(url).netloc
    return bool(host) and "." in host


def _run_job(job_id: str, req: AuditRequest, target: str, site: str | None, competitors: list[str]):
    job = jobs[job_id]
    step = lambda text: job["progress"].append(text)  # agent calls this for each tool run
    try:
        report = run_job(req.engine, req.mode, target, on_step=step,
                         max_pages=req.max_pages, country=req.country, site=site,
                         length=req.length, tone=req.tone,
                         competitors=competitors, keyword=req.keyword.strip())
        path = REPORTS / report_name(req.mode, target, req.country)
        path.write_text(report)
        job.update(status="done", html=md.render(report), saved_as=path.name)
    except Exception as e:  # show the problem on the page instead of hanging
        job.update(status="error", error=f"{type(e).__name__}: {e}")
    finally:
        job["finished"] = time.time()


@app.get("/")
def index():
    return FileResponse(BASE / "web" / "index.html")


@app.post("/api/audit")
def start_audit(req: AuditRequest):
    site = None
    if req.mode in ("keywords", "article"):
        target = " ".join(req.target.split())[:80]
        if req.site and req.site.strip():
            site = normalize_url(req.site.strip())
            if not _looks_like_url(site):
                raise HTTPException(400, "Your website address doesn't look right.")
    else:
        target = normalize_url(req.target.strip())
        if not _looks_like_url(target):
            raise HTTPException(400, "That doesn't look like a website address.")

    competitors = []
    if req.mode == "compare":
        competitors = [normalize_url(u.strip()) for u in req.competitors if u.strip()][:3]
        if not competitors or not all(_looks_like_url(u) for u in competitors):
            raise HTTPException(400, "Add 1-3 competitor page addresses.")

    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"status": "running", "progress": [], "target": target, "mode": req.mode,
                    "engine": req.engine, "started": time.time()}
    threading.Thread(target=_run_job, args=(job_id, req, target, site, competitors), daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job (the server may have restarted).")
    end = job.get("finished", time.time())
    return {**job, "elapsed": round(end - job["started"])}


@app.get("/api/history")
def history_targets():
    """Every tracked page/site with latest and previous score."""
    return history.list_targets()


@app.get("/api/history/runs")
def history_runs(mode: str, target: str):
    """All saved runs for one page or site, oldest first."""
    if mode not in ("page", "site", "speed"):
        raise HTTPException(400, "mode must be page, site or speed")
    return history.get_runs(mode, target)


@app.get("/api/reports")
def list_reports():
    files = sorted(REPORTS.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "modified": p.stat().st_mtime} for p in files]


@app.get("/api/reports/{name}/pdf")
def get_report_pdf(name: str):
    """The report as a downloadable PDF (made with headless Chrome)."""
    if not re.fullmatch(r"[A-Za-z0-9.\-]+\.md", name):
        raise HTTPException(400, "Bad report name.")
    path = REPORTS / name
    if not path.is_file():
        raise HTTPException(404, "Report not found.")
    try:
        pdf = report_to_pdf(path.read_text(), path.stem)
    except Exception as e:
        raise HTTPException(500, f"Could not make the PDF: {e}")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{path.stem}.pdf"'})


@app.get("/api/reports/{name}")
def get_report(name: str):
    # Only allow simple file names, so nobody can read files outside reports/
    if not re.fullmatch(r"[A-Za-z0-9.\-]+\.md", name):
        raise HTTPException(400, "Bad report name.")
    path = REPORTS / name
    if not path.is_file():
        raise HTTPException(404, "Report not found.")
    return {"name": name, "html": md.render(path.read_text())}
