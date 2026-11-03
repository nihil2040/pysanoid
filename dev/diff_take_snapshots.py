#!/usr/bin/env python3
"""Differential test: upstream's take_snapshots (Module 6), extracted verbatim along with get_date,
vs pysanoid's plan_snapshots + take_snapshots.

This is the OTHER destructive/protective core function (alongside prune_snapshots -- see
diff_prune.py), and the gnarliest one: upstream decides "is a {daily,weekly,...} snapshot of this
dataset due" by building a candidate wall-clock time from $datestamp (localtime(time)) and manual
epoch-seconds arithmetic, one $config{...} field at a time. pysanoid's _last_preferred_time
deliberately does NOT replicate that arithmetic bug-for-bug -- see its own docstring in pysanoid.py
for the full, already-quantified explanation (upstream's flat epoch-second subtraction silently
mis-schedules daily/weekly/monthly/yearly around DST transitions; pysanoid's zoneinfo-based
approach doesn't have the bug, on purpose, confirmed against a real Perl interpreter 2026-09-16).

Given that KNOWN, ALREADY-DOCUMENTED divergence exists specifically around DST transitions, this
harness is scoped in two parts rather than one blind fuzz:
  1. The fast, large-N fuzz loop (gen()) only ever picks a "now" solidly in the middle of a season
     (see SAFE_MONTHS) -- nowhere near a transition, in several timezones (including two Southern-
     hemisphere and one no-DST zone). Everywhere away from a transition there is NO known reason
     for the two algorithms to disagree, so this is real, large-scale regression coverage: exact
     due-decision parity, exact snapshot naming, exact recursive-flag propagation, the dst_ retry
     mechanic (forced directly via a controlled "already exists" zfs failure, decoupled from
     needing a real transition to land exactly right), and hook invocation/env vars.
  2. A handful of explicit HAND cases sit exactly ON the two 2026 US DST transitions (2026-03-08
     spring-forward, 2026-11-01 fall-back) to CONFIRM the documented divergence is exactly what's
     documented -- not something worse -- the same role verify_upstream_alarm_crashes_process plays
     in dev/diff_hooks.py: a live check that stays true, not a one-time comment. Three more run
     upstream's own dst_ retry on a real fall-back day, where its $handleDst gate is armed, to pin
     post_snapshot_script's SANOID_SNAPNAME after a dst_ rename.

Two further, orthogonal, ALREADY-documented deviations (DEV-028, DEV-029) are
canonicalized rather than chased everywhere they'd otherwise show up as a false DIFF: a failed
`zfs snapshot` warns and continues upstream (exit 0) vs is tracked loudly here (a nonzero exit);
and the dst_ retry fires for ANY "already exists" collision on ANY type here, not only hourlies on
a day DST ended (upstream's $handleDst gate).

Compared on both sides: what was taken, every `zfs snapshot` call, every hook run with the SANOID_* env it
saw, the verbose stdout, and on stderr zfs's own words, the CRITICAL ERROR lines and a failed hook's warning (grouped
by dataset: upstream's dataset order is Perl's random hash order). Left out of stderr, each a documented difference:
Perl's ` at <script> line N.` and its own warnings, and pysanoid's own reports. A scenario's fail_start makes one `zfs snapshot` or hook fail to start, as a failed
fork does: a system() override on the Perl side, a subprocess.run patch on this one.

Both sides run on the scenario's clock, pysanoid through the path a real run takes (no `now=`). With a `tick`
the clock moves that many seconds for every zfs command and hook that has run, so the time in a snapshot's
name shows when the clock was read for it. Each side's names are first checked against upstream's rule (see
_name_times), then rewritten to the scenario's own time, since the two take the datasets in different orders.
A scenario's `noise` is a warning zfs prints on every snapshot, in bytes that needn't be UTF-8.

runscript/checklock/writelock/removelock are stubbed the same way every other target in this tool
stubs them; getsnaps/addcachedsnapshots (called only when something was actually taken) are logged,
not executed for real -- cache mutation and enumeration are diff_snapshot_cache_mutation.py's and
diff_snapshot_enumeration.py's targets, not this one's.

Run: python3 dev/diff_take_snapshots.py hand
     python3 dev/diff_take_snapshots.py fuzz <n> <seed>
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.extract import extract_subs
from lib.perlrun import (
    PERL_JSON_HEAD,
    READ_SCENARIO_PERL,
    WRITE_RESULT_PERL,
    build_harness,
    run_harness,
)

import pysanoid as p

WORK = DEV_DIR / "work" / "take_snapshots"
os.environ["PATH"] = f"{DEV_DIR / 'fake_zfs'}{os.pathsep}{os.environ['PATH']}"
HOOK_PROBE = DEV_DIR / "fake_hook" / "hook_probe"

HEAD = PERL_JSON_HEAD + r'''
use Time::Local;
use POSIX ();
# FAKE_TICK: the seconds each zfs command or hook takes on this clock, which counts them in their two logs
BEGIN {
    *CORE::GLOBAL::time = sub () {
        my $now = $ENV{FAKE_NOW} + 0;
        return $now unless $ENV{FAKE_TICK};
        foreach my $log ($ENV{FAKE_ZFS_LOG}, $ENV{HOOK_LOG}) {
            open(my $fh, '<', $log) or next;
            my $ran = () = <$fh>;
            close $fh;
            $now += $ENV{FAKE_TICK} * $ran;
        }
        return $now;
    };
}
# FAIL_START makes one system() call fail to start, as a failed fork does: it returns -1 with $? = -1.
# "snapshot:<prefix>" picks the first `zfs snapshot` of a name starting with <prefix>; "hook:<dataset>"
# the first hook run while SANOID_TARGET is <dataset>.
BEGIN {
    my $done = 0;
    *CORE::GLOBAL::system = sub {
        my ($kind, $arg) = split /:/, ($ENV{FAIL_START} // ''), 2;
        if (!$done && $kind) {
            my $hit = $kind eq 'snapshot' ? ((grep { defined $_ && $_ eq 'snapshot' } @_) && (grep { defined $_ && index($_, $arg) == 0 } @_))
                    : $kind eq 'hook' ? (@_ == 1 && ($ENV{SANOID_TARGET} // '') eq $arg)
                    : 0;
            if ($hit) { $done = 1; $! = POSIX::EAGAIN(); $? = -1; return -1; }
        }
        return CORE::system(@_);
    };
}
my %args = ('verbose' => 1, 'debug' => 0, 'readonly' => 0);
my $zfs = 'zfs';
my $cacheTTL = 1200;
my (%config, %snaps, %snapsbytype, %taken);
my @__getsnaps_log;
my @__flush_log;
sub tee_stderr (&) {
    my $code = shift;
    my $tmp = $ENV{TEE_STDERR_TMP};
    open(my $save_err, '>&', \*STDERR) or die "dup STDERR: $!";
    open(STDERR, '>', $tmp) or die "redirect STDERR: $!";
    my $ret = $code->();
    close STDERR;
    open(STDERR, '>&', $save_err) or die "restore STDERR: $!";
    local $/;
    open(my $rf, '<', $tmp) or die "read tmp stderr: $!";
    my $captured = <$rf>;
    close $rf;
    print STDERR $captured if defined $captured;  # a tee: what was captured also reaches the terminal
    return (defined($captured) ? $captured : '', $ret);
}
sub getsnaps { push @__getsnaps_log, 1; return (); }
sub addcachedsnapshots { push @__flush_log, 1; }
sub get_zpool_capacity { return 0; }
'''

TAIL = f'''
{READ_SCENARIO_PERL}
%config = %{{ $__scn->{{config}} }};
%snapsbytype = %{{ $__scn->{{snapsbytype}} }};
$args{{verbose}} = $__scn->{{verbose}} ? 1 : 0;
$args{{readonly}} = $__scn->{{readonly}} ? 1 : 0;
my %pruned;  # take_snapshots does not touch this, but shares file scope with prune_snapshots upstream
my $died = '';
eval {{ take_snapshots(\\%config, {{}}, \\%snapsbytype, {{}}); 1 }} or do {{ ($died = $@) =~ s/\\s+$//; }};
my %__out;
foreach my $snap (keys %taken) {{ $__out{{$snap}} = {{ recursive => ($taken{{$snap}}{{recursive}} ? 1 : 0) }}; }}
my %__result = ( died => $died, taken => \\%__out, getsnaps_called => (scalar(@__getsnaps_log) > 0 ? 1 : 0),
                 flush_called => (scalar(@__flush_log) > 0 ? 1 : 0) );
{WRITE_RESULT_PERL}
'''

SCRIPT = build_harness(HEAD, extract_subs("take_snapshots", "get_date", "runscript"), TAIL)


def _script_for(tag: str, exit_code: int) -> str:
    return f"{HOOK_PROBE} {tag} {exit_code}"


def _setup(wd: Path, scn: dict) -> None:
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True)
    (wd / "zfs_state.json").write_text(json.dumps({
        "datasets": scn["tree"], "snapshot_fail": scn.get("snapshot_fail", {}), "noise": scn.get("noise", ""),
    }))
    (wd / "zfs.log").write_text("")
    (wd / "hook.log").write_text("")


def _canon_stdout(text: str, paths: list[str]) -> list[str]:
    """Upstream takes datasets in Perl's random hash order, pysanoid in config order: group the lines by the
    dataset they name (a stable sort, so each dataset's own lines keep their order) before comparing."""
    def owner(line: str) -> str:
        hits = [p for p in paths if f"{p}@" in line or f"'{p}'" in line]
        return max(hits, key=len) if hits else ""
    return sorted(text.splitlines(), key=owner)


_STAMP = "%Y-%m-%d_%H:%M:%S"
_STAMP_RE = re.compile(r"autosnap_(\d{4}-\d\d-\d\d_\d\d:\d\d:\d\d)")
_FULL_RE = re.compile(rf"([^\s@',]+)@{_STAMP_RE.pattern}")


def _clock(wd: Path, scn: dict) -> float:
    """The scenario's clock as HEAD's time() reads it: now_epoch, and `tick` seconds for every zfs command and hook
    that has run."""
    tick = scn.get("tick", 0)  # .get: dev/simulate_long_horizon.py builds scenarios of its own, without the newer keys
    if not tick:
        return float(scn["now_epoch"])
    ran = sum(len((wd / log).read_text().splitlines()) for log in ("zfs.log", "hook.log"))
    return float(scn["now_epoch"] + tick * ran)


def _hook_sort(hook_log: list[dict]) -> list[dict]:
    return sorted(hook_log, key=lambda e: json.dumps(e, sort_keys=True))


def _name_times(scn: dict, res: dict) -> str:
    """Checks one side's snapshot names against upstream's rule, then rewrites them to the scenario's own time, so
    that the two sides compare whichever order each took the datasets in. The rule: a dataset's names all carry one
    time, the clock's as its turn came. Every zfs command and hook that ran took `tick` seconds, so with the datasets
    sorted by that time, each one's is the scenario's plus `tick` for everything run for the datasets before it.
    Returns what breaks the rule ("" for nothing), and then leaves the names as they are."""
    named: list[tuple[str, str]] = []  # (dataset, the time in a name it was given)
    ran: dict[str, int] = {}
    for name, _ in res["snapshot_calls"]:
        ran[name.split("@", 1)[0]] = ran.get(name.split("@", 1)[0], 0) + 1
    for entry in res["hook_log"]:
        target = entry["env"].get("SANOID_TARGET", "")
        ran[target] = ran.get(target, 0) + 1
        named += [(target, stamp) for key in ("SANOID_SNAPNAME", "SANOID_SNAPNAMES")
                  for stamp in _STAMP_RE.findall(entry["env"].get(key, ""))]
    for text in [*res["taken"], *(name for name, _ in res["snapshot_calls"]), *res["stdout"], *res["stderr"]]:
        named += _FULL_RE.findall(text)
    times: dict[str, set[str]] = {}
    for dataset, stamp in named:
        times.setdefault(dataset, set()).add(stamp)
    zone = ZoneInfo(scn["tz"])
    before = 0
    for dataset in sorted(times, key=lambda d: (min(times[d]), ran.get(d, 0) > 0)):
        turn = datetime.fromtimestamp(scn["now_epoch"] + scn.get("tick", 0) * before, tz=zone).strftime(_STAMP)
        if times[dataset] != {turn}:
            return f"{dataset}'s names carry {sorted(times[dataset])}; its turn came at {turn}"
        before += ran.get(dataset, 0)
    own = "autosnap_" + datetime.fromtimestamp(scn["now_epoch"], tz=zone).strftime(_STAMP)

    def fix(text: str) -> str:
        return _STAMP_RE.sub(own, text)

    res["taken"] = {fix(name): v for name, v in res["taken"].items()}
    res["snapshot_calls"] = sorted((fix(name), recursive) for name, recursive in res["snapshot_calls"])
    res["hook_log"] = _hook_sort([{**e, "env": {k: fix(v) for k, v in e["env"].items()}} for e in res["hook_log"]])
    res["stdout"] = [fix(line) for line in res["stdout"]]
    res["stderr"] = [fix(line) for line in res["stderr"]]
    return ""


_PERL_AT = re.compile(r" at \S+ line \d+(?:, <\S+> (?:line|chunk) \d+)?\.$")  # what Perl's warn adds
# not compared: Perl's own warnings about upstream's code, and what pysanoid reports itself
_NOT_SHARED = re.compile(r"WARN: 'script_timeout' is not set |ERROR: |could not run |Can't exec |"
                         r"Use of uninitialized value |Argument \".*\" isn't numeric ")


def _canon_stderr(text: str, paths: list[str]) -> list[str]:
    """zfs's own words, the CRITICAL ERROR lines and the hook warnings of one side's stderr, grouped by dataset as
    _canon_stdout groups stdout. A line naming no dataset can't be placed (the two sides take the datasets in different
    orders), so those are sorted among themselves. A byte that isn't UTF-8 reads U+FFFD, as run_harness reads Perl's."""
    text = text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    lines = [_PERL_AT.sub("", line) for line in text.split("\n") if line and not _NOT_SHARED.match(line)]
    grouped = _canon_stdout("\n".join(lines), paths)
    unplaced = sum(not any(f"{p}@" in line or f"'{p}'" in line for p in paths) for line in grouped)
    return sorted(grouped[:unplaced]) + grouped[unplaced:]


def _collect(wd: Path, scn: dict, died, taken: dict, getsnaps_called: bool, flush_called: bool, stdout: list[str],
             stderr: str) -> dict:
    zfs_calls = [json.loads(x) for x in (wd / "zfs.log").read_text().split("\n") if x]
    snapshots = sorted(
        (c[-1], "-r" in c) for c in zfs_calls if c[:1] == ["snapshot"]
    )
    hook_log = _hook_sort([json.loads(x) for x in (wd / "hook.log").read_text().split("\n") if x])
    res = {
        "died": bool(died), "taken": taken, "snapshot_calls": snapshots, "hook_log": hook_log, "stdout": stdout,
        "stderr": _canon_stderr(stderr, scn["tree"]),
        # getsnaps/add_cached_snapshots refresh: upstream's take_snapshots re-queries and flushes
        # internally when something was taken; pysanoid's equivalent (add_cached_snapshots) lives
        # one layer up, in main()'s do_take() wrapper (see pysanoid.py) -- out of scope for a
        # differential pinned to take_snapshots() itself, so collected for visibility but not
        # compared (matching diff_prune.py's treatment of prune_snapshots' own final flush).
        "getsnaps_called": bool(getsnaps_called), "flush_called": bool(flush_called),
    }
    res["names"] = "" if died else _name_times(scn, res)
    return res


def canon_snapsbytype_for_perl(snapsbytype: dict) -> dict:
    """getsnapsbytype stores each (path, type) entry as a HASHREF with a 'newest' key (age in
    seconds) -- not the bare age value this harness's own scenario shape uses. Feeding the bare
    number directly makes take_snapshots' `$snapsbytype{$path}{$type}{'newest'}` try to use that
    number as a hashref and die under strict refs -- found by this exact harness, 2026-09-22, the
    same per-side-shape trap diff_prune.py's canon_snapsbytype_for_perl already documents for
    'sorted' (a pipe-joined string there, not a list either)."""
    return {path: {t: {"newest": age} for t, age in types.items()} for path, types in snapsbytype.items()}


def run_perl(wd: Path, scn: dict) -> dict:
    wd = wd.resolve()  # see diff_prune.py's run_perl for why: a relative wd double-resolves inside the Perl child
    _setup(wd, scn)
    env = {"FAKE_ZFS_LOG": str(wd / "zfs.log"), "FAKE_ZFS_STATE": str(wd / "zfs_state.json"),
           "HOOK_LOG": str(wd / "hook.log"), "TEE_STDERR_TMP": str(wd / "tee_stderr.tmp"),
           "FAKE_NOW": str(scn["now_epoch"]), "FAKE_TICK": str(scn.get("tick") or ""), "TZ": scn["tz"],
           "FAIL_START": scn["fail_start"]}
    stdin_obj = {"config": canon_config_for_perl(scn["config"]), "snapsbytype": canon_snapsbytype_for_perl(scn["snapsbytype"]),
                "verbose": scn["verbose"], "readonly": scn["readonly"]}
    result = run_harness(SCRIPT, stdin_obj=stdin_obj, env=env, cwd=wd, timeout=15)
    if result.data is None:
        raise RuntimeError(f"perl harness produced no result:\n{result.stderr}")
    o = result.data
    return _collect(wd, scn, o["died"], o["taken"], o["getsnaps_called"], o["flush_called"],
                    _canon_stdout(result.stdout, scn["tree"]), result.stderr)


def _failing_start(fail_start: str):
    """subprocess.run, except that the call FAIL_START picks (see HEAD) raises what a failed fork raises."""
    kind, _, arg = fail_start.partition(":")
    real_run, done = subprocess.run, [False]

    def run(*a, **k):
        cmd = a[0] if a else k.get("args")
        if kind and not done[0]:
            # a hook is the one call made with env= (run_hook_script); zfs calls inherit the environment
            hit = ((kind == "snapshot" and isinstance(cmd, list) and "snapshot" in cmd
                    and any(str(x).startswith(arg) for x in cmd))
                   or (kind == "hook" and (k.get("env") or {}).get("SANOID_TARGET") == arg))
            if hit:
                done[0] = True
                raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN))
        return real_run(*a, **k)

    return run


