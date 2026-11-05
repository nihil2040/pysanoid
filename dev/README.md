# pysanoid differential testing

Differential tests against upstream Perl sanoid, kept in the repo as a dev tool rather than a
scratchpad, so the same rigor that found and fixed every real bug in the 2026-09 audit (see
`../AUDIT.md`) can be re-run after any future change instead of trusting `test_pysanoid.py` alone.

Three targets don't compare with upstream at all. `fault_sweep`, `config_fuzz` and `trouble` run pysanoid as a
whole program on a pool held in memory and judge each run by what its config promises: see "Judged by what the
config promises" below.

## What this is, and why it exists alongside `test_pysanoid.py`

`test_pysanoid.py` is the regression suite: every fix from the audit has a test there that fails
before the fix and passes after, and it's fast enough to run on every change. It's necessary, but
it only checks the specific scenarios someone already thought to write down.

This directory is different: each `diff_*.py` extracts the **real upstream Perl subroutine**,
verbatim, runs it under a real Perl interpreter, and compares its output against pysanoid's
equivalent on the same input -- by hand-picked cases and by randomized fuzzing. It's what actually
found every bug described in `../AUDIT.md`, including the one that mattered most: a 65% divergence
rate in the dataset-cache load (`diff_dataset_cache.py`) that turned out to be live on every one of
the production runs. Reading the code carefully is not a substitute for this -- several of
these bugs were only visible once thousands of concrete scenarios were actually run through both
implementations side by side.

Use `test_pysanoid.py` for "does this still work." Use this directory for "does this still match
upstream" -- especially after touching the ini grammar, `load_config`, `get_snapshots`, `ZfsClient`,
`parse_args`, `main`, or `print_debug_dump`, which is what each target below covers.

## Quick start

```bash
cd pysanoid
python3 dev/run.py                  # hand-picked cases for everything (about 2 min, no setup needed)
python3 dev/run.py --fuzz           # + a moderate randomized pass for everything (15 to 20 min)
python3 dev/run.py dataset_cache    # just one target -- see `python3 dev/run.py --list`
python3 dev/diff_dataset_cache.py fuzz 2000 7   # a target's own script directly: n, seed
```

Every target runs pysanoid under the interpreter that runs it. The gate runs under Python 3.11 only,
production's: `python3.11 dev/run.py --fuzz`, `python3.11 dev/e2e_subprocess.py` and, with pytest
importable there, `python3.11 -m pytest test_pysanoid.py`.

Requires only system Perl (`Getopt::Long`, `Pod::Usage`, `JSON::PP`, `Data::Dumper`, `File::Copy`,
`File::Path`, `Time::Local` are core modules, already present on any normal Perl install) -- except
`diff_ini_grammar.py`, `diff_config_merge.py` and `diff_dataset_cache.py`, which need the **real**
`Config::IniFiles`:

```bash
python3 dev/fetch_cpan.py   # one-time; pulls Config::IniFiles + IO::Stringy into dev/.cpan_cache/
```

## What each target covers

