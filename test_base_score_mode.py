"""
test_base_score_mode.py
-----------------------
Tests for the switchable verdict base score (BASE_SCORE_MODES in
analyze_portfolio.py).

The policy under test: compute_verdict_v2 starts every verdict from a 0-100
base, then applies the same context modifiers. The base is one of
  composite — the Composite Score (the original behavior, still the default),
  quality   — the nine quality filters as a soft-gated score (70%) plus the
              analyst and insider sub-scores (15% each),
  blend     — the average of those two bases.
Near-miss filters keep partial credit, filters with no data are left out
(lowering coverage) rather than failed, and a loss-maker's missing earnings
metrics still count as fails. The mode comes from --base-score, else the
BASE_SCORE_MODE env var, and a rank baseline set under another mode never
produces a ▲/▼ badge.

Hermetic: no network, no API key, no real ledger or cache file. Run it
directly —

    ./venv/bin/python test_base_score_mode.py

Exit code is 0 when everything passes, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import io
from html.parser import HTMLParser
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import analyze_portfolio as ap

# ---------------------------------------------------------------- harness ----

_results: list[tuple[bool, str]] = []


def check(got, want, label: str) -> bool:
    ok = got == want
    _results.append((ok, label))
    if not ok:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
    return ok


def section(title: str) -> None:
    print(f"\n== {title} ==")


@contextlib.contextmanager
def _mode_env(saved: str | None = None, override: str | None = None):
    """Run with BASE_SCORE_MODE=`saved` (unset when None) and the --base-score
    override set to `override`, restoring both afterwards."""
    old_env = os.environ.get("BASE_SCORE_MODE")
    try:
        if saved is None:
            os.environ.pop("BASE_SCORE_MODE", None)
        else:
            os.environ["BASE_SCORE_MODE"] = saved
        ap.set_base_score_mode(override)
        yield
    finally:
        ap.set_base_score_mode(None)
        if old_env is None:
            os.environ.pop("BASE_SCORE_MODE", None)
        else:
            os.environ["BASE_SCORE_MODE"] = old_env


def _credits(info: dict) -> dict:
    """{filter name: (passed, credit rounded)} for a synthetic yfinance info."""
    return {f.name: (f.passed, None if f.credit is None else round(f.credit, 6))
            for f in ap.apply_quality_filters(dict(info))}


def _filters(credits: list, passed: int | None = None) -> list:
    """FilterResults carrying the given credits; the first `passed` pass."""
    if passed is None:
        passed = sum(1 for c in credits if c == 1.0)
    return [ap.FilterResult(name=f"f{i}", passed=i < passed, actual=1.0,
                            threshold="x", credit=c)
            for i, c in enumerate(credits)]


def _position(**kw) -> ap.PositionAnalysis:
    pa = ap.PositionAnalysis(ticker=kw.pop("ticker", "TEST"), name="Test Co",
                             shares=1, statement_market_value=0,
                             statement_pct_portfolio=0)
    for k, v in kw.items():
        setattr(pa, k, v)
    return pa


PASSING = {
    "revenueCAGR3y": 0.20, "earningsCAGR3y": 0.25, "trailingPE": 25,
    "forwardPE": 20, "trailingPegRatio": 1.2, "roeAvg3y": 0.25,
    "operatingMarginAvg3y": 0.30, "debtToEquity": 40, "freeCashflow": 5e9,
    "fcfConsistency": {"positive_years": 3, "total_years": 3, "growing": True},
    "quickRatio": 1.5, "trailingEps": 5.0,
}


# ------------------------------------------------------ 1. soft-gate credit ----

def test_soft_credits() -> None:
    section("apply_quality_filters: near misses keep partial credit")
    got = _credits(PASSING)
    check(all(passed for passed, _ in got.values()), True,
          "the passing profile passes all nine")
    check({c for _, c in got.values()}, {1.0}, "and every pass earns full credit")

    near = dict(PASSING, revenueCAGR3y=0.09, earningsCAGR3y=0.085,
                trailingPE=33, forwardPE=34, trailingPegRatio=2.2,
                roeAvg3y=0.135, operatingMarginAvg3y=0.10, debtToEquity=110,
                fcfConsistency={"positive_years": 3, "total_years": 3,
                                "growing": False},
                quickRatio=0.9)
    got = _credits(near)
    check(any(passed for passed, _ in got.values()), False,
          "pass/fail is untouched: every near miss still fails")
    check(got["Revenue growth >=10%"][1], 0.5, "9% growth is halfway to the 8% floor")
    check(got["EPS growth >=10%"][1], 0.25, "8.5% EPS growth keeps a quarter")
    check(got["P/E < 30"][1], 0.5, "P/E 33 is halfway to the 36 ceiling")
    check(got["PEG < 2"][1], 0.5, "PEG 2.2 is halfway to 2.4")
    check(got["ROE >= 15%"][1], 0.5, "13.5% ROE is halfway to 12%")
    check(got["Op margin >= 15%"][1], 0.0, "a 10% margin is past the band: no credit")
    check(got["Debt/Equity < 1"][1], 0.5, "D/E 1.1 is halfway to 1.2")
    check(got["FCF positive & growing"][1], 0.5,
          "positive every year but not growing earns half")
    check(got["Quick ratio > 1.0"][1], 0.5, "a 0.9 quick ratio is halfway to 0.8")

    # The P/E band is what stops an intraday drift across 30 from swinging
    # the base a whole filter's worth.
    a = _credits(dict(PASSING, trailingPE=29.9, forwardPE=35))["P/E < 30"]
    b = _credits(dict(PASSING, trailingPE=30.1, forwardPE=35))["P/E < 30"]
    check((a[0], b[0]), (True, False), "P/E 29.9 passes and 30.1 fails")
    check(round(a[1] - b[1], 3), 0.017, "but that filter's credit only drops 1.7%")


def test_missing_versus_loss() -> None:
    section("apply_quality_filters: no data is left out, a loss is a fail")
    thin = {k: v for k, v in PASSING.items()
            if k not in ("trailingPegRatio", "debtToEquity", "quickRatio")}
    got = _credits(thin)
    check(got["PEG < 2"], (False, None), "a profitable name with no PEG is unrated")
    check(got["Debt/Equity < 1"], (False, None), "no D/E is unrated")
    check(got["Quick ratio > 1.0"], (False, None), "no quick ratio is unrated")

    no_pe = {k: v for k, v in PASSING.items() if k not in ("trailingPE", "forwardPE")}
    check(_credits(no_pe)["P/E < 30"], (False, None),
          "a profitable name with no P/E at all is unrated")

    loser = {"revenueGrowth": -0.5, "trailingEps": -2.0, "forwardPE": -29.1,
             "returnOnEquity": -0.33, "operatingMargins": -1.15,
             "debtToEquity": 20, "freeCashflow": 3e8, "quickRatio": 2.1,
             "fcfConsistency": {"positive_years": 0, "total_years": 3,
                                "growing": False}}
    got = _credits(loser)
    check(got["EPS growth >=10%"], (False, 0.0), "a loss-maker's missing EPS growth fails")
    check(got["P/E < 30"], (False, 0.0), "a negative P/E fails")
    check(got["PEG < 2"], (False, 0.0), "a loss-maker's missing PEG fails")
    check(got["ROE >= 15%"], (False, 0.0), "negative ROE earns nothing")
    check(got["FCF positive & growing"], (False, 0.0), "no positive FCF year earns nothing")

    check(_credits(dict(PASSING, trailingPegRatio=-1.0))["PEG < 2"], (False, 0.0),
          "a negative PEG fails outright")
    one_year = {"freeCashflow": 2e9, "_fcfGrowing": False}
    check(_credits(one_year)["FCF positive & growing"], (False, 0.5),
          "1-year fallback: positive but shrinking earns half")
    check(_credits({"freeCashflow": -1e9})["FCF positive & growing"], (False, 0.0),
          "1-year fallback: negative FCF earns nothing")
    check(_credits({})["FCF positive & growing"], (False, None),
          "no FCF data is unrated")


def test_filter_score() -> None:
    section("compute_filter_score: mean credit over the rated filters")
    check(ap.compute_filter_score(_filters([1.0, 0.5, None, 0.0])), (50.0, 0.75),
          "unrated filters drop out of the mean and show as lower coverage")
    check(ap.compute_filter_score(_filters([None, None])), (None, 0.0),
          "nothing rated means no score")
    check(ap.compute_filter_score([]), (None, 0.0), "no filters means no score")


# --------------------------------------------------------- 2. quality base ----

def test_quality_base() -> None:
    section("compute_quality_base: filters 70%, analyst 15%, insider 15%")
    credits = [1.0, 1.0, 1.0, 0.5, 0.5, None, None, None, None]   # 80 over 5/9

    pa = _position(filters=_filters(credits), score_analyst=60.0, score_insider=40.0)
    ap.compute_quality_base(pa)
    check((pa.filter_score, pa.filter_coverage), (80.0, 0.556),
          "the filter score and its data share")
    check(pa.quality_base, 71.0, "0.70*80 + 0.15*60 + 0.15*40")
    check(pa.quality_coverage, 0.689, "coverage scales the filters' 70% by 5/9")

    pa = _position(filters=_filters(credits), score_analyst=60.0)
    ap.compute_quality_base(pa)
    check(pa.quality_base, 76.5, "missing insider re-normalizes over 85%")
    check(pa.quality_coverage, 0.539, "and costs its 15% of coverage")

    pa = _position(filters=_filters([None] * 9), score_analyst=60.0, score_insider=40.0)
    ap.compute_quality_base(pa)
    check((pa.quality_base, pa.quality_coverage), (50.0, 0.3),
          "no filter data leaves a thin analyst/insider base")

    pa = _position(filters=_filters([None] * 9))
    ap.compute_quality_base(pa)
    check((pa.quality_base, pa.quality_coverage), (None, None),
          "no inputs at all means no quality base")


# ------------------------------------------------ 3. the verdict, per mode ----

def _verdict(mode: str, **kw):
    args = dict(
        composite_score=60.0, coverage=1.0,
        quality_base=80.0, quality_coverage=0.6,
        filters=_filters([1.0] * 7 + [0.5, None], passed=7),
        filter_score=82.0, score_analyst=70.0, score_insider=48.0,
        trend="uptrend", pct_above_ma200=10.0, sector_label="Hot",
        is_holding=True, base_mode=mode,
    )
    args.update(kw)
    return ap.compute_verdict_v2(**args)


def _base_line(v) -> str:
    return v.reason.split(" | ")[1]


def test_verdict_modes() -> None:
    section("compute_verdict_v2: same modifiers, different base")
    comp, qual, blend = (_verdict(m) for m in ("composite", "quality", "blend"))
    check(comp.score, 75.0, "composite: 60 + 10 uptrend + 5 hot sector")
    check(_base_line(comp), "Composite Score 60", "the composite base line is unchanged")
    check((comp.base_mode, comp.coverage, comp.confidence), ("composite", 1.0, "High"),
          "composite confidence comes from composite coverage")

    check(_base_line(qual),
          "Quality base 80 — filters 82 (7/9 pass, 1 without data), "
          "analyst 70, insider 48",
          "the quality base line names its parts")
    check((qual.base_mode, qual.coverage, qual.confidence), ("quality", 0.6, "Medium"),
          "quality confidence comes from quality coverage")
    # 80 + 15 of modifiers, dampened toward the base: 15 * (0.6/0.85 - 1) = -4.
    check(qual.score, 91.0, "quality: 80 + 15 of modifiers, dampened by 4")
    check("Medium confidence: built from 60% of inputs" in qual.reason, True,
          "and the dampening is itemized")

    check(_base_line(blend), "Blend base 70 — composite 60, quality 80",
          "the blend base averages the two")
    check((blend.coverage, blend.confidence), (0.8, "Medium"),
          "blend coverage averages the two coverages")
    check(blend.score, 84.0, "blend: 70 + 15 of modifiers, dampened by 1")

    only_q = _verdict("blend", composite_score=None, coverage=None)
    check(_base_line(only_q), "Blend base 80 — quality 80 only (composite unavailable)",
          "blend falls back to the side that exists")
    check(only_q.coverage, 0.6, "with that side's coverage")

    no_q = _verdict("quality", quality_base=None, quality_coverage=None)
    check((_base_line(no_q), no_q.score, no_q.confidence),
          ("Quality base unavailable, neutral baseline", 65.0, None),
          "a missing quality base starts from a neutral 50")
    bogus = _verdict("bogus")
    check((_base_line(bogus), bogus.base_mode), ("Composite Score 60", "composite"),
          "an unknown mode is treated (and recorded) as composite")

    thin = _verdict("quality", filter_score=None, score_analyst=None, score_insider=None)
    check(_base_line(thin), "Quality base 80 — filters n/a, analyst n/a, insider n/a",
          "missing parts read n/a")


def test_composite_regression() -> None:
    section("compute_verdict_v2: the default mode is the original composite path")
    v = ap.compute_verdict_v2(
        composite_score=64.2, trend="uptrend", pct_above_ma200=30.0,
        sector_label="Hot", upside_pct=25.0, is_holding=True, coverage=1.0)
    check((v.label, v.score, v.base_mode), ("ADD", 87.2, "composite"),
          "64.2 + 12 strong uptrend + 5 hot + 6 upside, ADD")
    check(v.reason,
          "Uptrend (+30% vs 200d), Strong upside to target (+25%) | "
          "Composite Score 64 | +12 · Uptrend (+30% vs 200d) | "
          "+6 · Strong upside to target (+25%) | +5 · Hot sector momentum | "
          "= verdict score 87",
          "the breakdown text is byte-identical to before")
    with _mode_env(saved="quality"):
        again = ap.compute_verdict_v2(
            composite_score=64.2, trend="uptrend", pct_above_ma200=30.0,
            sector_label="Hot", upside_pct=25.0, is_holding=True, coverage=1.0)
    check(again.reason, v.reason,
          "compute_verdict_v2 itself ignores BASE_SCORE_MODE (callers pass the mode)")


def test_verdicts_per_mode() -> None:
    section("verdicts_v2_for / set_v2_verdicts: every mode scored at once")
    pa = _position(composite_score=60.0, composite_coverage=1.0,
                   filters=_filters([1.0] * 9), score_analyst=70.0,
                   score_insider=50.0, trend="sideways", upside_pct=12.0)
    ap.compute_quality_base(pa)
    check(pa.quality_base, 88.0, "0.70*100 + 0.15*70 + 0.15*50")
    verdicts = ap.verdicts_v2_for(pa, is_holding=True)
    check({m: (v.label, v.score) for m, v in verdicts.items()},
          {"composite": ("HOLD", 63.0), "quality": ("ADD", 91.0), "blend": ("HOLD", 77.0)},
          "each mode: its base + 3 for moderate upside")
    check(verdicts["composite"].alternates,
          {"quality": ("ADD", 91.0), "blend": ("HOLD", 77.0)},
          "alternates carry the other modes' label and score")

    with _mode_env(saved="quality"):
        ap.set_v2_verdicts(pa, is_holding=True, position_pct=16.0)
    check(sorted(pa.verdicts), ["blend", "composite", "quality"],
          "set_v2_verdicts keeps all three on the position")
    check((pa.verdict.base_mode, pa.verdict.label, pa.verdict.score),
          ("quality", "HOLD", 87.0),
          "pa.verdict is the run's mode (an overweight holding can't be ADD)")
    check((ap.verdict_in(pa, "blend") is pa.verdicts["blend"],
           ap.verdict_in(pa, None) is pa.verdict), (True, True),
          "verdict_in picks a mode, or the run's verdict")
    etf = _position(verdict=ap.Verdict(label="HOLD", color="#000", reason="x"))
    check(ap.verdict_in(etf, "blend") is etf.verdict, True,
          "a position without per-mode verdicts has one verdict for every mode")

    with _mode_env(saved="blend"):
        ap.set_v2_verdicts(pa, is_holding=False)
    check((pa.verdict.base_mode, pa.verdict.label), ("blend", "BUY"),
          "a watchlist name: BUY under the run's blend base")

    no_comp = _position(filters=_filters([1.0] * 9), score_analyst=70.0)
    ap.compute_quality_base(no_comp)
    check(sorted(ap.verdicts_v2_for(no_comp, is_holding=True)), ["blend", "quality"],
          "a mode with no base gets no verdict")
    check((ap.has_verdict_base(no_comp, "composite"),
           ap.has_verdict_base(no_comp, "quality"),
           ap.has_verdict_base(no_comp, "blend")), (False, True, True),
          "has_verdict_base follows each mode's inputs")


# --------------------------------------------------- 4. choosing the mode ----

def test_mode_resolution() -> None:
    section("base_score_mode: --base-score, then BASE_SCORE_MODE, then composite")
    with _mode_env():
        check(ap.base_score_mode(), "composite", "unset means composite")
    with _mode_env(saved=" Quality "):
        check(ap.base_score_mode(), "quality", "the env var is trimmed and lowercased")
    with _mode_env(saved="quality", override="blend"):
        check(ap.base_score_mode(), "blend", "--base-score wins over the env var")
        check(ap.base_score_mode(saved_only=True), "quality",
              "saved_only reports the saved default underneath")
    with _mode_env(saved="sideways"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            first = ap.base_score_mode()
            ap.base_score_mode()
        check(first, "composite", "an unknown value falls back to composite")
        check(out.getvalue().count("Unknown BASE_SCORE_MODE"), 1, "and warns once")


# ----------------------------------------------------- 5. rank badges -------

def test_rank_badges_per_mode() -> None:
    section("_attach_rank_moves: each mode against its own history")
    today = "2026-09-17"
    ranks = {"composite": {"AAA": {"group": "compounder", "rank": 1}},
             "quality": {"AAA": {"group": "compounder", "rank": 2}},
             "blend": {"AAA": {"group": "compounder", "rank": 3}}}

    def moves(entry):
        r = SimpleNamespace(ticker="AAA")
        tickers = {} if entry is None else {"AAA": entry}
        with _mode_env(saved="quality"):
            ap._attach_rank_moves({"tickers": tickers}, ranks, [r], run_date=today)
        return r._rank_moves, r._rank_move

    legacy = {"first_date": "2026-09-01", "last_rank": 4,
              "last_rank_group": "compounder", "last_rank_date": "2026-09-16"}
    got, run_move = moves(legacy)
    check(got["composite"], {"delta": 3, "new": False},
          "composite reads the ranks on the entry itself (pre-modes ledgers too)")
    check((got["quality"], got["blend"]), (None, None),
          "a mode with no history yet shows no badge for a known stock")
    check(run_move, None, "_rank_move is the run's mode (quality here)")

    tracked = dict(legacy, mode_ranks={
        "quality": {"since": "2026-09-10", "last_rank": 5,
                    "last_rank_group": "compounder", "last_rank_date": "2026-09-16"},
        "blend": {"since": "2026-09-10", "last_rank": 3,
                  "last_rank_group": "watch:Tech", "last_rank_date": "2026-09-16"}})
    got, run_move = moves(tracked)
    check(got["quality"], {"delta": 3, "new": False},
          "quality compares with quality's own rank yesterday")
    check(run_move is got["quality"], True, "and that is the run's badge")
    check(got["blend"], {"delta": None, "new": True}, "a group change is still NEW")

    same_day = dict(legacy, last_rank_date=today, prev_day_rank=2,
                    prev_day_rank_group="compounder", mode_ranks={"quality": {
                        "since": "2026-09-10", "last_rank": 9,
                        "last_rank_group": "compounder", "last_rank_date": today,
                        "prev_day_rank": 4, "prev_day_rank_group": "compounder"}})
    got, _ = moves(same_day)
    check((got["composite"], got["quality"]),
          ({"delta": 1, "new": False}, {"delta": 2, "new": False}),
          "a same-day re-run compares with yesterday, per mode")

    fresh = {"first_date": today, "last_rank": 1,
             "last_rank_group": "compounder", "last_rank_date": today}
    got, _ = moves(fresh)
    check(got, {m: {"delta": None, "new": True} for m in ap.BASE_SCORE_MODES},
          "a stock first seen today is NEW in every mode")
    got, _ = moves(None)
    check(got["blend"], {"delta": None, "new": True}, "so is one with no ledger entry")


def test_ledger_ranks_per_mode() -> None:
    section("update_recs_history: every mode's ranks and verdicts are kept")
    comp = ap.Verdict(label="HOLD", color="#000", reason="x", score=66.0,
                      base_mode="composite")
    qual = ap.Verdict(label="ADD", color="#000", reason="x", score=81.04,
                      base_mode="quality")
    pa = _position(ticker="AAA", current_price=10.0,
                   verdicts={"composite": comp, "quality": qual}, verdict=qual)
    ranks = {"composite": {"AAA": {"group": "compounder", "rank": 2}},
             "quality": {"AAA": {"group": "compounder", "rank": 1}},
             "blend": {"AAA": {"group": "compounder", "rank": 3}}}
    hist = {"tickers": {"AAA": {
        "first_price": 9.0, "first_date": "2026-09-01", "first_verdict": "HOLD",
        "last_rank": 5, "last_rank_group": "compounder",
        "last_rank_date": "2026-09-16", "peak_price": 10.0}}}
    ap.update_recs_history(hist, [pa], run_date="2026-09-17", ranks_by_mode=ranks)
    e = hist["tickers"]["AAA"]
    check((e["prev_day_rank"], e["prev_rank"], e["last_rank"]), (5, 5, 2),
          "composite ranks stay on the entry and roll as before")
    check(e["mode_ranks"]["quality"],
          {"since": "2026-09-17", "prev_day_rank": None, "prev_day_rank_group": None,
           "last_rank": 1, "last_rank_group": "compounder",
           "last_rank_date": "2026-09-17"},
          "another mode gets its own record, tracked from today")
    check(e["last_factors"]["verdicts"],
          {"composite": ["HOLD", 66.0], "quality": ["ADD", 81.0]},
          "the snapshot keeps every mode's verdict")
    check(e["last_factors"]["base_mode"], "quality", "and which one the run acted on")

    ranks["quality"]["AAA"]["rank"] = 4
    ap.update_recs_history(hist, [pa], run_date="2026-09-18", ranks_by_mode=ranks)
    q = e["mode_ranks"]["quality"]
    check((q["prev_day_rank"], q["last_rank"], q["since"]), (1, 4, "2026-09-17"),
          "the next day rolls that mode's own baseline forward")

    ap.update_recs_history(
        hist, [_position(ticker="BBB", current_price=5.0)], run_date="2026-09-18",
        ranks_by_mode={m: {"BBB": {"group": "compounder", "rank": 1}}
                       for m in ap.BASE_SCORE_MODES})
    b = hist["tickers"]["BBB"]
    check((b["last_rank"], b["mode_ranks"]["blend"]["last_rank"],
           b["mode_ranks"]["blend"]["since"]), (1, 1, "2026-09-18"),
          "a first sighting records every mode")
    check("verdicts" in b["last_factors"], False,
          "a position without per-mode verdicts stores none")


# ----------------------------------------------------- 6. the run log -------

def _with_verdicts(ticker: str, by_mode: dict, run: str = "composite") -> ap.PositionAnalysis:
    """A position whose per-mode verdicts are {mode: (label, score)}."""
    verdicts = {m: ap.Verdict(label=label, color="#000", reason="x", score=score,
                              base_mode=m)
                for m, (label, score) in by_mode.items()}
    return _position(ticker=ticker, verdicts=verdicts, verdict=verdicts.get(run))


def test_compare_base_modes() -> None:
    section("compare_base_modes: side-by-side counts and changed verdicts")
    pa = _with_verdicts
    holdings = [
        pa("AAA", {"composite": ("HOLD", 70.0), "quality": ("ADD", 82.0),
                   "blend": ("HOLD", 76.0)}),
        pa("BBB", {"composite": ("TRIM", 40.0), "quality": ("HOLD", 55.0),
                   "blend": ("TRIM", 47.0)}),
    ]
    ccc = pa("CCC", {"composite": ("WAIT", 55.0), "quality": ("WATCH", 64.0),
                     "blend": ("WAIT", 59.5)})
    ddd = pa("DDD", {"composite": ("WATCH", 62.0), "quality": ("WAIT", 58.0),
                     "blend": ("WATCH", 60.0)})
    watch = {"Tech": [ccc, ddd, holdings[0]],     # a held name: not a watchlist row
             "More": [ccc]}
    with _mode_env():
        lines = ap.compare_base_modes(holdings, watch, prune_threshold=60)
    text = "\n".join(lines)
    check(lines[0].startswith("[base-score] Verdicts use the Composite base "
                              "(saved default)"), True, "the header names the active mode")
    rows = {ln.split()[0]: ln.split()[1:] for ln in lines[2:5]}
    check(rows["Composite*"], ["0/1/1/0", "0/1/1/0", "1"],
          "composite: HOLD+TRIM; a WATCH and a WAIT, pruning the WAIT")
    check(rows["Quality"], ["1/1/0/0", "0/1/1/0", "1"],
          "quality: ADD+HOLD; the watchlist pair swaps places")
    check(rows["Blend"], ["0/1/1/0", "0/1/1/0", "1"], "blend matches composite here")
    check("Quality would change 4: AAA HOLD→ADD, BBB TRIM→HOLD, CCC WAIT→WATCH, "
          "DDD WATCH→WAIT" in text, True, "the changed verdicts are listed once per ticker")
    check(lines[lines.index(next(ln for ln in lines if ln.startswith("  Quality would"))) + 1],
          "    and would also prune DDD; keep CCC",
          "a different prune set is spelled out by ticker")
    check(lines[-1], "  Blend would change no verdicts.",
          "a quiet mode says so, with no prune line when the set is the same")

    with _mode_env(saved="composite", override="quality"):
        flipped = [pa("AAA", {"composite": ("HOLD", 70.0), "quality": ("ADD", 82.0)},
                      run="quality")]
        lines = ap.compare_base_modes(flipped)
    check("(this run only)" in lines[0], True, "a one-off mode is labelled as such")
    check(any("prune" in ln for ln in lines), False, "no prune column without a threshold")
    with _mode_env():
        lines = ap.compare_base_modes(holdings, None, prune_threshold=60)
    check(("watchlist" in lines[1], "prune" in lines[1]), (False, False),
          "no watchlist names: no watchlist or prune column")
    check(ap.compare_base_modes([_position()]), [], "no v2 verdicts, no table")


# ------------------------------------------------------- 7. the report ------

def test_mode_variants() -> None:
    section("_mode_variants: one copy per distinct rendering")
    with _mode_env(saved="quality"):
        same = ap._mode_variants(lambda m: "<b>7</b>")
        diff = ap._mode_variants(lambda m: {"composite": "A", "quality": "B",
                                            "blend": "A"}[m])
        empty = ap._mode_variants(lambda m: "" if m == "blend" else m, tag="div")
    check(same, "<b>7</b>", "identical renderings come back unwrapped")
    check(diff,
          "<span class='bmode bmode-composite bmode-blend' style='display:none'>A</span>"
          "<span class='bmode bmode-quality'>B</span>",
          "modes sharing a rendering share a copy; copies the run doesn't use start hidden")
    check(empty,
          "<div class='bmode bmode-composite' style='display:none'>composite</div>"
          "<div class='bmode bmode-quality'>quality</div>",
          "an empty rendering gets no copy at all")
    css = ap._base_view_css()
    check(all(f"html[data-base-view='{m}'] .bmode:not(.bmode-{m})" in css
              and f"html[data-base-view='{m}'] .bmode.bmode-{m}" in css
              for m in ap.BASE_SCORE_MODES), True, "the view CSS covers every mode")
    check(css.count("!important"), 2, "and overrides the inline fallback")


def test_report_markup() -> None:
    section("report: switch, verdict cell, row data and filter dots")
    with _mode_env(saved="quality"):
        html = ap._base_switch_html(interactive=True)
    check('data-run="quality" data-saved="quality"' in html, True,
          "the switch knows the run's and the saved mode")
    check(re.findall(r'data-mode="(\w+)" aria-pressed="true"', html), ["quality"],
          "the run's mode starts pressed")
    check(re.findall(r'data-mode="(\w+)"[^>]*>\w+<span class=\'base-default-mark\'', html),
          ["quality"], "★ marks the saved default")
    check('id="baseDefaultBtn" hidden' in html and " disabled" not in html, True,
          "segments are always live; Make default starts hidden")

    with _mode_env(saved="composite", override="blend"):
        html = ap._base_switch_html(interactive=False)
    check((re.findall(r'data-mode="(\w+)" aria-pressed="true"', html),
           re.findall(r'data-mode="(\w+)"[^>]*>\w+<span', html)),
          (["blend"], ["composite"]),
          "a one-off run starts on its mode, with ★ still on the saved default")
    check(("This run used Blend" in html, "baseDefaultBtn" in html), (True, False),
          "the label explains the override; no repo means no Make default")

    v = ap.Verdict(label="HOLD", color="#2c3e50", score=70.0, base_mode="composite",
                   reason="Hold | Composite Score 64 | +6 · Upside | = verdict score 70",
                   alternates={"quality": ("ADD", 81.0), "blend": ("HOLD", 76.0)})
    cell = ap._verdict_cell(v)
    check("<div class='vcard-base'>Composite Score 64</div>" in cell, True,
          "the card shows the base line")
    check("<div class='valt'>Quality base: ADD 81 · Blend base: HOLD 76</div>" in cell,
          True, "and the other modes' verdicts")
    check("Quality base: ADD 81" in cell.split("data-tip='")[1].split("'")[0], True,
          "the tap-to-reveal text on phones carries them too")

    pa = _with_verdicts("AAA", {"composite": ("TRIM", 44.0), "quality": ("ADD", 86.0),
                                "blend": ("HOLD", 65.0)})
    pa.tax = object()
    pa._rank_moves = {"composite": {"delta": 2, "new": False}, "quality": None,
                      "blend": {"delta": None, "new": True}}
    pa._rank_move = pa._rank_moves["composite"]
    with _mode_env():
        td = ap._verdict_td(pa)
        tr = ap._tr_open(pa, {"composite": 3, "quality": 0, "blend": 1})
        badge = ap._rank_move_badge(pa)
    check(re.findall(r"data-sort(?:-\w+)?='([\d.]+)'", td), ["44.0", "44.0", "86.0", "65.0"],
          "the verdict cell sorts by the run's score, with every mode's alongside")
    check(td.count("class='verdict'"), 3, "and holds one verdict per mode")
    got = dict(re.findall(r"data-([\w-]+-(?:composite|quality|blend))='([^']*)'", tr))
    check((got["verdict-composite"], got["verdict-quality"], got["verdict-score-blend"]),
          ("TRIM", "ADD", "65.0"), "the row carries every mode's verdict and score")
    check((got["has-tax-composite"], got["has-tax-quality"], got["has-tax-blend"]),
          ("1", "0", "1"), "tax detail counts only where that mode flags it")
    check((got["rank-move-composite"], got["rank-delta-composite"],
           got["rank-move-quality"], got["rank-move-blend"]),
          ("up", "2", "", "new"), "rank movement per mode")
    check((got["order-composite"], got["order-quality"], got["order-blend"]),
          ("3", "0", "1"), "and the row's place in each mode's default order")
    check(("▲2" in badge, "NEW" in badge, badge.count("class='bmode"), "bmode-quality" in badge),
          (True, True, 2, False), "badges differ per mode, and quality shows none")

    dots = ap._filter_dots([
        ap.FilterResult("A", True, 1.0, "x", credit=1.0),
        ap.FilterResult("B", False, 1.0, "x", credit=0.4),
        ap.FilterResult("C", False, None, "x", credit=None),
        ap.FilterResult("D", False, 1.0, "x", credit=0.0),
    ])
    check(re.findall(r"background:(#\w+)", dots),
          ["#27ae60", "#c0392b", "#95a5a6", "#c0392b"],
          "green pass, red fail, grey for no data")
    check("near miss, 40% credit" in dots and "no data, left out" in dots, True,
          "tooltips explain partial credit and unrated filters")

    th = ap._verdict_th()
    check(("class='verdict-th'" in th, "picked with the Base switch" in th,
           "Composite base" in th),
          (True, True, False),
          "the verdict header is marked for re-sorting and names no single base")


def _scored(ticker: str, composite: float, credits: list, *, value: float = 1000.0,
            shares: float = 10, analyst: float = 70.0, insider: float = 50.0,
            **kw) -> ap.PositionAnalysis:
    """A compounder with every mode's verdict, as analyze_position leaves it."""
    pa = _position(ticker=ticker, shares=shares, statement_market_value=value,
                   live_market_value=value, current_price=value / max(shares, 1),
                   composite_score=composite, composite_coverage=1.0,
                   filters=_filters(credits), score_analyst=analyst,
                   score_insider=insider, trend="sideways", **kw)
    ap.compute_quality_base(pa)
    ap.set_v2_verdicts(pa, is_holding=shares > 0)
    return pa


