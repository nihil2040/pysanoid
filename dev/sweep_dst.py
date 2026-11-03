#!/usr/bin/env python3
"""DST and calendar check for take_snapshots' preferred times (_last_preferred_time).

pysanoid deliberately computes preferred times with zoneinfo rather than upstream's DST arithmetic (see
_last_preferred_time and DEV-030), so this is not a parity diff. It checks pysanoid against a
brute-force answer, and FAILS on any difference:

  hourly/frequently  the most recent real instant <= now whose local wall clock reads the preferred minute
                     (both passes through a repeated hour count; a skipped hour has none)
  daily and longer   one per calendar period: the wall time's first occurrence (zoneinfo fold=0, so a
                     skipped wall time maps forward by the gap and a repeated one to its first pass),
                     the latest such time <= now, skipping periods that lack the day (the 31st, 29 February)

Where the current month (or year) lacks the configured day, pysanoid raises and plan_snapshots reports it;
that is expected here, not a failure. weekly_wday is read modulo 7 and a negative frequent_period by its
size (DEV-031, DEV-032).

It also runs upstream's REAL take_snapshots under Perl (one instrumentation line records $lastpreferred
where $maxage is computed; time() is overridden per instant) and reports how often upstream differs from
the same answer, for information: those are the documented differences.

Modes:
  check   America/New_York, Europe/London, Europe/Dublin, Australia/Sydney: every 10 minutes from two days
          before to nine days after each changeover of 2026, several schedule variants; plus noon on every
          day of 2026-2028 for the settings some months or years lack
  wide    the same over 2026-2030, adding Australia/Lord_Howe (a 30-minute change) and America/St_Johns

Run: python3 dev/sweep_dst.py check
     python3 dev/sweep_dst.py wide
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DEV_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV_DIR))
sys.path.insert(0, str(DEV_DIR.parent))
from lib.extract import extract_subs
from lib.perlrun import PERL_JSON_HEAD, WRITE_RESULT_PERL, build_harness, run_harness
from lib.slots import ideal, perl_wday

import pysanoid as p

TYPES = ("yearly", "monthly", "weekly", "daily", "hourly", "frequently")

HEAD = PERL_JSON_HEAD + r'''
use Time::Local;
our $__NOW = 0;
BEGIN { *CORE::GLOBAL::time = sub () { $main::__NOW + 0 } }
my %args = ('verbose' => 0, 'debug' => 0, 'readonly' => 1);
my $zfs = 'zfs';
my $cacheTTL = 1200;
my (%config, %snaps, %snapsbytype, %taken);
our @__LP;
sub tee_stderr (&) { my $code = shift; my $ret = $code->(); return ('', $ret); }
sub getsnaps { return (); }
sub addcachedsnapshots { 1 }
sub runscript { 0 }
'''

TAIL = r'''
my $__scn = do { local $/; decode_json(<STDIN>) };
%config = %{ $__scn->{config} };
my @__out;
foreach my $now (@{ $__scn->{nows} }) {
    $main::__NOW = $now;
    @__LP = ();
    my $died = '';
    eval { take_snapshots(\%config, {}, \%snapsbytype, {}); 1 } or do { ($died = $@) =~ s/\s+$//; };
    push @__out, { now => $now, died => ($died ? 1 : 0), lp => { map { $_->[0] => $_->[1] + 0 } @__LP } };
}
my %__result = ( out => \@__out );
''' + WRITE_RESULT_PERL

MARK = "my $maxage = time()-$lastpreferred;"
_BODY = extract_subs("take_snapshots", "get_date")
if _BODY.count(MARK) != 1:
    raise SystemExit(f"upstream's take_snapshots no longer contains {MARK!r} exactly once; re-check the instrumentation")
SCRIPT = build_harness(HEAD, _BODY.replace(MARK, MARK + " push @__LP, [$type, $lastpreferred];"), TAIL)


def base_cfg(**over) -> dict:
    cfg = {"path": "tank/a", "autosnap": 1, "process_children_only": 0, "zfs_recursion": 0,
           "frequently": 1, "hourly": 1, "daily": 1, "weekly": 1, "monthly": 1, "yearly": 1,
           "frequent_period": 15, "hourly_min": 0, "daily_hour": 23, "daily_min": 59,
           "weekly_wday": 1, "weekly_hour": 23, "weekly_min": 30,
           "monthly_mday": 1, "monthly_hour": 0, "monthly_min": 0,
           "yearly_mon": 1, "yearly_mday": 1, "yearly_hour": 0, "yearly_min": 0}
    cfg.update(over)
    return cfg


def changeover_variants(tr: date) -> dict[str, dict]:
    out = {"defaults": base_cfg()}
    for hh in (1, 2):  # land in the skipped or repeated hour of most zones
        out[f"everything at {hh:02}:30 on the changeover day"] = base_cfg(
            frequent_period=7, hourly_min=30, daily_hour=hh, daily_min=30,
            weekly_wday=perl_wday(tr), weekly_hour=hh, weekly_min=30,
            monthly_mday=tr.day, monthly_hour=hh, monthly_min=30,
            yearly_mon=tr.month, yearly_mday=tr.day, yearly_hour=hh, yearly_min=30)
    out["hourly_min=45, frequent_period=-15"] = base_cfg(hourly_min=45, frequent_period=-15)
    for w in (7, 13, 20, -3):
        out[f"weekly_wday={w}"] = base_cfg(weekly_wday=w)
    return out


CALENDAR_VARIANTS = {
    "monthly_mday=31": base_cfg(monthly_mday=31),
    "monthly_mday=30": base_cfg(monthly_mday=30),
    "monthly_mday=29": base_cfg(monthly_mday=29),
    "yearly 29 February": base_cfg(yearly_mon=2, yearly_mday=29),
}


def changeovers(tz: ZoneInfo, year: int) -> list[date]:
    """Local dates on which the UTC offset changes, found by scanning the year hour by hour."""
    t = datetime(year, 1, 1, tzinfo=tz).timestamp()
    end = datetime(year + 1, 1, 1, tzinfo=tz).timestamp()
    out, prev = [], datetime.fromtimestamp(t, tz).utcoffset()
    while t < end:
        t += 3600
        dt = datetime.fromtimestamp(t, tz)
        if dt.utcoffset() != prev:
            out.append(dt.date())
            prev = dt.utcoffset()
    return out


def sweep(zone: str, cfg: dict, nows: list[int], label: str, tally: dict, failures: list) -> None:
    tz = ZoneInfo(zone)
    res = run_harness(SCRIPT, stdin_obj={"config": {"tank/a": cfg}, "nows": nows}, env={"TZ": zone}, timeout=600)
    if res.data is None:
        raise SystemExit(f"perl harness failed for {zone} / {label}:\n{res.stderr[-2000:]}")
    for row in res.data["out"]:
        now = row["now"]
        now_dt = datetime.fromtimestamp(now, tz)
        for t in TYPES:
            want = ideal(now, tz, cfg, t)
            try:
                got: float | None = p._last_preferred_time(now_dt, cfg, t).timestamp()
            except (ValueError, p.SanoidError):
                got = None
            tally["checked"] += 1
            if got != want:
                failures.append((zone, label, t, now_dt.isoformat(), got, want))
            up = row["lp"].get(t)
            if row["died"] or up is None:
                tally["upstream died (pysanoid reports that type and carries on)"] += 1
            elif want is not None and up != want:
                tally[f"upstream differs from the correct time ({t})"] += 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["check", "wide"])
    ap.add_argument("ignored", nargs="*", help="dev/run.py passes a count and a seed; unused")
    args = ap.parse_args()

    zones = ["America/New_York", "Europe/London", "Europe/Dublin", "Australia/Sydney"]
    years = [2026]
    if args.mode == "wide":
        zones += ["Australia/Lord_Howe", "America/St_Johns"]
        years = [2026, 2027, 2028, 2029, 2030]

    tally: dict[str, int] = defaultdict(int)
    failures: list = []
    for zone in zones:
        tz = ZoneInfo(zone)
        for year in years:
            for tr in changeovers(tz, year):
                start = int(datetime(tr.year, tr.month, tr.day, tzinfo=tz).timestamp()) - 2 * 86400
                nows = list(range(start, start + 11 * 86400, 600))
                for label, cfg in changeover_variants(tr).items():
                    sweep(zone, cfg, nows, f"{tr} {label}", tally, failures)
        first = datetime(years[0], 1, 1, 12, tzinfo=tz)
        noons = [int((first + timedelta(days=k)).timestamp()) for k in range(366 * (len(years) + 2))]
        for label, cfg in CALENDAR_VARIANTS.items():
            sweep(zone, cfg, noons, f"noon daily, {label}", tally, failures)

    for key in sorted(tally):
        print(f"  {tally[key]:>9,}  {key}")
    if failures:
        print(f"\nFAIL: pysanoid differs from the correct time at {len(failures)} (instant, type) points, e.g.:")
        by_kind = Counter((f[0], f[1].split(" ", 1)[1], f[2]) for f in failures)
        for (zone, label, t), n in by_kind.most_common(10):
            ex = next(f for f in failures if (f[0], f[1].split(" ", 1)[1], f[2]) == (zone, label, t))
            print(f"  {n:>6}  {zone} / {label} / {t}: e.g. now {ex[3]}: pysanoid {ex[4]}, correct {ex[5]}")
        sys.exit(1)
    print(f"\nOK: pysanoid matches the correct time at all {tally['checked']:,} (instant, type) points")


if __name__ == "__main__":
    main()