| short name | script | upstream subs (verbatim) | what it caught in the original audit |
|---|---|---|---|
| `ini_grammar` | `diff_ini_grammar.py` | none: the real `Config::IniFiles`, read as sanoid ties it (the lines `ReadConfig` loops over, and what the `tie` hands out) | new in 0.1.29. The original audit fuzzed the grammar once (4,600 files) and kept nothing, and none of its files began with a byte-order mark encoded twice, which the module's own BOM pattern strips (it is UTF-8 text Perl reads as Latin-1): upstream loaded such a file and pysanoid refused it, so nothing was snapshotted or pruned (fixed in 0.1.29). The one known class is harmless: a file whose only line ending is a final lone CR makes upstream read that CR again as an empty line, with a warning; the parse is the same |
| `config_merge` | `diff_config_merge.py` | `init` (real `Config::IniFiles`) | an order-dependent recursion/template merge bug: 54 of 400 configs diverged. It used a mock of `Config::IniFiles` that trimmed every value, which hid what the 2026-09-25 re-audit found: upstream reads a template below its sections untrimmed (so `autoprune = no ` prunes there) and a repeated key of a `[template_default]` below them as an ARRAY ref. On the real module these two documented classes are counted as known, not failures; an empty here-doc `path` also differed (fixed in 0.1.27). Unit 8 turned `--debug` on: it now compares the merge trace and the warnings in order (one child's `recursively setting` lines sorted, since they come in Perl's hash order) and, when the configs match, `Data::Dumper`'s text of the real `%config` against `_perl_dumper`; pysanoid printed no trace and a `pprint` dump (fixed in 0.1.36). The upstream history pass found what its matching hid: a section placed above the recursive section it belongs under loses its settings to that section's recursion (skip_children deletes it), in upstream and so in pysanoid (upstream #1021). Since 0.1.37 pysanoid refuses such a file, a third known class ("order"), checked against the rule as `lib/configorder.py` states it apart from `load_config`, word for word, with pysanoid's trace up to the refusal equal to upstream's; the generator puts parents first 85% of the time so recursion's merge is still compared (shuffled freely, 23% of its configs broke the rule). Since 0.1.38 two entries of the merged config that take or prune one dataset are a fourth known class ("shared"): pysanoid is loaded with that check and without it, so a refused file's merge is still compared with upstream's, and the refusal is checked against the rule as `lib/configorder.py` states it. Since 0.1.50, 4% of values and template names carry a pad: a character Python's `str.strip()` removes and Perl's `\s` doesn't match (a no-break space, NEL, U+2028, U+3000, U+001C, U+001F), which upstream keeps. Two more known classes: "toggle pad" (DEV-002), a toggle holding one, which pysanoid refuses and upstream reads as true, the key refused being one the file pads and the file without its pads giving what it gives without; and "template pad" (DEV-003), a name holding one, pysanoid's merge checked against upstream's of the file with those names unpadded. On 0.1.49, which read a padded toggle as its word, 27 of 400 fuzzed configs differ Since 2026-10-26 a `recursive` or `skip_children` that is no true or false word is the known class `membership` (DEV-004): pysanoid refuses the file, where upstream reads `yess` as not recursive and compiles the value as a regex (`.` recurses, `(` ends its run); the value refused must be the section's own and no such word. Four hand cases, and the generator draws such a value now and then. |
| `snapshot_enumeration` | `diff_snapshot_enumeration.py` | `get_active_datasets`, `getsnaps`, `getsnapsbytype`, `getsnapsbypath` | unanchored-regex and byte-exactness bugs in the snapshot cache. The 2026-09-25 re-audit found what it couldn't reach, since it never compared stdout or made anything fail: a cache that couldn't be written or read ended the run (upstream carries on after a failed write, reading an unreadable cache as no snapshots), the TTL was compared in fractional seconds, `\d` and `\w` matched non-ASCII characters, and `--verbose --debug` missed upstream's `INFO: running:` line (fixed in 0.1.30). It now compares stdout, can kill `zfs get` with a signal (the fake zfs's `get_killed`) and break the cache, and counts the registered deviations as known classes. Unit 3b found it built the tables on a whole-second clock (`main()` reads a fractional one), left out the sorted list prune reads, and generated one type with no same-second snapshots, so it missed fractional ages, a damaged creation time that crashed the run, and a snapshot name upstream reads as a snapshot of another type (fixed in 0.1.31). It now builds the tables as `main()` does, compares every creation time and the sorted list (same-second snapshots as a set: upstream's order among them is per-process random), and generates every type, damaged times and crafted names. Unit 7 added a cache holding a NUL byte and a run directory no lock file can be made in (the Perl side's stub `removelock` then dies, as upstream's real one did end to end), as known classes that also check pysanoid against itself: with a NUL cache it must do what it does with no cache, and with no lock file its listing must be a normal run's, the cache untouched. The 2026-10-10 audit added a cache dated in the future and one cut short inside its last line, which upstream reads and 0.1.39 read too (fixed in 0.1.40), as known classes checked the same way: with the first pysanoid must do what it does with the same cache expired, with the second what it does with no cache. The generator dates a cache ahead, or cuts its end off, from a random stream of its own, so a seed's other scenarios are the ones it gave before. It then found the same cut one step earlier, where no cache check can see it: the fake zfs's `retry_cut` kills a `zfs get` naming no dataset that many bytes short, and both programs read the cut line (a creation time that lost digits reads as decades old). Since 0.1.41 pysanoid leaves that line out: the known class `listing_cut`, in which it must do what it does when the listing ends at the line before |
| `snapshot_cache_mutation` | `diff_snapshot_cache_mutation.py` | `removecachedsnapshots`, `addcachedsnapshots`, `getchilddatasets` | `add_cached_snapshots` silently building a wrong-mtime cache when the real one was missing. Since 0.1.30 a missing cache is left missing, a known class: upstream's remove writes an empty, fresh one and its add dies. Unit 3c found it stubbed the child lookup, never compared stdout, never wrote a line without a tab that spells a pruned name, and counted the remove's newline repair as a documented class the register didn't have. So it missed that the remove rewrote such lines, that a child lookup that couldn't start ended the run, and the lock-wait line's wording (fixed in 0.1.32). It now runs the real `getchilddatasets` (the dataset cache, `dev/fake_zfs`, or a lookup that can't start) and compares stdout and the lookups. Its hand mode also checks that upstream's re-read after a prune flush still destroys a boundary snapshot a run sooner, as DEV-013 says. Unit 7 added the lock mode `unusable` (no lock file can be made), a known class in which pysanoid must remove the cache and report it without dying. Since 0.1.40 an add onto a cache missing its final newline must leave the file exactly as it is: such a cache was cut short and is no cache, and the repair the class used to allow would have passed its last line off as whole Since 2026-10-26 a hand case keeps DEV-039e's reproduction: a child lookup killed inside its second line (`tank\ntan`, the fake zfs's `list_cut`), where upstream must cache a snapshot of the phantom child `tan`, and pysanoid keeps none of the lookup. |
| `dataset_cache` | `diff_dataset_cache.py` | `init` (dataset-cache half), `getchilddatasets` (real `Config::IniFiles`) | the dataset cache being loaded lazily instead of eagerly -- 969 of 1,500 scenarios diverged, live on every real run against the production config of the time, which had no recursive section. Unit 4 of the re-audit found it never compared stdout, never ran `--debug`, and never broke a child lookup or damaged datasets.txt. So it missed that a lookup killed partway let both programs set a child with its own section up from the defaults and prune it by the default retention, that a NUL byte in datasets.txt stopped pysanoid, and the dataset cache's `--debug` lines and upstream's warning wording (fixed in 0.1.33). It now compares stdout and loading's warnings, and makes the fake zfs's `list` fail to start, get killed or fail partway (`list_killed`, `list_fails_partway`, `list_prints`), with the two deviations as known classes. Unit 7 added a run directory no lock file can be made in, a known class in which pysanoid must report it once and leave datasets.txt as it was. Unit 8 found it gave upstream the full path of the fake zfs, as pysanoid runs it, so both printed that path in the child lookup's `DEBUG:` line where upstream prints the bare `zfs` (fixed in 0.1.36); upstream now runs `zfs` from PATH, and the warnings are compared in order. Since 0.1.37, a section above the recursive section it belongs under is the known class `ORDER` (checked as in `config_merge`, and datasets.txt must be left as it was), and the generator puts parents first 85% of the time. The 2026-10-10 audit found a case its fuzz had been generating all along with no difference to show: a section under a recursive section whose lookup didn't list its dataset, which both programs built from the defaults and pruned by them. Since 0.1.40 pysanoid starts it from the recursive section's settings: the known class `UNLISTED`, checked against the inheritance rule as `lib/configorder.py` states it (each key the section doesn't set itself is its parent's, what lies under it differs only where it does, every other section is upstream's, and nothing else differs but a warning that a section inside a zfs recursion asked for autosnap). A datasets.txt dated in the future is the class `FUTURE_CACHE`, checked like the NUL one, whose self-check now also covers a run directory no lock file can be made in; and the fake zfs's `list_prints: 0` makes a lookup fail having listed nothing |
| `options` | `diff_options.py` | the `GetOptions` block + `$cacheTTL` logic | `--cache-ttl` using Python's `int()` grammar instead of Getopt::Long's |
| `flow` | `diff_flow.py` | the whole top-level script body (lines 1-144) | `--debug` not implying `--verbose`. Unit 5 of the re-audit found it checked only that the `--force-prune` warning appeared, not its words, and never the negative `--cache-ttl` error, so it missed both texts (fixed in 0.1.35); it now compares both, word for word, without the ` at <script> line N.` Perl adds. What it can't reach in-process (output that can't be written, the locale, the Python version) is in `e2e_subprocess.py` |
| `debug_dump` | `diff_debug_dump.py` | `blabber`, `getsnapsbytype`, `getsnapsbypath` | a literal `"None"` printed instead of blank text for an unconfigured snapshot type. Unit 8 of the re-audit found it cut the config dump off and fed hand-built tables, so it missed that the dump was Python's `pprint` rather than `Data::Dumper`'s text and that a damaged cache's ages print differently (fixed in 0.1.36). It now runs upstream's own table subs on a pinned clock, sends every string as hex (any bytes) and every config value as the Perl integer or byte string upstream's init leaves, and compares the whole output as bytes, sorting only the summary's order |
| `hooks` | `diff_hooks.py` | `runscript` | `script_timeout` values like `" 5"`/`"+5"`/blank silently enforced a timeout upstream never would have -- the opposite-direction sibling of Module 7's original fix; also found upstream's alarm-based timeout kills the WHOLE process when it actually fires (`local $SIG{ALRM}`'s scope ends before `system()` runs), far worse than the already-known orphan-child risk. Unit 7 of the re-audit found it compared a timeout only as on or off, and every hook command it ran had no shell metacharacter, so Perl's `system()` and the shell split it alike; it missed that 24 of 45 `script_timeout` values set a different timeout (`5s`, `1_0`, `1.5`, `0.5`) and that a trailing `# comment` reaches the hook as arguments upstream (fixed in 0.1.34). It now reads back the timeout Perl's `alarm()` sets (a `CORE::GLOBAL::alarm` override), runs command lines with comments, quotes, builtins and odd whitespace, and has `hook_probe` log its arguments; a command Perl can't exec (-1) against `/bin/sh`'s `exec` (127) was a known class. It compared a failed hook's warning only as there or not: since 0.1.45 it compares the line, `WARN: <key> failed, <$?>`, word for word, which 0.1.44 ended in the exit code. And with the number in the line, the known class showed: upstream says `failed, -1` for words it can't exec, where 0.1.44, running them through `/bin/sh`'s `exec`, said 127. pysanoid now execs the words itself, as Perl does, so both return -1 and the class is gone. The generator also draws a first word starting with `-`, a file that isn't there, a directory, a shell that kills itself and a command of blanks alone |
| `prune` | `diff_prune.py` | `prune_snapshots`, `check_prune_defer`, `check_capacity_limit`, `runscript` | clean when built. The 2026-09-24 re-audit found four divergences it couldn't reach -- the hook lookup when two sections share a dataset (a veto upstream runs was skipped), hooks set to `0`, the whole-second clock, and the closing cache flush -- all fixed in 0.1.23 -- then two in `prune_defer`: `00` (upstream's `$limit eq 0` is a string comparison, so the pool is still asked) and a missing key (fatal upstream), fixed in 0.1.24. All now covered by hand cases and the fuzz. Unit 7 found it never made a destroy fail to start, which ended pysanoid's prune run (fixed in 0.1.34); `fail_start` now does, on both sides. Since 0.1.38 load_config refuses two sections that both prune a dataset, so its two-section cases give the section named after the dataset `process_children_only`, the layout that still loads. Since 0.1.39 `run_unusable` makes the run directory one no lock file can be made in, the class unit 7 gave the cache harnesses: upstream prunes its first batch unlocked and its `removelock` dies before the flush, and pysanoid is checked against itself (every batch that needs the lock skipped and reported, nothing destroyed, no hook run). No count in it was ever longer than a digit: since 0.1.41 hand cases and the generator use counts of 19, 400 and 5,000 digits, which upstream reads as a number no dataset reaches (none of that type pruned, the run goes on) and which ended 0.1.40's prune with a traceback. Its fake zfs never printed a byte that isn't UTF-8: since 0.1.42 a scenario's `noise` is a warning zfs writes on every destroy, and a destroy can fail with a complaint in such bytes (`stderr_hex`). Upstream counts a destroy that worked whatever was printed beside it; 0.1.41 took it for one that could not run. It compared what was destroyed, never what reached stderr: since 0.1.43 it compares zfs's own words and the `could not remove` lines there, as a set (Perl visits the types in its hash order), and checks on each side that a failed destroy's complaint comes just before its line. 0.1.42 showed nothing of a warning from a destroy that worked, and gave a failure in words of its own. Since 0.1.45 a failed hook's warning is compared there too |
| `zpool_capacity` | `diff_zpool_capacity.py` | `get_zpool_capacity` | new in 0.1.43. `prune` stubs the capacity query on both sides and compares what is done with the number; this compares the reading itself, on the same answer from a fake `zpool` (`fake_zfs/zpool`: stdout, stderr, exit status or signal, each as bytes): the number read, the words of a refusal, and what of zpool's stderr reaches the terminal. 0.1.42 trimmed the whole answer and so pruned by seven of its hand answers that upstream refuses (a capacity after a space, a tab or a blank line, or before a tab, a carriage return, a form feed or a no-break space), refused in other words, and showed zpool's stderr only inside a refusal. One known class: an answer holding more than a capacity, a second word or a second line, which upstream reads by its first word and pysanoid refuses (DEV-016). It is checked against the rule as the harness states it (`_more`), both ways |
| `zfs_output` | `diff_zfs_output.py` | none: upstream's whole script as it ships, run as a program (real `Config::IniFiles`, a stand-in `ps`) | new on 2026-10-25, rebuilding the one-off comparison of 0.1.43 (29 cases, byte for byte) that DEV-047's evidence rested on. Both programs run on the same config, cache directory and fake zfs, stdout and stderr on one pipe as the journal gets them, and the whole output is compared byte for byte: zfs's note on stdout and warning on stderr for every snapshot and destroy (ASCII, or bytes that aren't UTF-8), a snapshot or destroy that fails with zfs's complaint, a listing's complaint and its retry, and zpool's own warning when `prune_defer` asks for a capacity. Made the same first: the time in new names, Perl's ` at <script> line N.`, and the datasets' order, since upstream takes and prunes in hash order. Two known classes, each checked by its rule: a destroy's stderr before its stdout (DEV-047: pysanoid shows them the other way round) and a snapshot that fails (DEV-028: pysanoid's own ERROR lines and exit 1). On 0.1.46 nothing is unexplained in 17 hand cases and 180 fuzzed scenarios across three seeds under Python 3.14 (the hand cases and one seed's 60 under 3.11); five broken versions of pysanoid's output (zfs's stderr not shown, its stdout not shown, stderr shown first, bytes that aren't UTF-8 replaced, zfs's words held back to the end of the run) each fail 1 to 15 of the hand cases Since 2026-10-26 it also runs 0.1.45's one-off probe as 21 hand cases: hooks that exit non-zero, are killed by a signal, aren't there, are words Perl execs itself (`exit 0`), a script with no `#!`, a final `2>&1`, each kind of hook, on one dataset; each side's own line saying why a hook couldn't start is the known class `P6`. |
| `whole_run` | `diff_whole_run.py` | none: upstream's whole script as it ships, run as a program, its clock frozen (`lib/FakeTime.pm`) | new on 2026-10-25, from the fifth audit's one-off probe. Both programs run one to many times on copies of a pool that changes (`fake_pool/`: what is made is listed from then on, what is destroyed is gone), pysanoid under `lib/run_frozen.py`, each run at the scenario's time, the caches dated between runs so a cache's age is the scenario's. What each did is compared: the snapshots destroyed and made, each `zfs snapshot` and `zfs destroy`, every hook's arguments and `SANOID_*` variables in order for each dataset (prune's types come in Perl's hash order upstream, so its hooks are compared type by type), and whether each run succeeded. 38 hand cases: the production-shaped config (`fixtures/sanoid.conf`) in Europe/Dublin over three runs, a day of hourly runs and its two units, hooks that fail, veto or are held back, inherited `SANOID_*` variables, both kinds of recursion, `process_children_only`, `skip_children`, a child section overriding its parent, `prune_defer`, an empty pool, names a type can't be read from; and seven registered classes, each checked by its rule (DEV-012, DEV-017, DEV-028 twice, DEV-031, DEV-032, DEV-050). The fuzz pass draws configs both read alike, in UTC: up to five sections with templates, recursion, hooks, `prune_defer` and schedule settings, over a pool of every type, run one to four times hours to weeks apart. Of eight breaks of pysanoid it now fails all eight (`dev/mutants.py harness`); two stood at first, for cases that gave a parent no snapshots and a zfs recursion no second run |
| `overlap` | `check_overlap.py` | none: judged by rules it states, and against the same two runs one after the other | new on 2026-10-26: two runs at once, the one thing no other target did. pysanoid twice as programs, started 0 to 1.2 s apart on one pool that changes (`fake_pool/`, slowed; a listing reports the pool as it began, as a slow `zfs get` does), sharing one cache and one run directory, clocks frozen a few seconds apart. Rules: no type ends below its count (or what it had), nothing destroyed inside its window, nothing destroyed that the two runs one after the other wouldn't, the cache lists exactly the pool's snapshots of the datasets listed, and no run ends in a traceback. 11 hand cases (two prunes, two cron runs cold and warm, take and prune both ways round, clocks an hour apart, both kinds of recursion, a one-batch prune whose only flush meets the other's listing, `prune_defer`, the production-shaped config as cron and as its two units), each at four or more offsets; the fuzz pass pairs `whole_run`'s configs. A type taken by both runs is counted, not failed: neither program's take holds a lock. Of three breaks of the cache lock it fails all three (`dev/mutants.py harness`), which unit tests also catch with faked lock states. Dropping the pruning lock fails no rule: two prunes of one view pick the same oldest snapshots, and the second's destroys fail. Its first version caught none: it had to slow the listing, and the fake zfs had read the pool after its wait, so a listing was always current |
| `dst_schedule` | `sweep_dst.py` | `take_snapshots` (one instrumentation line records `$lastpreferred`) | not a parity diff: pysanoid deliberately computes preferred times with zoneinfo, so this checks them against a brute-force answer at every instant around each DST change, and counts upstream's differences for information. On 0.1.24 it failed at 33,856 of 713,664 points (hourly across a change, a refused negative `frequent_period`, `weekly_wday` outside 0-6); its wide mode then showed that only a real-minute scan is exact in Australia/Lord_Howe (30-minute changes). The take fuzz avoids DST changes on purpose, which is why this exists |
| `take_snapshots` | `diff_take_snapshots.py` | `take_snapshots`, `get_date`, `runscript` | `post_snapshot_script`'s `SANOID_SNAPNAME` silently missing its `dataset@` prefix -- upstream's own `foreach my $snap (@snapshots) { $snap = "$dataset\@$snap"; ...}` loop aliases and mutates the very array `pre_snapshot_script` already read the bare name from, so the two hooks upstream actually get different formats and pysanoid didn't replicate that. Later (2026-09-24), three hand cases on a real fall-back day showed it also missed upstream's `dst_` rename of that same element (fixed in 0.1.22). The re-audit then found three gaps it couldn't reach, since it never made a call fail to start or compared stdout: a process that can't start ended the whole take run, a snapshot held back by a failed pre hook was silent, and `--readonly` hid the hook lines (fixed in 0.1.26; it now does both). Since 0.1.27 take reads a malformed count or preferred time as upstream does (0.1.28: a blank preferred time too), and the generator feeds it some. Since 0.1.41 it also gives a type a newest snapshot dated ahead of the clock, which neither program takes (parity, by decision: upstream #651), and counts too long for a number, which both take and which 0.1.40, past 4,300 digits, did not. Its clock stood still and it handed pysanoid that clock, so the time in a name could not differ: since 0.1.42 both sides run on a clock that moves a scenario's `tick` seconds for every zfs command and hook that has run, pysanoid through the path a real run takes (no `now=`), with up to three datasets. Upstream reads the clock for the names as each dataset's turn comes; 0.1.41 read it once. Each side's names are checked against that rule and then rewritten to the scenario's own time, since the two take the datasets in different orders (`_name_times`). zfs can also print bytes that aren't UTF-8 on a snapshot (`noise`, `stderr_hex`), which made 0.1.41 take a snapshot that worked for one that could not run. It compared stdout, never stderr, and its stand-in for upstream's `tee_stderr` kept what it captured to itself, where the real one also passes it on. Since 0.1.43 the stand-in passes it on, and zfs's own words and the CRITICAL ERROR lines are compared, grouped by dataset as stdout is. 0.1.42 showed nothing of a warning from a snapshot that worked, and gave a failure in words of its own, zfs's complaint inside the line. Since 0.1.45 a failed hook's warning is compared there too |
| `perl_number` | `diff_perl_number.py` | none: Perl's own `0 + $string` | new in 0.1.27, with `_perl_number` (take's reading of a malformed value). Built against the source of perl 5.34's `grok_number`/`my_atof3`/`grok_infnan`, it found two things reading didn't: a NaN spelling at the very end of the string doesn't count after a second sign (`++snan` is 0, `++1.#QNAN` is 1), and under a UTF-8 locale macOS's `strtod` skips a 0xA0 byte as whitespace (Perl runs under `LC_ALL=C` here, as from cron or systemd) |
| `long_horizon` | `simulate_long_horizon.py` | `take_snapshots` and `prune_snapshots` on sampled runs (through `diff_take_snapshots.py` and `diff_prune.py`) | not a parity diff: pysanoid's take and prune run after run, on the production-shaped config and a fixture, with invariants checked on every run. A 2-year run of it found an extra daily, weekly or monthly for every dataset in a fall-back's repeated hour (the `_after()` fix). It joined this table only on 2026-09-27: until then nothing ran it, and it had broken without failing. Every take comparison raised `KeyError` once the take harness gained `fail_start` (0.1.26). Recursion's child lookup reached the fake zfs on PATH, which crashed, so the recursive section ran without its child. The in-memory zfs accepted a name that already existed, so the `dst_` retry never ran. It checked for a pile-up but never for over-pruning, and even that check needed every survivor past the window. Now the fake zfs keeps the pool (a repeated name is refused, `-r` reaches every descendant, `zfs list` answers the child lookup), each run is checked for over-pruning, missed and extra slots, pile-ups, take's return value and unreported failures, and each check has been shown to catch a deliberately broken pysanoid |
| `fault_sweep` | `sweep_faults.py` | none: judged by what the config promises (`lib/model.py`, `lib/audit.py`) | new on 2026-10-11, from the third audit's one-off probe. Every file operation, lock and zfs call of a run fails in turn, each in every way it can, or the process ends there, and two clean rounds follow (see "Judged by what the config promises"). On 0.1.39 it finds a child's own section built from the defaults when its recursive parent's lookup lists nothing, the child's weeklies destroyed below the count it keeps; on 0.1.39 and 0.1.40, a snapshot listing cut inside its last line, a dataset's newest daily and hourly destroyed inside their window. The probe compared a faulted pair of rounds with a clean pair, both at the real time of day, so a run that crossed the top of an hour differed for no reason (it did once); its fixture kept no type the defaults don't, so the first of those findings could not show in it; and what it listed as categories for a person to read is here a rule that fails or a class that is checked both ways |
| `config_fuzz` | `fuzz_configs.py` | none: judged as above, the retention as pysanoid itself loads it | new on 2026-10-11, from the audit's other probe. Configs with odd values in every setting, six runs each. On 0.1.40 its hand cases find the count too long for a number: a traceback at 4,400 digits, and at 320 digits one in the middle of a prune. The probe's stand-in for zfs read a hook's command line as `/bin/sh -c <script>`; since 0.1.45 a hook's words come alone, and it raised on every config with a hook. Nothing ran it, so nothing said so: it was found on the day it was rebuilt |
| `trouble` | `simulate_trouble.py` | none: judged as above | new on 2026-10-11: the wider simulator the audit's plan asked for. pysanoid as a whole program, run after run, through any failure of anything, refused commands, runs killed partway, a clock set ahead or back, missed runs, runs by hand and a pool filling, on the production-shaped config, the long-horizon fixture and random configs. On 0.1.39 it finds a cache dated ahead being read: after one round with the clock set ahead and a failure in it, and the clock set back, the runs go by what that round left (the fixture's child of a recursive section goes without its hourlies for twenty rounds, and the production-shaped config's cache keeps a snapshot a killed prune had destroyed). On 0.1.41 it finds what 0.1.43 changed: the warning zfs gives when a name is taken, in a repeated hour, never reached stderr |

Every script's own module docstring has the specifics: which upstream sub, what's stubbed and why,
and what's deliberately excluded from fuzzing because it's a documented, decided divergence rather
than a bug (see `../DEVIATIONS.md`, which lists every deliberate deviation by its ID -- these
scripts and that file should stay consistent; if a script starts finding something DEVIATIONS.md
doesn't mention, that's either a new bug or DEVIATIONS.md is stale).

### Verification (2026-09-22, prune/take_snapshots/hooks)

All three were built incrementally (hand cases first, fixing harness bugs as they surfaced --
several did, see each script's own commentary) then run at fuzz scale before being trusted:
`hooks` 18/18 hand + 600/600 fuzz; `prune` 16/16 hand + 1,300/1,300 fuzz across two seeds;
`take_snapshots` 18/18 hand + 1,100/1,100 fuzz across two seeds (21/21 hand + 600/600 fuzz at 0.1.22,
after the fall-back-day cases were added). At 0.1.23, `prune` is 23/23 hand + 1,200/1,200 fuzz across
two seeds, with a wider generator (a second section on the same path, hooks set to `0`, two snapshot
types per run, a busy cache lock) that finds 97 of 600 on 0.1.22, and `take_snapshots` is 22/22 hand +
1,000/1,000 fuzz across two seeds, its generator now also setting hooks to `0` (3 of 600 differ on
0.1.22). At 0.1.24, `prune` is 28/28 hand + 1,200/1,200 fuzz across two seeds: its capacity stub
fails for an unknown pool and logs every query, and its generator also tries `prune_defer` values
`00`, `000`, `080` and a missing key (51 of 600 differ on 0.1.23). At 0.1.25, `dst_schedule` matches
the brute-force answer at all 713,664 points (`check`: four zones, 2026) and all 4,930,848 (`wide`: six
zones, 2026-2030), and `take_snapshots` is 28/28 hand + 1,000/1,000 fuzz across two seeds: its three
new DST and `frequent_period` hand cases all differ on 0.1.24, and a known case covers `weekly_wday = 7`.
At 0.1.26, `take_snapshots` also compares verbose stdout (grouped by dataset, since upstream's dataset
order is Perl's random hash order) and can make one `zfs snapshot` or hook fail to start, as a failed
fork does (a `system()` override in the Perl harness, a `subprocess.run` patch on the Python side). It
is 33/33 hand + 1,000/1,000 fuzz across two seeds; its five new hand cases all differ on 0.1.25, and
the widened fuzz finds 30 of 600 there.
At 0.1.27, `config_merge` runs on the real `Config::IniFiles` and also compares init's warnings: 15/15
hand cases as expected (11 match, 3 in the known classes, the tab-after-comma divergence) and 3,000
fuzzed configs across three seeds with nothing unexplained (807 in the known classes); its empty
here-doc `path` case differs on 0.1.26. `take_snapshots` is 40 hand (36 match, 4 known) + 1,000/1,000
fuzz across two seeds, with malformed counts and preferred times in 15% of scenarios each; its four new
hand cases and 74 of 500 fuzz scenarios differ on 0.1.26. `perl_number` is 159/159 hand + 120,000/120,000
fuzzed strings across six seeds. At 0.1.28, `take_snapshots` is 43 hand (38 match, 5 known) + 1,500/1,500
fuzz across three seeds; the generator adds blank preferred times and snapshot ages of 5 h, 20 h and 2 days,
and its two new timing hand cases differ on 0.1.27 (blanks rarely land where the time matters, so the fuzz
alone doesn't separate the versions). At 0.1.29, `ini_grammar` is 40 hand cases (38 match, 2 in the
known class) + 300,000 fuzzed files across three seeds on the pinned 3.003000, and 20,000 each on 2.94
and 3.000003 (`--ini-lib`), with nothing unexplained; on 0.1.28, 5 hand cases and 846 of 20,000 fuzzed
files differ. At 0.1.30, `snapshot_enumeration` is 17 hand cases (12 match, 5 in the known classes) +
2,000 fuzzed scenarios across four seeds, with nothing unexplained; on 0.1.29, 10 hand cases and about a
third of 1,000 fuzzed scenarios differ. `snapshot_cache_mutation` is 14/14 hand + 600/600 fuzz. At 0.1.31,
`snapshot_enumeration` is 24 hand cases (18 match, 6 in the known classes) + 2,000 fuzzed scenarios across
four seeds, with nothing unexplained; on 0.1.30, 5 hand cases and 142 of 500 fuzzed scenarios differ.
At 0.1.32, `snapshot_cache_mutation` is 25 hand cases (21 match, 4 in the known classes) + 2,000 fuzzed
scenarios across four seeds, with nothing unexplained, and its re-read check confirms; on 0.1.31, 5 hand
cases and 86 of 500 fuzzed scenarios differ. At 0.1.33, `dataset_cache` is 29 hand cases (22 match, 7 in
the known classes) + 2,000 fuzzed scenarios across four seeds, with nothing unexplained; on 0.1.32, 12 hand
cases and 244 of 500 fuzzed scenarios differ. At 0.1.34, `hooks` is 37 hand cases (3 in the known class) +
1,200 fuzzed scenarios across three seeds, `prune` 31 hand + 600 fuzzed, `snapshot_enumeration` 28 hand (9 in
the known classes) + 1,500 fuzzed across three seeds, `snapshot_cache_mutation` 30 hand + 1,000 fuzzed across
two seeds, and `dataset_cache` 32 hand + 1,500 fuzzed across three seeds, with nothing unexplained; on 0.1.33,
`hooks` differs in 21 hand cases and 393 of 500 fuzzed scenarios, `prune` in 3 and 12 of 600,
`snapshot_enumeration` in 3 and 22 of 500, `snapshot_cache_mutation` in 3 and 23 of 500, and `dataset_cache`
in 2 and 11 of 500. At 0.1.35, `flow` is 24 hand cases + 500 fuzzed scenarios under Python 3.14 and 3.11
(and 400 more on a second seed under 3.14), and every target passes under both; on 0.1.34, `flow` differs in
3 hand cases and 163 of 400 fuzzed scenarios. At 0.1.36, `debug_dump` is 12 hand cases + 1,200 fuzzed
scenarios across two seeds and `config_merge` 15 hand cases (11 match, 3 known, the tab-after-comma
divergence) + 1,200 fuzzed configs across three seeds, with nothing unexplained, and every target passes under
Python 3.14 and 3.11; on 0.1.35, `debug_dump` differs in all 12 hand cases and 300 of 300 fuzzed scenarios,
`config_merge` in 14 hand cases and 200 of 200 fuzzed configs, and `dataset_cache` in 3 hand cases and 70 of
300 fuzzed scenarios. At 0.1.37, `config_merge` is 21 hand cases (12 match, 8 known, the tab-after-comma
divergence) + 400 fuzzed configs (131 known, 24 of them the section order) and `dataset_cache` 34 hand cases (11
known) + 500 fuzzed scenarios (98 known, 13 the order), with nothing unexplained, and every target passes under
Python 3.14 and 3.11; on 0.1.36, `config_merge` differs in 5 hand cases and 24 of 400 fuzzed configs, and
`dataset_cache` in 2 hand cases and 8 of 400 fuzzed scenarios. At 0.1.38, `config_merge` is 26 hand cases (13
match, 12 known, 4 of them a shared dataset, the tab-after-comma divergence) + 400 fuzzed configs (164 known, 50 of
them shared, each with its merge equal to upstream's), with nothing unexplained, and every target passes under
Python 3.14 and 3.11; on 0.1.37, `config_merge` differs in the 4 shared hand cases and those 50 fuzzed configs.
At 0.1.39, `prune` is 37 hand cases (4 known, a run directory no lock file can be made in) + 400 fuzzed scenarios
(11 known), with nothing unexplained, and every target passes under Python 3.14 and 3.11; a pysanoid that prunes
unlocked, as upstream does, or skips a batch without reporting it fails all 4.
At 0.1.40, `dataset_cache` is 46 hand cases (20 known, 7 of them a section its lookup didn't list and 2 a
datasets.txt dated ahead) + 1,500 fuzzed scenarios across three seeds, `snapshot_enumeration` 36 hand (14 known) +
1,000 fuzzed across two seeds (39 a cache cut short, 30 one dated ahead), and `snapshot_cache_mutation` 30 hand +
1,000 fuzzed across two seeds (41 an add onto a cache cut short), with nothing unexplained, and all 28 runs pass
under Python 3.14 and 3.11; on 0.1.39 the new hand cases differ (9 in `dataset_cache`, 5 in
`snapshot_enumeration`, 1 in `snapshot_cache_mutation`), as do 16 of 300 fuzzed scenarios in `snapshot_enumeration`
and 12 of 300 in `snapshot_cache_mutation`.
At 0.1.41, `snapshot_enumeration` is 48 hand cases (23 known, 9 of them a retry killed inside a line) + 2,000
fuzzed scenarios across four seeds (148 with such a retry), `prune` 47 hand + 1,200 fuzzed across three seeds (74
with a count too long for a number), and `take_snapshots` 53 hand (5 documented divergences) + 900 fuzzed across
three seeds (201 with a newest snapshot dated ahead, 50 with such a count), with nothing
unexplained, and all 28 runs pass under Python 3.14 and 3.11; on 0.1.40 the new hand cases differ (9 in
`snapshot_enumeration`, 4 in `prune`, 1 in `take_snapshots`), as do 20 of 500, 9 of 400 and 4 of 300 fuzzed
scenarios.
At 0.1.42, `prune` is 51 hand cases + 1,200 fuzzed scenarios across three seeds (107 with zfs printing a byte that
isn't UTF-8) and `take_snapshots` 65 hand (5 documented divergences) + 900 fuzzed across three seeds (278 with two
or three datasets on a moving clock, 63 with such a byte), with nothing unexplained, and all 28 runs pass under
Python 3.14 and 3.11; on 0.1.41 the new hand cases differ (3 of 4 in `prune`, 8 of 12 in `take_snapshots`), as do
10 of 400 and 101 of 300 fuzzed scenarios.
At 0.1.43, `prune` and `take_snapshots` run the same cases with stderr compared as well (51 hand + 1,200 fuzzed
across three seeds, and 65 hand + 900 fuzzed across three seeds; in 270 and 212 of the fuzzed scenarios zfs warns on
every command, a command fails with a complaint, or one can't start), and `zpool_capacity` is 37 hand cases (31
match, 6 known) + 1,800 fuzzed answers across three seeds (151 a capacity alone, 1,583 refused by both in the same
words, 66 known), with nothing unexplained, and all 30 runs pass under Python 3.14 and 3.11; on 0.1.42, `prune`
differs in 8 hand cases and 20 of 400 fuzzed scenarios, `take_snapshots` in 12 and 28 of 300, and `zpool_capacity`
in 26 and 582 of 600.
At 0.1.45, `hooks` is 45 hand cases + 1,200 fuzzed scenarios across three seeds with no known class but macOS's alarm
limit, and `prune` and `take_snapshots` run their cases with a failed hook's warning compared as well (51 hand + 1,200
fuzzed, and 65 hand + 900 fuzzed, across three seeds each), with nothing unexplained, and all 30 runs pass under
Python 3.14 and 3.11; on 0.1.44, `hooks` differs in 13 hand cases and 307 of 400 fuzzed scenarios, `take_snapshots`
in 6 and 35 of 300, and `prune` in 2 and 10 of 400.
Every known divergence surfaced is canonicalized against
DEVIATIONS.md (see above), not silently absorbed -- `dev/run.py --fuzz` exits 0. A HAND case can also list
`known_diffs`: result keys expected to differ for a documented reason, while every other key must
still match.
Building `take_snapshots` also surfaced a nondeterminism in this harness's OWN comparison (a
section with two due types calls the stubbed cache-flush once per type, and which type Perl visits
first is hash-random per process -- comparing the flush calls' raw cumulative counts made an
already-correct run look like a diff about 50% of the time); fixed by diffing each batch's
incremental size instead, and re-run several times afterward to confirm the fix actually holds
against both orderings, not just the one that happened to come up first.

## Beyond the diff_*.py targets

Two more tools live here, neither shaped like a diff_*.py -- one runs a continuous trajectory
instead of independent scenarios, the other has no upstream Perl side at all -- but both reuse this
directory's machinery rather than duplicating it.

**`dev/simulate_long_horizon.py`** (`long_horizon` in `run.py`) -- pysanoid's real `take_snapshots`
and `prune_snapshots`, run after run over weeks to years of simulated time on a real sanoid.conf, in
process against an in-memory zfs: the state each run leaves for the next, which no single run shows.
Every run is checked, and any failure fails the target:
- prune destroyed nothing younger than count x period, never left fewer than count, and touched
  nothing a section doesn't prune, or prunes only while its pool defers pruning;
- every type a section takes stays on its slots: none missed, none taken again early (daily and
  longer), no pile-up past count once the type has had count x period to fill;
- take returns exactly what zfs created, and only types a section takes;
- nothing is reported but the zfs failures `--fail-rate` injects, each exactly once (a failed destroy is found by
  the words of its line, `could not remove <name> : `).

Sampled runs also go through real upstream Perl on the same inventory, via `diff_take_snapshots.py`
and `diff_prune.py`: every run within 3 hours of a clock change, and every run that takes or prunes a
daily or longer type, with the run before it, within 3 days of a change (elsewhere the month's first).
A difference DEVIATIONS.md documents -- upstream's DST arithmetic (DEV-030), and the `dst_` retry upstream leaves
out of `%taken` (DEV-029) -- is counted; any other fails.

The fake zfs keeps the pool. `zfs snapshot` refuses a name the dataset already has, as the second
pass through a fall-back's repeated hour does, so the `dst_` retry runs as on a real pool; `-r`
reaches every descendant; destroying a missing snapshot fails; `zfs list` answers recursion's child
lookup at load from the config's datasets plus `--dataset`/`--datasets` (`zfs list -H -o name`
output). `--split-take-prune` runs take and prune as the two services do, prune listing the pool
afresh; without it they share take's listing, as one `--cron` run does.

Its schedule is a run on every tick of real time, on the exact tick. That is harder on take than the units'
timer is, and it is not what production should see: an `OnCalendar=` timer elapses once for a wall-clock time
that comes twice (systemd's source, and a production host's own `systemd-analyze calendar` for 25 October 2026), so no
run starts in the other pass of a fall-back's repeated hour, and the timer's `RandomizedDelaySec=120` puts each
start up to two minutes after the hour (see the correction of 2026-10-11 in `../AUDIT.md`). The `dst_` names it reports are what a scheduler that goes by real time
would get.

`check` (the hand pass, about 25 s) runs the production-shaped `fixtures/sanoid.conf` (Europe/Dublin, hourly,
split, tank at 78% and zroot at 11%, with `tank/software/test` under the recursive `tank/software`)
for 45 days across each clock change of 2026-27, and `FIXTURE_CONF` for 40 days across New York's:
every type, `hourly_min`, both kinds of recursion, `process_children_only`, `skip_children`, a custom
`path` and a pool that defers pruning, every 15 minutes, take and prune sharing a listing. `long` (the
fuzz pass, about 5 minutes) runs the production-shaped one for 4 years, 8 clock changes, and the fixture's for 400 days
with 1 in `n` zfs calls failing (default 1,000). The scenarios run side by side, one process each.

Each check has been shown to catch a deliberately broken pysanoid (scratch mutants, 2026-09-27):
pruning one past the floor, or two hours early; no daily on Wednesdays; a second daily at noon; never
pruning weeklies; take leaving a snapshot out of its return value, or taking a type no section takes;
prune returning a destroy it didn't make; take swallowing its problems; the daily slot moved to noon
(seen by the Perl comparison alone); a refused name not retried. Building them found three faults in
the checks themselves, all fixed: the pile-up check wanted every survivor past the window, where one
is enough; a type's first snapshot, delayed by an injected failure, was judged as if on a slot; and a
single missed slot that the next run made up went unseen, since only the newest snapshot's age was
checked, not the gap between two.

It was written on 2026-09-22 for a 6-year run on CERN's config (a 2-year run found the extra daily,
weekly or monthly in a fall-back's repeated hour that `_after()` now prevents); `--fail-rate` found
the relative-`wd` bug every `run_perl` now guards against, and `--split-take-prune`/`--pool-cap` came
from the production units. It stayed outside `run.py` until 2026-09-27 and broke without failing (see
its row above).

**`dev/e2e_subprocess.py`** -- black-box smoke tests: invokes `pysanoid.py` as a REAL subprocess
(real argv, the real `if __name__ == "__main__"` entry point, a real process exit code) against a
fake `zfs` on PATH, instead of importing it and calling a function in-process like every other test
in this repo does (`test_pysanoid.py`'s tests, every `diff_*.py`, `simulate_long_horizon.py`
included). A handful of hand-picked scenarios, not a fuzzer -- real exit codes for `--version`, an
unknown flag, a negative `--cache-ttl`; a real `--cron` run's actual on-disk side effects;
confirmation that `--readonly` makes literally zero real zfs calls at the process level, not just
at the function level; and a real, two-dataset confirmation that one dataset's take failure doesn't
block pruning for another -- the single most safety-critical claim in `../AUDIT.md`, now verified
as a real subprocess, not just an in-process mock. Since 0.1.34 it also checks that a run directory
no lock file can be made in still lets snapshots be taken (once), that a hook's output stays in
order with pysanoid's own lines on one pipe, that a NUL-damaged snapshots.txt is read as no cache,
and that a `zfs destroy` that can't start doesn't stop the prune run. Since 0.1.35 it checks that output which
can't be written (a closed pipe with SIGPIPE ignored, as systemd sets it, and a log that can't grow) doesn't stop
the run, that a latin-1 stdout gets names as read, and that a cache directory pysanoid can't search doesn't stop
it (a failure only Python 3.13 and earlier show). Since 0.1.36 it checks that `--debug` prints upstream's merge
trace in order and `Data::Dumper`'s dump, stdout and stderr joined as systemd joins them. Since 0.1.37 it checks
that a section above its recursive parent stops the run before any zfs call, and it starts take and prune together
eight times, as the cron jobs in upstream #1063 did (the fake zfs's `get_sleep` slows each listing so they overlap):
in every round one run defers its cache updates to the other, and both must finish cleanly. Since 0.1.38 it
checks that two sections on one dataset stop the run before any zfs call, that a setting upstream doesn't know is
refused in upstream's words, and that a `TZ` set but empty names snapshots in UTC. Since 0.1.39 it checks that a
POSIX rule in `TZ` names snapshots by the rule, that a `TZ` naming no zone names them in UTC with a warning, and that
a zone file cut in its closing rule line, or whose transition names a type it lacks, doesn't stop take (under
Python 3.11, 0.1.38 hung on the first and crashed on the second). Since 0.1.40 it checks that a section newer than
the dataset cache keeps its recursive parent's settings (and that the cache was the one read), that a snapshot cache
dated in the future is read afresh, and that one cut short inside its last line is no cache; on 0.1.39 each of the
three destroys snapshots the config keeps. Since 0.1.41 it checks that a snapshot listing killed inside its last
line goes without that line (the newest daily kept, the run ending as upstream's does with exit 0, the cache
left holding whole lines), that a 400-digit count doesn't stop the prune, and that a daily dated a year ahead holds
the dailies back in silence, exit 0, as upstream has it; two of the five fail on 0.1.40. Since 0.1.42 it checks
that a warning from zfs or zpool in bytes that aren't UTF-8 fails no command (the snapshot taken and cached, the
destroys counted and out of the cache, a pool under `prune_defer` pruned), that a failed snapshot's complaint is
written out in the bytes zfs wrote, under a Latin-1 locale too, and that a dataset's snapshot names carry the time
its turn came (a slow `pre_snapshot_script` is given the name its own dataset then takes, and the next dataset's
is later); six of the seven fail on 0.1.41. Since 0.1.43 it reads pysanoid's output as bytes (its own text mode
had translated a carriage return and refused a byte that isn't UTF-8) and checks that what zfs and zpool write is
shown where upstream shows it: a warning from a snapshot, a destroy or the capacity query that worked, after the
line that announces the command, stdout's before stderr's, each byte as written and none added; a failed
snapshot's or destroy's complaint, then upstream's line with the wait status; a listing's complaint with no
newline of its own; and seven capacity answers, read, refused in upstream's words, or refused as holding more
than a capacity. 13 of its 71 checks fail on 0.1.42, 11 of the 12 new ones. Since 0.1.45 it checks a failed
hook's warning in a real run: the wait status for a hook that exits 3, the signal's number for one a signal killed,
and for a command that isn't there, or `exit 0` as a veto hook, a line saying why and then `failed, -1`, with no
shell's complaint and nothing destroyed; all four fail on 0.1.44. Not registered in
`dev/run.py`'s TARGETS --
that driver's docstring says "each target is one dev/diff_*.py", and this isn't one.

