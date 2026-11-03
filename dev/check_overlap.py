#!/usr/bin/env python3
"""Two runs at once: pysanoid twice as programs, started a moment apart on one pool that changes (dev/fake_pool), with
one cache directory and one run directory between them, as two overlapping runs on one host would share them. The pool
is slowed ($FAKE_POOL_DELAY), and its listing more ($FAKE_POOL_GET_DELAY: a run holds its cache lock while it lists),
so their steps interleave: one run's listing under the lock spans the other's destroys, cache removals and adds. No
upstream: its locks ask `ps`, which the sandbox forbids, so pysanoid is judged by rules stated here, and against the
same two runs made one after the other.

The rules, for every section that prunes, each type it keeps (counts read as pysanoid loads them):
  - floor: no type ends with fewer snapshots than its count, or than it had, whichever is fewer;
  - window: nothing is destroyed inside its age window (count x period before the later run's clock);
  - serial: nothing is destroyed that the same two runs one after the other wouldn't destroy;
  - cache: once both have finished, the snapshot cache lists exactly the pool's snapshots of the datasets pysanoid
    lists (each section's path and what lies under it; the whole pool when one isn't there, and the listing is
    retried without names);
  - and each run ends, without a traceback.
Two runs at once can each take a snapshot of a type the other already took (neither takes a lock to take, as
upstream's take doesn't): counted, not failed.

Each scenario runs at several start offsets (the second run 0 to 1.2 s after the first), each on a fresh pool.
Clocks are frozen (lib/run_frozen.py), the second run's a few seconds after the first's, as two real runs' are; a
cache's file date is the real one, so to each run a cache the other just wrote is dated ahead, and it lists zfs (under
the lock) rather than read it (DEV-037).

  python3 dev/check_overlap.py check      # dev/run.py's hand pass: the scenarios below, every offset
  python3 dev/check_overlap.py fuzz [n] [seed]   # random configs (dev/diff_whole_run.py's generator), random pairs
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
import diff_whole_run as w

import pysanoid as ps

WORK = DEV_DIR / "work" / "overlap"
RUN_FROZEN = DEV_DIR / "lib" / "run_frozen.py"
OFFSETS = (0.0, 0.3, 0.7, 1.2)
DELAY, GET_DELAY = "0.02", "0.5"
H, D = w.H, w.D


def _pool_file(wd: Path, scn: dict[str, Any]) -> tuple[Path, dict[str, int]]:
    now, tz = scn.get("now", w.JUNE), scn.get("tz", "UTC")
    snaps = sorted([[f"{d}@{w.snap_name(now - age, t, tz)}", now - age] for d, t, age in scn["seed"]]
                   + [list(x) for x in scn.get("extra", [])], key=lambda s: s[1])
    pool = wd / "pool.json"
    pool.write_text(json.dumps({"datasets": scn["datasets"], "snaps": snaps, "cap": scn.get("cap", {})}))
    return pool, {s[0]: s[1] for s in snaps}


def _env(wd: Path, pool: Path, scn: dict[str, Any]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SANOID_")}
    env.update(PATH=os.pathsep.join([str(wd / "bin"), str(DEV_DIR / "fake_pool"), "/usr/bin", "/bin"]),
               FAKE_POOL=str(pool), FAKE_POOL_DELAY=DELAY, FAKE_POOL_GET_DELAY=GET_DELAY, TZ=scn.get("tz", "UTC"), PYTHONDONTWRITEBYTECODE="1")
    return env


def run_pair(scn: dict[str, Any], offset: float | None) -> dict[str, Any]:
    """Both runs, at once with the second `offset` seconds after the first, or one after the other when offset is
    None. What the pool held before and after, and what each run printed."""
    wd = WORK / f"{'serial' if offset is None else 'overlap'}-{os.getpid()}"
    shutil.rmtree(wd, ignore_errors=True)
    (wd / "conf").mkdir(parents=True)
    w._bin(wd)
    (wd / "conf" / "sanoid.conf").write_text(scn["conf"].replace("@HOOK@", str(w.HOOK)))
    (wd / "conf" / "sanoid.defaults.conf").write_text(w.DEFAULTS)
    pool, before = _pool_file(wd, scn)
    env = _env(wd, pool, scn)
    now = scn.get("now", w.JUNE)
    dirs = ["--configdir", str(wd / "conf"), "--cache-dir", str(wd / "cache"), "--run-dir", str(wd / "run")]
    for step in scn.get("before", []):  # runs made first, one at a time (a warm cache, say)
        _ = subprocess.run([sys.executable, str(RUN_FROZEN), *dirs, *step["args"]], cwd=wd, capture_output=True,
                           env={**env, "FAKE_NOW": str(now + step["at"]), "HOOK_LOG": str(wd / "hooks.log")},
                           timeout=120, check=False)
        w._date_caches(wd, now + step["at"])
    procs: list[subprocess.Popen[bytes]] = []
    outputs: list[tuple[int, str]] = []
    for i, step in enumerate(scn["pair"]):
        if i and offset is not None:
            time.sleep(offset)
        proc = subprocess.Popen([sys.executable, str(RUN_FROZEN), *dirs, *step["args"]], cwd=wd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                env={**env, "FAKE_NOW": str(now + step["at"]), "HOOK_LOG": str(wd / f"hooks-{i}.log")})
        if offset is None:
            out, _ = proc.communicate(timeout=120)
            outputs.append((proc.returncode, out.decode("utf-8", "backslashreplace")))
        else:
            procs.append(proc)
    for proc in procs:
        out, _ = proc.communicate(timeout=120)
        outputs.append((proc.returncode, out.decode("utf-8", "backslashreplace")))
    after = {s[0]: s[1] for s in json.loads(pool.read_text())["snaps"]}
    cache = wd / "cache" / "snapshots.txt"
    cached = {line.split("\t", 1)[0] for line in cache.read_text().splitlines()} if cache.is_file() else None
    result = {"before": before, "after": after, "destroyed": sorted(set(before) - set(after)),
              "made": sorted(set(after) - set(before)), "outputs": outputs, "cached": cached,
              "clocks": [now + s["at"] for s in scn["pair"]]}
    shutil.rmtree(wd, ignore_errors=True)
    return result


Counts = dict[tuple[str, str], tuple[int, float]]


def kept_counts(scn: dict[str, Any]) -> tuple[Counts, list[str] | None] | None:
    """({(dataset, type): (count, period)} for every section that prunes, and the datasets the listing names, or None
    for all of them), read as pysanoid loads the config against the scenario's own pool; None when it refuses it."""
    wd = WORK / f"load-{os.getpid()}"
    shutil.rmtree(wd, ignore_errors=True)
    (wd / "conf").mkdir(parents=True)
    w._bin(wd)
    (wd / "conf" / "sanoid.conf").write_text(scn["conf"].replace("@HOOK@", str(w.HOOK)))
    (wd / "conf" / "sanoid.defaults.conf").write_text(w.DEFAULTS)
    pool, _ = _pool_file(wd, scn)
    saved = dict(os.environ)
    os.environ.update(_env(wd, pool, scn))
    zfs = ps.ZFS
    ps.ZFS = str(DEV_DIR / "fake_pool" / "zfs")
    try:
        config = ps.load_config(wd / "conf", ps.ZfsClient(wd / "cache", 1200, True))
        listed = ps.get_active_dataset_paths(config)
        roots = listed if set(listed) <= set(scn["datasets"]) else None
        counts: Counts = {}
        for section, cfg in config.items():
            if section.startswith("template") or not cfg.get("autoprune") or cfg.get("process_children_only"):
                continue
            for t in ps.PERIOD_TYPES:
                if t in cfg:
                    period = 60 * ps._cfg_int(cfg, "frequent_period", 15) if t == "frequently" else ps._PERIOD_SECONDS[t]
                    counts[(cfg["path"], t)] = (ps._cfg_count(cfg, t, 0), float(period or 0))
        return counts, roots
    except ps.SanoidError:
        return None
    finally:
        os.environ.clear()
        os.environ.update(saved)
        ps.ZFS = zfs
        shutil.rmtree(wd, ignore_errors=True)


