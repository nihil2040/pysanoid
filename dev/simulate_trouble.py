#!/usr/bin/env python3
"""Trouble simulation: pysanoid as a whole program, run after run on a clock that moves, through the trouble a real
system has, with every run judged by what the config promises (lib/audit.py). dev/simulate_long_horizon.py calls
take and prune themselves, with no cache and no process to lose; here a run is pysanoid.main in a process of its
own, with its config, its caches and its locks on disk (lib/world.py), so what one run leaves is what the next
finds.

The trouble, drawn at random or scripted:
  fault    one event of a run fails, in any way it can: a file operation, a lock, a zfs or zpool command
  refused  zfs refuses one snapshot or one destroy
  killed   the process ends at an event, or at a destroy partway through a prune: what systemd does to a run that
           outlasts its unit's timeout
  ahead    the clock jumps forward: the machine was off and the timer makes up one run (Persistent=true), or the
           clock was set ahead. Half the jumps of nine hours or less are a clock set wrong for one round, with a
           failure in it, and set back in the next: what that round leaves in a cache is then dated ahead, and
           must not be read
  back     the clock is set back, which leaves the snapshots and caches made meanwhile dated in the future
  by hand  a second round a few minutes after a scheduled one, while the caches are fresh
  fuller   a pool's capacity changes, across what a prune_defer waits for

The clock is the system's, and a run is judged by it: the simulation can't tell a clock set wrong from time gone by,
and neither can pysanoid. Rounds come on the tick, up to two minutes late as the units' timer runs them, or always as
late, so that both passes through a repeated hour want the same name and the second takes its dst_ form; a round is
one --cron run, or a take run and then a prune run.

Every run is held to the auditor's first rule: nothing leaves main but its return, and no destroy breaks the
retention. A run nothing was done to, that reads the pool as it is, is held to all of them: exactly the snapshots
that are due, exactly the destroys the retention gives up, exit 0, nothing of its own on stderr, and both caches
left current and true. That is the check on the runs after trouble: whatever a run left behind, the next one that
reads the pool afresh must do exactly what a run on that pool should, and leave nothing wrong behind itself. A snapshot
dated more than five minutes ahead of the clock is set aside (DEV-051): it neither holds its type back nor
counts toward prune's count, and the exact rule says so. The clock set back makes them, and they are counted.
Every scenario closes with rounds free of trouble, past the life of both caches, and the last must be judged exactly.

Not here: hooks, a pool that goes away, a snapshot someone else destroys, two runs at once (dev/e2e_subprocess.py
starts take and prune together), and upstream: nothing is compared with Perl, which the other targets do run by run.

  python3 dev/simulate_trouble.py check             # dev/run.py's hand pass: the production-shaped config and the fixture, scripted
  python3 dev/simulate_trouble.py random [n] [seed] # its fuzz pass: n random configs (default 160), random trouble
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.audit import CATEGORIES, Auditor
from lib.model import (
    DAY,
    HOUR,
    config_paths,
    dataset_tree,
    model_sections,
    nest,
    set_aside,
)
from lib.parallel import fan_out
from lib.world import (
    TTL,
    ConfigRefused,
    Fault,
    World,
    clean_config,
    fresh_world,
    run_main,
)
from simulate_long_horizon import FIXTURE_CONF, FIXTURE_DATASETS

WORK = DEV_DIR / "work" / "trouble"
PRODUCTION_CONF = DEV_DIR / "fixtures" / "sanoid.conf"
ZONES = ("Europe/Dublin", "America/New_York", "Australia/Sydney", "Australia/Lord_Howe", "Asia/Kolkata", "UTC")
TROUBLE = ("fault", "refused", "killed", "ahead", "back", "by hand", "fuller")
WEIGHTS = (4, 2, 3, 1, 1, 2, 1)
AHEAD = (2 * HOUR, 9 * HOUR, 3 * DAY, 40 * DAY, 400 * DAY)
BACK = (90, 20 * 60, HOUR, 3 * HOUR, DAY, 8 * DAY)
CALM = 6  # rounds free of trouble at the end, after the caches' life has passed


@dataclass
class Scenario:
    name: str
    conf: str
    tree: list[str]
    caps: dict[str, int]
    tz: str
    start: int
    tick: int  # seconds between rounds
    split: bool
    rounds: int
    seed: int
    late: int | None = None  # seconds after the tick a round starts; None: 0 to 119, drawn for each
    script: dict[int, tuple[Any, ...] | list[tuple[Any, ...]]] | None = None  # round -> its trouble, in place of the dice


# ============================================================================
# The scenarios
# ============================================================================

def local(tz: str, when: str) -> int:
    return int(datetime.fromisoformat(when).replace(tzinfo=ZoneInfo(tz)).timestamp())


def stock(sections: list[Any], rng: random.Random, now: int, full: bool) -> dict[str, int]:
    """A pool with a past: up to three more than its count of each type a section keeps (`full`: three more), at ages
    around the type's period, and a snapshot or two that aren't sanoid's."""
    snaps: dict[str, int] = {}
    for sec in sections:
        for t in {**sec.prunes, **sec.takes}:
            period, most = sec.periods[t], sec.prunes.get(t, 0) + 3
            for i in range(most if full else rng.randrange(most + 1)):
                snaps[f"{sec.path}@autosnap_old{i}_{t}"] = now - int((i + rng.random()) * period) - 1
        if rng.random() < 0.5:
            snaps[f"{sec.path}@manual_keep"] = now - 400 * DAY
        if rng.random() < 0.3:
            snaps[f"{sec.path}@before_upgrade_daily"] = now - 40 * DAY
    return dict(sorted(snaps.items(), key=lambda item: item[1]))


