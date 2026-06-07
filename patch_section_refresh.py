"""
patch_section_refresh.py
------------------------
Run this once to patch analyze_portfolio.py so the generated report.html
has per-section 🔄 refresh buttons baked in.

Usage:
    python patch_section_refresh.py
    python patch_section_refresh.py --file /path/to/analyze_portfolio.py

Creates a backup at analyze_portfolio.py.bak before modifying.
Safe to re-run — checks if patch is already applied.

v2 fixes:
  - Removed duplicate button (server.py no longer injects one)
  - Fixed filters not working after section swap
  - Fixed sorting broken on string data-sort values (watchlists)
"""

import argparse
import shutil
import sys
from pathlib import Path

PATCHES = []

# ──────────────────────────────────────────────────────────────────────────────
# PATCH 1 — CSS for section buttons, appended just before </style>
# ──────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    "  @media (max-width: 900px) {{\n"
    "    body {{ padding: 18px 12px 32px; font-size: 13px; }}\n"
    "    h1 {{ font-size: 22px; }}\n"
    "    h2 {{ font-size: 17px; }}\n"
    "    .filter-bar input[type=\"text\"] {{ min-width: 160px; }}\n"
    "    .filter-group-label {{ min-width: auto; }}\n"
    "    table {{ font-size: 12px; }}\n"
    "    thead th, td {{ padding: 8px 6px; }}\n"
    "    .theme-toggle {{ top: 12px; right: 12px;\n"
    "                     width: 34px; height: 34px; font-size: 16px; }}\n"
    "  }}\n"
    "</style>",

    "  @media (max-width: 900px) {{\n"
    "    body {{ padding: 18px 12px 32px; font-size: 13px; }}\n"
    "    h1 {{ font-size: 22px; }}\n"
    "    h2 {{ font-size: 17px; }}\n"
    "    .filter-bar input[type=\"text\"] {{ min-width: 160px; }}\n"
    "    .filter-group-label {{ min-width: auto; }}\n"
    "    table {{ font-size: 12px; }}\n"
    "    thead th, td {{ padding: 8px 6px; }}\n"
    "    .theme-toggle {{ top: 12px; right: 12px;\n"
    "                     width: 34px; height: 34px; font-size: 16px; }}\n"
    "  }}\n"
    "\n"
    "  /* ---------- Section refresh buttons ---------- */\n"
    "  .section-refresh-btn {{\n"
    "    display: inline-flex; align-items: center; justify-content: center;\n"
    "    margin-left: 10px; width: 26px; height: 26px;\n"
    "    border-radius: 50%;\n"
    "    background: var(--bg-card); border: 1px solid var(--border-medium);\n"
    "    cursor: pointer; font-size: 13px;\n"
    "    box-shadow: var(--shadow-card);\n"
    "    transition: transform 0.15s, background 0.15s;\n"
    "    vertical-align: middle;\n"
    "  }}\n"
    "  .section-refresh-btn:hover   {{ background: var(--bg-card-hover); transform: scale(1.12); }}\n"
    "  .section-refresh-btn.running {{ cursor: wait; animation: _sec-spin 0.8s linear infinite; }}\n"
    "  .section-refresh-btn.success {{ background: var(--bg-chip-green); }}\n"
    "  .section-refresh-btn.error   {{ background: var(--bg-chip-red); }}\n"
    "  @keyframes _sec-spin {{ to {{ transform: rotate(360deg); }} }}\n"
    "  .section-refresh-status {{\n"
    "    margin-left: 8px; font-size: 12px; font-weight: 400;\n"
    "    color: var(--fg-muted); vertical-align: middle;\n"
    "  }}\n"
    "  .section-log-panel {{\n"
    "    margin: 4px 0 10px;\n"
    "    background: #1a2028; color: #e8eaed;\n"
    "    border: 1px solid #2d3540; border-radius: 8px;\n"
    "    font-family: \"SF Mono\", SFMono-Regular, Consolas, monospace;\n"
    "    font-size: 11px; line-height: 1.5;\n"
    "    max-height: 160px; overflow-y: auto;\n"
    "    padding: 8px 12px; display: none;\n"
    "  }}\n"
    "  .section-log-panel.open {{ display: block; }}\n"
    "  .section-log-panel p {{ margin: 0; padding: 1px 0; white-space: pre-wrap; }}\n"
    "  .l-phase  {{ color: #fbbf24; font-weight: 700; }}\n"
    "  .l-ticker {{ color: #60a5fa; }}\n"
    "  .l-add    {{ color: #4ade80; }}\n"
    "  .l-sell   {{ color: #f87171; }}\n"
    "  .l-trim   {{ color: #fb923c; }}\n"
    "  .l-error  {{ color: #f87171; }}\n"
    "  .section-content.loading {{\n"
    "    position: relative; pointer-events: none;\n"
    "  }}\n"
    "  .section-content.loading::after {{\n"
    "    content: ''; position: absolute; inset: 0;\n"
    "    background: rgba(255,255,255,0.5); border-radius: 10px;\n"
    "    animation: _shimmer 1.2s ease-in-out infinite alternate;\n"
    "  }}\n"
    "  [data-theme=\"dark\"] .section-content.loading::after {{\n"
    "    background: rgba(0,0,0,0.4);\n"
    "  }}\n"
    "  @keyframes _shimmer {{ from {{ opacity: 0.3; }} to {{ opacity: 0.7; }} }}\n"
    "</style>",
))

