#!/usr/bin/env python3
"""Differential test of whole runs: upstream's whole script, `dev/upstream/sanoid` as it ships, and pysanoid.py, each
run as a program, one run or several, on copies of one pool that changes (dev/fake_pool: a snapshot made is listed
from then on, a snapshot destroyed is gone). What each program DID is compared: the snapshots destroyed and made (and
each `zfs snapshot` and `zfs destroy` it ran), every hook's arguments and SANOID_* variables in the order it ran them
for each dataset, and whether each run succeeded. What each program printed is `zfs_output`'s to compare.

The clock is frozen, on both sides, at the scenario's time: upstream under lib/FakeTime.pm, pysanoid under
lib/run_frozen.py. A run starts a scenario's `at` seconds after the first, and between runs the cache files are
dated at the run before, so a cache's age is what the scenario says (a frozen clock and real file dates would
disagree). The production-shaped config (fixtures/sanoid.conf) runs in Europe/Dublin on a June day, every other scenario in UTC: upstream computes its
own DST offsets (DEV-030), and no clock change falls in what these scenarios look back on.

A stand-in `ps` goes first on upstream's PATH (its lock code asks `ps` about a PID; the sandbox forbids the real one),
and dev/fake_hook/hook_probe is the hook, logging what it was given.

Known classes (each checked by its rule):
  - DEV-028: pysanoid exits non-zero after something went unprotected (its closing count of problems says so) where
    upstream exits 0; everything either did is the same.
  - DEV-050: a defaults version upstream numifies (`2.5`, `+2`, `2x`): pysanoid refuses the file and does nothing.
  - DEV-032: a `frequent_period` that is a comment alone, which pysanoid reads as blank (15) and upstream as 0.
  - DEV-012: a section with `path =` and its own pre_pruning_script: pysanoid also runs that hook before each
    destroy, where upstream reads hooks only from the section named after the dataset.
  - DEV-017: a recursive section whose path holds a space: upstream's shell splits the name and finds no children.

Run: python3 dev/diff_whole_run.py hand
     python3 dev/diff_whole_run.py fuzz <n> <seed>
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
from lib.perlrun import PERL, cpan_env

UPSTREAM = DEV_DIR / "upstream" / "sanoid"
REPO = DEV_DIR.parent
DEFAULTS = (REPO / "sanoid.defaults.conf").read_text()
PRODUCTION_CONF = (DEV_DIR / "fixtures" / "sanoid.conf").read_text()
HOOK = DEV_DIR / "fake_hook" / "hook_probe"
WORK = DEV_DIR / "work" / "whole_run"
H, D = 3600, 86400
JUNE = int(datetime(2026, 6, 17, 13, 34, 56, tzinfo=UTC).timestamp())  # a Wednesday, far from either clock change

DEV_028 = "DEV-028: pysanoid exits non-zero for what went unprotected, upstream 0"
DEV_050 = "DEV-050: a defaults version upstream numifies, refused"
DEV_032 = "DEV-032: frequent_period a comment alone, read as blank"
DEV_012 = "DEV-012: a path = section's own veto hook runs too"
DEV_017 = "DEV-017: a space in a recursive section's path, children found only here"
DEV_031 = "DEV-031: weekly_wday = 7 read as Sunday, where upstream takes a weekly every run"
DEV_028_DIED = "DEV-028: a bad schedule value costs its type only, where upstream dies"


def series(ds: str, typ: str, every: int, count: int, first: int) -> list[tuple[str, str, int]]:
    """`count` snapshots of `typ` on `ds`, the newest `first` seconds old, one every `every` seconds."""
    return [(ds, typ, first + k * every) for k in range(count)]


def snap_name(when: int, typ: str, tz: str) -> str:
    return f"autosnap_{datetime.fromtimestamp(when, _zone(tz)):%Y-%m-%d_%H:%M:%S}_{typ}"


def _zone(tz: str):
    from zoneinfo import ZoneInfo
    return ZoneInfo(tz)


def _bin(wd: Path) -> Path:
    b = wd / "bin"
    b.mkdir(parents=True)
    (b / "ps").write_text("#!/bin/sh\necho sanoid\n")
    (b / "ps").chmod(0o755)
    (b / "python3").symlink_to(sys.executable)  # for the fakes' `#!/usr/bin/env python3`
    return b


def _date_caches(wd: Path, when: int) -> None:
    for f in (wd / "cache").glob("*"):
        os.utime(f, (when, when))


def run(side: str, scn: dict[str, Any]) -> dict[str, Any]:
    wd = WORK / f"{side}-{os.getpid()}"
    shutil.rmtree(wd, ignore_errors=True)
    (wd / "conf").mkdir(parents=True)
    hook = f"{HOOK}"
    (wd / "conf" / "sanoid.conf").write_text(scn["conf"].replace("@HOOK@", hook))
    (wd / "conf" / "sanoid.defaults.conf").write_text(scn.get("defaults", DEFAULTS))
    now, tz = scn.get("now", JUNE), scn.get("tz", "UTC")
    snaps = sorted([[f"{d}@{snap_name(now - age, t, tz)}", now - age] for d, t, age in scn["seed"]]
                   + [list(x) for x in scn.get("extra", [])], key=lambda s: s[1])
    pool = wd / "pool.json"
    pool.write_text(json.dumps({"datasets": scn["datasets"], "snaps": snaps, "cap": scn.get("cap", {})}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("SANOID_")}
    env.update(PATH=os.pathsep.join([str(_bin(wd)), str(DEV_DIR / "fake_pool"), "/usr/bin", "/bin"]),
               FAKE_POOL=str(pool), TZ=tz, **scn.get("env", {}))
    libs = cpan_env()
    argv = ([PERL, "-I", libs["INI_LIB"], "-I", libs["STRINGY_LIB"], "-I", str(DEV_DIR / "lib"), "-MFakeTime",
             str(UPSTREAM)] if side == "perl" else [sys.executable, str(DEV_DIR / "lib" / "run_frozen.py")])
    dirs = ["--configdir", str(wd / "conf"), "--cache-dir", str(wd / "cache"), "--run-dir", str(wd / "run")]
    runs = []
    previous = None
    for i, step in enumerate(scn.get("runs", [{"args": []}])):
        when = now + step.get("at", 0)
        if previous is not None:
            _date_caches(wd, previous)
        run_env = {**env, "FAKE_NOW": str(when), "HOOK_LOG": str(wd / f"hooks-{i}.log")}
        proc = subprocess.run([*argv, *dirs, *step["args"]], env=run_env, cwd=wd,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120, check=False)
        runs.append({"rc": proc.returncode, "output": proc.stdout.decode("utf-8", "backslashreplace")})
        previous = when
    final = {s[0] for s in json.loads(pool.read_text())["snaps"]}
    before = {s[0] for s in snaps}
    log = Path(f"{pool}.log")
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    hooks: dict[str, list[Any]] = defaultdict(list)
    for i in range(len(runs)):
        log_i = wd / f"hooks-{i}.log"
        for line in log_i.read_text().splitlines() if log_i.exists() else []:
            entry = json.loads(line)
            hooks[f"run {i + 1}: {entry['env'].get('SANOID_TARGET', '')}"].append(entry)
    result = {"runs": runs, "destroyed": sorted(before - final), "made": sorted(final - before),
              "acts": sorted(" ".join(c) for c in calls if c[:1] in (["snapshot"], ["destroy"])),
              "hooks": {k: _in_type_order(v) for k, v in hooks.items()}}
    shutil.rmtree(wd, ignore_errors=True)
    return result


_PRUNE_TAGS = ("pre_pruning", "pruning")


def _in_type_order(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One dataset's hook calls in one run: take's as they came, then prune's grouped by type, each type's in the order
    they came. Upstream prunes a section's types in Perl's per-process random hash order (`keys %{$config{$section}}`),
    so only the order within a type can be matched."""
    take = [e for e in entries if e["tag"] not in _PRUNE_TAGS]
    prune = [e for e in entries if e["tag"] in _PRUNE_TAGS]
    return take + sorted(prune, key=lambda e: e["env"].get("SANOID_SNAPNAME", "").rsplit("_", 1)[-1])


