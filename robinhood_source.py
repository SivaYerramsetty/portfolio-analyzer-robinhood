"""
robinhood_source.py
-------------------
Fetches live positions and analyst ratings directly from Robinhood via robin_stocks.

AUTHENTICATION SETUP — two options:

  OPTION A (recommended): create a `.env` file in your project root with:
      RH_USERNAME=your_email@example.com
      RH_PASSWORD=your_password
      RH_MFA_SECRET=base32secret      # optional, for unattended runs

  OPTION B: export as shell env vars in ~/.zshrc:
      export RH_USERNAME="your_email@example.com"
      export RH_PASSWORD="your_password"
      export RH_MFA_SECRET="..."      # optional

How MFA works:
  - First run prompts you for the 6-digit code from your authenticator app / SMS.
  - robin_stocks caches a session token at ~/.tokens/robinhood.pickle (~24h lifetime),
    so subsequent runs reuse it without re-MFA.
  - For truly hands-off automation (cron / scheduler), set RH_MFA_SECRET to the
    TOTP secret. The script will then generate codes itself.
    (Getting the TOTP secret: in Robinhood, when enabling Authenticator-app 2FA,
     it shows a QR plus a "Can't scan?" link revealing the base32 secret. Save it.)

SECURITY NOTES:
  - robin_stocks uses Robinhood's PRIVATE API. It works reliably but is not
    officially sanctioned by Robinhood's ToS. Use at your own risk.
  - Never commit credentials. Add `.env`, `*.pickle`, and `.tokens/` to .gitignore.
"""

import os
import sys
from pathlib import Path
from typing import Optional

# Auto-load .env from project root (the directory containing this file).
# Falls back silently if python-dotenv isn't installed.
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
        print(f"[robinhood] Loaded credentials from {_env_path}")
except ImportError:
    pass

try:
    import robin_stocks.robinhood as rh
except ImportError:
    rh = None

try:
    import pyotp
except ImportError:
    pyotp = None


# Cache for ratings (avoid duplicate fetches in one run)
_RATINGS_CACHE: dict[str, Optional[dict]] = {}


def _require_rh():
    if rh is None:
        raise ImportError(
            "robin_stocks not installed. Run: pip install robin-stocks"
        )


def login(verbose: bool = True) -> None:
    """Login to Robinhood. Strategy:

    1. If RH_MFA_CODE is set (workflow input or shell var): use it for fresh login.
    2. Else if RH_MFA_SECRET is set: generate TOTP code (legacy; unlikely to work with
       Robinhood's new device-approval auth, but kept for backward compatibility).
    3. Else: rely on cached session pickle at ~/.tokens/robinhood.pickle.
    4. If no cached session AND running non-interactively (CI): fail with clear message.
    """
    _require_rh()

    username = os.environ.get("RH_USERNAME")
    password = os.environ.get("RH_PASSWORD")
    mfa_code = os.environ.get("RH_MFA_CODE", "").strip()
    mfa_secret = os.environ.get("RH_MFA_SECRET", "").strip()

    if not username or not password:
        raise RuntimeError(
            "Missing credentials. Add them via ONE of these:\n\n"
            "  Option A — Create a `.env` file in your project folder with:\n"
            "      RH_USERNAME=your_email@example.com\n"
            "      RH_PASSWORD=your_password\n"
            "    Requires: pip install python-dotenv\n\n"
            "  Option B — Export in your shell (and reload with `source ~/.zshrc`):\n"
            "      export RH_USERNAME='you@example.com'\n"
            "      export RH_PASSWORD='your_password'\n\n"
            "  Option C — GitHub Actions: configure secrets RH_USERNAME and RH_PASSWORD\n"
        )

    # If MFA secret is configured but no explicit code provided, generate TOTP
    if not mfa_code and mfa_secret:
        if pyotp is None:
            raise ImportError(
                "RH_MFA_SECRET is set but pyotp not installed. "
                "Run: pip install pyotp"
            )
        mfa_code = pyotp.TOTP(mfa_secret).now()
        if verbose:
            print(f"[robinhood] Auto-generated TOTP code: {mfa_code}")

    # Detect if cached session exists at the default path
    cached_session = Path.home() / ".tokens" / "robinhood.pickle"
    has_cache = cached_session.exists()
    is_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))

    if verbose:
        msg = f"[robinhood] Logging in as {username}"
        if mfa_code:
            msg += " (with MFA code)"
        elif has_cache:
            msg += " (using cached session)"
        else:
            msg += " (no MFA, no cache)"
        print(msg + "...")

    try:
        rh.login(
            username=username,
            password=password,
            mfa_code=mfa_code or None,
            store_session=True,
        )
    except Exception as e:
        err = str(e)
        # Detect the most common failure: session expired and no MFA provided
        if is_ci and not mfa_code and ("MFA" in err or "verification" in err.lower()
                                       or "code" in err.lower()):
            print(
                "\n" + "=" * 60 + "\n"
                "❌ Robinhood session expired or invalid.\n"
                "=" * 60 + "\n"
                "To re-authenticate:\n"
                "  1. Open robinhood.com in a browser and try to log in.\n"
                "     Robinhood will text an SMS code to your phone.\n"
                "  2. Within ~5 minutes, go to GitHub Actions:\n"
                "     Actions tab → 'Portfolio Analysis' → 'Run workflow'\n"
                "  3. Paste the SMS code into the 'mfa_code' input field.\n"
                "  4. Click 'Run workflow' — fresh login + new cached session.\n"
                "=" * 60,
                flush=True,
            )
            sys.exit(2)
        raise

    if verbose:
        print("[robinhood] Authenticated.")