def run_python(wd: Path, scn: dict) -> dict:
    _setup(wd, scn)
    os.environ.update(FAKE_ZFS_LOG=str(wd / "zfs.log"), FAKE_ZFS_STATE=str(wd / "zfs_state.json"),
                      HOOK_LOG=str(wd / "hook.log"))
    config = {sec: dict(cfg) for sec, cfg in scn["config"].items()}
    snaps_by_type = {path: {t: {"newest": age} for t, age in types.items()}
                     for path, types in scn["snapsbytype"].items()}
    zone = ZoneInfo(scn["tz"])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(_clock(wd, scn), tz)

    getsnaps_called = [False]
    flush_called = [False]
    saved_gs, saved_add = p.get_snapshots, p.add_cached_snapshots
    saved_clock = p.datetime, p.get_local_zoneinfo

    def fake_getsnaps(*a, **k):
        getsnaps_called[0] = True
        return {}

    def fake_add(*a, **k):
        flush_called[0] = True

    p.get_snapshots = fake_getsnaps
    p.add_cached_snapshots = fake_add
    p.datetime, p.get_local_zoneinfo = Clock, lambda: zone
    saved_run = subprocess.run
    subprocess.run = _failing_start(scn["fail_start"])
    out, err = io.StringIO(), io.StringIO()
    died = False
    taken: dict = {}
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            taken = p.take_snapshots(config, snaps_by_type, readonly=scn["readonly"], verbose=scn["verbose"])
    except Exception:  # noqa: BLE001 - harness: any failure counts as "died"
        died = True
    finally:
        p.get_snapshots, p.add_cached_snapshots = saved_gs, saved_add
        p.datetime, p.get_local_zoneinfo = saved_clock
        subprocess.run = saved_run
    out_taken = {k: {"recursive": int(bool(v["recursive"]))} for k, v in taken.items()}
    return _collect(wd, scn, died, out_taken, getsnaps_called[0], flush_called[0],
                    _canon_stdout(out.getvalue(), scn["tree"]), err.getvalue())


