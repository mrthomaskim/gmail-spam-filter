#!/usr/bin/env bash
# Uploads a fresh token.json (from get_token.py) to Secret Manager and restarts
# both functions so they pick it up. Faster than a full ./deploy.sh — no rebuild.
#   PROJECT_ID=my-proj ./update_token.sh
# See REAUTH.md for the full re-authentication steps.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID}"
REGION="${REGION:-us-central1}"
TOKEN_FILE="${TOKEN_FILE:-token.json}"
SECRET="gmail-oauth-token"
SERVICES=(gmail-filter gmail-filter-manage)

cd "$(dirname "$0")"
[[ -f "$TOKEN_FILE" ]] || { echo "ERROR: $TOKEN_FILE not found. Run get_token.py first (see REAUTH.md)." >&2; exit 1; }

echo "==> Checking the refresh token with Google"
# Exchange the refresh token for an access token so a bad token never reaches the functions.
python3 - "$TOKEN_FILE" <<'EOF'
import json, sys, urllib.error, urllib.parse, urllib.request
info = json.load(open(sys.argv[1]))
data = urllib.parse.urlencode({
    "client_id": info["client_id"],
    "client_secret": info["client_secret"],
    "refresh_token": info["refresh_token"],
    "grant_type": "refresh_token",
}).encode()
try:
    resp = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", data))
except urllib.error.HTTPError as e:
    sys.exit(f"ERROR: Google rejected the token: {e.read().decode()}")
scopes = resp.get("scope", "").split()
if "https://www.googleapis.com/auth/gmail.modify" not in scopes:
    sys.exit("ERROR: token lacks gmail.modify — re-run get_token.py and tick every permission.")
if "https://www.googleapis.com/auth/spreadsheets" not in scopes:
    print("    WARNING: token lacks spreadsheets scope — filtering works, Sheet logging won't.")
print("    OK")
EOF

echo "==> Uploading new secret version"
OLD_VERSIONS="$(gcloud secrets versions list "$SECRET" --project="$PROJECT_ID" \
  --filter="state=ENABLED" --format="value(name.basename())")"
NEW_VERSION="$(gcloud secrets versions add "$SECRET" --project="$PROJECT_ID" \
  --data-file="$TOKEN_FILE" --format="value(name.basename())")"
echo "    Added version $NEW_VERSION"

echo "==> Restarting functions on the new token"
# Functions read TOKEN_JSON (secret version 'latest') only when an instance starts,
# so roll out a new revision. Changing an env var does that without a rebuild.
for svc in "${SERVICES[@]}"; do
  gcloud run services update "$svc" --project="$PROJECT_ID" --region="$REGION" \
    --update-env-vars="TOKEN_SECRET_VERSION=${NEW_VERSION}" --quiet >/dev/null
  echo "    $svc restarted"
done

echo "==> Verifying: renewing the Gmail watch with the new token"
URL="$(gcloud functions describe gmail-filter-manage --gen2 --project="$PROJECT_ID" \
  --region="$REGION" --format='value(serviceConfig.uri)')"
curl -fsS -H "Authorization: Bearer $(gcloud auth print-identity-token)" "${URL}?action=renew"
echo

echo "==> Disabling old secret versions (kept for rollback, not destroyed)"
for v in $OLD_VERSIONS; do
  gcloud secrets versions disable "$v" --secret="$SECRET" --project="$PROJECT_ID" --quiet >/dev/null
  echo "    Disabled version $v"
done

echo
echo "Done. Delete the local token now:  rm $TOKEN_FILE"
