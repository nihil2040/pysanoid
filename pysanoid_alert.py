#!/usr/bin/env python3
"""
pysanoid_alert - mails TrueNAS's administrators when pysanoid needs attention

Not part of the Sanoid port: pysanoid.py neither calls nor needs this file, and nothing here takes, destroys or
changes a snapshot.

  failure UNIT   The handler systemd starts when a pysanoid unit fails: pysanoid-alert@.service, which the units name
                 with OnFailure=. Mails the failed run's log.

  freshness      A watchdog for a TrueNAS Cron Job, which outlives what a unit can't report: an update that removed
                 the units, a timer that stopped firing, a run skipped because its condition wasn't met. Mails when a
                 snapshot type pysanoid is configured to take has no recent snapshot, or when the check can't run.
                 Exits 0 when every type is fresh, 1 otherwise.

TrueNAS sends the mail (`midclt call mail.send`): by its own mail settings, to every local administrator with an
e-mail address, the subject prefixed with "TrueNAS <host>: ". So there is no address or password to keep here. Both
commands run as root, which midclt needs. --no-mail prints the mail instead of sending it.

The Cron Job runs `pysanoid_alert.py freshness --configdir=DIR` as root, with Hide Standard Output checked (the report
is mailed from here) and Hide Standard Error unchecked (a check that broke or couldn't mail is then in the job's log,
and TrueNAS mails it to root when root has an address).

freshness, in detail
--------------------
What should exist comes from pysanoid: the snapshots it would take of a pool that had none (plan_snapshots), so every
rule about which sections and counts take snapshots stays pysanoid's own. What does exist is asked of zfs directly,
not read from pysanoid's snapshot cache or through its listing code: a watchdog shouldn't share those with what it
watches. A type is stale when its newest snapshot is older than LIMITS allows, or missing, or dated in the future,
which says the clock went wrong: pysanoid sets such a snapshot aside until the clock passes it (DEV-051). Under
`recursive = zfs` only the parent is checked: one `zfs snapshot -r` makes its children's snapshots too. A dataset
section whose name starts with "template" is reported as well: pysanoid, as upstream, never takes or prunes its
snapshots (expected_types).

The check has _CHECK_TIMEOUT to finish. zfs can hang where it can't be killed (on a suspended pool, say); the check
then reports that it couldn't finish, and exits without it.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

MIDCLT = shutil.which("midclt") or "midclt"
JOURNALCTL = shutil.which("journalctl") or "journalctl"
ZFS = shutil.which("zfs") or "zfs"

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR

# The oldest a type's newest snapshot may be: one period, and room for a late or slow run. A run takes a due
# frequently snapshot whatever frequent_period is, so an hourly timer makes one an hour.
LIMITS = {
    "yearly": 367 * _DAY,
    "monthly": 32 * _DAY,
    "weekly": 8 * _DAY,
    "daily": 28 * _HOUR,
    "hourly": 90 * _MINUTE,
    "frequently": 90 * _MINUTE,
}

# pysanoid's own snapshots: autosnap_<date>_<time>_<type>, or ...dst_<type> for the hour a clock change repeats
_AUTOSNAP_RE = re.compile(rf"autosnap_.*_({'|'.join(LIMITS)})")
_INVOCATION_RE = re.compile(r"[0-9a-f]{32}")

_RUN_LOG_LINES = 200  # of the failed run, when systemd says which run it was
_UNIT_LOG_LINES = 50  # of the unit, earlier runs included, when it doesn't
_MAX_LINE = 2_000  # characters kept of one line
_MAX_PAYLOAD = 100_000  # bytes of mail.send's message
_MAIL_TIMEOUT = 400  # seconds; mail.send itself gives the mail server 300
_CHECK_TIMEOUT = 15 * _MINUTE  # for the whole freshness check
_CLOCK_SLACK = 5 * _MINUTE  # how far ahead of the clock a newest snapshot may be dated: room for a clock set right by a step

# One mail: its subject, the lines always kept, and the lines a mail too long loses from the top.
Mail = tuple[str, list[str], list[str]]


class AlertError(Exception):
    """Something this script needs could not be done; the message says what."""


def _run(cmd: list[str], what: str, timeout: int, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """One command, given `stdin` if any, its output captured as text. One that can't start or doesn't finish is an
    AlertError naming `what`, not the command line."""
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                              check=False)
    except subprocess.TimeoutExpired:
        raise AlertError(f"{what} did not finish in {timeout} seconds") from None
    except (OSError, ValueError) as e:
        raise AlertError(f"could not run {what}: {e}") from e


# ============================================================================
# Mail
# ============================================================================

def _payload(mail: Mail) -> str:
    """mail.send's message as the one JSON argument midclt takes. A mail longer than _MAX_PAYLOAD loses lines from the
    top of its last part: a log's last lines say how the run ended."""
    subject, head, tail = mail
    head = [line[:_MAX_LINE] for line in head]
    tail = [line[:_MAX_LINE] for line in tail]
    dropped = 0
    while True:
        kept = tail[dropped:]
        note = [f"[{dropped} earlier lines left out]"] if dropped else []
        payload = json.dumps({"subject": subject, "text": "\n".join([*head, *note, *kept])})  # ASCII: a byte a character
        if len(payload) <= _MAX_PAYLOAD or not kept:
            return payload
        dropped += (len(kept) + 1) // 2


