#!/usr/bin/env python3
"""Differential test: upstream's removecachedsnapshots / addcachedsnapshots (Module 3), extracted verbatim with the
getchilddatasets the add calls, vs pysanoid's remove_cached_snapshots / add_cached_snapshots.

Perl scoping: the subs read $cache, %pruned, %taken, @cachedatasets, %args, $zfs and the lock functions from the outer
lexical scope. HEAD stubs the locks (checklock finds the lock held for its first $LOCK_BUSY calls, and sleep returns at
once, so a run that waits polls once and goes on) and getsnaps, which removecachedsnapshots calls to re-read the
snapshot list (verify_upstream_reread_moves_boundary checks what that re-read does). getchilddatasets runs for real:
@cachedatasets holds the dataset cache as init reads it, and a lookup it can't answer runs dev/fake_zfs, or a path that
can't start.

Compared: whether it died, the cache's bytes and whether its mtime was kept, stdout (verbose; "another pysanoid process"
read as upstream's "another sanoid process"), the child lookups sent to zfs, and whether pysanoid reported a problem.
Upstream appends in hash order, so the appended lines and the lookups are compared as sets.

Known classes, from DEV-036, DEV-039c, DEV-039e and DEV-041: a missing cache, which pysanoid leaves missing where
upstream's remove writes an empty, fresh one and its add dies; an add onto a cache missing its final newline, which
was cut short and is no cache, so pysanoid must leave it as it is where upstream fuses its last line with the first
new one; a child lookup that can't start, which both leave out of the cache and pysanoid reports; one
killed partway, where upstream keeps the lines it printed and pysanoid leaves the children out and reports it; and
a run directory no lock file can be made in (lock mode "unusable"), where upstream updates the cache
unlocked and dies releasing the lock, as its real removelock did end to end, and pysanoid must remove the cache and
report it without dying.

Hand mode also runs verify_upstream_reread_moves_boundary. It is outside the tally, but a failure fails the run: the
DEV-013 would no longer describe upstream.

Run: python3 dev/diff_snapshot_cache_mutation.py hand
     python3 dev/diff_snapshot_cache_mutation.py fuzz <n> <seed>
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
import shutil
import subprocess
import sys
from collections import Counter
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

WORK = DEV_DIR / "work" / "snapshot_cache_mutation"
FAKE_ZFS = DEV_DIR / "fake_zfs" / "zfs"
NO_ZFS = "/nonexistent/zfs"  # a lookup that can't start, as when the fork fails
NOW = 1_790_000_000
p.time.time = lambda: float(NOW)
DATASETS = ["tank", "tank/a", "tank/a/x", "tank/b", "pool2"]

HEAD = PERL_JSON_HEAD + r'''
use File::Copy;
BEGIN {
    *CORE::GLOBAL::time = sub () { __NOW__ + 0 };
    *CORE::GLOBAL::sleep = sub { 0 };
}
my %args = ('verbose' => 1, 'debug' => 0);
my $zfs = $ENV{ZFS_BIN};
my $workdir = shift @ARGV;
my $cache = "$workdir/cache/snapshots.txt";
my $run_dir = "$workdir/run";
my $cacheTTL = 1200;
my (%pruned, %taken, %snaps, %config, @cachedatasets);
my $__busy = $ENV{LOCK_BUSY} || 0;
sub checklock  { if ($__busy > 0) { $__busy--; return 0 } return 1 }
sub writelock  { 1 }
# RUN_UNUSABLE: no lock file could be made, so writelock's unchecked open wrote nothing and checklock finds none
sub removelock { die "ERROR: No valid lockfile found - Did a rogue process or user update or delete it?\n" if $ENV{RUN_UNUSABLE}; 1 }
sub getsnaps   { return () }
'''.replace("__NOW__", str(NOW))

TAIL = f'''
{READ_SCENARIO_PERL}
@cachedatasets = map {{ "$_\\n" }} @{{ $__scn->{{dataset_cache}} }};
%pruned = map {{ $_ => 1 }} @{{ $__scn->{{pruned}} }};
foreach my $s (keys %{{ $__scn->{{taken}} }}) {{
    $taken{{$s}} = {{ time => $__scn->{{taken}}{{$s}}{{time}}, recursive => ($__scn->{{taken}}{{$s}}{{recursive}} ? 1 : 0) }};
}}
my $died = '';
eval {{
    if ($__scn->{{op}} eq 'remove') {{ removecachedsnapshots($__scn->{{wait}} ? 1 : 0) }}
    else {{ addcachedsnapshots() }}
    1
}} or do {{ ($died = $@) =~ s/\\s+$//; }};
my %__result = ( died => $died );
{WRITE_RESULT_PERL}
'''

SCRIPT = build_harness(HEAD, extract_subs("removecachedsnapshots", "addcachedsnapshots", "getchilddatasets"), TAIL)


def _setup(wd: Path, scn: dict) -> None:
    shutil.rmtree(wd, ignore_errors=True)
    (wd / "cache").mkdir(parents=True)
    if scn["lock"] == "unusable":
        (wd / "run").write_text("")  # a regular file where the run directory should be
    else:
        (wd / "run").mkdir()
    if scn["cache"] is not None:
        f = wd / "cache" / "snapshots.txt"
        f.write_bytes(scn["cache"]["bytes"])
        os.utime(f, (NOW - scn["cache"]["age"], NOW - scn["cache"]["age"]))
    (wd / "zfs.log").write_text("")
    state = {"datasets": scn["zfs_datasets"], "snapshots": [], "list_killed": scn["lookup_killed"], "list_prints": 2,
             "list_cut": scn["lookup_cut"]}
    (wd / "zfs_state.json").write_text(json.dumps(state))


def _zfs_env(wd: Path, scn: dict) -> dict[str, str]:
    return {"ZFS_BIN": NO_ZFS if scn["zfs_cannot_start"] else str(FAKE_ZFS),
            "FAKE_ZFS_LOG": str(wd / "zfs.log"), "FAKE_ZFS_STATE": str(wd / "zfs_state.json")}


def _result(wd: Path, scn: dict, died: bool, stdout: str, reported: bool) -> dict:
    f = wd / "cache" / "snapshots.txt"
    old_mtime = (NOW - scn["cache"]["age"]) if scn["cache"] is not None else None
    return {
        "died": died,
        "cache": f.read_bytes() if f.exists() else None,
        "mtime_preserved": (int(f.stat().st_mtime) == old_mtime) if f.exists() and old_mtime is not None else None,
        "stdout": stdout.replace("another pysanoid process", "another sanoid process"),  # the program's own name
        "zfs_calls": sorted(json.loads(x) for x in (wd / "zfs.log").read_text().splitlines() if x),
        "reported": reported,
    }


def run_perl(wd: Path, scn: dict) -> dict:
    wd = wd.resolve()  # see diff_prune.py's run_perl for why: a relative wd double-resolves inside the Perl child
    _setup(wd, scn)
    stdin_obj = {k: scn[k] for k in ("op", "wait", "pruned", "taken", "dataset_cache")}
    env = {"LOCK_BUSY": "1" if scn["lock"] in ("busy", "busy_then_free") else "0",
           "RUN_UNUSABLE": "1" if scn["lock"] == "unusable" else "", **_zfs_env(wd, scn)}
    result = run_harness(SCRIPT, stdin_obj=stdin_obj, argv=[str(wd)], env=env)
    if result.data is None:
        raise RuntimeError(f"perl harness produced no result:\n{result.stderr}")
    return _result(wd, scn, bool(result.data["died"]), result.stdout, False)


def run_python(wd: Path, scn: dict) -> dict:
    wd = wd.resolve()
    _setup(wd, scn)
    env = _zfs_env(wd, scn)
    os.environ.update(env)
    p.ZFS = env["ZFS_BIN"]
    real_flock = fcntl.flock
    held = None
    if scn["lock"] == "busy":  # another process holds it: a real flock contender
        held = open(wd / "run" / "sanoid_cacheupdate.lock", "w")  # noqa: SIM115 - held open across this function, released in `finally`
        real_flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif scn["lock"] == "busy_then_free":  # held when first tried, then released: only the first non-blocking try fails
        tries: list[int] = []

        def busy_at_first(fd: Any, operation: int) -> None:
            if operation & fcntl.LOCK_NB and not tries:
                tries.append(operation)
                raise BlockingIOError(errno.EWOULDBLOCK, "Resource temporarily unavailable")
            real_flock(fd, operation)

        fcntl.flock = busy_at_first
    died, problems, out = False, [], io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            if scn["op"] == "remove":
                p.remove_cached_snapshots(list(scn["pruned"]), wd / "cache", wd / "run", wait=scn["wait"], verbose=True,
                                          problems=problems)
            else:
                taken = {k: {"time": v["time"], "recursive": v["recursive"]} for k, v in scn["taken"].items()}
                zfs = p.ZfsClient(wd / "cache", 1200, True)
                zfs._cached_datasets = list(scn["dataset_cache"])  # as load_config leaves it (see diff_dataset_cache.py)
                p.add_cached_snapshots(taken, wd / "cache", wd / "run", zfs, verbose=True, problems=problems)
    except Exception:  # noqa: BLE001 - harness: any failure counts as "died"
        died = True
    finally:
        fcntl.flock = real_flock
        if held:
            held.close()
    return _result(wd, scn, died, out.getvalue(), bool(problems))


NO_CACHE_REMOVE = "remove with no cache: upstream writes an empty, fresh one"
NO_CACHE_ADD = "add with no cache: upstream dies"
NO_FINAL_NEWLINE = "add onto a cache missing its final newline: upstream fuses the lines, pysanoid leaves the file as it is"
LOOKUP_CANNOT_START = "a child lookup that can't start: both leave the children out, pysanoid reports it"
LOOKUP_KILLED = "a child lookup killed partway: upstream keeps the lines it printed, pysanoid reports it and keeps none"
RUN_UNUSABLE = "no lock file can be made: upstream updates unlocked and dies releasing the lock, pysanoid drops the cache"
KNOWN: Counter[str] = Counter()


def _needs_lookup(scn: dict) -> bool:
    """Whether the add asks zfs for a recursive snapshot's children: the dataset cache doesn't list the parent."""
    listed = set(scn["dataset_cache"])
    return scn["cache"] is not None and any(v["recursive"] and k.split("@")[0] not in listed for k, v in scn["taken"].items())


def run_both(scn: dict) -> tuple[dict, dict, list[str], list[str]]:
    a, b = run_perl(WORK / "perl", scn), run_python(WORK / "python", scn)
    orig = scn["cache"]["bytes"] if scn["cache"] is not None else None
    if scn["op"] == "add" and orig is not None:  # upstream iterates %taken in hash order: the new lines as a set
        for r in (a, b):
            if r["cache"] is not None and r["cache"].startswith(orig):
                r["cache"] = (orig, sorted(r["cache"][len(orig):].split(b"\n")))
    diffs = [k for k in a if a[k] != b[k]]
    # Deliberate deviations (DEV-036, DEV-039c, DEV-039e, DEV-041), classed rather than shown as a false DIFF on every run:
    known: list[str] = []
    if orig is None and b["cache"] is None and not b["died"]:
        if scn["op"] == "remove" and diffs == ["cache"] and a["cache"] == b"":
            known.append(NO_CACHE_REMOVE)
            diffs = []
        elif scn["op"] == "add" and diffs == ["died"] and a["died"]:
            known.append(NO_CACHE_ADD)
            diffs = []
    if scn["op"] == "add" and orig and not orig.endswith(b"\n") and "cache" in diffs:
        # pysanoid against itself: the file as it was (no lines appended), unless the run directory made it drop it
        if b["cache"] == (orig, [b""]) or scn["lock"] == "unusable":
            known.append(NO_FINAL_NEWLINE)
            diffs.remove("cache")
        else:
            diffs.append("no-final-newline self-check")
    if "reported" in diffs and b["reported"] and scn["zfs_cannot_start"] and _needs_lookup(scn):
        known.append(LOOKUP_CANNOT_START)
        diffs.remove("reported")
    if b["reported"] and scn["lookup_killed"] and _needs_lookup(scn) and set(diffs) <= {"cache", "reported"}:
        known.append(LOOKUP_KILLED)
        diffs = []
    if scn["lock"] == "unusable" and (scn["pruned"] if scn["op"] == "remove" else scn["taken"]):
        # pysanoid against itself: the cache removed and reported, never a death
        if b["died"] or b["cache"] is not None or not b["reported"]:
            diffs.append("unusable self-check")
        elif a["died"] and set(diffs) <= {"died", "cache", "mtime_preserved", "reported", "stdout", "zfs_calls"}:
            known = [RUN_UNUSABLE]
            diffs = []
    if scn.get("upstream_caches") is not None:  # the line a reproduction says upstream caches (DEV-039e's evidence)
        new_lines = a["cache"][1] if isinstance(a["cache"], tuple) else []
        if not any(line.startswith(scn["upstream_caches"]) for line in new_lines):
            diffs.append(f"upstream did not cache {scn['upstream_caches']!r}")
    KNOWN.update(known)
    return a, b, diffs, known


def L(ds: str, name: str, ct: int = 1_780_000_000) -> bytes:
    return f"{ds}@{name}\tcreation\t{ct}\t-\n".encode()


def T(text: bytes, age: int = 100) -> dict:
    return {"bytes": text, "age": age}


C = b"".join([L("tank", "autosnap_a_daily", 1), L("tank", "autosnap_b_daily", 2),
              L("tank/a", "autosnap_a_daily", 3), L("tank/a/x", "autosnap_a_daily", 4)])


def S(op: str, **kw: Any) -> dict:
    scn = {"op": op, "wait": False, "lock": "free", "cache": T(C), "pruned": [], "taken": {}, "dataset_cache": DATASETS,
           "zfs_datasets": DATASETS, "zfs_cannot_start": False, "lookup_killed": False, "lookup_cut": None,
           "upstream_caches": None}
    scn.update(kw)
    return scn


A_DAILY = ["tank@autosnap_a_daily"]
PLAIN = {"tank@autosnap_c_daily": {"time": 1_789_999_999, "recursive": False}}
RECURSIVE = {"tank@autosnap_c_daily": {"time": 1_789_999_999, "recursive": True}}

HAND = {
    "remove: two of four lines, mtime preserved": S("remove", pruned=["tank@autosnap_a_daily", "tank/a@autosnap_a_daily"]),
    "remove: a name that isn't in the cache": S("remove", pruned=["nope@x_daily"]),
    "remove: nothing pruned -> untouched": S("remove"),
    "remove: everything pruned -> empty cache file": S("remove", pruned=[
        "tank@autosnap_a_daily", "tank@autosnap_b_daily", "tank/a@autosnap_a_daily", "tank/a/x@autosnap_a_daily"]),
    "remove: duplicate lines of one snapshot all go": S("remove", cache=T(C + L("tank", "autosnap_a_daily", 1)), pruned=A_DAILY),
    "remove: junk and blank lines survive": S("remove", cache=T(b"junk\n\n" + C + b"\t\n"), pruned=A_DAILY),
    "remove: the kept last line has no newline -> left so": S("remove", cache=T(C.rstrip(b"\n")), pruned=A_DAILY),
    "remove: a line without a tab spelling a pruned name stays": S("remove", pruned=A_DAILY,
        cache=T(C + b"tank@autosnap_a_daily\n" + L("tank", "autosnap_z_daily", 9))),
    "remove: ...unless it is the last line, without a newline": S("remove", cache=T(C + b"tank@autosnap_a_daily"),
                                                                   pruned=A_DAILY),
    "remove: a CRLF line of a pruned snapshot goes": S("remove", pruned=A_DAILY,
        cache=T(L("tank", "autosnap_a_daily", 1).replace(b"\n", b"\r\n") + L("tank", "autosnap_b_daily", 2))),
    "remove: a line starting with a tab stays": S("remove", cache=T(b"\ttank@autosnap_a_daily\n" + C), pruned=A_DAILY),
    "remove: lock held, non-blocking -> deferred": S("remove", lock="busy", pruned=A_DAILY),
    "remove: lock held, blocking -> waits, then removes": S("remove", wait=True, lock="busy_then_free", pruned=A_DAILY),
    "remove: cache file missing": S("remove", cache=None, pruned=A_DAILY, known=[NO_CACHE_REMOVE]),
    "add: one plain snapshot": S("add", taken=PLAIN),
    "add: nothing taken -> untouched": S("add"),
    "add: lock held -> waits, then adds": S("add", lock="busy_then_free", taken=PLAIN),
    "add: recursive, the dataset cache lists the children": S("add", taken=RECURSIVE),
    "add: recursive, no dataset cache -> zfs lists the children": S("add", taken=RECURSIVE, dataset_cache=[]),
    "add: recursive, the dataset cache lacks the parent -> zfs": S("add", taken=RECURSIVE,
                                                                  dataset_cache=["tank/a", "tank/b"]),
    "add: recursive, zfs doesn't know the parent -> no children": S("add", taken=RECURSIVE, dataset_cache=[],
                                                                   zfs_datasets=["pool2"]),
    "add: recursive, the child lookup can't start": S("add", taken=RECURSIVE, dataset_cache=[], zfs_cannot_start=True,
                                                     known=[LOOKUP_CANNOT_START]),
    "add: recursive, zfs can't start but isn't asked": S("add", taken=RECURSIVE, zfs_cannot_start=True),
    "add: recursive, the child lookup is killed after two lines": S("add", taken=RECURSIVE, dataset_cache=[],
                                                                   lookup_killed=True, known=[LOOKUP_KILLED]),
    # DEV-039e's reproduction: a lookup killed three bytes into its second line ("tank\ntan", where it would have said
    # "tank/a") gives upstream a child "tan", and a snapshot of it that no pool has, dated now
    "add: recursive, the child lookup is cut inside its second line": S(
        "add", taken=RECURSIVE, dataset_cache=[], lookup_killed=True, lookup_cut=8, upstream_caches=b"tan@autosnap_c_daily\t",
        known=[LOOKUP_KILLED]),
    "add: cache file missing": S("add", cache=None, taken=PLAIN, known=[NO_CACHE_ADD]),
    "add: cache has no trailing newline": S("add", cache=T(C.rstrip(b"\n")), taken=PLAIN, known=[NO_FINAL_NEWLINE]),
    "remove: no lock file can be made": S("remove", lock="unusable", pruned=A_DAILY, known=[RUN_UNUSABLE]),
    "remove, blocking: no lock file can be made": S("remove", wait=True, lock="unusable", pruned=A_DAILY, known=[RUN_UNUSABLE]),
    "add: no lock file can be made": S("add", lock="unusable", taken=RECURSIVE, dataset_cache=[], known=[RUN_UNUSABLE]),
    "add: no lock file can be made, nothing taken -> no lock needed": S("add", lock="unusable"),
}


NAMES = [f"autosnap_{i}_daily" for i in range(6)]


def gen(r: random.Random) -> dict:
    def line(ds: str, name: str) -> bytes:
        shape = r.random()
        if shape < 0.08:
            return f"{ds}@{name}\n".encode()  # no tab: upstream's first column keeps the newline
        if shape < 0.14:
            return L(ds, name, r.randint(1, 10**9)).replace(b"\n", b"\r\n")
        if shape < 0.18:
            return f"\t{ds}@{name}\n".encode()  # an empty first column
        return L(ds, name, r.randint(1, 10**9))

    lines = [line(r.choice(DATASETS), r.choice(NAMES)) for _ in range(r.randint(0, 10))]
    lines += [r.choice([b"junk\n", b"\n", b"\t\n", b"tank@x\tused\t5\t-\n"]) for _ in range(r.randint(0, 2))]
    r.shuffle(lines)
    text = b"".join(lines)
    if text and r.random() < 0.15:
        text = text.rstrip(b"\n")
    cache = None if r.random() < 0.1 else T(text, r.choice([5, 900, 5000]))
    if r.random() < 0.5:
        pruned = [f"{r.choice(DATASETS)}@{r.choice(NAMES)}" for _ in range(r.randint(0, 4))] + r.choice([[], ["nope@x_daily"]])
        wait = r.random() < 0.3
        lock = "free" if r.random() < 0.8 else ("busy_then_free" if wait else "busy")  # waiting on a lock never freed blocks forever
        if r.random() < 0.06:
            lock = "unusable"
        return S("remove", wait=wait, lock=lock, cache=cache, pruned=pruned)
    taken = {f"{r.choice(DATASETS)}@autosnap_new{i}_hourly": {"time": r.randint(1, 10**9), "recursive": r.random() < 0.5}
             for i in range(r.randint(0, 3))}
    dataset_cache = r.choice([DATASETS, DATASETS, [d for d in DATASETS if r.random() < 0.6], []])
    lookup = r.choices(["ok", "cannot_start", "killed"], weights=[75, 15, 10])[0]
    lock = r.choices(["free", "busy_then_free", "unusable"], weights=[76, 18, 6])[0]
    return S("add", lock=lock, cache=cache, taken=taken,
             dataset_cache=dataset_cache, zfs_cannot_start=lookup == "cannot_start", lookup_killed=lookup == "killed")


# -- the standing check of upstream's re-read (DEV-013) ------------------------------------------------------------------

BOUNDARY_ZFS = NOW + 1 - 86400  # zfs's creation of tank/b's older daily; the cache holds one second later

REREAD_HEAD = PERL_JSON_HEAD + r'''
our $NOW = $ENV{NOW0} + 0;
our (@DESTROYED, @ORDER);
BEGIN {
    *CORE::GLOBAL::time = sub () { $main::NOW };
    *CORE::GLOBAL::system = sub {
        if (($_[1] // '') eq 'destroy') { push @main::DESTROYED, $_[2]; $main::NOW += 2; return 0 }
        return CORE::system(@_);
    };
}
my %args = ('verbose' => 0, 'debug' => 0);
my $zfs = $ENV{ZFS_BIN};
my $cache = "$ENV{WORKDIR}/cache/snapshots.txt";
my $cacheTTL = 1200;
my (%config, %snaps, %snapsbytype, %snapsbypath, %pruned);
my @params = (\%config, \%snaps, \%snapsbytype, \%snapsbypath);
sub checklock { 1 }
sub writelock { 1 }
sub removelock { 1 }
sub check_prune_defer { push @main::ORDER, $_[1]; return 0 }
sub get_date { return () }
sub runscript { return 0 }
'''
REREAD_TAIL = r'''
for my $ds ('tank/a', 'tank/b') {
    $config{$ds} = { path => $ds, autoprune => 1, autosnap => 1, process_children_only => 0, daily => 1 };
}
%snaps = getsnaps(\%config, $cacheTTL, 0);
%snapsbytype = getsnapsbytype(\%config, \%snaps);
prune_snapshots(@params);
my %__result = (destroyed => \@DESTROYED, order => \@ORDER);
''' + WRITE_RESULT_PERL
REREAD_SCRIPT = build_harness(REREAD_HEAD, extract_subs("prune_snapshots", "removecachedsnapshots", "getsnaps",
                                                        "get_active_datasets", "getsnapsbytype"), REREAD_TAIL)


def _reread_setup(wd: Path, zfs_ctime: int, cache_age: int) -> None:
    """[tank/a] and [tank/b], daily = 1. tank/a's 10-day-old daily goes when its section is decided, and the destroy
    moves the clock 2 s, past the cache's TTL when it was `cache_age` old. tank/b's older daily is then on the age
    boundary: the cache holds BOUNDARY_ZFS + 1, as a cache add records it, and zfs `zfs_ctime`."""
    shutil.rmtree(wd, ignore_errors=True)
    for sub in ("cache", "run", "conf"):
        (wd / sub).mkdir(parents=True)

    def rows(boundary: int) -> list[str]:
        return [f"tank/a@autosnap_a_old_daily\tcreation\t{NOW - 10 * 86400}\t-",
                f"tank/a@autosnap_a_new_daily\tcreation\t{NOW - 100}\t-",
                f"tank/b@autosnap_b_boundary_daily\tcreation\t{boundary}\t-",
                f"tank/b@autosnap_b_new_daily\tcreation\t{NOW - 100}\t-"]

    cache = wd / "cache" / "snapshots.txt"
    cache.write_text("".join(f"{row}\n" for row in rows(BOUNDARY_ZFS + 1)))
    os.utime(cache, (NOW - cache_age, NOW - cache_age))
    snapshots = [{"ds": row.split("@")[0], "line": row} for row in rows(zfs_ctime)]
    (wd / "zfs_state.json").write_text(json.dumps({"datasets": ["tank", "tank/a", "tank/b"], "snapshots": snapshots}))
    (wd / "zfs.log").write_text("")


def _reread_perl(wd: Path, seed: int) -> tuple[list[str], list[str]]:
    env = {"NOW0": str(NOW), "WORKDIR": str(wd), "PERL_HASH_SEED": str(seed), "ZFS_BIN": str(FAKE_ZFS),
           "FAKE_ZFS_LOG": str(wd / "zfs.log"), "FAKE_ZFS_STATE": str(wd / "zfs_state.json")}
    result = run_harness(REREAD_SCRIPT, env=env)
    if result.data is None:
        raise RuntimeError(f"perl re-read check produced no result:\n{result.stderr}")
    return result.data["order"], result.data["destroyed"]


def _reread_python(wd: Path) -> list[str]:
    clock, destroyed = [float(NOW)], []

    def destroy(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        destroyed.append(cmd[2])
        clock[0] += 2
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    body = "\tdaily = 1\n\thourly = 0\n\tweekly = 0\n\tmonthly = 0\n\tyearly = 0\n\tfrequently = 0\n"
    (wd / "conf" / "sanoid.defaults.conf").write_bytes((DEV_DIR.parent / "sanoid.defaults.conf").read_bytes())
    (wd / "conf" / "sanoid.conf").write_text(f"[tank/a]\n{body}[tank/b]\n{body}")
    saved = p.time.time, p.run_zfs_cmd
    p.time.time, p.run_zfs_cmd = (lambda: clock[0]), destroy
    try:
        config = p.load_config(wd / "conf", p.ZfsClient(wd / "cache", 1200, False))
        snaps = p.get_snapshots(config, wd / "cache", wd / "run", 1200)
        p.prune_snapshots(config, snaps, p.snapshots_by_type(snaps, config), wd / "cache", wd / "run")
    finally:
        p.time.time, p.run_zfs_cmd = saved
    return destroyed


def verify_upstream_reread_moves_boundary() -> bool:
    """Standing check of DEV-013, upstream's re-read. After a prune flush, removecachedsnapshots
    re-reads the snapshot list, listing zfs again once the cache has expired, and later sections are decided on zfs's
    creation times; a cache add records a later one (when `zfs snapshot` returned). Expected over Perl hash seeds:
    upstream destroys tank/b's boundary daily exactly when tank/a's section goes first, never when zfs agrees with the
    cache or the cache hasn't expired; pysanoid never does. Prints CONFIRMED or NOT REPRODUCED and returns which."""
    wd = (WORK / "reread").resolve()
    boundary = "tank/b@autosnap_b_boundary_daily"
    orders: set[tuple[str, ...]] = set()
    failures: list[str] = []
    for label, zfs_ctime, cache_age in (("expired by the flush, zfs a second earlier", BOUNDARY_ZFS, 1200),
                                        ("control: zfs agrees with the cache", BOUNDARY_ZFS + 1, 1200),
                                        ("control: the cache hasn't expired", BOUNDARY_ZFS, 1195)):
        for seed in range(12):
            _reread_setup(wd, zfs_ctime, cache_age)
            order, destroyed = _reread_perl(wd, seed)
            moved = label.startswith("expired")
            if moved:
                orders.add(tuple(order))
            if (boundary in destroyed) != (moved and order[:1] == ["tank/a"]):
                failures.append(f"{label}, seed {seed}: sections {order}, upstream destroyed {destroyed}")
        _reread_setup(wd, zfs_ctime, cache_age)
        if boundary in _reread_python(wd):
            failures.append(f"{label}: pysanoid destroyed {boundary}")
    if len(orders) < 2:
        failures.append(f"only saw the section order(s) {sorted(orders)}; the check needs both")
    ok = not failures
    print(f"[{'CONFIRMED' if ok else 'NOT REPRODUCED'}] upstream's re-read after a prune flush destroys a boundary "
          "snapshot a run sooner, pysanoid keeps it (DEV-013)")
    for failure in failures:
        print(f"    {failure}")
    return ok