# ──────────────────────────────────────────────────────────────────────────────
# PATCH 2 — Compounders h2: add button + log div + open section-content wrapper
# ──────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    '        html += "<h2>Quality Compounders</h2>\\n"\n'
    '        html += "<div class=\'table-wrap\'><table>\\n<thead><tr>"',

    '        html += (\n'
    '            "<h2>Quality Compounders "\n'
    '            "<button class=\'section-refresh-btn\' data-section=\'compounders\' "\n'
    '            "title=\'Refresh with live data\'>\\U0001f504</button>"\n'
    '            "<span class=\'section-refresh-status\' id=\'status-compounders\'></span>"\n'
    '            "</h2>\\n"\n'
    '            "<div id=\'log-compounders\' class=\'section-log-panel\'></div>\\n"\n'
    '        )\n'
    '        html += "<div class=\'section-content\' id=\'content-compounders\'>\\n"\n'
    '        html += "<div class=\'table-wrap\'><table>\\n<thead><tr>"',
))

# ──────────────────────────────────────────────────────────────────────────────
# PATCH 3 — Compounders: close section-content after table
# ──────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    '        html += "</tbody></table></div>\\n"\n'
    '\n'
    '\n'
    '    # ---------- Watchlist sections ----------',

    '        html += "</tbody></table></div>\\n"\n'
    '        html += "</div>\\n"  # close section-content#content-compounders\n'
    '\n'
    '\n'
    '    # ---------- Watchlist sections ----------',
))

# ──────────────────────────────────────────────────────────────────────────────
# PATCH 4 — Watchlists h2: add button + log div
# ──────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    "        html += f\"<h2 style='margin-top:{'48px' if has_holdings else '24px'};'>{wl_title}</h2>\\n\"",

    "        html += (\n"
    "            f\"<h2 style='margin-top:{'48px' if has_holdings else '24px'};'>\"\n"
    "            f\"{wl_title} \"\n"
    "            \"<button class='section-refresh-btn' data-section='watchlists' \"\n"
    "            \"title='Refresh with live data'>\\U0001f504</button>\"\n"
    "            \"<span class='section-refresh-status' id='status-watchlists'></span>\"\n"
    "            \"</h2>\\n\"\n"
    "            \"<div id='log-watchlists' class='section-log-panel'></div>\\n\"\n"
    "        )",
))

# ──────────────────────────────────────────────────────────────────────────────
# PATCH 5 — Watchlists loop: enumerate + open section-content on first iteration
# ──────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    '        for wl_name, items in watchlists.items():\n'
    '            # Filter out anything already in holdings (avoids duplicate rows)\n'
    '            items = [r for r in items if r.ticker not in held_tickers]',

    '        for wl_idx, (wl_name, items) in enumerate(watchlists.items()):\n'
    '            # Filter out anything already in holdings (avoids duplicate rows)\n'
    '            items = [r for r in items if r.ticker not in held_tickers]',
))

PATCHES.append((
    "            html += f\"<h3 style='margin-top:24px;color:#34495e;'>📋 {wl_name} ({len(items)})</h3>\\n\"\n"
    "            html += \"<div class='table-wrap'><table>\\n<thead><tr>\"",

    "            if wl_idx == 0:\n"
    "                html += \"<div class='section-content' id='content-watchlists'>\\n\"\n"
    "            html += f\"<h3 style='margin-top:24px;color:#34495e;'>📋 {wl_name} ({len(items)})</h3>\\n\"\n"
    "            html += \"<div class='table-wrap'><table>\\n<thead><tr>\"",
))

# ──────────────────────────────────────────────────────────────────────────────
# PATCH 6 — Watchlists: close section-content after loop
# ──────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    '            html += "</tbody></table></div>\\n"\n'
    '\n'
    '    # ---------- Screening section (passed-the-screen universe) ----------',

    '            html += "</tbody></table></div>\\n"\n'
    '        html += "</div>\\n"  # close section-content#content-watchlists\n'
    '\n'
    '    # ---------- Screening section (passed-the-screen universe) ----------',
))