def logout(verbose: bool = False) -> None:
    """Logout — does NOT remove the cached token (so reruns still work)."""
    if rh is None:
        return
    try:
        rh.logout()
        if verbose:
            print("[robinhood] Logged out.")
    except Exception:
        pass


def fetch_positions() -> list[dict]:
    """
    Return current holdings as dicts matching the shape produced by parse_statement.py,
    PLUS extra fields available only via Robinhood (cost basis, P&L).

    Returned shape per position:
        {
            "ticker": "AAPL",
            "name": "Apple Inc.",
            "shares": 25.0,
            "price": 258.17,
            "market_value": 6454.25,
            "est_dividend": 0.0,
            "pct_portfolio": 12.50,
            "est_yield": 0.0,
            # Robinhood-only extras:
            "average_buy_price": 142.50,
            "equity_change": 2891.75,
            "percent_change": 81.18,
        }
    """
    _require_rh()
    holdings = rh.account.build_holdings()  # dict keyed by ticker
    positions: list[dict] = []

    # Fetch position open dates (created_at) for holding-period / tax analysis.
    # build_holdings() doesn't include dates, so we pull them separately and
    # map by ticker. This is POSITION-level (when the position first opened),
    # not per-lot — see tax_analysis.py for the caveat this implies.
    open_dates = _fetch_position_open_dates()

    for ticker, data in (holdings or {}).items():
        try:
            shares = float(data.get("quantity", 0) or 0)
            price = float(data.get("price", 0) or 0)
            equity = float(data.get("equity", 0) or 0)
            pct = float(data.get("percentage", 0) or 0)
            avg = float(data.get("average_buy_price", 0) or 0)
            equity_change = float(data.get("equity_change", 0) or 0)
            percent_change = float(data.get("percent_change", 0) or 0)

            if shares <= 0:
                continue  # skip closed positions

            positions.append({
                "ticker": ticker,
                "name": data.get("name", ticker),
                "shares": shares,
                "price": price,
                "market_value": equity,
                "est_dividend": 0.0,
                "pct_portfolio": pct,
                "est_yield": 0.0,
                "average_buy_price": avg,
                "equity_change": equity_change,
                "percent_change": percent_change,
                "position_opened": open_dates.get(ticker),  # ISO date str or None
            })
        except (TypeError, ValueError) as e:
            print(f"[robinhood] Skipping {ticker}: {e}")

    return positions


def _fetch_position_open_dates() -> dict[str, str]:
    """
    Map ticker -> earliest position open date (ISO 'YYYY-MM-DD').

    Uses get_open_stock_positions() which includes `created_at`. Resolves the
    instrument URL to a ticker symbol. Returns {} on any failure (graceful —
    the tax section just shows "unknown holding period" without dates).
    """
    out: dict[str, str] = {}
    try:
        raw = rh.account.get_open_stock_positions()
        if not raw:
            return out
        for pos in raw:
            try:
                created = pos.get("created_at") or pos.get("updated_at")
                if not created:
                    continue
                date_str = created[:10]  # 'YYYY-MM-DD' from ISO timestamp
                inst_url = pos.get("instrument")
                sym = None
                if inst_url:
                    try:
                        sym = rh.stocks.get_symbol_by_url(inst_url)
                    except Exception:
                        inst = rh.helper.request_get(inst_url)
                        sym = (inst or {}).get("symbol")
                if sym:
                    # Keep the EARLIEST date if ticker appears more than once
                    if sym not in out or date_str < out[sym]:
                        out[sym] = date_str
            except Exception:
                continue
    except Exception as e:
        print(f"[robinhood] Could not fetch position dates: {e}")
    return out


