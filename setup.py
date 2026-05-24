#!/usr/bin/env python3
"""
Run this ONCE before installing the LaunchAgent to authorize Google Calendar.
A browser window will open asking you to sign in and grant access.
"""
from pathlib import Path

CREDENTIALS_PATH = "credentials.json"
TOKEN_PATH = "token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def main():
    if not Path(CREDENTIALS_PATH).exists():
        print(f"Error: {CREDENTIALS_PATH} not found.")
        print("Steps to get it:")
        print("  1. Go to console.cloud.google.com")
        print("  2. Create a project → Enable Google Calendar API")
        print("  3. APIs & Services → Credentials → Create OAuth 2.0 Client ID (Desktop)")
        print(f"  4. Download the JSON and save it as {CREDENTIALS_PATH} in this folder")
        raise SystemExit(1)

    from google_auth_oauthlib.flow import InstalledAppFlow

    print("Opening browser for Google Calendar authorization...")
    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)

    Path(TOKEN_PATH).write_text(creds.to_json())
    print(f"\nAuthorization complete. Token saved to {TOKEN_PATH}")
    print("You can now install the LaunchAgent.")


if __name__ == "__main__":
    main()
