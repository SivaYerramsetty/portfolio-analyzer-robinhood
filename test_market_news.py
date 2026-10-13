"""
test_market_news.py
-------------------
Tests for the header "Today's news highlights" tile: reading the business-news
feeds, keeping only today's stories (ET), the cache and feed fallbacks, and the
tile and hover-panel markup.

The policy under test (see the market-news section of analyze_portfolio.py):
Google News' business top stories are the source, in feed order (most
prominent first); CNBC's top news stands in when Google can't be reached; a
failed fetch serves the cached list; and nothing from an earlier day is shown,
so a stale cache hides the tile instead of passing yesterday off as today.

Hermetic: no network, no real cache file. Run it directly —

    ./venv/bin/python test_market_news.py

Exit code is 0 when everything passes, 1 otherwise.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import analyze_portfolio as ap

NY = ZoneInfo("America/New_York")
UTC = timezone.utc
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=NY)       # a Tuesday, midday ET

GOOGLE_URL = ap._MARKET_NEWS_FEEDS[0][1]
CNBC_URL = ap._MARKET_NEWS_FEEDS[1][1]

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


def _rfc(dt: datetime) -> str:
    """An RSS pubDate, the way the feeds write it."""
    return format_datetime(dt.astimezone(UTC), usegmt=True)


def _item(title: str, when: datetime | None, link: str = "https://example.com/a",
          source: str | None = None) -> str:
    parts = [f"<title>{title}</title>", f"<link>{link}</link>"]
    if when is not None:
        parts.append(f"<pubDate>{_rfc(when)}</pubDate>")
    if source:
        parts.append(f'<source url="https://example.com">{source}</source>')
    return "<item>" + "".join(parts) + "</item>"


def _rss(*items: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
            "<title>Feed</title>" + "".join(items) + "</channel></rss>").encode()


# Google's shape: every title ends in " - Publisher", which <source> repeats.
GOOGLE_FEED = _rss(
    _item("Stocks fall after bank earnings - CNBC", NOW - timedelta(hours=1),
          "https://news.google.com/rss/articles/abc?oc=5", "CNBC"),
    _item("Fed's Waller - a dove after all? - Bloomberg.com",
          NOW - timedelta(hours=3), source="Bloomberg.com"),
    _item("Yesterday's wrap - WSJ", datetime(2026, 10, 12, 23, 30, tzinfo=NY),
          source="WSJ"),
    # 03:30 UTC on the 14th is still the 13th in New York.
    _item("Late tonight - Reuters", datetime(2026, 10, 14, 3, 30, tzinfo=UTC),
          source="Reuters"),
    _item("Undated - AP", None, source="AP"),
    _item("Odd link - Axios", NOW - timedelta(hours=2), "javascript:alert(1)",
          "Axios"),
)
# CNBC's shape: no <source>, titles carry no publisher suffix.
CNBC_FEED = _rss(
    _item("UnitedHealth tops estimates", NOW - timedelta(minutes=30)),
    _item("Oil prices fall", NOW - timedelta(hours=4)),
)
YESTERDAY_ONLY = _rss(
    _item("Old news - WSJ", datetime(2026, 10, 12, 15, 0, tzinfo=NY), source="WSJ"))


class _Resp:
    def __init__(self, status: int, content: bytes = b""):
        self.status_code, self.content = status, content


@contextlib.contextmanager
def _sandbox(routes: dict):
    """Pin market time to NOW, point the cache at a temp dir and answer feed
    requests from `routes` (url -> _Resp or an exception to raise). Yields the
    list of URLs requested."""
    calls: list[str] = []

    def get(url, **kw):
        calls.append(url)
        r = routes.get(url, _Resp(404))
        if isinstance(r, Exception):
            raise r
        return r

    tmp = tempfile.TemporaryDirectory()
    saved = (ap._MARKET_NEWS_CACHE_PATH, ap.requests, ap._et_now)
    meter = os.environ.pop("MARKET_METER", None)
    ap._MARKET_NEWS_CACHE_PATH = Path(tmp.name) / "market_news.json"
    ap.requests = SimpleNamespace(get=get)
    ap._et_now = lambda ts=None: NOW if ts is None else datetime.fromtimestamp(ts, NY)
    try:
        yield calls
    finally:
        ap._MARKET_NEWS_CACHE_PATH, ap.requests, ap._et_now = saved
        if meter is not None:
            os.environ["MARKET_METER"] = meter
        tmp.cleanup()


def _titles(news) -> list[str]:
    return [i["title"] for i in (news or {}).get("items", [])]


def _write_cache(data) -> None:
    ap._MARKET_NEWS_CACHE_PATH.write_text(
        data if isinstance(data, str) else json.dumps(data))


def _fetch():
    """fetch_market_news with the log it prints captured: (news, log)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        news = ap.fetch_market_news()
    return news, buf.getvalue()


