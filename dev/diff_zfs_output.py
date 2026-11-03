#!/usr/bin/env python3
"""Differential test of DEV-047: upstream's whole script, `dev/upstream/sanoid` as it ships, against pysanoid.py, each
run as a program on the same config, cache directory and fake zfs, with stdout and stderr on one pipe, as the journal
gets them. What the two print is compared byte for byte: zfs's and zpool's own words beside each program's lines.
Upstream's zfs writes to the terminal itself; pysanoid reads what it writes and writes it out when the command has
finished. DEV-047 says the bytes are the same and come at the same place among the run's lines; this keeps checking it.

Each scenario has two datasets, each with four dailies of which `daily = 2` keeps two, so a prune destroys two of
each, and takes, prunes or both, verbose or not. What zfs writes: a warning on stderr and a note on stdout for every
snapshot and destroy (each naming the snapshot, so a line can be traced to its dataset), as ASCII or with bytes that
aren't UTF-8; a snapshot that fails with zfs's complaint; a destroy that fails; a configured dataset that isn't there,
so the listing complains and is retried without names; and `prune_defer`, so zpool is asked for a capacity and writes
a warning of its own, with an answer that prunes or defers.

Before comparing, three things are made the same on both sides: the time in a new snapshot's name (the two runs start
a second or so apart), the ` at <script> line N.` Perl adds to a warning (DEV-027), and the order of the datasets'
lines, since upstream takes and prunes in Perl's random hash order (the lines naming no dataset stay where they are,
and each dataset's own lines keep their order). A stand-in `ps` goes first on upstream's PATH: its lock code asks `ps`
for a process's command line, and the sandbox forbids the real one.

A snapshot's stderr before its stdout is left out by construction: upstream runs `zfs snapshot` inside Capture::Tiny's
`tee_stderr`, which passes stderr on through a process of its own, so what reaches the pipe first is a race. In
practice stdout comes first (8 of 8 runs), as pysanoid writes it.

Known classes (each checked by its rule, not ignored):
  - DEV-047, stderr before stdout: a `zfs destroy` that writes its warning before its note. Upstream's zfs writes
    to the terminal itself, so they come in that order; pysanoid writes what it read from stdout first. pysanoid's
    output must be upstream's with each such pair the other way round.
  - DEV-028, a snapshot that fails: upstream exits 0, pysanoid exits 1 and adds its own ERROR lines, which only
    pysanoid writes. Without those lines the two outputs must be the same.
  - P6, a hook that can't start: each program says why in its own words first (pysanoid's `could not run '...'`,
    Perl's `Can't exec ...`). Without those lines the two outputs must be the same.

The hook cases (from 0.1.45's one-off probe: exit statuses, signals, a hook that isn't there, `exit 0`, a script with no
`#!`, a final `2>&1`, each kind of hook) configure one dataset only: a hook's own lines name none, so the stretches
canon() sorts by dataset would otherwise mix. Their hooks are scripts in the run's bin directory, on PATH.

Run: python3 dev/diff_zfs_output.py hand
     python3 dev/diff_zfs_output.py fuzz <n> <seed>
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
from lib.perlrun import PERL, cpan_env

UPSTREAM = DEV_DIR / "upstream" / "sanoid"
PYSANOID = DEV_DIR.parent / "pysanoid.py"
DEFAULTS_CONF = DEV_DIR.parent / "sanoid.defaults.conf"
WORK = DEV_DIR / "work" / "zfs_output"
DATASETS = ["tank/data", "tank/other"]
DAY = 86400

ERR = b"err: a warning about {name}\n"
OUT = b"out: a note about {name}\n"
NOT_UTF8 = b"err: \xff\xfe not UTF-8 about {name}\n"
ZPOOL_WARNING = b"zpool: a warning of its own\n"

DEV_047 = "DEV-047: a destroy's stderr before its stdout, shown the other way round"
DEV_028 = "DEV-028: a snapshot that fails, reported and exiting non-zero"
P6_WHY = "P6: a hook that can't start, why said in each program's own words"
_WHY = re.compile(rb"^(could not run '|Can't exec ).*$\n?", re.MULTILINE)


def _conf(scn: dict[str, Any]) -> str:
    sections = "".join(f"[{d}]\n\tuse_template = t\n" for d in [*(DATASETS[:1] if scn["single"] else DATASETS), *scn["missing"]])
    defer = f"\tprune_defer = {scn['prune_defer']}\n" if scn["prune_defer"] is not None else ""
    return (f"{sections}[template_t]\n\tdaily = 2\n\thourly = 0\n\tfrequently = 0\n\tweekly = 0\n\tmonthly = 0\n"
            f"\tyearly = 0\n\tautosnap = yes\n\tautoprune = yes\n{defer}{scn['hooks']}")


def _state(scn: dict[str, Any], now: int) -> dict[str, Any]:
    snaps = [{"ds": d, "line": f"{d}@autosnap_d{k}_daily\tcreation\t{now - k * DAY - 3600}\t-"}
             for d in DATASETS for k in (4, 3, 2, 1)]
    return {"datasets": ["tank", *DATASETS], "snapshots": snaps, "noise": scn["noise_err"].hex(),
            "noise_out": scn["noise_out"].hex(), "noise_err_first": scn["err_first"],
            "snapshot_fail_dataset": scn["snapshot_fail"],
            "destroy_fail": {name: {"stderr": f"cannot destroy '{name}': dataset is busy\n"}
                             for name in scn["destroy_fail"]}}


def run(side: str, scn: dict[str, Any]) -> dict[str, Any]:
    wd = WORK / side
    shutil.rmtree(wd, ignore_errors=True)
    (wd / "conf").mkdir(parents=True)
    (wd / "bin").mkdir()
    shutil.copy(DEFAULTS_CONF, wd / "conf" / "sanoid.defaults.conf")
    (wd / "conf" / "sanoid.conf").write_text(_conf(scn))
    (wd / "state.json").write_text(json.dumps(_state(scn, int(time.time()))))
    (wd / "zpool.json").write_text(json.dumps({"stdout": scn["capacity"].hex(), "stderr": ZPOOL_WARNING.hex()}))
    (wd / "zfs.log").write_text("")
    (wd / "bin" / "ps").write_text("#!/bin/sh\necho sanoid\n")
    (wd / "bin" / "ps").chmod(0o755)
    (wd / "bin" / "python3").symlink_to(sys.executable)  # for the fake zfs's `#!/usr/bin/env python3`
    for name, body in scn["scripts"].items():
        (wd / "bin" / name).write_text(body.removeprefix("RAW:") if body.startswith("RAW:") else f"#!/bin/sh\n{body}\n")
        (wd / "bin" / name).chmod(0o755)
    env = {**os.environ, "PATH": os.pathsep.join([str(wd / "bin"), str(DEV_DIR / "fake_zfs"), "/usr/bin", "/bin"]),
           "FAKE_ZFS_LOG": str(wd / "zfs.log"), "FAKE_ZFS_STATE": str(wd / "state.json"),
           "FAKE_ZPOOL": str(wd / "zpool.json")}
    opts = ["--configdir", str(wd / "conf"), "--cache-dir", str(wd / "cache"), "--run-dir", str(wd / "run"),
            *scn["flags"], *(["--verbose"] if scn["verbose"] else [])]
    libs = cpan_env()
    argv = ([PERL, "-I", libs["INI_LIB"], "-I", libs["STRINGY_LIB"], str(UPSTREAM)] if side == "perl"
            else [sys.executable, str(PYSANOID)])
    proc = subprocess.run([*argv, *opts], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=wd,
                          timeout=60, check=False)
    calls = [json.loads(line) for line in (wd / "zfs.log").read_text().splitlines() if line]
    return {"rc": proc.returncode, "output": proc.stdout, "calls": calls}


_NEW_NAME = re.compile(rb"autosnap_\d{4}-\d\d-\d\d_\d\d:\d\d:\d\d_")
_PERL_AT = re.compile(rb" at \S+ line \d+\.$")
_NAMED = re.compile(rb"(tank/[\w/]+)@|cannot open '([^']+)'|\((tank/[\w/]+)\)")
_FOLLOWS = (b"INFO: removing destroyed snapshots from cache.",)  # a batch's own flush: its dataset's
_FIRST = (ZPOOL_WARNING.rstrip(b"\n"),)  # asked once per pool, for whichever of its sections comes first


def canon(output: bytes) -> list[bytes]:
    """The output's lines with the new names' time and Perl's ` at <script> line N.` taken out, and each stretch of
    lines between two lines that name no dataset sorted by dataset, stably (see the module docstring). zpool's
    warning leads its stretch: it is written once per pool, before the first of its sections, whichever that is."""
    lines = [_PERL_AT.sub(b"", _NEW_NAME.sub(b"autosnap_<time>_", line)) for line in output.split(b"\n")]
    owners: list[bytes | None] = []
    for line in lines:
        m = _NAMED.search(line)
        owners.append((m.group(1) or m.group(2) or m.group(3)) if m else b"" if line in _FIRST else None)
    for i, line in enumerate(lines):
        if owners[i] is None and line in _FOLLOWS and i:
            owners[i] = owners[i - 1]
    out: list[bytes] = []
    stretch: list[tuple[bytes, bytes]] = []
    for line, owner in zip(lines, owners, strict=True):
        if owner is None:
            out += [text for _, text in sorted(stretch, key=lambda pair: pair[0])]
            stretch = []
            out.append(line)
        else:
            stretch.append((owner, line))
    return out + [text for _, text in sorted(stretch, key=lambda pair: pair[0])]


def _calls(res: dict[str, Any]) -> list[str]:
    return sorted(_NEW_NAME.sub(b"autosnap_<time>_", json.dumps(c).encode()).decode() for c in res["calls"])


def _err_first_as_pysanoid_shows_it(output: bytes, scn: dict[str, Any]) -> bytes:
    """DEV-047's rule: each destroy's stderr line and the stdout line after it, the other way round."""
    for k in (4, 3):
        for d in DATASETS:
            name = f"{d}@autosnap_d{k}_daily".encode()
            err, out = scn["noise_err"].replace(b"{name}", name), scn["noise_out"].replace(b"{name}", name)
            output = output.replace(err + out, out + err)
    return output


_PYSANOID_ONLY = re.compile(rb"^ERROR: .*$\n?", re.MULTILINE)


def compare(scn: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(differences, known classes seen). The known classes are checked by their rules (see the module docstring)."""
    a, b = run("perl", scn), run("python", scn)
    known: list[str] = []
    expected = a["output"]
    if scn["err_first"] and scn["noise_err"] and scn["noise_out"] and "--take-snapshots" not in scn["flags"]:
        expected = _err_first_as_pysanoid_shows_it(expected, scn)
        known.append(DEV_047)
    shown, rc_ok = b["output"], a["rc"] == b["rc"]
    if _WHY.search(expected) or _WHY.search(shown):
        expected, shown = _WHY.sub(b"", expected), _WHY.sub(b"", shown)
        known.append(P6_WHY)
    if scn["snapshot_fail"] and "--prune-snapshots" not in scn["flags"] and a["rc"] == 0 and b["rc"] == 1:
        shown, rc_ok = _PYSANOID_ONLY.sub(b"", shown), True
        known.append(DEV_028)
    diffs = [k for k, same in (("output", canon(expected) == canon(shown)), ("rc", rc_ok),
                               ("calls", _calls(a) == _calls(b))) if not same]
    if diffs:
        print(f"    upstream (rc={a['rc']}):\n" + _show(a["output"]) + f"    pysanoid (rc={b['rc']}):\n" + _show(b["output"]))
    return diffs, known


