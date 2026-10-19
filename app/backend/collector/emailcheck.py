"""Email verification: is a collected address real, and does its mailbox exist?

Combines the strongest parts of three open-source verifiers:

- python-email-validator (JoshData, Unlicense): format rules and whether the
  domain has mail servers (MX records, or the A/AAAA fallback) - its DNS check.
- koprjaa/email-verifier (MIT): the mailbox probe. Connect to the mail server
  only at an address checked to be public (an MX record is controlled by
  whoever owns the domain, so following it blindly could reach a private
  network), ask RCPT TO without sending anything, detect accept-all domains
  with a random address, and read 552/553/554 and "bad reputation" refusals as
  inconclusive rather than "no such mailbox".
- ahmadthedev/email-verifier (MIT): the valid / risky / invalid / unknown
  verdict and the 0-100 score. Generic addresses (info@, sales@) are NOT
  marked down here: for business contacts they are the normal case.

No email is ever sent: the probe stops after RCPT TO and says QUIT.
"""

from __future__ import annotations

import logging
import random
import re
import smtplib
import socket
import string
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import dns.exception
import dns.resolver
from email_validator import (EmailNotValidError, EmailSyntaxError, caching_resolver,
                             validate_email)

from .. import config
from .ssrf import _ip_is_public

try:   # maintained list of ~10,000 throwaway-mail domains
    from disposable_email_domains import blocklist as _DISPOSABLE
except ImportError:   # optional: the check is skipped without it
    _DISPOSABLE = frozenset()

log = logging.getLogger("emailcheck")

VALID, RISKY, INVALID, UNKNOWN = "Valid", "Risky", "Invalid", "Unknown"

DNS_TIMEOUT = 5
CACHE_TTL_S = 6 * 3600
UNKNOWN_TTL_S = 5 * 60           # an Unknown verdict (DNS timeout, error, ...) is retried soon
CACHE_MAX = 20_000
PORT25_OK_TTL_S = 6 * 3600       # re-test an open port 25 this often
PORT25_BLOCKED_TTL_S = 30 * 60   # ... and a blocked one this often
PORT25_TEST_DOMAIN = "gmail.com"
MAX_MX_HOSTS = 2
SMTP_PORT = 25                   # (tests point this at a local fake server)

# Codes that mean "ask again later" (koprjaa): 421/45x are temporary by the
# standard; 552/553/554 are permanent, but servers send them when they dislike
# the sender or connection, so they say nothing about the mailbox.
TEMPORARY_CODES = {421, 450, 451, 452, 454, 458, 459, 471, 472, 552, 553, 554}
TEMPORARY_WORDS = ("temporar", "try again", "later", "busy", "overloaded",
                   "rate limit", "throttl", "too many", "greylist", "graylist")
# Refusals of the SENDER (our address, name or IP), not of the recipient.
# Measured: "550 Sender () has no A, AAAA, or MX DNS records" answers RCPT TO
# on some servers - it says nothing about the mailbox.
REPUTATION_WORDS = ("reputation", "spamhaus", "blocklist", "blacklist", "blocked",
                    "spam", "policy", "not allowed", "access denied", "dnsbl",
                    "sender", "mail from", "reverse path", "envelope", "helo", "ehlo",
                    "ptr", "reverse dns", "rdns", "client host", "authentication")
# Explicit "this mailbox does not exist" wording. Checked FIRST: Postfix's
# standard "Recipient address rejected: User unknown" contains words that the
# reputation list alone would misread as a sender refusal.
NO_MAILBOX_WORDS = ("user unknown", "unknown user", "no such user", "no such recipient",
                    "does not exist", "doesn't exist", "not exist", "no mailbox",
                    "mailbox unavailable", "mailbox not found", "recipient not found",
                    "invalid recipient", "unknown recipient", "address not found",
                    "user not found", "not a valid mailbox", "unrouteable", "5.1.1",
                    "account disabled", "account has been disabled", "inactive")


@dataclass(frozen=True)
class Verdict:
    email: str
    status: str      # Valid | Risky | Invalid | Unknown
    score: int       # 0-100: 80+ safe to email, 50-79 caution, below 50 don't
    detail: str      # plain-language reason


# -- shared state (process-wide: every job benefits from earlier answers) --------
_lock = threading.Lock()
_resolver = None
_verdicts: "OrderedDict[str, tuple[float, Verdict]]" = OrderedDict()
_catch_all: "OrderedDict[str, tuple[float, bool]]" = OrderedDict()
_port25: dict = {"ok": None, "until": 0.0}
_next_probe_at: dict[str, float] = {}
_slots = threading.BoundedSemaphore(config.EMAIL_SMTP_CONCURRENCY)


