"""
test_news_decay.py
------------------
Tests for the news-signal age decay: how much weight a carried-over news read
still gets as it ages, and how that flows into the verdict.

The policy under test (see the news-sentiment section of analyze_portfolio.py):
a Claude news read is kept visible while a refresh is in flight
(stale-while-revalidate), but the ±6 nudge it feeds into the verdict halves
after a trading day and disappears after five — so a news path that stays
broken decays to "no signal" instead of pinning old headlines to every verdict.

Hermetic: no network, no API key, no real cache file. Run it directly —

    ./venv/bin/python test_news_decay.py

Exit code is 0 when everything passes, 1 otherwise.
"""
from __future__ import annotations

import json
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import analyze_portfolio as ap

NY = ZoneInfo("America/New_York")

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


def _pin_today(d: date) -> None:
    """Freeze market-time 'now' at midday on `d`. Both the as_of stamp and the
    cache freshness check read the clock through _et_now, so this pins the whole
    news path to one day."""
    ap._et_now = lambda ts=None, _d=d: (
        datetime(_d.year, _d.month, _d.day, 12, 0, tzinfo=NY) if ts is None
        else datetime.fromtimestamp(ts, NY)
    )


_REAL_ET_NOW = ap._et_now


def _unpin_today() -> None:
    ap._et_now = _REAL_ET_NOW


def _as_of_aged(days: int, today: date) -> str:
    """The latest *weekday* that is exactly `days` trading days before `today`.

    Weekdays only, because a real `as_of` is always a day the scorer ran and the
    crons are weekday-only. Found by searching with _weekdays_between rather
    than by inverting it, so it stays correct even when the suite happens to run
    on a weekend (the arithmetic itself is pinned separately, against hardcoded
    dates, in test_weekdays_between)."""
    d = today
    for _ in range(60):
        if d.weekday() < 5 and ap._weekdays_between(d, today) == days:
            return d.isoformat()
        d -= timedelta(days=1)
    raise AssertionError(f"no weekday is {days} trading days before {today}")


def _news(score: float, as_of: str | None, label: str = "bullish",
          rationale: str = "raised guidance", method: str | None = None) -> dict:
    n = {"score": score, "label": label, "rationale": rationale,
         "headlines": ["h1", "h2"]}
    if as_of is not None:
        n["as_of"] = as_of
    if method is not None:
        n["method"] = method
    return n


# ------------------------------------------------- 1. trading-day distance ----

def test_weekdays_between() -> None:
    section("trading-day distance: weekdays in (start, end]")
    mon, tue, wed, thu, fri = (date(2026, 8, 31) + timedelta(days=i)
                               for i in range(5))
    sat, sun, next_mon = date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 7)
    assert mon.weekday() == 0 and fri.weekday() == 4 and next_mon.weekday() == 0

    check(ap._weekdays_between(mon, mon), 0, "same day is age 0")
    check(ap._weekdays_between(mon, tue), 1, "Mon -> Tue is 1")
    check(ap._weekdays_between(wed, thu), 1, "Wed -> Thu is 1")
    check(ap._weekdays_between(fri, next_mon), 1,
          "Fri -> Mon is 1 (the weekend produced no news)")
    check(ap._weekdays_between(fri, sat), 0, "Fri -> Sat is 0")
    check(ap._weekdays_between(fri, sun), 0, "Fri -> Sun is 0")
    check(ap._weekdays_between(thu, next_mon), 2, "Thu -> Mon is 2 (Fri, Mon)")
    check(ap._weekdays_between(mon, mon + timedelta(days=7)), 5, "one week is 5")
    check(ap._weekdays_between(mon, mon + timedelta(days=28)), 20,
          "four weeks is 20")
    check(ap._weekdays_between(tue, mon), 0,
          "end before start is 0, not negative (clock skew)")


