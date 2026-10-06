"""
test_screen_scoring.py
----------------------
Tests for the S&P 500/400 screen: the one-request prefilter (screener.py), the
stage-2 scorer that gives the Screening table the report's own Composite
(analyze_portfolio.score_screen_shortlist), and the Yahoo rate-limit handling
both stages run through (yahoo_limits.py).

The policy under test: a screened name's filters, sub-scores and Composite
come from the same functions as a holding's, so the Screening table and the
Quality Compounders table never disagree about one stock; each run spends a
capped number of Yahoo requests on the scan and stops at the first refusal,
leaving the rest for the next run instead of retrying into a longer block;
and nothing fetched while Yahoo was refusing is cached.

Hermetic: no network, no real cache file. Run it directly —

    ./venv/bin/python test_screen_scoring.py

Exit code is 0 when everything passes, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import json
import tempfile
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


def quiet_budget(limit=None) -> yl.YahooBudget:
    return yl.YahooBudget(limit, log=lambda _m: None)


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

    yl._count_requests(Fake, YFRateLimitError)
    sent, refused = yl.requests(), yl.refusals()
    check(Fake().get(False), "ok", "a wrapped request passes its answer through")
    try:
        Fake().get(True)
        raised = False
    except YFRateLimitError:
        raised = True
    check((raised, yl.requests() - sent, yl.refusals() - refused), (True, 2, 1),
          "every request is counted; a refused one also as a refusal, and raised")
    check(yl.install(), True, "install() wraps yfinance's request method")
    from yfinance.data import YfData
    check(hasattr(YfData.get, "__wrapped__"), True, "and leaves it wrapped")


def test_budget() -> None:
    section("YahooBudget: a run's allowance, and the first refusal stops it")
    log = []
    budget = yl.YahooBudget(limit=3, log=log.append)
    check(budget.spent(), False, "a fresh budget has room")
    for _ in range(3):
        yl.note_request()
    check((budget.used(), budget.spent()), (3, True), "three requests spend three")

    open_ended = yl.YahooBudget(log=log.append)
    for _ in range(50):
        yl.note_request()
    check(open_ended.spent(), False, "no limit: only a refusal stops it")
    open_ended.refused()
    open_ended.refused()
    check((open_ended.spent(), open_ended.stopped, len(log)), (True, True, 1),
          "a refusal stops it, and is logged once")


def test_run_gated() -> None:
    section("run_gated: stops at the budget or the first refusal")
    started = []

    def costs_two(item) -> bool:
        started.append(item)
        yl.note_request()
        yl.note_request()
        return False

    left = yl.run_gated(list("ABCDE"), costs_two, budget=quiet_budget(4),
                        workers=1)
    check((started, left), (["A", "B"], ["C", "D", "E"]),
          "once the budget is spent the rest are handed back unstarted")

    started.clear()
    budget = quiet_budget()
    left = yl.run_gated(list("ABC"), lambda i: (started.append(i), i == "B")[1],
                        budget=budget, workers=1)
    check((started, left, budget.stopped), (["A", "B"], ["B", "C"], True),
          "a refusal stops the run: the refused name and the rest wait")

    # A swallowed refusal: fn reports nothing, but a 429 landed meanwhile.
    budget = quiet_budget()
    left = yl.run_gated(["S"], lambda i: (yl.note_refusal(), False)[1],
                        budget=budget, watch_counter=True)
    check((left, budget.stopped), (["S"], True),
          "watch_counter treats an attempt that saw any 429 as refused")
    left = yl.run_gated(["T"], lambda i: (yl.note_refusal(), False)[1],
                        budget=quiet_budget())
    check(left, [], "without watch_counter only fn's own answer counts")


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
    section("prefilter_universe: a refusal leaves the rest for the next run")

    def fake(t):
        if t == "RL":
            return sc.ScreenResult(ticker=t, error=RATE_LIMITED)
        if t == "NONE":
            return sc.ScreenResult(ticker=t, error="no info")
        return sc.ScreenResult(ticker=t, prefilter_failed=0)

    with patched(sc, prefilter_one=fake):
        done, left = sc.prefilter_universe(["AAA", "NONE", "RL", "LATER"],
                                           budget=quiet_budget(),
                                           max_workers=1, verbose=False)
    check([r.ticker for r in done], ["AAA", "NONE"],
          "fetched names come back, and so does one with no data")
    check(done[1].error, "no info", "that one carries its error")
    check(left, ["RL", "LATER"],
          "the refused name and everything after it wait for the next run")


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
        left = ap.score_screen_shortlist(rows, budget=quiet_budget(),
                                         verbose=False)
    passing, near, far = rows
    check(left, [], "with room in the budget every name is scored")
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
    section("score_screen_shortlist: a refusal hands the rest back")
    from yfinance.exceptions import YFRateLimitError

    def refuses_rl(ticker, name):
        if ticker == "RL":
            raise YFRateLimitError()
        return _inputs({"OK": PASSING, "AFTER": PASSING})(ticker, name)

    rows = [sc.ScreenResult(ticker=t) for t in ("OK", "NONE", "RL", "AFTER")]
    with patched(ap, _screen_scoring_inputs=refuses_rl,
                 _set_insider_scores=lambda pa, t, i: None,
                 _flush_fund_cache=lambda: None):
        left = ap.score_screen_shortlist(rows, budget=quiet_budget(),
                                         max_workers=1, verbose=False)
    ok, none, rl, after = rows
    check((ok.error, ok.num_passed), (None, 9), "the name before it is scored")
    check(none.error, "ValueError: no info", "a name with no data says so")
    check([r.ticker for r in left], ["RL", "AFTER"],
          "the refused name and the rest come back for the next run")
    check((rl.error, rl.score_composite, after.num_failed), (None, None, 9),
          "untouched: no error recorded against them, nothing scored")
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

def _fake_scorer(verdicts: dict, scored: list):
    """A scorer that spends two requests a name, as far as the budget goes."""
    def score(batch, budget, max_workers, verbose):
        def one(r) -> bool:
            yl.note_request()
            yl.note_request()
            scored.append(r.ticker)
            r.num_failed, r.score_composite = verdicts[r.ticker]
            r.num_passed = 9 - r.num_failed
            return False
        return yl.run_gated(batch, one, budget=budget, workers=1)
    return score


def test_screen_universe() -> None:
    section("screen_universe: one budget per run, the rest next run")
    pre = {"AAA": 0, "BBB": 1, "CCC": sc.PREFILTER_MAX_FAILED,
           "DDD": sc.PREFILTER_MAX_FAILED + 1, "FFF": 2}
    verdicts = {"AAA": (0, 70.0), "BBB": (1, 80.0), "CCC": (4, 50.0),
                "FFF": (0, 75.0)}

    def fake_prefilter(t):
        yl.note_request()
        if t == "EEE":
            return sc.ScreenResult(ticker=t, error="no info")
        return sc.ScreenResult(ticker=t, name=t, prefilter_failed=pre[t])

    scored: list = []
    score = _fake_scorer(verdicts, scored)
    universe = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    with tempfile.TemporaryDirectory() as d, \
            patched(sc, _SCREEN_CACHE_PATH=Path(d) / "screen.json",
                    fetch_sp500_sp400=lambda verbose: list(universe),
                    prefilter_one=fake_prefilter,
                    _request_budget=lambda: 8):
        first = sc.screen_universe(score, max_workers=1, verbose=False)
        check(scored, ["AAA"],
              "run 1: six prefilter requests leave room to score one name, "
              "the one with the fewest misses")
        check(([r.ticker for r in first["passed"]], first["pending"],
               first["unscreened"]), (["AAA"], 3, ["EEE"]),
              "it shows what it has and counts what is left")
        cached = json.loads((Path(d) / "screen.json").read_text())
        check([r["ticker"] for r in cached["to_score"]], ["BBB", "FFF", "CCC"],
              "the rest wait in the cache, fewest misses first")

        second = sc.screen_universe(score, max_workers=1, verbose=False)
        check(scored, ["AAA", "BBB", "FFF", "CCC"],
              "run 2 skips the prefilter and scores the rest")
        check(([r.ticker for r in second["passed"]],
               [r.ticker for r in second["near_miss"]], second["pending"]),
              (["FFF", "AAA"], ["BBB"], 0),
              "the table now covers the whole shortlist, best Composite first")

        third = sc.screen_universe(lambda *a, **k: scored.append("again"),
                                   verbose=False)
        check((third["from_cache"], "again" in scored,
               [r.ticker for r in third["passed"]]),
              (True, False, ["FFF", "AAA"]), "a finished scan is read back")

        cached = json.loads((Path(d) / "screen.json").read_text())
        old_format = {k: cached[k] for k in ("date", "scoring", "scanned_at",
                                             "universe_size", "unscreened")}
        old_format["passed"] = cached["shown"][:1]
        old_format["near_miss"] = []
        (Path(d) / "screen.json").write_text(json.dumps(old_format))
        state = sc._load_screen_state(verbose=False)
        check((len(state["shown"]), state["to_score"], state["to_prefilter"]),
              (1, [], []), "a cache from before scans were spread out loads "
                           "as a finished scan")

        cached["scoring"] = None              # a scan from the old screener
        (Path(d) / "screen.json").write_text(json.dumps(cached))
        check(sc._load_screen_state(verbose=False), None,
              "a cache scored any other way is not today's scan")


def test_screen_universe_refusal() -> None:
    section("screen_universe: a refusal mid-prefilter resumes next run")
    refuse = {"RL"}

    def fake_prefilter(t):
        if t in refuse:
            refuse.discard(t)                 # refused once, fine next run
            return sc.ScreenResult(ticker=t, error=RATE_LIMITED)
        return sc.ScreenResult(ticker=t, name=t, prefilter_failed=0)

    scored: list = []
    score = _fake_scorer({t: (0, 60.0) for t in ("AAA", "RL", "ZZZ")}, scored)
    with tempfile.TemporaryDirectory() as d, \
            patched(sc, _SCREEN_CACHE_PATH=Path(d) / "screen.json",
                    fetch_sp500_sp400=lambda verbose: ["AAA", "RL", "ZZZ"],
                    prefilter_one=fake_prefilter,
                    _request_budget=lambda: 1000):
        first = sc.screen_universe(score, max_workers=1, verbose=False)
        check((scored, first["pending"], first["refused"]), ([], 3, True),
              "run 1 stops at the refusal and says so: nothing scored, "
              "nothing lost")
        second = sc.screen_universe(score, max_workers=1, verbose=False)
        check((sorted(scored), second["pending"], len(second["passed"]),
               second["refused"]), (["AAA", "RL", "ZZZ"], 0, 3, False),
              "run 2 prefilters the rest and scores all three")


def test_add_screened_names() -> None:
    section("add_screened_names: the scan's additions join this run's report")

    def pa(ticker: str) -> ap.PositionAnalysis:
        return ap.PositionAnalysis(ticker=ticker, name=ticker, shares=0,
                                   statement_market_value=0,
                                   statement_pct_portfolio=0)

    on_list, pinned, kept, dup = pa("ONLIST"), pa("PINNED"), pa("KEPT"), pa("DUP")
    groups = {"Screening": [dup], "Tech": [on_list],
              ap.RECENTLY_HELD_GROUP: [pinned, kept]}
    ran: list = []

    def analyze(rows):
        ran.extend(r["ticker"] for r in rows)
        return [pa(r["ticker"]) for r in rows]

    landed = [{"ticker": t, "name": t}
              for t in ("ONLIST", "PINNED", "NEW", "DUP")]
    added = ap.add_screened_names(
        groups, landed, analyze=analyze,
        analyzed={"ONLIST": on_list, "PINNED": pinned, "KEPT": kept})
    check(ran, ["NEW"], "only a name this run hasn't analyzed is analyzed")
    check([p.ticker for p in groups["Screening"]],
          ["DUP", "ONLIST", "PINNED", "NEW"],
          "the rest reuse their analysis; one already listed isn't doubled")
    check((groups["Screening"][1] is on_list, [p.ticker for p in added]),
          (True, ["ONLIST", "PINNED", "NEW"]), "the very same analysis object")
    check([p.ticker for p in groups[ap.RECENTLY_HELD_GROUP]], ["KEPT"],
          "a pinned name moves off the recently-held group")

    groups = {ap.RECENTLY_HELD_GROUP: [pinned]}
    ap.add_screened_names(groups, [{"ticker": "PINNED", "name": "P"}],
                          analyze=analyze, analyzed={"PINNED": pinned})
    check(sorted(groups), ["Screening"],
          "an emptied recently-held group goes; a missing Screening group is made")
    check(ap.add_screened_names({}, [], analyze=analyze, analyzed={}), [],
          "nothing landed, nothing to do")


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
    check("Screened 898 of 900 tickers against the 9-filter quality framework "
          "(2 could not be fetched from Yahoo)." in html,
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

    html = ap._render_screening_section(dict(sr, pending=415), {})
    check("Screening 900 tickers against the 9-filter quality framework; 415 "
          "are still to go, and the next runs fill them in. So far" in html,
          True, "a scan still in progress says so")


# -------------------------------------------------------------------- main ----

def main() -> int:
    for t in (test_rate_limit_detection, test_budget, test_run_gated,
              test_quote_summary, test_prefilter_one, test_prefilter_universe,
              test_scorer_matches_the_holding, test_scorer_refusals,
              test_growth_cache_guard, test_screen_universe,
              test_screen_universe_refusal, test_add_screened_names, test_split,
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
