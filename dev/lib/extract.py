"""Extract one Perl `sub NAME { ... }` verbatim from upstream/sanoid, by NAME and brace-matching --
not by line number. Line numbers drift the moment upstream changes; a name-based extraction either
finds the current sub correctly or fails loudly (UpstreamShapeError), which is what a differential
test comparing against a moving reference implementation needs: silently comparing against the
wrong code is worse than refusing to run.

Handles, scanning character by character: '#' comments (to end of line), single- and double-quoted
strings (backslash-escaped), and '/.../ ' regex/match/substitution literals (m/.../, s/...*/.../,
tr/.../.../, qr/.../, and a bare leading /.../) -- braces inside any of those don't count towards
depth. Does NOT handle heredocs or non-'/' regex delimiters (q{...}, m{...}, etc.) -- none of the
subs this file's diff_*.py scripts extract use them (checked against the current pin when this was
built: every target sub matched the boundaries found by hand; every harness run extracts them again). If a
future upstream rewrite introduces one into a targeted sub, extraction will raise (unbalanced
braces) rather than silently returning a truncated or overrun sub body -- fix it here, don't work
around it in a diff_*.py script.
"""
from __future__ import annotations

import re
from pathlib import Path

PYSANOID_UPSTREAM_COMMIT = "d39b51a013081e86bb26a6859866c34d98c39ea3"

DEV_DIR = Path(__file__).resolve().parent.parent
UPSTREAM_FILE = DEV_DIR / "upstream" / "sanoid"

_REGEX_WORD = re.compile(r"\b(?:m|s|tr|qr)$")


class UpstreamShapeError(RuntimeError):
    """Upstream's source no longer looks like what this extraction expected -- a stale line-number
    guess, a renamed/removed sub, or a construct the brace-scanner doesn't understand. Meant to be
    loud: a differential test result is only meaningful if both sides ran the code they claim to."""


def load_upstream() -> str:
    if not UPSTREAM_FILE.exists():
        raise UpstreamShapeError(
            f"{UPSTREAM_FILE} is missing. See dev/upstream/PROVENANCE.md to (re-)vendor it."
        )
    text = UPSTREAM_FILE.read_text()
    # loud on a pin mismatch, not silent -- the whole point of pinning is that every diff_*.py's
    # comments about "line N" and "confirmed against a real Perl interpreter" refer to ONE exact commit
    if PYSANOID_UPSTREAM_COMMIT not in (DEV_DIR / "upstream" / "PROVENANCE.md").read_text():
        raise UpstreamShapeError(
            "dev/lib/extract.py's PYSANOID_UPSTREAM_COMMIT and dev/upstream/PROVENANCE.md have "
            "drifted apart -- update both together (see PROVENANCE.md's 'Updating the pin')."
        )
    return text


def _skip_string(text: str, i: int, quote: str) -> int:
    """i is the index of the opening quote; returns the index just past the matching close."""
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    raise UpstreamShapeError(f"unterminated {quote!r} string starting search from index {i}")


def _skip_slash_regex(text: str, i: int) -> int:
    """i is the index of the opening '/'; returns the index just past the matching close, skipping
    a trailing modifier-letters run (e.g. 'gi')."""
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "/":
            i += 1
            while i < len(text) and text[i].isalpha():
                i += 1
            return i
        i += 1
    raise UpstreamShapeError("unterminated /.../ regex literal")


def _find_sub_start(text: str, name: str) -> int:
    m = re.search(rf"(?m)^sub\s+{re.escape(name)}\s*\{{", text)
    if not m:
        raise UpstreamShapeError(
            f"no 'sub {name} {{' found in upstream/sanoid -- renamed, removed, or reformatted "
            f"upstream of the pinned commit? Check dev/upstream/PROVENANCE.md."
        )
    if len(re.findall(rf"(?m)^sub\s+{re.escape(name)}\s*\{{", text)) > 1:
        raise UpstreamShapeError(f"'sub {name}' appears more than once in upstream/sanoid")
    return m.start()


def extract_sub(name: str, source: str | None = None) -> str:
    """The verbatim text of `sub NAME { ... }`, opening 'sub' through the matching closing brace,
    inclusive. Raises UpstreamShapeError rather than guessing if anything looks off."""
    text = source if source is not None else load_upstream()
    start = _find_sub_start(text, name)
    depth = 0
    i = start
    prev_nonspace = ""
    while i < len(text):
        c = text[i]
        if c == "#":
            nl = text.find("\n", i)
            i = len(text) if nl < 0 else nl + 1
            continue
        if c in "'\"":
            i = _skip_string(text, i, c)
            continue
        if c == "/" and (prev_nonspace in "=(,!~" or _REGEX_WORD.search(text[max(0, i - 3):i])):
            i = _skip_slash_regex(text, i)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        if not c.isspace():
            prev_nonspace = c
        i += 1
    raise UpstreamShapeError(f"ran off the end of the file looking for the closing brace of 'sub {name}'")


def extract_subs(*names: str, source: str | None = None) -> str:
    """Several subs, concatenated in the order given, each on its own verbatim block."""
    text = source if source is not None else load_upstream()
    return "\n\n".join(extract_sub(n, text) for n in names)


def extract_between(start_text: str, end_text: str, *, source: str | None = None) -> str:
    """The verbatim text from the start of the (unique) line containing `start_text` through the
    end of the (unique) line containing `end_text`, inclusive of both. For the top-level script
    statements this file has no sub-name boundary for (option parsing, the main dispatch) -- less
    robust than extract_sub's brace-matching (a rewrite that keeps the same behavior but changes
    this exact wording would break it), but still anchored to distinctive source text rather than a
    line number, and still fails loudly (UpstreamShapeError) rather than silently extracting the
    wrong range."""
    text = source if source is not None else load_upstream()
    for label, needle in (("start", start_text), ("end", end_text)):
        if text.count(needle) != 1:
            raise UpstreamShapeError(
                f"extract_between {label} marker {needle!r} appears {text.count(needle)} times in "
                f"upstream/sanoid, not exactly once -- pick a more specific anchor."
            )
    start_line = text.rfind("\n", 0, text.index(start_text)) + 1
    end_idx = text.index(end_text) + len(end_text)
    end_line = text.find("\n", end_idx)
    end_line = len(text) if end_line < 0 else end_line
    return text[start_line:end_line]
