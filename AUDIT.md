# pysanoid audit summary

Scope: an adversarial, module-by-module audit of pysanoid's snapshot take/prune logic
against upstream Perl sanoid, pinned at master `d39b51a013081e86bb26a6859866c34d98c39ea3`
(the v2.3.0 line). **syncoid, findoid, and the monitor-\* Nagios checks were never in
scope** — pysanoid does not implement them, and this audit says nothing about them.

Written 2026-09-22, after the audit's 8th and final module, as a summary of the audit as a
whole and an answer to whether this release had earned a 1.0 tag. Every number below is re-derived from the actual git history and
the current source as of this commit, not recalled from memory.

## Verdict, up front

**Tag it — but as 1.0 of what pysanoid actually is: a narrower, more rigorously verified
take/prune tool, not a drop-in replacement for the whole sanoid project.** Within that
scope, this code is more conservative than upstream everywhere the audit found a
difference, backed by real differential testing rather than assertion, and every fix
carries a regression test that fails before it and passes after. It does not yet carry
upstream's other kind of evidence — years of diverse, independent production use — and no
amount of code audit manufactures that; only time does. See "What this cannot yet claim"
below before deciding how to describe it to anyone else.

## Timeline

| Date | Event |
|---|---|
| 2026-09-14 | First commit |
| 2026-09-16 | An earlier hardening pass (`ee99f95`), predating the systematic audit |
| 2026-09-21 – 2026-09-22 | The 8-module systematic audit (this document), `b479b29` → `95f71c6` |
| 2026-09-22 | `0.1.21` deployed to production |
| 2026-09-24 | Comments trimmed (no code change); `0.1.22` fixes the post-hook `dst_` name parity gap |
| 2026-09-24 | Second-pass re-audit begins; `0.1.23` fixes the four divergences its first unit (`prune_snapshots`) found |
| 2026-09-24 | `0.1.24` fixes the two `prune_defer` divergences re-audit unit 1b found |
| 2026-09-24 | `0.1.25` fixes what re-audit unit 6a (take planning) found, and adds a DST check to `dev/` |
| 2026-09-24 | `0.1.26` fixes what re-audit unit 6b (take execution) found; the take harness now compares stdout and can make a call fail to start |
| 2026-09-25 | `0.1.26` deployed to production; `0.1.27` fixes what re-audit unit 2a (config loading) found, and the config-merge harness now runs on the real `Config::IniFiles` |
| 2026-09-25 | `0.1.28` reads a blank preferred-time setting as upstream does (the last unit 2a finding) |
| 2026-09-25 | `0.1.29` strips a byte-order mark encoded twice, as `Config::IniFiles` does (re-audit unit 2b, the ini grammar), and the grammar gets its own `dev/` target |
| 2026-09-25 | `0.1.30` fixes what re-audit unit 3a (the snapshot listing) found: a cache that can't be read or written no longer stops a run, and a NUL byte in the config is refused |
| 2026-09-25 | `0.1.31` fixes what re-audit unit 3b (the per-type and per-path tables) found: a snapshot's name can no longer stand in for a snapshot type, a damaged creation time no longer stops a run, and ages are whole seconds |
| 2026-09-25 | `0.1.32` fixes what re-audit unit 3c (the cache add and remove) found: a child lookup that can't start no longer stops the run, the remove writes lines back as upstream reads them, and the register now says what upstream's re-read of the snapshot list does |
| 2026-09-25 | `0.1.33` fixes what re-audit unit 4 (the dataset cache) found: a child lookup that comes back short stops the run instead of letting prune use the defaults for a child with its own section, a damaged datasets.txt no longer stops a run, and `--debug` and the warnings read as upstream's |
| 2026-09-25 | `0.1.34` fixes what re-audit unit 7 (hooks and locks) found: a lock file that can't be made no longer stops every snapshot, a `zfs destroy` that can't start no longer ends the prune run, a damaged snapshots.txt is no cache, and `script_timeout` and hook command lines are read as upstream reads them |
| 2026-09-26 | `0.1.35` fixes what re-audit unit 5 (options and main flow) found: output that can't be written no longer stops the run, a directory pysanoid can't search no longer stops it on production's Python 3.11, a `timedatectl` that doesn't answer no longer stops take, and `$TZ` and two messages read as upstream's; the tests and the whole `dev/` gate now also run on Python 3.11 |
| 2026-09-26 | `0.1.36` fixes what re-audit unit 8 (`--debug`) found: the config dump is `Data::Dumper`'s text, upstream's config-merge trace is printed, and the child lookup's line, the config file names and a damaged cache's ages read as upstream's |
| 2026-09-26 | `0.1.36` deployed to production. The upstream history pass reads upstream's 1,061 issues and pull requests; `0.1.37` fixes the one new problem it finds (a section placed above the recursive section it belongs under is refused, where upstream lets that section's recursion overwrite it), and nine register entries now cite the upstream reports behind them |
| 2026-09-27 | `0.1.37` deployed to production the evening before. The long-horizon simulator, which `dev/run.py` never ran and which had broken without failing after `0.1.25`, is rebuilt and becomes a `dev/run.py` target; on the production config it finds nothing wrong across 4 years and 8 clock changes |
| 2026-09-27 | `0.1.38` closes the leads left after the second pass: two sections that take or prune the same dataset are refused, an empty `$TZ` and a copied /etc/localtime are read as the C library reads them, and a setting upstream doesn't know is refused in upstream's words. Read together, the re-audit's own changes (`0.1.22` to `0.1.37`) need nothing more |
| 2026-09-27 | `0.1.38` committed. `0.1.39` reads `$TZ` and /etc/localtime as glibc does: a POSIX rule is applied, a zone name is looked up where glibc looks, and anything naming no zone is UTC with a warning, as timedatectl and /etc/timezone no longer decide. A damaged zone file, which under production's Python 3.11 could hang or crash every run, now reads as no zone. The prune harness gains the run directory no lock file can be made in |
| 2026-09-28 | `0.1.39` a day in production, every run clean. Both systemd units now stop a run after 30 minutes, so a hung zfs call can no longer stop snapshots and pruning for good without a failure. The plan for the next audit is written: the last section below |
| 2026-10-10 | The third audit, the one that plan describes: does pysanoid keep its promises when what a run reads is wrong? Every file operation and zfs call of a run is failed in turn, and upstream is run beside pysanoid on old and damaged caches and a clock set ahead. `0.1.40` fixes the three ways it found, all shared with upstream, for prune to destroy what the config keeps: a child's own section built from the defaults when its recursive parent's lookup didn't list it, a cache dated in the future never expiring, and a snapshot cache cut short inside its last line |
| 2026-10-10 | `0.1.40` committed. `0.1.41` fixes two more of that audit's findings: a snapshot listing killed inside its last line goes without that line, where both programs read it and destroyed a dataset's newest daily, and a retention count too long for a number no longer ends prune with a traceback. A newest snapshot dated in the future still holds its type back in silence, as upstream's does: the decision of 2026-09-27 stands, by the rule that pysanoid mirrors upstream unless the data is at risk. The freshness check, which is not part of the port, no longer calls such a snapshot fresh |
| 2026-10-10 | `0.1.41` committed. `0.1.42` fixes the last two of that audit's findings, both places where pysanoid had drifted from upstream. What zfs or zpool prints is no longer read in a way that can fail: a message that isn't UTF-8 had turned a command that worked into one that "could not run", so a snapshot that was taken was reported as failed, a destroy that worked stayed in the cache, and a prune run that asks zpool how full the pool is ended there. And a dataset's snapshot names carry the time its turn came, as upstream's do, where every name of a run had carried the time the run began |
| 2026-10-11 | `0.1.42` committed the night before. `0.1.43` closes the two differences that work had found, both in where zfs's own words are shown. What `zfs snapshot`, `zfs destroy` and `zpool list` write now goes out as they wrote it, whether the command worked or failed, and a listing's complaint keeps its bytes. With zfs's words shown as upstream shows them, pysanoid's own failure lines take upstream's words (`CRITICAL ERROR: zfs snapshot ... failed, 256`, `could not remove ... : 256`), which reverses a closure of 2026-09-27, and `prune_defer`'s capacity answer is read as upstream reads it, except where upstream would prune by a number that needn't be the pool's. The prediction for 25 October is corrected: the production timer starts one run, not two, for the hour the clock repeats, by systemd's source and by the production host's own `systemd-analyze` |
| 2026-10-11 | `0.1.43` committed and pushed. `0.1.44` changes nothing a run does. ruff, when it lints for Python 3.11 or later, as the user's editor does, asks for `datetime.UTC` where `pysanoid.py` wrote `timezone.utc`: the same object under the name 3.11 gave it. With that name `pysanoid.py` needs Python 3.11 or later, production's, where `0.1.43` still started under 3.9. The gate's ruff had been given no target and so never asked; a `ruff.toml` in the repo now sets it |
| 2026-10-11 | `0.1.44` committed. `0.1.45` gives a failed hook's warning upstream's words, `WARN: <key> failed, 768`, the last line where pysanoid's wording differed by an earlier decision (2026-09-25), now reversed. With the status in the line, one more difference showed: for a hook command that can't be exec'd upstream says `-1`, where pysanoid, which ran such commands through `/bin/sh`'s `exec`, said 127. pysanoid now execs them itself, as Perl does. And an import used only for type checking no longer fails a check made for Python 3.11 |
| 2026-10-11 | `0.1.45` committed, pushed, and in production the same day, with `pysanoid_alert.py` and the units as they now stand. The third audit's two probes, one-offs until now, become `dev/run.py` targets, and the wider simulator its plan asked for is built as a third: `fault_sweep`, `config_fuzz` and `trouble` run pysanoid as a whole program and judge it by what its config promises, with no Perl. On `0.1.45` they find nothing, and nothing in `pysanoid.py` changes. The audit is closed: see the last section |
| 2026-10-25 | The night summer time ends, on `0.1.45`: the production timer starts one run for 01:00 and the next at 02:02 UTC, tank/media's two hourlies either side stand 7,304 seconds apart, none is named `dst_`, and every run is clean. See "Field time, meanwhile" |
| 2026-10-25 | `0.1.46` changes no behavior: the register moves from `pysanoid.py` to `DEVIATIONS.md`, each entry with an ID marked beside its code. Checking every entry as it moved corrected two entries' reasoning and one's account of upstream (a missing zfs reads there as a listing that worked and found nothing), and found that a deviation called harmless guards a path to data loss: upstream caches what a killed child lookup printed, a name cut short included, and a pool whose name is a prefix of the parent's can lose a daily its retention keeps. Five entries that rested on a reading or a one-off now have checks that run every time, among them a new `dev/run.py` target comparing upstream's whole script with pysanoid as programs. See the end of the last section |
| 2026-10-25 | A fourth audit, of security and data safety, changes nothing in `pysanoid.py`. `pysanoid_alert.py`'s freshness check now reports a dataset section whose name starts with "template": pysanoid, as upstream, never takes or prunes its snapshots, and the check had called such a config fine. See "The fourth audit" |
| 2026-10-25 | `0.1.47` reads the same lines in linear time. Four regexes, among them the snapshot line's, took quadratic time in Python on a line built for it (a damaged cache's, or root's own config); each is replaced by code that gives what the regex gave, checked against it on 2.5 million random strings. Nothing a run does changes. See "The fourth audit" |
| 2026-10-25 | `0.1.48`: a process that finds a lock held no longer empties the lock file, so the holder's PID stays in it for a person looking; the PID is written once the lock is held. The fourth audit's remaining items close with no change. See "The fourth audit" |
| 2026-10-25 | A fifth audit compares upstream with pysanoid from a blank slate, reading both whole and running upstream's whole script beside pysanoid on 31 scenarios, the production config among them. `0.1.49` closes the two differences it found: a hook's variables are set in pysanoid's own environment and deleted after the hook, as upstream's are (P9), and the defaults file's version is read as plain digits (DEV-050). See "The fifth audit" |
| 2026-10-25 | A sixth audit stresses the port with property tests, inputs at scale and a hunt for Perl behavior the Python reads differently. `0.1.50` refuses a toggle such as `autoprune = no` followed by a no-break space, as DEV-002 says, where `str.strip()` had read it as "no". See "The sixth audit" |
| 2026-10-27 | `0.1.51` changes no behavior. `pysanoid.py`'s comments are cut to a summary and the ID of the entry in `DEVIATIONS.md` that explains them, which gains twelve parity notes (P10 to P21) for what the comments had held alone. A `README.md` says what pysanoid is and why it was ported. The project names no host: the production config and units move out of it, the repo ships an example `sanoid.conf` and units after upstream's Debian packaging, and the dev targets that ran the production config run a fixture of the same shape (`dev/fixtures/sanoid.conf`) |
| 2026-10-28 | A review of the comments against the code corrects eight of them, with no code change. `0.1.52` reads the capacity answer's trailing whitespace as Perl's `\s`, so a no-break space or a control character after `85%` is more than a capacity and refused (DEV-016), where `str.strip()` had trimmed it and a whole run pruned 3 of 5 dailies `prune_defer = 50` was holding back. It also drops a `ZeroDivisionError` from take's planning that nothing raises any more. See "The 2026-10-28 review" |
| 2026-10-28 | `0.1.52` deployed to production. Upstream's issues and pull requests since the history pass hold two about the code pysanoid ports. #1116, a hook's timeout killing sanoid itself, is DEV-042, and gains a test of its exact case. #1119, a host whose clock ran 127 years ahead and took nothing for four days, reopens #651 with what the September decision didn't weigh: once the clock is set right, a snapshot dated ahead fills one of prune's places, so prune destroys a real snapshot the count keeps. `0.1.53` sets aside every snapshot dated more than five minutes ahead (DEV-051, P16 retired), which also keeps snapshots coming while a clock runs behind. See "The 2026-10-28 review" |

The whole project, start to now, spans **28 calendar days**. The systematic audit itself
ran across parts of 2 days. This is a young codebase by any measure — see "What this
cannot yet claim."

## Growth

| | Before (`0.1.9`) | After the 8-module audit (`0.1.19`) | Current (`0.1.53`) |
|---|---|---|---|
| `pysanoid.py` | 1,553 lines | 2,075 lines | 2,178 lines (comments trimmed at `0.1.22` and `0.1.51`; the register moved to `DEVIATIONS.md` at `0.1.46`) |
| `test_pysanoid.py` | 435 lines | 1,833 lines | 5,987 lines |
| Tests | 20 | 225 | 993 |
| ruff | (not tracked) | clean | clean (`pysanoid.py`/`test_pysanoid.py` and `dev/`, separately), since `0.1.44` with production's Python 3.11 as its target (`ruff.toml`) |
| basedpyright | (not tracked) | 0 errors, 180 warnings | 0 errors, 144 warnings, since `0.1.45` also when it checks for Python 3.11 (`dev/` is a separate, explicitly not-gated-to-0 pool -- see `dev/README.md`) |
| Python the gate runs on | (not tracked) | 3.14 | 3.14 and 3.11 (production's is 3.11.9; since `0.1.44` `pysanoid.py` needs 3.11 or later) |
| `dev/run.py` targets | (scratch only) | (scratch only) | 21, 42 runs with `--fuzz`; three of them, since 2026-10-11, judge pysanoid by what its config promises, two, since 2026-10-25, run upstream's whole script beside it (`zfs_output` its words, `whole_run` what it does), and one, since 2026-10-26, runs two pysanoids at once (`overlap`); `dev/mutants.py` checks the targets, the tests and five harnesses with 229 breaks |

The `0.1.19` → `0.1.21` growth is not new audit modules -- it's two more real bugs found while
building the `dev/` differential-testing tool below (see "Update, 2026-09-22" under Recommendation).
`0.1.22` adds one more parity fix, `0.1.23` to `0.1.36` the fixes from the second-pass re-audit,
`0.1.37` the one from the upstream history pass, `0.1.38` the leads left after them, `0.1.39` `$TZ` read as
glibc reads it, `0.1.40` the three fixes of the third audit, `0.1.41` two more of its findings, `0.1.42` the last two, and `0.1.43` the two differences `0.1.42` had found in where zfs's own words are shown (see "What was actually wrong, and got fixed"). `0.1.44` renames a constant for the linter and adds no test, `0.1.45` gives a failed hook's warning upstream's words, `0.1.46` moves the register to `DEVIATIONS.md` and changes no behavior, `0.1.47` to `0.1.50` come from the fourth to sixth audits, `0.1.51` cuts the comments and changes no behavior, `0.1.52` reads the capacity answer's whitespace as Perl's `\s`, and `0.1.53` sets aside snapshots dated ahead of the clock. `pysanoid.py`'s comments were trimmed on 2026-09-24 (no
code change); `0.1.27`'s port of Perl's number reading is most of its growth since.

Test growth by step (each number is the actual count immediately before/after that
commit, taken from the commit messages themselves):

```
20 -> 75   (0.1.11, Module 1 prune + Module 6 take)
75 -> 114  (0.1.13, Module 2 config/recursion)
114 -> 125 (0.1.14, Module 3 snapshot cache)
125 -> 139 (0.1.15, Module 4 dataset cache)
139 -> 208 (0.1.16, Module 5 script flow/options)
208 -> 218 (0.1.17, Module 7 locks/hooks)
218 -> 225 (0.1.19, Module 8 --debug dump)
247 -> 265 (0.1.23, second-pass re-audit, unit 1a prune_snapshots)
265 -> 274 (0.1.24, second-pass re-audit, unit 1b prune_defer)
274 -> 310 (0.1.25, second-pass re-audit, unit 6a take planning)
310 -> 322 (0.1.26, second-pass re-audit, unit 6b take execution)
322 -> 395 (0.1.27, second-pass re-audit, unit 2a config loading)
395 -> 403 (0.1.28, unit 2a: a blank preferred-time setting)
403 -> 410 (0.1.29, second-pass re-audit, unit 2b ini grammar)
410 -> 433 (0.1.30, second-pass re-audit, unit 3a snapshot listing)
433 -> 445 (0.1.31, second-pass re-audit, unit 3b snapshot tables)
445 -> 452 (0.1.32, second-pass re-audit, unit 3c cache add/remove)
452 -> 462 (0.1.33, second-pass re-audit, unit 4 dataset cache)
462 -> 503 (0.1.34, second-pass re-audit, unit 7 hooks and locks)
503 -> 514 (0.1.35, second-pass re-audit, unit 5 options and main flow)
514 -> 528 (0.1.36, second-pass re-audit, unit 8 --debug)
528 -> 544 (0.1.37, upstream history pass)
544 -> 566 (0.1.38, the leads left: shared datasets, $TZ, an unknown setting's wording)
566 -> 583 (0.1.39, $TZ and /etc/localtime as glibc reads them)
583 -> 604 (0.1.40, the third audit: a child section its lookup didn't list, caches dated ahead or cut short)
604 -> 639 (0.1.41, the third audit: a listing cut short, a count too long for a number)
639 -> 657 (0.1.42, the third audit: what zfs prints that isn't UTF-8, the time in snapshot names)
657 -> 705 (0.1.43, zfs's own words shown as upstream shows them, the capacity answer as upstream reads it)
705 -> 733 (0.1.45, a failed hook's warning in upstream's words, hook commands exec'd as Perl execs them)
733 -> 740 (0.1.46, the register moved: pins for Ctrl-C, a missing zfs, what a hook inherits, an empty here-doc)
740 -> 968 (0.1.47 to 0.1.50, the fourth to sixth audits)
968 -> 969 (0.1.51, the example sanoid.conf loads)
969 -> 982 (0.1.52, whitespace Perl's \s doesn't match after a capacity)
982 -> 984 (the documentation's references checked)
984 -> 993 (0.1.53, snapshots dated ahead of the clock set aside; a pre hook that times out)
```

## Methodology: what was actually tested, and why

Two layers, used together, because each catches what the other can't, a third read after them, and a fourth
since the third audit:

**1. The permanent pytest suite (733 tests at `0.1.45`, ships in the repo, gate-enforced).** Every
behavioral fix has a red/green pair — confirmed to fail on the code as it stood
immediately before the fix, and to pass after. The rest are parity locks: they pin
behavior that already matched upstream, so a future change can't silently break it. This
is the suite that runs today, and the suite a future change should be checked against.

**2. Differential testing against upstream's own code (the layer that actually found the
bugs).** For each module, the relevant Perl subroutine was extracted **verbatim** — not
reimplemented, not summarized, the literal source lines — from the pinned upstream
tarball, wrapped in a small harness, and run under a real system Perl interpreter (with
the real `Config::IniFiles` fetched from CPAN where that module was involved, and a fake
`zfs` executable on `PATH` that both sides called identically). The harness then generated
random and handcrafted scenarios, ran both implementations on identical input, and
compared the outputs field by field. This is the technique that surfaced every real bug
below — reading the code carefully matters, but several of these divergences only became
visible once thousands of concrete scenarios were run through both implementations side
by side.

**Where the harnesses live:** in the repo, as `dev/` (see `dev/README.md`). It has 18 targets:
14 check pysanoid against real Perl, most of them running upstream's own subroutines extracted
verbatim from the vendored `dev/upstream/sanoid`; one, the long-horizon simulator, runs take and
prune after one another over weeks and years (a target since 2026-09-27); and three, since 2026-10-11,
are the fourth layer below. End-to-end checks run pysanoid as a real process. `python3 dev/run.py`
runs each target's hand cases and `--fuzz` adds a randomized pass; they were re-run as each fix of the
second-pass re-audit landed, and at `0.1.45` all 36 runs pass under Python 3.14 and 3.11, as do the 75
end-to-end checks. Three targets need the real
`Config::IniFiles`, fetched once with `dev/fetch_cpan.py` (its cache is not kept in git). One-off
probes written while auditing a unit are not kept; what they found is, as pytest tests or as cases
in a `dev/` target.