def _show(output: bytes) -> str:
    return "".join(f"      | {line.decode('utf-8', 'backslashreplace')}\n" for line in output.split(b"\n"))


def S(flags: str = "--cron", *, verbose: bool = True, noise_err: bytes = ERR, noise_out: bytes = OUT,
      err_first: bool = False, snapshot_fail: list[str] | None = None, destroy_fail: list[str] | None = None,
      missing: list[str] | None = None, prune_defer: int | None = None, capacity: bytes = b"95%\n",
      hooks: str = "", scripts: dict[str, str] | None = None) -> dict[str, Any]:
    return {"flags": flags.split(), "verbose": verbose, "noise_err": noise_err, "noise_out": noise_out,
            "err_first": err_first, "snapshot_fail": snapshot_fail or [], "destroy_fail": destroy_fail or [],
            "missing": missing or [], "prune_defer": prune_defer, "capacity": capacity, "single": bool(hooks),
            "hooks": hooks, "scripts": scripts or {}}


HOOK3 = {"hook3": 'echo "the hook says: $*"; echo "the hook warns" >&2; exit 3'}


def H(flags: str, hooks: str, scripts: dict[str, str] | None = None, *, verbose: bool = True) -> dict[str, Any]:
    """A hook case: zfs silent, so each line is a hook's or a program's own."""
    return S(flags, verbose=verbose, noise_err=b"", noise_out=b"", hooks=hooks, scripts=scripts)


