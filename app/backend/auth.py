"""User accounts: sign-up, login, sessions.

Enabled when SUPABASE_DB_URL is set (users and sessions live in Postgres,
see db.py), or with APP_LOCAL_ACCOUNTS=1 (a JSON file in app/output, for one
machine without a database); otherwise the app keeps working with anonymous
per-browser client ids.

  * passwords are stored only as scrypt hashes (random salt per user)
  * a login returns a random session token; the database keeps only its
    SHA-256, so a leaked sessions table cannot be replayed
  * sessions expire after SESSION_DAYS (expired rows are purged at
    each login)
  * a logged-in user's identity ("u" + user id) replaces the browser
    client id: their records, jobs and Excel files follow the account
    to any browser

The store is pluggable: tests install MemoryStore with use_store().
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from . import config, db

log = logging.getLogger("auth")

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
PASSWORD_MIN, PASSWORD_MAX = 8, 128
NAME_MAX = 60
# scrypt cost: ~50 ms CPU / 16 MB per hash on a laptop core
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
SESSION_CACHE_S = 60.0           # a session is re-checked in the DB at most this often
SESSION_CACHE_MAX = 5000
MISS_CACHE_MAX = 2000            # unknown tokens, cached apart (can't evict real sessions)
EVENTS_KEEP_DAYS = 90            # login / logout history kept in the database
EVENTS_KEEP_LOCAL = 5000         # ... and in the local accounts file
ONLINE_S = 120                   # "online now" = a request within this many seconds
# user id -> time of their last API request (this process; the page polls
# every few seconds, so an open tab keeps a user "online")
last_seen: dict[str, float] = {}


class AuthError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                        p=SCRYPT_P, maxmem=64 * 1024 * 1024, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()   # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        if algo != "scrypt":
            return False
        want = base64.b64decode(dk)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                             n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(want))
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError):
        return False


# A real-looking hash to verify against when the email is unknown, so a
# login for a missing account takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(12))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------- stores
class DuplicateEmail(Exception):
    pass


class PgStore:
    """Users + sessions in Postgres (tables: db.py)."""

    def create_user(self, doc: dict) -> str:
        from psycopg.errors import UniqueViolation
        uid = secrets.token_hex(12)
        try:
            with db.connection() as conn:
                conn.execute(
                    f"INSERT INTO {db.SCHEMA}.users (id, name, email, password_hash,"
                    " created_at) VALUES (%s, %s, %s, %s, %s)",
                    (uid, doc["name"], doc["email"], doc["password_hash"],
                     doc["created_at"]))
        except UniqueViolation:
            raise DuplicateEmail() from None
        return uid

    def user_by_email(self, email: str) -> dict | None:
        with db.connection() as conn:
            row = conn.execute(
                f"SELECT id, name, email, password_hash FROM {db.SCHEMA}.users"
                " WHERE email = %s", (email,)).fetchone()
        return (dict(zip(("id", "name", "email", "password_hash"), row))
                if row else None)

    def list_users(self) -> list[dict]:
        with db.connection() as conn:
            rows = conn.execute(
                f"SELECT id, name, email, created_at FROM {db.SCHEMA}.users"
                " ORDER BY created_at").fetchall()
        return [{"id": r[0], "name": r[1], "email": r[2], "created_at": str(r[3])}
                for r in rows]

    def create_session(self, token_hash: str, user: dict, expires: datetime):
        with db.connection() as conn:
            conn.execute(f"DELETE FROM {db.SCHEMA}.sessions WHERE expires_at < now()")
            conn.execute(
                f"INSERT INTO {db.SCHEMA}.sessions (token_hash, user_id, expires_at)"
                " VALUES (%s, %s, %s)", (token_hash, user["id"], expires))

    def session(self, token_hash: str) -> dict | None:
        with db.connection() as conn:
            row = conn.execute(
                f"SELECT u.id, u.name, u.email FROM {db.SCHEMA}.sessions s"
                f" JOIN {db.SCHEMA}.users u ON u.id = s.user_id"
                " WHERE s.token_hash = %s AND s.expires_at > now()",
                (token_hash,)).fetchone()
        return (dict(zip(("user_id", "name", "email"), row)) if row else None)

    def delete_session(self, token_hash: str):
        with db.connection() as conn:
            conn.execute(f"DELETE FROM {db.SCHEMA}.sessions WHERE token_hash = %s",
                         (token_hash,))

    def add_event(self, kind: str, user_id: str, email: str, ip: str):
        with db.connection() as conn:
            conn.execute(f"INSERT INTO {db.SCHEMA}.auth_events (kind, user_id, email, ip)"
                         " VALUES (%s, %s, %s, %s)", (kind, user_id, email, ip))
            if secrets.randbelow(50) == 0:          # keep EVENTS_KEEP_DAYS of history
                conn.execute(f"DELETE FROM {db.SCHEMA}.auth_events WHERE at < now() - "
                             f"interval '{EVENTS_KEEP_DAYS} days'")

    def events(self, since: datetime, limit: int) -> list[dict]:
        with db.connection() as conn:
            rows = conn.execute(
                f"SELECT at, kind, user_id, email, ip FROM {db.SCHEMA}.auth_events"
                " WHERE at >= %s ORDER BY at DESC LIMIT %s", (since, limit)).fetchall()
        return [{"at": r[0], "kind": r[1], "user_id": r[2], "email": r[3], "ip": r[4]}
                for r in rows]

    def active_sessions(self) -> dict[str, int]:
        with db.connection() as conn:
            rows = conn.execute(
                f"SELECT user_id, count(*) FROM {db.SCHEMA}.sessions"
                " WHERE expires_at > now() GROUP BY user_id").fetchall()
        return {r[0]: int(r[1]) for r in rows}


class MemoryStore:
    """Same interface, in memory (tests, or a demo without a database)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.users: dict[str, dict] = {}       # email -> doc
        self.sessions: dict[str, dict] = {}    # token hash -> doc
        self.event_log: list[dict] = []        # oldest first, capped

    def create_user(self, doc: dict) -> str:
        with self.lock:
            if doc["email"] in self.users:
                raise DuplicateEmail()
            uid = secrets.token_hex(12)
            self.users[doc["email"]] = dict(doc, id=uid)
            return uid

    def user_by_email(self, email: str) -> dict | None:
        with self.lock:
            doc = self.users.get(email)
            return dict(doc) if doc else None

    def list_users(self) -> list[dict]:
        with self.lock:
            return [{"id": d["id"], "name": d["name"], "email": d["email"],
                     "created_at": str(d.get("created_at", ""))}
                    for d in self.users.values()]

    def create_session(self, token_hash: str, user: dict, expires: datetime):
        with self.lock:
            self.sessions[token_hash] = {"user_id": user["id"], "name": user["name"],
                                         "email": user["email"], "expires_at": expires}

    def session(self, token_hash: str) -> dict | None:
        with self.lock:
            s = self.sessions.get(token_hash)
            return dict(s) if s and s["expires_at"] > _now() else None

    def delete_session(self, token_hash: str):
        with self.lock:
            self.sessions.pop(token_hash, None)

    def add_event(self, kind: str, user_id: str, email: str, ip: str):
        with self.lock:
            self.event_log.append({"at": _now(), "kind": kind, "user_id": user_id,
                                   "email": email, "ip": ip})
            del self.event_log[:-EVENTS_KEEP_LOCAL]

    def events(self, since: datetime, limit: int) -> list[dict]:
        with self.lock:
            out = [dict(e) for e in reversed(self.event_log) if e["at"] >= since]
        return out[:limit]

    def active_sessions(self) -> dict[str, int]:
        now, out = _now(), {}
        with self.lock:
            for s in self.sessions.values():
                if s["expires_at"] > now:
                    out[s["user_id"]] = out.get(s["user_id"], 0) + 1
        return out


