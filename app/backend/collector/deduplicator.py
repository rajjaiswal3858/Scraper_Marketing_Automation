"""Cross-category deduplication on normalized domain, company name, email and
phone. The registry persists in the checkpoint so a company found again in a
later run or another category is recognized as a duplicate."""

from __future__ import annotations

import threading
from typing import Callable

from .normalize import norm_domain, norm_email, norm_name, norm_phone


class DedupRegistry:
    def __init__(self):
        self.domains: dict[str, str] = {}   # key -> "Category|Company"
        self.names: dict[str, str] = {}
        self.emails: dict[str, str] = {}
        self.phones: dict[str, str] = {}
        # name key -> [domain/cid, phone, ...] of the record that registered
        # it: a same-name record with a different website or phone is another
        # business ("Smile Dental" Austin vs Dallas), not a duplicate
        self.name_ids: dict[str, list[str]] = {}
        self.lock = threading.Lock()

    def _keys(self, record: dict) -> tuple[str, str, list[str], list[str]]:
        dom = norm_domain(record.get("Official Website", ""))
        if not dom and record.get("_cid"):
            dom = f"cid:{record['_cid']}"
        nm = norm_name(record.get("Company Name", ""))
        emails = [norm_email(e) for e in record.get("Business Email", "").split(";") if norm_email(e)]
        phones = [norm_phone(p) for p in record.get("Business Phone", "").split(";") if norm_phone(p)]
        return dom, nm, emails, phones

    def _name_dup(self, nm: str, dom: str, phones: list[str]) -> bool:
        """A name match is a duplicate unless the two records conflict: both
        have a website (or both a Places cid) and they differ, or both have
        phones and none is shared. Names registered before this rule (no
        stored ids) still match on the name alone. Caller holds self.lock."""
        if not nm or nm not in self.names:
            return False
        ids = self.name_ids.get(nm)
        if not ids:
            return True
        odom, ophones = ids[0], ids[1:]
        if dom and odom and dom != odom and dom.startswith("cid:") == odom.startswith("cid:"):
            return False
        if phones and ophones and not set(phones) & set(ophones):
            return False
        return True

    def _match(self, dom: str, nm: str, emails: list[str], phones: list[str]) -> str:
        """Owner of the record this one duplicates, else "" (caller holds lock)."""
        if dom and dom in self.domains:
            return self.domains[dom]
        if self._name_dup(nm, dom, phones):
            return self.names[nm]
        for e in emails:
            if e in self.emails:
                return self.emails[e]
        for p in phones:
            if p in self.phones:
                return self.phones[p]
        return ""

    def peek(self, record: dict) -> str:
        """Non-mutating duplicate probe: the owner of an existing record that
        matches on domain/cid, name, email or phone, else "". Lets the engine
        skip crawling a business it already has."""
        keys = self._keys(record)
        with self.lock:
            return self._match(*keys)

    def check_and_add(self, record: dict, category: str,
                      commit: Callable[[], bool] | None = None) -> tuple[bool, str]:
        """Atomically test for duplicates and register the record.
        Returns (added, duplicate_of) — duplicate_of names the earlier record.
        `commit` (e.g. storing the record against a target) runs under the
        lock once the record is known to be new; if it returns False nothing
        is registered and (False, "") is returned, so a record refused for a
        full target is never left behind as a phantom duplicate."""
        dom, nm, emails, phones = self._keys(record)
        owner = f"{category}|{record.get('Company Name', '')[:60]}"
        with self.lock:
            dup_of = self._match(dom, nm, emails, phones)
            if dup_of:
                return False, dup_of
            if commit is not None and not commit():
                return False, ""
            if dom:
                self.domains[dom] = owner
            if nm and nm not in self.names:   # a same-name distinct business keeps the first owner
                self.names[nm] = owner
                self.name_ids[nm] = [dom] + phones
            for e in emails:
                self.emails[e] = owner
            for p in phones:
                self.phones[p] = owner
            return True, ""

    def ensure(self, record: dict, category: str) -> int:
        """Register any missing keys of an already-stored record (checkpoint
        repair: the registry snapshot can predate the newest records).
        Existing owners are never overwritten. Returns keys added."""
        dom, nm, emails, phones = self._keys(record)
        owner = f"{category}|{record.get('Company Name', '')[:60]}"
        added = 0
        with self.lock:
            # also backfills the ids of a name registered before they existed
            if nm and (nm not in self.names
                       or (nm not in self.name_ids and self.names[nm] == owner)):
                self.name_ids[nm] = [dom] + phones
            for table, keys in ((self.domains, [dom]), (self.names, [nm]),
                                (self.emails, emails), (self.phones, phones)):
                for k in keys:
                    if k and k not in table:
                        table[k] = owner
                        added += 1
        return added

    # -- persistence ---------------------------------------------------------
    def to_dict(self) -> dict:
        with self.lock:   # copies: serialised outside the lock
            return {"domains": dict(self.domains), "names": dict(self.names),
                    "emails": dict(self.emails), "phones": dict(self.phones),
                    "name_ids": {k: list(v) for k, v in self.name_ids.items()
                                 if k in self.names}}

    @classmethod
    def from_dict(cls, data: dict) -> "DedupRegistry":
        reg = cls()
        reg.domains = dict(data.get("domains", {}))
        reg.names = dict(data.get("names", {}))
        reg.emails = dict(data.get("emails", {}))
        reg.phones = dict(data.get("phones", {}))
        reg.name_ids = {k: list(v) for k, v in data.get("name_ids", {}).items()
                        if k in reg.names}
        return reg