# ──────────────────────────────────────────────────────────────────────────────
# PATCH 7 — ETFs h2: add button + log div + open section-content wrapper
# ──────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    '        html += "<h2>ETFs &amp; Thematic Positions</h2>\\n"\n'
    '        html += "<div class=\'table-wrap\'><table>\\n<thead><tr>"',

    '        html += (\n'
    '            "<h2>ETFs &amp; Thematic Positions "\n'
    '            "<button class=\'section-refresh-btn\' data-section=\'etfs\' "\n'
    '            "title=\'Refresh with live data\'>\\U0001f504</button>"\n'
    '            "<span class=\'section-refresh-status\' id=\'status-etfs\'></span>"\n'
    '            "</h2>\\n"\n'
    '            "<div id=\'log-etfs\' class=\'section-log-panel\'></div>\\n"\n'
    '        )\n'
    '        html += "<div class=\'section-content\' id=\'content-etfs\'>\\n"\n'
    '        html += "<div class=\'table-wrap\'><table>\\n<thead><tr>"',
))

# ──────────────────────────────────────────────────────────────────────────────
# PATCH 8 — ETFs: close section-content after table
# ──────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    '        html += "</tbody></table></div>\\n"\n'
    '    # ---------- Tax analysis section (moved to bottom by request) ----------',

    '        html += "</tbody></table></div>\\n"\n'
    '        html += "</div>\\n"  # close section-content#content-etfs\n'
    '    # ---------- Tax analysis section (moved to bottom by request) ----------',
))

# ──────────────────────────────────────────────────────────────────────────────
# PATCH 9 — Tax h2: add button + log div + open section-content wrapper
# ──────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    '    html = "<h2 style=\'margin-top:48px;\'>Tax-Aware Trim Guidance</h2>\\n"',

    '    html = (\n'
    '        "<h2 style=\'margin-top:48px;\'>Tax-Aware Trim Guidance "\n'
    '        "<button class=\'section-refresh-btn\' data-section=\'tax\' "\n'
    '        "title=\'Refresh with live data\'>\\U0001f504</button>"\n'
    '        "<span class=\'section-refresh-status\' id=\'status-tax\'></span>"\n'
    '        "</h2>\\n"\n'
    '        "<div id=\'log-tax\' class=\'section-log-panel\'></div>\\n"\n'
    '        "<div class=\'section-content\' id=\'content-tax\'>\\n"\n'
    '    )',
))

# ──────────────────────────────────────────────────────────────────────────────
# PATCH 10 — Tax: close section-content at end of _render_tax_section
# ──────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    '        html += "</div>\\n"\n'
    '\n'
    '    return html\n'
    '\n'
    '\ndef generate_html_report(',

    '        html += "</div>\\n"\n'
    '\n'
    '    html += "</div>\\n"  # close section-content#content-tax\n'
    '    return html\n'
    '\n'
    '\ndef generate_html_report(',
))

