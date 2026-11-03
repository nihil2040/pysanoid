#!/usr/bin/env python3
"""Differential test: upstream's prune_snapshots (Module 1 -- the actual destructive core of the
whole tool), extracted verbatim along with check_prune_defer/check_capacity_limit, vs pysanoid's
prune_snapshots + select_prune_candidates + check_prune_defer.

Given a pre-built snapshot inventory (the shape getsnaps/getsnapsbytype produce -- enumeration
itself is diff_snapshot_enumeration.py's job, not this one) and a config, compares: which
snapshots get destroyed, which pre_pruning_script/pruning_script hooks run and with what env vars
(and whether a pre_pruning_script's nonzero exit correctly vetoes that one destroy), whether a
section defers via prune_defer/pool capacity, whether a busy `sanoid_pruning` lock correctly
defers a batch rather than blocking or destroying anyway, and on stderr zfs's own words, the
`could not remove` lines and a failed hook's warning (as a set: Perl visits types in its hash order;
each side is also checked for a failed destroy's complaint coming just before its line).

Two upstream dependencies are stubbed rather than extracted for real, matching this tool's
established scope boundaries (see dev/README.md's design notes):
  - get_zpool_capacity: shells out to a real `zpool list`. Stubbed on both sides to read from a
    scenario-supplied {pool: capacity} map, failing (die / SanoidError) for a pool the map lacks,
    as the real query does, and logging every pool asked about. check_prune_defer's own
    threshold/memoization logic (the thing worth differential-testing here) doesn't depend on how
    the capacity number was obtained.
  - removecachedsnapshots / remove_cached_snapshots: cache-file mutation is diff_snapshot_cache_
    mutation.py's target. Stubbed on both sides with upstream's control flow and none of the file
    work: return at once when nothing is pending; defer, keeping the pending set, when the scenario
    marks the cache lock busy and the call doesn't wait; otherwise log (wait flag, pending count)
    and clear the pending set, as upstream's `undef %pruned` does. That compares prune_snapshots'
    own orchestration: a flush after each batch, and a closing blocking flush that only acts on
    what a busy lock deferred.

checklock/writelock/removelock are stubbed the same way every other target in this tool stubs them
(a real PID+process-list lockfile has nothing left to differential-test after Module 7's audit
already confirmed the lock NAMES match -- see DEV-040 in DEVIATIONS.md).

One known class, from DEV-041: a run directory no lock file can be made in
(run_unusable). Upstream's unchecked writelock writes nothing, so it prunes its first batch unlocked
and its removelock dies before the flush, as the real one did end to end. pysanoid is checked
against itself: every batch that needs the lock is skipped and reported, nothing is destroyed, and
no hook runs.

Run: python3 dev/diff_prune.py hand
     python3 dev/diff_prune.py fuzz <n> <seed>
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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

WORK = DEV_DIR / "work" / "prune"
os.environ["PATH"] = f"{DEV_DIR / 'fake_zfs'}{os.pathsep}{os.environ['PATH']}"
HOOK_PROBE = DEV_DIR / "fake_hook" / "hook_probe"
NOW = 1_790_000_000
p.time.time = lambda: float(NOW)

HEAD = PERL_JSON_HEAD + r'''
BEGIN { *CORE::GLOBAL::time = sub () { $ENV{FAKE_NOW} + 0 } }
# FAIL_START: the `zfs destroy` of that snapshot can't start, once, as when fork fails: system() returns -1
our $__failed_start = 0;
BEGIN {
    *CORE::GLOBAL::system = sub {
        if (!$__failed_start && $ENV{FAIL_START} && @_ == 3 && $_[1] eq 'destroy' && $_[2] eq $ENV{FAIL_START}) {
            $__failed_start = 1;
            $? = -1;
            return -1;
        }
        return CORE::system(@_);
    };
}
my %args = ('verbose' => 1, 'debug' => 0, 'readonly' => 0);
my $zfs = 'zfs';
my (%config, %snaps, %snapsbytype, %pruned, %capacitycache);
my %__pool_caps;
my @__flush_log;
my %__flushed;  # what the stub below cleared out of %pruned, so the result can still list it
sub checklock  { return (defined $ENV{LOCK_FREE} && $ENV{LOCK_FREE} eq '0') ? 0 : 1 }
sub writelock  { 1 }
# RUN_UNUSABLE: no lock file could be made, so writelock's unchecked open wrote nothing and checklock finds none
sub removelock { die "ERROR: No valid lockfile found - Did a rogue process or user update or delete it?\n" if $ENV{RUN_UNUSABLE}; 1 }
my @__pool_queries;
sub get_zpool_capacity {
    my $pool = shift;
    push @__pool_queries, $pool;
    die "ERROR: 'zpool list -H -o cap $pool' command returned invalid capacity value ()!\n" unless exists $__pool_caps{$pool};
    return $__pool_caps{$pool};
}
sub removecachedsnapshots {
    my $wait = shift;
    return if not %pruned;
    return if !$wait && $ENV{CACHE_BUSY};
    push @__flush_log, { wait => ($wait ? 1 : 0), n => scalar(keys %pruned) };
    @__flushed{keys %pruned} = ();
    undef %pruned;
}
sub get_date { return (); }  # called by prune_snapshots but its result (%datestamp) is unused there
'''

TAIL = f'''
{READ_SCENARIO_PERL}
%config = %{{ $__scn->{{config}} }};
%snapsbytype = %{{ $__scn->{{snapsbytype}} }};
foreach my $path (keys %{{ $__scn->{{snaps}} }}) {{
    foreach my $name (keys %{{ $__scn->{{snaps}}{{$path}} }}) {{
        $snaps{{$path}}{{$name}}{{ctime}} = $__scn->{{snaps}}{{$path}}{{$name}}{{ctime}};
    }}
}}
%__pool_caps = %{{ $__scn->{{pool_caps}} }};
$args{{verbose}} = $__scn->{{verbose}} ? 1 : 0;
$args{{readonly}} = $__scn->{{readonly}} ? 1 : 0;
my $died = '';
eval {{ prune_snapshots(\\%config, \\%snaps, \\%snapsbytype, {{}}); 1 }} or do {{ ($died = $@) =~ s/\\s+$//; }};
my %__all_pruned = (%__flushed, %pruned);
my %__result = ( died => $died, pruned => [ sort keys %__all_pruned ], flush_log => \\@__flush_log,
                 pools_queried => [ sort @__pool_queries ] );
{WRITE_RESULT_PERL}
'''

SCRIPT = build_harness(HEAD, extract_subs("prune_snapshots", "check_prune_defer", "check_capacity_limit", "runscript"), TAIL)


def _script_for(tag: str, exit_code: int) -> str:
    return f"{HOOK_PROBE} {tag} {exit_code}"


def _setup(wd: Path, scn: dict) -> None:
    shutil.rmtree(wd, ignore_errors=True)
    (wd / "cache").mkdir(parents=True)
    if scn.get("run_unusable"):
        (wd / "run").write_text("")  # a regular file where the run directory should be
    else:
        (wd / "run").mkdir()
    (wd / "zfs_state.json").write_text(json.dumps({
        "datasets": scn["tree"], "snapshot_fail": {}, "destroy_fail": scn.get("destroy_fail", {}),
        "noise": scn.get("noise", ""),
    }))
    (wd / "zfs.log").write_text("")
    (wd / "hook.log").write_text("")


_PERL_AT = re.compile(r" at \S+ line \d+(?:, <\S+> (?:line|chunk) \d+)?\.$")  # what Perl's warn adds
# not compared: Perl's own warnings about upstream's code, and what pysanoid reports itself
_NOT_SHARED = re.compile(r"WARN: 'script_timeout' is not set |ERROR: |could not run |Can't exec |"
                         r"Use of uninitialized value |Argument \".*\" isn't numeric ")
_COULD_NOT = re.compile(r"could not remove (\S+) : (-?\d+)$")


def _own_words(scn: dict, stderr: str) -> tuple[list[str], str]:
    """zfs's own words, the `could not remove` lines and the hook warnings of one side's stderr, in order, a byte that
    isn't UTF-8 read as U+FFFD as run_harness reads Perl's; and what is out of place in them ("" for nothing): a failed
    destroy's line that doesn't come straight after the complaint zfs made of it."""
    text = stderr.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    lines = [_PERL_AT.sub("", line) for line in text.split("\n") if line and not _NOT_SHARED.match(line)]
    for at, line in enumerate(lines):
        failed = _COULD_NOT.match(line)
        if failed and failed.group(2) != "-1":  # one that couldn't start made no complaint
            fail = scn.get("destroy_fail", {}).get(failed.group(1), {})
            said = bytes.fromhex(fail["stderr_hex"]).decode("utf-8", "replace") if "stderr_hex" in fail else fail.get(
                "stderr", f"cannot destroy '{failed.group(1)}': unknown error\n")
            last = said.rstrip("\n").split("\n")[-1]
            if last and (at == 0 or lines[at - 1] != last):
                return lines, f"{line!r} doesn't follow {last!r}"
    return lines, ""


def _collect(wd: Path, scn: dict, died: str, pruned: list[str], flush_log: list[dict], pools_queried: list[str],
             stderr: str, *, lock_tries: int = 0, lock_skips: int = 0) -> dict:
    zfs_calls = [json.loads(x) for x in (wd / "zfs.log").read_text().split("\n") if x]
    destroyed = sorted(c[-1] for c in zfs_calls if c[:1] == ["destroy"])
    hook_log = sorted((json.loads(x) for x in (wd / "hook.log").read_text().split("\n") if x),
                      key=lambda e: json.dumps(e, sort_keys=True))
    said, out_of_place = _own_words(scn, stderr)
    return {
        "died": bool(died), "died_how": died, "pruned": sorted(pruned), "destroyed": destroyed, "hook_log": hook_log,
        "stderr": sorted(said), "stderr_order": out_of_place,
        # pysanoid only: its pruning lock tries, and the batches it reported as not pruned for want of the lock
        "lock_tries": lock_tries, "lock_skips": lock_skips,
        # Each applied flush logs what it cleared. The per-batch sizes are sorted because Perl visits
        # a section's types in its per-process hash order; pysanoid's order is fixed.
        "flush_batches": sorted(f["n"] for f in flush_log if not f["wait"]),
        # what the closing, blocking flush had left to do: 0 unless a busy cache lock deferred a batch
        "flush_final": sum(f["n"] for f in flush_log if f["wait"]),
        "pools_queried": sorted(pools_queried),  # prune_defer's capacity queries, one per pool per run
    }


def canon_snapsbytype_for_perl(snapsbytype: dict) -> dict:
    """getsnapsbytype stores 'sorted' as a PIPE-JOINED STRING (prune_snapshots' own extracted body
    does `split(/\\|/, $snapsbytype{$path}{$type}{'sorted'})`) -- not a list. Feeding it a JSON
    array instead silently stringifies the arrayref to "ARRAY(0x...)" and splits THAT on '|' (no
    match), producing one bogus fake snapshot name instead of a real list -- found by this exact
    harness, 2026-09-22 (the same class of per-side-normalization trap dev/README.md already
    documents for config booleans, now confirmed for this shape too)."""
    return {path: {t: {"sorted": "|".join(e["sorted"])} for t, e in types.items()} for path, types in snapsbytype.items()}


def run_perl(wd: Path, scn: dict) -> dict:
    # a relative wd, resolved against the CALLING process's cwd for the env vars built below (e.g.
    # FAKE_ZFS_LOG) but then handed to subprocess.run as the CHILD's own cwd, gets resolved a SECOND
    # time inside the child against that new cwd -- silently landing on a doubly-nested, usually
    # nonexistent path. Found 2026-09-22 building dev/simulate_long_horizon.py's failure-injection
    # extension: an explicit relative --scratch made every Perl spot-check "die" on `open(STDERR,
    # ...)` failing that way, misreporting as an empty, wrongly-compared result instead of the
    # missing-directory problem it actually was. Absolute by construction from here on closes it
    # for every caller, not just the one that happened to trip over it.
    wd = wd.resolve()
    _setup(wd, scn)
    env = {"FAKE_ZFS_LOG": str(wd / "zfs.log"), "FAKE_ZFS_STATE": str(wd / "zfs_state.json"),
           "HOOK_LOG": str(wd / "hook.log"), "LOCK_FREE": "1" if scn["lock_free"] else "0",
           # defaults to this file's own fixed NOW so every existing hand/fuzz scenario (none of
           # which set now_epoch) is unaffected; an external caller (dev/simulate_long_horizon.py)
           # passes its own now_epoch per call, which is the whole reason this is a runtime env
           # read (see HEAD) rather than baked into the script at build time like the other
           # diff_*.py targets that only ever need ONE fixed clock -- found needing this the hard
           # way, 2026-09-22: reusing this function with a real varying "now" while the Perl side
           # silently kept believing it was always 2026-09-21 produced a batch of "Perl found
           # nothing to prune" results that had nothing to do with pysanoid.
           "FAKE_NOW": str(int(scn.get("now_epoch", NOW))),
           "CACHE_BUSY": "1" if scn.get("cache_busy") else "", "FAIL_START": scn.get("fail_start", ""),
           "RUN_UNUSABLE": "1" if scn.get("run_unusable") else ""}
    stdin_obj = {"config": canon_config_for_perl(scn["config"]), "snaps": scn["snaps"],
                "snapsbytype": canon_snapsbytype_for_perl(scn["snapsbytype"]), "pool_caps": scn["pool_caps"],
                "verbose": scn["verbose"], "readonly": scn["readonly"]}
    result = run_harness(SCRIPT, stdin_obj=stdin_obj, env=env, timeout=15)
    if result.data is None:
        raise RuntimeError(f"perl harness produced no result:\n{result.stderr}")
    o = result.data
    return _collect(wd, scn, o["died"], o["pruned"], o["flush_log"], o["pools_queried"], result.stderr)


def run_python(wd: Path, scn: dict) -> dict:
    _setup(wd, scn)
    os.environ.update(FAKE_ZFS_LOG=str(wd / "zfs.log"), FAKE_ZFS_STATE=str(wd / "zfs_state.json"),
                      HOOK_LOG=str(wd / "hook.log"))
    config = {sec: dict(cfg) for sec, cfg in scn["config"].items()}
    snaps = {path: {name: p.Snapshot(fs=path, name=name, snap_type="daily", ctime=v["ctime"]) for name, v in names.items()}
             for path, names in scn["snaps"].items()}
    snaps_by_type = {path: {t: {"sorted": e["sorted"]} for t, e in types.items()}
                     for path, types in scn["snapsbytype"].items()}
    held = None
    if not scn["lock_free"]:
        held = open(wd / "run" / "sanoid_pruning.lock", "w")  # noqa: SIM115 - held open across this function, released in `finally`
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    flush_log: list[dict] = []
    saved_flush = p.remove_cached_snapshots
    saved_capacity = p.get_zpool_capacity
    saved_time = p.time.time

    def fake_flush(pruned, cache_dir, run_dir, *, wait=False, verbose=False, problems=None):
        # the same control flow as the Perl stub in HEAD
        if not pruned:
            return True
        if not wait and scn.get("cache_busy"):
            return False
        flush_log.append({"wait": int(bool(wait)), "n": len(pruned)})
        return True

    pools_queried: list[str] = []

    def fake_capacity(pool):
        # the same as the Perl stub in HEAD
        pools_queried.append(pool)
        if pool not in scn["pool_caps"]:
            raise p.SanoidError(f"'zpool list -H -o cap {pool}' returned invalid capacity value ('')")
        return scn["pool_caps"][pool]

    real_run = subprocess.run
    failed_start = [False]

    def failing_start(*args: Any, **kwargs: Any) -> Any:
        # the same as FAIL_START in HEAD: that destroy raises what a failed fork raises, once
        cmd = args[0] if args else kwargs.get("args")
        if scn.get("fail_start") and not failed_start[0] and isinstance(cmd, list) and cmd[1:] == ["destroy", scn["fail_start"]]:
            failed_start[0] = True
            raise BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN))
        return real_run(*args, **kwargs)

    real_try_lock = p.try_lock
    lock_tries = [0]

    @contextlib.contextmanager
    def counted_try_lock(run_dir: Path, name: str) -> Iterator[tuple[bool, OSError | None]]:
        if name == "sanoid_pruning":
            lock_tries[0] += 1
        with real_try_lock(run_dir, name) as got:
            yield got

    p.remove_cached_snapshots = fake_flush
    p.get_zpool_capacity = fake_capacity
    p.try_lock = counted_try_lock
    subprocess.run = failing_start
    # python_clock: seconds past the whole-second clock the Perl side sees (upstream's time() is whole seconds)
    clock = float(scn.get("now_epoch", NOW)) + scn.get("python_clock", 0.0)
    p.time.time = lambda: clock
    out, err = io.StringIO(), io.StringIO()
    died = ""
    pruned: list[str] = []
    problems: list[str] = []
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            pruned = p.prune_snapshots(config, snaps, snaps_by_type, wd / "cache", wd / "run",
                                       readonly=scn["readonly"], verbose=scn["verbose"], problems=problems)
    except Exception as e:  # noqa: BLE001 - harness: any failure counts as "died"
        died = f"{type(e).__name__}: {e}"
    finally:
        p.remove_cached_snapshots = saved_flush
        p.get_zpool_capacity = saved_capacity
        p.try_lock = real_try_lock
        p.time.time = saved_time
        subprocess.run = real_run
        if held:
            held.close()
    return _collect(wd, scn, died, pruned, flush_log, pools_queried, err.getvalue(), lock_tries=lock_tries[0],
                    lock_skips=sum(m.startswith("not pruning the ") for m in problems))


RUN_UNUSABLE = ("no lock file can be made: upstream prunes its first batch unlocked and dies releasing the lock, "
                "pysanoid prunes nothing and reports each batch")
UPSTREAM_NO_LOCKFILE = "ERROR: No valid lockfile found"
KNOWN: Counter[str] = Counter()


def run_both(scn: dict) -> tuple[dict, dict, list[str], list[str]]:
    a, b = run_perl(WORK / "perl", scn), run_python(WORK / "python", scn)
    if scn.get("run_unusable"):
        # pysanoid against itself (DEV-041): a batch that needs the lock is skipped and reported, so
        # nothing is destroyed, flushed or run
        if b["destroyed"] or b["hook_log"] or b["flush_batches"] or b["flush_final"] or b["lock_skips"] != b["lock_tries"]:
            return a, b, ["unusable self-check"], []
        upstream_locked = a["died_how"].startswith(UPSTREAM_NO_LOCKFILE)
        if upstream_locked and b["lock_tries"]:
            KNOWN[RUN_UNUSABLE] += 1
            return a, b, [], [RUN_UNUSABLE]
        if upstream_locked != bool(b["lock_tries"]) and not (a["died"] and b["died"]):
            return a, b, ["lock needed"], []  # one side found a batch to prune where the other didn't
    if a["died"] != b["died"]:
        return a, b, ["died"], []
    if a["died"]:
        return a, b, [], []  # both refused: _preflight_prune_config vs upstream's mid-loop die
        # legitimately differ in WHEN they refuse -- see prune_snapshots' own docstring
    diffs = [k for k in ("pruned", "destroyed", "hook_log", "flush_batches", "flush_final", "pools_queried", "stderr",
                         "stderr_order") if a[k] != b[k]]
    return a, b, diffs, []


def canon_config_for_perl(config: dict) -> dict:
    return {sec: {k: (1 if v is True else 0 if v is False else v) for k, v in cfg.items()}
            for sec, cfg in config.items()}


def _sec(path: str, **over) -> dict:
    # prune_defer=0 matches what real config supplies: sanoid.defaults.conf's [template_default] sets
    # `prune_defer = 0`, so every section inherits a value. prune_defer=None drops the key, as a
    # defaults file without it would (fatal on both sides).
    sec = {"path": path, "autoprune": True, "process_children_only": False, "prune_defer": 0, **over}
    if sec.get("prune_defer") is None:
        del sec["prune_defer"]
    return sec


def _snap(name: str, ctime: int) -> tuple[str, dict]:
    return name, {"ctime": ctime}


def snaptype(names_ctimes: list[tuple[str, int]]) -> dict:
    """names_ctimes: oldest-first (name, ctime) pairs -- matches getsnapsbytype's own 'sorted'."""
    return {"sorted": [n for n, _ in names_ctimes]}


