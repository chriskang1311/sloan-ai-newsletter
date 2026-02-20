"""
modal_app.py

Modal deployment for the AI for Sloanies newsletter.
Two-stage pipeline with human-in-the-loop review:

  Stage 1 — generate_and_preview() [runs on cron every Wednesday at 8am ET]:
    - Scrapes news, generates newsletter HTML via Claude
    - Stores the newsletter in a Modal Dict (persistent cache)
    - Sends a PREVIEW email to REVIEWER_EMAILS with an "Approve & Send" button

  Stage 2 — approve_and_send() [web endpoint, triggered by reviewer clicking the button]:
    - Validates the approval token from the URL
    - Retrieves the cached newsletter
    - Sends to all RECIPIENT_EMAILS
    - Returns a confirmation page

Setup:
  1. Install Modal: pip install modal
  2. Authenticate: modal token new
  3. Create secrets in Modal dashboard:
     - Secret name: newsletter-secrets
     - Keys: NEWSAPI_KEY, ANTHROPIC_API_KEY, GMAIL_CLIENT_ID,
             GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN, GMAIL_SENDER_EMAIL,
             RECIPIENT_EMAILS, REVIEWER_EMAILS, APPROVAL_SECRET_TOKEN, WEBHOOK_URL
  4. Deploy: modal deploy modal_app.py
  5. After deploying, copy the approve_and_send endpoint URL from Modal dashboard
     and add it as WEBHOOK_URL in your newsletter-secrets.
  6. Test Stage 1 manually: modal run modal_app.py
     Then click the approval link in the preview email to test Stage 2.

Monitoring:
  - View logs: modal app logs sloan-ai-newsletter
  - View scheduled runs: modal.com dashboard > Apps > sloan-ai-newsletter
"""

import os
import re
import sys

import modal

app = modal.App("sloan-ai-newsletter")

# Persistent Dict — stores the latest generated newsletter between Stage 1 and Stage 2.
# Accessible from any Modal function in this workspace.
newsletter_cache = modal.Dict.from_name("newsletter-cache", create_if_missing=True)

# Container image — installs all Python dependencies
newsletter_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install([
        "anthropic>=0.40.0",
        "requests>=2.31.0",
        "feedparser>=6.0.11",
        "google-auth>=2.27.0",
        "google-auth-oauthlib>=1.2.0",
        "google-api-python-client>=2.118.0",
        "python-dateutil>=2.9.0",
        "fastapi>=0.100.0",   # Required for web endpoint HTML responses
    ])
    # Mount the tools directory into the container
    .add_local_dir("tools", remote_path="/app/tools")
    .add_local_file("ai_club_logo.jpg", remote_path="/app/ai_club_logo.jpg")
)

# Reference to the Modal secrets (configure at modal.com/secrets)
newsletter_secrets = modal.Secret.from_name("newsletter-secrets")


