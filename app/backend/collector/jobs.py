"""Multi-user job scheduling: every user's collection is its own job.

    submit ──> QUEUED ──(free slot, category not busy)──> running ... ──> done
                  │                                           │
                  └── stop: removed from the queue            └── on_finish ──> next queued job starts

Isolation rules
  * up to MAX_ACTIVE_JOBS jobs run at once; more wait in a FIFO queue
  * every user has their own data (state_for(client), see stores.py): their
    records are never visible to, or counted for, anyone else
  * one job per CATEGORY of one user's data at a time (the target counts
    that data, so two jobs filling it would race each other); a second job
    for a busy category waits, and other queued jobs go ahead of it
  * each client (browser) may have MAX_JOBS_PER_CLIENT queued/running jobs
  * shared resources are split fairly: a starting job gets the crawl
    threads the running jobs leave free, at least an equal slice
    (config.job_crawl_workers), every running job an equal slice of the
    Serper account's concurrency (job.search_share), and all jobs pass through the
    same account rate limiter (search.account_limits)

A job never waits on another job's work: its searches, crawl pool,
planner, watchdog and failure handling are its own. Only the Serper
response cache is shared between users (internally locked).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime

from .. import config
from .engine import ACTIVE_STATUSES, CollectionJob, StateStore

log = logging.getLogger("jobs")


def _slot(job) -> tuple:
    """What two jobs must not fill at once: one category of one user's data."""
    return id(getattr(job, "state", None)), job.category


def _credits(job) -> int:
    """All Serper credits the job spent (own key + server key)."""
    n = int(getattr(job, "counters", {}).get("serper_credits", 0) or 0)
    p = getattr(job, "provider", None)
    # the provider's count is live; the job's counter is synced periodically
    return max(n, int(getattr(p, "credits_used", 0) or 0))


def _server_credits(job) -> int:
    """Credits the job spent on the app's shared server key - whatever key
    it holds now (a running job can switch keys either way)."""
    p = getattr(job, "provider", None)
    n = getattr(p, "server_credits_used", None)
    if n is not None:
        return int(n or 0)
    # no provider (yet) or one without the split: its current key decides
    return 0 if getattr(job, "serper_key", "") else _credits(job)


