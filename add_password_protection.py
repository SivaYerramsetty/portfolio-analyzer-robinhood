#!/usr/bin/env python3
"""
add_password_protection.py
--------------------------
Patches a generated report.html with a password gate.

HOW IT WORKS
  • On load, checks sessionStorage for a valid auth token.
  • If absent, shows a full-screen password overlay.
  • On submit, computes SHA-256 of the entered password (via browser's
    built-in crypto.subtle — no external libs) and compares it to the
    hardcoded hash. On match, stores a token in sessionStorage and reveals
    the report. Wrong password shows a shake animation and clears the input.
  • Closing the tab / browser clears sessionStorage, so auth is per-session.

SECURITY NOTE
  The password hash is embedded in the HTML (which is public). This protects
  against casual discovery (e.g. Google indexing, someone stumbling on the URL)
  but NOT against a determined attacker who reads the source and cracks the hash
  offline. For personal use this is a good tradeoff — use a reasonably strong
  password (12+ chars, not a dictionary word).

USAGE
  # One-time: set your password hash (run this script with --set-password)
  python add_password_protection.py --set-password "your_password_here"

  # Apply to a report (run after analyze_portfolio.py)
  python add_password_protection.py report.html

  # Apply in-place (overwrites report.html)
  python add_password_protection.py report.html --in-place

  # Apply to a different output file
  python add_password_protection.py report.html --out protected_report.html

  # Remove protection from a file
  python add_password_protection.py report.html --remove

SETTING YOUR PASSWORD
  Option 1 — Edit this file: find PASSWORD_HASH below and replace it with
  the output of: python -c "import hashlib; print(hashlib.sha256(b'yourpassword').hexdigest())"

  Option 2 — Run with --set-password:
  python add_password_protection.py --set-password "yourpassword"
  This prints the hash and optionally updates this script automatically.
"""

import argparse
import hashlib
import re
import sys
from pathlib import Path

# ============================================================
# *** SET YOUR PASSWORD HASH HERE ***
#
# Generate with:
#   python -c "import hashlib; print(hashlib.sha256(b'your_password').hexdigest())"
#
# Default hash below is for "portfolio2024" — CHANGE THIS before deploying.
# ============================================================
PASSWORD_HASH = "509a51859e0e92dc368cc59bf16458681977295abcbf1314d5a06f852f6fec91"

# Marker injected into the HTML so we can detect already-patched files
# and cleanly remove the protection later.
_START_MARKER = "<!-- PASSWORD_GATE_START -->"
_END_MARKER = "<!-- PASSWORD_GATE_END -->"

# ============================================================
# The overlay HTML + JS, injected right after <body ...>
# Uses crypto.subtle (Web Crypto API) — built into all modern browsers.
# No external libraries needed.
# ============================================================

