"""
test_screen_scoring.py
----------------------
Tests for the S&P 500/400 screen: the one-request prefilter (screener.py), the
stage-2 scorer that gives the Screening table the report's own Composite
(analyze_portfolio.score_screen_shortlist), and the Yahoo rate-limit handling
both stages run through (yahoo_limits.py).

The policy under test: a screened name's filters, sub-scores and Composite
come from the same functions as a holding's, so the Screening table and the
Quality Compounders table never disagree about one stock; a name Yahoo
refused is retried after a cool-down and, failing that, reported as not
screened instead of silently dropped; and nothing fetched while Yahoo was
refusing is cached.

Hermetic: no network, no real cache file. Run it directly —

    ./venv/bin/python test_screen_scoring.py

Exit code is 0 when everything passes, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import json
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import analyze_portfolio as ap
import screener as sc
import yahoo_limits as yl

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


class FakeClock:
    """A clock the gate can sleep on without real waiting."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


_RealGate = yl.YahooGate      # tests below swap yl.YahooGate for a quiet one


def quiet_gate(**kw) -> yl.YahooGate:
    clock = FakeClock()
    return _RealGate(sleep=clock.sleep, clock=clock, log=lambda _m: None, **kw)


RATE_LIMITED = "YFRateLimitError: Too Many Requests. Rate limited. Try after a while."

# A profile that passes all nine of analyze_portfolio's filters.
PASSING = {
    "revenueCAGR3y": 0.20, "earningsCAGR3y": 0.25, "trailingPE": 25,
    "forwardPE": 20, "trailingPegRatio": 1.2, "roeAvg3y": 0.25,
    "operatingMarginAvg3y": 0.30, "debtToEquity": 40, "freeCashflow": 5e9,
    "fcfConsistency": {"positive_years": 3, "total_years": 3, "growing": True},
    "quickRatio": 1.5, "trailingEps": 5.0, "marketCap": 1e11,
    "_fcfGrowing": True,
}


# ------------------------------------------------------- 1. yahoo_limits ----

def test_rate_limit_detection() -> None:
    section("yahoo_limits: a refusal is recognised however it surfaces")
    from yfinance.exceptions import YFRateLimitError
    check(yl.is_rate_limited(YFRateLimitError()), True, "the exception itself")
    check(yl.is_rate_limited(RATE_LIMITED), True, "an error string recording one")
    check(yl.is_rate_limited("no info"), False, "an ordinary error is not one")
    check(yl.is_rate_limited(None), False, "nor is no error")

    class Fake:
        def get(self, fail):
            if fail:
                raise YFRateLimitError()
            return "ok"

    yl._count_refusals(Fake, YFRateLimitError)
    before = yl.refusals()
    check(Fake().get(False), "ok", "a wrapped request passes its answer through")
    try:
        Fake().get(True)
        raised = False
    except YFRateLimitError:
        raised = True
    check((raised, yl.refusals() - before), (True, 1),
          "a refused one is counted and still raised")
    check(yl.install(), True, "install() wraps yfinance's request method")
    from yfinance.data import YfData
    check(hasattr(YfData.get, "__wrapped__"), True, "and leaves it wrapped")


def test_gate() -> None:
    section("YahooGate: one cool-down per block, and a limit on how many")
    clock = FakeClock()
    log = []
    gate = yl.YahooGate(cooldown_s=240, max_trips=2, sleep=clock.sleep,
                        clock=clock, log=log.append)
    gate.wait()
    check(clock.now, 0.0, "an open gate doesn't wait")
    check(gate.trip(), True, "the first refusal closes it")
    check(gate.trip(), True, "a second worker's refusal in the same block...")
    check(gate.trips, 1, "...doesn't start a second cool-down")
    gate.wait()
    check(clock.now >= 240, True, "wait() sits out the cool-down")
    check(gate.trip(), True, "a refusal after reopening starts another")
    clock.sleep(240)
    check((gate.trip(), gate.exhausted), (False, True),
          "past max_trips the gate gives up")
    check(len(log), 3, "each cool-down and the giving up are logged once")


