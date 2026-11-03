#!/usr/bin/env python3
"""Differential test: upstream's `init` (dataset-cache load at the top, rewrite at the bottom) and
`getchilddatasets` (Module 4), extracted verbatim, vs pysanoid's load_config + ZfsClient's dataset
cache -- against a shared fake `zfs`.

This is the highest-value target in this whole tool: the eager-vs-lazy dataset-cache load bug this
harness originally found (969 of 1,500 scenarios diverged) was live on every production run, since
the production config of the time had no recursive section. Keep this one running after any change to
load_config or ZfsClient.

Compared: whether it died, the merged config, child lookups made after loading, datasets.txt (bytes, and whether its
mtime was kept), every zfs call, stdout (INFO lines and the dataset cache's DEBUG lines, the child lookup's naming zfs
as upstream does, by its bare name found on PATH; the config-merge DEBUG trace is left to diff_config_merge.py, and
"another pysanoid process" reads as upstream's "another sanoid process"), and loading's warnings and zfs's complaints
on stderr, in order. Output is compared as UTF-8 read with replacement, as run_harness reads Perl's.

Known classes, from DEV-018 and DEV-019: a child lookup that can't start,
is killed or fails partway, where pysanoid stops and upstream carries on with what came back; and a datasets.txt
holding a NUL byte, which upstream uses and pysanoid treats as no cache (checked: pysanoid's result must equal its
result with no datasets.txt at all). From DEV-041: a run directory no lock file can be made in, where
upstream rewrites datasets.txt unlocked and dies releasing the lock, as its real removelock did end to end (checked:
pysanoid carries on, reports it once, and leaves datasets.txt as it was). From "Config loading": a section above the
recursive section it belongs under, which pysanoid refuses and upstream loads (checked against the rule as
dev/lib/configorder.py states it, word for word, with datasets.txt left as it was); and a section under a recursive
section whose lookup didn't list its dataset, which upstream builds from the defaults and pysanoid from that section's
settings (checked against the rule as dev/lib/configorder.py states it: each key the section doesn't set itself is its
parent's, every other section is upstream's, and nothing but the config and an autosnap warning differs). A
datasets.txt dated in the future is classed with the NUL one, and checked the same way: upstream uses it, pysanoid
must do what it does with none.

Run: python3 dev/diff_dataset_cache.py hand
     python3 dev/diff_dataset_cache.py fuzz <n> <seed>
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import os
import random
import re
import shutil
import sys
from pathlib import Path
from typing import Any

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.configorder import (
    first_misplaced,
    own_settings,
    recursion_above,
    refusal,
    starts_from_parent,
)
from lib.extract import extract_subs
from lib.perlrun import (
    PERL_JSON_HEAD,
    READ_SCENARIO_PERL,
    WRITE_RESULT_PERL,
    build_harness,
    cpan_env,
    run_harness,
)

import pysanoid as p

WORK = DEV_DIR / "work" / "dataset_cache"
FAKE_ZFS = DEV_DIR / "fake_zfs" / "zfs"
NO_ZFS = "/nonexistent/zfs"  # a lookup that can't start, as when the fork fails
NOW = 1_790_000_000
p.time.time = lambda: float(NOW)
TREE = ["pool2", "pool2/data", "pool2/data/one", "pool2/data/two",
        "tank", "tank/a", "tank/a/x", "tank/a/y", "tank/b", "tank/b/x"]

HEAD = PERL_JSON_HEAD + r'''
BEGIN { unshift @INC, $ENV{INI_LIB}, $ENV{STRINGY_LIB}; *CORE::GLOBAL::time = sub () { __NOW__ + 0 } }
use Config::IniFiles;
my $MINIMUM_DEFAULTS_VERSION = 2;
my $workdir = shift @ARGV;
my %args = ('debug' => 0);
my $cacheTTL = 1200;
my $zfs = 'zfs';
my $run_dir = "$workdir/run";
my $cachedatasetspath = "$workdir/cache/datasets.txt";
my @cachedatasets;
sub checklock  { return $ENV{LOCK_FREE} eq '1' ? 1 : 0 }
sub writelock  { 1 }
# RUN_UNUSABLE: no lock file could be made, so writelock's unchecked open wrote nothing and checklock finds none
sub removelock { die "ERROR: No valid lockfile found - Did a rogue process or user update or delete it?\n" if $ENV{RUN_UNUSABLE}; 1 }
'''.replace("__NOW__", str(NOW))

TAIL = f'''
{READ_SCENARIO_PERL}
$args{{'verbose'}} = $__scn->{{verbose}} ? 1 : 0;
$args{{'debug'}} = $__scn->{{debug}} ? 1 : 0;
$args{{'force-update'}} = $__scn->{{force_update}} ? 1 : 0;
$cacheTTL = $__scn->{{ttl}};
my %config;
my $died = '';
eval {{ %config = init("$workdir/conf/sanoid.conf", "$workdir/conf/sanoid.defaults.conf"); 1 }}
    or do {{ ($died = $@) =~ s/\\s+$//; }};
my %probes;
unless ($died) {{ foreach my $fs (@{{ $__scn->{{probe}} }}) {{ my @r = getchilddatasets($fs); $probes{{$fs}} = \\@r; }} }}
sub norm {{ my $v = shift; return '<undef>' unless defined $v; return ref($v) ? 'REF:' . ref($v) : "$v" }}
my %out;
foreach my $sec (keys %config) {{ foreach my $k (keys %{{ $config{{$sec}} }}) {{ $out{{$sec}}{{$k}} = norm($config{{$sec}}{{$k}}) }} }}
my %__result = ( died => ($died ? 1 : 0), died_msg => $died, config => \\%out, probes => \\%probes );
{WRITE_RESULT_PERL}
'''

SCRIPT = build_harness(HEAD, extract_subs("getchilddatasets", "init"), TAIL)
DEFAULTS = DEV_DIR.parent / "sanoid.defaults.conf"
MERGE_TRACE = ("DEBUG: initializing ", "DEBUG: overriding ", "DEBUG: recursively setting ", "DEBUG: ignoring ")
WARNING_RE = re.compile(r"^(?:duplicate key |ignored key |ignored autosnap configuration |cannot open )")


def _b2l(s: str) -> str:
    """A pysanoid str -> the same bytes read as latin-1, matching how the Perl side's byte strings arrive through JSON."""
    return s.encode("utf-8", "surrogateescape").decode("latin-1")