def _dns():
    global _resolver
    with _lock:
        if _resolver is None:
            _resolver = caching_resolver(timeout=DNS_TIMEOUT)
        return _resolver


def _cache_get(store: OrderedDict, key: str):
    with _lock:
        hit = store.get(key)
        if hit is None or hit[0] < time.monotonic():
            return None
        return hit[1]


def _cache_put(store: OrderedDict, key: str, value, ttl: float | None = None):
    with _lock:
        store[key] = (time.monotonic() + (CACHE_TTL_S if ttl is None else ttl), value)
        store.move_to_end(key)
        while len(store) > CACHE_MAX:
            store.popitem(last=False)


def _helo() -> str:
    name = config.EMAIL_VERIFY_HELO or socket.getfqdn()
    return name if "." in name else "localhost.localdomain"


def _public_ips(host: str) -> list[str]:
    """IPv4 addresses of a mail server that are on the public internet."""
    try:
        ans = _dns().resolve(host, "A")
    except (dns.exception.DNSException, OSError):
        return []
    return [r.address for r in ans if _ip_is_public(r.address)]


def _mx_hosts(domain: str) -> list[str]:
    try:
        ans = _dns().resolve(domain, "MX")
    except (dns.exception.DNSException, OSError):
        return []
    return [str(r.exchange).rstrip(".") for r in sorted(ans, key=lambda r: r.preference)]


def smtp_mode() -> str:
    m = config.EMAIL_SMTP_CHECK
    if m in ("off", "0", "no", "false"):
        return "off"
    if m in ("on", "1", "yes", "true"):
        return "on"
    return "auto"


def port25_open(force: bool = False) -> bool:
    """Whether this machine can reach mail servers on port 25 (tested against
    Gmail's, result cached). Many home connections and cloud hosts - Render's
    free plan among them - block it; mailbox probes are then skipped."""
    with _lock:
        if not force and _port25["ok"] is not None and time.monotonic() < _port25["until"]:
            return _port25["ok"]
    ok = False
    for host in _mx_hosts(PORT25_TEST_DOMAIN)[:2]:
        for ip in _public_ips(host)[:1]:
            try:
                with socket.create_connection((ip, 25), timeout=6):
                    ok = True
            except OSError:
                pass
        if ok:
            break
    with _lock:
        _port25["ok"] = ok
        _port25["until"] = time.monotonic() + (PORT25_OK_TTL_S if ok else PORT25_BLOCKED_TTL_S)
    log.info("outbound port 25 %s - mailbox checks %s", "open" if ok else "BLOCKED",
             "on" if ok else "skipped")
    return ok


def mailbox_checks_enabled() -> bool:
    mode = smtp_mode()
    return mode == "on" or (mode == "auto" and port25_open())


def classify_reply(code: int | None, message: str) -> str:
    """accepted | rejected | inconclusive, for one RCPT TO reply."""
    if code is None:
        return "inconclusive"
    if code in (250, 251):
        return "accepted"
    msg = (message or "").lower()
    if 500 <= code < 600 and any(w in msg for w in NO_MAILBOX_WORDS):
        return "rejected"
    if code in TEMPORARY_CODES or any(w in msg for w in TEMPORARY_WORDS):
        return "inconclusive"
    if any(w in msg for w in REPUTATION_WORDS):
        return "inconclusive"
    return "rejected" if 500 <= code < 600 else "inconclusive"


def _pace(host: str):
    """At most one probe per EMAIL_SMTP_HOST_INTERVAL seconds per mail server."""
    with _lock:
        now = time.monotonic()
        at = max(now, _next_probe_at.get(host, 0.0))
        _next_probe_at[host] = at + config.EMAIL_SMTP_HOST_INTERVAL
        if len(_next_probe_at) > CACHE_MAX:
            _next_probe_at.clear()
    if at > now:
        time.sleep(at - now)


_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _decode(msg) -> str:
    return msg.decode("utf-8", "replace") if isinstance(msg, bytes) else str(msg or "")


def _shown(code: int, msg) -> str:
    """A server reply as stored in the Excel file: short, and without this
    machine's own IP address (refusals often quote it)."""
    return f"{code} {_IPV4_RE.sub('[this server]', _decode(msg))[:120]}"


