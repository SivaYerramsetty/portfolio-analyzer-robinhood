"""
analyst_aggregator.py
---------------------
Combines analyst ratings from multiple sources into a single normalized view.

Sources used (each is optional — graceful fallback if unavailable):
  - Robinhood       (when --source robinhood is in use; same data the app shows)
  - Finnhub         (when FINNHUB_API_KEY is set; free tier sufficient)
  - Yahoo Finance   (via yfinance; always available)

Output dict:
    {
        "buy":   int,       # total count of buy/strong-buy ratings across sources
        "hold":  int,
        "sell":  int,       # total count of sell/strong-sell ratings
        "total": int,
        "rec_avg": float,   # 1.0 = strong buy ... 5.0 = strong sell (Yahoo-style)
        "source": "robinhood+finnhub+yahoo" | subset,
        "sources_used": ["robinhood", "finnhub", "yahoo"],
    }

The "rec_avg" is computed from the aggregated buy/hold/sell distribution using
the standard 1-5 scale: 1=strong buy (we collapse with buy at score 2),
3=hold, 5=strong sell (collapsed with sell at score 4). This matches Yahoo's
recommendationMean semantics so existing code that uses it keeps working.
"""

from __future__ import annotations

from typing import Optional


def normalize_breakdown(buy: int, hold: int, sell: int,
                        source: str) -> Optional[dict]:
    """Return a normalized breakdown dict, or None if no analysts."""
    total = buy + hold + sell
    if total <= 0:
        return None
    # rec_avg: weight buys at 2 (between strong buy 1 and buy 2), holds at 3,
    # sells at 4 (between sell 4 and strong sell 5). This gives a clean 1-5
    # blend without over-claiming "strong" intensity we don't actually have.
    rec_avg = (buy * 2.0 + hold * 3.0 + sell * 4.0) / total
    return {
        "buy": buy, "hold": hold, "sell": sell,
        "total": total, "rec_avg": round(rec_avg, 2),
        "source": source,
        "sources_used": [source],
    }


def aggregate(*breakdowns: Optional[dict]) -> Optional[dict]:
    """
    Combine multiple normalized breakdowns by summing counts and re-computing
    rec_avg. Drops Nones. Returns None if everything is None.
    """
    valid = [b for b in breakdowns if b]
    if not valid:
        return None
    if len(valid) == 1:
        return dict(valid[0])  # copy

    total_buy = sum(b.get("buy", 0) for b in valid)
    total_hold = sum(b.get("hold", 0) for b in valid)
    total_sell = sum(b.get("sell", 0) for b in valid)
    total = total_buy + total_hold + total_sell
    if total <= 0:
        return None

    rec_avg = (total_buy * 2.0 + total_hold * 3.0 + total_sell * 4.0) / total
    sources = []
    seen = set()
    for b in valid:
        for s in b.get("sources_used", [b.get("source")]) or []:
            if s and s not in seen:
                sources.append(s)
                seen.add(s)

    return {
        "buy": total_buy,
        "hold": total_hold,
        "sell": total_sell,
        "total": total,
        "rec_avg": round(rec_avg, 2),
        "source": "+".join(sources),
        "sources_used": sources,
    }