def _as_perl_output(s: str) -> str:
    """pysanoid's printed text as run_harness reads Perl's: the bytes, decoded as UTF-8 with replacement."""
    return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _setup(wd: Path, scn: dict) -> None:
    shutil.rmtree(wd, ignore_errors=True)
    for d in ("cache", "run", "conf", "bin"):
        (wd / d).mkdir(parents=True)
    if scn["lookup"] != "cannot_start":
        (wd / "bin" / "zfs").symlink_to(FAKE_ZFS)  # upstream's `zfs`, found on PATH
    if scn.get("run_unusable"):
        (wd / "run").rmdir()
        (wd / "run").write_text("")  # a regular file where the run directory should be
    shutil.copyfile(DEFAULTS, wd / "conf" / "sanoid.defaults.conf")
    (wd / "conf" / "sanoid.conf").write_bytes(scn["conf"])
    c = scn["cache"]
    if c is not None:
        f = wd / "cache" / "datasets.txt"
        f.write_bytes(c["bytes"])
        os.utime(f, (NOW - c["age"], NOW - c["age"]))
    lookup = scn["lookup"]
    state = {"datasets": scn["tree"], "list_killed": lookup == "killed", "list_fails_partway": lookup == "partway",
             "list_prints": scn["list_prints"]}
    (wd / "zfs_state.json").write_text(json.dumps(state))
    (wd / "zfs.log").write_text("")


def _zfs_env(wd: Path, scn: dict) -> dict[str, str]:
    """pysanoid runs ZFS_BIN, as it runs the path it found at startup; upstream runs `zfs` from PATH (_perl_path)."""
    return {"ZFS_BIN": NO_ZFS if scn["lookup"] == "cannot_start" else str(FAKE_ZFS),
            "FAKE_ZFS_LOG": str(wd / "zfs.log"), "FAKE_ZFS_STATE": str(wd / "zfs_state.json")}


