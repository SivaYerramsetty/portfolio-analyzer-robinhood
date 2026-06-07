#!/usr/bin/env python3
"""
setup_github.py
---------------
Interactive setup script — walks you through:
  1. Creating a private GitHub repo
  2. Pushing your project files
  3. Adding all secrets
  4. Enabling GitHub Pages
  5. Triggering the first run

Requirements:
    pip install requests PyGithub
    A GitHub account + personal access token

Usage:
    python setup_github.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def banner(msg): print(f"\n{BOLD}{CYAN}{'─'*54}\n  {msg}\n{'─'*54}{RESET}")
def ok(msg):     print(f"{GREEN}✓ {msg}{RESET}")
def warn(msg):   print(f"{YELLOW}⚠  {msg}{RESET}")
def err(msg):    print(f"{RED}✗ {msg}{RESET}")
def info(msg):   print(f"  {msg}")
def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    val = input(f"\n{BOLD}{prompt}{suffix}:{RESET} ").strip()
    return val or default


def check_deps():
    missing = []
    try:
        import github
    except ImportError:
        missing.append("PyGithub")
    try:
        import requests
    except ImportError:
        missing.append("requests")
    if missing:
        err(f"Missing packages: {', '.join(missing)}")
        print(f"\n  Install with:  pip install {' '.join(missing)}")
        sys.exit(1)


def run(cmd, cwd=None, capture=False):
    result = subprocess.run(
        cmd, shell=True, cwd=cwd,
        capture_output=capture, text=True
    )
    return result


def setup():
    banner("Portfolio Analyzer — GitHub Setup")

    check_deps()
    from github import Github, GithubException

    print("""
This script will:
  1. Create a private GitHub repo for your portfolio analyzer
  2. Push your project files
  3. Add your credentials as encrypted secrets
  4. Enable GitHub Pages
  5. Trigger the first report generation

Your Robinhood credentials will be stored as GitHub Encrypted Secrets
— they are encrypted with a public key and GitHub staff cannot read them.
""")

    # ── Step 1: GitHub token ─────────────────────────────────────────────────
    banner("Step 1 — GitHub Personal Access Token")
    print("""
Create a token at: https://github.com/settings/tokens/new
  ✓ Check: repo (full control)
  ✓ Check: workflow
  ✓ Check: admin:repo_hook (optional)

