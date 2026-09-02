# Changelog

Notable changes to the status line, newest first. Dates are the day the change
landed. Anything older than the first entry below lives in the commit history.

## 2026-09-02

### Fixed

- **The two total-quota fields come out bold again.** Bold was emitted glued to
  the parameters of a 256-color (`\x1b[1;38;5;67m`). That is valid ANSI, but the
  renderer that draws the status line rewrites the SGR it passes through and
  drops the `1` in that form, so the daily total and the weekly quota showed up
  colored and thin. `paint()` now emits bold as a sequence of its own
  (`\x1b[1m\x1b[38;5;67m`), which no parser has to recognize as a composed form.
  The Fable model label on line 1 was losing its bold to the same cause and is
  fixed by the same change.
  - Limit: only bold written as a PREFIX is split out. Writing it as a suffix
    (`38;5;240;1`) hands back the composed form.

### Added

- **Bold on the two TOTAL quotas** - the weekly one and the day's cap. They are
  the readings of the quota you pay for whole; the Fable field beside each one
  stays unbolded. The bold separates the two layers without spending more room
  on the bar.
- **`VIVID_RED` (`38;5;196`) is now a reserved, named color**, held for the
  layer of the total quotas inside line 2. A selftest invariant fails if any
  Fable color takes it.

### Changed

- **Fable's palette went down one step**, to `137` (muted amber) -> `173`
  (muted orange) -> `124` (dark red). While Fable shared the vivid red, the
  field that does *not* block was the loudest thing on the bar and stole the eye
  from the one that does.
- **The hot end of the blocking quotas went up**, to `184` (`#d7d700`) -> `178`
  (`#d7af00`) -> `196`. Yellow and orange always move together: leaving the
  orange behind made "running hot" darker than "on pace", a gradient running
  backwards right at the point of raising the alarm.
- **The context alert now suggests `/clear`, not `/compact`.**
