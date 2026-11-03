#!/usr/bin/env python3
"""Run some or all of the differential tests in one command.

    python3 dev/run.py                 # hand cases for everything (about 2 min)
    python3 dev/run.py --fuzz          # hand cases + a moderate fuzz pass for everything (15 to 20 min)
    python3 dev/run.py --fuzz -n 2000  # hand cases + a bigger fuzz pass
    python3 dev/run.py --fuzz -n 400 --seed 7   # a scenario count and seed for every target
    python3 dev/run.py snapshot_cache_mutation dataset_cache   # just these two, by short name
    python3 dev/run.py --list          # show the short names

Each target is one dev/diff_*.py, plus dev/sweep_dst.py (take's preferred times across DST changes,
checked against a brute-force answer; its "wide" mode runs as the fuzz pass),
dev/simulate_long_horizon.py (take and prune run after run, on a production-shaped config and a fixture: fixed-date
windows around clock changes, and years as the fuzz pass, with injected zfs failures), and three that run
pysanoid as a whole program on a pool held in memory and judge it by what its config promises, not by
upstream: dev/sweep_faults.py (every file operation and zfs call of a run failed in turn), dev/fuzz_configs.py
(configs with odd values in every setting) and dev/simulate_trouble.py (run after run through kills, clock
steps and missed runs; its fuzz pass draws random configs), and dev/check_overlap.py (two runs at once on one pool,
judged by rules of its own). See each script's own
module docstring for what it covers and why. This driver just sequences them and summarizes
pass/fail -- it doesn't replace reading a script's own output when something fails, which is where
the actual scenario and both sides' values are shown.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent

# (short name, script, whether it takes a "hand"/"fuzz" mode or "cases"/"fuzz")
TARGETS = [
    ("ini_grammar", "diff_ini_grammar.py", ("hand", "fuzz")),
    ("config_merge", "diff_config_merge.py", ("cases", "fuzz")),
    ("snapshot_enumeration", "diff_snapshot_enumeration.py", ("hand", "fuzz")),
    ("snapshot_cache_mutation", "diff_snapshot_cache_mutation.py", ("hand", "fuzz")),
    ("dataset_cache", "diff_dataset_cache.py", ("hand", "fuzz")),
    ("options", "diff_options.py", ("hand", "fuzz")),
    ("flow", "diff_flow.py", ("hand", "fuzz")),
    ("debug_dump", "diff_debug_dump.py", ("hand", "fuzz")),
    ("hooks", "diff_hooks.py", ("hand", "fuzz")),
    ("prune", "diff_prune.py", ("hand", "fuzz")),
    ("zpool_capacity", "diff_zpool_capacity.py", ("hand", "fuzz")),
    ("zfs_output", "diff_zfs_output.py", ("hand", "fuzz")),
    ("whole_run", "diff_whole_run.py", ("hand", "fuzz")),
    ("overlap", "check_overlap.py", ("check", "fuzz")),
    ("take_snapshots", "diff_take_snapshots.py", ("hand", "fuzz")),
    ("perl_number", "diff_perl_number.py", ("hand", "fuzz")),
    ("dst_schedule", "sweep_dst.py", ("check", "wide")),
    ("long_horizon", "simulate_long_horizon.py", ("check", "long")),
    ("fault_sweep", "sweep_faults.py", ("check", "full")),
    ("config_fuzz", "fuzz_configs.py", ("check", "fuzz")),
    ("trouble", "simulate_trouble.py", ("check", "random")),
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="short names to run (default: all)")
    ap.add_argument("--fuzz", action="store_true", help="also run each target's fuzz mode")
    ap.add_argument("-n", type=int, default=None, help="fuzz scenario count per target (default: each script's own)")
    ap.add_argument("--seed", type=int, default=None, help="fuzz seed, with -n (default: each script's own, 1)")
    ap.add_argument("--list", action="store_true", help="list short names and exit")
    args = ap.parse_args()
    if args.seed is not None and args.n is None:
        ap.error("--seed needs -n: each script reads its scenario count before its seed")

    if args.list:
        for name, script, _ in TARGETS:
            print(f"  {name:26} {script}")
        return

    wanted = {n for n in args.names} or {n for n, _, _ in TARGETS}
    unknown = wanted - {n for n, _, _ in TARGETS}
    if unknown:
        sys.exit(f"unknown target(s): {', '.join(sorted(unknown))} -- see --list")

    results: list[tuple[str, str, bool]] = []
    for name, script, (hand_mode, fuzz_mode) in TARGETS:
        if name not in wanted:
            continue
        modes = [hand_mode] + ([fuzz_mode] if args.fuzz else [])
        for mode in modes:
            cmd = [sys.executable, str(DEV_DIR / script), mode]
            if mode == fuzz_mode and args.n is not None:
                # positional n, then seed: a seed alone would be read as the scenario count
                cmd += [str(args.n)] + ([str(args.seed)] if args.seed is not None else [])
            print(f"\n{'=' * 70}\n{name} ({mode})\n{'=' * 70}", flush=True)
            r = subprocess.run(cmd, cwd=DEV_DIR, check=False)
            results.append((name, mode, r.returncode == 0))

    print(f"\n{'=' * 70}\nsummary\n{'=' * 70}")
    ok = True
    for name, mode, passed in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name} ({mode})")
        ok = ok and passed
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
