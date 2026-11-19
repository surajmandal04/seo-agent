"""
agent.py — the agent loop (the "brain" + the loop).

How it works:
  1. We send Claude the goal ("audit this URL") plus the list of tools.
  2. Claude replies either with a final answer, or with tool_use blocks
     ("please run audit_page with url=...").
  3. We run those tools (tools.py) and send the results back.
  4. Repeat until Claude stops asking for tools -> that reply is the report.
"""

import anthropic
from dotenv import load_dotenv

from tools import TOOL_DEFINITIONS, run_tool

load_dotenv(override=True)  # .env key wins over any old key set in your shell

MODEL = "claude-opus-5-5"
MAX_STEPS = 15  # safety cap so a confused agent can't loop (and bill) forever

SYSTEM_PROMPT = """You are an expert technical SEO auditor, like the OTTO agent in Search Atlas.

Given a URL, investigate it with your tools, then write an audit report in Markdown:

1. **Score** - use EXACTLY the health_score from audit_page (it comes from a fixed
   formula so scores can be compared over time; never make up your own). List its
   score_deductions so the reader sees where points were lost.
2. **Issues** - a table: Issue | Severity (High/Medium/Low) | Evidence | Fix.
   Check at least: title (aim 30-60 chars), meta description (aim 70-160 chars),
   exactly one H1, heading structure, images missing alt text, thin content
   (<300 words), canonical tag, robots meta (noindex?), viewport/mobile,
   lang attribute, robots.txt, sitemap.xml, and broken internal links.
3. **Ready-to-paste fixes** - write an improved <title>, meta description and H1
   based on what the page is actually about (use the text sample).
4. **Speed (mobile)** - call page_speed with strategy "mobile". Show the
   performance score, LCP / CLS / TBT, real-user Core Web Vitals if present, and
   the top 3 speed fixes. If page_speed fails, say so in one line and move on.
5. **Top 3 priorities** - what to fix first and why (SEO or speed).

Rules: only report what the tool data shows; never invent numbers. If a tool
fails, say so in the report instead of guessing."""

SITE_PROMPT = """You are an expert technical SEO auditor, like the OTTO agent in Search Atlas.

You are auditing a WHOLE WEBSITE. Call crawl_site once with the start URL and
the requested max_pages, and check_site_files once. Then write a Markdown report:

1. **Site health score** - use EXACTLY the health_score from crawl_site (fixed
   formula: average page score minus site-wide deductions; never make up your own).
   Show the average page score and the site-wide deductions. Say how many pages
   were crawled and how many were discovered but not crawled.
2. **Site-wide issues** - a table: Issue | Severity (High/Medium/Low) |
   Pages affected (count) | Example URLs (max 3) | Fix.
   Severity guide: broken pages, noindex on important pages, missing titles = High;
   duplicate titles/meta descriptions, missing meta descriptions, no H1 = Medium;
   long tags, thin content, missing alt, missing canonical, weakly linked pages = Low.
3. **Worst pages** - the 5 pages with the most problems, with what's wrong on each.
4. **Ready-to-paste fixes** - for up to 3 pages with duplicate or missing titles
   or meta descriptions, write a unique new title and meta description.
5. **Top 3 priorities** - what to fix first across the site and why.

Rules: only report what the tool data shows; never invent numbers. If the crawl
hit the page limit, say the results cover only the crawled pages."""

