"""
entry_signal.py
---------------
When is a good time to put new money into the US stock market? The report's
"Entry window" gauge answers with the one signal that held up in 76 years of
S&P 500 history, and shows the record behind it.

What the research found (2026-11-23; S&P 500 daily closes since 1928, VIX
since 1990, Shiller CAPE since 1871, T-bill yields since 1962; scripts in that
session's scratchpad `research/`):

  * Most of the time there is no timing edge. Across 754 monthly start dates
    (1962-2024), investing a lump sum at once beat every rule that waited:
    spreading it over 3-12 months cost 0.4-1.9% after two years on average,
    and waiting for a 5-20% dip cost 2.3-3.0% (DEPLOY_STUDY).
  * The exception is panic. When the VIX has closed at 40 or above within the
    last month, or the S&P is 30%+ below its record close, the next 12 months
    averaged about +24% (96% of days positive) against about +12% otherwise,
    and it held before and after 1990. That is the "window".
  * Milder stress is not an edge. VIX spikes of 30-40 and 10-20% corrections
    did fine before 1990 but averaged under +5% after it: they were often the
    start of a bear market (2000, 2008, 2022).
  * The window is too rare to wait for: about once every three years (median
    wait 2.8 years), the S&P was cheaper when it opened only 40% of the time,
    and waiting for it in T-bills cost 7.4% on average.
  * Valuation (CAPE) didn't time the next year, but it did shape the next ten.
  * A blended score of everything (fear, drawdown, valuation, trend, yield
    curve, credit spreads), fitted only on data before each year, had no
    forecasting power. So the gauge is a fixed, published rule, not a model.

Everything the report states about the past it recomputes here from the
history it is given, except DEPLOY_STUDY and NO_EDGE, which need data the run
doesn't fetch (T-bill yields, dividends, credit spreads) and are the study's
numbers, dated.

Pure data in, data out: no network, no HTML. The analyzer fetches the closes
and renders the result, so the tests run offline.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Optional, Sequence

import numpy as np

# History before this is the Depression and the war, when the market and its
# data were different animals.
SINCE = "1950-01-01"
TRADING_DAYS = 252
H3, H12 = 63, 252
# Before 1990 there is no VIX. Its stand-in is the S&P's own volatility over
# the last 21 sessions, annualized, times the median VIX-to-volatility ratio
# of 1990-2026.
VOL_SESSIONS = 21
VOL_PROXY_RATIO = 1.35
# A VIX close keeps counting for this many sessions (about a month). The edge
# after a spike lasted about a month and was gone by two.
RECENT_SESSIONS = 21
# The window: a VIX close at or above this within RECENT_SESSIONS, or the S&P
# at least this far below its record close.
WINDOW_VIX = 40.0
WINDOW_DRAWDOWN = 0.30
# Stress: the same with lower bars. Shown, not an edge.
STRESS_VIX = 30.0
STRESS_DRAWDOWN = 0.20
# Gauge score at the stress and window bars. The needle is the distance to
# the window, so it crosses WINDOW_SCORE exactly when the window opens.
STRESS_SCORE = 60.0
WINDOW_SCORE = 85.0
CALM_SCORE = 30.0
# Days in a state this close together belong to one episode.
EPISODE_GAP = 63
# The era split every stat is checked across.
ERA_SPLIT = "1990-01-01"

STATES = ("open", "stress", "normal")
STATE_LABELS = {
    "open": "Window open",
    "stress": "Stressed",
    "normal": "Normal",
    "calm": "Calm",
}

# Deploying a lump sum, 1962-2024: 754 monthly start dates, value after 24
# months, total return (dividends in), cash waiting in 3-month T-bills. Each
# row: (strategy, average vs all-at-once %, share of starts it beat
# all-at-once %, 5th-percentile outcome %, median outcome %).
DEPLOY_STUDY = {
    "span": "1962-2024",
    "starts": 754,
    "horizon_months": 24,
    "rows": (
        ("All at once", 0.0, None, -20.5, 25.0),
        ("Spread over 3 months", -0.4, 38.5, -20.3, 24.4),
        ("Spread over 6 months", -0.9, 36.2, -19.6, 23.9),
        ("Spread over 12 months", -1.9, 31.8, -17.4, 22.5),
        ("Wait for a 5% dip (buy after 12 months regardless)", -2.3, 55.0, -16.0, 20.3),
        ("Wait for a 10% dip (same)", -2.6, 41.1, -14.2, 18.9),
        ("Wait for a 20% dip (same)", -3.0, 30.6, -13.4, 17.5),
    ),
    # Holding cash in T-bills until the next window opened, then buying,
    # judged 12 months after it opened.
    "wait_for_window": {"avg_vs_now": -7.4, "won": 43.0},
}

# Tested and found wanting (same study; 12-month S&P returns, 1950-2026,
# against +12.8% on all days).
NO_EDGE = (
    ("A 10% correction on its own",
     "+10.3% over the next 12 months on average (25 episodes), below the "
     "+12.8% for all days."),
    ("VIX spikes of 30-40",
     "since 1990 +4.7% on average and positive 64% of the time; several were "
     "the start of a bear market (2000, 2008, 2022)."),
    ("A bounce after a spike",
     "a spike with a 10% drop that has already recovered to within 10% of "
     "the high: +11.3% since 1990, like any other day."),
    ("Trend (S&P vs its 200-day average)",
     "no consistent difference except at extremes that coincide with panics."),
    ("Yield curve (10-year minus 3-month)",
     "inverted: 4.6 points below average before 1990, 0.3 since."),
    ("Credit spreads (Baa minus 10-year)",
     "no consistent pattern."),
    ("Calendar (month, weekday, turn of month)",
     "since 2000 the turn of the month is worth about 0.15% in total and "
     "weekday effects are gone."),
    ("Time of day",
     "SPY since 2010: +0.03% a day overnight, +0.02% during the session."),
    ("A blended score of all of these",
     "fitted only on earlier data, its rank correlation with the next year's "
     "return was 0.00-0.08 and flipped sign between 1975-99 and 2000-25."),
)


# ------------------------------------------------------------ series ----

def _ordinals(dates: Sequence) -> np.ndarray:
    return np.array([date.fromisoformat(str(d)[:10]).toordinal() for d in dates],
                    dtype=np.int64)


def align(sp_dates: Sequence, sp_closes: Sequence,
          vix_dates: Sequence = (), vix_closes: Sequence = ()) -> dict:
    """The S&P's daily closes with a VIX reading for each day: the VIX close
    where there is one, else the stand-in from the S&P's own volatility.
    Dates are ISO strings (or dates); bad closes are dropped."""
    pairs = sorted({_ordinals([d])[0]: float(c) for d, c in zip(sp_dates, sp_closes)
                    if c is not None and np.isfinite(c) and c > 0}.items())
    if not pairs:
        return {"d": np.empty(0, np.int64), "c": np.empty(0), "vol": np.empty(0),
                "is_vix": np.empty(0, bool)}
    d = np.array([p[0] for p in pairs], dtype=np.int64)
    c = np.array([p[1] for p in pairs])
    lr = np.diff(np.log(c), prepend=np.nan)
    rv = np.full(c.size, np.nan)
    if c.size > VOL_SESSIONS:
        win = np.lib.stride_tricks.sliding_window_view(lr[1:], VOL_SESSIONS)
        rv[VOL_SESSIONS:] = win.std(axis=1, ddof=1) * np.sqrt(TRADING_DAYS) * 100
    vol = rv * VOL_PROXY_RATIO
    is_vix = np.zeros(c.size, bool)
    vix = {o: float(v) for o, v in zip(_ordinals(vix_dates) if len(vix_dates) else [],
                                         vix_closes)
           if v is not None and np.isfinite(v) and v > 0}
    if vix:
        first = min(vix)
        for i, o in enumerate(d):
            if o in vix:
                vol[i], is_vix[i] = vix[o], True
            elif o >= first and i:
                # A holiday on one calendar but not the other: carry the last.
                vol[i], is_vix[i] = vol[i - 1], is_vix[i - 1]
    return {"d": d, "c": c, "vol": vol, "is_vix": is_vix}


def drawdown(c: np.ndarray) -> np.ndarray:
    """Each day's close vs the record close up to that day (0 at a record,
    -0.3 when 30% below)."""
    return c / np.maximum.accumulate(c) - 1.0


def recent_peak(vol: np.ndarray, sessions: int = RECENT_SESSIONS) -> np.ndarray:
    """The highest VIX reading of the last `sessions` + 1 closes (today's
    included). NaN until the first reading."""
    v = np.where(np.isnan(vol), -np.inf, np.asarray(vol, dtype=float))
    padded = np.concatenate([np.full(sessions, -np.inf), v])
    out = np.lib.stride_tricks.sliding_window_view(padded, sessions + 1).max(axis=1)
    out[np.isinf(out)] = np.nan
    return out


def score(peak_vix, dd) -> np.ndarray:
    """The gauge: how close the market is to the window, 0-100. It reads
    STRESS_SCORE at the stress bars and WINDOW_SCORE at the window bars,
    whichever of the VIX and the drop is further along."""
    peak_vix = np.nan_to_num(np.asarray(peak_vix, dtype=float), nan=0.0)
    drop = -np.asarray(dd, dtype=float) * 100
    v = np.interp(peak_vix, [15, STRESS_VIX, WINDOW_VIX, 60],
                  [0, STRESS_SCORE, WINDOW_SCORE, 100])
    w = np.interp(drop, [0, STRESS_DRAWDOWN * 100, WINDOW_DRAWDOWN * 100, 45],
                  [0, STRESS_SCORE, WINDOW_SCORE, 100])
    return np.maximum(v, w)


def classify(s: float) -> str:
    """A score's state."""
    if s >= WINDOW_SCORE:
        return "open"
    if s >= STRESS_SCORE:
        return "stress"
    return "normal"