def _probe(email: str, domain: str, mx: list[str]) -> tuple[str, bool | None, str]:
    """Ask the domain's mail server whether `email` exists (and, once per
    domain, whether it accepts ANY address). Returns (outcome, accept_all,
    server reply). Nothing is sent: MAIL FROM, RCPT TO, QUIT."""
    accept_all = _cache_get(_catch_all, domain)
    random_rcpt = None
    if accept_all is None:
        random_rcpt = "".join(random.choices(string.ascii_lowercase + string.digits, k=18)) + "@" + domain
    sender = config.EMAIL_VERIFY_FROM
    last = "no reachable mail server"
    with _slots:
        for host in mx[:MAX_MX_HOSTS]:
            ips = _public_ips(host)
            if not ips:
                last = f"mail server {host} has no public address"
                continue
            _pace(host)
            try:
                with smtplib.SMTP(timeout=config.EMAIL_SMTP_TIMEOUT,
                                  local_hostname=_helo()) as s:
                    s.connect(ips[0], SMTP_PORT)
                    s.ehlo_or_helo_if_needed()
                    code, msg = s.docmd("MAIL", f"FROM:<{sender}>")
                    if code >= 400:
                        return "inconclusive", accept_all, _shown(code, msg)
                    code, msg = s.docmd("RCPT", f"TO:<{email}>")
                    reply = _shown(code, msg)
                    outcome = classify_reply(code, _decode(msg))
                    if random_rcpt and outcome == "accepted":
                        rc, rm = s.docmd("RCPT", f"TO:<{random_rcpt}>")
                        probe = classify_reply(rc, _decode(rm))
                        if probe != "inconclusive":
                            accept_all = probe == "accepted"
                            _cache_put(_catch_all, domain, accept_all)
                    try:
                        s.docmd("RSET")
                    except smtplib.SMTPException:
                        pass
                    return outcome, accept_all, reply
            except (OSError, smtplib.SMTPException) as exc:
                last = f"{host}: {type(exc).__name__} {str(exc)[:80]}"
                continue
    return "inconclusive", accept_all, last


def _disposable(domain: str) -> bool:
    if not _DISPOSABLE:
        return False
    parts = domain.split(".")
    return any(".".join(parts[i:]) in _DISPOSABLE for i in range(len(parts) - 1))


def verify(email: str) -> Verdict:
    """Full check of one address; never raises. Cached per address."""
    email = (email or "").strip().strip(".;,").lower()
    hit = _cache_get(_verdicts, email)
    if hit is not None:
        return hit
    try:
        v = _verify(email)
    except Exception as exc:   # one odd address must never stop a run
        log.exception("email check failed for %s", email)
        v = Verdict(email, UNKNOWN, 50, f"check failed ({type(exc).__name__})")
    # an inconclusive probe (busy server, greylisting) is retried next time;
    # any other Unknown (DNS timeout, check failed, port 25 blocked) is a
    # transient condition, not an answer about the mailbox: kept only briefly
    if "no clear answer" in v.detail:
        pass
    elif v.status == UNKNOWN:
        _cache_put(_verdicts, email, v, UNKNOWN_TTL_S)
    else:
        _cache_put(_verdicts, email, v)
    return v


# -- SPF -------------------------------------------------------------------------
# SPF ("v=spf1 ..." in the domain's DNS TXT records) lists the servers allowed
# to send the domain's mail. A real, active business domain almost always has
# one; none at all is a weak warning sign, and "v=spf1 -all" alone declares
# that the domain sends no email at all (typical of parked domains).
SPF_OK, SPF_NONE, SPF_SENDS_NONE, SPF_UNKNOWN = "ok", "none", "sends-none", "unknown"
SPF_PENALTY = {SPF_OK: 0, SPF_UNKNOWN: 0, SPF_NONE: 10, SPF_SENDS_NONE: 20}
_spf_cache: "OrderedDict[str, tuple[float, tuple[str, str]]]" = OrderedDict()