def _perl_path(wd: Path) -> str:
    """Upstream's PATH: the fake zfs first (none when the lookup can't start; see _setup), then this Python, which the
    fake zfs runs under, and the system's."""
    return f"{wd / 'bin'}:{Path(sys.executable).parent}:/usr/bin:/bin"


def _collect(wd: Path, scn: dict, died: bool, died_msg: str, cfg: dict, probes: dict, stdout: str, stderr: str) -> dict:
    f = wd / "cache" / "datasets.txt"
    initial = (NOW - scn["cache"]["age"]) if scn["cache"] is not None else None
    return {
        "died": died, "died_msg": died_msg, "config": cfg, "probes": probes,
        "ds_file": {"exists": f.exists(), "content": f.read_bytes().decode("latin-1") if f.is_file() else None,
                    "mtime_kept": (int(f.stat().st_mtime) == initial) if f.exists() and initial is not None else None},
        "zfs_calls": [json.loads(x) for x in (wd / "zfs.log").read_text().split("\n") if x],
        # split on "\n" only: a name may hold U+2028 or NEL, which str.splitlines() would also split on
        "stdout": [ln for ln in stdout.replace("another pysanoid process", "another sanoid process").split("\n")
                   if ln and not ln.startswith(MERGE_TRACE)],
        "warnings": [ln for ln in stderr.split("\n") if WARNING_RE.match(ln)],
    }


def run_perl(wd: Path, scn: dict) -> dict:
    wd = wd.resolve()  # see diff_prune.py's run_perl for why: a relative wd double-resolves inside the Perl child
    _setup(wd, scn)
    env = {"LOCK_FREE": "1" if scn["lock_free"] else "0", "RUN_UNUSABLE": "1" if scn.get("run_unusable") else "",
           **_zfs_env(wd, scn), "PATH": _perl_path(wd), **cpan_env()}
    stdin_obj = {k: scn[k] for k in ("verbose", "debug", "force_update", "ttl", "probe")}
    result = run_harness(SCRIPT, stdin_obj=stdin_obj, argv=[str(wd)], env=env)
    if result.data is None:
        raise RuntimeError(f"perl harness produced no result:\n{result.stderr}")
    o = result.data
    return _collect(wd, scn, bool(o["died"]), o["died_msg"], o["config"], o["probes"], result.stdout, result.stderr)


def _norm(v: Any) -> str:
    return "1" if v is True else "0" if v is False else str(v)


def run_python(wd: Path, scn: dict) -> dict:
    wd = wd.resolve()
    _setup(wd, scn)
    env = _zfs_env(wd, scn)
    os.environ.update(env)
    p.ZFS = env["ZFS_BIN"]
    held = None
    if not scn["lock_free"]:
        held = open(wd / "run" / "sanoid_cachedatasetupdate.lock", "w")  # noqa: SIM115 - held open across this function, released in `finally`
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    out, err = io.StringIO(), io.StringIO()
    died, died_msg, cfg, probes = False, "", {}, {}
    problems: list[str] = []
    zc = p.ZfsClient(wd / "cache", scn["ttl"], scn["force_update"], debug=scn["debug"])
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            raw = p.load_config(wd / "conf", zc)
            zc.flush_dataset_cache(wd / "run", verbose=scn["verbose"], problems=problems)
            cfg = {_b2l(sec): {k: _b2l(_norm(v)) for k, v in c.items()} for sec, c in raw.items()}
            for fs in scn["probe"]:
                probes[fs] = [_b2l(x) for x in zc.get_child_datasets(fs)]
    except Exception as e:  # noqa: BLE001 - harness: any failure counts as "died"
        died, died_msg = True, str(e)
    finally:
        if held:
            held.close()
    return {**_collect(wd, scn, died, died_msg, cfg, probes, _as_perl_output(out.getvalue()), _as_perl_output(err.getvalue())),
            "reported": len(problems)}