## Judged by what the config promises

Three targets ask another question than the rest. A `diff_*.py` asks whether pysanoid does what upstream does,
which can't see what both do wrong: the 2026-10-10 audit found four ways for prune to destroy snapshots the config
keeps, and upstream shares all four. These three ask whether a run did what its config promises, whatever happened
around it. No Perl runs in them.

They share `lib/`:

- **`world.py`** runs `pysanoid.main` in a forked child against a `World`. zfs and zpool are answered by a pool held
  in memory, the clock is the one the caller sets (pysanoid's `time`, its `datetime.now`, and the date of every
  file it writes), and its config, caches and locks are real files. A child, so that a run can end as a process
  ends, with nothing unwound: what SIGKILL does, and what systemd's SIGTERM does to a run that outlasts its unit's
  timeout, since pysanoid installs no handler. A `Fault` makes one event of a run fail (a file operation on
  pysanoid's own files, a lock, a zfs or zpool command) in one of the ways `kinds_for` lists, or ends the process
  there.
- **`model.py`** states the retention apart from pysanoid's code: the snapshots a take at an instant must create
  (by `slots.py`'s brute-force slots, the answer `dst_schedule` checks `_last_preferred_time` against), the
  snapshots a prune must destroy, and whether a destroy that happened was allowed. What each section takes and
  prunes is read from a clean `load_config`, in a child of its own.
- **`audit.py`** judges a run. Whatever was done to it: nothing leaves `main` but its return, and no destroy breaks
  the retention, each judged against the pool as it stood. A run nothing was done to, that reads the pool as it is,
  is judged exactly: the snapshots that are due and no others, under the names of its local time; the destroys the
  retention gives up and no others; exit 0; nothing of pysanoid's own on stderr; and both caches left current and
  true.
- **`parallel.py`** spreads the work over forked workers (`concurrent.futures` needs semaphores a sandbox may not
  give).

The clock is the system's, and a run is judged by it. Neither the tools nor pysanoid can tell a clock set wrong
from time gone by: a clock set ahead makes snapshots look older, and prune goes by that. What doesn't move with the
clock is the count: no destroy leaves fewer of a type than the section keeps.

"Reads the pool as it is" is where a harness like this can let the program off, so it is worked out, not assumed. A
run that finds a cache an earlier, failed run left wrong can't be held to the exact rules: pysanoid, like upstream,
trusts a cache for 20 minutes. Before each run the auditor decides, by the rules pysanoid states for its caches (a
regular file no older than the TTL and not dated ahead, no NUL in it, its last line ended), whether the run will use
each cache, and whether that cache says what zfs would. A run with a wrong cache is held to the first rule only.
That allowance would cover any bug that leaves a cache wrong, which is why the exact rules end with the caches: a
run judged exactly must leave both as a run just then would read them, saying what the pool holds. Before that rule
was there, a pysanoid that read a cache dated ahead passed everything: its first clean run left the stale cache in
place, and every run after was let off as reading what an earlier run had left.

**`fault_sweep`** takes one config and pool, and one date. For every event of a run, and every way it can fail, it
runs three rounds: the round with the fault, a clean one five minutes later that reads the caches the first left,
and a clean one an hour after the first, when both caches have expired, so that round is judged exactly. On top of
the auditor's rules: the fault fired where planned, and whatever the first two rounds left untaken was reported (a
run was killed or exited non-zero). One class is registered as silent, and must be: a recursive section's child
lookup that fails having listed nothing reads as no children, as upstream reads it (DEV-018), so the
children with no section of their own go without snapshots, exit 0, while that answer is cached. The class is
checked both ways: such a fault must leave children of that section untaken, and those only, and no other fault may
leave anything untaken in silence. Its fixture is built so that each rule can fail: more snapshots inside a type's
window than its count, so a prune that ignores the window shows; a child section whose parent keeps weeklies, which
the defaults don't, so a child built from the defaults loses its own; a dataset that isn't there, so the listing's
retry runs.

**`config_fuzz`** draws a config, a pool and a date, and makes six runs, three of them `--readonly`. Its configs
are mostly ones pysanoid refuses in whole or in part, so nothing is judged exactly; the auditor's first rule is,
and three more: a config pysanoid refuses changes nothing in any mode; where prune refuses a setting of any section
it would prune, no run destroys anything; and a run that exits 0 says nothing on stderr but zfs's words and
upstream's own warnings. Its hand cases are configs that have gone wrong before, each with what its six runs do
pinned.

**`trouble`** is the long run. A scenario is a config, a pool with a past, and 90 to 420 rounds on a clock that
moves, with trouble drawn at random or scripted: any failure of anything, a refused snapshot or destroy, a run
killed at an event or at a destroy partway through a prune, the clock jumping ahead (the machine was off and the
timer makes up one run, or the clock was set wrong), the clock set back, a second round by hand while the caches
are fresh, a pool's capacity crossing what a `prune_defer` waits for. Every scenario ends with rounds free of
trouble, and the last must be judged exactly. The exact rule is the check on the runs after trouble: whatever a run
left, the next run that reads the pool afresh must do exactly what a run on that pool should. A snapshot dated
more than five minutes ahead of the clock is set aside (DEV-051): it holds no type back and fills none of prune's
places, and the same rule says so; the clock set back makes such snapshots, and they are counted. Its hand pass is the production-shaped config through the night its clock goes back and the long-horizon fixture
through New York's, each with every trouble scripted (the script is written for `fixtures/sanoid.conf` as it stands: after a
change to that file, a trouble that no longer happens as scripted fails the pass as `harness`, and `hand()` is what
to adjust); its fuzz pass draws configs (both kinds of recursion, child sections, `process_children_only`,
`skip_children`, a section under another name, `prune_defer`, templates, the schedule settings), zones and dates.

