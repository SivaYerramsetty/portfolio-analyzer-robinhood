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
    --base-score {composite,quality,blend}
                           The base score this run acts on (watchlist pruning,
                           tax flags, history): the Composite Score (default),
                           the nine quality filters + analyst + insider, or the
                           average of the two. Overrides the BASE_SCORE_MODE env
                           var (a repo variable in CI, set with the report's
                           "Make default" button). The report always carries
                           all three; its Base switch flips between them.

--- HTML REPORT FEATURES (no flags needed; always on) ---------------------

    • Click any column header to sort; click again to reverse.
    • Filter bar at top: search box + one-click Quick screens + an expandable
      faceted panel. Facets (verdict, sector, analyst, insider, trend, …) OR
      within a group and AND across groups; every numeric metric (upside, gain,
      composite/verdict/quality score, position size, days held, …) has a
      min/max range slider. Active filters show as removable chips with live
      per-option counts. ☆ Save names the current combo and pins it to the bar
      (rename or delete it from the same button); the ✕ on any pill takes it
      off the bar. All of it lives in your browser's localStorage, per browser.
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
import io
import json
import os
import smtplib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
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

# --- News-sentiment (Claude) config -----------------------------------------
# When ANTHROPIC_API_KEY is set, each ticker's recent headlines are scored by
# Claude and the result nudges the verdict (bounded; see _NEWS_* below). Unset
# the key to disable entirely (no calls, no cost). NEWS_MODEL defaults to the
# cheapest model that fits the job — this is a one-sentence judgement over <=6
# headlines, not a reasoning task, so Haiku is 5x cheaper than Opus at
# equivalent quality here ($1/$5 vs $5/$25 per million in/out tokens). Set
# NEWS_MODEL=claude-opus-5 if a sharper read is ever worth the 5x.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
NEWS_MODEL = os.environ.get("NEWS_MODEL", "claude-haiku-4-5").strip() or "claude-haiku-4-5"
# Model for the ON-DEMAND missed-opportunity post-mortem (analyze_missed_
# opportunities_ai, run only via --analyze-misses — never on the twice-daily
# report). Unlike news scoring (per-ticker, high volume → Haiku), this is a
# single reasoning-heavy call over the whole tracked set, so it defaults to a
# capable model. Override with MISSED_OPP_MODEL (e.g. a model your key can access).
MISSED_OPP_MODEL = (os.environ.get("MISSED_OPP_MODEL", "claude-opus-5").strip()
                    or "claude-opus-5")


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
# Market-wide Fear & Greed — the gauge at the top of the report
# ============================================================
# Primary source is CNN's Fear & Greed index (recognizable 0–100 number +
# rating). Its data endpoint is undocumented, so if it fails in CI we fall back
# to a VIX-implied estimate via yfinance, and if THAT fails we serve the last
# cached reading. Result is cached in .cache/ (gitignored; carried across CI
# runs) so warm runs and brief CNN outages don't blank the gauge.
# Set MARKET_METER=0 to disable entirely.

_FEAR_GREED_CACHE_PATH = Path(__file__).resolve().parent / ".cache" / "fear_greed.json"
_FEAR_GREED_TTL_SEC = 30 * 60  # re-fetch at most every 30 min


def _fg_rating(score: float) -> str:
    """CNN's bucket labels for a 0–100 score."""
    if score < 25:
        return "Extreme Fear"
    if score < 45:
        return "Fear"
    if score <= 55:
        return "Neutral"
    if score <= 75:
        return "Greed"
    return "Extreme Greed"


def _fetch_fear_greed_cnn() -> Optional[dict]:
    """CNN Fear & Greed index. Returns a normalized dict or None."""
    if not requests:
        return None
    try:
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers={
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0 Safari/537.36"),
                "Accept": "application/json",
            },
            timeout=8,
        )
        if r.status_code != 200:
            return None
        fg = (r.json() or {}).get("fear_and_greed") or {}
        score = fg.get("score")
        if score is None:
            return None
        score = round(float(score))
        return {
            "score": score,
            "rating": _fg_rating(score),
            "source": "CNN Fear & Greed",
            "previous_close": fg.get("previous_close"),
            "previous_week": fg.get("previous_1_week"),
            "previous_month": fg.get("previous_1_month"),
        }
    except Exception:
        return None


def _fetch_fear_greed_vix() -> Optional[dict]:
    """VIX-implied fear/greed estimate via yfinance (fallback source)."""
    try:
        hist = yf.Ticker("^VIX").history(period="5d")
        if hist is None or hist.empty:
            return None
        vix = float(hist["Close"].dropna().iloc[-1])
        # Inverse-linear map: calm VIX → greed, spiking VIX → fear.
        # VIX 10 → 100, 20 → 70, 30 → 40, 40 → 10 (clamped to 0–100).
        score = round(max(0.0, min(100.0, 100.0 - (vix - 10.0) * 3.0)))
        return {
            "score": score,
            "rating": _fg_rating(score),
            "source": f"VIX-implied (VIX {vix:.1f})",
            "previous_close": None,
            "previous_week": None,
            "previous_month": None,
        }
    except Exception:
        return None


def fetch_market_fear_greed() -> Optional[dict]:
    """
    Market-wide Fear & Greed reading for the top-of-report gauge.

    Order of preference: fresh cache → CNN → VIX estimate → stale cache.
    Returns {"score", "rating", "source", "previous_*"} or None if every
    source (and the cache) is unavailable.
    """
    if os.environ.get("MARKET_METER", "").strip() == "0":
        return None

    # Fresh cache short-circuits any network call.
    cached: Optional[dict] = None
    try:
        if _FEAR_GREED_CACHE_PATH.exists():
            cached = json.loads(_FEAR_GREED_CACHE_PATH.read_text())
            if time.time() - cached.get("fetched_at", 0) < _FEAR_GREED_TTL_SEC:
                return cached
    except Exception:
        cached = None

    data = _fetch_fear_greed_cnn() or _fetch_fear_greed_vix()
    if data:
        data["fetched_at"] = time.time()
        try:
            _FEAR_GREED_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _FEAR_GREED_CACHE_PATH.write_text(json.dumps(data))
        except Exception:
            pass
        return data

    # Every live source failed — serve the last reading we have, however stale.
    return cached


# --- S&P 500 benchmark returns (for the portfolio-vs-market summary stat) ----
_SP500_CACHE_PATH = Path(__file__).resolve().parent / ".cache" / "sp500_returns.json"
_SP500_TTL_SEC = 30 * 60  # re-fetch at most every 30 min


def fetch_benchmark_returns() -> Optional[dict]:
    """S&P 500 (^GSPC) reference returns for the portfolio-vs-market stat:
    {"today_pct", "ytd_pct"}. today = last close vs the prior close; ytd = last
    close vs the first close of the calendar year. Cached in .cache/ (30-min
    TTL, carried across CI runs like the fear/greed cache); a stale cache is
    served if a fetch fails so the stat doesn't blank on a brief yfinance
    hiccup. Returns None only when there is no data at all."""
    if os.environ.get("MARKET_METER", "").strip() == "0":
        return None

    cached: Optional[dict] = None
    try:
        if _SP500_CACHE_PATH.exists():
            cached = json.loads(_SP500_CACHE_PATH.read_text())
            if time.time() - cached.get("fetched_at", 0) < _SP500_TTL_SEC:
                return cached
    except Exception:
        cached = None

    try:
        hist = yf.Ticker("^GSPC").history(period="ytd")
        closes = hist["Close"].dropna() if hist is not None else None
        if closes is None or len(closes) < 2:
            return cached
        first = float(closes.iloc[0])
        prev = float(closes.iloc[-2])
        last = float(closes.iloc[-1])
        data = {
            "today_pct": ((last - prev) / prev * 100) if prev else None,
            "ytd_pct": ((last - first) / first * 100) if first else None,
            # Raw index level (last close). Frozen per ticker at first sight in
            # the recs ledger so a missed opportunity can be scored on excess
            # return (alpha) — its gain vs the S&P over the same window.
            "level": last,
            "fetched_at": time.time(),
        }
        try:
            _SP500_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _SP500_CACHE_PATH.write_text(json.dumps(data))
        except Exception:
            pass
        return data
    except Exception:
        return cached


def _compute_holdings_ytd_return(results) -> Optional[float]:
    """Market-value-weighted YTD price return (%) of the CURRENT holdings — a
    like-for-like counterpart to the S&P 500 YTD figure. Each holding's live
    price is compared to its first close of the calendar year, weighted by live
    market value; holdings whose YTD history is unavailable are dropped and the
    weights renormalize over the rest. This is an approximation of 'how the
    stocks I hold now have done this year', not a true time-weighted account
    return (it ignores intra-year buys/sells). Returns None if no history."""
    holdings = [r for r in results
                if getattr(r, "current_price", None)
                and getattr(r, "live_market_value", None)
                and r.live_market_value > 0 and getattr(r, "ticker", None)]
    if not holdings:
        return None
    tickers = sorted({r.ticker for r in holdings})
    year = datetime.now(ZoneInfo("America/New_York")).year
    try:
        raw = yf.download(tickers, start=f"{year}-01-01",
                          progress=False, auto_adjust=False)
        close = raw["Close"]
    except Exception:
        return None

    start_prices: dict[str, float] = {}
    try:
        if hasattr(close, "columns"):        # multi-ticker DataFrame
            for t in tickers:
                if t in close.columns:
                    s = close[t].dropna()
                    if not s.empty:
                        start_prices[t] = float(s.iloc[0])
        else:                                 # single-ticker Series
            s = close.dropna()
            if not s.empty and len(tickers) == 1:
                start_prices[tickers[0]] = float(s.iloc[0])
    except Exception:
        return None

    acc = 0.0
    total_w = 0.0
    for r in holdings:
        sp = start_prices.get(r.ticker)
        if not sp or sp <= 0:
            continue
        acc += r.live_market_value * ((r.current_price - sp) / sp)
        total_w += r.live_market_value
    if total_w <= 0:
        return None
    return acc / total_w * 100


def _render_benchmark_stat(port_today_pct: Optional[float],
                           port_ytd_pct: Optional[float],
                           bench: Optional[dict]) -> str:
    """Two standalone summary tiles — 'Today · your holdings' and 'YTD · your
    holdings' — matching the single-value layout of the other stats in the row.
    The big figure is YOUR holdings' return (green when it beats the index for
    that horizon, red when it lags); the S&P 500's own return sits on a muted
    sub-line so the two can't be confused. A tile appears only when both sides
    have data; nothing renders if the benchmark is unavailable or no horizon
    can be shown."""
    if not bench:
        return ""
    tip = ("The big figure is your current holdings' price return; the S&P 500 "
           "(^GSPC) return is shown below it for comparison. Today = vs prior "
           "close; YTD = vs the first close of the year. Green = your holdings "
           "beat the index. YTD is value-weighted over holdings with available "
           "history (ignores intra-year trades).")
    cap_style = ("font-size:10px;color:var(--fg-muted);font-weight:400;"
                 "text-transform:none;letter-spacing:0;margin-top:2px;")
    blocks = []
    for label, port_val, spx in (
        ("Today", port_today_pct, bench.get("today_pct")),
        ("YTD", port_ytd_pct, bench.get("ytd_pct")),
    ):
        if port_val is None or spx is None:
            continue
        color = "var(--pos-up)" if port_val >= spx else "var(--pos-down)"
        blocks.append(
            f'<div class="stat" title="{tip}">'
            f'<strong style="color:{color};">{_fmt_pct(port_val, 2, True)}</strong>'
            f'{label} · your holdings'
            f'<div style="{cap_style}">S&amp;P 500: {_fmt_pct(spx, 2, True)}</div>'
            f'</div>'
        )
    return "".join(blocks)


def _zone_color(score: float) -> str:
    """Red (low) → green (high). Shared by every top-of-report gauge, so
    'needle to the right / greener' always reads as the healthier end."""
    return (
        "#e5484d" if score < 25 else
        "#f76b15" if score < 45 else
        "#f5b301" if score <= 55 else
        "#7fbf3f" if score <= 75 else
        "#2fa84f"
    )


def _render_gauge_card(dom_id: str, score: float, rating: str, label: str,
                       sub_html: str = "", source: str = "",
                       tooltip: str = "") -> str:
    """
    One semicircular SVG dial (0–100) rendered as a `.market-meter` card.
    Every top-of-report meter is built through here so they stay identical
    in shape/behavior; only the number, rating and captions differ.
    """
    score = max(0, min(100, int(round(score))))
    color = _zone_color(score)

    # Needle geometry: score 0 → 180° (left), 100 → 0° (right), 50 → straight up.
    angle = _math.radians(180.0 - (score / 100.0) * 180.0)
    cx, cy, needle_len = 100.0, 100.0, 68.0
    nx = cx + needle_len * _math.cos(angle)
    ny = cy - needle_len * _math.sin(angle)
    grad = f"grad-{dom_id}"  # unique per card so duplicate IDs don't collide
    title_attr = f' title="{tooltip}"' if tooltip else ""
    source_html = f'<div class="fg-source">{source}</div>' if source else ""

    return f"""
<div class="market-meter" id="{dom_id}"{title_attr}>
  <svg class="fg-gauge" viewBox="0 0 200 118" role="img"
       aria-label="{label}: {score}, {rating}">
    <defs>
      <linearGradient id="{grad}" x1="0%" y1="0%" x2="100%" y2="0%">
        <stop offset="0%" stop-color="#e5484d"/>
        <stop offset="30%" stop-color="#f76b15"/>
        <stop offset="50%" stop-color="#f5b301"/>
        <stop offset="72%" stop-color="#7fbf3f"/>
        <stop offset="100%" stop-color="#2fa84f"/>
      </linearGradient>
    </defs>
    <path d="M20,100 A80,80 0 0 1 180,100" fill="none"
          stroke="url(#{grad})" stroke-width="15" stroke-linecap="round"/>
    <line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}"
          stroke="var(--fg-strong)" stroke-width="3" stroke-linecap="round"/>
    <circle cx="{cx}" cy="{cy}" r="6" fill="var(--fg-strong)"/>
  </svg>
  <div class="fg-readout">
    <div class="fg-score" style="color:{color};">{score}</div>
    <div class="fg-rating" style="color:{color};">{rating}</div>
    <div class="fg-label">{label}</div>
    {sub_html}
    {source_html}
  </div>
</div>"""


def _render_fear_greed_gauge(fg: Optional[dict]) -> str:
    """Market Fear & Greed dial. Empty string when no reading is available."""
    if not fg:
        return ""
    score = max(0, min(100, int(fg.get("score", 50))))
    rating = fg.get("rating") or _fg_rating(score)
    source = fg.get("source") or "Fear & Greed"

    # Optional "vs prior" context — only present for the CNN source.
    prev_bits = []
    for lbl, key in (("Prev close", "previous_close"),
                     ("Week ago", "previous_week"),
                     ("Month ago", "previous_month")):
        v = fg.get(key)
        if isinstance(v, (int, float)):
            prev_bits.append(
                f'<span class="fg-prev-item">{lbl} <strong>{round(v)}</strong></span>'
            )
    sub_html = (f'<div class="fg-prev">{"".join(prev_bits)}</div>'
                if prev_bits else "")

    return _render_gauge_card(
        dom_id="marketMeter", score=score, rating=rating,
        label="Market Fear &amp; Greed", sub_html=sub_html, source=source,
        tooltip="Market-wide sentiment. A contrarian, slow-moving gauge — not a timing signal.",
    )


def _render_portfolio_health_gauge(results: list, mode: Optional[str] = None) -> str:
    """
    Value-weighted average of per-position verdict scores (0–100), under
    `mode`'s verdicts (default the run's).
    Pairs 'how the market feels' with 'how strong your book is'.
    """
    scores = {}
    for r in results:
        v = verdict_in(r, mode)
        if v and v.score is not None:
            scores[id(r)] = v.score
    scored = [r for r in results
              if id(r) in scores and r.live_market_value is not None]
    if not scored:
        return ""
    total_val = sum(r.live_market_value for r in scored)
    if total_val <= 0:
        return ""
    health = sum(scores[id(r)] * r.live_market_value for r in scored) / total_val
    rating = (
        "Weak" if health < 25 else
        "Soft" if health < 45 else
        "Mixed" if health <= 55 else
        "Solid" if health <= 75 else
        "Strong"
    )
    strong = sum(1 for r in scored if scores[id(r)] >= 55)
    weak = sum(1 for r in scored if scores[id(r)] < 45)
    sub_html = (
        '<div class="fg-prev">'
        f'<span class="fg-prev-item">Strong <strong>{strong}</strong></span>'
        f'<span class="fg-prev-item">Weak <strong>{weak}</strong></span>'
        f'<span class="fg-prev-item">Rated <strong>{len(scored)}</strong></span>'
        '</div>'
    )
    return _render_gauge_card(
        dom_id="healthMeter" if mode is None else f"healthMeter-{mode}",
        score=health, rating=rating,
        label="Portfolio Health", sub_html=sub_html,
        source="Value-weighted verdict scores",
        tooltip="Your holdings' verdict scores, weighted by position size. Higher = a stronger book overall.",
    )


def _render_diversification_gauge(results: list) -> str:
    """
    Diversification dial (0–100) from position weights. Uses effective number
    of holdings (1 / Herfindahl index), normalized so ~15 effective names = 100.
    High = broadly spread; low = concentrated (single-name risk).
    """
    holdings = [r for r in results
                if r.live_market_value is not None and r.live_market_value > 0]
    if len(holdings) < 2:
        return ""
    total_val = sum(r.live_market_value for r in holdings)
    if total_val <= 0:
        return ""
    weights = [(r.ticker, r.live_market_value / total_val) for r in holdings]
    hhi = sum(w * w for _, w in weights)
    eff_n = 1.0 / hhi if hhi > 0 else len(holdings)
    score = max(0.0, min(100.0, eff_n / 15.0 * 100.0))
    rating = (
        "Concentrated" if score < 25 else
        "Focused" if score < 45 else
        "Balanced" if score <= 55 else
        "Diversified" if score <= 75 else
        "Broadly Diversified"
    )
    top_ticker, top_w = max(weights, key=lambda x: x[1])
    sub_html = (
        '<div class="fg-prev">'
        f'<span class="fg-prev-item">Top <strong>{top_ticker} {top_w * 100:.0f}%</strong></span>'
        f'<span class="fg-prev-item">Positions <strong>{len(holdings)}</strong></span>'
        f'<span class="fg-prev-item">Effective <strong>~{eff_n:.0f}</strong></span>'
        '</div>'
    )
    return _render_gauge_card(
        dom_id="diversifyMeter", score=score, rating=rating,
        label="Diversification", sub_html=sub_html,
        source="Effective holdings (Herfindahl)",
        tooltip="How spread out your portfolio is. Based on effective number of holdings; low = concentrated single-name risk.",
    )


# ============================================================
# News sentiment — bounded nudge to the verdict score
# ============================================================
# Recent headlines per ticker (Finnhub free company-news, yfinance fallback) are
# scored into one bullish/bearish number, cached on disk (refreshed once per
# calendar day by default — see _news_cache_mode), then mapped to a small
# bounded modifier in compute_verdict_v2 (see _news_signal_modifier). Scoring is
# FREE by default via a headline lexicon; if ANTHROPIC_API_KEY is set it upgrades
# to Claude for sharper reads, and a prior Claude read is kept visible while a
# refresh is in flight (stale-while-revalidate). That carry is bounded: the
# nudge halves after a trading day and stops entirely after five, so a
# persistently failing refresh fades to "no news signal" instead of freezing a
# stale opinion into every verdict (see _news_signal_modifier). The cache lives
# in .cache/ (gitignored; carried across CI runs) so warm runs re-fetch nothing.
# Set NEWS_SIGNAL=0 to disable entirely.

_NEWS_CACHE_PATH = Path(__file__).resolve().parent / ".cache" / "news_sentiment.json"
_news_cache_lock = threading.Lock()
_news_cache: Optional[dict] = None
_news_cache_dirty = False
_news_hits = 0
_news_misses = 0
_news_batched = 0                 # tickers scored via the 50%-off Batches API
_news_batch_inflight: set[str] = set()   # submitted, result not back yet
_anthropic_client_singleton = None

# Reserved key inside the news cache file holding batches that were submitted
# but hadn't finished when the run ended. Not a valid ticker, so it can never
# collide with a real cache entry.
_NEWS_PENDING_KEY = "__pending_batches__"

_NEWS_SENTIMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "label": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
        "rationale": {"type": "string"},
    },
    "required": ["score", "label", "rationale"],
    "additionalProperties": False,
}


def _et_now(ts: Optional[float] = None) -> datetime:
    """Market-time (America/New_York) datetime — now, or `ts` if given."""
    tz = ZoneInfo("America/New_York")
    return datetime.now(tz) if ts is None else datetime.fromtimestamp(ts, tz)


def _et_date_iso(ts: Optional[float] = None) -> str:
    """Market-time calendar date as ISO "YYYY-MM-DD" — the `as_of` stamp on a
    news read. Takes `ts` so a batch harvested by a later run is stamped with
    when it was scored, not when it happened to be collected."""
    return _et_now(ts).date().isoformat()


def _news_cache_mode() -> tuple[str, float]:
    """How long a scored ticker stays cached, from NEWS_CACHE_TTL_HOURS:

      unset / "day" / "daily"  -> ("day", 0)    re-score on the first run of a
                                                 new calendar day (America/New_York)
      a positive number N      -> ("hours", N)  rolling N-hour window
      "0"                      -> ("off", 0)     never cache; re-score every run

    Calendar-day is the default: news moves the verdict by a bounded ±6 nudge,
    so one fresh read per day is plenty, and "first run of the day" is a simpler
    mental model than a window that drifts by a few minutes each day. Multiple
    runs the same day are all cache hits."""
    raw = os.environ.get("NEWS_CACHE_TTL_HOURS", "").strip().lower()
    if raw in ("", "day", "daily"):
        return ("day", 0.0)
    try:
        hours = float(raw)
    except ValueError:
        return ("day", 0.0)
    if hours <= 0:
        return ("off", 0.0)
    return ("hours", hours)


def _news_caching_enabled() -> bool:
    return _news_cache_mode()[0] != "off"


def _news_entry_fresh(entry: Optional[dict]) -> bool:
    """True when a cache entry is fresh enough to reuse without re-scoring.
    Keyed on the entry's write time (`ts`) so it works for both real scores and
    cached negatives (no-headline tickers, whose sentiment is None)."""
    if not entry:
        return False
    mode, hours = _news_cache_mode()
    if mode == "off":
        return False
    ts = entry.get("ts", 0)
    if not ts:
        return False
    if mode == "hours":
        return (time.time() - ts) < hours * 3600
    return _et_now(ts).date() == _et_now().date()


def _load_news_cache() -> dict:
    global _news_cache
    if _news_cache is None:
        try:
            data = json.loads(_NEWS_CACHE_PATH.read_text())
            _news_cache = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            _news_cache = {}
    return _news_cache


def _flush_news_cache() -> None:
    global _news_cache_dirty
    with _news_cache_lock:
        if not _news_cache_dirty or _news_cache is None:
            return
        try:
            _NEWS_CACHE_PATH.parent.mkdir(exist_ok=True)
            _NEWS_CACHE_PATH.write_text(json.dumps(_news_cache))
            _news_cache_dirty = False
        except OSError:
            pass


def _anthropic_client():
    """Lazily construct (and memoize) the Anthropic client. Returns None when the
    SDK isn't installed or no key is configured."""
    global _anthropic_client_singleton
    if not ANTHROPIC_API_KEY:
        return None
    if _anthropic_client_singleton is None:
        try:
            import anthropic
            _anthropic_client_singleton = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        except Exception as e:
            print(f"[news] Anthropic SDK unavailable: {e}")
            _anthropic_client_singleton = False   # sentinel: tried and failed
    return _anthropic_client_singleton or None


def fetch_recent_headlines(ticker: str, max_items: int = 8) -> list[str]:
    """Recent company headlines (last ~7 days), newest first. Finnhub
    company-news (free endpoint) preferred; yfinance .news as fallback."""
    out: list[str] = []
    if FINNHUB_API_KEY and requests:
        try:
            today = datetime.now(ZoneInfo("America/New_York")).date()
            frm = (today - timedelta(days=7)).isoformat()
            r = requests.get(
                "https://finnhub.io/api/v1/company-news",
                params={"symbol": ticker, "from": frm, "to": today.isoformat(),
                        "token": FINNHUB_API_KEY},
                timeout=8,
            )
            if r.status_code == 200:
                arts = sorted(r.json() or [],
                              key=lambda a: a.get("datetime", 0), reverse=True)
                for a in arts[:max_items]:
                    h = (a.get("headline") or "").strip()
                    if h:
                        src = (a.get("source") or "").strip()
                        out.append(h + (f" — {src}" if src else ""))
        except Exception:
            pass
    if not out:
        try:
            for a in (getattr(yf.Ticker(ticker), "news", None) or [])[:max_items]:
                if not isinstance(a, dict):
                    continue
                title = (a.get("title")
                         or (a.get("content") or {}).get("title") or "").strip()
                if title:
                    out.append(title)
        except Exception:
            pass
    return out[:max_items]


# The prompt and the response parsing are shared by the real-time path
# (_call_claude_sentiment) and the batch path (prewarm_news_sentiment) so the
# two can never drift into scoring the same headlines differently.
_NEWS_SYSTEM = (
    "You are an equity news analyst. Read a stock's recent headlines and "
    "judge the net sentiment for its forward price over the next few weeks. "
    "Weigh material catalysts (earnings, guidance, M&A, regulatory, "
    "litigation, products) far more than routine coverage; ignore generic "
    "market commentary; be skeptical of hype."
)


def _news_request_params(ticker, name, headlines) -> dict:
    """The Messages-API params for scoring one ticker — identical whether sent
    real-time or inside a batch."""
    label = f"{name} ({ticker})" if name else ticker
    # Headlines carry their signal in the first clause; a few sources emit
    # 300-char SEO titles. Trim so one outlier can't triple the input tokens.
    bullets = "\n".join(f"- {h[:160]}" for h in headlines)
    prompt = (
        f"Stock: {label}\n\nRecent headlines (newest first):\n{bullets}\n\n"
        "Return JSON with: score (number from -1.0 very bearish to +1.0 very "
        "bullish, 0 if neutral/no clear signal), label (bullish | neutral | "
        "bearish), and rationale (one concise sentence, max ~20 words, citing "
        "the key driver)."
    )
    return {
        "model": NEWS_MODEL,
        # The schema caps this at a score, a label and a ~20-word rationale
        # (~70 tokens). 200 is a spend guard rail, not a squeeze: output is
        # billed on tokens actually generated, so this only bounds a runaway.
        "max_tokens": 200,
        "system": _NEWS_SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema",
                                     "schema": _NEWS_SENTIMENT_SCHEMA}},
    }


def _parse_sentiment_response(content, headlines,
                              as_of: Optional[str] = None) -> Optional[dict]:
    """Turn a Message's content blocks into a sentiment dict, or None if the
    model returned something unparseable (caller falls back to the lexicon).

    `as_of` defaults to today, which is right for a real-time score. The harvest
    path passes the date its batch was *submitted*, because that's when the
    headlines were pulled — stamping harvest day would make a read look a run
    fresher than it is, and _news_signal_age_days decides weight off this."""
    text = next((b.text for b in content if b.type == "text"), "")
    data = json.loads(text)
    score = max(-1.0, min(1.0, float(data.get("score", 0) or 0)))
    return {
        "score": score,
        "label": str(data.get("label", "neutral")),
        "rationale": str(data.get("rationale", "")).strip(),
        "headlines": headlines[:5],
        "as_of": as_of or _et_date_iso(),
    }


def _call_claude_sentiment(client, ticker, name, headlines) -> Optional[dict]:
    """Score one ticker in real time (full price). The batch path in
    prewarm_news_sentiment is half the cost and handles the bulk of tickers;
    this covers stragglers it didn't cover."""
    try:
        resp = client.with_options(timeout=20.0).messages.create(
            **_news_request_params(ticker, name, headlines)
        )
        return _parse_sentiment_response(resp.content, headlines)
    except Exception as e:
        print(f"[news] {ticker}: scoring failed ({type(e).__name__}: {e})")
        return None


# Free, dependency-free fallback: a compact finance sentiment lexicon. Words are
# chosen to be clearly directional (avoiding ambiguous terms like "high"/"cut"
# that flip meaning by context), so the score only moves on one-sided headlines.
_NEWS_POS = {
    "beat", "beats", "surge", "surges", "soar", "soars", "jump", "jumps",
    "rally", "rallies", "upgrade", "upgrades", "upgraded", "raise", "raised",
    "raises", "record", "strong", "growth", "outperform", "outperforms", "tops",
    "gain", "gains", "bullish", "approval", "approved", "wins", "expand",
    "expands", "expansion", "profit", "profits", "breakthrough", "surpass",
    "surpasses", "accelerate", "accelerates", "milestone", "rebound", "rebounds",
    "boost", "boosts", "buyback", "upbeat", "momentum", "optimistic",
}
_NEWS_NEG = {
    "miss", "misses", "missed", "plunge", "plunges", "plummet", "plummets",
    "slump", "slumps", "downgrade", "downgrades", "downgraded", "slash",
    "slashes", "lawsuit", "sued", "probe", "investigation", "recall", "recalls",
    "warns", "warning", "weak", "weakness", "decline", "declines", "loss",
    "losses", "layoff", "layoffs", "bearish", "halt", "halts", "fraud",
    "bankruptcy", "sinks", "tumble", "tumbles", "slowdown", "downturn",
    "default", "delist", "scandal", "fears", "disappoint", "disappoints",
    "underperform", "selloff", "crash", "crashes",
}


def _lexicon_sentiment(headlines: list[str]) -> Optional[dict]:
    """Free fallback scorer: net directional-word balance across recent
    headlines, in the same shape as the Claude scorer. Stays in the neutral band
    (no nudge) unless there are at least a couple of clear signal words."""
    import re as _re
    pos = neg = 0
    for h in headlines:
        for tok in _re.findall(r"[a-z][a-z'-]+", h.lower()):
            if tok in _NEWS_POS:
                pos += 1
            elif tok in _NEWS_NEG:
                neg += 1
    total = pos + neg
    score = (pos - neg) / total if total >= 2 else 0.0   # thin signal -> neutral
    label = "bullish" if score >= 0.2 else "bearish" if score <= -0.2 else "neutral"
    return {
        "score": round(score, 2),
        "label": label,
        "rationale": (f"{pos} positive vs {neg} negative signal words across "
                      f"{len(headlines)} recent headlines"),
        "headlines": headlines[:5],
        "as_of": _et_date_iso(),
        "method": "lexicon",
    }


def _is_claude_sentiment(sentiment: Optional[dict]) -> bool:
    """A real model read, as opposed to the free lexicon fallback."""
    return bool(sentiment and sentiment.get("method") != "lexicon")


def score_news_sentiment(ticker: str, name: Optional[str] = None) -> Optional[dict]:
    """Score a ticker's recent news, cached on disk. Uses Claude when
    ANTHROPIC_API_KEY is set, otherwise a FREE headline lexicon — both return
    {score, label, rationale, headlines, as_of}. Returns None when disabled
    (NEWS_SIGNAL=0) or there's no news. Never raises; disk write is batched by
    _flush_news_cache()."""
    global _news_cache_dirty, _news_hits, _news_misses
    if os.environ.get("NEWS_SIGNAL", "1") == "0":
        return None

    with _news_cache_lock:
        entry = _load_news_cache().get(ticker)
    prior = (entry or {}).get("sentiment")
    if entry is not None and _news_entry_fresh(entry):
        with _news_cache_lock:
            _news_hits += 1
        return prior

    headlines = fetch_recent_headlines(ticker)
    result = None
    if headlines:
        # A ticker already in flight in a batch must NOT be re-scored in real
        # time — that would pay full price for an answer we've already bought at
        # half. The batch result lands in the cache on a later run (see
        # _harvest_pending_news_batches).
        client = None if ticker in _news_batch_inflight else _anthropic_client()
        if client is not None:
            result = _call_claude_sentiment(client, ticker, name, headlines)
        if result is None:                 # no key, in-flight, or LLM failed
            # Stale-while-revalidate: a prior Claude read — even a day old —
            # beats a fresh lexicon read for a bounded ±6 nudge. Keep serving it
            # and leave the cache entry untouched (so it stays "stale" and the
            # pending batch's harvest, or the next real-time attempt, replaces
            # it). Only fall back to the lexicon when there's no Claude score to
            # carry forward.
            #
            # Leaving the entry alone also preserves its `as_of`, which is what
            # bounds this carry: _news_signal_modifier fades the nudge by the
            # read's age, so a refresh that keeps failing decays to no signal
            # instead of pinning old headlines to the verdict forever.
            if _is_claude_sentiment(prior):
                with _news_cache_lock:
                    _news_hits += 1
                return prior
            result = _lexicon_sentiment(headlines)

    if _news_caching_enabled():
        with _news_cache_lock:
            _load_news_cache()[ticker] = {"ts": time.time(), "sentiment": result}
            _news_cache_dirty = True
            _news_misses += 1
    return result


# ============================================================
# News sentiment — batch pre-pass (50% cheaper than real-time)
# ============================================================
# Scoring every ticker one-at-a-time inside the analysis loop pays full API
# price. The Batches API is half price for the same requests, and this run is a
# nightly/scheduled job with no user waiting on any individual ticker — so all
# cache misses are submitted as one batch up front, and the analysis loop then
# reads them out of the cache as ordinary hits.
#
# Batches are usually done in well under a minute at this size, but the API only
# guarantees 24h. So the wait is bounded (NEWS_BATCH_WAIT_SECONDS, default 300):
# if the batch hasn't landed by then the run finishes on the free lexicon and
# the batch id is persisted, so the NEXT run harvests the results instead of
# abandoning work already paid for. Set NEWS_BATCH=0 to go back to real-time.


def _news_batch_enabled() -> bool:
    return os.environ.get("NEWS_BATCH", "1").strip() != "0"


def _news_batch_wait_seconds() -> float:
    try:
        return float(os.environ.get("NEWS_BATCH_WAIT_SECONDS", "") or 300)
    except ValueError:
        return 300.0


def _news_cache_fresh(cache: dict, ticker: str) -> bool:
    return _news_entry_fresh(cache.get(ticker))


def _store_batch_results(client, batch_id: str, id_map: dict,
                         headlines_by_ticker: dict,
                         as_of: Optional[str] = None) -> int:
    """Write a finished batch's results into the news cache. Returns how many
    tickers were scored. Unparseable or errored entries are simply left out —
    the ticker falls through to the lexicon on the next read.

    `as_of` dates the scores (default today, for a batch submitted this run);
    the cross-run harvest passes the submission date. The cache entry's own `ts`
    stays the write time either way — that drives the re-score TTL, while
    `as_of` drives how much weight the verdict gives the read."""
    global _news_cache_dirty, _news_batched
    scored = 0
    for res in client.messages.batches.results(batch_id):
        ticker = id_map.get(res.custom_id)
        if not ticker:
            continue
        if res.result.type != "succeeded":
            print(f"[news-batch] {ticker}: {res.result.type}")
            continue
        try:
            sentiment = _parse_sentiment_response(
                res.result.message.content,
                headlines_by_ticker.get(ticker, []),
                as_of,
            )
        except Exception as e:
            print(f"[news-batch] {ticker}: unparseable ({type(e).__name__}: {e})")
            continue
        with _news_cache_lock:
            _load_news_cache()[ticker] = {"ts": time.time(), "sentiment": sentiment}
            _news_cache_dirty = True
            _news_batched += 1
        scored += 1
    _flush_news_cache()   # make paid-for scores durable immediately
    return scored


def _pending_batches() -> list:
    with _news_cache_lock:
        pending = _load_news_cache().get(_NEWS_PENDING_KEY)
    return pending if isinstance(pending, list) else []


def _set_pending_batches(pending: list) -> None:
    """Replace the pending-batch list and persist immediately. The flush is not
    optional: a run that harvests everything and has nothing left to score
    reaches no other flush point, and would re-harvest the same batch forever."""
    global _news_cache_dirty
    with _news_cache_lock:
        cache = _load_news_cache()
        if pending:
            cache[_NEWS_PENDING_KEY] = pending
        else:
            cache.pop(_NEWS_PENDING_KEY, None)
        _news_cache_dirty = True
    _flush_news_cache()


def _harvest_pending_news_batches(client) -> None:
    """Collect results from batches a previous run submitted but didn't wait
    out. Never blocks: a batch still running is left pending for the run after
    this one. Batches older than 24h are dropped (the API expires them).

    Tickers in a batch that is *still* running are marked in-flight, so this run
    neither re-submits them in a new batch nor scores them real-time — either
    would be paying a second time for an answer already bought."""
    pending = _pending_batches()
    if not pending:
        return
    still_pending = []
    for rec in pending:
        batch_id, id_map = rec.get("id"), rec.get("map") or {}
        if not batch_id:
            continue
        if time.time() - rec.get("ts", 0) > 24 * 3600:
            print(f"[news-batch] dropping expired batch {batch_id}")
            continue
        try:
            status = client.messages.batches.retrieve(batch_id).processing_status
        except Exception as e:
            print(f"[news-batch] {batch_id}: status check failed "
                  f"({type(e).__name__}: {e})")
            still_pending.append(rec)
            _news_batch_inflight.update(id_map.values())  # unknown -> don't re-buy
            continue
        if status != "ended":
            still_pending.append(rec)
            _news_batch_inflight.update(id_map.values())
            continue
        try:
            n = _store_batch_results(client, batch_id, id_map,
                                     rec.get("headlines") or {},
                                     _et_date_iso(rec.get("ts") or None))
            print(f"[news-batch] harvested {n} score(s) from prior run's batch "
                  f"{batch_id}")
        except Exception as e:
            print(f"[news-batch] {batch_id}: result fetch failed "
                  f"({type(e).__name__}: {e})")
            still_pending.append(rec)
    _set_pending_batches(still_pending)


def prewarm_news_sentiment(rows: list[dict]) -> None:
    """Score every cache-missing ticker in `rows` via the Batches API (half the
    real-time price) before the analysis loop runs. Best-effort throughout: any
    failure leaves the existing real-time/lexicon path to handle the ticker.

    Safe to call more than once per process — tickers already cached or already
    in flight are skipped."""
    if os.environ.get("NEWS_SIGNAL", "1") == "0" or not _news_batch_enabled():
        return
    client = _anthropic_client()
    if client is None:            # no key -> lexicon path, nothing to batch
        return

    try:
        _harvest_pending_news_batches(client)
    except Exception as e:
        print(f"[news-batch] harvest failed ({type(e).__name__}: {e})")

    with _news_cache_lock:
        cache = _load_news_cache()
        # A stale ticker is re-batched to refresh it even when it has a prior
        # Claude score — stale-while-revalidate keeps that old score visible in
        # this run's report while the batch lands, rather than blocking the
        # refresh (see score_news_sentiment). The old score is carried at
        # reduced weight, decaying to none, so a batch that never lands can't
        # keep nudging verdicts (see _news_signal_modifier).
        todo = [r for r in rows
                if r.get("ticker")
                and r["ticker"] not in _news_batch_inflight
                and not _news_cache_fresh(cache, r["ticker"])
                # ETFs and thematic plays never reach the news scorer in the
                # analysis loop (compounders only), so batching them would buy
                # answers nothing reads. Static sets only — quoteType detection
                # needs a network call we don't have here.
                and r["ticker"] not in KNOWN_ETFS
                and r["ticker"] not in THEMATIC_OVERRIDES]
    if not todo:
        return

    # Headline fetch is network-bound and independent per ticker; serially this
    # would add ~1s each to startup.
    def _fetch(row):
        return row["ticker"], row.get("name"), fetch_recent_headlines(row["ticker"])

    fetched = []
    with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
        for ticker, name, headlines in ex.map(_fetch, todo):
            if headlines:
                fetched.append((ticker, name, headlines))
    if not fetched:
        return
    # Kept so a result can be stored next to the headlines it was scored from
    # (the report renders them), including across a cross-run harvest.
    headlines_by_ticker = {t: h[:5] for t, _, h in fetched}

    # custom_id must be [a-zA-Z0-9_-], so index rather than using the ticker
    # directly (a symbol like BRK.B would be rejected).
    id_map = {f"news-{i}": ticker for i, (ticker, _, _) in enumerate(fetched)}
    requests = [
        {"custom_id": f"news-{i}",
         "params": _news_request_params(ticker, name, headlines)}
        for i, (ticker, name, headlines) in enumerate(fetched)
    ]

    try:
        batch = client.messages.batches.create(requests=requests)
    except Exception as e:
        print(f"[news-batch] submit failed ({type(e).__name__}: {e}) — "
              f"falling back to real-time scoring")
        return

    _news_batch_inflight.update(id_map.values())
    wait = _news_batch_wait_seconds()
    deadline = time.time() + wait
    # wait=0 is the "always one run behind" mode: submit, don't block, and let
    # the next run harvest. Sensible because scores refresh only once a day and
    # a prior Claude read stays visible meanwhile (stale-while-revalidate), so
    # waiting for same-run freshness buys very little.
    print(f"[news-batch] submitted {len(requests)} ticker(s) as {batch.id}; "
          + ("not waiting (harvest on next run)" if wait <= 0
             else f"waiting up to {wait:.0f}s"))
    try:
        while True:
            status = client.messages.batches.retrieve(batch.id).processing_status
            if status == "ended":
                n = _store_batch_results(client, batch.id, id_map,
                                         headlines_by_ticker)
                _news_batch_inflight.difference_update(id_map.values())
                print(f"[news-batch] {n}/{len(requests)} scored at 50% cost")
                return
            if time.time() >= deadline:
                break
            time.sleep(min(5.0, max(0.5, deadline - time.time())))
    except Exception as e:
        print(f"[news-batch] polling failed ({type(e).__name__}: {e})")
        _news_batch_inflight.difference_update(id_map.values())
        return

    # Still running. Leave the tickers marked in-flight so this run uses the
    # lexicon rather than paying full price, and record the batch so the next
    # run picks up the results we've already been charged for.
    print(f"[news-batch] {batch.id} queued"
          + ("" if wait <= 0 else f", still running after {wait:.0f}s")
          + " — lexicon this run, harvesting next run")
    _set_pending_batches(_pending_batches() + [{
        "id": batch.id, "ts": time.time(), "map": id_map,
        "headlines": headlines_by_ticker,
    }])


# Age decay for a carried-over news read, in TRADING days (see
# _news_signal_age_days). A fresh read nudges at full strength; one that is only
# still here because the refresh failed loses half its weight, then all of it.
# Without this, a stuck news path (key pulled, Finnhub erroring, batches always
# landing after NEWS_BATCH_WAIT_SECONDS) would ride one day's headlines into
# every verdict indefinitely — and unlike P/E or ROE, a headline read is about a
# specific event, so its information is mostly gone within the week.
_NEWS_FULL_WEIGHT_DAYS = 1     # <= this many trading days old: full nudge
_NEWS_MAX_AGE_DAYS = 5         # >= this many: no nudge at all


def _weekdays_between(start: date, end: date) -> int:
    """Weekdays falling after `start`, up to and including `end`. Zero when
    `end` isn't after `start`, so a future-dated read (clock skew between the
    scorer and this run) reads as age 0 rather than a negative age."""
    total = (end - start).days
    if total <= 0:
        return 0
    weeks, extra = divmod(total, 7)
    n = weeks * 5                      # every whole week is exactly 5 weekdays
    d = start + timedelta(days=weeks * 7)
    for _ in range(extra):             # at most 6 iterations
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _news_signal_age_days(news: Optional[dict]) -> Optional[int]:
    """How many trading days old a news read is, or None when it carries no
    usable `as_of` — an unknown age is treated as fresh by the caller rather
    than silently discounting a score.

    Trading days rather than calendar days because the scheduled runs are
    weekday-only (.github/workflows/*.yml): a Friday read served on Monday is
    three calendar days old but has missed just one session of news, so pricing
    it as three days stale would penalize every Monday. Holidays aren't modeled,
    matching _us_market_open_now — a holiday counts as a session, which only
    ever fades the nudge slightly early."""
    raw = (news or {}).get("as_of")
    if not raw:
        return None
    try:
        as_of = datetime.strptime(str(raw), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return _weekdays_between(as_of, _et_now().date())


def _news_as_of_label(news: Optional[dict]) -> str:
    """A read's `as_of` as a short "Aug 24", or the raw value if it isn't a
    date. Shown in the verdict reason so an aged read can't pass for today's."""
    raw = str((news or {}).get("as_of") or "")
    try:
        d = datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return raw
    return f"{d:%b} {d.day}"


def _news_signal_modifier(news: Optional[dict]):
    """Map a news-sentiment dict to (delta, description) for the verdict, or None
    for a neutral/absent signal. Bounded to ±6 so news nudges but never dominates
    fundamentals (same magnitude band as the insider/sector signals), then
    haircut by the read's age so a carried-over score fades instead of standing
    in for today's news (see _NEWS_FULL_WEIGHT_DAYS)."""
    if not news:
        return None
    score = news.get("score")
    if score is None:
        return None
    if score >= 0.5:
        delta = 6
    elif score >= 0.2:
        delta = 3
    elif score <= -0.5:
        delta = -6
    elif score <= -0.2:
        delta = -3
    else:
        return None   # neutral band — no nudge

    age = _news_signal_age_days(news)
    stale = age is not None and age > _NEWS_FULL_WEIGHT_DAYS
    if stale:
        if age >= _NEWS_MAX_AGE_DAYS:
            return None       # too old to claim anything about today
        if abs(delta) < 6:
            # Half of ±3 rounds to nothing on purpose: a read that was only a
            # weak signal when fresh isn't worth carrying once it's stale.
            return None
        delta = 3 if delta > 0 else -3

    rationale = (news.get("rationale") or "").strip()
    lbl = news.get("label") or ("positive" if delta > 0 else "negative")
    # "Recent news" only when the read really is from today; otherwise the
    # reason carries the date, so the verdict can't call last week's headline
    # recent. `age` of None (no as_of) keeps the original wording.
    if not age:
        when = "Recent news"
    else:
        when = f"News ({_news_as_of_label(news)}"
        when += ", half weight)" if stale else ")"
    desc = f"{when} {lbl}" + (f": {rationale}" if rationale else "")
    return (delta, desc)


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
    # Optional pre-formatted tooltip value. When set, it replaces the
    # auto-formatted number (used by P/E to show trailing + forward side by
    # side, which the generic % formatter can't express).
    display: Optional[str] = None
    # Soft-gate credit (0-1) this filter adds to the quality-filter score: 1.0
    # on a pass, fading to 0 over a band past the threshold (see
    # _soft_credit_min/_max). None means the filter had no data — it's left out
    # of the score and lowers its coverage instead of counting as a fail.
    credit: Optional[float] = None


# Soft gates for the quality-filter score (the `quality` base-score mode). A
# filter that misses its threshold keeps partial credit, fading linearly to zero
# this far past it (relative to the threshold). Pass/fail itself is unchanged —
# this only stops a P/E drifting from 29.9 to 30.1 intraday from swinging the
# verdict by a whole filter's worth of points.
_SOFT_GATE_BAND = 0.20


def _soft_credit_min(value: float, threshold: float, passed: bool) -> float:
    """Credit for an at-least gate (growth, ROE, margin, quick ratio): 1.0 on a
    pass, fading to 0 at threshold * (1 - band)."""
    if passed:
        return 1.0
    floor = threshold * (1 - _SOFT_GATE_BAND)
    return _clip01((value - floor) / (threshold - floor))


def _soft_credit_max(value: float, threshold: float, passed: bool) -> float:
    """Credit for a below gate (P/E, PEG, debt/equity): 1.0 on a pass, fading
    to 0 at threshold * (1 + band)."""
    if passed:
        return 1.0
    ceiling = threshold * (1 + _SOFT_GATE_BAND)
    return _clip01((ceiling - value) / (ceiling - threshold))


def _is_loss_making(info: dict) -> bool:
    """True when yfinance shows the company losing money. Earnings-based gates
    (EPS growth, P/E, PEG) have no value for such names; that absence is a
    genuine fail, not missing data, so it must still cost the filter's credit."""
    for key in ("trailingEps", "netIncomeToCommon", "profitMargins"):
        v = _safe_get(info, key)
        if v is not None and v <= 0:
            return True
    return False


def _safe_get(d: dict, key: str) -> Optional[float]:
    v = d.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pe_values(
    info: dict,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (effective_pe, trailing_pe, forward_pe).

    effective_pe is the LOWER of trailing/forward (positive values only), so a
    stock counts as reasonably valued if EITHER basis is cheap. Trailing PE
    alone overstates valuation for fast growers and cyclical-trough names
    (e.g. MU ~49x trailing / ~9x forward), red-flagging the very compounders
    this framework hunts for. Forward PE still fails names that are expensive
    on next year's earnings too (VRT, ANET both stay >30x), so it remains a
    discriminating gate rather than a rubber stamp.
    """
    trailing = _safe_get(info, "trailingPE")
    forward = _safe_get(info, "forwardPE")
    candidates = [p for p in (trailing, forward) if p is not None and p > 0]
    effective = min(candidates) if candidates else None
    return effective, trailing, forward


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


# --- Fundamentals (quarterly-derived growth metrics) disk cache --------------
# _compute_multi_year_growth() makes the run's two heaviest yfinance calls per
# ticker (income_stmt + balance_sheet) to derive metrics that only change when a
# company reports — i.e. quarterly, never intraday. For a frequently-refreshing
# report that's wasted work, so we cache the *output* metrics per ticker with a
# TTL (default 24h). Live prices are NOT cached (they come from .info every run),
# so verdicts are identical to a fresh run within the TTL window. The cache lives
# in .cache/ (gitignored; carried across CI runs via the Actions cache), and any
# error falls back to a live compute. Set FUNDAMENTALS_CACHE_TTL_HOURS=0 to
# disable entirely.
_FUNDAMENTALS_CACHE_PATH = Path(__file__).resolve().parent / ".cache" / "fundamentals.json"
_GROWTH_METRIC_KEYS = (
    "revenueCAGR3y", "revenueGrowthLookback",
    "earningsCAGR3y", "earningsGrowthLookback",
    "operatingMarginAvg3y", "operatingMarginLookback",
    "roeAvg3y", "roeLookback",
    "fcfConsistency", "fcfConsistencyLookback",
)
_fund_cache_lock = threading.Lock()
_fund_cache: Optional[dict] = None
_fund_cache_dirty = False          # True once a miss adds/updates an entry
_fund_hits = 0
_fund_misses = 0

# After-hours price override (ticker -> Robinhood extended-hours last price).
# main() populates this in --source robinhood mode when the regular session is
# closed; analyze_position() then prefers it over the yfinance price so the
# report shows broker-accurate after-hours values. Empty in all other modes.
_RH_EXTENDED_PRICES: dict[str, float] = {}


def _us_market_open_now() -> bool:
    """True during the regular US equity session (Mon-Fri 9:30-16:00 ET).

    Holidays aren't modeled — on a holiday this returns True and we simply skip
    the after-hours override (the report shows the last regular close), which is
    harmless. Used to decide whether to pull extended-hours prices.
    """
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:          # Sat/Sun
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= mins < (16 * 60)


def _fundamentals_ttl_hours() -> float:
    try:
        return float(os.environ.get("FUNDAMENTALS_CACHE_TTL_HOURS", "24"))
    except ValueError:
        return 24.0


def _load_fund_cache() -> dict:
    """Lazy-load the on-disk metrics cache into a shared in-memory dict (the
    in-memory dict is the source of truth; disk is a write-through snapshot)."""
    global _fund_cache
    if _fund_cache is None:
        try:
            data = json.loads(_FUNDAMENTALS_CACHE_PATH.read_text())
            _fund_cache = data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            _fund_cache = {}
    return _fund_cache


def _compute_growth_cached(tkr, info: dict, ticker: str) -> None:
    """Cache-aware wrapper around _compute_multi_year_growth(). On a fresh cache
    hit, inject the stored metrics and skip the income_stmt + balance_sheet
    fetches; otherwise compute live and stage the result in memory (the disk
    write is batched once per run by _flush_fund_cache())."""
    global _fund_cache_dirty, _fund_hits, _fund_misses
    ttl = _fundamentals_ttl_hours()
    if ttl > 0:
        with _fund_cache_lock:
            entry = _load_fund_cache().get(ticker)
        if entry and (time.time() - entry.get("ts", 0)) < ttl * 3600:
            for k, v in (entry.get("metrics") or {}).items():
                info[k] = v
            with _fund_cache_lock:
                _fund_hits += 1
            return  # cache hit — no statement fetches

    _compute_multi_year_growth(tkr, info)

    if ttl > 0:
        metrics = {k: info[k] for k in _GROWTH_METRIC_KEYS if k in info}
        with _fund_cache_lock:
            _load_fund_cache()[ticker] = {"ts": time.time(), "metrics": metrics}
            _fund_cache_dirty = True
            _fund_misses += 1


def _flush_fund_cache() -> None:
    """Write the staged fundamentals cache to disk once (called after a parallel
    batch). Avoids the per-ticker full-file rewrites that O(n^2)'d large runs."""
    global _fund_cache_dirty
    with _fund_cache_lock:
        if not _fund_cache_dirty or _fund_cache is None:
            return
        try:
            _FUNDAMENTALS_CACHE_PATH.parent.mkdir(exist_ok=True)
            _FUNDAMENTALS_CACHE_PATH.write_text(json.dumps(_fund_cache))
            _fund_cache_dirty = False
        except OSError:
            pass


def apply_quality_filters(info: dict) -> list[FilterResult]:
    """
    Apply the nine-filter quality compounder framework.

    Note on yfinance units:
      - revenueGrowth / earningsGrowth / operatingMargins / returnOnEquity
        are returned as decimals (0.10 = 10%).
      - debtToEquity is returned as a percentage (100 = 1.0 ratio).
    """
    results: list[FilterResult] = []
    # Earnings-based gates with no value are a fail for a loss-maker (credit 0)
    # but missing data for anyone else (credit None) — see FilterResult.credit.
    no_earnings_credit = 0.0 if _is_loss_making(info) else None

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
    rev_pass = rev_cagr is not None and rev_cagr >= 0.10
    results.append(FilterResult(
        name="Revenue growth >=10%",
        passed=rev_pass,
        actual=(rev_cagr * 100) if rev_cagr is not None else None,
        threshold=">= 10%",
        note=rev_lookback or "",
        credit=(_soft_credit_min(rev_cagr, 0.10, rev_pass)
                if rev_cagr is not None else None),
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
    eps_pass = eps_cagr is not None and eps_cagr >= 0.10
    results.append(FilterResult(
        name="EPS growth >=10%",
        passed=eps_pass,
        actual=(eps_cagr * 100) if eps_cagr is not None else None,
        threshold=">= 10%",
        note=eps_lookback or "",
        credit=(_soft_credit_min(eps_cagr, 0.10, eps_pass)
                if eps_cagr is not None else no_earnings_credit),
    ))

    # 3. P/E < 30 — pass if EITHER trailing or forward PE is under 30 (uses
    #    the lower of the two; see _pe_values for the rationale).
    pe, trailing_pe, forward_pe = _pe_values(info)
    if trailing_pe and forward_pe:
        pe_display = f"{trailing_pe:.1f} trailing / {forward_pe:.1f} fwd"
    elif trailing_pe:
        pe_display = f"{trailing_pe:.1f} trailing"
    elif forward_pe:
        pe_display = f"{forward_pe:.1f} fwd"
    else:
        pe_display = None
    pe_pass = pe is not None and pe < 30
    if pe is not None:
        pe_credit = _soft_credit_max(pe, 30, pe_pass)
    elif trailing_pe is not None or forward_pe is not None:
        pe_credit = 0.0    # a P/E exists but none is positive: losses
    else:
        pe_credit = no_earnings_credit
    results.append(FilterResult(
        name="P/E < 30",
        passed=pe_pass,
        actual=pe,
        threshold="< 30",
        display=pe_display,
        credit=pe_credit,
    ))

    # 4. PEG < 2
    peg = _safe_get(info, "trailingPegRatio") or _safe_get(info, "pegRatio")
    peg_pass = peg is not None and 0 < peg < 2
    if peg is None:
        peg_credit = no_earnings_credit
    elif peg <= 0:
        peg_credit = 0.0   # negative PEG: shrinking or negative earnings
    else:
        peg_credit = _soft_credit_max(peg, 2, peg_pass)
    results.append(FilterResult(
        name="PEG < 2",
        passed=peg_pass,
        actual=peg,
        threshold="< 2",
        credit=peg_credit,
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
    roe_pass = roe_avg is not None and roe_avg >= 0.15
    results.append(FilterResult(
        name="ROE >= 15%",
        passed=roe_pass,
        actual=(roe_avg * 100) if roe_avg is not None else None,
        threshold=">= 15%",
        note=roe_lookback or "",
        credit=(_soft_credit_min(roe_avg, 0.15, roe_pass)
                if roe_avg is not None else None),
    ))

    # 6. Operating margin >= 15%  (prefer 3-year average; fall back to 1-year)
    #    Pricing power is a sustained phenomenon, not a one-year event. A
    #    cyclical with one good margin year shouldn't pass this filter.
    om_avg = _safe_get(info, "operatingMarginAvg3y")
    om_lookback = info.get("operatingMarginLookback")
    if om_avg is None:
        om_avg = _safe_get(info, "operatingMargins")
        om_lookback = "1yr TTM" if om_avg is not None else None
    om_pass = om_avg is not None and om_avg >= 0.15
    results.append(FilterResult(
        name="Op margin >= 15%",
        passed=om_pass,
        actual=(om_avg * 100) if om_avg is not None else None,
        threshold=">= 15%",
        note=om_lookback or "",
        credit=(_soft_credit_min(om_avg, 0.15, om_pass)
                if om_avg is not None else None),
    ))

    # 7. Debt-to-equity < 1 (yfinance returns this *100; 100 = 1.0)
    de_raw = _safe_get(info, "debtToEquity")
    de_ratio = (de_raw / 100) if de_raw is not None else None
    de_pass = de_ratio is not None and de_ratio < 1
    results.append(FilterResult(
        name="Debt/Equity < 1",
        passed=de_pass,
        actual=de_ratio,
        threshold="< 1",
        credit=(_soft_credit_max(de_ratio, 1, de_pass)
                if de_ratio is not None else None),
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
        # Near miss: cash-generative every year, just not growing.
        fcf_credit = 1.0 if fcf_pass else (0.5 if all_positive else 0.0)
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
        if fcf is None:
            fcf_credit = None
        else:
            fcf_credit = 1.0 if fcf_pass else (0.5 if fcf > 0 else 0.0)
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
        credit=fcf_credit,
    ))

    # 9. Quick ratio > 1.0
    qr = _safe_get(info, "quickRatio")
    qr_pass = qr is not None and qr > 1.0
    results.append(FilterResult(
        name="Quick ratio > 1.0",
        passed=qr_pass,
        actual=qr,
        threshold="> 1.0",
        credit=(_soft_credit_min(qr, 1.0, qr_pass) if qr is not None else None),
    ))

    return results


def compute_filter_score(filters: list[FilterResult]) -> tuple[Optional[float], float]:
    """Soft-gated quality-filter score: (0-100 or None, coverage 0-1).

    The mean credit over the filters that had data. Filters with no data are
    left out (like the composite drops a missing sub-score) and show up as
    lower coverage instead of dragging the score down as fails."""
    if not filters:
        return None, 0.0
    rated = [f.credit for f in filters if f.credit is not None]
    if not rated:
        return None, 0.0
    return (round(sum(rated) / len(rated) * 100, 1),
            round(len(rated) / len(filters), 3))


# ============================================================
# Verdict logic
# ============================================================

# A holding/watchlist name is flagged "earnings soon" (header stat + filter)
# when its next report is within this many calendar days. Kept in sync with the
# hardcoded threshold in the filter bar's 'earnings-soon' quick screen (plain-JS
# string, can't interpolate Python there).
EARNINGS_SOON_DAYS = 7

# Verdict score below which a position is included in the tax analysis
# section (in addition to explicit SELL/TRIM verdicts).
TAX_FLAG_SCORE_THRESHOLD = 75

# ---- Verdict base score (switchable) ----
# compute_verdict_v2 starts every verdict from a 0-100 base, then applies the
# context modifiers. The base comes from one of three modes:
#   composite — the Composite Score. The original behavior and the default.
#   quality   — the nine quality filters (soft-gated) at 70%, plus the same
#               analyst and insider sub-scores the composite uses, at 15% each.
#   blend     — the average of the composite and quality bases.
# Analyst and insider carry 15% each in every mode, so switching changes only
# how the fundamentals are measured. Chosen by --base-score, else the
# BASE_SCORE_MODE env var — a repository variable in CI, so scheduled runs
# (which dispatch without inputs) follow the saved choice too.
BASE_SCORE_BASES = ("composite", "quality", "blend")
BASE_SCORE_BASE_LABELS = {"composite": "Composite", "quality": "Quality",
                          "blend": "Blend"}
QUALITY_BASE_WEIGHTS = {"filters": 0.70, "analyst": 0.15, "insider": 0.15}

# ---- Calibration (switchable, orthogonal to the base) ----
# A second axis on top of the base: which *calibration* of the sub-scores and
# context modifiers the verdict uses. "std" is the original scoring, unchanged.
# "cal" applies the six findings from the September 2026 miss review (see
# CALIBRATION_BLURB) — six changes that were each traced to a specific scoring
# artefact rather than to a view about any one stock:
#
#   1. insider — a compensation-only / below-noise read returns *no score*
#      instead of 48, so the weight renormalizes away. The old value pinned a
#      15%-weight input near 48 for every RSU-paying mega-cap (53 of 55 ledger
#      entries sat in the 45-52 band), which is a level shift, not a signal.
#   2. value — rescaled so the quality GATE scores 50 rather than 0. The old
#      P/E 10->100 / 30->0 ramp is a deep-value scale: it marked a name the
#      nine filters passed as below-average on valuation, so the two bases
#      contradicted each other by construction.
#   3a. upside-to-target — a continuous ramp over the same +/-6 band instead of
#      a +6/+3/0/-3/-10 step function whose cliffs moved the score 6-9 points
#      on a 1% price move.
#   3b. momentum — a small, explicit credit for price holding above its own
#      200-day line. The old model rewarded realized strength only through the
#      trend bonus, which a name recovering from a drawdown cannot earn.
#   4. hysteresis — a label only gives ground once the score falls clear of the
#      threshold band, so a BUY holds instead of flickering off the next day.
#   6. earnings proximity — an imminent print is scored (fresh-money framing
#      only) instead of being rendered as a footnote the verdict ignores.
#
# Finding 5 (never drop a recently-held name from the tracked universe) is a
# data-collection fix, not a scoring one — it changes what gets analyzed, so it
# cannot be a per-view toggle. It is always on; --no-pin-recent-holdings opts
# out. See PIN_RECENT_HOLDINGS_DAYS.
CALIBRATIONS = ("std", "cal")
DEFAULT_CALIBRATION = "std"
CALIBRATION_LABELS = {"std": "Current", "cal": "Recalibrated"}
CALIBRATION_SUFFIX = "-cal"

# Finding 2: value sub-score ramps, as (x, score) anchor points. The gate
# thresholds the nine quality filters use (P/E 30, PEG 2) score 50 — passing a
# gate means average, not zero.
CAL_VALUE_PE_RAMP = ((15.0, 100.0), (30.0, 50.0), (45.0, 0.0))
CAL_VALUE_PEG_RAMP = ((0.5, 100.0), (2.0, 50.0), (3.5, 0.0))
# Finding 3a: upside-to-target is +/-CAL_UPSIDE_MAX, reaching full value at
# CAL_UPSIDE_FULL_PCT of upside (or downside) instead of stepping.
CAL_UPSIDE_MAX = 6.0
CAL_UPSIDE_FULL_PCT = 20.0
# Finding 3b: realized-strength credit from price vs its own 200-day MA.
CAL_MOMENTUM_BANDS = ((15.0, 4), (5.0, 2), (-5.0, 0), (-15.0, -2))   # floor, delta
CAL_MOMENTUM_FLOOR = -4
# Finding 4: a label holds until the score falls this far below its threshold.
CAL_HYSTERESIS_BAND = 3.0
# Finding 6: an imminent print, scored only in fresh-money (watchlist) framing —
# "don't initiate into a binary" is a real cost; "sell before the print" is not.
CAL_EARNINGS_BANDS = ((2, -4), (7, -2))    # within N days -> delta

# The full mode key is the base, plus CALIBRATION_SUFFIX when recalibrated
# ("composite", "blend-cal", ...). Every verdict-dependent piece of the report
# is rendered once per key, so both switches are instant in the browser.
BASE_SCORE_MODES = tuple(
    base + (CALIBRATION_SUFFIX if cal == "cal" else "")
    for cal in CALIBRATIONS for base in BASE_SCORE_BASES
)
DEFAULT_BASE_SCORE_MODE = "composite"
BASE_SCORE_LABELS = {
    base + (CALIBRATION_SUFFIX if cal == "cal" else ""):
        BASE_SCORE_BASE_LABELS[base] + ("" if cal == "std"
                                        else f" ({CALIBRATION_LABELS[cal]})")
    for cal in CALIBRATIONS for base in BASE_SCORE_BASES
}


# Compact mode names for the run log's side-by-side, where the full
# "Composite (Recalibrated)" would wrap the table.
def _short_mode_label(mode: str) -> str:
    base, cal = split_base_mode(mode)
    return BASE_SCORE_BASE_LABELS[base] + ("+cal" if cal == "cal" else "")


def split_base_mode(mode: Optional[str]) -> tuple[str, str]:
    """A mode key -> (base, calibration). Unknown keys read as the defaults."""
    raw = (mode or "").strip().lower()
    cal = "std"
    if raw.endswith(CALIBRATION_SUFFIX):
        raw, cal = raw[:-len(CALIBRATION_SUFFIX)], "cal"
    if raw not in BASE_SCORE_BASES:
        raw = "composite"
    return raw, cal


def join_base_mode(base: str, calibration: str) -> str:
    """(base, calibration) -> the mode key. Inverse of split_base_mode."""
    if base not in BASE_SCORE_BASES:
        base = "composite"
    return base + (CALIBRATION_SUFFIX if calibration == "cal" else "")


# The synthetic watchlist group recently-sold names are analyzed under. Not a
# real Robinhood list, so watchlist pruning never touches it.
RECENTLY_HELD_GROUP = "Recently held"

# Finding 5: a name held within this many days stays in the analyzed universe
# even after it leaves every watchlist, so the run keeps scoring it and the
# ledger keeps marking it to market. META left the Screening list twice in
# August 2026 — once at its lowest price of the window — and simply stopped
# being looked at. Disable with --no-pin-recent-holdings.
PIN_RECENT_HOLDINGS_DAYS = 180

# Finding 4 needs the label each name carried last run. Populated from the
# ledger by load_prior_verdict_labels() before any analysis; empty on a first
# run, in lookup mode, and in tests, where hysteresis simply does not apply.
_PRIOR_VERDICT_LABELS: dict[str, dict[str, str]] = {}


def load_prior_verdict_labels(history: Optional[dict]) -> int:
    """Fill the hysteresis store from the ledger's last snapshot per ticker.

    Reads the per-mode labels `_rec_factors` logs; an entry written before that
    logging existed contributes only to the mode the run recorded it under, so
    one base's label can never stand in for another's. Returns how many tickers
    carry a prior label."""
    _PRIOR_VERDICT_LABELS.clear()
    for ticker, entry in ((history or {}).get("tickers") or {}).items():
        stored = ((entry.get("last_factors") or {}).get("verdicts") or {})
        labels = {mode: pair[0] for mode, pair in stored.items()
                  if mode in BASE_SCORE_MODES
                  and isinstance(pair, (list, tuple)) and pair}
        if not labels and entry.get("last_verdict"):
            recorded = (entry.get("last_factors") or {}).get("base_mode")
            if recorded in BASE_SCORE_MODES:
                labels = {recorded: entry["last_verdict"]}
        if labels:
            _PRIOR_VERDICT_LABELS[ticker] = labels
    return len(_PRIOR_VERDICT_LABELS)


def prior_verdict_label(ticker: Optional[str], mode: str) -> Optional[str]:
    """The label `ticker` carried under `mode` last run, or None."""
    if not ticker:
        return None
    return (_PRIOR_VERDICT_LABELS.get(ticker) or {}).get(mode)


def _ramp(x: float, points) -> float:
    """Piecewise-linear interpolation over (x, y) anchors, clamped at both ends.
    Anchors run in increasing x; y may fall (every calibration ramp here does)."""
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            if x <= x0:
                return y0
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]
_base_score_mode_override: Optional[str] = None     # set from --base-score
_warned_base_modes: set[str] = set()


def set_base_score_mode(mode: Optional[str]) -> None:
    """Override the base-score mode for this process (--base-score). None
    clears it, falling back to BASE_SCORE_MODE."""
    global _base_score_mode_override
    _base_score_mode_override = mode


def base_score_mode(saved_only: bool = False) -> str:
    """The active base-score mode: the --base-score override, else the
    BASE_SCORE_MODE env var, else composite. `saved_only` skips the override,
    giving the saved default a one-off run would otherwise mask. An
    unrecognised value falls back to composite (warned once)."""
    raw = "" if saved_only else (_base_score_mode_override or "")
    raw = (raw or os.environ.get("BASE_SCORE_MODE", "")).strip().lower()
    if not raw:
        return DEFAULT_BASE_SCORE_MODE
    if raw not in BASE_SCORE_MODES:
        if raw not in _warned_base_modes:
            _warned_base_modes.add(raw)
            print(f"[base-score] Unknown BASE_SCORE_MODE {raw!r}; "
                  f"using {DEFAULT_BASE_SCORE_MODE}.")
        return DEFAULT_BASE_SCORE_MODE
    return raw


@dataclass
class Verdict:
    label: str       # SELL, TRIM, HOLD, ADD, BUY MORE
    color: str       # CSS color for HTML
    reason: str
    score: Optional[float] = None    # 0-100 numerical verdict score (v2 only)
    # Data-coverage confidence (v2 only): `coverage` is the fraction of the
    # base score's weight that actually had data (0-1); `confidence` is the
    # bucketed label (High/Medium/Low) shown on the verdict card.
    coverage: Optional[float] = None
    confidence: Optional[str] = None
    # Base-score mode the verdict was built on (v2 only), and what the other
    # modes would have said — {mode: (label, score)} — so the hover card and
    # the run log can compare them before anyone switches.
    base_mode: Optional[str] = None
    alternates: Optional[dict] = None


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _value_sub_score(pe: Optional[float], peg: Optional[float],
                     *, calibrated: bool) -> Optional[float]:
    """The composite's 0-100 value sub-score from P/E and PEG.

    Standard scoring maps P/E 10 -> 100 and 30 -> 0, PEG 0 -> 100 and 2 -> 0.
    Both are deep-value ramps: they hit zero exactly at the thresholds the nine
    quality filters treat as a PASS, so a name the filters cleared on valuation
    could still score 0 here. Finding 2 rescales both so the gate scores 50 —
    passing means average, not worthless — and lets a genuinely cheap multiple
    reach 100 (see CAL_VALUE_PE_RAMP / CAL_VALUE_PEG_RAMP).

    Missing or non-positive inputs drop out; None when neither is usable."""
    parts = []
    if pe is not None and pe > 0:
        parts.append(_ramp(pe, CAL_VALUE_PE_RAMP) if calibrated
                     else _clip01((30 - pe) / 20) * 100)
    if peg is not None and peg > 0:
        parts.append(_ramp(peg, CAL_VALUE_PEG_RAMP) if calibrated
                     else _clip01((2 - peg) / 2) * 100)
    return round(sum(parts) / len(parts), 1) if parts else None


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
    pe, _, _ = _pe_values(info)   # lower of trailing/forward (see _pe_values)
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

    # Value (20%): P/E, PEG — valuation *level* only.
    # NOTE: upside-to-analyst-target is deliberately NOT folded in here. It's a
    # forward / sell-side view that also interacts with trend ("analysts may be
    # lagging"), so it lives in the verdict layer (compute_verdict_v2) instead.
    # Keeping it out of the composite means valuation-vs-target is counted once
    # (in the verdict) rather than once here and again there — see the
    # no-double-counting note in compute_verdict_v2.
    pa.score_value = _value_sub_score(pe, peg, calibrated=False)
    pa.score_value_cal = _value_sub_score(pe, peg, calibrated=True)

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
    # Built once per calibration: the standard scoring, and the recalibrated
    # one, which swaps in the rescaled value sub-score and drops a
    # no-information insider read entirely (see CALIBRATIONS). Both go through
    # the same renormalize-over-available-weight rule, so a dropped insider
    # score is handled exactly like an ADR with no coverage.
    for cal, value_score, insider_score_v in (
        ("std", pa.score_value, pa.score_insider),
        ("cal", pa.score_value_cal, pa.score_insider_cal),
    ):
        weighted = (
            ("score_quality", pa.score_quality, 0.30),
            ("score_growth", pa.score_growth, 0.20),
            ("score_value", value_score, 0.20),
            ("score_analyst", pa.score_analyst, 0.15),
            ("score_insider", insider_score_v, 0.15),
        )
        weighted_sum = sum(v * w for _, v, w in weighted if v is not None)
        weight_total = sum(w for _, v, w in weighted if v is not None)
        if weight_total <= 0:
            continue
        score = round(weighted_sum / weight_total, 1)
        # weight_total is the share of the (normalized-to-1.0) weighting that
        # actually had data — i.e. how complete the fundamental picture is.
        # Drives the verdict's confidence label / modifier dampening.
        coverage = round(weight_total, 3)
        if cal == "std":
            pa.composite_score, pa.composite_coverage = score, coverage
        else:
            pa.composite_score_cal, pa.composite_coverage_cal = score, coverage


def compute_quality_base(pa) -> None:
    """Populate the `quality` base-score mode's inputs on `pa`.

    filter_score / filter_coverage: the nine filters as a soft-gated 0-100
    score and the share that had data. quality_base / quality_coverage: that
    score at 70% plus the analyst and insider sub-scores at 15% each (the
    weights they carry in the composite), re-normalized over what's available
    like the composite is. Coverage scales the filters' 70% by their data
    share, so a name missing two filters reads as less complete. Needs
    compute_composite_score first — it fills score_analyst.
    """
    pa.filter_score, pa.filter_coverage = compute_filter_score(pa.filters)
    for cal, insider_score_v in (("std", pa.score_insider),
                                 ("cal", pa.score_insider_cal)):
        parts = (
            ("filters", pa.filter_score, pa.filter_coverage),
            ("analyst", pa.score_analyst, 1.0),
            ("insider", insider_score_v, 1.0),
        )
        weighted_sum = weight_total = coverage = 0.0
        for key, val, data_share in parts:
            if val is None:
                continue
            w = QUALITY_BASE_WEIGHTS[key]
            weighted_sum += val * w
            weight_total += w
            coverage += w * data_share
        if weight_total <= 0:
            continue
        base, cov = round(weighted_sum / weight_total, 1), round(coverage, 3)
        if cal == "std":
            pa.quality_base, pa.quality_coverage = base, cov
        else:
            pa.quality_base_cal, pa.quality_coverage_cal = base, cov


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


# Verdict-score bands, strongest first: (minimum score, label, color).
# Holdings carry a stay-the-course bias — selling has tax friction, so the bar
# to leave is higher and HOLD covers a wide middle. Watchlist names need
# fresh-money conviction to reach BUY.
VERDICT_BANDS_HOLDING = (
    (78.0, "ADD", "#27ae60"),      # strong conviction add
    (60.0, "HOLD", "#2c3e50"),     # stay the course
    (50.0, "HOLD", "#7f8c8d"),     # weak HOLD (muted gray)
    (28.0, "TRIM", "#e67e22"),     # below neutral (~50) — lean out
    (0.0, "SELL", "#c0392b"),
)
VERDICT_BANDS_WATCHLIST = (
    (75.0, "BUY", "#27ae60"),
    (60.0, "WATCH", "#2980b9"),    # interesting, not yet
    (42.0, "WAIT", "#7f8c8d"),     # neutral
    (0.0, "PASS", "#c0392b"),
)


def _verdict_label(score: float, is_holding: bool, *,
                   prior_label: Optional[str] = None,
                   hysteresis: float = 0.0) -> tuple[str, str, Optional[str]]:
    """Map a verdict score to (label, color, hysteresis note).

    With `hysteresis` and a `prior_label`, a label keeps the ground it has
    already taken until the score falls clear of its band by that margin.
    Finding 4: the bands are crossed by a score built from step functions, so a
    name could read BUY one day and WATCH the next on a 1-2% price move — the
    call existed but never lasted long enough to act on. The margin applies in
    one direction only: earning a stronger label still takes a clean crossing,
    so this adds patience without lowering the bar."""
    bands = VERDICT_BANDS_HOLDING if is_holding else VERDICT_BANDS_WATCHLIST
    idx = next(i for i, (lo, _, _) in enumerate(bands) if score >= lo)
    if hysteresis > 0 and prior_label:
        # The score a name had to reach to earn the label it is holding.
        prior_idx = next((i for i, (_, lab, _) in enumerate(bands)
                          if lab == prior_label), None)
        if (prior_idx is not None and prior_idx < idx
                and score >= bands[prior_idx][0] - hysteresis):
            lo, lab, color = bands[prior_idx]
            return lab, color, (f"{lab} held — within {hysteresis:.0f} points "
                                f"of the {lo:.0f} bar it already cleared")
    return bands[idx][1], bands[idx][2], None


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
    news_signal: Optional[dict] = None,
    coverage: Optional[float] = None,
    base_mode: str = DEFAULT_BASE_SCORE_MODE,
    quality_base: Optional[float] = None,
    quality_coverage: Optional[float] = None,
    filter_score: Optional[float] = None,
    score_analyst: Optional[float] = None,
    score_insider: Optional[float] = None,
    days_to_earnings: Optional[int] = None,
    prior_label: Optional[str] = None,
) -> Verdict:
    """
    Evidence-weighted verdict logic.

    Synthesizes ALL available signals into a single "verdict score" (0-100).
    It starts from a fundamentals base chosen by `base_mode` (see
    BASE_SCORE_MODES): the Composite Score (quality, growth, valuation *level*,
    analyst quality, insider activity), the quality base (the nine filters plus
    the same analyst and insider sub-scores), or the blend of the two. On top
    of it we apply small modifiers ONLY for signals the base does NOT already
    contain: trend, sector momentum, 52-week position, upside-to-analyst-target,
    recent news, and portfolio concentration.

    `coverage` is the composite's data coverage; `quality_coverage` the quality
    base's. The mode picks which one drives confidence. `filter_score`,
    `score_analyst` and `score_insider` only label the quality base's parts in
    the breakdown.

    No double-counting: quality, analyst and insider already live in every base
    (as rich continuous sub-scores), so they are NOT re-applied as modifiers
    here. Upside-to-target is the opposite case — it is deliberately kept OUT of
    the base and applied here instead, where it can interact with trend. Each
    factor therefore influences the final score exactly once.

    Holdings (is_holding=True) use SELL/TRIM/HOLD/ADD vocabulary with
    "stay-the-course" bias — selling has tax friction so the bar is higher.
    Watchlist items (is_holding=False) use SELL/PASS/WAIT/WATCH/BUY where
    BUY requires a fresh-money commitment.

    Returns a Verdict with `reason` containing a transparent breakdown of
    every contributing factor (+5 for hot sector, -10 for downtrend, etc.).
    Hovering the verdict pill surfaces the full breakdown.
    """
    # ---- Base (0-100), built the way base_mode says ----
    # A mode key carries two independent choices: which base the verdict starts
    # from, and which calibration of the sub-scores and modifiers it uses. The
    # caller has already handed us the calibration's sub-scores; `calibrated`
    # below decides how the context modifiers behave.
    if base_mode not in BASE_SCORE_MODES:
        base_mode = DEFAULT_BASE_SCORE_MODE
    base_name, calibration = split_base_mode(base_mode)
    calibrated = calibration == "cal"
    base_value, coverage, base_line = _verdict_base(
        base_name, calibrated=calibrated,
        composite_score=composite_score, composite_coverage=coverage,
        quality_base=quality_base, quality_coverage=quality_coverage,
        filters=filters, filter_score=filter_score,
        score_analyst=score_analyst, score_insider=score_insider,
    )
    # Nothing to build the base from — fall back to mid-neutral.
    base = 50.0 if base_value is None else float(base_value)
    # 0 marker: the first entry just shows the base in the breakdown.
    contributors: list[tuple[str, float]] = [(base_line, 0)]

    # Score that we'll modify
    score = base

    # ---- Quality filter pass/fail count (NOT re-scored here) ----
    # Quality already drives every base (score_quality is 30% of the
    # composite, built from the same ROE / margin / leverage / FCF inputs as
    # these gates; the quality base scores the gates themselves), so
    # re-penalizing a quality miss here would double-count it. We still tally
    # passed/failed because the 52-week-position logic below uses the count to
    # distinguish a value entry from a falling knife.
    passed = None
    failed = 0
    if filters:
        passed = sum(1 for f in filters if f.passed)
        failed = len(filters) - passed

    # ---- Price-vs-own-range axis: who owns it ----
    # MA trend and 52-week position measure the SAME underlying variable —
    # where price sits relative to its own recent history. Across the tracked
    # universe they correlate at ~0.91: a stock at its 52-week low is *by
    # construction* below its 200d MA. Scoring both double-counts one
    # observation, and because the trend swing (±10) is twice the 52-week swing
    # (±5) and carries the opposite sign at the extremes, the blunt rule
    # silently vetoed the specific one — a quality name at a 52-week low earned
    # +5 for "intact fundamentals at a low" and then gave back -10 for
    # "downtrend", netting -5 for the exact setup the rule exists to find.
    #
    # Resolution follows the same no-double-counting doctrine already applied to
    # insider activity and analyst targets above: the more specific rule wins.
    # 52-week position is quality-gated, so it can distinguish a value entry
    # from a falling knife; trend cannot. When price is at either extreme of its
    # range, the 52-week rule owns the axis and the trend modifier stands down.
    # In the broad middle (~20-92% of range) — most of the universe — nothing
    # changes and trend scores as before.
    week52_extreme = (week52_position is not None
                      and (week52_position <= 20 or week52_position >= 92))

    # ---- Trend (50d MA / 200d MA alignment) ----
    # trend_scored records whether this block actually moved the score. The
    # recalibrated momentum credit below reads it so the two never stack: they
    # measure the same axis, and the doctrine above is that it is scored once.
    trend_scored = False
    if week52_extreme and trend in ("uptrend", "downtrend"):
        # Deferred, but keep it visible in the breakdown so the hover still
        # explains the full reasoning rather than silently dropping a factor.
        contributors.append(
            (f"{trend.capitalize()} not re-scored — already counted as "
             f"52-week position ({week52_position:.0f}% of range)", 0))
    elif trend == "uptrend":
        bonus = 10
        if pct_above_ma200 is not None and pct_above_ma200 >= 25:
            bonus = 12  # particularly strong uptrend
        score += bonus
        trend_scored = True
        contributors.append((f"Uptrend"
                              + (f" (+{pct_above_ma200:.0f}% vs 200d)"
                                 if pct_above_ma200 is not None else ""), +bonus))
    elif trend == "downtrend":
        score -= 10
        trend_scored = True
        contributors.append((f"Downtrend"
                              + (f" ({pct_above_ma200:+.0f}% vs 200d)"
                                 if pct_above_ma200 is not None else ""), -10))
    # sideways: no adjustment

    # ---- Insider activity (NOT re-scored here) ----
    # Insider buying/selling already feeds every base (score_insider is 15% of
    # it, as a rich continuous 0-100 built from buy/sell dollar volume).
    # The old coarse ±8 bucket here re-applied the same signal, so it's removed
    # to avoid double-counting. (insider_signal stays in the signature for
    # backward compatibility with callers.)

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
    # Finding 3a: the standard bands are a step function, and the steps are
    # large enough that a 1% price move can swing the verdict 6-9 points as
    # upside crosses 20% or 10%. A name that is working walks *down* the steps
    # — its price rises into a target that has not moved — so the model de-rates
    # it for succeeding. The recalibrated form keeps the same +/-6 authority but
    # spends it continuously, so the same 1% move is worth ~0.3 points.
    if calibrated and upside_pct is not None:
        delta = round(max(-CAL_UPSIDE_MAX,
                          min(CAL_UPSIDE_MAX,
                              upside_pct / CAL_UPSIDE_FULL_PCT * CAL_UPSIDE_MAX)), 1)
        if delta:
            score += delta
            direction = "upside to" if delta > 0 else "premium to"
            contributors.append(
                (f"{direction.capitalize()} target ({upside_pct:+.0f}%)", delta))
    elif upside_pct is not None:
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

    # ---- Realized strength vs the 200-day line (recalibrated only) ----
    # Finding 3b: the standard scoring rewards strength only through the trend
    # bucket, which needs the 50-day above the 200-day. A name recovering from
    # a drawdown can be well clear of its 200-day for months while the 50-day
    # is still catching up — it reads "sideways" and earns nothing, even as it
    # makes new highs. This fills exactly that gap, and only that gap: when the
    # trend modifier already scored this axis, it stands down rather than
    # stacking a second reading of the same observation on top.
    if calibrated and pct_above_ma200 is not None and not week52_extreme:
        if trend_scored:
            contributors.append(
                (f"Momentum not re-scored — trend already counted "
                 f"{pct_above_ma200:+.0f}% vs 200d", 0))
        else:
            delta = CAL_MOMENTUM_FLOOR
            for floor, band_delta in CAL_MOMENTUM_BANDS:
                if pct_above_ma200 >= floor:
                    delta = band_delta
                    break
            if delta:
                score += delta
                held = "above" if delta > 0 else "below"
                contributors.append(
                    (f"Price holding {held} its 200-day "
                     f"({pct_above_ma200:+.0f}%)", delta))

    # ---- Earnings proximity (recalibrated only) ----
    # Finding 6: days_to_earnings was computed and rendered as a footnote the
    # verdict never read. It is scored in fresh-money framing only — committing
    # new capital the day before a print is a real, avoidable cost, while
    # "sell ahead of the print" is not a call this model should be making. For
    # a holding it stays an annotation, so the reasoning shows the event
    # without the score leaning on it.
    if calibrated and days_to_earnings is not None and days_to_earnings >= 0:
        when = ("today" if days_to_earnings == 0
                else "tomorrow" if days_to_earnings == 1
                else f"in {days_to_earnings} days")
        if is_holding:
            if days_to_earnings <= EARNINGS_SOON_DAYS:
                contributors.append(
                    (f"Reports {when} — event risk noted, not scored "
                     f"(holding)", 0))
        else:
            for within, delta in CAL_EARNINGS_BANDS:
                if days_to_earnings <= within:
                    score += delta
                    contributors.append(
                        (f"Reports {when} — hold fresh money past the print",
                         delta))
                    break

    # ---- Latest-news sentiment (bounded ±6 nudge) ----
    _news_mod = _news_signal_modifier(news_signal)
    if _news_mod:
        _nd, _ndesc = _news_mod
        score += _nd
        contributors.append((_ndesc, _nd))

    # ---- Confidence dampening (thin data coverage) ----
    # When the base was built from only a few inputs, the context modifiers
    # above can swing a poorly-supported base too far (e.g. push a 2-input
    # composite to a strong BUY on momentum alone). Shrink the *net modifier*
    # toward neutral in proportion to how complete the data is. The base itself
    # is left untouched — only our confidence in the context tilt drops.
    # `coverage` is the fraction of the base's weight that had data (0-1);
    # >=0.85 (essentially every input) keeps full strength.
    confidence = None
    if coverage is not None:
        if coverage >= 0.85:
            confidence = "High"
        else:
            confidence = "Medium" if coverage >= 0.55 else "Low"
            conf_factor = max(0.45, min(1.0, coverage / 0.85))
            damp_delta = round((score - base) * (conf_factor - 1.0))  # toward base
            if damp_delta != 0:
                score += damp_delta
                contributors.append(
                    (f"{confidence} confidence: built from "
                     f"{coverage * 100:.0f}% of inputs — signals dampened",
                     damp_delta))

    # ---- Clamp to 0-100 ----
    score = max(0.0, min(100.0, score))

    # ---- Map to verdict label ----
    label, color, hyst_note = _verdict_label(
        score, is_holding, prior_label=prior_label,
        hysteresis=CAL_HYSTERESIS_BAND if calibrated else 0.0)
    if hyst_note:
        contributors.append((hyst_note, 0))

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
    # Zero-impact entries after the base line are informational notes — a factor
    # we deliberately did NOT score (e.g. trend standing down because 52-week
    # position already covers that axis). They carry no delta but they explain
    # an absence, so the breakdown would be misleading without them.
    for desc, delta in contributors[1:]:
        if delta == 0:
            breakdown_lines.append(f"· {desc}")
    breakdown_lines.append(f"= verdict score {score:.0f}")
    reason = headline + " | " + " | ".join(breakdown_lines)

    return Verdict(label=label, color=color, reason=reason, score=round(score, 1),
                   coverage=coverage, confidence=confidence, base_mode=base_mode)


def _verdict_base(
    mode: str,
    *,
    calibrated: bool = False,
    composite_score: Optional[float],
    composite_coverage: Optional[float],
    quality_base: Optional[float],
    quality_coverage: Optional[float],
    filters: Optional[list],
    filter_score: Optional[float],
    score_analyst: Optional[float],
    score_insider: Optional[float],
) -> tuple[Optional[float], Optional[float], str]:
    """Pick compute_verdict_v2's starting score for a base-score mode.

    Returns (base, coverage, breakdown line). base is None when the mode has
    nothing to build from; the line is the first row of the hover breakdown
    (the card parser takes the first non-±N segment as the base). Anything but
    quality/blend is treated as composite. `calibrated` only labels the line —
    the caller has already passed the matching calibration's values — so the
    hover never shows two different numbers under the same name."""
    tag = " (recalibrated)" if calibrated else ""
    if mode == "quality":
        if quality_base is None:
            return None, quality_coverage, "Quality base unavailable, neutral baseline"
        detail = _quality_base_detail(filters, filter_score, score_analyst,
                                      score_insider, calibrated=calibrated)
        return (quality_base, quality_coverage,
                f"Quality base{tag} {quality_base:.0f} — {detail}")

    if mode == "blend":
        sides = [(name, val, cov) for name, val, cov in (
            ("composite", composite_score, composite_coverage),
            ("quality", quality_base, quality_coverage),
        ) if val is not None]
        if not sides:
            return None, None, "Blend base unavailable, neutral baseline"
        base = round(sum(val for _, val, _ in sides) / len(sides), 1)
        covs = [cov for _, _, cov in sides if cov is not None]
        coverage = round(sum(covs) / len(covs), 3) if covs else None
        if len(sides) == 2:
            detail = f"composite {composite_score:.0f}, quality {quality_base:.0f}"
        else:
            name, val, _ = sides[0]
            missing = "quality" if name == "composite" else "composite"
            detail = f"{name} {val:.0f} only ({missing} unavailable)"
        return base, coverage, f"Blend base{tag} {base:.0f} — {detail}"

    if composite_score is None:
        return None, composite_coverage, "Composite Score unavailable, neutral baseline"
    base = float(composite_score)
    return base, composite_coverage, f"Composite Score{tag} {base:.0f}"


def _quality_base_detail(filters: Optional[list], filter_score: Optional[float],
                         score_analyst: Optional[float],
                         score_insider: Optional[float],
                         calibrated: bool = False) -> str:
    """The parts of a quality base for its breakdown line, e.g.
    'filters 82 (7/9 pass), analyst 70, insider 48'."""
    filters = filters or []
    if filter_score is None:
        parts = ["filters n/a"]
    else:
        passed = sum(1 for f in filters if f.passed)
        note = f"{passed}/{len(filters)} pass"
        no_data = sum(1 for f in filters if f.credit is None)
        if no_data:
            note += f", {no_data} without data"
        parts = [f"filters {filter_score:.0f} ({note})"]
    parts.append("analyst n/a" if score_analyst is None else f"analyst {score_analyst:.0f}")
    if score_insider is not None:
        parts.append(f"insider {score_insider:.0f}")
    else:
        # Under the recalibration a missing insider score is a decision, not a
        # gap: the filing history carried no directional information, so the
        # weight renormalized away rather than scoring a misleading number.
        parts.append("insider not scored (no signal)" if calibrated
                     else "insider n/a")
    return ", ".join(parts)


def _insider_signal(pa) -> Optional[str]:
    """Coarse insider bucket for compute_verdict_v2's `insider_signal` (kept for
    its signature; the verdict itself scores insiders only through the base)."""
    if not pa.insider_activity:
        return None
    sig = pa.insider_activity.get("net_signal", "")
    if sig == "Buying":
        return "supports_buy"
    if sig == "Selling" and pa.score_insider is not None and pa.score_insider <= 35:
        return "caution"
    return "no_signal"


def _calibration_inputs(pa, calibration: str) -> dict:
    """The sub-scores and bases a calibration scores from.

    Every calibration reads the same filters, analyst score and price factors;
    only the value sub-score, the insider sub-score, and the bases built from
    them differ (see CALIBRATIONS). Both sets live on `pa` after
    compute_composite_score, so one run scores both without re-fetching."""
    if calibration == "cal":
        return {"composite_score": pa.composite_score_cal,
                "coverage": pa.composite_coverage_cal,
                "quality_base": pa.quality_base_cal,
                "quality_coverage": pa.quality_coverage_cal,
                "score_insider": pa.score_insider_cal}
    return {"composite_score": pa.composite_score,
            "coverage": pa.composite_coverage,
            "quality_base": pa.quality_base,
            "quality_coverage": pa.quality_coverage,
            "score_insider": pa.score_insider}


def has_verdict_base(pa, mode: str) -> bool:
    """Whether `pa` has what `mode` builds its base from."""
    base, calibration = split_base_mode(mode)
    vals = _calibration_inputs(pa, calibration)
    if base == "quality":
        return vals["quality_base"] is not None
    if base == "blend":
        return (vals["composite_score"] is not None
                or vals["quality_base"] is not None)
    return vals["composite_score"] is not None


def _verdict_v2_under(pa, mode: str, is_holding: bool,
                      position_pct: Optional[float]) -> Optional[Verdict]:
    if not has_verdict_base(pa, mode):
        return None
    _, calibration = split_base_mode(mode)
    vals = _calibration_inputs(pa, calibration)
    return compute_verdict_v2(
        composite_score=vals["composite_score"],
        filters=pa.filters,
        current_price=pa.current_price,
        target_price=pa.target_mean,
        upside_pct=pa.upside_pct,
        trend=pa.trend,
        pct_above_ma200=pa.pct_above_ma200,
        week52_position=pa.week52_position,
        sector_label=(pa.sector_momentum or {}).get("label"),
        insider_signal=_insider_signal(pa),
        position_pct_portfolio=position_pct,
        is_holding=is_holding,
        news_signal=pa.news_sentiment,
        coverage=vals["coverage"],
        base_mode=mode,
        quality_base=vals["quality_base"],
        quality_coverage=vals["quality_coverage"],
        filter_score=pa.filter_score,
        score_analyst=pa.score_analyst,
        score_insider=vals["score_insider"],
        days_to_earnings=pa.days_to_earnings,
        prior_label=prior_verdict_label(pa.ticker, mode),
    )


def verdicts_v2_for(pa, *, is_holding: bool,
                    position_pct: Optional[float] = None) -> dict[str, Verdict]:
    """The v2 verdict for a position under every base-score mode that has a
    base to start from, each with `alternates` holding the others' label and
    score. `position_pct` is the live portfolio weight, known only once
    finalize_holding_verdicts has run."""
    verdicts = {}
    for mode in BASE_SCORE_MODES:
        verdict = _verdict_v2_under(pa, mode, is_holding, position_pct)
        if verdict is not None:
            verdicts[mode] = verdict
    for mode, verdict in verdicts.items():
        verdict.alternates = {other: (v.label, v.score)
                              for other, v in verdicts.items() if other != mode}
    return verdicts


def set_v2_verdicts(pa, *, is_holding: bool,
                    position_pct: Optional[float] = None) -> None:
    """Score `pa` under every mode. pa.verdicts keeps them all for the report's
    instant base switch; pa.verdict is the run's mode — what watchlist pruning,
    tax flags and the history ledger act on."""
    pa.verdicts = verdicts_v2_for(pa, is_holding=is_holding,
                                  position_pct=position_pct)
    pa.verdict = pa.verdicts.get(base_score_mode(), pa.verdict)


def verdict_in(r, mode: Optional[str]) -> Optional[Verdict]:
    """r's verdict under `mode`. Falls back to the run's verdict for mode None
    and for positions without per-mode verdicts (ETF/thematic, fallback
    verdicts), whose verdict doesn't depend on the base."""
    if mode is None:
        return r.verdict
    return (getattr(r, "verdicts", None) or {}).get(mode) or r.verdict


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
    # Today's move (regular session): per-share $ + %, and prior close
    prev_close: Optional[float] = None
    day_change: Optional[float] = None          # per-share $ change today
    day_change_pct: Optional[float] = None      # % change today
    # Extended-hours move (after-hours / pre-market): the current extended
    # price vs the regular-session price. Only populated when the market is
    # closed and an extended price is actually in use.
    regular_market_price: Optional[float] = None   # regular-session price (close/last)
    after_hours_change: Optional[float] = None      # per-share $ change in extended session
    after_hours_change_pct: Optional[float] = None  # % vs regular price
    extended_session: Optional[str] = None          # "post" | "pre" | None
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
    # Fraction of the composite's weight (0-1) that had data behind it — a
    # data-completeness measure used to express verdict confidence.
    composite_coverage: Optional[float] = None
    # `quality` base-score mode inputs (compute_quality_base): the nine filters
    # as a soft-gated 0-100 score + the share that had data, and the mode's
    # base (filters 70%, analyst 15%, insider 15%) + its coverage.
    filter_score: Optional[float] = None
    filter_coverage: Optional[float] = None
    quality_base: Optional[float] = None
    quality_coverage: Optional[float] = None
    # Recalibrated twins of the two sub-scores the calibration changes, and of
    # every base built from them (see CALIBRATIONS). Held alongside the
    # standard values rather than replacing them so one run can score both and
    # the report's Scoring switch needs no refresh. score_insider_cal is None
    # whenever the insider read carries no information — that is the point of
    # finding 1, not missing data.
    score_value_cal: Optional[float] = None
    score_insider_cal: Optional[float] = None
    composite_score_cal: Optional[float] = None
    composite_coverage_cal: Optional[float] = None
    quality_base_cal: Optional[float] = None
    quality_coverage_cal: Optional[float] = None
    # Next earnings report (event-risk timing): ISO date + days from today.
    # days_to_earnings is forward-only (None once a report is in the past).
    next_earnings_date: Optional[str] = None
    days_to_earnings: Optional[int] = None
    # Insider activity (raw data for display)
    insider_activity: Optional[dict] = None
    # Latest-news sentiment from Claude: {score, label, rationale, headlines, as_of}
    news_sentiment: Optional[dict] = None
    # Output
    verdict: Optional[Verdict] = None
    # v2 verdict under every base-score mode ({mode: Verdict}); `verdict` is
    # the run's mode. Empty for ETF/thematic and fallback verdicts.
    verdicts: dict = field(default_factory=dict)
    error: Optional[str] = None
    # Holding period / tax
    position_opened: Optional[str] = None     # ISO date string or None
    tax: Optional[object] = None              # TaxAnalysis (set post-hoc)


def _extract_next_earnings(info: dict) -> tuple[Optional[str], Optional[int]]:
    """Best-effort *next* earnings date from yfinance `info` (no extra network
    call — these timestamps already ride along with the info we fetched).

    Returns (iso_date, days_from_today). Only forward-looking dates (today or
    later) count as "next earnings"; a stale past timestamp (already reported)
    yields (None, None) so it neither flags nor filters. Picks the soonest
    future date across the available earnings-timestamp fields.
    """
    today = datetime.now(ZoneInfo("America/New_York")).date()
    best = None
    for key in ("earningsTimestampStart", "earningsTimestamp", "earningsTimestampEnd"):
        ts = info.get(key)
        if not ts:
            continue
        try:
            d = datetime.fromtimestamp(int(ts), ZoneInfo("America/New_York")).date()
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if d >= today and (best is None or d < best):
            best = d
    if best is None:
        return None, None
    return best.isoformat(), (best - today).days


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
        # Cache-aware: a warm 24h cache skips the two heaviest statement fetches
        # (income_stmt + balance_sheet) since these metrics only change at
        # earnings, not intraday (see _compute_growth_cached).
        _compute_growth_cached(tkr, info, ticker)

        pa.bucket = classify_position(ticker, info)
        # Next-earnings timing (event risk) — drives the header "Earnings soon"
        # stat/filter and the verdict-card footer note.
        pa.next_earnings_date, pa.days_to_earnings = _extract_next_earnings(info)
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

        # Broker-accurate after-hours override: when the regular session is
        # closed, prefer Robinhood's extended-hours last trade (populated by
        # main() only in --source robinhood mode while the market is closed).
        # Bounded vs the yfinance regular price (±30%) to reject bad ticks; if
        # yfinance has no price, trust the broker value outright.
        _rh_px = _RH_EXTENDED_PRICES.get(ticker)
        if _rh_px and _rh_px > 0 and (
                _regular is None or _sane_extended(_rh_px, _regular)):
            pa.current_price = _rh_px

        # After-hours / pre-market move: the extended price now in use vs the
        # regular-session price. Non-None only when the market is closed and an
        # extended price differs from the regular close (during regular hours
        # current_price == _regular, so this stays None and the header tile is
        # hidden). Session is inferred from which extended price is in use.
        pa.regular_market_price = _regular
        if (_regular and _regular > 0 and pa.current_price is not None
                and abs(pa.current_price - _regular) > 1e-9):
            pa.after_hours_change = pa.current_price - _regular
            pa.after_hours_change_pct = pa.after_hours_change / _regular * 100
            pa.extended_session = ("pre" if (_pre and abs(pa.current_price - _pre) < 1e-9)
                                   else "post")

        # Compute LIVE market value from live price × shares
        if pa.current_price is not None:
            pa.live_market_value = pa.current_price * pa.shares

        # Today's move — regular-session change vs prior close. Computed from
        # change/prevClose (unambiguous) rather than regularMarketChangePercent
        # (which yfinance returns inconsistently as fraction vs percent).
        pa.prev_close = (_safe_get(info, "regularMarketPreviousClose")
                         or _safe_get(info, "previousClose"))
        _chg = _safe_get(info, "regularMarketChange")
        if _chg is None and pa.prev_close and _regular:
            _chg = _regular - pa.prev_close
        pa.day_change = _chg
        if pa.prev_close and pa.prev_close > 0 and _chg is not None:
            pa.day_change_pct = _chg / pa.prev_close * 100

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
            # The recalibrated read: identical except that a no-information
            # outcome (below-noise selling, RSU withholding, compensation only)
            # comes back as None so the weight renormalizes away — finding 1.
            pa.score_insider_cal = insider_score(
                pa.insider_activity, market_cap=market_cap,
                neutral_as_missing=True)
            # Stash the score on the activity dict so the renderer can use it
            # to decide between "Caution" and "No signal" for selling cases.
            if pa.insider_activity is not None:
                pa.insider_activity["_score"] = pa.score_insider
        except Exception as e:
            print(f"[insider] {ticker}: {e}")

        # Composite scoring (now includes insider as 5th sub-score)
        compute_composite_score(pa, info)
        # Quality base-score mode (nine filters + the analyst/insider above)
        if pa.bucket == "compounder":
            compute_quality_base(pa)

        # ---- Evidence-weighted v2 verdict (replaces the preliminary one above) ----
        # Run for compounders (both held and watchlist). The v2 logic uses every
        # available signal — the base score (see BASE_SCORE_MODES), trend,
        # sector, valuation, 52-week position, news — to produce a single
        # weighted verdict with full transparency in the reason text.
        if pa.bucket == "compounder" and has_verdict_base(pa, base_score_mode()):
            # Latest-news sentiment: Claude when ANTHROPIC_API_KEY is set, else
            # a free headline lexicon (no key/cost). Cached on disk so warm runs
            # re-fetch nothing. Disable entirely with NEWS_SIGNAL=0.
            pa.news_sentiment = score_news_sentiment(ticker, pa.name)
            set_v2_verdicts(pa, is_holding=not is_watchlist)

    except Exception as e:
        pa.error = f"{type(e).__name__}: {e}"
        pa.verdict = Verdict(label="ERROR", color="#7f8c8d", reason=pa.error)

    return pa


# ============================================================
# Parallel position analysis
# ============================================================
# analyze_position() is ~99% network waiting (yfinance info/financials,
# analyst ratings, SEC insider filings), so a thread pool turns the
# 4-5s-per-ticker sequential loop into a near-constant-time batch.
# Module caches touched by workers (_SECTOR_MOMENTUM_CACHE, _RATINGS_CACHE,
# _INSIDER_CACHE, _CIK_CACHE) are plain dicts: GIL-atomic get/set, and a
# race only costs a duplicated fetch. Default worker count stays moderate
# because SEC EDGAR allows ~10 req/s and Yahoo rate-limits aggressive bursts.

class _ThreadOutputRouter:
    """stdout proxy that diverts print() output to a thread-local buffer.

    Workers run analyze_position concurrently, but its progress prints
    would interleave unreadably. Each worker pushes a buffer, and the
    coordinator prints the collected block when the ticker completes.
    Threads without an active buffer (e.g. the main thread) write through.
    """

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    def push(self) -> None:
        self._local.buf = io.StringIO()

    def pop(self) -> str:
        buf = getattr(self._local, "buf", None)
        self._local.buf = None
        return buf.getvalue() if buf is not None else ""

    def write(self, s):
        buf = getattr(self._local, "buf", None)
        return (buf if buf is not None else self._real).write(s)

    def flush(self):
        buf = getattr(self._local, "buf", None)
        if buf is None:
            self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


def analyze_positions_parallel(
    rows: list[dict],
    use_robinhood_ratings: bool = False,
    is_watchlist: bool = False,
    max_workers: Optional[int] = None,
    log_fn=None,
    batch_news: bool = True,
) -> list[PositionAnalysis]:
    """Run analyze_position over rows concurrently, preserving input order.

    Progress is reported per completed ticker (to stdout, and to log_fn when
    given — used by server.py to stream into the dashboard log panel). A
    ticker whose analysis errored is retried once after a short pause, which
    absorbs transient Yahoo rate-limit hiccups.

    batch_news pre-scores news sentiment through the half-price Batches API,
    which can block for minutes. Right for a scheduled run; pass False from
    anything a person is waiting on (see server.py).
    """
    if max_workers is None:
        try:
            max_workers = int(os.environ.get("ANALYZE_MAX_WORKERS", "6"))
        except ValueError:
            max_workers = 6
    max_workers = max(1, min(max_workers, len(rows) or 1))

    # Score news for every cache-missing ticker in one half-price batch before
    # the loop starts, so the per-ticker calls below are cache hits. No-op
    # without an API key, with NEWS_BATCH=0, or when everything is still fresh.
    if batch_news:
        try:
            prewarm_news_sentiment(rows)
        except Exception as e:
            print(f"[news-batch] pre-pass skipped ({type(e).__name__}: {e})")

    total = len(rows)
    results: list[Optional[PositionAnalysis]] = [None] * total

    def work(row: dict) -> tuple[PositionAnalysis, str, float]:
        t0 = time.time()
        router.push()
        try:
            pa = analyze_position(row, use_robinhood_ratings=use_robinhood_ratings,
                                  is_watchlist=is_watchlist)
            if pa.error:
                time.sleep(2)  # transient rate limits usually clear quickly
                retry = analyze_position(row, use_robinhood_ratings=use_robinhood_ratings,
                                         is_watchlist=is_watchlist)
                if not retry.error:
                    pa = retry
        finally:
            captured = router.pop()
        return pa, captured, time.time() - t0

    router = _ThreadOutputRouter(sys.stdout)
    old_stdout, sys.stdout = sys.stdout, router
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(work, row): i for i, row in enumerate(rows)}
            for fut in as_completed(futures):
                i = futures[fut]
                pa, captured, elapsed = fut.result()
                results[i] = pa
                done += 1
                v = (f"ERROR: {pa.error}" if pa.error
                     else f"{pa.verdict.label if pa.verdict else '?'} ({pa.bucket})")
                print(f"  [{done:>2}/{total}] {pa.ticker} -> {v} [{elapsed:.1f}s]",
                      flush=True)
                captured = captured.strip()
                if captured:
                    for line in captured.splitlines():
                        print(f"      {line}")
                if log_fn:
                    log_fn(f"[{done:>3}/{total}] {pa.ticker} → "
                           f"{pa.verdict.label if pa.verdict else '?'} ({pa.bucket})")
    finally:
        sys.stdout = old_stdout

    # Persist the staged fundamentals cache once for this batch, and report the
    # hit rate so a slow run is easy to diagnose (cold cache vs. network).
    _flush_fund_cache()
    if _fund_hits or _fund_misses:
        print(f"[fundamentals-cache] {_fund_hits} hit / {_fund_misses} miss "
              f"(skipped {_fund_hits} statement-fetch pairs)")
    # Same for the news-sentiment cache (headlines fetched + scored on misses;
    # scoring is Claude when keyed, else the free lexicon).
    # Same for the news-sentiment cache. A ticker scored by the batch pre-pass
    # is already in the cache by the time the loop reads it, so it counts as a
    # hit here — _news_batched is what that cost, at half rate. Misses are the
    # stragglers scored inline (real-time price, or free lexicon without a key).
    _flush_news_cache()
    if _news_hits or _news_misses:
        if not ANTHROPIC_API_KEY:
            _how = f"{_news_misses} scored via lexicon (free)"
        else:
            _how = (f"{_news_batched} scored via {NEWS_MODEL} at 50% batch rate"
                    f", {_news_misses} inline")
        print(f"[news-cache] {_news_hits} hit / {_news_misses} miss ({_how})")

    return results  # type: ignore[return-value]


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


def select_watchlist_prune_candidates(
    watchlists_analyzed: dict[str, list[PositionAnalysis]],
    threshold: float = 60.0,
) -> dict[str, list[str]]:
    """Pick watchlist tickers whose verdict score fell below `threshold`.

    Returns {watchlist_name: [tickers_to_remove]}. Only positions with a
    real numeric verdict score qualify — analysis errors (score None) are
    never pruned, so a transient data failure can't empty a watchlist.
    Held positions never appear here: watchlist analysis skips tickers you
    own, so a low-scoring holding stays on its watchlist.
    """
    out: dict[str, list[str]] = {}
    for wl_name, items in (watchlists_analyzed or {}).items():
        if wl_name == RECENTLY_HELD_GROUP:
            continue        # synthetic group — there is no such list to prune
        ticks = [pa.ticker for pa in items
                 if not pa.error
                 and pa.verdict is not None
                 and pa.verdict.score is not None
                 and pa.verdict.score < threshold]
        if ticks:
            out[wl_name] = ticks
    return out


def _gh_repo_slug() -> str:
    """Resolve 'owner/repo' for the GitHub-Actions refresh button.

    Prefers GITHUB_REPOSITORY (set automatically inside Actions), then the
    GH_REPO secret, then the local git remote. Returns "" when unknown —
    the report then renders without the refresh button.
    """
    import re as _re
    slug = (os.environ.get("GITHUB_REPOSITORY")
            or os.environ.get("GH_REPO") or "").strip()
    if slug:
        # Accept either 'owner/repo' or a full GitHub URL
        m = _re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?/?$", slug)
        if m:
            return m.group(1)
        if "/" in slug and "://" not in slug:
            return slug
    try:
        import subprocess
        url = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        m = _re.search(r"github\.com[:/](?:[^@/]+@)?([^/]+/[^/\s]+?)(?:\.git)?/?$", url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


_BASE_DEFAULT_NOTE = " ★ The default for scheduled runs."
_BASE_SCORE_BLURBS = {
    "composite": "the Composite Score (quality 30%, growth 20%, value 20%, "
                 "analyst 15%, insider 15%)",
    "quality": "the nine quality filters (70%; near misses earn partial "
               "credit) plus the analyst (15%) and insider (15%) scores",
    "blend": "the average of the Composite and Quality bases",
}

_CALIBRATION_BLURBS = {
    "std": "Scoring as originally calibrated — the value and insider "
           "sub-scores and the context modifiers exactly as they have always "
           "been.",
    "cal": "Scoring with the six fixes from the missed-opportunity review: a "
           "no-information insider read drops out instead of scoring ~48; the "
           "value scale treats the quality gate as average rather than zero; "
           "upside-to-target ramps instead of stepping; price holding above "
           "its 200-day earns credit the trend bucket cannot give it; a label "
           "holds through a near miss; and an imminent earnings print is "
           "scored for fresh money.",
}


def _base_switch_html(interactive: bool) -> str:
    """Header segmented control for the verdict base score and the scoring
    calibration — the two halves of a mode key (see BASE_SCORE_MODES).

    Every combination is already in the report, so either group switches the
    view instantly (the base-view script); the pressed pair starts as the run's
    mode. ★ marks the saved default on both groups: what scheduled runs use for
    watchlist pruning, tax flags and the missed-opportunity history. When
    interactive, a "Make default" button saves the viewed combination as that
    default (the script in _build_refresh_widget)."""
    run_mode = base_score_mode()
    saved = base_score_mode(saved_only=True)
    run_base, run_cal = split_base_mode(run_mode)
    saved_base, saved_cal = split_base_mode(saved)

    def group(items, attr, active, saved_value, title_for):
        out = []
        for key, name in items:
            title = title_for(key)
            full_title, mark = title, ""
            if key == saved_value:
                full_title += _BASE_DEFAULT_NOTE
                mark = "<span class='base-default-mark' aria-hidden='true'>★</span>"
            out.append(
                f'<button type="button" data-{attr}="{key}" '
                f'aria-pressed="{"true" if key == active else "false"}" '
                f'data-title="{title}" title="{full_title}">{name}{mark}</button>')
        return "".join(out)

    base_buttons = group(
        [(b, BASE_SCORE_BASE_LABELS[b]) for b in BASE_SCORE_BASES],
        "base", run_base, saved_base,
        lambda b: f"{BASE_SCORE_BASE_LABELS[b]}: verdicts start from "
                  f"{_BASE_SCORE_BLURBS[b]}.")
    cal_buttons = group(
        [(c, CALIBRATION_LABELS[c]) for c in CALIBRATIONS],
        "cal", run_cal, saved_cal, lambda c: _CALIBRATION_BLURBS[c])

    label_tip = ("Verdict base score: what every verdict starts from. Every "
                 "base and scoring combination is in this report, so switching "
                 "is instant. ★ marks the default that scheduled runs use for "
                 "watchlist pruning, tax flags and the missed-opportunity "
                 "history.")
    if run_mode != saved:
        label_tip += (f" This run used {BASE_SCORE_LABELS[run_mode]} for those "
                      f"(a one-off override).")
    cal_tip = ("Scoring: which calibration of the sub-scores and context "
               "modifiers the verdict uses. Recalibrated applies the six fixes "
               "from the missed-opportunity review — it changes how every base "
               "is measured, not which base you are looking at.")
    html = (f'<div class="base-switch" id="baseScoreSwitch" role="group" '
            f'aria-label="Verdict base score and scoring calibration" '
            f'data-run="{run_mode}" data-saved="{saved}">'
            f'<span class="base-switch-label" title="{label_tip}">Base</span>'
            + base_buttons
            + '<span class="base-switch-sep" aria-hidden="true"></span>'
            + f'<span class="base-switch-label" title="{cal_tip}">Scoring</span>'
            + cal_buttons + "</div>")
    if interactive:
        html += ('<button type="button" class="refresh-btn base-default-btn" '
                 'id="baseDefaultBtn" hidden title="Save the viewed base score '
                 'and scoring as the default for scheduled runs">'
                 '★ Make default</button>')
    return html


def _build_refresh_widget() -> tuple[str, str]:
    """Button + JS that triggers the Actions workflow_dispatch from the report.

    The GitHub API allows CORS from any origin, so the static Pages report
    can call it directly — no server needed. Auth uses a fine-grained PAT
    (Actions: read/write on this one repo) that the user pastes once per
    browser; it lives only in localStorage, never in the published HTML.

    Returns (button_html, status_and_script_html) so the button can sit in
    the header controls cluster while the status line + script live below.
    When the repo can't be resolved there is nothing to dispatch to: the
    buttons are just a read-only base-score indicator and the script is "".
    """
    repo = _gh_repo_slug()
    if not repo:
        return _base_switch_html(interactive=False), ""
    # Base-score switch first; the script below saves its "Make default"
    # choice as the BASE_SCORE_MODE repository variable.
    # Tax toggle sits before the refresh button. Its state lives in
    # localStorage; when on, the dispatch below sends include_tax=true so the
    # regenerated report contains the Tax-Aware Trim Guidance section.
    button = _base_switch_html(interactive=True)
    button += ('<button id="taxSectionToggle" class="refresh-btn tax-toggle" '
              'aria-pressed="false" '
              'title="Include the Tax-Aware Trim Guidance section in the '
              'next data refresh">'
              '&#129534; Tax</button>'
              '<button id="missAnalysisToggle" class="refresh-btn miss-toggle" '
              'aria-pressed="false" '
              'title="Include a Claude AI post-mortem of the Missed '
              'Opportunities in the next data refresh">'
              '&#128269; Analyze with AI</button>'
              '<button id="ghRefreshBtn" class="refresh-btn" '
              'onclick="ghTriggerRefresh()" '
              'title="Trigger the GitHub Actions workflow to regenerate this report">'
              '&#10227; Refresh data</button>')
    widget = """
<div id="ghRefreshStatus" class="refresh-status"></div>
<script>
(function() {
  var REPO = "__REPO__";
  var API = "https://api.github.com/repos/" + REPO;
  var WORKFLOW = "portfolio.yml";   // file name under .github/workflows/
  var REF = "main";
  var TOKEN_KEY = "gh-dispatch-token";
  var TAX_KEY = "tax-section-enabled";
  var MISS_KEY = "miss-analysis-enabled";
  var BASE_VAR = "BASE_SCORE_MODE";   // repository variable the workflow reads
  var BASE_LABELS = __BASE_LABELS__;
  var DEFAULT_NOTE = __DEFAULT_NOTE__;
  var pollTimer = null, startedAt = null;

  function taxEnabled() {
    try { return localStorage.getItem(TAX_KEY) === "1"; }
    catch (e) { return false; }
  }
  function missEnabled() {
    try { return localStorage.getItem(MISS_KEY) === "1"; }
    catch (e) { return false; }
  }
  // Tax section toggle: persisted per-browser; honored by every dispatch
  // the Refresh data button makes (ghTriggerRefresh reads it at send time).
  (function() {
    var tb = document.getElementById("taxSectionToggle");
    if (!tb) return;
    function paint() {
      var on = taxEnabled();
      tb.classList.toggle("active", on);
      tb.setAttribute("aria-pressed", on ? "true" : "false");
      tb.title = on
        ? "Tax section ON \\u2014 the next data refresh will include the " +
          "Tax-Aware Trim Guidance section. Click to turn off."
        : "Include the Tax-Aware Trim Guidance section in the next data " +
          "refresh (fetches full order history, so the run takes longer).";
    }
    tb.addEventListener("click", function() {
      try { localStorage.setItem(TAX_KEY, taxEnabled() ? "0" : "1"); }
      catch (e) {}
      paint();
    });
    paint();
  })();
  // Analyze-with-AI toggle: same pattern as Tax. When on, the dispatch sends
  // analyze_misses=true and the regenerated report embeds a Claude post-mortem
  // of the Missed Opportunities.
  (function() {
    var mb = document.getElementById("missAnalysisToggle");
    if (!mb) return;
    function paint() {
      var on = missEnabled();
      mb.classList.toggle("active", on);
      mb.setAttribute("aria-pressed", on ? "true" : "false");
      mb.title = on
        ? "AI analysis ON \\u2014 the next data refresh will embed a Claude " +
          "post-mortem of the Missed Opportunities. Click to turn off."
        : "Include a Claude AI post-mortem of the Missed Opportunities in the " +
          "next data refresh.";
    }
    mb.addEventListener("click", function() {
      try { localStorage.setItem(MISS_KEY, missEnabled() ? "0" : "1"); }
      catch (e) {}
      paint();
    });
    paint();
  })();
  // "Make default" for the base-score switch. The switch itself is instant
  // (every mode is in the report); the default is what scheduled runs use for
  // watchlist pruning, tax flags and the missed-opportunity history. Those
  // runs dispatch with no inputs, so it lives in the BASE_SCORE_MODE
  // repository variable (token needs Variables: write).
  (function() {
    var sw = document.getElementById("baseScoreSwitch");
    var btn = document.getElementById("baseDefaultBtn");
    if (!sw || !btn) return;
    function viewed() {
      return document.documentElement.getAttribute("data-base-view");
    }
    function sync() {
      var mode = viewed();
      btn.hidden = !BASE_LABELS[mode] || mode === sw.getAttribute("data-saved");
      btn.textContent = "\\u2605 Make " + (BASE_LABELS[mode] || "") + " default";
    }
    function markSaved(mode) {
      sw.setAttribute("data-saved", mode);
      var suffix = window.BASE_CAL_SUFFIX || "-cal";
      var savedCal = mode.slice(-suffix.length) === suffix ? "cal" : "std";
      var savedBase = savedCal === "cal" ? mode.slice(0, -suffix.length) : mode;
      sw.querySelectorAll("button[data-base], button[data-cal]").forEach(function(b) {
        var isDefault = b.hasAttribute("data-base")
          ? b.getAttribute("data-base") === savedBase
          : b.getAttribute("data-cal") === savedCal;
        var mark = b.querySelector(".base-default-mark");
        if (isDefault && !mark) {
          mark = document.createElement("span");
          mark.className = "base-default-mark";
          mark.setAttribute("aria-hidden", "true");
          mark.textContent = "\\u2605";
          b.appendChild(mark);
        } else if (!isDefault && mark) {
          mark.remove();
        }
        b.title = b.getAttribute("data-title") + (isDefault ? DEFAULT_NOTE : "");
      });
      sync();
    }
    function send(tok, method, url, body) {
      return fetch(url, {method: method, headers: headers(tok),
                         body: JSON.stringify(body)})
        .then(function(r) { return r.status; });
    }
    async function saveDefault(tok, mode) {
      var body = {name: BASE_VAR, value: mode};
      var status = await send(tok, "PATCH", API + "/actions/variables/" + BASE_VAR, body);
      if (status === 404) {   // first save: the variable doesn't exist yet
        status = await send(tok, "POST", API + "/actions/variables", body);
      }
      return status;
    }
    btn.addEventListener("click", async function() {
      var mode = viewed(), name = BASE_LABELS[mode];
      if (!name) return;
      if (!confirm("Make " + name + " the default base score?\\n\\n" +
                   "Scheduled runs will use it for watchlist pruning, tax flags " +
                   "and the missed-opportunity history (the " + BASE_VAR +
                   " repository variable). Every base stays one click away " +
                   "in the report.")) return;
      var tok = getToken(false);
      if (!tok) return;
      btn.disabled = true;
      setStatus("Saving " + name + " as the default base score\\u2026");
      var status = null;
      try { status = await saveDefault(tok, mode); }
      catch (e) { setStatus("Network error: " + e, true); }
      btn.disabled = false;
      if (status === 204 || status === 201) {
        markSaved(mode);
        setStatus(name + " is now the default base score for scheduled runs.");
      } else if (status === 401) {
        localStorage.removeItem(TOKEN_KEY);
        setStatus("Token rejected (HTTP 401) \\u2014 click Make default again " +
                  "to re-enter it.", true);
      } else if (status !== null) {
        setStatus("Couldn't save the default (HTTP " + status + "): the token " +
                  "needs \\u201cVariables: Read and write\\u201d on " + REPO + ".", true);
      }
    });
    document.addEventListener("basescorechange", sync);
    sync();
  })();

  function setStatus(msg, isError) {
    var el = document.getElementById("ghRefreshStatus");
    el.textContent = msg;
    el.style.color = isError ? "#c0392b" : "var(--fg-muted)";
  }
  function headers(tok) {
    return {"Authorization": "Bearer " + tok,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"};
  }
  function getToken(forcePrompt) {
    var tok = localStorage.getItem(TOKEN_KEY);
    if (!tok || forcePrompt) {
      tok = prompt(
        "Paste a GitHub fine-grained personal access token to trigger the refresh.\\n\\n" +
        "Create one at github.com -> Settings -> Developer settings -> " +
        "Fine-grained tokens:\\n" +
        "  - Repository access: only " + REPO + "\\n" +
        "  - Permissions: Actions = Read and write\\n" +
        "    (plus Variables = Read and write, so the Base switch can save\\n" +
        "    its choice for scheduled runs)\\n\\n" +
        "It is stored only in this browser (localStorage), never on any server.");
      if (tok) localStorage.setItem(TOKEN_KEY, tok.trim());
    }
    return tok ? tok.trim() : null;
  }
  function elapsedStr() {
    var s = Math.floor((Date.now() - startedAt) / 1000);
    return Math.floor(s / 60) + "m " + (s % 60) + "s";
  }

  window.ghTriggerRefresh = async function() {
    var tok = getToken(false);
    if (!tok) return;
    var btn = document.getElementById("ghRefreshBtn");
    btn.disabled = true;
    setStatus("Triggering workflow\\u2026");
    try {
      var payload = {ref: REF};
      // workflow_dispatch inputs must be strings, even for boolean-typed ones.
      var inputs = {};
      if (taxEnabled()) inputs.include_tax = "true";
      if (missEnabled()) inputs.analyze_misses = "true";
      if (Object.keys(inputs).length) payload.inputs = inputs;
      var r = await fetch(API + "/actions/workflows/" + WORKFLOW + "/dispatches", {
        method: "POST", headers: headers(tok),
        body: JSON.stringify(payload)
      });
      if (r.status === 204) {
        startedAt = Date.now();
        setStatus("Workflow queued" +
                  (taxEnabled() ? " with tax section" : "") +
                  (missEnabled() ? " with AI analysis" : "") +
                  " \\u2014 a fresh report usually takes a few minutes\\u2026");
        pollTimer = setInterval(pollRun, 12000);
      } else if (r.status === 401 || r.status === 403) {
        localStorage.removeItem(TOKEN_KEY);
        setStatus("Token rejected (HTTP " + r.status + ") \\u2014 click Refresh again to re-enter it.", true);
        btn.disabled = false;
      } else {
        var body = await r.text();
        setStatus("Dispatch failed: HTTP " + r.status + " " + body.slice(0, 120), true);
        btn.disabled = false;
      }
    } catch (e) {
      setStatus("Network error: " + e, true);
      btn.disabled = false;
    }
  };

  async function pollRun() {
    var tok = getToken(false);
    if (!tok) return;
    try {
      var r = await fetch(API + "/actions/runs?event=workflow_dispatch&per_page=1",
                          {headers: headers(tok)});
      if (!r.ok) return;
      var data = await r.json();
      var run = (data.workflow_runs || [])[0];
      // Ignore runs from before this click (clock skew margin of 90s)
      if (!run || new Date(run.created_at).getTime() < startedAt - 90000) {
        setStatus("Waiting for run to appear\\u2026 " + elapsedStr());
        return;
      }
      if (run.status !== "completed") {
        setStatus("Run " + run.status.replace("_", " ") + "\\u2026 " + elapsedStr());
        return;
      }
      clearInterval(pollTimer);
      if (run.conclusion === "success") {
        setStatus("\\u2713 Done in " + elapsedStr() +
                  " \\u2014 reloading the fresh report in ~20s (Pages deploy lag)\\u2026");
        setTimeout(function() {
          (window.reloadFreshReport || location.reload.bind(location))();
        }, 20000);
      } else {
        setStatus("Run finished: " + run.conclusion +
                  " \\u2014 see the repo's Actions tab for logs.", true);
        document.getElementById("ghRefreshBtn").disabled = false;
      }
    } catch (e) { /* transient polling error — try again next tick */ }
  }
})();
</script>
"""
    widget = (widget.replace("__REPO__", repo)
              .replace("__BASE_LABELS__", json.dumps(BASE_SCORE_LABELS))
              .replace("__DEFAULT_NOTE__", json.dumps(_BASE_DEFAULT_NOTE)))
    return button, widget


# For sortable verdict column: most urgent action first.
# Holdings: SELL → TRIM → HOLD → ADD
# Watchlist: BUY → WATCH → WAIT → PASS
_VERDICT_ORDER = {
    "SELL": 0, "TRIM": 1, "BUY": 2, "WATCH": 3, "WAIT": 4,
    "HOLD": 5, "ADD": 6, "PASS": 7, "ERROR": 8,
}


def _score_strength_color(s: float) -> str:
    """Shared strength color for the verdict score (inline number + card bar)."""
    if s >= 70:
        return "var(--pos-up)"
    if s >= 50:
        return "var(--fg-strong)"
    if s >= 35:
        return "#e67e22"
    return "var(--pos-down)"


def _mode_variants(render, tag: str = "span") -> str:
    """HTML for every base-score mode, of which the report shows only the
    viewed one (html[data-base-view] + the .bmode rules in _base_view_css).

    `render(mode)` builds one variant. Modes that render the same HTML share
    a single copy; if all do, it comes back unwrapped. Empty variants get no
    wrapper, so those modes show nothing. Copies the run's mode doesn't use
    also carry an inline display:none, so a client that drops the stylesheet
    (an email) still shows exactly one."""
    run_mode = base_score_mode()
    groups: dict[str, list[str]] = {}
    for mode in BASE_SCORE_MODES:
        groups.setdefault(render(mode), []).append(mode)
    if len(groups) == 1:
        return next(iter(groups))
    out = []
    for html, modes in groups.items():
        if not html:
            continue
        classes = " ".join(f"bmode-{m}" for m in modes)
        hidden = "" if run_mode in modes else " style='display:none'"
        out.append(f"<{tag} class='bmode {classes}'{hidden}>{html}</{tag}>")
    return "".join(out)


def _base_view_css() -> str:
    """CSS that shows only the viewed mode's copy of each .bmode variant."""
    hide = ",\n".join(f"html[data-base-view='{m}'] .bmode:not(.bmode-{m})"
                      for m in BASE_SCORE_MODES)
    show = ",\n".join(f"html[data-base-view='{m}'] .bmode.bmode-{m}"
                      for m in BASE_SCORE_MODES)
    return (f"{hide} {{ display: none !important; }}\n"
            f"{show} {{ display: contents !important; }}\n")


def tax_flagged(verdict: Optional[Verdict]) -> bool:
    """Whether a verdict puts its position in the tax section: SELL/TRIM, or a
    verdict score below TAX_FLAG_SCORE_THRESHOLD."""
    if not verdict:
        return False
    return (verdict.label in ("SELL", "TRIM")
            or (verdict.score is not None
                and verdict.score < TAX_FLAG_SCORE_THRESHOLD))


def _verdict_sort_value(verdict: Optional[Verdict]):
    """Verdict column sort key: the score, else the label's urgency order."""
    if verdict and verdict.score is not None:
        return verdict.score
    return 100 - _VERDICT_ORDER.get(verdict.label if verdict else "—", 99)


def _mode_orders(rows: list, key) -> dict[int, dict[str, int]]:
    """{id(row): {mode: index}}: each row's place in its table's default order
    (key(row, mode), highest first) under every base-score mode, for the base
    switch to re-sort by. Empty when no row has per-mode verdicts."""
    if not any(getattr(r, "verdicts", None) for r in rows):
        return {}
    orders: dict[int, dict[str, int]] = {id(r): {} for r in rows}
    for mode in BASE_SCORE_MODES:
        ranked = sorted(rows, key=lambda r: key(r, mode), reverse=True)
        for index, r in enumerate(ranked):
            orders[id(r)][mode] = index
    return orders


def _verdict_td(r) -> str:
    """The verdict <td>: one cell per base-score mode, and a sort value per
    mode that the base switch copies into data-sort."""
    days = getattr(r, "days_to_earnings", None)
    cell = _mode_variants(lambda m: _verdict_cell(verdict_in(r, m), days))
    per_mode = "".join(f" data-sort-{m}='{_verdict_sort_value(verdict_in(r, m))}'"
                       for m in BASE_SCORE_MODES) if r.verdicts else ""
    return f"<td data-sort='{_verdict_sort_value(r.verdict)}'{per_mode}>{cell}</td>"


def _verdict_cell(verdict, days_to_earnings: Optional[int] = None) -> str:
    """Render the verdict pill + 0-100 score with a styled hover-card breakdown.

    Layout: a colored verdict pill (label) and the numeric score sit side by
    side. Hovering the cell reveals a styled card that breaks the score down
    factor by factor — the base score (Composite, Quality or Blend), then each
    +/- modifier (trend, sector, upside, news, ...) — topped with a 0-100
    strength bar. The card also surfaces data-coverage confidence, the verdict
    under the other base-score modes, and, when known, the next-earnings date.
    At-a-glance markers sit beside the score: an amber dot for thin-data (Low)
    confidence and a calendar glyph when earnings are within a week.

    v2 verdict reasons are structured as
        headline | <base line> | +X · factor | -Y · factor | = verdict score N
    which we parse into the card. Non-v2 verdicts (ETF/thematic — no numeric
    score) fall back to the simple pill + native tooltip. The score also lives
    in the parent <td> data-sort so the column sorts by conviction strength.
    """
    if not verdict:
        return "<span style='color:var(--fg-faint);'>—</span>"
    label = verdict.label or "—"
    color = verdict.color or "#7f8c8d"
    reason = verdict.reason or ""
    score = getattr(verdict, "score", None)

    def _esc(s: object) -> str:
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))

    # --- Fallback: no numeric score or unstructured reason (ETF/thematic) ---
    if score is None or " | " not in reason:
        title = _esc(reason.replace(" | ", "\n"))
        return (f"<span class='verdict' style='background:{color};cursor:help;' "
                f"title='{title}'>{label}</span>")

    sc = _score_strength_color(score)
    parts = [p.strip() for p in reason.split(" | ")]
    headline = parts[0] if parts else ""
    # parts[1] = base line (e.g. "Composite Score N"); middle = factor lines;
    # last = "= verdict score N"
    base_line = ""
    factor_rows = []
    for seg in parts[1:]:
        if seg.startswith("= verdict score"):
            continue
        # Factor line "±N · desc" — split on the first middle-dot separator.
        if " · " in seg and seg[:1] in "+-":
            delta_str, _, desc = seg.partition(" · ")
            negative = delta_str.strip().startswith("-")
            cls = "vd-neg" if negative else "vd-pos"
            disp = delta_str.strip().replace("-", "−")  # prettier minus
            factor_rows.append(
                f"<div class='vrow'><span class='vd {cls}'>{_esc(disp)}</span>"
                f"<span class='vt'>{_esc(desc)}</span></div>"
            )
        elif not base_line:
            base_line = seg

    bar_pct = max(0.0, min(100.0, float(score)))
    bar = (f"<div class='vbar'><div class='vbar-fill' "
           f"style='width:{bar_pct:.0f}%;background:{sc};'></div></div>")
    base_html = f"<div class='vcard-base'>{_esc(base_line)}</div>" if base_line else ""

    # Confidence chip — how complete the data behind the score is. Only shown
    # for Medium/Low (High is the unremarkable default), so a thin name reads
    # honestly instead of looking as authoritative as a fully-covered one.
    confidence = getattr(verdict, "confidence", None)
    coverage = getattr(verdict, "coverage", None)
    conf_html = ""
    cell_marker = ""
    if confidence in ("Medium", "Low"):
        cov_txt = f" · {coverage * 100:.0f}% data coverage" if coverage is not None else ""
        ccls = "vconf-low" if confidence == "Low" else "vconf-med"
        conf_html = (f"<div class='vconf {ccls}'>{confidence} confidence{cov_txt}"
                     f"{' — built from limited data' if confidence == 'Low' else ''}</div>")
        if confidence == "Low":
            cell_marker += "<span class='vmark vmark-conf' aria-hidden='true'>●</span>"

    # Earnings footer + at-a-glance calendar marker (event-risk timing).
    earn_html = ""
    if days_to_earnings is not None and days_to_earnings >= 0:
        if days_to_earnings == 0:
            etxt = "Reports today"
        elif days_to_earnings == 1:
            etxt = "Reports tomorrow"
        else:
            etxt = f"Reports in {days_to_earnings} days"
        soon = days_to_earnings <= EARNINGS_SOON_DAYS
        if days_to_earnings <= 30:
            ecls = "vearn-soon" if soon else "vearn"
            earn_html = (f"<div class='vearn-row {ecls}'>"
                         f"<span class='vearn-ico'>📅</span>{etxt}</div>")
        if soon:
            cell_marker += "<span class='vmark vmark-earn' aria-hidden='true'>📅</span>"

    # The same verdict under the other base-score modes, for comparing them
    # before switching (see BASE_SCORE_MODES).
    alt_html = ""
    alternates = getattr(verdict, "alternates", None) or {}
    alt_bits = [f"{BASE_SCORE_LABELS.get(m, m)} base: {alt_label} {alt_score:.0f}"
                for m, (alt_label, alt_score) in alternates.items()
                if alt_score is not None]
    if alt_bits:
        alt_html = f"<div class='valt'>{_esc(' · '.join(alt_bits))}</div>"

    card = (
        f"<div class='vcard' role='tooltip'>"
        f"<div class='vcard-head'>"
        f"<span class='vcard-headline'>{_esc(headline)}</span>"
        f"<span class='vcard-score' style='color:{sc};'>{score:.0f}</span></div>"
        f"{bar}{conf_html}{base_html}"
        f"<div class='vrows'>{''.join(factor_rows)}</div>"
        f"{alt_html}{earn_html}"
        f"</div>"
    )
    # No native title= (it duplicated, and lagged behind, the styled card on
    # desktop). The full breakdown still reaches touch devices via data-tip,
    # which the mobile tap-to-reveal sheet reads.
    mobile_tip = _esc(reason.replace(" | ", "\n")
                      + ("\n" + " · ".join(alt_bits) if alt_bits else ""))
    return (
        f"<span class='vcell' data-tip='{mobile_tip}'>"
        f"<span class='verdict' style='background:{color};'>{label}</span>"
        f"<span class='vscore' style='color:{sc};'>{score:.0f}</span>"
        f"{cell_marker}{card}</span>"
    )


def _td(value: str, sort_value, css_class: str = "") -> str:
    """Render a <td>. sort_value goes in data-sort for client-side sorting.

    Use float("-inf") or empty string for missing values so they sort to bottom.
    """
    cls = f" class='{css_class}'" if css_class else ""
    sv = "" if sort_value is None else sort_value
    return f"<td{cls} data-sort='{sv}'>{value}</td>"


def _rank_move_attrs(move: Optional[dict]) -> tuple[str, object]:
    """(data-rank-move, data-rank-delta) for a rank movement: up/down/new/''
    and the signed places gained (+climbed / -slipped) for magnitude filters."""
    if not move:
        return "", ""
    if move.get("new"):
        return "new", ""
    delta = move.get("delta") or 0
    if delta > 0:
        return "up", delta
    if delta < 0:
        return "down", delta
    return "", ""


def _tr_open(r, orders: Optional[dict[str, int]] = None) -> str:
    """Open a <tr> with data attributes used by the filter bar.

    Rows with per-mode verdicts also carry each mode's verdict, score, rank
    movement and tax flag (data-<attr>-<mode>), which the base switch copies
    into the plain attributes. `orders` ({mode: index}) is the row's place in
    its table's default order under each mode."""
    verdict = (r.verdict.label if r.verdict else "") or ""
    # Verdict numeric score (0-100), separate from the label
    verdict_score = ("" if (not r.verdict or r.verdict.score is None)
                     else r.verdict.score)
    quality = sum(1 for f in r.filters if f.passed) if r.filters else ""
    gain = "" if r.unrealized_gain is None else r.unrealized_gain
    gain_pct = "" if r.unrealized_gain_pct is None else r.unrealized_gain_pct
    day_pct = "" if getattr(r, "day_change_pct", None) is None else r.day_change_pct
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
    # Rank movement vs the previous day (set by _attach_rank_moves; may be
    # absent in lookup mode).
    rank_move, rank_delta = _rank_move_attrs(getattr(r, "_rank_move", None))
    # Whether tax analysis is populated (for "show tax-relevant" filter)
    has_tax = "1" if getattr(r, "tax", None) is not None else "0"
    # The same per base-score mode. Tax detail is computed for positions any
    # mode flags, but only counts in the modes that flag it.
    per_mode = ""
    if getattr(r, "verdicts", None):
        moves = getattr(r, "_rank_moves", None) or {}
        for m in BASE_SCORE_MODES:
            v = verdict_in(r, m)
            m_move, m_delta = _rank_move_attrs(moves.get(m))
            m_tax = "1" if has_tax == "1" and tax_flagged(v) else "0"
            per_mode += (f"data-verdict-{m}='{v.label if v else ''}' "
                         f"data-verdict-score-{m}='{'' if not v or v.score is None else v.score}' "
                         f"data-rank-move-{m}='{m_move}' data-rank-delta-{m}='{m_delta}' "
                         f"data-has-tax-{m}='{m_tax}' ")
    for m, index in (orders or {}).items():
        per_mode += f"data-order-{m}='{index}' "
    # Days until next earnings (forward-only) — drives the 'earnings-soon' filter
    earnings_days = "" if getattr(r, "days_to_earnings", None) is None else r.days_to_earnings
    # News sentiment — label (bullish/neutral/bearish) drives the news facet;
    # score (-1.0 very bearish … +1.0 very bullish) drives the news-score slider.
    _ns = getattr(r, "news_sentiment", None) or {}
    news = (_ns.get("label") or "").lower()
    news_score = "" if _ns.get("score") is None else _ns.get("score")
    # Combined search text — lowercased for case-insensitive contains() matching
    search_text = f"{r.ticker} {r.name} {r.sector or ''}".lower()
    return (
        f"<tr data-verdict='{verdict}' data-verdict-score='{verdict_score}' "
        f"data-earnings-days='{earnings_days}' "
        f"data-quality='{quality}' "
        f"data-gain='{gain}' data-gain-pct='{gain_pct}' "
        f"data-day-pct='{day_pct}' "
        f"data-upside='{upside}' "
        f"data-bucket='{bucket}' "
        f"data-sector-mom='{sector_mom_label}' data-sector='{sector_raw}' "
        f"data-pos52='{pos52}' data-score='{score}' "
        f"data-insider='{insider}' data-has-insider='{has_insider_data}' "
        f"data-news='{news}' data-news-score='{news_score}' "
        f"data-trend='{trend}' data-ma-pct='{ma_pct}' "
        f"data-port-pct='{port_pct}' "
        f"data-days-held='{days_held}' "
        f"data-recommendation='{recommendation}' "
        f"data-has-tax='{has_tax}' "
        f"data-rank-move='{rank_move}' data-rank-delta='{rank_delta}' "
        f"{per_mode}"
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


def _rank_move_badge(r) -> str:
    """Small ▲/▼/NEW chip showing rank movement vs the previous day (rank is by
    verdict score within the ticker's table), one per base-score mode.
    Unchanged rows render nothing, to keep tables clean. Movement is set on
    r._rank_moves / r._rank_move by _attach_rank_moves."""
    moves = getattr(r, "_rank_moves", None)
    if moves:
        return _mode_variants(lambda m: _rank_badge_html(moves.get(m)))
    return _rank_badge_html(getattr(r, "_rank_move", None))


def _rank_badge_html(move: Optional[dict]) -> str:
    if not move:
        return ""
    base = ("font-size:8px;font-weight:700;padding:1px 4px;border-radius:6px;"
            "margin-left:5px;vertical-align:middle;")
    if move.get("new"):
        return (f"<span title='New to the rankings this run' "
                f"style='{base}letter-spacing:.3px;"
                f"background:var(--bg-chip-blue);color:var(--fg-chip-blue);'>"
                f"NEW</span>")
    delta = move.get("delta") or 0
    if delta == 0:
        return ""
    if delta > 0:
        arrow, bg, fg, word = "▲", "var(--bg-chip-green)", "var(--fg-chip-green)", "up"
    else:
        arrow, bg, fg, word = "▼", "var(--bg-chip-red)", "var(--fg-chip-red)", "down"
    n = abs(delta)
    title = (f"Moved {word} {n} place{'s' if n != 1 else ''} "
             f"vs the previous day (by verdict rank)")
    return (f"<span title='{title}' style='{base}"
            f"background:{bg};color:{fg};'>{arrow}{n}</span>")


def _ticker_cell(r) -> str:
    """Render ticker with business-summary tooltip on hover, plus a rank-movement
    badge (▲/▼/NEW) vs the previous day."""
    if not r.ticker:
        return "—"
    badge = _rank_move_badge(r)
    if r.business_summary:
        # Escape attribute-breaking chars
        summary = (r.business_summary
                   .replace("&", "&amp;")
                   .replace("'", "&#39;")
                   .replace('"', "&quot;"))
        return (f"<span class='ticker' style='cursor:help;' "
                f"title='{summary}'>{r.ticker}</span>{badge}")
    return f"<span class='ticker'>{r.ticker}</span>{badge}"


def _news_chip(r) -> str:
    """Compact news-sentiment badge (📰) — only for clear bullish/bearish reads;
    neutral is omitted to avoid clutter. Rationale shows on hover."""
    ns = getattr(r, "news_sentiment", None)
    if not ns:
        return ""
    label = (ns.get("label") or "").lower()
    if label == "bullish":
        bg, fg = "var(--bg-chip-green)", "var(--fg-chip-green)"
    elif label == "bearish":
        bg, fg = "var(--bg-chip-red)", "var(--fg-chip-red)"
    else:
        return ""
    rationale = ((ns.get("rationale") or "")
                 .replace("&", "&amp;").replace("'", "&#39;").replace('"', "&quot;"))
    asof = ns.get("as_of", "")
    title = (f"{rationale} (news as of {asof})" if rationale
             else f"News sentiment as of {asof}")
    return (f"<span title=\"{title}\" style='font-size:9px;font-weight:600;"
            f"background:{bg};color:{fg};padding:1px 5px;border-radius:6px;"
            f"margin-left:5px;cursor:help;'>📰 {label}</span>")


def _name_sector_cell(r) -> str:
    """Combined Name + Sector — name primary, sector momentum badge below.

    The full business summary appears as a tooltip on hover of the name.
    """
    name = r.name or "—"
    news_chip = _news_chip(r)
    # Build the name with optional business-summary tooltip
    if r.business_summary:
        summary = (r.business_summary
                   .replace("&", "&amp;")
                   .replace("'", "&#39;")
                   .replace('"', "&quot;"))
        name_html = (f"<div style='font-weight:500;'>"
                     f"<span style='cursor:help;' title='{summary}'>{name}</span>"
                     f"{news_chip}</div>")
    else:
        name_html = f"<div style='font-weight:500'>{name}{news_chip}</div>"

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


def _today_cell(r) -> str:
    """Today's move: per-share % (primary) + position $ impact (for holdings)."""
    if getattr(r, "day_change_pct", None) is None:
        return "<span style='color:var(--fg-faint);'>—</span>"
    cls = "pos-up" if r.day_change_pct > 0 else (
          "pos-down" if r.day_change_pct < 0 else "")
    out = (f"<div class='{cls}' style='font-weight:600;'>"
           f"{_fmt_pct(r.day_change_pct, 2, True)}</div>")
    # Dollar impact on the position, when shares are held.
    if getattr(r, "shares", 0) and r.day_change is not None:
        impact = r.day_change * r.shares
        out += (f"<div class='{cls}' style='font-size:10px;margin-top:1px;'>"
                f"{_fmt_money(impact)}</div>")
    return out


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
    """Render the Composite Score as a bold number with a compact Q/G/V/A/I
    sub-score strip beneath it.

    The strip puts the column's empty space to use and surfaces the sub-score
    breakdown that previously hid in the tooltip (also visible on mobile).
    Each letter is colored green/amber/red by its sub-score and carries its
    own number in a tooltip; the full breakdown stays in the number's tooltip.
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

    # Compact sub-score strip: one colored letter per dimension.
    def _sub_color(s: float) -> str:
        return ("var(--pos-up)" if s >= 60
                else "#e67e22" if s >= 40 else "var(--pos-down)")
    letters = [("Q", "Quality", q), ("G", "Growth", g), ("V", "Value", v),
               ("A", "Analyst", a), ("I", "Insider", ins)]
    strip = ""
    for ltr, full, sub in letters:
        if sub is None:
            strip += (f"<span style='color:var(--fg-faint);' "
                      f"title='{full}: n/a'>{ltr}</span>")
        else:
            strip += (f"<span style='color:{_sub_color(sub)};' "
                      f"title='{full}: {sub:.0f}'>{ltr}</span>")

    return (
        f"<div style='display:flex;flex-direction:column;align-items:flex-end;"
        f"line-height:1.1;'>"
        f"<span title='{title}' style='font-weight:700;color:{color};"
        f"font-size:15px;cursor:help;font-variant-numeric:tabular-nums;'>"
        f"{score:.0f}</span>"
        f"<span style='font-size:9px;font-weight:700;letter-spacing:1.5px;"
        f"margin-top:1px;cursor:help;'>{strip}</span>"
        f"</div>"
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
    """Render filter pass/fail as colored dots with hover tooltip. A grey dot
    is a filter with no data (left out of the quality-filter score); a red
    dot's tooltip shows any partial soft-gate credit it still earns."""
    parts = []
    for f in filters:
        if f.passed:
            color = "#27ae60"
        elif f.credit is None:
            color = "#95a5a6"
        else:
            color = "#c0392b"
        # Format actual value with units. For filters whose `note` is empty
        # or a "%" unit indicator, treat it as the unit suffix (legacy
        # behavior). For filters whose `note` is descriptive metadata
        # like "3yr CAGR" / "1yr YoY", append it as a separate clause
        # in the tooltip so it doesn't get smashed into the number.
        if f.display is not None:
            actual_str = f.display
        elif f.actual is None:
            actual_str = "n/a"
        elif f.note in (None, "", "%"):
            actual_str = f"{f.actual:.1f}{f.note or '%'}"
        else:
            # Note is descriptive — render value with default % unit,
            # then append the note as context.
            actual_str = f"{f.actual:.1f}% ({f.note})"
        title = f"{f.name}: {actual_str} (threshold {f.threshold})"
        if f.credit is None and not f.passed:
            title += " · no data, left out of the quality score"
        elif not f.passed and f.credit:
            title += f" · near miss, {f.credit * 100:.0f}% credit in the quality score"
        parts.append(
            f'<span title="{title}" class="qdot" '
            f'style="background:{color};"></span>'
        )
    return "".join(parts)


def _filter_count_html(r, passed: int) -> str:
    """The 'N/9' beside the filter dots; its tooltip carries the soft-gated
    quality-filter score the Quality base score is built from."""
    style = "color:var(--fg-muted);font-size:11px"
    if r.filter_score is None:
        return f"<span style='{style}'>{passed}/9</span>"
    rated = sum(1 for f in r.filters if f.credit is not None)
    tip = (f"Quality-filter score {r.filter_score:.0f}/100: near misses earn "
           f"partial credit; {rated} of {len(r.filters)} filters had data")
    return f"<span style='{style};cursor:help;' title='{tip}'>{passed}/9</span>"


def _verdict_th() -> str:
    """Verdict column header. Its tooltip names no single base: the Base
    switch changes that in the page. `verdict-th` lets the switch re-sort a
    table the reader sorted by this column."""
    return (
        "<th class='verdict-th' title='Verdict score (0-100): starts from the base "
        "score picked with the Base switch (Composite, Quality or Blend), then adds "
        "trend, sector, 52-week position, upside to target, position size and "
        "news. Hover a verdict for the breakdown.' style='cursor:help;'>"
        "Verdict <span style='color:var(--fg-faint);font-weight:400;font-size:10px;"
        "text-transform:none;letter-spacing:0;'>(score)</span></th>"
    )


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
        # Compact: narrower bar + counts-only label (B/H/S color-coded), with
        # the full "X Buy · Y Hold · Z Sell · source" in the tooltip. Saves
        # ~30% of the column's width versus the spelled-out label.
        full = f"{buy} Buy · {hold} Hold · {sell} Sell · {source}"
        bar = f'<div title="{full}" class="rbar">'
        for pct, color in zip(pcts, colors):
            if pct > 0:
                bar += f'<div style="width:{pct:.1f}%;background:{color};"></div>'
        bar += "</div>"
        bar += (
            f'<div title="{full}" style="font-size:10px;cursor:help;'
            f'font-variant-numeric:tabular-nums;">'
            f'<span style="color:#27ae60;">{buy}</span>·'
            f'<span style="color:#f39c12;">{hold}</span>·'
            f'<span style="color:#c0392b;">{sell}</span>'
            f'<span style="color:var(--fg-faint);"> {total}</span></div>'
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
      flagged: list of PositionAnalysis with `tax` field populated
               (SELL/TRIM verdicts, or verdict score below 75)
      all_holdings: full holdings list (used to find loss-harvest candidates)
      realized_ytd: dict from fetch_realized_ytd() — if present, YTD section renders
    """
    html = "<h2 style='margin-top:48px;'>Tax-Aware Trim Guidance</h2>\n"
    html += (
        '<p style="color:var(--fg-muted);font-size:12px;margin-top:-6px;margin-bottom:8px;">'
        "For positions flagged SELL or TRIM, or with a verdict score below 75: "
        "holding-period status, estimated tax if trimmed now, and the "
        "least-taxable ways to do it."
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

    # ---------- Per-position trim guidance ----------
    # Tax detail exists for every position any base-score mode flags; each
    # mode shows only the cards its own verdicts flag (see tax_flagged).
    flagged = [r for r in flagged if r.tax is not None]
    if flagged:
        html += "<h3 style='margin-top:32px;'>Per-Position Trim Detail</h3>\n"

        def _none_flagged(mode):
            if any(tax_flagged(verdict_in(r, mode)) for r in flagged):
                return ""
            return ("<p style='color:var(--fg-muted);font-size:12px;'>No positions "
                    f"are flagged under the {BASE_SCORE_LABELS[mode]} base score.</p>")
        html += _mode_variants(_none_flagged, tag="div")

    for r in flagged:
        ta = r.tax
        # Everything but the verdict pill is the same in every mode.
        card = ""
        # Holding period badge
        if getattr(ta, "has_lots", False) and ta.lt_shares and ta.st_shares:
            card += (f"<span style='font-size:11px;background:var(--bg-chip-amber);color:var(--fg-chip-amber);"
                     f"padding:3px 8px;border-radius:4px;'>Mixed: "
                     f"{ta.lt_shares:g} LT + {ta.st_shares:g} ST</span>")
        elif ta.is_long_term is True:
            card += ("<span style='font-size:11px;background:var(--bg-chip-green);color:var(--fg-chip-green);"
                     "padding:3px 8px;border-radius:4px;'>Long-term ✓</span>")
        elif ta.is_long_term is False:
            badge = "Short-term"
            if getattr(ta, "next_lot_to_lt_days", None) is not None:
                badge += f" · {ta.next_lot_to_lt_days}d to long-term"
            elif ta.days_to_long_term is not None:
                badge += f" · {ta.days_to_long_term}d to long-term"
            card += (f"<span style='font-size:11px;background:var(--bg-chip-red);color:var(--fg-chip-red);"
                     f"padding:3px 8px;border-radius:4px;'>{badge}</span>")
        else:
            card += ("<span style='font-size:11px;background:var(--bg-chip-neutral);color:var(--fg-chip-neutral);"
                     "padding:3px 8px;border-radius:4px;'>Holding period unknown</span>")
        card += "</div>\n"

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
            card += "<div style='font-size:12px;color:var(--fg-body);margin-bottom:8px;'>"
            card += (
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
            card += (
                f"<tr><td style='padding:2px 12px 2px 0;border:none;color:var(--pos-up);'>"
                f"Long-term</td>"
                f"<td style='text-align:right;padding:2px 12px;border:none;'>{lt_sh:g}</td>"
                f"<td style='text-align:right;padding:2px 12px;border:none;'>{_fmt_money(lt_g)}</td>"
                f"<td style='text-align:right;padding:2px 12px;border:none;'>"
                f"{_fmt_money(lt_tax)}"
                f"{f' ({ta.effective_rate_lt*100:.0f}%)' if ta.effective_rate_lt else ''}</td></tr>"
            )
            card += (
                f"<tr><td style='padding:2px 12px 2px 0;border:none;color:var(--pos-down);'>"
                f"Short-term</td>"
                f"<td style='text-align:right;padding:2px 12px;border:none;'>{st_sh:g}</td>"
                f"<td style='text-align:right;padding:2px 12px;border:none;'>{_fmt_money(st_g)}</td>"
                f"<td style='text-align:right;padding:2px 12px;border:none;'>"
                f"{_fmt_money(st_tax)}"
                f"{f' ({ta.effective_rate_st*100:.0f}%)' if ta.effective_rate_st else ''}</td></tr>"
            )
            card += (
                f"<tr style='border-top:1px solid var(--border-medium);font-weight:600;'>"
                f"<td style='padding:3px 12px 3px 0;border:none;'>Total</td>"
                f"<td style='text-align:right;padding:3px 12px;border:none;'>{lt_sh+st_sh:g}</td>"
                f"<td style='text-align:right;padding:3px 12px;border:none;'>{_fmt_money(lt_g+st_g)}</td>"
                f"<td style='text-align:right;padding:3px 12px;border:none;'>{_fmt_money(lt_tax+st_tax)}</td></tr>"
            )
            card += "</table></div>\n"

            # Collapsible per-lot detail
            if ta.lots_detail:
                card += (
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
                    card += (
                        f"<tr><td>{lot['date']}</td>"
                        f"<td class='num'>{lot['shares']:g}</td>"
                        f"<td class='num'>{_fmt_money(lot['buy_price'])}</td>"
                        f"<td class='num'>{lot['days_held']}</td>"
                        f"<td>{status}</td>"
                        f"<td class='num' style='color:{gain_color};'>"
                        f"{_fmt_money(lot['gain'])}</td></tr>"
                    )
                card += "</tbody></table></details>\n"
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
            card += ("<div style='font-size:12px;color:var(--fg-body);margin-bottom:8px;'>"
                     + " &nbsp;·&nbsp; ".join(parts) + "</div>\n")
        elif gain is not None and gain < 0:
            card += (f"<div style='font-size:12px;color:var(--fg-body);margin-bottom:8px;'>"
                     f"Unrealized loss: <strong style='color:var(--pos-down);'>"
                     f"{_fmt_money(gain)}</strong> &nbsp;·&nbsp; "
                     f"Selling harvests a deductible loss</div>\n")
        else:
            card += ("<div style='font-size:12px;color:var(--fg-muted);margin-bottom:8px;'>"
                     "Cost basis unavailable — connect via Robinhood for gain/tax "
                     "estimates</div>\n")

        # Timing note
        if ta.timing_note:
            card += (f"<div style='font-size:12px;color:#34495e;background:#f8f9fa;"
                     f"padding:8px 10px;border-radius:4px;margin-bottom:8px;'>"
                     f"⏱ {ta.timing_note}</div>\n")

        # Strategies
        if ta.strategies:
            card += "<ul style='margin:6px 0 0;padding-left:18px;font-size:12px;color:var(--fg-body);'>"
            for strat in ta.strategies:
                card += f"<li style='margin-bottom:4px;'>{strat}</li>"
            card += "</ul>\n"

        def render_card(mode):
            v = verdict_in(r, mode)
            if not tax_flagged(v):
                return ""
            return (
                "<div style='border:1px solid var(--border-medium);border-radius:8px;"
                "background:var(--bg-card);padding:14px 16px;margin-bottom:14px;'>"
                "<div style='display:flex;align-items:center;gap:10px;margin-bottom:8px;'>"
                f"<span class='ticker' style='font-size:15px;'>{r.ticker}</span>"
                f"<span class='verdict' style='background:{v.color}'>{v.label}</span>"
                f"{card}</div>\n"
            )
        html += _mode_variants(render_card, tag="div")

    return html


def finalize_holding_verdicts(results: list[PositionAnalysis]) -> float:
    """Populate live_pct_portfolio and re-run the v2 verdict with position size.

    analyze_position() runs per-stock without portfolio context, so its verdict
    can't include the position-size signal. Once the whole portfolio is known,
    this applies the final verdict — the size penalty can flip a HOLD to TRIM
    for overweight positions. Idempotent; returns the live portfolio total.

    MUST run before tax analysis: the tax section selects SELL/TRIM positions,
    so any verdict that flips after the tax loop would silently get no tax card.
    Watchlist items aren't re-run (you don't own them, so size doesn't apply).
    """
    live_total = sum(
        r.live_market_value for r in results if r.live_market_value is not None
    )
    for r in results:
        if r.live_market_value is not None and live_total > 0:
            r.live_pct_portfolio = r.live_market_value / live_total * 100

    mode = base_score_mode()
    for r in results:
        if (r.bucket == "compounder"
                and has_verdict_base(r, mode)
                and r.live_pct_portfolio is not None):
            # Reuses r.news_sentiment scored in analyze_position.
            set_v2_verdicts(r, is_holding=True, position_pct=r.live_pct_portfolio)
    return live_total


def compare_base_modes(
    results: list[PositionAnalysis],
    watchlists: Optional[dict[str, list[PositionAnalysis]]] = None,
    prune_threshold: Optional[float] = None,
) -> list[str]:
    """Side-by-side of the base-score modes over this run's v2 verdicts, as log
    lines: verdict counts per mode for holdings and watchlist names, how many
    watchlist names each would prune (when `prune_threshold` is given), and
    which verdicts would change. Read off each position's `verdicts` — nothing
    is re-scored. Run after finalize_holding_verdicts so holdings include the
    position-size overlay."""
    active = base_score_mode()
    held = {r.ticker for r in results}
    seen: set[str] = set()
    watch: list[PositionAnalysis] = []
    for items in (watchlists or {}).values():
        for r in items:
            if r.ticker not in held and r.ticker not in seen:
                seen.add(r.ticker)
                watch.append(r)

    def by_mode(r) -> dict:
        if r.error:
            return {}
        return {m: (v.label, v.score) for m, v in (r.verdicts or {}).items()}

    holding_rows = [by_mode(r) for r in results]
    watch_rows = [by_mode(r) for r in watch]
    groups = [(name, rows, labels) for name, rows, labels in (
        ("holdings", holding_rows, ("ADD", "HOLD", "TRIM", "SELL")),
        ("watchlist", watch_rows, ("BUY", "WATCH", "WAIT", "PASS")),
    ) if any(rows)]
    if not groups:
        return []
    show_prune = prune_threshold is not None and any(watch_rows)

    saved = base_score_mode(saved_only=True)
    source = "saved default" if saved == active else "this run only"
    lines = [f"[base-score] Verdicts use the {BASE_SCORE_LABELS[active]} base "
             f"({source}). Side by side — same modifiers, different base:"]
    header = f"  {'base':<16}"
    for name, _, labels in groups:
        header += f"{name + ' ' + '/'.join(labels):<34}"
    if show_prune:
        header += f"prune <{prune_threshold:g}"
    lines.append(header.rstrip())
    for mode in BASE_SCORE_MODES:
        row = f"  {_short_mode_label(mode) + ('*' if mode == active else ''):<16}"
        for _, rows, labels in groups:
            got = [m[mode][0] for m in rows if mode in m]
            row += f"{'/'.join(str(got.count(lbl)) for lbl in labels):<34}"
        if show_prune:
            row += str(sum(1 for m in watch_rows
                           if mode in m and m[mode][1] is not None
                           and m[mode][1] < prune_threshold))
        lines.append(row.rstrip())

    def pruned_under(mode: str) -> set[str]:
        return {r.ticker for r, m in zip(watch, watch_rows)
                if mode in m and m[mode][1] is not None
                and m[mode][1] < prune_threshold}

    tickers = [r.ticker for r in results] + [r.ticker for r in watch]
    modes_by_ticker = holding_rows + watch_rows
    for mode in BASE_SCORE_MODES:
        if mode == active:
            continue
        name = _short_mode_label(mode)
        changes = [f"{t} {m[active][0]}→{m[mode][0]}"
                   for t, m in zip(tickers, modes_by_ticker)
                   if active in m and mode in m and m[active][0] != m[mode][0]]
        if changes:
            lines.append(f"  {name} would change {len(changes)}: " + ", ".join(changes))
        else:
            lines.append(f"  {name} would change no verdicts.")
        # Pruning deletes from the real watchlists, so name the tickers.
        if show_prune:
            now, then = pruned_under(active), pruned_under(mode)
            bits = []
            if then - now:
                bits.append("also prune " + ", ".join(sorted(then - now)))
            if now - then:
                bits.append("keep " + ", ".join(sorted(now - then)))
            if bits:
                lines.append("    and would " + "; ".join(bits))
    return lines


# ---------------------------------------------------------------------------
# Missed-opportunity tracking
# ---------------------------------------------------------------------------
# A persistent ledger (recs_history.json) remembers the FIRST time each ticker
# was seen (any verdict — holding or watchlist), with the price/allocation at
# that moment. Every run we refresh the latest price/allocation for tickers we
# can still see, so the report can show which names ran up while we under-held
# them. The ledger holds real allocations, so it is gitignored (kept out of the
# public repo) and persisted across CI runs via the GitHub Actions cache
# instead — see .github/workflows/portfolio.yml.

RECS_HISTORY_FILE = "recs_history.json"
# Buy-type verdicts — purely for nicer wording in the "why it's a miss" reason
# (a name first seen as ADD/BUY/WATCH reads as "Flagged ..."). Tracking is NOT
# limited to these; every ticker we see gets a ledger entry.
REC_VERDICT_LABELS = {"ADD", "BUY", "WATCH"}
# A tracked ticker is a "missed opportunity" once it is up at least this much
# since it was first seen...
MISSED_OPP_GAIN_PCT = 5.0
# ...AND we still hold less than this share of the portfolio (0% = never added;
# a small position counts too — we under-allocated relative to the conviction).
MISSED_OPP_ALLOC_THRESHOLD = 2.0
# Buy-grade verdicts — the ones that ARE an action signal (unlike WATCH, which
# is a "not yet"). Used to classify a miss as a model gap (never rated a buy) vs
# an execution gap (rated a buy, but we didn't size up), and to flag names still
# rated buy-grade today with a token allocation.
BUY_GRADE_VERDICTS = {"ADD", "BUY", "BUY MORE"}
# A name still rated buy-grade today counts as "still actionable" only while our
# allocation is under this — i.e. the call is live and we still haven't acted.
STILL_ACTIONABLE_ALLOC_PCT = 1.0


def _fmt_short_date(iso: Optional[str]) -> str:
    """ISO 'YYYY-MM-DD' -> 'Jun 23, 2026'. Falls back to the raw value."""
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%b %d, %Y")
    except (ValueError, TypeError):
        return iso or ""


def load_recs_history(path: str = RECS_HISTORY_FILE) -> dict:
    """Load the recommendation-history ledger; tolerant of a missing/corrupt file."""
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("tickers"), dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {"version": 1, "tickers": {}}


def _verdict_why_not_buy(verdict) -> str:
    """From a Verdict's transparent breakdown (the ' | '-joined reason text),
    return a short plain-text phrase naming the factors that kept the score
    below the BUY/ADD bar — i.e. the negative contributors, biggest impact
    first. The breakdown lines look like "-8 · Price 30% above target"; we keep
    the description after the dot. Returns '' when there are no drags (e.g. an
    already buy-type verdict) or no reason text. This is what answers "why we
    didn't buy" in the Missed Opportunities table."""
    reason = getattr(verdict, "reason", None) if verdict else None
    if not reason:
        return ""
    drags: list[str] = []
    for part in reason.split("|"):
        part = part.strip()
        if part.startswith("-") and "·" in part:          # a negative factor line
            desc = part.split("·", 1)[1].strip()
            if desc:
                # Lowercase the leading word so it reads inline:
                # "held back by price above target".
                drags.append(desc[0].lower() + desc[1:])
    return "; ".join(drags[:3])


def _pick_catalyst_headline(ticker: str, name: Optional[str],
                            headlines: Optional[list]) -> str:
    """Choose the headline most likely to be the run-up catalyst: the first one
    that names the ticker or the company, else the first headline. Concrete
    headlines matter most on the free-lexicon path, whose rationale is only a
    word-count summary."""
    if not headlines:
        return ""
    import re as _re
    name_words = [w for w in _re.split(r"\W+", name or "") if len(w) >= 4]
    for h in headlines:
        up = h.upper()
        if ticker and ticker.upper() in up:
            return h.strip()
        if any(w.upper() in up for w in name_words[:2]):
            return h.strip()
    return headlines[0].strip()


def _rec_factors(r: PositionAnalysis) -> dict:
    """Numeric factor snapshot stored per ledger entry.

    `why` (below) records the verdict's drags as *rendered prose*, which is fine
    for the report but useless for measurement — you can't correlate a sentence
    with a forward return. Without these raw values, asking "did the 52-week
    position actually predict anything?" means re-downloading history and
    recomputing every factor from scratch. Persisting them makes that a query
    over the ledger instead.

    Rounded to keep the pretty-printed JSON diffs readable; None-valued factors
    are dropped so limited-coverage tickers don't store a wall of nulls."""
    def _r(v, nd=2):
        return round(float(v), nd) if isinstance(v, (int, float)) else None

    factors = {
        # Price-vs-own-range axis (the two that used to double-count)
        "week52_position": _r(r.week52_position, 1),
        "pct_above_ma200": _r(r.pct_above_ma200, 1),
        "trend": r.trend,
        # Composite sub-scores, so score attribution needs no re-derivation
        "score_quality": _r(r.score_quality, 1),
        "score_growth": _r(r.score_growth, 1),
        "score_value": _r(r.score_value, 1),
        "score_analyst": _r(r.score_analyst, 1),
        "score_insider": _r(r.score_insider, 1),
        "composite_score": _r(r.composite_score, 1),
        "composite_coverage": _r(r.composite_coverage, 3),
        # The recalibrated twins, so the two calibrations can be graded against
        # the same forward returns straight off the ledger. score_insider_cal
        # is absent whenever the insider read carried no information — which is
        # itself the measurement finding 1 rests on.
        "score_value_cal": _r(r.score_value_cal, 1),
        "score_insider_cal": _r(r.score_insider_cal, 1),
        "composite_score_cal": _r(r.composite_score_cal, 1),
        "quality_base_cal": _r(r.quality_base_cal, 1),
        # Quality base-score mode inputs, which base the verdict used, and what
        # every base said — so the modes can be graded against outcomes.
        "filter_score": _r(r.filter_score, 1),
        "filter_coverage": _r(r.filter_coverage, 3),
        "quality_base": _r(r.quality_base, 1),
        "quality_coverage": _r(r.quality_coverage, 3),
        "base_mode": (r.verdict.base_mode if r.verdict else None),
        "verdicts": ({m: [v.label, _r(v.score, 1)] for m, v in r.verdicts.items()}
                     or None),
        # Verdict-layer inputs
        "upside_pct": _r(r.upside_pct, 1),
        "sector_momentum": ((r.sector_momentum or {}).get("label")
                            if isinstance(r.sector_momentum, dict) else None),
        "quality_filters_passed": (sum(1 for f in r.filters if f.passed)
                                   if r.filters else None),
        "quality_filters_total": (len(r.filters) if r.filters else None),
    }
    return {k: v for k, v in factors.items() if v is not None}


def _rec_diagnostic(r: PositionAnalysis) -> dict:
    """Compact 'why not a buy' diagnostic stored alongside each ledger snapshot:
    the verdict's drag factors, the latest-news catalyst (when news scoring
    is enabled), and the numeric factor values behind the verdict. Lets the
    Missed Opportunities table explain why the analyzer didn't flag a BUY/ADD
    and what news drove the run-up — without re-deriving anything at render
    time — and lets factor attribution run straight off the ledger."""
    news = None
    ns = getattr(r, "news_sentiment", None)
    if ns and (ns.get("rationale") or ns.get("label") or ns.get("headlines")):
        news = {
            "label": (ns.get("label") or "").strip(),
            "rationale": (ns.get("rationale") or "").strip(),
            "headline": _pick_catalyst_headline(r.ticker, r.name, ns.get("headlines")),
        }
    return {"why": _verdict_why_not_buy(r.verdict), "news": news,
            "factors": _rec_factors(r)}


def _current_rec_snapshot(
    results: list[PositionAnalysis],
    watchlists: Optional[dict[str, list[PositionAnalysis]]],
) -> dict[str, dict]:
    """Build {ticker: {price, alloc, verdict, name, sector, why, news}} for
    everything we can see this run. Holdings win over watchlist entries (real
    allocation). `alloc` is % of portfolio (0 for non-held watchlist names).
    `why`/`news` capture why it wasn't a BUY (verdict drags + news catalyst)."""
    snapshot: dict[str, dict] = {}
    # Watchlist first, so holdings overwrite with the real allocation.
    for items in (watchlists or {}).values():
        for r in items:
            if r.current_price is None:
                continue
            snapshot[r.ticker] = {
                "price": r.current_price,
                "alloc": 0.0,
                "verdict": (r.verdict.label if r.verdict else None),
                "verdict_score": (r.verdict.score if r.verdict else None),
                "name": r.name,
                "sector": r.sector,
                **_rec_diagnostic(r),
            }
    for r in results:
        if r.current_price is None:
            continue
        snapshot[r.ticker] = {
            "price": r.current_price,
            "alloc": (r.live_pct_portfolio if r.live_pct_portfolio is not None else 0.0),
            "verdict": (r.verdict.label if r.verdict else None),
            "verdict_score": (r.verdict.score if r.verdict else None),
            "name": r.name,
            "sector": r.sector,
            **_rec_diagnostic(r),
        }
    return snapshot


# --- Run-over-run ranking -------------------------------------------------
# Every report table is sorted by verdict score (highest conviction first). We
# record each ticker's position under that canonical sort so the next run can
# show how far it climbed or slipped (▲/▼ badges in the ticker cell). Rank is
# tracked per group — holdings compounders, holdings thematics, and each
# watchlist rank independently — so we only ever compare like-for-like.

def _holding_rank_key(r, mode: Optional[str] = None):
    """Default holdings sort: verdict score desc (under `mode`, default the
    run's), market value as tiebreak."""
    v = verdict_in(r, mode)
    score = v.score if v and v.score is not None else -1
    return (score, r.live_market_value or 0)


def _watchlist_rank_key(r, mode: Optional[str] = None):
    """Default watchlist sort: verdict score desc (under `mode`, default the
    run's), upside as tiebreak."""
    v = verdict_in(r, mode)
    score = v.score if v and v.score is not None else -1
    return (score, r.upside_pct if r.upside_pct is not None else -1e6)


def compute_run_ranks(
    results: list[PositionAnalysis],
    watchlists: Optional[dict[str, list[PositionAnalysis]]] = None,
    mode: Optional[str] = None,
) -> dict[str, dict]:
    """Return {ticker: {"group": str, "rank": int}} — each ticker's 1-based
    position under the report's default (verdict-score) sort within its group,
    scoring with `mode`'s verdicts (default the run's). Groups: 'compounder',
    'thematic', 'watch:<list>'. Held tickers are excluded from watchlist
    ranking to mirror the rendered tables (holdings win)."""
    def holding_key(r):
        return _holding_rank_key(r, mode)

    def watchlist_key(r):
        return _watchlist_rank_key(r, mode)

    ranks: dict[str, dict] = {}
    compounders = sorted((r for r in results if r.bucket == "compounder"),
                         key=holding_key, reverse=True)
    thematics = sorted((r for r in results if r.bucket == "thematic"),
                       key=holding_key, reverse=True)
    for group, lst in (("compounder", compounders), ("thematic", thematics)):
        for i, r in enumerate(lst, 1):
            ranks[r.ticker] = {"group": group, "rank": i}
    if watchlists:
        held = {r.ticker for r in results}
        for wl_name, items in watchlists.items():
            ordered = sorted((r for r in items if r.ticker not in held),
                             key=watchlist_key, reverse=True)
            for i, r in enumerate(ordered, 1):
                # A ticker in several lists is ranked by the first list it
                # appears in (its analysis object is shared across lists).
                ranks.setdefault(r.ticker,
                                 {"group": f"watch:{wl_name}", "rank": i})
    return ranks


def compute_run_ranks_by_mode(
    results: list[PositionAnalysis],
    watchlists: Optional[dict[str, list[PositionAnalysis]]] = None,
) -> dict[str, dict[str, dict]]:
    """compute_run_ranks for every base-score mode: {mode: {ticker: rank}}, so
    each mode's view of the report gets its own rank-movement badges."""
    return {m: compute_run_ranks(results, watchlists, mode=m)
            for m in BASE_SCORE_MODES}


def _rank_record(entry: dict, mode: str, run_date: Optional[str] = None) -> dict:
    """The ledger's rank fields for one base-score mode. Composite ranks live
    on the entry itself (they predate modes, so older ledgers keep working);
    each other mode gets a `mode_ranks` sub-record. Passing run_date creates a
    missing record, stamped with the day tracking began (`since`)."""
    if mode == "composite":
        return entry
    records = entry.get("mode_ranks") or {}
    if run_date is None:
        return records.get(mode) or {}
    entry["mode_ranks"] = records
    return records.setdefault(mode, {"since": run_date})


def _roll_rank(record: dict, cur_rank: dict, run_date: str) -> None:
    """Store today's rank in a rank record. The first run of a new day first
    rolls the previous run's rank into the prior-day baseline; same-day
    re-runs keep it, so every run today compares to yesterday."""
    if record.get("last_rank_date") != run_date:
        record["prev_day_rank"] = record.get("last_rank")
        record["prev_day_rank_group"] = record.get("last_rank_group")
    record["last_rank"] = cur_rank.get("rank")
    record["last_rank_group"] = cur_rank.get("group")
    record["last_rank_date"] = run_date


def _attach_rank_moves(
    history: dict,
    ranks_by_mode: dict[str, dict[str, dict]],
    results: list[PositionAnalysis],
    watchlists: Optional[dict[str, list[PositionAnalysis]]] = None,
    run_date: Optional[str] = None,
) -> None:
    """Compare this run's ranks (per base-score mode, from
    compute_run_ranks_by_mode) against the rank recorded on the *previous
    calendar day* (not merely the previous run) and stash the movement on each
    analysis object for the ticker badge: `r._rank_moves` = {mode: {"delta":
    places_gained, "new": bool}}, and `r._rank_move` for the run's mode.
    Positive delta = climbed. `new` marks a ticker with no comparable prior-day
    rank (first sighting, or it changed group).

    Comparing to the prior day (rather than the prior run) means the two runs on
    the same trading day — 9:30 AM and 4 PM — both show movement relative to
    yesterday's close-of-day ranking, instead of the afternoon run diffing only
    against the morning run. MUST run before update_recs_history rolls the
    baseline forward.

    Each mode compares only against its own rank history. A mode with no
    history before today shows no badge for known tickers: that says nothing
    about the stock."""
    if run_date is None:
        run_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    tickers = history.get("tickers", {})

    def _move_for(ticker: str, mode: str) -> Optional[dict]:
        cur = (ranks_by_mode.get(mode) or {}).get(ticker)
        if not cur:
            return None
        entry = tickers.get(ticker)
        if not entry:
            return {"delta": None, "new": True}
        record = _rank_record(entry, mode)
        # Baseline = rank as of the last run of a previous calendar day. If the
        # most recent stored run was itself on an earlier day, that run IS the
        # prior-day baseline; if it already ran earlier today, use the retained
        # prior-day baseline so both of today's runs compare to yesterday.
        if record.get("last_rank_date") != run_date:
            base_rank = record.get("last_rank")
            base_group = record.get("last_rank_group")
        else:
            base_rank = record.get("prev_day_rank")
            base_group = record.get("prev_day_rank_group")
        if base_rank is None:
            if (mode != "composite"
                    and record.get("since", run_date) == run_date
                    and entry.get("first_date") != run_date):
                return None
            return {"delta": None, "new": True}
        if base_group != cur["group"]:
            return {"delta": None, "new": True}
        return {"delta": base_rank - cur["rank"], "new": False}

    run_mode = base_score_mode()
    seen: set[str] = set()
    for r in [*results, *(r for items in (watchlists or {}).values() for r in items)]:
        if r.ticker in seen:
            continue
        seen.add(r.ticker)
        r._rank_moves = {m: _move_for(r.ticker, m) for m in BASE_SCORE_MODES}
        r._rank_move = r._rank_moves.get(run_mode)


# Fixed return horizons (calendar days from first sight) captured for durability
# grading, with a grace window that still counts as "the ~N-day mark" when run
# cadence or market holidays mean no run lands exactly on day N. An entry already
# older than N + grace when this shipped never gets that horizon: we lack the
# data, and a null is honest where a mislabelled longer return would not be.
_RETURN_HORIZONS_D = (30, 90, 180)
_HORIZON_GRACE_D = 7


def _refresh_outcome_fields(
    entry: dict, price: Optional[float], run_date: str, sp_level: Optional[float],
) -> None:
    """Update an existing ledger entry's outcome-tracking fields, in place: the
    latest S&P level (for alpha), the peak and the post-peak trough (a new high
    restarts the give-back window, so peak→trough is the round-trip depth), and
    the fixed-horizon returns stamped by the first run landing in each horizon's
    window. Self-contained (no PositionAnalysis) so it unit-tests directly.

    sp_last is refreshed only for entries that already carry a first-sight level,
    so a pre-existing entry can't gain a level mid-window and mis-state its alpha
    (we can't recover the index level as of a first sight that predates capture)."""
    if sp_level is not None and entry.get("sp_at_first") is not None:
        entry["sp_last"] = sp_level
    if price is None:
        return
    # Peak, and the lowest price since the most recent peak.
    if price > entry.get("peak_price", 0):
        entry["peak_price"] = price
        entry["peak_date"] = run_date
        entry["trough_after_peak"] = price       # new high → give-back resets
        entry["trough_date"] = run_date
    else:
        tap = entry.get("trough_after_peak")
        if tap is None or price < tap:
            entry["trough_after_peak"] = price
            entry["trough_date"] = run_date
    # Fixed-horizon returns from first sight (stamped once, in-window only).
    fp, fd = entry.get("first_price"), entry.get("first_date")
    if not fp or fp <= 0 or not fd:
        return
    try:
        age = (datetime.strptime(run_date[:10], "%Y-%m-%d")
               - datetime.strptime(fd[:10], "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return
    ret_now = (price - fp) / fp * 100
    for h in _RETURN_HORIZONS_D:
        key = f"ret_{h}d"
        if entry.get(key) is None and h <= age <= h + _HORIZON_GRACE_D:
            entry[key] = round(ret_now, 2)
            entry[f"{key}_asof"] = run_date


def update_recs_history(
    history: dict,
    results: list[PositionAnalysis],
    watchlists: Optional[dict[str, list[PositionAnalysis]]] = None,
    run_date: Optional[str] = None,
    ranks_by_mode: Optional[dict[str, dict[str, dict]]] = None,
    sp_level: Optional[float] = None,
) -> dict:
    """Record the first sighting of every ticker we see (any verdict) and refresh
    latest price/alloc for already-tracked tickers. Mutates and returns
    `history`. The ranks of every base-score mode (from
    compute_run_ranks_by_mode) are persisted per ticker (see _rank_record),
    rolling a prior-DAY baseline forward on the first run of each new day so
    the next run can render rank-movement badges relative to yesterday.
    `sp_level` (current ^GSPC close) is frozen per ticker at first sight and
    refreshed after, so a miss can later be scored on excess return (alpha)
    rather than raw gain."""
    if run_date is None:
        run_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    if ranks_by_mode is None:
        ranks_by_mode = compute_run_ranks_by_mode(results, watchlists)
    tickers = history.setdefault("tickers", {})
    snapshot = _current_rec_snapshot(results, watchlists)

    def _mode_ranks(entry: dict, ticker: str) -> None:
        for mode in BASE_SCORE_MODES:
            if mode != "composite":
                _roll_rank(_rank_record(entry, mode, run_date),
                           (ranks_by_mode.get(mode) or {}).get(ticker) or {},
                           run_date)

    for ticker, cur in snapshot.items():
        entry = tickers.get(ticker)
        cur_rank = (ranks_by_mode.get("composite") or {}).get(ticker) or {}
        if entry is None:
            # Track every ticker from its first sighting, whatever the verdict.
            tickers[ticker] = {
                "name": cur["name"],
                "sector": cur["sector"],
                "first_date": run_date,
                "first_price": cur["price"],
                "first_verdict": cur["verdict"],
                "first_verdict_score": cur.get("verdict_score"),
                "first_alloc": cur["alloc"],
                "first_why": cur.get("why", ""),
                "first_news": cur.get("news"),
                "first_factors": cur.get("factors") or {},
                "last_date": run_date,
                "last_price": cur["price"],
                "last_verdict": cur["verdict"],
                "last_verdict_score": cur.get("verdict_score"),
                "last_alloc": cur["alloc"],
                "last_held_date": run_date if (cur["alloc"] or 0) > 0 else None,
                "last_why": cur.get("why", ""),
                "last_news": cur.get("news"),
                "last_factors": cur.get("factors") or {},
                "peak_price": cur["price"],
                "peak_date": run_date,
                # Outcome tracking (durability + alpha). trough_after_peak is the
                # lowest price since the most recent peak; sp_at_first freezes the
                # index level at first sight so alpha is a query, not a re-fetch.
                "trough_after_peak": cur["price"],
                "trough_date": run_date,
                "sp_at_first": sp_level,
                "sp_last": sp_level,
                "ret_30d": None, "ret_30d_asof": None,
                "ret_90d": None, "ret_90d_asof": None,
                "ret_180d": None, "ret_180d_asof": None,
                "last_rank": cur_rank.get("rank"),
                "last_rank_group": cur_rank.get("group"),
                "last_rank_date": run_date,
                "prev_rank": None,
                "prev_rank_group": None,
                # Daily baseline: the rank as of the last run of a prior day,
                # what the ▲/▼ badge diffs against. None until a new day runs.
                "prev_day_rank": None,
                "prev_day_rank_group": None,
            }
            _mode_ranks(tickers[ticker], ticker)
            continue
        # Refresh latest snapshot for an already-tracked ticker.
        entry["name"] = cur["name"] or entry.get("name")
        entry["sector"] = cur["sector"] or entry.get("sector")
        entry["last_date"] = run_date
        entry["last_price"] = cur["price"]
        entry["last_verdict"] = cur["verdict"]
        entry["last_verdict_score"] = cur.get("verdict_score")
        entry["last_alloc"] = cur["alloc"]
        if (cur["alloc"] or 0) > 0:
            # When it was last actually a position — what recently_held_tickers
            # keeps in the universe after it stops being one.
            entry["last_held_date"] = run_date
        entry["last_why"] = cur.get("why", "")
        entry["last_news"] = cur.get("news")
        entry["last_factors"] = cur.get("factors") or {}
        # Backfill the first-sight diagnostics for entries created before this
        # field existed — but only while the verdict is unchanged, so the current
        # reasoning still represents the original sighting (don't misattribute a
        # later ADD's reasoning to an original HOLD).
        if not entry.get("first_why") and entry.get("first_verdict") == cur["verdict"]:
            entry["first_why"] = cur.get("why", "")
        if entry.get("first_news") is None and entry.get("first_verdict") == cur["verdict"]:
            entry["first_news"] = cur.get("news")
        # Same rule for factors: only stamp the first-sight snapshot onto an
        # older entry while the verdict still matches, so a backfilled value
        # can't be mistaken for a genuine reading from the original sighting.
        if not entry.get("first_factors") and entry.get("first_verdict") == cur["verdict"]:
            entry["first_factors"] = cur.get("factors") or {}
        # Peak/trough (round-trip depth), fixed-horizon returns, and the latest
        # S&P level for alpha — all derived from the entry itself.
        _refresh_outcome_fields(entry, cur["price"], run_date, sp_level)
        # Rank (composite, on the entry itself): prev_rank still tracks the
        # immediately-previous run (kept for history); _roll_rank rolls the
        # daily baseline forward on the first run of a new day.
        entry["prev_rank"] = entry.get("last_rank")
        entry["prev_rank_group"] = entry.get("last_rank_group")
        _roll_rank(entry, cur_rank, run_date)
        _mode_ranks(entry, ticker)
    return history


def save_recs_history(history: dict, path: str = RECS_HISTORY_FILE) -> None:
    """Write the ledger back out (pretty-printed for clean git diffs)."""
    try:
        with open(path, "w") as f:
            json.dump(history, f, indent=2, sort_keys=True)
            f.write("\n")
    except OSError as e:
        print(f"[history] Could not save {path}: {e}")


def _missed_reason_diagnosis(
    *, gain_pct: float, first_verdict: Optional[str], last_verdict: Optional[str],
    first_score: Optional[float], last_score: Optional[float],
    first_factors: dict, last_factors: dict,
    first_why: str, last_why: str, last_alloc: float,
) -> str:
    """The insight core of a miss reason. Answers three questions the flat prose
    couldn't, using the logged score/factor trajectory:

      1. Did the model ever catch on? — score (or, absent that, composite)
         movement vs the price move. A score that sat flat while the price ran
         double digits is the real indictment; one that climbed to buy-grade
         "only after the run" is a timing problem, not a blindness problem.
      2. Where does it stand now? — distance to the buy bar, or a live-call flag
         when it's buy-grade today with token allocation.
      3. What capped it, and was it a value entry? — the persistent drag plus a
         52-week-low note (the setup the trend rule discounts).

    Returns HTML beginning with <br> (possibly empty-ish fallback). Degrades
    gracefully: entries logged before factor capture fall back to drag prose."""
    esc = _miss_esc
    BUY = BUY_GRADE_VERDICTS
    # Buy bar depends on vocabulary: holdings cross into ADD at 78, watchlist
    # names into BUY at 75. Infer which from the current verdict's family.
    holding_vocab = {"ADD", "HOLD", "TRIM", "SELL"}
    is_holding = (last_verdict in holding_vocab) or (first_verdict in holding_vocab)
    bar, buy_label = (78.0, "ADD") if is_holding else (75.0, "BUY")

    clauses: list[str] = []

    # ---- 1. Did the model catch on? (prefer verdict score, else composite) ----
    fc = first_factors.get("composite_score")
    lc = last_factors.get("composite_score")
    traj = ""
    if first_score is not None and last_score is not None:
        move = last_score - first_score
        if last_score >= bar:
            traj = (f"Its score climbed <strong>{first_verdict} {first_score:.0f} → "
                    f"{last_verdict} {last_score:.0f}</strong>, clearing the {buy_label} "
                    f"bar — but only after the {_fmt_pct(gain_pct, 0, True)} run.")
        elif abs(move) < 4:
            traj = (f"Its verdict score barely moved (<strong>{first_score:.0f} → "
                    f"{last_score:.0f}</strong>) while the price ran "
                    f"{_fmt_pct(gain_pct, 0, True)} — the model never registered the move.")
        elif move >= 4:
            traj = (f"Its score firmed <strong>{first_score:.0f} → {last_score:.0f}</strong> "
                    f"but stayed under the {bar:.0f} {buy_label} bar — it warmed up, "
                    f"late and not enough.")
        else:
            traj = (f"Its score actually slipped <strong>{first_score:.0f} → "
                    f"{last_score:.0f}</strong> as the price ran "
                    f"{_fmt_pct(gain_pct, 0, True)}.")
    elif fc is not None and lc is not None:
        cmove = lc - fc
        if abs(cmove) < 3:
            traj = (f"Its composite score held flat (~<strong>{lc:.0f}</strong>) while the "
                    f"price ran {_fmt_pct(gain_pct, 0, True)} — the fundamentals read "
                    f"never caught the move.")
        elif cmove >= 3:
            traj = (f"Its composite firmed <strong>{fc:.0f} → {lc:.0f}</strong>, but the "
                    f"read lagged the {_fmt_pct(gain_pct, 0, True)} price move.")
        else:
            traj = (f"Its composite eased <strong>{fc:.0f} → {lc:.0f}</strong> even as the "
                    f"price ran {_fmt_pct(gain_pct, 0, True)}.")
    elif lc is not None:
        traj = (f"Its composite score sits at <strong>{lc:.0f}</strong> — mid-pack, never "
                f"into conviction — while the price ran {_fmt_pct(gain_pct, 0, True)}.")
    if traj:
        clauses.append("<strong>Did the model catch it?</strong> " + traj)

    # ---- 2. Where it stands now: live call, or distance to the bar ----
    if last_verdict in BUY and last_alloc < STILL_ACTIONABLE_ALLOC_PCT:
        alloc_txt = ("no position" if last_alloc <= 0
                     else f"just {last_alloc:.1f}%")
        clauses.append(f"<strong>Live now:</strong> rated <strong>{esc(last_verdict)}</strong> "
                       f"today with {alloc_txt} — the call is current, not hindsight.")
    elif last_score is not None and last_score < bar:
        gap = bar - last_score
        clauses.append(f"<strong>Standing:</strong> {esc(last_verdict or '—')} "
                       f"{last_score:.0f}, still {gap:.0f} short of the {bar:.0f} "
                       f"{buy_label} bar.")

    # ---- 3. What capped it + value-entry note ----
    w52f = first_factors.get("week52_position")
    if w52f is not None and w52f <= 25:
        clauses.append(f"Entered near its 52-week low (<strong>{w52f:.0f}%</strong> of range) "
                       f"— a value setup the trend rule discounts.")
    elif w52f is not None and w52f >= 92:
        clauses.append(f"Was already near its 52-week high (<strong>{w52f:.0f}%</strong>) when "
                       f"flagged, which capped the score.")
    drag = last_why or first_why
    if drag and not traj:
        # Only fall back to raw drag prose when we produced no trajectory insight
        # (older entries with no logged scores/factors) — otherwise it's noise.
        clauses.append(f"<strong>Held back by</strong> {esc(drag)}.")

    return ("<br>" + "<br>".join(clauses)) if clauses else ""


def _alpha_fields(e: dict, move_pct: float) -> dict:
    """Derived outcome metrics shared by the missed / avoided rows, all computed
    from the ledger's captured levels — every one is None when the entry predates
    the capture (older ledger) or the benchmark was unavailable:

      • sp_return_pct          — the S&P's own move over the same window
                                 (sp_at_first → sp_last)
      • alpha_pct              — the name's move minus the S&P's: the real "left
                                 on the table". A +8% miss in a +10% tape is
                                 NEGATIVE alpha — market beta, not a miss.
      • max_drawdown_from_peak — peak → post-peak trough give-back: the durability
                                 read (did the run hold, or round-trip?).
      • ret_30d/90d/180d       — fixed-horizon returns from first sight.

    These are what let step 2's grade rank misses by signal (alpha, durability)
    instead of by raw, unbenchmarked gain."""
    spf, spl = e.get("sp_at_first"), e.get("sp_last")
    sp_ret = ((spl - spf) / spf * 100) if (spf and spl and spf > 0) else None
    peak, trough = e.get("peak_price"), e.get("trough_after_peak")
    dd = ((peak - trough) / peak * 100) if (peak and trough and peak > 0) else None
    return {
        "sp_return_pct": sp_ret,
        "alpha_pct": (move_pct - sp_ret) if sp_ret is not None else None,
        "max_drawdown_from_peak": dd,
        "ret_30d": e.get("ret_30d"),
        "ret_90d": e.get("ret_90d"),
        "ret_180d": e.get("ret_180d"),
    }


def _snapshot_verdicts(factors: Optional[dict]) -> dict[str, tuple]:
    """{mode: (label, score)} for one ledger snapshot (first sight or latest),
    from the per-mode verdicts _rec_factors logs.

    This is what lets the Missed-Opportunities tables answer the question the
    base switch raises — would another base have caught this? Empty for
    snapshots taken before that logging landed, and for ETF/thematic names
    whose verdict doesn't depend on the base; `_snapshot_label` then falls back
    to the snapshot's single verdict."""
    stored = (factors or {}).get("verdicts") or {}
    out: dict[str, tuple] = {}
    for mode in BASE_SCORE_MODES:
        pair = stored.get(mode)
        if isinstance(pair, (list, tuple)) and pair:
            out[mode] = (pair[0], pair[1] if len(pair) > 1 else None)
    return out


def _snapshot_label(by_mode: dict, mode: str, fallback: Optional[str]) -> Optional[str]:
    """`mode`'s verdict label in a snapshot, falling back to its single stored
    verdict when that mode wasn't logged separately."""
    pair = by_mode.get(mode)
    return pair[0] if pair else fallback


def recently_held_tickers(history: Optional[dict],
                          within_days: int = PIN_RECENT_HOLDINGS_DAYS,
                          today: Optional[date] = None) -> dict[str, str]:
    """{ticker: name} for names held within `within_days` that the ledger has
    stopped seeing as a position.

    Finding 5: a name you sell leaves the holdings, and if it is also not on a
    watchlist the run stops analyzing it — so the ledger's last_price freezes
    at the day it dropped out and every later move is invisible. That is not a
    scoring error, it is the universe quietly shrinking: META left the
    Screening list twice in August 2026, once at the lowest price of the
    window, and simply stopped being looked at. Re-adding these keeps the
    position you just exited under the same scrutiny as the ones you hold.

    `last_held_date` is stamped by update_recs_history whenever allocation is
    above zero. An entry that predates that field falls back to first_date when
    the first sighting was a real position, so names sold before this shipped
    are still picked up."""
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    out: dict[str, str] = {}
    for ticker, entry in ((history or {}).get("tickers") or {}).items():
        if (entry.get("last_alloc") or 0) > 0:
            continue                      # still held — already in the universe
        held_on = entry.get("last_held_date")
        if not held_on and (entry.get("first_alloc") or 0) > 0:
            held_on = entry.get("first_date")
        if not held_on:
            continue
        try:
            age = (today - date.fromisoformat(held_on[:10])).days
        except (ValueError, TypeError):
            continue
        if 0 <= age <= within_days:
            out[ticker] = entry.get("name") or ticker
    return out


def compute_missed_opportunities(history: dict) -> list[dict]:
    """From the ledger, return tickers that ran up >= MISSED_OPP_GAIN_PCT since
    they were first seen while we still hold below MISSED_OPP_ALLOC_THRESHOLD.
    Sorted by current gain (largest miss first)."""
    def _esc_txt(s: object) -> str:
        return (str(s).replace("&", "&amp;")
                .replace("<", "&lt;").replace(">", "&gt;"))

    out: list[dict] = []
    for ticker, e in (history.get("tickers") or {}).items():
        first_price = e.get("first_price")
        last_price = e.get("last_price")
        if not first_price or not last_price or first_price <= 0:
            continue
        gain_pct = (last_price - first_price) / first_price * 100
        if gain_pct < MISSED_OPP_GAIN_PCT:
            continue
        last_alloc = e.get("last_alloc") or 0.0
        if last_alloc >= MISSED_OPP_ALLOC_THRESHOLD:
            continue
        peak_price = e.get("peak_price") or last_price
        peak_gain_pct = (peak_price - first_price) / first_price * 100
        verdict = e.get("first_verdict")
        date_str = _fmt_short_date(e.get("first_date"))
        when = f" on {date_str}" if date_str else ""
        # Lead-in: a buy-type first verdict reads as "Flagged BUY"; anything else
        # (HOLD, WATCH-list pass, no verdict, etc.) is just "First seen".
        if verdict in REC_VERDICT_LABELS:
            lead = f"Flagged <strong>{verdict}</strong>{when}"
        elif verdict:
            lead = f"First seen{when} (verdict: {verdict})"
        else:
            lead = f"First seen{when}"
        if last_alloc <= 0:
            hold_part = "you never added it to the portfolio"
        else:
            hold_part = (f"you only hold {last_alloc:.1f}% of the portfolio "
                         f"(under the {MISSED_OPP_ALLOC_THRESHOLD:g}% bar)")
        reason = (f"{lead} at {_fmt_money(first_price)}; now "
                  f"{_fmt_pct(gain_pct, 0, True)} "
                  f"(${first_price:,.2f} → ${last_price:,.2f}), but {hold_part}.")
        if peak_gain_pct - gain_pct >= 5:
            reason += f" Was up as much as {_fmt_pct(peak_gain_pct, 0, True)} at its peak."

        # --- The core diagnosis: did the model ever catch on? Built from the
        #     logged score/factor trajectory (first sight → now), not just the
        #     current drag prose — this is what turns "why it wasn't a buy" from
        #     a snapshot into an insight. ---
        last_verdict = e.get("last_verdict")
        reason += _missed_reason_diagnosis(
            gain_pct=gain_pct,
            first_verdict=verdict, last_verdict=last_verdict,
            first_score=e.get("first_verdict_score"),
            last_score=e.get("last_verdict_score"),
            first_factors=e.get("first_factors") or {},
            last_factors=e.get("last_factors") or {},
            first_why=(e.get("first_why") or "").strip(),
            last_why=(e.get("last_why") or "").strip(),
            last_alloc=last_alloc,
        )

        # --- News catalyst (the "check the news and compile" part). Pulled from
        #     the ledger's stored sentiment, refreshed each run by
        #     score_news_sentiment (Claude when keyed, else a free lexicon). The
        #     lexicon's rationale is a bland word-count, so fall back to the most
        #     relevant headline as the concrete catalyst in that case. ---
        news = e.get("last_news") or e.get("first_news")
        if news and (news.get("rationale") or news.get("label") or news.get("headline")):
            lbl = _esc_txt((news.get("label") or "").strip())
            rat = (news.get("rationale") or "").strip()
            hl = (news.get("headline") or "").strip()
            if "signal words" in rat.lower() or not rat:
                body = f"&ldquo;{_esc_txt(hl)}&rdquo;" if hl else _esc_txt(rat)
            else:
                body = _esc_txt(rat)
                if hl and hl.lower() not in rat.lower():
                    body += f" &mdash; &ldquo;{_esc_txt(hl)}&rdquo;"
            if body:
                head = f"📰 News{(' ' + lbl) if lbl else ''}:"
                reason += f"<br><strong>{head}</strong> {body}"

        # --- Classification for "further analysis" (all derivable from the
        #     ledger, no re-derivation): split a model gap (never rated a buy —
        #     the score genuinely missed it) from an execution gap (rated a buy
        #     at some point, but allocation never followed). These are different
        #     failures with different fixes, so the report shouldn't conflate
        #     them. `still_actionable` marks a call that is STILL live today. ---
        #     Classified per base-score mode, because a name the Composite base
        #     never rated a buy may well have been an ADD under Quality — which
        #     is exactly what the reader is switching bases to find out.
        first_by_mode = _snapshot_verdicts(e.get("first_factors"))
        last_by_mode = _snapshot_verdicts(e.get("last_factors"))

        def _classify(mode):
            fl = _snapshot_label(first_by_mode, mode, verdict)
            ll = _snapshot_label(last_by_mode, mode, last_verdict)
            ever_buy = fl in BUY_GRADE_VERDICTS or ll in BUY_GRADE_VERDICTS
            return ("execution" if ever_buy else "model",
                    ll in BUY_GRADE_VERDICTS and last_alloc < STILL_ACTIONABLE_ALLOC_PCT)

        by_mode = {m: _classify(m) for m in BASE_SCORE_MODES}
        miss_type_by_mode = {m: t for m, (t, _) in by_mode.items()}
        actionable_by_mode = {m: a for m, (_, a) in by_mode.items()}
        # The run's own mode stays the headline value the ledger stats, the AI
        # post-mortem and the summary counts read.
        run_mode = base_score_mode()
        miss_type = miss_type_by_mode.get(run_mode)
        still_actionable = actionable_by_mode.get(run_mode, False)

        # --- Numeric factors as of first sight (populated by _rec_factors on
        #     runs after that logging landed; older entries fall back to the
        #     latest snapshot, then to None so the cell renders "—"). Surfacing
        #     these as sortable columns is what makes the table analysable
        #     rather than just readable. ---
        ff = e.get("first_factors") or {}
        lf = e.get("last_factors") or {}

        def _factor(key):
            v = ff.get(key)
            return v if v is not None else lf.get(key)

        out.append({
            "ticker": ticker,
            "name": e.get("name") or ticker,
            "sector": e.get("sector"),
            "first_date": e.get("first_date"),
            "first_verdict": e.get("first_verdict"),
            "first_verdict_score": e.get("first_verdict_score"),
            "last_verdict": e.get("last_verdict"),
            "last_verdict_score": e.get("last_verdict_score"),
            "first_price": first_price,
            "current_price": last_price,
            "gain_pct": gain_pct,
            "peak_gain_pct": peak_gain_pct,
            "gave_back_pct": max(0.0, peak_gain_pct - gain_pct),
            "last_alloc": last_alloc,
            "reason": reason,
            "miss_type": miss_type,
            "still_actionable": still_actionable,
            "miss_type_by_mode": miss_type_by_mode,
            "still_actionable_by_mode": actionable_by_mode,
            "first_verdicts": first_by_mode,
            "last_verdicts": last_by_mode,
            "w52_first": _factor("week52_position"),
            "trend_first": _factor("trend"),
            "composite_first": _factor("composite_score"),
            **_alpha_fields(e, gain_pct),
        })
    out.sort(key=lambda d: d["gain_pct"], reverse=True)
    return out


def compute_avoided_losses(history: dict) -> list[dict]:
    """The symmetric companion to compute_missed_opportunities: tracked names
    that FELL >= MISSED_OPP_GAIN_PCT since first seen while we stayed under the
    allocation bar. Without this the Missed-Opportunities section only ever
    shows the upside tail of a two-sided market and reads as pure model failure;
    the same low-allocation discipline that "missed" the winners also sidestepped
    these. Sorted by biggest drop first.

    Each row is tagged `dodge_type`: 'caution' when the analyzer was already wary
    at first sight (a correct call), or 'lucky' when it actually rated the name a
    buy and only low allocation saved us (a call that was wrong, surfaced honestly
    rather than hidden)."""
    def _esc_txt(s: object) -> str:
        return (str(s).replace("&", "&amp;")
                .replace("<", "&lt;").replace(">", "&gt;"))

    out: list[dict] = []
    for ticker, e in (history.get("tickers") or {}).items():
        first_price = e.get("first_price")
        last_price = e.get("last_price")
        if not first_price or not last_price or first_price <= 0:
            continue
        move_pct = (last_price - first_price) / first_price * 100
        if move_pct > -MISSED_OPP_GAIN_PCT:      # only real drops qualify
            continue
        last_alloc = e.get("last_alloc") or 0.0
        if last_alloc >= MISSED_OPP_ALLOC_THRESHOLD:
            continue
        verdict = e.get("first_verdict")
        last_verdict = e.get("last_verdict")
        trough_price = e.get("trough_price")     # optional; may not be tracked
        date_str = _fmt_short_date(e.get("first_date"))
        when = f" on {date_str}" if date_str else ""

        # Per base-score mode, so the badge stays truthful under the switch:
        # a name the Composite base never liked may have been a buy under
        # Quality, which makes it a lucky dodge in that view and not a correct
        # one. The run's mode supplies the headline value.
        first_by_mode = _snapshot_verdicts(e.get("first_factors"))
        last_by_mode = _snapshot_verdicts(e.get("last_factors"))
        dodge_by_mode = {
            m: ("lucky" if _snapshot_label(first_by_mode, m, verdict)
                in BUY_GRADE_VERDICTS else "caution")
            for m in BASE_SCORE_MODES}
        dodge_type = dodge_by_mode.get(
            base_score_mode(),
            "lucky" if verdict in BUY_GRADE_VERDICTS else "caution")
        was_buy = dodge_type == "lucky"
        if verdict in REC_VERDICT_LABELS:
            lead = f"Flagged <strong>{verdict}</strong>{when}"
        elif verdict:
            lead = f"First seen{when} (verdict: {verdict})"
        else:
            lead = f"First seen{when}"
        reason = (f"{lead} at {_fmt_money(first_price)}; now "
                  f"{_fmt_pct(move_pct, 0, True)} "
                  f"(${first_price:,.2f} → ${last_price:,.2f}).")
        if was_buy:
            reason += ("<br><strong>Wrong call, dodged anyway:</strong> the "
                       "analyzer rated this a buy — only staying under-allocated "
                       "avoided the loss.")
        else:
            drag = (e.get("last_why") or e.get("first_why") or "").strip()
            reason += ("<br><strong>Caution held up:</strong> never a buy signal"
                       + (f" — flagged {_esc_txt(drag)}" if drag else "") + ".")

        ff = e.get("first_factors") or {}
        lf = e.get("last_factors") or {}

        def _factor(key):
            v = ff.get(key)
            return v if v is not None else lf.get(key)

        out.append({
            "ticker": ticker,
            "name": e.get("name") or ticker,
            "sector": e.get("sector"),
            "first_date": e.get("first_date"),
            "first_verdict": verdict,
            "first_verdict_score": e.get("first_verdict_score"),
            "last_verdict": last_verdict,
            "last_verdict_score": e.get("last_verdict_score"),
            "first_price": first_price,
            "current_price": last_price,
            "loss_pct": move_pct,
            "last_alloc": last_alloc,
            "dodge_type": dodge_type,
            "dodge_type_by_mode": dodge_by_mode,
            "first_verdicts": first_by_mode,
            "last_verdicts": last_by_mode,
            "reason": reason,
            "w52_first": _factor("week52_position"),
            "trend_first": _factor("trend"),
            "composite_first": _factor("composite_score"),
            **_alpha_fields(e, move_pct),
        })
    out.sort(key=lambda d: d["loss_pct"])
    return out


def compute_missed_opp_insights(
    history: dict, missed: list[dict], avoided: list[dict]
) -> dict:
    """Summary statistics for the Missed-Opportunities header strip.

    The point is context the raw table can't give: a "miss" only means something
    against a base rate. If 27% of everything we track rose >=5% and 17% fell
    >=5%, then a handful of missed winners is roughly what a two-sided market
    hands out — not proof the model is broken. This computes that base rate over
    the whole tracked universe, plus the model-gap / execution-gap split and the
    still-actionable count, so the reader can size the signal before reading
    a single row."""
    tickers = (history.get("tickers") or {})
    valid = up5 = down5 = 0
    first_dates: list[str] = []
    last_dates: list[str] = []
    for e in tickers.values():
        fp, lp = e.get("first_price"), e.get("last_price")
        if not fp or not lp or fp <= 0:
            continue
        valid += 1
        g = (lp - fp) / fp * 100
        if g >= MISSED_OPP_GAIN_PCT:
            up5 += 1
        elif g <= -MISSED_OPP_GAIN_PCT:
            down5 += 1
        if e.get("first_date"):
            first_dates.append(e["first_date"][:10])
        if e.get("last_date"):
            last_dates.append(e["last_date"][:10])

    gains = sorted(m["gain_pct"] for m in missed)
    n = len(gains)
    mean_gain = sum(gains) / n if n else 0.0
    median_gain = (gains[n // 2] if n % 2 else
                   (gains[n // 2 - 1] + gains[n // 2]) / 2) if n else 0.0

    model_gap = sum(1 for m in missed if m.get("miss_type") == "model")
    exec_gap = sum(1 for m in missed if m.get("miss_type") == "execution")
    actionable = [m for m in missed if m.get("still_actionable")]
    # The same two counts under every base, so the header strip keeps pace with
    # the switch instead of stating the run's mode's split over swapped badges.
    by_mode = {}
    for mode in BASE_SCORE_MODES:
        types = [(m.get("miss_type_by_mode") or {}).get(mode, m.get("miss_type"))
                 for m in missed]
        by_mode[mode] = {
            "model_gap": sum(1 for t in types if t == "model"),
            "execution_gap": sum(1 for t in types if t == "execution"),
            "still_actionable": [
                m["ticker"] for m in missed
                if (m.get("still_actionable_by_mode") or {}).get(
                    mode, m.get("still_actionable"))],
        }

    return {
        "tracked_valid": valid,
        "up5": up5, "down5": down5,
        "up5_pct": (up5 / valid * 100) if valid else 0.0,
        "down5_pct": (down5 / valid * 100) if valid else 0.0,
        "missed_count": n,
        "avoided_count": len(avoided),
        "mean_gain": mean_gain,
        "median_gain": median_gain,
        "model_gap": model_gap,
        "execution_gap": exec_gap,
        "still_actionable": [m["ticker"] for m in actionable],
        "by_mode": by_mode,
        "window_start": min(first_dates) if first_dates else None,
        "window_end": max(last_dates) if last_dates else None,
    }


# ---------------------------------------------------------------------------
# On-demand AI post-mortem
# ---------------------------------------------------------------------------
# The narrative layer that sits ON TOP of the deterministic scorecard: it feeds
# the ALREADY-COMPUTED miss rows (alpha, horizon returns, factor snapshots, the
# why-not-a-buy reasoning) to Claude and asks for the cross-cutting patterns and
# model-improvement hypotheses that per-row stats can't express. It is triggered
# explicitly (--analyze-misses / a direct call), NOT on the twice-daily CI run —
# so the report stays deterministic and key-free, and the AI never produces the
# grade/numbers, only reasons over them. Mirrors score_news_sentiment's "Claude
# when keyed, graceful skip otherwise" shape.

_MISS_ANALYSIS_SYSTEM = (
    "You are a quantitative post-mortem analyst reviewing a personal stock-"
    "screening model's MISSED OPPORTUNITIES: names it tracked but rated below its "
    "buy bar (or that the user under-allocated) which then rose. Your job is "
    "strictly retrospective — find the systematic reasons behind the misses and "
    "propose concrete improvements to the SCORING MODEL and the allocation "
    "process.\n\n"
    "Ground rules:\n"
    "- This is analysis of a model's past decisions, not investment advice. Never "
    "tell the user to buy, sell, or hold any specific security, and never predict "
    "prices. Frame findings as 'the model under-weighted X' or 'consider "
    "re-calibrating factor Y', never 'you should buy Z'.\n"
    "- Separate MODEL gaps (the score never flagged it) from EXECUTION gaps (rated "
    "a buy but under-sized) — different failures, different fixes.\n"
    "- Where an alpha figure is present, judge the miss on excess return vs the "
    "S&P, not raw gain: a gain below the market's move is beta, not a real miss — "
    "say so when the data shows it.\n"
    "- Be concrete and quantitative; cite tickers and the numbers you were given. "
    "Prefer a few high-conviction findings over an exhaustive list.\n"
    "- If the data is too thin to support a claim, say so rather than inventing one."
)


def _strip_html_to_text(s: str) -> str:
    """Flatten the pre-built reason HTML (it carries <strong>/<br>/entities) into
    one plain-text line, so the prompt reads as prose, not markup."""
    if not s:
        return ""
    import re as _re
    import html as _html
    s = _re.sub(r"(?i)<br\s*/?>", "; ", s)
    s = _re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    return _re.sub(r"\s+", " ", s).strip()


def _build_miss_analysis_prompt(
    missed: list[dict], avoided: list[dict], insights: dict
) -> str:
    """Render the computed miss/avoided rows + base-rate insights into a compact,
    plain-text brief for the model. Sends signal (alpha, horizons, factors, the
    reasoning), never raw prices — the model reasons over the grade, it doesn't
    recompute it."""
    def _n(v, dec=0, sign=False):
        if v is None:
            return "n/a"
        return f"{v:+.{dec}f}" if sign else f"{v:.{dec}f}"

    lines: list[str] = []
    if insights:
        lines.append(
            f"Tracked universe: {insights.get('tracked_valid', 0)} names with valid "
            f"prices. Two-sided base rate: {insights.get('up5', 0)} up ≥5% "
            f"({_n(insights.get('up5_pct'))}%), {insights.get('down5', 0)} down ≥5% "
            f"({_n(insights.get('down5_pct'))}%). "
            f"Misses flagged: {insights.get('missed_count', 0)} "
            f"(model gap {insights.get('model_gap', 0)}, execution gap "
            f"{insights.get('execution_gap', 0)}); mean +{_n(insights.get('mean_gain'))}%, "
            f"median +{_n(insights.get('median_gain'))}%. "
            f"Avoided losses (dodged ≥5% drops while under-allocated): "
            f"{insights.get('avoided_count', 0)}. "
            f"Window {insights.get('window_start')} → {insights.get('window_end')}."
        )

    # Data-coverage calibration. The alpha/horizon/durability capture is populated
    # only as the pipeline runs (and never back-fills for names first seen before
    # it shipped), so tell the model how much exists — otherwise it may lean on an
    # axis that's mostly 'n/a', or worse, infer alpha it doesn't have.
    na = sum(1 for m in missed if m.get("alpha_pct") is not None)
    nh = sum(1 for m in missed if any(m.get(f"ret_{h}d") is not None for h in (30, 90, 180)))
    nd = sum(1 for m in missed if m.get("max_drawdown_from_peak") is not None)
    if missed:
        lines.append(
            f"Data coverage: alpha available for {na}/{len(missed)} misses, horizon "
            f"returns for {nh}/{len(missed)}, peak-to-trough drawdown for "
            f"{nd}/{len(missed)}. Where a field reads 'n/a' it simply wasn't captured "
            f"yet — lean on the base rate, factors and reasoning there, and flag any "
            f"alpha- or durability-based claim as provisional rather than inferring it."
        )

    lines.append("\nMISSED OPPORTUNITIES:")
    for m in missed[:60]:            # defensive cap so the prompt can't balloon
        alpha = m.get("alpha_pct")
        alpha_txt = (f"alpha {_n(alpha, sign=True)}% vs S&P {_n(m.get('sp_return_pct'), sign=True)}%"
                     if alpha is not None else "alpha n/a (benchmark not yet captured)")
        dd = m.get("max_drawdown_from_peak")
        dd_txt = f", maxDD -{_n(dd)}% off peak" if dd is not None else ""
        hz = [f"{h}d {_n(m.get(f'ret_{h}d'), sign=True)}%"
              for h in (30, 90, 180) if m.get(f"ret_{h}d") is not None]
        hz_txt = (" [horizon " + ", ".join(hz) + "]") if hz else ""
        why = _strip_html_to_text(m.get("reason") or "")
        if len(why) > 420:
            why = why[:420] + "…"
        lines.append(
            f"- {m['ticker']} ({m.get('sector') or '—'}): {m.get('miss_type')} gap"
            + (", STILL LIVE (buy-grade today, under-held)" if m.get("still_actionable") else "")
            + f"; first seen {m.get('first_date')} as {m.get('first_verdict')} "
            f"(score {_n(m.get('first_verdict_score'))}) → now {m.get('last_verdict')} "
            f"(score {_n(m.get('last_verdict_score'))}), held {_n(m.get('last_alloc'), 1)}%; "
            f"gain +{_n(m.get('gain_pct'))}%, "
            f"{alpha_txt}{dd_txt}{hz_txt}; at first sight composite "
            f"{_n(m.get('composite_first'), 1)}, 52w-pos {_n(m.get('w52_first'))}%, "
            f"trend {m.get('trend_first') or '—'}. Why not a buy: {why}"
        )

    if avoided:
        lines.append("\nAVOIDED LOSSES (sample — names that FELL while under-allocated, "
                     "the mirror of the misses):")
        for a in avoided[:8]:
            why = _strip_html_to_text(a.get("reason") or "")
            if len(why) > 220:
                why = why[:220] + "…"
            lines.append(f"- {a['ticker']} ({a.get('sector') or '—'}): "
                         f"{a.get('dodge_type')} dodge, {_n(a.get('loss_pct'), sign=True)}%; {why}")

    lines.append(
        "\nWrite a post-mortem in Markdown with these sections:\n"
        "1. **Headline** — 2-3 sentences: are these misses real signal or the "
        "upside tail of a two-sided market? Judge against the base rate and, where "
        "present, alpha.\n"
        "2. **Systematic patterns** — the 2-4 recurring threads across the misses "
        "(shared sectors, factor signatures, the model-vs-execution split, catalyst "
        "types). Cite tickers.\n"
        "3. **The single most fixable gap** — name it, and the concrete change to "
        "the scoring model or allocation rule that would have caught it.\n"
        "4. **Watch-outs** — where the data is too thin to conclude, or where a "
        "'miss' is really beta rather than alpha.\n"
        "Keep it tight and quantitative. No personalized buy/sell advice."
    )
    return "\n".join(lines)


def analyze_missed_opportunities_ai(
    missed: list[dict],
    avoided: Optional[list[dict]] = None,
    insights: Optional[dict] = None,
    *,
    model: Optional[str] = None,
) -> Optional[str]:
    """On-demand AI post-mortem over the captured miss data. Returns a Markdown
    analysis, or None when no API key is configured or the call fails/declines.
    Best-effort and side-effect-free beyond the API call — the caller decides
    what to do with the text. NOT invoked by the normal report path."""
    if not missed:
        print("[miss-analysis] No missed opportunities to analyze.")
        return None
    client = _anthropic_client()
    if client is None:
        print("[miss-analysis] ANTHROPIC_API_KEY not set — skipping AI post-mortem. "
              "The deterministic scorecard (alpha, horizons, miss-type) is already in "
              "the report; set the key and re-run with --analyze-misses for the "
              "written analysis.")
        return None
    mdl = model or MISSED_OPP_MODEL
    prompt = _build_miss_analysis_prompt(missed, avoided or [], insights or {})
    print(f"[miss-analysis] Analyzing {len(missed)} missed opportunit"
          f"{'y' if len(missed) == 1 else 'ies'} with {mdl} …")
    try:
        # Generous timeout + max_tokens: a capable model may think before writing,
        # and thinking counts toward max_tokens — leave room so the prose isn't
        # truncated. No temperature (rejected by the Opus 5 family).
        resp = client.with_options(timeout=180.0).messages.create(
            model=mdl,
            max_tokens=12000,
            system=_MISS_ANALYSIS_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"[miss-analysis] AI call failed ({type(e).__name__}: {e}). "
              f"If it's a model-access error, set MISSED_OPP_MODEL to a model your "
              f"key can use.")
        return None
    if getattr(resp, "stop_reason", None) == "refusal":
        print("[miss-analysis] Model declined to analyze (refusal).")
        return None
    text = "".join(getattr(b, "text", "") for b in (resp.content or [])
                   if getattr(b, "type", None) == "text").strip()
    if not text:
        print("[miss-analysis] Empty analysis returned.")
        return None
    return text


_MISS_VERDICT_COLORS = {
    "ADD": "#27ae60", "BUY": "#27ae60", "BUY MORE": "#27ae60",
    "WATCH": "#2980b9", "HOLD": "#2c3e50", "TRIM": "#e67e22", "SELL": "#c0392b",
}


def _miss_esc(s: object) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _miss_reason_attr(reason_html: str) -> str:
    """Escape pre-built reason HTML for a double-quoted data-reason attribute.
    Straight double quotes must become &quot;; apostrophes are attr-safe."""
    return (reason_html.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _miss_verdict_chip(v_label: str, v_score: Optional[float]) -> str:
    color = _MISS_VERDICT_COLORS.get(v_label, "#7f8c8d")
    score_str = (f"<span class='vscore' style='color:var(--fg-body);font-size:11px;"
                 f"font-weight:700;margin-left:4px;font-variant-numeric:tabular-nums;'>"
                 f"{int(round(v_score))}</span>") if v_score is not None else ""
    return (f"<span class='verdict' style='background:{color};'>"
            f"{_miss_esc(v_label)}</span>{score_str}")


_MISS_BADGE_STYLE = ("display:inline-block;padding:2px 7px;border-radius:10px;"
                     "font-size:10px;font-weight:700;white-space:nowrap;")
_MISS_LIVE_PILL = ("<span title='Still rated buy-grade today with token allocation' "
                   "style='display:inline-block;margin-left:5px;padding:1px 5px;"
                   "border-radius:8px;font-size:9px;font-weight:700;"
                   "background:rgba(39,174,96,0.18);color:var(--pos-up);'>● LIVE</span>")


def _miss_type_badge(miss_type: str) -> str:
    """Model gap (never rated a buy) vs execution gap (rated a buy, never
    sized up) — different failures with different fixes."""
    if miss_type == "execution":
        return (f"<span style='{_MISS_BADGE_STYLE}background:rgba(41,128,185,0.15);"
                f"color:#2980b9;' title='Rated a buy at some point — allocation "
                f"never followed'>Didn&#39;t act</span>")
    return (f"<span style='{_MISS_BADGE_STYLE}background:rgba(230,126,34,0.15);"
            f"color:var(--fg-chip-amber);' title='Never rated a buy — the score "
            f"missed it'>Model gap</span>")


def _miss_dodge_badge(dodge_type: str) -> str:
    """Correct caution (never a buy) vs lucky dodge (rated a buy; only low
    allocation avoided the loss)."""
    if dodge_type == "lucky":
        return (f"<span style='{_MISS_BADGE_STYLE}background:rgba(230,126,34,0.15);"
                f"color:var(--fg-chip-amber);' title='Rated a buy — only low "
                f"allocation avoided the loss'>Lucky dodge</span>")
    return (f"<span style='{_MISS_BADGE_STYLE}background:rgba(39,174,96,0.15);"
            f"color:var(--pos-up);' title='Never a buy signal — the caution was "
            f"right'>Correct caution</span>")


def _miss_base_line(by_mode: dict, mode: str) -> str:
    """The muted "what the other bases called it" line under a verdict chip —
    the same readout the holdings hover-card carries, in table form. Empty when
    the snapshot has no per-mode verdicts logged."""
    bits = []
    for other in BASE_SCORE_MODES:
        if other == mode or other not in by_mode:
            continue
        lbl, sc = by_mode[other]
        name = BASE_SCORE_LABELS.get(other, other)
        score = f" {sc:.0f}" if sc is not None else ""
        bits.append(f"{name} base: {_miss_esc(lbl or '—')}{score}")
    return (f"<span class='miss-alt'>{' · '.join(bits)}</span>") if bits else ""


def _miss_verdict_td(by_mode: dict, label: Optional[str],
                     score: Optional[float]) -> str:
    """A missed/avoided verdict <td>: the viewed base's chip, with what the
    other bases called the same name underneath. Both follow the base switch,
    as does the column's sort value, so "would another base have caught this?"
    is answerable without leaving the table. Snapshots logged before per-mode
    verdicts existed render the single stored verdict, unswitched."""
    shown = label or "—"

    def _one(mode):
        pair = by_mode.get(mode)
        chip = (_miss_verdict_chip(pair[0] or "—", pair[1]) if pair
                else _miss_verdict_chip(shown, score))
        return chip + _miss_base_line(by_mode, mode)

    if by_mode:
        extra = "".join(
            f" data-sort-{m}='{_miss_esc(_snapshot_label(by_mode, m, shown))}'"
            for m in BASE_SCORE_MODES)
    else:
        # Says why this one chip sits still while the rest of the report
        # switches, rather than leaving it looking like a stuck cell.
        extra = (" title='Logged before the analyzer recorded a verdict per "
                 "base score, so this is the single verdict from that run&#39;s "
                 "own base — it does not switch.' style='cursor:help;'")
    return (f"<td data-sort='{_miss_esc(shown)}'{extra}>"
            f"{_mode_variants(_one)}</td>")


def _miss_factor_cell(row: dict) -> str:
    """Compact 'as of first sight' cell: composite score, 52-week position, and
    trend arrow — the numeric factors now logged per ledger entry. Renders '—'
    for entries first seen before factor logging landed. data-sort keys off the
    composite so the column sorts by conviction-at-sighting."""
    comp = row.get("composite_first")
    w52 = row.get("w52_first")
    trend = row.get("trend_first")
    if comp is None and w52 is None and trend is None:
        return "<td data-sort='-1'><span style='color:var(--fg-muted);'>—</span></td>"
    bits = []
    if comp is not None:
        bits.append(f"<span title='Composite score at first sight' "
                    f"style='font-weight:700;font-variant-numeric:tabular-nums;'>"
                    f"{comp:.0f}</span>")
    if w52 is not None:
        # Low in the 52w range = value entry; high = stretched. Tint the edges.
        c = ("var(--pos-up)" if w52 <= 20 else
             "var(--pos-down)" if w52 >= 92 else "var(--fg-muted)")
        bits.append(f"<span title='Position in 52-week range at first sight' "
                    f"style='color:{c};font-size:11px;'>52w {w52:.0f}%</span>")
    if trend:
        arrow = {"uptrend": "↑", "downtrend": "↓", "sideways": "→"}.get(trend, "")
        tc = ("var(--pos-up)" if trend == "uptrend" else
              "var(--pos-down)" if trend == "downtrend" else "var(--fg-muted)")
        bits.append(f"<span title='Trend at first sight' style='color:{tc};"
                    f"font-size:11px;'>{arrow} {_miss_esc(trend)}</span>")
    sort_key = comp if comp is not None else -1
    inner = "<div style='display:flex;flex-direction:column;gap:1px;'>" + \
            "".join(f"<span>{b}</span>" for b in bits) + "</div>"
    return f"<td data-sort='{sort_key:.1f}'>{inner}</td>"


def _miss_summary_strip(insights: Optional[dict]) -> str:
    """The header band above the table: base-rate context + the two counts that
    turn a flat list into a diagnosis. A 'miss' is only meaningful against how
    the whole tracked universe moved, so lead with that."""
    if not insights:
        return ""
    up5, down5 = insights["up5"], insights["down5"]
    valid = insights["tracked_valid"]
    per_mode = insights.get("by_mode") or {}

    def _gaps(mode):
        d = per_mode.get(mode) or insights
        return d["model_gap"], d["execution_gap"]

    def _actionable(mode):
        return (per_mode.get(mode) or insights)["still_actionable"]

    def card(big, label, tone="neutral", sub=""):
        tone_color = {"good": "var(--pos-up)", "bad": "var(--pos-down)",
                      "warn": "var(--fg-chip-amber)",
                      "neutral": "var(--fg-strong)"}.get(tone, "var(--fg-strong)")
        sub_html = (f"<div style='font-size:10px;color:var(--fg-muted);"
                    f"margin-top:2px;'>{sub}</div>") if sub else ""
        return (
            "<div style='flex:1 1 130px;min-width:130px;background:var(--bg-card);"
            "border:1px solid var(--border-medium);border-radius:8px;padding:12px 14px;'>"
            f"<div style='font-size:22px;font-weight:700;line-height:1;color:{tone_color};"
            f"font-variant-numeric:tabular-nums;'>{big}</div>"
            f"<div style='font-size:11px;color:var(--fg-muted);margin-top:4px;'>{label}</div>"
            f"{sub_html}</div>"
        )

    cards = "".join([
        card(f"{insights['missed_count']}", "Missed (up ≥5%, under-allocated)",
             "warn",
             f"mean +{insights['mean_gain']:.0f}% · median +{insights['median_gain']:.0f}%"),
        card(f"{up5}<span style='font-size:13px;color:var(--fg-muted);'> / {valid}</span>",
             "Tracked names up ≥5%", "good",
             f"{insights['up5_pct']:.0f}% of the universe — the base rate"),
        card(f"{down5}<span style='font-size:13px;color:var(--fg-muted);'> / {valid}</span>",
             "Tracked names down ≥5%", "bad",
             f"{insights['down5_pct']:.0f}% — the misses' mirror image"),
        # The gap split is a verdict readout, so it follows the base switch.
        _mode_variants(
            lambda mode: card(
                f"{_gaps(mode)[0]} <span style='font-size:13px;"
                f"color:var(--fg-muted);'>·</span> {_gaps(mode)[1]}",
                "Model gap · Execution gap", "neutral",
                "never a buy · rated buy, under-sized"),
            tag="div"),
    ])

    def _actionable_banner(mode):
        actionable = _actionable(mode)
        if not actionable:
            return ""
        shown = ", ".join(_miss_esc(t) for t in actionable[:8])
        more = f" +{len(actionable) - 8} more" if len(actionable) > 8 else ""
        return (
            "<div style='margin-top:12px;padding:10px 14px;border-radius:8px;"
            "background:rgba(39,174,96,0.08);border:1px solid rgba(39,174,96,0.35);"
            "font-size:12px;color:var(--fg-body);'>"
            "<strong style='color:var(--pos-up);'>● Still actionable:</strong> "
            f"{len(actionable)} name{'' if len(actionable) == 1 else 's'} the "
            f"analyzer <em>still</em> rates buy-grade today with under "
            f"{STILL_ACTIONABLE_ALLOC_PCT:g}% allocation — "
            f"<strong>{shown}{more}</strong>. These are live, not hindsight.</div>"
        )

    actionable_html = _mode_variants(_actionable_banner, tag="div")
    window = ""
    if insights.get("window_start") and insights.get("window_end"):
        window = (f"<span style='color:var(--fg-muted);font-size:11px;'>"
                  f"Tracking window {insights['window_start']} → "
                  f"{insights['window_end']}.</span>")
    return (
        "<div style='display:flex;flex-wrap:wrap;gap:10px;margin-bottom:6px;'>"
        + cards + "</div>" + actionable_html
        + (f"<div style='margin-top:8px;'>{window}</div>" if window else "")
    )


def _miss_md_to_html(md: str) -> str:
    """Minimal, safe Markdown → HTML for the embedded AI panel: escape first, then
    render only headings, **bold**, and bullet / numbered lists."""
    import re as _re
    import html as _html
    out: list[str] = []
    in_list = False

    def _close():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in (md or "").splitlines():
        if not raw.strip():
            _close()
            continue
        esc = _html.escape(raw.strip())
        esc = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc)
        h = _re.match(r"^(#{1,6})\s+(.*)$", esc)
        if h:
            _close()
            out.append(f"<h4 style='margin:16px 0 6px;font-size:15px;"
                       f"color:var(--fg-strong);'>{h.group(2)}</h4>")
            continue
        li = _re.match(r"^(?:[-*]|\d+\.)\s+(.*)$", esc)
        if li:
            if not in_list:
                out.append("<ul style='margin:6px 0;padding-left:22px;'>")
                in_list = True
            out.append(f"<li style='margin:4px 0;'>{li.group(1)}</li>")
            continue
        _close()
        out.append(f"<p style='margin:9px 0;'>{esc}</p>")
    _close()
    return "\n".join(out)


def _render_miss_analysis_panel(md: str) -> str:
    """Styled panel wrapping the embedded AI post-mortem — present only when the
    run used --analyze-misses (i.e. the header toggle was on for this refresh)."""
    return (
        "<div style='margin:16px 0 20px;max-width:900px;border:1px solid "
        "var(--border-medium);border-radius:12px;background:var(--bg-card);"
        "overflow:hidden;'>"
        "<div style='padding:10px 16px;font-size:12px;font-weight:700;"
        "color:var(--fg-muted);border-bottom:1px solid var(--border-medium);'>"
        "🔍 AI Post-Mortem</div>"
        "<div style='padding:6px 20px 8px;color:var(--fg-body);font-size:14px;"
        "line-height:1.65;'>" + _miss_md_to_html(md) + "</div>"
        "<div style='padding:0 20px 14px;font-size:11px;color:var(--fg-muted);"
        "font-style:italic;'>Retrospective model-improvement analysis, not "
        "investment advice.</div>"
        "</div>\n"
    )


def _render_missed_opportunities(
    missed: list[dict],
    tracked_count: int = 0,
    avoided: Optional[list[dict]] = None,
    insights: Optional[dict] = None,
    analysis_md: Optional[str] = None,
) -> str:
    """Render the 'Missed Opportunities' section: a base-rate summary strip, the
    misses table (with miss-type, first-sight factors, and peak give-back), and a
    symmetric 'Avoided Losses' table so the section measures the model rather than
    just the market's upside tail. Empty misses still render a discoverable
    empty-state note."""
    _esc = _miss_esc

    html = ("<h2 id='missed-opps' style='margin-top:48px;'>"
            "🪟 Missed Opportunities</h2>\n")

    # ----- Empty state -----
    if not missed:
        html += (
            '<p style="color:var(--fg-muted);font-size:13px;line-height:1.5;'
            'background:var(--bg-card);border:1px dashed var(--border-medium);'
            'padding:14px 16px;border-radius:6px;max-width:760px;">'
            f"No missed opportunities yet — tracking <strong>{tracked_count}</strong> "
            f"stock{'' if tracked_count == 1 else 's'} (every holding and watchlist "
            f"name) since the analyzer started watching. A stock lands here once it "
            f"climbs <strong>≥{MISSED_OPP_GAIN_PCT:g}%</strong> above its first-seen "
            f"price while you still hold "
            f"<strong>under {MISSED_OPP_ALLOC_THRESHOLD:g}%</strong> of the portfolio "
            f"(never bought, or under-allocated). This fills in automatically on future "
            f"runs as prices move.</p>\n"
        )
        return html

    # ----- Summary strip (base-rate context + gap split + still-actionable) -----
    html += _miss_summary_strip(insights)

    # ----- Embedded AI post-mortem (only when this run used --analyze-misses) -----
    if analysis_md:
        html += _render_miss_analysis_panel(analysis_md)

    # ----- Misses table -----
    html += (
        '<p style="color:#7f8c8d;font-size:12px;margin-top:16px;margin-bottom:12px;">'
        f"Stocks the analyzer has tracked (every holding and watchlist name) that "
        f"climbed <strong>≥{MISSED_OPP_GAIN_PCT:g}%</strong> since they were first "
        f"seen, yet you still hold <strong>under {MISSED_OPP_ALLOC_THRESHOLD:g}%</strong> "
        f"of the portfolio. <strong>Type</strong> splits a <em>model gap</em> (never "
        f"rated a buy) from an <em>execution gap</em> (rated a buy, but we didn't size "
        f"up). <strong>At first sight</strong> shows the factors behind the original "
        f"call. Sorted by gain left on the table.</p>\n"
    )
    html += "<div class='table-wrap'><table>\n<thead><tr>"
    html += (
        "<th>Ticker</th>"
        "<th>Name / Sector</th>"
        "<th>Type</th>"
        "<th>First seen</th>"
        "<th>First verdict</th>"
        "<th>Current verdict</th>"
        "<th title='Composite score · 52-week position · trend, as of first sight.' "
        "style='cursor:help;'>At first sight &#9432;</th>"
        "<th class='num'>Gain since</th>"
        "<th title='Hover for full explanation.' style='cursor:help;'>Miss reason &#9432;</th>"
        "</tr></thead><tbody>\n"
    )
    for m in missed:
        label = m.get("first_verdict") or "—"
        cur_verdict = m.get("last_verdict") or "—"
        ticker = _esc(m["ticker"])
        sector = _esc(m["sector"]) if m.get("sector") else "—"
        first_date = m.get("first_date") or ""
        date_iso = first_date[:10] if first_date else ""
        date_display = _esc(_fmt_short_date(first_date)) if first_date else "—"
        reason_attr = _miss_reason_attr(m.get("reason") or "")
        search_val = f"{m['ticker'].lower()} {m['name'].lower()} {(m.get('sector') or '').lower()}"

        # Type badge: model gap vs execution gap — per base, since a name
        # one base never rated a buy can be an execution gap under another.
        types = m.get("miss_type_by_mode") or {}
        type_sort = m.get("miss_type") or "model"
        type_badge = _mode_variants(
            lambda mode: _miss_type_badge(types.get(mode, type_sort)))
        type_per_mode = "".join(
            f" data-sort-{mode}='{types.get(mode, type_sort)}'"
            for mode in BASE_SCORE_MODES) if types else ""

        live = m.get("still_actionable_by_mode") or {}
        live_pill = _mode_variants(
            lambda mode: (_MISS_LIVE_PILL
                          if live.get(mode, m.get("still_actionable")) else ""))

        html += f"<tr data-search='{_esc(search_val)}'>"
        html += (f"<td class='ticker' data-sort='{ticker}'>"
                 f"<a class='miss-ticker-link' data-ticker='{ticker}' href='#' "
                 f"style='font-weight:700;color:inherit;text-decoration:underline;"
                 f"text-decoration-style:dotted;cursor:pointer;'>{ticker}</a>{live_pill}</td>")
        html += (f"<td data-sort='{_esc(m['name'])}'><div style='font-weight:500'>{_esc(m['name'])}</div>"
                 f"<div style='color:var(--fg-muted);font-size:11px;'>{sector}</div></td>")
        html += f"<td data-sort='{type_sort}'{type_per_mode}>{type_badge}</td>"
        html += (f"<td data-sort='{date_iso}'>"
                 f"<span style='color:var(--fg-muted);font-size:11px;'>{date_display}</span></td>")
        html += _miss_verdict_td(m.get("first_verdicts") or {}, label,
                                 m.get("first_verdict_score"))
        html += _miss_verdict_td(m.get("last_verdicts") or {}, cur_verdict,
                                 m.get("last_verdict_score"))
        html += _miss_factor_cell(m)
        # Gain cell, with a muted peak give-back sub-line when the name has
        # round-tripped meaningfully off its high (invisible when sorted by
        # current gain alone).
        gave = m.get("gave_back_pct") or 0.0
        peak_sub = ""
        if gave >= 5:
            peak_sub = (f"<div style='font-size:10px;color:var(--fg-muted);font-weight:400;'>"
                        f"peak +{m['peak_gain_pct']:.0f}%, gave back {gave:.0f}</div>")
        html += (f"<td class='num pos-up' data-sort='{m['gain_pct']:.2f}' style='font-weight:600;'>"
                 f"{_fmt_pct(m['gain_pct'], 1, True)}{peak_sub}</td>")
        html += f'<td class=\'miss-reason\' data-reason="{reason_attr}"></td>'
        html += "</tr>\n"
    html += "</tbody></table></div>\n"

    # ----- Avoided Losses (symmetric companion) -----
    html += _render_avoided_losses(avoided or [])
    return html


def _render_avoided_losses(avoided: list[dict]) -> str:
    """The downside mirror of the misses table: names that fell ≥5% while we
    stayed under-allocated. Same low-allocation discipline that 'missed' the
    winners sidestepped these — showing both is what keeps the section an
    honest scorecard instead of a highlight reel of regrets."""
    if not avoided:
        return ""
    _esc = _miss_esc
    correct = sum(1 for a in avoided if a.get("dodge_type") == "caution")
    lucky = len(avoided) - correct
    html = (
        "<h3 style='margin-top:34px;margin-bottom:4px;font-size:15px;'>"
        "🛡️ Avoided Losses <span style='font-weight:400;color:var(--fg-muted);"
        "font-size:12px;'>— the same discipline, other direction</span></h3>\n"
        '<p style="color:#7f8c8d;font-size:12px;margin-top:0;margin-bottom:12px;">'
        f"Tracked names that <strong>fell ≥{MISSED_OPP_GAIN_PCT:g}%</strong> since "
        f"first seen while you stayed under {MISSED_OPP_ALLOC_THRESHOLD:g}% — losses the "
        f"low-allocation calls sidestepped. <strong>{correct}</strong> were correct "
        f"caution (never a buy); <strong>{lucky}</strong> were dodged despite a buy "
        f"rating. Sorted by biggest drop.</p>\n"
    )
    html += "<div class='table-wrap'><table>\n<thead><tr>"
    html += (
        "<th>Ticker</th>"
        "<th>Name / Sector</th>"
        "<th>Dodge</th>"
        "<th>First seen</th>"
        "<th>First verdict</th>"
        "<th>Current verdict</th>"
        "<th title='Composite score · 52-week position · trend, as of first sight.' "
        "style='cursor:help;'>At first sight &#9432;</th>"
        "<th class='num'>Drop since</th>"
        "<th title='Hover for full explanation.' style='cursor:help;'>Detail &#9432;</th>"
        "</tr></thead><tbody>\n"
    )
    for a in avoided:
        label = a.get("first_verdict") or "—"
        cur_verdict = a.get("last_verdict") or "—"
        ticker = _esc(a["ticker"])
        sector = _esc(a["sector"]) if a.get("sector") else "—"
        first_date = a.get("first_date") or ""
        date_iso = first_date[:10] if first_date else ""
        date_display = _esc(_fmt_short_date(first_date)) if first_date else "—"
        reason_attr = _miss_reason_attr(a.get("reason") or "")
        search_val = f"{a['ticker'].lower()} {a['name'].lower()} {(a.get('sector') or '').lower()}"

        # Per base, for the same reason the misses table splits its Type
        # badge: another base may have rated this a buy.
        dodges = a.get("dodge_type_by_mode") or {}
        dodge_sort = a.get("dodge_type") or "caution"
        dodge_badge = _mode_variants(
            lambda mode: _miss_dodge_badge(dodges.get(mode, dodge_sort)))
        dodge_per_mode = "".join(
            f" data-sort-{mode}='{dodges.get(mode, dodge_sort)}'"
            for mode in BASE_SCORE_MODES) if dodges else ""

        html += f"<tr data-search='{_esc(search_val)}'>"
        html += (f"<td class='ticker' data-sort='{ticker}'>"
                 f"<a class='miss-ticker-link' data-ticker='{ticker}' href='#' "
                 f"style='font-weight:700;color:inherit;text-decoration:underline;"
                 f"text-decoration-style:dotted;cursor:pointer;'>{ticker}</a></td>")
        html += (f"<td data-sort='{_esc(a['name'])}'><div style='font-weight:500'>{_esc(a['name'])}</div>"
                 f"<div style='color:var(--fg-muted);font-size:11px;'>{sector}</div></td>")
        html += f"<td data-sort='{dodge_sort}'{dodge_per_mode}>{dodge_badge}</td>"
        html += (f"<td data-sort='{date_iso}'>"
                 f"<span style='color:var(--fg-muted);font-size:11px;'>{date_display}</span></td>")
        html += _miss_verdict_td(a.get("first_verdicts") or {}, label,
                                 a.get("first_verdict_score"))
        html += _miss_verdict_td(a.get("last_verdicts") or {}, cur_verdict,
                                 a.get("last_verdict_score"))
        html += _miss_factor_cell(a)
        html += (f"<td class='num pos-down' data-sort='{a['loss_pct']:.2f}' style='font-weight:600;'>"
                 f"{_fmt_pct(a['loss_pct'], 1, True)}</td>")
        html += f'<td class=\'miss-reason\' data-reason="{reason_attr}"></td>'
        html += "</tr>\n"
    html += "</tbody></table></div>\n"
    return html


def _portfolio_insights(results: list[PositionAnalysis],
                        mode: Optional[str] = None) -> list[dict]:
    """Build prioritized, data-driven recommendations for the header chip.

    Turns the per-position analysis into a few concise, actionable findings
    (exit/trim flags, concentration, high-conviction adds, stretched
    valuations, weak fundamentals, insider selling), judging verdicts under
    `mode` (default the run's). Each item is a dict {icon, label, detail,
    tone} where tone (danger/warn/good) drives the colored icon chip in the
    panel. Most important first; empty list means nothing notable.
    """
    def label(r):
        v = verdict_in(r, mode)
        return v.label if v else None

    held = [r for r in results if (r.shares or 0) > 0]
    if not held:
        return []

    def names(rs, n=4):
        shown = ", ".join(r.ticker for r in rs[:n])
        if len(rs) > n:
            shown += f" +{len(rs) - n} more"
        return shown

    def qpass(r):
        return sum(1 for f in r.filters if f.passed) if r.filters else None

    out: list[dict] = []

    sells = [r for r in held if label(r) == "SELL"]
    if sells:
        out.append({"icon": "🚩", "label": "Review for exit", "tone": "danger",
                    "detail": f"{names(sells)} — scoring below the framework's "
                              f"keep threshold."})

    trims = [r for r in held if label(r) == "TRIM"]
    if trims:
        out.append({"icon": "✂️", "label": "Trim candidates", "tone": "warn",
                    "detail": names(trims) + "."})

    over = sorted((r for r in held if (r.live_pct_portfolio or 0) >= 15),
                  key=lambda r: r.live_pct_portfolio or 0, reverse=True)
    if over:
        parts = ", ".join(f"{r.ticker} ({r.live_pct_portfolio:.0f}%)"
                          for r in over[:3])
        out.append({"icon": "📊", "label": "Concentration", "tone": "warn",
                    "detail": f"{parts} — sizeable position(s); consider "
                              f"rebalancing."})

    adds = [r for r in held if label(r) == "ADD"]
    if adds:
        out.append({"icon": "➕", "label": "High-conviction adds", "tone": "good",
                    "detail": names(adds) + "."})

    stretched = sorted((r for r in held
                        if r.upside_pct is not None and r.upside_pct <= -15),
                       key=lambda r: r.upside_pct)
    if stretched:
        out.append({"icon": "🎯", "label": "Above analyst target", "tone": "warn",
                    "detail": f"{names(stretched)} — limited upside to consensus."})

    weak = [r for r in held
            if qpass(r) is not None and qpass(r) <= 4
            and label(r) not in ("SELL", "TRIM")]
    if weak:
        parts = ", ".join(f"{r.ticker} ({qpass(r)}/9)" for r in weak[:3])
        out.append({"icon": "🔻", "label": "Weak fundamentals", "tone": "danger",
                    "detail": f"{parts} — watch quality trend."})

    caution = [r for r in held
               if (r.insider_activity or {}).get("net_signal") == "Selling"]
    if caution:
        out.append({"icon": "👀", "label": "Insider selling", "tone": "warn",
                    "detail": f"{names(caution)} — discretionary sales worth "
                              f"a look."})

    return out


def generate_html_report(
    results: list[PositionAnalysis],
    watchlists: Optional[dict[str, list[PositionAnalysis]]] = None,
    screening_results: Optional[dict] = None,
    realized_ytd: Optional[dict] = None,
    missed_opportunities: Optional[list[dict]] = None,
    recs_tracked_count: int = 0,
    avoided_losses: Optional[list[dict]] = None,
    missed_insights: Optional[dict] = None,
    missed_analysis_md: Optional[str] = None,
    account_summary: Optional[dict] = None,
) -> str:
    # Final verdicts with portfolio context (idempotent — main() already ran
    # this before tax analysis; other callers may not have).
    live_total = finalize_holding_verdicts(results)

    statement_total = sum(r.statement_market_value for r in results)
    delta = live_total - statement_total
    delta_pct = (delta / statement_total * 100) if statement_total else 0

    # Today's portfolio move: sum per-position $ impact, % vs prior-day total.
    day_change_total = sum(
        (r.day_change or 0) * (r.shares or 0)
        for r in results if r.day_change is not None and r.shares
    )
    prev_day_total = sum(
        (r.prev_close or 0) * (r.shares or 0)
        for r in results if r.prev_close is not None and r.shares
    )
    day_change_pct = (day_change_total / prev_day_total * 100
                      if prev_day_total else None)

    # After-hours move: per-position extended-session $ impact, % vs the
    # regular-close portfolio total. Only populated when the market is closed
    # (positions carry after_hours_change); otherwise the tile is hidden.
    ah_positions = [r for r in results
                    if r.after_hours_change is not None and r.shares]
    ah_change_total = sum((r.after_hours_change or 0) * (r.shares or 0)
                          for r in ah_positions)
    ah_regular_total = sum(
        ((r.regular_market_price if r.regular_market_price is not None
          else r.current_price) or 0) * (r.shares or 0)
        for r in results if r.shares
    )
    ah_change_pct = (ah_change_total / ah_regular_total * 100
                     if ah_regular_total and ah_positions else None)
    # Label after the dominant extended session (pre-market only if no
    # after-hours quotes are present).
    ah_label = ("Pre-market change"
                if (ah_positions
                    and all(r.extended_session == "pre" for r in ah_positions))
                else "After-hours change")

    compounders = [r for r in results if r.bucket == "compounder"]
    thematics = [r for r in results if r.bucket == "thematic"]
    # Default order: verdict score high → low (market value as tiebreak).
    # Shared with compute_run_ranks so rank badges match the displayed order.
    compounders.sort(key=_holding_rank_key, reverse=True)
    thematics.sort(key=_holding_rank_key, reverse=True)

    # Header counts, one per base-score mode (see _mode_variants).
    def count_labels(rows, labels) -> str:
        def render(mode):
            n = 0
            for r in rows:
                v = verdict_in(r, mode)
                if v and v.label in labels:
                    n += 1
            return str(n)
        return _mode_variants(render)

    action_count_html = count_labels(results, ("SELL", "TRIM"))
    add_count_html = count_labels(results, ("ADD",))

    _now_est = datetime.now(ZoneInfo("America/New_York"))
    now = _now_est.strftime("%B %d, %Y · %I:%M %p %Z")

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
    # Epoch ms of generation, embedded so the browser can keep the
    # "last updated X ago" text ticking while the page sits open.
    now_epoch_ms = int(_now_est.timestamp() * 1000)
    refresh_button_html, refresh_status_html = _build_refresh_widget()
    delta_class = "pos-up" if delta >= 0 else "pos-down"
    delta_sign = "+" if delta >= 0 else ""

    # Watchlist counts for summary card
    watchlist_unique: list[PositionAnalysis] = []
    if watchlists:
        seen_tickers: set[str] = set()
        held_tickers = {r.ticker for r in results}
        for items in watchlists.values():
            for r in items:
                if r.ticker in held_tickers or r.ticker in seen_tickers:
                    continue
                seen_tickers.add(r.ticker)
                watchlist_unique.append(r)
    watchlist_stat_html = ""
    if watchlist_unique:
        watchlist_stat_html = (
            f'<a class="stat clickable" href="#" '
            f'onclick="applyHeaderFilter(\'verdict-buy\');return false;">'
            f'<strong>{count_labels(watchlist_unique, ("BUY",))} / '
            f'{len(watchlist_unique)}</strong>'
            f'Watchlist BUY signals</a>'
        )

    has_holdings = bool(results)
    report_title = "Portfolio Analysis" if has_holdings else "Stock Analysis"

    # Base-score view: CSS showing the viewed mode's variants, and a script that
    # restores this browser's last viewed mode before the tables render.
    base_view_css = _base_view_css()
    base_view_boot_js = (
        "<script>\n"
        "// Base-score view: reopen the mode this browser last viewed, before the\n"
        "// tables render so they never flash another mode.\n"
        f"window.BASE_SCORE_MODES = {json.dumps(list(BASE_SCORE_MODES))};\n"
        f"window.BASE_SCORE_BASES = {json.dumps(list(BASE_SCORE_BASES))};\n"
        f"window.BASE_CAL_SUFFIX = {json.dumps(CALIBRATION_SUFFIX)};\n"
        "(function() {\n"
        "  try {\n"
        "    var v = localStorage.getItem('base-score-view');\n"
        "    if (window.BASE_SCORE_MODES.indexOf(v) !== -1) {\n"
        "      document.documentElement.setAttribute('data-base-view', v);\n"
        "    }\n"
        "  } catch (e) {}\n"
        "})();\n"
        "</script>"
    )

    # Top-of-report meter row: market sentiment · book quality · concentration.
    # Each renderer returns "" when its data is unavailable, so the row simply
    # shows whichever gauges apply (and collapses entirely with no holdings).
    _meter_cards = [
        _render_fear_greed_gauge(fetch_market_fear_greed()),
        _mode_variants(lambda m: _render_portfolio_health_gauge(results, m),
                       tag="div"),
        _render_diversification_gauge(results),
    ]
    _meter_cards = [c for c in _meter_cards if c]
    market_meter_html = (
        f'<div class="market-meters-row">{"".join(_meter_cards)}</div>'
        if _meter_cards else ""
    )

    # Quick-recommendations chip — hidden by default to keep the header
    # controls to one clean row; set QUICK_RECS=1 to put it back beside the
    # Base switch. Hover reveals the full list (JS handles the auto-hide).
    qr_chip_html = ""
    if has_holdings and os.environ.get("QUICK_RECS", "").strip() == "1":
        # One set of findings per base-score mode (see _mode_variants).
        _insights = {m: _portfolio_insights(results, m) for m in BASE_SCORE_MODES}
        if any(_insights.values()):
            def _qr_rows(mode):
                items = _insights[mode] or [{
                    "icon": "✅", "label": "No critical issues", "tone": "good",
                    "detail": "Nothing notable under this base score."}]
                return "".join(
                    f'<div class="qr-item qr-{it["tone"]}">'
                    f'<span class="qr-ico">{it["icon"]}</span>'
                    f'<span class="qr-text">'
                    f'<span class="qr-label">{it["label"]}</span>'
                    f'<span class="qr-detail">{it["detail"]}</span>'
                    f'</span></div>'
                    for it in items
                )

            def _qr_label(mode):
                if not _insights[mode]:
                    return "✅ No critical issues"
                return ('<span class="qr-bulb">💡</span>Quick recommendations'
                        f'<span class="qr-count">{len(_insights[mode])}</span>')

            qr_chip_html = (
                '<div class="qr-wrap" id="qrWrap">'
                f'<button class="qr-trigger" id="qrTrigger">'
                f'{_mode_variants(_qr_label)}</button>'
                f'<div class="qr-panel" id="qrPanel">'
                f'<div class="qr-panel-head">Quick recommendations</div>'
                f'<div class="qr-list">{_mode_variants(_qr_rows, tag="div")}</div>'
                f'</div>'
                "</div>"
            )
        else:
            qr_chip_html = (
                '<div class="qr-wrap"><button class="qr-trigger" '
                'style="cursor:default;">✅ No critical issues</button></div>'
            )

    # Missed-opportunities summary stat — sits in the stat row right after the
    # watchlist stat, and scrolls to the section on click. Shown whenever
    # tracking is active (main portfolio flow), even at zero, so it's
    # discoverable; the count is tinted amber when there's at least one miss.
    missed_stat_html = ""
    if missed_opportunities is not None:
        _mc = len(missed_opportunities)
        _num_style = " style='color:var(--fg-chip-amber);'" if _mc > 0 else ""
        missed_stat_html = (
            f'<a class="stat clickable" href="#missed-opps" '
            f'onclick="scrollToSection(\'missed-opps\');return false;">'
            f'<strong{_num_style}>{_mc}</strong>Missed opportunities</a>'
        )

    # Earnings-soon stat — sits right after Missed opportunities. Counts every
    # rendered name (holdings + de-duped watchlist) reporting within
    # EARNINGS_SOON_DAYS, so the count matches what the click-through filter
    # reveals. Clicking filters the tables to just those (event-risk heads-up
    # before adding/trimming). Shown only when at least one name qualifies, so
    # it stays out of the way off-season.
    def _earns_soon(r) -> bool:
        d = getattr(r, "days_to_earnings", None)
        return d is not None and 0 <= d <= EARNINGS_SOON_DAYS

    earnings_stat_html = ""
    _ec = sum(1 for r in results if _earns_soon(r))
    if watchlists:
        _held = {r.ticker for r in results}
        _seen: set[str] = set()
        for _items in watchlists.values():
            for r in _items:
                if r.ticker in _held or r.ticker in _seen:
                    continue
                _seen.add(r.ticker)
                if _earns_soon(r):
                    _ec += 1
    if _ec:
        earnings_stat_html = (
            f'<a class="stat clickable" href="#" '
            f'onclick="applyHeaderFilter(\'earnings-soon\');return false;">'
            f'<strong style="color:var(--fg-chip-amber);">{_ec}</strong>'
            f'Earnings within {EARNINGS_SOON_DAYS}d</a>'
        )

    # Portfolio-vs-S&P 500 stat — sits right after Earnings within 7d. Shows the
    # current holdings' return against the index for Today and YTD. Only built
    # with holdings, and hides itself if the benchmark can't be fetched.
    benchmark_stat_html = ""
    if has_holdings:
        benchmark_stat_html = _render_benchmark_stat(
            day_change_pct,
            _compute_holdings_ytd_return(results),
            fetch_benchmark_returns(),
        )

    holdings_summary = ""
    if has_holdings:
        # Today's-change stat (colored), only when we have the data.
        if day_change_pct is not None:
            dc_color = ("var(--pos-up)" if day_change_total > 0
                        else "var(--pos-down)" if day_change_total < 0
                        else "var(--fg-strong)")
            today_stat = (
                f'<div class="stat"><strong style="color:{dc_color};">'
                f'{_fmt_money(day_change_total)} '
                f'({_fmt_pct(day_change_pct, 2, True)})</strong>'
                f'Today\'s change</div>')
        else:
            today_stat = ""
        # After-hours-change stat (colored), shown only when the market is
        # closed and we have extended-hours pricing for at least one position.
        if ah_change_pct is not None:
            ah_color = ("var(--pos-up)" if ah_change_total > 0
                        else "var(--pos-down)" if ah_change_total < 0
                        else "var(--fg-strong)")
            after_hours_stat = (
                f'<div class="stat"><strong style="color:{ah_color};">'
                f'{_fmt_money(ah_change_total)} '
                f'({_fmt_pct(ah_change_pct, 2, True)})</strong>'
                f'{ah_label}</div>')
        else:
            after_hours_stat = ""
        # Portfolio value tile. Default is the gross sum of live position
        # values. When we have an account snapshot AND it carries a margin
        # loan (or uninvested cash), that gross figure isn't what the account
        # is actually worth, so show the true net value (gross + net cash,
        # where cash is negative for a margin loan) with a breakdown caption.
        pv_cash = account_summary.get("cash") if account_summary else None
        _cap_style = ("font-size:10px;color:var(--fg-muted);font-weight:400;"
                      "text-transform:none;letter-spacing:0;margin-top:2px;")
        if pv_cash is not None and pv_cash < -0.5:      # margin loan outstanding
            net_value = live_total + pv_cash
            pv_caption = (f"<div style='{_cap_style}'>{_fmt_money(live_total)} "
                          f"positions &minus; {_fmt_money(-pv_cash)} margin</div>")
            pv_tip = ("Net account value: live position value minus the "
                      "outstanding margin loan (money borrowed to buy positions, "
                      "so it is subtracted to get what the account is worth).")
            portfolio_value_stat = (
                f'<div class="stat" title="{pv_tip}">'
                f'<strong>{_fmt_money(net_value)}</strong>'
                f'Portfolio value (live){pv_caption}</div>')
        elif pv_cash is not None and pv_cash > 0.5:     # uninvested cash on top
            net_value = live_total + pv_cash
            pv_caption = (f"<div style='{_cap_style}'>{_fmt_money(live_total)} "
                          f"positions + {_fmt_money(pv_cash)} cash</div>")
            pv_tip = ("Net account value: live position value plus uninvested "
                      "cash.")
            portfolio_value_stat = (
                f'<div class="stat" title="{pv_tip}">'
                f'<strong>{_fmt_money(net_value)}</strong>'
                f'Portfolio value (live){pv_caption}</div>')
        else:
            portfolio_value_stat = (
                f'<div class="stat"><strong>{_fmt_money(live_total)}</strong>'
                f'Portfolio value (live)</div>')
        holdings_summary = f"""
    {portfolio_value_stat}
    {today_stat}
    {after_hours_stat}
    <a class="stat clickable" href="#compounders" onclick="scrollToSection('compounders');return false;"><strong>{len(compounders)}</strong>Compounder positions</a>
    <a class="stat clickable" href="#thematic" onclick="scrollToSection('thematic');return false;"><strong>{len(thematics)}</strong>Thematic / ETF positions</a>
    <a class="stat clickable" href="#" onclick="applyHeaderFilter('action');return false;"><strong>{action_count_html}</strong>Sell / Trim flags</a>
    <a class="stat clickable" href="#" onclick="applyHeaderFilter('verdict-add');return false;"><strong>{add_count_html}</strong>Add candidates</a>"""

    html = f"""<!DOCTYPE html>
<html data-base-view="{base_score_mode()}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- GitHub Pages serves HTML with Cache-Control: max-age=600, so a plain
     browser refresh can show the previous report for up to ~10 min after a new
     run deploys. Tell the browser to always revalidate the document so a manual
     refresh fetches the freshly published HTML (the header's reload button
     separately cache-busts with a ?v=timestamp query). -->
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
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
  /* Compact report header: title + meta on the left, controls (base switch,
     refresh, icons) on the right, all in one wrapping flex row. The control
     row sits on the title's baseline and keeps a flush right edge with the
     cards below, even when it wraps onto its own line. */
  .report-header {{ display: flex; align-items: flex-end;
                    gap: 10px 20px; flex-wrap: wrap; margin: 0 0 14px; }}
  /* Title and timestamp share one baseline and only stack when the controls
     leave too little room — so the title line fills the width instead of
     trailing off into empty space. The title block grows, which is what
     pushes the controls to the right edge while they fit beside it; once
     they wrap they start at the title's left edge rather than hanging in
     the middle of their own row. */
  .report-title {{ display: flex; align-items: baseline; flex-wrap: wrap;
                   gap: 0 14px; flex: 1 1 auto; min-width: 0; }}
  .hdr-meta {{ color: var(--fg-muted); font-size: 13px; white-space: nowrap; }}
  /* Controls wrap onto an extra row on narrow screens rather than pushing
     the page into horizontal scroll. */
  .report-controls {{ display: flex; align-items: center; gap: 8px;
                      flex-wrap: wrap; flex: 0 1 auto; }}
  /* Icon-only controls (reload, theme) cluster at the end of the row, set
     slightly apart from the labelled buttons. */
  .ctl-icons {{ display: inline-flex; align-items: center; gap: 8px;
                margin-left: 4px; }}
  .refresh-btn {{ height: 34px; padding: 0 14px; border-radius: 17px;
                  border: 1px solid var(--border-medium);
                  background: var(--bg-card); color: var(--fg-body);
                  cursor: pointer; font-size: 12px; font-weight: 600;
                  display: flex; align-items: center; gap: 6px;
                  white-space: nowrap;
                  box-shadow: var(--shadow-card);
                  transition: transform 0.15s, background 0.2s; }}
  .refresh-btn:hover {{ transform: scale(1.04); background: var(--bg-card-hover); }}
  .refresh-btn:disabled {{ opacity: 0.5; cursor: default; transform: none; }}
  .tax-toggle.active,
  .miss-toggle.active {{ background: var(--bg-chip-green);
                        color: var(--fg-chip-green);
                        border-color: var(--pos-up); }}
  /* Verdict base-score switch: a segmented pill matching .refresh-btn. */
  .base-switch {{ min-height: 34px; flex: none; display: flex; align-items: center;
                  flex-wrap: wrap; row-gap: 2px;
                  gap: 2px; padding: 0 3px 0 12px; border-radius: 17px;
                  border: 1px solid var(--border-medium);
                  background: var(--bg-card); box-shadow: var(--shadow-card);
                  font-size: 12px; font-weight: 600; }}
  .base-switch-label {{ color: var(--fg-muted); margin-right: 4px; cursor: help;
                        white-space: nowrap; }}
  .base-switch button {{ height: 26px; padding: 0 10px; border: 0;
                         border-radius: 13px; background: transparent;
                         color: var(--fg-body); font: inherit; cursor: pointer;
                         transition: background 0.2s; }}
  .base-switch button:not([aria-pressed="true"]):hover {{ background: var(--bg-card-hover); }}
  .base-switch button[aria-pressed="true"] {{ background: var(--bg-pill-active);
                                              color: var(--fg-pill-active); }}
  .base-switch-sep {{ width: 1px; height: 16px; margin: 0 6px; flex: none;
                      background: var(--border-medium); }}
  .base-default-mark {{ font-size: 9px; margin-left: 3px; opacity: 0.8; }}
  .base-default-btn[hidden] {{ display: none; }}
  /* Only the viewed base score's copy of each verdict-dependent piece shows. */
{base_view_css}
  @media (max-width: 560px) {{
    .base-switch-label {{ display: none; }}
    .base-switch {{ padding-left: 3px; }}
    .base-switch-sep {{ margin: 0 3px; }}
  }}
  .refresh-status {{ font-size: 12px; color: var(--fg-muted);
                     text-align: right; margin: -6px 0 10px; }}
  .refresh-status:empty {{ display: none; }}
  h1 {{ font-size: 24px; margin: 0; font-weight: 600;
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

  /* ---------- Summary card ---------- */
  /* Top-of-report meters — market sentiment · portfolio health · diversification. */
  .market-meters-row {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 14px; }}
  .market-meter {{ display: flex; align-items: center; gap: 12px;
                   flex: 1 1 240px; min-width: 210px;
                   background: var(--bg-summary); border: 1px solid var(--border-medium);
                   border-radius: 9px; padding: 8px 14px;
                   box-shadow: var(--shadow-card); }}
  .fg-gauge {{ width: 96px; height: auto; flex: 0 0 auto; }}
  .fg-readout {{ display: flex; flex-direction: column; gap: 0; line-height: 1.15; }}
  .fg-score {{ font-size: 21px; font-weight: 800; letter-spacing: -0.5px; }}
  .fg-rating {{ font-size: 12px; font-weight: 700; }}
  .fg-label {{ font-size: 8.5px; font-weight: 600; text-transform: uppercase;
               letter-spacing: 0.4px; color: var(--fg-muted); margin-top: 2px; }}
  .fg-prev {{ display: flex; flex-wrap: wrap; gap: 2px 8px; margin-top: 3px; }}
  .fg-prev-item {{ font-size: 8.5px; color: var(--fg-muted);
                   text-transform: uppercase; letter-spacing: 0.3px; }}
  .fg-prev-item strong {{ color: var(--fg-strong); font-size: 9.5px; }}
  .fg-source {{ font-size: 8.5px; color: var(--fg-muted); margin-top: 3px; opacity: 0.8; }}
  @media (max-width: 520px) {{
    .market-meter {{ gap: 10px; padding: 8px 12px; }}
    .fg-gauge {{ width: 84px; }}
    .fg-score {{ font-size: 19px; }}
  }}
  .summary-card {{ background: var(--bg-summary); border: 1px solid var(--border-medium);
                   border-radius: 10px; padding: 14px 20px;
                   margin-bottom: 16px;
                   box-shadow: var(--shadow-card); }}
  /* Stats packed left-to-right in order so each tile sits directly after the
     previous one (e.g. Missed opportunities right after Watchlist BUY signals).
     flex-wrap still lets them stack on narrow screens; the larger column gap
     keeps them readable without space-between flinging tiles to the edges. */
  .summary-row {{ display: flex; gap: 14px 34px; flex-wrap: wrap;
                  justify-content: flex-start; }}
  .stat {{ font-size: 11px; color: var(--fg-muted);
           text-transform: uppercase; letter-spacing: 0.3px;
           font-weight: 600; white-space: nowrap; }}
  .stat strong {{ font-size: 19px; display: block;
                  color: var(--fg-strong); margin-top: 3px;
                  font-weight: 700; letter-spacing: -0.3px;
                  text-transform: none; }}
  /* Actionable stats (jump to a section or apply a filter). */
  a.stat.clickable {{ cursor: pointer; text-decoration: none;
                      color: var(--fg-muted); transition: color 0.15s; }}
  a.stat.clickable:hover {{ color: var(--fg-strong); }}
  a.stat.clickable:hover strong {{ text-decoration: underline; }}

  /* Utility classes for the most-repeated cell decorations — the static box
     rules move out of every row (smaller markup, identical rendering); only
     the dynamic color/width stays inline. */
  .qdot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin:0 1px; }}
  .rbar {{ display:flex; height:9px; border-radius:3px; overflow:hidden;
           min-width:60px; max-width:84px; margin-bottom:2px; cursor:help; }}

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
  /* Sticky column headers (desktop): pin the thead just below the sticky h2
     while scrolling a long table. Needs overflow:visible on .table-wrap —
     any overflow other than visible would trap the sticky cells inside the
     wrapper instead of pinning to the viewport — so this is desktop-only;
     narrow screens keep overflow-x:auto for horizontal table scrolling.
     --h2-pin-h is measured by JS at load (h2 height varies with theme/font).
     The inset box-shadow replaces the th border-bottom while pinned:
     border-collapse drops cell borders from stuck cells in Chrome. */
  @media (min-width: 901px) {{
    .table-wrap {{ overflow-x: visible; }}
    thead th {{ position: sticky;
                top: var(--h2-pin-h, 49px);
                z-index: 10;   /* below the h2 (15), above row content */
                box-shadow: inset 0 -2px 0 var(--border-medium); }}
    thead th:first-child {{ border-top-left-radius: 9px; }}
    thead th:last-child {{ border-top-right-radius: 9px; }}
  }}
  td {{ padding: 9px 8px; border-bottom: 1px solid var(--border-soft);
        vertical-align: middle; color: var(--fg-body); }}
  tbody tr:nth-child(even) td {{ background: var(--bg-row-even); }}
  tbody tr:hover td {{ background: var(--bg-row-hover); }}
  tbody tr:last-child td {{ border-bottom: none; }}

  /* ---------- Cell styles ---------- */
  .verdict {{ padding: 4px 10px; border-radius: 12px; color: white;
              font-weight: 600; font-size: 11px; letter-spacing: 0.4px;
              display: inline-block; }}

  /* Verdict cell: pill + score with a styled hover-card breakdown that
     replaces the old plain title= tooltip. The card escapes the table on
     desktop (.table-wrap is overflow:visible >=901px); on narrow screens it
     would clip, so a short native title= stays as the fallback there. */
  .vcell {{ position: relative; display: inline-flex; align-items: center;
            gap: 6px; cursor: help; }}
  .vscore {{ font-weight: 600; font-size: 13px;
             font-variant-numeric: tabular-nums; }}
  /* position:fixed + JS-computed top/left (see the verdict-card script) so the
     card opens into whatever space is available around the cell and never
     spills past the viewport — no horizontal scrollbar, no clipping. Falls
     back to the short native title= tooltip if JS is unavailable. */
  .vcard {{ display: none; position: fixed; z-index: 40;
            width: 300px; max-width: calc(100vw - 16px);
            max-height: calc(100vh - 16px); overflow-y: auto;
            background: var(--bg-card); color: var(--fg-body);
            border: 1px solid var(--border-medium); border-radius: 10px;
            box-shadow: var(--shadow-sticky); padding: 12px 14px;
            text-align: left; font-size: 12px; line-height: 1.45;
            white-space: normal; cursor: default; }}
  .vcard.show {{ display: block; }}
  .miss-trigger {{ font-size: 11px; color: var(--fg-muted); cursor: help;
                  border-bottom: 1px dotted var(--fg-muted); white-space: nowrap; }}
  /* What the other base scores called a missed/avoided name. */
  .miss-alt {{ display: block; margin-top: 3px; font-size: 10px;
               color: var(--fg-muted); white-space: nowrap;
               font-variant-numeric: tabular-nums; }}
  .miss-vcard {{ width: 360px; }}
  .vcard-head {{ display: flex; align-items: baseline;
                 justify-content: space-between; gap: 10px; margin-bottom: 8px; }}
  .vcard-headline {{ font-weight: 600; color: var(--fg-strong); font-size: 12px; }}
  .vcard-score {{ font-weight: 700; font-size: 20px; letter-spacing: -0.5px;
                  font-variant-numeric: tabular-nums; flex: none; }}
  .vbar {{ height: 6px; border-radius: 3px; background: var(--bg-chip-neutral);
           overflow: hidden; margin-bottom: 10px; }}
  .vbar-fill {{ height: 100%; border-radius: 3px; }}
  .vcard-base {{ color: var(--fg-muted); font-size: 11px; font-weight: 600;
                 text-transform: uppercase; letter-spacing: 0.3px;
                 padding-bottom: 6px; margin-bottom: 6px;
                 border-bottom: 1px dashed var(--border-medium); }}
  .vrows {{ display: flex; flex-direction: column; gap: 5px; }}
  .vrow {{ display: flex; align-items: flex-start; gap: 8px; }}
  .vd {{ flex: none; min-width: 26px; text-align: right; font-weight: 700;
         font-variant-numeric: tabular-nums; font-size: 12px; }}
  .vd-pos {{ color: var(--pos-up); }}
  .vd-neg {{ color: var(--pos-down); }}
  .vt {{ color: var(--fg-body); }}
  /* Confidence chip inside the verdict card (Medium/Low data coverage). */
  .vconf {{ font-size: 11px; font-weight: 600; padding: 4px 8px;
            border-radius: 6px; margin-bottom: 8px; line-height: 1.35; }}
  .vconf-med {{ background: var(--bg-chip-neutral); color: var(--fg-chip-neutral); }}
  .vconf-low {{ background: var(--bg-chip-amber); color: var(--fg-chip-amber); }}
  /* Next-earnings footer inside the verdict card. */
  .vearn-row {{ display: flex; align-items: center; gap: 6px;
                margin-top: 10px; padding-top: 8px;
                border-top: 1px dashed var(--border-medium);
                font-size: 11px; font-weight: 600; }}
  .vearn {{ color: var(--fg-muted); }}
  .vearn-soon {{ color: var(--fg-chip-amber); }}
  .vearn-ico {{ font-size: 12px; }}
  /* Same verdict under the other base-score modes. */
  .valt {{ margin-top: 10px; padding-top: 8px;
           border-top: 1px dashed var(--border-medium);
           font-size: 11px; color: var(--fg-muted); }}
  /* At-a-glance markers beside the score (low-confidence dot, earnings glyph). */
  .vmark {{ font-size: 10px; cursor: help; line-height: 1; }}
  .vmark-conf {{ color: #e67e22; }}
  .vmark-earn {{ font-size: 11px; filter: grayscale(0.1); }}
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

  /* Quick recommendations — subtle, hover-revealed chip in the header
     controls (beside Refresh). Hovering opens a floating panel with the full
     list; auto-hides on scroll / mouse-leave (JS). Sized to sit in the
     34px control row; panel is right-anchored so it stays on screen. */
  .qr-wrap {{ position: relative; display: inline-flex; align-items: center; }}
  .qr-trigger {{ height: 34px; padding: 0 13px; box-sizing: border-box;
                 display: flex; align-items: center; gap: 5px;
                 background: var(--bg-card); color: var(--fg-muted);
                 border: 1px solid var(--border-medium); border-radius: 17px;
                 font-size: 12px; font-weight: 600;
                 cursor: default; user-select: none; white-space: nowrap;
                 box-shadow: var(--shadow-card);
                 transition: background 0.2s, color 0.2s; }}
  .qr-trigger:hover {{ background: var(--bg-card-hover); color: var(--fg-body); }}
  .qr-wrap.open .qr-trigger {{ background: var(--bg-card-hover);
                               color: var(--fg-body); }}
  .qr-bulb {{ font-size: 13px; }}
  /* Count badge in the trigger. */
  .qr-count {{ display: inline-flex; align-items: center; justify-content: center;
               min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px;
               background: var(--bg-chip-amber); color: var(--fg-chip-amber);
               font-size: 11px; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .qr-panel {{ display: none; position: absolute; top: calc(100% + 8px);
               right: 0; left: auto; z-index: 30;
               width: 384px; max-width: 92vw;
               background: var(--bg-card); color: var(--fg-body);
               border: 1px solid var(--border-medium); border-radius: 12px;
               box-shadow: var(--shadow-sticky);
               padding: 6px; text-align: left; }}
  .qr-wrap.open .qr-panel {{ display: block; }}
  .qr-panel-head {{ font-size: 10px; font-weight: 700; text-transform: uppercase;
                    letter-spacing: 0.6px; color: var(--fg-faint);
                    padding: 8px 10px 7px; }}
  .qr-list {{ display: flex; flex-direction: column; gap: 1px; }}
  .qr-item {{ display: flex; gap: 11px; align-items: flex-start;
              padding: 9px 10px; border-radius: 9px;
              transition: background 0.15s; }}
  .qr-item:hover {{ background: var(--bg-card-hover); }}
  .qr-item + .qr-item {{ position: relative; }}
  .qr-ico {{ flex: none; width: 27px; height: 27px; border-radius: 50%;
             display: flex; align-items: center; justify-content: center;
             font-size: 13px; line-height: 1; margin-top: 1px; }}
  .qr-text {{ display: flex; flex-direction: column; gap: 2px; min-width: 0; }}
  .qr-label {{ font-size: 12.5px; font-weight: 700; color: var(--fg-strong);
               letter-spacing: -0.1px; }}
  .qr-detail {{ font-size: 12px; line-height: 1.45; color: var(--fg-muted); }}
  /* tone → colored icon chip (matches the report's chip palette) */
  .qr-danger .qr-ico {{ background: var(--bg-chip-red); }}
  .qr-warn .qr-ico {{ background: var(--bg-chip-amber); }}
  .qr-good .qr-ico {{ background: var(--bg-chip-green); }}
  .qr-info .qr-ico {{ background: var(--bg-chip-blue); }}

  /* ---------- Filter bar ----------
     Note: filter bar is intentionally NOT sticky. We tried scroll-direction
     toggling (sticky-on-scroll-up) but the attribute-conditional sticky rules
     don't reliably work in Safari. The simpler, working behavior: h2 section
     headers are always sticky (so you know which section you're reading);
     filters live at the top of the page and you scroll back up to use them. */
  /* ===================== Redesigned filter bar (flt-*) ===================== */
  .flt-bar {{ background: var(--bg-card); border: 1px solid var(--border-medium);
             border-radius: 10px; padding: 10px 14px; margin-bottom: 18px;
             box-shadow: var(--shadow-card); }}
  .flt-top {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .flt-search {{ position: relative; flex: 1 1 240px; min-width: 200px; display: flex; }}
  .flt-search input {{ width: 100%; padding: 7px 30px 7px 12px;
                      border: 1px solid var(--border-strong); border-radius: 8px;
                      font-size: 13px; outline: none; background: var(--bg-input);
                      color: var(--fg-body); transition: border-color .15s, box-shadow .15s; }}
  .flt-search input:focus {{ border-color: var(--fg-table-header);
                            box-shadow: 0 0 0 3px rgba(74,144,226,.15); }}
  .flt-search .flt-x {{ position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
                       border: 0; background: none; color: var(--fg-faint); cursor: pointer;
                       font-size: 15px; line-height: 1; padding: 2px; display: none; }}
  .flt-search.has-val .flt-x {{ display: block; }}

  .flt-btn {{ background: var(--bg-table-header); color: var(--fg-pill);
             border: 1px solid var(--border-strong); border-radius: 8px;
             padding: 6px 12px; font-size: 12px; font-weight: 600; cursor: pointer;
             user-select: none; white-space: nowrap; transition: all .15s; }}
  .flt-btn:hover {{ background: var(--bg-table-header-hover); }}
  .flt-btn.on {{ background: var(--bg-pill-active); color: var(--fg-pill-active);
                border-color: var(--bg-pill-active); }}
  .flt-btn .flt-badge {{ display: inline-block; min-width: 16px; margin-left: 5px;
                        padding: 0 5px; border-radius: 9px; font-size: 10px; font-weight: 700;
                        background: var(--bg-pill-active); color: #fff; line-height: 16px; }}
  .flt-btn.on .flt-badge {{ background: rgba(255,255,255,.25); }}
  .flt-clear {{ color: var(--fg-faint); background: var(--bg-page); }}
  .flt-clear:hover {{ background: var(--bg-chip-red); color: var(--fg-chip-red);
                     border-color: var(--fg-chip-red); }}
  .flt-count {{ font-size: 12px; color: var(--fg-muted); margin-left: auto;
               font-variant-numeric: tabular-nums; white-space: nowrap; }}

  /* ---- Saved + most-used combos (★ = one you named, ☆ = learned/suggested) ---- */
  .flt-used {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
              margin-top: 9px; }}
  .flt-used-label {{ font-size: 10px; color: var(--fg-faint); font-weight: 700;
                    text-transform: uppercase; letter-spacing: .6px; }}
  .flt-combo {{ display: inline-flex; align-items: center; gap: 4px;
               background: var(--bg-pill); border: 1px solid var(--border-strong);
               border-radius: 14px; padding: 3px 6px 3px 11px; font-size: 12px; cursor: pointer;
               color: var(--fg-pill); font-weight: 500; white-space: nowrap; transition: all .15s; }}
  .flt-combo::before {{ content: "☆"; color: var(--accent, #e6a817); font-size: 10px; }}
  .flt-combo.named::before {{ content: "★"; }}
  .flt-combo:hover {{ background: var(--bg-pill-hover); border-color: var(--fg-faint); }}
  .flt-combo.on {{ background: var(--bg-pill-active); color: var(--fg-pill-active);
                  border-color: var(--bg-pill-active); }}
  .flt-combo.on::before {{ color: rgba(255,255,255,.9); }}
  /* Remove-from-bar ✕: revealed on hover (always visible on touch). */
  .flt-combo-x {{ display: inline-flex; align-items: center; justify-content: center;
                 width: 14px; height: 14px; border-radius: 50%; font-size: 10px;
                 line-height: 1; color: inherit; opacity: 0; transition: opacity .12s; }}
  .flt-combo:hover .flt-combo-x, .flt-combo.on .flt-combo-x {{ opacity: .6; }}
  .flt-combo:hover .flt-combo-x:hover,
  .flt-combo.on .flt-combo-x:hover {{ opacity: 1; background: rgba(0,0,0,.16); }}
  @media (hover: none) {{ .flt-combo-x {{ opacity: .5; }} }}
  .flt-restore {{ color: var(--fg-faint); }}
  .flt-restore::before {{ content: "↺"; }}
  .flt-save.saved {{ color: var(--accent, #e6a817); border-color: var(--accent, #e6a817); }}

  /* ---- Active-filter chips ---- */
  .flt-chips {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 9px; }}
  .flt-chips:empty {{ display: none; }}
  .flt-chip {{ display: inline-flex; align-items: center; gap: 5px;
              background: var(--bg-chip-blue); color: var(--fg-chip-blue);
              border: 1px solid transparent; border-radius: 13px;
              padding: 3px 5px 3px 10px; font-size: 11.5px; font-weight: 600; white-space: nowrap; }}
  .flt-chip b {{ font-weight: 700; }}
  .flt-chip .flt-chip-x {{ display: inline-flex; align-items: center; justify-content: center;
                          width: 15px; height: 15px; border-radius: 50%; cursor: pointer;
                          font-size: 12px; line-height: 1; color: inherit; opacity: .65; }}
  .flt-chip .flt-chip-x:hover {{ opacity: 1; background: rgba(0,0,0,.12); }}

  /* ---- Expandable panel ---- */
  .flt-panel {{ display: none; margin-top: 12px; padding-top: 12px;
               border-top: 1px solid var(--border-soft); }}
  .flt-panel.show {{ display: block; }}
  .flt-section {{ margin-bottom: 6px; }}
  .flt-section-head {{ display: flex; align-items: center; gap: 7px; cursor: pointer;
                      padding: 5px 2px; user-select: none; }}
  .flt-section-head .flt-caret {{ color: var(--fg-faint); font-size: 10px; width: 10px;
                                 transition: transform .15s; }}
  .flt-section.collapsed .flt-caret {{ transform: rotate(-90deg); }}
  .flt-section-title {{ font-size: 11px; color: var(--fg-table-header); font-weight: 700;
                       text-transform: uppercase; letter-spacing: .5px; }}
  .flt-section-active {{ font-size: 10px; font-weight: 700; color: #fff;
                        background: var(--bg-pill-active); border-radius: 9px;
                        padding: 0 6px; line-height: 15px; }}
  .flt-section-active:empty {{ display: none; }}
  .flt-cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(215px, 1fr));
               gap: 8px 12px; padding: 2px 2px 8px; }}
  .flt-section.collapsed .flt-cards {{ display: none; }}
  .flt-card {{ background: var(--bg-page); border: 1px solid var(--border-soft);
              border-radius: 8px; padding: 8px 10px; }}
  .flt-card-label {{ font-size: 10.5px; color: var(--fg-muted); font-weight: 700;
                    text-transform: uppercase; letter-spacing: .4px; margin-bottom: 7px;
                    display: flex; justify-content: space-between; align-items: baseline; }}
  .flt-card-reset {{ font-weight: 600; font-size: 10px; color: var(--fg-faint);
                    cursor: pointer; text-transform: none; letter-spacing: 0; display: none; }}
  .flt-card.narrowed .flt-card-reset {{ display: inline; }}
  .flt-card-reset:hover {{ color: var(--fg-chip-red); }}

  /* ---- Facet option pills ---- */
  .flt-opts {{ display: flex; flex-wrap: wrap; gap: 5px; }}
  .flt-opt {{ display: inline-flex; align-items: center; gap: 5px; cursor: pointer;
             background: var(--bg-pill); border: 1px solid var(--border-strong);
             border-radius: 12px; padding: 2px 8px; font-size: 11.5px; color: var(--fg-pill);
             font-weight: 500; white-space: nowrap; transition: all .12s; }}
  .flt-opt:hover {{ background: var(--bg-pill-hover); border-color: var(--fg-faint); }}
  .flt-opt.on {{ background: var(--bg-pill-active); color: var(--fg-pill-active);
                border-color: var(--bg-pill-active); }}
  .flt-opt .flt-n {{ font-size: 10px; color: var(--fg-faint); font-variant-numeric: tabular-nums; }}
  .flt-opt.on .flt-n {{ color: rgba(255,255,255,.75); }}
  .flt-opt.zero {{ opacity: .38; cursor: default; }}
  .flt-opt.zero:hover {{ background: var(--bg-pill); border-color: var(--border-strong); }}

  /* ---- Dual-range slider ---- */
  .flt-range-vals {{ display: flex; justify-content: space-between; align-items: center;
                    font-size: 11.5px; color: var(--fg-body); font-weight: 600;
                    font-variant-numeric: tabular-nums; margin-bottom: 8px; }}
  .flt-range-vals .flt-range-n {{ font-size: 10px; color: var(--fg-faint); font-weight: 500; }}
  .flt-slider {{ position: relative; height: 20px; }}
  .flt-slider .flt-track {{ position: absolute; top: 8px; left: 0; right: 0; height: 4px;
                           background: var(--border-medium); border-radius: 2px; }}
  .flt-slider .flt-fill {{ position: absolute; top: 8px; height: 4px;
                          background: var(--bg-pill-active); border-radius: 2px; }}
  [data-theme="dark"] .flt-slider .flt-fill {{ background: #4a90e2; }}
  .flt-slider input[type=range] {{ position: absolute; top: 0; left: 0; width: 100%;
                                  height: 20px; margin: 0; background: none; pointer-events: none;
                                  -webkit-appearance: none; appearance: none; }}
  .flt-slider input[type=range]::-webkit-slider-thumb {{ -webkit-appearance: none; appearance: none;
       width: 15px; height: 15px; border-radius: 50%; background: var(--bg-card);
       border: 2px solid var(--bg-pill-active); cursor: pointer; pointer-events: auto;
       box-shadow: 0 1px 3px rgba(15,23,42,.25); margin-top: 0; }}
  .flt-slider input[type=range]::-moz-range-thumb {{ width: 15px; height: 15px; border-radius: 50%;
       background: var(--bg-card); border: 2px solid var(--bg-pill-active); cursor: pointer;
       pointer-events: auto; box-shadow: 0 1px 3px rgba(15,23,42,.25); }}
  [data-theme="dark"] .flt-slider input[type=range]::-webkit-slider-thumb {{ border-color: #4a90e2; }}
  [data-theme="dark"] .flt-slider input[type=range]::-moz-range-thumb {{ border-color: #4a90e2; }}

  @media (max-width: 900px) {{
    .flt-cards {{ grid-template-columns: 1fr 1fr; }}
    .flt-count {{ margin-left: 0; }}
  }}
  @media (max-width: 560px) {{
    .flt-cards {{ grid-template-columns: 1fr; }}
  }}

  /* ---------- Theme toggle button (in the header controls cluster) ---------- */
  .theme-toggle, .icon-btn {{ width: 34px; height: 34px; flex: 0 0 auto;
                   border-radius: 50%; border: 1px solid var(--border-medium);
                   background: var(--bg-card); color: var(--fg-body);
                   cursor: pointer; font-size: 16px; line-height: 1;
                   padding: 0;
                   display: flex; align-items: center; justify-content: center;
                   box-shadow: var(--shadow-card);
                   transition: transform 0.15s, background 0.2s; }}
  .theme-toggle:hover, .icon-btn:hover {{ transform: scale(1.08);
                                          background: var(--bg-card-hover); }}
  /* The reload glyph reads better a touch larger and optically centred. */
  .icon-btn {{ font-size: 18px; }}
  .icon-btn:disabled {{ opacity: 0.5; cursor: default; transform: none; }}

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
    .refresh-status {{ text-align: left; margin-top: 0; }}
    /* Stacked header: let the timestamp wrap onto its own lines rather than
       push the page sideways. */
    .report-header {{ align-items: flex-start; }}
    .hdr-meta {{ white-space: normal; }}
    /* Chip can sit anywhere once the controls wrap, so anchor the panel to
       the viewport (full-width sheet) instead of the chip to avoid clipping. */
    .qr-panel {{ position: fixed; top: auto; bottom: 12px;
                 left: 10px; right: 10px; width: auto; min-width: 0;
                 max-width: none; }}
  }}

  /* ---------- Mobile tap-to-reveal tooltip (bottom sheet) ----------
     Touch devices can't hover, so the title="" tooltips on cells (quality
     dots, score breakdown, verdict reason, insider detail, cost/gain) are
     invisible. On touch-primary devices a tap reveals that text here.
     The elements are only created/shown by JS in touch mode, so these
     rules are inert on desktop. */
  .m-tip-backdrop {{ position: fixed; inset: 0; z-index: 199;
                     background: rgba(0, 0, 0, 0.35); opacity: 0;
                     pointer-events: none; transition: opacity 0.2s; }}
  .m-tip-backdrop.show {{ opacity: 1; pointer-events: auto; }}
  .m-tip {{ position: fixed; left: 0; right: 0; bottom: 0; z-index: 200;
            background: var(--bg-card); color: var(--fg-body);
            border-top: 1px solid var(--border-medium);
            border-radius: 14px 14px 0 0;
            box-shadow: 0 -6px 24px rgba(0, 0, 0, 0.28);
            padding: 18px 20px calc(16px + env(safe-area-inset-bottom, 0px));
            transform: translateY(110%);
            transition: transform 0.24s ease;
            max-height: 64vh; overflow-y: auto;
            -webkit-overflow-scrolling: touch; }}
  .m-tip.show {{ transform: translateY(0); }}
  .m-tip-label {{ font-size: 11px; font-weight: 700; letter-spacing: 0.4px;
                  text-transform: uppercase; color: var(--fg-muted);
                  margin-bottom: 8px; }}
  .m-tip-body {{ font-size: 14px; line-height: 1.5; white-space: pre-wrap;
                 word-break: break-word; }}
  .m-tip-close {{ margin-top: 16px; width: 100%; padding: 11px;
                  border: 1px solid var(--border-medium); border-radius: 8px;
                  background: var(--bg-page); color: var(--fg-body);
                  font-size: 14px; font-weight: 600; cursor: pointer; }}
</style>
</head>
<body>
<script>
  // Keep refreshes (the browser's own reload or the header's reload
  // button) anchored at the top. Two things otherwise scroll the page on reload: (1) the browser's
  // default scroll-position restoration, and (2) a stale "#section" hash left in
  // the URL (e.g. after a header tile jump) which makes the browser re-jump to
  // that section on every load. Disable restoration and strip any hash before
  // the target element is parsed, so reloads always start at the header.
  (function() {{
    try {{
      if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
      if (location.hash) {{
        history.replaceState(null, '', location.pathname + location.search);
      }}
    }} catch (e) {{}}
  }})();

  // Cache-busting reload. GitHub Pages serves the report with Cache-Control:
  // max-age=600, so a plain location.reload() can be answered from the browser
  // or the Pages CDN with the OLD report even after a fresh deploy (stale HTML
  // with a stale "Last updated" time). A unique ?v= query string is a distinct
  // URL, forcing an origin fetch of the just-published file. sessionStorage
  // (password session) survives same-origin reloads, so this stays unlocked.
  window.reloadFreshReport = function() {{
    try {{
      location.replace(location.pathname + '?v=' + Date.now());
    }} catch (e) {{
      location.reload();
    }}
  }};

  // Keep "Last updated X ago" ticking. The Python side can only bake in
  // "0m ago" (it renders at generation time), so the real relative age is
  // computed here from the embedded generation timestamp and refreshed
  // every 30s while the page stays open.
  (function() {{
    function tick() {{
      var el = document.getElementById('lastUpdatedAgo');
      if (!el) return;
      var gen = parseInt(el.getAttribute('data-generated-ms'), 10);
      if (!gen) return;
      var s = Math.max(0, Math.floor((Date.now() - gen) / 1000));
      var d = Math.floor(s / 86400);
      var h = Math.floor((s % 86400) / 3600);
      var m = Math.floor((s % 3600) / 60);
      el.textContent = d > 0 ? d + 'd ' + h + 'h ago'
                     : h > 0 ? h + 'h ' + m + 'm ago'
                     : m + 'm ago';
    }}
    if (document.readyState === 'loading') {{
      document.addEventListener('DOMContentLoaded', tick);
    }} else {{
      tick();
    }}
    setInterval(tick, 30000);
  }})();
</script>
{base_view_boot_js}
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
<div class="report-header">
  <div class="report-title">
    <h1>{report_title}</h1>
    <div class="hdr-meta">Last updated <span id="lastUpdatedAgo" data-generated-ms="{now_epoch_ms}">{relative_now}</span> · {now}{' · Finnhub enabled' if FINNHUB_API_KEY else ''}</div>
  </div>
  <div class="report-controls">
    {qr_chip_html}
    {refresh_button_html}
    <span class="ctl-icons">
      <button class="icon-btn" id="pageReloadBtn"
              title="Reload this page to pick up the latest published report"
              aria-label="Reload page">&#8635;</button>
      <button class="theme-toggle" id="themeToggle"
              title="Toggle light/dark theme" aria-label="Toggle theme">🌙</button>
    </span>
  </div>
</div>
{refresh_status_html}
{market_meter_html}

<div class="summary-card">
  <div class="summary-row">{holdings_summary}
    {watchlist_stat_html}
    {missed_stat_html}
    {earnings_stat_html}
    {benchmark_stat_html}
  </div>
</div>

<div class="flt-bar" id="fltBar">
  <div class="flt-top">
    <span class="flt-search" id="fltSearchWrap">
      <input type="text" id="searchInput" placeholder="🔍 Search ticker, name or sector…" autocomplete="off">
      <button class="flt-x" id="fltSearchX" title="Clear search" aria-label="Clear search">✕</button>
    </span>
    <button class="flt-btn" id="fltToggle" aria-expanded="false">Filters ▾</button>
    <button class="flt-btn flt-save" id="fltSave">☆ Save</button>
    <button class="flt-btn flt-clear" id="clearFilters">✕ Clear all</button>
    <span class="flt-count" id="filterStatus"></span>
  </div>
  <div class="flt-used" id="fltUsed">
    <span class="flt-used-label">Most used</span>
  </div>
  <div class="flt-chips" id="fltChips"></div>
  <div class="flt-panel" id="fltPanel"></div>
</div>
"""

    # (Quick recommendations now live in the header controls — see qr_chip_html.)

    # Compounder section (only if there are compounder holdings)
    if compounders:
        html += "<h2 id='compounders'>Quality Compounders</h2>\n"
        html += "<div class='table-wrap'><table>\n<thead><tr>"
        html += (
            "<th>Ticker</th>"
            "<th>Name / Sector</th>"
            "<th class='num'>Position</th>"
            "<th class='num'>Cost / Gain</th>"
            "<th class='num'>Price → Target</th>"
            "<th class='num'>Today</th>"
            "<th>Range / Trend</th>"
            "<th>Quality (9)</th>"
            "<th class='num' title='Composite of Quality 30% + Growth 20% + Value 20% + Analyst 15% + Insider 15%. Hover any cell for sub-score breakdown.' style='cursor:help;'>Composite <span style='color:var(--fg-faint);font-weight:400;font-size:10px;text-transform:none;letter-spacing:0;'>&#9432;</span></th>"
            "<th>Analyst Ratings</th>"
            "<th title='Decision verdict from insider activity. &#10003; Supports buy = real open-market buying with personal cash (rare, strong positive). &mdash; No signal = typical compensation, 10b5-1 plans, or tax-withholds (most mega-caps; ignore). &#9888; Caution = discretionary selling large enough relative to market cap to warrant a closer look before buying.' style='cursor:help;'>Insider 90d <span style='color:#bdc3c7;font-size:10px;'>&#9432;</span></th>"
            + _verdict_th() +
            "</tr></thead><tbody>\n"
        )
        orders = _mode_orders(compounders, _holding_rank_key)
        for r in compounders:
            passed = sum(1 for f in r.filters if f.passed)
            rating_score = -1
            if r.rating_breakdown and r.rating_breakdown.get("total"):
                t = r.rating_breakdown["total"]
                rating_score = (
                    (r.rating_breakdown.get("buy", 0)
                     - r.rating_breakdown.get("sell", 0)) / t
                )
            rating_html = _rating_bar(r.rating_breakdown, r.recommendation, r.num_analysts)
            html += _tr_open(r, orders.get(id(r)))
            html += _td(_ticker_cell(r), r.ticker, "ticker")
            html += _td(_name_sector_cell(r), r.name)
            html += _td(_position_cell(r), r.live_market_value or -1, "num")
            html += _td(_cost_gain_cell(r),
                        r.unrealized_gain if r.unrealized_gain is not None else -1e12,
                        "num")
            html += _td(_price_target_cell(r),
                        r.upside_pct if r.upside_pct is not None else -1e6,
                        "num")
            html += _td(_today_cell(r),
                        r.day_change_pct if r.day_change_pct is not None else -1e6,
                        "num")
            html += _td(_range_trend_cell(r),
                        r.week52_position if r.week52_position is not None else -1)
            html += _td(
                f"{_filter_dots(r.filters)} {_filter_count_html(r, passed)}",
                passed,
            )
            html += _td(_score_cell(r.composite_score, r.score_quality, r.score_growth,
                                    r.score_value, r.score_analyst, r.score_insider),
                        r.composite_score if r.composite_score is not None else -1, "num")
            html += _td(rating_html, rating_score)
            html += _td(_insider_cell(r.insider_activity),
                        r.score_insider if r.score_insider is not None else -1)
            html += _verdict_td(r)
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
            # Default order: verdict score high → low (upside as tiebreak).
            # Shared with compute_run_ranks so rank badges match the display.
            items.sort(key=_watchlist_rank_key, reverse=True)
            html += f"<h3 style='margin-top:24px;color:#34495e;'>📋 {wl_name} ({len(items)})</h3>\n"
            html += "<div class='table-wrap'><table>\n<thead><tr>"
            html += (
                "<th>Ticker</th>"
                "<th>Name / Sector</th>"
                "<th class='num'>Price → Target</th>"
                "<th>Range / Trend</th>"
                "<th>Quality (9)</th>"
                "<th class='num' title='Composite of Quality 30% + Growth 20% + Value 20% + Analyst 15% + Insider 15%. Hover any cell for sub-score breakdown.' style='cursor:help;'>Composite <span style='color:var(--fg-faint);font-weight:400;font-size:10px;text-transform:none;letter-spacing:0;'>&#9432;</span></th>"
                "<th>Analyst Ratings</th>"
                "<th title='Decision verdict from insider activity. &#10003; Supports buy = real open-market buying with personal cash (rare, strong positive). &mdash; No signal = typical compensation, 10b5-1 plans, or tax-withholds (most mega-caps; ignore). &#9888; Caution = discretionary selling large enough relative to market cap to warrant a closer look before buying.' style='cursor:help;'>Insider 90d <span style='color:#bdc3c7;font-size:10px;'>&#9432;</span></th>"
                + _verdict_th() +
                "</tr></thead><tbody>\n"
            )
            orders = _mode_orders(items, _watchlist_rank_key)
            for r in items:
                passed = sum(1 for f in r.filters if f.passed)
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
                quality_cell = (
                    f"{_filter_dots(r.filters)} {_filter_count_html(r, passed)}"
                    if r.filters else "<span style='color:var(--fg-faint);'>n/a</span>"
                )
                html += _tr_open(r, orders.get(id(r)))
                html += _td(_ticker_cell(r), r.ticker, "ticker")
                html += _td(_name_sector_cell(r), r.name)
                # Watchlist items omit Position / Cost-Gain (you don't own them)
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
                html += _verdict_td(r)
                html += "</tr>\n"
            html += "</tbody></table></div>\n"

    # ---------- Missed Opportunities (recs we under-acted on) ----------
    # `missed_opportunities is not None` signals the history-tracking flow is
    # active (main portfolio run) — render even when empty so the section is
    # discoverable. Ad-hoc/lookup mode passes None and the section is omitted.
    if missed_opportunities is not None:
        html += _render_missed_opportunities(
            missed_opportunities, recs_tracked_count,
            avoided=avoided_losses, insights=missed_insights,
            analysis_md=missed_analysis_md)

    # ---------- Screening section (passed-the-screen universe) ----------
    if screening_results:
        html += _render_screening_section(screening_results)

    # ---------- ETFs & Thematic positions (moved before Tax section) ----------
    if thematics:
        html += "<h2 id='thematic'>ETFs &amp; Thematic Positions</h2>\n"
        html += "<div class='table-wrap'><table>\n<thead><tr>"
        html += (
            "<th>Ticker</th>"
            "<th>Name</th>"
            "<th class='num'>Position</th>"
            "<th class='num'>Cost / Gain</th>"
            "<th class='num'>Price → Target</th>"
            "<th class='num'>Today</th>"
            "<th>Range / Trend</th>"
            "<th class='num' title='Composite of Quality 30% + Growth 20% + Value 20% + Analyst 15% + Insider 15%. Hover any cell for sub-score breakdown.' style='cursor:help;'>Composite <span style='color:var(--fg-faint);font-weight:400;font-size:10px;text-transform:none;letter-spacing:0;'>&#9432;</span></th>"
            "<th>Analyst Ratings</th>"
            "<th title='Decision verdict from insider activity. &#10003; Supports buy = real open-market buying with personal cash (rare, strong positive). &mdash; No signal = typical compensation, 10b5-1 plans, or tax-withholds (most mega-caps; ignore). &#9888; Caution = discretionary selling large enough relative to market cap to warrant a closer look before buying.' style='cursor:help;'>Insider 90d <span style='color:#bdc3c7;font-size:10px;'>&#9432;</span></th>"
            "<th>Verdict <span style='color:var(--fg-faint);font-weight:400;font-size:10px;text-transform:none;letter-spacing:0;'>(score)</span></th>"
            "</tr></thead><tbody>\n"
        )
        for r in thematics:
            rating_score = -1
            if r.rating_breakdown and r.rating_breakdown.get("total"):
                t = r.rating_breakdown["total"]
                rating_score = (
                    (r.rating_breakdown.get("buy", 0)
                     - r.rating_breakdown.get("sell", 0)) / t
                )
            rating_html = _rating_bar(r.rating_breakdown, r.recommendation, r.num_analysts)
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
            html += _td(_today_cell(r),
                        r.day_change_pct if r.day_change_pct is not None else -1e6,
                        "num")
            html += _td(_range_trend_cell(r),
                        r.week52_position if r.week52_position is not None else -1)
            html += _td(_score_cell(r.composite_score, r.score_quality, r.score_growth,
                                    r.score_value, r.score_analyst, r.score_insider),
                        r.composite_score if r.composite_score is not None else -1, "num")
            html += _td(rating_html, rating_score)
            html += _td(_insider_cell(r.insider_activity),
                        r.score_insider if r.score_insider is not None else -1)
            html += _verdict_td(r)
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
Quality dots: green = pass, red = fail, grey = no data. Hover for actual values.
Click any column header to sort. Click again to reverse.
Verdicts are framework outputs, not investment advice.
</p>
<script>
// Base-score view switch. Every verdict-dependent piece of the report is
// rendered once per base-score mode (.bmode copies, shown by CSS from
// html[data-base-view]), and rows carry each mode's verdict, score, rank move
// and tax flag as data-<attr>-<mode>. Switching copies the viewed mode's
// values into the plain attributes that sorting and the filter bar read,
// restores the table order, and remembers the choice in this browser.
(function() {
  var MODES = window.BASE_SCORE_MODES || [];
  var BASES = window.BASE_SCORE_BASES || [];
  var CAL_SUFFIX = window.BASE_CAL_SUFFIX || '-cal';
  var VIEW_KEY = 'base-score-view';
  // A mode key is "<base>" or "<base><CAL_SUFFIX>" — the two switch groups
  // each set one half and leave the other alone.
  function splitMode(mode) {
    var cal = 'std', base = mode || '';
    if (base.slice(-CAL_SUFFIX.length) === CAL_SUFFIX) {
      base = base.slice(0, -CAL_SUFFIX.length);
      cal = 'cal';
    }
    if (BASES.indexOf(base) === -1) base = BASES[0];
    return {base: base, cal: cal};
  }
  function joinMode(base, cal) {
    return base + (cal === 'cal' ? CAL_SUFFIX : '');
  }
  var PER_MODE = ['verdict', 'verdict-score', 'rank-move', 'rank-delta', 'has-tax'];
  var root = document.documentElement;
  var sw = document.getElementById('baseScoreSwitch');

  function sortValue(td) {
    var s = td ? td.getAttribute('data-sort') : null;
    if (s === null || s === '') return null;
    var n = parseFloat(s);
    return isNaN(n) ? s.toLowerCase() : n;
  }
  // Default order comes from data-order-<mode>. A table the reader sorted by
  // the verdict column is re-sorted the same way on the new scores; one sorted
  // by any other column keeps its order.
  function reorder(table, mode) {
    var tbody = table.tBodies[0];
    if (!tbody || !tbody.rows.length) return;
    var key = 'data-order-' + mode;
    var rows = Array.prototype.slice.call(tbody.rows);
    if (!rows.every(function(r) { return r.hasAttribute(key); })) return;
    var sorted = table.querySelector('th.sort-asc, th.sort-desc');
    if (sorted && !sorted.classList.contains('verdict-th')) return;
    if (sorted) {
      var idx = sorted.cellIndex, asc = sorted.classList.contains('sort-asc');
      rows.sort(function(a, b) {
        var av = sortValue(a.cells[idx]), bv = sortValue(b.cells[idx]);
        if (av === null && bv === null) return 0;
        if (av === null) return 1;
        if (bv === null) return -1;
        var cmp = (typeof av === 'number' && typeof bv === 'number')
          ? av - bv : String(av).localeCompare(String(bv));
        return asc ? cmp : -cmp;
      });
    } else {
      rows.sort(function(a, b) { return a.getAttribute(key) - b.getAttribute(key); });
    }
    rows.forEach(function(r) { tbody.appendChild(r); });
  }
  function apply(mode) {
    root.setAttribute('data-base-view', mode);
    document.querySelectorAll('tr[data-verdict-' + mode + ']').forEach(function(tr) {
      PER_MODE.forEach(function(attr) {
        var v = tr.getAttribute('data-' + attr + '-' + mode);
        if (v !== null) tr.setAttribute('data-' + attr, v);
      });
    });
    document.querySelectorAll('td[data-sort-' + mode + ']').forEach(function(td) {
      td.setAttribute('data-sort', td.getAttribute('data-sort-' + mode));
    });
    document.querySelectorAll('table').forEach(function(t) { reorder(t, mode); });
    if (sw) {
      var parts = splitMode(mode);
      sw.querySelectorAll('button[data-base]').forEach(function(b) {
        b.setAttribute('aria-pressed',
          b.getAttribute('data-base') === parts.base ? 'true' : 'false');
      });
      sw.querySelectorAll('button[data-cal]').forEach(function(b) {
        b.setAttribute('aria-pressed',
          b.getAttribute('data-cal') === parts.cal ? 'true' : 'false');
      });
    }
    document.dispatchEvent(new CustomEvent('basescorechange', {detail: {mode: mode}}));
  }
  window.setBaseView = function(mode) {
    if (MODES.indexOf(mode) === -1) return;
    try { localStorage.setItem(VIEW_KEY, mode); } catch (e) {}
    apply(mode);
  };
  if (sw) {
    sw.addEventListener('click', function(ev) {
      var btn = ev.target.closest('button[data-base], button[data-cal]');
      if (!btn) return;
      var cur = splitMode(root.getAttribute('data-base-view'));
      var base = btn.getAttribute('data-base') || cur.base;
      var cal = btn.getAttribute('data-cal') || cur.cal;
      window.setBaseView(joinMode(base, cal));
    });
  }
  // The early script may have restored another mode than the run's.
  var initial = root.getAttribute('data-base-view');
  if (MODES.indexOf(initial) !== -1) apply(initial);
})();
// Measure the sticky h2 height so pinned table headers (thead) sit exactly
// beneath it (CSS uses top: var(--h2-pin-h)). Re-measured on resize because
// the h2 height changes with viewport font scaling.
(function() {
  function setPinOffset() {
    var h2 = document.querySelector('h2');
    if (h2) {
      // -1px overlap avoids a hairline gap between h2 and pinned thead
      document.documentElement.style.setProperty(
        '--h2-pin-h', (h2.offsetHeight - 1) + 'px');
    }
  }
  setPinOffset();
  window.addEventListener('resize', setPinOffset);
  window.addEventListener('load', setPinOffset);
})();
// Browser reload button. GitHub Pages caches the report, so this uses the
// cache-busting reload (reloadFreshReport) to fetch whatever is currently
// published rather than a stale copy. Distinct from "Refresh data", which
// dispatches the Actions workflow to regenerate the report from live data.
(function() {
  var btn = document.getElementById('pageReloadBtn');
  if (!btn) return;
  btn.addEventListener('click', function() {
    btn.disabled = true;
    (window.reloadFreshReport || location.reload.bind(location))();
  });
})();
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

/* ---------- Filter bar: faceted pills + range sliders + active chips ---------- */
(function() {
  var searchInput = document.getElementById('searchInput');
  var searchWrap  = document.getElementById('fltSearchWrap');
  var searchX     = document.getElementById('fltSearchX');
  var clearBtn    = document.getElementById('clearFilters');
  var toggleBtn   = document.getElementById('fltToggle');
  var panel       = document.getElementById('fltPanel');
  var usedEl      = document.getElementById('fltUsed');
  var usedLabel   = usedEl ? usedEl.querySelector('.flt-used-label') : null;
  var saveBtn     = document.getElementById('fltSave');
  var chipsEl     = document.getElementById('fltChips');
  var statusEl    = document.getElementById('filterStatus');
  if (!searchInput || !panel) return;

  var rows = Array.prototype.slice.call(document.querySelectorAll('tbody tr'))
               .filter(function(r) { return r.hasAttribute('data-verdict'); });

  // ---- Config: every metric a row exposes, grouped into collapsible sections.
  //      type:'facet'  -> categorical, OR within a card, AND across cards.
  //      type:'range'  -> numeric min/max dual slider (AND).
  //      Controls render only when the data actually varies, so reports with
  //      no tax/watchlist/insider rows simply don't show those controls. ----
  var SECTIONS = [
    { id: 'verdict', title: 'Verdict & signals', cards: [
      { type:'facet', attr:'verdict', label:'Verdict',
        order:['ADD','BUY','HOLD','WAIT','WATCH','TRIM','SELL','PASS'] },
      { type:'facet', attr:'recommendation', label:'Analyst rating', short:'Analyst', skip:['none',''],
        order:['strong_buy','buy','hold','sell','strong_sell'],
        labels:{strong_buy:'Strong Buy',buy:'Buy',hold:'Hold',sell:'Sell',strong_sell:'Strong Sell'} },
      { type:'facet', attr:'insider', label:'Insider 90d', short:'Insider', skip:[''],
        order:['supports_buy','no_signal','caution'],
        labels:{supports_buy:'✓ Buying',no_signal:'No signal',caution:'⚠ Caution'} },
      { type:'facet', attr:'news', label:'News sentiment', short:'News', skip:[''],
        order:['bullish','neutral','bearish'],
        labels:{bullish:'📰 Bullish',neutral:'📰 Neutral',bearish:'📰 Bearish'} },
      { type:'range', attr:'news-score', label:'News score (−1…+1)', short:'News score', step:0.1, signed:true },
      { type:'range', attr:'verdict-score', label:'Verdict score', short:'Verdict pts', integer:true },
      { type:'range', attr:'quality', label:'Quality gates', short:'Quality', integer:true },
      { type:'range', attr:'score', label:'Composite score', short:'Composite', integer:true },
    ]},
    { id: 'price', title: 'Price & value', cards: [
      { type:'range', attr:'upside', label:'Upside to target', short:'Upside', unit:'%' },
      { type:'range', attr:'gain-pct', label:'Total gain', short:'Gain', unit:'%' },
      { type:'range', attr:'gain', label:'Gain', short:'Gain $', money:true },
      { type:'range', attr:'day-pct', label:'Today', short:'Today', unit:'%' },
      { type:'range', attr:'pos52', label:'52-wk position', short:'52-wk', unit:'%' },
      { type:'range', attr:'ma-pct', label:'vs 200-day MA', short:'vs 200d', unit:'%' },
    ]},
    { id: 'class', title: 'Classification', cards: [
      { type:'facet', attr:'sector', label:'Sector', skip:[''] },
      { type:'facet', attr:'bucket', label:'Type', skip:[''],
        labels:{compounder:'Compounder',thematic:'Thematic',etf:'ETF'} },
      { type:'facet', attr:'sector-mom', label:'Sector momentum', short:'Sector', skip:['Unknown',''],
        order:['Hot','Neutral','Cool'],
        labels:{Hot:'🔥 Hot',Neutral:'→ Neutral',Cool:'❄️ Cool'} },
      { type:'facet', attr:'trend', label:'Price trend', short:'Trend', skip:[''],
        order:['uptrend','sideways','downtrend'],
        labels:{uptrend:'↑ Uptrend',sideways:'→ Sideways',downtrend:'↓ Downtrend'} },
    ]},
    { id: 'timing', title: 'Position & timing', cards: [
      { type:'range', attr:'port-pct', label:'Position size', short:'Position', unit:'%' },
      { type:'range', attr:'days-held', label:'Days held', short:'Held', unit:'d', integer:true },
      { type:'range', attr:'earnings-days', label:'Days to earnings', short:'Earnings in', unit:'d', integer:true },
      { type:'facet', attr:'rank-move', label:'Rank movement', short:'Rank', skip:[''],
        order:['up','down','new'],
        labels:{up:'▲ Moved up',down:'▼ Moved down','new':'★ New today'} },
      { type:'range', attr:'rank-delta', label:'Rank Δ (places)', short:'Rank Δ', integer:true },
      { type:'facet', attr:'has-tax', label:'Tax', skip:['0',''], labels:{'1':'Has tax detail'} },
    ]},
  ];

  // Named presets. They (a) back the header summary tiles via
  // window.applyHeaderFilter(key) and (b) seed the "Most used" bar until your
  // own history builds up. `facets`/`ranges` describe a full target state;
  // a null range bound means "open to the data's edge".
  var PRESETS = [
    { key:'verdict-add',   label:'Add candidates',       facets:{verdict:['ADD']} },
    { key:'action',        label:'Sell / Trim',          facets:{verdict:['SELL','TRIM']} },
    { key:'verdict-buy',   label:'Buy (watchlist)',      facets:{verdict:['BUY']} },
    { key:'high-quality',  label:'Quality ≥7',      ranges:{quality:[7,null]} },
    { key:'insider-buy',   label:'✓ Insider buying', facets:{insider:['supports_buy']} },
    { key:'news-bullish',  label:'📰 Bullish news', facets:{news:['bullish']} },
    { key:'hot-sector',    label:'🔥 Hot sector', facets:{'sector-mom':['Hot']} },
    { key:'big-upside',    label:'Upside ≥15%',      ranges:{upside:[15,null]} },
    { key:'earnings-soon', label:'📅 Reports ≤7d', ranges:{'earnings-days':[0,7]} },
    { key:'tax-loss',      label:'Loss harvest',          ranges:{'gain-pct':[null,-5]} },
  ];
  var USAGE_KEY  = 'fltComboUsage';   // { signature: {c:count, t:lastUsedMs} }
  var SAVED_KEY  = 'fltSavedViews';   // [{ sig, name, t }] — combos you named; pinned first
  var HIDDEN_KEY = 'fltHiddenCombos'; // { signature: 1 } — combos you took off the bar
  var MAX_SHOWN  = 12;                // learned combos to render alongside saved ones

  // ---------- State ----------
  var state = { facets: {}, ranges: {} };   // facets: attr->Set; ranges: attr->{min,max}
  var domains = {};                         // attr -> {min,max,step,integer}
  var LS = { open:'fltPanelOpen', collapsed:'fltCollapsed' };

  function attrRaw(row, attr) { var v = row.getAttribute('data-' + attr); return v == null ? '' : v; }
  function attrNum(row, attr) { var v = row.getAttribute('data-' + attr);
    if (v == null || v === '') return NaN; var n = parseFloat(v); return isNaN(n) ? NaN : n; }
  // Base-score-dependent attributes switch with the base view, so their
  // controls are built from every mode's values (data-<attr>-<mode>) too.
  function attrVariants(row, attr) {
    var out = [attr];
    (window.BASE_SCORE_MODES || []).forEach(function(m) {
      if (row.hasAttribute('data-' + attr + '-' + m)) out.push(attr + '-' + m);
    });
    return out;
  }

  function niceStep(span) {
    if (span <= 0) return 1;
    var raw = span / 200, mag = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / mag;
    return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * mag;
  }
  function computeDomain(card) {
    var vals = [];
    for (var i = 0; i < rows.length; i++) {
      attrVariants(rows[i], card.attr).forEach(function(a) {
        var v = attrNum(rows[i], a); if (!isNaN(v)) vals.push(v);
      });
    }
    if (!vals.length) return null;
    var mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    if (mx <= mn) return null;
    var step = card.step || (card.integer ? 1 : niceStep(mx - mn));
    mn = Math.floor(mn / step) * step; mx = Math.ceil(mx / step) * step;
    return { min: mn, max: mx, step: step, integer: !!card.integer };
  }
  function presentValues(card) {
    var seen = {}, skip = card.skip || [];
    for (var i = 0; i < rows.length; i++) {
      attrVariants(rows[i], card.attr).forEach(function(a) {
        var v = attrRaw(rows[i], a);
        if (skip.indexOf(v) === -1) seen[v] = (seen[v] || 0) + 1;
      });
    }
    var keys = Object.keys(seen);
    var ord = card.order || [];
    keys.sort(function(a, b) {
      var ia = ord.indexOf(a), ib = ord.indexOf(b);
      if (ia !== -1 || ib !== -1) { if (ia === -1) ia = 99; if (ib === -1) ib = 99; return ia - ib; }
      return a < b ? -1 : a > b ? 1 : 0;
    });
    return keys;
  }
  function cardFor(attr) { return cardEls[attr] ? cardEls[attr].card : null; }
  function optLabel(card, v) { return (card && card.labels && card.labels[v]) || v; }

  function fmtNum(v, card) {
    if (card.money) {
      var a = Math.abs(v), s = v < 0 ? '-' : '';
      if (a >= 1000) return s + '$' + (a / 1000).toFixed(a >= 100000 ? 0 : 1) + 'k';
      return s + '$' + a.toFixed(0);
    }
    var step = domains[card.attr] && domains[card.attr].step;
    var d = (step && step < 1) ? (step < 0.1 ? 2 : 1) : 0;
    var s = (card.signed && v > 0) ? '+' : '';
    return s + v.toFixed(d) + (card.unit || '');
  }

  // ---------- Matching ----------
  function passSearch(row) {
    var t = searchInput.value.trim().toLowerCase();
    if (!t) return true;
    return (row.getAttribute('data-search') || '').indexOf(t) !== -1;
  }
  function passFacet(row, attr) {
    var set = state.facets[attr]; if (!set || !set.size) return true;
    return set.has(attrRaw(row, attr));
  }
  function passRange(row, attr) {
    var r = state.ranges[attr]; if (!r) return true;
    var v = attrNum(row, attr); if (isNaN(v)) return false;
    return v >= r.min - 1e-9 && v <= r.max + 1e-9;
  }
  function passAll(row, skipAttr) {
    if (!passSearch(row)) return false;
    for (var a in state.facets) if (a !== skipAttr && !passFacet(row, a)) return false;
    for (var b in state.ranges) if (b !== skipAttr && !passRange(row, b)) return false;
    return true;
  }

  // ---------- Rendering: build the panel from config ----------
  var cardEls = {};   // attr -> {el, kind, card, ...}
  function loadCollapsed() { try { return JSON.parse(localStorage.getItem(LS.collapsed)) || {}; } catch (e) { return {}; } }
  function saveCollapsed(c) { try { localStorage.setItem(LS.collapsed, JSON.stringify(c)); } catch (e) {} }

  function buildPanel() {
    var collapsed = loadCollapsed();
    SECTIONS.forEach(function(sec) {
      var live = sec.cards.filter(function(card) {
        if (card.type === 'range') { var d = computeDomain(card); if (!d) return false; domains[card.attr] = d; return true; }
        return presentValues(card).length >= 2;   // a facet needs a real choice
      });
      if (!live.length) return;

      var secEl = document.createElement('div');
      secEl.className = 'flt-section' + (collapsed[sec.id] ? ' collapsed' : '');
      secEl.setAttribute('data-sec', sec.id);
      var head = document.createElement('div');
      head.className = 'flt-section-head';
      head.innerHTML = '<span class="flt-caret">▼</span>' +
                       '<span class="flt-section-title">' + sec.title + '</span>' +
                       '<span class="flt-section-active" data-sec-active="' + sec.id + '"></span>';
      head.addEventListener('click', function() {
        secEl.classList.toggle('collapsed');
        var c = loadCollapsed(); c[sec.id] = secEl.classList.contains('collapsed'); saveCollapsed(c);
      });
      secEl.appendChild(head);

      var grid = document.createElement('div');
      grid.className = 'flt-cards';
      live.forEach(function(card) {
        grid.appendChild(card.type === 'range' ? buildRangeCard(card) : buildFacetCard(card));
      });
      secEl.appendChild(grid);
      panel.appendChild(secEl);
    });
  }

  function buildFacetCard(card) {
    var el = document.createElement('div');
    el.className = 'flt-card'; el.setAttribute('data-attr', card.attr);
    var reset = '<span class="flt-card-reset" data-reset="' + card.attr + '">reset</span>';
    el.innerHTML = '<div class="flt-card-label"><span>' + card.label + '</span>' + reset + '</div>';
    var opts = document.createElement('div'); opts.className = 'flt-opts';
    presentValues(card).forEach(function(v) {
      var b = document.createElement('button');
      b.className = 'flt-opt'; b.setAttribute('data-val', v);
      b.innerHTML = '<span>' + optLabel(card, v) + '</span><span class="flt-n"></span>';
      b.addEventListener('click', function() {
        var set = state.facets[card.attr] || (state.facets[card.attr] = new Set());
        if (set.has(v)) set.delete(v); else set.add(v);
        if (!set.size) delete state.facets[card.attr];
        refresh();
      });
      opts.appendChild(b);
    });
    el.appendChild(opts);
    el.querySelector('[data-reset]').addEventListener('click', function() {
      delete state.facets[card.attr]; refresh();
    });
    cardEls[card.attr] = { el: el, kind: 'facet', card: card };
    return el;
  }

  function buildRangeCard(card) {
    var d = domains[card.attr];
    var el = document.createElement('div');
    el.className = 'flt-card'; el.setAttribute('data-attr', card.attr);
    el.innerHTML =
      '<div class="flt-card-label"><span>' + card.label + '</span>' +
        '<span class="flt-card-reset" data-reset="' + card.attr + '">reset</span></div>' +
      '<div class="flt-range-vals"><span class="flt-lo"></span>' +
        '<span class="flt-range-n"></span><span class="flt-hi"></span></div>' +
      '<div class="flt-slider"><div class="flt-track"></div><div class="flt-fill"></div>' +
        '<input type="range" class="flt-in-lo" min="' + d.min + '" max="' + d.max + '" step="' + d.step + '" value="' + d.min + '">' +
        '<input type="range" class="flt-in-hi" min="' + d.min + '" max="' + d.max + '" step="' + d.step + '" value="' + d.max + '"></div>';
    var lo = el.querySelector('.flt-in-lo'), hi = el.querySelector('.flt-in-hi');
    function onInput() {
      var l = +lo.value, h = +hi.value;
      if (l > h) { if (this === lo) { h = l; hi.value = h; } else { l = h; lo.value = l; } }
      if (l <= d.min && h >= d.max) delete state.ranges[card.attr];
      else state.ranges[card.attr] = { min: l, max: h };
      refresh();
    }
    lo.addEventListener('input', onInput); hi.addEventListener('input', onInput);
    el.querySelector('[data-reset]').addEventListener('click', function() {
      delete state.ranges[card.attr]; refresh();
    });
    var entry = { el: el, kind: 'range', card: card, lo: lo, hi: hi,
                  fill: el.querySelector('.flt-fill'),
                  loLbl: el.querySelector('.flt-lo'), hiLbl: el.querySelector('.flt-hi'),
                  nLbl: el.querySelector('.flt-range-n') };
    cardEls[card.attr] = entry;
    return el;
  }

  function syncRange(entry) {
    var card = entry.card, d = domains[card.attr], r = state.ranges[card.attr];
    var lo = r ? r.min : d.min, hi = r ? r.max : d.max;
    entry.lo.value = lo; entry.hi.value = hi;
    var span = d.max - d.min || 1;
    var lp = (lo - d.min) / span * 100, hp = (hi - d.min) / span * 100;
    entry.fill.style.left = lp + '%'; entry.fill.style.width = (hp - lp) + '%';
    entry.loLbl.textContent = fmtNum(lo, card); entry.hiLbl.textContent = fmtNum(hi, card);
    entry.lo.style.zIndex = (lo > (d.min + d.max) / 2) ? 5 : 3;
    entry.el.classList.toggle('narrowed', !!r);
  }

  // ---------- State signatures + (de)serialization ----------
  // A "combo" is a full facets+ranges state (search excluded). Ranges use '~'
  // between the bounds so negative numbers survive the round-trip.
  function signature(st) {
    var parts = [];
    Object.keys(st.facets).sort().forEach(function(a) {
      if (st.facets[a] && st.facets[a].size) parts.push('f:' + a + '=' + Array.from(st.facets[a]).sort().join(','));
    });
    Object.keys(st.ranges).sort().forEach(function(a) {
      parts.push('r:' + a + '=' + st.ranges[a].min + '~' + st.ranges[a].max);
    });
    return parts.join('|');
  }
  function parseSignature(sig) {
    var st = { facets: {}, ranges: {} };
    if (!sig) return st;
    sig.split('|').forEach(function(part) {
      var kind = part.charAt(0), body = part.slice(2), eq = body.indexOf('=');
      if (eq < 0) return;
      var attr = body.slice(0, eq), val = body.slice(eq + 1);
      if (kind === 'f') st.facets[attr] = new Set(val.split(','));
      else { var mm = val.split('~'); if (mm.length === 2) st.ranges[attr] = { min: parseFloat(mm[0]), max: parseFloat(mm[1]) }; }
    });
    return st;
  }
  function resolveScreen(s) {
    var st = { facets: {}, ranges: {} };
    if (s.facets) for (var f in s.facets) st.facets[f] = new Set(s.facets[f]);
    if (s.ranges) for (var a in s.ranges) {
      var d = domains[a]; if (!d) continue;
      var lo = s.ranges[a][0], hi = s.ranges[a][1];
      lo = (lo == null) ? d.min : Math.max(d.min, lo);
      hi = (hi == null) ? d.max : Math.min(d.max, hi);
      if (lo > d.min || hi < d.max) st.ranges[a] = { min: lo, max: hi };
    }
    return st;
  }
  function applyState(st) {
    state.facets = {}; state.ranges = {};
    for (var f in st.facets) if (st.facets[f].size) state.facets[f] = new Set(st.facets[f]);
    for (var r in st.ranges) state.ranges[r] = { min: st.ranges[r].min, max: st.ranges[r].max };
    refresh();
  }

  // Short human label for a combo (used by the "Most used" pills).
  function comboLabel(st) {
    var toks = [];
    Object.keys(st.facets).forEach(function(a) {
      var card = cardFor(a);
      st.facets[a].forEach(function(v) { toks.push(card ? optLabel(card, v) : v); });
    });
    Object.keys(st.ranges).forEach(function(a) {
      var card = cardFor(a), d = domains[a], r = st.ranges[a];
      if (!card || !d) { toks.push(a); return; }
      var nm = card.short || card.label;
      var loOpen = r.min <= d.min + 1e-9, hiOpen = r.max >= d.max - 1e-9;
      if (loOpen) toks.push(nm + ' ≤' + fmtNum(r.max, card));
      else if (hiOpen) toks.push(nm + ' ≥' + fmtNum(r.min, card));
      else toks.push(nm + ' ' + fmtNum(r.min, card) + '–' + fmtNum(r.max, card));
    });
    if (!toks.length) return '';
    return toks.length > 3 ? toks.slice(0, 3).join(' · ') + ' +' + (toks.length - 3) : toks.join(' · ');
  }

  // ---------- "Most used" combos ----------
  var seedSigs = [], seedLabel = {};
  function buildSeeds() {
    PRESETS.forEach(function(p) {
      var usable = (!p.ranges) || Object.keys(p.ranges).every(function(a) { return domains[a]; });
      if (!usable) return;
      var sig = signature(resolveScreen(p));
      if (!sig || seedLabel[sig]) return;
      seedSigs.push(sig); seedLabel[sig] = p.label;
    });
  }
  function loadUsage() { try { return JSON.parse(localStorage.getItem(USAGE_KEY)) || {}; } catch (e) { return {}; } }
  function saveUsage(u) { try { localStorage.setItem(USAGE_KEY, JSON.stringify(u)); } catch (e) {} }

  // ---------- Saved (named) combos ----------
  // A saved view is one you named: it pins to the front of the bar and only you
  // remove it. Learned combos fill the rest and can be dropped with the same ✕,
  // which also forgets their usage so they don't drift straight back in. All
  // three stores are per-browser localStorage — nothing goes in the HTML.
  function loadSaved() {
    try { var a = JSON.parse(localStorage.getItem(SAVED_KEY)); return (a instanceof Array) ? a : []; }
    catch (e) { return []; }
  }
  function storeSaved(list) { try { localStorage.setItem(SAVED_KEY, JSON.stringify(list)); } catch (e) {} }
  function savedEntry(sig) {
    var l = loadSaved();
    for (var i = 0; i < l.length; i++) if (l[i].sig === sig) return l[i];
    return null;
  }
  function loadHidden() { try { return JSON.parse(localStorage.getItem(HIDDEN_KEY)) || {}; } catch (e) { return {}; } }
  function storeHidden(h) { try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(h)); } catch (e) {} }

  // A combo only makes sense in this report if every range it names exists here
  // — a missing metric would filter every row away. (Same rule buildSeeds uses.)
  function usableSig(sig) {
    var st = parseSignature(sig);
    for (var a in st.ranges) if (!domains[a]) return false;
    return true;
  }

  // Name / rename / delete, all in one prompt: a blank name deletes the view.
  function promptSave(sig) {
    if (!sig) return;
    var ent = savedEntry(sig);
    var fallback = seedLabel[sig] || comboLabel(parseSignature(sig)) || 'My filter';
    var name = window.prompt(
      ent ? 'Rename this saved filter (blank deletes it):'
          : 'Name these filters — saved in this browser only:',
      ent ? ent.name : fallback);
    if (name === null) return;                       // cancelled
    name = name.replace(/\s+/g, ' ').trim().slice(0, 40);
    var list = loadSaved(), at = -1;
    for (var i = 0; i < list.length; i++) if (list[i].sig === sig) at = i;
    if (!name) { if (at >= 0) list.splice(at, 1); }
    else if (at >= 0) list[at].name = name;
    else list.push({ sig: sig, name: name, t: Date.now() });
    storeSaved(list);
    if (name) { var h = loadHidden(); delete h[sig]; storeHidden(h); }   // naming un-hides
    renderUsed(); syncSaveBtn();
  }

  // ✕ on a pill: off the bar. A saved view is unsaved; every combo is hidden and
  // its usage forgotten, so nothing you removed comes back on its own.
  function removeCombo(sig) {
    var list = loadSaved(), kept = list.filter(function(v) { return v.sig !== sig; });
    if (kept.length !== list.length) storeSaved(kept);
    var h = loadHidden(); h[sig] = 1; storeHidden(h);
    var u = loadUsage(); if (u[sig]) { delete u[sig]; saveUsage(u); }
    renderUsed(); syncSaveBtn();
  }

  // The Save button doubles as the rename/delete entry point for whatever combo
  // is showing, so a saved view needs no control of its own beyond the pill's ✕.
  function syncSaveBtn() {
    if (!saveBtn) return;
    var sig = signature(state), ent = sig ? savedEntry(sig) : null;
    saveBtn.textContent = ent ? '★ Saved' : '☆ Save';
    saveBtn.classList.toggle('saved', !!ent);
    saveBtn.style.opacity = sig ? '1' : '.4';
    saveBtn.style.pointerEvents = sig ? 'auto' : 'none';
    saveBtn.title = !sig ? 'Pick some filters first, then save them under a name'
      : ent ? 'Rename or delete “' + ent.name + '”'
            : 'Save these filters under a name (this browser only)';
  }

  var recTimer = null, lastRecSig = '';
  function scheduleRecord() {
    clearTimeout(recTimer);
    recTimer = setTimeout(function() {
      var sig = signature(state);
      if (!sig) return;                      // never record the empty state
      var u = loadUsage();
      var e = u[sig] || (u[sig] = { c: 0, t: 0 });
      e.c += 1; e.t = Date.now();
      var keys = Object.keys(u);
      if (keys.length > 40) {                // cap growth: keep 40 most-used
        keys.sort(function(a, b) { return (u[b].c - u[a].c) || (u[b].t - u[a].t); });
        var t = {}; keys.slice(0, 40).forEach(function(k) { t[k] = u[k]; }); u = t;
      }
      saveUsage(u); renderUsed();
    }, 1200);
  }

  // Bar order: your saved views first (never crowded out), then the combos you
  // actually use, then the seeded suggestions — minus anything you removed.
  function renderUsed() {
    var saved = loadSaved(), hidden = loadHidden(), u = loadUsage();
    var seen = {}, combos = [];
    saved.forEach(function(v) {
      if (seen[v.sig] || !usableSig(v.sig)) return;
      seen[v.sig] = 1; combos.push({ sig: v.sig, name: v.name, saved: true });
    });
    Object.keys(u).map(function(sig) { return { sig: sig, c: u[sig].c, t: u[sig].t }; })
      .sort(function(a, b) { return (b.c - a.c) || (b.t - a.t); })
      .forEach(function(c) {
        if (seen[c.sig] || hidden[c.sig] || !usableSig(c.sig)) return;
        seen[c.sig] = 1; combos.push({ sig: c.sig, c: c.c });
      });
    seedSigs.forEach(function(sig) {
      if (seen[sig] || hidden[sig]) return;
      seen[sig] = 1; combos.push({ sig: sig, c: 0 });
    });

    // rebuild (keep the label span, drop old pills)
    usedEl.querySelectorAll('.flt-combo').forEach(function(b) { b.remove(); });
    var cur = signature(state), shown = 0;
    combos.forEach(function(c) {
      if (!c.saved && shown >= MAX_SHOWN) return;
      var label = c.name || seedLabel[c.sig] || comboLabel(parseSignature(c.sig));
      if (!label) return;
      var b = document.createElement('button');
      b.className = 'flt-combo' + (c.saved ? ' named' : '') +
                    (c.sig === cur && cur !== '' ? ' on' : '');
      b.setAttribute('data-sig', c.sig);
      b.title = c.saved ? 'Saved filter — click to apply, ✕ to delete'
              : c.c ? ('Used ' + c.c + '× — click to apply, ✕ to remove from the bar')
                    : 'Suggested combo — click to apply, ✕ to remove from the bar';
      var txt = document.createElement('span');
      txt.textContent = label;
      b.appendChild(txt);
      var x = document.createElement('span');
      x.className = 'flt-combo-x';
      x.textContent = '✕';
      x.title = c.saved ? 'Delete this saved filter' : 'Remove from the bar';
      x.addEventListener('click', function(ev) { ev.stopPropagation(); removeCombo(c.sig); });
      b.appendChild(x);
      b.addEventListener('click', function() {
        applyState(signature(state) === c.sig ? { facets:{}, ranges:{} } : parseSignature(c.sig));
      });
      usedEl.appendChild(b);
      if (!c.saved) shown++;
    });

    if (Object.keys(hidden).length) {              // way back from an over-zealous ✕
      var r = document.createElement('button');
      r.className = 'flt-combo flt-restore';
      r.title = 'Bring back the quick filters you removed';
      r.appendChild(document.createTextNode('Restore removed'));
      r.addEventListener('click', function() { storeHidden({}); renderUsed(); });
      usedEl.appendChild(r);
    }
    if (usedLabel) usedLabel.textContent = saved.length ? 'Saved & most used' : 'Most used';
  }

  // ---------- Refresh everything ----------
  function refresh() {
    var visible = 0, total = rows.length;
    rows.forEach(function(row) {
      var ok = passAll(row);
      row.style.display = ok ? '' : 'none';
      if (ok) visible++;
    });

    document.querySelectorAll('.table-wrap').forEach(function(wrap) {
      var any = false;
      wrap.querySelectorAll('tbody tr').forEach(function(r) { if (r.style.display !== 'none') any = true; });
      wrap.style.display = any ? '' : 'none';
      var prev = wrap.previousElementSibling;
      while (prev && prev.tagName !== 'H2' && prev.tagName !== 'H3') prev = prev.previousElementSibling;
      if (prev && prev.tagName === 'H3') prev.style.display = any ? '' : 'none';
    });

    // facet option counts (faceted: count excludes the option's own facet)
    var cache = {};
    function poolFor(attr) { if (!cache[attr]) cache[attr] = rows.filter(function(r) { return passAll(r, attr); }); return cache[attr]; }
    for (var attr in cardEls) {
      var entry = cardEls[attr];
      if (entry.kind !== 'facet') { syncRange(entry); continue; }
      var pool = poolFor(attr), counts = {};
      pool.forEach(function(r) { var v = attrRaw(r, attr); counts[v] = (counts[v] || 0) + 1; });
      var set = state.facets[attr];
      entry.el.querySelectorAll('.flt-opt').forEach(function(b) {
        var v = b.getAttribute('data-val'), n = counts[v] || 0;
        b.querySelector('.flt-n').textContent = n;
        b.classList.toggle('on', !!(set && set.has(v)));
        b.classList.toggle('zero', n === 0 && !(set && set.has(v)));
      });
      entry.el.classList.toggle('narrowed', !!(set && set.size));
    }

    document.querySelectorAll('[data-sec-active]').forEach(function(badge) {
      var sec = SECTIONS.filter(function(s) { return s.id === badge.getAttribute('data-sec-active'); })[0];
      var n = 0;
      sec.cards.forEach(function(c) {
        if (state.facets[c.attr] && state.facets[c.attr].size) n += state.facets[c.attr].size;
        if (state.ranges[c.attr]) n += 1;
      });
      badge.textContent = n ? n : '';
    });

    renderChips();

    // highlight the active "most used" combo (if the current state is one)
    var sig = signature(state);
    usedEl.querySelectorAll('.flt-combo').forEach(function(b) {
      b.classList.toggle('on', b.getAttribute('data-sig') === sig && sig !== '');
    });

    var activeN = 0;
    for (var f in state.facets) activeN += state.facets[f].size;
    activeN += Object.keys(state.ranges).length;
    var searchOn = !!searchInput.value.trim();
    if (statusEl) {
      if (visible === total && !activeN && !searchOn) statusEl.textContent = 'Showing all ' + total;
      else {
        var bits = [];
        if (activeN) bits.push(activeN + ' filter' + (activeN > 1 ? 's' : ''));
        if (searchOn) bits.push('search');
        statusEl.textContent = 'Showing ' + visible + ' of ' + total + (bits.length ? ' (' + bits.join(' + ') + ')' : '');
      }
    }
    toggleBtn.innerHTML = 'Filters ' + (panel.classList.contains('show') ? '▴' : '▾') +
      (activeN ? '<span class="flt-badge">' + activeN + '</span>' : '');
    var hasAny = activeN > 0 || searchOn;
    clearBtn.style.opacity = hasAny ? '1' : '.4';
    clearBtn.style.pointerEvents = hasAny ? 'auto' : 'none';
    syncSaveBtn();
    if (searchWrap) searchWrap.classList.toggle('has-val', searchOn);

    // learn the combos you actually settle on
    if (sig !== lastRecSig) { lastRecSig = sig; scheduleRecord(); }
  }

  function renderChips() {
    var chips = [];
    for (var attr in state.facets) {
      (function(attr) {
        var card = cardFor(attr), lbl = card ? card.label : attr;
        state.facets[attr].forEach(function(v) {
          chips.push({ text: lbl + ': <b>' + (card ? optLabel(card, v) : v) + '</b>',
            remove: function() { state.facets[attr].delete(v); if (!state.facets[attr].size) delete state.facets[attr]; refresh(); } });
        });
      })(attr);
    }
    for (var a in state.ranges) {
      (function(a) {
        var card = cardFor(a), d = domains[a], r = state.ranges[a];
        var loOpen = r.min <= d.min + 1e-9, hiOpen = r.max >= d.max - 1e-9, txt;
        if (loOpen) txt = card.label + ' ≤ <b>' + fmtNum(r.max, card) + '</b>';
        else if (hiOpen) txt = card.label + ' ≥ <b>' + fmtNum(r.min, card) + '</b>';
        else txt = card.label + ': <b>' + fmtNum(r.min, card) + '–' + fmtNum(r.max, card) + '</b>';
        chips.push({ text: txt, remove: function() { delete state.ranges[a]; refresh(); } });
      })(a);
    }
    chipsEl.innerHTML = '';
    chips.forEach(function(c) {
      var el = document.createElement('span');
      el.className = 'flt-chip';
      el.innerHTML = '<span>' + c.text + '</span><span class="flt-chip-x" title="Remove">✕</span>';
      el.querySelector('.flt-chip-x').addEventListener('click', c.remove);
      chipsEl.appendChild(el);
    });
  }

  // ---------- Wire up ----------
  buildPanel();
  buildSeeds();
  renderUsed();

  searchInput.addEventListener('input', refresh);
  document.addEventListener('basescorechange', function() { refresh(); });
  searchX.addEventListener('click', function() { searchInput.value = ''; refresh(); searchInput.focus(); });
  clearBtn.addEventListener('click', function() { searchInput.value = ''; applyState({ facets:{}, ranges:{} }); });

  function setOpen(open) {
    panel.classList.toggle('show', open);
    toggleBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    try { localStorage.setItem(LS.open, open ? '1' : '0'); } catch (e) {}
    refresh();
  }
  if (saveBtn) saveBtn.addEventListener('click', function() { promptSave(signature(state)); });
  toggleBtn.addEventListener('click', function() { setOpen(!panel.classList.contains('show')); });
  try { if (localStorage.getItem(LS.open) === '1') panel.classList.add('show'); } catch (e) {}

  // Public API used by header summary tiles + other sections (unchanged contract).
  window.applyHeaderFilter = function(key) {
    var s = PRESETS.filter(function(x) { return x.key === key; })[0];
    if (!s) return;
    var target = resolveScreen(s);
    var off = signature(state) === signature(target);
    applyState(off ? { facets:{}, ranges:{} } : target);
    if (off) return;
    var wraps = document.querySelectorAll('.table-wrap');
    for (var i = 0; i < wraps.length; i++) {
      if (wraps[i].style.display !== 'none') {
        var prev = wraps[i].previousElementSibling, tgt = wraps[i];
        while (prev) { if (prev.tagName === 'H2' || prev.tagName === 'H3') { tgt = prev; break; } prev = prev.previousElementSibling; }
        tgt.scrollIntoView({ behavior: 'smooth', block: 'start' });
        break;
      }
    }
  };
  window.applySearchFilter = function(term) { searchInput.value = term; refresh(); };

  refresh();
})();

// Smooth-scroll a header summary tile to its section (no-ops if the section
// isn't in this report). Global so the inline onclick handlers can reach it.
window.scrollToSection = function(id) {
  var el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
};
</script>
<script>
// Quick recommendations: hover the chip to reveal the panel; auto-hide when
// the pointer leaves or the page scrolls. Click toggles for touch devices
// (where hover/mouseleave don't fire). Same pattern as the More-filters panel.
(function() {
  var wrap = document.getElementById('qrWrap');
  var trigger = document.getElementById('qrTrigger');
  if (!wrap || !trigger) return;
  function open() { wrap.classList.add('open'); }
  function close() { wrap.classList.remove('open'); }
  trigger.addEventListener('mouseenter', open);
  trigger.addEventListener('click', function() {
    wrap.classList.contains('open') ? close() : open();
  });
  wrap.addEventListener('mouseleave', close);
  window.addEventListener('scroll', close, { passive: true });
})();
</script>
<script>
// Mobile tap-to-reveal tooltips. Native title="" tooltips never appear on a
// tap, so on touch-primary devices (no hover, coarse pointer) tapping any
// element that carries a title — quality dots, score cell, verdict pill,
// insider detail, cost/gain, range/trend — shows that text in a bottom
// sheet. Sortable column headers and buttons are excluded so tap still
// performs their action. Desktop hover is untouched (this only runs when
// hover is unavailable).
(function() {
  var touch = false;
  try {
    touch = window.matchMedia('(hover: none) and (pointer: coarse)').matches;
  } catch (e) {}
  if (!touch) return;

  var backdrop = document.createElement('div');
  backdrop.className = 'm-tip-backdrop';
  var sheet = document.createElement('div');
  sheet.className = 'm-tip';
  sheet.setAttribute('role', 'dialog');
  sheet.innerHTML =
    '<div class="m-tip-label" id="mTipLabel"></div>' +
    '<div class="m-tip-body" id="mTipBody"></div>' +
    '<button type="button" class="m-tip-close" id="mTipClose">Got it</button>';
  document.body.appendChild(backdrop);
  document.body.appendChild(sheet);
  var bodyEl = sheet.querySelector('#mTipBody');
  var labelEl = sheet.querySelector('#mTipLabel');

  function openTip(text, label) {
    bodyEl.textContent = text;
    labelEl.textContent = label || '';
    labelEl.style.display = label ? '' : 'none';
    backdrop.classList.add('show');
    sheet.classList.add('show');
  }
  function closeTip() {
    backdrop.classList.remove('show');
    sheet.classList.remove('show');
  }

  // Elements with their own tap action (or that aren't real tooltips).
  var SKIP = 'th,button,a,summary,label,input,select,' +
             '.refresh-btn,.theme-toggle,.icon-btn';

  // Best-effort: the column header text for a tapped data cell, as a label.
  function columnLabel(cell) {
    if (!cell || cell.tagName !== 'TD') return '';
    var table = cell.closest('table');
    if (!table) return '';
    var idx = Array.prototype.indexOf.call(cell.parentNode.children, cell);
    var ths = table.querySelectorAll('thead th');
    if (idx >= 0 && idx < ths.length) {
      // Trim a trailing info glyph (ⓘ / ℹ) and whitespace.
      return (ths[idx].textContent || '')
        .replace(/[ⓘℹ\s]+$/g, '').trim();
    }
    return '';
  }

  document.addEventListener('click', function(e) {
    if (sheet.contains(e.target) || backdrop.contains(e.target)) return;
    if (e.target.closest(SKIP)) return;
    // [data-tip] carries tooltip text for elements that intentionally have no
    // native title= (e.g. the verdict cell, whose desktop tooltip is the
    // styled hover card).
    var el = e.target.closest('[title],[data-tip]');
    if (!el) return;
    var text = el.getAttribute('title') || el.getAttribute('data-tip');
    if (!text || !text.trim()) return;
    e.preventDefault();
    openTip(text, columnLabel(el.closest('td')));
  }, false);

  backdrop.addEventListener('click', closeTip);
  document.getElementById('mTipClose').addEventListener('click', closeTip);
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeTip();
  });
})();
</script>
<script>
// Verdict hover card placement. The card is position:fixed; we compute its
// top/left from the cell's viewport rect so it opens into whatever space is
// available (left or right, above or below) and is always clamped inside the
// viewport — so it never spills past the right edge (which previously forced a
// horizontal scrollbar) or gets clipped on small screens. Hover-capable
// devices only; touch devices fall back to the short native title= tooltip.
(function() {
  if (!window.matchMedia || !window.matchMedia('(hover: hover)').matches) return;
  var GAP = 8, M = 8;                 // gap from the cell, margin from viewport edge
  var curCell = null, curCard = null;

  function hide() {
    if (curCard) curCard.classList.remove('show');
    curCard = null; curCell = null;
  }

  function place(cell) {
    var card = cell.querySelector('.vcard');
    if (!card) return;
    if (curCard && curCard !== card) curCard.classList.remove('show');
    curCell = cell; curCard = card;
    card.classList.add('show');               // show first so offsetW/H are real
    var c = cell.getBoundingClientRect();
    var w = card.offsetWidth, h = card.offsetHeight;
    var vw = document.documentElement.clientWidth;
    var vh = document.documentElement.clientHeight;
    // Horizontal: left-align to the cell; if that overflows the right edge,
    // right-align to the cell; then clamp into [M, vw - w - M].
    var left = c.left;
    if (left + w > vw - M) left = c.right - w;
    left = Math.max(M, Math.min(left, vw - w - M));
    // Vertical: prefer below; if it overflows the bottom, open above; clamp.
    var top = c.bottom + GAP;
    if (top + h > vh - M) {
      var above = c.top - GAP - h;
      top = above >= M ? above : Math.max(M, vh - h - M);
    }
    card.style.left = Math.round(left) + 'px';
    card.style.top = Math.round(top) + 'px';
  }

  document.addEventListener('mouseover', function(e) {
    var cell = e.target.closest ? e.target.closest('.vcell') : null;
    if (cell && cell !== curCell) place(cell);
  });
  document.addEventListener('mouseout', function(e) {
    if (!curCell) return;
    var cell = e.target.closest ? e.target.closest('.vcell') : null;
    if (cell !== curCell) return;             // not leaving the active cell
    var to = e.relatedTarget;
    if (!to || (!curCell.contains(to) && !curCard.contains(to))) hide();
  });
  // Page scroll detaches a fixed card from its cell — hide it. But ignore
  // scrolling *inside* the card itself (a tall card scrolls internally).
  window.addEventListener('scroll', function(e) {
    if (curCard && (e.target === curCard || curCard.contains(e.target))) return;
    hide();
  }, { capture: true, passive: true });
  window.addEventListener('resize', hide);
})();
</script>
<script>
/* ---------- Missed Opportunities: vcell tooltip + ticker click ---------- */
(function() {
  document.querySelectorAll('td.miss-reason').forEach(function(td) {
    var raw = td.getAttribute('data-reason') || '';
    if (!raw) return;
    var vcell = document.createElement('span');
    vcell.className = 'vcell';
    vcell.setAttribute('data-tip', 'Miss reason');
    var trigger = document.createElement('span');
    trigger.className = 'miss-trigger';
    trigger.textContent = '📋 Why a miss';
    var card = document.createElement('div');
    card.className = 'vcard miss-vcard';
    card.setAttribute('role', 'tooltip');
    card.innerHTML = raw;
    vcell.appendChild(trigger);
    vcell.appendChild(card);
    td.removeAttribute('style');
    td.style.whiteSpace = 'nowrap';
    while (td.firstChild) td.removeChild(td.firstChild);
    td.appendChild(vcell);
  });
  var _missedWrap = (function() {
    var h = document.getElementById('missed-opps');
    if (!h) return null;
    var n = h.nextElementSibling;
    while (n && !n.classList.contains('table-wrap')) n = n.nextElementSibling;
    return n;
  })();

  document.querySelectorAll('.miss-ticker-link').forEach(function(el) {
    el.addEventListener('click', function(e) {
      e.preventDefault();
      var ticker = el.getAttribute('data-ticker') || '';
      if (!ticker) return;
      if (typeof window.applySearchFilter === 'function') {
        window.applySearchFilter(ticker);
      } else {
        var input = document.getElementById('searchInput');
        if (input) { input.value = ticker; input.dispatchEvent(new Event('input', {bubbles:true})); }
      }
      var mainVisible = Array.from(document.querySelectorAll('tbody tr[data-search]')).some(function(r) {
        return r.style.display !== 'none' && (!_missedWrap || !_missedWrap.contains(r));
      });
      if (mainVisible) {
        window.scrollTo({ top: 0, behavior: 'smooth' });
      } else {
        var h2 = document.getElementById('missed-opps');
        if (h2) h2.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
  });
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
    ap.add_argument("--tax", action="store_true",
                    help="Opt in to the Tax-Aware Trim Guidance section. Off by "
                         "default — it's the only step that fetches your full "
                         "order history, so skipping it keeps runs fast.")
    ap.add_argument("--analyze-misses", action="store_true",
                    help="On-demand AI post-mortem of the Missed Opportunities: "
                         "sends the computed miss data (alpha, horizons, factors, "
                         "why-missed) to Claude for a written analysis of the "
                         "systematic gaps, and saves it to "
                         "missed_opp_analysis_<date>.md. Off by default and never "
                         "part of the normal report (needs ANTHROPIC_API_KEY; model "
                         "via MISSED_OPP_MODEL).")
    ap.add_argument("--base-score", choices=BASE_SCORE_MODES, default=None,
                    help="The base score this run acts on for watchlist "
                         "pruning, tax flags and the history ledger: composite "
                         "(the Composite Score), quality (the nine quality "
                         "filters 70%% + analyst 15%% + insider 15%%) or blend "
                         "(the average of the two). Overrides the "
                         "BASE_SCORE_MODE env var; default composite. The "
                         "report carries all three either way.")
    ap.add_argument("--lots-csv", default=None,
                    help="Optional purchase-history CSV (columns: ticker,date,"
                         "shares,price) for exact lot-level tax analysis in CSV "
                         "mode. Each row is one buy lot; multiple rows per ticker "
                         "are split into long/short-term automatically. Without "
                         "this, CSV mode falls back to a position-level estimate.")
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
    ap.add_argument("--no-pin-recent-holdings", dest="pin_recent_holdings",
                    action="store_false",
                    help="Don't keep recently-sold positions in the analyzed "
                         f"universe (default: keep for "
                         f"{PIN_RECENT_HOLDINGS_DAYS} days, so the ledger "
                         f"keeps marking them to market after you exit).")
    ap.add_argument("--prune-watchlists", action="store_true",
                    help="With --include-watchlists: remove tickers whose verdict "
                         "score is below --prune-threshold from their Robinhood "
                         "watchlist (read-write). Tickers you hold and tickers "
                         "whose analysis failed are never removed.")
    ap.add_argument("--prune-threshold", type=float, default=60.0,
                    help="Verdict-score cutoff for --prune-watchlists "
                         "(default 60 — keeps BUY and WATCH, removes WAIT/PASS).")
    ap.add_argument("--prune-dry-run", action="store_true",
                    help="With --prune-watchlists, print what would be removed "
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
    set_base_score_mode(args.base_score)

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
    realized_ytd = None   # populated only when --tax is set
    account_summary = None  # cash/margin snapshot; only the robinhood source has it

    # Optional lot-level purchase history (CSV mode). Builds the same
    # ticker -> [{date, shares, price, cost}] structure that the Robinhood
    # order-history reconstruction produces, so the exact lot-level tax
    # rendering (LT/ST split, per-lot days-to-long-term) lights up.
    if args.lots_csv:
        import csv as _lots_csv
        with open(args.lots_csv, newline="") as _lf:
            for _row in _lots_csv.DictReader(_lf):
                _tk = (_row.get("ticker") or "").strip().upper()
                if not _tk:
                    continue
                try:
                    _sh = float(_row["shares"])
                    _pr = float(_row["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                tax_lots_lookup.setdefault(_tk, []).append({
                    "date": (_row.get("date") or "").strip()[:10],
                    "shares": _sh,
                    "price": _pr,
                    "cost": round(_sh * _pr, 2),
                })
        print(f"[tax] Loaded purchase history for {len(tax_lots_lookup)} "
              f"ticker(s) from {args.lots_csv}")
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
        # Account-level cash/margin snapshot — lets the header show true net
        # portfolio value instead of the gross sum of positions (which
        # overstates value when the account carries a margin loan).
        try:
            account_summary = rhs.fetch_account_summary()
            if account_summary and account_summary.get("margin_used", 0) > 0:
                print(f"[robinhood] Margin loan outstanding: "
                      f"${account_summary['margin_used']:,.2f}")
        except Exception as e:
            print(f"[robinhood] Account summary fetch skipped: {e}")
            account_summary = None
        use_rh_ratings = True
        if args.include_watchlists:
            print("[robinhood] Fetching watchlists...")
            watchlist_lookup = rhs.fetch_watchlists()
        # Tax analysis is opt-in (--tax): it's the only thing that needs the
        # full order history, so skipping it keeps normal runs fast.
        if args.tax:
            print("[robinhood] Reconstructing tax lots from order history...")
            tax_lots_lookup = rhs.fetch_tax_lots(verbose=True)
            # Same order history computes YTD realized gains for the tax section
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

        # After-hours prices: when the regular session is closed, override the
        # report's live prices with Robinhood's broker-accurate extended-hours
        # last trade (holdings + watchlist). Skipped while the market is open,
        # or when RH_EXTENDED_HOURS=0. analyze_position() reads _RH_EXTENDED_PRICES.
        if (os.environ.get("RH_EXTENDED_HOURS", "1") != "0"
                and not _us_market_open_now()):
            try:
                _wl_ticks = [it["ticker"]
                             for items in (watchlist_lookup or {}).values()
                             for it in items]
                _all_ticks = list({r["ticker"] for r in rows} | set(_wl_ticks))
                _px = rhs.fetch_latest_prices(_all_ticks)
                if _px:
                    _RH_EXTENDED_PRICES.update(_px)
                    print(f"[robinhood] Market closed — using extended-hours "
                          f"prices for {len(_px)}/{len(_all_ticks)} tickers.")
            except Exception as e:
                print(f"[robinhood] Extended-hours price fetch skipped: {e}")
    else:
        if not args.positions_csv:
            print("ERROR: provide a positions CSV (or use --source robinhood).",
                  file=sys.stderr)
            sys.exit(1)
        with open(args.positions_csv) as f:
            rows = list(csv.DictReader(f))

    _mode, _saved_mode = base_score_mode(), base_score_mode(saved_only=True)
    print(f"Verdict base score: {BASE_SCORE_LABELS[_mode]}"
          + ("" if _mode == _saved_mode else
             f" (this run only; saved default {BASE_SCORE_LABELS[_saved_mode]})"))

    # The ledger is read BEFORE any analysis now, because two things depend on
    # it up front: the recalibrated verdict's hysteresis needs each name's
    # label from last run, and the universe needs the names we held recently
    # (finding 5). It is re-read below for the missed-opportunity tables, which
    # must see this run's own results.
    try:
        _prior_history = load_recs_history()
    except Exception as e:
        print(f"[history] Could not read the ledger up front: {e}")
        _prior_history = {}
    _prior_n = load_prior_verdict_labels(_prior_history)
    if _prior_n:
        print(f"[verdict] Carrying last run's label for {_prior_n} ticker(s) "
              f"(recalibrated scoring only).")

    print(f"Analyzing {len(rows)} positions...")
    results: list[PositionAnalysis] = analyze_positions_parallel(
        rows, use_robinhood_ratings=use_rh_ratings)

    # Analyze watchlists. Dedupe by ticker (a stock in multiple lists is
    # analyzed once), then fan the cached results back out per list.
    watchlists_analyzed: dict[str, list[PositionAnalysis]] = {}
    # Finding 5: names held within the last PIN_RECENT_HOLDINGS_DAYS that are on
    # no watchlist any more still get analyzed, under their own group, so
    # selling a position does not silently end the analyzer's coverage of it.
    if args.pin_recent_holdings:
        try:
            _held_now = {r["ticker"] for r in rows}
            _on_a_list = {it["ticker"]
                          for items in (watchlist_lookup or {}).values()
                          for it in items}
            _pin = {t: n for t, n in recently_held_tickers(_prior_history).items()
                    if t not in _held_now and t not in _on_a_list}
            if _pin:
                watchlist_lookup = dict(watchlist_lookup or {})
                watchlist_lookup[RECENTLY_HELD_GROUP] = [
                    {"ticker": t, "name": n} for t, n in sorted(_pin.items())]
                print(f"[universe] Keeping {len(_pin)} recently-held name(s) in "
                      f"the run: {', '.join(sorted(_pin))}")
        except Exception as e:
            print(f"[universe] Could not pin recently-held names: {e}")
    if watchlist_lookup:
        held_set = {r.ticker for r in results}
        unique_rows: dict[str, dict] = {}
        for items in watchlist_lookup.values():
            for it in items:
                t = it["ticker"]
                if t not in held_set and t not in unique_rows:
                    unique_rows[t] = {
                        "ticker": t, "name": it["name"],
                        "shares": 0, "market_value": 0, "pct_portfolio": 0,
                    }
        print(f"\nAnalyzing {len(unique_rows)} unique watchlist tickers...")
        analyzed = analyze_positions_parallel(
            list(unique_rows.values()),
            use_robinhood_ratings=use_rh_ratings,
            is_watchlist=True,
        )
        ticker_cache = {pa.ticker: pa for pa in analyzed}
        for wl_name, items in watchlist_lookup.items():
            analyzed_items = [ticker_cache[it["ticker"]] for it in items
                              if it["ticker"] in ticker_cache]
            if analyzed_items:
                watchlists_analyzed[wl_name] = analyzed_items

    # Prune weak watchlist tickers (verdict score below threshold) from the
    # actual Robinhood watchlists. Removal is verified by re-reading, and
    # errored analyses are never pruned (see select_watchlist_prune_candidates).
    if args.prune_watchlists and watchlists_analyzed and args.source == "robinhood":
        try:
            import robinhood_source as rhs
            candidates = select_watchlist_prune_candidates(
                watchlists_analyzed, threshold=args.prune_threshold)
            if not candidates:
                print(f"\n[prune] No watchlist tickers below verdict score "
                      f"{args.prune_threshold:g} — nothing to remove.")
            for wl_name, ticks in candidates.items():
                scores = {pa.ticker: pa.verdict.score
                          for pa in watchlists_analyzed[wl_name]
                          if pa.ticker in ticks}
                detail = ", ".join(f"{t} ({scores[t]:.0f})" for t in ticks)
                print(f"\n[prune] '{wl_name}': below {args.prune_threshold:g} "
                      f"→ {detail}")
                rhs.prune_watchlist(wl_name, ticks,
                                    dry_run=args.prune_dry_run, verbose=True)
        except Exception as e:
            print(f"[prune] Skipped watchlist pruning: {e}")

    # Finalize verdicts with portfolio context BEFORE selecting tax candidates.
    # analyze_position() can't see position size, so the size overlay applied
    # here can flip HOLD → TRIM (overweight positions). Running it only inside
    # generate_html_report() meant such positions showed TRIM in the report but
    # were never flagged for tax analysis — missing from the tax section.
    finalize_holding_verdicts(results)

    # What the other base-score modes would have decided (and pruned) — the
    # numbers to check before switching BASE_SCORE_MODE.
    try:
        for line in compare_base_modes(
                results, watchlists_analyzed or None,
                prune_threshold=(args.prune_threshold
                                 if args.prune_watchlists else None)):
            print(line)
    except Exception as e:
        print(f"[base-score] Skipped the mode comparison: {e}")

    # Missed-opportunity tracking. Refresh the git-tracked ledger with this run's
    # buy-type verdicts (first sighting) and latest prices, then derive the set of
    # recommendations we under-acted on while the stock ran up. Best-effort: any
    # failure here must not block the report.
    missed_opportunities: list[dict] = []
    avoided_losses: list[dict] = []
    missed_insights: dict = {}
    recs_tracked_count = 0
    try:
        # Re-read rather than reuse `_prior_history`: nothing has written to it,
        # but this keeps the tables reading exactly what is on disk.
        recs_history = load_recs_history()
        # Rank each ticker under the report's default sort, then diff against the
        # ledger's prior-DAY ranks (attaches r._rank_move for the ▲/▼ badges)
        # BEFORE update_recs_history rolls the daily baseline forward. Both use
        # the same run_date so the same-day/new-day split stays consistent.
        _run_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        run_ranks = compute_run_ranks_by_mode(results, watchlists_analyzed or None)
        _attach_rank_moves(recs_history, run_ranks, results,
                           watchlists_analyzed or None, run_date=_run_date)
        # S&P level at this run — frozen per ticker at first sight, refreshed
        # after, so each miss can be scored on excess return (alpha), not raw
        # gain. Cheap: fetch_benchmark_returns is cached (30-min TTL).
        _sp_level = (fetch_benchmark_returns() or {}).get("level")
        update_recs_history(recs_history, results, watchlists_analyzed or None,
                            run_date=_run_date, ranks_by_mode=run_ranks,
                            sp_level=_sp_level)
        save_recs_history(recs_history)
        recs_tracked_count = len(recs_history.get("tickers", {}))
        missed_opportunities = compute_missed_opportunities(recs_history)
        avoided_losses = compute_avoided_losses(recs_history)
        missed_insights = compute_missed_opp_insights(
            recs_history, missed_opportunities, avoided_losses)
        print(f"[history] Tracking {recs_tracked_count} "
              f"stock(s); {len(missed_opportunities)} missed "
              f"opportunit{'y' if len(missed_opportunities) == 1 else 'ies'}, "
              f"{len(avoided_losses)} avoided loss(es).")
    except Exception as e:
        print(f"[history] Skipped missed-opportunity tracking: {e}")

    # On-demand AI post-mortem of the misses (opt-in via --analyze-misses; never
    # part of the normal report). Writes a dated Markdown file and echoes it to
    # stdout. Best-effort: any failure here must not block the report.
    _miss_analysis_md = None   # embedded into the report below when produced
    if args.analyze_misses:
        try:
            _miss_md = analyze_missed_opportunities_ai(
                missed_opportunities, avoided_losses, missed_insights)
            if _miss_md:
                _miss_analysis_md = _miss_md
                _adate = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
                _apath = f"missed_opp_analysis_{_adate}.md"
                try:
                    with open(_apath, "w") as _af:
                        _af.write(f"# Missed-Opportunity Post-Mortem — {_adate}\n\n")
                        _af.write(f"_Model: {MISSED_OPP_MODEL}. Retrospective "
                                  f"model-improvement analysis, not investment "
                                  f"advice._\n\n")
                        _af.write(_miss_md + "\n")
                    print(f"[miss-analysis] Wrote {_apath}")
                except OSError as _ae:
                    print(f"[miss-analysis] Could not write {_apath}: {_ae}")
                print("\n" + "=" * 72 + "\n" + _miss_md + "\n" + "=" * 72)
            elif missed_opportunities:
                # Toggle was on but no analysis came back (no/blocked key, model
                # access, or a refusal). Say so in-report rather than silently
                # dropping the panel the reader asked for.
                _miss_analysis_md = (
                    "**AI analysis unavailable for this run.** The post-mortem "
                    "couldn't be generated — most often a missing or mismatched "
                    "ANTHROPIC_API_KEY, or a model the key can't access (set the "
                    "MISSED_OPP_MODEL repo variable to one it can). See the Actions "
                    "log for the exact reason.")
        except Exception as e:
            print(f"[miss-analysis] Skipped: {e}")

    # Tax analysis for SELL/TRIM and low-score positions (holding period +
    # trim timing).
    try:
        from tax_analysis import (TaxConfig, analyze_tax, analyze_tax_with_lots,
                                  reconcile_lots_with_position)
        tax_cfg = TaxConfig.from_env()
        # Tax analysis is opt-in (--tax). When off, leave `flagged` empty so no
        # r.tax is populated and the tax section is omitted from the report.
        # Flag SELL/TRIM verdicts plus any position whose verdict score is
        # below 75 — weak-scoring holds are trim candidates too. A position
        # any base-score mode flags gets tax detail, so the report's base
        # switch can show each mode's own set without a re-run.
        flagged = ([r for r in results
                    if any(tax_flagged(verdict_in(r, m)) for m in BASE_SCORE_MODES)]
                   if args.tax else [])
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
                    # Reconcile reconstructed lots against the live share
                    # count — unrecorded disposals (partial-fill cancels,
                    # option assignments, transfers) otherwise leave phantom
                    # lots that misreport the long/short-term split.
                    recon_note = None
                    if lots and r.shares:
                        lots, recon_note = reconcile_lots_with_position(
                            lots, r.shares, ticker=r.ticker)
                        if recon_note:
                            print(f"[tax] {r.ticker}: {recon_note}")
                    if lots and r.current_price:
                        r.tax = analyze_tax_with_lots(
                            ticker=r.ticker,
                            verdict=r.verdict.label,
                            lots=lots,
                            current_price=r.current_price,
                            cfg=tax_cfg,
                        )
                        if recon_note:
                            r.tax.timing_note = (
                                f"⚠ {recon_note} {r.tax.timing_note}".strip())
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
        realized_ytd=realized_ytd,   # None unless --tax populated it
        missed_opportunities=missed_opportunities,
        recs_tracked_count=recs_tracked_count,
        avoided_losses=avoided_losses,
        missed_insights=missed_insights,
        missed_analysis_md=_miss_analysis_md,
        account_summary=account_summary,
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