def _problems_line(output: str) -> bool:
    return "problem(s) this run (see above); exiting non-zero" in output


def compare(scn: dict[str, Any]) -> tuple[list[str], list[str], dict[str, Any], dict[str, Any]]:
    """(differences, known classes seen, upstream's result, pysanoid's)."""
    a, b = run("perl", scn), run("python", scn)
    known: list[str] = []
    diffs = [k for k in ("destroyed", "made", "acts", "hooks") if a[k] != b[k]]
    for i, (ra, rb) in enumerate(zip(a["runs"], b["runs"], strict=True)):
        if (ra["rc"] == 0) == (rb["rc"] == 0):
            continue
        if ra["rc"] == 0 and _problems_line(rb["output"]):
            known.append(DEV_028)
        else:
            diffs.append(f"run {i + 1} exit status (upstream {ra['rc']}, pysanoid {rb['rc']})")
    for check in scn.get("known", []):
        diffs, seen = KNOWN_RULES[check](scn, a, b, diffs)
        known += seen
    return diffs, sorted(set(known)), a, b


# --- the known classes' rules: each takes what differs and gives back what its class doesn't explain -------------

def _dev_050(scn, a, b, diffs):
    refused = all(r["rc"] != 0 and "'version' in [version]" in r["output"] for r in b["runs"])
    if refused and not b["made"] and not b["destroyed"] and not b["hooks"]:
        return [d for d in diffs if d not in ("destroyed", "made", "acts", "hooks") and "exit status" not in d], [DEV_050]
    return diffs, []


