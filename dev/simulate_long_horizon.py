#!/usr/bin/env python3
"""Long-horizon simulation: pysanoid's real take_snapshots and prune_snapshots, run after run, over weeks to
years of simulated time on a real sanoid.conf, in process against an in-memory zfs. It checks what a single
run can't show: the state each run leaves for the next.

Every run is checked, and any failure makes the exit status 1:
  - prune destroyed nothing younger than count x period, never left fewer than count, and touched nothing a
    section doesn't prune, or prunes only while its pool defers pruning;
  - every type a section takes stays on its slots: none missing, none taken again early (daily and longer),
    and no pile-up past count once the type has had count x period to fill;
  - take returns exactly what zfs created, and only types a section takes;
  - nothing is reported (ERROR, CRITICAL ERROR, could not remove) except the failures injected, and every
    injected failure is reported.

Sampled runs also go through real upstream Perl on the same inventory (dev/diff_take_snapshots.py and
dev/diff_prune.py): every run within 3 hours of a clock change, and every run that takes or prunes a daily or
longer type, with the run before it, within 3 days of a change, or the first of each month elsewhere
(--perl-checks). A difference DEVIATIONS.md documents -- upstream's DST arithmetic (DEV-030) and the dst_
retry (DEV-029) -- is counted as known; any other fails.

The fake zfs keeps the pool. `zfs snapshot` refuses a name the dataset (or, with -r, a descendant) already
has, as the second pass through a fall-back's repeated hour does, so the dst_ retry runs as on a real pool;
-r snapshots every descendant; `zfs destroy` of a missing snapshot fails; `zfs list` answers recursion's child
lookup at load from the dataset tree: the config's datasets and their parents, plus --dataset/--datasets.
--fail-rate injects failed snapshots and destroys (seeded). With --split-take-prune, take and prune run as
the two services do, prune listing the pool afresh; without it, as one --cron run sharing take's listing.

  python3 dev/simulate_long_horizon.py check             # dev/run.py's hand pass: fixed-date windows
  python3 dev/simulate_long_horizon.py long [n] [seed]   # its fuzz pass: years, 1-in-n zfs failures
  python3 dev/simulate_long_horizon.py --conf fixtures/sanoid.conf --tz Europe/Dublin --split-take-prune \\
      --pool-cap tank=78 --pool-cap zroot=11 --tick-minutes 60 --dataset tank/software/test --days 45

`check` runs the production-shaped config, fixtures/sanoid.conf (Europe/Dublin, hourly, split), for 45 days across each clock change of
2026-27, and FIXTURE_CONF (every type and kind of recursion, America/New_York, every 15 minutes, shared) for
40 days across each. `long` runs the production-shaped one for 4 years and the fixture's for 400 days with injected failures.
The scenarios run side by side, one process each.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import random
import shutil
import statistics
import subprocess
import sys
import time as time_module
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
import diff_prune
import diff_take_snapshots
from lib.model import (
    DAY,
    HOUR,
    PERIODS,
    Section,
    config_paths,
    dataset_tree,
    model_sections,
    type_of,
)

import pysanoid as p

PYSANOID_DEFAULTS = DEV_DIR.parent / "sanoid.defaults.conf"
PRODUCTION_CONF = DEV_DIR / "fixtures" / "sanoid.conf"
WORK = DEV_DIR / "work" / "long_horizon"

DAILY_PLUS = frozenset(("daily", "weekly", "monthly", "yearly"))
# CERN's `mega` pool (2026-09-22): the capacity model when no --pool-cap is given
STARTING_CAPACITY_PCT, CAPACITY_GROWTH_PCT_PER_YEAR = 66.0, 2.5

# The simulator's own coverage, apart from any real config: every type, hourly_min set, both kinds of
# recursion, a child section below its recursive parent, process_children_only, skip_children, a section
# whose path differs from its name, and a pool that defers pruning.
FIXTURE_CONF = """\
[tank/all]
	use_template = all
	hourly_min = 30

[tank/zfsrec]
	use_template = short
	recursive = zfs

[tank/rec]
	use_template = short
	recursive = yes

[tank/rec/b]
	use_template = long

[tank/pco]
	use_template = short
	recursive = yes
	process_children_only = yes

[tank/skip]
	use_template = short
	skip_children = yes

[tank/deferred]
	use_template = short
	prune_defer = 90

[offsite]
	path = tank/alias
	use_template = long

[template_all]
	frequently = 4
	hourly = 3
	daily = 2
	weekly = 1
	monthly = 1
	yearly = 1
	autosnap = yes
	autoprune = yes

[template_short]
	frequently = 0
	hourly = 2
	daily = 2
	weekly = 0
	monthly = 1
	yearly = 0
	autosnap = yes
	autoprune = yes

[template_long]
	frequently = 0
	hourly = 0
	daily = 5
	weekly = 2
	monthly = 0
	yearly = 0
	autosnap = yes
	autoprune = yes
