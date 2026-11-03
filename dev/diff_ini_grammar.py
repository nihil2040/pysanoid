#!/usr/bin/env python3
"""Differential test: the real Config::IniFiles, read the way sanoid ties it (`tie %ini, 'Config::IniFiles',
(-file => ...)`), vs pysanoid's _ini_lines and _read_ini -- on raw byte files, one Perl process for all of them.

Per file it compares the physical lines the module's own reader produces (_nextline via _read_next_line, the loop
ReadConfig runs) with _ini_lines; whether the file loads; and what the tie hands out: sections and keys in order,
each value undef, a scalar or an ARRAY ref. pysanoid reads the file as load_config does (UTF-8, surrogateescape).
The module must print no warning while loading.

One known class, harmless: a file whose only line ending is a final lone CR. Upstream peeks past the CR, finds
nothing, seeks back and reads the CR again as one more, empty line, warning "Use of uninitialized value $nextchar"
(the parse is the same). Anything else that differs fails.

Files come from three generators: well-formed files with exotic but legal content, so most load and their
contents are compared; nasty ones (mixed and odd line endings, BOMs, non-ASCII whitespace, here-doc markers and
terminators, malformed lines, stray bytes); and short strings over the grammar's own alphabet. Config::IniFiles
2.94 and 3.000003 read files the same way as the pinned 3.003000; --ini-lib checks another release's lib/.

Run: python3 dev/diff_ini_grammar.py hand
     python3 dev/diff_ini_grammar.py fuzz <n> <seed>     (n batches of 10 files)
"""
from __future__ import annotations

import argparse
import random
import shutil
import sys
from pathlib import Path
from typing import Any

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.perlrun import (
    PERL_JSON_HEAD,
    READ_SCENARIO_PERL,
    WRITE_RESULT_PERL,
    build_harness,
    cpan_env,
    run_harness,
)

import pysanoid as p

WORK = DEV_DIR / "work" / "ini_grammar"

HEAD = PERL_JSON_HEAD + r'''
BEGIN { unshift @INC, $ENV{INI_LIB}, $ENV{STRINGY_LIB}; }
use Config::IniFiles;
sub hx { return unpack('H*', $_[0]); }
'''

TAIL = READ_SCENARIO_PERL + r'''
my @files;
foreach my $path (@{ $__scn->{paths} }) {
    my @lines;
    {
        local $SIG{__WARN__} = sub {};
        my $reader = Config::IniFiles->new();
        open my $fh, '<', $path or die "cannot read $path: $!";
        delete $reader->{line_ends};
        $reader->_read_line_num(0);
        while (defined(my $line = $reader->_read_next_line($fh))) { push @lines, hx($line); }
        close $fh;
    }
    my (@warnings, %ini, @sections);
    my $loaded = do {
        local $SIG{__WARN__} = sub { (my $w = $_[0]) =~ s/ at \S+ line \d+.*\z//s; push @warnings, $w; };
        eval { tie %ini, 'Config::IniFiles', (-file => $path) } ? 1 : 0;
    };
    if ($loaded) {
        foreach my $section (keys %ini) {
            my @keys;
            foreach my $key (keys %{ $ini{$section} }) {
                my $v = $ini{$section}{$key};
                my @values = !defined($v) ? () : ref($v) eq 'ARRAY' ? @$v : ($v);
                push @keys, [hx($key), [map { hx($_) } @values], ref($v) eq 'ARRAY' ? 1 : 0];
            }
            push @sections, [hx($section), \@keys];
        }
    }
    push @files, {lines => \@lines, loaded => $loaded, sections => \@sections, warnings => \@warnings};
}
my %__result = (files => \@files);
''' + WRITE_RESULT_PERL
SCRIPT = build_harness(HEAD, "", TAIL)

LONE_CR_WARNING = "Use of uninitialized value $nextchar in string eq"
BOM = b"\xef\xbb\xbf"
BOM_TWICE = b"\xc3\xaf\xc2\xbb\xc2\xbf"  # the BOM encoded twice: what Config::IniFiles' BOM pattern strips

