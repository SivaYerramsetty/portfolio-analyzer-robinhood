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
import io
import json
import os
import smtplib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
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
# most capable model; set NEWS_MODEL=claude-haiku-4-5 to cut cost/latency ~5x.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
NEWS_MODEL = os.environ.get("NEWS_MODEL", "claude-opus-4-8").strip() or "claude-opus-4-8"


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
    """Two standalone summary tiles — 'vs S&P 500 · Today' and 'vs S&P 500 ·
    YTD' — matching the single-value layout of the other stats in the row. Each
    shows the portfolio's return as the big figure (green when it beats the
    index for that horizon, red when it lags) with the S&P's own return in the
    label. A tile appears only when both sides have data; nothing renders if the
    benchmark is unavailable or no horizon can be shown."""
    if not bench:
        return ""
    tip = ("Your current holdings' price return vs the S&P 500 (^GSPC). "
           "Today = vs prior close; YTD = vs the first close of the year. "
           "Green = beating the index. YTD is value-weighted over holdings "
           "with available history (ignores intra-year trades).")
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
            f'vs S&amp;P 500 · {label} ({_fmt_pct(spx, 2, True)})</div>'
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


def _render_portfolio_health_gauge(results: list) -> str:
    """
    Value-weighted average of per-position verdict scores (0–100).
    Pairs 'how the market feels' with 'how strong your book is'.
    """
    scored = [r for r in results
              if r.verdict and r.verdict.score is not None
              and r.live_market_value is not None]
    if not scored:
        return ""
    total_val = sum(r.live_market_value for r in scored)
    if total_val <= 0:
        return ""
    health = sum(r.verdict.score * r.live_market_value for r in scored) / total_val
    rating = (
        "Weak" if health < 25 else
        "Soft" if health < 45 else
        "Mixed" if health <= 55 else
        "Solid" if health <= 75 else
        "Strong"
    )
    strong = sum(1 for r in scored if r.verdict.score >= 55)
    weak = sum(1 for r in scored if r.verdict.score < 45)
    sub_html = (
        '<div class="fg-prev">'
        f'<span class="fg-prev-item">Strong <strong>{strong}</strong></span>'
        f'<span class="fg-prev-item">Weak <strong>{weak}</strong></span>'
        f'<span class="fg-prev-item">Rated <strong>{len(scored)}</strong></span>'
        '</div>'
    )
    return _render_gauge_card(
        dom_id="healthMeter", score=health, rating=rating,
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
# scored into one bullish/bearish number, cached on disk (default 6h TTL), then
# mapped to a small bounded modifier in compute_verdict_v2 (see
# _news_signal_modifier). Scoring is FREE by default via a headline lexicon; if
# ANTHROPIC_API_KEY is set it upgrades to Claude for sharper reads. The cache
# lives in .cache/ (gitignored; carried across CI runs) so warm runs re-fetch
# nothing. Set NEWS_SIGNAL=0 to disable entirely.

_NEWS_CACHE_PATH = Path(__file__).resolve().parent / ".cache" / "news_sentiment.json"
_news_cache_lock = threading.Lock()
_news_cache: Optional[dict] = None
_news_cache_dirty = False
_news_hits = 0
_news_misses = 0
_anthropic_client_singleton = None

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


def _news_cache_ttl_hours() -> float:
    try:
        return float(os.environ.get("NEWS_CACHE_TTL_HOURS", "6"))
    except ValueError:
        return 6.0


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


def _call_claude_sentiment(client, ticker, name, headlines) -> Optional[dict]:
    label = f"{name} ({ticker})" if name else ticker
    bullets = "\n".join(f"- {h}" for h in headlines)
    system = (
        "You are an equity news analyst. Read a stock's recent headlines and "
        "judge the net sentiment for its forward price over the next few weeks. "
        "Weigh material catalysts (earnings, guidance, M&A, regulatory, "
        "litigation, products) far more than routine coverage; ignore generic "
        "market commentary; be skeptical of hype."
    )
    prompt = (
        f"Stock: {label}\n\nRecent headlines (newest first):\n{bullets}\n\n"
        "Return JSON with: score (number from -1.0 very bearish to +1.0 very "
        "bullish, 0 if neutral/no clear signal), label (bullish | neutral | "
        "bearish), and rationale (one concise sentence, max ~20 words, citing "
        "the key driver)."
    )
    try:
        resp = client.with_options(timeout=20.0).messages.create(
            model=NEWS_MODEL,
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema",
                                       "schema": _NEWS_SENTIMENT_SCHEMA}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(text)
        score = max(-1.0, min(1.0, float(data.get("score", 0) or 0)))
        return {
            "score": score,
            "label": str(data.get("label", "neutral")),
            "rationale": str(data.get("rationale", "")).strip(),
            "headlines": headlines[:5],
            "as_of": datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
        }
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
        "as_of": datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
        "method": "lexicon",
    }


def score_news_sentiment(ticker: str, name: Optional[str] = None) -> Optional[dict]:
    """Score a ticker's recent news, cached on disk (TTL). Uses Claude when
    ANTHROPIC_API_KEY is set, otherwise a FREE headline lexicon — both return
    {score, label, rationale, headlines, as_of}. Returns None when disabled
    (NEWS_SIGNAL=0) or there's no news. Never raises; disk write is batched by
    _flush_news_cache()."""
    global _news_cache_dirty, _news_hits, _news_misses
    if os.environ.get("NEWS_SIGNAL", "1") == "0":
        return None
    ttl = _news_cache_ttl_hours()
    if ttl > 0:
        with _news_cache_lock:
            entry = _load_news_cache().get(ticker)
        if entry and (time.time() - entry.get("ts", 0)) < ttl * 3600:
            with _news_cache_lock:
                _news_hits += 1
            return entry.get("sentiment")

    headlines = fetch_recent_headlines(ticker)
    result = None
    if headlines:
        client = _anthropic_client()       # paid, higher quality — only if keyed
        if client is not None:
            result = _call_claude_sentiment(client, ticker, name, headlines)
        if result is None:                 # no key, or LLM failed -> free lexicon
            result = _lexicon_sentiment(headlines)

    if ttl > 0:
        with _news_cache_lock:
            _load_news_cache()[ticker] = {"ts": time.time(), "sentiment": result}
            _news_cache_dirty = True
            _news_misses += 1
    return result