def build(config: dict, per_path_types: dict[str, dict[str, list[tuple[str, int]]]]) -> tuple[dict, dict]:
    """per_path_types: {path: {type: [(name, ctime), ...]}} -> (snaps, snapsbytype) scenario dicts."""
    snaps: dict = {}
    snapsbytype: dict = {}
    for path, types in per_path_types.items():
        for _type, entries in types.items():
            snapsbytype.setdefault(path, {})[_type] = snaptype(entries)
            for name, ctime in entries:
                snaps.setdefault(path, {})[name] = {"ctime": ctime}
    return snaps, snapsbytype


def S(config: dict, snaps: dict, snapsbytype: dict, *, pool_caps=None, verbose=True, readonly=False,
      lock_free=True, run_unusable=False, tree=None, destroy_fail=None, cache_busy=False, python_clock=0.0,
      fail_start="", known_diffs=(), known=(), noise="") -> dict:
    """cache_busy: the cache lock is held whenever a flush doesn't wait. run_unusable: no lock file can be
    made in the run directory (so no other process holds one). python_clock: seconds the Python side's
    clock runs past the Perl side's whole second. fail_start: the snapshot whose `zfs destroy` can't
    start, as when fork fails. known_diffs: result keys a HAND case expects to differ for an
    already-documented reason; every other key must still match. known: the known classes a HAND case
    expects run_both to find. noise: what zfs writes to stderr on every destroy, as hex."""
    assert lock_free or not run_unusable, "a lock another process holds needs a lock file"
    return {"config": config, "snaps": snaps, "snapsbytype": snapsbytype, "pool_caps": pool_caps or {},
           "verbose": verbose, "readonly": readonly, "lock_free": lock_free, "run_unusable": run_unusable,
           "tree": tree or sorted({c["path"] for c in config.values()}), "destroy_fail": destroy_fail or {},
           "cache_busy": cache_busy, "python_clock": python_clock, "fail_start": fail_start,
           "known_diffs": list(known_diffs), "known": list(known), "noise": noise}


