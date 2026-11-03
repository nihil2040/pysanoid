"""What a config asks of a run, stated apart from pysanoid's take and prune, so that a run can be judged by it.

model_sections reads a merged config (a clean load_config's) into what each section takes and prunes. The rest is
the policy itself, written from its statement and not from pysanoid's code: which snapshots a take at an instant
must create (lib/slots.py's brute-force slots), which a prune at that instant must destroy, and whether a destroy
that happened was allowed. Times are the system's clock as pysanoid sees it: a clock set wrong is judged as set.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pysanoid as p
from lib.slots import ideal

HOUR, DAY = 3600, 86400
# upstream's prune periods (prune_snapshots); frequently's is frequent_period
PERIODS: dict[str, float] = {"hourly": HOUR, "daily": DAY, "weekly": 7 * DAY, "monthly": 31 * DAY, "yearly": 365.25 * DAY}

View = dict[str, dict[str, int]]  # dataset -> snapshot name -> creation time, in the order zfs lists them

# DEV-051, stated here apart from pysanoid: a snapshot dated more than five minutes ahead of the clock is set aside,
# neither holding its type back nor counting toward prune's count, and never destroyed
AHEAD_ALLOWED = 5 * 60


def counts(made: int, now: float) -> bool:
    """Whether a snapshot made at `made` counts at `now` (DEV-051)."""
    return made - now <= AHEAD_ALLOWED

# upstream's type regex, `m/.*_(\w*ly)/`, on names the world makes (short ones)
_TYPE_RE = re.compile(r".*_(\w*ly)", re.ASCII)


@functools.cache
def type_of(name: str) -> str:
    m = _TYPE_RE.match(name)
    return m.group(1) if m else ""


def kind(name: str) -> str | None:
    """The type of a snapshot sanoid counts as its own, by upstream's rule (a name starting `autosnap` and ending in
    a type); None for any other name."""
    tail = name.rsplit("_", 1)[-1]
    return tail if name.startswith("autosnap") and (tail in PERIODS or tail == "frequently") else None


def nest(snaps: dict[str, int]) -> View:
    """{full name: creation} as a View."""
    view: View = {}
    for full, made in snaps.items():
        ds, _, name = full.partition("@")
        view.setdefault(ds, {})[name] = made
    return view


def config_paths(conf: Path) -> list[str]:
    """The dataset each section names (its `path`, else its name), read as load_config reads them, so the
    fake zfs can answer recursion's child lookup during load."""
    ini = p._read_ini(conf, user_editable=True)
    return [p._section_dict(ini, s)["path"] if params.get("path") else s
            for s, params in ini.items() if not s.startswith("template_")]


def dataset_tree(names: list[str]) -> list[str]:
    tree: set[str] = set()
    for ds in names:
        parts = ds.split("/")
        tree.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
    return sorted(tree)


@dataclass
class Section:
    name: str
    path: str
    pool: str  # prune_defer's pool comes from the section NAME, as in prune_snapshots
    takes: dict[str, int]  # type -> count, for the types take acts on
    prunes: dict[str, int]  # type -> count, for the types prune acts on
    periods: dict[str, float]
    defer: int | None  # prune_defer's limit; None: never defers
    cfg: dict[str, Any]

    def setting(self, key: str, default: int) -> int:
        try:
            return p._cfg_int(self.cfg, key, default, self.path)
        except p.SanoidError:
            return default

    def deferred(self, caps: dict[str, int]) -> bool:
        """Whether prune_defer holds this section's pruning back while its pool is this full."""
        return self.defer is not None and self.defer >= caps.get(self.pool, 0)


def model_sections(config: dict[str, dict[str, Any]]) -> tuple[list[Section], list[str]]:
    sections, unchecked = [], []
    for name, cfg in config.items():
        path = cfg["path"]
        periods = dict(PERIODS)
        try:
            periods["frequently"] = 60 * p._cfg_int(cfg, "frequent_period", 15, path)
        except p.SanoidError:
            unchecked.append(f"[{name}] frequently")
        acts = not cfg.get("process_children_only")
        takes, prunes = {}, {}
        for t in p.PERIOD_TYPES:
            if t not in periods:
                continue
            try:
                count = p._cfg_count(cfg, t, 0, path)
            except p.SanoidError:
                unchecked.append(f"[{name}] {t}")
                continue
            if acts and cfg.get("autosnap") and count > 0:
                takes[t] = count
            if acts and cfg.get("autoprune") and t in cfg:
                prunes[t] = count
        raw = str(cfg.get("prune_defer", "0"))
        digits = raw.lstrip("0") or "0"
        # past 100 pysanoid refuses the limit: here it defers at any capacity, so a destroy under it is never allowed
        defer = None if raw == "0" or not raw.isdigit() else int(digits) if len(digits) <= 3 else 101
        sections.append(Section(name, path, name.split("/", 1)[0], takes, prunes, periods, defer, cfg))
    return sections, unchecked


