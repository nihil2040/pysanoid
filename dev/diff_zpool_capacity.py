#!/usr/bin/env python3
"""Differential test: upstream's get_zpool_capacity, extracted verbatim, vs pysanoid's.

prune_defer asks `zpool list -H -o cap <pool>` how full the pool is. diff_prune.py stubs that query on both sides
(it compares what is done with the number); this target compares the reading itself: which answers count as a
capacity, the number read, the words a refusal is given in, and what of zpool's own stderr reaches the terminal,
byte for byte. A fake `zpool` (dev/fake_zfs/zpool) gives each side the same stdout, stderr and exit.

One known class, DEV-016: an answer holding MORE than a capacity, a second word or a
second line, or anything after it that Perl's \\s doesn't match (a no-break space, U+001C). Upstream reads its first word and returns it; pysanoid refuses the answer. The class is checked against
the rule as stated here (_more), both ways: upstream must have returned that first word's number and pysanoid given
its own refusal, and an answer the rule doesn't cover must read the same on both sides.

Left out of what reaches stderr: Perl's own warnings about upstream's code on an answer with no first line.

Run: python3 dev/diff_zpool_capacity.py hand
     python3 dev/diff_zpool_capacity.py fuzz <n> <seed>     (one Perl process for all n answers)
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import random
import re
import shutil
import sys
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.extract import extract_subs
from lib.perlrun import (
    PERL_JSON_HEAD,
    READ_SCENARIO_PERL,
    WRITE_RESULT_PERL,
    build_harness,
    run_harness,
)

import pysanoid as p

WORK = DEV_DIR / "work" / "zpool_capacity"
FAKE_BIN = DEV_DIR / "fake_zfs"
os.environ["PATH"] = f"{FAKE_BIN}{os.pathsep}{os.environ['PATH']}"
p.ZPOOL = str(FAKE_BIN / "zpool")  # whatever zpool this machine has

HEAD = PERL_JSON_HEAD + '''
my $zpool = 'zpool';
'''

# For each answer: the fake zpool's file, then upstream's sub with STDERR sent to a file of its own.
TAIL = f'''
{READ_SCENARIO_PERL}
my @out;
foreach my $case (@{{ $__scn->{{cases}} }}) {{
    open(my $st, '>', $ENV{{FAKE_ZPOOL}}) or die "cannot write $ENV{{FAKE_ZPOOL}}: $!";
    print $st encode_json($case);
    close $st;
    open(my $save, '>&', \\*STDERR) or die "dup STDERR: $!";
    open(STDERR, '>', $__scn->{{err}}) or die "redirect STDERR: $!";
    my $cap = eval {{ get_zpool_capacity('tank') }};
    my $died = $@;
    close STDERR;
    open(STDERR, '>&', $save) or die "restore STDERR: $!";
    open(my $rf, '<:raw', $__scn->{{err}}) or die "read stderr: $!";
    my $said = do {{ local $/; <$rf> }};
    close $rf;
    push @out, {{ cap => (defined $cap ? "$cap" : undef), died => unpack("H*", $died), said => unpack("H*", $said // '') }};
}}
my %__result = (results => \\@out);
{WRITE_RESULT_PERL}
'''
SCRIPT = build_harness(HEAD, extract_subs("get_zpool_capacity"), TAIL)

_PERLS_OWN = re.compile(rb"Use of uninitialized value [^\n]* at \S+ line \d+(?:, <\S+> (?:line|chunk) \d+)?\.\n")
MORE = "an answer holding more than a capacity: upstream reads its first word, pysanoid refuses it"


def A(stdout: bytes, stderr: bytes = b"", rc: int = 0, kill: int = 0) -> dict:
    return {"stdout": stdout.hex(), "stderr": stderr.hex(), "rc": rc, "kill": kill}


def _more(stdout: bytes) -> int | None:
    """The rule for the known class, stated apart from pysanoid's code: the number upstream reads (the first word of
    the first line, a capacity) when the answer, trimmed of Perl's \\s, is more than that word. None when the rule doesn't
    apply."""
    first = stdout.split(b"\n", 1)[0].split(b" ", 1)[0]
    if not re.fullmatch(rb"[0-9]{1,3}%", first):
        return None
    return None if stdout.strip(b" \t\n\r\f\v") == first else int(first[:-1])


def run_perl(cases: list[dict]) -> list[dict]:
    WORK.mkdir(parents=True, exist_ok=True)
    result = run_harness(SCRIPT, stdin_obj={"cases": cases, "err": str(WORK / "perl_stderr")},
                         env={"FAKE_ZPOOL": str(WORK / "perl_zpool.json")}, timeout=300)
    if result.data is None:
        raise RuntimeError(f"perl harness produced no result:\n{result.stderr}")
    out = []
    for r in result.data["results"]:
        died = bytes.fromhex(r["died"]).removesuffix(b"\n")
        out.append({"value": None if died else int(r["cap"]), "died": died,
                    "said": _PERLS_OWN.sub(b"", bytes.fromhex(r["said"]))})
    return out


def run_python(cases: list[dict]) -> list[dict]:
    WORK.mkdir(parents=True, exist_ok=True)
    os.environ["FAKE_ZPOOL"] = str(WORK / "python_zpool.json")
    out = []
    for case in cases:
        (WORK / "python_zpool.json").write_text(json.dumps(case))
        err = io.StringIO()
        value, died = None, b""
        with contextlib.redirect_stderr(err):
            try:
                value = p.get_zpool_capacity("tank")
            except p.SanoidError as e:  # as main prints it
                died = f"{e.prefix}: {e}".encode("utf-8", "surrogateescape")
        out.append({"value": value, "died": died, "said": err.getvalue().encode("utf-8", "surrogateescape")})
    return out


def compare(case: dict, a: dict, b: dict) -> tuple[list[str], bool]:
    """(the keys that differ, whether the case is the known class)."""
    stdout = bytes.fromhex(case["stdout"])
    upstreams = _more(stdout)
    diffs = ["said"] if a["said"] != b["said"] else []
    if upstreams is None:
        return diffs + [k for k in ("value", "died") if a[k] != b[k]], False
    refusal = f"FATAL: 'zpool list -H -o cap tank' returned more than a capacity value ({stdout.decode('utf-8', 'surrogateescape')!r})"
    if a["value"] != upstreams or a["died"]:
        diffs.append("the rule (upstream)")
    if b["value"] is not None or b["died"] != refusal.encode("utf-8", "surrogateescape"):
        diffs.append("the rule (pysanoid)")
    return diffs, True


HAND: dict[str, dict] = {
    "85%: the answer real zpool gives": A(b"85%\n"),
    "0%": A(b"0%\n"),
    "100%": A(b"100%\n"),
    "999%: three digits are a capacity to both": A(b"999%\n"),
    "085%": A(b"085%\n"),
    "no newline": A(b"85%"),
    "trailing spaces": A(b"85%  \n"),
    "a blank line after": A(b"85%\n\n"),
    "blanks on a second line": A(b"85%\n \n"),
    "the pool isn't there: nothing on stdout, zpool's complaint, exit 1": A(b"", b"cannot open 'tank': no such pool\n", 1),
    "a capacity and exit 1: both read it": A(b"85%\n", b"", 1),
    "a capacity and a warning": A(b"85%\n", b"warning: a device is missing\n"),
    "a warning in bytes that aren't UTF-8, with no newline": A(b"85%\n", b"warning: caf\xe9"),
    "a complaint after a blank line and a tab, and no capacity": A(b"-\n", b"\n\tindented\n\n", 2),
    "killed before it answers": A(b"", b"", 0, 9),
    "killed after it answers": A(b"85%\n", b"", 0, 9),
    "a leading space: not a capacity to upstream": A(b" 85%\n"),
    "a leading tab": A(b"\t85%\n"),
    "a trailing tab": A(b"85%\t\n"),
    "CRLF": A(b"85%\r\n"),
    "a blank first line": A(b"\n85%\n"),
    "a form feed": A(b"85%\f\n"),
    "four digits": A(b"1000%\n"),
    "no percent sign": A(b"85\n"),
    "no digits": A(b"%\n"),
    "a dash, as zpool prints for no value": A(b"-\n"),
    "a word first": A(b"x 85%\n"),
    "bytes that aren't UTF-8": A(b"caf\xe9\n"),
    "digits of another script": A("٤٢%\n".encode()),
    "a no-break space after": A("85% \n".encode()),
    "only blanks": A(b"   \n"),
    "KNOWN: two lines": A(b"85%\n90%\n"),
    "KNOWN: a second word": A(b"85% x\n"),
    "KNOWN: two capacities on a line": A(b"85% 90%\n"),
    "KNOWN: a word on a second line, and a warning": A(b"85%\nx\n", b"warning: a device is missing\n"),
    "KNOWN: a dot after blanks": A(b"85% \t.\n"),
    "KNOWN: a second line that isn't UTF-8": A(b"7%\ncaf\xe9\n"),
    "KNOWN: a no-break space on a second line": A("85%\n\N{NO-BREAK SPACE}\n".encode()),
    "KNOWN: an em space on a second line, no newline": A("85%\n\N{EM SPACE}".encode()),
    "KNOWN: NEL on a second line": A("85%\n\N{NEXT LINE}".encode()),
    "KNOWN: U+001C on a second line": A(b"85%\n\x1c\n"),
    "KNOWN: a no-break space after a space": A("85% \N{NO-BREAK SPACE}\n".encode()),
    "a vertical tab on a second line: Perl's \\s": A(b"85%\n\v\n"),
}

_BASES = [b"85%", b"0%", b"100%", b"7%", b"085%", b"999%", b"1000%", b"85", b"%", b"x", b"8 5%", b"-", b"85%%",
          "٤٢%".encode(), b"caf\xe9", b""]
_BEFORE = [b"", b"", b"", b"", b" ", b"\n", b"\t", b"\r", b"  "]
_AFTER = [b"\n", b"\n", b"\n", b"", b" \n", b"  \n", b"\t\n", b"\r\n", b"\n\n", b"\n90%\n", b" x\n", b" 90%\n", b"\nx\n",
          b" \t.\n", b"\f\n", b"\xe9\n", "\n\N{NO-BREAK SPACE}\n".encode(), "\n\N{EM SPACE}".encode(), b"\n\x1c\n",
          b"\n\v\n", b" \n \n", b"\n \n", " \n".encode(), b" ", b"\n\n\n"]
_SAID = [b"", b"", b"", b"warning: a device is missing\n", b"cannot open 'tank': no such pool\n", b"warning: caf\xe9",
         b"\n\tindented\n", b"one\r\ntwo\n"]


def gen(r: random.Random) -> dict:
    return A(r.choice(_BEFORE) + r.choice(_BASES) + r.choice(_AFTER), r.choice(_SAID), r.choice([0, 0, 0, 1, 2]),
             9 if r.random() < 0.03 else 0)


def show(case: dict, a: dict, b: dict, diffs: list[str]) -> None:
    print(f"  zpool: stdout={bytes.fromhex(case['stdout'])!r} stderr={bytes.fromhex(case['stderr'])!r} "
          f"rc={case['rc']} kill={case['kill']}")
    for side, res in (("perl  ", a), ("python", b)):
        print(f"    {side}: value={res['value']} died={res['died']!r} said={res['said']!r}")
    print(f"    differs on {diffs}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=600)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()

    if args.mode == "hand":
        names, cases = list(HAND), list(HAND.values())
    else:
        r = random.Random(args.seed)
        cases = [gen(r) for _ in range(args.n)]
        names = [""] * len(cases)
    perl, python = run_perl(cases), run_python(cases)
    tally = {"match": 0, "known": 0, "DIFF": 0}
    shown: set[tuple[str, ...]] = set()
    for name, case, a, b in zip(names, cases, perl, python, strict=True):
        diffs, known = compare(case, a, b)
        if args.mode == "hand" and known != name.startswith("KNOWN: "):
            diffs.append("the class")
        tally["DIFF" if diffs else "known" if known else "match"] += 1
        if args.mode == "hand":
            print(f"[{'DIFF ' if diffs else 'KNOWN' if known else 'MATCH'}] {name}")
        if diffs and (args.mode == "hand" or tuple(diffs) not in shown):
            shown.add(tuple(diffs))
            show(case, a, b, diffs)
    print(tally, "known class:", MORE if tally["known"] else "none met")
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if tally["DIFF"] else 0)


if __name__ == "__main__":
    main()