# 10 dailies, oldest-first, one per (simulated) day
_DAILIES = [(f"autosnap_d{i}_daily", NOW - (10 - i) * 86400) for i in range(10)]

HAND = {}

snaps, sbt = build({"a": {"daily": _DAILIES}}, {"tank/a": {"daily": _DAILIES}})
HAND["daily=3, 10 present: the 7 oldest are pruned, 3 newest kept"] = S(
    {"tank/a": _sec("tank/a", daily=3)}, snaps, sbt)

snaps, sbt = build({}, {"tank/a": {"daily": _DAILIES}})
HAND["daily=3, exactly the retained count -- nothing to prune"] = S(
    {"tank/a": _sec("tank/a", daily=3)}, snaps, {"tank/a": {"daily": snaptype(_DAILIES[-3:])}})

HAND["autoprune off: nothing touched even though plenty are old enough"] = S(
    {"tank/a": _sec("tank/a", daily=3, autoprune=False)}, snaps, sbt)

HAND["process_children_only: section itself is skipped"] = S(
    {"tank/a": _sec("tank/a", daily=3, process_children_only=True)}, snaps, sbt)

HAND["template-prefixed section name is always skipped"] = S(
    {"template_x": _sec("tank/a", daily=3)}, snaps, sbt)

HAND["daily=0: prune every existing daily immediately (documented upstream feature)"] = S(
    {"tank/a": _sec("tank/a", daily=0)}, snaps, sbt)