def test_age_from_as_of() -> None:
    section("age from as_of, with today pinned to Mon 2026-09-07")
    _pin_today(date(2026, 9, 7))
    try:
        check(ap._news_signal_age_days({"as_of": "2026-09-07"}), 0, "today")
        check(ap._news_signal_age_days({"as_of": "2026-09-04"}), 1,
              "the Friday before")
        check(ap._news_signal_age_days({"as_of": "2026-09-03"}), 2,
              "the Thursday before")
        check(ap._news_signal_age_days({"as_of": "2026-08-31"}), 5,
              "a week earlier")
        check(ap._news_signal_age_days({"as_of": "2026-09-09"}), 0,
              "a future date reads as fresh, not negative")
        for bad, why in [({}, "no as_of key"), ({"as_of": None}, "null as_of"),
                         ({"as_of": "not-a-date"}, "unparseable as_of"),
                         ({"as_of": ""}, "empty as_of"), (None, "no dict")]:
            check(ap._news_signal_age_days(bad), None, f"{why} -> unknown age")
    finally:
        _unpin_today()


# ------------------------------------------------------ 2. the decay table ----

def test_decay_table() -> None:
    section("decay: delta by score strength and age")
    today = date(2026, 9, 7)
    _pin_today(today)
    try:
        ages = [0, 1, 2, 3, 4, 5, 8]
        as_of = {a: _as_of_aged(a, today) for a in ages}

        def delta(score, age, label="bullish"):
            m = ap._news_signal_modifier(_news(score, as_of[age], label=label))
            return None if m is None else m[0]

        want_strong = {0: 6, 1: 6, 2: 3, 3: 3, 4: 3, 5: None, 8: None}
        for a in ages:
            check(delta(0.8, a), want_strong[a], f"strong bullish +0.8 at age {a}")
            check(delta(1.0, a), want_strong[a], f"max bullish +1.0 at age {a}")
        for a in ages:
            want = None if want_strong[a] is None else -want_strong[a]
            check(delta(-0.8, a, "bearish"), want,
                  f"strong bearish -0.8 at age {a} (mirrors bullish)")

        # A weak read is worth 3 while fresh and nothing once stale: half of 3
        # rounds to zero on purpose.
        want_weak = {0: 3, 1: 3, 2: None, 3: None, 4: None, 5: None, 8: None}
        for a in ages:
            check(delta(0.3, a), want_weak[a], f"weak bullish +0.3 at age {a}")
            want = None if want_weak[a] is None else -want_weak[a]
            check(delta(-0.3, a, "bearish"), want, f"weak bearish -0.3 at age {a}")

        # Boundaries of the score bands are unchanged by the decay work.
        check(delta(0.5, 0), 6, "+0.50 is the bottom of the strong band")
        check(delta(0.49, 0), 3, "+0.49 is still only the weak band")
        check(delta(0.2, 0), 3, "+0.20 is the bottom of the weak band")
        check(delta(0.19, 0), None, "+0.19 is inside the neutral band")
        check(delta(-0.19, 0, "bearish"), None, "-0.19 is inside the neutral band")
        check(delta(0.0, 0, "neutral"), None, "a flat 0.0 never nudges")
        check(delta(0.05, 8, "neutral"), None, "neutral and stale still nothing")
    finally:
        _unpin_today()


def test_decay_leaves_odd_input_alone() -> None:
    section("decay: absent, unknown-age and malformed reads")
    _pin_today(date(2026, 9, 7))
    try:
        check(ap._news_signal_modifier(None), None, "no news dict -> no nudge")
        check(ap._news_signal_modifier({}), None, "empty dict -> no nudge")
        check(ap._news_signal_modifier({"label": "bullish"}), None,
              "no score key -> no nudge")
        check(ap._news_signal_modifier({"score": None}), None,
              "explicit null score -> no nudge")

        # An unknown age must not silently discount a real score.
        m = ap._news_signal_modifier(_news(0.8, None))
        check(m and m[0], 6, "missing as_of is treated as fresh (full 6)")
        m = ap._news_signal_modifier(_news(0.8, "garbage"))
        check(m and m[0], 6, "unparseable as_of is treated as fresh (full 6)")
    finally:
        _unpin_today()