def spf_check(domain: str) -> tuple[str, str]:
    """(status, the SPF record) of a domain; cached per domain."""
    hit = _cache_get(_spf_cache, domain)
    if hit is not None:
        return hit
    # TXT answers are often large: over UDP they get truncated or lost
    # (measured: gmail.com 6.2 s over UDP - a timeout - vs 0.0 s over TCP),
    # so TCP first, UDP as the fallback.
    texts = None
    for tcp in (True, False):
        try:
            ans = _dns().resolve(domain, "TXT", tcp=tcp)
            texts = [b"".join(r.strings).decode("utf-8", "replace") for r in ans]
            break
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            texts = []
            break
        except (dns.exception.DNSException, OSError):
            continue
    if texts is None:
        return SPF_UNKNOWN, ""            # timeout: not cached, asked again later
    spf = next((t.strip() for t in texts if t.strip().lower().startswith("v=spf1")), "")
    if not spf:
        result = (SPF_NONE, "")
    elif spf.lower().split()[1:] == ["-all"]:
        result = (SPF_SENDS_NONE, spf)
    else:
        result = (SPF_OK, spf)
    _cache_put(_spf_cache, domain, result)
    return result


def _spf_note(status: str) -> str:
    return {SPF_OK: "SPF OK",
            SPF_NONE: "SPF missing (domain has no sender policy)",
            SPF_SENDS_NONE: "SPF says the domain sends no email (often a parked domain)",
            SPF_UNKNOWN: "SPF not checked (DNS timeout)"}[status]


def _verify(email: str) -> Verdict:
    """Checks in order: format -> domain -> DNS/MX -> throwaway -> SPF -> SMTP.
    The Email Check text lists the outcome of every check that ran."""
    done: list[str] = []

    def verdict(status: str, score: int, reason: str, spf: str = SPF_OK) -> Verdict:
        if status != INVALID:
            score = max(0, score - SPF_PENALTY[spf])
        return Verdict(email, status, score, " · ".join(done + [reason]))

    # 1) format, 2) domain exists, 3) DNS/MX mail servers (python-email-validator)
    try:
        info = validate_email(email, check_deliverability=True, dns_resolver=_dns())
    except EmailSyntaxError as exc:
        return verdict(INVALID, 0, f"Format: not a valid email address ({exc})")
    except EmailNotValidError as exc:   # EmailUndeliverableError
        done.append("Format OK")
        if "does not exist" in str(exc):
            return verdict(INVALID, 10, f"Domain: does not exist - cannot receive email ({exc})")
        done.append("Domain OK")
        return verdict(INVALID, 10, f"DNS/MX: domain cannot receive email ({exc})")
    email = info.normalized
    domain = info.ascii_domain
    done += ["Format OK", "Domain OK"]
    mx = [h for _, h in sorted(getattr(info, "mx", None) or [])]
    if not mx:
        return verdict(UNKNOWN, 50, "DNS/MX: mail servers could not be looked up (DNS timeout)")
    done.append(f"MX OK ({mx[0].rstrip('.')})")
    # 4) throwaway domain
    if _disposable(domain):
        return verdict(RISKY, 20, "throwaway (disposable) email service")
    # 5) SPF (sender policy in DNS)
    spf, _ = spf_check(domain)
    done.append(_spf_note(spf))
    # 6) SMTP: does the mailbox exist (where port 25 is open)
    mode = smtp_mode()
    if mode == "off":
        return verdict(UNKNOWN, 60, "SMTP: mailbox not checked "
                                    "(mailbox checks are turned off)", spf)
    if not mailbox_checks_enabled():
        return verdict(UNKNOWN, 60, "SMTP: mailbox not checked "
                                    "(port 25 is blocked on this server)", spf)
    outcome, accept_all, reply = _probe(email, domain, [h.rstrip(".") for h in mx])
    if outcome == "rejected":
        return verdict(INVALID, 5, f"SMTP: mail server says this mailbox does not exist ({reply})")
    if outcome == "accepted":
        if accept_all:
            return verdict(RISKY, 60, "SMTP: domain accepts every address (accept-all), "
                                      "so this mailbox cannot be confirmed", spf)
        if accept_all is False:
            return verdict(VALID, 100, "SMTP: mailbox exists (confirmed by its mail server)", spf)
        return verdict(VALID, 90, "SMTP: mailbox accepted by its mail server "
                                  "(accept-all test inconclusive)", spf)
    return verdict(UNKNOWN, 60, f"SMTP: no clear answer from the mail server ({reply})", spf)


def summarize(verdicts: list[Verdict]) -> dict:
    """Record fields for one company's addresses: the best address decides the
    status and score; the check column explains every address."""
    best = max(verdicts, key=lambda v: v.score)
    if len(verdicts) == 1:
        detail = best.detail
    else:
        detail = " | ".join(f"{v.email}: {v.status} - {v.detail}" for v in verdicts)
    return {"Email Status": best.status, "Email Score": best.score,
            "Email Check": detail[:500]}
