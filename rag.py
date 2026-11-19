"""
rag.py — Retrieval-Augmented Generation (RAG) over a website's own content.

Why: when the agent writes for a brand, it shouldn't guess facts. RAG lets it
LOOK THEM UP: we index the site once, then the agent searches it like a tiny
private Google and cites the page each fact came from.

Two phases:

  INGEST (slow, once a week)            SEARCH (fast, during a job)
  crawl site -> split pages into        embed the question -> find the most
  sections -> "chunks" -> embed each    similar chunks (vectors) + exact word
  chunk -> store in SQLite              matches (full-text) -> merge -> top k

Design choices (each one is explained where it happens below):
  - Local embeddings (fastembed + BAAI/bge-small-en-v1.5): free, no API key.
  - Chunk by HEADINGS, not fixed size: a chunk = one topic, so search is precise.
  - Hybrid search (vectors + SQLite FTS5 keywords), merged with Reciprocal Rank Fusion.
  - Idempotent ingest: each chunk has a content hash, so re-indexing only embeds
    what changed and running it twice changes nothing.

Terminal usage:
    .venv/bin/python rag.py index https://example.com --pages 30
    .venv/bin/python rag.py search example.com "what does the pricing include"
    .venv/bin/python rag.py stats
"""

import argparse
import hashlib
import os
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

DATA = Path(__file__).parent / "data"
DB_PATH = Path(os.environ.get("SEO_AGENT_RAG_DB", DATA / "rag.db"))
MODEL_NAME = "BAAI/bge-small-en-v1.5"   # 384-dim English model, ~70 MB, runs on CPU

CHUNK_WORDS = 180    # bge-small reads max 512 tokens; 180 words leaves room for the header
OVERLAP_WORDS = 30   # repeat a little text between neighbouring chunks so no sentence is cut off from its context
MIN_WORDS = 12       # skip scraps like "Learn more" or "Book a demo"
BOILERPLATE_PAGES = 3  # text that appears on 3+ pages is a menu/CTA/banner, not content
RRF_K = 60           # standard constant for Reciprocal Rank Fusion

BLOCKS = ["h1", "h2", "h3", "h4", "p", "li", "td", "th", "blockquote", "dt", "dd", "pre", "figcaption"]
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4}
STOPWORDS = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "is", "are",
             "what", "how", "does", "do", "i", "my", "your", "it", "can", "be", "at", "by", "from"}


# ---------------------------------------------------------------------------
# 1. Embeddings: text -> vector of 384 numbers. Similar meaning = similar vector.
# ---------------------------------------------------------------------------
_model = None


def _embedder():
    """Load the model once (first call downloads it to data/models/, ~70 MB)."""
    global _model
    if _model is None:
        from fastembed import TextEmbedding  # imported lazily: only RAG jobs pay the load time
        _model = TextEmbedding(MODEL_NAME, cache_dir=str(DATA / "models"))
    return _model


def _normalise(m: np.ndarray) -> np.ndarray:
    """Scale every vector to length 1, so a dot product = cosine similarity."""
    return m / np.clip(np.linalg.norm(m, axis=-1, keepdims=True), 1e-12, None)


def embed_passages(texts: list[str]) -> np.ndarray:
    return _normalise(np.array(list(_embedder().passage_embed(texts)), dtype=np.float32))


def embed_query(text: str) -> np.ndarray:
    # BGE models want questions phrased as a search ("Represent this sentence for
    # searching relevant passages: ..."); query_embed adds that prefix for us.
    return _normalise(np.array(list(_embedder().query_embed([text]))[0], dtype=np.float32))


