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
Auth: uses GH_TOKEN env var, or the token embedded in `git remote origin`.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
from pathlib import Path

import requests

SECRET_NAME = "RH_SESSION_B64"
PICKLE_PATH = Path.home() / ".tokens" / "robinhood.pickle"


def _repo_and_token() -> tuple[str, str]:
    token = os.environ.get("GH_TOKEN", "").strip()
    url = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True, text=True, cwd=Path(__file__).parent,
    ).stdout.strip()
    m = re.search(r"github\.com[:/](?:([^@/]+)@)?", url)
    if not token:
        m_tok = re.match(r"https://([^@]+)@github\.com/", url)
        if m_tok:
            token = m_tok.group(1)
    m_repo = re.search(r"github\.com[:/](?:[^@/]+@)?([^/]+/[^/\s]+?)(?:\.git)?/?$", url)
    repo = m_repo.group(1) if m_repo else ""
    if not repo or not token:
        sys.exit("ERROR: could not resolve repo/token. Set GH_TOKEN and run "
                 "from the repo directory.")
    return repo, token


def main() -> None:
    try:
        from nacl import encoding, public
    except ImportError:
        sys.exit("ERROR: PyNaCl is required to encrypt the secret.\n"
                 "Run: pip install pynacl")

    # Ensure the local session is valid (logs in / refreshes if needed,
    # which may prompt for device approval ON YOUR PHONE — that's fine here).
    print("[1/3] Validating local Robinhood session...")
    import robinhood_source as rhs
    rhs.login(verbose=True)
    if not PICKLE_PATH.exists():
        sys.exit(f"ERROR: {PICKLE_PATH} not found even after login.")

    payload = base64.b64encode(PICKLE_PATH.read_bytes()).decode()
    repo, token = _repo_and_token()
    headers = {"Authorization": f"token {token}",
               "Accept": "application/vnd.github+json"}

    print(f"[2/3] Fetching public key for {repo}...")
    r = requests.get(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        headers=headers, timeout=15)
    r.raise_for_status()
    key = r.json()

    sealed = public.SealedBox(
        public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    ).encrypt(payload.encode())

    print(f"[3/3] Uploading secret {SECRET_NAME}...")
    r = requests.put(
        f"https://api.github.com/repos/{repo}/actions/secrets/{SECRET_NAME}",
        headers=headers, timeout=15,
        json={"encrypted_value": base64.b64encode(sealed).decode(),
              "key_id": key["key_id"]})
    r.raise_for_status()
    print(f"✓ {SECRET_NAME} updated for {repo} "
          f"(HTTP {r.status_code}). Re-run the workflow — CI will seed its "
          f"session from this secret whenever its cache is stale.")


if __name__ == "__main__":
    main()