class JobRejected(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class JobManager:
    SPEND_SAVE_S = 10.0     # how often the per-network spend is written to disk

    def __init__(self, state: StateStore | None = None, max_active: int | None = None,
                 job_factory=CollectionJob, state_for=None, spend_path: str | None = None):
        # state_for(client) -> that user's StateStore (per-user data); without
        # it every job uses the one `state`. spend_path: JSON file keeping the
        # per-network daily spend across worker restarts (None = memory only).
        self.state = state
        self.state_for = state_for or (lambda client: state)
        self.max_active = max_active or config.MAX_ACTIVE_JOBS
        self.job_factory = job_factory
        self.lock = threading.RLock()
        self.jobs: dict[str, CollectionJob] = {}       # id -> job (live + history)
        self.queue: deque[str] = deque()                # queued ids, FIFO
        self.active: dict[str, CollectionJob] = {}      # started, not finished
        self.history: deque[str] = deque()              # finished ids, oldest first
        self.by_client: dict[str, str] = {}             # client -> latest job id
        self.changed: set[str] = set()                  # ids changed since last drain
        # ip -> (finished at, server-key Serper credits) of finished jobs, last 24 h
        self.ip_spend: dict[str, deque] = {}
        self.spend_path = spend_path
        self._spend_saved = ""
        self._spend_saved_at = 0.0
        self._load_spend()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reaper, daemon=True,
                                        name="jobs-reaper")
        self._thread.start()

    # -- queries ----------------------------------------------------------------
    def active_count(self) -> int:
        return max(1, len(self.active))

    def client_job(self, client: str) -> CollectionJob | None:
        with self.lock:
            jid = self.by_client.get(client)
            return self.jobs.get(jid) if jid else None

    def queue_position(self, job_id: str) -> int:
        with self.lock:
            try:
                return self.queue.index(job_id) + 1
            except ValueError:
                return 0

    def summary(self) -> dict:
        with self.lock:
            return {"running": len(self.active), "queued": len(self.queue),
                    "max_active": self.max_active,
                    "busy_categories": sorted({j.category for j in self.active.values()})}

    def drain_changed(self) -> set[str]:
        with self.lock:
            out, self.changed = self.changed, set()
            return out

    # -- commands ---------------------------------------------------------------
    def submit(self, client: str, category: str, keywords: list[str], location: str,
               target: int, provider: str, max_queries: int, geo: dict | None = None,
               job_id: str | None = None, ip: str = "",
               serper_key: str = "") -> CollectionJob:
        with self.lock:
            mine = [j for j in self.jobs.values()
                    if j.owner == client and j.status in ACTIVE_STATUSES]
            if len(mine) >= config.MAX_JOBS_PER_CLIENT:
                raise JobRejected(409, "You already have a collection running or "
                                       "queued. Stop it first.")
            # A client id is caller-chosen; the network address is not. Without
            # these checks one caller could start unlimited paid collections
            # by sending a new X-Client-Id each time.
            remaining = None
            if ip and not serper_key:      # the cap protects the server's key
                same_ip = [j for j in self.jobs.values()
                           if getattr(j, "ip", "") == ip and j.status in ACTIVE_STATUSES]
                if len(same_ip) >= config.MAX_JOBS_PER_IP:
                    raise JobRejected(429, f"Your network already has "
                                           f"{len(same_ip)} collections running or "
                                           f"queued. Wait for one to finish.")
                cap = config.MAX_CREDITS_PER_IP_PER_DAY
                if cap:
                    remaining = cap - self._ip_credits_24h(ip)
                    if remaining <= 0:
                        raise JobRejected(429, f"Your network has used its daily "
                                               f"search limit ({cap} Serper credits "
                                               f"per 24 h). Try again later.")
            if len(self.queue) >= config.MAX_QUEUED_JOBS:
                raise JobRejected(503, "The server is at capacity (too many "
                                       "collections waiting). Try again in a few "
                                       "minutes.")
            job = self.job_factory(self.state_for(client), category, keywords, location,
                                   target, provider, max_queries, geo=geo)
            if job_id:
                job.id = job_id
            job.ip = ip
            job.serper_key = serper_key
            budget = getattr(job, "credit_budget", None)
            # the uncapped budget: restored if the job switches to the
            # user's own key, re-capped if it switches to the server's
            job.base_credit_budget = budget
            if remaining is not None and budget is not None \
                    and (budget == 0 or budget > remaining):
                job.credit_budget = remaining
                job.budget_reason = (
                    f"Daily search limit of your network reached "
                    f"({config.MAX_CREDITS_PER_IP_PER_DAY} Serper credits per 24 h, "
                    f"{remaining} were left for this collection)")
            job.owner = client
            job.status = "queued"
            job.crawl_workers = config.job_crawl_workers()
            job.search_share = self.active_count
            job.on_finish = self._on_finish
            self.jobs[job.id] = job
            self.queue.append(job.id)
            self.by_client[client] = job.id
            self.changed.add(job.id)
            self._schedule()
            if job.status == "queued":
                busy = any(_slot(j) == _slot(job) for j in self.active.values())
                job.say(f"Queued: position {self.queue_position(job.id)} - "
                        + ("another collection is filling this category right now; "
                           "this one starts when it finishes"
                           if busy else f"all {self.max_active} collection slots are "
                                        f"busy; starts automatically when one frees up"))
            return job

    def stop(self, client: str, job_id: str | None = None) -> CollectionJob | None:
        with self.lock:
            jid = job_id or self.by_client.get(client)
            job = self.jobs.get(jid) if jid else None
            if job is None or job.owner != client:
                return None
            job.stop()
            if jid in self.queue:           # never started: finish it right here
                self.queue.remove(jid)
                job.status = "stopped"
                job.stop_reason = "stopped before it started"
                job.finished_at = datetime.now().isoformat(timespec="seconds")
                job.say("Stopped before it started.")
                self._retire(job)
                self._schedule()
            self.changed.add(jid)
            return job

    def set_key(self, client: str, serper_key: str = "") -> CollectionJob | None:
        """The client saved (key) or removed ("") their own Serper key: their
        queued / running job switches to it. Moving onto the server's key
        re-applies the network's daily cap to what the job may still spend
        (it was submitted uncapped on its own key); moving off it lifts the
        cap again. Returns the job, or None when nothing is active."""
        job = self.client_job(client)
        if job is None or job.status not in ACTIVE_STATUSES:
            return None
        # outside self.lock: the job takes its own lock and logs
        job.use_serper_key(serper_key)
        with self.lock:
            if job.status not in ACTIVE_STATUSES:
                return job
            if serper_key:
                base = getattr(job, "base_credit_budget", None)
                if base is not None:
                    job.credit_budget = base
                job.budget_reason = ""
                if getattr(job, "ip_cap_stopped", False) \
                        and getattr(job, "_stop_search", "") == "budget":
                    job._stop_search = ""       # our cap stopped it: searches resume
                job.ip_cap_stopped = False
            else:
                self._apply_ip_cap(job)
            self.changed.add(job.id)
            return job

    def _apply_ip_cap(self, job: CollectionJob):
        """Cap what an active job may still spend on the server's key at what
        its network has left of the daily limit (caller holds self.lock)."""
        cap = config.MAX_CREDITS_PER_IP_PER_DAY
        ip = getattr(job, "ip", "")
        budget = getattr(job, "credit_budget", None)
        if not cap or not ip or budget is None:
            return
        remaining = cap - self._ip_credits_24h(ip)
        spent = _credits(job)
        if remaining > 0:
            if budget == 0 or budget > spent + remaining:
                job.credit_budget = spent + remaining
                job.budget_reason = (
                    f"Daily search limit of your network reached ({cap} Serper "
                    f"credits per 24 h, {remaining} were left for this collection)")
            return
        reason = (f"Daily search limit of your network reached ({cap} Serper credits "
                  f"per 24 h)")
        job.budget_reason = reason
        if job.id in self.queue:          # never started: finish it right here
            job.stop()
            self.queue.remove(job.id)
            job.status = "stopped"
            job.stop_reason = reason + "."
            job.finished_at = datetime.now().isoformat(timespec="seconds")
            job.say(f"Stopped before it started: {reason}. Add your own Serper "
                    f"API key to continue.")
            self._retire(job)
            self._schedule()
            return
        # running: no further searches (a budget of 0 would mean "no cap")
        job.credit_budget = max(1, spent)
        job._stop_search = "budget"
        job.ip_cap_stopped = True
        job.say(f"{reason} - no further searches on the app's key; finishing "
                f"in-flight work. Add your own Serper API key to continue.")

    def stop_all(self, timeout: float = 20.0):
        """Shutdown: stop every job (progress is saved by each job)."""
        with self.lock:
            for jid in list(self.queue):
                self.jobs[jid].stop()
            self.queue.clear()
            running = list(self.active.values())
        for j in running:
            j.stop()
        t_end = time.time() + timeout
        for j in running:
            if j.thread is not None:
                j.thread.join(max(0.1, t_end - time.time()))
        self._stop.set()
        self._save_spend(force=True)

    def category_busy(self, category: str, state: StateStore | None = None) -> bool:
        """A job of `state`'s data (default: any) is filling `category`."""
        with self.lock:
            return any(j.category == category and j.status in ACTIVE_STATUSES
                       and (state is None or j.state is state)
                       for j in self.jobs.values())

    def any_active(self, state: StateStore | None = None) -> bool:
        with self.lock:
            return any(j.status in ACTIVE_STATUSES and (state is None or j.state is state)
                       for j in self.jobs.values())

    def owners_active(self) -> set[str]:
        with self.lock:
            return {j.owner for j in self.jobs.values() if j.status in ACTIVE_STATUSES}

    # -- scheduling -------------------------------------------------------------
    def _schedule(self):
        """Start queued jobs while slots are free (caller holds self.lock)."""
        busy = {_slot(j) for j in self.active.values()}
        for jid in list(self.queue):
            if len(self.active) >= self.max_active:
                break
            job = self.jobs[jid]
            if _slot(job) in busy:
                continue                 # wait for the job filling this category
            self.queue.remove(jid)
            job.crawl_workers = config.job_crawl_workers(
                sum(j.crawl_workers for j in self.active.values()))
            self.active[jid] = job
            busy.add(_slot(job))
            job.status = "pending"
            self.changed.add(jid)
            try:
                job.start()
            except Exception as exc:     # e.g. cannot start a thread
                log.exception("could not start job %s", jid)
                self.active.pop(jid, None)
                job.status, job.error = "failed", f"could not start: {exc}"
                job.stop_reason = job.error
                self._retire(job)

    def _on_finish(self, job: CollectionJob):
        with self.lock:
            if self.active.pop(job.id, None) is not None:
                self._retire(job)
            self.changed.add(job.id)
            self._schedule()

    def _ip_credits_24h(self, ip: str) -> int:
        """Server-key Serper credits spent by `ip` in the last 24 h: finished
        jobs plus what running ones spent on it so far, whichever key they
        hold now (caller holds lock). Jobs from one address that run at once
        can together overshoot the daily limit by at most what they spend in
        parallel (MAX_JOBS_PER_IP of them)."""
        now = time.time()
        spent = self.ip_spend.get(ip)
        while spent and now - spent[0][0] > 86400:
            spent.popleft()
        if spent is not None and not spent:
            del self.ip_spend[ip]
        return (sum(c for _, c in spent or ())
                + sum(_server_credits(j) for j in self.active.values()
                      if getattr(j, "ip", "") == ip))

    def _retire(self, job: CollectionJob):
        """Move a finished job to the bounded history (caller holds lock)."""
        # only the server key's credits count against the network's cap -
        # by what was spent on it, not by the key the job ends with
        server = _server_credits(job)
        if getattr(job, "ip", "") and server:
            self.ip_spend.setdefault(job.ip, deque()).append((time.time(), server))
        self.history.append(job.id)
        while len(self.history) > config.JOB_HISTORY:
            old = self.history.popleft()
            gone = self.jobs.pop(old, None)
            if gone is not None and self.by_client.get(gone.owner) == old:
                del self.by_client[gone.owner]

    def _reaper(self):
        """Safety net: a job thread that died without its finish callback
        (should not happen) must not hold a slot forever."""
        while not self._stop.wait(1.0):
            with self.lock:
                dead = [j for j in self.active.values()
                        if j.thread is not None and not j.thread.is_alive()]
                for j in dead:
                    log.error("job %s thread ended without finishing; releasing "
                              "its slot", j.id)
                    self.active.pop(j.id, None)
                    if j.status in ACTIVE_STATUSES:
                        j.status = "failed"
                        j.error = j.stop_reason = "collection thread ended unexpectedly"
                    self._retire(j)
                    self.changed.add(j.id)
                if dead:
                    self._schedule()
            try:
                self._save_spend()
            except Exception:                # the reaper itself must keep running
                log.exception("saving per-network spend failed")

    # -- per-network spend on disk ----------------------------------------------
    def _load_spend(self):
        """Spend of the last 24 h saved by an earlier worker: a restart must
        not reset the daily cap."""
        if not self.spend_path:
            return
        try:
            with open(self.spend_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("per-network spend unreadable (%s); starting empty", exc)
            return
        now = time.time()
        for ip, rows in (data.get("ip_spend") or {}).items() \
                if isinstance(data, dict) else ():
            try:
                keep = deque(sorted((float(t), int(c)) for t, c in rows
                                    if now - float(t) <= 86400 and int(c) > 0))
            except (TypeError, ValueError):
                continue
            if keep:
                self.ip_spend[str(ip)] = keep

    def _save_spend(self, force: bool = False):
        """Every SPEND_SAVE_S: finished jobs' spend plus running jobs' live
        server-key spend (a worker that dies mid-job keeps it charged; its
        resumed job starts counting from 0)."""
        if not self.spend_path:
            return
        now = time.time()
        if not force and now - self._spend_saved_at < self.SPEND_SAVE_S:
            return
        self._spend_saved_at = now
        with self.lock:
            out: dict[str, list] = {}
            for ip in list(self.ip_spend):
                self._ip_credits_24h(ip)          # drops entries older than 24 h
                if ip in self.ip_spend:
                    out[ip] = [[t, c] for t, c in self.ip_spend[ip]]
            for j in self.active.values():
                ip, c = getattr(j, "ip", ""), _server_credits(j)
                if ip and c:
                    out.setdefault(ip, []).append([now, c])
        blob = json.dumps({"ip_spend": out}, sort_keys=True)
        if blob == self._spend_saved or (not out and not self._spend_saved
                                         and not os.path.exists(self.spend_path)):
            return
        try:
            os.makedirs(os.path.dirname(self.spend_path) or ".", exist_ok=True)
            tmp = f"{self.spend_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(blob)
            os.replace(tmp, self.spend_path)
            self._spend_saved = blob
        except OSError as exc:
            log.warning("could not save per-network spend: %s", exc)
