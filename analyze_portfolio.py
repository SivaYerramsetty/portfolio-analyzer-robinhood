"""
analyze_portfolio.py
--------------------
Analyzes Robinhood portfolio holdings using:
  - 9-filter quality compounder framework (matches existing screener)
  - Analyst price targets & ratings (via Robinhood, Finnhub, or yfinance)
  - Strict verdict logic: SELL if fails 3+ quality filters AND above target

ETFs / thematic / speculative positions go in a separate, simpler bucket.
The HTML report has sortable columns and a filter bar (search + quick-filter pills).

============================================================================
COMMAND REFERENCE — every way to run this script
============================================================================

--- SETUP (one-time) ------------------------------------------------------

    # Install dependencies
    pip install yfinance pdfplumber robin-stocks python-dotenv requests pandas
    # (pyotp only needed if using legacy TOTP auth)

    # Create a .env file in the project folder with your credentials:
    #     RH_USERNAME=your_email@example.com
    #     RH_PASSWORD=your_password
    #     FINNHUB_API_KEY=your_key        (optional, richer analyst ratings)
    #     SMTP_HOST=smtp.gmail.com        (optional, for --email)
    #     SMTP_PORT=587
    #     SMTP_USER=your_sending_email
    #     SMTP_PASS=your_app_password
    #     EMAIL_TO=where_to_send

--- MODE 1: LIVE ROBINHOOD (fetches your real positions) ------------------

    # Basic — pull live positions, analyze, write report
    python analyze_portfolio.py --source robinhood --out report.html

    # Add Robinhood watchlists ("should I buy?" section)
    python analyze_portfolio.py --source robinhood --include-watchlists --out report.html

    # Save a CSV snapshot of positions alongside the report (for audit history)
    python analyze_portfolio.py --source robinhood --save-positions positions.csv --out report.html

    # The full daily-driver command (positions + watchlists + snapshot + email)
    python analyze_portfolio.py --source robinhood --include-watchlists \
        --save-positions positions.csv --out report.html --email

    # Then open the report (macOS)
    open report.html

--- MODE 2: CSV (analyze a saved/parsed positions file, no Robinhood) -----

    # First parse a Robinhood monthly statement PDF into a CSV:
    python parse_statement.py "/path/to/statement.pdf" positions.csv

    # Then analyze that CSV
    python analyze_portfolio.py positions.csv --out report.html

    # CSV mode with email
    python analyze_portfolio.py positions.csv --out report.html --email

--- MODE 3: AD-HOC TICKERS (quick lookup, NO Robinhood, NO holdings) ------

    # Single stock
    python analyze_portfolio.py --tickers AAPL --out lookup.html

    # Multiple stocks (comma-separated)
    python analyze_portfolio.py --tickers AAPL,MSFT,GOOGL,NVDA --out lookup.html

    # Multiple stocks (space-separated, must be quoted)
    python analyze_portfolio.py --tickers "AAPL MSFT GOOGL NVDA" --out lookup.html

    # Ad-hoc lookup emailed to you
    python analyze_portfolio.py --tickers AAPL,MSFT --out lookup.html --email

--- MODE 4: ADD TO ROBINHOOD WATCHLIST (no report; write-only) -------------

    # Append tickers to an existing watchlist (skips ones already in it).
    # Requires the watchlist to exist already (create it in the app first).
    python analyze_portfolio.py --add-to-watchlist "AI Plays" --tickers NVDA,GOOGL

    # Preview without writing
    python analyze_portfolio.py --add-to-watchlist "AI Plays" --tickers NVDA --sync-dry-run

    # Bulk-add a longer list
    python analyze_portfolio.py --add-to-watchlist "Dividend Stocks" \
        --tickers "JNJ KO PEP PG MO"

--- STANDALONE MODULE CHECKS (test individual pieces) ---------------------

    # Test Robinhood login + print top holdings (sanity check auth)
    python robinhood_source.py

    # Just parse a statement PDF to CSV without analyzing
    python parse_statement.py "/path/to/statement.pdf" positions.csv

--- ENABLING RICHER ANALYST DATA ------------------------------------------

    # Finnhub adds a Buy/Hold/Sell breakdown bar (free key at finnhub.io).
    # Set FINNHUB_API_KEY in .env, OR inline for one run:
    FINNHUB_API_KEY=your_key python analyze_portfolio.py --tickers NVDA --out nvda.html

--- ALL FLAGS -------------------------------------------------------------

    positions_csv          Positional. CSV from parse_statement.py (CSV mode only).
    --source {csv,robinhood}   Where to load positions (default: csv).
    --tickers TICKERS      Ad-hoc mode: comma/space separated symbols. Skips
                           Robinhood + holdings entirely (no auth needed).
    --include-watchlists   With --source robinhood: also analyze your watchlists.
    --save-positions FILE  With --source robinhood: dump positions snapshot CSV.
    --out FILE             Output HTML path (default: portfolio_report.html).
    --email                Also send the report via SMTP (uses env vars).
    --screen               Run S&P 500/400 screen; adds Screening section.
    --screen-limit N       Cap screened universe for testing.
    --sync-screening-watchlist  Sync passing tickers to "Screening" watchlist.
    --add-to-watchlist NAME  Append --tickers to a Robinhood watchlist (no report).
    --sync-dry-run         Preview --add-to-watchlist / --sync without writing.
    --debug-insider TICKER Diagnose insider data sources for one stock.

--- HTML REPORT FEATURES (no flags needed; always on) ---------------------

    • Click any column header to sort; click again to reverse.
    • Filter bar at top: search box (ticker/name) + quick-filter pills:
        All · Action items · Buy signals · High quality (7+) · Winners · Losers
    • Live prices via yfinance; analyst ratings via Robinhood/Finnhub/yfinance.

--- GITHUB ACTIONS (manual trigger, see portfolio.yml) --------------------

    # Runs automatically in CI when you click "Run workflow" in the Actions tab.
    # Equivalent command it runs:
    #   python analyze_portfolio.py --source robinhood --include-watchlists \
    #       --save-positions positions.csv --out report.html --email

============================================================================
"""

from __future__ import annotations
from zoneinfo import ZoneInfo   # Python 3.9+; add near top of file if not already there
import math as _math
import argparse
import csv
import os
import smtplib
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import yfinance as yf

try:
    import requests  # for Finnhub
except ImportError:
    requests = None

# Finnhub free tier: 60 calls/min. Get a free key at https://finnhub.io
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()


# ============================================================
# Classification: which positions get the full framework
# ============================================================

# Known ETF tickers in this portfolio (extensible). The script also auto-detects
# ETFs via yfinance quoteType, so this list is a fallback / override.
KNOWN_ETFS = {"COPX", "LIT", "SLV", "GLD", "SETM", "SPY", "QQQ", "VTI", "VOO"}

# Speculative / thematic plays that don't fit the compounder framework.
# These get the simpler price-vs-target treatment.
THEMATIC_OVERRIDES = {
    "MSTR",  # Bitcoin proxy
    "BMNR",  # Crypto / bitcoin mining
    "AI",    # C3.AI - unprofitable small cap
}


def classify_position(ticker: str, info: dict) -> str:
    """Return 'compounder' or 'thematic'."""
    if ticker in KNOWN_ETFS:
        return "thematic"
    if ticker in THEMATIC_OVERRIDES:
        return "thematic"
    quote_type = (info.get("quoteType") or "").upper()
    if quote_type in {"ETF", "MUTUALFUND", "CURRENCY", "CRYPTOCURRENCY"}:
        return "thematic"
    return "compounder"


# ============================================================
# Sector "hot / cool" — driven by live SPDR sector-ETF momentum
# ============================================================

# yfinance `sector` value -> representative SPDR sector ETF.
SECTOR_ETF = {
    "Technology": "XLK",
    "Healthcare": "XLV",
    "Financial Services": "XLF",
    "Financial": "XLF",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
    "Communication Services": "XLC",
}

# Cache so each sector ETF is fetched at most once per run.
_SECTOR_MOMENTUM_CACHE: dict[str, dict] = {}


def get_sector_momentum(sector: Optional[str]) -> dict:
    """
    Classify a sector as Hot / Neutral / Cool from its SPDR ETF's momentum.

    Returns: {"label": "Hot"|"Neutral"|"Cool"|"Unknown",
              "etf": "XLK", "pct_vs_200ma": float|None, "color": css}
    Uses the ETF's price vs 50/200-day moving averages (from yfinance .info),
    consistent with the thematic-trend logic elsewhere. Cached per ETF.
    """
    if not sector:
        return {"label": "Unknown", "etf": None, "pct_vs_200ma": None,
                "color": "#bdc3c7"}
    etf = SECTOR_ETF.get(sector)
    if not etf:
        return {"label": "Unknown", "etf": None, "pct_vs_200ma": None,
                "color": "#bdc3c7"}
    if etf in _SECTOR_MOMENTUM_CACHE:
        return _SECTOR_MOMENTUM_CACHE[etf]

    result = {"label": "Unknown", "etf": etf, "pct_vs_200ma": None,
              "color": "#bdc3c7"}
    try:
        info = yf.Ticker(etf).info or {}
        price = _safe_get(info, "regularMarketPrice") or _safe_get(info, "currentPrice")
        ma50 = _safe_get(info, "fiftyDayAverage")
        ma200 = _safe_get(info, "twoHundredDayAverage")
        if price and ma200 and ma200 > 0:
            pct = (price / ma200 - 1) * 100
            result["pct_vs_200ma"] = round(pct, 1)
            uptrend_cross = (ma50 is not None and ma50 > ma200)
            downtrend_cross = (ma50 is not None and ma50 < ma200 * 0.99)
            if pct >= 3 and uptrend_cross:
                result["label"] = "Hot"
                result["color"] = "#c0392b"   # warm red
            elif pct <= -2 or downtrend_cross:
                result["label"] = "Cool"
                result["color"] = "#2980b9"   # cool blue
            else:
                result["label"] = "Neutral"
                result["color"] = "#7f8c8d"
    except Exception:
        pass

    _SECTOR_MOMENTUM_CACHE[etf] = result
    return result


# ============================================================
# Finnhub: richer analyst data (optional)
# ============================================================

