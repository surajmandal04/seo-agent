#!/usr/bin/env bash
# audit_free.sh — shortcut for main.py with the free engine (your Claude
# subscription via Claude Code, no API credits).
#
# Usage:
#   ./audit_free.sh https://yoursite.com                    # audit one page
#   ./audit_free.sh https://yoursite.com --site --pages 30  # crawl whole site
#   ./audit_free.sh "coffee grinder" --keywords             # keyword research
#   ./audit_free.sh "coffee grinder" --keywords --country gb --mysite https://mysite.com
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python main.py "$@" --engine free
