#!/usr/bin/env python3
"""Black-box smoke tests: invoke pysanoid.py as a REAL subprocess -- real argv, the real
`if __name__ == "__main__"` entry point, a real process exit code -- against a fake `zfs` on
PATH, instead of importing pysanoid and calling its functions in-process like everything else in
this repo does. Nothing else exercises this: not test_pysanoid.py's tests, not any dev/diff_*.py
fuzzer, not dev/simulate_long_horizon.py -- all of them `import pysanoid` and call a function or
`main([...])` directly. That leaves the real fork+exec path -- real `sys.argv`, the early
`os.environ["PATH"] = ... + ":/bin:/sbin"` mutation, the process's own stdout and stderr under
whatever locale and whatever descriptors the child actually gets -- with zero coverage. This
script is that coverage.

Deliberately not a dev/diff_*.py: there's no upstream Perl side here and no fuzzing, just a
handful of hand-picked, real end-to-end invocations checking real exit codes, real stdout/stderr,
real on-disk side effects, and the fake zfs's own call log. Not registered in dev/run.py's TARGETS
for the same reason -- that driver's docstring says "each target is one dev/diff_*.py", and this
isn't one. See dev/README.md.

    python3 dev/e2e_subprocess.py
"""
from __future__ import annotations

import calendar
import json
import os
import resource
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from zoneinfo import TZPATH

DEV_DIR = Path(__file__).resolve().parent
REPO_DIR = DEV_DIR.parent
PYSANOID = REPO_DIR / "pysanoid.py"
DEFAULTS_CONF = REPO_DIR / "sanoid.defaults.conf"

_pass = 0
_fail = 0


def _check(name: str, cond: bool, detail: str = "") -> None:
    global _pass, _fail
    if cond:
        _pass += 1
        print(f"  ok    {name}")
    else:
        _fail += 1
        print(f"  FAIL  {name}" + (f"  -- {detail}" if detail else ""))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _prepare(wd: Path, zfs_state: dict[str, object], bin_dir: Path | None = None,
             env_extra: dict[str, str] | None = None) -> dict[str, str]:
    """The defaults file, the fake zfs's state and log, and the environment a run of pysanoid.py in wd gets."""
    (wd / "conf").mkdir(parents=True, exist_ok=True)
    shutil.copy(DEFAULTS_CONF, wd / "conf" / "sanoid.defaults.conf")
    (wd / "zfs_state.json").write_text(json.dumps(zfs_state))
    (wd / "zfs.log").write_text("")
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)  # stdout to a pipe is block-buffered unless pysanoid says otherwise
    env["PATH"] = os.pathsep.join([*([str(bin_dir)] if bin_dir else []), str(DEV_DIR / "fake_zfs"), env.get("PATH", "")])
    env["FAKE_ZFS_LOG"] = str(wd / "zfs.log")
    env["FAKE_ZFS_STATE"] = str(wd / "zfs_state.json")
    env.update(env_extra or {})
    return env


def _argv(wd: Path, args: list[str]) -> list[str]:
    return [sys.executable, str(PYSANOID),
            "--configdir", str(wd / "conf"), "--cache-dir", str(wd / "cache"), "--run-dir", str(wd / "run"), *args]


def _run(wd: Path, args: list[str], zfs_state: dict[str, object], *, merged: bool = False,
         bin_dir: Path | None = None, output: tuple[int, int] | None = None, env_extra: dict[str, str] | None = None,
         preexec_fn: Callable[[], None] | None = None) -> subprocess.CompletedProcess[str]:
    """Real fork+exec of pysanoid.py -- not an import, not a function call. merged: stderr goes to the stdout pipe, as
    systemd sends both to the journal. bin_dir: searched before dev/fake_zfs, for a zfs wrapper. output: descriptors
    for stdout and stderr instead of pipes (nothing is captured). env_extra: added to the environment. preexec_fn: run in
    the child before pysanoid starts (signal dispositions, resource limits). What it printed is read as pysanoid reads
    zfs: no byte changed, and one that isn't UTF-8 kept (it reads as a lone surrogate here)."""
    env = _prepare(wd, zfs_state, bin_dir, env_extra)
    stdout, stderr = output or (subprocess.PIPE, subprocess.STDOUT if merged else subprocess.PIPE)
    proc = subprocess.run(_argv(wd, args), stdout=stdout, stderr=stderr, timeout=30, env=env, cwd=wd, check=False,
                          preexec_fn=preexec_fn)
    as_text = [None if data is None else data.decode("utf-8", "surrogateescape") for data in (proc.stdout, proc.stderr)]
    return subprocess.CompletedProcess(proc.args, proc.returncode, *as_text)


def _zfs_log(wd: Path) -> list[list[str]]:
    return [json.loads(line) for line in (wd / "zfs.log").read_text().splitlines() if line.strip()]


def _snap_line(ds: str, name: str, ctime: int) -> dict[str, str]:
    return {"ds": ds, "line": f"{ds}@{name}\tcreation\t{ctime}\t-"}


def test_version() -> None:
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _write(wd / "conf" / "sanoid.conf", "")
    proc = _run(wd, ["--version"], {"datasets": [], "snapshots": []})
    _check("--version exits 0", proc.returncode == 0, f"rc={proc.returncode} stderr={proc.stderr!r}")
    _check("--version prints pysanoid + VERSION", "pysanoid" in proc.stdout and any(c.isdigit() for c in proc.stdout),
           proc.stdout)


def test_unknown_flag_is_usage_error() -> None:
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _write(wd / "conf" / "sanoid.conf", "")
    proc = _run(wd, ["--this-flag-does-not-exist"], {"datasets": [], "snapshots": []})
    _check("unknown flag exits 2", proc.returncode == 2, f"rc={proc.returncode}")
    _check("unknown flag prints usage to stderr", "usage" in proc.stderr.lower(), proc.stderr)


def test_negative_cache_ttl_is_a_clean_fatal() -> None:
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _write(wd / "conf" / "sanoid.conf", "")
    proc = _run(wd, ["--take-snapshots", "--cache-ttl=-3"], {"datasets": [], "snapshots": []})
    _check("negative --cache-ttl exits 1", proc.returncode == 1, f"rc={proc.returncode}")
    _check("negative --cache-ttl gives upstream's exact message",
           "ERROR: cache-ttl needs to be positive!" in proc.stderr, proc.stderr)
    _check("negative --cache-ttl prints no Python traceback", "Traceback" not in proc.stderr, proc.stderr)


def _one_dataset_conf(wd: Path, section: str = "tank/data", wanted: int = 5) -> None:
    # every other type pinned to 0 so a fresh run's zfs-call count is exactly predictable
    _write(wd / "conf" / "sanoid.conf",
           f"[{section}]\n\tuse_template = prod\n\n[template_prod]\n\tdaily = {wanted}\n"
           "\thourly = 0\n\tfrequently = 0\n\tweekly = 0\n\tmonthly = 0\n\tyearly = 0\n"
           "\tautosnap = yes\n\tautoprune = yes\n")


def test_fresh_cron_run_takes_one_snapshot_for_real() -> None:
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd)
    proc = _run(wd, ["--cron"], {"datasets": ["tank/data"], "snapshots": []})
    _check("fresh --cron run exits 0", proc.returncode == 0, f"rc={proc.returncode} stderr={proc.stderr!r}")
    calls = _zfs_log(wd)
    snapshot_calls = [c for c in calls if c[0] == "snapshot"]
    destroy_calls = [c for c in calls if c[0] == "destroy"]
    _check("exactly one real `zfs snapshot` call happened", len(snapshot_calls) == 1, str(calls))
    _check("no `zfs destroy` calls on a first-ever run", len(destroy_calls) == 0, str(calls))
    _check("a real cache dir was created on disk", (wd / "cache").is_dir())
    _check("a real run dir was created on disk", (wd / "run").is_dir())