**3. Upstream's field record.** The pinned code is what the first two layers compare against;
upstream's GitHub issues and pull requests are where sanoid's users met that code on real pools.
Read after the second-pass re-audit, they turned up one problem neither layer had caught (the
section order, fixed in `0.1.37`) and put field reports behind nine register entries: see "What
upstream's users reported" below.

**4. Judged by what the config promises.** The first two layers ask whether pysanoid does what upstream
does, which can't see what both do wrong: the third audit found four ways for prune to destroy
snapshots the config keeps, and upstream shares all four. Three targets ask instead whether a run did
what its config promises, whatever happened around it. They run pysanoid as a whole program, in a
process of its own, on a pool held in memory and a clock they set, and judge every run by rules stated
apart from pysanoid's code: nothing leaves `main` but its return and no destroy breaks the retention,
whatever was done to the run; and a run nothing was done to takes exactly what is due, destroys
exactly what the retention gives up, exits 0, says nothing of its own on stderr and leaves its caches
true. `fault_sweep` fails every file operation and zfs call of a run in turn, in every way each can
fail; `config_fuzz` feeds it configs with odd values in every setting; `trouble` runs it round after
round through kills, clock steps and missed runs, on the production config and on random ones. See "The third
audit: where it ended", and `dev/README.md`.

### Differential results, module by module (real numbers from the commit history)

| Module | Before the fix | After the fix |
|---|---|---|
| 2 — config/recursion merge | 54 of 400 random configs diverged. *(2026-09-25, `0.1.27`)* The harness's stand-in for `Config::IniFiles` trimmed every value, so it could not see that upstream reads a template below its sections untrimmed; on the real module, with trailing whitespace, repeated keys and a `[template_default]` in the generator, 27% of random configs differ only that way (documented; pysanoid is the safe side), and an empty here-doc `path` differed (fixed) | 0 of 3,800 (merge + grammar, across both CPAN `Config::IniFiles` releases); at `0.1.27`, on the real module, nothing unexplained in 15 hand cases and 3,000 fuzzed configs across three seeds (807 in the two documented classes); at `0.1.37`, with the section order as a third known class (checked against the rule as `dev/lib/configorder.py` states it, and the trace up to the refusal against upstream's), 21 hand cases (12 match, 8 known, the tab-after-comma divergence) + 400 fuzzed configs (131 known, 24 of them the order), nothing unexplained under Python 3.14 and 3.11; on `0.1.36`, 5 hand cases and 24 of 400 fuzzed configs differ. The generator now puts parents first most of the time, so recursion's merge is still compared: shuffled freely, 23% of its configs broke the rule; at `0.1.38`, with a shared dataset as a fourth known class (the merge still compared with upstream's, the refusal with the rule as `dev/lib/configorder.py` states it), 26 hand cases (13 match, 12 known, 4 of them shared, the tab-after-comma divergence) + 400 fuzzed configs (164 known, 50 of them shared), nothing unexplained under Python 3.14 and 3.11; on `0.1.37`, those 4 hand cases and 50 fuzzed configs differ |
| 2 — ini grammar (`_ini_lines`, `_parse_ini_text`) | *(2026-09-25, second-pass re-audit, unit 2b)* The original audit fuzzed the grammar once (4,600 files, not kept), and none of its files began with a byte-order mark encoded twice, which `Config::IniFiles` strips: upstream loaded such a file and pysanoid refused it. On `0.1.28`, 5 of 40 hand cases and 846 of 20,000 fuzzed files differ | 0 unexplained at `0.1.29`: 40 hand cases (2 in the one known, harmless class) + 300,000 fuzzed files across three seeds on the pinned 3.003000, and 20,000 each on 2.94 and 3.000003 (`dev/diff_ini_grammar.py`) |
| 3 — snapshot cache | 249 of 300 matched (51 diverged). *(2026-09-25, second-pass re-audit, unit 3a)* The enumeration harness never compared stdout and never made a cache or a `zfs get` fail; extended, 10 of its 17 hand cases and about a third of 1,000 fuzzed scenarios differ on `0.1.29`. *(Unit 3b)* It built the tables on a whole-second clock, left out the sorted list prune reads, and generated one type with no same-second snapshots; widened, 5 of its 24 hand cases and 142 of 500 fuzzed scenarios differ on `0.1.30`. *(Unit 3c)* The add/remove harness stubbed the child lookup, never compared stdout and never wrote a line without a tab that spells a pruned name; widened, 5 of its 25 hand cases and 86 of 500 fuzzed scenarios differ on `0.1.31`. *(Unit 7)* Neither harness damaged a cache with a NUL or took away the run directory; with both as known classes that check pysanoid against itself, enumeration differs in 3 of 28 hand cases and 22 of 500 fuzzed scenarios on `0.1.33`, and add/remove in 3 of 30 and 23 of 500. *(2026-10-10, the third audit)* Neither harness dated a cache in the future or cut one short inside its last line, and the add/remove harness allowed pysanoid's repair of a missing final newline; with both as known classes that check pysanoid against itself, enumeration differs in 5 of 36 hand cases and 16 of 300 fuzzed scenarios on `0.1.39`, and add/remove in 1 of 30 and 12 of 300. *(2026-10-10, `0.1.41`)* Its fake zfs never cut a listing short; with a retried `zfs get` killed inside its last line as a known class, checked against real Perl and against pysanoid itself, enumeration differs in 9 of 12 new hand cases and 20 of 500 fuzzed scenarios on `0.1.40` | 1,200 of 1,200 (enumeration); 600 of 600 random + all handcrafted (add/remove); at `0.1.30`, enumeration 17 hand (5 in documented classes) + 2,000 fuzzed across four seeds, and add/remove 14 hand + 600 fuzzed, nothing unexplained; at `0.1.31`, enumeration 24 hand (6 in documented classes) + 2,000 fuzzed across four seeds, nothing unexplained, and the two tables alone match real Perl in 9,000 scenarios on a whole-second clock; at `0.1.32`, add/remove 25 hand (4 in documented classes) + 2,000 fuzzed across four seeds, nothing unexplained, and a standing check confirms what upstream's re-read does; at `0.1.34`, enumeration 28 hand (9 in documented classes) + 1,500 fuzzed across three seeds and add/remove 30 hand + 1,000 fuzzed across two seeds, nothing unexplained; at `0.1.40`, enumeration 36 hand (14 in documented classes) + 1,000 fuzzed across two seeds (39 of them a cache cut short, 30 one dated ahead) and add/remove 30 hand + 1,000 fuzzed across two seeds (41 an add onto a cache cut short), nothing unexplained under Python 3.14 and 3.11; at `0.1.41`, enumeration 48 hand (23 in documented classes) + 2,000 fuzzed across four seeds (148 of them a retry cut inside a line), nothing unexplained |
| 4 — dataset cache | 969 of 1,500 diverged (**65%**). *(2026-09-25, second-pass re-audit, unit 4)* The harness never compared stdout, never ran `--debug`, and never broke a child lookup or damaged datasets.txt; widened, 12 of its 29 hand cases and 244 of 500 fuzzed scenarios differ on `0.1.32`. *(Unit 7)* It never took away the run directory; as a known class checked against pysanoid itself, 2 of 32 hand cases and 11 of 500 fuzzed scenarios differ on `0.1.33`. *(2026-10-10, the third audit)* Its fuzz had been generating, in 5 to 13 of every 500 scenarios, a section under a recursive section whose lookup didn't list it; upstream and pysanoid both built that section from the defaults, so the two matched and nothing showed. It also never dated datasets.txt in the future. With the inheritance as a known class checked against the rule as `dev/lib/configorder.py` states it, and a future date as one checked against pysanoid with no cache, 9 of 46 hand cases differ on `0.1.39` (its fuzz can't: there pysanoid still matches upstream) | 0 of 1,500, plus 28 handcrafted; at `0.1.33`, 29 hand (7 in documented classes) + 2,000 fuzzed across four seeds, nothing unexplained; at `0.1.34`, 32 hand + 1,500 fuzzed across three seeds, nothing unexplained; at `0.1.37`, 34 hand (11 known, 2 of them the section order, datasets.txt left as it was) + 500 fuzzed (98 known, 13 the order), nothing unexplained under Python 3.14 and 3.11; on `0.1.36`, 2 hand cases and 8 of 400 fuzzed scenarios differ; at `0.1.40`, 46 hand (20 known, 7 of them the inheritance and 2 a future date) + 1,500 fuzzed across three seeds, nothing unexplained under Python 3.14 and 3.11 |
| 5 — option grammar | 265 of 6,000 diverged. *(2026-09-26, second-pass re-audit, unit 5)* Clean as it stood on `0.1.34`, under Python 3.14 and 3.11 | 0 of 6,000; at `0.1.35`, 45 hand + 2,000 fuzzed, under Python 3.14 and 3.11 |
| 5 — script-level flow | 200 of 800 diverged. *(2026-09-26, second-pass re-audit, unit 5)* The harness checked only whether the `--force-prune` warning appeared, not its words, and never the negative `--cache-ttl` error; comparing both, 3 of its 24 hand cases and 163 of 400 fuzzed scenarios differ on `0.1.34`. What it can't reach in-process (output that can't be written, the locale, the Python version) went to `dev/e2e_subprocess.py`: 6 new checks, of which 5 fail on `0.1.34` under Python 3.14 and all 6 under 3.11 | 0 of 1,500; at `0.1.35`, 24 hand + 500 fuzzed under Python 3.14 and 3.11, and 400 more on a second seed under 3.14 |
| 8 — `--debug` dump | found by close reading + targeted probes against a real Perl interpreter. *(2026-09-26, second-pass re-audit, unit 8)* The dump harness cut the config dump off, fed `blabber` hand-built tables instead of upstream's own `getsnapsbytype`/`getsnapsbypath`, and never saw a damaged creation time; the config-merge harness ran with `--debug` off; and the dataset-cache harness gave upstream the full zfs path pysanoid printed. Widened, the dump harness differs in all 12 of its hand cases and 300 of 300 fuzzed scenarios on `0.1.35`, the merge harness in 14 of 15 hand cases and 200 of 200 fuzzed configs, and the dataset-cache harness in 3 of 32 hand cases and 70 of 300 fuzzed scenarios | 0 of several hundred (hand + fuzz, `dev/diff_debug_dump.py`); at `0.1.36`, the whole dump byte for byte in 12 hand + 1,200 fuzzed scenarios across two seeds, and the merge trace, the warnings in order and the dump of the real `%config` in 15 hand + 1,200 fuzzed configs across three seeds, nothing unexplained |
| 7 — hooks (`runscript`/`run_hook_script`) | *(2026-09-22, post-audit)* several `script_timeout` spellings (` 5`, `+5`, blank) silently enforced a timeout upstream never would have -- found building the fuzz target itself, not by the original audit. *(2026-09-25, second-pass re-audit, unit 7)* The harness compared a timeout only as on or off, and every hook command it ran had no shell metacharacter, so Perl and the shell split it alike; reading back the timeout Perl's `alarm()` sets, and running command lines with comments, quotes and builtins, 21 of its 37 hand cases and 393 of 500 fuzzed scenarios differ on `0.1.33`. *(2026-10-11, `0.1.45`)* It compared a failed hook's warning only as there or not, and counted a command Perl can't exec as a known class, upstream's -1 against `/bin/sh`'s 127. Comparing the line word for word, and with that class a difference like any other, 13 of 45 hand cases and 307 of 400 fuzzed scenarios differ on `0.1.44`; in the take and prune harnesses, which now compare the line too, 6 of 65 and 2 of 51 hand cases, and 35 of 300 and 10 of 400 fuzzed scenarios | 0 of 618 (18 hand + 600 fuzz, `dev/diff_hooks.py`); at `0.1.34`, 37 hand (3 in the documented can't-start class) + 1,200 fuzzed across three seeds, nothing unexplained; at `0.1.45`, 45 hand + 1,200 fuzzed across three seeds, the can't-start class gone and nothing unexplained under Python 3.14 and 3.11 |
| 1 — `prune_snapshots` | *(2026-09-22, post-audit)* n/a -- clean from the first complete run. *(2026-09-24, second-pass re-audit)* four divergences the harness couldn't reach, all confirmed end to end against the real upstream script; its widened fuzz finds 97 of 600 on `0.1.22`. Then two in `prune_defer` (unit 1b); the fuzz, now trying `00` and a missing key, finds 51 of 600 on `0.1.23`. *(Unit 7)* It never made a destroy fail to start; its 3 new hand cases and 12 of 600 fuzzed scenarios differ on `0.1.33`. *(2026-09-27)* It never took away the run directory, the class unit 7 gave the cache harnesses; added as a known class that checks pysanoid against itself, it fails two deliberately broken versions of pysanoid's prune (one pruning unlocked, as upstream does, one skipping silently) in all 4 of its hand cases. *(2026-10-10, `0.1.41`)* No count in it was longer than a digit; with counts of 19, 400 and 5,000 digits, pysanoid's prune ends in a traceback in 4 of 10 new hand cases and 9 of 400 fuzzed scenarios on `0.1.40`, where upstream prunes none of that type and carries on. *(2026-10-10, `0.1.42`)* Its fake zfs never printed a byte that isn't UTF-8; with a warning in such bytes on every destroy, 3 of 4 new hand cases and 10 of 400 fuzzed scenarios differ on `0.1.41`, which took each destroy that worked for one that could not run: nothing recorded as pruned, no `pruning_script` run, nothing flushed from the cache. *(2026-10-11, `0.1.43`)* It compared what was destroyed, never what reached stderr. Comparing zfs's own words and the `could not remove` lines there, 8 of 51 hand cases and 20 of 400 fuzzed scenarios differ on `0.1.42`: a warning from a destroy that worked was not shown, and a destroy that failed or couldn't start was reported in pysanoid's words | 0 of 1,316 (16 hand + 1,300 fuzz across two seeds, `dev/diff_prune.py`); at `0.1.23`, 0 of 1,223 (23 hand + 1,200 fuzz across two seeds); at `0.1.24`, 0 of 1,228 (28 hand + 1,200 fuzz across two seeds); at `0.1.34`, 0 of 631 (31 hand + 600 fuzz); at `0.1.39`, 0 of 437 (37 hand + 400 fuzz, 4 and 11 of them in the new class); at `0.1.41`, 0 of 1,247 (47 hand + 1,200 fuzz across three seeds, 74 of them with a count too long for a number); at `0.1.42`, 0 of 1,251 (51 hand + 1,200 fuzz across three seeds, 107 of them with zfs printing a byte that isn't UTF-8); at `0.1.43`, the same 1,251 with stderr compared as well, 0 differing (in 270 of the fuzzed scenarios zfs warns on every destroy, a destroy fails with a complaint, or one can't start) |
| 6 — `take_snapshots` | *(2026-09-22, post-audit)* `post_snapshot_script`'s `SANOID_SNAPNAME` was missing upstream's `dataset@` prefix -- found building the fuzz target itself. *(2026-09-24)* It also missed upstream's `dst_` rename: 2 of 3 new hand cases on a real fall-back day diverged. *(2026-09-24, `0.1.23`)* A hook set to `0` ran as a command, and with `no_inconsistent_snapshot` the dataset was skipped; a new hand case and the widened fuzz (3 of 600 on `0.1.22`) cover it. *(2026-09-24, `0.1.25`)* The take fuzz avoids DST changes by design; a brute-force sweep (`dev/sweep_dst.py`) found pysanoid's preferred time wrong at 33,856 of 713,664 (instant, type) points on `0.1.24`, and three new hand cases differ there. *(2026-09-24, `0.1.26`)* A process that couldn't start ended the whole take run, a snapshot held back by a failed pre hook was silent, and `--readonly` hid the hook lines; the harness never made a call fail or compared stdout. Its new cases and widened fuzz differ on `0.1.25` (5 hand, 30 of 600 fuzz). *(2026-09-25, `0.1.27`)* Take refused a malformed count or preferred time (`daily = 3o`) that upstream reads and takes; its new cases and malformed values in the fuzz differ on `0.1.26` (4 hand, 74 of 500 fuzz). *(2026-09-25, `0.1.28`)* A blank preferred time read as the default, not upstream's 0; two new timing hand cases differ on `0.1.27`. *(2026-10-10, `0.1.41`)* No snapshot in it was dated ahead of the clock, and no count was longer than two digits. With a newest snapshot dated ahead, pysanoid matches upstream (neither takes the type), which is to stay so; with counts of 19 to 5,000 digits, 1 of 10 new hand cases and 4 of 300 fuzzed scenarios differ on `0.1.40`, which took none of a type upstream takes. *(2026-10-10, `0.1.42`)* Its clock stood still, it handed pysanoid that clock, and all but a few hand cases had one dataset, so the time in a name could not differ. Now the clock moves for every zfs command and hook that has run, a scenario has up to three datasets, and pysanoid runs as a real run does: 5 of 8 new hand cases differ on `0.1.41`, whose names all carry the time the run began. With zfs printing a byte that isn't UTF-8, 3 of 4 more differ (a snapshot that worked not counted as taken, a name already taken not retried as `dst_`), and so do 101 of 300 fuzzed scenarios. *(2026-10-11, `0.1.43`)* It compared stdout, never stderr, and its stand-in for upstream's `tee_stderr` kept what it captured to itself. With that passed on as the real one passes it, and zfs's own words and the CRITICAL ERROR lines compared, 12 of 65 hand cases and 28 of 300 fuzzed scenarios differ on `0.1.42`: a warning from a snapshot that worked was not shown, and a snapshot that failed or couldn't start was reported in pysanoid's words, zfs's complaint inside the line | 0 of 1,118 (18 hand + 1,100 fuzz across two seeds, `dev/diff_take_snapshots.py`); at `0.1.22`, 0 of 621 (21 hand + 600 fuzz); at `0.1.23`, 0 of 1,022 (22 hand + 1,000 fuzz across two seeds); at `0.1.25`, 0 of 1,028 (28 hand + 1,000 fuzz across two seeds), and the sweep is right at all 713,664 points (`check`) and 4,930,848 (`wide`); at `0.1.26`, 0 of 1,033 (33 hand + 1,000 fuzz across two seeds); at `0.1.27`, 0 of 1,040 (40 hand, 4 of them documented divergences, + 1,000 fuzz across two seeds); at `0.1.28`, 0 of 1,543 (43 hand, 5 documented, + 1,500 fuzz across three seeds, with blank preferred times and more snapshot ages); at `0.1.41`, 0 of 953 (53 hand, 5 documented, + 900 fuzz across three seeds, 201 of them with a newest snapshot dated ahead of the clock and 50 with a count too long for a number); at `0.1.42`, 0 of 965 (65 hand, 5 documented, + 900 fuzz across three seeds, 278 of them with two or three datasets on a moving clock and 63 with zfs printing a byte that isn't UTF-8), each side's names checked against upstream's rule before the two are compared; at `0.1.43`, the same 965 with stderr compared as well, 0 differing (in 212 of the fuzzed scenarios zfs warns on every snapshot, a snapshot fails with a complaint, or one can't start) |
| `prune_defer`'s capacity answer (`get_zpool_capacity`) | *(2026-10-11, `0.1.43`)* The prune harness stubs the capacity query on both sides, so nothing had compared the reading itself. A new target gives upstream's sub and pysanoid's the same answer from a fake `zpool`, byte for byte: 26 of 37 hand cases and 582 of 600 fuzzed answers differ on `0.1.42`. Most are a refusal in other words, or zpool's stderr folded into it or not shown. In 7 hand cases the two read the answer differently: pysanoid pruned by an answer upstream refuses (a leading space or tab, a trailing tab, a carriage return, a form feed or a no-break space after it, a blank line before it) | 0 unexplained at `0.1.43`: 37 hand cases (6 in the one known class, an answer holding more than a capacity) + 1,800 fuzzed answers across three seeds (151 a capacity alone, read alike; 1,583 refused by both in the same words; 66 in the known class). The known class is checked against the rule as the harness states it, both ways (`dev/diff_zpool_capacity.py`) |
| take's reading of a malformed number (`_perl_number`, new in `0.1.27`) | — | 0 of 120,159 against perl 5.34's own `0 + $string` (159 hand + 120,000 fuzzed strings across six seeds, `dev/diff_perl_number.py`) |

The Module 4 number is worth sitting with: **65% of random scenarios showed some
divergence before that fix**, and the actual mechanism — the dataset cache was loaded
lazily instead of eagerly, so any config with *no recursion at all* rewrote a perfectly
valid cache on every single run — is exactly the production config's shape. Production `sanoid.conf`
has 16 sections, zero recursion, zero hooks. That bug was firing on every real run before
it was found, not a theoretical edge case. It cost extra I/O and a misleading log line,
not data — but it's the clearest example in this whole audit of why the differential
layer mattered more than reading the code alone would have.

## What was actually wrong, and got fixed

These are not deviations — they're places the port had *accidentally* drifted from
upstream, now closed. This is most of the real value of the audit:

- **(pre-audit, 0.1.9)** A blank `prune_defer` silently disabled the pool-capacity guard
  instead of failing. The very first fix in the audited series.
- **Module 2:** the recursion/template merge was order-dependent — a child dataset's own
  `process_children_only` or a nested `recursive = zfs` could be silently overwritten
  depending on section order in the config file.
- **Module 3:** `add_cached_snapshots` built a cache from only the new lines when the real
  cache was missing, with a fresh mtime — every other snapshot type then looked overdue
  for a full TTL, got re-taken, and the extras ate retention slots. Byte-exactness bugs
  (text-mode capture silently translating CR/CRLF). Snapshot-name regexes anchored where
  upstream's are not.
- **Module 4:** the dataset cache load was lazy instead of eager (see above) — the
  single highest-impact bug found, since it was live on every production run.
- **Module 5:** `--debug` didn't imply `--verbose`; a non-UTF-8 byte in a config path
  crashed the whole run under a strict locale (silently fine under systemd, which sets no
  `LANG` — so this was latent, not yet observed); an empty `--cache-dir=` silently became
  the current directory; `--cache-ttl` used Python's `int()` grammar instead of
  Getopt::Long's.
- **Module 7:** a malformed *or merely negative* `script_timeout` could crash
  `take_snapshots` entirely — killing protection for every *other* dataset in that run —
  or crash `prune_snapshots` mid-loop, *after* real `zfs destroy` calls had already run
  for earlier sections. This is the most safety-critical bug found in the whole audit: it
  directly touched prune's fail-closed guarantee.
- **Module 8:** cosmetic-only text bugs in the `--debug` dump (a literal "None" printed
  instead of blank text; the blank-line count no longer matched upstream's own, admittedly
  odd, output).
- **2026-09-22, post-audit (building `dev/`):** two more real bugs, found not by re-reading
  the code but by the act of building a mass-fuzz differential for it. Both fixed, tested,
  registered, and covered by the fuzz target that found them:
  - `run_hook_script`'s `script_timeout` gate was inferring "should this enable a timeout"
    from whether `_cfg_float` raised or was negative — an approximation that got several
    real spellings backwards. `script_timeout = " 5"`, `"+5"`, or blank all string-compare
    *below* `"0"` in upstream's actual `$timeout gt 0` gate (a leading space or `+` sorts
    below a digit), so upstream never arms a timeout for them — pysanoid's old code parsed
    all three as a clean, positive `5.0` and silently enforced one anyway. Fixed by
    deciding enforcement from the same string comparison upstream uses, not a numeric
    approximation of it.
  - `take_snapshots`' `post_snapshot_script` call was giving `SANOID_SNAPNAME` the bare
    snapshot name, matching what `pre_snapshot_script` gets — but upstream's own two hooks
    actually receive *different* formats: a `foreach my $snap (@snapshots) { $snap =
    "$dataset\@$snap"; ... }` loop, which runs between the two hook calls and actually
    takes the snapshots, aliases and mutates the same array both hooks read `$snapshots[0]`
    from. `pre_snapshot_script` (before the loop) sees the bare name; `post_snapshot_script`
    (after) sees it prefixed with `dataset@`. An accidental Perl aliasing side effect, not a
    documented feature — but real, confirmed behavior an existing `post_snapshot_script`
    could depend on. Fixed to match exactly (no safety angle either way, purely a
    third-party-hook-script compatibility question).

