"""Run locally once. Opens a browser to authorize your Gmail account and writes
token.json (client_id, client_secret, refresh_token) for deploy.sh to upload
to Secret Manager.

    pip install google-auth-oauthlib
    python get_token.py
"""
import json

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

if not creds.refresh_token:
    raise SystemExit("No refresh token returned — revoke the app at "
                     "myaccount.google.com/connections and run again.")

with open("token.json", "w") as f:
    json.dump({
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "refresh_token": creds.refresh_token,
    }, f)

print("Wrote token.json")