def show(a: dict, b: dict, diffs: list[str]) -> None:
    for k in diffs:
        if k in a:
            print(f"    {k}: perl={a[k]!r}\n    {' ' * len(k)}  python={b[k]!r}")
        else:  # a self-check of pysanoid's own result
            print(f"    {k}: python died={b['died']} cache={b['cache']!r} reported={b['reported']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=400)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()

    tally = {"match": 0, "DIFF": 0}
    reread_ok = True
    if args.mode == "hand":
        for name, scn in HAND.items():
            a, b, diffs, known = run_both(scn)
            expected = scn.get("known", [])
            ok = not diffs and sorted(known) == sorted(expected)
            tally["match" if ok else "DIFF"] += 1
            label = "DIFF " if not ok else ("KNOWN" if known else "MATCH")
            print(f"[{label}] {name}" + (f"  (classed {known}, expected {expected})" if sorted(known) != sorted(expected) else ""))
            if diffs:
                show(a, b, diffs)
        print()
        reread_ok = verify_upstream_reread_moves_boundary()
    else:
        r = random.Random(args.seed)
        seen: dict[tuple, tuple] = {}
        for _ in range(args.n):
            scn = gen(r)
            a, b, diffs, _ = run_both(scn)
            tally["match" if not diffs else "DIFF"] += 1
            if diffs:
                seen.setdefault(tuple(diffs), (scn, a, b))
        for sig, (scn, a, b) in seen.items():
            print(f"\n--- differs on {sig}: op={scn['op']} lock={scn['lock']} wait={scn['wait']} "
                  f"zfs_cannot_start={scn['zfs_cannot_start']} lookup_killed={scn['lookup_killed']} dataset_cache={scn['dataset_cache']} "
                  f"cache={'missing' if scn['cache'] is None else scn['cache']['bytes'][:120]!r}")
            print("    pruned/taken:", scn["pruned"] or scn["taken"])
            show(a, b, list(sig))
    print(tally, "known/documented classes:", KNOWN)
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if tally["DIFF"] or not reread_ok else 0)


if __name__ == "__main__":
    main()
