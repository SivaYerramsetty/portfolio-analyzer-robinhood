"""
fix_js_braces.py
----------------
Fixes the broken JavaScript in analyze_portfolio.py.

The bottom <script> block in generate_html_report() uses {{ and }} 
(Python f-string escaping) but is inside a plain triple-quoted string, 
not an f-string. This makes the braces appear literally in the HTML,
breaking the theme toggle and section refresh JS.

This script corrects all {{ → { and }} → } in the affected JS blocks.

Usage:
    python fix_js_braces.py
    python fix_js_braces.py --file /path/to/analyze_portfolio.py
"""

import argparse
import re
import shutil
import sys
from pathlib import Path


def fix_file(src: Path, dry_run: bool = False) -> None:
    original = src.read_text(encoding="utf-8")

    # The broken region starts at the closing </style> tag that ends the
    # generate_html_report f-string and the plain html += """ block that follows.
    # We need to fix {{ → { and }} → } only inside the plain (non-f-string)
    # triple-quoted blocks at the bottom of generate_html_report.
    #
    # Strategy: find the two broken JS blocks by their unique markers and
    # replace {{ / }} with { / } inside them.

    # ── Block 1: theme toggle IIFE ──────────────────────────────────────────
    # Marker: the btn.addEventListener click handler with {{ double braces
    THEME_BROKEN = (
        "  btn.addEventListener('click', function() {{\n"
        "    var next = currentTheme() === 'dark' ? 'light' : 'dark';\n"
        "    document.documentElement.setAttribute('data-theme', next);\n"
        "    try {{ localStorage.setItem('portfolio-theme', next); }} catch (e) {{}}\n"
        "    setIcon();\n"
        "  }});\n"
        "}})();\n"
        "\n"
        "/* ---------- Refresh button ---------- */\n"
        "function refreshReport() {{\n"
        "  var btn = document.getElementById('refreshBtn');\n"
        "  if (btn) btn.classList.add('spinning');\n"
        "  // Brief visual feedback, then reload the page (picks up the latest saved report.html)\n"
        "  setTimeout(function() {{ location.reload(); }}, 300);\n"
        "}}"
    )

    THEME_FIXED = (
        "  btn.addEventListener('click', function() {\n"
        "    var next = currentTheme() === 'dark' ? 'light' : 'dark';\n"
        "    document.documentElement.setAttribute('data-theme', next);\n"
        "    try { localStorage.setItem('portfolio-theme', next); } catch (e) {}\n"
        "    setIcon();\n"
        "  });\n"
        "})();\n"
        "\n"
        "/* ---------- Refresh button ---------- */\n"
        "function refreshReport() {\n"
        "  var btn = document.getElementById('refreshBtn');\n"
        "  if (btn) btn.classList.add('spinning');\n"
        "  // Brief visual feedback, then reload the page (picks up the latest saved report.html)\n"
        "  setTimeout(function() { location.reload(); }, 300);\n"
        "}"
    )

    # ── Block 2: section refresh IIFE ───────────────────────────────────────
    # Find the whole section refresh IIFE (starts with /* --- Section refresh)
    # and replace all {{ / }} with { / } inside it.
    SECTION_START_MARKER = "/* ---------- Section refresh ---------- */\n(function() {{"
    SECTION_END_MARKER   = "}})();\n</script>\n</body></html>\n\"\"\"\n    return html"

    result = original

    # Fix 1: theme toggle + refreshReport
    if THEME_BROKEN in result:
        result = result.replace(THEME_BROKEN, THEME_FIXED, 1)
        print("  Fix 1 ✓  theme toggle + refreshReport braces corrected")
    elif THEME_FIXED in result:
        print("  Fix 1 —  already correct, skipped")
    else:
        print("  Fix 1 ✗  theme toggle block NOT FOUND — may need manual fix")

    # Fix 2: section refresh IIFE — find the block and fix all {{ / }}
    start_idx = result.find(SECTION_START_MARKER)
    end_idx   = result.find(SECTION_END_MARKER)

    if start_idx == -1 or end_idx == -1:
        # Try already-fixed markers
        fixed_start = "/* ---------- Section refresh ---------- */\n(function() {"
        if result.find(fixed_start) != -1:
            print("  Fix 2 —  section refresh already correct, skipped")
        else:
            print("  Fix 2 ✗  section refresh IIFE NOT FOUND — may need manual fix")
    else:
        end_idx_full = end_idx + len(SECTION_END_MARKER)
        broken_block = result[start_idx:end_idx_full]

        # Replace {{ → { and }} → } throughout this block
        fixed_block = broken_block.replace("{{", "{").replace("}}", "}")

        # But we need to restore the Python-level escaping for any CSS/style
        # braces that were legitimately double-escaped in an f-string context.
        # In this block there are none — it's all JS, no CSS variables.
        # The SECTION_END_MARKER itself contains the Python string close,
        # which we need to keep intact.
        result = result[:start_idx] + fixed_block + result[end_idx_full:]
        print("  Fix 2 ✓  section refresh IIFE braces corrected")

    if result == original:
        print("\nNo changes made — file may already be correct or patterns not found.")
        return

    if dry_run:
        print("\n--dry-run: no files written.")
        # Show a diff summary
        orig_lines = original.splitlines()
        new_lines  = result.splitlines()
        changed = sum(1 for a, b in zip(orig_lines, new_lines) if a != b)
        print(f"  Would change ~{changed} lines.")
        return

    bak = src.with_suffix(".py.bak2")
    shutil.copy2(src, bak)
    print(f"\nBackup written to {bak}")

    src.write_text(result, encoding="utf-8")
    print(f"✓ {src} fixed successfully.")
    print()
    print("Next steps:")
    print("  1. Regenerate the report:")
    print("     python analyze_portfolio.py --source robinhood --include-watchlists --out report.html")
    print("  2. Open http://localhost:5000  (make sure server.py is running)")
    print("  3. Filters and sorting should now work correctly.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default="analyze_portfolio.py")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    src = Path(args.file)
    if not src.exists():
        print(f"ERROR: {src} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"Fixing {src} …")
    fix_file(src, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
