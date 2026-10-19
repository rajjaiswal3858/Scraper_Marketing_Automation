"""Collection worker process.

All scraping - jobs, crawl threads, the analysis process pool, checkpoints,
Excel exports - runs HERE, in a process separate from the web API. The API
process therefore never competes with crawl threads for the GIL: however
hard the jobs work, status polls, page loads and downloads stay fast.

Protocol (multiprocessing Pipe, pickled dicts):
    API -> worker   {"id": n, "cmd": name, ...args}
    worker -> API   {"type": "reply", "id": n, "ok": True, "data": ...}
                    {"type": "reply", "id": n, "ok": False, "status": 4xx, "detail": msg}
                    {"type": "bundle", ...}   every STATUS_PUBLISH_INTERVAL
The bundle carries everything the API serves from memory: per-category
stats, changed job snapshots, new live-log lines, queue summary.

If the API goes away the pipe breaks and the worker saves and exits; if the
worker dies, the API restarts it (see hub.py) and re-submits running jobs.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import queue
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import config, persist
from .collector.categories import CATEGORIES, suggested_keywords
from .collector.engine import ACTIVE_STATUSES, StateStore
from .collector.jobs import JobManager, JobRejected
from .collector.stores import StoreRegistry

log = logging.getLogger("worker")

MAX_CUSTOM_CATEGORIES = 50      # per user: each one is a category + an Excel file


def _setup_logging():
    """Worker log -> output/worker.log through a queue (the writing happens
    on one background thread, never on crawl threads). Warnings also go to
    the console the app was started from."""
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    q: queue.Queue = queue.Queue(-1)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    handlers = []
    try:
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(config.OUTPUT_DIR, "worker.log"),
            maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        handlers.append(fh)
    except OSError:
        pass
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(fmt)
    handlers.append(ch)
    listener = logging.handlers.QueueListener(q, *handlers, respect_handler_level=True)
    listener.start()
    root = logging.getLogger()
    root.handlers[:] = [logging.handlers.QueueHandler(q)]
    root.setLevel(logging.INFO)
    return listener


class Worker:
    def __init__(self, conn, resume: list[dict] | None = None):
        self.conn = conn
        self.send_lock = threading.Lock()
        # every user's records / stats / Excel files are their own
        self.stores = StoreRegistry()
        # the per-network daily credit cap survives worker restarts
        self.manager = JobManager(state_for=self.stores.get,
                                  spend_path=os.path.join(config.OUTPUT_DIR,
                                                          "ip_spend.json"))
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rpc")
        self.stopping = threading.Event()
        self.epoch = f"{os.getpid()}-{int(time.time())}"
        self._log_sent: dict[str, int] = {}     # job id -> log seq published
        self._pos_sent: dict[str, int] = {}     # queued job id -> position published
        self._client_rev: dict[str, tuple] = {}  # client -> (gen, rev) published
        self._last_evict = time.time()
        for r in resume or []:                  # jobs running when a worker died
            try:
                self.cmd_collect(**r)
                log.warning("resumed job %s (%s) after a worker restart",
                            r.get("job_id"), r.get("category"))
            except Exception as exc:
                log.warning("could not resume job %s: %s", r.get("job_id"), exc)

    # -- transport ----------------------------------------------------------
    def send(self, msg: dict):
        with self.send_lock:
            self.conn.send(msg)

    def serve(self):
        threading.Thread(target=self._publisher, daemon=True, name="publisher").start()
        persist.start_syncer(self.stopping)     # users' data -> database
        while not self.stopping.is_set():
            try:
                msg = self.conn.recv()
            except (EOFError, OSError):       # API process gone
                break
            if msg.get("cmd") == "shutdown":
                self._reply(msg["id"], True, None)
                break
            self.pool.submit(self._handle, msg)
        self.shutdown()

    def _reply(self, mid, ok: bool, data=None, status: int = 500, detail: str = ""):
        try:
            if ok:
                self.send({"type": "reply", "id": mid, "ok": True, "data": data})
            else:
                self.send({"type": "reply", "id": mid, "ok": False, "status": status,
                           "detail": detail})
        except (OSError, EOFError, BrokenPipeError):
            self.stopping.set()

    def _handle(self, msg: dict):
        mid, cmd = msg.get("id"), msg.get("cmd")
        fn = getattr(self, f"cmd_{cmd}", None)
        if fn is None:
            self._reply(mid, False, status=400, detail=f"unknown command {cmd}")
            return
        try:
            self._reply(mid, True, fn(**msg.get("args", {})))
        except JobRejected as exc:
            self._reply(mid, False, status=exc.status, detail=exc.detail)
        except persist.Unavailable:
            self._reply(mid, False, status=503,
                        detail="Your saved data could not be loaded from the "
                               "database - try again in a minute.")
        except Exception as exc:
            log.exception("command %s failed", cmd)
            self._reply(mid, False, status=500, detail=f"{type(exc).__name__}")

    # -- status bundle ------------------------------------------------------
    @staticmethod
    def per_category(state: StateStore) -> dict:
        """One user's categories with their counts."""
        cats = state.all_categories()
        stats = state.category_stats()
        return {c: {"display": d["display"], "file": d["file"],
                    "custom": c not in CATEGORIES,
                    "suggested": suggested_keywords(c),
                    **stats.get(c, {"count": 0, "emails": 0, "websites": 0})}
                for c, d in cats.items()}

    def client_view(self, state: StateStore) -> dict:
        with state.lock:
            rev, cat_rev = state.rev, dict(state.cat_rev)
        # "ver" orders views of one user: (store generation, revision)
        return {"ver": [getattr(state, "gen", 0), rev],
                "per_category": self.per_category(state), "cat_rev": cat_rev}

    def job_view(self, job) -> dict:
        snap = job.snapshot(include_log=False)
        snap["owner"] = job.owner                      # stripped by the API
        snap["queue_position"] = (self.manager.queue_position(job.id)
                                  if job.status == "queued" else 0)
        return snap

    def bundle(self) -> dict:
        m = self.manager
        changed = m.drain_changed()
        with m.lock:
            live = list(m.active.values())
            # queued jobs only when they changed or moved in the queue
            for pos, jid in enumerate(m.queue, 1):
                if jid in changed or self._pos_sent.get(jid) != pos:
                    self._pos_sent[jid] = pos
                    live.append(m.jobs[jid])
            for jid in changed:
                j = m.jobs.get(jid)
                if j is not None and j not in live:
                    live.append(j)
            queued = set(m.queue)
            for jid in [k for k in self._pos_sent if k not in queued]:
                del self._pos_sent[jid]
            known = list(m.jobs)
            summary = m.summary()
            active = [{"category": j.category, "display": j.display,
                       "status": j.status, "collected": j.category_count(),
                       "target": j.target}
                      for j in list(m.active.values()) + [m.jobs[k] for k in m.queue]]
        jobs, logs = {}, {}
        for j in live:
            jobs[j.id] = self.job_view(j)
            seq, lines, reset = j.log_since(self._log_sent.get(j.id, 0))
            if lines or reset:
                logs[j.id] = {"seq": seq, "lines": lines, "reset": reset}
            self._log_sent[j.id] = seq
        for jid in [k for k in self._log_sent if k not in m.jobs]:
            del self._log_sent[jid]
        # per-user category stats, only for users whose data changed
        clients = {}
        loaded = self.stores.loaded()
        for client, st in loaded:
            if (getattr(st, "gen", 0), st.rev) != self._client_rev.get(client):
                view = self.client_view(st)
                self._client_rev[client] = tuple(view["ver"])
                clients[client] = view
        live_clients = {c for c, _ in loaded}
        for c in [c for c in self._client_rev if c not in live_clients]:
            del self._client_rev[c]
        return {"type": "bundle", "epoch": self.epoch, "time": time.time(),
                "jobs": jobs, "logs": logs, "known": known, "queue": summary,
                "clients": clients,
                "active": active[:50],
                "worker": {"pid": os.getpid(), "threads": threading.active_count(),
                           "users_loaded": len(loaded),
                           "saves": sum(st.saves for _, st in loaded)}}

    def _publisher(self):
        while not self.stopping.is_set():
            try:
                if time.time() - self._last_evict > 60:
                    self._last_evict = time.time()
                    self.stores.evict_idle(self.manager.owners_active())
                self.send(self.bundle())
            except (OSError, EOFError, BrokenPipeError):
                self.stopping.set()
                break
            except Exception:
                log.exception("status publish failed")
            self.stopping.wait(config.STATUS_PUBLISH_INTERVAL)

    # -- commands -------------------------------------------------------------
    def cmd_ping(self):
        return {"pid": os.getpid(), "epoch": self.epoch}

    def cmd_collect(self, client: str, category: str, custom_category: str,
                    keywords: list[str], location: str, geo: dict, target: int,
                    provider: str, max_queries: int, job_id: str | None = None,
                    ip: str = "", serper_key: str = ""):
        state = self.stores.get(client)
        created = ""
        if category == "__custom__":
            name = " ".join(custom_category.split())
            if len(name) < 3:
                raise JobRejected(400, "Enter a custom category name (3+ characters).")
            with state.lock:
                known = {d["display"].lower() for d in state.custom.values()}
                before = set(state.custom)
            if name.lower() not in known and len(before) >= MAX_CUSTOM_CATEGORIES:
                raise JobRejected(400, f"You have {len(before)} custom categories "
                                       f"(the limit is {MAX_CUSTOM_CATEGORIES}). "
                                       f"Delete one you no longer need first.")
            category = state.ensure_custom(name)
            if category not in before:
                created = category
        elif category not in state.all_categories():
            raise JobRejected(400, "unknown category")
        try:
            job = self.manager.submit(client, category, keywords, location, target,
                                      provider, max_queries, geo=geo, job_id=job_id,
                                      ip=ip, serper_key=serper_key)
        except Exception:
            # a rejected collection (busy / limit / capacity) leaves no
            # empty custom category behind
            if created:
                self._drop_new_custom(state, created)
            raise
        view = self.job_view(job)
        _, view["log"], _ = job.log_since(0)
        return view

    @staticmethod
    def _drop_new_custom(state: StateStore, slug: str):
        """Undo ensure_custom() of a category nothing was collected into."""
        with state.lock:
            if slug not in state.custom or state.records.get(slug):
                return
            del state.custom[slug]
            state.records.pop(slug, None)
            state.stats.pop(slug, None)
            state.cat_rev.pop(slug, None)
            state.rev += 1

    def cmd_stop(self, client: str, job_id: str | None = None):
        job = self.manager.stop(client, job_id)
        if job is None:
            raise JobRejected(404, "no collection of yours to stop")
        return self.job_view(job)

    def cmd_usage(self, owners: list[str]):
        """Admin report: Serper credits each account spent (lifetime, from
        its own ledger - shared key vs own key), searches, records, last use
        and whether a collection is running now."""
        out = {}
        for owner in owners:
            try:
                state = self.stores.get(owner)
                s = state.ledger.summary(top=0)
                with state.lock:
                    records = sum(len(r) for r in state.records.values())
                job = self.manager.client_job(owner)
                out[owner] = {
                    "credits": s["total_credits"],
                    "server_key_credits": s["server_key_credits"],
                    "own_key_credits": s["own_key_credits"],
                    "searches": s["total_queries"],
                    "records": records,
                    "last_used": s["last_used"],
                    "running": bool(job is not None and job.status in ACTIVE_STATUSES),
                }
            except Exception as exc:          # one unreadable user never hides the rest
                log.warning("usage report: %s: %s", owner[:12], exc)
                out[owner] = {"error": type(exc).__name__}
        return out

    def cmd_set_key(self, client: str, serper_key: str = ""):
        """The caller saved / removed their own Serper key: their queued or
        running collection switches to it (a credit-paused one resumes)."""
        # the manager re-applies (server key) or lifts (own key) the
        # network's daily credit cap on the switched job
        job = self.manager.set_key(client, serper_key)
        if job is None:
            return {"switched": False}
        return {"switched": True, "job_id": job.id}

    def cmd_export(self, client: str, name: str):
        """Bring one of the caller's Excel files up to date right before a
        download: mid-run exports are throttled (EXPORT_MIN_INTERVAL), so
        the file could lag the records shown on the page. Unchanged
        content is not rewritten."""
        state = self.stores.get(client)
        if not os.path.exists(os.path.join(state.out_dir, name)):
            return {"ok": False}             # never generated: nothing to refresh
        cats = state.all_categories()
        wanted = [c for c, d in cats.items() if d["file"] == name]
        state.export_categories(wanted)      # always refreshes the summary too
        return {"ok": True}

    def cmd_categories(self, client: str):
        """The caller's categories + counts (the API caches them; bundles
        keep them current while the user's data is loaded)."""
        return self.client_view(self.stores.get(client))

    def cmd_records(self, client: str, category: str, limit: int):
        state = self.stores.get(client)
        if category not in state.all_categories():
            raise JobRejected(400, "unknown category")
        with state.lock:
            rows = state.records.get(category, [])
            total = len(rows)
            rows = list(rows)[-limit:] if limit > 0 else []
            rev = state.cat_rev[category]
        return {"category": category, "total": total, "records": rows[::-1],
                "rev": [getattr(state, "gen", 0), rev]}

    def cmd_credits(self, client: str):
        job = self.manager.client_job(client)
        return {"ledger": self.stores.get(client).ledger.summary(),
                "run": job.credit_metrics() if job is not None else None}

    def cmd_delete_category(self, client: str, category: str):
        from .collector.exporter import write_category_file, write_master_summary
        state = self.stores.get(client)
        cats = state.all_categories()
        if category not in cats:
            raise JobRejected(400, "unknown category")
        if self.manager.category_busy(category, state):
            raise JobRejected(409, "A collection is running or queued for this "
                                   "category. Stop it first.")
        display = cats[category]["display"]
        removed, is_custom, fname = state.delete_category(category)
        state.save(state.path)
        path = os.path.join(state.out_dir, fname)
        try:
            if is_custom:
                if os.path.exists(path):
                    os.remove(path)
            else:
                write_category_file(display, fname, [], state.out_dir)
        except OSError:
            pass  # e.g. the file is open in Excel; data is already deleted
        cats = state.all_categories()
        with state.lock:
            records = {c: list(rs) for c, rs in state.records.items()}
            stats = {c: dict(s) for c, s in state.stats.items()}
        write_master_summary(cats, records, stats, state.out_dir)
        persist.sync(client)
        return {"ok": True, "removed": removed, "was_custom": is_custom,
                "display": display}

    def cmd_reset(self, client: str):
        """Wipe the caller's own data (records + dedup registry). Excel
        files stay."""
        if self.manager.any_active(self.stores.get(client)):
            raise JobRejected(409, "stop your running collection first")
        self.stores.replace(client)
        persist.sync(client)
        return {"ok": True}

    # -- lifecycle ----------------------------------------------------------------
    def shutdown(self):
        self.stopping.set()
        try:
            self.manager.stop_all(timeout=20.0)
        except Exception:
            log.exception("stopping jobs at shutdown")
        try:
            self.stores.flush_all()
        except Exception:
            log.exception("final checkpoint at shutdown")
        self.pool.shutdown(wait=False, cancel_futures=True)


