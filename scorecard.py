"""
scorecard.py
------------
The report's track record: what each verdict said on a given day, and what
the stock did next.

The recommendation ledger (recs_history.json) keeps each ticker's FIRST and
LATEST verdict. That answers "what did we miss?" but not "do the calls work?",
which needs every day's call. So this module keeps a second ledger,
verdict_log.json:

  * days    — one snapshot per market day of every analyzed ticker: its verdict
              and score under every base-score mode, and the inputs behind
              them. Each run overwrites the day's entry for the tickers it
              analyzed, so a day's call is that day's last (near-close) call.
  * closes  — daily closes for every ticker with a call still maturing, plus
              the S&P 500. Fetched once per session, after the close.
  * found   — every name the universe scan added to the Screening watchlist:
              the day, the channel that found it and where it was found (in
              the S&P 500/400, another US company, or based abroad). Graded
              from the day it was added, so each source has a track record.

From those it measures each call's return, and its excess over the S&P 500
(alpha), 1 week, 1 month and 3 months later. It rolls them up into the
scorecard the report shows: how each verdict label did, whether higher scores
meant better returns, which base-score mode ranks stocks best, and which
inputs carry signal. A few conservative rules turn that into suggestions for
the scoring model.

Pure data in, data out: no PositionAnalysis and no HTML. The analyzer builds
the day's calls and renders the result, and Yahoo is reached only through a
fetch function the caller passes in, so the tests run offline.

Two measurement choices worth knowing:

  * Entry and exit are official daily closes (split-adjusted, not dividend-
    adjusted, the same basis as the ^GSPC price index), not the prices the
    runs saw. That keeps every return on one basis and survives splits, which
    the logged prices would not.
  * Pruned names keep being priced until their last call matures. The prune
    removes exactly the names the model rated worst, so dropping them would
    flatter the scorecard (survivorship bias).
"""

from __future__ import annotations

import functools
import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta
from typing import Callable, Optional

import numpy as np

LOG_FILE = "verdict_log.json"
LOG_VERSION = 1

# (label, calendar days). Calendar days rather than trading days so a missed
# run, a holiday or a runner outage can't shift what "one month" means.
HORIZONS = (("1W", 7), ("1M", 30), ("3M", 91))
HORIZON_NAMES = {"1W": "1 week", "1M": "1 month", "3M": "3 months"}
# An exit is the first close on or after day D+h, as long as it lands within
# this many days of it. Longer gaps (a delisting, a data hole) give no exit.
HORIZON_GRACE_D = 6
MAX_HORIZON_D = max(d for _, d in HORIZONS)
# Calls are kept this long. Every horizon has matured well before then.
RETENTION_D = 400

BENCHMARK = "^GSPC"
# A session's close is final from this ET time on. Before it, the latest
# complete session is the previous weekday.
SESSION_DONE_ET = (16, 30)
# Closes overlap the stored series by this many days on each fetch, so a
# split shows up as a jump between the stored and the fetched close of the
# same date and the stored history can be rescaled to match.
CLOSE_OVERLAP_D = 7
SPLIT_TOLERANCE = 0.05
# A fetch this many days past a series' last close that brings nothing newer
# means the ticker is gone (delisted, renamed); it isn't asked for again.
STALE_SERIES_D = 14

# A day needs this many scored calls before its rank correlation counts.
MIN_IC_CALLS = 8
# A label, tier or score bucket needs this many calls before a suggestion
# leans on its average.
MIN_GROUP_CALLS = 20
# Ranking-skill thresholds used by the suggestions: an IC this far from zero,
# with a t-stat at least this big, over at least this many non-overlapping
# periods.
IC_MATERIAL = 0.05
IC_STRONG = 0.08
IC_T_STAT = 2.0
# Real daily ICs always vary; this floor only stops identical ones dividing
# by zero in the t-stat.
IC_SD_FLOOR = 0.01
MIN_WINDOWS = 3
# A base that rates more than this share of names buy-grade has bars that no
# longer discriminate, whatever its ranking skill.
MAX_BUY_SHARE = 0.35

BUY_GRADE = frozenset({"BUY", "ADD"})
NEUTRAL = frozenset({"HOLD", "WATCH"})
BELOW_BAR = frozenset({"TRIM", "SELL", "WAIT", "PASS"})
TIERS = (("buy", "Buy-grade", BUY_GRADE),
         ("neutral", "Hold / watch", NEUTRAL),
         ("below", "Below the bar", BELOW_BAR))
# Fresh-money vocabulary first, then the holdings one, strongest first.
LABEL_ORDER = ("BUY", "WATCH", "WAIT", "PASS", "ADD", "HOLD", "TRIM", "SELL")

# How the screen's additions are grouped (record_found). Channels are the
# screen's ways of finding a name; ponds say where it was found, which grades
# the move from the S&P 500/400 to the whole US market.
FOUND_CHANNELS = (("quality", "Nine-filter screen"),)
FOUND_PONDS = (("sp", "In the S&P 500/400"),
               ("us", "Other US companies"),
               ("intl", "Based outside the US"))

# Verdict-score buckets, highest first: (lower bound, label). 75 and 78 are
# the watchlist BUY and holdings ADD bars, so 70-75 is "just under the bar".
SCORE_BUCKETS = ((80, "80+"), (75, "75–80"), (70, "70–75"), (60, "60–70"),
                 (50, "50–60"), (40, "40–50"), (0, "<40"))
ABOVE_BAR_BUCKETS = ("75–80", "80+")
NEAR_BAR_BUCKET = "70–75"

