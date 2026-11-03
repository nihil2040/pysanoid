#!/usr/bin/env python3
"""Differential test: upstream's `init` (Module 2 -- the config/template/recursion merge),
extracted verbatim, vs pysanoid.load_config -- on real config files, real sanoid.defaults.conf,
and the real Config::IniFiles (dev/.cpan_cache; run dev/fetch_cpan.py first).

The real module, not a stand-in: a mock that trimmed every value hid the one place upstream's
merge depends on file order. Upstream trims a section's values only when its section loop
reaches that section, so a template or [template_default] placed below the sections using it
reaches them untrimmed, and a repeated key of [template_default]'s as an ARRAY ref. pysanoid
reads every value trimmed, and a repeated key as its first value (DEV-001 in DEVIATIONS.md), so
those two differences are classified as known: "untrimmed" and "arrayref".
A third known class is DEV-005, "order": a section for a dataset under a
recursive or skip_children section, placed above it, which pysanoid refuses and upstream loads, its
recursion overwriting that section or skip_children deleting it. It is checked against the rule as
dev/lib/configorder.py states it apart from load_config, word for word, and with the trace pysanoid
printed before refusing equal to upstream's up to there. A fourth is DEV-007,
"shared": two entries of the merged config that take or prune one dataset, which pysanoid refuses once its
merge is done and upstream loads. pysanoid is loaded twice, with that last check and without it: the merge
is compared with upstream's as for any file, and the refusal with the rule as dev/lib/configorder.py states
it, word for word. Anything else that differs fails.

--debug is on, and what init prints is compared too, stdout and stderr joined in the order written: its
DEBUG lines tracing the merge and its warnings, in order. Only a run of "recursively setting" lines for
one child is sorted first, since upstream lists them in Perl's per-process random hash order. Perl's own
warnings about upstream's code, and the child-lookup DEBUG line (the stub here doesn't print it;
diff_dataset_cache.py compares it), are left out. When the configs match, Data::Dumper's text of the
real %config -- with the integers init really stored -- is compared with pysanoid's _perl_dumper.

The ini grammar itself is a separate layer (_ini_lines, _parse_ini_text), fuzzed on raw byte files by
diff_ini_grammar.py.

Run: python3 dev/diff_config_merge.py cases
     python3 dev/diff_config_merge.py fuzz <n> <seed>
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.configorder import first_misplaced, first_shared, refusal, shared_refusal
from lib.extract import extract_sub
from lib.perlrun import PERL_JSON_HEAD, build_harness, cpan_env, run_harness

import pysanoid as p

WORK = DEV_DIR / "work" / "config_merge"
DEFAULTS = DEV_DIR.parent / "sanoid.defaults.conf"

HEAD = PERL_JSON_HEAD + r'''
BEGIN { unshift @INC, $ENV{INI_LIB}, $ENV{STRINGY_LIB}; }
use Config::IniFiles;
use Data::Dumper;
$Data::Dumper::Sortkeys = 1;

my $MINIMUM_DEFAULTS_VERSION = 2;
my %args = ('debug' => 1, 'verbose' => 0, 'force-update' => 1);
my $cacheTTL = 1200;
my $zfs = 'zfs';
my ($confdir, $fakelist) = @ARGV;
my $cachedatasetspath = "$confdir/datasets.cache";
my @cachedatasets;
open my $lf, '<', $fakelist or die "cannot read $fakelist";
chomp(my @FAKE = <$lf>);
close $lf;
sub checklock  { 1 }
sub writelock  { 1 }
sub removelock { 1 }
sub getchilddatasets {
    my $fs = shift;
    my @out = grep { $_ eq $fs || index($_, "$fs/") == 0 } @FAKE;
    shift @out;
    return @out;
}
'''

TAIL = '''
open(STDERR, '>&', \\*STDOUT) or die;
$| = 1;
my %config = eval { init("$confdir/sanoid.conf", "$confdir/sanoid.defaults.conf") };
my %__result;
if ($@) {
    (my $msg = $@) =~ s/\\s+$//;
    %__result = ('died' => $msg);
} else {
    sub norm { my $v = shift; return '<undef>' unless defined $v; return ref($v) eq 'ARRAY' ? 'REF:ARRAY' : "$v" }
    my %out;
    foreach my $sec (keys %config) { foreach my $k (keys %{ $config{$sec} }) { $out{$sec}{$k} = norm($config{$sec}{$k}) } }
    %__result = ('died' => '', 'config' => \\%out, 'dumper' => Dumper(\\%config));
}
open my $__of, '>', $ENV{DIFF_OUT} or die;
print $__of encode_json(\\%__result);
close $__of;
'''

SCRIPT = build_harness(HEAD, extract_sub("init"), TAIL)
FAKE_TREE = ["tank", "tank/a", "tank/a/x", "tank/a/y", "tank/b", "tank/b/x", "pool2",
            "pool2/data", "pool2/data/one", "pool2/data/two"]
# init's three warnings, in upstream's words, which pysanoid prints as they are
WARN_RE = re.compile(r"^(?:duplicate key|ignored key|ignored autosnap)")
RECURSE_RE = re.compile(r"^DEBUG: recursively setting .* from (.*) to (.*)\.$")


def _stream(output: str) -> list[str]:
    """init's DEBUG lines and warnings, in order, each run of "recursively setting" lines for one child sorted."""
    lines = [line for line in output.splitlines()
             if WARN_RE.match(line) or (line.startswith("DEBUG: ")
                                        and not line.startswith("DEBUG: getting list of child datasets"))]
    result: list[str] = []
    run: list[str] = []
    for line in [*lines, ""]:
        m = RECURSE_RE.match(line)
        if run and (m is None or RECURSE_RE.match(run[0]).groups() != m.groups()):
            result += sorted(run)
            run = []
        if m:
            run.append(line)
        elif line:
            result.append(line)
    return result