- **2026-09-24 (`0.1.22`):** when the first snapshot needs the `dst_` retry, upstream renames
  that same aliased array element *before* retrying, so `post_snapshot_script`'s
  `SANOID_SNAPNAME` carries the `dst_` name, even if the retry then fails. pysanoid passed the
  original name. Found while checking a comment against upstream's code; the fuzz had missed
  it because upstream only takes that path on a real fall-back day, where its `$handleDst`
  gate is armed. Three new hand cases on 2026-11-01 now pin it against real Perl. Fixed to
  match; a hook-compatibility question only, with no safety angle.

- **2026-09-24 (`0.1.23`, second-pass re-audit, unit 1a `prune_snapshots`):** four divergences,
  each proven against real Perl and then end to end (the real upstream `sanoid` script with the
  real `Config::IniFiles` against the real `pysanoid.py`, on the same config files):
  - **A data-loss path inside a documented deviation.** pysanoid took `pre_pruning_script` from
    the pruning section's own config, upstream from `$config{$dataset}`. With two sections on one
    dataset (for example a recursive parent's veto hook inherited by `tank/a`, plus a custom
    `[b] path = tank/a`), upstream vetoed every prune and pysanoid destroyed them. The veto now
    comes from both places, and `pruning_script` from upstream's.
  - **A hook set to `0`**, which Perl treats as unset, ran as a command (exit 127): every prune
    of that dataset was vetoed, and in take, with `no_inconsistent_snapshot`, the dataset was
    skipped on every run.
  - **Ages measured with a fractional clock**: a snapshot exactly on its age boundary was pruned
    one run early (the keep-at-least-N floor still held). Upstream's `time()` is whole seconds.
  - **The closing cache flush redid the whole run**: a second "removing destroyed snapshots"
    line on every verbose prune, and a wait for the cache lock with nothing left to flush.
  `dev/diff_prune.py` couldn't see any of these: its hooks were always the probe script, its
  clock a whole second, its scenarios one section each, and its closing flush stubbed out. It
  covers all four now.

- **2026-09-24 (`0.1.24`, second-pass re-audit, unit 1b `prune_defer`):** two divergences,
  confirmed end to end against the real upstream script with a fake `zpool`:
  - **`prune_defer = 00`**: upstream's early return is `$limit eq 0`, a string comparison, so
    `00` or `000` still asks the pool: pruning defers on an empty pool, and the run fails when
    the section name doesn't start with a pool. pysanoid read it as 0 and pruned without asking.
  - **A missing `prune_defer`** (only a `sanoid.defaults.conf` without the key does this):
    upstream dies on every prune run; pysanoid treated it as 0. It is now fatal, like a blank.
  The harness's capacity stub now fails for an unknown pool and logs every query, and its fuzz
  tries `00`, `000`, `080` and a missing key.

- **2026-09-24 (`0.1.25`, second-pass re-audit, unit 6a take planning):** the take fuzz avoids
  DST changes by design, so a new sweep (`dev/sweep_dst.py`) compares pysanoid's preferred times
  with a brute-force answer at every instant around each change, and runs upstream's real code
  alongside. It found, all confirmed against real Perl or the brute force:
  - **Hourly snapshots near a DST change, with `hourly_min` above 0.** Stepping back a
    wall-clock hour landed in the skipped hour after a spring change (an extra hourly on each
    run until the minute came round) or on the wrong pass through the repeated hour in autumn.
    The wide sweep then showed that only a real-minute scan is exact where just part of an
    hour moves (Australia/Lord_Howe's 30-minute change), so hourly and frequent times now scan.
  - **False errors for a monthly on the 29th to 31st** (and a yearly on 29 February): looking
    back into a month without that day raised, so every run until the day came round logged an
    ERROR and exited non-zero, though nothing was due (450 of 1,096 days in 2026-2028 for the
    31st).
  - **`int()` read `1_5` as 15** (Perl: 1) in every schedule setting, silently moving
    snapshots. It is now refused, as retention counts already were.
  - **`weekly_wday` outside 0 to 6 was read wrongly.** From 14 up pysanoid's own time could be
    in the future, so it took a weekly on every run for part or all of each week; from -1 to -7
    the weekly came on Sunday at midnight instead of the chosen day, and below -7 only every
    second or third week. It is now read modulo 7, which matches upstream from -7 to 6 (checked
    against real Perl for -22 to 21).
  - **A negative `frequent_period` was refused**, where upstream's arithmetic slices it like its
    size; take now does the same.
  Upstream's DST arithmetic, which pysanoid deliberately doesn't follow, was re-justified to the
  "dangerous for the data" standard: after the autumn change it takes no frequent snapshots for
  the whole repeated hour in zones such as New York and Sydney (0 of 4 runs; pysanoid 4 of 4),
  and with `weekly_wday` of 7 or more it takes a weekly on every run, which can fill the pool.
  From -15 down it takes one only every second week or less often, losing restore points. From
  -8 to -14 it takes one a week at the start of Sunday rather than on the chosen day: harmless,
  and different in pysanoid only because the modulo reading covers every value.
  0.1.25 matches the brute-force answer at all 4,930,848 checked (instant, type) points (six
  zones, 2026-2030); 0.1.24 failed at 33,856 of the smaller check's 713,664.

- **2026-09-24 (`0.1.26`, second-pass re-audit, unit 6b take execution):** confirmed end to end
  against the real upstream script, with one simulated fork failure in each program:
  - **A process that couldn't start ended the whole take run.** When fork fails, upstream's
    `system()` returns -1, so it warns and goes on. pysanoid's error went uncaught: every later
    dataset went unsnapshotted, what the run had already taken never reached the cache (so the
    next run took it again), `--cron` skipped pruning, and a hook that couldn't start ended in a
    traceback. Now only that snapshot or hook fails, as upstream's does, and a prune hook that
    can't start vetoes, as upstream's does.
  - **A snapshot held back by a failed pre hook was silent.** With `no_inconsistent_snapshot`,
    both upstream and pysanoid skipped the dataset with a WARN and exit 0, so a hook that always
    fails stopped its snapshots for good. It is now a take problem, an ERROR and a non-zero exit,
    like a failed `zfs snapshot` (a documented deviation).
  - **`--readonly --verbose` hid the hooks.** Upstream still prints its "executing ..." lines,
    before its readonly checks; pysanoid now does too.
  The take harness could see none of these: it never made a call fail to start and never
  compared stdout. It now does both, and its new cases and widened fuzz differ on 0.1.25 (5 hand
  cases, 30 of 600 fuzz scenarios).

- **2026-09-25 (`0.1.27`, second-pass re-audit, unit 2a config loading):** confirmed end to end
  against the real upstream script and fuzzed through upstream's own `init` on the real
  `Config::IniFiles`:
  - **Take refused a malformed number that upstream reads.** `daily = 3o`, `daily = 30 days`,
    `hourly_min = 7.5` or `daily_hour = 1_1` cost that snapshot type on every dataset using the
    setting, until someone read the journal; upstream reads them as 3, 30, 7.5 and 1 and takes the
    snapshots. Taking destroys nothing, so the refusal had no data-safety reason. Take now reads such
    a value as upstream does (a port of Perl's own number reading, fuzzed against perl 5.34) and still
    reports it, exiting non-zero; a preferred time drops its fraction. Prune still refuses it before
    its first destroy: upstream's reading of `3o` typed for 30 destroyed 7 of 10 dailies in the probe.
  - **A huge preferred-time value crashed take planning.** `daily_hour = 99999999999999999999`
    raised OverflowError out of the planner, so no dataset was snapshotted; it now costs only its
    type, like any bad value.
  - **An empty here-doc `path` read as "".** Upstream uses the section name; every snapshot of the
    dataset failed here.
  - **Documented, no code change:** upstream trims a section's values only when its loop reaches
    that section, and never trims sanoid.defaults.conf, so a template below the sections using it
    (the usual layout, the production config's too) reaches them untrimmed. `autoprune = no ` with a trailing space
    then prunes (7 of 10 dailies destroyed), `process_children_only = no ` takes nothing, and
    `prune_defer = 80 ` stops the prune run. pysanoid has always read these trimmed; the register
    now says so, with the two harmless side effects.
  The config-merge harness could see none of the untrimmed cases: its stand-in for
  `Config::IniFiles` trimmed every value. It now runs on the real module.

- **2026-09-25 (`0.1.28`, the last unit 2a finding):** a blank (or comment-only) preferred-time
  setting read as the shipped default, where upstream reads 0: `daily_hour =` took the daily at
  23:59, upstream at 00:59, and a blank `monthly_mday`, `yearly_mon` or `yearly_mday` makes
  upstream die for every dataset. Take now reads it as 0 and reports it, like a malformed value,
  so a blank day or month costs only its type. Confirmed end to end: with `monthly_mday =`
  upstream exits 255 having taken nothing, and pysanoid takes the daily and reports the monthly.

- **2026-09-25 (`0.1.29`, second-pass re-audit, unit 2b ini grammar):** `Config::IniFiles` strips
  a byte-order mark from the first line with a pattern written as UTF-8 in a module Perl reads as
  Latin-1, so what it strips is the BOM encoded twice (bytes `C3 AF C2 BB C2 BF`, what a Latin-1
  round trip makes of a UTF-8 BOM), never a real one. Upstream loaded a sanoid.conf that starts
  with it; pysanoid refused the file, so nothing was snapshotted or pruned. Confirmed end to end:
  upstream took the daily and destroyed the 7 due, pysanoid did neither. It now strips it too; a
  real BOM is still refused, as upstream refuses it. Checked against releases 2.94, 3.000003 and
  3.003000. The original audit's one-off grammar fuzz never generated this input and wasn't kept;
  the grammar now has its own target, `dev/diff_ini_grammar.py`.
  Recorded, no code change: when a file's only line ending is a final lone CR, upstream reads that
  CR again as one more, empty line and prints `Use of uninitialized value $nextchar in string eq`
  on every run. The parse is the same, so pysanoid neither re-reads it nor warns.

- **2026-09-25 (`0.1.30`, second-pass re-audit, unit 3a snapshot listing):** confirmed end to end
  against the real upstream script:
  - **A cache that couldn't be written ended the run.** On a full disk (a write that fails after
    the file opened), upstream carries on with the listing it just read: it took the due daily and
    destroyed the 597 due, leaving a truncated cache. pysanoid died at the write: nothing taken,
    nothing pruned, on every run until space was freed. Upstream itself dies when it can't create
    the file (a read-only disk). Every cache write (the listing, take's add, prune's remove,
    `datasets.txt`) now reports the failure, removes the stale file so the next run reads zfs,
    and carries on, exiting non-zero.
  - **An unreadable cache, or one that isn't a regular file, ended the run** (a named pipe would
    have hung it). Upstream reads it as no snapshots: it took another daily of every dataset, then
    died. It now counts as expired, and zfs is read; so is a missing cache while another process
    holds the update lock. A missing cache is left missing, where upstream's prune writes an
    empty, fresh one that hides every snapshot until it expires, and its add dies.
  - **A NUL byte in the config crashed pysanoid.** Upstream loads it and every command it reaches
    is cut short there: its listing dropped the datasets sorted after that section (so take
    re-took them), and a recursive section snapshotted another dataset's children. It is now
    refused at load.
  - **Parity details:** the cache age is whole seconds, the listing's `\d` and `\w` are ASCII, and
    `--verbose --debug` prints upstream's `INFO: running: zfs get ...` line.
  - **Registered, no change:** a `zfs get` killed by a signal is retried without dataset names;
    upstream's `$? >> 8` reads it as success and keeps the partial listing (it took new dailies
    of the two datasets it missed).
  The listing harness never compared stdout and never made a cache or `zfs get` fail; it now does
  both, with the deviations above as documented classes.

- **2026-09-25 (`0.1.31`, second-pass re-audit, unit 3b, the per-type and per-path tables):**
  confirmed against real Perl:
  - **A snapshot's name could switch off a snapshot type.** Upstream's listing pattern allows no tab
    before "creation", so both programs read a legal name such as
    `autosnap_dailycreation9999999999_x` as a daily named `autosnap_daily` made in 2286. End to end,
    identically in both, that dataset's dailies stopped for as long as the snapshot existed, and
    prune kept one fewer. Anyone allowed to create a snapshot there can do this, even with only the
    delegated `snapshot` permission. A name now ends at the tab `zfs get -H` prints.
  - **A damaged cache line ended the run.** A creation time too long for a float made the age
    overflow, and one past 4,300 digits made parsing fail: nothing taken or pruned until the cache
    expired. Upstream reads it as a float (infinity past the range) and carries on; only that
    dataset's type waits for the next listing. It is now read the same way.
  - **Parity details:** ages are whole seconds, as upstream's `time` gives. A fractional age took a
    snapshot one run earlier than upstream when the clock ticked over a second between building the
    table and planning, and printed some `--debug` ages differently (540 s as 0.2 hours, upstream
    0.1).
  - **Registered, no change:** snapshots made in the same second are pruned in the order zfs lists
    them, which is creation order; upstream's order among them is random per process (with two
    such dailies it destroyed the first 4 times and the second 8 times in 12 runs).
  The listing harness built the tables on a whole-second clock, left out the sorted list prune
  reads, and generated one type with no same-second snapshots; it now builds them as `main()`
  does, compares the sorted list with same-second snapshots as a set, and compares every creation
  time.

- **2026-09-25 (`0.1.32`, second-pass re-audit, unit 3c, the cache add and remove):** confirmed
  against real Perl:
  - **A child lookup that couldn't start ended the run.** For a `recursive = zfs` snapshot the cache
    add looks up the children; when `zfs list` couldn't start (a fork failure), pysanoid went FATAL.
    End to end under `--cron`, nothing was pruned, and the cache kept none of the run's snapshots,
    so the next run took the whole tree again. Upstream's unchecked pipe finds no children, caches
    the parent's line and carries on; pysanoid now does the same and reports the failure.
  - **Parity details:** the remove writes each kept line back exactly as read. pysanoid added a
    missing final newline, and dropped a line without a tab that spelled a pruned name, which
    upstream's unchomped `split` never matches. The lock-wait line now uses upstream's words.
  - **Register corrected, no code change:** it said upstream's re-read of the snapshot list after a
    prune flush can't change a decision. It can: a cache add records a snapshot's creation as the
    time `zfs snapshot` returned, zfs's own or later, so once the cache has expired the re-read lets
    upstream destroy a snapshot on its age boundary a run sooner (6 of 12 hash orders; never in the
    two controls). Take's closing re-read does the same under `--cron`. pysanoid keeps such a
    snapshot for that run and never destroys sooner.
  - **Found, changed in `0.1.34`:** a `zfs destroy` that can't start ends the prune run (FATAL) where
    upstream warns "could not remove" and carries on, and the snapshots that batch already destroyed
    stay in the cache until it is next listed (confirmed end to end).
  The add/remove harness stubbed the child lookup, never compared stdout, and counted the remove's
  newline repair as a documented class the register didn't have. It now runs the real lookup,
  compares stdout and the lookups, and checks the re-read claim on every hand run.

- **2026-09-25 (`0.1.33`, second-pass re-audit, unit 4, the dataset cache):** confirmed end to end
  against the real upstream script:
  - **A child lookup that came back short let prune use the defaults.** A child with a section of its
    own inherits its recursive parent's settings only through the recursion; when the load-time
    `zfs list` misses it, the child is set up from the defaults. With the parent's `hourly = 100`
    and the default 48, a lookup killed after printing the parent's line (as the OOM killer would)
    made both programs destroy 12 of the child's 60 hourlies. Upstream does the same when the
    lookup can't start at all; pysanoid already stopped there, and the register now says why. A
    lookup that is killed or fails after listing anything now stops the run too; one that fails
    having listed nothing still reads as no children (its parent isn't there).
  - **A damaged datasets.txt stopped the run.** A NUL byte (a crash can leave them in a file just
    written) made pysanoid stop FATAL on every run until the file expired. Upstream uses the file and
    cuts its snapshot listing short at the NUL: it took an extra daily for a dataset sorted after it
    and never pruned that dataset's 3 due dailies. A datasets.txt holding a NUL now counts as no
    cache: the children come from zfs and the file is rewritten.
  - **Parity details:** `--debug` prints upstream's two dataset-cache lines, the lock-deferral line
    has upstream's words, and three loading warnings lose the `WARN: ` prefix upstream doesn't print.
  - **Checked, no change:** a pool that isn't there yet is cached as having no children, in both, so
    its children without a section of their own aren't snapshotted until the file expires; a child
    with a section of its own keeps inheriting, since its path is in the file.
  The harness never compared stdout, never ran `--debug`, and never broke a lookup or datasets.txt.
  It now does all four, with the two deviations as known classes, and checks that a NUL file makes
  pysanoid behave exactly as with no file.

- **2026-09-25 (`0.1.34`, second-pass re-audit, unit 7, hooks and locks):** confirmed end to end
  against the real upstream script and real Perl:
  - **A lock file that couldn't be made stopped every snapshot.** With a run directory no lock file
    could be made in (tested unwritable; a read-only or full `/run` fails the same way), the first
    lock ended the run with a traceback: the snapshots just taken were never cached, so each run took
    them again, and once a cache expired no run took any, and nothing was pruned. Upstream carries on
    unlocked and dies releasing the lock, after its update, so it keeps taking snapshots. A lock that
    can't be taken is now reported and only what it guards is skipped: the listing is read from zfs
    without updating the cache, a cache that can't be updated is removed, the dataset cache isn't
    rewritten, and prune skips that batch. A run or cache directory that can't be made no longer
    stops the run at startup, where upstream's `make_path` dies.
  - **A `zfs destroy` that couldn't start ended the prune run** (found in unit 3c): it is now
    `could not remove`, as upstream's `system()` returning -1 makes it, and the run goes on, flushing
    what it destroyed. Like any failed destroy, it keeps the snapshot and isn't a problem for the exit
    status.
  - **A NUL in snapshots.txt:** upstream pruned a damaged line under the name before the NUL and
    destroyed a dataset's newest daily, leaving 2 of the 3 its retention keeps; pysanoid stopped
    FATAL at that line. A snapshots.txt holding a NUL now counts as no cache, as datasets.txt does.
  - **`script_timeout`:** 24 of 45 values set a different timeout from upstream's (`5s` and `1_0` ran
    untimed, `1.5` and `0.5` were enforced to the fraction). It now sets exactly what upstream's
    `alarm()` sets, and a value that isn't a plain whole number is reported. The sub-second
    enforcement, kept as "more capable", made a hook that upstream runs to completion fail on every
    run, holding back its snapshots with `no_inconsistent_snapshot`.
  - **Hook command lines:** Perl's `system()` runs a command with no shell metacharacter without the
    shell, so a trailing `# comment` reaches the hook as arguments and `exit 0` can't start; pysanoid
    ran every hook through `/bin/sh -c`. It now splits and runs them as Perl does.
  - **Output order:** stdout was block-buffered under systemd, so its lines reached the journal at
    exit, after everything hooks and zfs had printed; it is now line-buffered (Perl flushes before
    every command it starts).
  - **Register only:** Ctrl-C stops pysanoid, where upstream's `system()` ignores it and carries on
    destroying; while the system is out of processes, upstream retries each command every 5 s, which
    can hang a run silently, where pysanoid fails that command; a timed-out hook's own process is
    killed, not what it started.
  The hooks harness now reads back the timeout Perl's `alarm()` sets and runs command lines with
  comments, quotes and builtins; the prune harness makes a destroy fail to start; and the three cache
  harnesses gained a run directory with no lock file (and the listing a NUL cache) as known classes
  that check pysanoid against itself.

  **Assessment of this unit.** Much of what upstream does here is itself a failure: it updates the
  cache unlocked and dies releasing the lock, its timeout kills sanoid itself, it hangs while the
  system is out of processes, and it ignores Ctrl-C while a command runs. So `0.1.34` matches
  upstream's decisions (which timeout, which arguments, which destroys, what is cached) and departs
  only from those failure modes, each in the register.
  - **Closer to upstream:** yes, wherever a config decides the outcome. `script_timeout` values that
    set a different timeout: 24 of 45 on `0.1.33`, none on `0.1.34` apart from two that only macOS
    refuses; hook command lines split differently: 7 of 24, none. A destroy that can't start now
    ends as upstream's did end to end (same destroys, same cache, exit 0), and journal order matches
    upstream at every command it starts. Four silent differences went (every hook through `sh -c`,
    block-buffered stdout, the FATAL destroy, and 5 s for a missing `script_timeout`), and so did the
    documented "more capable" sub-second timeout. Two changes move further from upstream on purpose:
    a lock file that can't be made (the guarded step is skipped, where upstream does it unlocked and
    then dies) and a NUL in snapshots.txt (ignored, where upstream reads it). A malformed
    `script_timeout` now fails the unit, where upstream only warns.
  - **Safer:** yes. Nothing in this release adds a way to destroy a snapshot the retention settings
    keep; the gains are above. The costs, each where pysanoid now matches upstream, or fails closed,
    where `0.1.33` was more forgiving: with an unusable run directory prune destroys nothing and
    reports it every run (`0.1.33` pruned nothing either; upstream prunes one batch a run, unlocked),
    so the pool fills if nobody acts on the alerts; out of processes, a command fails at once where
    upstream waits, so a brief spike costs that run's snapshots; `script_timeout = 0.5` now sets no
    timeout, as upstream, so a hung hook stalls the run where `0.1.33` killed it (an ERROR line says
    so first); and a hook that is a shell builtin (`:`, `exit 0`) can't start, as upstream, so as a
    `pre_pruning_script` it vetoes every destroy of its dataset, with only a warning. The production host has no
    hooks, so only the first two can reach it.
  - **Correctness:** close. The two ports are exact against real Perl over thousands of fuzzed
    inputs, and every behavior change fails on `0.1.33`. After this review, the lock helpers yield
    `(held, error)` rather than True, False or an `OSError` (an exception object reads as true), so a
    caller can't take a failure for a held lock. Open: a timed-out hook's own process is killed, not
    what it started; a bounded retry while out of processes (every 5 s for up to a minute) would
    recover brief spikes without upstream's silent hang; with an unusable run directory every run
    lists zfs without the lock (upstream issue #202's one-lister question, settled by the history
    pass: no change, see `0.1.37`); and the prune harness doesn't simulate the lock-file failure
    (unit tests and the end-to-end probe do).