def _dev_032(scn, a, b, diffs):
    """pysanoid destroys no frequent snapshot upstream keeps; upstream's extra destroys are all frequentlies."""
    extra = set(a["destroyed"]) - set(b["destroyed"])
    if not set(b["destroyed"]) - set(a["destroyed"]) and extra and all(s.endswith("_frequently") for s in extra):
        rest = [d for d in diffs if d not in ("destroyed", "acts")]
        return rest, [DEV_032]
    return diffs, []


def _dev_012(scn, a, b, diffs):
    """pysanoid's hooks are upstream's plus pre_pruning calls, one before each destroy, all of which let it go."""
    extra = {t: [e for e in es if e not in a["hooks"].get(t, [])] for t, es in b["hooks"].items()}
    added = [e for es in extra.values() for e in es]
    if added and all(e["tag"] == "pre_pruning" and e["argv"][1] == "0" for e in added) and "hooks" in diffs:
        trimmed = {t: [e for e in es if e["tag"] != "pre_pruning"] for t, es in b["hooks"].items()}
        upstream = {t: [e for e in es if e["tag"] != "pre_pruning"] for t, es in a["hooks"].items()}
        if {t: es for t, es in trimmed.items() if es} == {t: es for t, es in upstream.items() if es}:
            return [d for d in diffs if d != "hooks"], [DEV_012]
    return diffs, []


def _target(entry: str) -> str:
    """The snapshot a destroyed/made name or an act names: `destroy x@y`, `snapshot -r x@y` and `x@y` give x@y."""
    for verb in ("destroy ", "snapshot -r ", "snapshot "):
        if entry.startswith(verb):
            return entry[len(verb):]
    return entry


def _only_pysanoid_did_more(a, b, diffs, extra_ok, cls):
    """Everything upstream did, pysanoid did, and what it alone did passes `extra_ok`."""
    keys = ("destroyed", "made", "acts")
    if all(set(a[k]) <= set(b[k]) and all(extra_ok(_target(x)) for x in set(b[k]) - set(a[k])) for k in keys) \
            and any(set(b[k]) - set(a[k]) for k in keys):
        return [d for d in diffs if d not in keys], [cls]
    return diffs, []


def _dev_017(scn, a, b, diffs):
    """What pysanoid alone did is on the children of the spaced path, which upstream's shell never found."""
    return _only_pysanoid_did_more(a, b, diffs, lambda s: s.split("@")[0].startswith(scn["spaced"] + "/"), DEV_017)


def _dev_031(scn, a, b, diffs):
    """Every difference is a weekly, and upstream made at least as many as pysanoid."""
    keys = ("destroyed", "made", "acts")
    weekly_only = all(_target(x).endswith("_weekly") for k in keys for x in set(a[k]) ^ set(b[k]))
    if weekly_only and len(a["made"]) >= len(b["made"]) and any(set(a[k]) ^ set(b[k]) for k in keys):
        return [d for d in diffs if d not in keys], [DEV_031]
    return diffs, []


def _dev_028_died(scn, a, b, diffs):
    """Upstream died on the bad value before taking or pruning anything; pysanoid did the rest, nothing of the bad type,
    and exited non-zero saying so."""
    bad = scn["bad_type"]
    upstream_idle = not a["acts"] and not a["hooks"] and all(r["rc"] != 0 for r in a["runs"])
    reported = all(r["rc"] != 0 and _problems_line(r["output"]) for r in b["runs"])
    if upstream_idle and reported and b["acts"] and not any(f"_{bad}" in x for x in b["made"]):
        return [d for d in diffs if d not in ("destroyed", "made", "acts", "hooks")], [DEV_028_DIED]
    return diffs, []