class _TagChecker(HTMLParser):
    """Records end tags that don't close the innermost open element."""
    VOID = {"area", "base", "br", "circle", "col", "hr", "img", "input", "line",
            "link", "meta", "path", "source", "stop", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
            return
        self.errors.append(f"</{tag}> at {self.getpos()} inside {self.stack[-3:]}")
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]


def _tag_errors(html: str) -> list[str]:
    checker = _TagChecker()
    checker.feed(html)
    return checker.errors + [f"unclosed <{t}>" for t in checker.stack]


def _render_report(results, watchlists, env=None) -> str:
    """generate_html_report with its network lookups stubbed out."""
    stubs = {"fetch_market_fear_greed": lambda: None,
             "fetch_benchmark_returns": lambda: None,
             "_compute_holdings_ytd_return": lambda results: None}
    real = {name: getattr(ap, name) for name in stubs}
    over = {"GITHUB_REPOSITORY": "owner/repo", **(env or {})}
    old = {k: os.environ.get(k) for k in over}
    os.environ.update(over)
    try:
        for name, stub in stubs.items():
            setattr(ap, name, stub)
        with contextlib.redirect_stdout(io.StringIO()):
            return ap.generate_html_report(results, watchlists=watchlists)
    finally:
        for name, fn in real.items():
            setattr(ap, name, fn)
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_full_report() -> None:
    section("generate_html_report: every mode in one page")
    from tax_analysis import TaxAnalysis

    all_pass, all_fail = [1.0] * 9, [0.0] * 9
    with _mode_env():
        # Nine equal holdings (11% each: a -2 size nudge, no ADD ceiling).
        holdings = [
            _scored("AAA", 80, all_pass),     # ADD under every base
            _scored("BBB", 45, all_pass),     # TRIM / ADD / HOLD
            _scored("CCC", 70, all_fail),     # HOLD / SELL / TRIM
            *(_scored(t, 65, all_pass) for t in ("DDD", "EEE", "FFF", "GGG")),
            _scored("HHH", 60, [1.0] * 6 + [0.5, None, None]),
        ]
        etf = _position(ticker="ETF", bucket="thematic", shares=10,
                        statement_market_value=1000.0, live_market_value=1000.0,
                        current_price=100.0,
                        verdict=ap.Verdict(label="HOLD", color="#2c3e50",
                                           reason="Thematic hold"))
        results = holdings + [etf]
        holdings[1].tax = TaxAnalysis(
            ticker="BBB", verdict=holdings[1].verdict.label, has_lots=True,
            lt_shares=6, st_shares=4, lt_gain=300.0, st_gain=200.0,
            lt_tax=45.0, st_tax=48.0, timing_note="Trim the long-term lots first.",
            strategies=["Trim in stages."],
            lots_detail=[{"date": "2024-01-02", "shares": 6, "buy_price": 50.0,
                          "days_held": 990, "is_long_term": True, "days_to_lt": 0,
                          "gain": 300.0},
                         {"date": "2026-05-01", "shares": 4, "buy_price": 50.0,
                          "days_held": 139, "is_long_term": False,
                          "days_to_lt": 227, "gain": 200.0}])
        holdings[2].tax = TaxAnalysis(
            ticker="CCC", verdict=holdings[2].verdict.label, unrealized_gain=500.0,
            is_long_term=True, tax_if_long_term=75.0, tax_if_short_term=120.0,
            effective_rate_lt=0.15, effective_rate_st=0.24,
            strategies=["Trim in stages."])
        watch = {"Tech": [_scored("WWW", 50, all_pass, shares=0, value=0.0,
                                  upside_pct=5.0),
                          _scored("XXX", 75, all_fail, shares=0, value=0.0,
                                  upside_pct=5.0)]}
        html = _render_report(results, watch)
        qr_html = _render_report(results, watch, env={"QUICK_RECS": "1"})

    labels = {p.ticker: {m: v.label for m, v in p.verdicts.items()}
              for p in holdings[:3] + watch["Tech"]}
    check(labels,
          {"AAA": {"composite": "ADD", "quality": "ADD", "blend": "ADD"},
           "BBB": {"composite": "TRIM", "quality": "ADD", "blend": "HOLD"},
           "CCC": {"composite": "HOLD", "quality": "SELL", "blend": "TRIM"},
           "WWW": {"composite": "WAIT", "quality": "BUY", "blend": "WATCH"},
           "XXX": {"composite": "BUY", "quality": "PASS", "blend": "WAIT"}},
          "the fixture's verdicts differ by mode (after the size overlay)")
    check('<html data-base-view="composite">' in html, True,
          "the page opens on the run's mode")
    rows = re.findall(r"<tr data-verdict='[^>]*>", html)
    per_mode = [r for r in rows if "data-verdict-quality=" in r]
    check((len(rows), len(per_mode)), (11, 10),
          "every compounder row carries per-mode data; the ETF row doesn't")
    check(all(f"data-order-{m}=" in r for r in per_mode for m in ap.BASE_SCORE_MODES),
          True, "and its place in each mode's default order")
    bbb = next(r for r in rows if "data-search='bbb" in r)
    check(("data-verdict-quality='ADD'" in bbb, "data-has-tax-quality='0'" in bbb,
           "data-has-tax-composite='1'" in bbb), (True, True, True),
          "BBB: ADD with no tax flag under quality, flagged under composite")

    cards = re.findall(r"<div class='bmode ([\w -]+)'[^>]*><div style='border:1px solid "
                       r"var\(--border-medium\);border-radius:8px;background:var\(--bg-card\);"
                       r"padding:14px 16px;margin-bottom:14px;'><div[^>]*>"
                       r"<span class='ticker'[^>]*>(\w+)</span>", html)
    check(sorted((t, c) for c, t in cards),
          [("BBB", "bmode-blend"), ("BBB", "bmode-composite"),
           ("CCC", "bmode-blend"), ("CCC", "bmode-composite"), ("CCC", "bmode-quality")],
          "tax cards show only in the modes that flag them, one per verdict pill")
    check("No positions are flagged under" in html, False,
          "every mode flags something here, so no empty-section note")

    sell_trim = re.search(r"<strong>(.*?)</strong>Sell / Trim flags", html).group(1)
    check(sell_trim, "1",
          "a count that's the same in every mode renders once (BBB, CCC, CCC)")
    adds = re.search(r"<strong>(.*?)</strong>Add candidates", html).group(1)
    check(re.findall(r"class='bmode ([\w -]+)'[^>]*>(\d+)<", adds),
          [("bmode-composite bmode-blend", "1"), ("bmode-quality", "7")],
          "one that differs renders per mode")
    buys = re.search(r"<strong>(.*?)</strong>Watchlist BUY signals", html).group(1)
    check(re.findall(r"class='bmode ([\w -]+)'[^>]*>(\d+)<", buys),
          [("bmode-composite bmode-quality", "1"), ("bmode-blend", "0")],
          "watchlist BUY count per mode")
    check(all(f'id="healthMeter-{m}"' in html for m in ap.BASE_SCORE_MODES), True,
          "a health gauge per mode")
    # Header controls: quick recommendations are hidden unless QUICK_RECS=1,
    # and the old auto-refresh toggle is gone in favour of a reload button.
    check(('<div class="qr-list">' in html, 'id="qrWrap"' in html), (False, False),
          "the quick-recommendations chip stays out of the header by default")
    check(('id="pageReloadBtn"' in html, 'id="autoReloadToggle"' in html),
          (True, False), "the header has a browser-reload button, no Auto toggle")
    qr = re.search(r'<div class="qr-list">(.*?)</div></div></div>',
                   qr_html, re.S).group(1)
    check(qr.count("class='bmode"), 3,
          "QUICK_RECS=1 brings the chip back, one list per mode")
    check(_tag_errors(qr_html), [], "and the page still balances with it")

    check(_tag_errors(html), [],
          "the page's tags balance, per-lot tax detail and mode copies included")
    ids = re.findall(r"""\sid=["']([^"']+)["']""", html)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    check(dupes, [], "no element id repeats across the mode copies")

    node = shutil.which("node")
    if not node:
        print("  (node not found — skipping the JS syntax check)")
        return
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, js in enumerate(scripts):
            path = Path(tmp) / f"s{i}.js"
            path.write_text(js)
            proc = subprocess.run([node, "--check", str(path)],
                                  capture_output=True, text=True)
            if proc.returncode:
                bad.append(proc.stderr.strip().splitlines()[-1])
    check((len(scripts) >= 5, bad), (True, []), "every inline script parses")


