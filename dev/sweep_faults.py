#!/usr/bin/env python3
"""Fault sweep: every file operation and every zfs call of a run is made to fail in turn, in every way it can
(lib/world.py's kinds_for), or the process is ended there. Two clean rounds follow each fault, and every run of the
three is judged (lib/audit.py). It is the check that one failure, of anything a run touches, costs no snapshot the
config keeps and nothing a later run doesn't put right.

A trial is three rounds of pysanoid as a whole program, on a pool held in memory and a clock that stands at a fixed
date: the round with the fault, a clean round five minutes later, which reads the caches the first one left, and a
clean round an hour after the first, when both caches have expired. In `cron` mode a round is one --cron run; in
`split` mode a --take-snapshots run and then a --prune-snapshots run, as the two units make them.

Checked in every trial, on top of what the auditor checks in every run:
  - the fault fired at the event it was planned for;
  - what the fault and the round after it left untaken was reported: one of those runs was killed or exited
    non-zero. One class is registered as silent, and must be: a recursive section's child lookup that fails having
    listed nothing, or answers nothing, reads as no children, as upstream reads it (DEV-018), so the
    children with no section of their own go without snapshots while that answer is cached, at most 20 minutes;
  - the third round reads the pool afresh, so it is judged exactly.

A run takes a type a second time when the run before it could not record the first (it was killed, or zfs died
having made the snapshot): counted, not failed. The exact rule keeps it to the life of the cache that hides the first.

The config has a recursive section with a child of its own and one without, a zfs-recursive section, a section whose
pool defers pruning, and a section for a dataset that isn't there, so the first snapshot listing fails and the retry
runs. Its template keeps weeklies, which the defaults don't, so a child built from the defaults would lose its own.
The pool holds more than each type keeps, and more hourlies inside the four hours the type covers than it keeps, so
a prune that ignored the window would show. Every type is due, and a manual snapshot sits on each dataset.

  python3 dev/sweep_faults.py check    # dev/run.py's hand pass: cron, every event killed and failed one way,
                                       # and what asks zfs something failed every way
  python3 dev/sweep_faults.py full     # its fuzz pass: cron and split, every event, every way
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.audit import CATEGORIES, Auditor
from lib.model import DAY, HOUR, config_paths, expected_take, kind, model_sections, nest
from lib.parallel import fan_out
from lib.world import Fault, World, clean_config, fresh_world, kinds_for, run_main

WORK = DEV_DIR / "work" / "fault_sweep"

CONF = """\
[tank/a]
	use_template = prod

[tank/b]
	use_template = prod
	recursive = yes

[tank/b/c]
	daily = 5

[tank/z]
	use_template = prod
	recursive = zfs

[big/x]
	use_template = prod
	prune_defer = 50

[gone/x]
	use_template = prod
	autosnap = no

[template_prod]
	frequently = 0
	hourly = 4
	daily = 3
	weekly = 2
	monthly = 2
	yearly = 0
	autosnap = yes
	autoprune = yes
