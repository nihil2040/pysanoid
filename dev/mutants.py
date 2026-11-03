#!/usr/bin/env python3
"""Checks the checks: pysanoid.py (or pysanoid_alert.py) broken on purpose, one line at a time, and each broken copy
given to what should catch it. Every break must fail it, or the rule it breaks is one that nothing checks any more.

Four suites:
  targets  the breaks below, given to the three targets that judge a run by what its config promises
           (dev/sweep_faults.py, dev/fuzz_configs.py and dev/simulate_trouble.py): their hand passes and a random
           pass of 60 scenarios. Run it after changing lib/world.py, lib/model.py, lib/audit.py or one of the three
           targets, and add a break with every rule. About half an hour.
  tests    lib/breaks.py's TESTS, each given to test_pysanoid.py (pytest -x). Run it after changing the tests.
  alert    lib/breaks.py's ALERT, each given to test_pysanoid_alert.py.
  harness  lib/breaks.py's HARNESS, each given to the hand pass of the dev/run.py target it names.

Each break is a piece of the file and what to put in its place. The piece must be there exactly once: when the file
has changed and it isn't, the break is reported as not caught, so this can't pass by going stale; nor can a break that
no longer compiles. Each runs in a copy of the tree under dev/work. Not a dev/run.py target: it tests the tools and the
tests, not pysanoid.

  python3 dev/mutants.py                # the targets suite
  python3 dev/mutants.py 10 13          # its 11th to 13th
  python3 dev/mutants.py tests          # a suite by name, or all four: python3 dev/mutants.py all
  python3 dev/mutants.py alert 0 5      # a suite's first five
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
from lib import breaks

REPO = DEV_DIR.parent
COPY = DEV_DIR / "work" / f"mutants-{os.getpid()}"  # one per run, so two suites can run at once
PASSES = [("fault_sweep", ["dev/sweep_faults.py", "check"]), ("config_fuzz", ["dev/fuzz_configs.py", "check"]),
          ("trouble", ["dev/simulate_trouble.py", "check"]), ("trouble, random", ["dev/simulate_trouble.py", "random", "60"])]

# (what the break is, the line, the line in its place)
MUTANTS = [
    ("prune goes below the count", "        if ctime < max_age and remaining > min_keep:", "        if ctime < max_age:"),
    ("prune ignores the window", "        if ctime < max_age and remaining > min_keep:", "        if remaining > min_keep:"),
    ("prune keeps one too many", "        if ctime < max_age and remaining > min_keep:", "        if ctime < max_age and remaining > min_keep + 1:"),
    ("prune destroys the snapshot on the boundary", "        if ctime < max_age and remaining > min_keep:",
     "        if ctime <= max_age and remaining > min_keep:"),
    ("prune ignores prune_defer", "    return limit >= pool_capacity_cache[pool]", "    return False"),
    ("prune doesn't check its settings first", "    _preflight_prune_config(config)\n\n    pruned: list[str] = []", "    pruned: list[str] = []"),
    ("take: due again at the slot's own second", "            if newest_age > max_age:", "            if newest_age >= max_age:"),
    ("DEV-051: snapshots dated ahead counted, as upstream", "        snaps = without_snapshots_dated_ahead(snaps)\n",
     "        snaps = dict(snaps)\n"),
    ("take: names in UTC", '    return f"autosnap_{now:%Y-%m-%d_%H:%M:%S}_{snap_type}"',
     '    return f"autosnap_{now.astimezone(UTC):%Y-%m-%d_%H:%M:%S}_{snap_type}"'),
    ("take: no dst_ retry", '            if status != 0 and b"already exists" in complaint:', "            if False:"),
    ("take: a failed snapshot isn't a problem", "            problems.append(failure)", "            pass"),
    ("--readonly takes", "            if readonly:\n                continue\n\n            status, complaint",
     "            if False:\n                continue\n\n            status, complaint"),
    ("--readonly destroys", "                    if not readonly:\n                        try:", "                    if True:\n                        try:"),
    ("a snapshot cache dated ahead is read", "    text = _read_cache(cache) if age is not None and 0 <= age <= cache_ttl else None",
     "    text = _read_cache(cache) if age is not None and age <= cache_ttl else None"),
    ("a dataset cache dated ahead is read", "        if not 0 <= age <= self.cache_ttl:", "        if age > self.cache_ttl:"),
    ("the snapshot cache lasts twice its TTL", "    text = _read_cache(cache) if age is not None and 0 <= age <= cache_ttl else None",
     "    text = _read_cache(cache) if age is not None and 0 <= age <= 2 * cache_ttl else None"),
    ("taken snapshots aren't added to the cache", "    if not taken:\n        return", "    if True:\n        return"),
    ("destroyed snapshots stay in the cache", "    if not pruned:\n        return True\n    cache = cache_dir",
     "    if True:\n        return True\n    cache = cache_dir"),
    ("a cache that can't be written ends the run", "        _ = tmp.rename(cache)\n    except OSError as e:",
     "        _ = tmp.rename(cache)\n    except PermissionError as e:"),
    ("a listing's cut line is kept", "        _ = lines.pop()  # cut short (DEV-035)", "        pass"),
    ("a short child listing is taken for a whole one", "        if proc.returncode < 0 or (proc.returncode > 0 and proc.stdout):", "        if False:"),
    ("a child section its lookup didn't list starts from the defaults",
     "            unlisted = [] if is_skip_children and not is_zfs_recursive else [", "            unlisted = [] if True else ["),
    ("a count of thousands of digits is read whole", '    return int(digits or "0") if len(digits) <= 18 else _COUNT_MAX',
     '    return int(digits or "0")'),
    ("a line of pysanoid's own on stderr", '    if verbose:\n        print("INFO: taking snapshots...")',
     '    print("taking snapshots", file=sys.stderr)'),
]


def failed(output: str) -> str:
    """The checks a pass's output says failed."""
    kinds = {part.strip().rsplit(" ", 1)[0] for line in output.splitlines() if line.startswith("failed checks: ")
             for part in line.removeprefix("failed checks: ").split("|") if not part.strip().endswith(" 0")}
    return ", ".join(sorted(kinds)) or "its own clean rounds"


ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}  # a stale .pyc of a break the same size would run the old code
PYTEST = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"]


