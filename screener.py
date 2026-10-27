"""
screener.py
-----------
Screens the US market against the 9-filter quality framework.

The universe is every company listed on a US exchange (ADRs included) worth at
least $1B, trading at least $5M a day and profitable over the last twelve
months, pulled from Yahoo's own screener (fetch_us_universe). It used to be
the S&P 500 + 400, but a company joins those only after a year of trading,
four profitable quarters and a committee vote, so the scan found names late:
on 2026-10-21, 140 of the 200 best one-year performers worth $2B+ weren't on
that list. The S&P list is still fetched, to tag each name's membership and
as the fallback universe when Yahoo's screener fails.

Filter set (analyze_portfolio.apply_quality_filters is the definition):
    1. Revenue growth >= 10%/yr     (3-year CAGR, else 1-year YoY)
    2. EPS growth >= 10%/yr         (3-year CAGR, else 1-year YoY)
    3. P/E < 30                     (lower of trailing / forward)
    4. PEG < 2
    5. ROE >= 15%                   (3-year average, else TTM)
    6. Operating margin >= 15%      (3-year average, else TTM)
    7. Debt/Equity < 1
    8. FCF positive & growing       (every recent year, else 1-year YoY)
    9. Quick ratio > 1.0

The scan runs in two stages:

  1. Prefilter — one Yahoo request per name (quoteSummary) and a rough reading
     of the nine filters from trailing numbers. It exists to drop the names
     that plainly fail, cheaply: Yahoo blocks an IP once it has made a few
     thousand requests (see yahoo_limits), and full scoring costs about seven
     requests a name.
  2. Scoring — the caller scores the shortlist. The report passes
     analyze_portfolio.score_screen_shortlist, which runs the same filters,
     sub-scores and Composite as every holding and watchlist row, so the
     names the scan adds to the Screening watchlist rank by the report's
     numbers.

Two channels nominate names for the Screening watchlist (CHANNELS): the nine
filters, and a cash-flow screen (fcf_eligible). Each name added is tagged with
the channels that nominated it, so the Report Scorecard grades one against the
other. The report doesn't show the scan itself: the names added appear on the
Screening watchlist with their verdicts.

screen_universe() is the entry point the report uses. It scans once per
market day (cached in .cache/screen.json), spending a capped number of Yahoo
requests per run and leaving the rest to the next run, which is what makes the
scan cheap enough to run inside a report that regenerates every 10 minutes.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import threading
import time
import io
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import requests
import pandas as pd
import yfinance as yf

import yahoo_limits as yl


# ============================================================
# Universe — the US market, from Yahoo's screener
# ============================================================
# yf.screen filters on Yahoo's side and returns up to 250 names a request, so
# the whole market costs about nine requests where reading each name would
# cost thousands. The screener's numbers are trailing twelve months, so the
# query only sets the floors; the prefilter and the scorer judge the names.
#
# Nasdaq's three tiers, the NYSE and NYSE American. Over-the-counter listings,
# where many foreign ADRs trade thinly and Yahoo's data is patchy, are left out.
_US_EXCHANGES = ("NMS", "NGM", "NCM", "NYQ", "ASE")
_SCREENER_PAGE = 250
# A runaway pager stops here: ~2,000 names fill nine pages.
_SCREENER_MAX_PAGES = 40
# Fewer names than this from Yahoo's screener means the query or the endpoint
# broke, and the scan falls back to the S&P 500 + 400.
MIN_US_UNIVERSE = 500


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def min_market_cap() -> float:
    """The smallest company the scan looks at (SCREEN_MIN_MARKET_CAP)."""
    return _env_float("SCREEN_MIN_MARKET_CAP", 1e9)


def min_dollar_volume() -> float:
    """The least average daily dollar volume (3 months) a name needs
    (SCREEN_MIN_DOLLAR_VOLUME). It drops listings too thin to trade, which
    is mostly ADRs whose real market is at home."""
    return _env_float("SCREEN_MIN_DOLLAR_VOLUME", 5e6)


def _fmt_dollars(x: float) -> str:
    for size, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if x >= size:
            return f"${x / size:g}{unit}"
    return f"${x:,.0f}"


def _universe_key() -> str:
    """Recorded with the day's scan, so changing a floor restarts it."""
    return (f"us:{min_market_cap():.0f}:{min_dollar_volume():.0f}:profitable"
            f":fcf{fcf_min_yield():g}")