class FileStore(MemoryStore):
    """MemoryStore saved to a JSON file after every change: accounts on one
    machine without a database (APP_LOCAL_ACCOUNTS=1). Same hashes and
    token hashes as the database - never a password or token in clear."""

    def __init__(self, path: str):
        super().__init__()
        self.path = path
        self.loaded = False
        self.load_lock = threading.Lock()

    def _ensure(self):
        """Load the file on first use. A file that exists but can't be read
        (corrupt, or locked for a moment by antivirus / backup) is NEVER
        replaced by an empty store: the call fails (503 'try again') and the
        next one tries again, so no later save can wipe the accounts."""
        if self.loaded:
            return
        with self.load_lock:
            if self.loaded:
                return
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
                users = data.get("users", {})
                raw_sessions = data.get("sessions", {})
                if not isinstance(users, dict) or not isinstance(raw_sessions, dict):
                    raise ValueError("'users' / 'sessions' is not an object")
                sessions = {}
                for th, s in raw_sessions.items():
                    sessions[th] = dict(s, expires_at=datetime.fromisoformat(s["expires_at"]))
                event_log = [dict(e, at=datetime.fromisoformat(e["at"]))
                             for e in (data.get("events") or []) if isinstance(e, dict)]
            except FileNotFoundError:
                users, sessions, event_log = {}, {}, []
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                log.exception("could not read %s - accounts unavailable until it "
                              "can be read (the file is left untouched; a backup "
                              "of the last good version is %s.bak)", self.path, self.path)
                raise
            with self.lock:
                self.users, self.sessions, self.event_log = users, sessions, event_log
            self.loaded = True

    def _save(self):
        """Caller holds self.lock."""
        now = _now()
        self.sessions = {th: s for th, s in self.sessions.items()
                         if s["expires_at"] > now}
        data = {"users": self.users,
                "sessions": {th: dict(s, expires_at=s["expires_at"].isoformat())
                             for th, s in self.sessions.items()},
                "events": [dict(e, at=e["at"].isoformat()) for e in self.event_log]}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, default=str)   # created_at
        try:                       # keep the last good version to recover from
            shutil.copyfile(self.path, self.path + ".bak")
        except FileNotFoundError:
            pass
        try:                       # owner-only on POSIX hosts (no-op on Windows)
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def create_user(self, doc: dict) -> str:
        self._ensure()
        uid = super().create_user(doc)
        with self.lock:
            self._save()
        return uid

    def user_by_email(self, email: str) -> dict | None:
        self._ensure()
        return super().user_by_email(email)

    def list_users(self) -> list[dict]:
        self._ensure()
        return super().list_users()

    def create_session(self, token_hash: str, user: dict, expires: datetime):
        self._ensure()
        super().create_session(token_hash, user, expires)
        with self.lock:
            self._save()

    def session(self, token_hash: str) -> dict | None:
        self._ensure()
        return super().session(token_hash)

    def delete_session(self, token_hash: str):
        self._ensure()
        super().delete_session(token_hash)
        with self.lock:
            self._save()

    def add_event(self, kind: str, user_id: str, email: str, ip: str):
        self._ensure()
        super().add_event(kind, user_id, email, ip)
        with self.lock:
            self._save()

    def events(self, since: datetime, limit: int) -> list[dict]:
        self._ensure()
        return super().events(since, limit)

    def active_sessions(self) -> dict[str, int]:
        self._ensure()
        return super().active_sessions()


