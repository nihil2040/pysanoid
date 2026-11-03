#!/usr/bin/env python3
"""Differential test: upstream's --debug dump -- blabber, fed by getsnapsbytype and getsnapsbypath, all three
extracted verbatim and run on a pinned clock -- vs pysanoid's print_debug_dump, fed by snapshots_by_type and
snapshots_by_path, on the same config and snapshots.

The whole output is compared as bytes: the Data::Dumper config dump exactly, and the per-dataset summary after
sorting its blocks and each block's type lines, since upstream's order there is Perl's per-process random hash order
(DEV-048). Strings travel as hex, so names and values can hold any bytes. A config value is a
Perl integer (a toggle, initialized, zfs_recursion) or a byte string, as upstream's init leaves it, because
Data::Dumper prints the two differently. Creation times are the digit strings zfs or a damaged cache gives, damaged
ones included, since upstream numifies them its own way.

Perl scoping note: blabber, getsnapsbytype and getsnapsbypath read the OUTER lexical %config/%snaps/... directly,
not the parameters they receive, so those hashes are declared before the subs are compiled.

Run: python3 dev/diff_debug_dump.py hand
     python3 dev/diff_debug_dump.py fuzz <n> <seed>
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.extract import extract_sub
from lib.perlrun import (
    PERL_JSON_HEAD,
    READ_SCENARIO_PERL,
    WRITE_RESULT_PERL,
    build_harness,
    run_harness,
)

import pysanoid as p

HEAD = PERL_JSON_HEAD + '''
use Data::Dumper;
our $NOW = 0;
BEGIN { *CORE::GLOBAL::time = sub () { $main::NOW } }
my (%config, %snaps, %snapsbytype, %snapsbypath);
'''

TAIL = READ_SCENARIO_PERL + r'''
$NOW = $__scn->{now};
for my $sec (@{ $__scn->{config} }) {
    my ($name, $pairs) = @$sec;
    my $h = $config{pack("H*", $name)} = {};
    $h->{pack("H*", $_->[0])} = $_->[1] eq "i" ? 0 + $_->[2] : pack("H*", $_->[2]) for @$pairs;
}
for my $fs (@{ $__scn->{snaps} }) {
    my ($path, $list) = @$fs;
    for my $s (@$list) {
        my ($name, $type, $ctime) = @$s;
        $snaps{pack("H*", $path)}{pack("H*", $name)} = { ctime => "$ctime", type => defined $type ? pack("H*", $type) : undef };
    }
}
{
    no warnings 'uninitialized';  # a snapshot with no type; Perl's own warning, not compared (DEV-027)
    %snapsbytype = getsnapsbytype(\%config, \%snaps);
}
%snapsbypath = getsnapsbypath(\%config, \%snaps);
open(my $__fh, '>', \my $__captured) or die;
select $__fh;
{
    no warnings 'uninitialized';
    blabber(\%config, \%snaps, \%snapsbytype, \%snapsbypath);
}
select STDOUT;
close $__fh;
my %__result = ( text => unpack("H*", $__captured) );
''' + WRITE_RESULT_PERL

SCRIPT = build_harness(HEAD, "\n".join(extract_sub(name) for name in ("getsnapsbytype", "getsnapsbypath", "blabber")),
                       TAIL)

DUMP_RE = re.compile(rb"\A\*{6} CONFIGS \*{6}\n\$VAR1 = (?:\{\};|\{\n.*?\n {8}\};)\n\n", re.DOTALL)
TYPE_START = re.compile(rb" {10}\d")


def _canon_block(block: bytes) -> bytes:
    """A dataset's block with its type entries (a count line and the two after it) sorted."""
    lines = block.split(b"\n")
    starts = [i for i, line in enumerate(lines) if TYPE_START.match(line)]
    if not starts:
        return block
    chunks = sorted(b"\n".join(lines[i:i + 3]) for i in starts)
    return b"\n".join([*lines[:starts[0]], *chunks, *lines[starts[-1] + 3:]])


def canon(text: bytes) -> bytes:
    """The dump as is, then the summary's blocks sorted, each with its type entries sorted (see the docstring)."""
    m = DUMP_RE.match(text)
    if m is None:
        return text  # not the expected shape: compared as is, so the difference shows
    blocks = [b for b in re.split(rb"(?m)^(?=Filesystem )", text[m.end():]) if b]
    return text[:m.end()] + b"".join(sorted(_canon_block(b) for b in blocks))


