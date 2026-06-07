"""
fix_tax_missing.py
------------------
Fixes the bug where TRIM/SELL positions with valid tax lots still don't
appear in the tax section because r.current_price is None at tax-analysis time.

Root cause: in main(), the tax loop does:
    if lots and r.current_price:
        r.tax = analyze_tax_with_lots(...)
    else:
        r.tax = analyze_tax(...)   # no lots, needs unrealized_gain

If r.current_price is None (yfinance returned nothing during the main run),
analyze_tax_with_lots is skipped. The fallback analyze_tax() also fails
silently if r.unrealized_gain is None (no cost basis from Robinhood).

Fix: fetch the price on-the-spot if r.current_price is None before the
tax loop, so analyze_tax_with_lots always gets a valid price.

Usage:
    python fix_tax_missing.py
    python fix_tax_missing.py --file /path/to/analyze_portfolio.py
"""

import argparse
import shutil
import sys
from pathlib import Path

FIND = '''\
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
                    print(f"[tax] {r.ticker}: skipping ({per_e})")'''

REPLACE = '''\
            for r in flagged:
                try:
                    lots = (tax_lots_lookup.get(r.ticker)
                            if tax_lots_lookup else None)

                    # If current_price is None (yfinance returned nothing during
                    # the main analysis run), do a fresh fetch now so we can still
                    # run the lot-level tax analysis. Without a price we can't
                    # compute per-lot gains, and the position silently disappears
                    # from the tax section.
                    current_price = r.current_price
                    if current_price is None and lots:
                        try:
                            import yfinance as _yf
                            _info = _yf.Ticker(r.ticker).info or {}
                            current_price = (
                                _info.get("regularMarketPrice")
                                or _info.get("currentPrice")
                            )
                            if current_price:
                                current_price = float(current_price)
                                r.current_price = current_price  # update PA too
                                print(f"[tax] {r.ticker}: fetched price ${current_price:.2f} "
                                      f"(was None during main run)")
                        except Exception as _pe:
                            print(f"[tax] {r.ticker}: could not fetch price: {_pe}")

                    if lots and current_price:
                        r.tax = analyze_tax_with_lots(
                            ticker=r.ticker,
                            verdict=r.verdict.label,
                            lots=lots,
                            current_price=current_price,
                            cfg=tax_cfg,
                        )
                    else:
                        # Fallback: position-level open date. Note: if
                        # position_opened is None (CSV mode without dates),
                        # analyze_tax still returns a TaxAnalysis with the
                        # holding-period fields empty but a tax estimate
                        # using representative rates. That's enough to keep
                        # the position visible in the tax section.
                        if not lots:
                            print(f"[tax] {r.ticker}: no lots found, "
                                  f"falling back to position-level estimate")
                        elif not current_price:
                            print(f"[tax] {r.ticker}: price unavailable, "
                                  f"falling back to position-level estimate")
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
                    print(f"[tax] {r.ticker}: skipping ({per_e})")'''


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="analyze_portfolio.py")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.file)
    if not src.exists():
        print(f"ERROR: {src} not found.", file=sys.stderr)
        sys.exit(1)

    original = src.read_text(encoding="utf-8")

    if FIND not in original:
        if REPLACE in original:
            print("✓ Already patched — nothing to do.")
        else:
            print("✗ Pattern NOT FOUND — the file may differ from the expected version.")
            print("  Check that analyze_portfolio.py matches the version this patch targets.")
        sys.exit(0)

    patched = original.replace(FIND, REPLACE, 1)

    if args.dry_run:
        print("--dry-run: would apply patch (1 change).")
        sys.exit(0)

    bak = src.with_suffix(".py.bak3")
    shutil.copy2(src, bak)
    print(f"Backup → {bak}")

    src.write_text(patched, encoding="utf-8")
    print(f"✓ Patched {src}")
    print()
    print("Re-run the report to see MU in the tax section:")
    print("  python analyze_portfolio.py --source robinhood --include-watchlists --out report.html")


if __name__ == "__main__":
    main()
