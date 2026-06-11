"""
server.py — Local dev server for Portfolio Analyzer
----------------------------------------------------
Serves report.html with per-section refresh buttons injected next to
each <h2> header. Clicking a section button re-fetches only that section's
data and swaps in the new table HTML — other sections stay untouched.

Sections:
  Quality Compounders   → /api/section/compounders
  Watchlists            → /api/section/watchlists
  ETFs & Thematic       → /api/section/etfs
  Tax                   → /api/section/tax

Usage:
    pip install flask
    python server.py
    open http://localhost:5000
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, stream_with_context

# ── Add project dir to path so we can import analyze_portfolio modules ──────────
PROJECT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_DIR))

app = Flask(__name__)

# ── Configuration ────────────────────────────────────────────────────────────────
REPORT_FILE = Path(os.environ.get("PORTFOLIO_REPORT", "report.html"))

# These are read from the environment or fall back to sensible defaults.
# They mirror what you'd pass on the CLI.
RH_SOURCE        = True   # set False to use CSV mode
INCLUDE_WATCHLISTS = True
SAVE_POSITIONS   = "positions.csv"

# ── Per-section job state ────────────────────────────────────────────────────────
# Each section has its own lock + running flag + log buffer + result.
_sections = {
    "compounders": {"lock": threading.Lock(), "running": False, "lines": [], "result": None},
    "watchlists":  {"lock": threading.Lock(), "running": False, "lines": [], "result": None},
    "etfs":        {"lock": threading.Lock(), "running": False, "lines": [], "result": None},
    "tax":         {"lock": threading.Lock(), "running": False, "lines": [], "result": None},
}

def _log(section: str, msg: str):
    _sections[section]["lines"].append(msg)
    print(f"[{section}] {msg}")


# ── Helpers — thin wrappers around analyze_portfolio internals ───────────────────

def _rh_login():
    import robinhood_source as rhs
    rhs.login(verbose=False)
    return rhs

def _analyze_rows(rows, use_rh_ratings=True, is_watchlist=False, log_fn=None):
    """Run analyze_position on each row, return list[PositionAnalysis]."""
    from analyze_portfolio import analyze_position
    results = []
    total = len(rows)
    for i, row in enumerate(rows, 1):
        ticker = row.get("ticker", "?")
        if log_fn:
            log_fn(f"[{i:>3}/{total}] {ticker} …")
        pa = analyze_position(row, use_robinhood_ratings=use_rh_ratings,
                              is_watchlist=is_watchlist)
        if log_fn:
            v = pa.verdict.label if pa.verdict else "?"
            log_fn(f"[{i:>3}/{total}] {ticker} → {v} ({pa.bucket})")
        results.append(pa)
    return results

def _render_section_html(section: str, results, watchlists=None, realized_ytd=None,
                          tax_lots=None) -> str:
    """
    Re-render just the table HTML for the requested section.
    Returns the inner HTML that replaces the existing table-wrap div.
    """
    from analyze_portfolio import (
        generate_html_report, _render_tax_section,
        PositionAnalysis,
    )

    if section == "compounders":
        # Borrow generate_html_report but extract only the compounders table
        # by rendering a mini-report with no watchlists/etfs/tax and
        # pulling the table-wrap div out.
        compounders = [r for r in results if r.bucket == "compounder"]
        return _render_table_only(compounders, section="compounders")

    if section == "etfs":
        thematics = [r for r in results if r.bucket == "thematic"]
        return _render_table_only(thematics, section="etfs")

    if section == "watchlists":
        # watchlists is dict[name -> list[PA]]
        return _render_watchlist_tables(watchlists or {})

    if section == "tax":
        flagged = [r for r in results if r.verdict and r.verdict.label in ("SELL","TRIM")]
        return _render_tax_section(flagged, results, realized_ytd)

    return "<p>Unknown section.</p>"


def _render_table_only(rows, section: str) -> str:
    """
    Render just the <div class='table-wrap'>…</div> for compounders or ETFs,
    reusing the cell-renderer helpers from analyze_portfolio.
    """
    from analyze_portfolio import (
        _tr_open, _td, _ticker_cell, _name_sector_cell, _position_cell,
        _cost_gain_cell, _price_target_cell, _range_trend_cell,
        _filter_dots, _score_cell, _rating_bar, _insider_cell,
        _verdict_cell, _VERDICT_ORDER,
        generate_html_report,
    )

    if not rows:
        return "<p style='color:#7f8c8d;padding:12px;'>No positions in this section.</p>"

    # Compute live portfolio total so pct columns work
    live_total = sum(r.live_market_value for r in rows if r.live_market_value)
    for r in rows:
        if r.live_market_value and live_total:
            r.live_pct_portfolio = r.live_market_value / live_total * 100

    rows_sorted = sorted(rows, key=lambda r: r.live_market_value or 0, reverse=True)

    if section == "compounders":
        headers = (
            "<th>Ticker</th><th>Name / Sector</th>"
            "<th class='num'>Position</th><th class='num'>Cost / Gain</th>"
            "<th class='num'>Price → Target</th><th>Range / Trend</th>"
            "<th>Quality (9)</th>"
            "<th class='num'>Composite</th>"
            "<th>Analyst Ratings</th><th>Insider 90d</th>"
            "<th>Verdict <span style='font-weight:400;font-size:10px;'>(score)</span></th>"
        )
    else:
        headers = (
            "<th>Ticker</th><th>Name</th>"
            "<th class='num'>Position</th><th class='num'>Cost / Gain</th>"
            "<th class='num'>Price → Target</th><th>Range / Trend</th>"
            "<th class='num'>Composite</th>"
            "<th>Analyst Ratings</th><th>Insider 90d</th>"
            "<th>Verdict <span style='font-weight:400;font-size:10px;'>(score)</span></th>"
        )

    html = "<div class='table-wrap'><table>\n<thead><tr>" + headers + "</tr></thead><tbody>\n"

    for r in rows_sorted:
        verdict_label = r.verdict.label if r.verdict else "—"
        rating_score = -1
        if r.rating_breakdown and r.rating_breakdown.get("total"):
            t = r.rating_breakdown["total"]
            rating_score = (r.rating_breakdown.get("buy", 0)
                            - r.rating_breakdown.get("sell", 0)) / t
        html += _tr_open(r)
        html += _td(_ticker_cell(r), r.ticker, "ticker")
        if section == "compounders":
            html += _td(_name_sector_cell(r), r.name)
        else:
            html += _td(r.name, r.name)
        html += _td(_position_cell(r), r.live_market_value or -1, "num")
        html += _td(_cost_gain_cell(r),
                    r.unrealized_gain if r.unrealized_gain is not None else -1e12, "num")
        html += _td(_price_target_cell(r),
                    r.upside_pct if r.upside_pct is not None else -1e6, "num")
        html += _td(_range_trend_cell(r),
                    r.week52_position if r.week52_position is not None else -1)
        if section == "compounders":
            passed = sum(1 for f in r.filters if f.passed)
            html += _td(
                f"{_filter_dots(r.filters)} <span style='color:var(--fg-muted);font-size:11px'>{passed}/9</span>",
                passed,
            )
        html += _td(_score_cell(r.composite_score, r.score_quality, r.score_growth,
                                r.score_value, r.score_analyst, r.score_insider),
                    r.composite_score if r.composite_score is not None else -1, "num")
        html += _td(_rating_bar(r.rating_breakdown, r.recommendation, r.num_analysts),
                    rating_score)
        html += _td(_insider_cell(r.insider_activity),
                    r.score_insider if r.score_insider is not None else -1)
        html += _td(_verdict_cell(r.verdict),
                    r.verdict.score if r.verdict and r.verdict.score is not None
                    else (100 - _VERDICT_ORDER.get(verdict_label, 99)))
        html += "</tr>\n"

    html += "</tbody></table></div>\n"
    return html


def _render_watchlist_tables(watchlists: dict) -> str:
    from analyze_portfolio import (
        _tr_open, _td, _ticker_cell, _name_sector_cell,
        _price_target_cell, _range_trend_cell, _filter_dots,
        _score_cell, _rating_bar, _insider_cell, _verdict_cell, _VERDICT_ORDER,
    )
    if not watchlists:
        return "<p style='color:#7f8c8d;padding:12px;'>No watchlist items.</p>"

    html = ""
    for wl_name, items in watchlists.items():
        if not items:
            continue
        items_sorted = sorted(items, key=lambda r: (
            _VERDICT_ORDER.get(r.verdict.label if r.verdict else "ERROR", 99),
            -(r.upside_pct or -1e6),
        ))
        html += (f"<h3 style='margin-top:24px;color:#34495e;'>"
                 f"📋 {wl_name} ({len(items_sorted)})</h3>\n")
        html += ("<div class='table-wrap'><table>\n<thead><tr>"
                 "<th>Ticker</th><th>Name / Sector</th>"
                 "<th class='num'>Price → Target</th><th>Range / Trend</th>"
                 "<th>Quality (9)</th><th class='num'>Composite</th>"
                 "<th>Analyst Ratings</th><th>Insider 90d</th>"
                 "<th>Verdict</th>"
                 "</tr></thead><tbody>\n")
        for r in items_sorted:
            passed = sum(1 for f in r.filters if f.passed) if r.filters else 0
            verdict_label = r.verdict.label if r.verdict else "—"
            rating_score = -1
            if r.rating_breakdown and r.rating_breakdown.get("total"):
                t = r.rating_breakdown["total"]
                rating_score = (r.rating_breakdown.get("buy", 0)
                                - r.rating_breakdown.get("sell", 0)) / t
            na = "<span style='color:var(--fg-faint);'>—</span>"
            html += _tr_open(r)
            html += _td(_ticker_cell(r), r.ticker, "ticker")
            html += _td(_name_sector_cell(r), r.name)
            html += _td(_price_target_cell(r),
                        r.upside_pct if r.upside_pct is not None else -1e6, "num")
            html += _td(_range_trend_cell(r),
                        r.week52_position if r.week52_position is not None else -1)
            quality_cell = (
                f"{_filter_dots(r.filters)} "
                f"<span style='color:var(--fg-muted);font-size:11px'>{passed}/9</span>"
                if r.filters else na
            )
            html += _td(quality_cell, passed if r.filters else -1)
            html += _td(_score_cell(r.composite_score, r.score_quality, r.score_growth,
                                    r.score_value, r.score_analyst, r.score_insider),
                        r.composite_score if r.composite_score is not None else -1, "num")
            html += _td(_rating_bar(r.rating_breakdown, r.recommendation, r.num_analysts),
                        rating_score)
            html += _td(_insider_cell(r.insider_activity),
                        r.score_insider if r.score_insider is not None else -1)
            html += _td(_verdict_cell(r.verdict),
                        r.verdict.score if r.verdict and r.verdict.score is not None
                        else (100 - _VERDICT_ORDER.get(verdict_label, 99)))
            html += "</tr>\n"
        html += "</tbody></table></div>\n"
    return html


# ── Section refresh workers ──────────────────────────────────────────────────────

def _run_section(section: str):
    """Background worker: fetch data + render HTML for one section."""
    s = _sections[section]

    def log(msg):
        _log(section, msg)

    try:
        from analyze_portfolio import analyze_position, compute_verdict_v2

        # ── Compounders ──────────────────────────────────────────────────────
        if section == "compounders":
            log("Logging into Robinhood…")
            rhs = _rh_login()
            log("Fetching positions…")
            rows = rhs.fetch_positions()
            comp_rows = rows   # re-analyze all; filter to compounders in render
            log(f"Got {len(rows)} positions. Analyzing…")
            results = _analyze_rows(comp_rows, use_rh_ratings=True, log_fn=log)
            log("Rendering compounders table…")
            html = _render_table_only(
                [r for r in results if r.bucket == "compounder"],
                section="compounders"
            )
            s["result"] = {"success": True, "html": html,
                           "message": f"Updated {len(results)} positions."}
            log(f"✓ Done — {len(results)} positions refreshed.")

        # ── ETFs ─────────────────────────────────────────────────────────────
        elif section == "etfs":
            log("Logging into Robinhood…")
            rhs = _rh_login()
            log("Fetching positions…")
            rows = rhs.fetch_positions()
            log(f"Got {len(rows)} positions. Analyzing…")
            results = _analyze_rows(rows, use_rh_ratings=True, log_fn=log)
            thematics = [r for r in results if r.bucket == "thematic"]
            log(f"Rendering ETF table ({len(thematics)} positions)…")
            html = _render_table_only(thematics, section="etfs")
            s["result"] = {"success": True, "html": html,
                           "message": f"Updated {len(thematics)} ETF/thematic positions."}
            log(f"✓ Done.")

        # ── Watchlists ───────────────────────────────────────────────────────
        elif section == "watchlists":
            log("Logging into Robinhood…")
            rhs = _rh_login()
            log("Fetching watchlists…")
            watchlist_lookup = rhs.fetch_watchlists()
            total = sum(len(v) for v in watchlist_lookup.values())
            log(f"Found {len(watchlist_lookup)} watchlists, {total} tickers. Analyzing…")

            ticker_cache = {}
            watchlists_analyzed = {}
            for wl_name, items in watchlist_lookup.items():
                analyzed = []
                for it in items:
                    t = it["ticker"]
                    if t not in ticker_cache:
                        log(f"  Analyzing {t}…")
                        pa = analyze_position(
                            {"ticker": t, "name": it["name"], "shares": 0,
                             "market_value": 0, "pct_portfolio": 0},
                            use_robinhood_ratings=True,
                            is_watchlist=True,
                        )
                        ticker_cache[t] = pa
                        v = pa.verdict.label if pa.verdict else "?"
                        log(f"  {t} → {v}")
                    analyzed.append(ticker_cache[t])
                if analyzed:
                    watchlists_analyzed[wl_name] = analyzed

            log("Rendering watchlist tables…")
            html = _render_watchlist_tables(watchlists_analyzed)
            s["result"] = {"success": True, "html": html,
                           "message": f"Updated {len(ticker_cache)} watchlist tickers."}
            log("✓ Done.")

        # ── Tax ──────────────────────────────────────────────────────────────
        elif section == "tax":
            log("Logging into Robinhood…")
            rhs = _rh_login()
            log("Fetching positions…")
            rows = rhs.fetch_positions()
            log(f"Analyzing {len(rows)} positions for tax…")
            results = _analyze_rows(rows, use_rh_ratings=True, log_fn=log)

            log("Reconstructing tax lots from order history…")
            tax_lots = rhs.fetch_tax_lots(verbose=False)
            log("Fetching YTD realized gains…")
            realized_ytd = rhs.fetch_realized_ytd(verbose=False)

            # Apply the portfolio-level verdict overlay (position size) before
            # selecting tax candidates — analyze_position alone can't flip an
            # overweight HOLD to TRIM, so without this those positions would
            # show TRIM in the report but be missing from the tax section.
            from analyze_portfolio import finalize_holding_verdicts
            log("Finalizing verdicts with portfolio context…")
            finalize_holding_verdicts(results)

            from tax_analysis import TaxConfig, analyze_tax, analyze_tax_with_lots
            tax_cfg = TaxConfig.from_env()
            flagged = [r for r in results
                       if r.verdict and r.verdict.label in ("SELL", "TRIM")]
            log(f"Running tax analysis on {len(flagged)} flagged positions…")
            for r in flagged:
                try:
                    lots = tax_lots.get(r.ticker) if tax_lots else None
                    if lots and r.current_price:
                        r.tax = analyze_tax_with_lots(
                            ticker=r.ticker, verdict=r.verdict.label,
                            lots=lots, current_price=r.current_price, cfg=tax_cfg,
                        )
                    else:
                        r.tax = analyze_tax(
                            ticker=r.ticker, verdict=r.verdict.label,
                            unrealized_gain=r.unrealized_gain,
                            position_opened=r.position_opened, cfg=tax_cfg,
                        )
                    log(f"  {r.ticker} ✓")
                except Exception as e:
                    log(f"  {r.ticker} ✗ {e}")

            from analyze_portfolio import _render_tax_section
            log("Rendering tax section…")
            html = _render_tax_section(
                [r for r in flagged if getattr(r, "tax", None)],
                results, realized_ytd,
            )
            s["result"] = {"success": True, "html": html,
                           "message": "Tax section updated."}
            log("✓ Done.")

    except Exception as e:
        import traceback
        log(f"✗ ERROR: {e}")
        log(traceback.format_exc())
        s["result"] = {"success": False, "html": None,
                       "message": f"Error: {e}"}
    finally:
        with s["lock"]:
            s["running"] = False


# ── API routes ───────────────────────────────────────────────────────────────────

@app.route("/api/section/<section>", methods=["POST"])
def section_refresh(section):
    if section not in _sections:
        return jsonify({"error": f"Unknown section '{section}'"}), 404

    s = _sections[section]
    with s["lock"]:
        if s["running"]:
            return jsonify({"running": True,
                            "message": "Already refreshing — please wait…"})
        s["running"] = True
        s["lines"]   = []
        s["result"]  = None

    threading.Thread(target=_run_section, args=(section,), daemon=True).start()
    return jsonify({"running": True, "message": f"Started refreshing {section}…"})


@app.route("/api/section/<section>/log")
def section_log(section):
    """SSE stream of log lines for a section refresh."""
    if section not in _sections:
        return jsonify({"error": "Unknown section"}), 404

    def generate():
        s = _sections[section]
        sent = 0
        while True:
            with s["lock"]:
                new_lines = s["lines"][sent:]
                running   = s["running"]
                result    = dict(s["result"]) if s["result"] else None

            for line in new_lines:
                yield f"data: {json.dumps({'line': line})}\n\n"
            sent += len(new_lines)

            if not running:
                yield f"event: done\ndata: {json.dumps(result or {})}\n\n"
                return

            time.sleep(0.25)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/section/<section>/status")
def section_status(section):
    if section not in _sections:
        return jsonify({"error": "Unknown section"}), 404
    s = _sections[section]
    with s["lock"]:
        return jsonify({
            "running": s["running"],
            "lines":   len(s["lines"]),
            "result":  s["result"],
        })


# ── Serve report ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if not REPORT_FILE.exists():
        return (
            "<h2>report.html not found.</h2>"
            "<p>Run <code>python analyze_portfolio.py ...</code> once to generate it, "
            "then reload.</p>"
        ), 404

    # Buttons are baked in by patch_section_refresh.py — serve as-is.
    return REPORT_FILE.read_text(encoding="utf-8")


def _inject_section_buttons_UNUSED(html: str) -> str:
    """Kept for reference only — buttons are now baked into the HTML by patch_section_refresh.py."""
    import re
    section_map = {
        "Quality Compounders": "compounders",
        "Watchlists":          "watchlists",
        "Stock Analysis":      "watchlists",
        "ETFs":                "etfs",
        "Tax-Aware":           "tax",
    }
    def make_btn(section_key):
        return (
            f" <button class='section-refresh-btn' "
            f"data-section='{section_key}' "
            f"title='Refresh this section with live data'>"
            f"🔄</button>"
            f"<span class='section-refresh-status' "
            f"id='status-{section_key}'></span>"
        )
    def replace_h2(m):
        full_tag = m.group(0)
        text     = m.group(1)
        for label, key in section_map.items():
            if label.lower() in text.lower():
                # Wrap the following table-wrap in a target div
                btn = make_btn(key)
                # Insert button before closing </h2>
                new_tag = full_tag.replace("</h2>", btn + "</h2>")
                # Also wrap the next .table-wrap sibling in a section-content div
                return new_tag
        return full_tag

    # Insert buttons into h2 tags
    html = re.sub(r"<h2[^>]*>(.*?)</h2>", replace_h2, html, flags=re.DOTALL)

    # Wrap each table-wrap in a named section-content div so JS can swap it
    # Strategy: assign section IDs based on position (compounders first, etc.)
    counter = [0]
    section_order = ["compounders", "watchlists", "etfs", "tax"]

    def wrap_table(m):
        idx = counter[0]
        if idx < len(section_order):
            sec = section_order[idx]
            counter[0] += 1
            return (f"<div class='section-content' id='content-{sec}'>"
                    + m.group(0) + "</div>")
        return m.group(0)

    html = re.sub(r"<div class='table-wrap'>", wrap_table, html)
    return html


# ── SECTION_REFRESH_JS removed ───────────────────────────────────────────────────
# The CSS + JS for section refresh buttons is now baked directly into
# analyze_portfolio.py by patch_section_refresh.py.  server.py only needs
# to serve the file and provide the /api/section/* endpoints.

_PLACEHOLDER = """
<style>
  /* ── Section refresh button ── */
  .section-refresh-btn {
    display: inline-flex; align-items: center; justify-content: center;
    margin-left: 10px;
    width: 28px; height: 28px;
    border-radius: 50%;
    background: var(--bg-card); border: 1px solid var(--border-medium);
    cursor: pointer; font-size: 14px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.1);
    transition: transform 0.15s, background 0.15s;
    vertical-align: middle;
  }
  .section-refresh-btn:hover   { background: var(--bg-card-hover); transform: scale(1.1); }
  .section-refresh-btn.running { cursor: wait; animation: _spin 0.8s linear infinite; }
  .section-refresh-btn.success { background: #d4edda; }
  .section-refresh-btn.error   { background: #f8d7da; }
  @keyframes _spin { to { transform: rotate(360deg); } }

  .section-refresh-status {
    margin-left: 8px;
    font-size: 12px; font-weight: 400;
    color: var(--fg-muted);
    vertical-align: middle;
  }

  /* ── Section log panel ── */
  .section-log-panel {
    margin: 6px 0 12px;
    background: #1a2028; color: #e8eaed;
    border: 1px solid #2d3540; border-radius: 8px;
    font-family: "SF Mono", SFMono-Regular, Consolas, monospace;
    font-size: 11px; line-height: 1.5;
    max-height: 180px; overflow-y: auto;
    padding: 8px 12px;
    display: none;
  }
  .section-log-panel.open { display: block; }
  .section-log-panel p { margin: 0; padding: 1px 0; white-space: pre-wrap; }
  .section-log-panel .l-ticker { color: #60a5fa; }
  .section-log-panel .l-add    { color: #4ade80; }
  .section-log-panel .l-sell   { color: #f87171; }
  .section-log-panel .l-trim   { color: #fb923c; }
  .section-log-panel .l-phase  { color: #fbbf24; font-weight: 700; }
  .section-log-panel .l-error  { color: #f87171; }

  /* ── Overlay shimmer while section is loading ── */
  .section-content.loading {
    position: relative; pointer-events: none;
  }
  .section-content.loading::after {
    content: '';
    position: absolute; inset: 0;
    background: rgba(255,255,255,0.55);
    border-radius: 10px;
    animation: shimmer 1.2s ease-in-out infinite alternate;
  }
  [data-theme="dark"] .section-content.loading::after {
    background: rgba(0,0,0,0.45);
  }
  @keyframes shimmer { from { opacity: 0.3; } to { opacity: 0.7; } }
</style>

<script>
(function() {

  // ── Classify log line ──
  function cls(text) {
    if (/\[\s*\d+\/\d+\]/.test(text)) {
      if (/→ ADD/i.test(text))  return 'l-ticker l-add';
      if (/→ SELL/i.test(text)) return 'l-ticker l-sell';
      if (/→ TRIM/i.test(text)) return 'l-ticker l-trim';
      return 'l-ticker';
    }
    if (/^\[robinhood\]|^\[tax\]|^\[screen\]|Logging|Fetching|Rendering|Analyzing|Got \d/i.test(text))
      return 'l-phase';
    if (/error|exception|✗/i.test(text)) return 'l-error';
    return '';
  }

  // ── For each section refresh button ──
  document.querySelectorAll('.section-refresh-btn').forEach(function(btn) {
    var section    = btn.getAttribute('data-section-refresh');
    var statusEl   = document.getElementById('status-' + section);
    var contentEl  = document.getElementById('content-' + section);
    var logPanel   = null;   // created on first use
    var evtSource  = null;

    function getOrCreateLog() {
      if (logPanel) return logPanel;
      logPanel = document.createElement('div');
      logPanel.className = 'section-log-panel';
      // Insert right after the h2 that contains this button
      var h2 = btn.closest('h2');
      if (h2 && h2.parentNode) {
        h2.parentNode.insertBefore(logPanel, h2.nextSibling);
      }
      return logPanel;
    }

    function appendLog(text) {
      var panel = getOrCreateLog();
      panel.classList.add('open');
      var p = document.createElement('p');
      p.className = cls(text);
      p.textContent = text;
      panel.appendChild(p);
      panel.scrollTop = panel.scrollHeight;
    }

    function setRunning() {
      btn.classList.add('running');
      btn.classList.remove('success', 'error');
      btn.disabled = true;
      if (statusEl) statusEl.textContent = 'Fetching live data…';
      if (contentEl) contentEl.classList.add('loading');
      var panel = getOrCreateLog();
      panel.innerHTML = '';
      panel.classList.add('open');
    }

    function setDone(success, message, newHtml) {
      btn.classList.remove('running');
      btn.classList.add(success ? 'success' : 'error');
      btn.disabled = false;
      if (statusEl) {
        statusEl.textContent = success ? '✓ Updated' : '✗ Error';
        setTimeout(function() { statusEl.textContent = ''; }, 4000);
      }
      if (contentEl) contentEl.classList.remove('loading');

      if (success && newHtml) {
        // Swap in the new table HTML
        if (contentEl) {
          contentEl.innerHTML = newHtml;
        }
        // Re-attach sort listeners on the new table
        attachSortListeners(contentEl);
        // Re-run the filter bar so new rows respect active filters
        if (window._applyFilters) window._applyFilters();
        // Fade the success state back to neutral after 3s
        setTimeout(function() { btn.classList.remove('success'); }, 3000);
        // Close log after a moment
        setTimeout(function() {
          if (logPanel) logPanel.classList.remove('open');
        }, 2500);
      }

      appendLog((success ? '✓ ' : '✗ ') + message);
    }

    btn.addEventListener('click', function() {
      if (evtSource) { evtSource.close(); evtSource = null; }
      setRunning();
      appendLog('▶ Starting ' + section + ' refresh…');

      fetch('/api/section/' + section, { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          appendLog(data.message || 'Running…');

          evtSource = new EventSource('/api/section/' + section + '/log');

          evtSource.onmessage = function(e) {
            try { appendLog(JSON.parse(e.data).line); } catch(_) {}
          };

          evtSource.addEventListener('done', function(e) {
            evtSource.close(); evtSource = null;
            try {
              var result = JSON.parse(e.data);
              setDone(result.success === true, result.message || '', result.html || '');
            } catch(_) {
              setDone(false, 'Unexpected server response.', '');
            }
          });

          evtSource.onerror = function() {
            evtSource.close(); evtSource = null;
            setDone(false, 'Lost connection to server.', '');
          };
        })
        .catch(function(e) {
          setDone(false, 'Could not reach server: ' + e, '');
        });
    });
  });

  // ── Re-attach column-sort listeners to a (newly swapped) container ──
  function attachSortListeners(container) {
    if (!container) return;
    container.querySelectorAll('table').forEach(function(table) {
      var headers = table.querySelectorAll('th');
      headers.forEach(function(th, idx) {
        th.style.cursor = 'pointer';
        th.addEventListener('click', function() {
          var tbody = table.querySelector('tbody');
          if (!tbody) return;
          var rows = Array.from(tbody.querySelectorAll('tr'));
          var asc = th.classList.contains('sort-desc');
          headers.forEach(function(h) {
            h.classList.remove('sort-asc','sort-desc');
          });
          th.classList.add(asc ? 'sort-asc' : 'sort-desc');
          rows.sort(function(a, b) {
            var av = parseFloat(a.children[idx] && a.children[idx].getAttribute('data-sort'));
            var bv = parseFloat(b.children[idx] && b.children[idx].getAttribute('data-sort'));
            if (isNaN(av)) av = -Infinity;
            if (isNaN(bv)) bv = -Infinity;
            return asc ? av - bv : bv - av;
          });
          rows.forEach(function(r) { tbody.appendChild(r); });
        });
      });
    });
  }

  // Expose _applyFilters hook for the filter bar to re-run after a section swap.
  // The filter bar script sets window._applyFilters = applyFilters internally;
  // we just need to call it here if it exists.

"""  # end _PLACEHOLDER (unused)


# ── Entry point ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Portfolio Analyzer — Section Refresh Server")
    print("=" * 60)
    print(f"  Report file : {REPORT_FILE.resolve()}")
    print(f"  URL         : http://localhost:5000")
    print()
    print("  Section endpoints:")
    for s in _sections:
        print(f"    POST /api/section/{s}")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