# Runs midclt (argv[1], found on PATH) with the rest of argv and, last, the mail read from stdin. A process's command
# line is there for every local user to read (ps, /proc/PID/cmdline); sys.argv changed once the process runs is not.
# midclt is a Python script, run here by runpy as its #! would run it: it reads the mail, sends it and exits as it does
# when the mail is an argument.
_MIDCLT_FROM_STDIN = """\
import runpy, shutil, sys
path = shutil.which(sys.argv[1])
if path is None:
    sys.exit(f"{sys.argv[1]} was not found on PATH")
sys.argv = [path, *sys.argv[2:], sys.stdin.read()]
runpy.run_path(path, run_name="__main__")
"""


def send_mail(mail: Mail) -> None:
    """Has TrueNAS mail its local administrators, and waits for it. AlertError when the mail isn't sent: no
    administrator has an address, the mail settings or server are wrong, or middlewared isn't running."""
    cmd = [sys.executable, "-I", "-c", _MIDCLT_FROM_STDIN, MIDCLT, "call", "-j", "-jp", "description", "mail.send"]
    proc = _run(cmd, "midclt call mail.send", _MAIL_TIMEOUT, _payload(mail))
    if proc.returncode != 0:
        said = [line for line in proc.stderr.splitlines() if line.strip()]  # midclt prints the error, then a traceback
        if said and said[0] == "Traceback (most recent call last):":
            said = said[-1:]  # one midclt didn't catch, or a midclt that isn't Python: the last line says what
        raise AlertError("TrueNAS did not send the mail: " + (said[0] if said else f"midclt exit status {proc.returncode}"))


def _mailable(text: str) -> str:
    """Text a mail and a terminal can hold. pysanoid keeps a byte that isn't UTF-8 as it read it, in a section's name
    or in what zfs said; here it reads U+FFFD."""
    return text.encode("utf-8", errors="surrogateescape").decode("utf-8", errors="replace")


def deliver(mail: Mail, no_mail: bool) -> bool:
    """Sends the mail, or prints it for --no-mail. False when it couldn't be sent: stderr then has it, so the journal
    or the Cron Job's log does."""
    subject, head, tail = mail = (_mailable(mail[0]), [_mailable(line) for line in mail[1]], [_mailable(line) for line in mail[2]])
    report = "\n".join([f"Subject: {subject}", "", *head, *tail])
    if no_mail:
        print(report)
        return True
    try:
        send_mail(mail)
    except AlertError as e:
        print(f"pysanoid_alert: {e}\n\n{report}", file=sys.stderr)
        return False
    print(f"mailed: {subject}")
    return True


# ============================================================================
# failure: a unit's OnFailure= handler
# ============================================================================

def unit_log(unit: str, invocation: str | None) -> list[str]:
    """The failed run's log: that run's own lines when systemd names it ($MONITOR_INVOCATION_ID, systemd 251 and later),
    else the unit's latest. A journal that can't be read is one line saying so, and the mail still goes out."""
    with contextlib.suppress(AlertError):  # the run's last lines may not have reached the journal yet
        _ = _run([JOURNALCTL, "--sync"], "journalctl --sync", 30)
    if invocation is not None and _INVOCATION_RE.fullmatch(invocation):
        # the unit's own output, and what systemd logged about that run
        select = ["-n", str(_RUN_LOG_LINES), f"_SYSTEMD_INVOCATION_ID={invocation}", "+", f"INVOCATION_ID={invocation}"]
    else:
        select = ["-n", str(_UNIT_LOG_LINES), f"--unit={unit}"]
    try:
        proc = _run([JOURNALCTL, "--no-pager", "-o", "short-iso", *select], "journalctl", 60)
    except AlertError as e:
        return [f"({e})"]
    if proc.returncode != 0:
        return [f"(journalctl exit status {proc.returncode}: {proc.stderr.strip()})"]
    return proc.stdout.splitlines() or ["(the journal has no lines for it)"]