def rating(s: float) -> str:
    """The gauge's word: Calm splits the bottom of Normal so a quiet market
    reads differently from an uneasy one. The state stays "normal"."""
    st = classify(s)
    if st == "normal" and s < CALM_SCORE:
        return STATE_LABELS["calm"]
    return STATE_LABELS[st]


# ------------------------------------------------------------ history ----

def episodes(mask: np.ndarray, gap: int = EPISODE_GAP) -> list[int]:
    """The first day of each run of True, runs closer than `gap` sessions
    merged."""
    out, prev = [], -10 ** 9
    for i in np.flatnonzero(mask):
        if i - prev > gap:
            out.append(int(i))
        prev = i
    return out


def forward(c: np.ndarray, h: int) -> np.ndarray:
    """Each day's return over the next `h` sessions (NaN near the end)."""
    out = np.full(c.size, np.nan)
    if c.size > h:
        out[:-h] = c[h:] / c[:-h] - 1.0
    return out


def forward_low(c: np.ndarray, h: int = H12) -> np.ndarray:
    """Each day's deepest close over the next `h` sessions vs that day's
    close (0 when it never closed lower). NaN with fewer than `h` ahead."""
    out = np.full(c.size, np.nan)
    if c.size > h:
        ahead = np.lib.stride_tricks.sliding_window_view(c[1:], h).min(axis=1)
        out[:c.size - h] = np.minimum(0.0, ahead / c[:c.size - h] - 1.0)
    return out