**Shown to fail.** `dev/mutants.py` breaks `pysanoid.py` on purpose, one line at a time, 24 ways, and gives each
broken copy to the three hand passes and a random pass of 60 scenarios. Each must fail at least one, and all 24 do.
`fault_sweep`'s hand pass fails 12 of them, and is the only pass that fails a listing whose cut line is kept and a
short child listing taken for a whole one. `config_fuzz`'s is the only one that fails a count of thousands of
digits read whole, `--readonly` taking or destroying, and a prune that doesn't check its settings first. `trouble`
is the only one that fails the snapshot on its age's boundary destroyed, a snapshot dated ahead counted as upstream
counts it, no `dst_` retry, a cache dated ahead being read, and a cache trusted for twice its TTL. Four of the
24 stood at first, and each stood for a hole in the tools: the two caches dated ahead (see the design notes), the
short child listing (the hand pass did not fail a child lookup partway) and `--readonly` taking (no run took under
`--readonly`). It is not a `run.py` target: it takes about half an hour, and it tests the tools, not pysanoid. Run
`python3 dev/mutants.py` (its `targets` suite) after changing `lib/world.py`, `lib/model.py`, `lib/audit.py` or one
of the three targets. A mutant whose line has gone from `pysanoid.py` counts as not caught, so it can't pass by going
stale, and so does one that no longer compiles.

