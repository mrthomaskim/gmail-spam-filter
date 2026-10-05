"""Gmail spam filter — Cloud Run functions (gen2).

on_gmail_push : Pub/Sub-triggered. Gmail publishes to the topic whenever the
                INBOX changes; we scan recent inbox mail and move matches to Spam.
manage        : HTTP (authenticated). ?action=renew re-arms the Gmail watch
                (called daily by Cloud Scheduler). ?action=scan&days=N backfills.
"""
import json
import os
import re
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import functions_framework
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ---- Tunables ---------------------------------------------------------------
KEYWORDS = [
    "fidelitylife",
    "carshield",
    "endurance",
    "autoplanadvisors",
    "extendedwarranty",
]
LOOKBACK_SECONDS = 15 * 60  # each push re-checks the last 15 min of inbox mail
LOG_TZ = ZoneInfo(os.environ.get("LOG_TZ", "America/Chicago"))
# -----------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]
DIGIT_MAP = str.maketrans({"1": "i", "0": "o", "3": "e", "5": "s"})

_creds = None
_gmail = None
_sheets = None


def _get_creds():
    global _creds
    if _creds is None:
        info = json.loads(os.environ["TOKEN_JSON"])  # mounted from Secret Manager
        _creds = Credentials(
            None,
            refresh_token=info["refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=info["client_id"],
            client_secret=info["client_secret"],
            scopes=SCOPES,
        )
    return _creds


def gmail():
    global _gmail
    if _gmail is None:
        _gmail = build("gmail", "v1", credentials=_get_creds(), cache_discovery=False)
    return _gmail


def sheets():
    global _sheets
    if _sheets is None:
        _sheets = build("sheets", "v4", credentials=_get_creds(), cache_discovery=False)
    return _sheets


def normalize(s: str) -> str:
    """'FiD.ELI-TY_L1FE 🎉' -> 'fidelitylife'"""
    s = unicodedata.normalize("NFKD", s).lower().translate(DIGIT_MAP)
    return re.sub(r"[^a-z]", "", s)


def classify(frm: str, subject: str, to: str):
    text = normalize(f"{frm} {subject}")
    for k in KEYWORDS:
        if k in text:
            return f"keyword: {k}"
    if re.search(r"@gmail\.com@|random_anm", to, re.I):
        return "broken To field"
    if len(re.findall(r"\.ltd", frm, re.I)) >= 2:  # e.g. "x.Ltd0.0.Ltd.MCK0.Ltd@..."
        return "junk sender pattern"
    if re.search(r"\s{40,}", subject) and re.search(r"[A-Za-z0-9]{80,}", subject):
        return "padded subject"  # visible subject + whitespace + random blob
    return None


def log_rows(rows):
    sheet_id = os.environ.get("SHEET_ID")
    if not rows or not sheet_id:
        return
    try:
        sheets().spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range="A:D",
            valueInputOption="RAW",  # RAW so a subject like "=IMPORTXML(...)" is never a formula
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute(num_retries=RETRIES)
    except Exception as e:  # logging must never block filtering
        print(json.dumps({"severity": "WARNING", "message": f"sheet log failed: {e}"}))


# Gmail allows 6,000 quota units per user per minute (see the Gmail API
# Quotas page; the old 15,000 limit is "Previous quota"); messages.get costs 5.
# 15 gets/sec = 4,500 units/min, leaving headroom for the push function.
GETS_PER_SECOND = 15
RETRIES = 6  # client retries 429/403-rateLimitExceeded/5xx with exponential backoff
FLUSH_EVERY = 100  # move + log in chunks so a timeout never loses finished work


def _flush(svc, hits, rows):
    if hits:
        svc.messages().batchModify(
            userId="me",
            body={"ids": hits, "addLabelIds": ["SPAM"], "removeLabelIds": ["INBOX"]},
        ).execute(num_retries=RETRIES)
    log_rows(rows)


def scan(query: str, time_budget: float = None):
    """Returns (checked, moved, complete). Stops early if time_budget (seconds) runs out."""
    start = time.monotonic()
    svc = gmail().users()
    ids, page = [], None
    while True:
        resp = svc.messages().list(
            userId="me", q=query, maxResults=500, pageToken=page
        ).execute(num_retries=RETRIES)
        ids += [m["id"] for m in resp.get("messages", [])]
        page = resp.get("nextPageToken")
        if not page:
            break

    hits, rows, moved, checked = [], [], 0, 0
    interval = 1.0 / GETS_PER_SECOND
    for mid in ids:
        if time_budget and time.monotonic() - start > time_budget:
            break
        t0 = time.monotonic()
        msg = svc.messages().get(
            userId="me", id=mid, format="metadata",
            metadataHeaders=["From", "Subject", "To"],
        ).execute(num_retries=RETRIES)
        checked += 1
        h = {x["name"].lower(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
        frm, subject, to = h.get("from", ""), h.get("subject", ""), h.get("to", "")
        reason = classify(frm, subject, to)
        if reason:
            hits.append(mid)
            rows.append([datetime.now(LOG_TZ).strftime("%Y-%m-%d %H:%M:%S"), frm, subject, reason])
            print(json.dumps({"severity": "INFO", "message": "moved to spam",
                              "reason": reason, "from": frm, "subject": subject}))
            if len(hits) >= FLUSH_EVERY:
                _flush(svc, hits, rows)
                moved += len(hits)
                hits, rows = [], []
        if len(ids) > 20:  # pace only big scans; push scans stay instant
            time.sleep(max(0.0, interval - (time.monotonic() - t0)))

    _flush(svc, hits, rows)
    moved += len(hits)
    return checked, moved, checked == len(ids)


@functions_framework.cloud_event
def on_gmail_push(cloud_event):
    # The notification only says "inbox changed"; we don't need its historyId.
    # Scanning a short recent window is stateless and self-healing.
    after = int(time.time()) - LOOKBACK_SECONDS
    checked, moved, _ = scan(f"in:inbox after:{after}")
    print(json.dumps({"severity": "INFO", "message": "push scan", "checked": checked, "moved": moved}))


@functions_framework.http
def manage(request):
    action = request.args.get("action", "renew")
    if action == "renew":
        resp = gmail().users().watch(
            userId="me",
            body={"topicName": os.environ["TOPIC"], "labelIds": ["INBOX"]},
        ).execute(num_retries=RETRIES)
        print(json.dumps({"severity": "INFO", "message": "watch renewed", **resp}))
        return resp, 200
    if action == "scan":
        days = max(1, min(int(request.args.get("days", "60")), 365))
        # Function timeout is 540s; stop at 480s and ask for a re-run.
        checked, moved, complete = scan(f"in:inbox newer_than:{days}d", time_budget=480)
        return {"checked": checked, "moved": moved, "complete": complete,
                "note": "" if complete else "Time limit reached — run again to continue."}, 200
    return {"error": f"unknown action '{action}'"}, 400