def _pct(x) -> Optional[float]:
    return None if x is None or not np.isfinite(x) else round(float(x) * 100, 1)


def state_stats(mask: np.ndarray, f3: np.ndarray, f12: np.ndarray,
                era: np.ndarray) -> dict:
    """What the S&P did after the days in one state: averages over days (so
    long episodes weigh more), episode count, and the 12-month average
    before and after ERA_SPLIT."""
    have = mask & np.isfinite(f12)
    out = {"episodes": len(episodes(mask)), "days": int(have.sum())}
    if not have.any():
        return out
    r = f12[have]
    out.update(
        avg3=_pct(np.nanmean(f3[mask & np.isfinite(f3)])),
        avg12=_pct(r.mean()), med12=_pct(np.median(r)),
        pos12=round(float((r > 0).mean()) * 100),
        worst12=_pct(r.min()),
    )
    for key, sel in (("pre", have & ~era), ("post", have & era)):
        out[key + "12"] = _pct(f12[sel].mean()) if sel.sum() >= 20 else None
    return out


def wait_stats(c: np.ndarray, is_open: np.ndarray, start: int) -> dict:
    """From a day outside the window: sessions until the next window opened,
    and whether the S&P was cheaper then. Weekly samples from `start`, only
    days a later window exists for."""
    nxt = np.full(c.size, -1)
    j = -1
    for i in range(c.size - 1, -1, -1):
        if is_open[i]:
            j = i
        nxt[i] = j
    waits, cheaper = [], []
    for t in range(start, c.size, 5):
        if is_open[t] or nxt[t] < 0:
            continue
        waits.append(nxt[t] - t)
        cheaper.append(c[nxt[t]] < c[t])
    if not waits:
        return {}
    return {"median_years": round(float(np.median(waits)) / TRADING_DAYS, 1),
            "cheaper_pct": round(float(np.mean(cheaper)) * 100),
            "n": len(waits)}


