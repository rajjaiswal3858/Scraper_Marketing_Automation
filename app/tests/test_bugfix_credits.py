"""Regression tests for credit-accounting fixes (offline, no credits):

  1. the per-network daily cap on the server's Serper key cannot be dodged
     by switching keys mid-run (own -> server, server -> own), live
     server-key spend counts at once, and the spend survives a restart
  2. a timed-out Serper request counts as 1 credit; a reported 0 stays 0
  3. a rejected collection leaves no custom category behind; per-user cap
  4. non-Latin queries get distinct registry keys; ASCII keys unchanged
  5. the ledger CSV is safe from formula injection

Run (from app/):  python tests/test_bugfix_credits.py
"""

from __future__ import annotations

import csv
import os
import re
import sys
import threading
import time
import types
from collections import deque
from _scratch import scratch_dir  # noqa: E402  (tests/_scratch.py)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import config  # noqa: E402

_tmp = scratch_dir(prefix="bugfix_credits_test_")
config.OUTPUT_DIR = _tmp
config.STATE_PATH = os.path.join(_tmp, "state.json")
config.ANALYZE_PROCESSES = 0
config.MAX_CREDITS_PER_IP_PER_DAY = 10
config.MAX_JOBS_PER_IP = 100
config.MAX_JOBS_PER_CLIENT = 1

import requests  # noqa: E402

from backend.collector import discovery  # noqa: E402
from backend.collector.credits import CreditLedger, ledger_key, norm_query  # noqa: E402
from backend.collector.engine import StateStore  # noqa: E402
from backend.collector.jobs import JobManager, JobRejected  # noqa: E402
from backend.collector.search import SearchError, SerperProvider  # noqa: E402

IP = "203.0.113.7"


# --------------------------------------------------------------------------- #
# 1. per-network daily cap
# --------------------------------------------------------------------------- #

class FakeJob:
    """The slice of CollectionJob the manager uses, with a provider whose
    credit counters the test drives by hand."""

    def __init__(self, state, category, *a, **kw):
        import uuid
        self.id = uuid.uuid4().hex[:12]
        self.category, self.status, self.owner = category, "queued", ""
        self.credit_budget = 50
        self.budget_reason = self.stop_reason = self.finished_at = self.error = ""
        self.crawl_workers = 0
        self.thread = None
        self.on_finish = None
        self.serper_key = ""
        self.counters = {"serper_credits": 0}
        self.provider = None
        self._stop_search = ""
        self.lines: list[str] = []

    def say(self, msg):
        self.lines.append(msg)

    def start(self):
        self.status = "running"
        self.provider = types.SimpleNamespace(credits_used=0, server_credits_used=0,
                                              follow_env=not self.serper_key)

    def stop(self):
        self.status = "stopped"

    def use_serper_key(self, key):
        self.serper_key = key
        if self.provider is not None:
            self.provider.follow_env = not key

    def spend(self, n):
        p = self.provider
        p.credits_used += n
        if p.follow_env:
            p.server_credits_used += n
        self.counters["serper_credits"] = p.credits_used

    def finish(self, manager):
        self.status = "completed"
        manager._on_finish(self)


def new_manager(**kw):
    return JobManager(max_active=8, job_factory=FakeJob,
                      state_for=lambda c: object(), **kw)