HAND = {
    "--cron --verbose, zfs writes a note and a warning on every snapshot and destroy": S(),
    "--cron, not verbose: zfs's words alone": S(verbose=False),
    "--take-snapshots --verbose": S("--take-snapshots"),
    "--prune-snapshots --verbose": S("--prune-snapshots"),
    "a warning only, no note": S(noise_out=b""),
    "a note only, no warning": S(noise_err=b""),
    "zfs silent: each program's own lines alone": S(noise_err=b"", noise_out=b""),
    "a warning in bytes that aren't UTF-8": S(noise_err=NOT_UTF8),
    "a destroy that fails, with zfs's complaint": S(destroy_fail=["tank/data@autosnap_d4_daily"]),
    "a destroy that fails, not verbose": S(verbose=False, destroy_fail=["tank/other@autosnap_d3_daily"]),
    "a configured dataset that isn't there: the listing's complaint and its retry": S(missing=["tank/gone"]),
    "prune_defer: zpool's own warning, and a capacity that prunes": S("--prune-snapshots", prune_defer=90),
    "prune_defer: a capacity that defers": S("--prune-snapshots", prune_defer=90, capacity=b"40%\n"),
    "KNOWN DEV-047: a destroy's warning before its note": S("--prune-snapshots", err_first=True),
    "KNOWN DEV-047: the same, not verbose": S("--prune-snapshots", err_first=True, verbose=False),
    "KNOWN DEV-028: a snapshot that fails, with zfs's complaint": S("--take-snapshots", snapshot_fail=["tank/data"]),
    "KNOWN DEV-028: the same under --cron": S(snapshot_fail=["tank/other"]),
    "pre hook exits 3": H("--take-snapshots", "\tpre_snapshot_script = hook3 tank/a\n", HOOK3),
    "pre hook exits 3, not verbose": H("--take-snapshots", "\tpre_snapshot_script = hook3 tank/a\n", HOOK3, verbose=False),
    "post hook exits 3": H("--take-snapshots", "\tpost_snapshot_script = hook3 tank/a\n", HOOK3),
    "pre and post hooks both fail, force_post_snapshot_script": H(
        "--take-snapshots", "\tpre_snapshot_script = hook3 pre\n\tpost_snapshot_script = hook3 post\n"
        "\tforce_post_snapshot_script = yes\n", HOOK3),
    "pre hook exits 255": H("--take-snapshots", "\tpre_snapshot_script = hook255\n", {"hook255": "exit 255"}),
    "pre hook run by the shell exits 7": H("--take-snapshots", "\tpre_snapshot_script = hook3 a ; exit 7\n", HOOK3),
    "pre hook kills itself with SIGKILL": H("--take-snapshots", "\tpre_snapshot_script = kill -9 $$\n"),
    "pre hook killed by SIGTERM": H("--take-snapshots", "\tpre_snapshot_script = kill -TERM $$\n"),
    "KNOWN P6: pre hook is `exit 0`, words Perl execs itself, which can't start": H(
        "--take-snapshots", "\tpre_snapshot_script = exit 0\n"),
    "KNOWN P6: pre hook whose first word starts with a dash": H("--take-snapshots", "\tpre_snapshot_script = -c hook3\n", HOOK3),
    "pre hook is a script with no #! that exits 4": H(
        "--take-snapshots", "\tpre_snapshot_script = bare tank/a\n", {"bare": 'RAW:echo "bare got: $*"\nexit 4\n'}),
    "pre hook with a final 2>&1 fails": H("--take-snapshots", "\tpre_snapshot_script = hook3 tank/a 2>&1\n", HOOK3),
    "veto hook exits 3: nothing destroyed": H("--prune-snapshots", "\tpre_pruning_script = hook3\n", HOOK3),
    "KNOWN P6: veto hook is `exit 0`, not verbose: nothing destroyed": H(
        "--prune-snapshots", "\tpre_pruning_script = exit 0\n", verbose=False),
    "pruning hook exits 3 after each destroy": H("--prune-snapshots", "\tpruning_script = hook3\n", HOOK3),
    "pruning hook killed, not verbose": H("--prune-snapshots", "\tpruning_script = kill -9 $$\n", verbose=False),
    "KNOWN P6: pre hook isn't there (a bare name)": H(
        "--take-snapshots", "\tpre_snapshot_script = no_such_hook_anywhere tank/a\n"),
    "KNOWN P6: pre hook isn't there (a path)": H("--take-snapshots", "\tpre_snapshot_script = /nonexistent/hook tank/a\n"),
    "KNOWN P6: post hook isn't there": H("--take-snapshots", "\tpost_snapshot_script = no_such_hook_anywhere\n"),
    "KNOWN P6: veto hook isn't there: nothing destroyed": H(
        "--prune-snapshots", "\tpre_pruning_script = no_such_hook_anywhere\n"),
    "KNOWN P6: pruning hook isn't there": H("--prune-snapshots", "\tpruning_script = no_such_hook_anywhere\n"),
}