def worker_main(conn, resume=None, init=None):
    """Entry point of the worker process (multiprocessing target)."""
    # Ctrl+C in the console reaches every process attached to it. Only the
    # API process handles it; it then asks this worker to shut down cleanly
    # (stop jobs, final checkpoint) over the pipe. Without this, the
    # KeyboardInterrupt would kill the worker before its final save.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, "SIGBREAK"):          # Windows Ctrl+Break: same story
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    if os.name != "nt":
        # Same for SIGTERM on Linux: a host shutting the app down (a Render
        # deploy / restart) may signal the whole process group. The API
        # handles it and stops this worker over the pipe; if the API is
        # gone the pipe breaks and the worker still saves and exits.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    listener = _setup_logging()
    if hasattr(os, "nice"):
        # Crawling must not starve the API process on a small CPU share:
        # status polls, Stop and downloads stay responsive mid-collection.
        try:
            os.nice(10)
        except OSError:
            pass
    try:
        if init is not None:         # test hook: install simulators
            init()
        from .collector import analysis, tls
        analysis.warm_up()
        # Load the CA bundle now instead of inside the first job's first
        # crawl (measured: 0.33 s CPU at 1 CPU, 3.6 s at 0.1 CPU).
        threading.Thread(target=tls.shared_tls_context, daemon=True,
                         name="tls-warmup").start()
        w = Worker(conn, resume)
        log.info("collection worker started (pid %d, %d job slots, %d crawl "
                 "threads per job)", os.getpid(), config.MAX_ACTIVE_JOBS,
                 config.job_crawl_workers())
        w.serve()
    finally:
        log.info("collection worker exiting")
        listener.stop()
        try:
            conn.close()
        except OSError:
            pass
        os._exit(0)   # don't wait for daemon crawl threads stuck in I/O