def _targets_check(_: str) -> list[str]:
    caught = []
    for label, cmd in PASSES:
        r = subprocess.run([sys.executable, *cmd], cwd=COPY, capture_output=True, text=True, errors="replace", env=ENV,
                           check=False)
        if r.returncode != 0:
            caught.append(f"{label} ({failed(r.stdout)})")
    return caught


def _pytest_check(test_file: str):
    def check(_: str) -> list[str]:
        r = subprocess.run([*PYTEST, test_file], cwd=COPY, capture_output=True, text=True, errors="replace", env=ENV,
                           check=False)
        first = next((line.split("::", 1)[1].split(" ")[0] for line in r.stdout.splitlines() if line.startswith("FAILED")), "")
        return [first or f"pytest exit {r.returncode}"] if r.returncode != 0 else []
    return check


def _harness_check(target: str) -> list[str]:
    r = subprocess.run([sys.executable, "dev/run.py", target], cwd=COPY, capture_output=True, text=True,
                       errors="replace", env=ENV, check=False)
    return [f"{target} hand pass"] if r.returncode != 0 else []


SUITES = {
    "targets": ("pysanoid.py", [(None, *m) for m in MUTANTS], _targets_check),
    "tests": ("pysanoid.py", [(None, *m) for m in breaks.TESTS], _pytest_check("test_pysanoid.py")),
    "alert": ("pysanoid_alert.py", [(None, *m) for m in breaks.ALERT], _pytest_check("test_pysanoid_alert.py")),
    "harness": ("pysanoid.py", breaks.HARNESS, _harness_check),
}


def run_suite(name: str, first: int, last: int | None) -> list[str]:
    """Each break of the suite in turn; the ones nothing caught."""
    file, entries, check = SUITES[name]
    original = (REPO / file).read_text()
    standing: list[str] = []
    t0 = time.monotonic()
    chosen = entries[first:last]
    print(f"--- {name}: {len(chosen)} breaks of {file}", flush=True)
    for n, (target, what, old, new) in enumerate(chosen, first):
        label = f"{name} {n + 1}. {what}" + (f" [{target}]" if target else "")
        broken = original.replace(old, new)
        if original.count(old) != 1:
            caught, label = [], label + f" [its text is in {file} {original.count(old)} times, not once]"
        else:
            try:
                _ = compile(broken, file, "exec")
            except SyntaxError as e:
                caught, label = [], label + f" [the break doesn't compile: {e.msg}]"
            else:
                _ = (COPY / file).write_text(broken)
                caught = check(target or "")
                _ = (COPY / file).write_text(original)
        print(f"{label}: " + ("; ".join(caught) if caught else "NOT CAUGHT"), flush=True)
        if not caught:
            standing.append(label)
    print(f"{len(chosen) - len(standing)} of {len(chosen)} caught ({time.monotonic() - t0:.0f} s)\n", flush=True)
    return standing


def main() -> None:
    argv = sys.argv[1:]
    names = ["targets"]
    if argv and not argv[0].isdigit():
        names = list(SUITES) if argv[0] == "all" else [argv[0]]
        if not set(names) <= set(SUITES):
            sys.exit(f"unknown suite {argv[0]!r}: one of {', '.join(SUITES)} or all")
        argv = argv[1:]
    first = int(argv[0]) if argv else 0
    last = int(argv[1]) if len(argv) > 1 else None
    shutil.rmtree(COPY, ignore_errors=True)
    _ = shutil.copytree(REPO, COPY, ignore=shutil.ignore_patterns("work", "__pycache__", ".pytest_cache", ".ruff_cache"))
    standing = [label for name in names for label in run_suite(name, first, last)]
    shutil.rmtree(COPY, ignore_errors=True)
    if standing:
        print("not caught:\n" + "\n".join(f"  {label}" for label in standing))
    sys.exit(1 if standing else 0)


if __name__ == "__main__":
    main()