def test_directories_starting_with_whitespace_are_used_as_given() -> None:
    # DEV-025: upstream's two-argument open() drops a leading space its file tests and locks keep, and exits 2 every
    # run before taking anything; pysanoid uses the paths as given, relative to the working directory
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd)
    cache, run = " " + str(wd / "spaced_cache"), " " + str(wd / "spaced_run")
    proc = _run(wd, ["--cache-dir", cache, "--run-dir", run, "--cron"], {"datasets": ["tank/data"], "snapshots": []})
    _check("a --cache-dir and --run-dir with a leading space: the run exits 0", proc.returncode == 0,
           f"rc={proc.returncode} stderr={proc.stderr!r}")
    _check("...and takes its snapshot", len([c for c in _zfs_log(wd) if c[0] == "snapshot"]) == 1, str(_zfs_log(wd)))
    _check("...writing the cache under the directory as given",
           (wd / " " / cache.lstrip().lstrip("/") / "snapshots.txt").is_file(), cache)
    _check("...and the locks there too", (wd / " " / run.lstrip().lstrip("/") / "sanoid_cacheupdate.lock").is_file(), run)
    _check("...and nothing where the space is dropped",
           not (wd / "spaced_cache").exists() and not (wd / "spaced_run").exists())


def test_readonly_cron_makes_zero_real_zfs_calls() -> None:
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd)
    proc = _run(wd, ["--readonly", "--cron"], {"datasets": ["tank/data"], "snapshots": []})
    _check("--readonly --cron exits 0", proc.returncode == 0, f"rc={proc.returncode} stderr={proc.stderr!r}")
    calls = _zfs_log(wd)
    mutating = [c for c in calls if c[0] in ("snapshot", "destroy")]
    _check("--readonly makes zero snapshot/destroy calls, for real, at the process level",
           len(mutating) == 0, str(calls))


def test_one_datasets_take_failure_does_not_block_prune_for_the_other() -> None:
    # tank/bad's every snapshot attempt fails regardless of its timestamp-based name (via the fake
    # zfs's "snapshot_fail_dataset", not "snapshot_fail" -- this is a real subprocess, so there's no
    # in-process `p.time.time` to pin, unlike every diff_*.py, and no reliable way to predict the
    # exact name pysanoid's real wall clock will produce from outside the process).
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _write(wd / "conf" / "sanoid.conf",
           "[tank/bad]\n\tuse_template = prod\n\n"
           "[tank/good]\n\tuse_template = prod\n\n"
           "[template_prod]\n\tdaily = 1\n\thourly = 0\n\tfrequently = 0\n\tweekly = 0\n\tmonthly = 0\n\tyearly = 0\n"
           "\tautosnap = yes\n\tautoprune = yes\n")
    zfs_state: dict[str, object] = {
        "datasets": ["tank/bad", "tank/good"],
        "snapshots": [
            _snap_line("tank/good", "autosnap_2026-09-01_00:00:00_daily", 1756684800),  # ~9 days old
            _snap_line("tank/good", "autosnap_2026-09-02_00:00:00_daily", 1756771200),  # ~8 days old, kept (newest)
        ],
        "snapshot_fail_dataset": ["tank/bad"],
    }
    proc = _run(wd, ["--cron"], zfs_state)
    _check("tank/bad's snapshot failure makes the run exit 1", proc.returncode == 1, f"rc={proc.returncode}")
    _check("the exact problems summary is printed",
           "ERROR: 1 problem(s) this run" in proc.stderr, proc.stderr)
    calls = _zfs_log(wd)
    destroyed = {c[-1] for c in calls if c[0] == "destroy"}
    _check("tank/good's stale extra daily was still destroyed -- prune ran to completion "
           "despite tank/bad's take failure",
           "tank/good@autosnap_2026-09-01_00:00:00_daily" in destroyed, str(calls))
    _check("tank/good's newest daily was kept, not destroyed",
           "tank/good@autosnap_2026-09-02_00:00:00_daily" not in destroyed, str(calls))


def test_an_unusable_run_dir_does_not_stop_taking_snapshots() -> None:
    # DEV-041: a lock file that can't be made is reported and the run goes on. Ending the run at the first lock would
    # leave the snapshot just taken uncached, and once a cache expired none would be taken.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd)
    _write(wd / "run", "")  # a regular file where the run directory should be
    state: dict[str, object] = {"datasets": ["tank/data"], "snapshots": []}
    first = _run(wd, ["--take-snapshots", "--verbose"], state)
    taken = [c[-1] for c in _zfs_log(wd) if c[0] == "snapshot"]
    _check("with no run directory the run still takes its snapshot", len(taken) == 1, first.stdout + first.stderr)
    _check("...exits 1, the problem reported", first.returncode == 1 and "ERROR: could not update" in first.stderr,
           f"rc={first.returncode} stderr={first.stderr!r}")
    _check("...with no Python traceback", "Traceback" not in first.stderr, first.stderr)
    state["snapshots"] = [_snap_line("tank/data", name.split("@", 1)[1], int(time.time())) for name in taken]
    second = _run(wd, ["--take-snapshots", "--verbose"], state)
    retaken = [c for c in _zfs_log(wd) if c[0] == "snapshot"]
    _check("the next run reads zfs and takes nothing again", retaken == [] and second.returncode == 1,
           f"rc={second.returncode} calls={retaken}")


def test_hook_output_stays_in_order_with_pysanoids_own_lines() -> None:
    # Perl flushes STDOUT before each command it starts; block-buffered, pysanoid's lines reached the journal after
    # everything a hook printed
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _write(wd / "conf" / "sanoid.conf",
           "[tank/data]\n\tdaily = 5\n\thourly = 0\n\tfrequently = 0\n\tweekly = 0\n\tmonthly = 0\n\tyearly = 0\n"
           "\tpre_snapshot_script = echo THE HOOK RAN\n")
    proc = _run(wd, ["--take-snapshots", "--verbose"], {"datasets": ["tank/data"], "snapshots": []}, merged=True)
    lines = proc.stdout.splitlines()
    executing = next((i for i, ln in enumerate(lines) if ln.startswith("executing pre_snapshot_script")), None)
    hook = next((i for i, ln in enumerate(lines) if ln == "THE HOOK RAN"), None)
    _check("the hook's output follows the line announcing it, on one pipe",
           executing is not None and hook is not None and executing < hook, proc.stdout)


def test_a_snapshot_cache_holding_a_nul_byte_is_no_cache() -> None:
    # DEV-038. Upstream pruned a damaged line under the name before the NUL, tank/data's newest daily. zfs knows six
    # dailies; daily = 3 keeps the newest three.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd, wanted=3)
    now = int(time.time())
    real = [_snap_line("tank/data", f"autosnap_d{i}_daily", now - int(age * 86400))
            for i, age in enumerate((0.2, 1, 2, 3.5, 4.5, 5.5))]
    damaged = f"tank/data@autosnap_d0_daily{chr(0) * 40}_daily\tcreation\t{now - 9 * 86400}\t-"
    _write(wd / "cache" / "snapshots.txt", "\n".join([*(s["line"] for s in real[:3]), damaged,
                                                        *(s["line"] for s in real[3:])]) + "\n")
    proc = _run(wd, ["--prune-snapshots"], {"datasets": ["tank/data"], "snapshots": real})
    destroyed = [c[-1] for c in _zfs_log(wd) if c[0] == "destroy"]
    _check("a NUL-damaged cache is read as none: the three oldest go, from zfs's own listing",
           destroyed == [f"tank/data@autosnap_d{i}_daily" for i in (5, 4, 3)] and proc.returncode == 0,
           f"rc={proc.returncode} destroyed={destroyed} stderr={proc.stderr!r}")


