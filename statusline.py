#!/usr/bin/env python3
"""A two-line status line for Claude Code (a replacement for ccstatusline).

Reads the status line JSON from stdin and prints TWO ANSI lines. Layout:

    <model> <effort> | <session(italic)> · <ctx%> <session-tokens>
    <5h%> · <5h-reset> | <fable-day%> · <day-total%> | <fable%> <week%> <weekly-reset>

Line 1 = what belongs to this session (model, effort, name, context, tokens);
line 2 = the account quotas (the 5-hour window and the weekly one).

No labels. Every percentage that has a DEADLINE (5h, day, week) is painted by
PACE, not by value: the color comes from the consumption PROJECTED to the reset
(`pace_percent`), where 100 = lands exactly on the cap. 80% of the week on day 7
projects to ~86 and comes out yellow; the same 80% on day 2 projects past 250 and
comes out red. SCALE_PACE's palette: gray -> blue -> green -> yellow -> muted
orange, vivid red only when the pace projects blowing through the cap with room
to spare. Context has no deadline and stays on the value thermometer (SCALE_CTX).
Fable has a scale of its own (SCALE_FABLE): always orange, red when it projects
overrunning its own cap. Only the Fable model is bold.

Fable's cap is Anthropic's official limit - half the weekly quota on Max/Team
Premium; past that Fable only keeps going on usage credits. What the script
estimates is how much of that cap is already gone (see `fable_cap_percent`).

That estimate is CALIBRATED against the official number, and the calibration
holds for the weighting Anthropic applied on the date it was measured -
2026-07-30, the `FABLE_SHARE_CALIBRATION`. It is not a constant of nature:
Anthropic can change how much each model weighs against the quota whenever it
wants, and the day it does the factor becomes wrong with nothing to flag it.
Re-measure with `--calibrate <all%> <fable%>`, reading both numbers under
Settings > Usage on claude.ai (the same place the first pair came from). The
history of the measurements lives in the constant's comment.

The middle block of line 2 carries the two DAILY caps, measured over the current
calendar day (00:00 to 23:59 local): Fable's on the left, the TOTAL quota's (all
models) on the right. Both use the same MOVING cap: the week's balance left at
the start of the day divided by the days remaining until the reset - blowing one
day narrows the following ones. It goes past 100% if the day eats more than its
share - the number is not clamped, and that is the information. Both are painted
by pace against the end of the day - which on the weekly reset day is clipped by
the week, not by midnight (see `day_elapsed_fraction`).

With no usable deadline in the payload (`resets_at` missing or implausible) the
field falls back to the value scale, and the three derived numbers that depend on
"how many days are left" (weekly Fable, Fable's day, day total) DISAPPEAR instead
of coming out guessed.

Usage:  python statusline.py            (reads the payload from stdin)
        python statusline.py --selftest  (internal checks)
        python statusline.py --calibrate 92 84   (re-measures Fable's factor
                                                  against the usage screen:
                                                  <all%> <fable%>)

Any failure prints whatever was already assembled - the status line never takes
the session down.

Known limits, deliberately left in:
  - Fable's calibration rests on ONE measurement (2026-07-30) and does not
    separate the two possible causes of the bias - a quota weight lower than the
    price ratio, or usage with no local transcript (claude.ai web, Cowork). While
    the usage mix looks like it did that day, the factor corrects for both; change
    the mix a lot and it starts correcting too little or too much. It ages
    silently: nothing here detects that Anthropic re-weighted the quota - only
    re-measuring does.
  - Fable's DAILY cap is far more sensitive to the calibration than the weekly
    one, and that is not an effect of the calibration: it is the shape of the
    rationing. The daily cap divides the BALANCE (cap - spent before today), and
    near the cap that balance is the difference between two nearly equal numbers.
    Measured with the week at 92% and TWO days until the reset (the day count
    feeds the cap, so it is part of the measurement): the factor moves the weekly
    from 93.4% to 83.5% (proportional), but the daily from 89.2% to 44.6%.
    So the daily
    field inherits the estimate's uncertainty AMPLIFIED - read it as an order of
    magnitude, not as a measurement. Leaving the daily RAW next to a calibrated
    weekly would be worse: it would put two different rulers on the same bar.
  - The projection is LINEAR and human usage comes in bursts, so it is
    pessimistic in the morning and optimistic in the small hours. The floors
    (PACE_FLOOR_*) only cut the numeric blow-up at the start of the window;
    inside them the color does not move with the clock. Calibrating a curve by
    active hours would need usage history - a deliberate tradeoff: it stays this
    way until it starts to bother.
  - PACE_HARD is a step, not a gradient: 94.9% projecting 95.9 comes out yellow
    and 95.0% goes red on the spot. That is deliberate - near the block the
    absolute value takes the color back.
  - The clock is sampled several times per render (`time.time()`, `day_start()`).
    A render that crosses midnight can mix one day's share with the other's cost:
    1 crooked bar per day, until the next refresh. Fixing it would cost threading
    a single `now` through the whole module - not worth it.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

# -------------------------------------------------------------------- tuning

# Palette (xterm-256). Changing it here changes the whole bar.
C_MODEL = "38;5;247"  # fallback: a model outside the table below
# Model, matched by the id's prefix (the first match wins). Deliberately distinct
# tones from the effort ones - the two fields sit right next to each other on the
# bar. Only Fable is luminous: it is the priciest tier, so it is the only one
# that pops.
MODEL_COLORS = (
    ("claude-fable", "1;38;5;208"),  # orange + bold - the priciest tier
    ("claude-opus", "38;5;109"),    # desaturated cyan
    ("claude-sonnet", "38;5;186"),   # desaturated yellow
    ("claude-haiku", "38;5;103"),    # desaturated blue
)
C_EFFORT = "38;5;242"
C_SEP_BLOCK = "38;5;237"  # the "|" that separate the three blocks
# Effort: a muted gradient climbing up to `high`; only xhigh/max/ultracode light up.
EFFORT_COLORS = {
    "low": "38;5;37",         # cyan
    "medium": "38;5;69",      # blue
    "high": "38;5;178",       # yellow
    "xhigh": "38;5;214",    # orange
    "max": "38;5;196",      # red
    "ultracode": "38;5;201",  # magenta
}
C_SEP_ITEM = "38;5;247"   # the "·", in the session name's color
QUOTA_DASHES = "-------"  # the rule that opens and closes line 2 (in the "|" color)
C_NAME = "3;38;5;247"     # 3 = italic
C_SESSION_TOKENS = "38;5;240"

C_RESET_5H = "38;5;241"
C_RESET_WEEK = "38;5;239"

# Thermometer by VALUE: (exclusive upper bound, SGR). Used only as a fallback,
# when the payload carries no `resets_at` for the window and there is therefore
# no deadline to measure pace against.
SCALE = (
    (20.0, "38;5;240"),  # dark gray
    (40.0, "38;5;67"),   # muted blue
    (60.0, "38;5;71"),   # muted green
    (75.0, "38;5;143"),  # muted yellow
    (88.0, "38;5;173"),  # muted orange
)
SCALE_ALERT = "38;5;196"  # >= 88%: vivid red

# Thermometer by PACE - the default scale for everything that has a deadline. The
# number fed in here is NOT the consumption, it is the consumption PROJECTED to
# the reset (`pace_percent`): 100 = on pace to land exactly on the cap. Same
# palette as SCALE, thresholds recentered on 100. Red now means "will blow
# through the cap", not "spent a lot" - which is why 80% of the week on the eve
# of the reset (proj ~86) comes out yellow and the same 80% on day 2 (proj >250)
# comes out red.
SCALE_PACE = (
    (45.0, "38;5;240"),   # gray   - less than half the pace
    (65.0, "38;5;67"),    # blue   - plenty of slack
    (82.0, "38;5;71"),    # green  - below pace
    (108.0, "38;5;143"),  # yellow - on pace (lands right on the cap)
    (135.0, "38;5;173"),  # orange - running hot
)
SCALE_PACE_ALERT = "38;5;196"  # projects blowing through the cap with room to spare

# Floor for the pace denominator. Extrapolating linearly from a tiny sliver of
# the deadline is noise (2% spent over 1% of the time would project 200%), so
# before that point the window is treated as if the floor had already elapsed.
PACE_FLOOR_WINDOW = 0.15  # 45min of the 5h; ~25h of the week
# The calendar day starts at midnight, human consumption does not: a night spent
# asleep is not "zero pace", it is absence of data. Higher floor for the day (~08h24).
PACE_FLOOR_DAY = 0.35

# Real quota nearly exhausted: red regardless of pace. Being 5% away from the end
# of the window is actionable even on the eve of the reset - the block gets there
# first. Does not apply to the DAILY caps, which are self-imposed rationing and
# block nothing.
PACE_HARD = 95.0

# Slack for accepting a `resets_at` that just went by: at the instant of the
# reset the payload still carries the old value for a few seconds.
RESET_TOLERANCE = 300.0

# Fable's cap: the expensive tier never goes cold - the bottom of the scale is
# already muted orange, as a permanent warning. Above pace the orange lights up,
# and red only when the projection reaches the whole cap (the point where Fable
# leaves what is included and starts eating usage credits). Three bands on
# purpose: a single orange from 0 to 99.9 is not a gradient, and the warning
# would arrive together with the overrun.
SCALE_FABLE = (
    (85.0, "38;5;173"),  # muted orange - the expensive tier's permanent warning
    (100.0, "38;5;214"),  # vivid orange - projects touching the cap
)
SCALE_FABLE_ALERT = "38;5;196"
# Fable's fallback (payload with no usable deadline): back to value, with the 80%
# cut the bar used before pacing existed.
SCALE_FABLE_VALUE = ((80.0, "38;5;173"),)

FIVE_HOURS = 5 * 3600
SEVEN_DAYS = 7 * 86400
# Anthropic's OFFICIAL limit (Max/Team Premium): "you can use up to half of your
# weekly usage limit on Fable 5. After that, you can continue using Fable 5 with
# usage credits, or switch to another model" - announcement of 2026-07-12. Not a
# rule of thumb: 100% in this field = the point where Fable leaves what is
# included and starts consuming credits.
FABLE_CAP_SHARE = 0.50
FABLE_PREFIXES = ("claude-fable",)

# Empirical correction of Fable's estimated share, measured against the OFFICIAL
# number. The share comes from a scan of local transcripts weighted by API PRICE,
# and price is not the quota weight: measuring both sides at the same instant
# (2026-07-30), the bar read 94.4% of the cap against the official 84%.
#
# The official number lives on claude.ai, in `GET /api/organizations/<org>/usage`,
# inside the `limits` array: the entry with `kind: "weekly_scoped"` whose
# `scope.model.display_name` is "Fable" is Fable's cap, and `weekly_all` is the
# total quota. The payload Claude Code sends here carries only `five_hour` and
# `seven_day` - the per-model field exists server-side and does NOT reach us,
# which is why this bar estimates instead of reading.
#
# TWO causes may be behind it, and a single measurement cannot separate them:
# (a) Fable's weight in the quota being lower than the PRICE ratio (today 2x
# Opus); (b) usage that counts against the quota but leaves no local transcript
# (claude.ai web, Cowork).
#
# Cause (b) has NO guaranteed direction, and saying it "inflates the share" would be
# too strong: absent usage only
# inflates if it is LESS Fable-heavy than the local one; being more Fable-heavy,
# the local share understates; and with the same mix, it biases nothing. Both are
# absorbable by the same empirical factor while the proportions stay stable, but
# they do not necessarily point the same way.
#
# That is why the factor applies to the SHARE and not to the price: it does not
# claim which cause it is.
#
# Re-measure with `--calibrate <all%> <fable%>`, reading both numbers off the
# usage screen, and paste the result here. 1.0 = no correction (the old behavior).
#
# MEASUREMENTS so far:
#
#   2026-07-30 morning   raw share 51.33%   official 84% of 92%   ->  0.889
#   2026-07-30 afternoon raw share 50.88%   official 86% of 94%   ->  0.899
#   2026-07-31           raw share 38.35%   official  5% of  7%   ->  0.931  (noisy)
#   2026-08-05           raw share 47.36%   official 60% of 68%   ->  0.931
#   2026-08-16           raw share 24.66%   official 15% of 22%   ->  1.382  <- IN USE
#
# Until 08-05 the value was 0.894, the average of the two 07-30 points, and this
# note claimed there was "no drift to chase". There was. Propagating only the
# ROUNDING of the official numbers - the screen serves integers, so "60%" is
# anything between 59.5 and 60.5 - the two ranges do NOT touch:
#
#   07-30 morning   0.889  (0.879 - 0.900)
#   08-05           0.931  (0.917 - 0.946)
#
# Two readings are possible and one measurement does not separate them: Anthropic
# re-weighted the quota, or the usage MIX moved enough to shift the bias. Under
# either, what counts is the CURRENT regime - which is why the value is now the
# recent measurement and not the average with 07-30. Averaging across two regimes
# produces a number that describes neither; while the points sat 0.01 apart the
# average was right, and it stopped being right when they separated.
#
# The 07-31 point gained meaning in hindsight: it already pointed at 0.931 and was
# dropped as noise (with official numbers of 5% and 7%, the same ±0.5 from the
# screen becomes ±0.11 in the factor). The 08-05 measurement lands on the SAME
# value with large official numbers, which corroborates it - two independent
# points at 0.931, one of them precise.
#
# The ranges above propagate only the SCREEN's rounding. The raw share comes from
# the local scan and carries its own, unmeasured uncertainty - so they are a floor
# on the uncertainty, not the whole of it.
#
# 2026-08-16: the factor CROSSED 1.0 and the sign of the bias flipped. Until now
# every measurement sat below 1 - the local share OVERestimated Fable and the
# factor cut it down. Now it UNDERestimates: with the bar at 10.1%, the official
# number read 15%. The rounding ranges still do not touch, by a wide margin:
#
#   08-05   0.931  (0.917 - 0.946)
#   08-16   1.382  (1.306 - 1.462)
#
# The likeliest cause is the (b) this note already anticipated, now pointing the
# other way: Fable usage that counts against the quota and leaves NO transcript on
# this machine - claude.ai on the web, Cowork, another device. The usage screen
# itself warns that its breakdown is "based on local sessions on this machine";
# the CAP at the top comes from the server and sees everything. Fable used
# elsewhere shows up only in the official number, and the local scan, blind to it,
# comes in low. One measurement cannot separate that from a quota re-weighting.
#
# Practical consequence: while the share of Fable running off this machine varies,
# so does the factor - re-measure more often than before, preferably with the week
# well advanced (a large official number means a narrow range). This point was
# measured with the week at 22%, so its range is wide.
#
# This constant is now a FALLBACK. With `USAGE_API_ENV` enabled, the factor is
# re-measured on every round against the official number and this value is only
# used when no measurement is available - see `factor_in_use`.
FABLE_SHARE_CALIBRATION = 1.382
# The weekly share moves slowly and the scan now covers the subagents too, so it
# reads a lot more disk. 10 min keeps the cost near 1% of one core; below that
# the scan gets heavy again without improving the reading.
WEEK_CACHE_TTL = 600

CHUNK = 256 * 1024  # reverse reading of the .jsonl files
# Ceiling for one transcript line. Above it the reverse reader's accumulator is
# discarded - see `read_lines_reverse`.
MAX_LINE = 16 * 1024 * 1024
# Time budget for the transcript scan. Once it is blown, this round's estimate is
# abandoned and the last cache stands, even if stale: Claude Code KILLS a status
# line that is still running when the next refresh arrives, so a scan that never
# finishes never gets to write a cache and restarts from zero forever.
SCAN_DEADLINE = 8.0
DEFAULT_CONTEXT_WINDOW = 200_000

# Token fields that occupy the context WINDOW - what the model wrote has already
# left it.
TOKENS_CTX = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
# What the SESSION spent: the window ones plus the output.
TOKENS_TOTAL = TOKENS_CTX + ("output_tokens",)

# Transient files (the weekly share cache, the diagnostic dump) live in the
# USER's own directory, not the system temp folder. On a shared machine /tmp
# belongs to everyone: a fixed name collides across users and opens the classic
# planted symlink vector (the process writes where someone else pointed).
# `~/.claude` is already a requirement of this script - the transcript scan lives
# there.
STATE_DIR = Path.home() / ".claude"
WEEK_CACHE_FILE = "statusline-week-share.json"
# OFFICIAL per-model usage, written by the sibling `claude_usage_fetch.py`. It
# fetches from `GET /api/oauth/usage` (the same endpoint Claude Code itself uses)
# what the status line payload does NOT carry: how much of the Fable cap is gone.
# When the file is present and fresh, the weekly Fable field STOPS being an
# estimate and becomes a reading.
OFFICIAL_CACHE_FILE = "statusline-usage-official.json"
FETCH_OFFICIAL = Path(__file__).resolve().parent / "claude_usage_fetch.py"
# Re-fetch every 10 min - the SAME TTL as the local scan, and not for the sake of
# symmetry: the calibration factor is a ratio between the official number and the
# local share, and different cadences would make every factor mix two instants,
# which is exactly the flaw in the manual calibration this replaces. On top of
# that the official number arrives rounded to an INTEGER, so chasing finer
# granularity than 10 min buys no precision at all.
OFFICIAL_TTL = 600
# Up to 1h old, the official number still beats the estimate - the weekly quota
# moves slowly (a 7-day window) and the number carries a timestamp. Past that the
# bar falls back to the estimate: an old number painted in the same color as a
# fresh one is precisely what this file refuses everywhere else.
OFFICIAL_MAX_AGE = 3600
# Floor between two spawns of the fetch, so a fast repeated failure (no network)
# does not turn into a burst of processes on every 15s refresh.
OFFICIAL_SPAWN_FLOOR = 120
OFFICIAL_STAMP_FILE = "statusline-usage-spawn.stamp"
# Fetching the official number is OPT-IN. It reads the user's credential from the
# Keychain and calls an endpoint with no public contract: on the author's machine
# that is his own account, but in a program other people install, reading the
# keychain of whoever downloaded it without them asking is not an acceptable
# default, however benign the use. Disabled, the bar estimates the Fable share
# exactly as it always did.
USAGE_API_ENV = "CLAUDE_STATUSLINE_USAGE_API"
# Dump of the last payload: handy for finding out which field Claude Code sends,
# but the payload carries the session name and paths. Writing that on every
# refresh without the user asking is a leak, not a convenience - it sits behind
# this environment variable.
DEBUG_ENV = "CLAUDE_STATUSLINE_DEBUG"
DEBUG_DUMP = "statusline-payload.json"

# Context's own thermometer: % of the WHOLE window (the payload's official
# used_percentage when present), not of an artificial 80% "usable window".
# Cold < 50%, amber 50-75%, red >= 75% + a /compact reminder.
SCALE_CTX = ((50.0, "38;5;240"), (75.0, "38;5;143"))
SCALE_CTX_ALERT = "38;5;196"
CTX_HINT = "/compact"

# USD per 1M tokens (input, output) - official API docs, checked 2026-07-22.
# Matched by the model id's prefix; the first match wins, so the order matters.
PRICES = (
    ("claude-fable-5", (10.0, 50.0)),
    ("claude-opus", (5.0, 25.0)),
    ("claude-sonnet", (3.0, 15.0)),
    ("claude-haiku", (1.0, 5.0)),
)
DEFAULT_PRICE = (5.0, 25.0)  # the Opus tier - the most common one
CACHE_WRITE_MULT = 1.25  # 5-minute cache (the default); a 1h TTL would be 2.0
CACHE_READ_MULT = 0.10


# ---------------------------------------------------------------- primitives


class Shares(NamedTuple):
    """The three shares that come out of one scan, all over the SAME
    denominator: the week's total cost. They are a `NamedTuple` because the three
    are similar-looking fractions and nothing in a raw `(0.5, 0.5, 0.25)` says
    which is which."""

    fable: float  # Fable's cost over the week
    fable_day: float  # Fable's cost TODAY
    day_total: float  # TODAY's cost, all models


class Window(NamedTuple):
    """The weekly quota window, the way the daily caps need it."""

    percent: float  # % of the weekly quota already used (the payload's official number)
    start: float  # epoch of the start of the 7 days
    days: int  # calendar days until the reset, counting today


def paint(text: str, sgr: str) -> str:
    return f"\x1b[{sgr}m{text}\x1b[0m" if text else ""


def write_private(path: Path, text: str) -> None:
    """Writes a file readable ONLY by its owner.

    On POSIX the file would be born with the user's umask - typically 0644, that
    is, readable by any other user on the machine. The diagnostic dump carries
    the session name and paths from the disk, so the mode has to go in at
    CREATION time: creating and then `chmod` leaves a window in which the file
    exists wide open. On Windows the mode is ignored, and the profile's ACL
    already covers it.

    It writes to a temporary file and RENAMES it over the target, rather than
    truncating the target. One move solves three problems:

    - `O_CREAT` only applies the mode at CREATION. A file that already exists as
      0644 - from an older version, or created by hand - stayed 0644 forever,
      while the mode on the `open` gave the impression of protecting it.
    - Truncating and writing leaves a window in which a concurrent reader sees
      half a JSON document. With `os.replace` the swap is atomic: the reader
      sees either the whole old version or the whole new one.
    - `O_NOFOLLOW` on the temporary refuses a planted symlink, and the `replace`
      substitutes the link at the destination instead of writing through it.

    The temporary's name carries the PID so two concurrent rounds do not fight
    over the same file. Leftovers from a dead process are removed before
    retrying: `O_EXCL` would otherwise refuse an old temporary from the same PID
    forever.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o600)
    except FileExistsError:
        os.unlink(tmp)
        fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def state_file(name: str) -> Path:
    """Path of one of the script's transient files, in the user's directory.

    Creates `~/.claude` if it is missing - the caller is already inside a `try`
    and treats the failure as "no cache", so a read-only home degrades instead of
    taking the bar down.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR / name


def as_dict(value) -> dict:
    """A nested payload field as a dict - `{}` if it arrives as another type.

    `(x or {}).get(...)` only guards against None: a `"model": "bad"` sails
    through and raises AttributeError mid-render, wiping out the ENTIRE bar
    (main's `except` returns an empty line). The payload comes from outside;
    treating a wrong type as a missing field is the only way to degrade piece by
    piece.
    """
    return value if isinstance(value, dict) else {}


def as_text(value) -> str:
    """A text field of the payload - `""` if it is not a string."""
    return value if isinstance(value, str) else ""


# C0, DEL and C1 (the basics), plus the ones that do not look like control
# characters and mess with the layout just the same. Three families beyond the
# obvious: U+2028/2029, the line and paragraph separators, which would break the
# two-line contract; U+202A-202E and U+2066-2069, the bidirectional overrides and
# isolates, which reorder what has already been written on the line - the
# "Trojan Source" trick; and the invisibles that survive every eyeball test
# (zero-width, word joiner, soft hyphen, LRM/RLM/ALM, ZWNBSP), which pad the
# text with characters that occupy no column and hide content in plain sight.
_CONTROL = re.compile(
    r"["
    r"\x00-\x1f\x7f-\x9f"           # C0, DEL and C1 (CSI/OSC/DCS in 8 bits)
    r"\xad"                             # soft hyphen: invisible, splits a word
    r"\u061c\u200b\u200e\u200f"           # ALM, zero-width, and the LRM/RLM marks
    r"\u2028\u2029"                   # line and paragraph separators
    r"\u202a-\u202e\u2066-\u2069"  # bidi overrides/isolates: "Trojan Source"
    r"\u2060-\u2064\ufeff"           # word joiner, invisible operators, ZWNBSP
    r"]"
)


def safe_text(value, limit: int = 120) -> str:
    """Text from outside, ready to go to the terminal.

    The session name and the transcript title are written by something else (the
    user, or the model itself summarizing the conversation). Loose in a terminal,
    an ESC in there is not a character, it is a command: it moves the cursor,
    retitles the window, opens an OSC-8 hyperlink. A lone `\\n` would already
    break the two-line layout, which is this bar's contract. Only the printable
    gets through, and short.
    """
    return _CONTROL.sub("", as_text(value))[:limit].strip()


def finite_number(value) -> float | None:
    """float from a numeric payload field; None if it is not usable.

    Two cases sail through `isinstance(x, (int, float))`: `bool` (True would turn
    into 1.0) and NaN/Infinity, which `json.loads` accepts by default. NaN is the
    worst - it survives the clamp and comes out the wrong side: `min(100.0, nan)`
    returns 100.0, meaning a corrupted payload would turn into a full-quota alert.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # A JSON integer has no ceiling, and `float()` on a 400-digit one raises
    # OverflowError rather than returning `inf`. This is the sanitizer: what
    # comes in here is exactly what is not to be trusted, so it swallows and
    # reports "unusable" instead of exploding inside whoever called it.
    try:
        value = float(value)
    except (OverflowError, ValueError):
        return None
    return value if math.isfinite(value) else None


def read_lines_reverse(path: Path):
    """Yields the lines of `path` from the end to the start, without loading it all.

    The `tail` holds the piece of a line cut at the boundary between two blocks,
    and concatenating it on every pass copies it whole: on a single giant line
    that degenerates into quadratic time and memory the size of the line. Past
    MAX_LINE the line is ABANDONED - the largest one measured in real use was
    1.6 MB, and anything over 16 is an attachment pasted into the content, which
    does not belong in any token count.

    Abandoning means staying in a discard state until we find the break that
    OPENS that line, not just zeroing the accumulator: zeroing would let the rest
    of it be emitted as if it were a whole line, and a fragment cut in the right
    place can even form valid JSON and enter the sum. The neighboring lines keep
    coming out.
    """
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        pos = fh.tell()
        tail = b""
        discarding = False
        while pos > 0:
            size = min(CHUNK, pos)
            pos -= size
            fh.seek(pos)
            parts = (fh.read(size) + tail).split(b"\n")
            tail = parts.pop(0)
            if discarding and parts:
                # We found the break that opens the giant line: whatever comes
                # before it is already another line, so back to normal.
                parts.pop()
                discarding = False
            if len(tail) > MAX_LINE:
                tail = b""
                discarding = True
            for line in reversed(parts):
                if line.strip():
                    yield line
        if tail.strip() and not discarding:
            yield tail


def parse_ts(value) -> float | None:
    """An ISO-8601 timestamp from the transcript, as epoch.

    Claude Code writes them with a `Z` suffix (UTC), and `datetime.fromisoformat`
    only started accepting that suffix in Python 3.11 - on 3.8-3.10 it raises
    ValueError and every timestamp would silently turn into None. The damage
    would not show up as an error: `cost_shares` stops recognizing what belongs
    to today and both daily caps sit at 0.0%, with the whole bar looking like it
    works. Since the README promises 3.8+, normalizing the suffix here is what
    keeps the promise.

    The `strptime` covers the rear: the older versions are also pickier about the
    number of decimal places, and a format their `fromisoformat` rejects still
    matches here.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # `.timestamp()` is what raises on a date outside the platform's range, and
    # it does not raise ValueError: a year like 1500 comes out as OSError
    # (errno 22) on Windows, OverflowError elsewhere. Letting either escape is
    # expensive out of proportion - it aborts the whole scan through the
    # `except OSError` upstairs, and three fields leave the bar in silence
    # because one line of one transcript, in any project, carries a bad date.
    try:
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, OSError, OverflowError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except (ValueError, OSError, OverflowError):
            continue
    return None


def format_tokens(count: int) -> str:
    """The same scale ccstatusline used, so the bar does not read differently."""
    if count >= 1_000_000 - 50:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def format_duration(seconds: float) -> str:
    """'1d 22h' / '2h 17m' / '43m' - the smaller unit enters only once the larger leaves.

    With days on screen the minute is noise; without days it matters again.
    """
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def scale_sgr(percent: float, scale=SCALE, alert: str = SCALE_ALERT) -> str:
    for limit, sgr in scale:
        if percent < limit:
            return sgr
    return alert


def pace_percent(percent: float, fraction: float, floor: float) -> float:
    """Consumption PROJECTED to the end of the deadline, at the current pace.

        proj = consumption / share_of_the_deadline_already_elapsed

    100 = lands exactly on the cap. It is the honest linear extrapolation: 40%
    spent over 20% of the deadline ends at 200%. Since `fraction` never goes past
    1, proj never falls BELOW the consumption - at the last instant of the window
    the two coincide and the color goes back to reading the raw value, which is
    what is left that is actionable.

    The `floor` protects the start of the window, where the denominator is too
    small to support extrapolation.
    """
    return percent / max(fraction, floor)


def pace_sgr(
    percent: float,
    fraction: float | None,
    floor: float = PACE_FLOOR_WINDOW,
    scale=SCALE_PACE,
    alert: str = SCALE_PACE_ALERT,
    hard: float | None = PACE_HARD,
    fallback=SCALE,
    fallback_alert: str = SCALE_ALERT,
) -> str:
    """Color of a percentage WITH a deadline.

    With no `fraction` (payload without `resets_at`) there is no deadline to
    measure against and the color falls back to the value scale - the behavior
    the bar had before pacing. `hard` is the short circuit for a nearly exhausted
    quota; `hard=None` turns it off (the daily caps, which block nothing).
    """
    if hard is not None and percent >= hard:
        return alert
    if fraction is None:
        return scale_sgr(percent, fallback, fallback_alert)
    return scale_sgr(pace_percent(percent, fraction, floor), scale, alert)


def fable_sgr(percent: float, fraction: float | None,
              floor: float = PACE_FLOOR_WINDOW) -> str:
    """Color of the expensive model's two fields: always orange, red when the
    pace projects occupying the whole cap. With no deadline, back to the cut by
    value.

    It exists so the configuration lives in one place only: written out in full
    at every call site, the selftest ended up REPEATING the config instead of
    exercising it - flipping `hard=None` in the render would leave the tests
    green.
    """
    return pace_sgr(
        percent,
        fraction,
        floor=floor,
        scale=SCALE_FABLE,
        alert=SCALE_FABLE_ALERT,
        hard=None,
        fallback=SCALE_FABLE_VALUE,
        fallback_alert=SCALE_FABLE_ALERT,
    )


def by_prefix(ident, table, default):
    """The value of the first row of `table` whose prefix matches `ident`.

    Model color and model price were the same algorithm written twice. The
    `default` covers both "no match" and "the field is not even text" - the model
    id comes from the payload and from the transcript.
    """
    if isinstance(ident, str):
        target = ident.lower()
        for prefix, value in table:
            if target.startswith(prefix):
                return value
    return default


def model_sgr(model_id: str | None) -> str:
    """Model color from the id's prefix; falls back to neutral gray."""
    return by_prefix(model_id, MODEL_COLORS, C_MODEL)


def price_for(model: str | None) -> tuple[float, float]:
    """Price (input, output) of the model, from the id's prefix."""
    return by_prefix(model, PRICES, DEFAULT_PRICE)


# -------------------------------------------------------------------- pieces


def transcript_file(data: dict) -> Path | None:
    """Path of this session's transcript, only when it exists on disk.

    The field comes from the payload: `as_text` first, otherwise a numeric
    `transcript_path` would make `Path(7)` raise TypeError inside three different
    functions.
    """
    path = as_text(data.get("transcript_path"))
    return Path(path) if path and Path(path).is_file() else None


def session_name(data: dict) -> str:
    """The name given by /rename. The payload already carries `session_name`; the
    transcript (a `custom-title` entry) is only the fallback."""
    direct = safe_text(data.get("session_name"))
    if direct:
        return direct

    transcript = transcript_file(data)
    if transcript is None:
        return ""
    for raw in read_lines_reverse(transcript):
        if b'"custom-title"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("type") == "custom-title" and entry.get("customTitle"):
            return safe_text(entry["customTitle"])
    return ""


def usage_entries(path: Path, start: float | None = None):
    """Entries carrying `message.usage`, already deduplicated.

    When a `stop_reason` field exists, only the entries with it filled in count
    (plus the last one, if it comes as null) - otherwise the partial streaming
    events get counted twice. `start` limits the reading to a time window.
    """
    entries = []
    has_stop_reason = False
    for raw in read_lines_reverse(path):
        # Substring pre-filter: `json.loads` is the scan's bottleneck (measured:
        # ~2s of reading against ~13s when parsing everything), and more than
        # half the transcript lines carry no `usage` at all. An entry with usage
        # always carries the literal key, so this produces no false negative; a
        # line that merely MENTIONS the word in its text is dropped just below,
        # at the type check, as it always was.
        if b'"usage"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if start is not None:
            ts = parse_ts(entry.get("timestamp"))
            if ts is not None and ts < start:
                break
        message = entry.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            continue
        if "stop_reason" in message:
            has_stop_reason = True
        entries.append(entry)

    entries.reverse()  # back to chronological order
    if has_stop_reason:
        last = len(entries) - 1
        entries = [
            e
            for i, e in enumerate(entries)
            if e["message"].get("stop_reason")
            or (e["message"].get("stop_reason") is None and i == last)
        ]
    return entries


def token_count(usage: dict, key: str) -> float:
    """One token counter from the transcript, sanitized.

    The transcript is ANOTHER program's file, not a contract: a counter holding a
    string, a negative or a NaN comes in just the same. Missing or crooked turns
    into 0 - without that a single NaN poisons the whole sum, and silently,
    because NaN contaminates every later operation without raising anything.
    """
    value = finite_number(usage.get(key))
    return max(0.0, value) if value is not None else 0.0


def sum_tokens(usage: dict, fields=TOKENS_CTX) -> float:
    """Sums the requested counters of a `usage` block."""
    return sum(token_count(usage, field) for field in fields)


def entry_cost(entry: dict) -> float:
    """USD of one entry, each token type weighted by that entry's model price."""
    usage = entry["message"]["usage"]
    price_in, price_out = price_for(entry["message"].get("model"))
    return (
        token_count(usage, "input_tokens") * price_in
        + token_count(usage, "output_tokens") * price_out
        + token_count(usage, "cache_creation_input_tokens") * price_in * CACHE_WRITE_MULT
        + token_count(usage, "cache_read_input_tokens") * price_in * CACHE_READ_MULT
    ) / 1_000_000


def session_tokens(data: dict) -> str:
    """Tokens accumulated in this session: the ones occupying the window plus
    what the model wrote."""
    transcript = transcript_file(data)
    if transcript is None:
        return ""
    total = sum(sum_tokens(e["message"]["usage"], TOKENS_TOTAL)
                for e in usage_entries(transcript))
    return format_tokens(int(total))


def context_window_size(data: dict) -> int:
    """The model's window: from the payload; otherwise inferred from the id ('[1m]', '200k')."""
    window = finite_number(as_dict(data.get("context_window")).get("context_window_size"))
    if window is not None and window > 0:
        return int(window)

    model = as_dict(data.get("model"))
    ident = f"{model.get('id', '')} {model.get('display_name', '')}".lower()
    match = re.search(r"(\d+(?:\.\d+)?)\s*([mk])\b", ident)
    if match:
        value = float(match.group(1))
        return int(value * (1_000_000 if match.group(2) == "m" else 1000))
    return DEFAULT_CONTEXT_WINDOW


def context_used_tokens(data: dict) -> int | None:
    """Tokens occupying the window right now."""
    cw = as_dict(data.get("context_window"))
    usage = cw.get("current_usage")
    if isinstance(usage, dict):
        return int(sum_tokens(usage))
    direct = finite_number(usage if usage is not None else cw.get("total_input_tokens"))
    if direct is not None:
        return int(direct)

    # Fallback (payload without context_window): last entry of the main chain.
    transcript = transcript_file(data)
    if transcript is None:
        return None
    for raw in read_lines_reverse(transcript):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("isSidechain") is True or entry.get("isApiErrorMessage"):
            continue
        usage = as_dict(entry.get("message")).get("usage")
        if isinstance(usage, dict):
            return int(sum_tokens(usage))
    return None


def context_percent(data: dict) -> float | None:
    """% of the WHOLE window: the official used_percentage; fallback tokens/window."""
    cw = as_dict(data.get("context_window"))
    pct = finite_number(cw.get("used_percentage"))
    if pct is not None:
        return min(100.0, max(0.0, pct))
    used = context_used_tokens(data)
    if used is None:
        return None
    window = context_window_size(data)
    if window <= 0:
        return None
    return min(100.0, max(0.0, used / window * 100))


def scan_projects(start: float, accumulate, root: Path | None = None) -> bool:
    """Walks the transcripts touched since `start`; returns whether it COMPLETED.

    `root` exists so the test can point at a toy tree - without it this function
    only ever runs against the real `~/.claude` and stays uncovered.

    RECURSIVE on purpose. Subagent and workflow transcripts do not sit next to
    the session's own: they go into subfolders (`<session>/subagents/*.jsonl`),
    and whoever orchestrates with subagents has most of their consumption
    precisely there - in one real measurement, 63% of the volume. Scanning only
    the top level inflated the expensive model's share, because the denominator
    lost the CHEAP consumption that had been delegated: the same state measured a
    shallow share of 60% against a real 51.6%.

    `os.walk` already swallows directory errors (permissions, broken links) on
    its own; the `try` covers the file that disappears or closes between the
    listing and the opening.
    """
    projects = root if root is not None else Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return False
    # `monotonic`: the wall clock can be adjusted mid-run (NTP, DST) and turn the
    # deadline into the past or into eternity.
    deadline = time.monotonic() + SCAN_DEADLINE
    complete = True

    def note_error(_error):
        # An unreadable directory = a PARTIAL scan. Without this `os.walk`
        # swallows the error and the round declares itself complete having seen
        # less than what is there.
        nonlocal complete
        complete = False

    for dirpath, _dirs, files in os.walk(projects, onerror=note_error):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            if time.monotonic() > deadline:
                # Stopping midway leaves a biased share (what was left out is not
                # a random sample). The caller discards the round.
                return False
            path = os.path.join(dirpath, name)
            try:
                if os.stat(path).st_mtime < start:  # cheap filter before opening
                    continue
                accumulate(usage_entries(Path(path), start))
            except OSError:
                complete = False  # a file inside the window that could not be read
                continue
    # A single slow file can blow the deadline AFTER the last check: without this
    # final test, the slowest round of all would be the one declaring itself
    # complete.
    return complete and time.monotonic() <= deadline


def day_start(now: datetime | None = None) -> float:
    """Local midnight of the current day - the window of Fable's daily cap.

    `now` exists so tests can pin the date. The edges that matter - the 23-hour
    day and the 25-hour day of daylight saving - happen on TWO days of the year;
    reading the real clock, a test would go a whole year without touching them
    and give a sense of coverage it does not have.
    """
    ref = now if now is not None else datetime.now()
    return ref.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def day_end(now: datetime | None = None) -> float:
    """The next local midnight.

    NOT `day_start() + 86400`: on the day daylight saving starts or ends the
    calendar day has 23 or 25 hours, and adding seconds would put the "end of the
    day" at 1 a.m. or at 11 p.m. - skewing the elapsed share on precisely the day
    the clock is already strange. Adding ONE DAY to a local date and letting
    `timestamp()` resolve the offset is the only correct way.
    """
    ref = now if now is not None else datetime.now()
    start = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()


def window_span(window: str) -> float:
    return FIVE_HOURS if window == "five_hour" else SEVEN_DAYS


def elapsed_share(now: float, start: float, end: float) -> float:
    """How much of [start, end] has gone by at `now`, clamped to 0..1.

    Kept apart from whoever discovers the bounds so it can be tested with fixed
    numbers - the functions that call it read the clock and give no deterministic
    test.
    """
    span = end - start
    if span <= 0:
        return 1.0
    return min(1.0, max(0.0, (now - start) / span))


def valid_reset(data: dict, window: str) -> float | None:
    """The payload's `resets_at`, only when it is a PLAUSIBLE deadline.

    The window has a fixed size, so a legitimate reset falls between now and
    now+span (with slack for the seconds in which the payload has not rolled over
    yet after the reset). Missing, zero, NaN, in the past or 99 days ahead is not
    a bad deadline - it is invalid data, and building a window on top of it
    manufactures a fake number that looks like a measurement: a reset in the
    distant future pushes the start of the week forward, the transcript scan
    finds nothing and Fable's share comes out at 0.0% with the week full. An
    invalid deadline = no deadline.
    """
    limits = as_dict(as_dict(data.get("rate_limits")).get(window))
    value = finite_number(limits.get("resets_at"))
    if value is None:
        return None
    now = time.time()
    if not (now - RESET_TOLERANCE < value <= now + window_span(window) + RESET_TOLERANCE):
        return None
    return value


def elapsed_fraction(data: dict, window: str) -> float | None:
    """Share of the quota window already elapsed (0..1), from the payload's reset.

    The start comes from the reset minus the window's size. With no usable
    deadline it returns None and the caller paints by value. Clamped on both
    ends: a clock out of sync must not turn into a negative fraction nor one > 1.
    """
    resets_at = valid_reset(data, window)
    if resets_at is None:
        return None
    return elapsed_share(time.time(), resets_at - window_span(window), resets_at)


def day_elapsed_fraction(week_start: float | None = None,
                         week_reset: float | None = None) -> float:
    """Share of the quota-day already elapsed - the deadline of both daily caps.

    Almost always it is the calendar day, but on the WEEKLY RESET DAY it is
    clipped by the week, on both ends: before the reset, the remaining balance is
    only spendable until then (the day ends at 12:00, not at 24:00); afterwards,
    the new week's spending starts at the reset, not at midnight. Without the
    clipping, 80% of the day's cap at 11:00 with a reset at 12:00 would project
    175% (red) instead of ~87%.

    The clipping matches the MEASUREMENT: `cost_shares` only sees entries from
    the start of the week on, so "today's spending" already means "since the
    start of the week or since midnight, whichever is more recent". Outside the
    reset day neither bound bites and the whole calendar day is left.
    """
    start = day_start()
    end = day_end()
    if week_start is not None:
        start = max(start, week_start)
    if week_reset is not None:
        end = min(end, week_reset)
    return elapsed_share(time.time(), start, end)


def read_cache(cache_path: Path | None, start: float, today: float, accept_stale: bool):
    """The shares stored by the last scan, or None.

    `accept_stale` ignores the TTL: it serves the case where this round's scan
    cannot be completed and the old number beats no number at all. The values are
    validated as if they came from outside - the file may have been written by an
    older version, hand-edited, or truncated by a concurrent session, and a `NaN`
    share or one outside 0..1 would contaminate the whole bar.
    """
    if cache_path is None:
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        # The METADATA goes through the same filter as the values. NaN is the
        # dangerous case: every comparison with it is false, so
        # `abs(nan - x) >= 300` rejects nothing and a cache from another week
        # would come in as good - the guard would become decoration on precisely
        # the corrupted file.
        meta = [finite_number(cached[key]) for key in ("start", "day_start", "ts")]
        if any(m is None for m in meta):
            return None
        recorded_start, recorded_day, written_at = meta
        if abs(recorded_start - start) >= 300 or abs(recorded_day - today) >= 1:
            return None
        age = time.time() - written_at
        if age < 0 or (not accept_stale and age >= WEEK_CACHE_TTL):
            return None  # negative age = the clock went backwards; do not trust it
        shares = [
            finite_number(cached[key]) for key in ("share", "share_day", "share_day_all")
        ]
        if any(s is None or not 0.0 <= s <= 1.0 for s in shares):
            return None
        return Shares(*shares)
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        return None


def cost_shares(start: float) -> Shares | None:
    """The window's three `Shares`, all over the week's total cost, so they can
    be projected onto the same scale as the aggregate quota.

    `day_total` does not filter by model: it is the whole day, all models - what
    feeds the daily cap of the TOTAL quota.

    One scan feeds all three: the day's window is inside the week's window.

    Returns None when THERE IS NO BASIS to estimate - a scan with no sample at
    all (new user, wiped history, a `~/.claude/projects` from another machine) or
    a scan that blew its time budget. Zero here would be worse than nothing: it
    would show up on the bar as a measured `0.0%` next to a weekly quota of 68%.
    """
    today = day_start()
    try:
        cache_path = state_file(WEEK_CACHE_FILE)
    except OSError:  # read-only home: carry on without a cache
        cache_path = None

    fresh = read_cache(cache_path, start, today, accept_stale=False)
    if fresh is not None:
        return fresh

    totals = {"fable": 0.0, "fable_day": 0.0, "all": 0.0, "all_day": 0.0}

    def accumulate(entries):
        for entry in entries:
            cost = entry_cost(entry)
            totals["all"] += cost
            ts = parse_ts(entry.get("timestamp"))
            # Bounded on both sides. "From midnight on" alone lets an entry
            # dated in the future - a clock that ran ahead, a transcript copied
            # from another machine, a mangled timezone - count against today's
            # cap, inflating it with spending that never happened today. The
            # slack matches RESET_TOLERANCE: seconds of clock skew are normal,
            # days are not.
            # No upper bound, deliberately. Refusing entries dated ahead of the
            # clock was tried and reverted: it kept them in the denominator while
            # dropping them from the numerator, so a machine running a few minutes
            # fast - or transcripts synced from one - drove both daily fields to a
            # measured 0.0% beside a weekly quota at 68%. Counting a future-dated
            # entry as today's overstates the daily cap, which is the loud,
            # conservative error; refusing it understates, which is the quiet one
            # that makes someone blow through the cap. Prefer the loud one.
            is_today = ts is not None and ts >= today
            if is_today:
                totals["all_day"] += cost
            model = as_text(entry["message"].get("model")).lower()
            if model.startswith(FABLE_PREFIXES):
                totals["fable"] += cost
                if is_today:
                    totals["fable_day"] += cost

    completed = scan_projects(start, accumulate)
    if not completed or totals["all"] <= 0:
        # With no trustworthy scan, a stale cache still describes the week better
        # than an invented zero. Not even that? The field disappears from the bar.
        return read_cache(cache_path, start, today, accept_stale=True)

    shares = Shares(
        totals["fable"] / totals["all"],
        totals["fable_day"] / totals["all"],
        totals["all_day"] / totals["all"],
    )
    if cache_path is not None:
        # ATOMIC write: several Claude Code sessions render at the same time and
        # write this very file. A straight `write_text` leaves a window in which
        # the file is truncated/half-written, and the neighboring session would
        # read cut JSON. Write to a temp file in the same directory and rename -
        # `os.replace` is atomic on both systems.
        tmp_file = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
        try:
            write_private(
                tmp_file,
                json.dumps(
                    {
                        "start": start,
                        "day_start": today,
                        "ts": time.time(),
                        "share": shares.fable,
                        "share_day": shares.fable_day,
                        "share_day_all": shares.day_total,
                    }
                ),
            )
            os.replace(tmp_file, cache_path)
        except OSError:
            try:
                tmp_file.unlink()  # do not leave litter behind per session
            except OSError:
                pass
    return shares


def weekly_window(data: dict) -> Window | None:
    """(% of the weekly quota already used, start of the 7-day window, calendar
    days until the reset counting today) - the common ground of both daily caps.

    The reset day counts whole: whatever balance is left is spendable up to the
    reset hour, so it is not rationed by a fraction of a day.

    WITHOUT a valid reset in the payload it returns None, and the three fields
    derived from it (weekly Fable, Fable's day, day total) disappear from the
    bar. Guessing "7 days left" would be worse than not showing: the rationing
    divides the balance by the days remaining, so the widest possible guess
    squeezes the day's cap to the minimum and paints red a day that may well be
    slack. The official weekly percentage does not depend on this and stays on
    screen.
    """
    weekly = usage_percent(data, "seven_day")
    if weekly is None:
        return None

    resets_at = valid_reset(data, "seven_day")
    if resets_at is None:
        return None
    start = resets_at - SEVEN_DAYS
    # CALENDAR days until the reset, counting today. It counts calendar DATES:
    # dividing seconds by 86400 is off by one on the day daylight saving starts
    # or ends. The reset day only does NOT count when the reset falls exactly at
    # midnight - then that day is born with no quota at all. A reset at
    # 00:00:00.5 still has quota, however little, so the day counts.
    when = datetime.fromtimestamp(resets_at)
    reset_day = when.date()
    if (when.hour, when.minute, when.second, when.microsecond) == (0, 0, 0, 0):
        reset_day -= timedelta(days=1)
    days = max(1, (reset_day - date.today()).days + 1)
    return Window(weekly, start, days)


def daily_cap_percent(week_spent: float, today_spent: float, cap: float, days: float) -> float:
    """% of the DAILY cap already consumed, given the week's spending, today's and the cap.

    The day's cap is MOVING, not a fixed 1/7: it is what is LEFT of the cap at
    the start of the day, divided by the calendar days remaining until the reset
    (counting today).

        todays_cap = (cap - spent_BEFORE_today) / days_remaining

    Blowing today does not change today's cap (it was fixed at midnight -
    otherwise the bar would shrink the ruler as you spend and would never close
    at 100%); it narrows the FOLLOWING days, which is the rationing this is for.
    It is not clamped at 100: going past the day's share is legitimate and has to
    show. With no balance at all at the start of the day, the day is born at 100%.
    """
    balance_at_day_start = max(0.0, cap - (week_spent - today_spent))
    todays_cap = balance_at_day_start / days
    return today_spent / todays_cap * 100 if todays_cap > 0 else 100.0


def daily_total_percent(data: dict) -> float | None:
    """% of the DAILY cap of the TOTAL quota (all models) already consumed.

    Same rationing as Fable's daily cap, but over the whole quota (cap = 100
    points, not 50) and without filtering by model. The payload gives the
    official WEEKLY total (`seven_day.used_percentage`) and exposes no daily
    breakdown at all (checked against the real payload, 2026-07-27: `rate_limits`
    carries only `five_hour` and `seven_day`), so TODAY's part comes out of the
    transcripts, by weighted cost, the same way Fable's share does - and with the
    same estimate caveat.
    """
    window = weekly_window(data)
    if window is None:
        return None
    shares = cost_shares(window.start)
    if shares is None:  # no basis to estimate today's slice: the field disappears
        return None
    today_spent = shares.day_total * window.percent
    return daily_cap_percent(window.percent, today_spent, 100.0, window.days)


def usage_api_enabled() -> bool:
    """Whether fetching the OFFICIAL usage is enabled (`USAGE_API_ENV`).

    Opt-in by design, not by technical caution - see the constant's comment.
    Enable with `export CLAUDE_STATUSLINE_USAGE_API=1`.
    """
    value = as_text(os.environ.get(USAGE_API_ENV, "")).strip().lower()
    return value in ("1", "true", "yes", "on")


def _official_raw() -> dict:
    """The official cache as it sits on disk, with no freshness filter.

    Kept apart from `official_usage` because the fetch trigger needs to see the
    STALE record - that record is precisely what says it is time to fetch again.
    """
    try:
        record = json.loads(state_file(OFFICIAL_CACHE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def official_usage(now: float | None = None) -> dict | None:
    """The officially measured usage, if it was measured recently. Else None.

    None on any doubt - API disabled, file missing, no timestamp, too old, or a
    timestamp in the FUTURE (the clock moved). The caller falls back to the
    estimate, which is the honest degradation: an old number painted in the same
    color as a fresh one is what this file refuses everywhere else.
    """
    if not usage_api_enabled():
        return None
    now = time.time() if now is None else now
    record = _official_raw()
    ts = finite_number(record.get("ts"))
    if ts is None or not 0 <= now - ts <= OFFICIAL_MAX_AGE:
        return None
    # Age alone is not enough: the measurement carries the RESET of the window it
    # belongs to, and one taken shortly before the weekly reset would stay
    # "fresh" for up to an hour AFTER it - the bar would show 99% of the cap with
    # the new week at 0%. Once the reset the record itself declares has passed,
    # the record belongs to another week and no longer counts, however new it is.
    reset = parse_ts(record.get("fable_resets_at") or record.get("week_resets_at"))
    if reset is not None and now >= reset:
        return None
    return record


def official_percent(record, key: str) -> float | None:
    """One percentage out of the official cache, validated as outside data.

    It IS outside data: the file may have been written by an older version of the
    fetcher, truncated mid-write, or hand-edited.
    """
    if not isinstance(record, dict):
        return None
    value = finite_number(record.get(key))
    if value is None or not 0 <= value <= 100:
        return None
    return value


def factor_in_use(record) -> float:
    """The calibration factor to apply to the LOCAL Fable share.

    The re-measured one wins: the fetcher recomputes the factor on every round
    against the local share of the same instant, which is what stops the
    calibration from silently ageing - the flaw that motivated all of this. The
    source constant is the floor, and is the only value when the API is disabled.
    A factor outside 0..5 is not bad calibration, it is a corrupt file.
    """
    measured = finite_number(record.get("factor")) if isinstance(record, dict) else None
    if measured is None or not 0 < measured <= 5:
        return FABLE_SHARE_CALIBRATION
    return measured


def should_fetch_official(now: float, ts, attempt) -> bool:
    """Whether it is time to fetch the official number again. PURE decision.

    Split from the spawn because whatever reads the clock and creates processes
    gives no deterministic test - and this is the part that needs one, not the
    `Popen`. Two brakes with different deadlines: the TTL (age of the
    MEASUREMENT) says when the number got old, and the floor (age of the ATTEMPT)
    stops a fast repeated failure from becoming a burst of processes. A timestamp
    in the future counts as absent, as everywhere else in this file.
    """
    if ts is not None and 0 <= now - ts < OFFICIAL_TTL:
        return False
    if attempt is not None and 0 <= now - attempt < OFFICIAL_SPAWN_FLOOR:
        return False
    return True


def claim_attempt(stamp: Path, now: float) -> bool:
    """Try to win the turn to fetch. True for only ONE session per floor window.

    Reading the mtime and then writing gives no exclusion: between the two
    operations every session reads the same old stamp and each believes it won -
    measured with 5 Claude Code sessions, that produced 5 fetches at the same
    instant. Here the stamp is opened once and locked with a non-blocking
    `flock`; whoever misses the lock drops the round, and the mtime is re-checked
    INSIDE the lock, the only point where it cannot change underneath the
    decision.

    A freshly created file is born with an mtime of now, so the mtime alone would
    block everyone's first fetch. SIZE is what separates the two cases: a stamp
    that was never claimed holds 0 bytes; whoever claims it writes 1.

    Without `fcntl` (Windows) the mtime check is all that is left: a race again,
    but it still stops the refresh burst of a single session, which is the common
    case.
    """
    try:
        fd = os.open(stamp, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass  # no flock on this platform: carry on with the mtime alone
        except OSError:
            return False  # another session holds the turn right now
        try:
            state = os.fstat(fd)
        except OSError:
            return False
        claimed = state.st_size > 0
        # A stamp NEWER than the reference `now` counts as recent, not as absent.
        # The rest of this file treats the future as missing data, but the
        # meaning differs here: `now` is read at the start of the render and the
        # stamp is written microseconds later, so the session that got in first
        # produces a NEGATIVE age. With a floor of 0 that read as "nobody
        # claimed it" and fired anyway - the very hole this lock closes. An
        # absurd future (a clock moved back) counts again, so the fetch is not
        # blocked forever.
        age = now - state.st_mtime
        if claimed and -86400 <= age < OFFICIAL_SPAWN_FLOOR:
            return False
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"x")  # claims it and moves the mtime in the same stroke
        return True
    finally:
        os.close(fd)


def spawn_official_fetch(now: float | None = None) -> bool:
    """Fire the fetcher DETACHED once the cache expires. Never waits for it.

    The render has a deadline and Claude Code KILLS a status line still running
    when the next refresh arrives: a network call in here would turn a slow
    network into a bar that never appears. All that happens here is the process
    being started - whoever reads the result is the NEXT round, from the file.

    The attempt stamp is a file of its OWN, not the `attempt_ts` inside the
    cache, because it has to advance even when the child never gets as far as
    writing (missing script, broken interpreter). Without it, the "spawn always
    fails" case would become a fork attempt every 15 seconds.
    """
    if not usage_api_enabled():
        return False
    now = time.time() if now is None else now
    stamp = state_file(OFFICIAL_STAMP_FILE)
    try:
        last = stamp.stat().st_mtime
    except OSError:
        last = None
    if not should_fetch_official(now, finite_number(_official_raw().get("ts")), last):
        return False
    # The stamp is the MUTEX, which is why it is claimed through an exclusive
    # open rather than through `write_private`. Between the `stat` above and the
    # write there is a window in which several sessions read the same old stamp
    # and each believes it was elected: with 5 Claude Code sessions open, that
    # measured 5 fetches at the same instant.
    if not claim_attempt(stamp, now):
        return False
    try:
        import subprocess

        subprocess.Popen(
            [sys.executable, str(FETCH_OFFICIAL)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError, ImportError):
        return False
    return True


def fable_cap_percent(data: dict) -> tuple[float, float] | None:
    """(% of Fable's WEEKLY cap, % of the DAILY one) already consumed.

    The CAP is official (FABLE_CAP_SHARE). How much of it is gone comes from one
    of two sources, in this order:

    1. MEASURED, when `official_usage()` answers: Fable's weekly consumption read
       from `GET /api/oauth/usage` by the sibling `claude_usage_fetch.py`. It is
       the same number the claude.ai usage screen shows. Opt-in - see
       `USAGE_API_ENV`.
    2. ESTIMATED, the usual path, when there is no fresh official number.

    The DAY field has no official source in either case: the API serves the
    weekly per-model slice and nothing daily, so it always comes from the local
    share corrected by `factor_in_use`. The difference is that, with the API
    enabled, that factor is RE-MEASURED every round instead of sitting in the
    source ageing.

    The payload Claude Code sends here does not break consumption down by model -
    it lists only `rate_limits.five_hour` and `rate_limits.seven_day` (checked
    2026-07-26), and that gap is what the estimate covers. It derives Fable's
    share from the week's transcripts (weighted cost, not raw tokens, which is
    what approximates the quota) and projects it onto that aggregate:

        fable_share = fable_cost / total_cost   (over the 7 days)
        quota_points_spent_by_fable = fable_share * seven_day.used_percentage
        % of the cap = points / 50 * 100

    The DAY's cap is the same moving rationing as `daily_cap_percent`, here with
    cap = 50 points (the half that belongs to Fable).

    The projection is an estimate: Anthropic publishes the quota in model HOURS,
    with wide ranges (Max 5x: 15-35h of Opus per week), never as per-token weight
    - the real weighting of each model inside the quota is not published. It
    serves as a compass, not as accounting.
    """
    spawn_official_fetch()
    window = weekly_window(data)
    if window is None:
        return None
    shares = cost_shares(window.start)
    if shares is None:  # no basis to estimate the share: both fields disappear
        return None
    official = official_usage()
    weekly_cap = FABLE_CAP_SHARE * 100  # quota points Fable may occupy
    # The calibration is applied HERE, at consumption time, and not inside
    # `cost_shares`: that way the cache keeps the RAW share and changing the
    # factor takes effect immediately, with no need to invalidate the file (and
    # without mixing measurement and correction into the same number).
    factor = factor_in_use(official)
    day_share = shares.fable_day * factor
    today_spent = day_share * window.percent
    # The WEEKLY one, when a fresh official number exists, is a reading and not
    # an estimate - which is why it goes in directly, through neither share nor
    # factor. The spend in quota points is reconstructed from it because the DAY
    # cap needs the remaining balance, and that balance has to come from the same
    # number the bar displays: deriving the weekly from the official and the daily
    # from the estimate would put two rulers on one bar, which is what this
    # function already refused by calibrating both together.
    official_week = official_percent(official, "fable_percent")
    if official_week is not None:
        week_spent = official_week / 100 * weekly_cap
        # TODAY sits INSIDE the week, so it cannot have spent more than the week
        # has. On the all-estimated path the inequality holds by construction
        # (the day's share comes from the same denominator as the week's), but
        # here the two sides come off different rulers: the weekly measured, the
        # daily estimated. Without the cap, a daily estimate above the official
        # weekly makes the day's balance start out LARGER than the cap - the bar
        # would reach "week 0%, day 250%".
        today_spent = min(today_spent, week_spent)
    else:
        week_spent = shares.fable * factor * window.percent
    # NOT capped, for the same reason the daily cap is not: 100% here is the
    # point where Fable leaves the included tier and starts eating credits, so
    # 144% is the most actionable thing on the bar - "you passed it a while ago".
    # Capped, whoever blew through the cap saw 100.0%, exactly like someone who
    # landed on it dead on.
    pct_week = week_spent / weekly_cap * 100
    pct_day = daily_cap_percent(week_spent, today_spent, weekly_cap, window.days)
    return pct_week, pct_day


def calibration_factor(raw_share: float, all_percent: float, fable_percent: float) -> float:
    """The `FABLE_SHARE_CALIBRATION` that would make the bar reproduce the official.

    The bar shows `share * factor * all_percent / (FABLE_CAP_SHARE * 100) * 100`.
    Setting that equal to Fable's official number and isolating the factor:

        factor = fable_percent * FABLE_CAP_SHARE / (raw_share * all_percent)

    ValueError when no division is possible: a share or a quota at zero is not a
    bad calibration, it is a missing measurement - and returning a number there
    would invent a factor wearing the face of a measured one.

    Everything goes through `finite_number` BEFORE any range comparison, for the
    same reason the rest of the file does it: NaN slips past range checks, because
    every comparison against it is false. A `nan` typed into the command would
    walk through the `if`s untouched and come out as a NaN factor, which would
    then erase both Fable fields from the bar without saying why.
    """
    values = [finite_number(v) for v in (raw_share, all_percent, fable_percent)]
    if any(v is None for v in values):
        raise ValueError("invalid number: finite values only")
    raw_share, all_percent, fable_percent = values
    if not 0 < raw_share <= 1:
        raise ValueError("share outside 0..1: no measurement to calibrate against")
    if all_percent <= 0:
        raise ValueError("total quota at zero: no measurement to calibrate against")
    if fable_percent < 0:
        raise ValueError("negative Fable percentage")
    return fable_percent * FABLE_CAP_SHARE / (raw_share * all_percent)


def cache_is_calibratable(start: float, day: float, written_at: float, now: float) -> str:
    """Why the cache CANNOT be used to calibrate; "" when it can.

    Split out of `calibrate_command` so it has a deterministic test - code that
    reads the clock has none - and because this is precisely the rule that keeps
    the factor from crossing two periods, which was the 2026-07-30 review finding.

    Week and day are HARD cuts: last week's share against this week's official
    number yields a perfectly formatted factor that means nothing. Freshness is
    deliberately loose (2x the TTL): the weekly share moves slowly - the very
    reason the TTL exists - and the cache only gets much older than that when the
    bar stopped rendering. Cutting at 1x would refuse to calibrate during the
    seconds the cache sits expired waiting for the next render.
    """
    if not 0 <= now - start <= SEVEN_DAYS:
        return "from another week"
    if abs(day - day_start()) >= 1:
        return "from another day"
    age = now - written_at
    if age < 0 or age >= 2 * WEEK_CACHE_TTL:
        return f"too old ({age / 60:.0f} min, limit {2 * WEEK_CACHE_TTL // 60})"
    return ""


def calibrate_command(argv) -> int:
    """`--calibrate <all%> <fable%>` - recompute the factor against the usage screen.

    Both numbers are the ones claude.ai shows under Settings > Usage (and which
    the API returns in `limits`, as `weekly_all` and `weekly_scoped`/Fable). The
    raw share comes from this machine's cache, so the command only works after
    the bar has scanned the transcripts at least once.
    """
    def number(text: str) -> float:
        # `float("nan")` and `float("inf")` do NOT raise - they would come through
        # intact and only blow up later, as an absurd factor.
        value = finite_number(float(text.strip().rstrip("%").replace(",", ".")))
        if value is None:
            raise ValueError("not finite")
        return value

    try:
        all_percent = number(argv[0])
        fable_percent = number(argv[1])
    except (IndexError, ValueError):
        print(
            "usage: statusline.py --calibrate <all%> <fable%>\n"
            "\n"
            "  Both numbers come from Settings > Usage on claude.ai:\n"
            "    <all%>    weekly limit across all models\n"
            "    <fable%>  weekly limit for Fable\n"
            "\n"
            "  Read both at the SAME moment: they move, and calibrating with\n"
            "  numbers taken at different times bakes the drift into the factor."
        )
        return 2

    # The cache has to be from THIS week, THIS day, and be fresh. Calibrating
    # against last week's share would hand back a perfectly formatted factor that
    # crosses two periods - an invented number wearing the face of a measured one,
    # which is exactly what this bar refuses everywhere else. These are the same
    # checks `read_cache` runs, spelled out here because there is no payload (and
    # therefore no `resets_at`) to hand it. Finding from the 2026-07-30 review.
    try:
        cached = json.loads(state_file(WEEK_CACHE_FILE).read_text(encoding="utf-8"))
        raw_share, start, day, written_at = (
            finite_number(cached.get(key))
            for key in ("share", "start", "day_start", "ts")
        )
    except (OSError, ValueError, AttributeError):
        raw_share = start = day = written_at = None
    if None in (raw_share, start, day, written_at):
        print("no weekly share cache yet - let the bar render once and try again")
        return 1

    reason = cache_is_calibratable(start, day, written_at, time.time())
    if reason:
        print(f"the weekly share cache is {reason} - calibrating against it would"
              " cross two periods.\nlet the bar render once and try again")
        return 1

    try:
        new = calibration_factor(raw_share, all_percent, fable_percent)
    except ValueError as error:
        print(str(error))
        return 1

    cap = FABLE_CAP_SHARE * 100
    current = raw_share * FABLE_SHARE_CALIBRATION * all_percent / cap * 100
    raw = raw_share * all_percent / cap * 100
    print(f"raw Fable share (cache)     {raw_share * 100:.2f}%")
    print(f"uncalibrated the bar shows   {raw:.1f}%")
    print(f"factor in use now            {FABLE_SHARE_CALIBRATION}")
    print(f"and with it the bar shows    {current:.1f}%")
    print(f"official you reported        {fable_percent:.1f}%"
          f"  (out of {all_percent:.1f}% of the total quota)")
    print()
    print(f"factor that matches:  FABLE_SHARE_CALIBRATION = {new:.3f}")
    return 0


def reset_in(data: dict, window: str) -> str:
    """How long until the 'five_hour' or 'seven_day' reset.

    Same plausibility filter as everything else: an absurd reset used to show up
    as '95076d 21h' on the bar, which is noise wearing the face of data.
    """
    resets_at = valid_reset(data, window)
    if resets_at is None:
        return ""
    return format_duration(resets_at - time.time())


def usage_percent(data: dict, window: str) -> float | None:
    limits = as_dict(as_dict(data.get("rate_limits")).get(window))
    percent = finite_number(limits.get("used_percentage"))
    if percent is None:
        return None
    return max(0.0, min(100.0, percent))


# -------------------------------------------------------------------- render


def render(data: dict) -> str:
    """Assembles the two lines. Each piece fails to an empty string and
    disappears without leaving a hole."""

    def safe(fn, *args):
        try:
            return fn(*args)
        except Exception:
            return ""

    # `safe_text` on the THREE texts that go to the terminal, not just the
    # session name: the model name and the effort level also arrive via the
    # payload.
    model_info = as_dict(data.get("model"))
    model = safe_text(model_info.get("display_name"), 60)
    model_color = model_sgr(model_info.get("id"))
    effort = safe_text(as_dict(data.get("effort")).get("level"), 20)
    name = safe(session_name, data)
    session_tok = safe(session_tokens, data)
    reset_5h = safe(reset_in, data, "five_hour")
    reset_week = safe(reset_in, data, "seven_day")

    def fraction(window: str) -> float | None:
        value = safe(elapsed_fraction, data, window)
        return value if isinstance(value, float) else None

    def pct(window: str) -> str:
        """Real quota (5h / week): color by pace against its own reset."""
        value = safe(usage_percent, data, window)
        if not isinstance(value, float):
            return ""
        return paint(f"{value:.1f}%", pace_sgr(value, fraction(window)))

    ctx = safe(context_percent, data)
    if isinstance(ctx, float):
        ctx_txt = paint(f"{ctx:.1f}%", scale_sgr(ctx, SCALE_CTX, SCALE_CTX_ALERT))
        if ctx >= SCALE_CTX[-1][0]:
            ctx_txt += " " + paint(CTX_HINT, SCALE_CTX_ALERT)
    else:
        ctx_txt = ""
    pct_5h_txt = pct("five_hour")
    pct_week_txt = pct("seven_day")
    # Deadline of both daily caps: the calendar day, clipped by the week on the
    # reset day (see `day_elapsed_fraction`).
    window = safe(weekly_window, data)
    if isinstance(window, Window):
        day_frac = safe(day_elapsed_fraction, window.start, window.start + SEVEN_DAYS)
    else:
        day_frac = safe(day_elapsed_fraction)
    if not isinstance(day_frac, float):
        day_frac = None

    fable = safe(fable_cap_percent, data)
    if isinstance(fable, tuple):
        fable_week, fable_day = fable
        pct_fable_txt = paint(
            f"{fable_week:.1f}%", fable_sgr(fable_week, fraction("seven_day"))
        )
        pct_fable_day_txt = paint(
            f"{fable_day:.1f}%", fable_sgr(fable_day, day_frac, PACE_FLOOR_DAY)
        )
    else:
        pct_fable_txt = pct_fable_day_txt = ""

    # Daily cap of the TOTAL quota: pace scale, measured against the end of the
    # day. No `hard`: 96% of the day's cap at 11 p.m. is the rationing working,
    # not a block.
    day_total = safe(daily_total_percent, data)
    pct_day_total_txt = (
        paint(f"{day_total:.1f}%", pace_sgr(day_total, day_frac, floor=PACE_FLOOR_DAY, hard=None))
        if isinstance(day_total, float)
        else ""
    )

    def join(*parts):
        return "  ".join(p for p in parts if p)

    def join_with_dot(left: str, right: str) -> str:
        """The two sides with a "·" in between - the dot only enters if BOTH exist.

        The rule was written three times, with a slightly different condition in
        each one: it is the kind of thing that survives an edit and leaves a
        loose separator on the bar.
        """
        return join(left, paint("·", C_SEP_ITEM) if left and right else "", right)

    session_block = join_with_dot(
        paint(name, C_NAME), join(ctx_txt, paint(session_tok, C_SESSION_TOKENS))
    )
    block_5h = join_with_dot(pct_5h_txt, paint(reset_5h, C_RESET_5H))
    # The two daily caps: Fable on the left, total quota on the right.
    day_block = join_with_dot(pct_fable_day_txt, pct_day_total_txt)
    # Same order as the day block: Fable on the left, total quota on the right - so
    # both blocks are read from the same position, with no flip halfway through the
    # line. No "·" here: the last block closes with a single space up to the timer.
    week_block = join(pct_fable_txt, pct_week_txt, paint(reset_week, C_RESET_WEEK))
    model_block = join(
        paint(model, model_color),
        paint(effort, EFFORT_COLORS.get(effort.lower(), C_EFFORT)),
    )

    sep = f"  {paint('|', C_SEP_BLOCK)}  "
    session_line = sep.join(b for b in (model_block, session_block) if b)
    quota_line = sep.join(b for b in (block_5h, day_block, week_block) if b)
    if quota_line:
        rule = paint(QUOTA_DASHES, C_SEP_BLOCK)
        quota_line = f"{rule} {quota_line} {rule}"
    return "\n".join(l for l in (session_line, quota_line) if l)


# ------------------------------------------------------------------ selftest


def load_payload(raw: bytes) -> dict:
    """Decodes the payload ALWAYS as UTF-8, whatever the codepage.

    Claude Code sends the JSON in UTF-8. As a subprocess (no console) Python's
    `sys.stdin` falls back to the ANSI codepage - cp1252 on Windows - and
    'implementação' arrives as 'implementaÃ§Ã£o'. Reading the BINARY buffer and
    decoding it explicitly is the only way not to depend on the console.
    """
    if not raw:
        return {}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")  # non-UTF-8 payload: do not crash
    # Not just ValueError: deeply nested JSON blows the stack and comes out as
    # RecursionError, which is not a subclass of it. Letting that escape breaks
    # the one promise this file makes - that it never takes the session down -
    # and prints a traceback carrying absolute paths from the user's disk.
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def selftest() -> int:
    failures = []
    ran = 0

    def check(label, got, want):
        # Counted, not just failed: "OK - selftest (0 failure(s))" would print
        # exactly the same if the whole battery had been deleted. The number of
        # checks that ran is what tells green from empty.
        nonlocal ran
        ran += 1
        if got != want:
            failures.append(f"{label}: {got!r} != {want!r}")

    check("duration in minutes", format_duration(300), "5m")
    check("duration in hours", format_duration(2 * 3600 + 17 * 60), "2h 17m")
    check("duration in days drops the minute", format_duration(86400 + 22 * 3600 + 44 * 60), "1d 22h")
    check("below 24h the minute comes back", format_duration(23 * 3600 + 59 * 60), "23h 59m")
    check("negative duration", format_duration(-99), "0m")
    check("tokens k", format_tokens(1800), "1.8k")
    check("tokens M", format_tokens(1_800_000), "1.8M")
    check("low on the scale", scale_sgr(5), SCALE[0][1])
    check("scale boundary", scale_sgr(88), SCALE_ALERT)
    check("high on the scale", scale_sgr(99.9), SCALE_ALERT)
    check("opus price", price_for("claude-opus-4-8"), (5.0, 25.0))
    check("unknown price", price_for(None), DEFAULT_PRICE)
    check(
        "window from the id",
        context_window_size({"model": {"id": "claude-opus-4-8[1m]"}}),
        1_000_000,
    )
    check("default window", context_window_size({"model": {"id": "no-clue"}}), 200_000)
    check(
        "window from the payload",
        context_window_size({"context_window": {"context_window_size": 123}}),
        123,
    )
    check(
        "ctx% is of the whole window",
        round(
            context_percent(
                {
                    "context_window": {
                        "context_window_size": 1_000_000,
                        "current_usage": {"input_tokens": 400_000},
                    }
                }
            ),
            1,
        ),
        40.0,
    )
    check(
        "the official ctx% beats the computation",
        context_percent(
            {"context_window": {"used_percentage": 62.5,
                                "current_usage": {"input_tokens": 1}}}
        ),
        62.5,
    )
    check("ctx scale cold", scale_sgr(49, SCALE_CTX, SCALE_CTX_ALERT), "38;5;240")
    check("ctx scale amber", scale_sgr(50, SCALE_CTX, SCALE_CTX_ALERT), "38;5;143")
    check("ctx scale alert", scale_sgr(75, SCALE_CTX, SCALE_CTX_ALERT), SCALE_CTX_ALERT)
    check(
        "/compact hint in the red",
        CTX_HINT in render({"context_window": {"used_percentage": 80.0}}),
        True,
    )
    check(
        "no hint below the alert",
        CTX_HINT in render({"context_window": {"used_percentage": 60.0}}),
        False,
    )
    check("fable model orange bold", model_sgr("claude-fable-5"), "1;38;5;208")
    check("opus model cyan", model_sgr("claude-opus-4-8[1m]"), "38;5;109")
    check("sonnet model yellow", model_sgr("claude-sonnet-5"), "38;5;186")
    check("haiku model blue", model_sgr("claude-haiku-4-5"), "38;5;103")
    check("an unknown model falls back to neutral", model_sgr("gpt-whatever"), C_MODEL)
    check("low effort cyan", EFFORT_COLORS["low"], "38;5;37")
    check("xhigh effort orange", EFFORT_COLORS["xhigh"], "38;5;214")
    check("max effort red", EFFORT_COLORS["max"], "38;5;196")
    check("fable scale orange at the floor", scale_sgr(84, SCALE_FABLE, SCALE_FABLE_ALERT), "38;5;173")
    check("fable scale lights up before the cap",
          scale_sgr(99, SCALE_FABLE, SCALE_FABLE_ALERT), "38;5;214")
    check("fable scale alert", scale_sgr(100, SCALE_FABLE, SCALE_FABLE_ALERT), SCALE_FABLE_ALERT)
    check("fable scale never goes cold", scale_sgr(0, SCALE_FABLE, SCALE_FABLE_ALERT), "38;5;173")
    check(
        "the fable fallback keeps the cut at 80",
        scale_sgr(80, SCALE_FABLE_VALUE, SCALE_FABLE_ALERT),
        SCALE_FABLE_ALERT,
    )

    # ------------------------------------------------------------- pacing
    # The projection: consumption / share of the deadline elapsed. 100 = lands on
    # the cap.
    check("exact pace projects 100", pace_percent(50.0, 0.5, 0.15), 100.0)
    check("double the pace projects 200", pace_percent(40.0, 0.2, 0.15), 200.0)
    check("at the end of the deadline the projection is the value", pace_percent(80.0, 1.0, 0.15), 80.0)
    check("the floor holds the extrapolation at the start", pace_percent(3.0, 0.01, 0.15), 20.0)

    # The case that motivated pacing: 80% of the week near the reset is NOT an alert.
    check("80% on day 7 comes out yellow", pace_sgr(80.0, 6.2 / 7), "38;5;143")  # proj ~90
    check("the same 80% on day 2 is red", pace_sgr(80.0, 1.5 / 7), SCALE_PACE_ALERT)
    check("88% on the eve of the reset stays yellow", pace_sgr(88.0, 6.5 / 7), "38;5;143")
    check("at exact pace the middle of the week is yellow", pace_sgr(50.0, 0.5), "38;5;143")
    check("half the pace is blue", pace_sgr(25.0, 0.5), "38;5;67")
    check("a fifth of the pace is gray", pace_sgr(10.0, 0.5), "38;5;240")
    check("50% over a quarter of the deadline is red", pace_sgr(50.0, 0.25), SCALE_PACE_ALERT)
    check("a nearly exhausted quota is red even at the end", pace_sgr(96.0, 1.0), SCALE_PACE_ALERT)
    check("without hard the same reading does not alert", pace_sgr(96.0, 1.0, hard=None), "38;5;143")
    check("with no deadline it falls back to the value scale", pace_sgr(85.0, None), "38;5;173")
    check("with no deadline the old alert still holds", pace_sgr(90.0, None), SCALE_ALERT)
    # Through `fable_sgr`, not repeating the config: a test that re-declares what
    # it tests stays green after the config changes in the render.
    check("fable below pace is muted orange", fable_sgr(40.0, 0.5), "38;5;173")
    check("fable projecting an overrun is red", fable_sgr(60.0, 0.5), SCALE_FABLE_ALERT)
    check("fable with no deadline goes back to the value cut", fable_sgr(85.0, None), SCALE_FABLE_ALERT)

    # ------------------------------------------------- share calibration
    # Anchor case: the 2026-07-30 measurement. Both official numbers were read at
    # the SAME instant (84% of Fable's cap, with 92% of the total quota spent) and
    # confronted with the raw share the cache held at that hour.
    measured_share = 0.5132557883430428

    def pct_with(factor, share=measured_share, all_pct=92.0):
        """What the bar would show for Fable with a given calibration factor."""
        return share * factor * all_pct / (FABLE_CAP_SHARE * 100) * 100

    check("factor of the first measurement",
          round(calibration_factor(measured_share, 92.0, 84.0), 3), 0.889)
    # 2nd point, taken hours later with the official numbers at another level. It
    # is what turns the calibration into a supported hypothesis: two independent
    # factors landing 0.01 from each other.
    check("factor of the second measurement",
          round(calibration_factor(0.5088, 94.0, 86.0), 3), 0.899)
    check("uncalibrated the bar was inflating", round(pct_with(1.0), 1), 94.4)
    # Round-trip: the computed factor, REAPPLIED, must reproduce the official
    # number. That is what proves the formula's inversion - checking only the
    # factor's value would be running the same arithmetic twice and calling it a
    # test.
    check("the computed factor reproduces the official",
          round(pct_with(calibration_factor(measured_share, 92.0, 84.0)), 1), 84.0)
    # The factor IN USE has to reproduce the measurement that JUSTIFIES it - since
    # 2026-08-16, that day's, no longer 08-05's nor the 07-30 average (table at the
    # top of the file). Without this check, swapping the factor without recording
    # the new measurement would go unnoticed. The 1-point tolerance is the order of
    # the uncertainty in the official numbers, which the screen serves rounded to
    # integers.
    check("the factor in use reproduces the measurement behind it",
          abs(pct_with(FABLE_SHARE_CALIBRATION, share=0.2466, all_pct=22.0) - 15.0) < 1.0,
          True)
    # And it no longer reproduces the earlier regimes. These checks are the pair of
    # the one above: they are what records, executably, that there was a REGIME
    # CHANGE and not a tweak - if some future factor serves two periods at once, the
    # premise behind swapping the value (ranges that do not touch) has fallen, and
    # that has to show up as a failure rather than as silence.
    check("and no longer reproduces the 08-05 regime",
          abs(pct_with(FABLE_SHARE_CALIBRATION, share=0.4736, all_pct=68.0) - 60.0) < 1.0,
          False)
    check("and no longer reproduces the 07-30 regime",
          abs(pct_with(FABLE_SHARE_CALIBRATION) - 84.0) < 1.0, False)
    # A different pair, to make sure the formula was not fitted to the single case
    # that motivated it.
    check("the formula holds for another pair",
          round(pct_with(calibration_factor(measured_share, 50.0, 30.0), all_pct=50.0), 1), 30.0)
    nan, inf = float("nan"), float("inf")
    for name, bad_share, bad_all, bad_fable in (
        ("a zero share", 0.0, 92.0, 84.0),
        ("a zero quota", 0.5, 0.0, 84.0),
        ("a negative share", -0.1, 92.0, 84.0),
        ("a share above 1", 1.5, 92.0, 84.0),
        ("a negative fable", 0.5, 92.0, -1.0),
        # NaN slips past ANY range check, because every comparison against it is
        # false: only a finiteness filter BEFORE the `if`s catches it. Without
        # that, the command would hand back a NaN factor, which would erase both
        # Fable fields from the bar without explaining why.
        ("a NaN share", nan, 92.0, 84.0),
        ("a NaN quota", 0.5, nan, 84.0),
        ("a NaN fable", 0.5, 92.0, nan),
        ("an infinite quota", 0.5, inf, 84.0),
        ("an infinite fable", 0.5, 92.0, inf),
        ("a boolean share", True, 92.0, 84.0),
        ("a fable as text", 0.5, 92.0, "84"),
    ):
        try:
            calibration_factor(bad_share, bad_all, bad_fable)
            outcome = "no error"
        except ValueError:
            outcome = "ValueError"
        check(f"{name} does not calibrate", outcome, "ValueError")

    # Cache freshness for calibrating: the guard that keeps the factor from
    # crossing two periods. Fixed numbers, independent of the test's clock.
    today_t = day_start()
    now_t = today_t + 12 * 3600
    good_start = now_t - 3 * 86400
    check("a cache from this week and day works",
          cache_is_calibratable(good_start, today_t, now_t - 60, now_t), "")
    check("a cache from last week does not",
          cache_is_calibratable(now_t - SEVEN_DAYS - 60, today_t, now_t - 60, now_t) != "", True)
    check("a cache starting in the future does not",
          cache_is_calibratable(now_t + 600, today_t, now_t - 60, now_t) != "", True)
    check("a cache from another day does not",
          cache_is_calibratable(good_start, today_t - 86400, now_t - 60, now_t) != "", True)
    check("a cache expired by 1x the TTL still works",
          cache_is_calibratable(good_start, today_t, now_t - WEEK_CACHE_TTL - 30, now_t), "")
    check("a cache past 2x the TTL does not",
          cache_is_calibratable(good_start, today_t, now_t - 2 * WEEK_CACHE_TTL - 1, now_t) != "",
          True)
    check("a cache written in the future does not",
          cache_is_calibratable(good_start, today_t, now_t + 60, now_t) != "", True)

    # ------------------------------------------------- OFFICIAL usage (opt-in)
    # The path that swaps Fable's estimate for a reading. Everything on a fixed
    # clock and in a temporary directory - nothing here touches the network or
    # creates a process: the `Popen` is deliberately left out, and what gets
    # tested is the DECISION to fire, which is the part with a rule in it.
    import tempfile as _tempfile

    real_env = os.environ.get(USAGE_API_ENV)
    real_state = globals()["state_file"]
    real_fetch = globals()["FETCH_OFFICIAL"]
    with _tempfile.TemporaryDirectory(prefix="statusline-official-") as _dir:
        base = Path(_dir)
        try:
            globals()["state_file"] = lambda name: base / name
            # Point the fetcher at a path that does NOT exist: that way no test
            # here can create a real process or touch the network, not even under
            # a mutation that removes one of the brakes.
            globals()["FETCH_OFFICIAL"] = base / "does-not-exist.py"

            def write_official(**fields):
                (base / OFFICIAL_CACHE_FILE).write_text(
                    json.dumps(fields), encoding="utf-8")

            def enable(value):
                if value is None:
                    os.environ.pop(USAGE_API_ENV, None)
                else:
                    os.environ[USAGE_API_ENV] = value

            t0 = 1_800_000_000.0
            write_official(ts=t0, fable_percent=17.0, all_percent=24.0, factor=1.4)

            # -- the gate. Disabled is the DEFAULT, and a perfect cache does not
            #    open it.
            enable(None)
            check("the API is disabled by default", usage_api_enabled(), False)
            check("disabled, a fresh official number is ignored",
                  official_usage(t0), None)

            # Firing with an EXPIRED cache: with a fresh one the TTL brake holds
            # on its own and the test would pass even without the gate - a fixture
            # that fails to tell the two candidates apart (a mutant survived that
            # way). And what gets observed is the attempt STAMP, not just the
            # return value. Pointing the fetcher at a missing path does NOT stop
            # `Popen`: the executable is python itself and the bad path is only an
            # argument, so a process is born and dies at once, with no network and
            # no writes. The stamp is what proves both brakes were passed.
            stamp = base / OFFICIAL_STAMP_FILE
            write_official(ts=t0 - 86400, fable_percent=17.0)
            stamp.unlink(missing_ok=True)
            check("disabled, does not fire the fetch", spawn_official_fetch(t0), False)
            check("disabled, does not even try", stamp.exists(), False)
            enable("1")
            check("enabled and expired, it tries", spawn_official_fetch(t0), True)
            check("enabled and expired, it stamps the attempt", stamp.exists(), True)
            stamp.unlink(missing_ok=True)
            write_official(ts=t0, fable_percent=17.0, all_percent=24.0, factor=1.4)

            enable("0")
            check("the value 0 does not enable it", usage_api_enabled(), False)
            enable("maybe")
            check("an unknown value does not enable it", usage_api_enabled(), False)
            enable("1")
            check("the value 1 enables it", usage_api_enabled(), True)
            enable("On")
            check("enabling does not depend on case", usage_api_enabled(), True)

            # -- freshness of the official number
            enable("1")
            check("a fresh official number is used",
                  (official_usage(t0) or {}).get("fable_percent"), 17.0)
            check("at the edge of the max age it still counts",
                  (official_usage(t0 + OFFICIAL_MAX_AGE) or {}).get("fable_percent"), 17.0)
            check("too old and it disappears",
                  official_usage(t0 + OFFICIAL_MAX_AGE + 1), None)
            check("a timestamp in the future disappears", official_usage(t0 - 1), None)
            write_official(fable_percent=17.0)
            check("no timestamp, no deal", official_usage(t0), None)
            write_official(ts=float("nan"), fable_percent=17.0)
            check("a NaN timestamp is no deal", official_usage(t0), None)
            (base / OFFICIAL_CACHE_FILE).write_text("{ this is not json", encoding="utf-8")
            check("a corrupt file does not bring it down", official_usage(t0), None)
            (base / OFFICIAL_CACHE_FILE).write_text('["list"]', encoding="utf-8")
            check("json that is not an object is no deal", official_usage(t0), None)
            (base / OFFICIAL_CACHE_FILE).unlink()
            check("a missing file is no deal", official_usage(t0), None)

            # The RESET of the window the measurement belongs to. Age alone let a
            # measurement taken shortly before the reset count for another hour
            # after it - the bar would show 99% of the cap with the new week at
            # 0%.
            from datetime import timezone as _tz

            def iso(epoch):
                return datetime.fromtimestamp(epoch, _tz.utc).isoformat()

            write_official(ts=t0, fable_percent=99.0, fable_resets_at=iso(t0 + 60))
            check("a measurement whose window is still open counts",
                  (official_usage(t0) or {}).get("fable_percent"), 99.0)
            write_official(ts=t0, fable_percent=99.0, fable_resets_at=iso(t0 - 60))
            check("a measurement past its reset does not, however new",
                  official_usage(t0), None)
            write_official(ts=t0, fable_percent=99.0, week_resets_at=iso(t0 - 60))
            check("the week's reset stands in when Fable's is missing",
                  official_usage(t0), None)
            write_official(ts=t0, fable_percent=99.0, fable_resets_at="not a date")
            check("an unreadable reset does not invalidate the measurement",
                  (official_usage(t0) or {}).get("fable_percent"), 99.0)

            # -- the percentage from the file, validated as outside data
            check("a valid percentage passes",
                  official_percent({"fable_percent": 17.0}, "fable_percent"), 17.0)
            check("a NaN percentage disappears",
                  official_percent({"fable_percent": float("nan")}, "fable_percent"), None)
            check("a percentage above 100 disappears",
                  official_percent({"fable_percent": 101}, "fable_percent"), None)
            check("a negative percentage disappears",
                  official_percent({"fable_percent": -1}, "fable_percent"), None)
            check("a boolean percentage disappears",
                  official_percent({"fable_percent": True}, "fable_percent"), None)
            check("a non-dict record disappears",
                  official_percent("x", "fable_percent"), None)

            # -- the factor: measured beats the constant, junk falls back to it
            check("a measured factor beats the constant",
                  factor_in_use({"factor": 1.25}), 1.25)
            check("no measurement falls back to the constant",
                  factor_in_use({}), FABLE_SHARE_CALIBRATION)
            check("a None record falls back to the constant",
                  factor_in_use(None), FABLE_SHARE_CALIBRATION)
            check("a NaN factor falls back to the constant",
                  factor_in_use({"factor": float("nan")}), FABLE_SHARE_CALIBRATION)
            check("a zero factor falls back to the constant",
                  factor_in_use({"factor": 0.0}), FABLE_SHARE_CALIBRATION)
            check("an absurd factor falls back to the constant",
                  factor_in_use({"factor": 99.0}), FABLE_SHARE_CALIBRATION)

            # -- the firing decision, on fixed numbers
            check("with no measurement at all, fetch",
                  should_fetch_official(t0, None, None), True)
            check("a fresh measurement does not fetch",
                  should_fetch_official(t0, t0 - OFFICIAL_TTL + 1, None), False)
            check("an expired measurement fetches",
                  should_fetch_official(t0, t0 - OFFICIAL_TTL, None), True)
            check("a recent attempt holds even with an expired measurement",
                  should_fetch_official(t0, t0 - 86400, t0 - OFFICIAL_SPAWN_FLOOR + 1),
                  False)
            check("an old attempt lets it through",
                  should_fetch_official(t0, t0 - 86400, t0 - OFFICIAL_SPAWN_FLOOR), True)
            check("a measurement in the future does not hold",
                  should_fetch_official(t0, t0 + 600, None), True)
            check("an attempt in the future does not hold",
                  should_fetch_official(t0, t0 - 86400, t0 + 600), True)

            # -- only ONE session wins the turn per floor window
            # REAL clock here, not the `t0` fixture: what rules is the file's
            # mtime, which the system stamps with the actual time. Mixing the two
            # would make the difference millions of seconds and the floor would
            # never bite - the check would pass while testing nothing.
            real_now = time.time()
            turn = base / "turn.stamp"
            turn.unlink(missing_ok=True)
            check("the first attempt wins", claim_attempt(turn, real_now), True)
            check("the second, at the same instant, loses",
                  claim_attempt(turn, real_now), False)
            check("past the floor, it wins again",
                  claim_attempt(turn, time.time() + OFFICIAL_SPAWN_FLOOR + 1), True)
            # A 0-byte stamp means "never claimed": without that distinction a
            # freshly created file would already fall inside the floor and the
            # very first fetch would never happen.
            turn.unlink(missing_ok=True)
            turn.touch()
            check("an empty stamp does not count as claimed",
                  claim_attempt(turn, time.time()), True)

            # The lock only shows up with REAL processes competing. Two calls in
            # sequence are stopped by the mtime alone, so a sequential test lets
            # the removal of `flock` through - a mutant survived exactly that way.
            # Here three children start together and exactly one may win. Skipped
            # where there is no `fork` (Windows), the same platform where the
            # lock does not exist either.
            if hasattr(os, "fork"):
                race = base / "race.stamp"
                race.unlink(missing_ok=True)
                start_at = time.time() + 0.3
                pipes = []
                for _ in range(3):
                    read_fd, write_fd = os.pipe()
                    if os.fork() == 0:  # child
                        os.close(read_fd)
                        while time.time() < start_at:
                            pass
                        won = claim_attempt(race, time.time())
                        os.write(write_fd, b"1" if won else b"0")
                        os._exit(0)
                    os.close(write_fd)
                    pipes.append(read_fd)
                wins = 0
                for read_fd in pipes:
                    wins += os.read(read_fd, 1) == b"1"
                    os.close(read_fd)
                for _ in pipes:
                    os.wait()
                check("on a simultaneous start, only one process wins the turn",
                      wins, 1)

            # -- integration: with a fresh official number the WEEKLY is a reading.
            # Without this check, deleting the official branch inside
            # `fable_cap_percent` would pass green - every function above would
            # still be correct and nobody would be using them.
            write_official(ts=t0, fable_percent=17.0, all_percent=24.0, factor=1.0)
            fake_shares = Shares(fable=0.10, fable_day=0.05, day_total=0.5)
            # The `fable_payload` helper is only defined further down in this same
            # selftest, so the payload goes in by hand here.
            payload_o = {"rate_limits": {"seven_day": {
                "used_percentage": 24.0, "resets_at": day_start() + 5 * 86400}}}
            real_cost, real_official = globals()["cost_shares"], globals()["official_usage"]
            # The constant is pinned too: the ESTIMATED branch depends on it, and an
            # expected literal computed from the value in use would be
            # self-referential - it would pass for any value.
            real_factor = globals()["FABLE_SHARE_CALIBRATION"]
            try:
                globals()["FABLE_SHARE_CALIBRATION"] = 1.0
                globals()["cost_shares"] = lambda start: fake_shares
                globals()["official_usage"] = lambda now=None: {
                    "ts": t0, "fable_percent": 17.0, "factor": 1.0}
                with_official = fable_cap_percent(payload_o)
                globals()["official_usage"] = lambda now=None: None
                without_official = fable_cap_percent(payload_o)
            finally:
                globals()["cost_shares"] = real_cost
                globals()["official_usage"] = real_official
                globals()["FABLE_SHARE_CALIBRATION"] = real_factor
            check("with the official number, the weekly IS the official one",
                  round(with_official[0], 1) if with_official else None, 17.0)
            # Estimated: 0.10 * 1.0 * 24 = 2.4 points out of 50 = 4.8%. Far enough
            # from 17 that the check above cannot pass by fixture coincidence.
            check("without it, the weekly goes back to being estimated",
                  round(without_official[0], 1) if without_official else None, 4.8)
            check("the day field exists in both cases",
                  (with_official is not None and with_official[1] is not None
                   and without_official is not None and without_official[1] is not None),
                  True)
            # The day's VALUE, not just its existence. Without this check,
            # replacing the daily with `0.0` on the official branch passed green -
            # the hybrid arithmetic, which is the new part, went untested.
            # 8.5 points on the week, 1.2 today, 5 days to the reset:
            # balance = 50 - (8.5 - 1.2) = 42.7 -> day cap 8.54 -> 1.2/8.54.
            check("with the official number, the day comes from the hybrid arithmetic",
                  round(with_official[1], 1) if with_official else None, 14.1)

            # And today's CAP against the week. Fixture chosen to differentiate:
            # without the `min`, the day's balance would start out larger than
            # the cap and the field would read 97.6% instead of 5.0%.
            greedy_shares = Shares(fable=0.10, fable_day=0.50, day_total=0.9)
            try:
                globals()["FABLE_SHARE_CALIBRATION"] = 1.0
                globals()["cost_shares"] = lambda start: greedy_shares
                globals()["official_usage"] = lambda now=None: {
                    "ts": t0, "fable_percent": 1.0, "factor": 1.0}
                bigger_day = fable_cap_percent(payload_o)
            finally:
                globals()["cost_shares"] = real_cost
                globals()["official_usage"] = real_official
                globals()["FABLE_SHARE_CALIBRATION"] = real_factor
            check("today never exceeds the official week",
                  round(bigger_day[1], 1) if bigger_day else None, 5.0)
        finally:
            globals()["state_file"] = real_state
            globals()["FETCH_OFFICIAL"] = real_fetch
            if real_env is None:
                os.environ.pop(USAGE_API_ENV, None)
            else:
                os.environ[USAGE_API_ENV] = real_env
    # A fixture leak does not show up as an error: a test running under its own
    # patch agrees with it. These prove the `finally` really did restore things.
    check("state_file restored", globals()["state_file"] is real_state, True)
    check("environment restored", os.environ.get(USAGE_API_ENV), real_env)
    check("the calibration constant was restored",
          globals()["FABLE_SHARE_CALIBRATION"], 1.382)
    check("the fetcher path was restored",
          globals()["FETCH_OFFICIAL"] is real_fetch, True)

    # ------------------------------------------ reading and summing transcripts
    # Up to here nothing exercised the reverse reader, the deduplication or the
    # token sums - the part that actually reads disk. With real files in a
    # temporary directory, which go away at the end.
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="statusline-selftest-"))
    try:
        def write_jsonl(name: str, rows) -> Path:
            target = tmp / name
            target.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                encoding="utf-8",
            )
            return target

        def entry(model="claude-opus-5", inp=0, out=0, ts="2026-07-29T12:00:00.000Z", **extra):
            msg = {"model": model, "usage": {"input_tokens": inp, "output_tokens": out}}
            msg.update(extra)
            return {"timestamp": ts, "message": msg}

        # A file holding sensitive data is born for its owner only. On Windows
        # the mode is ignored and the test counts as "it did not break"; on POSIX
        # it bites.
        private = tmp / "private.json"
        write_private(private, '{"secret": 1}')
        check("a private write preserves the content",
              private.read_text(encoding="utf-8"), '{"secret": 1}')
        if os.name == "posix":
            check("on POSIX the file is born 0600", oct(private.stat().st_mode & 0o777), "0o600")
        write_private(private, '{"secret": 2}')
        check("a private write truncates whatever was there",
              private.read_text(encoding="utf-8"), '{"secret": 2}')

        simple = write_jsonl("simple.jsonl", [{"a": 1}, {"b": 2}, {"c": 3}])
        got = [json.loads(l) for l in read_lines_reverse(simple)]
        check("the reverse reader yields end to start", got, [{"c": 3}, {"b": 2}, {"a": 1}])

        # A line bigger than the read block (CHUNK): it has to come out whole.
        huge = {"x": "y" * (CHUNK * 2)}
        spanning = write_jsonl("spanning.jsonl", [huge, {"end": True}])
        got = [json.loads(l) for l in read_lines_reverse(spanning)]
        check("a line bigger than the block comes out whole", got[-1], huge)
        check("and the neighbors stay correct", got[0], {"end": True})

        # A line ABOVE the ceiling: it disappears whole, and the neighbors keep
        # coming out. With a toy CHUNK and MAX_LINE, otherwise the test would
        # have to write 16 MB - and a ceiling never reached in the test proves
        # nothing.
        real_chunk, real_max = CHUNK, MAX_LINE
        try:
            globals()["CHUNK"], globals()["MAX_LINE"] = 8, 16
            (tmp / "monster.jsonl").write_text(
                "before\n" + "M" * 200 + "\nafter\n", encoding="utf-8"
            )
            # `.strip()`: on Windows `write_text` writes CRLF and the `\r` would
            # stick to the line - platform noise, not behavior.
            out = [l.decode().strip() for l in read_lines_reverse(tmp / "monster.jsonl")]
            check("a line above the ceiling disappears whole",
                  any("M" in l for l in out), False)
            check("and not a fragment of it leaks", out, ["after", "before"])
            # Two giants in a row, so the discard state does not get stuck.
            (tmp / "monsters.jsonl").write_text(
                "a\n" + "M" * 200 + "\n" + "N" * 200 + "\nb\n", encoding="utf-8"
            )
            out = [l.decode().strip() for l in read_lines_reverse(tmp / "monsters.jsonl")]
            check("two giants in a row both disappear", out, ["b", "a"])
            # The giant as the FIRST line: the case where its fragment becomes
            # the reader's last accumulated chunk and would come out as if it
            # were a line.
            (tmp / "monster-on-top.jsonl").write_text(
                "M" * 200 + "\nend\n", encoding="utf-8"
            )
            out = [l.decode().strip() for l in read_lines_reverse(tmp / "monster-on-top.jsonl")]
            check("a giant on top leaves no leftover", out, ["end"])
        finally:
            globals()["CHUNK"], globals()["MAX_LINE"] = real_chunk, real_max

        (tmp / "empty.jsonl").write_text("", encoding="utf-8")
        check("an empty file does not break the reader",
              list(read_lines_reverse(tmp / "empty.jsonl")), [])
        (tmp / "dirty.jsonl").write_text('{"ok":1}\nnot json\n\n', encoding="utf-8")
        check("an invalid line does not take the parse down",
              len(usage_entries(tmp / "dirty.jsonl")), 0)

        # Streaming deduplication: only the entries with `stop_reason`, plus the
        # last one if it comes as null (the response still open).
        stream = write_jsonl("stream.jsonl", [
            entry(inp=10, stop_reason=None),   # partial
            entry(inp=20, stop_reason=None),   # partial
            entry(inp=30, stop_reason="end_turn"),
            entry(inp=40, stop_reason=None),   # the last one, still open
        ])
        sums = [int(sum_tokens(e["message"]["usage"], TOKENS_TOTAL))
                for e in usage_entries(stream)]
        check("dedup keeps the closed one and the last open one", sums, [30, 40])

        # Time window: an entry older than `start` stays out.
        window_file = write_jsonl("window.jsonl", [
            entry(inp=1, ts="2026-07-20T12:00:00.000Z"),
            entry(inp=2, ts="2026-07-29T12:00:00.000Z"),
        ])
        cutoff = parse_ts("2026-07-25T00:00:00.000Z")
        check("the time window cuts off what is old",
              len(usage_entries(window_file, cutoff)), 1)
        check("with no window, everything comes in", len(usage_entries(window_file)), 2)

        # Cost: weighted by token type and by the entry's own model price.
        opus = entry(model="claude-opus-5", inp=1_000_000, out=1_000_000)
        check("cost weighs input and output", round(entry_cost(opus), 2), 30.0)
        haiku = entry(model="claude-haiku-4-5", inp=1_000_000, out=1_000_000)
        check("cost uses the price of the entry's own model", round(entry_cost(haiku), 2), 6.0)
        check("an unknown model falls back to the default price",
              round(entry_cost(entry(model="model-that-does-not-exist", inp=1_000_000)), 2), 5.0)
        cache_entry = {"timestamp": "2026-07-29T12:00:00.000Z", "message": {
            "model": "claude-opus-5",
            "usage": {"cache_read_input_tokens": 1_000_000,
                      "cache_creation_input_tokens": 1_000_000}}}
        check("a cache read costs 10% of the input",
              round(entry_cost(cache_entry), 3), round(5.0 * CACHE_READ_MULT + 5.0 * CACHE_WRITE_MULT, 3))

        # Crooked counters in the transcript must not poison the sum.
        check("a NaN token becomes zero", token_count({"input_tokens": float("nan")}, "input_tokens"), 0.0)
        check("a negative token becomes zero", token_count({"input_tokens": -5}, "input_tokens"), 0.0)
        check("a string token becomes zero", token_count({"input_tokens": "lots"}, "input_tokens"), 0.0)
        check("a missing token becomes zero", token_count({}, "input_tokens"), 0.0)
        poisoned = entry(inp=float("nan"), out=100)
        check("one entry with a NaN does not contaminate the cost",
              math.isfinite(entry_cost(poisoned)), True)

        # `sum_tokens`: the session total includes the output, the window does not.
        full_usage = {"input_tokens": 1, "output_tokens": 10,
                      "cache_read_input_tokens": 100, "cache_creation_input_tokens": 1000}
        check("the context window does not count the output", sum_tokens(full_usage), 1101.0)
        check("the session total counts the output",
              sum_tokens(full_usage, TOKENS_TOTAL), 1111.0)

        # On-disk cache: it only counts if it is from the same window, the same
        # day, and sane.
        cache_file = tmp / "cache.json"

        def write_cache(**fields):
            base = {"start": 1000.0, "day_start": 2000.0, "ts": time.time(),
                    "share": 0.5, "share_day": 0.25, "share_day_all": 0.3}
            base.update(fields)
            cache_file.write_text(json.dumps(base), encoding="utf-8")

        write_cache()
        check("a healthy cache is read", read_cache(cache_file, 1000.0, 2000.0, False), Shares(0.5, 0.25, 0.3))
        check("a cache from another window is ignored", read_cache(cache_file, 9999.0, 2000.0, False), None)
        check("a cache from another day is ignored", read_cache(cache_file, 1000.0, 9999.0, False), None)
        write_cache(ts=time.time() - WEEK_CACHE_TTL - 10)
        check("an expired cache does not count", read_cache(cache_file, 1000.0, 2000.0, False), None)
        check("but it does when stale is accepted",
              read_cache(cache_file, 1000.0, 2000.0, True), Shares(0.5, 0.25, 0.3))
        write_cache(ts=time.time() + 99999)
        check("a cache from the future (clock went backwards) does not count",
              read_cache(cache_file, 1000.0, 2000.0, True), None)
        write_cache(share=float("nan"))
        check("a NaN share in the cache does not count", read_cache(cache_file, 1000.0, 2000.0, False), None)
        write_cache(share=1.5)
        check("a share outside 0..1 does not count", read_cache(cache_file, 1000.0, 2000.0, False), None)
        cache_file.write_text("{garbage", encoding="utf-8")
        check("a corrupted cache does not count", read_cache(cache_file, 1000.0, 2000.0, False), None)
        check("with no cache path there is no reading", read_cache(None, 1.0, 2.0, False), None)
        # The cache metadata is a number from outside too: NaN makes EVERY
        # comparison false, so a cache from another week would slip past the
        # guards.
        for field in ("start", "day_start", "ts"):
            for poison in (float("nan"), float("inf"), "text", None, True):
                write_cache(**{field: poison})
                check(f"a cache with {field}={poison!r} does not count",
                      read_cache(cache_file, 1000.0, 2000.0, False), None)
        write_cache(ts=10**300)
        check("a cache with an absurd ts does not count (nor blow up)",
              read_cache(cache_file, 1000.0, 2000.0, False), None)

        # `scan_projects` against a toy tree: proves that it DESCENDS into the
        # subfolders (where the subagent transcripts live, most of the volume for
        # anyone who orchestrates) and that it knows when it did NOT complete.
        tree = tmp / "projects"
        (tree / "proj-a" / "subagents").mkdir(parents=True)
        now_iso = datetime.now().replace(hour=12).isoformat()
        entry_json = json.dumps(
            {"timestamp": now_iso,
             "message": {"model": "claude-opus-5", "usage": {"input_tokens": 1},
                         "stop_reason": "end_turn"}}
        )
        (tree / "proj-a" / "session.jsonl").write_text(entry_json + "\n", encoding="utf-8")
        (tree / "proj-a" / "subagents" / "agent-1.jsonl").write_text(
            entry_json + "\n", encoding="utf-8")
        (tree / "proj-a" / "not-a-transcript.txt").write_text("x", encoding="utf-8")

        seen = []
        completed = scan_projects(time.time() - SEVEN_DAYS, seen.extend, tree)
        check("the scan sees the session and the subagent", len(seen), 2)
        check("a complete scan says it completed", completed, True)
        check("a nonexistent root is not a complete scan",
              scan_projects(0.0, seen.extend, tmp / "does-not-exist"), False)

        real_deadline = SCAN_DEADLINE
        try:
            globals()["SCAN_DEADLINE"] = -1.0  # blown from birth
            check("a blown deadline does not declare itself complete",
                  scan_projects(time.time() - SEVEN_DAYS, lambda e: None, tree), False)

            # The budget can also blow INSIDE the reading of the last file - then
            # the loop ends without passing any check, and only the test at the
            # end stops the slowest round of all from calling itself complete.
            globals()["SCAN_DEADLINE"] = 0.05

            def slow(entries):
                time.sleep(0.12)

            check("a deadline blown during the reading counts too",
                  scan_projects(time.time() - SEVEN_DAYS, slow, tree), False)
        finally:
            globals()["SCAN_DEADLINE"] = real_deadline

        # A file inside the window that cannot be read = a PARTIAL scan. The
        # failure is injected instead of manufactured on disk: file permissions
        # do not behave the same on Windows and on Unix, and a test that only
        # bites on one of them is no good for a script that promises both.
        real_usage = globals()["usage_entries"]
        try:
            def failing_usage(path, start=None):
                if path.name == "session.jsonl":
                    raise OSError("simulating an unreadable transcript")
                return real_usage(path, start)

            globals()["usage_entries"] = failing_usage
            check("an unreadable file makes the scan partial",
                  scan_projects(time.time() - SEVEN_DAYS, lambda e: None, tree), False)
        finally:
            globals()["usage_entries"] = real_usage

        # An unreadable directory: `os.walk` swallows the error if nobody passes
        # `onerror`, and the scan would declare itself complete having seen less
        # than what is there. The error is injected here too - directory
        # permissions do not behave the same on both systems either.
        real_walk = os.walk
        try:
            def failing_walk(top, onerror=None, **kwargs):
                if onerror is not None:
                    onerror(OSError("simulating an unreadable directory"))
                return iter(())

            os.walk = failing_walk
            check("an unreadable directory makes the scan partial",
                  scan_projects(time.time() - SEVEN_DAYS, lambda e: None, tree), False)
        finally:
            os.walk = real_walk

        # The REAL `cost_shares`, with the scan faked and the cache in tmp.
        # Swapping `cost_shares` for a lambda (as the arithmetic tests do) leaves
        # this whole path with no execution at all - that is how a NameError in
        # the cache write once slipped past a green selftest.
        real_scan, real_state = globals()["scan_projects"], globals()["state_file"]
        try:
            globals()["state_file"] = lambda name: tmp / name

            def fake_scan(start, accumulate, entries=None, complete=True):
                accumulate(entries or [])
                return complete

            # Noon today, or now if noon has not come yet: a fixture standing for
            # "spent today" should be an instant that already happened, and on any
            # machine still in the morning a plain noon is in the future.
            now = datetime.now()
            today_iso = min(now.replace(hour=12), now).isoformat()
            populated = [
                {"timestamp": today_iso,
                 "message": {"model": "claude-fable-5",
                             "usage": {"input_tokens": 1_000_000}}},
                {"timestamp": today_iso,
                 "message": {"model": "claude-haiku-4-5",
                             "usage": {"input_tokens": 1_000_000}}},
            ]
            globals()["scan_projects"] = lambda s, acc: fake_scan(s, acc, populated)
            (tmp / WEEK_CACHE_FILE).unlink(missing_ok=True)
            result = cost_shares(time.time() - SEVEN_DAYS)
            check("the real cost_shares returns Shares", isinstance(result, Shares), True)
            # Fable $10/M against Haiku $1/M, both today: 10/11 of the cost.
            check("fable's share comes from the weighted cost",
                  round(result.fable, 3) if result else None, 0.909)
            check("today's spending is the whole total", round(result.day_total, 3) if result else None, 1.0)
            check("the cache was written", (tmp / WEEK_CACHE_FILE).is_file(), True)

            # The second call has to come from the cache, without scanning again.
            globals()["scan_projects"] = lambda s, acc: fake_scan(s, acc, [])
            check("the second call reuses the cache", cost_shares(time.time() - SEVEN_DAYS), result)

            # A scan with no sample and an incomplete scan: None, not zero.
            (tmp / WEEK_CACHE_FILE).unlink(missing_ok=True)
            check("an empty scan does not turn into zero", cost_shares(time.time() - SEVEN_DAYS), None)
            globals()["scan_projects"] = lambda s, acc: fake_scan(s, acc, populated, complete=False)
            check("an incomplete scan with no cache is None too",
                  cost_shares(time.time() - SEVEN_DAYS), None)
        finally:
            globals()["scan_projects"], globals()["state_file"] = real_scan, real_state
    finally:
        # Scope of the rmtree: `tmp` is the directory THIS process just created
        # with `mkdtemp`, never a path received from outside.  risky-scan: ignore
        shutil.rmtree(tmp, ignore_errors=True)

    # Share of the window elapsed, from the payload's resets_at.
    now = time.time()
    check(
        "middle of the 5h window",
        round(elapsed_fraction({"rate_limits": {"five_hour": {"resets_at": now + 2.5 * 3600}}},
                               "five_hour"), 2),
        0.5,
    )
    check(
        "day 5 of 7",
        round(elapsed_fraction({"rate_limits": {"seven_day": {"resets_at": now + 2 * 86400}}},
                               "seven_day"), 2),
        0.71,
    )
    check("with no resets_at there is no share", elapsed_fraction({}, "seven_day"), None)
    check("the day's share is inside the interval", 0.0 <= day_elapsed_fraction() <= 1.0, True)

    # An implausible deadline is not a deadline: it becomes "no deadline" instead
    # of becoming a false measurement.
    def reset(window, value):
        return valid_reset({"rate_limits": {window: {"resets_at": value}}}, window)

    check("a reset in the past does not count", reset("five_hour", now - 99999), None)
    check("a reset beyond the window does not count", reset("seven_day", now + 99 * 86400), None)
    check("a 5h reset with a week's deadline does not count", reset("five_hour", now + 6 * 3600), None)
    check("a zeroed reset does not count", reset("seven_day", 0), None)
    check("a NaN reset does not count", reset("seven_day", float("nan")), None)
    check("an infinite reset does not count", reset("seven_day", float("inf")), None)
    check("a boolean reset does not count", reset("seven_day", True), None)
    check("a string reset does not count", reset("seven_day", "tomorrow"), None)
    check("a reset inside the window counts", reset("seven_day", now + 3 * 86400) is not None, True)
    check(
        "a just-expired reset still counts (the payload lags at the reset)",
        reset("five_hour", now - 10) is not None,
        True,
    )
    check(
        "an absurd reset does not become a timer",
        reset_in({"rate_limits": {"seven_day": {"resets_at": now + 99 * 86400}}}, "seven_day"),
        "",
    )

    # With no valid weekly reset there is no way to ration by day: the derived
    # fields disappear instead of showing a guessed number.
    no_reset = {"rate_limits": {"seven_day": {"used_percentage": 40.0}}}
    check("no weekly reset means no window", weekly_window(no_reset), None)
    check("no weekly reset means no fable cap", fable_cap_percent(no_reset), None)
    check("no weekly reset means no daily total cap", daily_total_percent(no_reset), None)
    check("the official weekly survives without a reset", usage_percent(no_reset, "seven_day"), 40.0)

    # Reset day: the quota-day is clipped by the week on both ends. Fixed numbers
    # (h = an hour of the day in seconds), without depending on the test's hour.
    h = 3600
    check("calendar day, noon", elapsed_share(12 * h, 0, 24 * h), 0.5)
    # Reset at 12:00: at 11:00 there are 60min left of the quota-day, not 13h.
    check("eve of the reset: 11:00 of a day that closes at noon",
          round(elapsed_share(11 * h, 0, 12 * h), 3), 0.917)
    check("without the clipping the same hour would give 0.458",
          round(elapsed_share(11 * h, 0, 24 * h), 3), 0.458)
    # After the 12:00 reset, the new quota-day starts there.
    check("post-reset: 13:00 is the start of the new day",
          round(elapsed_share(13 * h, 12 * h, 24 * h), 3), 0.083)
    check("the share never goes past 1", elapsed_share(30 * h, 0, 24 * h), 1.0)
    check("the share never goes negative", elapsed_share(-5 * h, 0, 24 * h), 0.0)
    check("a null interval does not divide by zero", elapsed_share(12 * h, 12 * h, 12 * h), 1.0)
    check("an inverted interval does not divide by zero", elapsed_share(12 * h, 24 * h, 0), 1.0)

    # The calendar day swept across a whole year, with the clock injected. Under
    # the developer's own timezone this is trivial (every day has 24h), but CI
    # runs this same battery under timezones WITH daylight saving - including one
    # where the switch happens at MIDNIGHT (Brazil pre-2019, Chile, Cuba, Iran),
    # where local midnight simply does not exist. `day_start` is the denominator
    # of the daily cap: a one-hour error there skews the whole day's projection.
    for month in range(1, 13):
        for day_of_month in (1, 15, 28):
            ref = datetime(2026, month, day_of_month, 12, 0)
            start_of_day, end_of_day = day_start(ref), day_end(ref)
            label = f"2026-{month:02d}-{day_of_month:02d}"
            # Adding 86400 would put the end of the day at 1 a.m. or 11 p.m. on a
            # transition day.
            if datetime.fromtimestamp(end_of_day).hour != 0:
                failures.append(f"end of day {label} did not land on midnight")
            if end_of_day <= start_of_day:
                failures.append(f"day {label} has no positive duration")
            if not 22 * 3600 <= end_of_day - start_of_day <= 26 * 3600:
                failures.append(
                    f"day {label} has an absurd duration: "
                    f"{(end_of_day - start_of_day) / 3600}h"
                )
            if start_of_day > ref.timestamp():
                failures.append(f"start of day {label} landed after the instant itself")

    midnight = day_start()
    check(
        "outside the reset day the whole calendar day counts",
        round(day_elapsed_fraction(midnight - 3 * 86400, midnight + 4 * 86400), 4),
        round(day_elapsed_fraction(), 4),
    )
    check(
        "clipping by the reset never shrinks the share",
        day_elapsed_fraction(midnight - 6 * 86400, midnight + 86400 / 2)
        >= day_elapsed_fraction(),
        True,
    )

    # Numbers from the payload: bool, NaN and infinity must not become measurements.
    check("NaN does not become a full quota",
          usage_percent({"rate_limits": {"seven_day": {"used_percentage": float("nan")}}},
                        "seven_day"), None)
    check("infinity does not become a full quota",
          usage_percent({"rate_limits": {"seven_day": {"used_percentage": float("inf")}}},
                        "seven_day"), None)
    check("a boolean does not become 1%",
          usage_percent({"rate_limits": {"seven_day": {"used_percentage": True}}},
                        "seven_day"), None)
    check("NaN does not become context",
          context_percent({"context_window": {"used_percentage": float("nan")}}), None)
    check("finite_number rejects bool", finite_number(True), None)
    check("finite_number accepts int", finite_number(7), 7.0)

    # Nothing from outside may carry a terminal command into the bar.
    check("ESC leaves the text", safe_text("a\x1b[31mb"), "a[31mb")
    check("BEL leaves the text", safe_text("a\x07b"), "ab")
    check("NUL leaves the text", safe_text("a\x00b"), "ab")
    check("DEL leaves the text", safe_text("a\x7fb"), "ab")
    check("C1 leaves the text", safe_text("a\x9bb"), "ab")
    check("CR and LF leave the text", safe_text("a\r\nb"), "ab")
    check("TAB leaves the text", safe_text("a\tb"), "ab")
    check("long text is truncated", len(safe_text("x" * 500)), 120)
    check("an explicit limit is respected", len(safe_text("x" * 500, 20)), 20)
    check("accents survive", safe_text("implementação"), "implementação")
    check("a non-string becomes empty", safe_text(7), "")
    # These do not look like control characters and mess with the layout just the same.
    check("the unicode line separator leaves", safe_text("a b"), "ab")
    check("the unicode paragraph separator leaves", safe_text("a b"), "ab")
    check("the bidi override leaves", safe_text("a‮b"), "ab")
    check("the bidi isolate leaves", safe_text("a⁦b"), "ab")
    check("an emoji is not a control character and stays", safe_text("ok ✓"), "ok ✓")
    # All THREE text fields of the payload go through the filter, not just the name.
    for field, payload in (
        ("session name", {"session_name": "x\x1b[31my"}),
        ("model name", {"model": {"id": "claude-opus-5", "display_name": "x\x1b[31my"}}),
        ("effort level", {"model": {"id": "claude-opus-5", "display_name": "M"},
                          "effort": {"level": "x\x1b[31my"}}),
    ):
        out = render(payload)
        check(f"ESC does not get through the {field}", "\x1b[31m" in out, False)
        check(f"and the bar gains no extra line from the {field}", out.count("\n"), 0)

    # An invalid nested type must not wipe out the ENTIRE bar.
    for case_label, payload in (
        ("model string", {"model": "bad", "rate_limits": {"five_hour": {"used_percentage": 10.0}}}),
        ("numeric model.id", {"model": {"id": 7, "display_name": 9}}),
        ("numeric effort", {"model": {"id": "claude-opus-5"}, "effort": {"level": 7}}),
        ("loose effort string", {"effort": "high"}),
        ("rate_limits string", {"rate_limits": "nothing"}),
        ("context_window list", {"context_window": []}),
    ):
        try:
            out = render(payload)
            check(f"render survives {case_label}", isinstance(out, str), True)
        except Exception as exc:  # noqa: BLE001 - the test is precisely not to leak
            failures.append(f"render blew up on {case_label}: {exc!r}")
    check(
        "a crooked payload does not erase what was valid",
        "10.0%" in render({"model": "bad", "rate_limits": {"five_hour": {"used_percentage": 10.0}}}),
        True,
    )
    # Arithmetic of the daily caps, with the shares pinned (without touching disk).
    real_shares = globals()["cost_shares"]

    def fable_payload(weekly, days_to_reset=5):
        return {
            "rate_limits": {
                "seven_day": {
                    "used_percentage": weekly,
                    "resets_at": day_start() + days_to_reset * 86400,
                }
            }
        }

    def patch_shares(share, share_day, share_day_all=0.0):
        # It has to be `Shares`, not a raw tuple: the consumers read by field
        # NAME, and a tuple would pass the render's `isinstance(..., tuple)` only
        # to then blow up with an AttributeError inside `safe()` - the field
        # would silently disappear from the bar and the test would stay green
        # measuring nothing.
        globals()["cost_shares"] = lambda start: Shares(share, share_day, share_day_all)

    def caps(share, share_day, weekly, days=5):
        patch_shares(share, share_day)
        return fable_cap_percent(fable_payload(weekly, days))

    def total_cap(share_day_all, weekly, days=5):
        patch_shares(0.0, 0.0, share_day_all)
        return daily_total_percent(fable_payload(weekly, days))

    real_calibration = FABLE_SHARE_CALIBRATION
    try:
        # BOTH Fable fields, not just the weekly one: with the factor applied to
        # only one of them the bar would show a calibrated number next to a raw
        # one, and checking the weekly alone still passed (a mutant that escaped
        # on the first round).
        raw_weekly, raw_daily = 40.0, 200.0  # this fixture, uncalibrated
        check(
            "the calibration reaches fable's weekly cap",
            round(caps(0.50, 0.50, 40.0)[0], 3),
            round(raw_weekly * real_calibration, 3),
        )
        check(
            "the calibration reaches fable's daily cap",
            round(caps(0.50, 0.50, 40.0)[1], 3),
            round(raw_daily * real_calibration, 3),
        )
        # From here down, factor 1.0. The checks below are about the MECHANICS of
        # the rationing (balance at the start of the day, days left, cap): an
        # empirical factor in the middle would make the arithmetic in the comments
        # impossible to verify by eye, and would make every re-calibration break a
        # test that is not about calibration at all.
        globals()["FABLE_SHARE_CALIBRATION"] = 1.0

        # 0.50 * 40 = 20 points out of 50 -> 40% of the weekly cap.
        # Nothing spent before today: balance 50 / 5 days = cap 10; spent today 20 -> 200%.
        week, day = caps(0.50, 0.50, 40.0)
        check("fable's weekly cap", round(week, 1), 40.0)
        # Blowing through the Fable cap has to SHOW: 0.8 x 90 = 72 points against
        # a 50-point cap is 144%, not "100% and that's it".
        check("fable's weekly cap is not capped at 100",
              round(caps(0.80, 0.10, 90.0)[0], 1), 144.0)
        check("daily cap with a clean week", round(day, 1), 200.0)
        # Same spending today (4 points), clean week: balance 50/5 = 10 -> 40%.
        check("a slack daily cap", round(caps(0.10, 0.10, 40.0)[1], 1), 40.0)
        # Blowing the previous days squeezes today: 45 spent, of which 5 today ->
        # balance at the start of the day = 10, /5 days = cap 2; spent 5 -> 250%.
        check("a blown previous day narrows today", round(caps(0.90, 0.10, 50.0)[1], 1), 250.0)
        # Eve of the reset: the day inherits all the remaining balance (days = 1).
        check("the last day inherits the whole balance", round(caps(0.20, 0.20, 50.0, 1)[1], 1), 20.0)
        check("the daily cap is not clamped at 100", caps(0.90, 0.90, 90.0)[1] > 100, True)
        check("with no balance the day is born at 100%", round(caps(1.0, 0.0, 100.0)[1], 1), 100.0)
        check("no weekly quota means no fable cap", fable_cap_percent({}), None)

        # Daily cap of the TOTAL quota: same rationing, cap 100 and no model filter.
        # The whole week spent today: spent 40, balance 100 / 5 days = cap 20 -> 200%.
        check("daily total cap with a clean week", round(total_cap(1.0, 40.0), 1), 200.0)
        # Today = 1/4 of the week: spent 10, before 30, balance 70 / 5 = cap 14 -> 71.4%.
        check("daily total cap inside its share", round(total_cap(0.25, 40.0), 1), 71.4)
        # Eve of the reset: the day inherits the whole balance (days = 1).
        check("daily total cap on the last day", round(total_cap(0.20, 50.0, 1), 1), 16.7)
        check("the daily total cap is not clamped at 100", total_cap(0.90, 90.0) > 100, True)
        check("blown quota: the day total is born at 100%", round(total_cap(0.0, 100.0), 1), 100.0)
        check("no weekly quota means no daily total cap", daily_total_percent({}), None)

        # Render: both daily caps, in order and with each scale's colors.
        patch_shares(0.50, 0.50, 0.25)
        day_line = render(
            {
                "rate_limits": {
                    "five_hour": {"used_percentage": 15.0},
                    "seven_day": {
                        "used_percentage": 40.0,
                        "resets_at": day_start() + 5 * 86400,
                    },
                }
            }
        ).split("\n")[-1]  # payload with no model: only the quota line comes out
        # Both daily caps are painted by pace against the end of the calendar day
        # - the expectation comes from the same functions so it does not depend
        # on the test's hour.
        day_frac = day_elapsed_fraction()
        fable_day_txt = paint("200.0%", fable_sgr(200.0, day_frac, PACE_FLOOR_DAY))
        total_day_txt = paint("71.4%", pace_sgr(71.4, day_frac, floor=PACE_FLOOR_DAY, hard=None))
        check("fable's cap on line 2", fable_day_txt in day_line, True)
        check("the day's total cap on line 2", total_day_txt in day_line, True)
        check(
            "the day's total cap sits to the RIGHT of fable's",
            day_line.index(fable_day_txt) < day_line.index(total_day_txt),
            True,
        )
        check(
            "the two daily caps separated by ·",
            f"{fable_day_txt}  {paint('·', C_SEP_ITEM)}  {total_day_txt}" in day_line,
            True,
        )
        check(
            "the day's total cap uses the weekly total's scale",
            scale_sgr(71.4) == scale_sgr(71.4, SCALE, SCALE_ALERT),
            True,
        )
        # Without fable's cap, the day total stays alone in the block, no loose "·".
        only_total = render(
            {
                "rate_limits": {
                    "seven_day": {
                        "used_percentage": 40.0,
                        "resets_at": day_start() + 5 * 86400,
                    }
                }
            }
        )
        check("without both there is no loose ·", f"{paint('·', C_SEP_ITEM)}  {paint('·', C_SEP_ITEM)}" in only_total, False)

        # The weekly block: the SAME order as the day block - Fable on the left,
        # total quota on the right. Distinct shares on purpose: with both fields
        # landing on the same number (the `day_line` case above, 40.0% on both)
        # `index` would find the same occurrence twice and the check would pass
        # with either order. Here 0.25 x 40 = 10 points out of 50 -> 20.0% of
        # Fable's cap, against the 40.0% of the total quota.
        patch_shares(0.25, 0.0, 0.0)
        week_line = render(
            {
                "rate_limits": {
                    "seven_day": {
                        "used_percentage": 40.0,
                        "resets_at": day_start() + 5 * 86400,
                    }
                }
            }
        ).split("\n")[-1]
        check("fable's weekly cap on line 2", "20.0%" in week_line, True)
        check("the weekly total quota on line 2", "40.0%" in week_line, True)
        check(
            "the weekly total sits to the RIGHT of fable's cap",
            week_line.index("20.0%") < week_line.index("40.0%"),
            True,
        )
    finally:
        globals()["cost_shares"] = real_shares
        globals()["FABLE_SHARE_CALIBRATION"] = real_calibration
    # The block above runs with the calibration neutralized; if the `finally`
    # stopped restoring the real value, every check AFTER it would measure with a
    # factor of 1.0 and nothing would flag it. A fixture leak does not show up as
    # an error - only as a test that agrees with its own patch (a mutant that
    # escaped on the first round).
    check("the calibration returns to its real value on leaving the block",
          FABLE_SHARE_CALIBRATION, real_calibration)
    check("local midnight", datetime.fromtimestamp(day_start()).hour, 0)

    accented = '{"session_name": "implementação"}'.encode("utf-8")
    check("a UTF-8 payload preserves the accent", load_payload(accented).get("session_name"), "implementação")
    check("an empty payload becomes a dict", load_payload(b""), {})
    check("an invalid payload becomes a dict", load_payload(b"not json"), {})
    check("a non-object payload becomes a dict", load_payload(b"[1,2]"), {})
    check(
        "a cp1252 payload does not take it down",
        load_payload('{"session_name": "cão"}'.encode("cp1252")).get("session_name") is not None,
        True,
    )
    check("an empty payload does not break", isinstance(render({}), str), True)
    check("an empty payload invents no second line", "\n" in render({}), False)
    two_lines = render(
        {
            "model": {"id": "claude-opus-4-8[1m]", "display_name": "Opus 4.8 (1M context)"},
            "effort": {"level": "medium"},
            "rate_limits": {
                "five_hour": {"used_percentage": 15.0},
                "seven_day": {"used_percentage": 35.0},
            },
        }
    )
    check("session and quotas on separate lines", two_lines.count("\n"), 1)
    check("the model on line 1", "Opus 4.8" in two_lines.split("\n")[0], True)
    check("the 5h quota on line 2", "15.0%" in two_lines.split("\n")[1], True)
    line2 = two_lines.split("\n")[1]
    check("the rule opens line 2", line2.startswith(paint(QUOTA_DASHES, C_SEP_BLOCK) + " "), True)
    check("the rule closes line 2", line2.endswith(" " + paint(QUOTA_DASHES, C_SEP_BLOCK)), True)
    check("line 1 gains no rule", QUOTA_DASHES in two_lines.split("\n")[0], False)
    check("with no quotas there is no loose rule", QUOTA_DASHES in render({}), False)

    for line in failures:
        print(f"FAIL {line}")
    print(f"{'FAILED' if failures else 'OK'} - selftest ({ran} checks, {len(failures)} failure(s))")
    return 1 if failures else 0


# ---------------------------------------------------------------------- main


def main() -> int:
    # As a subprocess (no console) Python falls back to the ANSI codepage - on
    # Windows this emits the "·" as a raw 0xb7 and the terminal shows garbage.
    # Force UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    argv = sys.argv[1:]
    if "--selftest" in argv:
        return selftest()
    if "--calibrate" in argv:
        return calibrate_command(argv[argv.index("--calibrate") + 1:])

    try:
        raw = sys.stdin.buffer.read()
    except (AttributeError, OSError):  # stdin replaced/closed
        raw = (sys.stdin.read() or "").encode("utf-8", errors="replace")
    data = load_payload(raw)

    # The last payload received, to diagnose which field Claude Code sends (or
    # does not send) without instrumenting anything on the spot. Only with
    # CLAUDE_STATUSLINE_DEBUG on: the payload carries the session name and paths,
    # and writing that to disk on every refresh is the user's decision, not a
    # default. Fails silently.
    # An allow-list, not a deny-list. The bare `os.environ.get(...)` took the
    # string "0" as true and wrote the dump on the very command meant to stop
    # it; a deny-list fixed that direction but still opened on anything it did
    # not recognise - `=nao`, `=disabled`, a typo. What this switch turns on is
    # a file holding the session name and paths from the disk, rewritten every
    # refresh, so the unrecognised value has to mean OFF.
    if os.environ.get(DEBUG_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            write_private(
                state_file(DEBUG_DUMP), json.dumps(data, indent=2, ensure_ascii=False)
            )
        # Not just OSError. With `indent`, json uses the pure-Python encoder,
        # which recurses far deeper per level than the C scanner in `loads` -
        # so there is a band of nesting that survives being parsed and blows up
        # being written back out. Diagnostics must never be the thing that
        # takes the bar down.
        except Exception:
            pass

    try:
        # A last line holding one space (not empty, otherwise Claude Code drops
        # it) just to keep the bar away from the prompt/indicator right below.
        print(render(data) + "\n ")
    except Exception:
        print("")  # the status line can never take the session down
    return 0


if __name__ == "__main__":
    sys.exit(main())