def failure_mail(unit: str, env: Mapping[str, str]) -> Mail:
    """The mail for a failed unit. systemd describes the failure to an OnFailure= handler in $MONITOR_*."""
    result = env.get("MONITOR_SERVICE_RESULT")
    code, status = env.get("MONITOR_EXIT_CODE"), env.get("MONITOR_EXIT_STATUS")
    how = ""
    if result:
        how = f" ({result}: {code}, status {status})" if code and status else f" ({result})"
    return f"{unit} failed", [f"{unit} failed{how}. Its log:", ""], unit_log(unit, env.get("MONITOR_INVOCATION_ID"))


# ============================================================================
# freshness: is pysanoid still taking the snapshots sanoid.conf asks for?
# ============================================================================

def expected_types(configdir: str) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    """{dataset: the snapshot types pysanoid takes of it}; {dataset: the types it would take but for a section name
    starting with "template"}; and what pysanoid said while reading sanoid.conf.

    Take and prune skip every section whose name starts with "template", as upstream's do, while only `template_` ones
    are read as templates: `[templates/vm]` loads, and is never snapshotted. Planned under another name, such a section
    gives the types pysanoid would take; one that another section takes of the same dataset isn't counted."""
    sys.dont_write_bytecode = True  # no __pycache__ beside pysanoid.py
    import pysanoid  # here, so that `failure` still mails when pysanoid.py can't be loaded

    said = io.StringIO()
    with contextlib.redirect_stderr(said):
        # no cache directory, and force_update: a recursive section's children are asked of zfs
        zfs = pysanoid.ZfsClient(Path(os.devnull), cache_ttl=0, force_update=True)
        config = pysanoid.load_config(configdir, zfs)
        plan = pysanoid.plan_snapshots(config, {})
    expected = {path: list(pending.types) for path, pending in plan.items()}
    with contextlib.redirect_stderr(io.StringIO()):  # pysanoid never plans these, so never says this of them
        renamed = {f"skipped {name}": cfg for name, cfg in config.items() if name.startswith("template")}
        hidden = pysanoid.plan_snapshots(renamed, {})
    skipped = {path: types for path, pending in hidden.items()
               if (types := [t for t in pending.types if t not in expected.get(path, [])])}
    return expected, skipped, said.getvalue().splitlines()


def newest_snapshots(datasets: list[str]) -> tuple[dict[tuple[str, str], int], list[str]]:
    """{(dataset, type): when its newest pysanoid snapshot was made}, and zfs's complaints, such as a dataset that
    doesn't exist. What zfs did list is used either way."""
    proc = _run([ZFS, "list", "-H", "-p", "-d", "1", "-t", "snapshot", "-o", "name,creation", *datasets], "zfs list", 600)
    newest: dict[tuple[str, str], int] = {}
    for line in proc.stdout.split("\n"):
        name, _, creation = line.rpartition("\t")
        dataset, _, snapshot = name.partition("@")
        match = _AUTOSNAP_RE.fullmatch(snapshot)
        if match and creation.isascii() and creation.isdigit():
            key = (dataset, match.group(1))
            newest[key] = max(newest.get(key, 0), int(creation))
    complaints = [line for line in proc.stderr.split("\n") if line.strip()]
    if proc.returncode != 0 and not complaints:
        complaints = [f"zfs list exit status {proc.returncode}"]
    return newest, complaints


