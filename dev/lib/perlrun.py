"""Run a small, disposable Perl program built from (head, extracted subs, tail), feeding it a
scenario as JSON on stdin and reading its result back as JSON from an output file -- not from
stdout, because several of the subs under test (getsnaps, prune_snapshots, ...) print their own
INFO:/WARN: lines to stdout when verbose, which would otherwise corrupt a stdout-as-JSON
convention. stdout/stderr are still captured and returned raw, for a diff_*.py that wants to
assert on one of those lines too.

The pattern every diff_*.py in this directory shares: HEAD declares whatever globals/stubs the
extracted subs' bodies expect (matching upstream sanoid's own lexical scoping -- see
dev/lib/extract.py's module docstring and dev/README.md for why), the extracted subs are
upstream's own verbatim code, and TAIL reads the scenario (from stdin and/or argv), calls the
sub(s) under test, and writes one JSON object to the path in $ENV{DIFF_OUT}.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PERL = "perl"
PERL_JSON_HEAD = "use strict;\nuse warnings;\nuse JSON::PP;\n"
CPAN_CACHE = Path(__file__).resolve().parent.parent / ".cpan_cache"


def cpan_env() -> dict[str, str]:
    """INI_LIB and STRINGY_LIB, for a HEAD that loads the real Config::IniFiles (`BEGIN { unshift @INC, ... }`)."""
    ini = sorted(CPAN_CACHE.glob("Config-IniFiles-*"))
    stringy = sorted(CPAN_CACHE.glob("IO-stringy-*"))
    if not ini or not stringy:
        raise RuntimeError(
            "Config::IniFiles not found in dev/.cpan_cache/ -- run `python3 dev/fetch_cpan.py` first. The "
            "init() harnesses need the real module, not a mock: its own quirks (a value's trailing whitespace "
            "kept, a repeated key as an array reference) are part of what they compare."
        )
    return {"INI_LIB": str(ini[-1] / "lib"), "STRINGY_LIB": str(stringy[-1] / "lib")}

# TAIL scripts read their scenario with this, and write their result with the matching writer below.
READ_SCENARIO_PERL = 'my $__scn = do { local $/; decode_json(<STDIN>) };'
WRITE_RESULT_PERL = (
    'open my $__of, ">", $ENV{DIFF_OUT} or die "cannot write $ENV{DIFF_OUT}: $!";\n'
    'print $__of encode_json(\\%__result);\n'
    'close $__of;'
)


class PerlError(RuntimeError):
    """The assembled Perl program failed to compile/run AND produced no result file -- a genuine
    harness bug (syntax error, an exception the TAIL didn't mean to let propagate), not a
    modeled upstream failure (those should be caught in TAIL and written into the result as data,
    e.g. {"died": "..."}, so the comparison can see WHAT upstream said, not just THAT it failed).
    Includes the program text and stderr so the failure is debuggable without re-deriving what
    was actually sent to `perl`."""


@dataclass
class Result:
    data: Any  # the JSON written to $DIFF_OUT, or None if the script never wrote it
    stdout: str
    stderr: str
    returncode: int


def build_harness(head: str, body: str, tail: str) -> str:
    return f"{head.rstrip()}\n\n{body.strip()}\n\n{tail.strip()}\n"


def run_harness(script: str, *, stdin_obj: Any = None, argv: list[str] | None = None,
                 env: dict[str, str] | None = None, cwd: Path | None = None,
                 timeout: float = 10) -> Result:
    """Writes `script` to a temp .pl file and runs it: argv appended after the script path, the
    JSON encoding of stdin_obj piped to stdin if given. Reads the result back from a fresh temp
    file whose path is exported as $ENV{DIFF_OUT} -- TAIL must write to it (see WRITE_RESULT_PERL).

    Never raises on a nonzero exit by itself: some harnesses model a legitimate upstream outcome
    as a direct exit() (e.g. pod2usage(2) for a bad option), which looks identical, from the
    outside, to a genuine crash -- only the CALLER knows which exit codes its own harness expects.
    Always returns a Result; `.data` is None when TAIL never got to write $DIFF_OUT (whether that
    means "modeled failure" or "actual crash" is for the caller to decide from `.returncode` and
    `.stderr`). Only raises PerlError for something no caller could sensibly model: a timeout, or
    $DIFF_OUT existing but not being valid JSON (TAIL's own WRITE_RESULT_PERL always writes valid
    JSON, so that means TAIL was never reached with a sane %__result -- a genuine harness bug)."""
    with tempfile.NamedTemporaryFile("w", suffix=".pl", delete=False) as f:
        f.write(script)
        script_path = f.name
    out_fd, out_path = tempfile.mkstemp(suffix=".json")
    os.close(out_fd)
    os.unlink(out_path)  # existence itself is the "did the script write it" signal
    try:
        full_env = dict(os.environ, DIFF_OUT=out_path, **(env or {}))
        try:
            proc = subprocess.run(
                [PERL, script_path, *(argv or [])],
                input=json.dumps(stdin_obj).encode() if stdin_obj is not None else None,
                capture_output=True, env=full_env, cwd=cwd, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise PerlError(f"perl harness timed out after {timeout}s\n\n--- script ---\n{script}") from e
        data = None
        if os.path.exists(out_path):
            try:
                data = json.loads(Path(out_path).read_text())
            except json.JSONDecodeError as e:
                raise PerlError(
                    f"$DIFF_OUT was not valid JSON: {Path(out_path).read_text()[:500]!r}\n"
                    f"--- stderr ---\n{proc.stderr.decode('utf-8', 'replace')}\n--- script ---\n{script}"
                ) from e
        return Result(data, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"),
                      proc.returncode)
    finally:
        os.unlink(script_path)
        if os.path.exists(out_path):
            os.unlink(out_path)