def fetch_tax_lots(verbose: bool = True) -> dict[str, list[dict]]:
    """
    Reconstruct OPEN tax lots per ticker from full order history using FIFO.

    Returns: {ticker: [{"date": "YYYY-MM-DD", "shares": float,
                        "price": float, "cost": float}, ...]}
    where each entry is a still-open lot (after FIFO-matching sells against buys).

    This gives EXACT short-term vs long-term treatment because every remaining
    lot keeps its own acquisition date — unlike position-level open dates.

    FIFO is the IRS default method. (Robinhood lets you specify lots at sale
    time; if you've done custom lot selection, this FIFO reconstruction may
    differ from your actual realized lots. It is exact for the common case of
    never having manually picked lots.)

    Returns {} on failure (caller falls back to position-level dates).
    """
    _require_rh()
    lots_by_ticker: dict[str, list[dict]] = {}

    try:
        if verbose:
            print("[tax-lots] Fetching full order history (may take a moment)...")
        orders = rh.orders.get_all_stock_orders()
    except Exception as e:
        print(f"[tax-lots] Could not fetch orders: {e}")
        return lots_by_ticker

    if not orders:
        return lots_by_ticker

    # Resolve instrument URLs to symbols (cache to avoid repeat calls)
    inst_cache: dict[str, str] = {}

    def resolve_symbol(order: dict) -> Optional[str]:
        # Newer responses sometimes include 'symbol' directly
        if order.get("symbol"):
            return order["symbol"]
        inst_url = order.get("instrument")
        if not inst_url:
            return None
        if inst_url in inst_cache:
            return inst_cache[inst_url]
        sym = None
        try:
            sym = rh.stocks.get_symbol_by_url(inst_url)
        except Exception:
            try:
                inst = rh.helper.request_get(inst_url)
                sym = (inst or {}).get("symbol")
            except Exception:
                sym = None
        if sym:
            inst_cache[inst_url] = sym
        return sym

    # Group filled buy/sell executions by ticker, each with date/shares/price
    # Structure: {ticker: [{"side","date","shares","price"}, ...]}
    txns: dict[str, list[dict]] = {}

    for order in orders:
        try:
            if order.get("state") != "filled":
                continue
            side = order.get("side")  # 'buy' or 'sell'
            if side not in ("buy", "sell"):
                continue
            sym = resolve_symbol(order)
            if not sym:
                continue

            # Each order can have multiple executions on different dates/prices
            executions = order.get("executions") or []
            if executions:
                for ex in executions:
                    qty = float(ex.get("quantity", 0) or 0)
                    price = float(ex.get("price", 0) or 0)
                    ts = ex.get("timestamp") or ex.get("settlement_date") \
                        or order.get("last_transaction_at")
                    if qty <= 0 or not ts:
                        continue
                    txns.setdefault(sym, []).append({
                        "side": side, "date": ts[:10],
                        "shares": qty, "price": price,
                    })
            else:
                # Fallback: use order-level aggregate
                qty = float(order.get("cumulative_quantity", 0) or 0)
                price = float(order.get("average_price", 0) or 0)
                ts = order.get("last_transaction_at") or order.get("created_at")
                if qty > 0 and ts:
                    txns.setdefault(sym, []).append({
                        "side": side, "date": ts[:10],
                        "shares": qty, "price": price,
                    })
        except (TypeError, ValueError):
            continue

    # Apply FIFO per ticker: sells consume oldest buy lots first
    for sym, events in txns.items():
        events.sort(key=lambda e: e["date"])  # chronological
        open_lots: list[dict] = []  # FIFO queue of buy lots
        for ev in events:
            if ev["side"] == "buy":
                open_lots.append({
                    "date": ev["date"],
                    "shares": ev["shares"],
                    "price": ev["price"],
                })
            else:  # sell — consume from front (oldest first)
                to_sell = ev["shares"]
                while to_sell > 1e-9 and open_lots:
                    lot = open_lots[0]
                    if lot["shares"] <= to_sell + 1e-9:
                        to_sell -= lot["shares"]
                        open_lots.pop(0)
                    else:
                        lot["shares"] -= to_sell
                        to_sell = 0
        # Finalize remaining open lots
        finalized = []
        for lot in open_lots:
            if lot["shares"] > 1e-6:
                finalized.append({
                    "date": lot["date"],
                    "shares": round(lot["shares"], 6),
                    "price": lot["price"],
                    "cost": round(lot["shares"] * lot["price"], 2),
                })
        if finalized:
            lots_by_ticker[sym] = finalized
            if verbose:
                print(f"[tax-lots]   {sym}: {len(finalized)} open lot(s)")

    if verbose:
        print(f"[tax-lots] Reconstructed lots for {len(lots_by_ticker)} ticker(s).")
    return lots_by_ticker