HAND: list[tuple[str, bytes]] = [
    ("a real BOM is a malformed line", BOM + b"[tank]\n    daily = 7\n"),
    ("a real BOM before a comment", BOM + b"# c\n[tank]\n    daily = 7\n"),
    ("a BOM encoded twice is stripped", BOM_TWICE + b"[tank]\n    daily = 7\n"),
    ("a BOM encoded twice before a comment", BOM_TWICE + b"# sanoid.conf\n[tank]\n    daily = 7\n"),
    ("a BOM encoded twice before a blank line", BOM_TWICE + b"\n[tank]\n    daily = 7\n"),
    ("a BOM encoded twice, CRLF", BOM_TWICE + b"[tank]\r\n    daily = 7\r\n"),
    ("a BOM encoded twice, no line ending", BOM_TWICE + b"[tank]"),
    ("a BOM encoded twice, twice", BOM_TWICE * 2 + b"[tank]\n    daily = 7\n"),
    ("a BOM encoded twice on line 2", b"[tank]\n" + BOM_TWICE + b"    daily = 7\n"),
    ("a UTF-16LE BOM", b"\xff\xfe[tank]\n    daily = 7\n"),
    ("KNOWN CLASS: one line ending in a lone CR", b"[tank]\r"),
    ("KNOWN CLASS: a lone CR only", b"\r"),
    ("CR-separated", b"[tank]\r    daily = 7\r"),
    ("CR-separated, LF inside a value", b"[tank]\r    daily = 7\n\r"),
    ("CR-separated, LF inside a line", b"[tank]\r    daily = 7\n    hourly = 3\r"),
    ("CRLF first, bare LF later", b"[tank]\r\n    daily = 7\n    hourly = 3\n"),
    ("LF first, CRLF later", b"[tank]\n    daily = 7\r\n    hourly = 3\r\n"),
    ("NAK-separated", b"[tank]\x15    daily = 7\x15"),
    ("VT and FF around a section", b"\x0b[\x0ctank\x0b]\x0c\n    daily = 7\n"),
    ("NBSP inside a section's brackets", b"[\xc2\xa0tank]\n    daily = 7\n"),
    ("a raw 0xA0 inside a section's brackets", b"[\xa0tank]\n    daily = 7\n"),
    ("NEL at the end of a value", b"[tank]\n    daily = 7\xc2\x85\n"),
    ("here-doc, empty marker", b"[tank]\n    path = <<\npool/a\n\n    daily = 7\n"),
    ("here-doc, marker with a trailing space", b"[tank]\n    path = <<EOT \npool/a\nEOT\nEOT \n"),
    ("here-doc, marker ending in CR", b"[tank]\n    path = <<EOT\r\r\npool/a\r\nEOT\r\r\n"),
    ("here-doc, unterminated", b"[tank]\n    path = <<EOT\npool/a\n"),
    ("here-doc, two lines", b"[tank]\n    path = <<EOT\npool/a\npool/b\nEOT\n"),
    ("an empty here-doc, then a value", b"[tank]\n    daily = <<E\nE\n    daily = 5\n"),
    ("a value, then an empty here-doc", b"[tank]\n    daily = 5\n    daily = <<E\nE\n"),
    ("a repeated section and key", b"[tank]\n    daily = 5\n[pool]\n    daily = 1\n[tank]\n    daily = 6\n"),
    ("a parameter before any section", b"daily = 5\n[tank]\n"),
    ("a malformed line, then a valid one", b"[tank]\n    junk\n    daily = 5\n"),
    ("a comment after a section", b"[tank] # c\n    daily = 5\n"),
    ("= inside a value", b"[tank]\n    pre_snapshot_script = /bin/x --a=b\n"),
    ("NUL in a section name", b"[ta\x00nk]\n    daily = 5\n"),
    ("a backslash continuation", b"[tank]\n    daily = 5 \\\n    6\n"),
    ("an empty file", b""),
    ("no line ending at all", b"[tank]"),
    ("only comments", b"# a\n; b\n"),
    ("an empty section", b"[tank]\n"),
]


def hx(text: str) -> str:
    return text.encode("utf-8", "surrogateescape").hex()


