# statusline.py - a two-line status line for Claude Code

[![portability](https://github.com/capitulinojr/claude-statusline/actions/workflows/portability.yml/badge.svg)](https://github.com/capitulinojr/claude-statusline/actions/workflows/portability.yml)

Leia em [português](README.pt-BR.md).

![The status line: top line with model, effort, session name, context and tokens; bottom line with the 5-hour, daily and weekly quotas](docs/statusline.png)

```
<model> <effort> | <session(italic)> · <ctx%> <session-tokens>
------- <5h%> · <5h-reset> | <fable-day%> · <day-total%> | <fable%> <week%> <weekly-reset> -------
```

The top line is this session. The bottom line is your account quota. There is a
third line holding a single space, so the bar doesn't touch the prompt right
below it.

No labels. Position and color carry the meaning. You read it at a glance: which
model am I on, how much context is left, how much quota did I burn.

One file, stdlib only, no dependencies. It reads JSON from stdin and prints two
lines. That is the entire architecture.

---

## Install

You need Python 3.8+ on your PATH. Nothing else.

### Fast: have Claude Code install it

Download `statusline.py` and paste this into a session:

> Install this status line: put the `statusline.py` I downloaded in `~/.claude/`,
> add the `statusLine` block to my `~/.claude/settings.json` pointing at its
> absolute path with `python -S -E`, and run `--selftest` to confirm. Show me the
> settings diff before you write it.

It knows the block format and finds `settings.json` on its own. Asking for the
diff isn't ceremony: that is your configuration file, and a careless merge drops
whatever was already in it.

### Manual: three steps

1. Save `statusline.py` wherever you like (e.g. `~/.claude/statusline.py`).

2. In `~/.claude/settings.json`, add:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python -S -E \"C:\\full\\path\\statusline.py\"",
    "padding": 0,
    "refreshInterval": 15
  }
}
```

On macOS/Linux use `python3 -S -E "/path/statusline.py"` instead. The `-S -E`
flags skip `site-packages` and the `PYTHON*` environment variables: it starts
faster, and a dirty `PYTHONPATH` can't break it.

3. Check it. `python statusline.py --selftest` prints
   `OK - selftest (0 failure(s))`.

---

## What each field is

**Line 1: the session**

![Line 1: Opus 5 (1M context), medium, Tuning the status line, 11.0%, 2.9M](docs/statusline-line1.png)

| Field | What |
| --- | --- |
| `Opus 5 (1M context)` | the model, colored by tier: Fable orange and **bold**, Opus cyan, Sonnet yellow, Haiku blue |
| `medium` | reasoning effort (`low` up to `ultracode`) |
| `Tuning the status line` | the session name, in italics: whatever you set with `/rename`, or the title Claude Code generated. The script strips control characters and truncates the text before it reaches your terminal |
| `11.0%` | the share of the whole window already occupied, the `context`. From 75% up it turns red and a `/compact` hint shows up beside it |
| `2.9M` | tokens this session accumulated (input + output + cache) |

**Line 2: the quotas**

![Line 2: 4.0%, 4h 47m, 48.5%, 21.0%, 37.1%, 32.0%, 4d 14h](docs/statusline-line2.png)

| Field | What |
| --- | --- |
| `4.0%` | the `5-hour` window quota |
| `4h 47m` | time left until the 5-hour reset |
| `48.5%` | the daily cap of the expensive model (see below) |
| `21.0%` | the daily cap of the total quota, same rationing, all models |
| `37.1%` | how much of the Fable cap (half the weekly quota) is gone |
| `32.0%` | the `weekly` window quota |
| `4d 14h` | time left until the weekly reset |

The last two blocks each hold a Fable number and a total number, and both use the
**same order**: `Fable · total` for the day, `Fable total` for the week. Same
column, same meaning - you don't flip your eye halfway through the line. A field
disappears from the bar when there is no data behind it, so the number of fields
varies. Position is relative to the `|` separators, never fixed.

### Thermometer: color from the consumption projected to the window's reset

`80%` of the weekly quota, on day 7, comes out yellow. The same `80%` on day 2
comes out red.

Same number, opposite situations. On day 7 the cycle is ending along with you
and it resets tomorrow, so the projection lands at ~86. On day 2 you burned the
whole week before halftime, the projection goes past 250, and the quota runs out
on Wednesday. A value-based scale paints both identically.

It's like the fuel gauge in a car. Half a tank 10 km from home doesn't mean what
half a tank means 400 km from home.

So every percentage that has a deadline (5-hour, day, week) is painted by the
consumption projected to the reset, not by the raw value:

```
projection = consumption ÷ share of the deadline already elapsed   (100 = lands exactly on the cap)
```

The thermometer (`SCALE_PACE`) runs gray, blue, green, yellow, orange, all
muted, and vivid red from a projection of 135 up. That is the pace blowing
through the cap with room to spare.

Three deliberate exceptions:

- `context` has no deadline, since it doesn't reset on its own. It stays on the
  value thermometer (`SCALE_CTX`): red from 75% up, with the `/compact` hint
  beside it.
- A real quota at or above `PACE_HARD` (95%) goes back to red regardless of
  pace. At that point the block arrives before the reset, and that is actionable
  even on the eve of it.
- **The two Fable fields have a scale of their own** (`SCALE_FABLE`) and never
  go gray, blue or green: muted orange throughout, vivid orange from a
  projection of 85, red from 100. Fable is the expensive tier and its cap is
  half the weekly quota, so the field is meant to be legible from the first
  point spent - not to blend into the bar until it's late. That is why the
  screenshot up top shows `37.1%` in red beside a `32.0%` in yellow: same week,
  same deadline, different rulers.

With no usable `resets_at` in the payload there is no deadline to measure
against. The field falls back to the value scale, and the fields that depend on
"how many days are left" disappear from the bar instead of being guessed.

---

## Fable's cap

The cap is official. What the script estimates is how much of it you already
spent.

Claude Code hands you the aggregate quota (`seven_day.used_percentage`) and
never says how much of it was the expensive model. Orchestrate with Fable and
execute with Sonnet and Haiku, and the aggregate number can't tell you whether
you are burning quota on the wrong tier.

So the script scans the transcripts (`~/.claude/projects/**/*.jsonl`) for the
current week. It prices every entry at that entry's own model rate, weighted:
input, output, cache-write and cache-read don't cost the same. Then it projects
the share onto the official aggregate:

```
fable_share  = fable_cost / total_cost              (over the 7 days)
fable_share  = fable_share × 0.894                  (measured calibration, below)
points_spent_by_fable = fable_share × seven_day.used_percentage
% of the cap = points ÷ 50 × 100                    (FABLE_CAP_SHARE = 50%)
```

`FABLE_CAP_SHARE = 0.50` is Anthropic's stated limit, checked 2026-07-29, not a
guess. On the Max and Team Premium plans, Fable 5 may consume up to half of your
weekly quota. Past that you either keep going on Fable with usage credits or
switch models to stay inside what's left. Hence the ruler: 100% in this field is
the point where Fable stops being included in the subscription. Plan terms
change, so check your own plan page before trusting the constant. If your plan
reads differently (standard seats on Team and Enterprise, for instance, where
Fable runs on credits rather than on an included share) adjust the constant at
the top of the file.

### Calibration against the official number

API price is not quota weight. The raw share comes out high enough to paint the
bar the wrong color, so it is multiplied by `FABLE_SHARE_CALIBRATION` before it
becomes a percentage of the cap.

**The value in use is 0.894**, the average of two measurements against the
official number:

| measurement | raw share | official | factor |
| --- | --- | --- | --- |
| 2026-07-30, morning | 51.33% | 84% of 92% | 0.889 |
| 2026-07-30, afternoon | 50.88% | 86% of 94% | 0.899 |

Each one carries a margin of ~±0.01, all of it from rounding: the usage screen
serves integers, so "84%" is anything between 83.5 and 84.5. The two factors land
inside that margin of each other.

Without the correction the bar reads about ten points above the official number -
in the morning measurement, 94.4% of the cap against the real 84%. The deviation
is always upward, so the bar cries wolf early.

**What the factor absorbs.** Two causes, which one measurement can't separate:

1. Fable's weight against the quota being lower than the price ratio (today 2×
   Opus);
2. usage that counts against the quota but leaves no local transcript - claude.ai
   web, Cowork.

The second has no guaranteed direction. Absent usage only inflates the share if it
is *less* Fable-heavy than the local one; being more Fable-heavy, the local share
understates; and with the same mix, it biases nothing. The same factor absorbs
both while the proportions stay stable, but they don't necessarily point the same
way. Hence the correction living on the **share** rather than on the price: that
way it doesn't claim which of the two it is.

**Where the official number lives:** Claude Code doesn't send it in the payload
(only `five_hour` and `seven_day`), but claude.ai shows it under **Settings >
Usage**, and the API behind that screen (`GET /api/organizations/<org>/usage`)
returns all three limits in the `limits` array - the `weekly_all` entry is the
total quota, and the `weekly_scoped` one with `scope.model.display_name: "Fable"`
is Fable's cap.

### Fable's daily cap amplifies the error

The **weekly** field responds proportionally to the factor. The **daily** one does
not - and that isn't an effect of the calibration, it's the shape of the
rationing. The daily cap divides the **balance** (`50 − what Fable spent before
today`), and near the cap that balance is the difference between two nearly equal
numbers:

| factor | share | weekly | daily |
| --- | --- | --- | --- |
| 1.000 (raw) | 50.8% | 93.4% | **89.2%** |
| 0.920 | 46.7% | 86.0% | 51.4% |
| **0.894** (in use) | 45.4% | **83.5%** | **44.6%** |
| 0.800 | 40.6% | 74.7% | 28.7% |

Measured with the week at 92% and **two days until the reset** - the day count
feeds the cap calculation, so it is part of the measurement. One point of error in
the share moves the weekly ~1.8 points and the daily by tens. **Read the daily as an order of magnitude, not
as a measurement.** Leaving it raw next to a calibrated weekly would be worse - it
would mix two rulers on the same bar.

> **The calibration holds for the weighting Anthropic applied on 2026-07-30.** It
> is not a constant of nature - they can re-weight the quota whenever they want,
> and the day they do, the factor is wrong with nothing to flag it. Nothing in the
> script detects that aging; only re-measuring does.

**To re-measure,** read both numbers off the usage screen, at the same moment, and
run:

```bash
python statusline.py --calibrate 92 84    # <all%> <fable%>
```

It calibrates against the cached weekly share, so it needs the bar to have
rendered in the last ~20 minutes, on the same day. Otherwise it refuses and says
so, instead of calibrating against a stale share - open a session, let the bar
draw once, and run it again.

It compares them against this machine's raw share and prints the
`FABLE_SHARE_CALIBRATION` that matches. A factor of `1.0` turns the correction off
and restores the old behavior.

### The daily cap moves

Burn two days' worth on a Tuesday and Wednesday's cap is born smaller, so the
percentage climbs faster. Blowing today's budget doesn't change today's number.
It narrows the following days.

```
today's_cap = (50 points − what Fable spent BEFORE today) ÷ days until the reset (counting today)
```

This was the fiddly part, and it's what makes the number useful. Three details:

- The cap is **fixed** at midnight, not recomputed on every spend. If it shrank
  along with consumption, the ruler would never reach 100%.
- The reset day counts whole. On the eve, the day inherits all the remaining
  balance: what's left is spendable until the reset hour, and rationing it by
  fraction would make no sense.
- It is not capped at 100%. Going past the day's share is legitimate and has to
  show. Wake up with no balance at all and the day is born at 100%.

The color of these two fields comes from pace too, measured against the end of
the day. On the weekly reset day the "day" is clipped by the week, not by
midnight: if the reset is at noon, the morning quota-day ends at 12:00 and the
afternoon one starts at 12:00. Without that clipping, 80% of the cap at 11:00
would project to 175% (red) instead of ~87%. The clipping matches the
measurement, which likewise only sees spending from the start of the week.

**Why estimate instead of reading the official number?** Because it doesn't
exist in the payload. The [status line
docs](https://code.claude.com/docs/en/statusline) expose only
`rate_limits.five_hour` and `rate_limits.seven_day`, with no per-model breakdown
(checked 2026-07-26; if one ever shows up, replace the estimate with it). The
real weighting can't be reconstructed either: Anthropic publishes the quota in
model hours with wide ranges (Max 5x: 15-35h of Opus per week), never as
per-token weight.

So the cap behind both numbers is official, and the position inside it is a
calibrated estimate. A compass, not accounting.

What's left of the error after the calibration has a known cause that no constant
solves: Anthropic rations by model hours, the script weighs by dollar cost. The
factor pins the two together at one operating point, and that is all it does - it
doesn't turn one proxy into the other. Change the model mix enough and the gap
opens again.

Claude Code's own usage panel (`/usage`) shows the official Fable number, the one
the payload doesn't hand over. Want the exact value, it's there. The bar is so you
don't have to look.

Two payload details worth knowing for any status line. `rate_limits` only shows
up for Pro/Max subscribers, and only after the first API response in the
session. Each window can go missing on its own, too. That is why everything here
goes through `safe()`. And `context_window.used_percentage` counts input tokens
only (`input + cache_creation + cache_read`, no `output_tokens`); the script's
fallback uses the same formula so it won't drift from the official number.

Don't use the expensive model, or want the official quotas and nothing else?
Make `fable_cap_percent()` and `daily_total_percent()` return `None` on their
first line. All three fields disappear and the transcript scan stops running.
Deleting the fields from `render()` is not enough: both functions would already
have been called, and they are what triggers the scan. What drives the cost is
the call, not the display.

---

## Tuning

Everything you'd want to change lives in the constants at the top, each with a
comment:

- `MODEL_COLORS`, `EFFORT_COLORS`: per model and per effort colors (xterm-256).
- `SCALE_PACE`, `SCALE_CTX`, `SCALE_FABLE`: the thermometers. Each is a tuple of
  `(exclusive upper bound, SGR code)` plus one alert color above everything. The
  number fed into `SCALE_PACE` is the **projection**, not the consumption, so
  moving its thresholds means moving "how far off pace counts as off pace".
  `SCALE` is only the fallback for when the payload carries no deadline.
- `PACE_FLOOR_WINDOW` (0.15) and `PACE_FLOOR_DAY` (0.35): floor for the
  projection's denominator. Early in a window, extrapolating is noise: 2% spent
  over 1% of the time would project 200%. Before that point the window counts as
  if the floor had already elapsed. The day's floor is higher because the
  calendar day starts at midnight and human usage does not.
- `PACE_HARD` (95): above this, a real quota goes back to being painted by
  value.
- `RESET_TOLERANCE` (300 s): slack for accepting a just-expired `resets_at`,
  since the payload takes a few seconds to roll over after a reset.
- `SCAN_DEADLINE` (8 s): time budget for the transcript scan. Past it, this
  round's estimate is dropped and the last cache stands, even if stale.
- `PRICES`: USD per 1M tokens, matched by model id prefix. **Check** the pricing
  tables before trusting the number; the ones in the file were verified in July
  2026. Matching is by prefix, so entry order matters.
- `FABLE_CAP_SHARE`: the share of the weekly quota Fable may occupy (0.50 = the
  official limit on Max/Team Premium).
- `FABLE_SHARE_CALIBRATION`: empirical correction of Fable's estimated share, the
  average of two measurements against the official number on 2026-07-30 (0.894).
  Re-measure with `--calibrate`; `1.0` turns it off.
- `CTX_HINT`: the text that shows up once context passes 75%.
- `QUOTA_DASHES`: the little rule that opens and closes the second line.

---

## Limits

The projection is linear and human usage comes in bursts, so it reads
pessimistic in the morning and optimistic in the small hours. The floors only
cut the numeric blow-up at the start of a window; inside them the color doesn't
move with the clock. Calibrating a curve by active hours would need usage
history.

`PACE_HARD` is a step, not a gradient. 94.9% projecting 95.9 comes out yellow
and 95.0% goes red on the spot. That is deliberate: near the block, the absolute
value takes the color back.

The script samples the clock several times per render (`time.time()`,
`day_start()`). A render that crosses midnight can mix one day's share with the
other's cost. One crooked bar per day, until the next refresh.

An entry dated ahead of the clock counts as today's. That is a choice, not an
oversight: refusing them was tried and it read worse. A refused entry still
counts in the week's total while leaving the day's, so a machine running a few
minutes fast - or transcripts synced from one - pushed both daily fields toward a
measured `0.0%` next to a weekly quota of 68%. Counting them overstates the daily
cap, which is the loud, conservative error. Refusing them understates it, which
is the quiet one, and on a gauge the quiet error is the one that lets you blow
through the limit thinking you're fine.

`SCAN_DEADLINE` is checked between files, not inside one. A single very large
`.jsonl` is read to the end before the budget is looked at again, so the 8
seconds are a target, not a ceiling. And when the budget does blow with no cache
to fall back on, the partial work is dropped rather than saved: the three
derived fields stay away and the next render starts over. Enough history on a
slow enough disk and that becomes the steady state - the bar keeps working, the
three Fable and daily fields just never show up. If that is where you are, the
escape hatch is the one in the section above: make `fable_cap_percent()` and
`daily_total_percent()` return `None` and the scan stops running at all.

What it does guarantee: it never takes the session down. Every piece of the bar
runs inside a `safe()`, and whatever fails becomes an empty string and
disappears without leaving a hole. An empty or invalid payload prints a blank
line instead of a stack trace.

---

## Implementation details worth knowing

- **The payload comes from outside, so the script treats it as hostile.**
  `(x or {}).get(...)` only guards against `None`. A `"model": "bad"` sails
  through and blows up mid-render, wiping out the entire bar. Every nested
  access goes through `as_dict`/`as_text`, and every number through
  `finite_number`, which rejects `bool` (an `int` in Python) and `NaN`/`Infinity`
  (which `json.loads` accepts by default). `NaN` was the nastiest: it survives
  clamping and comes out the wrong side. `min(100.0, nan)` returns `100.0`, and
  that would have turned into a false "quota full" alert.
- **An implausible deadline is a missing deadline.** A `resets_at` in the past,
  zeroed, or 99 days out isn't a bad deadline, it's invalid data. Building a
  window on top of it manufactures a number that looks measured: the daily cap
  used to be born at a red 100% purely because the payload carried no reset and
  the code guessed "7 days left".
- **The same goes for transcripts.** They are another program's files, not a
  contract. Token counters arrive as strings, negatives or `NaN` just as easily,
  so the script sanitizes each one before it enters a sum. A single `NaN` would
  poison every later addition, silently.
- **Nothing written for the terminal is trusted.** The session name is written
  by someone else: you, or the model summarizing the conversation. Loose in a
  terminal, an `ESC` in there isn't a character, it's a command. It moves the
  cursor, retitles the window, opens an OSC-8 hyperlink. A lone `\n` would break
  the two-line layout that is this bar's whole contract.
- **UTF-8 on both sides.** Running as a subprocess, with no console attached,
  Python falls back to the system ANSI codepage: cp1252 on Windows. That
  corrupts the output (the `·` comes out as a raw byte) and the input (an
  accented session name turns into `implementaÃ§Ã£o`, a classic double-encode).
  So the script reads `sys.stdin.buffer` and decodes UTF-8 explicitly, and
  forces `sys.stdout.reconfigure(encoding="utf-8")`. If you touch that part,
  verify by **byte**, not by looking at the screen. And don't test through a
  PowerShell pipe: it re-encodes and fakes the result.
- **Reverse reading of the `.jsonl` files.** Transcripts get big. The script
  reads back to front in 256 KB blocks and stops as soon as it leaves the time
  window. Before opening any file it filters by `mtime`, so the weekly scan only
  touches what was modified inside the window.
- **The scan is recursive.** Subagent and workflow transcripts don't sit next to
  the session's own, they go into subfolders. Orchestrate with subagents and
  most of your consumption lives there: in one real measurement, 63% of the
  volume. Scanning only the top level inflated the expensive model's share,
  because the denominator lost precisely the cheap delegated work.
- **10-minute cache** for the weekly share, in
  `~/.claude/statusline-week-share.json`, invalidated when the day turns.
  Without it the scan would run on every refresh. It lives in your own directory
  rather than the system temp folder on purpose: on a shared machine `/tmp`
  belongs to everyone, a fixed name collides across users and opens the classic
  planted symlink vector. The write is atomic (temp file plus `os.replace`),
  because several Claude Code sessions render at the same time and write that
  same file.
- **Streaming deduplication.** Partial streaming entries would be counted twice.
  The script keeps only those with `stop_reason` filled in, plus the last one if
  it comes as `null`.
- **Transcript dates.** Timestamps carry a `Z` suffix, which
  `datetime.fromisoformat` only started accepting in Python 3.11. On 3.8 to 3.10
  that would turn every timestamp into `None` silently: the daily caps would sit
  at 0.0% with the whole bar looking like it works. The script normalizes the
  suffix and keeps a `strptime` fallback.

### Diagnostics

Set `CLAUDE_STATUSLINE_DEBUG=1` and every render writes the payload it received
to `~/.claude/statusline-payload.json`. It is the fastest way to find out which
fields Claude Code is, or isn't, sending in your version.

It is off by default on purpose. The payload carries your session name and paths
from your disk, and writing that to disk every 15 seconds is your call, not a
library default.

---

## Tests

```bash
python statusline.py --selftest     # internal checks, 0 dependencies
python statusline.py --calibrate 92 84   # re-measure Fable's factor: <all%> <fable%>
```

It prints how many checks ran: `OK - selftest (251 checks, 0 failure(s))`. The
count is there because `0 failure(s)` alone would read exactly the same if the
whole battery had been deleted. On Windows it reads one lower: the file-mode
check only means something where POSIX permissions do.

It covers duration and token formatting, the thermometers, context window
inference, the arithmetic of both caps (clean week, blown previous day, reset
eve, zeroed balance), payload decoding and the assembly of both lines. Then,
with real temporary files, the reverse reader, streaming deduplication, the time
window, weighted cost and the on-disk cache.

To see a real render with your own payload:

```python
import subprocess, pathlib
raw = (pathlib.Path.home() / '.claude' / 'statusline-payload.json').read_bytes()
print(subprocess.run(['python', 'statusline.py'], input=raw, capture_output=True).stdout.decode('utf-8'))
```

That file only exists once you've enabled `CLAUDE_STATUSLINE_DEBUG=1`.

---

## License

MIT, see [LICENSE](LICENSE).

---

Independent project, not affiliated with or endorsed by Anthropic. "Claude" and
"Claude Code" are theirs; they are used here to say what this thing plugs into.
