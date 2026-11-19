"""
tools.py — the agent's "hands".

Each function here is a normal Python function. The LLM cannot run code itself;
it can only ASK us to run one of these by name. We describe each tool to the
model in TOOL_DEFINITIONS (name + description + input schema), and
run_tool() dispatches the model's request to the right function.
"""

import json
import os
import re
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import gsc
import history
import rag

load_dotenv(Path(__file__).parent / ".env")  # PAGESPEED_API_KEY etc., also when run via tools_cli.py

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SEOAgent/0.1)"}
TIMEOUT = 15  # seconds


def _get_soup(url: str):
    """Download a page and parse its HTML. Shared by the tools below."""
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    resp.raise_for_status()
    return resp, BeautifulSoup(resp.content, "html.parser")  # bytes -> correct charset


# ---------------------------------------------------------------------------
# Tool 1: on-page SEO audit
# ---------------------------------------------------------------------------
def audit_page(url: str) -> dict:
    """Fetch one page and collect the on-page SEO facts an auditor checks."""
    resp, soup = _get_soup(url)

    title = soup.title.get_text(strip=True) if soup.title else None
    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_desc = meta_desc_tag.get("content", "").strip() if meta_desc_tag else None
    canonical_tag = soup.find("link", rel="canonical")
    robots_tag = soup.find("meta", attrs={"name": "robots"})
    viewport_tag = soup.find("meta", attrs={"name": "viewport"})
    html_tag = soup.find("html")

    headings = {
        f"h{i}": [h.get_text(" ", strip=True)[:120] for h in soup.find_all(f"h{i}")]
        for i in range(1, 4)
    }

    images = soup.find_all("img")
    missing_alt = [img.get("src", "")[:150] for img in images if not img.get("alt", "").strip()]

    # Word count of visible text (drop scripts/styles first)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    words = soup.get_text(" ", strip=True).split()

    # Internal vs external links
    host = urlparse(resp.url).netloc
    internal, external = set(), set()
    for a in soup.find_all("a", href=True):
        full = urljoin(resp.url, a["href"])
        if not full.startswith("http"):
            continue
        (internal if urlparse(full).netloc == host else external).add(full.split("#")[0])

    result = {
        "final_url": resp.url,
        "status_code": resp.status_code,
        "title": title,
        "title_length": len(title) if title else 0,
        "meta_description": meta_desc,
        "meta_description_length": len(meta_desc) if meta_desc else 0,
        "canonical": canonical_tag.get("href") if canonical_tag else None,
        "robots_meta": robots_tag.get("content") if robots_tag else None,
        "has_viewport_meta": viewport_tag is not None,
        "lang_attribute": html_tag.get("lang") if html_tag else None,
        "headings": headings,
        "h1_count": len(headings["h1"]),
        "image_count": len(images),
        "images_missing_alt": missing_alt[:20],
        "images_missing_alt_count": len(missing_alt),
        "word_count": len(words),
        "text_sample": " ".join(words[:300]),  # so the model can write relevant fixes
        "internal_link_count": len(internal),
        "external_link_count": len(external),
        "internal_links_sample": sorted(internal)[:15],
    }
    result["health_score"], result["score_deductions"] = score_page(result)
    return result


# ---------------------------------------------------------------------------
# Fixed scoring formula. The score is computed by CODE, not by the AI, so the
# same site always gets the same score and a trend over time means something.
# ---------------------------------------------------------------------------
def score_page(p: dict) -> tuple[int, list[str]]:
    """Score one page 0-100 from its audit data. Returns (score, reasons for deductions)."""
    rules = []
    title_len = p.get("title_length", 0)
    meta_len = p.get("meta_description_length", 0)
    missing_alt = p.get("images_missing_alt_count", p.get("images_missing_alt", 0))
    if isinstance(missing_alt, list):
        missing_alt = len(missing_alt)

    if not title_len:
        rules.append((15, "missing title"))
    elif not 30 <= title_len <= 60:
        rules.append((5, "title length outside 30-60 chars"))
    if not meta_len:
        rules.append((10, "missing meta description"))
    elif not 70 <= meta_len <= 160:
        rules.append((3, "meta description length outside 70-160 chars"))
    if p.get("h1_count", 0) == 0:
        rules.append((10, "no H1"))
    elif p.get("h1_count", 0) > 1:
        rules.append((5, "more than one H1"))
    if p.get("word_count", 0) < 300:
        rules.append((10, "thin content (<300 words)"))
    if missing_alt:
        rules.append((min(10, missing_alt), f"{missing_alt} images missing alt text"))
    if not p.get("canonical"):
        rules.append((5, "missing canonical"))
    noindex = p.get("noindex", "noindex" in (p.get("robots_meta") or "").lower())
    if noindex:
        rules.append((20, "page set to noindex"))
    if p.get("has_viewport_meta") is False:
        rules.append((10, "no viewport meta (mobile)"))
    if "lang_attribute" in p and not p.get("lang_attribute"):
        rules.append((3, "no lang attribute"))

    score = max(0, 100 - sum(points for points, _ in rules))
    return score, [f"-{points}: {reason}" for points, reason in rules]


