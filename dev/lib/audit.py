"""One run of pysanoid in a World (lib/world.py), judged by what its config asks (lib/model.py).

Whatever happens to a run, nothing may leave main but its return, and no destroy may break the retention: each is
judged against the pool as it stood, by the clock as it stood.

A run nothing was done to, reading the pool as it is, is judged exactly: it creates the snapshots that are due and
no others, under the names of its local time; it destroys what the retention gives up and nothing else; it exits 0,
and its stderr holds zfs's own words and nothing of pysanoid's; and it leaves both caches as a run just then would
read them, saying what the pool holds, so that the run after it reads the pool as it is too. A run that reads a
cache an earlier, failed run left behind is held only to the first rule, and so is the failed run itself (were a
clean run allowed to leave such a cache, every run after it would be let off); if either exits 0, no line of its own
on stderr may be anything but one of upstream's warnings: a destroy that did not happen, a hook that failed, a key
written twice, a child's autosnap setting under zfs recursion. The exact rules need a config whose every setting
reads as written; an auditor made with exact=False keeps to the others.
"""
from __future__ import annotations

import re
from collections import Counter
from zoneinfo import ZoneInfo

from lib.model import Section, expected_prune, expected_take, judge_destroys, kind, nest
from lib.world import (
    Fault,
    Result,
    World,
    dataset_cache,
    run_main,
    sees_the_pool,
    snapshot_cache,
)

CATEGORIES = ("crash", "over-prune", "take", "prune", "output", "cache", "harness")
# pysanoid's own lines on the stderr of a run that exits 0: upstream's warnings, in upstream's words
WARNINGS = re.compile(r"could not remove |could not run |WARN: \w+ failed, -?\d+$|WARN: \w+ didn't finish in the allowed time!$"
                      r"|duplicate key '.*' in section '.*', using the value from the first occurence and ignoring the others\.$"
                      r"|ignored autosnap configuration for '.*' because it's part of a zfs recursion\.$")


def _some(names: set[str], n: int = 3) -> str:
    return ", ".join(sorted(names)[:n]) + (f" (+{len(names) - n} more)" if len(names) > n else "")


class Auditor:
    def __init__(self, world: World, sections: list[Section], exact: bool = True) -> None:
        self.world, self.sections, self.exactly = world, sections, exact
        self.tz = ZoneInfo(world.tz)
        self.findings: dict[str, list[str]] = {c: [] for c in CATEGORIES}
        self.runs = self.exact = 0

    def fail(self, category: str, tag: str, message: str) -> None:
        self.findings[category].append(f"{tag}: {message}")

    @property
    def ok(self) -> bool:
        return not any(self.findings.values())

    def run(self, tag: str, argv: list[str], now: int, fault: Fault | None = None) -> Result:
        """Runs pysanoid at `now`, leaves the world as the run left it, and judges the run."""
        world = self.world
        before = dict(world.snaps)
        true_view = sees_the_pool(world, self.sections, now)
        r = run_main(world, argv, now, fault)
        world.snaps = r.snaps
        self.runs += 1
        r.exact = self.exactly and r.fired is None and not r.killed and true_view and "--readonly" not in argv
        self.judge(tag, argv, now, before, r)
        return r

    def judge(self, tag: str, argv: list[str], now: int, before: dict[str, int], r: Result) -> None:
        takes = "--cron" in argv or "--take-snapshots" in argv
        prunes = "--cron" in argv or "--prune-snapshots" in argv
        made = [full for full, _ in r.created]
        if r.escaped and not r.killed:
            self.fail("crash", tag, r.escaped.strip().splitlines()[-1])
        elif not r.killed and r.rc not in (0, 1):
            self.fail("crash", tag, f"exit status {r.rc}")
        stood = nest({**before, **dict.fromkeys(made, now)})
        for wrong in judge_destroys(self.sections, stood, r.destroyed, now, self.world.caps):
            self.fail("over-prune", tag, wrong)
        if made and (not takes or "--readonly" in argv):
            self.fail("take", tag, f"a run that takes nothing made {_some(set(made))}")
        if r.destroyed and (not prunes or "--readonly" in argv):
            self.fail("prune", tag, f"a run that prunes nothing destroyed {_some(set(r.destroyed))}")
        if not r.exact:
            if not r.killed and r.rc == 0:
                self.quiet(tag, r)
            return

        self.exact += 1
        view = nest(before)
        if takes:
            want = expected_take(self.sections, self.world.tree, view, now, self.tz)
            if want is None:
                self.fail("harness", tag, "the config has a type that can't be planned at this time")
            elif want != set(made):
                missing, extra = want - set(made), set(made) - want
                self.fail("take", tag, "; ".join(
                    f"{what} {_some(names)}" for what, names in (("not taken:", missing), ("taken, not due:", extra)) if names))
        if prunes:
            want_gone = expected_prune(self.sections, view, now, self.world.caps)
            gone: dict[tuple[str, str], list[int]] = {}
            for full in r.destroyed:
                ds, _, name = full.partition("@")
                gone.setdefault((ds, kind(name) or ""), []).append(before.get(full, -1))
            for key in sorted(set(want_gone) | set(gone)):
                want_made, got_made = want_gone.get(key, []), sorted(gone.get(key, []))
                if want_made != got_made:
                    self.fail("prune", tag, f"{key[1]} of {key[0]}: destroyed {len(got_made)} (made {got_made[:4]}), "
                              f"where the retention gives up {len(want_made)} (made {want_made[:4]})")
        if r.rc != 0 or r.stderr != "".join(r.said):
            ours = [line for line in r.stderr.splitlines() if f"{line}\n" not in r.said]
            self.fail("output", tag, f"exit status {r.rc}, and " + (
                f"on stderr: {ours[0]}" if ours else "stderr as zfs wrote it" if r.stderr == "".join(r.said)
                else "zfs's own words are not on stderr as zfs wrote them"))
        if not (snapshot_cache(self.world, self.sections, now) and dataset_cache(self.world, now)):
            self.fail("cache", tag, "the run left a cache that isn't current, or doesn't say what the pool holds")

    def quiet(self, tag: str, r: Result) -> None:
        """A run that exits 0 and isn't judged exactly: its own lines on stderr may only be upstream's warnings."""
        zfs_said = Counter("".join(r.said).splitlines())
        for line in r.stderr.splitlines():
            if zfs_said[line] > 0:
                zfs_said[line] -= 1
            elif not WARNINGS.match(line):
                self.fail("output", tag, f"exit status 0 after: {line}")