def fetch_realized_ytd(year: Optional[int] = None,
                       verbose: bool = True) -> dict:
    """
    Compute realized gains/losses for the current calendar year using FIFO.

    Reuses the same order-history reconstruction as fetch_tax_lots() but
    captures the SALE side instead of the open-lots side. For each sell
    transaction in the target year, matches it against open buy lots (FIFO)
    and records the resulting realized gain.

    Returns:
        {
          "year": 2026,
          "realized_gains": [   # one entry per (sale, consumed lot) match
            {"ticker": "AAPL", "sale_date": "2026-03-15",
             "buy_date": "2024-02-10", "shares": 50.0,
             "buy_price": 175.0, "sale_price": 220.0,
             "gain": 2250.0, "is_long_term": True}
          ],
          "lt_gains": float,   # sum of long-term realized gains (positive)
          "lt_losses": float,  # sum of long-term realized losses (positive number)
          "st_gains": float,
          "st_losses": float,
          "net_lt": float,     # lt_gains - lt_losses
          "net_st": float,
          "net_total": float,
        }
    Returns the empty shape on failure.

    Note: a "long-term" classification here means holding period > 365 days
    AT TIME OF SALE. Wash-sale rules are NOT modeled — if you sold at a loss
    and rebought within 30 days, the IRS would disallow the loss; this code
    will still show it as a realized loss. Treat the output as a planning
    estimate, not a tax filing.
    """
    from datetime import datetime
    _require_rh()

    if year is None:
        year = datetime.now().year

    empty = {
        "year": year, "realized_gains": [],
        "lt_gains": 0.0, "lt_losses": 0.0,
        "st_gains": 0.0, "st_losses": 0.0,
        "net_lt": 0.0, "net_st": 0.0, "net_total": 0.0,
    }

    try:
        if verbose:
            print(f"[realized-ytd] Computing realized gains for {year}...")
        orders = rh.orders.get_all_stock_orders()
    except Exception as e:
        print(f"[realized-ytd] Could not fetch orders: {e}")
        return empty

    if not orders:
        return empty

    # Resolve instrument URLs to symbols (cache to avoid repeat calls)
    inst_cache: dict[str, str] = {}

    def resolve_symbol(order: dict) -> Optional[str]:
        if order.get("symbol"):
            return order["symbol"]
        inst_url = order.get("instrument")
        if not inst_url:
            return None
        if inst_url in inst_cache:
            return inst_cache[inst_url]
        sym = None
        try:
            sym = rh.stocks.get_symbol_by_url(inst_url)
        except Exception:
            try:
                inst = rh.helper.request_get(inst_url)
                sym = (inst or {}).get("symbol")
            except Exception:
                sym = None
        if sym:
            inst_cache[inst_url] = sym
        return sym

    # Same grouping as fetch_tax_lots
    txns: dict[str, list[dict]] = {}
    for order in orders:
        try:
            if order.get("state") != "filled":
                continue
            side = order.get("side")
            if side not in ("buy", "sell"):
                continue
            sym = resolve_symbol(order)
            if not sym:
                continue
            ts = (order.get("last_transaction_at")
                  or order.get("updated_at")
                  or order.get("created_at"))
            if not ts:
                continue
            for ex in (order.get("executions") or []):
                try:
                    qty = float(ex.get("quantity") or 0)
                    price = float(ex.get("price") or 0)
                    if qty <= 0 or price <= 0:
                        continue
                    txns.setdefault(sym, []).append({
                        "side": side, "date": ts[:10],
                        "shares": qty, "price": price,
                    })
                except (TypeError, ValueError):
                    continue
        except (TypeError, ValueError):
            continue

    realized_gains: list[dict] = []

    # FIFO replay, capturing SELL-side matches
    for sym, events in txns.items():
        events.sort(key=lambda e: e["date"])
        open_lots: list[dict] = []
        for ev in events:
            if ev["side"] == "buy":
                open_lots.append({
                    "date": ev["date"],
                    "shares": ev["shares"],
                    "price": ev["price"],
                })
                continue
            # Sell: consume from front, record gain per match if in target year
            to_sell = ev["shares"]
            sale_date = ev["date"]
            sale_price = ev["price"]
            sale_year = int(sale_date[:4])
            while to_sell > 1e-9 and open_lots:
                lot = open_lots[0]
                consumed = min(lot["shares"], to_sell)
                if sale_year == year:
                    # Compute holding period
                    try:
                        d_buy = datetime.strptime(lot["date"], "%Y-%m-%d")
                        d_sell = datetime.strptime(sale_date, "%Y-%m-%d")
                        days_held = (d_sell - d_buy).days
                        is_lt = days_held > 365
                    except Exception:
                        days_held = None
                        is_lt = False
                    gain = consumed * (sale_price - lot["price"])
                    realized_gains.append({
                        "ticker": sym,
                        "sale_date": sale_date,
                        "buy_date": lot["date"],
                        "shares": round(consumed, 6),
                        "buy_price": lot["price"],
                        "sale_price": sale_price,
                        "gain": round(gain, 2),
                        "days_held": days_held,
                        "is_long_term": is_lt,
                    })
                lot["shares"] -= consumed
                to_sell -= consumed
                if lot["shares"] <= 1e-9:
                    open_lots.pop(0)

    # Aggregate
    lt_gains = sum(r["gain"] for r in realized_gains if r["is_long_term"] and r["gain"] > 0)
    lt_losses = -sum(r["gain"] for r in realized_gains if r["is_long_term"] and r["gain"] < 0)
    st_gains = sum(r["gain"] for r in realized_gains if not r["is_long_term"] and r["gain"] > 0)
    st_losses = -sum(r["gain"] for r in realized_gains if not r["is_long_term"] and r["gain"] < 0)

    if verbose:
        print(f"[realized-ytd] {len(realized_gains)} matched sale(s) in {year}: "
              f"ST net ${st_gains - st_losses:,.0f}, LT net ${lt_gains - lt_losses:,.0f}")

    return {
        "year": year,
        "realized_gains": realized_gains,
        "lt_gains": round(lt_gains, 2),
        "lt_losses": round(lt_losses, 2),
        "st_gains": round(st_gains, 2),
        "st_losses": round(st_losses, 2),
        "net_lt": round(lt_gains - lt_losses, 2),
        "net_st": round(st_gains - st_losses, 2),
        "net_total": round((lt_gains - lt_losses) + (st_gains - st_losses), 2),
    }