def _typed(names: Any) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    for full in names:
        dataset, name = full.split("@", 1)
        t = ps._snapshot_type(name) if name.startswith("autosnap") else None
        if t:
            out.setdefault((dataset, t), []).append(full)
    return out


def judge(scn: dict[str, Any], over: dict[str, Any], serial: dict[str, Any],
          loaded: tuple[Counts, list[str] | None] | None) -> tuple[list[str], int]:
    """(what broke a rule, how many types were taken twice)."""
    broken: list[str] = []
    for rc, out in over["outputs"]:
        if "Traceback" in out or rc not in (0, 1):
            broken.append(f"a run ended with exit {rc}" + (" and a traceback" if "Traceback" in out else ""))
    counts, roots = loaded if loaded is not None else (None, None)
    if counts is not None:
        before, after = _typed(over["before"]), _typed(over["after"])
        latest = max(over["clocks"])
        for (dataset, t), (count, period) in counts.items():
            had, has = len(before.get((dataset, t), [])), len(after.get((dataset, t), []))
            if has < min(count, had):
                broken.append(f"floor: {dataset} {t} kept {has} of {had}, count {count}")
        for full in over["destroyed"]:
            dataset, name = full.split("@", 1)
            t = ps._snapshot_type(name)
            count, period = counts.get((dataset, t or ""), (None, 0.0))
            if count is None:
                broken.append(f"window: destroyed {full}, which no section prunes")
            elif not over["before"][full] < latest - count * period:
                broken.append(f"window: destroyed {full} inside its window")
    extra = sorted(set(over["destroyed"]) - set(serial["destroyed"]))
    if extra:
        broken.append(f"serial: destroyed {extra[:3]}{' ...' if len(extra) > 3 else ''}, which one after the other didn't")
    listed = {full for full in over["after"] if roots is None
              or any(full.split("@")[0] == r or full.startswith(r + "/") for r in roots)}
    if loaded is not None and over["cached"] is not None and over["cached"] != listed:
        stale = sorted(over["cached"] - listed)[:3]
        missing = sorted(listed - over["cached"])[:3]
        broken.append(f"cache: lists {stale} the pool hasn't, lacks {missing}")
    twice = sum(len({n.rsplit("_", 1)[0][-19:] for n in names}) > 1
                for names in _typed(over["made"]).values())
    return broken, twice