def run_both(scn: dict) -> tuple[dict, dict, list[str]]:
    a, b = run_perl(WORK / "perl", scn), run_python(WORK / "python", scn)
    if a["died"] != b["died"]:
        return a, b, ["died"]
    if a["died"]:
        return a, b, []
    diffs = [k for k in ("names", "taken", "snapshot_calls", "hook_log", "stdout", "stderr") if a[k] != b[k]]
    return a, b, diffs


def canon_config_for_perl(config: dict) -> dict:
    return {sec: {k: (1 if v is True else 0 if v is False else v) for k, v in cfg.items()}
            for sec, cfg in config.items()}


_TYPES = ("frequently", "hourly", "daily", "weekly", "monthly", "yearly")


def _sec(path: str, **over) -> dict:
    # Matches sanoid.defaults.conf's real, shipped [template_default] values -- see diff_prune.py's
    # _sec for why a realistic starting point matters here (a genuinely absent schedule field is
    # not a load_config-reachable scenario for any of these; unlike prune_defer's die-on-absent,
    # these upstream reads would just silently numify undef to 0, a DIFFERENT and less dramatic
    # trap, but still not the realistic production input this harness should default to testing).
    sec = {
        "path": path, "autosnap": True, "process_children_only": False, "zfs_recursion": False,
        "frequently": 0, "hourly": 0, "daily": 0, "weekly": 0, "monthly": 0, "yearly": 0,
        "frequent_period": 15, "hourly_min": 0, "daily_hour": 23, "daily_min": 59,
        "weekly_wday": 1, "weekly_hour": 23, "weekly_min": 30,
        "monthly_mday": 1, "monthly_hour": 0, "monthly_min": 0,
        "yearly_mon": 1, "yearly_mday": 1, "yearly_hour": 0, "yearly_min": 0,
    }
    sec.update(over)
    return sec


