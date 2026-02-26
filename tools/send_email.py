"""
tools/send_email.py

Sends the HTML newsletter email via Gmail SMTP using an App Password.

No OAuth flow required — just a Gmail address and a 16-character App Password
generated from your Google Account settings.

Prerequisites:
  1. Enable 2-Step Verification on your Google Account:
       myaccount.google.com → Security → 2-Step Verification
  2. Generate an App Password:
       myaccount.google.com → Security → App passwords
       App: Mail | Device: Other (name it "Sloan Newsletter") → Generate
  3. Store in Modal secrets (or .env for local testing):
       GMAIL_SENDER_EMAIL  — your Gmail address (e.g. you@gmail.com)
       GMAIL_APP_PASSWORD  — the 16-character app password (no spaces)

Usage:
  from tools.send_email import send_newsletter

  # Preview to reviewer (direct To:)
  send_newsletter(
      sender_email=os.environ["GMAIL_SENDER_EMAIL"],
      app_password=os.environ["GMAIL_APP_PASSWORD"],
      recipient_emails=["reviewer@example.com"],
      subject="[PREVIEW] AI for Sloanies | Feb 25, 2026",
      html_body=html_string,
  )

  # Subscriber blast (all recipients in BCC to protect their privacy)
  send_newsletter(
      sender_email=os.environ["GMAIL_SENDER_EMAIL"],
      app_password=os.environ["GMAIL_APP_PASSWORD"],
      recipient_emails=["sub1@example.com", "sub2@example.com"],
      subject="AI for Sloanies | Feb 25, 2026",
      html_body=html_string,
      bcc=True,
  )

Standalone test (sends a test email using .env credentials):
  python tools/send_email.py
"""

import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def send_newsletter(
    sender_email: str,
    app_password: str,
    recipient_emails: list[str],
    subject: str,
    html_body: str,
    bcc: bool = False,
) -> dict:
    """
    Sends the HTML newsletter via Gmail SMTP.

    bcc=True  → all recipient_emails go in BCC (subscriber blast — hides the list from recipients)
    bcc=False → recipient_emails go in To: directly (reviewer preview or small sends)

    Returns {"id": <smtp-message-id>} to stay compatible with the rest of the pipeline.
    Raises RuntimeError on SMTP auth or send failures.
    """
    if not recipient_emails:
        raise ValueError("recipient_emails list is empty. At least one recipient required.")

    plain_text = (
        "This newsletter is best viewed in an HTML-compatible email client.\n\n"
        "AI for Sloanies: All things AI — translated from nerd to MBA.\n\n"
        f"Subject: {subject}"
    )

    message = MIMEMultipart("alternative")
    message["From"] = sender_email
    message["Subject"] = subject

    if bcc:
        # Subscriber blast — show sender in To:, put all subscribers in BCC
        message["To"] = sender_email
        message["Bcc"] = ", ".join(recipient_emails)
    else:
        # Direct send — reviewer preview or small targeted sends
        message["To"] = ", ".join(recipient_emails)

    message.attach(MIMEText(plain_text, "plain"))
    message.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, app_password)
            # sendmail needs all actual recipients (To + Bcc) in the rcpt list
            result = server.sendmail(
                sender_email,
                recipient_emails,
                message.as_bytes(),
            )
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(
            "Gmail SMTP authentication failed. Check that GMAIL_APP_PASSWORD is the "
            "16-character App Password (not your regular Gmail password) and that "
            "2-Step Verification is enabled on the account."
        ) from e
    except smtplib.SMTPException as e:
        raise RuntimeError(f"Gmail SMTP error: {e}") from e

    # result is a dict of {recipient: (code, msg)} for any failed addresses; empty = all succeeded
    failed = result
    if failed:
        print(f"Warning: {len(failed)} recipient(s) failed: {list(failed.keys())}")

    dest = "BCC" if bcc else "To"
    print(f"Email sent via Gmail SMTP ({dest}: {len(recipient_emails)} recipient(s)).")
    # Return a compatible dict — SMTP doesn't give a server-side message ID, so use a placeholder
    return {"id": f"smtp-{sender_email}"}


if __name__ == "__main__":
    # Standalone test — sends a simple test email
    from dotenv import load_dotenv
    load_dotenv()

    required = ["GMAIL_SENDER_EMAIL", "GMAIL_APP_PASSWORD", "RECIPIENT_EMAILS"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        print("Add them to .env first.")
        sys.exit(1)

    test_html = """
    <html><body>
    <h1 style="color: #1a1a2e;">AI for Sloanies — Test Email</h1>
    <p>If you're reading this, the Gmail SMTP pipeline works!</p>
    <p>The full newsletter will be sent every Wednesday at 8am ET.</p>
    </body></html>
    """

    recipients = [r.strip() for r in os.environ["RECIPIENT_EMAILS"].split(",")]
    result = send_newsletter(
        sender_email=os.environ["GMAIL_SENDER_EMAIL"],
        app_password=os.environ["GMAIL_APP_PASSWORD"],
        recipient_emails=recipients,
        subject="TEST: AI for Sloanies Email Pipeline",
        html_body=test_html,
        bcc=True,
    )
    print(f"Test email sent! Result: {result}")