class _TagChecker(HTMLParser):
    VOID = {"br", "img", "input", "meta", "link", "hr"}

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        else:
            self.errors.append(f"</{tag}> inside {self.stack[-3:]}")


def _tag_errors(html: str) -> list[str]:
    checker = _TagChecker()
    checker.feed(html)
    return checker.errors + [f"unclosed <{t}>" for t in checker.stack]


# ------------------------------------------------------------- 1. parsing ----

def test_parse() -> None:
    section("_parse_news_feed: titles, sources, dates and links")
    items = ap._parse_news_feed(GOOGLE_FEED, "Google News")
    check([i["title"] for i in items],
          ["Stocks fall after bank earnings", "Fed's Waller - a dove after all?",
           "Yesterday's wrap", "Late tonight", "Odd link"],
          "feed order kept, the publisher suffix stripped, the undated item dropped")
    check([i["source"] for i in items[:2]], ["CNBC", "Bloomberg.com"],
          "the publisher moves to `source`")
    check(items[0]["link"], "https://news.google.com/rss/articles/abc?oc=5",
          "http(s) links are kept")
    check(items[-1]["link"], "", "a javascript: link is blanked")
    check(items[0]["ts"], (NOW - timedelta(hours=1)).timestamp(),
          "pubDate becomes an epoch timestamp")

    cnbc = ap._parse_news_feed(CNBC_FEED, "CNBC")
    check([(i["title"], i["source"]) for i in cnbc],
          [("UnitedHealth tops estimates", "CNBC"), ("Oil prices fall", "CNBC")],
          "without <source>, the feed's own name is the source")

    naive = ap._parse_news_feed(_rss(
        "<item><title>Unzoned</title>"
        "<pubDate>Tue, 13 Oct 2026 15:00:00 -0000</pubDate></item>"), "X")
    check(naive[0]["ts"], datetime(2026, 10, 13, 15, 0, tzinfo=UTC).timestamp(),
          "a date with no zone (-0000) is read as UTC")


# ---------------------------------------------------- 2. fetching + cache ----

def test_fetch() -> None:
    section("fetch_market_news: today's stories only, cached between runs")
    with _sandbox({GOOGLE_URL: _Resp(200, GOOGLE_FEED)}) as calls:
        news = _fetch()[0]
        check(_titles(news),
              ["Stocks fall after bank earnings", "Fed's Waller - a dove after all?",
               "Late tonight", "Odd link"],
              "yesterday's story is dropped; late tonight (ET) still counts")
        check((news["source"], calls), ("Google News", [GOOGLE_URL]),
              "Google answers first, so CNBC is never asked")
        saved = json.loads(ap._MARKET_NEWS_CACHE_PATH.read_text())
        check(len(saved["items"]), 5, "the whole parsed feed is cached")

        again = _fetch()[0]
        check((_titles(again) == _titles(news), len(calls)), (True, 1),
              "within the TTL the cache answers without a request")


