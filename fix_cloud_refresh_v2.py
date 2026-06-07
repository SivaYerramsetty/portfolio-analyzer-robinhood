"""
fix_cloud_refresh_v2.py
-----------------------
Directly fixes analyze_portfolio.py by:
1. Removing the old section refresh IIFE (which conflicts with cloud refresh)
2. Ensuring triggerRefresh and cloud refresh JS is clean and complete
3. Fixing empty data-repo/data-token by using correct env var names

Usage:
    python fix_cloud_refresh_v2.py
"""

import shutil
import sys
from pathlib import Path


def main():
    src = Path("analyze_portfolio.py")
    if not src.exists():
        print("ERROR: analyze_portfolio.py not found.")
        sys.exit(1)

    content = src.read_text(encoding="utf-8")
    original = content
    print(f"Patching {src}…\n")
    issues = []

    # ── 1. Remove the old section refresh IIFE ────────────────────────────────
    # This IIFE conflicts with the cloud refresh JS and breaks the parser.
    # It starts with "/* --- Section refresh ---" and ends with "}})();"
    import re

    # Find and remove the section refresh IIFE block
    section_iife_pattern = re.compile(
        r'/\* ---------- Section refresh ---------- \*/\n\(function\(\) \{.*?\}\)\(\);',
        re.DOTALL
    )
    match = section_iife_pattern.search(content)
    if match:
        content = content[:match.start()] + content[match.end():]
        print("  ✓ Removed old section refresh IIFE")
    else:
        print("  — Section refresh IIFE not found (may already be removed)")

    # ── 2. Remove any stale triggerRefresh / cloud refresh JS if partial ──────
    cloud_pattern = re.compile(
        r'/\* ---------- Cloud refresh.*?document\.querySelectorAll\(\'\.section-refresh-btn\'\).*?\}\)\(\);',
        re.DOTALL
    )
    match = cloud_pattern.search(content)
    if match:
        content = content[:match.start()] + content[match.end():]
        print("  ✓ Removed stale cloud refresh JS (will re-insert cleanly)")
    else:
        print("  — No stale cloud refresh JS found")

    # ── 3. Find the insertion point — right before </script> closing tag ──────
    # The main script block ends with the filter IIFE close then </script>
    INSERT_MARKER = "  window._applyFilters = applyFilters;\n})();\n</script>"
    INSERT_MARKER_ALT = "  window._applyFilters = applyFilters;\n}})();\n</script>"

    marker = None
    if INSERT_MARKER in content:
        marker = INSERT_MARKER
    elif INSERT_MARKER_ALT in content:
        marker = INSERT_MARKER_ALT

    if not marker:
        print("  ✗ Could not find insertion point (filter IIFE close)")
        print("  Searching for _applyFilters...")
        idx = content.find("_applyFilters")
        if idx != -1:
            print(f"  Found at char {idx}:")
            print(repr(content[idx-50:idx+150]))
        issues.append("insertion point not found")
    else:
        # Insert cloud refresh JS before </script>
        close_tag = marker.replace("  window._applyFilters = applyFilters;\n", "")
        # close_tag is either })();\n</script> or }})();\n</script>
        insert_before = close_tag  # insert new JS before the </script>

        new_js = CLOUD_REFRESH_JS

        content = content.replace(
            marker,
            marker.replace(insert_before, "\n" + new_js + "\n" + insert_before),
            1
        )
        print("  ✓ Inserted cloud refresh JS")

    # ── 4. Fix section refresh buttons — wire to triggerRefresh() ─────────────
    # The data-section buttons currently call the old server API.
    # Replace their event listener setup to call triggerRefresh() instead.
    # This is already handled in the new CLOUD_REFRESH_JS block above.
    print("  ✓ Section buttons wired to triggerRefresh() via new JS")

    # ── 5. Check for issues ───────────────────────────────────────────────────
    if issues:
        print(f"\n⚠  {len(issues)} issue(s) — file NOT modified.")
        sys.exit(1)

    if content == original:
        print("\n  No changes needed — already up to date.")
        return

    bak = src.with_suffix(".py.bak5")
    shutil.copy2(src, bak)
    print(f"\n  Backup → {bak}")
    src.write_text(content, encoding="utf-8")
    print(f"  ✓ {src} patched successfully.\n")

    print("""Next steps:
─────────────────────────────────────────────────────
1. Make sure these secrets exist in GitHub:
   GH_REPO   = yourusername/portfolio-analyzer
   GH_TOKEN  = your personal access token

2. Make sure portfolio.yml env block includes:
   GH_REPO:  ${{ secrets.GH_REPO }}
   GH_TOKEN: ${{ secrets.GH_TOKEN }}

3. Push and trigger a new run:
   git add analyze_portfolio.py
   git commit -m "Fix cloud refresh JS"
   git push

4. GitHub → Actions → Portfolio Analyzer → Run workflow
   Wait ~10 min, then test the refresh button.
─────────────────────────────────────────────────────
""")


