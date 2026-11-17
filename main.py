"""
main.py — terminal entry point.

Usage:
    .venv/bin/python main.py https://yoursite.com                      # audit one page
    .venv/bin/python main.py https://yoursite.com --site --pages 30    # crawl whole site
    .venv/bin/python main.py "coffee grinder" --keywords                # keyword research
    .venv/bin/python main.py "coffee grinder" --keywords --country in --mysite https://mysite.com
    .venv/bin/python main.py "coffee grinder for french press" --article --length 1500 --tone "casual"
    .venv/bin/python main.py https://mysite.com/page --compare https://rival.com/page --keyword "coffee grinder"
    .venv/bin/python main.py https://mysite.com --gsc        # needs: .venv/bin/python gsc.py login

    --engine api   (default) uses API credits via agent.py
    --engine free  uses your Claude subscription via Claude Code (free_engine.py)

Reports are printed and saved in reports/.
"""

import argparse
import re
from pathlib import Path
from urllib.parse import urlparse

import anthropic

from agent import run_agent
from free_engine import run_free_agent
from tools import MARKETS


def normalize_url(url: str) -> str:
    return url if url.startswith(("http://", "https://")) else "https://" + url


def report_name(mode: str, target: str, country: str = "us") -> str:
    """File name for a saved report, e.g. example.com-site.md or keywords-coffee-grinder-us.md"""
    if mode in ("keywords", "article"):
        slug = re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")[:60] or mode
        return f"keywords-{slug}-{country}.md" if mode == "keywords" else f"article-{slug}.md"
    if mode == "compare":
        return f"compare-{urlparse(target).netloc}.md"
    if mode == "gsc":
        return f"search-console-{urlparse(target).netloc}.md"
    return f"{urlparse(target).netloc}{'-site' if mode == 'site' else ''}.md"


def prettify_article(markdown: str) -> str:
    """Turn the article's front matter (title_tag/meta_description/slug) into a readable table."""
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*\n(.*)", markdown, re.S)
    if not m:
        return markdown
    rows = []
    for line in m.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            value = value.strip().strip('"')
            safe = value.replace("|", "\\|")  # a "|" would break the table
            rows.append(f"| {key.strip().replace('_', ' ').title()} | {safe} | {len(value)} chars |")
    table = "| SEO field | Value | Length |\n|---|---|---|\n" + "\n".join(rows)
    return f"{table}\n\n---\n\n{m.group(2).lstrip()}"


def run_job(engine: str, mode: str, target: str, on_step=print, **options) -> str:
    """Run a job on the chosen engine and return the finished Markdown."""
    runner = run_free_agent if engine == "free" else run_agent
    report = runner(target, mode=mode, on_step=on_step, **options)
    return prettify_article(report) if mode == "article" else report


def main():
    parser = argparse.ArgumentParser(description="AI SEO agent")
    parser.add_argument("target", help="URL to audit, or keyword with --keywords / --article")
    parser.add_argument("--site", action="store_true", help="crawl and audit the whole site")
    parser.add_argument("--pages", type=int, default=30, help="max pages to crawl (1-100)")
    parser.add_argument("--keywords", action="store_true", help="keyword research for a seed keyword")
    parser.add_argument("--article", action="store_true", help="write an SEO article for a keyword")
    parser.add_argument("--length", type=int, default=1200, help="article length in words")
    parser.add_argument("--tone", default="friendly and expert", help="article tone of voice")
    parser.add_argument("--country", default="us", choices=list(MARKETS), help="keyword market")
    parser.add_argument("--mysite", help="your website (keyword gaps / article brand context)")
    parser.add_argument("--compare", nargs="+", metavar="URL", help="compare target with 1-3 competitor URLs")
    parser.add_argument("--keyword", default="", help="target keyword for --compare")
    parser.add_argument("--gsc", action="store_true", help="report from your Google Search Console data")
    parser.add_argument("--engine", default="api", choices=["api", "free"])
    args = parser.parse_args()

    mode = ("gsc" if args.gsc else "compare" if args.compare else "article" if args.article
            else "keywords" if args.keywords
            else "site" if args.site else "page")
    competitors = [normalize_url(u) for u in (args.compare or [])][:3]
    target = args.target.strip() if mode in ("keywords", "article") else normalize_url(args.target)
    site = normalize_url(args.mysite) if args.mysite else None

    print(f"Running {mode} job for {target} ({args.engine} engine) ...\n")
    try:
        report = run_job(args.engine, mode, target, max_pages=args.pages, country=args.country,
                         site=site, length=args.length, tone=args.tone,
                         competitors=competitors, keyword=args.keyword)
    except anthropic.AuthenticationError:
        raise SystemExit("API key is missing or invalid. Set ANTHROPIC_API_KEY (see .env.example).")
    except anthropic.RateLimitError:
        raise SystemExit("Rate limited by the API. Wait a minute and try again.")
    except anthropic.APIStatusError as e:
        raise SystemExit(f"API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise SystemExit("Could not reach the Anthropic API. Check your internet connection.")
    except RuntimeError as e:  # raised by the free engine
        raise SystemExit(f"Claude Code error: {e}")

    print("\n" + report)
    out = Path("reports") / report_name(mode, target, args.country)
    out.parent.mkdir(exist_ok=True)
    out.write_text(report)
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