def run_perl(paths: list[Path], ini_lib: str | None) -> list[dict[str, Any]]:
    env = cpan_env()
    if ini_lib:
        env["INI_LIB"] = ini_lib
    result = run_harness(SCRIPT, stdin_obj={"paths": [str(path) for path in paths]}, env=env, timeout=900)
    if result.data is None:
        raise RuntimeError(f"perl harness produced no result:\n{result.stderr}")
    return result.data["files"]


def run_python(path: Path) -> dict[str, Any]:
    lines = [hx(line) for line in p._ini_lines(path.read_bytes().decode("utf-8", "surrogateescape"))]
    try:
        ini = p._read_ini(path, user_editable=True)
    except p.SanoidError:
        return {"lines": lines, "loaded": 0, "sections": [], "warnings": []}
    sections = [[hx(name), [[hx(key), [hx(v) for v in values], 1 if len(values) > 1 else 0] for key, values in params.items()]]
                for name, params in ini.items()]
    return {"lines": lines, "loaded": 1, "sections": sections, "warnings": []}


def lone_cr_at_eof(data: bytes) -> bool:
    """The file's first line ending is a CR, and it is the file's last byte."""
    return data.endswith(b"\r") and not any(c in data[:-1] for c in (b"\r", b"\n", b"\x15"))


def compare(data: bytes, perl: dict[str, Any], python: dict[str, Any]) -> tuple[str, list[str]]:
    known = lone_cr_at_eof(data)
    expected = {**python, "lines": [*python["lines"], ""], "warnings": [LONE_CR_WARNING]} if known else python
    diffs = [f"{k}: perl={perl[k]!r} python={expected[k]!r}" for k in ("lines", "loaded", "sections", "warnings")
             if perl[k] != expected[k]]
    return ("differ" if diffs else "known" if known else "match"), diffs


# ------------------------------------------------------------------ generators
EOLS = [b"\n", b"\r\n", b"\r", b"\x15"]
ODD_EOLS = [b"\n\r", b"\r\r\n", b"\x15\n", b"\r\x15", b"\n\n", b"\r\r"]
WS = [b" ", b"\t", b"\x0b", b"\x0c"]
FAKE_WS = [b"\xa0", b"\x85", b"\xc2\xa0", b"\xc2\x85", b"\xe2\x80\xa8", b"\xe3\x80\x80", b"\x1c", b"\x1f", b"\x00", BOM]
NAMES = [b"tank", b"tank/a", b"template_x", b"template_default", b"a b", b"a]b", b"[x]", b"x[", b"DEFAULT",
         b"t\xc3\xa9", b"t\xe9", b"t\x00x", b"t#x", b"t;x", b"t=x", b"#t", b";t", b"t\x0bx", b"=", b"<<x", b"t\rx", b"t\nx"]
KEYS = [b"daily", b"hourly", b"path", b"use_template", b"k", b"da ily", b"a#b", b"a;b", b"[a]", b"K", b"k\xa0",
        b"\xa0k", b"<<k", b"k<<", b"k\rk", b"k\nk", b"]", b"["]
VALUES = [b"", b"5", b"5 # c", b" 5 ", b"a=b", b"=", b"yes", b"no ", b"\xa0", b"x\x0b", b"#", b";", b"[a]",
          b"a\rb", b"a\nb", b"< <x", b"<", b"x<<y", b"\xc3\xa9", b"\xe9"]
MARKERS = [b"EOT", b"", b" EOT", b"EOT ", b"E\x0b", b"\"EOT\"", b"'E'", b"=", b"[s]", b"#", b"<<", b"E\r"]
BODY = [b"pool/a", b"", b" ", b"[s]", b"# c", b"k = v", b"EOT", b" EOT", b"EOT ", b"\x0b", b"x\ry", b"x\ny"]
PREFIXES = [BOM, BOM_TWICE, BOM * 2, BOM_TWICE * 2, b"\xff\xfe", b"\xfe\xff", BOM + BOM_TWICE, BOM_TWICE[:3],
            BOM_TWICE[:4]]
