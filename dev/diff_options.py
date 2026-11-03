#!/usr/bin/env python3
"""Differential test: upstream's option-parsing block (Module 5) -- the real Getopt::Long call,
the `keys %args < 4` cron default, and the $cacheTTL block -- extracted verbatim, vs
pysanoid.parse_args, on identical argv.

This is a top-level script block, not a named sub, so it's extracted by dev/lib/extract.py's
extract_between (text-anchored, not line-number-anchored -- still fails loudly on a mismatch,
just a softer guarantee than extract_sub's brace-matching; see that function's docstring).

Outcome classes: ok (parsed) / usage (bad option: pod2usage(2) vs argparse's usage error, both exit
2) / exit0 (--help, --version) / die (a negative --cache-ttl). For two "ok" results the *effective*
options are compared: every flag's truthiness, every string value, and cache-ttl's parsed number.

Excludes, by construction (see gen()), argv DEVIATIONS.md documents as a deliberate, decided
divergence (DEV-022, DEV-023, DEV-026: single-dash long options, mixed case, `+opt`, a lone `--`,
`--v`/`--ver`, an option-like value, non-option arguments, `--monitor-*`) -- fuzzing those would just re-demonstrate
a known, already-decided difference on every run. See dev/README.md for how to also exercise that
excluded space if you want to confirm that list is still exhaustive after a change.

Run: python3 dev/diff_options.py hand
     python3 dev/diff_options.py fuzz <n> <seed>
"""
from __future__ import annotations

import argparse
import contextlib
import io
import random
import sys
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.extract import extract_between
from lib.perlrun import PERL_JSON_HEAD, build_harness, run_harness

import pysanoid as p

FLAGS = ["verbose", "debug", "cron", "readonly", "quiet", "force-update", "take-snapshots",
        "prune-snapshots", "force-prune"]
MONITOR = ["monitor-health", "monitor-snapshots", "monitor-capacity"]
VALUED = ["configdir", "cache-dir", "run-dir"]
ALL_OPT_NAMES = FLAGS + MONITOR + VALUED + ["cache-ttl"]

HEAD = PERL_JSON_HEAD + '''
use Getopt::Long qw(:config auto_version auto_help);
use Pod::Usage;
$::VERSION = '2.3.0';
sub make_path { 1 }  # File::Path's real one would try to mkdir the (default /var/...) paths for real

sub __run {
'''
TAIL = '''
return (\\%args, $cacheTTL);
}

my @__r = eval { __run() };
my $__died = $@; $__died =~ s/\\s+$//;
my %__result = (died => $__died, leftover => [@ARGV]);
if (@__r) {
    my ($args, $ttl) = @__r;
    foreach my $n (qw(''' + " ".join(ALL_OPT_NAMES) + ''')) {
        $__result{opts}{$n} = { exists => (exists $args->{$n} ? 1 : 0),
                                value => (defined $args->{$n} ? "$args->{$n}" : undef),
                                truthy => ($args->{$n} ? 1 : 0) };
    }
    $__result{cacheTTL} = "$ttl";
}
open my $__of, '>', $ENV{DIFF_OUT} or die;
print $__of encode_json(\\%__result);
close $__of;
'''

SCRIPT = build_harness(HEAD, extract_between("my %args = (", "$cacheTTL = $args{'cache-ttl'};\n}"), TAIL)


def run_perl(argv: list[str]) -> dict:
    result = run_harness(SCRIPT, argv=argv)
    if result.data is None:
        # pod2usage(2) and Getopt::Long's auto_help/auto_version call exit() directly -- TAIL never
        # runs, so $DIFF_OUT is never written; the exit code is the only signal left.
        kind = {2: "usage", 0: "exit0"}.get(result.returncode, "other")
        return {"kind": kind, "code": result.returncode, "err": result.stderr}
    o = result.data
    if o["died"]:
        return {"kind": "die", "msg": o["died"]}
    return {"kind": "ok", "opts": o["opts"], "leftover": o["leftover"]}


def run_python(argv: list[str]) -> dict:
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ns = p.parse_args(list(argv))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        return {"kind": {2: "usage", 0: "exit0"}.get(code, "other"), "code": code}
    except p.SanoidError as e:
        return {"kind": "die", "msg": str(e)}
    return {"kind": "ok", "ns": ns}


def eff_perl(r: dict) -> dict:
    o = r["opts"]
    e = {n: bool(o[n]["truthy"]) for n in FLAGS}
    e.update({n: o[n]["value"] for n in VALUED})
    ttl = o["cache-ttl"]
    e["cache-ttl"] = int(float(ttl["value"])) if ttl["exists"] else None
    return e


def eff_py(r: dict) -> dict:
    ns = r["ns"]
    e = {n: bool(getattr(ns, n.replace("-", "_"))) for n in FLAGS}
    e.update({n: getattr(ns, n.replace("-", "_")) for n in VALUED})
    e["cache-ttl"] = ns.cache_ttl
    return e


def classify(a: dict, b: dict, argv: list[str]) -> str:
    # --monitor-*: out of scope by design (DEV-026), not a parity question --
    # checked against the raw argv, not the parsed result: if pysanoid refuses to parse at all
    # (an unrecognized option), there IS no parsed opts dict to inspect on that side, but the
    # monitor flag in argv is still the reason, regardless of what upstream's own parse did with it.
    if any(a.startswith("--monitor-") for a in argv):
        return ""
    if a["kind"] == "ok" and a["leftover"]:
        return f"stray args: perl ignores them, python {b['kind']}"
    if a["kind"] != b["kind"]:
        return f"perl {a['kind']} / python {b['kind']}"
    if a["kind"] != "ok":
        return ""
    ea, eb = eff_perl(a), eff_py(b)
    diffs = [k for k in ea if (float(ea[k]) != float(eb[k]) if k == "cache-ttl" and ea[k] is not None and eb[k] is not None
                                else ea[k] != eb[k])]
    return ("value differs: " + ",".join(diffs)) if diffs else ""


