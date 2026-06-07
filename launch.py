#!/usr/bin/env python3
"""
launch.py — Start Portfolio Analyzer + ngrok tunnel
----------------------------------------------------
Starts server.py and opens an ngrok tunnel.
Prints the public HTTPS URL you can open from any device.

Usage:
    pip install ngrok flask
    python launch.py
    python launch.py --port 8080   # if 5000 is taken by AirPlay
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_PORT  = 5000
SERVER_SCRIPT = Path(__file__).parent / "server.py"

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RED    = "\033[91m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def banner(msg): print(f"\n{BOLD}{CYAN}{msg}{RESET}")
def ok(msg):     print(f"{GREEN}✓ {msg}{RESET}")
def warn(msg):   print(f"{YELLOW}⚠  {msg}{RESET}")
def err(msg):    print(f"{RED}✗ {msg}{RESET}")
def info(msg):   print(f"  {msg}")

# ── Process registry ──────────────────────────────────────────────────────────
_procs: list[subprocess.Popen] = []

def _shutdown(sig=None, frame=None):
    print(f"\n{YELLOW}Shutting down…{RESET}")
    for p in _procs:
        try:
            p.terminate()
        except Exception:
            pass
    sys.exit(0)

signal.signal(signal.SIGINT,  _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ── Check ngrok is installed ──────────────────────────────────────────────────
def check_ngrok():
    try:
        import ngrok
        return ngrok
    except ImportError:
        err("ngrok not installed.")
        print()
        print("Install it with:")
        print("  pip install ngrok")
        sys.exit(1)

# ── Start Flask server ────────────────────────────────────────────────────────
def start_server(port: int) -> subprocess.Popen:
    if not SERVER_SCRIPT.exists():
        err(f"{SERVER_SCRIPT} not found. Make sure server.py is in the same folder.")
        sys.exit(1)

    env = os.environ.copy()
    env["PORT"] = str(port)

    proc = subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    _procs.append(proc)

    def _stream():
        for line in proc.stdout:
            print(f"  {CYAN}[server]{RESET} {line}", end="")
    threading.Thread(target=_stream, daemon=True).start()
    return proc


def wait_for_server(port: int, timeout: int = 15) -> bool:
    import socket
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


# ── QR code (optional) ────────────────────────────────────────────────────────
def print_qr(url: str) -> None:
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        print()
        qr.print_ascii(invert=True)
    except ImportError:
        pass


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token", default=None,
                    help="ngrok authtoken (optional — free account gives stable URLs)")
    args = ap.parse_args()
    port = args.port

    banner("Portfolio Analyzer — Launch")
    print()

    # 1. Check ngrok
    ngrok = check_ngrok()
    ok("ngrok found")

    # 2. Set auth token if provided
    if args.token:
        ngrok.set_auth_token(args.token)
        ok("ngrok auth token set")
    else:
        # Check env var
        token = os.environ.get("NGROK_AUTHTOKEN", "").strip()
        if token:
            ngrok.set_auth_token(token)
            ok("ngrok auth token loaded from environment")

    # 3. Start Flask server
    info(f"Starting Flask server on port {port}…")
    start_server(port)

    if not wait_for_server(port):
        err(f"Flask didn't start on port {port} within 15 seconds.")
        warn(f"If port {port} is in use (AirPlay?), try:  python launch.py --port 8080")
        _shutdown()
    ok(f"Flask server running on http://localhost:{port}")

    # 4. Open ngrok tunnel
    info("Opening ngrok tunnel…")
    try:
        listener = ngrok.forward(port, proto="http")
        public_url = listener.url()
    except Exception as e:
        err(f"ngrok tunnel failed: {e}")
        print()
        warn("If you see 'authentication failed', create a free account at")
        warn("https://dashboard.ngrok.com and run:")
        warn(f"  python launch.py --token YOUR_TOKEN_HERE")
        _shutdown()

    # ── Print access info ─────────────────────────────────────────────────────
    print()
    print("=" * 58)
    print(f"{BOLD}{GREEN}  ✓ Your portfolio is live!{RESET}")
    print("=" * 58)
    print()
    print(f"  {BOLD}Local (this Mac):{RESET}")
    print(f"    http://localhost:{port}")
    print()
    print(f"  {BOLD}From any device (phone, tablet, other computer):{RESET}")
    print(f"    {BOLD}{GREEN}{public_url}{RESET}")
    print()
    print("  Open either URL in any browser.")
    print("  The public URL works on any device on any network.")
    print()
    print(f"  {YELLOW}Keep this terminal open — closing it stops the server.{RESET}")
    print("=" * 58)

    # Print QR code for easy mobile access
    print_qr(public_url)

    print()
    info("Optional: pip install qrcode[pil]  for a scannable QR code")
    print()
    info("Press Ctrl+C to stop everything.")
    print()

    # Keep alive
    while True:
        for p in _procs:
            if p.poll() is not None:
                err("Flask server exited unexpectedly.")
                _shutdown()
        time.sleep(2)


if __name__ == "__main__":
    main()
