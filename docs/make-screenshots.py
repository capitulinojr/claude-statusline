#!/usr/bin/env python3
"""Regenerates the three README screenshots from the real `statusline.py`.

The pictures in the README are documentation, so they drift the moment a color,
a separator or a field changes. This script removes the drift: it imports the
published `statusline.py`, calls its own `render()` with pinned inputs, and
paints the resulting ANSI into a PNG. Whatever the bar prints is what the README
shows.

Only the INPUTS are pinned - the quota percentages and the reset timers. Colors,
separators, spacing and the rules that open and close line 2 all come from the
module, so a change in any of them lands in the picture on the next run.

    python docs/make-screenshots.py            # writes the three PNGs
    python docs/make-screenshots.py --html     # writes the HTML only, no browser
    python docs/make-screenshots.py --selftest # tests this script

Rendering needs a Chromium-family browser in headless mode. The script looks for
one in the usual places; override with `CHROME=/path/to/binary`.

The PNGs are reproducible in CONTENT, not byte for byte: font selection, hinting
and antialiasing differ between systems and browser versions. Regenerate them on
one machine rather than diffing them across several.
"""

from __future__ import annotations

import argparse
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DOCS = Path(__file__).resolve().parent
PACKAGE = DOCS.parent
MODULE = PACKAGE / "statusline.py"

# The bar renders in 256-color mode and nothing else: every color in the module
# is an `SGR 38;5;N`. So the palette this script needs is exactly the xterm-256
# cube, and the six colors below are the ones a terminal picks for 0-15.
BASE_16 = (
    "#000000", "#cd3131", "#0dbc79", "#e5e510", "#2472c8", "#bc3fbc", "#11a8cd", "#e5e5e5",
    "#666666", "#f14c4c", "#23d18b", "#f5f543", "#3b8eea", "#d670d6", "#29b8db", "#e5e5e5",
)
CUBE_STEPS = (0, 95, 135, 175, 215, 255)

# The terminal the screenshots imitate: dark card, rounded corners, generous
# padding.
BACKGROUND = "#0d1117"
FONT_STACK = "Menlo, 'DejaVu Sans Mono', 'Liberation Mono', monospace"
FONT_SIZE_PX = 34
LINE_HEIGHT = 1.9
PAD_X_PX = 44
PAD_Y_PX = 26
RADIUS_PX = 22
SCALE = 2  # device pixel ratio: the PNGs are retina-sized
# Advance width of one character, as a fraction of the font size. The browser
# cannot report the card's size back to a `--screenshot` run, so the window is
# sized from the text instead - and in every monospace face in the stack above,
# one character advances exactly 0.6em.
CHAR_RATIO = 0.6
SHOT_TIMEOUT_S = 60  # how long to wait for one PNG to appear and settle

SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def load_statusline():
    """Imports the sibling `statusline.py` as a module.

    By path, not by name: the file lives next to this one and is not on
    `sys.path`, and importing it is the whole point - a screenshot generated
    from a reimplementation would prove nothing.
    """
    spec = importlib.util.spec_from_file_location("statusline_published", MODULE)
    if spec is None or spec.loader is None:  # pragma: no cover - unreachable in practice
        raise SystemExit(f"could not import {MODULE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def xterm_color(index: int) -> str:
    """The hex color a terminal shows for `SGR 38;5;<index>`."""
    if index < 16:
        return BASE_16[index]
    if index < 232:
        index -= 16
        return "#%02x%02x%02x" % (
            CUBE_STEPS[index // 36],
            CUBE_STEPS[(index // 6) % 6],
            CUBE_STEPS[index % 6],
        )
    level = 8 + (index - 232) * 10
    return "#%02x%02x%02x" % (level, level, level)


def ansi_to_html(text: str) -> str:
    """One ANSI line into one HTML line, keeping color, bold and italic.

    The module emits only `38;5;N`, `1` (bold), `3` (italic) and `0` (reset), so
    this handles those and drops anything else instead of guessing - an unknown
    code in the picture would be a lie about what the terminal does.
    """
    # `re.split` on a capturing pattern interleaves [text, codes, text, codes,
    # ...], so the codes sit on the odd indices and the text they open sits on
    # the next one.
    parts = SGR_RE.split(text)
    out = []
    open_spans = 0
    out.append(html.escape(parts[0]))
    for i in range(1, len(parts), 2):
        codes = [c for c in parts[i].split(";") if c != ""]
        chunk = html.escape(parts[i + 1]) if i + 1 < len(parts) else ""
        if not codes or codes == ["0"]:
            out.append("</span>" * open_spans)
            open_spans = 0
            out.append(chunk)
            continue
        style = []
        j = 0
        while j < len(codes):
            code = codes[j]
            if code == "1":
                style.append("font-weight:700")
            elif code == "3":
                style.append("font-style:italic")
            elif code == "38" and codes[j + 1 : j + 2] == ["5"] and j + 2 < len(codes):
                style.append(f"color:{xterm_color(int(codes[j + 2]))}")
                j += 2
            j += 1
        out.append(f'<span style="{";".join(style)}">')
        open_spans += 1
        out.append(chunk)
    out.append("</span>" * open_spans)
    # A blank line still has to occupy its height, or the card collapses.
    body = "".join(out)
    return body if body.strip() else "&nbsp;"


def plain(text: str) -> str:
    """The line with every SGR sequence removed - what the eye actually sees."""
    return SGR_RE.sub("", text)


def card_size(lines: list[str]) -> tuple[int, int]:
    """CSS pixel size of the card holding `lines`, padding included.

    The card is then stretched to fill the whole window, so this size IS the
    screenshot: no white page around it, nothing to crop afterwards.
    """
    columns = max((len(plain(l)) for l in lines), default=0)
    width = round(columns * FONT_SIZE_PX * CHAR_RATIO) + 2 * PAD_X_PX
    height = round(len(lines) * FONT_SIZE_PX * LINE_HEIGHT) + 2 * PAD_Y_PX
    return width, height


def page_html(lines: list[str]) -> str:
    """The full HTML page for one card of `lines`."""
    rows = "\n".join(f"<div>{ansi_to_html(l)}</div>" for l in lines)
    return f"""<!doctype html>
<meta charset="utf-8">
<style>
  html, body {{ margin: 0; padding: 0; background: {BACKGROUND}; }}
  #card {{
    box-sizing: border-box;
    width: 100vw;
    height: 100vh;
    overflow: hidden;
    background: {BACKGROUND};
    border-radius: {RADIUS_PX}px;
    padding: {PAD_Y_PX}px {PAD_X_PX}px;
    font-family: {FONT_STACK};
    font-size: {FONT_SIZE_PX}px;
    line-height: {LINE_HEIGHT};
    color: #e5e5e5;
  }}
  /* `pre` on the ROWS, not on the card: on the card, the newlines this template
     puts around the rows would each render as an extra blank line and push the
     real ones out of the shot. */
  #card > div {{ white-space: pre; }}
</style>
<div id="card">
{rows}
</div>
"""


def runnable(path: Path) -> bool:
    """Is this an actual program this process can execute?

    `exists()` alone accepts a directory or a data file, and the failure then
    surfaces as an unreadable browser error instead of a clear one here.
    """
    return path.is_file() and os.access(str(path), os.X_OK)


def find_chrome() -> str | None:
    """Path to a headless-capable Chromium, or None.

    `CHROME` wins, then whatever is on PATH, then the usual install locations on
    the three systems, then a Playwright download cache if one exists.
    """
    override = os.environ.get("CHROME")
    if override:
        return override if runnable(Path(override)) else None
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
                 "chrome"):
        found = shutil.which(name)
        if found:
            return found
    home = Path.home()
    program_files = [
        Path(os.environ[var])
        for var in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")
        if os.environ.get(var)
    ]
    candidates = [
        # macOS
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        # Linux
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
        Path("/snap/bin/chromium"),
    ]
    candidates.extend(
        base / vendor / "Application" / "chrome.exe"
        for base in program_files
        for vendor in ("Google/Chrome", "Chromium")
    )
    # Playwright's download cache, wherever this system keeps it.
    caches = [
        home / "Library" / "Caches" / "ms-playwright",   # macOS
        home / ".cache" / "ms-playwright",               # Linux
    ]
    if os.environ.get("LOCALAPPDATA"):                   # Windows
        caches.append(Path(os.environ["LOCALAPPDATA"]) / "ms-playwright")
    for cache in caches:
        if not cache.is_dir():
            continue
        for entry in sorted(cache.glob("chromium*-*"), reverse=True):
            candidates.extend(entry.glob("chrome-mac*/*.app/Contents/MacOS/*"))
            candidates.extend(entry.glob("chrome-linux*/chrome"))
            candidates.extend(entry.glob("chrome-win*/chrome.exe"))
    for candidate in candidates:
        if runnable(candidate):
            return str(candidate)
    return None


def complete_png(path: Path) -> bool:
    """Is this a whole PNG - right signature, and an IEND chunk at the end?

    Waiting for the file to stop growing is not proof that it finished: a
    browser killed mid-write leaves a file that is stable because nothing is
    writing it anymore. The end marker is what says the picture is complete.
    """
    try:
        with path.open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                return False
            handle.seek(0, os.SEEK_END)
            if handle.tell() < 20:
                return False
            handle.seek(-12, os.SEEK_END)
            return handle.read(12)[4:8] == b"IEND"
    except OSError:
        return False


def shoot(chrome: str, html_path: Path, png_path: Path, size: tuple[int, int]) -> None:
    """Screenshots one HTML card into `png_path`.

    The window is sized to the card, so the shot IS the card. Waiting for the
    browser to EXIT is not part of the contract: Chrome writes the PNG and then
    keeps its process alive, so the wait is on the FILE - it has to become a
    complete PNG - and the process is killed once the picture is on disk.

    The browser shoots into a temporary file that is moved over `png_path` only
    after it validates. A run that fails therefore leaves the previous picture
    untouched, instead of leaving the README with a broken image.
    """
    width, height = size
    # The pending file sits NEXT TO its destination, not in the system temp
    # directory: `os.replace` is only atomic within one filesystem, and on Linux
    # `/tmp` is routinely a different one from the checkout. It keeps the `.png`
    # extension - Chrome picks the output format from it and writes nothing at
    # all for a name ending in anything else.
    pending = png_path.with_name(png_path.stem + ".pending.png")
    # A run interrupted between the write and the move leaves a COMPLETE pending
    # file behind. Without this, the next run finds it immediately, kills its own
    # browser and publishes the stale picture as if it had just been shot.
    pending.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="statusline-shot-") as workdir:
        command = [
            chrome,
            # `--headless=new` explicitly: the old headless mode was removed in
            # Chrome 132, and a bare `--headless` there opens a real window.
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--user-data-dir={workdir}/profile",
            f"--force-device-scale-factor={SCALE}",
            f"--window-size={width},{height}",
            f"--screenshot={pending}",
            html_path.as_uri(),
        ]
        # Only where Chromium refuses to sandbox at all - running as root, which
        # is the usual container case. Dropping the sandbox by default would
        # give it up on every normal run for nothing.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            command.insert(2, "--no-sandbox")
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + SHOT_TIMEOUT_S
            while time.monotonic() < deadline:
                if complete_png(pending):
                    break
                time.sleep(0.25)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                # `wait` after `kill` too: without it the child is left as a
                # zombie, and on Windows the profile directory is still held
                # open when `TemporaryDirectory` tries to remove it.
                process.wait()
        if not complete_png(pending):
            pending.unlink(missing_ok=True)
            raise SystemExit(
                f"{chrome} wrote no complete {png_path.name} within {SHOT_TIMEOUT_S}s"
            )
        # `replace`, not `rename`: on Windows renaming onto an existing file
        # raises instead of overwriting it.
        os.replace(str(pending), str(png_path))


def build_payloads(module) -> dict[str, dict]:
    """The three cards, with every number pinned.

    The resets are stated as an offset from now, so the timers read the same on
    every run instead of counting down toward a hardcoded date. Each offset
    carries half a unit of slack: the bar TRUNCATES the remaining time, so an
    exact `4h 47m` would render as `4h 46m` by the time the render runs.
    """
    now = time.time()
    return {
        "statusline": {
            "model": {"id": "claude-opus-5[1m]", "display_name": "Opus 5 (1M context)"},
            "effort": {"level": "medium"},
            "session_name": "Tuning the status line",
            "context_window": {"used_percentage": 11.0},
            "rate_limits": {
                "five_hour": {
                    "used_percentage": 4.0,
                    "resets_at": now + 4 * 3600 + 47 * 60 + 30,
                },
                "seven_day": {
                    "used_percentage": 32.0,
                    "resets_at": now + 4 * 86400 + 14 * 3600 + 30 * 60,
                },
            },
        },
    }


def render_cards(module, transcript: Path) -> dict[str, list[str]]:
    """Renders the full bar once and slices it into the three cards.

    One render, three pictures: line 1 and line 2 of the README are crops of the
    same bar, so shooting them from separate renders is how they would drift
    apart.
    """
    payload = build_payloads(module)["statusline"]
    payload["transcript_path"] = str(transcript)

    real_fable = module.fable_cap_percent
    real_daily = module.daily_total_percent
    try:
        # Both caps are derived from the account's real spending on disk. Pinned
        # here so the picture is reproducible; the COLORS around them are still
        # computed by the module, from the pinned resets above.
        module.fable_cap_percent = lambda data: (37.1, 48.5)
        module.daily_total_percent = lambda data: 21.0
        bar = module.render(payload)
    finally:
        module.fable_cap_percent = real_fable
        module.daily_total_percent = real_daily

    lines = [l for l in bar.split("\n") if l.strip()]
    if len(lines) < 2:
        raise SystemExit(f"render produced {len(lines)} usable line(s), expected 2")
    return {
        "statusline": lines[:2],
        "statusline-line1": lines[:1],
        "statusline-line2": lines[1:2],
    }


def write_transcript(directory: Path) -> Path:
    """A one-entry transcript worth ~2.9M tokens, for the session counter."""
    path = directory / "transcript.jsonl"
    entry = {
        "timestamp": "2026-08-17T12:00:00.000Z",
        "message": {
            "model": "claude-opus-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 2_800_000, "output_tokens": 100_000},
        },
    }
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--html", action="store_true", help="write the HTML and stop")
    parser.add_argument("--selftest", action="store_true", help="test this script")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()

    module = load_statusline()
    with tempfile.TemporaryDirectory(prefix="statusline-docs-") as tmp:
        cards = render_cards(module, write_transcript(Path(tmp)))
        chrome = None if args.html else find_chrome()
        if not args.html and chrome is None:
            print("no Chromium found - set CHROME=/path/to/binary", file=sys.stderr)
            return 1
        for name, lines in cards.items():
            html_path = Path(tmp) / f"{name}.html"
            html_path.write_text(page_html(lines), encoding="utf-8")
            if args.html:
                target = DOCS / f"{name}.html"
                target.write_text(page_html(lines), encoding="utf-8")
                print(f"wrote {target}")
                continue
            png = DOCS / f"{name}.png"
            shoot(chrome, html_path, png, card_size(lines))
            print(f"wrote {png}")
    return 0


def selftest() -> int:
    failures: list[str] = []
    ran = 0

    def check(label, got, want=True):
        nonlocal ran
        ran += 1
        if got != want:
            failures.append(f"{label}: got {got!r}, wanted {want!r}")

    # The palette: the three regions of the xterm-256 space, against literals.
    check("index 0 is black", xterm_color(0), "#000000")
    check("index 15 is the bright gray", xterm_color(15), "#e5e5e5")
    check("index 196 is pure red", xterm_color(196), "#ff0000")
    check("index 21 is pure blue", xterm_color(21), "#0000ff")
    check("index 232 is the darkest gray", xterm_color(232), "#080808")
    check("index 255 is the lightest gray", xterm_color(255), "#eeeeee")
    check("index 237 is the rule's gray", xterm_color(237), "#3a3a3a")

    # ANSI to HTML: color, bold, italic, reset, and text that must survive.
    check("plain text goes through", ansi_to_html("abc"), "abc")
    check(
        "a color becomes a span",
        ansi_to_html("\x1b[38;5;196mred\x1b[0m"),
        '<span style="color:#ff0000">red</span>',
    )
    check(
        "bold and color ride together",
        ansi_to_html("\x1b[1;38;5;196mred\x1b[0m"),
        '<span style="font-weight:700;color:#ff0000">red</span>',
    )
    check(
        "italic survives",
        ansi_to_html("\x1b[3;38;5;247mname\x1b[0m"),
        '<span style="font-style:italic;color:#9e9e9e">name</span>',
    )
    check("HTML in the text is escaped", ansi_to_html("a<b>&c"), "a&lt;b&gt;&amp;c")
    check("an empty line keeps its height", ansi_to_html(" "), "&nbsp;")
    check(
        "every span closes",
        ansi_to_html("\x1b[38;5;1ma\x1b[38;5;2mb").count("<span"),
        ansi_to_html("\x1b[38;5;1ma\x1b[38;5;2mb").count("</span>"),
    )

    # A picture is only finished when it ends in IEND: "the file stopped growing"
    # is also true of a browser that was killed mid-write.
    with tempfile.TemporaryDirectory(prefix="statusline-png-test-") as tmp:
        area = Path(tmp)
        real = area / "real.png"
        # The smallest valid PNG there is: signature, IHDR, one IDAT, IEND.
        real.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
            b"\x1f\x15\xc4\x89"
            b"\x00\x00\x00\x0aIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        check("a whole PNG is accepted", complete_png(real))
        truncated = area / "truncated.png"
        truncated.write_bytes(real.read_bytes()[:-12])
        check("a PNG with no IEND is rejected", complete_png(truncated), False)
        empty = area / "empty.png"
        empty.write_bytes(b"")
        check("an empty file is rejected", complete_png(empty), False)
        wrong = area / "wrong.png"
        wrong.write_bytes(b"GIF89a" + b"\x00" * 40)
        check("a file that is not a PNG is rejected", complete_png(wrong), False)
        check("a missing file is rejected", complete_png(area / "gone.png"), False)
        # `runnable` has to separate a program from anything else that exists.
        check("a directory is not runnable", runnable(area), False)
        check("a data file is not runnable", runnable(real), False)
        check("a missing path is not runnable", runnable(area / "gone"), False)
        script = area / "run.sh"
        script.write_text("#!/bin/sh\n", encoding="utf-8")
        script.chmod(0o755)
        check("an executable is runnable", runnable(script), os.name == "posix")

    # `find_chrome` and `shoot` against a FAKE browser. Without this the two
    # functions that make this a screenshot generator are never executed, and
    # the battery stays green with browser discovery and publication broken.
    if os.name == "posix":
        with tempfile.TemporaryDirectory(prefix="statusline-browser-test-") as tmp:
            area = Path(tmp)
            valid_png = (
                b"\x89PNG\r\n\x1a\n"
                b"\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
                b"\x1f\x15\xc4\x89"
                b"\x00\x00\x00\x0aIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
                b"\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            source = area / "source.png"
            source.write_bytes(valid_png)
            # Stands in for Chrome: reads `--screenshot=`, writes a valid PNG
            # there, and then keeps running - which is the behaviour the waiting
            # logic exists for.
            fake = area / "fake-chrome"
            fake.write_text(
                "#!/bin/sh\n"
                'for arg in "$@"; do\n'
                '  case "$arg" in --screenshot=*) out="${arg#--screenshot=}";; esac\n'
                "done\n"
                f'cp "{source}" "$out"\n'
                "sleep 30\n",
                encoding="utf-8",
            )
            fake.chmod(0o755)
            silent = area / "silent-chrome"
            silent.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
            silent.chmod(0o755)
            # Writes a TRUNCATED png first and the whole one a second later, the
            # way a real browser's output exists before it is finished. A wait
            # that only asks "does the file exist" publishes the truncated one.
            truncated_bytes = valid_png[:-12]
            (area / "half.png").write_bytes(truncated_bytes)
            slow = area / "slow-chrome"
            slow.write_text(
                "#!/bin/sh\n"
                'for arg in "$@"; do\n'
                '  case "$arg" in --screenshot=*) out="${arg#--screenshot=}";; esac\n'
                "done\n"
                f'cp "{area / "half.png"}" "$out"\n'
                "sleep 1\n"
                f'cp "{source}" "$out"\n'
                # Marks that it is still alive. `shoot` must have terminated it
                # long before this line runs.
                "sleep 3\n"
                f'touch "{area / "still-running"}"\n'
                "sleep 30\n",
                encoding="utf-8",
            )
            slow.chmod(0o755)

            real_environ = os.environ.get("CHROME")
            try:
                os.environ["CHROME"] = str(fake)
                check("CHROME is honored", find_chrome(), str(fake))
                os.environ["CHROME"] = str(area)
                check("a CHROME that is a directory is refused", find_chrome(), None)
                os.environ["CHROME"] = str(source)
                check("a CHROME that is not executable is refused", find_chrome(), None)
                os.environ["CHROME"] = str(area / "nowhere")
                check("a CHROME that does not exist is refused", find_chrome(), None)
            finally:
                if real_environ is None:
                    os.environ.pop("CHROME", None)
                else:
                    os.environ["CHROME"] = real_environ

            page = area / "card.html"
            page.write_text(page_html(["x"]), encoding="utf-8")
            target = area / "shot.png"
            previous = b"OLD PICTURE, STILL VALID"
            target.write_bytes(previous)

            real_timeout = globals()["SHOT_TIMEOUT_S"]
            try:
                shoot(str(fake), page, target, (100, 50))
                check("the picture is published", target.read_bytes(), valid_png)
                check("no pending file is left behind",
                      (area / "shot.pending.png").exists(), False)

                # A browser that writes nothing must leave the old picture alone
                # instead of destroying it - the whole point of the pending file.
                target.write_bytes(previous)
                globals()["SHOT_TIMEOUT_S"] = 2
                exploded = False
                try:
                    shoot(str(silent), page, target, (100, 50))
                except SystemExit:
                    exploded = True
                check("a browser that writes nothing fails loudly", exploded)
                check("and the previous picture survives", target.read_bytes(), previous)

                # A complete pending file left by an interrupted run must not be
                # mistaken for this run's output.
                (area / "shot.pending.png").write_bytes(valid_png)
                stale = False
                try:
                    shoot(str(silent), page, target, (100, 50))
                except SystemExit:
                    stale = True
                check("a stale pending file is not republished", stale)
                check("and it did not overwrite the picture", target.read_bytes(), previous)

                # A half-written picture must never be published, and the
                # browser must be terminated once the real one lands.
                globals()["SHOT_TIMEOUT_S"] = real_timeout
                target.write_bytes(previous)
                shoot(str(slow), page, target, (100, 50))
                check("a half-written picture is never published",
                      target.read_bytes(), valid_png)
                time.sleep(4)
                check("the browser is terminated once the picture lands",
                      (area / "still-running").exists(), False)
                # Not covered here: the `wait()` after `kill()`. It guards
                # against a zombie child and, on Windows, against the profile
                # directory still being held open - neither is observable from
                # inside this process.
            finally:
                globals()["SHOT_TIMEOUT_S"] = real_timeout
            check("the timeout constant is restored", SHOT_TIMEOUT_S, real_timeout)

    # Stripping SGR, and the window size derived from what is left.
    check("SGR leaves the plain text", plain("\x1b[38;5;196mred\x1b[0m"), "red")
    check("plain text is left alone", plain("abc"), "abc")
    # Against LITERALS on both sides, padding included: comparing the padding to
    # its own constant would let the constant change with the test still green.
    # 10 columns at 34px and ratio 0.6 is 204px, plus 44px of padding each side.
    check("width follows the longest line", card_size(["x" * 10])[0], 292)
    check("color does not widen the card",
          card_size(["\x1b[38;5;196m" + "x" * 10 + "\x1b[0m"])[0], 292)
    # 34px at 1.9 is 64.6 for one line and 129.2 for two, plus 26px top and bottom.
    check("one line is one line tall", card_size(["a"])[1], 117)
    check("two lines are two lines tall", card_size(["a", "b"])[1], 181)
    check("the longest of several lines wins", card_size(["x", "x" * 10])[0], 292)
    # No lines at all must give the padding, not an exception out of `max`.
    check("an empty card is just its padding", card_size([]), (2 * PAD_X_PX, 2 * PAD_Y_PX))
    # The exact boundary between the base-16 table and the color cube.
    check("index 15 is the last of the base table", xterm_color(15), BASE_16[15])
    check("index 16 is the first of the cube", xterm_color(16), "#000000")
    check("index 17 is already in the cube", xterm_color(17), "#00005f")

    # The generator has to agree with the module it documents, which is the one
    # thing a picture cannot show once it is a PNG.
    module = load_statusline()
    with tempfile.TemporaryDirectory(prefix="statusline-docs-test-") as tmp:
        transcript = write_transcript(Path(tmp))
        cards = render_cards(module, transcript)
        check("three cards are produced", sorted(cards),
              ["statusline", "statusline-line1", "statusline-line2"])
        check("the full card has both lines", len(cards["statusline"]), 2)
        line1, line2 = cards["statusline"]
        check("the model is on line 1", "Opus 5" in line1)
        check("the window suffix is not", "1M context" in line1, False)
        check("the session name is on line 1", "Tuning the status line" in line1)
        check("the session tokens are on line 1", "2.9M" in line1)
        check("the context is on line 1", "11.0%" in line1)
        check("the 5-hour quota is on line 2", "4.0%" in line2)
        check("the pinned caps are on line 2", "48.5%" in line2 and "21.0%" in line2)
        check("the weekly quota is on line 2", "32.0%" in line2)
        # The timers the README's table names, proving the slack in the offsets.
        check("the 5-hour timer reads 4h 47m", "4h 47m" in line2)
        check("the weekly timer reads 4d 14h", "4d 14h" in line2)
        # The rows carry `pre`, so the template's own newlines cannot become
        # blank lines that push the real ones out of the picture.
        check("the card itself is not pre", "#card {\n" in page_html(cards["statusline"])
              and "white-space: pre;\n    color" in page_html(cards["statusline"]), False)
        check("the rows are pre", "#card > div { white-space: pre; }" in page_html(cards["statusline"]))
        rule = module.paint(module.QUOTA_DASHES, module.C_SEP_BLOCK)
        check("line 2 opens with the module's own rule", line2.startswith(rule + " "))
        check("line 2 closes with it", line2.endswith(" " + rule))
        # The patching must not leak: a later run would render fabricated caps
        # into a bar that is supposed to read the account.
        check("fable_cap_percent is restored",
              module.fable_cap_percent.__name__, "fable_cap_percent")
        check("daily_total_percent is restored",
              module.daily_total_percent.__name__, "daily_total_percent")
        # The rule is compared to the LITERAL dash, not to the module's constant:
        # against the constant, a module that went back to seven dashes would
        # still pass here.
        check("the rule in the picture is one dash",
              module.paint("-", module.C_SEP_BLOCK), rule)
        page = page_html(cards["statusline"])
        check("the page carries both rows", page.count("<div>"), 2)

        # The two guards around the slicing, against a bar the real payload
        # cannot produce: a blank line in the MIDDLE, and a bar too short to cut
        # into two cards. Without a render that differs, both guards are
        # satisfied by accident and could be deleted unnoticed.
        real_render = module.render
        try:
            module.render = lambda data: "top\n\nbottom\n \n"
            odd = render_cards(module, transcript)
            check("a blank line never reaches a card", odd["statusline"], ["top", "bottom"])
            module.render = lambda data: "one lonely line"
            too_short = False
            try:
                render_cards(module, transcript)
            except SystemExit:
                too_short = True
            check("a bar with fewer than two lines is refused", too_short)
        finally:
            module.render = real_render
        check("render is restored", module.render.__name__, "render")
        check("the page declares utf-8", 'charset="utf-8"' in page)
        check("no raw ESC reaches the HTML", "\x1b" in page, False)

    for line in failures:
        print(f"FAIL {line}")
    print(f"{'FAILED' if failures else 'OK'} - selftest ({ran} checks, {len(failures)} failure(s))")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
