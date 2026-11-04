#!/usr/bin/env python3
"""
pysanoid - ZFS snapshot management: a Python port of sanoid's take and prune (no syncoid)

Needs Python 3.11 or later.

sanoid (https://github.com/jimsalterjrs/sanoid) is the work of Jim Salter and its contributors. This is a modified
version of it: ported to Python by nihil2040 in 2026, from sanoid at commit d39b51a, and changed where DEVIATIONS.md
says. It is an independent port, not affiliated with or endorsed by the sanoid project.

This software is licensed for use under the Free Software Foundation's GPL v3.0 license, as retrieved
from http://www.gnu.org/licenses/gpl-3.0.html on 2014-11-17. A copy is in LICENSE, alongside this file.

README.md says what this is and how to run it. DEVIATIONS.md lists every place it differs from upstream
sanoid, and why; the code marks each with its ID (DEV-NNN, or PN for parity that needed a note).
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import io
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import tzinfo as _tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    # what RawIOBase.write takes, under whichever Python the type checker targets
    from _typeshed import ReadableBuffer

VERSION = "0.1.54"
MINIMUM_DEFAULTS_VERSION = 2

# One section's resolved options: str from the ini, bool for TOGGLE_KEYS and the initialized and zfs_recursion
# marks -- hence Any.
SectionConfig = dict[str, Any]

# Snapshot counts, ages and taken-snapshot details: same shape as SectionConfig, different meaning.
Stats = dict[str, Any]

ZFS = shutil.which("zfs") or "zfs"
ZPOOL = shutil.which("zpool") or "zpool"

PERIOD_TYPES = ("yearly", "monthly", "weekly", "daily", "hourly", "frequently")

# keys read as Python bools (_normalize_toggles)
TOGGLE_KEYS = (
    "autosnap", "autoprune", "recursive", "process_children_only", "skip_children",
    "no_inconsistent_snapshot", "force_post_snapshot_script",
    "monitor", "monitor_dont_warn", "monitor_dont_crit",
)
# Upstream's @istrue/@isfalse exactly, matched case-sensitively: "yEs" is recognized by neither.
TRUE_VALUES = {"1", "true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"}
FALSE_VALUES = {"0", "false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"}

# Upstream's "don't copy this key" filters, one per source: defaults, templates, a section's own settings, and
# the two recursion copies. They differ as upstream's do.
_DEFAULTS_EXCLUDE = re.compile(r"template|recursive|children_only")
_TEMPLATE_EXCLUDE = re.compile(r"template|recursive")
_LOCAL_EXCLUDE = re.compile(r"template|recursive|skip_children")
_RECURSE_COPY_EXCLUDE = re.compile(r"template|recursive|children_only")
_ZFS_RECURSE_COPY_EXCLUDE = re.compile(r"template|recursive|children_only|autosnap")


class SanoidError(Exception):
    """A fatal, user-facing error, as upstream's `die`; main prints it after `prefix`."""

    prefix: str = "FATAL"


class _UnknownSettingError(SanoidError):
    """A setting upstream doesn't know, refused in upstream's words."""

    prefix: str = "FATAL ERROR"


class _CapacityError(SanoidError):
    """A pool's capacity that can't be read, in upstream's words."""

    prefix: str = "ERROR"