KNOWN_RULES = {"DEV-050": _dev_050, "DEV-032": _dev_032, "DEV-012": _dev_012, "DEV-017": _dev_017,
               "DEV-031": _dev_031, "DEV-028-died": _dev_028_died}


# --- the hand cases: the fifth audit's scenarios -----------------------------------------------------------------

PRODUCTION_DS = ["tank", "tank/software", "tank/software/a", "tank/software/a/x", "tank/software/b",
            "tank/encrypted", "tank/encrypted/archive", "tank/encrypted/backup", "tank/encrypted/home",
            "tank/encrypted/home/alice", "tank/encrypted/home/bob", "tank/vm", "tank/media",
            "zroot", "zroot/media", "zroot/db", "zroot/dns", "zroot/bin", "zroot/docker", "zroot/home"]
PRODUCTION_SEED: list[tuple[str, str, int]] = []
for _ds in ("tank/software", "tank/software/a", "tank/software/a/x", "tank/software/b", "tank/encrypted/archive"):
    PRODUCTION_SEED += series(_ds, "daily", D, 40, 5 * H)
for _ds in ("tank/encrypted/backup", "tank/encrypted/home/alice", "tank/encrypted/home/bob", "tank/vm"):
    PRODUCTION_SEED += series(_ds, "daily", D, 10, 5 * H) + series(_ds, "weekly", 7 * D, 8, 2 * D) \
        + series(_ds, "monthly", 31 * D, 45, 9 * D)
PRODUCTION_SEED += series("tank/media", "hourly", H, 20, 1200) + series("tank/media", "daily", D, 10, 5 * H)
for _ds in ("zroot/media", "zroot/db", "zroot/dns", "zroot/bin", "zroot/docker", "zroot/home"):
    PRODUCTION_SEED += series(_ds, "daily", D, 12, 5 * H)

HOOKS = ("\tpre_snapshot_script = @HOOK@ pre_snapshot {pre}\n\tpost_snapshot_script = @HOOK@ post_snapshot 0\n"
         "\tpre_pruning_script = @HOOK@ pre_pruning {veto}\n\tpruning_script = @HOOK@ pruning 0\n")
COUNTS = "\tfrequently = 0\n\thourly = 2\n\tdaily = 0\n\tweekly = 0\n\tmonthly = 0\n\tyearly = 0\n"
ALL2 = "\tfrequently = 2\n\thourly = 2\n\tdaily = 2\n\tweekly = 2\n\tmonthly = 2\n\tyearly = 2\n"
A = ["tank", "tank/a"]


def hooks(pre: int = 0, veto: int = 0) -> str:
    return HOOKS.format(pre=pre, veto=veto)


def production(**over: Any) -> dict[str, Any]:
    return {"conf": PRODUCTION_CONF, "datasets": PRODUCTION_DS, "seed": PRODUCTION_SEED, "cap": {"tank": "50%", "zroot": "40%"},
            "tz": "Europe/Dublin", **over}


