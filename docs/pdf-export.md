# PDF export

Turns any Markdown report into a clean, print-styled PDF you can send to a client.

## Run it

```bash
.venv/bin/python export_pdf.py reports/example.com.md     # writes reports/example.com.pdf
```

Web UI: click **Download PDF** on any open report.

API:

```
GET /api/reports/{name}/pdf
```

## How it works (`export_pdf.py`)

1. Markdown → HTML with `markdown-it-py`. Raw HTML is disabled because reports contain text from crawled pages.
2. The HTML is wrapped in print-friendly CSS: a white page, readable tables and page-break rules.
3. **Google Chrome in headless mode** prints it to PDF (`--headless=new --print-to-pdf --no-pdf-header-footer`), using a throwaway browser profile so your own Chrome isn't touched.

No extra Python packages are needed.

## Requirements

Google Chrome at `/Applications/Google Chrome.app` (macOS). On other systems, change `CHROME` at the top of `export_pdf.py` to your Chrome or Chromium path.