def _build_overlay(password_hash: str) -> str:
    return f"""{_START_MARKER}
<div id="pw-gate" style="
  position:fixed;inset:0;z-index:99999;
  background:linear-gradient(135deg,#0f1419 0%,#1a2028 100%);
  display:flex;align-items:center;justify-content:center;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <div style="
    background:#1a2028;border:1px solid #2d3540;border-radius:16px;
    padding:40px 48px;text-align:center;max-width:380px;width:90%;
    box-shadow:0 20px 60px rgba(0,0,0,0.6);">
    <div style="font-size:40px;margin-bottom:16px;">🔒</div>
    <div style="font-size:22px;font-weight:700;color:#e8eaed;margin-bottom:6px;">
      Portfolio Report</div>
    <div style="font-size:13px;color:#8b95a3;margin-bottom:28px;">
      Enter password to access</div>
    <input id="pw-input" type="password" placeholder="Password"
      autocomplete="current-password"
      style="
        width:100%;padding:12px 16px;font-size:15px;
        border:1px solid #3a4250;border-radius:10px;
        background:#0f1419;color:#e8eaed;outline:none;
        margin-bottom:14px;box-sizing:border-box;
        transition:border-color 0.2s;">
    <button id="pw-submit" style="
      width:100%;padding:12px;font-size:15px;font-weight:600;
      background:#4a90e2;color:#fff;border:none;border-radius:10px;
      cursor:pointer;transition:background 0.2s;">
      Unlock Report
    </button>
    <div id="pw-error" style="
      color:#f87171;font-size:13px;margin-top:12px;
      min-height:18px;opacity:0;transition:opacity 0.2s;">
      Incorrect password
    </div>
  </div>
</div>
<style>
  @keyframes pw-shake {{
    0%,100% {{ transform:translateX(0); }}
    20%,60%  {{ transform:translateX(-6px); }}
    40%,80%  {{ transform:translateX(6px); }}
  }}
  #pw-gate.shake {{ animation:pw-shake 0.4s ease; }}
  #pw-input:focus {{ border-color:#4a90e2 !important;
                     box-shadow:0 0 0 3px rgba(74,144,226,0.2); }}
</style>
<script>
(function() {{
  var CORRECT_HASH = "{password_hash}";
  var SESSION_KEY  = "pw_auth_ok";

  // If already authenticated this session, remove the gate immediately.
  try {{
    if (sessionStorage.getItem(SESSION_KEY) === "1") {{
      var g = document.getElementById("pw-gate");
      if (g) g.remove();
      return;
    }}
  }} catch(e) {{}}

  // Otherwise freeze the body scroll while gate is up.
  document.body.style.overflow = "hidden";

  var input  = document.getElementById("pw-input");
  var submit = document.getElementById("pw-submit");
  var errDiv = document.getElementById("pw-error");
  var gate   = document.getElementById("pw-gate");

  function hexDigest(buffer) {{
    return Array.from(new Uint8Array(buffer))
      .map(function(b) {{ return b.toString(16).padStart(2,"0"); }})
      .join("");
  }}

  function unlock() {{
    var pw = input.value;
    if (!pw) return;
    submit.disabled = true;
    var encoded = new TextEncoder().encode(pw);
    crypto.subtle.digest("SHA-256", encoded).then(function(buf) {{
      var hex = hexDigest(buf);
      if (hex === CORRECT_HASH) {{
        try {{ sessionStorage.setItem(SESSION_KEY, "1"); }} catch(e) {{}}
        gate.style.transition = "opacity 0.3s";
        gate.style.opacity = "0";
        setTimeout(function() {{
          gate.remove();
          document.body.style.overflow = "";
        }}, 300);
      }} else {{
        // Wrong password: shake + show error + clear input
        gate.classList.remove("shake");
        void gate.offsetWidth; // force reflow to restart animation
        gate.classList.add("shake");
        errDiv.style.opacity = "1";
        input.value = "";
        input.focus();
        setTimeout(function() {{ errDiv.style.opacity = "0"; }}, 2500);
        submit.disabled = false;
      }}
    }}).catch(function() {{
      // crypto.subtle not available (HTTP context); fall back to alert
      if (pw === prompt("Enter password:","")) {{
        gate.remove(); document.body.style.overflow = "";
      }}
      submit.disabled = false;
    }});
  }}

  submit.addEventListener("click", unlock);
  input.addEventListener("keydown", function(e) {{
    if (e.key === "Enter") unlock();
  }});
  input.focus();
}})();
</script>
{_END_MARKER}"""


def patch_html(html: str, password_hash: str) -> str:
    """Inject the password gate into the HTML string. Idempotent."""
    # Remove any existing gate first (idempotency)
    html = remove_gate(html)
    # Insert right after the opening <body> tag (handles attributes like class="")
    body_match = re.search(r"<body[^>]*>", html, re.IGNORECASE)
    if not body_match:
        print("WARNING: <body> tag not found — injecting at start of file.", file=sys.stderr)
        return _build_overlay(password_hash) + "\n" + html
    insert_at = body_match.end()
    return html[:insert_at] + "\n" + _build_overlay(password_hash) + html[insert_at:]


