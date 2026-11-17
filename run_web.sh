#!/usr/bin/env bash
# run_web.sh — start the SEO Agent web page at http://localhost:8000
# Stop it with Ctrl+C.
cd "$(dirname "$0")"
echo "Open http://localhost:8000 in your browser (Ctrl+C to stop)"
exec .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8000