def _confdir(n: int, text: str | bytes) -> Path:
    d = WORK / f"c{n}"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    shutil.copyfile(DEFAULTS, d / "sanoid.defaults.conf")
    (d / "sanoid.conf").write_bytes(text.encode() if isinstance(text, str) else text)
    return d


def _as_read(text: str) -> str:
    """A Perl byte string as encode_json gives it (a character per byte), as pysanoid reads the same bytes."""
    return text.encode("latin-1").decode("utf-8", "surrogateescape")


def run_perl(n: int, text: str | bytes) -> dict:
    d = _confdir(n, text)
    fakelist = WORK / "fake_datasets.txt"
    fakelist.write_text("\n".join(FAKE_TREE) + "\n")
    result = run_harness(SCRIPT, argv=[str(d), str(fakelist)], env=cpan_env())
    if result.data is None:
        raise RuntimeError(f"perl harness produced no result:\n{result.stderr}")
    data = result.data
    if "config" in data:
        data["config"] = {_as_read(sec): {_as_read(k): _as_read(v) for k, v in c.items()} for sec, c in data["config"].items()}
        data["dumper"] = _as_read(data["dumper"])
    return {**data, "stream": _stream(result.stdout)}


def _load_python(d: Path, *, refuse_shared: bool) -> dict:
    def fake_run(cmd, **kw):
        fs = cmd[-1]
        out = [x for x in FAKE_TREE if x == fs or x.startswith(fs + "/")]
        return subprocess.CompletedProcess(cmd, 0, stdout="".join(x + "\n" for x in out).encode(), stderr=b"")

    real_run, real_check = subprocess.run, getattr(p, "_refuse_shared_datasets", None)  # None before 0.1.38
    subprocess.run = fake_run
    if real_check is not None and not refuse_shared:
        p._refuse_shared_datasets = lambda *_: None
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            cfg = p.load_config(d, p.ZfsClient(WORK / "cache", 1200, True, debug=True))
    except Exception as e:  # noqa: BLE001 - harness: report anything
        return {"died": f"{type(e).__name__}: {e}", "stream": _stream(out.getvalue()), "merged": None}
    finally:
        subprocess.run = real_run
        if real_check is not None:
            p._refuse_shared_datasets = real_check
    return {"died": "", "config": {sec: {k: _norm(v) for k, v in c.items()} for sec, c in cfg.items()},
            "stream": _stream(out.getvalue()), "dumper": p._perl_dumper(cfg), "merged": cfg}