def run_both(argv: list[str]) -> tuple[dict, dict, str]:
    a, b = run_perl(argv), run_python(argv)
    return a, b, classify(a, b, argv)


HAND = [
    [], ["--cron"], ["--verbose"], ["--configdir=/x"], ["--configdir", "/x"], ["--cache-ttl=5"],
    ["--cache-ttl", "5"], ["--cache-ttl=0"],
    ["--take-snapshots", "--verbose", "--configdir=/usr/local/etc/sanoid"],
    ["--prune-snapshots", "--verbose", "--configdir=/usr/local/etc/sanoid"],
    ["--cron", "--quiet"], ["--debug"], ["--force-prune"], ["--readonly"], ["--quiet"], ["--force-update"],
    ["--verb"], ["--cro"], ["--take"], ["--prune"], ["--force-u"], ["--conf=/x"], ["--cache-t=5"],
    ["--help"], ["--version"], ["--nope"], ["--verbose=1"], ["--cron=0"],
    ["--configdir"], ["--configdir="], ["--configdir=-x"], ["--cache-ttl"], ["--cache-ttl="],
    ["--cache-ttl=abc"], ["--cache-ttl=1.5"], ["--cache-ttl=-5"], ["--cache-ttl", "-5"],
    ["--cache-ttl=+5"], ["--cache-ttl=007"], ["--cache-ttl=1_000"], ["--cache-ttl=0x10"], ["--cache-ttl=1e3"],
    ["--cache-ttl=5", "--cache-ttl=7"], ["--configdir=/a", "--configdir=/b"], ["--monitor-snapshots"],
]

FULL_FLAGS = ["--" + n for n in FLAGS]
ABBREVS = ["--verb", "--cro", "--rea", "--qui", "--take", "--prune", "--force-u", "--force-p",
          "--conf", "--cache-t", "--cache-d", "--run", "--deb"]
# NOT "--ver": ambiguous with --version, a documented dialect-gap case (DEV-022),
# not a clean spelling
TTLS = ["0", "1", "5", "1200", "-5", "+5", "007", "1_000", "0x10", "1e3", "1.5", "abc", ""]
VALS = ["/x", "/tmp/a b", "rel", "0"]
# NOT an option-like value ("--cron"): upstream reads it as the option's own value (a mandatory
# value is simply the next argument); a documented dialect-gap case (DEV-022),
# not a clean spelling
SPECIAL = ["--nope", "--verbose=1", "--noverbose", "--no-verbose", "--cron=0", "--cron=", "--debug=x",
          "--monitor-snapshots", "--monitor-health"]


def gen(r: random.Random) -> list[str]:
    """Clean spellings only -- the ones outside the documented dialect gap (DEV-022). See
    diff_options.py's own module docstring for why, and dev/README.md for the broader space."""
    argv = []
    if r.random() < 0.03:
        return [r.choice(["--help", "--version"])]
    for _ in range(r.randint(0, 6)):
        k = r.random()
        if k < 0.35:
            argv.append(r.choice(FULL_FLAGS))
        elif k < 0.55:
            name, val = r.choice(VALUED), r.choice(VALS)
            argv += [f"--{name}={val}"] if r.random() < 0.6 else [f"--{name}", val]
        elif k < 0.75:
            val = r.choice(TTLS)
            argv += [f"--cache-ttl={val}"] if r.random() < 0.6 else ["--cache-ttl", val]
        elif k < 0.9:
            a = r.choice(ABBREVS)
            name = a[2:]
            if any(v.startswith(name) for v in VALUED + ["cache-ttl"]):
                argv.append(a + "=" + r.choice([*VALS, "5", "0", "-3", "+7"]))
            else:
                argv.append(a)
        else:
            argv.append(r.choice(SPECIAL))
    return argv


def minimize(argv: list[str], cls: str) -> list[str]:
    i = 0
    while i < len(argv):
        cand = argv[:i] + argv[i + 1:]
        if run_both(cand)[2] == cls:
            argv = cand
        else:
            i += 1
    return argv


def show(argv: list[str], a: dict, b: dict, cls: str) -> None:
    print(f"  argv={argv!r}: {cls}")
    for side, r in (("perl  ", a), ("python", b)):
        detail = eff_perl(r) if (r["kind"] == "ok" and side == "perl  ") else (eff_py(r) if r["kind"] == "ok" else r.get("msg", ""))
        print(f"      {side}: {r['kind']:6} {detail}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=2000)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()

    tally = {"match": 0, "DIFF": 0}
    if args.mode == "hand":
        for argv in HAND:
            a, b, cls = run_both(argv)
            tally["match" if not cls else "DIFF"] += 1
            if cls:
                show(argv, a, b, cls)
            else:
                print(f"[MATCH] {argv!r}")
    else:
        r = random.Random(args.seed)
        first: dict[str, list[str]] = {}
        for _ in range(args.n):
            argv = gen(r)
            a, b, cls = run_both(argv)
            tally["match" if not cls else "DIFF"] += 1
            if cls:
                first.setdefault(cls, argv)
        for cls, argv in first.items():
            mini = minimize(list(argv), cls)
            a, b, c2 = run_both(mini)
            show(mini, a, b, c2)
    print(tally)
    sys.exit(1 if tally["DIFF"] else 0)


if __name__ == "__main__":
    main()
