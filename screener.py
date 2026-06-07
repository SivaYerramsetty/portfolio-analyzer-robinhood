"""
screener.py
-----------
Screens the S&P 500 + S&P 400 universe against the 9-filter quality framework
and computes a composite score for ranking the passers.

Filter set (matches analyze_portfolio.py):
    1. Revenue growth >= 10% YoY
    2. EPS growth >= 10% YoY
    3. P/E < 30
    4. PEG < 2
    5. ROE >= 15%
    6. Operating margin >= 15%
    7. Debt/Equity < 1
    8. FCF positive & growing YoY
    9. Quick ratio > 1.0

Composite score weighting (0-100):
    Quality 35% · Growth 25% · Value 20% · Analyst 20%

Per-stock metrics surfaced to the HTML report:
    RecAvg     — Yahoo's analyst recommendation mean (1 strong buy ... 5 strong sell)
    52w Pos    — where price sits in 52-week range (0% = low, 100% = high)
    #F         — number of filters FAILED (we list 0, 1, 2 in the "near misses" view)

Allows shows in the screen output:
    - "passed" (0 failed): primary list
    - "near_miss" (1-2 failed): runners-up for visibility
"""

from __future__ import annotations

import time
import io
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests
import pandas as pd
import yfinance as yf


# ============================================================
# Universe fetch — S&P 500 + S&P 400
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
# Per-stock fundamentals fetch + 9-filter scoring
# ============================================================

def _safe(d: dict, k: str) -> Optional[float]:
    v = d.get(k)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fcf_growing(tkr: yf.Ticker) -> Optional[bool]:
    """Same FCF-growing logic used in analyze_portfolio.py."""
    try:
        cf = tkr.cashflow
        if cf is None or cf.empty:
            return None
        s = None
        if "Free Cash Flow" in cf.index:
            s = cf.loc["Free Cash Flow"].dropna()
        elif ("Operating Cash Flow" in cf.index) and ("Capital Expenditure" in cf.index):
            s = (cf.loc["Operating Cash Flow"] + cf.loc["Capital Expenditure"]).dropna()
        if s is None or len(s) < 2:
            return None
        cur = float(s.iloc[0])
        prv = float(s.iloc[1])
        return cur > prv and cur > 0
    except Exception:
        return None


@dataclass
class ScreenResult:
    ticker: str
    name: str = ""
    sector: Optional[str] = None
    industry: Optional[str] = None
    price: Optional[float] = None
    week52_high: Optional[float] = None
    week52_low: Optional[float] = None
    week52_pos: Optional[float] = None
    # Raw metrics
    rev_growth: Optional[float] = None      # decimal
    eps_growth: Optional[float] = None
    pe: Optional[float] = None
    peg: Optional[float] = None
    roe: Optional[float] = None
    op_margin: Optional[float] = None
    de_ratio: Optional[float] = None
    fcf: Optional[float] = None
    fcf_growing: Optional[bool] = None
    quick: Optional[float] = None
    # Analyst
    rec_avg: Optional[float] = None         # 1 = strong buy, 5 = strong sell
    num_analysts: Optional[int] = None
    target_mean: Optional[float] = None
    upside_pct: Optional[float] = None
    # Filter pass/fail
    passes: dict[str, bool] = field(default_factory=dict)
    num_passed: int = 0
    num_failed: int = 9
    # Sub-scores (0-100) and composite (0-100)
    score_quality: Optional[float] = None
    score_growth: Optional[float] = None
    score_value: Optional[float] = None
    score_analyst: Optional[float] = None
    score_insider: Optional[float] = None
    insider_activity: Optional[dict] = None
    score_composite: Optional[float] = None
    # Errors
    error: Optional[str] = None