LOOKUP_FAILED = "a child lookup that can't start, is killed or fails partway: pysanoid stops, upstream carries on"
NUL_CACHE = "a datasets.txt holding a NUL: upstream uses it, pysanoid asks zfs as if there were none"
RUN_UNUSABLE = "no lock file can be made: upstream rewrites datasets.txt unlocked and dies, pysanoid reports it"
ORDER = "a section above the recursive section it belongs under: pysanoid refuses the file, upstream loads it"
FUTURE_CACHE = "a datasets.txt dated in the future: upstream uses it, pysanoid asks zfs as if there were none"
UNLISTED = ("a section under a recursive section whose lookup didn't list it: upstream builds it from the defaults, "
            "pysanoid from that section's settings")
COMPARED = ("config", "probes", "ds_file", "zfs_calls", "stdout", "warnings")
ZFS_AUTOSNAP_RE = re.compile(r"^ignored autosnap configuration for '(.*)' because it's part of a zfs recursion\.$")


def _cache_only_upstream_uses(scn: dict) -> str | None:
    """The class of a datasets.txt upstream reads and pysanoid counts as no cache, or None."""
    c = scn["cache"]
    if c is None or scn["force_update"] or c["age"] > scn["ttl"]:
        return None
    return NUL_CACHE if b"\0" in c["bytes"] else FUTURE_CACHE if c["age"] < 0 else None


def _unlisted_check(conf: Path, a: dict, b: dict, diffs: list[str]) -> bool:
    """Whether upstream and pysanoid differ only as the inheritance rule says: in sections named under a recursive
    section, each of which starts from that section's settings as dev/lib/configorder.py states the rule, and in what
    lies under them, where only the keys such a section itself differs in may differ. Every other section is
    upstream's; pysanoid's one extra warning is that a section inside a zfs recursion asked for autosnap."""
    if not set(diffs) <= {"config", "warnings"}:
        return False
    above = {_b2l(name): _b2l(parent) for name, parent in recursion_above(conf).items()}
    differing = [name for name in above if a["config"].get(name) != b["config"].get(name)]
    if not differing:
        return False
    for name in differing:
        mine, parent, theirs = b["config"].get(name), b["config"].get(above[name]), a["config"].get(name)
        raw_name = next(raw for raw in recursion_above(conf) if _b2l(raw) == name)
        if mine is None or parent is None or theirs is None or starts_from_parent(mine, parent, theirs, *own_settings(conf, raw_name)):
            return False
    for name in set(a["config"]) | set(b["config"]):
        if name in differing or a["config"].get(name) == b["config"].get(name):
            continue
        over = next((d for d in differing if name.startswith(f"{d}/")), None)
        mine, theirs = b["config"].get(name), a["config"].get(name)
        if over is None or mine is None or theirs is None or set(mine) != set(theirs):
            return False
        if any(mine[k] != theirs[k] and b["config"][over].get(k) == a["config"][over].get(k) for k in mine):
            return False
    if [w for w in a["warnings"] if w not in b["warnings"]]:
        return False
    extra = [ZFS_AUTOSNAP_RE.match(w) for w in b["warnings"] if w not in a["warnings"]]
    return all(m is not None and m.group(1) in differing for m in extra)


