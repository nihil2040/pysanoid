#!/usr/bin/env python3
"""Differential test: upstream's get_active_datasets / getsnaps / getsnapsbytype / getsnapsbypath
(Module 3), extracted verbatim, vs pysanoid's get_active_dataset_paths / get_snapshots /
snapshots_by_type / snapshots_by_path -- against a shared fake `zfs`. Compared: whether it died, every
snapshot's creation time as a number, the per-type and per-path tables, the active dataset list, every
zfs call, the cache file left behind, and stdout (verbose, with and without --debug). The tables are
built as main() builds them, on a clock with a fraction (py_frac). Snapshots made in the same second
are compared as a set: upstream's order among them is Perl's per-process random hash order
(DEV-014).

Perl scoping note: getsnaps/getsnapsbytype/getsnapsbypath all read $zfs / %args / the cache and run
paths through the OUTER lexical scope (upstream's top-level script variables), not their own @_
parameters for everything -- HEAD below declares those before the subs are compiled, matching how
upstream's own file scoping makes them visible (see dev/lib/extract.py's module docstring and
dev/README.md for why this must be done this way, not worked around).

Known classes, from DEV-033 to DEV-039b and DEV-041 (KNOWN below): a `zfs get` killed by a
signal (DEV-034: pysanoid retries without dataset names, where upstream's `$? >> 8` keeps the partial listing);
a fresh cache that can't be read, and a held update lock with no readable cache (DEV-039a: pysanoid reads zfs,
where upstream reads no snapshots); a cache directory that can't be written, and a directory named
snapshots.txt (DEV-039b, DEV-039a: pysanoid carries on, where upstream dies); a snapshot name holding a type and
"creation" (DEV-033: pysanoid ignores it, where upstream reads a snapshot of that type made at the time its
digits give); a cache holding a NUL byte, or cut short inside its last line (DEV-038, DEV-036: pysanoid reads zfs
as with no cache, where upstream reads the damaged lines); a cache dated in the future (DEV-037: pysanoid reads zfs
as with an expired one, where upstream uses it); a retried `zfs get` killed inside its last line (DEV-035: pysanoid
leaves that line out, where upstream reads it); and a run directory no lock
file can be made in (DEV-041: pysanoid reads zfs without updating the cache, where upstream updates it unlocked
and dies releasing the lock, as its real removelock did end to end). A HAND case in a class must
differ in exactly the keys it lists; a fuzzed scenario may differ only in its classes' keys. The last
five classes also check pysanoid against itself: with a NUL cache or one cut short it must do what it
does with no cache (a cache it may not write, the lock being held, is left alone); with one dated in
the future, what it does with the same cache expired; with a listing cut inside its last line, what it
does with the listing ending at the line before; and with no lock file its listing must be a normal
run's, with the cache left as it was.

Run: python3 dev/diff_snapshot_enumeration.py hand
     python3 dev/diff_snapshot_enumeration.py fuzz <n> <seed>
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import os
import random
import shutil
import sys
from pathlib import Path

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

WORK = DEV_DIR / "work" / "snapshot_enumeration"
os.environ["PATH"] = f"{DEV_DIR / 'fake_zfs'}{os.pathsep}{os.environ['PATH']}"
NOW = 1_790_000_000

HEAD = PERL_JSON_HEAD + r'''
BEGIN { *CORE::GLOBAL::time = sub () { __NOW__ + 0 } }
my %args = ('verbose' => 1, 'debug' => ($ENV{DIFF_DEBUG} ? 1 : 0));
my $zfs = 'zfs';
my $workdir = shift @ARGV;
my $cache = "$workdir/cache/snapshots.txt";
my (%config, %snaps, %snapsbytype, %snapsbypath);
my @params = (\%config, \%snaps, \%snapsbytype, \%snapsbypath);
sub checklock  { return (defined $ENV{LOCK_FREE} && $ENV{LOCK_FREE} eq '0') ? 0 : 1 }
sub writelock  { 1 }
# RUN_UNUSABLE: no lock file could be made, so writelock's unchecked open wrote nothing and checklock finds none
sub removelock { die "ERROR: No valid lockfile found - Did a rogue process or user update or delete it?\n" if $ENV{RUN_UNUSABLE}; 1 }
'''.replace("__NOW__", str(NOW))

TAIL = f'''
{READ_SCENARIO_PERL}
%config = %{{ $__scn->{{config}} }};
my $cacheTTL = $__scn->{{ttl}};
my $forced = $__scn->{{force}} ? 1 : 0;
my @params = (\\%config, \\%snaps, {{}}, {{}});
my $died = '';
eval {{ %snaps = getsnaps(\\%config, $cacheTTL, $forced); 1 }} or do {{ ($died = $@) =~ s/\\s+$//; }};
%snapsbytype = $died ? () : getsnapsbytype(\\%config, \\%snaps);
%snapsbypath = $died ? () : getsnapsbypath(\\%config, \\%snaps);
# JSON has no Inf, and Perl's own stringification keeps only 15 digits
sub __num {{ my $v = shift; return (abs($v) < 1e15 && $v == int($v)) ? sprintf("%d", $v) : sprintf("%.17g", $v); }}
for my $types (values %snapsbytype) {{ $_->{{newest}} = __num($_->{{newest}}) for values %$types; }}
for my $entry (values %snapsbypath) {{ $entry->{{newest}} = __num($entry->{{newest}}) if defined $entry->{{newest}}; }}
my %ctimes = map {{ my $fs = $_; ($fs => {{ map {{ ($_ => $snaps{{$fs}}{{$_}}{{ctime}}) }} keys %{{ $snaps{{$fs}} }} }}) }} keys %snaps;
my %__result = ( died => $died, snapsbytype => \\%snapsbytype, snapsbypath => \\%snapsbypath,
                 active => get_active_datasets(@params), ctimes => \\%ctimes );
{WRITE_RESULT_PERL}
'''

SCRIPT = build_harness(HEAD, extract_subs("get_active_datasets", "getsnaps", "getsnapsbytype", "getsnapsbypath"), TAIL)

KNOWN = {
    "killed": ("a zfs get killed by a signal: pysanoid retries without dataset names",
               {"zfs_calls", "stdout", "cache_bytes", "ctimes", "snapsbytype", "snapsbypath"}),
    "unreadable": ("a fresh cache that can't be read: pysanoid reads zfs, upstream reads no snapshots",
                   {"zfs_calls", "stdout", "cache_bytes", "ctimes", "snapsbytype", "snapsbypath"}),
    "read_only": ("a cache directory that can't be written: upstream dies, pysanoid carries on",
                  {"died", "ctimes", "snapsbytype", "snapsbypath"}),
    "cache_is_dir": ("snapshots.txt is a directory: upstream dies at the rename, pysanoid carries on",
                     {"died", "ctimes", "snapsbytype", "snapsbypath"}),
    "lock_held": ("the lock is held and no cache can be read: pysanoid reads zfs, upstream reads no snapshots",
                  {"zfs_calls", "stdout", "ctimes", "snapsbytype", "snapsbypath"}),  # stdout: the debug and retry lines
    "crafted": (("a snapshot name holding a type and 'creation': upstream reads a snapshot of that type made at the "
                 "time its digits give, pysanoid ignores it"), {"ctimes", "snapsbytype", "snapsbypath"}),
    "nul": ("a cache holding a NUL byte: upstream reads the damaged lines, pysanoid reads zfs as with no cache",
            {"zfs_calls", "stdout", "cache_bytes", "ctimes", "snapsbytype", "snapsbypath"}),
    "run_unusable": (("no lock file can be made: upstream updates the cache unlocked and dies releasing the lock, "
                      "pysanoid reads zfs and leaves the cache"), {"died", "stdout", "cache_bytes", "ctimes", "snapsbytype",
                                                                  "snapsbypath"}),
    "cut": ("a cache cut short inside its last line: upstream reads it, pysanoid reads zfs as with no cache",
            {"zfs_calls", "stdout", "cache_bytes", "ctimes", "snapsbytype", "snapsbypath"}),
    "future": ("a cache dated in the future: upstream uses it, pysanoid reads zfs as with an expired one",
               {"zfs_calls", "stdout", "cache_bytes", "ctimes", "snapsbytype", "snapsbypath"}),
    "listing_cut": ("a retried zfs get killed inside its last line: upstream reads that line, pysanoid leaves it out",
                    {"cache_bytes", "ctimes", "snapsbytype", "snapsbypath"}),
}


def _cut_short(data: bytes) -> bool:
    return bool(data) and not data.endswith(b"\n")


def _retry_listing(scn: dict, cut: int | None = None) -> str:
    """What a `zfs get` naming no dataset prints: every snapshot line, less the scenario's retry_cut."""
    whole = "".join(f"{snap['line']}\n" for snap in scn["zfs_snaps"])
    return whole[:max(0, len(whole) - (scn.get("retry_cut", 0) if cut is None else cut))]