def run_python(n: int, text: str | bytes) -> dict:
    """The merge without the shared-dataset refusal, to compare with upstream's, and what load_config itself does."""
    d = _confdir(n, text)
    return {**_load_python(d, refuse_shared=False), "died": _load_python(d, refuse_shared=True)["died"]}


def _norm(v):
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


TT = ["1", "true", "True", "TRUE", "yes", "Yes", "YES", "on", "On", "ON"]
TF = ["0", "false", "False", "FALSE", "no", "No", "NO", "off", "Off", "OFF"]


def _known_class(key: str, perl: str, python: str, perl_section: dict) -> str | None:
    """"arrayref": upstream kept the ARRAY ref of a repeated key. "untrimmed": upstream's value is pysanoid's with
    the whitespace upstream didn't trim (a toggle compared normalized) -- including the zfs-recursion child whose
    untrimmed `autosnap = yes ` escapes upstream's `== 1` guard, so pysanoid alone switches it off."""
    if perl == "REF:ARRAY":
        return "arrayref"
    if perl == "<missing>" or perl == perl.strip(p._WS):
        return None
    trimmed = perl.strip(p._WS)
    if key in p.TOGGLE_KEYS:
        trimmed = "1" if trimmed in TT else "0" if trimmed in TF else trimmed
    if trimmed == python or (key == "autosnap" and trimmed == "1" and python == "0"
                             and perl_section.get("zfs_recursion") == "1"):
        return "untrimmed"
    return None


def compare(pl: dict, py: dict) -> tuple[str, list[str]]:
    if pl["died"] or py["died"]:
        kind = "both_died" if (pl["died"] and py["died"]) else ("perl_died" if pl["died"] else "python_died")
        return kind, [f"perl: {pl['died']}", f"python: {py['died']}"]
    diffs: list[str] = []
    known: list[str] = []
    switched_off: set[str] = set()  # sections where only pysanoid applied the zfs-recursion autosnap guard
    for sec in sorted(set(pl["config"]) | set(py["config"])):
        if sec not in pl["config"]:
            diffs.append(f"[{sec}] exists only in Python")
        elif sec not in py["config"]:
            diffs.append(f"[{sec}] exists only in Perl")
        else:
            for k in sorted(set(pl["config"][sec]) | set(py["config"][sec])):
                a, b = pl["config"][sec].get(k, "<missing>"), py["config"][sec].get(k, "<missing>")
                if a == b:
                    continue
                cls = _known_class(k, a, b, pl["config"][sec])
                (known if cls else diffs).append(f"[{sec}] {k}: perl={a!r} python={b!r}" + (f"  ({cls})" if cls else ""))
                if cls and k == "autosnap" and b == "0":
                    switched_off.add(sec)
    # the zfs-recursion guard's warning, which pysanoid alone gives where upstream's untrimmed value escaped the guard
    guard = [line for line in py["stream"]
             if (m := re.match(r"ignored autosnap configuration for '(.*)' because", line)) and m.group(1) in switched_off]
    known += [f"output only in Python: {line}  (untrimmed)" for line in guard]
    py_stream = [line for line in py["stream"] if line not in guard]
    if pl["stream"] != py_stream:
        at = next(i for i, (a, b) in enumerate(zip([*pl["stream"], None], [*py_stream, None])) if a != b)
        diffs.append(f"output differs at line {at + 1}: perl={(pl['stream'][at:] or [None])[0]!r} "
                     f"python={(py_stream[at:] or [None])[0]!r}")
    if not diffs and not known and pl["dumper"] != py["dumper"]:
        diffs.append("Data::Dumper text differs")
    return ("differ" if diffs else "known" if known else "match"), diffs + known


