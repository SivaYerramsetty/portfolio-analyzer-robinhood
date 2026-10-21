"""
test_scorecard.py
-----------------
Tests for the Report Scorecard: the daily verdict log, the closes that grade
it (scorecard.py), and the analyzer's side of it — building each day's calls,
the run-time orchestration and the rendered section (analyze_portfolio.py).

The policy under test: one call per ticker per day, the day's last run
winning; entry and exit are official closes on one split-adjusted basis, and
a split between fetches rescales the stored history rather than reading as a
crash; pruned names keep being priced, so the weakest calls still count;
nothing is fetched while Yahoo is refusing; and a suggestion appears only
once enough non-overlapping periods back it.

Hermetic: no network, no real ledger or log file. Run it directly —

    ./venv/bin/python test_scorecard.py

Exit code is 0 when everything passes, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import analyze_portfolio as ap
import scorecard as sc
import yahoo_limits as yl
from test_base_score_mode import _mode_env, _scored, _tag_errors

ET = ZoneInfo("America/New_York")

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


def run_all(items, fn):
    """A runner with no budget: every item, in order. Returns those refused."""
    return [it for it in items if fn(it)]


def weekdays(start: str, n: int) -> list[str]:
    d, out = date.fromisoformat(start), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def call(label: str, score, *, mode: str = "composite", held: int = 0,
         **factors) -> dict:
    return {"p": 100.0, "h": held, "v": {mode: [label, score]}, "f": factors}


def watch_label(score: float) -> str:
    for lo, label in ((75, "BUY"), (60, "WATCH"), (42, "WAIT")):
        if score >= lo:
            return label
    return "PASS"


def synthetic_log(n_days: int = 140, n_tickers: int = 20, *,
                  start: str = "2026-01-05") -> dict:
    """A log where the composite score orders stocks perfectly (ticker i
    drifts up i-10 basis points a day), the quality score orders them
    backwards, and the benchmark is flat, so alpha is the stock's return.
    Growth carries the same signal as the composite; value the reverse."""
    days = weekdays(start, n_days + 70)
    log = sc.empty_log()
    for i in range(n_tickers):
        t = f"T{i:02d}"
        price, series = 100.0, {}
        for d in days:
            series[d] = round(price, 4)
            price *= 1 + (i - n_tickers / 2) * 0.001
        log["closes"][t] = series
    log["closes"][sc.BENCHMARK] = {d: 4000.0 for d in days}
    for d in days[:n_days]:
        calls = {}
        for i in range(n_tickers):
            comp = 40 + 2.5 * i
            qual = 90 - 2.5 * i
            calls[f"T{i:02d}"] = {
                "p": 100.0, "h": 0,
                "v": {"composite": [watch_label(comp), comp],
                      "quality": [watch_label(qual), qual]},
                "f": {"g": comp, "v": qual, "q": 50.0},
            }
        sc.record_day(log, d, calls)
    return log


# -------------------------------------------------------------- the log ----

def test_compact_factors() -> None:
    section("compact_factors: the ledger's factor names in short keys")
    got = sc.compact_factors({"score_quality": 58.84, "score_growth": 9.6,
                              "week52_position": 91.0, "trend": "uptrend",
                              "sector_momentum": "Cool", "verdicts": {"x": 1},
                              "upside_pct": None, "composite_coverage": 1.0},
                             news_score=0.4567)
    check(got, {"q": 58.8, "g": 9.6, "w52": 91.0, "sm": -1, "n": 0.46},
          "mapped, rounded, None and unmapped keys dropped")
    check(sc.compact_factors(None), {}, "no factors, no keys")


def test_record_day() -> None:
    section("record_day: the day's last run wins, per ticker")
    log = sc.empty_log()
    check(sc.record_day(log, "2026-10-06", {"AAA": call("BUY", 80),
                                            "BBB": call("WAIT", 50)}), 2,
          "returns how many calls it wrote")
    sc.record_day(log, "2026-10-06", {"AAA": call("WATCH", 70)})
    day = log["days"]["2026-10-06"]
    check((day["AAA"]["v"]["composite"], day["BBB"]["v"]["composite"]),
          (["WATCH", 70], ["WAIT", 50]),
          "a later run replaces its own tickers and keeps the rest")
    sc.record_day(log, "2026-10-05", {"CCC": call("PASS", 30)})
    check(log["started"], "2026-10-05", "started tracks the earliest day")
    check(sc.record_day(log, "2026-10-07", {}), 0, "nothing to write")
    check("2026-10-07" in log["days"], False, "and no empty day appears")


def test_seed_from_ledger() -> None:
    section("seed_from_ledger: the ledger's first sightings, once")
    ledger = {"tickers": {
        "OLD": {"first_date": "2026-06-23", "first_price": 93.4,
                "first_verdict": "WATCH", "first_verdict_score": 66.0,
                "first_alloc": 0.0,
                "first_factors": {"score_quality": 53.4, "sector_momentum": "Hot"}},
        "NEW": {"first_date": "2026-09-22", "first_price": 10.0,
                "first_verdict": "ADD", "first_alloc": 3.0,
                "first_factors": {"verdicts": {"composite": ["ADD", 80.0],
                                               "quality": ["HOLD", 70.0]}}},
        "NOPRICE": {"first_date": "2026-06-23", "first_verdict": "BUY"},
        "LOGGED": {"first_date": "2026-09-22", "first_price": 5.0,
                   "first_verdict": "PASS"},
    }}
    log = sc.empty_log()
    sc.record_day(log, "2026-09-22", {"LOGGED": call("BUY", 90)})
    check(sc.seed_from_ledger(log, ledger, "2026-10-06"), 2,
          "two first sightings recovered")
    old = log["days"]["2026-06-23"]["OLD"]
    check(old, {"p": 93.4, "h": 0, "v": {"composite": ["WATCH", 66.0]},
                "f": {"q": 53.4, "sm": 1}, "s": 1},
          "an entry from before the modes is filed under composite")
    check(log["days"]["2026-09-22"]["NEW"]["v"],
          {"composite": ["ADD", 80.0], "quality": ["HOLD", 70.0]},
          "a newer entry keeps every mode's verdict")
    check(log["days"]["2026-09-22"]["NEW"]["h"], 1, "held when first seen")
    check(log["days"]["2026-09-22"]["LOGGED"]["v"]["composite"], ["BUY", 90],
          "a logged call is never overwritten by a seed")
    check(sc.seed_from_ledger(log, ledger, "2026-10-07"), 0, "and it runs once")
    blank = sc.empty_log()
    check(sc.seed_from_ledger(blank, None, "2026-10-06"), 0,
          "no ledger, nothing to seed")
    check(sc.seed_from_ledger(blank, ledger, "2026-10-07"), 3,
          "and the next run that can read one still seeds (all three priced "
          "entries, in a log with nothing logged yet)")


def test_prune_and_io() -> None:
    section("prune_log, save_log and load_log")
    log = sc.empty_log()
    sc.record_day(log, "2025-01-02", {"GONE": call("BUY", 80)})
    sc.record_day(log, "2026-09-01", {"KEPT": call("BUY", 80)})
    log["closes"] = {"GONE": {"2025-01-02": 1.0},
                     "KEPT": {"2026-07-01": 1.0, "2026-08-25": 2.0},
                     sc.BENCHMARK: {"2026-08-25": 3.0}}
    log["fetched"] = {"GONE": "2025-01-02", "KEPT": "2026-10-05"}
    sc.prune_log(log, date(2026, 10, 6))
    check(sorted(log["days"]), ["2026-09-01"], "calls past retention dropped")
    check(log["closes"], {"KEPT": {"2026-08-25": 2.0},
                          sc.BENCHMARK: {"2026-08-25": 3.0}},
          "and closes no remaining call can use")
    check(log["fetched"], {"KEPT": "2026-10-05"}, "and stale fetch stamps")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "verdict_log.json")
        check(sc.load_log(path), sc.empty_log(), "a missing file loads empty")
        sc.save_log(log, path)
        check(sc.load_log(path), log, "a saved log loads back unchanged")
        check(" " in Path(path).read_text(), False, "written compactly")
        check(os.listdir(tmp), ["verdict_log.json"], "no temp file left behind")
        Path(path).write_text("{not json")
        check(sc.load_log(path), sc.empty_log(), "a corrupt file loads empty")


# --------------------------------------------------------------- closes ----

def test_last_session() -> None:
    section("last_session: the latest close that is final")
    at = lambda s: datetime.fromisoformat(s).replace(tzinfo=ET)
    check(sc.last_session(at("2026-10-06T16:40")), "2026-10-06",
          "after 16:30 on a weekday: today")
    check(sc.last_session(at("2026-10-06T15:00")), "2026-10-05",
          "during the session: yesterday")
    check(sc.last_session(at("2026-10-05T07:00")), "2026-10-02",
          "Monday morning: last Friday")
    check(sc.last_session(at("2026-10-04T18:00")), "2026-10-02",
          "a Sunday: last Friday")


def test_closes_wanted() -> None:
    section("closes_wanted: what each series still lacks")
    log = sc.empty_log()
    sc.record_day(log, "2026-06-23", {"OLD": call("BUY", 80)})
    sc.record_day(log, "2026-10-01", {"NEW": call("BUY", 80),
                                      "INC": call("WAIT", 50)})
    log["closes"] = {
        "OLD": {"2026-06-16": 1.0, "2026-10-02": 1.0},   # covers its last exit
        "INC": {"2026-09-20": 1.0, "2026-10-01": 1.0},
    }
    wanted = sc.closes_wanted(log, "2026-10-05")
    check(wanted, {"NEW": "2026-09-24", "INC": "2026-09-24",
                   sc.BENCHMARK: "2026-06-16"},
          "new series from a week before the first call; an existing one "
          "with a week's overlap; a covered one not at all")
    log["fetched"] = {"NEW": "2026-10-05"}
    check("NEW" in sc.closes_wanted(log, "2026-10-05"), False,
          "a series fetched this session isn't asked for again")
    check("NEW" in sc.closes_wanted(log, "2026-10-06"), True,
          "but is the next session")
    check(sc.closes_wanted(sc.empty_log(), "2026-10-05"), {}, "empty log")

    gone = sc.empty_log()
    sc.record_day(gone, "2026-09-01", {"DEAD": call("WAIT", 50),
                                       "NONE": call("WAIT", 50)})
    gone["closes"] = {"DEAD": {"2026-08-25": 5.0, "2026-09-10": 4.0}}
    gone["fetched"] = {"DEAD": "2026-09-30", "NONE": "2026-09-20",
                       sc.BENCHMARK: "2026-09-30"}
    check(sorted(sc.closes_wanted(gone, "2026-10-05")), [sc.BENCHMARK],
          "a series a fetch 14+ days on couldn't extend is given up on")
    gone["fetched"] = {"DEAD": "2026-09-15", "NONE": "2026-09-10"}
    check(sorted(sc.closes_wanted(gone, "2026-10-05")),
          ["DEAD", "NONE", sc.BENCHMARK],
          "but one last fetched soon after its last close is retried, so an "
          "outage doesn't read as delisting")


def test_merge_closes() -> None:
    section("merge_closes: splits rescale the stored history")
    stored = {"2026-09-01": 200.0, "2026-09-02": 210.0}
    merged, ratio = sc.merge_closes(stored, {"2026-09-02": 210.5,
                                             "2026-09-03": 212.0})
    check((ratio, merged["2026-09-01"], merged["2026-09-03"]),
          (None, 200.0, 212.0), "no split: fetched values win, nothing rescaled")
    merged, ratio = sc.merge_closes(stored, {"2026-09-02": 105.0,
                                             "2026-09-03": 106.0})
    check((ratio, merged), (0.5, {"2026-09-01": 100.0, "2026-09-02": 105.0,
                                  "2026-09-03": 106.0}),
          "a 2-for-1 split halves the stored closes")
    check(stored["2026-09-01"], 200.0, "the input is left alone")
    check(sc.merge_closes(None, {"2026-09-03": 1.0}),
          ({"2026-09-03": 1.0}, None), "no stored series")


def test_refresh_closes() -> None:
    section("refresh_closes: final closes only, refusals left for later")
    log = sc.empty_log()
    sc.record_day(log, "2026-10-01", {"AAA": call("BUY", 80),
                                      "RRR": call("WAIT", 50)})
    now = datetime(2026, 10, 6, 15, 0, tzinfo=ET)        # session = Oct 5
    asked = []

    def fetch(ticker, start):
        asked.append((ticker, start))
        if ticker == "RRR":
            return None, True
        return {"2026-10-01": 10.0, "2026-10-05": 11.0, "2026-10-06": 99.0}, False

    msgs = []
    got = sc.refresh_closes(log, now, fetch, run_all, log_fn=msgs.append)
    check(got, {"wanted": 3, "fetched": 2, "left": 1, "splits": {}},
          "two series updated, the refused one left")
    check(log["closes"]["AAA"], {"2026-10-01": 10.0, "2026-10-05": 11.0},
          "a bar for the session still trading is not stored")
    check(log["fetched"], {"AAA": "2026-10-05", sc.BENCHMARK: "2026-10-05"},
          "fetched series are stamped; the refused one isn't")
    check(sorted(t for t, _ in asked), ["AAA", "RRR", sc.BENCHMARK],
          "each wanted series asked once")

    asked.clear()
    sc.refresh_closes(log, now, fetch, run_all, log_fn=msgs.append)
    check([t for t, _ in asked], ["RRR"], "the next run retries only the refused one")

    def split_fetch(ticker, start):
        return {"2026-10-05": 5.5, "2026-10-06": 5.6}, False

    later = datetime(2026, 10, 6, 17, 0, tzinfo=ET)       # session = Oct 6
    got = sc.refresh_closes(log, later, split_fetch, run_all, log_fn=msgs.append)
    check(log["closes"]["AAA"], {"2026-10-01": 5.0, "2026-10-05": 5.5,
                                 "2026-10-06": 5.6},
          "after a 2-for-1 split the whole series is on the new basis")
    check(any("AAA: rescaled stored closes by 0.5" in m for m in msgs), True,
          "and the run log says so")


# ----------------------------------------------------------- evaluation ----

def test_evaluate_calls() -> None:
    section("evaluate_calls: entry close, exit close, alpha")
    log = sc.empty_log()
    sc.record_day(log, "2026-09-01", {"AAA": call("BUY", 80)})
    sc.record_day(log, "2026-09-05", {"AAA": call("BUY", 80)})     # a Saturday
    sc.record_day(log, "2026-10-05", {"AAA": call("BUY", 80)})     # no close yet
    sc.record_day(log, "2026-09-02", {"GAP": call("WAIT", 50)})
    log["closes"] = {
        "AAA": {"2026-09-01": 100.0, "2026-09-08": 110.0, "2026-09-09": 111.0,
                "2026-09-15": 115.0, "2026-10-01": 120.0, "2026-10-02": 121.0},
        "GAP": {"2026-09-02": 50.0, "2026-09-30": 60.0},
        sc.BENCHMARK: {"2026-09-01": 1000.0, "2026-09-08": 1010.0,
                       "2026-09-15": 1020.0, "2026-10-01": 1050.0,
                       "2026-10-02": 1050.0},
    }
    ev = {(c["date"], c["ticker"]): c for c in sc.evaluate_calls(log)}
    first = ev[("2026-09-01", "AAA")]
    check(first["ret"], {"1W": 10.0, "1M": 20.0},
          "1W exits Sep 8 (day 7), 1M Oct 1 (day 30); 3M hasn't matured")
    check(first["alpha"], {"1W": 9.0, "1M": 15.0}, "alpha nets out the S&P")
    check(ev[("2026-09-05", "AAA")]["ret"]["1W"], round((115 / 110 - 1) * 100, 3),
          "a weekend call enters at the next close")
    check(("2026-10-05", "AAA") in ev, False,
          "a call with no entry close yet is pending, not graded")
    check(ev[("2026-09-02", "GAP")]["ret"], {},
          "an exit more than the grace period late gives no return")


def test_statistics() -> None:
    section("spearman, independent_windows, rank_ic")
    check(round(sc.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 6), 1.0, "perfect")
    check(round(sc.spearman([1, 2, 3, 4], [4, 3, 2, 1]), 6), -1.0, "reversed")
    check(round(sc.spearman([1, 1, 2, 2], [1, 2, 3, 4]), 4), 0.8944,
          "ties share their average rank")
    check(sc.spearman([1, 1, 1], [1, 2, 3]), None, "no spread, no correlation")
    days = weekdays("2026-01-05", 60)
    check(sc.independent_windows(days, 30), 3,
          "60 trading days of daily calls are 3 independent 1-month looks")
    check(sc.independent_windows(days, 7), 12, "and 12 weekly ones")
    small = {"2026-01-05": [(1, 1), (2, 2), (3, 3)]}
    check(sc.rank_ic(small, 7)["ic"], None,
          f"a day with fewer than {sc.MIN_IC_CALLS} calls doesn't count")
    pairs = {d: [(i, i + (k % 3)) for i in range(10)]
             for k, d in enumerate(days[:30])}
    ic = sc.rank_ic(pairs, 7)
    check((ic["days"], ic["windows"], ic["n"], ic["ic"] > 0.9,
           ic["t"] is not None and ic["t"] > 2),
          (30, 6, 300, True, True), "days, windows, calls and a big t")


def test_build_scorecard() -> None:
    section("build_scorecard: labels, tiers, buckets, ranking, bases, inputs")
    log = synthetic_log()
    card = sc.build_scorecard(log, ("composite", "quality"), "composite",
                              {"composite": "Composite", "quality": "Quality"})
    meta = card["meta"]
    check((meta["days_logged"], meta["calls_logged"], meta["seed_calls"]),
          (140, 2800, 0), "every logged day and call counted")
    check(meta["matured"]["1M"] > 0 and meta["matured"]["3M"] > 0, True,
          "the longer horizons mature inside the synthetic window")
    comp, qual = card["modes"]["composite"], card["modes"]["quality"]
    check((comp["ic"]["1M"]["ic"] > 0.95, qual["ic"]["1M"]["ic"] < -0.95),
          (True, True), "a score that orders stocks has rank IC near +1, a "
                        "reversed one near -1")
    check(sc.significant(comp["ic"]["1M"]), True, "and it is significant")
    t = comp["tiers"]["1M"]
    check((t["buy"]["alpha"] > 0 > t["below"]["alpha"], t["spread"] > 0),
          (True, True), "buy-grade beat the S&P and below-the-bar lagged")
    check(list(comp["labels"]), ["BUY", "WATCH", "WAIT", "PASS"],
          "labels in display order")
    check(comp["buckets"]["80+"]["1M"]["alpha"]
          > comp["buckets"]["<40"]["1M"]["alpha"] if "<40" in comp["buckets"]
          else comp["buckets"]["80+"]["1M"]["alpha"]
          > comp["buckets"]["40–50"]["1M"]["alpha"], True,
          "the top bucket beat the bottom one")
    check(card["factors"]["g"]["1M"]["ic"] > 0.95, True, "growth tracks the score")
    check(card["factors"]["v"]["1M"]["ic"] < -0.95, True, "value runs backwards")
    check(card["factors"]["q"]["1M"]["ic"], None, "a constant input has no IC")
    check(card["compare_n"] > 0, True, "both bases graded on the same calls")
    check(comp["buy_share"], 0.3, "6 of 20 names rated buy-grade lately")


def test_suggestions() -> None:
    section("suggest: rules that need enough data")
    labels = {"composite": "Composite", "quality": "Quality"}
    log = sc.empty_log()
    sc.record_day(log, "2026-10-01", {"AAA": call("BUY", 80)})
    early = sc.build_scorecard(log, ("composite",), "composite", labels)
    check([tone for tone, _ in early["suggestions"]["composite"]], ["neutral"],
          "with nothing matured there is one note")
    check("Not enough matured calls" in early["suggestions"]["composite"][0][1],
          True, "saying why")

    card = sc.build_scorecard(synthetic_log(), ("composite", "quality"),
                              "composite", labels)
    texts = dict((text.split(":")[0], tone)
                 for tone, text in card["suggestions"]["composite"])
    joined = " ".join(t for _, t in card["suggestions"]["composite"])
    check(texts.get("Following the calls has paid"), "good",
          "buy-grade beating below-the-bar is called out")
    check(texts.get("Higher Composite scores have meant better returns"), "good",
          "so is a working score")
    check("Composite ranks stocks best of the 2 bases" in joined, True,
          "the best base, when it's the one being viewed")
    check("Growth has carried real signal over 1 month" in joined, True,
          "a strong input is a candidate for more weight")
    check("Value has pointed the wrong way over 1 month" in joined, True,
          "an input running backwards is flagged")
    q_joined = " ".join(t for _, t in card["suggestions"]["quality"])
    check("Composite ranks the same stocks better than Quality" in q_joined, True,
          "viewing a worse base points at the better one")
    check("It is the saved default." in q_joined, True,
          "and notes when the better one is already the default")
    card_q = sc.build_scorecard(synthetic_log(), ("composite", "quality"),
                                "quality", labels)
    check("It is a candidate for the default base." in
          " ".join(t for _, t in card_q["suggestions"]["quality"]), True,
          "or offers it as a candidate when it isn't")
    check("Higher Quality scores have meant worse returns" in q_joined, True,
          "and says its own ordering is backwards")

    # The breadth guard: a better-ranking base that rates too many names a buy.
    wide = synthetic_log()
    for d in wide["days"]:
        for c in wide["days"][d].values():
            c["v"]["composite"][0] = "BUY"
    card = sc.build_scorecard(wide, ("composite", "quality"), "quality", labels)
    q_joined = " ".join(t for _, t in card["suggestions"]["quality"])
    check("rates 100% of names buy-grade" in q_joined, True,
          "a base rating every name a buy isn't offered as the default")


def test_digest() -> None:
    section("digest: the scorecard as text for the AI brief")
    card = sc.build_scorecard(synthetic_log(), ("composite", "quality"),
                              "composite", {"composite": "Composite",
                                            "quality": "Quality"})
    text = sc.digest(card, "composite", {"composite": "Composite",
                                         "quality": "Quality"})
    check(text.startswith("Calls logged daily since 2026-01-05 (140 days, "
                          "2800 calls)"), True, "leads with the log's size")
    check("Bases by 1M rank IC on the same calls: Composite +1.00, Quality "
          "-1.00." in text, True, "ranks the bases best first")
    check("Rule-based finding (good): Following the calls has paid" in text,
          True, "and carries the findings")
    prompt = ap._build_miss_analysis_prompt([], [], {}, scorecard_text=text)
    check("REPORT SCORECARD" in prompt and text in prompt, True,
          "the AI post-mortem's brief includes it")
    check("REPORT SCORECARD" in ap._build_miss_analysis_prompt([], [], {}), False,
          "and leaves it out when there is none")


# ------------------------------------------------------------- analyzer ----

def test_scorecard_calls() -> None:
    section("scorecard_calls: what the analyzer logs")
    all_pass = [1.0] * 9
    with _mode_env():
        held = _scored("HHH", 80, all_pass, upside_pct=12.0)
        watched = _scored("WWW", 60, all_pass, shares=0, value=0.0)
        broken = _scored("ERR", 60, all_pass, shares=0, value=0.0)
        broken.error = "boom"
        etf = ap.PositionAnalysis(ticker="ETF", name="ETF", shares=1,
                                  statement_market_value=1,
                                  statement_pct_portfolio=1, bucket="thematic",
                                  current_price=10.0,
                                  verdict=ap.Verdict(label="HOLD", color="",
                                                     reason=""))
        held.news_sentiment = {"score": 0.6, "as_of": ap._et_date_iso()}
        watched.news_sentiment = {"score": -0.7, "as_of": "2026-01-02"}
        calls = ap.scorecard_calls([held, etf], {"Tech": [watched, broken]})
    check(sorted(calls), ["HHH", "WWW"],
          "a failed analysis and an ETF verdict aren't logged")
    h = calls["HHH"]
    check((h["h"], sorted(h["v"]) == sorted(ap.BASE_SCORE_MODES)), (1, True),
          "a holding, with every mode's verdict")
    check(h["v"]["composite"], [held.verdicts["composite"].label,
                                round(held.verdicts["composite"].score, 1)],
          "as [label, score]")
    check((h["f"]["c"], h["f"]["up"], h["f"]["n"]), (80.0, 12.0, 0.6),
          "with its inputs, today's news read included")
    check("n" in calls["WWW"]["f"], False, "a carried-over news read is not")


def test_update_scorecard() -> None:
    section("update_scorecard: one run, end to end, offline")
    all_pass = [1.0] * 9
    with _mode_env():
        held = _scored("HHH", 80, all_pass)
    ledger = {"tickers": {"OLD": {"first_date": "2026-06-23", "first_price": 10.0,
                                  "first_verdict": "BUY",
                                  "first_verdict_score": 80.0}}}
    asked = []

    def fetch(ticker, start):
        asked.append(ticker)
        return {"2026-06-23": 10.0, "2026-07-01": 11.0, "2026-10-05": 12.0}, False

    now = datetime(2026, 10, 6, 15, 0, tzinfo=ET)
    with tempfile.TemporaryDirectory() as tmp, _mode_env():
        path = os.path.join(tmp, "verdict_log.json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            card = ap.update_scorecard([held], None, ledger, run_date="2026-10-06",
                                       now_et=now, path=path, fetch=fetch)
        log = json.loads(Path(path).read_text())
        check(sorted(log["days"]), ["2026-06-23", "2026-10-06"],
              "the ledger's first sighting and today's calls are on disk")
        check(sorted(asked), ["HHH", "OLD", sc.BENCHMARK], "closes fetched")
        check(card["meta"]["matured"]["1W"], 1, "the recovered call is graded")
        check(held._turned_buy, {}, "and today's names are checked for a turn")
        text = out.getvalue()
        check(("Recovered 1 first-sighting call" in text,
               "Logged 1 call(s) for 2026-10-06" in text,
               "Closes: 3/3 series updated" in text), (True, True, True),
              "the run log says what happened")

        asked.clear()
        with patched(yl, refusals=lambda: 1), \
                contextlib.redirect_stdout(io.StringIO()) as out2:
            ap.update_scorecard([held], None, ledger, run_date="2026-10-07",
                                now_et=now + timedelta(days=1), path=path,
                                fetch=fetch)
        check(asked, [], "nothing is fetched once Yahoo has refused this run")
        check("fetching closes next run" in out2.getvalue(), True, "and it says so")


def _labels_log(rows: dict[str, dict[str, str]], *,
                seeded: tuple = ()) -> dict:
    """A log from {day: {ticker: label}}, each label under composite only,
    or {day: {ticker: {mode: label}}}. Tickers in `seeded` are flagged as
    recovered first sightings."""
    log = sc.empty_log()
    for d, calls in rows.items():
        for t, label in calls.items():
            by_mode = label if isinstance(label, dict) else {"composite": label}
            entry = {"p": 100.0, "h": 0, "f": {},
                     "v": {m: [lab, 70.0] for m, lab in by_mode.items()}}
            if t in seeded:
                entry["s"] = 1
            log["days"].setdefault(d, {})[t] = entry
    return log


def test_turned_buy_grade() -> None:
    section("turned_buy_grade: buy-grade now, something else at an earlier close")
    today = "2026-10-21"                       # a Wednesday; the window opens Oct 15
    week = ("2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
            "2026-10-19", "2026-10-20")
    rows = {d: {} for d in ("2026-10-08", *week)}
    rows["2026-10-08"]["FAR"] = "HOLD"
    for d in week:
        rows[d]["ALWAYS"] = "ADD"
        rows[d]["EDGE_IN"] = "HOLD" if d < "2026-10-15" else "ADD"
        rows[d]["EDGE_OUT"] = "HOLD" if d < "2026-10-14" else "ADD"
    rows["2026-10-20"]["TODAY"] = "HOLD"
    rows["2026-10-16"]["WEEK"] = "WATCH"
    rows["2026-10-19"]["WEEK"] = rows["2026-10-20"]["WEEK"] = "BUY"
    rows["2026-10-19"]["FRESH"] = rows["2026-10-20"]["FRESH"] = "BUY"
    rows["2026-10-16"]["GAPPY"] = "HOLD"
    rows["2026-10-16"]["SEED"] = "WATCH"
    rows["2026-10-15"]["BOUGHT"] = "WATCH"
    for d in ("2026-10-16", "2026-10-19", "2026-10-20"):
        rows[d]["BOUGHT"] = "BUY"
    rows["2026-10-20"]["MODES"] = {"composite": "HOLD", "quality": "ADD"}
    rows["2026-10-19"]["PARTIAL"] = {"composite": "ADD", "quality": "HOLD"}
    rows["2026-10-20"]["PARTIAL"] = {"composite": "ADD"}
    rows[today] = {"TODAY": "ADD"}             # this run's own call, already logged
    log = _labels_log(rows, seeded=("SEED",))

    now = {t: {"composite": "ADD"} for t in
           ("TODAY", "ALWAYS", "EDGE_IN", "EDGE_OUT", "GAPPY", "FAR")}
    now.update({"WEEK": {"composite": "BUY"}, "FRESH": {"composite": "BUY"},
                "SEED": {"composite": "BUY"}, "BOUGHT": {"composite": "ADD"},
                "HELD": {"composite": "HOLD"},
                "MODES": {"composite": "ADD", "quality": "ADD", "blend": "TRIM"},
                "PARTIAL": {"composite": "ADD", "quality": "ADD"}})
    got = sc.turned_buy_grade(log, today, now)

    check(got.get("TODAY"), {"composite": {"from": "HOLD", "since": today,
                                           "was_on": "2026-10-20", "age_d": 0}},
          "HOLD at yesterday's close, ADD now: turned today")
    check(got.get("WEEK"), {"composite": {"from": "WATCH", "since": "2026-10-19",
                                          "was_on": "2026-10-16", "age_d": 2}},
          "turned Monday and still BUY: dated to Monday, over the weekend")
    check(got.get("EDGE_IN", {}).get("composite", {}).get("age_d"), 6,
          "a run that began on the window's first day still counts")
    check(("ALWAYS" in got, "EDGE_OUT" in got), (False, False),
          "a run older than the window doesn't")
    check(("FRESH" in got, "SEED" in got), (False, False),
          "nor does a name buy-grade on every call we have; a recovered first "
          "sighting isn't a call to change from")
    check(got.get("GAPPY", {}).get("composite", {}).get("was_on"), "2026-10-16",
          "days without a call are skipped back to the last one")
    check("FAR" in got, False,
          "but a last other call more than a week before the run says nothing")
    check(got.get("BOUGHT", {}).get("composite", {}).get("from"), "WATCH",
          "BUY to ADD after buying is one buy-grade run, from the WATCH before it")
    check("HELD" in got, False, "a name that isn't buy-grade now gets nothing")
    check(got.get("MODES"), {"composite": {"from": "HOLD", "since": today,
                                           "was_on": "2026-10-20", "age_d": 0}},
          "each mode against its own history: only composite changed")
    check(got.get("PARTIAL", {}).get("quality", {}).get("was_on"), "2026-10-19",
          "a day logged without a mode is skipped for that mode")
    check("composite" in got.get("PARTIAL", {}), False,
          "while the mode it has is judged as usual")
    check(sc.turned_buy_grade(sc.empty_log(), today, now), {},
          "an empty log flags nothing")


def test_attach_turned_buy() -> None:
    section("attach_turned_buy: the verdict chip, the filter and the header count")
    from test_base_score_mode import _render_report, _row_attrs
    all_pass = [1.0] * 9
    with _mode_env():
        held = _scored("AAA", 85, all_pass, upside_pct=20.0)
        other = _scored("BBB", 85, all_pass, upside_pct=20.0)
        # Twelve equal positions: under 10% each, so no size penalty.
        book = [held, other, *(_scored(f"F{i}", 65, all_pass) for i in range(10))]
        watched = _scored("WWW", 85, all_pass, shares=0, value=0.0,
                          upside_pct=20.0)
        # As in main(): the size overlay lands before the scorecard runs.
        ap.finalize_holding_verdicts(book)
    modes = ap.BASE_SCORE_MODES
    check(({held.verdicts[m].label for m in modes},
           {watched.verdicts[m].label for m in modes}), ({"ADD"}, {"BUY"}),
          "the fixture is ADD / BUY under every mode")
    rows = {"2026-10-16": {"WWW": {m: "WATCH" for m in modes},
                           "AAA": {m: "ADD" for m in modes},
                           "BBB": {m: "ADD" for m in modes}},
            "2026-10-19": {"WWW": {m: "BUY" for m in modes}},
            "2026-10-20": {"WWW": {m: "BUY" for m in modes},
                           "AAA": {m: "ADD" if m.startswith("quality") else "HOLD"
                                   for m in modes},
                           "BBB": {m: "ADD" for m in modes}}}
    out = io.StringIO()
    with _mode_env(), contextlib.redirect_stdout(out):
        turned = ap.attach_turned_buy(_labels_log(rows), book,
                                      {"Tech": [watched]}, "2026-10-21")
    check((sorted(turned), sorted(held._turned_buy), other._turned_buy),
          (["AAA", "WWW"],
           sorted(m for m in modes if not m.startswith("quality")), {}),
          "AAA turned under every base but Quality; BBB never changed")
    check("Turned BUY/ADD this past week (Composite): AAA (was HOLD, since "
          "2026-10-21), WWW (was WATCH, since 2026-10-19)" in out.getvalue(), True,
          "the run log names them under the run's mode")

    with _mode_env():
        page = _render_report(book, {"Tech": [watched]})
    check(("<span class='vturn-chip fresh'>↑ was HOLD</span>" in page,
           "<span class='vturn-chip'>↑ was WATCH</span>" in page),
          (True, True), "a chip on each: solid the day it turned, outlined after")
    check(("Turned ADD today; it was HOLD at the Tue, Oct 20 close." in page,
           "Turned BUY on Mon, Oct 19; it was WATCH at the Fri, Oct 16 close."
           in page), (True, True), "the card and the tap tip say what and when")
    a, b, w = (_row_attrs(page, t) for t in ("AAA", "BBB", "WWW"))
    check((a["turned-buy"], a["turned-buy-composite"], a["turned-buy-quality"],
           w["turned-buy-blend-cal"], b["turned-buy"]),
          ("today", "today", "", "week", ""),
          "the rows carry it per mode for the filter")
    adds = re.search(r"</strong>Add candidates(.*?)</a>", page).group(1)
    check(("↑ 1 new today" in adds, "bmode-composite" in adds,
           "bmode-quality" in adds), (True, True, False),
          "Add candidates counts the new ones, in every view but Quality's")
    buys = re.search(r"</strong>Watchlist BUY signals(.*?)</a>", page).group(1)
    check(("↑ 1 new this week" in buys, "applyHeaderFilter('turned-buy')" in buys),
          (True, True), "Watchlist BUY signals too, and the count is a filter")
    script = re.search(r"/\* ---------- Filter bar:.*?\n\}\)\(\);", page, re.S).group(0)
    check(("attr:'turned-buy'" in script, "key:'turned-buy'" in script,
           "'turned-buy'];" in page), (True, True, True),
          "a Turned BUY / ADD card, its quick screen, and the base switch copies it")
    check(_tag_errors(page), [], "the page's tags balance")

    with _mode_env():
        quiet = _render_report(book[1:], None)
    check(("vturn" in quiet.split("</style>", 1)[1], "stat-turned'" in quiet),
          (False, False), "no change, no chip and no count")


def test_render_section() -> None:
    section("_render_scorecard_section and the full report")
    labels = ap.BASE_SCORE_LABELS
    check(ap._render_scorecard_section(None), "", "no scorecard, no section")

    log = sc.empty_log()
    sc.record_day(log, "2026-10-06", {"AAA": call("BUY", 80)})
    empty = ap._render_scorecard_section(
        sc.build_scorecard(log, ap.BASE_SCORE_MODES, "composite", labels))
    check(("id='scorecard'" in empty, "No call has matured yet" in empty,
           "Oct 13, 2026" in empty), (True, True, True),
          "before anything matures: the date the first results land")

    log = synthetic_log()
    for d in log["days"]:
        for c in log["days"][d].values():
            c["v"] = {m: list(c["v"]["composite"]) for m in ap.BASE_SCORE_MODES}
    card = sc.build_scorecard(log, ap.BASE_SCORE_MODES, "composite", labels)
    html = ap._render_scorecard_section(card)
    for title in ("How each verdict did", "Do higher scores do better?",
                  "Which base ranks stocks best", "Which inputs carry signal",
                  "What the data suggests"):
        check(title in html, True, f"has '{title}'")
    check(_tag_errors(html), [], "the section's tags balance")
    check("data-verdict" in html, False,
          "its rows stay out of the filter bar (no data-verdict)")
    check(html.count("<tr><td data-sort='Composite'>Composite <span"), 1,
          "the saved default is starred in the base table")

    with _mode_env():
        holding = _scored("AAA", 80, [1.0] * 9)
    stubs = {"fetch_market_fear_greed": lambda: None,
             "fetch_benchmark_returns": lambda: None,
             "fetch_market_news": lambda: None,
             "_compute_holdings_ytd_return": lambda results: None}
    with patched(ap, **stubs), _mode_env(), \
            contextlib.redirect_stdout(io.StringIO()):
        page = ap.generate_html_report([holding], missed_opportunities=[],
                                       report_scorecard=card)
    check(page.index("id='missed-opps'") < page.index("id='scorecard'"), True,
          "the full report places it after Missed Opportunities")
    check(_tag_errors(page), [], "and the page still balances")


# -------------------------------------------------------------------- main ----

def main() -> int:
    for t in (test_compact_factors, test_record_day, test_seed_from_ledger,
              test_prune_and_io, test_last_session, test_closes_wanted,
              test_merge_closes, test_refresh_closes, test_evaluate_calls,
              test_statistics, test_build_scorecard, test_suggestions,
              test_digest, test_scorecard_calls, test_update_scorecard,
              test_turned_buy_grade, test_attach_turned_buy,
              test_render_section):
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