# ---------------------------------------------------------- valuation ----

_MULTPL_ROW = re.compile(
    r"<td>\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*</td>\s*<td>\s*(?:&#x2002;|&ensp;)?"
    r"\s*(?:<abbr[^>]*>[^<]*</abbr>)?\s*([0-9]+(?:\.[0-9]+)?)\s*%?\s*</td>")


def parse_multpl_table(html: str) -> tuple[list[str], list[float]]:
    """multpl.com's monthly table (date, value), oldest first, as ISO dates."""
    from datetime import datetime
    rows = {}
    for d, v in _MULTPL_ROW.findall(html or ""):
        try:
            rows[datetime.strptime(d, "%b %d, %Y").date().isoformat()] = float(v)
        except ValueError:
            continue
    ds = sorted(rows)
    return ds, [rows[d] for d in ds]


def cape_context(cape_dates: Sequence, cape_values: Sequence,
                 sp: dict) -> Optional[dict]:
    """Where today's CAPE sits in its history, and what the S&P did over the
    next year and the next ten from the dearest tenth of months since SINCE
    vs all of them (price only, annualized for ten years)."""
    if not len(cape_dates):
        return None
    od = _ordinals(cape_dates)
    vals = np.asarray(cape_values, dtype=float)
    ok = np.isfinite(vals) & (vals > 0)
    od, vals = od[ok], vals[ok]
    if vals.size < 120:
        return None
    now = float(vals[-1])
    out = {"cape": round(now, 1), "as_of": date.fromordinal(int(od[-1])).isoformat(),
           "pct": round(float((vals < now).mean()) * 100),
           "since": date.fromordinal(int(od[0])).year}
    d, c = sp["d"], sp["c"]
    if d.size < 11 * TRADING_DAYS:
        return out
    # Monthly: each CAPE month against the first S&P close on or after it.
    since = date.fromisoformat(SINCE).toordinal()
    sel = od >= since
    idx = np.searchsorted(d, od[sel])
    keep = idx < d.size
    idx, cv = idx[keep], vals[sel][keep]
    f12 = forward(c, H12)[idx]
    f120 = forward(c, 10 * TRADING_DAYS)[idx]
    have10 = np.isfinite(f120)
    if have10.sum() < 60:
        return out
    top = float(np.quantile(cv[have10], 0.9))
    ann = lambda r: (1 + r) ** 0.1 - 1
    dear10 = have10 & (cv >= top)
    have12 = np.isfinite(f12)
    out.update(
        top_decile=round(top, 1),
        in_top=bool(now >= top),
        f10_top=_pct(np.mean(ann(f120[dear10]))),
        f10_all=_pct(np.mean(ann(f120[have10]))),
        f12_top=_pct(np.mean(f12[have12 & (cv >= top)])),
        f12_all=_pct(np.mean(f12[have12])),
    )
    return out


# ------------------------------------------------------------ assess ----

def _iso(o) -> str:
    return date.fromordinal(int(o)).isoformat()