def test_fallbacks() -> None:
    section("fetch_market_news: feed fallback and the stale cache")
    for label, google, logged in (
            ("a 503", _Resp(503), "HTTP 503"),
            ("a timeout", TimeoutError("slow"), "TimeoutError"),
            ("an HTML page", _Resp(200, b"<html><body>no</body>"), "ParseError"),
            ("only yesterday's stories", _Resp(200, YESTERDAY_ONLY),
             "0 of 1 stories are from today")):
        with _sandbox({GOOGLE_URL: google, CNBC_URL: _Resp(200, CNBC_FEED)}) as calls:
            news, log = _fetch()
            check((news and news["source"], _titles(news), len(calls)),
                  ("CNBC", ["UnitedHealth tops estimates", "Oil prices fall"], 2),
                  f"Google answering with {label} falls back to CNBC")
            check(log.splitlines(),
                  [f"[market-news] Google News: {logged}",
                   "[market-news] CNBC: 2 of 2 stories are from today"],
                  f"the log names Google's {label} and what CNBC gave")

    with _sandbox({GOOGLE_URL: _Resp(503), CNBC_URL: ConnectionError("down")}) as calls:
        stale = {"items": ap._parse_news_feed(GOOGLE_FEED, "Google News"),
                 "source": "Google News", "fetched_at": time.time() - 3600}
        _write_cache(stale)
        news = _fetch()[0]
        check((_titles(news)[:1], news["fetched_at"], len(calls)),
              (["Stocks fall after bank earnings"], stale["fetched_at"], 2),
              "every feed failing serves the stale cache, still filtered to today")
        _fetch()
        check(len(calls), 4, "a stale cache doesn't stop the next call retrying")


def test_hidden() -> None:
    section("fetch_market_news: None when there is nothing from today")
    with _sandbox({}) as calls:
        _write_cache({"items": ap._parse_news_feed(YESTERDAY_ONLY, "Google News"),
                      "source": "Google News", "fetched_at": time.time() - 3600})
        check(_fetch()[0], None,
              "yesterday's cache with the feeds down hides the tile")

    with _sandbox({GOOGLE_URL: _Resp(200, GOOGLE_FEED)}) as calls:
        os.environ["MARKET_METER"] = "0"
        try:
            check((_fetch()[0], calls), (None, []),
                  "MARKET_METER=0 disables it without a request")
        finally:
            os.environ.pop("MARKET_METER", None)

    for label, raw in (("not JSON", "{oops"), ("a JSON list", "[1, 2]"),
                       ("a bad timestamp", '{"fetched_at": "noon", "items": []}')):
        with _sandbox({GOOGLE_URL: _Resp(200, GOOGLE_FEED)}) as calls:
            _write_cache(raw)
            news = _fetch()[0]
            check((len(_titles(news)), calls), (4, [GOOGLE_URL]),
                  f"a cache file holding {label} is ignored and the feed is read")

    with _sandbox({}) as calls:
        _write_cache({"fetched_at": time.time(),
                      "items": [{"title": "No timestamp"}, "junk"]})
        check(_fetch()[0], None,
              "malformed cached items hide the tile instead of raising")


# ------------------------------------------------------------ 3. rendering ----

