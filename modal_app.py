"""
modal_app.py

Modal deployment for the AI for Sloanies newsletter.
Runs every Wednesday at 8am ET (1pm UTC).

Setup:
  1. Install Modal: pip install modal
  2. Authenticate: modal token new
  3. Create secrets in Modal dashboard:
     - Secret name: newsletter-secrets
     - Keys: NEWSAPI_KEY, ANTHROPIC_API_KEY, GMAIL_CLIENT_ID,
             GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN,
             GMAIL_SENDER_EMAIL, RECIPIENT_EMAILS
  4. Deploy: modal deploy modal_app.py
  5. Test manually: modal run modal_app.py

Monitoring:
  - View logs: modal app logs sloan-ai-newsletter
  - View scheduled runs: modal.com dashboard > Apps > sloan-ai-newsletter
"""

import os
import sys

import modal

app = modal.App("sloan-ai-newsletter")

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
def send_weekly_newsletter() -> None:
    """
    Weekly newsletter orchestrator. Runs every Wednesday at 8am ET.

    Step 1: Scrape news (NewsAPI + RSS)
    Step 2: Generate newsletter HTML (Claude)
    Step 3: Send email (Gmail API)
    """
    # Add /app to path so tools/ imports work
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
    recipient_emails = [r.strip() for r in os.environ["RECIPIENT_EMAILS"].split(",")]

    print("=" * 50, flush=True)
    print(f"AI for Sloanies Newsletter — {datetime.now().strftime('%B %d, %Y')}", flush=True)
    print("=" * 50, flush=True)

    # Step 1: Scrape news
    print("\n[1/3] Scraping news articles...", flush=True)
    articles = scrape_news(newsapi_key=newsapi_key, target_article_count=15)
    print(f"      Found {len(articles)} articles to work with.", flush=True)

    # Step 2: Generate newsletter
    print("\n[2/3] Generating newsletter with Claude...", flush=True)
    html_body, subject = generate_newsletter(
        anthropic_api_key=anthropic_key,
        articles=articles,
        logo_path="/app/ai_club_logo.jpg",
    )
    print(f"      Newsletter generated ({len(html_body):,} bytes of HTML).", flush=True)
    print(f"      Subject: {subject}", flush=True)

    # Step 3: Send email
    print("\n[3/3] Sending email via Gmail...", flush=True)

    result = send_newsletter(
        client_id=gmail_client_id,
        client_secret=gmail_secret,
        refresh_token=gmail_refresh,
        sender_email=sender_email,
        recipient_emails=recipient_emails,
        subject=subject,
        html_body=html_body,
    )

    print(f"\n{'=' * 50}", flush=True)
    print(f"Newsletter sent! Message ID: {result.get('id', 'unknown')}", flush=True)
    print(f"Recipients: {', '.join(recipient_emails)}", flush=True)
    print("=" * 50, flush=True)


@app.local_entrypoint()
def main() -> None:
    """
    Manual trigger for testing. Run with:
      modal run modal_app.py

    This runs the full pipeline once immediately (not on cron schedule).
    Useful for testing before the first scheduled Wednesday run.
    """
    print("Triggering newsletter manually via Modal...")
    send_weekly_newsletter.remote()
    print("Done! Check your inbox.")
