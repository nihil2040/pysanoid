#!/usr/bin/env python3
"""Differential test: Perl's own `0 + $string` vs pysanoid._perl_number, the reading take falls back to for a
retention count or preferred-time setting the strict parsers refuse (`daily = 3o` is 3, `hourly_min = 1_5` is 1).

There is no upstream sub to extract here: the reference is the Perl interpreter itself (grok_number, my_atof3 and
C's strtod). Strings are built as raw bytes, as sanoid.conf can hold any, and handed to Perl hex-encoded; pysanoid
decodes them the way load_config reads the file (UTF-8, surrogateescape). Both sides print "%.17g"; the sign of a
zero and of a NaN is ignored, since neither can change take's `> 0` gate or a whole-number setting.

Perl runs under LC_ALL=C, as sanoid does from cron or systemd: under a UTF-8 locale, macOS's strtod (not glibc's)
also skips a 0xA0 byte as whitespace. Not generated: a NaN payload other than digits (`nan(0x1f)`, `snan(abc)`),
which _grok_infnan reads as no spelling where Perl accepts some.

Run: python3 dev/diff_perl_number.py hand
     python3 dev/diff_perl_number.py fuzz <n> <seed>     (n batches of 50 strings, one Perl process for all)
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.perlrun import (
    PERL_JSON_HEAD,
    READ_SCENARIO_PERL,
    WRITE_RESULT_PERL,
    build_harness,
    run_harness,
)

import pysanoid as p

TAIL = f'''
{READ_SCENARIO_PERL}
my @out;
{{
    no warnings;
    push @out, sprintf("%.17g", 0 + pack("H*", $_)) for @{{ $__scn->{{hex}} }};
}}
my %__result = (numbers => \\@out);
{WRITE_RESULT_PERL}
'''
SCRIPT = build_harness(PERL_JSON_HEAD, "", TAIL)

HAND = [
    "3o", "30 days", "7d", "1_5", "1_000", "7.5", "+2", "-3", "007", " 12", "\t\n\r\f\v12", "12 ", "0 but true",
    ".5", "5.", ".", "-.5", "+.5", "1e3", "1E3", "1e", "1e+", "1e+2x", "1.5e-1", ".e3", "0e5", "5e5e5", "1..5",
    "1.2.3", "1e999", "-1e999", "1e-999", "0.0", "-0", "", "abc", "#5", "; 5", "  ",
    "0x1A", "0X1A", "0b11", "0x", "00x1", "-0x1A", "+0x1A", "- 0x1A", "+ 0x1p3", "--0x10", "+-0x10", "- 0x",
    "- 0x.8", "- 0x1.8p1", "- 0xg", "- 0x1p99999", "- 0b1",
    "+-5", "--5", "-+5", "++5", "+ 5", "- 5", "-\t5", "+\n5", "- -5", "-  5", " - 5", "- .5", "-- 5", "+ -5",
    "+-.5", "- 5x", "- .5e1", "+ 1e5",
    "inf", "Inf", "INFINITY", "infinity", "infx", "information", "info", "in", "i", "-inf", "+inf", "--inf",
    "-+inf", "++inf", "- inf", "+ inf", "- infinity", "1.#INF", "1.#INF00", "-1.#INF", "1.#IND", "1.#QNAN",
    "1.#SNAN", "1.#NAN", "1.#X", "1.#", "1.#I", "1.#IN", "ind", "--1.#INF", "++1.#INF",
    "nan", "NaN", "nanq", "nans", "qnan", "snan", "nan(123)", "nanx", "nana", "na", "sn", "qna", "+nan", "-nan",
    "- nan", "- nan(1)", "+ nan",
    # grok_infnan returning without moving its pointer, which only my_atof3 (after a second sign) notices
    "++snan", "++qnan", "+-snan", "++1.#QNAN", "+-1.#SNAN", "++1.#NAN", "--1.#IND", "-+1.#QNAN", "++snanx",
    "++1.#QNANx", "++nanq", "++nan(", "++snan(12", "++infin", "1.#INFI", "1.#INFINITY", "1#INF", "++1#INF",
    "1.#INF00x", "1.#IND0", "nan(12)x", "nan(", "nan(12", "snan(7)", "nan(1.5)", "snan(1.5)", "1.#QNAN(1.5)",
    "\xa05", "\x855", "5\x006", "\x005", "²", "٣", "１２", " 5", "é5", "5é",
]


def _bytes(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _norm(text: str) -> str:
    text = text.lower()
    return text.lstrip("-") if text.lstrip("-") in ("0", "nan") else text


def run_perl(raws: list[bytes]) -> list[str]:
    result = run_harness(SCRIPT, stdin_obj={"hex": [r.hex() for r in raws]}, env={"LC_ALL": "C"}, timeout=60)
    if result.data is None:
        raise RuntimeError(f"perl harness produced no result:\n{result.stderr}")
    return [_norm(x) for x in result.data["numbers"]]


def run_python(raws: list[bytes]) -> list[str]:
    return [_norm(f"{p._perl_number(r.decode('utf-8', 'surrogateescape')):.17g}") for r in raws]


def compare(raws: list[bytes]) -> list[str]:
    return [f"{r!r}: perl={a} python={b}" for r, a, b in zip(raws, run_perl(raws), run_python(raws), strict=True) if a != b]


WS = [b" ", b"\t", b"\n", b"\r", b"\f", b"\v"]
ODD = [b"\xa0", b"\x85", b"\x00", "é".encode(), "٣".encode(), " ".encode(), b"\xff"]
WORDS = [b"inf", b"INF", b"Infinity", b"infin", b"info", b"in", b"nan", b"NaN", b"nanq", b"nans", b"qnan", b"snan",
         b"nan(12)", b"snan(7)", b"nan(", b"nan(12", b"na", b"1.#INF", b"1#INF", b"1.#INF00", b"1.#INFI", b"1.#IND",
         b"1.#IND0", b"1.#QNAN", b"1.#SNAN", b"1#NAN", b"1.#", b"ind"]
GARBAGE = [b"x", b"o", b"_", b"#", b";", b" ", b"e", b"E", b".", b"p", b"-", b"+", b"days", b"min", b"th", b"d",
           b"a", b"f", b"0", b"i", b"q", b"s"]


def _digits(r: random.Random) -> bytes:
    return "".join(r.choice("0123456789") for _ in range(r.choice([1, 1, 2, 3, 5, 18, 25]))).encode()


def _core(r: random.Random) -> bytes:
    kind = r.random()
    if kind < 0.45:  # a decimal, as strtod reads it
        out = _digits(r) if r.random() < 0.85 else b""
        if r.random() < 0.35:
            out += b"." + (_digits(r) if r.random() < 0.7 else b"")
        if r.random() < 0.25:
            out += r.choice([b"e", b"E"]) + r.choice([b"", b"+", b"-"]) + (_digits(r) if r.random() < 0.8 else b"")
        return out
    if kind < 0.6:  # hex, and the prefixes Perl guards
        out = r.choice([b"0x", b"0X", b"0b", b"0B"]) + "".join(r.choice("0123456789abcdefABCDEFg.") for _ in range(r.randint(0, 5))).encode()
        if r.random() < 0.3:
            out += r.choice([b"p", b"P"]) + r.choice([b"", b"+", b"-"]) + _digits(r)
        return out
    if kind < 0.85:
        return r.choice(WORDS)
    return b"".join(r.choice(GARBAGE + ODD) for _ in range(r.randint(0, 3)))


def gen(r: random.Random) -> bytes:
    out = b"".join(r.choice(WS) for _ in range(r.choice([0, 0, 0, 1, 2])))
    for _ in range(r.choice([0, 0, 0, 1, 1, 2])):
        out += r.choice([b"+", b"-"]) + (r.choice(WS) if r.random() < 0.25 else b"")
    out += _core(r)
    for _ in range(r.choice([0, 0, 1, 2, 3])):
        out += r.choice(GARBAGE + WS + ODD + WORDS) if r.random() < 0.8 else _core(r)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=200)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()

    if args.mode == "hand":
        raws = [_bytes(t) for t in HAND]
    else:
        r = random.Random(args.seed)
        raws = [gen(r) for _ in range(args.n * 50)]
    diffs = compare(raws)
    for d in diffs[:25]:
        print("DIFF", d)
    print(f"{len(raws) - len(diffs)}/{len(raws)} match")
    sys.exit(1 if diffs else 0)


if __name__ == "__main__":
    main()