def _listing_cut(scn: dict) -> bool:
    return bool(scn.get("retry_cut")) and not scn.get("fail_all") and _cut_short(_retry_listing(scn).encode())


def classes(scn: dict) -> list[str]:
    cache = scn["cache"] or {}
    # with no dataset to name, the first `zfs get` is the one retry_cut kills
    first_killed = scn.get("get_killed") or (_listing_cut(scn) and not p.get_active_dataset_paths(scn["config"]))
    flags = {"killed": first_killed, "unreadable": cache.get("unreadable"),
             "read_only": scn.get("read_only_cache_dir"), "cache_is_dir": scn.get("cache_is_dir"),
             "lock_held": scn.get("lock_held"), "crafted": scn.get("crafted"), "nul": b"\0" in cache.get("bytes", b""),
             "run_unusable": scn.get("run_unusable"), "cut": _cut_short(cache.get("bytes", b"")),
             "future": cache.get("age", 0) < 0, "listing_cut": _listing_cut(scn)}
    return [k for k, on in flags.items() if on]


def _unlock(wd: Path) -> None:
    """Undo a scenario's permissions, so the files can be read and removed."""
    for d in (wd, wd / "cache"):
        if d.is_dir():
            d.chmod(0o755)
    f = wd / "cache" / "snapshots.txt"
    if f.is_file():
        f.chmod(0o644)


