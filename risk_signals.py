"""
risk_signals.py
---------------
Two price-risk reads the report shows next to each verdict, without scoring
them yet:

  * Drop vs normal. How far a stock sits below its 52-week closing high,
    judged against its own history: the share of trading days over the past
    five years it was less far down than it is now. A 20% pullback is routine
    for a stock that swings 55% a year and rare for one that swings 15%, so
    the raw drop alone can't say whether something has changed.

  * Share of portfolio risk. Each holding's part in the portfolio's day-to-day
    swings (its Euler contribution to portfolio volatility, from a year of
    daily returns, counting how it moves with the other holdings). A 10%
    position in a stock that swings 60% a year carries about three times the
    risk of a 10% position in one that swings 20%; the position-size rule
    only sees the 10%.

Both are logged with every call, so the Report Scorecard can grade the drop
read and volatility against what the stocks did next before either is
allowed to move a verdict.

Pure data in, data out: no PositionAnalysis, no HTML and no yfinance. The
analyzer fetches the closes and renders the result, so the tests run offline.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import numpy as np

TRADING_DAYS = 252
# The drop is measured from the highest close over this many sessions.
HIGH_WINDOW = TRADING_DAYS
# Years of daily closes behind "normal". The first year only builds the first
# 52-week window, so this gives five years of drawdown samples.
HISTORY_YEARS = 6
# Fewer drawdown samples than this (about a year of them) is too little
# history to say what is normal for the stock.
MIN_DRAWDOWN_SAMPLES = TRADING_DAYS
# Daily returns behind a volatility or a covariance: a year at most, half a
# year at least.
VOL_WINDOW = TRADING_DAYS
MIN_RETURNS = TRADING_DAYS // 2
# Stored drawdown quantiles: 0%, 1%, … 100%.
QUANTILE_STEPS = 100
# Closes kept per ticker for the volatility and the covariance (a year of
# returns needs one more close than returns).
KEEP_CLOSES = VOL_WINDOW + 1

# A drop smaller than this (in %) gets no label: every stock spends much of
# its time a few percent off its high.
DROP_MIN_PCT = 5.0
# Drop bands by severity, the share of days the stock was less far down:
# (lowest severity, key, label), most severe first.
DROP_BANDS = (
    (97.5, "rare", "rare"),
    (90.0, "unusual", "unusual"),
    (75.0, "deep", "deep"),
    (0.0, "normal", "normal"),
)


# -------------------------------------------------------------- history ----

def history_start(today: date) -> str:
    """The first date to fetch closes from."""
    return (today - timedelta(days=int(365.25 * HISTORY_YEARS) + 7)).isoformat()


def drawdowns(closes) -> np.ndarray:
    """Each day's drawdown from the highest close of the HIGH_WINDOW sessions
    ending that day, as a fraction (0 at a new high, -0.2 for 20% below).
    Starts once a full window exists; empty for a shorter series."""
    c = np.asarray(closes, dtype=float)
    if c.size < HIGH_WINDOW:
        return np.empty(0)
    highs = np.lib.stride_tricks.sliding_window_view(c, HIGH_WINDOW).max(axis=1)
    return c[HIGH_WINDOW - 1:] / highs - 1.0


def summarize_history(dates: list[str], closes: list[float]) -> dict:
    """The cache entry for one ticker's daily closes (oldest first, ending
    before today): the last KEEP_CLOSES closes with their dates, for the
    volatility, the covariance and the live drop; the drawdown history's
    quantiles; and how many drawdown samples they summarize."""
    pairs = [(d, float(c)) for d, c in zip(dates, closes)
             if c is not None and np.isfinite(c) and c > 0]
    if not pairs:
        return {"n": 0, "d": [], "c": []}
    ds, cs = zip(*pairs)
    dd = drawdowns(cs)
    entry = {"n": int(dd.size), "d": list(ds[-KEEP_CLOSES:]),
             "c": [round(c, 4) for c in cs[-KEEP_CLOSES:]]}
    if dd.size:
        q = np.quantile(dd, np.linspace(0.0, 1.0, QUANTILE_STEPS + 1))
        entry["q"] = [round(float(x), 5) for x in q]
    return entry


# ---------------------------------------------------------------- stats ----

def daily_returns(closes) -> np.ndarray:
    """Simple daily returns over the last VOL_WINDOW sessions."""
    c = np.asarray(closes, dtype=float)[-(VOL_WINDOW + 1):]
    if c.size < 2:
        return np.empty(0)
    return c[1:] / c[:-1] - 1.0


def volatility(closes) -> Optional[float]:
    """Annualized volatility of daily returns over the last year, in %. None
    with under half a year of returns."""
    r = daily_returns(closes)
    if r.size < MIN_RETURNS:
        return None
    return round(float(r.std(ddof=1)) * np.sqrt(TRADING_DAYS) * 100, 1)


def severity(drop: float, quantiles) -> float:
    """The share of the history's days (0-100) that were less far down than
    `drop` (a fraction, <= 0), from the stored quantiles. A stock at its high
    scores 0; one further down than it has ever been in the window, 100."""
    q = np.asarray(quantiles, dtype=float)
    steps = q.size - 1
    k = int(np.searchsorted(q, drop, side="right"))   # quantile points <= drop
    if k == 0:
        below = 0.0
    elif k > steps:
        below = 1.0
    else:
        lo, hi = q[k - 1], q[k]
        below = ((k - 1) + ((drop - lo) / (hi - lo) if hi > lo else 0.0)) / steps
    return round((1.0 - below) * 100, 1)


def drop_band(drop_pct: Optional[float],
              severity_pct: Optional[float]) -> Optional[tuple[str, str]]:
    """(key, label) for a drop worth labeling, or None: under DROP_MIN_PCT, or
    too little history to say what is normal."""
    if drop_pct is None or severity_pct is None or drop_pct > -DROP_MIN_PCT:
        return None
    for floor, key, label in DROP_BANDS:
        if severity_pct >= floor:
            return key, label
    return None


def price_stats(entry: Optional[dict], live_price: Optional[float]) -> dict:
    """Volatility and the drop read for one ticker, from its cache entry and
    the price now. Keys (any may be None):

      volatility  annualized, in %
      drop        % below the 52-week closing high (<= 0), counting today
      severity    share of days over the history that were less far down
      typical     the median drawdown over the history, in %
      tail        the drawdown only 1 day in 10 was worse than, in %
      years       years of drawdown history behind severity
    """
    out = {"volatility": None, "drop": None, "severity": None,
           "typical": None, "tail": None, "years": None}
    closes = (entry or {}).get("c") or []
    if not closes:
        return out
    out["volatility"] = volatility(closes)
    if live_price is None or live_price <= 0:
        return out
    high = max(max(closes[-(HIGH_WINDOW - 1):]), live_price)
    drop = live_price / high - 1.0
    out["drop"] = round(drop * 100, 1)
    q = entry.get("q")
    if q and (entry.get("n") or 0) >= MIN_DRAWDOWN_SAMPLES:
        out["severity"] = severity(drop, q)
        out["typical"] = round(q[QUANTILE_STEPS // 2] * 100, 1)
        out["tail"] = round(q[QUANTILE_STEPS // 10] * 100, 1)
        out["years"] = round(entry["n"] / TRADING_DAYS, 1)
    return out


# ------------------------------------------------------- portfolio risk ----

def risk_contributions(series: dict[str, tuple[list[str], list[float]]],
                       values: dict[str, float]) -> Optional[dict]:
    """Each holding's share of the portfolio's volatility (%), its own
    volatility (%) and its correlation with the portfolio, from a year of
    daily returns. `series` is {ticker: (dates, closes)}; `values` is
    {ticker: market value}.

    The shares are Euler contributions, w_i (Σw)_i / w'Σw: they add up to
    100, a holding that moves with the rest counts for more than its own
    volatility suggests, and one that offsets the rest can come out negative.
    Covariances are pairwise over the days both names traded, so one recent
    listing doesn't shorten everyone's window. Holdings with under half a year
    of returns are left out and listed under "missing".

    Returns {"share", "vol", "corr", "portfolio_vol", "missing"}, or None
    when fewer than two holdings can be measured."""
    held = [t for t, v in values.items() if v and v > 0]
    returns: dict[str, dict[str, float]] = {}
    missing = []
    for t in held:
        dates, closes = series.get(t) or ([], [])
        r = daily_returns(closes)
        if r.size < MIN_RETURNS:
            missing.append(t)
            continue
        returns[t] = dict(zip(list(dates)[-r.size:], r.tolist()))
    tickers = sorted(returns)
    if len(tickers) < 2:
        return None
    days = sorted({d for by_day in returns.values() for d in by_day})
    row = {d: i for i, d in enumerate(days)}
    R = np.full((len(days), len(tickers)), np.nan)
    for j, t in enumerate(tickers):
        for d, x in returns[t].items():
            R[row[d], j] = x
    ok = ~np.isnan(R)
    n = len(tickers)
    cov = np.zeros((n, n))
    for i in range(n):
        for j in range(i, n):
            both = ok[:, i] & ok[:, j]
            if both.sum() < MIN_RETURNS:
                continue        # too little overlap: treated as uncorrelated
            cov[i, j] = cov[j, i] = np.cov(R[both, i], R[both, j])[0, 1]
    cov *= TRADING_DAYS
    w = np.array([values[t] for t in tickers], dtype=float)
    w /= w.sum()
    marginal = cov @ w
    variance = float(w @ marginal)
    if variance <= 0:
        return None
    vol_p = np.sqrt(variance)
    vols = np.sqrt(np.clip(np.diag(cov), 0.0, None))
    share = w * marginal / variance * 100
    corr = np.divide(marginal, vols * vol_p, out=np.full(n, np.nan),
                     where=vols > 0)
    return {
        "share": {t: round(float(s), 1) for t, s in zip(tickers, share)},
        "vol": {t: round(float(v) * 100, 1) for t, v in zip(tickers, vols)},
        "corr": {t: (None if np.isnan(c) else round(float(c), 2))
                 for t, c in zip(tickers, corr)},
        "portfolio_vol": round(float(vol_p) * 100, 1),
        "missing": sorted(missing),
    }