def counts(rng: random.Random) -> list[str]:
    lines = [f"frequently = {rng.choice((0, 0, 0, 2, 4))}", f"hourly = {rng.choice((0, 2, 3, 6))}",
             f"daily = {rng.choice((0, 2, 3))}", f"weekly = {rng.choice((0, 0, 1, 2))}",
             f"monthly = {rng.choice((0, 0, 1, 2))}", f"yearly = {rng.choice((0, 0, 1))}"]
    return rng.sample(lines, rng.randrange(2, 7))


def schedule(rng: random.Random) -> list[str]:
    lines = [f"frequent_period = {rng.choice((5, 10, 15, 30))}", f"hourly_min = {rng.choice((0, 15, 30, 59))}",
             f"daily_hour = {rng.randrange(24)}", f"daily_min = {rng.choice((0, 30, 59))}",
             f"weekly_wday = {rng.randrange(7)}", f"weekly_hour = {rng.randrange(24)}", f"weekly_min = {rng.choice((0, 30))}",
             f"monthly_mday = {rng.randrange(1, 29)}", f"monthly_hour = {rng.randrange(24)}", f"monthly_min = {rng.choice((0, 30))}",
             f"yearly_mon = {rng.randrange(1, 13)}", f"yearly_mday = {rng.randrange(1, 29)}", f"yearly_hour = {rng.randrange(24)}"]
    return rng.sample(lines, rng.randrange(6))


def random_config(rng: random.Random) -> tuple[str, list[str], dict[str, int]]:
    """(config, datasets, capacities): one or two pools; sections that stand alone, recurse either way with or
    without a child section of their own, leave their own dataset or their children out, or go by another name; one to
    three templates; counts small enough for a few days to fill and empty them."""
    pools = rng.sample(["tank", "vault"], rng.randrange(1, 3))
    templates = [f"t{i}" for i in range(rng.randrange(1, 4))]
    out: list[str] = []
    datasets: list[str] = []
    for pool in pools:
        for leaf in rng.sample("abcde", rng.randrange(2, 5)):
            ds = f"{pool}/{leaf}"
            children = [f"{ds}/{c}" for c in rng.sample("xyz", rng.randrange(3))]
            datasets += [ds, *children, *(f"{c}/deep" for c in children if rng.random() < 0.3)]
            shape = rng.choice(("alone", "alone", "recursive", "zfs", "children only", "no children", "alias", "unmanaged"))
            if shape == "unmanaged":
                continue
            out += [f"[offsite_{pool}_{leaf}]", f"path = {ds}"] if shape == "alias" else [f"[{ds}]"]
            out.append(f"use_template = {rng.choice(templates)}")
            if rng.random() < 0.3:
                out += counts(rng)[:2]
            if shape in ("recursive", "children only"):
                out.append("recursive = yes")
            if shape == "zfs":
                out.append("recursive = zfs")
            if shape == "children only":
                out.append("process_children_only = yes")
            if shape == "no children":
                out.append("skip_children = yes")
            if shape == "alone" and rng.random() < 0.3:
                out.append(f"prune_defer = {rng.choice((50, 80))}")
            if shape in ("recursive", "children only") and children and rng.random() < 0.6:
                out += [f"[{children[0]}]", *counts(rng)[:3], *schedule(rng)[:2]]
    for t in templates:
        out += [f"[template_{t}]", *counts(rng), *schedule(rng),
                f"autosnap = {rng.choice(('yes', 'yes', 'yes', 'no'))}", f"autoprune = {rng.choice(('yes', 'yes', 'yes', 'no'))}"]
    return "\n".join(out) + "\n", dataset_tree(datasets), {pool: rng.choice((30, 60, 90)) for pool in pools}


