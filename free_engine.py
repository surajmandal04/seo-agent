"""
free_engine.py — run the agent through Claude Code (your subscription, no API
credits), the same way audit_free.sh does, but from Python so the web page can
show live progress.

Claude Code prints one JSON event per line when run with
--output-format stream-json. We read those lines as they arrive:
  - "assistant" events containing tool_use  -> a progress step ("Crawling site...")
  - the final "result" event                 -> the finished Markdown report
"""

import json
import os
import shlex
import subprocess
from pathlib import Path

from agent import build_job

PROJECT_DIR = Path(__file__).parent
CLAUDE_BIN = Path.home() / ".local/bin/claude"

TOOLS_HELP = """Your ONLY tools are these shell commands (run them with Bash, from this folder):
  .venv/bin/python tools_cli.py audit_page <url>
  .venv/bin/python tools_cli.py check_site_files <url>
  .venv/bin/python tools_cli.py check_links <url1> <url2> ...   (max 20)
  .venv/bin/python tools_cli.py crawl_site <url> <max_pages>
  .venv/bin/python tools_cli.py keyword_suggestions "<seed keyword>" <country>
  .venv/bin/python tools_cli.py page_topics <url>
  .venv/bin/python tools_cli.py page_speed <url> <mobile|desktop>
  .venv/bin/python tools_cli.py compare_pages "<keyword or empty>" <your_url> <competitor_url> [more]
  .venv/bin/python tools_cli.py search_console <site> <days>
  .venv/bin/python tools_cli.py search_knowledge <site> "<focused question>" <k>
Each prints JSON. Do not create or edit files. Write the report in plain,
full English sentences. Output ONLY the final Markdown report."""

# Friendly names for the progress list on the web page
STEP_LABELS = {
    "audit_page": "Auditing page",
    "check_site_files": "Checking robots.txt and sitemap.xml",
    "check_links": "Checking links for errors",
    "crawl_site": "Crawling the site",
    "keyword_suggestions": "Collecting keyword ideas",
    "page_topics": "Reading what your page targets",
    "check_draft": "Checking the draft",
    "page_speed": "Measuring page speed",
    "compare_pages": "Comparing pages",
    "search_console": "Reading your Google Search Console data",
    "search_knowledge": "Searching the site's content",
}


def _describe(command: str) -> str:
    """Turn '.venv/bin/python tools_cli.py crawl_site https://x.com 30' into a readable step."""
    try:
        parts = shlex.split(command)  # respects quotes: "seo tools" stays one argument
    except ValueError:
        parts = command.split()
    if "tools_cli.py" in parts:
        i = parts.index("tools_cli.py")
        tool = parts[i + 1] if len(parts) > i + 1 else "?"
        args = parts[i + 2:]
        label = STEP_LABELS.get(tool, tool)
        if tool == "keyword_suggestions" and args:
            return f"{label} for “{args[0]}”"
        if tool == "check_draft":
            return label
        if tool == "search_knowledge" and len(args) > 1:
            return f"{label}: “{args[1]}”"
        if tool == "compare_pages":
            return f"{label}: yours vs {max(0, len(args) - 2)} competitor(s)"
        if tool == "check_links":
            return f"{label} ({len(args)} URLs)"
        if tool == "crawl_site" and len(args) > 1:
            return f"{label} (up to {args[1]} pages)"
        return f"{label}: {args[0]}" if args else label
    return f"Running: {command[:80]}"


ARTICLE_TOOLS_HELP = """
To check a draft: save it with the Write tool to drafts/<slug>.md (the ONLY
place you may write), then run:
  .venv/bin/python tools_cli.py check_draft drafts/<slug>.md "<target keyword>"
To revise, overwrite the same file and run check_draft again."""


def run_free_agent(target: str, mode: str = "page", on_step=print, **options) -> str:
    """options: max_pages, country, site, length, tone (see agent.build_job)."""
    system, task = build_job(mode, target, **options)
    tools_help = TOOLS_HELP
    allowed = ["Bash(.venv/bin/python tools_cli.py *)"]
    if mode == "article":
        tools_help = TOOLS_HELP.replace("Do not create or edit files. ", "") + ARTICLE_TOOLS_HELP
        allowed.append("Edit(drafts/**)")  # Claude Code may write ONLY inside drafts/
        (PROJECT_DIR / "drafts").mkdir(exist_ok=True)

    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)  # use the subscription login, not API credits
    env["BASH_DEFAULT_TIMEOUT_MS"] = env["BASH_MAX_TIMEOUT_MS"] = "600000"

    cmd = [
        str(CLAUDE_BIN), "-p", task,
        "--append-system-prompt", f"{system}\n\n{tools_help}",
        "--allowedTools", *allowed,
        "--permission-mode", "dontAsk",
        "--output-format", "stream-json", "--verbose",
    ]
    # A list of args (no shell) means the URL can't be used to inject shell commands.
    proc = subprocess.Popen(cmd, cwd=PROJECT_DIR, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    report, error = None, None
    for line in proc.stdout:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") != "tool_use":
                    continue
                if block.get("name") == "Write":
                    on_step("Writing the draft")
                else:
                    on_step(_describe(block.get("input", {}).get("command", block.get("name", ""))))
        elif event.get("type") == "result":
            if event.get("is_error"):
                error = event.get("result") or "Claude Code reported an error."
            else:
                report = event.get("result")

    proc.wait()
    if report:
        return report
    stderr = proc.stderr.read().strip()
    raise RuntimeError(error or stderr or f"Claude Code exited with code {proc.returncode}")