def test_a_destroy_that_cannot_start_does_not_stop_the_prune() -> None:
    # Parity: upstream's system() returns -1, `could not remove`, and the run goes on. zfs vanishes after the first
    # destroy, so the second can't start.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd, wanted=1)
    bin_dir = wd / "bin"
    _write(bin_dir / "zfs", f'''#!/bin/sh
"{sys.executable}" "{DEV_DIR / 'fake_zfs' / 'zfs'}" "$@"
rc=$?
if [ "$1" = destroy ]; then mv "$0" "$0.hidden"; fi
exit $rc
''')
    (bin_dir / "zfs").chmod(0o755)
    now = int(time.time())
    snaps = [_snap_line("tank/data", f"autosnap_d{i}_daily", now - (5 - i) * 86400) for i in range(4)]
    proc = _run(wd, ["--prune-snapshots"], {"datasets": ["tank/data"], "snapshots": snaps}, bin_dir=bin_dir)
    destroyed = [c[-1] for c in _zfs_log(wd) if c[0] == "destroy"]
    cache = (wd / "cache" / "snapshots.txt").read_text()
    _check("a destroy that can't start is `could not remove`, and the run exits 0",
           proc.returncode == 0 and "could not remove tank/data@autosnap_d1_daily" in proc.stderr
           and "Traceback" not in proc.stderr, f"rc={proc.returncode} stderr={proc.stderr!r}")
    _check("...the one it did destroy leaves the cache", destroyed == ["tank/data@autosnap_d0_daily"]
           and "autosnap_d0_daily" not in cache and "autosnap_d1_daily" in cache, f"destroyed={destroyed} cache={cache!r}")


def _no_spare_process() -> None:
    """RLIMIT_NPROC at 1, so every fork pysanoid makes fails with EAGAIN, as when the system is out of processes."""
    resource.setrlimit(resource.RLIMIT_NPROC, (1, resource.getrlimit(resource.RLIMIT_NPROC)[1]))


def test_a_run_with_no_process_to_spare_stops_at_once() -> None:
    # DEV-043. Upstream's piped open of its `zfs get` retries the fork every 5 s for as long as that lasts, so the run
    # hangs, taking nothing and showing nothing (dev/diff_hooks.py checks that side). pysanoid's listing can't start:
    # FATAL at once, nothing taken, and the unit shows failed.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd, wanted=3)
    started = time.monotonic()
    proc = _run(wd, ["--cron"], {"datasets": ["tank/data"], "snapshots": []}, preexec_fn=_no_spare_process)
    took = time.monotonic() - started
    _check("with no process to spare, the run stops at once with a FATAL, having run nothing",
           proc.returncode == 1 and took < 5 and proc.stderr.startswith("FATAL: could not run ")
           and not (wd / "zfs.log").read_text().strip(), f"rc={proc.returncode} took={took:.1f}s stderr={proc.stderr!r}")


def test_ctrl_c_during_a_hook_stops_the_run() -> None:
    # DEV-044. The hook sends SIGINT to the whole process group, as Ctrl-C at a terminal does. Upstream's system()
    # ignores it while it waits, so only the hook dies and the run goes on to destroy (dev/diff_hooks.py checks that
    # side). pysanoid dies from it: nothing is destroyed, tank/b's dailies included.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _write(wd / "conf" / "sanoid.conf",
           "[tank/a]\n\tdaily = 1\n\thourly = 0\n\tmonthly = 0\n\tscript_timeout = 10\n"
           "\tpre_pruning_script = kill -INT 0; sleep 5\n"
           "[tank/b]\n\tdaily = 1\n\thourly = 0\n\tmonthly = 0\n")
    now = int(time.time())
    snaps = [_snap_line(ds, f"autosnap_d{k}_daily", now - k * 86400 - 600) for ds in ("tank/a", "tank/b") for k in (3, 2, 1)]
    proc = _run(wd, ["--prune-snapshots"], {"datasets": ["tank/a", "tank/b"], "snapshots": snaps}, preexec_fn=os.setsid)
    destroyed = [c[-1] for c in _zfs_log(wd) if c[0] == "destroy"]
    _check("Ctrl-C during a hook stops the run: it dies from SIGINT and destroys nothing",
           proc.returncode == -signal.SIGINT and destroyed == [], f"rc={proc.returncode} destroyed={destroyed}")


def _systemd_signals() -> None:
    """As systemd starts a service (IgnoreSIGPIPE=yes); SIGXFSZ ignored so a file that can't grow fails the write."""
    _ = signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    _ = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)


def test_output_that_cannot_be_written_does_not_stop_the_run() -> None:
    # P7. Perl's print and warn don't fail, so upstream carries on; Python's print would raise, and the run would end
    # at its first line with nothing taken.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd)
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        proc = _run(wd, ["--take-snapshots", "--verbose"], {"datasets": ["tank/data"], "snapshots": []},
                    output=(write_end, write_end), preexec_fn=_systemd_signals)
    finally:
        os.close(write_end)
    taken = [c for c in _zfs_log(wd) if c[0] == "snapshot"]
    _check("with stdout and stderr a closed pipe the run still takes its snapshot, and exits 1",
           len(taken) == 1 and proc.returncode == 1, f"rc={proc.returncode} calls={taken}")
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd)
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        proc = _run(wd, ["--take-snapshots", "--verbose"], {"datasets": ["tank/data"], "snapshots": []},
                    output=(write_end, subprocess.PIPE), preexec_fn=_systemd_signals)
    finally:
        os.close(write_end)
    _check("with only stdout closed, the lost output is reported on stderr, with no Python traceback",
           "ERROR: could not write to stdout" in proc.stderr and "Traceback" not in proc.stderr, proc.stderr)


def test_a_log_that_cannot_grow_does_not_stop_the_run() -> None:
    # A full disk under the log: output appended to a file already at the process's size limit
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd, wanted=1)
    now = int(time.time())
    snaps = [_snap_line("tank/data", f"autosnap_d{i}_daily", now - (4 - i) * 86400 - 3600) for i in range(3)]
    log = wd / "sanoid.log"
    limit = 1 << 20
    _ = log.write_bytes(b"x" * limit)

    def at_the_limit() -> None:
        _systemd_signals()
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, resource.getrlimit(resource.RLIMIT_FSIZE)[1]))

    with open(log, "ab") as fh:
        proc = _run(wd, ["--cron", "--verbose"], {"datasets": ["tank/data"], "snapshots": snaps},
                    output=(fh.fileno(), fh.fileno()), preexec_fn=at_the_limit)
    calls = _zfs_log(wd)
    taken = [c for c in calls if c[0] == "snapshot"]
    destroyed = [c[-1] for c in calls if c[0] == "destroy"]
    _check("with a log that can't grow, --cron still takes and prunes, and exits 1",
           len(taken) == 1 and destroyed == [f"tank/data@autosnap_d{i}_daily" for i in (0, 1)] and proc.returncode == 1,
           f"rc={proc.returncode} taken={taken} destroyed={destroyed}")
    _check("...and the log was left as it was", log.stat().st_size == limit, str(log.stat().st_size))


def test_names_are_written_as_read_whatever_the_locale() -> None:
    # Perl writes the bytes it read; a latin-1 stdout raised UnicodeEncodeError on a name it can't spell, and the
    # datasets after it got no snapshot
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    body = "\tdaily = 5\n\thourly = 0\n\tfrequently = 0\n\tweekly = 0\n\tmonthly = 0\n\tyearly = 0\n"
    name = "tank/\u65e5\u672c"
    _write(wd / "conf" / "sanoid.conf", f"[tank/b]\n{body}[{name}]\n{body}[tank/c]\n{body}")
    proc = _run(wd, ["--take-snapshots", "--verbose"], {"datasets": ["tank/b", "tank/c"], "snapshots": []},
                env_extra={"PYTHONIOENCODING": "latin-1"})
    taken = sorted(c[-1].split("@")[0] for c in _zfs_log(wd) if c[0] == "snapshot")
    _check("under a latin-1 stdout every section is snapshotted, the name written as the bytes read",
           taken == sorted(["tank/b", name, "tank/c"]) and f"taking snapshot {name}@" in proc.stdout
           and "Traceback" not in proc.stderr, f"rc={proc.returncode} taken={taken} stderr={proc.stderr!r}")