def fetch_us_universe(verbose: bool = True, screen=None) -> list[str]:
    """Every US-listed company (ADRs included) worth at least min_market_cap(),
    profitable over the last twelve months, and trading at least
    min_dollar_volume() a day on average. About nine requests.

    Profitable mirrors the S&P's own entry rule, which the old universe had
    built in: no loss-making company passes the nine filters, and on
    2026-10-21 the bar kept all 75 names the previous scan had shown. Errors,
    a refusal among them, propagate. `screen` stands in for yf.screen in tests.
    """
    from yfinance import EquityQuery as Q

    screen = screen or yf.screen
    query = Q("and", [
        Q("eq", ["region", "us"]),
        Q("is-in", ["exchange", *_US_EXCHANGES]),
        Q("gte", ["intradaymarketcap", min_market_cap()]),
        Q("gt", ["netincomeis.lasttwelvemonths", 0]),
    ])
    quotes: list[dict] = []
    total = None
    for _ in range(_SCREENER_MAX_PAGES):
        # Sorted by ticker so the pages don't shift while prices move.
        page = screen(query, offset=len(quotes), size=_SCREENER_PAGE,
                      sortField="ticker", sortAsc=True) or {}
        got = page.get("quotes") or []
        total = page.get("total") or 0
        quotes += got
        if not got or len(quotes) >= total:
            break
    floor = min_dollar_volume()
    liquid = {q["symbol"] for q in quotes if q.get("symbol")
              and (q.get("averageDailyVolume3Month") or 0)
              * (q.get("regularMarketPrice") or 0) >= floor}
    if verbose:
        print(f"[universe] Yahoo screener: {len(quotes)} profitable US-listed "
              f"names over {_fmt_dollars(min_market_cap())} (of {total} it "
              f"counted); {len(liquid)} trade {_fmt_dollars(floor)}+ a day.")
    return sorted(liquid)


def fetch_universe(verbose: bool = True) -> tuple[list[str], str]:
    """(tickers, source) for a new day's scan: Yahoo's US screener ("us"), or
    the S&P 500 + 400 ("sp") when the screener fails or comes back short. A
    refusal is raised instead: the S&P fallback would be refused too, and the
    next run, on a fresh IP, can still scan the real universe."""
    try:
        tickers = fetch_us_universe(verbose=verbose)
    except Exception as e:
        if yl.is_rate_limited(e):
            raise
        if verbose:
            print(f"[universe] Yahoo screener failed ({type(e).__name__}: {e}); "
                  f"falling back to the S&P 500 + 400.")
        tickers = []
    else:
        if len(tickers) < MIN_US_UNIVERSE and verbose:
            print(f"[universe] Yahoo screener returned only {len(tickers)} "
                  f"names; falling back to the S&P 500 + 400.")
    if len(tickers) >= MIN_US_UNIVERSE:
        return tickers, "us"
    return fetch_sp500_sp400(verbose=verbose), "sp"


# ============================================================
# S&P 500 + S&P 400 — membership tags and the fallback universe
# ============================================================

_WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_WIKI_SP400 = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"

# Browser-like User-Agent — Wikipedia 403s anything that looks like a bot.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _extract_symbols_from_tables(tables: list, source_name: str,
                                 verbose: bool) -> set[str]:
    """Pick the first table that has a Symbol/Ticker column, return upper-cased set."""
    out: set[str] = set()
    for idx, df in enumerate(tables):
        try:
            cols = [str(c).strip().lower() for c in df.columns]
        except Exception:
            continue
        # Match column headers like "Symbol", "Ticker", "Ticker symbol"
        sym_col_idx = None
        for i, c in enumerate(cols):
            if c == "symbol" or c == "ticker" or "ticker symbol" in c:
                sym_col_idx = i
                break
        if sym_col_idx is None:
            continue
        try:
            syms = (df.iloc[:, sym_col_idx]
                      .astype(str)
                      .str.replace(".", "-", regex=False)
                      .str.upper().str.strip())
            picked = {s for s in syms
                      if s and s != "NAN" and len(s) <= 6 and s.replace("-","").isalnum()}
            if picked:
                if verbose:
                    print(f"[universe] {source_name}: table #{idx} -> "
                          f"{len(picked)} symbols")
                out.update(picked)
                return out  # use the first valid table
        except Exception as e:
            if verbose:
                print(f"[universe] {source_name}: table #{idx} parse error: {e}")
    return out