def test_reason_text() -> None:
    section("reason text names the read's date once it isn't today's")
    today = date(2026, 9, 7)
    _pin_today(today)
    try:
        def desc(score, age):
            m = ap._news_signal_modifier(_news(score, _as_of_aged(age, today)))
            return None if m is None else m[1]

        check(desc(0.8, 0), "Recent news bullish: raised guidance",
              "age 0 keeps the original wording")
        check(desc(0.8, 1), "News (Sep 4) bullish: raised guidance",
              "age 1 states the date at full weight")
        check(desc(0.8, 3), "News (Sep 2, half weight) bullish: raised guidance",
              "a discounted read says both date and weight")

        m = ap._news_signal_modifier(_news(0.8, None))
        check(m and m[1], "Recent news bullish: raised guidance",
              "unknown age keeps the original wording")
        m = ap._news_signal_modifier(
            _news(0.8, _as_of_aged(3, today), rationale=""))
        check(m and m[1], "News (Sep 2, half weight) bullish",
              "no rationale leaves no trailing colon")
        m = ap._news_signal_modifier(
            _news(-0.8, _as_of_aged(3, today), label=""))
        check(m and m[1],
              "News (Sep 2, half weight) negative: raised guidance",
              "a missing label falls back to positive/negative")

        check(ap._news_as_of_label({"as_of": "2026-12-01"}), "Dec 1",
              "date label is not zero-padded")
        check(ap._news_as_of_label({"as_of": "nonsense"}), "nonsense",
              "a non-date label passes through unchanged")
        check(ap._news_as_of_label({}), "", "a missing date labels as empty")

        # The Missed Opportunities table parses verdict reasons on "|" and "·";
        # the new wording must not introduce either.
        for age in (1, 3):
            d = desc(0.8, age)
            check("|" not in d and "·" not in d, True,
                  f"age {age} reason text stays parseable (no | or ·)")
    finally:
        _unpin_today()


# ------------------------------------------- 3. flow through the verdict ------

def test_verdict_integration() -> None:
    section("compute_verdict_v2: the decayed nudge is what lands in the score")
    today = date(2026, 9, 7)
    _pin_today(today)
    try:
        def verdict(news):
            return ap.compute_verdict_v2(
                composite_score=60.0, current_price=100.0, target_price=105.0,
                trend="neutral", is_holding=True, news_signal=news,
            )

        base = verdict(None).score
        fresh = verdict(_news(0.8, _as_of_aged(0, today)))
        aged = verdict(_news(0.8, _as_of_aged(3, today)))
        expired = verdict(_news(0.8, _as_of_aged(6, today)))

        check(fresh.score - base, 6, "a fresh strong read moves the verdict +6")
        check(aged.score - base, 3, "a 3-day-old read moves it only +3")
        check(expired.score - base, 0, "a 6-day-old read does not move it")

        # The verdict's reason is the "|"-joined breakdown the report renders,
        # each line "{+/-delta} · {desc}" — so this pins the delta and the
        # wording exactly as a reader sees them on the verdict card.
        check("+6 · Recent news bullish" in fresh.reason, True,
              "the fresh read renders as a +6 breakdown line")
        check(f"+3 · News ({ap._news_as_of_label(_news(0.8, _as_of_aged(3, today)))}"
              ", half weight) bullish" in aged.reason, True,
              "the aged read renders as +3, flagged half weight")
        check("news" in expired.reason.lower(), False,
              "the expired read is not mentioned at all")

        # Bearish decay has to shrink the drag, not flip its sign.
        b_fresh = verdict(_news(-0.8, _as_of_aged(0, today), "bearish"))
        b_aged = verdict(_news(-0.8, _as_of_aged(3, today), "bearish"))
        check(b_fresh.score - base, -6, "a fresh bearish read moves it -6")
        check(b_aged.score - base, -3, "an aged bearish read moves it only -3")
        check(b_aged.score > b_fresh.score, True,
              "decay relaxes a bearish drag rather than reversing it")
    finally:
        _unpin_today()


# ------------------------------- 4. the carry path preserves the real age -----

def _isolated_cache(entries: dict) -> Path:
    """Point the news cache at a fresh temp file holding `entries`, and reset
    the in-process cache and in-flight set so nothing leaks between tests."""
    tmp = Path(tempfile.mkdtemp(prefix="news-decay-")) / "news_sentiment.json"
    tmp.write_text(json.dumps(entries))
    ap._NEWS_CACHE_PATH = tmp
    ap._news_cache = None
    ap._news_cache_dirty = False
    ap._news_batch_inflight.clear()
    return tmp


