"""
insider_trading.py
------------------
Aggregates recent insider buy/sell activity for a ticker.

Sources (each optional, tried in order):
  1. SEC EDGAR — official Form 4 filings (free, no API key)
  2. Finnhub   — `/stock/insider-transactions` endpoint (free with key)
  3. yfinance  — `Ticker.insider_transactions` (best-effort, schema varies)

Output dict:
    {
        "buy_count":   int,
        "sell_count":  int,
        "buy_value":   float,   # USD value of buys in window
        "sell_value":  float,
        "net_value":   float,   # buy_value - sell_value
        "net_signal":  "Buying" | "Selling" | "Neutral",
        "lookback_days": int,
        "source": "sec" | "finnhub" | "yahoo",
    }

Cached by ticker so repeated lookups across watchlist + holdings are free.
"""

from __future__ import annotations

import json
import os
import sys
import socket
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    import requests
except ImportError:
    requests = None


def _default_sec_ua() -> str:
    """
    Build a sensible default SEC User-Agent.

    SEC's fair-use policy requires a UA with a contact (email/URL). They reject
    the literal 'example.com' placeholder but accept any reasonable identifier
    with an @ sign. We construct one from the machine hostname so it's unique
    per user without requiring configuration.

    Users can override by setting SEC_USER_AGENT in their .env.
    """
    user_ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if user_ua and "example.com" not in user_ua.lower() \
            and "your_email" not in user_ua.lower():
        return user_ua
    # Auto-construct a reasonable default
    try:
        host = socket.gethostname().replace(".local", "")[:30]
    except Exception:
        host = "user"
    return f"Portfolio-Analyzer/1.0 admin@{host}.portfolio-analyzer.local"


_SEC_UA = _default_sec_ua()

# SEC EDGAR fair-use limit is 10 requests/second per user agent. With
# parallel ticker analysis, uncoordinated workers burst past that and get
# HTTP 429s (silently degrading insider data). All SEC requests go through
# _sec_get(), which enforces a global minimum spacing across threads and
# retries once on 429.
_SEC_MIN_INTERVAL_S = 0.13   # ~7.5 req/s, comfortably under the limit
_sec_gate = threading.Lock()
_sec_last_request = 0.0


def _sec_get(url: str, timeout: int = 10) -> "requests.Response":
    global _sec_last_request
    for attempt in (1, 2):
        with _sec_gate:
            wait = _SEC_MIN_INTERVAL_S - (time.time() - _sec_last_request)
            if wait > 0:
                time.sleep(wait)
            _sec_last_request = time.time()
        resp = requests.get(url, headers={"User-Agent": _SEC_UA}, timeout=timeout)
        if resp.status_code != 429 or attempt == 2:
            return resp
        time.sleep(1.0)  # back off once, then return whatever we get
    return resp


_INSIDER_CACHE: dict[str, Optional[dict]] = {}

# Scanning a company's recent Form 4s costs ~2 gated SEC requests per filing
# (mega-caps have 25-45 in a 90-day window — 10+ seconds each). The
# underlying filings change at most a few times a day, so results are
# persisted to disk with a TTL: only the first run of the day pays the
# scan cost. Delete .cache/insider.json (or set INSIDER_CACHE_TTL_HOURS=0)
# to force a fresh scan.
_INSIDER_DISK_PATH = Path(__file__).resolve().parent / ".cache" / "insider.json"
try:
    _INSIDER_TTL_S = float(os.environ.get("INSIDER_CACHE_TTL_HOURS", "12")) * 3600
except ValueError:
    _INSIDER_TTL_S = 12 * 3600
_insider_disk_lock = threading.Lock()
_insider_disk: Optional[dict] = None


def _insider_disk_load() -> dict:
    global _insider_disk
    if _insider_disk is None:
        try:
            _insider_disk = json.loads(_INSIDER_DISK_PATH.read_text())
        except Exception:
            _insider_disk = {}
    return _insider_disk


def _insider_disk_get(key: str) -> Optional[dict]:
    with _insider_disk_lock:
        entry = _insider_disk_load().get(key)
    if not entry or time.time() - entry.get("ts", 0) > _INSIDER_TTL_S:
        return None
    return entry.get("result")