def run_both(scn: dict) -> tuple[dict, dict, list[str], list[str]]:
    a, b = run_perl(WORK / "perl", scn), run_python(WORK / "python", scn)
    conf = (WORK / "python" / "conf").resolve()
    misplaced = first_misplaced(conf / "sanoid.conf")
    if b["died"] and " must come after [" in b["died_msg"]:
        # the order rule's words, and nothing written: datasets.txt as it was
        before = scn["cache"]["bytes"].decode("latin-1") if scn["cache"] is not None else None
        if misplaced is not None and b["died_msg"] == refusal(f"{conf}/sanoid.conf", *misplaced) \
                and b["ds_file"]["content"] == before:
            return a, b, [], [ORDER]
        return a, b, ["order self-check"], []
    if misplaced is not None and not b["died"]:
        return a, b, ["order: pysanoid loaded a file the rule refuses"], []
    upstream_died_unlocking = bool(scn.get("run_unusable")) and a["died"] and "No valid lockfile" in a["died_msg"]
    if (b["died"] and (not a["died"] or upstream_died_unlocking) and scn["lookup"] != "ok"
            and " list -o name " in b["died_msg"]):
        return a, b, [], [LOOKUP_FAILED]  # pysanoid stopped at the lookup, before any lock
    if upstream_died_unlocking:
        # pysanoid against itself: it carries on, reports it once, and leaves datasets.txt as it was
        before = scn["cache"]["bytes"].decode("latin-1") if scn["cache"] is not None else None
        if not b["died"] and b["reported"] == 1 and b["ds_file"]["content"] == before:
            return a, b, [], [RUN_UNUSABLE]
        return a, b, ["unusable self-check"], []
    if a["died"] != b["died"]:
        return a, b, ["died"], []
    if a["died"]:
        return a, b, [], []  # both refused: the wording/die-point legitimately differs
    diffs = [k for k in COMPARED if a[k] != b[k]]
    unused = _cache_only_upstream_uses(scn)
    if diffs and unused is not None:
        # pysanoid must behave exactly as with no datasets.txt: rerun it without one, and compare it with itself
        c = run_python(WORK / "python_no_cache", {**scn, "cache": None})
        same = [k for k in ("config", "probes", "zfs_calls", "warnings") if b[k] == c[k]]
        written_alike = b["ds_file"]["content"] == c["ds_file"]["content"]
        # with the update lock held, or no lock file to be made, neither run may write: the file is left as it was
        left_alone = ((not scn["lock_free"] or bool(scn.get("run_unusable"))) and c["ds_file"]["content"] is None
                      and b["ds_file"]["content"] == scn["cache"]["bytes"].decode("latin-1"))
        if (len(same) == 4 and (written_alike or left_alone) and b["reported"] == c["reported"]
                and [ln for ln in b["stdout"] if "not expired" not in ln] == c["stdout"]):
            return a, b, [], [unused]
    if diffs and _unlisted_check(conf / "sanoid.conf", a, b, diffs):
        return a, b, [], [UNLISTED]
    return a, b, diffs, []


def S(conf: str | bytes, cache: dict | None = None, **kw: Any) -> dict:
    scn = {"conf": conf.encode() if isinstance(conf, str) else conf, "cache": cache, "tree": TREE, "force_update": False,
           "verbose": True, "debug": False, "ttl": 1200, "lock_free": True, "probe": [], "lookup": "ok", "list_prints": 1}
    scn.update(kw)
    return scn


def C(text: str | bytes, age: int = 5) -> dict:
    return {"bytes": text.encode() if isinstance(text, str) else text, "age": age}


NOREC = "[tank/a]\n    daily = 3\n[tank/b]\n    daily = 2\n"
REC = "[tank]\n    recursive = yes\n    daily = 9\n"
OKC = "tank\ntank/a\ntank/a/x\ntank/a/y\ntank/b\ntank/b/x\n"
EXPLICIT = "[tank]\n    recursive = yes\n    hourly = 100\n[tank/a]\n    daily = 0\n"