GUARD_RE = re.compile(r"ignored autosnap configuration for ")  # the "untrimmed" class can add or drop this warning


def _order_class(n: int, pl: dict, py: dict) -> tuple[str, list[str]] | None:
    """The "order" class when the rule applies and pysanoid refused in its words, having traced what upstream traced up
    to there; a failure when only one of the two holds; None when neither does."""
    d = WORK / f"c{n}"
    misplaced = first_misplaced(d / "sanoid.conf")
    refused = " must come after [" in py["died"]
    if misplaced is None and not refused:
        return None
    if misplaced is None or py["died"] != f"SanoidError: {refusal(f'{d}/sanoid.conf', *misplaced)}":
        return "differ", [f"order rule: expected {misplaced}, python: {py['died'] or 'loaded'}"]
    if pl["died"]:
        return "differ", [f"order rule: perl died: {pl['died']}"]
    py_stream = [line for line in py["stream"] if not GUARD_RE.match(line)]
    pl_stream = [line for line in pl["stream"] if not GUARD_RE.match(line)]
    if py_stream != pl_stream[:len(py_stream)]:
        return "differ", ["order rule: the trace before the refusal differs from upstream's"]
    child, parent, dropped = misplaced
    return "known", [(f"[{child}] above [{parent}]: pysanoid refuses the file, upstream "
                      f"{'drops' if dropped else 'overwrites'} it  (order)")]


def _shared_class(n: int, pl: dict, py: dict) -> tuple[str, list[str]] | None:
    """The "shared" class when the rule applies to pysanoid's merge and pysanoid refused in its words, that merge
    compared with upstream's as for any file; a failure when only one of the two holds; None when neither does."""
    d = WORK / f"c{n}"
    shared = first_shared(d / "sanoid.conf", py["merged"]) if py["merged"] is not None else None
    refused = " take or prune " in py["died"]
    if shared is None and not refused:
        return None
    if shared is None or py["died"] != f"SanoidError: {shared_refusal(f'{d}/sanoid.conf', *shared)}":
        return "differ", [f"shared rule: expected {shared}, python: {py['died'] or 'loaded'}"]
    kind, notes = compare(pl, {**py, "died": ""})
    if kind not in ("match", "known"):
        return kind, notes
    path, who, _ = shared
    return "known", [f"{' and '.join(who)} take or prune {path}: pysanoid refuses the file, upstream loads it  (shared)",
                     *notes]


_n = [0]

# what str.strip() removes and Perl's \s on bytes doesn't match: upstream keeps these in a value
PADS = ["\xa0", "\x85", "\u2028", "\u3000", "\x1c", "\x1f"]
PAD_RE = re.compile(f"[{''.join(PADS)}]")
TOGGLE_REFUSAL_RE = re.compile(r"SanoidError: '(\w+)' in \[(.*)\] must be one of ")


def _names_unpadded(text: str) -> str:
    """The file with the pads taken out of its use_template lines only: the templates pysanoid's reading names."""
    return "\n".join(PAD_RE.sub("", line) if line.lstrip(p._WS).startswith("use_template") else line
                     for line in text.split("\n"))


