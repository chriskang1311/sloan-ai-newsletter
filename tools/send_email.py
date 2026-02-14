"""
tools/send_email.py

Sends the HTML newsletter email via Gmail API using refresh-token OAuth.
No interactive browser flow — works headlessly in Modal.

Prerequisites:
  Run tools/setup_gmail_oauth.py once locally to get GMAIL_CLIENT_ID,
  GMAIL_CLIENT_SECRET, and GMAIL_REFRESH_TOKEN, then store them as Modal secrets.

Usage:
  from tools.send_email import send_newsletter
  result = send_newsletter(
      client_id=os.environ["GMAIL_CLIENT_ID"],
      client_secret=os.environ["GMAIL_CLIENT_SECRET"],
      refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
      sender_email=os.environ["GMAIL_SENDER_EMAIL"],
      recipient_emails=["you@gmail.com"],
      subject="AI for Sloanies | February 14, 2026",
      html_body=html_string,
  )

Standalone test (sends a test email using .env credentials):
  python tools/send_email.py
"""

import base64
import os
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def build_gmail_service(
    client_id: str,
    client_secret: str,
    refresh_token: str,
):
    """
    Constructs a Gmail API service client using stored OAuth credentials.
    No interactive flow — uses refresh token directly.
    """
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError(
            "Required packages missing. Run: "
            "pip install google-auth google-api-python-client"
        )

    creds = Credentials(
        token=None,  # Will be refreshed automatically
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def create_mime_message(
    sender: str,
    recipients: list[str],
    subject: str,
    html_body: str,
) -> MIMEMultipart:
    """
    Creates a MIME email message with plain text fallback and HTML body.
    """
    message = MIMEMultipart("alternative")
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject

    # Plain text fallback for email clients that don't render HTML
    plain_text = (
        "This newsletter is best viewed in an HTML-compatible email client.\n\n"
        "AI for Sloanies: All things AI — translated from nerd to MBA.\n\n"
        f"Subject: {subject}"
    )

    message.attach(MIMEText(plain_text, "plain"))
    message.attach(MIMEText(html_body, "html"))

    return message


def encode_message(message: MIMEMultipart) -> dict:
    """
    Encodes a MIME message to the format required by Gmail API.
    """
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return {"raw": encoded}


def send_newsletter(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    sender_email: str,
    recipient_emails: list[str],
    subject: str,
    html_body: str,
) -> dict:
    """
    Main entry point. Sends the HTML newsletter via Gmail API.

    Returns the Gmail API response dict (contains message 'id').
    Raises on auth failures or missing recipients.
    """
    if not recipient_emails:
        raise ValueError("recipient_emails list is empty. At least one recipient required.")

    try:
        from google.auth.exceptions import RefreshError
    except ImportError:
        raise ImportError("Run: pip install google-auth")

    # Build Gmail service
    try:
        service = build_gmail_service(client_id, client_secret, refresh_token)
    except RefreshError as e:
        raise RuntimeError(
            "Gmail refresh token is invalid or expired. "
            "Re-run tools/setup_gmail_oauth.py locally to get a new token."
        ) from e

    # Build and encode message
    message = create_mime_message(sender_email, recipient_emails, subject, html_body)
    encoded = encode_message(message)

    # Send via Gmail API
    try:
        result = (
            service.users()
            .messages()
            .send(userId="me", body=encoded)
            .execute()
        )
    except Exception as e:
        error_str = str(e)
        if "403" in error_str:
            raise RuntimeError(
                f"Gmail API 403 Forbidden. Ensure Gmail API is enabled in Google Cloud Console "
                f"and the OAuth scope includes gmail.send. Full error: {e}"
            ) from e
        raise

    msg_id = result.get("id", "unknown")
    print(f"Email sent successfully to {', '.join(recipient_emails)}. Message ID: {msg_id}")
    return result


if __name__ == "__main__":
    # Standalone test — sends a simple test email
    from dotenv import load_dotenv
    load_dotenv()

    required = [
        "GMAIL_CLIENT_ID",
        "GMAIL_CLIENT_SECRET",
        "GMAIL_REFRESH_TOKEN",
        "GMAIL_SENDER_EMAIL",
        "RECIPIENT_EMAILS",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        print("Add them to .env or run tools/setup_gmail_oauth.py first.")
        sys.exit(1)

    test_html = """
    <html><body>
    <h1 style="color: #1a1a2e;">AI for Sloanies — Test Email</h1>
    <p>If you're reading this, the Gmail sending pipeline works! 🎉</p>
    <p>The full newsletter will be sent every Wednesday at 8am ET.</p>
    </body></html>
    """

    recipients = [r.strip() for r in os.environ["RECIPIENT_EMAILS"].split(",")]
    result = send_newsletter(
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        sender_email=os.environ["GMAIL_SENDER_EMAIL"],
        recipient_emails=recipients,
        subject="TEST: AI for Sloanies Email Pipeline",
        html_body=test_html,
    )
    print(f"Test email sent! Message ID: {result.get('id')}")