def random_scenario(seed: int, n: int) -> Scenario:
    rng = random.Random(f"{seed}:{n}")
    conf, tree, caps = random_config(rng)
    tz = rng.choice(ZONES)
    zone = ZoneInfo(tz)
    start = int(datetime(2026, 10, 1, tzinfo=zone).timestamp()) + rng.randrange(800 * DAY)
    if rng.random() < 0.4:  # a day or two before a clock change, where the zone has one
        hour = int(datetime(rng.choice((2027, 2028)), 1, 1, tzinfo=zone).timestamp())
        offset = datetime.fromtimestamp(hour, zone).utcoffset()
        for _ in range(366 * 24):
            hour += HOUR
            if datetime.fromtimestamp(hour, zone).utcoffset() != offset:
                start = hour - rng.randrange(6 * HOUR, 2 * DAY)
                break
    tick = rng.choice((300, 900, 900, 1800, HOUR, HOUR))
    return Scenario(f"random {n} of seed {seed}", conf, tree, caps, tz, start - start % tick, tick, rng.random() < 0.5,
                    rng.randrange(90, 180), rng.randrange(1 << 30), rng.choice((None, 0, 37)))


def hand() -> list[Scenario]:
    """The production-shaped config through the night its clock goes back, and the long-horizon fixture through New York's,
    each with one of every trouble. The clock set ahead and then back leaves snapshots dated in the future, and a
    cache dated so that the round ahead left wrong: the production-shaped one's, by a prune killed after its first destroy; the fixture's,
    by a child lookup that listed nothing."""
    production_conf = PRODUCTION_CONF.read_text()
    production = Scenario("production", production_conf, dataset_tree([*config_paths(PRODUCTION_CONF), "tank/software/test"]),
                     {"tank": 78, "zroot": 11}, "Europe/Dublin", local("Europe/Dublin", "2026-10-23 00:00"), HOUR, True, 110, 1, 37,
                     {0: ("killed", ("zfs:destroy", 3), "kill_mid", 1), 5: ("refused", "snapshot", 0), 9: ("ahead", 9 * HOUR),
                      14: ("refused", "destroy", 0), 20: ("ahead", 3 * HOUR), 25: ("killed", ("zfs:destroy", 0), "kill_mid", 1),
                      26: ("back", 3 * HOUR), 34: ("fault", 8, 7),
                      41: ("by hand", 240), 47: ("killed", 12, "kill", 0), 60: ("fault", 5, 2), 70: ("back", 20 * 60),
                      80: ("killed", ("zfs:snapshot", 0), "kill_mid", 0)})
    fixture = Scenario("fixture", FIXTURE_CONF, dataset_tree([*config_paths_of(FIXTURE_CONF), *FIXTURE_DATASETS]),
                       {"tank": 50}, "America/New_York", local("America/New_York", "2026-10-31 20:00"), 900, False, 420, 2, 37,
                       {0: ("killed", ("zfs:destroy", 2), "kill", 0), 9: ("refused", "snapshot", 0), 15: ("by hand", 180),
                        22: ("fuller", "tank", 95), 30: ("refused", "destroy", 0), 40: ("ahead", 2 * DAY), 55: ("fault", 8, 5),
                        77: [("ahead", 5 * HOUR), ("refused", "list tank/rec", 0)], 78: ("back", 5 * HOUR),
                        120: ("killed", 9, "kill_mid", 0), 150: ("fault", 6, 11),
                        160: ("fuller", "tank", 50), 200: ("back", DAY), 330: ("ahead", 40 * DAY)})
    return [production, fixture]


def config_paths_of(conf: str) -> list[str]:
    path = WORK / "paths.conf"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(conf)
    return config_paths(path)


# ============================================================================
# The simulation
# ============================================================================