def _pad_class(text: str) -> tuple[str, list[str]]:
    """A file with a pad (PADS) in it. "toggle pad" (DEV-002): pysanoid refuses a toggle whose value has one, where
    upstream reads it as true; the key refused must be one the file pads, and the file with every pad taken out must
    give what it gives without. "template pad" (DEV-003): a name in use_template keeps a pad upstream looks up and
    pysanoid ignores, so pysanoid's merge must be upstream's of the file with those names unpadded. Any other pad must
    read alike. A file refused for its `recursive` or `skip_children` is the membership class's, pad or not."""
    _n[0] += 1
    n = _n[0]
    unpadded = _names_unpadded(text)
    pl = run_perl(n, unpadded)
    py = run_python(n, text)  # last, so the order and shared rules read the file pysanoid read
    membership = _membership_class(text, py)
    if membership is not None:
        return membership
    refused = TOGGLE_REFUSAL_RE.match(py["died"])
    if refused is not None:
        key, section = refused.groups()
        padded = re.search(rf"^[{p._WS}]*{key}[{p._WS}]*=.*{PAD_RE.pattern}", text, re.MULTILINE)
        if key not in p.TOGGLE_KEYS or key in ("recursive", "skip_children") or padded is None:
            return "differ", [f"toggle pad rule: no line pads '{key}', python: {py['died']}"]
        kind, notes = run_both(PAD_RE.sub("", text))
        if kind not in ("match", "known", "both_died"):
            return kind, ["toggle pad rule: the file without its pads:", *notes]
        return "known", [f"[{section}] {key} holds a pad: pysanoid refuses the file, upstream reads it as true  (toggle pad)"]
    kind, notes = _order_class(n, pl, py) or _shared_class(n, pl, py) or compare(pl, py)
    if unpadded != text and kind == "match":
        return "known", ["a use_template name holds a pad: pysanoid applies the template, upstream none  (template pad)"]
    return kind, notes


MEMBERSHIP_RE = re.compile(r"SanoidError: '(recursive|skip_children)' in \[(.*)\] must be one of .*, got (.*)\.$")


def _membership_class(text: str, py: dict) -> tuple[str, list[str]] | None:
    """DEV-004, "membership": pysanoid refuses a `recursive` or `skip_children` that is no true or false word (and,
    for `recursive`, holds no "zfs"), where upstream reads it as not recursive, or compiles it as a regex: `.` recurses
    and `(` ends its run. The value refused must be the section's own, as the file has it, and be no such word."""
    m = MEMBERSHIP_RE.match(py["died"])
    if m is None:
        return None
    key, section, value = m.group(1), m.group(2), ast.literal_eval(m.group(3))
    own = p._section_dict(p._parse_ini_text(text)[0], section).get(key)
    if own != value or value in TT + TF or (key == "recursive" and "zfs" in value.lower()):
        return "differ", [f"membership rule: [{section}] {key} = {own!r}, python: {py['died']}"]
    return "known", [f"[{section}] {key} = {value!r}: pysanoid refuses the file, upstream reads it as a pattern  (membership)"]


def run_both(text: str | bytes) -> tuple[str, list[str]]:
    raw = text if isinstance(text, str) else text.decode("utf-8", "surrogateescape")
    if PAD_RE.search(raw):
        return _pad_class(raw)
    _n[0] += 1
    pl, py = run_perl(_n[0], raw), run_python(_n[0], raw)
    return (_membership_class(raw, py) or _order_class(_n[0], pl, py) or _shared_class(_n[0], pl, py)
            or compare(pl, py))