def run_zfs_bytes(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    """subprocess.run for every zfs and zpool call, keeping what the command printed as bytes (P10).

    A command that can't be run raises SanoidError: a missing binary (DEV-045), no process to spare (DEV-043), or a NUL
    byte in an argument, which only a damaged cache can supply."""
    try:
        return subprocess.run(cmd, check=False, capture_output=True)
    except (OSError, ValueError) as e:
        raise SanoidError(f"could not run '{' '.join(cmd)}': {e}") from e


def _write_as_written(data: bytes, stream: TextIO) -> None:
    """Writes what zfs or zpool printed to one of pysanoid's streams, byte for byte (P10)."""
    if data:
        print(data.decode("utf-8", errors="surrogateescape"), end="", file=stream, flush=True)


def _echo_zfs_stderr(proc: subprocess.CompletedProcess[bytes]) -> None:
    """Writes a zfs or zpool command's stderr out as it was read (DEV-047)."""
    _write_as_written(proc.stderr, sys.stderr)


def run_zfs_cmd(cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Runs a `zfs snapshot` or `zfs destroy` and writes out what it printed, stdout then stderr, whether it worked
    or failed (DEV-047)."""
    proc = run_zfs_bytes(cmd)
    _write_as_written(proc.stdout, sys.stdout)
    _echo_zfs_stderr(proc)
    return proc


def _wait_status(returncode: int) -> int:
    """Perl's $? for a command that ran: its exit status times 256, or the signal that killed it (DEV-027: without the
    128 a core dump adds)."""
    return returncode << 8 if returncode >= 0 else -returncode


# Perl's \s on a byte string (Config::IniFiles reads raw bytes), not Python's Unicode \s, which also
# matches NBSP, NEL, U+2003 and more. Shared by the ini grammar and the numeric parsers.
_WS = " \t\r\n\f\v"
# P11: a trailing comment, which upstream's numification ignores. The lookbehind starts a match only where a run of
# whitespace starts, so a long run is read once, not once per character.
_TRAILING_COMMENT_RE = re.compile(rf"(?<![{_WS}])[{_WS}]*[#;].*")
# a whole number Perl and int() read alike: ASCII digits, an optional sign, surrounding whitespace
_INT_RE = re.compile(rf"[{_WS}]*[+-]?[0-9]+[{_WS}]*")


def _cfg_int(cfg: SectionConfig, key: str, default: int, context: str = "") -> int:
    """A whole-number setting: ASCII digits with an optional sign, a trailing comment ignored (P11). Blank or absent
    gives `default`; anything else raises SanoidError naming the key and value.

    DEV-032: read strictly, where upstream numifies (`3o` is 3); take's preferred times fall back to upstream's
    reading (P1). A setting a destroy depends on goes through _cfg_count instead."""
    raw = cfg.get(key, default)
    if isinstance(raw, str):
        raw = _TRAILING_COMMENT_RE.sub("", raw, count=1)
    if raw is None or raw == "":
        return default
    where = f" for '{context}'" if context else ""
    if isinstance(raw, str) and not _INT_RE.fullmatch(raw):
        raise SanoidError(f"'{key}'{where} must be a whole number, got {raw!r}.")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise SanoidError(f"'{key}'{where} must be a whole number, got {raw!r}.") from None


# ASCII only -- deliberately not \d, which also matches other scripts' digits
_COUNT_RE = re.compile(r"([0-9]+)")  # prune_defer (upstream's check_capacity_limit is /^\d+\z/) and DEV-050's version
_COUNT_COMMENT_RE = re.compile(rf"([0-9]+)(?:[{_WS}]*[#;].*)?")  # retention: upstream numifies the leading digits
_COUNT_MAX = 2**63 - 1  # what a count of more than 18 digits reads as (_cfg_count)


def _cfg_count(cfg: SectionConfig, key: str, default: int, context: str = "", *, digits_only: bool = False) -> int:
    """A whole-number setting a destroy depends on: a retention count, or prune_defer with `digits_only`. ASCII digits
    only, a count allowing a trailing comment (P11). Absent gives `default`; blank, negative, signed, padded, decimal
    or other scripts' digits raise SanoidError (DEV-008). More than 18 digits reads as _COUNT_MAX, a count no dataset
    reaches (DEV-008). Take reads a refused count upstream's way instead (P1, _take_count)."""
    if key not in cfg:
        return default
    raw: object = cfg[key]
    match = (_COUNT_RE if digits_only else _COUNT_COMMENT_RE).fullmatch(str(raw))
    if match is None:
        where = f" for '{context}'" if context else ""
        raise SanoidError(f"'{key}'{where} must be a whole number >= 0 (digits only), got {raw!r}.")
    digits = match.group(1).lstrip("0")
    return int(digits or "0") if len(digits) <= 18 else _COUNT_MAX


# Perl's reading of a string as a number (perl 5.34's grok_number, my_atof3 and strtod), for P1: whitespace, one sign,
# then the longest number strtod reads (decimal, hex, inf, nan), the rest ignored; no number is 0 ("3o" is 3). Perl
# adds its own Inf and NaN spellings (grok_infnan: "1.#IND", "snan"), but not after a second sign: "++snan" is 0.
_STRTOD_RE = re.compile(
    rf"[{_WS}]*([+-]?)(?:(inf)|(nan)|0x([0-9a-f]+\.?[0-9a-f]*|\.[0-9a-f]+)(p[+-]?[0-9]+)?"
    + r"|([0-9]+\.?[0-9]*|\.[0-9]+)(e[+-]?[0-9]+)?)",
    re.ASCII | re.IGNORECASE,
)
_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _grok_infnan(text: str) -> tuple[float, bool] | None:
    """grok_infnan on the start of `text`: (value, moved) for an Inf or NaN spelling, else None. `moved` is whether
    Perl advanced its pointer; my_atof3 takes an unmoved one for no match. A NaN payload other than digits, as in
    `snan(abc)`, counts as no spelling here, though Perl accepts some."""
    low, n, s = text.translate(_ASCII_LOWER), len(text), 0
    inf = -math.inf if low[:1] == "-" else math.inf
    if low[:1] in ("+", "-"):
        s = 1
    odh = low[s:s + 1] == "1"  # Visual C's 1.#INF, 1.#IND, 1.#QNAN (the dot is optional)
    if odh:
        s += 2 if low[s + 1:s + 2] == "." else 1
        if low[s:s + 1] != "#":
            return None
        s += 1
    if s >= n:
        return None
    if low[s] == "i":
        if low[s + 1:s + 2] != "n" or s + 2 >= n:
            return None
        s += 2
        if low[s] == "f":
            # "INFINITY" cut short returns early
            return inf, low[s + 1:s + 2] != "i" or low[s + 2:s + 6] == "nity"
        if low[s] == "d" and odh:
            return math.nan, True
        return None
    if low[s] in ("s", "q"):
        s += 1
        if s == n:
            return None
    if low[s:s + 3] != "nan":
        return None
    s += 3
    if low[s:s + 1] in ("q", "s"):
        s += 1
    if s == n:
        return math.nan, False
    if text[s] == "(":
        close = s + 2
        while close < n and text[close] not in ("\0", ")"):
            close += 1
        if close >= n:
            return math.nan, False
        if text[close] == ")" and not re.fullmatch(r"[0-9]+", text[s + 1:close]):
            return None
    return math.nan, True


def _strtod(text: str) -> float:
    """The number C's strtod reads at the start of `text`, 0 when there is none (see above)."""
    match = _STRTOD_RE.match(text)
    if match is None:
        return 0.0
    sign, inf, nan, hex_digits, hex_exponent, digits, exponent = match.groups()
    if inf:
        value = math.inf
    elif nan:
        value = math.nan
    elif hex_digits is not None:
        try:
            value = float.fromhex(f"0x{hex_digits}{hex_exponent or ''}")
        except OverflowError:
            value = math.inf  # strtod's HUGE_VAL
    else:
        value = float(digits + (exponent or ""))
    return -value if sign == "-" else value


def _perl_number(raw: str) -> float:
    """What upstream's arithmetic makes of `raw` (see above), for take's reading of a value the strict parsers
    refuse (P1)."""
    text = raw.lstrip(_WS)
    body = text[1:] if text[:1] in ("+", "-") else text
    if body[:1] not in ("+", "-") and (found := _grok_infnan(body)) is not None:  # grok_number: one sign
        return -found[0] if text[:1] == "-" else found[0]
    text = raw.split("\0", 1)[0].lstrip(_WS)  # my_atof3 reads a C string
    negative = text[:1] == "-"
    body = text[1:] if text[:1] in ("+", "-") else text
    found = _grok_infnan(("-" if negative else "") + body)
    if found is not None and found[1]:
        return found[0]
    if body[:1] == "0" and body[1:2] in ("x", "X", "b", "B"):
        return 0.0  # Perl's guard: strtod reads hex, and some C libraries binary
    value = _strtod(body)
    return -value if negative else value


# ============================================================================
# CLI
# ============================================================================

_GETOPT_INT_RE = re.compile(r"[-+]?_*[0-9][0-9_]*\n?")


def _getopt_int(value: str) -> int:
    """`--cache-ttl`'s value as Getopt::Long's `=i` reads it (P12): an optional sign, then digits with underscores
    anywhere (`1_000`, `_5`, `5_`), and a final newline allowed."""
    if not _GETOPT_INT_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(f"invalid integer value: {value!r}")
    return int(value.rstrip("\n").replace("_", ""))


def _path_arg(value: str) -> str:
    """Refuses an empty path, as Getopt::Long's `=s` does for `--cache-dir=`.
    DEV-024: the `--configdir ''` form is refused too; upstream accepts it."""
    if not value:
        raise argparse.ArgumentTypeError("requires a non-empty value")
    return value


def parse_args(argv: list[str]) -> argparse.Namespace:
    """The command line, as upstream's GetOptions reads it for every spelling units and cron lines use (P12).
    DEV-022: argparse in place of Getopt::Long; Getopt::Long's odd spellings are usage errors here.
    DEV-023: arguments that are not options are refused; upstream ignores them.
    DEV-024: an empty directory option is refused (_path_arg).
    DEV-026: --monitor-* is not implemented.
    DEV-052: the config, cache and lock directories default to pysanoid's own, not upstream's."""
    p = argparse.ArgumentParser(
        prog="pysanoid",
        description="ZFS snapshot management (Python port of sanoid)",
    )
    p.add_argument("--configdir", default="/etc/pysanoid", type=_path_arg)
    p.add_argument("--cache-dir", default="/var/cache/pysanoid", type=_path_arg)
    p.add_argument("--run-dir", default="/var/run/pysanoid", type=_path_arg)

    p.add_argument("--cron", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--readonly", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--force-update", action="store_true")
    p.add_argument("--force-prune", action="store_true",
                    help="deprecated, behavior is now always standard")

    p.add_argument("--take-snapshots", action="store_true")
    p.add_argument("--prune-snapshots", action="store_true")
    p.add_argument("--cache-ttl", type=_getopt_int, default=None)

    p.add_argument("--version", action="version", version=f"pysanoid {VERSION}")

    args = p.parse_args(argv)

    # P12: given nothing but the directory options, run as --cron --verbose
    action_flags = (
        args.cron, args.verbose, args.debug, args.readonly, args.quiet,
        args.force_update, args.take_snapshots, args.prune_snapshots, args.force_prune,
    )
    if not any(action_flags) and args.cache_ttl is None:
        args.cron = True
        args.verbose = True

    if args.force_prune:
        print("WARN: --force-prune argument is deprecated and its behavior is now standard", file=sys.stderr)

    if args.cache_ttl is not None and args.cache_ttl < 0:
        raise SanoidError("cache-ttl needs to be positive!")

    return args


# ============================================================================
# Locking
# ============================================================================

def _open_lock(run_dir: Path, name: str) -> io.FileIO:
    """The lock file, made if missing and never emptied here: another process may hold it, and its PID is in it."""
    run_dir.mkdir(parents=True, exist_ok=True)
    return open(run_dir / f"{name}.lock", "ab", buffering=0)


def _note_pid(fh: io.FileIO) -> None:
    """Replaces what a lock file it now holds says with this process's PID, for a person looking. Only the flock
    matters, so a run directory too full to take the PID doesn't cost the lock; unbuffered, so nothing is left to fail
    at close."""
    with contextlib.suppress(OSError):
        _ = fh.truncate(0)
        _ = fh.write(str(os.getpid()).encode())


@contextlib.contextmanager
def try_lock(run_dir: Path, name: str) -> Generator[tuple[bool, OSError | None], None, None]:
    """Non-blocking: yields (held, error). (True, None) while the lock is held, (False, None) when another process
    holds it, and (False, the OSError) when the lock file can't be made or locked (a read-only or full run directory,
    or one that can't be made).

    DEV-040: flock in place of upstream's PID lockfile. DEV-041: on that error the caller reports it and skips only what
    the lock guards; upstream carries on unlocked and dies releasing the lock."""
    with contextlib.ExitStack() as stack:
        error: OSError | None = None
        try:
            fh = stack.enter_context(_open_lock(run_dir, name))
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            held = False
        except OSError as e:
            held, error = False, e
        else:
            held = True
            _note_pid(fh)
            _ = stack.callback(fcntl.flock, fh, fcntl.LOCK_UN)
        yield held, error


@contextlib.contextmanager
def wait_lock(run_dir: Path, name: str, verbose: bool = False) -> Generator[tuple[bool, OSError | None], None, None]:
    """Blocking: yields (held, error), (True, None) once the lock is held or (False, the OSError) when it can't be taken
    (see try_lock). Only the cache update lock is waited for, as upstream's is, saying so once (DEV-040)."""
    with contextlib.ExitStack() as stack:
        error: OSError | None = None
        try:
            fh = stack.enter_context(_open_lock(run_dir, name))
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if verbose:
                    print("INFO: waiting for cache update lock held by another pysanoid process.")
                fcntl.flock(fh, fcntl.LOCK_EX)
        except OSError as e:
            held, error = False, e
        else:
            held = True
            _note_pid(fh)
            _ = stack.callback(fcntl.flock, fh, fcntl.LOCK_UN)
        yield held, error


# ============================================================================
# Config parsing and template merge
# ============================================================================

def _normalize_toggles(section: SectionConfig, context: str) -> None:
    """Reads each toggle in `section` as a Python bool; a blank one stays unset."""
    for key in TOGGLE_KEYS:
        if key not in section:
            continue
        val = section[key]
        if isinstance(val, bool):
            continue
        # trimmed already (_section_dict); str.strip() would also drop NBSP, NEL and U+001C-001F
        sval = str(val)
        if sval == "":
            # sanoid.defaults.conf ships several toggles blank, meaning unset
            continue
        if sval in TRUE_VALUES:
            section[key] = True
        elif sval in FALSE_VALUES:
            section[key] = False
        else:
            # DEV-002: an unrecognized value is refused; upstream reads it as true.
            raise SanoidError(
                f"'{key}' in [{context}] must be one of "
                f"{', '.join(sorted(TRUE_VALUES | FALSE_VALUES))}, got {sval!r}."
            )


# ----------------------------------------------------------------------------
# ini parsing
# ----------------------------------------------------------------------------
# P13: a port of Config::IniFiles' line grammar with its default options, not configparser. Each pattern is the
# module's own, as the comment beside it shows.
_BLANK_RE = re.compile(rf"[{_WS}]*")  # \A\s*\z
_COMMENT_RE = re.compile(rf"[{_WS}]*[;#]")  # \A\s*[;#]   (default allowed comment chars)
_SECTION_RE = re.compile(rf"[{_WS}]*\[[{_WS}]*([^{_WS}]|[^{_WS}].*[^{_WS}])[{_WS}]*\][{_WS}]*")  # \A\s*\[\s*(\S|\S.*\S)\s*\]\s*\z
# ^\s*([^=]*?[^=\s])\s*=\s*(.*)$, possessive (*+) where giving characters back can't make a match: Python's matcher
# otherwise takes quadratic time on a long line of whitespace with no `=`.
_PARAM_RE = re.compile(rf"[{_WS}]*+([^=]*?[^={_WS}])[{_WS}]*+=[{_WS}]*+(.*)$")
_LINE_END_RE = re.compile(r"(\r\n?|\n|\x15)\Z")
_BOM_TWICE = "\N{BYTE ORDER MARK}".encode().decode("latin-1")  # a file's C3 AF C2 BB C2 BF, decoded (see _ini_lines)

IniData = dict[str, dict[str, list[str]]]


def _ini_lines(text: str) -> list[str]:
    """Physical lines, split as Config::IniFiles' _nextline splits them (P13): the file's first line ending (CR, LF,
    NAK or CRLF) separates every line, and any other character is content, VT, FF and NEL included, which is why this
    isn't str.splitlines(). A file with no line ending has no lines. The first line loses a byte-order mark encoded
    twice (C3 AF C2 BB C2 BF), the only one upstream's pattern matches."""
    first = re.search(r"[\r\n\x15]", text)
    if first is None:
        return []
    sep = first.group()
    if sep == "\r" and text[first.end():first.end() + 1] == "\n":
        sep = "\r\n"
    pieces = text.split(sep)
    lines: list[str] = []
    for i, piece in enumerate(pieces):
        followed_by_sep = i < len(pieces) - 1
        if not followed_by_sep and piece == "":
            break  # nothing after the final separator
        lines.append(_LINE_END_RE.sub("", piece + sep if followed_by_sep else piece, count=1))
    lines[0] = lines[0].removeprefix(_BOM_TWICE)
    return lines


def _parse_ini_text(text: str) -> tuple[IniData, list[str]]:
    """Config::IniFiles' ReadConfig with its default options (P13): (sections, errors), where any error, or a file with
    no sections, is one upstream refuses to load. A value is kept verbatim, a here-doc runs to a line equal to its
    marker, a repeated section merges, and a repeated key keeps every value, in order."""
    sections: IniData = {}
    errors: list[str] = []
    lines = _ini_lines(text)
    current: dict[str, list[str]] | None = None
    i = 0
    while i < len(lines):
        line, lineno = lines[i], i + 1
        i += 1
        if _BLANK_RE.fullmatch(line) or _COMMENT_RE.match(line):
            continue
        section = _SECTION_RE.fullmatch(line)
        if section is not None:
            current = sections.setdefault(section.group(1), {})
            continue
        param = _PARAM_RE.match(line)
        if param is None:
            errors.append(f"line {lineno} is malformed: {line!r}")
            continue
        if current is None:
            errors.append(f"line {lineno}: parameter found outside a section")
            return sections, errors  # upstream stops parsing at this one
        key, value = param.group(1), param.group(2)
        values: list[str] = [value]
        if value.startswith("<<"):
            marker = value[2:]
            values = []
            while i < len(lines) and lines[i] != marker:
                values.append(lines[i])
                i += 1
            if i == len(lines):
                errors.append(f"line {lineno}: no end marker ({marker!r}) found")
                return sections, errors
            i += 1
        existing = current.get(key)
        if existing is None:
            current[key] = values  # upstream's newval(): no values -> undef, one -> scalar, several -> list
        elif existing:
            existing.extend(values)  # upstream's push()
        # else an empty here-doc left it undef, and upstream's push() drops what is added to undef
    if not sections:
        errors.append("empty file treated as error")
    return sections, errors


def _read_ini(path: str | Path, *, user_editable: bool) -> IniData:
    """One ini file as {section: {key: [values...]}}, in file order. A file upstream would refuse to load raises
    SanoidError naming the file as given, in upstream's words."""
    remedy = (
        "please create a valid local config file before running pysanoid!"
        if user_editable else "please restore a clean copy, this is not a user-editable file!"
    )
    try:
        # P13: never decoded strictly, so a byte that isn't UTF-8 is harmless where the grammar doesn't read it
        text = Path(path).read_bytes().decode("utf-8", errors="surrogateescape")
    except OSError as e:
        raise SanoidError(f"cannot load {path} - {remedy} ({e})") from e
    sections, errors = _parse_ini_text(text)
    if errors:
        raise SanoidError(f"cannot load {path} - {remedy} ({'; '.join(errors[:3])})")
    return sections


def _refuse_nul(path: str | Path, ini: IniData) -> None:
    """DEV-009: a NUL byte in any section name, key or value refuses the file; upstream loads it."""
    for section, params in ini.items():
        if "\0" in section or any("\0" in text for key, values in params.items() for text in (key, *values)):
            raise SanoidError(f"cannot load {path} - section {section!r} contains a NUL byte.")


def _section_dict(ini: IniData, section: str) -> dict[str, str]:
    """{key: first value, trimmed of ASCII whitespace} for one section ({} if there is no such section).
    DEV-001: every source is trimmed, where upstream skips defaults and templates below their sections.
    DEV-010, DEV-049: an empty here-doc (no values) reads as blank, in either file, where upstream reads undef."""
    return {key: (values[0] if values else "").strip(_WS) for key, values in ini.get(section, {}).items()}


class ZfsClient:
    """Wraps `zfs`/`zpool` subprocess calls plus the on-disk dataset cache."""

    def __init__(self, cache_dir: Path, cache_ttl: int, force_update: bool, debug: bool = False):
        self.cache_dir = cache_dir
        self.cache_ttl = cache_ttl
        self.force_update = force_update
        self.debug = debug
        self._dataset_cache_path = cache_dir / "datasets.txt"
        self._cached_datasets: list[str] | None = None
        self._pending_datasets: list[str] = []

    def load_dataset_cache(self) -> list[str]:
        """Reads datasets.txt if it is usable ([] if not) and remembers it, once, before the config is merged, as
        upstream's init does. A usable cache is also what keeps flush_dataset_cache from rewriting the file. One that
        is unreadable, expired, forced past or empty is no cache, and so is a damaged one (DEV-019, DEV-020)."""
        self._cached_datasets = self._read_dataset_cache()
        return self._cached_datasets

    def _read_dataset_cache(self) -> list[str]:
        """datasets.txt's lines, or [] when it is no cache (see load_dataset_cache)."""
        path = self._dataset_cache_path
        if self.force_update or not os.path.isfile(path):
            return []
        try:
            age = int(time.time()) - int(path.stat().st_mtime)  # P14
        except OSError:
            return []
        # DEV-020: a file dated in the future is no cache; upstream's age test passes it.
        if not 0 <= age <= self.cache_ttl:
            return []
        if self.debug:
            print(f"DEBUG: dataset cache not expired ({age} seconds old with TTL of {self.cache_ttl}): pulling dataset "
                  + "list from cache.")
        try:
            text = _read_text(path)
        except OSError:
            return []
        # DEV-019: a NUL byte means the file is damaged: no cache; upstream uses it.
        return [] if "\0" in text else _lines(text)

    def note_dataset(self, path: str) -> None:
        """Record a dataset path for the cache rewrite: a section's own, or a child a recursion listed."""
        self._pending_datasets.append(path)

    def get_child_datasets(self, fs: str) -> list[str]:
        """All descendant datasets of `fs` (matches `zfs list -Hr <fs>`, minus fs itself). Raises SanoidError when the
        lookup can't start, is killed, or fails partway: only a complete list of children can be trusted."""
        cached = self._cached_datasets if self._cached_datasets is not None else self.load_dataset_cache()

        if cached:
            found_parent = False
            children: list[str] = []
            for ds in cached:
                if ds.startswith(fs + "/"):
                    children.append(ds)
                elif ds == fs:
                    found_parent = True
            if found_parent:
                return children
            # the cache doesn't list fs: ask zfs

        # DEV-017: `fs` is one argument; upstream splices it into a shell command line.
        cmd = [ZFS, "list", "-o", "name", "-t", "filesystem,volume", "-Hr", fs]
        if self.debug:  # P8: upstream's line, naming zfs as upstream runs it
            print(f"DEBUG: getting list of child datasets on {fs} using  zfs {' '.join(cmd[1:])} |...")
        proc = run_zfs_bytes(cmd)
        _echo_zfs_stderr(proc)
        # DEV-018: a lookup killed, or failing after it listed something, stops the run
        if proc.returncode < 0 or (proc.returncode > 0 and proc.stdout):
            how = f"was killed by signal {-proc.returncode}" if proc.returncode < 0 else f"failed partway (exit status {proc.returncode})"
            raise SanoidError(f"'{' '.join(cmd)}' {how}, so the children of '{fs}' aren't known")
        return _lines(proc.stdout.decode("utf-8", errors="surrogateescape"))[1:]  # first line is the parent itself

    def flush_dataset_cache(self, run_dir: Path, verbose: bool = False, problems: list[str] | None = None) -> None:
        """Rewrites datasets.txt from what this run asked zfs, only when the cache on disk wasn't usable, as upstream's
        does. DEV-039b, DEV-041: one that can't be written or locked is reported, and the run carries on."""
        cached = self._cached_datasets if self._cached_datasets is not None else self.load_dataset_cache()
        if cached:
            return
        with try_lock(run_dir, "sanoid_cachedatasetupdate") as (held, error):
            if error is not None:
                _report_problem(f"could not update {self._dataset_cache_path} ({error})", problems)
                return
            if not held:
                if verbose:
                    print("INFO: deferring dataset cache update - valid cache update lock held by another pysanoid process.")
                return
            if verbose:
                reason = "forcibly expired" if self.force_update else "expired"
                print(f"INFO: dataset cache {reason} - updating from zfs list.")
            _ = _replace_cache(self._dataset_cache_path, "".join(f"{ds}\n" for ds in self._pending_datasets), problems)


def load_config(configdir: str | Path, zfs: ZfsClient) -> dict[str, SectionConfig]:
    """sanoid.conf and sanoid.defaults.conf merged, recursion applied, into one entry per dataset, as upstream's init
    builds them, with its --debug trace when zfs.debug is set."""
    # spelled as upstream builds them for its messages: `--configdir=/etc/pysanoid/` gives "/etc/pysanoid//sanoid.conf"
    conf_file = f"{configdir}/sanoid.conf"
    default_conf_file = f"{configdir}/sanoid.defaults.conf"

    # os.path, not pathlib, in every file check: through Python 3.13 pathlib's raise where upstream's -e reads false
    if not os.path.exists(default_conf_file):
        raise SanoidError(
            f"cannot load {default_conf_file} - please restore a clean copy, "
            "this is not a user-editable file!"
        )
    if not os.path.exists(conf_file):
        raise SanoidError(
            f"cannot load {conf_file} - please create a valid local config file before running pysanoid!"
        )

    defaults = _read_ini(default_conf_file, user_editable=False)
    ini = _read_ini(conf_file, user_editable=True)
    _refuse_nul(default_conf_file, defaults)
    _refuse_nul(conf_file, ini)

    defaults_version = 1
    if defaults.get("version", {}).get("version"):
        raw_version = _section_dict(defaults, "version")["version"]
        # DEV-050: ASCII digits only, where upstream numifies ("2a" is 2, "1_0" is 1); int() would read "1_0" as 10
        if not _COUNT_RE.fullmatch(raw_version):
            raise SanoidError(
                f"'version' in [version] of {default_conf_file} must be a whole number, "
                f"got {raw_version!r}."
            )
        defaults_version = int(raw_version)
    if defaults_version < MINIMUM_DEFAULTS_VERSION:
        raise SanoidError(
            f"you're using sanoid.defaults.conf v{defaults_version}, this version of "
            f"pysanoid requires a minimum sanoid.defaults.conf v{MINIMUM_DEFAULTS_VERSION}"
        )

    template_default_keys = set(_section_dict(defaults, "template_default"))

    # once, before the section loop, as upstream reads it
    zfs.load_dataset_cache()

    config: dict[str, SectionConfig] = {}
    sections_seen: list[str] = []  # the dataset sections the loop has passed, in file order
    made_by: dict[str, str] = {}  # a dataset's entry -> the recursive section that last copied its settings there

    def apply_keys(
        target: SectionConfig, source: SectionConfig, exclude: re.Pattern[str], warn_context: str | None = None,
        trace: str | None = None,
    ) -> None:
        """Copies `source`'s keys into `target`, but those `exclude` matches, warning and tracing as upstream does."""
        for key, value in source.items():
            if exclude.search(key):
                if warn_context:
                    print(f"ignored key '{key}' from {warn_context}.", file=sys.stderr)
                continue
            if trace and zfs.debug:
                print(f"DEBUG: overriding {key} on {trace}.")
            target[key] = value

    # Section by section, in file order, as upstream's loop goes, so its warnings and --debug lines come in its order
    # (but for the recursion's, DEV-048).
    for section, params in ini.items():
        for key, values in params.items():
            if key not in template_default_keys:
                raise _UnknownSettingError(
                    f"I don't understand the setting {key} you've set in [{section}] in {conf_file}."
                )
            # DEV-001: a repeated key's first value, warned about in upstream's words
            if len(values) > 1:
                print(
                    f"duplicate key '{key}' in section '{section}', using the value from the "
                    f"first occurence and ignoring the others.",
                    file=sys.stderr,
                )

        if section.startswith("template_"):
            continue  # templates are only ever applied, never processed directly

        if "initialized" not in config.get(section, {}):
            if zfs.debug:
                print(f"DEBUG: initializing $config{{{section}}} with default values from {default_conf_file}.")
            config.setdefault(section, {})
            defaults_section = _section_dict(defaults, "template_default")
            apply_keys(config[section], defaults_section, _DEFAULTS_EXCLUDE)

            user_default_template = _section_dict(ini, "template_default")
            apply_keys(config[section], user_default_template, _TEMPLATE_EXCLUDE, "user-defined default template",
                       f"{section} with value from user-defined default template")

        if "use_template" in params:
            # DEV-003: whitespace around each name is ignored; upstream splits on ' *, *' only.
            templates = [t.strip() for t in _section_dict(ini, section)["use_template"].split(",") if t.strip()]
            for tmpl_name in templates:
                tmpl_full_name = f"template_{tmpl_name}"
                # DEV-003: an undefined template is refused; upstream applies nothing. An empty one is valid.
                if tmpl_full_name not in ini:
                    raise SanoidError(
                        f"[{section}] in {conf_file} uses template '{tmpl_name}', but there's "
                        f"no [{tmpl_full_name}] section defined."
                    )
                tmpl_section = _section_dict(ini, tmpl_full_name)
                apply_keys(config[section], tmpl_section, _TEMPLATE_EXCLUDE, f"'{tmpl_name}' template",
                           f"{section} with value from user-defined template {tmpl_full_name}")

        local = _section_dict(ini, section)
        apply_keys(config[section], local, _LOCAL_EXCLUDE, trace=f"{section} with value directly set in module")

        _normalize_toggles(config[section], section)

        # DEV-049: an empty here-doc path is the section's name
        path = local["path"] if params.get("path") else section
        config[section]["path"] = path
        zfs.note_dataset(path)

        # a dataset inside a zfs recursion is covered by its parent's `zfs snapshot -r`
        if config[section].get("zfs_recursion") and config[section].get("autosnap"):
            print(f"ignored autosnap configuration for '{section}' because it's part of a zfs recursion.", file=sys.stderr)
            config[section]["autosnap"] = False

        # from the section's own settings only, never templates or defaults, as upstream reads $ini{$section}
        raw_recursive = local.get("recursive", "")
        # "zfs" anywhere in the value, in any case, as upstream's /zfs/i
        is_zfs_recursive = "zfs" in raw_recursive.lower() if raw_recursive else False
        is_recursive = (raw_recursive in TRUE_VALUES) if raw_recursive else False
        # DEV-004: membership test, other values refused (here and for skip_children); upstream uses a regex.
        if raw_recursive and not (is_zfs_recursive or is_recursive or raw_recursive in FALSE_VALUES):
            raise SanoidError(
                f"'recursive' in [{section}] must be one of "
                f"{', '.join(sorted(TRUE_VALUES | FALSE_VALUES))}, or contain 'zfs' (for native "
                f"ZFS recursion), got {raw_recursive!r}."
            )
        raw_skip_children = local.get("skip_children", "")
        # same membership test and validation as `recursive` above
        is_skip_children = (raw_skip_children in TRUE_VALUES) if raw_skip_children else False
        if raw_skip_children and not (is_skip_children or raw_skip_children in FALSE_VALUES):
            raise SanoidError(
                f"'skip_children' in [{section}] must be one of "
                f"{', '.join(sorted(TRUE_VALUES | FALSE_VALUES))}, got {raw_skip_children!r}."
            )

        if is_zfs_recursive or is_recursive or is_skip_children:
            # DEV-005: a section above its recursive parent is refused; upstream's recursion overwrites it.
            above = next((s for s in sections_seen if s.startswith(f"{path}/")), None)
            if above is not None:
                effect = ("skip_children would drop it" if is_skip_children and not is_zfs_recursive
                          else "recursion would replace its settings")
                raise SanoidError(
                    f"[{above}] in {conf_file} must come after [{section}]: sections are read in order, and "
                    f"[{section}]'s {effect}. Move it below [{section}]."
                )
            if is_zfs_recursive:
                config[section]["zfs_recursion"] = True

            # DEV-018: a lookup that doesn't finish cleanly stops the run here
            children = zfs.get_child_datasets(path)
            listed = set(children)
            # DEV-006: a section under this one starts from its settings, listed or not; upstream uses the defaults.
            unlisted = [] if is_skip_children and not is_zfs_recursive else [
                name for name in ini
                if name.startswith(f"{path}/") and not name.startswith("template_") and name not in listed]
            for dataset in (*children, *unlisted):
                if dataset in listed:
                    zfs.note_dataset(dataset)  # datasets.txt holds what zfs listed, as upstream's does

                if is_zfs_recursive:
                    child_cfg: SectionConfig = {"autosnap": False}  # the parent's `zfs snapshot -r` covers it
                    exclude = _ZFS_RECURSE_COPY_EXCLUDE
                elif is_skip_children:
                    if zfs.debug:
                        print(f"DEBUG: ignoring {dataset}.")
                    config.pop(dataset, None)
                    continue
                else:
                    child_cfg = {}
                    exclude = _RECURSE_COPY_EXCLUDE
                for key, value in config[section].items():
                    if not exclude.search(key):
                        if zfs.debug:
                            print(f"DEBUG: recursively setting {key} from {section} to {dataset}.")
                        child_cfg[key] = value

                child_cfg["path"] = dataset
                child_cfg["initialized"] = True
                # updated in place, as upstream's is: the entry may hold an outer recursion's copy
                config.setdefault(dataset, {}).update(child_cfg)
                made_by[dataset] = section

        sections_seen.append(section)

    _refuse_shared_datasets(config, {s for s in ini if not s.startswith("template_")}, made_by, conf_file)
    return config


def _refuse_shared_datasets(config: dict[str, SectionConfig], sections: set[str], made_by: dict[str, str],
                            conf_file: str) -> None:
    """DEV-007: two entries that take or prune the same dataset refuse the config; upstream acts on both.
    Only a section with its own `path =` can share one: every other entry is keyed by its dataset. An entry a recursion
    made is named after the recursive section, and the advice is the child section upstream's recursion expects."""
    owners: dict[str, list[str]] = {}
    for name, cfg in config.items():
        # the entries take and prune act on, by their skip rules
        if name.startswith("template") or cfg.get("process_children_only") or not (cfg.get("autosnap") or cfg.get("autoprune")):
            continue
        owners.setdefault(cfg["path"], []).append(name)
    for path, names in owners.items():
        if len(names) < 2:
            continue
        who = [f"[{name}]" if name in sections else f"[{made_by[name]}]'s recursion" for name in names]
        listing = f"{', '.join(who[:-1])} and {who[-1]}"
        recursion = next((made_by[name] for name in names if name not in sections), None)
        advice = (f"To give {path} settings of its own, name its section [{path}] and place it below [{recursion}]."
                  if recursion else f"Keep one section for {path}.")
        raise SanoidError(f"{listing} in {conf_file} {'both' if len(names) == 2 else 'all'} take or prune {path}, "
                          + f"each by its own settings. {advice}")


# ============================================================================
# ZFS snapshot data
# ============================================================================

@dataclass
class Snapshot:
    """One autosnap snapshot, from a listing or the cache."""

    fs: str
    name: str
    snap_type: str
    ctime: float  # an int, unless the cache holds a creation time too large for one (_perl_digits)


def get_active_dataset_paths(config: dict[str, SectionConfig]) -> list[str]:
    """The dataset paths take and prune act on, for `zfs get` (upstream's get_active_datasets)."""
    paths: list[str] = []
    for section, cfg in config.items():
        if section.startswith("template"):
            continue
        if not cfg.get("autoprune") and not cfg.get("autosnap"):
            continue
        if cfg.get("process_children_only"):
            continue
        paths.append(cfg["path"])
    # DEV-021: each path once; upstream asks zfs for a path twice when two sections resolve to it.
    return sorted(set(paths), key=str.lower)


# P15: upstream's unanchored `m/(.*)\@(.*ly)\t*creation\t*(\d*)/` and `m/.*_(\w*ly)/`, ASCII as its byte matching is,
# read in linear time by _snapshot_line and _snapshot_type rather than as one regex. DEV-033: `\t+`, not `\t*`.
_SNAP_TAIL_RE = re.compile(r"(.*ly)\t+creation\t*(\d*)", re.ASCII)
_WORD_RE = re.compile(r"\w+", re.ASCII)


def _snapshot_line(line: str) -> tuple[str, str, str] | None:
    """(dataset, name, creation digits) as `(.*)@(.*ly)\\t+creation\\t*(\\d*)` reads `line`, or None. Both `.*` are greedy,
    so the regex takes the last `@` that has a `ly`, tabs and "creation" after it, and then the last such `ly`: the last
    one before a newline, which _SNAP_TAIL_RE finds, then the last `@` before it."""
    tail = _SNAP_TAIL_RE.match(line)
    if tail is None:
        return None
    end = tail.end(1)
    at = line.rfind("@", 0, end - 2)
    return None if at < 0 else (line[:at], line[at + 1:end], tail.group(2))


def _snapshot_type(name: str) -> str | None:
    """The type `.*_(\\w*ly)` reads in a snapshot's name, or None. `_` is a word character, so the regex takes the last
    `_` with a `ly` after it in its run of word characters (before a newline), and that run up to its last `ly`."""
    for word in reversed([m.group() for m in _WORD_RE.finditer(name.split("\n", 1)[0])]):
        ly = word.rfind("ly")
        underscore = word.rfind("_", 0, ly) if ly > 0 else -1
        if underscore >= 0:
            return word[underscore + 1:ly + 2]
    return None


def _read_text(path: Path) -> str:
    """The file's bytes as a str that can never fail to decode (P10)."""
    return path.read_bytes().decode("utf-8", errors="surrogateescape")


def _write_text(path: Path, text: str) -> None:
    """Writes `text` back as the bytes _read_text read it from."""
    path.write_bytes(text.encode("utf-8", errors="surrogateescape"))


def _lines(text: str) -> list[str]:
    """Lines as Perl's `<FH>` reads them, split on the newline only (P10), which is why this isn't str.splitlines()."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _read_cache(path: Path) -> str | None:
    """A cache file's text, or None when it is no cache: not a regular file (a named pipe would hang the run),
    unreadable, holding a NUL byte (DEV-038), or cut short inside its last line (DEV-036)."""
    if not os.path.isfile(path):
        return None
    try:
        text = _read_text(path)
    except OSError:
        return None
    return None if "\0" in text or (text and not text.endswith("\n")) else text


def _cache_age(path: Path) -> int | None:
    """Whole seconds since the cache file was written (P14); None without a regular file."""
    try:
        return int(time.time()) - int(path.stat().st_mtime) if os.path.isfile(path) else None
    except OSError:
        return None


def _drop_cache(cache: Path, error: OSError, problems: list[str] | None) -> None:
    """A cache that can't be updated is reported (_report_problem) and removed with its temp file, so no stale copy
    outlives the run and the next run reads zfs afresh. The caller carries on with what it holds in memory."""
    _report_problem(f"could not update {cache} ({error}); removing it, so the next run reads zfs afresh", problems)
    for stale in (cache.with_suffix(".txt.tmp"), cache):
        with contextlib.suppress(OSError):
            stale.unlink()


def _replace_cache(cache: Path, text: str, problems: list[str] | None, keep_times: os.stat_result | None = None) -> bool:
    """Writes a cache file through a temp file and a rename, as upstream does, giving it `keep_times`' atime and mtime
    (an incremental edit must not restart the TTL clock).

    DEV-039b: a failure is reported and the file removed (_drop_cache), and the run carries on; upstream dies, or installs
    a truncated file."""
    tmp = cache.with_suffix(".txt.tmp")
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        _write_text(tmp, text)
        if keep_times is not None:
            os.utime(tmp, (keep_times.st_atime, keep_times.st_mtime))
        _ = tmp.rename(cache)
    except OSError as e:
        _drop_cache(cache, e, problems)
        return False
    return True


def _perl_digits(digits: str) -> float:
    """A creation time's digits as Perl reads them: exact up to 2**64 - 1, a float beyond (infinity past a float's
    range), and "" is 0 (P15). Only a damaged cache holds more than 20 digits; int() refuses more than 4,300."""
    significant = digits.lstrip("0")
    if len(significant) <= 20 and (value := int(significant or "0")) <= 2**64 - 1:
        return value
    return float(significant)


def _parse_snapshot_lines(lines: list[str]) -> dict[str, dict[str, Snapshot]]:
    """{dataset: {name: Snapshot}} for the autosnap snapshots in a listing or cache (P15)."""
    snaps: dict[str, dict[str, Snapshot]] = {}
    for line in lines:
        parsed = _snapshot_line(line)
        if parsed is None:
            continue
        fs, name, ctime = parsed
        if not name.startswith("autosnap"):
            continue
        # P15: no type is kept, as type "", which nothing acts on
        snap_type = _snapshot_type(name)
        if snap_type is None:
            snap_type = ""
        # P15: no creation time is 0, decades old
        snaps.setdefault(fs, {})[name] = Snapshot(fs, name, snap_type, _perl_digits(ctime))
    return snaps


def _list_snapshots(config: dict[str, SectionConfig], verbose: bool, debug: bool) -> list[str]:
    """`zfs get` over the configured datasets, retried without names when it fails, as in upstream's getsnaps.

    DEV-034: a `zfs get` killed by a signal is retried too; upstream's `$? >> 8` reads it as success.
    DEV-035: a listing that doesn't end in a newline loses its last line; upstream reads the cut line."""
    active = get_active_dataset_paths(config)
    if verbose and debug:
        print(f"INFO: running: zfs get -Hrpt snapshot creation {' '.join(active)}")
    proc = run_zfs_bytes([ZFS, "get", "-Hrpt", "snapshot", "creation", *active])
    _echo_zfs_stderr(proc)
    if proc.returncode != 0:
        # "shapshots" is upstream's spelling of this line
        print("INFO: zfs list shapshots with dataset names does not work, retrying without dataset names")
        proc = run_zfs_bytes([ZFS, "get", "-Hrpt", "snapshot", "creation"])
        _echo_zfs_stderr(proc)
    # P4: if the retry fails too, the cache is rewritten from whatever came back -- empty, and fresh -- as upstream's is.
    text = proc.stdout.decode("utf-8", errors="surrogateescape")
    lines = _lines(text)
    if lines and not text.endswith("\n"):
        _ = lines.pop()  # cut short (DEV-035)
    return lines


