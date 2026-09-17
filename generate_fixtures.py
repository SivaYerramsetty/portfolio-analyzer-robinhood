"""
generate_fixtures.py
--------------------
Fixture generator bridge for the CompounderWatch iOS app.

Runs the SAME data sources the report uses — yfinance (fundamentals, prices,
sector-ETF momentum), Finnhub (analyst), SEC EDGAR (insider Form 4), and the
news-sentiment scorer — and writes them into the app's `CompanyFundamentals`
JSON schema. The iOS app can't run these Python sources itself (they're
Python libraries / unlicensed Yahoo scraping), so we capture real numbers here
on the dev machine and bake them into the app's mock provider.

This doubles as the field spec for the production Swift clients: whatever this
script emits is exactly what a licensed fundamentals API + an EDGAR/Finnhub
Swift port must supply.

Usage:
    python generate_fixtures.py                 # all default tickers
    python generate_fixtures.py AAPL MSFT NVO   # a subset
    FINNHUB_API_KEY=... NEWS_SIGNAL=1 python generate_fixtures.py

Missing sources degrade gracefully: no Finnhub key -> analyst fields omitted;
EDGAR/news failures -> those factors simply drop, exactly as in the report.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Optional

import yfinance as yf

# Reuse the report's own source functions — same code path as report.html.
from analyze_portfolio import (
    get_sector_momentum,
    fetch_finnhub_recommendation,
    fetch_finnhub_price_target,
    score_news_sentiment,
    _safe_get,
)

try:
    from insider_trading import get_insider_activity, insider_score
    _HAVE_INSIDER = True
except Exception:
    _HAVE_INSIDER = False


# Default universe for the app's dev build: a broad S&P-100-style set of
# large-cap quality names plus popular ADRs, so a real broker export mostly
# resolves instead of landing in "not covered". ADRs are flagged so the app
# can badge them and note the reporting currency.
DEFAULT_TICKERS = [
    # Mega-cap tech / communication
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA",
    "AVGO", "ORCL", "CRM", "ADBE", "AMD", "INTC", "CSCO", "ACN", "IBM",
    "QCOM", "TXN", "INTU", "NOW", "AMAT", "MU", "ADI", "NFLX", "DIS",
    "CMCSA", "TMUS", "T", "VZ",
    # Financials
    "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "AXP", "BLK", "C",
    "SCHW", "SPGI", "CB", "PGR", "PYPL",
    # Healthcare
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "DHR", "PFE",
    "AMGN", "ISRG", "MDT", "BMY", "GILD", "ELV", "VRTX", "REGN",
    # Consumer
    "WMT", "COST", "HD", "PG", "KO", "PEP", "MCD", "NKE", "SBUX", "LOW",
    "TGT", "TJX", "BKNG", "CMG", "PM", "MO", "MDLZ", "CL", "EL",
    # Industrials / materials / energy
    "HON", "UNP", "CAT", "GE", "BA", "RTX", "DE", "LMT", "UPS", "ADP",
    "MMM", "LIN", "XOM", "CVX", "COP",
    # Berkshire
    "BRK-B",
    # Mid-caps & growth names common in quality-investor portfolios
    "ANET", "APH", "BSX", "CELH", "DUOL", "DXCM", "EA", "EXLS", "GATX",
    "GMED", "HWM", "LULU", "MSTR", "NBIX", "NEM", "PCTY", "UBER", "VRT",
    "DAL", "ON", "BE",
    # ADRs (foreign listings — reported in local currency)
    "NVO", "ASML", "TSM", "SAP", "TM", "AZN", "SNY", "UL", "BABA", "SHOP",
    "TCEHY",
]
ADR_TICKERS = {"NVO", "ASML", "TSM", "SAP", "TM", "AZN", "SNY", "UL", "BABA", "TCEHY"}
DISPLAY_NAME_SUFFIX = {t: " (ADR)" for t in ADR_TICKERS}

OUTPUT_PATH = (
    Path.home()
    / "Quality-Compounder Portfolio Monitor"
    / "CompounderWatch" / "Resources" / "fundamentals-fixtures.json"
)


# ---------------------------------------------------------------------------
# yfinance statement extraction -> the app's annuals[] schema
# ---------------------------------------------------------------------------

def _row(df, *labels) -> Optional[list]:
    """First matching row of a yfinance statement DataFrame, oldest-first,
    as a plain float list (drops NaN/inf). yfinance gives columns
    most-recent-first, so we reverse."""
    if df is None or df.empty:
        return None
    for label in labels:
        if label in df.index:
            vals = []
            for v in reversed(list(df.loc[label].values)):
                if v is None:
                    continue
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    continue
                if math.isnan(f) or math.isinf(f):
                    continue
                vals.append(f)
            return vals or None
    return None


def _annuals(tkr: yf.Ticker, info: dict) -> list[dict]:
    """Build the per-year annuals[] the app scores. Monetary values in
    millions of the listing currency; matches AnnualFinancials in Swift."""
    income = tkr.income_stmt
    balance = tkr.balance_sheet
    cash = tkr.cashflow

    # yfinance columns are Timestamps, most-recent-first. Reverse to ascending.
    years = [c.year for c in reversed(list(income.columns))] if income is not None and not income.empty else []
    if not years:
        return []

    revenue = _row(income, "Total Revenue", "Operating Revenue") or []
    net_income = _row(income, "Net Income", "Net Income Common Stockholders",
                      "Net Income From Continuing Operations") or []
    op_income = _row(income, "Operating Income", "Total Operating Income As Reported") or []
    diluted_eps = _row(income, "Diluted EPS", "Basic EPS")

    equity = _row(balance, "Stockholders Equity", "Common Stock Equity",
                  "Total Stockholder Equity") or []
    total_debt = _row(balance, "Total Debt")
    long_term_debt = _row(balance, "Long Term Debt")
    current_debt = _row(balance, "Current Debt", "Current Debt And Capital Lease Obligation")
    current_assets = _row(balance, "Current Assets")
    inventory = _row(balance, "Inventory")
    current_liab = _row(balance, "Current Liabilities")

    fcf = _row(cash, "Free Cash Flow")
    if fcf is None:
        ocf = _row(cash, "Operating Cash Flow")
        capex = _row(cash, "Capital Expenditure")
        if ocf and capex:
            n = min(len(ocf), len(capex))
            fcf = [ocf[i] + capex[i] for i in range(n)]  # capex is negative

    def at(series, i):
        return series[i] if series and i < len(series) else None

    def to_millions(v):
        return round(v / 1e6, 2) if v is not None else None

    out = []
    n = len(years)
    for i in range(n):
        rev = at(revenue, i)
        ni = at(net_income, i)
        eq = at(equity, i)
        op = at(op_income, i)
        f = at(fcf, i)
        if rev is None or ni is None or eq is None:
            continue  # can't score a year missing the core figures

        # Total debt: prefer the reported line, else LT + current.
        debt = at(total_debt, i)
        if debt is None:
            ltd, cd = at(long_term_debt, i), at(current_debt, i)
            debt = (ltd or 0) + (cd or 0) if (ltd or cd) else None

        # Historical quick ratio from the balance sheet: (CA - inventory) / CL.
        ca, inv, cl = at(current_assets, i), at(inventory, i), at(current_liab, i)
        quick = None
        if ca is not None and cl and cl > 0:
            quick = round((ca - (inv or 0)) / cl, 3)

        eps = at(diluted_eps, i)

        out.append({
            "fiscalYear": years[i],
            "revenue": to_millions(rev),
            "eps": round(eps, 2) if eps is not None else 0.0,
            "netIncome": to_millions(ni),
            "shareholdersEquity": to_millions(eq),
            "operatingIncome": to_millions(op) if op is not None else 0.0,
            "freeCashFlow": to_millions(f) if f is not None else 0.0,
            "totalDebt": to_millions(debt) if debt is not None else 0.0,
            "quickRatio": quick,
        })
    return out


# ---------------------------------------------------------------------------
# market{} — valuation + verdict-layer context (incl. the 3 new factors)
# ---------------------------------------------------------------------------

def _market(ticker: str, info: dict) -> dict:
    price = _safe_get(info, "regularMarketPrice") or _safe_get(info, "currentPrice")
    ma50 = _safe_get(info, "fiftyDayAverage")
    ma200 = _safe_get(info, "twoHundredDayAverage")

    # Trend from 50/200-day MA alignment (same basis the report uses).
    trend = None
    pct_above_ma200 = None
    if price and ma200 and ma200 > 0:
        pct_above_ma200 = round((price / ma200 - 1) * 100, 1)
        if ma50 and ma50 > ma200 and price > ma200:
            trend = "uptrend"
        elif ma50 and ma50 < ma200 and price < ma200:
            trend = "downtrend"
        else:
            trend = "sideways"

    m = {
        "price": round(price, 2) if price else None,
        "trailingPE": _safe_get(info, "trailingPE"),
        "forwardPE": _safe_get(info, "forwardPE"),
        "pegRatio": _safe_get(info, "trailingPegRatio") or _safe_get(info, "pegRatio"),
        "week52Low": _safe_get(info, "fiftyTwoWeekLow"),
        "week52High": _safe_get(info, "fiftyTwoWeekHigh"),
        "trend": trend,
        "pctAboveMA200": pct_above_ma200,
    }

    # --- Analyst (Finnhub preferred; yfinance fallback) ---
    rec = fetch_finnhub_recommendation(ticker)
    target = fetch_finnhub_price_target(ticker)
    if rec and rec.get("total"):
        total = rec["total"]
        # Weighted mean on the 1..5 scale (1 = strong buy).
        weighted = (rec.get("strongBuy", 0) * 1 + rec.get("buy", 0) * 2
                    + rec.get("hold", 0) * 3 + rec.get("sell", 0) * 4
                    + rec.get("strongSell", 0) * 5)
        m["analystRecAvg"] = round(weighted / total, 2)
        m["analystCount"] = total
    else:
        m["analystRecAvg"] = _safe_get(info, "recommendationMean")
        na = info.get("numberOfAnalystOpinions")
        m["analystCount"] = int(na) if na else None
    if target and target.get("targetMean"):
        m["analystTargetMean"] = round(float(target["targetMean"]), 2)
    else:
        m["analystTargetMean"] = _safe_get(info, "targetMeanPrice")

    # --- (1) Sector momentum -> verdict modifier +5/-4 ---
    sector = info.get("sector")
    sm = get_sector_momentum(sector)
    if sm.get("label") and sm["label"] not in ("Unknown",):
        m["sectorLabel"] = sm["label"]          # Hot | Neutral | Cool

    # --- (2) Insider (SEC EDGAR Form 4) -> composite sub-score (15%) ---
    if _HAVE_INSIDER:
        try:
            activity = get_insider_activity(ticker, lookback_days=90, verbose=False)
            score = insider_score(activity, market_cap=info.get("marketCap"))
            if score is not None:
                m["insiderScore"] = round(float(score), 1)
                if activity and activity.get("net_signal"):
                    m["insiderSignal"] = activity["net_signal"]
        except Exception as e:
            print(f"  [{ticker}] insider skipped: {e}")

    # --- (3) News sentiment -> verdict modifier ±6 ---
    try:
        news = score_news_sentiment(ticker, info.get("shortName") or info.get("longName"))
        if news and news.get("score") is not None:
            m["newsScore"] = round(float(news["score"]), 3)
            if news.get("label"):
                m["newsLabel"] = news["label"]
            if news.get("rationale"):
                m["newsRationale"] = news["rationale"][:160]
    except Exception as e:
        print(f"  [{ticker}] news skipped: {e}")

    return {k: v for k, v in m.items() if v is not None}


def build_company(ticker: str) -> Optional[dict]:
    print(f"[{ticker}] fetching…")
    tkr = yf.Ticker(ticker)
    info = tkr.info or {}
    if not info:
        print(f"  [{ticker}] no info — skipped")
        return None

    annuals = _annuals(tkr, info)
    if len(annuals) < 2:
        print(f"  [{ticker}] only {len(annuals)} usable years — skipped")
        return None

    name = info.get("longName") or info.get("shortName") or ticker
    name += DISPLAY_NAME_SUFFIX.get(ticker, "")
    company = {
        "ticker": ticker,
        "name": name,
        "currency": info.get("financialCurrency") or info.get("currency") or "USD",
        "isADR": ticker in ADR_TICKERS,
        "annuals": annuals,
        "market": _market(ticker, info),
    }
    print(f"  [{ticker}] {len(annuals)} yrs · "
          f"sector={company['market'].get('sectorLabel', '—')} · "
          f"insider={company['market'].get('insiderScore', '—')} · "
          f"news={company['market'].get('newsScore', '—')}")
    return company


def _flush(companies: list[dict]) -> None:
    """Write progress to disk. Called after each success so a long or
    rate-limited run still leaves a usable file (sorted by ticker for stable
    diffs)."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(companies, key=lambda c: c["ticker"])
    OUTPUT_PATH.write_text(json.dumps(ordered, indent=2))


def main() -> None:
    tickers = sys.argv[1:] or DEFAULT_TICKERS
    companies: list[dict] = []
    total = len(tickers)
    for i, t in enumerate(tickers, 1):
        try:
            c = build_company(t)
            if c:
                companies.append(c)
                _flush(companies)  # incremental — survive interruption/throttle
        except Exception as e:
            print(f"  [{t}] failed: {e}")
        if i % 10 == 0 or i == total:
            print(f"  … {i}/{total} processed, {len(companies)} written")

    if not companies:
        print("No companies built — nothing written.")
        sys.exit(1)

    print(f"\nWrote {len(companies)} companies -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
