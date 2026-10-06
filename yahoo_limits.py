"""
yahoo_limits.py
---------------
Keeps bulk Yahoo Finance work inside the limit Yahoo actually enforces.

Measured 2026-10-05, from a GitHub runner and from a home connection alike:
after roughly 2,500 requests within a couple of minutes, Yahoo answers 429
("Too Many Requests") to everything from that IP for about three minutes.
Pacing alone does not avoid it — a run held to ~19 requests/s was cut off at
~2,400 — so bulk work has to make fewer requests, stop the moment Yahoo starts
refusing, and retry what failed once the block has passed.

Two pieces:

  * A refusal counter. install() wraps yfinance's request method so every 429
    is counted. The counter is what makes a refusal visible at all: yfinance
    swallows the error in several places — a refused financial-statement
    fetch comes back as an empty table, indistinguishable from a company that
    has none.
  * YahooGate and run_gated(). The gate closes on the first refusal; workers
    wait out the cool-down, then retry the names that failed.
"""

from __future__ import annotations

import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, Optional

_lock = threading.Lock()
_refusals = 0
_installed = False


def install() -> bool:
    """Count every 429 yfinance receives. Idempotent. Returns False when
    yfinance's internals have moved, in which case only the refusals that
    surface as exceptions are seen."""
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
        _count_refusals(YfData, YFRateLimitError)
        _installed = True
        return True


def _count_refusals(cls, error: type) -> None:
    """Wrap cls.get so each `error` it raises is counted, then re-raised."""
    original = cls.get

    @functools.wraps(original)
    def get(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except error:
            note_refusal()
            raise

    cls.get = get


def note_refusal() -> None:
    global _refusals
    with _lock:
        _refusals += 1


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


class YahooGate:
    """Closes on Yahoo's first refusal and reopens after a cool-down.

    Workers call wait() before each name. trip() closes the gate — once per
    block, however many workers saw the refusal — and returns False once the
    scan has used up its cool-downs, so the caller gives up on what is left
    instead of waiting indefinitely.
    """

    def __init__(self, cooldown_s: float = 240.0, max_trips: int = 3,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 log: Callable[[str], None] = print):
        self.cooldown_s = cooldown_s
        self.max_trips = max_trips
        self.trips = 0
        self.exhausted = False       # out of cool-downs: stop sending requests
        self._sleep, self._clock, self._log = sleep, clock, log
        self._lock = threading.Lock()
        self._reopen_at = 0.0

    def wait(self) -> None:
        while True:
            with self._lock:
                left = self._reopen_at - self._clock()
            if left <= 0:
                return
            self._sleep(min(left, 5.0))

    def trip(self) -> bool:
        """Report a refusal. True: wait() and retry. False: out of cool-downs."""
        with self._lock:
            now = self._clock()
            if now < self._reopen_at:      # this block is already being waited out
                return True
            if self.trips >= self.max_trips:
                if not self.exhausted:
                    self.exhausted = True
                    self._log(f"[yahoo] Still rate limited after "
                              f"{self.max_trips} cool-downs — giving up on "
                              f"the names left.")
                return False
            self.trips += 1
            self._reopen_at = now + self.cooldown_s
            n = self.trips
        self._log(f"[yahoo] Rate limited (429) — pausing {self.cooldown_s:.0f}s, "
                  f"then retrying (cool-down {n} of {self.max_trips}).")
        return True


def run_gated(items: Iterable, fn: Callable[[object], bool], *,
              gate: YahooGate, workers: int = 6, attempts: int = 3,
              watch_counter: bool = False,
              on_done: Optional[Callable[[object], None]] = None) -> list:
    """Run fn over items on a thread pool, retrying through `gate`.

    fn(item) does the work and returns True when its own result shows a
    refusal. With watch_counter, an attempt during which the process received
    any 429 also counts as refused — that is how a swallowed refusal (an
    empty financial statement) gets retried instead of scored. on_done(item)
    runs after each item's last attempt, for progress output.

    Returns the items still refused after `attempts` tries or once the gate
    ran out of cool-downs; the caller decides what to record for them.
    """
    items = list(items)
    if not items:
        return []

    def one(item) -> bool:
        try:
            for _ in range(attempts):
                gate.wait()
                if gate.exhausted:
                    return False
                before = refusals()
                refused = fn(item)
                if watch_counter and refusals() != before:
                    refused = True
                if not refused:
                    return True
                if not gate.trip():
                    return False
            return False
        finally:
            if on_done is not None:
                on_done(item)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items)))) as ex:
        ok = list(ex.map(one, items))
    return [item for item, good in zip(items, ok) if not good]