def test_server_spend_charged_after_switch_to_own_key():
    m = new_manager()
    j = m.submit("u1", "Finance", [], "", 10, "serper", 5, ip=IP)
    assert j.status == "running" and j.credit_budget == 10, j.credit_budget
    j.spend(6)                                    # on the server key
    assert m._ip_credits_24h(IP) == 6             # charged live, not at the end
    m.set_key("u1", "ownkey-ownkey-ownkey")       # user saves their own key
    assert j.credit_budget == 50 and j.budget_reason == ""   # own key: uncapped
    assert m._ip_credits_24h(IP) == 6             # server spend still counted
    j.spend(20)                                   # own-key credits: not the cap's
    assert m._ip_credits_24h(IP) == 6
    j.finish(m)
    assert m._ip_credits_24h(IP) == 6, m.ip_spend   # charged although it ended on own key
    try:
        m.submit("u2", "Law_Firms", [], "", 10, "serper", 5, ip=IP)
        j2 = m.client_job("u2")
        assert j2.credit_budget == 4, j2.credit_budget
    except JobRejected:
        raise AssertionError("4 credits are still left")
    m.stop_all(timeout=0.1)
    print("  server -> own key: server-key spend still charged to the network: OK")


def test_own_key_then_server_key_is_capped():
    m = new_manager()
    m.ip_spend[IP] = deque([(time.time(), 7)])
    j = m.submit("u1", "Finance", [], "", 10, "serper", 5, ip=IP,
                 serper_key="ownkey-ownkey-ownkey")
    assert j.credit_budget == 50                  # own key: no cap at submit
    j.spend(30)                                   # own key credits
    m.set_key("u1", "")                           # key deleted -> server key
    # only the 3 credits the network has left may still be spent
    assert j.credit_budget == 33, j.credit_budget
    assert "Daily search limit" in j.budget_reason
    assert j._stop_search == ""
    j.spend(3)
    assert m._ip_credits_24h(IP) == 10
    m.stop_all(timeout=0.1)

    # cap already used up -> no further searches on the server key
    m = new_manager()
    m.ip_spend[IP] = deque([(time.time(), 10)])
    j = m.submit("u1", "Finance", [], "", 10, "serper", 5, ip=IP,
                 serper_key="ownkey-ownkey-ownkey")
    m.set_key("u1", "")
    assert j._stop_search == "budget" and j.credit_budget >= 1
    assert any("Daily search limit" in x for x in j.lines), j.lines
    m.set_key("u1", "ownkey-ownkey-ownkey")       # own key again: searches resume
    assert j._stop_search == "" and j.credit_budget == 50
    m.stop_all(timeout=0.1)
    print("  own -> server key: the network's daily cap is re-applied: OK")


def test_queued_job_switching_to_exhausted_server_key_stops():
    m = JobManager(max_active=1, job_factory=FakeJob, state_for=lambda c: object())
    m.ip_spend[IP] = deque([(time.time(), 10)])
    a = m.submit("u0", "Finance", [], "", 10, "serper", 5, serper_key="k" * 20)
    b = m.submit("u1", "Law_Firms", [], "", 10, "serper", 5, ip=IP,
                 serper_key="ownkey-ownkey-ownkey")
    assert a.status == "running" and b.status == "queued"
    m.set_key("u1", "")
    assert b.status == "stopped" and "Daily search limit" in b.stop_reason
    assert b.id not in m.queue
    m.stop_all(timeout=0.1)
    print("  queued job moved onto an exhausted server key is stopped: OK")


def test_spend_persists_across_restart():
    path = os.path.join(_tmp, "ip_spend.json")
    m = new_manager(spend_path=path)
    j = m.submit("u1", "Finance", [], "", 10, "serper", 5, ip=IP)
    j.spend(4)                                    # running job's live spend
    m.ip_spend.setdefault("198.51.100.1", deque()).append(
        (time.time() - 90_000, 99))   # older than 24 h: dropped
    m._save_spend(force=True)
    m.stop_all(timeout=0.1)
    m2 = new_manager(spend_path=path)             # worker restarted
    assert m2._ip_credits_24h(IP) == 4, m2.ip_spend
    assert m2._ip_credits_24h("198.51.100.1") == 0
    m2.ip_spend[IP].append((time.time(), 6))
    try:
        m2.submit("u9", "Finance", [], "", 10, "serper", 5, ip=IP)
        raise AssertionError("daily cap reset by a restart")
    except JobRejected as exc:
        assert exc.status == 429
    m2.stop_all(timeout=0.1)
    print("  per-network spend survives a worker restart: OK")