CASES = {
    # known class (DEV-005): see _order_class
    "KNOWN CLASS: a child listed BEFORE its recursive parent, with process_children_only": """
[tank/a]
    process_children_only = yes
    daily = 3
[tank]
    recursive = yes
    daily = 9
""",
    "KNOWN CLASS: a zfs-recursive child listed BEFORE its plain-recursive parent": """
[tank/a]
    recursive = zfs
    daily = 3
[tank]
    recursive = yes
    daily = 9
""",
    "KNOWN CLASS: an explicit child listed BEFORE its skip_children parent": """
[tank/a]
    daily = 3
[tank]
    skip_children = yes
    daily = 9
""",
    "KNOWN CLASS: a section under a recursive section's path =, listed before it": """
[pool2/data]
    daily = 2
[alias]
    path = pool2
    recursive = yes
""",
    "a child listed AFTER its recursive parent, with process_children_only": """
[tank]
    recursive = yes
    daily = 9
[tank/a]
    process_children_only = yes
    daily = 3
""",
    "a zfs-recursive child listed AFTER its plain-recursive parent": """
[tank]
    recursive = yes
    daily = 9
[tank/a]
    recursive = zfs
    daily = 3
""",
    "an explicit child listed AFTER its skip_children parent": """
[tank]
    skip_children = yes
    daily = 9
[tank/a]
    daily = 3
""",
    "a section whose name only starts like a recursive section's path, listed before it": """
[tank/ab]
    daily = 2
[tank/a]
    recursive = yes
""",
    "two templates, later one overrides; whitespace around comma": """
[template_t1]
    daily = 1
    weekly = 2
[template_t2]
    daily = 7
[tank]
    use_template = t1 , t2
""",
    "duplicate keys in a dataset section (first wins)": """
[tank]
    daily = 4
    daily = 8
""",
    "blank values everywhere": """
[template_t1]
    daily =
[tank]
    use_template = t1
    prune_defer =
    path =
""",
    "KNOWN CLASS: nested plain recursion, inner listed first": """
[pool2/data]
    recursive = yes
    daily = 2
[pool2]
    recursive = yes
    daily = 9
""",
    "nested plain recursion, inner listed after": """
[pool2]
    recursive = yes
    daily = 9
[pool2/data]
    recursive = yes
    daily = 2
""",
    "skip_children set in user [template_default]": """
[template_default]
    skip_children = yes
[tank]
    recursive = yes
""",
    # DEV-003: upstream splits use_template on ' *, *' only, so 'a,\tb' looks up a template literally
    # named "\tb", finds none, and applies nothing for it.
    "KNOWN DIVERGENCE: tab after comma in use_template": "[template_a]\n    daily = 1\n[template_b]\n    weekly = 2\n[tank]\n    use_template = a,\tb\n",
    # known class (DEV-007): see _shared_class
    "KNOWN CLASS: path points at another configured section": """
[tank/b]
    daily = 2
[alias]
    path = tank/b
    daily = 6
    recursive = yes
""",
    "KNOWN CLASS: a section's path = under a recursive section": """
[tank]
    recursive = yes
    monthly = 0
[keep-a]
    path = tank/a
    monthly = 12
""",
    "KNOWN CLASS: a recursive section's path = above the recursive section it sits under": """
[inner]
    path = tank/a
    recursive = yes
    monthly = 12
[tank]
    recursive = yes
    monthly = 0
""",
    "KNOWN CLASS: one section takes, another with path = prunes": """
[tank/a]
    autoprune = no
[p]
    path = tank/a
    autosnap = no
""",
    "a recursive parent that skips itself, and a section with path = for the parent (#1088's layout)": """
[tank]
    recursive = yes
    daily = 9
[tank/a]
    recursive = yes
    process_children_only = yes
    daily = 3
[a-itself]
    path = tank/a
    daily = 5
""",
    "a section with path = under skip_children, and one that neither takes nor prunes": """
[tank]
    skip_children = yes
[x]
    path = tank/a
    daily = 3
[watch]
    path = tank/a
    autosnap = no
    autoprune = no
""",
    "an empty here-doc path is the section name": "[tank]\n    path = <<EOT\nEOT\n    daily = 3\n",
    "a template ABOVE its section is trimmed by upstream too": "[template_t]\n    autoprune = no \n    daily = 3 \n[tank]\n    use_template = t\n",
    # known classes (DEV-001): see _known_class
    "KNOWN CLASS: a template BELOW its section reaches it untrimmed upstream": (
        "[tank]\n    use_template = t\n[template_t]\n    autoprune = no \n    daily = 3 \n    prune_defer = 80\t\n"),
    "KNOWN CLASS: a repeated key in a [template_default] below its section stays an ARRAY ref upstream": (
        "[tank]\n    hourly = 0\n[template_default]\n    autoprune = no\n    autoprune = no\n    daily = 3\n    daily = 30\n"),
    "KNOWN CLASS: an untrimmed `autosnap = yes ` escapes upstream's zfs-recursion guard": (
        "[tank]\n    recursive = zfs\n[tank/a]\n    use_template = t\n[template_t]\n    autosnap = yes \n"),
    # known class (DEV-004): see _membership_class
    "KNOWN CLASS: recursive = yess, which upstream reads as not recursive": "[tank]\n    recursive = yess\n    daily = 3\n",
    "KNOWN CLASS: recursive = ., which upstream's regex reads as true": "[tank]\n    recursive = .\n    daily = 3\n",
    "KNOWN CLASS: recursive = (, which ends upstream's run": "[tank]\n    recursive = (\n    daily = 3\n",
    "KNOWN CLASS: skip_children = zfs, no word for skip_children": "[tank]\n    skip_children = zfs\n    daily = 3\n",
    "recursive = usezfs: native ZFS recursion on both": "[tank]\n    recursive = usezfs\n    daily = 3\n",
    # known classes (DEV-002, DEV-003): see _pad_class
    "KNOWN CLASS: `autoprune = no` and a no-break space, which upstream reads as true": (
        "[tank]\n    autoprune = no\xa0\n    daily = 3\n"),
    "KNOWN CLASS: a toggle padded in a template, after a section that overrides it": (
        "[template_t]\n    autosnap = off\x1f\n[tank/a]\n    use_template = t\n    autosnap = yes\n[tank/b]\n    use_template = t\n"),
    "KNOWN CLASS: a use_template name and a no-break space, which upstream looks up as it is": (
        "[template_t]\n    daily = 5\n[template_u]\n    weekly = 2\n[tank]\n    use_template = t\xa0, \x85u\n"),
    "a pad in a value that isn't a toggle reads alike": "[tank]\n    pre_snapshot_script = /bin/true\xa0\n    daily = 3\x1c\n",
}