- **2026-09-26 (`0.1.35`, second-pass re-audit, unit 5, options and main flow):** confirmed end to
  end against the real upstream script. Option parsing and the take/prune dispatch already matched
  (both harnesses clean on `0.1.34`); what differed sits where they don't look:
  - **Output that couldn't be written stopped the run.** Perl's `print` and `warn` don't fail;
    Python's `print` raises. With stdout and stderr a closed pipe and SIGPIPE ignored (systemd's
    default), or appended to a log that can't grow (a full disk), upstream took and pruned as usual
    and exited 1; pysanoid died at its first line, nothing taken or pruned, exit 120 -- the production
    unit command lines included, and a run whose only output is the `--force-prune` warning.
    `0.1.34`'s line buffering had moved this to the first line (`0.1.26` failed only at exit, or
    when its output buffer first filled). Under a latin-1 locale, a section name latin-1 can't spell raised mid-take, and
    the datasets after it got no snapshot. stdout and stderr now write as Perl's do: a line that
    can't be written is dropped, the bytes go out as read whatever the locale, and a lost stdout is
    reported and exits 1, as upstream's failed last flush does. When a pipeline's reader goes away
    with SIGPIPE at its default, upstream is killed mid-run; pysanoid can't tell that case from
    systemd's and carries on, which does only what upstream's next run would (checked end to end:
    the snapshot a killed run leaves in the cache is its oldest, retried harmlessly).
  - **Production runs Python 3.11; every gate had run on 3.14.** Through 3.13, pathlib's `exists()` and
    `is_file()` raise PermissionError where 3.14 returns False. On 3.9 (the same pathlib code) and
    on 3.11, end to end, a cache directory pysanoid can't search stopped the run with a traceback,
    nothing taken, where 3.14 carried on as the register says. Every file check now uses `os.path`,
    which reads false on every version. The tests and the whole `dev/` gate now run on 3.11 too;
    `0.1.34` was otherwise clean there (all 26 runs, 26 end-to-end checks).
  - **A `timedatectl` that doesn't answer stopped take.** The timezone lookup gives it 5 s but caught
    only OSError, and the timeout isn't one: take never started, and `--cron` didn't prune either.
    Reached only with TZ unset and /etc/localtime not a zoneinfo symlink.
  - **`$TZ` in the C library's other forms** (`:America/New_York`, `:/etc/localtime`, a zone file's
    path) was ignored with a warning and the system's zone used (5 h off in the check); they are now
    read as the C library reads them. A POSIX rule (`IST-1GMT0,M10.5.0,M3.5.0/1`) still can't be:
    zoneinfo reads zones, not rules (registered).
  - **Parity details:** the `--force-prune` warning has upstream's word "argument", and a negative
    `--cache-ttl` prints upstream's `ERROR: cache-ttl needs to be positive!`.
  - **Register only:** `--cache-ttl=0` now gives its reason (with it, upstream silently used a cache
    that still listed a daily destroyed outside sanoid, and destroyed one of the three `daily = 3`
    keeps); a `--cache-dir` or `--run-dir` starting with whitespace makes upstream exit 2 on every run
    (Perl's two-argument `open()` drops the whitespace, its file tests and renames don't); and hooks
    and zfs start with SIGPIPE at its default and `LC_CTYPE=C.UTF-8`, set up by Python before
    pysanoid runs.
  - **Found, not yet changed:** a `TZ` that is set but empty reads as UTC in the C library, and so
    upstream, and as the system's zone here (confirmed; it moves when snapshots are taken, not which
    are kept); and an /etc/localtime that is a copy rather than a symlink is read here through
    `timedatectl` or /etc/timezone, not from the file as the C library reads it (by reading the
    code; the test machine's can't be changed).
  The flow harness checked only that the `--force-prune` warning appeared; it now compares both
  messages word for word. `dev/e2e_subprocess.py` gained six process-level checks (a closed pipe, a
  log that can't grow, a latin-1 stdout, a cache directory that can't be searched), and the tests,
  the harnesses and the end-to-end checks all run under Python 3.11 as well.

- **2026-09-26 (`0.1.36`, second-pass re-audit, unit 8, `--debug`):** confirmed end to end against
  the real upstream script on ten configs (templates, both kinds of recursion, `skip_children`, odd
  names, a damaged cache, `--cron`). The summary, the dataset cache's lines and everything `--debug`
  changes about a run already matched; its text did not. Production never runs `--debug`, so none of this
  reached production.
  - **The config dump was Python's `pprint`**, not `Data::Dumper`'s: the same keys and values in
    different text. It is now `Data::Dumper`'s to the byte: keys sorted by their bytes, a nested hash
    indented by its key's length in bytes, the toggles and the `initialized`/`zfs_recursion` marks as
    bare `1`/`0` because upstream's init stores Perl integers there, and every other value quoted,
    counts included, since `Data::Dumper` prints a number bare only when Perl holds it as one.
    Identical to the real module on 6,000 random structures.
  - **The config-merge trace was missing** (`DEBUG: initializing $config{...}`, `overriding <key> on
    <section> ...`, `recursively setting ...`, `ignoring ...`), left out for a reason the parity rule
    doesn't accept. `Config::IniFiles` hands back sections and keys in file order, so every line
    matches upstream's but the order of one child's `recursively setting` lines, which come from a
    plain Perl hash. Each section's unknown and repeated keys are now checked when its turn comes, as
    upstream checks them, so the warnings fall between its lines, and a key upstream doesn't know is
    refused after the sections before it have loaded.
  - **The child lookup's `DEBUG:` line** (new in `0.1.33`) named the zfs pysanoid found,
    `/usr/sbin/zfs`, where upstream's names the bare `zfs` it runs. The dataset-cache harness gave
    upstream that same path, so it couldn't see this.
  - **Config file names** are spelled as upstream builds them: with `--configdir=/etc/sanoid/`,
    `/etc/sanoid//sanoid.conf`.
  - **A damaged cache's ages** print as Perl computes them: infinity as `-Inf`, and past 2**53 with
    Perl's own integer and floating-point steps. Every real creation time already printed the same:
    no difference among them in 60,000 fuzzed values, rounding edges included.
  - **Register only:** Perl's own warnings about upstream's code (`Use of uninitialized value ...
    line N`) and messages that call the program pysanoid join the cosmetic entry, and an empty
    here-doc value in sanoid.conf dumps as `''` where upstream's is `undef` (harmless).
  - **Found, not yet changed:** upstream words its refusal of an unknown key `FATAL ERROR: I don't
    understand ...` where pysanoid prints `FATAL: I don't understand ...`; and an empty here-doc value
    in sanoid.defaults.conf's `[template_default]` makes upstream refuse that key in every section
    that sets it, where pysanoid loads it as blank (confirmed; only a hand-edited defaults file has
    one).
  `dev/diff_debug_dump.py` now runs upstream's `getsnapsbytype` and `getsnapsbypath` into `blabber`
  and compares the whole output as bytes; `dev/diff_config_merge.py` runs with `--debug` on and
  compares the trace and warnings in order, and the dump of the real `%config`;
  `dev/diff_dataset_cache.py` runs upstream's bare `zfs` from PATH; and `dev/e2e_subprocess.py`
  checks `--debug` in a real process.

  Building the fuzz harnesses themselves also surfaced (and ruled out as *not* pysanoid
  bugs) two more upstream facts worth recording: upstream's `runscript` doesn't just risk
  leaving an orphaned child on timeout — when its `alarm()`-based timeout genuinely fires,
  it kills the **entire sanoid process** (`local $SIG{ALRM}`'s scope ends before the
  `system()` call it's meant to guard even runs, confirmed three independent ways against a
  real Perl interpreter); and a `script_timeout` with trailing garbage after a leading digit
  (`"5s"`, `"5,0"`) actually numifies to a real, enforced value upstream. The first
  *strengthens* the existing justification for pysanoid's own timeout mechanism; the second was
  left as a documented corner until `0.1.34`, which reads every value as upstream's `alarm()` does.

- **2026-09-26 (`0.1.37`, the upstream history pass):** upstream has no commits after the pin, so
  the pass read its issues and pull requests (see "What upstream's users reported"). One report
  was a problem pysanoid shared, confirmed end to end against the real upstream script:
  - **A section above the recursive section it belongs under lost its settings.** Upstream reads
    sections in file order, and a recursion copies its settings over every one of its datasets'
    sections already read (`skip_children` deletes them), so a child's own section works only
    below its parent. Upstream's code comment and example config describe a child's section as
    overriding its parent's recursion; neither mentions order. A `[tank/a]` keeping 12 monthlies
    and 5 yearlies, placed above a recursive `[tank]` keeping none, would lose all 17 to prune;
    placed below, it keeps them; above a `skip_children` parent it takes no snapshots at all.
    pysanoid did the same: the original audit made its merge follow upstream's order exactly. A
    user met it in the field (#1021: zvols meant to take no hourlies kept taking them until an NVMe
    array filled). Such a file is now refused, naming both sections and the fix; a file with its
    sections in the order upstream intends loads exactly as before. The rule is the file's, not
    zfs's: a misplaced section is refused whether or not its dataset exists yet, so the refusal
    comes when the file is edited, not the day the dataset appears.
  - **Register only:** nine entries cite the upstream reports behind them.
  - **Settled:** issue #202's one-lister question, left open by unit 7: no change (see "What
    upstream's users reported").
  - **Found, not yet changed:** snapshots dated in the future, after a clock that ran ahead, hold
    back their type until real time passes them, in pysanoid as upstream (#651).
  The config-merge and dataset-cache harnesses class the refusal as known, checked against the rule
  as `dev/lib/configorder.py` states it apart from `load_config`, and their generators put parents
  first most of the time so recursion's merge is still compared. The tests gain 16: the section
  order's, end to end among them, and the configurations and events users reported in #1021, #702,
  #938, #1038, #834, #952 and #1030; the three that pinned upstream's order now pin the refusal (7
  fail on `0.1.36`). `dev/e2e_subprocess.py` checks the refusal in a real process, before any zfs call, and
  starts take and prune together eight times (#1063): in every round one run deferred its cache
  updates to the other, and both finished cleanly.

- **2026-09-27 (`0.1.38`, the leads left after the second pass):** confirmed end to end against the real
  upstream script:
  - **Two sections that take or prune one dataset.** Upstream takes and prunes the dataset by each section, so
    every snapshot type keeps only what the stricter retention allows. A second section added for hourlies left
    its weekly count at the default 0 and destroyed all 4 weeklies the first section keeps; a `[keep-a] path =
    tank/a` keeping 12 monthlies under a recursive section keeping none lost all 5; and a recursive section with
    `path =` lost its whole tree's monthlies when placed above the recursive section it sits under, and its own
    dataset's when placed below, a shape `0.1.37`'s name-based order rule doesn't see. pysanoid did the same. Both sections also take the same
    snapshot: upstream's second attempt fails, with a CRITICAL ERROR and exit 0, and under `recursive = zfs` one
    of 16 Perl hash orders failed the whole tree's daily; pysanoid's `dst_` retry took a second copy, the lead
    unit 6a left. Such a file is now refused once its merge is done, naming the sections and the fix, before any
    snapshot is taken or destroyed. A section that neither takes nor prunes, or has `process_children_only`,
    doesn't count, so the layout #1088 describes, a recursive parent that skips itself plus a section of its own
    for the parent, still loads, as the production config does.
  - **An empty `$TZ`, and an /etc/localtime that names no zone.** The C library reads a `TZ` that is set but
    empty, or `:` alone, as UTC, and reads /etc/localtime itself, a link or a copy. pysanoid used the system's
    zone for the first, and for a copy asked timedatectl, then /etc/timezone. Both are now read as the C library
    reads them. The production units set `TZ` to a zone name, so neither reached it.
  - **Parity detail:** a setting upstream doesn't know is refused in upstream's words, `FATAL ERROR: I don't
    understand the setting ...`, the line its users quote (#645); `0.1.36` found the difference.
  - **Register only:** an empty here-doc value in sanoid.defaults.conf, which makes upstream refuse the key in
    every section that sets it (found in `0.1.36`); a hook that writes to pysanoid's output after its reader has
    gone, killed where upstream's gets a write error (the register had called that difference harmless); and a
    `$TZ` name with no zone file, or no /etc/localtime at all, both UTC to the C library.
  - **Found, not yet changed:** those last two, where the C library, and so upstream, uses UTC: pysanoid warns
    about a `$TZ` naming no zone and uses the system's, and with no /etc/localtime asks timedatectl, then
    /etc/timezone.
  - **Checked, no change:** the re-audit's own changes, `0.1.22` to `0.1.37`, read together for places where
    one fix meets another (locks and caches, the output writer and hooks, take's lenient readings and prune's
    preflight, the `dst_` retry and the post hook, the order, NUL and unknown-key refusals): nothing more. Closed
    with no change: a bounded retry while out of processes (failing at once is loud and safe, where upstream's
    retry can hang silently), killing a timed-out hook's whole process group (the oneshot units kill whatever is
    left when the run ends), take's CRITICAL ERROR wording (like the `$?` wording, declined), and future-dated
    snapshots (#651: parity, and the production host keeps time with NTP). The wording was taken up again on 2026-10-11: `0.1.43`
    gives the CRITICAL ERROR and `could not remove` lines upstream's words, and `0.1.45` the hook warning.
  The config-merge harness loads pysanoid twice, with the new check and without it, so a refused file's merge
  is still compared with upstream's, and checks the refusal against the rule as `dev/lib/configorder.py`
  states it: 4 hand cases and 50 of 400 fuzzed configs differ on `0.1.37`. The prune harness's two-section
  cases now use the layout that still loads. The tests gain 22, 15 of them failing on `0.1.37`, and
  `dev/e2e_subprocess.py` checks the refusal, upstream's words and an empty `$TZ` in a real process (all three
  fail on `0.1.37`).

- **2026-09-27 (`0.1.39`, `$TZ` and /etc/localtime as glibc reads them):** checked against this machine's C
  library, and end to end:
  - **A POSIX rule in `$TZ`** (`IST-1GMT0,M10.5.0,M3.5.0/1`) is applied, as the C library applies it. The register
    said zoneinfo can't read one, but it reads one as the closing line of a zone file, and a zone file with no
    transitions before that line leaves every instant to the rule. Nine rules matched the C library at every instant
    checked over two years, under Python 3.14 and 3.11. `0.1.38` warned and used the system's zone.
  - **A `$TZ` that names no zone, and no /etc/localtime,** are UTC, as the C library reads them (confirmed against
    real Perl for a name with no zone file), and pysanoid warns. `0.1.38` used the system's zone for the first, and
    for the second asked timedatectl, then /etc/timezone. The C library reads neither, so both are gone, with the
    warning about /etc/timezone disagreeing. A zone name is looked up where glibc looks, under `$TZDIR`, else
    /usr/share/zoneinfo, not along zoneinfo's own path.
  - **A damaged zone file stopped take, and under production's Python could hang or crash it.** Python 3.11's zoneinfo
    reads on forever through a zone file cut short in its closing rule line, and reads past its own table when a
    transition names a type the file lacks, which crashes the process (a segmentation fault, end to end). A file cut
    short anywhere else raised an error pysanoid didn't catch. Python 3.14 refuses both files, as glibc does (the
    versions between weren't checked). Every version reached this through `ZoneInfo(<zone name>)`, which is how
    production reads its `TZ`: its zone file damaged that way would have crashed every take, or hung the first for
    good. The production take unit is a oneshot with no start timeout, so the hourly timer would not have started it again,
    nor would the prune unit ordered after it have run. The production host's root is checksummed ZFS, and tzdata replaces its
    files by rename, so neither was likely. pysanoid now reads zone files itself, refuses what 3.14 refuses, and
    reads such a file, or a zone whose offset is a day or more (a rule such as `XST24`, which datetime can't hold),
    as no zone: UTC, with the warning.
  - **Register:** the `$TZ` entry now lists what zoneinfo and datetime read otherwise than glibc: a rule's zero-based
    day number, a day early, and `J59`, a day late in a leap year (both CPython bugs); malformed rules the two parse
    differently; an offset of a day or more; and a zone file cut in its closing rule line, which glibc reads in part.
  The prune harness gains the class unit 7 gave the cache harnesses, a run directory no lock file can be made in.
  Upstream's unchecked `writelock` writes nothing, so it destroys its first batch unlocked, hooks included, and its
  `removelock` dies before the flush; pysanoid is checked against itself: every batch that needs the lock is skipped
  and reported, and nothing is destroyed or run. It is 4 of 6 new hand cases and 11 of 400 fuzzed scenarios, and two
  broken versions, one pruning unlocked as upstream does and one skipping silently, fail it. The tests replace 15
  timezone tests with 32, of which 23 fail on `0.1.38` (under Python 3.11 the run crashes partway), and
  `dev/e2e_subprocess.py` gains four checks, all failing on `0.1.38` (under 3.11 it hangs on one and crashes on
  another).

- **2026-10-10 (`0.1.40`, the third audit):** the audit the plan at the end of this document describes. It asked
  whether pysanoid keeps its promises when what a run reads is wrong, and ran the real upstream script beside
  pysanoid on every case it broke. It found three ways for prune to destroy snapshots the config keeps. Upstream
  shares all three, which is why two audits of parity had not seen them:
  - **A child's own section, built from the defaults.** A section under a recursive section gets that section's
    settings only through its recursion, and only when the child lookup lists the dataset. It doesn't when
    datasets.txt was written before the dataset arrived and got its section, for as long as that file lasts (20
    minutes), or when the lookup exits non-zero having printed nothing while the dataset is there. Both programs
    then built the section from the defaults and pruned by them: a child meant to keep its parent's 4 weeklies, 12
    monthlies and 3 yearlies lost 13 of the 19 to the defaults' 0, 6 and 0, exit 0. Unit 4 of the re-audit had
    stopped the run for a lookup that is killed or fails partway, and read one that fails having listed nothing as
    a parent that isn't there; that reading missed the lookup failing so while the dataset exists, and it took a
    child with a section of its own to be safe on a cached list, which holds only for a list written under the same
    config. A section named under a recursive section now starts from that section's settings whether or not the
    lookup listed it, so what a dataset with a section of its own keeps no longer depends on a cache or on a
    listing. Under `skip_children`, which hands nothing down, such a section starts from the defaults as before.
    The production host has no section under its one recursive section, and its hourly timer always finds datasets.txt expired; a
    run started by hand within 20 minutes of one reads it.
  - **A cache dated in the future never expired.** Both programs take a cache's age as the time now less its date,
    so one written while the clock ran ahead passed the test until real time reached that date. A snapshots.txt
    dated a year ahead, still listing two dailies a rollback had destroyed, let both destroy 2 of the 3 dailies a
    `daily = 3` dataset really held, without asking zfs; a datasets.txt dated so hides every dataset made since from
    its recursive section. Both now count as expired.
  - **A snapshot cache cut short inside its last line.** The newest snapshot's creation time loses digits and reads
    as decades old: both destroyed a dataset's newest daily and kept one 4 days old. Every cache written ends its
    last line, so one that doesn't is now no cache, like one holding a NUL byte. The cache add had repaired such a
    file by ending its last line, a deviation from the first audit; that would pass a line cut short off as whole,
    so the add now leaves the file as it is, for the next listing to replace.
  - **Checked, no change:** every file operation and every zfs call of a take run, a prune run and a `--cron` run
    was made to fail in turn (an error, a partial write, a command that can't start, is refused, or is killed before
    or after acting, an empty or half listing), or the process was killed at that point, and a clean run followed. In
    6,862 such trials under Python 3.14 and 3.11 nothing escaped as a traceback and no destroy broke the retention.
    A snapshot destroyed but still listed in the cache can't make prune destroy more: it is the oldest of its type,
    so it takes up the extra count itself. 37,500 runs over random configs with odd values in every setting found
    one thing, the long count below.
  - **Left as decided:** a pool-capacity query that fails ends the prune run, as upstream's does (re-audit unit 1b,
    and the read of 2026-09-27). pysanoid reads sections in file order, so with the production sanoid.conf and its tank
    pool away it ends at `[tank/encrypted/backup]`, the one section with `prune_defer`, before any of zroot's.
    Stopping destroys nothing and fails the unit on every run; with that section moved below the others, the rest
    are pruned before the query is made (checked on `0.1.39`).
  - **Found, not yet changed:** a snapshot listing killed inside its last line does what the cut cache did, in both
    programs: the retry's output is used as it came, and a creation time cut short reads as decades old (the
    newest daily destroyed, confirmed against real Perl); a retention count past 308 digits stops prune with a
    traceback after the sections above it were pruned, and past 4,300 before any, where upstream reads infinity and
    carries on (unit 5 had noted the second for `prune_defer`); `pysanoid_alert.py`'s freshness check calls a
    snapshot dated in the future fresh, though take takes none of its type until then (#651); a zfs message that
    isn't UTF-8 reads as a command that couldn't run; snapshot names carry the time the run began, where upstream's
    carry the time each dataset's turn came (both fixed in `0.1.42`); and `pysanoid.timer` has `Requires=pysanoid.service`, as the timer in
    upstream's INSTALL.md has, so stopping the service by hand stops the timer too (by systemd's documented rule; not
    tried in production; the line was removed on 2026-10-10, see `0.1.41`).
  The dataset-cache harness had been generating the first case all along, in 5 to 13 of every 500 scenarios, with
  no difference to show; it now checks each such section key by key against the rule as `dev/lib/configorder.py`
  states it, and a datasets.txt dated ahead against pysanoid with no cache. The enumeration harness dates caches
  ahead and cuts them short, each checked against pysanoid itself, and the add/remove harness requires a cache cut
  short to be left exactly as it was. New cases come from a random stream of their own, so a seed's other scenarios
  are the ones it gave before. The tests gain 21 and one is rewritten (the cache add's repair): 15 fail on `0.1.39`
  under Python 3.14 and 3.11. `dev/e2e_subprocess.py` gains four checks; the three that count destroys fail on
  `0.1.39`. The fault injection and the config fuzz were one-off probes, not kept in `dev/`.
- **2026-10-10 (`0.1.41`, the third audit continued):** two more of that audit's findings, one left as decided, and
  the timer unit brought in line with upstream's Debian packaging.
  - **A snapshot listing killed inside its last line.** When `zfs get` over the configured datasets fails, both
    programs list every dataset instead and use what comes back, whatever that second command's exit. Killed before
    its last buffer is written out, it ends inside a line, and a creation time cut short reads as decades old: both
    destroyed a dataset's newest daily and kept one 4 days old, exit 0. pysanoid then wrote the cut line into its
    cache with a newline, where the check `0.1.40` gave the cache can't see it. Upstream reads a first listing
    killed by a signal the same way; pysanoid has retried that one since `0.1.30`. No zfs that finished leaves a
    line unended, so a listing that doesn't end in a newline now goes without its last line. That is the whole
    difference: the run prints and exits as upstream's does. The lines before it are used, as before: seeing fewer
    snapshots than there are, prune destroys none it wouldn't have. Take still takes every type of the datasets zfs
    didn't get to, as it does for a listing that comes back empty (kept as upstream's since 2026-09-21, the first
    audit). It needs the first listing to fail, as it does while a pool named in sanoid.conf is away, and the retry
    to be killed.
  - **A retention count too long for a number.** Upstream reads `yearly = 999...` as a float, infinity past 308
    digits, and carries on: no dataset has that many snapshots, so none of that type is pruned. pysanoid read it
    whole. Past 4,300 digits Python's `int()` refuses it: a traceback before any destroy (unit 5 had noted that for
    `prune_defer` on 2026-09-26, and left it), and take, reporting the same refusal, took none of a type upstream
    takes. Past 308 digits a `yearly` count overflowed its period, a float: a traceback at that section, the ones
    above it pruned and the ones below it not. A count of more than 18 digits now reads as `2**63 - 1`, which
    behaves as upstream's number does in every comparison, and a `prune_defer` that long is refused like any other
    over 100.
  - **Left as decided: a newest snapshot dated in the future.** The history pass found it (#651), and the read of
    2026-09-27 closed it with no change: a type isn't due until the clock passes its newest snapshot's date, so once
    a clock that ran ahead is set right, the type's snapshots stop for as long as it was ahead, and neither program
    says so. This audit listed it again, without saying that it had been decided, and a report from take was built
    for `0.1.41`: an error and a non-zero exit where upstream is silent. It was taken out before any commit. The
    rule, restated on 2026-10-10: pysanoid mirrors upstream 1:1 unless that compromises data safety. Holding a type
    back destroys nothing, so the rule gives no ground for a difference here, and a report is one. The September
    decision stands. A comment at the comparison, three tests, five hand cases of the take harness and an end-to-end
    check now pin it, so that it reads as decided where the next audit will look. The same rule took a second
    report out of this version: the listing fix above first announced the line it left out with an error and a
    non-zero exit, and leaving the line out is all the data needs. `pysanoid_alert.py` is not part of the port and
    has no upstream to mirror. Its freshness check, written on 2026-10-05 to catch a type that has stopped, called
    such a snapshot fresh; it now reports one dated more than 5 minutes ahead. *The September decision was reversed
    in `0.1.53` (DEV-051); see "The 2026-10-28 review".*
  - **The timer's `Requires=pysanoid.service`, removed to match upstream's Debian packaging.** By systemd's rule
    for `Requires=`, stopping `pysanoid.service` by hand stopped `pysanoid.timer` with it (systemd.unit(5); not tried
    in production). Upstream publishes the timer in three places, and they differ: the `sanoid.timer` of its INSTALL.md has
    `Requires=sanoid.service`, and the timers in its Debian and RHEL packaging leave the line out. pysanoid's units
    had followed INSTALL.md's, and the line was first recorded here as kept, as parity with those. The production host is a TrueNAS
    appliance, which is based on Debian, so the Debian package's units are the ones to mirror: the line was removed
    on 2026-10-10 (`b3f4056`). The three units are now upstream's Debian units (`packages/debian/` at the pinned
    commit) with the production settings: its paths and `--configdir`, the hourly schedule with `RandomizedDelaySec`,
    the local `TZ`, `TimeoutStartSec=30min`, and the `OnFailure=` handler.
  The enumeration harness's fake zfs can cut the retry short at any byte. The class is checked against real Perl
  and against pysanoid itself, which must do what it does when the listing ends at the line before: 9 of 12 new
  hand cases and 20 of 500 fuzzed scenarios differ on `0.1.40`. The prune and take harnesses
  draw counts of 19, 400 and 5,000 digits (prune: 4 of 10 new hand cases and 9 of 400 fuzzed scenarios differ on
  `0.1.40`; take: 1 of 10 and 4 of 300), and take's also dates newest snapshots ahead, where the two programs must
  match and do. The tests gain 35, and the alert script's 5, with the test that pinned "fresh" rewritten: 19 and 4
  fail on `0.1.40`. A mutation check, 17 one-line breaks of the changes and of the parity the tests now pin, fails a
  test every time. `dev/e2e_subprocess.py` gains five checks; two fail on `0.1.40`. The audit's fault injection
  had not seen the first of these: it cut a listing only at a line's end, and every dataset its config named was
  there, so the retry never ran. Given a config naming a dataset that isn't there, a pool that lists snapshots
  oldest first as zfs does, and a retry killed inside the line of each dataset's newest snapshot of each type, it
  counts 14 destroys inside their window on `0.1.40` in each of its two modes, and none in 7,070 trials on `0.1.41`
  under Python 3.14 and 3.11, with no traceback. It is still a one-off probe, not in `dev/`. **Still found, not yet
  changed:** a zfs message that isn't UTF-8 reads as a command that couldn't run, and snapshot names carry the time
  the run began (both fixed in `0.1.42`).
