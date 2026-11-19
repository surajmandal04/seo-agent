# RAG: grounding the agent in a site's own content

**Retrieval-Augmented Generation** lets the agent look facts up instead of guessing. The site is indexed once; after that, the agent searches it like a small private search engine and cites the page each fact came from.

It's used by the [article writer](article-writer.md) when you give it **your website**. Brand facts such as products, plans, prices and features come only from your pages, and internal link suggestions use real URLs.

## How it works

```
INGEST (rag.ingest, weekly)                       SEARCH (rag.search, during a job)
crawl_site (robots.txt-aware)                     agent calls search_knowledge(site, query, k)
  └─ _on_page hook: same download, no 2nd fetch          │
page_chunks: split by H1–H4 headings                     ├─ (a) vector: embed query, cosine vs all chunks
  └─ pack small sibling cards under one H2               ├─ (b) keyword: SQLite FTS5, BM25 ranking
drop boilerplate (same text on 3+ pages)                 └─ (c) Reciprocal Rank Fusion of (a) + (b)
embed only NEW chunks (content hash)                     max 2 chunks per page → top k with URL + section
store in SQLite: chunks + vectors + FTS5 index
```

Classic RAG retrieves first and pastes the results into the prompt. This project uses **agentic RAG** instead: retrieval is a *tool*, so Claude decides what to ask and how often, and can make several focused searches.

## Design choices

| Choice | Why |
|---|---|
| **Local embeddings**: `fastembed` + `BAAI/bge-small-en-v1.5` (384 dims) | Free, no API key, runs on a laptop CPU, no PyTorch. The model is about 64 MB and downloads to `data/models/` on first use. |
| **Chunk by headings**, about 180 words with a 30-word overlap | One chunk covers one topic, so results are precise. The overlap stops a sentence from losing its context at a boundary. |
| **Pack small sections** under the same H2 | Marketing pages are made of one-sentence "cards". On searchatlas.com this cut 701 chunks to 388 and raised the median chunk from 25 to 111 words. |
| **Contextual header**: the embedded text is `page title \| section path` + text | "$99/mo" becomes findable by "how much does the plan cost". |
| **Drop boilerplate** (text that appears on 3+ pages) | Cookie banners and CTAs would otherwise fill every result list. |
| **Hybrid search**: vectors + FTS5 keywords, fused with **RRF** | Vectors understand meaning; keywords catch exact product names, prices and model numbers. RRF combines the two by *rank*, so a cosine score and a BM25 score never have to be compared. |
| **Max 2 chunks per page** | Keeps results varied. |
| **Idempotent ingest**: a `content_hash` per chunk | Re-indexing only embeds what changed and deletes removed sections. Running it twice changes nothing. |
| **Brute-force numpy search** over SQLite | A few milliseconds for up to about 50k chunks. Beyond that, move to `sqlite-vec` or Postgres `pgvector`. |

## Commands

```bash
.venv/bin/python rag.py index https://example.com --pages 30     # build or refresh the index
.venv/bin/python rag.py search example.com "what plans are offered" -k 5
.venv/bin/python rag.py stats                                    # indexed sites
.venv/bin/python tools_cli.py search_knowledge https://example.com "pricing" 5   # as the agent sees it
.venv/bin/python rag_eval.py                                     # retrieval quality
```

Use it in an article:

```bash
./audit_free.sh "white label seo reporting for agencies" --article --mysite https://searchatlas.com
```

The first `search_knowledge` call for a new site indexes it automatically (up to 30 pages, about 1–2 minutes). After that, the [weekly monitor](weekly-monitoring.md) re-indexes any site whose index is more than 7 days old.

## Storage

`data/rag.db` (git-ignored). Set `SEO_AGENT_RAG_DB` to use another file.

- `chunks`: site, url, title, heading path, text, `content_hash`, `embedding` (float32 blob), `indexed_at`; unique on `(site, url, content_hash)`
- `chunks_fts`: FTS5 external-content index (porter stemming), kept in sync by triggers
- `sites`: one row per indexed site, used for weekly refresh

## Measured results

### Retrieval (`rag_eval.py`)

20 paraphrased questions about searchatlas.com (30 pages, 388 chunks), each with its correct page(s):

| Method | recall@5 | MRR |
|---|---|---|
| Keyword only (FTS5 / BM25) | 95% | 0.62 |
| Vector only (bge-small) | 100% | 0.75 |
| **Hybrid (RRF)** | **100%** | **0.82** |

- **recall@5**: the share of questions where a correct page appears in the top 5.
- **MRR**: also rewards putting the correct page *first*.

Hybrid finds the right page as often as vectors alone, and ranks it higher.

Caveat: this is a small set of questions written by hand, not a benchmark. It's a regression test: re-run it after changing chunking or ranking.

### End to end: article A/B

Same command for both runs (`"white label seo reporting for agencies" --article --length 800 --mysite https://searchatlas.com`). **A** used the code before RAG, where brand context came from `page_topics` on the homepage only. **B** used RAG.

| | A: before RAG | B: with RAG |
|---|---|---|
| Site lookups | 1 (`page_topics`, homepage) | 5 focused `search_knowledge` calls, including a follow-up the agent chose itself ("Report Builder data sources…") |
| Brand claims | 3, all vague ("built for agency workflows") | 5 specific ones: 40+ data sources incl. GA4/GSC/GBP, Pro $399/mo, client portals on Agency from $999/mo, OTTO + Content Genius |
| Claims traceable to a page | n/a (generic) | **5 / 5**, each checked against the indexed passage |
| `[VERIFY]` placeholders | 2 | 1 |
| Internal links | 3 descriptions ("the white-label features page") | 4 real URLs, all returning HTTP 200 |
| Sources section | none | yes, 8 URLs |
| `check_draft` | 16/16 | 16/16 |

What still needs a human: one sentence ("It starts on the Pro plan") is true of White Label, but a reader could take it to mean Report Builder. RAG fixes *where facts come from*, not the clarity of the prose, so keep a human review step before publishing.

## Ideas for later

- **Reranker**: a cross-encoder (for example `bge-reranker-base`) re-scores the top 20 for better precision.
- **"Ask my site" mode** in the web UI: a Q&A chat over the index, with citations.
- **Index past reports and Search Console data**, so the agent can answer "why did my score drop?".
- **pgvector**, when moving to Postgres (fits the planned Django + Celery version).