def test_an_unsearchable_cache_dir_does_not_stop_the_run() -> None:
    # Through Python 3.13 pathlib's checks raise PermissionError here, where upstream's -f reads false (the run
    # stopped with a traceback, nothing taken). Root searches any directory, so this needs a normal user.
    if os.geteuid() == 0:
        _check("an unsearchable cache dir (skipped: root can search it)", True)
        return
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd)
    (wd / "cache").mkdir()
    (wd / "cache").chmod(0)
    try:
        proc = _run(wd, ["--take-snapshots", "--verbose"], {"datasets": ["tank/data"], "snapshots": []})
    finally:
        (wd / "cache").chmod(0o755)
    taken = [c for c in _zfs_log(wd) if c[0] == "snapshot"]
    _check("a cache directory pysanoid can't search still takes the snapshot, reports, exits 1",
           len(taken) == 1 and proc.returncode == 1 and "Traceback" not in proc.stderr,
           f"rc={proc.returncode} taken={taken} stderr={proc.stderr!r}")


def test_debug_prints_upstreams_trace_and_dump() -> None:
    # stdout and stderr joined, as systemd joins them: upstream's merge trace, a later section's warning where upstream
    # gives it, the child lookup naming zfs bare, then Data::Dumper's dump (each confirmed against the real script)
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _write(wd / "conf" / "sanoid.conf", "[tank]\n\trecursive = yes\n\tdaily = 3\n[tank/b]\n\thourly = 6\n\thourly = 7\n")
    proc = _run(wd, ["--debug"], {"datasets": ["tank", "tank/a", "tank/b"], "snapshots": []}, merged=True)
    lines = proc.stdout.splitlines()
    head = [f"DEBUG: initializing $config{{tank}} with default values from {wd}/conf/sanoid.defaults.conf.",
            "DEBUG: overriding daily on tank with value directly set in module.",
            "DEBUG: getting list of child datasets on tank using  zfs list -o name -t filesystem,volume -Hr tank |..."]
    tail = ["duplicate key 'hourly' in section 'tank/b', using the value from the first occurence and ignoring the others.",
            "DEBUG: overriding hourly on tank/b with value directly set in module.", "****** CONFIGS ******", "$VAR1 = {"]
    at = lines.index(tail[0]) if tail[0] in lines else len(lines)
    copied = {child: {line.split()[3] for line in lines[3:at] if line.endswith(f" to {child}.")} for child in ("tank/a", "tank/b")}
    _check("--debug traces the merge in upstream's order, children's copies in between",
           proc.returncode == 0 and lines[:3] == head and lines[at:at + 4] == tail
           and all(line.startswith("DEBUG: recursively setting ") for line in lines[3:at])
           and copied["tank/a"] == copied["tank/b"] and {"daily", "path", "autoprune"} <= copied["tank/a"],
           f"rc={proc.returncode} output={proc.stdout[:1500]!r}")
    _check("--debug's config dump is Data::Dumper's text",
           "          'tank' => {\n                      'autoprune' => 1,\n" in proc.stdout
           and "                      'daily' => '3',\n" in proc.stdout and "\n        };\n\nFilesystem " in proc.stdout,
           proc.stdout[-1500:])


def test_a_child_section_above_its_recursive_parent_stops_the_run_before_zfs_is_touched() -> None:
    # DEV-005 (upstream #1021): upstream loads this and prunes tank/a by [tank]'s settings
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _write(wd / "conf" / "sanoid.conf", "[tank/a]\n\tdaily = 3\n[tank]\n\trecursive = yes\n\tdaily = 9\n")
    proc = _run(wd, ["--cron"], {"datasets": ["tank", "tank/a"], "snapshots": []})
    _check("a child section above its recursive parent: FATAL, exit 1, before any zfs call",
           proc.returncode == 1 and _zfs_log(wd) == []
           and proc.stderr.startswith(f"FATAL: [tank/a] in {wd}/conf/sanoid.conf must come after [tank]: ")
           and "Traceback" not in proc.stderr, f"rc={proc.returncode} stderr={proc.stderr!r} zfs={_zfs_log(wd)}")


def test_two_sections_on_one_dataset_stop_the_run_before_zfs_is_touched() -> None:
    # DEV-007: upstream loads this and takes and prunes tank/data by both sections
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _write(wd / "conf" / "sanoid.conf", "[tank/data]\n\tdaily = 3\n[offsite]\n\tpath = tank/data\n\tdaily = 3\n")
    proc = _run(wd, ["--cron"], {"datasets": ["tank/data"], "snapshots": []})
    _check("two sections on one dataset: FATAL, exit 1, before any zfs call",
           proc.returncode == 1 and _zfs_log(wd) == []
           and proc.stderr == (f"FATAL: [tank/data] and [offsite] in {wd}/conf/sanoid.conf both take or prune tank/data, "
                               "each by its own settings. Keep one section for tank/data.\n"),
           f"rc={proc.returncode} stderr={proc.stderr!r} zfs={_zfs_log(wd)}")


def test_a_setting_upstream_does_not_know_is_refused_in_its_words() -> None:
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _write(wd / "conf" / "sanoid.conf", "[tank/data]\n\tdialy = 3\n")
    proc = _run(wd, ["--cron"], {"datasets": ["tank/data"], "snapshots": []})
    _check("a setting upstream doesn't know: upstream's `FATAL ERROR: I don't understand ...`, exit 1",
           proc.returncode == 1 and _zfs_log(wd) == [] and proc.stderr == (
               f"FATAL ERROR: I don't understand the setting dialy you've set in [tank/data] in {wd}/conf/sanoid.conf.\n"),
           f"rc={proc.returncode} stderr={proc.stderr!r}")


def _take_with_tz(tz: str) -> tuple[subprocess.CompletedProcess[str] | None, list[str], float]:
    """A take run with TZ set: the process (None if it was still running after 30 s), the snapshot names it chose, and
    how far the time in the one name, read as UTC, is ahead of the clock (0 when there isn't exactly one)."""
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd)
    started = time.time()
    try:
        proc = _run(wd, ["--take-snapshots"], {"datasets": ["tank/data"], "snapshots": []}, env_extra={"TZ": tz})
    except subprocess.TimeoutExpired:
        return None, [], 0.0
    names = [c[-1] for c in _zfs_log(wd) if c[0] == "snapshot"]
    stamp = names[0].split("@autosnap_", 1)[1].rsplit("_", 1)[0] if len(names) == 1 else ""
    try:
        return proc, names, calendar.timegm(time.strptime(stamp, "%Y-%m-%d_%H:%M:%S")) - started
    except ValueError:
        return proc, names, 0.0


def _no_tz_warning(tz: str) -> str:
    return f"WARN: $TZ is set to '{tz}', which is neither a zone nor a POSIX rule pysanoid can read - using UTC.\n"


def test_a_tz_set_but_empty_names_snapshots_in_utc() -> None:
    # the C library, and so upstream, reads a TZ that is set but empty as UTC; 0.1.37 used the system's zone
    proc, names, ahead = _take_with_tz("")
    _check("TZ set but empty: the snapshot is named in UTC", proc is not None and proc.returncode == 0 and abs(ahead) < 120,
           f"rc={proc and proc.returncode} names={names} stderr={proc and proc.stderr!r}")


def test_a_tz_rule_names_snapshots_by_the_rule() -> None:
    # the C library, and so upstream, applies a POSIX rule; 0.1.38 warned and used the system's zone
    proc, names, ahead = _take_with_tz("XST5")
    _check("TZ a POSIX rule (XST5, 5 h behind UTC): the snapshot is named by it, without a warning",
           proc is not None and proc.returncode == 0 and abs(ahead + 5 * 3600) < 120 and proc.stderr == "",
           f"rc={proc and proc.returncode} names={names} stderr={proc and proc.stderr!r}")


