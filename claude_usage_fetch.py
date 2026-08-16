#!/usr/bin/env python3
"""Fetch the account's OFFICIAL usage and cache it for `statusline.py` to read.

The bar knows the total quota (Claude Code's payload carries `five_hour` and
`seven_day`), but NOT how much of it was Fable - the payload does not break usage
down by model. Without this script the bar estimates that share by scanning the
local transcripts and correcting it with a hand-calibrated factor, which drifts
as soon as the account's model mix changes.

The number itself can be fetched. Claude Code fetches it from
`GET /api/oauth/usage`, authenticated by the OAuth credential already in this
machine's Keychain (the binary's `fetchUtilization`). The response carries a
`limits` array, and each entry has `kind`, `percent` and `resets_at`:

    kind=session         percent=10            the 5-hour window
    kind=weekly_all      percent=24            the whole weekly quota
    kind=weekly_scoped   percent=17  Fable     Fable's cap

Fable's `weekly_scoped` is exactly the "Current week (Fable)" of the usage
screen. This script reads all three, writes them to
`~/.claude/statusline-usage-official.json`, and the bar starts DISPLAYING the
official number instead of estimating it.

One field is left uncovered by the API: Fable's DAILY cap. The API serves the
weekly per-model slice and nothing daily, so the daily field still comes from the
local share - and still needs a factor. That is why this script also RE-MEASURES
the factor on every round, whenever the local share in the bar's cache is
comparable (same week, same day, fresh). The source's `FABLE_SHARE_CALIBRATION`
becomes just the floor for when no measurement is available.

OPT-IN, and it stays that way. This script reads YOUR credential from the
Keychain and calls an endpoint with no public contract. The status line never
runs it unless you set:

    export CLAUDE_STATUSLINE_USAGE_API=1

Usage:  claude_usage_fetch.py             fetch and cache (quiet; for hook/spawn)
        claude_usage_fetch.py --print     fetch, cache and show what was found
        claude_usage_fetch.py --selftest  internal checks
        claude_usage_fetch.py --help      this help

Trigger: `statusline.py` itself fires this script DETACHED once the cache is past
its TTL - never inside the render, which has a deadline and gets killed on the
next refresh. Running it by hand works too.

FAIL-OPEN throughout. This script exists to decorate a status bar: no failure of
its own may bring a session down, and no failure of its own erases the last good
number - an error writes `error` + `attempt_ts` and PRESERVES the `ts` and the
values from the last successful fetch. That way a network outage does not trade a
10-minute-old number for no number at all.

THE TOKEN NEVER REACHES STDOUT, not in `--print`, not in an error message. Failure
messages name the CLASS of the problem (no token, expired, HTTP 401, network),
never the content of the credential.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# This script is called by another process (the status line, a hook). With no
# console attached Python does not guarantee UTF-8, so it is forced here.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover - py3.6-
    pass

HERE = Path(__file__).resolve().parent
STATUSLINE = HERE / "statusline.py"

# The endpoint Claude Code itself uses (`fetchUtilization`). It is NOT a public
# API with a contract: it may change without notice. That is why every consumer
# of this cache has to survive the file being absent - the field disappears, it
# is never guessed.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# ABSOLUTE path: resolving `security` through PATH would let a tampered PATH run
# a different program in exactly the flow that touches the Keychain.
SECURITY_BIN = "/usr/bin/security"
KEYCHAIN_SERVICE = "Claude Code-credentials"
# The Keychain item holds two blocks; only the claude.ai one matters here.
KEYCHAIN_BLOCK = "claudeAiOauth"

KEYCHAIN_TIMEOUT = 5
HTTP_TIMEOUT = 8
# Ceiling on the response body. The real payload is a few KB.
MAX_RESPONSE = 256 * 1024

# Labels in the `limits` array. Fable's is keyed by `scope.model.display_name`,
# not by position: the order of the array is not a contract.
KIND_SESSION = "session"
KIND_WEEK = "weekly_all"
KIND_SCOPED = "weekly_scoped"
FABLE_MODEL = "fable"


def _statusline():
    """The bar's module, imported by path.

    Deliberate reuse, and the direction matters: this script imports the bar, the
    bar NEVER imports this one (it only reads the JSON and spawns by path). The
    reverse import would close a cycle. What comes from there is what must not
    diverge into two copies - the calibration formula above all: two places
    computing the same factor is the double source that always drifts.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("statusline_mod", STATUSLINE)
    if spec is None or spec.loader is None:
        raise ImportError("statusline.py is not importable")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- primitives


