"""
fix_cloud_refresh.py
--------------------
Replaces the local server-dependent refresh buttons in analyze_portfolio.py
with a GitHub Actions trigger. Clicking any refresh button:
  1. Calls GitHub API to trigger a new workflow run
  2. Polls run status every 10 seconds with a live progress bar
  3. Auto-reloads the page when the new report is deployed (~10 min)

Usage:
    python fix_cloud_refresh.py
    python fix_cloud_refresh.py --file /path/to/analyze_portfolio.py

You need two GitHub secrets already set (from setup_github.py):
  GITHUB_REPO   = yourusername/portfolio-analyzer
  GITHUB_TOKEN  = your personal access token (needs workflow scope)

Add them in GitHub: repo → Settings → Secrets → Actions → New secret.
"""

import argparse
import shutil
import sys
from pathlib import Path


# ── What to find and replace ──────────────────────────────────────────────────

# 1. Remove the top refresh button CSS + HTML element
FIND_REFRESH_BTN_CSS = """\
  /* ---------- Refresh button (floating, left of theme toggle) ---------- */
  .refresh-btn {{ position: fixed; top: 20px; right: 66px;
                  height: 38px; padding: 0 14px;
                  border-radius: 19px; border: 1px solid var(--border-medium);
                  background: var(--bg-card); color: var(--fg-body);
                  cursor: pointer; font-size: 13px; font-weight: 600;
                  display: flex; align-items: center; gap: 6px;
                  box-shadow: var(--shadow-card); z-index: 100;
                  transition: transform 0.15s, background 0.2s; white-space: nowrap; }}
  .refresh-btn:hover {{ transform: scale(1.05); background: var(--bg-card-hover); }}
  .refresh-btn.spinning .refresh-icon {{ display: inline-block; animation: spin 0.8s linear infinite; }}
  @keyframes spin {{ from {{ transform: rotate(0deg); }} to {{ transform: rotate(360deg); }} }}

  /* ---------- Refresh button (floating, left of theme toggle) ---------- */
  .refresh-btn {{ position: fixed; top: 20px; right: 66px;
                  height: 38px; padding: 0 14px;
                  border-radius: 19px; border: 1px solid var(--border-medium);
                  background: var(--bg-card); color: var(--fg-body);
                  cursor: pointer; font-size: 13px; font-weight: 600;
                  display: flex; align-items: center; gap: 6px;
                  box-shadow: var(--shadow-card); z-index: 100;
                  transition: transform 0.15s, background 0.2s; white-space: nowrap; }}
  .refresh-btn:hover {{ transform: scale(1.05); background: var(--bg-card-hover); }}
  .refresh-btn.spinning .refresh-icon {{ display: inline-block; animation: spin 0.8s linear infinite; }}
  @keyframes spin {{ from {{ transform: rotate(0deg); }} to {{ transform: rotate(360deg); }} }}"""

