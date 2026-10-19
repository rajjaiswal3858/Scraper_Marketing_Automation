"""Regression tests for pipeline bug fixes, offline:
  1) a record refused for a full target leaves no dedup keys behind
     (no phantom "Duplicate" in a later search / location), race-safe
  2) a same-name business on another website / phone is not a duplicate
  3) company-name keys: short names (IBM, 3M, H&M), any script, accents
     folded; pure-ASCII keys unchanged
  4) trailing-dot domains; international phone numbers keep their country
  5) transient Unknown email verdicts are not cached for hours
  6) Excel sheet titles never end with an apostrophe after truncation
Run:  python tests/test_bugfix_pipeline.py  (from the app/ directory)"""

from __future__ import annotations

import os
import re
import sys
import threading
from _scratch import scratch_dir  # noqa: E402  (tests/_scratch.py)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import config  # noqa: E402

_tmp = scratch_dir(prefix="bugfix_test_")
config.OUTPUT_DIR = _tmp
config.STATE_PATH = os.path.join(_tmp, "bugfix_state.json")
config.EMAIL_VERIFY = False

from backend.collector import emailcheck as ec  # noqa: E402
from backend.collector.deduplicator import DedupRegistry  # noqa: E402
from backend.collector.engine import CollectionJob, StateStore  # noqa: E402
from backend.collector.exporter import _sheet_title  # noqa: E402
from backend.collector.normalize import norm_domain, norm_name, norm_phone  # noqa: E402
from backend.collector.validator import validate  # noqa: E402


def _rec(name, site="", phone="", email=""):
    return {"Company Name": name, "Official Website": site,
            "Business Phone": phone, "Business Email": email}


def test_full_target_leaves_no_phantom_duplicate():
    state = StateStore()
    a = CollectionJob(state, "RPO", ["rpo"], "Austin, Texas, USA", 1, "serper", 4,
                      geo={"city": "Austin", "state": "Texas", "country": "USA"})
    loc_a = dict(a.search_place)
    first = _rec("Alpha Staffing", "https://alphastaffing.example", "+1 512 555 0101") | loc_a
    second = _rec("Beta Recruiting", "https://betarecruit.example", "+1 512 555 0202") | loc_a
    assert a._try_add(first)
    assert not a._try_add(second), "target 1 is full: the second record is refused"
    assert a.counters["duplicates"] == 0, "a full target is not a duplicate"
    reg = state.registry
    assert "betarecruit.example" not in reg.domains, "refused record left its domain key"
    assert norm_name("Beta Recruiting") not in reg.names
    assert norm_phone("+1 512 555 0202") not in reg.phones
    # the same business found by a search elsewhere is new, not a "Duplicate"
    b = CollectionJob(state, "RPO", ["rpo"], "Dallas, Texas, USA", 5, "serper", 4,
                      geo={"city": "Dallas", "state": "Texas", "country": "USA"})
    again = _rec("Beta Recruiting", "https://betarecruit.example", "+1 512 555 0202") \
        | dict(b.search_place)
    assert b._try_add(again), "business refused for a full target later reported Duplicate"
    assert b.counters["duplicates"] == 0
    # the refused keys never reached the checkpoint either
    state.save(config.STATE_PATH)
    reloaded = StateStore.load(config.STATE_PATH)
    assert reloaded.registry.peek(_rec("Gamma HR", "https://gammahr.example")) == ""
    print("  full target: refused record not registered, found later as new")


def test_concurrent_full_target_registers_exactly_stored():
    state = StateStore()
    target = 5
    barrier = threading.Barrier(16)

    def worker(i):
        rec = _rec(f"Company Number {i:03d}", f"https://co{i}.example", f"+1 512 555 {1000 + i}")
        rec["Search Location"] = "Austin"

        def store():
            return state.append_record("RPO", rec, target, "austin")
        barrier.wait()
        state.registry.check_and_add(rec, "RPO", commit=store)
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stored = state.records["RPO"]
    assert len(stored) == target, len(stored)
    assert len(state.registry.domains) == target, state.registry.domains
    assert len(state.registry.names) == target and len(state.registry.phones) == target
    assert {norm_domain(r["Official Website"]) for r in stored} == set(state.registry.domains)
    print("  16 concurrent workers, target 5: exactly the 5 stored records registered")


