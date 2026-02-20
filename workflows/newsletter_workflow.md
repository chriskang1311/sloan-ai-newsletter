# Newsletter Workflow

## Objective
Send a weekly AI/ML newsletter — *AI for Sloanies* — to MIT Sloan MBA students every Wednesday at 8am ET. The newsletter summarizes the week's most relevant AI news in two sections: big-company gossip and early-stage startup activity. A reviewer approves the newsletter before it goes to all subscribers.

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
| `RECIPIENT_EMAILS` | Full mailing list | Comma-separated, no spaces |
| `REVIEWER_EMAILS` | Leadership reviewer(s) | Comma-separated; receives preview before full send |
| `APPROVAL_SECRET_TOKEN` | Any long random string | Secures the approval webhook; generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `WEBHOOK_URL` | Modal dashboard (after first deploy) | URL of the `approve_and_send` endpoint; see setup steps |

## Execution Sequence

### Stage 1 — Generate & Preview (automatic, every Wednesday 8am ET)

```
1. tools/scrape_news.py
   - Fetches up to 30 articles from the past 6 days via NewsAPI + 5 RSS feeds
   - Three-layer deduplication: exact URL match → title similarity (0.85) → topic keyword overlap (0.45)
   - Returns list of article dicts sorted by date (newest first)
   - Raises ValueError if fewer than 6 articles found

2. tools/generate_newsletter.py
   - Claude call 1: Categorizes articles into gossip vs builders sections
     • Gossip: selects the most conversation-worthy, widely-covered, dramatic stories
     • Builders: ONLY early-stage startups (Seed, Series A, Series B) — no Series C+, no IPOs
   - Claude call 2: Generates AI Gossip section (drama-forward tone)
   - Claude call 3: Generates AI Builders section (early-stage focus)
   - Claude call 4: Generates subject line and opening hook
   - Assembles full HTML email with inline CSS and footer disclaimer

3. modal_app.py (generate_and_preview)
   - Stores (html, subject) in Modal Dict "newsletter-cache"
   - Sends PREVIEW email to REVIEWER_EMAILS with an "Approve & Send" button
```

### Stage 2 — Approve & Send (triggered by reviewer clicking the button)

```
4. approve_and_send web endpoint
   - Reviewer clicks "Approve & Send" button in the preview email
   - Endpoint validates the APPROVAL_SECRET_TOKEN from the URL
   - Retrieves (html, subject) from Modal Dict
   - Sends newsletter to all RECIPIENT_EMAILS via Gmail API
   - Returns a confirmation page in the reviewer's browser
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

### Step 4: Generate Approval Token

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the output — this is your `APPROVAL_SECRET_TOKEN`.

### Step 5: Store Secrets in Modal (initial — without WEBHOOK_URL)

```bash
pip install modal
modal token new
```

Go to [modal.com](https://modal.com) > Secrets > Create New Secret > Custom.
- Name: `newsletter-secrets`
- Add all keys **except `WEBHOOK_URL`** (you'll get that after deploy)

### Step 6: Deploy

```bash
modal deploy modal_app.py
```

After deploying, go to Modal dashboard > Apps > sloan-ai-newsletter > Functions > `approve_and_send`. Copy the web endpoint URL (it looks like `https://<workspace>--sloan-ai-newsletter-approve-and-send.modal.run`).

### Step 7: Add WEBHOOK_URL to Secrets

Go back to Modal secrets and add:
- `WEBHOOK_URL`: the `approve_and_send` endpoint URL from Step 6

### Step 8: Redeploy

```bash
modal deploy modal_app.py
```

## Testing Checklist

Before relying on the Wednesday cron, verify each component:

```bash
# 1. Test scraper (should print ~20-30 articles from the past 6 days)
python tools/scrape_news.py

# 2. Test newsletter generation (writes .tmp/newsletter_preview.html)
python tools/generate_newsletter.py
# Open .tmp/newsletter_preview.html in browser to review
# Verify: gossip stories feel dramatic/conversation-worthy
# Verify: all builders stories are Seed/Series A/Series B companies
# Verify: footer shows AI disclaimer

# 3. Test email sending (sends a test HTML to RECIPIENT_EMAILS)
python tools/send_email.py

# 4. Test Stage 1 via Modal (generates newsletter, sends preview to REVIEWER_EMAILS)
modal run modal_app.py
# Check reviewer inbox for preview email with orange banner and green "Approve" button
# Click the button → verify subscribers receive the newsletter
# Verify confirmation page appears in browser
```