def S(config: dict, snapsbytype: dict, *, now_epoch: int, tz: str = "America/New_York",
      verbose=True, readonly=False, tree=None, snapshot_fail=None, known_diffs=(), fail_start="", tick=0, noise="") -> dict:
    """known_diffs: result keys (e.g. "taken") a HAND case expects to differ for an already-documented
    reason; every other key must still match, unlike a "KNOWN DIVERGENCE" case, which tolerates any diff.
    fail_start: one call that fails to start, as a failed fork does (see HEAD). tick: the seconds every zfs
    command and hook takes (0: the clock stands still). noise: what zfs writes to stderr on every snapshot, as hex."""
    return {"config": config, "snapsbytype": snapsbytype, "now_epoch": now_epoch, "tz": tz,
           "verbose": verbose, "readonly": readonly,
           "tree": tree or sorted({c["path"] for c in config.values()}), "snapshot_fail": snapshot_fail or {},
           "known_diffs": list(known_diffs), "fail_start": fail_start, "tick": tick, "noise": noise}


def epoch(y, mo, d, h, mi, s, tz) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=ZoneInfo(tz)).timestamp())


# A quiet, non-transition instant: 2026-07-15 12:00:00 America/New_York
SAFE_NOW = epoch(2026, 7, 15, 12, 0, 0, "America/New_York")

HAND = {}

HAND["nothing configured to take (all counts 0): no-op"] = S(
    {"tank/a": _sec("tank/a")}, {}, now_epoch=SAFE_NOW)

HAND["daily=90, no existing snapshot: due, taken non-recursively"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {}, now_epoch=SAFE_NOW)

HAND["daily=90, a daily taken 1 hour ago: not due"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {"tank/a": {"daily": 3600}}, now_epoch=SAFE_NOW)

HAND["daily=90, a daily taken 25 hours ago: due again"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {"tank/a": {"daily": 90000}}, now_epoch=SAFE_NOW)

HAND["autosnap off: nothing taken even though due"] = S(
    {"tank/a": _sec("tank/a", daily=90, autosnap=False)}, {}, now_epoch=SAFE_NOW)

HAND["process_children_only: section itself skipped"] = S(
    {"tank/a": _sec("tank/a", daily=90, process_children_only=True)}, {}, now_epoch=SAFE_NOW)

HAND["template-prefixed section always skipped"] = S(
    {"template_x": _sec("tank/a", daily=90)}, {}, now_epoch=SAFE_NOW)

HAND["zfs_recursion on: snapshot issued with -r"] = S(
    {"tank/a": _sec("tank/a", daily=90, zfs_recursion=True)}, {}, now_epoch=SAFE_NOW)

HAND["hourly + daily both due: two snapshots, one per type, same run"] = S(
    {"tank/a": _sec("tank/a", hourly=48, daily=90)}, {}, now_epoch=SAFE_NOW)

HAND["readonly: decides but takes nothing, no zfs call"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {}, now_epoch=SAFE_NOW, readonly=True)

HAND["pre_snapshot_script exits 0: snapshot proceeds, post_snapshot_script fires"] = S(
    {"tank/a": _sec("tank/a", daily=90, pre_snapshot_script=_script_for("pre", 0),
                    post_snapshot_script=_script_for("post", 0))}, {}, now_epoch=SAFE_NOW)

HAND["pre_snapshot_script exits nonzero + no_inconsistent_snapshot: dataset skipped entirely"] = S(
    {"tank/a": _sec("tank/a", daily=90, pre_snapshot_script=_script_for("pre", 1),
                    no_inconsistent_snapshot=True)}, {}, now_epoch=SAFE_NOW)

HAND["pre_snapshot_script exits nonzero, no_inconsistent_snapshot NOT set: snapshot still taken"] = S(
    {"tank/a": _sec("tank/a", daily=90, pre_snapshot_script=_script_for("pre", 1))}, {}, now_epoch=SAFE_NOW)

HAND["post_snapshot_script only fires if force_post_snapshot_script when pre failed"] = S(
    {"tank/a": _sec("tank/a", daily=90, pre_snapshot_script=_script_for("pre", 1),
                    post_snapshot_script=_script_for("post", 0), force_post_snapshot_script=True)},
    {}, now_epoch=SAFE_NOW)

# Perl's "0" is false, so upstream treats a hook set to 0 as unset: nothing runs, nothing fails
HAND['pre_snapshot_script = "0" + no_inconsistent_snapshot: unset, so the snapshot is taken and post runs'] = S(
    {"tank/a": _sec("tank/a", daily=90, pre_snapshot_script="0", no_inconsistent_snapshot=True,
                    post_snapshot_script=_script_for("post", 0))}, {}, now_epoch=SAFE_NOW)