HAND = {
    "no recursion + a valid non-empty cache: upstream leaves it alone": S(NOREC, C(OKC)),
    "no recursion + no cache: written": S(NOREC, None),
    "no recursion + expired cache (1201s): rewritten": S(NOREC, C(OKC, 1201)),
    "no recursion + cache exactly at the TTL (1200s): kept": S(NOREC, C(OKC, 1200)),
    "no recursion + force-update: rewritten": S(NOREC, C(OKC), force_update=True),
    "no recursion + empty cache file: treated as absent, rewritten": S(NOREC, C("")),
    "no recursion + lock held: deferred, in upstream's words": S(NOREC, None, lock_free=False),
    "recursion + complete valid cache: no zfs call, no rewrite": S(REC, C(OKC), probe=["tank", "tank/a"]),
    "recursion + no cache: live query, cache written with children": S(REC, None, probe=["tank"]),
    "recursion + cache missing the parent: live fallback, cache NOT rewritten": S(REC, C("tank/a\ntank/b\n"), probe=["tank"]),
    "recursion + expired cache: live + rewritten": S(REC, C(OKC, 1201), probe=["tank"]),
    "recursion + cache with CRLF endings: parent never matches, live fallback": S(REC, C(OKC.replace("\n", "\r\n")), probe=["tank"]),
    "recursion + a non-UTF-8 byte in the cache": S(REC, C(b"tank\ntank/caf\xe9\ntank/b\n"), probe=["tank"]),
    "no recursion + a section path with a latin-1 byte": S(b"[tank/caf\xe9]\n    daily = 3\n", None),
    "recursion on a dataset that does not exist: zfs complains": S("[tank/nope]\n    recursive = yes\n    daily = 3\n", None),
    "--debug, no cache: the lookup's line": S(REC, None, debug=True, probe=["tank/a"]),
    "--debug, a fresh cache: 'not expired'": S(REC, C(OKC, 37), debug=True),
    "--debug, a fresh empty cache: 'not expired', then rewritten": S(REC, C("", 12), debug=True),
    "--debug, an expired cache: no 'not expired' line": S(REC, C(OKC, 1201), debug=True),
    "warnings: a duplicate key, an ignored template key, a zfs-recursion child set to autosnap": S(
        "[template_default]\n    recursive = yes\n[tank]\n    recursive = zfs\n    daily = 3\n    daily = 4\n"
        "[tank/a]\n    autosnap = yes\n", None),
    "recursion + a NUL in a child's line: pysanoid asks zfs": S(REC, C(b"tank\ntank/a\0x\ntank/b\n"), probe=["tank"],
                                                               known=[NUL_CACHE]),
    "no recursion + a NUL: no cache, rewritten": S(NOREC, C(b"tank/a\0\ntank/b\n"), known=[NUL_CACHE]),
    "a NUL in an expired cache: no difference": S(REC, C(b"tank\ntank/a\0x\n", 1201)),
    "recursion + the lookup can't start": S(REC, None, lookup="cannot_start", known=[LOOKUP_FAILED]),
    "recursion + the lookup is killed after the parent": S(REC, None, lookup="killed", known=[LOOKUP_FAILED]),
    "recursion + the lookup fails after two lines": S(REC, None, lookup="partway", list_prints=2, known=[LOOKUP_FAILED]),
    "recursion + a lookup that can't start isn't needed: the cache answers": S(REC, C(OKC), lookup="cannot_start"),
    "an explicit child + the lookup killed: upstream sets it up from the defaults": S(EXPLICIT, None, lookup="killed",
                                                                                     known=[LOOKUP_FAILED]),
    "recursion on a dataset that is not there, zfs killed: nothing listed, a signal all the same": S(
        "[tank/nope]\n    recursive = yes\n", None, lookup="killed", known=[LOOKUP_FAILED]),
    "no lock file can be made, no cache: reported, not written": S(NOREC, None, run_unusable=True, known=[RUN_UNUSABLE]),
    "no lock file can be made, an expired cache: reported, left as it was": S(REC, C(OKC, 1201), run_unusable=True,
                                                                               probe=["tank"], known=[RUN_UNUSABLE]),
    "no lock file can be made, a usable cache: no lock needed": S(REC, C(OKC), run_unusable=True, probe=["tank"]),
    "an explicit child above its recursive parent: refused": S("[tank/a]\n    daily = 0\n[tank]\n    recursive = yes\n"
                                                               "    hourly = 100\n", C(OKC, 1201), known=[ORDER]),
    "an explicit child above its skip_children parent: refused": S("[tank/a]\n    daily = 0\n[tank]\n"
                                                                   "    skip_children = yes\n", None, known=[ORDER]),
    "an explicit child the cache doesn't list: upstream sets it up from the defaults": S(
        EXPLICIT, C("tank\ntank/b\n"), known=[UNLISTED]),
    "an explicit child + a lookup that fails having listed nothing: the same": S(
        EXPLICIT, None, lookup="partway", list_prints=0, known=[UNLISTED]),
    "an explicit child of a recursive dataset that isn't there: the same": S(
        "[tank/nope]\n    recursive = yes\n    hourly = 100\n[tank/nope/x]\n    daily = 0\n", None, known=[UNLISTED]),
    "an explicit child with a template, unlisted: the template's keys are its own": S(
        "[tank]\n    recursive = yes\n    hourly = 100\n    monthly = 12\n[tank/zz]\n    use_template = t\n    daily = 0\n"
        "[template_t]\n    monthly = 3\n    autosnap = no\n", None, known=[UNLISTED]),
    "an explicit child under recursive = zfs, unlisted: no snapshots of its own, and told so": S(
        "[tank]\n    recursive = zfs\n    hourly = 100\n[tank/zz]\n    autosnap = yes\n", None, known=[UNLISTED]),
    "an explicit grandchild, neither it nor its parent listed: the nearer recursion's settings": S(
        "[tank]\n    recursive = yes\n    hourly = 100\n[tank/zz]\n    recursive = yes\n    monthly = 24\n"
        "[tank/zz/y]\n    daily = 0\n", None, known=[UNLISTED]),
    "an unlisted explicit child that is recursive itself: its children follow it": S(
        "[tank]\n    recursive = yes\n    hourly = 100\n[tank/a]\n    recursive = yes\n    monthly = 24\n",
        C("tank\ntank/b\n"), known=[UNLISTED]),
    "an explicit child under skip_children, unlisted: the defaults in both": S(
        "[tank]\n    skip_children = yes\n    hourly = 100\n[tank/zz]\n    daily = 0\n", None),
    "an explicit child the lookup lists: no difference": S(EXPLICIT, None),
    "recursion + a cache dated a second ahead: pysanoid asks zfs": S(REC, C(OKC, -1), probe=["tank"], known=[FUTURE_CACHE]),
    "no recursion + a cache dated a year ahead: no cache, rewritten": S(NOREC, C(OKC, -365 * 86400), known=[FUTURE_CACHE]),
    "a cache dated ahead + force-update: no difference": S(REC, C(OKC, -1), force_update=True),
}