def _insider_disk_put(key: str, result: dict) -> None:
    with _insider_disk_lock:
        cache = _insider_disk_load()
        cache[key] = {"ts": time.time(), "result": result}
        try:
            _INSIDER_DISK_PATH.parent.mkdir(exist_ok=True)
            _INSIDER_DISK_PATH.write_text(json.dumps(cache))
        except Exception:
            pass  # cache is an optimization — never fail the run over it


# How many days of insider activity to consider "recent"
DEFAULT_LOOKBACK_DAYS = 90


def get_insider_activity(
    ticker: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    verbose: bool = True,
) -> Optional[dict]:
    """
    Return aggregated insider activity for the past `lookback_days`, or None.

    Sources (in order of reliability for transaction-code accuracy):
      1. SEC EDGAR  — canonical Form 4 data with exact transaction codes
      2. Finnhub    — requires FINNHUB_API_KEY; respects transactionCode field

    yfinance is intentionally NOT used: Yahoo's insider data routinely
    conflates tax-withholding (F-code) with discretionary sales (S-code),
    producing false "Selling" signals for mega-caps. Better to show "—"
    honestly than to mislead.
    """
    cache_key = f"{ticker}:{lookback_days}"
    if cache_key in _INSIDER_CACHE:
        return _INSIDER_CACHE[cache_key]

    disk_hit = _insider_disk_get(cache_key)
    if disk_hit is not None:
        if verbose:
            print(f"[insider:{ticker}] {disk_hit.get('net_signal', '?')} "
                  f"(cached, source={disk_hit.get('source', '?')})")
        _INSIDER_CACHE[cache_key] = disk_hit
        return disk_hit

    cutoff = date.today() - timedelta(days=lookback_days)
    result = None
    errors: list[str] = []

    # 1. SEC EDGAR — primary
    try:
        result = _fetch_sec_insider(ticker, cutoff, verbose=verbose)
        if result is None and verbose:
            errors.append("sec: no Form 4 buys/sells in window")
    except Exception as e:
        errors.append(f"sec: {e}")
        if verbose:
            print(f"[insider:{ticker}] SEC error: {e}")

    # 2. Finnhub if available
    if result is None and os.environ.get("FINNHUB_API_KEY"):
        try:
            result = _fetch_finnhub_insider(ticker, cutoff, verbose=verbose)
            if result is None and verbose:
                errors.append("finnhub: no data")
        except Exception as e:
            errors.append(f"finnhub: {e}")
            if verbose:
                print(f"[insider:{ticker}] Finnhub error: {e}")

    if result is not None:
        result["lookback_days"] = lookback_days
        net = result.get("net_value", 0)
        bc = result.get("buy_count", 0)
        sc = result.get("sell_count", 0)
        sv = result.get("sell_value", 0.0)
        tw = result.get("tax_withhold_value", 0)
        other = result.get("other_activity_count", 0)
        plan_value = result.get("plan_value", 0.0)
        discretionary_sv = result.get("discretionary_sell_value",
                                       max(sv - plan_value, 0.0))

        # What fraction of selling was 10b5-1 scheduled?
        plan_ratio = (plan_value / sv) if sv > 0 else 0.0

        # Classify net signal (richer, plan-aware)
        if net > 50_000 and bc >= 2:
            result["net_signal"] = "Buying"
        elif sc >= 2 and discretionary_sv >= 250_000:
            # Real discretionary selling above threshold
            result["net_signal"] = "Selling"
        elif sc >= 2 and plan_ratio >= 0.7 and sv >= 250_000:
            # Most of the selling was 10b5-1 scheduled — distinct label
            result["net_signal"] = "Scheduled selling"
        elif tw >= 10_000_000:
            # Heavy tax-withholding = executives cashing out vesting RSUs.
            result["net_signal"] = "Cashing out"
        elif bc == 0 and sc == 0 and other > 0:
            # Active but no open-market trades — compensation/exercises only
            result["net_signal"] = "Compensation"
        else:
            result["net_signal"] = "Neutral"

        if verbose:
            extras = []
            if tw > 0:
                extras.append(f"${tw/1e6:.1f}M tax-withhold")
            if plan_value > 0:
                extras.append(f"${plan_value/1e6:.1f}M 10b5-1 scheduled")
            if other > 0:
                extras.append(f"{other} grants/exercises")
            tail = f" [{', '.join(extras)}]" if extras else ""
            print(f"[insider:{ticker}] {result['net_signal']} "
                  f"({bc}B/{sc}S, net ${net:,.0f}, source={result['source']}){tail}")
    elif verbose:
        print(f"[insider:{ticker}] no data ({'; '.join(errors)})")

    _INSIDER_CACHE[cache_key] = result
    if result is not None:
        # Persist only real results: a None from a transient SEC outage
        # shouldn't suppress retries for the whole TTL window.
        _insider_disk_put(cache_key, result)
    return result