_store = None
_store_lock = threading.Lock()
_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()       # valid sessions
_miss_cache: "OrderedDict[str, float]" = OrderedDict()               # unknown tokens


def enabled() -> bool:
    return _store is not None or db.enabled() or config.LOCAL_ACCOUNTS


def use_store(store):
    """Install a store (tests); None = back to the database."""
    global _store
    with _store_lock:
        _store = store
        _cache.clear()
        _miss_cache.clear()


def _get_store():
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = PgStore() if db.enabled() else FileStore(
                    os.path.join(config.OUTPUT_DIR, "accounts.json"))
    return _store


async def _call(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except (AuthError, DuplicateEmail):
        raise
    except Exception:
        log.exception("user database unavailable")
        raise AuthError(503, "The user database is unavailable - try again "
                             "in a minute.") from None


def identity(user_id: str) -> str:
    """Per-user data key; matches the client-id format the app expects."""
    return f"u{user_id}"


def is_admin(email: str) -> bool:
    """Admins (ADMIN_EMAILS, comma-separated) see the usage report."""
    return bool(email) and email.strip().lower() in config.admin_emails()


def _public(user: dict) -> dict:
    return {"id": user["id"], "name": user["name"], "email": user["email"],
            "is_admin": is_admin(user["email"])}


async def list_users() -> list[dict]:
    return await _call(_get_store().list_users)


# --------------------------------------------------------------- operations
def _clean_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise AuthError(400, "Enter a valid email address.")
    return email


def _check_password(password: str):
    if not PASSWORD_MIN <= len(password or "") <= PASSWORD_MAX:
        raise AuthError(400, f"Password must be {PASSWORD_MIN}-{PASSWORD_MAX} characters.")


async def _new_session(user: dict) -> dict:
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=config.SESSION_DAYS)
    await _call(_get_store().create_session, _token_hash(token), user, expires)
    return {"token": token, "user": _public(user),
            "expires_at": expires.isoformat(timespec="seconds")}


async def signup(name: str, email: str, password: str, ip: str = "") -> dict:
    name = " ".join((name or "").split())[:NAME_MAX]
    if not name:
        raise AuthError(400, "Enter your name.")
    email = _clean_email(email)
    _check_password(password)
    pw_hash = await asyncio.to_thread(hash_password, password)
    doc = {"name": name, "email": email, "password_hash": pw_hash,
           "created_at": _now()}
    try:
        uid = await _call(_get_store().create_user, doc)
    except DuplicateEmail:
        raise AuthError(409, "An account with this email already exists - "
                             "log in instead.") from None
    log.info("signup: user %s", uid)
    user = {"id": uid, "name": name, "email": email}
    out = await _new_session(user)
    await record("signup", user, ip)
    return out


async def record(kind: str, user: dict | None, ip: str = "", email: str = ""):
    """Login history for the admin report (signup / login / logout /
    login_failed). Never fails the request it belongs to."""
    try:
        await _call(_get_store().add_event, kind, (user or {}).get("id", ""),
                    (user or {}).get("email", "") or email, ip)
    except Exception:
        log.warning("could not record %s event", kind)