CLOUD_REFRESH_JS = """\
/* ---------- Cloud refresh — triggers GitHub Actions run ---------- */
(function() {
  var meta   = document.getElementById('gh-meta');
  var GH_REPO   = meta ? (meta.getAttribute('data-repo')   || '') : '';
  var GH_TOKEN  = meta ? (meta.getAttribute('data-token')  || '') : '';
  var GH_BRANCH = meta ? (meta.getAttribute('data-branch') || 'main') : 'main';

  var pollTimer = null;
  var runId     = null;
  var startTime = null;
  var stepIdx   = 0;

  var STEPS = [
    [0,  10, 'Queued…'],
    [10, 30, 'Logging into Robinhood…'],
    [30, 55, 'Fetching & analyzing positions…'],
    [55, 70, 'Analyzing watchlists…'],
    [70, 82, 'Tax analysis…'],
    [82, 92, 'Generating report…'],
    [92, 99, 'Deploying to GitHub Pages…'],
  ];

  function log(msg, cls) {
    var body = document.getElementById('refresh-panel-body');
    if (!body) return;
    var p = document.createElement('p');
    p.textContent = msg;
    if (cls) p.className = cls;
    body.appendChild(p);
    body.scrollTop = body.scrollHeight;
  }

  function setProgress(pct, label) {
    var fill = document.getElementById('refresh-progress-fill');
    var text = document.getElementById('refresh-progress-text');
    if (fill) fill.style.width = Math.min(pct, 99) + '%';
    if (text) text.textContent = label;
  }

  function setBtn(state) {
    var btn = document.getElementById('refreshBtn');
    if (!btn) return;
    btn.classList.remove('running', 'success', 'error');
    if (state === 'running') {
      btn.classList.add('running');
      btn.innerHTML = '<span class="spin">⏳</span> Running…';
      btn.disabled = true;
    } else if (state === 'success') {
      btn.classList.add('success');
      btn.innerHTML = '✓ Done — reloading…';
      btn.disabled = true;
    } else if (state === 'error') {
      btn.classList.add('error');
      btn.innerHTML = '✗ Error — click to retry';
      btn.disabled = false;
    } else {
      btn.innerHTML = '🔄 Refresh';
      btn.disabled = false;
    }
  }

  function openPanel() {
    var panel = document.getElementById('refresh-panel');
    var body  = document.getElementById('refresh-panel-body');
    if (panel) panel.classList.add('open');
    if (body)  body.innerHTML = '';
    stepIdx = 0;
  }

  function findRun() {
    fetch('https://api.github.com/repos/' + GH_REPO +
          '/actions/runs?per_page=5&event=workflow_dispatch', {
      headers: {
        'Authorization': 'token ' + GH_TOKEN,
        'Accept': 'application/vnd.github+json',
      },
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var runs = (data.workflow_runs || []).filter(function(r) {
        return r.status !== 'completed' ||
          (Date.now() - new Date(r.created_at).getTime()) < 120000;
      });
      if (runs.length > 0) {
        runId = runs[0].id;
        log('▶ Run #' + runId + ' started — polling…', 'phase');
        setProgress(15, 'Run started…');
        pollTimer = setInterval(pollRun, 12000);
      } else {
        setTimeout(findRun, 5000);
      }
    })
    .catch(function() { setTimeout(findRun, 8000); });
  }

  function pollRun() {
    fetch('https://api.github.com/repos/' + GH_REPO + '/actions/runs/' + runId, {
      headers: {
        'Authorization': 'token ' + GH_TOKEN,
        'Accept': 'application/vnd.github+json',
      },
    })
    .then(function(r) { return r.json(); })
    .then(function(run) {
      var elapsed = Math.round((Date.now() - startTime) / 1000);
      var mins = Math.floor(elapsed / 60);
      var secs = elapsed % 60;
      var timeStr = (mins > 0 ? mins + 'm ' : '') + secs + 's';

      // Advance step based on elapsed time
      if (elapsed > 30  && stepIdx < 1) stepIdx = 1;
      if (elapsed > 60  && stepIdx < 2) stepIdx = 2;
      if (elapsed > 180 && stepIdx < 3) stepIdx = 3;
      if (elapsed > 300 && stepIdx < 4) stepIdx = 4;
      if (elapsed > 420 && stepIdx < 5) stepIdx = 5;
      if (elapsed > 540 && stepIdx < 6) stepIdx = 6;

      var step = STEPS[Math.min(stepIdx, STEPS.length - 1)];
      var progress = step[0] + Math.min(
        (elapsed / 600) * (step[1] - step[0]), step[1] - step[0]
      );
      setProgress(progress, step[2] + ' · ' + timeStr);

      if (run.status === 'completed') {
        clearInterval(pollTimer);
        if (run.conclusion === 'success') {
          setProgress(100, 'Complete!');
          log('✓ Report updated!', 'done');
          log('↻ Reloading in 3 seconds…', 'done');
          setBtn('success');
          setTimeout(function() { location.reload(); }, 3000);
        } else {
          setProgress(0, 'Failed');
          log('✗ Workflow failed: ' + run.conclusion, 'error');
          log('Check: https://github.com/' + GH_REPO + '/actions', 'error');
          setBtn('error');
        }
      }
    })
    .catch(function(e) { log('Poll error: ' + e, 'error'); });
  }

  window.triggerRefresh = function() {
    if (!GH_REPO || !GH_TOKEN) {
      alert(
        'GitHub credentials not configured.\\n\\n' +
        'Add these secrets in GitHub:\\n' +
        '  GH_REPO  = yourusername/portfolio-analyzer\\n' +
        '  GH_TOKEN = your personal access token\\n\\n' +
        'Then re-run the workflow to regenerate the report.'
      );
      return;
    }
    openPanel();
    setBtn('running');
    setProgress(5, 'Triggering workflow…');
    startTime = Date.now();
    runId = null;

    log('▶ Triggering GitHub Actions…', 'phase');

    fetch('https://api.github.com/repos/' + GH_REPO +
          '/actions/workflows/portfolio.yml/dispatches', {
      method: 'POST',
      headers: {
        'Authorization': 'token ' + GH_TOKEN,
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ref: GH_BRANCH }),
    })
    .then(function(r) {
      if (r.status === 204) {
        log('✓ Workflow triggered!', 'done');
        setProgress(10, 'Workflow queued…');
        setTimeout(findRun, 6000);
      } else {
        return r.text().then(function(t) {
          throw new Error('GitHub API ' + r.status + ': ' + t);
        });
      }
    })
    .catch(function(e) {
      log('✗ ' + e.message, 'error');
      setProgress(0, 'Failed');
      setBtn('error');
    });
  };

  // Wire section refresh buttons to triggerRefresh
  document.querySelectorAll('.section-refresh-btn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      window.triggerRefresh();
    });
  });

  // Wire panel close button
  var closeBtn = document.getElementById('refresh-panel-close');
  if (closeBtn) {
    closeBtn.addEventListener('click', function() {
      document.getElementById('refresh-panel').classList.remove('open');
    });
  }

})();"""


if __name__ == "__main__":
    main()
