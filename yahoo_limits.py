"""
yahoo_limits.py
---------------
Keeps bulk Yahoo Finance work inside the limit Yahoo actually enforces.

Yahoo answers 429 ("Too Many Requests") to everything from an IP once it has
made a few thousand requests in a short span: ~2,500 in tests from a home
connection on 2026-10-05, ~2,600 and ~3,800 on two GitHub runners. From home
the block lifted after about three minutes. On a runner it did not: it
outlasted three 4-minute pauses and was still refusing some requests 20
minutes later, by which time the holdings analysis was running into it
(2026-10-06, the first scan with this module: GOOG and BX errored). Pacing
doesn't help either; a run held to ~19 requests/s was cut off at ~2,400.

So bulk work here never waits out a block. It spends a fixed allowance of
requests per run, stops at the first refusal, and leaves the rest for the
next run — which, in CI, starts on a fresh IP.

Two pieces:

  * Counters. install() wraps yfinance's request method to count every
    request and every 429. The refusal count is what makes a refusal visible
    at all: yfinance swallows the error in several places — a refused
    financial-statement fetch comes back as an empty table, indistinguishable
    from a company that has none.
  * YahooBudget and run_gated(): one run's allowance, and a thread pool that
    stops starting work once the allowance is spent or Yahoo refuses.
"""

from __future__ import annotations

import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Optional

_lock = threading.Lock()
_requests = 0
_refusals = 0
_installed = False


def install() -> bool:
    """Count every request and 429 that goes through yfinance. Idempotent.
    Returns False when yfinance's internals have moved, in which case only
    the refusals that surface as exceptions are seen."""
    global _installed
    with _lock:
        if _installed:
            return True
        try:
            from yfinance.data import YfData
            from yfinance.exceptions import YFRateLimitError
        except ImportError:
            return False
        if getattr(YfData, "get", None) is None:
            return False
        _count_requests(YfData, YFRateLimitError)
        _installed = True
        return True


def _count_requests(cls, error: type) -> None:
    """Wrap cls.get so each call is counted, and each `error` it raises is
    counted as a refusal and re-raised."""
    original = cls.get

    @functools.wraps(original)
    def get(self, *args, **kwargs):
        note_request()
        try:
            return original(self, *args, **kwargs)
        except error:
            note_refusal()
            raise

    cls.get = get


def note_request() -> None:
    global _requests
    with _lock:
        _requests += 1


def note_refusal() -> None:
    global _refusals
    with _lock:
        _refusals += 1


def requests() -> int:
    """How many requests this process has sent through yfinance."""
    with _lock:
        return _requests


def refusals() -> int:
    """How many 429s this process has received so far."""
    with _lock:
        return _refusals


def is_rate_limited(err) -> bool:
    """True for a YFRateLimitError, or an error string that records one."""
    if err is None:
        return False
    text = err if isinstance(err, str) else f"{type(err).__name__}: {err}"
    return ("YFRateLimitError" in text or "Too Many Requests" in text
            or "Rate limited" in text)


class YahooBudget:
    """One run's allowance of Yahoo requests, and its stop switch.

    `limit` is how many yfinance requests the run may make (None: no cap).
    The first refusal stops the run outright — waiting a block out costs
    more than it saves (see the module docstring)."""

    def __init__(self, limit: Optional[int] = None,
                 log: Callable[[str], None] = print):
        self.limit = limit
        self.stopped = False         # Yahoo refused: send nothing more
        self._start = requests()
        self._log = log
        self._lock = threading.Lock()

    def used(self) -> int:
        return requests() - self._start

    def spent(self) -> bool:
        return self.stopped or (self.limit is not None
                                and self.used() >= self.limit)

    def refused(self) -> None:
        with self._lock:
            if self.stopped:
                return
            self.stopped = True
        self._log("[yahoo] Rate limited (429) — stopping here; the next run "
                  "picks up the rest.")


def run_gated(items: Iterable, fn: Callable[[object], bool], *,
              budget: YahooBudget, workers: int = 6,
              watch_counter: bool = False,
              on_done: Optional[Callable[[object], None]] = None) -> list:
    """Run fn over items on a thread pool until `budget` is spent.

    fn(item) does the work and returns True when its own result shows a
    refusal. With watch_counter, an attempt during which the process received
    any 429 also counts as refused — that is how a swallowed refusal (an
    empty financial statement) is caught instead of scored. A refusal stops
    the budget; items not yet started then stay unstarted. on_done(item) runs
    after each item that was started, for progress output.

    Returns the items not done — refused, or never started — in input order.
    """
    items = list(items)
    if not items:
        return []

    def one(item) -> bool:
        if budget.spent():
            return False
        try:
            before = refusals()
            refused = fn(item)
            if watch_counter and refusals() != before:
                refused = True
            if refused:
                budget.refused()
            return not refused
        finally:
            if on_done is not None:
                on_done(item)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as ex:
        ok = list(ex.map(one, items))
    return [item for item, good in zip(items, ok) if not good]