**The tests and the other harnesses, checked the same way.** Since 2026-10-25 `dev/mutants.py` has three more suites,
their breaks in `lib/breaks.py`: every one-line break written as a change landed, from the alert script's first audit
(2026-10-05) and 0.1.41 on, recovered from the audits' records. `tests` gives 119 breaks of `pysanoid.py` to
`test_pysanoid.py` (pytest -x, about 10 minutes), `alert` 58 breaks of `pysanoid_alert.py` to its tests (3 minutes),
and `harness` 21 breaks to the hand pass of the target each names (`whole_run`, `zfs_output`, `hooks`,
`config_merge`, `overlap`; 30 minutes).
All are caught. Run `tests` after changing the tests, `alert` after changing the alert script's, `harness` after
changing one of those targets, and add a break with every change. Left out of the list on recovery: eight breaks of
behavior since removed (0.1.41's report of a snapshot dated ahead and of a cut listing, both withdrawn, and a
0.1.42 capture 0.1.43 replaced), five of the snapshot listing's decode (zfs names are ASCII, so no listing can tell
them apart), two that can't change a result (0.1.47's, each widening a search by a position that always holds an
`l`) and one faulty draft; eleven whose code had moved were ported to it.

**Not in them.** Hooks, but for `config_fuzz`'s stand-ins that work or fail by name: `hooks` and
`e2e_subprocess.py` run real ones. A pool that goes away for a while, a snapshot someone else destroys. Two runs at
once: `overlap` has them. A cache file damaged on disk (cut short, a NUL in it), which no failure of a single write produces, since a
cache is written beside itself and renamed: `snapshot_enumeration` and `dataset_cache` have those classes. And real
zfs: the pool is a stand-in, here as everywhere in this directory.

