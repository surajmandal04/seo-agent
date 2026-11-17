"""
tools_cli.py — run one of our tools from the terminal and print the JSON result.

This lets Claude Code (the free route) use the SAME tools as agent.py:
    .venv/bin/python tools_cli.py audit_page https://example.com
    .venv/bin/python tools_cli.py check_site_files https://example.com
    .venv/bin/python tools_cli.py check_links https://a.com/x https://a.com/y
    .venv/bin/python tools_cli.py crawl_site https://example.com 30
    .venv/bin/python tools_cli.py keyword_suggestions "coffee grinder" us
    .venv/bin/python tools_cli.py page_topics https://example.com
    .venv/bin/python tools_cli.py check_draft drafts/my-article.md "target keyword"
"""

import sys
from pathlib import Path

from tools import run_tool

DRAFTS = (Path(__file__).parent / "drafts").resolve()

if len(sys.argv) < 3:
    sys.exit("Usage: tools_cli.py <tool name> <url or keyword> [...]")

name, args = sys.argv[1], sys.argv[2:]
if name == "check_links":
    tool_input = {"urls": args}
elif name == "crawl_site":
    tool_input = {"url": args[0], "max_pages": int(args[1]) if len(args) > 1 else 30}
elif name == "keyword_suggestions":
    tool_input = {"seed": args[0], "country": args[1] if len(args) > 1 else "us"}
elif name == "compare_pages":
    # compare_pages "<keyword>" <your_url> <competitor_url> [more competitors]
    tool_input = {"keyword": args[0], "your_url": args[1], "competitor_urls": args[2:5]}
elif name == "search_console":
    tool_input = {"site": args[0], "days": int(args[1]) if len(args) > 1 else 28}
elif name == "page_speed":
    tool_input = {"url": args[0], "strategy": args[1] if len(args) > 1 else "mobile"}
elif name == "check_draft":
    path = Path(args[0]).resolve()
    if path.parent != DRAFTS or path.suffix != ".md":  # only read drafts/*.md
        sys.exit("check_draft only reads .md files inside drafts/")
    tool_input = {"draft": path.read_text(), "keyword": args[1] if len(args) > 1 else ""}
else:
    tool_input = {"url": args[0]}

output, is_error = run_tool(name, tool_input)
print(output)
sys.exit(1 if is_error else 0)