def _setup(wd: Path, scn: dict) -> None:
    if wd.exists():
        _unlock(wd)
    shutil.rmtree(wd, ignore_errors=True)
    (wd / "cache").mkdir(parents=True)
    if scn.get("run_unusable"):
        (wd / "run").write_text("")  # a regular file where the run directory should be
    else:
        (wd / "run").mkdir()
    f = wd / "cache" / "snapshots.txt"
    if scn.get("cache_is_dir"):
        f.mkdir()
    elif scn["cache"] is not None:
        f.write_bytes(scn["cache"]["bytes"])
        mtime = NOW - scn["cache"]["age"] + scn["cache"].get("frac", 0)
        os.utime(f, (mtime, mtime))
        if scn["cache"].get("unreadable"):
            f.chmod(0)
    (wd / "zfs_state.json").write_text(json.dumps({"datasets": scn["tree"], "snapshots": scn["zfs_snaps"],
                                                    "fail_all": scn.get("fail_all", False),
                                                    "get_killed": scn.get("get_killed", False),
                                                    "retry_cut": scn.get("retry_cut", 0)}))
    (wd / "zfs.log").write_text("")
    if scn.get("read_only_cache_dir"):
        (wd / "cache").chmod(0o555)


def run_perl(wd: Path, scn: dict) -> dict:
    wd = wd.resolve()  # see diff_prune.py's run_perl for why: a relative wd double-resolves inside the Perl child
    _setup(wd, scn)
    env = {"FAKE_ZFS_LOG": str(wd / "zfs.log"), "FAKE_ZFS_STATE": str(wd / "zfs_state.json"),
           "DIFF_DEBUG": "1" if scn.get("debug") else "", "LOCK_FREE": "0" if scn.get("lock_held") else "1",
           "RUN_UNUSABLE": "1" if scn.get("run_unusable") else ""}
    stdin_obj = {"config": canon_config_for_perl(scn["config"]), "ttl": scn["ttl"], "force": scn["force"]}
    result = run_harness(SCRIPT, stdin_obj=stdin_obj, argv=[str(wd)], env=env)
    if result.data is None:
        raise RuntimeError(f"perl harness produced no result:\n{result.stderr}")
    return _collect(wd, {**result.data, "stdout": result.stdout})


def _collect(wd: Path, extra: dict) -> dict:
    _unlock(wd)
    f = wd / "cache" / "snapshots.txt"
    return {
        **extra,
        "zfs_calls": [json.loads(x) for x in (wd / "zfs.log").read_text().split("\n") if x],
        "cache_bytes": f.read_bytes() if f.is_file() else ("<directory>" if f.is_dir() else None),
    }


def run_python(wd: Path, scn: dict) -> dict:
    _setup(wd, scn)
    os.environ.update(FAKE_ZFS_LOG=str(wd / "zfs.log"), FAKE_ZFS_STATE=str(wd / "zfs_state.json"))
    p.time.time = lambda: float(NOW) + scn.get("py_frac", 0)  # the Perl side's time() is the whole second
    config = {sec: cfg for sec, cfg in scn["config"].items()}
    out, err = io.StringIO(), io.StringIO()
    died = ""
    snaps: dict = {}
    snapsbytype: dict = {}
    snapsbypath: dict = {}
    held = None
    if scn.get("lock_held"):
        held = open(wd / "run" / "sanoid_cacheupdate.lock", "w")  # noqa: SIM115 - held across the call, closed in `finally`
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            snaps = p.get_snapshots(config, wd / "cache", wd / "run", scn["ttl"], scn["force"], verbose=True,
                                    debug=scn.get("debug", False), problems=[])
            snapsbytype = p.snapshots_by_type(snaps, config)  # as main() calls them: the clock is read inside
            snapsbypath = p.snapshots_by_path(snaps, config)
    except Exception as e:  # noqa: BLE001 - harness: any failure counts as "died"
        died = f"{type(e).__name__}: {e}"
    finally:
        if held:
            held.close()
    active = p.get_active_dataset_paths(config)
    ctimes = {fs: {name: s.ctime for name, s in by_name.items()} for fs, by_name in snaps.items()}
    return _collect(wd, {"died": died, "snapsbytype": _as_perl_json(snapsbytype), "snapsbypath": _as_perl_json(snapsbypath),
                          "active": " ".join(active), "stdout": out.getvalue(), "ctimes": _as_perl_json(ctimes)})


def _as_perl_json(obj):
    """What the Perl side's encode_json makes of the same bytes: each byte one Latin-1 character."""
    if isinstance(obj, str):
        return obj.encode("utf-8", "surrogateescape").decode("latin-1")
    if isinstance(obj, dict):
        return {_as_perl_json(k): _as_perl_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_as_perl_json(x) for x in obj]
    return obj


def _number(value: object) -> object:
    """A creation time or an age as a number. The Perl side gives creation times as the captured digits, which Perl
    reads exactly up to 2**64 - 1 and as a float beyond, and ages as text (JSON has no Inf)."""
    if not isinstance(value, str):
        return value
    digits = value.lstrip("0")
    if digits.isdigit() and len(digits) <= 20 and int(digits) <= 2**64 - 1:
        return int(digits)
    return float(value) if value else 0


def _same_second_groups(names: list[str], ctimes: dict) -> list[list[str]]:
    """The names oldest first, with those made in the same second as one sorted group."""
    groups: list[list[str]] = []
    for i, name in enumerate(names):
        if i and ctimes[name] == ctimes[names[i - 1]]:
            groups[-1] = sorted([*groups[-1], name])
        else:
            groups.append([name])
    return groups