def fetch_robinhood_ratings(ticker: str) -> Optional[dict]:
    """
    Fetch Robinhood's aggregated analyst ratings for a ticker.

    Returns:
        {"buy": int, "hold": int, "sell": int, "total": int, "source": "robinhood"}
        or None if unavailable.
    """
    if rh is None:
        return None
    if ticker in _RATINGS_CACHE:
        return _RATINGS_CACHE[ticker]
    try:
        data = rh.stocks.get_ratings(ticker)
        if not data:
            _RATINGS_CACHE[ticker] = None
            return None
        summary = data.get("summary") or {}
        buy = int(summary.get("num_buy_ratings", 0) or 0)
        hold = int(summary.get("num_hold_ratings", 0) or 0)
        sell = int(summary.get("num_sell_ratings", 0) or 0)
        total = buy + hold + sell
        if total == 0:
            _RATINGS_CACHE[ticker] = None
            return None
        result = {
            "buy": buy, "hold": hold, "sell": sell, "total": total,
            "source": "robinhood",
        }
        _RATINGS_CACHE[ticker] = result
        return result
    except Exception:
        _RATINGS_CACHE[ticker] = None
        return None


def fetch_robinhood_price_target(ticker: str) -> Optional[dict]:
    """
    Fetch Robinhood's analyst price targets.
    Returns {"targetMean": float, "targetHigh": float, "targetLow": float} or None.
    """
    if rh is None:
        return None
    try:
        data = rh.stocks.get_price_targets(ticker)
        if not data:
            return None
        # robin_stocks returns a dict with summary stats
        mean = data.get("price_target_mean") or data.get("mean")
        high = data.get("price_target_high") or data.get("high")
        low = data.get("price_target_low") or data.get("low")
        if mean is None and high is None and low is None:
            return None
        return {
            "targetMean": float(mean) if mean else None,
            "targetHigh": float(high) if high else None,
            "targetLow": float(low) if low else None,
        }
    except Exception:
        return None