def _has_real_sec_ua() -> bool:
    """Kept for backward compatibility with --debug-insider flag."""
    return True  # _SEC_UA is now always set to a sensible default


# Share-class twins (GOOG/GOOGL) resolve to the same CIK and would scan the
# same filings twice. Scans are cached per (CIK, cutoff); a per-key lock
# makes a concurrent worker for the twin wait for the first scan instead of
# duplicating ~50 gated SEC requests.
_SEC_SCAN_CACHE: dict[str, Optional[dict]] = {}
_SEC_SCAN_LOCKS: dict[str, threading.Lock] = {}
_sec_scan_registry_lock = threading.Lock()


def _fetch_sec_insider(ticker: str, cutoff: date,
                       verbose: bool = False) -> Optional[dict]:
    """Pull recent Form 4 filings and aggregate ALL non-derivative activity.

    Returns activity dict with separate counts for discretionary (P/S) vs
    compensation/tax-withholding activity (A/F/M/G/...).
    """
    if requests is None:
        return None

    cik = _resolve_cik(ticker)
    if not cik:
        if verbose:
            print(f"[insider:{ticker}] SEC: no CIK found")
        return None

    scan_key = f"{cik}:{cutoff.isoformat()}"
    with _sec_scan_registry_lock:
        scan_lock = _SEC_SCAN_LOCKS.setdefault(scan_key, threading.Lock())
    with scan_lock:
        if scan_key in _SEC_SCAN_CACHE:
            if verbose:
                print(f"[insider:{ticker}] SEC: reusing scan for CIK {cik}")
        else:
            _SEC_SCAN_CACHE[scan_key] = _scan_sec_form4s(
                ticker, cik, cutoff, verbose=verbose)
        result = _SEC_SCAN_CACHE[scan_key]
    # Copy so per-ticker post-processing never mutates the shared entry
    return dict(result) if result is not None else None


def _scan_sec_form4s(ticker: str, cik: int, cutoff: date,
                     verbose: bool = False) -> Optional[dict]:
    """The actual EDGAR crawl: submissions index + every Form 4 in window."""
    url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    try:
        resp = _sec_get(url, timeout=15)
        if verbose:
            print(f"[insider:{ticker}] SEC submissions: HTTP {resp.status_code} "
                  f"(CIK {cik})")
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception as e:
        if verbose:
            print(f"[insider:{ticker}] SEC submissions error: {e}")
        return None

    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accessions = recent.get("accessionNumber") or []

    # Aggregate by transaction code across all Form 4s in window
    form4_count = 0
    code_totals: dict[str, dict] = {}  # code -> {"count": N, "value": $}

    # Track plan-scheduled S-code value separately from discretionary S-code value
    total_plan_value = 0.0
    plan_filings = 0

    for form, fdate, acc in zip(forms, dates, accessions):
        if form != "4":
            continue
        try:
            f_d = datetime.strptime(fdate, "%Y-%m-%d").date()
        except ValueError:
            continue
        if f_d < cutoff:
            break  # filings are reverse-chrono
        form4_count += 1
        # No politeness sleep needed here — every request already goes
        # through _sec_get()'s global rate gate.
        parsed = _parse_form4(cik, acc, verbose=verbose)
        if not parsed:
            continue
        for code, value in (parsed.get("by_code") or {}).items():
            slot = code_totals.setdefault(code, {"count": 0, "value": 0.0})
            slot["count"] += 1
            slot["value"] += value
        # Roll up 10b5-1 plan attribution
        if parsed.get("is_10b5_1"):
            plan_filings += 1
            total_plan_value += parsed.get("plan_value", 0.0)

    # Buy = P codes only (genuine discretionary buys)
    buy_count = code_totals.get("P", {}).get("count", 0)
    buy_value = code_totals.get("P", {}).get("value", 0.0)
    # Sell = S codes (open-market sells, BUT a portion may be 10b5-1 scheduled)
    sell_count = code_totals.get("S", {}).get("count", 0)
    sell_value = code_totals.get("S", {}).get("value", 0.0)
    # Of those S-code sells, how much was preset 10b5-1 (low signal)?
    plan_value = min(total_plan_value, sell_value)  # cap at total S value
    discretionary_sell_value = max(sell_value - plan_value, 0.0)
    # F = sell-to-cover for taxes (tracked separately — not discretionary,
    # but heavy F activity indicates lots of RSUs being immediately cashed out)
    f_count = code_totals.get("F", {}).get("count", 0)
    f_value = code_totals.get("F", {}).get("value", 0.0)
    # Other activity: A=grants, M=option exercise, G=gift, D, U, etc.
    other_count = sum(
        v["count"] for k, v in code_totals.items()
        if k not in ("P", "S", "F")
    )

    if verbose:
        codes_str = ", ".join(
            f"{k}:{v['count']}" for k, v in sorted(code_totals.items())
        ) or "none"
        plan_str = (f" ({plan_filings} 10b5-1 plan filings, "
                    f"${plan_value/1e6:.1f}M scheduled)") if plan_filings else ""
        print(f"[insider:{ticker}] SEC: scanned {form4_count} Form 4s "
              f"(codes: {codes_str}){plan_str}")

    if form4_count == 0:
        return None

    return {
        "buy_count": buy_count, "sell_count": sell_count,
        "buy_value": round(buy_value, 2),
        "sell_value": round(sell_value, 2),
        "net_value": round(buy_value - sell_value, 2),
        # 10b5-1 detail
        "plan_value": round(plan_value, 2),                  # scheduled portion
        "discretionary_sell_value": round(discretionary_sell_value, 2),
        "plan_filings": plan_filings,
        # Extra fields for richer display
        "tax_withhold_count": f_count,
        "tax_withhold_value": round(f_value, 2),
        "other_activity_count": other_count,
        "total_filings": form4_count,
        "source": "sec",
    }