def check(name: str, scn: dict[str, Any], offsets: tuple[float, ...]) -> tuple[list[str], dict[str, int]]:
    loaded = kept_counts(scn)
    serial = run_pair(scn, None)
    failures: list[str] = []
    stats = {"destroyed": 0, "made": 0, "taken twice": 0}
    for offset in offsets:
        over = run_pair(scn, offset)
        broken, twice = judge(scn, over, serial, loaded)
        stats["destroyed"] += len(over["destroyed"])
        stats["made"] += len(over["made"])
        stats["taken twice"] += twice
        failures += [f"{name}, second run {offset:.2f} s later: {b}" for b in broken]
        if broken:
            for i, (rc, out) in enumerate(over["outputs"]):
                print(f"    --- run {i + 1}, exit {rc}:\n" + "".join(f"      | {ln}\n" for ln in out.splitlines()[-20:]))
    return failures, stats


def series(ds: str, typ: str, every: int, count: int, first: int) -> list[tuple[str, str, int]]:
    return w.series(ds, typ, every, count, first)


COUNTS = "\tfrequently = 0\n\thourly = 3\n\tdaily = 3\n\tweekly = 0\n\tmonthly = 0\n\tyearly = 0\n"
TREE = ["tank", "tank/a", "tank/b", "tank/c", "tank/c/x", "tank/c/y"]
# every hourly and daily due (the newest of each older than its last preferred time), and more of each than kept
SEED = [s for ds in TREE[1:] for s in (*series(ds, "hourly", H, 8, 4000), *series(ds, "daily", D, 8, D + 5 * H))]
CRON, PRUNE, TAKE = ["--cron"], ["--prune-snapshots", "--verbose"], ["--take-snapshots", "--verbose"]


def S(conf: str, pair: list[list[str]], *, gap: int = 7, before: list[dict[str, Any]] | None = None,
      **over: Any) -> dict[str, Any]:
    return {"conf": conf, "datasets": TREE, "seed": SEED, "cap": {"tank": "60%"},
            "pair": [{"args": pair[0], "at": 0}, {"args": pair[1], "at": gap}], "before": before or [], **over}