def test_run_gated() -> None:
    section("run_gated: refused names are retried after the cool-down")
    gate = quiet_gate(cooldown_s=60, max_trips=3)
    calls: dict[str, int] = {}
    lock = threading.Lock()

    def fn(item):
        with lock:
            calls[item] = calls.get(item, 0) + 1
            n = calls[item]
        return item == "B" and n == 1          # B is refused once

    refused = yl.run_gated(["A", "B", "C"], fn, gate=gate, workers=2)
    check(refused, [], "everything lands in the end")
    check((calls["A"], calls["B"], calls["C"]), (1, 2, 1),
          "only the refused name is fetched again")
    check(gate.trips, 1, "after one cool-down")

    gate = quiet_gate(cooldown_s=60, max_trips=5)
    calls.clear()
    refused = yl.run_gated(["X"], lambda i: True, gate=gate, attempts=3)
    check(refused, ["X"], "a name refused every time is handed back")

    gate = quiet_gate(cooldown_s=60, max_trips=1)
    refused = yl.run_gated(["P", "Q"], lambda i: True, gate=gate, workers=1)
    check((sorted(refused), gate.exhausted), (["P", "Q"], True),
          "once the gate is exhausted the rest are handed back unfetched")

    # A swallowed refusal: fn reports nothing, but a 429 landed meanwhile.
    gate = quiet_gate(cooldown_s=60, max_trips=3)
    seen = {"n": 0}

    def swallowing(item):
        seen["n"] += 1
        if seen["n"] == 1:
            yl.note_refusal()                  # yfinance ate the error
        return False

    refused = yl.run_gated(["S"], swallowing, gate=gate, watch_counter=True)
    check((refused, seen["n"]), ([], 2),
          "watch_counter retries an attempt that saw any 429")
    refused = yl.run_gated(["T"], lambda i: (yl.note_refusal(), False)[1],
                           gate=quiet_gate(cooldown_s=1, max_trips=9),
                           attempts=2)
    check(refused, [], "without watch_counter only fn's own answer counts")


# -------------------------------------------------------- 2. the prefilter ----

QS = {"quoteSummary": {"result": [{
    "financialData": {"currentPrice": 100.0, "targetMeanPrice": 120.0,
                      "recommendationMean": 1.8, "numberOfAnalystOpinions": 20,
                      "revenueGrowth": 0.12, "earningsGrowth": 0.05,
                      "returnOnEquity": 0.30, "operatingMargins": 0.25,
                      "debtToEquity": 50.0, "quickRatio": 1.4,
                      "freeCashflow": 2e9},
    "defaultKeyStatistics": {"pegRatio": 1.5, "trailingEps": 4.0},
    "summaryDetail": {"trailingPE": 32.0, "forwardPE": 24.0,
                      "marketCap": 5e10, "fiftyTwoWeekHigh": 150.0,
                      "fiftyTwoWeekLow": 50.0},
    "assetProfile": {"sector": "Technology", "industry": "Software"},
    "quoteType": {"shortName": "Acme", "longName": "Acme Corp"},
}]}}


class FakeTicker:
    def __init__(self, payload=QS, info=None):
        self._quote = SimpleNamespace(_fetch=lambda modules: payload)
        self.info = info


def test_quote_summary() -> None:
    section("prefilter: one quoteSummary request, flattened like Ticker.info")
    with patched(sc.yf, Ticker=lambda t: FakeTicker()):
        info = sc._quote_summary("ACME")
    check((info["currentPrice"], info["pegRatio"], info["sector"],
           info["shortName"]), (100.0, 1.5, "Technology", "Acme"),
          "every module's fields land in one flat dict")

    raw = {"quoteSummary": {"result": [{"summaryDetail": {
        "trailingPE": {"raw": 12.5, "fmt": "12.50"}, "forwardPE": None}}]}}
    with patched(sc.yf, Ticker=lambda t: FakeTicker(payload=raw)):
        info = sc._quote_summary("ACME")
    check(info, {"trailingPE": 12.5}, "formatted values unwrap; empty ones drop")

    with patched(sc.yf, Ticker=lambda t: FakeTicker(payload=None)):
        check(sc._quote_summary("ACME"), {}, "no payload (a 404) is no info")

    fallback = SimpleNamespace(info={"trailingPE": 9.0})
    with patched(sc.yf, Ticker=lambda t: fallback):
        check(sc._quote_summary("ACME"), {"trailingPE": 9.0},
              "if yfinance's internals move it falls back to Ticker.info")


