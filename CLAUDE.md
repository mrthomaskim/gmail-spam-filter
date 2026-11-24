# CLAUDE.md

Context for Claude Code in this repo. Read this first. Ready-to-paste task prompts are in `PROMPTS.md`.

## What this is

The owner's **personal** Gmail spam filter. It's single-user and runs in GCP project `tkim-gmail-spam-blocker` (us-central1). It catches spam whose sender names are disguised with junk characters (`FID.ELI.TY LI.FE`, `C.ARSHIELD`, `E^NDURANCE`) and moves it to Spam seconds after it arrives.

It's the prototype for **Unmangle**, the multi-tenant SaaS in a separate repo. Rules proven here get ported there (Unmangle's `app/filtering.py`), so keep the rule logic in step with it. The owner is a staff-level systems/infra engineer. Be direct and skip basics.

## Architecture

```
Gmail ──users.watch──▶ Pub/Sub "gmail-push" ──▶ gmail-filter (Cloud Function gen2, on_gmail_push)
                                                   └─ scan last 15 min of INBOX → classify → batchModify to SPAM
                                                   └─ optional: append a row to a Google Sheet (SHEET_ID)
Cloud Scheduler "renew-gmail-watch" (daily 06:17 America/Chicago)
      ──OIDC──▶ gmail-filter-manage (HTTP, manage)  ?action=renew  re-arms the watch (expires after 7 days)
                                                    ?action=scan&days=N  one-off backfill (480s budget, re-run if incomplete)
```

- **Credentials:** one OAuth refresh token for the owner's Gmail, stored in Secret Manager as `gmail-oauth-token` (JSON with client_id, client_secret, refresh_token). It's mounted into both functions as `TOKEN_JSON`. It's created locally with `get_token.py` (Desktop OAuth client, `client_secret.json`).
- **Service account:** `gmail-filter@<project>.iam.gserviceaccount.com`. It runs both functions, invokes the manage function from Scheduler, and acts as the Eventarc trigger identity.
- **Deploy:** `deploy.sh`, gcloud only and idempotent. It enables APIs, creates the SA, topic, and secret (uploading `token.json` if present), deploys both functions, and recreates the Scheduler job. At the end it calls `?action=renew`.
- **Env vars:** `TOPIC`, `SHEET_ID` (optional), `MY_EMAIL` (optional; turns on the username-in-subject signal), `LOG_TZ` (default America/Chicago).

## How a message is classified (`main.py`, `classify`)

Only the **From, To, Cc, Bcc, and Subject** headers are read (`format=metadata`). Order matters, and the first match wins:

1. **Keywords**: a substring of `normalize(From + Subject)`. `normalize` applies NFKD, lowercases, maps 1/0/3/5 → i/o/e/s, and keeps only a–z. The list is `KEYWORDS`.
2. **Forged recipient**: any address in To/Cc/Bcc with two `@` signs (`me@gmail.com@junk.net`), or `random_anm`. A strong signal; it decides on its own.
3. **Junk sender**: 2+ `.Ltd.` segments in From (counted with a lookahead, since segments can have words between them). Strong.
4. **Weak signals**: these need **2 or more** to trigger. Never let a single weak signal decide.
   - invisible characters (zero-width, BOM, soft hyphen)
   - a mixed-script subject: Latin plus Cyrillic/Hebrew/Arabic. Hangul and CJK are excluded on purpose, because the owner gets legitimate Korean-language mail.
   - the first 8 letters of `MY_EMAIL`'s local part appearing in the subject
   - a monthly price in the subject (`$39Mo`, `$9.99/month`)

The reason string goes to the logs and the Sheet ("keyword: carshield", "forged recipient address", "signals: …").

## Commands

```bash
pip install -r requirements-dev.txt
pytest -q                          # rule tests (tests/test_rules.py); no network needed
python get_token.py                # new refresh token → token.json (needs client_secret.json)
PROJECT_ID=tkim-gmail-spam-blocker SHEET_ID=<sheet id> MY_EMAIL=<owner gmail> ./deploy.sh
rm token.json                      # always, right after deploy
gcloud functions logs read gmail-filter --gen2 --region=us-central1 --limit=50
```

Backfill (one-off; the manage function is authenticated):

```bash
URL=$(gcloud functions describe gmail-filter-manage --gen2 --region=us-central1 --format='value(serviceConfig.uri)')
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL?action=scan&days=60"
```

## Rules: do not break these

1. **Never read message bodies or attachments.** Headers only (the list above).
2. **Never commit or print credentials:** `token.json`, `client_secret*.json`, or the secret's contents. Never ask the owner to paste a token into chat. Run `rm token.json` after every deploy.
3. **Keep the RefreshError handling in `on_gmail_push`.** An expired token must log one CRITICAL `gmail token invalid_grant` line and ack the message. It must never raise, because raising makes Pub/Sub retry forever.
4. **Every rule change gets tests:** the spam is caught *and* legitimate look-alikes pass (receipts, GitHub notifications, newsletters, Korean-language mail, family mail). Use placeholder addresses (`janedoe123@gmail.com`) in tests, never the owner's.
5. **Respect Gmail quota:** 15k units/user/minute, and `messages.get` costs 5. Keep `GETS_PER_SECOND` pacing and the 480s scan budget. Don't run 60-day backfills repeatedly.
6. **Ask before deploying.** Show the diff and the test results first.

## Known gotchas (learned the hard way)

- **The OAuth app must be "In production"** (Google Auth Platform → Audience). In Testing mode, refresh tokens expire after 7 days. Publishing doesn't fix a token issued while the app was in Testing; you need a new token after publishing. Other things that revoke a token: changing the Google password, removing the app at myaccount.google.com/connections, 6 months unused, or more than ~100 tokens issued for the same client.
- There's no way to refresh an expired *refresh* token automatically. It takes a human consent click (`get_token.py`).
- Spammers rotate domains and pass SPF/DKIM/DMARC on throwaway domains, so blocking senders or domains doesn't hold. Use structural rules instead.
- An earlier version of the `.Ltd.` regex (`(\.Ltd\.){2,}`) never matched real senders, because they put words between the segments. The lookahead count fixes it.
- Spam can hide the forged address in **Bcc** only. That's why Cc and Bcc are read.

## Status

- [x] Push-based filter running, with Sheet logging, forged-Bcc and weak-signal rules, and RefreshError handling
- [ ] Log-based alert on `jsonPayload.message="gmail token invalid_grant"` (recommended)
- [ ] Optional: move to Terraform (see the Provision prompt in PROMPTS.md)
