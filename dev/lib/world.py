"""pysanoid as a whole program, run after run, against a pool held in memory.

run_main forks, and in the child pysanoid.main runs against a World: zfs and zpool are answered by a Pool, the clock
is the one the caller sets (pysanoid's `time` and `datetime.now`, and the date of every file it writes), and its
config, caches and locks are real files under the world's directory. A forked child, so that a run can end as a
process ends: a Fault makes one event of the run fail, or ends the process there with nothing unwound, as SIGKILL
does, and as systemd's SIGTERM does when a unit's timeout stops a run (pysanoid installs no handler).

An event is one file operation on pysanoid's own files (Path's read_bytes, write_bytes, rename, unlink, stat and
mkdir, os.utime, open, os.path.isfile and exists), one flock, or one zfs or zpool command. kinds_for says how each can
fail. The clock stands still while a run lasts.
"""
from __future__ import annotations

import errno
import fcntl
import io
import json
import os
import pathlib
import random
import shutil
import subprocess
import sys
import time as time_module
import traceback
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import pysanoid as ps
from lib.model import Section

DEFAULTS = Path(ps.__file__).resolve().parent / "sanoid.defaults.conf"
TTL = 1200  # main's --cache-ttl when none is given


class Clock:
    """Stands in for pysanoid's `time` module: time() is the simulated clock, the rest the real module."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(time_module, name)


def clock_datetime(clock: Clock) -> type[datetime]:
    """pysanoid's `datetime`, with now() read from the clock."""
    class ClockDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> ClockDatetime:
            return cls.fromtimestamp(clock.now, tz)

    return ClockDatetime


def set_clock(clock: Clock) -> None:
    setattr(ps, "time", clock)  # noqa: B010 - a stand-in of another type
    setattr(ps, "datetime", clock_datetime(clock))  # noqa: B010


class Pool:
    """The zfs and zpool a run talks to. `snaps` is {dataset@name: creation} in the order the snapshots were made,
    which is the order zfs lists them in; `caps` is each pool's capacity in percent."""

    def __init__(self, tree: list[str], snaps: dict[str, int], caps: dict[str, int], clock: Clock) -> None:
        self.tree, self.snaps, self.caps, self.clock = list(tree), dict(snaps), dict(caps), clock
        self.created: list[tuple[str, bool]] = []  # (full name, made by an ancestor's -r)
        self.destroyed: list[str] = []
        self.said: list[str] = []  # what zfs and zpool wrote to stderr

    def below(self, fs: str) -> list[str]:
        return [d for d in self.tree if d == fs or d.startswith(f"{fs}/")]

    def command(self, cmd: list[str], perform: bool = True) -> tuple[int, str, str]:
        """(exit status, stdout, stderr) of one zfs or zpool command; perform=False answers without acting."""
        rc, out, err = self._command(cmd, perform)
        if err:
            self.said.append(err)
        return rc, out, err

    def _command(self, cmd: list[str], perform: bool) -> tuple[int, str, str]:
        if cmd[0] == ps.ZPOOL and cmd[1:5] == ["list", "-H", "-o", "cap"] and len(cmd) == 6:
            if cmd[5] not in self.caps:
                return 1, "", f"cannot open '{cmd[5]}': no such pool\n"
            return 0, f"{self.caps[cmd[5]]}%\n", ""
        if cmd[0] != ps.ZFS:
            raise AssertionError(f"the pool got a command that isn't zfs or zpool: {cmd}")
        if cmd[1:5] == ["get", "-Hrpt", "snapshot", "creation"]:
            names, rc, err, listed = cmd[5:], 0, "", []
            for name in names or sorted({d.split("/", 1)[0] for d in self.tree}):
                if name not in self.tree:
                    rc, err = 1, err + f"cannot open '{name}': dataset does not exist\n"
                    continue
                listed += [d for d in self.below(name) if d not in listed]
            by_dataset: dict[str, list[str]] = {}
            for full, made in self.snaps.items():
                by_dataset.setdefault(full.split("@", 1)[0], []).append(f"{full}\tcreation\t{made}\t-\n")
            return rc, "".join(line for d in listed for line in by_dataset.get(d, [])), err
        if cmd[1:7] == ["list", "-o", "name", "-t", "filesystem,volume", "-Hr"] and len(cmd) == 8:
            if cmd[7] not in self.tree:
                return 1, "", f"cannot open '{cmd[7]}': dataset does not exist\n"
            return 0, "".join(f"{d}\n" for d in self.below(cmd[7])), ""
        if cmd[1] == "snapshot" and cmd[2:-1] in ([], ["-r"]):
            full = cmd[-1]
            ds, _, name = full.partition("@")
            if ds not in self.tree or not name:
                return 1, "", f"cannot open '{ds}': dataset does not exist\n"
            targets = self.below(ds) if cmd[2:-1] else [ds]
            taken = next((d for d in targets if f"{d}@{name}" in self.snaps), None)
            if taken is not None:
                return 1, "", f"cannot create snapshot '{taken}@{name}': dataset already exists\n"
            if perform:
                for d in targets:
                    self.snaps[f"{d}@{name}"] = int(self.clock.now)
                    self.created.append((f"{d}@{name}", d != ds))
            return 0, "", ""
        if cmd[1] == "destroy" and len(cmd) == 3:
            if cmd[2] not in self.snaps:
                return 1, "", "could not find any snapshots to destroy; check snapshot names.\n"
            if perform:
                del self.snaps[cmd[2]]
                self.destroyed.append(cmd[2])
            return 0, "", ""
        raise AssertionError(f"the pool got a zfs command pysanoid isn't known to run: {cmd}")