def test_prefilter_one() -> None:
    section("prefilter: a rough reading that only rules out plain failures")
    with patched(sc.yf, Ticker=lambda t: FakeTicker()):
        r = sc.prefilter_one("ACME")
    check((r.name, r.sector, r.price, r.upside_pct, r.week52_pos),
          ("Acme", "Technology", 100.0, 20.0, 50.0), "the table's columns fill in")
    check(r.pe, 24.0, "P/E is the lower of trailing and forward, as in stage 2")
    check(r.de_ratio, 0.5, "D/E is read as a ratio")
    check(r.prefilter_failed, 1, "only the 5% EPS growth counts against it")
    check((r.passes, r.num_failed, r.score_composite), ({}, 9, None),
          "the filters and scores are left for stage 2")

    thin = dict(QS["quoteSummary"]["result"][0])
    thin["financialData"] = {"currentPrice": 100.0, "debtToEquity": 50.0,
                             "quickRatio": 1.4}
    thin["defaultKeyStatistics"] = {}
    with patched(sc.yf, Ticker=lambda t: FakeTicker(
            payload={"quoteSummary": {"result": [thin]}})):
        r = sc.prefilter_one("THIN")
    check(r.prefilter_failed, 0,
          "missing growth, ROE, margin, PEG or FCF isn't held against a name")

    def boom(modules):
        raise RuntimeError("Too Many Requests. Rate limited. Try after a while.")

    with patched(sc.yf, Ticker=lambda t: SimpleNamespace(
            _quote=SimpleNamespace(_fetch=boom))):
        r = sc.prefilter_one("RL")
    check(yl.is_rate_limited(r.error), True, "a refusal is recorded as one")


def test_prefilter_universe() -> None:
    section("prefilter_universe: refusals retried, the rest reported")
    tries: dict[str, int] = {}

    def fake(t):
        tries[t] = tries.get(t, 0) + 1
        if t == "LATE" and tries[t] == 1:
            return sc.ScreenResult(ticker=t, error=RATE_LIMITED)
        if t == "GONE":
            return sc.ScreenResult(ticker=t, error=RATE_LIMITED)
        return sc.ScreenResult(ticker=t, prefilter_failed=0)

    with patched(sc, prefilter_one=fake):
        out = sc.prefilter_universe(["AAA", "LATE", "GONE"],
                                    gate=quiet_gate(max_trips=5),
                                    verbose=False)
    check([r.ticker for r in out], ["AAA", "LATE", "GONE"], "input order kept")
    check((out[1].error, tries["LATE"]), (None, 2),
          "a name refused once is fetched again and lands")
    check(yl.is_rate_limited(out[2].error), True,
          "one refused every time comes back marked, not missing")


# ------------------------------------------------------- 3. the stage-2 scorer ----

def _inputs(info_by_ticker: dict, breakdown=None):
    """A stand-in for _screen_scoring_inputs over canned infos."""
    def fake(ticker, name):
        if ticker not in info_by_ticker:
            raise ValueError("no info")
        pa = ap.PositionAnalysis(ticker=ticker, name=name or ticker,
                                 shares=0.0, statement_market_value=0.0,
                                 statement_pct_portfolio=0.0)
        pa.rating_breakdown = breakdown or {"buy": 20, "hold": 5, "sell": 0,
                                            "total": 25, "rec_avg": 2.2}
        return pa, dict(info_by_ticker[ticker])
    return fake


