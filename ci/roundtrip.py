#!/usr/bin/env python3
"""Round-trip check: JSON on stdin -> ANSI bytes on stdout, under hostile encodings.

The status line runs as a subprocess with no console attached. That is precisely
where Python stops defaulting to UTF-8 and falls back to the system ANSI
codepage - cp1252 on Windows - which corrupts both the separator it prints and
the accented session name it receives.

This runs the real script the way Claude Code runs it and compares BYTES, never
what a terminal shows. A PowerShell pipe re-encodes and would fake the result.

Each scenario runs TWICE, and the second run is the one that earns its keep:

  -S -E   how the README tells you to install it. `-E` makes Python ignore every
          PYTHON* variable, so this proves the recommended install is immune to
          a dirty environment - but it also means the variables below are not
          what is being tested. On their own, these four runs are one run.
  -S      no `-E`, so PYTHONUTF8, PYTHONIOENCODING and PYTHONLEGACYWINDOWSSTDIO
          actually take effect. This is what proves the SCRIPT defends itself
          (reading `sys.stdin.buffer`, forcing `sys.stdout.reconfigure`) rather
          than being carried by a flag someone can drop from the command line.

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

# ("label shown in the log", flags passed to the interpreter)
MODES = (
    ("-S -E", ["-S", "-E"]),
    ("-S", ["-S"]),
)

failures = []
for mode_label, flags in MODES:
    print(f"[{mode_label}]")
    for label, extra in SCENARIOS:
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("PYTHONUTF8", "PYTHONIOENCODING", "PYTHONLEGACYWINDOWSSTDIO")
        }
        env.update(extra)
        proc = subprocess.run(
            [sys.executable, *flags, SCRIPT], input=RAW, capture_output=True, env=env
        )
        where = f"{mode_label} / {label}"
        if proc.returncode != 0:
            failures.append(f"{where}: exit {proc.returncode} / {proc.stderr[:200]!r}")
            continue
        out = proc.stdout
        if ACCENT not in out:
            failures.append(f"{where}: accent did not survive as UTF-8 -> {out[:120]!r}")
        elif b"\x1b[" not in out:
            failures.append(f"{where}: no ANSI colour in the output -> {out[:120]!r}")
        else:
            print(f"  {label}: OK ({len(out)} bytes)")

if failures:
    for line in failures:
        print(f"FAIL {line}")
    sys.exit(1)
print("OK - round-trip (0 failure(s))")