HAND["a type with no configured count is left alone even though snapshots exist"] = S(
    {"tank/a": _sec("tank/a", weekly=3)}, snaps, sbt)  # 'daily' never appears in the section's own config

HAND["prune_defer=50, pool at 40%: deferred, nothing pruned"] = S(
    {"tank/a": _sec("tank/a", daily=3, prune_defer=50)}, snaps, sbt, pool_caps={"tank": 40})

HAND["prune_defer=50, pool at 60%: not deferred, prunes normally"] = S(
    {"tank/a": _sec("tank/a", daily=3, prune_defer=50)}, snaps, sbt, pool_caps={"tank": 60})

HAND["prune_defer=0: never defers regardless of capacity (documented: never even queries the pool)"] = S(
    {"tank/a": _sec("tank/a", daily=3, prune_defer=0)}, snaps, sbt, pool_caps={})

HAND["sanoid_pruning lock held by another process: this batch deferred, nothing destroyed"] = S(
    {"tank/a": _sec("tank/a", daily=3)}, snaps, sbt, lock_free=False)

HAND["readonly: decides what to prune but destroys nothing"] = S(
    {"tank/a": _sec("tank/a", daily=3)}, snaps, sbt, readonly=True)

HAND["pre_pruning_script exits 0: destroy proceeds, pruning_script also fires"] = S(
    {"tank/a": _sec("tank/a", daily=3, pre_pruning_script=_script_for("prepr", 0), pruning_script=_script_for("postpr", 0))},
    snaps, sbt)

