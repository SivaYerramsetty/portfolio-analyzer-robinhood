#!/usr/bin/env python3
"""
fix_github_setup.py
-------------------
Fixes two issues:
  1. Makes the repo public (required for free GitHub Pages)
  2. Ensures the workflow file is correctly pushed
  3. Triggers the first action run
  4. Enables GitHub Pages

Usage:
    python fix_github_setup.py
"""

import os
import subprocess
import sys
from pathlib import Path

GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):   print(f"{GREEN}✓ {msg}{RESET}")
def warn(msg): print(f"{YELLOW}⚠  {msg}{RESET}")
def err(msg):  print(f"{RED}✗ {msg}{RESET}")
def info(msg): print(f"  {msg}")
def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    val = input(f"\n{BOLD}{prompt}{suffix}:{RESET} ").strip()
    return val or default

def run(cmd, cwd=None):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True)

def main():
    print(f"\n{BOLD}{CYAN}{'─'*54}")
    print("  GitHub Setup — Fix Issues")
    print(f"{'─'*54}{RESET}\n")

    try:
        import requests
        from github import Github, GithubException
    except ImportError:
        err("Missing packages.")
        print("Run:  pip install PyGithub PyNaCl requests")
        sys.exit(1)

    # ── Get token ─────────────────────────────────────────────────────────────
    token = ask("Paste your GitHub personal access token")
    if not token:
        err("Token required.")
        sys.exit(1)

    g = Github(token)
    try:
        user = g.get_user()
        ok(f"Authenticated as: {user.login}")
    except Exception as e:
        err(f"Invalid token: {e}")
        sys.exit(1)

    username = user.login
    repo_name = ask("Repository name", "portfolio-analyzer")

    # ── Get or verify repo ────────────────────────────────────────────────────
    try:
        repo = user.get_repo(repo_name)
        ok(f"Found repo: {repo.html_url}")
    except Exception:
        err(f"Repo '{repo_name}' not found under your account.")
        print("  Make sure you created it at github.com first.")
        sys.exit(1)

    # ── Fix 1: Make repo public ───────────────────────────────────────────────
    print(f"\n{BOLD}Fix 1 — Making repo public (required for free GitHub Pages){RESET}")
    if not repo.private:
        ok("Repo is already public")
    else:
        print("""
  GitHub Pages requires a public repo on the free plan.
  Your CODE will be visible but your SECRETS (Robinhood credentials)
  are stored encrypted and are never in the code — safe to make public.
""")
        confirm = ask("Make repo public? (yes/no)", "yes")
        if confirm.lower() in ("yes", "y"):
            repo.edit(private=False)
            ok("Repo is now public")
        else:
            warn("Skipped — Pages won't work until repo is public")

    # ── Fix 2: Ensure workflow file exists locally ────────────────────────────
    print(f"\n{BOLD}Fix 2 — Ensuring workflow file exists{RESET}")

    project_dir = Path(__file__).parent
    workflow_dir = project_dir / ".github" / "workflows"
    workflow_file = workflow_dir / "portfolio.yml"

    workflow_dir.mkdir(parents=True, exist_ok=True)

    if not workflow_file.exists():
        warn("portfolio.yml not found — creating it now")
        workflow_file.write_text(WORKFLOW_CONTENT)
        ok(f"Created {workflow_file}")
    else:
        ok(f"Workflow file exists: {workflow_file}")

    # ── Fix 3: Push everything to GitHub ─────────────────────────────────────
    print(f"\n{BOLD}Fix 3 — Pushing code to GitHub{RESET}")

    remote_url = f"https://{token}@github.com/{username}/{repo_name}.git"

    # Init git if needed
    if not (project_dir / ".git").exists():
        run("git init", cwd=project_dir)
        run(f"git remote add origin {remote_url}", cwd=project_dir)
        ok("Initialized git repo")
    else:
        run(f"git remote set-url origin {remote_url}", cwd=project_dir)
        ok("Updated git remote URL")

    # Set default branch name
    run("git config init.defaultBranch main", cwd=project_dir)

    # Stage and commit
    run("git add .github/ .gitignore requirements.txt", cwd=project_dir)

    # Also stage python files (but not .env or credentials)
    run("git add *.py", cwd=project_dir)

    result = run('git commit -m "Add GitHub Actions workflow and config" --allow-empty',
                 cwd=project_dir)
    if result.returncode == 0:
        ok("Committed changes")
    else:
        info(result.stderr.strip() or result.stdout.strip())

    # Push to main
    result = run("git push -u origin main 2>&1", cwd=project_dir)
    if result.returncode != 0:
        # Try master
        result = run("git push -u origin master 2>&1", cwd=project_dir)
        if result.returncode != 0:
            warn("Push failed — trying force push to main")
            run("git branch -M main", cwd=project_dir)
            result = run("git push -u origin main --force 2>&1", cwd=project_dir)

    if result.returncode == 0:
        ok("Code pushed to GitHub")
    else:
        err("Push failed:")
        info(result.stdout)
        info(result.stderr)

    # ── Fix 4: Enable GitHub Pages ────────────────────────────────────────────
    print(f"\n{BOLD}Fix 4 — Enabling GitHub Pages{RESET}")

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Enable Pages via API
    r = requests.post(
        f"https://api.github.com/repos/{username}/{repo_name}/pages",
        headers=headers,
        json={"build_type": "workflow"},
    )
    if r.status_code == 201:
        ok("GitHub Pages enabled")
    elif r.status_code == 409:
        ok("GitHub Pages already enabled")
    else:
        warn(f"Could not auto-enable Pages ({r.status_code}: {r.text})")
        warn("Do it manually: repo Settings → Pages → Source → GitHub Actions → Save")

    # ── Fix 5: Trigger first workflow run ─────────────────────────────────────
    print(f"\n{BOLD}Fix 5 — Triggering first workflow run{RESET}")

    import time
    time.sleep(3)  # Give GitHub a moment to register the pushed workflow

    for branch in ("main", "master"):
        r = requests.post(
            f"https://api.github.com/repos/{username}/{repo_name}/actions/workflows/portfolio.yml/dispatches",
            headers=headers,
            json={"ref": branch},
        )
        if r.status_code == 204:
            ok(f"Workflow triggered on branch '{branch}'")
            break
        elif r.status_code == 422:
            continue
    else:
        warn("Could not auto-trigger workflow")
        warn("Do it manually: GitHub → Actions tab → Portfolio Analyzer → Run workflow")

    # ── Done ──────────────────────────────────────────────────────────────────
    pages_url = f"https://{username}.github.io/{repo_name}"
    actions_url = f"https://github.com/{username}/{repo_name}/actions"

    print(f"""
{'='*54}
{BOLD}{GREEN}  ✓ All fixes applied!{RESET}
{'='*54}

  {BOLD}Watch the workflow run:{RESET}
    {actions_url}

  {BOLD}Your report URL (live in ~10 min):{RESET}
    {GREEN}{pages_url}{RESET}

  {BOLD}Trigger manual refresh anytime:{RESET}
    {actions_url}
    → Portfolio Analyzer → Run workflow

  {BOLD}Auto-refresh schedule:{RESET}
    Every weekday at 8:30 AM ET
{'='*54}
""")