def fetch_finnhub_recommendation(ticker: str) -> Optional[dict]:
    """
    Returns latest analyst rating breakdown like Robinhood shows:
        {"strongBuy": 15, "buy": 22, "hold": 5, "sell": 1, "strongSell": 0,
         "period": "2026-04-01", "total": 43}
    Returns None if no key configured or fetch failed.
    """
    if not FINNHUB_API_KEY or not requests:
        return None
    try:
        url = "https://finnhub.io/api/v1/stock/recommendation"
        r = requests.get(
            url,
            params={"symbol": ticker, "token": FINNHUB_API_KEY},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if not data:
            return None
        latest = data[0]  # Finnhub returns most-recent first
        total = (latest.get("strongBuy", 0) + latest.get("buy", 0)
                 + latest.get("hold", 0) + latest.get("sell", 0)
                 + latest.get("strongSell", 0))
        latest["total"] = total
        return latest
    except Exception:
        return None


def fetch_finnhub_price_target(ticker: str) -> Optional[dict]:
    """
    Returns {"targetHigh": ..., "targetLow": ..., "targetMean": ...,
             "targetMedian": ..., "lastUpdated": ...} or None.
    """
    if not FINNHUB_API_KEY or not requests:
        return None
    try:
        url = "https://finnhub.io/api/v1/stock/price-target"
        r = requests.get(
            url,
            params={"symbol": ticker, "token": FINNHUB_API_KEY},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


# ============================================================
# Quality filters (matches existing screener: nine gates)
# ============================================================

@dataclass
class FilterResult:
    name: str
    passed: bool
    actual: Optional[float]
    threshold: str
    note: str = ""


def _safe_get(d: dict, key: str) -> Optional[float]:
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _compute_multi_year_growth(tkr, info: dict) -> None:
    """Compute multi-year quality metrics from annual financials.

    Mutates `info` in-place with new keys (all decimals where applicable):
      revenueCAGR3y          — 3-year revenue CAGR, or None
      earningsCAGR3y         — 3-year net-income CAGR, or None
      revenueGrowthLookback  — "3yr CAGR" | "2yr CAGR" | "1yr YoY" | None
      earningsGrowthLookback — same
      roeAvg3y               — 3-year average ROE (0.18 = 18%), or None
      roeLookback            — "3yr avg" | "2yr avg" | None
      operatingMarginAvg3y   — 3-year average operating margin, or None
      operatingMarginLookback— same shape
      fcfConsistency         — dict {positive_years, total_years, growing} or None
      fcfConsistencyLookback — "3yr" | "2yr" | "1yr" | None

    Strategy:
      • One income_stmt + balance_sheet + cashflow call gives 4 years of data.
      • For CAGR metrics (revenue/EPS): endpoints over N years.
      • For AVERAGE metrics (ROE, op margin): arithmetic mean over N years.
        Averages are more appropriate than CAGR for ratios that shouldn't
        compound (you can't earn returns on returns).
      • For FCF: a 3-of-3 positive check + latest > 3-years-ago growth check.
      • Each metric independently degrades 3yr→2yr→none if data is sparse.

    On any failure (no data, exception, malformed DataFrame), leaves info
    untouched and the callers fall back to the existing 1-year fields.
    """
    try:
        stmt = tkr.income_stmt  # pandas DataFrame; columns are years (recent first)
        if stmt is None or stmt.empty:
            return

        # Locate revenue row — yfinance uses "Total Revenue" but be defensive
        rev_row = None
        for label in ("Total Revenue", "TotalRevenue", "Revenue", "Operating Revenue"):
            if label in stmt.index:
                rev_row = stmt.loc[label]
                break

        # Locate earnings row
        eps_row = None
        for label in ("Net Income", "NetIncome",
                      "Net Income Common Stockholders",
                      "Net Income From Continuing Operations"):
            if label in stmt.index:
                eps_row = stmt.loc[label]
                break

        # Locate operating income row (needed for op margin)
        opinc_row = None
        for label in ("Operating Income", "OperatingIncome",
                      "Total Operating Income As Reported"):
            if label in stmt.index:
                opinc_row = stmt.loc[label]
                break

        # Sort columns chronologically (yfinance gives them most-recent-first)
        # so series[0] is the latest year, series[-1] is the oldest.
        def usable_series(row):
            if row is None:
                return None
            values = [float(v) for v in row.values
                      if v is not None and not (isinstance(v, float)
                                                and (v != v or v == float('inf')))]
            return values if len(values) >= 2 else None

        rev = usable_series(rev_row)
        eps = usable_series(eps_row)
        opinc = usable_series(opinc_row)

        # ---- Revenue CAGR ----
        if rev:
            latest, earliest, n_years = None, None, 0
            if len(rev) >= 4 and rev[3] > 0:
                latest, earliest, n_years = rev[0], rev[3], 3
            elif len(rev) >= 3 and rev[2] > 0:
                latest, earliest, n_years = rev[0], rev[2], 2
            elif len(rev) >= 2 and rev[1] > 0:
                latest, earliest, n_years = rev[0], rev[1], 1

            if latest is not None and earliest is not None and earliest > 0:
                cagr = (latest / earliest) ** (1.0 / n_years) - 1.0
                info["revenueCAGR3y"] = cagr
                info["revenueGrowthLookback"] = f"{n_years}yr CAGR"

        # ---- Earnings CAGR (handle negatives carefully) ----
        if eps:
            latest, earliest, n_years = None, None, 0
            if len(eps) >= 4 and eps[0] > 0 and eps[3] > 0:
                latest, earliest, n_years = eps[0], eps[3], 3
            elif len(eps) >= 3 and eps[0] > 0 and eps[2] > 0:
                latest, earliest, n_years = eps[0], eps[2], 2
            elif len(eps) >= 2 and eps[0] > 0 and eps[1] > 0:
                latest, earliest, n_years = eps[0], eps[1], 1

            if latest is not None and earliest is not None and earliest > 0:
                cagr = (latest / earliest) ** (1.0 / n_years) - 1.0
                info["earningsCAGR3y"] = cagr
                info["earningsGrowthLookback"] = f"{n_years}yr CAGR"

        # ---- Operating Margin: 3-year ARITHMETIC AVERAGE ----
        # Op margin = OperatingIncome / Revenue per year, averaged over N years.
        # Why average (not CAGR): margins are ratios, not compounded values —
        # a 15% margin sustained for 3 years averages to 15%, not (15%)^3.
        if opinc and rev:
            n_data = min(len(opinc), len(rev))
            margins = []
            for i in range(min(n_data, 3)):
                if rev[i] > 0:
                    margins.append(opinc[i] / rev[i])
            if len(margins) >= 2:
                info["operatingMarginAvg3y"] = sum(margins) / len(margins)
                info["operatingMarginLookback"] = f"{len(margins)}yr avg"

        # ---- ROE: 3-year AVERAGE using balance sheet equity ----
        # ROE = NetIncome / StockholdersEquity per year, averaged over N years.
        # The 1-year value yfinance provides can be wildly distorted by
        # share buybacks (shrinking equity inflates ROE) or one-time items.
        try:
            bs = tkr.balance_sheet
            if bs is not None and not bs.empty and eps:
                eq_row = None
                for label in ("Stockholders Equity", "StockholdersEquity",
                              "Total Stockholder Equity",
                              "Common Stock Equity"):
                    if label in bs.index:
                        eq_row = bs.loc[label]
                        break
                if eq_row is not None:
                    eq = usable_series(eq_row)
                    if eq:
                        n_data = min(len(eq), len(eps))
                        roes = []
                        for i in range(min(n_data, 3)):
                            if eq[i] > 0:
                                roes.append(eps[i] / eq[i])
                        if len(roes) >= 2:
                            info["roeAvg3y"] = sum(roes) / len(roes)
                            info["roeLookback"] = f"{len(roes)}yr avg"
        except Exception:
            pass  # balance sheet issues fall through to 1yr value

        # ---- FCF consistency: 3-of-3 positive AND growing ----
        # Compounder framework wants reliable cash generation, not one good year.
        # The 1-year YoY check (current_fcf > prior_fcf AND current_fcf > 0)
        # was already done in analyze_position(); here we add a multi-year check.
        try:
            cf = tkr.cashflow
            if cf is not None and not cf.empty:
                fcf_row = None
                if "Free Cash Flow" in cf.index:
                    fcf_row = cf.loc["Free Cash Flow"]
                elif ("Operating Cash Flow" in cf.index
                      and "Capital Expenditure" in cf.index):
                    fcf_row = cf.loc["Operating Cash Flow"] + cf.loc["Capital Expenditure"]
                if fcf_row is not None:
                    fcf_values = usable_series(fcf_row)
                    if fcf_values and len(fcf_values) >= 2:
                        # Use up to 3 most recent years
                        recent = fcf_values[:min(3, len(fcf_values))]
                        positive_years = sum(1 for v in recent if v > 0)
                        # Growing = latest > earliest of the window
                        growing = recent[0] > recent[-1] and recent[0] > 0
                        info["fcfConsistency"] = {
                            "positive_years": positive_years,
                            "total_years": len(recent),
                            "growing": growing,
                        }
                        info["fcfConsistencyLookback"] = f"{len(recent)}yr"
        except Exception:
            pass  # cashflow issues fall through to existing 1yr check

    except Exception as e:
        print(f"[multi-year growth] {info.get('symbol', '?')}: {e}")


def apply_quality_filters(info: dict) -> list[FilterResult]:
    """
    Apply the nine-filter quality compounder framework.

    Note on yfinance units:
      - revenueGrowth / earningsGrowth / operatingMargins / returnOnEquity
        are returned as decimals (0.10 = 10%).
      - debtToEquity is returned as a percentage (100 = 1.0 ratio).
    """
    results: list[FilterResult] = []

    # 1. Revenue growth >= 10%  (prefer 3-year CAGR; fall back to 1-year YoY)
    #    A compounder is defined by sustained growth, not flash-in-the-pan
    #    growth — a 3-year CAGR is much more representative of business
    #    quality than last quarter's YoY comparison. CAGR is computed in
    #    _compute_multi_year_growth() upstream; this filter uses it if
    #    present and falls back to revenueGrowth otherwise.
    rev_cagr = _safe_get(info, "revenueCAGR3y")
    rev_lookback = info.get("revenueGrowthLookback")
    if rev_cagr is None:
        rev_cagr = _safe_get(info, "revenueGrowth")
        rev_lookback = "1yr YoY" if rev_cagr is not None else None
    results.append(FilterResult(
        name="Revenue growth >=10%",
        passed=(rev_cagr is not None and rev_cagr >= 0.10),
        actual=(rev_cagr * 100) if rev_cagr is not None else None,
        threshold=">= 10%",
        note=rev_lookback or "",
    ))

    # 2. EPS growth >= 10%  (prefer 3-year CAGR; fall back to 1-year YoY)
    #    CAGR is skipped automatically when either endpoint has non-positive
    #    earnings (a loss-to-profit transition breaks compound-growth math).
    #    In that case we fall back to the 1-year value, which yfinance
    #    computes from current-vs-prior-year EPS regardless of sign.
    eps_cagr = _safe_get(info, "earningsCAGR3y")
    eps_lookback = info.get("earningsGrowthLookback")
    if eps_cagr is None:
        eps_cagr = _safe_get(info, "earningsGrowth")
        eps_lookback = "1yr YoY" if eps_cagr is not None else None
    results.append(FilterResult(
        name="EPS growth >=10%",
        passed=(eps_cagr is not None and eps_cagr >= 0.10),
        actual=(eps_cagr * 100) if eps_cagr is not None else None,
        threshold=">= 10%",
        note=eps_lookback or "",
    ))

    # 3. P/E < 30 (prefer trailing, fall back to forward)
    pe = _safe_get(info, "trailingPE") or _safe_get(info, "forwardPE")
    results.append(FilterResult(
        name="P/E < 30",
        passed=(pe is not None and pe < 30),
        actual=pe,
        threshold="< 30",
    ))

    # 4. PEG < 2
    peg = _safe_get(info, "trailingPegRatio") or _safe_get(info, "pegRatio")
    results.append(FilterResult(
        name="PEG < 2",
        passed=(peg is not None and 0 < peg < 2),
        actual=peg,
        threshold="< 2",
    ))

    # 5. ROE >= 15%  (prefer 3-year average; fall back to 1-year TTM)
    #    A single-year ROE can be distorted by share buybacks (shrinking
    #    equity denominator inflates ROE) or one-time gains. A 3-year
    #    average is a more reliable signal of sustained return on capital.
    #    Note: this is still a proxy for ROIC since yfinance doesn't expose
    #    ROIC; high-leverage companies like AAPL will still show distorted
    #    values because the equity denominator can be artificially small.
    roe_avg = _safe_get(info, "roeAvg3y")
    roe_lookback = info.get("roeLookback")
    if roe_avg is None:
        roe_avg = _safe_get(info, "returnOnEquity")
        roe_lookback = "1yr TTM" if roe_avg is not None else None
    results.append(FilterResult(
        name="ROE >= 15%",
        passed=(roe_avg is not None and roe_avg >= 0.15),
        actual=(roe_avg * 100) if roe_avg is not None else None,
        threshold=">= 15%",
        note=roe_lookback or "",
    ))

    # 6. Operating margin >= 15%  (prefer 3-year average; fall back to 1-year)
    #    Pricing power is a sustained phenomenon, not a one-year event. A
    #    cyclical with one good margin year shouldn't pass this filter.
    om_avg = _safe_get(info, "operatingMarginAvg3y")
    om_lookback = info.get("operatingMarginLookback")
    if om_avg is None:
        om_avg = _safe_get(info, "operatingMargins")
        om_lookback = "1yr TTM" if om_avg is not None else None
    results.append(FilterResult(
        name="Op margin >= 15%",
        passed=(om_avg is not None and om_avg >= 0.15),
        actual=(om_avg * 100) if om_avg is not None else None,
        threshold=">= 15%",
        note=om_lookback or "",
    ))

    # 7. Debt-to-equity < 1 (yfinance returns this *100; 100 = 1.0)
    de_raw = _safe_get(info, "debtToEquity")
    de_ratio = (de_raw / 100) if de_raw is not None else None
    results.append(FilterResult(
        name="Debt/Equity < 1",
        passed=(de_ratio is not None and de_ratio < 1),
        actual=de_ratio,
        threshold="< 1",
    ))

    # 8. Free cash flow positive AND growing
    #    Prefer the multi-year consistency check from fcfConsistency dict:
    #    requires FCF positive in ALL years of the lookback window (typically
    #    3yr) AND latest > earliest of the window. Falls back to the older
    #    1-year YoY check (_fcfGrowing) when multi-year data isn't available.
    fcf = _safe_get(info, "freeCashflow")
    fcf_consistency = info.get("fcfConsistency")
    fcf_lookback = info.get("fcfConsistencyLookback")
    if fcf_consistency is not None:
        # Multi-year check: all positive + growing across the window
        positive_years = fcf_consistency.get("positive_years", 0)
        total_years = fcf_consistency.get("total_years", 0)
        growing = fcf_consistency.get("growing", False)
        all_positive = positive_years == total_years and total_years >= 2
        fcf_pass = all_positive and growing
        if all_positive and growing:
            note_suffix = f" ({positive_years}/{total_years} yrs +, growing)"
        elif all_positive:
            note_suffix = f" ({positive_years}/{total_years} yrs +, declining)"
        elif positive_years > 0:
            note_suffix = f" ({positive_years}/{total_years} yrs +)"
        else:
            note_suffix = " (no positive years)"
    else:
        # Fallback: 1-year YoY (the old behavior)
        fcf_growing = info.get("_fcfGrowing")
        fcf_pass = (
            fcf is not None and fcf > 0
            and fcf_growing is not False
        )
        if fcf_growing is True:
            note_suffix = " (1yr: growing)"
        elif fcf_growing is False:
            note_suffix = " (1yr: shrinking)"
        else:
            note_suffix = ""
    results.append(FilterResult(
        name="FCF positive & growing",
        passed=fcf_pass,
        actual=(fcf / 1e9) if fcf is not None else None,
        threshold="> 0, all yrs",
        note=("$B" + note_suffix) if fcf is not None else "",
    ))

    # 9. Quick ratio > 1.0
    qr = _safe_get(info, "quickRatio")
    results.append(FilterResult(
        name="Quick ratio > 1.0",
        passed=(qr is not None and qr > 1.0),
        actual=qr,
        threshold="> 1.0",
    ))

    return results


# ============================================================
# Verdict logic
# ============================================================

@dataclass
class Verdict:
    label: str       # SELL, TRIM, HOLD, ADD, BUY MORE
    color: str       # CSS color for HTML
    reason: str
    score: Optional[float] = None    # 0-100 numerical verdict score (v2 only)


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_composite_score(pa, info: dict) -> None:
    """
    Populate pa.score_{quality,growth,value,analyst} and pa.composite_score
    using the same weights as the screener: 35/25/20/20.

    Each sub-score is 0-100. Missing data sub-scores are dropped from the
    weighted average rather than treated as 0 — fairer to limited-coverage
    tickers (ADRs etc.).
    """
    # Growth uses the same CAGR-preferred values as the quality filters.
    # _compute_multi_year_growth() upstream populates revenueCAGR3y/earningsCAGR3y
    # when annual statements are available; we fall back to 1-year YoY otherwise.
    rev_g = _safe_get(info, "revenueCAGR3y") or _safe_get(info, "revenueGrowth")
    eps_g = _safe_get(info, "earningsCAGR3y") or _safe_get(info, "earningsGrowth")
    pe = _safe_get(info, "trailingPE") or _safe_get(info, "forwardPE")
    peg = _safe_get(info, "trailingPegRatio") or _safe_get(info, "pegRatio")
    # Quality sub-score prefers multi-year averages for ROE and op margin
    # (same logic as the quality filters — sustained quality matters more
    # than a single-year snapshot).
    roe = _safe_get(info, "roeAvg3y") or _safe_get(info, "returnOnEquity")
    om = _safe_get(info, "operatingMarginAvg3y") or _safe_get(info, "operatingMargins")
    de_raw = _safe_get(info, "debtToEquity")
    de = de_raw / 100 if de_raw is not None else None
    quick = _safe_get(info, "quickRatio")
    fcf = _safe_get(info, "freeCashflow")
    fcf_growing = info.get("_fcfGrowing")

    # Quality (35%): ROE, op margin, D/E, quick, FCF positive & growing
    q_components = []
    if roe is not None:
        q_components.append(_clip01(roe / 0.30))      # 30% ROE -> 100
    if om is not None:
        q_components.append(_clip01(om / 0.30))       # 30% OM -> 100
    if de is not None:
        q_components.append(_clip01(1 - de / 1.5))    # D/E 0 -> 100, 1.5 -> 0
    if quick is not None:
        q_components.append(_clip01((quick - 0.5) / 1.5))  # 0.5 -> 0, 2.0 -> 100
    if fcf is not None:
        fcf_score = 0.5 if fcf > 0 else 0.0
        if fcf_growing is True:
            fcf_score = 1.0
        elif fcf_growing is False:
            fcf_score = 0.2
        q_components.append(fcf_score)
    if q_components:
        pa.score_quality = round(sum(q_components) / len(q_components) * 100, 1)

    # Growth (25%): revenue and earnings YoY
    g_components = []
    if rev_g is not None:
        g_components.append(_clip01(rev_g / 0.30))   # 30% growth -> 100
    if eps_g is not None:
        g_components.append(_clip01(eps_g / 0.30))
    if g_components:
        pa.score_growth = round(sum(g_components) / len(g_components) * 100, 1)

    # Value (20%): P/E, PEG, upside-to-target
    v_components = []
    if pe is not None and pe > 0:
        # P/E 10 -> 100, P/E 30 -> 0, scaled
        v_components.append(_clip01((30 - pe) / 20))
    if peg is not None and peg > 0:
        v_components.append(_clip01((2 - peg) / 2))
    if pa.upside_pct is not None:
        # 0% upside -> 50, +30% -> 100, -20% -> 0
        v_components.append(_clip01((pa.upside_pct + 20) / 50))
    if v_components:
        pa.score_value = round(sum(v_components) / len(v_components) * 100, 1)

    # Analyst (20%): rec_avg (lower = better) + number of analysts (more = more conviction)
    if pa.rating_breakdown and pa.rating_breakdown.get("total"):
        rec_avg = pa.rating_breakdown.get("rec_avg")
        if rec_avg is not None:
            # rec_avg 1 -> 100, 5 -> 0
            rec_score = _clip01((5 - rec_avg) / 4)
            # Confidence factor: 5+ analysts ~ full weight
            n = pa.rating_breakdown["total"]
            conf = _clip01(n / 10)
            # Blend: 80% rec-quality, 20% conviction
            pa.score_analyst = round(
                (rec_score * 0.8 + conf * 0.2) * 100, 1
            )

    # Insider (15%): buys/sells over last ~90 days
    # score_insider is populated externally by analyze_position before we get
    # here — we just read it. Missing data drops the weight as usual.
    # (no-op block to keep all 5 sub-scores explicit)

    # Composite (weighted; weights re-normalized over what's available).
    # Insider activity gets meaningful weight because it's high-conviction
    # information — but not dominant.
    weights = {
        "score_quality": 0.30, "score_growth": 0.20,
        "score_value": 0.20, "score_analyst": 0.15,
        "score_insider": 0.15,
    }
    weighted_sum = 0.0
    weight_total = 0.0
    for attr, w in weights.items():
        val = getattr(pa, attr)
        if val is not None:
            weighted_sum += val * w
            weight_total += w
    if weight_total > 0:
        pa.composite_score = round(weighted_sum / weight_total, 1)


def apply_context_adjustments(pa) -> None:
    """
    Light, transparent verdict adjustment using sector momentum + 52-week range.

    Philosophy: fundamentals lead. This can shift the verdict by AT MOST one
    notch, and always appends the reason. Rules:

      Holdings (sell/hold/add):
        • HOLD + Hot sector + upside >10%        -> ADD   (momentum + room to run)
        • ADD  + Cool sector + upside <20%       -> HOLD  (wait out the sector)
        • Any  + price in top 10% of 52w range   -> append "near 52w high" caution
        • Any  + price in bottom 25% + quality OK -> append "value-entry zone" note

      Watchlist / ad-hoc (buy framing):
        • WATCH/WAIT + Hot sector + upside >10%  -> BUY
        • BUY + Cool sector + upside <15%        -> WATCH

    All notes are appended to verdict.reason so nothing is hidden.
    """
    v = pa.verdict
    if not v:
        return
    sm = pa.sector_momentum or {}
    sector_label = sm.get("label")
    upside = pa.upside_pct
    pos = pa.week52_position
    passed = sum(1 for f in pa.filters if f.passed) if pa.filters else None

    notes = []

    # --- Sector-driven notch shifts ---
    if v.label == "HOLD" and sector_label == "Hot" and upside is not None and upside > 10:
        v.label = "ADD"
        v.color = "#27ae60"
        notes.append(f"upgraded on hot {pa.sector} sector + {upside:.0f}% upside")
    elif v.label == "ADD" and sector_label == "Cool" and (upside is None or upside < 20):
        v.label = "HOLD"
        v.color = "#2c3e50"
        notes.append(f"held back — {pa.sector} sector is cooling")
    elif v.label in ("WATCH", "WAIT") and sector_label == "Hot" \
            and upside is not None and upside > 10:
        v.label = "BUY"
        v.color = "#27ae60"
        notes.append(f"upgraded on hot {pa.sector} sector")
    elif v.label == "BUY" and sector_label == "Cool" and (upside is None or upside < 15):
        v.label = "WATCH"
        v.color = "#f39c12"
        notes.append(f"downgraded — {pa.sector} sector cooling")
    elif sector_label in ("Hot", "Cool"):
        # No flip, but surface the sector context
        notes.append(f"{pa.sector} sector {sector_label.lower()}")

    # --- 52-week range context (notes only, no flips) ---
    if pos is not None:
        if pos >= 90:
            notes.append(f"near 52w high ({pos:.0f}% of range)")
        elif pos <= 25 and (passed is None or passed >= 6):
            notes.append(f"value-entry zone ({pos:.0f}% of range)")

    if notes:
        v.reason = v.reason + " · " + " · ".join(notes)


def compute_verdict_v2(
    *,
    composite_score: Optional[float],
    filters: Optional[list] = None,
    current_price: Optional[float] = None,
    target_price: Optional[float] = None,
    upside_pct: Optional[float] = None,
    trend: Optional[str] = None,
    pct_above_ma200: Optional[float] = None,
    week52_position: Optional[float] = None,
    sector_label: Optional[str] = None,
    insider_signal: Optional[str] = None,
    position_pct_portfolio: Optional[float] = None,
    is_holding: bool = True,
) -> Verdict:
    """
    Evidence-weighted verdict logic.

    Synthesizes ALL available signals into a single "verdict score" (0-100)
    by starting from the Composite Score and applying small modifiers
    for each of: trend, insider activity, sector momentum, 52-week position,
    quality miss, valuation vs target. Final number maps to a verdict.

    Holdings (is_holding=True) use SELL/TRIM/HOLD/ADD vocabulary with
    "stay-the-course" bias — selling has tax friction so the bar is higher.
    Watchlist items (is_holding=False) use SELL/PASS/WAIT/WATCH/BUY where
    BUY requires a fresh-money commitment.

    Returns a Verdict with `reason` containing a transparent breakdown of
    every contributing factor (+5 for hot sector, -10 for downtrend, etc.).
    Hovering the verdict pill surfaces the full breakdown.
    """
    # ---- Base: Composite Score (0-100) ----
    if composite_score is None:
        # No composite available — fall back to mid-neutral, no confidence.
        base = 50.0
        contributors: list[tuple[str, float]] = [("Composite Score unavailable, neutral baseline", 0)]
    else:
        base = float(composite_score)
        contributors = [(f"Composite Score {base:.0f}", 0)]  # 0 marker, just shows the base

    # Score that we'll modify
    score = base

    # ---- Quality filter check (only meaningful when filters are present) ----
    passed = None
    failed = 0
    if filters:
        passed = sum(1 for f in filters if f.passed)
        failed = len(filters) - passed
        if failed >= 4:
            score -= 15
            contributors.append((f"Fails {failed}/9 quality filters", -15))
        elif failed == 3:
            score -= 8
            contributors.append((f"Fails 3/9 quality filters", -8))
        elif passed == 9:
            score += 5
            contributors.append(("Passes all 9 quality filters", +5))

    # ---- Trend (50d MA / 200d MA alignment) ----
    if trend == "uptrend":
        bonus = 10
        if pct_above_ma200 is not None and pct_above_ma200 >= 25:
            bonus = 12  # particularly strong uptrend
        score += bonus
        contributors.append((f"Uptrend"
                              + (f" (+{pct_above_ma200:.0f}% vs 200d)"
                                 if pct_above_ma200 is not None else ""), +bonus))
    elif trend == "downtrend":
        score -= 10
        contributors.append((f"Downtrend"
                              + (f" ({pct_above_ma200:+.0f}% vs 200d)"
                                 if pct_above_ma200 is not None else ""), -10))
    # sideways: no adjustment

    # ---- Insider activity ----
    if insider_signal == "supports_buy":
        score += 8
        contributors.append(("Insider buying (rare signal)", +8))
    elif insider_signal == "caution":
        score -= 8
        contributors.append(("Insider selling meaningful for size", -8))
    # "no_signal" / unset: no adjustment

    # ---- Sector momentum ----
    if sector_label == "Hot":
        score += 5
        contributors.append(("Hot sector momentum", +5))
    elif sector_label == "Cool":
        score -= 4
        contributors.append(("Cool sector momentum", -4))

    # ---- 52-week position ----
    if week52_position is not None:
        if week52_position <= 20:
            # Deep value zone — but only credit if quality is decent
            if passed is None or passed >= 6:
                score += 5
                contributors.append((f"Near 52w low ({week52_position:.0f}%) "
                                     "with intact fundamentals", +5))
            else:
                # Low + low quality = falling knife
                score -= 5
                contributors.append((f"Near 52w low ({week52_position:.0f}%) "
                                     "but quality is weak", -5))
        elif week52_position >= 92:
            score -= 4
            contributors.append((f"Near 52w high ({week52_position:.0f}%)", -4))

    # ---- Valuation vs analyst target ----
    if upside_pct is not None:
        if upside_pct >= 20:
            score += 6
            contributors.append((f"Strong upside to target ({upside_pct:+.0f}%)", +6))
        elif upside_pct >= 10:
            score += 3
            contributors.append((f"Moderate upside to target ({upside_pct:+.0f}%)", +3))
        elif upside_pct <= -15:
            # Price is well above target. Trend already factored in separately —
            # so this is mostly about valuation.
            if trend == "uptrend":
                # Mild penalty — analysts may simply be lagging
                score -= 4
                contributors.append((f"Price {abs(upside_pct):.0f}% above target "
                                     "(analysts may be lagging)", -4))
            else:
                score -= 10
                contributors.append((f"Price {abs(upside_pct):.0f}% above target", -10))
        elif upside_pct < 0:
            score -= 3
            contributors.append((f"Slightly above target ({upside_pct:+.0f}%)", -3))

    # ---- Position-size awareness (holdings only) ----
    # Concentration matters: even a great stock shouldn't be a "buy MORE" candidate
    # if it's already a huge slice of the portfolio. This penalty discourages
    # adding to over-concentrated positions and reflects real portfolio-risk
    # thinking (single-name risk, sector overlap, sequence-of-returns sensitivity).
    # Magnitudes are deliberately moderate — they nudge ADD→HOLD but don't push
    # a quality stock to SELL.
    position_size_flag = None    # used below for hard ADD-ceiling override
    if is_holding and position_pct_portfolio is not None:
        if position_pct_portfolio >= 25:
            score -= 8
            contributors.append(
                (f"Already very overweight ({position_pct_portfolio:.0f}% of portfolio)", -8)
            )
            position_size_flag = "very_overweight"
        elif position_pct_portfolio >= 15:
            score -= 4
            contributors.append(
                (f"Already overweight ({position_pct_portfolio:.0f}% of portfolio)", -4)
            )
            position_size_flag = "overweight"
        elif position_pct_portfolio >= 10:
            score -= 2
            contributors.append(
                (f"Sizeable position ({position_pct_portfolio:.0f}% of portfolio)", -2)
            )
            # No flag — 10-15% doesn't trigger the ADD ceiling, just a small nudge

    # ---- Clamp to 0-100 ----
    score = max(0.0, min(100.0, score))

    # ---- Map to verdict label ----
    if is_holding:
        # Holdings: stay-the-course bias. HOLD covers a wide middle band.
        if score >= 78:
            label, color = "ADD", "#27ae60"        # strong conviction add
        elif score >= 60:
            label, color = "HOLD", "#2c3e50"       # stay the course
        elif score >= 42:
            label, color = "HOLD", "#7f8c8d"       # weak HOLD (muted gray)
        elif score >= 28:
            label, color = "TRIM", "#e67e22"
        else:
            label, color = "SELL", "#c0392b"
    else:
        # Watchlist: requires fresh-money conviction for BUY.
        if score >= 75:
            label, color = "BUY", "#27ae60"
        elif score >= 60:
            label, color = "WATCH", "#2980b9"      # interesting, not yet
        elif score >= 42:
            label, color = "WAIT", "#7f8c8d"       # neutral
        else:
            label, color = "PASS", "#c0392b"

    # ---- Hard ADD-ceiling for overweight positions ----
    # Even with the position-size penalty applied, a very-strong-fundamentals
    # stock could still cross the ADD threshold. For holdings that are already
    # 15%+ of the portfolio, that's the wrong recommendation regardless of
    # how good the stock looks — the action is "rebalance," not "buy more."
    # Downgrade ADD to HOLD in those cases, with the position size as the reason.
    if label == "ADD" and position_size_flag in ("overweight", "very_overweight"):
        label, color = "HOLD", "#2c3e50"
        contributors.append(
            (f"ADD overridden: position already "
             f"{'very ' if position_size_flag == 'very_overweight' else ''}"
             f"overweight — rebalance, don't add", 0)
        )

    # ---- Reason: short headline + transparent breakdown ----
    # Sort contributors by absolute impact (biggest first), drop the 0-base entry
    contributors_with_impact = [c for c in contributors if c[1] != 0]
    contributors_with_impact.sort(key=lambda c: abs(c[1]), reverse=True)
    headline = _verdict_headline(label, score, contributors_with_impact)

    # The breakdown shows each factor with its delta. Format: "+5 Hot sector"
    breakdown_lines = []
    if contributors and contributors[0][1] == 0:
        breakdown_lines.append(contributors[0][0])  # base score line
    for desc, delta in contributors_with_impact:
        sign = "+" if delta > 0 else ""
        breakdown_lines.append(f"{sign}{delta:.0f} · {desc}")
    breakdown_lines.append(f"= verdict score {score:.0f}")
    reason = headline + " | " + " | ".join(breakdown_lines)

    return Verdict(label=label, color=color, reason=reason, score=round(score, 1))


def _verdict_headline(label: str, score: float,
                      sorted_contributors: list) -> str:
    """Generate a short headline based on the top positive/negative factors."""
    positives = [c for c in sorted_contributors if c[1] > 0][:2]
    negatives = [c for c in sorted_contributors if c[1] < 0][:2]
    if label == "ADD" or label == "BUY":
        if positives:
            return f"{', '.join(p[0] for p in positives)}"
        return "Strong overall signal"
    if label == "SELL" or label == "PASS":
        if negatives:
            return f"Weak: {', '.join(n[0] for n in negatives)}"
        return "Weak overall signal"
    if label == "TRIM":
        if negatives:
            return f"Trim candidate: {negatives[0][0]}"
        return "Trim candidate"
    if label == "WATCH":
        if positives:
            return f"Watch: {positives[0][0]}"
        return "On watch"
    if label == "WAIT":
        return "Wait for better setup"
    # HOLD
    if positives and negatives:
        return f"Hold: {positives[0][0]}, but {negatives[0][0]}"
    if positives:
        return f"Hold: {positives[0][0]}"
    return "Hold (no strong signal)"


def compute_compounder_verdict(
    filters: list[FilterResult],
    current_price: Optional[float],
    target_price: Optional[float],
    trend: Optional[str] = None,
    pct_above_ma200: Optional[float] = None,
) -> Verdict:
    """
    Trend-aware logic:
      - SELL: fails 3+ filters AND price > target (regardless of trend)
      - TRIM: fails 3+ filters OR (price > target by >15% AND NOT in uptrend)
      - HOLD (instead of TRIM): price > target but trend is uptrend
              — analysts haven't caught up; don't fight the tape
      - ADD:  passes 7+ filters AND upside to target > 15%
      - HOLD: everything else
    """
    failed = sum(1 for f in filters if not f.passed)
    passed = sum(1 for f in filters if f.passed)

    upside = None
    above_target = False
    if current_price and target_price and target_price > 0:
        upside = (target_price - current_price) / current_price * 100
        above_target = current_price > target_price

    if failed >= 3 and above_target:
        return Verdict(
            label="SELL",
            color="#c0392b",
            reason=f"Fails {failed}/9 quality filters and trades above analyst target",
        )

    # Trend-aware TRIM: only trim on "above target" if the stock isn't ALSO
    # in a clean uptrend. If price is above target but trend is up, the
    # analysts are simply lagging — don't fight the tape.
    if failed >= 3:
        return Verdict(
            label="TRIM",
            color="#e67e22",
            reason=f"Fails {failed}/9 filters",
        )

    if upside is not None and upside < -15:
        if trend == "uptrend":
            # Override: don't trim a stock that's working
            ma_note = (f" (price {pct_above_ma200:+.0f}% above 200-day MA)"
                       if pct_above_ma200 is not None else "")
            return Verdict(
                label="HOLD",
                color="#2c3e50",
                reason=(
                    f"Price {abs(upside):.1f}% above target but trend is "
                    f"strong{ma_note} — analysts catching up"
                ),
            )
        return Verdict(
            label="TRIM",
            color="#e67e22",
            reason=f"Price {abs(upside):.1f}% above analyst target"
                   + (f" and trend is {trend}" if trend in ("downtrend", "sideways") else ""),
        )

    if passed >= 7 and upside is not None and upside > 15:
        return Verdict(
            label="ADD",
            color="#27ae60",
            reason=f"Passes {passed}/9 filters with {upside:.1f}% upside to target",
        )

    return Verdict(
        label="HOLD",
        color="#2c3e50",
        reason=f"Passes {passed}/9 filters" + (
            f", {upside:+.1f}% to target" if upside is not None else ""
        ),
    )


def compute_thematic_verdict(
    current_price: Optional[float],
    target_price: Optional[float],
    ma_50: Optional[float],
    ma_200: Optional[float],
) -> Verdict:
    """Simpler logic for ETFs / thematic plays."""
    upside = None
    if current_price and target_price and target_price > 0:
        upside = (target_price - current_price) / current_price * 100

    trend = "N/A"
    if ma_50 and ma_200:
        if ma_50 > ma_200 * 1.02:
            trend = "uptrend"
        elif ma_50 < ma_200 * 0.98:
            trend = "downtrend"
        else:
            trend = "sideways"

    if upside is not None and upside < -15:
        return Verdict(
            label="TRIM",
            color="#e67e22",
            reason=f"Price {abs(upside):.1f}% above target ({trend})",
        )
    if upside is not None and upside > 15 and trend != "downtrend":
        return Verdict(
            label="ADD",
            color="#27ae60",
            reason=f"{upside:.1f}% upside to target ({trend})",
        )
    if trend == "downtrend" and (upside is None or upside < 5):
        return Verdict(
            label="WATCH",
            color="#e67e22",
            reason="Downtrend with limited upside",
        )
    if upside is not None:
        return Verdict(
            label="HOLD",
            color="#2c3e50",
            reason=f"{upside:+.1f}% to target, {trend}",
        )
    return Verdict(
        label="HOLD",
        color="#2c3e50",
        reason=f"Trend: {trend}",
    )


# ------- Watchlist verdicts (different framing — "should I buy?") -------

def compute_watchlist_compounder_verdict(
    filters: list[FilterResult],
    current_price: Optional[float],
    target_price: Optional[float],
) -> Verdict:
    """
    Watchlist logic for compounder candidates:
      - BUY:   passes 7+ filters AND upside > 15%
      - WAIT:  passes 7+ filters but limited/no upside (good company, wait for price)
      - WATCH: passes 5-6 filters (borderline quality)
      - PASS:  fails 4+ filters (doesn't fit framework)
    """
    failed = sum(1 for f in filters if not f.passed)
    passed = sum(1 for f in filters if f.passed)

    upside = None
    if current_price and target_price and target_price > 0:
        upside = (target_price - current_price) / current_price * 100

    if passed >= 7 and upside is not None and upside > 15:
        return Verdict(
            label="BUY", color="#27ae60",
            reason=f"Passes {passed}/9 filters with {upside:.1f}% upside",
        )
    if passed >= 7:
        return Verdict(
            label="WAIT", color="#3498db",
            reason=(
                f"Quality is there ({passed}/9), but valuation isn't"
                + (f" ({upside:+.1f}% to target)" if upside is not None else "")
            ),
        )
    if passed >= 5:
        return Verdict(
            label="WATCH", color="#f39c12",
            reason=f"Borderline quality ({passed}/9 filters pass)",
        )
    return Verdict(
        label="PASS", color="#7f8c8d",
        reason=f"Fails {failed}/9 filters — doesn't fit framework",
    )


def compute_watchlist_thematic_verdict(
    current_price: Optional[float],
    target_price: Optional[float],
    ma_50: Optional[float],
    ma_200: Optional[float],
) -> Verdict:
    """Watchlist logic for ETFs / thematic candidates: trend + upside."""
    upside = None
    if current_price and target_price and target_price > 0:
        upside = (target_price - current_price) / current_price * 100

    trend = "N/A"
    if ma_50 and ma_200:
        if ma_50 > ma_200 * 1.02:
            trend = "uptrend"
        elif ma_50 < ma_200 * 0.98:
            trend = "downtrend"
        else:
            trend = "sideways"

    if upside is not None and upside > 15 and trend != "downtrend":
        return Verdict(
            label="BUY", color="#27ae60",
            reason=f"{upside:.1f}% upside, {trend}",
        )
    if trend == "uptrend":
        up_str = f" ({upside:+.1f}% to target)" if upside is not None else ""
        return Verdict(
            label="WATCH", color="#f39c12",
            reason=f"Uptrend{up_str}",
        )
    if trend == "downtrend":
        up_str = f", {upside:+.1f}% to target" if upside is not None else ""
        return Verdict(
            label="PASS", color="#7f8c8d",
            reason=f"Downtrend{up_str}",
        )
    return Verdict(
        label="WATCH", color="#f39c12",
        reason=trend + (f", {upside:+.1f}% to target" if upside is not None else ""),
    )


# ============================================================
# Per-position analysis
# ============================================================

@dataclass
class PositionAnalysis:
    ticker: str
    name: str
    shares: float
    # Statement-time / source-time values
    statement_market_value: float
    statement_pct_portfolio: float
    bucket: str = "compounder"
    # Cost basis (from Robinhood; None when CSV source)
    average_buy_price: Optional[float] = None
    cost_basis_total: Optional[float] = None
    unrealized_gain: Optional[float] = None
    unrealized_gain_pct: Optional[float] = None
    # Live values
    current_price: Optional[float] = None
    live_market_value: Optional[float] = None
    live_pct_portfolio: Optional[float] = None
    # Analyst data
    target_mean: Optional[float] = None
    target_high: Optional[float] = None
    target_low: Optional[float] = None
    num_analysts: Optional[int] = None
    recommendation: Optional[str] = None
    upside_pct: Optional[float] = None
    # Ratings breakdown - normalized format: {buy, hold, sell, total, source}
    rating_breakdown: Optional[dict] = None
    # Quality framework
    filters: list[FilterResult] = field(default_factory=list)
    # Trend
    ma_50: Optional[float] = None
    ma_200: Optional[float] = None
    # Sector
    sector: Optional[str] = None
    sector_momentum: Optional[dict] = None    # from get_sector_momentum()
    business_summary: Optional[str] = None    # one-paragraph description for hover
    # 52-week range
    week52_high: Optional[float] = None
    week52_low: Optional[float] = None
    week52_position: Optional[float] = None    # 0-100% of the way up the range
    # Trend / moving averages (used for verdict context)
    ma_50: Optional[float] = None              # 50-day moving average
    ma_200: Optional[float] = None             # 200-day moving average
    pct_above_ma200: Optional[float] = None    # (price - ma200) / ma200 * 100
    trend: Optional[str] = None                # "uptrend" | "sideways" | "downtrend"
    # Composite scoring (0-100, sub-scores + final blend)
    score_quality: Optional[float] = None
    score_growth: Optional[float] = None
    score_value: Optional[float] = None
    score_analyst: Optional[float] = None
    score_insider: Optional[float] = None
    composite_score: Optional[float] = None
    # Insider activity (raw data for display)
    insider_activity: Optional[dict] = None
    # Output
    verdict: Optional[Verdict] = None
    error: Optional[str] = None
    # Holding period / tax
    position_opened: Optional[str] = None     # ISO date string or None
    tax: Optional[object] = None              # TaxAnalysis (set post-hoc)


def analyze_position(
    row: dict,
    use_robinhood_ratings: bool = False,
    is_watchlist: bool = False,
) -> PositionAnalysis:
    ticker = row["ticker"]
    name = row["name"]
    shares = float(row.get("shares", 0) or 0)
    statement_mv = float(row.get("market_value", 0) or 0)
    statement_pct = float(row.get("pct_portfolio", 0) or 0)

    pa = PositionAnalysis(
        ticker=ticker, name=name, shares=shares,
        statement_market_value=statement_mv,
        statement_pct_portfolio=statement_pct,
    )

    # Capture cost basis if present (Robinhood source provides it; CSV does not)
    avg = row.get("average_buy_price")
    if avg is not None:
        try:
            avg_f = float(avg)
            if avg_f > 0:
                pa.average_buy_price = avg_f
                pa.cost_basis_total = avg_f * shares
        except (TypeError, ValueError):
            pass

    # Capture position open date for holding-period / tax analysis
    pa.position_opened = row.get("position_opened") or None

    try:
        tkr = yf.Ticker(ticker)
        info = tkr.info or {}
        if not info or info.get("regularMarketPrice") is None:
            try:
                fi = tkr.fast_info
                info["regularMarketPrice"] = getattr(fi, "last_price", None)
            except Exception:
                pass

        # Augment `info` with 3-year revenue/earnings CAGR computed from the
        # annual income statement. The quality filters and composite scoring
        # prefer this over the 1-year YoY values yfinance provides directly,
        # because compounders are defined by sustained growth, not last-year
        # snapshots. Silently no-ops for tickers without annual data.
        _compute_multi_year_growth(tkr, info)

        pa.bucket = classify_position(ticker, info)
        _regular = _safe_get(info, "regularMarketPrice") or _safe_get(info, "currentPrice")
        _post = _safe_get(info, "postMarketPrice")
        _pre = _safe_get(info, "preMarketPrice")

        def _sane_extended(ext, reg):
            return ext and reg and reg > 0 and abs(ext / reg - 1) < 0.30

        if _sane_extended(_post, _regular):
            pa.current_price = _post
        elif _sane_extended(_pre, _regular):
            pa.current_price = _pre
        else:
            pa.current_price = _regular

        # Compute LIVE market value from live price × shares
        if pa.current_price is not None:
            pa.live_market_value = pa.current_price * pa.shares

        # Determine FCF YoY growth from historical cashflow statements.
        info["_fcfGrowing"] = None  # default: unknown
        try:
            cf = tkr.cashflow
            if cf is not None and not cf.empty:
                fcf_series = None
                if "Free Cash Flow" in cf.index:
                    fcf_series = cf.loc["Free Cash Flow"].dropna()
                elif (("Operating Cash Flow" in cf.index)
                      and ("Capital Expenditure" in cf.index)):
                    op = cf.loc["Operating Cash Flow"]
                    capex = cf.loc["Capital Expenditure"]
                    fcf_series = (op + capex).dropna()
                if fcf_series is not None and len(fcf_series) >= 2:
                    current_fcf = float(fcf_series.iloc[0])
                    prior_fcf = float(fcf_series.iloc[1])
                    info["_fcfGrowing"] = bool(
                        current_fcf > prior_fcf and current_fcf > 0
                    )
        except Exception:
            pass

        # Analyst data — yfinance baseline
        pa.target_mean = _safe_get(info, "targetMeanPrice")
        pa.target_high = _safe_get(info, "targetHighPrice")
        pa.target_low = _safe_get(info, "targetLowPrice")
        na = info.get("numberOfAnalystOpinions")
        pa.num_analysts = int(na) if na else None
        pa.recommendation = info.get("recommendationKey")
        pa.ma_50 = _safe_get(info, "fiftyDayAverage")
        pa.ma_200 = _safe_get(info, "twoHundredDayAverage")

        # Sector + hot/cool momentum
        pa.sector = info.get("sector")
        # Truncate the long business summary to a tooltip-friendly length.
        # yfinance returns paragraphs that can run 500+ words; we want the
        # first ~1-2 sentences (~250 chars) for a hover.
        raw_summary = info.get("longBusinessSummary") or ""
        if raw_summary:
            summary = raw_summary.strip()
            if len(summary) > 280:
                # Cut at sentence boundary if possible
                cutoff = summary.rfind(". ", 0, 280)
                if cutoff > 150:
                    summary = summary[:cutoff + 1]
                else:
                    summary = summary[:277].rstrip() + "..."
            pa.business_summary = summary
        pa.sector_momentum = get_sector_momentum(pa.sector)

        # 52-week range position (where current price sits, 0% = low, 100% = high)
        pa.week52_high = _safe_get(info, "fiftyTwoWeekHigh")
        pa.week52_low = _safe_get(info, "fiftyTwoWeekLow")
        if (pa.current_price and pa.week52_high and pa.week52_low
                and pa.week52_high > pa.week52_low):
            pa.week52_position = round(
                (pa.current_price - pa.week52_low)
                / (pa.week52_high - pa.week52_low) * 100, 1
            )

        # Trend / moving averages — used for verdict context so we don't
        # issue tone-deaf TRIM calls on stocks in established uptrends.
        # yfinance exposes these directly via the `info` payload.
        pa.ma_50 = _safe_get(info, "fiftyDayAverage")
        pa.ma_200 = _safe_get(info, "twoHundredDayAverage")
        if pa.current_price and pa.ma_200 and pa.ma_200 > 0:
            pa.pct_above_ma200 = round(
                (pa.current_price - pa.ma_200) / pa.ma_200 * 100, 1
            )
        # Classify trend. Three states:
        #   uptrend  = price > 50d MA AND 50d MA > 200d MA (clean golden-cross alignment)
        #   downtrend= price < 50d MA AND 50d MA < 200d MA (death-cross alignment)
        #   sideways = everything else (mixed signals, no clear direction)
        if pa.current_price and pa.ma_50 and pa.ma_200:
            if pa.current_price > pa.ma_50 and pa.ma_50 > pa.ma_200:
                pa.trend = "uptrend"
            elif pa.current_price < pa.ma_50 and pa.ma_50 < pa.ma_200:
                pa.trend = "downtrend"
            else:
                pa.trend = "sideways"

        # Build aggregated ratings: combine Robinhood + Finnhub + Yahoo
        from analyst_aggregator import normalize_breakdown, aggregate
        rh_norm = fh_norm = yh_norm = None

        # 1. Robinhood
        if use_robinhood_ratings:
            try:
                from robinhood_source import (
                    fetch_robinhood_ratings, fetch_robinhood_price_target,
                )
                rh_rating = fetch_robinhood_ratings(ticker)
                if rh_rating:
                    rh_norm = normalize_breakdown(
                        buy=rh_rating.get("buy", 0),
                        hold=rh_rating.get("hold", 0),
                        sell=rh_rating.get("sell", 0),
                        source="robinhood",
                    )
                rh_target = fetch_robinhood_price_target(ticker)
                if rh_target and rh_target.get("targetMean"):
                    pa.target_mean = rh_target["targetMean"]
                    pa.target_high = rh_target.get("targetHigh") or pa.target_high
                    pa.target_low = rh_target.get("targetLow") or pa.target_low
            except Exception as e:
                print(f"[robinhood-ratings] {ticker}: {e}")

        # 2. Finnhub
        if FINNHUB_API_KEY:
            fh_target = fetch_finnhub_price_target(ticker)
            if fh_target:
                # Only override target if Robinhood didn't provide one
                if not (use_robinhood_ratings and pa.target_mean):
                    if fh_target.get("targetMean"):
                        pa.target_mean = fh_target["targetMean"]
                    if fh_target.get("targetHigh"):
                        pa.target_high = fh_target["targetHigh"]
                    if fh_target.get("targetLow"):
                        pa.target_low = fh_target["targetLow"]
            fh_rec = fetch_finnhub_recommendation(ticker)
            if fh_rec:
                fh_norm = normalize_breakdown(
                    buy=fh_rec.get("strongBuy", 0) + fh_rec.get("buy", 0),
                    hold=fh_rec.get("hold", 0),
                    sell=fh_rec.get("strongSell", 0) + fh_rec.get("sell", 0),
                    source="finnhub",
                )
            time.sleep(0.05)

        # 3. Yahoo Finance (always — comes from info we already have)
        # yfinance .info exposes: numberOfAnalystOpinions, recommendationMean,
        # recommendationKey. For lot-count breakdown we use the latest row of
        # tkr.recommendations if available; else estimate from rec_mean.
        try:
            rec_df = tkr.recommendations
            if rec_df is not None and not rec_df.empty:
                # Most recent row sums per category
                latest = rec_df.iloc[0]
                yb = int(latest.get("strongBuy", 0) or 0) + int(latest.get("buy", 0) or 0)
                yh = int(latest.get("hold", 0) or 0)
                ys = int(latest.get("sell", 0) or 0) + int(latest.get("strongSell", 0) or 0)
                if yb + yh + ys > 0:
                    yh_norm = normalize_breakdown(yb, yh, ys, "yahoo")
        except Exception:
            pass
        # Fallback: use recommendationMean if no breakdown rows
        if yh_norm is None:
            rec_mean = _safe_get(info, "recommendationMean")
            n_an = info.get("numberOfAnalystOpinions") or 0
            if rec_mean and n_an:
                # Reverse-engineer a buy/hold/sell split from rec_mean & count.
                # rec_mean ~1.5 = mostly buys, ~3 = mostly holds, ~4.5 = mostly sells.
                # Simple heuristic split (good enough for aggregation weighting).
                if rec_mean < 2.0:
                    yb, yh, ys = int(n_an * 0.85), int(n_an * 0.15), 0
                elif rec_mean < 2.5:
                    yb, yh, ys = int(n_an * 0.65), int(n_an * 0.30), int(n_an * 0.05)
                elif rec_mean < 3.0:
                    yb, yh, ys = int(n_an * 0.40), int(n_an * 0.50), int(n_an * 0.10)
                elif rec_mean < 3.5:
                    yb, yh, ys = int(n_an * 0.20), int(n_an * 0.60), int(n_an * 0.20)
                else:
                    yb, yh, ys = int(n_an * 0.10), int(n_an * 0.40), int(n_an * 0.50)
                if yb + yh + ys > 0:
                    yh_norm = normalize_breakdown(yb, yh, ys, "yahoo")

        # Aggregate all three (drops Nones internally)
        agg = aggregate(rh_norm, fh_norm, yh_norm)
        if agg:
            pa.rating_breakdown = agg
            pa.num_analysts = agg["total"]

        if pa.current_price and pa.target_mean and pa.target_mean > 0:
            pa.upside_pct = (pa.target_mean - pa.current_price) / pa.current_price * 100

        # Unrealized gain (only if cost basis present)
        if pa.cost_basis_total is not None and pa.live_market_value is not None:
            pa.unrealized_gain = pa.live_market_value - pa.cost_basis_total
            if pa.cost_basis_total > 0:
                pa.unrealized_gain_pct = (
                    pa.unrealized_gain / pa.cost_basis_total * 100
                )

        if pa.bucket == "compounder":
            pa.filters = apply_quality_filters(info)
            # Compute a preliminary verdict using the older logic. This is
            # used as a fallback if the v2 evidence-weighted logic can't run
            # (e.g. composite score unavailable for some reason).
            if is_watchlist:
                pa.verdict = compute_watchlist_compounder_verdict(
                    pa.filters, pa.current_price, pa.target_mean,
                )
            else:
                pa.verdict = compute_compounder_verdict(
                    pa.filters, pa.current_price, pa.target_mean,
                    trend=pa.trend, pct_above_ma200=pa.pct_above_ma200,
                )
        else:
            if is_watchlist:
                pa.verdict = compute_watchlist_thematic_verdict(
                    pa.current_price, pa.target_mean, pa.ma_50, pa.ma_200,
                )
            else:
                pa.verdict = compute_thematic_verdict(
                    pa.current_price, pa.target_mean, pa.ma_50, pa.ma_200,
                )

        # Layer in sector + 52-week context (light, transparent adjustment)
        apply_context_adjustments(pa)

        # Insider activity (free for the first call per ticker; cached after)
        try:
            from insider_trading import get_insider_activity, insider_score
            pa.insider_activity = get_insider_activity(ticker, lookback_days=90)
            # Pass market cap so the score scales sells by company size —
            # $163M selling at $4T NVDA is very different from $163M at $5B
            market_cap = info.get("marketCap")
            pa.score_insider = insider_score(pa.insider_activity, market_cap=market_cap)
            # Stash the score on the activity dict so the renderer can use it
            # to decide between "Caution" and "No signal" for selling cases.
            if pa.insider_activity is not None:
                pa.insider_activity["_score"] = pa.score_insider
        except Exception as e:
            print(f"[insider] {ticker}: {e}")

        # Composite scoring (now includes insider as 5th sub-score)
        compute_composite_score(pa, info)

        # ---- Evidence-weighted v2 verdict (replaces the preliminary one above) ----
        # Run for compounders (both held and watchlist). The v2 logic uses every
        # available signal — composite score, trend, insider, sector, valuation,
        # 52-week position, quality — to produce a single weighted verdict with
        # full transparency in the reason text.
        if pa.bucket == "compounder" and pa.composite_score is not None:
            insider_signal = None
            if pa.insider_activity:
                sig = pa.insider_activity.get("net_signal", "")
                ins_score = pa.score_insider
                if sig == "Buying":
                    insider_signal = "supports_buy"
                elif sig == "Selling" and ins_score is not None and ins_score <= 35:
                    insider_signal = "caution"
                else:
                    insider_signal = "no_signal"
            sector_label = (pa.sector_momentum or {}).get("label")
            pa.verdict = compute_verdict_v2(
                composite_score=pa.composite_score,
                filters=pa.filters,
                current_price=pa.current_price,
                target_price=pa.target_mean,
                upside_pct=pa.upside_pct,
                trend=pa.trend,
                pct_above_ma200=pa.pct_above_ma200,
                week52_position=pa.week52_position,
                sector_label=sector_label,
                insider_signal=insider_signal,
                is_holding=not is_watchlist,
            )

    except Exception as e:
        pa.error = f"{type(e).__name__}: {e}"
        pa.verdict = Verdict(label="ERROR", color="#7f8c8d", reason=pa.error)

    return pa


# ============================================================
# HTML report
# ============================================================

def _fmt_money(x: Optional[float], decimals: int = 2) -> str:
    if x is None:
        return "—"
    return f"${x:,.{decimals}f}"


def _fmt_pct(x: Optional[float], decimals: int = 1, signed: bool = False) -> str:
    if x is None:
        return "—"
    fmt = f"{{:{'+' if signed else ''}.{decimals}f}}%"
    return fmt.format(x)


# For sortable verdict column: most urgent action first.
# Holdings: SELL → TRIM → HOLD → ADD
# Watchlist: BUY → WATCH → WAIT → PASS
_VERDICT_ORDER = {
    "SELL": 0, "TRIM": 1, "BUY": 2, "WATCH": 3, "WAIT": 4,
    "HOLD": 5, "ADD": 6, "PASS": 7, "ERROR": 8,
}


def _verdict_cell(verdict) -> str:
    """Render the verdict pill + score; full reason shows on hover.

    Layout: colored verdict pill (label) and the numeric score side-by-side.
    Score is also stored in the parent <td> data-sort so the column sorts
    by actual conviction strength rather than label alphabetically.

    v2 verdict reasons use ' | ' to separate the headline from the
    line-by-line factor breakdown (+5 hot sector, -10 downtrend, etc.).
    We replace these with newlines for a readable multi-line tooltip.
    """
    if not verdict:
        return "<span style='color:var(--fg-faint);'>—</span>"
    label = verdict.label or "—"
    color = verdict.color or "#7f8c8d"
    reason = verdict.reason or ""
    # v2 reasons use ' | ' as line separator. Convert to actual newlines so
    # the native browser tooltip shows them on separate lines.
    reason_for_title = reason.replace(" | ", "\n")
    reason_attr = (reason_for_title
                   .replace("&", "&amp;")
                   .replace("'", "&#39;")
                   .replace('"', "&quot;"))
    # Score: shown next to the pill, color-coded by strength
    score = getattr(verdict, "score", None)
    score_html = ""
    if score is not None:
        if score >= 70:
            score_color = "var(--pos-up)"
        elif score >= 50:
            score_color = "var(--fg-strong)"
        elif score >= 35:
            score_color = "#e67e22"
        else:
            score_color = "var(--pos-down)"
        score_html = (
            f"<span style='margin-left:6px;font-weight:600;font-size:13px;"
            f"color:{score_color};font-variant-numeric:tabular-nums;'>"
            f"{score:.0f}</span>"
        )
    return (
        f"<span class='verdict' style='background:{color};cursor:help;' "
        f"title='{reason_attr}'>{label}</span>{score_html}"
    )


def _td(value: str, sort_value, css_class: str = "") -> str:
    """Render a <td>. sort_value goes in data-sort for client-side sorting.

    Use float("-inf") or empty string for missing values so they sort to bottom.
    """
    cls = f" class='{css_class}'" if css_class else ""
    sv = "" if sort_value is None else sort_value
    return f"<td{cls} data-sort='{sv}'>{value}</td>"


def _tr_open(r) -> str:
    """Open a <tr> with data attributes used by the filter bar."""
    verdict = (r.verdict.label if r.verdict else "") or ""
    # Verdict numeric score (0-100), separate from the label
    verdict_score = ("" if (not r.verdict or r.verdict.score is None)
                     else r.verdict.score)
    quality = sum(1 for f in r.filters if f.passed) if r.filters else ""
    gain = "" if r.unrealized_gain is None else r.unrealized_gain
    gain_pct = "" if r.unrealized_gain_pct is None else r.unrealized_gain_pct
    upside = "" if r.upside_pct is None else r.upside_pct
    bucket = r.bucket or ""
    # NOTE: data-sector-mom is the Hot/Cool/Neutral *label*; data-sector is
    # the raw GICS sector name (Technology, Healthcare, etc.). These are two
    # different concepts that the filter bar treats independently.
    sector_mom_label = (r.sector_momentum or {}).get("label", "") or ""
    sector_raw = (r.sector or "").replace("'", "")
    # 52-week position (0-100), Composite Score (0-100), insider verdict
    pos52 = "" if r.week52_position is None else r.week52_position
    score = "" if r.composite_score is None else r.composite_score
    # Trend & position size (used by new filter pills)
    trend = r.trend or ""
    ma_pct = "" if r.pct_above_ma200 is None else r.pct_above_ma200
    port_pct = "" if r.live_pct_portfolio is None else r.live_pct_portfolio
    # Days held — used for LT/ST/days-to-LT filters
    days_held = ""
    if r.position_opened:
        try:
            from datetime import datetime
            d = datetime.strptime(r.position_opened[:10], "%Y-%m-%d")
            days_held = (datetime.now() - d).days
        except Exception:
            pass
    # Analyst recommendation (e.g., "strong_buy", "buy", "hold")
    recommendation = (r.recommendation or "").lower()
    # Insider verdict: derive from net_signal so the filter aligns with the chip
    insider = ""
    has_insider_data = ""
    if r.insider_activity:
        has_insider_data = "1"
        sig = r.insider_activity.get("net_signal", "")
        ins_score = r.score_insider
        if sig == "Buying":
            insider = "supports_buy"
        elif sig == "Selling" and ins_score is not None and ins_score <= 35:
            insider = "caution"
        else:
            insider = "no_signal"
    # Whether tax analysis is populated (for "show tax-relevant" filter)
    has_tax = "1" if getattr(r, "tax", None) is not None else "0"
    # Combined search text — lowercased for case-insensitive contains() matching
    search_text = f"{r.ticker} {r.name} {r.sector or ''}".lower()
    return (
        f"<tr data-verdict='{verdict}' data-verdict-score='{verdict_score}' "
        f"data-quality='{quality}' "
        f"data-gain='{gain}' data-gain-pct='{gain_pct}' "
        f"data-upside='{upside}' "
        f"data-bucket='{bucket}' "
        f"data-sector-mom='{sector_mom_label}' data-sector='{sector_raw}' "
        f"data-pos52='{pos52}' data-score='{score}' "
        f"data-insider='{insider}' data-has-insider='{has_insider_data}' "
        f"data-trend='{trend}' data-ma-pct='{ma_pct}' "
        f"data-port-pct='{port_pct}' "
        f"data-days-held='{days_held}' "
        f"data-recommendation='{recommendation}' "
        f"data-has-tax='{has_tax}' "
        f"data-search='{search_text}'>"
    )


def _sector_cell(r) -> str:
    """Render the sector name with a Hot/Neutral/Cool momentum badge."""
    sector = r.sector or "—"
    sm = r.sector_momentum or {}
    label = sm.get("label", "Unknown")
    color = sm.get("color", "#bdc3c7")
    pct = sm.get("pct_vs_200ma")
    if label in ("Unknown", None):
        return f"<span style='font-size:11px;color:#7f8c8d;'>{sector}</span>"
    icon = {"Hot": "🔥", "Cool": "❄️", "Neutral": "→"}.get(label, "")
    title = f"{sm.get('etf','')}: {pct:+.1f}% vs 200-day avg" if pct is not None else ""
    return (
        f"<div style='font-size:11px;'>{sector}</div>"
        f"<span title=\"{title}\" style='font-size:10px;font-weight:600;"
        f"color:#fff;background:{color};padding:1px 6px;border-radius:8px;'>"
        f"{icon} {label}</span>"
    )


def _range52_cell(r) -> str:
    """Render where the price sits in its 52-week range as a mini bar."""
    pos = r.week52_position
    if pos is None:
        return "—"
    # Color: near high = amber (caution), near low = blue (value), mid = neutral
    if pos >= 90:
        bar_color, note = "#e67e22", "near high"
    elif pos <= 25:
        bar_color, note = "#2980b9", "near low"
    else:
        bar_color, note = "#95a5a6", ""
    bar = (
        f"<div style='position:relative;width:64px;height:8px;background:#ecf0f1;"
        f"border-radius:4px;display:inline-block;vertical-align:middle;'>"
        f"<div style='position:absolute;left:{min(max(pos,0),100):.0f}%;top:-2px;"
        f"width:3px;height:12px;background:{bar_color};border-radius:2px;'></div>"
        f"</div>"
    )
    label = (f" <span style='font-size:10px;color:#7f8c8d;'>{pos:.0f}%"
             f"{(' · ' + note) if note else ''}</span>")
    return bar + label


def _trend_cell(r) -> str:
    """Render the price trend (vs 50d/200d MAs) as a compact chip."""
    if not r.trend:
        return "<span style='color:#bdc3c7;font-size:11px;'>—</span>"

    if r.trend == "uptrend":
        bg, color, icon, label = "#d4edda", "#1e7e34", "↑", "Uptrend"
    elif r.trend == "downtrend":
        bg, color, icon, label = "#f8d7da", "#a02622", "↓", "Downtrend"
    else:  # sideways
        bg, color, icon, label = "#ecf0f1", "#7f8c8d", "→", "Sideways"

    # Tooltip with the actual MA values for transparency
    title_parts = []
    if r.current_price:
        title_parts.append(f"Price ${r.current_price:.2f}")
    if r.ma_50:
        title_parts.append(f"50d MA ${r.ma_50:.2f}")
    if r.ma_200:
        title_parts.append(f"200d MA ${r.ma_200:.2f}")
    title = " · ".join(title_parts) if title_parts else "Trend"

    # Caption: % above 200d MA. Strongly positive = real trend, near 0 = weak.
    caption = ""
    if r.pct_above_ma200 is not None:
        sign = "+" if r.pct_above_ma200 > 0 else ""
        caption = (f"<div style='font-size:10px;color:#7f8c8d;margin-top:2px;'>"
                   f"{sign}{r.pct_above_ma200:.0f}% vs 200d</div>")

    return (
        f"<span title=\"{title}\" style='display:inline-block;background:{bg};"
        f"color:{color};padding:2px 7px;border-radius:10px;font-size:11px;"
        f"font-weight:600;'>{icon} {label}</span>{caption}"
    )


def _ticker_cell(r) -> str:
    """Render ticker with business-summary tooltip on hover."""
    if not r.ticker:
        return "—"
    if r.business_summary:
        # Escape attribute-breaking chars
        summary = (r.business_summary
                   .replace("&", "&amp;")
                   .replace("'", "&#39;")
                   .replace('"', "&quot;"))
        return (f"<span class='ticker' style='cursor:help;' "
                f"title='{summary}'>{r.ticker}</span>")
    return f"<span class='ticker'>{r.ticker}</span>"


def _name_sector_cell(r) -> str:
    """Combined Name + Sector — name primary, sector momentum badge below.

    The full business summary appears as a tooltip on hover of the name.
    """
    name = r.name or "—"
    # Build the name with optional business-summary tooltip
    if r.business_summary:
        summary = (r.business_summary
                   .replace("&", "&amp;")
                   .replace("'", "&#39;")
                   .replace('"', "&quot;"))
        name_html = (f"<div style='font-weight:500;cursor:help;' "
                     f"title='{summary}'>{name}</div>")
    else:
        name_html = f"<div style='font-weight:500'>{name}</div>"

    sm = r.sector_momentum or {}
    label = sm.get("label", "")
    if not r.sector or label in ("Unknown", None, ""):
        return name_html
    color = sm.get("color", "#bdc3c7")
    icon = {"Hot": "🔥", "Cool": "❄️", "Neutral": "→"}.get(label, "")
    pct = sm.get("pct_vs_200ma")
    title = f"{sm.get('etf','')}: {pct:+.1f}% vs 200d" if pct is not None else ""
    return (
        f"{name_html}"
        f"<div style='font-size:10px;color:var(--fg-muted);margin-top:2px;'>"
        f"{r.sector} "
        f"<span title=\"{title}\" style='font-size:9px;font-weight:600;"
        f"color:#fff;background:{color};padding:1px 5px;border-radius:6px;"
        f"margin-left:3px;'>{icon} {label}</span></div>"
    )


def _position_cell(r) -> str:
    """Combined Mkt Val + %Port — value primary, percent as subtitle."""
    if r.live_market_value is None and r.live_pct_portfolio is None:
        return "—"
    val = _fmt_money(r.live_market_value) if r.live_market_value is not None else "—"
    pct = _fmt_pct(r.live_pct_portfolio, 2) if r.live_pct_portfolio is not None else ""
    return (
        f"<div>{val}</div>"
        f"<div style='font-size:10px;color:var(--fg-muted);margin-top:1px;'>"
        f"{pct}</div>"
    )


def _cost_gain_cell(r) -> str:
    """Combined Cost/Share + Unrealized — avg cost primary, gain $/% below."""
    if r.average_buy_price is None and r.unrealized_gain is None:
        return "—"
    cost_part = (f"<div>{_fmt_money(r.average_buy_price)} avg</div>"
                 if r.average_buy_price is not None else "")
    if r.unrealized_gain is not None:
        cls = "pos-up" if r.unrealized_gain > 0 else (
              "pos-down" if r.unrealized_gain < 0 else "")
        gain_part = (
            f"<div style='font-size:10px;margin-top:1px;' class='{cls}'>"
            f"{_fmt_money(r.unrealized_gain)} "
            f"({_fmt_pct(r.unrealized_gain_pct, 1, True)})</div>"
        )
    else:
        gain_part = ""
    return cost_part + gain_part


def _price_target_cell(r) -> str:
    """Combined Price + Target + Upside — arrow shows direction; subtitle shows %."""
    if r.current_price is None:
        return "—"
    parts = [f"<div><strong>{_fmt_money(r.current_price)}</strong>"]
    if r.target_mean is not None:
        parts.append(f" <span style='color:var(--fg-muted);font-size:11px;'>"
                     f"→ {_fmt_money(r.target_mean)}</span>")
    parts.append("</div>")
    if r.upside_pct is not None:
        cls = "pos-up" if r.upside_pct > 0 else "pos-down"
        parts.append(
            f"<div style='font-size:10px;margin-top:1px;' class='{cls}'>"
            f"{_fmt_pct(r.upside_pct, 1, True)} upside</div>"
        )
    return "".join(parts)


def _range_trend_cell(r) -> str:
    """Combined 52W Range mini-bar + Trend chip stacked vertically."""
    parts = []
    # Top: range mini-bar
    if r.week52_position is not None:
        pos = r.week52_position
        if pos >= 90:
            bar_color, note = "#e67e22", "near high"
        elif pos <= 25:
            bar_color, note = "#2980b9", "near low"
        else:
            bar_color, note = "#95a5a6", ""
        bar = (
            f"<div style='display:flex;align-items:center;gap:6px;'>"
            f"<div style='position:relative;width:54px;height:6px;"
            f"background:var(--bg-chip-neutral);border-radius:3px;'>"
            f"<div style='position:absolute;left:{min(max(pos,0),100):.0f}%;top:-3px;"
            f"width:3px;height:12px;background:{bar_color};border-radius:2px;'></div>"
            f"</div>"
            f"<span style='font-size:10px;color:var(--fg-muted);'>{pos:.0f}%"
            f"{(' · ' + note) if note else ''}</span></div>"
        )
        parts.append(bar)
    # Bottom: trend chip
    if r.trend:
        if r.trend == "uptrend":
            bg, color, icon, label = "#d4edda", "#1e7e34", "↑", "Up"
        elif r.trend == "downtrend":
            bg, color, icon, label = "#f8d7da", "#a02622", "↓", "Down"
        else:
            bg, color, icon, label = "#ecf0f1", "#7f8c8d", "→", "Sideways"
        ma_part = ""
        if r.pct_above_ma200 is not None:
            ma_part = f" <span style='color:var(--fg-muted);'>{r.pct_above_ma200:+.0f}%</span>"
        title_parts = []
        if r.current_price: title_parts.append(f"Price ${r.current_price:.2f}")
        if r.ma_50: title_parts.append(f"50d ${r.ma_50:.2f}")
        if r.ma_200: title_parts.append(f"200d ${r.ma_200:.2f}")
        parts.append(
            f"<div style='margin-top:3px;'>"
            f"<span title=\"{' · '.join(title_parts)}\" "
            f"style='display:inline-block;background:{bg};color:{color};"
            f"padding:1px 6px;border-radius:8px;font-size:10px;"
            f"font-weight:600;'>{icon} {label}</span>{ma_part}</div>"
        )
    return "".join(parts) if parts else "—"


def _score_cell(score: Optional[float], q: Optional[float] = None,
                g: Optional[float] = None, v: Optional[float] = None,
                a: Optional[float] = None,
                ins: Optional[float] = None) -> str:
    """Render the Composite Score as a single bold number.

    Sub-score breakdown (Q/G/V/A/I) moves to the hover tooltip so the cell
    stays compact — just the number. The header column also has a tooltip
    explaining what the composite is.
    """
    if score is None:
        return "<span style='color:var(--fg-faint);'>—</span>"
    # Color: red <40, amber 40-60, olive 50-69, green 70+
    if score >= 70:
        color = "var(--pos-up)"
    elif score >= 50:
        color = "#7d9b3a"
    elif score >= 35:
        color = "#e67e22"
    else:
        color = "var(--pos-down)"

    # Build tooltip lines — each sub-score on its own line for readability.
    # Lines are joined with literal '\n' so the native browser tooltip wraps.
    lines = [f"Composite Score: {score:.0f}"]
    parts = [("Quality", q), ("Growth", g), ("Value", v),
             ("Analyst", a), ("Insider", ins)]
    parts = [(n, s) for n, s in parts if s is not None]
    if parts:
        lines.append("")  # blank line separator
        for name, sub in parts:
            lines.append(f"{name:<8} {sub:.0f}")
    title = "\n".join(lines).replace("'", "&#39;").replace('"', "&quot;")

    return (
        f"<span title='{title}' style='font-weight:700;color:{color};"
        f"font-size:15px;cursor:help;font-variant-numeric:tabular-nums;'>"
        f"{score:.0f}</span>"
    )


def _insider_cell(activity: Optional[dict]) -> str:
    """Render insider 90-day activity as a decision-oriented signal.

    Instead of describing WHAT insiders did, this answers "should this affect
    my buy decision?" with three actionable states:

      ✓ Supports buy   — meaningful open-market buying (real conviction signal)
      — No signal      — typical compensation/plan/tax activity (most mega-caps)
      ⚠ Caution        — discretionary selling large enough relative to size
                         to warrant investigation before buying

    The underlying "Buying / Selling / Scheduled selling / Cashing out /
    Compensation / Neutral" breakdown is still in the tooltip for users who
    want to dig in.
    """
    if not activity:
        return (
            "<span style='display:inline-block;background:#ecf0f1;color:#7f8c8d;"
            "padding:2px 7px;border-radius:10px;font-size:11px;font-weight:600;'>"
            "— No signal</span>"
            "<div style='font-size:10px;color:#bdc3c7;margin-top:2px;'>"
            "no Form 4 data</div>"
        )

    signal = activity.get("net_signal", "Neutral")
    bc = activity.get("buy_count", 0)
    sc = activity.get("sell_count", 0)
    bv = activity.get("buy_value", 0.0)
    sv = activity.get("sell_value", 0.0)
    tw_value = activity.get("tax_withhold_value", 0)
    plan_value = activity.get("plan_value", 0.0)
    discretionary_sv = activity.get("discretionary_sell_value",
                                     max(sv - plan_value, 0.0))
    plan_filings = activity.get("plan_filings", 0)
    other_count = activity.get("other_activity_count", 0)
    total = activity.get("total_filings", bc + sc + other_count)
    score = activity.get("_score")  # set by caller for size-aware decision

    def _money(v):
        a = abs(v)
        if a >= 1_000_000: return f"${a/1_000_000:.1f}M"
        if a >= 1_000: return f"${a/1_000:.0f}k"
        return f"${a:.0f}"

    # ---- Decide the verdict from underlying signal + score ----
    # The Insider score (0-100) was computed with size-awareness already, so
    # we can lean on it. Score >=70 = clear buy support. Score <=35 = caution.
    # Everything in between is too noisy to act on (mega-cap compensation).

    if signal == "Buying" and bc >= 2:
        verdict = "Supports buy"
        bg, color, icon = "#d4edda", "#1e7e34", "✓"
        # Caption shows the conviction-relevant info
        if plan_value > 0:
            detail = f"{_money(bv)} bought · {bc} insider(s)"
        else:
            detail = f"{_money(bv)} bought · {bc} insider(s)"
        sub_reason = "voluntary cash purchase"
    elif signal == "Selling" and score is not None and score <= 35:
        # Real discretionary selling that was large enough relative to cap
        # for the size-aware scorer to flag it as serious.
        verdict = "Caution"
        bg, color, icon = "#f8d7da", "#a02622", "⚠"
        detail = f"-{_money(discretionary_sv)} discretionary · {sc} sells"
        sub_reason = "meaningful relative to size"
    elif signal == "Selling":
        # Real selling but small relative to market cap → not actionable
        verdict = "No signal"
        bg, color, icon = "#ecf0f1", "#7f8c8d", "—"
        detail = f"-{_money(discretionary_sv)} disc. · small for size"
        sub_reason = "tiny vs market cap"
    elif signal == "Scheduled selling":
        verdict = "No signal"
        bg, color, icon = "#ecf0f1", "#7f8c8d", "—"
        detail = f"-{_money(sv)} preset 10b5-1 plan"
        sub_reason = "scheduled trades"
    elif signal == "Cashing out":
        verdict = "No signal"
        bg, color, icon = "#ecf0f1", "#7f8c8d", "—"
        detail = f"{_money(tw_value)} tax-cover only"
        sub_reason = "mechanical RSU vest"
    elif signal == "Compensation":
        verdict = "No signal"
        bg, color, icon = "#ecf0f1", "#7f8c8d", "—"
        detail = f"{total} grants/exercises"
        sub_reason = "no open-market trades"
    else:  # Neutral
        verdict = "No signal"
        bg, color, icon = "#ecf0f1", "#7f8c8d", "—"
        detail = f"{total} filings"
        sub_reason = "no clear direction"

    # ---- Tooltip: complete breakdown for users who want to dig in ----
    tooltip_parts = [
        f"Underlying signal: {signal}",
        f"{bc} open-market buy(s), {sc} open-market sell(s)",
        f"{total} total Form 4 filings",
    ]
    if bv > 0:
        tooltip_parts.append(f"{_money(bv)} bought (P-code, discretionary)")
    if discretionary_sv > 0:
        tooltip_parts.append(f"{_money(discretionary_sv)} discretionary selling")
    if plan_value > 0:
        tooltip_parts.append(
            f"{_money(plan_value)} via 10b5-1 plan ({plan_filings} filings) - "
            f"preset, low signal"
        )
    if tw_value > 0:
        tooltip_parts.append(f"{_money(tw_value)} tax-withhold (mechanical)")
    if other_count > 0:
        tooltip_parts.append(f"{other_count} grants/exercises")
    tooltip_parts.append(f"source: {activity.get('source','?')}")
    title = " | ".join(tooltip_parts)

    return (
        f"<span title=\"{title}\" style='display:inline-block;"
        f"background:{bg};color:{color};padding:2px 7px;"
        f"border-radius:10px;font-size:11px;font-weight:600;'>"
        f"{icon} {verdict}</span>"
        f"<div style='font-size:10px;color:#7f8c8d;margin-top:2px;'>"
        f"{detail}<br><span style='color:#bdc3c7;'>{sub_reason}</span></div>"
    )


def _filter_dots(filters: list[FilterResult]) -> str:
    """Render filter pass/fail as colored dots with hover tooltip."""
    parts = []
    for f in filters:
        color = "#27ae60" if f.passed else "#c0392b"
        # Format actual value with units. For filters whose `note` is empty
        # or a "%" unit indicator, treat it as the unit suffix (legacy
        # behavior). For filters whose `note` is descriptive metadata
        # like "3yr CAGR" / "1yr YoY", append it as a separate clause
        # in the tooltip so it doesn't get smashed into the number.
        if f.actual is None:
            actual_str = "n/a"
        elif f.note in (None, "", "%"):
            actual_str = f"{f.actual:.1f}{f.note or '%'}"
        else:
            # Note is descriptive — render value with default % unit,
            # then append the note as context.
            actual_str = f"{f.actual:.1f}% ({f.note})"
        title = f"{f.name}: {actual_str} (threshold {f.threshold})"
        parts.append(
            f'<span title="{title}" '
            f'style="display:inline-block;width:10px;height:10px;'
            f'border-radius:50%;background:{color};margin:0 1px;"></span>'
        )
    return "".join(parts)


def _rating_bar(breakdown: Optional[dict], rec_key: Optional[str],
                num_analysts: Optional[int]) -> str:
    """Robinhood-style horizontal bar showing buy/hold/sell distribution.

    Accepts normalized breakdown {buy, hold, sell, total, source}.
    """
    if breakdown and breakdown.get("total"):
        total = breakdown["total"]
        buy = breakdown.get("buy", 0)
        hold = breakdown.get("hold", 0)
        sell = breakdown.get("sell", 0)
        source = breakdown.get("source", "")
        # Pct widths
        pcts = [buy / total * 100, hold / total * 100, sell / total * 100]
        colors = ["#27ae60", "#f39c12", "#c0392b"]
        bar = (
            f'<div style="display:flex;height:10px;border-radius:3px;overflow:hidden;'
            f'min-width:90px;margin-bottom:2px;">'
        )
        for pct, color in zip(pcts, colors):
            if pct > 0:
                bar += f'<div style="width:{pct:.1f}%;background:{color};"></div>'
        bar += "</div>"
        bar += (
            f'<div style="font-size:10px;color:#7f8c8d;">'
            f'{buy} Buy · {hold} Hold · {sell} Sell'
            f'<span style="color:#bdc3c7;"> · {source}</span></div>'
        )
        return bar
    if rec_key:
        label = rec_key.upper().replace("_", " ")
        count = f" ({num_analysts})" if num_analysts else ""
        return f'<span style="font-size:12px;">{label}{count}</span>'
    return "—"


def _render_screening_section(sr: dict) -> str:
    """
    Render the S&P 500/400 screening output.
    sr: {"passed": [ScreenResult], "near_miss": [ScreenResult], "universe_size": int}
    """
    passed = sr.get("passed") or []
    near_miss = sr.get("near_miss") or []
    uni = sr.get("universe_size", 0)

    html = "<h2 style='margin-top:48px;'>📊 Screening — S&amp;P 500 + 400</h2>\n"
    html += (
        f'<p style="color:#7f8c8d;font-size:12px;margin-top:-6px;margin-bottom:8px;">'
        f"Screened {uni} tickers against the 9-filter quality framework. "
        f"<strong>{len(passed)}</strong> passed all 9; "
        f"<strong>{len(near_miss)}</strong> failed only 1-2 (near misses, sorted by score).</p>\n"
    )
    html += (
        '<p style="background:#f1f8e9;border-left:3px solid #689f38;padding:8px 12px;'
        'font-size:11px;color:#33691e;margin-bottom:16px;border-radius:3px;">'
        "<strong>Filters:</strong> Rev/EPS Growth ≥10%/yr · ROE ≥15% · Op Margin ≥15% · "
        "D/E &lt;1.0 · Quick &gt;1.0 · FCF positive &amp; growing · P/E &lt;30 · PEG &lt;2.0 "
        "<br><strong>RecAvg:</strong> 1 = Strong Buy, 5 = Strong Sell · "
        "<strong>52w Pos:</strong> 0% = at 52-wk low, 100% = at 52-wk high · "
        "<strong>Insider 90d:</strong> Decision verdict — Supports buy / No signal / Caution · "
        "<strong>Score:</strong> Composite of Quality 30% · Growth 20% · Value 20% · Analyst 15% · Insider 15% · "
        "<strong>#F:</strong> Number of filters failed (1 or 2 for near misses)</p>\n"
    )

    def _render_table(rows: list, is_near_miss: bool) -> str:
        if not rows:
            return ""
        h = "<div class='table-wrap'><table>\n<thead><tr>"
        h += "<th>Ticker</th><th>Name</th><th>Sector</th>"
        h += "<th class='num'>Price</th><th class='num'>Target</th>"
        h += "<th class='num'>Upside</th><th class='num'>52w Pos</th>"
        h += "<th class='num'>RecAvg</th><th class='num'># Analysts</th>"
        h += "<th class='num'>Quality</th><th class='num'>Growth</th>"
        h += "<th class='num'>Value</th><th class='num'>Analyst</th>"
        h += "<th title='Decision verdict from insider activity. &#10003; Supports buy = real open-market buying with personal cash (rare, strong positive). &mdash; No signal = typical compensation, 10b5-1 plans, or tax-withholds (most mega-caps; ignore). &#9888; Caution = discretionary selling large enough relative to market cap to warrant a closer look before buying.' style='cursor:help;'>Insider 90d <span style='color:#bdc3c7;font-size:10px;'>&#9432;</span></th>"
        h += "<th class='num' title='Composite of Quality 30% + Growth 20% + Value 20% + Analyst 15% + Insider 15%. Hover any cell for sub-score breakdown.' style='cursor:help;'>Composite <span style='color:var(--fg-faint);font-weight:400;font-size:10px;text-transform:none;letter-spacing:0;'>&#9432;</span></th>"
        if is_near_miss:
            h += "<th class='num'>#F</th><th>Failed</th>"
        h += "</tr></thead><tbody>\n"
        for r in rows:
            upside_class = ""
            if r.upside_pct is not None:
                upside_class = "pos-up" if r.upside_pct > 0 else "pos-down"
            failed_list = ""
            n_failed = 0
            if is_near_miss:
                fails = [k for k, v in (r.passes or {}).items() if v is False]
                n_failed = len(fails)
                failed_list = ", ".join(fails)
            h += "<tr>"
            h += _td(r.ticker, r.ticker, "ticker")
            h += _td(r.name or r.ticker, r.name or r.ticker)
            h += _td(r.sector or "—", r.sector or "zzz")
            h += _td(_fmt_money(r.price), r.price or -1, "num")
            h += _td(_fmt_money(r.target_mean), r.target_mean or -1, "num")
            h += _td(_fmt_pct(r.upside_pct, 1, True),
                     r.upside_pct if r.upside_pct is not None else -1e6,
                     f"num {upside_class}")
            h += _td(f"{r.week52_pos:.0f}%" if r.week52_pos is not None else "—",
                     r.week52_pos if r.week52_pos is not None else -1, "num")
            h += _td(f"{r.rec_avg:.2f}" if r.rec_avg is not None else "—",
                     r.rec_avg if r.rec_avg is not None else 99, "num")
            h += _td(str(r.num_analysts) if r.num_analysts else "—",
                     r.num_analysts or 0, "num")
            h += _td(f"{r.score_quality:.0f}" if r.score_quality is not None else "—",
                     r.score_quality if r.score_quality is not None else -1, "num")
            h += _td(f"{r.score_growth:.0f}" if r.score_growth is not None else "—",
                     r.score_growth if r.score_growth is not None else -1, "num")
            h += _td(f"{r.score_value:.0f}" if r.score_value is not None else "—",
                     r.score_value if r.score_value is not None else -1, "num")
            h += _td(f"{r.score_analyst:.0f}" if r.score_analyst is not None else "—",
                     r.score_analyst if r.score_analyst is not None else -1, "num")
            h += _td(_insider_cell(getattr(r, 'insider_activity', None)),
                     getattr(r, 'score_insider', None) if getattr(r, 'score_insider', None) is not None else -1)
            score_cell = _score_cell(
                r.score_composite, r.score_quality, r.score_growth,
                r.score_value, r.score_analyst,
                getattr(r, 'score_insider', None),
            )
            h += _td(score_cell, r.score_composite if r.score_composite is not None else -1, "num")
            if is_near_miss:
                h += _td(str(n_failed), n_failed, "num")
                h += _td(failed_list, failed_list)
            h += "</tr>\n"
        h += "</tbody></table></div>\n"
        return h

    if passed:
        html += f"<h3 style='margin-top:18px;'>✓ Passed all 9 filters ({len(passed)})</h3>\n"
        html += _render_table(passed, is_near_miss=False)
    if near_miss:
        html += f"<h3 style='margin-top:18px;'>≈ Near misses (failed 1-2) ({len(near_miss)})</h3>\n"
        html += _render_table(near_miss, is_near_miss=True)
    return html


def _render_ytd_summary(ytd, cfg=None) -> str:
    """Render the YTD realized-gains summary card with tax estimate."""
    # Choose card accent color based on whether we owe or have losses
    if ytd.net_total_gain > 0:
        accent = "var(--pos-down)"   # owing tax
        sign = "+"
    elif ytd.net_total_gain < 0:
        accent = "var(--pos-up)"     # losses = tax benefit
        sign = ""
    else:
        accent = "var(--fg-muted)"
        sign = ""

    if ytd.realized_count == 0:
        # No realized activity this year
        return (
            f"<div style='background:var(--bg-card);border:1px solid var(--border-medium);"
            f"border-radius:8px;padding:14px 16px;margin-bottom:18px;'>"
            f"<div style='font-weight:600;font-size:14px;color:var(--fg-strong);margin-bottom:4px;'>"
            f"{ytd.year} Year-to-Date Realized Gains</div>"
            f"<div style='color:var(--fg-muted);font-size:12px;'>"
            f"No realized sales yet in {ytd.year}. Estimated tax owed on YTD: <strong>$0</strong>."
            f"</div></div>\n"
        )

    rows = []
    # Gross gain/loss rows
    rows.append((
        "Short-term gains", ytd.st_gains, "var(--pos-up)",
        f"Realized gains held ≤ 1 year"
    ))
    rows.append((
        "Short-term losses", -ytd.st_losses, "var(--pos-down)",
        f"Realized losses held ≤ 1 year"
    ))
    rows.append((
        "Long-term gains", ytd.lt_gains, "var(--pos-up)",
        f"Realized gains held > 1 year"
    ))
    rows.append((
        "Long-term losses", -ytd.lt_losses, "var(--pos-down)",
        f"Realized losses held > 1 year"
    ))

    rows_html = ""
    for label, val, color, tooltip in rows:
        if abs(val) < 0.01:
            continue
        rows_html += (
            f"<tr title='{tooltip}'>"
            f"<td style='padding:3px 12px 3px 0;color:var(--fg-body);'>{label}</td>"
            f"<td style='padding:3px 0;text-align:right;color:{color};font-variant-numeric:tabular-nums;'>"
            f"{_fmt_money(val)}</td></tr>"
        )

    # Net rows
    rows_html += (
        f"<tr style='border-top:1px solid var(--border-medium);'>"
        f"<td style='padding:6px 12px 3px 0;color:var(--fg-strong);font-weight:600;'>Net total gain/loss</td>"
        f"<td style='padding:6px 0 3px;text-align:right;color:{accent};font-weight:700;font-variant-numeric:tabular-nums;'>"
        f"{sign}{_fmt_money(ytd.net_total_gain)}</td></tr>"
    )

    # Decomposition note (helpful when ST and LT have different rates)
    decomp_lines = []
    if ytd.st_tax_component > 0:
        decomp_lines.append(
            f"<div style='font-size:11px;color:var(--fg-muted);'>"
            f"&nbsp;&nbsp;Short-term: {_fmt_money(ytd.net_st_gain)} × ordinary rates → "
            f"<strong style='color:var(--fg-body);'>{_fmt_money(ytd.st_tax_component)}</strong></div>"
        )
    if ytd.lt_tax_component > 0:
        decomp_lines.append(
            f"<div style='font-size:11px;color:var(--fg-muted);'>"
            f"&nbsp;&nbsp;Long-term: {_fmt_money(ytd.net_lt_gain)} × LTCG rates → "
            f"<strong style='color:var(--fg-body);'>{_fmt_money(ytd.lt_tax_component)}</strong></div>"
        )
    if ytd.ordinary_offset_used > 0:
        decomp_lines.append(
            f"<div style='font-size:11px;color:var(--pos-up);'>"
            f"&nbsp;&nbsp;Losses offsetting ordinary income (${ytd.ordinary_offset_used:,.0f} "
            f"used of $3,000 max) → saves <strong>{_fmt_money(ytd.ordinary_tax_saved)}</strong></div>"
        )
    if ytd.loss_carryforward > 0:
        decomp_lines.append(
            f"<div style='font-size:11px;color:var(--fg-muted);'>"
            f"&nbsp;&nbsp;Carries forward to next year: <strong style='color:var(--fg-body);'>"
            f"{_fmt_money(ytd.loss_carryforward)}</strong></div>"
        )

    return (
        f"<div style='background:var(--bg-card);border:1px solid var(--border-medium);"
        f"border-left:4px solid {accent};border-radius:8px;"
        f"padding:14px 16px;margin-bottom:18px;'>"
        f"<div style='font-weight:600;font-size:14px;color:var(--fg-strong);margin-bottom:8px;'>"
        f"{ytd.year} Year-to-Date Realized Gains "
        f"<span style='color:var(--fg-muted);font-size:11px;font-weight:400;'>"
        f"({ytd.realized_count} sale match{'es' if ytd.realized_count != 1 else ''})</span>"
        f"</div>"
        f"<table style='width:100%;border-collapse:collapse;font-size:13px;'>"
        f"{rows_html}"
        f"</table>"
        f"<div style='margin-top:10px;padding-top:10px;border-top:1px solid var(--border-soft);'>"
        f"<div style='font-size:13px;'>"
        f"Estimated tax owed on YTD realized gains: "
        f"<strong style='color:{accent};font-size:16px;'>{_fmt_money(ytd.estimated_tax)}</strong></div>"
        f"{''.join(decomp_lines)}"
        f"{_render_fallback_hint(cfg) if cfg else ''}"
        f"</div></div>\n"
    )


def _render_fallback_hint(cfg) -> str:
    """If TAX_TAXABLE_INCOME isn't set, show a small note that defaults are in use.

    Without configured income, _marginal_*_rate would put the user in the 0%
    bracket — producing a misleadingly low tax estimate. Our code falls back
    to representative 15% LT / 24% ST rates, but the user should know that.
    """
    if cfg is None or cfg.is_configured:
        return ""
    return (
        "<div style='font-size:11px;color:var(--fg-muted);margin-top:6px;"
        "font-style:italic;'>Using representative default rates "
        "(15% LT, 24% ST). Set <code>TAX_TAXABLE_INCOME</code>, "
        "<code>TAX_FILING_STATUS</code>, <code>TAX_STATE_RATE</code>, "
        "and <code>TAX_APPLY_NIIT</code> in your .env for personalized "
        "estimates.</div>"
    )


def _render_tax_recommendations(recs: list[dict]) -> str:
    """Render tax-minimization recommendations as priority-ordered cards."""
    if not recs:
        return ""

    priority_color = {
        "high": ("var(--pos-down)", "🔴"),
        "medium": ("#e67e22", "🟡"),
        "low": ("var(--fg-muted)", "🔵"),
    }

    html = (
        "<h3 style='margin-top:18px;margin-bottom:10px;'>"
        "Tax-Minimization Recommendations</h3>"
        "<p style='color:var(--fg-muted);font-size:12px;margin-top:-4px;margin-bottom:12px;'>"
        "Ordered by impact. Estimated $ savings shown where applicable.</p>"
    )

    for rec in recs:
        color, icon = priority_color.get(rec["priority"], ("var(--fg-muted)", "•"))
        impact = rec.get("dollar_impact", 0)
        impact_str = ""
        if impact and abs(impact) >= 50:
            impact_str = (
                f"<span style='font-size:12px;color:{color};font-weight:600;"
                f"margin-left:8px;'>~{_fmt_money(impact)} impact</span>"
            )
        html += (
            f"<div style='background:var(--bg-card);border:1px solid var(--border-medium);"
            f"border-left:4px solid {color};border-radius:6px;"
            f"padding:10px 14px;margin-bottom:8px;'>"
            f"<div style='display:flex;align-items:center;margin-bottom:4px;'>"
            f"<span style='font-size:10px;color:{color};font-weight:700;"
            f"text-transform:uppercase;letter-spacing:0.4px;margin-right:8px;'>"
            f"{rec['priority']}</span>"
            f"<span style='font-size:11px;color:var(--fg-muted);'>{rec['category']}</span>"
            f"{impact_str}</div>"
            f"<div style='font-weight:600;color:var(--fg-strong);font-size:13px;margin-bottom:4px;'>"
            f"{rec['headline']}</div>"
            f"<div style='font-size:12px;color:var(--fg-body);line-height:1.5;'>"
            f"{rec['detail']}</div>"
            f"</div>"
        )
    return html


def _render_tax_section(flagged: list,
                       all_holdings: Optional[list] = None,
                       realized_ytd: Optional[dict] = None) -> str:
    """Render the tax section: YTD realized + recommendations + per-position trim guidance.

    Args:
      flagged: list of PositionAnalysis with `tax` field populated (SELL/TRIM verdicts)
      all_holdings: full holdings list (used to find loss-harvest candidates)
      realized_ytd: dict from fetch_realized_ytd() — if present, YTD section renders
    """
    html = "<h2 style='margin-top:48px;'>Tax-Aware Trim Guidance</h2>\n"
    html += (
        '<p style="color:var(--fg-muted);font-size:12px;margin-top:-6px;margin-bottom:8px;">'
        "For positions flagged SELL or TRIM: holding-period status, estimated tax "
        "if trimmed now, and the least-taxable ways to do it."
        "</p>\n"
    )
    html += (
        '<p style="background:var(--bg-chip-yellow);border-left:3px solid #f39c12;padding:8px 12px;'
        'font-size:11px;color:var(--fg-chip-amber);margin-bottom:18px;border-radius:3px;">'
        "<strong>Not tax advice.</strong> When order history is available, lots are "
        "reconstructed via FIFO (the IRS default) for exact short/long-term splits. "
        "If you manually selected specific lots at past sales, your actual lots may "
        "differ. State tax, NIIT, AMT, and your full income picture also matter. "
        "Confirm in Robinhood's app and consult a tax professional before acting."
        "</p>\n"
    )

    # ---------- YTD realized-gains summary + recommendations ----------
    if realized_ytd:
        try:
            from tax_analysis import (TaxConfig, compute_ytd_tax_estimate,
                                       generate_tax_minimization_recommendations)
            from datetime import datetime
            cfg = TaxConfig.from_env()
            ytd_est = compute_ytd_tax_estimate(realized_ytd, cfg)

            # Build lightweight holdings data for ALL positions (not just flagged).
            # Need: ticker, unrealized_gain, days_held — enough to identify
            # loss-harvest candidates, LT-threshold candidates, and big winners.
            holdings_data = []
            now = datetime.now()
            for r in (all_holdings or []):
                if not getattr(r, "ticker", None):
                    continue
                days_held = None
                opened = getattr(r, "position_opened", None)
                if opened:
                    try:
                        d_opened = datetime.strptime(opened[:10], "%Y-%m-%d")
                        days_held = (now - d_opened).days
                    except Exception:
                        pass
                holdings_data.append({
                    "ticker": r.ticker,
                    "unrealized_gain": getattr(r, "unrealized_gain", None),
                    "days_held": days_held,
                })
            recs = generate_tax_minimization_recommendations(
                ytd_est, holdings_data, cfg
            )

            html += _render_ytd_summary(ytd_est, cfg=cfg)
            if recs:
                html += _render_tax_recommendations(recs)
        except Exception as e:
            print(f"[tax-section] Could not render YTD summary: {e}")

    # ---------- Per-position trim guidance (existing behavior) ----------
    if flagged:
        html += "<h3 style='margin-top:32px;'>Per-Position Trim Detail</h3>\n"

    for r in flagged:
        ta = r.tax
        verdict_color = r.verdict.color if r.verdict else "#7f8c8d"
        # Header row
        html += (
            f"<div style='border:1px solid var(--border-medium);border-radius:8px;background:var(--bg-card);"
            f"padding:14px 16px;margin-bottom:14px;'>"
        )
        html += (
            f"<div style='display:flex;align-items:center;gap:10px;margin-bottom:8px;'>"
            f"<span class='ticker' style='font-size:15px;'>{r.ticker}</span>"
            f"<span class='verdict' style='background:{verdict_color}'>"
            f"{r.verdict.label}</span>"
        )
        # Holding period badge
        if getattr(ta, "has_lots", False) and ta.lt_shares and ta.st_shares:
            html += (f"<span style='font-size:11px;background:var(--bg-chip-amber);color:var(--fg-chip-amber);"
                     f"padding:3px 8px;border-radius:4px;'>Mixed: "
                     f"{ta.lt_shares:g} LT + {ta.st_shares:g} ST</span>")
        elif ta.is_long_term is True:
            html += ("<span style='font-size:11px;background:var(--bg-chip-green);color:var(--fg-chip-green);"
                     "padding:3px 8px;border-radius:4px;'>Long-term ✓</span>")
        elif ta.is_long_term is False:
            badge = "Short-term"
            if getattr(ta, "next_lot_to_lt_days", None) is not None:
                badge += f" · {ta.next_lot_to_lt_days}d to long-term"
            elif ta.days_to_long_term is not None:
                badge += f" · {ta.days_to_long_term}d to long-term"
            html += (f"<span style='font-size:11px;background:var(--bg-chip-red);color:var(--fg-chip-red);"
                     f"padding:3px 8px;border-radius:4px;'>{badge}</span>")
        else:
            html += ("<span style='font-size:11px;background:var(--bg-chip-neutral);color:var(--fg-chip-neutral);"
                     "padding:3px 8px;border-radius:4px;'>Holding period unknown</span>")
        html += "</div>\n"

        # Gain + tax estimate line
        gain = ta.unrealized_gain
        if getattr(ta, "has_lots", False):
            # ---- Exact lot-level rendering ----
            lt_sh = ta.lt_shares or 0
            st_sh = ta.st_shares or 0
            lt_g = ta.lt_gain or 0
            st_g = ta.st_gain or 0
            lt_tax = ta.lt_tax or 0
            st_tax = ta.st_tax or 0
            html += "<div style='font-size:12px;color:var(--fg-body);margin-bottom:8px;'>"
            html += (
                f"<table style='margin:0;font-size:12px;width:auto;"
                f"border-collapse:collapse;'>"
                f"<tr><th style='background:#fff;color:#7f8c8d;text-align:left;"
                f"padding:2px 12px 2px 0;border:none;'></th>"
                f"<th style='background:#fff;color:#7f8c8d;text-align:right;"
                f"padding:2px 12px;border:none;'>Shares</th>"
                f"<th style='background:#fff;color:#7f8c8d;text-align:right;"
                f"padding:2px 12px;border:none;'>Unrealized</th>"
                f"<th style='background:#fff;color:#7f8c8d;text-align:right;"
                f"padding:2px 12px;border:none;'>Est. tax if sold</th></tr>"
            )
            html += (
                f"<tr><td style='padding:2px 12px 2px 0;border:none;color:var(--pos-up);'>"
                f"Long-term</td>"
                f"<td style='text-align:right;padding:2px 12px;border:none;'>{lt_sh:g}</td>"
                f"<td style='text-align:right;padding:2px 12px;border:none;'>{_fmt_money(lt_g)}</td>"
                f"<td style='text-align:right;padding:2px 12px;border:none;'>"
                f"{_fmt_money(lt_tax)}"
                f"{f' ({ta.effective_rate_lt*100:.0f}%)' if ta.effective_rate_lt else ''}</td></tr>"
            )
            html += (
                f"<tr><td style='padding:2px 12px 2px 0;border:none;color:var(--pos-down);'>"
                f"Short-term</td>"
                f"<td style='text-align:right;padding:2px 12px;border:none;'>{st_sh:g}</td>"
                f"<td style='text-align:right;padding:2px 12px;border:none;'>{_fmt_money(st_g)}</td>"
                f"<td style='text-align:right;padding:2px 12px;border:none;'>"
                f"{_fmt_money(st_tax)}"
                f"{f' ({ta.effective_rate_st*100:.0f}%)' if ta.effective_rate_st else ''}</td></tr>"
            )
            html += (
                f"<tr style='border-top:1px solid var(--border-medium);font-weight:600;'>"
                f"<td style='padding:3px 12px 3px 0;border:none;'>Total</td>"
                f"<td style='text-align:right;padding:3px 12px;border:none;'>{lt_sh+st_sh:g}</td>"
                f"<td style='text-align:right;padding:3px 12px;border:none;'>{_fmt_money(lt_g+st_g)}</td>"
                f"<td style='text-align:right;padding:3px 12px;border:none;'>{_fmt_money(lt_tax+st_tax)}</td></tr>"
            )
            html += "</table></div>\n"

            # Collapsible per-lot detail
            if ta.lots_detail:
                html += (
                    "<details style='margin-bottom:8px;'>"
                    "<summary style='font-size:11px;color:var(--fg-muted);cursor:pointer;'>"
                    f"View all {len(ta.lots_detail)} lot(s)</summary>"
                    "<table style='margin:6px 0 0;font-size:11px;'>"
                    "<thead><tr>"
                    "<th>Purchased</th><th class='num'>Shares</th>"
                    "<th class='num'>Buy Price</th><th class='num'>Held (days)</th>"
                    "<th>Status</th><th class='num'>Unrealized</th>"
                    "</tr></thead><tbody>"
                )
                for lot in ta.lots_detail:
                    status = ("<span style='color:var(--pos-up);'>LT</span>"
                              if lot["is_long_term"]
                              else f"<span style='color:#a02622;'>ST "
                                   f"({lot['days_to_lt']}d to LT)</span>")
                    gain_color = "var(--pos-up)" if lot["gain"] >= 0 else "var(--pos-down)"
                    html += (
                        f"<tr><td>{lot['date']}</td>"
                        f"<td class='num'>{lot['shares']:g}</td>"
                        f"<td class='num'>{_fmt_money(lot['buy_price'])}</td>"
                        f"<td class='num'>{lot['days_held']}</td>"
                        f"<td>{status}</td>"
                        f"<td class='num' style='color:{gain_color};'>"
                        f"{_fmt_money(lot['gain'])}</td></tr>"
                    )
                html += "</tbody></table></div></details>\n"
        elif gain is not None and gain > 0:
            st = ta.tax_if_short_term
            lt = ta.tax_if_long_term
            parts = [f"Unrealized gain: <strong>{_fmt_money(gain)}</strong>"]
            if st is not None and lt is not None:
                if ta.is_long_term is True:
                    parts.append(
                        f"Est. tax if sold now (long-term): "
                        f"<strong style='color:#1e7e34;'>{_fmt_money(lt)}</strong> "
                        f"({ta.effective_rate_lt*100:.0f}%)"
                    )
                elif ta.is_long_term is False:
                    parts.append(
                        f"Est. tax now (short-term): "
                        f"<strong style='color:#a02622;'>{_fmt_money(st)}</strong> "
                        f"({ta.effective_rate_st*100:.0f}%)"
                    )
                    parts.append(
                        f"If held to long-term: "
                        f"<strong style='color:#1e7e34;'>{_fmt_money(lt)}</strong> "
                        f"({ta.effective_rate_lt*100:.0f}%)"
                    )
                    saved = st - lt
                    if saved > 0:
                        parts.append(
                            f"Potential saving: "
                            f"<strong>{_fmt_money(saved)}</strong>"
                        )
                else:
                    parts.append(
                        f"Est. tax: {_fmt_money(lt)} (LT) / {_fmt_money(st)} (ST)"
                    )
            html += ("<div style='font-size:12px;color:var(--fg-body);margin-bottom:8px;'>"
                     + " &nbsp;·&nbsp; ".join(parts) + "</div>\n")
        elif gain is not None and gain < 0:
            html += (f"<div style='font-size:12px;color:var(--fg-body);margin-bottom:8px;'>"
                     f"Unrealized loss: <strong style='color:var(--pos-down);'>"
                     f"{_fmt_money(gain)}</strong> &nbsp;·&nbsp; "
                     f"Selling harvests a deductible loss</div>\n")
        else:
            html += ("<div style='font-size:12px;color:var(--fg-muted);margin-bottom:8px;'>"
                     "Cost basis unavailable — connect via Robinhood for gain/tax "
                     "estimates</div>\n")

        # Timing note
        if ta.timing_note:
            html += (f"<div style='font-size:12px;color:#34495e;background:#f8f9fa;"
                     f"padding:8px 10px;border-radius:4px;margin-bottom:8px;'>"
                     f"⏱ {ta.timing_note}</div>\n")

        # Strategies
        if ta.strategies:
            html += "<ul style='margin:6px 0 0;padding-left:18px;font-size:12px;color:var(--fg-body);'>"
            for strat in ta.strategies:
                html += f"<li style='margin-bottom:4px;'>{strat}</li>"
            html += "</ul>\n"

        html += "</div>\n"

    return html


def generate_html_report(
    results: list[PositionAnalysis],
    watchlists: Optional[dict[str, list[PositionAnalysis]]] = None,
    screening_results: Optional[dict] = None,
    realized_ytd: Optional[dict] = None,
) -> str:
    # Compute live total portfolio value, then set live_pct_portfolio per position
    live_total = sum(
        r.live_market_value for r in results if r.live_market_value is not None
    )
    for r in results:
        if r.live_market_value is not None and live_total > 0:
            r.live_pct_portfolio = r.live_market_value / live_total * 100

    # Re-run v2 verdict now that live_pct_portfolio is populated. This is the
    # last signal the verdict needs — couldn't be applied earlier because
    # position size requires knowing the total portfolio, which only this
    # function does (analyze_position runs per-stock without portfolio context).
    # Watchlist items aren't re-run (you don't own them, so size doesn't apply).
    for r in results:
        if (r.bucket == "compounder"
                and r.composite_score is not None
                and r.live_pct_portfolio is not None):
            insider_signal = None
            if r.insider_activity:
                sig = r.insider_activity.get("net_signal", "")
                ins_score = r.score_insider
                if sig == "Buying":
                    insider_signal = "supports_buy"
                elif sig == "Selling" and ins_score is not None and ins_score <= 35:
                    insider_signal = "caution"
                else:
                    insider_signal = "no_signal"
            sector_label = (r.sector_momentum or {}).get("label")
            r.verdict = compute_verdict_v2(
                composite_score=r.composite_score,
                filters=r.filters,
                current_price=r.current_price,
                target_price=r.target_mean,
                upside_pct=r.upside_pct,
                trend=r.trend,
                pct_above_ma200=r.pct_above_ma200,
                week52_position=r.week52_position,
                sector_label=sector_label,
                insider_signal=insider_signal,
                position_pct_portfolio=r.live_pct_portfolio,
                is_holding=True,
            )

    statement_total = sum(r.statement_market_value for r in results)
    delta = live_total - statement_total
    delta_pct = (delta / statement_total * 100) if statement_total else 0

    compounders = [r for r in results if r.bucket == "compounder"]
    thematics = [r for r in results if r.bucket == "thematic"]
    compounders.sort(key=lambda r: r.live_market_value or 0, reverse=True)
    thematics.sort(key=lambda r: r.live_market_value or 0, reverse=True)

    action_items = [r for r in results
                    if r.verdict and r.verdict.label in ("SELL", "TRIM")]
    add_items = [r for r in results if r.verdict and r.verdict.label == "ADD"]

    _now_est = datetime.now(ZoneInfo("America/New_York"))
    now = _now_est.strftime("%B %d, %Y · %I:%M %p EST")

    # --- Relative "last updated X ago" ---
    def _relative_time(dt) -> str:
        """Return a human-readable 'X days Y hrs ago' string."""
        total_secs = int((datetime.now(ZoneInfo("America/New_York")) - dt).total_seconds())
        days = total_secs // 86400
        hours = (total_secs % 86400) // 3600
        mins = (total_secs % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h ago"
        if hours > 0:
            return f"{hours}h {mins}m ago"
        return f"{mins}m ago"

    relative_now = _relative_time(_now_est)
    delta_class = "pos-up" if delta >= 0 else "pos-down"
    delta_sign = "+" if delta >= 0 else ""

    # Watchlist counts for summary card
    watchlist_total = 0
    watchlist_buys = 0
    if watchlists:
        seen_tickers: set[str] = set()
        held_tickers = {r.ticker for r in results}
        for items in watchlists.values():
            for r in items:
                if r.ticker in held_tickers or r.ticker in seen_tickers:
                    continue
                seen_tickers.add(r.ticker)
                watchlist_total += 1
                if r.verdict and r.verdict.label == "BUY":
                    watchlist_buys += 1
    watchlist_stat_html = ""
    if watchlist_total:
        watchlist_stat_html = (
            f'<div class="stat"><strong>{watchlist_buys} / {watchlist_total}</strong>'
            f'Watchlist BUY signals</div>'
        )

    has_holdings = bool(results)
    report_title = "Portfolio Analysis" if has_holdings else "Stock Analysis"
    holdings_summary = ""
    if has_holdings:
        holdings_summary = f"""
    <div class="stat"><strong>{_fmt_money(live_total)}</strong>Portfolio value (live)</div>
    <div class="stat"><strong>{len(compounders)}</strong>Compounder positions</div>
    <div class="stat"><strong>{len(thematics)}</strong>Thematic / ETF positions</div>
    <div class="stat"><strong>{len(action_items)}</strong>Sell / Trim flags</div>
    <div class="stat"><strong>{len(add_items)}</strong>Add candidates</div>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  /* ---------- Theme tokens (light by default) ---------- */
  :root {{
    --bg-page: #fafbfc;
    --bg-card: #ffffff;
    --bg-card-hover: #f0f3f7;
    --bg-table-header: #f3f5f7;
    --bg-table-header-hover: #e8ebef;
    --bg-row-even: #fafbfc;
    --bg-row-hover: #eef2f7;
    --bg-pill: #ffffff;
    --bg-pill-hover: #f0f3f7;
    --bg-pill-active: #2c3e50;
    --bg-input: #ffffff;
    --bg-summary: #ffffff;
    --bg-alert: #fffbeb;
    --bg-alert-border: #fde68a;
    --bg-chip-neutral: #ecf0f1;
    --bg-chip-green: #d4edda;
    --bg-chip-red: #f8d7da;
    --bg-chip-amber: #fff3cd;
    --bg-chip-blue: #e7f1ff;
    --bg-chip-yellow: #fff8e1;

    --fg-strong: #1a2533;
    --fg-body: #2c3e50;
    --fg-muted: #7f8c8d;
    --fg-faint: #95a5a6;
    --fg-table-header: #34495e;
    --fg-pill: #34495e;
    --fg-pill-active: #ffffff;
    --fg-alert: #7d5d00;
    --fg-chip-green: #1e7e34;
    --fg-chip-red: #a02622;
    --fg-chip-amber: #7d6608;
    --fg-chip-blue: #1c4d8c;
    --fg-chip-neutral: #7f8c8d;

    --border-soft: #f1f3f5;
    --border-medium: #e1e4e8;
    --border-strong: #d0d7de;

    --pos-up: #1e7e34;
    --pos-down: #a02622;
    --shadow-card: 0 1px 3px rgba(15, 23, 42, 0.04);
    --shadow-sticky: 0 2px 6px rgba(15, 23, 42, 0.06);
  }}

  /* ---------- Dark theme overrides ---------- */
  [data-theme="dark"] {{
    --bg-page: #0f1419;
    --bg-card: #1a2028;
    --bg-card-hover: #232a35;
    --bg-table-header: #232a35;
    --bg-table-header-hover: #2d3540;
    --bg-row-even: #161c24;
    --bg-row-hover: #232a35;
    --bg-pill: #1a2028;
    --bg-pill-hover: #2d3540;
    --bg-pill-active: #4a90e2;
    --bg-input: #1a2028;
    --bg-summary: #1a2028;
    --bg-alert: #2d2517;
    --bg-alert-border: #6b5a20;
    /* Chips in dark mode — muted backgrounds, brighter text */
    --bg-chip-neutral: #2d3540;
    --bg-chip-green: #143324;
    --bg-chip-red: #3d1a1a;
    --bg-chip-amber: #3a2d10;
    --bg-chip-blue: #1a2c44;
    --bg-chip-yellow: #3d3010;

    --fg-strong: #e8eaed;
    --fg-body: #cbd5e0;
    --fg-muted: #8b95a3;
    --fg-faint: #6b7280;
    --fg-table-header: #cbd5e0;
    --fg-pill: #cbd5e0;
    --fg-pill-active: #ffffff;
    --fg-alert: #f0c97a;
    --fg-chip-green: #4ade80;
    --fg-chip-red: #f87171;
    --fg-chip-amber: #fbbf24;
    --fg-chip-blue: #60a5fa;
    --fg-chip-neutral: #9ca3af;

    --border-soft: #232a35;
    --border-medium: #2d3540;
    --border-strong: #3a4250;

    --pos-up: #4ade80;
    --pos-down: #f87171;
    --shadow-card: 0 1px 3px rgba(0, 0, 0, 0.4);
    --shadow-sticky: 0 2px 8px rgba(0, 0, 0, 0.5);
  }}

  /* ---------- Foundation ---------- */
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          "Helvetica Neue", Arial, sans-serif;
          color: var(--fg-body); background: var(--bg-page);
          max-width: 1500px; margin: 0 auto;
          padding: 32px 28px 48px;
          line-height: 1.5; font-size: 14px;
          transition: background 0.2s, color 0.2s; }}

  /* ---------- Headers ---------- */
  h1 {{ font-size: 28px; margin: 0 0 6px; font-weight: 600;
        letter-spacing: -0.3px; color: var(--fg-strong); }}
  h2 {{ font-size: 19px; margin: 36px 0 12px; font-weight: 600;
        color: var(--fg-strong);
        background: var(--bg-page);
        padding: 12px 4px 10px;
        border-bottom: 2px solid var(--border-medium);
        position: sticky;
        top: 0;
        z-index: 15;
        /* Subtle drop-shadow under the pinned header so it visually separates
           from the content scrolling underneath. */
        box-shadow: 0 2px 4px var(--bg-page); }}
  h3 {{ font-size: 15px; margin: 24px 0 10px; font-weight: 600;
        color: var(--fg-table-header); }}
  .sub {{ color: var(--fg-muted); font-size: 13px; margin-bottom: 28px; }}

  /* ---------- Summary card ---------- */
  .summary-card {{ background: var(--bg-summary); border: 1px solid var(--border-medium);
                   border-radius: 10px; padding: 20px 24px;
                   margin-bottom: 24px;
                   box-shadow: var(--shadow-card); }}
  .summary-row {{ display: flex; gap: 36px; flex-wrap: wrap; }}
  .stat {{ font-size: 12px; color: var(--fg-muted);
           text-transform: uppercase; letter-spacing: 0.4px;
           font-weight: 600; }}
  .stat strong {{ font-size: 22px; display: block;
                  color: var(--fg-strong); margin-top: 4px;
                  font-weight: 700; letter-spacing: -0.3px;
                  text-transform: none; }}

  /* ---------- Tables ---------- */
  .table-wrap {{ overflow-x: auto; border: 1px solid var(--border-medium);
                 border-radius: 10px; background: var(--bg-card);
                 margin-bottom: 28px;
                 box-shadow: var(--shadow-card); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  thead th {{ background: var(--bg-table-header); color: var(--fg-table-header);
              padding: 10px 8px; text-align: left;
              font-weight: 600; font-size: 11px;
              border-bottom: 2px solid var(--border-medium);
              cursor: pointer; user-select: none;
              text-transform: uppercase; letter-spacing: 0.3px; }}
  thead th:hover {{ background: var(--bg-table-header-hover); color: var(--fg-strong); }}
  thead th.sort-asc::after {{ content: " ▲"; font-size: 9px; opacity: 0.7; }}
  thead th.sort-desc::after {{ content: " ▼"; font-size: 9px; opacity: 0.7; }}
  td {{ padding: 9px 8px; border-bottom: 1px solid var(--border-soft);
        vertical-align: middle; color: var(--fg-body); }}
  tbody tr:nth-child(even) td {{ background: var(--bg-row-even); }}
  tbody tr:hover td {{ background: var(--bg-row-hover); }}
  tbody tr:last-child td {{ border-bottom: none; }}

  /* ---------- Cell styles ---------- */
  .verdict {{ padding: 4px 10px; border-radius: 12px; color: white;
              font-weight: 600; font-size: 11px; letter-spacing: 0.4px;
              display: inline-block; }}
  .ticker {{ font-weight: 700; font-family: "SF Mono", SFMono-Regular,
             Consolas, "Liberation Mono", monospace;
             color: var(--fg-strong); letter-spacing: -0.2px; }}
  .reason {{ color: var(--fg-muted); font-size: 11px;
             line-height: 1.4; margin-top: 2px; }}
  .num {{ text-align: right;
          font-variant-numeric: tabular-nums; }}
  .pos-up {{ color: var(--pos-up); font-weight: 600; }}
  .pos-down {{ color: var(--pos-down); font-weight: 600; }}

  /* ---------- Alerts ---------- */
  .alert {{ background: var(--bg-alert); border: 1px solid var(--bg-alert-border);
            border-left: 4px solid #f39c12;
            padding: 12px 16px; margin-bottom: 18px;
            font-size: 13px; border-radius: 6px;
            color: var(--fg-alert); }}

  /* ---------- Filter bar ----------
     Note: filter bar is intentionally NOT sticky. We tried scroll-direction
     toggling (sticky-on-scroll-up) but the attribute-conditional sticky rules
     don't reliably work in Safari. The simpler, working behavior: h2 section
     headers are always sticky (so you know which section you're reading);
     filters live at the top of the page and you scroll back up to use them. */
  .filter-bar {{ background: var(--bg-card); border: 1px solid var(--border-medium);
                 border-radius: 10px;
                 padding: 10px 14px; margin-bottom: 18px;
                 box-shadow: var(--shadow-card); }}
  .filter-bar-top {{ display: flex; align-items: center; gap: 8px;
                     flex-wrap: wrap; }}
  .filter-bar input[type="text"] {{ padding: 6px 10px;
                                    border: 1px solid var(--border-strong);
                                    border-radius: 6px; font-size: 13px;
                                    min-width: 240px; outline: none;
                                    background: var(--bg-input);
                                    color: var(--fg-body);
                                    transition: border-color 0.15s; }}
  .filter-bar input[type="text"]:focus {{ border-color: var(--fg-table-header);
                                          box-shadow: 0 0 0 3px rgba(74, 144, 226, 0.15); }}
  .filter-pill {{ background: var(--bg-pill); border: 1px solid var(--border-strong);
                  border-radius: 14px; padding: 3px 11px;
                  font-size: 12px; cursor: pointer; color: var(--fg-pill);
                  transition: all 0.15s; user-select: none;
                  font-weight: 500; white-space: nowrap; }}
  .filter-pill:hover {{ background: var(--bg-pill-hover); border-color: var(--fg-faint); }}
  .filter-pill.active {{ background: var(--bg-pill-active); color: var(--fg-pill-active);
                         border-color: var(--bg-pill-active);
                         box-shadow: 0 1px 3px rgba(15, 23, 42, 0.15); }}
  .clear-pill {{ color: var(--fg-faint); font-size: 11px;
                 background: var(--bg-page); }}
  .clear-pill:hover {{ background: var(--bg-chip-red); color: var(--fg-chip-red);
                       border-color: var(--fg-chip-red); }}
  .more-toggle {{ background: var(--bg-table-header); color: var(--fg-pill);
                  border: 1px solid var(--border-strong);
                  border-radius: 14px; padding: 3px 11px;
                  font-size: 12px; cursor: pointer;
                  font-weight: 500; user-select: none;
                  transition: all 0.15s; }}
  .more-toggle:hover {{ background: var(--bg-table-header-hover); }}
  .more-toggle.expanded {{ background: var(--bg-pill-active); color: var(--fg-pill-active);
                           border-color: var(--bg-pill-active); }}
  .filter-more {{ display: none; margin-top: 10px;
                  padding-top: 10px;
                  border-top: 1px solid var(--border-soft); }}
  .filter-more.show {{ display: block; }}
  .filter-group {{ display: flex; align-items: center;
                   gap: 6px; flex-wrap: wrap;
                   margin-bottom: 6px; }}
  .filter-group:last-child {{ margin-bottom: 0; }}
  .filter-group-label {{ font-size: 10px; color: var(--fg-faint);
                         font-weight: 700;
                         text-transform: uppercase; letter-spacing: 0.6px;
                         min-width: 110px; }}
  .filter-status {{ font-size: 12px; color: var(--fg-muted);
                    margin-left: auto;
                    font-variant-numeric: tabular-nums;
                    white-space: nowrap; }}

  /* ---------- Theme toggle button (floating in top-right) ---------- */
  .theme-toggle {{ position: fixed; top: 20px; right: 20px;
                   width: 38px; height: 38px;
                   border-radius: 50%; border: 1px solid var(--border-medium);
                   background: var(--bg-card); color: var(--fg-body);
                   cursor: pointer; font-size: 18px;
                   display: flex; align-items: center; justify-content: center;
                   box-shadow: var(--shadow-card);
                   z-index: 100;
                   transition: transform 0.15s, background 0.2s; }}
  .theme-toggle:hover {{ transform: scale(1.08); background: var(--bg-card-hover); }}

  /* ---------- Inline-chip overrides (dark mode) ---------- */
  /* Cell renderers use inline styles with hardcoded chip colors. We override
     them in dark mode using attribute selectors so they remain readable. */
  [data-theme="dark"] span[style*="background:#d4edda"],
  [data-theme="dark"] span[style*="background: #d4edda"] {{
    background: var(--bg-chip-green) !important; color: var(--fg-chip-green) !important;
  }}
  [data-theme="dark"] span[style*="background:#f8d7da"],
  [data-theme="dark"] span[style*="background: #f8d7da"] {{
    background: var(--bg-chip-red) !important; color: var(--fg-chip-red) !important;
  }}
  [data-theme="dark"] span[style*="background:#fff3cd"],
  [data-theme="dark"] span[style*="background: #fff3cd"] {{
    background: var(--bg-chip-amber) !important; color: var(--fg-chip-amber) !important;
  }}
  [data-theme="dark"] span[style*="background:#fff8e1"],
  [data-theme="dark"] span[style*="background: #fff8e1"] {{
    background: var(--bg-chip-yellow) !important; color: var(--fg-chip-amber) !important;
  }}
  [data-theme="dark"] span[style*="background:#ecf0f1"],
  [data-theme="dark"] span[style*="background: #ecf0f1"] {{
    background: var(--bg-chip-neutral) !important; color: var(--fg-chip-neutral) !important;
  }}
  [data-theme="dark"] span[style*="background:#e7f1ff"],
  [data-theme="dark"] span[style*="background: #e7f1ff"] {{
    background: var(--bg-chip-blue) !important; color: var(--fg-chip-blue) !important;
  }}
  /* Fade out muted-grey ticker links so they don't disappear into the dark bg */
  [data-theme="dark"] div[style*="color:#7f8c8d"],
  [data-theme="dark"] div[style*="color: #7f8c8d"] {{
    color: var(--fg-muted) !important;
  }}
  [data-theme="dark"] div[style*="color:#bdc3c7"],
  [data-theme="dark"] div[style*="color: #bdc3c7"] {{
    color: var(--fg-faint) !important;
  }}
  /* Filter dots — make the inactive ones visible in dark */
  [data-theme="dark"] span[style*="background:#bdc3c7"] {{
    background: #4a5568 !important;
  }}
  /* Range-bar background */
  [data-theme="dark"] div[style*="background:#ecf0f1"] {{
    background: var(--bg-chip-neutral) !important;
  }}

  /* ---------- Responsive ---------- */
  @media (max-width: 900px) {{
    body {{ padding: 18px 12px 32px; font-size: 13px; }}
    h1 {{ font-size: 22px; }}
    h2 {{ font-size: 17px; }}
    .filter-bar input[type="text"] {{ min-width: 160px; }}
    .filter-group-label {{ min-width: auto; }}
    table {{ font-size: 12px; }}
    thead th, td {{ padding: 8px 6px; }}
    .theme-toggle {{ top: 12px; right: 12px;
                     width: 34px; height: 34px; font-size: 16px; }}
  }}
</style>
</head>
<body>
<script>
  // Apply saved theme BEFORE first paint to avoid a white flash on dark-mode loads.
  (function() {{
    try {{
      var saved = localStorage.getItem('portfolio-theme');
      if (saved === 'dark' || saved === 'light') {{
        document.documentElement.setAttribute('data-theme', saved);
      }} else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {{
        document.documentElement.setAttribute('data-theme', 'dark');
      }}
    }} catch (e) {{}}
  }})();
</script>
<button class="theme-toggle" id="themeToggle"
        title="Toggle light/dark theme" aria-label="Toggle theme">🌙</button>

<h1>{report_title}</h1>
<div class="sub">Last updated {relative_now} · {now}{' · Finnhub enabled' if FINNHUB_API_KEY else ' · yfinance only'}</div>

<div class="summary-card">
  <div class="summary-row">{holdings_summary}
    {watchlist_stat_html}
  </div>
</div>

<div class="filter-bar">
  <div class="filter-bar-top">
    <input type="text" id="searchInput" placeholder="🔍 Search ticker or name…" autocomplete="off">
    <button class="filter-pill" data-filter="buy">Buy signals</button>
    <button class="filter-pill" data-filter="action">Action (SELL/TRIM)</button>
    <button class="filter-pill" data-filter="high-quality">Quality 7+</button>
    <button class="filter-pill" data-filter="hot-sector">🔥 Hot</button>
    <button class="filter-pill" data-filter="insider-buy">✓ Insider buying</button>
    <button class="more-toggle" id="moreToggle">More filters ▾</button>
    <button class="filter-pill clear-pill" id="clearFilters">✕ Clear</button>
    <span class="filter-status" id="filterStatus"></span>
  </div>
  <div class="filter-more" id="filterMore">
    <div class="filter-group">
      <span class="filter-group-label">Verdict</span>
      <button class="filter-pill" data-filter="verdict-add">ADD only</button>
      <button class="filter-pill" data-filter="verdict-hold">HOLD only</button>
      <button class="filter-pill" data-filter="verdict-trim">TRIM only</button>
      <button class="filter-pill" data-filter="verdict-sell">SELL only</button>
      <button class="filter-pill" data-filter="verdict-buy">BUY (watchlist) only</button>
      <button class="filter-pill" data-filter="verdict-watch">WATCH only</button>
      <button class="filter-pill" data-filter="verdict-score-high">Verdict score 75+</button>
      <button class="filter-pill" data-filter="verdict-score-low">Verdict score &lt;40</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">Quality</span>
      <button class="filter-pill" data-filter="high-score">Composite 70+</button>
      <button class="filter-pill" data-filter="mid-score">Composite 50–70</button>
      <button class="filter-pill" data-filter="weak-score">Composite &lt;40</button>
      <button class="filter-pill" data-filter="passes-9">Passes 9/9 filters</button>
      <button class="filter-pill" data-filter="passes-8">Passes 8+ filters</button>
      <button class="filter-pill" data-filter="quality-6">Quality 6 (borderline)</button>
      <button class="filter-pill" data-filter="low-quality">Quality &lt;5</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">Sector momentum</span>
      <button class="filter-pill" data-filter="cool-sector">❄️ Cool sector</button>
      <button class="filter-pill" data-filter="neutral-sector">Neutral sector</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">By sector</span>
      <button class="filter-pill" data-filter="sector-technology">Technology</button>
      <button class="filter-pill" data-filter="sector-healthcare">Healthcare</button>
      <button class="filter-pill" data-filter="sector-financial">Financials</button>
      <button class="filter-pill" data-filter="sector-comm">Communication</button>
      <button class="filter-pill" data-filter="sector-consumer-cyclical">Consumer Cyclical</button>
      <button class="filter-pill" data-filter="sector-consumer-defensive">Consumer Defensive</button>
      <button class="filter-pill" data-filter="sector-energy">Energy</button>
      <button class="filter-pill" data-filter="sector-industrials">Industrials</button>
      <button class="filter-pill" data-filter="sector-utilities">Utilities</button>
      <button class="filter-pill" data-filter="sector-real-estate">Real Estate</button>
      <button class="filter-pill" data-filter="sector-basic-materials">Basic Materials</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">Trend</span>
      <button class="filter-pill" data-filter="uptrend">↑ Uptrend</button>
      <button class="filter-pill" data-filter="sideways">→ Sideways</button>
      <button class="filter-pill" data-filter="downtrend">↓ Downtrend</button>
      <button class="filter-pill" data-filter="far-above-ma">Far above 200d MA (+25%)</button>
      <button class="filter-pill" data-filter="far-below-ma">Far below 200d MA (-15%)</button>
      <button class="filter-pill" data-filter="near-200d-ma">Near 200d MA (±5%)</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">Price action</span>
      <button class="filter-pill" data-filter="winners">Winners</button>
      <button class="filter-pill" data-filter="big-winners">Big winners (+25%)</button>
      <button class="filter-pill" data-filter="huge-winners">Huge winners (+100%)</button>
      <button class="filter-pill" data-filter="losers">Losers</button>
      <button class="filter-pill" data-filter="beaten-down">Beaten down (-15%)</button>
      <button class="filter-pill" data-filter="deep-losers">Deep losers (-30%)</button>
      <button class="filter-pill" data-filter="near-low">Near 52w low</button>
      <button class="filter-pill" data-filter="mid-range">Mid-range (40–70%)</button>
      <button class="filter-pill" data-filter="near-high">Near 52w high</button>
      <button class="filter-pill" data-filter="big-upside">Upside &gt;20%</button>
      <button class="filter-pill" data-filter="massive-upside">Upside &gt;40%</button>
      <button class="filter-pill" data-filter="overvalued">Above target</button>
      <button class="filter-pill" data-filter="very-overvalued">Above target by 15%+</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">Analyst rating</span>
      <button class="filter-pill" data-filter="analyst-strong-buy">Strong Buy</button>
      <button class="filter-pill" data-filter="analyst-buy">Buy rated</button>
      <button class="filter-pill" data-filter="analyst-hold">Hold rated</button>
      <button class="filter-pill" data-filter="analyst-sell">Sell rated</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">Insider activity</span>
      <button class="filter-pill" data-filter="insider-caution">⚠ Insider caution</button>
      <button class="filter-pill" data-filter="insider-no-signal">No signal</button>
      <button class="filter-pill" data-filter="has-insider-data">Any insider data</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">Position size</span>
      <button class="filter-pill" data-filter="very-large-position">Very large (&gt;20%)</button>
      <button class="filter-pill" data-filter="large-position">Large (&gt;10%)</button>
      <button class="filter-pill" data-filter="mid-position">Mid (2–10%)</button>
      <button class="filter-pill" data-filter="small-position">Small (&lt;2%)</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">Holding period</span>
      <button class="filter-pill" data-filter="long-term">Long-term (&gt;1yr)</button>
      <button class="filter-pill" data-filter="short-term">Short-term (≤1yr)</button>
      <button class="filter-pill" data-filter="approaching-lt">Approaching LT (within 90d)</button>
      <button class="filter-pill" data-filter="recent-buy">Recent (&lt;30d)</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">Tax</span>
      <button class="filter-pill" data-filter="has-tax-flag">Has tax detail</button>
      <button class="filter-pill" data-filter="tax-loss-candidate">Loss-harvest candidate</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">Type</span>
      <button class="filter-pill" data-filter="compounder-only">Compounders</button>
      <button class="filter-pill" data-filter="thematic-only">Thematic/ETFs</button>
    </div>
  </div>
</div>
"""

    if action_items:
        html += '<div class="alert"><strong>Action items:</strong> '
        html += ", ".join(
            f"{r.ticker} ({r.verdict.label})" for r in action_items
        )
        html += "</div>\n"

    # Compounder section (only if there are compounder holdings)
    if compounders:
        html += "<h2>Quality Compounders</h2>\n"
        html += "<div class='table-wrap'><table>\n<thead><tr>"
        html += (
            "<th>Ticker</th>"
            "<th>Name / Sector</th>"
            "<th class='num'>Position</th>"
            "<th class='num'>Cost / Gain</th>"
            "<th class='num'>Price → Target</th>"
            "<th>Range / Trend</th>"
            "<th>Quality (9)</th>"
            "<th class='num' title='Composite of Quality 30% + Growth 20% + Value 20% + Analyst 15% + Insider 15%. Hover any cell for sub-score breakdown.' style='cursor:help;'>Composite <span style='color:var(--fg-faint);font-weight:400;font-size:10px;text-transform:none;letter-spacing:0;'>&#9432;</span></th>"
            "<th>Analyst Ratings</th>"
            "<th title='Decision verdict from insider activity. &#10003; Supports buy = real open-market buying with personal cash (rare, strong positive). &mdash; No signal = typical compensation, 10b5-1 plans, or tax-withholds (most mega-caps; ignore). &#9888; Caution = discretionary selling large enough relative to market cap to warrant a closer look before buying.' style='cursor:help;'>Insider 90d <span style='color:#bdc3c7;font-size:10px;'>&#9432;</span></th>"
            "<th>Verdict <span style='color:var(--fg-faint);font-weight:400;font-size:10px;text-transform:none;letter-spacing:0;'>(score)</span></th>"
            "</tr></thead><tbody>\n"
        )
        for r in compounders:
            passed = sum(1 for f in r.filters if f.passed)
            verdict_label = r.verdict.label if r.verdict else "—"
            rating_score = -1
            if r.rating_breakdown and r.rating_breakdown.get("total"):
                t = r.rating_breakdown["total"]
                rating_score = (
                    (r.rating_breakdown.get("buy", 0)
                     - r.rating_breakdown.get("sell", 0)) / t
                )
            rating_html = _rating_bar(r.rating_breakdown, r.recommendation, r.num_analysts)
            verdict_html = _verdict_cell(r.verdict)
            html += _tr_open(r)
            html += _td(_ticker_cell(r), r.ticker, "ticker")
            html += _td(_name_sector_cell(r), r.name)
            html += _td(_position_cell(r), r.live_market_value or -1, "num")
            html += _td(_cost_gain_cell(r),
                        r.unrealized_gain if r.unrealized_gain is not None else -1e12,
                        "num")
            html += _td(_price_target_cell(r),
                        r.upside_pct if r.upside_pct is not None else -1e6,
                        "num")
            html += _td(_range_trend_cell(r),
                        r.week52_position if r.week52_position is not None else -1)
            html += _td(
                f"{_filter_dots(r.filters)} <span style='color:var(--fg-muted);font-size:11px'>{passed}/9</span>",
                passed,
            )
            html += _td(_score_cell(r.composite_score, r.score_quality, r.score_growth,
                                    r.score_value, r.score_analyst, r.score_insider),
                        r.composite_score if r.composite_score is not None else -1, "num")
            html += _td(rating_html, rating_score)
            html += _td(_insider_cell(r.insider_activity),
                        r.score_insider if r.score_insider is not None else -1)
            html += _td(verdict_html, (r.verdict.score if r.verdict and r.verdict.score is not None else (100 - _VERDICT_ORDER.get(verdict_label, 99))))
            html += "</tr>\n"
        html += "</tbody></table></div>\n"


    # ---------- Watchlist sections ----------
    if watchlists:
        held_tickers = {r.ticker for r in results}
        wl_title = "Watchlists" if has_holdings else "Stock Analysis"
        wl_subtitle = (
            "Stocks you're tracking but don't own. Verdicts answer "
            "<em>“should I buy?”</em> rather than <em>“should I sell?”</em>."
            if has_holdings else
            "Verdicts answer <em>“should I buy?”</em> based on the 9-filter "
            "quality framework and analyst targets."
        )
        html += f"<h2 style='margin-top:{'48px' if has_holdings else '24px'};'>{wl_title}</h2>\n"
        html += (
            f'<p style="color:#7f8c8d;font-size:12px;margin-top:-6px;margin-bottom:18px;">'
            f"{wl_subtitle}"
            "</p>\n"
        )
        for wl_name, items in watchlists.items():
            # Filter out anything already in holdings (avoids duplicate rows)
            items = [r for r in items if r.ticker not in held_tickers]
            if not items:
                continue
            # Sort by best opportunity first (BUY < WATCH < WAIT < PASS)
            items.sort(key=lambda r: (
                _VERDICT_ORDER.get(r.verdict.label if r.verdict else "ERROR", 99),
                -(r.upside_pct or -1e6),
            ))
            html += f"<h3 style='margin-top:24px;color:#34495e;'>📋 {wl_name} ({len(items)})</h3>\n"
            html += "<div class='table-wrap'><table>\n<thead><tr>"
            html += (
                "<th>Ticker</th>"
                "<th>Name / Sector</th>"
                "<th class='num'>Position</th>"
                "<th class='num'>Cost / Gain</th>"
                "<th class='num'>Price → Target</th>"
                "<th>Range / Trend</th>"
                "<th>Quality (9)</th>"
                "<th class='num' title='Composite of Quality 30% + Growth 20% + Value 20% + Analyst 15% + Insider 15%. Hover any cell for sub-score breakdown.' style='cursor:help;'>Composite <span style='color:var(--fg-faint);font-weight:400;font-size:10px;text-transform:none;letter-spacing:0;'>&#9432;</span></th>"
                "<th>Analyst Ratings</th>"
                "<th title='Decision verdict from insider activity. &#10003; Supports buy = real open-market buying with personal cash (rare, strong positive). &mdash; No signal = typical compensation, 10b5-1 plans, or tax-withholds (most mega-caps; ignore). &#9888; Caution = discretionary selling large enough relative to market cap to warrant a closer look before buying.' style='cursor:help;'>Insider 90d <span style='color:#bdc3c7;font-size:10px;'>&#9432;</span></th>"
                "<th>Verdict <span style='color:var(--fg-faint);font-weight:400;font-size:10px;text-transform:none;letter-spacing:0;'>(score)</span></th>"
                "</tr></thead><tbody>\n"
            )
            for r in items:
                passed = sum(1 for f in r.filters if f.passed)
                verdict_label = r.verdict.label if r.verdict else "—"
                rating_score = -1
                if r.rating_breakdown and r.rating_breakdown.get("total"):
                    t = r.rating_breakdown["total"]
                    rating_score = (
                        (r.rating_breakdown.get("buy", 0)
                         - r.rating_breakdown.get("sell", 0)) / t
                    )
                rating_html = _rating_bar(
                    r.rating_breakdown, r.recommendation, r.num_analysts
                )
                verdict_html = _verdict_cell(r.verdict)
                quality_cell = (
                    f"{_filter_dots(r.filters)} "
                    f"<span style='color:var(--fg-muted);font-size:11px'>{passed}/9</span>"
                    if r.filters else "<span style='color:var(--fg-faint);'>n/a</span>"
                )
                html += _tr_open(r)
                html += _td(_ticker_cell(r), r.ticker, "ticker")
                html += _td(_name_sector_cell(r), r.name)
                # Watchlist items have no Position or Cost/Gain (you don't own them)
                na = "<span style='color:var(--fg-faint);'>—</span>"
                html += _td(na, -1, "num")
                html += _td(na, -1e12, "num")
                html += _td(_price_target_cell(r),
                            r.upside_pct if r.upside_pct is not None else -1e6,
                            "num")
                html += _td(_range_trend_cell(r),
                            r.week52_position if r.week52_position is not None else -1)
                html += _td(quality_cell, passed if r.filters else -1)
                html += _td(_score_cell(r.composite_score, r.score_quality, r.score_growth,
                                        r.score_value, r.score_analyst, r.score_insider),
                            r.composite_score if r.composite_score is not None else -1, "num")
                html += _td(rating_html, rating_score)
                html += _td(_insider_cell(r.insider_activity),
                            r.score_insider if r.score_insider is not None else -1)
                html += _td(verdict_html, (r.verdict.score if r.verdict and r.verdict.score is not None else (100 - _VERDICT_ORDER.get(verdict_label, 99))))
                html += "</tr>\n"
            html += "</tbody></table></div>\n"

    # ---------- Screening section (passed-the-screen universe) ----------
    if screening_results:
        html += _render_screening_section(screening_results)

    # ---------- ETFs & Thematic positions (moved before Tax section) ----------
    if thematics:
        html += "<h2>ETFs &amp; Thematic Positions</h2>\n"
        html += "<div class='table-wrap'><table>\n<thead><tr>"
        html += (
            "<th>Ticker</th>"
            "<th>Name</th>"
            "<th class='num'>Position</th>"
            "<th class='num'>Cost / Gain</th>"
            "<th class='num'>Price → Target</th>"
            "<th>Range / Trend</th>"
            "<th class='num' title='Composite of Quality 30% + Growth 20% + Value 20% + Analyst 15% + Insider 15%. Hover any cell for sub-score breakdown.' style='cursor:help;'>Composite <span style='color:var(--fg-faint);font-weight:400;font-size:10px;text-transform:none;letter-spacing:0;'>&#9432;</span></th>"
            "<th>Analyst Ratings</th>"
            "<th title='Decision verdict from insider activity. &#10003; Supports buy = real open-market buying with personal cash (rare, strong positive). &mdash; No signal = typical compensation, 10b5-1 plans, or tax-withholds (most mega-caps; ignore). &#9888; Caution = discretionary selling large enough relative to market cap to warrant a closer look before buying.' style='cursor:help;'>Insider 90d <span style='color:#bdc3c7;font-size:10px;'>&#9432;</span></th>"
            "<th>Verdict <span style='color:var(--fg-faint);font-weight:400;font-size:10px;text-transform:none;letter-spacing:0;'>(score)</span></th>"
            "</tr></thead><tbody>\n"
        )
        for r in thematics:
            verdict_label = r.verdict.label if r.verdict else "—"
            rating_score = -1
            if r.rating_breakdown and r.rating_breakdown.get("total"):
                t = r.rating_breakdown["total"]
                rating_score = (
                    (r.rating_breakdown.get("buy", 0)
                     - r.rating_breakdown.get("sell", 0)) / t
                )
            rating_html = _rating_bar(r.rating_breakdown, r.recommendation, r.num_analysts)
            verdict_html = _verdict_cell(r.verdict)
            html += _tr_open(r)
            html += _td(_ticker_cell(r), r.ticker, "ticker")
            html += _td(r.name, r.name)
            html += _td(_position_cell(r), r.live_market_value or -1, "num")
            html += _td(_cost_gain_cell(r),
                        r.unrealized_gain if r.unrealized_gain is not None else -1e12,
                        "num")
            html += _td(_price_target_cell(r),
                        r.upside_pct if r.upside_pct is not None else -1e6,
                        "num")
            html += _td(_range_trend_cell(r),
                        r.week52_position if r.week52_position is not None else -1)
            html += _td(_score_cell(r.composite_score, r.score_quality, r.score_growth,
                                    r.score_value, r.score_analyst, r.score_insider),
                        r.composite_score if r.composite_score is not None else -1, "num")
            html += _td(rating_html, rating_score)
            html += _td(_insider_cell(r.insider_activity),
                        r.score_insider if r.score_insider is not None else -1)
            html += _td(verdict_html, (r.verdict.score if r.verdict and r.verdict.score is not None else (100 - _VERDICT_ORDER.get(verdict_label, 99))))
            html += "</tr>\n"
        html += "</tbody></table></div>\n"
    # ---------- Tax analysis section (moved to bottom by request) ----------
    flagged_with_tax = [r for r in results
                        if getattr(r, "tax", None) is not None]
    # Render the tax section if there are flagged positions OR YTD data
    # (YTD section is valuable even when no positions are flagged for trim).
    if flagged_with_tax or realized_ytd:
        html += _render_tax_section(flagged_with_tax, results, realized_ytd)

    html += """
<p style="color:#95a5a6;font-size:11px;margin-top:30px;">
Prices live via yfinance. Analyst ratings via Finnhub if configured, else yfinance fallback.
Quality dots: green = pass, red = fail. Hover for actual values.
Click any column header to sort. Click again to reverse.
Verdicts are framework outputs, not investment advice.
</p>
<script>
(function() {
  function sortableValue(td) {
    var s = td.getAttribute('data-sort');
    if (s === null || s === '') return null;
    var n = parseFloat(s);
    return isNaN(n) ? s.toLowerCase() : n;
  }
  document.querySelectorAll('table').forEach(function(table) {
    var headers = table.querySelectorAll('th');
    headers.forEach(function(th, idx) {
      th.addEventListener('click', function() {
        var tbody = table.querySelector('tbody');
        var rows = Array.from(tbody.querySelectorAll('tr'));
        var wasAsc = th.classList.contains('sort-asc');
        // Default to descending on first click (most numeric cols are 'biggest first')
        var asc = wasAsc ? false : false;
        // If header was already descending, flip to ascending
        if (th.classList.contains('sort-desc')) asc = true;
        // Clear all headers' sort state
        headers.forEach(function(h) {
          h.classList.remove('sort-asc'); h.classList.remove('sort-desc');
        });
        th.classList.add(asc ? 'sort-asc' : 'sort-desc');
        rows.sort(function(a, b) {
          var av = sortableValue(a.children[idx]);
          var bv = sortableValue(b.children[idx]);
          // Nulls sort last regardless of direction
          if (av === null && bv === null) return 0;
          if (av === null) return 1;
          if (bv === null) return -1;
          var cmp;
          if (typeof av === 'number' && typeof bv === 'number') {
            cmp = av - bv;
          } else {
            cmp = String(av).localeCompare(String(bv));
          }
          return asc ? cmp : -cmp;
        });
        rows.forEach(function(r) { tbody.appendChild(r); });
      });
    });
  });
})();

/* ---------- Theme toggle ---------- */
(function() {
  var btn = document.getElementById('themeToggle');
  if (!btn) return;
  function currentTheme() {
    return document.documentElement.getAttribute('data-theme') || 'light';
  }
  function setIcon() {
    btn.textContent = currentTheme() === 'dark' ? '☀️' : '🌙';
  }
  setIcon();
  btn.addEventListener('click', function() {
    var next = currentTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('portfolio-theme', next); } catch (e) {}
    setIcon();
  });
})();

/* ---------- Filter bar: multi-select pills + search + more toggle ---------- */
(function() {
  var searchInput = document.getElementById('searchInput');
  var pills = document.querySelectorAll('.filter-pill[data-filter]');
  var clearBtn = document.getElementById('clearFilters');
  var moreToggle = document.getElementById('moreToggle');
  var moreSection = document.getElementById('filterMore');
  var statusEl = document.getElementById('filterStatus');
  if (!searchInput || !pills.length) return;

  var activeFilters = new Set();

  function num(s) {
    if (s === null || s === '') return NaN;
    var n = parseFloat(s);
    return isNaN(n) ? NaN : n;
  }

  function rowMatchesFilter(row, filter) {
    var verdict = row.getAttribute('data-verdict') || '';
    var verdictScore = num(row.getAttribute('data-verdict-score'));
    var quality = num(row.getAttribute('data-quality'));
    var gain = num(row.getAttribute('data-gain'));
    var gainPct = num(row.getAttribute('data-gain-pct'));
    var upside = num(row.getAttribute('data-upside'));
    var pos52 = num(row.getAttribute('data-pos52'));
    var score = num(row.getAttribute('data-score'));
    // Two sector attributes: the raw GICS name (Technology, Healthcare...)
    // and the momentum label (Hot/Cool/Neutral). Both used by different filters.
    var sectorRaw = (row.getAttribute('data-sector') || '').toLowerCase();
    var sectorMom = row.getAttribute('data-sector-mom') || '';
    var insider = row.getAttribute('data-insider') || '';
    var hasInsider = row.getAttribute('data-has-insider') || '';
    var trend = row.getAttribute('data-trend') || '';
    var maPct = num(row.getAttribute('data-ma-pct'));
    var portPct = num(row.getAttribute('data-port-pct'));
    var daysHeld = num(row.getAttribute('data-days-held'));
    var recommendation = row.getAttribute('data-recommendation') || '';
    var hasTax = row.getAttribute('data-has-tax') || '0';
    var bucket = row.getAttribute('data-bucket') || '';

    switch (filter) {
      // ------------- Essentials (top row) -------------
      case 'action':          return verdict === 'SELL' || verdict === 'TRIM';
      case 'buy':             return verdict === 'BUY'  || verdict === 'ADD';
      case 'high-quality':    return !isNaN(quality) && quality >= 7;
      case 'hot-sector':      return sectorMom === 'Hot';
      case 'insider-buy':     return insider === 'supports_buy';

      // ------------- Verdict-specific -------------
      case 'verdict-add':     return verdict === 'ADD';
      case 'verdict-hold':    return verdict === 'HOLD';
      case 'verdict-trim':    return verdict === 'TRIM';
      case 'verdict-sell':    return verdict === 'SELL';
      case 'verdict-buy':     return verdict === 'BUY';
      case 'verdict-watch':   return verdict === 'WATCH';
      case 'verdict-score-high': return !isNaN(verdictScore) && verdictScore >= 75;
      case 'verdict-score-low':  return !isNaN(verdictScore) && verdictScore < 40;

      // ------------- Quality / Composite -------------
      case 'high-score':      return !isNaN(score) && score >= 70;
      case 'mid-score':       return !isNaN(score) && score >= 50 && score < 70;
      case 'weak-score':      return !isNaN(score) && score < 40;
      case 'passes-9':        return !isNaN(quality) && quality === 9;
      case 'passes-8':        return !isNaN(quality) && quality >= 8;
      case 'quality-6':       return !isNaN(quality) && quality === 6;
      case 'low-quality':     return !isNaN(quality) && quality < 5;

      // ------------- Sector momentum -------------
      case 'cool-sector':     return sectorMom === 'Cool';
      case 'neutral-sector':  return sectorMom === 'Neutral';

      // ------------- By sector (case-insensitive substring match) -------------
      case 'sector-technology':         return sectorRaw.indexOf('technolog') !== -1;
      case 'sector-healthcare':         return sectorRaw.indexOf('healthcare') !== -1;
      case 'sector-financial':          return sectorRaw.indexOf('financ') !== -1;
      case 'sector-comm':               return sectorRaw.indexOf('communication') !== -1;
      case 'sector-consumer-cyclical':  return sectorRaw.indexOf('consumer cyclical') !== -1
                                            || sectorRaw.indexOf('discretionary') !== -1;
      case 'sector-consumer-defensive': return sectorRaw.indexOf('consumer defensive') !== -1
                                            || sectorRaw.indexOf('staples') !== -1;
      case 'sector-energy':             return sectorRaw.indexOf('energy') !== -1;
      case 'sector-industrials':        return sectorRaw.indexOf('industrial') !== -1;
      case 'sector-utilities':          return sectorRaw.indexOf('utilit') !== -1;
      case 'sector-real-estate':        return sectorRaw.indexOf('real estate') !== -1;
      case 'sector-basic-materials':    return sectorRaw.indexOf('basic material') !== -1
                                            || sectorRaw.indexOf('materials') !== -1;

      // ------------- Trend -------------
      case 'uptrend':         return trend === 'uptrend';
      case 'sideways':        return trend === 'sideways';
      case 'downtrend':       return trend === 'downtrend';
      case 'far-above-ma':    return !isNaN(maPct) && maPct >= 25;
      case 'far-below-ma':    return !isNaN(maPct) && maPct <= -15;
      case 'near-200d-ma':    return !isNaN(maPct) && Math.abs(maPct) <= 5;

      // ------------- Price action -------------
      case 'winners':
        if (!isNaN(gain))   return gain > 0;
        if (!isNaN(upside)) return upside > 0;
        return false;
      case 'big-winners':     return !isNaN(gainPct) && gainPct >= 25;
      case 'huge-winners':    return !isNaN(gainPct) && gainPct >= 100;
      case 'losers':
        if (!isNaN(gain))   return gain < 0;
        if (!isNaN(upside)) return upside < 0;
        return false;
      case 'beaten-down':     return !isNaN(gainPct) && gainPct <= -15;
      case 'deep-losers':     return !isNaN(gainPct) && gainPct <= -30;
      case 'near-low':        return !isNaN(pos52) && pos52 <= 25;
      case 'mid-range':       return !isNaN(pos52) && pos52 >= 40 && pos52 <= 70;
      case 'near-high':       return !isNaN(pos52) && pos52 >= 90;
      case 'big-upside':      return !isNaN(upside) && upside >= 20;
      case 'massive-upside':  return !isNaN(upside) && upside >= 40;
      case 'overvalued':      return !isNaN(upside) && upside < 0;
      case 'very-overvalued': return !isNaN(upside) && upside <= -15;

      // ------------- Analyst rating -------------
      case 'analyst-strong-buy': return recommendation === 'strong_buy';
      case 'analyst-buy':        return recommendation === 'buy';
      case 'analyst-hold':       return recommendation === 'hold';
      case 'analyst-sell':       return recommendation === 'sell' || recommendation === 'strong_sell';

      // ------------- Insider -------------
      case 'insider-caution':   return insider === 'caution';
      case 'insider-no-signal': return insider === 'no_signal';
      case 'has-insider-data':  return hasInsider === '1';

      // ------------- Position size -------------
      case 'very-large-position': return !isNaN(portPct) && portPct >= 20;
      case 'large-position':      return !isNaN(portPct) && portPct >= 10;
      case 'mid-position':        return !isNaN(portPct) && portPct >= 2 && portPct < 10;
      case 'small-position':      return !isNaN(portPct) && portPct > 0 && portPct < 2;

      // ------------- Holding period -------------
      case 'long-term':       return !isNaN(daysHeld) && daysHeld > 365;
      case 'short-term':      return !isNaN(daysHeld) && daysHeld <= 365;
      case 'approaching-lt':  return !isNaN(daysHeld) && daysHeld >= 275 && daysHeld <= 365;
      case 'recent-buy':      return !isNaN(daysHeld) && daysHeld < 30;

      // ------------- Tax -------------
      case 'has-tax-flag':       return hasTax === '1';
      case 'tax-loss-candidate': return !isNaN(gainPct) && gainPct <= -5;

      // ------------- Type -------------
      case 'compounder-only': return bucket === 'compounder';
      case 'thematic-only':   return bucket === 'thematic' || bucket === 'etf';

      default: return true;
    }
  }

  function applyFilters() {
    var searchTerm = searchInput.value.trim().toLowerCase();
    var visible = 0, total = 0;

    document.querySelectorAll('tbody tr').forEach(function(row) {
      total++;
      var searchData = row.getAttribute('data-search') || '';
      var matches = !searchTerm || searchData.indexOf(searchTerm) !== -1;
      if (matches && activeFilters.size > 0) {
        for (var f of activeFilters) {
          if (!rowMatchesFilter(row, f)) { matches = false; break; }
        }
      }
      row.style.display = matches ? '' : 'none';
      if (matches) visible++;
    });

    // Hide empty tables (and their .table-wrap + preceding h3 sub-heading)
    document.querySelectorAll('.table-wrap').forEach(function(wrap) {
      var anyVisible = false;
      wrap.querySelectorAll('tbody tr').forEach(function(r) {
        if (r.style.display !== 'none') anyVisible = true;
      });
      wrap.style.display = anyVisible ? '' : 'none';
      var prev = wrap.previousElementSibling;
      while (prev && prev.tagName !== 'H2' && prev.tagName !== 'H3') {
        prev = prev.previousElementSibling;
      }
      if (prev && prev.tagName === 'H3') {
        prev.style.display = anyVisible ? '' : 'none';
      }
    });

    if (statusEl) {
      if (visible === total && !searchTerm && activeFilters.size === 0) {
        statusEl.textContent = 'Showing all ' + total;
      } else {
        var bits = [];
        if (activeFilters.size) bits.push(activeFilters.size + ' filter' + (activeFilters.size > 1 ? 's' : ''));
        if (searchTerm) bits.push('search');
        var suffix = bits.length ? ' (' + bits.join(' + ') + ')' : '';
        statusEl.textContent = 'Showing ' + visible + ' of ' + total + suffix;
      }
    }
    if (clearBtn) {
      var hasAny = activeFilters.size > 0 || !!searchTerm;
      clearBtn.style.opacity = hasAny ? '1' : '0.4';
      clearBtn.style.pointerEvents = hasAny ? 'auto' : 'none';
    }
    // If any "more filter" is active, auto-open the more section so user sees it
    if (moreSection) {
      var anyMoreActive = false;
      moreSection.querySelectorAll('.filter-pill.active').forEach(function() {
        anyMoreActive = true;
      });
      if (anyMoreActive && !moreSection.classList.contains('show')) {
        moreSection.classList.add('show');
        if (moreToggle) {
          moreToggle.classList.add('expanded');
          moreToggle.textContent = 'More filters ▴';
        }
      }
    }
  }

  // Pills toggle on click (multi-select)
  pills.forEach(function(pill) {
    pill.addEventListener('click', function() {
      var f = pill.getAttribute('data-filter');
      if (activeFilters.has(f)) {
        activeFilters.delete(f);
        pill.classList.remove('active');
      } else {
        activeFilters.add(f);
        pill.classList.add('active');
      }
      applyFilters();
    });
  });

  // Clear-all
  if (clearBtn) {
    clearBtn.addEventListener('click', function() {
      activeFilters.clear();
      pills.forEach(function(p) { p.classList.remove('active'); });
      searchInput.value = '';
      applyFilters();
    });
  }

  // More-filters toggle (expand/collapse advanced pills)
  if (moreToggle && moreSection) {
    moreToggle.addEventListener('click', function() {
      var isOpen = moreSection.classList.toggle('show');
      moreToggle.classList.toggle('expanded', isOpen);
      moreToggle.textContent = isOpen ? 'More filters ▴' : 'More filters ▾';
    });
  }

  searchInput.addEventListener('input', applyFilters);
  applyFilters();
})();
</script>
</body></html>
"""
    return html


# ============================================================
# Optional email delivery (matches existing screener SMTP pattern)
# ============================================================

def send_email(html: str, subject: str) -> None:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    sender = os.environ.get("EMAIL_FROM", user)
    recipient = os.environ.get("EMAIL_TO")

    if not all([host, user, pwd, recipient]):
        print("[email] missing SMTP_HOST / SMTP_USER / SMTP_PASS / EMAIL_TO; "
              "skipping send.")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pwd)
        s.sendmail(sender, [recipient], msg.as_string())
    print(f"[email] sent to {recipient}")


# ============================================================
# Main
# ============================================================

def main():
    ap = argparse.ArgumentParser(
        description="Analyze a portfolio against the 9-filter compounder framework."
    )
    ap.add_argument(
        "positions_csv", nargs="?", default=None,
        help="CSV from parse_statement.py (omit when using --source robinhood)",
    )
    ap.add_argument(
        "--source", choices=["csv", "robinhood"], default="csv",
        help="Where to load positions from (default: csv)",
    )
    ap.add_argument("--out", default="portfolio_report.html",
                    help="Output HTML file (default: portfolio_report.html)")
    ap.add_argument("--save-positions", default=None,
                    help="When --source robinhood, also save positions to this CSV "
                         "(useful for auditing / fallback)")
    ap.add_argument("--email", action="store_true",
                    help="Also send via SMTP (uses env vars)")
    ap.add_argument("--include-watchlists", action="store_true",
                    help="When --source robinhood, also analyze Robinhood watchlists "
                         "and add a 'should I buy?' section to the report")
    ap.add_argument("--tickers", default=None,
                    help="Ad-hoc mode: analyze just these tickers (comma-separated, "
                         "e.g. 'AAPL,MSFT,GOOGL'). Skips Robinhood/holdings entirely "
                         "— no auth needed. Useful for quick stock lookups.")
    ap.add_argument("--screen", action="store_true",
                    help="Run S&P 500/400 screening and add the screening section. "
                         "Slow (~15-25 min for full universe).")
    ap.add_argument("--screen-limit", type=int, default=None,
                    help="Cap the screening universe size (e.g. 50 for a fast test).")
    ap.add_argument("--sync-screening-watchlist", action="store_true",
                    help="When --screen is used and --source is robinhood, "
                         "sync the passing tickers to the 'Screening' watchlist "
                         "in Robinhood (read-write).")
    ap.add_argument("--sync-dry-run", action="store_true",
                    help="With --sync-screening-watchlist, preview adds/removes "
                         "without writing.")
    ap.add_argument("--debug-insider", default=None,
                    help="Diagnose insider lookup for one ticker. Prints which "
                         "data sources are reachable and what each returns. "
                         "Example: --debug-insider AAPL")
    ap.add_argument("--add-to-watchlist", default=None,
                    metavar="WATCHLIST_NAME",
                    help="Append tickers to an existing Robinhood watchlist. "
                         "Requires --tickers for the symbol list. Skips tickers "
                         "already present. Use --sync-dry-run to preview. "
                         "Example: --add-to-watchlist 'AI Plays' --tickers NVDA,GOOGL")
    args = ap.parse_args()

    # ---------- Debug insider lookup (standalone) ----------
    if args.debug_insider:
        ticker = args.debug_insider.strip().upper()
        print(f"Debugging insider lookup for {ticker}\n" + "=" * 60)
        from insider_trading import (
            get_insider_activity, _has_real_sec_ua,
            _fetch_yfinance_insider, _fetch_sec_insider, _fetch_finnhub_insider,
        )
        from datetime import date as _date, timedelta as _td
        cutoff = _date.today() - _td(days=90)

        print(f"\nSEC_USER_AGENT set with real email: {_has_real_sec_ua()}")
        print(f"FINNHUB_API_KEY set: {bool(os.environ.get('FINNHUB_API_KEY'))}\n")

        print("[1/3] Trying yfinance...")
        try:
            r1 = _fetch_yfinance_insider(ticker, cutoff, verbose=True)
            print(f"      Result: {r1}\n")
        except Exception as e:
            print(f"      EXCEPTION: {e}\n")

        if _has_real_sec_ua():
            print("[2/3] Trying SEC EDGAR...")
            try:
                r2 = _fetch_sec_insider(ticker, cutoff, verbose=True)
                print(f"      Result: {r2}\n")
            except Exception as e:
                print(f"      EXCEPTION: {e}\n")
        else:
            print("[2/3] SEC skipped — set SEC_USER_AGENT='Your Name email@yours.com'\n")

        if os.environ.get("FINNHUB_API_KEY"):
            print("[3/3] Trying Finnhub...")
            try:
                r3 = _fetch_finnhub_insider(ticker, cutoff, verbose=True)
                print(f"      Result: {r3}\n")
            except Exception as e:
                print(f"      EXCEPTION: {e}\n")
        else:
            print("[3/3] Finnhub skipped — set FINNHUB_API_KEY in .env\n")

        print("=" * 60)
        print("Aggregated (all sources):")
        agg = get_insider_activity(ticker, lookback_days=90, verbose=True)
        print(f"  Final: {agg}")
        return

    # ---------- Add-to-watchlist mode (write-only, no analysis) ----------
    if args.add_to_watchlist:
        if not args.tickers:
            print("ERROR: --add-to-watchlist requires --tickers TICKER1,TICKER2,...",
                  file=sys.stderr)
            sys.exit(1)
        tickers = [t.strip().upper() for t in args.tickers.replace(" ", ",").split(",")
                   if t.strip()]
        if not tickers:
            print("ERROR: --tickers given but no valid tickers parsed.",
                  file=sys.stderr)
            sys.exit(1)
        try:
            import robinhood_source as rhs
        except ImportError:
            print("ERROR: robinhood_source.py not found in path.", file=sys.stderr)
            sys.exit(1)
        rhs.login(verbose=True)
        print(f"\nAdding {len(tickers)} ticker(s) to watchlist "
              f"'{args.add_to_watchlist}': {', '.join(tickers)}")
        if args.sync_dry_run:
            print("(DRY RUN — no changes will be written)")
        result = rhs.add_to_watchlist(
            watchlist_name=args.add_to_watchlist,
            tickers=tickers,
            dry_run=args.sync_dry_run,
            verbose=True,
        )
        # Concise summary at the end
        print("\nResult:")
        if result["watchlist_missing"]:
            print(f"  Watchlist '{args.add_to_watchlist}' was not found. "
                  "Create it in the Robinhood app, then rerun.")
            sys.exit(1)
        if result["already_present"]:
            print(f"  Already present (skipped): {len(result['already_present'])}")
        if args.sync_dry_run:
            print(f"  Would add: {len(result['to_add'])}")
        else:
            print(f"  Successfully added: {len(result['added'])}")
            if result["failed_add"]:
                print(f"  Failed to persist: {len(result['failed_add'])} "
                      f"({', '.join(result['failed_add'])})")
        if result["errors"]:
            print(f"  Errors: {len(result['errors'])}")
            for e in result["errors"]:
                print(f"    - {e}")
        return

    # ---------- Ad-hoc tickers mode (standalone, no Robinhood, no holdings) ----------
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.replace(" ", ",").split(",")
                   if t.strip()]
        if not tickers:
            print("ERROR: --tickers given but no valid tickers parsed.",
                  file=sys.stderr)
            sys.exit(1)
        print(f"Ad-hoc analysis of {len(tickers)} ticker(s): {', '.join(tickers)}")
        adhoc_results: list[PositionAnalysis] = []
        for i, t in enumerate(tickers, 1):
            print(f"  [{i:>2}/{len(tickers)}] {t}", end=" ", flush=True)
            pa = analyze_position(
                {"ticker": t, "name": t, "shares": 0,
                 "market_value": 0, "pct_portfolio": 0},
                use_robinhood_ratings=False,
                is_watchlist=True,
            )
            adhoc_results.append(pa)
            if pa.error:
                print(f"ERROR: {pa.error}")
            else:
                v = pa.verdict.label if pa.verdict else "?"
                print(f"-> {v} ({pa.bucket})")

        # Synthetic watchlist labeled "Stock Lookup"; no holdings passed.
        html = generate_html_report(
            results=[],
            watchlists={"Stock Lookup": adhoc_results},
        )
        out = Path(args.out)
        out.write_text(html)
        print(f"\nReport written to {out.resolve()}")
        if args.email:
            send_email(html, f"Stock Lookup — {datetime.now():%Y-%m-%d}")
        return

    use_rh_ratings = False
    watchlist_lookup: dict[str, list[dict]] = {}
    tax_lots_lookup: dict[str, list[dict]] = {}
    if args.source == "robinhood":
        try:
            import robinhood_source as rhs
        except ImportError:
            print("ERROR: robinhood_source.py not found in path.", file=sys.stderr)
            sys.exit(1)
        rhs.login(verbose=True)
        print("[robinhood] Fetching positions...")
        rows = rhs.fetch_positions()
        print(f"[robinhood] Got {len(rows)} positions.")
        use_rh_ratings = True
        if args.include_watchlists:
            print("[robinhood] Fetching watchlists...")
            watchlist_lookup = rhs.fetch_watchlists()
        # Reconstruct exact tax lots from order history for precise tax analysis
        print("[robinhood] Reconstructing tax lots from order history...")
        tax_lots_lookup = rhs.fetch_tax_lots(verbose=True)
        # Same order history can compute YTD realized gains for the tax section
        realized_ytd = rhs.fetch_realized_ytd(verbose=True)
        if args.save_positions:
            # Mirror CSV format from parse_statement.py
            import csv as _csv
            with open(args.save_positions, "w", newline="") as f:
                fields = ["ticker", "name", "shares", "price", "market_value",
                          "est_dividend", "est_yield", "pct_portfolio",
                          "average_buy_price", "equity_change", "percent_change"]
                w = _csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow(r)
            print(f"[robinhood] Saved positions snapshot to {args.save_positions}")
    else:
        if not args.positions_csv:
            print("ERROR: provide a positions CSV (or use --source robinhood).",
                  file=sys.stderr)
            sys.exit(1)
        with open(args.positions_csv) as f:
            rows = list(csv.DictReader(f))

    print(f"Analyzing {len(rows)} positions...")
    results: list[PositionAnalysis] = []
    for i, row in enumerate(rows, 1):
        print(f"  [{i:>2}/{len(rows)}] {row['ticker']}", end=" ", flush=True)
        pa = analyze_position(row, use_robinhood_ratings=use_rh_ratings)
        results.append(pa)
        if pa.error:
            print(f"ERROR: {pa.error}")
        else:
            v = pa.verdict.label if pa.verdict else "?"
            print(f"-> {v} ({pa.bucket})")

    # Analyze watchlists. Cache results by ticker so a stock in multiple lists
    # only triggers one yfinance/Robinhood call.
    watchlists_analyzed: dict[str, list[PositionAnalysis]] = {}
    if watchlist_lookup:
        held_set = {r.ticker for r in results}
        ticker_cache: dict[str, PositionAnalysis] = {}
        total_to_fetch = sum(
            1 for items in watchlist_lookup.values()
            for it in items if it["ticker"] not in held_set
        )
        print(f"\nAnalyzing {total_to_fetch} unique watchlist tickers...")
        seen = 0
        for wl_name, items in watchlist_lookup.items():
            analyzed_items: list[PositionAnalysis] = []
            for it in items:
                t = it["ticker"]
                if t in held_set:
                    continue  # already covered as a holding
                if t not in ticker_cache:
                    seen += 1
                    print(f"  [{seen:>2}/{total_to_fetch}] {t}", end=" ", flush=True)
                    pa = analyze_position(
                        {
                            "ticker": t, "name": it["name"],
                            "shares": 0, "market_value": 0, "pct_portfolio": 0,
                        },
                        use_robinhood_ratings=use_rh_ratings,
                        is_watchlist=True,
                    )
                    ticker_cache[t] = pa
                    if pa.error:
                        print(f"ERROR: {pa.error}")
                    else:
                        v = pa.verdict.label if pa.verdict else "?"
                        print(f"-> {v} ({pa.bucket})")
                analyzed_items.append(ticker_cache[t])
            if analyzed_items:
                watchlists_analyzed[wl_name] = analyzed_items

    # Tax analysis for SELL/TRIM positions (holding period + trim timing).
    try:
        from tax_analysis import TaxConfig, analyze_tax, analyze_tax_with_lots
        tax_cfg = TaxConfig.from_env()
        flagged = [r for r in results
                   if r.verdict and r.verdict.label in ("SELL", "TRIM")]
        if flagged:
            has_lots = bool(tax_lots_lookup)
            method = "exact lot-level" if has_lots else "position-level estimate"
            status_note = ("personalized" if tax_cfg.is_configured
                           else "representative default rates")
            print(f"\nTax analysis for {len(flagged)} flagged position(s) "
                  f"[{method}, {status_note}]...")
            # Per-position try/except so one bad ticker doesn't kill the rest.
            # Previously a single exception in analyze_tax_with_lots OR
            # analyze_tax would propagate to the outer except, leaving
            # r.tax=None for ALL flagged positions — making them silently
            # disappear from the tax section.
            successes = []
            failures = []
            for r in flagged:
                try:
                    lots = (tax_lots_lookup.get(r.ticker)
                            if tax_lots_lookup else None)
                    if lots and r.current_price:
                        r.tax = analyze_tax_with_lots(
                            ticker=r.ticker,
                            verdict=r.verdict.label,
                            lots=lots,
                            current_price=r.current_price,
                            cfg=tax_cfg,
                        )
                    else:
                        # Fallback: position-level open date. Note: if
                        # position_opened is None (CSV mode without dates),
                        # analyze_tax still returns a TaxAnalysis with the
                        # holding-period fields empty but a tax estimate
                        # using representative rates. That's enough to keep
                        # the position visible in the tax section.
                        r.tax = analyze_tax(
                            ticker=r.ticker,
                            verdict=r.verdict.label,
                            unrealized_gain=r.unrealized_gain,
                            position_opened=r.position_opened,
                            cfg=tax_cfg,
                        )
                    successes.append(r.ticker)
                except Exception as per_e:
                    failures.append((r.ticker, str(per_e)))
                    print(f"[tax] {r.ticker}: skipping ({per_e})")
            print(f"[tax] Tax analysis complete: "
                  f"{len(successes)} succeeded, {len(failures)} failed")
            if failures:
                print(f"[tax] Failed tickers: "
                      f"{', '.join(t for t, _ in failures)}")
    except Exception as e:
        print(f"[tax] Skipped tax analysis: {e}")

    total_value = sum(r.live_market_value or 0 for r in results)

    # ---------- Optional: screen the S&P 500/400 universe ----------
    screening_results = None
    if args.screen:
        try:
            import screener as scr
            print("\n" + "=" * 60)
            print("Running S&P 500/400 screen — this takes ~15-25 minutes")
            print("=" * 60)
            universe = scr.fetch_sp500_sp400(verbose=True)
            if args.screen_limit:
                print(f"[screen] Limiting to first {args.screen_limit} for speed test")
                universe = universe[:args.screen_limit]
            raw_results = scr.run_screen(universe, verbose=True)
            passed, near = scr.split_passers_and_near_misses(raw_results)
            screening_results = {
                "passed": passed,
                "near_miss": near,
                "universe_size": len(universe),
            }
            print(f"\n[screen] Passed: {len(passed)}  Near-miss: {len(near)}")

            # Optional: sync to Robinhood "Screening" watchlist
            if args.sync_screening_watchlist and args.source == "robinhood":
                target = [r.ticker for r in passed]
                if target:
                    import robinhood_source as rhs
                    rhs.sync_watchlist(
                        watchlist_name="Screening",
                        target_tickers=target,
                        dry_run=args.sync_dry_run,
                        verbose=True,
                    )
                else:
                    print("[sync] No tickers passed — skipping sync.")
        except Exception as e:
            print(f"[screen] Error: {e}")

    html = generate_html_report(
        results, watchlists=watchlists_analyzed or None,
        screening_results=screening_results,
        realized_ytd=realized_ytd if 'realized_ytd' in locals() else None,
    )

    out = Path(args.out)
    out.write_text(html)
    print(f"\nLive portfolio value: ${total_value:,.2f}")
    if watchlists_analyzed:
        total_wl = sum(len(v) for v in watchlists_analyzed.values())
        print(f"Watchlist tickers analyzed: {total_wl}")
    print(f"Report written to {out.resolve()}")

    if args.email:
        send_email(html, f"Portfolio Analysis — {datetime.now():%Y-%m-%d}")


if __name__ == "__main__":
    main()