HAND: dict[str, dict[str, Any]] = {
    "the production-shaped config: a cron run, then one 20 minutes later on its cache, then one an hour on": production(
        runs=[{"args": []}, {"args": [], "at": 20 * 60}, {"args": [], "at": 80 * 60}]),
    "tank at 90%, so backup's prune_defer = 85 lets it prune": production(cap={"tank": "90%", "zroot": "40%"}),
    "production-shaped, --take-snapshots then --prune-snapshots, as the units run them": production(
        runs=[{"args": ["--take-snapshots", "--verbose"]}, {"args": ["--prune-snapshots", "--verbose"]}]),
    "production-shaped, a day of hourly runs": production(runs=[{"args": [], "at": k * H} for k in range(25)]),
    "hooks on a dataset section, hourlies due and over the count": {
        "conf": f"[tank/a]\n{COUNTS}{hooks()}", "datasets": A, "seed": series("tank/a", "hourly", H, 5, 4000)},
    "pre_snapshot_script fails": {
        "conf": f"[tank/a]\n{COUNTS}{hooks(pre=1)}", "datasets": A, "seed": series("tank/a", "hourly", H, 5, 4000)},
    "pre_snapshot_script fails, no_inconsistent_snapshot = yes": {
        "conf": f"[tank/a]\n{COUNTS}{hooks(pre=1)}\tno_inconsistent_snapshot = yes\n", "datasets": A,
        "seed": series("tank/a", "hourly", H, 5, 4000)},
    "pre hook fails, force_post_snapshot_script = yes, six types due": {
        "conf": f"[tank/a]\n{ALL2}{hooks(pre=1)}\tforce_post_snapshot_script = yes\n", "datasets": A, "seed": []},
    "pre_pruning_script vetoes every destroy": {
        "conf": f"[tank/a]\n{COUNTS}{hooks(veto=3)}", "datasets": A, "seed": series("tank/a", "hourly", H, 5, 4000)},
    "SANOID_SCRIPT and SANOID_TARGETS already in the environment": {
        "conf": f"[tank/a]\n{COUNTS}{hooks()}", "datasets": A, "seed": series("tank/a", "hourly", H, 5, 4000),
        "env": {"SANOID_SCRIPT": "leaked", "SANOID_TARGETS": "leaked"}},
    "the same, prune hooks only": {
        "conf": f"[tank/a]\n{COUNTS}\tpre_pruning_script = @HOOK@ pre_pruning 0\n\tpruning_script = @HOOK@ pruning 0\n",
        "datasets": A, "seed": series("tank/a", "hourly", H, 5, 1800), "env": {"SANOID_SCRIPT": "leaked"}},
    "recursive = zfs: one snapshot -r, the children pruned one by one, then a run on the cache 20 minutes on": {
        "conf": f"[tank/a]\n\trecursive = zfs\n{COUNTS}", "datasets": [*A, "tank/a/b", "tank/a/c"],
        "seed": [*series("tank/a", "hourly", H, 5, 4000), *series("tank/a/b", "hourly", H, 5, 4000),
                 *series("tank/a/c", "hourly", H, 5, 4000)], "runs": [{"args": []}, {"args": [], "at": 20 * 60}]},
    "recursive = zfs with process_children_only on the parent": {
        "conf": f"[tank/a]\n\trecursive = zfs\n\tprocess_children_only = yes\n{COUNTS}",
        "datasets": [*A, "tank/a/b"],
        "seed": [*series("tank/a", "hourly", H, 5, 1800), *series("tank/a/b", "hourly", H, 5, 1800)]},
    "recursive = yes with process_children_only, under a section whose listing holds the parent": {
        "conf": f"[tank]\n{COUNTS}[tank/a]\n\trecursive = yes\n\tprocess_children_only = yes\n{COUNTS}",
        "datasets": [*A, "tank/a/b"],
        "seed": [*series("tank/a", "hourly", H, 5, 1800), *series("tank/a/b", "hourly", H, 5, 1800)]},
    "recursive = yes, a child section below it overriding daily": {
        "conf": "[tank/a]\n\trecursive = yes\n\tdaily = 3\n\thourly = 0\n\tmonthly = 0\n[tank/a/b]\n\tdaily = 10\n",
        "datasets": [*A, "tank/a/b", "tank/a/c"],
        "seed": [*series("tank/a", "daily", D, 12, 5 * H), *series("tank/a/b", "daily", D, 12, 5 * H),
                 *series("tank/a/c", "daily", D, 12, 5 * H)]},
    "skip_children on the parent": {
        "conf": f"[tank/a]\n\tskip_children = yes\n{COUNTS}", "datasets": [*A, "tank/a/b"],
        "seed": series("tank/a/b", "hourly", H, 5, 1800)},
    "--debug alone: the dump and nothing else": production(runs=[{"args": ["--debug"]}]),
    "--cron --readonly --verbose, hooks set": {
        "conf": f"[tank/a]\n{COUNTS}{hooks()}", "datasets": A, "seed": series("tank/a", "hourly", H, 5, 1800),
        "runs": [{"args": ["--cron", "--readonly", "--verbose"]}]},
    "hourly = 0 with hourlies present": {
        "conf": "[tank/a]\n\thourly = 0\n\tdaily = 0\n\tmonthly = 0\n", "datasets": A,
        "seed": series("tank/a", "hourly", H, 5, 1800)},
    "autosnap on, every count 0, snapshots of every type present": {
        "conf": "[tank/a]\n\tfrequently = 0\n\thourly = 0\n\tdaily = 0\n\tweekly = 0\n\tmonthly = 0\n\tyearly = 0\n",
        "datasets": A, "seed": [*series("tank/a", "hourly", H, 3, 4000), *series("tank/a", "daily", D, 3, 5 * H),
                                *series("tank/a", "monthly", 31 * D, 3, 9 * D)]},
    "an empty pool: the first run takes one of each due type, the next one takes nothing": {
        "conf": f"[tank/a]\n{ALL2}", "datasets": A, "seed": [], "runs": [{"args": []}, {"args": [], "at": 60}]},
    "a sanoid.conf with templates and no dataset sections": {
        "conf": "[template_x]\n\thourly = 3\n", "datasets": A, "seed": series("tank/a", "hourly", H, 5, 4000)},
    "a section for a dataset zfs doesn't have: the listing's retry": {
        "conf": f"[tank/gone]\n{COUNTS}[tank/a]\n{COUNTS}", "datasets": A,
        "seed": series("tank/a", "hourly", H, 5, 4000)},
    "a dataset name holding a space": {
        "conf": f"[tank/my data]\n{COUNTS}", "datasets": ["tank", "tank/my data"],
        "seed": series("tank/my data", "hourly", H, 5, 4000)},
    "autosnap names without a type, and a snapshot that isn't pysanoid's": {
        "conf": f"[tank/a]\n{COUNTS}", "datasets": A, "seed": series("tank/a", "hourly", H, 5, 4000),
        "extra": [["tank/a@autosnapfoo", 1], ["tank/a@manual_hourly", 2], ["tank/a@autosnap_x_only", 3]]},
    "prune_defer on a pool below it, and on one above it": {
        "conf": f"[tank/a]\n{COUNTS}\tprune_defer = 60\n[pool2/b]\n{COUNTS}\tprune_defer = 60\n",
        "datasets": [*A, "pool2", "pool2/b"], "cap": {"tank": "40%", "pool2": "75%"},
        "seed": [*series("tank/a", "hourly", H, 5, 4000), *series("pool2/b", "hourly", H, 5, 4000)]},
    "a weekly on Saturday, a monthly on the 30th, two runs a day apart": {
        "conf": "[tank/a]\n\thourly = 0\n\tdaily = 0\n\tweekly = 3\n\tweekly_wday = 6\n\tmonthly = 3\n"
                "\tmonthly_mday = 30\n", "datasets": A,
        "seed": [*series("tank/a", "weekly", 7 * D, 6, 3 * D), *series("tank/a", "monthly", 31 * D, 6, 17 * D)],
        "runs": [{"args": []}, {"args": [], "at": D}]},
    "weekly_wday = 7 on a Wednesday, read alike": {
        "conf": "[tank/a]\n\thourly = 0\n\tdaily = 0\n\tweekly = 3\n\tweekly_wday = 7\n", "datasets": A,
        "seed": series("tank/a", "weekly", 7 * D, 6, 3 * D), "runs": [{"args": []}, {"args": [], "at": H}]},
    "KNOWN DEV-031: weekly_wday = 7 on a Sunday, two runs an hour apart": {
        "conf": "[tank/a]\n\thourly = 0\n\tdaily = 0\n\tweekly = 3\n\tweekly_wday = 7\n", "datasets": A,
        "seed": series("tank/a", "weekly", 7 * D, 6, 3 * D), "runs": [{"args": []}, {"args": [], "at": H}],
        "now": JUNE + 4 * D, "known": ["DEV-031"]},
    "KNOWN DEV-028: monthly_mday = 31 in June, a 30-day month": {
        "conf": "[tank/a]\n\thourly = 2\n\tdaily = 0\n\tmonthly = 3\n\tmonthly_mday = 31\n", "datasets": A,
        "seed": [*series("tank/a", "hourly", H, 5, 4000), *series("tank/a", "monthly", 31 * D, 6, 17 * D)],
        "bad_type": "monthly", "known": ["DEV-028-died"]},
    "KNOWN DEV-028: a snapshot that fails (the dataset is gone by the time it's taken)": {
        "conf": f"[tank/a]\n{COUNTS}[tank/b]\n{COUNTS}", "datasets": A, "seed": series("tank/a", "hourly", H, 5, 4000)},
    "KNOWN DEV-032: frequent_period a comment alone, --prune-snapshots, ten young frequentlies": {
        "conf": "[tank/a]\n\tfrequently = 4\n\tfrequent_period = # x\n\thourly = 0\n\tdaily = 0\n\tmonthly = 0\n",
        "datasets": A, "seed": series("tank/a", "frequently", 60, 10, 30), "runs": [{"args": ["--prune-snapshots"]}],
        "known": ["DEV-032"]},
    "KNOWN DEV-012: hooks on [backup] with path = tank/a": {
        "conf": f"[backup]\n\tpath = tank/a\n{COUNTS}{hooks()}", "datasets": A,
        "seed": series("tank/a", "hourly", H, 5, 1800), "known": ["DEV-012"]},
    "KNOWN DEV-017: a recursive section whose name holds a space": {
        "conf": f"[tank/my data]\n\trecursive = yes\n{COUNTS}", "datasets": ["tank", "tank/my data", "tank/my data/c"],
        "seed": [*series("tank/my data", "hourly", H, 5, 4000), *series("tank/my data/c", "hourly", H, 5, 4000)],
        "spaced": "tank/my data", "known": ["DEV-017"]},
    **{f"KNOWN DEV-050: sanoid.defaults.conf version = {v}": {
        "conf": "[tank/a]\n\thourly = 2\n", "datasets": A, "seed": [],
        "defaults": DEFAULTS.replace("version = 2", f"version = {v}"), "known": ["DEV-050"]}
       for v in ("2.5", "+2", "2x")},
    "sanoid.defaults.conf version = 1_0: both refuse it": {
        "conf": "[tank/a]\n\thourly = 2\n", "datasets": A, "seed": [],
        "defaults": DEFAULTS.replace("version = 2", "version = 1_0")},
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=40)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    if args.mode == "hand":
        cases = [(name, scn) for name, scn in HAND.items() if args.only in name]
    else:
        r = random.Random(args.seed)
        cases = [(f"fuzz #{i}", gen(r)) for i in range(args.n)]
    failed = 0
    known_seen: dict[str, int] = {}
    did = {"destroyed": 0, "made": 0, "hook calls": 0}
    for name, scn in cases:
        diffs, known, a, _ = compare(scn)
        did["destroyed"] += len(a["destroyed"])
        did["made"] += len(a["made"])
        did["hook calls"] += sum(map(len, a["hooks"].values()))
        expect_known = name.startswith("KNOWN")
        ok = not diffs and (bool(known) or not expect_known)
        failed += not ok
        for k in known:
            known_seen[k] = known_seen.get(k, 0) + 1
        if args.mode == "hand" or not ok:
            label = "KNOWN" if ok and known else "MATCH" if ok else "DIFF "
            print(f"[{label}] {name}" + (f"  ({', '.join(known)})" if known else "") + (f"  -- {diffs}" if diffs else ""))
            if not ok:
                _show(scn)
    print(f"{len(cases) - failed} of {len(cases)} pass; upstream's runs did {did}; known classes: {known_seen}")
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if failed else 0)


