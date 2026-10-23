"""About Us pages, Facebook page links and Bing search - WITHOUT network
access or credits. A fake Serper provider, a fake Bing provider and a fake
fetcher feed canned pages through the real engine.
Run:  python tests/test_about_bing.py   (from the app/ directory)
"""

from __future__ import annotations

import os
import sys
from _scratch import scratch_dir  # noqa: E402  (tests/_scratch.py)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import config  # noqa: E402

_tmp = scratch_dir(prefix="about_bing_test_")
config.OUTPUT_DIR = _tmp
config.STATE_PATH = os.path.join(_tmp, "demo_state.json")

from backend.collector import dom, emailcheck, engine  # noqa: E402
from backend.collector.crawler import find_contact_links, is_about_url  # noqa: E402
from backend.collector.engine import CollectionJob, StateStore  # noqa: E402
from backend.collector.search import BingProvider, SearchProvider  # noqa: E402

emailcheck.verify = lambda e: emailcheck.Verdict(e, "Valid", 100, "test: mailbox exists")

PAGES = {
    # homepage complete (email + phone + address): only the About Us page
    # is read, and it adds a second email ID and the Facebook page
    "http://alpharpo.example": """
      <html><head><title>Alpha RPO Partners | Recruitment Outsourcing</title></head>
      <body><a href="mailto:info@alpharpo.example">mail</a>
      <a href="tel:+912233445566">call</a>
      <address>45 Business Park, Mumbai, Maharashtra 400001, India</address>
      <a href="/contact">Contact</a> <a href="/about-us">About Us</a></body></html>""",
    "http://alpharpo.example/about-us": """
      <html><head><title>About Alpha RPO</title></head>
      <body><p>Write to hr@alpharpo.example</p>
      <a href="https://www.facebook.com/AlphaRPO">Facebook</a></body></html>""",
    # no email on the homepage, no Contact page: the email is on About Us
    "http://betahire.example": """
      <html><head><title>Beta Hire Solutions - RPO Services</title></head>
      <body><a href="tel:+914455667788">call</a>
      <a href="/who-we-are">Who we are</a></body></html>""",
    "http://betahire.example/who-we-are": """
      <html><head><title>Who we are</title></head>
      <body><a href="mailto:team@betahire.example">mail</a></body></html>""",
    # only Bing returns this firm
    "http://gammatalent.example": """
      <html><head><title>Gamma Talent RPO</title></head>
      <body><a href="mailto:hello@gammatalent.example">mail</a>
      <a href="tel:+918899001122">call</a></body></html>""",
    # no email anywhere on the site, but its own Facebook page: Google's
    # snippet has no email, Bing's does
    "http://deltarecruit.example": """
      <html><head><title>Delta Recruit RPO</title></head>
      <body><a href="tel:+917766554433">call</a>
      <a href="https://facebook.com/deltarecruit">fb</a></body></html>""",
}


class FakeFetcher:
    def fetch_raw(self, url: str):
        html = PAGES.get(url.rstrip("/"))
        if html is None:
            return None, url, "connection/DNS error"
        return html.encode("utf-8"), url, ""


class FakeSerper(SearchProvider):
    name = "fake"
    supports_places = False

    def __init__(self, urls):
        self._urls, self._served, self.credits_used = urls, False, 0

    def organic(self, query, max_results):
        self.credits_used += 1
        if self._served:
            return []
        self._served = True
        return list(self._urls)

    def organic_items(self, query):
        self.credits_used += 1
        return [{"link": "https://www.facebook.com/deltarecruit",
                 "title": "Delta Recruit", "snippet": "Recruitment agency in Pune"}]


class FakeBing(BingProvider):
    def __init__(self, urls):
        self._urls, self.calls = urls, []
        self.disabled_reason = ""

    def organic_page(self, query, page=1):
        self.calls.append(("organic", query, page))
        return list(self._urls) if len(self.calls) == 1 else []

    def organic_items(self, query):
        self.calls.append(("social", query, 1))
        return [{"link": "https://www.facebook.com/deltarecruit/about",
                 "title": "Delta Recruit - About",
                 "snippet": "Email: contact@deltarecruit.example"}]

    def balance(self):
        return 99


