"""
export_pdf.py — turn a Markdown report into a clean PDF you can send to a client.

How: Markdown -> HTML (with print-friendly styles) -> Google Chrome in
"headless" mode (no window) prints it to PDF. No extra Python packages needed.

Usage:
    .venv/bin/python export_pdf.py reports/example.com.md      # writes reports/example.com.pdf
"""

import subprocess
import sys
import tempfile
import time
from html import escape
from pathlib import Path

from markdown_it import MarkdownIt

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
md = MarkdownIt("commonmark", {"html": False}).enable("table")  # raw HTML in reports is never run

PRINT_CSS = """
  @page { size: A4; margin: 18mm 16mm 20mm; }
  body { font: 10.5pt/1.5 -apple-system, "Helvetica Neue", Arial, sans-serif; color: #1b1f24; }
  header { border-bottom: 2px solid #3b5bdb; padding-bottom: 8px; margin-bottom: 18px;
           display: flex; justify-content: space-between; align-items: baseline; }
  header .brand { font-weight: 700; font-size: 13pt; color: #3b5bdb; }
  header .meta { color: #5d6673; font-size: 9pt; }
  h1 { font-size: 18pt; margin: 0 0 10px; }
  h2 { font-size: 13.5pt; margin: 22px 0 8px; border-bottom: 1px solid #e3e6eb; padding-bottom: 4px;
       break-after: avoid; }
  h3 { font-size: 11.5pt; margin: 16px 0 6px; break-after: avoid; }
  table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 9pt; }
  th, td { border: 1px solid #d5d9df; padding: 5px 7px; text-align: left; vertical-align: top; }
  th { background: #f1f3f5; }
  tr { break-inside: avoid; }
  code { background: #f1f3f5; padding: 1px 4px; border-radius: 3px; font-size: 8.5pt; word-break: break-word; }
  pre { background: #f1f3f5; padding: 8px; border-radius: 4px; white-space: pre-wrap; font-size: 8.5pt; }
  a { color: #3b5bdb; }
  hr { border: 0; border-top: 1px solid #e3e6eb; }
"""


def report_html(markdown: str, title: str) -> str:
    """The report as a standalone, print-styled HTML page."""
    date = time.strftime("%d %b %Y")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{escape(title)}</title><style>{PRINT_CSS}</style></head><body>
<header><span class="brand">SEO Agent report</span><span class="meta">{escape(title)} · {date}</span></header>
{md.render(markdown)}
</body></html>"""


def report_to_pdf(markdown: str, title: str) -> bytes:
    """Render Markdown to PDF bytes using headless Chrome."""
    if not CHROME.exists():
        raise RuntimeError("Google Chrome not found - it's needed to make PDFs.")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "report.html").write_text(report_html(markdown, title), encoding="utf-8")
        out = tmp / "report.pdf"
        subprocess.run([
            str(CHROME), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
            f"--user-data-dir={tmp / 'profile'}",  # throwaway profile: never touches your Chrome
            f"--print-to-pdf={out}", (tmp / "report.html").as_uri(),
        ], check=True, capture_output=True, timeout=90)
        return out.read_bytes()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: export_pdf.py reports/<name>.md")
    src = Path(sys.argv[1])
    pdf = src.with_suffix(".pdf")
    pdf.write_bytes(report_to_pdf(src.read_text(), src.stem))
    print(f"Saved {pdf} ({pdf.stat().st_size // 1024} KB)")