def screen_one(ticker: str) -> ScreenResult:
    """Pull fundamentals for one ticker and apply the 9 filters."""
    r = ScreenResult(ticker=ticker)
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        if not info:
            r.error = "no info"
            return r

        r.name = info.get("shortName") or info.get("longName") or ticker
        r.sector = info.get("sector")
        r.industry = info.get("industry")
        r.price = _safe(info, "regularMarketPrice") or _safe(info, "currentPrice")
        r.week52_high = _safe(info, "fiftyTwoWeekHigh")
        r.week52_low = _safe(info, "fiftyTwoWeekLow")
        if r.price and r.week52_high and r.week52_low and r.week52_high > r.week52_low:
            r.week52_pos = round(
                (r.price - r.week52_low) / (r.week52_high - r.week52_low) * 100, 1
            )

        r.rev_growth = _safe(info, "revenueGrowth")
        r.eps_growth = _safe(info, "earningsGrowth")
        r.pe = _safe(info, "trailingPE") or _safe(info, "forwardPE")
        r.peg = _safe(info, "trailingPegRatio") or _safe(info, "pegRatio")
        r.roe = _safe(info, "returnOnEquity")
        r.op_margin = _safe(info, "operatingMargins")
        de_raw = _safe(info, "debtToEquity")
        r.de_ratio = (de_raw / 100) if de_raw is not None else None
        r.fcf = _safe(info, "freeCashflow")
        r.fcf_growing = _fcf_growing(t)
        r.quick = _safe(info, "quickRatio")

        r.rec_avg = _safe(info, "recommendationMean")
        na = info.get("numberOfAnalystOpinions")
        r.num_analysts = int(na) if na else None
        r.target_mean = _safe(info, "targetMeanPrice")
        if r.price and r.target_mean and r.target_mean > 0:
            r.upside_pct = round((r.target_mean - r.price) / r.price * 100, 1)

        # Apply filters
        p = {
            "rev_growth": (r.rev_growth is not None and r.rev_growth >= 0.10),
            "eps_growth": (r.eps_growth is not None and r.eps_growth >= 0.10),
            "pe": (r.pe is not None and 0 < r.pe < 30),
            "peg": (r.peg is not None and 0 < r.peg < 2),
            "roe": (r.roe is not None and r.roe >= 0.15),
            "op_margin": (r.op_margin is not None and r.op_margin >= 0.15),
            "de": (r.de_ratio is not None and r.de_ratio < 1),
            "fcf": (r.fcf is not None and r.fcf > 0 and r.fcf_growing is not False),
            "quick": (r.quick is not None and r.quick > 1.0),
        }
        r.passes = p
        r.num_passed = sum(1 for v in p.values() if v)
        r.num_failed = 9 - r.num_passed

        # Sub-scores
        r.score_quality = _quality_subscore(r)
        r.score_growth = _growth_subscore(r)
        r.score_value = _value_subscore(r)
        r.score_analyst = _analyst_subscore(r)

        # Insider activity (optional — only if module available)
        try:
            from insider_trading import get_insider_activity, insider_score
            r.insider_activity = get_insider_activity(ticker, lookback_days=90)
            r.score_insider = insider_score(
                r.insider_activity,
                market_cap=info.get("marketCap"),
            )
            if r.insider_activity is not None:
                r.insider_activity["_score"] = r.score_insider
        except Exception:
            pass

        # Composite weights (matches analyze_portfolio.py)
        weights = {
            "quality": 0.30, "growth": 0.20,
            "value": 0.20, "analyst": 0.15, "insider": 0.15,
        }
        parts, total_w = 0.0, 0.0
        for k, w in weights.items():
            sub = getattr(r, f"score_{k}")
            if sub is not None:
                parts += sub * w
                total_w += w
        r.score_composite = round(parts / total_w, 1) if total_w > 0 else None

    except Exception as e:
        r.error = f"{type(e).__name__}: {e}"
    return r


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _quality_subscore(r: ScreenResult) -> Optional[float]:
    """0-100 based on ROE, op margin, D/E, FCF positivity, quick ratio."""
    parts = []
    if r.roe is not None:
        parts.append(_clip01(r.roe / 0.30) * 100)            # cap at 30% ROE
    if r.op_margin is not None:
        parts.append(_clip01(r.op_margin / 0.30) * 100)
    if r.de_ratio is not None:
        parts.append(_clip01(1 - r.de_ratio / 2) * 100)      # 0 -> 100, 2.0 -> 0
    if r.fcf is not None:
        parts.append(100 if (r.fcf > 0 and r.fcf_growing is not False) else 0)
    if r.quick is not None:
        parts.append(_clip01((r.quick - 0.5) / 1.5) * 100)
    return round(sum(parts) / len(parts), 1) if parts else None