REPLACE_REFRESH_BTN_CSS = """\
  /* ---------- Cloud refresh button ---------- */
  .refresh-btn {{
    position: fixed; top: 20px; right: 66px;
    height: 38px; padding: 0 14px;
    border-radius: 19px; border: 1px solid var(--border-medium);
    background: var(--bg-card); color: var(--fg-body);
    cursor: pointer; font-size: 13px; font-weight: 600;
    display: flex; align-items: center; gap: 6px;
    box-shadow: var(--shadow-card); z-index: 100;
    transition: transform 0.15s, background 0.2s; white-space: nowrap;
  }}
  .refresh-btn:hover   {{ transform: scale(1.05); background: var(--bg-card-hover); }}
  .refresh-btn.running {{ background: #2c3e50; color: #fff; cursor: wait; }}
  .refresh-btn.success {{ background: #27ae60; color: #fff; }}
  .refresh-btn.error   {{ background: #c0392b; color: #fff; }}
  @keyframes spin {{ from {{ transform: rotate(0deg); }} to {{ transform: rotate(360deg); }} }}
  .spin {{ display: inline-block; animation: spin 0.9s linear infinite; }}

  /* ---------- Refresh progress panel ---------- */
  #refresh-panel {{
    position: fixed; bottom: 24px; right: 24px;
    width: 340px;
    background: #1a2028; color: #e8eaed;
    border: 1px solid #2d3540; border-radius: 10px;
    font-size: 12px; font-family: "SF Mono", Consolas, monospace;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
    z-index: 9999; display: none; overflow: hidden;
  }}
  #refresh-panel.open {{ display: block; }}
  #refresh-panel-header {{
    background: #232a35; padding: 8px 12px;
    font-size: 11px; font-weight: 600; color: #8b95a3;
    text-transform: uppercase; letter-spacing: 0.5px;
    display: flex; justify-content: space-between; align-items: center;
  }}
  #refresh-panel-close {{
    cursor: pointer; background: none; border: none;
    color: #8b95a3; font-size: 14px; padding: 0 4px;
  }}
  #refresh-panel-close:hover {{ color: #e8eaed; }}
  #refresh-panel-body {{ padding: 10px 12px; }}
  #refresh-panel-body p {{ margin: 2px 0; padding: 1px 0; color: #cbd5e0; }}
  #refresh-panel-body p.phase {{ color: #fbbf24; font-weight: 700; }}
  #refresh-panel-body p.done  {{ color: #4ade80; }}
  #refresh-panel-body p.error {{ color: #f87171; }}
  #refresh-progress-wrap {{
    padding: 6px 12px 10px;
    background: #232a35; border-top: 1px solid #2d3540;
  }}
  #refresh-progress-bar {{
    width: 100%; height: 4px; background: #2d3540;
    border-radius: 2px; overflow: hidden; margin-bottom: 4px;
  }}
  #refresh-progress-fill {{
    height: 100%; background: #4a90e2; border-radius: 2px;
    width: 0%; transition: width 0.8s ease;
  }}
  #refresh-progress-text {{
    font-size: 11px; color: #8b95a3; text-align: right;
  }}"""


# 2. Replace the top refresh button HTML
FIND_REFRESH_BTN_HTML = """\
<button class="refresh-btn" id="refreshBtn" title="Reload report (re-run the script first to refresh data)" onclick="refreshReport()">
  <span class="refresh-icon">🔄</span> Refresh
</button>"""

REPLACE_REFRESH_BTN_HTML = """\
<button class="refresh-btn" id="refreshBtn" onclick="triggerRefresh()"
        title="Trigger a fresh data pull via GitHub Actions (~10 min)">
  🔄 Refresh
</button>
<div id="refresh-panel">
  <div id="refresh-panel-header">
    <span>📡 Refreshing report…</span>
    <button id="refresh-panel-close" onclick="document.getElementById('refresh-panel').classList.remove('open')">✕</button>
  </div>
  <div id="refresh-panel-body"></div>
  <div id="refresh-progress-wrap">
    <div id="refresh-progress-bar"><div id="refresh-progress-fill"></div></div>
    <div id="refresh-progress-text">Starting…</div>
  </div>
</div>"""


# 3. Replace the section refresh buttons — they now call triggerRefresh() too
FIND_SECTION_BTN = """\
    "<button class='section-refresh-btn' data-section='compounders' "\n"""

REPLACE_SECTION_BTN = """\
    "<button class='section-refresh-btn' data-section='compounders' "\n"""
# (section buttons are handled differently — we patch the JS instead)


# 4. Replace the old JS (refreshReport + section refresh IIFE) with new cloud JS
FIND_JS = """\
/* ---------- Refresh button ---------- */
function refreshReport() {{
  var btn = document.getElementById('refreshBtn');
  if (btn) btn.classList.add('spinning');
  // Brief visual feedback, then reload the page (picks up the latest saved report.html)
  setTimeout(function() {{ location.reload(); }}, 300);
}}"""