_defaults_keys = list(p._section_dict(p._read_ini(DEFAULTS, user_editable=False), "template_default").keys())
SPECIAL = {"use_template", "path", "recursive", "skip_children", "version"}
VOCAB = [k for k in _defaults_keys if k not in SPECIAL]
INTS = {"hourly": (0, 72), "daily": (0, 90), "weekly": (0, 8), "monthly": (0, 40), "yearly": (0, 5), "frequently": (0, 8)}


def _padded(word: str) -> str:
    """Now and then, `word` with a pad (PADS) before or after it, from a stream of its own so a seed's other choices
    stay the ones it gave before."""
    pick = _PAD_RANDOM.random()
    if pick >= 0.04:
        return word
    pad = _PAD_RANDOM.choice(PADS)
    return pad + word if pick < 0.015 else word + pad


_PAD_RANDOM = random.Random(0)


def rnd_val(r: random.Random, key: str) -> str:
    if r.random() < 0.03:
        return ""
    if key in p.TOGGLE_KEYS:
        v = r.choice(TT + TF)
    elif key in INTS:
        v = str(r.randint(*INTS[key]))
    elif key == "frequent_period":
        v = str(r.choice([5, 10, 15, 30]))
    elif key == "prune_defer":
        v = str(r.choice([0, 50, 80, 100]))
    elif key.endswith("_script"):
        v = r.choice(["/bin/true", "/opt/x/hook.sh", "0"])
    elif key.endswith("_min"):
        v = str(r.randint(0, 59))
    elif key.endswith("_hour"):
        v = str(r.randint(0, 23))
    elif key.endswith(("_warn", "_crit")):
        v = r.choice(["0", "90m", "28h", "32d"])
    else:
        v = "1"
    v = _padded(v)
    if r.random() < 0.2:
        v += r.choice([" ", "\t", "  ", " \t", "\f"])  # kept by Config::IniFiles (see the module docstring)
    return v


def body_lines(r: random.Random, n: int) -> list[str]:
    body = []
    for k in r.sample(VOCAB, n):
        body.append(f"    {k} = {rnd_val(r, k)}")
        if r.random() < 0.08:
            body.append(f"    {k} = {rnd_val(r, k)}")  # a repeated key
    return body