def test_a_tz_naming_no_zone_names_snapshots_in_utc_with_a_warning() -> None:
    # the C library, and so upstream, reads UTC; 0.1.38 warned and used the system's zone
    proc, names, ahead = _take_with_tz("Europe/Dubln")
    _check("TZ naming no zone: the snapshot is named in UTC, with a warning",
           proc is not None and proc.returncode == 0 and abs(ahead) < 120 and proc.stderr == _no_tz_warning("Europe/Dubln"),
           f"rc={proc and proc.returncode} names={names} stderr={proc and proc.stderr!r}")


def test_a_damaged_zone_file_does_not_stop_take() -> None:
    # Python 3.11's zoneinfo reads on forever through a zone file cut short in its closing rule line, and reads past its
    # own table when a transition names a type the file lacks: 0.1.38 hung, or crashed, there under 3.11, before any
    # snapshot. glibc, and so upstream, reads UTC.
    dublin = next((Path(d) / "Europe/Dublin" for d in TZPATH if (Path(d) / "Europe/Dublin").is_file()), None)
    if dublin is None:
        _check("a damaged zone file: no zone file for Europe/Dublin to damage", False, str(TZPATH))
        return
    ttinfo = struct.pack(">lbB", 3600, 0, 0) + b"ZZZ\0"
    bad_index = b"".join(b"TZif2" + bytes(15) + struct.pack(">6l", 0, 0, 0, 1, 1, 4) + struct.pack(fmt, 0) + b"\x01" + ttinfo
                         for fmt in (">l", ">q")) + b"\n\n"
    for what, data in (("cut in its closing rule line", dublin.read_bytes()[:-3]),
                       ("whose transition names a type it lacks", bad_index)):
        zone = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_")) / "zone"
        zone.write_bytes(data)
        proc, names, ahead = _take_with_tz(str(zone))
        _check(f"a zone file {what}: take finishes, naming the snapshot in UTC, with a warning",
               proc is not None and proc.returncode == 0 and abs(ahead) < 120 and proc.stderr == _no_tz_warning(str(zone)),
               "still running after 30 s" if proc is None else f"rc={proc.returncode} names={names} stderr={proc.stderr!r}")


def test_take_and_prune_started_together_both_finish() -> None:
    # upstream #1063: take and prune started by cron in the same minute raced on upstream's lock and cache files, and one
    # of them died ("Could not rename to snapshots.txt!") on nearly every run. zfs is slowed down here so each round's
    # listings overlap; whichever way they interleave, both runs must finish cleanly.
    now = int(time.time())
    failures: list[str] = []
    for _ in range(8):
        wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
        _one_dataset_conf(wd, wanted=3)
        env = _prepare(wd, {"datasets": ["tank/data"], "get_sleep": 0.3, "snapshots": [
            _snap_line("tank/data", f"autosnap_d{i}_daily", now - (i + 1) * 86400 - 3600) for i in range(6)]})
        procs = [subprocess.Popen(_argv(wd, [verb]), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
                                  cwd=wd) for verb in ("--take-snapshots", "--prune-snapshots")]
        for proc in procs:
            _, err = proc.communicate(timeout=60)
            if proc.returncode != 0 or any(word in err for word in ("Traceback", "FATAL", "ERROR")):
                failures.append(f"{proc.args[-1]} rc={proc.returncode} stderr={err!r}")
        cache = wd / "cache" / "snapshots.txt"
        lines = cache.read_text().splitlines() if cache.exists() else []
        if not all(len(line.split("\t")) == 4 and line.split("\t")[1] == "creation" for line in lines):
            failures.append(f"a damaged snapshots.txt: {lines!r}")
        destroyed = {call[-1] for call in _zfs_log(wd) if call[0] == "destroy"}
        if not {f"tank/data@autosnap_d{i}_daily" for i in (3, 4, 5)} <= destroyed:
            failures.append(f"prune missed an old daily: {sorted(destroyed)}")
    _check("take and prune started together, 8 times: both finish cleanly, and prune destroys the old dailies",
           not failures, "; ".join(failures[:3]))


def test_a_section_newer_than_the_dataset_cache_keeps_its_parents_settings() -> None:
    # DEV-006. The first run writes datasets.txt. Then tank/c arrives with its history and gets a
    # section of its own, and the next run reads the same datasets.txt, which doesn't list it. Upstream built
    # that section from the defaults (weekly 0, monthly 6, yearly 0) and destroyed 13 of the 19 snapshots its parent keeps.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    parent = "[tank]\n\tuse_template = keep\n\trecursive = yes\n"
    template = ("[template_keep]\n\tfrequently = 0\n\thourly = 0\n\tdaily = 7\n\tweekly = 4\n\tmonthly = 12\n\tyearly = 3\n"
                "\tautosnap = no\n\tautoprune = yes\n")
    _write(wd / "conf" / "sanoid.conf", parent + template)
    first = _run(wd, ["--prune-snapshots"], {"datasets": ["tank", "tank/a"], "snapshots": []})
    (wd / "cache" / "snapshots.txt").unlink()  # the snapshots are listed afresh; the dataset list is the one just written
    now = int(time.time())
    ages = ([("weekly", k * 7 * 86400) for k in range(1, 5)] + [("monthly", k * 31 * 86400) for k in range(1, 13)]
            + [("yearly", int(k * 365.25 * 86400)) for k in range(1, 4)])
    history = [_snap_line("tank/c", f"autosnap_{n}_{kind}", now - age + 3600) for n, (kind, age) in enumerate(ages)]
    _write(wd / "conf" / "sanoid.conf", parent + "[tank/c]\n\tdaily = 30\n" + template)
    second = _run(wd, ["--prune-snapshots"], {"datasets": ["tank", "tank/a", "tank/c"], "snapshots": history})
    calls = _zfs_log(wd)
    destroyed = [c[-1] for c in calls if c[0] == "destroy"]
    _check("a section newer than the dataset cache starts from its recursive parent's settings: nothing is destroyed",
           first.returncode == 0 and second.returncode == 0 and destroyed == [],
           f"rc={first.returncode},{second.returncode} destroyed={destroyed} stderr={second.stderr!r}")
    _check("...and the dataset cache was the one read: zfs wasn't asked for tank's children again",
           not [c for c in calls if c[0] == "list"], f"calls={calls}")


def test_a_snapshot_cache_dated_in_the_future_is_read_afresh() -> None:
    # DEV-037. Written while the clock ran ahead, the file passed upstream's age test until real time reached its date. It still lists two dailies a rollback destroyed; counted toward daily = 3, they
    # let both destroy 2 of the 3 dailies the pool holds.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd, wanted=3)
    now = int(time.time())
    listed = [_snap_line("tank/data", f"autosnap_d{k}_daily", now - k * 86400 - 600) for k in (5, 4, 3, 2, 1)]
    _write(wd / "cache" / "snapshots.txt", "".join(s["line"] + "\n" for s in listed))
    os.utime(wd / "cache" / "snapshots.txt", (now + 365 * 86400, now + 365 * 86400))
    proc = _run(wd, ["--prune-snapshots"], {"datasets": ["tank/data"], "snapshots": listed[:3]})
    calls = _zfs_log(wd)
    destroyed = [c[-1] for c in calls if c[0] == "destroy"]
    _check("a cache dated a year ahead is read afresh: zfs is asked, and none of the pool's three dailies is destroyed",
           proc.returncode == 0 and destroyed == [] and sum(c[0] == "get" for c in calls) == 1,
           f"rc={proc.returncode} destroyed={destroyed} calls={calls} stderr={proc.stderr!r}")