ODD = [b"tank/caf\xe9", "tank/café".encode(), b"tank/a\xc2\x85b", b"tank/a\xe2\x80\xa8b"]


def gen_cache(r: random.Random) -> dict | None:
    if r.random() < 0.2:
        return None
    lines = []
    for parent in r.sample(TREE, r.randint(0, 4)):
        lines.append(parent)
        if r.random() < 0.7:
            lines += [d for d in TREE if d.startswith(parent + "/") and r.random() < 0.85]
    if r.random() < 0.3:
        r.shuffle(lines)
    if r.random() < 0.08 and lines:
        lines.append(r.choice(lines))
    if r.random() < 0.08:
        lines.insert(r.randint(0, len(lines)), r.choice(ODD).decode("latin-1"))
    if r.random() < 0.06 and lines:  # damage: a NUL inside a line
        i = r.randrange(len(lines))
        cut = r.randint(0, len(lines[i]))
        lines[i] = lines[i][:cut] + "\0" + lines[i][cut:]
    text = "\n".join(lines) + ("\n" if lines else "")
    if r.random() < 0.08:
        text = text.rstrip("\n")
    if r.random() < 0.06:
        text = text.replace("\n", "\r\n")
    return {"bytes": text.encode("latin-1"), "age": r.choice([0, 5, 600, 1199, 1200, 1201, 5000])}


TOGGLES = ("1", "true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON",
          "0", "false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF")