def fetch_sp500_sp400(verbose: bool = True,
                       cache_path: str = "universe_cache.json",
                       force_refresh: bool = False) -> list[str]:
    """
    Fetch the combined S&P 500 + S&P 400 ticker list, deduplicated.

    Cache behavior:
      - Reads from `cache_path` if present (unless force_refresh=True).
      - On successful Wikipedia fetch, writes the result to `cache_path`
        for next time.
      - If Wikipedia is unreachable and the cache exists, uses the cache.

    Strategy stack for the fresh fetch:
      1. requests.get(...) with browser User-Agent + pd.read_html
      2. pd.read_html(url) directly (uses urllib; different request path)
      3. Read from cache if available
      4. Fall back to a small hardcoded sample so the report still renders
    """
    import json
    from pathlib import Path

    cache_file = Path(cache_path)

    # Cache hit: skip the network entirely
    if cache_file.exists() and not force_refresh:
        try:
            cached = json.loads(cache_file.read_text())
            if isinstance(cached, list) and len(cached) > 100:
                if verbose:
                    print(f"[universe] Using cached list at {cache_file} "
                          f"({len(cached)} tickers). "
                          f"Pass force_refresh=True to re-fetch.")
                return sorted(cached)
        except Exception as e:
            if verbose:
                print(f"[universe] Cache read failed ({e}); fetching fresh.")

    tickers: set[str] = set()

    for name, url in [("S&P 500", _WIKI_SP500), ("S&P 400", _WIKI_SP400)]:
        got = None
        # Strategy 1: requests with browser headers
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=20)
            if verbose:
                print(f"[universe] {name}: HTTP {resp.status_code}, "
                      f"{len(resp.text):,} bytes")
            if resp.status_code == 200:
                tables = pd.read_html(io.StringIO(resp.text))
                if verbose:
                    print(f"[universe] {name}: pandas parsed {len(tables)} tables")
                got = _extract_symbols_from_tables(tables, name, verbose)
        except Exception as e:
            if verbose:
                print(f"[universe] {name}: requests strategy failed ({e})")

        # Strategy 2: pd.read_html directly (fallback)
        if not got:
            try:
                tables = pd.read_html(
                    url,
                    storage_options={"User-Agent": _HEADERS["User-Agent"]},
                )
                if verbose:
                    print(f"[universe] {name}: direct read_html -> "
                          f"{len(tables)} tables")
                got = _extract_symbols_from_tables(tables, name, verbose)
            except Exception as e:
                if verbose:
                    print(f"[universe] {name}: read_html strategy failed ({e})")

        if got:
            before = len(tickers)
            tickers.update(got)
            if verbose:
                print(f"[universe] {name}: added {len(tickers) - before} new "
                      f"(total {len(tickers)})")
        else:
            if verbose:
                print(f"[universe] {name}: ⚠ no tickers extracted!")

    # Strategy 3: stale cache (better than nothing)
    if not tickers and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            if isinstance(cached, list) and cached:
                if verbose:
                    print(f"[universe] Wikipedia unreachable; falling back to "
                          f"stale cache ({len(cached)} tickers).")
                return sorted(cached)
        except Exception:
            pass

    # Strategy 4: emergency hardcoded fallback
    if not tickers:
        if verbose:
            print("[universe] ⚠ All fetch strategies failed and no cache available.")
            print("[universe] Using small hardcoded fallback (top 30 mega-caps).")
            print("[universe] To screen the full universe, manually populate "
                  f"{cache_file} with a JSON array of tickers, e.g.:")
            print('[universe]   ["AAPL","MSFT","NVDA",...]')
        tickers = {
            "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
            "BRK-B", "AVGO", "JPM", "LLY", "V", "WMT", "MA", "XOM", "UNH",
            "ORCL", "HD", "PG", "JNJ", "COST", "ABBV", "BAC", "NFLX", "CRM",
            "CVX", "KO", "MRK", "PEP",
        }

    out = sorted(tickers)

    # Persist successful fetches to cache for next time
    if len(out) > 100:  # only cache "real" fetches, not the fallback
        try:
            cache_file.write_text(json.dumps(out, indent=2))
            if verbose:
                print(f"[universe] Saved to cache: {cache_file}")
        except Exception as e:
            if verbose:
                print(f"[universe] Could not write cache ({e})")

    if verbose:
        print(f"[universe] Total unique tickers: {len(out)}")
    return out


# ============================================================
# Stage 1 — the prefilter
# ============================================================
# One quoteSummary request per name. Ticker.info costs three (quoteSummary, the
# v7 quote and a fundamentals-timeseries call for the trailing PEG), and the
# scan used to add a fourth for the cash-flow statement: ~3,600 requests for
# the universe against Yahoo's limit of about 2,500. Every scan was cut off
# partway — on 2026-10-05 the last ~250 names alphabetically (roughly Q onward)
# failed within seconds and were left out without a word.
_PREFILTER_MODULES = ["financialData", "defaultKeyStatistics", "summaryDetail",
                      "assetProfile", "quoteType"]


def _safe(d: dict, k: str) -> Optional[float]:
    v = d.get(k)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _quote_summary(ticker: str) -> dict:
    """The prefilter's one request, flattened the way Ticker.info flattens it.

    Uses yfinance's internal quoteSummary fetch (yfinance is pinned in
    requirements.txt). If that ever moves, falls back to Ticker.info, which
    works but costs three requests a name."""
    t = yf.Ticker(ticker)
    fetch = getattr(getattr(t, "_quote", None), "_fetch", None)
    if fetch is None:
        return t.info or {}
    raw = fetch(modules=_PREFILTER_MODULES)
    try:
        modules = raw["quoteSummary"]["result"][0]
    except (TypeError, KeyError, IndexError):
        return {}
    info: dict = {}
    for module in modules.values():
        if not isinstance(module, dict):
            continue
        for k, v in module.items():
            if isinstance(v, dict) and "raw" in v:
                v = v["raw"]
            if v is not None:
                info[k] = v
    return info


