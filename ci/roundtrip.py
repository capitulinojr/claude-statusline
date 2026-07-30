#!/usr/bin/env python3
"""Round-trip check: JSON on stdin -> ANSI bytes on stdout, under hostile encodings.

The status line runs as a subprocess with no console attached. That is precisely
where Python stops defaulting to UTF-8 and falls back to the system ANSI
codepage - cp1252 on Windows - which corrupts both the separator it prints and
the accented session name it receives.

This runs the real script the way Claude Code runs it and compares BYTES, never
what a terminal shows. A PowerShell pipe re-encodes and would fake the result.

    python ci/roundtrip.py path/to/statusline.py
"""

import json
import os
import subprocess
import sys

SCRIPT = sys.argv[1] if len(sys.argv) > 1 else "statusline.py"

PAYLOAD = {
    "model": {"id": "claude-opus-5", "display_name": "Opus 5 (1M context)"},
    "effort": {"level": "high"},
    "session_name": "implementação · acentuação",
    "context_window": {"used_percentage": 11.0},
}
RAW = json.dumps(PAYLOAD, ensure_ascii=False).encode("utf-8")

# The accent, as correct UTF-8. `c3 a7` = ç. A lone `e7` would mean the output
# was written in cp1252; `c3 83 c2 a7` would mean it was double-encoded.
ACCENT = b"implementa\xc3\xa7\xc3\xa3o"

SCENARIOS = (
    ("clean", {}),
    ("utf8 mode off", {"PYTHONUTF8": "0"}),
    ("legacy windows stdio", {"PYTHONUTF8": "0", "PYTHONLEGACYWINDOWSSTDIO": "1"}),
    ("io forced to cp1252", {"PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"}),
)

failures = []
for label, extra in SCENARIOS:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO")
    }
    env.update(extra)
    proc = subprocess.run(
        [sys.executable, "-S", "-E", SCRIPT], input=RAW, capture_output=True, env=env
    )
    if proc.returncode != 0:
        failures.append(f"{label}: exit {proc.returncode} / {proc.stderr[:200]!r}")
        continue
    out = proc.stdout
    if ACCENT not in out:
        failures.append(f"{label}: accent did not survive as UTF-8 -> {out[:120]!r}")
    elif b"\x1b[" not in out:
        failures.append(f"{label}: no ANSI colour in the output -> {out[:120]!r}")
    else:
        print(f"  {label}: OK ({len(out)} bytes)")

if failures:
    for line in failures:
        print(f"FAIL {line}")
    sys.exit(1)
print("OK - round-trip (0 failure(s))")