Token only needs to be created once.
""")
    token = ask("Paste your GitHub token")
    if not token:
        err("Token required.")
        sys.exit(1)

    g = Github(token)
    try:
        user = g.get_user()
        ok(f"Authenticated as: {user.login}")
    except GithubException as e:
        err(f"Invalid token: {e}")
        sys.exit(1)

    username = user.login

    # ── Step 2: Create repo ──────────────────────────────────────────────────
    banner("Step 2 — Create Private GitHub Repository")
    repo_name = ask("Repository name", "portfolio-analyzer")

    try:
        repo = user.get_repo(repo_name)
        ok(f"Repo already exists: {repo.html_url}")
    except GithubException:
        repo = user.create_repo(
            repo_name,
            private=True,
            description="Personal portfolio analyzer — powered by Robinhood + yfinance",
            auto_init=False,
        )
        ok(f"Created private repo: {repo.html_url}")

    # ── Step 3: Add secrets ──────────────────────────────────────────────────
    banner("Step 3 — Add Encrypted Secrets")
    print("Enter your credentials. Press Enter to skip optional ones.")
    print("These are encrypted and cannot be read back — even by you.\n")

    secrets = {}

    # Required
    secrets["RH_USERNAME"] = ask("Robinhood email (RH_USERNAME)")
    secrets["RH_PASSWORD"] = ask("Robinhood password (RH_PASSWORD)")

    # Optional but recommended
    val = ask("Finnhub API key (optional, press Enter to skip)")
    if val: secrets["FINNHUB_API_KEY"] = val

    val = ask("Tax filing status e.g. single/married_filing_jointly (optional)")
    if val: secrets["TAX_FILING_STATUS"] = val

    val = ask("Tax taxable income e.g. 150000 (optional)")
    if val: secrets["TAX_TAXABLE_INCOME"] = val

    val = ask("State tax rate e.g. 0.093 for 9.3% (optional)")
    if val: secrets["TAX_STATE_RATE"] = val

    val = ask("Apply NIIT? true/false (optional)")
    if val: secrets["TAX_APPLY_NIIT"] = val

    val = ask("SEC user agent e.g. 'Your Name email@example.com' (optional)")
    if val: secrets["SEC_USER_AGENT"] = val

    val = ask("Email SMTP host e.g. smtp.gmail.com (optional)")
    if val: secrets["SMTP_HOST"] = val

    val = ask("Email SMTP port e.g. 587 (optional)")
    if val: secrets["SMTP_PORT"] = val

    val = ask("Email SMTP user (optional)")
    if val: secrets["SMTP_USER"] = val

    val = ask("Email SMTP password / app password (optional)")
    if val: secrets["SMTP_PASS"] = val

    val = ask("Email recipient (optional)")
    if val: secrets["EMAIL_TO"] = val

    # Add secrets via GitHub API
    import requests as req
    from base64 import b64encode
    from nacl import encoding, public  # pip install PyNaCl

    # Get repo public key for secret encryption
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    key_resp = req.get(
        f"https://api.github.com/repos/{username}/{repo_name}/actions/secrets/public-key",
        headers=headers,
    )
    pub_key_data = key_resp.json()
    pub_key_id   = pub_key_data["key_id"]
    pub_key      = pub_key_data["key"]

    def encrypt_secret(public_key_b64: str, secret_value: str) -> str:
        pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder())
        box = public.SealedBox(pk)
        encrypted = box.encrypt(secret_value.encode())
        return b64encode(encrypted).decode()

    added = []
    for name, value in secrets.items():
        if not value:
            continue
        try:
            encrypted = encrypt_secret(pub_key, value)
            r = req.put(
                f"https://api.github.com/repos/{username}/{repo_name}/actions/secrets/{name}",
                headers=headers,
                json={"encrypted_value": encrypted, "key_id": pub_key_id},
            )
            if r.status_code in (201, 204):
                ok(f"Secret added: {name}")
                added.append(name)
            else:
                warn(f"Failed to add {name}: {r.text}")
        except Exception as e:
            warn(f"Could not encrypt {name}: {e}")
            warn("Install PyNaCl:  pip install PyNaCl")

    # ── Step 4: Push project files ───────────────────────────────────────────
    banner("Step 4 — Push Project to GitHub")

    project_dir = Path(__file__).parent
    remote_url  = f"https://{token}@github.com/{username}/{repo_name}.git"

    # Init git if needed
    if not (project_dir / ".git").exists():
        run("git init", cwd=project_dir)
        run(f"git remote add origin {remote_url}", cwd=project_dir)
        ok("Git initialized")
    else:
        # Update remote URL with token
        run(f"git remote set-url origin {remote_url}", cwd=project_dir)
        ok("Git remote updated")

    run("git add .", cwd=project_dir)
    run('git commit -m "Portfolio analyzer setup" --allow-empty', cwd=project_dir)

    result = run("git push -u origin main 2>&1", cwd=project_dir, capture=True)
    if result.returncode != 0:
        # Try master branch
        result = run("git push -u origin master 2>&1", cwd=project_dir, capture=True)
    if result.returncode == 0:
        ok("Code pushed to GitHub")
    else:
        warn("Push had issues — you may need to push manually")
        info(result.stdout)
        info(result.stderr)

    # ── Step 5: Enable GitHub Pages ──────────────────────────────────────────
    banner("Step 5 — Enable GitHub Pages")

    r = req.post(
        f"https://api.github.com/repos/{username}/{repo_name}/pages",
        headers={**headers, "Accept": "application/vnd.github+json"},
        json={"build_type": "workflow"},
    )
    if r.status_code in (201, 409):  # 409 = already enabled
        ok("GitHub Pages enabled")
    else:
        warn(f"Could not auto-enable Pages ({r.status_code})")
        warn("Enable manually: repo Settings → Pages → Source → GitHub Actions")

    # ── Step 6: Trigger first run ────────────────────────────────────────────
    banner("Step 6 — Trigger First Report Generation")

    r = req.post(
        f"https://api.github.com/repos/{username}/{repo_name}/actions/workflows/portfolio.yml/dispatches",
        headers=headers,
        json={"ref": "main"},
    )
    if r.status_code == 204:
        ok("First run triggered!")
    else:
        # Try master
        r = req.post(
            f"https://api.github.com/repos/{username}/{repo_name}/actions/workflows/portfolio.yml/dispatches",
            headers=headers,
            json={"ref": "master"},
        )
        if r.status_code == 204:
            ok("First run triggered!")
        else:
            warn("Could not auto-trigger — start manually:")
            info(f"  {repo.html_url}/actions → Portfolio Analyzer → Run workflow")

    # ── Done ─────────────────────────────────────────────────────────────────
    pages_url = f"https://{username}.github.io/{repo_name}"
    actions_url = f"{repo.html_url}/actions"

    print(f"""
{'='*54}
{BOLD}{GREEN}  ✓ Setup complete!{RESET}
{'='*54}

  {BOLD}Your report URL:{RESET}
    {GREEN}{pages_url}{RESET}

  {BOLD}Watch the first run:{RESET}
    {actions_url}

  {BOLD}Trigger a manual refresh anytime:{RESET}
    {actions_url} → Portfolio Analyzer → Run workflow

  {BOLD}Auto-refresh schedule:{RESET}
    Every weekday at 8:30 AM ET

  The first report takes ~5-10 minutes to generate.
  Bookmark {pages_url} on all your devices.

{'='*54}
""")


if __name__ == "__main__":
    setup()
