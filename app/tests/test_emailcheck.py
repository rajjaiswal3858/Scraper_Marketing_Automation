"""Email verification (backend/collector/emailcheck.py), offline: a fake mail
server on localhost answers the mailbox probe, DNS answers are stubbed.
Checks the verdicts (valid / risky / invalid / unknown), the reading of real
mail-server replies, accept-all detection, the throwaway-domain list, and
that a blocked port 25 skips the probe instead of guessing.
Run:  python tests/test_emailcheck.py  (from the app/ directory)"""

from __future__ import annotations

import os
import socket
import sys
import threading
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import config  # noqa: E402
from backend.collector import emailcheck as ec  # noqa: E402


class FakeMailServer:
    """Minimal SMTP server: knows `mailboxes`; accept_all answers 250 to all.
    Records every command so the test can prove nothing was ever sent."""

    def __init__(self, mailboxes=(), accept_all=False):
        self.mailboxes = {m.lower() for m in mailboxes}
        self.accept_all = accept_all
        self.commands: list[str] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._session, args=(conn,), daemon=True).start()

    def _session(self, conn):
        f = conn.makefile("rwb")

        def send(line):
            f.write(line.encode() + b"\r\n")
            f.flush()
        send("220 fake.mx ESMTP")
        for raw in f:
            line = raw.decode().strip()
            self.commands.append(line)
            verb = line.split(" ")[0].split(":")[0].upper()
            if verb in ("EHLO", "HELO"):
                send("250 fake.mx")
            elif verb == "MAIL":
                send("250 2.1.0 Ok")
            elif verb == "RCPT":
                addr = line.split("<", 1)[1].rstrip(">").lower()
                if self.accept_all or addr in self.mailboxes:
                    send("250 2.1.5 Ok")
                else:
                    send("550 5.1.1 <%s>: Recipient address rejected: User unknown" % addr)
            elif verb == "RSET":
                send("250 Ok")
            elif verb == "QUIT":
                send("221 Bye")
                break
            else:
                send("502 not implemented")
        conn.close()


def fake_validate(email, **_kw):
    local, _, domain = email.partition("@")
    if not local or "." not in domain or " " in email:
        raise ec.EmailSyntaxError("The email address is not valid.")
    if domain.endswith("nomail.example"):
        raise ec.EmailNotValidError("The domain name nomail.example does not accept email.")
    if domain.endswith("gone.example"):
        raise ec.EmailNotValidError("The domain name gone.example does not exist.")
    return SimpleNamespace(normalized=email, ascii_domain=domain,
                           mx=[(10, "mx." + domain)])


def reset():
    ec._verdicts.clear()
    ec._catch_all.clear()
    ec._next_probe_at.clear()


