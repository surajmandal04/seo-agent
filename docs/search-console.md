# Google Search Console

Reads your **real** Google data for a site you own: the searches that showed your site, clicks, impressions, CTR and average position. It then finds opportunities. Access is **read-only** (`webmasters.readonly` scope), so this code can never change anything in your Search Console.

## One-time setup

1. **Have a verified property.** In [Search Console](https://search.google.com/search-console), the Google account you'll sign in with must own the site or have been added as a user (Settings → Users and permissions).
2. **Create an OAuth client** in [Google Cloud console](https://console.cloud.google.com):
   - Create a project (or reuse one).
   - APIs & Services → Library → enable **Google Search Console API**.
   - OAuth consent screen → External → add **your Google account as a Test user**. Without this you get `Error 403: access_denied`.
   - Credentials → Create credentials → OAuth client ID → **Desktop app** → download the JSON.
3. **Save the file** as `data/gsc_client_secret.json`. It's git-ignored, so never commit it.
4. **Sign in** once:

   ```bash
   .venv/bin/python gsc.py login     # opens a browser; you sign in to Google
   ```

   This saves `data/gsc_token.json` with permissions 600. The token refreshes itself, so you won't need to sign in again.
5. **Check** which sites you can see:

   ```bash
   .venv/bin/python gsc.py sites
   .venv/bin/python gsc.py status
   ```

## Run a report

```bash
./audit_free.sh https://mysite.com --gsc
```

Web UI: mode **Search Console**. If setup is missing, the page shows a hint, which re-checks when you return to the tab.

Report: `reports/search-console-<host>.md`.

The site can be given as `https://mysite.com` or a bare domain. It's matched to your URL-prefix or `sc-domain:` property.

## What it analyses (`gsc.search_console`)

It compares the last 28 days with the 28 days before. Data is about 3 days behind, so the window ends 3 days ago.

- **Totals**: clicks, impressions, CTR and position, with the change for each
- **Top queries** and **top pages**
- **Striking distance**: queries at average position 8–20 with real impressions, where a small push reaches page one
- **Low CTR on page one**: you rank, but few people click, which usually means the title or description needs work
- **Losing clicks**: queries and pages that dropped (drops only)

## Troubleshooting

| Message | Fix |
|---|---|
| `Access blocked … Error 403: access_denied` | Add your account as a Test user on the OAuth consent screen |
| `No verified sites on this Google account.` | Verify the site, or ask its owner to add your account in Search Console |
| Not signed in / no token | Run `gsc.py login` |
| Client secret missing | Put the downloaded JSON at `data/gsc_client_secret.json` |
