# Missed-Opportunity Analysis — capture + on-demand AI post-mortem

**Goal:** *grab* the missed-opportunity data in a benchmarked, analysable form, and add an
**on-demand AI post-mortem** that reasons over it for patterns and model improvements.

**Principle:** the capture and all numbers are deterministic code — auditable, key-free, and part
of every report. The AI is a *separate, on-demand* narrative layer: it reasons over the captured
data, never runs on the twice-daily report, and never produces the numbers.

> Note: an earlier version of this doc proposed a deterministic **A–F grade** as the middle layer.
> That was **dropped by choice** — the goal is to grab the data for study and let an AI analyse it,
> not to score each miss with a letter. No grade is computed anywhere.

---

## Layer 1 — Deterministic capture (always runs, in the report)

**Built (step 1).** On each `recs_history.json` → `tickers[T]` entry:

| Field | Purpose | Reuses |
|---|---|---|
| `sp_at_first`, `sp_last` | S&P level frozen at first sight + refreshed → **alpha** | `fetch_benchmark_returns()` (^GSPC), now returns `level` |
| `trough_after_peak`, `trough_date` | round-trip depth (durability) | the reserved `trough_price` hook |
| `ret_30d/90d/180d` + `_asof` | fixed-horizon outcomes | stamped by the first run inside each horizon's ±7-day window |

Exposed on every miss/avoided row by `compute_missed_opportunities` / `compute_avoided_losses`:
`alpha_pct` (excess vs S&P), `sp_return_pct`, `max_drawdown_from_peak`, `ret_30d/90d/180d`.
Backward-compatible — entries that predate capture return `None` (no invented alpha); a name's
alpha becomes computable one refresh after its first sighting.

**Available to add later (same pattern, not built):** dollar-regret inputs
(`portfolio_value_at_first` + a model-implied `model_weight`), trajectory `snapshots[]` + event
dates (`buy_bar_crossed_date`, `days_under_bar`), `catalyst_type` enum, `status`/`resolved_date`
lifecycle. Each just enriches what the post-mortem can reason over.

---

## Layer 2 — On-demand AI post-mortem (built)

- **Trigger:** `--analyze-misses` flag, or a direct `analyze_missed_opportunities_ai(...)` call.
  **Never** on the normal report — the twice-daily CI run never touches the API.
- **Input:** the *computed* miss rows (alpha, horizons, factors, why-missed) + base-rate insights
  + a sample of avoided losses. Signal, not raw prices — the model reasons, it doesn't recompute.
- **Output:** Markdown written to `missed_opp_analysis_<date>.md` and echoed to stdout. Not in the
  committed ledger, not in the HTML report.
- **Model:** `MISSED_OPP_MODEL` (default `claude-opus-5`), via the existing `_anthropic_client()`.
- **Guardrail (baked into the system prompt):** retrospective *model-improvement* analysis only —
  separates model gaps from execution gaps, judges on alpha not raw gain, and gives **no**
  personalized buy/sell advice.
- **Fallback:** no key, or an API failure/refusal → a clear message, returns `None`, report
  unaffected. Mirrors the `score_news_sentiment` "Claude when keyed" shape.

---

## CI wiring

- **Capture** runs inside the normal `analyze_portfolio` job — deterministic, key-free; the ledger
  persists across runs via the GitHub Actions cache (as today).
- **The post-mortem is NOT wired into CI** — it's opt-in (`--analyze-misses`) and run locally / on
  demand, so the scheduled report never incurs the cost, latency, or key dependency.

---

## Status

1. ✅ **Alpha + horizon capture** — done (step 1).
2. ✅ **On-demand AI post-mortem** (no grade) — done. `--analyze-misses` → dated Markdown analysis.
3. ⬜ **Optional richer capture** (dollar regret, trajectory snapshots + event dates, catalyst
   tags, lifecycle status) — additive; each gives the post-mortem more to work with.

Run it: `python analyze_portfolio.py --analyze-misses` (needs `ANTHROPIC_API_KEY`; set
`MISSED_OPP_MODEL` to a model your key can access).