## What's *not* here, and why

- **`--monitor-*` (the Nagios checks) and syncoid/findoid.** Out of scope for pysanoid itself, so
  out of scope here too.
- **What hooks and zfs inherit from Python's startup** (SIGPIPE at its default, and `LC_CTYPE=C.UTF-8`
  when no locale is set, where upstream's inherit sanoid's own): registered as harmless and confirmed
  by hand, not a target.
- **A `script_timeout` over 100,000,000 seconds on macOS**, where `alarm()` refuses it and Linux
  sets it: `diff_hooks.py` counts it as a known class, and pysanoid sets it, as Linux does.
- **Perl's own warnings about upstream's code** (`Use of uninitialized value ... at <script> line N.`),
  which name lines of upstream's source: registered as cosmetic, and left out by the harnesses.
- **Stderr lines only one side writes**, left out where `prune` and `take_snapshots` compare stderr, each a
  registered difference: the ` at <script> line N.` Perl adds to a warning, pysanoid's own `ERROR:` reports, and
  what says why a command couldn't start (`could not run ...` here, Perl's own `Can't exec ...` for a hook
  there). Those two harnesses read both sides' stderr as text, a byte that isn't UTF-8
  as U+FFFD; the bytes themselves are compared by `zpool_capacity` and by `e2e_subprocess.py`.