def gen_conf(r: random.Random) -> bytes:
    blocks = []
    templates = [f"t{i}" for i in range(r.randint(0, 3))]
    for t in templates:
        body = body_lines(r, r.randint(0, 4))
        if r.random() < 0.1:
            body.append("    recursive = yes")
        blocks.append((f"template_{t}", body))
    if r.random() < 0.4:
        body = body_lines(r, r.randint(0, 4))
        if r.random() < 0.1:
            body.append("    use_template = t0")
        if r.random() < 0.1:
            body.append(f"    {r.choice(['recursive = yes', 'path = tank/b', 'process_children_only = yes '])}")
        blocks.append(("template_default", body))
    for n in r.sample(FAKE_TREE, r.randint(1, 6)):
        body = []
        if templates and r.random() < 0.6:
            chosen = r.sample(templates, r.randint(1, min(2, len(templates))))
            body.append("    use_template = " + r.choice([",", ", ", " , "]).join(_padded(t) for t in chosen)
                        + r.choice(["", "", " "]))
        rec = r.choice([None, None, None, "yes", "no", "zfs", "1", "0", "ZFS", "usezfs"])
        if r.random() < 0.03:
            rec = r.choice(["yess", ".", "y.s", ".*", "(", "maybe"])  # DEV-004's class
        if rec:
            body.append(f"    recursive = {rec}" + r.choice(["", "", " "]))
        if r.random() < 0.1:
            body.append(f"    skip_children = {r.choice(TT + TF) if r.random() < 0.9 else r.choice(['maybe', '.', 'zfs'])}")
        if r.random() < 0.12:
            body.append(f"    path = {r.choice(FAKE_TREE)}" + r.choice(["", "", " "]))
        body += body_lines(r, r.randint(0, 5))
        r.shuffle(body)
        blocks.append((n, body))
    r.shuffle(blocks)
    parents_first(r, blocks)
    return ("\n\n".join(f"[{name}]\n" + "\n".join(body) for name, body in blocks) + "\n").encode()


def parents_first(r: random.Random, blocks: list[tuple[str, list[str]]]) -> None:
    """Mostly, a section above its datasets' sections, as the order rule has them, so recursion's merge is compared
    and not only its refusal: dataset sections sorted by depth in the places they fell, templates left where they are."""
    if r.random() < 0.85:
        at = [i for i, (name, _) in enumerate(blocks) if not name.startswith("template_")]
        for i, block in zip(at, sorted((blocks[i] for i in at), key=lambda b: b[0].count("/")), strict=True):
            blocks[i] = block


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["cases", "fuzz"])
    ap.add_argument("n", nargs="?", type=int, default=400)
    ap.add_argument("seed", nargs="?", type=int, default=1)
    args = ap.parse_args()

    tally: dict[str, int] = {}
    failed = False
    if args.mode == "cases":
        for name, text in CASES.items():
            kind, diffs = run_both(text)
            if name.startswith("KNOWN DIVERGENCE"):
                ok, label = kind in ("differ", "known"), "expected divergence"
            elif name.startswith("KNOWN CLASS"):
                ok, label = kind == "known", "known"
            else:
                ok, label = kind == "match", kind
            label = label if ok else f"UNEXPECTED {kind}"
            failed = failed or not ok
            tally[label] = tally.get(label, 0) + 1
            print(f"[{label.upper():20}] {name}")
            for d in diffs:
                print("     ", d)
    else:
        r = random.Random(args.seed)
        _PAD_RANDOM.seed(args.seed)
        first: dict[tuple, bytes] = {}
        for _ in range(args.n):
            text = gen_conf(r)
            kind, diffs = run_both(text)
            tally[kind] = tally.get(kind, 0) + 1
            if kind in ("differ", "perl_died", "python_died"):
                failed = True
                sig = tuple(sorted({d.split("] ", 1)[1].split(":", 1)[0] if "] " in d else d for d in diffs}))
                first.setdefault(sig, text)
        for text in first.values():
            _, diffs = run_both(text)
            print("\n--- differs:\n" + text.decode("latin-1"))
            print(*diffs, sep="\n   ")
    print(tally)
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