HAND["pre_pruning_script exits nonzero: vetoes the destroy for every candidate"] = S(
    {"tank/a": _sec("tank/a", daily=3, pre_pruning_script=_script_for("prepr", 1))}, snaps, sbt)

HAND["a zfs destroy that fails leaves that one snapshot un-pruned"] = S(
    {"tank/a": _sec("tank/a", daily=3)}, snaps, sbt,
    destroy_fail={f"tank/a@{_DAILIES[0][0]}": {"stderr": "cannot destroy: dataset is busy\n"}})

snaps2, sbt2 = build({}, {"tank/a": {"daily": _DAILIES, "hourly": [(f"autosnap_h{i}_hourly", NOW - (5 - i) * 3600) for i in range(5)]}})
HAND["two types in the same section: each pruned independently against its own count"] = S(
    {"tank/a": _sec("tank/a", daily=3, hourly=2)}, snaps2, sbt2)

# Two sections on one path: [tank/a] is a recursive parent that skips itself (process_children_only), the one
# layout load_config still allows beside another section for it, and [b] (path = tank/a) prunes to 3. Upstream
# reads hooks from $config{$dataset}, the section named after the dataset.
HAND["two sections on one path: [b]'s prunes get the veto hook in [tank/a]"] = S(
    {"tank/a": _sec("tank/a", process_children_only=True, pre_pruning_script=_script_for("named_veto", 1)),
     "b": _sec("tank/a", daily=3)}, snaps, sbt)