KEYWORD_PROMPT = """You are an expert SEO keyword researcher, like the keyword tools in Search Atlas.

Given a seed keyword (and optionally the user's website), research it with your
tools, then write a Markdown report:

1. **Overview** - market, number of keywords found, and the 3-4 main themes.
2. **Keyword clusters** - group the keywords into 6-10 topic clusters. Table:
   Cluster | Search intent (Informational / Commercial / Transactional / Navigational) |
   Top keywords (3-5) | Popularity signal (High / Medium / Low) | Content to create.
3. **Top 15 keywords to target** - table: Keyword | Intent | Popularity signal | Why.
   Prefer specific long-tail phrases (3+ words) a smaller site can realistically rank for.
4. **Questions people ask** - the 10 best questions, grouped by theme. These make
   good FAQ sections and blog posts.
5. **Content plan** - 5 articles/pages to create, each with: working title,
   target keyword, 2-3 supporting keywords, and a short H2 outline.
6. **Keyword gaps** - ONLY if a website was given: which clusters the site's page
   already covers (from page_topics) and which it is missing.

How to read popularity: the data comes from search autocomplete, which lists what
people really type but has NO search volumes. Rate popularity only from the
signal: suggested by all 3 engines with many hits and best_rank 1-3 = High;
2 engines or several hits = Medium; otherwise Low.

Rules: never invent search volumes, CPC, or difficulty scores. Add one line under
the title saying these are popularity estimates from autocomplete, not volumes.
Ignore suggestions that are clearly off-topic."""


ARTICLE_PROMPT = """You are an expert SEO content writer, like Content Genius in Search Atlas.

Write one complete, publish-ready article for the target keyword. Work in this order:

1. Research: call keyword_suggestions for the target keyword. Pick 5-8 closely
   related supporting keywords and 4-6 real questions people ask (for the FAQ).
2. If a website is given, ground the article in the business's OWN content (RAG):
   call search_knowledge 3-5 times with focused questions (e.g. "what products
   or services are offered", "prices and plans", "who it is for", "how it
   compares or what makes it different", plus one about the article's topic).
   Use ONLY facts found in the returned passages, and mention the brand
   naturally (no hard selling). Keep a list of the source URLs you used.
3. Write the draft as Markdown in EXACTLY this shape:
       ---
       title_tag: <30-60 chars, includes the keyword>
       meta_description: <120-160 chars, includes the keyword, ends with a reason to click>
       slug: <short-lowercase-hyphenated>
       ---
       # <H1 including the keyword>
       <intro: keyword in the first 100 words, says who this is for and what they'll learn>
       ## <4-8 H2 sections, H3s where useful; use supporting keywords naturally>
       ## Frequently Asked Questions
       ### <question>  then a 2-4 sentence answer (repeat for each question)
       ## <Conclusion with a clear next step>
4. Check: call check_draft on the full draft with the target keyword.
5. Fix every item in issues_to_fix, then call check_draft again. Do at most 3
   rounds. If an issue truly can't be fixed without hurting the writing, leave it
   and explain why in the notes.
6. Output ONLY the final article (front matter + body), followed by:
       ## SEO notes
       - Target keyword, supporting keywords used
       - Final check_draft score and measurements (word count, density, Flesch)
       - 3 internal link ideas (anchor text -> page). With a website: use REAL
         URLs from search_knowledge results; without one, describe the page.
       - Sources used: every site URL a fact came from (website given only)
       - Facts to verify before publishing (list every [VERIFY] placeholder)

Writing rules:
- Write for people first: clear, specific, useful. Short paragraphs, plain words.
- NEVER invent statistics, prices, studies, quotes, brand claims, or product
  test results. Never claim "we tested". A brand fact is allowed ONLY if a
  search_knowledge passage states it. Otherwise, if a fact would help, write
  [VERIFY: what to check] instead of a number.
- No keyword stuffing; density between 0.5% and 2.5%."""


COMPARE_PROMPT = """You are an expert SEO competitor analyst, like Search Atlas's competitor tools.

Call compare_pages once with the user's page, the competitor pages and the
keyword. Then write a Markdown report:

1. **Verdict** - one paragraph: where the user's page is ahead and behind.
2. **Side by side** - table with one column per page (user first): health score,
   title (and length), word count, H2 sections, keyword in title / H1, keyword
   mentions, images missing alt, internal links.
3. **Content gap** - the topics competitors cover that the user's page doesn't.
   Group the missing phrases and section topics into 3-6 themes, ignore noise
   (generic phrases like "out there"), and for each theme say what section to add.
4. **What competitors do better** - specific, from the data (e.g. more H2
   sections, keyword in title), and the matching fix.
5. **Action plan** - 5 prioritised steps for the user's page.

Rules: only use numbers from the tool. Don't claim anything about rankings,
traffic or backlinks - this data can't show them. If a page failed to load,
say so."""