@app.function(
    image=newsletter_image,
    secrets=[newsletter_secrets],
    schedule=modal.Cron("0 13 * * 3"),  # Wednesday 13:00 UTC = 8:00am EST
    timeout=300,  # 5 minutes — plenty for scrape + Claude + send
    retries=modal.Retries(max_retries=1, backoff_coefficient=1.0, initial_delay=30.0),
)
def generate_and_preview() -> None:
    """
    Stage 1: Generate newsletter and send preview to reviewer for approval.
    Runs automatically every Wednesday at 8am ET.

    Step 1: Scrape news (NewsAPI + RSS)
    Step 2: Generate newsletter HTML (Claude)
    Step 3: Cache newsletter in Modal Dict
    Step 4: Send preview email with approval button to REVIEWER_EMAILS
    """
    sys.path.insert(0, "/app")

    from tools.scrape_news import scrape_news
    from tools.generate_newsletter import generate_newsletter
    from tools.send_email import send_newsletter
    from datetime import datetime

    # Read secrets from environment (injected by Modal)
    newsapi_key      = os.environ["NEWSAPI_KEY"]
    anthropic_key    = os.environ["ANTHROPIC_API_KEY"]
    gmail_client_id  = os.environ["GMAIL_CLIENT_ID"]
    gmail_secret     = os.environ["GMAIL_CLIENT_SECRET"]
    gmail_refresh    = os.environ["GMAIL_REFRESH_TOKEN"]
    sender_email     = os.environ["GMAIL_SENDER_EMAIL"]
    reviewer_emails  = [r.strip() for r in os.environ["REVIEWER_EMAILS"].split(",")]
    webhook_url      = os.environ["WEBHOOK_URL"]
    approval_token   = os.environ["APPROVAL_SECRET_TOKEN"]

    print("=" * 55, flush=True)
    print(f"AI for Sloanies — {datetime.now().strftime('%B %d, %Y')}", flush=True)
    print("STAGE 1: Generating newsletter and sending preview", flush=True)
    print("=" * 55, flush=True)

    # Step 1: Scrape news
    print("\n[1/4] Scraping news articles...", flush=True)
    articles = scrape_news(newsapi_key=newsapi_key, target_article_count=30, lookback_days=6)
    print(f"      Found {len(articles)} articles to work with.", flush=True)

    # Step 2: Generate newsletter
    print("\n[2/4] Generating newsletter with Claude...", flush=True)
    html_body, subject = generate_newsletter(
        anthropic_api_key=anthropic_key,
        articles=articles,
        logo_path="/app/ai_club_logo.jpg",
    )
    print(f"      Newsletter generated ({len(html_body):,} bytes of HTML).", flush=True)
    print(f"      Subject: {subject}", flush=True)

    # Step 3: Cache newsletter for the approval step
    print("\n[3/4] Caching newsletter for approval...", flush=True)
    newsletter_cache["latest"] = {"html": html_body, "subject": subject}
    print("      Cached successfully.", flush=True)

    # Step 4: Build preview HTML with reviewer banner and send
    print("\n[4/4] Building preview and sending to reviewer...", flush=True)
    approve_url = f"{webhook_url}?token={approval_token}"
    preview_banner = (
        '<div style="background-color:#e65100;color:white;padding:20px 30px;'
        'text-align:center;font-family:Arial,Helvetica,sans-serif;">'
        '<p style="margin:0 0 8px 0;font-size:17px;font-weight:bold;">'
        '&#9888; REVIEWER PREVIEW &mdash; Not yet sent to subscribers'
        '</p>'
        '<p style="margin:0 0 16px 0;font-size:14px;line-height:1.5;">'
        'Review the newsletter below. Click the button to approve and send to all subscribers.'
        '</p>'
        '<a href="' + approve_url + '" '
        'style="display:inline-block;background-color:#2e7d32;color:white;'
        'padding:14px 32px;font-size:16px;font-weight:bold;text-decoration:none;'
        'border-radius:4px;">&#10003; Approve &amp; Send to All Subscribers</a>'
        '</div>'
    )
    preview_html = re.sub(r'(<body[^>]*>)', r'\1' + preview_banner, html_body, count=1)

    result = send_newsletter(
        client_id=gmail_client_id,
        client_secret=gmail_secret,
        refresh_token=gmail_refresh,
        sender_email=sender_email,
        recipient_emails=reviewer_emails,
        subject=f"[PREVIEW] {subject}",
        html_body=preview_html,
    )

    print(f"\n{'=' * 55}", flush=True)
    print(f"Preview sent! Message ID: {result.get('id', 'unknown')}", flush=True)
    print(f"Reviewer(s): {', '.join(reviewer_emails)}", flush=True)
    print(f"Approval URL: {approve_url}", flush=True)
    print("=" * 55, flush=True)