## Failure Handling

| Stage | Failure | Resolution |
|-------|---------|-----------|
| NewsAPI | 429 Rate limit | Automatically falls back to RSS-only. Log: "WARNING: NewsAPI rate limit hit." |
| NewsAPI | 401 Invalid key | Check `NEWSAPI_KEY` in secrets. |
| RSS | Feed timeout | That feed is skipped. Others continue. |
| Scraper | < 6 articles | `ValueError` raised. Check connectivity and API key. |
| Claude | Malformed JSON | Auto-retried once with explicit JSON-only prompt. |
| Claude | 529 Overloaded | Exponential backoff: 1s, 2s, 4s delays. |
| Gmail | RefreshError | Re-run `tools/setup_gmail_oauth.py` and update Modal secret. |
| Gmail | 403 Forbidden | Gmail API not enabled or OAuth scope missing. Check Google Cloud Console. |
| Approval | No cached newsletter | Stage 1 hasn't run yet, or cache expired. Re-run `modal run modal_app.py`. |
| Approval | Invalid token | Wrong or missing `APPROVAL_SECRET_TOKEN` in URL. Check Modal secrets. |
| Modal | Container timeout | 5-min limit. Typical run ~60s. Check for slow RSS or Claude delays. |
| Modal | Secret missing | `KeyError` in logs. Add the missing key to `newsletter-secrets` in Modal dashboard. |

## Newsletter Section Rules

### AI Gossip: Hot Takes & Hallucinations
- Must be widely covered, surprising, or controversial stories
- Focus: executive drama, regulatory battles, power plays by big AI companies
- Goal: articles people will ACTUALLY bring up in networking conversations

### AI Builders: Startups You'll Pretend You Already Knew About
- **Strict constraint**: only Seed, Series A, or Series B funding stages
- No Series C+, IPOs, or product launches from established tech companies
- Focus: what scrappy early-stage founders are building and why investors are betting on them

## Known Constraints

- **NewsAPI free tier**: 100 requests/day, articles up to 30 days old. We use 1 request per run.
- **Claude model**: Using `claude-sonnet-4-5-20250929`. Good balance of quality and cost (~$0.10-0.20/run).
- **Cron timing**: `0 13 * * 3` = Wednesday 1pm UTC = 8am EST. During EDT (summer), this is 9am ET. Adjust to `0 12 * * 3` if you want strict 8am EDT year-round.
- **Gmail OAuth**: The refresh token does not expire unless you revoke access or the app is inactive for 6 months.
- **RSS feed reliability**: VentureBeat and Wired feeds sometimes return empty; NewsAPI compensates.
- **Modal Dict**: The newsletter cache persists until overwritten by the next week's Stage 1 run. If Stage 2 is never triggered, the old newsletter remains cached (harmless).

## Recipient Management

`RECIPIENT_EMAILS` is a comma-separated string in Modal secrets. To add/remove recipients, update the secret value in Modal dashboard. No redeployment needed — the value is read at runtime.

`REVIEWER_EMAILS` follows the same format. Typically just one reviewer (e.g., the club president).

## Updating the Newsletter

- **Add new RSS feeds**: Edit `RSS_FEEDS` list in `tools/scrape_news.py`
- **Change Claude model**: Edit `CLAUDE_MODEL` in `tools/generate_newsletter.py`
- **Change email design**: Edit `build_html_email()` in `tools/generate_newsletter.py`
- **Change newsletter tone**: Edit `NEWSLETTER_PERSONA` in `tools/generate_newsletter.py`
- **Change cron schedule**: Edit the `schedule=modal.Cron(...)` in `modal_app.py`, then `modal deploy modal_app.py`
- **Skip approval and send directly**: Remove the reviewer step from `generate_and_preview()` and send directly to `RECIPIENT_EMAILS`