# ──────────────────────────────────────────────────────────────────────────────
# PATCH 11 — JS: expose _applyFilters inside the filter IIFE (fix #2)
#            + add section refresh JS with fixed sort (fix #3)
#
# FIX 1 (duplicate button): server.py's _inject_section_buttons() is removed
#        from server.py separately — the HTML now has exactly one button per section.
#
# FIX 2 (filters): window._applyFilters is assigned INSIDE the filter IIFE,
#        before it closes, so it's always defined when section refresh calls it.
#
# FIX 3 (sort): attachSort uses the same sortableValue() logic as the original
#        sort script — handles both numeric and string data-sort values correctly.
# ──────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    # FIND: the closing of the filter IIFE + end of script
    "  searchInput.addEventListener('input', applyFilters);\n"
    "  applyFilters();\n"
    "})();\n"
    "</script>\n"
    "</body></html>\n",

    # REPLACE: expose applyFilters globally INSIDE the IIFE, then add section JS
    "  searchInput.addEventListener('input', applyFilters);\n"
    "  applyFilters();\n"
    "  // Expose so section-refresh can re-run filters after swapping table HTML\n"
    "  window._applyFilters = applyFilters;\n"
    "})();\n"
    "\n"
    "/* ---------- Section refresh ---------- */\n"
    "(function() {{\n"
    "  var evtSources = {{}};\n"
    "\n"
    "  // Classify log line for colour coding\n"
    "  function cls(text) {{\n"
    "    if (/\\[\\s*\\d+\\/\\d+\\]/.test(text)) {{\n"
    "      if (/→ ADD/i.test(text))  return 'l-ticker l-add';\n"
    "      if (/→ SELL/i.test(text)) return 'l-ticker l-sell';\n"
    "      if (/→ TRIM/i.test(text)) return 'l-ticker l-trim';\n"
    "      return 'l-ticker';\n"
    "    }}\n"
    "    if (/Logging|Fetching|Rendering|Analyzing|Got \\d|\\[robinhood\\]|\\[tax\\]/i.test(text))\n"
    "      return 'l-phase';\n"
    "    if (/error|✗/i.test(text)) return 'l-error';\n"
    "    return '';\n"
    "  }}\n"
    "\n"
    "  function appendLog(section, text) {{\n"
    "    var panel = document.getElementById('log-' + section);\n"
    "    if (!panel) return;\n"
    "    panel.classList.add('open');\n"
    "    var p = document.createElement('p');\n"
    "    p.className = cls(text);\n"
    "    p.textContent = text;\n"
    "    panel.appendChild(p);\n"
    "    panel.scrollTop = panel.scrollHeight;\n"
    "  }}\n"
    "\n"
    "  function setRunning(btn, section) {{\n"
    "    btn.classList.add('running');\n"
    "    btn.classList.remove('success', 'error');\n"
    "    btn.disabled = true;\n"
    "    var st = document.getElementById('status-' + section);\n"
    "    if (st) st.textContent = 'Fetching live data…';\n"
    "    var content = document.getElementById('content-' + section);\n"
    "    if (content) content.classList.add('loading');\n"
    "    var panel = document.getElementById('log-' + section);\n"
    "    if (panel) {{ panel.innerHTML = ''; panel.classList.add('open'); }}\n"
    "  }}\n"
    "\n"
    "  function setDone(btn, section, success, message, newHtml) {{\n"
    "    btn.classList.remove('running');\n"
    "    btn.classList.add(success ? 'success' : 'error');\n"
    "    btn.disabled = false;\n"
    "    var st = document.getElementById('status-' + section);\n"
    "    if (st) {{\n"
    "      st.textContent = success ? '✓ Updated' : '✗ Error';\n"
    "      setTimeout(function() {{ st.textContent = ''; }}, 4000);\n"
    "    }}\n"
    "    var content = document.getElementById('content-' + section);\n"
    "    if (content) content.classList.remove('loading');\n"
    "    if (success && newHtml && content) {{\n"
    "      content.innerHTML = newHtml;\n"
    "      attachSort(content);        // re-wire sort on new rows\n"
    "      if (window._applyFilters) window._applyFilters();  // re-run active filters\n"
    "      setTimeout(function() {{ btn.classList.remove('success'); }}, 3000);\n"
    "      setTimeout(function() {{\n"
    "        var panel = document.getElementById('log-' + section);\n"
    "        if (panel) panel.classList.remove('open');\n"
    "      }}, 2500);\n"
    "    }}\n"
    "    appendLog(section, (success ? '✓ ' : '✗ ') + message);\n"
    "  }}\n"
    "\n"
    "  // FIX: use same sortableValue logic as the original sort script so\n"
    "  // string data-sort values (ticker names, sector names) sort correctly.\n"
    "  function sortableValue(td) {{\n"
    "    var s = td ? td.getAttribute('data-sort') : null;\n"
    "    if (s === null || s === '') return null;\n"
    "    var n = parseFloat(s);\n"
    "    return isNaN(n) ? s.toLowerCase() : n;\n"
    "  }}\n"
    "\n"
    "  function attachSort(container) {{\n"
    "    if (!container) return;\n"
    "    container.querySelectorAll('table').forEach(function(table) {{\n"
    "      var ths = table.querySelectorAll('th');\n"
    "      ths.forEach(function(th, idx) {{\n"
    "        th.style.cursor = 'pointer';\n"
    "        th.addEventListener('click', function() {{\n"
    "          var tbody = table.querySelector('tbody');\n"
    "          if (!tbody) return;\n"
    "          var rows = Array.from(tbody.querySelectorAll('tr'));\n"
    "          // First click = descending (biggest first); second = ascending\n"
    "          var asc = th.classList.contains('sort-desc');\n"
    "          ths.forEach(function(h) {{ h.classList.remove('sort-asc', 'sort-desc'); }});\n"
    "          th.classList.add(asc ? 'sort-asc' : 'sort-desc');\n"
    "          rows.sort(function(a, b) {{\n"
    "            var av = sortableValue(a.children[idx]);\n"
    "            var bv = sortableValue(b.children[idx]);\n"
    "            // Nulls always sink to the bottom\n"
    "            if (av === null && bv === null) return 0;\n"
    "            if (av === null) return 1;\n"
    "            if (bv === null) return -1;\n"
    "            var cmp;\n"
    "            if (typeof av === 'number' && typeof bv === 'number') {{\n"
    "              cmp = av - bv;\n"
    "            }} else {{\n"
    "              cmp = String(av).localeCompare(String(bv));\n"
    "            }}\n"
    "            return asc ? cmp : -cmp;\n"
    "          }});\n"
    "          rows.forEach(function(r) {{ tbody.appendChild(r); }});\n"
    "        }});\n"
    "      }});\n"
    "    }});\n"
    "  }}\n"
    "\n"
    "  document.querySelectorAll('.section-refresh-btn').forEach(function(btn) {{\n"
    "    var section = btn.getAttribute('data-section');\n"
    "    btn.addEventListener('click', function() {{\n"
    "      if (evtSources[section]) {{ evtSources[section].close(); evtSources[section] = null; }}\n"
    "      setRunning(btn, section);\n"
    "      appendLog(section, '▶ Starting ' + section + ' refresh…');\n"
    "      fetch('/api/section/' + section, {{ method: 'POST' }})\n"
    "        .then(function(r) {{ return r.json(); }})\n"
    "        .then(function(data) {{\n"
    "          appendLog(section, data.message || 'Running…');\n"
    "          var es = new EventSource('/api/section/' + section + '/log');\n"
    "          evtSources[section] = es;\n"
    "          es.onmessage = function(e) {{\n"
    "            try {{ appendLog(section, JSON.parse(e.data).line); }} catch(_) {{}}\n"
    "          }};\n"
    "          es.addEventListener('done', function(e) {{\n"
    "            es.close(); evtSources[section] = null;\n"
    "            try {{\n"
    "              var res = JSON.parse(e.data);\n"
    "              setDone(btn, section, res.success === true, res.message || '', res.html || '');\n"
    "            }} catch(_) {{ setDone(btn, section, false, 'Unexpected response.', ''); }}\n"
    "          }});\n"
    "          es.onerror = function() {{\n"
    "            es.close(); evtSources[section] = null;\n"
    "            setDone(btn, section, false, 'Lost connection to server.', '');\n"
    "          }};\n"
    "        }})\n"
    "        .catch(function(e) {{\n"
    "          setDone(btn, section, false, 'Could not reach server: ' + e, '');\n"
    "        }});\n"
    "    }});\n"
    "  }});\n"
    "}})();\n"
    "</script>\n"
    "</body></html>\n",
))