def get_snapshots(
    config: dict[str, SectionConfig], cache_dir: Path, run_dir: Path, cache_ttl: int,
    force_update: bool = False, verbose: bool = False, debug: bool = False, problems: list[str] | None = None,
) -> dict[str, dict[str, Snapshot]]:
    """Every autosnap snapshot: from the cache while it is fresh, else from zfs, rewriting the cache (upstream's
    getsnaps).

    DEV-039a: a cache that can't be read or isn't a regular file counts as expired (as one holding a NUL, DEV-038, or cut
    short, DEV-036), and zfs is read even while another process holds the update lock; upstream reads no snapshots. A
    cache that can't be written (DEV-039b), or locked for writing (DEV-041), is reported while the listing just read is
    used."""
    cache = cache_dir / "snapshots.txt"
    age = None if force_update else _cache_age(cache)
    # DEV-037: a negative age, the file written while the clock ran ahead, counts as expired
    text = _read_cache(cache) if age is not None and 0 <= age <= cache_ttl else None
    if text is not None:
        raw = _lines(text)
    else:
        with try_lock(run_dir, "sanoid_cacheupdate") as (held, error):
            if error is not None:
                _report_problem(f"could not update {cache} ({error}); the snapshots are read from zfs this run", problems)
                raw = _list_snapshots(config, verbose, debug)
            elif held:
                if verbose:
                    print(f"INFO: cache {'forcibly expired' if force_update else 'expired'} - updating from zfs list.")
                raw = _list_snapshots(config, verbose, debug)
                _ = _replace_cache(cache, "\n".join(raw) + ("\n" if raw else ""), problems)
            else:
                if verbose:
                    print("INFO: deferring cache update - valid cache update lock held by another pysanoid process.")
                text = _read_cache(cache)
                raw = _lines(text) if text is not None else _list_snapshots(config, verbose, debug)

    return _parse_snapshot_lines(raw)


