#!/usr/bin/env python3
"""Config fuzz: sanoid.conf files with odd values in every setting, through pysanoid as a whole program in every
mode, on a pool held in memory (lib/world.py). It is the check that no config, however wrong, makes a run crash or
destroy what the config it loaded keeps.

A case is a random config (counts and schedule settings from a list of numbers a person or an editor might leave
there: blank, signed, spaced, 4,400 digits long, other scripts' digits; toggles likewise; templates, both kinds of
recursion, `path =`, hooks, an unknown key, a repeated key), a random pool, a time zone and a date, some of them an
hour either side of a clock change, on the last day of a month or on the first of a year. Six runs follow on the
same pool: --cron --readonly, --prune-snapshots --readonly, --debug --readonly, --cron, --take-snapshots,
--prune-snapshots.

Checked for every run:
  - nothing leaves main but its return, and the exit status is 0 or 1;
  - --readonly changes nothing;
  - every destroy is one the config allows as pysanoid itself loads it, judged by lib/model.py against the pool:
    a snapshot sanoid owns, of a type a section prunes, older than count x period, leaving count of its type;
  - a config pysanoid refuses changes nothing in any mode, and every run exits 1;
  - where prune refuses a setting of any section it would prune, no run destroys anything and each prune exits 1:
    upstream dies at that section, after the ones before it were pruned;
  - a run that exits 0 has nothing on stderr but zfs's own words and upstream's warnings (lib/audit.py);
  - a hand case's runs exit, take and destroy as they did when the case was written.

Whether a value is one prune refuses is asked of pysanoid's own preflight: what this checks is that a refusal comes
before the first destroy, not how a value reads, which dev/diff_prune.py and dev/diff_perl_number.py compare with
upstream's.

  python3 dev/fuzz_configs.py check            # dev/run.py's hand pass: the hand cases and 300 random ones
  python3 dev/fuzz_configs.py fuzz [n] [seed]  # its fuzz pass: n random cases (default 4000, seed 1)
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.audit import CATEGORIES, Auditor
from lib.model import DAY, PERIODS, model_sections
from lib.parallel import fan_out
from lib.world import ConfigRefused, World, clean_config, fresh_world

import pysanoid as p

WORK = DEV_DIR / "work" / "config_fuzz"
TREE = ["big", "big/x", "tank", "tank/a", "tank/a/x", "tank/b", "tank/b/c", "tank/b/c/d"]
CAPS = {"big": 60, "tank": 60}
ZONES = ("Europe/Dublin", "America/New_York", "Australia/Lord_Howe", "Asia/Kolkata", "UTC")
RUNS = (["--cron", "--readonly"], ["--prune-snapshots", "--readonly"], ["--debug", "--readonly"], ["--cron"], ["--take-snapshots"],
        ["--prune-snapshots"])
NUMBERS = ["0", "1", "2", "3", "7", "30", "59", "60", "61", "-1", "+3", "3o", "1_0", "", " ", "abc", "1.5", "1e3", "0x10",
           "9" * 25, "9" * 320, "9" * 4400, "００７", "7 # a week", "# off", "inf", "nan", "-0", "00", "007", "1 2", "٣", "1\t",
           "2147483648", "4294967297", "-9" * 3, "1e400", ".5", "5.", "0.0", "31", "32", "12", "13", "24", "23", "29", "366"]
TOGGLES = ["yes", "no", "1", "0", "true", "false", "on", "off", "", "YES", "No", "maybe", "yes ", "zfs", "y"]
SCHEDULE = ["hourly_min", "daily_hour", "daily_min", "weekly_wday", "weekly_hour", "weekly_min", "monthly_mday", "monthly_hour",
            "monthly_min", "yearly_mon", "yearly_mday", "yearly_hour", "yearly_min", "frequent_period", "script_timeout"]
TYPES = ["frequently", "hourly", "daily", "weekly", "monthly", "yearly"]
HOOKS = ["pre_snapshot_script", "post_snapshot_script", "pre_pruning_script", "pruning_script"]
EXTRA = ("refused config", "refused setting", "hand case")

# Configs that have gone wrong before. (name, config, zone, local time, what its six runs do: each one's exit status,
# snapshots taken and snapshots destroyed). The last is what 0.1.45 does, read through and found right: a change in it
# is to be looked at, not assumed wrong.
HAND: list[tuple[str, str, str, str, str]] = [
    ("a yearly count of 320 digits, below a section that prunes",
     "[tank/a]\nhourly = 1\ndaily = 1\n[tank/b]\nyearly = " + "9" * 320 + "\n", "Europe/Dublin", "2026-10-11 10:30",
     "0/0/0 0/0/0 0/0/0 0/3/20 0/0/0 0/0/0"),
    ("a daily count of 4,400 digits", "[tank/a]\ndaily = 1\n[tank/b]\ndaily = " + "9" * 4400 + "\n", "UTC", "2026-10-11 10:30",
     "0/0/0 0/0/0 0/0/0 0/2/18 0/0/0 0/0/0"),
    ("a prune_defer of 4,400 digits", "[tank/a]\ndaily = 1\n[big/x]\nprune_defer = " + "9" * 4400 + "\n", "UTC", "2026-10-11 10:30",
     "1/0/0 1/0/0 0/0/0 1/3/0 0/0/0 1/0/0"),
    ("a count prune refuses, below a section that prunes", "[tank/a]\nhourly = 1\ndaily = 1\n[tank/b]\ndaily = 3o\n",
     "UTC", "2026-10-11 10:30",
     "1/0/0 1/0/0 0/0/0 1/3/0 1/0/0 1/0/0"),
    ("a blank and a negative count", "[tank/a]\nhourly =\ndaily = -1\nmonthly = 1\n", "UTC", "2026-10-11 10:30",
     "1/0/0 1/0/0 0/0/0 1/0/0 1/0/0 1/0/0"),
    ("the 31st, in a month of 30 days", "[tank/a]\nmonthly = 2\nmonthly_mday = 31\nhourly = 2\n", "America/New_York", "2027-04-30 12:00",
     "1/0/0 0/0/0 0/0/0 1/2/8 1/0/0 0/0/0"),
    ("29 February, in a year without one", "[tank/a]\nyearly = 2\nyearly_mon = 2\nyearly_mday = 29\ndaily = 2\n", "UTC", "2027-03-01 00:10",
     "1/0/0 0/0/0 0/0/0 1/3/12 1/0/0 0/0/0"),
    ("a minute, an hour and a period out of range",
     "[tank/a]\nfrequently = 2\nfrequent_period = 0\nhourly = 2\nhourly_min = 75\ndaily = 2\ndaily_hour = 24\n", "UTC", "2026-10-11 10:30",
     "1/0/0 0/0/0 0/0/0 1/1/11 1/0/0 0/0/0"),
    ("a template that isn't there", "[tank/a]\nuse_template = production\n", "UTC", "2026-10-11 10:30",
     "1/0/0 1/0/0 1/0/0 1/0/0 1/0/0 1/0/0"),
    ("a child's section above its recursive parent",
     "[tank/b/c]\ndaily = 9\n[tank/b]\nrecursive = yes\ndaily = 1\n", "UTC", "2026-10-11 10:30",
     "1/0/0 1/0/0 1/0/0 1/0/0 1/0/0 1/0/0"),
    ("two sections for one dataset", "[tank/a]\ndaily = 1\n[offsite]\npath = tank/a\ndaily = 9\n", "UTC", "2026-10-11 10:30",
     "1/0/0 1/0/0 1/0/0 1/0/0 1/0/0 1/0/0"),
    ("an unknown key", "[tank/a]\ndaily = 1\nretain = 5\n", "UTC", "2026-10-11 10:30",
     "1/0/0 1/0/0 1/0/0 1/0/0 1/0/0 1/0/0"),
    ("a pool that isn't there, deferring", "[tank/a]\ndaily = 1\n[gone/x]\ndaily = 1\nprune_defer = 50\n[tank/b]\ndaily = 1\n",
     "UTC", "2026-10-11 10:30",
     "1/0/0 1/0/0 0/0/0 1/1/8 1/0/0 1/0/0"),
    ("hooks that fail, and a snapshot they hold back",
     ("[tank/a]\ndaily = 1\nhourly = 1\npre_snapshot_script = false\nno_inconsistent_snapshot = yes\n"
      "[tank/b]\ndaily = 1\npre_pruning_script = false\n[big/x]\ndaily = 1\npruning_script = false\npost_snapshot_script = false\n"),
     "UTC", "2026-10-11 10:30",
     "0/0/0 0/0/0 0/0/0 1/4/25 1/0/0 0/0/0"),
    ("both kinds of recursion, the second pass of a repeated hour",
     "[tank/a]\nrecursive = zfs\nhourly = 2\nfrequently = 3\n[tank/b]\nrecursive = yes\nhourly = 2\n[tank/b/c]\nhourly = 5\n",
     "America/New_York", "2026-11-01 01:30 fold",
     "0/0/0 0/0/0 0/0/0 0/15/44 0/0/0 0/0/0"),
]


def body(rng: random.Random, *, template: bool) -> list[str]:
    lines = []
    for key in TYPES:
        if rng.random() < 0.5:
            lines.append(f"{key} = {rng.choice(NUMBERS) if rng.random() < 0.3 else rng.choice(['0', '1', '2', '3', '5'])}")
    for key in SCHEDULE:
        if rng.random() < 0.1:
            lines.append(f"{key} = {rng.choice(NUMBERS)}")
    for key in ("autosnap", "autoprune", "process_children_only", "no_inconsistent_snapshot", "force_post_snapshot_script"):
        if rng.random() < 0.25:
            lines.append(f"{key} = {rng.choice(TOGGLES) if rng.random() < 0.3 else rng.choice(['yes', 'no'])}")
    if rng.random() < 0.15:
        lines.append(f"prune_defer = {rng.choice(NUMBERS) if rng.random() < 0.4 else rng.choice(['0', '50', '99', '100'])}")
    if not template:
        if rng.random() < 0.3:
            lines.append(f"recursive = {rng.choice(TOGGLES) if rng.random() < 0.3 else rng.choice(['yes', 'zfs', 'no'])}")
        if rng.random() < 0.08:
            lines.append(f"skip_children = {rng.choice(['yes', 'no', 'x'])}")
        if rng.random() < 0.08:
            lines.append(f"path = {rng.choice([*TREE, '', 'nope/x', 'tank/a@s', ' tank/a'])}")
    for key in HOOKS:
        if rng.random() < 0.04:
            lines.append(f"{key} = {rng.choice(['0', '', 'true', 'false'])}")
    if rng.random() < 0.03:
        lines.append("bogus_key = 1")
    if rng.random() < 0.05 and lines:
        lines.append(rng.choice(lines))  # a repeated key
    return lines


def make_conf(rng: random.Random) -> str:
    out: list[str] = []
    templates = [f"t{i}" for i in range(rng.randrange(3))]
    names = rng.sample([*TREE, "tank/zz", "alias", "template", "templatex/y", "Tank/A"], rng.randrange(1, 6))
    if rng.random() < 0.7:
        names.sort()  # parents first, most of the time: the other order is refused
    for name in names:
        out.append(f"[{name}]")
        if templates and rng.random() < 0.6:
            pick = rng.sample(templates, rng.randrange(1, len(templates) + 1))
            out.append("use_template = " + rng.choice([",", ", ", " ,", ",\t"]).join(pick) + (",nope" if rng.random() < 0.03 else ""))
        out += body(rng, template=False)
    for t in templates:
        out.append(f"[template_{t}]")
        out += body(rng, template=True)
    if rng.random() < 0.15:
        out.append("[template_default]")
        out += body(rng, template=True)
    return "\n".join(out) + "\n"


def changes(tz: ZoneInfo, year: int) -> list[int]:
    """The first hour on each new offset in the year."""
    at = int(datetime(year, 1, 1, tzinfo=tz).timestamp())
    out, before = [], datetime.fromtimestamp(at, tz).utcoffset()
    for _ in range(366 * 24):
        at += 3600
        if (now := datetime.fromtimestamp(at, tz).utcoffset()) != before:
            out.append(at)
            before = now
    return out


def moment(rng: random.Random, tz: ZoneInfo) -> int:
    """A date from October 2026 to the end of 2028, a third of them where a schedule can go wrong."""
    year = rng.choice((2027, 2028))
    pick = rng.random()
    if pick < 0.12 and (turns := changes(tz, year)):
        return rng.choice(turns) + rng.randrange(-5400, 5400)
    if pick < 0.24:  # the last day of a month, a leap day in 2028
        first = datetime(year, rng.randrange(2, 13), 1, tzinfo=tz)
        return int((first - timedelta(days=1)).timestamp()) + rng.randrange(DAY)
    if pick < 0.3:
        return int(datetime(year, 1, 1, tzinfo=tz).timestamp()) + rng.randrange(-3600, 3600)
    return int(datetime(2026, 10, 1, tzinfo=tz).timestamp()) + rng.randrange(820 * DAY)


def inventory(now: int, rng: random.Random) -> dict[str, int]:
    """Up to six snapshots of each type on each dataset, around the ages a small count keeps."""
    snaps: dict[str, int] = {}
    for ds in TREE:
        for t, period in (*PERIODS.items(), ("frequently", 900)):
            for i in range(rng.randrange(7)):
                snaps[f"{ds}@autosnap_{t[0]}{i}_{t}"] = now - int((i + rng.random()) * period * rng.choice([0.5, 1, 1, 2]))
        if rng.random() < 0.3:
            snaps[f"{ds}@manual_daily"] = now - 400 * DAY
    return dict(sorted(snaps.items(), key=lambda item: item[1]))


def case(seed: int, n: int) -> tuple[str, str, str, int, dict[str, int]]:
    """(name, config, zone, time, pool) of the n-th case: a hand case, then the seed's random ones."""
    if n < len(HAND):
        name, conf, zone, when, _ = HAND[n]
        local = datetime.fromisoformat(when.removesuffix(" fold")).replace(tzinfo=ZoneInfo(zone), fold=int(when.endswith("fold")))
        return name, conf, zone, int(local.timestamp()), inventory(int(local.timestamp()), random.Random(name))
    rng = random.Random(f"{seed}:{n}")
    zone = rng.choice(ZONES)
    now = moment(rng, ZoneInfo(zone))
    return f"case {n - len(HAND)} of seed {seed}", make_conf(rng), zone, now, inventory(now, rng)


