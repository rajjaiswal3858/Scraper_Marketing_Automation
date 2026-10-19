"""Regression tests: per-IP rate limits run before the session lookup (bogus-token floods get 429 without a
database call and can't evict real sessions from the cache); an unreadable
user_keys.json is never replaced by an empty one.
Run:  python tests/test_bugfix_auth.py  (from the app/ directory)"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from _scratch import scratch_dir  # noqa: E402  (tests/_scratch.py)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SUPABASE_DB_URL"] = ""

from backend import auth, config, keys, main  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_auth import call  # noqa: E402


class CountingStore(auth.MemoryStore):
    def __init__(self):
        super().__init__()
        self.lookups = 0

    def session(self, token_hash):
        self.lookups += 1
        return super().session(token_hash)


def test_ip_limit_before_session_lookup():
    store = CountingStore()
    auth.use_store(store)
    real = config.RATE_LIMIT_IP_PER_MIN
    config.RATE_LIMIT_IP_PER_MIN = 5
    try:
        codes = [call("GET", "/api/status", token=f"bogus-{i}", ip="10.30.0.1")[0]
                 for i in range(12)]
        assert codes[0] == 401 and codes[-1] == 429, codes
        limited = codes.count(429)
        assert store.lookups == 12 - limited, (store.lookups, codes)
        print(f"  bogus-token flood: 429 after the per-IP limit, no lookup once limited "
              f"({store.lookups} lookups for 12 requests)")
        # an anonymous caller (no token) counts against the same limit
        codes = [call("GET", "/api/status", ip="10.30.0.2")[0] for _ in range(8)]
        assert codes[0] == 401 and codes[-1] == 429, codes
        print("  unauthenticated requests are counted too")
    finally:
        config.RATE_LIMIT_IP_PER_MIN = real
    auth.use_store(None)


def test_misses_do_not_evict_sessions():
    store = CountingStore()
    auth.use_store(store)
    real = auth.MISS_CACHE_MAX, auth.SESSION_CACHE_MAX
    auth.MISS_CACHE_MAX, auth.SESSION_CACHE_MAX = 5, 5
    try:
        user = {"id": "u1", "name": "Real", "email": "real@example.com"}
        out = asyncio.run(auth._new_session(user))

        async def flood():
            assert (await auth.session_user(out["token"]))["id"] == "u1"
            for i in range(50):
                assert await auth.session_user(f"made-up-{i}") is None
            before = store.lookups
            assert (await auth.session_user(out["token"]))["id"] == "u1"
            return before
        before = asyncio.run(flood())
        assert store.lookups == before, "real session was evicted by bogus tokens"
        assert len(auth._miss_cache) <= 5 and len(auth._cache) == 1
        print("  50 bogus tokens: real session still cached, misses capped apart")
    finally:
        auth.MISS_CACHE_MAX, auth.SESSION_CACHE_MAX = real
        auth.use_store(None)


def test_unreadable_keys_file_not_wiped():
    tmp = scratch_dir(prefix="bugfix_keys_")
    path = os.path.join(tmp, "user_keys.json")
    good = json.dumps({"uA": "a" * 20, "uB": "b" * 20})
    for broken in (good[:-3], "[]"):
        with open(path, "w", encoding="utf-8") as f:
            f.write(broken)
        store = keys.FileStore(path)
        for op in (lambda: store.get("uA"), lambda: store.set("uC", "c" * 20),
                   lambda: store.delete("uA")):
            try:
                op()
            except (OSError, ValueError):
                pass
            else:
                raise AssertionError(f"no error for {broken[:10]!r}")
        assert store.keys is None                        # nothing cached: retried next time
        assert open(path, encoding="utf-8").read() == broken   # left untouched
        keys.use_store(store)
        try:
            keys.save("uC", "c" * 20)
        except keys.KeyError_ as exc:
            assert exc.status == 503
        else:
            raise AssertionError("save over an unreadable file must fail")
        # repaired: everyone's keys are back and saving works
        with open(path, "w", encoding="utf-8") as f:
            f.write(good)
        assert store.get("uA") == "a" * 20
        store.set("uC", "c" * 20)
        assert json.load(open(path, encoding="utf-8")) == {"uA": "a" * 20, "uB": "b" * 20,
                                                          "uC": "c" * 20}
    keys.use_store(None)
    print("  unreadable user_keys.json: errors, file untouched, works once repaired")

    fresh = keys.FileStore(os.path.join(tmp, "sub", "none.json"))
    assert fresh.get("x") == ""
    fresh.set("x", "x" * 20)
    assert fresh.get("x") == "x" * 20
    print("  missing user_keys.json is an empty store")


def main_():
    test_ip_limit_before_session_lookup()
    test_misses_do_not_evict_sessions()
    test_unreadable_keys_file_not_wiped()
    print("\nALL BUGFIX AUTH TESTS PASSED")


if __name__ == "__main__":
    main_()