HAND["a zfs snapshot that fails outright (not 'already exists'): tracked, no retry"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {}, now_epoch=SAFE_NOW,
    snapshot_fail={"tank/a@autosnap_2026-07-15_12:00:00_daily": {"stderr": "out of space\n"}})

# DEV-029: upstream only retries when $handleDst is set, which this scenario (a 'daily' type, an
# ordinary non-transition date) never sets -- so upstream just warns and drops the snapshot, while
# pysanoid's unconditional retry succeeds. Canonicalized as KNOWN, not asserted equal.
HAND["KNOWN DIVERGENCE: 'already exists' collision on a non-DST day -- pysanoid retries unconditionally, upstream doesn't"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {}, now_epoch=SAFE_NOW,
    snapshot_fail={"tank/a@autosnap_2026-07-15_12:00:00_daily": {"stderr": "cannot create snapshot: dataset already exists\n"}})

HAND["two datasets, one recursive one not, same run"] = S(
    {"tank/a": _sec("tank/a", daily=90), "tank/b": _sec("tank/b", daily=90, zfs_recursion=True)},
    {}, now_epoch=SAFE_NOW)

# -- DST hand cases: proves the documented divergence in _last_preferred_time is exactly what's
# documented (daily is ~1h early on spring-forward day), not something worse or different. The
# sensitive age window is derived from pysanoid's OWN (correct) _last_preferred_time rather than
# hand-computed, to avoid re-deriving DST arithmetic by hand for a test about DST arithmetic bugs:
# any age strictly between max_age_correct and max_age_correct+3600 lands in the ~1h-wide window
# where pysanoid says "due" (correctly) and upstream says "not due yet" (its own confirmed bug).
SPRING_FORWARD_AFTERNOON = epoch(2026, 3, 8, 15, 0, 0, "America/New_York")
_sf_now = datetime.fromtimestamp(SPRING_FORWARD_AFTERNOON, tz=ZoneInfo("America/New_York"))
_sf_max_age_correct = _sf_now.timestamp() - p._last_preferred_time(_sf_now, {"path": "tank/a", "daily_hour": 23, "daily_min": 59}, "daily").timestamp()
HAND["DST: 2026-03-08 (spring-forward day) afternoon, daily=90, none existing: still due on both (not the sensitive boundary)"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {}, now_epoch=SPRING_FORWARD_AFTERNOON, tz="America/New_York")
HAND["KNOWN DIVERGENCE: DST 2026-03-08, a daily aged into the ~1h window upstream's own bug misses"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {"tank/a": {"daily": int(_sf_max_age_correct) + 1800}},
    now_epoch=SPRING_FORWARD_AFTERNOON, tz="America/New_York")
FALL_BACK_AFTERNOON = epoch(2026, 11, 1, 15, 0, 0, "America/New_York")
HAND["DST: 2026-11-01 (fall-back day) afternoon, daily=90, none existing: still due on both"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {}, now_epoch=FALL_BACK_AFTERNOON, tz="America/New_York")

# -- upstream's OWN dst_ path. After a fall-back, $dstOffset arms $handleDst for a group whose first
# due type is hourly; an "already exists" collision then renames the @snapshots element in place
# (before retrying), and post_snapshot_script reads $snapshots[0] afterwards. `taken` is expected to
# differ: upstream records a dst_ snapshot with a forced cache refresh ($forcecacheupdate), not
# %taken, while pysanoid adds it to `taken` for its incremental cache add.
_EXISTS = "cannot create snapshot: dataset already exists\n"
_FB = "tank/a@autosnap_2026-11-01_15:00:00"
HAND["DST dst_ path, 2026-11-01: first snapshot collides, retry succeeds: post hook gets the dst_ name"] = S(
    {"tank/a": _sec("tank/a", hourly=48, post_snapshot_script=_script_for("post", 0))}, {},
    now_epoch=FALL_BACK_AFTERNOON, snapshot_fail={f"{_FB}_hourly": {"stderr": _EXISTS}}, known_diffs=("taken",))
HAND["DST dst_ path, 2026-11-01: the retry fails too: post hook still gets the dst_ name"] = S(
    {"tank/a": _sec("tank/a", hourly=48, post_snapshot_script=_script_for("post", 0))}, {},
    now_epoch=FALL_BACK_AFTERNOON,
    snapshot_fail={f"{_FB}_hourly": {"stderr": _EXISTS}, f"{_FB}dst_hourly": {"stderr": "out of space\n"}})
HAND["DST dst_ path, 2026-11-01: only the second snapshot collides: post hook keeps the first name"] = S(
    {"tank/a": _sec("tank/a", hourly=48, frequently=4, post_snapshot_script=_script_for("post", 0))}, {},
    now_epoch=FALL_BACK_AFTERNOON, snapshot_fail={f"{_FB}_frequently": {"stderr": _EXISTS}}, known_diffs=("taken",))

# hourly_min=30 around the 2026 changes. Spring: 03:15 EDT, the last hourly at 01:30 EST 45 real minutes ago,
# so none is due. Autumn: 02:00 EST, the 01:30 EST run missed, the last hourly at 01:30 EDT, so one is due.
_UTC = ZoneInfo("UTC")
HAND["DST 2026-03-08 03:15 EDT, hourly_min=30: no extra hourly just after the skipped hour"] = S(
    {"tank/a": _sec("tank/a", hourly=48, hourly_min=30)}, {"tank/a": {"hourly": 2700}},
    now_epoch=int(datetime(2026, 3, 8, 7, 15, tzinfo=_UTC).timestamp()))
HAND["DST 2026-11-01 02:00 EST, hourly_min=30, the 01:30 EST run missed: the hourly is due"] = S(
    {"tank/a": _sec("tank/a", hourly=48, hourly_min=30)}, {"tank/a": {"hourly": 5400}},
    now_epoch=int(datetime(2026, 11, 1, 7, 0, tzinfo=_UTC).timestamp()))

HAND["frequent_period=-15: slices like 15, as upstream's arithmetic makes it"] = S(
    {"tank/a": _sec("tank/a", frequently=4, frequent_period=-15)}, {"tank/a": {"frequently": 900}}, now_epoch=SAFE_NOW)

# DEV-031: upstream steps back a single week, so with weekly_wday=7
# its time is still ahead all Sunday until 23:30 and a weekly is taken on every run.
HAND["KNOWN DIVERGENCE: weekly_wday=7 on a Sunday, a weekly 15 min ago: upstream takes another (DEV-031)"] = S(
    {"tank/a": _sec("tank/a", weekly=5, weekly_wday=7)}, {"tank/a": {"weekly": 900}},
    now_epoch=epoch(2026, 7, 19, 12, 0, 0, "America/New_York"))

# A call that fails to start, as a failed fork does: upstream's system() returns -1, so that snapshot or hook
# fails and the run goes on.
HAND["a zfs snapshot that can't start: that snapshot fails, the next dataset's is still taken"] = S(
    {"tank/a": _sec("tank/a", daily=90), "tank/b": _sec("tank/b", daily=90)}, {}, now_epoch=SAFE_NOW,
    fail_start="snapshot:tank/a@")