def _hex(text: str) -> str:
    return text.encode("utf-8", "surrogateescape").hex()


def _perl_scenario(scn: dict[str, Any]) -> dict[str, Any]:
    return {
        "now": scn["now"],
        "config": [[_hex(sec), [[_hex(k), "i", int(v)] if isinstance(v, bool) else [_hex(k), "s", _hex(v)]
                                for k, v in cfg.items()]] for sec, cfg in scn["config"].items()],
        "snaps": [[_hex(path), [[_hex(n), None if t is None else _hex(t), c] for n, t, c in listed]]
                  for path, listed in scn["snaps"].items()],
    }


def run_perl(scn: dict[str, Any]) -> bytes:
    result = run_harness(SCRIPT, stdin_obj=_perl_scenario(scn))
    if result.data is None:
        raise RuntimeError(f"perl harness produced no result:\n{result.stderr}")
    return canon(bytes.fromhex(result.data["text"]))


def run_python(scn: dict[str, Any]) -> bytes:
    snaps = {path: {n: p.Snapshot(path, n, t or "", p._perl_digits(c)) for n, t, c in listed}
             for path, listed in scn["snaps"].items()}
    config = scn["config"]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        p.print_debug_dump(config, p.snapshots_by_type(snaps, config, now=scn["now"]),
                           p.snapshots_by_path(snaps, config, now=scn["now"]))
    return canon(out.getvalue().encode("utf-8", "surrogateescape"))


def run_both(scn: dict[str, Any]) -> tuple[bytes, bytes, bool]:
    a, b = run_perl(scn), run_python(scn)
    return a, b, a == b


NOW = 1_790_000_000


def _ago(seconds: int) -> str:
    return str(NOW - seconds)


def _cfg(path: str, **values: Any) -> dict[str, Any]:
    return {"autosnap": True, "autoprune": True, "path": path, **values}


HAND: dict[str, dict[str, Any]] = {
    "no snapshots at all": {
        "now": NOW, "config": {"tank/a": _cfg("tank/a", daily="7")}, "snaps": {}},
    "one type, one snapshot": {
        "now": NOW, "config": {"tank/a": _cfg("tank/a", daily="7")},
        "snaps": {"tank/a": [("autosnap_x_daily", "daily", _ago(3600))]}},
    "several types, several snapshots each": {
        "now": NOW, "config": {"tank/a": _cfg("tank/a", daily="7", hourly="24", weekly="0")},
        "snaps": {"tank/a": [("autosnap_a_daily", "daily", _ago(90000)), ("autosnap_c_daily", "daily", _ago(1800)),
                             ("autosnap_y_hourly", "hourly", _ago(3900)), ("autosnap_z_hourly", "hourly", _ago(300))]}},
    "two filesystems, one empty one not": {
        "now": NOW, "config": {"a": _cfg("tank/a", daily="7"), "b": _cfg("tank/b", hourly="12")},
        "snaps": {"tank/b": [("autosnap_y_hourly", "hourly", _ago(900)), ("autosnap_x_hourly", "hourly", _ago(4500))]}},
    "a type the config never set, and a snapshot with no type": {
        "now": NOW, "config": {"tank/a": _cfg("tank/a", daily="7")},
        "snaps": {"tank/a": [("autosnap_x_really", "really", _ago(3600)), ("autosnap_y.ly", None, _ago(7200))]}},
    "fractional hours, rounding": {
        "now": NOW, "config": {"tank/a": _cfg("tank/a")},
        "snaps": {"tank/a": [("autosnap_x_daily", "daily", _ago(5410)), ("autosnap_x_hourly", "hourly", _ago(540))]}},
    "a snapshot made after the clock (a negative age)": {
        "now": NOW, "config": {"tank/a": _cfg("tank/a")},
        "snaps": {"tank/a": [("autosnap_x_daily", "daily", _ago(-180))]}},
    "toggles, marks and counts as init leaves them": {
        "now": NOW, "config": {"tank": _cfg("tank", autoprune=False, zfs_recursion=True, hourly="5", skip_children=""),
                               "tank/a": _cfg("tank/a", autosnap=False, initialized=True, zfs_recursion=True,
                                              pre_snapshot_script="/opt/it's \\ here", daily="90")},
        "snaps": {}},
    "names with a quote, a backslash, UTF-8 and a lone byte": {
        "now": NOW, "config": {name: _cfg(name) for name in ("tank/it's", "tank/a\\b", "tank/café", "tank/\udcff",
                                                             "tank/\U0001f600")},
        "snaps": {"tank/café": [("autosnap_é_daily", "daily", _ago(60))]}},
    "creation times from a damaged cache": {
        "now": NOW, "config": {f"tank/{c}": _cfg(f"tank/{c}") for c in "abcdef"},
        "snaps": {"tank/a": [("autosnap_x_daily", "daily", "")], "tank/b": [("autosnap_x_daily", "daily", "9" * 400)],
                  "tank/c": [("autosnap_x_daily", "daily", "9223372036854775808")],
                  "tank/d": [("autosnap_x_daily", "daily", "18446744073709551615")],
                  "tank/e": [("autosnap_x_daily", "daily", "03109049318243306005523")],
                  "tank/f": [("autosnap_x_daily", "daily", "12345678901234567")]}},
    "no sections at all": {"now": NOW, "config": {}, "snaps": {}},
    "two sections on one path": {
        "now": NOW, "config": {"tank/b": _cfg("tank/b", daily="3"), "alias": _cfg("tank/b", daily="6")},
        "snaps": {"tank/b": [("autosnap_x_daily", "daily", _ago(100))]}},
}

