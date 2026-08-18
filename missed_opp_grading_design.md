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

- **Trigger (opt-in, three ways):** the report header's **"🔍 Analyze with AI" toggle** (sticky,
  green-when-on, beside **Tax**) → the next **Refresh data** dispatch sends `analyze_misses=true`;
  the workflow's `analyze_misses` checkbox on a manual run; or the `--analyze-misses` CLI flag /
  a direct `analyze_missed_opportunities_ai(...)` call. **Off by default** — a normal/scheduled run
  never touches the API.
- **Input:** the *computed* miss rows (alpha, horizons, factors, why-missed) + base-rate insights
  + a sample of avoided losses. Signal, not raw prices — the model reasons, it doesn't recompute.
- **Output:** an **AI Post-Mortem panel embedded in the Missed Opportunities section** of the
  report (so it rides GitHub Pages), plus a keepable `missed_opp_analysis_<date>.md` and stdout.
  If the toggle was on but the call failed, the panel shows a clear "unavailable" note instead.
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
- **The post-mortem is wired into CI as an opt-in:** `portfolio.yml` has an `analyze_misses`
  input; the report's toggle sets it on the `workflow_dispatch`, and the job passes
  `--analyze-misses` only when it's checked. A scheduled/normal run never incurs the cost, latency,
  or key dependency — but a report you *asked* to analyse embeds the write-up. It uses the existing
  `ANTHROPIC_API_KEY` secret; `MISSED_OPP_MODEL` is an optional repo variable.
- **The toggle is sticky** (like Tax): on until turned off, and each on-run spends tokens — so with
  the Auto (30-min) refresh on, turn it off when done.

---

## Status

1. ✅ **Alpha + horizon capture** — done (step 1).
2. ✅ **On-demand AI post-mortem** (no grade) — done.
3. ✅ **In-report delivery** — "Analyze with AI" header toggle (Tax-style, green-when-on) +
   `analyze_misses` workflow input; the write-up embeds in the Missed Opportunities section.
4. ⬜ **Optional richer capture** (dollar regret, trajectory snapshots + event dates, catalyst
   tags, lifecycle status) — additive; each gives the post-mortem more to work with.

Use it (from the GitHub Pages report): toggle **🔍 Analyze with AI** on → **Refresh data**. Or
locally: `python analyze_portfolio.py --analyze-misses`. Needs `ANTHROPIC_API_KEY`; set the
`MISSED_OPP_MODEL` repo variable to a model your key can access if it lacks `claude-opus-5`.