- **2026-10-10 (`0.1.42`, the third audit's last two findings):** both are places where pysanoid had drifted from
  upstream, so both fixes restore parity and the register gains no entry.
  - **What zfs or zpool prints that isn't UTF-8.** Upstream never decodes what a command prints: it looks for
    `already exists` in the bytes, and the rest goes to the terminal as written. pysanoid read the output of `zfs
    snapshot`, `zfs destroy` and `zpool list` as text in the locale's encoding, and Python refuses a byte that
    encoding can't hold. The refusal comes once the command has finished, and pysanoid took it for a command that
    could not run. So a snapshot that was taken was reported as failed, exit 1, and never reached the cache; a name
    already taken got no `dst_` retry; each destroy that worked was reported as `could not remove` and stayed in the
    cache; and under `prune_defer` the capacity query ended the prune run at that section on every run, where
    upstream prunes (each confirmed against the real script). Real zfs prints no such byte about a name it accepts,
    so it takes a wrapper around zfs or zpool, or a section name holding one, to get there; the production host has neither. The
    output is now read as the listings are: as UTF-8 whatever the locale, a byte that isn't UTF-8 kept as it is.
    Nothing a command prints can fail it, and its complaint reaches the journal in the bytes zfs wrote. The fix
    first proposed was to show such a byte as U+FFFD; keeping it is what upstream does, and what the register says
    of everything else pysanoid writes out.
  - **The time in snapshot names.** Upstream reads the clock for the names inside its loop over the datasets, before
    the dataset's `pre_snapshot_script` runs, and gives all the dataset's types that one time. pysanoid read it
    once, as the run began: with snapshots that take 2 seconds, upstream's names were 2 seconds apart from one
    dataset to the next and pysanoid's all the same, and a hook that ran for minutes left every later name minutes
    behind the clock. Retention goes by a snapshot's creation time, not its name, so nothing was kept or destroyed
    differently; the name is what a person restoring reads, and what other tools read. pysanoid now reads the clock
    as each dataset's turn comes. Which dataset comes first still differs: upstream's order is Perl's, random from
    run to run, and pysanoid's is the config's.
  - **Found, not changed (new, 2026-10-10):** two places where zfs's own words are shown differently, neither with
    any effect on what is taken or pruned. When a `zfs snapshot`, `zfs destroy` or `zpool list` works and still
    writes to stderr, upstream's zfs writes that to the terminal itself; pysanoid reads the command's stderr, to
    give a failure's reason in its own line, and shows nothing of it when the command worked. And the complaint of
    a snapshot or child listing, which pysanoid echoes, shows a byte that isn't UTF-8 as U+FFFD where upstream's
    journal has the byte (`_echo_zfs_stderr`, since `0.1.14`). That text also reaches `pysanoid_alert.py`'s mail, so
    it was left as it is rather than changed in passing. Both are fixed in `0.1.43`.
  The take harness ran both programs on a clock that stood still and handed pysanoid that clock, so it could not
  see the second finding, and all but a few of its cases had one dataset. It now runs pysanoid the way a real run
  runs, on a clock that moves a scenario's `tick` seconds for every zfs command and hook that has run, with up to
  three datasets. Upstream takes the datasets in Perl's random order, so each side's names are first checked
  against upstream's rule (a dataset's names all carry the clock as its turn came), then rewritten to the
  scenario's own time and compared: 5 of 8 new hand cases differ on `0.1.41`. The fake zfs can print a warning or
  a complaint in bytes that aren't UTF-8: 3 of 4 more hand cases of the take harness differ on `0.1.41`, as do 101
  of 300 fuzzed scenarios, and 3 of 4 new hand cases and 10 of 400 fuzzed scenarios of the prune harness. The tests
  gain 18: 14 fail on `0.1.41` under Python 3.14 and 3.11, and the ones about bytes go through real executables
  standing in for zfs and zpool, since the decoding is `subprocess.run`'s own. A mutation check, 16 one-line breaks
  of the two changes, fails a test every time. `dev/e2e_subprocess.py` gains seven checks; six fail on `0.1.41`.
  The audit's fault injection, run again, finds what it found on `0.1.41`, category for category, in 7,070 trials
  under Python 3.14 and 3.11: no traceback, and no destroy inside its window. The gate caught a slip in this
  version's own harness work: the simulator hands the take harness scenarios of its own, which lacked the new
  `tick`, and its four runs failed until the harness read that key with a default.