def test_same_name_other_business():
    reg = DedupRegistry()
    assert reg.check_and_add(_rec("Smile Dental", "https://smiledental-austin.com",
                                  "(512) 555-0100"), "Dentists")[0]
    added, dup = reg.check_and_add(_rec("Smile Dental", "https://smiledentaldallas.com",
                                        "(214) 555-0100"), "Dentists")
    assert added and not dup, "different website + phone: a different business"
    assert reg.peek(_rec("Smile Dental", "https://smiledental-houston.com")) == ""
    # a name match with nothing conflicting is still a duplicate
    assert not reg.check_and_add(_rec("Smile Dental"), "Dentists")[0]
    assert not reg.check_and_add(_rec("Smile Dental", phone="512-555-0100"), "Dentists")[0]
    assert not reg.check_and_add(_rec("SMILE DENTAL LLC", "https://smiledental-austin.com/"),
                                 "Dentists")[0]
    # same website / email / phone still dedup regardless of the name
    assert not reg.check_and_add(_rec("Other Name", "https://smiledentaldallas.com"), "X")[0]
    assert not reg.check_and_add(_rec("Other Name", phone="+1 214 555 0100"), "X")[0]
    # Places listings: two different cids under one name are two listings
    p1 = _rec("Joe's Pizza", phone="+1 212 555 0001") | {"_cid": "111"}
    p2 = _rec("Joe's Pizza", phone="+1 718 555 0002") | {"_cid": "222"}
    assert reg.check_and_add(p1, "Food")[0] and reg.check_and_add(p2, "Food")[0]
    # the name ids survive a checkpoint round trip
    reg2 = DedupRegistry.from_dict(reg.to_dict())
    assert reg2.peek(_rec("Smile Dental", "https://smiledental-houston.com",
                          "(713) 555-0100")) == ""
    assert reg2.peek(_rec("Smile Dental")) != ""
    # an old registry (no name ids) is backfilled from the stored records
    old = reg.to_dict()
    old.pop("name_ids")
    reg3 = DedupRegistry.from_dict(old)
    assert reg3.peek(_rec("Smile Dental", "https://smiledental-houston.com")) != "", \
        "legacy names keep the name-only rule until backfilled"
    reg3.ensure(_rec("Smile Dental", "https://smiledental-austin.com", "(512) 555-0100"),
                "Dentists")
    assert reg3.peek(_rec("Smile Dental", "https://smiledental-houston.com")) == ""
    print("  same name, different website/phone: distinct; name-only match still dedups")


def _old_norm_name(name):
    from backend.collector.normalize import LEGAL_SUFFIX_RE
    n = LEGAL_SUFFIX_RE.sub(" ", (name or "").lower())
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n if len(n) >= 4 else ""


def test_norm_name():
    for name in ("Acme Talent Solutions", "Vertex RPO Services Pvt. Ltd.", "Joe's Pizza",
                 "A-1 Plumbing & Heating LLC", "Smile Dental", "3D Systems Corp",
                 "ShieldNet Security", "Nimbus Hiring Solutions Inc.", "  MIXED case Co  ",
                 "Example GmbH & Co. KG", "Johnson+Johnson", "100 Acres"):
        assert norm_name(name) == _old_norm_name(name), (name, norm_name(name))
    assert norm_name("IBM") == "ibm" and norm_name("3M") == "3m"
    assert norm_name("H&M") == "hm" and norm_name("BP plc") == "bp"
    assert norm_name("Café Müller") == norm_name("Cafe Muller") == "cafemuller"
    assert norm_name("トヨタ自動車") and norm_name("Яндекс") == "яндекс"
    assert norm_name("STRASSE") == norm_name("straße"), "casefold"
    assert norm_name("A") == "" and norm_name("&&") == "" and norm_name("Ltd.") == ""
    for name in ("IBM", "3M", "H&M", "BP plc", "トヨタ自動車", "Яндекс", "Café Müller"):
        ok, why = validate(_rec(name, "https://brand.example", "+1 555 010 2030"),
                           from_places=False)
        assert ok, (name, why)
    assert not validate(_rec("X", "https://x.example"), from_places=False)[0]
    print("  names: IBM / 3M / H&M / BP plc / CJK / Cyrillic accepted, accents folded, "
          "ASCII keys unchanged")