# --------------------------------------------------------------------------- #
# 1 + 2. SerperProvider credit counting
# --------------------------------------------------------------------------- #

class FakeResp:
    def __init__(self, data):
        self.status_code, self._data, self.text, self.headers = 200, data, "", {}

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, script):
        self.script = list(script)

    def post(self, url, json=None, timeout=None, headers=None):
        step = self.script.pop(0)
        if step == "timeout":
            raise requests.Timeout("simulated")
        return FakeResp(step)


def provider(script, follow_env=True):
    p = SerperProvider("k" * 20, follow_env=follow_env)
    p.session = FakeSession(script)
    p._backoff = staticmethod(lambda attempt, ra: 0.0)
    return p


def test_timeouts_and_reported_credits():
    p = provider(["timeout", {"organic": [], "credits": 2}])
    p.organic("dentists in Austin", 10)
    assert p.credits_used == 3 and p.last_credits == 3, (p.credits_used, p.last_credits)
    assert p.server_credits_used == 3

    p = provider(["timeout"] * 8)
    try:
        p.organic("dentists in Austin", 10)
        raise AssertionError("must give up")
    except SearchError as exc:
        assert exc.kind == "timeout"
    n = config.SERPER_TIMEOUT_RETRIES + 1
    assert p.credits_used == n and p.last_timeouts == n, (p.credits_used, n)

    p = provider([{"organic": [], "credits": 0}, {"organic": []}], follow_env=False)
    p.organic("a in b", 10)
    assert p.credits_used == 0 and p.last_credits == 0   # explicit 0 honoured
    p.organic("a in b", 10)
    assert p.credits_used == 1 and p.last_credits == 1   # missing -> 1
    assert p.server_credits_used == 0                    # own key: not the server's
    print("  timed-out requests count 1 credit; reported 0 stays 0; key split: OK")


# --------------------------------------------------------------------------- #
# 3. custom categories
# --------------------------------------------------------------------------- #

def test_custom_category_rollback_and_cap():
    from backend import worker as worker_mod
    st = StateStore()

    class Mgr:
        reject = True

        def submit(self, client, category, *a, **kw):
            if self.reject:
                raise JobRejected(429, "network limit")
            return types.SimpleNamespace(id="j", log_since=lambda s: (0, [], False))

    w = worker_mod.Worker.__new__(worker_mod.Worker)
    w.stores = types.SimpleNamespace(get=lambda c: st)
    w.manager = Mgr()
    w.job_view = lambda job: {}
    args = dict(client="u1", category="__custom__", keywords=[], location="",
                geo={}, target=10, provider="serper", max_queries=5)
    try:
        w.cmd_collect(custom_category="Solar Installers", **args)
        raise AssertionError("must be rejected")
    except JobRejected:
        pass
    assert not st.custom and "solar" not in " ".join(st.all_categories()), st.custom
    # an EXISTING custom category is never removed by a rejection
    slug = st.ensure_custom("Drone Pilots")
    try:
        w.cmd_collect(custom_category="drone  pilots", **args)
    except JobRejected:
        pass
    assert slug in st.custom
    # success creates it
    w.manager.reject = False
    w.cmd_collect(custom_category="Solar Installers", **args)
    assert any(d["display"] == "Solar Installers" for d in st.custom.values())
    # per-user cap
    for i in range(worker_mod.MAX_CUSTOM_CATEGORIES - len(st.custom)):
        st.ensure_custom(f"Niche number {i}")
    try:
        w.cmd_collect(custom_category="One Too Many", **args)
        raise AssertionError("cap not enforced")
    except JobRejected as exc:
        assert exc.status == 400 and "limit" in exc.detail
    w.cmd_collect(custom_category="Solar Installers", **args)   # existing: still fine
    assert len(st.custom) == worker_mod.MAX_CUSTOM_CATEGORIES
    print("  rejected submit leaves no custom category; per-user cap: OK")