SAFE_NAMES = [b"tank", b"tank/a", b"template_x", b"template_default", b"a b", b"a]b", b"[x]", b"x[", b"t\xc3\xa9",
              b"t\xe9", b"t\x00x", b"t#x", b"t;x", b"t=x", b"=", b"<<x", b"t\x0bx", b"\xa0t", b"t\xc2\xa0", BOM + b"t"]
SAFE_KEYS = [b"daily", b"hourly", b"path", b"k", b"da ily", b"a#b", b"a;b", b"[a]", b"K", b"k\xa0", b"\xa0k", b"<<k",
             b"k<<", b"]", b"[", BOM + b"k", b"k\x00"]
SAFE_VALUES = [b"", b"5", b"5 # c", b" 5 ", b"a=b", b"=", b"yes", b"no ", b"\xa0", b"x\x0b", b"#", b";", b"[a]",
               b"< <x", b"<", b"x<<y", b"\xc3\xa9", b"\xe9", b"\x0c", b"<<", b" <<x"]
SAFE_MARKERS = [b"EOT", b"", b" EOT", b"EOT ", b"E\x0b", b"\"EOT\"", b"'E'", b"=", b"[s]", b"#", b"<<", b"\xa0"]
SAFE_BODY = [b"pool/a", b"", b" ", b"[s]", b"# c", b"k = v", b"EOT", b" EOT", b"EOT ", b"\x0b", b"\xe9", b"<<X"]
RAW_ALPHABET = [b"\n", b"\r", b"\x15", b" ", b"\t", b"\x0b", b"[", b"]", b"=", b"#", b";", b"<", b"<<", b"a", b"E",
                b"k = v", b"[s]", BOM_TWICE, BOM, b"\xc2", b"\xa0", b"\x00"]


def ws(r: random.Random, fake: float = 0.08) -> bytes:
    return b"".join(r.choice(FAKE_WS) if r.random() < fake else r.choice(WS) for _ in range(r.choice([0, 0, 0, 1, 1, 2, 4])))


def gen_valid(r: random.Random) -> bytes:
    """A file that loads unless a generated name, key or here-doc happens to break it."""
    eol = r.choice(EOLS)
    out = BOM_TWICE if r.random() < 0.05 else b""
    out += ws(r, 0) + b"[" + ws(r, 0) + r.choice(SAFE_NAMES) + ws(r, 0) + b"]" + ws(r, 0) + eol
    for _ in range(r.randint(0, 14)):
        kind = r.choices(["blank", "comment", "section", "param", "heredoc"], [1, 1.5, 2, 6, 1.5])[0]
        if kind == "blank":
            out += ws(r, 0) + eol
        elif kind == "comment":
            out += ws(r, 0) + r.choice([b"#", b";"]) + r.choice([b"", b" daily = 5", b"[x]", b"<<EOT"]) + eol
        elif kind == "section":
            out += ws(r, 0) + b"[" + ws(r, 0) + r.choice(SAFE_NAMES) + ws(r, 0) + b"]" + ws(r, 0) + eol
        elif kind == "param":
            out += ws(r, 0) + r.choice(SAFE_KEYS) + ws(r, 0) + b"=" + ws(r) + r.choice(SAFE_VALUES) + ws(r) + eol
        else:
            marker = r.choice(SAFE_MARKERS)
            out += ws(r, 0) + r.choice(SAFE_KEYS) + ws(r, 0) + b"=" + ws(r, 0) + b"<<" + marker + eol
            out += b"".join(r.choice(SAFE_BODY) + eol for _ in range(r.choice([0, 0, 1, 1, 2, 3])))
            out += marker + eol
    return out[: -len(eol)] if r.random() < 0.1 else out