# ============================================================================
# Faults
# ============================================================================

@dataclass
class Fault:
    """Which event of a run fails, and how: `target` is the event's index in the run, or (label prefix, n) for the
    n-th event so labelled, counting from 0; `kind` is one of kinds_for(label), or "any" for one drawn by `seed`."""
    target: int | tuple[str, int]
    kind: str
    seed: int = 0


FILE_KINDS = ("enospc", "eacces", "partial", "kill", "kill_mid")
ZFS_KINDS = ("eagain", "rc1", "killed", "half_rc1", "done_killed", "empty_ok", "kill", "kill_mid")
ERRNO = {"enospc": errno.ENOSPC, "eacces": errno.EACCES, "eio": errno.EIO, "eagain": errno.EAGAIN}


def kinds_for(label: str) -> tuple[str, ...]:
    """How an event can fail.

    A file operation: the error (enospc, eacces), the process ending before it (kill), and for a write, half the
    bytes written and then the error (partial) or the end of the process (kill_mid). A flock or a file test: the
    error, or for the test, the answer no.

    A zfs or zpool command: it can't start (eagain); it refuses, having done and printed nothing (rc1); it is killed
    before acting, a listing with half its lines out (killed); a listing fails partway (half_rc1), or answers nothing
    and exits 0 (empty_ok), or answers junk (junk*); the snapshot listing is killed, or fails, inside the line of some
    dataset's newest snapshot of a type (cut*); a snapshot or a destroy happens and the command dies before saying so
    (done_killed); the process ends before the command (kill) or after it, the answer unread (kill_mid)."""
    if label.startswith(("zfs:snapshot", "zfs:destroy")):
        # zfs never reports a snapshot or a destroy it didn't make
        return tuple(k for k in ZFS_KINDS if k not in ("empty_ok", "half_rc1"))
    if label.startswith(("zfs:", "zpool:")):
        listing = (*(k for k in ZFS_KINDS if k != "done_killed"), "junk1", "junk22", "junk333")
        return (*listing, *(f"cut{i}" for i in range(21))) if label == "zfs:get" else listing
    if label.startswith("os.path."):
        return ("eacces",)
    if label.startswith("Path.write_bytes"):
        return FILE_KINDS
    return ("enospc", "eacces", "kill")


class Injector:
    """Counts a run's events and turns one into the fault."""

    def __init__(self, pool: Pool, fault: Fault | None, state_file: Path) -> None:
        self.pool, self.fault, self.state_file = pool, fault, state_file
        self.labels: list[str] = []
        self.matched: dict[str, int] = {}
        self.armed = True
        self.fired: str | None = None
        self.kind = ""

    def hit(self, label: str) -> bool:
        if not self.armed:
            return False
        index = len(self.labels)
        self.labels.append(label)
        fault = self.fault
        if fault is None or self.fired is not None:
            return False
        if isinstance(fault.target, int):
            if index != fault.target:
                return False
        else:
            prefix, nth = fault.target
            if not label.startswith(prefix):
                return False
            self.matched[prefix] = self.matched.get(prefix, 0) + 1
            if self.matched[prefix] != nth + 1:
                return False
        self.fired = label
        self.kind = random.Random(fault.seed).choice(kinds_for(label)) if fault.kind == "any" else fault.kind
        if self.kind == "kill":
            self.die()
        return True

    def die(self) -> None:
        """The process ends here: no handler, no finally, no flush."""
        self.armed = False
        self.state_file.write_text(json.dumps(self.state(killed=True)))
        os._exit(137)

    def state(self, **more: Any) -> dict[str, Any]:
        pool = self.pool
        return {"snaps": pool.snaps, "created": pool.created, "destroyed": pool.destroyed, "said": pool.said,
                "events": self.labels, "fired": self.fired, "kind": self.kind, **more}

    def oserror(self) -> OSError:
        code = ERRNO.get(self.kind, errno.EIO)
        return OSError(code, os.strerror(code))


