"""Normalization helpers shared by extraction, validation and deduplication.
Ported from business_collector.py."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

ILLEGAL_XLSX_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
LEGAL_SUFFIX_RE = re.compile(
    r"\b(ltd|limited|llc|l\.l\.c|inc|incorporated|llp|l\.l\.p|pvt|private|plc|"
    r"corp|corporation|co|company|gmbh|ag|sa|s\.a|pte|sdn|bhd|bv|b\.v|nv|"
    r"pty|pllc|pc|p\.c|lp|group|holdings?)\b\.?", re.I,
)
MULTI_PART_SECOND_LEVEL = {
    "co", "com", "org", "net", "gov", "ac", "edu", "gen", "firm", "ltd",
    "plc", "nic", "or", "ne", "biz", "info",
}


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = ILLEGAL_XLSX_RE.sub("", str(s))
    return re.sub(r"\s+", " ", s).strip()


def norm_domain(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    netloc = urlparse(url).netloc.lower()
    netloc = netloc.split("@")[-1].split(":")[0]
    netloc = netloc.rstrip(".")   # "example.com." (fully qualified) == "example.com"
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def root_domain(url: str) -> str:
    d = norm_domain(url)
    parts = [p for p in d.split(".") if p]
    if len(parts) >= 3 and parts[-2] in MULTI_PART_SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return d


def norm_name(name: str) -> str:
    """Dedup key of a company name: legal suffixes dropped, accents folded
    ("Café Müller" == "Cafe Muller"), letters/digits of any script kept
    (IBM, 3M, H&M, 株式会社..., Яндекс). A pure-ASCII name gives the same key as
    the old [a-z0-9]-only rule, so stored registries stay valid."""
    n = LEGAL_SUFFIX_RE.sub(" ", (name or "").lower())
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c)).casefold()
    n = "".join(c for c in n if c.isalnum())
    return n if len(n) >= 2 else ""


def norm_email(email: str) -> str:
    return (email or "").strip().lower()


def norm_phone(phone: str) -> str:
    """Dedup key of a phone number. National forms keep their last 10 digits
    (as before). A number written with an international prefix ("+" or "00")
    keeps its full digit string, so "+44 20 7946 0958" no longer equals
    "+1 207 946 0958" - except NANP "+1 NXX..." numbers, which map to their
    10 national digits so "+1 555 010 2030" == "(555) 010-2030"."""
    phone = phone or ""
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 8:
        return ""
    first = re.search(r"[+\d]", phone)
    if first.group() == "+":
        intl = digits
    elif digits.startswith("00"):
        intl = digits[2:]
    else:
        return digits[-10:]
    if intl.startswith("1") and len(intl) == 11:   # NANP: +1 + 10 digits
        return intl[1:]
    return intl if len(intl) >= 8 else digits[-10:]


# digits with phone punctuation only ("+91 98765 43210", "+1 (555) 010-2030"):
# no formula can be built from these, so they keep their leading "+"
_PHONE_LIKE_RE = re.compile(r"\+?[\d\s().\-/]+")


def xlsx_safe(value) -> str:
    s = clean_text(value)
    if s[:1] in ("=", "+", "@") and not _PHONE_LIKE_RE.fullmatch(s):
        s = " " + s
    return s[:32000]
