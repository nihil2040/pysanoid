#!/usr/bin/env python3
"""Differential test: upstream's runscript, extracted verbatim, vs pysanoid's run_hook_script, in isolation, the way
both are invoked: a hook command line plus the SANOID_* vars the caller sets (diff_take_snapshots.py and
diff_prune.py cover the call sites).

Compared per scenario:
  - the timeout each side set: upstream's through a CORE::GLOBAL::alarm override that reads back what
    `alarm $timeout` armed (alarm 0 returns the seconds left), pysanoid's from the timeout it passed to subprocess.run;
  - the hook's exit status as the caller sees it: its exit code, the signal that killed it, or -1 for a command
    that couldn't be exec'd;
  - the arguments and SANOID_* environment the hook got (dev/fake_hook/hook_probe logs both);
  - the warning for a hook that failed, `WARN: <key> failed, <$?>`, word for word, less the ` at <script> line N.`
    Perl adds; and whether a timeout was reported.
Scenarios vary script_timeout across upstream's string gate (`$timeout gt 0`) and Perl's numification (`5s`, `1_0`,
`1.5`, `0.5`, `inf`, 32-bit wrap), and the command line: plain words, a trailing `# comment`, a final ` 2>&1`, quotes,
a leading `exec ` or `NAME=`, whitespace Perl splits on and the shell doesn't, a shell that kills itself, and words
Perl can't exec (`exit 0`, `:`, `!`, a first word starting with `-`, a file that isn't there, a directory). Each side
runs in an empty directory of its own, so no shell glob finds anything.

No scenario lets a timeout fire: upstream's alarm kills the whole Perl process when it does, so every hook sleeps well
under a second and no armed timeout is shorter than one. verify_upstream_alarm_crashes_process keeps confirming that
fact, in hand mode, outside the tally (DEV-042). Three more standing checks run there, each running upstream's runscript
and pysanoid's run_hook_script in child processes of their own, since what they check is how a process meets a signal, a
fork that fails, or what it was started with: verify_fork_failure_waits_upstream_only (DEV-043: with no process to
spare, under RLIMIT_NPROC = 1, upstream's system() and a piped open still wait after FORK_WAIT seconds, and pysanoid's
hook fails at once), verify_ctrl_c_stops_pysanoid_only (DEV-044: a SIGINT to the whole process group, as Ctrl-C sends
it, while a hook runs: upstream carries on past the hook it killed, and pysanoid stops) and
verify_hooks_inherit_what_python_sets_up (DEV-046: started as systemd starts a service, upstream's hook inherits SIGPIPE
ignored and no locale, pysanoid's SIGPIPE at its default and LC_CTYPE=C.UTF-8).

Left out of the comparison: what says why a command couldn't be exec'd, Perl's own `Can't exec` warning there and
pysanoid's `could not run` line here.

Known class (counted, not a difference):
  - over macOS's limit: macOS's alarm() refuses more than 100,000,000 s, so upstream sets nothing there; Linux sets it,
    and so does pysanoid.
Self-checks on the pysanoid side, each a DIFF when broken: a script_timeout that isn't a plain whole number (digits, a
trailing comment allowed) is reported once as a problem, a plain one never, and a missing one only warns.

Run: python3 dev/diff_hooks.py hand
     python3 dev/diff_hooks.py fuzz <n> <seed>
"""
from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import json
import os
import random
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.extract import extract_sub
from lib.perlrun import (
    PERL,
    PERL_JSON_HEAD,
    READ_SCENARIO_PERL,
    WRITE_RESULT_PERL,
    build_harness,
    run_harness,
)

import pysanoid as p

WORK = DEV_DIR / "work" / "hooks"
HOOK_PROBE = DEV_DIR / "fake_hook" / "hook_probe"
MACOS_ALARM_MAX = 100_000_000

# The override keeps alarm()'s own prototype, so `alarm $timeout` and `alarm 0` compile as they do upstream, and it
# hands alarm() the caller's very scalar, so Perl's numification of it is untouched.
HEAD = PERL_JSON_HEAD + r'''my %config;
our @__ARMED;
BEGIN {
    *CORE::GLOBAL::alarm = sub (_) {
        my $previous = CORE::alarm($_[0]);
        my $armed = CORE::alarm(0);
        CORE::alarm($armed) if $armed;
        push @__ARMED, $armed;
        return $previous;
    };
}
'''