HAND["two sections on one path: pruning_script comes from [tank/a], not [b]"] = S(
    {"tank/a": _sec("tank/a", process_children_only=True, pruning_script=_script_for("named_post", 0)),
     "b": _sec("tank/a", daily=3, pruning_script=_script_for("own_post", 0))}, snaps, sbt)

HAND["two sections on one path: a veto hook set the same in both runs once"] = S(
    {"tank/a": _sec("tank/a", process_children_only=True, pre_pruning_script=_script_for("check", 0)),
     "b": _sec("tank/a", daily=3, pre_pruning_script=_script_for("check", 0))}, snaps, sbt)

# DEV-012: [b]'s own, different veto hook also runs, after
# upstream's. Only the hook log may differ.
HAND["two sections on one path, different veto hooks: both run (documented)"] = S(
    {"tank/a": _sec("tank/a", process_children_only=True, pre_pruning_script=_script_for("named_hook", 0)),
     "b": _sec("tank/a", daily=3, pre_pruning_script=_script_for("own_hook", 0))},
    snaps, sbt, known_diffs=("hook_log",))

HAND['pre_pruning_script = "0" is unset, as Perl reads it'] = S(
    {"tank/a": _sec("tank/a", daily=3, pre_pruning_script="0")}, snaps, sbt)

# dailies aged 3, 2, 1 and 0 days: the 3-day-old one sits exactly on daily=3's age boundary
_EDGE = [(f"autosnap_e{k}_daily", NOW - k * 86400) for k in (3, 2, 1, 0)]
snaps_e, sbt_e = build({}, {"tank/a": {"daily": _EDGE}})
HAND["whole-second clock: the boundary snapshot is kept with Python's clock 0.5 s into that second"] = S(
    {"tank/a": _sec("tank/a", daily=3)}, snaps_e, sbt_e, python_clock=0.5)

HAND["cache lock busy: each batch's flush deferred, then applied by the closing flush"] = S(
    {"tank/a": _sec("tank/a", daily=3, hourly=2)}, snaps2, sbt2, cache_busy=True)

# No prune_defer key at all: only a sanoid.defaults.conf without it gets here. Upstream's
# check_capacity_limit fails on undef and it dies.
HAND["prune_defer missing (a defaults file without it): fatal on both sides"] = S(
    {"tank/a": _sec("tank/a", daily=3, prune_defer=None)}, snaps, sbt)

# Upstream's early return is `$limit eq 0`, a string comparison: "00" is not "0", so the pool is still asked.
HAND["prune_defer = 00, pool at 0%: the pool is still asked, and pruning defers"] = S(
    {"tank/a": _sec("tank/a", daily=3, prune_defer="00")}, snaps, sbt, pool_caps={"tank": 0})

HAND["prune_defer = 000, pool at 50%: asked, not deferred"] = S(
    {"tank/a": _sec("tank/a", daily=3, prune_defer="000")}, snaps, sbt, pool_caps={"tank": 50})

HAND["prune_defer = 00 on [b] (path = tank/a): no pool named b, so the capacity query fails"] = S(
    {"b": _sec("tank/a", daily=3, prune_defer="00")}, snaps, sbt, pool_caps={"tank": 50})

HAND["prune_defer = 080, pool at 90%: read as 80"] = S(
    {"tank/a": _sec("tank/a", daily=3, prune_defer="080")}, snaps, sbt, pool_caps={"tank": 90})

# A `zfs destroy` that can't start (fork failing): upstream's system() returns -1, so it's `could not remove`,
# and the run goes on, flushing what it did destroy.
HAND["a destroy that can't start: that snapshot kept, the rest pruned and flushed"] = S(
    {"tank/a": _sec("tank/a", daily=3)}, snaps, sbt, fail_start=f"tank/a@{_DAILIES[1][0]}")

snaps_ab, sbt_ab = build({}, {"tank/a": {"daily": _DAILIES}, "tank/b": {"daily": _DAILIES}})
HAND["a destroy that can't start in one section: the next section is still pruned"] = S(
    {"tank/a": _sec("tank/a", daily=3), "tank/b": _sec("tank/b", daily=3)}, snaps_ab, sbt_ab,
    fail_start=f"tank/a@{_DAILIES[0][0]}")

HAND["a destroy that can't start, with pre_pruning_script and pruning_script"] = S(
    {"tank/a": _sec("tank/a", daily=3, pre_pruning_script=_script_for("prepr", 0), pruning_script=_script_for("postpr", 0))},
    snaps, sbt, fail_start=f"tank/a@{_DAILIES[2][0]}")

# A run directory no lock file can be made in (DEV-041): upstream prunes its first batch unlocked and its
# removelock dies before the flush; pysanoid prunes nothing and reports each batch.
HAND["no lock file can be made: nothing pruned, the batch reported"] = S(
    {"tank/a": _sec("tank/a", daily=3)}, snaps, sbt, run_unusable=True, known=[RUN_UNUSABLE])