def test_carry_preserves_as_of() -> None:
    section("score_news_sentiment: a carried read keeps its original as_of")
    real_path, real_client, real_headlines = (
        ap._NEWS_CACHE_PATH, ap._anthropic_client, ap.fetch_recent_headlines)
    today = _REAL_ET_NOW().date()
    try:
        old_as_of = _as_of_aged(3, today)
        prior = _news(0.8, old_as_of)                 # a real Claude read...
        stale_ts = time.time() - 3 * 86400            # ...written 3 days ago
        cache_file = _isolated_cache({"AAA": {"ts": stale_ts, "sentiment": prior}})

        # The refresh fails: no API key, but headlines are available.
        ap._anthropic_client = lambda: None
        ap.fetch_recent_headlines = lambda t, max_items=8: ["a headline"]

        check(ap._news_entry_fresh({"ts": stale_ts}), False,
              "a 3-day-old entry is stale, so a refresh is attempted")

        got = ap.score_news_sentiment("AAA")
        check(got and got.get("as_of"), old_as_of,
              "the carried read still carries the date it was scored")
        check(got and got.get("score"), 0.8, "and the score it was scored with")
        check(ap._news_signal_age_days(got), 3,
              "so the decay sees its true age of 3 trading days")
        m = ap._news_signal_modifier(got)
        check(m and m[0], 3, "and discounts the nudge from 6 to 3")

        # Nothing may be rewritten: a bumped ts would look fresh and the ticker
        # would never be retried; a bumped as_of would reset the decay clock.
        on_disk = json.loads(cache_file.read_text())["AAA"]
        check(on_disk["ts"], stale_ts, "the entry's ts is left untouched")
        check(on_disk["sentiment"]["as_of"], old_as_of,
              "the entry's as_of is left untouched")
        check(ap._news_entry_fresh(on_disk), False,
              "the entry stays stale, so the next run retries the refresh")
    finally:
        ap._NEWS_CACHE_PATH, ap._anthropic_client, ap.fetch_recent_headlines = (
            real_path, real_client, real_headlines)
        ap._news_cache = None


def test_lexicon_read_is_not_carried() -> None:
    section("score_news_sentiment: only a real model read is worth carrying")
    real_path, real_client, real_headlines = (
        ap._NEWS_CACHE_PATH, ap._anthropic_client, ap.fetch_recent_headlines)
    today = _REAL_ET_NOW().date()
    try:
        prior = _news(0.8, _as_of_aged(3, today), method="lexicon")
        _isolated_cache({"AAA": {"ts": time.time() - 3 * 86400,
                                 "sentiment": prior}})
        ap._anthropic_client = lambda: None
        ap.fetch_recent_headlines = lambda t, max_items=8: [
            "shares surge on record profit beat", "analysts raise price target"]

        got = ap.score_news_sentiment("AAA")
        check(got.get("method"), "lexicon", "a stale lexicon read is re-scored")
        check(got.get("as_of"), ap._et_date_iso(),
              "the fresh lexicon read is dated today")
        check(ap._news_signal_age_days(got), 0, "so it is not discounted")
    finally:
        ap._NEWS_CACHE_PATH, ap._anthropic_client, ap.fetch_recent_headlines = (
            real_path, real_client, real_headlines)
        ap._news_cache = None


# ------------------------- 5. batch harvest dates by submission, not pickup ---

class _FakeBatchResult:
    def __init__(self, custom_id, payload):
        self.custom_id = custom_id
        text = SimpleNamespace(type="text", text=json.dumps(payload))
        self.result = SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(content=[text]),
        )


class _FakeBatchesClient:
    """Just enough of the Anthropic batches surface for _store_batch_results."""

    def __init__(self, payloads):
        self._payloads = payloads
        inner = SimpleNamespace(results=lambda batch_id: [
            _FakeBatchResult(cid, p) for cid, p in self._payloads.items()])
        self.messages = SimpleNamespace(batches=inner)