def install(inj: Injector, clock: Clock, roots: tuple[str, ...]) -> None:
    """Puts the pool behind subprocess.run, dates what pysanoid writes under `roots` by the clock, and makes each
    event there, each flock and each command one the injector can fail."""
    def mine(path: object) -> bool:
        return str(path).startswith(roots)

    real_utime = os.utime

    def half(path: pathlib.Path, data: bytes) -> None:
        with open(path, "wb") as fh:
            _ = fh.write(data[:len(data) // 2])
        real_utime(path, (clock.now, clock.now))

    def wrap_path(name: str) -> None:
        real = getattr(pathlib.Path, name)

        def wrapper(self: pathlib.Path, *args: Any, **kwargs: Any) -> Any:
            if not mine(self):
                return real(self, *args, **kwargs)
            if inj.hit(f"Path.{name}:{self.name}"):
                if name == "write_bytes" and inj.kind in ("partial", "kill_mid"):
                    half(self, args[0])
                    if inj.kind == "kill_mid":
                        inj.die()
                    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
                raise inj.oserror()
            result = real(self, *args, **kwargs)
            if name == "write_bytes":
                real_utime(self, (clock.now, clock.now))
            return result

        setattr(pathlib.Path, name, wrapper)

    for name in ("read_bytes", "write_bytes", "rename", "unlink", "stat", "mkdir"):
        wrap_path(name)

    def utime(path: Any, *args: Any, **kwargs: Any) -> None:
        if mine(path) and inj.hit(f"os.utime:{Path(path).name}"):
            raise inj.oserror()
        real_utime(path, *args, **kwargs)

    os.utime = utime

    real_open = open

    def opener(path: Any, *args: Any, **kwargs: Any) -> Any:
        if mine(path) and inj.hit(f"open:{Path(path).name}"):
            raise inj.oserror()
        return real_open(path, *args, **kwargs)

    setattr(ps, "open", opener)  # noqa: B010 - pysanoid has no `open` of its own to assign to

    real_flock = fcntl.flock

    def flock(fd: Any, op: int) -> None:
        if op != fcntl.LOCK_UN and inj.hit("flock"):
            raise inj.oserror()
        real_flock(fd, op)

    fcntl.flock = flock

    for name in ("isfile", "exists"):
        real_test = getattr(os.path, name)

        def tester(path: Any, _real: Any = real_test, _name: str = name) -> bool:
            if mine(path) and inj.hit(f"os.path.{_name}:{Path(path).name}"):
                return False  # what the real test answers for a path it can't reach
            return bool(_real(path))

        setattr(os.path, name, tester)

    def run(cmd: list[str], text: bool = False, **_kwargs: Any) -> subprocess.CompletedProcess[Any]:
        rc, out, err = _answer(inj, cmd)
        if text:  # how pysanoid read zfs before 0.1.42
            return subprocess.CompletedProcess(cmd, rc, stdout=out.replace("\udcff", "?"), stderr=err)
        return subprocess.CompletedProcess(cmd, rc, stdout=out.encode("utf-8", "surrogateescape"),
                                           stderr=err.encode("utf-8", "surrogateescape"))

    setattr(subprocess, "run", run)  # noqa: B010


JUNK = ["\t", "\n", "@", "autosnap", "ly", "creation", "_daily", "tank/a", "\x00", "\r", "%", ",", " ", "9" * 30, "\udcff",
        "é", "0", "17", "-", "/", "hourly", "\x0b", " ", "tank/b/c"]


def _answer(inj: Injector, cmd: list[str]) -> tuple[int, str, str]:
    pool = inj.pool
    if cmd[0] not in (ps.ZFS, ps.ZPOOL):  # a hook: `false` fails, anything else works
        return int(cmd[-1].endswith("false")), "", ""
    label = f"zpool:{cmd[1]}" if cmd[0] == ps.ZPOOL else f"zfs:list {cmd[-1]}" if cmd[1] == "list" else f"zfs:{cmd[1]}"
    if not inj.hit(label):
        return pool.command(cmd)
    kind = inj.kind
    if kind in ERRNO or kind == "partial":
        raise inj.oserror()
    if kind == "rc1":
        err = "internal error: failed to initialize ZFS library\n"
        pool.said.append(err)
        return 1, "", err
    if kind == "done_killed":
        return -9, *pool.command(cmd)[1:]
    if kind == "kill_mid":
        _ = pool.command(cmd)
        inj.die()
    _, out, _ = pool._command(cmd, perform=False)
    lines = out.splitlines(keepends=True)
    if kind == "killed":
        return -9, "".join(lines[:len(lines) // 2]), ""
    if kind == "half_rc1":
        err = "cannot iterate: I/O error\n"
        pool.said.append(err)
        return 1, "".join(lines[:len(lines) // 2]), err
    if kind == "empty_ok":
        return 0, "", ""
    if kind.startswith("cut"):
        def group(line: str) -> tuple[str, str]:
            name = line.split("\t", 1)[0]
            return name.split("@", 1)[0], name.rsplit("_", 1)[-1]

        n = int(kind[3:])
        ends = [i for i, line in enumerate(lines) if "@autosnap" in line
                and (i + 1 == len(lines) or group(lines[i + 1]) != group(line))]
        if ends:
            at = ends[n % len(ends)]
            out = "".join(lines[:at]) + lines[at][:-8]
        if n % 2 == 0:
            return -9, out, ""
        err = "cannot iterate: I/O error\n"
        pool.said.append(err)
        return 1, out, err
    if kind.startswith("junk"):
        rng = random.Random(len(inj.labels) * 7919 + len(kind))
        return 0, "".join(rng.choice(JUNK) for _ in range(rng.randrange(1, 400))), ""
    raise AssertionError(f"no such kind of fault for {label}: {kind}")


# ============================================================================
# A world, and a run in it
# ============================================================================

@dataclass
class World:
    wd: Path
    tree: list[str]
    caps: dict[str, int]
    tz: str
    snaps: dict[str, int] = field(default_factory=dict)
    datasets_txt: str = ""  # what a clean run writes to the dataset cache


@dataclass
class Result:
    """One run: its exit status (or what left main instead), what it printed, its events, and the pool after it."""
    rc: int | str
    escaped: str
    stdout: str
    stderr: str
    events: list[str]
    fired: str | None
    kind: str
    killed: bool
    snaps: dict[str, int]
    created: list[tuple[str, bool]]
    destroyed: list[str]
    said: list[str]
    exact: bool = False  # set by the auditor: nothing was done to the run, and it read the pool as it is


def fresh_world(wd: Path, conf: str) -> None:
    """An empty directory for a world, with its config."""
    shutil.rmtree(wd, ignore_errors=True)
    (wd / "conf").mkdir(parents=True)
    _ = shutil.copy(DEFAULTS, wd / "conf" / "sanoid.defaults.conf")
    _ = (wd / "conf" / "sanoid.conf").write_text(conf)


def _in_child(world: World, now: float) -> tuple[Clock, Pool]:
    clock = Clock(now)
    os.environ["PATH"], os.environ["TZ"] = "/usr/bin:/bin", world.tz
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    return clock, Pool(world.tree, world.snaps, world.caps, clock)


def run_main(world: World, argv: list[str], now: float, fault: Fault | None = None) -> Result:
    """pysanoid.main(argv) in a forked child at `now`, against the world as it stands. The world's files change; its
    `snaps` don't: the pool after the run is in the result."""
    wd = world.wd
    state_file = wd / "result.json"
    state_file.unlink(missing_ok=True)
    sys.stdout.flush()
    pid = os.fork()
    if pid == 0:
        try:
            clock, pool = _in_child(world, now)
            inj = Injector(pool, fault, state_file)
            install(inj, clock, tuple(str(wd / d) for d in ("conf", "cache", "run")))
            set_clock(clock)
            rc: int | str
            escaped = ""
            try:
                rc = ps.main(["--configdir", str(wd / "conf"), "--cache-dir", str(wd / "cache"), "--run-dir", str(wd / "run"), *argv])
            except SystemExit as e:
                rc = f"SystemExit({e.code})"
            except BaseException as e:  # noqa: BLE001 - anything that leaves main is the finding
                rc, escaped = "EXC", "".join(traceback.format_exception(type(e), e, e.__traceback__))
            inj.armed = False
            out, err = sys.stdout, sys.stderr
            assert isinstance(out, io.StringIO) and isinstance(err, io.StringIO)
            _ = state_file.write_text(json.dumps(inj.state(killed=False, rc=rc, escaped=escaped, stdout=out.getvalue(),
                                                             stderr=err.getvalue())))
        finally:
            os._exit(0)
    _, status = os.waitpid(pid, 0)
    if not state_file.exists():
        raise RuntimeError(f"the child running pysanoid left no result (wait status {status})")
    state: dict[str, Any] = json.loads(state_file.read_text())
    return Result(rc=state.get("rc", 137), escaped=state.get("escaped", ""), stdout=state.get("stdout", ""),
                  stderr=state.get("stderr", ""), events=state["events"], fired=state["fired"], kind=state["kind"],
                  killed=state["killed"], snaps=state["snaps"], created=[(full, bool(by)) for full, by in state["created"]],
                  destroyed=state["destroyed"], said=state["said"])


class ConfigRefused(Exception):
    """pysanoid refuses the world's config, in these words."""


def clean_config(world: World, now: float) -> dict[str, dict[str, Any]]:
    """The merged config as pysanoid loads it at `now` with no cache and nothing failing, from a child of its own.
    ConfigRefused when pysanoid refuses the config; RuntimeError, with the traceback, when loading it crashes."""
    r, w = os.pipe()
    sys.stdout.flush()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            clock, pool = _in_child(world, now)
            install(Injector(pool, None, world.wd / "unused.json"), clock, ())
            set_clock(clock)
            try:
                answer: dict[str, Any] = {"config": ps.load_config(str(world.wd / "conf"), ps.ZfsClient(world.wd / "no-cache", 0, True))}
            except ps.SanoidError as e:
                answer = {"refused": str(e)}
            except BaseException as e:  # noqa: BLE001 - reported to the parent
                answer = {"crashed": "".join(traceback.format_exception(type(e), e, e.__traceback__))}
            _ = os.write(w, json.dumps(answer).encode())
        finally:
            os._exit(0)
    os.close(w)
    data = b""
    while chunk := os.read(r, 65536):
        data += chunk
    _ = os.waitpid(pid, 0)
    answer: dict[str, Any] = json.loads(data) if data else {"crashed": "the child loading the config left no answer"}
    if "refused" in answer:
        raise ConfigRefused(answer["refused"])
    if "crashed" in answer:
        raise RuntimeError(answer["crashed"])
    config: dict[str, dict[str, Any]] = answer["config"]
    return config


def _cache_text(path: Path, now: float) -> str | None:
    """A cache file's text if a run at `now` uses it, by the rules pysanoid states: a regular file no older than the
    TTL and not dated ahead, with no NUL in it and its last line ended."""
    if not path.is_file():
        return None
    age = int(now) - int(path.stat().st_mtime)
    text = path.read_bytes().decode("utf-8", "surrogateescape")
    return text if 0 <= age <= TTL and "\0" not in text and (not text or text.endswith("\n")) else None


def dataset_cache(world: World, now: float) -> bool | None:
    """Whether the dataset cache, as a run at `now` finds it, says what zfs would: None when the run won't use it.
    An empty one is no cache to pysanoid, which asks zfs and writes it again."""
    text = _cache_text(world.wd / "cache" / "datasets.txt", now)
    return None if text is None or (not text and world.datasets_txt) else text == world.datasets_txt


def snapshot_cache(world: World, sections: list[Section], now: float) -> bool | None:
    """Whether the snapshot cache, as a run at `now` finds it, says what the pool holds: None when the run won't use it."""
    text = _cache_text(world.wd / "cache" / "snapshots.txt", now)
    if text is None:
        return None
    active = [s.path for s in sections if (s.cfg.get("autoprune") or s.cfg.get("autosnap")) and not s.cfg.get("process_children_only")]
    listed = {full: made for full, made in world.snaps.items()
              if any(full.startswith((f"{a}@", f"{a}/")) for a in active)}
    cached: dict[str, int] = {}
    for line in text.split("\n")[:-1]:
        full, _, rest = line.partition("\tcreation\t")
        made = rest.split("\t", 1)[0]
        if not made.isdigit():
            return False
        if any(full.startswith((f"{a}@", f"{a}/")) for a in active):
            cached[full] = int(made)
    return cached == listed


def sees_the_pool(world: World, sections: list[Section], now: float) -> bool:
    """Whether a run starting at `now` reads the pool and its datasets as they are: each cache is one the run won't
    use, or says what zfs would. A run that doesn't is one whose cache an earlier, failed run left behind."""
    return dataset_cache(world, now) is not False and snapshot_cache(world, sections, now) is not False
