#!/usr/bin/env python3
"""Differential test: upstream's script-level main body (Module 5, lines 1-144 -- everything from
the top of the script through `exit 0;`), extracted verbatim with its subs (init, getsnaps, take_
snapshots, prune_snapshots, blabber, the monitor_* subs) stubbed to log the state they're called
with, vs pysanoid.main() with the same functions monkeypatched. Same argv, real temp directories.

Compared: exit status, the ordered call log (which steps ran, and what verbose/readonly/cache-ttl/
force-update each one saw), the words of the --force-prune warning and of the negative --cache-ttl
error (without the ` at <script> line N.` Perl adds), the directories created, and the final
$ENV{PATH}.

Run: python3 dev/diff_flow.py hand
     python3 dev/diff_flow.py fuzz <n> <seed>
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import random
import re
import shutil
import sys
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.extract import extract_between
from lib.perlrun import PERL_JSON_HEAD, build_harness, run_harness

import pysanoid as p

WORK = DEV_DIR / "work" / "flow"
MESSAGE_RE = re.compile(r"^(WARN: --force-prune|ERROR: cache-ttl).*?(?= at \S+ line \d+\.$|$)", re.MULTILINE)
BASE_PATH = os.environ["PATH"]

STUBS = '''

# ---- stubs: log the state each sub would have been given, instead of doing the real thing ----
sub _state { return { verbose => ($args{verbose} ? 1 : 0), readonly => ($args{readonly} ? 1 : 0),
                      debug => ($args{debug} ? 1 : 0), quiet => ($args{quiet} ? 1 : 0),
                      ttl => "$cacheTTL", force_update => ($args{'force-update'} ? 1 : 0) } }
sub init            { my ($c, $d) = @_; push @LOG, ['init', { %{ _state() }, conf_file => $c, default_conf_file => $d,
                      cache_dir_exists => (-d $cache_dir ? 1 : 0), run_dir_exists => (-d $run_dir ? 1 : 0) }]; return (); }
sub getsnaps        { my ($cfg, $ttl, $force) = @_; push @LOG, ['getsnaps', { %{ _state() }, ttl_arg => "$ttl",
                      forcecacheupdate => ($force ? 1 : 0) }]; return (); }
sub getsnapsbytype  { return (); }
sub getsnapsbypath  { return (); }
sub blabber         { push @LOG, ['blabber', _state()]; }
sub monitor_snapshots { push @LOG, ['monitor_snapshots', _state()]; }
sub monitor_health    { push @LOG, ['monitor_health', _state()]; }
sub monitor_capacity  { push @LOG, ['monitor_capacity', _state()]; }
sub take_snapshots  { push @LOG, ['take', _state()]; }
sub prune_snapshots { push @LOG, ['prune', _state()]; }

END {
    my $status = $?;
    if ($ENV{DIFF_OUT}) {
        open my $of, '>', $ENV{DIFF_OUT} or die;
        print $of encode_json({ log => \\@LOG, status => $status, path => $ENV{PATH} });
        close $of;
    }
    $? = $status;
}
'''

SCRIPT = build_harness(
    PERL_JSON_HEAD + "use JSON::PP; our @LOG;\n",
    extract_between("#!/usr/bin/perl", "exit 0;").replace("use Config::IniFiles;", "# (stubbed below)"),
    STUBS,
)


def argv_for(scn: dict, wd: Path) -> list[str]:
    return [*scn["flags"], f"--configdir={wd / 'conf'}", f"--cache-dir={wd / 'cache'}", f"--run-dir={wd / 'run'}"]


def canon_perl(o: dict) -> list:
    log = []
    for name, st in o["log"]:
        if name == "init":
            log.append([name, {"ttl": int(float(st["ttl"])), "force_update": st["force_update"], "debug": st["debug"],
                               "verbose": st["verbose"], "cache_dir_exists": st["cache_dir_exists"],
                               "run_dir_exists": st["run_dir_exists"]}])
        elif name == "getsnaps":
            log.append([name, {"ttl_arg": int(float(st["ttl_arg"])), "forcecacheupdate": st["forcecacheupdate"],
                               "verbose": st["verbose"]}])
        elif name in ("take", "prune"):
            log.append([name, {"verbose": st["verbose"], "readonly": st["readonly"]}])
        else:
            log.append([name, {}])
    return log


def messages(stderr: str) -> list[str]:
    """The --force-prune warning and the negative --cache-ttl error, as written, minus Perl's location suffix."""
    return [m.group(0) for m in MESSAGE_RE.finditer(stderr)]