def _news_signal_modifier(news: Optional[dict]):
    """Map a news-sentiment dict to (delta, description) for the verdict, or None
    for a neutral/absent signal. Bounded to ±6 so news nudges but never dominates
    fundamentals (same magnitude band as the insider/sector signals)."""
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
    rationale = (news.get("rationale") or "").strip()
    lbl = news.get("label") or ("positive" if delta > 0 else "negative")
    desc = f"Recent news {lbl}" + (f": {rationale}" if rationale else "")
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
    results.append(FilterResult(
        name="P/E < 30",
        passed=(pe is not None and pe < 30),
        actual=pe,
        threshold="< 30",
        display=pe_display,
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

# A holding/watchlist name is flagged "earnings soon" (header stat + filter)
# when its next report is within this many calendar days. Kept in sync with the
# hardcoded threshold in the rowMatchesFilter() 'earnings-soon' case (plain-JS
# string, can't interpolate Python there).
EARNINGS_SOON_DAYS = 7

# Verdict score below which a position is included in the tax analysis
# section (in addition to explicit SELL/TRIM verdicts).
TAX_FLAG_SCORE_THRESHOLD = 75


@dataclass
class Verdict:
    label: str       # SELL, TRIM, HOLD, ADD, BUY MORE
    color: str       # CSS color for HTML
    reason: str
    score: Optional[float] = None    # 0-100 numerical verdict score (v2 only)
    # Data-coverage confidence (v2 only): `coverage` is the fraction of the
    # Composite Score's weight that actually had data (0-1); `confidence` is the
    # bucketed label (High/Medium/Low) shown on the verdict card.
    coverage: Optional[float] = None
    confidence: Optional[str] = None


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
    v_components = []
    if pe is not None and pe > 0:
        # P/E 10 -> 100, P/E 30 -> 0, scaled
        v_components.append(_clip01((30 - pe) / 20))
    if peg is not None and peg > 0:
        v_components.append(_clip01((2 - peg) / 2))
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
        # weight_total is the share of the (normalized-to-1.0) weighting that
        # actually had data — i.e. how complete the fundamental picture is.
        # Drives the verdict's confidence label / modifier dampening.
        pa.composite_coverage = round(weight_total, 3)


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
    news_signal: Optional[dict] = None,
    coverage: Optional[float] = None,
) -> Verdict:
    """
    Evidence-weighted verdict logic.

    Synthesizes ALL available signals into a single "verdict score" (0-100).
    The Composite Score is the fundamentals base (quality, growth, valuation
    *level*, analyst quality, insider activity). On top of it we apply small
    modifiers ONLY for signals the composite does NOT already contain:
    trend, sector momentum, 52-week position, upside-to-analyst-target, recent
    news, and portfolio concentration.

    No double-counting: quality and insider already live in the composite (as
    rich continuous sub-scores), so they are NOT re-applied as modifiers here.
    Upside-to-target is the opposite case — it is deliberately kept OUT of the
    composite and applied here instead, where it can interact with trend. Each
    factor therefore influences the final score exactly once.

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

    # ---- Quality filter pass/fail count (NOT re-scored here) ----
    # Quality already drives the Composite Score base (score_quality is 30% of
    # it, built from the same ROE / margin / leverage / FCF inputs as these
    # gates), so re-penalizing a quality miss here would double-count it. We
    # still tally passed/failed because the 52-week-position logic below uses
    # the count to distinguish a value entry from a falling knife.
    passed = None
    failed = 0
    if filters:
        passed = sum(1 for f in filters if f.passed)
        failed = len(filters) - passed

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

    # ---- Insider activity (NOT re-scored here) ----
    # Insider buying/selling already feeds the Composite Score (score_insider is
    # 15% of it, as a rich continuous 0-100 built from buy/sell dollar volume).
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

    # ---- Latest-news sentiment (bounded ±6 nudge) ----
    _news_mod = _news_signal_modifier(news_signal)
    if _news_mod:
        _nd, _ndesc = _news_mod
        score += _nd
        contributors.append((_ndesc, _nd))

    # ---- Confidence dampening (thin data coverage) ----
    # When the Composite Score was built from only a few sub-scores, the context
    # modifiers above can swing a poorly-supported base too far (e.g. push a
    # 2-input composite to a strong BUY on momentum alone). Shrink the *net
    # modifier* toward neutral in proportion to how complete the data is. The
    # composite base itself is left untouched — only our confidence in the
    # context tilt drops. `coverage` is the fraction of composite weight that
    # had data (0-1); >=0.85 (essentially every sub-score) keeps full strength.
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
    if is_holding:
        # Holdings: stay-the-course bias. HOLD covers a wide middle band.
        if score >= 78:
            label, color = "ADD", "#27ae60"        # strong conviction add
        elif score >= 60:
            label, color = "HOLD", "#2c3e50"       # stay the course
        elif score >= 50:
            label, color = "HOLD", "#7f8c8d"       # weak HOLD (muted gray)
        elif score >= 28:
            label, color = "TRIM", "#e67e22"       # below neutral (~50) — lean out
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

    return Verdict(label=label, color=color, reason=reason, score=round(score, 1),
                   coverage=coverage, confidence=confidence)


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
            # Latest-news sentiment: Claude when ANTHROPIC_API_KEY is set, else
            # a free headline lexicon (no key/cost). Cached on disk so warm runs
            # re-fetch nothing. Disable entirely with NEWS_SIGNAL=0.
            pa.news_sentiment = score_news_sentiment(ticker, pa.name)
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
                news_signal=pa.news_sentiment,
                coverage=pa.composite_coverage,
            )

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
) -> list[PositionAnalysis]:
    """Run analyze_position over rows concurrently, preserving input order.

    Progress is reported per completed ticker (to stdout, and to log_fn when
    given — used by server.py to stream into the dashboard log panel). A
    ticker whose analysis errored is retried once after a short pause, which
    absorbs transient Yahoo rate-limit hiccups.
    """
    if max_workers is None:
        try:
            max_workers = int(os.environ.get("ANALYZE_MAX_WORKERS", "6"))
        except ValueError:
            max_workers = 6
    max_workers = max(1, min(max_workers, len(rows) or 1))

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
    _flush_news_cache()
    if _news_hits or _news_misses:
        _mode = "Claude" if ANTHROPIC_API_KEY else "lexicon (free)"
        print(f"[news-cache] {_news_hits} hit / {_news_misses} miss "
              f"({_news_misses} scored via {_mode} this batch)")

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


def _build_refresh_widget() -> tuple[str, str]:
    """Button + JS that triggers the Actions workflow_dispatch from the report.

    The GitHub API allows CORS from any origin, so the static Pages report
    can call it directly — no server needed. Auth uses a fine-grained PAT
    (Actions: read/write on this one repo) that the user pastes once per
    browser; it lives only in localStorage, never in the published HTML.

    Returns (button_html, status_and_script_html) so the button can sit in
    the header controls cluster while the status line + script live below.
    Both are "" when the repo can't be resolved.
    """
    repo = _gh_repo_slug()
    if not repo:
        return "", ""
    # Tax toggle sits before the refresh button. Its state lives in
    # localStorage; when on, the dispatch below sends include_tax=true so the
    # regenerated report contains the Tax-Aware Trim Guidance section.
    button = ('<button id="taxSectionToggle" class="refresh-btn tax-toggle" '
              'aria-pressed="false" '
              'title="Include the Tax-Aware Trim Guidance section in the '
              'next data refresh">'
              '&#129534; Tax</button>'
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
  var pollTimer = null, startedAt = null;

  function taxEnabled() {
    try { return localStorage.getItem(TAX_KEY) === "1"; }
    catch (e) { return false; }
  }
  // Tax section toggle: persisted per-browser; honored by every dispatch
  // (manual button AND the auto-refresh timer, which calls ghTriggerRefresh).
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
        "  - Permissions: Actions = Read and write\\n\\n" +
        "It is stored only in this browser (localStorage), never on any server.");
      if (tok) localStorage.setItem(TOKEN_KEY, tok.trim());
    }
    return tok ? tok.trim() : null;
  }
  // Hooks for the auto-refresh toggle: check for a stored token
  // without prompting, and ensure one exists (prompting once) at enable time.
  window.ghHasToken = function() { return !!localStorage.getItem(TOKEN_KEY); };
  window.ghEnsureToken = function() { return getToken(false); };
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
      if (taxEnabled()) payload.inputs = {include_tax: "true"};
      var r = await fetch(API + "/actions/workflows/" + WORKFLOW + "/dispatches", {
        method: "POST", headers: headers(tok),
        body: JSON.stringify(payload)
      });
      if (r.status === 204) {
        startedAt = Date.now();
        setStatus("Workflow queued" +
                  (taxEnabled() ? " with tax section" : "") +
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
    return button, widget.replace("__REPO__", repo)


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


def _verdict_cell(verdict, days_to_earnings: Optional[int] = None) -> str:
    """Render the verdict pill + 0-100 score with a styled hover-card breakdown.

    Layout: a colored verdict pill (label) and the numeric score sit side by
    side. Hovering the cell reveals a styled card that breaks the score down
    factor by factor — the Composite Score base, then each +/- modifier (trend,
    sector, upside, news, ...) — topped with a 0-100 strength bar. The card also
    surfaces data-coverage confidence and, when known, the next-earnings date.
    At-a-glance markers sit beside the score: an amber dot for thin-data (Low)
    confidence and a calendar glyph when earnings are within a week.

    v2 verdict reasons are structured as
        headline | Composite Score N | +X · factor | -Y · factor | = verdict score N
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
    # parts[1] = Composite Score base; middle = factor lines; last = "= verdict score N"
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

    card = (
        f"<div class='vcard' role='tooltip'>"
        f"<div class='vcard-head'>"
        f"<span class='vcard-headline'>{_esc(headline)}</span>"
        f"<span class='vcard-score' style='color:{sc};'>{score:.0f}</span></div>"
        f"{bar}{conf_html}{base_html}"
        f"<div class='vrows'>{''.join(factor_rows)}</div>"
        f"{earn_html}"
        f"</div>"
    )
    # No native title= (it duplicated, and lagged behind, the styled card on
    # desktop). The full breakdown still reaches touch devices via data-tip,
    # which the mobile tap-to-reveal sheet reads.
    mobile_tip = _esc(reason.replace(" | ", "\n"))
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


def _tr_open(r) -> str:
    """Open a <tr> with data attributes used by the filter bar."""
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
    # absent in lookup mode). data-rank-move: up/down/new/''; data-rank-delta is
    # the signed places gained (+climbed / -slipped) for magnitude filters.
    rank_move = ""
    rank_delta = ""
    _rm = getattr(r, "_rank_move", None)
    if _rm:
        if _rm.get("new"):
            rank_move = "new"
        else:
            _d = _rm.get("delta") or 0
            if _d > 0:
                rank_move, rank_delta = "up", _d
            elif _d < 0:
                rank_move, rank_delta = "down", _d
    # Whether tax analysis is populated (for "show tax-relevant" filter)
    has_tax = "1" if getattr(r, "tax", None) is not None else "0"
    # Days until next earnings (forward-only) — drives the 'earnings-soon' filter
    earnings_days = "" if getattr(r, "days_to_earnings", None) is None else r.days_to_earnings
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
        f"data-trend='{trend}' data-ma-pct='{ma_pct}' "
        f"data-port-pct='{port_pct}' "
        f"data-days-held='{days_held}' "
        f"data-recommendation='{recommendation}' "
        f"data-has-tax='{has_tax}' "
        f"data-rank-move='{rank_move}' data-rank-delta='{rank_delta}' "
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
    verdict score within the ticker's table). Unchanged rows render nothing, to
    keep tables clean. Movement is set on r._rank_move by _attach_rank_moves."""
    move = getattr(r, "_rank_move", None)
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
    """Render filter pass/fail as colored dots with hover tooltip."""
    parts = []
    for f in filters:
        color = "#27ae60" if f.passed else "#c0392b"
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
        parts.append(
            f'<span title="{title}" class="qdot" '
            f'style="background:{color};"></span>'
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

    # ---------- Per-position trim guidance (existing behavior) ----------
    if flagged:
        html += "<h3 style='margin-top:32px;'>Per-Position Trim Detail</h3>\n"

    for r in flagged:
        ta = r.tax
        if ta is None:
            continue  # tax analysis failed for this one — don't render a broken card
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
                news_signal=r.news_sentiment,   # reuse the score from analyze_position
                coverage=r.composite_coverage,
            )
    return live_total


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


def _rec_diagnostic(r: PositionAnalysis) -> dict:
    """Compact 'why not a buy' diagnostic stored alongside each ledger snapshot:
    the verdict's drag factors and the latest-news catalyst (when news scoring
    is enabled). Lets the Missed Opportunities table explain why the analyzer
    didn't flag a BUY/ADD and what news drove the run-up — without re-deriving
    anything at render time."""
    news = None
    ns = getattr(r, "news_sentiment", None)
    if ns and (ns.get("rationale") or ns.get("label") or ns.get("headlines")):
        news = {
            "label": (ns.get("label") or "").strip(),
            "rationale": (ns.get("rationale") or "").strip(),
            "headline": _pick_catalyst_headline(r.ticker, r.name, ns.get("headlines")),
        }
    return {"why": _verdict_why_not_buy(r.verdict), "news": news}


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

def _holding_rank_key(r):
    """Default holdings sort: verdict score desc, market value as tiebreak."""
    score = (r.verdict.score if r.verdict and r.verdict.score is not None else -1)
    return (score, r.live_market_value or 0)


def _watchlist_rank_key(r):
    """Default watchlist sort: verdict score desc, upside as tiebreak."""
    score = (r.verdict.score if r.verdict and r.verdict.score is not None else -1)
    return (score, r.upside_pct if r.upside_pct is not None else -1e6)


def compute_run_ranks(
    results: list[PositionAnalysis],
    watchlists: Optional[dict[str, list[PositionAnalysis]]] = None,
) -> dict[str, dict]:
    """Return {ticker: {"group": str, "rank": int}} — each ticker's 1-based
    position under the report's default (verdict-score) sort within its group.
    Groups: 'compounder', 'thematic', 'watch:<list>'. Held tickers are excluded
    from watchlist ranking to mirror the rendered tables (holdings win)."""
    ranks: dict[str, dict] = {}
    compounders = sorted((r for r in results if r.bucket == "compounder"),
                         key=_holding_rank_key, reverse=True)
    thematics = sorted((r for r in results if r.bucket == "thematic"),
                       key=_holding_rank_key, reverse=True)
    for group, lst in (("compounder", compounders), ("thematic", thematics)):
        for i, r in enumerate(lst, 1):
            ranks[r.ticker] = {"group": group, "rank": i}
    if watchlists:
        held = {r.ticker for r in results}
        for wl_name, items in watchlists.items():
            ordered = sorted((r for r in items if r.ticker not in held),
                             key=_watchlist_rank_key, reverse=True)
            for i, r in enumerate(ordered, 1):
                # A ticker in several lists is ranked by the first list it
                # appears in (its analysis object is shared across lists).
                ranks.setdefault(r.ticker,
                                 {"group": f"watch:{wl_name}", "rank": i})
    return ranks


def _attach_rank_moves(
    history: dict,
    ranks: dict[str, dict],
    results: list[PositionAnalysis],
    watchlists: Optional[dict[str, list[PositionAnalysis]]] = None,
    run_date: Optional[str] = None,
) -> None:
    """Compare this run's ranks against the rank recorded on the *previous
    calendar day* (not merely the previous run) and stash the movement on each
    analysis object as `r._rank_move` for the ticker badge: {"delta":
    places_gained, "new": bool}. Positive delta = climbed. `new` marks a ticker
    with no comparable prior-day rank (first sighting, or it changed group).

    Comparing to the prior day (rather than the prior run) means the two runs on
    the same trading day — 9:30 AM and 4 PM — both show movement relative to
    yesterday's close-of-day ranking, instead of the afternoon run diffing only
    against the morning run. MUST run before update_recs_history rolls the
    baseline forward."""
    if run_date is None:
        run_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    tickers = history.get("tickers", {})

    def _move_for(ticker: str) -> Optional[dict]:
        cur = ranks.get(ticker)
        if not cur:
            return None
        entry = tickers.get(ticker)
        if not entry:
            return {"delta": None, "new": True}
        # Baseline = rank as of the last run of a previous calendar day. If the
        # most recent stored run was itself on an earlier day, that run IS the
        # prior-day baseline; if it already ran earlier today, use the retained
        # prior-day baseline so both of today's runs compare to yesterday.
        if entry.get("last_rank_date") != run_date:
            base_rank = entry.get("last_rank")
            base_group = entry.get("last_rank_group")
        else:
            base_rank = entry.get("prev_day_rank")
            base_group = entry.get("prev_day_rank_group")
        if base_rank is None or base_group != cur["group"]:
            return {"delta": None, "new": True}
        return {"delta": base_rank - cur["rank"], "new": False}

    seen: set[str] = set()
    for r in results:
        r._rank_move = _move_for(r.ticker)
        seen.add(r.ticker)
    for items in (watchlists or {}).values():
        for r in items:
            if r.ticker not in seen:
                r._rank_move = _move_for(r.ticker)
                seen.add(r.ticker)


def update_recs_history(
    history: dict,
    results: list[PositionAnalysis],
    watchlists: Optional[dict[str, list[PositionAnalysis]]] = None,
    run_date: Optional[str] = None,
    ranks: Optional[dict[str, dict]] = None,
) -> dict:
    """Record the first sighting of every ticker we see (any verdict) and refresh
    latest price/alloc for already-tracked tickers. Mutates and returns
    `history`. `ranks` (from compute_run_ranks) is persisted per ticker, rolling
    a prior-DAY baseline forward on the first run of each new day so the next run
    can render rank-movement badges relative to yesterday."""
    if run_date is None:
        run_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    if ranks is None:
        ranks = compute_run_ranks(results, watchlists)
    tickers = history.setdefault("tickers", {})
    snapshot = _current_rec_snapshot(results, watchlists)

    for ticker, cur in snapshot.items():
        entry = tickers.get(ticker)
        cur_rank = ranks.get(ticker) or {}
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
                "last_date": run_date,
                "last_price": cur["price"],
                "last_verdict": cur["verdict"],
                "last_verdict_score": cur.get("verdict_score"),
                "last_alloc": cur["alloc"],
                "last_why": cur.get("why", ""),
                "last_news": cur.get("news"),
                "peak_price": cur["price"],
                "peak_date": run_date,
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
            continue
        # Refresh latest snapshot for an already-tracked ticker.
        entry["name"] = cur["name"] or entry.get("name")
        entry["sector"] = cur["sector"] or entry.get("sector")
        entry["last_date"] = run_date
        entry["last_price"] = cur["price"]
        entry["last_verdict"] = cur["verdict"]
        entry["last_verdict_score"] = cur.get("verdict_score")
        entry["last_alloc"] = cur["alloc"]
        entry["last_why"] = cur.get("why", "")
        entry["last_news"] = cur.get("news")
        # Backfill the first-sight diagnostics for entries created before this
        # field existed — but only while the verdict is unchanged, so the current
        # reasoning still represents the original sighting (don't misattribute a
        # later ADD's reasoning to an original HOLD).
        if not entry.get("first_why") and entry.get("first_verdict") == cur["verdict"]:
            entry["first_why"] = cur.get("why", "")
        if entry.get("first_news") is None and entry.get("first_verdict") == cur["verdict"]:
            entry["first_news"] = cur.get("news")
        if cur["price"] is not None and cur["price"] > entry.get("peak_price", 0):
            entry["peak_price"] = cur["price"]
            entry["peak_date"] = run_date
        # Rank: on the first run of a new day, roll the daily baseline forward
        # (the prior run — yesterday's last — becomes what today diffs against).
        # Same-day re-runs keep the baseline so both runs compare to yesterday.
        if entry.get("last_rank_date") != run_date:
            entry["prev_day_rank"] = entry.get("last_rank")
            entry["prev_day_rank_group"] = entry.get("last_rank_group")
        # prev_rank still tracks the immediately-previous run (kept for history).
        entry["prev_rank"] = entry.get("last_rank")
        entry["prev_rank_group"] = entry.get("last_rank_group")
        entry["last_rank"] = cur_rank.get("rank")
        entry["last_rank_group"] = cur_rank.get("group")
        entry["last_rank_date"] = run_date
    return history


def save_recs_history(history: dict, path: str = RECS_HISTORY_FILE) -> None:
    """Write the ledger back out (pretty-printed for clean git diffs)."""
    try:
        with open(path, "w") as f:
            json.dump(history, f, indent=2, sort_keys=True)
            f.write("\n")
    except OSError as e:
        print(f"[history] Could not save {path}: {e}")


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

        # --- Why it wasn't a BUY/ADD (the core of the column). The analyzer's
        #     verdict breakdown already names the factors that held the score
        #     below the buy bar; surface them, distinguishing the original
        #     sighting ("initially") from now. ---
        BUY_TYPES = {"ADD", "BUY", "BUY MORE"}
        last_verdict = e.get("last_verdict")
        first_why = (e.get("first_why") or "").strip()
        last_why = (e.get("last_why") or "").strip()
        if last_verdict in BUY_TYPES:
            # The analyzer rates it a buy NOW — so the miss is that it only
            # turned bullish after the move while we stayed under-allocated.
            extra = ("<strong>Why we under-acted:</strong> the analyzer rates it "
                     f"<strong>{_esc_txt(last_verdict)}</strong> only now — it turned "
                     f"bullish after the run-up")
            if verdict and verdict not in BUY_TYPES:
                lim = f" held back by {_esc_txt(first_why)}" if first_why else ""
                extra += (f"; at first sight it was <strong>{_esc_txt(verdict)}</strong>"
                          f"{(',' + lim) if lim else ''}")
            extra += ", so you stayed under-allocated as it climbed."
        else:
            drag = last_why or first_why
            extra = ("<strong>Why it wasn't a buy:</strong> rated "
                     f"<strong>{_esc_txt(last_verdict or '—')}</strong>")
            extra += (f" — held back by {_esc_txt(drag)}" if drag
                      else " — no single strong buy signal scored high enough")
            if verdict and last_verdict and verdict != last_verdict:
                extra += f" (was <strong>{_esc_txt(verdict)}</strong> at first sight)"
            extra += "."
        reason += "<br>" + extra

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
            "last_alloc": last_alloc,
            "reason": reason,
        })
    out.sort(key=lambda d: d["gain_pct"], reverse=True)
    return out


def _render_missed_opportunities(missed: list[dict], tracked_count: int = 0) -> str:
    """Render the 'Missed Opportunities' section. When there are no qualifying
    misses, render an empty-state note so the section is still discoverable and
    shows that tracking is live (option 2)."""

    def _esc(s: str) -> str:
        return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

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

    # ----- Populated table -----
    verdict_colors = {
        "ADD": "#27ae60", "BUY": "#27ae60", "BUY MORE": "#27ae60",
        "WATCH": "#2980b9",
        "HOLD": "#2c3e50",
        "TRIM": "#e67e22",
        "SELL": "#c0392b",
    }
    html += (
        '<p style="color:#7f8c8d;font-size:12px;margin-top:-6px;margin-bottom:18px;">'
        f"Stocks the analyzer has tracked (every holding and watchlist name) that "
        f"climbed <strong>≥{MISSED_OPP_GAIN_PCT:g}%</strong> since they were first "
        f"seen, yet you still hold <strong>under {MISSED_OPP_ALLOC_THRESHOLD:g}%</strong> "
        f"of the portfolio (never bought, or under-allocated). Sorted by gain you left "
        f"on the table.</p>\n"
    )
    html += "<div class='table-wrap'><table>\n<thead><tr>"
    html += (
        "<th>Ticker</th>"
        "<th>Name / Sector</th>"
        "<th>First seen</th>"
        "<th>First verdict</th>"
        "<th>Current verdict</th>"
        "<th class='num'>Gain since</th>"
        "<th title='Hover for full explanation.' style='cursor:help;'>Miss reason &#9432;</th>"
        "</tr></thead><tbody>\n"
    )
    import re as _re
    for m in missed:
        label = m.get("first_verdict") or "—"
        color = verdict_colors.get(label, "#7f8c8d")
        cur_verdict = m.get("last_verdict") or "—"
        cur_color = verdict_colors.get(cur_verdict, "#7f8c8d")
        ticker = _esc(m["ticker"])
        sector = _esc(m["sector"]) if m.get("sector") else "—"
        first_date = m.get("first_date") or ""
        date_iso = first_date[:10] if first_date else ""
        date_display = _esc(_fmt_short_date(first_date)) if first_date else "—"
        # reason is pre-built HTML (contains <strong>); store as HTML in data-reason.
        reason_html = m.get("reason") or ""
        # Escape for use inside a double-quoted HTML attribute.
        # Headlines can contain apostrophes (safe in double-quoted attrs) but not
        # straight double quotes, so only &quot; escaping is needed for ".
        reason_attr = reason_html.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        # Short preview text (strip tags) for the visible truncated cell.
        reason_preview = _re.sub(r'<[^>]+>', '', reason_html).replace('&ldquo;', '"').replace('&rdquo;', '"').replace('&mdash;', '—').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').strip()
        search_val = f"{m['ticker'].lower()} {m['name'].lower()} {(m.get('sector') or '').lower()}"
        html += f"<tr data-search='{_esc(search_val)}'>"
        html += (f"<td class='ticker' data-sort='{ticker}'>"
                 f"<a class='miss-ticker-link' data-ticker='{ticker}' href='#' "
                 f"style='font-weight:700;color:inherit;text-decoration:underline;"
                 f"text-decoration-style:dotted;cursor:pointer;'>{ticker}</a></td>")
        html += (f"<td data-sort='{_esc(m['name'])}'><div style='font-weight:500'>{_esc(m['name'])}</div>"
                 f"<div style='color:var(--fg-muted);font-size:11px;'>{sector}</div></td>")
        html += (f"<td data-sort='{date_iso}'>"
                 f"<span style='color:var(--fg-muted);font-size:11px;'>{date_display}</span></td>")
        def _verdict_chip(v_label, v_color, v_score):
            score_str = (f"<span class='vscore' style='color:var(--fg-body);font-size:11px;"
                         f"font-weight:700;margin-left:4px;font-variant-numeric:tabular-nums;'>"
                         f"{int(round(v_score))}</span>") if v_score is not None else ""
            return (f"<span class='verdict' style='background:{v_color};'>{v_label}</span>"
                    f"{score_str}")
        first_score = m.get("first_verdict_score")
        cur_score   = m.get("last_verdict_score")
        html += f"<td data-sort='{label}'>{_verdict_chip(label, color, first_score)}</td>"
        html += f"<td data-sort='{cur_verdict}'>{_verdict_chip(_esc(cur_verdict), cur_color, cur_score)}</td>"
        html += (f"<td class='num pos-up' data-sort='{m['gain_pct']:.2f}' style='font-weight:600;'>"
                 f"{_fmt_pct(m['gain_pct'], 1, True)}</td>")
        # miss-reason cell: JS (at page load) converts this into a vcell+vcard
        # hover card identical to the verdict-score tooltip system.
        html += f'<td class=\'miss-reason\' data-reason="{reason_attr}"></td>'
        html += "</tr>\n"
    html += "</tbody></table></div>\n"
    return html


def _portfolio_insights(results: list[PositionAnalysis]) -> list[dict]:
    """Build prioritized, data-driven recommendations for the header chip.

    Turns the per-position analysis into a few concise, actionable findings
    (exit/trim flags, concentration, high-conviction adds, stretched
    valuations, weak fundamentals, insider selling). Each item is a dict
    {icon, label, detail, tone} where tone (danger/warn/good) drives the
    colored icon chip in the panel. Most important first; empty list means
    nothing notable.
    """
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

    sells = [r for r in held if r.verdict and r.verdict.label == "SELL"]
    if sells:
        out.append({"icon": "🚩", "label": "Review for exit", "tone": "danger",
                    "detail": f"{names(sells)} — scoring below the framework's "
                              f"keep threshold."})

    trims = [r for r in held if r.verdict and r.verdict.label == "TRIM"]
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

    adds = [r for r in held if r.verdict and r.verdict.label == "ADD"]
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
            and (not r.verdict or r.verdict.label not in ("SELL", "TRIM"))]
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

    action_items = [r for r in results
                    if r.verdict and r.verdict.label in ("SELL", "TRIM")]
    add_items = [r for r in results if r.verdict and r.verdict.label == "ADD"]

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
            f'<a class="stat clickable" href="#" '
            f'onclick="applyHeaderFilter(\'verdict-buy\');return false;">'
            f'<strong>{watchlist_buys} / {watchlist_total}</strong>'
            f'Watchlist BUY signals</a>'
        )

    has_holdings = bool(results)
    report_title = "Portfolio Analysis" if has_holdings else "Stock Analysis"

    # Top-of-report meter row: market sentiment · book quality · concentration.
    # Each renderer returns "" when its data is unavailable, so the row simply
    # shows whichever gauges apply (and collapses entirely with no holdings).
    _meter_cards = [
        _render_fear_greed_gauge(fetch_market_fear_greed()),
        _render_portfolio_health_gauge(results),
        _render_diversification_gauge(results),
    ]
    _meter_cards = [c for c in _meter_cards if c]
    market_meter_html = (
        f'<div class="market-meters-row">{"".join(_meter_cards)}</div>'
        if _meter_cards else ""
    )

    # Quick-recommendations chip — sits in the header controls beside Refresh.
    # Hover reveals the full list (JS handles hover/scroll/leave auto-hide).
    qr_chip_html = ""
    if has_holdings:
        _insights = _portfolio_insights(results)
        if _insights:
            _rows = "".join(
                f'<div class="qr-item qr-{it["tone"]}">'
                f'<span class="qr-ico">{it["icon"]}</span>'
                f'<span class="qr-text">'
                f'<span class="qr-label">{it["label"]}</span>'
                f'<span class="qr-detail">{it["detail"]}</span>'
                f'</span></div>'
                for it in _insights
            )
            qr_chip_html = (
                '<div class="qr-wrap" id="qrWrap">'
                f'<button class="qr-trigger" id="qrTrigger">'
                f'<span class="qr-bulb">💡</span>Quick recommendations'
                f'<span class="qr-count">{len(_insights)}</span></button>'
                f'<div class="qr-panel" id="qrPanel">'
                f'<div class="qr-panel-head">Quick recommendations</div>'
                f'<div class="qr-list">{_rows}</div>'
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
        holdings_summary = f"""
    <div class="stat"><strong>{_fmt_money(live_total)}</strong>Portfolio value (live)</div>
    {today_stat}
    {after_hours_stat}
    <a class="stat clickable" href="#compounders" onclick="scrollToSection('compounders');return false;"><strong>{len(compounders)}</strong>Compounder positions</a>
    <a class="stat clickable" href="#thematic" onclick="scrollToSection('thematic');return false;"><strong>{len(thematics)}</strong>Thematic / ETF positions</a>
    <a class="stat clickable" href="#" onclick="applyHeaderFilter('action');return false;"><strong>{len(action_items)}</strong>Sell / Trim flags</a>
    <a class="stat clickable" href="#" onclick="applyHeaderFilter('verdict-add');return false;"><strong>{len(add_items)}</strong>Add candidates</a>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<!-- GitHub Pages serves HTML with Cache-Control: max-age=600, so a plain
     browser refresh can show the previous report for up to ~10 min after a new
     run deploys. Tell the browser to always revalidate the document so a manual
     refresh fetches the freshly published HTML (the auto-refresh separately
     cache-busts with a ?v=timestamp query). -->
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
  /* Compact report header: title + meta on the left, controls (refresh,
     theme toggle) on the right, all in one wrapping flex row. */
  .report-header {{ display: flex; justify-content: space-between;
                    align-items: center; gap: 16px; flex-wrap: wrap;
                    margin: 0 0 12px; }}
  .report-header .sub {{ margin: 3px 0 0; }}
  .report-controls {{ display: flex; align-items: center; gap: 8px; }}
  .refresh-btn {{ height: 34px; padding: 0 14px; border-radius: 17px;
                  border: 1px solid var(--border-medium);
                  background: var(--bg-card); color: var(--fg-body);
                  cursor: pointer; font-size: 12px; font-weight: 600;
                  display: flex; align-items: center; gap: 6px;
                  box-shadow: var(--shadow-card);
                  transition: transform 0.15s, background 0.2s; }}
  .refresh-btn:hover {{ transform: scale(1.04); background: var(--bg-card-hover); }}
  .refresh-btn:disabled {{ opacity: 0.5; cursor: default; transform: none; }}
  .auto-toggle.active,
  .tax-toggle.active {{ background: var(--bg-chip-green);
                        color: var(--fg-chip-green);
                        border-color: var(--pos-up); }}
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
  .sub {{ color: var(--fg-muted); font-size: 13px; margin-bottom: 28px; }}

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
  /* Frequent-combo pills live in #freqFilters; display:contents lets the
     buttons flow directly in the filter-bar-top flex row. The ★ marks them
     as your recently-used combos vs. the regular pills in More filters. */
  #freqFilters {{ display: contents; }}
  .freq-pill::before {{ content: "★ "; color: var(--accent, #e6a817);
                        font-size: 10px; }}
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

  /* ---------- Theme toggle button (in the header controls cluster) ---------- */
  .theme-toggle {{ width: 34px; height: 34px; flex: 0 0 auto;
                   border-radius: 50%; border: 1px solid var(--border-medium);
                   background: var(--bg-card); color: var(--fg-body);
                   cursor: pointer; font-size: 16px;
                   display: flex; align-items: center; justify-content: center;
                   box-shadow: var(--shadow-card);
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
    .refresh-status {{ text-align: left; margin-top: 0; }}
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
  // Keep refreshes (browser reload or the auto-refresh) anchored at the
  // top. Two things otherwise scroll the page on reload: (1) the browser's
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
  <div>
    <h1>{report_title}</h1>
    <div class="sub">Last updated <span id="lastUpdatedAgo" data-generated-ms="{now_epoch_ms}">{relative_now}</span> · {now}{' · Finnhub enabled' if FINNHUB_API_KEY else ''}</div>
  </div>
  <div class="report-controls">
    {qr_chip_html}
    {refresh_button_html}
    <button class="refresh-btn auto-toggle" id="autoReloadToggle" aria-pressed="false"
            title="Auto-reload this page every 30 min to pick up the latest published report">
      &#9201; Auto</button>
    <button class="theme-toggle" id="themeToggle"
            title="Toggle light/dark theme" aria-label="Toggle theme">🌙</button>
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

<div class="filter-bar">
  <div class="filter-bar-top">
    <input type="text" id="searchInput" placeholder="🔍 Search ticker or name…" autocomplete="off">
    <!-- Your 10 most recently-used filter combos, populated from localStorage
         by JS (persists across regenerated reports — same Pages origin). Seeds
         with the Quick-picks defaults below until usage history builds up. -->
    <span id="freqFilters"></span>
    <button class="more-toggle" id="moreToggle">More filters ▾</button>
    <button class="filter-pill clear-pill" id="clearFilters">✕ Clear</button>
    <span class="filter-status" id="filterStatus"></span>
  </div>
  <div class="filter-more" id="filterMore">
    <div class="filter-group">
      <span class="filter-group-label">Quick picks</span>
      <button class="filter-pill" data-filter="buy">Buy signals</button>
      <button class="filter-pill" data-filter="action">Action (SELL/TRIM)</button>
      <button class="filter-pill" data-filter="high-quality">Quality 7+</button>
      <button class="filter-pill" data-filter="hot-sector">🔥 Hot</button>
      <button class="filter-pill" data-filter="insider-buy">✓ Insider buying</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">Verdict</span>
      <button class="filter-pill" data-filter="verdict-add">ADD only</button>
      <button class="filter-pill" data-filter="verdict-hold">HOLD only</button>
      <button class="filter-pill" data-filter="verdict-trim">TRIM only</button>
      <button class="filter-pill" data-filter="verdict-sell">SELL only</button>
      <button class="filter-pill" data-filter="verdict-buy">BUY (watchlist) only</button>
      <button class="filter-pill" data-filter="verdict-watch">WATCH only</button>
      <button class="filter-pill" data-filter="verdict-score-high">Verdict score 75+</button>
      <button class="filter-pill" data-filter="verdict-score-85">Verdict score 85+</button>
      <button class="filter-pill" data-filter="verdict-score-90">Verdict score 90+</button>
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
      <span class="filter-group-label">Today</span>
      <button class="filter-pill" data-filter="up-today">▲ Up today</button>
      <button class="filter-pill" data-filter="down-today">▼ Down today</button>
      <button class="filter-pill" data-filter="big-up-today">▲ Movers (+3%)</button>
      <button class="filter-pill" data-filter="big-down-today">▼ Movers (-3%)</button>
    </div>
    <div class="filter-group">
      <span class="filter-group-label">Rank movement</span>
      <button class="filter-pill" data-filter="moved-up">▲ Moved up</button>
      <button class="filter-pill" data-filter="moved-down">▼ Moved down</button>
      <button class="filter-pill" data-filter="big-climbers">▲▲ Big climbers (3+)</button>
      <button class="filter-pill" data-filter="big-fallers">▼▼ Big fallers (3+)</button>
      <button class="filter-pill" data-filter="rank-new">★ New today</button>
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
      <span class="filter-group-label">Earnings</span>
      <button class="filter-pill" data-filter="earnings-soon">📅 Reports within 7d</button>
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
            verdict_html = _verdict_cell(r.verdict, getattr(r, 'days_to_earnings', None))
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
            html += _td(_today_cell(r),
                        r.day_change_pct if r.day_change_pct is not None else -1e6,
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
                verdict_html = _verdict_cell(r.verdict, getattr(r, 'days_to_earnings', None))
                quality_cell = (
                    f"{_filter_dots(r.filters)} "
                    f"<span style='color:var(--fg-muted);font-size:11px'>{passed}/9</span>"
                    if r.filters else "<span style='color:var(--fg-faint);'>n/a</span>"
                )
                html += _tr_open(r)
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
                html += _td(verdict_html, (r.verdict.score if r.verdict and r.verdict.score is not None else (100 - _VERDICT_ORDER.get(verdict_label, 99))))
                html += "</tr>\n"
            html += "</tbody></table></div>\n"

    # ---------- Missed Opportunities (recs we under-acted on) ----------
    # `missed_opportunities is not None` signals the history-tracking flow is
    # active (main portfolio run) — render even when empty so the section is
    # discoverable. Ad-hoc/lookup mode passes None and the section is omitted.
    if missed_opportunities is not None:
        html += _render_missed_opportunities(missed_opportunities, recs_tracked_count)

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
            verdict_label = r.verdict.label if r.verdict else "—"
            rating_score = -1
            if r.rating_breakdown and r.rating_breakdown.get("total"):
                t = r.rating_breakdown["total"]
                rating_score = (
                    (r.rating_breakdown.get("buy", 0)
                     - r.rating_breakdown.get("sell", 0)) / t
                )
            rating_html = _rating_bar(r.rating_breakdown, r.recommendation, r.num_analysts)
            verdict_html = _verdict_cell(r.verdict, getattr(r, 'days_to_earnings', None))
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
// Auto-refresh toggle: when enabled (persisted in localStorage), every
// 30 min it triggers the GitHub Actions workflow (same flow as the manual
// Refresh button: dispatch -> poll -> reload when the new report deploys).
// Fallbacks: no stored token or no refresh widget -> plain page reload; a
// refresh already in progress -> skip this cycle (its success path reloads).
// The reload keeps the password session (sessionStorage) and re-arms the
// timer. A 30s interval checking a deadline (rather than one long setTimeout)
// survives background-tab throttling and laptop sleep.
(function() {
  var KEY = 'auto-reload-hourly';
  var PERIOD_MS = 1800000;
  var btn = document.getElementById('autoReloadToggle');
  if (!btn) return;
  var timer = null;

  function canDispatch() {
    var rb = document.getElementById('ghRefreshBtn');
    return typeof window.ghTriggerRefresh === 'function' && rb &&
           typeof window.ghHasToken === 'function' && window.ghHasToken();
  }
  function label() {
    if (!btn.classList.contains('active')) {
      btn.innerHTML = '&#9201; Auto';
      btn.title = 'Every 30 min: trigger the GitHub workflow to regenerate the '
                + 'report, then reload this page when it deploys. (Without a '
                + 'saved token it only reloads the page.)';
      return;
    }
    var nextAt = parseInt(btn.dataset.nextAt || '0', 10);
    var mins = Math.max(1, Math.round((nextAt - Date.now()) / 60000));
    btn.innerHTML = '&#9201; Auto &middot; ' + mins + 'm';
    btn.title = 'Auto-refresh is ON — in ~' + mins + ' min: '
              + (canDispatch()
                 ? 'trigger the workflow and reload when the new report deploys.'
                 : 'reload the page (no saved token, so the workflow is not triggered).')
              + ' Click to turn off.';
  }
  function fire() {
    // Re-arm first so a failed run is retried next cycle, not every 30s.
    btn.dataset.nextAt = String(Date.now() + PERIOD_MS);
    var rb = document.getElementById('ghRefreshBtn');
    if (rb && rb.disabled) {
      label();   // a refresh is already running — it reloads the page itself
      return;
    }
    if (canDispatch()) {
      window.ghTriggerRefresh();
    } else {
      (window.reloadFreshReport || location.reload.bind(location))();
    }
    label();
  }
  function check() {
    var nextAt = parseInt(btn.dataset.nextAt || '0', 10);
    if (nextAt && Date.now() >= nextAt) { fire(); return; }
    label();
  }
  function setState(on, save, interactive) {
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (save) { try { localStorage.setItem(KEY, on ? '1' : '0'); } catch (e) {} }
    if (timer) { clearInterval(timer); timer = null; }
    if (on) {
      // Ask for the token now (once) so the unattended auto-refresh trigger can
      // work later — prompting at 3am when the timer fires would be useless.
      if (interactive && typeof window.ghEnsureToken === 'function'
          && !((window.ghHasToken && window.ghHasToken()))) {
        window.ghEnsureToken();
      }
      btn.dataset.nextAt = String(Date.now() + PERIOD_MS);
      timer = setInterval(check, 30000);
      document.addEventListener('visibilitychange', check);
    } else {
      delete btn.dataset.nextAt;
      document.removeEventListener('visibilitychange', check);
    }
    label();
  }
  btn.addEventListener('click', function() {
    setState(!btn.classList.contains('active'), true, true);
  });
  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) {}
  setState(saved === '1', false, false);
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
    var dayPct = num(row.getAttribute('data-day-pct'));
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
    var earningsDays = num(row.getAttribute('data-earnings-days'));
    var rankMove = row.getAttribute('data-rank-move') || '';
    var rankDelta = num(row.getAttribute('data-rank-delta'));

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
      case 'verdict-score-85':   return !isNaN(verdictScore) && verdictScore >= 85;
      case 'verdict-score-90':   return !isNaN(verdictScore) && verdictScore >= 90;
      case 'verdict-score-low':  return !isNaN(verdictScore) && verdictScore < 40;

      // ------------- Rank movement (vs previous day) -------------
      case 'moved-up':       return rankMove === 'up';
      case 'moved-down':     return rankMove === 'down';
      case 'rank-new':       return rankMove === 'new';
      case 'big-climbers':   return rankMove === 'up'   && !isNaN(rankDelta) && rankDelta >= 3;
      case 'big-fallers':    return rankMove === 'down' && !isNaN(rankDelta) && rankDelta <= -3;

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

      // ------------- Today's move -------------
      case 'up-today':        return !isNaN(dayPct) && dayPct > 0;
      case 'down-today':      return !isNaN(dayPct) && dayPct < 0;
      case 'big-up-today':    return !isNaN(dayPct) && dayPct >= 3;
      case 'big-down-today':  return !isNaN(dayPct) && dayPct <= -3;

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

      // ------------- Earnings -------------
      // "Soon" = reports within 7 days (forward-only). Keep this threshold in
      // sync with EARNINGS_SOON_DAYS in the Python side.
      case 'earnings-soon':      return !isNaN(earningsDays) && earningsDays >= 0 && earningsDays <= 7;

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
    // Highlight the More-filters button when advanced filters are active,
    // WITHOUT force-opening — the panel is hover/scroll controlled below, so
    // it stays hidden until hovered even while an advanced filter is on.
    if (moreSection && moreToggle) {
      var anyMoreActive =
        moreSection.querySelectorAll('.filter-pill.active').length > 0;
      moreToggle.classList.toggle('expanded', anyMoreActive);
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
      recordUsageDebounced();   // learn the user's combos over time
      renderFreqActive();
    });
  });

  // Clear-all
  if (clearBtn) {
    clearBtn.addEventListener('click', function() {
      activeFilters.clear();
      pills.forEach(function(p) { p.classList.remove('active'); });
      searchInput.value = '';
      applyFilters();
      renderFreqActive();
    });
  }

  // More-filters: hover the button to open; auto-hide when the pointer
  // leaves the filter bar or the page scrolls. Click still toggles — needed
  // on touch devices, where hover/mouseleave don't fire. The 'expanded'
  // highlight (active advanced filters) is managed separately in applyFilters,
  // so only the arrow + .show reflect open/closed here.
  if (moreToggle && moreSection) {
    function openMore() {
      moreSection.classList.add('show');
      moreToggle.textContent = 'More filters ▴';
    }
    function closeMore() {
      if (!moreSection.classList.contains('show')) return;
      moreSection.classList.remove('show');
      moreToggle.textContent = 'More filters ▾';
    }
    moreToggle.addEventListener('mouseenter', openMore);
    moreToggle.addEventListener('click', function() {
      moreSection.classList.contains('show') ? closeMore() : openMore();
    });
    var filterBar = moreToggle.closest('.filter-bar');
    if (filterBar) filterBar.addEventListener('mouseleave', closeMore);
    window.addEventListener('scroll', closeMore, { passive: true });
  }

  // ----- Recently-used filter combos -----
  // The most RECENTLY used filter combinations, persisted in localStorage and
  // shown as ★ pills at the top — adapting to how you actually filter.
  // localStorage is per-origin, so this history survives every
  // regenerated/republished report.
  var freqEl = document.getElementById('freqFilters');
  var USAGE_KEY = 'filterComboUsage';   // { comboKey: lastUsedEpochMs }
  var FREQ_MAX = 10;   // how many ★ combo pills to show (latest N)
  // Seed (and fallback) combos = the former fixed quick-picks.
  var DEFAULT_COMBOS = [['buy'], ['action'], ['high-quality'],
                        ['hot-sector'], ['insider-buy']];

  function loadUsage() {
    try { return JSON.parse(localStorage.getItem(USAGE_KEY)) || {}; }
    catch (e) { return {}; }
  }
  function saveUsage(u) {
    try { localStorage.setItem(USAGE_KEY, JSON.stringify(u)); } catch (e) {}
  }
  function comboKey(keys) { return keys.slice().sort().join('+'); }
  function currentKey() { return Array.from(activeFilters).sort().join('+'); }

  var recordTimer = null;
  function recordUsageDebounced() {
    // Record the combo the user settles on (not every intermediate toggle),
    // stamped with when it was last used so the bar shows most-recent combos.
    clearTimeout(recordTimer);
    recordTimer = setTimeout(function() {
      if (!activeFilters.size) return;        // never record the empty set
      var u = loadUsage();
      u[currentKey()] = Date.now();           // last-used timestamp (ms)
      var keys = Object.keys(u);
      if (keys.length > 40) {                 // cap growth: keep the 40 most recent
        keys.sort(function(a, b) { return u[b] - u[a]; });
        var t = {}; keys.slice(0, 40).forEach(function(x) { t[x] = u[x]; });
        u = t;
      }
      saveUsage(u);
    }, 1500);
  }

  function comboLabel(keys) {
    var out = [];
    for (var i = 0; i < keys.length; i++) {
      var p = document.querySelector('.filter-pill[data-filter="' + keys[i] + '"]');
      if (!p) return null;                    // key absent in this report version
      out.push(p.textContent.trim());
    }
    return out.join(' + ');
  }

  function setCombo(keys) {
    activeFilters.clear();
    // Activate every requested key directly — not just ones with a matching
    // pill — so header-stat filters (e.g. 'earnings-soon') still work even if
    // no pill is rendered for them.
    keys.forEach(function(k) { if (k) activeFilters.add(k); });
    pills.forEach(function(p) {
      p.classList.toggle('active', keys.indexOf(p.getAttribute('data-filter')) >= 0);
    });
    applyFilters();
    renderFreqActive();
  }

  function renderFreqActive() {
    if (!freqEl) return;
    var cur = currentKey();
    freqEl.querySelectorAll('.freq-pill').forEach(function(b) {
      b.classList.toggle('active', b.dataset.combo === cur);
    });
  }

  function renderFrequent() {
    if (!freqEl) return;
    var u = loadUsage();
    // Most-recent first (values are last-used timestamps).
    var combos = Object.keys(u)
      .sort(function(a, b) { return u[b] - u[a]; })
      .map(function(k) { return k.split('+'); });
    // Pad with default quick-picks not already present so the bar is never empty.
    var seen = {};
    combos.forEach(function(c) { seen[comboKey(c)] = 1; });
    DEFAULT_COMBOS.forEach(function(c) {
      if (!seen[comboKey(c)]) { combos.push(c); seen[comboKey(c)] = 1; }
    });
    freqEl.innerHTML = '';
    var shown = 0;
    for (var i = 0; i < combos.length && shown < FREQ_MAX; i++) {
      var label = comboLabel(combos[i]);
      if (!label) continue;                   // skip combos with stale keys
      var b = document.createElement('button');
      b.className = 'filter-pill freq-pill';
      b.textContent = label;
      b.dataset.combo = comboKey(combos[i]);
      b.title = 'Saved filter combo — click to apply, click again to clear';
      (function(keys) {
        b.addEventListener('click', function() {
          if (currentKey() === comboKey(keys)) { setCombo([]); }   // toggle off
          else { setCombo(keys); recordUsageDebounced(); }
        });
      })(combos[i]);
      freqEl.appendChild(b);
      shown++;
    }
    renderFreqActive();
  }

  // Header summary tiles: apply a single filter (toggle off if it's already the
  // only active one) and scroll to the first non-empty results table so the
  // effect is visible even when the matching rows live in a lower section.
  window.applyHeaderFilter = function(key) {
    var off = (currentKey() === key);
    setCombo(off ? [] : [key]);
    if (off) return;
    var wraps = document.querySelectorAll('.table-wrap');
    for (var i = 0; i < wraps.length; i++) {
      if (wraps[i].style.display !== 'none') {
        var prev = wraps[i].previousElementSibling, tgt = wraps[i];
        while (prev) {
          if (prev.tagName === 'H2' || prev.tagName === 'H3') { tgt = prev; break; }
          prev = prev.previousElementSibling;
        }
        tgt.scrollIntoView({ behavior: 'smooth', block: 'start' });
        break;
      }
    }
  };

  renderFrequent();
  searchInput.addEventListener('input', applyFilters);
  applyFilters();
  window.applySearchFilter = function(term) {
    searchInput.value = term;
    applyFilters();
  };
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
             '.refresh-btn,.theme-toggle,#autoReloadToggle';

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

    print(f"Analyzing {len(rows)} positions...")
    results: list[PositionAnalysis] = analyze_positions_parallel(
        rows, use_robinhood_ratings=use_rh_ratings)

    # Analyze watchlists. Dedupe by ticker (a stock in multiple lists is
    # analyzed once), then fan the cached results back out per list.
    watchlists_analyzed: dict[str, list[PositionAnalysis]] = {}
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

    # Missed-opportunity tracking. Refresh the git-tracked ledger with this run's
    # buy-type verdicts (first sighting) and latest prices, then derive the set of
    # recommendations we under-acted on while the stock ran up. Best-effort: any
    # failure here must not block the report.
    missed_opportunities: list[dict] = []
    recs_tracked_count = 0
    try:
        recs_history = load_recs_history()
        # Rank each ticker under the report's default sort, then diff against the
        # ledger's prior-DAY ranks (attaches r._rank_move for the ▲/▼ badges)
        # BEFORE update_recs_history rolls the daily baseline forward. Both use
        # the same run_date so the same-day/new-day split stays consistent.
        _run_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        run_ranks = compute_run_ranks(results, watchlists_analyzed or None)
        _attach_rank_moves(recs_history, run_ranks, results,
                           watchlists_analyzed or None, run_date=_run_date)
        update_recs_history(recs_history, results, watchlists_analyzed or None,
                            run_date=_run_date, ranks=run_ranks)
        save_recs_history(recs_history)
        recs_tracked_count = len(recs_history.get("tickers", {}))
        missed_opportunities = compute_missed_opportunities(recs_history)
        print(f"[history] Tracking {recs_tracked_count} "
              f"stock(s); {len(missed_opportunities)} missed "
              f"opportunit{'y' if len(missed_opportunities) == 1 else 'ies'}.")
    except Exception as e:
        print(f"[history] Skipped missed-opportunity tracking: {e}")

    # Tax analysis for SELL/TRIM and low-score positions (holding period +
    # trim timing).
    try:
        from tax_analysis import (TaxConfig, analyze_tax, analyze_tax_with_lots,
                                  reconcile_lots_with_position)
        tax_cfg = TaxConfig.from_env()
        # Tax analysis is opt-in (--tax). When off, leave `flagged` empty so no
        # r.tax is populated and the tax section is omitted from the report.
        # Flag SELL/TRIM verdicts plus any position whose verdict score is
        # below 75 — weak-scoring holds are trim candidates too.
        flagged = ([r for r in results
                    if r.verdict and (
                        r.verdict.label in ("SELL", "TRIM")
                        or (r.verdict.score is not None
                            and r.verdict.score < TAX_FLAG_SCORE_THRESHOLD))]
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