def test_render() -> None:
    section("_render_news_stat: the tile and its panel")
    with _sandbox({}):
        check((ap._render_news_stat(None), ap._render_news_stat({"items": []})),
              ("", ""), "no stories, no tile")

        ts = (NOW - timedelta(minutes=90)).timestamp()
        news = {"source": "Google News", "fetched_at": NOW.timestamp(), "items": [
            {"title": "Lead <b>story</b> & more", "source": "CNBC",
             "link": "https://x.test/a?b=1&c=2", "ts": ts},
            {"title": "Second", "source": "WSJ", "link": "", "ts": ts},
            {"title": "Third", "source": "Barron's", "link": "https://x.test/3",
             "ts": ts}]}
        html = ap._render_news_stat(news)
        check(_tag_errors(html), [], "the markup is balanced")
        check(re.search(r'<strong class="news-lead">(.*?)</strong>', html).group(1),
              "Lead &lt;b&gt;story&lt;/b&gt; &amp; more",
              "the first story is the lead, HTML-escaped")
        check("CNBC · 10:30 AM · 3 stories today" in html, True,
              "the sub-line names the lead's source, time and the day's count")
        check('href="https://x.test/a?b=1&amp;c=2"' in html, True,
              "links are attribute-escaped")
        check(html.count("<a class=\"news-item\""), 2,
              "a story without a link renders as plain text")
        check(html.count('target="_blank" rel="noopener noreferrer"'), 2,
              "links open in a new tab without handing it the report")
        check(("Barron&#x27;s" in html, "Tue Oct 13" in html,
               "Google News, most prominent first. All times ET; updated 12:00 PM."
               in html),
              (True, True, True),
              "panel: escaped sources, today's date, the feed and refresh time")

        one = ap._render_news_stat({"items": news["items"][:1]})
        check(("1 story today" in one, "updated" in one), (True, False),
              "one story reads singular; no refresh time when it is unknown")


def test_report() -> None:
    section("generate_html_report: the tile sits after Next market event")
    stubs = {
        "fetch_market_fear_greed": lambda: None,
        "fetch_benchmark_returns": lambda: None,
        "fetch_market_events": lambda: [{
            "name": "CPI inflation", "timed": True, "detail": "est 0.3%",
            "when": datetime.now(NY) + timedelta(days=1)}],
    }
    real = {name: getattr(ap, name) for name in (*stubs, "fetch_market_news")}
    old_repo = os.environ.get("GITHUB_REPOSITORY")
    os.environ["GITHUB_REPOSITORY"] = "owner/repo"
    try:
        for name, stub in stubs.items():
            setattr(ap, name, stub)
        ap.fetch_market_news = lambda: {
            "source": "Google News", "fetched_at": time.time(),
            "items": [{"title": "Stocks fall", "source": "CNBC",
                       "link": "https://x.test/1", "ts": time.time()}]}
        with contextlib.redirect_stdout(io.StringIO()):
            html = ap.generate_html_report([])
        ap.fetch_market_news = lambda: None
        with contextlib.redirect_stdout(io.StringIO()):
            bare = ap.generate_html_report([])
    finally:
        for name, fn in real.items():
            setattr(ap, name, fn)
        if old_repo is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = old_repo

    row = html[html.index('<div class="summary-row">'):html.index('id="fltBar"')]
    check(0 <= row.find("Next market event") < row.find('id="newsWrap"'), True,
          "the news tile follows the events tile in the summary row")
    check(('id="newsWrap"' in bare, "Next market event" in bare), (False, True),
          "no stories: the tile is left out and the row is otherwise unchanged")

    script = next((s for s in re.findall(r"<script>(.*?)</script>", html, re.S)
                   if "getElementById('newsWrap')" in s), "")
    check(bool(script), True, "the page carries the panel script")
    node = shutil.which("node")
    if not node:
        print("  (node not found — skipping the JS syntax check)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "news.js"
        path.write_text(script)
        proc = subprocess.run([node, "--check", str(path)],
                              capture_output=True, text=True)
    check(proc.returncode, 0, "node --check accepts the panel script"
          + (f": {proc.stderr.strip()}" if proc.returncode else ""))


# -------------------------------------------------------------------- main ----

def main() -> int:
    for t in (test_parse, test_fetch, test_fallbacks, test_hidden,
              test_render, test_report):
        before = len(_results)
        t()
        passed = sum(1 for ok, _ in _results[before:] if ok)
        print(f"  {passed}/{len(_results) - before} passed")

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