def fetch_watchlists(debug: bool = False) -> dict[str, list[dict]]:
    """
    Fetch all user watchlists from Robinhood.

    Returns: {watchlist_name: [{"ticker": "AAPL", "name": "Apple Inc."}, ...]}

    Tolerates the different shapes robin_stocks has returned across versions —
    each watchlist may come back as a string OR a dict; each item may come back
    as a string, dict with `symbol`, or dict with only an `instrument` URL.

    Set debug=True to log raw response types when something looks off.
    """
    _require_rh()
    out: dict[str, list[dict]] = {}

    try:
        all_lists = rh.account.get_all_watchlists()
    except Exception as e:
        print(f"[watchlists] get_all_watchlists failed: {e}")
        return out

    # Normalize the outer container
    if isinstance(all_lists, dict):
        watchlists = all_lists.get("results", []) or []
    elif isinstance(all_lists, list):
        watchlists = all_lists
    else:
        print(f"[watchlists] Unexpected response type: {type(all_lists).__name__}")
        return out

    if not watchlists:
        print("[watchlists] No watchlists found.")
        return out

    print(f"[watchlists] Found {len(watchlists)} watchlist(s).")
    if debug and watchlists:
        sample = watchlists[0]
        print(f"[watchlists] Sample shape: {type(sample).__name__} = {sample!r}")

    for wl in watchlists:
        # Extract the watchlist name — handle string or dict
        if isinstance(wl, str):
            name = wl
        elif isinstance(wl, dict):
            name = (wl.get("display_name")
                    or wl.get("name")
                    or wl.get("display")
                    or "Unnamed")
        else:
            print(f"[watchlists]   Skipping unknown watchlist type: "
                  f"{type(wl).__name__}")
            continue

        try:
            items = rh.account.get_watchlist_by_name(name)
        except Exception as e:
            print(f"[watchlists]   '{name}': could not fetch ({e})")
            continue

        # Normalize items container
        if isinstance(items, dict):
            items = items.get("results", []) or []
        if not isinstance(items, list):
            print(f"[watchlists]   '{name}': unexpected items type "
                  f"{type(items).__name__}, skipping")
            continue

        tickers: list[dict] = []
        for item in items:
            sym = None
            item_name = None

            if isinstance(item, str):
                # Item is just the ticker symbol
                sym = item
                item_name = item
            elif isinstance(item, dict):
                sym = item.get("symbol")
                item_name = item.get("name") or item.get("simple_name") or sym
                # Older API responses provide only an `instrument` URL; resolve
                if not sym:
                    inst_url = (item.get("instrument")
                                or item.get("instrument_url"))
                    if inst_url:
                        try:
                            inst = rh.helper.request_get(inst_url)
                            sym = (inst or {}).get("symbol")
                            item_name = (
                                (inst or {}).get("simple_name")
                                or (inst or {}).get("name")
                                or sym
                            )
                        except Exception:
                            pass
            else:
                if debug:
                    print(f"[watchlists]   '{name}': skipping item type "
                          f"{type(item).__name__}")
                continue

            if sym:
                tickers.append({"ticker": sym, "name": item_name or sym})

        if tickers:
            out[name] = tickers
            print(f"[watchlists]   '{name}': {len(tickers)} ticker(s)")
        else:
            print(f"[watchlists]   '{name}': empty or unparseable")

    return out


if __name__ == "__main__":
    # Quick sanity check: login + print portfolio
    login()
    pos = fetch_positions()
    total = sum(p["market_value"] for p in pos)
    print(f"\nFetched {len(pos)} positions, total = ${total:,.2f}\n")
    for p in sorted(pos, key=lambda x: -x["market_value"])[:10]:
        gain = p["percent_change"]
        sign = "+" if gain >= 0 else ""
        print(f"  {p['ticker']:6s} ${p['market_value']:>10,.2f}  "
              f"({sign}{gain:5.2f}%)  {p['name']}")


