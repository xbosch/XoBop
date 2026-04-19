#!/usr/bin/env python3
"""One-time Google OAuth setup for XoBop Gmail + Calendar skills.

Before running:
  1. Go to https://console.cloud.google.com
  2. APIs & Services → Library → enable Gmail API and Google Calendar API
  3. APIs & Services → Credentials → + Create Credentials → OAuth 2.0 Client ID
  4. Application type: Desktop App → Create → Download JSON
  5. Save the downloaded file as:  config/google_credentials.json

Then run:
  cd /home/xavi/workspace/AgentsWork/XoBop
  source .venv/bin/activate
  pip install google-auth-oauthlib google-api-python-client
  python scripts/google_oauth_setup.py

A browser window will open. Log in and grant access.
The token is saved to config/google_token.json — restart XoBop to activate the skills.
"""

import os
import sys
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
]

CREDS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "config/google_credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "config/google_token.json")


def main() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Missing package. Run:  pip install google-auth-oauthlib google-api-python-client")
        sys.exit(1)

    creds_path = Path(CREDS_FILE)
    if not creds_path.exists():
        print(f"Credentials file not found: {CREDS_FILE}")
        print("Download it from Google Cloud Console → APIs & Services → Credentials")
        sys.exit(1)

    print(f"Opening browser for Google OAuth...")
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=0)

    token_path = Path(TOKEN_FILE)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    print(f"✅ Token saved → {TOKEN_FILE}")
    print("Restart XoBop — Gmail and Calendar skills will activate automatically.")


if __name__ == "__main__":
    main()