"""
TREE = ["big", "big/x", "tank", "tank/a", "tank/b", "tank/b/c", "tank/b/d", "tank/z", "tank/z/k"]
MANAGED = ["tank/a", "tank/b", "tank/b/c", "tank/b/d", "tank/z", "tank/z/k", "big/x"]
CAPS = {"big": 60, "tank": 10}
TZ = "Europe/Dublin"
START = int(datetime(2026, 10, 11, 9, 30, tzinfo=UTC).timestamp())
ROUNDS = (0, 300, HOUR)  # the fault; the caches still fresh; both expired
MODES = {"cron": [["--cron", "--verbose"]],
         "split": [["--take-snapshots", "--verbose"], ["--prune-snapshots", "--verbose"]]}
SILENT = ("rc1", "empty_ok")  # how a child lookup fails for the registered class
EXTRA = ("unreported",)


def inventory() -> dict[str, int]:
    """Hourlies 2 to 9 hours old and three between them, which makes five inside the four hours the type keeps;
    dailies 2 to 8 days, weeklies 2 to 5 weeks, monthlies 2 to 6 months, and a manual snapshot."""
    snaps: dict[str, int] = {}
    for ds in MANAGED:
        snaps[f"{ds}@manual_keep"] = START - 400 * DAY
        for i in range(2, 7):
            snaps[f"{ds}@autosnap_m{i}_monthly"] = START - i * 31 * DAY - 120
        for i in range(2, 6):
            snaps[f"{ds}@autosnap_w{i}_weekly"] = START - i * 7 * DAY - 120
        for i in range(2, 9):
            snaps[f"{ds}@autosnap_d{i}_daily"] = START - i * DAY - 120
        for i in range(2, 10):
            snaps[f"{ds}@autosnap_h{i}_hourly"] = START - i * HOUR - 120
        for i in range(1, 4):
            snaps[f"{ds}@autosnap_h{i}30_hourly"] = START - i * HOUR - 1920
    return dict(sorted(snaps.items(), key=lambda item: item[1]))


def quick(label: str) -> tuple[str, ...]:
    """The hand pass's share of an event's faults: the process ended there, and the first other way it fails. The
    few commands that ask zfs something (the listings, the child lookups, the capacity) fail every way."""
    kinds = kinds_for(label)
    return kinds if label.startswith(("zfs:get", "zfs:list", "zpool:")) else tuple(dict.fromkeys(("kill", kinds[0])))


class Sweep:
    """One mode's trials. Made before the workers fork, so each has the model and the clean rounds to compare with."""

    def __init__(self, mode: str) -> None:
        self.mode, self.steps = mode, MODES[mode]
        wd = WORK / mode / "base"
        fresh_world(wd, CONF)
        world = World(wd, TREE, CAPS, TZ, inventory())
        self.sections, unchecked = model_sections(clean_config(world, START))
        if unchecked:
            raise SystemExit(f"the sweep's own config has settings pysanoid refuses: {unchecked}")
        if run_main(world, ["--readonly", "--take-snapshots"], START).rc != 0:
            raise SystemExit("a clean run of the sweep's own config failed")
        self.datasets_txt = (wd / "cache" / "datasets.txt").read_text()
        named = config_paths(wd / "conf" / "sanoid.conf")
        # the datasets recursion takes snapshots of that have no section of their own
        self.childless = {s.path for s in self.sections if s.takes and s.path not in named}
        clean = self.rounds(wd, None)
        self.events: list[list[str]] = clean["events"]
        self.made: Counter[tuple[str, str]] = clean["made"]
        self.baseline: list[str] = [f"{c}: {m}" for c, items in clean["findings"].items() for m in items]
        if clean["exact"] != clean["runs"]:
            self.baseline.append(f"only {clean['exact']} of {clean['runs']} clean runs read the pool as it is")

    def rounds(self, wd: Path, fault: tuple[int, int, str] | None) -> dict[str, Any]:
        """The three rounds, the first with the fault (step, event, kind) if there is one."""
        fresh_world(wd, CONF)
        world = World(wd, TREE, CAPS, TZ, inventory(), self.datasets_txt)
        audit = Auditor(world, self.sections)
        findings: dict[str, list[str]] = {c: [] for c in EXTRA}
        events: list[list[str]] = []
        made: Counter[tuple[str, str]] = Counter()
        reported = False  # a run was killed or exited non-zero: someone was told
        label = ""
        for n, after in enumerate(ROUNDS):
            for step, argv in enumerate(self.steps):
                now = START + after + 5 * step
                planned = Fault(fault[1], fault[2]) if fault is not None and n == 0 and step == fault[0] else None
                r = audit.run(f"round {n + 1}{f' step {step + 1}' if len(self.steps) > 1 else ''}", argv, now, planned)
                made.update((full.split("@", 1)[0], kind(full.split("@", 1)[1]) or "") for full, _ in r.created)
                reported = reported or r.killed or r.rc != 0
                if n == 0:
                    events.append(r.events)
                if planned is not None and fault is not None:
                    label = r.fired or ""
                    if r.fired != self.events[step][fault[1]] or r.kind != fault[2]:
                        audit.fail("harness", "round 1", f"the fault fired at {r.fired} as {r.kind or 'nothing'}")
                if n == 2 and not r.exact:
                    audit.fail("harness", "round 3", "the run did not read the pool afresh")
            if n == 1 and fault is not None:
                owed = expected_take(self.sections, TREE, nest(world.snaps), START + after, audit.tz) or set()
                silent = {full.split("@", 1)[0] for full in owed} if not reported else set()
                parent = label.removeprefix("zfs:list ")
                if self.registered(label, fault[2]) != bool(silent) or not all(
                        ds in self.childless and ds.startswith(f"{parent}/") for ds in silent):
                    findings["unreported"].append(
                        f"after round 2, with nothing reported, no snapshot of {sorted(silent)}" if silent
                        else "the registered class (a child lookup that lists nothing) left nothing untaken, or reported it")
        return {"findings": {**audit.findings, **findings}, "events": events, "made": made, "runs": audit.runs,
                "exact": audit.exact, "again": fault is not None and any(made[key] > self.made[key] for key in made)}

    def registered(self, label: str, how: str) -> bool:
        """Whether a fault is of the class registered as silent: the child lookup of a section whose children take
        snapshots under its settings alone, failing in a way that lists nothing."""
        parent = label.removeprefix("zfs:list ")
        return label.startswith("zfs:list ") and how in SILENT and any(c.startswith(f"{parent}/") for c in self.childless)

    def trials(self, every_kind: bool) -> list[tuple[int, int, str]]:
        return [(step, at, how) for step, labels in enumerate(self.events) for at, label in enumerate(labels)
                for how in (kinds_for(label) if every_kind else quick(label))]