def sync_watchlist(
    watchlist_name: str,
    target_tickers: list[str],
    dry_run: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Sync a Robinhood watchlist to a target set of tickers (add new, remove gone).

    Returns: {"to_add": [...], "to_remove": [...], "added": [...], "removed": [...],
              "failed_add": [...], "failed_remove": [...], "errors": [...]}

    IMPORTANT: This relies on rh.account.post_symbols_to_watchlist and
    rh.account.delete_symbols_from_watchlist, which have been historically
    flaky across robin_stocks versions. We always confirm by re-reading the
    watchlist afterward and reporting actual outcome. Set dry_run=True to
    preview without writing.
    """
    _require_rh()
    result = {
        "to_add": [], "to_remove": [],
        "added": [], "removed": [],
        "failed_add": [], "failed_remove": [], "errors": [],
    }
    target_set = {t.upper() for t in target_tickers}

    # Read current contents of the named watchlist
    current_items: list[dict] = []
    try:
        items = rh.account.get_watchlist_by_name(watchlist_name)
        if isinstance(items, dict):
            items = items.get("results", []) or []
        current_items = items or []
    except Exception as e:
        result["errors"].append(
            f"Could not read watchlist '{watchlist_name}': {e}"
        )
        return result

    current_set: set[str] = set()
    for it in current_items:
        if isinstance(it, str):
            current_set.add(it.upper())
        elif isinstance(it, dict):
            sym = it.get("symbol")
            if not sym:
                inst_url = it.get("instrument") or it.get("instrument_url")
                if inst_url:
                    try:
                        inst = rh.helper.request_get(inst_url)
                        sym = (inst or {}).get("symbol")
                    except Exception:
                        sym = None
            if sym:
                current_set.add(sym.upper())

    result["to_add"] = sorted(target_set - current_set)
    result["to_remove"] = sorted(current_set - target_set)

    if verbose:
        print(f"[sync] '{watchlist_name}': "
              f"+{len(result['to_add'])} to add, "
              f"-{len(result['to_remove'])} to remove "
              f"(current {len(current_set)}, target {len(target_set)})")

    if dry_run:
        if verbose:
            print("[sync] DRY RUN — no changes written.")
        return result

    # Try add
    if result["to_add"]:
        try:
            rh.account.post_symbols_to_watchlist(
                inputSymbols=result["to_add"], name=watchlist_name
            )
        except Exception as e:
            result["errors"].append(f"post_symbols_to_watchlist: {e}")

    # Try remove
    if result["to_remove"]:
        try:
            rh.account.delete_symbols_from_watchlist(
                inputSymbols=result["to_remove"], name=watchlist_name
            )
        except Exception as e:
            result["errors"].append(f"delete_symbols_from_watchlist: {e}")

    # Confirm by re-reading
    try:
        items = rh.account.get_watchlist_by_name(watchlist_name)
        if isinstance(items, dict):
            items = items.get("results", []) or []
        after_set: set[str] = set()
        for it in (items or []):
            if isinstance(it, str):
                after_set.add(it.upper())
            elif isinstance(it, dict):
                sym = it.get("symbol")
                if sym:
                    after_set.add(sym.upper())
        result["added"] = sorted(after_set - current_set)
        result["removed"] = sorted(current_set - after_set)
        result["failed_add"] = sorted(set(result["to_add"]) - after_set)
        result["failed_remove"] = sorted(set(result["to_remove"]) & after_set)
    except Exception as e:
        result["errors"].append(f"verify-read: {e}")

    if verbose:
        print(f"[sync] Confirmed added: {len(result['added'])}  "
              f"removed: {len(result['removed'])}  "
              f"failed_add: {len(result['failed_add'])}  "
              f"failed_remove: {len(result['failed_remove'])}")
        for err in result["errors"]:
            print(f"[sync] ERROR: {err}")
    return result


def add_to_watchlist(
    watchlist_name: str,
    tickers: list[str],
    dry_run: bool = False,
    create_if_missing: bool = False,
    verbose: bool = True,
) -> dict:
    """
    Append tickers to a Robinhood watchlist WITHOUT removing existing entries.

    Returns: {"already_present": [...], "to_add": [...],
              "added": [...], "failed_add": [...],
              "watchlist_missing": bool, "errors": [...]}

    - Tickers already in the watchlist are reported as `already_present`
      and silently skipped (Robinhood would no-op them anyway).
    - Genuinely new tickers go to `to_add`. After writing we re-read the
      watchlist to confirm — anything that didn't land shows up in
      `failed_add` so you can see exactly what stuck.
    - If the named watchlist doesn't exist, returns immediately with
      `watchlist_missing: True` unless create_if_missing=True (in which
      case it attempts a write that may auto-create the list, depending
      on the robin_stocks version).
    - Set dry_run=True to preview without writing.
    """
    _require_rh()
    result = {
        "already_present": [],
        "to_add": [],
        "added": [],
        "failed_add": [],
        "watchlist_missing": False,
        "errors": [],
    }
    incoming = [t.strip().upper() for t in tickers if t and t.strip()]
    if not incoming:
        result["errors"].append("No tickers provided.")
        return result
    incoming_set = set(incoming)

    # Read current state of the named watchlist
    current_set: set[str] = set()
    watchlist_found = False
    try:
        items = rh.account.get_watchlist_by_name(watchlist_name)
        # Detect "doesn't exist" BEFORE normalizing to a list.
        # Robinhood returns None when the name is unknown; an empty list/dict
        # means the watchlist exists but has no items.
        if items is None:
            watchlist_found = False
        else:
            watchlist_found = True
            if isinstance(items, dict):
                items = items.get("results", []) or []
            for it in items:
                if isinstance(it, str):
                    current_set.add(it.upper())
                elif isinstance(it, dict):
                    sym = it.get("symbol")
                    if not sym:
                        inst_url = it.get("instrument") or it.get("instrument_url")
                        if inst_url:
                            try:
                                inst = rh.helper.request_get(inst_url)
                                sym = (inst or {}).get("symbol")
                            except Exception:
                                sym = None
                    if sym:
                        current_set.add(sym.upper())
    except Exception as e:
        result["errors"].append(
            f"Could not read watchlist '{watchlist_name}': {e}"
        )
        watchlist_found = False

    if not watchlist_found and not create_if_missing:
        result["watchlist_missing"] = True
        if verbose:
            print(f"[add] Watchlist '{watchlist_name}' not found. "
                  f"Use --create-watchlist to attempt auto-creation, "
                  f"or create it in the Robinhood app first.")
        return result

    # Split incoming into already-present vs to-add
    result["already_present"] = sorted(incoming_set & current_set)
    result["to_add"] = sorted(incoming_set - current_set)

    if verbose:
        print(f"[add] '{watchlist_name}': "
              f"{len(result['already_present'])} already present, "
              f"{len(result['to_add'])} new to add"
              + (" (DRY RUN)" if dry_run else ""))
        if result["already_present"]:
            print(f"[add]   Skipping (already in list): "
                  f"{', '.join(result['already_present'])}")
        if result["to_add"]:
            print(f"[add]   Will add: {', '.join(result['to_add'])}")

    if dry_run or not result["to_add"]:
        return result

    # Write
    try:
        rh.account.post_symbols_to_watchlist(
            inputSymbols=result["to_add"], name=watchlist_name
        )
    except Exception as e:
        result["errors"].append(f"post_symbols_to_watchlist: {e}")

    # Verify by re-reading
    try:
        items = rh.account.get_watchlist_by_name(watchlist_name)
        if isinstance(items, dict):
            items = items.get("results", []) or []
        after_set: set[str] = set()
        for it in (items or []):
            if isinstance(it, str):
                after_set.add(it.upper())
            elif isinstance(it, dict):
                sym = it.get("symbol")
                if sym:
                    after_set.add(sym.upper())
                elif it.get("instrument") or it.get("instrument_url"):
                    inst_url = it.get("instrument") or it.get("instrument_url")
                    try:
                        inst = rh.helper.request_get(inst_url)
                        sym = (inst or {}).get("symbol")
                        if sym:
                            after_set.add(sym.upper())
                    except Exception:
                        pass
        result["added"] = sorted((after_set - current_set) & incoming_set)
        result["failed_add"] = sorted(set(result["to_add"]) - after_set)
    except Exception as e:
        result["errors"].append(f"verify-read: {e}")

    if verbose:
        if result["added"]:
            print(f"[add]   ✓ Confirmed added: {', '.join(result['added'])}")
        if result["failed_add"]:
            print(f"[add]   ✗ Failed to add (API didn't persist): "
                  f"{', '.join(result['failed_add'])}")
        for err in result["errors"]:
            print(f"[add]   ERROR: {err}")
    return result