HAND["no lock file can be made, two sections: each batch reported"] = S(
    {"tank/a": _sec("tank/a", daily=3), "tank/b": _sec("tank/b", daily=3)}, snaps_ab, sbt_ab, run_unusable=True,
    known=[RUN_UNUSABLE])

HAND["no lock file can be made: neither pre_pruning_script nor pruning_script runs"] = S(
    {"tank/a": _sec("tank/a", daily=3, pre_pruning_script=_script_for("prepr", 0), pruning_script=_script_for("postpr", 0))},
    snaps, sbt, run_unusable=True, known=[RUN_UNUSABLE])

HAND["no lock file can be made, readonly: upstream still locks, and dies releasing it"] = S(
    {"tank/a": _sec("tank/a", daily=3)}, snaps, sbt, readonly=True, run_unusable=True, known=[RUN_UNUSABLE])

HAND["no lock file can be made, nothing to prune: no lock needed"] = S(
    {"tank/a": _sec("tank/a", daily=3)}, snaps, {"tank/a": {"daily": snaptype(_DAILIES[-3:])}}, run_unusable=True)

HAND["no lock file can be made, prune_defer defers: no lock needed"] = S(
    {"tank/a": _sec("tank/a", daily=3, prune_defer=50)}, snaps, sbt, pool_caps={"tank": 40}, run_unusable=True)

# DEV-008, a count too long for a number: upstream reads a float, infinity past 308 digits, so
# none of that type is pruned and the run goes on to the next section.
_YEARLIES = [(f"autosnap_y{i}_yearly", NOW - (5 - i) * 366 * 86400) for i in range(5)]
_FREQUENTS = [(f"autosnap_f{i}_frequently", NOW - (9 - i) * 900) for i in range(9)]
snaps_y, sbt_y = build({}, {"tank/a": {"yearly": _YEARLIES, "frequently": _FREQUENTS}, "tank/b": {"daily": _DAILIES}})
for _digits in (19, 400, 5000):
    HAND[f"yearly = {_digits} nines: no yearly pruned, and the next section still is"] = S(
        {"tank/a": _sec("tank/a", yearly="9" * _digits), "tank/b": _sec("tank/b", daily=3)}, snaps_y, sbt_y)
    HAND[f"daily = {_digits} nines: no daily pruned"] = S({"tank/a": _sec("tank/a", daily="9" * _digits)}, snaps, sbt)
HAND["frequently = 400 nines, yearly = 2: the yearlies are pruned to 2, no frequent one"] = S(
    {"tank/a": _sec("tank/a", frequently="9" * 400, frequent_period=15, yearly=2)}, snaps_y, sbt_y)
HAND["daily = 400 nines and a comment: read as the nines"] = S(
    {"tank/a": _sec("tank/a", daily="9" * 400 + " # for good")}, snaps, sbt)
HAND["daily = 5000 zeros and a 3: a count of 3"] = S({"tank/a": _sec("tank/a", daily="0" * 5000 + "3")}, snaps, sbt)
HAND["prune_defer = 400 nines: fatal on both sides"] = S(
    {"tank/a": _sec("tank/a", daily=3, prune_defer="9" * 400)}, snaps, sbt, pool_caps={"tank": 50})

# What zfs prints that isn't UTF-8. Upstream never decodes it: a destroy that worked is one whatever zfs wrote beside it.
_NOISE = b"warning: caf\xe9\n".hex()
HAND["destroys that work while zfs prints a byte that isn't UTF-8: the 7 oldest pruned and flushed"] = S(
    {"tank/a": _sec("tank/a", daily=3)}, snaps, sbt, noise=_NOISE)
HAND["the same with pre_pruning_script and pruning_script: both run for each"] = S(
    {"tank/a": _sec("tank/a", daily=3, pre_pruning_script=_script_for("prepr", 0), pruning_script=_script_for("postpr", 0))},
    snaps, sbt, noise=_NOISE)
HAND["the same in two sections, the cache lock busy: both pruned, flushed at the end"] = S(
    {"tank/a": _sec("tank/a", daily=3), "tank/b": _sec("tank/b", daily=3)}, snaps_ab, sbt_ab, noise=_NOISE, cache_busy=True)
HAND["a destroy that fails with a complaint that isn't UTF-8: that snapshot kept, the rest pruned"] = S(
    {"tank/a": _sec("tank/a", daily=3)}, snaps, sbt,
    destroy_fail={f"tank/a@{_DAILIES[0][0]}": {"stderr_hex": b"cannot destroy: caf\xe9 is busy\n".hex()}})