TYPES: list[str | None] = ["daily", "hourly", "weekly", "monthly", "yearly", "frequently", "really", None]
NAMES = ["tank/a", "tank/b", "pool/data", "tank/it's", "tank/a\\b", "tank/café", "tank/\udcff", "tank/\U0001f600",
         "tank/Z", "tank/_"]
TEXT = ["", "0", "7", "48", "90 ", "3o", "it's", "a\\b", "/opt/hook --x", "café", "\udcfe", "\t5"]
DAMAGED = ["", "0", "9" * 400, "9223372036854775808", "18446744073709551615", "03109049318243306005523",
           "12345678901234567", "1" * 25]


def _ctime(r: random.Random, used: set[str]) -> str:
    while True:
        c = r.choice(DAMAGED) if r.random() < 0.15 else _ago(r.randint(-3600, 400 * 86400))
        if c not in used:
            used.add(c)
            return c


def gen(r: random.Random) -> dict[str, Any]:
    config: dict[str, dict[str, Any]] = {}
    for name in r.sample(NAMES, r.randint(0, 4)):
        path = name if r.random() < 0.85 or not config else r.choice([c["path"] for c in config.values()])
        cfg: dict[str, Any] = {"path": path}
        for key in r.sample(["autosnap", "autoprune", "monitor", "initialized", "zfs_recursion"], r.randint(0, 5)):
            cfg[key] = r.random() < 0.6
        for key in r.sample(["hourly", "daily", "weekly", "monthly", "yearly", "frequently", "pre_snapshot_script",
                             "skip_children"], r.randint(0, 6)):
            cfg[key] = r.choice(TEXT)
        config[name] = cfg
    snaps: dict[str, list[tuple[str, str | None, str]]] = {}
    for path in {c["path"] for c in config.values()}:
        used: set[str] = set()
        listed = []
        for i in range(r.randint(0, 6)):
            t = r.choice(TYPES)
            listed.append((f"autosnap_{i}_{t}" if t else f"autosnap_{i}.ly", t, _ctime(r, used)))
        snaps[path] = listed
    return {"now": NOW + r.randint(0, 59), "config": config, "snaps": snaps}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=300)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()

    tally = {"match": 0, "DIFF": 0}
    if args.mode == "hand":
        for name, scn in HAND.items():
            a, b, ok = run_both(scn)
            tally["match" if ok else "DIFF"] += 1
            print(f"[{'MATCH' if ok else 'DIFF '}] {name}")
            if not ok:
                print(f"  perl  ={a!r}\n  python={b!r}")
    else:
        r = random.Random(args.seed)
        first = None
        for _ in range(args.n):
            scn = gen(r)
            a, b, ok = run_both(scn)
            tally["match" if ok else "DIFF"] += 1
            if not ok and first is None:
                first = (scn, a, b)
        if first:
            scn, a, b = first
            print("--- first mismatch")
            print("scenario:", json.dumps(scn, ensure_ascii=True, default=list)[:600])
            print(f"perl  ={a!r}\npython={b!r}")
    print(tally)
    sys.exit(1 if tally["DIFF"] else 0)


if __name__ == "__main__":
    main()