def gen(r: random.Random) -> dict[str, Any]:
    flags = r.choice(["--cron", "--take-snapshots", "--prune-snapshots"])
    noise = {"none": b"", "ascii": ERR, "not utf-8": NOT_UTF8}
    defer = r.random() < 0.3
    return S(flags, verbose=r.random() < 0.6, noise_err=noise[r.choice(list(noise))], noise_out=r.choice([b"", OUT]),
             err_first=flags == "--prune-snapshots" and r.random() < 0.5,
             snapshot_fail=[r.choice(DATASETS)] if r.random() < 0.2 else [],
             destroy_fail=[f"{r.choice(DATASETS)}@autosnap_d{r.choice((4, 3))}_daily"] if r.random() < 0.2 else [],
             missing=["tank/gone"] if r.random() < 0.15 else [], prune_defer=90 if defer else None,
             capacity=r.choice([b"95%\n", b"40%\n"]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=60)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()
    cases = HAND.items() if args.mode == "hand" else ((f"fuzz #{i}", gen(r)) for r in [random.Random(args.seed)]
                                                       for i in range(args.n))
    tally = {"match": 0, "DIFF": 0}
    known_seen: dict[str, int] = {}
    for name, scn in cases:
        diffs, known = compare(scn)
        tally["match" if not diffs else "DIFF"] += 1
        for k in known:
            known_seen[k] = known_seen.get(k, 0) + 1
        if args.mode == "hand" or diffs:
            print(f"[{'MATCH' if not diffs else 'DIFF '}] {name}" + (f"  -- {diffs}" if diffs else "")
                  + (f"  ({', '.join(known)})" if known and not diffs else ""))
    print(tally, "known classes:", known_seen)
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if tally["DIFF"] else 0)


if __name__ == "__main__":
    main()