def _show(scn: dict[str, Any]) -> None:
    _, _, a, b = compare(scn)
    for k in ("destroyed", "made", "acts"):
        if a[k] != b[k]:
            print(f"    {k} only upstream: {sorted(set(a[k]) - set(b[k]))}\n    {k} only pysanoid: {sorted(set(b[k]) - set(a[k]))}")
    if a["hooks"] != b["hooks"]:
        print(f"    hooks upstream: {json.dumps(a['hooks'])}\n    hooks pysanoid: {json.dumps(b['hooks'])}")
    for side, res in (("upstream", a), ("pysanoid", b)):
        for i, r in enumerate(res["runs"]):
            print(f"    --- {side} run {i + 1}, exit {r['rc']}:\n" + "".join(f"      | {ln}\n" for ln in r["output"].splitlines()[-25:]))


FUZZ_TREE = ["tank", "tank/a", "tank/a/x", "tank/a/x/deep", "tank/a/y", "tank/b", "tank/c", "pool2", "pool2/d",
             "pool2/d/e"]
PERIOD = {"frequently": 15 * 60, "hourly": H, "daily": D, "weekly": 7 * D, "monthly": 31 * D, "yearly": 365 * D}
FLAGS = [[], ["--cron"], ["--cron", "--verbose"], ["--take-snapshots"], ["--prune-snapshots", "--verbose"],
         ["--take-snapshots", "--prune-snapshots"], ["--cron", "--readonly"]]


