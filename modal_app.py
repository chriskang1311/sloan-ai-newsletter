"""
modal_app.py

Modal deployment for the AI for Sloanies newsletter.
Two-stage pipeline with human-in-the-loop review:

  Stage 1 — generate_and_preview() [runs on cron every Wednesday at 8am ET]:
    - Scrapes news, generates newsletter HTML via Claude
    - Stores the newsletter (HTML, subject, articles) in a Modal Dict (persistent cache)
    - Sends a PREVIEW email to REVIEWER_EMAILS with "Approve & Send" and "Request Changes" buttons

  Stage 2a — approve_and_send() [web endpoint, triggered by reviewer clicking "Approve"]:
    - Validates the approval token from the URL
    - Retrieves the cached newsletter (idempotent — ignores duplicate clicks)
    - Sends to all RECIPIENT_EMAILS
    - Returns a confirmation page

  Stage 2b — request_changes() [web endpoint, triggered by reviewer clicking "Request Changes"]:
    - Validates the approval token from the URL
    - Shows a feedback form when no comments are present
    - When comments are submitted, stores them in the cache and re-triggers Stage 1
    - The regenerated newsletter incorporates the reviewer's feedback

Setup:
  1. Install Modal: pip install modal
  2. Authenticate: modal token new
  3. Set up Brevo SMTP relay for deliverability (prevents Outlook junk filtering):
       a. Create a free account at brevo.com
       b. Verify sender: Brevo dashboard → Senders & IPs → Senders → Add sender email
       c. Get SMTP key: Brevo dashboard → Transactional → Email → SMTP & API → Generate key
  4. Create secrets in Modal dashboard:
     - Secret name: newsletter-secrets
     - Required keys:
         NEWSAPI_KEY, ANTHROPIC_API_KEY
         GMAIL_SENDER_EMAIL     — the verified From address (e.g. mitsloanaiclub@gmail.com)
         BREVO_SMTP_LOGIN       — the email you use to log into Brevo
         BREVO_SMTP_KEY         — the SMTP key from Brevo dashboard
         RECIPIENT_EMAILS       — comma-separated subscriber list
         REVIEWER_EMAILS        — comma-separated reviewer list
         APPROVAL_SECRET_TOKEN  — any long random string (acts as a shared secret for button links)
         WEBHOOK_URL            — approve_and_send endpoint URL (add after step 5)
     - Optional keys (add after step 5 if Modal changes the auto-derived URLs):
         DECLINE_WEBHOOK_URL    — request_changes endpoint URL
         BROWSER_PREVIEW_URL    — browser_preview endpoint URL
  5. Deploy: modal deploy modal_app.py
  6. After deploying, copy the approve_and_send endpoint URL from Modal dashboard
     and add it as WEBHOOK_URL in your newsletter-secrets.
  7. Test Stage 1 manually: modal run modal_app.py
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
    gmail_sender     = os.environ["GMAIL_SENDER_EMAIL"]
    smtp_password    = os.environ["BREVO_SMTP_KEY"]
    smtp_login       = os.environ["BREVO_SMTP_LOGIN"]
    reviewer_emails  = [r.strip() for r in os.environ["REVIEWER_EMAILS"].split(",")]
    webhook_url      = os.environ["WEBHOOK_URL"]
    approval_token   = os.environ["APPROVAL_SECRET_TOKEN"]

    # Endpoint URLs — prefer explicit secrets, fall back to derivation from WEBHOOK_URL.
    # Modal URL pattern: https://workspace--app-name-function-name.modal.run
    decline_base_url = (
        os.environ.get("DECLINE_WEBHOOK_URL")
        or webhook_url.replace("approve-and-send", "request-changes")
    )
    preview_base_url = (
        os.environ.get("BROWSER_PREVIEW_URL")
        or webhook_url.replace("approve-and-send", "browser-preview")
    )

    print("=" * 55, flush=True)
    print(f"AI for Sloanies — {datetime.now().strftime('%B %d, %Y')}", flush=True)
    print("STAGE 1: Generating newsletter and sending preview", flush=True)
    print("=" * 55, flush=True)

    # Check for reviewer feedback from a previous decline
    reviewer_feedback = newsletter_cache.get("feedback", "")
    if reviewer_feedback:
        print(f"\n  Reviewer feedback found. Will incorporate into generation.", flush=True)
        try:
            del newsletter_cache["feedback"]
        except KeyError:
            pass
    else:
        # Fresh cron run — reset the revision counter so the reviewer gets a clean slate
        try:
            del newsletter_cache["revision_count"]
        except KeyError:
            pass

    # Step 1: Scrape news (or reuse cached articles on a feedback-triggered re-run)
    print("\n[1/4] Scraping / loading articles...", flush=True)
    if reviewer_feedback:
        prior = newsletter_cache.get("latest", {})
        articles = prior.get("articles", [])
        if articles:
            print(f"      Reusing {len(articles)} cached articles from previous run.", flush=True)
        else:
            print("      No cached articles found — scraping fresh.", flush=True)
            articles = scrape_news(newsapi_key=newsapi_key, target_article_count=30, lookback_days=6)
    else:
        articles = scrape_news(newsapi_key=newsapi_key, target_article_count=30, lookback_days=6)
    print(f"      {len(articles)} articles ready.", flush=True)

    # Step 2: Generate newsletter
    print("\n[2/4] Generating newsletter with Claude...", flush=True)
    html_body, subject = generate_newsletter(
        anthropic_api_key=anthropic_key,
        articles=articles,
        reviewer_feedback=reviewer_feedback,
    )
    print(f"      Newsletter generated ({len(html_body):,} bytes of HTML).", flush=True)
    print(f"      Subject: {subject}", flush=True)

    # Step 3: Cache newsletter + articles for the approval step (reset sent flag)
    print("\n[3/4] Caching newsletter for approval...", flush=True)
    newsletter_cache["latest"] = {
        "html": html_body,
        "subject": subject,
        "sent": False,
        "articles": articles,  # Preserved so a feedback re-run can reuse the same article pool
    }
    print("      Cached successfully.", flush=True)

    # Step 4: Build preview HTML with reviewer banner and send
    print("\n[4/4] Building preview and sending to reviewer...", flush=True)
    approve_url = f"{webhook_url}?token={approval_token}"
    decline_url = f"{decline_base_url}?token={approval_token}"
    preview_url = f"{preview_base_url}?token={approval_token}"
    preview_banner = (
        '<div style="background-color:#e65100;color:white;padding:20px 30px;'
        'text-align:center;font-family:Arial,Helvetica,sans-serif;">'
        '<p style="margin:0 0 4px 0;font-size:17px;font-weight:bold;">'
        '&#9888; REVIEWER PREVIEW &mdash; Not yet sent to subscribers'
        '</p>'
        '<p style="margin:0 0 4px 0;font-size:12px;opacity:0.85;">'
        '<a href="' + preview_url + '" style="color:white;text-decoration:underline;">'
        'View newsletter in browser &rarr;</a>'
        '</p>'
        '<p style="margin:0 0 16px 0;font-size:14px;line-height:1.5;">'
        'Review the newsletter below. Approve to send to all subscribers, '
        'or request changes with feedback.'
        '</p>'
        '<a href="' + approve_url + '" '
        'style="display:inline-block;background-color:#2e7d32;color:white;'
        'padding:14px 32px;font-size:16px;font-weight:bold;text-decoration:none;'
        'border-radius:4px;margin-right:12px;">&#10003; Approve &amp; Send to All Subscribers</a>'
        '<a href="' + decline_url + '" '
        'style="display:inline-block;background-color:#b71c1c;color:white;'
        'padding:14px 32px;font-size:16px;font-weight:bold;text-decoration:none;'
        'border-radius:4px;">&#10007; Request Changes</a>'
        '</div>'
    )
    preview_html = re.sub(r'(<body[^>]*>)', r'\1' + preview_banner, html_body, count=1)

    with open("/app/ai_club_logo.jpg", "rb") as f:
        logo_bytes = f.read()

    result = send_newsletter(
        sender_email=gmail_sender,
        smtp_password=smtp_password,
        smtp_login=smtp_login,
        recipient_emails=reviewer_emails,
        subject=f"[PREVIEW] {subject}",
        html_body=preview_html,
        inline_images={"logo": logo_bytes},
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
    Stage 2a (GET): Reviewer clicks "Approve" in the preview email.

    Shows a confirmation page — does NOT send the newsletter. This prevents email clients
    (Outlook SafeLinks, Gmail link-scanner) from accidentally triggering a send when they
    pre-fetch URLs in the preview email. The actual send happens in confirm_and_send (POST).
    """
    from fastapi.responses import HTMLResponse

    approval_token = os.environ["APPROVAL_SECRET_TOKEN"]

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

    if cached.get("sent"):
        subject = cached.get("subject", "")
        return HTMLResponse(
            content=(
                '<!DOCTYPE html><html><head><meta charset="UTF-8">'
                '<title>Already Sent</title></head>'
                '<body style="font-family:Arial,Helvetica,sans-serif;text-align:center;'
                'padding:60px 20px;background:#f0f2f5;">'
                '<div style="max-width:500px;margin:0 auto;background:white;padding:40px;'
                'border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">'
                '<h1 style="color:#f57c00;margin-bottom:16px;">&#8505; Already Sent</h1>'
                '<p style="font-size:15px;color:#555;margin-bottom:12px;">'
                'This newsletter has already been sent to subscribers.</p>'
                f'<p style="font-size:14px;color:#666;"><strong>Subject:</strong> {subject}</p>'
                '</div></body></html>'
            ),
            status_code=200,
        )

    subject = cached["subject"]
    n = len([r for r in os.environ["RECIPIENT_EMAILS"].split(",") if r.strip()])
    confirm_url = os.environ["WEBHOOK_URL"].replace("approve-and-send", "confirm-and-send")

    return HTMLResponse(content=(
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<title>Confirm Send</title></head>'
        '<body style="font-family:Arial,Helvetica,sans-serif;text-align:center;'
        'padding:60px 20px;background:#f0f2f5;">'
        '<div style="max-width:520px;margin:0 auto;background:white;padding:40px;'
        'border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">'
        '<h1 style="color:#1a1a2e;margin-bottom:12px;">&#9993; Ready to Send</h1>'
        f'<p style="font-size:15px;color:#333;margin-bottom:6px;"><strong>Subject:</strong> {subject}</p>'
        f'<p style="font-size:15px;color:#555;margin-bottom:28px;">This will send to <strong>{n}</strong> subscriber(s). This action cannot be undone.</p>'
        f'<a href="{confirm_url}?token={token}" '
        'style="display:inline-block;background-color:#2e7d32;color:white;'
        'padding:16px 40px;font-size:17px;font-weight:bold;text-decoration:none;'
        'border-radius:4px;">&#10003; Yes, Send to All Subscribers</a>'
        '<p style="margin-top:20px;font-size:12px;color:#aaa;">This link only appears in your browser — email clients cannot trigger it.</p>'
        '</div></body></html>'
    ))


