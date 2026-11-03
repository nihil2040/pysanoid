# Contributing to pysanoid

Thank you for helping. pysanoid destroys snapshots for a living, so it changes slowly and every change is checked.
This page says what a change needs before it can be merged.

## The rule

pysanoid matches sanoid one for one, unless matching it has a plausible path to losing snapshots that the
configuration means to keep. So:

- **A difference from sanoid that loses nothing is a bug in pysanoid.** The fix makes pysanoid do what sanoid does.
- **A deliberate difference needs a reason that involves data.** It gets an entry in [DEVIATIONS.md](DEVIATIONS.md),
  with sanoid's behaviour, pysanoid's, why, the evidence (ideally sanoid's Perl run on the same input), the tests,
  and any sanoid issues behind it. The code marks it with the entry's ID, as `# DEV-NNN: one line`.
- **No new features that sanoid doesn't have,** unless they are off by default and an existing `sanoid.conf` behaves
  exactly as before. Open an issue first to talk it over.

pysanoid needs Python 3.11 or later and nothing outside the standard library. Please keep it that way.

## Before you open a pull request

Run the tests and the linter from the repository's root:

```sh
python3 -m pytest test_pysanoid.py test_pysanoid_alert.py
ruff check .
```

The tests need only pytest. They include a check that every test name, file, `dev/` path and DEV ID the documentation
cites exists, so a rename has to update the docs too.

If your change touches what a run takes, destroys, prints or exits with, also run the comparison with sanoid's Perl:

```sh
python3 dev/run.py
```

It needs Perl with Capture::Tiny; [dev/README.md](dev/README.md) says how to set it up and what each target checks.

## What a change should include

- **A test that fails before the change and passes after it.** For a bug, the test reproduces it.
- **The documentation it affects:** [HANDBOOK.adoc](HANDBOOK.adoc) for anything a user sees, DEVIATIONS.md for a
  difference from sanoid, and a dated line in [AUDIT.md](AUDIT.md)'s timeline for a fix.
- **A version bump** in `VERSION` in `pysanoid.py`, for a change to what pysanoid does.

Commit messages start with what they change, such as `pysanoid 0.1.54: ...` for a release, or `pysanoid: ...` for
the tests, `dev/` or the docs.

## Reporting bugs

Open an issue with pysanoid's version, the operating system, the `sanoid.conf` involved and the output of the run
with `--readonly --verbose`. A bug that could destroy snapshots should be reported privately instead; see
[SECURITY.md](SECURITY.md).

## Licence

pysanoid is licensed under the GNU General Public License v3.0. By contributing, you agree that your contribution is
licensed under the same terms.
