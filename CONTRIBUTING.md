# Contributing to pysanoid

pysanoid is a personal project, published as-is so that others can use it. It is not run as a community project:
issues and pull requests may be read and taken up, or not, whenever its maintainer gets to them. There is no promise
of a reply, a review or a fix.

**Forking is encouraged.** pysanoid is licensed under the GNU General Public License v3.0, so you may fork it, change
it and publish your own version, as long as your version stays under the same licence. You don't need to ask.

## If you send a change anyway

A change is more likely to be taken up if it follows the project's rule and comes with its tests.

**The rule:** pysanoid matches sanoid one for one, unless matching it has a plausible path to losing snapshots that
the configuration means to keep.

- A difference from sanoid that loses nothing is a bug in pysanoid, and the fix makes pysanoid do what sanoid does.
- A deliberate difference needs a reason that involves data, and an entry in [DEVIATIONS.md](DEVIATIONS.md) with
  sanoid's behaviour, pysanoid's, why, and the evidence. The code marks it with the entry's ID, as `# DEV-NNN: ...`.
- pysanoid needs Python 3.11 or later and nothing outside the standard library.

**The checks:** run these from the repository's root:

```sh
python3 -m pytest test_pysanoid.py test_pysanoid_alert.py
ruff check .
```

If the change touches what a run takes, destroys, prints or exits with, also run `python3 dev/run.py`, which compares
pysanoid with sanoid's own Perl; [dev/README.md](dev/README.md) says what it needs.

A change should come with a test that fails before it and passes after it, and should update
[HANDBOOK.adoc](HANDBOOK.adoc) for anything a user sees.

## Licence

By contributing, you agree that your contribution is licensed under the GNU General Public License v3.0.