def _growth_subscore(r: ScreenResult) -> Optional[float]:
    parts = []
    if r.rev_growth is not None:
        parts.append(_clip01(r.rev_growth / 0.30) * 100)     # cap at 30% rev growth
    if r.eps_growth is not None:
        parts.append(_clip01(r.eps_growth / 0.30) * 100)
    return round(sum(parts) / len(parts), 1) if parts else None


def _value_subscore(r: ScreenResult) -> Optional[float]:
    """Lower P/E and PEG = higher score."""
    parts = []
    if r.pe is not None and r.pe > 0:
        parts.append(_clip01(1 - (r.pe / 40)) * 100)         # 0 -> 100, 40 -> 0
    if r.peg is not None and r.peg > 0:
        parts.append(_clip01(1 - (r.peg / 3)) * 100)
    return round(sum(parts) / len(parts), 1) if parts else None


def _analyst_subscore(r: ScreenResult) -> Optional[float]:
    """Lower recAvg (1 = strong buy) + higher upside = higher score."""
    parts = []
    if r.rec_avg is not None and r.rec_avg > 0:
        # 1.0 (strong buy) -> 100; 3.0 (hold) -> 50; 5.0 (strong sell) -> 0
        parts.append(_clip01((5 - r.rec_avg) / 4) * 100)
    if r.upside_pct is not None:
        # 0% -> 50; +30% -> 100; -30% -> 0
        parts.append(_clip01(0.5 + r.upside_pct / 60) * 100)
    return round(sum(parts) / len(parts), 1) if parts else None


# ============================================================
# Batch screening with progress + throttling
# ============================================================

def run_screen(
    tickers: list[str],
    sleep_sec: float = 0.05,
    log_every: int = 25,
    max_tickers: Optional[int] = None,
    verbose: bool = True,
) -> list[ScreenResult]:
    """Screen all tickers. yfinance is rate-friendly but we sleep a bit anyway."""
    if max_tickers:
        tickers = tickers[:max_tickers]
    total = len(tickers)
    out: list[ScreenResult] = []
    start = time.time()
    for i, tk in enumerate(tickers, 1):
        r = screen_one(tk)
        out.append(r)
        if verbose and (i % log_every == 0 or i == total):
            elapsed = time.time() - start
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            passers = sum(1 for s in out if s.num_passed == 9)
            print(f"  [{i:>4}/{total}]  {rate:.1f}/s  ETA {eta/60:.1f}m  "
                  f"passers so far: {passers}")
        if sleep_sec > 0:
            time.sleep(sleep_sec)
    return out


def split_passers_and_near_misses(
    results: list[ScreenResult],
) -> tuple[list[ScreenResult], list[ScreenResult]]:
    """Return (passed, near_miss). Passed = all 9 filters. Near-miss = 1-2 failed."""
    passed = [r for r in results if r.num_passed == 9 and not r.error]
    near_miss = [r for r in results if r.num_failed in (1, 2) and not r.error]
    # Sort each by composite descending (best first)
    passed.sort(key=lambda r: (r.score_composite or -1), reverse=True)
    near_miss.sort(key=lambda r: (r.score_composite or -1), reverse=True)
    return passed, near_miss


if __name__ == "__main__":
    # Quick manual test: screen a handful of known tickers
    test = ["AAPL", "MSFT", "GOOGL", "META", "NVDA", "MU", "TSLA", "JNJ"]
    print(f"Screening {len(test)} test tickers...")
    res = run_screen(test, sleep_sec=0, log_every=1)
    passed, near = split_passers_and_near_misses(res)
    print(f"\nPassed (9/9): {[r.ticker for r in passed]}")
    print(f"Near-miss (7-8/9): {[(r.ticker, r.num_passed) for r in near]}")
    for r in passed + near[:3]:
        print(f"  {r.ticker:6s} passed={r.num_passed}/9 composite={r.score_composite} "
              f"Q={r.score_quality} G={r.score_growth} V={r.score_value} A={r.score_analyst}")