RUNNING = "INFO: running: zfs get -Hrpt snapshot creation "


def _once(names: list[str]) -> list[str]:
    return list(dict.fromkeys(names))


def _running_line_once(line: str) -> str:
    if not line.startswith(RUNNING):
        return line
    return RUNNING + " ".join(_once(line[len(RUNNING):].rstrip("\n").split(" "))) + "\n"


def canon(r: dict) -> dict:
    r = dict(r)
    # DEV-021: pysanoid asks zfs for a path once, where upstream names it again for each section on it
    r["active"] = " ".join(_once(r["active"].split(" ")))
    r["zfs_calls"] = [c[:4] + _once(c[4:]) if c[:1] == ["get"] else c for c in r["zfs_calls"]]
    r["stdout"] = "".join(map(_running_line_once, r["stdout"].splitlines(keepends=True)))
    r["zfs_calls"] = sorted(json.dumps(c) for c in r["zfs_calls"])
    r["died"] = bool(r["died"])
    r["stdout"] = r["stdout"].replace("another pysanoid process", "another sanoid process")  # the program's own name
    # upstream's tables leave an empty entry in %snaps for every configured path without snapshots (`keys %{ $snaps{$path}
    # }` creates it); nothing reads it
    r["ctimes"] = {fs: {name: _number(c) for name, c in by_name.items()} for fs, by_name in r.get("ctimes", {}).items()
                   if by_name}
    for path, byt in r.get("snapsbytype", {}).items():
        for entry in byt.values():
            names = entry["sorted"].split("|") if isinstance(entry["sorted"], str) else entry["sorted"]
            entry["sorted"] = _same_second_groups(names, r["ctimes"][path])
            newest = entry["sorted"][-1]
            entry["newestname"] = newest if entry["newestname"] in newest else f"not the newest: {entry['newestname']}"
            entry["newest"] = _number(entry["newest"])
    for byp in r.get("snapsbypath", {}).values():
        byp.pop("sorted", None)  # read only by the Nagios monitor, which pysanoid leaves out
        if "newest" in byp:
            byp["newest"] = _number(byp["newest"])
    # snapshots_by_type sets result[path] = {} for a configured path with zero matching snapshots;
    # getsnapsbytype never creates $snapsbytype{$path} at all in that case -- a real structural
    # difference (found by this harness, not assumed), but confirmed inert: every consumer in
    # pysanoid.py (plan_snapshots, prune_snapshots, print_debug_dump) reads this via
    # `snaps_by_type.get(path, {})`, so "key present with an empty dict" and "key absent" are
    # indistinguishable to every actual caller. Canonicalized here rather than "fixed" in
    # pysanoid.py, since removing the key would cost a line of code to prevent a difference that
    # is not observable anywhere it's read.
    r["snapsbytype"] = {p: t for p, t in r.get("snapsbytype", {}).items() if t}
    return r


def self_checks(scn: dict, b: dict) -> list[str]:
    """pysanoid against itself, for the classes where its answer isn't upstream's (see the module docstring)."""
    bad: list[str] = []
    cache = scn["cache"] or {}
    data = cache.get("bytes", b"")
    if b"\0" in data or _cut_short(data):
        ref = canon(run_python(WORK / "python_ref", {**scn, "cache": None}))
        for k, value in b.items():
            # where a run with no cache writes none (the lock held, the directory read-only), the damaged file stays
            left_alone = k == "cache_bytes" and value == data and ref[k] is None
            if value != ref[k] and not left_alone:
                bad.append(f"no-cache self-check: {k}")
    if cache.get("age", 0) < 0:
        ref = canon(run_python(WORK / "python_ref", {**scn, "cache": {**cache, "age": scn["ttl"] + 1, "frac": 0}}))
        bad += [f"future self-check: {k}" for k, value in b.items() if value != ref[k]]
    if _listing_cut(scn):
        listing = _retry_listing(scn)
        whole_lines = len(_retry_listing(scn, 0)) - (listing.rfind("\n") + 1)  # the cut that ends it at the line before
        ref = canon(run_python(WORK / "python_ref", {**scn, "retry_cut": whole_lines}))
        bad += [f"listing-cut self-check: {k}" for k, value in b.items() if value != ref[k]]
    if scn.get("run_unusable"):
        ref = canon(run_python(WORK / "python_ref", {**scn, "run_unusable": False}))
        bad += [f"run_unusable self-check: {k}" for k in ("died", "zfs_calls", "ctimes", "snapsbytype", "snapsbypath")
                if b[k] != ref[k]]
        before = cache["bytes"] if scn["cache"] is not None else ("<directory>" if scn.get("cache_is_dir") else None)
        if b["cache_bytes"] != before:
            bad.append("run_unusable self-check: cache_bytes")
    return bad


def run_both(scn: dict) -> tuple[dict, dict, list[str]]:
    a, b = canon(run_perl(WORK / "perl", scn)), canon(run_python(WORK / "python", scn))
    diffs = [k for k in a if a[k] != b[k]] + self_checks(scn, b)
    return a, b, diffs


def L(ds: str, name: str, ct: int | str) -> dict:
    return {"ds": ds, "line": f"{ds}@{name}\tcreation\t{ct}\t-"}


def _cache_lines(*lines: tuple[str, str, int | str]) -> bytes:
    return b"".join(f"{L(ds, name, ct)['line']}\n".encode() for ds, name, ct in lines)