def test_domain_and_phone():
    assert norm_domain("https://Example.com./") == "example.com"
    assert norm_domain("https://www.Example.com.:8080/x") == "example.com"
    assert norm_domain("example.com") == "example.com"
    # international numbers keep their country code
    assert norm_phone("+44 20 7946 0958") != norm_phone("+1 207 946 0958")
    assert norm_phone("+44 20 7946 0958") == norm_phone("0044 20 7946 0958") == "442079460958"
    # NANP written with / without +1 (or 001) still match, as before
    us = "5550102030"
    for p in ("+1 555 010 2030", "555-010-2030", "(555) 010-2030", "1-555-010-2030",
              "001 555 010 2030", "Tel: +1 (555) 010-2030"):
        assert norm_phone(p) == us, (p, norm_phone(p))
    assert norm_phone("+91 98765 43210") == "919876543210"
    assert norm_phone("98765 43210") == "9876543210"   # national form unchanged
    assert norm_phone("123") == "" and norm_phone("") == ""
    print("  domains: trailing dot stripped; phones: +44 != +1, +1 == national")


def test_email_transient_not_cached():
    calls = []
    real = ec._verify

    def fake(email):
        calls.append(email)
        if email.startswith("dns"):
            return ec.Verdict(email, ec.UNKNOWN, 50,
                              "Format OK · Domain OK · DNS/MX: mail servers could not "
                              "be looked up (DNS timeout)")
        if email.startswith("boom"):
            raise RuntimeError("resolver crashed")
        return ec.Verdict(email, ec.VALID, 100, "SMTP: mailbox exists")
    ec._verify = fake
    ec._verdicts.clear()
    try:
        for addr in ("dns@a.example", "boom@b.example", "ok@c.example"):
            ec.verify(addr)
        v = ec.verify("boom@b.example")
        assert v.status == ec.UNKNOWN and "check failed" in v.detail, v
        now = ec.time.monotonic()
        for addr in ("dns@a.example", "boom@b.example"):
            exp = ec._verdicts[addr][0]
            assert exp - now <= ec.UNKNOWN_TTL_S + 1, (addr, exp - now)
        assert ec._verdicts["ok@c.example"][0] - now > ec.UNKNOWN_TTL_S
        # once the short TTL has passed the address is checked again
        for addr in ("dns@a.example", "boom@b.example"):
            ec._verdicts[addr] = (now - 1, ec._verdicts[addr][1])
        n = len(calls)
        ec.verify("dns@a.example")
        ec.verify("boom@b.example")
        ec.verify("ok@c.example")
        assert len(calls) == n + 2, calls
    finally:
        ec._verify = real
        ec._verdicts.clear()
    print("  email: DNS timeout / check failed cached 5 min only, real answers 6 h")


def test_sheet_title():
    used: set[str] = set()
    name = "A" * 30 + "'s Region"            # 31st char is an apostrophe
    t = _sheet_title(name, used)
    assert len(t) <= 31 and not t.endswith("'") and not t.startswith("'"), t
    assert t == "A" * 30
    t2 = _sheet_title(name, used)
    assert t2 == "A" * 27 + " (2)" and len(t2) <= 31, t2
    t3 = _sheet_title("B" * 26 + "'xyz", {("B" * 26 + "'xyz").lower()})
    assert t3 == "B" * 26 + " (2)", t3
    assert _sheet_title("'''", set()) == "Results"
    assert _sheet_title("Texas", {"texas"}) == "Texas (2)"
    assert _sheet_title("a/b:c", set()) == "a-b-c"
    print("  sheet titles: never end with an apostrophe, collisions stay within 31")


def main():
    test_full_target_leaves_no_phantom_duplicate()
    test_concurrent_full_target_registers_exactly_stored()
    test_same_name_other_business()
    test_norm_name()
    test_domain_and_phone()
    test_email_transient_not_cached()
    test_sheet_title()
    print("\nALL BUGFIX PIPELINE TESTS PASSED")


if __name__ == "__main__":
    main()