TAIL = f'''
{READ_SCENARIO_PERL}
my $dataset = $__scn->{{dataset}};
my $key = $__scn->{{key}};
$config{{$dataset}} = {{}};
if (defined $__scn->{{timeout_raw}}) {{ $config{{$dataset}}{{'script_timeout'}} = $__scn->{{timeout_raw}}; }}
$config{{$dataset}}{{$key}} = $__scn->{{script}};
foreach my $k (keys %{{ $__scn->{{env_extra}} }}) {{ $ENV{{$k}} = $__scn->{{env_extra}}{{$k}}; }}
my $ret = runscript($key, $dataset);
my ($armed) = sort {{ $b <=> $a }} (@__ARMED, 0);
my %__result = ( ret => $ret, armed => $armed );
{WRITE_RESULT_PERL}
'''

SCRIPT = build_harness(HEAD, extract_sub("runscript"), TAIL)

# How a scenario's hook command line is spelled: {hook} is hook_probe, {args} its tag, exit code and sleep.
COMMANDS = {
    "plain": "{hook} {args}",
    "comment": "{hook} {args} # quiesce the db",
    "semicolon": "{hook} {args} ; note",
    "final 2>&1": "{hook} {args} 2>&1",
    "quoted word": "{hook} {args} 'two words'",
    "exec": "exec {hook} {args}",
    "assignment": "A=1 {hook} {args}",
    "tabs": "{hook}\t{args}\textra",
    "vertical tab": "{hook} {args}\x0bextra",
    "carriage return": "{hook} {args}\rextra",
    "leading spaces": "  {hook} {args}",
    "exit 0": "exit 0",
    "colon": ":",
    "bang": "! {hook} {args}",
}
# More ways to fail. The generator draws these from a stream of its own, so a seed's other scenarios stay as they were.
FAILING = {
    "dash first": "-c {hook} {args}",
    "no such file": "{hook}_is_not_there {args}",
    "a directory": "/ {args}",
    "killed": "kill -9 $$",
    "killed after the hook": "{hook} {args} ; kill -TERM $$",
    "blanks alone": " \t ",
}
COMMANDS_ALL = {**COMMANDS, **FAILING}


def _script_for(scn: dict[str, Any]) -> str:
    args = f"{scn['tag']} {scn['exit_code']}" + (f" {scn['sleep']}" if scn["sleep"] else "")
    return COMMANDS_ALL[scn["command"]].replace("{hook}", str(HOOK_PROBE)).replace("{args}", args)


@contextlib.contextmanager
def _fd2_to(path: Path) -> Iterator[None]:
    """File descriptor 2 into `path`, so what a hook and /bin/sh print is captured, as run_harness captures Perl's."""
    sys.stderr.flush()
    saved = os.dup(2)
    with open(path, "wb") as f:
        os.dup2(f.fileno(), 2)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


def _setup(wd: Path) -> None:
    shutil.rmtree(wd, ignore_errors=True)
    wd.mkdir(parents=True)
    (wd / "hook.log").write_text("")


def _log(wd: Path) -> list[Any]:
    return [json.loads(x) for x in (wd / "hook.log").read_text().split("\n") if x]


def run_perl(wd: Path, scn: dict[str, Any]) -> dict[str, Any]:
    wd = wd.resolve()  # see diff_prune.py's run_perl for why: a relative wd double-resolves inside the Perl child
    _setup(wd)
    stdin_obj = {"dataset": scn["dataset"], "key": scn["key"], "timeout_raw": scn["timeout_raw"],
                 "script": _script_for(scn), "env_extra": scn["env_extra"]}
    result = run_harness(SCRIPT, stdin_obj=stdin_obj, env={"HOOK_LOG": str(wd / "hook.log")}, cwd=wd, timeout=15)
    if result.data is None:
        raise RuntimeError(f"perl harness produced no result:\n{result.stderr}")
    raw = result.data["ret"]
    ret = -1 if raw == -1 else raw >> 8 if raw & 0x7F == 0 else -(raw & 0x7F)  # system()'s wait status
    return {"ret": ret, "timeout": result.data["armed"], "stderr": result.stderr, "log": _log(wd), "problems": []}