def _age(seconds: int) -> str:
    """An age to its two largest units: 40d 3h, 1h 30m, 1d, 12m."""
    days, rest = divmod(seconds, _DAY)
    hours, rest = divmod(rest, _HOUR)
    minutes = rest // _MINUTE
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def stale_types(expected: dict[str, list[str]], newest: dict[tuple[str, str], int], now: int) -> dict[str, list[str]]:
    """{dataset: one line for each of its types with no snapshot recent enough, or whose newest is dated in the future},
    the datasets in order."""
    stale: dict[str, list[str]] = {}
    for dataset in sorted(expected):
        for snap_type in expected[dataset]:
            made = newest.get((dataset, snap_type))
            if made is None:
                stale.setdefault(dataset, []).append(f"{dataset}: no {snap_type} snapshot")
            elif made - now > _CLOCK_SLACK:
                stale.setdefault(dataset, []).append(
                    f"{dataset}: newest {snap_type} snapshot is dated {_age(made - now)} in the future: pysanoid sets it "
                    + "aside, neither counting nor destroying it, until the clock passes it")
            elif now - made > LIMITS[snap_type]:
                stale.setdefault(dataset, []).append(
                    f"{dataset}: newest {snap_type} snapshot is {_age(now - made)} old (limit {_age(LIMITS[snap_type])})")
    return stale


def _could_not_run(why: str) -> Mail:
    return ("pysanoid: the snapshot freshness check could not run",
            ["The check could not compare the snapshots with sanoid.conf:", ""], [why])


def freshness_mail(configdir: str) -> Mail | str:
    """The mail when a type is stale or the check can't run; when every type is fresh, a line saying so."""
    try:
        expected, skipped, said = expected_types(configdir)
        newest, complaints = newest_snapshots(sorted(expected)) if expected else ({}, [])
        stale = stale_types(expected, newest, int(time.time()))
    except Exception as e:  # noqa: BLE001 - a watchdog that can't look says so, whatever stopped it
        return _could_not_run(f"{type(e).__name__}: {e}")

    for dataset, types in skipped.items():
        stale.setdefault(dataset, []).append(
            f"{dataset}: pysanoid never takes its {', '.join(types)} snapshots: take and prune skip a section whose "
            + 'name starts with "template", as upstream sanoid does')
    stale = dict(sorted(stale.items()))

    if not expected and not stale:
        return "OK: sanoid.conf has no dataset pysanoid takes snapshots of"
    if not stale:
        types = _count(sum(map(len, expected.values())), "snapshot type")
        return f"OK: {types} on {_count(len(expected), 'dataset')}, none stale"

    lines = [line for dataset in stale for line in stale[dataset]]
    if complaints:
        lines += ["", "zfs said:", *complaints]
    if said:
        lines += ["", "pysanoid said, reading sanoid.conf:", *said]
    lines += ["", "Start with: systemctl status pysanoid.timer pysanoid.service"]
    return (f"pysanoid: stale snapshots on {_count(len(stale), 'dataset')}",
            ["pysanoid is not taking these snapshots on schedule:", ""], lines)


def _give_up(no_mail: bool) -> NoReturn:
    """The freshness check's deadline. A zfs that can't be killed can't be waited for either, and Python waits for
    every command it started: report, then exit at once."""
    _ = deliver(_could_not_run(f"It did not finish in {_age(_CHECK_TIMEOUT)}. zfs may be hanging: see zpool status."),
                no_mail)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)


# ============================================================================
# Command line
# ============================================================================

def parse_args(argv: list[str]) -> argparse.Namespace:
    common = argparse.ArgumentParser(add_help=False)
    _ = common.add_argument("--no-mail", action="store_true", help="print the mail instead of sending it")
    p = argparse.ArgumentParser(description="Mails TrueNAS's administrators when pysanoid needs attention.")
    commands = p.add_subparsers(dest="command", required=True)
    failure = commands.add_parser("failure", parents=[common], help="mail a failed unit's log (systemd OnFailure=)")
    _ = failure.add_argument("unit")
    freshness = commands.add_parser("freshness", parents=[common],
                                    help="mail when a snapshot type pysanoid takes has no recent snapshot")
    _ = freshness.add_argument("--configdir", default="/etc/sanoid")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    no_mail: bool = args.no_mail

    if args.command == "failure":
        return 0 if deliver(failure_mail(args.unit, os.environ), no_mail) else 1

    _ = signal.signal(signal.SIGALRM, lambda _signum, _frame: _give_up(no_mail))
    _ = signal.alarm(_CHECK_TIMEOUT)
    result = freshness_mail(args.configdir)
    _ = signal.alarm(0)
    if isinstance(result, str):
        print(result)
        return 0
    _ = deliver(result, no_mail)
    return 1


if __name__ == "__main__":
    sys.exit(main())
