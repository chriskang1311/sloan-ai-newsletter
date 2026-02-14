# Newsletter Workflow

## Objective
Send a weekly AI/ML newsletter — *AI for Sloanies* — to MIT Sloan MBA students every Wednesday at 8am ET. The newsletter summarizes the week's most relevant AI news in two sections: big-company gossip and startup/builder activity.

## Required Secrets

All stored in Modal under secret name `newsletter-secrets` AND mirrored in `.env` for local dev.

| Key | Source | Notes |
|-----|--------|-------|
| `NEWSAPI_KEY` | newsapi.org free account | 100 req/day limit on free tier |
| `ANTHROPIC_API_KEY` | console.anthropic.com | Powers all Claude calls |
| `GMAIL_CLIENT_ID` | Google Cloud Console OAuth app | From credentials.json |
| `GMAIL_CLIENT_SECRET` | Google Cloud Console OAuth app | From credentials.json |
| `GMAIL_REFRESH_TOKEN` | Output of `tools/setup_gmail_oauth.py` | Long-lived; rarely expires |
| `GMAIL_SENDER_EMAIL` | Your Gmail address | Must match the account used in OAuth |
| `RECIPIENT_EMAILS` | Your mailing list | Comma-separated, no spaces |

## Execution Sequence

```
1. tools/scrape_news.py
   - Fetches up to 15 articles from NewsAPI + 5 RSS feeds
   - Deduplicates by URL and title similarity
   - Returns list of article dicts sorted by date (newest first)
   - Raises ValueError if fewer than 6 articles found

2. tools/generate_newsletter.py
   - Claude call 1: Categorizes articles into gossip vs builders sections
   - Claude call 2: Generates AI Gossip section content (summary, bullets, coffee chat tip)
   - Claude call 3: Generates AI Builders section content (summary, bullets, coffee chat tip)
   - Claude call 4: Generates 1-sentence overall blurb
   - Assembles full HTML email with inline CSS (table-based layout)
   - Returns HTML string

3. tools/send_email.py
   - Authenticates to Gmail API using refresh token (no browser required)
   - Sends HTML email to all RECIPIENT_EMAILS
   - Returns Gmail message ID on success
```

## One-Time Setup (Do Before First Run)

### Step 1: Google Cloud Console

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create project: **Sloan AI Newsletter**
3. Enable **Gmail API** (APIs & Services > Library > search "Gmail API")
4. APIs & Services > **OAuth consent screen**
   - User type: External
   - App name: AI for Sloanies
   - Add your email as test user (under "Test users")
   - Scopes: add `https://www.googleapis.com/auth/gmail.send`
5. APIs & Services > **Credentials** > Create Credentials > OAuth 2.0 Client ID
   - Application type: **Desktop app**
   - Name: Newsletter Local Dev
   - Download JSON → save as `credentials.json` in project root

### Step 2: Run OAuth Flow

```bash
python tools/setup_gmail_oauth.py
```

A browser will open. Log in with the Gmail account that will send newsletters. Grant permission. The terminal will print `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, and `GMAIL_REFRESH_TOKEN`.

### Step 3: Get NewsAPI Key

Sign up for a free account at [newsapi.org](https://newsapi.org). Copy your API key.

### Step 4: Store Secrets in Modal

```bash
# Install Modal
pip install modal

# Authenticate
modal token new
```

Go to [modal.com](https://modal.com) > Secrets > Create New Secret > Custom.
- Name: `newsletter-secrets`
- Add all 7 keys from the table above

### Step 5: Deploy

```bash
modal deploy modal_app.py
```

Verify the schedule in Modal dashboard > Apps > sloan-ai-newsletter > Schedule tab.

## Testing Checklist

Before relying on the Wednesday cron, verify each component:

```bash
# 1. Test scraper (should print 15 articles)
python tools/scrape_news.py

# 2. Test newsletter generation (writes .tmp/newsletter_preview.html)
python tools/generate_newsletter.py
# Open .tmp/newsletter_preview.html in browser to review

# 3. Test email sending (sends a test HTML to RECIPIENT_EMAILS)
python tools/send_email.py

# 4. End-to-end test via Modal (runs full pipeline remotely)
modal run modal_app.py
```

## Failure Handling

| Stage | Failure | Resolution |
|-------|---------|-----------|
| NewsAPI | 429 Rate limit | Automatically falls back to RSS-only. Log appears: "WARNING: NewsAPI rate limit hit." |
| NewsAPI | 401 Invalid key | Check `NEWSAPI_KEY` in secrets. Free tier key is valid for 100 req/day. |
| RSS | Feed timeout | That feed is skipped. Other feeds continue. No action needed unless all feeds fail. |
| Scraper | < 6 articles | `ValueError` raised. Check internet connectivity, verify API key, check if NewsAPI free tier reset. |
| Claude | Malformed JSON | Auto-retried once with explicit JSON-only prompt. If still fails, check for API changes. |
| Claude | 529 Overloaded | Exponential backoff: 1s, 2s, 4s delays. If still failing, Anthropic may have an outage. |
| Gmail | RefreshError | Refresh token expired or revoked. Re-run `tools/setup_gmail_oauth.py` and update Modal secret. |
| Gmail | 403 Forbidden | Gmail API not enabled, or OAuth scope missing. Check Google Cloud Console setup. |
| Modal | Container timeout | 5-minute limit. Typical run is ~60s. If hitting timeout, check for slow RSS feeds or Claude delays. |
| Modal | Secret missing | `KeyError` in logs with the secret name. Add the missing key to `newsletter-secrets` in Modal dashboard. |

## Known Constraints

- **NewsAPI free tier**: 100 requests/day, articles up to 30 days old. We use 1 request per run.
- **Claude model**: Using `claude-sonnet-4-5-20250929`. Good balance of quality and cost (~$0.10-0.20/run).
- **Cron timing**: `0 13 * * 3` = Wednesday 1pm UTC = 8am EST. During EDT (summer), this is 9am ET. Adjust to `0 12 * * 3` if you want strict 8am EDT year-round.
- **Gmail OAuth**: The refresh token does not expire unless you revoke access in Google account settings or the app has been inactive for 6 months on External consent screen without publishing.
- **RSS feed reliability**: VentureBeat and Wired feeds sometimes return empty; NewsAPI compensates.

## Recipient Management

`RECIPIENT_EMAILS` is a comma-separated string in Modal secrets. To add/remove recipients, update the secret value in Modal dashboard. No redeployment needed — the value is read at runtime.

Example value: `you@gmail.com,friend@sloan.mit.edu,classmate@sloan.mit.edu`

## Updating the Newsletter

- **Add new RSS feeds**: Edit `RSS_FEEDS` list in `tools/scrape_news.py`
- **Change Claude model**: Edit `CLAUDE_MODEL` in `tools/generate_newsletter.py`
- **Change email design**: Edit `build_html_email()` in `tools/generate_newsletter.py`
- **Change newsletter tone**: Edit `NEWSLETTER_PERSONA` in `tools/generate_newsletter.py`
- **Change cron schedule**: Edit the `schedule=modal.Cron(...)` in `modal_app.py`, then `modal deploy modal_app.py`
- **Add a new section**: Add a new Claude call in `generate_newsletter()` and a new HTML block in `build_html_email()`
