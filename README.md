# Gmail spam filter on GCP (push-based)

Gmail notifies a Pub/Sub topic whenever your inbox changes → a Cloud Run function
checks recent inbox mail → matches (mangled "Fidelity Life", CarShield, Endurance, etc.)
are moved to Spam and logged to your Google Sheet. A daily Cloud Scheduler job renews
the Gmail watch, which otherwise expires after 7 days.

```
Gmail ──watch──▶ Pub/Sub "gmail-push" ──▶ gmail-filter (function) ──▶ Gmail: move to Spam
                                                         └──────────▶ Sheet: log row
Cloud Scheduler (daily) ──▶ gmail-filter-manage?action=renew ──▶ Gmail: renew watch
```

Cost: ~$0/month (all within free tiers). Set a $1 budget alert anyway (step 6).

## 1. Project
Use an existing GCP project with billing attached, or create one. Note its PROJECT_ID.

## 2. OAuth consent screen (Console → Google Auth Platform)
1. **Branding**: app name "Gmail Spam Filter", your email as support + developer contact.
2. **Audience**: User type **External**. Then click **Publish app** so status is
   **In production**. ⚠️ If left in *Testing*, the refresh token expires after 7 days
   and the filter silently stops.
3. **Data access**: add scopes `https://www.googleapis.com/auth/gmail.modify` and
   `https://www.googleapis.com/auth/spreadsheets`.

Skip verification — it's not needed for your own account.

## 3. OAuth client
Google Auth Platform → **Clients** → **Create client** → type **Desktop app**.
Download the JSON and save it in this folder as `client_secret.json`.

## 4. Get your refresh token
```bash
cd gmail-spam-filter
python3 -m venv .venv && source .venv/bin/activate
pip install google-auth-oauthlib
python get_token.py
```
In the browser: pick your Gmail account → **Advanced** → **Go to … (unsafe)** →
**Select all** permissions → Continue. This writes `token.json`.

## 5. Deploy
```bash
gcloud auth login
PROJECT_ID=your-project-id SHEET_ID=your-sheet-id ./deploy.sh
```
`SHEET_ID` is optional (same ID you used in Apps Script). The last line should print a
JSON response with `historyId` and `expiration` — that means the watch is live.

Then delete the local credentials:
```bash
rm token.json client_secret.json
```
If the token ever stops working (`invalid_grant` in the logs), see [REAUTH.md](REAUTH.md).

## 6. Budget alert
Console → Billing → **Budgets & alerts** → Create budget → $1/month, alert at 50/90/100%.

## Using it

**Backfill old inbox mail (one time):**
```bash
URL=$(gcloud functions describe gmail-filter-manage --gen2 --region=us-central1 --format='value(serviceConfig.uri)')
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL?action=scan&days=60"
```

**Watch the logs:**
```bash
gcloud functions logs read gmail-filter --gen2 --region=us-central1 --limit=50
```

**Add a keyword:** edit `KEYWORDS` in `main.py` (lowercase, letters only — the code strips
dots, dashes, underscores, spaces, emoji, accents, and maps 1/0/3/5 → i/o/e/s), then re-run
`./deploy.sh`. No need for token.json on re-runs.

**Re-authenticate Gmail:** if the filter stops moving mail with `invalid_grant` errors,
get a new token and upload it with `./update_token.sh` — see [REAUTH.md](REAUTH.md).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `invalid_grant` in logs | Token revoked or app was in Testing mode. Publish app (step 2), then follow [REAUTH.md](REAUTH.md). |
| Watch call: `User not authorized to perform this action` | Pub/Sub publisher binding for `gmail-api-push@system.gserviceaccount.com` missing — re-run `./deploy.sh`. |
| Push function never fires (project created before Apr 2021) | Grant the Pub/Sub service agent `roles/iam.serviceAccountTokenCreator` on the project. |
| Rows not appearing in Sheet | Check `SHEET_ID` and that the sheet is owned by/shared with your Gmail account. Filtering still works without it. |