def test_a_snapshot_cache_cut_short_is_no_cache() -> None:
    # DEV-036. The file lost its last 8 bytes, so the newest daily's creation time lost digits and read as decades old:
    # upstream destroyed it and the oldest, and kept one 4 days old.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd, wanted=3)
    now = int(time.time())
    snaps = [_snap_line("tank/data", f"autosnap_d{k}_daily", now - k * 86400 - 600) for k in (5, 4, 3, 2, 1)]
    _write(wd / "cache" / "snapshots.txt", "".join(s["line"] + "\n" for s in snaps)[:-8])
    proc = _run(wd, ["--prune-snapshots"], {"datasets": ["tank/data"], "snapshots": snaps})
    destroyed = [c[-1] for c in _zfs_log(wd) if c[0] == "destroy"]
    _check("a cache cut short inside its last line is read as none: the two oldest dailies go, the newest stays",
           proc.returncode == 0 and destroyed == [f"tank/data@autosnap_d{k}_daily" for k in (5, 4)],
           f"rc={proc.returncode} destroyed={destroyed} stderr={proc.stderr!r}")


def test_a_snapshot_listing_killed_inside_its_last_line_goes_without_that_line() -> None:
    # DEV-035. A configured dataset is away, so the listing with names fails, and the retry is killed 8 bytes short: the
    # newest daily's creation time lost digits and read as decades old. Upstream destroyed it and the oldest. Of the four dailies whose lines are whole, the oldest is the one to go, and the run ends as
    # upstream's does: exit 0, nothing said but zfs's own complaint.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd, wanted=3)
    with (wd / "conf" / "sanoid.conf").open("a") as conf:
        conf.write("[gone/data]\n\tdaily = 3\n\thourly = 0\n\tmonthly = 0\n\tautosnap = no\n")
    now = int(time.time())
    snaps = [_snap_line("tank/data", f"autosnap_d{k}_daily", now - k * 86400 - 600) for k in (5, 4, 3, 2, 1)]
    proc = _run(wd, ["--prune-snapshots"], {"datasets": ["tank/data"], "snapshots": snaps, "retry_cut": 8})
    calls = _zfs_log(wd)
    destroyed = [c[-1] for c in calls if c[0] == "destroy"]
    _check("a listing killed inside its last line goes without that line: the oldest daily goes, the newest stays",
           destroyed == ["tank/data@autosnap_d5_daily"] and sum(c[0] == "get" for c in calls) == 2,
           f"destroyed={destroyed} calls={calls} stderr={proc.stderr!r}")
    _check("...and the run ends as upstream's: exit 0, and only zfs's own complaint on stderr",
           proc.returncode == 0 and proc.stderr == "cannot open 'gone/data': dataset does not exist\n",
           f"rc={proc.returncode} stderr={proc.stderr!r}")
    cache = (wd / "cache" / "snapshots.txt").read_text()
    _check("...and the cache it leaves holds whole lines only", cache.endswith("\t-\n") and "autosnap_d1_daily" not in cache,
           f"cache={cache!r}")


def test_a_count_too_long_for_a_number_does_not_stop_the_prune() -> None:
    # DEV-008. Upstream reads so long a count as a number no dataset reaches: none of that type is
    # pruned, and the run goes on.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _write(wd / "conf" / "sanoid.conf",
           f"[tank/keep]\n\thourly = 0\n\tdaily = 0\n\tmonthly = 0\n\tyearly = {'9' * 400}\n\tautosnap = no\n"
           "[tank/data]\n\thourly = 0\n\tdaily = 3\n\tmonthly = 0\n\tautosnap = no\n")
    now = int(time.time())
    snaps = [_snap_line("tank/keep", "autosnap_y_yearly", now - 400 * 86400),
             *(_snap_line("tank/data", f"autosnap_d{k}_daily", now - k * 86400 - 600) for k in (5, 4, 3, 2, 1))]
    proc = _run(wd, ["--prune-snapshots"], {"datasets": ["tank/keep", "tank/data"], "snapshots": snaps})
    destroyed = [c[-1] for c in _zfs_log(wd) if c[0] == "destroy"]
    _check("a 400-digit yearly count keeps every yearly, and the section after it is pruned as usual",
           proc.returncode == 0 and destroyed == [f"tank/data@autosnap_d{k}_daily" for k in (5, 4)] and not proc.stderr,
           f"rc={proc.returncode} destroyed={destroyed} stderr={proc.stderr[-300:]!r}")


def test_a_snapshot_dated_ahead_of_the_clock_is_set_aside() -> None:
    # DEV-051 (upstream #1119, #651). The clock ran a year ahead for one take and was set right: upstream takes no daily
    # until the clock passes that snapshot, and its prune counts it. pysanoid neither counts nor destroys it.
    now = int(time.time())
    ahead = _snap_line("tank/data", "autosnap_ahead_daily", now + 365 * 86400)

    def run(action: str, days: tuple[int, ...]) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
        wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
        _one_dataset_conf(wd, wanted=3)
        snaps = [ahead, *(_snap_line("tank/data", f"autosnap_d{k}_daily", now - k * 86400 - 600) for k in days)]
        proc = _run(wd, [action], {"datasets": ["tank/data"], "snapshots": snaps})
        log = _zfs_log(wd)
        return proc, [c[-1] for c in log if c[0] == "snapshot"], [c[-1] for c in log if c[0] == "destroy"]

    proc, taken, _ = run("--take-snapshots", (4, 3, 2))
    _check("a daily dated a year ahead doesn't hold the dailies back: one taken, exit 0, nothing said (DEV-051)",
           proc.returncode == 0 and len(taken) == 1 and taken[0].endswith("_daily") and proc.stderr == "",
           f"rc={proc.returncode} taken={taken} stderr={proc.stderr!r}")
    proc, _, destroyed = run("--prune-snapshots", (6, 5, 4, 2, 1))
    _check("a daily dated a year ahead takes none of daily = 3's places, and stays: d6 and d5 destroyed, d4 kept (DEV-051)",
           proc.returncode == 0 and destroyed == ["tank/data@autosnap_d6_daily", "tank/data@autosnap_d5_daily"],
           f"rc={proc.returncode} destroyed={destroyed} stderr={proc.stderr!r}")


def _wrapped(wd: Path, name: str, first: str) -> Path:
    """A `zfs` or `zpool` that runs `first` (sh, given the command's own arguments) and then, for zfs, the fake one.
    Returns its directory, for _run's bin_dir."""
    then = f'exec "{sys.executable}" "{DEV_DIR / "fake_zfs" / "zfs"}" "$@"\n' if name == "zfs" else ""
    _write(wd / "bin" / name, f"#!/bin/sh\n{first}\n{then}")
    (wd / "bin" / name).chmod(0o755)
    return wd / "bin"


_WARNS = 'printf "warning: caf\\351\\n" >&2'  # Latin-1's e acute: a byte no UTF-8 text has there


def test_what_zfs_prints_that_is_not_utf8_fails_no_command() -> None:
    # Parity: upstream never decodes what zfs prints. 0.1.41 read it as text, and a message it couldn't decode made the
    # command one that "could not run": the snapshot was taken and reported as failed, exit 1, and each destroy worked,
    # was reported as `could not remove`, and stayed in the cache.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd, wanted=2)
    bin_dir = _wrapped(wd, "zfs", f'case "$1" in snapshot|destroy) {_WARNS} ;; esac')
    now = int(time.time())
    snaps = [_snap_line("tank/data", f"autosnap_d{k}_daily", now - k * 86400 - 600) for k in (5, 4, 3, 2, 1)]
    proc = _run(wd, ["--cron"], {"datasets": ["tank/data"], "snapshots": snaps}, bin_dir=bin_dir)
    calls = _zfs_log(wd)
    taken = [c[-1] for c in calls if c[0] == "snapshot"]
    destroyed = [c[-1] for c in calls if c[0] == "destroy"]
    cached = [line.split("\t")[0] for line in (wd / "cache" / "snapshots.txt").read_text().splitlines()]
    _check("zfs warning in bytes that aren't UTF-8: the snapshot is taken, the old dailies destroyed, exit 0",
           proc.returncode == 0 and len(taken) == 1 and destroyed == [f"tank/data@autosnap_d{k}_daily" for k in (5, 4, 3)]
           and "could not" not in proc.stderr and "CRITICAL" not in proc.stderr,
           f"rc={proc.returncode} taken={taken} destroyed={destroyed} stderr={proc.stderr[:300]!r}")
    _check("...and each warning is written out as zfs wrote it, and nothing else is (0.1.42 showed none)",
           proc.stderr == "warning: caf\udce9\n" * 4, f"stderr={proc.stderr[:300]!r}")
    _check("...and the cache has the new snapshot and none of the destroyed ones",
           sorted(cached) == sorted([*taken, "tank/data@autosnap_d2_daily", "tank/data@autosnap_d1_daily"]), f"cache={cached}")