REPLACE_JS = """\
/* ---------- Cloud refresh — triggers GitHub Actions run ---------- */
// GitHub repo and token are injected at report-generation time (see below).
// GITHUB_REPO format: "username/repo-name"
// GITHUB_TOKEN needs workflow scope (read-only would 403 on dispatch).
var _GH_REPO  = document.getElementById('gh-meta') ?
                document.getElementById('gh-meta').dataset.repo : '';
var _GH_TOKEN = document.getElementById('gh-meta') ?
                document.getElementById('gh-meta').dataset.token : '';
var _GH_BRANCH = document.getElementById('gh-meta') ?
                 document.getElementById('gh-meta').dataset.branch : 'main';

var _pollTimer = null;
var _runId = null;
var _startTime = null;

function _log(msg, cls) {{
  var body = document.getElementById('refresh-panel-body');
  if (!body) return;
  var p = document.createElement('p');
  p.textContent = msg;
  if (cls) p.className = cls;
  body.appendChild(p);
  body.scrollTop = body.scrollHeight;
}}

function _setProgress(pct, label) {{
  var fill = document.getElementById('refresh-progress-fill');
  var text = document.getElementById('refresh-progress-text');
  if (fill) fill.style.width = pct + '%';
  if (text) text.textContent = label;
}}

function _setBtn(state) {{
  var btn = document.getElementById('refreshBtn');
  if (!btn) return;
  btn.classList.remove('running','success','error');
  if (state === 'running') {{
    btn.classList.add('running');
    btn.innerHTML = '<span class="spin">⏳</span> Running…';
    btn.disabled = true;
  }} else if (state === 'success') {{
    btn.classList.add('success');
    btn.innerHTML = '✓ Done — reloading…';
    btn.disabled = true;
  }} else if (state === 'error') {{
    btn.classList.add('error');
    btn.innerHTML = '✗ Error — click to retry';
    btn.disabled = false;
  }} else {{
    btn.innerHTML = '🔄 Refresh';
    btn.disabled = false;
  }}
}}

function triggerRefresh() {{
  if (!_GH_REPO || !_GH_TOKEN) {{
    alert('GitHub repo/token not configured.\\n\\nAdd GITHUB_REPO and GITHUB_TOKEN as GitHub Secrets, then regenerate the report.');
    return;
  }}
  document.getElementById('refresh-panel').classList.add('open');
  document.getElementById('refresh-panel-body').innerHTML = '';
  _setBtn('running');
  _setProgress(5, 'Triggering workflow…');
  _startTime = Date.now();
  _runId = null;

  _log('▶ Triggering GitHub Actions workflow…', 'phase');

  fetch('https://api.github.com/repos/' + _GH_REPO + '/actions/workflows/portfolio.yml/dispatches', {{
    method: 'POST',
    headers: {{
      'Authorization': 'token ' + _GH_TOKEN,
      'Accept': 'application/vnd.github+json',
      'Content-Type': 'application/json',
    }},
    body: JSON.stringify({{ ref: _GH_BRANCH }}),
  }})
  .then(function(r) {{
    if (r.status === 204) {{
      _log('✓ Workflow triggered successfully', 'done');
      _log('⏳ Waiting for run to start…');
      _setProgress(10, 'Workflow queued…');
      setTimeout(_findRun, 5000);
    }} else {{
      return r.text().then(function(t) {{
        throw new Error('GitHub API ' + r.status + ': ' + t);
      }});
    }}
  }})
  .catch(function(e) {{
    _log('✗ ' + e.message, 'error');
    _setProgress(0, 'Failed');
    _setBtn('error');
  }});
}}

function _findRun() {{
  fetch('https://api.github.com/repos/' + _GH_REPO + '/actions/runs?per_page=5&event=workflow_dispatch', {{
    headers: {{
      'Authorization': 'token ' + _GH_TOKEN,
      'Accept': 'application/vnd.github+json',
    }},
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(data) {{
    var runs = data.workflow_runs || [];
    var recent = runs.filter(function(r) {{
      return r.status !== 'completed' ||
        (Date.now() - new Date(r.created_at).getTime()) < 120000;
    }});
    if (recent.length > 0) {{
      _runId = recent[0].id;
      _log('▶ Run #' + _runId + ' started — polling status…', 'phase');
      _setProgress(20, 'Run started…');
      _pollTimer = setInterval(_pollRun, 10000);
    }} else {{
      // Not found yet, keep waiting
      setTimeout(_findRun, 5000);
    }}
  }})
  .catch(function(e) {{
    _log('✗ Could not find run: ' + e.message, 'error');
    setTimeout(_findRun, 10000);
  }});
}}

var _STEPS = [
  [0,  20, 'Queued'],
  [20, 35, 'Logging into Robinhood…'],
  [35, 55, 'Fetching positions…'],
  [55, 70, 'Analyzing compounders…'],
  [70, 80, 'Analyzing watchlists…'],
  [80, 88, 'Tax analysis…'],
  [88, 95, 'Generating report…'],
  [95, 99, 'Deploying to GitHub Pages…'],
];
var _stepIdx = 0;

function _pollRun() {{
  fetch('https://api.github.com/repos/' + _GH_REPO + '/actions/runs/' + _runId, {{
    headers: {{
      'Authorization': 'token ' + _GH_TOKEN,
      'Accept': 'application/vnd.github+json',
    }},
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(run) {{
    var status     = run.status;
    var conclusion = run.conclusion;
    var elapsed    = Math.round((Date.now() - _startTime) / 1000);
    var mins       = Math.floor(elapsed / 60);
    var secs       = elapsed % 60;
    var elapsedStr = mins > 0 ? mins + 'm ' + secs + 's' : secs + 's';

    // Advance progress step based on elapsed time
    var step = _STEPS[Math.min(_stepIdx, _STEPS.length - 1)];
    var progress = step[0] + Math.min(
      (elapsed / 600) * (step[1] - step[0]),
      step[1] - step[0]
    );
    if (elapsed > 60  && _stepIdx < 2) {{ _stepIdx = 2; }}
    if (elapsed > 90  && _stepIdx < 3) {{ _stepIdx = 3; }}
    if (elapsed > 150 && _stepIdx < 4) {{ _stepIdx = 4; }}
    if (elapsed > 240 && _stepIdx < 5) {{ _stepIdx = 5; }}
    if (elapsed > 360 && _stepIdx < 6) {{ _stepIdx = 6; }}
    if (elapsed > 480 && _stepIdx < 7) {{ _stepIdx = 7; }}

    _setProgress(Math.min(progress, 98), _STEPS[_stepIdx][2] + ' · ' + elapsedStr);

    if (status === 'completed') {{
      clearInterval(_pollTimer);
      if (conclusion === 'success') {{
        _setProgress(100, 'Complete!');
        _log('✓ Report updated successfully!', 'done');
        _log('↻ Reloading in 3 seconds…', 'done');
        _setBtn('success');
        setTimeout(function() {{ location.reload(); }}, 3000);
      }} else {{
        _setProgress(100, 'Failed');
        _log('✗ Workflow failed: ' + conclusion, 'error');
        _log('  Check: https://github.com/' + _GH_REPO + '/actions', 'error');
        _setBtn('error');
      }}
    }}
  }})
  .catch(function(e) {{
    _log('Poll error: ' + e.message, 'error');
  }});
}}

// Section refresh buttons now trigger a full Actions run instead of local API
document.querySelectorAll('.section-refresh-btn').forEach(function(btn) {{
  btn.addEventListener('click', function() {{
    triggerRefresh();
  }});
}});"""