HAND["DST dst_ path, 2026-11-01: the retry can't start: post hook still gets the dst_ name"] = S(
    {"tank/a": _sec("tank/a", hourly=48, post_snapshot_script=_script_for("post", 0))}, {},
    now_epoch=FALL_BACK_AFTERNOON, snapshot_fail={f"{_FB}_hourly": {"stderr": _EXISTS}}, fail_start=f"snapshot:{_FB}dst_")
HAND["a pre_snapshot_script that can't start: a failed hook, so the snapshot is taken and post skipped"] = S(
    {"tank/a": _sec("tank/a", daily=90, pre_snapshot_script=_script_for("pre", 0),
                    post_snapshot_script=_script_for("post", 0))}, {}, now_epoch=SAFE_NOW, fail_start="hook:tank/a")
HAND["a pre_snapshot_script that can't start + no_inconsistent_snapshot: dataset skipped"] = S(
    {"tank/a": _sec("tank/a", daily=90, pre_snapshot_script=_script_for("pre", 0), no_inconsistent_snapshot=True)},
    {}, now_epoch=SAFE_NOW, fail_start="hook:tank/a")
HAND["readonly with pre and post hooks: both announced, neither run"] = S(
    {"tank/a": _sec("tank/a", daily=90, pre_snapshot_script=_script_for("pre", 0),
                    post_snapshot_script=_script_for("post", 0))}, {}, now_epoch=SAFE_NOW, readonly=True)

# P1: malformed numbers are read as upstream reads them; the report goes to stderr,
# which isn't compared.
HAND["daily = '3o': read as 3, as upstream does, so the daily is taken"] = S(
    {"tank/a": _sec("tank/a", daily="3o")}, {}, now_epoch=SAFE_NOW)
HAND["hourly = '30 days' and daily = '+2': both read as upstream reads them"] = S(
    {"tank/a": _sec("tank/a", hourly="30 days", daily="+2")}, {}, now_epoch=SAFE_NOW)
HAND["daily = 'abc' (read as 0) and weekly = '-5': nothing taken, as upstream"] = S(
    {"tank/a": _sec("tank/a", daily="abc", weekly="-5")}, {}, now_epoch=SAFE_NOW)
# '1_1' is 1 in Perl (11 for int()): at 05:00 the 01:59 daily is due for a 5-hour-old one, not a 2-hour-old one
HAND["daily_hour = '1_1' is 01:59: a daily taken 5 hours ago is due"] = S(
    {"tank/a": _sec("tank/a", daily=7, daily_hour="1_1")}, {"tank/a": {"daily": 5 * 3600}},
    now_epoch=epoch(2026, 7, 15, 5, 0, 0, "America/New_York"))
HAND["daily_hour = '1_1' is 01:59: a daily taken 2 hours ago is not"] = S(
    {"tank/a": _sec("tank/a", daily=7, daily_hour="1_1")}, {"tank/a": {"daily": 2 * 3600}},
    now_epoch=epoch(2026, 7, 15, 5, 0, 0, "America/New_York"))
HAND["hourly_min = '30 min' and weekly_wday = '3rd': read as 30 and Wednesday"] = S(
    {"tank/a": _sec("tank/a", hourly=24, hourly_min="30 min", weekly=5, weekly_wday="3rd")},
    {"tank/a": {"hourly": 45 * 60, "weekly": 7 * 86400}}, now_epoch=SAFE_NOW)
# P1: a preferred time drops its fraction. With hourly_min 7.5 upstream's time is :07:30, so at
# 10:07:15 its hourly is still last hour's and a 30-minute-old one isn't due; here :07 has passed and it is.
HAND["KNOWN DIVERGENCE: hourly_min = '7.5' at 10:07:15: minute 7 here, so the hourly is due 30 s early (P1)"] = S(
    {"tank/a": _sec("tank/a", hourly=24, hourly_min="7.5")}, {"tank/a": {"hourly": 1800}},
    now_epoch=epoch(2026, 7, 15, 10, 7, 15, "America/New_York"))
# A blank preferred time is upstream's 0: 00:59 for daily_hour, Sunday for weekly_wday (the defaults were 23:59, Monday)
HAND["daily_hour blank is 00:59: at 05:00 a daily taken 4.5 hours ago is due"] = S(
    {"tank/a": _sec("tank/a", daily=7, daily_hour="")}, {"tank/a": {"daily": 4 * 3600 + 1800}},
    now_epoch=epoch(2026, 7, 15, 5, 0, 0, "America/New_York"))
HAND["weekly_wday blank is Sunday: on Wednesday a weekly taken 2 days ago is not due"] = S(
    {"tank/a": _sec("tank/a", weekly=4, weekly_wday="# unset")}, {"tank/a": {"weekly": 2 * 86400}}, now_epoch=SAFE_NOW)
# P1 and DEV-028: upstream's timelocal dies on day 0, for every dataset; here only the monthly is lost
HAND["KNOWN DIVERGENCE: monthly_mday blank: upstream dies, here only the monthly is lost (P1)"] = S(
    {"tank/a": _sec("tank/a", daily=7, monthly=6, monthly_mday="")}, {}, now_epoch=SAFE_NOW)


# A type whose newest snapshot is dated in the future (upstream #651): given such a table, neither take_snapshots takes
# it until the clock passes that date, and nothing here may differ. pysanoid's main sets aside a snapshot dated more
# than five minutes ahead before it builds the table (DEV-051); dev/e2e_subprocess.py and the tests check that.
_YEAR = 365 * 86400
HAND["a daily dated an hour in the future: none taken by either"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {"tank/a": {"daily": -3600}}, now_epoch=SAFE_NOW)
HAND["a daily dated a second in the future: none taken by either"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {"tank/a": {"daily": -1}}, now_epoch=SAFE_NOW)
HAND["a daily dated a year ahead, the hourly due: only the hourly is taken"] = S(
    {"tank/a": _sec("tank/a", hourly=48, daily=90)}, {"tank/a": {"daily": -_YEAR}}, now_epoch=SAFE_NOW)
HAND["every type dated a year ahead: none taken"] = S(
    {"tank/a": _sec("tank/a", **dict.fromkeys(_TYPES, 4))}, {"tank/a": dict.fromkeys(_TYPES, -_YEAR)}, now_epoch=SAFE_NOW)
HAND["two datasets, one under zfs_recursion, both dailies dated ahead, hooks set: none taken, no hook run"] = S(
    {"tank/a": _sec("tank/a", daily=90, pre_snapshot_script=_script_for("pre", 0)),
     "tank/b": _sec("tank/b", daily=90, zfs_recursion=True, post_snapshot_script=_script_for("post", 0))},
    {"tank/a": {"daily": -7200}, "tank/b": {"daily": -7200}}, now_epoch=SAFE_NOW)