- **The exact same DST-transition scheduling divergence already documented in `_last_preferred_time`
  itself** is not re-derived by `take_snapshots`' fast fuzz loop (which deliberately stays on safe,
  mid-season dates -- see that script's own docstring) -- it's confirmed instead by two dedicated,
  date-pinned HAND cases sitting exactly on 2026's two US DST transitions, using
  `_last_preferred_time` itself to derive the sensitive age window rather than hand-computing DST
  arithmetic for a test about DST arithmetic bugs.

## Design notes (for extending this)

- `dev/upstream/sanoid` is the pinned reference source (see `PROVENANCE.md` for the commit and how
  to move the pin). Every extraction is by subroutine **name** (`dev/lib/extract.py`, brace-matched)
  or, for the two top-level script blocks that aren't inside a named sub (`options`, `flow`), by
  distinctive surrounding **text** (`extract_between`) -- never by line number, which drifts the
  moment upstream changes and would silently compare against stale code instead of failing loudly.
- Perl scoping gotcha, worth knowing before adding a new target: several upstream subs read their
  inputs from the **outer lexical scope** (top-level `my` variables the real script declares
  before calling them), not from their own `@_` parameters -- `blabber`'s `$config`/`$snaps`/...
  parameters are entirely unused; its body reads the file-scope `%config` etc. directly. Whatever
  a new `diff_*.py` extracts, check what globals its body actually touches (`grep -oE '\$\w+|%\w+'`
  over the extracted text is a fast way to find them) and declare them in HEAD *before* the sub
  text, or Perl's `use strict` will refuse to compile it.