def assess(sp_dates: Sequence, sp_closes: Sequence,
           vix_dates: Sequence = (), vix_closes: Sequence = (),
           cape_dates: Sequence = (), cape_values: Sequence = ()) -> Optional[dict]:
    """Today's reading and the record behind it. None without enough S&P
    history (a decade past SINCE) to say anything."""
    sp = align(sp_dates, sp_closes, vix_dates, vix_closes)
    d, c, vol = sp["d"], sp["c"], sp["vol"]
    start = int(np.searchsorted(d, date.fromisoformat(SINCE).toordinal()))
    if d.size - start < 10 * TRADING_DAYS or not np.isfinite(vol[-1]):
        return None
    dd = drawdown(c)
    peak = recent_peak(vol)
    s = score(peak, dd)
    is_open = s >= WINDOW_SCORE
    stress = (s >= STRESS_SCORE) & ~is_open
    in_span = np.arange(d.size) >= start
    era = d >= date.fromisoformat(ERA_SPLIT).toordinal()
    f3, f12 = forward(c, H3), forward(c, H12)
    states = {"open": is_open & in_span, "stress": stress & in_span,
              "normal": ~is_open & ~stress & in_span}
    stats = {k: state_stats(m, f3, f12, era) for k, m in states.items()}
    every = state_stats(in_span, f3, f12, era)

    now = float(s[-1])
    state = classify(now)
    i = d.size - 1
    # The spike behind today's VIX reading: the latest close at that peak.
    look = slice(max(0, i - RECENT_SESSIONS), i + 1)
    k = look.start + int(np.nanargmax(np.where(np.isnan(vol[look]), -np.inf, vol[look])))
    ath = int(np.argmax(c))
    reading = {
        "date": _iso(d[i]), "level": round(float(c[i]), 2),
        "change_1d": _pct(c[i] / c[i - 1] - 1) if i else None,
        "drawdown": _pct(dd[i]), "high": round(float(c[ath]), 2), "high_date": _iso(d[ath]),
        "vix": round(float(vol[i]), 1), "vix_is_proxy": not bool(sp["is_vix"][i]),
        "vix_peak": round(float(peak[i]), 1), "vix_peak_date": _iso(d[k]),
        "low_21": _pct(dd[look].min()),
        "trend_200": (_pct(c[i] / c[i - 199:i + 1].mean() - 1) if i >= 199 else None),
        "return_21": _pct(c[i] / c[i - 21] - 1) if i >= 21 else None,
    }
    # How far each bar is from today (positive: not there yet).
    reading["to_window_vix"] = round(WINDOW_VIX - reading["vix"], 1)
    reading["to_window_drop"] = round(WINDOW_DRAWDOWN * 100 + reading["drawdown"], 1)

    # Past windows: when each opened, what set it off, what followed.
    lows = forward_low(c)
    windows = []
    for j in episodes(states["open"]):
        trig = ("vix" if peak[j] >= WINDOW_VIX else "drop")
        windows.append({
            "date": _iso(d[j]), "trigger": trig,
            "vix_peak": round(float(peak[j]), 1), "is_proxy": not bool(sp["is_vix"][j]),
            "drawdown": _pct(dd[j]),
            "f3": _pct(f3[j]), "f12": _pct(f12[j]), "low12": _pct(lows[j]),
        })
    # The window open now (if it is): the day this run of it began.
    since = None
    if state == "open":
        j = i
        while j > 0 and is_open[j - 1]:
            j -= 1
        since = _iso(d[j])

    return {
        "score": round(now, 1), "state": state, "rating": rating(now),
        "since": since, "reading": reading,
        "stats": stats, "all": every, "windows": windows,
        "wait": wait_stats(c, is_open, start),
        "cape": cape_context(cape_dates, cape_values, sp),
        "span": {"from": _iso(d[start]), "to": _iso(d[i]),
                 "vix_from": (_iso(d[int(np.argmax(sp["is_vix"]))])
                              if sp["is_vix"].any() else None)},
        "bars": {"window_vix": WINDOW_VIX, "window_drop": WINDOW_DRAWDOWN * 100,
                 "stress_vix": STRESS_VIX, "stress_drop": STRESS_DRAWDOWN * 100,
                 "recent": RECENT_SESSIONS},
    }