_CIK_CACHE: dict[str, int] = {}


def _resolve_cik(ticker: str) -> Optional[int]:
    """Resolve a stock ticker to its SEC CIK number."""
    global _CIK_CACHE
    if _CIK_CACHE:
        return _CIK_CACHE.get(ticker.upper())
    if requests is None:
        return None
    try:
        url = "https://www.sec.gov/files/company_tickers.json"
        resp = _sec_get(url, timeout=20)
        if resp.status_code != 200:
            return None
        data = resp.json()
        for entry in data.values():
            t = (entry.get("ticker") or "").upper()
            if t:
                _CIK_CACHE[t] = int(entry["cik_str"])
        return _CIK_CACHE.get(ticker.upper())
    except Exception:
        return None


def _aggregate_form4_transactions(xml: str) -> Optional[dict]:
    """Parse Form 4 XML and aggregate non-derivative transactions by code.

    Also detects whether the filing is pursuant to a Rule 10b5-1 trading plan
    (preset sales) — these carry much weaker signal than discretionary trades.

    Returns:
        {"by_code": {"P": $val, "S": $val, ...},
         "is_10b5_1": bool,        # whole filing is under a plan
         "plan_value": float}      # $ amount specifically marked 10b5-1
    """
    if not xml or "<" not in xml:
        return None
    try:
        from xml.etree import ElementTree as ET
    except ImportError:
        return None

    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError:
        try:
            cleaned = xml.lstrip("\ufeff \t\r\n")
            root = ET.fromstring(cleaned)
        except (ET.ParseError, Exception):
            return None
    except Exception:
        return None

    def _local(elem) -> str:
        tag = elem.tag
        if isinstance(tag, str) and "}" in tag:
            return tag.split("}", 1)[1]
        return tag if isinstance(tag, str) else ""

    def _find_local(parent, name: str):
        for el in parent.iter():
            if _local(el) == name:
                return el
        return None

    def _findall_local(parent, name: str) -> list:
        return [el for el in parent.iter() if _local(el) == name]

    # Detect document-level 10b5-1 indicator. Form 4 has an <aff10b5One> field
    # that's "1" when the filing is pursuant to a plan, "0" otherwise.
    aff_el = _find_local(root, "aff10b5One")
    doc_is_10b5_1 = False
    if aff_el is not None and aff_el.text:
        try:
            doc_is_10b5_1 = aff_el.text.strip() in ("1", "true", "True")
        except Exception:
            doc_is_10b5_1 = False

    # Also scan footnotes for plan language — many filings use footnote text
    # like "This sale was effected pursuant to a Rule 10b5-1 trading plan
    # adopted by the reporting person on [date]."
    footnote_text = " ".join(
        (el.text or "") for el in _findall_local(root, "footnote")
    ).lower()
    has_plan_footnote = (
        "10b5-1" in footnote_text or "10b5-1" in footnote_text.replace("-", "")
        or "trading plan" in footnote_text
    )

    txns = _findall_local(root, "nonDerivativeTransaction")
    if not txns:
        return None

    code_values: dict[str, float] = {}
    plan_value = 0.0  # $ amount of transactions specifically tied to a plan

    for txn in txns:
        try:
            code_el = _find_local(txn, "transactionCode")
            shares_el = _find_local(txn, "transactionShares")
            price_el = _find_local(txn, "transactionPricePerShare")
            if code_el is None or shares_el is None:
                continue

            shares_val = _find_local(shares_el, "value")
            shares_text = (shares_val.text if shares_val is not None
                           else shares_el.text)
            if not shares_text:
                continue
            shares = float(shares_text.strip())

            price = 0.0
            if price_el is not None:
                price_val = _find_local(price_el, "value")
                price_text = (price_val.text if price_val is not None
                              else price_el.text)
                if price_text and price_text.strip():
                    try:
                        price = float(price_text.strip())
                    except ValueError:
                        price = 0.0

            code_t = (code_el.text or "").strip().upper() or "?"
            value = shares * price
            code_values[code_t] = code_values.get(code_t, 0.0) + value

            # If filing is under 10b5-1 plan, attribute the sale value to plan
            if code_t == "S" and (doc_is_10b5_1 or has_plan_footnote):
                plan_value += value
        except (ValueError, AttributeError, TypeError):
            continue

    if not code_values:
        return None
    return {
        "by_code": code_values,
        "is_10b5_1": doc_is_10b5_1 or has_plan_footnote,
        "plan_value": plan_value,
    }