# The inputs graded against forward returns: (key, label, the part it plays
# in the verdict, the direction the model leans on it). Direction +1: the
# model treats a higher value as better. 52-week position is the exception:
# the verdict docks names near their high and credits quality names near
# their low. Direction 0: shown beside the verdict but not scored (see
# UNSCORED_FACTORS).
FACTORS = (
    ("q", "Quality", "30% of the Composite", +1),
    ("g", "Growth", "20% of the Composite", +1),
    ("v", "Value", "20% of the Composite", +1),
    ("vc", "Value (recalibrated)", "Value's recalibrated twin", +1),
    ("a", "Analyst", "15% of every base", +1),
    ("i", "Insider", "15% of every base", +1),
    ("fs", "Nine filters", "70% of the Quality base", +1),
    ("ma", "Price vs 200-day", "the trend modifier, ±10", +1),
    ("w52", "52-week position", "a modifier, −4 near highs to +5 near lows", -1),
    ("up", "Upside to target", "a modifier, −10 to +6", +1),
    ("sm", "Sector momentum", "a modifier, −4 to +5", +1),
    ("n", "News sentiment", "a modifier, ±6", +1),
    ("c", "Composite", "the Composite base", +1),
    ("qb", "Quality base", "the Quality base", +1),
    ("vol", "Volatility", "shown, not scored", 0),
    ("ddp", "Drop vs its normal", "shown, not scored", 0),
)
# Only these get weight suggestions; c and qb are whole bases, and vc is a
# variant the mode comparison already covers.
WEIGHTED_FACTORS = ("q", "g", "v", "a", "i", "ma", "w52", "up", "sm", "n")
# Price-risk reads the report shows but doesn't score (risk_signals): a year's
# volatility, and how far below its 52-week high a stock is against its own
# history (the share of days it was less far down). They are graded like the
# inputs, and a significant result says which way scoring them would lean:
# {key: (finding when the rank correlation is negative, when positive)}.
UNSCORED_FACTORS = {
    "vol": ("Higher-volatility names lagged the S&P over {span} (rank "
            "correlation {ic:+.2f}, {windows} independent periods). "
            "Volatility is shown, not scored; this is the case for docking it.",
            "Higher-volatility names beat the S&P over {span} (rank "
            "correlation {ic:+.2f}, {windows} independent periods). Docking "
            "volatility would have cost returns."),
    "ddp": ("Names further below their 52-week high than usual kept lagging "
            "the S&P over {span} (rank correlation {ic:+.2f}, {windows} "
            "independent periods). The drop read is shown, not scored; this "
            "is the case for scoring an unusual drop as a warning.",
            "Names further below their 52-week high than usual went on to "
            "beat the S&P over {span} (rank correlation {ic:+.2f}, {windows} "
            "independent periods). An unusual drop has not been a reason to "
            "rate a name lower."),
}

# The recs ledger's factor names (analyze_portfolio._rec_factors) -> the
# short keys stored here. Short keys keep a year of daily calls near 10 MB.
LEDGER_FACTOR_KEYS = {
    "score_quality": "q", "score_growth": "g", "score_value": "v",
    "score_value_cal": "vc", "score_analyst": "a", "score_insider": "i",
    "filter_score": "fs", "pct_above_ma200": "ma", "week52_position": "w52",
    "upside_pct": "up", "composite_score": "c", "quality_base": "qb",
    "volatility": "vol", "drop_severity": "ddp",
}
SECTOR_MOMENTUM_VALUE = {"Hot": 1, "Neutral": 0, "Cool": -1}


# ------------------------------------------------------------------ log ----

def empty_log() -> dict:
    return {"version": LOG_VERSION, "days": {}, "closes": {}, "fetched": {},
            "found": []}


def load_log(path: str = LOG_FILE) -> dict:
    """The verdict log, or an empty one when the file is missing or corrupt."""
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("days"), dict):
            for key in ("closes", "fetched"):
                if not isinstance(data.get(key), dict):
                    data[key] = {}
            if not isinstance(data.get("found"), list):
                data["found"] = []
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return empty_log()