def _sec(path: str, **over) -> dict:
    # Python bool, matching what load_config actually hands these functions in the real pipeline
    # (_normalize_toggles converts "yes"/"1"/etc to True/False before anything downstream sees it) --
    # NOT the raw ini string. Upstream's own init() does the analogous normalization to Perl 1/0
    # before storing into %config, so this must diverge per side, not share one raw representation;
    # see canon_config_for_perl(). A raw string "0" is non-empty and therefore TRUTHY in Python but
    # FALSY in Perl -- feeding the same raw string to both sides would silently test the wrong thing.
    return {"path": path, "autosnap": True, "autoprune": True, "process_children_only": False, **over}


def canon_config_for_perl(config: dict) -> dict:
    """Python bool -> Perl-truthy 1/0 for JSON, matching what upstream's init() stores after its own
    istrue/isfalse normalization (not the raw config string either side would have started from)."""
    return {sec: {k: (1 if v else 0) if isinstance(v, bool) else v for k, v in cfg.items()}
            for sec, cfg in config.items()}


ABC_TREE = ["tank", "tank/a", "tank/b", "tank/c"]
ABC_SNAPS = [L(ds, "autosnap_2026-09-01_00:00:01_daily", 1_789_990_000) for ds in ("tank/a", "tank/b", "tank/c")]
ABC = {"a": _sec("tank/a"), "b": _sec("tank/b"), "c": _sec("tank/c")}
ABC_GONE = {**ABC, "gone": _sec("tank/gone")}  # a dataset that isn't there: the listing with names fails, and is retried
CACHED = {"bytes": b"tank/a@autosnap_old_daily\tcreation\t1\t-\n", "age": 5}
ARABIC_12 = b"\xd9\xa1\xd9\xa2"  # ARABIC-INDIC DIGIT ONE, TWO, as UTF-8
TILDE_N = b"\xc3\xb1"  # LATIN SMALL LETTER N WITH TILDE, as UTF-8
TIED = _cache_lines(("tank/a", "autosnap_A_daily", 1_789_000_000), ("tank/a", "autosnap_B_daily", 1_789_000_000),
                    ("tank/a", "autosnap_C_daily", 1_789_500_000), ("tank/a", "autosnap_D_daily", 1_789_500_000))
MIXED = _cache_lines(*(("tank/a", name, ct) for name, ct in (
    ("autosnap_1_hourly", 1_789_990_000), ("autosnap_1_frequently", 1_789_990_001), ("autosnap_1_weekly", 1_789_000_000),
    ("autosnap_1_monthly", 1_780_000_000), ("autosnap_1_yearly", 1_750_000_000), ("autosnap_1_reallly", 1_789_990_002),
    ("autosnap1daily", 1_789_990_003), ("autosnap_2_daily", ""))))


def _damaged(digits: int) -> bytes:
    return _cache_lines(("tank/a", "autosnap_x_daily", "9" * digits), ("tank/a", "autosnap_y_daily", 5))


# the file's last 8 bytes lost: the newest snapshot's creation time loses digits, and reads as decades old
CUT = b"".join(f"{s['line']}\n".encode() for s in ABC_SNAPS)[:-8]
CUT_NEWLINE = b"".join(f"{s['line']}\n".encode() for s in ABC_SNAPS)[:-1]

# zeroes from just after a snapshot's name into a later line, as a crash can leave them
NUL_DAMAGED = (b"tank/a@autosnap_x_daily" + b"\0" * 24 + b"_daily\tcreation\t1000\t-\n"
               + b"tank/b@autosnap_y_daily\tcreation\t1789990000\t-\n")