def finite_number(value):
    """A local copy of the bar's sanitiser, so this script does not need the import.

    The import can fail (file moved, old version) and the official fetch still has
    to work - and it has to validate numbers coming off the network BEFORE any
    range test. NaN escapes every range test: every comparison with it is false.
    """
    import math

    if isinstance(value, bool):  # bool IS int in Python
        return None
    if not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def valid_percent(value):
    """A quota percentage is only valid between 0 and 100.

    Above 100 does not mean "blew through it": the API caps at 100 and anything
    past that is a strange payload. Out of range = absent, by the same rule as
    always - implausible data is missing data, not bad data to be repaired.
    """
    num = finite_number(value)
    if num is None or not 0 <= num <= 100:
        return None
    return num


def short_text(value, limit=80):
    """Outside text, truncated, no line breaks - for `resets_at` and `error`."""
    if not isinstance(value, str):
        return None
    clean = value.replace("\n", " ").replace("\r", " ").strip()
    return clean[:limit] or None


# ---------------------------------------------------------------- credential


def read_token():
    """(token, reason). Token is None when unusable; reason says why.

    Reads ONLY Claude Code's own item, by name, never a dump of the keychain.
    `expiresAt` is checked BEFORE the call: an expired token gets a 401, and a 401
    helps nothing here, because this script renews nothing. Renewal belongs to the
    CLI (`refreshOAuth`), and its next session rewrites the Keychain - so expired
    just means skipping this round and waiting.
    """
    try:
        proc = subprocess.run(
            [SECURITY_BIN, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as err:
        return None, "keychain unavailable (%s)" % type(err).__name__
    if proc.returncode != 0:
        return None, "keychain refused (rc=%d)" % proc.returncode
    try:
        block = json.loads(proc.stdout).get(KEYCHAIN_BLOCK)
    except (ValueError, AttributeError):
        return None, "the keychain item is not the expected JSON"
    if not isinstance(block, dict):
        return None, "the keychain item has no %s block" % KEYCHAIN_BLOCK

    token = block.get("accessToken")
    if not isinstance(token, str) or not token:
        return None, "no accessToken"
    # `expiresAt` arrives in MILLISECONDS. Treating it as seconds would give a
    # 1970 date and the script would declare itself expired forever, in silence.
    expires = finite_number(block.get("expiresAt"))
    if expires is not None and expires / 1000.0 <= time.time():
        return None, "token expired"
    return token, ""


# ---------------------------------------------------------------- network


def extract_limits(payload):
    """The three percentages that matter, out of the `limits` array.

    Returns a dict holding only what arrived valid - a missing or implausible
    field simply does not go in, and the reader decides whether the bar can still
    be built.
    """
    found = {}
    if not isinstance(payload, dict):
        return found
    limits = payload.get("limits")
    if not isinstance(limits, list):
        return found

    for item in limits:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        pct = valid_percent(item.get("percent"))
        if pct is None:
            continue
        reset = short_text(item.get("resets_at"))
        if kind == KIND_SESSION:
            found["session_percent"] = pct
        elif kind == KIND_WEEK:
            found["all_percent"] = pct
            found["week_resets_at"] = reset
        elif kind == KIND_SCOPED:
            scope = item.get("scope")
            model = scope.get("model") if isinstance(scope, dict) else None
            name = model.get("display_name") if isinstance(model, dict) else None
            # Matched by NAME, lowercased. If Anthropic renames the model, the
            # field disappears from the bar instead of becoming another model's
            # number.
            if isinstance(name, str) and name.strip().lower() == FABLE_MODEL:
                found["fable_percent"] = pct
                found["fable_resets_at"] = reset
    return found


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses ANY redirect on this call.

    `urlopen` follows redirects on its own, and the default handler COPIES the
    original request's headers - `Authorization` included - onto the new request
    without checking whether the host changed. A 302 from the endpoint (an infra
    change, or a compromise) would hand the whole token to another host, and the
    default handler even accepts a downgrade to `http`.

    There is no legitimate use for a redirect here: the endpoint answers 200 or
    fails. Returning None makes urllib raise `HTTPError` carrying the 3xx itself,
    which lands in the normal error handling - the token never goes back out.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch(token):
    """(limits, reason). Never raises; never returns the token anywhere."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                return None, "HTTP %s" % resp.status
            # BOUNDED read: `timeout` applies to socket operations, not to the
            # total deadline, so a server dripping bytes forever would keep
            # `read()` alive eating memory. The real payload is a few KB; 256 KB
            # is two orders of magnitude of headroom.
            raw = resp.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                return None, "response too large"
            payload = json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as err:
        # The code only. A 4xx body can echo the authentication header back.
        return None, "HTTP %s" % err.code
    except (urllib.error.URLError, OSError) as err:
        return None, "network unavailable (%s)" % type(err).__name__
    except ValueError:
        return None, "the response is not JSON"
    except Exception as err:
        # The network raises things that are neither `URLError` nor `OSError`:
        # `http.client`'s `IncompleteRead`, `BadStatusLine` and `LineTooLong`
        # come up outside both. In a status line, an escaping exception kills the
        # process instead of degrading - this broad catch is the fail-open
        # contract, not carelessness. Only the class NAME goes into the reason,
        # never the exception's `str`, which can carry part of the response.
        return None, "unexpected failure (%s)" % type(err).__name__

    limits = extract_limits(payload)
    if "fable_percent" not in limits and "all_percent" not in limits:
        # A 200 arrived without either of the two fields the bar uses: the shape
        # of the response changed. Fail explicitly, so an empty cache is not
        # written with the look of a good one.
        return None, "a 200 with none of the expected limits"
    return limits, ""


# ---------------------------------------------------------------- calibration


def measure_factor(mod, all_percent, fable_percent):
    """(factor, raw_share) re-measured right now, or (None, None).

    The raw share comes from the cache the bar writes when it scans transcripts.
    The period guards are the SAME as the manual `--calibrate`
    (`cache_is_calibratable`): a share from another week or another day against
    the official number of right now produces a perfectly formatted factor
    straddling two periods - an invented number wearing the look of a measured
    one.
    """
    if all_percent is None or fable_percent is None:
        return None, None
    try:
        cache = json.loads(
            mod.state_file(mod.WEEK_CACHE_FILE).read_text(encoding="utf-8"))
        share, start, day, written = (
            finite_number(cache.get(k)) for k in ("share", "start", "day_start", "ts"))
    except (OSError, ValueError, AttributeError):
        return None, None
    if None in (share, start, day, written):
        return None, None
    if mod.cache_is_calibratable(start, day, written, time.time()):
        return None, None
    try:
        return mod.calibration_factor(share, all_percent, fable_percent), share
    except ValueError:
        return None, None


# ---------------------------------------------------------------- cache


def cache_path(mod):
    return mod.state_file(mod.OFFICIAL_CACHE_FILE)


def read_cache(mod):
    """The current cache as a dict, or {} - used to keep the last good values."""
    try:
        current = json.loads(cache_path(mod).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return current if isinstance(current, dict) else {}


def write_cache(mod, limits, reason):
    """Write the cache. A failure PRESERVES the values of the last good fetch.

    The two timestamps exist for different reasons and cannot collapse into one:
      `ts`           when the VALUES were measured - says whether they still serve
      `attempt_ts`   when it was last attempted - holds off the next attempt

    Collapsing them would make a network failure look like a fresh measurement (a
    new `ts` over old values), or make the script retry in a burst on every
    render.
    """
    now = time.time()
    record = read_cache(mod)
    record["attempt_ts"] = now
    if limits:
        record.pop("error", None)
        record["ts"] = now
        # The new round's keys REPLACE the old ones, and those that did not
        # arrive disappear: keeping last round's `fable_percent` beside this
        # round's `all_percent` would mix two instants in one record.
        for key in ("session_percent", "all_percent", "fable_percent",
                    "week_resets_at", "fable_resets_at", "factor", "raw_share"):
            record.pop(key, None)
        record.update(limits)
    else:
        record["error"] = reason or "failure with no reason"

    try:
        # `cache_path` calls `state_file`, which calls `mkdir` - with a read-only
        # HOME that raises OSError. Evaluating it BEFORE the `try` let that
        # failure escape as a traceback instead of degrading.
        mod.write_private(cache_path(mod), json.dumps(record, ensure_ascii=True))
    except OSError:
        return record, False
    return record, True


def run_once(mod):
    """One full fetch: credential -> network -> calibration -> cache."""
    token, reason = read_token()
    if token is None:
        return write_cache(mod, None, reason)[0]
    limits, reason = fetch(token)
    del token  # no live reference left after use
    if limits is None:
        return write_cache(mod, None, reason)[0]

    factor, share = measure_factor(
        mod, limits.get("all_percent"), limits.get("fable_percent"))
    if factor is not None:
        limits["factor"] = round(factor, 4)
        limits["raw_share"] = round(share, 6)
    return write_cache(mod, limits, "")[0]


# ---------------------------------------------------------------- selftest


def selftest():
    failures = []

    def check(name, got, want):
        if got != want:
            failures.append(name)
            print("FAIL %s: %r != %r" % (name, got, want))

    # -- sanitising numbers coming off the network
    check("NaN does not pass", finite_number(float("nan")), None)
    check("inf does not pass", finite_number(float("inf")), None)
    check("bool is not a number", finite_number(True), None)
    check("a string is not a number", finite_number("17"), None)
    check("an int passes as float", finite_number(17), 17.0)
    # Range against a LITERAL, not against the constant itself - a
    # self-referential check pins nothing.
    check("percent 0 is valid", valid_percent(0), 0.0)
    check("percent 100 is valid", valid_percent(100), 100.0)
    check("percent 101 is not", valid_percent(101), None)
    check("a negative percent is not", valid_percent(-1), None)
    check("a NaN percent is not", valid_percent(float("nan")), None)

    check("outside text loses its line breaks",
          short_text("2026-08-21\nT12:59Z"), "2026-08-21 T12:59Z")
    check("non-string text becomes None", short_text(123), None)
    check("empty text becomes None", short_text("   "), None)
    check("text gets truncated", len(short_text("x" * 500)), 80)

    # -- extraction from the `limits` array, in the REAL shape measured 2026-08-16
    payload = {"limits": [
        {"kind": "session", "percent": 10,
         "resets_at": "2026-08-16T17:09:59+00:00"},
        {"kind": "weekly_all", "percent": 24,
         "resets_at": "2026-08-21T12:59:59+00:00"},
        {"kind": "weekly_scoped", "percent": 17,
         "scope": {"model": {"display_name": "Fable"}},
         "resets_at": "2026-08-21T12:59:59+00:00"},
    ]}
    found = extract_limits(payload)
    check("reads the 5-hour session", found.get("session_percent"), 10.0)
    check("reads the whole week", found.get("all_percent"), 24.0)
    check("reads Fable's cap", found.get("fable_percent"), 17.0)
    check("reads Fable's reset", found.get("fable_resets_at"),
          "2026-08-21T12:59:59+00:00")

    # A `weekly_scoped` for ANOTHER model must not become Fable's. Without this
    # check, replacing the name comparison with a bare `if scope:` would slip by.
    other = {"limits": [
        {"kind": "weekly_scoped", "percent": 99,
         "scope": {"model": {"display_name": "Opus"}}},
    ]}
    check("another model's weekly_scoped is ignored",
          extract_limits(other).get("fable_percent"), None)
    check("the model name matches regardless of case",
          extract_limits({"limits": [
              {"kind": "weekly_scoped", "percent": 5,
               "scope": {"model": {"display_name": "  FABLE "}}}]}
          ).get("fable_percent"), 5.0)

    # -- degradation: a malformed payload must not raise
    for name, broken in (
        ("a non-dict payload", "this is not an object"),
        ("limits missing", {}),
        ("limits not a list", {"limits": "x"}),
        ("an item that is not a dict", {"limits": ["x", 3, None]}),
        ("scope not a dict", {"limits": [
            {"kind": "weekly_scoped", "percent": 5, "scope": "x"}]}),
        ("model not a dict", {"limits": [
            {"kind": "weekly_scoped", "percent": 5, "scope": {"model": 7}}]}),
        ("percent missing", {"limits": [{"kind": "weekly_all"}]}),
        ("percent NaN", {"limits": [
            {"kind": "weekly_all", "percent": float("nan")}]}),
    ):
        try:
            check(name, extract_limits(broken), {})
        except Exception as err:  # pragma: no cover - this is what the check forbids
            failures.append(name)
            print("FAIL %s raised %r" % (name, err))

    # -- the cache keeps the two timestamps for different reasons
    class FakeMod:
        OFFICIAL_CACHE_FILE = "x.json"
        WEEK_CACHE_FILE = "y.json"

        def __init__(self, base):
            self.base = base
            self.written = None

        def state_file(self, name):
            return self.base / name

        def write_private(self, path, text):
            self.written = text
            path.write_text(text, encoding="utf-8")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        fake = FakeMod(Path(tmp))
        good, ok = write_cache(fake, {"fable_percent": 17.0, "all_percent": 24.0}, "")
        check("it wrote", ok, True)
        check("success stamps ts", "ts" in good, True)

        bad, _ = write_cache(fake, None, "network unavailable (URLError)")
        check("failure PRESERVES the good value", bad.get("fable_percent"), 17.0)
        check("failure PRESERVES the measurement ts", bad.get("ts"), good["ts"])
        check("failure records the reason", bad.get("error"),
              "network unavailable (URLError)")
        check("failure moves attempt_ts",
              bad["attempt_ts"] >= good["attempt_ts"], True)

        # A new round must not leave an old field beside a new one.
        all_only, _ = write_cache(fake, {"all_percent": 30.0}, "")
        check("a field absent from the new round DISAPPEARS",
              all_only.get("fable_percent"), None)
        check("a field that did arrive goes in", all_only.get("all_percent"), 30.0)
        # This write comes AFTER the one that failed on purpose: on a cache that
        # never had an `error`, "success clears the error" passes without
        # clearing anything - a fixture that fails to tell the candidates apart
        # (a mutant survived that way).
        check("a later success WIPES the earlier failure's error",
              "error" in all_only, False)

        # The token must never have passed through the file.
        check("the cache holds no token", "accessToken" in (fake.written or ""), False)

    if failures:
        print("FAILED - selftest (%d failure(s))" % len(failures))
        return 1
    print("OK - claude_usage_fetch selftest")
    return 0


# ---------------------------------------------------------------- cli


def main(argv):
    if "--help" in argv or "-h" in argv:
        print(__doc__.strip())
        return 0
    if "--selftest" in argv:
        return selftest()

    try:
        mod = _statusline()
    except Exception as err:  # fail-open: with no bar, there is nothing to feed
        print("statusline.py is not importable: %s" % type(err).__name__)
        return 1

    try:
        record = run_once(mod)
    except Exception as err:
        # Last net of the fail-open: nothing in this script may kill the process.
        print("unexpected failure: %s" % type(err).__name__)
        return 1

    if "--print" in argv:
        if record.get("error"):
            print("failed: %s" % record["error"])
            print("(the values below are from the last good fetch, if any)")
        for label, key in (("5h session", "session_percent"),
                           ("week", "all_percent"),
                           ("Fable", "fable_percent")):
            value = record.get(key)
            if value is not None:
                print("%-11s %5.1f%%" % (label, value))
        if record.get("factor") is not None:
            print("factor re-measured now  %.3f  (raw share %.2f%%)"
                  % (record["factor"], record["raw_share"] * 100))
        else:
            print("factor NOT re-measured (the local share cache is not comparable)")
        print("measured %.0f s ago" % (time.time() - record.get("ts", 0)))
    return 0 if not record.get("error") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