def test_refresh_script_parses() -> None:
    section("report: the dispatch script (with Make default) is valid JS")
    old = os.environ.get("GITHUB_REPOSITORY")
    os.environ["GITHUB_REPOSITORY"] = "owner/repo"
    try:
        buttons, widget = ap._build_refresh_widget()
    finally:
        if old is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = old
    check('id="baseScoreSwitch"' in buttons and 'id="baseDefaultBtn"' in buttons, True,
          "the switch and Make default sit with the header buttons")
    check(buttons.index("baseScoreSwitch") < buttons.index("taxSectionToggle"), True,
          "ahead of the Tax toggle")
    script = widget.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    check(('"/actions/variables/" + BASE_VAR' in script, "base_score_mode" in script),
          (True, False),
          "Make default saves the repository variable; refreshes no longer pick a mode")
    check("__BASE_LABELS__" in script or "__DEFAULT_NOTE__" in script, False,
          "the placeholders are filled in")
    node = shutil.which("node")
    if not node:
        print("  (node not found — skipping the JS syntax check)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "widget.js"
        path.write_text(script)
        proc = subprocess.run([node, "--check", str(path)],
                              capture_output=True, text=True)
    check(proc.returncode, 0, "node --check accepts the script"
          + (f": {proc.stderr.strip()}" if proc.returncode else ""))


# -------------------------------------------------------------------- main ----

def main() -> int:
    for t in (test_soft_credits, test_missing_versus_loss, test_filter_score,
              test_quality_base, test_verdict_modes, test_composite_regression,
              test_verdicts_per_mode, test_mode_resolution,
              test_rank_badges_per_mode, test_ledger_ranks_per_mode,
              test_compare_base_modes, test_mode_variants, test_report_markup,
              test_full_report, test_refresh_script_parses):
        before = len(_results)
        t()
        passed = sum(1 for ok, _ in _results[before:] if ok)
        total = len(_results) - before
        print(f"  {passed}/{total} passed")

    failed = [label for ok, label in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    if failed:
        print("\nFAILED:")
        for label in failed:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
