"""Regression tests for email / phone extraction bugs in extractor.py.
Run:  python tests/test_bugfix_extract.py   (from the app/ directory)
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.collector import dom  # noqa: E402
from backend.collector.extractor import (  # noqa: E402
    clean_emails, extract_company, find_emails,
)


def emails_in(s: str) -> list[str]:
    return clean_emails(find_emails(s))


def company(body: str, ld=None):
    head = ""
    if ld is not None:
        head = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    raw = f"<html><head><title>Acme</title>{head}</head><body>{body}</body></html>"
    return extract_company(dom.parse(raw.encode()), raw, "https://acme.com")


def test_regex_junk():
    # sentence period with no space: the next word is not a TLD
    assert emails_in("Write to info@acme.com.Call us today") == ["info@acme.com"]
    assert emails_in("Mail info@acme.co.uk.Thanks") == ["info@acme.co.uk"]
    assert emails_in("Mail jobs@acme.co.uk today") == ["jobs@acme.co.uk"]
    # JSON-escaped HTML in raw page source
    raw_json = '<script>var x = "\\u003cp\\u003einfo@acme.com\\u003c/p\\u003e";</script>'
    ex = company(raw_json)
    assert ex.email == ["info@acme.com"], ex.email
    assert emails_in("\\u003cp\\u003einfo@acme.com") == ["info@acme.com"]
    assert emails_in("u003einfo@acme.com") == ["info@acme.com"]
    # URL-escaped mailto (raw html scan and the mailto link itself)
    ex = company('<a href="mailto:%20sales@acme.com">mail</a>')
    assert ex.email == ["sales@acme.com"], ex.email
    assert emails_in("mailto:%20sales@acme.com") == ["sales@acme.com"]
    # HTML entities in raw page source
    ex = company('<div data-x="&lt;b&gt;info@acme.com&lt;/b&gt;"></div>')
    assert ex.email == ["info@acme.com"], ex.email
    # retina / image asset names
    for junk in ("logo@3x.avif", "logo@2x.png", "hero@1.5x.webp", "icon@4x.svg",
                 "a@b.gif", "x@banner.jpg", "x@banner.jpeg", "fav@site.ico",
                 "bootstrap@5.3.0.min.css", "jquery@3.6.0.min.js"):
        assert emails_in(junk) == [], junk


def test_no_substring_false_positives():
    real = ["sales@bigdomain.com", "accounts@freedomain.com",
            "info@sentrysecurity.com", "hello@bootstrapcapital.com",
            "username@acme.com", "rename@acme.com", "user@acme.com",
            "info@email.computer", "jobs@test.co.uk"]
    for e in real:
        assert clean_emails([e]) == [e], e


def test_real_junk_still_filtered():
    junk = ["email@example.com", "info@example.org", "x@mail.example.com",
            "your@email.com", "name@domain.com", "user@domain.com",
            "abc123@o12345.ingest.sentry.io", "abc@sentry.io",
            "abc@sentry-next.wixpress.com", "x@wixpress.com",
            "info@yourdomain.com", "x@schema.org", "x@www.w3.org",
            "noreply@acme.com", "no-reply@acme.com", "donotreply@acme.com",
            "do-not-reply@acme.com", "x@test.com", "root@localhost.localdomain",
            "jquery@acme.com", "bootstrap@acme.com", "yourname@acme.com",
            "john@doe.com"]
    for e in junk:
        assert clean_emails([e]) == [], e
    # the .example TLD (test fixtures) is still accepted
    assert clean_emails(["hello@acmetalent.example"]) == ["hello@acmetalent.example"]


def test_json_ld_email_list():
    ld = {"@type": "Organization", "name": "Acme",
          "email": ["mailto:Info@Acme.com", "sales@acme.com", ["support@acme.com"],
                    "help@acme.com"]}
    ex = company("", ld)
    assert ex.email == ["info@acme.com", "sales@acme.com", "support@acme.com"], ex.email
    assert all("[" not in e and "'" not in e for e in ex.email), ex.email

    ld = {"@type": "Organization", "name": "Acme", "email": "email@example.com"}
    ex = company('<a href="mailto:hello@acme.com">m</a>', ld)
    assert ex.email == ["hello@acme.com"], ex.email

    ld = {"@type": "Organization", "name": "Acme", "email": "mailto:%20sales@acme.com"}
    ex = company("", ld)
    assert ex.email == ["sales@acme.com"], ex.email

    # cap of 3 holds on the combined list
    ld = {"@type": "Organization", "name": "Acme", "email": ["a@acme.com", "b@acme.com"]}
    ex = company('<a href="mailto:c@acme.com">c</a> <a href="mailto:d@acme.com">d</a>', ld)
    assert ex.email == ["a@acme.com", "b@acme.com", "c@acme.com"], ex.email


def test_json_ld_phone_list_and_dedupe():
    ld = {"@type": "Organization", "name": "Acme",
          "telephone": ["+1-555-010-2030", "+1-555-010-9999"]}
    ex = company('<a href="tel:+1 555 010 2030">call</a>', ld)
    digits = [''.join(c for c in p if c.isdigit()) for p in ex.phone]
    assert digits.count("15550102030") == 1, ex.phone
    assert ex.phone[0] == "+1-555-010-2030", ex.phone
    assert all("[" not in p for p in ex.phone), ex.phone

    ld = {"@type": "Organization", "name": "Acme", "telephone": "tel:+1 555 010 2030"}
    ex = company('<a href="tel:+1-555-010-2030">call</a>', ld)
    digits = [''.join(c for c in p if c.isdigit()) for p in ex.phone]
    assert digits == ["15550102030"], ex.phone


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("ALL PASSED")
