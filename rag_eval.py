"""
rag_eval.py — measure retrieval quality instead of guessing.

Each question lists the page(s) that truly answer it. recall@k = share of
questions where a correct page shows up in the top k results. MRR (mean
reciprocal rank) also rewards putting it FIRST (1st = 1.0, 2nd = 0.5, ...).

Questions are deliberately paraphrased (few words copied from the pages), so
pure keyword search can't win just by matching titles.

    .venv/bin/python rag_eval.py            # searchatlas.com must be indexed first
"""
import rag

SITE = "searchatlas.com"
B = "https://searchatlas.com/"
QUESTIONS = [
    ("how much is the cheapest plan each month", ["pricing/", "search-atlas-vs-semrush/"]),
    ("tool that fixes technical problems on my site without a developer", ["otto-seo/"]),
    ("put my agency's own logo on client reports and dashboards", ["white-label-seo-software/", "report-builder/"]),
    ("find out whether chatbots recommend my company", ["llm-visibility/"]),
    ("write blog posts that are ready to publish", ["content-genius/"]),
    ("manage my Google Maps business listing automatically", ["gbp-galactic/"]),
    ("get new pages picked up by Google faster", ["indexer/"]),
    ("create Facebook and Instagram ads with AI", ["meta-ad-studio/"]),
    ("run Google paid search campaigns automatically", ["smart-ads/"]),
    ("plan a cluster of related articles from one keyword", ["seo-topical-maps/"]),
    ("fix inconsistent business name and address across directories", ["local-citations/"]),
    ("an AI assistant that works inside Slack", ["ai-coworker/"]),
    ("build a new website that is already optimised for search", ["website-studio/"]),
    ("how is it different from Ahrefs", ["search-atlas-vs-ahrefs/"]),
    ("what do real customers say about results", ["testimonials/", "case-studies/"]),
    ("scoring how well content covers a topic for LLMs", ["scholar/"]),
    ("companies with many brands and locations", ["enterprise-brand/", "enterprise-agency/"]),
    ("automatic monthly SEO reports for clients", ["report-builder/", "white-label-seo-software/"]),
    ("agent that builds a 90 day growth plan", ["atlas-agent/"]),
    ("side by side matrix of thirty SEO tools", ["compare-seo-tools/"]),
]


def evaluate(method: str, k: int = 5) -> tuple[float, float, list[str]]:
    hits, rr, misses = 0, 0.0, []
    for q, good in QUESTIONS:
        urls = [r["url"] for r in rag.search(SITE, q, k=k, method=method)]
        ranks = [i for i, u in enumerate(urls, 1) if any(u == B + g for g in good)]
        if ranks:
            hits += 1
            rr += 1 / ranks[0]
        else:
            misses.append(q)
    n = len(QUESTIONS)
    return hits / n, rr / n, misses


if __name__ == "__main__":
    print(f"{len(QUESTIONS)} questions on {SITE}\n")
    print(f"{'method':<9} {'recall@5':>9} {'MRR':>6}")
    for method in ("keyword", "vector", "hybrid"):
        recall, mrr, misses = evaluate(method)
        print(f"{method:<9} {recall:>9.0%} {mrr:>6.2f}   misses: {misses or '-'}")