def gen_nasty(r: random.Random) -> bytes:
    tame = r.random() < 0.5  # no malformed lines or byte mutations
    eol, mixed = r.choice(EOLS), r.random() < 0.25

    def end() -> bytes:
        return r.choice(EOLS + ODD_EOLS) if mixed and r.random() < 0.3 else eol

    out = r.choice(PREFIXES) if r.random() < 0.2 else b""
    have_section = False
    for _ in range(r.randint(0, 12)):
        kind = r.choices(["blank", "comment", "fakecomment", "section", "param", "heredoc", "malformed"],
                         [1, 2, 0 if tame else 0.5, 3, 5, 1.2, 0 if tame else 0.6])[0]
        if kind == "param" and not have_section and r.random() < 0.8:
            kind = "section"
        if kind == "blank":
            out += ws(r) + end()
        elif kind == "comment":
            out += ws(r) + r.choice([b"#", b";"]) + r.choice([b"", b" note", b" daily = 5", b"[x]", b"<<EOT"]) + end()
        elif kind == "fakecomment":
            out += ws(r) + r.choice([b"//", b"!", b"%", BOM + b"#"]) + b" x" + end()
        elif kind == "section":
            have_section = True
            tail = r.choice([b"", b" # c", b"x", b"]"]) if r.random() < 0.15 and not tame else b""
            out += ws(r) + b"[" + ws(r) + r.choice(NAMES) + ws(r) + b"]" + ws(r) + tail + end()
        elif kind == "param":
            out += ws(r) + r.choice(KEYS) + ws(r) + b"=" + ws(r) + r.choice(VALUES) + ws(r) + end()
        elif kind == "heredoc":
            marker = r.choice(MARKERS)
            out += ws(r) + r.choice(KEYS) + ws(r) + b"=" + ws(r) + b"<<" + marker + end()
            out += b"".join(r.choice(BODY) + end() for _ in range(r.choice([0, 0, 1, 1, 2, 3])))
            term = r.choices([marker, marker + b" ", b" " + marker, marker + b"\r", None], [8, 1, 1, 1, 1])[0]
            out += b"" if term is None else term + end()
        else:
            out += ws(r) + r.choice([b"junk", b"daily", b"[unclosed", b"]", b"k: v", b"= v", b" = ", b"<<"]) + end()
    if r.random() < 0.15:
        out = out.rstrip(b"\r\n\x15")  # no final line ending
    for _ in range(0 if tame else r.choice([0, 0, 0, 1, 2, 3])):
        pos, op = r.randint(0, len(out)), r.random()
        special = r.choice([*EOLS, *WS, *FAKE_WS, b"=", b"[", b"]", b"#", b";", b"<", b"<<", bytes([r.randrange(256)])])
        if op < 0.5:
            out = out[:pos] + special + out[pos:]
        elif op < 0.75:
            out = out[:pos] + out[pos + 1:]
        else:
            out = out[:pos] + special + out[pos + 1:]
    return out


def gen_raw(r: random.Random) -> bytes:
    """Every token the grammar's regexes care about, in any order."""
    return b"".join(r.choice(RAW_ALPHABET) for _ in range(r.randint(0, 30)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["hand", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=2000)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    ap.add_argument("--ini-lib", help="another Config::IniFiles release's lib/ (default: the pinned one)")
    args = ap.parse_args()

    if args.mode == "hand":
        cases = HAND
    else:
        r = random.Random(args.seed)
        gens = [gen_valid, gen_nasty, gen_raw]
        cases = [(f"fuzz #{i}", r.choices(gens, [4, 4, 2])[0](r)) for i in range(args.n * 10)]
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True)
    paths = [WORK / f"{i:06d}.ini" for i in range(len(cases))]
    for path, (_, data) in zip(paths, cases, strict=True):
        path.write_bytes(data)

    tally: dict[str, int] = {}
    failed, shown = False, 0
    for (name, data), path, perl in zip(cases, paths, run_perl(paths, args.ini_lib), strict=True):
        kind, diffs = compare(data, perl, run_python(path))
        status = "loads" if perl["loaded"] else "refused"
        if args.mode == "hand":
            ok = kind == ("known" if name.startswith("KNOWN CLASS") else "match")
            label = f"{kind}, {status}" if ok else f"UNEXPECTED {kind}"
            print(f"[{label.upper():22}] {name}")
        else:
            ok = kind != "differ"
            label = f"{kind}, {status}"
        tally[label] = tally.get(label, 0) + 1
        failed = failed or not ok
        if not ok and shown < 10:
            shown += 1
            print(f"--- {name}: {data!r}", *diffs, sep="\n      ")
    print(tally)
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
