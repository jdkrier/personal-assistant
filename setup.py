#!/usr/bin/env python3
"""
Run this ONCE before installing the LaunchAgent to authorize Google Calendar + Gmail.
A browser window will open asking you to sign in and grant access.
"""
import os
from pathlib import Path

# Match goose.py: DATA_DIR env var or ~/.goose/data by default
DATA_DIR         = Path(os.environ.get("DATA_DIR", str(Path.home() / ".goose" / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"
TOKEN_PATH       = DATA_DIR / "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/calendar",           # read + write calendar events
    "https://www.googleapis.com/auth/gmail.readonly",     # read Gmail inbox
]


def main():
    if not Path(CREDENTIALS_PATH).exists():
        print(f"Error: {CREDENTIALS_PATH} not found.")
        print("Steps to get it:")
        print("  1. Go to console.cloud.google.com")
        print("  2. Create a project → Enable Google Calendar API + Gmail API")
        print("  3. APIs & Services → Credentials → Create OAuth 2.0 Client ID (Desktop)")
        print(f"  4. Download the JSON and save it as {CREDENTIALS_PATH} in this folder")
        raise SystemExit(1)

    from google_auth_oauthlib.flow import InstalledAppFlow

    # Remove old token so we get a fresh grant with the new scopes
    if Path(TOKEN_PATH).exists():
        Path(TOKEN_PATH).unlink()
        print("Old token removed — re-authorizing with expanded scopes.")

    print("Opening browser for Google Calendar + Gmail authorization...")
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0)

    TOKEN_PATH.write_text(creds.to_json())
    print(f"\nAuthorization complete. Token saved to {TOKEN_PATH}")
    print("Scopes granted:")
    for s in SCOPES:
        print(f"  • {s}")
    print("\nYou can now restart the server.")


if __name__ == "__main__":
    main()