def _counts(r: random.Random) -> str:
    return "".join(f"\t{t} = {r.choice([0, 1, 2, 3, 5])}\n" for t in PERIOD if r.random() < 0.6)


def gen(r: random.Random) -> dict[str, Any]:
    """A config both programs read alike, on a pool seeded with series of each type, run one to four times. Kept
    clear of the registered differences the hand cases cover (see the module docstring): no `path =`, no space in a
    name, parents above children, no missing dataset under a recursive section, monthly and yearly on their default
    days, no two snapshots of one type made in the same second, and UTC."""
    templates = [f"t{i}" for i in range(r.randint(0, 2))]
    blocks = [f"[template_{t}]\n{_counts(r)}" for t in templates]
    chosen = sorted(r.sample(FUZZ_TREE[1:], r.randint(1, 5)), key=lambda d: d.count("/"))
    recursive: list[str] = []
    for ds in chosen:
        body = _counts(r)
        if templates and r.random() < 0.5:
            body += f"\tuse_template = {', '.join(r.sample(templates, r.randint(1, len(templates))))}\n"
        has_children = any(d.startswith(ds + "/") for d in FUZZ_TREE)
        if has_children and not any(ds.startswith(p + "/") for p in recursive) and r.random() < 0.4:
            kind = r.choice(["yes", "zfs", "skip"])
            body += "\tskip_children = yes\n" if kind == "skip" else f"\trecursive = {kind}\n"
            if kind != "skip":
                recursive.append(ds)
                if r.random() < 0.25:
                    body += "\tprocess_children_only = yes\n"
        if r.random() < 0.3:
            body += (f"\tpre_snapshot_script = @HOOK@ pre_snapshot {r.choice([0, 0, 1])}\n"
                     f"\tpost_snapshot_script = @HOOK@ post_snapshot 0\n")
            if r.random() < 0.3:
                body += f"\tno_inconsistent_snapshot = yes\n\tforce_post_snapshot_script = {r.choice(['yes', 'no'])}\n"
        if r.random() < 0.3:
            body += f"\tpre_pruning_script = @HOOK@ pre_pruning {r.choice([0, 0, 1])}\n\tpruning_script = @HOOK@ pruning 0\n"
        if r.random() < 0.2:
            body += f"\tprune_defer = {r.choice([0, 50, 80])}\n"
        if r.random() < 0.2:
            body += (f"\thourly_min = {r.randint(0, 59)}\n\tdaily_hour = {r.randint(0, 23)}\n"
                     f"\tdaily_min = {r.randint(0, 59)}\n\tweekly_wday = {r.randint(0, 6)}\n"
                     f"\tfrequent_period = {r.choice([5, 10, 15, 30])}\n")
        if r.random() < 0.5:
            body += "\tautosnap = no\n" if r.random() < 0.3 else "\tautoprune = no\n" if r.random() < 0.3 else ""
        blocks.append(f"[{ds}]\n{body}")
    if r.random() < 0.15:
        blocks.append(f"[tank/gone]\n{_counts(r)}")
    seed: list[tuple[str, str, int]] = []
    used: set[tuple[str, str, int]] = set()
    for ds in FUZZ_TREE:
        for typ, every in PERIOD.items():
            if r.random() < 0.35:
                newest = r.randint(60, every + every // 2)
                for age in (newest + k * every + r.randint(-30, 30) for k in range(r.randint(1, 10))):
                    if (ds, typ, age) not in used and age >= 60:
                        used.add((ds, typ, age))
                        seed.append((ds, typ, age))
    extra = [["tank/a@manual_hourly", JUNE - 9 * D]] if r.random() < 0.2 else []
    when = JUNE + r.randint(-150, 150) * D + r.randint(0, D)
    runs = [{"args": r.choice(FLAGS), "at": 0}]
    for _ in range(r.randint(0, 3)):
        runs.append({"args": r.choice(FLAGS), "at": runs[-1]["at"] + r.choice([15 * 60, H, 3 * H, D, 7 * D])})
    return {"conf": "".join(blocks), "datasets": FUZZ_TREE, "seed": seed, "extra": extra, "now": when,
            "cap": {"tank": f"{r.choice([30, 60, 90])}%", "pool2": f"{r.choice([30, 60, 90])}%"}, "runs": runs}


if __name__ == "__main__":
    main()