@app.function(
    image=newsletter_image,
    secrets=[newsletter_secrets],
    timeout=120,
)
@modal.fastapi_endpoint(method="GET")
def confirm_and_send(token: str = "") -> None:
    """
    Stage 2b (GET): Actually sends the newsletter to all subscribers.
    Triggered by the reviewer clicking "Yes, Send" on the confirmation page from approve_and_send.
    This URL is only ever shown in the browser (never in an email), so email client
    pre-fetchers cannot reach it.
    """
    sys.path.insert(0, "/app")

    from tools.send_email import send_newsletter
    from fastapi.responses import HTMLResponse

    approval_token = os.environ["APPROVAL_SECRET_TOKEN"]

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
                '<p style="font-size:15px;color:#555;">No newsletter is waiting for approval.</p>'
                '</div></body></html>'
            ),
            status_code=404,
        )

    # Idempotency guard
    if cached.get("sent"):
        subject = cached.get("subject", "")
        return HTMLResponse(
            content=(
                '<!DOCTYPE html><html><head><meta charset="UTF-8">'
                '<title>Already Sent</title></head>'
                '<body style="font-family:Arial,Helvetica,sans-serif;text-align:center;'
                'padding:60px 20px;background:#f0f2f5;">'
                '<div style="max-width:500px;margin:0 auto;background:white;padding:40px;'
                'border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">'
                '<h1 style="color:#f57c00;margin-bottom:16px;">&#8505; Already Sent</h1>'
                '<p style="font-size:15px;color:#555;margin-bottom:12px;">'
                'This newsletter has already been sent to subscribers.</p>'
                f'<p style="font-size:14px;color:#666;"><strong>Subject:</strong> {subject}</p>'
                '</div></body></html>'
            ),
            status_code=200,
        )

    html_body = cached["html"]
    subject   = cached["subject"]

    gmail_sender     = os.environ["GMAIL_SENDER_EMAIL"]
    smtp_password    = os.environ["BREVO_SMTP_KEY"]
    smtp_login       = os.environ["BREVO_SMTP_LOGIN"]
    recipient_emails = [r.strip() for r in os.environ["RECIPIENT_EMAILS"].split(",")]
    n = len(recipient_emails)

    print(f"Approval confirmed. Sending to {n} recipient(s) via BCC...", flush=True)

    with open("/app/ai_club_logo.jpg", "rb") as f:
        logo_bytes = f.read()

    result = send_newsletter(
        sender_email=gmail_sender,
        smtp_password=smtp_password,
        smtp_login=smtp_login,
        recipient_emails=recipient_emails,
        subject=subject,
        html_body=html_body,
        bcc=True,
        inline_images={"logo": logo_bytes},
    )

    msg_id = result.get("id", "unknown")
    print(f"Newsletter sent! ID: {msg_id}. Recipients: {', '.join(recipient_emails)}", flush=True)

    newsletter_cache["latest"] = {**cached, "sent": True}

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