def run_perl(scn: dict, wd: Path) -> dict:
    os.environ["PATH"] = BASE_PATH
    (wd / "conf").mkdir(parents=True)
    result = run_harness(SCRIPT, argv=argv_for(scn, wd), cwd=wd)
    if result.data is None:
        raise RuntimeError(f"perl harness produced no result:\n{result.stderr}")
    o = result.data
    return {"log": canon_perl(o), "ok": o["status"] == 0, "messages": messages(result.stderr),
           "path": o["path"], "dirs": [(wd / "cache").is_dir(), (wd / "run").is_dir()]}


def run_python(scn: dict, wd: Path) -> dict:
    (wd / "conf").mkdir(parents=True)
    LOG: list = []

    def fake_load_config(configdir, zfs):
        LOG.append(["init", {"ttl": int(zfs.cache_ttl), "force_update": int(zfs.force_update), "debug": int(zfs.debug),
                             "cache_dir_exists": int((wd / "cache").is_dir()), "run_dir_exists": int((wd / "run").is_dir())}])
        return {}

    def fake_flush(self, run_dir, verbose=False, problems=None):
        LOG[0][1]["verbose"] = int(bool(verbose))  # upstream's init prints its dataset-cache INFO with THIS verbose

    def fake_get_snapshots(config, cache_dir, run_dir, cache_ttl, force_update=False, verbose=False, debug=False,
                           problems=None):
        LOG.append(["getsnaps", {"ttl_arg": int(cache_ttl), "forcecacheupdate": int(bool(force_update)), "verbose": int(bool(verbose))}])
        return {}

    def fake_take(config, by_type, readonly=False, verbose=False, problems=None):
        LOG.append(["take", {"verbose": int(bool(verbose)), "readonly": int(bool(readonly))}])
        return {}

    def fake_prune(config, snaps, by_type, cache_dir, run_dir, *, readonly=False, verbose=False, problems=None):
        LOG.append(["prune", {"verbose": int(bool(verbose)), "readonly": int(bool(readonly))}])
        return []

    saved = {k: getattr(p, k) for k in ("load_config", "get_snapshots", "snapshots_by_type", "snapshots_by_path",
                                        "print_debug_dump", "take_snapshots", "add_cached_snapshots", "prune_snapshots",
                                        "remove_cached_snapshots")}
    saved_flush = p.ZfsClient.flush_dataset_cache
    p.load_config, p.get_snapshots, p.take_snapshots, p.prune_snapshots = fake_load_config, fake_get_snapshots, fake_take, fake_prune
    p.snapshots_by_type = lambda *a, **k: {}
    p.snapshots_by_path = lambda *a, **k: {}
    p.print_debug_dump = lambda *a, **k: LOG.append(["blabber", {}])
    p.add_cached_snapshots = lambda *a, **k: None
    p.remove_cached_snapshots = lambda *a, **k: None
    p.ZfsClient.flush_dataset_cache = fake_flush
    os.environ["PATH"] = BASE_PATH
    err, code, crashed = io.StringIO(), None, None
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = p.main(argv_for(scn, wd))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except Exception as e:  # noqa: BLE001 - harness: a traceback is a finding
        code, crashed = 1, f"{type(e).__name__}: {e}"
    finally:
        for k, v in saved.items():
            setattr(p, k, v)
        p.ZfsClient.flush_dataset_cache = saved_flush
    r = {"log": list(LOG), "ok": code == 0, "messages": messages(err.getvalue()),
        "path": os.environ["PATH"], "dirs": [(wd / "cache").is_dir(), (wd / "run").is_dir()]}
    if crashed:
        r["crashed"] = crashed
    return r


def run_both(scn: dict, n: int) -> tuple[dict, dict, list[str]]:
    root = WORK / f"f{os.getpid()}_{n}"
    res = {}
    for side, fn in (("perl", run_perl), ("py", run_python)):
        wd = root / side
        shutil.rmtree(wd, ignore_errors=True)
        res[side] = fn(scn, wd)
    shutil.rmtree(root, ignore_errors=True)
    a, b = res["perl"], res["py"]
    # DEV-015: upstream's truthiness test discards an explicit 0 and keeps the 1200s default.
    # Canonicalized so it doesn't show as a false "DIFF" on every run.
    la = [[n, {**s, **({"ttl": 0} if s.get("ttl") == 1200 else {}), **({"ttl_arg": 0} if s.get("ttl_arg") == 1200 else {})}]
         for n, s in a["log"]] if scn.get("ttl") == "0" else a["log"]
    diffs = []
    if a["ok"] != b["ok"]:
        diffs.append("exit status")
    if la != b["log"]:
        diffs.append("call log")
    if a["messages"] != b["messages"]:
        diffs.append("messages")
    if a["dirs"] != b["dirs"] and scn.get("ttl") != "-3":  # upstream makes cache/run dirs BEFORE dying on a negative TTL
        diffs.append("directories")
    if not (a["path"] == b["path"] == BASE_PATH + ":/bin:/sbin"):
        diffs.append("PATH")
    if b.get("crashed"):
        diffs.append("python traceback")
    return a, b, diffs