async def login(email: str, password: str, ip: str = "") -> dict:
    bad = AuthError(401, "Wrong email or password.")
    try:
        email = _clean_email(email)
    except AuthError:
        raise bad from None
    user = await _call(_get_store().user_by_email, email)
    ok = await asyncio.to_thread(verify_password, password or "",
                                 user["password_hash"] if user else _DUMMY_HASH)
    if not (user and ok):
        await record("login_failed", None, ip, email[:254])
        raise bad
    out = await _new_session(user)
    await record("login", user, ip)
    return out


async def activity(days: int = 30, limit: int = 300) -> dict:
    """Admin report: users, their sessions, online state, login history."""
    store = _get_store()
    users = await _call(store.list_users)
    sessions = await _call(store.active_sessions)
    since = _now() - timedelta(days=days)
    events = await _call(store.events, since, 5000)
    now = time.time()
    day0 = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    week0 = _now() - timedelta(days=7)
    per: dict[str, dict] = {}
    for e in events:                                  # newest first
        p = per.setdefault(e["user_id"], {"last_login": None, "last_logout": None,
                                          "logins_7d": 0, "logouts_7d": 0})
        if e["kind"] == "login" and p["last_login"] is None:
            p["last_login"] = e["at"]
        if e["kind"] == "logout" and p["last_logout"] is None:
            p["last_logout"] = e["at"]
        if e["at"] >= week0:
            if e["kind"] == "login":
                p["logins_7d"] += 1
            elif e["kind"] == "logout":
                p["logouts_7d"] += 1
    ts = lambda d: d.timestamp() if d else None      # noqa: E731
    rows = []
    for u in users:
        p = per.get(u["id"], {})
        seen = last_seen.get(u["id"])
        rows.append({"id": u["id"], "name": u["name"], "email": u["email"],
                     "is_admin": is_admin(u["email"]),
                     "online": bool(seen and now - seen < ONLINE_S),
                     "last_seen": seen, "sessions": sessions.get(u["id"], 0),
                     "last_login": ts(p.get("last_login")),
                     "last_logout": ts(p.get("last_logout")),
                     "logins_7d": p.get("logins_7d", 0), "logouts_7d": p.get("logouts_7d", 0)})
    rows.sort(key=lambda r: (not r["online"], -(r["last_seen"] or r["last_login"] or 0)))
    today = [e for e in events if e["at"] >= day0]
    count = lambda evs, k: sum(1 for e in evs if e["kind"] == k)   # noqa: E731
    return {
        "summary": {
            "users": len(users), "online_now": sum(r["online"] for r in rows),
            "logged_in_users": sum(1 for r in rows if r["sessions"]),
            "active_sessions": sum(sessions.values()),
            "logins_today": count(today, "login"), "logouts_today": count(today, "logout"),
            "signups_today": count(today, "signup"),
            "failed_today": count(today, "login_failed"),
            "logins_7d": sum(r["logins_7d"] for r in rows),
            "logouts_7d": sum(r["logouts_7d"] for r in rows),
        },
        "users": rows,
        "events": [{"at": ts(e["at"]), "kind": e["kind"], "email": e["email"],
                    "ip": e["ip"]} for e in events[:limit]],
        "online_window_s": ONLINE_S, "days": days,
    }


async def session_user(token: str) -> dict | None:
    """{id, name, email} for a valid session token, else None. Cached for
    SESSION_CACHE_S, so status polls do not hit the database."""
    if not token or len(token) > 200:
        return None
    th = _token_hash(token)
    hit = _cache.get(th)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    miss = _miss_cache.get(th)
    if miss is not None and miss > time.monotonic():
        return None
    s = await _call(_get_store().session, th)
    user = ({"id": s["user_id"], "name": s["name"], "email": s["email"],
             "is_admin": is_admin(s["email"])} if s else None)
    # misses (bogus / expired tokens) go to their own, smaller cache, so a
    # flood of made-up tokens can't evict the real sessions from _cache
    until = time.monotonic() + SESSION_CACHE_S
    if user is not None:
        cache, cap = _cache, SESSION_CACHE_MAX
        cache[th] = (until, user)
    else:
        cache, cap = _miss_cache, MISS_CACHE_MAX
        cache[th] = until
    cache.move_to_end(th)
    while len(cache) > cap:
        cache.popitem(last=False)
    return user


async def logout(token: str, ip: str = ""):
    user = await session_user(token)
    th = _token_hash(token)
    _cache.pop(th, None)
    _miss_cache.pop(th, None)
    await _call(_get_store().delete_session, th)
    if user is not None:
        last_seen.pop(user["id"], None)      # logged out = not online any more
        await record("logout", user, ip)


def bearer(headers: dict) -> str:
    v = headers.get(b"authorization", b"").decode("latin-1")
    return v[7:].strip() if v[:7].lower() == "bearer " else ""