# 5. Find the closing of the filter IIFE and inject the gh-meta tag + new JS
FIND_CLOSE = """\
  searchInput.addEventListener('input', applyFilters);
  applyFilters();
  // Expose so section-refresh can re-run filters after swapping table HTML
  window._applyFilters = applyFilters;
})();"""

REPLACE_CLOSE = """\
  searchInput.addEventListener('input', applyFilters);
  applyFilters();
  window._applyFilters = applyFilters;
})();"""


# 6. Find the now statement in the HTML and inject the meta tag after it
FIND_META_INJECT = """\
<h1>{report_title}</h1>
<div class="sub">Live data as of {now}{' · Finnhub enabled' if FINNHUB_API_KEY else ' · yfinance only'}</div>"""

REPLACE_META_INJECT = """\
<h1>{report_title}</h1>
<div class="sub">Live data as of {now}{' · Finnhub enabled' if FINNHUB_API_KEY else ' · yfinance only'}</div>
<div id="gh-meta" style="display:none"
     data-repo="{{GH_REPO}}"
     data-token="{{GH_TOKEN}}"
     data-branch="{{GH_BRANCH}}"></div>"""


# 7. Inject GH_REPO/TOKEN/BRANCH into generate_html_report() from env
FIND_HTML_START = """\
    html = f\"\"\"<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">"""