def run_python(wd: Path, scn: dict[str, Any]) -> dict[str, Any]:
    wd = wd.resolve()
    _setup(wd)
    cfg: dict[str, Any] = {scn["key"]: _script_for(scn)}
    if scn["timeout_raw"] is not None:
        cfg["script_timeout"] = scn["timeout_raw"]
    timeouts: list[object] = []
    real_run = subprocess.run

    def recording_run(*args: Any, **kwargs: Any) -> Any:
        timeouts.append(kwargs.get("timeout"))
        return real_run(*args, **kwargs)

    problems: list[str] = []
    # a version from before the problems parameter still runs, so this harness can be pointed at one
    extra = {"problems": problems} if "problems" in inspect.signature(p.run_hook_script).parameters else {}
    out, err = io.StringIO(), io.StringIO()
    cwd = os.getcwd()
    os.environ["HOOK_LOG"] = str(wd / "hook.log")
    subprocess.run = recording_run
    try:
        os.chdir(wd)
        with _fd2_to(wd / "fd2.txt"), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ret = p.run_hook_script(cfg, scn["key"], scn["dataset"], dict(scn["env_extra"]), **extra)
    finally:
        subprocess.run = real_run
        os.chdir(cwd)
    stderr = (wd / "fd2.txt").read_text(errors="replace") + err.getvalue()
    return {"ret": ret, "timeout": (timeouts[0] if timeouts else None) or 0, "stderr": stderr,
            "log": _log(wd), "problems": problems}


KNOWN: Counter[str] = Counter()
_PLAIN_TIMEOUT_RE = re.compile(r"[0-9]+(?:[ \t\r\n\f\v]*[#;].*)?")  # digits, then maybe a comment


_PERL_AT = re.compile(r" at \S+ line \d+(?:, <\S+> (?:line|chunk) \d+)?\.$")  # what Perl's warn adds


def warnings(stderr: str) -> list[str]:
    """The lines both sides write for a hook that failed or ran out of time, as written."""
    return [_PERL_AT.sub("", line) for line in stderr.split("\n")
            if re.match(r"WARN: \w+ (?:failed, |didn't finish in the allowed time)", line)]