HAND = {
    "empty config, no cache": {"config": {}, "cache": None, "ttl": 1200, "force": False, "tree": [], "zfs_snaps": []},
    "fresh cache is used, zfs untouched": {
        "config": {"a": _sec("tank")}, "cache": {"bytes": b"tank@autosnap_x_daily\tcreation\t1\t-\n", "age": 5},
        "ttl": 1200, "force": False, "tree": ["tank"], "zfs_snaps": []},
    "expired cache triggers a live query": {
        "config": {"a": _sec("tank")}, "cache": {"bytes": b"stale\n", "age": 5000},
        "ttl": 1200, "force": False, "tree": ["tank"], "zfs_snaps": [L("tank", "autosnap_x_daily", 2)]},
    "force-update ignores a fresh cache": {
        "config": {"a": _sec("tank")}, "cache": {"bytes": b"tank@autosnap_x_daily\tcreation\t1\t-\n", "age": 5},
        "ttl": 1200, "force": True, "tree": ["tank"], "zfs_snaps": [L("tank", "autosnap_y_daily", 2)]},
    "a dataset-scoped query failing falls back to querying everything (the retry line)": {
        "config": {"a": _sec("tank"), "b": _sec("gone")}, "cache": None, "ttl": 1200, "force": False,
        "tree": ["tank"], "zfs_snaps": [L("tank", "autosnap_x_daily", 2)]},
    "zfs fails outright: empty, fresh cache": {
        "config": {"a": _sec("tank")}, "cache": None, "ttl": 1200, "force": False, "tree": ["tank"], "zfs_snaps": [], "fail_all": True},
    "both-off and children-only sections are not queried": {
        "config": {"a": _sec("tank"), "off": _sec("skip", autosnap=False, autoprune=False),
               "kids": _sec("kidsonly", process_children_only=True)},
        "cache": None, "ttl": 1200, "force": False, "tree": ["tank", "skip", "kidsonly"], "zfs_snaps": []},
    "two sections sort case-insensitively": {
        "config": {"a": _sec("Zed"), "b": _sec("apple")}, "cache": None, "ttl": 1200, "force": False,
        "tree": ["Zed", "apple"], "zfs_snaps": []},
    "--verbose --debug prints the zfs command": {
        "config": ABC, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS, "debug": True},
    "a cache 1200.6 s old is 1200 s old: fresh at a TTL of 1200": {
        "config": ABC, "cache": {**CACHED, "age": 1200, "frac": 0.3}, "py_frac": 0.9, "ttl": 1200, "force": False,
        "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS},
    "a non-ASCII digit in a creation time is no time": {
        "config": ABC, "cache": {"bytes": b"tank/a@autosnap_x_daily\tcreation\t" + ARABIC_12 + b"\t-\n", "age": 5},
        "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": []},
    "a non-ASCII letter before 'ly' is no type": {
        "config": ABC, "cache": {"bytes": b"tank/a@autosnap_x_" + TILDE_N + b"ly\tcreation\t5\t-\n", "age": 5},
        "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": []},
    "the tables' ages are whole seconds, the clock 0.9 s into the second": {
        "config": ABC, "cache": {"bytes": b"".join(f"{s['line']}\n".encode() for s in ABC_SNAPS), "age": 5}, "py_frac": 0.9,
        "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": []},
    "snapshots made in the same second, the newest two included": {
        "config": ABC, "cache": {"bytes": TIED, "age": 5}, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": []},
    "every type, a name with no type, an empty creation time": {
        "config": ABC, "cache": {"bytes": MIXED, "age": 5}, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": []},
    "two sections on one path": {
        "config": {**ABC, "alias": _sec("tank/a")}, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS, "debug": True},
    "a damaged cache line: a 400-digit creation time": {
        "config": ABC, "cache": {"bytes": _damaged(400), "age": 5}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": []},
    "a damaged cache line: a 5000-digit creation time": {
        "config": ABC, "cache": {"bytes": _damaged(5000), "age": 5}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": []},
    "KNOWN: a snapshot name holding a type and 'creation'": {
        "config": ABC, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": [*ABC_SNAPS, L("tank/a", "autosnap_dailycreation9999999999_x", 1_789_990_100)], "crafted": True,
        "known_diffs": ["ctimes", "snapsbypath", "snapsbytype"]},
    "KNOWN: a zfs get killed by a signal mid-listing": {
        "config": ABC, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS,
        "get_killed": True, "known_diffs": ["cache_bytes", "ctimes", "snapsbypath", "snapsbytype", "stdout", "zfs_calls"]},
    "KNOWN: a fresh cache that can't be read": {
        "config": ABC, "cache": {**CACHED, "unreadable": True}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS,
        "known_diffs": ["cache_bytes", "ctimes", "snapsbypath", "snapsbytype", "stdout", "zfs_calls"]},
    "KNOWN: a cache directory that can't be written": {
        "config": ABC, "cache": {**CACHED, "age": 5000}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS, "read_only_cache_dir": True,
        "known_diffs": ["ctimes", "died", "snapsbypath", "snapsbytype"]},
    "KNOWN: snapshots.txt is a directory": {
        "config": ABC, "cache": None, "cache_is_dir": True, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS, "known_diffs": ["ctimes", "died", "snapsbypath", "snapsbytype"]},
    "KNOWN: the lock is held and there is no cache": {
        "config": ABC, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS,
        "lock_held": True, "known_diffs": ["ctimes", "snapsbypath", "snapsbytype", "zfs_calls"]},
    "KNOWN: a fresh cache holding a NUL byte": {
        "config": ABC, "cache": {"bytes": NUL_DAMAGED, "age": 5}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS, "known_diffs": ["cache_bytes", "ctimes", "snapsbypath", "snapsbytype", "stdout", "zfs_calls"]},
    "KNOWN: a fresh cache holding a NUL byte, the lock held": {
        "config": ABC, "cache": {"bytes": NUL_DAMAGED, "age": 5}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS, "lock_held": True, "known_diffs": ["ctimes", "snapsbypath", "snapsbytype", "stdout", "zfs_calls"]},
    "KNOWN: no lock file can be made, the cache expired": {
        "config": ABC, "cache": {**CACHED, "age": 5000}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS, "run_unusable": True,
        "known_diffs": ["cache_bytes", "ctimes", "died", "snapsbypath", "snapsbytype", "stdout"]},
    "no lock file can be made, the cache fresh: no lock is needed": {
        "config": ABC, "cache": CACHED, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS,
        "run_unusable": True},
    "KNOWN: a cache dated a second ahead": {
        "config": ABC, "cache": {**CACHED, "age": -1}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS, "known_diffs": ["cache_bytes", "ctimes", "snapsbypath", "snapsbytype", "stdout", "zfs_calls"]},
    "KNOWN: a cache dated a year ahead, the lock held: read all the same, and said so": {
        "config": ABC, "cache": {**CACHED, "age": -365 * 86400}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS, "lock_held": True, "known_diffs": ["stdout"]},
    "a cache dated ahead + force-update: no difference": {
        "config": ABC, "cache": {**CACHED, "age": -1}, "ttl": 1200, "force": True, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS},
    "KNOWN: a cache cut short inside a creation time": {
        "config": ABC, "cache": {"bytes": CUT, "age": 5}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS, "known_diffs": ["cache_bytes", "ctimes", "snapsbypath", "snapsbytype", "stdout", "zfs_calls"]},
    "KNOWN: a cache that lost only its last newline: the same snapshots, read from zfs": {
        "config": ABC, "cache": {"bytes": CUT_NEWLINE, "age": 5}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS, "known_diffs": ["cache_bytes", "stdout", "zfs_calls"]},
    "KNOWN: a cache cut short, the lock held": {
        "config": ABC, "cache": {"bytes": CUT, "age": 5}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS, "lock_held": True, "known_diffs": ["ctimes", "snapsbypath", "snapsbytype", "stdout", "zfs_calls"]},
    "a cache cut short that has expired: no difference": {
        "config": ABC, "cache": {"bytes": CUT, "age": 5000}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS},
    "an empty cache has no last line to lose": {
        "config": ABC, "cache": {"bytes": b"", "age": 5}, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS},
    "KNOWN: the retry killed inside a creation time: that line is left out": {
        "config": ABC_GONE, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS,
        "retry_cut": 8, "known_diffs": ["cache_bytes", "ctimes", "snapsbypath", "snapsbytype"]},
    "KNOWN: the retry killed before its last newline: a whole line, left out all the same": {
        "config": ABC_GONE, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS,
        "retry_cut": 1, "known_diffs": ["cache_bytes", "ctimes", "snapsbypath", "snapsbytype"]},
    "KNOWN: the retry killed right after 'creation': upstream reads a snapshot made in 1970": {
        "config": ABC_GONE, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS,
        "retry_cut": 14, "known_diffs": ["cache_bytes", "ctimes", "snapsbypath", "snapsbytype"]},
    "KNOWN: the retry killed inside a snapshot's name: only the cache differs": {
        "config": ABC_GONE, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS,
        "retry_cut": 30, "known_diffs": ["cache_bytes"]},
    "the retry killed at the end of a line: no difference": {
        "config": ABC_GONE, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS,
        "retry_cut": len(ABC_SNAPS[-1]["line"]) + 1},
    "KNOWN: the retry killed inside its only line": {
        "config": ABC_GONE, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS[:1],
        "retry_cut": 8, "known_diffs": ["cache_bytes", "ctimes", "snapsbypath", "snapsbytype"]},
    "KNOWN: the retry killed inside a line, an expired cache replaced, --debug": {
        "config": ABC_GONE, "cache": {**CACHED, "age": 5000}, "ttl": 1200, "force": False, "tree": ABC_TREE,
        "zfs_snaps": ABC_SNAPS, "retry_cut": 8, "debug": True,
        "known_diffs": ["cache_bytes", "ctimes", "snapsbypath", "snapsbytype"]},
    "KNOWN: a zfs get killed by a signal, and the retry killed inside a line too": {
        "config": ABC, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS,
        "get_killed": True, "retry_cut": 8,
        "known_diffs": ["cache_bytes", "ctimes", "snapsbypath", "snapsbytype", "stdout", "zfs_calls"]},
    "KNOWN: the retry killed inside a line, the lock held and no cache": {
        "config": ABC_GONE, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS,
        "lock_held": True, "retry_cut": 8, "known_diffs": ["ctimes", "snapsbypath", "snapsbytype", "stdout", "zfs_calls"]},
    "a retry that would be killed, a fresh cache: zfs isn't asked": {
        "config": ABC_GONE, "cache": CACHED, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS,
        "retry_cut": 8},
    "a retry that would be killed, the first listing works: no retry": {
        "config": ABC, "cache": None, "ttl": 1200, "force": False, "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS, "retry_cut": 8},
    "KNOWN: no dataset to name, so the first zfs get is the one killed inside a line": {
        "config": {"kids": _sec("tank/a", process_children_only=True)}, "cache": None, "ttl": 1200, "force": False,
        "tree": ABC_TREE, "zfs_snaps": ABC_SNAPS[:1], "retry_cut": 8,
        "known_diffs": ["cache_bytes", "ctimes", "snapsbypath", "snapsbytype", "stdout", "zfs_calls"]},
}


TYPES = ["daily", "hourly", "weekly", "monthly", "yearly", "frequently", "reallly", None]  # None: a name with no type


def _snap(r: random.Random, tree: list[str], times: list[int | str]) -> dict:
    t = r.choice(TYPES)
    return L(r.choice(tree), f"autosnap{r.randint(0, 3)}daily" if t is None else f"autosnap_{r.randint(0, 3)}_{t}",
             r.choice(times))


def gen(r: random.Random) -> dict:
    tree = [f"tank/{c}" for c in "abcde"[:r.randint(1, 5)]]
    times: list[int | str] = [r.randint(1, 10**9) for _ in range(r.randint(1, 4))]  # few, so same-second pairs are common
    if r.random() < 0.1:
        times.append("")
    zfs_snaps = [_snap(r, tree, times) for _ in range(r.randint(0, 8))]
    crafted = r.random() < 0.08
    if crafted:
        zfs_snaps.append(L(r.choice(tree), f"autosnap_{r.choice(TYPES[:6])}creation{r.randint(1, 10**10)}_x", r.choice(times)))
    config = {}
    for i, ds in enumerate(r.sample(tree, r.randint(1, len(tree)))):
        config[f"s{i}"] = _sec(ds, autosnap=r.choice([True, False]), autoprune=r.choice([True, False]),
                               process_children_only=r.choice([True, False, False, False]))
    if r.random() < 0.2:
        config["alias"] = _sec(r.choice(tree))  # a second section on a path
    if r.random() < 0.1:
        config["templatefoo"] = _sec("templatefoo")  # named like a template: in the tables, never queried
    if r.random() < 0.2:
        config["gone"] = _sec("tank/gone")  # makes the dataset-scoped query fail: the retry line
    cache = None
    if r.random() < 0.7:
        lines = [f"{_snap(r, tree, times)['line']}\n".encode() for _ in range(r.randint(0, 5))]
        if r.random() < 0.15:
            lines.append(r.choice([
                f"{r.choice(tree)}@autosnap_u_daily\tcreation\t".encode() + ARABIC_12 + b"\t-\n",
                f"{r.choice(tree)}@autosnap_u_".encode() + TILDE_N + b"ly\tcreation\t5\t-\n",
                f"{r.choice(tree)}@autosnap_u_daily\tcreation\t{'9' * r.choice([25, 400, 5000])}\t-\n".encode(),  # damaged
            ]))
        if crafted and r.random() < 0.5:
            lines.append(f"{zfs_snaps[-1]['line']}\n".encode())
        data = b"".join(lines)
        if data and r.random() < 0.06:  # zeroes where a crash left them
            at = r.randrange(len(data))
            data = data[:at] + b"\0" * r.randint(1, 30) + data[at + r.randint(0, 20):]
        cache = {"bytes": data, "age": r.choice([5, 900, 1199, 1200, 1201, 5000]),
                 "frac": r.choice([0, 0, 0.3, 0.7]), "unreadable": r.random() < 0.05}
    lock_held = r.random() < 0.05
    scn = {"config": config, "cache": cache, "ttl": r.choice([1200, 60, 18000]), "force": r.random() < 0.15,
           "tree": tree, "zfs_snaps": zfs_snaps, "fail_all": r.random() < 0.05, "debug": r.random() < 0.3,
           "py_frac": r.choice([0, 0, 0.4, 0.9]), "get_killed": r.random() < 0.05, "lock_held": lock_held,
           "read_only_cache_dir": r.random() < 0.05, "cache_is_dir": cache is None and r.random() < 0.05,
           "crafted": crafted, "run_unusable": not lock_held and r.random() < 0.06}
    cuts = random.Random(json.dumps([tree, zfs_snaps, sorted(config)]))  # a stream of its own, as `late` below
    if zfs_snaps and cuts.random() < 0.08:
        scn["retry_cut"] = cuts.randint(1, 16)  # the retry killed that many bytes short
        if cuts.random() < 0.7:
            config.setdefault("gone", _sec("tank/gone"))  # so that the listing with names fails, and both retry
    if cache is not None:
        late = random.Random(cache["bytes"])  # a stream of its own: a seed still gives the scenarios it gave before these
        if late.random() < 0.06:
            cache["age"] = late.choice([-1, -4000])  # written while the clock ran ahead
        if cache["bytes"] and late.random() < 0.08:
            cache["bytes"] = cache["bytes"][:len(cache["bytes"]) - late.randint(1, min(12, len(cache["bytes"])))]  # lost its end
    return scn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=500)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()

    tally = {"match": 0, "known": 0, "DIFF": 0}
    known_tally: dict[str, int] = {}
    if args.mode == "hand":
        for name, scn in HAND.items():
            _, _, diffs = run_both(scn)
            expected = scn.get("known_diffs", [])
            ok = sorted(diffs) == expected
            tally[("known" if expected else "match") if ok else "DIFF"] += 1
            label = ("KNOWN" if expected else "MATCH") if ok else "DIFF "
            print(f"[{label}] {name}" + (f"  diffs={diffs}" if diffs and not ok else ""))
    else:
        r = random.Random(args.seed)
        seen: dict[tuple, tuple] = {}
        for _ in range(args.n):
            scn = gen(r)
            a, b, diffs = run_both(scn)
            allowed = set().union(*(KNOWN[k][1] for k in classes(scn)))
            if not diffs:
                tally["match"] += 1
            elif set(diffs) <= allowed:
                tally["known"] += 1
                for k in classes(scn):
                    known_tally[KNOWN[k][0]] = known_tally.get(KNOWN[k][0], 0) + 1
            else:
                tally["DIFF"] += 1
                seen.setdefault(tuple(diffs), (scn, a, b))
        for sig, (scn, a, b) in seen.items():
            print(f"\n--- differs on {sig}")
            print("    scenario:", json.dumps(scn, default=str)[:400])
            for k in sig:
                if k in a:
                    print(f"    {k}: perl={a[k]!r}\n         python={b[k]!r}")
                else:  # a self-check of pysanoid's own result
                    print(f"    {k}")
    print(tally, "known classes:", known_tally)
    for side in ("perl", "python", "python_ref"):
        _unlock(WORK / side)
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if tally["DIFF"] else 0)


if __name__ == "__main__":
    main()