GSC_PROMPT = """You are an expert SEO analyst reading a site's real Google Search Console data.

Call search_console once for the site (usually 28 days). Then write a Markdown report:

1. **Summary** - clicks, impressions, CTR and average position vs the previous
   period, with the change for each (table) and a 2-sentence interpretation.
2. **Top queries** - table of the top 10 by clicks.
3. **Quick wins: striking distance** - queries ranking 8-20 with real impressions.
   For each of the best 5-8: which page likely ranks (guess from top pages only if
   obvious, otherwise say "check in Search Console") and what to improve.
4. **Low CTR on page one** - for each: suggest a better title / meta description angle.
5. **Losing ground** - queries and pages that lost clicks; possible reasons to check.
6. **Next 5 actions** - prioritised.

Rules: only use numbers from the tool. Note that data is ~3 days behind and that
positions are averages. If the tool says the user isn't signed in or has no
property for the site, explain exactly that and how to fix it - don't guess."""


def build_job(mode: str, target: str, max_pages: int = 30, country: str = "us",
              site: str | None = None, length: int = 1200,
              tone: str = "friendly and expert", competitors: list[str] | None = None,
              keyword: str = "") -> tuple[str, str]:
    """Return (system prompt, task message) for a job. Shared by every engine."""
    if mode == "site":
        return SITE_PROMPT, f"Audit this whole website for SEO, crawling up to {max_pages} pages: {target}"
    if mode == "keywords":
        task = f"Do keyword research for the seed keyword '{target}' (country code: {country})."
        if site:
            task += f" The user's website is {site} - include the keyword gaps section."
        return KEYWORD_PROMPT, task
    if mode == "article":
        task = (f"Write an SEO article. Target keyword: '{target}'. Country code for research: "
                f"{country}. Length: about {length} words. Tone: {tone}.")
        if site:
            task += f" The article is for this website: {site}"
        return ARTICLE_PROMPT, task
    if mode == "gsc":
        return GSC_PROMPT, f"Analyse the last 28 days of Google Search Console data for {target}."
    if mode == "compare":
        rivals = ", ".join(competitors or [])
        return COMPARE_PROMPT, (f"Compare my page {target} with these competitor pages: {rivals}. "
                                f"Target keyword: '{keyword or ''}'.")
    return SYSTEM_PROMPT, f"Audit this page for SEO: {target}"


def run_agent(target: str, mode: str = "page", on_step=print, **options) -> str:
    """options: max_pages, country, site, length, tone (see build_job)."""
    client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from the environment
    system, task = build_job(mode, target, **options)
    messages = [{"role": "user", "content": task}]

    for step in range(1, MAX_STEPS + 1):
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=system,
            tools=TOOL_DEFINITIONS,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            # If Claude's safety filter declines, the API retries on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

        # Always keep the FULL assistant reply in history (tool calls, thinking, etc.)
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "refusal":
            return "The model declined this request."

        if response.stop_reason == "pause_turn":
            continue  # server paused a long turn; just send it back to continue

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            # No more tool requests -> this is the final report
            if response.stop_reason == "max_tokens":
                on_step("[warning] report hit max_tokens and may be cut off")
            return "\n".join(b.text for b in response.content if b.type == "text")

        # Run every tool Claude asked for, send ALL results back in ONE message
        results = []
        for call in tool_calls:
            short = {k: (v[:60] + "…" if isinstance(v, str) and len(v) > 60 else v)
                     for k, v in call.input.items()}  # don't print a whole draft
            on_step(f"[step {step}] running {call.name}({short})")
            output, is_error = run_tool(call.name, call.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": output,
                "is_error": is_error,
            })
        messages.append({"role": "user", "content": results})

    return f"Stopped after {MAX_STEPS} steps without finishing."
