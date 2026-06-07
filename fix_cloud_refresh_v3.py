"""
fix_cloud_refresh_v3.py
-----------------------
Simple, direct approach — finds the exact closing of the HTML report
in analyze_portfolio.py and appends the cloud refresh JS cleanly.

Usage:
    python fix_cloud_refresh_v3.py
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
    print(f"Patching {src}…\n")

    # ── Step 1: Remove old section refresh IIFE if present ───────────────────
    import re
    old_iife = re.compile(
        r'\n/\* ---------- Section refresh ---------- \*/\n'
        r'\(function\(\) \{.*?\}\)\(\);\n',
        re.DOTALL
    )
    content, n = old_iife.subn('', content)
    if n:
        print(f"  ✓ Removed old section refresh IIFE")

    # ── Step 2: Remove any existing cloud refresh JS if present ──────────────
    old_cloud = re.compile(
        r'\n/\* ---------- Cloud refresh.*?\}\)\(\);\n',
        re.DOTALL
    )
    content, n = old_cloud.subn('', content)
    if n:
        print(f"  ✓ Removed old cloud refresh JS")

    # ── Step 3: Find the end of the HTML string in generate_html_report ──────
    # The report HTML ends with:
    #   </script>\n</body></html>\n"""
    # followed by either `    return html` or `\n    return html`
    # We search for this specific pattern in the Python source.

    target = '</script>\\n</body></html>\\n"""\n    return html'
    target_alt = "</script>\\n</body></html>\\n\"\"\"\n    return html"

    # Find the actual string in the file
    # In the Python source it looks like: ...\\n</body></html>\\n"""\n    return html
    # But we need to find the LAST occurrence (the generate_html_report one)

    # Search for the pattern directly
    search_str = '\\n</body></html>\\n"""\n    return html'
    idx = content.rfind(search_str)

    if idx == -1:
        # Try without the leading \\n
        search_str = '</body></html>\\n"""\n    return html'
        idx = content.rfind(search_str)

    if idx == -1:
        # Try the actual newline version (not escaped)
        search_str = '\n</body></html>\n"""\n    return html'
        idx = content.rfind(search_str)

    if idx == -1:
        print("  ✗ Could not find end of HTML report string")
        print()
        print("  Showing last 200 chars before 'return html':")
        ret_idx = content.rfind('    return html')
        if ret_idx != -1:
            print(repr(content[ret_idx-200:ret_idx+20]))
        sys.exit(1)

    print(f"  ✓ Found insertion point (char {idx})")

    # Insert the new JS before the closing </script> tag
    # The marker is: ...existing_js...</script>\n</body></html>\n"""...
    # We want:      ...existing_js...\nNEW_JS\n</script>\n</body></html>\n"""...

    # Find the </script> right before our search_str
    script_close = '</script>'
    script_idx = content.rfind(script_close, 0, idx)

    if script_idx == -1:
        print("  ✗ Could not find </script> before end of HTML")
        sys.exit(1)

    print(f"  ✓ Found </script> at char {script_idx}")

    # Insert the cloud refresh JS right before </script>
    new_content = (
        content[:script_idx]
        + "\n" + CLOUD_REFRESH_JS + "\n"
        + content[script_idx:]
    )

    # ── Step 4: Verify triggerRefresh is now in the file ─────────────────────
    if 'triggerRefresh' not in new_content:
        print("  ✗ triggerRefresh not found after insertion — something went wrong")
        sys.exit(1)
    print("  ✓ triggerRefresh inserted successfully")

    # ── Step 5: Write the file ────────────────────────────────────────────────
    bak = src.with_suffix(".py.bak5")
    shutil.copy2(src, bak)
    print(f"  Backup → {bak}")

    src.write_text(new_content, encoding="utf-8")
    print(f"  ✓ {src} patched successfully\n")

    print("""Next steps:
─────────────────────────────────────────────────────
1. Confirm secrets in GitHub repo → Settings → Secrets:
     GH_REPO   = yourusername/portfolio-analyzer
     GH_TOKEN  = your personal access token

2. Confirm portfolio.yml env block has:
     GH_REPO:  ${{ secrets.GH_REPO }}
     GH_TOKEN: ${{ secrets.GH_TOKEN }}

3. Push and trigger:
     git add analyze_portfolio.py
     git commit -m "Fix cloud refresh JS"
     git push

4. GitHub → Actions → Portfolio Analyzer → Run workflow
   Wait ~10 min, then test the 🔄 button.
─────────────────────────────────────────────────────
""")