def test_batch_harvest_as_of() -> None:
    section("cross-run harvest: scores are dated when submitted, not collected")
    real_path = ap._NEWS_CACHE_PATH
    try:
        _isolated_cache({})
        client = _FakeBatchesClient({
            "news-0": {"score": 0.9, "label": "bullish", "rationale": "beat"}})
        submitted_ts = time.time() - 3 * 86400
        submitted_day = ap._et_date_iso(submitted_ts)

        n = ap._store_batch_results(
            client, "batch_x", {"news-0": "AAA"}, {"AAA": ["h1"]},
            submitted_day)
        check(n, 1, "the batch result is stored")

        entry = ap._load_news_cache()["AAA"]
        check(entry["sentiment"]["as_of"], submitted_day,
              "the score is dated the day its batch went out")
        check(entry["sentiment"]["as_of"] != ap._et_date_iso(), True,
              "not the day it happened to be harvested")
        check(ap._news_signal_age_days(entry["sentiment"]), 3,
              "so a 3-day-late harvest is correctly seen as 3 days old")

        # ts is the write time regardless, because it drives the re-score TTL:
        # we just paid for this score, so today's run must not re-buy it.
        check(abs(entry["ts"] - time.time()) < 60, True,
              "the entry's ts is still the write time")
        check(ap._news_entry_fresh(entry), True,
              "so the score counts as cached today and is not re-bought")

        # A batch submitted in this run gets today's date by default.
        _isolated_cache({})
        ap._store_batch_results(
            client, "batch_y", {"news-0": "BBB"}, {"BBB": ["h1"]})
        fresh = ap._load_news_cache()["BBB"]["sentiment"]
        check(fresh["as_of"], ap._et_date_iso(),
              "a same-run batch defaults to today")
        check(ap._news_signal_age_days(fresh), 0, "and is not discounted")
    finally:
        ap._NEWS_CACHE_PATH = real_path
        ap._news_cache = None


# --------------------------------- 6. the failure mode this exists to fix -----

def test_stuck_pipeline_fades_out() -> None:
    section("a news path that stays broken fades to no signal")
    real_path, real_client, real_headlines = (
        ap._NEWS_CACHE_PATH, ap._anthropic_client, ap.fetch_recent_headlines)
    try:
        # Monday, with a strong bullish read scored that morning.
        scored_on = date(2026, 9, 7)
        prior = _news(0.9, scored_on.isoformat())
        prior_ts = datetime(2026, 9, 7, 9, 0, tzinfo=NY).timestamp()

        ap._anthropic_client = lambda: None           # the refresh never works
        ap.fetch_recent_headlines = lambda t, max_items=8: ["a headline"]

        timeline = []
        for offset in range(0, 13):                   # ~2.5 weeks of runs
            run_day = scored_on + timedelta(days=offset)
            if run_day.weekday() >= 5:
                continue                              # crons are weekday-only
            _pin_today(run_day)
            _isolated_cache({"AAA": {"ts": prior_ts, "sentiment": prior}})
            served = ap.score_news_sentiment("AAA")
            m = ap._news_signal_modifier(served)
            timeline.append((run_day.strftime("%a %b %-d"),
                             ap._news_signal_age_days(served),
                             0 if m is None else m[0]))
            _unpin_today()

        for day, age, nudge in timeline:
            print(f"    {day:<10} age {age:>2} td   nudge {nudge:+d}")

        # Mon Sep 7 through Fri Sep 18: ten weekday runs, two weekends skipped.
        check([n for _, _, n in timeline],
              [6, 6, 3, 3, 3, 0, 0, 0, 0, 0], "the nudge fades 6 -> 3 -> 0")
        check([a for _, a, _ in timeline], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
              "and each run ages the read by exactly one session")
        check(dict(zip([d for d, _, _ in timeline],
                       [a for _, a, _ in timeline]))["Mon Sep 14"], 5,
              "the weekend itself does not age it (Fri 4 -> Mon 5)")
        check(timeline[-1][2], 0,
              "a permanently broken refresh ends at no signal, not a frozen one")
    finally:
        _unpin_today()
        ap._NEWS_CACHE_PATH, ap._anthropic_client, ap.fetch_recent_headlines = (
            real_path, real_client, real_headlines)
        ap._news_cache = None


# -------------------------------------------------------------------- main ----

def main() -> int:
    for t in (test_weekdays_between, test_age_from_as_of, test_decay_table,
              test_decay_leaves_odd_input_alone, test_reason_text,
              test_verdict_integration, test_carry_preserves_as_of,
              test_lexicon_read_is_not_carried, test_batch_harvest_as_of,
              test_stuck_pipeline_fades_out):
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