def main():
    # 1) reading real mail-server replies
    cr = ec.classify_reply
    assert cr(250, "2.1.5 Ok") == "accepted"
    assert cr(550, "5.1.1 <x@y.com>: Recipient address rejected: User unknown") == "rejected", \
        "Postfix 'user unknown' must count as no mailbox, not as a sender refusal"
    assert cr(550, "5.1.1 The email account that you tried to reach does not exist") == "rejected"
    assert cr(450, "4.2.0 Greylisted, please try again later") == "inconclusive"
    assert cr(554, "5.7.1 Service unavailable; client host blocked using Spamhaus") == "inconclusive"
    assert cr(550, "5.7.1 Message rejected due to poor reputation") == "inconclusive"
    assert cr(550, "Sender () has no A, AAAA, or MX DNS records.") == "inconclusive", \
        "a refusal of OUR sender says nothing about the mailbox"
    assert cr(None, "") == "inconclusive"
    shown = ec._shown(550, b"5.7.1 Client host [203.0.113.7] blocked using zen.spamhaus.org")
    assert "203.0.113.7" not in shown and "[this server]" in shown, shown
    print("  mail-server replies read correctly")

    # SPF: read from DNS TXT records (fake resolver, no network)
    class TXT:
        def __init__(self, *parts):
            self.strings = [p.encode() for p in parts]
    zone = {"good.example": [TXT("google-site-verification=x"), TXT("v=spf1 include:_spf.", "google.com ~all")],
            "parked.example": [TXT("v=spf1 -all")],
            "nospf.example": [TXT("some other record")]}

    class FakeResolver:
        def resolve(self, name, rtype, tcp=False):
            assert rtype == "TXT"
            if name not in zone:
                raise ec.dns.resolver.NXDOMAIN()
            return zone[name]
    ec._resolver = FakeResolver()
    assert ec.spf_check("good.example") == ("ok", "v=spf1 include:_spf.google.com ~all")
    assert ec.spf_check("parked.example")[0] == "sends-none"
    assert ec.spf_check("nospf.example")[0] == "none"
    assert ec.spf_check("missing.example")[0] == "none"
    print("  SPF read from DNS: present / missing / 'sends no email' told apart")

    # stubs: no real DNS, mail server on localhost
    spf_of = {"acme.example": "ok", "nospf.example": "none", "parked.example": "sends-none"}
    ec.spf_check = lambda d: (spf_of.get(d, "ok"), "")
    ec.validate_email = fake_validate
    ec._public_ips = lambda host: ["127.0.0.1"]
    config.EMAIL_SMTP_HOST_INTERVAL = 0.0

    server = FakeMailServer(mailboxes=["sales@acme.example"])
    ec.SMTP_PORT = server.port
    config.EMAIL_SMTP_CHECK = "on"
    reset()

    # 2) format / domain
    v = ec.verify("not an email")
    assert v.status == ec.INVALID and v.score == 0, v
    v = ec.verify("info@nomail.example")
    assert v.status == ec.INVALID and "cannot receive email" in v.detail, v
    assert v.detail.startswith("Format OK · Domain OK · DNS/MX:"), v.detail
    v = ec.verify("info@gone.example")
    assert v.status == ec.INVALID and v.detail.startswith("Format OK · Domain: does not exist"), v
    print("  bad format, non-existent domain, mail-less domain -> Invalid (failed check named)")

    # 3) mailbox exists / does not exist (domain is NOT accept-all)
    v = ec.verify("Sales@Acme.example")
    assert v.status == ec.VALID and v.score == 100, v
    assert v.detail.startswith("Format OK · Domain OK · MX OK (mx.acme.example) · SPF OK · SMTP: "
                               "mailbox exists"), v.detail
    v = ec.verify("nobody@acme.example")
    assert v.status == ec.INVALID and "does not exist" in v.detail, v
    assert not any(c.upper().startswith(("DATA", "BDAT")) for c in server.commands), \
        "the probe must never send a message"
    print("  existing mailbox -> Valid 100, missing mailbox -> Invalid; no email sent")

    # SPF lowers confidence, never decides on its own
    server3 = FakeMailServer(mailboxes=["sales@nospf.example", "sales@parked.example"])
    ec.SMTP_PORT = server3.port
    ec._verdicts.clear()
    ec._catch_all.clear()
    v = ec.verify("sales@nospf.example")
    assert v.status == ec.VALID and v.score == 90 and "SPF missing" in v.detail, v
    v = ec.verify("sales@parked.example")
    assert v.status == ec.VALID and v.score == 80 and "sends no email" in v.detail, v
    ec.SMTP_PORT = server.port
    print("  SPF missing -10, 'sends no email' -20; mailbox result still decides the status")

    # 4) accept-all domain: a random address is accepted too -> Risky
    server2 = FakeMailServer(accept_all=True)
    ec.SMTP_PORT = server2.port
    v = ec.verify("anyone@catchall.example")
    assert v.status == ec.RISKY and "accept-all" in v.detail, v
    print("  accept-all domain -> Risky")

    # 5) throwaway domain (maintained list) -> Risky without any probe
    n = len(server2.commands)
    v = ec.verify("temp@mailinator.com")
    assert v.status == ec.RISKY and "throwaway" in v.detail, v
    assert len(server2.commands) == n, "throwaway address must not be probed"
    print("  throwaway domain -> Risky (list of %d domains)" % len(ec._DISPOSABLE))

    # 6) port 25 blocked: no probe, honest Unknown
    reset()
    config.EMAIL_SMTP_CHECK = "auto"
    ec.port25_open = lambda force=False: False
    v = ec.verify("sales@acme.example")
    assert v.status == ec.UNKNOWN and "port 25 is blocked" in v.detail, v
    config.EMAIL_SMTP_CHECK = "off"
    reset()
    v = ec.verify("sales@acme.example")
    assert v.status == ec.UNKNOWN and "turned off" in v.detail, v
    print("  port 25 blocked / checks off -> Unknown, mailbox not probed")

    # 7) one company, several addresses: the best decides
    config.EMAIL_SMTP_CHECK = "on"
    ec.SMTP_PORT = server.port
    reset()
    f = ec.summarize([ec.verify("nobody@acme.example"), ec.verify("sales@acme.example")])
    assert f["Email Status"] == ec.VALID and f["Email Score"] == 100, f
    assert "nobody@acme.example: Invalid" in f["Email Check"], f
    print("  several addresses: best one decides, every one explained")

    # 8) record credibility follows the email check; one overall status
    from backend.collector.validator import confidence, record_status
    base = {"Company Name": "Acme Tax Partners", "Official Website": "https://acme.example",
            "Business Email": "info@acme.example", "Business Phone": "+1 512 555 0100",
            "Full Business Address": "1 Main St, Austin, TX", "Industry": "Accounting"}
    terms = ["tax", "accounting"]
    unchecked = confidence(base, terms)
    valid = dict(base, **{"Email Status": "Valid", "Email Score": 100})
    dead = dict(base, **{"Email Status": "Invalid", "Email Score": 5})
    assert unchecked == confidence(valid, terms) == 100
    assert confidence(dead, terms) == 81, confidence(dead, terms)   # 19 of 20 email points lost
    valid["Confidence Score"] = confidence(valid, terms)
    dead["Confidence Score"] = confidence(dead, terms)
    assert record_status(valid) == "Verified"
    assert record_status(dead) == "Needs review", "a proven-dead email needs review"
    no_email = {k: v for k, v in base.items() if k != "Business Email"}
    no_email["Confidence Score"] = confidence(no_email, terms)
    assert no_email["Confidence Score"] == 80 and record_status(no_email) == "Likely valid"
    thin = {"Company Name": "x", "Business Phone": "1", "Confidence Score": 30}
    assert record_status(thin) == "Low quality"
    print("  credibility follows the email check; Verified / Likely valid / Needs review / Low quality")

    print("\nALL EMAIL CHECK TESTS PASSED")


if __name__ == "__main__":
    main()