_AHEAD_ALLOWED = 5 * 60  # DEV-051: how far ahead of the clock a snapshot may be dated and still count


def without_snapshots_dated_ahead(snaps: dict[str, dict[str, Snapshot]], now: float | None = None) -> dict[str, dict[str, Snapshot]]:
    """The snapshots take and prune act on: all but those dated more than _AHEAD_ALLOWED ahead of the clock, which
    they then neither count nor destroy (DEV-051)."""
    now = int(time.time()) if now is None else now
    return {fs: {name: snap for name, snap in by_name.items() if snap.ctime - now <= _AHEAD_ALLOWED}
            for fs, by_name in snaps.items()}


def snapshots_by_type(snaps: dict[str, dict[str, Snapshot]], config: dict[str, SectionConfig], now: float | None = None) -> dict[str, dict[str, Stats]]:
    """Per path and type: the count, the names oldest first, and the newest's age and name (upstream's getsnapsbytype).
    Ages are whole seconds (P14). DEV-014: snapshots made in the same second keep the order zfs lists them."""
    now = int(time.time()) if now is None else now
    result: dict[str, dict[str, Stats]] = {}
    for cfg in config.values():
        path = cfg["path"]
        by_type: dict[str, list[Snapshot]] = {}
        for snap in snaps.get(path, {}).values():
            by_type.setdefault(snap.snap_type, []).append(snap)

        entry: dict[str, Stats] = {}
        for snap_type, items in by_type.items():
            items.sort(key=lambda s: s.ctime)
            entry[snap_type] = {
                "numsnaps": len(items),
                "sorted": [s.name for s in items],
                "newest": now - items[-1].ctime,
                "newestname": items[-1].name,
            }
        result[path] = entry
    return result