FLAG_POOL = ["--cron", "--verbose", "--debug", "--readonly", "--quiet", "--force-update", "--force-prune",
            "--take-snapshots", "--prune-snapshots"]


def scn_of(flags: list[str], ttl: str | None = None) -> dict:
    flags = list(flags)
    if ttl is not None:
        flags.append(f"--cache-ttl={ttl}")
    return {"flags": flags, "ttl": ttl}


HAND = [
    scn_of([]), scn_of(["--cron"]), scn_of(["--take-snapshots", "--verbose"]), scn_of(["--prune-snapshots", "--verbose"]),
    scn_of(["--cron", "--quiet"]), scn_of(["--cron", "--quiet", "--verbose"]), scn_of(["--debug"]),
    scn_of(["--debug", "--take-snapshots"]), scn_of(["--debug", "--cron", "--quiet"]), scn_of(["--readonly"]),
    scn_of(["--quiet"]), scn_of(["--verbose"]), scn_of(["--force-update"]), scn_of(["--force-prune"]),
    scn_of(["--force-prune", "--cron"]), scn_of(["--cron"], "0"), scn_of(["--cron"], "5"),
    scn_of(["--take-snapshots"], "-3"), scn_of([], "5"), scn_of(["--take-snapshots", "--prune-snapshots"]),
    scn_of(["--cron", "--take-snapshots"]), scn_of(["--readonly", "--cron"]), scn_of(["--force-update", "--cron"]),
    scn_of(["--debug", "--force-update"]),
]


def gen(r: random.Random) -> dict:
    flags = r.sample(FLAG_POOL, r.randint(0, 6))
    return scn_of(flags, r.choice([None, None, None, "0", "5", "1200", "-3"]))


def minimize(scn: dict, sig: tuple, n: int) -> dict:
    flags = list(scn["flags"])
    i = 0
    while i < len(flags):
        cand = flags[:i] + flags[i + 1:]
        c_scn = {"flags": cand, "ttl": scn["ttl"] if any(f.startswith("--cache-ttl") for f in cand) else None}
        if tuple(run_both(c_scn, n)[2]) == sig:
            flags = cand
        else:
            i += 1
    return {"flags": flags, "ttl": scn["ttl"] if any(f.startswith("--cache-ttl") for f in flags) else None}


def show(scn: dict, a: dict, b: dict, diffs: list[str]) -> None:
    print(f"  flags={scn['flags']}: differs on {diffs}")
    if "call log" in diffs:
        print("      perl  :", a["log"])
        print("      python:", b["log"])
    for k in ("ok", "messages", "dirs"):
        if a[k] != b[k]:
            print(f"      {k}: perl={a[k]} python={b[k]}")
    if b.get("crashed"):
        print("      python crashed:", b["crashed"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=500)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()

    tally = {"match": 0, "DIFF": 0}
    if args.mode == "hand":
        for i, scn in enumerate(HAND):
            a, b, diffs = run_both(scn, i)
            tally["match" if not diffs else "DIFF"] += 1
            if diffs:
                show(scn, a, b, diffs)
            else:
                print(f"[MATCH] {scn['flags']} -> {[n for n, _ in a['log']]} ok={a['ok']}")
    else:
        r = random.Random(args.seed)
        first: dict[tuple, dict] = {}
        for i in range(args.n):
            scn = gen(r)
            a, b, diffs = run_both(scn, i)
            tally["match" if not diffs else "DIFF"] += 1
            if diffs:
                first.setdefault(tuple(diffs), scn)
        for sig, scn in first.items():
            m = minimize(scn, sig, 99999)
            a, b, diffs = run_both(m, 99999)
            show(m, a, b, diffs)
    print(tally)
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if tally["DIFF"] else 0)


if __name__ == "__main__":
    main()