def run(google, bing_urls, target=10):
    state = StateStore()
    job = CollectionJob(state, "RPO", ["RPO"], "India", target, "serper", 2)
    job.fetcher = FakeFetcher()
    bing = FakeBing(bing_urls)
    real_make, real_bing = engine.make_provider, engine.make_bing_provider
    engine.make_provider = lambda *_: FakeSerper(google)
    engine.make_bing_provider = lambda: bing
    try:
        job._run()
    finally:
        engine.make_provider, engine.make_bing_provider = real_make, real_bing
    recs = {r["Company Name"]: r for r in state.records["RPO"]}
    return job, bing, recs


def test_links():
    doc = dom.parse(PAGES["http://alpharpo.example"].encode())
    links = find_contact_links(doc, "http://alpharpo.example")
    assert links == ["http://alpharpo.example/contact",
                     "http://alpharpo.example/about-us"], links
    doc = dom.parse(b'<a href="/team">Team</a><a href="/about">About</a>'
                    b'<a href="/locations">Offices</a>')
    links = find_contact_links(doc, "http://x.example")
    assert links[0] == "http://x.example/about", links   # About beats Offices
    assert is_about_url("http://x.example/about-us") and is_about_url("http://x.example/who-we-are")
    assert not is_about_url("http://x.example/contact")
    print("ok contact page first, About Us page second")


def test_pipeline():
    job, bing, recs = run(
        google=["http://alpharpo.example", "http://betahire.example",
                "http://deltarecruit.example"],
        bing_urls=["http://alpharpo.example", "http://gammatalent.example"])

    a = recs["Alpha RPO Partners"]
    assert a["Business Email"] == "info@alpharpo.example; hr@alpharpo.example", a
    assert a["Facebook Page"] == "https://www.facebook.com/alpharpo", a
    assert a["Found Via"] == "Google"
    print("ok About Us page read on a complete homepage: extra email ID + Facebook page")

    b = recs["Beta Hire Solutions"]
    assert b["Business Email"] == "team@betahire.example", b
    assert b["Email Source"] == "http://betahire.example/who-we-are", b
    print("ok email ID found on the About Us page")

    g = recs["Gamma Talent RPO"]
    assert g["Found Via"] == "Bing", g
    assert job.counters["valid_bing"] == 1 and job.counters["bing_new"] == 1
    assert ("organic", bing.calls[0][1], 1) == bing.calls[0]
    print("ok Bing-only firm collected and marked 'Bing'; firm on both engines crawled once")

    d = recs["Delta Recruit RPO"]
    assert d["Business Email"] == "contact@deltarecruit.example", d
    assert d["Facebook Page"] == "https://www.facebook.com/deltarecruit", d
    assert job.counters["bing_social_emails"] == 1
    print("ok Facebook page email found through Bing when Google shows none")

    # Bing never spends Serper credits and is capped per run
    assert job.counters["bing_searches"] == len(bing.calls)
    config.BING_MAX_SEARCHES_PER_RUN, old = 1, config.BING_MAX_SEARCHES_PER_RUN
    try:
        job2, bing2, _ = run(google=["http://deltarecruit.example"],
                             bing_urls=["http://gammatalent.example"])
    finally:
        config.BING_MAX_SEARCHES_PER_RUN = old
    assert len(bing2.calls) == 1 and "cap" in job2._bing_off, (bing2.calls, job2._bing_off)
    print("ok Bing search cap per run")


def test_bing_off():
    config.ABOUT_PAGE_CRAWL, old = 0, config.ABOUT_PAGE_CRAWL
    state = StateStore()
    job = CollectionJob(state, "RPO", ["RPO"], "India", 5, "serper", 1)
    job.fetcher = FakeFetcher()
    real_make, real_bing = engine.make_provider, engine.make_bing_provider
    engine.make_provider = lambda *_: FakeSerper(["http://alpharpo.example"])
    engine.make_bing_provider = lambda: None
    try:
        job._run()
    finally:
        engine.make_provider, engine.make_bing_provider = real_make, real_bing
        config.ABOUT_PAGE_CRAWL = old
    rec = state.records["RPO"][0]
    assert rec["Business Email"] == "info@alpharpo.example" and not rec["Facebook Page"]
    assert job.counters["bing_searches"] == 0 and job.counters["about_pages"] == 0
    print("ok without a Bing key / with ABOUT_PAGE_CRAWL=0: Google only, homepage only")


def main():
    test_links()
    test_pipeline()
    test_bing_off()
    print("\nALL ABOUT/BING TESTS PASSED")


if __name__ == "__main__":
    main()