REPLACE_HTML_START = """\
    import os as _os
    _GH_REPO   = _os.environ.get("GITHUB_REPO",   "")
    _GH_TOKEN  = _os.environ.get("GITHUB_TOKEN",  "")
    _GH_BRANCH = _os.environ.get("GITHUB_BRANCH", "main")

    html = f\"\"\"<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">"""


# 8. Replace {GH_REPO} placeholders with actual f-string variables
FIND_META_TAG = """\
<div id="gh-meta" style="display:none"
     data-repo="{{GH_REPO}}"
     data-token="{{GH_TOKEN}}"
     data-branch="{{GH_BRANCH}}"></div>"""

REPLACE_META_TAG = """\
<div id="gh-meta" style="display:none"
     data-repo="{_GH_REPO}"
     data-token="{_GH_TOKEN}"
     data-branch="{_GH_BRANCH}"></div>"""


PATCHES = [
    ("CSS — refresh button",          FIND_REFRESH_BTN_CSS,  REPLACE_REFRESH_BTN_CSS),
    ("HTML — refresh button element", FIND_REFRESH_BTN_HTML, REPLACE_REFRESH_BTN_HTML),
    ("JS — replace refresh logic",    FIND_JS,               REPLACE_JS),
    ("JS — filter IIFE close",        FIND_CLOSE,            REPLACE_CLOSE),
    ("HTML — gh-meta placeholder",    FIND_META_INJECT,      REPLACE_META_INJECT),
    ("Python — read GH env vars",     FIND_HTML_START,       REPLACE_HTML_START),
    ("HTML — gh-meta f-string",       FIND_META_TAG,         REPLACE_META_TAG),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="analyze_portfolio.py")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.file)
    if not src.exists():
        print(f"ERROR: {src} not found.", file=sys.stderr)
        sys.exit(1)

    result = src.read_text(encoding="utf-8")
    print(f"Patching {src}…\n")

    errors = []
    for label, find, replace in PATCHES:
        if find in result:
            result = result.replace(find, replace, 1)
            print(f"  ✓ {label}")
        elif replace in result:
            print(f"  — {label} (already applied)")
        else:
            print(f"  ✗ {label} — NOT FOUND")
            errors.append(label)

    if errors:
        print(f"\n⚠  {len(errors)} patch(es) not applied — file may differ from expected version.")
        print("   File was NOT modified.")
        sys.exit(1)

    if args.dry_run:
        print("\n--dry-run: no files written.")
        return

    bak = src.with_suffix(".py.bak4")
    shutil.copy2(src, bak)
    print(f"\nBackup → {bak}")
    src.write_text(result, encoding="utf-8")
    print(f"✓ {src} patched.\n")

    print("""Next steps:
─────────────────────────────────────────────────────
1. Add two GitHub Secrets to your repo:
   repo → Settings → Secrets → Actions → New secret

   GITHUB_REPO   = yourusername/portfolio-analyzer
   GITHUB_TOKEN  = your personal access token
                   (needs 'repo' + 'workflow' scopes)

   Optional:
   GITHUB_BRANCH = main   (default, skip if using main)

2. Push the updated analyze_portfolio.py to GitHub:
   git add analyze_portfolio.py
   git commit -m "Add cloud refresh buttons"
   git push

3. GitHub Actions will regenerate the report automatically.
   Once deployed, every 🔄 button on the page will trigger
   a fresh GitHub Actions run and auto-reload when done.
─────────────────────────────────────────────────────
""")


if __name__ == "__main__":
    main()