@dataclass
class ScreenResult:
    ticker: str
    name: str = ""
    sector: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = None           # headquarters (assetProfile)
    in_sp: Optional[bool] = None            # S&P 500/400 member; None: unknown
    price: Optional[float] = None
    market_cap: Optional[float] = None
    week52_high: Optional[float] = None
    week52_low: Optional[float] = None
    week52_pos: Optional[float] = None
    # Trailing metrics the prefilter read (decimals; D/E as a ratio)
    rev_growth: Optional[float] = None
    eps_growth: Optional[float] = None
    pe: Optional[float] = None              # lower of trailing / forward
    peg: Optional[float] = None
    roe: Optional[float] = None
    op_margin: Optional[float] = None
    de_ratio: Optional[float] = None
    fcf: Optional[float] = None
    quick: Optional[float] = None
    net_margin: Optional[float] = None      # trailing profit margin
    financial_currency: Optional[str] = None  # the statements' currency
    fcf_yield: Optional[float] = None       # free cash flow / market cap, in USD
    # Analyst (Yahoo)
    rec_avg: Optional[float] = None         # 1 = strong buy, 5 = strong sell
    num_analysts: Optional[int] = None
    target_mean: Optional[float] = None
    upside_pct: Optional[float] = None
    # Filters the prefilter's rough reading failed (stage 1)
    prefilter_failed: Optional[int] = None
    # The nine filters, sub-scores (0-100) and Composite (stage 2)
    passes: dict[str, bool] = field(default_factory=dict)
    num_passed: int = 0
    num_failed: int = 9
    score_quality: Optional[float] = None
    score_growth: Optional[float] = None
    score_value: Optional[float] = None
    score_analyst: Optional[float] = None
    score_insider: Optional[float] = None
    insider_activity: Optional[dict] = None
    score_composite: Optional[float] = None
    # The Buyback base's rule as a 0-1 product (stage 2; analyze_portfolio.
    # buyback_rule). Orders the additions when Buyback is the run's base.
    buyback_rule: Optional[float] = None
    # Errors
    error: Optional[str] = None


def pond(r: ScreenResult) -> Optional[str]:
    """Where a screened name was found, for grading the screen's additions by
    where they came from (scorecard.py):
      "sp"   — in the S&P 500 or 400, the old universe
      "us"   — another US company, which only the wider universe reaches
      "intl" — based outside the US; most trade here as ADRs
    None for a US company on a day the S&P list couldn't be fetched, when
    membership is unknown."""
    if r.in_sp:
        return "sp"
    if r.country and r.country != "United States":
        return "intl"
    return None if r.in_sp is None else "us"


# ============================================================
# Channels — the screen's ways of nominating a name
# ============================================================
#   "quality" — passes all nine filters.
#   "fcf"     — the cash-flow screen: a free-cash-flow yield of at least
#               fcf_min_yield(), debt/equity under 1, and profitable.
#               Free cash flow is Yahoo's trailing levered figure, converted to
#               dollars for a company that reports in another currency.
# Tested over Oct 2025 -> Oct 2026 on SEC filings as they stood a year ago:
# the top fifth of the universe by FCF yield with D/E under 1 rose a median
# 14.6% (206 names) against 4.9% for the universe, as well as the names that
# passed eight or nine of the nine filters (17.2%, 49 names). Of the nine, only
# P/E and D/E carried the signal; the growth filters did worse than failing
# them. One year is one regime, so both channels run side by side and the
# scorecard grades them on live calls.
CHANNELS = ("quality", "fcf")


def fcf_min_yield() -> float:
    """The cash-flow channel's yield bar (SCREEN_MIN_FCF_YIELD): the top
    fifth of the universe started at 8.4% on 2026-10-21 (median 3.9%)."""
    return _env_float("SCREEN_MIN_FCF_YIELD", 0.08)


# A yield this high is a data error, not a bargain: before yields were put
# into dollars, YPF read 6,309% (pesos over dollars).
FCF_YIELD_MAX = 0.5


def fcf_eligible(r: ScreenResult) -> bool:
    """r qualifies for the cash-flow channel. A missing D/E doesn't qualify,
    as in the nine filters, and nor does negative equity."""
    return (not r.error and r.fcf_yield is not None
            and fcf_min_yield() <= r.fcf_yield < FCF_YIELD_MAX
            and r.de_ratio is not None and 0 <= r.de_ratio < 1
            and (r.net_margin is None or r.net_margin > 0))


def _usd_rate(currency: str) -> Optional[float]:
    """Dollars per unit of `currency`, from Yahoo's last daily close. One
    request. Raises on a refusal; None when Yahoo has no rate."""
    hist = yf.Ticker(f"{currency}USD=X").history(period="5d")
    closes = hist["Close"].dropna() if "Close" in hist else []
    return float(closes.iloc[-1]) if len(closes) and closes.iloc[-1] > 0 else None