# A count too long for a number: upstream reads a float, infinity past 308 digits, which is more than 0
HAND["daily = 19 nines: taken, as upstream"] = S({"tank/a": _sec("tank/a", daily="9" * 19)}, {}, now_epoch=SAFE_NOW)
HAND["daily = 400 nines: taken, as upstream"] = S({"tank/a": _sec("tank/a", daily="9" * 400)}, {}, now_epoch=SAFE_NOW)
HAND["daily = 5000 nines: taken, as upstream"] = S({"tank/a": _sec("tank/a", daily="9" * 5000)}, {}, now_epoch=SAFE_NOW)
HAND["daily = 5000 zeros and a 2, a daily taken an hour ago: a count of 2, not due"] = S(
    {"tank/a": _sec("tank/a", daily="0" * 5000 + "2")}, {"tank/a": {"daily": 3600}}, now_epoch=SAFE_NOW)
HAND["daily = 5000 zeros: 0, nothing taken"] = S({"tank/a": _sec("tank/a", daily="0" * 5000)}, {}, now_epoch=SAFE_NOW)

# The time in the names, on a clock that moves `tick` seconds for every zfs command and hook that runs. Upstream reads
# it as each dataset's turn comes, before the dataset's pre_snapshot_script (_name_times checks each side by that rule).
HAND["three datasets, 2 s a snapshot: each one's names carry the time its turn came"] = S(
    {f"tank/{d}": _sec(f"tank/{d}", daily=90) for d in "abc"}, {}, now_epoch=SAFE_NOW, tick=2)
HAND["two types due in one dataset and one in the next, 2 s a snapshot: one time for a dataset's types"] = S(
    {"tank/a": _sec("tank/a", hourly=48, daily=90), "tank/b": _sec("tank/b", daily=90)}, {}, now_epoch=SAFE_NOW, tick=2)
HAND["a 90 s pre_snapshot_script is given the names its dataset's snapshots take; the next dataset's are later"] = S(
    {"tank/a": _sec("tank/a", daily=90, pre_snapshot_script=_script_for("pre", 0),
                    post_snapshot_script=_script_for("post", 0)),
     "tank/b": _sec("tank/b", daily=90, pre_snapshot_script=_script_for("pre", 0))}, {}, now_epoch=SAFE_NOW, tick=90)
HAND["a pre_snapshot_script that fails under no_inconsistent_snapshot still takes its 90 s"] = S(
    {"tank/a": _sec("tank/a", daily=90, pre_snapshot_script=_script_for("pre", 1), no_inconsistent_snapshot=True),
     "tank/b": _sec("tank/b", daily=90)}, {}, now_epoch=SAFE_NOW, tick=90)
HAND["an hour a snapshot, one dataset under zfs_recursion: the names are hours apart"] = S(
    {"tank/a": _sec("tank/a", daily=90, zfs_recursion=True), "tank/b": _sec("tank/b", hourly=48, daily=90),
     "tank/c": _sec("tank/c", daily=90)}, {}, now_epoch=SAFE_NOW, tick=3600)
HAND["readonly, three datasets: nothing runs, so every name carries the time the run began"] = S(
    {f"tank/{d}": _sec(f"tank/{d}", daily=90) for d in "abc"}, {}, now_epoch=SAFE_NOW, tick=2, readonly=True)
HAND["a snapshot that can't start takes no time: the next dataset's names carry the same time"] = S(
    {"tank/a": _sec("tank/a", daily=90), "tank/b": _sec("tank/b", daily=90)}, {}, now_epoch=SAFE_NOW, tick=2,
    fail_start="snapshot:tank/a@")
HAND["DST dst_ path, 2026-11-01, 5 s a snapshot: the retry and the next type keep the time of the dataset's turn"] = S(
    {"tank/a": _sec("tank/a", hourly=48, frequently=4, post_snapshot_script=_script_for("post", 0))}, {},
    now_epoch=FALL_BACK_AFTERNOON, snapshot_fail={f"{_FB}_hourly": {"stderr": _EXISTS}}, known_diffs=("taken",), tick=5)

# What zfs prints that isn't UTF-8. Upstream never decodes it: a snapshot that worked is taken whatever zfs wrote
# beside it, and "already exists" is looked for in the bytes.
_NOISE = b"warning: caf\xe9\n".hex()
HAND["a snapshot that works while zfs prints a byte that isn't UTF-8: taken"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {}, now_epoch=SAFE_NOW, noise=_NOISE)
HAND["two types and a post hook, zfs printing such a byte on each snapshot: both taken, the hook run"] = S(
    {"tank/a": _sec("tank/a", hourly=48, daily=90, post_snapshot_script=_script_for("post", 0))}, {},
    now_epoch=SAFE_NOW, noise=_NOISE)
HAND["a snapshot that fails with a complaint that isn't UTF-8: not taken, no retry"] = S(
    {"tank/a": _sec("tank/a", daily=90)}, {}, now_epoch=SAFE_NOW, snapshot_fail={
        "tank/a@autosnap_2026-07-15_12:00:00_daily": {"stderr_hex": b"cannot create snapshot: caf\xe9 is busy\n".hex()}})
HAND["DST dst_ path, 2026-11-01: 'already exists' in a complaint that isn't UTF-8 is retried as dst_"] = S(
    {"tank/a": _sec("tank/a", hourly=48, post_snapshot_script=_script_for("post", 0))}, {},
    now_epoch=FALL_BACK_AFTERNOON, known_diffs=("taken",), snapshot_fail={
        f"{_FB}_hourly": {"stderr_hex": b"cannot create snapshot 'caf\xe9': dataset already exists\n".hex()}})


def show(scn: dict, a: dict, b: dict, diffs: list[str]) -> None:
    dt = datetime.fromtimestamp(scn["now_epoch"], tz=ZoneInfo(scn["tz"]))
    print(f"  now={dt.isoformat()} tz={scn['tz']} tick={scn.get('tick', 0)}"
          + ("  (zfs prints a byte that isn't UTF-8)" if scn.get("noise") else ""))
    for k in diffs:
        print(f"    {k}: perl={a[k]!r}\n         python={b[k]!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=300)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()

    tally = {"match": 0, "DIFF": 0}
    known = 0
    if args.mode == "hand":
        for name, scn in HAND.items():
            a, b, diffs = run_both(scn)
            if diffs and "KNOWN DIVERGENCE" in name:
                known += 1
                print(f"[KNOWN] {name}  (diffs={diffs}, expected)")
                continue
            expected = [k for k in diffs if k in scn["known_diffs"]]
            diffs = [k for k in diffs if k not in expected]
            tally["match" if not diffs else "DIFF"] += 1
            note = f"  (known diffs={expected}, expected)" if expected else ""
            print(f"[{'MATCH' if not diffs else 'DIFF '}] {name}{note}")
            if diffs:
                show(scn, a, b, diffs)
    else:
        r = random.Random(args.seed)
        seen: dict[tuple, dict] = {}
        for _ in range(args.n):
            scn = gen(r)
            a, b, diffs = run_both(scn)
            tally["match" if not diffs else "DIFF"] += 1
            if diffs:
                seen.setdefault(tuple(diffs), scn)
        for sig, scn in seen.items():
            a, b, diffs = run_both(scn)
            print(f"\n--- differs on {sig}")
            show(scn, a, b, diffs)
    print(tally, "known divergences:", known)
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if tally["DIFF"] else 0)


