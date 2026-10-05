# Re-authenticating Gmail (OAuth2)

The functions act as your Gmail account using an OAuth **refresh token** stored in
Secret Manager (`gmail-oauth-token`). If that token stops working, the filter stops
moving mail. This guide gets a new token and swaps it in without a full redeploy.

## When you need this

Look for `invalid_grant` in the logs:

```bash
gcloud functions logs read gmail-filter --gen2 --region=us-central1 --limit=50 | grep -i invalid_grant
```

Common causes:

| Cause | Avoid it by |
|---|---|
| OAuth app left in **Testing** — tokens expire after 7 days | Publishing the app (Google Auth Platform → Audience → **Publish app**) |
| You changed your Google password | — (Gmail scopes are revoked on password change) |
| You removed the app at [myaccount.google.com/connections](https://myaccount.google.com/connections) | — |
| Token unused for 6 months | Nothing to do — the daily watch renewal keeps it in use |
| OAuth client deleted or its secret reset | Downloading the new `client_secret.json` first (step 1) |

## 1. Get `client_secret.json`

It's gitignored, so you may have deleted it after the first setup. If so:
Console → **Google Auth Platform → Clients** → your Desktop client → **Download JSON**,
saved in this folder as `client_secret.json`.

If you get `invalid_client` in the next step, the client secret was reset or the
client deleted — create/download a fresh one here.

## 2. Get a new refresh token

```bash
cd gmail-spam-filter
python3 -m venv .venv && source .venv/bin/activate   # skip if .venv exists
pip install google-auth-oauthlib
python get_token.py
```

In the browser: pick the Gmail account the filter runs on → **Advanced** →
**Go to … (unsafe)** → **Select all** permissions → Continue. This writes `token.json`.

> If it says *"No refresh token returned"*, remove the app at
> [myaccount.google.com/connections](https://myaccount.google.com/connections) and run it again.

## 3. Upload it to GCP

```bash
gcloud auth login            # if your gcloud session has expired
PROJECT_ID=your-project-id ./update_token.sh
```

`update_token.sh`:

1. Checks the token with Google first, so a bad token never reaches the functions.
2. Adds it as a new version of the `gmail-oauth-token` secret.
3. Restarts both functions on a new revision. They only read the secret when an
   instance starts, so without this they'd keep using the old token.
4. Renews the Gmail watch through the manage function, which proves the new token works.
5. Disables (doesn't destroy) the old secret versions.

The last line of output before "Disabling" should be JSON with `historyId` and
`expiration`.

> `./deploy.sh` also uploads `token.json` if it's present, but it rebuilds
> everything. Use `update_token.sh` when only the token changed.

## 4. Clean up

```bash
rm token.json
```

Keep `client_secret.json` somewhere outside this folder if you want to skip step 1
next time — never commit either file.

## Rolling back

The functions always read the secret's `latest` version (the newest one, which must
be enabled), so roll back by re-adding an old token as a new version:

```bash
gcloud secrets versions list gmail-oauth-token
gcloud secrets versions enable <old-N> --secret=gmail-oauth-token
gcloud secrets versions access <old-N> --secret=gmail-oauth-token > token.json
PROJECT_ID=your-project-id ./update_token.sh
rm token.json
```
