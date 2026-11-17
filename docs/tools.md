# Tools reference

All tools live in `tools.py`. The agent calls them through `tools.run_tool(name, input)`, which returns compact JSON and saves scores to the history. Run any tool yourself with `tools_cli.py`:

```bash
.venv/bin/python tools_cli.py <tool> <args…>
```

| Tool | What it does | CLI example |
|---|---|---|
| `audit_page` | On-page SEO data + health score for one URL | `tools_cli.py audit_page https://example.com` |
| `check_site_files` | Does `robots.txt` / `sitemap.xml` exist? | `tools_cli.py check_site_files https://example.com` |
| `check_links` | HTTP status of up to 20 URLs | `tools_cli.py check_links https://a.com/x https://a.com/y` |
| `crawl_site` | Crawl + audit up to N pages, site health score | `tools_cli.py crawl_site https://example.com 30` |
| `keyword_suggestions` | Autocomplete keywords with a popularity signal | `tools_cli.py keyword_suggestions "coffee grinder" us` |
| `page_topics` | What a page is about (headings, key phrases, text) | `tools_cli.py page_topics https://example.com` |
| `check_draft` | 16 SEO and readability checks on a Markdown draft | `tools_cli.py check_draft drafts/my-article.md "keyword"` |
| `compare_pages` | Your page vs up to 3 competitors + content gap | `tools_cli.py compare_pages "keyword" https://me.com https://rival.com` |
| `search_console` | Search Console totals, queries, opportunities | `tools_cli.py search_console https://mysite.com 28` |
| `page_speed` | PageSpeed Insights / Core Web Vitals | `tools_cli.py page_speed https://example.com mobile` |

Notes:
- `check_draft` only reads `.md` files inside `drafts/`.
- Tool output is kept compact (trimmed lists, no extra whitespace) so it fits comfortably in the model's context.
- All web requests are `GET` only, with a timeout, and identify themselves with the User-Agent `Mozilla/5.0 (compatible; SEOAgent/0.1)`.

## Adding a new tool

1. Write the function in `tools.py`. It should return a JSON-serialisable dict.
2. Add it to `_REGISTRY` and add a schema to `TOOL_DEFINITIONS` (`strict: true`, `additionalProperties: false`).
3. Add its argument parsing to `tools_cli.py` and a line to `TOOLS_HELP` in `free_engine.py`.
4. Mention it in the relevant prompt in `agent.py`.
