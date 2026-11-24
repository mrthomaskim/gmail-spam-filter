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
from email.utils import getaddresses
from zoneinfo import ZoneInfo

import functions_framework
from google.auth.exceptions import RefreshError
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


# --- structural rules -------------------------------------------------------
# Strong signals move a message on their own. Weak signals need two or more together,
# so a single coincidence (a receipt with "$9.99/mo", a GitHub mail naming your
# username) never sends real mail to Spam.
FORGED_ADDR = re.compile(r"[^\s<>,;\"]+@[^\s<>,;\"]+@|random_anm", re.I)   # a@b.com@c.net
INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
ODD_SCRIPT = re.compile("[\u0400-\u04ff\u0590-\u05ff\u0600-\u06ff]")      # Cyrillic, Hebrew, Arabic
LATIN = re.compile("[A-Za-z]")
PER_MONTH = re.compile(r"\$\s?\d+(?:\.\d\d)?\s*(?:/|per\s*)?\s*mo(?:nth)?\b", re.I)
MY_EMAIL = os.environ.get("MY_EMAIL", "").lower()   # set in deploy.sh; enables the username rule
EMAILISH = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def recipients(*headers):
    """[(display name, address)] across To/Cc/Bcc."""
    return [(n.strip().lower(), a.strip().lower()) for n, a in getaddresses(list(headers)) if a]


def bad_recipient_domain(rcpts):
    # '"me@gmail.com" <me@90538>': a domain with no dot can't receive internet mail.
    return any("@" in a and "." not in a.rsplit("@", 1)[1] for _, a in rcpts)


def spoofed_recipient_name(rcpts):
    # '"me@outlook.com" <stranger@junk.com>': the display name is a different address.
    return any(EMAILISH.match(n) and n != a for n, a in rcpts)


def weak_signals(frm: str, subject: str, rcpts=()):
    found = []
    if spoofed_recipient_name(rcpts):
        found.append("recipient name is a different address")
    if INVISIBLE.search(subject) or INVISIBLE.search(frm):
        found.append("invisible characters")
    if LATIN.search(subject) and ODD_SCRIPT.search(subject):
        found.append("mixed-script subject")       # Hangul/CJK are deliberately not counted
    local = normalize(MY_EMAIL.split("@")[0])
    if len(local) >= 6 and local[:8] in normalize(subject):
        found.append("your username in subject")
    if PER_MONTH.search(subject):
        found.append("price per month in subject")
    return found


def classify(frm: str, subject: str, to: str, cc: str = "", bcc: str = ""):
    text = normalize(f"{frm} {subject}")
    for k in KEYWORDS:
        if k in text:
            return f"keyword: {k}"
    if FORGED_ADDR.search(f"{to} {cc} {bcc}"):
        return "forged recipient address"
    rcpts = recipients(to, cc, bcc)
    if bad_recipient_domain(rcpts):
        return "recipient domain has no dot"
    if len(re.findall(r"(?=\.Ltd\.)", frm, re.I)) >= 2:  # segments may have words between them
        return "junk sender pattern"
    weak = weak_signals(frm, subject, rcpts)
    if len(weak) >= 2:
        return "signals: " + ", ".join(weak)
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


# Gmail allows 15,000 quota units per user per minute; messages.get costs 5.
# 30 gets/sec = 9,000 units/min, leaving headroom for the push function.
GETS_PER_SECOND = 30
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
            metadataHeaders=["From", "Subject", "To", "Cc", "Bcc"],
        ).execute(num_retries=RETRIES)
        checked += 1
        h = {x["name"].lower(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
        frm, subject, to = h.get("from", ""), h.get("subject", ""), h.get("to", "")
        reason = classify(frm, subject, to, h.get("cc", ""), h.get("bcc", ""))
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
    try:
        checked, moved, _ = scan(f"in:inbox after:{after}")
    except RefreshError as e:
        # Token expired/revoked: retrying can't help, so log once and ack the message.
        # Fix: python get_token.py && ./deploy.sh
        print(json.dumps({"severity": "CRITICAL", "message": "gmail token invalid_grant",
                          "error": str(e)[:200]}))
        return
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