def test_scorer_matches_the_holding() -> None:
    section("score_screen_shortlist: the Screening row is the holding's row")
    insider_reads = []

    def fake_insider(pa, ticker, info):
        insider_reads.append(ticker)
        pa.insider_activity = {"net_signal": "Selling"}
        pa.score_insider = 48.0
        pa.score_insider_cal = None

    infos = {
        "PASS": PASSING,
        "NEAR": dict(PASSING, earningsCAGR3y=0.05, quickRatio=0.8),
        "FAR": dict(PASSING, earningsCAGR3y=0.05, quickRatio=0.8,
                    debtToEquity=150),
    }
    rows = [sc.ScreenResult(ticker=t, name=t) for t in ("PASS", "NEAR", "FAR")]
    with patched(ap, _screen_scoring_inputs=_inputs(infos),
                 _set_insider_scores=fake_insider,
                 _flush_fund_cache=lambda: None):
        ap.score_screen_shortlist(rows, gate=quiet_gate(), verbose=False)
    passing, near, far = rows
    check((passing.num_passed, near.num_failed, far.num_failed), (9, 2, 3),
          "the nine filters are analyze_portfolio's")
    check(sorted(k for k, v in near.passes.items() if not v),
          ["eps_growth", "quick"], "failures are listed by their short names")
    check(sorted(insider_reads), ["NEAR", "PASS"],
          "the insider read is made only for names the table shows")
    check(far.score_composite, None, "a name the table won't show isn't scored")

    # The same inputs through the holdings path: compute_composite_score.
    pa = ap.PositionAnalysis(ticker="PASS", name="PASS", shares=10,
                             statement_market_value=0,
                             statement_pct_portfolio=0)
    pa.rating_breakdown = {"buy": 20, "hold": 5, "sell": 0, "total": 25,
                           "rec_avg": 2.2}
    pa.score_insider, pa.score_insider_cal = 48.0, None
    ap.compute_composite_score(pa, dict(PASSING))
    check((passing.score_composite, passing.score_quality, passing.score_growth,
           passing.score_value, passing.score_analyst, passing.score_insider),
          (pa.composite_score, pa.score_quality, pa.score_growth,
           pa.score_value, pa.score_analyst, pa.score_insider),
          "Composite and every sub-score equal the holding's")


def test_scorer_refusals() -> None:
    section("score_screen_shortlist: a refused name is marked, not scored")
    from yfinance.exceptions import YFRateLimitError

    def always_refused(ticker, name):
        if ticker == "RL":
            raise YFRateLimitError()
        return _inputs({"OK": PASSING})(ticker, name)

    rows = [sc.ScreenResult(ticker="OK"), sc.ScreenResult(ticker="RL"),
            sc.ScreenResult(ticker="NONE")]
    with patched(ap, _screen_scoring_inputs=always_refused,
                 _set_insider_scores=lambda pa, t, i: None,
                 _flush_fund_cache=lambda: None):
        ap.score_screen_shortlist(rows, gate=quiet_gate(max_trips=5),
                                  verbose=False)
    ok, rl, none = rows
    check((ok.error, ok.num_passed), (None, 9), "the good name is scored")
    check((yl.is_rate_limited(rl.error), rl.score_composite), (True, None),
          "the refused one carries the refusal and no score")
    check(none.error, "ValueError: no info", "a name with no data says so")
    check(sc.split_passers_and_near_misses(rows)[0], [ok],
          "only the scored name reaches the table")


def test_growth_cache_guard() -> None:
    section("_compute_growth_cached: nothing fetched during a block is cached")

    def computes(refuse: bool):
        def fake(tkr, info):
            if refuse:
                yl.note_refusal()              # the statement fetch was refused
            info["revenueCAGR3y"] = 0.2
        return fake

    with tempfile.TemporaryDirectory() as d, \
            patched(ap, _fund_cache={}, _fund_cache_dirty=False,
                    _FUNDAMENTALS_CACHE_PATH=Path(d) / "f.json"):
        with patched(ap, _compute_multi_year_growth=computes(refuse=True)):
            ap._compute_growth_cached(None, {}, "BLOCKED")
        with patched(ap, _compute_multi_year_growth=computes(refuse=False)):
            ap._compute_growth_cached(None, {}, "CLEAN")
        check(sorted(ap._fund_cache), ["CLEAN"],
              "only the name fetched cleanly is cached")


# ----------------------------------------------- 4. the scan and its cache ----

