"""Rule tests. Run: pip install -r requirements-dev.txt && pytest -q
Use placeholder addresses only (janedoe123@gmail.com), never real ones."""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["MY_EMAIL"] = "janedoe123@gmail.com"
main = importlib.import_module("main")
classify = main.classify
ME = "janedoe123@gmail.com"


@pytest.mark.parametrize("raw,expected", [
    ("FID.ELI.TY LI.FE", "fidelitylife"),
    ("FiDelity      Life^^^^^^^", "fidelitylife"),
    ("F1del1ty_Life", "fidelitylife"),
    ("C.ARSHIELD", "carshield"),
    ("E^NDURANCE", "endurance"),
    ("A.UT.O.PLAN.A.DV.ISO.RS", "autoplanadvisors"),
])
def test_normalize(raw, expected):
    assert main.normalize(raw) == expected


def test_keywords():
    assert classify('"C.ARSHIELD" <x@spam.com>', "Your quote", ME) == "keyword: carshield"


def test_forged_recipient_in_to_cc_or_bcc():
    frm = '"ISN printableannouncements" <support@printableannouncements.net>'
    assert classify(frm, "hi", f"{ME}@7c1x6symq8kns8edt.__random_anm") == "forged recipient address"
    assert classify(frm, "hi", ME, bcc=f'"x" <{ME}@pxkXO.email.gmx.net>') == "forged recipient address"
    assert classify(frm, "hi", ME, cc=f"{ME}@junk.net") == "forged recipient address"


def test_ltd_segments():
    frm = "Y <SuRvEy-offe.Ltd.BIMMER.Ltd.ICK.Ltd.SuRvEy@kitayamatriangle.com>"
    assert classify(frm, "hi", ME) == "junk sender pattern"
    assert classify("Acme <billing@acme.ltd.uk>", "Invoice", ME) is None


def test_two_weak_signals_trigger():
    frm = '"ISN printableannouncements" <support@printableannouncements.net>'
    subj = "janedoe1:✨Confirm your $39Mo Quote-Now﻿! #47g [הלוואי]"
    reason = classify(frm, subj, ME)
    assert reason.startswith("signals:") and "invisible characters" in reason


EHEALTH_FROM = "epmuvmentz EHEALTH <EHEALTH@mail.epmuvmentz.com>"
EHEALTH_SUBJ = "[JANEDOE123, COMPaRE MEdicar PLaNs for 2027 . #39]"


def test_ehealth_dotless_recipient_domain():
    to = '"janedoe12@gmail.com" <janedoe12@90538>'
    cc = '"janedoe12@IAZV1C5Z136.namprd04.prod.outlook.com" <tanhanphuc@gbqexrvzjppuk.com>'
    assert classify(EHEALTH_FROM, EHEALTH_SUBJ, to, cc) == "recipient domain has no dot"


def test_ehealth_spoofed_name_plus_username_is_two_weak_signals():
    # Same spam without the dotless To: the Cc display-name trick + username still catch it.
    cc = '"janedoe12@IAZV1C5Z136.namprd04.prod.outlook.com" <tanhanphuc@gbqexrvzjppuk.com>'
    reason = classify(EHEALTH_FROM, EHEALTH_SUBJ, ME, cc)
    assert reason.startswith("signals:") and "recipient name is a different address" in reason


def test_spoofed_recipient_name_alone_does_not_trigger():
    cc = '"someone@corp.com" <someone@corp.onmicrosoft.com>'
    assert classify("Boss <boss@corp.com>", "Q4 plan", ME, cc) is None


@pytest.mark.parametrize("to", [
    f'"{ME}" <{ME}>',                       # Outlook-style quoted address, same as real one
    f"Jane Doe <{ME}>, team@corp.com",
    "undisclosed-recipients:;",
    "",
])
def test_normal_recipient_headers_pass(to):
    assert classify("Mom <mom@family.com>", "Dinner Sunday?", to) is None


@pytest.mark.parametrize("frm,subj", [
    ("Netflix <info@netflix.com>", "Your plan is now $15.49/month"),
    ("GitHub <notifications@github.com>", "[janedoe123/unmangle] Run failed: CI"),
    ("Naver <news@naver.com>", "이번 주 IT 뉴스: Google Cloud Next"),
    ("Stripe <receipts@stripe.com>", "Your receipt from Unmangle $3/mo"),
    ("Mom <mom@family.com>", "Dinner Sunday?"),
])
def test_legit_mail_is_not_moved(frm, subj):
    assert classify(frm, subj, ME) is None
