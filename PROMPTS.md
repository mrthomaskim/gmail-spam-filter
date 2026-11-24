# PROMPTS.md

Ready-to-paste prompts for Claude Code. Run it from the repo root on your Mac or in Cloud Shell. It needs `gcloud` (signed in as you) and access to `*.googleapis.com`. Every prompt assumes Claude has read `CLAUDE.md`, and all of them follow its "Rules: do not break these" section.

| Prompt | Use when |
|---|---|
| [Fix an expired token](#fix-an-expired-token) | Spam is getting through again, or the logs show `invalid_grant` / "gmail token invalid_grant" |
| [Add a spam rule](#add-a-spam-rule) | A spam email reached your inbox and should have been caught |
| [Deploy / redeploy](#deploy--redeploy) | Shipping a code or keyword change, or rolling back |
| [Provision infrastructure](#provision-infrastructure) | Rebuilding the filter in a new GCP project, or moving it to Terraform |

---

## Fix an expired token

```text
Read CLAUDE.md. The filter looks broken; I think the Gmail token expired.

1. Confirm it. Read the last 48h of logs for gmail-filter and gmail-filter-manage
   (gcloud functions logs read ... --gen2 --region=us-central1) and look for invalid_grant,
   RefreshError, and "gmail token invalid_grant". Tell me when it started and whether
   pushes are still arriving.
2. Find the cause before fixing anything. Have me check Google Auth Platform → Audience
   for project tkim-gmail-spam-blocker:
   - Status "Testing" → that's the cause (7-day token expiry). Walk me through Publish app →
     In production, and warn me that the existing token stays dead.
   - Status "In production" → it was revoked: a password change, removal at
     myaccount.google.com/connections, or too many tokens issued. Ask which is likely.
3. Re-grant. Check client_secret.json exists locally (if not, tell me how to add a new
   secret to the Desktop client and download it). Then I run `python get_token.py` myself;
   tell me what to click: Advanced → Go to … (unsafe) → Select all → Continue.
4. With my OK, run deploy.sh with PROJECT_ID=tkim-gmail-spam-blocker, plus SHEET_ID and
   MY_EMAIL (ask me for them; read them from the current function config if you can).
   Then delete token.json.
5. Verify: the manage ?action=renew call succeeds, then ask me to send myself a test
   email with "C.ARSHIELD" in the subject and confirm a "moved to spam" log line appears.
6. If no log-based alert exists for jsonPayload.message="gmail token invalid_grant",
   offer to create one that emails me.
Never print the token or ask me to paste it into chat.
```

---

## Add a spam rule

```text
Read CLAUDE.md, especially "How a message is classified". A spam email reached my inbox.
I've attached its headers (or pasted them below).

1. Work out why it wasn't caught. For each rule in classify(), say whether it matched and
   why not. Only use the headers we read (From, To, Cc, Bcc, Subject).
2. List the spam signals in those headers, ranked strong (enough on its own) vs weak (needs 2+).
3. Propose the smallest change that catches it. Prefer, in this order: a structural rule >
   a weak signal (still needing 2+) > a new keyword in KEYWORDS.
   Never let a single weak signal decide.
4. Before writing code, list the legitimate mail it could hit (receipts, GitHub, newsletters,
   Korean-language mail, family) and show the change avoids it.
5. Add tests to tests/test_rules.py: one that the spam is caught, plus one per legitimate
   look-alike to show it isn't. Use janedoe123@gmail.com, never my real address.
6. Run `pytest -q`, update the classification section of CLAUDE.md, and show me the diff.
7. Write the equivalent patch for Unmangle's app/filtering.py (and its test) so I can apply
   it there too. Don't deploy; I'll run the Deploy prompt.

Headers:
<paste here>
```

---

## Deploy / redeploy

```text
Read CLAUDE.md. Deploy the current code to tkim-gmail-spam-blocker.

1. Run `git status` and `git log -3 --oneline`, and summarize what's changing.
   Show me `git diff` for main.py.
2. Run `pytest -q`. If anything fails, stop.
3. Record the current revisions for rollback:
   gcloud run services describe gmail-filter --region us-central1 \
     --format='value(status.traffic[0].revisionName)'
   (and the same for gmail-filter-manage).
4. Ask me for SHEET_ID and MY_EMAIL (or read them from the current function's env vars),
   then wait for my OK.
5. Run deploy.sh. If token.json is present, it uploads a new secret version; delete
   token.json afterwards.
6. Verify: the renew call at the end of deploy.sh succeeded, and there are no new ERROR or
   CRITICAL logs in the next few minutes. Ask me to send a test email containing "C.ARSHIELD"
   and confirm it's moved.
7. If verification fails, show me the errors and offer to roll back:
   gcloud run services update-traffic gmail-filter --region us-central1 \
     --to-revisions=<previous revision>=100
   Never roll back without my OK.
```

---

## Provision infrastructure

```text
Read CLAUDE.md. I want to <rebuild the filter in a new GCP project | move it to Terraform
in the existing project>.

Rebuild in a new project:
1. Preflight: gcloud auth, billing accounts (`gcloud billing accounts list`; ask me which),
   and access to googleapis.com.
2. Propose a project ID and show me the create + billing-link commands. Wait for my OK.
3. Walk me through the console-only steps one screen at a time: OAuth consent screen
   (External, then Publish → In production; scopes gmail.modify and spreadsheets), and a
   Desktop OAuth client saved as client_secret.json.
4. I run `python get_token.py`. Then run deploy.sh with the new PROJECT_ID (with my OK)
   and delete token.json.
5. Verify with a test email. Then tell me how to shut down the old project, but don't do it.

Move to Terraform (existing project):
1. Write infra/terraform for the existing resources: APIs, the gmail-filter SA + IAM,
   the gmail-push topic + gmail-api-push publisher binding, the gmail-oauth-token secret
   (container only; the value stays out of state), both gen2 functions (source zipped to a
   GCS bucket), and the renew-gmail-watch Scheduler job. Pin Terraform 1.16.x with a GCS backend.
2. Generate `import` blocks for everything that already exists, so the first plan shows
   0 to add and 0 to destroy. Summarize the plan and wait for my OK before any apply.
3. Keep deploy.sh working until Terraform has applied cleanly once, then update CLAUDE.md.
Never put the refresh token in Terraform variables or state.
```