def _set_fcf_yields(results: list[ScreenResult], rates: dict[str, float],
                    budget: yl.YahooBudget) -> list[ScreenResult]:
    """Fill in the FCF yield of each foreign reporter in `results`, fetching
    the exchange rates `rates` (the day's, kept with the scan) doesn't have
    yet. Only a refusal stops the fetch, not a spent budget: the prefilter
    usually spends the first run's budget, a day needs ~20 rates, and a batch
    without them would lose its foreign names from the cash-flow channel. A
    currency Yahoo has no rate for leaves its names without a yield. Returns
    the names left without one because Yahoo refused, to prefilter again."""
    foreign = [r for r in results if not r.error and r.fcf is not None
               and r.market_cap and r.financial_currency not in (None, "USD")]
    for cur in sorted({r.financial_currency for r in foreign} - rates.keys()):
        if budget.stopped:
            break
        try:
            rate = _usd_rate(cur)
        except Exception as e:
            if yl.is_rate_limited(e):
                budget.refused()
                break
            rate = None
        if rate:
            rates[cur] = rate
    unrated = []
    for r in foreign:
        if r.financial_currency in rates:
            r.fcf_yield = r.fcf * rates[r.financial_currency] / r.market_cap
        elif budget.stopped:
            unrated.append(r)
    return unrated


def channel_candidates(out: dict) -> dict[str, list]:
    """Each channel's candidates from screen_universe's output, best
    Composite first."""
    return {"quality": out["passed"], "fcf": out["fcf"]}


def _prefilter_passes(r: ScreenResult) -> dict[str, bool]:
    """The nine filters as far as trailing numbers can judge them.

    Stage 2 judges growth on 3-year CAGRs, ROE and margin on 3-year averages,
    FCF on its history and PEG on the trailing ratio — none of which this
    reading has — so a value missing for one of those isn't held against the
    name. P/E, D/E and the quick ratio read the fields stage 2 reads."""
    return {
        "rev_growth": r.rev_growth is None or r.rev_growth >= 0.10,
        "eps_growth": r.eps_growth is None or r.eps_growth >= 0.10,
        "pe": r.pe is not None and r.pe < 30,
        "peg": r.peg is None or 0 < r.peg < 2,
        "roe": r.roe is None or r.roe >= 0.15,
        "op_margin": r.op_margin is None or r.op_margin >= 0.15,
        "de": r.de_ratio is not None and r.de_ratio < 1,
        "fcf": r.fcf is None or r.fcf > 0,
        "quick": r.quick is not None and r.quick > 1.0,
    }


def prefilter_one(ticker: str) -> ScreenResult:
    """Fetch one name's quoteSummary and count the filters it plainly fails."""
    r = ScreenResult(ticker=ticker)
    try:
        info = _quote_summary(ticker)
    except Exception as e:
        r.error = f"{type(e).__name__}: {e}"
        return r
    if not info:
        r.error = "no info"
        return r

    r.name = info.get("shortName") or info.get("longName") or ticker
    r.sector = info.get("sector")
    r.industry = info.get("industry")
    r.country = info.get("country")
    r.price = _safe(info, "regularMarketPrice") or _safe(info, "currentPrice")
    r.market_cap = _safe(info, "marketCap")
    r.week52_high = _safe(info, "fiftyTwoWeekHigh")
    r.week52_low = _safe(info, "fiftyTwoWeekLow")
    if r.price and r.week52_high and r.week52_low and r.week52_high > r.week52_low:
        r.week52_pos = round(
            (r.price - r.week52_low) / (r.week52_high - r.week52_low) * 100, 1
        )

    r.rev_growth = _safe(info, "revenueGrowth")
    r.eps_growth = _safe(info, "earningsGrowth")
    pes = [p for p in (_safe(info, "trailingPE"), _safe(info, "forwardPE"))
           if p is not None and p > 0]
    r.pe = min(pes) if pes else None
    r.peg = _safe(info, "trailingPegRatio") or _safe(info, "pegRatio")
    r.roe = _safe(info, "returnOnEquity")
    r.op_margin = _safe(info, "operatingMargins")
    de_raw = _safe(info, "debtToEquity")
    r.de_ratio = (de_raw / 100) if de_raw is not None else None
    r.fcf = _safe(info, "freeCashflow")
    r.quick = _safe(info, "quickRatio")
    r.net_margin = _safe(info, "profitMargins")
    r.financial_currency = info.get("financialCurrency")
    # Market cap is in dollars, free cash flow in the statements' currency: a
    # foreign reporter's yield waits for an exchange rate (_set_fcf_yields).
    if r.fcf is not None and r.market_cap and r.financial_currency in (None, "USD"):
        r.fcf_yield = r.fcf / r.market_cap

    r.rec_avg = _safe(info, "recommendationMean")
    na = info.get("numberOfAnalystOpinions")
    r.num_analysts = int(na) if na else None
    r.target_mean = _safe(info, "targetMeanPrice")
    if r.price and r.target_mean and r.target_mean > 0:
        r.upside_pct = round((r.target_mean - r.price) / r.price * 100, 1)

    r.prefilter_failed = sum(1 for ok in _prefilter_passes(r).values() if not ok)
    return r


