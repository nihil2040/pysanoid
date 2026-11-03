# Vendored upstream reference source

`sanoid` in this directory is the **unmodified** main script from upstream Perl sanoid
(https://github.com/jimsalterjrs/sanoid), pinned at commit:

```
d39b51a013081e86bb26a6859866c34d98c39ea3
```

(master, the v2.3.0 line, fetched 2026-09.) It is here **only** as the reference
implementation the `dev/diff_*.py` differential tests extract subroutines from verbatim and
run under a real Perl interpreter, to compare against pysanoid. It is not part of
pysanoid, is not imported or executed by pysanoid itself, and is not installed with it.
It is published in this repository, unmodified and under upstream's own GPLv3 terms, only
as a dev-only comparison fixture, the same idea as vendoring a reference implementation's
test vectors.

It is upstream's own code, licensed by its authors under GPLv3 (see the header comment in
the file itself). Keeping it here, unmodified, with this note, is the same kind of use as
any other porting project keeping a copy of the reference implementation for differential
testing.

## Updating the pin

To re-audit against a newer upstream commit, replace this file with the new version and
update the commit hash above **and** `PYSANOID_UPSTREAM_COMMIT` in `dev/lib/extract.py`
(the differential scripts assert the two match, so a stale pin fails loudly instead of
silently comparing against the wrong code):

```bash
curl -fsSL https://codeload.github.com/jimsalterjrs/sanoid/tar.gz/<NEW_COMMIT> -o /tmp/sanoid.tar.gz
tar -xzf /tmp/sanoid.tar.gz -C /tmp
cp /tmp/sanoid-<NEW_COMMIT>/sanoid dev/upstream/sanoid
```

Then update the pin in both places above, and re-run `dev/run.py` — every extraction is by
subroutine name (brace-matched), not by line number, so it will pick up the new source
correctly, but re-reading each `diff_*.py`'s docstring against the new upstream diff is
still worth doing by hand before trusting a clean run.