@app.function(
    image=newsletter_image,
    secrets=[newsletter_secrets],
    timeout=120,
)
@modal.fastapi_endpoint(method="GET")
def request_changes(token: str = "", comments: str = "") -> None:
    """
    Stage 2b: Decline the newsletter and optionally submit reviewer feedback.

    GET with no comments → shows the feedback form.
    GET with comments → stores feedback, triggers Stage 1 regeneration, confirms to reviewer.

    The "Request Changes" button in the preview email points here with just the token.
    The form on this page submits back to this same URL with the token + comments.
    """
    from fastapi.responses import HTMLResponse

    approval_token = os.environ["APPROVAL_SECRET_TOKEN"]

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

    MAX_REVISIONS = 3
    revision_count = newsletter_cache.get("revision_count", 0)

    if not comments.strip():
        # Hit the revision cap — tell reviewer to contact admin instead of showing form
        if revision_count >= MAX_REVISIONS:
            return HTMLResponse(content=(
                '<!DOCTYPE html><html><head><meta charset="UTF-8">'
                '<title>Maximum Revisions Reached</title></head>'
                '<body style="font-family:Arial,Helvetica,sans-serif;text-align:center;'
                'padding:60px 20px;background:#f0f2f5;">'
                '<div style="max-width:520px;margin:0 auto;background:white;padding:40px;'
                'border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">'
                '<h1 style="color:#e65100;margin-bottom:16px;">&#9888; Maximum Revisions Reached</h1>'
                f'<p style="font-size:15px;color:#555;margin-bottom:12px;">'
                f'This newsletter has been revised {revision_count} time(s) already. '
                f'Please approve the current version or contact the newsletter admin to '
                f'trigger a fresh run next week.</p>'
                '</div></body></html>'
            ))

        # Show the feedback form — action submits back to this same URL
        form_action = f"?token={token}"
        revisions_left = MAX_REVISIONS - revision_count
        return HTMLResponse(content=(
            '<!DOCTYPE html><html><head><meta charset="UTF-8">'
            '<title>Request Newsletter Changes</title>'
            '<style>'
            'body{margin:0;padding:40px 16px;background:#f0f2f5;'
            'font-family:Arial,Helvetica,sans-serif;}'
            '.card{max-width:540px;margin:0 auto;background:white;padding:40px;'
            'border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.1);}'
            'h1{color:#b71c1c;margin:0 0 8px 0;font-size:22px;}'
            'p{color:#555;font-size:14px;line-height:1.6;margin:0 0 20px 0;}'
            '.quota{font-size:12px;color:#999;margin:0 0 16px 0;}'
            'label{display:block;font-size:13px;font-weight:bold;color:#333;'
            'margin-bottom:6px;}'
            'textarea{width:100%;box-sizing:border-box;padding:12px;font-size:14px;'
            'font-family:Arial,Helvetica,sans-serif;border:1px solid #ccc;'
            'border-radius:4px;resize:vertical;min-height:140px;}'
            'button{margin-top:16px;padding:14px 32px;background:#b71c1c;color:white;'
            'font-size:15px;font-weight:bold;border:none;border-radius:4px;'
            'cursor:pointer;width:100%;}'
            'button:hover{background:#c62828;}'
            '</style></head>'
            '<body>'
            '<div class="card">'
            '<h1>&#10007; Request Changes</h1>'
            '<p>Describe what you would like changed in the next version of the newsletter. '
            'The newsletter will be regenerated using the same articles and a new preview '
            'will be sent to you.</p>'
            f'<p class="quota">Revisions remaining this week: <strong>{revisions_left} of {MAX_REVISIONS}</strong></p>'
            f'<form method="GET" action="{form_action}">'
            f'<input type="hidden" name="token" value="{token}" />'
            '<label for="comments">Your feedback:</label>'
            '<textarea id="comments" name="comments" placeholder="e.g. Focus more on enterprise AI adoption stories. The builders section needs more emphasis on revenue traction. Avoid including opinion pieces." required></textarea>'
            '<button type="submit">&#8635; Submit &amp; Regenerate Newsletter</button>'
            '</form>'
            '</div></body></html>'
        ))

    # Feedback submitted — increment counter, store feedback, trigger regeneration
    reviewer_comments = comments.strip()
    newsletter_cache["revision_count"] = revision_count + 1
    newsletter_cache["feedback"] = reviewer_comments
    print(f"Reviewer requested changes (revision {revision_count + 1}/{MAX_REVISIONS}). "
          f"Feedback: {reviewer_comments[:200]}", flush=True)

    generate_and_preview.spawn()
    print("Stage 1 regeneration spawned.", flush=True)

    return HTMLResponse(content=(
        '<!DOCTYPE html><html><head><meta charset="UTF-8">'
        '<title>Regenerating Newsletter</title></head>'
        '<body style="font-family:Arial,Helvetica,sans-serif;text-align:center;'
        'padding:60px 20px;background:#f0f2f5;">'
        '<div style="max-width:520px;margin:0 auto;background:white;padding:40px;'
        'border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">'
        '<h1 style="color:#1565c0;margin-bottom:16px;">&#8635; Regenerating Newsletter</h1>'
        '<p style="font-size:15px;color:#333;margin-bottom:12px;">'
        'Your feedback has been received. A new newsletter is being generated now.</p>'
        '<p style="font-size:14px;color:#555;margin-bottom:20px;line-height:1.6;">'
        '<strong>Your feedback:</strong><br />'
        f'<em style="color:#444;">{reviewer_comments}</em></p>'
        '<p style="font-size:13px;color:#888;">'
        'You will receive a new preview email shortly. This typically takes 2&ndash;3 minutes.</p>'
        '</div></body></html>'
    ))


@app.function(
    image=newsletter_image,
    secrets=[newsletter_secrets],
    timeout=30,
)
@modal.fastapi_endpoint(method="GET")
def browser_preview(token: str = "") -> None:
    """
    Serves the cached newsletter HTML directly in a browser.
    Linked from the "View in browser" link in the preview email banner.
    Lets the reviewer see a canonical render independent of their email client.
    """
    from fastapi.responses import HTMLResponse

    approval_token = os.environ["APPROVAL_SECRET_TOKEN"]

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

    return HTMLResponse(content=cached["html"])


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