def _workers(max_workers: Optional[int] = None) -> int:
    """Thread-pool size for the scan. Moderate by default: Yahoo rate-limits
    aggressive bursts and SEC EDGAR allows ~10 req/s."""
    if max_workers is not None:
        return max(1, max_workers)
    try:
        return max(1, int(os.environ.get("SCREEN_MAX_WORKERS", "6")))
    except ValueError:
        return 6


def prefilter_universe(tickers: list[str], *, budget: yl.YahooBudget,
                       max_workers: Optional[int] = None, log_every: int = 50,
                       verbose: bool = True) -> tuple[list[ScreenResult], list[str]]:
    """Stage 1 over `tickers` until `budget` runs out.

    Returns (results, left): a result for every name fetched — or failed for
    good, carrying its `error` — in input order, and the names to try again on
    a later run because Yahoo refused or the budget ran out first."""
    total = len(tickers)
    results: dict[str, ScreenResult] = {}
    lock = threading.Lock()
    done = [0]
    start = time.time()

    def fetch(t: str) -> bool:
        r = prefilter_one(t)
        refused = yl.is_rate_limited(r.error)
        if not refused:
            with lock:
                results[t] = r
        return refused

    def progress(_t: str) -> None:
        with lock:
            done[0] += 1
            n = done[0]
            short = sum(1 for r in results.values() if not r.error
                        and r.prefilter_failed <= PREFILTER_MAX_FAILED)
        if verbose and (n % log_every == 0 or n == total):
            elapsed = time.time() - start
            rate = n / elapsed if elapsed > 0 else 0
            print(f"  [{n:>4}/{total}]  {rate:.1f}/s  shortlisted so far: {short}")

    left = yl.run_gated(tickers, fetch, budget=budget,
                        workers=_workers(max_workers), on_done=progress)
    return [results[t] for t in tickers if t in results], left


# ============================================================
# Shortlist and split
# ============================================================
# Names within this many prefilter misses go on to full scoring, as does every
# name the cash-flow channel can use (fcf_eligible), whatever it misses. The rough
# reading disagrees with stage 2 mostly on growth (a weak last year inside a
# strong three) and on trailing versus averaged ROE and margins, so the
# allowance has to cover a few flips at once. Measured over the whole universe
# on 2026-10-05: all 13 names that passed the nine filters had at most 2
# prefilter misses; of the ~163 the table showed, 3 misses caught 148 (91%)
# and 2 caught 121 (74%). At 3, about 500 of the ~900 names are fully scored.
PREFILTER_MAX_FAILED = 3
# The table's near misses: names stage 2 found failing one or two filters.
NEAR_MISS_MAX_FAILED = 2


def split_passers_and_near_misses(
    results: list[ScreenResult],
) -> tuple[list[ScreenResult], list[ScreenResult]]:
    """Return (passed, near_miss), each best Composite first. Passed = all
    nine filters; near miss = one or two failed."""
    passed = [r for r in results if r.num_passed == 9 and not r.error]
    near_miss = [r for r in results
                 if 1 <= r.num_failed <= NEAR_MISS_MAX_FAILED and not r.error]
    passed.sort(key=lambda r: (r.score_composite or -1), reverse=True)
    near_miss.sort(key=lambda r: (r.score_composite or -1), reverse=True)
    return passed, near_miss


# ============================================================
# Once-a-day universe scan, spread over runs
# ============================================================
# The scan is the slowest thing the analyzer can do. Measured on 2026-10-21:
# 9 screener requests for 1,887 names, a prefilter request for each, and ~7
# for each of the 1,128 within PREFILTER_MAX_FAILED misses, ~9,800 Yahoo
# requests in all (the S&P 500 + 400 took ~4,400). It is cached per market
# day in .cache/ (gitignored; carried across CI runs by the Actions cache)
# together with what is still to do. Each run spends at most the request
# budget on it and leaves the rest to the next run, so a day's scan completes
# over the first ~7 runs: the 7:00 ET run and the first hour after the open.
# The likeliest passers are scored first, so additions land early.

_SCREEN_CACHE_PATH = Path(__file__).resolve().parent / ".cache" / "screen.json"
_ET = ZoneInfo("America/New_York")
# Recorded in the cache so a scan scored some other way — the screener's own
# composite, before 2026-10-06 — is never read back as today's.
_CACHE_SCORING = "report"


def _request_budget() -> int:
    """The Yahoo requests one run's scan may make (SCREEN_REQUEST_BUDGET).
    Well under the few thousand after which Yahoo blocks a runner, so the
    holdings analysis later in the same run still has room."""
    try:
        return max(0, int(os.environ.get("SCREEN_REQUEST_BUDGET", "1500")))
    except ValueError:
        return 1500


