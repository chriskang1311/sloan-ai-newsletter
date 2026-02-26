# Newsletter Workflow

## Objective
Send a weekly AI/ML newsletter — *AI for Sloanies* — to MIT Sloan MBA students every Wednesday at 8am ET. The newsletter summarizes the week's most relevant AI news in two sections: big-company gossip and early-stage startup activity. A reviewer approves the newsletter before it goes to all subscribers.

## Required Secrets

All stored in Modal under secret name `newsletter-secrets` AND mirrored in `.env` for local dev.

| Key | Source | Notes |
|-----|--------|-------|
| `NEWSAPI_KEY` | newsapi.org free account | 100 req/day limit on free tier |
| `ANTHROPIC_API_KEY` | console.anthropic.com | Powers all Claude calls |
| `GMAIL_SENDER_EMAIL` | The verified From address | Must be verified as a sender in Brevo |
| `BREVO_SMTP_LOGIN` | Your Brevo account | The email you use to log into brevo.com (e.g. `a36599001@smtp-brevo.com`) |
| `BREVO_SMTP_KEY` | Brevo dashboard → Transactional → Email → SMTP & API | The generated SMTP key |
| `RECIPIENT_EMAILS` | Full mailing list | Comma-separated |
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
   - Logo is NOT embedded here — it is attached as a MIME inline image by send_email.py

3. modal_app.py (generate_and_preview)
   - Stores (html, subject, articles) in Modal Dict "newsletter-cache"
   - Sends PREVIEW email to REVIEWER_EMAILS with an orange banner, "Approve & Send" button,
     "Request Changes" button, and "View in browser" link
```

### Stage 2a — Approve & Send (triggered by reviewer clicking "Approve")

```
4. approve_and_send web endpoint (GET)
   - Reviewer clicks "Approve & Send" in the preview email
   - Validates APPROVAL_SECRET_TOKEN; shows a confirmation page (does NOT send yet)
   - This two-step design prevents Outlook SafeLinks / Gmail link scanners from
     accidentally triggering a send when they pre-fetch URLs

5. confirm_and_send web endpoint (GET)
   - Reviewer clicks "Yes, Send to All Subscribers" on the confirmation page
   - Retrieves (html, subject) from Modal Dict
   - Sends newsletter to all RECIPIENT_EMAILS via Brevo SMTP relay (BCC blast)
   - Sets cache["sent"] = True to block duplicate sends
   - Returns a confirmation page
```

### Stage 2b — Request Changes (triggered by reviewer clicking "Request Changes")

```
4. request_changes web endpoint (GET)
   - Shows a feedback form (up to 3 revision rounds per week)
   - On submit: stores feedback in cache, spawns a new Stage 1 run
   - The regenerated newsletter incorporates the feedback via NEWSLETTER_PERSONA
   - Reviewer receives a new preview email (same flow as Stage 1)
```

## One-Time Setup (Do Before First Run)

### Step 1: Set Up Brevo SMTP Relay

Email is sent via [Brevo](https://brevo.com) (formerly Sendinblue) instead of Gmail SMTP. Brevo's sending IPs are trusted by Outlook/Microsoft, preventing newsletters from going to junk.

1. Create a free account at [brevo.com](https://brevo.com) (free tier: 300 emails/day — sufficient)
2. **Verify sender address**: Brevo dashboard → (top-right account menu) → Senders & IPs → Senders → Add `mitsloanaiclub@gmail.com` → confirm the verification email sent to that address
3. **Get SMTP credentials**: Brevo dashboard → Transactional → Email → SMTP & API → Generate new SMTP key
   - Note the **Login** (format: `a36599001@smtp-brevo.com`) and the **Key** (the SMTP password)

### Step 2: Get NewsAPI Key

Sign up for a free account at [newsapi.org](https://newsapi.org). Copy your API key.

### Step 3: Generate Approval Token

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Copy the output — this is your `APPROVAL_SECRET_TOKEN`.

### Step 4: Store Secrets in Modal (initial — without WEBHOOK_URL)

```bash
pip install modal
modal token new
```

Go to [modal.com](https://modal.com) → Secrets → Create New Secret → Custom.
- Name: `newsletter-secrets`
- Add all keys **except `WEBHOOK_URL`** (you'll get that after deploy)

### Step 5: Deploy

```bash
modal deploy modal_app.py
```

After deploying, go to Modal dashboard → Apps → sloan-ai-newsletter → Functions → `approve_and_send`. Copy the web endpoint URL (format: `https://<workspace>--sloan-ai-newsletter-approve-and-send.modal.run`).

### Step 6: Add WEBHOOK_URL to Secrets

Go back to Modal secrets and add:
- `WEBHOOK_URL`: the `approve_and_send` endpoint URL from Step 5

### Step 7: Redeploy

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

# 3. Test email sending (sends a test HTML to RECIPIENT_EMAILS via Brevo)
python tools/send_email.py
# Check that the email arrives in inbox (not junk) for Outlook recipients

# 4. Test Stage 1 via Modal (generates newsletter, sends preview to REVIEWER_EMAILS)
modal run modal_app.py
# Check reviewer inbox for preview email with orange banner and approve/decline buttons
# Click "Approve & Send" → verify confirmation page appears
# Click "Yes, Send to All Subscribers" → verify subscribers receive the newsletter
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
| Brevo | SMTPAuthenticationError | Check `BREVO_SMTP_KEY` and `BREVO_SMTP_LOGIN` in secrets. Regenerate key in Brevo dashboard if needed. |
| Brevo | Sender not verified | Add `GMAIL_SENDER_EMAIL` as a verified sender in Brevo dashboard → Senders & IPs → Senders. |
| Approval | No cached newsletter | Stage 1 hasn't run yet. Re-run `modal run modal_app.py`. |
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
- **Brevo free tier**: 300 emails/day. Sufficient for the newsletter; upgrade if subscriber list grows beyond ~250.
- **Deliverability note**: Email is sent from `mitsloanaiclub@gmail.com` via Brevo's relay. Brevo's IPs are trusted by Outlook. For full DMARC alignment, a custom domain (e.g. `newsletter@mitsloanaiclub.com`) would be the long-term improvement.
- **Cron timing**: `0 13 * * 3` = Wednesday 1pm UTC = 8am EST. During EDT (summer), this is 9am ET. Adjust to `0 12 * * 3` if you want strict 8am EDT year-round.
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
- **Change cron schedule**: Edit `schedule=modal.Cron(...)` in `modal_app.py`, then `modal deploy modal_app.py`
- **Rotate Brevo SMTP key**: Generate a new key in Brevo dashboard → update `BREVO_SMTP_KEY` in Modal secrets (no redeployment needed)
