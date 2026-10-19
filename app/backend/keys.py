"""Per-user Serper API keys.

Every user (account, or anonymous browser id) may save their own Serper key;
their collections then spend THEIR credits. A user without one falls back to
the server's key (SERPER_API_KEY in app/.env), if the operator set one.

A saved key never leaves the server again: the API only ever answers with
a masked form ("....abcd"), and keys are looked up by the caller's own
identity, which the request guard derives from the session / client id - so
no request can name another user's key. Storage:
  * Postgres table bdc.user_keys when SUPABASE_DB_URL is set (row level
    security on, no policies: the publishable / anon key cannot read it)
  * otherwise app/output/user_keys.json (server-side file, never served)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time

import requests

from . import config, db

log = logging.getLogger("keys")

KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")
ACCOUNT_URL = "https://google.serper.dev/account"


class KeyError_(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def mask(key: str) -> str:
    return "•" * 8 + key[-4:] if key else ""


def clean(key: str) -> str:
    key = (key or "").strip()
    if not KEY_RE.match(key):
        raise KeyError_(422, "That does not look like a Serper API key "
                             "(letters and digits, copied from serper.dev -> API Key).")
    return key


# ---------------------------------------------------------------------------
# stores
# ---------------------------------------------------------------------------

class FileStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.Lock()
        self.keys: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        """A file that exists but can't be read (corrupt, or locked for a
        moment by antivirus / backup) is NEVER cached as an empty map: the
        call fails and the next one retries, so no save can wipe the keys."""
        if self.keys is None:
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
                self.keys = {str(k): str(v) for k, v in data.items()}
            except FileNotFoundError:
                self.keys = {}
            except (OSError, ValueError):
                log.exception("could not read %s - saved keys unavailable until it "
                              "can be read (the file is left untouched)", self.path)
                raise
        return self.keys

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.keys, f)
        try:                       # owner-only on POSIX hosts (no-op on Windows)
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def get(self, owner: str) -> str:
        with self.lock:
            return self._load().get(owner, "")

    def set(self, owner: str, key: str):
        with self.lock:
            self._load()[owner] = key
            self._save()

    def delete(self, owner: str):
        with self.lock:
            if self._load().pop(owner, None) is not None:
                self._save()


class PgStore:
    def get(self, owner: str) -> str:
        with db.connection() as conn:
            row = conn.execute(f"SELECT api_key FROM {db.SCHEMA}.user_keys "
                               f"WHERE owner = %s", (owner,)).fetchone()
        return row[0] if row else ""

    def set(self, owner: str, key: str):
        with db.connection() as conn:
            conn.execute(
                f"INSERT INTO {db.SCHEMA}.user_keys (owner, api_key) VALUES (%s, %s) "
                f"ON CONFLICT (owner) DO UPDATE SET api_key = EXCLUDED.api_key, "
                f"updated_at = now()", (owner, key))

    def delete(self, owner: str):
        with db.connection() as conn:
            conn.execute(f"DELETE FROM {db.SCHEMA}.user_keys WHERE owner = %s", (owner,))


_store = None
_store_lock = threading.Lock()


def use_store(store):
    """Install a store (tests); None = default for this configuration."""
    global _store
    with _store_lock:
        _store = store


def _get_store():
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = PgStore() if db.enabled() else FileStore(
                    os.path.join(config.OUTPUT_DIR, "user_keys.json"))
    return _store


def user_key(owner: str) -> str:
    """The caller's own saved key ('' if none). Blocking - call in a thread."""
    try:
        return _get_store().get(owner)
    except Exception:
        log.exception("key store unavailable")
        raise KeyError_(503, "Saved API keys are unavailable - try again in a minute.") \
            from None


def save(owner: str, key: str):
    try:
        _get_store().set(owner, key)
    except Exception:
        log.exception("key store unavailable")
        raise KeyError_(503, "Could not save the API key - try again in a minute.") \
            from None


def delete(owner: str):
    try:
        _get_store().delete(owner)
    except Exception:
        log.exception("key store unavailable")
        raise KeyError_(503, "Could not remove the API key - try again in a minute.") \
            from None


def effective(owner: str) -> tuple[str, str]:
    """(key, source) a collection by this owner uses: their own key, else the
    server's. source is 'user' | 'server' | ''."""
    own = user_key(owner)
    if own:
        return own, "user"
    server = config.serper_api_key()
    return (server, "server") if server else ("", "")


BALANCE_CACHE_S = 30.0
_balances: dict[str, tuple[float, int | None, bool]] = {}   # key hash -> (t, balance, rejected)
_balances_lock = threading.Lock()


def balance(key: str) -> tuple[int | None, bool]:
    """(remaining credits or None if unknown, key rejected?) for the page's
    credit display - cached BALANCE_CACHE_S per key so status polls never
    hammer Serper. Blocking - call in a thread."""
    h = hashlib.sha256(key.encode()).hexdigest()
    now = time.time()
    with _balances_lock:
        hit = _balances.get(h)
        if hit and now - hit[0] < BALANCE_CACHE_S:
            return hit[1], hit[2]
    try:
        bal, rejected = check_with_serper(key), False
    except KeyError_:
        bal, rejected = None, True
    with _balances_lock:
        _balances[h] = (now, bal, rejected)
        if len(_balances) > 5000:
            _balances.clear()
    return bal, rejected


def forget_balance(key: str):
    with _balances_lock:
        _balances.pop(hashlib.sha256(key.encode()).hexdigest(), None)


def check_with_serper(key: str) -> int | None:
    """Ask Serper whether the key works (the account endpoint costs no
    credit). Returns the remaining credits; raises KeyError_ on a rejected
    key. None = Serper could not be reached, the key is saved unchecked."""
    try:
        r = requests.get(ACCOUNT_URL, headers={"X-API-KEY": key},
                         timeout=(config.SERPER_TIMEOUT[0], 15))
    except requests.RequestException as exc:
        log.warning("serper key check failed: %s", str(exc)[:120])
        return None
    if r.status_code in (401, 403):
        raise KeyError_(400, "Serper rejected this API key - copy it again from "
                             "serper.dev -> API Key.")
    if r.status_code != 200:
        log.warning("serper key check: HTTP %s", r.status_code)
        return None
    try:
        return int(r.json().get("balance"))
    except (ValueError, TypeError, AttributeError):
        return None