_sweep: list[Sweep] = []  # the mode being swept, for the workers


def one(trial: tuple[int, int, str]) -> tuple[dict[str, list[str]], int, int, bool]:
    result = _sweep[0].rounds(WORK / _sweep[0].mode / f"w{os.getpid()}", trial)
    return result["findings"], result["runs"], result["exact"], result["again"]


def sweep(mode: str, every_kind: bool, show: int) -> bool:
    t0 = time.monotonic()
    sw = Sweep(mode)
    _sweep[:] = [sw]
    trials = sw.trials(every_kind)
    found: dict[str, list[str]] = {c: [] for c in (*CATEGORIES, *EXTRA)}
    runs = exact = registered = again = 0
    for (step, at, how), (findings, n_runs, n_exact, took_again) in zip(trials, fan_out(trials, one), strict=True):
        label = sw.events[step][at]
        runs, exact, again = runs + n_runs, exact + n_exact, again + took_again
        registered += sw.registered(label, how)
        for category, items in findings.items():
            found[category] += [f"[{mode} step {step + 1} #{at} {label} / {how}] {item}" for item in items]
    print(f"{'=' * 78}\n{mode}: {len(trials)} trials over {' + '.join(str(len(e)) for e in sw.events)} events, "
          f"{'every way each can fail' if every_kind else 'each killed and failed one way, zfs asked every way'}\n{'=' * 78}")
    print(f"runs: {runs}, {exact} of them judged exactly | a type taken a second time in {again} trials | registered as "
          f"silent (a child lookup that lists nothing): {registered} trials")
    for item in sw.baseline:
        print(f"  the rounds with no fault: {item}")
    print("failed checks: " + " | ".join(f"{c} {len(found[c])}" for c in found))
    for category, items in found.items():
        if items:
            print(f"\n--- {category} (first {min(show, len(items))} of {len(items)}) ---")
            for item in items[:show]:
                print(f"  {item}")
    ok = not sw.baseline and not any(found.values())
    print(f"{'PASS' if ok else 'FAIL'} ({time.monotonic() - t0:.0f} s)")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("check", "full"))
    ap.add_argument("ignored", nargs="*", help=argparse.SUPPRESS)  # dev/run.py's -n and --seed: the sweep has no dice
    ap.add_argument("--show", type=int, default=10, help="how many findings of a kind to print")
    args = ap.parse_args()
    results = [sweep(mode, args.mode == "full", args.show) for mode in (("cron",) if args.mode == "check" else MODES)]
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
