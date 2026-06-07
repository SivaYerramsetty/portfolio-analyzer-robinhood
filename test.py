# run in your project folder
import robinhood_source as rhs
from tax_analysis import TaxConfig, analyze_tax_with_lots
import yfinance as yf

rhs.login(verbose=False)
lots = rhs.fetch_tax_lots(verbose=False)
mu_lots = lots.get("MU", [])

print(f"MU lots: {len(mu_lots)}")
for i, lot in enumerate(mu_lots):
    print(f"  lot {i}: {lot}")

price = yf.Ticker("MU").info.get("regularMarketPrice")
print(f"\nMU current price: {price}")

cfg = TaxConfig.from_env()
try:
    result = analyze_tax_with_lots(
        ticker="MU",
        verdict="TRIM",
        lots=mu_lots,
        current_price=price,
        cfg=cfg,
    )
    print(f"\nTax result: {result}")
except Exception as e:
    import traceback
    print(f"\nERROR: {e}")
    traceback.print_exc()