"""
FIXTURE_DATASETS = ["tank/zfsrec/a", "tank/zfsrec/a/x", "tank/rec/a", "tank/rec/b", "tank/pco/a", "tank/pco/b",
                    "tank/skip/a"]

Pool = dict[str, dict[str, int]]  # dataset -> snapshot name -> creation time


@dataclass
class Scenario:
    name: str
    tz: str
    start: date
    end: date
    tick_minutes: int
    split: bool
    conf: Path | None = None
    conf_text: str | None = None
    pool_capacity: dict[str, tuple[float, float]] | None = None  # pool -> (percent, growth per year)
    datasets: list[str] = field(default_factory=list)
    fail_rate: float = 0.0
    fail_seed: int = 1
    perl_checks: str = "sampled"


def builtin(mode: str, n: int | None = None, seed: int | None = None) -> list[Scenario]:
    production: dict[str, Any] = {
        "conf": PRODUCTION_CONF, "tz": "Europe/Dublin", "tick_minutes": 60, "split": True,
        # capacities read on a production host
        "pool_capacity": {"tank": (78.0, 0.0), "zroot": (11.0, 0.0)}, "datasets": ["tank/software/test"],
    }
    fixture: dict[str, Any] = {
        "conf_text": FIXTURE_CONF, "tz": "America/New_York", "tick_minutes": 15, "split": False,
        "pool_capacity": {"tank": (50.0, 0.0)}, "datasets": FIXTURE_DATASETS,
    }
    if mode == "check":
        return [
            Scenario("production-autumn", start=date(2026, 9, 27), end=date(2026, 11, 11), **production),
            Scenario("production-spring", start=date(2027, 2, 26), end=date(2027, 4, 12), **production),
            Scenario("fixture-autumn", start=date(2026, 10, 11), end=date(2026, 11, 20), **fixture),
            Scenario("fixture-spring", start=date(2027, 2, 21), end=date(2027, 4, 2), **fixture),
        ]
    return [
        Scenario("production-4-years", start=date(2026, 9, 27), end=date(2030, 9, 27), **production),
        Scenario("fixture-400-days", start=date(2026, 10, 11), end=date(2027, 11, 15),
                 fail_rate=1 / (n or 1000), fail_seed=seed or 1, **fixture),
    ]


# ============================================================================
# The pool
# ============================================================================

def copy_pool(pool: Pool) -> Pool:
    return {ds: dict(names) for ds, names in pool.items()}


def snapshot_view(pool: Pool) -> dict[str, dict[str, p.Snapshot]]:
    """The pool as get_snapshots would hand it to take and prune."""
    return {ds: {n: p.Snapshot(ds, n, type_of(n), c) for n, c in names.items()} for ds, names in pool.items()}


def newest_by_type(pool: Pool) -> dict[tuple[str, str], int]:
    newest: dict[tuple[str, str], int] = {}
    for ds, names in pool.items():
        for n, c in names.items():
            key = (ds, type_of(n))
            if c > newest.get(key, -1):
                newest[key] = c
    return newest


class Clock:
    """Stands in for pysanoid's `time` module: time() is the simulated clock, the rest the real module."""

    def __init__(self) -> None:
        self.now = time_module.time()

    def time(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(time_module, name)


@dataclass
class FakeProc:
    """A zfs command as pysanoid reads one: its exit status, and what it wrote, in bytes."""

    returncode: int
    stderr: bytes = b""
    stdout: bytes = b""


class FakeZfs:
    def __init__(self, tree: list[str], clock: Clock, fail_rate: float, rng: random.Random) -> None:
        self.pool: Pool = {}
        self.tree, self.clock = tree, clock
        self.fail_rate, self.rng = fail_rate, rng
        self.injected_snapshot_failures = self.injected_destroy_failures = 0
        self.reset_run()

    def reset_run(self) -> None:
        self.snapshot_fail: dict[str, dict[str, str]] = {}  # injected this run, for the Perl side too
        self.destroy_fail: dict[str, dict[str, str]] = {}
        self.refused: list[str] = []  # full names refused as existing this run
        self.created: list[tuple[str, bool]] = []  # (full name, made by an ancestor's -r)

    def _inject(self) -> bool:
        return self.fail_rate > 0 and self.rng.random() < self.fail_rate

    def run_zfs_cmd(self, cmd: list[str], **_kwargs: object) -> FakeProc:
        full = cmd[-1]
        ds, name = full.split("@", 1)
        if cmd[1] == "snapshot":
            if self._inject():
                self.injected_snapshot_failures += 1
                stderr = f"cannot create snapshot '{full}': out of space\n"
                self.snapshot_fail[full] = {"stderr": stderr}
                return FakeProc(1, stderr=stderr.encode())
            if ds not in self.tree:
                return FakeProc(1, stderr=f"cannot open '{ds}': dataset does not exist\n".encode())
            targets = [ds, *(d for d in self.tree if d.startswith(ds + "/"))] if "-r" in cmd[2:-1] else [ds]
            for target in targets:
                if name in self.pool.get(target, {}):
                    self.refused.append(f"{target}@{name}")
                    return FakeProc(1, stderr=f"cannot create snapshot '{target}@{name}': dataset already exists\n".encode())
            for target in targets:
                self.pool.setdefault(target, {})[name] = int(self.clock.now)
                self.created.append((f"{target}@{name}", target != ds))
            return FakeProc(0)
        if cmd[1] == "destroy":
            if self._inject():
                self.injected_destroy_failures += 1
                stderr = f"cannot destroy '{full}': dataset is busy\n"
                self.destroy_fail[full] = {"stderr": stderr}
                return FakeProc(1, stderr=stderr.encode())
            if name not in self.pool.get(ds, {}):
                return FakeProc(1, stderr=b"could not find any snapshots to destroy; check snapshot names.\n")
            del self.pool[ds][name]
            return FakeProc(0)
        raise AssertionError(f"the simulation's fake zfs got an unexpected call: {cmd}")

    def run_zfs_bytes(self, cmd: list[str]) -> subprocess.CompletedProcess[bytes]:
        if cmd[1:7] != ["list", "-o", "name", "-t", "filesystem,volume", "-Hr"]:
            raise AssertionError(f"the simulation's fake zfs got an unexpected call: {cmd}")
        out, err, rc = [], [], 0
        for fs in cmd[7:]:
            if fs not in self.tree:
                err.append(f"cannot open '{fs}': dataset does not exist\n")
                rc = 1
                continue
            out += [d for d in self.tree if d == fs or d.startswith(fs + "/")]
        return subprocess.CompletedProcess(cmd, rc, "".join(f"{d}\n" for d in out).encode(), "".join(err).encode())


# ============================================================================
# The clock changes in the window, and what a section takes and prunes
# ============================================================================

class Changes:
    """The window's clock changes: (first instant on the new offset, "back" or "forward", size in seconds)."""

    def __init__(self, tz: ZoneInfo, start: int, end: int) -> None:
        self.tz = tz

        def offset(epoch: int) -> timedelta:
            off = datetime.fromtimestamp(epoch, tz).utcoffset()
            assert off is not None
            return off

        self.at: list[tuple[int, str, int]] = []
        epoch, prev = start, offset(start)
        while epoch < end:
            nxt = epoch + 6 * HOUR  # no zone changes twice within 6 hours
            cur = offset(nxt)
            if cur != prev:
                lo, hi = epoch, nxt
                while hi - lo > 1:
                    mid = (lo + hi) // 2
                    lo, hi = (mid, hi) if offset(mid) == prev else (lo, mid)
                self.at.append((hi, "back" if cur < prev else "forward", abs(int((cur - prev).total_seconds()))))
            epoch, prev = nxt, cur
        self.shift = max((size for _, _, size in self.at), default=0)
        self.days = {(datetime.fromtimestamp(e, tz).date(), kind) for e, kind, _ in self.at}

    def near(self, epoch: int, within: int) -> bool:
        return any(abs(epoch - e) <= within for e, _, _ in self.at)

    def on_day(self, now: datetime, kind: str) -> bool:
        return (now.date(), kind) in self.days

    @staticmethod
    def repeated(now: datetime) -> bool:
        """True in either pass through a fall-back's repeated wall-clock time."""
        return now.replace(fold=1 - now.fold).utcoffset() != now.utcoffset()


def slot_gaps(t: str, sec: Section, shift: int) -> tuple[float, float | None]:
    """The least and the most real time between two consecutive slots of a type, a clock change moving a
    longer type's by `shift`; None where no bound is useful (a yearly on 29 February)."""
    if t == "frequently":
        return 0, sec.periods["frequently"]
    if t == "hourly":
        return 0, HOUR + shift % HOUR  # a 30-minute change can drop one hourly slot
    if t == "daily":
        return DAY - shift, DAY + shift
    if t == "weekly":
        return 7 * DAY - shift, 7 * DAY + shift
    if t == "monthly":  # a month without monthly_mday has no slot
        return 28 * DAY - shift, (31 if sec.setting("monthly_mday", 1) <= 28 else 62) * DAY + shift
    leap_day = (sec.setting("yearly_mon", 1), sec.setting("yearly_mday", 1)) == (2, 29)
    return 365 * DAY - shift, None if leap_day else 366 * DAY + shift


def configured_ahead(now: datetime, sec: Section, kind: str) -> bool:
    """Whether this month's monthly (this year's yearly) time is still ahead of now."""
    try:
        if kind == "monthly":
            when = datetime(now.year, now.month, sec.setting("monthly_mday", 1), sec.setting("monthly_hour", 0),
                            sec.setting("monthly_min", 0), tzinfo=now.tzinfo)
        else:
            when = datetime(now.year, sec.setting("yearly_mon", 1), sec.setting("yearly_mday", 1),
                            sec.setting("yearly_hour", 0), sec.setting("yearly_min", 0), tzinfo=now.tzinfo)
    except ValueError:
        return True  # this month (year) lacks the day
    return when.timestamp() > now.timestamp()


# ============================================================================
# The simulation
# ============================================================================

@dataclass
class Run:
    """One run, as the Perl check needs it."""
    epoch: int
    now: datetime
    pre_take: Pool
    prune_view: Pool
    taken: dict[str, dict[str, Any]]
    pruned: list[str]
    refused: list[str]
    snapshot_fail: dict[str, dict[str, str]]
    destroy_fail: dict[str, dict[str, str]]
    caps: dict[str, int]
    checked: bool = False


CATEGORIES = ("crash", "store", "take bookkeeping", "unexpected take", "over-prune", "missed slot", "extra take",
              "pile-up", "problem", "perl take", "perl prune", "perl harness")


class Simulation:
    def __init__(self, sc: Scenario, conf: Path, scratch: Path) -> None:
        self.sc, self.conf, self.scratch = sc, conf, scratch
        self.tz = ZoneInfo(sc.tz)
        self.start = int(datetime.combine(sc.start, datetime.min.time(), self.tz).timestamp())
        self.end = int(datetime.combine(sc.end, datetime.min.time(), self.tz).timestamp())
        self.tick = sc.tick_minutes * 60
        self.clock = Clock()
        named = config_paths(conf)
        self.extra = sorted(set(sc.datasets) - set(named))
        self.tree = dataset_tree(named + self.extra)
        self.fake = FakeZfs(self.tree, self.clock, sc.fail_rate, random.Random(sc.fail_seed))
        self.capacity: dict[str, float] = {}
        self.cache_dir, self.run_dir = scratch / "cache", scratch / "run"
        self.cache_dir.mkdir()
        self.run_dir.mkdir()
        self.findings: dict[str, list[str]] = {c: [] for c in CATEGORIES}
        self.known: Counter[str] = Counter()  # documented take differences, by rule
        self.known_runs: dict[str, set[int]] = defaultdict(set)
        self.perl = Counter()
        self.month_firsts: set[tuple[int, int, str]] = set()
        self.fail_streak: dict[tuple[str, str], int] = {}  # consecutive runs an injected failure cost a type
        self.late: dict[tuple[str, str], int] = {}  # how many runs late the newest of a type was taken
        self.first: dict[tuple[str, str], int] = {}  # when a type's first snapshot was taken
        self.gaps: dict[tuple[str, str], list[int]] = defaultdict(list)
        self.dst_names: list[str] = []
        self.series: dict[str, list[tuple[str, int]]] = defaultdict(list)
        self.runs = self.taken = self.destroyed = 0
        self.by_type: dict[str, Counter[str]] = {"taken": Counter(), "destroyed": Counter()}
        self.saved = {name: getattr(p, name) for name in
                      ("run_zfs_cmd", "run_zfs_bytes", "get_zpool_capacity", "remove_cached_snapshots", "time")}
        p.run_zfs_cmd = self.fake.run_zfs_cmd
        p.run_zfs_bytes = self.fake.run_zfs_bytes
        p.get_zpool_capacity = lambda pool: round(self.capacity.get(pool, 0))
        p.remove_cached_snapshots = lambda *_a, **_k: True  # no cache: every run lists the pool afresh
        p.time = self.clock

    def restore(self) -> None:
        for name, value in self.saved.items():
            setattr(p, name, value)

    def load(self) -> None:
        conf_dir = self.scratch / "conf"
        conf_dir.mkdir()
        shutil.copyfile(self.conf, conf_dir / "sanoid.conf")
        shutil.copyfile(PYSANOID_DEFAULTS, conf_dir / "sanoid.defaults.conf")
        zc = p.ZfsClient(self.cache_dir, cache_ttl=1200, force_update=True)
        self.config = p.load_config(conf_dir, zc)
        self.sections, self.unchecked = model_sections(self.config)
        self.by_path: dict[str, list[Section]] = defaultdict(list)
        for sec in self.sections:
            self.by_path[sec.path].append(sec)
        self.zfs_parents = [c["path"] for c in self.config.values() if c.get("zfs_recursion")]
        self.pools = sorted({sec.pool for sec in self.sections})
        self.cap_spec = (dict(self.sc.pool_capacity) if self.sc.pool_capacity is not None
                         else dict.fromkeys(self.pools, (STARTING_CAPACITY_PCT, CAPACITY_GROWTH_PCT_PER_YEAR)))
        self.changes = Changes(self.tz, self.start, self.end)

    def fail(self, category: str, now: datetime, message: str) -> None:
        self.findings[category].append(f"{now.isoformat()}: {message}")

    def run(self) -> None:
        prev: Run | None = None
        epoch = self.start
        while epoch < self.end:
            run = self.step(epoch)
            if run is not None and self.sc.perl_checks != "none":
                for due in self.perl_due(run, prev):
                    self.perl_check(due)
            prev = run
            epoch += self.tick

    def step(self, epoch: int) -> Run | None:
        now = datetime.fromtimestamp(epoch, self.tz)
        self.clock.now = float(epoch)
        years = (epoch - self.start) / PERIODS["yearly"]
        for pool, (pct, growth) in self.cap_spec.items():
            self.capacity[pool] = pct + growth * years
        caps = {pool: round(self.capacity.get(pool, 0)) for pool in self.pools}
        fake = self.fake
        fake.reset_run()
        pre_take = copy_pool(fake.pool)
        view = snapshot_view(fake.pool)
        by_type = p.snapshots_by_type(view, self.config, now=epoch)
        take_problems: list[str] = []
        prune_problems: list[str] = []
        err = io.StringIO()
        self.runs += 1
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                taken = p.take_snapshots(self.config, by_type, now=now, problems=take_problems)
                after_take = copy_pool(fake.pool)
                if self.sc.split:
                    prune_snaps = snapshot_view(fake.pool)
                    prune_by_type = p.snapshots_by_type(prune_snaps, self.config, now=epoch)
                    prune_view = after_take
                else:
                    prune_snaps, prune_by_type, prune_view = view, by_type, pre_take
                pruned = p.prune_snapshots(self.config, prune_snaps, prune_by_type, self.cache_dir, self.run_dir,
                                           problems=prune_problems)
        except Exception:  # noqa: BLE001 - a crash is the finding; the next run shows whether it recurs
            self.fail("crash", now, traceback.format_exc())
            return None
        self.taken += len(taken)
        self.destroyed += len(pruned)
        self.by_type["taken"].update(type_of(full.split("@", 1)[1]) for full, _ in fake.created)
        self.by_type["destroyed"].update(type_of(full.split("@", 1)[1]) for full in pruned)

        created = [(full, by_parent) for full, by_parent in fake.created]
        self.check_take(now, taken, created, pre_take, after_take)
        self.check_prune(now, epoch, pruned, after_take, caps)
        self.check_problems(now, take_problems, prune_problems, err.getvalue())
        if now.hour == 0 and now.minute == 0:
            for ds, names in fake.pool.items():
                self.series[ds].append((now.isoformat(), len(names)))
        return Run(epoch, now, pre_take, prune_view, taken, pruned, list(fake.refused), dict(fake.snapshot_fail),
                   dict(fake.destroy_fail), caps)

    # -- invariants ----------------------------------------------------------

    def check_take(self, now: datetime, taken: dict[str, dict[str, Any]], created: list[tuple[str, bool]],
                   pre_take: Pool, after_take: Pool) -> None:
        epoch = int(now.timestamp())
        made = {full for full, by_parent in created if not by_parent}
        for full in sorted(made ^ set(taken)):
            self.fail("take bookkeeping", now, f"{full} {'created but not returned' if full in made else 'returned but not created'}")
        for full, info in taken.items():
            ds, name = full.split("@", 1)
            t = type_of(name)
            if int(info["time"]) != after_take.get(ds, {}).get(name, int(info["time"])):
                self.fail("take bookkeeping", now, f"{full} returned with time {info['time']}, created at {after_take[ds][name]}")
            if not any(t in sec.takes for sec in self.by_path.get(ds, [])):
                self.fail("unexpected take", now, f"{full}: no section takes {t} snapshots of {ds}")
            if name.endswith(f"dst_{t}"):
                self.dst_names.append(full)

        for full in self.fake.snapshot_fail:
            ds, name = full.split("@", 1)
            key = (ds, type_of(name))
            self.fail_streak[key] = self.fail_streak.get(key, 0) + 1
        pre_newest = newest_by_type(pre_take)
        for full, by_parent in created:
            ds, name = full.split("@", 1)
            t = type_of(name)
            key = (ds, t)
            prev = pre_newest.get(key)
            if prev is not None:
                self.gaps[key].append(epoch - prev)
            late, self.late[key] = self.late.get(key, 0), self.fail_streak.pop(key, 0)
            first = self.first.setdefault(key, epoch)
            sec = None if by_parent else next((s for s in self.by_path.get(ds, []) if t in s.takes), None)
            if sec is None or prev is None:
                continue  # a -r descendant (its parent is checked), an unexpected take (reported), or the first
            least, most = slot_gaps(t, sec, self.changes.shift)
            if most is not None and epoch - prev >= most + self.tick * (1 + self.late[key]):
                self.fail("missed slot", now, f"{full}: {(epoch - prev) / HOUR:.2f} h after the last {t}, "
                          f"where slots are at most {most / HOUR:.2f} h apart")
            # a type's first snapshot is taken because there is none, not on a slot: the gap after it is no measure
            if t in DAILY_PLUS and prev != first and epoch - prev <= least - self.tick * (1 + late):
                self.fail("extra take", now, f"{full}: {(epoch - prev) / HOUR:.2f} h after the last {t}, "
                          f"where slots are at least {least / HOUR:.2f} h apart")

        newest = newest_by_type(after_take)
        for sec in self.sections:
            for t in sec.takes:
                key = (sec.path, t)
                streak = self.fail_streak.get(key, 0)
                made_at = newest.get(key)
                if made_at is None:
                    if not streak:
                        self.fail("missed slot", now, f"{sec.path} has no {t} snapshot")
                    continue
                _, most = slot_gaps(t, sec, self.changes.shift)
                if most is not None and epoch - made_at >= most + self.tick * (1 + streak):
                    self.fail("missed slot", now, f"{sec.path}'s newest {t} is {(epoch - made_at) / HOUR:.2f} h old, "
                              f"where slots are at most {most / HOUR:.2f} h apart")

    def _deferred(self, sec: Section, caps: dict[str, int]) -> bool:
        return sec.deferred(caps)

    def check_prune(self, now: datetime, epoch: int, pruned: list[str], before: Pool, caps: dict[str, int]) -> None:
        after = self.fake.pool
        seen: set[str] = set()
        for full in pruned:
            ds, name = full.split("@", 1)
            if full in seen:
                self.fail("store", now, f"{full} returned as destroyed twice")
            elif name not in before.get(ds, {}):
                self.fail("store", now, f"{full} returned as destroyed, but wasn't there")
            elif name in after.get(ds, {}):
                self.fail("store", now, f"{full} returned as destroyed, but is still there")
            seen.add(full)
        gone: dict[tuple[str, str], list[int]] = defaultdict(list)
        for ds, names in before.items():
            for name, made_at in names.items():
                if name not in after.get(ds, {}):
                    if f"{ds}@{name}" not in seen:
                        self.fail("store", now, f"{ds}@{name} vanished without being returned as destroyed")
                    gone[(ds, type_of(name))].append(made_at)

        for (ds, t), made in gone.items():
            permitting = [s for s in self.by_path.get(ds, []) if t in s.prunes and not self._deferred(s, caps)]
            if not permitting:
                self.fail("over-prune", now, f"{len(made)} {t} of {ds} destroyed, which no pruning section covers")
                continue
            left = sum(1 for name in after.get(ds, {}) if type_of(name) == t)
            floor = min(s.prunes[t] for s in permitting)
            if left < floor:
                self.fail("over-prune", now, f"{ds} left with {left} {t}, fewer than {floor}")
            for made_at in made:
                if not any(made_at < epoch - s.prunes[t] * s.periods[t] for s in permitting):
                    windows = ", ".join(f"{s.prunes[t] * s.periods[t] / HOUR:g} h" for s in permitting)
                    self.fail("over-prune", now, f"a {t} of {ds} destroyed {(epoch - made_at) / HOUR:.2f} h old, "
                              f"inside its window ({windows})")

        failed = {(full.split("@", 1)[0], type_of(full.split("@", 1)[1])) for full in self.fake.destroy_fail}
        for sec in self.sections:
            if self._deferred(sec, caps):
                continue
            made_by_type: dict[str, list[int]] = defaultdict(list)
            for name, made_at in after.get(sec.path, {}).items():
                made_by_type[type_of(name)].append(made_at)
            for t, made in made_by_type.items():
                # One over count is the steady state (the boundary snapshot is exactly count x period old), one
                # more while an injected destroy failure waits for the next run. Beyond that, a snapshot past its
                # window is one prune should have destroyed.
                wanted = sec.prunes.get(t, 0)
                if wanted <= 0 or len(made) <= wanted + 1 + ((sec.path, t) in failed):
                    continue
                window = wanted * sec.periods[t]
                if epoch - min(made) > window * 1.02:
                    self.fail("pile-up", now, f"{sec.path} has {len(made)} {t} (count {wanted}); the oldest is "
                              f"{(epoch - min(made)) / HOUR:.1f} h old, the window {window / HOUR:g} h")

    def check_problems(self, now: datetime, take_problems: list[str], prune_problems: list[str], stderr: str) -> None:
        injected = list(self.fake.snapshot_fail)
        reported = Counter()
        for msg in take_problems:
            hits = [full for full in injected if full in msg]
            if hits:
                reported.update(hits)
            else:
                self.fail("problem", now, f"take: {msg}")
        for full in injected:
            if reported[full] != 1:
                self.fail("problem", now, f"the injected failure of {full} was reported {reported[full]} times")
        for msg in prune_problems:
            self.fail("problem", now, f"prune: {msg}")
        destroys = Counter()
        for line in stderr.splitlines():
            if line.startswith(("CRITICAL ERROR: ", "ERROR: ")):
                continue  # each also went into a problems list, checked above
            # by prefix: a snapshot name holds colons
            hit = next((full for full in self.fake.destroy_fail if line.startswith(f"could not remove {full} : ")), None)
            if hit is not None:
                destroys[hit] += 1
            elif line.strip():
                self.fail("problem", now, f"stderr: {line}")
        for full in self.fake.destroy_fail:
            if destroys[full] != 1:
                self.fail("problem", now, f"the injected failure of {full} was reported {destroys[full]} times")

    # -- upstream agreement --------------------------------------------------

    def perl_due(self, run: Run, prev: Run | None) -> list[Run]:
        mode = self.sc.perl_checks
        due = [run] if mode == "all" or self.changes.near(run.epoch, 3 * HOUR) else []
        events = {kind for kind, fulls in (("take", run.taken), ("prune", run.pruned))
                  if any(type_of(full.split("@", 1)[1]) in DAILY_PLUS for full in fulls)}
        if events:
            if mode in ("all", "events") or self.changes.near(run.epoch, 3 * DAY):
                wanted = True
            else:
                keys = {(run.now.year, run.now.month, kind) for kind in events}
                wanted = bool(keys - self.month_firsts)
                self.month_firsts |= keys
            if wanted:
                due = [prev, run] if prev is not None else [run]
        return [r for r in due if not r.checked]

    def perl_check(self, run: Run) -> None:
        run.checked = True
        wd = self.scratch / "perl"
        stamp = f"autosnap_{run.now:%Y-%m-%d_%H:%M:%S}"
        exists: dict[str, dict[str, str]] = {}
        for ds, names in run.pre_take.items():
            for name in names:
                if name.startswith(stamp):
                    for target in [ds, *(r for r in self.zfs_parents if ds.startswith(r + "/"))]:
                        exists[f"{target}@{name}"] = {"stderr": f"cannot create snapshot '{ds}@{name}': dataset already exists\n"}
        ages = defaultdict(dict)
        for (ds, t), made_at in newest_by_type(run.pre_take).items():
            ages[ds][t] = run.epoch - made_at
        take_scn = {"config": self.config, "snapsbytype": dict(ages), "now_epoch": run.epoch, "tz": self.sc.tz,
                    "verbose": False, "readonly": False, "tree": self.tree,
                    "snapshot_fail": {**exists, **run.snapshot_fail}, "fail_start": ""}
        self.perl["take"] += 1
        try:
            upstream = set(diff_take_snapshots.run_perl(wd / "take", take_scn)["taken"])
            self.compare_take(run, set(run.taken), upstream)
        except Exception as e:  # noqa: BLE001 - a harness failure is a finding, not the end of the run
            self.fail("perl harness", run.now, f"take: {e}")

        by_type: dict[str, dict[str, list[tuple[int, str]]]] = defaultdict(lambda: defaultdict(list))
        for ds, names in run.prune_view.items():
            for name, made_at in names.items():
                by_type[ds][type_of(name)].append((made_at, name))
        prune_scn = {
            "config": self.config,
            "snaps": {ds: {n: {"ctime": c} for n, c in names.items()} for ds, names in run.prune_view.items() if names},
            "snapsbytype": {ds: {t: {"sorted": [n for _, n in sorted(v)]} for t, v in types.items()}
                            for ds, types in by_type.items()},
            "pool_caps": run.caps, "verbose": False, "readonly": False, "lock_free": True, "tree": self.tree,
            "destroy_fail": run.destroy_fail, "now_epoch": run.epoch,
        }
        self.perl["prune"] += 1
        try:
            upstream_pruned = diff_prune.run_perl(wd / "prune", prune_scn)["pruned"]
            if sorted(run.pruned) == upstream_pruned:
                self.perl["prune same"] += 1
            else:
                self.fail("perl prune", run.now, f"pysanoid {sorted(run.pruned)}, upstream {upstream_pruned}")
        except Exception as e:  # noqa: BLE001
            self.fail("perl harness", run.now, f"prune: {e}")

    def compare_take(self, run: Run, ours: set[str], upstream: set[str]) -> None:
        if ours == upstream:
            self.perl["take same"] += 1
            return
        unexplained = []
        for full in sorted(ours ^ upstream):
            side = "pysanoid" if full in ours else "upstream"
            why = self.explain(run, full, side)
            if why:
                why = f"{why} [{side} only]"
                self.known[why] += 1
                self.known_runs[why].add(run.epoch)
            else:
                unexplained.append(f"{full} taken by {side} only")
        if unexplained:
            self.fail("perl take", run.now, "; ".join(unexplained))
        else:
            self.perl["take documented"] += 1

    def explain(self, run: Run, full: str, side: str) -> str | None:
        """The documented take differences: the dst_ retry (DEV-029) and upstream's DST arithmetic (DEV-030)."""
        ds, name = full.split("@", 1)
        t, now, secs = type_of(name), run.now, self.by_path.get(ds, [])
        if side == "pysanoid" and name.endswith(f"dst_{t}"):
            plain = f"{name[:-len(t) - 4]}_{t}"
            if any(r.split("@", 1)[1] == plain for r in run.refused):
                return "the dst_ retry of a name zfs refused (upstream keeps it out of %taken, or doesn't retry)"
        if t == "frequently" and Changes.repeated(now):
            return "frequently in a fall-back's repeated hour"
        if t == "hourly" and self.changes.on_day(now, "back") and any(now.minute < s.setting("hourly_min", 0) for s in secs):
            return "hourly below hourly_min on a fall-back day"
        if t == "daily" and self.changes.on_day(now, "forward"):
            return "daily on a spring-forward day"
        if t == "weekly" and self.changes.near(run.epoch, 3 * DAY):
            return "weekly within 3 days of a clock change"
        if t in ("monthly", "yearly") and any(configured_ahead(now, s, t) for s in secs):
            return f"{t} while its configured day is still ahead"
        return None

    # -- report --------------------------------------------------------------

    @property
    def ok(self) -> bool:
        return not any(self.findings.values())

    def report(self) -> None:
        sc, rule = self.sc, "=" * 78
        span = (sc.end - sc.start).days
        how = "take and prune as two runs" if sc.split else "take and prune in one run (--cron)"
        conf = sc.conf.name if sc.conf else "FIXTURE_CONF"
        print(f"{rule}\n{sc.name}: {conf}, {sc.tz}, {sc.start} to {sc.end} ({span} days), every {sc.tick_minutes} "
              f"minutes, {how}\n{rule}")
        print(f"datasets: {len(self.tree)} in the tree ({', '.join(self.extra) or 'none'} beyond the config's); "
              f"sections after recursion: {len(self.sections)}")
        for epoch, kind, size in self.changes.at:
            before = datetime.fromtimestamp(epoch - 1, self.tz)
            after = datetime.fromtimestamp(epoch, self.tz)
            print(f"clock change: {before:%Y-%m-%d %H:%M:%S %Z} -> {after:%H:%M %Z} ({kind}, {size // 60} minutes)")
        if self.unchecked:
            print(f"not checked (a count or frequent_period pysanoid refuses): {', '.join(self.unchecked)}")
        fake = self.fake
        print(f"runs: {self.runs} | taken {self.taken} | destroyed {self.destroyed} | injected failures: "
              f"{fake.injected_snapshot_failures} snapshot, {fake.injected_destroy_failures} destroy"
              + (f" (1 in {1 / sc.fail_rate:g}, seed {sc.fail_seed})" if sc.fail_rate else ""))
        for what, counts in self.by_type.items():
            print(f"  {what} (with -r descendants)" if what == "taken" else f"  {what}", end=": ")
            print(", ".join(f"{t} {counts[t]}" for t in p.PERIOD_TYPES if counts[t]) or "none")
        print("failed checks: " + " | ".join(f"{c} {len(self.findings[c])}" for c in CATEGORIES))
        perl = self.perl
        print(f"upstream (Perl, {sc.perl_checks}): {perl['take']} runs; take {perl['take same']} the same + "
              f"{perl['take documented']} differing only as documented, prune {perl['prune same']} the same")
        for why, count in sorted(self.known.items()):
            print(f"  documented: {why}: {count} difference(s) in {len(self.known_runs[why])} run(s)")
        if self.dst_names:
            print(f"dst_ snapshots taken: {', '.join(self.dst_names[:6])}"
                  + (f" (+{len(self.dst_names) - 6} more)" if len(self.dst_names) > 6 else ""))
        for c in CATEGORIES:
            if self.findings[c]:
                print(f"\n--- {c} (first 10 of {len(self.findings[c])}) ---")
                for item in self.findings[c][:10]:
                    print(f"  {item}")
        print("\ncadence, hours between consecutive snapshots of a type: n, min, mean, max")
        for (ds, t), gaps in sorted(self.gaps.items()):
            hours = [g / HOUR for g in gaps]
            print(f"  {ds:32s} {t:10s} {len(hours):6d} {min(hours):9.3f} {statistics.mean(hours):9.3f} {max(hours):9.3f}")
        print("PASS" if self.ok else "FAIL")


def run_scenario(sc: Scenario, scratch: Path, out: Path | None = None) -> bool:
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    conf = sc.conf
    if conf is None:
        conf = scratch / "fixture.conf"
        conf.write_text(sc.conf_text or "")
    sim = Simulation(sc, conf, scratch)
    try:
        sim.load()
        t0 = time_module.monotonic()
        sim.run()
        elapsed = time_module.monotonic() - t0
    except p.SanoidError as e:
        print(f"{sc.name}: FATAL: {e}\nFAIL")
        return False
    finally:
        sim.restore()
    sim.report()
    print(f"({elapsed:.0f} s)")
    if out is not None:
        out.write_text(json.dumps(sim.series))
    return sim.ok


def run_builtin(mode: str, args: list[int]) -> int:
    """Each scenario in its own process (they patch pysanoid's module), side by side."""
    started = []
    for sc in builtin(mode, *args[:2]):
        log = WORK / f"{sc.name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as fh:
            cmd = [sys.executable, str(Path(__file__).resolve()), mode, *map(str, args), "--only", sc.name]
            started.append((sc.name, log, subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, cwd=DEV_DIR)))
    results = []
    for name, log, proc in started:
        proc.wait()
        print(log.read_text(), end="\n", flush=True)
        results.append((name, proc.returncode == 0))
    print("=" * 78)
    for name, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    return 0 if all(passed for _, passed in results) else 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=("check", "long"), help="run the built-in scenarios instead of --conf")
    ap.add_argument("mode_args", nargs="*", type=int, metavar="N", help="long: n (1-in-n zfs failures, default 1000), seed")
    ap.add_argument("--only", help=argparse.SUPPRESS)
    ap.add_argument("--conf", type=Path, help="a sanoid.conf to simulate")
    ap.add_argument("--tz", default="Europe/Zurich")
    ap.add_argument("--start", type=date.fromisoformat, help="first day, YYYY-MM-DD (default: today)")
    ap.add_argument("--years", type=int, default=6)
    ap.add_argument("--days", type=int, default=None, help="the window in days, instead of --years")
    ap.add_argument("--tick-minutes", type=int, default=15)
    ap.add_argument("--split-take-prune", action="store_true",
                    help="take and prune as two runs, prune listing the pool afresh (the two services)")
    ap.add_argument("--pool-cap", action="append", default=[], metavar="POOL=PCT[+GROWTH]",
                    help="a pool's capacity, e.g. tank=78, or tank=78+1.2 for 1.2%% a year more (repeatable; "
                         "other pools 0%%). Without any, every pool follows CERN's ramp")
    ap.add_argument("--dataset", action="append", default=[], metavar="NAME",
                    help="a dataset besides the config's, e.g. a recursive section's child (repeatable)")
    ap.add_argument("--datasets", type=Path, metavar="FILE", help="more datasets, one per line (`zfs list -H -o name`)")
    ap.add_argument("--fail-rate", type=float, default=0.0, help="per-call chance (0..1) of a failed snapshot or destroy")
    ap.add_argument("--fail-seed", type=int, default=1)
    ap.add_argument("--perl-checks", choices=("sampled", "events", "all", "none"), default="sampled",
                    help="sampled (default), every run with a daily-or-longer event (events), every run (all), none")
    ap.add_argument("--scratch", type=Path, default=WORK / "manual")
    ap.add_argument("--out", type=Path, help="write each dataset's snapshot count at every local midnight here (JSON)")
    args = ap.parse_args()

    if args.mode:
        if args.only is None:
            sys.exit(run_builtin(args.mode, args.mode_args))
        (sc,) = [s for s in builtin(args.mode, *args.mode_args[:2]) if s.name == args.only]
        sys.exit(0 if run_scenario(sc, WORK / sc.name) else 1)
    if args.conf is None:
        ap.error("give --conf, or a mode (check, long)")

    tz = ZoneInfo(args.tz)
    start = args.start or datetime.now(tz).date()
    if args.days is not None:
        end = start + timedelta(days=args.days)
    else:
        try:
            end = start.replace(year=start.year + args.years)
        except ValueError:  # 29 February
            end = start.replace(year=start.year + args.years, day=28)
    capacity = None
    if args.pool_cap:
        capacity = {}
        for entry in args.pool_cap:
            pool, _, rhs = entry.partition("=")
            if not rhs:
                ap.error(f"--pool-cap wants POOL=PCT or POOL=PCT+GROWTH, got {entry!r}")
            pct, _, growth = rhs.partition("+")
            capacity[pool] = (float(pct), float(growth) if growth else 0.0)
    datasets = list(args.dataset)
    if args.datasets:
        datasets += [line.split()[0] for line in args.datasets.read_text().splitlines() if line.strip()]
    sc = Scenario("manual", tz=args.tz, start=start, end=end, tick_minutes=args.tick_minutes,
                  split=args.split_take_prune, conf=args.conf.resolve(), pool_capacity=capacity, datasets=datasets,
                  fail_rate=args.fail_rate, fail_seed=args.fail_seed, perl_checks=args.perl_checks)
    sys.exit(0 if run_scenario(sc, args.scratch.resolve(), args.out) else 1)


if __name__ == "__main__":
    main()