def snapshots_by_path(snaps: dict[str, dict[str, Snapshot]], config: dict[str, SectionConfig], now: float | None = None) -> dict[str, Stats]:
    """Per path: the count and the newest's age, for the --debug dump (upstream's getsnapsbypath). Ages are whole
    seconds (P14)."""
    now = int(time.time()) if now is None else now
    result: dict[str, Stats] = {}
    for cfg in config.values():
        path = cfg["path"]
        items = list(snaps.get(path, {}).values())
        entry: Stats = {"numsnaps": len(items)}
        if items:
            newest = max(items, key=lambda s: s.ctime)
            entry["newest"] = now - newest.ctime
        result[path] = entry
    return result


def _perl_dumper(config: dict[str, SectionConfig]) -> str:
    """`Dumper(\\%config)` under blabber's `$Data::Dumper::Sortkeys = 1`, every other setting its default: keys sorted
    by their bytes, a nested hash indented past its key as counted in bytes, a toggle (or the initialized and
    zfs_recursion marks) as the Perl integer 1 or 0 init stores, and every other value single-quoted with only \\ and '
    escaped, as Data::Dumper writes a string, a count included (P8)."""
    def as_bytes(text: str) -> bytes:
        return text.encode("utf-8", "surrogateescape")

    def quoted(text: str) -> str:
        return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"

    if not config:
        return "$VAR1 = {};\n"
    entries: list[str] = []
    for section in sorted(config, key=as_bytes):
        name = quoted(section)
        if not config[section]:
            entries.append(f"          {name} => {{}}")
            continue
        pad = " " * (len("$VAR1 = ") + len(as_bytes(name)) + len(" => "))
        values = sorted(config[section].items(), key=lambda item: as_bytes(item[0]))
        lines = [f"{pad}    {quoted(k)} => {(1 if v else 0) if isinstance(v, bool) else quoted(str(v))}" for k, v in values]
        entries.append(f"          {name} => {{\n" + ",\n".join(lines) + f"\n{pad}  }}")
    return "$VAR1 = {\n" + ",\n".join(entries) + "\n        };\n"


def _perl_hours(age: float) -> str:
    """blabber's `sprintf("%.1f", $age / 60 / 60)`. The same as `age / 3600` to one decimal for every real creation
    time; a damaged cache's can differ, and Perl's arithmetic decides it: an integer stays one only in Perl's integer
    range, is divided exactly when past 2**53 and divisible, and infinity is spelled "-Inf" (P8)."""
    hours: float = float(age) if isinstance(age, int) and not -2**63 <= age < 2**64 else age
    for _ in range(2):
        if isinstance(hours, int) and abs(hours) > 2**53 and hours % 60 == 0:
            hours //= 60
        else:
            hours = float(hours) / 60
    if math.isinf(hours):
        return "-Inf" if hours < 0 else "Inf"
    return f"{float(hours):.1f}"


def print_debug_dump(config: dict[str, SectionConfig], snaps_by_type: dict[str, dict[str, Stats]], snaps_by_path: dict[str, Stats]) -> None:
    """--debug dump: the merged config plus a per-dataset snapshot summary, in the words and format of upstream's
    blabber() (P8). DEV-048: in a fixed order, where upstream's summary comes in Perl's hash order."""
    print("****** CONFIGS ******")
    print(_perl_dumper(config), end="")
    print()
    for cfg in config.values():
        path = cfg["path"]
        entry = snaps_by_path.get(path, {"numsnaps": 0})
        print(f"Filesystem {path} has:")
        # P8: upstream's uneven blank lines, one after "(no current snapshots)" and two after "newest: ..."
        print(f"     {entry['numsnaps']} total snapshots ", end="")
        if entry["numsnaps"] == 0:
            print("(no current snapshots)", end="")
        else:
            print(f"(newest: {_perl_hours(entry['newest'])} hours old)")
            for snap_type, type_entry in snaps_by_type.get(path, {}).items():
                print(f"          {type_entry['numsnaps']} {snap_type}")
                # P8: a type the section never configured shows blank, as upstream prints its undef
                print(f"              desired: {cfg.get(snap_type, '')}")
                print(f"              newest: {_perl_hours(type_entry['newest'])} hours old, named {type_entry['newestname']}")
        print("\n\n", end="")


# ============================================================================
# Snapshot scheduling
# ============================================================================

_ETC_LOCALTIME = Path("/etc/localtime")
_TZDIR = "/usr/share/zoneinfo"  # where glibc looks for a zone name when $TZDIR is unset or empty


class _ZoneBytes(io.BytesIO):
    """A zone file's bytes that refuse a read past their end. Python 3.11's zoneinfo reads on forever through a closing
    rule line cut short; 3.12 and later refuse the file."""

    def read(self, size: int | None = -1, /) -> bytes:
        """BytesIO.read, raising at the end of the bytes instead of returning nothing."""
        data = super().read(size)
        if not data and size != 0:
            raise ValueError("zone file cut short")
        return data


def _types_in_range(data: bytes) -> bool:
    """Whether each transition in a zone file names one of its time types, as Python 3.12 and later check: 3.11's
    zoneinfo reads past its table instead. The block checked is the one zoneinfo reads, from version 2 the second."""
    counts, at, time_size = struct.unpack_from(">6l", data, 20), 44, 4
    if data[4:5] != b"\0":
        isut, isstd, leap, timecnt, typecnt, chars = counts
        at += timecnt * 5 + typecnt * 6 + chars + leap * 8 + isstd + isut
        counts, at, time_size = struct.unpack_from(">6l", data, at + 20), at + 44, 8
    timecnt, typecnt = counts[3], counts[4]
    return not timecnt or max(data[at + timecnt * time_size:at + timecnt * (time_size + 1)], default=0) < typecnt


def _usable(zone: _tzinfo) -> _tzinfo | None:
    """zone, or None when it gives an offset datetime can't hold (a day or more: a rule such as `XST24`, or a damaged
    zone file) over the year and more take looks back on. glibc applies one; here take would stop."""
    now = time.time()
    try:
        for hours in range(-24 * 400, 48, 6):
            _ = datetime.fromtimestamp(now + hours * 3600, zone).utcoffset()
    except (ValueError, TypeError, OverflowError):
        return None
    return zone


def _zone_file(path: str | Path, key: str) -> _tzinfo | None:
    """The zone a zone file holds, or None when it holds none that glibc and datetime both read (zoneinfo raises
    ValueError, struct.error or AssertionError on a damaged file)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(1 << 20)  # a zone file is a few KB
        if not _types_in_range(data):
            return None
        zone = ZoneInfo.from_file(_ZoneBytes(data), key=key)
    except (OSError, ValueError, struct.error, AssertionError):
        return None
    return _usable(zone)


def _posix_rule_zone(rule: str) -> _tzinfo | None:
    """A POSIX rule (`IST-1GMT0,M10.5.0,M3.5.0/1`) as a zone. zoneinfo reads one as the closing line of a zone file,
    and with no transitions before it the rule decides every instant. None when zoneinfo can't read it (P2)."""
    block = b"TZif2" + bytes(15) + struct.pack(">6l", 0, 0, 0, 0, 1, 4) + struct.pack(">lbB", 0, 0, 0) + b"UTC\0"
    try:
        zone = ZoneInfo.from_file(_ZoneBytes(block + block + b"\n" + rule.encode() + b"\n"), key=rule)
    except (ValueError, struct.error, AssertionError):
        return None
    return _usable(zone)


def _tz_zone(tz: str) -> _tzinfo | None:
    """P2: $TZ as glibc reads it: UTC when it is empty or ':' alone; otherwise, a leading ':' dropped, a zone file (a
    name under $TZDIR, else /usr/share/zoneinfo, or a path), then a POSIX rule. None when it is neither (UTC)."""
    name = tz.removeprefix(":")
    if not name:
        return UTC
    path = name if name.startswith("/") else os.path.join(os.environ.get("TZDIR") or _TZDIR, name)
    return _zone_file(path, key=name) or _posix_rule_zone(name)


def get_local_zoneinfo() -> _tzinfo:
    """The local zone as glibc reads it (P2): $TZ when it is set (_tz_zone), else /etc/localtime, a link or a copy, else
    UTC with a warning. A real zone, not `datetime.now().astimezone()`'s fixed offset: _last_preferred_time reuses it
    for other dates, where a fixed offset would be wrong for a whole period after a DST change."""
    tz_env = os.environ.get("TZ")
    if tz_env is not None:
        if (zone := _tz_zone(tz_env)) is not None:
            return zone
        print(f"WARN: $TZ is set to '{tz_env}', which is neither a zone nor a POSIX rule pysanoid can read - using UTC.",
              file=sys.stderr)
        return UTC
    if (zone := _zone_file(_ETC_LOCALTIME, key=str(_ETC_LOCALTIME))) is not None:
        return zone
    print(f"WARN: {_ETC_LOCALTIME} is missing or isn't a zone file - using UTC.", file=sys.stderr)
    return UTC


def _autosnap_name(now: datetime, snap_type: str) -> str:
    """A snapshot's name, as upstream gives it: autosnap_YYYY-MM-DD_HH:MM:SS_<type>, in local time."""
    return f"autosnap_{now:%Y-%m-%d_%H:%M:%S}_{snap_type}"


def _after(a: datetime, b: datetime) -> bool:
    """True if a is really, chronologically after b. Not `a > b`: CPython compares aware datetimes sharing one tzinfo
    by their naive fields, ignoring fold, so in a fall-back's repeated hour `>` can put a candidate on the wrong side of
    now and take an extra daily, weekly or monthly. `.timestamp()` reads fold."""
    return a.timestamp() > b.timestamp()


def _latest_wall_minute(now: datetime, minutes: range | set[int]) -> datetime:
    """The latest real instant <= now whose local wall clock reads hh:mm:00 with mm in `minutes`.
    Scanning real minutes is exact through any clock change: a skipped wall time never matches, and
    both passes through a repeated one do. Stepping through wall-clock fields is not, where only part
    of an hour is skipped or repeated (Australia/Lord_Howe's clocks move 30 minutes)."""
    t = int(now.timestamp()) // 60 * 60
    for _ in range(3 * 60):  # a match is never more than about 90 minutes back
        candidate = datetime.fromtimestamp(t, now.tzinfo)
        if candidate.minute in minutes:
            return candidate
        t -= 60
    raise SanoidError(f"no local time in the last 3 hours ends in minute {sorted(minutes)}")


def _schedule_setting(cfg: SectionConfig, key: str, default: int, ctx: str, problems: list[str] | None) -> int:
    """A preferred-time setting as take reads it: _cfg_int's reading, or, for a value it refuses or a blank one,
    upstream's (_perl_number) without its fraction, reported (P1): `1_5` is 1, `30 min` is 30, `7.5` is 7, and blank or
    a comment alone is 0 where _cfg_int gives the default."""
    raw = cfg.get(key)
    if isinstance(raw, str) and not _TRAILING_COMMENT_RE.sub("", raw, count=1):
        _report_problem(f"'{key}' for '{ctx}' must be a whole number, got {raw!r}; reading it as 0", problems)
        return 0
    try:
        return _cfg_int(cfg, key, default, ctx)
    except SanoidError as e:
        reading = _perl_number(str(cfg[key]))
        if not math.isfinite(reading):
            raise
        _report_problem(f"{str(e).rstrip('.')}; reading it as {int(reading)}", problems)
        return int(reading)