def test_screen_universe() -> None:
    section("screen_universe: prefilter, shortlist, score, cache")
    pre = {"AAA": 0, "BBB": 1, "CCC": sc.PREFILTER_MAX_FAILED,
           "DDD": sc.PREFILTER_MAX_FAILED + 1}

    def fake_prefilter(t):
        if t == "EEE":
            return sc.ScreenResult(ticker=t, error=RATE_LIMITED)
        return sc.ScreenResult(ticker=t, name=t, prefilter_failed=pre[t])

    scored = []

    def fake_score(shortlist, gate, max_workers, verbose):
        for r in shortlist:
            scored.append(r.ticker)
            r.num_failed = {"AAA": 0, "BBB": 1, "CCC": 4}[r.ticker]
            r.num_passed = 9 - r.num_failed
            r.score_composite = {"AAA": 70.0, "BBB": 80.0, "CCC": 50.0}[r.ticker]

    with tempfile.TemporaryDirectory() as d, \
            patched(sc, _SCREEN_CACHE_PATH=Path(d) / "screen.json",
                    fetch_sp500_sp400=lambda verbose: list(pre) + ["EEE"],
                    prefilter_one=fake_prefilter), \
            patched(yl, YahooGate=lambda: quiet_gate(max_trips=5)):
        out = sc.screen_universe(fake_score, verbose=False)
        check(sorted(scored), ["AAA", "BBB", "CCC"],
              f"names within {sc.PREFILTER_MAX_FAILED} prefilter misses are scored")
        check(([r.ticker for r in out["passed"]],
               [r.ticker for r in out["near_miss"]]), (["AAA"], ["BBB"]),
              "the scorer's verdict decides passed and near miss")
        check((out["universe_size"], out["unscreened"]), (5, ["EEE"]),
              "a name Yahoo refused is reported as not screened")

        cached = json.loads((Path(d) / "screen.json").read_text())
        check(cached["scoring"], sc._CACHE_SCORING, "the cache records how it was scored")
        again = sc.screen_universe(lambda *a, **k: scored.append("again"),
                                   verbose=False)
        check((again["from_cache"], [r.ticker for r in again["passed"]],
               again["unscreened"], "again" in scored),
              (True, ["AAA"], ["EEE"], False), "the day's scan is read back")

        cached["scoring"] = None              # a scan from the old screener
        (Path(d) / "screen.json").write_text(json.dumps(cached))
        check(sc._load_screen_cache(verbose=False), None,
              "a cache scored any other way is not today's scan")


def test_split() -> None:
    section("split_passers_and_near_misses: best Composite first")
    rows = [SimpleNamespace(ticker=t, num_passed=9 - f, num_failed=f,
                            score_composite=c, error=e)
            for t, f, c, e in [("A", 0, 60.0, None), ("B", 0, 75.0, None),
                               ("C", 2, 66.0, None), ("D", 1, None, None),
                               ("E", 3, 90.0, None), ("F", 0, 99.0, "boom")]]
    passed, near = sc.split_passers_and_near_misses(rows)
    check(([r.ticker for r in passed], [r.ticker for r in near]),
          (["B", "A"], ["C", "D"]),
          "three misses is out, an error is out, unscored sorts last")


# ------------------------------------------------------------ 5. rendering ----

def test_render_live_scores() -> None:
    section("Screening table: this run's numbers for names it analyzed")
    rows = [sc.ScreenResult(ticker=t, name=t, num_passed=9, num_failed=0,
                            score_composite=c, score_quality=c)
            for t, c in (("HELD", 60.0), ("SCAN", 65.0))]
    sr = {"passed": rows, "near_miss": [], "universe_size": 900,
          "unscreened": ["X", "Y"], "scanned_at": "2026-10-05T07:01:55-04:00"}
    held = ap.PositionAnalysis(ticker="HELD", name="Held", shares=5,
                               statement_market_value=0,
                               statement_pct_portfolio=0)
    held.composite_score, held.score_quality = 72.4, 81.0
    html = ap._render_screening_section(sr, {"HELD": held})
    check("Screened 898 of 900 tickers (2 could not be fetched from Yahoo)" in html,
          True, "the header counts what was actually screened")
    check("from the day's scan at 7:01 AM ET" in html, True,
          "and says when the scan ran")
    check(html.index(">HELD<") < html.index(">SCAN<"), True,
          "a live score re-sorts the table")
    check("Composite Score: 72" in html, True, "the held name shows its run score")
    check(rows[0].score_composite, 60.0, "the scan's own rows are left untouched")

    errored = ap.PositionAnalysis(ticker="SCAN", name="S", shares=0,
                                  statement_market_value=0,
                                  statement_pct_portfolio=0, error="boom")
    html = ap._render_screening_section(sr, {"SCAN": errored})
    check("Composite Score: 65" in html, True,
          "an analysis that errored doesn't replace the scan's score")


# -------------------------------------------------------------------- main ----

def main() -> int:
    for t in (test_rate_limit_detection, test_gate, test_run_gated,
              test_quote_summary, test_prefilter_one, test_prefilter_universe,
              test_scorer_matches_the_holding, test_scorer_refusals,
              test_growth_cache_guard, test_screen_universe, test_split,
              test_render_live_scores):
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