- **2026-10-11 (`0.1.43`, zfs's own words, shown as upstream shows them):** the two differences `0.1.42` found and
  left, and what closing them brought with it. Each point is confirmed against the real upstream script, byte for
  byte.
  - **What zfs and zpool write when the command works.** Upstream runs `zfs destroy` through `system()` and `zfs
    snapshot` inside `tee_stderr`, and reads the listings and the capacity through a pipe: whatever a command
    writes to stderr reaches the terminal as written, and so does what a snapshot or a destroy writes to stdout.
    pysanoid read all of it, and showed a command's stderr only inside its own line about a failure, so a warning
    from a command that worked was lost. Now what a snapshot or a destroy wrote to stdout and to stderr is written
    out once the command has finished, and so is the capacity query's stderr, whether the command worked or failed:
    the bytes zfs wrote, none added, changed or dropped.
  - **A listing's complaint, byte for byte.** The complaint of a snapshot listing or a child lookup, which pysanoid
    has echoed since `0.1.14`, was shown with U+FFFD for a byte that isn't UTF-8, trimmed, and given a newline. It
    is now written out as it was read. `pysanoid_alert.py` catches that text for its mail, and neither a mail nor a
    terminal can hold a lone byte: its print raised on one, as it already did for a section's name holding such a
    byte, and the mail's JSON carried half a character. The alert script now shows such a byte as U+FFFD in what it
    sends and prints, which for zfs's complaint is what its mail showed before.
  - **The failure lines, in upstream's words.** With zfs's complaint on a line of its own, pysanoid's lines, which
    quoted it, would have said it twice. They now read as upstream's do: `CRITICAL ERROR: zfs snapshot x failed,
    256` (with `-r` for a recursive snapshot) and `could not remove x : 256`, the number being Perl's `$?`: the
    exit status times 256, or the number of the signal that killed the command. The read of 2026-09-27 had closed
    "take's CRITICAL ERROR wording" with no change (see `0.1.38`). That closure was found before anything was built
    against it and put to the user, who chose upstream's form. A command that can't start gets a line saying why,
    then upstream's line with -1, which is what Perl's `system()` returns for it.
  - **`prune_defer`'s capacity answer, read as upstream reads it.** Upstream takes the first word of the answer's
    first line and requires it to be one to three digits and a percent sign. pysanoid trimmed the whole answer
    first, so it pruned by answers upstream refuses: a capacity after a space, a tab or a blank line, or before a
    tab, a carriage return, a form feed or a no-break space. It now reads the answer as upstream does and stops the
    prune in upstream's words, `ERROR: 'zpool list -H -o cap x' command returned invalid capacity value (...)!`.
    The user's condition for porting this was that it must not endanger data, and it doesn't: every answer `0.1.43`
    prunes by, `0.1.42` pruned by too, and by the same number, so the port only adds refusals (by the two readings
    themselves, and in two million generated answers run through both versions). One difference
    stays, as a deviation in the register: an answer holding more than a capacity, a second word or a second line.
    Upstream prunes by its first word (`85%` and a line after it pruned 3 of 5 dailies under `prune_defer = 50`).
    pysanoid refused such an answer before and still does: with more in the answer, that word needn't be this
    pool's capacity, and a number that isn't prunes what `prune_defer` holds back. Real zpool gives one pool's
    capacity alone on a line, so none of this reaches production.
  - **What still differs, each in the register:** Perl's ` at <script> line N.` after a warning; the 128 a core
    dump adds to the wait status, which Python's `subprocess` doesn't pass on; the line saying why a command
    couldn't start; and when zfs's words come: once the command has finished, so later by the time it took, and a
    command's stdout before its stderr. pysanoid reads them where upstream's zfs writes to the terminal itself,
    and reading them is what keeps zfs running when the journal's stream is lost. A failed hook's warning also
    still gives the hook's exit code where upstream's gives the wait status: that wording was declined on
    2026-09-25, is now in the register, and was not changed here (it was in `0.1.45`).
  A probe ran the real upstream script beside pysanoid on 29 scenarios (commands that work and warn, in UTF-8 or
  not, with or without a final newline; failures by exit status and by signal; the `dst_` retry; listings and
  child lookups that complain; capacity answers) and compared stdout and stderr byte for byte, less Perl's ` at
  ... line N.`, Perl's own warnings, the time in a name and pysanoid's closing count of problems. 27 are the same
  under Python 3.14 and 3.11, and the other 2 are the capacity deviation; on `0.1.42`, 3 of 29. The probe is a
  one-off, not in `dev/`; what it showed is kept in three places. The take and prune harnesses now compare stderr:
  zfs's own words and the two failure lines (the take harness's stand-in for `tee_stderr` had kept to itself what
  the real one passes on). 12 of 65 and 8 of 51 hand cases differ on `0.1.42`, and 28 of 300 and 20 of 400 fuzzed
  scenarios. A new target, `zpool_capacity`, gives upstream's `get_zpool_capacity` and pysanoid's the same answer
  from a fake `zpool`: 26 of 37 hand cases and 582 of 600 fuzzed answers differ on `0.1.42`. And
  `dev/e2e_subprocess.py` gains 12 checks, in real processes on real bytes: 13 of its 71 fail on `0.1.42`, two of
  them older checks given the new form. The tests gain 48, of which 45 fail on `0.1.42` under Python 3.14 and
  3.11. 24 older cases, rewritten for a stand-in zfs that answers in bytes and for upstream's failure lines, fail
  there too, and the alert script's tests gain 3, of which 2 fail. A mutation check, 33 one-line breaks of the
  changes, fails a test every time. The audit's fault injection, run again, finds what it found on `0.1.42`,
  category for category, in 7,070 trials under Python 3.14 and 3.11: no traceback, and no destroy inside its
  window. (Its first run under 3.11 crossed 10:00 on the real clock, where one trial's second round took the next
  hour's hourly and pruned one more; run again, it matched.) The gate caught a slip in this version's harness
  work, as it did in `0.1.42`'s: the simulator's own check of what a run may report still looked for `could not
  remove x: `, and its long run failed on every injected destroy failure until the check was given upstream's
  words. The whole gate was run again from fresh copies: 30 of 30 under Python 3.14 and 3.11.
- **2026-10-11 (`0.1.44`, lint, and the oldest Python):** nothing a run does changes. `pysanoid.py` wrote UTC as
  `timezone.utc` in three places, and ruff, when it lints for Python 3.11 or later, asks for `datetime.UTC`, the
  same object under the name 3.11 gave it (UP017). With that name the file needs Python 3.11 or later, and its
  first lines now say so; `0.1.43` still started under 3.9. Production's is 3.11.9, the oldest pysanoid has to run on.
  The tests' two uses take the test file's own `UTC`. No test is added: on a Python pysanoid runs on there is no
  difference to show. What was wrong was in the gate, not in the code: see the update of 2026-10-11 under
  Recommendation.
- **2026-10-11 (`0.1.45`, a failed hook's warning, and how a hook is started):** the last line whose words differed
  from upstream's by decision, and what it showed once it was changed. Confirmed against the real upstream script,
  byte for byte.
  - **The warning, in upstream's words.** Upstream's `runscript` warns `WARN: $key failed, $?`, the wait status:
    256 times the exit status, or the number of the signal that killed the hook. pysanoid wrote `WARN: <key> failed,
    exit code 3`. Taking upstream's `$?` was declined on 2026-09-25 and left alone by `0.1.43`, which gave the zfs
    failure lines upstream's words; the user asked for it on 2026-10-11. The line is now upstream's: `WARN:
    pre_snapshot_script failed, 768`.
  - **A hook command that can't be exec'd.** With the status in the line, a difference the hooks harness had
    counted as known since `0.1.34` could be read off the log. Upstream's `system()` execs a command with no shell
    metacharacter itself, and when it can't (a file that isn't there or isn't executable, a directory, a script
    whose interpreter is missing, `exit 0`) it returns -1: `WARN: ... failed, -1`, after Perl's own `Can't exec`.
    pysanoid ran those words through `/bin/sh`'s `exec`, which made the shell complain and exit 127 or 126, so the
    line would have read `failed, 32512`, and which number depended on the system's `/bin/sh`: under one that is
    bash, a missing interpreter was 1, and a hook named `-c` worked. Both were a failed hook to every caller, which
    is why the class was accepted. pysanoid now execs the words itself, as Perl does: a command that can't be
    exec'd is -1 on both sides, a line of pysanoid's own saying why where Perl has `Can't exec`, and no shell is
    involved. A script with no `#!` line still goes to `/bin/sh`, as `execvp` sends it. Nothing changes for a hook
    that starts. The production `sanoid.conf` has no hooks.
  - **A hook that can't start for want of a process** was one line of pysanoid's wording; it is now a line saying
    why, then upstream's with -1, as `0.1.43` did for a snapshot and a destroy.
  - **An import for the type checker.** `pysanoid.py` named the type of one parameter by `collections.abc.Buffer`,
    which Python 3.12 added. The name is read only by a type checker, never by a run, but a check made for Python
    3.11, production's, called it unknown; the gate's check ran for the Python it runs on, 3.14, and passed. It is now
    typeshed's own name for what that parameter takes, good under any version, and the gate checks for 3.11 too.
  The register keeps its 56 entries: three were reworded, and the sentence saying the hook warning differs is
  gone. A probe ran the real upstream script beside pysanoid on 21 runs with a failing hook (exit statuses, signals,
  commands that can't be exec'd, a script with no `#!`, a final `2>&1`, each kind of hook): all 21 read the same
  under Python 3.14 and 3.11, less Perl's ` at ... line N.`, its `Can't exec` and pysanoid's line saying why; on
  `0.1.44`, none. It is a one-off, not in `dev/`; the hooks, take and prune harnesses and four new end-to-end checks
  keep what it showed (see the results table; all four checks fail on `0.1.44`). The tests gain 28, of which 24
  fail on `0.1.44` under Python 3.14 and 3.11, as do 3 older cases given the new words. A mutation check, 27
  one-line breaks of the changes, fails a test every time, after three tests were added for the four it first let
  through (one of the four was a break that didn't compile).

## Is the code more safe, neutral, or more dangerous?

**More safe, on every axis this audit tested, with zero known place where pysanoid is
riskier than upstream.** Every fix and every kept, documented deviation moves in one of
two directions: prune becomes *more* conservative (fail closed, validate before the first
destroy, never let an unrelated config value abort a run mid-destroy), or take becomes
*more* protective and *louder* about failure (isolated per dataset, exits non-zero instead
of upstream's silent exit-0). I did not find a single documented divergence that weakens a
safety property relative to upstream. The clearest confirmation of this came *after* the
original 8-module audit: building `dev/`'s `hooks` fuzz target found that upstream's own
hook-script timeout, when it actually fires, doesn't fail gracefully at all — it kills the
whole sanoid process (see "What was actually wrong, and got fixed" below). pysanoid's
already-existing, already-documented decision to use `subprocess.run(timeout=...)` instead
of `alarm()` turns out to guard against something considerably worse than the "orphaned
child" risk it was originally written to describe.

**Correction, 2026-09-24:** the second-pass re-audit found one documented divergence that did
weaken a safety property. Prune's hook-source deviation skipped a veto hook upstream would run
when two sections share a dataset. It is fixed in `0.1.23`, so the statement above holds again.

The one place worth a caveat rather than a clean "safer": pysanoid's locking uses
`fcntl.flock` in place of upstream's PID+process-list lockfile hack. The two mechanisms
don't recognize each other's lock files. This is not a risk in pysanoid's own operation,
and not a *new* risk from this port (upstream's mechanism doesn't protect against a third
tool touching the same files either) — it would only matter if both implementations ran
against the same `run_dir` at once, which the production systemd units never do.

## Have we actually written a better port — "more capable"?

Better, in specific and verifiable ways, within a **smaller** scope than upstream — "more
capable" isn't quite the right frame, and I'd avoid it. pysanoid does less than sanoid (no
syncoid, no findoid, no Nagios monitors), by design, from day one. Within what it does do:

**Measurably better than upstream:**
- Prune fails closed and all-or-nothing; upstream can die mid-run after partially pruning.
- Take fails soft, isolated per dataset, and loud (exit non-zero); upstream's failure to
  take a snapshot is a silent warning and exit 0.
- Snapshot-cache I/O is byte-exact and newline-only; upstream's own cache is vulnerable to
  a stray CR being silently dropped by a naive port (which this one no longer is, but
  upstream's own pipe-based read has no such protection either).
- Scheduling uses `zoneinfo`, which is DST-correct; upstream's manual DST offset math is
  measurably buggy, and after the autumn change it takes no frequent snapshots for the whole
  repeated hour in zones such as New York and Sydney (confirmed against a real Perl
  interpreter). `dev/sweep_dst.py` checks pysanoid's times against a brute-force answer at
  every instant around each change.
- A hook that outlives its timeout has its own process killed and `run_hook_script` returns
  cleanly; upstream's `alarm()`-based timeout doesn't just risk orphaning the child — when
  it genuinely fires, it crashes the **entire sanoid process** (confirmed 2026-09-22: the
  signal handler that's supposed to catch it is scoped to the block that arms the alarm,
  not to the `system()` call it's meant to guard, so an uncaught `SIGALRM` takes the whole
  run down instead of the graceful "warn and continue" the code visibly intends).
- A cache that can't be read or written never stops a run; upstream dies when it can't
  create one, so a full or read-only disk stops every snapshot and prune while it lasts.
- A lock file that can't be made never stops a run: only what that lock guards is skipped, and
  prune skips the batch. Upstream carries on unlocked and dies releasing the lock.
- Ctrl-C stops a run; upstream's `system()` ignores it while a command runs, so a mistaken prune
  can't be stopped that way.
- Output that can't be written never stops a run, as upstream's doesn't; where a pipeline's reader
  goes away with SIGPIPE at its default, upstream is killed mid-run and pysanoid finishes.
- A `--cache-dir` or `--run-dir` starting with whitespace works; upstream exits 2 on every run,
  taking no snapshots, or under `--cron` never pruning.
- A snapshot's name can't stand in for a snapshot type; upstream reads a legal name such as
  `autosnap_dailycreation9999999999_x` as a daily made in 2286, which stops that dataset's
  dailies for as long as the snapshot exists.
- A recursive section's child lookup that comes back short stops the run before anything is
  pruned; upstream takes the partial list, so a child with a section of its own is set up from the
  defaults instead of its parent's settings and pruned by the default retention.
- A section under a recursive section starts from that section's settings whatever the lookup
  listed; upstream builds it from the defaults whenever its dataset wasn't listed (a dataset cache
  older than the section, a lookup that fails having listed nothing) and prunes it by them.
- A cache dated in the future is read afresh, and so is a snapshot cache cut short; upstream trusts
  the first until real time reaches its date, and reads the second's last line as it stands, a
  creation time that lost its digits included.
- A snapshot listing cut short by a killed `zfs` is used without its cut line; upstream reads
  the cut line, whose creation time reads as decades old, and prunes that snapshot, a dataset's
  newest.
- A missing `zfs`/`zpool` binary is a clear error; upstream's shell-pipe read silently
  returns an empty result, which can look like "no snapshots exist."
- Malformed numeric config values are reported by name: prune refuses them before its first
  destroy, while take and `script_timeout` read them as upstream does and exit non-zero; upstream
  silently numeric-coerces ("3o" → 3) everywhere and carries on.

**Deliberately narrower/stricter (a real tradeoff, chosen and tested, not a defect):**
- The CLI dialect (`argparse`) accepts less than Getopt::Long does — unusual spellings
  that upstream tolerates by accident (`-cron`, `--Cron`, a lone `--`) are refused here.
  The production units are unaffected; this only matters for compatibility with existing
  scripts/habits built around upstream's looser parsing.
- Config values that upstream silently misinterprets are refused instead (bad toggles, an
  undefined `use_template`, non-numeric retention counts when pruning).

**Equivalent — the large majority of the port.** The merge/recursion algorithm, the ini
grammar, the retention/prune-candidate math, snapshot naming, and lock *names* were all
independently verified to match upstream exactly (see the differential numbers above).

## Percentage in line with upstream vs. deviation, and why

`DEVIATIONS.md` holds the register — the *exhaustive* list of every point where behavior can
differ from upstream at all, each with its evidence, code and tests; until `0.1.46` it was
`pysanoid.py`'s module docstring. As of this commit it holds **57 entries**: the docstring's 56,
one for one, and P4, a parity note moved out of the code (56 were 25 at the close of the 8-module audit, +2 from the
2026-09-22 post-audit fixes below, +3 from the 2026-09-24 re-audit, +13 from the 2026-09-25
re-audit, +4 from the 2026-09-26 re-audit, whose unit 8 also traded the `--debug` trace's scope
entry for a harmless one, +1 from the upstream history pass, +2 from `0.1.38`, +3 from `0.1.40`, which also
replaced the entry on a cache missing its final newline, +1 from `0.1.41`, and +2 from `0.1.43`; `0.1.39` rewrote the
`$TZ` entry, and `0.1.42`, which only restores parity, added none, nor did `0.1.44` or `0.1.45`), for a 2,320-line file implementing dozens of distinct behaviors. One entry,
DEV-039, is five cases, each with an entry of its own (DEV-039a to DEV-039e). From 2026-09-24 until `0.1.38` this count read one
too many: that day's re-audit was counted as 4 entries where it added 3. Classified by cause:

| Category | Count | % of register | What it means |
|---|---|---|---|
| Safety-motivated | 36 | 63.2% | A concrete data-loss or silent-failure path in upstream; pysanoid deliberately differs |
| Port mechanism | 7 | 12.3% | Python vs. Perl (argparse, fcntl.flock, argv vs. shell string, output a pipeline's reader has left, what hooks and zfs inherit from Python's startup, the `$TZ` forms zoneinfo and datetime read otherwise than glibc, zfs's own output read and written out where upstream's zfs writes to the terminal itself) — several of these are *also* incidentally safer, but that's not why they exist |
| Explicit scope exclusion | 1 | 1.8% | Never attempted: `--monitor-*` (Nagios, out of scope from day one) |
| Harmless, nothing put at risk | 7 | 12.3% | Path de-duplication (performance only), `--help`/exit-code text, `--debug` iteration order, how the `--debug` dump spells an empty here-doc value, an empty here-doc value in sanoid.defaults.conf (a section that sets the key keeps its own value, where upstream refuses the key), upstream's mid-run snapshot-list re-read (one more `zfs get`, and a snapshot on its age boundary destroyed there a run sooner than here), and which of two snapshots made in the same second prune removes first (the same number of the same age is kept) |
| Documented as matching, not actually a deviation | 6 | 10.5% | Kept in the register only so a future reader doesn't "fix" something that's already intentional (the empty, fresh cache written when both `zfs get` calls fail; the `--debug` output's text: its merge trace, `Data::Dumper` dump, blank `desired:`, asymmetric blank-line count and Perl's ages; `take_snapshots`' post-hook `SANOID_SNAPNAME` prefix and `dst_` rename, take's upstream reading of a malformed or blank number, whose only differences are a dropped fraction and a blank day or month costing its type where upstream dies, `script_timeout` read as upstream's `alarm()` reads it, and hook command lines split as Perl's `system()` splits them) |

Outside those 57 points, there is no known behavioral difference anywhere in the ported
logic — and "known" is load-bearing: it means verified by the differential testing above,
not merely unnoticed. On the input space actually exercised by that testing (many
thousands of scenarios across modules 1 through 8, real Perl-interpreter probing for
locking specifically), the *current* code matches upstream on effectively everything
outside those 57 points. The two differences the third audit had left open were fixed in `0.1.42`. That work found
two more, both about where zfs's own words are shown; `0.1.43` fixed those and took up upstream's reading of the
capacity answer. Its two entries are what it left different: zfs's words are written out once the command has
finished, where upstream's zfs writes them itself (port mechanism), and a capacity answer holding more than a
capacity is refused, where upstream prunes by its first word (safety).

## What this cannot yet claim

Upstream sanoid's real advantage isn't in its code — this audit found real, fixable bugs
in it (the manual DST math, `alarm()`'s timeout crashing the whole process instead of just
risking an orphan, the shell-metacharacter locking quirks) alongside plenty that's simply
fine. Its advantage is **years of diverse,
independent, real-world exposure**: edge cases nobody thought to write a test for, because
someone's actual pool hit them first. That kind of evidence isn't something an audit can
manufacture, however careful — only time and diverse use produce it, and pysanoid has had
28 days of git history and whatever length of real production time you already
know and I don't have independent visibility into.

I'd also flag plainly: this whole audit is the work of one reviewer (me), however
adversarial I tried to be, differentially tested against one pinned upstream commit. That
carries real weight — it's not guesswork, every claim above traces to a concrete test run
— but it is a different, narrower kind of evidence than upstream's field record, and I
don't want to conflate the two.

The history pass (next section) borrowed what can be borrowed of that record: what sanoid's
users reported about the same code, checked against pysanoid. It adds field evidence to the
register; it doesn't give pysanoid field time of its own.

## What upstream's users reported

Upstream has no commits after the pin (`d39b51a`, 2026-06-05, is still its newest), so the
history pass read its issues and pull requests instead: all 1,061 of them as of 2026-09-26 (709
issues and 352 pull requests, 158 still open). About 300 concern syncoid or findoid alone, and many
more are packaging, documentation or feature requests; the rest were read against pysanoid, about
65 in full with their discussion. The pin also carries eight fixes made after upstream's 2.3.0
release, the version distributions ship, so reports of 2.3.0's own bugs (duplicate snapshots under
`recursive = zfs` from a file-handle mix-up, #1014 and #1020, fixed by #1023) don't reach pysanoid.

**What it found.** One problem pysanoid shared: a section placed above the recursive section it
belongs under (#1021, fixed in `0.1.37`, above). Everything else users reported about the code
pysanoid ports was already a register entry or doesn't apply to it. Nine entries now cite the
reports behind them:

| Register entry | Upstream reports | What sanoid's users saw |
|---|---|---|
| A section above its recursive parent is refused (new) | #1021 (open) | Hourlies kept appearing on zvols set to keep none, until an NVMe array filled; moving the sections below the recursive one fixed it |
| A `use_template` naming no template is an error | #702, #938, #1038 (open); pull request #1039 (open) | Datasets silently ran on the defaults, the template named with a `template_` prefix or in quotes. The open fix would only warn, its reviewers preferring too many snapshots to none; here the defaults' retention can destroy what the intended template keeps, so it stays an error |
| Every value is trimmed, and a repeated key reads as its first value | #834, #901, #952 | A trailing space after `recursive = yes` silently turned recursion off, and a repeated `autosnap = no` in a template read as on, taking snapshots on a replication target. Upstream fixed both where those users hit them (a section's own values, a named template); the register covers the places it didn't |
| A failed snapshot exits non-zero, and a bad value costs only its type | #671 (open), #394, #440 | Exit 0 after a failed snapshot, so nothing was alerted; and in 2019 upstream's own date arithmetic died on the 1st of the month, stopping every snapshot |
| A hook that outlives `script_timeout` is killed, and the run carries on | #1030 | A post-snapshot hook running syncoid outlasted the default 5 seconds; hooks stopped running and dailies were taken two or three times a day until the timeout was lifted |
| `fcntl.flock` locks | #1063 (open), #790 (open), #391 | Take and prune started in the same minute failed on nearly every run; lock files left behind or written by two runs at once produced errors |
| A cache that can't be written never stops a run | #1063, #534 | The same race died on the cache's rename; a run by a user who couldn't write the cache died there |
| Preferred times from zoneinfo | #155, #395, #406, #409 (open) | On the night of an autumn change, a daily every minute for an hour, 60 per dataset (2017); the DST code has been rewritten more than once, and its maintainers advise running in UTC |
| `SANOID_SNAPNAME` as upstream sets it (parity) | #798 (open) | The post-snapshot hook's value gained the dataset in 2.1.0, though upstream's README still describes the bare name; pysanoid keeps upstream's behavior |

New tests run pysanoid on the configurations users posted in #1021, #702, #938, #1038, #834 and
#952, and on #1030's two runs in a row; `dev/e2e_subprocess.py` starts take and prune together, as
#1063's cron did. The other reports' cases were already pinned by earlier tests.

**Settled.** Issue #202 (2018) is where upstream's snapshot cache and its lock took their shape.
Upstream's maintainers describe the lock as keeping one process at a time listing snapshots, so an
overloaded system isn't buried in listings, weighed against a snapshot being taken late; they also
warn that a cache missing changes can over-prune. pysanoid lists zfs itself only when another run
holds the lock and there is no readable cache (upstream would read that as no snapshots and take
everything again), or when the run directory is unusable, which it reports on every run. A fresh
listing is never staler, and production runs take and prune one after the other, so the question unit 7
left open closes with no change.

**Found, and left as upstream has it.** A pull request (#651, closed unmerged) points out that
snapshots taken while the clock ran ahead carry future creation times. Once the clock is corrected,
take sees them as newest and takes nothing of their type until real time passes them, and prune
can't age them out; pysanoid does the same as upstream and, like it, says nothing: decided on
2026-09-27 and again on 2026-10-10 (see `0.1.41` above). It needs a clock set wrong into the
future; the production host keeps time with NTP. `pysanoid_alert.py`'s freshness check, which is not part of the
port, mails a type whose newest snapshot is dated ahead. *Reversed in `0.1.53` (DEV-051), on #1119's report and on prune's
side of it, which this decision didn't weigh; see "The 2026-10-28 review".*

**Lessons for running pysanoid**, from what sanoid's users ran into:
- Run one scheduler. Reports of duplicate snapshots and lock errors came down to a package's own
  timer or cron job running beside the user's (#557, #790), and upstream's Debian package enables its
  timer on install (#772). The production host has only `pysanoid.timer` (checked 2026-09-26); pysanoid's locks
  can't see upstream's, so the two must never share a run directory.
- Take and prune one after the other, as the production units do (#1063).
- Name a template without `template_` and without quotes (#938, #1038); pysanoid refuses either.
- Keep a dataset's own section below any recursive section above it in the tree (#1021); pysanoid
  refuses the other order.
- Give each dataset one section that takes or prunes it, named after the dataset: a second one, or a
  section's `path =` inside a recursive section's tree, would apply both retentions (pysanoid refuses
  either since `0.1.38`).
- Give a long hook a `script_timeout` long enough for it, or 0 for none (#1030). The default is 5
  seconds; past it pysanoid kills only the hook.
- Turning `autosnap` off doesn't make a dataset's snapshots age away: prune always keeps the
  configured number of each type (#707), here as upstream.
- A recursive section with `process_children_only` takes nothing for the parent itself (#1088),
  here as upstream; a second section with `path =` for the parent can, since the parent's own
  section neither takes nor prunes it.
- Listing snapshots slows as their number grows or memory runs short, and a slow listing is what
  lets runs overlap (#202, #995, #1045); production's counts are small.

**What the record can't show.** Most of upstream's traffic is about syncoid; reports come from the
users who wrote them, on versions going back to 2015; and silence isn't evidence that something
works.

## Recommendation

Tag it. Describe the tag's scope accurately if it's ever shown to anyone else: this is
1.0 of pysanoid's take/prune port, audited module by module against upstream master and
found either matching or deliberately (and narrowly) safer everywhere a difference exists
— not a claim that it has sanoid's own field record.

**Update, 2026-09-22:** the differential-testing harnesses this audit relied on are now
preserved in the repo as `dev/` (see `dev/README.md`), rebuilt with a shared, upstream-
version-safe extraction framework rather than the original scratchpad's hardcoded line
numbers. Every one of the seven targets was independently re-verified end to end while
rebuilding it (hand cases plus a real fuzz run, not just written and trusted), and all
seven are clean. The one gap flagged at the time: prune_snapshots and take_snapshots —
the actual destructive/protective core — didn't have an equivalent mass-fuzz target yet.

**Update, 2026-09-22 (later the same day):** that gap is closed. `dev/diff_prune.py` and
`dev/diff_take_snapshots.py` now mass-fuzz prune_snapshots and take_snapshots directly
(1,316 and 1,118 scenarios respectively, hand + fuzz across two seeds each, all clean —
see the differential-results table above), and `dev/diff_hooks.py` does the same for the
`script_timeout`/hook-execution machinery both of them share. This is not a clean "ran it
and everything was already fine" story: building these three targets found and fixed two
more real bugs (the `script_timeout` gate direction and `post_snapshot_script`'s
`SANOID_SNAPNAME` prefix — see "What was actually wrong, and got fixed" above), which is
itself the strongest evidence yet that this kind of testing is worth keeping around as the
code changes, not a one-time audit exercise. `dev/` is now genuinely complete for
sanoid/pysanoid's own take/prune logic — the only remaining, deliberately out-of-scope
gap (`--monitor-*`/syncoid/findoid; the raw ini grammar was one too until `0.1.29` gave it a
target) is explained in `dev/README.md` and doesn't bear on this recommendation.

**Update, 2026-09-26:** the production host runs Python 3.11.9, and until `0.1.35` every test, fuzz run and
end-to-end check had run on 3.14. All of them now run on 3.11 as well (Homebrew's 3.11.16; the
releases after 3.11.9 are security fixes only). On `0.1.34` everything in them already passed
there; the one difference found, pathlib's (see `0.1.35`), is one none of them exercised, which is
the case for running the gate on the version production uses, not only on the newest.

**Update, 2026-09-27:** the long-horizon simulator (`dev/simulate_long_horizon.py`) is the one
tool here that runs take and prune together, run after run, on the production config: the check for
a regression that only shows across runs. Nothing had run it since `0.1.25`, because `dev/run.py`
didn't, and it had broken without failing. Every comparison of take with upstream raised an
error, tank/software's recursion ran without its child, and its zfs accepted a snapshot name that
already existed, so the `dst_` retry never ran. Its only check was for a pile-up, never for
over-pruning, and even that needed every survivor past the window. Rebuilt, it checks every run
for over-pruning, missed and extra slots, pile-ups, take's return value and unreported failures,
and compares sampled runs with upstream. It is now a `dev/run.py` target, and each check has
been shown to catch a deliberately broken pysanoid. On the production config it finds nothing wrong in 4
years of hourly runs across 8 clock changes (35,064 runs; prune the same as upstream in all 330
sampled), nor on a fixture of every type and kind of recursion over 400 days with 1 zfs call in
1,000 failing. It also shows what a run in each pass of a repeated hour does: at the second 01:00
of 25 October, zfs refuses tank/media's second hourly of that name and pysanoid takes
`autosnap_2026-10-25_01:00:00dst_hourly` instead: the `dst_` retry upstream built for an hourly on
the day summer time ends (see the register). It counts as an hourly and ages out like one.

**Correction, 2026-10-11:** the last part of that update first read as what 25 October will do in
production. It is what the simulator's schedule does, a run on every hour of real time and on the exact
hour, and the production timer does neither. `pysanoid.timer` is `OnCalendar=hourly` with
`RandomizedDelaySec=120`, and systemd reads it in the system's time zone, which in production has
summer time. systemd matches a calendar time against the wall clock, and a wall-clock time that
comes twice elapses once: 01:00 gets one run where real time has two, and no run starts in the
hour's other pass (`calendarspec.c` and systemd's own test of it, "02:30 happens only once", the
same at v252 and at v254, which the production host runs; systemd's issue #8647, open since 2018, reports the
same of a timer that runs every minute). The production host's own systemd says so of this night: asked on
2026-10-11, `systemd-analyze calendar --base-time='2026-10-25 00:30:00' --iterations=3 hourly`
lists 01:00 UTC+1, then 02:00 UTC and 03:00 UTC, with no second 01:00. Each run also starts up to
two minutes after the hour, not on it. So production should take one hourly in the repeated hour, in
its first pass, and none named `dst_`, and what that night should show is tank/media's hourlies
of 01:00 UTC+1 and 02:00 UTC, two hours apart. The `dst_` retry is still what a scheduler that goes
by real time needs: started on the same minute and second in both passes, its second run finds
the name taken.

**Update, 2026-10-11 (lint):** the gate's lint had the blind spot its tests had before `0.1.35`.
ruff was run with no target version, which it takes as Python 3.10, so what it reports only for
later Pythons never showed. The user's editor, which lints for a later one, showed it:
`timezone.utc` in three places in `pysanoid.py` (UP017), and the same in two of the tests. `0.1.44`
is the fix (see above). A `ruff.toml` beside `pysanoid.py` now sets the target to Python 3.11,
production's, for the gate and for an editor alike: a lower target hides what 3.11 offers, and a higher
one asks for what only a later Python has (for 3.13, two `Generator` annotations without their
defaults). The rules stay ruff's defaults. Under that file the `0.1.43` sources show the five
findings and `0.1.44`'s none.

## The next audit: a plan

*Written 2026-09-28, after `0.1.39`'s first day in production.*

The two audits so far asked where pysanoid differs from upstream, and that question is answered: every
difference is fixed or in the register (50 entries, each with its evidence), and outside the register the
differential gate finds none. Most of the latest findings came from outside that comparison: sanoid's users
(#1021), the C library (`$TZ`), Python itself (3.11's zoneinfo hanging or crashing on a damaged zone file) and
the production setup (units with no start timeout). As pysanoid moves further from Perl, matching it says less
about whether pysanoid is right. The next audit asks whether pysanoid does what it promises, on the Python and
the system it runs on. The working rules stay: every finding is fixed with a test that fails before and passes
after, or registered with its evidence, and the gate runs under production's Python as well as the newest.

1. **Write down the promises.** A short list of what pysanoid guarantees, each beside the test that checks it.
   From the register and the code:
   - prune never leaves fewer snapshots of a type than its count, and never destroys one younger than its
     type's age;
   - prune checks every setting it will read before its first destroy, and destroys nothing without its lock;
   - take takes each due snapshot once per run, and a failure costs only its section and type, is reported and
     makes the exit non-zero;
   - snapshot names carry the local time as glibc reads `$TZ`;
   - nothing a run reads (the config, the caches, a zone file, zfs's output, the environment) stops it, except
     where stopping is the safe choice and is documented (a config error, a child lookup that comes back
     short), and no run outlives its unit's timeout.

   A promise with no test is the audit's first finding.

2. **Test the promises, not the parity.** The long-horizon simulator already checks the retention promises
   run after run on the production config, across years and clock changes; it becomes the main check. It needs random
   configs, not only the production config and one fixture; random failures, including a run the timeout stops halfway
   through a prune, and the runs after it; and clock trouble: a step back or forward, a clock set ahead and then
   corrected (#651's future-dated snapshots), and runs missed while the host was off, which the timer makes up
   with `Persistent=true`.

3. **Python in production.** The production host is a TrueNAS appliance, so its Python stays 3.11.9 until TrueNAS 26, expected
   about December 2026 and ready for production about spring 2027: through the clock changes of 25 October and
   28 March. `0.1.39` found 3.11's zoneinfo hanging or crashing on a damaged zone file, which 3.14 refuses. The
   same question goes to the rest of the standard library pysanoid relies on, fed damaged or odd input under
   3.11: starting commands (fork failures, signals, a closed pipe), file locks, renames on a full or read-only
   disk, and names that aren't UTF-8. Before the production host moves to TrueNAS 26, the whole gate runs under the Python it
   ships.

4. **The production setup.** Done on 2026-09-28: both units stop a run after 30 minutes. A hung `zfs` call (a
   suspended pool, say) used to leave take running for good, and with it the timer and prune; now the run is
   stopped and the unit marked failed. A failed unit used to be seen only by someone who looked;
   `pysanoid_alert.py`, written on 2026-10-05 and outside the port, makes it a mail. Both units name a handler
   with `OnFailure=` that mails the failed run's log, and a daily TrueNAS Cron Job mails when a snapshot type
   `sanoid.conf` asks for has no recent snapshot, which is what removed units or a stopped timer look like and
   no unit can report. TrueNAS sends the mail to its administrators (`midclt call mail.send`); its API has no
   call that raises an alert of one's own, and the production host keeps no log server, by choice. Still open: installing it
   in production, and seeing one mail of each kind arrive.

5. **What stays of the Perl comparison.** The differential gate keeps running on every change: about 15
   minutes per Python, and it still catches regressions (it caught a slip in `0.1.39`'s own harness work). New
   differential work goes only where matching upstream is a contract: how a `sanoid.conf` is read and merged,
   the retention arithmetic, and snapshot names, which other tools read. Where pysanoid differs on purpose, a
   new known class in a harness weakens that harness; those behaviors are tested against the promises instead.

**Run on 2026-10-10.** See `0.1.40` under "What was actually wrong, and got fixed": three ways, shared with
upstream, for prune to destroy what the config keeps, each fixed; a list of what was found and not yet changed; and
what held (no failure of a file or of zfs, one at a time, made a run crash or over-prune). Its fault injection and
its config fuzz were one-off probes and are not in `dev/`; item 2's wider simulator (random configs, clock steps,
a prune the timeout stops) is still to build. `0.1.41` fixes two of the things on that list and leaves one as
decided; the timer's `Requires=` line was removed, as upstream's Debian packaging has none. `0.1.42` fixes the last
two, which leaves nothing on that list open. `0.1.43` closes the two differences `0.1.42` itself found, in where
zfs's own words are shown.

**Built on 2026-10-11.** The fault injection and the config fuzz are `dev/run.py` targets now (`fault_sweep`,
`config_fuzz`), and item 2's wider simulator is built as a third (`trouble`): random configs, any failure of
anything, a run killed partway through a prune and the runs after it, a clock set ahead or back, runs missed
while the machine was off, and a pool filling up. Where the plan stands:
1. The promises, each beside the test that checks it: **not written.** The audit went straight to breaking
   things. The rules every simulated run is now judged by (`dev/lib/model.py`, `dev/lib/audit.py`) state the
   first promise whole, prune's count and age, and parts of the others: that prune refuses a bad setting
   before its first destroy (`config_fuzz`), that take takes each due snapshot once and under the name of the
   local time, that what a failure left untaken was reported (`fault_sweep`), and that nothing a run reads
   ends it with a traceback. Not among them: that prune destroys nothing without its lock, which
   `dev/diff_prune.py` checks with a run directory no lock can be made in; that a failure costs only its
   section and type, which `test_pysanoid.py` checks case by case; names under the forms of `$TZ` that
   glibc reads, which the tests and `dev/e2e_subprocess.py` check; and that no run outlives its unit's
   timeout, which nothing has tested.
2. Test the promises, not the parity: done as far as one machine and a stand-in zfs go.
3. Python in production: the gate runs under 3.11 as well as the newest. TrueNAS 26's Python is still to come.
4. The production setup: `pysanoid_alert.py` and its units were installed in production on 2026-10-11. Whether a mail of
   each kind has been seen to arrive is not recorded here.
5. The Perl comparison: as planned. The three new targets have no known class that permits a difference
   from upstream, since they don't compare with it; the one class they register is checked both ways.

**Field time, meanwhile.** It is the one kind of evidence an audit can't make. Dates worth a look in production:
29 September, the first weekly under `0.1.39`, taken by the midnight run; 1 October, the first monthly at
midnight, and tank/vm's first pruned daily in the 14:00 or 15:00 run; 25 October, the night summer time ends,
when the production timer should start one run for 01:00, not two, leaving tank/media's hourlies of 01:00 UTC+1 and 02:00
UTC two hours apart and none named `dst_` (this first named `autosnap_2026-10-25_01:00:00dst_hourly` as the snapshot to expect; see the
correction of 2026-10-11 under Recommendation; no deploys 18 to 26 October); and 28 March 2027, the spring change,
when no hour repeats and no `dst_` snapshot is expected (asked the same way on 2026-10-11, the production host's systemd lists
the runs after 00:30 UTC that night as 02:00, 03:00 and 04:00 UTC+1, an hour of real time apart).

**25 October 2026, seen in production** (`0.1.45`; the journals of both units from 23:00 UTC+1 to 09:30 UTC, tank/media's
hourlies with their creation times, both read the next morning). As predicted on 2026-10-11:
- One run for 01:00: take started at 01:00:19 UTC+1, and the next at 02:02:03 UTC. No run started in the repeated
  hour's second pass.
- tank/media's hourlies of 01:00:20 UTC+1 and 02:02:04 UTC were made 7,304 seconds apart; every other pair that
  morning is 3,494 to 3,669 seconds apart. None is named `dst_`, and the names carry the local time of each run.
- Pruning went by real time. The 02:02 UTC run destroyed the hourly of 14:02:13 UTC+1 the day before, 12 hours 59
  minutes old, and kept the next, 11 hours 59 minutes old; each run after it destroyed one. tank/media kept its 12
  hourlies. The midnight run took the dailies, and pruned those past 30 and 7 days. backup stayed deferred.
- All 22 runs, 11 of each unit, ended successfully. Nothing from either unit or the alert units was logged at
  warning or above, and neither service was left failed.

## The third audit: where it ended

*Written 2026-10-11.*

**What was asked.** After two audits of structure, one "focusing strictly on correctness, bug hunting, and data
safety": that the port "safely handles real-world operations without risking the underlying data", and not only
that it matches the Perl. Four tasks: trace every error path (an unexpected state, a missing file, a full disk)
and say whether the run ends safely; find where the Python runs but breaks what the Perl meant; hunt the pitfalls
a port brings with it; and see that files, locks and child processes are let go of, in a crash too.

**What it found, and how each ended.** The first four destroy snapshots the config keeps.

| Finding | Upstream has it too | Ended |
|---|---|---|
| A child's own section, built from the defaults when its recursive parent's lookup didn't list it | yes | fixed, `0.1.40` |
| A cache dated in the future never expires | yes | fixed, `0.1.40` |
| A snapshot cache cut short inside its last line | yes | fixed, `0.1.40` |
| A snapshot listing killed inside its last line | yes | fixed, `0.1.41` |
| A count too long for a number: a traceback, at 320 digits in the middle of a prune | no | fixed, `0.1.41` |
| What zfs prints that isn't UTF-8, read as a command that could not run | no | fixed, `0.1.42` |
| Snapshot names carrying the time the run began | no | fixed, `0.1.42` |
| zfs's own words, the failure lines, the capacity answer, a failed hook's warning and how a hook is started | no | as upstream's, `0.1.43` and `0.1.45` |
| The freshness check calling a snapshot dated ahead fresh | (not part of the port) | fixed in `pysanoid_alert.py` |
| A newest snapshot dated ahead holds its type back, in silence (#651) | yes | left as upstream has it; set aside since `0.1.53` (DEV-051) |
| A pool-capacity query that fails ends the prune run | yes | left as upstream has it |
| A child lookup that fails having listed nothing reads as no children | yes | left as upstream has it; a child with a section of its own no longer depends on it (`0.1.40`) |
| The timer's `Requires=` line | in INSTALL.md's unit only | removed, as upstream's Debian packaging has none |

Of the tasks, the last found nothing: locks and file handles are let go of on every path, and hooks and zfs don't
inherit them. Nor did it find a mutable default argument or an exception handled in the wrong scope. The rule the
three "left" rows go by was restated during it: pysanoid mirrors upstream 1:1 unless that compromises data safety.
Two reports that this audit had added, where upstream is silent, were taken out by that rule; two closures of
wording made in September were reversed by it, at the user's word.

**What held.** Every file operation and every zfs call of a run, failed in turn or the process ended there, with
a clean run after: no traceback and no destroy that breaks the retention, on every version from `0.1.41`. The
probe that showed it was run again on `0.1.45` (7,070 trials under the two Pythons), as was the config fuzz
(37,500 runs under each), before either was rebuilt.

**What it leaves in the gate.** The two probes were scratch files, and one had already gone wrong unnoticed: its
stand-in for zfs read a hook's command line the way `0.1.44` wrote it. Both are `dev/run.py` targets now, with
the simulator the plan asked for. `dev/run.py --fuzz` now has 18 targets and 36 runs, all passing under Python 3.14
and 3.11:
- `fault_sweep`: every event of a run failed every way it can, 4,299 trials under Python 3.14 and 4,845 under 3.11,
  where a run has more events, each of three rounds. On `0.1.39` it finds the first row of the table above, a
  child's weeklies destroyed below their count; on `0.1.39` and `0.1.40` the fourth, a newest daily and hourly
  destroyed inside their window.
- `config_fuzz`: 4,000 random configs and 15 hand cases, six runs each. On `0.1.40` its hand cases find the fifth
  row.
- `trouble`: the production config and the long-horizon fixture through scripted trouble, and 160 random scenarios: 33,643
  runs under Python 3.14, 32,499 of them judged exactly. On `0.1.39` it finds the second row: after one round with
  the clock set ahead and a failure in it, and the clock set back, the runs go by the caches that round left.

`dev/mutants.py` checks the three: `pysanoid.py` broken on purpose, one line at a time, 24 ways, each of which must
fail one of them. All 24 do. Four stood at first, and each showed a hole in the new tools, since closed
(`dev/README.md`, "Shown to fail").

**Where it runs.** `0.1.45` has been in production since 2026-10-11, with `pysanoid_alert.py` and its
units installed the same day.

**What remains.**
- The promises, each beside the test that checks it: the plan's first step, not written (see "Built on
  2026-10-11" above).
- The next TrueNAS's Python: the user expects TrueNAS 27, with Python 3.13, just before 28 March 2027. Before the production host
  moves, the whole gate runs under it.
- A mail of each kind from `pysanoid_alert.py`, seen to arrive.
- One night worth a look in production: 28 March 2027. 25 October went as predicted (see "Field time, meanwhile").

**What it can't claim.** All of it ran against a stand-in zfs on one machine: nothing ran on a real pool, and
nothing in production but pysanoid itself. A sweep trial has one fault; the simulation has many in a scenario, never
two in one run. Nothing has tested that a unit's 30-minute timeout stops a run. And it is still one reviewer's
work, as the first two audits were.

**25 October 2026, while the register moved to `DEVIATIONS.md`** (`0.1.46`; only comments changed in `pysanoid.py`).
The register's cache entry, split into DEV-039a to DEV-039e, was checked claim by claim against upstream's
`getsnaps`, `take_snapshots`, `addcachedsnapshots`, `removecachedsnapshots` and `getchilddatasets`, and three
harnesses' hand modes passed against real Perl (`snapshot_enumeration`, `snapshot_cache_mutation`,
`dataset_cache`):
- DEV-039a held as written. A fresh cache upstream can't read, and a missing one while another process holds the
  update lock, both reach an unchecked `open` and read as no snapshots; the "then died" of 2026-09-25 is the cache
  add's `copy(...) or die` on the unreadable file, so a `--cron` run never reaches its prune.
- DEV-039c's reasoning was corrected. While the empty, fresh cache upstream's remove writes lasts, take takes
  another snapshot of every type and prune destroys nothing: the narrow path P4 accepts, where failing instead
  would be worse. Here leaving the cache missing costs one zfs listing. The cache add's harm is its die, which
  skips a `--cron` run's prune.
- DEV-039e had been called harmless. One of its two parts guards a path to data loss, and it is
  reproduced. Upstream's cache add keeps the lines a killed child lookup printed, the last of which can be a name
  cut short. A stand-in `zfs` printing `tank\ntan` and killing itself, run through upstream's own
  `addcachedsnapshots` for a `recursive = zfs` snapshot of `tank`, cached `tan@autosnap_c_daily` dated now.
  Read as both programs read a cache, a pool `tan` with `daily = 3` then had its daily skipped by take, and prune
  destroyed three of its five dailies, not two, leaving 2 real ones. It needs a dataset whose whole name is a
  prefix of the parent's and zfs killed within those first bytes of a line. pysanoid keeps none of a killed
  lookup's lines, as `test_add_cached_snapshots_leaves_out_the_children_of_a_killed_lookup` pins, so no change
  was needed. The reproduction was scratch work and is not in `dev/`.

The other entries were checked the same way as they moved. Five that rested on a reading of upstream's code, a
"confirmed against real Perl" with nothing to keep it confirmed, or a one-off, now have checks that run every time.
Each new test or check was first seen to fail against a copy of pysanoid broken to behave as upstream does:
- DEV-043, a command that can't start for want of a process. Under RLIMIT_NPROC = 1, upstream's own `runscript` and
  a piped open, as its listings use, are still waiting after 12 seconds; pysanoid's hook fails in a tenth of one,
  and a whole run stops at its first listing with a FATAL (`dev/diff_hooks.py`, a standing check;
  `dev/e2e_subprocess.py`). The entry now names that last case, which it had left out. A copy that retries the fork
  every 5 seconds, as Perl does, hangs the end-to-end check.
- DEV-044, Ctrl-C. A hook sends SIGINT to its process group, as a terminal does: under upstream's `runscript` the
  hook dies and the run carries on; pysanoid dies from it. A whole run on two datasets destroys nothing, where a
  copy that ignores SIGINT while a command runs, as Perl's `system()` does, exited 0 having destroyed all four
  dailies the run was to prune (`dev/diff_hooks.py`; `test_pysanoid.py`; `dev/e2e_subprocess.py`).
- DEV-045, a zfs that can't be run. The register said upstream's shell pipe reads it as a listing that failed. It
  doesn't (confirmed against real Perl): with no shell metacharacter Perl execs the command itself, the failed exec
  leaves the pipe empty and `$? >> 8` at 0, so the listing reads as one that worked and found nothing, with no
  retry. The cache is written empty and fresh, take tries every type and only warns, and the run exits 0. Only a
  command holding a metacharacter reaches `/bin/sh`, which exits 127. The conclusion, a silent loss of protection,
  stood; the account of it is corrected, and a test pins pysanoid's FATAL.
- DEV-046, what a hook inherits. Started as systemd starts a service, upstream's hook gets SIGPIPE ignored and no
  `LC_CTYPE`, pysanoid's SIGPIPE at its default and `LC_CTYPE=C.UTF-8`, whether pysanoid execs the hook's words or
  runs it through the shell (`dev/diff_hooks.py`; `test_pysanoid.py`, its probe writing into a pipe whose reader
  has gone). The first version of the check ran its hook only through the shell, so a copy breaking the other path
  passed it; it now runs both.
- DEV-047, zfs's own words. The 0.1.43 comparison of upstream's whole script with pysanoid, 29 cases byte for byte,
  is rebuilt as `dev/run.py` target `zfs_output`: both programs run on the same config and fake zfs, stdout and
  stderr on one pipe, and the whole output is compared, with the time in new names, Perl's ` at <script> line N.`
  and the datasets' hash order made the same first. 17 hand cases and 180 fuzzed scenarios across three seeds match
  under Python 3.14, and the hand cases and one seed's 60 under 3.11, with two known classes checked by their rules (a destroy's stderr before its stdout,
  DEV-047; a snapshot that fails, DEV-028). Five broken copies of pysanoid's output each fail between 1 and 15 of
  the hand cases. It found that for `zfs snapshot` upstream also shows stdout first in practice: `tee_stderr`
  passes stderr on through a process of its own (8 of 8 runs; a race, so the target doesn't rely on it).
- DEV-049 and DEV-010, an empty here-doc read as blank in either config file: one test now pins both.

## The fourth audit: security and data safety

*Written 2026-10-25.*

**What was asked.** A review as an application-security engineer: data safety and file I/O, input validation and
injection (commands, regular expressions), Perl-to-Python logic (context, truth, scope), dependencies, and the
documentation against the code.

**What it found.** Nothing that puts data at risk in `pysanoid.py`. There is no `eval`, `exec`, `pickle`, YAML or SQL,
and nothing outside the standard library; every zfs and zpool call is an argument list, and a hook's values reach it
in its environment. No lock is taken inside another, recursion copies only strings and booleans, toggles are booleans
before any truth test, and every test and harness DEVIATIONS.md names exists. Four things were already decided and
were not reopened: a timed-out hook's children, a stale cache's count, a clock that ran ahead (#651), and a failed
capacity query.

- **Fixed, in `pysanoid_alert.py`:** take and prune skip every section whose name starts with "template", as
  upstream's do (its lines 156, 217, 348 and 483), while only `template_` sections are read as templates. A pool
  named `templates` loads and is never snapshotted or pruned, and the freshness check, which asks pysanoid's own
  plan what to expect, called it fine. `pysanoid.py` keeps upstream's behavior: nothing is destroyed. The check now
  plans those sections under another name and mails each type pysanoid would take of them, unless another section
  takes it of the same dataset. 7 new tests, failing before the change; four one-line breaks of it each fail one.
- **Fixed, in the host's own install script (outside this project):** `rsync -a` installed the units, and a database backup script, with their source's
  owner, so whoever owns the copies on tank/software owned what root runs. Each is now given to root after the copy,
  as the scrub timers already were.
- **Fixed, in `pysanoid_alert.py`:** the mail, up to 100 KB of a failed run's log, was `midclt`'s last argument, and
  so in a command line any local user can read (`ps`, `/proc/PID/cmdline`) for as long as `mail.send` took. midclt
  reads arguments only from its command line, so a short launcher (`python3 -I -c`) takes the mail on stdin, adds it
  to `sys.argv` and runs midclt's own script with `runpy`: midclt sends, prints and exits as before. midclt is found
  on PATH, as exec finds it, never in the working directory, and `-I` keeps a planted module there from loading. 6
  new tests; the two that look for the mail in a command line fail against the committed script, and five broken
  versions each fail one.
- **Left to the user, on TrueNAS:** who can write pysanoid's install directory (the code root runs, and a config whose
  hooks and counts decide what is run and destroyed).
- **Fixed, as `0.1.47`:** four of the module's fifteen regexes took quadratic time in Python on a line built for it,
  where Perl's matcher rejects a snapshot line without "creation" at once: the snapshot line, the type in a
  snapshot's name, the ini grammar's `key = value` and a value's trailing comment (a timing sweep over crafted
  families; 4,000 units: 0.3 to 1.5 s, growing a hundredfold for ten times the length). Only a damaged cache or
  root's own config could hold such a line; zfs's names are 255 bytes at most. The snapshot line is now read in two
  linear steps (`_snapshot_line`: the last `ly`, tabs and "creation", then the last `@` before it, which is what the
  greedy regex takes), the type run by run (`_snapshot_type`), the parameter with possessive quantifiers where giving
  characters back can't make a match, and the trailing comment from the start of a whitespace run only. Results are
  unchanged: the old regexes and the new code agree on 2.5 million random strings under Python 3.14 and 3.11, and
  the tests keep the old regexes as the reference (60,000 seeded strings and 33 hand cases). Three tests bound a
  crafted line to a second (12.8, 8.6 and 7.8 s before). Of 11 one-line breaks, 9 fail a test; the other 2 can't
  change a result (each widens a search by a position that always holds an `l`). `dev/run.py --fuzz` passes all 44
  runs under Python 3.14 and 3.11, and e2e 77 under both. `dev/mutants.py` had skipped one mutant since `0.1.46`,
  whose comment change it still looked for; with the line corrected all 24 are caught.
- **Fixed, as `0.1.48`:** opening a lock file emptied it, so a process that found the lock held wiped the holder's
  PID. The file is now opened without emptying it, and emptied for the PID only once the flock is held. Only the
  flock decides who holds the lock; the PID is a note for a person looking. A test fails on `0.1.47` (the holder's
  note was gone), and three breaks of the fix each fail one. `_perl_gt_zero`'s docstring spoke of Latin-1, where the
  text is UTF-8 with surrogateescape; it now says why the comparison against "0" still gives the bytes' answer
  (checked on 300,000 random byte strings). `dev/run.py --fuzz` 44/44 and e2e 77 under Python 3.14 and 3.11.
- **Closed with no change:** an unset `PATH` becomes `:/bin:/sbin`, putting the working directory first, as
  upstream's does (parity; systemd and cron always set it, and zfs is found while it is still set). Cache files are
  written without fsync and through a symlink, as upstream's are: on ZFS a rename can't land before the data written
  ahead of it, a file cut short is no cache (DEV-036), a cache missing lines makes prune keep more, and the
  directories are root's.

## The fifth audit: parity from a blank slate

*Written 2026-10-25.*

**What was asked.** An adversarial check of the port against upstream from first principles, without the earlier
audits' conclusions: Perl's scoping, truth, context and regex behavior traced to what the Python does, the edge
cases where Python's types and standard library read input differently, and the ways a run that passes can still
fail under stress or with empty input.

**How.** Upstream's `sanoid` and `pysanoid.py` read whole, then upstream's whole script (Perl 5.34, Config::IniFiles
3.003000) and pysanoid run as programs on copies of one pool, through a fake zfs written for the purpose, outside
the repo's harnesses: 31 scenarios under Python 3.11 and 3.14, among them the production `sanoid.conf` over two runs,
hooks that fail, veto or are held back, `recursive = zfs`, `skip_children`, a child section overriding its
recursive parent, every count 0, an empty pool, and names a type can't be read from. Destroys, snapshots taken,
each hook's `SANOID_*` variables and the output were compared. Not covered: two runs at once (the sandbox forbids
`ps`, which upstream's locks need) and times near a clock change (the real clock was used).

- **Fixed, as `0.1.49`:** upstream sets a hook's variables in `%ENV` and deletes them after the hook, so a
  `SANOID_*` variable sanoid inherited is gone once a hook has set it; pysanoid gave each hook its variables in a
  fresh environment, and every hook saw what it inherited (P9). The defaults file's version was read with `int()`,
  which took `1_0` as 10 where upstream reads 1 and refuses the file; it is now plain digits (DEV-050). Four new
  tests (one with ten cases), failing on `0.1.48`; five one-line breaks of the fix each fail one.
- **Corrected in DEVIATIONS.md:** DEV-032 now says a comment alone (`frequent_period = # x`) is read as blank, as the
  code always read it. Under `--prune-snapshots` upstream reads it as 0 and destroyed 6 of 10 frequent snapshots a
  few minutes old; pysanoid keeps them.
- **Confirmed, with no change:** the run's default mode, prune's arithmetic and its scan of every key ending in `ly`,
  snapshots just taken staying out of that run's prune, hooks read from the section named after the dataset, the
  snapshot-line and type readings, take's comparison with a clock read in fractions, frequent slices, and the
  recursion's copy rules. Every other difference seen is registered: DEV-012, DEV-017 (a space in a recursive
  section's name: upstream finds no children), DEV-028, DEV-030, DEV-032 and DEV-042, and a failed capacity query
  ending the prune run (left as decided).

## The sixth audit: stress, scale and Perl's behavior read differently

*Written 2026-10-25.*

**What was asked.** An adversarial pass at `0.1.49`: property tests at the boundaries of the most complex functions,
where the code fails at scale, places where Perl's implicit behavior was translated into Python that may read input
differently, and how to fuzz for the rest.

**How.** `pysanoid.py` read whole; 17 properties (seeded draws biased to the edges: 2**31, 2**63, 2**64, 10**309,
10,000-digit counts, NUL, lone surrogates, other scripts' digits, every Unicode space) run 20,000 times each under
Python 3.14 and 3.11, outside the repo; upstream's whole script beside pysanoid on a fake pool for what they found;
timings with zfs patched out.

- **Fixed, as `0.1.50`:** a toggle was trimmed with `str.strip()`, which also removes a no-break space, NEL, U+2028,
  U+3000 and U+001C-001F, where Perl's `\s` does not. `autoprune = no<NBSP>` read as "no", silently; upstream's `eq`
  leaves it a string that reads as true and pruned 4 of 6 snapshots, and DEV-002 says such a value is refused. It
  is now refused. 114 new test cases (three tests); the 105 that check the refusal fail on `0.1.49`. Three partial
  fixes (`rstrip`, `lstrip`, a strip that adds only the no-break space) each fail them. 1,017 tests, `dev/run.py --fuzz`
  38/38 and e2e 77 under Python 3.14 and 3.11.
- **Confirmed, with no change:** `use_template = home<NBSP>` applies `[template_home]`, where upstream applies
  nothing and prunes by the defaults' retention: DEV-003 ignores all whitespace around a name, so the template the
  operator meant is applied. Prune rewrites the snapshot cache after each batch, as upstream does (upstream also
  re-reads it), so the cost grows with batches times cache lines: 113 s for 2,000 datasets with zfs patched out;
  the production config has 17 sections. A million snapshots are listed and read in 9.3 s and about 1.2 GB; a recursion over 50,000
  datasets loads in about a second. The prune decision kept its count, took only the oldest past their window and
  was monotonic in count and clock; the schedule gave a recent past time on the configured wall clock in every zone
  from 2000 to 2037; the readers raised nothing but `SanoidError`; the cache round trip kept every byte.
- **Pinned, after `0.1.50`:** the template reading above is now a decision on record: DEV-003 says it covers what
  Perl's `\s` doesn't match, and a test (21 cases) fails if a name is read byte for byte. `dev/diff_config_merge.py`
  now pads 4% of values and template names with those characters, with two known classes ("toggle pad", "template
  pad"); on `0.1.49` 27 of 400 fuzzed configs differ, none on `0.1.50`, and a byte-exact template reading fails its
  hand case. The committed tests' list of such characters held three of them raw; it now spells them as escapes. 1,038 tests,
  `dev/run.py --fuzz` 38/38 and e2e 77 under Python 3.14 and 3.11.
- **Closed with no change:** `get_zpool_capacity` checks for an answer holding more than a capacity with
  `str.strip()`, so `85% <NBSP>` or a U+001C after the line passes DEV-016's refusal; it reads as 85, the value
  upstream's first word gives, and zpool never writes either. A config or snapshot cache of gigabytes is read whole
  and ends the run in a `MemoryError` traceback; both files are root's, and the cache is pysanoid's own, replaced from
  zfs once its TTL passes. `_cfg_int` raises `OverflowError` for `float('inf')`, which only a caller's own dict can
  hold: a config gives the setting as text.
- **Kept, as tests:** the properties, as 15 tests in `test_pysanoid.py` (14 drawing 800 to 6,000 seeded cases each,
  1.5 s in all; one fixed case for a negative `frequent_period`): prune's count floor, oldest first, age window and
  monotonicity in count and clock; the numeric readers raising nothing but `SanoidError`, a count's digits read whole
  to 18, Perl's number and a hook's timeout; the cache line take writes read back, and removal keeping every other
  byte; the ini grammar never raising and reading a written config back under each line ending; the last preferred
  time in every zone from 2000 to 2037, and a snapshot just taken holding its type back; zpool's answer. Fifteen
  one-line breaks of `pysanoid.py` each fail one. The first draft of prune's draws scattered snapshots over 400 days
  and missed an age window an hour too wide: they now also come one a period, with some on the window's edge, and a
  window that includes its edge fails too. 1,053 tests under Python 3.14 and 3.11.
- **Kept, in the gate:** three kinds of check the earlier audits ran once and never kept. `dev/diff_whole_run.py`
  (target `whole_run`) runs upstream's whole script and pysanoid as programs, one run or several, on copies of a pool
  that changes, both clocks frozen, and compares what each did: snapshots destroyed and made, each `zfs snapshot` and
  `zfs destroy`, every hook's arguments and `SANOID_*` variables. Its 38 hand cases are the fifth audit's (the production
  config among them) and seven registered classes, each checked by its rule; its fuzz pass draws configs both read
  alike. 38 of 38 and 40 of 40 pass. `dev/mutants.py` gains three suites, from every one-line break the audits wrote as
  changes landed (2026-10-05, then 0.1.41 to today), recovered from their records: 115 given to `test_pysanoid.py`,
  58 to the alert script's tests, 15 to the hand passes of `whole_run`, `zfs_output` and `hooks`. All are caught, as
  are the 24 the targets suite already held. Building them found holes in the new target, not in pysanoid: two of its
  breaks stood until a case gave a parent snapshots under a listing that holds it, and a zfs recursion a second run
  on its cache. `dev/run.py --fuzz` 40 of 40 and e2e 77 under Python 3.14 and 3.11.
- **Registered:** take and prune go through the datasets, and prune through each dataset's types, in Perl's
  per-process random hash order upstream; pysanoid's order is fixed. Harmless (each destroy and hook stands alone,
  and no order can be matched), and the harnesses already compared around it, but DEVIATIONS.md named hash order only
  for the `--debug` summary and the recursion trace. DEV-048 now covers both.
- **Pinned, after a second look at what wasn't kept:** DEV-004 and DEV-025, both registered as dangerous for the data,
  had no test and no harness. DEV-004 has three tests (21 cases) and the `config_merge` known class `membership`
  (four hand cases; the generator draws such a value now and then); DEV-025 has a test and an `e2e_subprocess.py`
  check. Each fails on a copy made to behave as upstream, and those four breaks are in `dev/mutants.py`. The 0.1.45
  probe of failing hooks against upstream's whole script, byte for byte, is now 21 `zfs_output` hand cases (all
  match; each side's own line saying why a hook couldn't start is the known class `P6`), and two breaks of the hook
  warning fail them. DEV-039e's reproduction is a `snapshot_cache_mutation` hand case: a child lookup cut inside its
  second line, where upstream must cache a snapshot of the phantom child `tan`. The register's Harness lines now name
  `whole_run` for the seven entries it checks. The first fuzz pass of `config_merge` with the new values read a
  `recursive` refused in a file holding a pad as the pad's: its rule now looks at the membership class first.
  1,076 tests, `dev/run.py --fuzz` 40 of 40 (`config_merge`'s pass rerun after that fix), e2e 82, under Python 3.14
  and 3.11; `dev/mutants.py` tests 119 of 119, harness 18 of 18.
- **Closed with no change:** DEV-009 (a NUL byte refuses the config), DEV-010 (an empty here-doc in the defaults
  file reads as blank) and P2 (`$TZ` read as glibc reads it) are pinned by tests, with no harness running upstream
  beside them. Their upstream side can't drift (it is the pinned copy in `dev/upstream/`) and the tests guard
  pysanoid's, so the tests are enough; P2's would also need the C library in a harness.
- **Built, two runs at once:** `dev/check_overlap.py` (target `overlap`) starts pysanoid twice, 0 to 1.2 s apart, on
  one pool that changes, sharing the cache and run directories, and judges by rules it states: no type below its
  count, nothing destroyed inside its window, nothing destroyed that the two runs one after the other wouldn't, the
  cache matching the pool's listed snapshots, no traceback. 11 hand cases (the production config as cron and as its two
  units among them) and a fuzz pass: all hold, under Python 3.14 and 3.11. Two runs can each take the same type, as
  upstream's can: counted, not failed. It caught nothing at first. The fake zfs read the pool after a listing's wait,
  so a listing was always current; a real slow `zfs get` reports the pool as it began, and with that, and offsets
  wide enough for one run's listing to span the other's destroys, it fails three breaks of the cache lock (the add
  unlocked, the closing flush dropped, a deferred removal never retried), which unit tests also catch with faked
  lock states. Dropping the pruning lock breaks no rule, as reasoned in the first report: two prunes of one view pick
  the same oldest snapshots, and the second's destroys fail. 1,076 tests, `dev/run.py --fuzz` 42 of 42, e2e 82 under
  Python 3.14 and 3.11; `dev/mutants.py harness` 21 of 21.

## The 2026-10-28 review

*Written 2026-10-28.*

**What was asked.** Every comment checked against the code it describes, then a full gate review at `0.1.51`.

**How.** `pysanoid.py` and `pysanoid_alert.py` read whole, each claim checked against the code, upstream's script
or a probe; every name a comment gives, and every test, check and path `DEVIATIONS.md` and the READMEs cite, looked
up mechanically (all exist, but one); the gate run under Python 3.14 and 3.11.

- **Corrected, with no code change (committed as `d3a8b27`):** eight comments that said less than or other than the
  code does: `note_dataset` records a recursion's children too, `_COUNT_RE` also checks DEV-050's version, a missing
  run directory is made rather than failing, under `--readonly` a hook's variables are only deleted (P9), and others
  of the same weight. `dev/lib/extract.py` cited a test that was never written.
- **Fixed, as `0.1.52`:** DEV-016's refusal of an answer holding more than a capacity compared the answer trimmed
  by `str.strip()`, which also removes a no-break space, NEL, U+2028, U+3000 and U+001C-001F, where Perl's `\s`
  does not: `85%` with a no-break space on the next line read as 85. Upstream reads 85 there too, and a whole run
  pruned 3 of 5 dailies that `prune_defer = 50` was holding back, the same harm DEV-016 exists to prevent. Only
  Perl's `\s` is now nothing more, as `0.1.50` made it for the toggles. 13 new test cases (two tests); the 8 that
  check the refusal fail on `0.1.51`, the 5 guards pass on both. `zpool_capacity`'s known class states the rule with
  Perl's `\s` and gains five hand cases (all five diff on `0.1.51`), and `e2e_subprocess.py` two whole runs (both fail
  on `0.1.51`, which destroys 3). Two breaks (`str.strip()` again, and a strip that adds only the no-break space) are
  in `dev/mutants.py`; both are caught.