def _last_preferred_time(now: datetime, cfg: SectionConfig, snap_type: str, problems: list[str] | None = None) -> datetime:
    """The most recent local wall-clock instant at which a snapshot of this type "should" have been
    taken, per the section's preferred-time settings. hourly and frequently scan back through real
    minutes (_latest_wall_minute); the longer types are built from local date and time fields with the
    zone attached, so zoneinfo picks the right UTC offset for that date.

    A malformed or blank preferred-time setting is read as upstream reads it and reported into
    `problems` (P1, _schedule_setting); frequent_period is not (DEV-032).

    DEV-030: zoneinfo, not upstream's flat-seconds DST arithmetic."""
    tz = now.tzinfo
    ctx = cfg.get("path", "?")

    def setting(key: str, default: int) -> int:
        """A preferred-time setting of this section, as take reads it (_schedule_setting)."""
        return _schedule_setting(cfg, key, default, ctx, problems)

    if snap_type == "frequently":
        period = _cfg_int(cfg, "frequent_period", 15, ctx)
        if period == 0:
            # DEV-032: 0 costs only this type; upstream dies for every dataset. Blank never gets here (15).
            raise SanoidError(f"'frequent_period' for '{ctx}' can't be 0.")
        # DEV-032: upstream's int($min / $period) * $period, a negative period slicing like its size
        return _latest_wall_minute(now, range(0, 60, abs(period)))

    if snap_type == "hourly":
        minute = setting("hourly_min", 0)
        if not 0 <= minute <= 59:
            raise SanoidError(f"'hourly_min' for '{ctx}' must be 0 to 59, got {minute}.")  # upstream dies
        return _latest_wall_minute(now, {minute})

    if snap_type == "daily":
        d = now.date()
        daily_hour, daily_min = setting("daily_hour", 23), setting("daily_min", 59)
        candidate = datetime(d.year, d.month, d.day, daily_hour, daily_min, tzinfo=tz)
        if _after(candidate, now):
            d -= timedelta(days=1)
            candidate = datetime(d.year, d.month, d.day, daily_hour, daily_min, tzinfo=tz)
        return candidate

    if snap_type == "weekly":
        # DEV-031: modulo 7, so 7 is Sunday again (0=Sunday, Perl's localtime convention)
        wday_cfg = setting("weekly_wday", 1) % 7
        today_wday = (now.weekday() + 1) % 7  # Python Monday=0 -> Perl Sunday=0 convention
        d = now.date() - timedelta(days=(today_wday - wday_cfg) % 7)
        candidate = datetime(d.year, d.month, d.day, setting("weekly_hour", 23), setting("weekly_min", 30), tzinfo=tz)
        if _after(candidate, now):
            candidate -= timedelta(days=7)
        return candidate

    if snap_type == "monthly":
        y, mo = now.year, now.month
        monthly_mday = setting("monthly_mday", 1)
        monthly_hour, monthly_min = setting("monthly_hour", 0), setting("monthly_min", 0)
        # raises in a month without monthly_mday, which plan_snapshots reports
        candidate = datetime(y, mo, monthly_mday, monthly_hour, monthly_min, tzinfo=tz)
        while _after(candidate, now):
            # back to the latest month that has the day; one without it (the 31st, after April) had no monthly
            mo -= 1
            if mo == 0:
                mo, y = 12, y - 1
            with contextlib.suppress(ValueError):
                candidate = datetime(y, mo, monthly_mday, monthly_hour, monthly_min, tzinfo=tz)
        return candidate

    if snap_type == "yearly":
        y = now.year
        yearly_mon, yearly_mday = setting("yearly_mon", 1), setting("yearly_mday", 1)
        yearly_hour, yearly_min = setting("yearly_hour", 0), setting("yearly_min", 0)
        # raises in a year without the day (29 February), which plan_snapshots reports
        candidate = datetime(y, yearly_mon, yearly_mday, yearly_hour, yearly_min, tzinfo=tz)
        while _after(candidate, now):
            y -= 1  # back to the latest year that has the day
            with contextlib.suppress(ValueError):
                candidate = datetime(y, yearly_mon, yearly_mday, yearly_hour, yearly_min, tzinfo=tz)
        return candidate

    raise SanoidError(f"unknown interval type {snap_type} in config!")


@dataclass
class PendingSnapshots:
    """The types due for one dataset this run, and whether its snapshot is recursive."""

    path: str
    recursive: bool
    types: list[str] = field(default_factory=list)


def _report_problem(msg: str, problems: list[str] | None) -> None:
    """Loud but non-fatal: prints an ERROR line and records the problem, so main()
    can exit non-zero (visible to systemd) after everything else has still run."""
    print(f"ERROR: {msg}", file=sys.stderr)
    if problems is not None:
        problems.append(msg)


def _take_count(cfg: SectionConfig, snap_type: str, section: str, problems: list[str] | None) -> float:
    """A retention count as take's `> 0` gate reads it: _cfg_count's reading, or, for a value it refuses that upstream
    reads as more than 0 (`3o` as 3, `30 days` as 30), upstream's, reported (P1). A blank, negative or wordy count
    (`abc` is 0) still raises, and takes nothing, as upstream takes nothing for it."""
    try:
        return _cfg_count(cfg, snap_type, 0, section)
    except SanoidError as e:
        reading = _perl_number(str(cfg[snap_type]))
        if not reading > 0:
            raise
        _report_problem(f"{str(e).rstrip('.')}; reading it as {reading:.15g}", problems)
        return reading


def plan_snapshots(
    config: dict[str, SectionConfig], snaps_by_type: dict[str, dict[str, Stats]], now: datetime | None = None,
    problems: list[str] | None = None,
) -> dict[str, PendingSnapshots]:
    """The snapshot types due for each dataset. A value that can't be read, a bad count or an out-of-range schedule
    setting, is reported (stderr, and `problems` if given) and costs only its (section, type) (DEV-028). A count of 0
    takes nothing; one upstream reads as a number the strict readers refuse is read upstream's way, reported (P1)."""
    now = now or datetime.now(get_local_zoneinfo())
    plan: dict[str, PendingSnapshots] = {}

    # DEV-048: in config order, where upstream's is Perl's hash order
    for section, cfg in config.items():
        if section.startswith("template"):
            continue
        if not cfg.get("autosnap"):
            continue
        if cfg.get("process_children_only"):
            continue

        path = cfg["path"]
        for snap_type in PERIOD_TYPES:
            try:
                if not _take_count(cfg, snap_type, section, problems) > 0:
                    continue

                newest_age = snaps_by_type.get(path, {}).get(snap_type, {}).get("newest", float("inf"))
                preferred = _last_preferred_time(now, cfg, snap_type, problems)
                # .timestamp(), not `now - preferred`: see _after()
                max_age = now.timestamp() - preferred.timestamp()
            except (SanoidError, ValueError, OverflowError) as e:
                # ValueError, OverflowError: datetime() rejecting an out-of-range schedule value
                _report_problem(f"not taking {snap_type} snapshots of '{section}' this run: {e}", problems)
                continue

            # a newest dated ahead holds its type back, as upstream's; main set aside one more than 5 minutes ahead (DEV-051)
            if newest_age > max_age:
                entry = plan.setdefault(path, PendingSnapshots(path=path, recursive=bool(cfg.get("zfs_recursion"))))
                entry.types.append(snap_type)

    return plan


def take_snapshots(
    config: dict[str, SectionConfig], snaps_by_type: dict[str, dict[str, Stats]], *,
    readonly: bool = False, verbose: bool = False, now: datetime | None = None,
    problems: list[str] | None = None,
) -> dict[str, Stats]:
    """Takes the snapshots that are due, with their hooks: {full name: {"time": epoch, "recursive": bool}} for each
    one made, for the cache. Names carry the time each dataset's turn came; a caller's `now` stands still (P17).

    A snapshot that couldn't be made or that a failed pre_snapshot_script held back, and a (section, type) that
    couldn't be planned, goes into `problems`, which main() turns into a non-zero exit (DEV-028). One that can't
    start fails alone, at once (DEV-043)."""
    zone = now.tzinfo if now else get_local_zoneinfo()
    plan = plan_snapshots(config, snaps_by_type, now or datetime.now(zone), problems)
    taken: dict[str, Stats] = {}

    def zfs_snapshot(name: str, recursive: bool) -> tuple[int, bytes]:
        """Runs `zfs snapshot [-r] name`: Perl's $? for it, and what zfs wrote to stderr. One that can't start is
        -1, as upstream's system() returns, after a line saying why."""
        try:
            proc = run_zfs_cmd([ZFS, "snapshot", *(["-r"] if recursive else []), name])
        except SanoidError as e:
            print(e, file=sys.stderr)
            return -1, b""
        return _wait_status(proc.returncode), proc.stderr

    def snapshot_failed(name: str, recursive: bool, status: int) -> None:
        """Reports a snapshot that wasn't made, in upstream's words, as a problem."""
        failure = f"zfs snapshot {'-r ' if recursive else ''}{name} failed, {status}"  # upstream's words
        print(f"CRITICAL ERROR: {failure}", file=sys.stderr)
        if problems is not None:
            problems.append(failure)

    if verbose:
        print("INFO: taking snapshots...")

    for path, pending in plan.items():
        dataset_cfg = config.get(path, {})
        recursive_flag = pending.recursive
        extra = " (zfs recursive)" if recursive_flag else ""
        turn = now or datetime.now(zone)  # P17: before the dataset's pre_snapshot_script runs

        env_base = {
            "SANOID_TARGET": path,
            "SANOID_TARGETS": path,
            "SANOID_TYPES": ",".join(pending.types),
        }

        presnapshot_failure = False
        if _perl_true(dataset_cfg.get("pre_snapshot_script")):
            snap_names = [_autosnap_name(turn, t) for t in pending.types]
            ret = run_hook_script(
                dataset_cfg, "pre_snapshot_script", path,
                {**env_base, "SANOID_SNAPNAME": snap_names[0], "SANOID_SNAPNAMES": ",".join(snap_names),
                 "SANOID_SCRIPT": "pre"},
                verbose=verbose, readonly=readonly, problems=problems,
            )
            if ret != 0:
                if dataset_cfg.get("no_inconsistent_snapshot"):
                    # DEV-028: a problem here, where upstream only warns
                    _report_problem(f"not taking {', '.join(pending.types)} snapshots of '{path}' this run: "
                                    + "pre_snapshot_script failed and no_inconsistent_snapshot is set", problems)
                    continue
                presnapshot_failure = True

        # P3: upstream's $snapshots[0] as its snapshot loop leaves it, for post_snapshot_script
        post_snapname = f"{path}@{_autosnap_name(turn, pending.types[0])}"
        for i, snap_type in enumerate(pending.types):
            snap_name = _autosnap_name(turn, snap_type)
            full = f"{path}@{snap_name}"
            if verbose:
                print(f"taking snapshot {full}{extra}")
            if readonly:
                continue

            status, complaint = zfs_snapshot(full, recursive_flag)
            if status != 0 and b"already exists" in complaint:  # in the bytes zfs wrote, as upstream looks
                # DEV-029: any type, any "already exists" collision; upstream retries only hourlies after a fall-back.
                full = re.sub(r"_([a-z]+)$", r"dst_\1", full)
                if i == 0:
                    post_snapname = full  # renamed before the retry runs, so even a failed retry counts
                if verbose:
                    print(f"taking dst snapshot {full}{extra}")
                status, _ = zfs_snapshot(full, recursive_flag)
            if status == 0:
                taken[full] = {"time": time.time(), "recursive": recursive_flag}
            else:
                snapshot_failed(full, recursive_flag, status)

        if (_perl_true(dataset_cfg.get("post_snapshot_script"))
                and (not presnapshot_failure or dataset_cfg.get("force_post_snapshot_script"))):
            snap_names = [_autosnap_name(turn, t) for t in pending.types]
            run_hook_script(
                dataset_cfg, "post_snapshot_script", path,
                # P3: "path@name" here, with the dst_ rename, but bare "name" for the pre hook, as upstream's are.
                {**env_base, "SANOID_SNAPNAME": post_snapname, "SANOID_SNAPNAMES": ",".join(snap_names),
                 "SANOID_SCRIPT": "post", "SANOID_PRE_FAILURE": "1" if presnapshot_failure else "0"},
                verbose=verbose, readonly=readonly, problems=problems,
            )

    return taken


def _perl_true(value: object) -> bool:
    """Perl's truth test, which upstream applies to every hook setting: undef, "" and "0" are false (P18)."""
    if isinstance(value, str):
        return value not in ("", "0")
    return bool(value)


def _perl_gt_zero(raw: str) -> bool:
    """Upstream's gate for whether a hook gets a timeout at all, `$timeout gt 0`: a string comparison, which `>` on str
    reproduces for text read with surrogateescape (P5). False for "0", blank, and whatever starts with whitespace, `+` or
    `-`; true for "05", "0x" and anything starting above '0'."""
    return raw > "0"