def one(job: tuple[int, int]) -> dict[str, Any]:
    name, conf, zone, now, snaps = case(*job)
    did: list[str] = []
    wd = WORK / f"w{os.getpid()}"
    fresh_world(wd, conf)
    world = World(wd, TREE, CAPS, zone, snaps)
    extra: dict[str, list[str]] = {c: [] for c in EXTRA}
    refused, crashed, config = "", "", {}
    try:
        config = clean_config(world, now)
    except ConfigRefused as e:
        refused = str(e)
    except RuntimeError as e:
        crashed = str(e).strip().splitlines()[-1]
    audit = Auditor(world, [], exact=False)
    setting = ""
    try:
        audit.sections = model_sections(config)[0]
        if config:
            p._preflight_prune_config(config)
    except p.SanoidError as e:
        setting = str(e)
    except Exception as e:  # noqa: BLE001 - one of pysanoid's own readers failing on a setting is the finding
        crashed = f"reading its settings: {type(e).__name__}: {str(e)[:100]}"
    if crashed:
        audit.fail("crash", "the config", crashed)
    tally: Counter[str] = Counter({"refused configs": bool(refused), "configs with a setting prune refuses": bool(setting)})
    for argv in [] if crashed else RUNS:
        tag = " ".join(argv)
        before = dict(world.snaps)
        r = audit.run(tag, argv, now)
        tally[f"exit {r.rc}"] += 1
        did.append(f"{r.rc}/{len(r.created)}/{len(r.destroyed)}")
        tally["snapshots taken"] += len(r.created)
        tally["snapshots destroyed"] += len(r.destroyed)
        if refused and (r.snaps != before or r.rc != 1):
            extra["refused config"].append(f"{tag}: exit status {r.rc}, {len(r.created)} taken, {len(r.destroyed)} destroyed, "
                                           f"though the config is refused ({refused[:80]})")
        if setting and (r.destroyed or (r.rc != 1 and ("--cron" in argv or "--prune-snapshots" in argv))):
            extra["refused setting"].append(f"{tag}: exit status {r.rc}, {len(r.destroyed)} destroyed, though prune "
                                            f"refuses a setting ({setting[:80]})")
    if job[1] < len(HAND) and " ".join(did) != HAND[job[1]][4]:
        extra["hand case"].append(f"its runs' exit status/taken/destroyed are {' '.join(did)}, where they were {HAND[job[1]][4]}")
    return {"name": name, "conf": conf, "findings": {**audit.findings, **extra}, "tally": tally,
            "when": f"{datetime.fromtimestamp(now, ZoneInfo(zone)):%Y-%m-%d %H:%M:%S} {zone}"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("check", "fuzz"))
    ap.add_argument("n", nargs="?", type=int, default=None, help="fuzz: how many random cases (default 4000)")
    ap.add_argument("seed", nargs="?", type=int, default=1)
    ap.add_argument("--show", type=int, default=4, help="how many findings of a kind to print, each with its config")
    args = ap.parse_args()
    count = len(HAND) + 300 if args.mode == "check" else args.n or 4000
    cases = [(args.seed, n) for n in (range(count) if args.mode == "check" else range(len(HAND), len(HAND) + count))]
    t0 = time.monotonic()
    found: dict[str, list[str]] = {c: [] for c in (*CATEGORIES, *EXTRA)}
    tally: Counter[str] = Counter()
    for result in fan_out(cases, one):
        tally.update(result["tally"])
        for category, items in result["findings"].items():
            if items and len(found[category]) < args.show:
                found[category].append(f"{result['name']}, {result['when']}: {items[0]}\n{result['conf'][:1500]}")
            tally[f"failed: {category}"] += len(items)
    print(f"{len(cases)} configs x {len(RUNS)} runs" + (f", seed {args.seed}" if args.mode == "fuzz" else f", {len(HAND)} of them hand cases"))
    for key in ("refused configs", "configs with a setting prune refuses", "exit 0", "exit 1", "snapshots taken", "snapshots destroyed"):
        print(f"  {tally[key]:7d}  {key}")
    if not tally["snapshots destroyed"] or not tally["snapshots taken"] or not tally["refused configs"]:
        found["harness"].append("the cases never reached a snapshot, a destroy or a refused config")
        tally["failed: harness"] += 1
    print("failed checks: " + " | ".join(f"{c} {tally[f'failed: {c}']}" for c in found))
    for category, items in found.items():
        for item in items:
            print(f"\n--- {category}: {item}")
    ok = not any(tally[f"failed: {c}"] for c in found)
    print(f"{'PASS' if ok else 'FAIL'} ({time.monotonic() - t0:.0f} s)")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
