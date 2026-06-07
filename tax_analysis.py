"""
tax_analysis.py
---------------
Tax-aware trim guidance for positions flagged SELL or TRIM.

For each flagged position this estimates:
  - Holding-period status (long-term vs short-term) from the position open date
  - Estimated federal tax if trimmed NOW, at both short-term and long-term rates
  - "Days until long-term" if the position is approaching the 1-year mark
  - The least-taxable ways to trim (specific-lot ID, harvest pairing, etc.)

IMPORTANT DATA CAVEAT
---------------------
Robinhood's API exposes POSITION-level open dates, not individual tax LOTS.
So if you bought a stock in 2023 and added more last month, this module sees
only the earliest date and will treat the whole position as long-term — but
your most recent shares are actually still short-term. For exact lot-level
treatment, use Robinhood's in-app "lots" view or your 1099-B. Treat the
long/short split here as a planning estimate, not a filing number.

NOT TAX ADVICE
--------------
This produces educational estimates using published 2026 federal brackets.
It is not personalized tax advice. State taxes, NIIT, AMT interactions, your
specific lots, and your full income picture all matter. Consult a tax
professional or CPA before acting.

2026 figures sourced from IRS Rev. Proc. 2025-32 / Tax Foundation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


# ============================================================
# 2026 federal long-term capital gains brackets (taxable income)
# ============================================================
# Rate -> upper income threshold for that rate, by filing status.
LTCG_BRACKETS_2026 = {
    "single": [(0.00, 49_450), (0.15, 545_500), (0.20, float("inf"))],
    "mfj":    [(0.00, 98_900), (0.15, 613_700), (0.20, float("inf"))],
    "hoh":    [(0.00, 66_200), (0.15, 0),       (0.20, float("inf"))],  # 15% upper varies
    "mfs":    [(0.00, 49_450), (0.15, 306_850), (0.20, float("inf"))],
}

# 2026 ordinary income brackets (used for SHORT-term gains), by filing status.
# (rate, upper_threshold_of_taxable_income)
ORDINARY_BRACKETS_2026 = {
    "single": [
        (0.10, 12_400), (0.12, 50_400), (0.22, 105_700), (0.24, 201_775),
        (0.32, 256_225), (0.35, 640_600), (0.37, float("inf")),
    ],
    "mfj": [
        (0.10, 24_800), (0.12, 100_800), (0.22, 211_400), (0.24, 403_550),
        (0.32, 512_450), (0.35, 768_600), (0.37, float("inf")),
    ],
    "hoh": [
        (0.10, 17_700), (0.12, 67_450), (0.22, 105_700), (0.24, 201_775),
        (0.32, 256_200), (0.35, 640_600), (0.37, float("inf")),
    ],
    "mfs": [
        (0.10, 12_400), (0.12, 50_400), (0.22, 105_700), (0.24, 201_775),
        (0.32, 256_225), (0.35, 384_300), (0.37, float("inf")),
    ],
}

# Net Investment Income Tax — 3.8% surtax above these MAGI thresholds (not
# inflation-indexed). Applied to investment gains for high earners.
NIIT_RATE = 0.038
NIIT_THRESHOLDS = {
    "single": 200_000, "mfj": 250_000, "hoh": 200_000, "mfs": 125_000,
}


@dataclass
class TaxConfig:
    """User tax situation, read from env vars (all optional)."""
    filing_status: str = "single"      # single | mfj | hoh | mfs
    taxable_income: Optional[float] = None   # ordinary taxable income (pre-gains)
    state_rate: float = 0.0            # state cap-gains rate as decimal, e.g. 0.05
    apply_niit: bool = False           # whether NIIT 3.8% likely applies

    @classmethod
    def from_env(cls) -> "TaxConfig":
        status = (os.environ.get("TAX_FILING_STATUS", "single") or "single").lower()
        if status not in LTCG_BRACKETS_2026:
            status = "single"
        income = os.environ.get("TAX_TAXABLE_INCOME")
        state = os.environ.get("TAX_STATE_RATE", "0")
        niit = os.environ.get("TAX_APPLY_NIIT", "")
        try:
            income_f = float(income) if income else None
        except ValueError:
            income_f = None
        try:
            state_f = float(state) / 100 if state and float(state) > 1 else float(state or 0)
        except ValueError:
            state_f = 0.0
        return cls(
            filing_status=status,
            taxable_income=income_f,
            state_rate=state_f,
            apply_niit=str(niit).lower() in ("1", "true", "yes"),
        )

    @property
    def is_configured(self) -> bool:
        """True if the user gave enough to personalize (income + status)."""
        return self.taxable_income is not None


@dataclass
class TaxAnalysis:
    ticker: str
    verdict: str
    # Holding period
    opened: Optional[str] = None          # ISO date or None
    days_held: Optional[int] = None
    is_long_term: Optional[bool] = None
    days_to_long_term: Optional[int] = None
    # Gain
    unrealized_gain: Optional[float] = None
    # Tax estimates (on the full unrealized gain if trimmed entirely)
    tax_if_short_term: Optional[float] = None
    tax_if_long_term: Optional[float] = None
    effective_rate_st: Optional[float] = None
    effective_rate_lt: Optional[float] = None
    # Strategy notes
    strategies: list[str] = field(default_factory=list)
    timing_note: str = ""
    # ---- Exact lot-level breakdown (when order history is available) ----
    has_lots: bool = False
    lt_shares: Optional[float] = None
    st_shares: Optional[float] = None
    lt_gain: Optional[float] = None         # unrealized gain on long-term lots
    st_gain: Optional[float] = None         # unrealized gain on short-term lots
    lt_tax: Optional[float] = None          # tax on LT lots if sold now
    st_tax: Optional[float] = None          # tax on ST lots if sold now
    next_lot_to_lt_days: Optional[int] = None   # days until next ST lot turns LT
    next_lot_to_lt_shares: Optional[float] = None
    lots_detail: list[dict] = field(default_factory=list)  # per-lot rows for report


def _marginal_ltcg_rate(income: float, status: str) -> float:
    brackets = LTCG_BRACKETS_2026.get(status, LTCG_BRACKETS_2026["single"])
    for rate, upper in brackets:
        if income <= upper:
            return rate
    return 0.20


def _marginal_ordinary_rate(income: float, status: str) -> float:
    brackets = ORDINARY_BRACKETS_2026.get(status, ORDINARY_BRACKETS_2026["single"])
    for rate, upper in brackets:
        if income <= upper:
            return rate
    return 0.37


def _estimate_tax(gain: float, cfg: TaxConfig, long_term: bool) -> tuple[float, float]:
    """Return (tax_dollars, effective_rate) for a gain, given config.

    When taxable_income is unknown, falls back to a representative middle rate
    (LT 15%, ST 24%) so the report still shows a ballpark.
    """
    if gain <= 0:
        return 0.0, 0.0

    if cfg.is_configured:
        income = cfg.taxable_income or 0.0
        if long_term:
            fed_rate = _marginal_ltcg_rate(income + gain, cfg.filing_status)
        else:
            fed_rate = _marginal_ordinary_rate(income + gain, cfg.filing_status)
    else:
        fed_rate = 0.15 if long_term else 0.24  # representative defaults

    rate = fed_rate + cfg.state_rate
    if cfg.apply_niit:
        rate += NIIT_RATE

    return gain * rate, rate


def analyze_tax(
    ticker: str,
    verdict: str,
    unrealized_gain: Optional[float],
    position_opened: Optional[str],
    cfg: TaxConfig,
    today: Optional[date] = None,
) -> TaxAnalysis:
    """Build a TaxAnalysis for one flagged position."""
    today = today or date.today()
    ta = TaxAnalysis(ticker=ticker, verdict=verdict, unrealized_gain=unrealized_gain)

    # Holding period
    if position_opened:
        ta.opened = position_opened
        try:
            opened_date = datetime.strptime(position_opened[:10], "%Y-%m-%d").date()
            ta.days_held = (today - opened_date).days
            ta.is_long_term = ta.days_held > 365
            if not ta.is_long_term:
                ta.days_to_long_term = 366 - ta.days_held
        except ValueError:
            pass

    gain = unrealized_gain or 0.0

    # Tax estimates (only meaningful for gains; losses are a different play)
    if gain > 0:
        ta.tax_if_short_term, ta.effective_rate_st = _estimate_tax(gain, cfg, False)
        ta.tax_if_long_term, ta.effective_rate_lt = _estimate_tax(gain, cfg, True)

    ta.strategies = _build_strategies(ta, gain, cfg)
    ta.timing_note = _build_timing_note(ta, gain)
    return ta


def _build_timing_note(ta: TaxAnalysis, gain: float) -> str:
    if gain <= 0:
        # It's a loss — different framing
        if ta.is_long_term is False:
            return ("This position is at a loss. Selling realizes a deductible "
                    "capital loss you can use to offset other gains "
                    "(watch the 30-day wash-sale rule if you plan to rebuy).")
        return ("This position is at a loss. Realizing it harvests a capital "
                "loss to offset gains elsewhere (mind the wash-sale rule).")

    if ta.is_long_term is True:
        return ("Already long-term — you qualify for the lower capital-gains "
                "rate now. No holding-period benefit to waiting.")

    if ta.is_long_term is False and ta.days_to_long_term is not None:
        if ta.days_to_long_term <= 60:
            saved = None
            if ta.tax_if_short_term is not None and ta.tax_if_long_term is not None:
                saved = ta.tax_if_short_term - ta.tax_if_long_term
            extra = f" — waiting could save about ${saved:,.0f} in tax" if saved else ""
            return (f"Only ~{ta.days_to_long_term} days from long-term status"
                    f"{extra}. Holding past that date drops the gain from "
                    f"ordinary-income rates to the lower long-term rate.")
        return (f"~{ta.days_to_long_term} days from long-term. If the thesis "
                f"allows waiting, crossing the 1-year mark moves this gain to "
                f"the lower long-term rate.")

    return ("Holding period unknown (no purchase date available) — check "
            "Robinhood's in-app lots view to confirm short- vs long-term.")


def _build_strategies(ta: TaxAnalysis, gain: float, cfg: TaxConfig) -> list[str]:
    s: list[str] = []

    if gain > 0:
        # Specific-lot identification
        s.append(
            "Use specific-lot identification: sell your highest-cost-basis lots "
            "first to realize the smallest gain per share trimmed."
        )
        if ta.is_long_term is False:
            s.append(
                "Prefer trimming any long-term lots first (held >1 year) — they're "
                "taxed at the lower capital-gains rate vs. ordinary rates for "
                "short-term lots."
            )
        # Tax-loss harvesting pairing
        s.append(
            "Pair the sale with tax-loss harvesting: realize losses elsewhere in "
            "the same tax year to offset this gain dollar-for-dollar."
        )
        # Spread across tax years
        s.append(
            "Spread the trim across two tax years (e.g., part in December, part in "
            "January) to avoid bunching the gain into one year and possibly "
            "tipping into a higher bracket."
        )
        # 0% bracket opportunity
        if cfg.is_configured and cfg.filing_status in LTCG_BRACKETS_2026:
            zero_ceiling = LTCG_BRACKETS_2026[cfg.filing_status][0][1]
            if (cfg.taxable_income or 0) < zero_ceiling:
                s.append(
                    f"Your taxable income may be under the 2026 0% long-term "
                    f"capital-gains ceiling (${zero_ceiling:,.0f} for "
                    f"{cfg.filing_status.upper()}). Long-term gains up to that "
                    f"line could be federally tax-free — a window to trim cheaply."
                )
    else:
        # Loss position
        s.append(
            "This is a loss: selling harvests a capital loss. Up to $3,000 of net "
            "capital loss can offset ordinary income per year; the rest carries "
            "forward."
        )
        s.append(
            "Avoid the wash-sale rule: don't buy the same (or substantially "
            "identical) security within 30 days before or after the sale, or the "
            "loss is disallowed."
        )

    # Donation alternative for big long-term gains
    if gain > 0 and ta.is_long_term:
        s.append(
            "If charitably inclined: donating appreciated long-term shares "
            "directly (vs. selling then donating cash) can avoid the capital-gains "
            "tax entirely while still giving a deduction for fair-market value."
        )

    return s


def analyze_tax_with_lots(
    ticker: str,
    verdict: str,
    lots: list[dict],
    current_price: float,
    cfg: TaxConfig,
    today: Optional[date] = None,
) -> TaxAnalysis:
    """
    EXACT tax analysis using reconstructed open lots.

    Each lot: {"date": "YYYY-MM-DD", "shares": float, "price": float, "cost": float}
    Splits holdings into long-term (held >365d) and short-term buckets, computes
    the unrealized gain and estimated tax for each precisely.
    """
    today = today or date.today()
    ta = TaxAnalysis(ticker=ticker, verdict=verdict, has_lots=True)

    lt_shares = st_shares = 0.0
    lt_gain = st_gain = 0.0
    soonest_days = None
    soonest_shares = None
    detail = []

    for lot in lots:
        try:
            d = datetime.strptime(lot["date"][:10], "%Y-%m-%d").date()
        except (ValueError, KeyError):
            continue
        shares = float(lot["shares"])
        buy_price = float(lot["price"])
        days = (today - d).days
        is_lt = days > 365
        market_val = shares * current_price
        cost = shares * buy_price
        gain = market_val - cost

        if is_lt:
            lt_shares += shares
            lt_gain += gain
        else:
            st_shares += shares
            st_gain += gain
            days_to_lt = 366 - days
            if soonest_days is None or days_to_lt < soonest_days:
                soonest_days = days_to_lt
                soonest_shares = shares

        detail.append({
            "date": lot["date"][:10],
            "shares": round(shares, 4),
            "buy_price": round(buy_price, 2),
            "days_held": days,
            "is_long_term": is_lt,
            "gain": round(gain, 2),
            "days_to_lt": (None if is_lt else 366 - days),
        })

    # Sort detail oldest-first
    detail.sort(key=lambda x: x["date"])

    ta.lt_shares = round(lt_shares, 4)
    ta.st_shares = round(st_shares, 4)
    ta.lt_gain = round(lt_gain, 2)
    ta.st_gain = round(st_gain, 2)
    ta.unrealized_gain = round(lt_gain + st_gain, 2)
    ta.next_lot_to_lt_days = soonest_days
    ta.next_lot_to_lt_shares = (round(soonest_shares, 4)
                                if soonest_shares is not None else None)
    ta.lots_detail = detail
    ta.is_long_term = (st_shares < 1e-6) if (lt_shares + st_shares) > 0 else None

    # Tax per bucket (only gains are taxable; losses handled in strategies)
    if lt_gain > 0:
        ta.lt_tax, ta.effective_rate_lt = _estimate_tax(lt_gain, cfg, long_term=True)
    else:
        ta.lt_tax, ta.effective_rate_lt = 0.0, 0.0
    if st_gain > 0:
        ta.st_tax, ta.effective_rate_st = _estimate_tax(st_gain, cfg, long_term=False)
    else:
        ta.st_tax, ta.effective_rate_st = 0.0, 0.0

    ta.tax_if_long_term = ta.lt_tax
    ta.tax_if_short_term = ta.st_tax

    ta.timing_note = _build_lot_timing_note(ta)
    ta.strategies = _build_lot_strategies(ta, cfg)
    return ta


def _build_lot_timing_note(ta: TaxAnalysis) -> str:
    notes = []
    if ta.lt_shares and ta.st_shares:
        notes.append(
            f"You hold a mix: {ta.lt_shares:g} long-term share(s) and "
            f"{ta.st_shares:g} short-term. Trim the long-term shares first for "
            f"the lower rate."
        )
    elif ta.lt_shares and not ta.st_shares:
        notes.append("All shares are long-term — you qualify for the lower "
                     "capital-gains rate on the entire position now.")
    elif ta.st_shares and not ta.lt_shares:
        notes.append("All shares are short-term — gains would be taxed at "
                     "ordinary-income rates today.")

    if ta.next_lot_to_lt_days is not None and ta.next_lot_to_lt_days <= 60:
        notes.append(
            f"Your next {ta.next_lot_to_lt_shares:g} share(s) cross into "
            f"long-term in ~{ta.next_lot_to_lt_days} days — waiting drops their "
            f"gain to the lower rate."
        )
    return " ".join(notes)


def _build_lot_strategies(ta: TaxAnalysis, cfg: TaxConfig) -> list[str]:
    s = []
    total_gain = (ta.lt_gain or 0) + (ta.st_gain or 0)

    if ta.lt_shares and ta.st_shares:
        s.append(
            f"Specifically identify the long-term lots at sale "
            f"({ta.lt_shares:g} share(s)) so you're taxed at the lower "
            f"capital-gains rate instead of ordinary rates."
        )
    if ta.st_gain and ta.st_gain > 0 and ta.next_lot_to_lt_days is not None \
            and ta.next_lot_to_lt_days <= 90:
        diff = None
        if ta.effective_rate_st and ta.effective_rate_lt:
            diff = ta.st_gain * (ta.effective_rate_st - ta.effective_rate_lt)
        saving = f" (~${diff:,.0f} saved)" if diff and diff > 0 else ""
        s.append(
            f"Delay trimming the short-term shares ~{ta.next_lot_to_lt_days} days "
            f"until they turn long-term{saving}."
        )
    if total_gain > 0:
        s.append(
            "Pair the sale with tax-loss harvesting elsewhere this year to offset "
            "the realized gain dollar-for-dollar."
        )
        s.append(
            "Spread the trim across two tax years to avoid bunching the gain into "
            "one bracket."
        )
        if cfg.is_configured and cfg.filing_status in LTCG_BRACKETS_2026:
            zero_ceiling = LTCG_BRACKETS_2026[cfg.filing_status][0][1]
            if (cfg.taxable_income or 0) < zero_ceiling:
                s.append(
                    f"Part of your long-term gain may fall in the 2026 0% bracket "
                    f"(taxable income under ${zero_ceiling:,.0f} for "
                    f"{cfg.filing_status.upper()}) — potentially tax-free."
                )
    # Loss lots present?
    has_loss = any(l["gain"] < 0 for l in ta.lots_detail)
    if has_loss:
        s.append(
            "Some lots are at a loss: selling those specific lots harvests a "
            "deductible capital loss (mind the 30-day wash-sale rule if rebuying)."
        )
    return s


# ============================================================
# YTD realized-gains tax estimate + minimization recommendations
# ============================================================

@dataclass
class YtdTaxEstimate:
    """Year-to-date realized gain summary and tax estimate."""
    year: int
    # Net realized gains (positive = gain, negative = loss)
    net_st_gain: float
    net_lt_gain: float
    net_total_gain: float
    # Tax owed on realized gains (applies offsets correctly per IRS rules)
    estimated_tax: float
    # Decomposed for transparency
    st_tax_component: float       # tax on net ST gains (or 0 if net is loss)
    lt_tax_component: float       # tax on net LT gains (or 0 if net is loss)
    ordinary_offset_used: float   # capital losses offsetting ordinary income (max $3000)
    ordinary_tax_saved: float     # tax saved from the ordinary offset
    # Loss carryforward
    loss_carryforward: float      # carries to next year if total > $3000 net loss
    # Raw breakdowns
    realized_count: int
    lt_gains: float
    lt_losses: float
    st_gains: float
    st_losses: float


def compute_ytd_tax_estimate(
    realized_ytd: dict,
    cfg: TaxConfig,
) -> YtdTaxEstimate:
    """
    Estimate total tax liability on YTD realized gains using IRS netting rules.

    Netting rules (simplified, sufficient for most cases):
      1. Net ST losses offset ST gains first; LT losses offset LT gains first.
      2. Net ST losses then offset net LT gains; net LT losses then offset net ST.
      3. After all netting, the remaining net loss (if any) can offset up to
         $3,000 of ordinary income per year. Any further loss carries forward.
      4. Net ST gain is taxed at ordinary rates; net LT gain at LTCG rates.

    NIIT (3.8%) applies on net investment income if AGI thresholds are crossed —
    we approximate this with cfg.apply_niit toggle.
    """
    if not realized_ytd or not realized_ytd.get("realized_gains"):
        return YtdTaxEstimate(
            year=realized_ytd.get("year", 0) if realized_ytd else 0,
            net_st_gain=0.0, net_lt_gain=0.0, net_total_gain=0.0,
            estimated_tax=0.0,
            st_tax_component=0.0, lt_tax_component=0.0,
            ordinary_offset_used=0.0, ordinary_tax_saved=0.0,
            loss_carryforward=0.0, realized_count=0,
            lt_gains=0.0, lt_losses=0.0, st_gains=0.0, st_losses=0.0,
        )

    st_gains = realized_ytd["st_gains"]
    st_losses = realized_ytd["st_losses"]
    lt_gains = realized_ytd["lt_gains"]
    lt_losses = realized_ytd["lt_losses"]

    # Step 1: net within each category
    net_st = st_gains - st_losses
    net_lt = lt_gains - lt_losses

    # Step 2: net across categories if one is a gain and the other a loss
    if net_st < 0 and net_lt > 0:
        # ST loss offsets LT gain
        offset = min(-net_st, net_lt)
        net_st += offset
        net_lt -= offset
    elif net_lt < 0 and net_st > 0:
        # LT loss offsets ST gain
        offset = min(-net_lt, net_st)
        net_lt += offset
        net_st -= offset

    net_total = net_st + net_lt

    # Step 3: tax on remaining gains
    # When the user hasn't set TAX_TAXABLE_INCOME, _marginal_*_rate() would
    # use income=$0 — which lands in the 0% bracket and produces $0 tax,
    # even on substantial gains. That's a misleading silence: it looks like
    # zero tax owed but really means "we don't know". Fall back to the same
    # representative defaults that _estimate_tax() uses (LT 15%, ST 24%) so
    # the user sees a ballpark figure, just like the per-position cards do.
    st_tax = 0.0
    lt_tax = 0.0
    if cfg.is_configured:
        st_fed_rate = _marginal_ordinary_rate(cfg.taxable_income or 0, cfg.filing_status)
        lt_fed_rate = _marginal_ltcg_rate(cfg.taxable_income or 0, cfg.filing_status)
    else:
        st_fed_rate = 0.24    # representative ordinary-income middle bracket
        lt_fed_rate = 0.15    # representative LTCG middle bracket

    if net_st > 0:
        st_tax = net_st * (st_fed_rate + (cfg.state_rate or 0))
        if cfg.apply_niit:
            st_tax += net_st * 0.038
    if net_lt > 0:
        lt_tax = net_lt * (lt_fed_rate + (cfg.state_rate or 0))
        if cfg.apply_niit:
            lt_tax += net_lt * 0.038

    # Step 4: ordinary income offset ($3,000/year cap) if net loss
    ordinary_offset = 0.0
    ordinary_savings = 0.0
    loss_carryforward = 0.0
    if net_total < 0:
        ordinary_offset = min(-net_total, 3000.0)
        # Same fallback for ordinary rate
        if cfg.is_configured:
            ord_rate = _marginal_ordinary_rate(cfg.taxable_income or 0, cfg.filing_status)
        else:
            ord_rate = 0.24
        ordinary_savings = ordinary_offset * (ord_rate + (cfg.state_rate or 0))
        loss_carryforward = -net_total - ordinary_offset

    estimated_tax = st_tax + lt_tax - ordinary_savings

    return YtdTaxEstimate(
        year=realized_ytd["year"],
        net_st_gain=round(net_st, 2),
        net_lt_gain=round(net_lt, 2),
        net_total_gain=round(net_total, 2),
        estimated_tax=round(estimated_tax, 2),
        st_tax_component=round(st_tax, 2),
        lt_tax_component=round(lt_tax, 2),
        ordinary_offset_used=round(ordinary_offset, 2),
        ordinary_tax_saved=round(ordinary_savings, 2),
        loss_carryforward=round(loss_carryforward, 2),
        realized_count=len(realized_ytd["realized_gains"]),
        lt_gains=round(lt_gains, 2),
        lt_losses=round(lt_losses, 2),
        st_gains=round(st_gains, 2),
        st_losses=round(st_losses, 2),
    )


def generate_tax_minimization_recommendations(
    ytd: YtdTaxEstimate,
    holdings_data: list,
    cfg: TaxConfig,
) -> list[dict]:
    """
    Build a prioritized list of tax-reduction recommendations.

    Returns list of dicts: {priority, category, headline, detail, dollar_impact}
    where priority is "high" / "medium" / "low".

    `holdings_data` is a list of dicts with keys:
      - ticker (str)
      - unrealized_gain (float, optional — None if cost basis unknown)
      - days_held (int, optional — days the position has been open)
    Loss-harvest candidates are positions with significant unrealized losses.
    LT-threshold candidates are positions with 275-365 days held and gains.
    Charitable-giving candidates are LT positions (>365 days) with big gains.
    """
    recs = []

    # -------- 1. Tax-loss harvesting candidates --------
    loss_candidates = []
    for h in holdings_data:
        if h.get("unrealized_gain") and h["unrealized_gain"] < -100:
            loss_candidates.append({
                "ticker": h["ticker"],
                "unrealized_loss": -h["unrealized_gain"],
                "is_long_term": (h.get("days_held") or 0) > 365,
            })
    loss_candidates.sort(key=lambda x: x["unrealized_loss"], reverse=True)

    if loss_candidates and ytd.net_total_gain > 0:
        total_harvestable = sum(c["unrealized_loss"] for c in loss_candidates)
        offset_amount = min(total_harvestable, ytd.net_total_gain)
        # Use representative fallback rates if user hasn't configured income.
        # Without this, the savings calc would multiply by 0% and show $0.
        if cfg.is_configured:
            rate_used = (_marginal_ltcg_rate(cfg.taxable_income or 0, cfg.filing_status)
                         + (cfg.state_rate or 0))
        else:
            rate_used = 0.15 + (cfg.state_rate or 0)   # representative LTCG default
        if cfg.apply_niit:
            rate_used += 0.038
        savings = offset_amount * rate_used
        top3 = ", ".join(
            f"{c['ticker']} (${c['unrealized_loss']:,.0f})"
            for c in loss_candidates[:3]
        )
        recs.append({
            "priority": "high",
            "category": "Tax-Loss Harvesting",
            "headline": f"Harvest up to ${offset_amount:,.0f} in losses to offset YTD gains",
            "detail": (
                f"You have ${total_harvestable:,.0f} in unrealized losses across "
                f"{len(loss_candidates)} position(s). Selling these (top candidates: "
                f"{top3}) could offset your ${ytd.net_total_gain:,.0f} realized gains, "
                f"saving roughly ${savings:,.0f} in taxes. "
                f"Mind the 30-day wash-sale rule — don't rebuy the same or "
                f"substantially identical security within 30 days before or after."
            ),
            "dollar_impact": round(savings, 2),
        })
    elif loss_candidates:
        total_harvestable = sum(c["unrealized_loss"] for c in loss_candidates)
        top3 = ", ".join(
            f"{c['ticker']} (${c['unrealized_loss']:,.0f})"
            for c in loss_candidates[:3]
        )
        recs.append({
            "priority": "medium",
            "category": "Tax-Loss Harvesting",
            "headline": f"Banking ${min(total_harvestable, 3000):,.0f} in losses "
                        f"could reduce ordinary income tax",
            "detail": (
                f"You have ${total_harvestable:,.0f} in unrealized losses (top: "
                f"{top3}). With no realized gains YTD, harvesting up to $3,000 "
                f"offsets ordinary income; anything above carries forward "
                f"indefinitely. Watch the 30-day wash-sale rule."
            ),
            "dollar_impact": min(total_harvestable, 3000) * 0.32,
        })

    # -------- 2. Approaching long-term threshold --------
    # Pre-compute the rates once (using fallbacks if not configured)
    if cfg.is_configured:
        _st_rate = _marginal_ordinary_rate(cfg.taxable_income or 0, cfg.filing_status)
        _lt_rate_fed = _marginal_ltcg_rate(cfg.taxable_income or 0, cfg.filing_status)
    else:
        _st_rate = 0.24
        _lt_rate_fed = 0.15

    approaching = []
    for h in holdings_data:
        gain = h.get("unrealized_gain")
        days = h.get("days_held")
        if gain and gain > 0 and days is not None and 275 <= days <= 365:
            days_to_lt = 366 - days
            potential_savings = gain * (_st_rate - _lt_rate_fed)
            approaching.append({
                "ticker": h["ticker"],
                "days": days_to_lt,
                "gain": gain,
                "savings": potential_savings,
            })
    approaching.sort(key=lambda x: x["savings"], reverse=True)
    if approaching:
        total_savings = sum(a["savings"] for a in approaching)
        items = "; ".join(
            f"{a['ticker']} ({a['days']}d to go, "
            f"save ~${a['savings']:,.0f})"
            for a in approaching[:5]
        )
        recs.append({
            "priority": "high" if total_savings > 1000 else "medium",
            "category": "Wait for Long-Term Rates",
            "headline": f"Holding {len(approaching)} position(s) "
                        f"a bit longer saves ~${total_savings:,.0f} in tax",
            "detail": (
                f"These positions are within 90 days of the 1-year long-term "
                f"threshold. Selling now triggers ordinary-income rates; waiting "
                f"converts them to long-term capital gains rates: {items}."
            ),
            "dollar_impact": round(total_savings, 2),
        })

    # -------- 3. Charitable giving with appreciated stock --------
    big_winners = [
        h for h in holdings_data
        if h.get("unrealized_gain") and h["unrealized_gain"] >= 10000
        and (h.get("days_held") or 0) > 365
    ]
    big_winners.sort(key=lambda h: h["unrealized_gain"], reverse=True)
    if big_winners:
        total_appreciation = sum(h["unrealized_gain"] for h in big_winners[:3])
        lt_rate = _lt_rate_fed + (cfg.state_rate or 0)
        if cfg.apply_niit:
            lt_rate += 0.038
        savings = total_appreciation * lt_rate
        names = ", ".join(
            f"{h['ticker']} (${h['unrealized_gain']:,.0f} LT gain)"
            for h in big_winners[:3]
        )
        recs.append({
            "priority": "low",
            "category": "Charitable Giving",
            "headline": f"Donating appreciated long-term shares avoids capital gains tax",
            "detail": (
                f"You hold {len(big_winners)} long-term position(s) with substantial "
                f"unrealized gains (top: {names}). If you plan to give to charity this "
                f"year, donating these shares directly (vs. selling and donating cash) "
                f"avoids the embedded capital gain entirely AND provides a deduction "
                f"at fair market value — potentially saving ~${savings:,.0f} on the "
                f"top 3 alone if all were donated. Donor-advised funds make this easy."
            ),
            "dollar_impact": round(savings, 2),
        })

    # -------- 4. NIIT --------
    if cfg.apply_niit and ytd.net_total_gain > 5000:
        recs.append({
            "priority": "low",
            "category": "NIIT awareness",
            "headline": "Net Investment Income Tax (3.8%) applies to your gains",
            "detail": (
                "Your AGI puts you above the NIIT threshold ($200K single / $250K "
                "married filing jointly). The 3.8% surtax on net investment income "
                "is included in the YTD estimate above. Strategies to reduce: "
                "1) accelerate deductible expenses to reduce AGI, 2) bunch losses "
                "into the same year as gains, 3) hold gains positions into low-income "
                "retirement years."
            ),
            "dollar_impact": ytd.net_total_gain * 0.038,
        })

    # -------- 5. Loss carryforward --------
    if ytd.loss_carryforward > 0:
        recs.append({
            "priority": "medium",
            "category": "Loss Carryforward",
            "headline": f"${ytd.loss_carryforward:,.0f} in losses carries to next year",
            "detail": (
                f"YTD losses exceed gains plus the $3,000 ordinary-income limit. "
                f"The remaining ${ytd.loss_carryforward:,.0f} carries forward "
                f"indefinitely to offset future capital gains or up to $3,000/year "
                f"of ordinary income. Track this on Schedule D / Form 8949 next year."
            ),
            "dollar_impact": ytd.loss_carryforward * 0.20,
        })

    priority_order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: (priority_order[r["priority"]], -r["dollar_impact"]))
    return recs