def test_a_pool_whose_zpool_prints_a_byte_that_is_not_utf8_is_still_pruned() -> None:
    # prune_defer asks zpool how full the pool is. 0.1.41 ended FATAL there on every run, nothing pruned, where upstream
    # prunes.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd, wanted=2)
    with (wd / "conf" / "sanoid.conf").open("a") as conf:
        conf.write("\tprune_defer = 50\n")
    bin_dir = _wrapped(wd, "zpool", f'{_WARNS}\necho "85%"')
    now = int(time.time())
    snaps = [_snap_line("tank/data", f"autosnap_d{k}_daily", now - k * 86400 - 600) for k in (5, 4, 3, 2, 1)]
    proc = _run(wd, ["--prune-snapshots"], {"datasets": ["tank/data"], "snapshots": snaps}, bin_dir=bin_dir)
    destroyed = [c[-1] for c in _zfs_log(wd) if c[0] == "destroy"]
    _check("zpool warning in bytes that aren't UTF-8, the pool at 85% past prune_defer = 50: pruned, exit 0",
           proc.returncode == 0 and destroyed == [f"tank/data@autosnap_d{k}_daily" for k in (5, 4, 3)],
           f"rc={proc.returncode} destroyed={destroyed} stderr={proc.stderr[:300]!r}")
    _check("...and zpool's warning is written out as zpool wrote it (0.1.42 showed none)",
           proc.stderr == "warning: caf\udce9\n", f"stderr={proc.stderr[:300]!r}")


def test_a_failed_snapshots_complaint_is_written_out_in_the_bytes_zfs_wrote() -> None:
    # As upstream's journal has it, where zfs writes to the terminal itself: the complaint as zfs wrote it, then
    # upstream's line with the wait status. 0.1.41 read the complaint in the locale's encoding: one that isn't UTF-8
    # became the decoder's complaint, and under a Latin-1 locale (where there is one; elsewhere the second check runs
    # as the first's does) UTF-8 came out encoded twice. 0.1.42 gave it inside a line of pysanoid's own wording.
    for what, octal, written, locale in (("that isn't UTF-8", "\\351", b"\xe9", {}),
                                         ("in UTF-8 under a Latin-1 locale", "\\303\\251", b"\xc3\xa9", {"LC_ALL": "en_US.ISO8859-1"})):
        wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
        _one_dataset_conf(wd)
        bin_dir = _wrapped(wd, "zfs", f'if [ "$1" = snapshot ]; then printf "cannot create snapshot: caf{octal} is busy\\n" >&2; exit 1; fi')
        env = _prepare(wd, {"datasets": ["tank/data"], "snapshots": []}, bin_dir, locale)
        proc = subprocess.run(_argv(wd, ["--take-snapshots"]), capture_output=True, timeout=30, env=env, cwd=wd, check=False)
        _check(f"a failed snapshot's complaint {what} is written out byte for byte, then upstream's line, and exits 1",
               proc.returncode == 1
               and proc.stderr.startswith(b"cannot create snapshot: caf" + written + b" is busy\nCRITICAL ERROR: zfs snapshot tank/data@")
               and b"_daily failed, 256\nERROR: 1 problem(s) this run" in proc.stderr,
               f"rc={proc.returncode} stderr={proc.stderr[:400]!r}")


def test_what_zfs_writes_is_shown_where_upstream_shows_it() -> None:
    # DEV-047 (confirmed against real Perl): upstream's zfs writes to the terminal itself, so what it writes on a
    # snapshot or a destroy, working or not, comes after the line announcing that command.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd, wanted=2)
    bin_dir = _wrapped(wd, "zfs", 'case "$1" in snapshot|destroy) echo "out: a note"; echo "err: a warning" >&2 ;; esac')
    now = int(time.time())
    snaps = [_snap_line("tank/data", f"autosnap_d{k}_daily", now - k * 86400 - 600) for k in (4, 3, 2, 1)]
    proc = _run(wd, ["--cron", "--verbose"], {"datasets": ["tank/data"], "snapshots": snaps}, bin_dir=bin_dir, merged=True)
    lines = proc.stdout.splitlines()
    announced = [i for i, line in enumerate(lines) if line.startswith(("taking snapshot tank/data@", "INFO: pruning tank/data@"))]
    _check("after each line announcing a snapshot or a destroy come zfs's own two, stdout's then stderr's",
           proc.returncode == 0 and len(announced) == 3
           and all(lines[i + 1:i + 3] == ["out: a note", "err: a warning"] for i in announced)
           and lines.count("out: a note") == 3, f"rc={proc.returncode} output={proc.stdout!r}")


def test_a_failed_destroy_is_given_as_upstream_gives_it() -> None:
    # zfs's complaint as zfs wrote it (no newline at its end here), then `could not remove NAME : $?`. 0.1.42 put the
    # complaint inside that line, in its own wording.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd, wanted=2)
    bin_dir = _wrapped(wd, "zfs", r'if [ "$1" = destroy ]; then printf "cannot destroy: caf\351 is busy" >&2; exit 1; fi')
    now = int(time.time())
    snaps = [_snap_line("tank/data", f"autosnap_d{k}_daily", now - k * 86400 - 600) for k in (3, 2, 1)]
    proc = _run(wd, ["--prune-snapshots"], {"datasets": ["tank/data"], "snapshots": snaps}, bin_dir=bin_dir)
    _check("a failed destroy: zfs's complaint byte for byte, then upstream's line with the wait status, exit 0",
           proc.returncode == 0
           and proc.stderr == "cannot destroy: caf\udce9 is busycould not remove tank/data@autosnap_d3_daily : 256\n",
           f"rc={proc.returncode} stderr={proc.stderr!r}")


def test_the_capacity_answer_is_read_as_upstream_reads_it() -> None:
    # prune_defer = 50, five dailies and daily = 2: three to destroy if the pool counts as over half full. The pool
    # away is the case of a pool named in sanoid.conf that isn't imported.
    def prune(answer: str) -> tuple[subprocess.CompletedProcess[str], int]:
        wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
        _one_dataset_conf(wd, wanted=2)
        with (wd / "conf" / "sanoid.conf").open("a") as conf:
            conf.write("\tprune_defer = 50\n")
        now = int(time.time())
        snaps = [_snap_line("tank/data", f"autosnap_d{k}_daily", now - k * 86400 - 600) for k in (5, 4, 3, 2, 1)]
        proc = _run(wd, ["--prune-snapshots"], {"datasets": ["tank/data"], "snapshots": snaps}, bin_dir=_wrapped(wd, "zpool", answer))
        return proc, sum(c[0] == "destroy" for c in _zfs_log(wd))

    invalid = "ERROR: 'zpool list -H -o cap tank' command returned invalid capacity value ({})!\n"
    proc, destroyed = prune('echo "cannot open \'tank\': no such pool" >&2; exit 1')
    _check("a pool that is away stops the prune in zpool's words and then upstream's, exit 1",
           proc.returncode == 1 and destroyed == 0 and proc.stderr == "cannot open 'tank': no such pool\n" + invalid.format(""),
           f"rc={proc.returncode} destroyed={destroyed} stderr={proc.stderr!r}")
    for what, answer, shown in (("a leading space", r'printf " 85%%\n"', ""), ("a carriage return", r'printf "85%%\r\n"', "85%\r"),
                                ("a trailing tab", r'printf "85%%\t\n"', "85%\t")):
        proc, destroyed = prune(answer)
        _check(f"an answer with {what} isn't a capacity to upstream: the prune stops, in upstream's words (0.1.42 pruned)",
               proc.returncode == 1 and destroyed == 0 and proc.stderr == invalid.format(shown),
               f"rc={proc.returncode} destroyed={destroyed} stderr={proc.stderr!r}")
    for what, answer in (("a second line", r'printf "85%%\n90%%\n"'), ("a second word", r'printf "85%% x\n"'),
                         ("a no-break space on a second line", r'printf "85%%\n\302\240\n"'),
                         ("U+001C on a second line", r'printf "85%%\n\034\n"')):
        proc, destroyed = prune(answer)
        _check(f"an answer with {what} holds more than a capacity: the prune stops, where upstream prunes (DEV-016)",
               proc.returncode == 1 and destroyed == 0
               and proc.stderr.startswith("FATAL: 'zpool list -H -o cap tank' returned more than a capacity value ("),
               f"rc={proc.returncode} destroyed={destroyed} stderr={proc.stderr!r}")
    proc, destroyed = prune(r'printf "85%%  \n"')
    _check("a capacity with spaces after it is one to both: pruned", proc.returncode == 0 and destroyed == 3 and proc.stderr == "",
           f"rc={proc.returncode} destroyed={destroyed} stderr={proc.stderr!r}")