# ---------------------------------------------------------------------------
# 2. Storage: one SQLite file. A normal table for chunks + vectors, and an FTS5
#    full-text index on the same rows for keyword search.
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY,
    site         TEXT NOT NULL,          -- e.g. searchatlas.com
    url          TEXT NOT NULL,
    title        TEXT,
    heading      TEXT,                   -- section path, e.g. "Pricing > Pro plan"
    text         TEXT NOT NULL,
    content_hash TEXT NOT NULL,          -- what makes re-indexing idempotent
    embedding    BLOB NOT NULL,          -- 384 float32 numbers
    indexed_at   REAL NOT NULL,
    UNIQUE (site, url, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_chunks_site_url ON chunks (site, url);

-- Full-text index that mirrors the chunks table ("external content" FTS5).
-- porter = match "grinders" to "grinder"; triggers keep it in sync automatically.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title, heading, text, content='chunks', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts (rowid, title, heading, text) VALUES (new.id, new.title, new.heading, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, title, heading, text)
    VALUES ('delete', old.id, old.title, old.heading, old.text);
END;

CREATE TABLE IF NOT EXISTS sites (
    site TEXT PRIMARY KEY, start_url TEXT, pages INTEGER, chunks INTEGER, indexed_at REAL
);
"""


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def site_key(url_or_host: str) -> str:
    """'https://www.Example.com/x' and 'example.com' both -> 'example.com'."""
    host = urlparse(url_or_host if "://" in url_or_host else f"https://{url_or_host}").netloc
    return host.lower().removeprefix("www.")


# ---------------------------------------------------------------------------
# 3. Chunking: page HTML -> list of {heading, text} pieces.
# ---------------------------------------------------------------------------
def page_chunks(title: str, soup) -> list[dict]:
    """
    Walk the page top to bottom. Every H1-H4 starts a new section; paragraphs,
    list items and table cells are added to the current section. Long sections
    are cut into ~180-word windows with a 30-word overlap.
    """
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "svg", "iframe"]):
        tag.decompose()  # menus, footers and forms are the same on every page: not content
    body = soup.find("main") or soup.body or soup

    sections, path = [], {}            # path = current heading at each level
    current = {"heading": title or "", "parts": []}
    for el in body.find_all(BLOCKS):
        if el.find(BLOCKS):            # a container (e.g. <li> holding <p>s): its children are visited anyway
            continue
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if el.name in HEADINGS:
            sections.append(current)
            level = HEADINGS[el.name]
            path = {lv: h for lv, h in path.items() if lv < level}  # drop deeper/older headings
            path[level] = text[:80]
            current = {"heading": " > ".join(path[lv] for lv in sorted(path)), "parts": []}
        else:
            current["parts"].append(text)
    sections.append(current)

    # Marketing pages are full of tiny H3 "cards" (one sentence each). One-sentence
    # chunks retrieve badly, so PACK small sibling sections that share the same
    # H1 > H2 parent into one chunk, keeping each card's own heading inline.
    chunks, buf = [], None

    def parent(heading):
        return " > ".join(heading.split(" > ")[:2])

    def flush():
        nonlocal buf
        if buf and len(" ".join(buf["parts"]).split()) >= MIN_WORDS:
            chunks.append({"heading": buf["heading"], "text": " ".join(buf["parts"])})
        buf = None

    for sec in sections:
        words = " ".join(sec["parts"]).split()
        if not words:
            continue
        if len(words) >= CHUNK_WORDS // 2:          # a real section: window it on its own
            flush()
            step = CHUNK_WORDS - OVERLAP_WORDS
            for start in range(0, len(words), step):
                piece = words[start:start + CHUNK_WORDS]
                if len(piece) >= MIN_WORDS:
                    chunks.append({"heading": sec["heading"], "text": " ".join(piece)})
                if start + CHUNK_WORDS >= len(words):
                    break
            continue
        par = parent(sec["heading"])
        leaf = sec["heading"].split(" > ")[-1]
        text = f"{leaf}: {' '.join(words)}" if sec["heading"] != par else " ".join(words)
        size = len(" ".join(buf["parts"]).split()) if buf else 0
        if buf and buf["heading"] == par and size + len(text.split()) <= CHUNK_WORDS:
            buf["parts"].append(text)
        else:
            flush()
            buf = {"heading": par, "parts": [text]}
    flush()
    return chunks


def _hash(heading: str, text: str) -> str:
    return hashlib.sha256(f"{heading}\n{text}".encode()).hexdigest()[:20]


def _embed_text(title: str, heading: str, text: str) -> str:
    # "Contextual header": the vector also knows WHICH page and section the text
    # is from, so "Pricing: $99/month" is findable by "how much does X cost".
    return f"{title} | {heading}\n{text}"


# ---------------------------------------------------------------------------
# 4. Ingest: crawl -> chunk -> drop boilerplate -> embed only NEW chunks -> store.
# ---------------------------------------------------------------------------
def ingest(url: str, max_pages: int = 30, log=print) -> dict:
    """Index a site. Safe to run again: unchanged chunks are kept, not re-embedded."""
    from tools import crawl_site  # local import: tools.py imports rag.py too

    site = site_key(url)
    collected = []  # (url, title, chunks) for every crawled page

    def on_page(page_url, title, soup):
        collected.append((page_url, title, page_chunks(title, soup)))

    t0 = time.time()
    crawl = crawl_site(url, max_pages, _on_page=on_page)
    log(f"[rag] crawled {len(collected)} pages in {time.time() - t0:.0f}s")

    # Boilerplate: the same chunk text on many pages (cookie banner, CTA block...)
    seen_on = Counter(_hash("", c["text"]) for _, _, chunks in collected
                      for c in {c["text"]: c for c in chunks}.values())
    is_boilerplate = (lambda c: seen_on[_hash("", c["text"])] >= BOILERPLATE_PAGES) \
        if len(collected) >= BOILERPLATE_PAGES else (lambda c: False)

    added = kept = removed = 0
    now = time.time()
    with _db() as conn:
        for page_url, title, chunks in collected:
            wanted = {}
            for c in chunks:
                if not is_boilerplate(c):
                    wanted[_hash(c["heading"], c["text"])] = c
            existing = dict(conn.execute(
                "SELECT content_hash, id FROM chunks WHERE site=? AND url=?", (site, page_url)))

            stale = [existing[h] for h in existing.keys() - wanted.keys()]
            conn.executemany("DELETE FROM chunks WHERE id=?", [(i,) for i in stale])
            removed += len(stale)

            new = [(h, c) for h, c in wanted.items() if h not in existing]
            kept += len(wanted) - len(new)
            if new:
                vectors = embed_passages([_embed_text(title, c["heading"], c["text"]) for _, c in new])
                conn.executemany(
                    "INSERT INTO chunks (site, url, title, heading, text, content_hash, embedding, indexed_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(site, page_url, title, c["heading"], c["text"], h, v.tobytes(), now)
                     for (h, c), v in zip(new, vectors)])
                added += len(new)

        # Pages that now return 404/500 are gone: forget their chunks too.
        gone = [b["url"] for b in crawl["findings"]["broken_pages"]] if "findings" in crawl else []
        for g in gone:
            removed += conn.execute("DELETE FROM chunks WHERE site=? AND url=?", (site, g)).rowcount

        total = conn.execute("SELECT COUNT(*) FROM chunks WHERE site=?", (site,)).fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO sites VALUES (?,?,?,?,?)",
                     (site, url, len(collected), total, now))

    result = {"site": site, "pages_indexed": len(collected), "chunks_total": total,
              "chunks_added": added, "chunks_unchanged": kept, "chunks_removed": removed,
              "seconds": round(time.time() - t0, 1)}
    log(f"[rag] {result}")
    return result


# ---------------------------------------------------------------------------
# 5. Search: vectors (meaning) + full-text (exact words), merged.
# ---------------------------------------------------------------------------
def _fts_query(query: str) -> str | None:
    """Turn a free-text question into a safe FTS5 query: "word1" OR "word2" ..."""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in STOPWORDS and len(w) > 1]
    return " OR ".join(f'"{w}"' for w in words) or None  # quoted -> user text can't inject FTS syntax


def search(site: str, query: str, k: int = 5, per_url: int = 2, method: str = "hybrid") -> list[dict]:
    """Top-k chunks for a question. At most `per_url` chunks per page, so results stay varied.
    method: "hybrid" (default), or "vector" / "keyword" alone (used by rag_eval.py to compare)."""
    site = site_key(site)
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, url, title, heading, text, embedding FROM chunks WHERE site=?", (site,)).fetchall()
        if not rows:
            raise LookupError(f"{site} is not indexed yet")

        # (a) Vector search: brute force is fine up to ~50k chunks (a few ms with numpy).
        ids = [r[0] for r in rows]
        matrix = np.vstack([np.frombuffer(r[5], dtype=np.float32) for r in rows])
        sims = matrix @ embed_query(query)                       # cosine similarity per chunk
        vector_rank = [ids[i] for i in np.argsort(-sims)[:50]]

        # (b) Keyword search: SQLite's BM25 ranking (lower bm25 = better match).
        keyword_rank = []
        fts = _fts_query(query)
        if fts:
            keyword_rank = [r[0] for r in conn.execute(
                "SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid"
                " WHERE chunks_fts MATCH ? AND c.site = ? ORDER BY bm25(chunks_fts) LIMIT 50",
                (fts, site))]

    # (c) Reciprocal Rank Fusion: score = sum of 1/(60 + rank) over both lists.
    # Uses only RANKS, so we never have to compare a cosine with a BM25 number.
    fused = Counter()
    rankings = {"hybrid": (vector_rank, keyword_rank), "vector": (vector_rank,), "keyword": (keyword_rank,)}
    for ranking in rankings[method]:
        for rank, cid in enumerate(ranking, start=1):
            fused[cid] += 1 / (RRF_K + rank)

    by_id = {r[0]: (r, float(sims[i])) for i, r in enumerate(rows)}
    results, per_page = [], Counter()
    for cid, score in fused.most_common():
        (row, sim) = by_id[cid]
        if per_page[row[1]] >= per_url:
            continue
        per_page[row[1]] += 1
        results.append({
            "url": row[1], "page_title": row[2], "section": row[3], "text": row[4],
            "similarity": round(sim, 3),
            "matched_by": "both" if cid in vector_rank and cid in keyword_rank
                          else "meaning" if cid in vector_rank else "keywords",
        })
        if len(results) == k:
            break
    return results


def search_knowledge(site: str, query: str, k: int = 5) -> dict:
    """The agent's tool. Indexes the site first (up to 30 pages) if it has never been indexed."""
    indexed_now = None
    try:
        hits = search(site, query, k)
    except LookupError:
        indexed_now = ingest(site if "://" in site else f"https://{site}", 30, log=lambda *_: None)
        hits = search(site, query, k)
    out = {"site": site_key(site), "query": query, "results": hits}
    if indexed_now:
        out["indexed_now"] = {k: indexed_now[k] for k in ("pages_indexed", "chunks_total")}
    if not hits:
        out["note"] = "Nothing relevant on this site. Do not invent it; use a [VERIFY] placeholder."
    return out


def refresh_stale(max_age_days: float = 7, log=print) -> int:
    """Re-index every site whose index is older than max_age_days (called weekly by monitor.py).
    Cheap when little changed: only new or edited sections are embedded again."""
    cutoff = time.time() - max_age_days * 86400
    stale = [s for s in stats() if s["indexed_at"] < cutoff]
    for s in stale:
        try:
            ingest(s["start_url"], max(int(s["pages"] or 30), 1), log=log)
        except Exception as e:  # one broken site must not stop the others
            log(f"[rag] refresh {s['site']} failed: {e}")
    return len(stale)


def stats() -> list[dict]:
    with _db() as conn:
        return [dict(zip(("site", "start_url", "pages", "chunks", "indexed_at"), r))
                for r in conn.execute("SELECT * FROM sites ORDER BY indexed_at DESC")]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="RAG index over a website's content")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("index"); i.add_argument("url"); i.add_argument("--pages", type=int, default=30)
    s = sub.add_parser("search"); s.add_argument("site"); s.add_argument("query"); s.add_argument("-k", type=int, default=5)
    sub.add_parser("stats")
    args = p.parse_args()

    if args.cmd == "index":
        ingest(args.url, args.pages)
    elif args.cmd == "search":
        for n, hit in enumerate(search(args.site, args.query, args.k), 1):
            print(f"\n#{n}  {hit['similarity']}  [{hit['matched_by']}]  {hit['url']}\n    § {hit['section']}")
            print("    " + hit["text"][:300] + ("…" if len(hit["text"]) > 300 else ""))
    else:
        for row in stats():
            print(f"{row['site']:30} {row['pages']:>4} pages {row['chunks']:>6} chunks  "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(row['indexed_at']))}")