def save_log(log: dict, path: str = LOG_FILE) -> None:
    """Write the log compactly, via a temp file so a killed run can't leave
    half a file for the next one to discard."""
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".verdict_log.", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(log, f, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def compact_factors(factors: Optional[dict],
                    news_score: Optional[float] = None) -> dict:
    """The recs ledger's factor snapshot, in this log's short keys."""
    out: dict = {}
    for long_key, short in LEDGER_FACTOR_KEYS.items():
        v = (factors or {}).get(long_key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[short] = round(float(v), 1)
    sm = SECTOR_MOMENTUM_VALUE.get((factors or {}).get("sector_momentum"))
    if sm is not None:
        out["sm"] = sm
    if isinstance(news_score, (int, float)) and not isinstance(news_score, bool):
        out["n"] = round(float(news_score), 2)
    return out


def record_day(log: dict, day: str, calls: dict) -> int:
    """Merge this run's calls into the day's snapshot. A ticker this run
    analyzed replaces its earlier call today; tickers it didn't (an analysis
    that errored this time) keep theirs. Returns how many calls it wrote."""
    if not calls:
        return 0
    log.setdefault("days", {}).setdefault(day, {}).update(calls)
    if not log.get("started") or day < log["started"]:
        log["started"] = day
    return len(calls)


def record_found(log: dict, day: str, entries: list[dict]) -> int:
    """Log the names the screen added to the Screening watchlist today:
    entries of {"t": ticker, "ch": channel, "pond": pond or None}. A name
    already logged for the day isn't logged twice. Returns how many it wrote."""
    found = log.setdefault("found", [])
    seen = {(e.get("t"), e.get("d")) for e in found}
    added = 0
    for e in entries:
        if not e.get("t") or (e["t"], day) in seen:
            continue
        found.append({"t": e["t"], "d": day, "ch": e.get("ch"),
                      "pond": e.get("pond")})
        seen.add((e["t"], day))
        added += 1
    return added


def seed_from_ledger(log: dict, ledger: Optional[dict], today: str) -> int:
    """Once, recover the calls the recs ledger already holds: each ticker's
    first sighting, with the verdict, score and factors it was first seen at.
    That gives the scorecard matured results on day one instead of in a month.

    Entries that predate the base-score modes only know the verdict the
    Composite gave, so they are filed under "composite". Seeded calls are
    flagged, never overwrite a logged call, and are left out of the mode
    comparison and the log's own day count."""
    entries = (ledger or {}).get("tickers") or {}
    # An empty ledger (a missing cache, a failed read) doesn't use up the one
    # chance to seed: the next run that can read it still recovers its calls.
    if log.get("seeded") or not entries:
        return 0
    days = log.setdefault("days", {})
    added = 0
    for ticker, e in entries.items():
        day, price = e.get("first_date"), e.get("first_price")
        if not day or not price:
            continue
        first = e.get("first_factors") or {}
        verdicts = {}
        for mode, pair in (first.get("verdicts") or {}).items():
            if isinstance(pair, (list, tuple)) and pair and pair[0]:
                verdicts[mode] = [pair[0], pair[1] if len(pair) > 1 else None]
        if not verdicts and e.get("first_verdict"):
            verdicts = {"composite": [e["first_verdict"],
                                      e.get("first_verdict_score")]}
        if not verdicts:
            continue
        day_calls = days.setdefault(day[:10], {})
        if ticker in day_calls:
            continue
        day_calls[ticker] = {
            "p": price, "h": 1 if (e.get("first_alloc") or 0) > 0 else 0,
            "v": verdicts, "f": compact_factors(first), "s": 1,
        }
        added += 1
    log["seeded"] = today
    return added


def prune_log(log: dict, today: date) -> None:
    """Drop calls past retention, closes no remaining call can use, and fetch
    stamps for tickers no longer in the log."""
    cutoff = (today - timedelta(days=RETENTION_D)).isoformat()
    days = log.get("days") or {}
    for d in [d for d in days if d < cutoff]:
        del days[d]
    found = log["found"] = [e for e in log.get("found") or []
                            if e.get("d", "") >= cutoff]
    if not days and not found:
        log["closes"], log["fetched"] = {}, {}
        return
    first = min([*days, *(e["d"] for e in found)])
    keep_from = (date.fromisoformat(first) - timedelta(days=14)).isoformat()
    tickers = ({t for calls in days.values() for t in calls}
               | {e["t"] for e in found} | {BENCHMARK})
    closes = log.get("closes") or {}
    for t in list(closes):
        if t not in tickers:
            del closes[t]
            continue
        series = closes[t]
        for d in [d for d in series if d < keep_from]:
            del series[d]
    fetched = log.get("fetched") or {}
    for t in [t for t in fetched if t not in tickers]:
        del fetched[t]


# ---------------------------------------------------------- turned buy ----

# A name whose buy-grade run began this many calendar days ago or less (today
# counts as day 1) is flagged as having turned buy-grade: a week of sessions.
TURNED_BUY_WINDOW_D = 7
# The call before the run must be this recent to say what it changed from.
# Further back, the name went unanalyzed too long to call it a change.
TURNED_BUY_GAP_D = 7


def turned_buy_grade(log: dict, today: str,
                     labels: dict[str, dict[str, Optional[str]]],
                     window_d: int = TURNED_BUY_WINDOW_D,
                     gap_d: int = TURNED_BUY_GAP_D) -> dict[str, dict[str, dict]]:
    """Which names are buy-grade now after something else, and since when.

    `labels` is {ticker: {mode: this run's label}}. Each earlier day's call
    is its last of the day, so a verdict that dips below the bar and back
    within one session isn't a change. Walking back from today, a buy-grade
    day extends the run, a day without a call for the name is skipped, and
    the first other label ends the walk. Seeded calls are left out: they are
    one-off first sightings, often from long before the log began.

    Returns {ticker: {mode: {"from": label, "since": first buy-grade day,
    "was_on": the day of that last other call, "age_d": calendar days since
    `since`, 0 for today}}}, holding only the modes where the run began within
    `window_d` days. A name that has been buy-grade on every call we have
    isn't a change, so it gets nothing."""
    cutoff = _shift(today, -(window_d - 1))
    earliest = _shift(cutoff, -gap_d)
    days = log.get("days") or {}
    past = sorted((d for d in days if earliest <= d < today), reverse=True)
    out: dict[str, dict[str, dict]] = {}
    for ticker, by_mode in labels.items():
        for mode, label in (by_mode or {}).items():
            if label not in BUY_GRADE:
                continue
            since = today
            for d in past:
                if since < cutoff or d < _shift(since, -gap_d):
                    break
                call = days[d].get(ticker)
                if not call or call.get("s"):
                    continue
                pair = (call.get("v") or {}).get(mode)
                if not pair or not pair[0]:
                    continue
                if pair[0] in BUY_GRADE:
                    since = d
                    continue
                out.setdefault(ticker, {})[mode] = {
                    "from": pair[0], "since": since, "was_on": d,
                    "age_d": (date.fromisoformat(today)
                              - date.fromisoformat(since)).days}
                break
    return out


# --------------------------------------------------------------- closes ----

def last_session(now_et: datetime) -> str:
    """The most recent session whose close is final: today once it is past
    SESSION_DONE_ET on a weekday, else the weekday before. Market holidays
    aren't known here; a holiday just costs one fetch that returns nothing
    new."""
    d = now_et.date()
    if d.weekday() < 5 and (now_et.hour, now_et.minute) >= SESSION_DONE_ET:
        return d.isoformat()
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def closes_wanted(log: dict, session: str) -> dict[str, str]:
    """{ticker: fetch-from date} for every series that needs closes it doesn't
    have yet. A ticker needs closes from just before its earliest call (or
    screen addition) to its latest one's last exit (or the session, if
    sooner). The benchmark needs
    the whole span. A series fetched this session isn't asked for again, and
    one that a fetch STALE_SERIES_D or more after its last close (or its first
    call, if it has none) still couldn't extend is given up on: a delisted or
    renamed ticker. Keying that on the fetch, not the calendar, means an
    outage that leaves every series weeks behind doesn't read as delisting."""
    days = log.get("days") or {}
    found = log.get("found") or []
    if not days and not found:
        return {}
    span: dict[str, list[str]] = {}
    # A screen addition is graded from the day it was added, whether or not
    # that day's analysis made it into the log.
    dated = [(d, t) for d, calls in days.items() for t in calls]
    dated += [(e["d"], e["t"]) for e in found]
    for d, t in dated:
        lo_hi = span.setdefault(t, [d, d])
        lo_hi[0], lo_hi[1] = min(lo_hi[0], d), max(lo_hi[1], d)
    span[BENCHMARK] = [min(d for d, _ in dated), session]
    closes, fetched = log.get("closes") or {}, log.get("fetched") or {}
    wanted: dict[str, str] = {}
    for t, (first, last) in span.items():
        if fetched.get(t, "") >= session:
            continue
        until = min(session, (date.fromisoformat(last)
                              + timedelta(days=MAX_HORIZON_D + HORIZON_GRACE_D)
                              ).isoformat())
        series = closes.get(t) or {}
        last_try = fetched.get(t, "")
        if t != BENCHMARK and last_try and last_try >= _shift(
                max(series) if series else first, STALE_SERIES_D):
            continue
        need_from = (date.fromisoformat(first) - timedelta(days=7)).isoformat()
        if not series or min(series) > first:
            wanted[t] = need_from
        elif max(series) < until:
            wanted[t] = max(need_from, (date.fromisoformat(max(series))
                                        - timedelta(days=CLOSE_OVERLAP_D)
                                        ).isoformat())
    return wanted


def merge_closes(stored: Optional[dict], fetched: dict
                 ) -> tuple[dict, Optional[float]]:
    """Merge freshly fetched closes into a stored series. Yahoo's closes are
    split-adjusted as of the fetch, so after a split the stored history is on
    the old share count: the same date then differs by the split ratio, and
    the stored series is rescaled by it before merging. Returns the merged
    series and the ratio applied (None when there was no split)."""
    stored = dict(stored or {})
    ratio = None
    overlap = sorted(set(stored) & set(fetched))
    if overlap:
        d = overlap[-1]
        if stored[d] and fetched[d]:
            r = fetched[d] / stored[d]
            if abs(r - 1) > SPLIT_TOLERANCE:
                ratio = r
                stored = {k: round(v * r, 4) for k, v in stored.items()}
    stored.update(fetched)
    return stored, ratio


def refresh_closes(log: dict, now_et: datetime,
                   fetch: Callable[[str, str], tuple[Optional[dict], bool]],
                   run: Callable, log_fn: Callable[[str], None] = print) -> dict:
    """Fetch the closes the log is missing, through `run` — the caller's
    budgeted runner (yahoo_limits.run_gated with a YahooBudget, in practice),
    called as run(items, fn) and returning the items not done.

    fetch(ticker, start_iso) returns (closes or None, refused). Closes dated
    after the last complete session are dropped: a bar for a session still
    trading is a quote, not a close, and would read as a split tomorrow.
    Returns {"wanted", "fetched", "left", "splits"}."""
    session = last_session(now_et)
    wanted = closes_wanted(log, session)
    closes = log.setdefault("closes", {})
    stamps = log.setdefault("fetched", {})
    splits: dict[str, float] = {}
    done: list[str] = []

    def one(ticker: str) -> bool:
        got, refused = fetch(ticker, wanted[ticker])
        if refused:
            return True
        final = {d: round(float(c), 4) for d, c in (got or {}).items()
                 if d <= session and c and c > 0}
        merged, ratio = merge_closes(closes.get(ticker), final)
        if merged:
            closes[ticker] = merged
        if ratio is not None:
            splits[ticker] = ratio
        stamps[ticker] = session
        done.append(ticker)
        return False

    left = run(sorted(wanted), one) if wanted else []
    for t, r in sorted(splits.items()):
        log_fn(f"[scorecard] {t}: rescaled stored closes by {r:.4g} "
               f"(a split since the last fetch).")
    return {"wanted": len(wanted), "fetched": len(done),
            "left": len(left), "splits": splits}


# ----------------------------------------------------------- evaluation ----

@functools.lru_cache(maxsize=None)
def _shift(d: str, days: int) -> str:
    """ISO date d moved by `days` (cached: evaluation asks for the same few
    hundred dates tens of thousands of times)."""
    return (date.fromisoformat(d) + timedelta(days=days)).isoformat()


class _Series:
    """One ticker's closes, for date lookups."""

    def __init__(self, series: Optional[dict]):
        self.closes = series or {}
        self.dates = sorted(self.closes)

    def on_or_after(self, d: str, within: int) -> Optional[str]:
        import bisect
        i = bisect.bisect_left(self.dates, d)
        if i >= len(self.dates):
            return None
        hit = self.dates[i]
        return hit if hit <= _shift(d, within) else None

    def on_or_before(self, d: str, within: int = 4) -> Optional[str]:
        import bisect
        i = bisect.bisect_right(self.dates, d) - 1
        if i < 0:
            return None
        hit = self.dates[i]
        return hit if hit >= _shift(d, -within) else None


def evaluate_calls(log: dict, horizons=HORIZONS) -> list[dict]:
    """Every logged call that has an entry close, with its forward return and
    alpha (both in %) at each horizon that has matured. A horizon that hasn't
    matured, or whose close is missing, is simply absent.

    Entry: the close of the call's day (the first close within 4 days after,
    for a call made on a non-trading day). Exit: the first close on or after
    entry + h days, within HORIZON_GRACE_D. Alpha: return minus the S&P 500's
    over the same two dates."""
    closes = log.get("closes") or {}
    forward = _Forward(closes)
    out: list[dict] = []
    for day in sorted(log.get("days") or {}):
        for ticker, call in log["days"][day].items():
            got = forward(ticker, day, horizons)
            if got is None:
                continue
            rets, alphas = got
            out.append({"date": day, "ticker": ticker,
                        "held": bool(call.get("h")), "seed": bool(call.get("s")),
                        "v": call.get("v") or {}, "f": call.get("f") or {},
                        "ret": rets, "alpha": alphas})
    return out


class _Forward:
    """Forward returns and alphas from a day's close, over the log's closes."""

    def __init__(self, closes: dict):
        self.closes = closes
        self.bench = _Series(closes.get(BENCHMARK))
        self.cache: dict[str, _Series] = {}

    def __call__(self, ticker: str, day: str, horizons
                 ) -> Optional[tuple[dict, dict]]:
        """({horizon: return %}, {horizon: alpha %}) from the close of `day`
        (the first close within 4 days after, for a non-trading day), or None
        without one."""
        s = self.cache.get(ticker)
        if s is None:
            s = self.cache[ticker] = _Series(self.closes.get(ticker))
        entry_d = s.on_or_after(day, 4)
        if entry_d is None:
            return None
        entry = s.closes[entry_d]
        bench = self.bench
        b_entry_d = bench.on_or_before(entry_d)
        rets, alphas = {}, {}
        for name, h in horizons:
            exit_d = s.on_or_after(_shift(entry_d, h), HORIZON_GRACE_D)
            if exit_d is None or not entry:
                continue
            ret = (s.closes[exit_d] / entry - 1) * 100
            rets[name] = round(ret, 3)
            b_exit_d = bench.on_or_before(exit_d)
            if b_entry_d and b_exit_d and bench.closes[b_entry_d]:
                b_ret = (bench.closes[b_exit_d]
                         / bench.closes[b_entry_d] - 1) * 100
                alphas[name] = round(ret - b_ret, 3)
        return rets, alphas


def evaluate_found(log: dict, horizons=HORIZONS) -> list[dict]:
    """Every screen addition with a close on its day, with its forward return
    and alpha at each matured horizon, like evaluate_calls."""
    forward = _Forward(log.get("closes") or {})
    out: list[dict] = []
    for e in log.get("found") or []:
        got = forward(e["t"], e["d"], horizons)
        if got is not None:
            out.append({**e, "ret": got[0], "alpha": got[1]})
    return out


def _found_group(rows: list[dict], h: str) -> dict:
    """n, mean return, mean alpha and the share that beat the S&P, at h."""
    pairs = [(r["ret"][h], r["alpha"][h]) for r in rows if h in r["alpha"]]
    if not pairs:
        return {"n": 0, "ret": None, "alpha": None, "beat": None}
    n = len(pairs)
    return {"n": n, "ret": round(sum(p[0] for p in pairs) / n, 2),
            "alpha": round(sum(p[1] for p in pairs) / n, 2),
            "beat": round(sum(1 for p in pairs if p[1] > 0) / n, 3)}


def found_scorecard(log: dict, horizons=HORIZONS) -> dict:
    """How the screen's additions did since the day each was added: all of
    them, by channel (once there is more than one) and by pond. Rows are
    (key, label, {horizon: group}); a group nothing was found in is left out."""
    found = log.get("found") or []
    rows = evaluate_found(log, horizons)
    groups = [("all", "All screen additions", lambda e: True)]
    if len({e.get("ch") for e in found}) > 1:
        groups += [(f"ch:{key}", label, lambda e, k=key: e.get("ch") == k)
                   for key, label in FOUND_CHANNELS]
    groups += [(f"pond:{key}", label, lambda e, k=key: e.get("pond") == k)
               for key, label in FOUND_PONDS]
    return {
        "n": len(found),
        "since": min((e["d"] for e in found), default=None),
        "matured": {h: sum(1 for r in rows if h in r["alpha"])
                    for h, _ in horizons},
        # A group shows once anything was found in it, priced or not.
        "rows": [(key, label, {h: _found_group([r for r in rows if keep(r)], h)
                               for h, _ in horizons})
                 for key, label, keep in groups if any(keep(e) for e in found)],
    }


# ------------------------------------------------------------ statistics ----

def _ranks(xs) -> np.ndarray:
    """1-based ranks, ties sharing their average rank (scipy's rankdata)."""
    a = np.asarray(xs, dtype=float)
    order = np.argsort(a, kind="mergesort")
    inverse = np.empty(len(a), dtype=np.intp)
    inverse[order] = np.arange(len(a))
    a = a[order]
    first = np.empty(len(a), dtype=bool)
    first[0] = True
    np.not_equal(a[1:], a[:-1], out=first[1:])
    dense = first.cumsum()[inverse]
    bounds = np.append(np.flatnonzero(first), len(first))
    return 0.5 * (bounds[dense] + bounds[dense - 1] + 1)


def spearman(xs, ys) -> Optional[float]:
    """Spearman rank correlation, or None when either side has no spread."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    rx -= rx.mean()
    ry -= ry.mean()
    sxx, syy = float(rx @ rx), float(ry @ ry)
    if sxx <= 0 or syy <= 0:
        return None
    return float(rx @ ry) / math.sqrt(sxx * syy)


def independent_windows(dates: list[str], h_days: int) -> int:
    """How many of these dates are at least h_days apart, counted greedily.
    Daily calls at a 1-month horizon overlap almost entirely, so 60 trading
    days of them are ~3 independent looks, not 60. This is the sample size
    the t-stat uses and what the confidence label reads."""
    count, last = 0, None
    for d in sorted(set(dates)):
        cur = date.fromisoformat(d)
        if last is None or (cur - last).days >= h_days:
            count += 1
            last = cur
    return count


def _ic_from_groups(groups, h_days: int) -> dict:
    """The rank-IC summary from per-day (date, scores, alphas) groups."""
    ics, used = [], []
    n = 0
    for d, xs, ys in groups:
        if len(xs) < MIN_IC_CALLS:
            continue
        ic = spearman(xs, ys)
        if ic is None:
            continue
        ics.append(ic)
        used.append(d)
        n += len(xs)
    if not ics:
        return {"ic": None, "days": 0, "windows": 0, "t": None, "n": 0}
    mean = sum(ics) / len(ics)
    windows = independent_windows(used, h_days)
    t = None
    if len(ics) >= 2 and windows >= 2:
        sd = math.sqrt(sum((x - mean) ** 2 for x in ics) / (len(ics) - 1))
        # Floored, so days that all agree exactly read as very significant
        # rather than as a division by zero.
        t = mean / (max(sd, IC_SD_FLOOR) / math.sqrt(windows))
    return {"ic": round(mean, 4), "days": len(ics), "windows": windows,
            "t": round(t, 2) if t is not None else None, "n": n}


def rank_ic(pairs_by_date: dict[str, list[tuple[float, float]]],
            h_days: int) -> dict:
    """Mean per-day Spearman correlation between a score and the forward
    alpha (the information coefficient), over days with at least
    MIN_IC_CALLS calls. t = mean / (sd / sqrt(independent windows))."""
    return _ic_from_groups(
        ((d, [p[0] for p in pairs], [p[1] for p in pairs])
         for d, pairs in sorted(pairs_by_date.items())), h_days)


def confidence(windows: int) -> str:
    """Plain-language confidence from the count of independent periods."""
    if windows >= 8:
        return "solid"
    if windows >= MIN_WINDOWS:
        return "building"
    return "early"


# ------------------------------------------------------------- scorecard ----

def _mode_call(c: dict, mode: str) -> tuple[Optional[str], Optional[float]]:
    pair = c["v"].get(mode)
    if not pair:
        return None, None
    score = pair[1] if len(pair) > 1 else None
    return pair[0], (float(score) if isinstance(score, (int, float)) else None)


def _number(v) -> float:
    return (float(v) if isinstance(v, (int, float)) and not isinstance(v, bool)
            else math.nan)


class _Columns:
    """The evaluated calls as numpy columns, so each table is a mask rather
    than another pass over every call: a year of daily calls is ~40,000 rows,
    graded under six modes and sixteen inputs at three horizons."""

    def __init__(self, calls: list[dict], modes: tuple, horizons):
        n = len(calls)
        self.calls = calls
        self.dates = sorted({c["date"] for c in calls})
        index = {d: i for i, d in enumerate(self.dates)}
        self.day = np.fromiter((index[c["date"]] for c in calls), np.int64, n)
        self.seed = np.fromiter((c["seed"] for c in calls), bool, n)
        self.alpha = {h: np.fromiter((c["alpha"].get(h, math.nan) for c in calls),
                                     float, n) for h, _ in horizons}
        self.ret = {h: np.fromiter((c["ret"].get(h, math.nan) for c in calls),
                                   float, n) for h, _ in horizons}
        self.label: dict[str, np.ndarray] = {}
        self.score: dict[str, np.ndarray] = {}
        for m in modes:
            pairs = [_mode_call(c, m) for c in calls]
            self.label[m] = np.array([p[0] or "" for p in pairs], dtype=object)
            self.score[m] = np.fromiter(
                (math.nan if p[1] is None else p[1] for p in pairs), float, n)

    def factor(self, key: str) -> np.ndarray:
        return np.fromiter((_number(c["f"].get(key)) for c in self.calls),
                           float, len(self.calls))

    def group(self, mask: np.ndarray, h: str) -> dict:
        """n, mean return, mean alpha and the share that beat the S&P, at h."""
        a = self.alpha[h][mask]
        ok = ~np.isnan(a)
        a, r = a[ok], self.ret[h][mask][ok]
        if not a.size:
            return {"n": 0, "ret": None, "alpha": None, "beat": None}
        return {"n": int(a.size), "ret": round(float(r.mean()), 2),
                "alpha": round(float(a.mean()), 2),
                "beat": round(float((a > 0).mean()), 3)}

    def ic(self, x: np.ndarray, mask: np.ndarray, h: str, h_days: int) -> dict:
        """rank_ic of x against the alpha at h, over the masked calls."""
        y = self.alpha[h]
        idx = np.nonzero(mask & ~np.isnan(x) & ~np.isnan(y))[0]
        if not idx.size:
            return _ic_from_groups([], h_days)
        idx = idx[np.argsort(self.day[idx], kind="stable")]
        days = self.day[idx]
        cuts = np.r_[0, np.nonzero(np.diff(days))[0] + 1, idx.size]
        return _ic_from_groups(
            ((self.dates[days[a]], x[idx[a:b]], y[idx[a:b]])
             for a, b in zip(cuts[:-1], cuts[1:])), h_days)


def _bucket_mask(score: np.ndarray, name: str) -> np.ndarray:
    """Calls whose score falls in the named SCORE_BUCKETS band."""
    bounds = [lo for lo, _ in SCORE_BUCKETS]
    i = [b for _, b in SCORE_BUCKETS].index(name)
    with np.errstate(invalid="ignore"):
        mask = score >= bounds[i] if i < len(bounds) - 1 else ~np.isnan(score)
        if i > 0:
            mask &= score < bounds[i - 1]
    return mask


def _mode_block(cols: _Columns, mode: str, horizons,
                mask: Optional[np.ndarray] = None, tables: bool = True) -> dict:
    """Label, tier, bucket and ranking-skill tables for one mode, over the
    masked calls (default all). With tables=False, only the tiers and ranking
    skill (the mode comparison)."""
    label, score = cols.label[mode], cols.score[mode]
    mine = label != ""
    if mask is not None:
        mine &= mask
    labels = {}
    if tables:
        for lab in LABEL_ORDER:
            m = mine & (label == lab)
            if m.any():
                labels[lab] = {h: cols.group(m, h) for h, _ in horizons}
    tier_masks = {key: mine & np.isin(label, list(members))
                  for key, _, members in TIERS}
    tiers = {}
    for h, _ in horizons:
        row = {key: cols.group(tier_masks[key], h) for key, _, _ in TIERS}
        b, w = row["buy"]["alpha"], row["below"]["alpha"]
        row["spread"] = round(b - w, 2) if b is not None and w is not None else None
        tiers[h] = row
    buckets = {}
    if tables:
        for _, name in SCORE_BUCKETS:
            m = mine & _bucket_mask(score, name)
            if m.any():
                buckets[name] = {h: cols.group(m, h) for h, _ in horizons}
    above = mine & np.logical_or.reduce(
        [_bucket_mask(score, name) for name in ABOVE_BAR_BUCKETS])
    return {"calls": int(mine.sum()), "labels": labels, "tiers": tiers,
            "buckets": buckets,
            "above_bar": {h: cols.group(above, h) for h, _ in horizons},
            "ic": {h: cols.ic(score, mine, h, d) for h, d in horizons}}


def _buy_share(log: dict, mode: str, last_days: int = 5) -> Optional[float]:
    """Share of buy-grade calls over the log's last few logged days."""
    days = sorted(d for d, calls in (log.get("days") or {}).items()
                  if any(not c.get("s") for c in calls.values()))[-last_days:]
    labels = [c["v"][mode][0] for d in days for c in log["days"][d].values()
              if not c.get("s") and (c.get("v") or {}).get(mode)]
    if not labels:
        return None
    return round(sum(1 for lab in labels if lab in BUY_GRADE) / len(labels), 3)


def build_scorecard(log: dict, modes: tuple, default_mode: str,
                    labels: Optional[dict] = None,
                    horizons=HORIZONS) -> dict:
    """Everything the report's scorecard section shows, from the log alone.

    `modes` are the base-score mode keys; `labels` their display names (for
    the suggestions' wording). Returns {"meta", "modes", "compare",
    "factors", "suggestions"}; "suggestions" is {mode: [(tone, text)]}."""
    labels = labels or {m: m for m in modes}
    calls = evaluate_calls(log, horizons)
    days = log.get("days") or {}
    logged_days = sorted(d for d, cs in days.items()
                         if any(not c.get("s") for c in cs.values()))
    all_calls = [c for cs in days.values() for c in cs.values()]
    meta = {
        "started": logged_days[0] if logged_days else None,
        "last_day": logged_days[-1] if logged_days else None,
        "days_logged": len(logged_days),
        "calls_logged": sum(1 for c in all_calls if not c.get("s")),
        "seed_calls": sum(1 for c in all_calls if c.get("s")),
        "seed_from": min((d for d, cs in days.items()
                          if any(c.get("s") for c in cs.values())), default=None),
        "priced": len(calls),
        "matured": {h: sum(1 for c in calls if h in c["alpha"])
                    for h, _ in horizons},
        "default_mode": default_mode,
    }
    cols = _Columns(calls, modes, horizons)
    meta["windows"] = {
        h: independent_windows(
            [cols.dates[i] for i in np.unique(cols.day[~np.isnan(cols.alpha[h])])], d)
        for h, d in horizons}
    per_mode = {m: _mode_block(cols, m, horizons) for m in modes}
    for m in modes:
        per_mode[m]["buy_share"] = _buy_share(log, m)

    # Mode comparison: only calls every mode scored, and no seeded calls, so
    # each base is graded on the same stocks on the same days.
    common = ~cols.seed
    for m in modes:
        common &= ~np.isnan(cols.score[m])
    compare = {}
    for m in modes:
        block = _mode_block(cols, m, horizons, mask=common, tables=False)
        compare[m] = {"ic": block["ic"], "tiers": block["tiers"],
                      "buy_share": per_mode[m]["buy_share"]}
    compare_n = int(common.sum())

    everyone = np.ones(len(calls), dtype=bool)
    factors = {}
    for key, _, _, _ in FACTORS:
        x = cols.factor(key)
        factors[key] = {h: cols.ic(x, everyone, h, d) for h, d in horizons}

    sc = {"meta": meta, "modes": per_mode, "compare": compare,
          "compare_n": compare_n, "factors": factors,
          "found": found_scorecard(log, horizons)}
    sc["suggestions"] = {m: suggest(sc, m, modes, labels, horizons)
                         for m in modes}
    return sc


# ----------------------------------------------------------- suggestions ----

def significant(ic: dict) -> bool:
    return (ic.get("ic") is not None and ic.get("t") is not None
            and ic.get("windows", 0) >= MIN_WINDOWS
            and abs(ic["ic"]) >= IC_MATERIAL and abs(ic["t"]) >= IC_T_STAT)


def suggestion_horizon(sc: dict, horizons=HORIZONS) -> Optional[str]:
    """The horizon suggestions read: 1 month once its matured calls span
    MIN_WINDOWS non-overlapping periods, else 3 months, else 1 week —
    whichever has the data first. Each rule still checks its own sample."""
    present = {h for h, _ in horizons}
    windows = sc["meta"].get("windows") or {}
    for h in ("1M", "3M", "1W"):
        if h in present and windows.get(h, 0) >= MIN_WINDOWS:
            return h
    return None


def _pct(x: float) -> str:
    return f"{x:+.1f}%"


def suggest(sc: dict, mode: str, modes: tuple, labels: dict,
            horizons=HORIZONS) -> list[tuple[str, str]]:
    """Plain-text findings for one mode, each (tone, text) with tone one of
    good / bad / neutral. Conservative on purpose: every rule needs enough
    calls and enough independent periods, and the wording is about the
    scoring model, never about a stock."""
    h = suggestion_horizon(sc, horizons)
    name = labels.get(mode, mode)
    if h is None:
        start = sc["meta"].get("started")
        when = ""
        if start:
            first = date.fromisoformat(start) + timedelta(days=7 * MIN_WINDOWS)
            when = (f" The log started {start}; 1-week results reach that "
                    f"around {first.isoformat()}, 1-month ones about three "
                    f"months in.")
        return [("neutral",
                 "Not enough matured calls for suggestions yet. They need at "
                 f"least {MIN_WINDOWS} non-overlapping periods at a horizon."
                 + when)]
    span = HORIZON_NAMES.get(h, h)
    block = sc["modes"][mode]
    out: list[tuple[str, str]] = []

    # 1. Do buy-grade calls beat below-the-bar calls?
    t = block["tiers"][h]
    buy, below = t["buy"], t["below"]
    if (buy["n"] >= MIN_GROUP_CALLS and below["n"] >= MIN_GROUP_CALLS
            and t["spread"] is not None):
        if t["spread"] >= 1.0:
            out.append(("good",
                        f"Following the calls has paid: over {span}, buy-grade "
                        f"calls beat the S&P by {_pct(buy['alpha'])} and "
                        f"below-the-bar calls by {_pct(below['alpha'])}, a "
                        f"{t['spread']:.1f}-point edge across "
                        f"{buy['n'] + below['n']:,} calls."))
        elif t["spread"] <= -1.0:
            out.append(("bad",
                        f"Buy-grade calls trailed below-the-bar calls by "
                        f"{-t['spread']:.1f} points over {span} "
                        f"({_pct(buy['alpha'])} vs {_pct(below['alpha'])} "
                        f"against the S&P). At this horizon the verdict is not "
                        f"separating winners from losers."))
        else:
            out.append(("neutral",
                        f"Buy-grade and below-the-bar calls have done about the "
                        f"same over {span} ({_pct(buy['alpha'])} vs "
                        f"{_pct(below['alpha'])} against the S&P)."))

    # 2. Does a higher score mean a better return?
    ic = block["ic"][h]
    if significant(ic):
        if ic["ic"] > 0:
            out.append(("good",
                        f"Higher {name} scores have meant better returns: rank "
                        f"correlation {ic['ic']:+.2f} with the next {span}'s "
                        f"return against the S&P, over {ic['windows']} "
                        f"independent periods."))
        else:
            out.append(("bad",
                        f"Higher {name} scores have meant worse returns "
                        f"({ic['ic']:+.2f} over {span}, {ic['windows']} "
                        f"independent periods): the score's ordering is working "
                        f"against it at this horizon."))

    # 3. Does another base rank the same stocks better?
    scored = {m: (sc["compare"][m]["ic"].get(h) or {}) for m in modes}
    usable = {m: v for m, v in scored.items()
              if v.get("ic") is not None and v.get("windows", 0) >= MIN_WINDOWS}
    if len(usable) >= 2 and mode in usable:
        best = max(usable, key=lambda m: usable[m]["ic"])
        mine, top = usable[mode]["ic"], usable[best]["ic"]
        if best != mode and top - mine >= 0.03:
            share = sc["compare"][best].get("buy_share")
            if share is not None and share > MAX_BUY_SHARE:
                out.append(("neutral",
                            f"{labels.get(best, best)} ranks the same stocks "
                            f"better than {name} ({top:+.2f} vs {mine:+.2f} over "
                            f"{span}), but it rates {share:.0%} of names "
                            f"buy-grade. Its BUY/ADD bars need raising before it "
                            f"could be the default, or it stops discriminating."))
            else:
                verdict = ("It is the saved default."
                           if best == sc["meta"].get("default_mode")
                           else "It is a candidate for the default base.")
                out.append(("good",
                            f"{labels.get(best, best)} ranks the same stocks "
                            f"better than {name} ({top:+.2f} vs {mine:+.2f} over "
                            f"{span}, {usable[best]['windows']} independent "
                            f"periods). {verdict}"))
        elif best == mode:
            out.append(("good",
                        f"{name} ranks stocks best of the {len(usable)} bases "
                        f"graded on the same calls ({mine:+.2f} over {span})."))

    # 4. Which inputs point the right way, and which the wrong way?
    factor_notes = []
    for key, label, role, lean in FACTORS:
        if key not in WEIGHTED_FACTORS:
            continue
        f = sc["factors"][key].get(h) or {}
        if not significant(f):
            continue
        agrees = (f["ic"] > 0) == (lean > 0)
        if not agrees:
            how = ("names scoring higher on it did worse" if lean > 0 else
                   "names nearer their 52-week high did better, though the "
                   "verdict docks them")
            factor_notes.append((abs(f["t"]), (
                "bad",
                f"{label} has pointed the wrong way over {span}: {how} (rank "
                f"correlation {f['ic']:+.2f}). It is {role}; less weight, or "
                f"scoring it the other way round, would have ranked better.")))
        elif abs(f["ic"]) >= IC_STRONG:
            factor_notes.append((abs(f["t"]), (
                "good",
                f"{label} has carried real signal over {span} (rank correlation "
                f"{f['ic']:+.2f}). It is {role}; a candidate for more weight.")))
    factor_notes.sort(key=lambda x: x[0], reverse=True)
    out.extend(note for _, note in factor_notes[:3])

    # 4b. Do the reads shown beside the verdict, but not scored, carry signal?
    for key, texts in UNSCORED_FACTORS.items():
        f = (sc["factors"].get(key) or {}).get(h) or {}
        if significant(f):
            out.append(("neutral", texts[f["ic"] > 0].format(
                span=span, ic=f["ic"], windows=f["windows"])))

    # 5. Are the BUY/ADD bars in the right place?
    near = (block["buckets"].get(NEAR_BAR_BUCKET) or {}).get(h) or {}
    above = block["above_bar"].get(h) or {}
    if (near.get("n", 0) >= MIN_GROUP_CALLS and above.get("n", 0) >= MIN_GROUP_CALLS
            and near["alpha"] >= above["alpha"] + 0.5):
        out.append(("neutral",
                    f"Scores of 70–75, just under the BUY bar, beat the S&P by "
                    f"{_pct(near['alpha'])} over {span}: more than scores of 75 "
                    f"and up did ({_pct(above['alpha'])}). The bar may sit too "
                    f"high."))
    elif above.get("n", 0) >= MIN_GROUP_CALLS and above["alpha"] < -0.5:
        out.append(("bad",
                    f"Scores of 75 and up, the buy-grade range, lagged the S&P "
                    f"by {-above['alpha']:.1f}% over {span} across "
                    f"{above['n']:,} calls."))

    if not out:
        out.append(("neutral",
                    f"No clear pattern yet over {span}: nothing in the "
                    f"scorecard passes the significance bar."))
    return out


def digest(sc: dict, mode: str, labels: dict, horizons=HORIZONS) -> str:
    """A compact plain-text summary of the scorecard for the AI post-mortem's
    brief: the numbers, not the rendering."""
    meta = sc["meta"]
    logged = (f"Calls logged daily since {meta['started']} "
              f"({meta['days_logged']} days, {meta['calls_logged']} calls)"
              if meta.get("started") else "No daily calls logged yet")
    lines = [f"{logged}; plus {meta['seed_calls']} first-sighting calls "
             f"recovered from the ledger. Matured: "
             + ", ".join(f"{h} {meta['matured'][h]}" for h, _ in horizons) + "."]
    block = sc["modes"].get(mode) or {}
    for h, _ in horizons:
        t = (block.get("tiers") or {}).get(h) or {}
        ic = (block.get("ic") or {}).get(h) or {}
        parts = []
        for key, title, _ in TIERS:
            g = t.get(key) or {}
            if g.get("n"):
                parts.append(f"{title} alpha {g['alpha']:+.1f}% (n={g['n']}, "
                             f"beat S&P {g['beat']:.0%})")
        if parts or ic.get("ic") is not None:
            ic_txt = (f"rank IC {ic['ic']:+.2f} over {ic['windows']} independent "
                      f"periods" if ic.get("ic") is not None else "rank IC n/a")
            lines.append(f"{h} [{labels.get(mode, mode)}]: " + "; ".join(parts)
                         + f"; {ic_txt}.")
    h = suggestion_horizon(sc, horizons) or "1W"
    ranked = sorted(((ic, m) for m in sc["compare"]
                     if (ic := (sc["compare"][m]["ic"].get(h) or {}).get("ic"))
                     is not None), reverse=True)
    if ranked:
        lines.append(f"Bases by {h} rank IC on the same calls: " + ", ".join(
            f"{labels.get(m, m)} {ic:+.2f}" for ic, m in ranked) + ".")
    facs = []
    for key, label, _, _ in FACTORS:
        f = sc["factors"][key].get(h) or {}
        if f.get("ic") is not None:
            facs.append(f"{label} {f['ic']:+.2f}")
    if facs:
        lines.append(f"Input rank ICs at {h}: " + ", ".join(facs) + ".")
    found = sc.get("found") or {}
    parts = []
    for _, label, by_h in found.get("rows") or []:
        g = by_h.get(h) or {}
        if g.get("n"):
            parts.append(f"{label} alpha {g['alpha']:+.1f}% (n={g['n']}, "
                         f"beat S&P {g['beat']:.0%})")
    if parts:
        lines.append(f"Screen additions at {h}, from the day each was added: "
                     + "; ".join(parts) + ".")
    for tone, text in sc["suggestions"].get(mode) or []:
        lines.append(f"Rule-based finding ({tone}): {text}")
    return "\n".join(lines)