def test_a_listings_complaint_is_written_out_as_zfs_wrote_it() -> None:
    # 0.1.42 trimmed it, ended it with a newline, and showed a byte that isn't UTF-8 as U+FFFD
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _one_dataset_conf(wd, wanted=2)
    bin_dir = _wrapped(wd, "zfs", r'[ "$1" = get ] && printf "\n warning: caf\351, no end" >&2')
    now = int(time.time())
    snaps = [_snap_line("tank/data", f"autosnap_d{k}_daily", now - k * 86400 - 600) for k in (3, 2, 1)]
    proc = _run(wd, ["--prune-snapshots"], {"datasets": ["tank/data"], "snapshots": snaps}, bin_dir=bin_dir)
    _check("a listing's complaint: every byte zfs wrote and no more, and the prune goes on",
           proc.returncode == 0 and proc.stderr == "\n warning: caf\udce9, no end"
           and sum(c[0] == "destroy" for c in _zfs_log(wd)) == 1, f"rc={proc.returncode} stderr={proc.stderr!r}")


def test_each_datasets_snapshot_names_carry_the_time_its_turn_came() -> None:
    # Parity: upstream reads the clock for the names as each dataset's turn comes, before its pre_snapshot_script.
    # 0.1.41 named every snapshot of a run by the time the run began.
    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    hook = wd / "bin" / "slow_hook"
    _write(hook, f'#!/bin/sh\necho "$SANOID_SNAPNAME" > "{wd}/hook_saw"\nsleep 1.1\n')
    hook.chmod(0o755)
    body = "\tdaily = 5\n\thourly = 0\n\tfrequently = 0\n\tweekly = 0\n\tmonthly = 0\n\tyearly = 0\n"
    _write(wd / "conf" / "sanoid.conf", f"[tank/a]\n{body}\tpre_snapshot_script = {hook}\n[tank/b]\n{body}")
    proc = _run(wd, ["--take-snapshots"], {"datasets": ["tank/a", "tank/b"], "snapshots": []}, env_extra={"TZ": "UTC"})
    names = dict(c[-1].split("@", 1) for c in _zfs_log(wd) if c[0] == "snapshot")
    saw = (wd / "hook_saw").read_text().strip() if (wd / "hook_saw").exists() else ""
    try:
        at = {ds: calendar.timegm(time.strptime(name, "autosnap_%Y-%m-%d_%H:%M:%S_daily")) for ds, name in names.items()}
    except ValueError:
        at = {}
    _check("a slow pre_snapshot_script is given the name its dataset's snapshot then takes",
           proc.returncode == 0 and sorted(names) == ["tank/a", "tank/b"] and saw == names.get("tank/a"),
           f"rc={proc.returncode} names={names} hook saw {saw!r} stderr={proc.stderr!r}")
    _check("...and the next dataset's name carries the later time its own turn came",
           len(at) == 2 and 1 <= at["tank/b"] - at["tank/a"] < 30, f"names={names}")


def test_a_hook_that_fails_is_warned_about_as_upstream_warns() -> None:
    # Parity (confirmed against real Perl): `WARN: <key> failed, $?`, the wait status, and -1 for words that can't be
    # exec'd, which upstream's system() execs itself. 0.1.44 gave the exit code, and for such words /bin/sh's complaint
    # and its 127. A failed hook is a warning: the snapshot is taken and the exit is 0, as upstream's.
    types = "\tdaily = 5\n\thourly = 0\n\tfrequently = 0\n\tweekly = 0\n\tmonthly = 0\n\tyearly = 0\n"

    def take(hook: str) -> tuple[subprocess.CompletedProcess[str], int]:
        wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
        _write(wd / "conf" / "sanoid.conf", f"[tank/data]\n{types}\tpre_snapshot_script = {hook}\n")
        proc = _run(wd, ["--take-snapshots"], {"datasets": ["tank/data"], "snapshots": []})
        return proc, sum(c[0] == "snapshot" for c in _zfs_log(wd))

    proc, taken = take("sh -c 'exit 3'")
    _check("a hook that exits 3: upstream's line with the wait status, the snapshot taken, exit 0",
           proc.returncode == 0 and taken == 1 and proc.stderr == "WARN: pre_snapshot_script failed, 768\n",
           f"rc={proc.returncode} taken={taken} stderr={proc.stderr!r}")
    proc, taken = take("kill -TERM $$")
    _check("a hook a signal killed: the signal's number",
           proc.returncode == 0 and taken == 1 and proc.stderr == "WARN: pre_snapshot_script failed, 15\n",
           f"rc={proc.returncode} taken={taken} stderr={proc.stderr!r}")
    proc, taken = take("/nonexistent/hook tank/data")
    lines = proc.stderr.splitlines()
    _check("a hook command that isn't there: a line saying why, then upstream's with -1, and no shell's complaint",
           proc.returncode == 0 and taken == 1 and len(lines) == 2
           and lines[0].startswith("could not run '/nonexistent/hook tank/data': ") and "No such file" in lines[0]
           and lines[1] == "WARN: pre_snapshot_script failed, -1", f"rc={proc.returncode} taken={taken} stderr={proc.stderr!r}")

    wd = Path(tempfile.mkdtemp(prefix="pysanoid_e2e_"))
    _write(wd / "conf" / "sanoid.conf", f"[tank/data]\n{types.replace('daily = 5', 'daily = 1')}\tpre_pruning_script = exit 0\n")
    now = int(time.time())
    snaps = [_snap_line("tank/data", f"autosnap_d{k}_daily", now - k * 86400 - 600) for k in (3, 2, 1)]
    proc = _run(wd, ["--prune-snapshots"], {"datasets": ["tank/data"], "snapshots": snaps})
    destroyed = sum(c[0] == "destroy" for c in _zfs_log(wd))
    _check("`exit 0` as a veto hook can't be exec'd, as upstream can't: -1 each time, and nothing destroyed",
           proc.returncode == 0 and destroyed == 0 and proc.stderr.count("WARN: pre_pruning_script failed, -1\n") == 2
           and proc.stderr.count("could not run 'exit 0': ") == 2 and len(proc.stderr.splitlines()) == 4,
           f"rc={proc.returncode} destroyed={destroyed} stderr={proc.stderr!r}")


def main() -> None:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"{name}:")
            fn()
    print(f"\n{_pass} ok, {_fail} failed")
    sys.exit(1 if _fail else 0)


if __name__ == "__main__":
    main()
