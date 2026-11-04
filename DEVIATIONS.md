# pysanoid: deviations from upstream sanoid

pysanoid matches upstream sanoid 1:1 unless doing so has a plausible path to data loss. This file lists every
place it doesn't, and every place where matching upstream needed a note of its own.

What stays the same everywhere: the config files' format, how sections and templates merge, snapshot names, and the
cache files' layout. An existing `sanoid.conf` and `sanoid.defaults.conf` work unchanged.

- Upstream reference: sanoid master `d39b51a` (the v2.3.0 line), vendored at `dev/upstream/sanoid`.
- **DEV-NNN** is a deliberate deviation. **PN** is parity that needed a note (pysanoid does what upstream does,
  for a reason that isn't obvious from the code).
- IDs are permanent. A deviation that is later removed keeps its entry, marked "Retired in 0.1.x"; its number
  is never reused.
- One rule that covers several distinct cases has a lettered entry for each (DEV-039a, DEV-039b, ...) under a
  short entry stating the rule. A code comment names the letter it applies to.
- In `pysanoid.py`, a deviation is marked with its ID and one line, e.g. `# DEV-002: ...`. The full reasoning
  lives here.
- "Confirmed against real Perl" means upstream's code was run, not assumed. "Upstream reports" names issues and
  pull requests in github.com/jimsalterjrs/sanoid where sanoid's users met the same problem.
- How each was found, and when: `AUDIT.md`.

Each entry has: what upstream does, what pysanoid does, why (the data-loss path, or "harmless"), the evidence,
upstream reports where there are any, and where it lives in the code and the tests.

## Config loading

### DEV-001: Every value is trimmed, and a repeated key reads as its first value

- **Upstream:** trims a section's own values, and keeps the first of a repeated key, only when its section loop
  reaches that section. `sanoid.defaults.conf` is never trimmed. A template or `[template_default]` placed below
  the sections that use it, as templates usually are, reaches them untrimmed, and a repeated key of
  `[template_default]`'s arrives as a Perl array reference.
- **pysanoid:** trims every value and takes the first of a repeated key, wherever it comes from.
- **Why:** read untrimmed, `autoprune = no ` (a trailing space) prunes, `process_children_only = no ` and
  `no_inconsistent_snapshot = no ` silently stop snapshots, and `prune_defer = 80 ` stops the prune run.
  Dangerous for the data.
- **Side effects (harmless):** `autosnap = no ` takes no snapshots here, where upstream takes them. A count
  repeated in a `[template_default]` below its sections keeps its first value, where upstream reads the array
  reference as a large number and keeps every snapshot.
- **Evidence:** confirmed against real Perl.
- **Upstream reports:** fixed upstream since for a section's own values (#834, #901: a trailing space turned
  recursion off) and for a template's repeated key (#952: autosnap came on).
- **Code:** `_section_dict`
- **Tests:** `test_a_template_below_its_sections_is_trimmed_before_it_is_applied`,
  `test_a_repeated_key_in_a_template_default_below_its_sections_reads_its_first_value`,
  `test_sanoid_defaults_conf_values_are_trimmed`, `test_a_trailing_space_after_recursive_still_reaches_the_children`,
  `test_a_repeated_autosnap_no_keeps_autosnap_off`
- **Harness:** `dev/diff_config_merge.py` known classes "untrimmed" and "arrayref"

### DEV-002: An unrecognized toggle value is an error

- **Upstream:** leaves a toggle value that is neither true nor false (`autoprune = noo`, `autoprune = no # off`)
  as a string, which Perl reads as true, so the setting stays ON.
- **pysanoid:** refuses the config at load, naming the key, the section and the values it accepts. A value is
  trimmed of Perl's `\s` only (DEV-001), so a word with a no-break space, NEL, U+2028, U+3000 or U+001C-001F
  around it is refused too. Until 0.1.49 `str.strip()` removed those and read `no<NBSP>` as "no", silently.
- **Why:** a typo made while turning pruning off leaves pruning on. Dangerous for the data.
- **Evidence:** confirmed against real Perl (`autoprune = no<NBSP>`: upstream pruned 4 of 6 snapshots).
- **Code:** `_normalize_toggles`
- **Tests:** `test_an_unrecognized_toggle_value_is_refused`, `test_a_toggle_padded_with_a_space_perl_keeps_is_refused`,
  `test_a_toggle_padded_with_perl_whitespace_is_read`
- **Harness:** `dev/diff_config_merge.py`, known class `toggle pad`

### DEV-003: A `use_template` naming an undefined template is an error; a tab after a comma is tolerated

- **Upstream:** applies nothing for a template that isn't defined (`keys %{$ini{$template}}` is empty) and runs
  the dataset on the raw defaults. It splits `use_template` on `' *, *'` only, so `a,<TAB>b` looks up a template
  literally named `"\tb"`, finds none, and silently applies nothing for it.
- **pysanoid:** refuses a config naming an undefined template. All whitespace around each name is ignored, so the
  templates the operator meant are the ones applied. That includes what Perl's `\s` doesn't match and `str.strip()`
  does: `use_template = home<NBSP>` applies `[template_home]`, where upstream applies nothing. A template with no
  settings of its own is still valid (the check is that the section exists).
- **Why:** a dataset silently left on the defaults is pruned by their retention. In the 2026-09-02 incident, the
  default `weekly = 0` destroyed every weekly snapshot. Dangerous for the data.
- **Evidence:** confirmed against real Perl and upstream's own `init` (`home<NBSP>`: upstream destroyed none of 40
  dailies by the defaults' `daily = 90`, pysanoid 35 by the template's `daily = 5`).