def _alarm_seconds(raw: str) -> int:
    """The timeout upstream's `alarm $timeout` sets for a script_timeout, in seconds, 0 for none: past the gate
    (_perl_gt_zero), Perl's number as the C int alarm() takes, which is its integer part cut to 32 bits (`5s` is 5,
    `1.5` is 1, `0.5` is 0, `4294967297` is 1), and none unless that is positive. A value Perl reads as Inf or NaN
    gives alarm() whatever number its variable last held, the previous hook's timeout; none here (P5)."""
    if not _perl_gt_zero(raw):
        return 0
    text = raw.rstrip(_WS)
    number = _perl_digits(text) if re.fullmatch(r"[0-9]+", text) else _perl_number(raw)
    if isinstance(number, int):
        iv = number - 2**64 if number >= 2**63 else number  # Perl's IV of a number past IV_MAX
    elif not math.isfinite(number):
        return 0
    else:
        iv = int(number) if number < 2.0**63 else int(number) - 2**64 if number < 2.0**64 else -1
    seconds = (iv + 2**31) % 2**32 - 2**31  # pp_alarm's `int anum = POPi`
    return max(seconds, 0)


def _script_timeout(cfg: SectionConfig, dataset: str, problems: list[str] | None) -> int:
    """A hook's script_timeout as upstream reads it (_alarm_seconds). P5, reported: a value that isn't a plain
    whole number (a trailing comment is fine, as in a retention count) is reported like a bad value, once a run, with
    how it is read. A missing one is warned about, as upstream's `use warnings` does, and sets none."""
    raw = cfg.get("script_timeout")
    if raw is None:
        print(f"WARN: 'script_timeout' is not set for '{dataset}'; running its hooks without a timeout", file=sys.stderr)
        return 0
    raw = str(raw)
    seconds = _alarm_seconds(raw)
    if not _COUNT_COMMENT_RE.fullmatch(raw):
        reading = f"reading it as {seconds}" if seconds else "running its hooks without a timeout"
        msg = f"'script_timeout' for '{dataset}' must be a whole number of seconds, got {raw!r}; {reading}"
        if problems is None or msg not in problems:
            _report_problem(msg, problems)
    return seconds


# Perl's shell metacharacters for a one-string system() (perl 5.34 doio.c, do_exec3)
_PERL_SHELL_METACHARS = "$&*(){}[]'\";\\|?<>~`\n"


def _hook_words(script: str) -> tuple[list[str], bool] | None:
    """How upstream's `system($script)` runs a hook command. None: through /bin/sh -c, when it holds a shell
    metacharacter, starts with `. ` or `exec `, or starts with `NAME=`. Otherwise Perl splits it on whitespace and
    execs the words itself, so a trailing `# comment` reaches the hook as arguments and `exit 0` or `:` can't start
    (P6): the words, and whether stderr joins stdout first. Perl drops one final newline, and
    does a final ` 2>&1` itself."""
    cmd = script.lstrip(_WS)
    if re.match(rf"(?:\.|exec)[{_WS}]|[A-Za-z0-9_]*=", cmd):
        return None
    joined = False
    for i, ch in enumerate(cmd):
        if ch not in _PERL_SHELL_METACHARS:
            continue
        if ch == "\n" and i == len(cmd) - 1:
            cmd = cmd[:i]
        elif cmd[i:i + 3] == ">&1" and i > 1 and cmd[i - 1] == "2" and cmd[i - 2] in _WS and not cmd[i + 3:].strip(_WS):
            cmd, joined = cmd[:i - 2], True
        else:
            return None
        break
    return [word for word in re.split(f"[{_WS}]+", cmd) if word], joined


def _run_hook(script: str, env: dict[str, str], timeout: int | None) -> int:
    """Runs a hook command as upstream's system() runs it (_hook_words) and returns its exit status as subprocess
    gives it. Words Perl execs itself are exec'd here too, so a command that can't be exec'd raises OSError where
    Perl's system() returns -1 (P6). A script with no #! is handed to /bin/sh, whose `exec`
    searches PATH and runs it as Perl's execvp does."""
    direct = _hook_words(script)
    if direct is None:
        argv = ["/bin/sh", "-c", script]
    elif not direct[0]:
        argv = ["/bin/sh", "-c", "exit 255"]  # Perl's child exits -1 with nothing to run
    else:
        words, joined = direct
        try:
            return subprocess.run(words, env=env, timeout=timeout, check=False,
                                  stderr=subprocess.STDOUT if joined else None).returncode
        except OSError as e:
            if e.errno != errno.ENOEXEC:
                raise
        argv = ["/bin/sh", "-c", f'exec "$0" "$@"{" 2>&1" if joined else ""}', *words]
    return subprocess.run(argv, env=env, timeout=timeout, check=False).returncode


def run_hook_script(cfg: SectionConfig, key: str, dataset: str, env_extra: dict[str, str], verbose: bool = False,
                    readonly: bool = False, problems: list[str] | None = None) -> int:
    """Runs the hook `key` names, as upstream's system() runs it (_run_hook, P6), with its timeout (_script_timeout, P5),
    and returns its exit status, 0 when it is unset, or -1 when it can't start (DEV-043).

    P18: unset when blank or "0"; with readonly, announced and not run; a failure warned about in upstream's words.
    P9: `env_extra` is set in pysanoid's own environment for the hook and deleted after it; with readonly it is only
    deleted.
    DEV-042: a hook that outlives script_timeout is killed and counts as failed."""
    script = cfg.get(key)
    if script is None or not _perl_true(script):
        return 0
    try:
        if verbose:
            print(f"executing {key} '{script}' on dataset '{dataset}'")
        if readonly:
            return 0
        timeout = _script_timeout(cfg, dataset, problems)
        try:
            os.environ.update(env_extra)  # a NUL byte in a value raises ValueError here
            returncode = _run_hook(str(script), dict(os.environ), timeout or None)
        except subprocess.TimeoutExpired:
            print(f"WARN: {key} didn't finish in the allowed time!", file=sys.stderr)
            return -1
        except (OSError, ValueError) as e:  # P18: can't start: -1, after a line saying why
            print(f"could not run '{script}': {e}", file=sys.stderr)
            print(f"WARN: {key} failed, -1", file=sys.stderr)
            return -1
        if returncode != 0:
            print(f"WARN: {key} failed, {_wait_status(returncode)}", file=sys.stderr)  # upstream's words
        return returncode
    finally:
        for name in env_extra:
            _ = os.environ.pop(name, None)


# ============================================================================
# Pruning
# ============================================================================

_PERIOD_SECONDS = {
    "frequently": None,  # depends on frequent_period, computed below
    "hourly": 60 * 60,
    "daily": 60 * 60 * 24,
    "weekly": 60 * 60 * 24 * 7,
    "monthly": 60 * 60 * 24 * 31,
    "yearly": 60 * 60 * 24 * 365.25,
}


def select_prune_candidates(
    cfg: SectionConfig, snap_type: str, sorted_snaps: Sequence[tuple[str, float]], now: float,
) -> list[str]:
    """The names to destroy among one type's snapshots, given oldest first as (name, ctime): each older than
    `count * period`, while more than `count` remain, as upstream decides. So "daily = 30" keeps at least 30 dailies,
    and none older than 30 days beyond those."""
    ctx = cfg.get("path", "?")
    # _cfg_count, not _cfg_int: a blank or negative count must not mean "prune everything"
    wanted = _cfg_count(cfg, snap_type, 0, ctx)
    period = (60 * _cfg_int(cfg, "frequent_period", 15, ctx)) if snap_type == "frequently" else _PERIOD_SECONDS[snap_type]
    # the ternary handles "frequently"; this only narrows the type for the checker
    assert period is not None
    max_age = now - wanted * period
    min_keep = wanted
    remaining = len(sorted_snaps)

    to_prune: list[str] = []
    for name, ctime in sorted_snaps:
        if ctime < max_age and remaining > min_keep:
            to_prune.append(name)
            remaining -= 1
    return to_prune


def _prune_defer_limit(cfg: SectionConfig) -> int:
    """The section's prune_defer, a whole number from 0 to 100; anything else, blank and missing included, raises
    (P19, DEV-008). Shared by check_prune_defer and the preflight, so the two read it alike."""
    ctx = cfg.get("path", "?")
    if "prune_defer" not in cfg:
        raise SanoidError(f"invalid prune_defer limit for '{ctx}': not set (sanoid.defaults.conf normally sets it to 0)")
    limit = _cfg_count(cfg, "prune_defer", 0, ctx, digits_only=True)
    if limit > 100:
        raise SanoidError(f"invalid prune_defer limit for '{ctx}': {cfg['prune_defer']} is not between 0 and 100!")
    return limit


def check_prune_defer(cfg: SectionConfig, pool_capacity_cache: dict[str, int], pool: str) -> bool:
    """True while the section's pool is no fuller than its prune_defer percent, so its pruning waits. Exactly "0"
    never defers and never asks the pool; "00" asks it (P19)."""
    limit = _prune_defer_limit(cfg)
    if str(cfg["prune_defer"]) == "0":
        return False
    if pool not in pool_capacity_cache:
        pool_capacity_cache[pool] = get_zpool_capacity(pool)
    return limit >= pool_capacity_cache[pool]


def _preflight_prune_config(config: dict[str, SectionConfig]) -> None:
    """Checks every setting prune reads, for every section it will act on, before the first destroy (DEV-011). It
    skips what prune skips, so a typo in a section prune never touches doesn't block the rest."""
    for section, cfg in config.items():
        if section.startswith("template") or not cfg.get("autoprune") or cfg.get("process_children_only"):
            continue
        ctx = cfg.get("path", section)
        _ = _prune_defer_limit(cfg)
        for snap_type in PERIOD_TYPES:
            _ = _cfg_count(cfg, snap_type, 0, ctx)
        if "frequently" in cfg:
            _ = _cfg_int(cfg, "frequent_period", 15, ctx)


def prune_snapshots(
    config: dict[str, SectionConfig], snaps: dict[str, dict[str, Snapshot]], snaps_by_type: dict[str, dict[str, Stats]],
    cache_dir: Path, run_dir: Path, *, readonly: bool = False, verbose: bool = False, problems: list[str] | None = None,
) -> list[str]:
    """Destroys what select_prune_candidates picks, section by section and type by type, and returns the names
    destroyed. Every setting is checked first (DEV-011); each batch is flushed from the cache as it goes, and what a
    busy lock deferred at the end (P20). A failed destroy is `could not remove` and the run goes on (P20); a batch
    whose lock file can't be made isn't pruned, and is a problem (DEV-041)."""
    if verbose:
        print("INFO: pruning snapshots...")

    _preflight_prune_config(config)

    pruned: list[str] = []
    unflushed: list[str] = []  # destroyed but still listed in the cache: upstream's %pruned
    pool_capacity_cache: dict[str, int] = {}

    # DEV-048: in config order, and each section's types yearly first, where upstream's are Perl's hash order
    for section, cfg in config.items():
        if section.startswith("template"):
            continue
        if not cfg.get("autoprune"):
            continue
        if cfg.get("process_children_only"):
            continue

        path = cfg["path"]
        # P19: the pool comes from the section's name, not its path
        pool = section.split("/", 1)[0]
        if check_prune_defer(cfg, pool_capacity_cache, pool):
            if verbose:
                print(f"INFO: deferring snapshot pruning ({section})...")
            continue

        # DEV-012: upstream reads hooks from $config{$dataset}, the section named after the dataset
        hook_cfg = config.get(path, {})
        # DEV-012: the section's own differing veto hook also runs, after it; either can veto
        veto_cfgs = [hook_cfg] if _perl_true(hook_cfg.get("pre_pruning_script")) else []
        if _perl_true(cfg.get("pre_pruning_script")) and cfg.get("pre_pruning_script") != hook_cfg.get("pre_pruning_script"):
            veto_cfgs.append(cfg)

        for snap_type in PERIOD_TYPES:
            if snap_type not in cfg:
                continue

            type_entry = snaps_by_type.get(path, {}).get(snap_type)
            if not type_entry:
                continue

            sorted_pairs = [(name, snaps[path][name].ctime) for name in type_entry["sorted"]]
            # P14: whole seconds, read per type as upstream reads time()
            now = int(time.time())
            to_prune = [f"{path}@{name}" for name in select_prune_candidates(cfg, snap_type, sorted_pairs, now)]

            if not to_prune:
                continue

            with try_lock(run_dir, "sanoid_pruning") as (held, error):
                if error is not None:
                    # DEV-041: fails closed; upstream destroys unlocked.
                    _report_problem(f"not pruning the {snap_type} snapshots of '{path}' this run: {error}", problems)
                    continue
                if not held:
                    if verbose:
                        print("INFO: deferring snapshot pruning - valid pruning lock held by other pysanoid process.")
                    continue

                for full_snap in to_prune:
                    dataset, snap_name = full_snap.split("@", 1)
                    env = {"SANOID_TARGET": dataset, "SANOID_SNAPNAME": snap_name}

                    if not readonly and any(
                        run_hook_script(c, "pre_pruning_script", dataset, env, verbose=verbose, problems=problems) != 0
                        for c in veto_cfgs
                    ):
                        continue

                    if verbose:
                        print(f"INFO: pruning {full_snap} ... ")

                    if not readonly:
                        try:
                            status = _wait_status(run_zfs_cmd([ZFS, "destroy", full_snap]).returncode)
                        except SanoidError as e:  # can't start: -1, after a line saying why (DEV-043)
                            print(e, file=sys.stderr)
                            status = -1
                        if status == 0:
                            pruned.append(full_snap)
                            unflushed.append(full_snap)
                            if _perl_true(hook_cfg.get("pruning_script")):
                                run_hook_script(hook_cfg, "pruning_script", dataset, {**env, "SANOID_SCRIPT": "prune"},
                                                verbose=verbose, problems=problems)
                        else:
                            print(f"could not remove {full_snap} : {status}", file=sys.stderr)  # upstream's words

            # P20: without waiting; a deferred batch stays in `unflushed`. DEV-013: no re-read of the listing after it.
            if remove_cached_snapshots(unflushed, cache_dir, run_dir, wait=False, verbose=verbose, problems=problems):
                unflushed.clear()

    # P20: the closing flush, waiting, and doing nothing when nothing is left
    _ = remove_cached_snapshots(unflushed, cache_dir, run_dir, wait=True, verbose=verbose, problems=problems)
    return pruned