def simulate(sc: Scenario, wd: Path) -> dict[str, Any]:
    fresh_world(wd, sc.conf)
    world = World(wd, sc.tree, dict(sc.caps), sc.tz)
    try:
        sections, unchecked = model_sections(clean_config(world, sc.start))
    except ConfigRefused as e:
        return {"name": sc.name, "skipped": str(e)}
    if unchecked:
        return {"name": sc.name, "skipped": f"settings pysanoid refuses: {unchecked}"}
    rng = random.Random(sc.seed)
    world.snaps = stock(sections, rng, sc.start, sc.script is not None)
    if run_main(world, ["--readonly", "--take-snapshots"], sc.start).rc != 0:
        return {"name": sc.name, "skipped": "a first run with nothing done to it fails"}
    world.datasets_txt = (wd / "cache" / "datasets.txt").read_text()
    fresh_world(wd, sc.conf)

    audit = Auditor(world, sections)
    steps = [["--take-snapshots", "--verbose"], ["--prune-snapshots", "--verbose"]] if sc.split else [["--cron", "--verbose"]]
    tally: Counter[str] = Counter()
    clock = sc.start - 1
    events = 60  # how many events the last run had: where a fault can land in the next
    calm_from = sc.rounds - max(CALM, 2 * TTL // sc.tick + 3)
    last_exact = False
    took: list[str] = []  # scripted trouble that happened as scripted

    def round_at(now: int, tag: str, fault: Fault | None, fault_step: int) -> None:
        nonlocal events, last_exact
        tally["set aside, dated ahead (DEV-051)"] += len(set_aside(sections, nest(world.snaps), now))
        for step, argv in enumerate(steps):
            planned = fault if step == fault_step % len(steps) else None
            r = audit.run(f"{tag}{' prune' if step else ''}", argv, now + 5 * step, planned)
            events = max(len(r.events), 20)
            last_exact = r.exact
            tally["runs"] += 1
            tally["runs killed"] += r.killed
            tally["runs that exited 1"] += r.rc == 1
            tally["snapshots under a dst_ name"] += sum("dst_" in full for full, by_parent in r.created if not by_parent)
            if planned is not None and r.fired is not None:
                how = re.sub(r"(junk|cut)[0-9]+", r"\1", r.kind)
                tally[f"faults: {r.fired.split(':')[0].split('.')[0]} {how}"] += 1
                took.append(tag)

    pending: list[tuple[Any, ...]] = []  # what a round leaves for the next: the clock set back after a round ahead
    for n in range(sc.rounds):
        troubles, pending = pending, []
        if sc.script is not None:
            entry = sc.script.get(n, [])
            troubles = [entry] if isinstance(entry, tuple) else list(entry)
        elif not troubles and n < calm_from and rng.random() < 0.14:
            what = rng.choices(TROUBLE, WEIGHTS)[0]
            drawn: dict[str, tuple[Any, ...]] = {
                       "fault": ("fault", rng.randrange(events), rng.randrange(1 << 30)),
                       "refused": ("refused", rng.choice(("snapshot", "destroy")), rng.randrange(4)),
                       "killed": ("killed", rng.choice((rng.randrange(events), ("zfs:destroy", rng.randrange(5)))),
                                  rng.choice(("kill", "kill_mid")), rng.randrange(2)),
                       "ahead": ("ahead", rng.choice(AHEAD) + rng.randrange(HOUR)),
                       "back": ("back", rng.choice(BACK) + rng.randrange(60)),
                       "by hand": ("by hand", rng.randrange(60, 900)),
                       "fuller": ("fuller", rng.choice(sorted(world.caps)), rng.choice((20, 55, 85, 95)))}
            troubles = [drawn[what]]
            if what == "ahead" and troubles[0][1] < 10 * HOUR and rng.random() < 0.5:
                troubles.append(drawn[rng.choice(("fault", "refused", "killed"))])
                pending = [("back", troubles[0][1])]
        catch_up, by_hand = False, 0
        fault, fault_step = None, 0
        for trouble in troubles:
            kind = trouble[0]
            tally[f"trouble: {kind}"] += 1
            if kind == "ahead":
                clock += trouble[1]
                catch_up = True
            elif kind == "back":
                clock -= trouble[1]
            elif kind == "fuller":
                world.caps[trouble[1]] = trouble[2]
            elif kind == "by hand":
                by_hand = int(trouble[1])
            elif kind == "fault":
                fault, fault_step = Fault(trouble[1], "any", trouble[2]), trouble[2]
            elif kind == "refused":
                fault, fault_step = Fault((f"zfs:{trouble[1]}", trouble[2]), "rc1"), int(trouble[1] == "destroy")
            elif kind == "killed":
                target: Any = trouble[1]  # an event's index, or a destroy partway through the prune
                fault, fault_step = Fault(target, trouble[2]), 1 if isinstance(target, tuple) and "destroy" in target[0] else trouble[3]
        late = rng.randrange(120)
        now = clock + 1 if catch_up else clock - clock % sc.tick + sc.tick + (late if sc.late is None else sc.late)
        kinds = [trouble[0] for trouble in troubles]
        stamp = f"round {n} ({datetime.fromtimestamp(now, audit.tz):%Y-%m-%d %H:%M:%S}{', ' + ' and '.join(kinds) if kinds else ''})"
        round_at(now, stamp, fault, fault_step)
        clock = now
        if by_hand:
            clock = now + by_hand
            round_at(clock, f"{stamp}, {by_hand} s later", None, 0)
        if sc.script is not None:
            took += [stamp for kind in kinds if kind in ("ahead", "back", "by hand", "fuller")]

    scripted = sum(1 if isinstance(entry, tuple) else len(entry) for entry in (sc.script or {}).values())
    if not last_exact:
        audit.fail("harness", "the last round", "not judged exactly: the scenario ends before its trouble is over")
    if sc.script is not None and len(took) != scripted:
        audit.fail("harness", "the script", f"{scripted - len(took)} of its {scripted} troubles did not happen as scripted")
    if sc.script is not None and not tally["set aside, dated ahead (DEV-051)"]:
        audit.fail("harness", "the script", "the clock set back left no snapshot dated ahead to set aside")
    if sc.script is not None and not tally["snapshots under a dst_ name"]:
        audit.fail("harness", "the script", "the repeated hour gave no snapshot a dst_ name")
    tally["runs judged exactly"] = audit.exact
    return {"name": sc.name, "findings": audit.findings, "tally": tally, "sections": len(sections),
            "what": f"{sc.tz}, every {sc.tick // 60} min, {'take and prune apart' if sc.split else '--cron'}, {sc.rounds} rounds "
                    f"from {datetime.fromtimestamp(sc.start, audit.tz):%Y-%m-%d %H:%M}", "conf": sc.conf}


SCENARIOS: list[Scenario] = []


def one(n: int) -> dict[str, Any]:
    return simulate(SCENARIOS[n], WORK / f"w{os.getpid()}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("check", "random"))
    ap.add_argument("n", nargs="?", type=int, default=160, help="random: how many scenarios (default 160)")
    ap.add_argument("seed", nargs="?", type=int, default=1)
    ap.add_argument("--show", type=int, default=6, help="how many findings of a kind to print for a scenario")
    args = ap.parse_args()
    SCENARIOS[:] = hand() if args.mode == "check" else [random_scenario(args.seed, n) for n in range(args.n)]
    t0 = time.monotonic()
    results = fan_out(list(range(len(SCENARIOS))), one)
    total: Counter[str] = Counter()
    failed = skipped = 0
    for res in results:
        if "skipped" in res:
            skipped += 1
            continue
        total.update(res["tally"])
        bad = {c: items for c, items in res["findings"].items() if items}
        failed += bool(bad)
        if bad or args.mode == "check":
            t = res["tally"]
            print(f"{'=' * 78}\n{res['name']}: {res['what']}, {res['sections']} sections\n{'=' * 78}")
            print(f"runs: {t['runs']}, {t['runs judged exactly']} of them judged exactly | killed {t['runs killed']} | exited 1: "
                  f"{t['runs that exited 1']} | set aside, dated ahead (DEV-051): {t['set aside, dated ahead (DEV-051)']} | "
                  f"dst_ names: {t['snapshots under a dst_ name']}")
            print("failed checks: " + " | ".join(f"{c} {len(res['findings'][c])}" for c in CATEGORIES))
            for category, items in bad.items():
                print(f"\n--- {category} (first {min(args.show, len(items))} of {len(items)}) ---")
                for item in items[:args.show]:
                    print(f"  {item}")
            if bad and args.mode == "random":
                print(f"\n{res['conf']}")
            print("PASS" if not bad else "FAIL")
    print(f"{'=' * 78}\n{len(results) - skipped} scenarios" + (f" ({skipped} more drawn and refused by pysanoid)" if skipped else "")
          + f", {total['runs']} runs, {total['runs judged exactly']} of them judged exactly")
    for key in sorted(k for k in total if k.startswith(("trouble: ", "faults: ", "held back", "runs killed", "runs that", "snapshots"))):
        print(f"  {total[key]:6d}  {key}")
    if args.mode == "random" and (skipped > len(results) // 2 or not total["runs killed"] or not total["trouble: back"]):
        failed += 1
        print("the scenarios drawn don't cover the trouble: too many refused, or no run killed, or no clock set back")
    print(f"{'PASS' if not failed else 'FAIL'} ({time.monotonic() - t0:.0f} s)")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
