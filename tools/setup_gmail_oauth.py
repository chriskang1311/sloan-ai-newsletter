"""
tools/setup_gmail_oauth.py

One-time local script to complete Google OAuth flow and extract secrets for Modal.

Prerequisites:
  1. Go to console.cloud.google.com
  2. Create project "Sloan AI Newsletter"
  3. Enable Gmail API (APIs & Services > Library)
  4. OAuth consent screen: External, add your email as test user, scope: gmail.send
  5. Create OAuth 2.0 Client ID (Desktop app), download as credentials.json in project root

Usage:
  python tools/setup_gmail_oauth.py

Output:
  Prints GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN to terminal.
  Copy these three values into Modal secrets dashboard under "newsletter-secrets".
  Also saves token.json locally as a backup.
"""

import json
import os
import sys

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]
CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"


def run_oauth_flow(client_secrets_file: str) -> dict:
    """
    Runs the interactive OAuth flow using the installed app flow.
    Opens a browser window for the user to authenticate.
    Returns credentials as a dict.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("ERROR: google-auth-oauthlib is not installed.")
        print("Run: pip install google-auth-oauthlib")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, SCOPES)
    creds = flow.run_local_server(port=0)

    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "token_uri": creds.token_uri,
        "scopes": list(creds.scopes) if creds.scopes else SCOPES,
    }


def extract_and_display_secrets(credentials_dict: dict) -> None:
    """
    Prints the three secrets needed for Modal to stdout.
    """
    print("\n" + "=" * 60)
    print("SUCCESS! Copy these values into Modal secrets dashboard")
    print("Secret name: newsletter-secrets")
    print("=" * 60)
    print(f"\nGMAIL_CLIENT_ID:     {credentials_dict['client_id']}")
    print(f"GMAIL_CLIENT_SECRET: {credentials_dict['client_secret']}")
    print(f"GMAIL_REFRESH_TOKEN: {credentials_dict['refresh_token']}")
    print("\n" + "=" * 60)
    print("Also add these other required secrets to newsletter-secrets:")
    print("  NEWSAPI_KEY         (from newsapi.org)")
    print("  ANTHROPIC_API_KEY   (from console.anthropic.com)")
    print("  GMAIL_SENDER_EMAIL  (the Gmail address you just authenticated)")
    print("  RECIPIENT_EMAILS    (comma-separated, e.g. you@gmail.com,friend@gmail.com)")
    print("=" * 60 + "\n")


def main() -> None:
    # Check credentials.json exists
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"ERROR: {CREDENTIALS_FILE} not found in current directory.")
        print("\nTo get credentials.json:")
        print("  1. Go to console.cloud.google.com")
        print("  2. Create project 'Sloan AI Newsletter'")
        print("  3. Enable Gmail API (APIs & Services > Library)")
        print("  4. APIs & Services > OAuth consent screen > External")
        print("     - Add your email as test user")
        print("     - Add scope: https://www.googleapis.com/auth/gmail.send")
        print("  5. APIs & Services > Credentials > Create Credentials > OAuth 2.0 Client ID")
        print("     - Application type: Desktop app")
        print("     - Download the JSON file and save as credentials.json here")
        sys.exit(1)

    print("Starting Gmail OAuth flow...")
    print("A browser window will open. Please log in and grant access.\n")

    credentials_dict = run_oauth_flow(CREDENTIALS_FILE)

    # Save token.json locally as backup
    with open(TOKEN_FILE, "w") as f:
        json.dump(credentials_dict, f, indent=2)
    print(f"Token saved locally to {TOKEN_FILE} (gitignored)")

    extract_and_display_secrets(credentials_dict)


if __name__ == "__main__":
    main()