# Timezones deliberately spanning: 2x/year US-style DST, 2x/year but different dates (UK), Southern-
# hemisphere DST (opposite direction/season), and two zones with none at all.
TZS = ["America/New_York", "Europe/London", "Australia/Sydney", "UTC", "Asia/Kolkata"]
# Safe months: comfortably mid-season in EITHER hemisphere, away from any of the above zones'
# transition dates (all currently fall in Mar/Apr/Sep/Oct/Nov).
SAFE_MONTH_DAYS = [(1, 15), (1, 20), (6, 15), (6, 20), (7, 10), (12, 15), (12, 20)]


# Malformed and blank values take reads as upstream does. The preferred times all read in range and without a
# fraction: upstream dies on an out-of-range time (a blank day or month is 0), and drops no fraction (see the
# KNOWN DIVERGENCE hand cases).
MALFORMED_COUNTS = ["3o", "30 days", "7d", "+2", "1_0", ".5", "1e1", "inf", "abc", "", "-3", "0.0", "nan"]
_BLANK = ["", "# unset"]
MALFORMED_TIMES = {
    "hourly_min": ["1_5", "7x", "30 min", *_BLANK], "daily_hour": ["1_1", "5h", "23 o'clock", *_BLANK],
    "daily_min": ["5_9", "30m", *_BLANK], "weekly_wday": ["1st", "3_0", "5x", *_BLANK],
    "weekly_hour": ["2_3", "6pm", *_BLANK], "weekly_min": ["3_0", *_BLANK],
    "monthly_mday": ["1st", "15th"], "monthly_hour": ["0h", *_BLANK], "monthly_min": ["0m", *_BLANK],
    "yearly_mon": ["1_2", "6th"], "yearly_mday": ["1st"], "yearly_hour": ["0h", *_BLANK], "yearly_min": ["0m", *_BLANK],
}


def gen(r: random.Random) -> dict:
    tz = r.choice(TZS)
    mo, d = r.choice(SAFE_MONTH_DAYS)
    now_epoch = epoch(2026, mo, d, r.randint(0, 23), r.randint(0, 59), r.randint(0, 59), tz)
    path = f"tank/{r.choice('abcde')}"
    cfg = _sec(path, zfs_recursion=r.random() < 0.3, process_children_only=r.random() < 0.05)
    ages: dict = {}
    for t in _TYPES:
        if r.random() < 0.6:
            cfg[t] = r.choice([0, 1, 3, 7, 30, 90])
        if cfg[t] and r.random() < 0.7:
            # sometimes "just taken" (not due), sometimes "long ago" (due) -- either side of any
            # plausible threshold for this type
            ages[t] = r.choice([30, 3600, 5 * 3600, 20 * 3600, 2 * 86400, 7 * 86400, 40 * 86400, 400 * 86400])
    if r.random() < 0.15:
        cfg[r.choice(_TYPES[1:])] = r.choice(MALFORMED_COUNTS)  # frequently's slices depend on frequent_period
    if r.random() < 0.15:
        for key in r.sample(sorted(MALFORMED_TIMES), r.randint(1, 2)):
            cfg[key] = r.choice(MALFORMED_TIMES[key])
    late = random.Random(f"{now_epoch} {path} {sorted(ages.items())}")  # a stream of its own: the seed's other scenarios stay
    for t in sorted(ages):
        if late.random() < 0.12:
            ages[t] = late.choice([-1, -300, -3600, -40 * 86400])  # its newest dated in the future: neither takes it
    if late.random() < 0.05:
        cfg[late.choice(_TYPES[1:])] = "9" * late.choice([19, 400, 5000])  # a count too long for a number
    snapsbytype = {path: ages} if ages else {}
    if r.random() < 0.2:
        cfg["pre_snapshot_script"] = r.choice([_script_for("pre", 0), _script_for("pre", 0), _script_for("pre", 1), "0"])
    if r.random() < 0.15:
        cfg["no_inconsistent_snapshot"] = True
    if r.random() < 0.15:
        cfg["post_snapshot_script"] = r.choice([_script_for("post", 0), "0"])
    snapshot_fail = {}
    if r.random() < 0.1:
        # a plausible autosnap name for SOME due type this run might issue -- if it doesn't match
        # anything actually issued, this is simply inert (never looked up), which is fine: still
        # exercises the "no matching failure" path through the fake zfs a good fraction of the time
        snapshot_fail[f"{path}@autosnap_2026-{mo:02d}-{d:02d}_00:00:00_daily"] = {
            "stderr": "cannot create snapshot: dataset already exists\n" if r.random() < 0.5 else "out of space\n"}
    fail_start = r.choice([f"snapshot:{path}@", f"hook:{path}"]) if r.random() < 0.08 else ""
    more = random.Random(f"more {now_epoch} {path}")  # another stream of its own: datasets beside this one, on a clock that moves
    config, tick = {path: cfg}, 0
    if more.random() < 0.3:
        tick = more.choice([1, 2, 30, 90, 3600])
        for letter in more.sample([c for c in "abcde" if f"tank/{c}" != path], more.randint(1, 2)):
            other = _sec(f"tank/{letter}", zfs_recursion=more.random() < 0.3)
            for t in more.sample(_TYPES[1:], more.randint(1, 2)):
                other[t] = more.choice([1, 7, 30])  # none of it yet, so it is due
            if more.random() < 0.4:
                other["pre_snapshot_script"] = more.choice([_script_for("pre", 0), _script_for("pre", 1)])
            if more.random() < 0.3:
                other["no_inconsistent_snapshot"] = True
            if more.random() < 0.3:
                other["post_snapshot_script"] = _script_for("post", 0)
            config = {f"tank/{letter}": other, **config} if more.random() < 0.5 else {**config, f"tank/{letter}": other}
    noise = _NOISE if more.random() < 0.08 else ""  # zfs warns on every snapshot, in bytes that aren't UTF-8
    return S(config, snapsbytype, now_epoch=now_epoch, tz=tz, verbose=r.random() < 0.6,
            readonly=r.random() < 0.1, snapshot_fail=snapshot_fail, fail_start=fail_start, tick=tick, noise=noise)


if __name__ == "__main__":
    main()
