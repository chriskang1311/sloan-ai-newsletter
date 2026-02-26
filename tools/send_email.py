"""
tools/send_email.py

Sends the HTML newsletter email via Brevo SMTP relay.

Using Brevo (formerly Sendinblue) instead of Gmail SMTP gives us established IP
reputation with Outlook/Microsoft, which prevents newsletters from going to junk.

Prerequisites:
  1. Create a free Brevo account at brevo.com
  2. Verify the sender address:
       Brevo dashboard → Senders & IPs → Senders → Add sender → confirm the email
  3. Generate an SMTP key:
       Brevo dashboard → Transactional → Email → SMTP & API → Generate new SMTP key
  4. Store in Modal secrets (or .env for local testing):
       GMAIL_SENDER_EMAIL  — the verified From address (e.g. mitsloanaiclub@gmail.com)
       BREVO_SMTP_LOGIN    — the email you use to log into Brevo
       BREVO_SMTP_KEY      — the SMTP key generated in step 3

Usage:
  from tools.send_email import send_newsletter

  # Preview to reviewer (direct To:)
  send_newsletter(
      sender_email=os.environ["GMAIL_SENDER_EMAIL"],
      smtp_password=os.environ["BREVO_SMTP_KEY"],
      smtp_login=os.environ["BREVO_SMTP_LOGIN"],
      recipient_emails=["reviewer@example.com"],
      subject="[PREVIEW] AI for Sloanies | Feb 25, 2026",
      html_body=html_string,
      inline_images={"logo": open("ai_club_logo.jpg", "rb").read()},
  )

  # Subscriber blast (all recipients in BCC to protect their privacy)
  send_newsletter(
      sender_email=os.environ["GMAIL_SENDER_EMAIL"],
      smtp_password=os.environ["BREVO_SMTP_KEY"],
      smtp_login=os.environ["BREVO_SMTP_LOGIN"],
      recipient_emails=["sub1@example.com", "sub2@example.com"],
      subject="AI for Sloanies | Feb 25, 2026",
      html_body=html_string,
      bcc=True,
      inline_images={"logo": open("ai_club_logo.jpg", "rb").read()},
  )

Standalone test (sends a test email using .env credentials):
  python tools/send_email.py
"""

from __future__ import annotations

import os
import smtplib
import sys
import uuid
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr


def send_newsletter(
    sender_email: str,
    smtp_password: str,
    recipient_emails: list[str],
    subject: str,
    html_body: str,
    smtp_login: str | None = None,
    bcc: bool = False,
    inline_images: dict[str, bytes] | None = None,
) -> dict:
    """
    Sends the HTML newsletter via Brevo SMTP relay.

    sender_email  → the From address shown to recipients (must be verified in Brevo)
    smtp_login    → the email used to authenticate with Brevo SMTP (your Brevo account
                    email). Defaults to sender_email if not provided.
    smtp_password → the SMTP key from Brevo dashboard (Transactional → Email → SMTP & API)

    bcc=True  → all recipient_emails go in BCC (subscriber blast — hides the list from recipients)
    bcc=False → recipient_emails go in To: directly (reviewer preview or small sends)

    inline_images → dict mapping CID names to raw image bytes.
                    The HTML should reference them as <img src="cid:logo" />.
                    This keeps images out of the HTML body, preventing Gmail clipping.

    Returns {"id": <smtp-message-id>} to stay compatible with the rest of the pipeline.
    Raises RuntimeError on SMTP auth or send failures.
    """
    if not recipient_emails:
        raise ValueError("recipient_emails list is empty. At least one recipient required.")

    login = smtp_login or sender_email

    plain_text = (
        "This newsletter is best viewed in an HTML-compatible email client.\n\n"
        "AI for Sloanies: All things AI — translated from nerd to MBA.\n\n"
        f"Subject: {subject}"
    )

    # Build MIME structure:
    #   multipart/related (when inline images present)
    #     └── multipart/alternative
    #           ├── text/plain
    #           └── text/html
    #     └── image/jpeg  (Content-ID: <logo>)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain_text, "plain"))
    alt.attach(MIMEText(html_body, "html"))

    if inline_images:
        message = MIMEMultipart("related")
        message.attach(alt)
        for cid, image_bytes in inline_images.items():
            img_part = MIMEImage(image_bytes)
            img_part.add_header("Content-ID", f"<{cid}>")
            img_part.add_header("Content-Disposition", "inline")
            message.attach(img_part)
    else:
        message = alt

    message["From"] = formataddr(("MIT Sloan AI Club", sender_email))
    message["Reply-To"] = formataddr(("MIT Sloan AI Club", sender_email))
    message["Subject"] = subject
    sender_domain = sender_email.split("@")[-1]
    message["Message-ID"] = f"<{uuid.uuid4()}@{sender_domain}>"
    message["List-Unsubscribe"] = f"<mailto:{sender_email}?subject=unsubscribe>"
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message["Precedence"] = "bulk"
    message["List-Id"] = "<ai-for-sloanies.mitsloanaiclub.com>"
    message["X-Mailer"] = "MIT Sloan AI Club Newsletter"

    if bcc:
        # Subscriber blast — show sender in To:, deliver to subscribers via SMTP envelope only.
        # Do NOT set a Bcc header — it would be visible to recipients in Gmail.
        message["To"] = formataddr(("MIT Sloan AI Club", sender_email))
    else:
        # Direct send — reviewer preview or small targeted sends
        message["To"] = ", ".join(recipient_emails)

    try:
        with smtplib.SMTP("smtp-relay.brevo.com", 587) as server:
            server.starttls()
            server.login(login, smtp_password)
            # sendmail needs all actual recipients (To + Bcc) in the rcpt list
            result = server.sendmail(
                sender_email,
                recipient_emails,
                message.as_bytes(),
            )
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(
            "Brevo SMTP authentication failed. Check that BREVO_SMTP_KEY is the key "
            "generated in Brevo dashboard (Transactional → Email → SMTP & API) and that "
            "BREVO_SMTP_LOGIN is the email address you use to log into Brevo."
        ) from e
    except smtplib.SMTPException as e:
        raise RuntimeError(f"Brevo SMTP error: {e}") from e

    # result is a dict of {recipient: (code, msg)} for any failed addresses; empty = all succeeded
    failed = result
    if failed:
        print(f"Warning: {len(failed)} recipient(s) failed: {list(failed.keys())}")

    dest = "BCC" if bcc else "To"
    print(f"Email sent via Brevo SMTP ({dest}: {len(recipient_emails)} recipient(s)).")
    return {"id": f"smtp-{sender_email}"}


if __name__ == "__main__":
    # Standalone test — sends a simple test email
    from dotenv import load_dotenv
    load_dotenv()

    required = ["GMAIL_SENDER_EMAIL", "BREVO_SMTP_LOGIN", "BREVO_SMTP_KEY", "RECIPIENT_EMAILS"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        print("Add them to .env first.")
        sys.exit(1)

    test_html = """
    <html><body>
    <h1 style="color: #1a1a2e;">AI for Sloanies — Test Email</h1>
    <p>If you're reading this, the Brevo SMTP pipeline works!</p>
    <p>The full newsletter will be sent every Wednesday at 8am ET.</p>
    </body></html>
    """

    recipients = [r.strip() for r in os.environ["RECIPIENT_EMAILS"].split(",")]
    result = send_newsletter(
        sender_email=os.environ["GMAIL_SENDER_EMAIL"],
        smtp_password=os.environ["BREVO_SMTP_KEY"],
        smtp_login=os.environ["BREVO_SMTP_LOGIN"],
        recipient_emails=recipients,
        subject="TEST: AI for Sloanies Email Pipeline",
        html_body=test_html,
        bcc=True,
    )
    print(f"Test email sent! Result: {result}")