# ---------------------------------------------------------------------------
# Tool 2: site-level files (robots.txt + sitemap.xml)
# ---------------------------------------------------------------------------
def check_site_files(url: str) -> dict:
    """Check that robots.txt and sitemap.xml exist for the site's domain."""
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    result = {}
    for name in ("robots.txt", "sitemap.xml"):
        try:
            r = requests.get(f"{root}/{name}", headers=HEADERS, timeout=TIMEOUT)
            result[name] = {
                "status_code": r.status_code,
                "found": r.status_code == 200,
                "preview": r.text[:500] if r.status_code == 200 else None,
            }
        except requests.RequestException as e:
            result[name] = {"found": False, "error": str(e)}
    return result


# ---------------------------------------------------------------------------
# Tool 3: broken-link check
# ---------------------------------------------------------------------------
def check_links(urls: list[str]) -> dict:
    """Return the HTTP status of each URL so the agent can spot broken links."""
    out = {}
    for u in urls[:20]:  # cap so one call can't take forever
        try:
            r = requests.head(u, headers=HEADERS, timeout=10, allow_redirects=True)
            if r.status_code in (405, 403):  # some servers refuse HEAD; retry with GET
                r = requests.get(u, headers=HEADERS, timeout=10, allow_redirects=True)
            out[u] = r.status_code
        except requests.RequestException as e:
            out[u] = f"error: {type(e).__name__}"
    return out


# ---------------------------------------------------------------------------
# Tool 4: crawl the whole site
# ---------------------------------------------------------------------------
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg",
                   ".zip", ".mp4", ".mp3", ".css", ".js", ".xml", ".json", ".ico")
CRAWL_DELAY = 0.3  # seconds between requests, so we don't hammer the site
HARD_PAGE_CAP = 100


def _normalize(url: str) -> str:
    """Drop #fragments and trailing slashes so /about and /about/ count once."""
    url = url.split("#")[0]
    path = urlparse(url).path
    if path == "":            # https://site.com  -> https://site.com/
        return url.replace(urlparse(url).netloc, urlparse(url).netloc + "/", 1) if "?" not in url else url
    if path != "/" and url.endswith("/"):  # /about/ -> /about
        return url[:-1]
    return url