def _market_today() -> str:
    """Today's date in market time — the cache key for a day's scan."""
    return _dt.datetime.now(_ET).date().isoformat()


def _load_screen_state(verbose: bool = True) -> Optional[dict]:
    """Today's scan as far as it got, or None to start one."""
    try:
        cached = json.loads(_SCREEN_CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if (not isinstance(cached, dict) or cached.get("date") != _market_today()
            or cached.get("scoring") != _CACHE_SCORING
            or cached.get("universe") != _universe_key()):
        return None
    shown = cached.get("shown")
    if shown is None:      # written before scans were spread over runs
        shown = (cached.get("passed") or []) + (cached.get("near_miss") or [])
    try:
        state = {
            "scanned_at": cached.get("scanned_at"),
            "universe_size": cached.get("universe_size", 0),
            "source": cached.get("source") or "us",
            "sp_members": list(cached.get("sp_members") or []),
            "fx": dict(cached.get("fx") or {}),
            "unscreened": list(cached.get("unscreened") or []),
            "to_prefilter": list(cached.get("to_prefilter") or []),
            "to_score": [ScreenResult(**d) for d in cached.get("to_score") or []],
            "shown": [ScreenResult(**d) for d in shown],
        }
    except TypeError:
        return None      # cache written by an older ScreenResult shape
    if verbose:
        pending = len(state["to_prefilter"]) + len(state["to_score"])
        print(f"[screen] {'Continuing' if pending else 'Reusing'} today's scan "
              f"from {_SCREEN_CACHE_PATH} (started {state['scanned_at']}; "
              f"{len(state['shown'])} shown so far"
              + (f", {len(state['to_prefilter'])} to prefilter, "
                 f"{len(state['to_score'])} to score" if pending else "")
              + ").")
    return state


def _save_screen_state(state: dict, verbose: bool = True) -> None:
    try:
        _SCREEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SCREEN_CACHE_PATH.write_text(json.dumps({
            "date": _market_today(),
            "scoring": _CACHE_SCORING,
            "universe": _universe_key(),
            "scanned_at": state["scanned_at"],
            "universe_size": state["universe_size"],
            "source": state["source"],
            "sp_members": state["sp_members"],
            "fx": state["fx"],
            "unscreened": state["unscreened"],
            "to_prefilter": state["to_prefilter"],
            "to_score": [asdict(r) for r in state["to_score"]],
            "shown": [asdict(r) for r in state["shown"]],
        }))
    except OSError as e:
        if verbose:
            print(f"[screen] Could not write scan cache ({e})")


def _scan_output(state: dict, from_cache: bool, refused: bool = False) -> dict:
    passed, near = split_passers_and_near_misses(state["shown"])
    cash = sorted((r for r in state["shown"] if fcf_eligible(r)
                   and r.score_composite is not None),
                  key=lambda r: r.score_composite, reverse=True)
    return {
        "passed": passed,
        "near_miss": near,
        "fcf": cash,
        "universe_size": state["universe_size"],
        "unscreened": sorted(state["unscreened"]),
        "pending": len(state["to_prefilter"]) + len(state["to_score"]),
        "scanned_at": state["scanned_at"],
        "from_cache": from_cache,
        "refused": refused,
    }


# score(shortlist, budget=, max_workers=, verbose=) scores what it can of the
# shortlist in place and returns the results it didn't get to.
Scorer = Callable[..., list]

# The S&P list is "real" above this many names; below it, fetch_sp500_sp400
# fell back to its mega-cap sample and membership can't be told.
_MIN_SP_LIST = 100


def _new_state(limit: Optional[int], verbose: bool) -> Optional[dict]:
    """A new day's scan: the universe and the S&P membership list. None when
    Yahoo refused the universe fetch."""
    try:
        universe, source = fetch_universe(verbose=verbose)
    except Exception as e:
        if not yl.is_rate_limited(e):
            raise
        if verbose:
            print("[screen] Yahoo refused the universe fetch; the next run "
                  "starts today's scan.")
        return None
    sp = universe if source == "sp" else fetch_sp500_sp400(verbose=verbose)
    if limit:
        universe = universe[:limit]
        if verbose:
            print(f"[screen] Limiting to first {limit} tickers (test run; "
                  f"not cached).")
    return {
        "scanned_at": _dt.datetime.now(_ET).isoformat(timespec="seconds"),
        "universe_size": len(universe), "source": source,
        "sp_members": sorted(sp) if len(sp) > _MIN_SP_LIST else [],
        "fx": {}, "unscreened": [], "to_prefilter": list(universe),
        "to_score": [], "shown": [],
    }


def screen_universe(
    score: Scorer,
    limit: Optional[int] = None,
    max_workers: Optional[int] = None,
    force: bool = False,
    verbose: bool = True,
) -> Optional[dict]:
    """Scan the US universe (fetch_universe) once per market day, over as many
    runs as the request budget needs. None when Yahoo refused the universe
    fetch itself, so there is nothing to show yet.

    `score` gets the shortlist and fills in, on each result it finishes, the
    nine filters (passes / num_passed / num_failed), the sub-scores, the
    Composite and the insider read — or an `error`. It returns the results it
    didn't get to (budget spent, or Yahoo refused), which wait for the next
    run. Its Yahoo work should go through the budget it is handed.

    Returns {"passed", "near_miss", "fcf", "universe_size", "unscreened",
    "pending", "scanned_at", "from_cache", "refused"}. "fcf" holds the
    cash-flow channel's candidates (fcf_eligible), scored like the rest.
    `pending` counts the names still to prefilter or score; `unscreened` lists
    the names Yahoo had no data for; `refused` says Yahoo turned this run
    away, so more Yahoo work now would fail too. `limit` caps the universe for
    a fast test, lifts the request budget, and neither reads nor writes the
    day's cache, so a test run can't stand in for the real scan.
    """
    state = None if (force or limit) else _load_screen_state(verbose=verbose)
    if state and not state["to_prefilter"] and not state["to_score"]:
        return _scan_output(state, from_cache=True)

    yl.install()
    # The budget starts before the universe fetch, which spends from it too.
    budget = yl.YahooBudget(None if limit else _request_budget())
    if state is None:
        state = _new_state(limit, verbose)
        if state is None:
            return None
    sp = set(state["sp_members"])
    workers = _workers(max_workers)

    if state["to_prefilter"]:
        start = time.time()
        fetched, left = prefilter_universe(state["to_prefilter"], budget=budget,
                                           max_workers=workers, verbose=verbose)
        for r in fetched:
            r.in_sp = (r.ticker in sp) if sp else None
        retry = {id(r) for r in _set_fcf_yields(fetched, state["fx"], budget)}
        if retry:
            left = left + [r.ticker for r in fetched if id(r) in retry]
            fetched = [r for r in fetched if id(r) not in retry]
        state["unscreened"] += [r.ticker for r in fetched if r.error]
        # Fewest misses first: the names likeliest to pass are scored first.
        # A cash-flow candidate goes among the names one miss out, so both
        # channels' additions land in the first runs.
        state["to_score"] = sorted(
            state["to_score"] + [r for r in fetched if not r.error and (
                r.prefilter_failed <= PREFILTER_MAX_FAILED or fcf_eligible(r))],
            key=lambda r: (min(r.prefilter_failed, 1) if fcf_eligible(r)
                           else r.prefilter_failed))
        state["to_prefilter"] = left
        if verbose:
            cash = sum(1 for r in state["to_score"] if fcf_eligible(r))
            print(f"[screen] Prefilter: {len(fetched)} fetched in "
                  f"{time.time() - start:.0f}s; {len(state['to_score'])} queued "
                  f"for full scoring (within {PREFILTER_MAX_FAILED} misses, or "
                  f"one of {cash} cash-flow candidates)"
                  + (f"; {len(left)} left for the next run" if left else "")
                  + ".")

    if state["to_score"] and not budget.spent():
        batch = state["to_score"]
        left = score(batch, budget=budget, max_workers=workers,
                     verbose=verbose) or []
        unfinished = {id(r) for r in left}
        done = [r for r in batch if id(r) not in unfinished]
        state["unscreened"] += [r.ticker for r in done if r.error]
        passed, near = split_passers_and_near_misses(done)
        kept = {id(r) for r in passed + near}
        cash = [r for r in done if id(r) not in kept and fcf_eligible(r)]
        state["shown"] += passed + near + cash
        state["to_score"] = left

    out = _scan_output(state, from_cache=False, refused=budget.stopped)
    if verbose and out["pending"]:
        why = "Yahoo refused" if budget.stopped else "request budget spent"
        print(f"[screen] {out['pending']} name(s) left for the next run "
              f"({why} after {budget.used()} requests).")
    if not limit:
        _save_screen_state(state, verbose=verbose)
    return out


if __name__ == "__main__":
    # Manual check: a handful of names through both stages, scored the way the
    # report scores them.
    from analyze_portfolio import score_screen_shortlist

    test = ["AAPL", "MSFT", "GOOGL", "META", "NVDA", "MU", "TSLA", "JNJ"]
    print(f"Screening {len(test)} test tickers...")
    yl.install()
    budget = yl.YahooBudget()
    res, _ = prefilter_universe(test, budget=budget, log_every=1)
    score_screen_shortlist([r for r in res if not r.error], budget=budget,
                           max_workers=4)
    passed, near = split_passers_and_near_misses(res)
    print(f"\nPassed (9/9): {[r.ticker for r in passed]}")
    print(f"Near-miss (7-8/9): {[(r.ticker, r.num_passed) for r in near]}")
    for r in passed + near[:3]:
        print(f"  {r.ticker:6s} passed={r.num_passed}/9 "
              f"composite={r.score_composite} Q={r.score_quality} "
              f"G={r.score_growth} V={r.score_value} A={r.score_analyst} "
              f"I={r.score_insider}")