def snapshot_name(now: float, tz: ZoneInfo, t: str) -> str:
    return f"autosnap_{datetime.fromtimestamp(now, tz):%Y-%m-%d_%H:%M:%S}_{t}"


def expected_take(sections: list[Section], tree: list[str], view: View, now: float, tz: ZoneInfo) -> set[str] | None:
    """The snapshots a take run at `now` must create, from the pool as it stands: for every section that takes, each
    type with no snapshot made at or after the type's last slot, under the name of the local time. With zfs recursion
    the name goes to every descendant too; where one of them has it already (the second pass through a repeated
    hour), the dst_ form. A snapshot dated more than five minutes ahead of the clock doesn't count (DEV-051); one
    dated less far ahead holds its type back, as upstream's does.
    None when a type can't be planned (a day the month or year lacks), which callers keep out of their configs."""
    want: set[str] = set()
    for sec in sections:
        if not sec.takes:
            continue
        mine = view.get(sec.path, {})
        targets = [sec.path, *(d for d in tree if d.startswith(f"{sec.path}/"))] if sec.cfg.get("zfs_recursion") else [sec.path]
        for t in sec.takes:
            slot = ideal(int(now), tz, sec.cfg, t)
            if slot is None:
                return None
            if any(made >= slot for name, made in mine.items() if kind(name) == t and counts(made, now)):
                continue
            name = snapshot_name(now, tz, t)
            if any(name in view.get(d, {}) for d in targets):
                name = f"{name[:-len(t) - 1]}dst_{t}"
            want.update(f"{d}@{name}" for d in targets)
    return want


def set_aside(sections: list[Section], view: View, now: float) -> list[tuple[str, str]]:
    """(dataset, type) for every type a section takes that has a snapshot set aside, dated more than five minutes
    ahead of the clock (DEV-051)."""
    return [(sec.path, t) for sec in sections for t in sec.takes
            if any(not counts(made, now) for name, made in view.get(sec.path, {}).items() if kind(name) == t)]


def expected_prune(sections: list[Section], view: View, now: float, caps: dict[str, int]) -> dict[tuple[str, str], list[int]]:
    """{(dataset, type): creation times} of what a prune run at `now` must destroy, from the pool as it stands: of
    each type a section prunes, the oldest snapshots older than count x period, as far as leaves count of the type,
    of those that count (DEV-051).
    Nothing while prune_defer holds the section back. By creation time, not name: which of two snapshots made in the
    same second goes first is not part of the policy."""
    out: dict[tuple[str, str], list[int]] = {}
    for sec in sections:
        if sec.deferred(caps):
            continue
        for t, count in sec.prunes.items():
            made = sorted(m for name, m in view.get(sec.path, {}).items() if kind(name) == t and counts(m, now))
            past = sum(1 for m in made if m < now - count * sec.periods[t])
            n = min(past, len(made) - count)
            if n > 0:
                out[(sec.path, t)] = made[:n]
    return out


def judge_destroys(sections: list[Section], view: View, destroyed: list[str], now: float, caps: dict[str, int]) -> list[str]:
    """What is wrong with the destroys a run made, in the order it made them, from the pool as it stood before: a
    name sanoid doesn't own, a dataset or type no section prunes (or prunes only once its pool is fuller), a snapshot
    no older than count x period, or one that leaves fewer than count of its type that count (DEV-051)."""
    live = {ds: dict(names) for ds, names in view.items()}
    by_path: dict[str, list[Section]] = {}
    for sec in sections:
        by_path.setdefault(sec.path, []).append(sec)
    wrong: list[str] = []
    for full in destroyed:
        ds, _, name = full.partition("@")
        made = live.get(ds, {}).pop(name, None)
        t = kind(name)
        if made is None:
            wrong.append(f"{full} destroyed, but it wasn't there")
        elif t is None:
            wrong.append(f"{full} destroyed: not a snapshot sanoid owns")
        else:
            permitting = [s for s in by_path.get(ds, []) if t in s.prunes and not s.deferred(caps)]
            left = sum(1 for other, m in live.get(ds, {}).items() if kind(other) == t and counts(m, now))
            if not permitting:
                wrong.append(f"{full} destroyed: no section prunes {t} snapshots of {ds} now")
                continue
            if not any(made < now - s.prunes[t] * s.periods[t] for s in permitting):
                windows = ", ".join(f"{s.prunes[t] * s.periods[t] / HOUR:g} h" for s in permitting)
                wrong.append(f"{full} destroyed {(now - made) / HOUR:.2f} h old, inside its window ({windows})")
            if left < min(s.prunes[t] for s in permitting):
                wrong.append(f"{full} destroyed, leaving {left} {t} of {ds}, fewer than {min(s.prunes[t] for s in permitting)}")
    return wrong