- `dev/lib/perlrun.py`'s `run_harness` reads the result from a file (`$ENV{DIFF_OUT}`), not stdout
  -- several of the subs under test print their own `INFO:`/`WARN:` lines when verbose, which would
  corrupt a stdout-as-JSON convention. It never raises on a nonzero exit by itself (some harnesses
  model a legitimate outcome as a direct Perl `exit()`, e.g. `pod2usage(2)` for a bad option, which
  looks identical from the outside to a crash); each script decides what "no result" means for its
  own harness.
- A Python-side scenario generator must produce config values in whatever **normalized** shape
  `load_config` actually hands the function under test (real Python `bool`s for toggles, not the
  raw ini string) -- and the Perl side needs the analogous Perl-normalized shape (`1`/`0`, matching
  what `init`'s own istrue/isfalse pass produces). Feeding the same raw string to both sides is a
  live trap, not a hypothetical one: it happened during this rebuild (`diff_snapshot_enumeration.py`
  fed `"0"` to both sides, which is truthy in Python and falsy in Perl) and would have silently
  tested nothing -- itself an instance of the exact class of bug this whole tool exists to catch.
  If a new scenario generator produces config values, run it through
  `canon_config_for_perl`-style conversion (see that function in `diff_snapshot_enumeration.py`) or
  build the analogous thing for the new target. The same per-side-shape issue shows up beyond
  booleans, whenever upstream stores something in a shape that isn't the obvious one: `getsnapsbytype`
  stores `'sorted'` as a PIPE-JOINED STRING, not a list (`diff_prune.py` fed it a JSON array the
  first time, which stringified to `"ARRAY(0x...)"` and silently produced one fake snapshot name
  instead of ten real ones), and each `(path, type)` entry as a hashref with a `'newest'` key, not
  a bare age integer (`diff_take_snapshots.py` hit the same thing one level up: a bare number where
  a hashref was expected dies under `strict refs`). Both found by actually running the harness
  against a real scenario, not by reading the source -- the general lesson repeats: know the exact
  shape upstream's OWN code expects at the point you're injecting scenario data, not just its type.
- A scenario config's SECTION NAME should equal its dataset `path` unless a test is deliberately
  about the two differing -- prune_snapshots and take_snapshots both look hook commands up via
  `$config{$dataset}` (the section literally named by the dataset path), not via the config for the
  section actually being processed (see DEV-012). A scenario with
  section name `"a"` and `path = "tank/a"` silently makes every hook invocation a no-op on the Perl
  side, for a reason that has nothing to do with whatever the test was trying to check -- found by
  every hook-related HAND case in `diff_prune.py` failing at once, until the section names were
  changed to match their paths.
- A stubbed-out counter or log driven by a Perl `foreach my $x (keys %hash)` loop over more than one
  key inherits that hash's per-process-random iteration order, even when the thing being counted
  (a cumulative total, an accumulating list) looks like it should just be a fixed pair of numbers.
  `diff_prune.py`'s cache-flush log first recorded `scalar(keys %pruned)` (a running total) once
  per type a section prunes; comparing those cumulative numbers directly passed roughly half the
  time and fell over the other half, depending on which type Perl happened to visit first that
  process -- not a real bug, just an order-sensitive comparison of an order-insensitive property.
  Its stubs now follow upstream's `undef %pruned`, so each applied flush logs only its own batch,
  compared as a sorted list. Diff per-batch sizes rather than running totals when in doubt, and
  re-run a fuzz/hand suite a few times after writing it, specifically looking for this class of
  flake, before trusting a single green run.
- A stub that simplifies what upstream does can hide exactly the difference being tested. The
  first `diff_prune.py` logged every flush call but never cleared anything, so it could not see
  pysanoid re-flushing the whole run at the end; its clock was a whole second on both sides, so a
  fractional one couldn't show; and its hooks were always the probe script, so a hook set to `0`
  never came up. Each gap hid a real divergence. The take harness's `tee_stderr` was another: it
  captured zfs's stderr and handed it to upstream's code, as the real one does, but did not also
  pass it on to the terminal, so for as long as it existed the harness could not see that upstream
  shows a warning from a snapshot that worked.
- A deliberate refusal classed as known is checked against its rule, stated apart from the code that
  refuses (`lib/configorder.py` for the section order), both ways: a refusal the rule doesn't call for
  fails, and so does a file the rule refuses that pysanoid loads. And a generator that makes such files
  freely spends that share of its runs checking only the refusal: once pysanoid refused misplaced
  sections, 23% of `config_merge`'s fuzzed configs no longer compared the merge, until it put parents
  first most of the time.
- A fuzz can generate a case for years and show nothing, when both programs are wrong the same way.
  `dataset_cache` built a section under a recursive section from the defaults in 5 to 13 of every 500
  scenarios, in upstream and in pysanoid alike, so it matched. Parity can't see that; only asking what
  the config promises can. And a class that merely permits a difference would pass a pysanoid that got
  the new behavior wrong, so `UNLISTED` checks each such section key by key against the rule
  (`lib/configorder.py`), as the refusals are checked.
- Add to a generator from a random stream of its own (seeded from the scenario), not from the main one:
  a draw added to the main stream changes every scenario after it, and a seed's earlier results stop
  being comparable.
- A class is flagged from the scenario, so it must cover every way the scenario gets there. `retry_cut`
  kills a `zfs get` naming no dataset; with no dataset to name, that is the first listing, not the retry,
  and upstream keeps a first listing killed by a signal: two fuzzed scenarios differed until the class for
  a killed first listing counted them too.
- When the two sides do the same things in different orders and what they print depends on the order (the time
  in a snapshot's name, on a clock that moves), compare in two steps: check each side against the rule from its
  own log, then rewrite the order-dependent part away and compare the rest. Try the rule on the wrong answer
  first. "A dataset's time is the start plus what ran for the datasets with an earlier time" is true of names
  that all carry the start, which is what 0.1.41 gave; sorting the datasets by time and adding up what ran
  before each one is not.
- A harness that hands pysanoid a value a real run reads for itself (`now=`) tests the handing over. The take
  harness passed its clock in for as long as it existed, and for that long could not see when pysanoid read the
  clock. Patch what the real path reads instead.
- `simulate_long_horizon.py` builds take and prune scenarios of its own and calls `run_perl` of both harnesses, so
  a scenario key added to either is read with `.get` there. The take harness's `tick` was not at first, and the
  simulator's four runs failed in the gate, each naming the harness, not pysanoid. It also knows the words of a
  failed destroy's line: when 0.1.43 changed them, its hand pass, which injects no failure, still passed, and its
  long pass failed on every injected one. After a change to what a failure prints, run
  `python3 dev/run.py --fuzz long_horizon` before the whole gate.
- A known class is a difference someone decided to live with, and it stays decided only while what it hides
  stays hidden. `hooks` counted "Perl's system() returns -1, /bin/sh's exec exits 127" as known from 0.1.34 on,
  since both are a failed hook to every caller. When 0.1.45 put the status into the warning, as upstream has it,
  the class became a line that read differently, and the right answer was to remove the difference, not to
  document it. Read the known classes again whenever a change touches what they cover.
- Compare what a command printed as bytes when bytes are the point. `e2e_subprocess.py` read pysanoid's output in
  text mode, which turned a carriage return into a newline and refused a byte that isn't UTF-8: the two things
  0.1.43's checks are about. A fake that answers from a file of hex (`fake_zfs/zpool`) gives both sides the same
  bytes without a shell in between.
- A probe nothing runs goes wrong without saying so. The audit's config fuzz read a hook's command line as
  `/bin/sh -c <script>`, and was rebuilt on the day 0.1.45's change to how a hook is started would have made every
  config with a hook raise in it. `simulate_long_horizon.py` did the same in September. What is worth running
  again belongs in `run.py`.
- A fixture has to make each rule reachable. `fault_sweep`'s pool first held nothing inside a type's window beyond
  its count, so a prune that ignored the window passed it; and its template first kept nothing the defaults don't,
  so a child section built from the defaults lost nothing. Break the rule in pysanoid and see the target fail
  before trusting that it checks the rule.
- A cache that never expires does no harm while every run keeps it true, so a clock set back is not enough to show
  one being read: something has to leave the cache wrong while the clock is ahead. `trouble` passed a pysanoid that
  read caches dated ahead in 60 random scenarios, until half its short jumps ahead became one round ahead with a
  failure in it and then the clock set back.
- An allowance for what an earlier failure left behind needs a rule for what a clean run leaves, or it covers the
  bug that leaves it (see "Judged by what the config promises").
- The exact rules give a difference for every run after the first wrong one. Read the first.
- Python 3.11 has no backslash inside an f-string's braces; 3.12 and later take one. ruff says so because
  `ruff.toml` names 3.11: a script that only ever ran under 3.14 would have failed the gate's other half.
- Every `diff_*.py` exits non-zero on any unexplained divergence and 0 otherwise, so `run.py` (and
  CI, if this is ever wired into one) can just check the exit code.