def remove_gate(html: str) -> str:
    """Remove a previously injected password gate. Safe to call on unpatched HTML."""
    pattern = re.compile(
        re.escape(_START_MARKER) + r".*?" + re.escape(_END_MARKER),
        re.DOTALL,
    )
    return pattern.sub("", html)


def is_patched(html: str) -> bool:
    return _START_MARKER in html


# ============================================================
# CLI
# ============================================================

def main():
    ap = argparse.ArgumentParser(
        description="Add/remove password protection from a generated report.html",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("report", nargs="?", default="report.html",
                    help="Path to the HTML report (default: report.html)")
    ap.add_argument("--out", default=None,
                    help="Output path (default: report_protected.html, "
                         "or in-place if --in-place)")
    ap.add_argument("--in-place", action="store_true",
                    help="Overwrite the input file")
    ap.add_argument("--remove", action="store_true",
                    help="Remove password protection from the file")
    ap.add_argument("--set-password", metavar="PASSWORD",
                    help="Print the SHA-256 hash for the given password "
                         "(use this to update PASSWORD_HASH in this script)")
    ap.add_argument("--hash", metavar="PASSWORD",
                    help="Alias for --set-password")
    args = ap.parse_args()

    # --set-password / --hash: just print the hash and optionally auto-update
    pw_to_hash = args.set_password or args.hash
    if pw_to_hash:
        h = hashlib.sha256(pw_to_hash.encode()).hexdigest()
        print(f"\nPassword : {pw_to_hash}")
        print(f"SHA-256  : {h}")
        print(f"\nTo use this hash, either:")
        print(f"  1. Edit this script: set PASSWORD_HASH = \"{h}\"")
        print(f"  2. Pass it at runtime: (not yet implemented — edit the script)")
        print(f"\nOr run:")
        print(f'  python -c "')
        print(f'  import pathlib, re')
        print(f'  p = pathlib.Path(__file__)')
        print(f'  p.write_text(re.sub(')
        print(f'    r\'PASSWORD_HASH = \"[0-9a-f]{{64}}\"\',')
        print(f'    \'PASSWORD_HASH = \"{h}\"\',')
        print(f'    p.read_text()))')
        print(f'  "')
        # Auto-update this script's PASSWORD_HASH line
        this_file = Path(__file__)
        try:
            src = this_file.read_text()
            new_src = re.sub(
                r'PASSWORD_HASH = "[0-9a-f]{64}"',
                f'PASSWORD_HASH = "{h}"',
                src,
            )
            if new_src != src:
                this_file.write_text(new_src)
                print(f"\n✓ Auto-updated PASSWORD_HASH in {this_file.name}")
            else:
                print("\n(Could not auto-update — update PASSWORD_HASH manually)")
        except Exception as e:
            print(f"\n(Auto-update failed: {e} — update PASSWORD_HASH manually)")
        return

    # Normal patch / remove mode
    report_path = Path(args.report)
    if not report_path.exists():
        print(f"ERROR: {report_path} not found.", file=sys.stderr)
        sys.exit(1)

    html = report_path.read_text(encoding="utf-8")

    if args.remove:
        if not is_patched(html):
            print(f"{report_path}: no password gate found — nothing to remove.")
            return
        result = remove_gate(html)
        out = report_path if args.in_place else (
            Path(args.out) if args.out else
            report_path.with_name(report_path.stem + "_unprotected.html")
        )
        out.write_text(result, encoding="utf-8")
        print(f"✓ Password gate removed → {out}")
        return

    if is_patched(html):
        print(f"{report_path}: already has a password gate — re-patching with current hash.")

    result = patch_html(html, PASSWORD_HASH)

    if args.in_place:
        out = report_path
    elif args.out:
        out = Path(args.out)
    else:
        out = report_path.with_name(report_path.stem + "_protected.html")

    out.write_text(result, encoding="utf-8")
    print(f"✓ Password gate injected → {out}")
    print(f"  Hash used: {PASSWORD_HASH[:16]}...  (change PASSWORD_HASH to update)")
    print(f"  To change password: python {Path(__file__).name} --set-password 'newpassword'")


if __name__ == "__main__":
    main()