# ── Apply patches ─────────────────────────────────────────────────────────────

def apply_patches(source: str) -> tuple[str, list[str]]:
    applied = []
    skipped = []
    result = source
    for i, (find, replace) in enumerate(PATCHES, 1):
        if find in result:
            result = result.replace(find, replace, 1)
            applied.append(f"  Patch {i:>2} ✓")
        elif replace in result:
            skipped.append(f"  Patch {i:>2} — already applied, skipped")
        else:
            skipped.append(f"  Patch {i:>2} ✗ NOT FOUND — check for version mismatch")
    return result, applied + skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="analyze_portfolio.py",
                    help="Path to analyze_portfolio.py (default: ./analyze_portfolio.py)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would change without writing anything")
    args = ap.parse_args()

    src = Path(args.file)
    if not src.exists():
        print(f"ERROR: {src} not found.", file=sys.stderr)
        sys.exit(1)

    original = src.read_text(encoding="utf-8")

    if "section-refresh-btn" in original:
        print("✓ analyze_portfolio.py already contains section refresh buttons.")
        print("  Nothing to do. To re-apply, restore from .bak first.")
        sys.exit(0)

    patched, log = apply_patches(original)

    print("Patch results:")
    for line in log:
        print(line)

    errors = [l for l in log if "NOT FOUND" in l]
    if errors:
        print(f"\n⚠  {len(errors)} patch(es) could not be applied.")
        print("   The file was NOT modified.")
        sys.exit(1)

    if args.dry_run:
        print("\n--dry-run: no files written.")
        sys.exit(0)

    bak = src.with_suffix(".py.bak")
    shutil.copy2(src, bak)
    print(f"\nBackup written to {bak}")
    src.write_text(patched, encoding="utf-8")
    print(f"✓ {src} patched successfully.")
    print()
    print("Next steps:")
    print("  1. python server.py")
    print("  2. python analyze_portfolio.py --source robinhood ... --out report.html")
    print("  3. open http://localhost:5000")


if __name__ == "__main__":
    main()