def self_check(scn: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """pysanoid's own reporting of script_timeout, which upstream doesn't do (see the module docstring)."""
    raw = scn["timeout_raw"]
    if raw is None:
        ok = b["problems"] == [] and "WARN: 'script_timeout' is not set" in b["stderr"]
    elif _PLAIN_TIMEOUT_RE.fullmatch(raw):
        ok = b["problems"] == []
    else:
        ok = len(b["problems"]) == 1 and b["problems"][0].startswith("'script_timeout' for ")
    return [] if ok else ["reporting"]


def run_both(scn: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    a, b = run_perl(WORK / "perl", scn), run_python(WORK / "python", scn)
    diffs = self_check(scn, b)
    if a["timeout"] != b["timeout"]:
        if a["timeout"] == 0 and type(b["timeout"]) is int and b["timeout"] > MACOS_ALARM_MAX and sys.platform == "darwin":
            KNOWN["over macOS's limit: macOS's alarm() refuses it, Linux and pysanoid set it"] += 1
        else:
            diffs.append("timeout")
    diffs += [k for k in ("ret", "log") if a[k] != b[k]]
    if warnings(a["stderr"]) != warnings(b["stderr"]):
        diffs.append("warning")
    return a, b, diffs


def S(*, key: str = "pre_snapshot_script", dataset: str = "tank/a", timeout_raw: str | None, exit_code: int = 0,
      sleep: float = 0.0, tag: str = "pre", command: str = "plain", env_extra: dict[str, str] | None = None) -> dict[str, Any]:
    return {"key": key, "dataset": dataset, "timeout_raw": timeout_raw, "exit_code": exit_code, "sleep": sleep,
            "tag": tag, "command": command,
            "env_extra": env_extra or {"SANOID_TARGET": dataset, "SANOID_SNAPNAME": "autosnap_x_daily"}}


HAND = {
    "no script_timeout key at all: no timeout on both, pysanoid warns": S(timeout_raw=None),
    "script_timeout = 5, script exits 0": S(timeout_raw="5"),
    "script_timeout = 5, script exits 3": S(timeout_raw="5", exit_code=3),
    "script_timeout = 0: upstream's documented disable": S(timeout_raw="0", sleep=0.2),
    "script_timeout = -5: fails the gate": S(timeout_raw="-5", sleep=0.2),
    "script_timeout = abc: passes the gate, numifies to 0": S(timeout_raw="abc", sleep=0.2),
    "script_timeout = 0x10: numifies to 0": S(timeout_raw="0x10", sleep=0.2),
    "script_timeout = (blank): fails the gate": S(timeout_raw="", sleep=0.2),
    "script_timeout = ' 5': fails the gate": S(timeout_raw=" 5", sleep=0.2),
    "script_timeout = '+5': fails the gate": S(timeout_raw="+5", sleep=0.2),
    "script_timeout = '5 ': 5 s on both": S(timeout_raw="5 ", sleep=0.2),
    "script_timeout = 05: 5 s on both": S(timeout_raw="05", sleep=0.2),
    "script_timeout = '5 # seconds': 5 s on both, not reported": S(timeout_raw="5 # seconds"),
    "script_timeout = 5s: 5 s on both, reported": S(timeout_raw="5s"),
    "script_timeout = 1_0: 1 s on both, reported": S(timeout_raw="1_0"),
    "script_timeout = 1.5: 1 s on both": S(timeout_raw="1.5"),
    "script_timeout = 0.3, script sleeps 0.6 s: no timeout on both": S(timeout_raw="0.3", sleep=0.6),
    "script_timeout = inf: none on both (fresh Perl variable)": S(timeout_raw="inf"),
    "script_timeout = 4294967297: 1 s on both (a C int)": S(timeout_raw="4294967297"),
    "script_timeout = 2147483648: none on both (negative as a C int)": S(timeout_raw="2147483648"),
    "post_snapshot_script key, with SANOID_PRE_FAILURE set": S(key="post_snapshot_script", tag="post", timeout_raw="5",
        env_extra={"SANOID_TARGET": "tank/a", "SANOID_SCRIPT": "post", "SANOID_PRE_FAILURE": "1"}),
    "pruning_script key": S(key="pruning_script", tag="prune", timeout_raw="5",
        env_extra={"SANOID_TARGET": "tank/a", "SANOID_SNAPNAME": "autosnap_x_daily", "SANOID_SCRIPT": "prune"}),
    "dataset/env values containing shell-ish characters": S(dataset="tank/a b", timeout_raw="5",
        env_extra={"SANOID_TARGET": "tank/a b", "SANOID_SNAPNAME": "autosnap_$(x)_daily"}),
    **{f"command: {name}": S(timeout_raw="5", command=name) for name in COMMANDS_ALL},
    "a hook that exits 255": S(timeout_raw="5", exit_code=255),
    "a hook the shell runs that exits 7": S(timeout_raw="5", exit_code=7, command="quoted word"),
}

GATE_FALSE = ["-1", "-5", "-0.5", "0", " 5", "+5", " -5", "", "\t5", "!5", "#5", "%5", ".5"]
PLAIN = ["1", "2", "5", "10", "01", "05", "5 # seconds", "30 ; s", "100", "86400", "00"]
PERL_NUMBER = ["5s", "5,0", "1_0", "1.5", "5.9", "2.0", "10.5", "1e3", "0.5", "0.99", "1e-3", "abc", "0x10", "0.0",
               "0abc", ":junk", "inf", "nan", "infinity", "4294967297", "4294967296", "2147483648", "9999999999", "1e30"]
ALPHABET = list("0123456789" * 3) + list("+-._eE#; \txabinfINF,s")
KEYS = ["pre_snapshot_script", "post_snapshot_script", "pre_pruning_script", "pruning_script"]


def gen(r: random.Random) -> dict[str, Any]:
    pool = r.choices([GATE_FALSE, PLAIN, PERL_NUMBER, None], weights=[3, 3, 4, 2])[0]
    timeout_raw = r.choice(pool) if pool else "".join(r.choice(ALPHABET) for _ in range(r.randint(0, 7)))
    if r.random() < 0.04:
        timeout_raw = None
    key = r.choice(KEYS)
    env_extra = {"SANOID_TARGET": f"tank/{r.choice('abcde')}", "SANOID_SNAPNAME": f"autosnap_{r.randint(0, 9)}_daily",
                 "SANOID_SCRIPT": r.choice(["pre", "post", "prune"])}
    command = r.choice(list(COMMANDS)) if r.random() < 0.5 else "plain"
    scn = S(key=key, dataset=env_extra["SANOID_TARGET"], timeout_raw=timeout_raw,
            exit_code=r.choice([0, 0, 1, 2, 7, 255]), sleep=r.choice([0.0, 0.0, 0.15]), tag=r.choice(["pre", "post", "prune"]),
            command=command, env_extra=env_extra)
    late = random.Random(json.dumps(scn, sort_keys=True))
    if late.random() < 0.15:
        scn["command"] = late.choice(list(FAILING))
    return scn


def show(scn: dict[str, Any], a: dict[str, Any], b: dict[str, Any], diffs: list[str]) -> None:
    print(f"  key={scn['key']} command={scn['command']!r} timeout_raw={scn['timeout_raw']!r} sleep={scn['sleep']} "
          f"exit_code={scn['exit_code']}")
    for k in diffs:
        if k == "warning":
            print(f"    stderr: perl={a['stderr']!r}\n            python={b['stderr']!r}")
        elif k == "reporting":
            print(f"    pysanoid problems={b['problems']!r} stderr={b['stderr']!r}")
        else:
            print(f"    {k}: perl={a[k]!r}\n         python={b[k]!r}")


def verify_upstream_alarm_crashes_process() -> bool:
    """Standalone confirmation, outside the tally, that upstream's runscript still kills the whole process when its
    alarm fires, rather than returning -1. A FAIL means upstream changed or this extraction drifted: re-verify
    DEV-042 in DEVIATIONS.md either way."""
    wd = (WORK / "perl_crash_probe").resolve()
    _setup(wd)
    stdin_obj = {"dataset": "tank/a", "key": "pre_snapshot_script", "timeout_raw": "1",
                 "script": _script_for(S(timeout_raw="1", sleep=2.0)), "env_extra": {}}
    result = run_harness(SCRIPT, stdin_obj=stdin_obj, env={"HOOK_LOG": str(wd / "hook.log")}, cwd=wd, timeout=15)
    crashed = result.data is None and result.returncode == -14
    detail = "still kills the whole process (SIGALRM, uncaught)" if crashed \
        else "did NOT crash as expected -- re-verify DEV-042 in DEVIATIONS.md"
    print(f"[{'CONFIRMED' if crashed else 'NOT REPRODUCED'}] upstream's alarm, when it actually fires, "
          f"{detail} (returncode={result.returncode})")
    return crashed


FORK_WAIT = 12  # seconds: past two of the 5 s retries Perl's fork makes on EAGAIN
_PYSANOID_HOOK = ("import sys; import pysanoid as p\n"
                  "print(p.run_hook_script({'pre_snapshot_script': sys.argv[1], 'script_timeout': '30'},"
                  " 'pre_snapshot_script', 'tank/a', {}), flush=True)\n")


def _no_spare_process() -> None:
    """In the child, before exec: RLIMIT_NPROC at 1, so every fork it makes fails with EAGAIN, as when the system is out
    of processes. The count is this user's, which is always more than one."""
    resource.setrlimit(resource.RLIMIT_NPROC, (1, resource.getrlimit(resource.RLIMIT_NPROC)[1]))


def _start(argv: list[str], wd: Path, stdin: bytes, *, base_env: dict[str, str] | None = None,
           **popen: Any) -> subprocess.Popen[bytes]:
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "stdin").write_bytes(stdin)
    with (wd / "stdin").open("rb") as given:
        return subprocess.Popen(argv, stdin=given, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=wd,
                                env={**(os.environ if base_env is None else base_env), "DIFF_OUT": str(wd / "out.json"),
                                     "HOOK_LOG": str(wd / "hook.log"), "PYTHONPATH": str(DEV_DIR.parent)}, **popen)


def _finish(proc: subprocess.Popen[bytes], wait: float) -> tuple[int | None, str]:
    """(returncode, stdout), or (None, stdout so far) when it is still running after `wait` seconds (then killed)."""
    try:
        out, _ = proc.communicate(timeout=wait)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        return None, out.decode(errors="replace")
    return proc.returncode, out.decode(errors="replace")


def _upstream_hook(wd: Path, command: str, **popen: Any) -> subprocess.Popen[bytes]:
    """upstream's runscript (SCRIPT) running `command` as a pre_snapshot_script, in a process of its own."""
    _setup(wd)
    (wd / "harness.pl").write_text(SCRIPT)
    stdin_obj = {"dataset": "tank/a", "key": "pre_snapshot_script", "timeout_raw": "30", "script": command,
                 "env_extra": {}}
    return _start([PERL, str(wd / "harness.pl")], wd, json.dumps(stdin_obj).encode(), **popen)


def _pysanoid_hook(wd: Path, command: str, **popen: Any) -> subprocess.Popen[bytes]:
    """pysanoid's run_hook_script running `command` as a pre_snapshot_script, in a process of its own."""
    _setup(wd)
    return _start([sys.executable, "-c", _PYSANOID_HOOK, command], wd, b"", **popen)


def verify_fork_failure_waits_upstream_only() -> bool:
    """Standing check of DEV-043, outside the tally. With no process to spare, upstream's system() (its own runscript)
    and a piped open (as its zfs listings use) retry the fork every 5 s and are still waiting after FORK_WAIT seconds;
    pysanoid's run_hook_script returns -1 at once. Perl's own retry, not sanoid's code: re-verify DEV-043 if it fails."""
    root = Path(tempfile.mkdtemp(prefix="pysanoid_fork_"))
    piped = [PERL, "-e",'open(my $fh, "-|", "/bin/true") or die "open: $!"; print "opened\\n"']
    started = time.monotonic()
    system = _upstream_hook(root / "system", "/bin/true", preexec_fn=_no_spare_process)
    piped_open = _start(piped, root, b"", preexec_fn=_no_spare_process)
    rc, out = _finish(_pysanoid_hook(root / "pysanoid", "/bin/true", preexec_fn=_no_spare_process), 5)
    took = time.monotonic() - started
    system_rc = _finish(system, max(0.0, FORK_WAIT - (time.monotonic() - started)))[0]
    piped_rc = _finish(piped_open, 0)[0]
    shutil.rmtree(root, ignore_errors=True)
    ok = system_rc is None and piped_rc is None and rc == 0 and out.strip() == "-1"
    print(f"[{'CONFIRMED' if ok else 'NOT REPRODUCED'}] with no process to spare, upstream's system() and a piped open "
          f"still wait after {FORK_WAIT} s, and pysanoid's hook fails at once (system() rc={system_rc}, piped open "
          f"rc={piped_rc}, None meaning still waiting; pysanoid returned {out.strip()!r} in {took:.1f} s, rc={rc})")
    return ok


def verify_ctrl_c_stops_pysanoid_only() -> bool:
    """Standing check of DEV-044, outside the tally. A hook sends SIGINT to its whole process group, as Ctrl-C at a
    terminal does. Upstream's system() ignores SIGINT while it waits, so only the hook dies and runscript returns its
    wait status (2, the signal). pysanoid dies from SIGINT."""
    root = Path(tempfile.mkdtemp(prefix="pysanoid_sigint_"))
    command = "kill -INT 0; sleep 5"
    upstream = _upstream_hook(root / "upstream", command, start_new_session=True)
    pysanoid = _pysanoid_hook(root / "pysanoid", command, start_new_session=True)
    up_rc, _ = _finish(upstream, 15)
    data = json.loads((root / "upstream" / "out.json").read_text()) if (root / "upstream" / "out.json").exists() else None
    py_rc, _ = _finish(pysanoid, 15)
    shutil.rmtree(root, ignore_errors=True)
    ok = up_rc == 0 and data is not None and data["ret"] == signal.SIGINT and py_rc == -signal.SIGINT
    print(f"[{'CONFIRMED' if ok else 'NOT REPRODUCED'}] Ctrl-C during a hook: upstream carries on past the hook it "
          f"killed, pysanoid stops (upstream: rc={up_rc} runscript returned {data and data['ret']}; pysanoid: rc={py_rc})")
    return ok


def _as_systemd_starts_a_service() -> None:
    """In the child, before exec: SIGPIPE ignored (IgnoreSIGPIPE=yes). The caller passes an environment with no locale."""
    _ = signal.signal(signal.SIGPIPE, signal.SIG_IGN)


def verify_hooks_inherit_what_python_sets_up() -> bool:
    """Standing check of DEV-046, outside the tally. Each side started as systemd starts a service, SIGPIPE ignored and
    no locale set, runs a hook (a Perl one-liner, since Perl's %SIG shows a disposition inherited as ignored) that
    prints its SIGPIPE disposition and $LC_CTYPE. Upstream's hook inherits sanoid's own: IGNORE, unset. pysanoid's gets
    what Python sets up before any of pysanoid runs: SIGPIPE at its default for children, and LC_CTYPE=C.UTF-8 (PEP 538)."""
    root = Path(tempfile.mkdtemp(prefix="pysanoid_inherit_"))
    probe = root / "probe.pl"
    probe.write_text(f"#!{shutil.which(PERL) or PERL}\n"
                     'print "PIPE=", ($SIG{PIPE} // "DEFAULT"), " LC_CTYPE=", ($ENV{LC_CTYPE} // "unset"), "\\n";\n')
    probe.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LC_", "LANG")) and k != "PYTHONCOERCECLOCALE"}
    seen: dict[str, list[str]] = {}
    for how, command in (("exec'd", str(probe)), ("through the shell", f"{probe} ; true")):  # both ways a hook starts
        upstream = _upstream_hook(root / "upstream", command, base_env=env, preexec_fn=_as_systemd_starts_a_service)
        pysanoid = _pysanoid_hook(root / "pysanoid", command, base_env=env, preexec_fn=_as_systemd_starts_a_service)
        seen[how] = [out.split("\n")[0] for _, out in (_finish(upstream, 15), _finish(pysanoid, 15))]
    shutil.rmtree(root, ignore_errors=True)
    ok = all(both == ["PIPE=IGNORE LC_CTYPE=unset", "PIPE=DEFAULT LC_CTYPE=C.UTF-8"] for both in seen.values())
    print(f"[{'CONFIRMED' if ok else 'NOT REPRODUCED'}] started as systemd starts a service, upstream's hook inherits "
          f"SIGPIPE ignored and no locale, pysanoid's SIGPIPE at its default and C.UTF-8, exec'd and through the shell "
          f"({'; '.join(f'{how}: upstream {a!r}, pysanoid {b!r}' for how, (a, b) in seen.items())})")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=400)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()

    tally = {"match": 0, "DIFF": 0}
    standing = True
    if args.mode == "hand":
        for name, scn in HAND.items():
            a, b, diffs = run_both(scn)
            tally["match" if not diffs else "DIFF"] += 1
            print(f"[{'MATCH' if not diffs else 'DIFF '}] {name}")
            if diffs:
                show(scn, a, b, diffs)
        print()
        standing = all([verify_upstream_alarm_crashes_process(), verify_fork_failure_waits_upstream_only(),
                        verify_ctrl_c_stops_pysanoid_only(), verify_hooks_inherit_what_python_sets_up()])
    else:
        r = random.Random(args.seed)
        seen: dict[tuple[str, ...], dict[str, Any]] = {}
        for _ in range(args.n):
            scn = gen(r)
            a, b, diffs = run_both(scn)
            tally["match" if not diffs else "DIFF"] += 1
            if diffs:
                seen.setdefault(tuple(diffs), scn)
        for sig, scn in seen.items():
            a, b, diffs = run_both(scn)
            print(f"\n--- differs on {sig}")
            show(scn, a, b, diffs)
    print(tally, "known classes:", dict(KNOWN))
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if tally["DIFF"] or not standing else 0)


if __name__ == "__main__":
    main()