CLOUD_REFRESH_JS = """\
/* ---------- Cloud refresh — triggers GitHub Actions run ---------- */
(function() {
  var meta      = document.getElementById('gh-meta');
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
      btn.innerHTML = '<span class="spin">&#9203;</span> Running&hellip;';
      btn.disabled = true;
    } else if (state === 'success') {
      btn.classList.add('success');
      btn.innerHTML = '&#10003; Done &mdash; reloading&hellip;';
      btn.disabled = true;
    } else if (state === 'error') {
      btn.classList.add('error');
      btn.innerHTML = '&#10007; Error &mdash; click to retry';
      btn.disabled = false;
    } else {
      btn.innerHTML = '&#128260; Refresh';
      btn.disabled = false;
    }
  }

  function openPanel() {
    var panel = document.getElementById('refresh-panel');
    var body  = document.getElementById('refresh-panel-body');
    if (panel) panel.classList.add('open');
    if (body)  body.innerHTML = '';
    stepIdx   = 0;
    startTime = Date.now();
    runId     = null;
  }

  function findRun() {
    fetch('https://api.github.com/repos/' + GH_REPO +
          '/actions/runs?per_page=5&event=workflow_dispatch', {
      headers: {
        'Authorization': 'token ' + GH_TOKEN,
        'Accept': 'application/vnd.github+json'
      }
    })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var runs = (data.workflow_runs || []).filter(function(r) {
        return r.status !== 'completed' ||
          (Date.now() - new Date(r.created_at).getTime()) < 120000;
      });
      if (runs.length > 0) {
        runId = runs[0].id;
        log('Run #' + runId + ' started — polling status…', 'phase');
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
        'Accept': 'application/vnd.github+json'
      }
    })
    .then(function(r) { return r.json(); })
    .then(function(run) {
      var elapsed = Math.round((Date.now() - startTime) / 1000);
      var mins    = Math.floor(elapsed / 60);
      var secs    = elapsed % 60;
      var timeStr = (mins > 0 ? mins + 'm ' : '') + secs + 's';

      if (elapsed > 30  && stepIdx < 1) stepIdx = 1;
      if (elapsed > 60  && stepIdx < 2) stepIdx = 2;
      if (elapsed > 180 && stepIdx < 3) stepIdx = 3;
      if (elapsed > 300 && stepIdx < 4) stepIdx = 4;
      if (elapsed > 420 && stepIdx < 5) stepIdx = 5;
      if (elapsed > 540 && stepIdx < 6) stepIdx = 6;

      var step     = STEPS[Math.min(stepIdx, STEPS.length - 1)];
      var progress = step[0] + Math.min(
        (elapsed / 600) * (step[1] - step[0]), step[1] - step[0]
      );
      setProgress(progress, step[2] + ' · ' + timeStr);

      if (run.status === 'completed') {
        clearInterval(pollTimer);
        if (run.conclusion === 'success') {
          setProgress(100, 'Complete!');
          log('Report updated successfully!', 'done');
          log('Reloading in 3 seconds…', 'done');
          setBtn('success');
          setTimeout(function() { location.reload(); }, 3000);
        } else {
          setProgress(0, 'Failed');
          log('Workflow failed: ' + run.conclusion, 'error');
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
        'GitHub credentials missing.\\n\\n' +
        'Add these secrets in your GitHub repo:\\n' +
        '  GH_REPO  = yourusername/portfolio-analyzer\\n' +
        '  GH_TOKEN = your personal access token\\n\\n' +
        'Then re-run the workflow to regenerate the report.'
      );
      return;
    }
    openPanel();
    setBtn('running');
    setProgress(5, 'Triggering workflow…');
    log('Triggering GitHub Actions workflow…', 'phase');

    fetch('https://api.github.com/repos/' + GH_REPO +
          '/actions/workflows/portfolio.yml/dispatches', {
      method: 'POST',
      headers: {
        'Authorization': 'token ' + GH_TOKEN,
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ ref: GH_BRANCH })
    })
    .then(function(r) {
      if (r.status === 204) {
        log('Workflow triggered successfully!', 'done');
        setProgress(10, 'Workflow queued…');
        setTimeout(findRun, 6000);
      } else {
        return r.text().then(function(t) {
          throw new Error('GitHub API ' + r.status + ': ' + t);
        });
      }
    })
    .catch(function(e) {
      log('Error: ' + e.message, 'error');
      setProgress(0, 'Failed');
      setBtn('error');
    });
  };

  // Wire all refresh buttons
  document.querySelectorAll('.section-refresh-btn, #refreshBtn').forEach(function(btn) {
    btn.removeAttribute('onclick');
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      window.triggerRefresh();
    });
  });

  // Wire panel close button
  var closeBtn = document.getElementById('refresh-panel-close');
  if (closeBtn) {
    closeBtn.addEventListener('click', function() {
      var panel = document.getElementById('refresh-panel');
      if (panel) panel.classList.remove('open');
    });
  }

})();"""


if __name__ == "__main__":
    main()