def crawl_site(url: str, max_pages: int = 30, _on_page=None) -> dict:
    """
    Breadth-first crawl: start at `url`, follow internal links, audit each page.
    Respects robots.txt. Returns per-page summaries plus site-wide findings
    (broken pages, duplicate titles/descriptions, missing tags, thin pages).

    _on_page(url, title, soup): optional Python-only hook (not exposed to the model)
    so rag.py can index each page from the SAME download instead of fetching twice.
    """
    max_pages = max(1, min(int(max_pages), HARD_PAGE_CAP))
    start = _normalize(url)
    host = urlparse(start).netloc

    # Fetch robots.txt with OUR headers (urllib's default user-agent is often
    # blocked by firewalls with 403, which RobotFileParser treats as "deny all").
    robots = None  # None = no usable robots.txt -> crawl everything
    try:
        r = requests.get(f"{urlparse(start).scheme}://{host}/robots.txt",
                         headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            robots = RobotFileParser()
            robots.parse(r.text.splitlines())
    except requests.RequestException:
        pass

    queue, seen, crawled = deque([start]), {start}, set()
    pages, broken, blocked = [], [], []
    inlinks = Counter()  # how many crawled pages link to each URL

    while queue and len(pages) < max_pages:
        page_url = queue.popleft()
        if robots and not robots.can_fetch(HEADERS["User-Agent"], page_url):
            blocked.append(page_url)
            continue
        time.sleep(CRAWL_DELAY)
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException as e:
            broken.append({"url": page_url, "status": f"error: {type(e).__name__}"})
            continue
        if resp.status_code >= 400:
            broken.append({"url": page_url, "status": resp.status_code})
            continue
        if "text/html" not in resp.headers.get("Content-Type", ""):
            continue
        if urlparse(resp.url).netloc != host:  # redirected off-site
            continue
        final_url = _normalize(resp.url)
        if final_url in crawled:  # a redirect landed on a page we already have
            continue
        crawled.add(final_url)

        # Pass raw bytes so BeautifulSoup reads the page's own <meta charset>.
        # (resp.text guesses ISO-8859-1 when the header has no charset -> "â¢" for "™")
        soup = BeautifulSoup(resp.content, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else ""
        desc_tag = soup.find("meta", attrs={"name": "description"})
        desc = desc_tag.get("content", "").strip() if desc_tag else ""
        robots_meta = soup.find("meta", attrs={"name": "robots"})
        canonical = soup.find("link", rel="canonical")
        viewport = soup.find("meta", attrs={"name": "viewport"})
        html_tag = soup.find("html")
        h1s = soup.find_all("h1")
        imgs = soup.find_all("img")

        # Collect internal links BEFORE stripping scripts, to queue the next pages
        links_on_this_page = set()
        for a in soup.find_all("a", href=True):
            link = _normalize(urljoin(resp.url, a["href"]))
            parsed = urlparse(link)
            if parsed.scheme not in ("http", "https") or parsed.netloc != host:
                continue
            if parsed.path.lower().endswith(SKIP_EXTENSIONS):
                continue
            if link not in links_on_this_page:  # count each linking PAGE once
                links_on_this_page.add(link)
                inlinks[link] += 1
            if link not in seen:
                seen.add(link)
                queue.append(link)

        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()

        pages.append({
            "url": resp.url,  # the real URL as served (normalized form is only for dedupe)
            "status": resp.status_code,
            "title": title[:90],
            "title_length": len(title),
            "meta_description_length": len(desc),
            "_meta_description": desc,  # used for duplicate check, removed below
            "h1_count": len(h1s),
            "h1": h1s[0].get_text(" ", strip=True)[:90] if h1s else None,
            "word_count": len(soup.get_text(" ", strip=True).split()),
            "images_missing_alt": sum(1 for i in imgs if not i.get("alt", "").strip()),
            "canonical": canonical.get("href") if canonical else None,
            "noindex": bool(robots_meta and "noindex" in robots_meta.get("content", "").lower()),
            "has_viewport_meta": viewport is not None,
            "lang_attribute": html_tag.get("lang") if html_tag else None,
        })
        pages[-1]["page_score"] = score_page(pages[-1])[0]
        if _on_page:
            _on_page(resp.url, title, soup)

    # ---- site-wide findings -------------------------------------------------
    def dupes(key):
        counts = Counter(p[key] for p in pages if p[key])
        return {val[:90]: [p["url"] for p in pages if p[key] == val]
                for val, n in counts.items() if n > 1}

    findings = {
        "broken_pages": broken,
        "duplicate_titles": dupes("title"),
        "duplicate_meta_descriptions": dupes("_meta_description"),
        "missing_title": [p["url"] for p in pages if not p["title"]],
        "missing_meta_description": [p["url"] for p in pages if not p["meta_description_length"]],
        "title_too_long_over_60": [p["url"] for p in pages if p["title_length"] > 60],
        "meta_description_over_160": [p["url"] for p in pages if p["meta_description_length"] > 160],
        "no_h1": [p["url"] for p in pages if p["h1_count"] == 0],
        "multiple_h1": [p["url"] for p in pages if p["h1_count"] > 1],
        "thin_content_under_300_words": [p["url"] for p in pages if p["word_count"] < 300],
        "missing_canonical": [p["url"] for p in pages if not p["canonical"]],
        "noindex_pages": [p["url"] for p in pages if p["noindex"]],
        "pages_with_images_missing_alt": {p["url"]: p["images_missing_alt"]
                                          for p in pages if p["images_missing_alt"]},
        "blocked_by_robots_txt": blocked,
        "linked_to_only_once": [p["url"] for p in pages
                                if inlinks[_normalize(p["url"])] <= 1 and _normalize(p["url"]) != start],
    }
    for p in pages:
        p.pop("_meta_description")

    # Site score = average page score, minus site-wide problems
    avg_page = round(sum(p["page_score"] for p in pages) / len(pages), 1) if pages else 0
    site_rules = []
    if broken:
        site_rules.append((min(20, 5 * len(broken)), f"{len(broken)} broken pages"))
    if findings["duplicate_titles"]:
        n = len(findings["duplicate_titles"])
        site_rules.append((min(10, 2 * n), f"{n} groups of duplicate titles"))
    if findings["duplicate_meta_descriptions"]:
        n = len(findings["duplicate_meta_descriptions"])
        site_rules.append((min(10, 2 * n), f"{n} groups of duplicate meta descriptions"))
    health_score = max(0, round(avg_page - sum(p for p, _ in site_rules))) if pages else 0

    return {
        "health_score": health_score,
        "score_breakdown": {
            "average_page_score": avg_page,
            "site_wide_deductions": [f"-{p}: {r}" for p, r in site_rules],
            "lowest_scoring_pages": sorted(
                ({"url": p["url"], "page_score": p["page_score"]} for p in pages),
                key=lambda x: x["page_score"])[:5],
        },
        "start_url": start,
        "pages_crawled": len(pages),
        "page_limit": max_pages,
        "urls_discovered_not_crawled": max(0, len(seen) - len(pages) - len(broken) - len(blocked)),
        "findings": findings,
        "pages": pages,
    }


# ---------------------------------------------------------------------------
# Tool 5: keyword ideas from free search-engine autocomplete
# ---------------------------------------------------------------------------
# Country code -> settings each suggestion service expects
MARKETS = {
    "us": {"hl": "en", "gl": "us", "bing": "en-US", "ddg": "us-en", "name": "United States"},
    "gb": {"hl": "en", "gl": "gb", "bing": "en-GB", "ddg": "uk-en", "name": "United Kingdom"},
    "in": {"hl": "en", "gl": "in", "bing": "en-IN", "ddg": "in-en", "name": "India"},
    "ca": {"hl": "en", "gl": "ca", "bing": "en-CA", "ddg": "ca-en", "name": "Canada"},
    "au": {"hl": "en", "gl": "au", "bing": "en-AU", "ddg": "au-en", "name": "Australia"},
}
QUESTION_WORDS = ["how", "what", "why", "which", "where", "when", "can", "is", "does", "who"]
MODIFIERS = ["best", "free", "vs", "for", "near me", "cost", "price", "alternative", "review", "how to"]


def _suggest(service: str, query: str, market: dict) -> list[str]:
    """One autocomplete request. Returns suggestions in the order the engine ranks them."""
    if service == "google":
        url, params = "https://suggestqueries.google.com/complete/search", \
            {"client": "firefox", "hl": market["hl"], "gl": market["gl"], "q": query}
    elif service == "bing":
        url, params = "https://api.bing.com/osjson.aspx", {"query": query, "market": market["bing"]}
    else:  # duckduckgo
        url, params = "https://duckduckgo.com/ac/", {"q": query, "kl": market["ddg"], "type": "list"}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        return [s.strip().lower() for s in data[1] if isinstance(s, str) and s.strip()]
    except (requests.RequestException, ValueError, IndexError, KeyError):
        return []


def keyword_suggestions(seed: str, country: str = "us") -> dict:
    """
    Expand a seed keyword into real search phrases using autocomplete from
    Google, Bing and DuckDuckGo. Autocomplete lists what people actually type,
    roughly ordered by popularity - but gives NO search-volume numbers.

    Popularity signal per keyword:
      hits      = how many different lookups returned it (more = more common)
      best_rank = its best position in any suggestion list (1 = top)
      sources   = which engines suggested it
    """
    seed = " ".join(seed.lower().split())[:80]
    market = MARKETS.get(country.lower(), MARKETS["us"])

    lookups = [("all", seed)]
    lookups += [("google", f"{seed} {c}") for c in "abcdefghijklmnopqrstuvwxyz"]  # "alphabet soup"
    lookups += [("all", f"{q} {seed}") for q in QUESTION_WORDS]
    lookups += [("all", f"{m} {seed}" if m in ("best", "free", "how to") else f"{seed} {m}") for m in MODIFIERS]

    requests_to_make = [(service, query) for engines, query in lookups
                        for service in (["google", "bing", "duckduckgo"] if engines == "all" else [engines])]

    # Run 6 lookups at a time instead of one by one (~90 lookups -> ~15s, not ~90s)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda sq: (sq[0], _suggest(sq[0], sq[1], market)), requests_to_make))

    # Keep only phrases containing every seed word, e.g. drop "stool softener" for "seo tools"
    seed_words = [w for w in seed.split() if w not in STOPWORDS] or seed.split()

    def on_topic(phrase: str) -> bool:
        words = phrase.split()
        return all(any(w == s or w == s + "s" or s == w + "s" for w in words) for s in seed_words)

    found: dict[str, dict] = {}
    for service, suggestions in results:
        for rank, phrase in enumerate(suggestions, start=1):
            if not on_topic(phrase):
                continue
            k = found.setdefault(phrase, {"keyword": phrase, "hits": 0, "best_rank": 99, "sources": set()})
            k["hits"] += 1
            k["best_rank"] = min(k["best_rank"], rank)
            k["sources"].add(service)

    keywords = sorted(found.values(), key=lambda k: (-len(k["sources"]), -k["hits"], k["best_rank"]))
    for k in keywords:
        # Short form keeps the output small: "bdg" = bing + duckduckgo + google
        k["sources"] = "".join(s[0] for s in sorted(k["sources"]))

    questions = [k for k in keywords if k["keyword"].split()[0] in QUESTION_WORDS]
    return {
        "seed": seed,
        "market": market["name"],
        "lookups_made": len(requests_to_make),
        "lookups_that_failed_or_empty": sum(1 for _, s in results if not s),
        "total_keywords": len(keywords),
        "note": ("Autocomplete data has no search volumes. Rank keywords by the popularity "
                 "signal only. sources: b=bing, d=duckduckgo, g=google."),
        "keywords": [k for k in keywords if k not in questions][:200],
        "questions": questions[:60],
    }


# ---------------------------------------------------------------------------
# Tool 6: what a page currently targets (for keyword gap analysis)
# ---------------------------------------------------------------------------
STOPWORDS = set("""a an and are as at be by for from has have how i in is it its of on or our
that the this to was we what when where which who why will with you your can more all
not but they their them about into than then so if do does get use using also just one""".split())


def page_topics(url: str, include_text: bool = False) -> dict:
    """Return the title, headings and most frequent 2- and 3-word phrases on a page.
    include_text=True also returns the page's full lowercase text (for compare_pages)."""
    resp, soup = _get_soup(url)
    title = soup.title.get_text(strip=True) if soup.title else None
    desc = soup.find("meta", attrs={"name": "description"})
    headings = [h.get_text(" ", strip=True)[:100] for h in soup.find_all(["h1", "h2", "h3"])][:40]
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()
    words = [w.strip(".,:;!?()[]\"'“”’").lower() for w in soup.get_text(" ", strip=True).split()]
    words = [w for w in words if w.isalpha() or "-" in w]

    phrases = Counter()
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if gram[0] in STOPWORDS or gram[-1] in STOPWORDS:
                continue
            phrases[" ".join(gram)] += 1

    result = {
        "url": resp.url,
        "title": title,
        "meta_description": desc.get("content", "").strip() if desc else None,
        "headings": headings,
        "top_phrases": [{"phrase": p, "count": c} for p, c in phrases.most_common(40) if c > 1],
        "word_count": len(words),
    }
    if include_text:
        result["full_text"] = " ".join(words)
    return result


# ---------------------------------------------------------------------------
# Tool 6b: compare your page with competitor pages (content gap)
# ---------------------------------------------------------------------------
def _core(words) -> set[str]:
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


def compare_pages(your_url: str, competitor_urls: list[str], keyword: str = "") -> dict:
    """
    Put your page next to up to 3 competitor pages: SEO numbers side by side,
    plus the content gap - phrases and section topics the competitors cover
    that your page never mentions.
    """
    kw = keyword.lower().strip()
    pages, errors = [], []
    for role, url in [("you", your_url)] + [("competitor", u) for u in competitor_urls[:3]]:
        try:
            a, t = audit_page(url), page_topics(url, include_text=True)
        except Exception as e:
            errors.append({"url": url, "error": f"{type(e).__name__}: {e}"})
            continue
        h2s = a["headings"]["h2"]
        pages.append({
            "role": role, "url": a["final_url"], "health_score": a["health_score"],
            "title": a["title"], "title_length": a["title_length"],
            "meta_description_length": a["meta_description_length"],
            "word_count": a["word_count"], "h1": a["headings"]["h1"][:1],
            "h2_count": len(h2s), "h2s": h2s[:15],
            "images_missing_alt": a["images_missing_alt_count"],
            "internal_links": a["internal_link_count"],
            "keyword_in_title": bool(kw) and kw in (a["title"] or "").lower(),
            "keyword_in_h1": bool(kw) and any(kw in h.lower() for h in a["headings"]["h1"]),
            "keyword_mentions": len(re.findall(r"\b" + re.escape(kw) + r"\b", t["full_text"])) if kw else None,
            "_phrases": {p["phrase"] for p in t["top_phrases"]},
            # Full page text + headings, so a topic mentioned anywhere counts as covered
            "_text": t["full_text"] + " " + " ".join(h.lower() for h in t["headings"]),
        })

    you = next((p for p in pages if p["role"] == "you"), None)
    rivals = [p for p in pages if p["role"] == "competitor"]
    gap = {"missing_phrases": [], "missing_section_topics": []}
    if you and rivals:
        your_words = set(re.findall(r"[a-z0-9-]+", you["_text"]))
        # Phrases that competitors repeat but your page never uses
        counts = Counter(ph for r in rivals for ph in r["_phrases"])
        for phrase, n in counts.most_common():
            if not _core(phrase.split()) <= your_words:
                gap["missing_phrases"].append({"phrase": phrase, "used_by_competitors": n})
        gap["missing_phrases"] = gap["missing_phrases"][:25]
        # Competitor H2 sections whose main words barely appear on your page
        for r in rivals:
            for h in r["h2s"]:
                core = _core(re.findall(r"[a-z0-9-]+", h.lower()))
                if core and len(core & your_words) / len(core) < 0.5:
                    gap["missing_section_topics"].append({"heading": h, "from": r["url"]})
        gap["missing_section_topics"] = gap["missing_section_topics"][:20]

    for p in pages:
        p.pop("_phrases"), p.pop("_text")
    avg = lambda k: round(sum(r[k] for r in rivals) / len(rivals)) if rivals else None
    return {
        "keyword": keyword or None,
        "pages": pages,
        "you_vs_competitor_average": {
            k: {"you": you[k] if you else None, "competitors_avg": avg(k)}
            for k in ("health_score", "word_count", "h2_count", "internal_links")},
        "content_gap": gap,
        "errors": errors,
        "note": "Text-based analysis of each page as fetched. It cannot see rankings, backlinks or traffic.",
    }


# ---------------------------------------------------------------------------
# Tool 7: check an article draft (the "editor" in the write -> check -> fix loop)
# ---------------------------------------------------------------------------
def _parse_frontmatter(markdown: str) -> tuple[dict, str]:
    """Split '---\\nkey: value\\n---\\nbody' into ({key: value}, body)."""
    meta, body = {}, markdown
    m = re.match(r"\s*---\s*\n(.*?)\n---\s*\n(.*)", markdown, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip().lower()] = v.strip().strip('"')
        body = m.group(2)
    return meta, body


def _syllables(word: str) -> int:
    """Rough English syllable count (good enough for a readability score)."""
    word = word.lower()
    groups = re.findall(r"[aeiouy]+", word)
    n = len(groups) - (1 if word.endswith("e") and len(groups) > 1 else 0)
    return max(1, n)


def check_draft(draft: str, keyword: str) -> dict:
    """
    Score an article draft for on-page SEO and readability. Returns the
    measurements plus a list of issues to fix. Expects Markdown with front matter:
        ---
        title_tag: ...
        meta_description: ...
        slug: ...
        ---
        # H1 ...
    """
    meta, body = _parse_frontmatter(draft)
    kw = keyword.lower().strip()
    kw_words = kw.split()
    core_words = [w for w in kw_words if w not in STOPWORDS] or kw_words  # "for", "the"... don't count

    h1s = re.findall(r"^# (.+)$", body, re.M)
    h2s = re.findall(r"^## (.+)$", body, re.M)
    # Plain text: drop headings markers, links, emphasis, code
    text = re.sub(r"`[^`]*`|!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)", r"\1", body)
    text = re.sub(r"^#+\s*|[*_>|]", " ", text, flags=re.M)
    words = re.findall(r"[A-Za-z0-9’']+", text)
    word_count = len(words)
    lower_text = " ".join(w.lower() for w in words)

    kw_count = len(re.findall(r"\b" + re.escape(kw) + r"\b", lower_text)) if kw else 0
    density = round(100 * kw_count * len(kw_words) / max(word_count, 1), 2)
    first_100 = " ".join(w.lower() for w in words[:100])

    sentences = [s for s in re.split(r"[.!?]+\s", text) if len(s.split()) > 2]
    avg_sentence = round(word_count / max(len(sentences), 1), 1)
    syllables = sum(_syllables(w) for w in words)
    flesch = round(206.835 - 1.015 * (word_count / max(len(sentences), 1))
                   - 84.6 * (syllables / max(word_count, 1)), 1)
    paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip() and not p.lstrip().startswith(("#", "|", "-", "*", "1."))]
    longest_para = max((len(p.split()) for p in paragraphs), default=0)

    title, desc, slug = meta.get("title_tag", ""), meta.get("meta_description", ""), meta.get("slug", "")
    has = lambda s: kw in s.lower()

    checks = {
        "front matter present (title_tag, meta_description, slug)": bool(title and desc and slug),
        "title_tag 30-60 chars": 30 <= len(title) <= 60,
        "keyword in title_tag": has(title),
        "meta_description 120-160 chars": 120 <= len(desc) <= 160,
        "keyword in meta_description": has(desc),
        "slug is short, lowercase, hyphenated": bool(re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+){0,7}", slug)),
        "exactly one H1": len(h1s) == 1,
        "keyword in H1": bool(h1s) and has(h1s[0]),
        "keyword in first 100 words": kw in first_100,
        "at least 4 H2 sections": len(h2s) >= 4,
        "keyword (or most of its words) in at least one H2": any(
            sum(w in h.lower() for w in core_words) >= max(1, round(len(core_words) * 0.6)) for h in h2s),
        "FAQ section present": any("faq" in h.lower() or "question" in h.lower() for h in h2s),
        "keyword density 0.5-2.5%": 0.5 <= density <= 2.5,
        "readability: Flesch score 50+": flesch >= 50,
        "average sentence under 22 words": avg_sentence < 22,
        "no paragraph over 120 words": longest_para <= 120,
    }
    return {
        "measurements": {
            "word_count": word_count, "title_tag_length": len(title),
            "meta_description_length": len(desc), "h1_count": len(h1s), "h2_count": len(h2s),
            "keyword_count": kw_count, "keyword_density_percent": density,
            "flesch_reading_ease": flesch, "avg_sentence_words": avg_sentence,
            "longest_paragraph_words": longest_para,
        },
        "passed": [name for name, ok in checks.items() if ok],
        "issues_to_fix": [name for name, ok in checks.items() if not ok],
        "score": f"{sum(checks.values())}/{len(checks)}",
    }


# ---------------------------------------------------------------------------
# Tool 8: page speed + Core Web Vitals (Google PageSpeed Insights, free)
# ---------------------------------------------------------------------------
PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
LAB_METRICS = {  # Lighthouse audit id -> friendly name
    "largest-contentful-paint": "LCP (main content shown)",
    "cumulative-layout-shift": "CLS (layout jumping)",
    "total-blocking-time": "TBT (page frozen)",
    "first-contentful-paint": "FCP (first content shown)",
    "speed-index": "Speed Index",
}
FIELD_METRICS = {  # real-user data from Chrome users (only for popular pages)
    "LARGEST_CONTENTFUL_PAINT_MS": "LCP",
    "INTERACTION_TO_NEXT_PAINT": "INP (response to clicks)",
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": "CLS",
}


def page_speed(url: str, strategy: str = "mobile") -> dict:
    """
    Run Google PageSpeed Insights on a URL. Returns the performance score (0-100),
    lab metrics (a test run by Google), real-user Core Web Vitals when Google has
    enough visitors, and the top opportunities to speed the page up.
    Needs PAGESPEED_API_KEY in .env (free from Google Cloud).
    """
    key = os.environ.get("PAGESPEED_API_KEY")
    if not key:
        raise RuntimeError("PAGESPEED_API_KEY is not set. Add a free Google API key to .env "
                           "(see the setup steps). Without it Google's shared quota is usually used up.")
    r = requests.get(PSI_URL, timeout=120, params={
        "url": url, "strategy": strategy, "category": "performance", "key": key})
    if r.status_code != 200:
        msg = r.json().get("error", {}).get("message", r.text[:200]) if r.headers.get(
            "content-type", "").startswith("application/json") else r.text[:200]
        raise RuntimeError(f"PageSpeed API error {r.status_code}: {msg}")
    data = r.json()
    lh = data["lighthouseResult"]
    audits = lh["audits"]

    lab = {name: (audits[aid].get("displayValue") or "").replace("\xa0", " ")  # Google uses non-breaking spaces
           for aid, name in LAB_METRICS.items() if aid in audits}

    field = {}
    for mid, name in FIELD_METRICS.items():
        m = data.get("loadingExperience", {}).get("metrics", {}).get(mid)
        if m:
            value = m["percentile"] / 100 if mid == "CUMULATIVE_LAYOUT_SHIFT_SCORE" else m["percentile"]
            field[name] = {"value": value, "rating": m.get("category")}  # FAST / AVERAGE / SLOW

    opportunities = sorted(
        ({"fix": a["title"], "saves_ms": round(a["details"].get("overallSavingsMs", 0))}
         for a in audits.values()
         if a.get("details", {}).get("type") == "opportunity"
         and a["details"].get("overallSavingsMs", 0) >= 100),
        key=lambda o: -o["saves_ms"])[:8]

    return {
        "url": lh.get("finalUrl", url),
        "strategy": strategy,
        "performance_score": round((lh["categories"]["performance"]["score"] or 0) * 100),
        "lab_metrics": lab,
        "real_user_core_web_vitals": field or "Not enough real-user data from Chrome for this page.",
        "overall_real_user_rating": data.get("loadingExperience", {}).get("overall_category"),
        "top_opportunities": opportunities,
        "_raw_ms": {  # numbers for the history chart
            "lcp": audits.get("largest-contentful-paint", {}).get("numericValue"),
            "tbt": audits.get("total-blocking-time", {}).get("numericValue"),
            "cls": audits.get("cumulative-layout-shift", {}).get("numericValue"),
            "fcp": audits.get("first-contentful-paint", {}).get("numericValue"),
        },
    }


# ---------------------------------------------------------------------------
# What we tell Claude about the tools. The description is what the model reads
# to decide WHEN to call a tool, so write it like docs for a teammate.
# ---------------------------------------------------------------------------
TOOL_DEFINITIONS = [
    {
        "name": "audit_page",
        "description": (
            "Fetch a single web page and return its on-page SEO data: title, meta "
            "description, canonical, robots meta, headings (h1-h3), images missing alt "
            "text, word count, a text sample, and internal/external link counts. "
            "Call this first for any URL you are asked to audit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full URL incl. https://"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "check_site_files",
        "description": "Check whether the site's robots.txt and sitemap.xml exist and preview them.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Any URL on the site"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "check_links",
        "description": (
            "Check HTTP status codes for up to 20 URLs to find broken links (4xx/5xx). "
            "Use it on a sample of the page's internal links."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}, "description": "URLs to check"}
            },
            "required": ["urls"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "crawl_site",
        "description": (
            "Crawl a whole website starting from a URL: follows internal links "
            "(respecting robots.txt) and audits every page found, up to max_pages. "
            "Returns site-wide findings (broken pages, duplicate titles and meta "
            "descriptions, missing/long tags, missing H1, thin pages, noindex, "
            "missing canonicals, images without alt) plus a short summary per page. "
            "Use this for a full-site audit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Homepage or start URL"},
                "max_pages": {"type": "integer", "description": "Pages to crawl (1-100)"},
            },
            "required": ["url", "max_pages"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "keyword_suggestions",
        "description": (
            "Expand a seed keyword into hundreds of real search phrases and questions, "
            "using free autocomplete from Google, Bing and DuckDuckGo for a country. "
            "Each keyword has a popularity signal (hits, best_rank, sources) but NO "
            "search volume. Call once per seed; you may call it again for 1-2 closely "
            "related seeds if the first results are thin."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "seed": {"type": "string", "description": "Seed keyword, e.g. 'seo tools'"},
                "country": {"type": "string", "enum": list(MARKETS), "description": "Market code"},
            },
            "required": ["seed", "country"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "page_topics",
        "description": (
            "Show what a web page currently targets: title, meta description, headings "
            "and its most frequent 2-3 word phrases. Use it on the user's own site to "
            "find keyword gaps."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full URL incl. https://"}},
            "required": ["url"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "check_draft",
        "description": (
            "Score an article draft for on-page SEO and readability: title/meta lengths, "
            "keyword placement (title, meta, H1, first 100 words, H2s), keyword density, "
            "H2 count, FAQ section, Flesch readability, sentence and paragraph length. "
            "Returns issues_to_fix. Call it on every draft and fix the issues before finishing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "draft": {"type": "string", "description": "Full article Markdown including front matter"},
                "keyword": {"type": "string", "description": "The target keyword"},
            },
            "required": ["draft", "keyword"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "compare_pages",
        "description": (
            "Compare the user's page with up to 3 competitor pages: health score, title, "
            "word count, H2 sections, keyword use, internal links - and the content gap "
            "(phrases and section topics competitors cover that the user's page doesn't)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "your_url": {"type": "string", "description": "The user's page"},
                "competitor_urls": {"type": "array", "items": {"type": "string"},
                                    "description": "1-3 competitor page URLs"},
                "keyword": {"type": "string", "description": "Target keyword, or empty string"},
            },
            "required": ["your_url", "competitor_urls", "keyword"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "search_console",
        "description": (
            "Real Google Search data for a site the user owns (Google Search Console): "
            "clicks, impressions, CTR and average position for the last N days vs the "
            "period before; top queries; striking-distance queries (positions 8-20); "
            "page-one queries with low CTR; queries losing clicks; top pages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "site": {"type": "string", "description": "The user's site, e.g. https://mysite.com"},
                "days": {"type": "integer", "description": "Days to look back (7-90), usually 28"},
            },
            "required": ["site", "days"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "page_speed",
        "description": (
            "Measure page speed with Google PageSpeed Insights: performance score 0-100, "
            "lab metrics (LCP, CLS, TBT, FCP, Speed Index), real-user Core Web Vitals when "
            "available, and the top fixes with estimated time saved. Takes 10-40 seconds. "
            "Use strategy 'mobile' (Google ranks on mobile) unless asked otherwise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL incl. https://"},
                "strategy": {"type": "string", "enum": ["mobile", "desktop"]},
            },
            "required": ["url", "strategy"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "search_knowledge",
        "description": (
            "Search a website's OWN content (RAG) and return the most relevant passages, "
            "each with its source URL and section. Use it to get real facts about a "
            "business (products, features, prices, policies, audience) before writing "
            "about it, and to find real pages to link to. Ask focused questions, one "
            "topic per call. The first call for a site indexes it (about 1-2 minutes)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "site": {"type": "string", "description": "The website, e.g. https://mysite.com"},
                "query": {"type": "string", "description": "A focused question, e.g. 'what plans and prices are offered'"},
                "k": {"type": "integer", "description": "How many passages to return (3-8)"},
            },
            "required": ["site", "query", "k"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]

# Map tool name -> Python function
_REGISTRY = {
    "audit_page": audit_page,
    "check_site_files": check_site_files,
    "check_links": check_links,
    "crawl_site": crawl_site,
    "keyword_suggestions": keyword_suggestions,
    "page_topics": page_topics,
    "check_draft": check_draft,
    "page_speed": page_speed,
    "compare_pages": compare_pages,
    "search_console": gsc.search_console,
    "search_knowledge": rag.search_knowledge,
}


def run_tool(name: str, tool_input: dict) -> tuple[str, bool]:
    """Run the tool the model asked for. Returns (result_text, is_error)."""
    fn = _REGISTRY.get(name)
    if fn is None:
        return f"Unknown tool: {name}", True
    try:
        result = fn(**tool_input)
    except Exception as e:  # send the error back so the model can adapt
        return f"{type(e).__name__}: {e}", True
    try:
        history.record_tool_result(name, result)  # save score snapshot for trends
    except Exception as e:  # history must never break an audit
        print(f"[history] could not save: {e}")
    # Compact JSON (no spaces) = fewer tokens for the model to read
    return json.dumps(result, ensure_ascii=False, separators=(",", ":")), False