- **Upstream reports:** #702, #938 and #1038 (a `template_` prefix, quotes around the name). An open pull
  request would warn instead (#1039).
- **Code:** `load_config`
- **Tests:** `test_use_template_list_tolerates_a_tab_after_a_comma`,
  `test_a_use_template_spelled_as_upstreams_users_mistyped_it_is_refused`,
  `test_a_template_name_padded_with_a_space_perl_keeps_is_applied`
- **Harness:** `dev/diff_config_merge.py` hand case for `a,<TAB>b`, and its known class `template pad`

### DEV-004: `recursive` and `skip_children` are read by membership, and other values are refused

- **Upstream:** reads a value that is none of true, false or zfs as "not recursive", so a typo
  (`recursive = yess`) leaves the children unmanaged. It also compiles the raw value as a regex
  (`grep(/^$value$/, @istrue)`): `recursive = .` is true, and `recursive = (` crashes the run.
- **pysanoid:** a plain membership test. Any other value is refused at load. (`zfs` anywhere in the value, in any
  case, still means native ZFS recursion, as upstream's `/zfs/i` reads it.)
- **Why:** a typo silently leaves a tree of datasets without snapshots. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Code:** `load_config`
- **Tests:** `test_a_recursive_or_skip_children_value_that_is_no_word_is_refused`,
  `test_recursive_holding_zfs_anywhere_in_any_case_is_native_zfs_recursion`,
  `test_recursive_reads_a_true_or_false_word_by_membership`
- **Harness:** `dev/diff_config_merge.py` known class `membership`, with hand cases for `yess`, `.`, `(` and a
  `skip_children = zfs`

### DEV-005: A section under a recursive or skip_children section must come after it

- **Upstream:** reads sections in file order. A recursion replaces every setting of a section above it for one
  of its datasets, and `skip_children` deletes that section, so the dataset is taken and pruned by its parent's
  settings, or not at all. Placed below, its own section overrides them, as upstream's comments and example
  config describe.
- **pysanoid:** refuses the config, naming both sections and saying to move the child below its parent. The rule
  goes by name, so it is refused before the dataset exists.
- **Why:** a child keeping 12 monthlies and 5 yearlies, placed above a parent keeping none, lost all 17 to prune.
  Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Upstream reports:** #1021 (open).
- **Code:** `load_config`
- **Tests:** `test_a_child_section_above_its_recursive_parent_is_refused`,
  `test_a_nested_recursive_section_above_its_parent_is_refused`,
  `test_a_child_section_above_a_skip_children_parent_is_refused_and_below_it_is_kept`,
  `test_main_refuses_a_child_section_above_its_recursive_parent_before_pruning_anything`,
  `test_the_section_order_rule_follows_the_recursive_sections_path`
- **Harness:** `dev/diff_config_merge.py` known class "order", checked against `dev/lib/configorder.py`

### DEV-006: A section under a recursive section starts from its settings, listed or not

- **Upstream:** gives the parent's settings only to the children its `zfs list` lookup returned. A section for a
  dataset the lookup didn't list is built from the defaults. A dataset goes unlisted while a `datasets.txt` older
  than it is still in use (`--cache-ttl`, 20 minutes by default), or when the lookup fails having listed nothing.
- **pysanoid:** a section named under a recursive section starts from that section's settings whether or not the
  lookup listed its dataset. `datasets.txt` still holds only what zfs listed. Under `skip_children` such a section
  starts from the defaults either way, as upstream's does.
- **Why:** the defaults' retention prunes the child. A child meant to keep its parent's 4 weeklies, 12 monthlies
  and 3 yearlies lost 13 of the 19 to the defaults' 0, 6 and 0. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Code:** `load_config`
- **Tests:** `test_a_section_under_a_recursive_section_starts_from_its_settings_though_unlisted`,
  `test_an_unlisted_grandchild_starts_from_the_nearest_recursive_section_above_it`,
  `test_a_section_under_a_skip_children_section_still_starts_from_the_defaults`,
  `test_main_keeps_a_childs_snapshots_when_the_dataset_cache_is_older_than_its_section`
- **Harness:** `dev/diff_dataset_cache.py` known class UNLISTED, checked against `dev/lib/configorder.py`;
  `dev/e2e_subprocess.py` `test_a_section_newer_than_the_dataset_cache_keeps_its_parents_settings`

### DEV-007: Two sections that take or prune the same dataset are refused

- **Upstream:** takes and prunes a dataset by every section that names it: one naming it with `path =` beside
  another section for it, or one inside a recursive section's tree.
- **pysanoid:** refuses the config once the merge is done, naming both entries. An entry a recursion made is named
  after the recursive section. A section that neither takes nor prunes, or has `process_children_only`, doesn't
  count, so a recursive parent that skips itself can still have a section of its own for the parent.
- **Why:** each type keeps only what the stricter retention allows, and a type one section leaves at the default
  0 loses every snapshot the other keeps. A section added for hourlies destroyed 4 of 4 weeklies, and one keeping
  12 monthlies under a recursive section keeping none, 5 of 5. Both also take the same snapshot: upstream's second
  attempt fails, and under `recursive = zfs`, in some of Perl's hash orders, the whole tree's. Dangerous for the
  data.
- **Evidence:** confirmed against real Perl.
- **Code:** `_refuse_shared_datasets`
- **Tests:** `test_two_sections_that_take_or_prune_one_dataset_are_refused`,
  `test_main_refuses_two_sections_on_one_dataset_before_pruning_anything`
- **Harness:** `dev/diff_config_merge.py` known class "shared" (pysanoid loaded with and without the check)

### DEV-008: A malformed retention count or prune_defer stops the prune run before its first destroy

- **Upstream:** numifies a count: `3o` is 3, blank is 0, `1_0` is 1. A blank or negative count prunes EVERY
  snapshot of that type (`""` is 0 in `$count * $period`; -5 satisfies `$n > -5` forever), and `3o` typed for 30
  prunes to 3. `init` validates neither.
- **pysanoid:** a retention count or `prune_defer` that is blank, negative, `3o`, `1_0` or written in another
  script's digits stops the prune run before its first destroy. Only `[0-9]+` is accepted. `0` is documented
  ("immediately prune any of those type snapshots already present") and still honored. Taking reads such a count
  as upstream does, reported (P1).
- **Why:** blank and negative are typos that would wipe a type in one run, and a mistyped number prunes to the
  wrong count. Dangerous for the data.
- **Parity kept, for reference:** a blank `prune_defer` is fatal upstream too. A count may end in a comment
  (`daily = 7 # keep a week` is 7), as upstream numifies it; one with no number (`# off`) is refused like a blank.
  A count of more than 18 digits is no error: it reads as a number no dataset reaches, as upstream's float or
  infinity does, so none of that type is pruned.
- **Evidence:** confirmed against real Perl; the retention arithmetic against upstream's own prune loop.
- **Code:** `_cfg_count`, `_preflight_prune_config`
- **Tests:** `test_cfg_count_rejects_blank_negative_and_permissive_forms`,
  `test_retention_count_still_rejects_comment_only_and_typo_values`, `test_prune_preflight_aborts_before_any_destroy`,
  `test_a_count_too_long_for_a_number_reads_as_one_no_dataset_reaches`,
  `test_prune_keeps_every_snapshot_of_a_type_whose_count_is_too_long_for_a_number`,
  `test_main_prunes_the_other_sections_when_a_count_is_too_long_for_a_number`
- **Harness:** `dev/diff_prune.py` hand cases for a count too long for a number;
  `dev/e2e_subprocess.py` `test_a_count_too_long_for_a_number_does_not_stop_the_prune`

### P11: A number may end in a comment

- **Upstream:** Config::IniFiles keeps a trailing comment in a value (`hourly_min = 30 # half past`), and Perl's
  numification reads only the leading digits: 30.
- **pysanoid:** every numeric setting ignores a trailing `# comment` or `; comment`, as upstream's reading does. A
  comment alone reads as blank: P1 and DEV-008 say what blank means for each kind of setting. `prune_defer` stays
  digits only, as upstream's own check of it is.
- **Why noted:** it looks like comment support the ini grammar lacks; it is upstream's numification.
- **Evidence:** confirmed against real Perl (`daily = 7 # keep a week` is 7).
- **Code:** `_TRAILING_COMMENT_RE`, `_cfg_int`, `_COUNT_COMMENT_RE`, `_cfg_count`
- **Tests:** `test_numeric_schedule_settings_ignore_a_trailing_comment`,
  `test_retention_count_may_carry_a_trailing_comment_like_upstream`,
  `test_retention_count_still_rejects_comment_only_and_typo_values`, `test_prune_defer_stays_digits_only_even_with_a_comment`,
  `test_a_config_with_inline_comments_on_its_counts_plans_and_prunes`

### DEV-009: A NUL byte in sanoid.conf or sanoid.defaults.conf refuses the file

- **Upstream:** loads it, and each command the byte reaches is cut short there. Its snapshot listing drops every
  dataset sorted after that section, so take re-takes their snapshots each time the cache is refreshed and prune
  never sees them. A recursive section lists the children of the truncated name (`tank/b` for `tank/b<NUL>x`) and
  snapshots them with its own settings.
- **pysanoid:** a NUL byte in any section name, key or value refuses the file, like any other config error.
- **Why:** snapshots re-taken without end, datasets prune never sees, and the wrong datasets snapshotted under
  the wrong settings. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Code:** `_refuse_nul`
- **Tests:** `test_a_nul_byte_in_sanoid_conf_is_refused`, `test_a_nul_byte_in_sanoid_defaults_conf_is_refused`

### DEV-010: An empty here-doc in sanoid.defaults.conf's [template_default] reads as blank

- **Upstream:** reads an empty here-doc value (`key = <<EOT`, then `EOT`) as undefined. Only a hand-edited
  defaults file has one. Upstream then refuses that key, as one it doesn't know, in every section of sanoid.conf
  that sets it.
- **pysanoid:** reads it as a blank value.
- **Why:** harmless. A section that sets the key uses its own value, and one that doesn't gets the blank value,
  read as these entries describe.
- **Evidence:** confirmed against real Perl.
- **Code:** `_section_dict`
- **Tests:** `test_an_empty_here_doc_reads_as_a_blank_value` (the `sanoid.defaults.conf` case; the same reading in
  sanoid.conf is DEV-049)

### DEV-050: sanoid.defaults.conf's version must be plain digits

- **Upstream:** numifies `[version]`'s `version` and compares it with the minimum, 2: `2a` and `2.5` load (with
  Perl's "isn't numeric" warning for `2a`), and `1_0` reads as 1 and is refused.
- **pysanoid:** ASCII digits only (`02` is 2). Anything else is refused at load, naming the value. Until 0.1.48 it
  read the value with `int()`, which took `1_0` as 10, `+2`, and other scripts' digits.
- **Why:** harmless and loud: the run stops before anything is taken or pruned, on a file that isn't user-editable,
  and `1_0` is refused as upstream refuses it. Decided 2026-10-25.
- **Evidence:** confirmed against real Perl (`1_0`, `2.5`, `+2`, `2x`).
- **Code:** `load_config`
- **Tests:** `test_the_defaults_version_is_read_as_plain_digits`
- **Harness:** `dev/diff_whole_run.py` known class, versions `2.5`, `+2` and `2x`

### P13: The ini grammar is Config::IniFiles' own

- **Upstream:** reads its config through Config::IniFiles with default options.
- **pysanoid:** a port of that module's line grammar, not Python's configparser, which is another dialect: it applies
  a `[DEFAULT]` section's keys to every dataset, keeps ` tank ` untrimmed as a section name, accepts `key: value` and
  text after `[section]`, refuses here-docs, fails on a byte that isn't UTF-8 inside a comment, and accepts an empty
  or comment-only `sanoid.conf`, so the units run green while managing nothing. Ported: the file's first line ending
  separates every line; blank and `;`/`#` lines are skipped; a repeated section merges; a value is kept verbatim,
  trailing whitespace included; a here-doc runs to a line equal to its marker; a repeated key keeps every value (and
  an empty here-doc swallows the values after it, as upstream's `push()` onto undef does); a byte-order mark encoded
  twice is stripped from the first line, as upstream's pattern matches it, and a real one stays a malformed line;
  a file with no sections is an error. The bytes are never decoded strictly.
- **Why noted:** porting the small grammar removes a whole class of difference that configparser would bring.
- **Evidence:** fuzzed against Config::IniFiles 2.94, 3.000003 and 3.003000.
- **Code:** `_ini_lines`, `_parse_ini_text`, `_read_ini`
- **Tests:** `test_ini_first_line_ending_is_the_separator_for_the_whole_file`,
  `test_crlf_comments_and_duplicate_section_blocks_parse_like_upstream`, `test_here_doc_value_is_accepted`,
  `test_empty_here_doc_makes_a_repeated_key_lose_its_later_values`, `test_utf8_bom_is_refused_like_upstream`,
  `test_a_bom_encoded_twice_is_stripped_like_upstream`, `test_ini_lines_strips_a_bom_encoded_twice_from_the_first_line_only`,
  `test_non_utf8_byte_in_a_comment_is_harmless`, `test_ini_parameters_and_trailing_comments_are_read_as_upstreams_regexes_read_them_fuzzed`,
  `test_a_long_ini_line_built_to_backtrack_is_read_at_once`, `test_for_any_text_the_ini_parser_returns_errors_and_never_raises`
- **Harness:** `dev/diff_ini_grammar.py`

## Pruning

### DEV-011: Every setting prune reads is checked before the first destroy

- **Upstream:** checks each setting inside its section loop, in Perl's random hash order, and dies at the first
  bad one, after earlier sections have already destroyed snapshots: a half-applied run.
- **pysanoid:** checks every setting the destructive path will read, for every section it will act on, before the
  first `zfs destroy`. A config error means nothing is destroyed anywhere until it is fixed. It follows prune's own
  skip rules (template prefix, `autoprune` off, `process_children_only`), so a typo in a section prune would never
  touch doesn't block the valid ones.
- **Why:** prune fails closed and all-or-nothing. Which sections a failing run has already pruned shouldn't depend
  on hash order. Dangerous for the data.
- **Code:** `_preflight_prune_config`
- **Tests:** `test_prune_preflight_aborts_before_any_destroy`, `test_prune_preflight_ignores_sections_pruning_would_skip`

### DEV-012: A section's own pre_pruning_script also runs when its name differs from its path

- **Upstream:** reads hooks only from `$config{$dataset}`, the section named after the dataset. A section with a
  custom `path =` never runs its own `pre_pruning_script`.
- **pysanoid:** runs upstream's hook first and then, when it differs, the pruning section's own. Either can veto. A
  hook set the same in both runs once.
- **Why:** with two sections on one path (a recursive parent's veto hook inherited by a child plus a custom
  `[b] path = tank/a`), upstream vetoed and pysanoid had destroyed. A veto can only stop a destroy, never force
  one, so running both is the safe reading. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Code:** `prune_snapshots`
- **Tests:** `test_prune_honors_the_veto_hook_of_the_section_named_after_the_dataset`,
  `test_prune_runs_both_differing_veto_hooks_and_either_can_veto`, `test_prune_still_runs_a_custom_path_sections_own_veto_hook`,
  `test_prune_runs_a_veto_hook_set_in_both_sections_once`
- **Harness:** `dev/diff_prune.py` hand case "two sections on one path, different veto hooks: both run (documented)";
  `dev/diff_whole_run.py` known class, hand case for hooks on a `path =` section

### DEV-013: Upstream's mid-run re-read of the snapshot list is not reproduced

- **Upstream:** re-reads the snapshot list after take's cache add and after each prune batch's cache flush,
  listing zfs again if the cache has expired by then, and a prune later in the same run decides on what it read. A
  cache add records a snapshot's creation as the time `zfs snapshot` returned, zfs's own or later.
- **pysanoid:** decides the whole run on the list it read at the start.
- **Why:** harmless. The re-read can make upstream destroy a snapshot on its age boundary one run sooner; pysanoid
  keeps it for that run, and nothing is destroyed sooner here. Only a type whose count x period is shorter than the
  cache's life (the TTL plus a run interval) can reach it. Upstream can also run one more `zfs get` and leave a
  fresher cache.
- **Evidence:** confirmed against real Perl.
- **Code:** `prune_snapshots`, `remove_cached_snapshots` (no re-read)
- **Tests:** none found by name.
- **Harness:** `dev/diff_snapshot_cache_mutation.py` standing check `verify_upstream_reread_moves_boundary`
  (hand mode)

### DEV-014: Snapshots made in the same second are pruned in listing order

- **Upstream:** orders snapshots of one type made in the same second by Perl's per-process random hash order, so
  either can go.
- **pysanoid:** keeps the order zfs lists them, which is creation order.
- **Why:** harmless. The same number of snapshots of the same age is kept either way.
- **Evidence:** confirmed against real Perl (upstream's order over 40 hash seeds).
- **Code:** `snapshots_by_type`
- **Tests:** `test_snapshots_made_in_the_same_second_keep_the_order_zfs_lists_them`
- **Harness:** `dev/diff_snapshot_enumeration.py` compares same-second snapshots as a set

### DEV-015: `--cache-ttl=0` is honored

- **Upstream:** its truth test (`if ($args{'cache-ttl'})`) reads 0 as not given and keeps the 20-minute default.
- **pysanoid:** 0 means what it says: every run lists zfs.
- **Why:** a cache can still list snapshots destroyed outside sanoid. Counted toward a type's keep-at-least number,
  they let prune destroy one the retention keeps: a `daily = 3` dataset was left with 2. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Code:** `main`
- **Tests:** `test_main_runs_the_same_steps_as_upstream_for_each_flag_combination` (the `--cron --cache-ttl=0` case)
- **Harness:** `dev/diff_flow.py` canonicalizes this case

### DEV-016: prune_defer's capacity answer must be one capacity and nothing more

- **Upstream:** reads the first word of `zpool list -H -o cap`'s first line and prunes by it, whatever follows: a
  second word or a second line.
- **pysanoid:** reads it as upstream does, and refuses (FATAL: the run stops before that section prunes) an answer
  holding more than that capacity. Only Perl's `\s` (ASCII space, tab, newline, carriage return, form feed, vertical
  tab) around it is nothing more: a no-break space, NEL, U+2028, U+3000 or U+001C-001F after it, which `str.strip()`
  also trims, is more.
- **Why:** with more in the answer, the first word needn't be this pool's capacity, and a number that isn't prunes
  a section its `prune_defer` holds back: `85%` with a line after it pruned 3 of 5 dailies under
  `prune_defer = 50`. Dangerous for the data. A character real zpool never prints after its capacity says the
  answer isn't the one expected, so it is refused like any other second value. Real zpool gives one pool's capacity
  alone on a line, and an answer neither can read stops both.
- **Parity kept, for reference:** the first word must be `[0-9]{1,3}%`; one that isn't stops the prune in
  upstream's words. What zpool wrote to stderr goes out as it wrote it.
- **Evidence:** confirmed against real Perl; every answer pysanoid reads, the version before read too, with the
  same number. Until 0.1.51 a no-break space on the line after `85%` was trimmed, and a whole run pruned 3 of 5
  dailies under `prune_defer = 50`, as upstream does.
- **Code:** `get_zpool_capacity`
- **Tests:** `test_a_capacity_alone_on_its_line_is_read`, `test_an_answer_holding_more_than_a_capacity_stops_the_prune`,
  `test_what_perl_does_not_call_whitespace_after_a_capacity_stops_the_prune`,
  `test_perls_whitespace_after_a_capacity_is_nothing_more`
- **Harness:** `dev/diff_zpool_capacity.py` (its known class states the rule with Perl's `\s`);
  `dev/e2e_subprocess.py` `test_the_capacity_answer_is_read_as_upstream_reads_it`

### P19: prune_defer's pool and its zero are upstream's

- **Upstream:** skips the capacity query only when `prune_defer` is the string `0` (`$limit eq 0`), so `00` still asks
  the pool and defers at 0%. It takes the pool from the section's name, not its resolved path, so a section with a
  custom `path =` whose name doesn't start with its pool fails there. A blank or missing `prune_defer` is fatal.
- **pysanoid:** the same in each case. Each pool is asked once a run.
- **Why noted:** reading `00` as zero, or the pool from the path, looks like the fix, and would part from what
  upstream defers.
- **Evidence:** confirmed against real Perl.
- **Code:** `_prune_defer_limit`, `check_prune_defer`, `prune_snapshots`
- **Tests:** `test_check_prune_defer_skips_the_pool_only_for_the_string_0`, `test_check_prune_defer_00_fails_when_the_pool_cannot_be_queried`,
  `test_prune_snapshots_defers_prune_defer_00_on_an_empty_pool`, `test_prune_snapshots_defers_until_pool_capacity_exceeds_limit`,
  `test_check_prune_defer_queries_each_pool_once`, `test_check_prune_defer_missing_key_is_fatal`,
  `test_a_defaults_file_without_prune_defer_aborts_prune`
- **Harness:** `dev/diff_prune.py` (sections named and pathed apart)

### P20: Prune flushes the cache batch by batch, as upstream does

- **Upstream:** after each (section, type) batch removes what it destroyed from the snapshot cache without waiting for
  the lock (`removecachedsnapshots(0)`), and at the end flushes, waiting, whatever a busy lock deferred
  (`removecachedsnapshots(1)`). A destroy that fails is `could not remove`, and the run goes on.
- **pysanoid:** the same. The closing flush does nothing when every batch was flushed, and a failed destroy, whose
  snapshot is kept, doesn't make the run exit non-zero.
- **Why noted:** flushing once at the end looks simpler, and isn't safe: a run killed midway would leave a cache
  listing destroyed snapshots, whose count lets the next run prune one the retention keeps.
- **Evidence:** confirmed against real Perl.
- **Code:** `prune_snapshots`, `remove_cached_snapshots`
- **Tests:** `test_prune_flushes_cache_incrementally_per_batch`, `test_prune_survives_interruption_after_first_batch`,
  `test_prune_applies_a_deferred_batch_flush_before_returning`,
  `test_main_prune_closing_flush_is_a_no_op_once_every_batch_was_flushed`, `test_a_failed_destroy_is_given_as_upstream_gives_it`
- **Harness:** `dev/diff_prune.py`

## Dataset selection and the dataset cache

### DEV-017: Dataset and pool names reach zfs and zpool as separate arguments

- **Upstream:** splices dataset and pool names into a shell command line for `zfs get`, `zfs list` and
  `zpool list`, so a name containing a space is split in two. The snapshot query fails and falls back to every
  dataset (same result). The recursion lookup finds no children and the run carries on without them (see DEV-006
  and DEV-018 for what that does to a child configured by name). `prune_defer`'s capacity query reads another pool
  or fails. Upstream's `zfs destroy` and `zfs snapshot` calls are argument lists already.
- **pysanoid:** passes each name as its own argument.
- **Why:** a port mechanism, and the safer one: a split name loses a recursive section's children and can read the
  wrong pool's capacity.
- **Code:** `get_snapshots`, `ZfsClient.get_child_datasets`, `get_zpool_capacity`
- **Tests:** none found by name.
- **Harness:** `dev/diff_whole_run.py` known class, hand case for a recursive section whose name holds a
  space

### DEV-018: A child lookup at load that can't start, is killed, or fails after listing something stops the run

- **Upstream:** takes whatever the lookup returned as all of the children. A child it missed that has a section of
  its own is set up from the defaults instead of its parent's settings and pruned by the default retention.
- **pysanoid:** stops the run (FATAL) when the lookup can't start, is killed by a signal, or exits non-zero after
  listing something.
- **Why:** a parent's `hourly = 100` became the default 48, and 12 of 60 hourlies were destroyed. Dangerous for the
  data.
- **Parity kept, for reference:** a lookup that fails having listed nothing reads as no children, as upstream reads
  it, whether its parent isn't there or zfs couldn't say. A child with a section of its own keeps its parent's
  settings even so (DEV-006). Children with no section of their own get no snapshots while that answer is cached.
- **Evidence:** confirmed against real Perl.
- **Code:** `ZfsClient.get_child_datasets`
- **Tests:** `test_a_child_lookup_that_does_not_finish_cleanly_is_an_error`,
  `test_main_does_not_prune_when_the_child_lookup_does_not_finish`,
  `test_main_keeps_a_childs_snapshots_when_the_child_lookup_fails_having_listed_nothing`
- **Harness:** `dev/diff_dataset_cache.py` known class for a lookup that can't start, is killed or fails partway;
  `dev/sweep_faults.py` registers the "listed nothing" case as the one silent class

### DEV-019: A datasets.txt holding a NUL byte counts as no cache

- **Upstream:** uses it, and every command a child's name reaches is cut short at the NUL. Its snapshot listing
  drops the datasets sorted after that name, so take re-takes them and prune never sees them.
- **pysanoid:** treats it as no cache: the children come from zfs and the file is rewritten. Only damage puts a
  NUL there (a crash can leave NULs in a file just written).
- **Why:** snapshots re-taken without end, and datasets prune never sees. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Code:** `ZfsClient._read_dataset_cache`
- **Tests:** `test_a_dataset_cache_holding_a_nul_byte_counts_as_no_cache`
- **Harness:** `dev/diff_dataset_cache.py` known class, checked against pysanoid's own run with no datasets.txt

### DEV-020: A datasets.txt dated in the future counts as no cache

- **Upstream:** its age test passes a file written while the clock ran ahead until real time reaches the file's
  date.
- **pysanoid:** a negative age counts as no cache, as a snapshots.txt dated in the future counts as expired
  (DEV-037).
- **Why:** until real time reaches its date, no dataset made since is found under its recursive section, so it gets
  no snapshots. Dangerous for the data.
- **Code:** `ZfsClient._read_dataset_cache`
- **Tests:** `test_a_dataset_cache_dated_in_the_future_counts_as_no_cache`
- **Harness:** `dev/diff_dataset_cache.py`, classed and checked with the NUL case

### DEV-021: Each active dataset path is asked of zfs once

- **Upstream:** `sort { lc($a) cmp lc($b) } @paths` keeps a path twice when two sections resolve to it, so
  `zfs get` is asked for it twice.
- **pysanoid:** removes duplicate paths.
- **Why:** harmless, performance only. The snapshot table comes out the same either way.
- **Code:** `get_active_dataset_paths`
- **Tests:** none found by name.
- **Harness:** `dev/diff_snapshot_enumeration.py` canonicalizes the repeated names

## Command line

### DEV-022: argparse stands in for Getopt::Long

- **Upstream:** Getopt::Long's defaults also take single-dash long options (`-cron`), any case (`--Cron`),
  `+verbose`, `-?`, a lone `--`, `--v` / `--ver`, and a mandatory value that looks like an option
  (`--configdir --cron` reads `--cron` as the directory).
- **pysanoid:** each of those is a usage error: exit 2, nothing runs, the unit shows failed. (`--v` and `--ver`
  are ambiguous with `--version` here.) What units and cron lines use (`--name`, `--name=value`, `--name value`,
  unique prefixes such as `--take`) parses the same.
- **Why:** all are loud, none is a data-loss path on its own, and matching them would mean a hand-written parser on
  the production path, more risk than these spellings are worth. Decided 2026-09-21: keep argparse and document
  the difference.
- **Evidence:** upstream's own GetOptions call, run under real Perl (Getopt::Long 2.52), agreed with `parse_args`
  on 6,000 random command lines that avoid the spellings above.
- **Code:** `parse_args`
- **Tests:** `test_spellings_only_getopt_long_accepts_are_usage_errors`,
  `test_the_production_command_lines_parse_as_upstream_would`
- **Harness:** `dev/diff_options.py` (its generator excludes these spellings by construction)

### DEV-023: Arguments that are not options are refused

- **Upstream:** leaves them in `@ARGV` unread, so `sanoid readonly --prune-snapshots` (the dashes forgotten) prunes
  for real.
- **pysanoid:** a usage error: exit 2, nothing runs.
- **Why:** a forgotten `--readonly` turns a dry run into a real prune. Dangerous for the data.
- **Evidence:** confirmed against the real Getopt::Long.
- **Code:** `parse_args`
- **Tests:** `test_arguments_that_are_not_options_are_refused`
- **Harness:** `dev/diff_options.py` excludes this case by construction

### DEV-024: An empty --configdir, --cache-dir or --run-dir is refused

- **Upstream:** refuses the `--x=` form, but accepts `--x ''` and then reads or writes under `/`.
- **pysanoid:** refuses both forms. (`Path("")` is `.`, so caches, locks or the config would land wherever the run
  starts.)
- **Why:** an empty value is typically an unset variable in a unit file; caches and locks written in the wrong place
  stop protecting what they guard.
- **Code:** `_path_arg`
- **Tests:** `test_an_empty_directory_option_is_refused`

### DEV-025: A --cache-dir or --run-dir starting with whitespace is used as given

- **Upstream:** opens its files with Perl's two-argument `open()`, which drops leading whitespace, while its file
  tests, renames, locks and `make_path` keep it. So it exits 2 on every run: with the cache directory, before taking
  any snapshot; with the run directory, on releasing its first lock, so a `--cron` run that took a snapshot never
  prunes, and a prune run stops after its first batch.
- **pysanoid:** uses the path exactly as given, everywhere.
- **Why:** a run that stops before taking any snapshot, every time, is a silent loss of protection. Dangerous for
  the data.
- **Evidence:** confirmed against real Perl.
- **Code:** `main` (no special handling: `Path(args.cache_dir)`, `Path(args.run_dir)`)
- **Tests:** `test_a_directory_option_starting_with_whitespace_is_used_as_given`
- **Harness:** `dev/e2e_subprocess.py` `test_directories_starting_with_whitespace_are_used_as_given`

### DEV-026: The --monitor-* options are not implemented

- **Upstream:** `--monitor-snapshots`, `--monitor-health` and `--monitor-capacity` run its Nagios checks.
- **pysanoid:** each is a usage error. The Nagios checks are out of scope. (`pysanoid_alert.py`, outside the port,
  watches snapshot freshness instead.)
- **Why:** scope, not safety.
- **Code:** `parse_args`
- **Tests:** `test_spellings_only_getopt_long_accepts_are_usage_errors` (the `--monitor-*` cases)
- **Harness:** `dev/diff_options.py` classifies any `--monitor-*` argv as out of scope

### DEV-027: Cosmetic differences in messages and exit status

- **Upstream / pysanoid:** these differ, and none changes what is taken or destroyed:
  - the `--help` and `--version` text;
  - the ` at <script> line N.` Perl adds to some warnings and fatal errors;
  - the warnings Perl itself gives about upstream's code (`Use of uninitialized value ... at <script> line N.`);
  - messages that name the program pysanoid where upstream's say sanoid (a lock here is only ever held by another
    pysanoid process);
  - the exit status of a FATAL: 1, where `die` gives 255 or errno. Every failure is still non-zero;
  - the wait status in `zfs snapshot ... failed, N`, `could not remove ... : N` and a failed hook's
    `WARN: ... failed, N` is without the 128 a core dump adds, which Python's subprocess doesn't pass on;
  - a negative `--cache-ttl` is refused before the cache and run directories are made, where upstream makes them
    first.
- **Why:** harmless.
- **Code:** `parse_args`, `main`, `_wait_status`
- **Tests:** `test_the_force_prune_warning_is_upstreams` (no ` at <script> line N.`),
  `test_a_negative_cache_ttl_is_an_error_message_not_a_traceback`
- **Harness:** `dev/e2e_subprocess.py` `test_negative_cache_ttl_is_a_clean_fatal`

### DEV-052: The config, cache and lock directories are pysanoid's own

- **Upstream:** `--configdir`, `--cache-dir` and `--run-dir` default to `/etc/sanoid`, `/var/cache/sanoid` and
  `/var/run/sanoid`.
- **pysanoid:** they default to `/etc/pysanoid`, `/var/cache/pysanoid` and `/var/run/pysanoid`. `pysanoid_alert.py`'s
  freshness check reads `/etc/pysanoid` too, and both systemd units check `/etc/pysanoid/sanoid.conf`. The files and
  their format are unchanged: an existing `sanoid.conf` copied from `/etc/sanoid` loads as it did. A first run with
  no cache in `/var/cache/pysanoid` reads every snapshot from zfs, as any run without a cache does (DEV-039c).
- **Why:** harmless. A port installed beside Perl sanoid never reads or overwrites sanoid's configuration, caches or
  lock files.
- **Code:** `parse_args`
- **Tests:** `test_pysanoid_uses_its_own_directories_by_default`
- **Harness:** `dev/diff_options.py` reads the three defaults as this entry, not as differences

### P12: The command line reads as upstream's GetOptions call reads it

- **Upstream:** Getopt::Long reads `--cache-ttl` as `=i`: an optional sign, then digits with underscores anywhere
  (`1_000`, `_5`, `5_`) and a final newline allowed. Given no option but the directory ones (`keys %args < 4`), a run
  is `--cron --verbose`. `--force-prune` is accepted with a warning and changes nothing, and a negative `--cache-ttl`
  is refused in its own words.
- **pysanoid:** the same, word for word. `int()` would read padding and other scripts' digits upstream refuses, and
  refuse the underscore forms it takes, so `--cache-ttl` has a reader of its own (`_getopt_int`).
- **Why noted:** the default run and the `--cache-ttl` grammar look like choices of pysanoid's, and are upstream's.
- **Evidence:** upstream's own GetOptions call, run under real Perl (DEV-022).
- **Code:** `_getopt_int`, `parse_args`
- **Tests:** `test_cache_ttl_accepts_what_getopt_long_accepts`, `test_cache_ttl_refuses_what_getopt_long_refuses`,
  `test_main_runs_the_same_steps_as_upstream_for_each_flag_combination`, `test_the_force_prune_warning_is_upstreams`,
  `test_a_negative_cache_ttl_is_an_error_message_not_a_traceback`
- **Harness:** `dev/diff_options.py`, `dev/diff_flow.py`

## Taking snapshots

### DEV-028: A bad value costs only its (section, type), and a failed take exits non-zero

- **Upstream:** plans in one loop before any snapshot is taken and dies on the first bad value, for EVERY dataset at
  once. That suits an error that shows on the next run after a config edit, but not a latent one: `monthly_mday = 31`
  looks valid and fails only in 30-day months, so it can stop every dataset's snapshots for up to a month (likewise
  `hourly_min = 75`, `daily_hour = 24`, `frequent_period = 0` or blank). It only warns, and exits 0, when a snapshot
  can't be created or a failed `pre_snapshot_script` holds one back (`no_inconsistent_snapshot`). The warning reaches
  the journal at `info` priority, which `journalctl -p err` doesn't show.
- **pysanoid:** a bad schedule or retention value costs only its (section, type): an ERROR line, and the rest of the
  run goes on. A failed `zfs snapshot`, or one a failed pre hook held back, is reported too. Any of these makes the
  run exit non-zero once everything else, pruning included, has run. Load-time errors (an unknown key, a malformed
  ini) still stop the run, as upstream's do.
- **Why:** a pool that can't take snapshots (out of space, dataset gone), or a hook that always fails, looked like a
  healthy unit: a silent failure to protect. Dangerous for the data. Safe against a full pool: the units use
  `Wants=`, not `Requires=`, so pruning still runs after a failed take.
- **Evidence:** confirmed against real Perl.
- **Upstream reports:** #671 (exit 0 after a failed snapshot); #394 and #440 (the same die for every dataset, from
  upstream's own date arithmetic on the 1st of the month).
- **Code:** `plan_snapshots`, `take_snapshots`, `main`
- **Tests:** `test_plan_snapshots_isolates_a_bad_section_from_good_ones`,
  `test_plan_snapshots_isolates_a_bad_type_within_a_section`,
  `test_plan_snapshots_monthly_mday_31_in_a_30_day_month_only_loses_the_monthly`,
  `test_plan_snapshots_out_of_range_hourly_min_only_loses_the_hourly`, `test_take_snapshots_reports_failed_snapshot_creation`,
  `test_take_snapshots_reports_a_snapshot_held_back_by_a_failed_pre_hook`,
  `test_main_exits_nonzero_when_taking_a_snapshot_fails`, `test_main_cron_still_prunes_after_take_problems_but_exits_nonzero`
- **Harness:** `dev/diff_take_snapshots.py` canonicalizes the exit status; `dev/diff_whole_run.py` known classes for the
  exit status and for `monthly_mday = 31` in June, where upstream dies

### P1: Malformed counts and preferred times are read as upstream reads them, and reported

- **Upstream:** numifies a malformed or blank retention count or preferred-time setting: `daily = 3o` is 3,
  `hourly_min = 1_5` is 1, `daily_hour =` is 0.
- **pysanoid:** take reads each the same way (`_perl_number`), so the same snapshots are taken, and reports it like a
  bad value (exit non-zero). Prune still refuses such a count (DEV-008).
- **Differences left, harmless:** a preferred time drops any fraction (`hourly_min = 7.5` is minute 7, where
  upstream's time is 7:30), because a setting read as a time is a whole number here. A blank day or month
  (`monthly_mday =`) reads as 0 and costs only that type; upstream dies on it for every dataset (DEV-028).
- **Evidence:** confirmed against real Perl; `_perl_number` against Perl's own numification in
  `dev/diff_perl_number.py`.
- **Code:** `_take_count`, `_schedule_setting`, `_perl_number`
- **Tests:** `test_plan_snapshots_takes_a_malformed_count_upstream_reads_as_a_positive_number`,
  `test_plan_snapshots_reads_an_underscored_schedule_value_as_upstream_does`,
  `test_last_preferred_time_reads_a_malformed_setting_as_upstream_does`,
  `test_last_preferred_time_reads_a_blank_setting_as_upstream_does`, `test_plan_snapshots_a_blank_day_or_month_costs_only_its_type`
- **Harness:** `dev/diff_take_snapshots.py` hand cases for malformed numbers, and known cases for a fractional
  `hourly_min` and a blank `monthly_mday`

### DEV-029: The dst_ retry covers every type on any "already exists" collision

- **Upstream:** retries a snapshot under a `dst_` name only for hourlies on a day it detected a fall-back
  (`$handleDst`), and only when hourly is the group's first due type. After the retry it forces the re-read of
  DEV-013, listing zfs again whether or not the cache has expired.
- **pysanoid:** any type, on any "already exists" collision, retries with the `dst_` name. Its snapshot is added to
  the cache like any other.
- **Why:** a disambiguated name beats a silently missed snapshot.
- **Code:** `take_snapshots`
- **Tests:** `test_take_snapshots_dst_retry_success_is_not_a_problem`, `test_take_snapshots_dst_retry_failure_is_a_problem`,
  `test_take_snapshots_dst_retry_that_cannot_start_is_a_problem`, `test_a_dst_retry_keeps_the_time_of_its_datasets_turn`
- **Harness:** `dev/diff_take_snapshots.py` known case for a daily's collision; `dev/simulate_long_horizon.py` counts
  it as a known take difference

### DEV-030: Preferred times are computed with zoneinfo

- **Upstream:** builds a time with `timelocal`, reaches earlier ones by subtracting flat seconds, and corrects for DST
  by an hour keyed off the system's DST flag.
- **pysanoid:** hourly and frequently scan back through real minutes; the longer types are built from local date and
  time fields with the zone attached, so zoneinfo picks the right UTC offset for that date.
- **Why:** upstream's arithmetic is dangerous for the data after the autumn change: in zones such as
  America/New_York and Australia/Sydney it takes no frequent snapshots for the whole repeated hour (0 of 4 runs, here
  4 of 4).
- **Other differences, harmless:** the same arithmetic moves other times near the changes. A daily an hour early all
  day on a spring-forward day (22:59 for 23:59). A weekly an hour early or late for about two days around each
  change. On an autumn-change day, upstream's hourly is in the future while the minute is below `hourly_min`, so
  each such run takes one. In Europe/London and Europe/Dublin, upstream's frequently is in the future during the
  repeated hour's first pass, so every run then takes one (as normal with a 15-minute timer). Monthly and yearly
  step back a flat 31 days (or 60*60*24*31*365.25 seconds, about 31 years) while the configured day is still
  ahead; never with the defaults (`monthly_mday` 1, yearly 1 January).
- **Evidence:** confirmed against real Perl.
- **Upstream reports:** its DST handling was reported and rewritten several times (#155, #395, #406); #409 (open)
  proposes replacing it, and its maintainers recommend running in UTC.
- **Code:** `_last_preferred_time`, `_latest_wall_minute`, `_after`
- **Tests:** `test_dst_transition_daily_time_has_correct_utc_offset`,
  `test_hourly_preferred_time_steps_back_one_real_hour_across_dst`,
  `test_hourly_and_frequent_times_are_exact_through_a_half_hour_clock_change`,
  `test_plan_snapshots_no_spurious_take_during_fold_second_pass`
- **Harness:** `dev/sweep_dst.py` checks `_last_preferred_time` against a brute-force answer every 10 minutes around
  each change, in six zones; `dev/simulate_long_horizon.py` counts upstream's DST arithmetic as a known difference

### P2: $TZ and /etc/localtime are read as glibc reads them

- **Upstream:** Perl's `localtime`, which is glibc's reading of `$TZ` and `/etc/localtime`.
- **pysanoid:** the same order: with `$TZ` set, UTC when it is empty or `:` alone; otherwise, a leading `:` dropped,
  a zone file (a name under `$TZDIR`, else `/usr/share/zoneinfo`, or a path), then a POSIX rule
  (`IST-1GMT0,M10.5.0,M3.5.0/1`), else UTC. Without `$TZ`, `/etc/localtime`, a link or a copy, else UTC.
- **Differences left, harmless** (they move when snapshots are taken, not which are kept):
  - pysanoid warns when a `$TZ` or `/etc/localtime` it can't read leaves it on UTC; glibc is silent;
  - a rule's zero-based day number (`XST5XDT,60,300`) is read a day early, and `J59` a day late in a leap year;
  - malformed rules zoneinfo and glibc read differently: a DST name with no dates, an hour past 24, minutes past 59,
    a name under 3 letters;
  - an offset of a day or more, which glibc applies and `datetime` can't hold: UTC, warned;
  - a zone file cut short in its closing rule line, which glibc reads in part: no zone here.
- **Evidence:** nine rules matched the C library at every instant checked over two years, on Python 3.14 and 3.11
  (`AUDIT.md`, 2026-09-27); confirmed against real Perl for a `$TZ` naming no zone file.
- **Code:** `get_local_zoneinfo`, `_tz_zone`, `_zone_file`, `_posix_rule_zone`
- **Tests:** `test_tz_names_a_zone_as_the_c_library_reads_it`, `test_a_tz_set_but_empty_is_utc_as_the_c_library_reads_it`,
  `test_a_posix_rule_in_tz_is_applied_as_the_c_library_applies_it`, `test_a_tz_naming_no_zone_or_rule_is_utc_with_a_warning`,
  `test_a_tz_naming_a_file_holding_no_zone_is_utc_with_a_warning`, `test_a_zone_name_is_looked_up_under_tzdir_as_glibc_does`,
  `test_without_tz_etc_localtime_is_read_itself`, `test_an_etc_localtime_holding_no_zone_is_utc_with_a_warning`

### DEV-031: weekly_wday is read modulo 7

- **Upstream:** steps back a single week from the configured day. From 7 up its time can be in the future, so it
  takes a weekly on every run (all Sunday for 7, every run for 13), each kept for the whole weekly window. From -15
  down its time is a week or more early, so it takes one only every second week or less often.
- **pysanoid:** reads the value modulo 7, so 7 is Sunday again. 0 to 6 and -1 to -7 match upstream.
- **Why:** a weekly on every run can fill the pool, and one every second week loses restore points. Both dangerous
  for the data.
- **Difference left, harmless:** from -8 to -14 upstream still takes one a week, but at the start of Sunday rather
  than the chosen day and time. Different only because one reading covers every value. Decided 2026-09-24.
- **Evidence:** confirmed against real Perl for every value from -22 to 21.
- **Code:** `_last_preferred_time`
- **Tests:** `test_weekly_wday_is_read_modulo_7_and_never_lands_in_the_future`
- **Harness:** `dev/diff_take_snapshots.py` known case for `weekly_wday = 7` on a Sunday; `dev/diff_whole_run.py` known
  class, `weekly_wday = 7` run twice on a Sunday

### DEV-032: frequent_period has a policy of its own

- **Upstream:** take dies on a blank `frequent_period`, or a comment alone, for every dataset (division by zero), and
  its prune reads it as 0. `0` dies too. `1.5`, `1_5` and `abc` read as 1.5, 1 and 0.
- **pysanoid:**
  - blank, or a comment alone (`frequent_period = # x`), reads as the default 15 in take and prune;
  - `0` costs only the section's frequent snapshots, with an ERROR; prune reads 0 as upstream does;
  - a negative value slices like its size, as upstream's arithmetic makes it;
  - decimals, `1_5`, other scripts' digits and text are refused in take and prune. It is not read upstream's way
    and reported, as P1's settings are.
- **Why:** upstream's die stops every dataset's snapshots (dangerous for the data). A mistyped period read
  upstream's way (`1_5` as 1) takes a frequent snapshot every minute and changes how long prune keeps them,
  destroying ones that should have stayed.
- **Evidence:** confirmed against real Perl.
- **Code:** `_last_preferred_time`, `_cfg_int`
- **Tests:** `test_plan_snapshots_frequent_period_zero_is_a_clear_error`,
  `test_last_preferred_time_still_reads_a_blank_frequent_period_as_15`,
  `test_last_preferred_time_still_refuses_a_malformed_frequent_period`, `test_negative_frequent_period_slices_like_its_positive_size`,
  `test_prune_refuses_an_underscored_frequent_period`
- **Harness:** `dev/diff_whole_run.py` known class, a comment alone under `--prune-snapshots`

### P3: Pre-hook and post-hook SANOID_SNAPNAME differ, as upstream's do

- **Upstream:** both hooks read `$snapshots[0]`, but the loop that takes the snapshots in between,
  `foreach my $snap (@snapshots) { $snap = "$dataset\@$snap"; ...}`, aliases and rewrites that array, and its dst_
  retry renames the element before retrying. So `pre_snapshot_script` gets the bare `name`, and `post_snapshot_script`
  gets `path@name`, with the `dst_` rename when the first snapshot needed it (even a retry that failed).
- **pysanoid:** the same. The retry here covers more cases (DEV-029), and so does the rename. `SANOID_SNAPNAMES` is
  unaffected on both sides: upstream joins it into a string before that loop.
- **Why noted:** it looks like a typo and isn't. An existing `post_snapshot_script` may depend on it.
- **Evidence:** confirmed against real Perl.
- **Upstream reports:** #798, whose README still describes the bare name.
- **Code:** `take_snapshots` (the post-hook call)
- **Tests:** `test_take_snapshots_post_hook_snapname_is_prefixed_but_pre_hook_snapname_is_not`,
  `test_take_snapshots_post_hook_snapname_carries_the_first_snapshots_dst_rename`,
  `test_take_snapshots_post_hook_snapname_is_renamed_even_when_the_dst_retry_fails`,
  `test_take_snapshots_post_hook_snapname_ignores_a_later_snapshots_dst_rename`
- **Harness:** `dev/diff_take_snapshots.py` hand cases on a real fall-back day

### DEV-051: A snapshot dated more than five minutes ahead of the clock is set aside

- **Upstream:** finds a type due only when its newest snapshot is older than the type's period, and keeps at least
  the configured count of each type, counting every snapshot. A snapshot dated ahead of the clock, made while the
  clock ran ahead or seen while it runs behind, is never that old: the type is taken again only once real time passes
  it, and it holds one of prune's places for as long, saying nothing.
- **pysanoid:** a run sets aside every snapshot dated more than five minutes ahead of its clock before take and prune
  build their tables. Take finds the type due by the snapshots that remain; prune neither counts nor destroys one set
  aside, which stays on disk until the clock passes it. Within five minutes, as after a clock stepped back a little,
  a snapshot counts, as upstream's does. `--debug` still prints every snapshot, as upstream's dump does. No new
  output or exit status.
- **Why:** once a clock that ran ahead is set right, upstream takes nothing of the type for as long as the clock was
  ahead, and each snapshot dated ahead fills a place the count keeps for real snapshots, so prune destroys one it
  would keep: a year after, `daily = 7` keeps 6 real dailies, `monthly = 12` 11 real monthlies, and `frequently = 4`
  after four hours ahead none at all. A clock that runs behind (a dead clock battery, a host that boots before its
  time is set) dates every real snapshot ahead of it, and upstream takes nothing while it stays behind. Dangerous for
  the data. Nothing can be done while the clock is wrong: a run then can't tell, and every real snapshot looks old.
- **Evidence:** confirmed against upstream's code and pysanoid's prune, which `dev/diff_prune.py` compares with
  upstream's; `dev/simulate_trouble.py`'s scripted pass, judged by this rule, finds 11 destroys below the count and
  110 missed snapshots on 0.1.52.
- **Upstream reports:** #1119 (open: a host booted with its clock in 2153, ntpd set it right four hours later, and
  sanoid took nothing on 18 datasets for four days while it went on pruning); #651 (closed unmerged).
- **Code:** `without_snapshots_dated_ahead`, `main`
- **Tests:** `test_main_takes_the_daily_when_its_newest_is_dated_more_than_five_minutes_ahead`,
  `test_prune_neither_counts_nor_destroys_a_snapshot_dated_more_than_five_minutes_ahead`,
  `test_with_the_clock_behind_the_snapshots_the_daily_is_taken_and_none_destroyed`,
  `test_a_newest_snapshot_dated_five_minutes_ahead_or_less_still_holds_its_type_back`; and, for `plan_snapshots`
  given a table, as upstream's: `test_a_type_whose_newest_snapshot_is_dated_in_the_future_is_not_taken_and_nothing_is_said`
- **Harness:** `dev/e2e_subprocess.py` `test_a_snapshot_dated_ahead_of_the_clock_is_set_aside`; `dev/simulate_trouble.py`
  and the model it judges by (`dev/lib/model.py`); `dev/diff_take_snapshots.py` still compares upstream's take given
  such a table

### P16: A newest snapshot dated in the future holds its type back, silently

Retired in 0.1.53: replaced by DEV-051. Until then pysanoid did as upstream does, decided as parity on 2026-09-27
and again on 2026-10-10, on the grounds that holding a type back destroys nothing.

### P17: A dataset's snapshot names carry the time its turn came

- **Upstream:** reads the clock for the names as it reaches each dataset in its loop (`get_date`), before that
  dataset's `pre_snapshot_script`: one time for all of a dataset's types, and a later one for a later dataset.
- **pysanoid:** the same. A caller that passes `now` gets a clock that stands still: the plan is made by it, and
  every name carries it.
- **Why noted:** naming every snapshot of a run by the time the run began looks simpler, and differs from upstream
  by however long the earlier datasets' commands and hooks took.
- **Evidence:** confirmed against real Perl.
- **Code:** `take_snapshots`
- **Tests:** `test_each_datasets_snapshot_names_carry_the_time_its_turn_came`,
  `test_main_names_each_datasets_snapshots_by_the_time_its_turn_came`,
  `test_a_readonly_run_names_what_it_would_take_by_the_same_clock`
- **Harness:** `dev/diff_take_snapshots.py` (a clock that moves for every command and hook)

## Snapshot cache

### DEV-033: A snapshot's name ends at the tab before "creation"

- **Upstream:** matches each listing line with `m/(.*)\@(.*ly)\t*creation\t*(\d*)/`, which allows no tab before
  "creation". A legal name such as `autosnap_dailycreation9999999999_x` reads as a daily named `autosnap_daily`, made
  at the time its own digits give (here 2286).
- **pysanoid:** requires at least one tab before "creation", as `zfs get -H` always prints and no snapshot name can
  contain. Otherwise the pattern is upstream's: unanchored, `.*` for the dataset, ASCII only.
- **Why:** while such a snapshot exists, that dataset's dailies stop and prune keeps one fewer. Anyone allowed to
  create a snapshot there can do this, even with only the delegated `snapshot` permission. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Code:** `_SNAP_TAIL_RE`, `_snapshot_line`
- **Tests:** `test_a_snapshot_name_holding_a_type_and_creation_is_not_read_as_that_type`,
  `test_a_snapshot_named_like_a_future_daily_does_not_stop_the_dailies`
- **Harness:** `dev/diff_snapshot_enumeration.py` known class "a snapshot name holding a type and 'creation'"

### DEV-034: A `zfs get` killed by a signal is retried, like a failed one

- **Upstream:** tests the listing's exit with `$? >> 8`, which reads a signal as success, and keeps the partial
  listing.
- **pysanoid:** a `zfs get` killed by a signal is retried without dataset names, as a failed one is.
- **Why:** each time the cache is refreshed while the kill recurs, take re-takes every due type of the datasets it
  missed, yearlies included, and prune never sees them. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Code:** `_list_snapshots`
- **Tests:** `test_get_snapshots_retries_when_zfs_is_killed_by_a_signal`
- **Harness:** `dev/diff_snapshot_enumeration.py` known class "a zfs get killed by a signal mid-listing"

### P4: When the retry fails too, the cache is written from what came back

- **Upstream:** when both `zfs get` calls fail, rewrites the cache from whatever the retry printed: usually nothing,
  so an empty cache, and a fresh one.
- **pysanoid:** the same.
- **Why noted:** it looks like a gap and was kept at parity by decision (2026-09-21). Until the cache expires, take
  sees no snapshots and treats every configured type as overdue (extra snapshots), and prune sees none and destroys
  nothing. The only loss path is those extras later inflating prune's "remaining" count, and only for a series that
  is already sparse (survivors older than count x period, held by the keep-at-least-N floor alone). Failing loudly
  instead would stop take for the whole outage, which is worse.
- **Code:** `_list_snapshots`
- **Tests:** `test_get_snapshots_writes_an_empty_cache_when_zfs_fails_outright`

### DEV-035: A snapshot listing that doesn't end in a newline loses its last line

- **Upstream:** uses a listing as it came. When zfs is killed mid-line, a creation time cut short reads as decades
  old.
- **pysanoid:** drops the last line of a listing that doesn't end in a newline: zfs ends every line it prints, so
  that one was cut short. The lines before it are used, as upstream uses them.
- **Why:** upstream's prune destroys that snapshot, the dataset's newest, in place of an old one: a `daily = 3`
  dataset lost yesterday's daily and kept one 4 days old. Dangerous for the data. Seeing fewer snapshots than there
  are, prune destroys none it wouldn't have.
- **Evidence:** confirmed against real Perl.
- **Code:** `_list_snapshots`
- **Tests:** `test_a_listing_cut_short_inside_its_last_line_goes_without_that_line`,
  `test_a_listing_cut_short_is_read_from_zfs_while_the_lock_is_held_and_no_cache_written`,
  `test_main_keeps_the_newest_daily_when_the_snapshot_listing_is_cut_short`
- **Harness:** `dev/diff_snapshot_enumeration.py` known classes "the retry killed ..." (checked against pysanoid's
  own run with the listing ending at the line before); `dev/e2e_subprocess.py`
  `test_a_snapshot_listing_killed_inside_its_last_line_goes_without_that_line`

### DEV-036: A snapshots.txt whose last line has no newline counts as no cache

- **Upstream:** reads it, and a creation time cut short reads as decades old. Its cache add appends straight onto
  that last line, fusing it with the first new one.
- **pysanoid:** counts it as no cache, so zfs is listed: every cache written ends its last line, so one that
  doesn't was cut short. The cache add leaves such a file as it is, and the next run lists zfs.
- **Why:** upstream's prune destroys that snapshot, the dataset's newest, in place of an old one: a `daily = 3`
  dataset lost yesterday's daily and kept one 4 days old. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Code:** `_read_cache`, `add_cached_snapshots`
- **Tests:** `test_a_snapshot_cache_cut_short_inside_its_last_line_counts_as_no_cache`,
  `test_main_prunes_by_zfs_when_the_snapshot_cache_was_cut_short`,
  `test_get_snapshots_reads_zfs_when_the_lock_is_held_and_the_cache_was_cut_short`,
  `test_add_cached_snapshots_leaves_a_cache_cut_short_as_it_is`
- **Harness:** `dev/diff_snapshot_enumeration.py` known classes "a cache cut short ..." (checked against pysanoid's
  own run with no cache); `dev/diff_snapshot_cache_mutation.py` known class `NO_FINAL_NEWLINE`;
  `dev/e2e_subprocess.py` `test_a_snapshot_cache_cut_short_is_no_cache`

### DEV-037: A snapshots.txt dated in the future counts as expired

- **Upstream:** its age test, `time() - $mtime` against the TTL, passes a cache written while the clock ran ahead
  until real time reaches its date, so that cache is never refreshed.
- **pysanoid:** a negative age counts as expired, as a datasets.txt dated in the future counts as no cache
  (DEV-020).
- **Why:** snapshots destroyed outside sanoid since, by a rollback for one, stay listed and count toward a type's
  keep-at-least number: a `daily = 3` dataset was left with 1 daily. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Code:** `get_snapshots`
- **Tests:** `test_a_snapshot_cache_dated_in_the_future_counts_as_expired`,
  `test_main_prunes_by_zfs_when_the_snapshot_cache_is_dated_in_the_future`
- **Harness:** `dev/diff_snapshot_enumeration.py` known classes "a cache dated ..." (checked against pysanoid's own
  run with the same cache expired); `dev/e2e_subprocess.py` `test_a_snapshot_cache_dated_in_the_future_is_read_afresh`

### DEV-038: A snapshots.txt holding a NUL byte counts as no cache

- **Upstream:** reads it, and prunes a damaged line under the name before the NUL, where the C string ends.
- **pysanoid:** counts it as no cache, as a datasets.txt holding one does (DEV-019). Only damage puts a NUL there.
- **Why:** upstream destroyed a dataset's newest daily, leaving 2 of the 3 its retention keeps. Dangerous for the
  data.
- **Evidence:** confirmed against real Perl.
- **Code:** `_read_cache`
- **Tests:** `test_a_snapshot_cache_holding_a_nul_byte_counts_as_no_cache`,
  `test_main_prunes_by_zfs_when_the_snapshot_cache_holds_a_nul_byte`
- **Harness:** `dev/diff_snapshot_enumeration.py` known classes "a fresh cache holding a NUL byte ..." (checked
  against pysanoid's own run with no cache); `dev/e2e_subprocess.py` `test_a_snapshot_cache_holding_a_nul_byte_is_no_cache`

### DEV-039: A cache is bookkeeping and never stops a run

A cache only saves zfs calls. Each case below is one way upstream lets a cache stop a run, or mislead one; pysanoid
reports what went wrong (a non-zero exit), reads zfs instead, and carries on.

#### DEV-039a: A cache that can't be read, or isn't a regular file, counts as expired

- **Upstream:** a fresh snapshots.txt it can't read, and a missing one while another process holds the update lock,
  both reach an unchecked `open FH, "< $cache"` and read as no snapshots. Take then finds every type with a
  non-zero count overdue and takes one of each for every dataset, and its cache add dies copying the unreadable
  file (`copy($cache, "$cache.tmp") or die`), so a `--cron` run never reaches its prune. A snapshots.txt that isn't a
  regular file is expired by `! -f $cache`; when it is a directory, renaming the new cache onto it dies.
- **pysanoid:** a cache that can't be read, or isn't a regular file, counts as expired, so zfs is read, even while
  another process holds the update lock. A directory in its place costs the cache update (DEV-039b), not the run.
  Reading a named pipe would hang the run, so it is never opened.
- **Why:** an extra snapshot of every type of every dataset, yearlies included, and a run that stops before its
  prune. Dangerous for the data.
- **Evidence:** confirmed against real Perl (upstream took another daily of every dataset, then died); the read,
  take and add paths read in upstream's `getsnaps`, `take_snapshots` and `addcachedsnapshots`.
- **Code:** `get_snapshots`, `_read_cache`
- **Tests:** `test_get_snapshots_reads_zfs_when_a_fresh_cache_cannot_be_read`, `test_get_snapshots_treats_a_directory_as_no_cache`,
  `test_get_snapshots_reads_zfs_when_the_lock_is_held_and_there_is_no_cache`,
  `test_main_never_asks_pathlib_whether_a_file_is_there`
- **Harness:** `dev/diff_snapshot_enumeration.py` known classes "a fresh cache that can't be read", "snapshots.txt
  is a directory" and "the lock is held and there is no cache"; `dev/e2e_subprocess.py`
  `test_an_unsearchable_cache_dir_does_not_stop_the_run`

#### DEV-039b: A cache that can't be updated is reported and removed

- **Upstream:** dies when a cache file can't be created or renamed, so a full or read-only disk stops every snapshot
  and prune while it lasts. When a write fails after the file opened (a full disk), it installs the truncated file.
- **pysanoid:** reports the failure, removes the cache and its temp file so the next run reads zfs, and carries on
  with what it holds in memory; the run exits non-zero. This covers every cache write: the listing, take's add,
  prune's remove and `datasets.txt`.
- **Why:** a disk problem that stops every snapshot and prune is a silent loss of protection, and a truncated cache
  misleads the next runs. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Upstream reports:** #1063 (take and prune started together, one dying on its rename nearly every run) and #534
  (a cache it couldn't write).
- **Code:** `_replace_cache`, `_drop_cache`, `get_snapshots`, `add_cached_snapshots`, `remove_cached_snapshots`,
  `ZfsClient.flush_dataset_cache`, `main` (the exit status)
- **Tests:** `test_main_takes_and_prunes_when_the_caches_cannot_be_written`,
  `test_get_snapshots_carries_on_when_the_cache_cannot_be_written`,
  `test_add_cached_snapshots_carries_on_when_the_cache_cannot_be_written`,
  `test_remove_cached_snapshots_carries_on_when_the_cache_cannot_be_written`,
  `test_flush_dataset_cache_carries_on_when_it_cannot_write`
- **Harness:** `dev/diff_snapshot_enumeration.py` known class "a cache directory that can't be written";
  `dev/e2e_subprocess.py` `test_take_and_prune_started_together_both_finish`

#### DEV-039c: A missing cache is left missing

- **Upstream:**
  - prune's cache remove reads the missing file as no lines, writes an empty `snapshots.txt.tmp`, and gives it the
    missing file's times: `stat` fails, so `utime(undef, undef)` sets the current time. The empty, fresh cache
    hides every snapshot until it expires, from the rest of that run (its re-read after the flush) and from every
    run in the TTL.
  - take's cache add dies (`copy($cache, "$cache.tmp") or die`), so a `--cron` run never reaches its prune.
- **pysanoid:** the cache add and the cache remove leave a missing cache missing, so the next run lists zfs. The add
  doesn't write a cache of only the new lines instead: with a fresh mtime it would, for a whole TTL, make every run
  re-take every type it doesn't hold.
- **Why:**
  - the remove: while the empty cache lasts, take takes another snapshot of every type of every dataset and prune
    destroys nothing. That is the narrow loss path P4 describes (the extras later inflating prune's count for a
    sparse series). P4 keeps it because failing instead would stop take for a whole outage; here leaving the cache
    missing costs one zfs listing.
  - the add: a run that stops before its prune, each time the cache is missing when a snapshot was taken.
    Dangerous for the data.
- **Evidence:** confirmed against real Perl; the remove's `stat`/`utime` path read in upstream's
  `removecachedsnapshots`.
- **Code:** `add_cached_snapshots`, `remove_cached_snapshots`
- **Tests:** `test_add_cached_snapshots_leaves_a_missing_cache_missing`, `test_remove_cached_snapshots_leaves_a_missing_cache_missing`
- **Harness:** `dev/diff_snapshot_cache_mutation.py` known classes `NO_CACHE_REMOVE` and `NO_CACHE_ADD`

#### DEV-039d: A cache or run directory that can't be made doesn't stop the run at startup

- **Upstream:** its `make_path` of the cache and run directories dies, so nothing is taken or pruned.
- **pysanoid:** a directory that can't be made costs only the cache updates and locks that need it, each reported
  where it happens (DEV-039b, DEV-041).
- **Why:** a run that stops before taking any snapshot, every time, is a silent loss of protection. Dangerous for the
  data.
- **Evidence:** confirmed against real Perl.
- **Code:** `main`
- **Tests:** `test_main_keeps_taking_snapshots_when_a_directory_is_unusable`

#### DEV-039e: Children the cache add can't look up are left out and reported

- **Upstream:** for a `recursive = zfs` snapshot, the cache add looks up the children through an unchecked pipe
  (`getchilddatasets`, when datasets.txt doesn't answer). One that can't start finds no children: the parent's line
  is cached and the run carries on. One killed partway keeps the lines it printed, less the first (taken to be the
  parent), and its last line may be a name cut short.
- **pysanoid:** a lookup that can't start, is killed or fails partway leaves the children out of the cache, and is
  reported. It is the same lookup the config load uses, which refuses a killed or failed-partway answer (DEV-018);
  here that refusal is caught, so none of a killed lookup's lines are kept. One that fails having listed nothing
  reads as no children, unreported, as at load.
- **Why the children are left out, not the run stopped:** harmless. The children's snapshots exist; the cache lacks
  them until it is next listed from zfs, and seeing fewer snapshots, prune destroys none it wouldn't have. Not raising keeps the run going, as upstream's
  does: raising ended it before a `--cron` prune and left the cache without the snapshots just taken, so the next
  run took the whole tree again. The report makes the gap show as a failed unit.
- **Why none of a killed lookup's lines:** a name cut short is a prefix of a child's. Any dataset it names inside
  the tree really has that snapshot, taken recursively, and a name that is no dataset holds nothing to prune. But a
  dataset whose whole name is a prefix of the parent's (a pool `tan` beside `tank`), with zfs killed within those
  first bytes of a line (where its stdout buffer was cut), gets a cache line for a snapshot it lacks, dated now.
  Until the cache is next listed from zfs, take skips that type there, and prune counts the line toward its
  keep-at-least number: `tan` with `daily = 3` was left with 2 real dailies. Dangerous for the data, if narrowly.
- **Evidence:** confirmed against real Perl: the lookup that can't start end to end under `--cron`; the killed
  lookup by `dev/diff_snapshot_cache_mutation.py`'s `LOOKUP_KILLED` class, against upstream's own subroutine; and a
  lookup cut three bytes into its second line (`tank\ntan`), run through upstream's `addcachedsnapshots`, which
  cached `tan@autosnap_c_daily` (`dev/diff_snapshot_cache_mutation.py` hand case "the child lookup is cut inside its
  second line", which requires upstream to cache that line; the effect on `tan` read as both programs read a cache).
- **Code:** `add_cached_snapshots`
- **Tests:** `test_add_cached_snapshots_carries_on_when_a_child_lookup_cannot_start`,
  `test_add_cached_snapshots_leaves_out_the_children_of_a_killed_lookup`,
  `test_main_cron_still_prunes_when_the_cache_add_cannot_list_children`
- **Harness:** `dev/diff_snapshot_cache_mutation.py` known classes `LOOKUP_CANNOT_START` and `LOOKUP_KILLED`

### P21: The cache remove writes the lines it keeps back as read

- **Upstream:** reads each cache line with its newline still on and takes its first tab-separated column
  (`split("\t", $snapline)`) as the snapshot's name, so a line without a tab, newline and all, never matches a
  pruned name, and every line it keeps goes back as it was.
- **pysanoid:** the same: a kept line, a malformed one included, is written back byte for byte, and the file keeps
  its mtime.
- **Why noted:** tidying the cache as it is rewritten looks harmless, and would part from what upstream's next run
  reads.
- **Evidence:** confirmed against real Perl.
- **Code:** `remove_cached_snapshots`
- **Tests:** `test_remove_cached_snapshots_writes_kept_lines_back_as_read`,
  `test_remove_cached_snapshots_keeps_junk_lines_and_preserves_the_mtime`
- **Harness:** `dev/diff_snapshot_cache_mutation.py`

### P15: A snapshot line is read with upstream's unanchored patterns

- **Upstream:** reads each listing or cache line with `m/(.*)\@(.*ly)\t*creation\t*(\d*)/`, and a snapshot's type
  with `m/.*_(\w*ly)/`, on bytes. So an empty dataset name parses; the type is the longest `\w*ly` after the last `_`,
  whatever follows it; `\d` and `\w` are ASCII only; a name with no type is still kept, under an undefined type; a
  line with no creation time is kept with time 0, so it looks decades old; and a creation time too long for an
  integer is a float, infinity past a float's range.
- **pysanoid:** the same in each case, but for DEV-033 (`\t+` before "creation"). Neither pattern runs as one Python
  regex, which takes quadratic time on some lines it fails on (a damaged cache's), where Perl checks a line for
  "creation" first: `_snapshot_line` and `_snapshot_type` give the patterns' answers in linear time. A snapshot with
  no type is kept under the type "", which no section configures, so nothing acts on it.
- **Why noted:** each of these looks like a bug to fix, and fixing it would part from what upstream keeps and prunes.
- **Evidence:** confirmed against real Perl; the readers fuzzed against the regexes themselves.
- **Code:** `_snapshot_line`, `_snapshot_type`, `_parse_snapshot_lines`, `_perl_digits`
- **Tests:** `test_snapshot_line_parsing_matches_upstreams_unanchored_regexes`, `test_a_snapshot_line_is_read_as_upstreams_regex_reads_it`,
  `test_snapshot_lines_are_read_as_upstreams_regex_reads_them_fuzzed`, `test_a_snapshot_type_is_read_as_upstreams_regex_reads_it`,
  `test_a_long_snapshot_line_built_to_backtrack_is_read_at_once`, `test_snapshot_lines_read_digits_and_word_characters_as_ascii_only`,
  `test_an_oversized_creation_time_reads_as_perl_reads_it`,
  `test_main_carries_on_past_a_cache_line_with_an_oversized_creation_time`
- **Harness:** `dev/diff_snapshot_enumeration.py`

### P14: Ages are whole seconds

- **Upstream:** reads the clock with Perl's `time()`, whole seconds, for a cache's age, a snapshot's age and prune's
  cut-off.
- **pysanoid:** the same: whole seconds everywhere an age is measured.
- **Why noted:** Python's clock has a fraction. With it, take's decision changed when the clock ticked over a second
  before planning, prune destroyed a snapshot exactly on its age boundary a run early, and some `--debug` ages
  rounded differently.
- **Evidence:** confirmed against real Perl.
- **Code:** `ZfsClient._read_dataset_cache`, `_cache_age`, `snapshots_by_type`, `snapshots_by_path`, `prune_snapshots`
- **Tests:** `test_dataset_cache_age_is_measured_in_whole_seconds`, `test_get_snapshots_measures_the_cache_age_in_whole_seconds`,
  `test_snapshot_tables_measure_ages_in_whole_seconds`, `test_plan_snapshots_decides_at_the_whole_second_boundary_as_upstream`,
  `test_prune_measures_age_in_whole_seconds_like_upstreams_time`

## Locking

### DEV-040: fcntl.flock stands in for upstream's PID lockfile

- **Upstream:** `checklock`, `writelock` and `removelock`: a lock file holding a PID, checked against the process
  list. A run that has to wait polls every 10 s and says so each time.
- **pysanoid:** `fcntl.flock` on lock files of the same names (`sanoid_cacheupdate`, `sanoid_cachedatasetupdate`,
  `sanoid_pruning`). A run that has to wait blocks on the lock and says so once, in upstream's words. The holder's
  PID is written into the file for a person looking; a run that finds the lock held leaves it there.
- **Why:** a port mechanism. The two can't see each other's locks, so never run both implementations against the
  same run directory (during a migration, for one).
- **Upstream reports:** #1063, #790 and #391 (runs racing on, or leaving behind, a lock file).
- **Code:** `try_lock`, `wait_lock`
- **Tests:** `test_prune_defers_batch_when_pruning_lock_is_held`,
  `test_dataset_cache_update_is_deferred_while_another_process_holds_the_lock`,
  `test_waiting_for_the_cache_lock_is_announced_in_upstreams_words`, `test_a_lock_is_still_held_when_its_pid_cannot_be_written`,
  `test_a_lock_that_cannot_be_taken_leaves_the_holders_pid_in_its_file`, `test_a_lock_file_holds_only_its_new_holders_pid`
- **Harness:** the `dev/diff_*.py` harnesses stub upstream's lock functions; `dev/e2e_subprocess.py`
  `test_take_and_prune_started_together_both_finish`

### DEV-041: A lock file that can't be made never stops a run

- **Upstream:** carries on unlocked and dies releasing the lock, after its update, so it keeps taking snapshots. (A
  run directory it can't make stops it at startup.)
- **pysanoid:** reports it, and skips only what the lock guards. The snapshot listing is read from zfs without
  updating the cache, a cache that can't be updated is removed so the next run reads zfs, datasets.txt isn't
  rewritten, and that prune batch isn't pruned: prune fails closed, where upstream destroys unlocked.
- **Why:** stopping at the first lock meant the snapshots just taken were never cached, so each run took them again
  until the cache expired, and after that no run took any. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Code:** `try_lock`, `wait_lock`, `get_snapshots`, `add_cached_snapshots`, `remove_cached_snapshots`,
  `ZfsClient.flush_dataset_cache`, `prune_snapshots`, `main`
- **Tests:** `test_prune_skips_a_batch_whose_lock_file_cannot_be_made`,
  `test_a_lock_file_that_cannot_be_made_is_handed_to_the_caller`, `test_main_keeps_taking_snapshots_when_a_directory_is_unusable`,
  `test_get_snapshots_reads_zfs_when_the_lock_file_cannot_be_made`,
  `test_add_cached_snapshots_drops_the_cache_when_the_lock_file_cannot_be_made`,
  `test_remove_cached_snapshots_drops_the_cache_when_the_lock_file_cannot_be_made`,
  `test_flush_dataset_cache_carries_on_when_the_lock_file_cannot_be_made`
- **Harness:** the `RUN_UNUSABLE` known classes of `dev/diff_prune.py`, `dev/diff_snapshot_cache_mutation.py` and
  `dev/diff_dataset_cache.py`, and "no lock file can be made, the cache expired" in
  `dev/diff_snapshot_enumeration.py`, each checking pysanoid against itself; `dev/e2e_subprocess.py`
  `test_an_unusable_run_dir_does_not_stop_taking_snapshots`

## Process and environment

### DEV-042: A hook that outlives script_timeout is killed, and the run carries on

- **Upstream:** arms `alarm $timeout` with `local $SIG{ALRM} = sub { die "alarm\n" }`, but that handler is scoped
  to the `if ($timeout gt 0) { ... }` block that arms the alarm, not to the `eval` around `system()`. By the time
  the hook runs, SIGALRM's default action is back, so a hook that outlives `script_timeout` kills sanoid itself
  mid-run (exit 142, 128 + SIGALRM), with nothing flushed and no cache update.
- **pysanoid:** `subprocess.run(timeout=...)` kills the hook's own process and the run carries on: the hook has
  failed (-1, with upstream's `didn't finish in the allowed time` warning). Anything the hook started keeps running,
  as the whole hook does upstream.
- **Why:** a slow hook costs at most its own dataset, not the run. Upstream's users met it as a `post_snapshot_script`
  running syncoid past the default 5 s: the snapshot just taken never reached the cache, so later runs took the daily
  again, two or three a day, and hooks stopped running. Dangerous for the data.
- **Evidence:** confirmed against real Perl.
- **Upstream reports:** #1030; #1116 (open: a `pre_snapshot_script` that timed out, and no snapshot taken).
- **Code:** `run_hook_script`
- **Tests:** `test_a_hook_that_outlives_its_timeout_is_killed_and_counts_as_failed`,
  `test_a_hook_that_outlives_script_timeout_does_not_make_the_next_run_take_again`,
  `test_a_pre_snapshot_script_that_times_out_is_a_failed_one_and_the_run_goes_on`
- **Harness:** `dev/diff_hooks.py` lets no timeout fire; its hand mode's standing check
  `verify_upstream_alarm_crashes_process` confirms upstream still dies when one does

### P5: script_timeout sets the timeout upstream's alarm() sets

- **Upstream:** gates the value as a string (`$timeout gt 0`, so `+5`, `-5` and a leading blank set none), then
  passes Perl's number to `alarm`, which takes a C int: whole seconds, cut to 32 bits. A missing one is undef: no
  timeout, with Perl's "uninitialized" warning.
- **pysanoid:** the same reading (`5s` is 5, `1_0` is 1, `1.5` is 1, `0.5` none, `4294967297` is 1). A value that
  isn't a plain whole number (a trailing comment allowed, as in a retention count) is reported like a bad value,
  once a run, saying how it is read. A missing one is warned about and sets none.
- **Differences left, harmless:** a value Perl reads as Inf or NaN sets none here; upstream's `alarm()` gets
  whatever number its variable last held, the previous hook's timeout. Over 100,000,000 s, macOS's `alarm()`
  refuses the value and Linux sets it; pysanoid sets it.
- **Why noted:** the gate and the numification look like bugs and are upstream's reading, which an existing config
  may depend on.
- **Evidence:** read back from real Perl with `alarm $timeout; alarm 0`; fuzzed by `dev/diff_hooks.py`.
- **Code:** `_alarm_seconds`, `_script_timeout`, `_perl_gt_zero`
- **Tests:** `test_run_hook_script_sets_the_timeout_upstreams_alarm_sets`,
  `test_a_script_timeout_that_is_not_a_plain_whole_number_is_reported`,
  `test_a_missing_script_timeout_runs_the_hook_untimed_with_a_warning`,
  `test_take_snapshots_negative_script_timeout_does_not_silently_stop_protecting_the_dataset`,
  `test_prune_negative_script_timeout_does_not_block_the_destroy`
- **Harness:** `dev/diff_hooks.py` (known class "over macOS's limit")

### P6: A hook command with no shell metacharacter runs without the shell

- **Upstream:** a one-string `system($script)` goes through `/bin/sh -c` only when it holds a shell metacharacter
  (or starts with `. `, `exec ` or `NAME=`). Otherwise Perl splits it on whitespace and execs the words itself,
  doing a final ` 2>&1` itself and dropping one final newline.
- **pysanoid:** the same rule. A trailing `# comment` reaches the hook as arguments, and `exit 0`, `:` or `! cmd`
  can't start. A command that can't be exec'd is a failed hook with -1 on both sides; Perl says why in its own
  `Can't exec` warning, pysanoid in a `could not run` line of its own. A script with no `#!` line is run by
  `/bin/sh`, as Perl's `execvp` runs it.
- **Why noted:** it looks like a reason to run every hook through the shell, and isn't. A `pre_pruning_script` of
  `exit 0` vetoes every prune, as upstream's does.
- **Evidence:** confirmed against real Perl; fuzzed by `dev/diff_hooks.py`.
- **Code:** `_hook_words`, `_run_hook`
- **Tests:** `test_a_hook_command_runs_as_perls_system_runs_it`, `test_a_hook_perl_cannot_start_is_a_failed_hook`,
  `test_a_hook_command_that_cannot_be_execd_is_a_failed_hook_with_minus_one`,
  `test_a_hook_script_with_no_interpreter_line_is_run_by_sh_as_execvp_runs_it`,
  `test_a_final_redirect_joins_the_hooks_stderr_to_its_stdout_without_a_shell`,
  `test_a_hook_command_of_blanks_alone_fails_as_perls_child_with_nothing_to_exec`
- **Harness:** `dev/diff_hooks.py`; `dev/diff_zfs_output.py` runs upstream's whole script beside pysanoid on 21 failing
  hooks, byte for byte, its known class `P6` taking out each side's own line saying why a hook couldn't start

### P9: A hook's variables are set in pysanoid's own environment and deleted after the hook

- **Upstream:** sets each hook's `SANOID_*` variables in `%ENV` before the hook and deletes them after it. Take does
  so around its hooks under `--readonly` too, where it doesn't run them. A `SANOID_*` variable sanoid inherited is
  therefore seen by every hook until one sets it, and by none after: an inherited `SANOID_SCRIPT` reaches the first
  `pre_pruning_script` and no later one.
- **pysanoid:** the same, in `os.environ`.
- **Why noted:** passing each hook its variables in a fresh environment looks equivalent and isn't. Until 0.1.48
  pysanoid did that, and every hook saw what it inherited, for example when started from another hook: a hook
  script that branches on `$SANOID_SCRIPT` took the wrong branch.
- **Evidence:** confirmed against real Perl: upstream's whole script and pysanoid run as programs with
  `SANOID_SCRIPT` and `SANOID_TARGETS` already set (`AUDIT.md`, the fifth audit). Under `--readonly`, by upstream's
  code: its set and delete sit outside its readonly check.
- **Code:** `run_hook_script`
- **Tests:** `test_a_hooks_variables_are_deleted_after_it_as_upstream_deletes_them`,
  `test_a_hook_that_cannot_start_still_has_its_variables_deleted`,
  `test_take_under_readonly_deletes_a_hooks_variables_without_running_it`
- **Harness:** `dev/diff_whole_run.py` compares every hook's `SANOID_*` variables, inherited ones included

### P18: When a hook runs, and what is said when it fails

- **Upstream:** applies Perl's truth test to every hook setting, so one set to `0` (or blank) is unset. Under
  `--readonly` its take prints a hook's `executing ...` line and then doesn't run it. A hook that fails is warned
  about as `WARN: <key> failed, $?`, Perl's wait status; one that can't start, with -1.
- **pysanoid:** the same, in upstream's words. One that can't start also gets a line of its own saying why, where
  Perl gives its own `Can't exec` warning.
- **Why noted:** Python's truth test would run `0` as a command, and the warnings' numbers look like a slip.
- **Evidence:** confirmed against real Perl.
- **Code:** `_perl_true`, `run_hook_script`, `_wait_status`
- **Tests:** `test_run_hook_script_treats_a_script_of_0_as_unset`, `test_prune_treats_a_hook_set_to_0_as_unset`,
  `test_take_snapshots_treats_a_pre_snapshot_script_of_0_as_unset`, `test_take_snapshots_treats_a_post_snapshot_script_of_0_as_unset`,
  `test_take_snapshots_announces_its_hooks_under_readonly_without_running_them`,
  `test_a_failed_hooks_warning_gives_its_wait_status_as_upstreams_does`, `test_the_warning_names_the_hook_that_failed`,
  `test_a_hook_that_works_is_not_warned_about`
- **Harness:** `dev/diff_hooks.py`

### DEV-043: A command that can't start for want of a process fails at once

- **Upstream:** Perl's `system()` and piped `open` retry a fork that fails with EAGAIN every 5 s for as long as the
  system is out of processes.
- **pysanoid:** a command that can't start fails at once. A snapshot is reported, a destroy is `could not remove`,
  and a hook has failed, each in upstream's words for a `system()` that returned -1, after a line saying why; the
  run goes on, as upstream's does once its `system()` returns -1. A listing (`zfs get`, `zfs list`, `zpool list`)
  that can't start stops the run with a FATAL, as a missing zfs binary does.
- **Why:** while the system is out of processes, upstream's run can hang, taking no snapshots and showing no
  failure. Dangerous for the data.
- **Evidence:** confirmed against real Perl, and kept confirmed: with RLIMIT_NPROC at 1, upstream's own `runscript`
  and a piped open are still waiting after 12 s, where pysanoid's `run_hook_script` returns -1 in a fraction of a
  second.
- **Code:** `take_snapshots`, `run_hook_script`, `prune_snapshots`, `run_zfs_bytes` (no special handling: subprocess
  raises at once)
- **Tests:** `test_take_snapshots_carries_on_after_a_snapshot_that_cannot_start`,
  `test_take_snapshots_dst_retry_that_cannot_start_is_a_problem`, `test_prune_carries_on_when_a_destroy_cannot_start`,
  `test_run_hook_script_returns_minus_one_when_the_hook_cannot_start`,
  `test_a_hook_that_cannot_start_for_want_of_a_process_is_told_as_one_that_cannot_be_execd`
- **Harness:** `dev/diff_hooks.py` standing check `verify_fork_failure_waits_upstream_only` (hand mode), the wait
  itself on both sides; `dev/diff_take_snapshots.py` and `dev/diff_prune.py` make one call fail to start
  (`FAIL_START`) by overriding upstream's `system()` to return -1 at once, comparing what follows the wait;
  `dev/e2e_subprocess.py` `test_a_run_with_no_process_to_spare_stops_at_once` (a fork-retrying mutant hangs it)

### DEV-044: Ctrl-C while a hook or zfs command runs stops pysanoid

- **Upstream:** Perl's `system()` ignores SIGINT and SIGQUIT in sanoid while it waits, so Ctrl-C kills only the
  command, and the run carries on with the next one, destroys included.
- **pysanoid:** Ctrl-C stops the run, as it stops any Python program: the command is killed, Python prints its
  traceback, and the process dies from SIGINT.
- **Why:** an operator can't stop a mistaken prune upstream. Dangerous for the data.
- **Evidence:** confirmed against real Perl, and kept confirmed: a hook sending SIGINT to its process group, as
  Ctrl-C does, dies under upstream's own `runscript`, which returns its wait status (2) and carries on; under
  pysanoid's `run_hook_script` the process dies from SIGINT.
- **Code:** none (no special handling: `subprocess.run` leaves SIGINT at Python's default)
- **Tests:** `test_ctrl_c_while_a_command_runs_stops_the_prune` (a veto hook and a `zfs destroy`; both fail against
  a mutant that ignores SIGINT while a command runs, as Perl's `system()` does)
- **Harness:** `dev/diff_hooks.py` standing check `verify_ctrl_c_stops_pysanoid_only` (hand mode);
  `dev/e2e_subprocess.py` `test_ctrl_c_during_a_hook_stops_the_run` (the whole program, two datasets: nothing
  destroyed, where the mutant destroyed all four dailies)

### DEV-045: A zfs or zpool that can't be run stops the run

- **Upstream:** reads its listings through an unchecked pipe (`open FH, "$zfs get ... |"`). With no shell
  metacharacter in the command, Perl execs it directly: when `zfs` is missing or can't be exec'd, `open` fails without
  a word, the listing reads as empty, and `$? >> 8` is 0, so there is no retry. The cache is written empty and fresh,
  take tries every type (each `zfs snapshot` only warned about, DEV-028), prune sees no snapshots, and the run exits 0.
  (A command holding a shell metacharacter goes through `/bin/sh`, which exits 127: the same, after one retry.)
- **pysanoid:** a zfs or zpool command that can't be run is a FATAL at the first listing: nothing is taken or
  pruned, no cache is written, and the run exits 1. (A snapshot or destroy that can't start is reported and the run
  goes on, DEV-043.)
- **Why:** a run that takes nothing from a false "no snapshots exist" while looking successful is a silent loss of
  protection. Dangerous for the data.
- **Evidence:** confirmed against real Perl: the pipe's open, read and exit status for a `zfs` that isn't there,
  with and without a shell metacharacter (2026-10-25); the rest read in upstream's `getsnaps`.
- **Code:** `run_zfs_bytes`
- **Tests:** `test_a_missing_zfs_binary_stops_the_run_before_anything_is_taken` (fails against a mutant that reads a
  missing binary as a listing that exited 127), `test_a_nul_byte_reaching_a_zfs_command_is_a_clean_error`
- **Harness:** `dev/e2e_subprocess.py` `test_a_run_with_no_process_to_spare_stops_at_once` (the same path, for a
  fork that fails)

### P7: Output that can't be written never stops the run, with one exception

- **Upstream:** Perl's `print` and `warn` don't fail: a line lost to a closed pipe or a full disk is dropped and the
  run carries on, and upstream exits 1 when its last flush of stdout fails. Perl writes the bytes it read, whatever
  the locale.
- **pysanoid:** the same. Its stdout and stderr drop a line that can't be written, and a lost stdout is reported and
  exits non-zero. Output is written as the bytes read (UTF-8 with surrogateescape), and line-buffered, as Perl flushes
  STDOUT before every command it starts, so the journal keeps each line in order with what a hook or zfs prints.
- **The exception, harmless:** where SIGPIPE is at its default (a shell pipeline whose reader has gone), upstream is
  killed at its next write, mid-run. Python ignores SIGPIPE before any of pysanoid runs, so which applies can't be
  known; pysanoid carries on, as upstream does under systemd, which ignores SIGPIPE. Finishing does only what
  upstream's next run would.
- **Why noted:** Python's `print` raises where Perl's doesn't: unhandled, the run would end at its first line,
  nothing taken or pruned, and a name the locale can't encode would end it the same way.
- **Evidence:** confirmed against real Perl, with a closed pipe and SIGPIPE ignored, as systemd sets it, and with a
  log that can't grow.
- **Code:** `_PerlOutput`, `_perl_output`, `main`
- **Tests:** `test_output_that_cannot_be_written_does_not_stop_the_run`, `test_output_is_written_as_read_whatever_the_locale`,
  `test_stdout_passes_non_utf8_bytes_through_instead_of_ending_the_run`,
  `test_stdout_is_line_buffered_so_the_journal_keeps_the_order`
- **Harness:** `dev/e2e_subprocess.py` `test_output_that_cannot_be_written_does_not_stop_the_run`,
  `test_a_log_that_cannot_grow_does_not_stop_the_run`, `test_names_are_written_as_read_whatever_the_locale`,
  `test_hook_output_stays_in_order_with_pysanoids_own_lines`

### DEV-046: Hooks start with SIGPIPE at its default, and with C.UTF-8 when no locale is set

- **Upstream:** its hooks inherit sanoid's own SIGPIPE disposition and locale, which under systemd means SIGPIPE
  ignored and no locale.
- **pysanoid:** Python sets both up before any of pysanoid runs. It ignores SIGPIPE itself and gives children the
  default, and with no locale set it coerces the C locale to `LC_CTYPE=C.UTF-8` (PEP 538). A hook gets both, exec'd
  or run through the shell.
- **Why:** harmless, and not a choice: it can't be known which disposition upstream's hooks would have had (as for
  output, P7). What follows from it: a hook's own pipeline (`zfs list | head -1`) stops quietly instead of seeing
  write errors, and a hook that writes to pysanoid's stdout or stderr after its reader has gone (a journal stream
  lost mid-run) is killed where upstream's gets a write error. Either way the hook has failed, so a
  `pre_pruning_script` vetoes and a `pre_snapshot_script` with `no_inconsistent_snapshot` holds back its snapshots,
  reported. zfs is unaffected: pysanoid reads everything it prints (DEV-047).
- **Evidence:** confirmed against real Perl, and kept confirmed: started as systemd starts a service, upstream's
  `runscript` gives a hook SIGPIPE ignored and no `LC_CTYPE`, pysanoid's `run_hook_script` SIGPIPE at its default and
  `LC_CTYPE=C.UTF-8`, both for a hook exec'd and one run through the shell.
- **Code:** none (Python's start-up; `_run_hook` passes the environment on)
- **Tests:** `test_a_hook_starts_with_sigpipe_at_its_default_and_c_utf8`, exec'd and through the shell: pysanoid in
  a child Python started as systemd starts a service, its hook writing into a pipe whose reader has gone (killed
  there at the default, carrying on if ignored). It fails against mutants that let either way of starting a hook
  inherit SIGPIPE ignored, or drop `LC_CTYPE`.
- **Harness:** `dev/diff_hooks.py` standing check `verify_hooks_inherit_what_python_sets_up` (hand mode: upstream's
  side beside pysanoid's; fails against the same mutants)

### DEV-047: What zfs and zpool print is read, then written out when the command has finished

- **Upstream:** leaves zfs's stderr, and the stdout and stderr of `zfs snapshot` and `zfs destroy`, on the terminal:
  zfs writes them itself.
- **pysanoid:** reads them and writes them out when the command has finished: a snapshot's or a destroy's stdout and
  stderr, and the stderr of a listing or a capacity query. The bytes are the same and come at the same place among
  the run's lines. They come later by the time the command took, and what one command wrote to stdout comes before
  what it wrote to stderr. For a destroy that wrote its stderr first, that is the other way round from upstream. For a
  snapshot it is the order upstream shows too: upstream runs `zfs snapshot` inside Capture::Tiny's `tee_stderr`,
  which passes stderr on through a process of its own, so stdout reaches the terminal first (8 of 8 runs; a race,
  not a rule).
- **Why:** harmless. Reading them is what keeps zfs running when the journal's stream is lost (P7, DEV-046).
- **Evidence:** confirmed against real Perl, and kept confirmed: upstream's whole script and pysanoid run side by
  side as programs, their merged output compared byte for byte (`dev/diff_zfs_output.py`, since 2026-10-25; first
  as a one-off on 29 cases, `AUDIT.md`, 0.1.43).
- **Code:** `run_zfs_bytes`, `run_zfs_cmd`, `_echo_zfs_stderr`, `_write_as_written`
- **Tests:** `test_main_shows_what_zfs_writes_on_a_snapshot_that_works`, `test_what_zfs_writes_on_a_destroy_that_works_is_shown`,
  `test_main_writes_zfss_complaint_out_in_the_bytes_zfs_wrote`,
  `test_main_writes_a_zfs_warning_out_in_the_bytes_zfs_wrote_and_adds_none`,
  `test_a_listings_complaint_is_written_out_as_zfs_wrote_it`, `test_main_writes_a_listings_complaint_out_in_the_bytes_zfs_wrote`
- **Harness:** `dev/diff_zfs_output.py` (17 hand cases, fuzz; known class: a destroy's stderr before its stdout,
  checked by this entry's rule; fails against five broken versions of pysanoid's output); `dev/e2e_subprocess.py`
  `test_what_zfs_writes_is_shown_where_upstream_shows_it`,
  `test_a_failed_snapshots_complaint_is_written_out_in_the_bytes_zfs_wrote`,
  `test_a_listings_complaint_is_written_out_as_zfs_wrote_it`

### P10: What zfs and zpool print is kept as bytes

- **Upstream:** never decodes what zfs and zpool print. A listing, a complaint or a warning is bytes, lines end at a
  newline only, and whether a command worked is its exit status alone.
- **pysanoid:** keeps them as bytes (`run_zfs_bytes`), reads a listing with every byte kept (UTF-8 with
  surrogateescape), splits lines on the newline only, and writes them out as read (DEV-047). Nothing zfs prints can
  make a command count as failed.
- **Why noted:** Python's subprocess reading text would refuse a byte the encoding can't hold once the command had
  finished, and turn CR and CRLF into newlines: a destroy that worked counted as one that couldn't run, and a
  cache line was rewritten.
- **Evidence:** confirmed against real Perl (a destroy that printed such a byte is a destroy that worked).
- **Code:** `run_zfs_bytes`, `_write_as_written`, `_read_text`, `_lines`
- **Tests:** `test_a_destroy_that_worked_counts_though_zfs_printed_a_byte_that_is_not_utf8`,
  `test_a_snapshot_that_worked_is_taken_though_zfs_printed_a_byte_that_is_not_utf8`,
  `test_a_pools_capacity_is_read_though_zpool_printed_a_byte_that_is_not_utf8`,
  `test_main_prunes_when_zpool_prints_a_byte_that_is_not_utf8`, `test_cache_and_zfs_output_are_split_on_newline_only`,
  `test_get_snapshots_cache_is_byte_exact_through_a_real_pipe`, `test_get_child_datasets_is_byte_exact_through_a_real_pipe`
- **Harness:** `dev/diff_prune.py` and `dev/diff_take_snapshots.py` (zfs warns in bytes that aren't UTF-8);
  `dev/e2e_subprocess.py` `test_what_zfs_prints_that_is_not_utf8_fails_no_command`,
  `test_a_pool_whose_zpool_prints_a_byte_that_is_not_utf8_is_still_pruned`

## --debug

### P8: --debug prints upstream's text

- **Upstream:** with `--debug`, init's DEBUG lines trace the config merge (`initializing`, `overriding`,
  `recursively setting`, `ignoring`), the dataset cache says when it is used and when a child lookup asks zfs, and
  `blabber` prints `Dumper(\%config)` under `$Data::Dumper::Sortkeys = 1` and a per-dataset summary.
- **pysanoid:** the same text. The config dump is Data::Dumper's (keys sorted and indented by their bytes, a toggle
  and the `initialized` and `zfs_recursion` marks as Perl integers, every other value single-quoted). The summary
  keeps blabber's uneven blank lines (one after "(no current snapshots)", two after a "newest: ..." line), its blank
  `desired:` for a type the section never configured, and its ages as Perl's arithmetic gives them.
- **Why noted:** the odd spacing and the integers among quoted values look like slips, and are upstream's text,
  which something reading the output may depend on.
- **Evidence:** confirmed against real Perl.
- **Code:** `load_config` (the trace), `ZfsClient` (the dataset cache's lines), `_perl_dumper`, `print_debug_dump`,
  `_perl_hours`
- **Tests:** `test_debug_traces_the_config_merge_as_upstream_does`, `test_debug_traces_recursion_as_upstream_does`,
  `test_debug_through_main_traces_the_config_merge`, `test_the_debug_trace_names_the_defaults_file_as_given`,
  `test_debug_says_when_the_dataset_cache_is_used`, `test_debug_shows_each_child_lookup_that_asks_zfs`,
  `test_the_config_dump_is_data_dumpers_text`, `test_the_config_dump_sorts_and_indents_by_bytes_as_data_dumper`,
  `test_debug_dump_prints_a_blank_desired_value_for_a_type_cfg_never_configured`,
  `test_debug_dump_matches_upstreams_exact_wording_for_no_and_some_snapshots`,
  `test_debug_dump_reproduces_upstreams_own_inconsistent_blank_line_count`, `test_debug_dump_ages_follow_perls_arithmetic`,
  `test_debug_dump_rounds_ages_as_upstream`
- **Harness:** `dev/diff_debug_dump.py` (the whole dump, as bytes); `dev/diff_config_merge.py` and
  `dev/diff_dataset_cache.py` (the trace); `dev/e2e_subprocess.py` `test_debug_prints_upstreams_trace_and_dump`

### DEV-048: The --debug summary, the recursion trace, and the order take and prune go in are fixed

- **Upstream:** prints the summary's datasets and each dataset's types, and the `recursively setting` lines for one
  child, in Perl's per-process random hash order. (Its config dump is sorted, and the rest of its trace comes in
  file order, as pysanoid's does.) Take and prune go through the sections in that order too (`keys %config`), and
  prune through each section's types (`keys %{$config{$section}}`), so the order of the datasets' snapshots, destroys
  and hooks changes from one run of upstream to the next. Take's types come in a fixed order, yearly first.
- **pysanoid:** a fixed order: the sections as the merged config holds them, the types in the order their
  snapshots first appear in the listing, and the recursion's keys in the order the section's merged settings hold
  them. Take and prune go through the sections as the merged config holds them, and both through the types yearly
  first, as upstream's take does.
- **Why:** harmless. Upstream has no fixed order to match, and each snapshot, destroy and hook stands alone: what is
  taken and destroyed doesn't depend on the order.
- **Code:** `print_debug_dump`, `load_config`, `take_snapshots`, `prune_snapshots`
- **Tests:** none: no order can be matched. The harnesses compare the lines as sets in those places.
- **Harness:** `dev/diff_debug_dump.py` sorts the summary's blocks and each block's type lines before comparing;
  `dev/diff_config_merge.py` sorts each child's run of `recursively setting` lines; `dev/diff_take_snapshots.py`
  and `dev/diff_zfs_output.py` group the lines by dataset, `dev/diff_prune.py` compares its warnings as a set, and
  `dev/diff_whole_run.py` compares each dataset's prune hooks type by type

### DEV-049: An empty here-doc in sanoid.conf reads as blank

- **Upstream:** reads an empty here-doc value (`daily = <<EOT`, then `EOT`) as undefined, which its dump shows as
  `undef` and its arithmetic reads as 0.
- **pysanoid:** reads it as a blank value, as in `sanoid.defaults.conf` (DEV-010): the dump shows `''`, and a blank
  value is read as these entries describe (a blank count is refused by prune, DEV-008, and read as 0 by take, P1).
  An empty here-doc `path` is the exception, and parity: the section's name is the path, as upstream's `defined`
  test reads it.
- **Why:** harmless here; what makes a blank count safe is DEV-008.
- **Evidence:** confirmed against real Perl.
- **Code:** `_section_dict`, `load_config` (the path)
- **Tests:** `test_an_empty_here_doc_reads_as_a_blank_value` (fails against a mutant that reads it as 0),
  `test_an_empty_here_doc_path_falls_back_to_the_section_name`
- **Harness:** `dev/diff_ini_grammar.py` hand cases "an empty here-doc, then a value" and "a value, then an empty
  here-doc"