# ── Workflow content ───────────────────────────────────────────────────────────
WORKFLOW_CONTENT = """\
name: Portfolio Analyzer

on:
  schedule:
    - cron: '30 13 * * 1-5'
  workflow_dispatch:

permissions:
  contents: read
  pages: write
  id-token: write

concurrency:
  group: pages
  cancel-in-progress: false

jobs:
  analyze:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install dependencies
        run: |
          pip install --upgrade pip
          pip install -r requirements.txt

      - name: Restore Robinhood session cache
        uses: actions/cache@v4
        with:
          path: ~/.tokens
          key: robinhood-session-${{ github.run_id }}
          restore-keys: robinhood-session-

      - name: Run portfolio analyzer
        env:
          RH_USERNAME:        ${{ secrets.RH_USERNAME }}
          RH_PASSWORD:        ${{ secrets.RH_PASSWORD }}
          FINNHUB_API_KEY:    ${{ secrets.FINNHUB_API_KEY }}
          TAX_FILING_STATUS:  ${{ secrets.TAX_FILING_STATUS }}
          TAX_TAXABLE_INCOME: ${{ secrets.TAX_TAXABLE_INCOME }}
          TAX_STATE_RATE:     ${{ secrets.TAX_STATE_RATE }}
          TAX_APPLY_NIIT:     ${{ secrets.TAX_APPLY_NIIT }}
          SEC_USER_AGENT:     ${{ secrets.SEC_USER_AGENT }}
          SMTP_HOST:          ${{ secrets.SMTP_HOST }}
          SMTP_PORT:          ${{ secrets.SMTP_PORT }}
          SMTP_USER:          ${{ secrets.SMTP_USER }}
          SMTP_PASS:          ${{ secrets.SMTP_PASS }}
          EMAIL_TO:           ${{ secrets.EMAIL_TO }}
        run: |
          python analyze_portfolio.py \\
            --source robinhood \\
            --include-watchlists \\
            --save-positions positions.csv \\
            --out report.html

      - name: Build site
        run: |
          mkdir -p _site
          cp report.html _site/index.html
          TIMESTAMP=$(date -u '+%B %d, %Y at %I:%M %p UTC')
          sed -i "s|<body>|<body><div style='background:#1a2028;color:#8b95a3;font-size:11px;text-align:center;padding:6px;'>Auto-updated: ${TIMESTAMP}</div>|" _site/index.html

      - name: Upload Pages artifact
        uses: actions/upload-pages-artifact@v3
        with:
          path: _site

  deploy:
    needs: analyze
    runs-on: ubuntu-latest
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}

    steps:
      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@v4
"""


if __name__ == "__main__":
    main()