@app.function(
    image=newsletter_image,
    secrets=[newsletter_secrets],
    timeout=120,
)
@modal.fastapi_endpoint(method="GET")
def approve_and_send(token: str = "") -> None:
    """
    Stage 2: Approve and send newsletter to all subscribers.
    Triggered when the reviewer clicks the "Approve & Send" button in the preview email.

    Validates the token, retrieves the cached newsletter, sends to RECIPIENT_EMAILS,
    and returns an HTML confirmation page.
    """
    sys.path.insert(0, "/app")

    from tools.send_email import send_newsletter
    from fastapi.responses import HTMLResponse

    approval_token = os.environ["APPROVAL_SECRET_TOKEN"]

    # Security: validate token
    if not token or token != approval_token:
        return HTMLResponse(
            content=(
                '<!DOCTYPE html><html><head><meta charset="UTF-8">'
                '<title>Unauthorized</title></head>'
                '<body style="font-family:Arial,Helvetica,sans-serif;text-align:center;'
                'padding:60px 20px;background:#f0f2f5;">'
                '<div style="max-width:480px;margin:0 auto;background:white;padding:40px;'
                'border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">'
                '<h1 style="color:#dc3545;margin-bottom:16px;">&#10060; Unauthorized</h1>'
                '<p style="font-size:15px;color:#555;">Invalid or missing approval token.</p>'
                '</div></body></html>'
            ),
            status_code=403,
        )

    # Retrieve cached newsletter
    cached = newsletter_cache.get("latest")
    if not cached:
        return HTMLResponse(
            content=(
                '<!DOCTYPE html><html><head><meta charset="UTF-8">'
                '<title>No Newsletter Found</title></head>'
                '<body style="font-family:Arial,Helvetica,sans-serif;text-align:center;'
                'padding:60px 20px;background:#f0f2f5;">'
                '<div style="max-width:480px;margin:0 auto;background:white;padding:40px;'
                'border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">'
                '<h1 style="color:#dc3545;margin-bottom:16px;">&#10060; No Newsletter Cached</h1>'
                '<p style="font-size:15px;color:#555;">No newsletter is waiting for approval. '
                'Has the preview (Stage 1) run yet this week?</p>'
                '</div></body></html>'
            ),
            status_code=404,
        )

    html_body = cached["html"]
    subject   = cached["subject"]

    gmail_client_id  = os.environ["GMAIL_CLIENT_ID"]
    gmail_secret     = os.environ["GMAIL_CLIENT_SECRET"]
    gmail_refresh    = os.environ["GMAIL_REFRESH_TOKEN"]
    sender_email     = os.environ["GMAIL_SENDER_EMAIL"]
    recipient_emails = [r.strip() for r in os.environ["RECIPIENT_EMAILS"].split(",")]

    print(f"Approval received. Sending to {len(recipient_emails)} recipient(s)...", flush=True)

    result = send_newsletter(
        client_id=gmail_client_id,
        client_secret=gmail_secret,
        refresh_token=gmail_refresh,
        sender_email=sender_email,
        recipient_emails=recipient_emails,
        subject=subject,
        html_body=html_body,
    )

    msg_id = result.get("id", "unknown")
    n = len(recipient_emails)
    print(f"Newsletter sent! ID: {msg_id}. Recipients: {', '.join(recipient_emails)}", flush=True)

    return HTMLResponse(content=(
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<title>Newsletter Sent</title></head>'
        '<body style="font-family:Arial,Helvetica,sans-serif;text-align:center;'
        'padding:60px 20px;background:#f0f2f5;">'
        '<div style="max-width:500px;margin:0 auto;background:white;padding:40px;'
        'border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">'
        '<h1 style="color:#2e7d32;margin-bottom:16px;">&#10003; Newsletter Sent!</h1>'
        f'<p style="font-size:16px;color:#333;margin-bottom:12px;">Sent to <strong>{n}</strong> subscriber(s).</p>'
        f'<p style="font-size:14px;color:#666;margin-bottom:8px;"><strong>Subject:</strong> {subject}</p>'
        f'<p style="font-size:12px;color:#999;">Message ID: {msg_id}</p>'
        '</div></body></html>'
    ))


@app.local_entrypoint()
def main() -> None:
    """
    Manual trigger for testing Stage 1. Run with:
      modal run modal_app.py

    This generates the newsletter and sends a preview to REVIEWER_EMAILS.
    After reviewing, click the approval link in the preview email to trigger Stage 2
    (send to all RECIPIENT_EMAILS).
    """
    print("Triggering Stage 1 (generate & preview) via Modal...")
    generate_and_preview.remote()
    print("Done! Check the reviewer inbox for the preview email.")