def gen(r: random.Random) -> dict:
    path = f"tank/{r.choice('abcde')}"
    count = r.randint(0, 6)
    n = r.randint(0, 10)
    entries = [(f"autosnap_d{i}_daily", NOW - (n - i) * 86400 + r.randint(-1000, 1000)) for i in range(n)]
    types: dict[str, list[tuple[str, int]]] = {"daily": entries} if entries else {}
    hourly_count = None
    if r.random() < 0.3:  # a second type, so a run can have two batches to flush
        m = r.randint(1, 8)
        types["hourly"] = [(f"autosnap_h{i}_hourly", NOW - (m - i) * 3600 + r.randint(-100, 100)) for i in range(m)]
        hourly_count = r.randint(0, 4)
    snaps, sbt = build({}, {path: types} if types else {})
    cfg: dict = {"path": path, "autoprune": r.random() < 0.9, "process_children_only": r.random() < 0.1,
                "daily": count, "prune_defer": 0}
    if hourly_count is not None:
        cfg["hourly"] = hourly_count
    if r.random() < 0.3:
        # strings as the config file gives them; "00"/"000" are not upstream's string "0"
        cfg["prune_defer"] = r.choice([0, 10, 50, 90, "0", "00", "000", "080"])
    elif r.random() < 0.05:
        del cfg["prune_defer"]  # a defaults file without it: fatal on both sides
    if r.random() < 0.3:
        cfg["pre_pruning_script"] = r.choice([_script_for("pre", 0), _script_for("pre", 0), _script_for("pre", 1), "0"])
    if r.random() < 0.2:
        cfg["pruning_script"] = r.choice([_script_for("post", 0), "0"])
    config = {path: cfg}
    pool_cap = 0 if r.random() < 0.2 else r.randint(0, 100)  # an empty pool is where "00" defers
    pool_caps = {path.split("/")[0]: pool_cap}
    if r.random() < 0.2:
        # A second section on the same path does the pruning, with hooks from [path]. Its own veto hook is
        # blank or the same command (a different one is the documented deviation, a HAND case).
        config["b"] = dict(cfg, pre_pruning_script=r.choice(["", cfg.get("pre_pruning_script", "")]),
                           pruning_script=r.choice(["", _script_for("own_post", 0)]))
        cfg["daily"] = 30  # [path] itself never prunes: every daily here is at most 10 days old
        if hourly_count is not None:
            cfg["hourly"] = 30
        if r.random() < 0.7:  # upstream takes the pool from the section name; otherwise asking about "b" fails
            pool_caps["b"] = pool_cap
    destroy_fail = {}
    if entries and r.random() < 0.1:
        victim = r.choice(entries)[0]
        destroy_fail[f"{path}@{victim}"] = {"stderr": "cannot destroy: dataset is busy\n"}
    fail_start = f"{path}@{r.choice(entries)[0]}" if entries and r.random() < 0.1 else ""
    verbose, readonly, lock_free, cache_busy = r.random() < 0.6, r.random() < 0.1, r.random() < 0.85, r.random() < 0.15
    late = random.Random(repr((path, count, n, hourly_count, sorted(config))))  # a stream of its own: other scenarios stay
    if late.random() < 0.06:
        pruning = config.get("b", cfg)  # the section whose counts prune
        pruning[late.choice(["daily", "hourly"])] = "9" * late.choice([19, 400, 5000])  # a count too long for a number
    noise = _NOISE if late.random() < 0.08 else ""  # zfs warns on every destroy, in bytes that aren't UTF-8
    return S(config, snaps, sbt, pool_caps=pool_caps, verbose=verbose, readonly=readonly, lock_free=lock_free,
             run_unusable=lock_free and r.random() < 0.06, destroy_fail=destroy_fail, cache_busy=cache_busy,
             fail_start=fail_start, noise=noise)


def show(scn: dict, a: dict, b: dict, diffs: list[str]) -> None:
    print(f"  config={scn['config']}" + ("  (no lock file can be made)" if scn.get("run_unusable") else "")
          + ("  (zfs prints a byte that isn't UTF-8)" if scn.get("noise") else ""))
    for k in diffs:
        if k in a:
            print(f"    {k}: perl={a[k]!r}\n         python={b[k]!r}")
        else:  # pysanoid against itself, or whether each side found a batch that needs the lock
            print(f"    {k}: perl died={a['died_how']!r}\n         python destroyed={b['destroyed']} hook_log={b['hook_log']} "
                  f"lock_tries={b['lock_tries']} lock_skips={b['lock_skips']} died={b['died_how']!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=400)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()

    tally = {"match": 0, "DIFF": 0}
    known = 0
    if args.mode == "hand":
        for name, scn in HAND.items():
            a, b, diffs, classed = run_both(scn)
            if diffs and name.startswith("KNOWN DIVERGENCE"):
                known += 1
                print(f"[KNOWN] {name}  (diffs={diffs}, expected)")
                continue
            expected = [k for k in diffs if k in scn["known_diffs"]]
            diffs = [k for k in diffs if k not in expected]
            ok = not diffs and sorted(classed) == sorted(scn["known"])
            tally["match" if ok else "DIFF"] += 1
            note = f"  (known diffs={expected}, expected)" if expected else ""
            if sorted(classed) != sorted(scn["known"]):
                note += f"  (classed {classed}, expected {scn['known']})"
            print(f"[{'DIFF ' if not ok else 'KNOWN' if classed else 'MATCH'}] {name}{note}")
            if diffs:
                show(scn, a, b, diffs)
    else:
        r = random.Random(args.seed)
        seen: dict[tuple, dict] = {}
        for _ in range(args.n):
            scn = gen(r)
            a, b, diffs, _ = run_both(scn)
            tally["match" if not diffs else "DIFF"] += 1
            if diffs:
                seen.setdefault(tuple(diffs), scn)
        for sig, scn in seen.items():
            a, b, diffs, _ = run_both(scn)
            print(f"\n--- differs on {sig}")
            show(scn, a, b, diffs)
    print(tally, "known divergences:", known, "known classes:", dict(KNOWN))
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if tally["DIFF"] else 0)


if __name__ == "__main__":
    main()