def _parse_form4(cik: int, accession: str,
                 verbose: bool = False) -> Optional[dict]:
    """
    Pull and parse a single Form 4 filing.

    Returns: {"by_code": {"P": 1234.0, "S": 0.0, "F": 5000.0, ...}} or None.

    Strategy:
      1. Use SEC's structured `index.json` endpoint to enumerate files in the
         filing — far more reliable than scraping HTML
      2. Score candidate .xml files and pick the most likely primary_doc
      3. If no .xml found, try the well-known fallback path primary_doc.xml
    """
    if requests is None:
        return None
    acc_clean = accession.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_clean}"

    candidate_urls: list[str] = []

    # --- Step 1: enumerate files via structured JSON endpoint ---
    try:
        idx_json = f"{base}/index.json"
        resp = _sec_get(idx_json, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            items = (data.get("directory") or {}).get("item") or []
            xml_files = [it.get("name", "") for it in items
                         if it.get("name", "").lower().endswith(".xml")]
            # Score: penalize obvious non-Form-4 files
            def _score(name: str) -> int:
                n = name.lower()
                if "filingsummary" in n: return -100  # never the primary doc
                if "metadata" in n: return -50
                if "primary_doc" in n: return 100     # most reliable indicator
                if "form4" in n or "form_4" in n: return 80
                if "ownership" in n: return 60
                if "doc4" in n: return 70
                if n.endswith(".xml"): return 10
                return 0
            xml_files.sort(key=_score, reverse=True)
            for name in xml_files:
                candidate_urls.append(f"{base}/{name}")
        elif verbose:
            print(f"[insider] {accession} index.json HTTP {resp.status_code}")
    except Exception as e:
        if verbose:
            print(f"[insider] {accession} index.json error: {e}")

    # --- Step 2: fallback to well-known primary_doc.xml path ---
    if not candidate_urls:
        candidate_urls.append(f"{base}/primary_doc.xml")

    # --- Step 3: try each candidate until one parses successfully ---
    for url in candidate_urls:
        try:
            r = _sec_get(url, timeout=10)
            if r.status_code != 200:
                continue
            parsed = _aggregate_form4_transactions(r.text)
            if parsed:
                return parsed
            if verbose:
                # Sample the first 200 chars so we can diagnose what we got
                preview = r.text[:200].replace("\n", " ")
                print(f"[insider] {accession} {url.rsplit('/',1)[-1]} "
                      f"({len(r.text)}b) → 0 codes. Preview: {preview!r}")
        except Exception as e:
            if verbose:
                print(f"[insider] {accession} candidate error: {e}")

    return None


def _fetch_finnhub_insider(ticker: str, cutoff: date,
                            verbose: bool = False) -> Optional[dict]:
    """Pull insider transactions from Finnhub."""
    if requests is None:
        return None
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        return None
    try:
        url = "https://finnhub.io/api/v1/stock/insider-transactions"
        resp = requests.get(
            url,
            params={
                "symbol": ticker, "token": key,
                "from": cutoff.isoformat(),
                "to": date.today().isoformat(),
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        data = resp.json() or {}
    except Exception:
        return None

    rows = data.get("data") or []
    code_totals: dict[str, dict] = {}
    total_filings = 0

    for r in rows:
        try:
            change = float(r.get("change") or 0)
            price = float(r.get("transactionPrice") or 0)
            tc = (r.get("transactionCode") or "").strip().upper()
            if not tc:
                # Finnhub sometimes omits code — infer from sign as last resort
                tc = "P" if change > 0 else ("S" if change < 0 else "?")
        except (TypeError, ValueError):
            continue
        if change == 0:
            continue
        total_filings += 1
        value = abs(change) * price
        slot = code_totals.setdefault(tc, {"count": 0, "value": 0.0})
        slot["count"] += 1
        slot["value"] += value

    # Categorize same way SEC fetcher does
    buy_count = code_totals.get("P", {}).get("count", 0)
    buy_value = code_totals.get("P", {}).get("value", 0.0)
    sell_count = code_totals.get("S", {}).get("count", 0)
    sell_value = code_totals.get("S", {}).get("value", 0.0)
    f_count = code_totals.get("F", {}).get("count", 0)
    f_value = code_totals.get("F", {}).get("value", 0.0)
    other_count = sum(
        v["count"] for k, v in code_totals.items()
        if k not in ("P", "S", "F")
    )

    if total_filings == 0:
        return None
    return {
        "buy_count": buy_count, "sell_count": sell_count,
        "buy_value": round(buy_value, 2),
        "sell_value": round(sell_value, 2),
        "net_value": round(buy_value - sell_value, 2),
        "tax_withhold_count": f_count,
        "tax_withhold_value": round(f_value, 2),
        "other_activity_count": other_count,
        "total_filings": total_filings,
        "source": "finnhub",
    }


def _fetch_yfinance_insider(ticker: str, cutoff: date,
                             verbose: bool = False) -> Optional[dict]:
    """
    Best-effort fallback using yfinance.

    yfinance exposes insider data via several properties depending on version:
      - .insider_transactions    (older)
      - .insider_purchases        (newer; aggregate over recent period)
      - .insider_roster_holders   (current holdings, not transactions)

    We try them in order and parse whatever shape we get.
    """
    try:
        import yfinance as yf
    except ImportError:
        return None

    try:
        tkr = yf.Ticker(ticker)
    except Exception:
        return None

    # Strategy A: .insider_transactions (most detailed when available)
    try:
        df = tkr.insider_transactions
        if df is not None and not df.empty:
            result = _parse_yf_transactions_df(df, cutoff, verbose=verbose, ticker=ticker)
            if result:
                return result
    except Exception as e:
        if verbose:
            print(f"[insider:{ticker}] yf.insider_transactions failed: {e}")

    # Strategy B: .insider_purchases (newer aggregate view)
    try:
        df = tkr.insider_purchases
        if df is not None and not df.empty:
            result = _parse_yf_purchases_df(df, verbose=verbose, ticker=ticker)
            if result:
                return result
    except Exception as e:
        if verbose:
            print(f"[insider:{ticker}] yf.insider_purchases failed: {e}")

    return None


def _parse_yf_transactions_df(df, cutoff: date, verbose: bool,
                               ticker: str) -> Optional[dict]:
    """Parse the row-per-transaction shape from yf.insider_transactions."""
    # Normalize column access (yfinance varies between "Date" / "Start Date")
    cols = {str(c).lower(): c for c in df.columns}
    date_col = cols.get("date") or cols.get("start date") or cols.get("startdate")
    txn_col = (cols.get("transaction") or cols.get("text")
               or cols.get("type"))
    value_col = cols.get("value") or cols.get("ownership value")
    shares_col = cols.get("shares") or cols.get("# of shares")

    if date_col is None or txn_col is None:
        if verbose:
            print(f"[insider:{ticker}] yfinance df missing expected columns "
                  f"(got {list(df.columns)})")
        return None

    code_totals: dict[str, dict] = {}
    total_filings = 0

    for _, row in df.iterrows():
        try:
            d = row[date_col]
            if hasattr(d, "date"):
                d = d.date()
            elif isinstance(d, str):
                d = datetime.strptime(d[:10], "%Y-%m-%d").date()
            else:
                continue
            if d < cutoff:
                continue
        except Exception:
            continue

        txn_raw = str(row[txn_col] or "").strip()
        val = 0.0
        if value_col is not None and row[value_col] is not None:
            try:
                val = float(row[value_col])
            except (TypeError, ValueError):
                val = 0.0

        # yfinance Transaction column can be:
        #   "P - Purchase"  / "Purchase"
        #   "S - Sale"      / "Sale"
        #   "F - Tax/Cover" / "Sale - tax payment"  (sell-to-cover)
        #   "A - Grant"     / "Conversion of derivatives"
        #   "M - Exercise"  / "Option exercise"
        # Map to Form-4-style codes.
        lower = txn_raw.lower()
        if lower.startswith("p ") or "purchase" in lower:
            code = "P"
        elif "tax" in lower or lower.startswith("f "):
            code = "F"
        elif lower.startswith("s ") or "sale" in lower or "sell" in lower:
            code = "S"
        elif "grant" in lower or "award" in lower or lower.startswith("a "):
            code = "A"
        elif "exercise" in lower or lower.startswith("m "):
            code = "M"
        elif "gift" in lower or lower.startswith("g "):
            code = "G"
        else:
            code = "?"

        total_filings += 1
        slot = code_totals.setdefault(code, {"count": 0, "value": 0.0})
        slot["count"] += 1
        slot["value"] += abs(val)

    if total_filings == 0:
        return None

    buy_count = code_totals.get("P", {}).get("count", 0)
    buy_value = code_totals.get("P", {}).get("value", 0.0)
    sell_count = code_totals.get("S", {}).get("count", 0)
    sell_value = code_totals.get("S", {}).get("value", 0.0)
    f_count = code_totals.get("F", {}).get("count", 0)
    f_value = code_totals.get("F", {}).get("value", 0.0)
    other_count = sum(
        v["count"] for k, v in code_totals.items()
        if k not in ("P", "S", "F")
    )

    return {
        "buy_count": buy_count, "sell_count": sell_count,
        "buy_value": round(buy_value, 2),
        "sell_value": round(sell_value, 2),
        "net_value": round(buy_value - sell_value, 2),
        "tax_withhold_count": f_count,
        "tax_withhold_value": round(f_value, 2),
        "other_activity_count": other_count,
        "total_filings": total_filings,
        "source": "yahoo",
    }


def _parse_yf_purchases_df(df, verbose: bool,
                            ticker: str) -> Optional[dict]:
    """
    Parse the aggregated 6-month view from yf.insider_purchases.

    Shape (rows are categories, single value column):
        "Purchases"                       shares=X  total=Y  (transactions=N)
        "Sales"                           shares=X  total=Y  (transactions=N)
        "Net Shares Purchased (Sold)"     shares=X
        "Total Insider Shares Held"
        "% Net Shares Purchased (Sold)"
        "% Buy Shares"
        "% Sell Shares"
    """
    try:
        rows = {str(idx).lower(): row for idx, row in df.iterrows()}
    except Exception:
        return None

    def _first_numeric(row) -> Optional[float]:
        for v in row.values:
            try:
                if v is None:
                    continue
                n = float(v)
                if n != 0:
                    return n
            except (TypeError, ValueError):
                continue
        return None

    purchases = rows.get("purchases")
    sales = rows.get("sales")
    if purchases is None and sales is None:
        return None

    # Extract: shares + total value + transaction count from each row
    def _extract(row):
        # row typically has columns: ["Shares", "Trans"] OR ["Total"]
        # We need: trans_count, total_value
        if row is None:
            return 0, 0.0
        try:
            cols = list(row.index.astype(str).str.lower())
        except Exception:
            return 0, 0.0
        trans_count = 0
        total_value = 0.0
        for c, v in zip(cols, row.values):
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if "trans" in c:
                trans_count = int(fv)
            elif "total" in c or "value" in c:
                total_value = fv
        return trans_count, total_value

    bc, bv = _extract(purchases)
    sc, sv = _extract(sales)

    if bc == 0 and sc == 0:
        return None
    return {
        "buy_count": bc, "sell_count": sc,
        "buy_value": round(bv, 2),
        "sell_value": round(sv, 2),
        "net_value": round(bv - sv, 2),
        "source": "yahoo",
    }


def insider_score(activity: Optional[dict],
                  market_cap: Optional[float] = None) -> Optional[float]:
    """
    Convert insider activity into a 0-100 sub-score for the Buy Score blend.

    Two adjustments make this fair across company sizes and trading styles:

    1. Size-aware thresholds. A $5M open-market buy is meaningful at a $1B
       small-cap (0.5% of cap) but invisible at a $4T mega-cap (0.0001%).
       When market_cap is known, we score selling/buying intensity as a
       percentage of market cap rather than absolute dollars. Without market
       cap we fall back to absolute-dollar thresholds.

    2. 10b5-1 plan discount. Sales filed under a Rule 10b5-1 trading plan are
       preset months in advance and don't reflect insiders' current view.
       We weight the discretionary portion of S-code sales at 4x the
       scheduled portion. Only the discretionary slice drives "Selling".

    Returns 0-100, or None if no Form 4 activity at all.
    """
    if activity is None:
        return None
    bc = activity.get("buy_count", 0)
    sc = activity.get("sell_count", 0)
    sv = activity.get("sell_value", 0.0)
    bv = activity.get("buy_value", 0.0)
    tw = activity.get("tax_withhold_value", 0.0)
    other = activity.get("other_activity_count", 0)
    plan_value = activity.get("plan_value", 0.0)
    discretionary_sell = activity.get(
        "discretionary_sell_value", max(sv - plan_value, 0.0)
    )

    # ----- 10b5-1-weighted "effective" sell value -----
    # Plan sales count 1/4 as much as discretionary sales for signal purposes.
    # The 4x weighting is a heuristic — academic research generally treats
    # 10b5-1 trades as carrying ~25% the predictive power of discretionary.
    effective_sell = discretionary_sell + plan_value * 0.25
    effective_net = bv - effective_sell

    # ----- Helper: convert $ to "% of market cap" if known -----
    def _pct(dollars: float) -> Optional[float]:
        if market_cap and market_cap > 0:
            return (dollars / market_cap) * 100
        return None

    # Sells expressed as % of market cap (None if we don't know cap)
    sell_pct = _pct(effective_sell)
    buy_pct = _pct(bv)

    # ----- Buying scores (positive signal) -----
    if bc >= 2 and effective_net > 0:
        if sell_pct is not None:
            # Size-aware. >0.1% of cap in buying = very meaningful for any size
            if buy_pct >= 0.1 and bc >= 3:
                return 95
            if buy_pct >= 0.02 and bc >= 2:
                return 75
            if buy_pct >= 0.005:
                return 60
        else:
            # Absolute fallback (used for small/non-US companies)
            if effective_net >= 1_000_000 and bc >= 3:
                return 95
            if effective_net >= 200_000 and bc >= 2:
                return 75
            if effective_net >= 50_000:
                return 60
        return 55

    # ----- Selling scores (negative signal, but size-aware) -----
    if sc >= 2 and effective_sell > effective_net + 250_000:
        if sell_pct is not None:
            # Size-aware. NVDA selling $164M = 0.004% of cap → barely registers.
            # Same $164M at a $5B company = 3.3% → much more alarming.
            if sell_pct >= 1.0:
                return 10   # major exodus
            if sell_pct >= 0.5:
                return 20
            if sell_pct >= 0.2:
                return 30
            if sell_pct >= 0.1:
                return 40
            if sell_pct >= 0.05:
                return 45
            return 48       # tiny relative to cap — basically noise
        else:
            # Absolute fallback
            if effective_sell >= 10_000_000:
                return 15
            if effective_sell >= 1_000_000 and sc >= 3:
                return 25
            if effective_sell >= 200_000:
                return 40
            return 48

    # ----- Heavy tax-withholding (RSU cash-out, not discretionary) -----
    tw_pct = _pct(tw) if market_cap else None
    if tw_pct is not None and tw_pct >= 0.2:
        return 40
    if tw_pct is None and tw >= 10_000_000:
        return 40

    # ----- Compensation only / Neutral -----
    return 50