# ============================================================================
# Cache maintenance
# ============================================================================

def add_cached_snapshots(taken: dict[str, Stats], cache_dir: Path, run_dir: Path, zfs: ZfsClient, verbose: bool = False,
                         problems: list[str] | None = None) -> None:
    """Appends the snapshots just taken to the snapshot cache, keeping its mtime (upstream's addcachedsnapshots).

    DEV-039c: a missing cache is left missing, where upstream dies. DEV-039b: one that can't be read, written or locked
    (DEV-041) is reported and removed (_drop_cache). DEV-039e: a recursive snapshot's children that can't be looked up
    are left out and reported. DEV-036: one cut short is left as it is, where upstream fuses its last line with the first new one."""
    if not taken:
        return
    cache = cache_dir / "snapshots.txt"
    with wait_lock(run_dir, "sanoid_cacheupdate", verbose=verbose) as (_, error):
        if error is not None:
            _drop_cache(cache, error, problems)
            return
        if verbose:
            print("INFO: adding taken snapshots to cache.")
        if not os.path.isfile(cache):
            return
        lines: list[str] = []
        for full_snap, details in taken.items():
            dataset, suffix = full_snap.split("@", 1)
            line = f"{dataset}@{suffix}\tcreation\t{int(details['time'])}\t-"
            lines.append(line)
            if details.get("recursive"):
                try:
                    children = zfs.get_child_datasets(dataset)
                except SanoidError as e:
                    _report_problem(f"{e}; the snapshot cache lacks the new snapshots of the children of '{dataset}' "
                                    + "until zfs is listed again", problems)
                    children = []
                for child in children:
                    lines.append(f"{child}@{suffix}\tcreation\t{int(details['time'])}\t-")

        try:
            existing_stat = cache.stat()
            existing = _read_text(cache)
        except OSError as e:
            _drop_cache(cache, e, problems)
            return
        # DEV-036: cut short, so no cache; ending its last line here would pass that line off as whole.
        if existing and not existing.endswith("\n"):
            return
        _ = _replace_cache(cache, existing + "\n".join(lines) + "\n", problems, existing_stat)


def remove_cached_snapshots(pruned: list[str], cache_dir: Path, run_dir: Path, wait: bool = False, verbose: bool = False,
                            problems: list[str] | None = None) -> bool:
    """Removes `pruned` from the snapshot cache, keeping its mtime (upstream's removecachedsnapshots). Returns False
    only when wait=False found the cache lock busy and wrote nothing, so the caller keeps the list for a later flush.
    Empty `pruned` does nothing.

    DEV-039c: a missing cache is left missing, where upstream writes an empty, fresh one. DEV-039b: one that can't be
    read, written or locked (DEV-041) is reported and removed (_drop_cache)."""
    if not pruned:
        return True
    cache = cache_dir / "snapshots.txt"
    pruned_set = set(pruned)

    def _do_removal() -> None:
        """Rewrites the cache without `pruned`'s lines; the caller holds the lock."""
        if verbose:
            print("INFO: removing destroyed snapshots from cache.")
        if not os.path.isfile(cache):
            return
        try:
            existing_stat = cache.stat()
            pieces = _read_text(cache).split("\n")
        except OSError as e:
            _drop_cache(cache, e, problems)
            return
        # P21: each kept line goes back as read, its name read with its newline still on
        lines = [piece + "\n" for piece in pieces[:-1]] + ([pieces[-1]] if pieces[-1] else [])
        kept = [line for line in lines if line.split("\t", 1)[0] not in pruned_set]
        _ = _replace_cache(cache, "".join(kept), problems, existing_stat)

    lock = wait_lock(run_dir, "sanoid_cacheupdate", verbose=verbose) if wait else try_lock(run_dir, "sanoid_cacheupdate")
    with lock as (held, error):
        if error is not None:
            _drop_cache(cache, error, problems)
        elif held:
            _do_removal()
        elif verbose:
            print("INFO: deferring cache update (snapshot removal) - valid cache update lock held by another pysanoid process.")
    return held or error is not None


# ============================================================================
# Pool capacity
# ============================================================================

def get_zpool_capacity(pool: str) -> int:
    """How full the pool is, in percent, from the first word of `zpool list -H -o cap`'s first line, as upstream reads
    it; one that isn't a capacity raises, in upstream's words. DEV-016: an answer holding more than that is refused."""
    proc = run_zfs_bytes([ZPOOL, "list", "-H", "-o", "cap", pool])
    _echo_zfs_stderr(proc)
    answer = proc.stdout.decode("utf-8", errors="surrogateescape")
    cap = answer.split("\n", 1)[0].split(" ", 1)[0]  # upstream's chomp, split(/ +/) and $row[0]
    # [0-9], not \d: Python's \d also matches other scripts' digits; upstream's does not
    if not re.fullmatch(r"[0-9]{1,3}%", cap):
        raise _CapacityError(f"'zpool list -H -o cap {pool}' command returned invalid capacity value ({cap})!")
    # DEV-016: only Perl's \s is nothing more; str.strip() would also trim NBSP, NEL and U+001C-001F
    if answer.strip(_WS) != cap:
        raise SanoidError(f"'zpool list -H -o cap {pool}' returned more than a capacity value ({answer!r})")
    return int(cap.rstrip("%"))


# ============================================================================
# main
# ============================================================================

class _PerlOutput(io.RawIOBase):
    """One of the process's standard streams, written as Perl's print and warn write theirs: a write that fails is
    dropped and the run goes on. The first failure is kept for main to report."""

    def __init__(self, name: str, fd: int) -> None:
        super().__init__()
        self.name: str = name
        self.fd: int = fd
        self.error: OSError | None = None

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return os.isatty(self.fd)

    def fileno(self) -> int:
        return self.fd

    def write(self, b: ReadableBuffer, /) -> int:
        try:
            return os.write(self.fd, b)
        except OSError as e:
            if self.error is None:
                self.error = e
            return memoryview(b).nbytes


def _perl_output() -> list[_PerlOutput]:
    """Points stdout and stderr at _PerlOutput writers (P7): UTF-8 with surrogateescape, so the bytes read
    from a config or zfs go out unchanged, whatever the locale; line-buffered, as Perl flushes STDOUT before every command
    it starts, so the journal has each line in order with what a hook or zfs prints. A stream without a descriptor (a
    test's) only gets surrogateescape and line buffering."""
    writers: list[_PerlOutput] = []
    for name in ("stdout", "stderr"):
        stream = sys.stdout if name == "stdout" else sys.stderr
        try:
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):  # no stream, or one without a descriptor
            if isinstance(stream, io.TextIOWrapper):
                _ = stream.reconfigure(errors="surrogateescape", line_buffering=True)
            continue
        with contextlib.suppress(OSError, ValueError):
            stream.flush()
        writer = _PerlOutput(name, fd)
        wrapper = io.TextIOWrapper(io.BufferedWriter(writer), encoding="utf-8", errors="surrogateescape",
                                   line_buffering=True)
        if name == "stdout":
            sys.stdout = wrapper
        else:
            sys.stderr = wrapper
        writers.append(writer)
    return writers


def main(argv: list[str] | None = None) -> int:
    """The program: reads the options and config, then takes, prunes or both, as upstream's main body does. Returns the
    exit status."""
    # as upstream, which appends these for older systems' zfs and zpool
    os.environ["PATH"] = os.environ.get("PATH", "") + ":/bin:/sbin"

    # P7: a line that can't be written is lost and the run carries on, and names go out as the bytes read
    output = _perl_output()

    try:
        args = parse_args(argv if argv is not None else sys.argv[1:])
    except SanoidError as e:  # a negative --cache-ttl, in upstream's words
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # DEV-025: used as given, leading whitespace and all; upstream's two-argument open() drops it.
    cache_dir = Path(args.cache_dir)
    run_dir = Path(args.run_dir)
    # DEV-039d, DEV-041: a directory that can't be made costs only the cache updates and locks that need it
    for directory in (cache_dir, run_dir):
        with contextlib.suppress(OSError):
            directory.mkdir(parents=True, exist_ok=True)

    # DEV-015: --cache-ttl=0 means 0; upstream's truth test keeps 1200.
    cache_ttl = args.cache_ttl if args.cache_ttl is not None else 1200

    # what went unprotected this run; non-empty, the run exits 1 once everything else has run (DEV-028)
    problems: list[str] = []

    try:
        zfs_client = ZfsClient(cache_dir, cache_ttl, args.force_update, debug=args.debug)
        config = load_config(args.configdir, zfs_client)
        zfs_client.flush_dataset_cache(run_dir, verbose=args.verbose, problems=problems)

        needs_snapshot_data = (
            args.cron or args.take_snapshots or args.prune_snapshots or args.force_update or args.debug
        )

        snaps: dict[str, dict[str, Snapshot]] = {}
        by_type: dict[str, dict[str, Stats]] = {}
        if needs_snapshot_data:
            snaps = get_snapshots(config, cache_dir, run_dir, cache_ttl, args.force_update, args.verbose,
                                  debug=args.debug, problems=problems)
            by_type = snapshots_by_type(snaps, config)

        if args.debug:
            args.verbose = True  # upstream sets it just before the dump, so take and prune below are verbose too
            print_debug_dump(config, by_type, snapshots_by_path(snaps, config))

        # DEV-051: a snapshot dated well ahead of the clock neither holds take back nor fills one of prune's places
        snaps = without_snapshots_dated_ahead(snaps)
        by_type = snapshots_by_type(snaps, config)

        def do_take():
            """Takes what is due, and adds it to the cache."""
            taken = take_snapshots(
                config, by_type, readonly=args.readonly, verbose=args.verbose, problems=problems,
            )
            add_cached_snapshots(taken, cache_dir, run_dir, zfs_client, verbose=args.verbose, problems=problems)

        def do_prune():
            """Prunes, flushing the cache itself (P20)."""
            _ = prune_snapshots(config, snaps, by_type, cache_dir, run_dir, readonly=args.readonly, verbose=args.verbose,
                                problems=problems)

        if args.cron:
            verbose = args.verbose and not args.quiet
            args.verbose = verbose
            do_take()
            do_prune()
        else:
            if args.take_snapshots:
                do_take()
            if args.prune_snapshots:
                do_prune()
    except SanoidError as e:
        print(f"{e.prefix}: {e}", file=sys.stderr)
        return 1

    for writer in output:  # upstream exits 1 when its last flush of stdout fails, and not for a lost stderr line
        if writer.name == "stdout" and writer.error is not None:
            _report_problem(f"could not write to stdout ({writer.error}); the run carried on without it", problems)

    # DEV-028, DEV-039b: non-zero when anything went unprotected, once pruning has run
    if problems:
        print(f"ERROR: {len(problems)} problem(s) this run (see above); exiting non-zero so systemd shows the failure.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
