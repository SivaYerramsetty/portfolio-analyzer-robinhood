"""
update_rh_session_secret.py
---------------------------
Upload your working local Robinhood session to GitHub so CI can use it.

Why: a fresh password login from GitHub Actions triggers Robinhood's
device-approval challenge, which needs a human. Instead, CI reuses YOUR
session: this script base64-encodes ~/.tokens/robinhood.pickle and stores
it (encrypted) as the RH_SESSION_B64 repository secret. robinhood_source
.login() falls back to that seed whenever the CI session cache is stale.

Run it whenever the workflow fails with "No valid Robinhood session":
    python update_rh_session_secret.py

Requirements: pip install pynacl requests
Auth: uses the GH_TOKEN env var, else the `gh` CLI's own credential
(`gh auth token`). It deliberately does NOT read a token from the git
remote URL: git echoes that URL on any remote error, so a token parked
there leaks into terminal output and CI logs.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

SECRET_NAME = "RH_SESSION_B64"
PICKLE_PATH = Path.home() / ".tokens" / "robinhood.pickle"


def _gh(*args: str) -> str:
    """Run a `gh` subcommand, returning stripped stdout ('' if gh is missing
    or the call fails)."""
    try:
        r = subprocess.run(("gh",) + args, capture_output=True, text=True,
                           cwd=Path(__file__).parent)
        return r.stdout.strip() if r.returncode == 0 else ""
    except FileNotFoundError:
        return ""


def _repo_and_token() -> tuple[str, str]:
    """Resolve (repo, token), preferring the `gh` CLI's own credential.

    The remote URL used to be the fallback token source, but embedding a token
    there is a liability — git prints the full URL on any remote error, so it
    leaks into terminal output and CI logs. The remote is now tokenless, so the
    order is: GH_TOKEN, then `gh auth token`. The repo name comes from `gh` too,
    falling back to parsing the remote (which needs no token).
    """
    token = os.environ.get("GH_TOKEN", "").strip() or _gh("auth", "token")

    repo = _gh("repo", "view", "--json", "nameWithOwner",
               "--jq", ".nameWithOwner")
    if not repo:
        url = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, cwd=Path(__file__).parent,
        ).stdout.strip()
        m_repo = re.search(
            r"github\.com[:/](?:[^@/]+@)?([^/]+/[^/\s]+?)(?:\.git)?/?$", url)
        repo = m_repo.group(1) if m_repo else ""

    if not repo or not token:
        sys.exit("ERROR: could not resolve repo/token. Either run `gh auth "
                 "login`, or set GH_TOKEN, and run from the repo directory.")
    return repo, token


def main() -> None:
    # Ensure the local session is valid (logs in / refreshes if needed,
    # which may prompt for device approval ON YOUR PHONE — that's fine here).
    print("[1/2] Validating local Robinhood session...")
    import robinhood_source as rhs
    rhs.login(verbose=True)
    if not PICKLE_PATH.exists():
        sys.exit(f"ERROR: {PICKLE_PATH} not found even after login.")

    repo, token = _repo_and_token()
    print(f"[2/2] Uploading secret {SECRET_NAME} to {repo}...")
    # Same encrypt-and-upload path CI uses to write back its rotated session,
    # so there's one implementation to keep correct.
    if not rhs.push_session_secret(PICKLE_PATH, repo=repo, token=token,
                                   verbose=True):
        sys.exit(f"ERROR: could not upload {SECRET_NAME} — see the message "
                 f"above.")
    print(f"✓ {SECRET_NAME} updated for {repo}. Re-run the workflow — CI will "
          f"seed its session from this secret whenever its cache is stale.")


if __name__ == "__main__":
    main()