def gen(r: random.Random) -> dict:
    blocks = []
    for n in r.sample(TREE, r.randint(1, 5)):
        body = []
        rec = r.choice([None, None, "yes", "no", "zfs", r.choice(TOGGLES)])
        if rec:
            body.append(f"    recursive = {rec}")
        body.append(f"    daily = {r.randint(0, 90)}")
        if r.random() < 0.1:
            body.append(f"    daily = {r.randint(0, 90)}")  # a repeated key
        if r.random() < 0.15:
            body.append(f"    autosnap = {r.choice(TOGGLES)}")
        if r.random() < 0.3:
            body.append(f"    process_children_only = {r.choice(TOGGLES)}")
        blocks.append((n, body))
    if r.random() < 0.12:
        odd = r.choice(ODD)
        blocks.append((odd.decode("latin-1"), [f"    daily = {r.randint(0, 30)}"]))
    if r.random() < 0.05:
        blocks.append(("template_default", ["    recursive = yes"]))
    r.shuffle(blocks)
    if r.random() < 0.85:  # mostly parents first, as the order rule has them, so loading is compared, not only refused
        at = [i for i, (name, _) in enumerate(blocks) if not name.startswith("template_")]
        for i, block in zip(at, sorted((blocks[i] for i in at), key=lambda b: b[0].count("/")), strict=True):
            blocks[i] = block
    conf = "\n\n".join(f"[{name}]\n" + "\n".join(body) for name, body in blocks).encode("latin-1") + b"\n"
    lookup = r.choices(["ok", "cannot_start", "killed", "partway"], weights=[85, 5, 5, 5])[0]
    lock_free = r.random() < 0.85
    scn = {"conf": conf, "cache": gen_cache(r), "tree": TREE, "force_update": r.random() < 0.1, "verbose": r.random() < 0.6,
           "debug": r.random() < 0.3, "ttl": r.choice([1200, 1200, 1200, 60, 18000]), "lock_free": lock_free,
           "probe": r.sample(TREE, r.randint(0, 2)), "lookup": lookup, "list_prints": r.randint(1, 3),
           "run_unusable": lock_free and r.random() < 0.06}
    late = random.Random(conf)  # a stream of its own: a seed still gives the scenarios it gave before these were added
    if scn["cache"] is not None and late.random() < 0.06:
        scn["cache"]["age"] = late.choice([-1, -5000])  # written while the clock ran ahead
    if lookup == "partway" and late.random() < 0.4:
        scn["list_prints"] = 0  # the lookup fails having listed nothing
    return scn


def show(scn: dict, a: dict, b: dict, diffs: list[str]) -> None:
    print(f"  conf={scn['conf'][:200]!r} lookup={scn['lookup']} debug={scn['debug']}")
    print(f"  cache={'missing' if scn['cache'] is None else (scn['cache']['age'], scn['cache']['bytes'][:100])!r}")
    for k in diffs:
        if k == "died":
            print(f"    died: perl={a['died']} python={b['died']} ({b['died_msg'] or a['died_msg']})")
        elif k not in a:
            print(f"    {k}: python died={b['died']} ({b['died_msg']}) reported={b['reported']} ds_file={b['ds_file']!r}")
        else:
            print(f"    {k}:\n      perl  ={a[k]!r}\n      python={b[k]!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=500)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()

    tally = {"match": 0, "DIFF": 0}
    known_tally: dict[str, int] = {}
    if args.mode == "hand":
        for name, scn in HAND.items():
            a, b, diffs, known = run_both(scn)
            expected = scn.get("known", [])
            ok = not diffs and known == expected
            tally["match" if ok else "DIFF"] += 1
            for k in known:
                known_tally[k] = known_tally.get(k, 0) + 1
            label = "DIFF " if not ok else ("KNOWN" if known else "MATCH")
            print(f"[{label}] {name}" + (f"  (classed {known}, expected {expected})" if known != expected else ""))
            if diffs:
                show(scn, a, b, diffs)
    else:
        r = random.Random(args.seed)
        seen: dict[tuple, dict] = {}
        for _ in range(args.n):
            scn = gen(r)
            a, b, diffs, known = run_both(scn)
            tally["match" if not diffs else "DIFF"] += 1
            for k in known:
                known_tally[k] = known_tally.get(k, 0) + 1
            if diffs:
                seen.setdefault(tuple(diffs), scn)
        for sig, scn in seen.items():
            a, b, diffs, _ = run_both(scn)
            print(f"\n--- differs on {sig}")
            show(scn, a, b, diffs)
    print(tally, "known/documented classes:", known_tally)
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if tally["DIFF"] else 0)


if __name__ == "__main__":
    main()
