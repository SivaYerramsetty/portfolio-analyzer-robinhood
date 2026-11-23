"""
test_entry_signal.py
--------------------
Tests for the Entry window: the market-wide reading of when new money has
historically had an edge (entry_signal.py), plus the analyzer's side — the
once-a-day history cache with its 30-minute top-up, the gauge, the evidence
panel and the one-email-per-window alert.

The policy under test: the window opens exactly when the VIX has closed at 40+
within the last 21 sessions or the S&P is 30%+ below its record, and not on
milder stress; before 1990 the VIX is estimated from the S&P's own volatility;
every stat the report shows is computed from the history it is given; history
is fetched whole once per ET day and topped up at most every 30 minutes, and
a failed or refused fetch never replaces the cached copy; an open window is
emailed once, only when the SMTP settings exist.

Hermetic: no network and no real cache file. Run it directly —

    ./venv/bin/python test_entry_signal.py

Exit code is 0 when everything passes, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import analyze_portfolio as ap
import entry_signal as es
import yahoo_limits as yl
from test_base_score_mode import _render_report, _tag_errors

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
    print(f"\n== {title}")


@contextlib.contextmanager
def patched(obj, **attrs):
    old = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(obj, k, v)


@contextlib.contextmanager
def env(**values):
    old = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# --------------------------------------------------------------- fixtures ----

def sessions(start: str, end: str) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]


def market(end: str = "2026-11-23", crash_at: str = "2008-09-15",
           tail_vix: float = 35.0, tail_drop: float = 0.10):
    """A synthetic S&P since 1950 (steady drift, small noise), a 40% crash
    with a VIX of 45 starting at `crash_at`, and a last month whose VIX
    peaks at `tail_vix` while the S&P slides `tail_drop` and half recovers.
    VIX quotes start in 1990, at 15 when nothing is happening."""
    d = sessions("1950-01-02", end)
    n = len(d)
    rng = np.random.default_rng(7)
    lr = 0.0003 + rng.normal(0, 0.006, n)
    k = d.index(next(x for x in d if x >= crash_at))
    lr[k:k + 60] += np.log(0.60) / 60          # -40% over 60 sessions
    lr[k + 60:k + 400] += -np.log(0.60) / 340  # and back
    lr[-12:-4] += np.log(1 - tail_drop) / 8
    lr[-4:] += -np.log(1 - tail_drop) / 2 / 4
    c = 20 * np.exp(np.cumsum(lr))
    vd = [x for x in d if x >= "1990-01-02"]
    v = np.full(len(vd), 15.0)
    kv = vd.index(d[k])
    v[kv + 20:kv + 50] = 45.0
    v[-6] = tail_vix
    v[-5:] = 26.0
    return d, list(c), vd, list(v)


def multpl_html(rows) -> str:
    cells = "".join(f'<tr class="odd">\n<td>{d}</td>\n<td>\n&#x2002;\n{v}\n</td>\n</tr>\n'
                    for d, v in rows)
    return f'<table id="datatable">\n<tr>\n<th>Date</th>\n<th>Value</th>\n</tr>\n{cells}</table>'


# ------------------------------------------------------------------ tests ----

def test_score_bars() -> None:
    section("the gauge reads 60 at the stress bars and 85 at the window bars")
    check(float(es.score([30.0], [0.0])[0]), es.STRESS_SCORE, "VIX 30 → stress bar")
    check(float(es.score([40.0], [0.0])[0]), es.WINDOW_SCORE, "VIX 40 → window bar")
    check(float(es.score([0.0], [-0.20])[0]), es.STRESS_SCORE, "S&P −20% → stress bar")
    check(float(es.score([0.0], [-0.30])[0]), es.WINDOW_SCORE, "S&P −30% → window bar")
    check(float(es.score([39.9], [-0.299])[0]) < es.WINDOW_SCORE, True,
          "just short of both bars stays closed")
    check(float(es.score([12.0], [0.0])[0]), 0.0, "a calm VIX at a record reads 0")
    check(float(es.score([np.nan], [-0.30])[0]), es.WINDOW_SCORE,
          "no VIX reading still lets the drop open it")
    check([es.classify(x) for x in (84.9, 85.0, 59.9, 60.0)],
          ["stress", "open", "normal", "stress"], "classify splits at the bars")
    check([es.rating(x) for x in (10, 45, 70, 90)],
          ["Calm", "Normal", "Stressed", "Window open"], "the gauge's words")


def test_series() -> None:
    section("the VIX before 1990 is the S&P's own volatility, scaled")
    d = sessions("1989-11-01", "1990-03-01")
    c = list(100 * np.exp(np.cumsum(np.r_[0, np.tile([0.01, -0.01], len(d))[:len(d) - 1]])))
    vd = sessions("1990-01-02", "1990-03-01")
    vd_skip = [x for x in vd if x != "1990-02-05"]        # a VIX holiday
    s = es.align(d, c, vd_skip, [20.0] * len(vd_skip))
    i89 = d.index("1989-12-29")
    expect = np.std(np.diff(np.log(c))[i89 - 20:i89 + 1], ddof=1) * np.sqrt(252) * 100 * es.VOL_PROXY_RATIO
    close(float(s["vol"][i89]), expect, 1e-6, "1989: volatility × ratio")
    check(bool(s["is_vix"][i89]), False, "and flagged as an estimate")
    check(float(s["vol"][d.index("1990-01-02")]), 20.0, "1990: the VIX close itself")
    check((float(s["vol"][d.index("1990-02-05")]), bool(s["is_vix"][d.index("1990-02-05")])),
          (20.0, True), "a day the VIX didn't trade carries the last close")
    check(es.align([], []) ["c"].size, 0, "no closes → empty series")
    s2 = es.align(["2020-01-02", "2020-01-03"], [10.0, float("nan")])
    check(s2["c"].tolist(), [10.0], "bad closes are dropped")

    section("history helpers")
    check(es.episodes(np.array([0, 1, 1, 0, 1, 0, 0, 0, 1], bool), gap=2), [1, 8],
          "runs closer than the gap merge into one episode")
    c = np.array([100.0, 110, 99, 121, 90])
    check([round(x, 4) for x in es.forward(c, 2)[:3]], [-0.01, 0.1, -0.0909],
          "forward returns")
    check(np.isnan(es.forward(c, 2)[3:]).all(), True, "NaN near the end")
    check([round(x, 4) for x in es.forward_low(c, 2)[:3]], [-0.01, -0.1, -0.0909],
          "deepest further drop over the next sessions")
    check([round(x, 4) for x in es.forward_low(c, 1)[:4]], [0.0, -0.1, 0.0, -0.2562],
          "0 when it never closed lower")
    check([round(x, 3) for x in es.drawdown(c)], [0.0, 0.0, -0.1, 0.0, -0.256],
          "drawdown from the record close so far")
    check(es.recent_peak(np.array([np.nan, 10, 50, 20, 20, 20]), sessions=2).tolist()[1:],
          [10.0, 50.0, 50.0, 50.0, 20.0], "the peak drops out after `sessions`")


def test_assess() -> None:
    section("assess: a stressed month that isn't a window")
    d, c, vd, v = market()
    sig = es.assess(d, c, vd, v)
    check(sig["state"], "stress", "VIX 35 a week ago → stressed")
    close(sig["score"], es.STRESS_SCORE + 5 * 2.5, 0.01, "and the needle sits 5 VIX points past 30")
    check(sig["since"], None, "no window, no since")
    r = sig["reading"]
    check((r["vix"], r["vix_peak"], r["vix_peak_date"]), (26.0, 35.0, d[-6]),
          "today's VIX, the month's peak and its date")
    check(r["date"], d[-1], "the reading is the last close")
    close(r["to_window_vix"], 14.0, 1e-9, "14 VIX points to the window")
    check(r["vix_is_proxy"], False, "a real VIX quote")
    check(sig["span"], {"from": "1950-01-02", "to": d[-1], "vix_from": "1990-01-02"},
          "span: S&P from 1950, VIX from 1990")

    section("assess: the record behind each state is computed, not assumed")
    st = sig["stats"]
    check(set(st), set(es.STATES), "one row per state")
    crash = next(w for w in sig["windows"] if w["date"] >= "2008-01-01")
    check(crash["trigger"], "vix", "the 2008 crash window opened on the VIX")
    check(crash["vix_peak"], 45.0, "at its peak reading")
    check(st["open"]["episodes"] >= 1, True, "the crash counts as an episode")
    # Every open day in the fixture is inside the crash, so its next-12-month
    # average must match a direct computation over those same days.
    s = es.align(d, c, vd, v)
    pk = es.recent_peak(s["vol"])
    sc = es.score(pk, es.drawdown(s["c"]))
    f12 = es.forward(s["c"], es.H12)
    days = (sc >= es.WINDOW_SCORE) & np.isfinite(f12)
    close(st["open"]["avg12"], round(float(f12[days].mean()) * 100, 1), 0.05,
          "the window's 12-month average matches its days")
    check(st["open"]["days"], int(days.sum()), "and its day count")
    check(sig["all"]["days"] >= st["open"]["days"] + st["stress"]["days"], True,
          "all days include every state")
    check(set(sig["wait"]), {"median_years", "cheaper_pct", "n"}, "waiting stats present")

    section("assess: a window opens and says since when")
    d2, c2, vd2, v2 = market(tail_vix=48.0)
    sig2 = es.assess(d2, c2, vd2, v2)
    check((sig2["state"], sig2["rating"], sig2["since"]), ("open", "Window open", d2[-6]),
          "a VIX close of 48 opens it, from that day")
    d3, c3, vd3, v3 = market(end="2026-12-31", tail_vix=48.0)
    v3 = list(v3)
    v3[-30] = 48.0
    v3[-6] = 26.0
    sig3 = es.assess(d3, c3, vd3, v3)
    check(sig3["state"] != "open", True, "29 sessions later the spike no longer counts")

    section("assess: too little history says nothing")
    check(es.assess(d[-2000:], c[-2000:]), None, "under ten years since 1950 → None")


def test_cape() -> None:
    section("CAPE: parse multpl's table and place today in it")
    html = multpl_html([("Nov 20, 2026", "36.44"), ("Nov 1, 2026", "39.82"),
                        ("Jan 1, 1881", "18.47")])
    check(es.parse_multpl_table(html),
          (["1881-01-01", "2026-11-01", "2026-11-20"], [18.47, 39.82, 36.44]),
          "oldest first, ISO dates")
    check(es.parse_multpl_table("<p>nothing</p>"), ([], []), "a changed page parses to nothing")
    d, c, vd, v = market()
    months = [x for x in pd.date_range("1881-01-01", "2026-11-01", freq="MS").strftime("%Y-%m-%d")]
    vals = list(np.linspace(10, 30, len(months)))
    vals[-1] = 36.0
    ctx = es.cape_context(months, vals, es.align(d, c))
    check(ctx["cape"], 36.0, "today's CAPE")
    check(ctx["pct"], 100, "above every earlier month")
    check(ctx["in_top"], True, "in the dearest tenth since 1950")
    check(all(ctx[k] is not None for k in ("f10_top", "f10_all", "f12_top", "f12_all")), True,
          "with next-year and next-decade returns for both groups")
    check(es.cape_context([], [], es.align(d, c)), None, "no CAPE → None")


# -------------------------------------------------------- analyzer side ----

def test_refresh() -> None:
    section("history cache: whole once per ET day, last sessions every 30 min")
    calls = []
    full = {"^GSPC": (sessions("1950-01-02", "2026-11-20"), None),
            "^VIX": (sessions("1990-01-02", "2026-11-20"), None)}

    def fake_closes(symbol, period):
        calls.append((symbol, period))
        if period == "max":
            ds = full[symbol][0]
            return ds, [1.0] * len(ds)
        return ["2026-11-20", "2026-11-23"], [2.0, 3.0]

    cape_calls = []

    def fake_cape():
        cape_calls.append(1)
        return {"d": ["2026-11-01"], "c": [36.0]}

    t0 = 1_795_000_000.0       # 2026-11-17, mid-session ET
    with patched(ap, _yf_daily_closes=fake_closes, _fetch_cape_table=fake_cape), \
            patched(yl, refusals=lambda: 0):
        cache: dict = {}
        check(ap.refresh_entry_history(cache, now=t0), True, "first run fetches")
        check(calls, [("^GSPC", "max"), ("^VIX", "max")], "the whole of both")
        check((cache["day"], cache["tail_at"]), (ap._et_date_iso(t0), t0), "stamped")
        check(len(cape_calls), 1, "and the CAPE table")
        calls.clear()
        check(ap.refresh_entry_history(cache, now=t0 + 600), False, "10 min later: nothing")
        check(calls, [], "no requests")
        ap.refresh_entry_history(cache, now=t0 + 1800)
        check(calls, [("^GSPC", "5d"), ("^VIX", "5d")], "30 min later: the last sessions")
        check(cache["sp"]["d"][-2:], ["2026-11-20", "2026-11-23"], "merged onto the end")
        check(cache["sp"]["c"][-3:], [1.0, 2.0, 3.0], "replacing the overlap")
        check(len(cape_calls), 1, "CAPE still once a day")
        calls.clear()
        ap.refresh_entry_history(cache, now=t0 + 86400)
        check(calls, [("^GSPC", "max"), ("^VIX", "max")], "next ET day: whole again")
        check(len(cape_calls), 2, "and the CAPE table again")

    section("history cache: failures keep the cached copy")
    good = {"sp": {"d": ["2026-11-20"], "c": [5.0]}, "vix": {"d": ["2026-11-20"], "c": [20.0]},
            "cape": {"d": ["2026-11-01"], "c": [36.0]}}
    with patched(ap, _yf_daily_closes=lambda s, p: None, _fetch_cape_table=lambda: None), \
            patched(yl, refusals=lambda: 0):
        cache = {k: dict(v) for k, v in good.items()}
        ap.refresh_entry_history(cache, now=t0)
        check({k: cache[k] for k in good}, good, "a failed fetch changes no series")
        check("day" in cache, False, "and isn't stamped, so the next run retries")
    calls.clear()
    with patched(ap, _yf_daily_closes=fake_closes, _fetch_cape_table=fake_cape), \
            patched(yl, refusals=lambda: 1):
        cache = {k: dict(v) for k, v in good.items()}
        ap.refresh_entry_history(cache, now=t0)
        check(calls, [], "nothing is fetched once Yahoo has refused the run")
    check(ap._merge_closes(None, (["a"], [1.0])), {"d": ["a"], "c": [1.0]},
          "a tail with no cache becomes the series")
    check(ap._merge_closes({"d": ["a"], "c": [1.0]}, None), {"d": ["a"], "c": [1.0]},
          "no tail keeps the cache")


def _signal(state: str = "stress"):
    d, c, vd, v = market(tail_vix=48.0 if state == "open" else 35.0)
    months = list(pd.date_range("1881-01-01", "2026-11-01", freq="MS").strftime("%Y-%m-%d"))
    vals = list(np.linspace(10, 30, len(months)))
    vals[-1] = 36.0
    sig = es.assess(d, c, vd, v, months, vals)
    sig["alerted"] = None
    return sig


def test_render() -> None:
    section("the gauge and its panel")
    sig = _signal("stress")
    g = ap._render_entry_gauge(sig)
    check('id="entryMeter"' in g and 'role="button"' in g, True, "a clickable meter")
    check(">Stressed<" in g, True, "rated Stressed")
    check("Opens at VIX 40 / S&amp;P &minus;30%" in g, True, "says where it opens")
    check("entry-new-chip" in g, False, "no NEW chip unless open")
    p = ap._render_entry_panel(sig)
    check('id="entryPanel" hidden' in p, True, "the panel starts hidden")
    check(p.count("<tr class='entry-today'>"), 1, "today's state row is marked once")
    check("Stressed, but not a window" in p, True, "the lead fits the state")
    check(p.count("<tr>") >= len(es.DEPLOY_STUDY["rows"]) + len(sig["windows"]), True,
          "lump-sum rows and every past window")
    check("dearest tenth" in p, True, "valuation context")
    check(ap._render_entry_gauge(None) + ap._render_entry_panel(None), "", "no reading → nothing")

    sig_open = _signal("open")
    g = ap._render_entry_gauge(sig_open)
    check(("entry-open" in g, "entry-new-chip" in g, f'data-entry-since="{sig_open["since"]}"' in g),
          (True, True, True), "an open window: ringed, NEW chip, since date")
    check("A rare entry window is open" in ap._render_entry_panel(sig_open), True,
          "and its lead")

    section("the full report")
    with patched(ap, fetch_market_fear_greed=lambda: None,
                 fetch_benchmark_returns=lambda: None, fetch_market_news=lambda: None,
                 fetch_entry_signal=lambda: sig,
                 _compute_holdings_ytd_return=lambda results: None), \
            env(GITHUB_REPOSITORY="owner/repo"), contextlib.redirect_stdout(io.StringIO()):
        page = ap.generate_html_report([])
    check('id="entryMeter"' in page and 'id="entryPanel"' in page, True,
          "gauge and panel in the page")
    check(page.index('id="entryMeter"') < page.index('id="entryPanel"')
          < page.index('class="summary-card"'), True,
          "panel right under the meter row, above the summary")
    check(_tag_errors(page), [], "the page still balances")
    page = _render_report([], None)
    check('id="entryMeter"' in page, False, "the test stub's None leaves it out")
    with env(ENTRY_SIGNAL="0"):
        check(ap.fetch_entry_signal(), None, "ENTRY_SIGNAL=0 turns it off")


def test_alert() -> None:
    section("one email per window, only with SMTP settings")
    sent = []
    smtp = dict(SMTP_HOST="h", SMTP_USER="u", SMTP_PASS="p", EMAIL_TO="t",
                GITHUB_REPOSITORY="Owner/repo")
    sig = _signal("open")
    with tempfile.TemporaryDirectory() as tmp, \
            patched(ap, _ENTRY_CACHE_PATH=Path(tmp) / "entry.json",
                    send_email=lambda html, subject: sent.append((html, subject))), \
            contextlib.redirect_stdout(io.StringIO()):
        with env(SMTP_HOST=None):
            check(ap.notify_entry_window(sig), False, "no SMTP: no email")
            check(ap._load_entry_cache().get("alerted"), None, "and not marked as sent")
        with env(**smtp):
            check(ap.notify_entry_window(_signal("stress")), False, "stressed: no email")
            check(ap.notify_entry_window(sig), True, "open: emailed")
            check(ap.notify_entry_window(sig), False, "once")
            check(ap._load_entry_cache().get("alerted"), sig["since"], "remembered by start date")
            later = dict(sig, since="2027-03-01")
            check(ap.notify_entry_window(later), True, "a new window emails again")
    check(len(sent), 2, "two emails in all")
    check(sent[0][1].startswith("Entry window open"), True, "subject")
    check("&minus;" in sent[0][1], False, "a plain minus in the subject")
    check("https://owner.github.io/repo/" in sent[0][0], True, "links the report")


def main() -> int:
    for t in (test_score_bars, test_series, test_assess, test_cape,
              test_refresh, test_render, test_alert):
        t()
    failed = [label for ok, label in _results if not ok]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