- **Removed, as `0.1.52`:** `plan_snapshots` caught `ZeroDivisionError`, added in `0.1.11` for a division by
  `frequent_period`. That division is gone (0 is refused by name, DEV-032), and nothing else in the block divides by
  a setting, so the catch could hide only an error nobody expects. No behavior changes.
- **Noted, no change:** with `PATH` unset, upstream and pysanoid both make it `:/bin:/sbin`, whose empty first entry
  is the working directory; systemd and cron always set `PATH`. The timeout kills only the hook's own process
  (DEV-042 says so). Rewriting the cache after every prune batch costs batches times the cache's size, as upstream's.
- **Built, a standing check of the documentation's references:** the review's one-off lookup is now two tests.
  `test_everything_the_documentation_cites_exists`: every DEV/P ID `pysanoid.py` names has its entry heading in
  `DEVIATIONS.md`; every test or `verify_` check cited there, in the READMEs, or in any source's comments and
  docstrings is a function some source defines; every name in the docs' backticks is in a source (upstream's
  script, the settings and file names count); every `dev/` path and relative link exists (`dev/.cpan_cache` and
  `dev/work` aside, which a fresh checkout doesn't have). It passes on a clean checkout, and on the tree before the
  comment fixes it reports exactly `dev/lib/extract.py`'s citation of a test never written.
  `test_the_reference_check_reports_each_kind_of_stale_citation` plants one of each kind and requires each
  reported, so the check can't pass by checking nothing.
- **Upstream since the history pass:** 9 issues and pull requests were opened or changed between 2026-09-26 and
  2026-10-28. Seven are syncoid's or the docs' (#554, #1092, #1113, #1114, #1115, #1117, #1118). Upstream's master is
  still the pin, `d39b51a`, and `sanoid` last changed on 2026-02-18, before it. #1116 (a `pre_snapshot_script` that
  outlives `script_timeout` kills sanoid, so nothing is taken whatever `no_inconsistent_snapshot` says) is DEV-042,
  which already does what it asks; `test_a_pre_snapshot_script_that_times_out_is_a_failed_one_and_the_run_goes_on`
  now pins its exact case, both ways. #1119 is #651 met in the field: a host booted with its clock in 2153, ntpd set
  it right four hours later, and sanoid took nothing on 18 datasets for four days while it went on pruning.
- **Reversed, as `0.1.53` (DEV-051):** the decision of 2026-09-27 and 2026-10-10 that a snapshot dated ahead holds
  its type back as upstream's does rested on "holding a type back destroys nothing". #1119's account of pruning is
  right: prune keeps the configured count of each type counting every snapshot, so once the clock is set right one
  dated ahead fills a place the count keeps for real snapshots, and prune destroys a real one. Measured on
  pysanoid's prune, a year after: `daily = 7` keeps 6 real dailies, `monthly = 12` 11, and `frequently = 4` after
  four hours ahead none. A clock that runs behind, the more common fault, dates every real snapshot ahead of it,
  and upstream takes nothing while it stays behind. pysanoid now sets aside every snapshot dated more than five
  minutes ahead before take and prune build their tables: take finds its types due by the rest, and prune neither
  counts nor destroys one set aside. Within five minutes, as after a small step back, parity stands; `--debug`
  still prints everything; no new output. P16 is retired. Nothing can help while the clock is wrong: a run then
  sees every real snapshot as old. The scripted pass of `trouble`, judged by the rule (now in `dev/lib/model.py`),
  finds 11 destroys below the count and 110 missed snapshots on `0.1.52`, and none on `0.1.53`. 10 new test cases
  (four tests, and #1116's), of which the 5 that check the change fail on `0.1.52`; the test that pinned a damaged
  cache line holding its dailies back now pins them taken. `e2e_subprocess.py`'s check flips to DEV-051 (both
  halves fail on `0.1.52`). `dev/mutants.py`'s targets break for #651 becomes one for DEV-051, and its tests suite
  gains five (the rule dropped, the allowance at 0 and at a day, its boundary, a type due within it).
  `pysanoid_alert.py` still mails a snapshot dated ahead, now saying pysanoid sets it aside until the clock passes it.
- **Gate at `0.1.52`:** ruff clean; basedpyright 0 errors (144 warnings for `pysanoid.py`), also as 3.11; 1,092 tests
  (984 + the alert script's 108; 1,090 before the reference check), `dev/run.py --fuzz` 42 of 42 and e2e 84 under Python 3.14 and 3.11;
  `dev/mutants.py tests` 121 of 121.
- **Gate at `0.1.53`:** ruff clean; basedpyright 0 errors (144 warnings for `pysanoid.py`), also as 3.11; 1,101 tests
  (993 + the alert script's 108), `dev/run.py --fuzz` 42 of 42 and e2e 85 under Python 3.14 and 3.11;
  `dev/mutants.py` targets 24 of 24, tests 126 of 126, alert 58 of 58.