# --------------------------------------------------------------------------- #
# 4. Unicode canonical keys
# --------------------------------------------------------------------------- #

def _old_phrase(phrase):
    toks = [discovery._stem(t) for t in re.findall(r"[a-z0-9&+#]+", phrase.lower())]
    keep = sorted({t for t in toks if t not in discovery._FILLER
                   and t not in discovery._BIZ_NOUNS})
    return " ".join(keep) or " ".join(sorted(set(toks)))


def _old_geo(geo):
    return " ".join(re.findall(r"[a-z0-9]+", geo.lower()))


def test_unicode_keys():
    import random
    rnd = random.Random(7)
    alphabet = "abcXYZ019 &+#-_.,'/()!?@=\t" + "studios agency in Top best"
    samples = ["Top recruitment agencies in St. Louis", "3D rendering studios",
               "C++ & C# developers", "St.-Louis, MO 63101", "", "  ", "_a_b_",
               "AT&T stores", "best   law-firms (NY)"]
    samples += ["".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 30)))
                for _ in range(3000)]
    for s in samples:
        assert discovery.canon_phrase(s) == _old_phrase(s), repr(s)
        assert discovery.canon_geo(s) == _old_geo(s), repr(s)
    assert norm_query("Top recruitment agencies in St. Louis") == \
        norm_query("recruitment agency in St Louis")
    # non-Latin text no longer collapses
    assert norm_query("dentists in 東京") != norm_query("dentists in 大阪")
    assert norm_query("歯科 in Tokyo") != norm_query("弁護士 in Tokyo")
    assert ledger_key("places", "Zahnärzte in München") != \
        ledger_key("places", "Zahnärzte in Köln")
    assert discovery.canon_geo("दिल्ली") != discovery.canon_geo("मुंबई")
    assert discovery.canon_geo("दिल्ली") == "दिल्ली"           # marks kept in-word
    assert norm_query("ＤＥＮＴＩＳＴＳ in Ｔｏｋｙｏ") == norm_query("dentists in tokyo")
    print("  non-Latin keys distinct; ASCII keys identical to before: OK")


# --------------------------------------------------------------------------- #
# 5. ledger CSV
# --------------------------------------------------------------------------- #

def test_csv_injection():
    led = CreditLedger()
    evil = ["=HYPERLINK(\"http://x\")", "+1+1", "-2+3", "@SUM(A1)", "\tTAB", "\rCR"]
    for i, q in enumerate(evil):
        led.record(f"k{i}", kind="places", query=q, page=1, category=q,
                   credits=1, results=1, new=0)
    led.record("ok", kind="places", query="dentists in Austin", page=1,
               category="Finance", credits=1, results=1, new=0)
    path = os.path.join(_tmp, "ledger.csv")
    led.write_csv(path)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    by_query = {r["query"]: r for r in rows}
    for q in evil:
        assert "'" + q in by_query, (q, list(by_query))
        assert by_query["'" + q]["category"] == "'" + q
    assert "dentists in Austin" in by_query                 # normal text untouched
    assert by_query["dentists in Austin"]["category"] == "Finance"
    print("  ledger CSV cells starting with = + - @ tab CR are quoted: OK")


if __name__ == "__main__":
    tests = [test_server_spend_charged_after_switch_to_own_key,
             test_own_key_then_server_key_is_capped,
             test_queued_job_switching_to_exhausted_server_key_stops,
             test_spend_persists_across_restart,
             test_timeouts_and_reported_credits,
             test_custom_category_rollback_and_cap,
             test_unicode_keys,
             test_csv_injection]
    for t in tests:
        print(t.__name__)
        t()
    assert threading.active_count() >= 1
    print("ALL BUGFIX CREDIT TESTS PASSED")
