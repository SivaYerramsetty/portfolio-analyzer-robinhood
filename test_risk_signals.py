"""
test_risk_signals.py
--------------------
Tests for the two price-risk reads the report shows beside each verdict
without scoring them: the drop vs the stock's own normal, and each holding's
share of the portfolio's risk (risk_signals.py), plus the analyzer's side —
the once-a-day price-history cache, the verdict notes, the cells, the header
insights, the run log and what the scorecard logs and grades.

The policy under test: neither read moves a score or a label; a drop is
judged against the stock's own five years, and a dip under 5% gets no label;
risk shares are Euler contributions that add up to 100 and count
correlation; history is fetched once per ET day per name and never cached
from a refused fetch; and nothing is fetched once Yahoo has refused the run.

Hermetic: no network and no real cache file. Run it directly —

    ./venv/bin/python test_risk_signals.py

Exit code is 0 when everything passes, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import analyze_portfolio as ap
import risk_signals as rs
import scorecard as sc
import yahoo_limits as yl
from test_base_score_mode import _mode_env, _render_report, _scored, _tag_errors

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------- harness ----

_results: list[tuple[bool, str]] = []


def check(got, want, label: str) -> bool:
    ok = got == want
    _results.append((ok, label))
    if not ok:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
    return ok


def close(got, want, tol: float, label: str) -> bool:
    ok = got is not None and abs(got - want) <= tol
    _results.append((ok, label))
    if not ok:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r} ± {tol}")
    return ok


def section(title: str) -> None:
    print(f"\n== {title} ==")


@contextlib.contextmanager
def patched(obj, **attrs):
    """Set attributes on obj for the duration, restoring them afterwards."""
    old = {k: getattr(obj, k) for k in attrs}
    try:
        for k, v in attrs.items():
            setattr(obj, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


def sessions(n: int, end: str = "2026-10-12") -> list[str]:
    """n weekdays ending on `end`, oldest first."""
    d, out = date.fromisoformat(end), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def closes_from(returns, start: float = 100.0) -> list[float]:
    out = [start]
    for r in returns:
        out.append(out[-1] * (1 + r))
    return out


def random_walk(n: int, vol: float = 0.02, seed: int = 7) -> list[float]:
    rng = np.random.default_rng(seed)
    return closes_from(rng.normal(0.0004, vol, n - 1))


# --------------------------------------------------------- risk_signals ----

def test_drawdowns() -> None:
    section("drawdowns: distance from the trailing 52-week closing high")
    c = random_walk(600)
    dd = rs.drawdowns(c)
    brute = [c[i] / max(c[i - rs.HIGH_WINDOW + 1:i + 1]) - 1
             for i in range(rs.HIGH_WINDOW - 1, len(c))]
    check(len(dd), len(c) - rs.HIGH_WINDOW + 1, "one sample per full window")
    check(bool(np.allclose(dd, brute)), True, "matches a brute-force rolling max")
    check(bool((dd <= 0).all()), True, "never above the high")
    check(rs.drawdowns(c[:100]).size, 0, "no samples under a full window")


def test_summarize_history() -> None:
    section("summarize_history: what the cache keeps")
    c = random_walk(1500)
    d = sessions(1500)
    e = rs.summarize_history(d, c)
    check((len(e["d"]), len(e["c"])), (rs.KEEP_CLOSES, rs.KEEP_CLOSES),
          "keeps a year of closes with their dates")
    check(e["d"][-1], d[-1], "ending with the latest close")
    check(e["n"], 1500 - rs.HIGH_WINDOW + 1, "counts the drawdown samples")
    check(len(e["q"]), rs.QUANTILE_STEPS + 1, "101 quantiles")
    check(e["q"] == sorted(e["q"]), True, "quantiles ascend, deepest first")
    bad = rs.summarize_history(["2026-01-02", "2026-01-05", "2026-01-06"],
                               [10.0, float("nan"), -1.0])
    check((bad["n"], bad["c"]), (0, [10.0]),
          "non-positive and missing closes are dropped")
    check(rs.summarize_history([], []), {"n": 0, "d": [], "c": []},
          "an empty history is a valid (empty) entry")


def test_severity() -> None:
    section("severity: the share of days that were less far down")
    c = random_walk(1500, seed=11)
    dd = rs.drawdowns(c)
    q = rs.summarize_history(sessions(1500), c)["q"]
    for drop in (-0.02, -0.08, -0.15, -0.25):
        want = float((dd > drop).mean() * 100)
        close(rs.severity(drop, q), want, 1.5,
              f"{drop:.0%} matches the brute-force share ({want:.1f}%)")
    check(rs.severity(0.0, q), 0.0, "at the high: 0")
    check(rs.severity(float(min(dd)) - 0.05, q), 100.0,
          "further down than ever: 100")
    # A fifth of days at a new high: just under the high is deeper than those.
    ties = [0.0] * 20 + list(np.linspace(-0.30, -0.01, 80))
    tq = list(np.quantile(ties, np.linspace(0, 1, 101)))
    close(rs.severity(-0.005, tq), 20.0, 1.0, "ties at the high count as shallower")


def test_volatility() -> None:
    section("volatility: a year of daily returns, annualized")
    alt = closes_from([0.01, -0.01] * 126)
    want = float(np.std([0.01, -0.01] * 126, ddof=1) * np.sqrt(252) * 100)
    close(rs.volatility(alt), want, 0.05, f"alternating ±1% days: {want:.1f}%")
    close(rs.volatility(closes_from([0.001] * 252)), 0.0, 1e-9,
          "a straight line has none")
    check(rs.volatility(closes_from([0.01, -0.01] * 50)), None,
          "under half a year of returns: none")
    long = closes_from([0.05, -0.05] * 300 + [0.01, -0.01] * 126)
    close(rs.volatility(long), want, 0.05, "only the last year counts")


def test_price_stats_and_bands() -> None:
    section("price_stats and drop_band: the live drop against the history")
    c = random_walk(1500, seed=3)
    entry = rs.summarize_history(sessions(1500), c)
    high = max(c[-(rs.HIGH_WINDOW - 1):])
    s = rs.price_stats(entry, high * 0.80)
    close(s["drop"], -20.0, 0.05, "drop: 20% under the 52-week closing high")
    check(s["severity"], rs.severity(-0.20, entry["q"]), "severity from the quantiles")
    check((s["typical"], s["tail"]),
          (round(entry["q"][50] * 100, 1), round(entry["q"][10] * 100, 1)),
          "typical = median drawdown, tail = the 1-in-10 depth")
    check(s["years"], round(entry["n"] / 252, 1), "years of samples")
    new_high = rs.price_stats(entry, high * 1.01)
    check((new_high["drop"], new_high["severity"]), (0.0, 0.0),
          "a new high: no drop")
    young = rs.summarize_history(sessions(300), random_walk(300))
    ys = rs.price_stats(young, young["c"][-1] * 0.9)
    check((ys["severity"], ys["volatility"] is not None, ys["drop"] is not None),
          (None, True, True),
          "under two years of history: a drop and a volatility, no severity")
    check(rs.price_stats(None, 10.0)["drop"], None, "no history: nothing")

    check(rs.drop_band(-4.9, 99.0), None, "under 5% off the high: no label")
    check(rs.drop_band(-12.0, None), None, "no severity: no label")
    check([rs.drop_band(-12.0, s)[0] for s in (10, 74.9, 75, 89.9, 90, 97.4, 97.5, 100)],
          ["normal", "normal", "deep", "deep", "unusual", "unusual", "rare", "rare"],
          "bands at 75 / 90 / 97.5")


def _pattern_closes(pattern, scale: float, n: int = 252):
    reps = n // len(pattern)
    return closes_from([p * scale for p in pattern] * reps)


def test_risk_contributions() -> None:
    section("risk_contributions: Euler shares that add up to 100")
    d = sessions(253)
    a = _pattern_closes([1, -1, 1, -1], 0.005)     # uncorrelated with b,
    b = _pattern_closes([1, 1, -1, -1], 0.015)     # three times as volatile
    r = rs.risk_contributions({"A": (d, a), "B": (d, b)}, {"A": 100, "B": 100})
    close(r["share"]["A"], 10.0, 0.05, "uncorrelated, equal weights: shares ∝ variance (A 10%)")
    close(r["share"]["B"], 90.0, 0.05, "(B 90%)")
    close(r["vol"]["B"] / r["vol"]["A"], 3.0, 0.05, "own volatilities (B 3x A, to rounding)")
    r2 = rs.risk_contributions({"A": (d, a), "B": (d, b)}, {"A": 300, "B": 100})
    close(r2["share"]["A"], 50.0, 0.05, "weights count: 3x the dollars of A balances B")

    c2 = _pattern_closes([1, -1, 1, -1], 0.010)    # a in lockstep, twice as big
    r3 = rs.risk_contributions({"A": (d, a), "C": (d, c2)}, {"A": 100, "C": 100})
    close(r3["share"]["A"], 33.3, 0.1, "perfectly correlated: shares ∝ weight x vol")
    check((r3["corr"]["A"], r3["corr"]["C"]), (1.0, 1.0), "both move with the portfolio")
    close(sum(r3["share"].values()), 100.0, 0.05, "shares add up to 100")

    short = (d[-50:], a[-50:])
    r4 = rs.risk_contributions({"A": (d, a), "B": (d, b), "NEW": short},
                               {"A": 100, "B": 100, "NEW": 100, "NOHIST": 50})
    check(r4["missing"], ["NEW", "NOHIST"], "short or missing history is listed")
    check(sorted(r4["share"]), ["A", "B"], "and left out of the shares")
    check(rs.risk_contributions({"A": (d, a)}, {"A": 100}), None,
          "one measurable holding: nothing to compare")
    # A holding that listed mid-year still gets its pairs from the overlap.
    late = (d[-200:], b[-200:])
    r5 = rs.risk_contributions({"A": (d, a), "B": late}, {"A": 100, "B": 100})
    check(sorted(r5["share"]), ["A", "B"], "pairwise overlap: a 200-day series counts")


# ------------------------------------------------------ the price cache ----

class _FakeTicker:
    """yf.Ticker stand-in: history() returns `frame` and counts its calls;
    `on_call` runs first (to fake a refusal)."""

    def __init__(self, frame, on_call=None):
        self.frame, self.on_call, self.calls = frame, on_call, 0

    def history(self, **kw):
        self.calls += 1
        if self.on_call:
            self.on_call()
        return self.frame


def _frame(dates: list[str], closes: list[float]) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d, tz=ET) for d in dates])
    return pd.DataFrame({"Close": closes}, index=idx)


@contextlib.contextmanager
def _fresh_price_cache():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "price_history.json"
        with patched(ap, _PRICE_HISTORY_CACHE_PATH=path, _price_cache=None,
                     _price_cache_dirty=False, _price_hits=0, _price_misses=0,
                     _price_held_back=0):
            yield path


def test_price_history_cache() -> None:
    section("price history: once per ET day, never from a refused fetch")
    today = ap._et_now().date()
    d = sessions(600, end=(today - timedelta(days=1)).isoformat())
    c = random_walk(600)
    frame = _frame(d + [today.isoformat()], c + [c[-1] * 5])   # + today's quote
    with _fresh_price_cache() as path, patched(yl, refusals=lambda: 0):
        tkr = _FakeTicker(frame)
        e1 = ap._price_history_cached(tkr, "AAA")
        e2 = ap._price_history_cached(tkr, "AAA")
        check(tkr.calls, 1, "the second ask the same day is a cache hit")
        check((e1["day"], e1["d"][-1], e1["c"][-1]),
              (today.isoformat(), d[-1], round(c[-1], 4)),
              "today's bar (a quote, not a close) is left out")
        check(e2 is e1, True, "the cached entry is returned")
        ap._flush_price_cache()
        check(sorted(json.loads(path.read_text())), ["AAA"], "flushed to disk")

    with _fresh_price_cache():
        count = {"n": 0}

        def refuse():
            count["n"] += 1
        with patched(yl, refusals=lambda: count["n"]):
            tkr = _FakeTicker(frame, on_call=refuse)
            got = ap._price_history_cached(tkr, "BBB")
            check(got is not None and got["d"][-1] == d[-1], True,
                  "data from a fetch a refusal overlapped is used this run")
            check("BBB" in ap._load_price_cache(), False, "but not cached")
            again = ap._price_history_cached(tkr, "CCC")
            check((tkr.calls, again), (1, None),
                  "after a refusal nothing more is fetched")

    with _fresh_price_cache():
        old = {"day": (today - timedelta(days=1)).isoformat(), "n": 0,
               "d": ["2026-01-02"], "c": [10.0]}
        ap._load_price_cache()["OLD"] = old
        with patched(yl, refusals=lambda: 1):
            tkr = _FakeTicker(frame)
            check((ap._price_history_cached(tkr, "OLD"), tkr.calls), (old, 0),
                  "while refused, yesterday's entry stands in")
        with patched(yl, refusals=lambda: 0):
            empty = _FakeTicker(pd.DataFrame())
            got = ap._price_history_cached(empty, "NOPE")
            check((got["n"], got["c"], "NOPE" in ap._load_price_cache()),
                  (0, [], True), "a ticker with no history is cached as empty")

    with _fresh_price_cache():
        ap._load_price_cache().update({
            "STALE": {"day": (today - timedelta(days=30)).isoformat()},
            "FRESH": {"day": today.isoformat()}})
        ap._price_cache_dirty = True
        ap._flush_price_cache()
        check(sorted(ap._load_price_cache()), ["FRESH"],
              "names no run has asked about in 10 days are dropped")


def test_set_price_risk() -> None:
    section("_set_price_risk: the fields analyze_position fills")
    today = ap._et_now().date()
    d = sessions(1500, end=(today - timedelta(days=1)).isoformat())
    c = random_walk(1500, seed=5)
    with _fresh_price_cache(), patched(yl, refusals=lambda: 0):
        pa = ap.PositionAnalysis(ticker="AAA", name="A", shares=1,
                                 statement_market_value=0,
                                 statement_pct_portfolio=0)
        pa.current_price = max(c[-251:]) * 0.75
        ap._set_price_risk(pa, _FakeTicker(_frame(d, c)), "AAA")
    close(pa.drop_pct, -25.0, 0.05, "drop from the live price")
    check(pa.drop_severity is not None and pa.volatility is not None, True,
          "severity and volatility set")
    check((len(pa.price_closes[0]), len(pa.price_closes[1])),
          (rs.KEEP_CLOSES, rs.KEEP_CLOSES), "a year of closes kept for the risk shares")

    class Broken:
        def history(self, **kw):
            raise RuntimeError("boom")
    with _fresh_price_cache(), patched(yl, refusals=lambda: 0), \
            contextlib.redirect_stdout(io.StringIO()):
        pb = ap.PositionAnalysis(ticker="BBB", name="B", shares=1,
                                 statement_market_value=0,
                                 statement_pct_portfolio=0)
        ap._set_price_risk(pb, Broken(), "BBB")
    check((pb.drop_pct, pb.volatility, pb.price_closes), (None, None, None),
          "a failed fetch leaves the fields empty")


# ------------------------------------------------------- verdict notes ----

def _risky(ticker: str, composite: float, *, value: float, risk_share=None,
           drop=None, severity=None, years=5.0, **kw):
    pa = _scored(ticker, composite, [1.0] * 9, value=value, **kw)
    pa.risk_share, pa.drop_pct, pa.drop_severity = risk_share, drop, severity
    pa.drop_years, pa.drop_typical, pa.drop_tail = years, -7.0, -19.0
    return pa


def _notes(v) -> list[str]:
    return [p[2:] for p in v.reason.split(" | ") if p.startswith("· ")]


def test_verdict_notes() -> None:
    section("verdict notes: shown in the breakdown, never scored")
    with _mode_env():
        plain = _risky("AAA", 66, value=1000)
        ap.set_v2_verdicts(plain, is_holding=True, position_pct=8.0)
        risky = _risky("AAA", 66, value=1000, risk_share=18.0,
                       drop=-24.0, severity=96.0)
        ap.set_v2_verdicts(risky, is_holding=True, position_pct=8.0)
    for m in ap.BASE_SCORE_MODES:
        if not (plain.verdicts[m].score == risky.verdicts[m].score
                and plain.verdicts[m].label == risky.verdicts[m].label):
            check(m, None, "a note moved a score or a label")
            break
    else:
        check(True, True, "every mode: same score and label with the notes")
    v = risky.verdicts["composite"]
    notes = _notes(v)
    check(len(notes), 2, "two notes: the drop and the sizing")
    check(notes[0], "Down 24% from its 52-week high, unusual for this stock: "
          "further down than on 96% of trading days over the past 5 years. "
          "Shown, not scored", "the drop note")
    check(v.reason.split(" | ")[-1].startswith("= verdict score"), True,
          "notes go before the closing score line")
    check(notes[1].startswith("Sized by risk: 18.0% of the portfolio's risk vs "
                              "8.0% of its value. The size rule would give −4 "
                              "instead of 0, "), True, "the sizing note")
    check(v.risk_sized, ("HOLD", round(v.score - 4, 1)),
          "risk_sized: the verdict the size rule would give on risk")
    check("still HOLD" in notes[1], True, "same label: says it holds")

    with _mode_env():
        near = _risky("BBB", 62, value=1000, risk_share=16.0)
        ap.set_v2_verdicts(near, is_holding=True, position_pct=4.0)
    nv = near.verdicts["composite"]
    check((nv.label, nv.risk_sized[0]), ("HOLD", "HOLD"), "62 - 4 = 58 is still HOLD")
    with _mode_env():
        same = _risky("DDD", 70, value=1000, risk_share=12.0, drop=-3.0,
                      severity=99.0)
        ap.set_v2_verdicts(same, is_holding=True, position_pct=11.0)
        watch = _risky("EEE", 70, value=0, shares=0, risk_share=None,
                       drop=-12.0, severity=40.0)
        ap.set_v2_verdicts(watch, is_holding=False)
    check((_notes(same.verdicts["composite"]), same.verdicts["composite"].risk_sized),
          ([], None), "same size band, a 3% dip: no notes")
    check(_notes(watch.verdicts["composite"]),
          ["Down 12% from its 52-week high, normal for this stock, which was "
           "this far down or further on 60% of trading days over the past "
           "5 years. Shown, not scored"],
          "watchlist names get the drop note (a normal pullback says so)")
    with _mode_env():
        rare = _risky("FFF", 70, value=0, shares=0, drop=-40.0, severity=100.0)
        ap.set_v2_verdicts(rare, is_holding=False)
        nearly = _risky("GGG", 70, value=0, shares=0, drop=-30.0, severity=99.7)
        ap.set_v2_verdicts(nearly, is_holding=False)
    check("further down than at any point over the past 5 years"
          in _notes(rare.verdicts["composite"])[0], True,
          "deeper than ever: says so")
    check("more than 99% of trading days" in _notes(nearly.verdicts["composite"])[0],
          True, "99.7% isn't rounded up to every day")


def test_risk_sized_flip() -> None:
    section("sizing by risk can change a label")
    with _mode_env():
        pa = _risky("HHH", 64, value=1000, risk_share=26.0)
        ap.set_v2_verdicts(pa, is_holding=True, position_pct=5.0)
    v = pa.verdicts["composite"]
    check((v.label, v.score), ("HOLD", 64.0), "on value: no size penalty")
    check(v.risk_sized[0], "HOLD", "on risk: 64 - 8 = 56, a weak HOLD")
    with _mode_env():
        pb = _risky("III", 56, value=1000, risk_share=26.0)
        ap.set_v2_verdicts(pb, is_holding=True, position_pct=5.0)
    vb = pb.verdicts["composite"]
    check((vb.label, vb.risk_sized), ("HOLD", ("TRIM", 48.0)),
          "56 on value is HOLD; 56 - 8 = 48 on risk is TRIM")
    check(any(n.endswith("making it TRIM 48. Shown, not scored") for n in _notes(vb)),
          True, "the note names the label it would become")
    with _mode_env():
        pc = _risky("JJJ", 84, value=1000, risk_share=16.0)
        ap.set_v2_verdicts(pc, is_holding=True, position_pct=5.0)
    vc = pc.verdicts["composite"]
    check((vc.label, vc.risk_sized[0]), ("ADD", "HOLD"),
          "84 - 4 = 80 clears the ADD bar, but overweight on risk hits the ceiling")


# ----------------------------------------------------- portfolio shares ----

def test_portfolio_risk() -> None:
    section("portfolio_risk: shares on the holdings")
    d = sessions(253)
    a = _pattern_closes([1, -1, 1, -1], 0.005)
    b = _pattern_closes([1, 1, -1, -1], 0.015)
    with _mode_env():
        h1 = _risky("AAA", 66, value=500)
        h2 = _risky("BBB", 66, value=500)
        h3 = _risky("BBB", 66, value=0.0)          # the same ticker, no value
        h4 = _risky("ETF", 66, value=700)          # no history
    h1.price_closes, h2.price_closes = (d, a), (d, b)
    risk = ap.portfolio_risk([h1, h2, h3, h4], set_shares=True)
    close(h1.risk_share, 10.0, 0.05, "AAA gets its share")
    close(h2.risk_share, 90.0, 0.05, "BBB too")
    check((h4.risk_share, risk["missing"]), (None, ["ETF"]),
          "a holding without history: no share, listed as missing")
    with _mode_env():
        h5 = _risky("AAA", 66, value=1000)
    h5.price_closes = (d, a)
    ap.portfolio_risk([h1, h2, h5], set_shares=True)
    close(h1.risk_share, 50.0, 0.05,
          "one ticker in two accounts counts once: 500 + 1000 is 3x BBB")


# ------------------------------------------------------------ rendering ----

def test_cells_and_insights() -> None:
    section("cells, header insights and the run log")
    with _mode_env():
        held = _risky("AAA", 56, value=1000, risk_share=26.0, drop=-24.0,
                      severity=96.0)
        ap.set_v2_verdicts(held, is_holding=True, position_pct=5.0)
        held.live_pct_portfolio, held.volatility, held.risk_corr = 5.0, 54.0, 0.71
        calm = _risky("BBB", 70, value=1000, risk_share=3.0, drop=-12.0,
                      severity=40.0)
        ap.set_v2_verdicts(calm, is_holding=True, position_pct=12.0)
        calm.live_pct_portfolio = 12.0
        dip = _risky("CCC", 70, value=1000, drop=-3.0, severity=99.0)

    rng = ap._range_trend_cell(held)
    check("−24% off high · unusual for it" in rng, True, "the drop line")
    check("var(--pos-down)" in rng, True, "an unusual drop is red")
    check(re.search(r"title='[^']*'", rng) is not None
          and "It typically sits 7% below its 52-week high; 1 day in 10, 19% "
              "or more." in rng, True, "the hover adds the typical and tail depth")
    check("off high" in ap._range_trend_cell(dip), False, "a 3% dip: no line")
    check("· normal for it" in ap._range_trend_cell(calm)
          and "var(--fg-muted)" in ap._range_trend_cell(calm), True,
          "a normal pullback says so, quietly")

    pos = ap._position_cell(held)
    check(">26% of risk</span>" in pos, True, "Position cell: share of risk")
    title = re.search(r"title='([^']*)'>26% of risk", pos)
    check(title is not None and "the portfolio&#x27;s value" in title.group(1), True,
          "apostrophes are escaped, so the title isn't cut short")
    check("var(--fg-chip-amber)" in pos, True, "amber when the size rule would bite harder")
    check("It swings about 54% a year, with a 0.71 correlation to the portfolio."
          in pos.replace("&#x27;", "'"), True, "volatility and correlation on hover")
    check("var(--fg-chip-amber)" in ap._position_cell(calm), False,
          "a lighter share of risk stays muted")
    check("of risk" in ap._position_cell(dip), False, "unmeasured: nothing added")

    cell = ap._verdict_cell(held.verdicts["composite"])
    check(cell.count("class='vnote'"), 2, "the hover card lists both notes")
    check("<div class='vnotes'>" in cell, True, "in their own block")
    held_note = ap.Verdict(label="HOLD", color="#000", score=60.0,
                           reason="h | Composite Score 60 | · Reports in 3 days — "
                                  "event risk noted, not scored (holding) | "
                                  "· HOLD held — within 3 points | = verdict score 60")
    c2 = ap._verdict_cell(held_note, days_to_earnings=3)
    check(("<div class='vnote'>· HOLD held" in c2,
           "<div class='vnote'>· Reports" in c2, "vearn-row" in c2),
          (True, False, True),
          "other notes show too; the report date only in its footer")

    with _mode_env():
        insights = ap._portfolio_insights([held, calm, dip])
    by_label = {i["label"]: i["detail"] for i in insights}
    check(by_label.get("Risk concentration"),
          "AAA (5% of value, 26% of risk) — a bigger share of the portfolio's "
          "swings than of its value.", "insight: risk concentration")
    check(by_label.get("Unusual drops"),
          "AAA (−24%) — further below their 52-week high than they usually fall.",
          "insight: unusual drops")

    with _mode_env():
        lines = ap.describe_price_risk([held, calm], {"W": [dip]})
    check(any("Sizing by risk instead of value (Composite): AAA HOLD 56 → "
              "TRIM 48; BBB HOLD 68 → HOLD 70 (1 label change)." in ln
              for ln in lines), True,
          "run log: every holding that changes size band, and the label changes")
    check(any(ln.startswith("[risk] Unusual drops: AAA −24% (unusual: deeper than "
                            "on 96% of days)") for ln in lines), True,
          "run log: unusual drops")


def test_full_report() -> None:
    section("generate_html_report with the risk reads")
    d = sessions(253)
    with _mode_env():
        holdings = [_risky("AAA", 56, value=1000, drop=-24.0, severity=96.0),
                    _risky("BBB", 70, value=3000, drop=-12.0, severity=40.0),
                    _risky("CCC", 80, value=1000)]
        holdings[0].price_closes = (d, _pattern_closes([1, 1, -1, -1], 0.03))
        holdings[1].price_closes = (d, _pattern_closes([1, -1, 1, -1], 0.004))
        holdings[2].price_closes = (d, _pattern_closes([1, -1, -1, 1], 0.01))
        watch = {"Tech": [_risky("WWW", 72, value=0, shares=0, drop=-31.0,
                                 severity=99.0)]}
        html = _render_report(holdings, watch)
    check(_tag_errors(html), [], "tags balance")
    check("% of risk</span>" in html and "off high · rare for it" in html, True,
          "risk share and drop lines render")
    check(holdings[0].risk_share is not None, True,
          "the report finalizes the risk shares itself")
    scripts = [js for js in re.findall(r"<script[^>]*>(.*?)</script>", html, flags=re.S)
               if js.strip()]
    with tempfile.TemporaryDirectory() as tmp:
        bad = []
        for i, js in enumerate(scripts):
            p = Path(tmp) / f"s{i}.js"
            p.write_text(js)
            res = subprocess.run(["node", "--check", str(p)], capture_output=True)
            if res.returncode:
                bad.append(res.stderr.decode()[:200])
    check(bad, [], "every inline script parses")


# ------------------------------------------------------------ scorecard ----

def test_scorecard_logging() -> None:
    section("scorecard: the reads are logged and graded")
    with _mode_env():
        pa = _risky("AAA", 66, value=1000, risk_share=18.0, drop=-24.0,
                    severity=96.0)
        pa.volatility = 41.2
    f = ap._rec_factors(pa)
    check((f["volatility"], f["drop_pct"], f["drop_severity"], f["risk_share"]),
          (41.2, -24.0, 96.0, 18.0), "the ledger snapshot carries them")
    check({k: v for k, v in sc.compact_factors(f).items() if k in ("vol", "ddp")},
          {"vol": 41.2, "ddp": 96.0}, "the verdict log keeps volatility and the drop read")
    keys = [k for k, *_ in sc.FACTORS]
    check(("vol" in keys, "ddp" in keys), (True, True), "both are graded as inputs")


def _synthetic_log(n_days: int = 140, n_tickers: int = 20, *, sign: int) -> dict:
    """A log where the drop read predicts each ticker's next moves (sign +1:
    deeper drops beat the S&P; -1: they lag)."""
    days = sessions(n_days, end="2026-06-30")
    log = sc.empty_log()
    rng = np.random.default_rng(1)
    tickers = [f"T{i:02d}" for i in range(n_tickers)]
    closes = {t: {} for t in tickers}
    closes[sc.BENCHMARK] = {}
    level = {t: 100.0 for t in tickers}
    bench = 100.0
    sev = {t: float(rng.uniform(0, 100)) for t in tickers}
    all_days = sessions(n_days + 80, end="2026-09-30")
    for d in all_days:
        bench *= 1.0003
        closes[sc.BENCHMARK][d] = round(bench, 4)
        for t in tickers:
            drift = 0.0003 + sign * (sev[t] - 50) / 50 * 0.002
            level[t] *= 1 + drift + float(rng.normal(0, 0.002))
            closes[t][d] = round(level[t], 4)
    for d in days:
        calls = {}
        for t in tickers:
            calls[t] = {"p": closes[t][d], "h": 0,
                        "v": {"composite": ["WATCH", 65.0]},
                        "f": {"ddp": round(sev[t], 1), "vol": 30.0 + sev[t] / 10}}
        sc.record_day(log, d, calls)
    log["closes"] = closes
    return log


def test_scorecard_findings() -> None:
    section("scorecard: a significant unscored read becomes a finding")
    for sign, phrase in ((-1, "kept lagging the S&P"), (1, "went on to beat the S&P")):
        log = _synthetic_log(sign=sign)
        card = sc.build_scorecard(log, ("composite",), "composite")
        ic = card["factors"]["ddp"]["1M"]
        check(sc.significant(ic) and (ic["ic"] > 0) == (sign > 0), True,
              f"drop read IC sign follows the data ({ic['ic']:+.2f})")
        texts = [t for _, t in card["suggestions"]["composite"]]
        check(any(phrase in t for t in texts), True,
              f"finding when deeper drops {'lag' if sign < 0 else 'beat'}")
    html = ap._sc_factor_table(card)
    check("Drop vs its normal" in html and "shown, not scored" in html, True,
          "the inputs table lists it")
    check("(lower is treated as better)" in html.split("Drop vs its normal")[1]
          .split("</tr>")[0], False, "an unscored read isn't said to lean either way")


# ------------------------------------------------------------------ main ----

def main() -> int:
    tests = [test_drawdowns, test_summarize_history, test_severity,
             test_volatility, test_price_stats_and_bands, test_risk_contributions,
             test_price_history_cache, test_set_price_risk, test_verdict_notes,
             test_risk_sized_flip, test_portfolio_risk, test_cells_and_insights,
             test_full_report, test_scorecard_logging, test_scorecard_findings]
    for t in tests:
        before = len(_results)
        try:
            t()
        except Exception as e:  # a crash is a failure, not an abort
            import traceback
            traceback.print_exc()
            _results.append((False, f"{t.__name__} raised {type(e).__name__}: {e}"))
        mine = _results[before:]
        print(f"  {sum(ok for ok, _ in mine)}/{len(mine)} passed")
    passed = sum(ok for ok, _ in _results)
    print(f"\n{passed}/{len(_results)} checks passed")
    return 0 if passed == len(_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