PLAIN = "".join(f"[{ds}]\n{COUNTS}" for ds in TREE[1:])
HAND: dict[str, dict[str, Any]] = {
    "two prunes at once, each type over its count": S(PLAIN, [PRUNE, PRUNE]),
    "two cron runs at once, no cache yet": S(PLAIN, [CRON, CRON]),
    "two cron runs at once on a warm cache": S(PLAIN, [CRON, CRON], before=[{"args": CRON, "at": -20 * 60}]),
    "take and prune at once, as the units would if one outlasted the hour": S(PLAIN, [TAKE, PRUNE]),
    "prune and take at once, the other way round": S(PLAIN, [PRUNE, TAKE]),
    "two cron runs an hour apart in their clocks, overlapping": S(PLAIN, [CRON, CRON], gap=H),
    "a recursive section and a zfs-recursive one": S(
        f"[tank/a]\n{COUNTS}[tank/c]\n\trecursive = yes\n{COUNTS}[tank/b]\n\trecursive = zfs\n{COUNTS}", [CRON, CRON]),
    "a prune of one batch while the other run lists: its only flush is its last": {
        "conf": f"[tank/a]\n{COUNTS}\tdaily = 0\n[tank/b]\n\tautoprune = no\n{COUNTS}", "datasets": TREE,
        "seed": [*series("tank/a", "hourly", H, 12, 4000), *series("tank/b", "hourly", H, 3, 4000)], "cap": {},
        "pair": [{"args": PRUNE, "at": 0}, {"args": CRON, "at": 3}], "before": [],
        "offsets": (0.2, 0.35, 0.5, 0.65, 0.8, 0.95, 1.1)},
    "prune_defer on a pool above it": S(f"[tank/a]\n{COUNTS}\tprune_defer = 50\n[tank/b]\n{COUNTS}", [PRUNE, PRUNE]),
    "the production-shaped config, two cron runs at once": {
        "conf": w.PRODUCTION_CONF, "datasets": w.PRODUCTION_DS, "seed": w.PRODUCTION_SEED, "cap": {"tank": "90%", "zroot": "40%"},
        "tz": "Europe/Dublin", "pair": [{"args": CRON, "at": 0}, {"args": CRON, "at": 7}], "before": []},
    "the production-shaped config, its take unit and prune unit at once": {
        "conf": w.PRODUCTION_CONF, "datasets": w.PRODUCTION_DS, "seed": w.PRODUCTION_SEED, "cap": {"tank": "90%", "zroot": "40%"},
        "tz": "Europe/Dublin", "pair": [{"args": TAKE, "at": 0}, {"args": PRUNE, "at": 3}], "before": []},
}


def gen(r: random.Random) -> dict[str, Any]:
    scn = w.gen(r)
    flags = [CRON, CRON, PRUNE, TAKE, ["--take-snapshots", "--prune-snapshots"]]
    scn["pair"] = [{"args": r.choice(flags), "at": 0}, {"args": r.choice(flags), "at": r.choice([0, 3, 7, 60, H])}]
    scn["before"] = [{"args": CRON, "at": -r.choice([20 * 60, H])}] if r.random() < 0.4 else []
    return scn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["check", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=12)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()
    if args.mode == "check":
        cases = [(name, scn, scn.get("offsets", OFFSETS)) for name, scn in HAND.items()]
    else:
        r = random.Random(args.seed)
        cases = [(f"fuzz #{i}", gen(r), (r.choice(OFFSETS), r.choice(OFFSETS))) for i in range(args.n)]
    failures: list[str] = []
    totals = {"destroyed": 0, "made": 0, "taken twice": 0}
    for name, scn, offsets in cases:
        found, stats = check(name, scn, offsets)
        failures += found
        for k, v in stats.items():
            totals[k] += v
        print(f"[{'FAIL' if found else 'ok  '}] {name}  {stats}", flush=True)
        for f in found:
            print(f"    {f}")
    print(f"{len(cases) - len({f.split(',')[0] for f in failures})} of {len(cases)} hold every rule; the overlapping "
          f"runs did {totals}")
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
