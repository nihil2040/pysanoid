import ast
import contextlib
import errno
import fcntl
import io
import json
import math
import os
import random
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
import tokenize
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import TZPATH, ZoneInfo, available_timezones

import pytest

import pysanoid as ps

TZ = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

REPO_ROOT = Path(__file__).parent
REAL_DEFAULTS_CONF = REPO_ROOT / "sanoid.defaults.conf"


def write_confdir(tmp_path: Path, sanoid_conf: str) -> Path:
    """Uses the actual shipped sanoid.defaults.conf, not a copy, so these
    tests break if the shipped file and the code drift apart."""
    d = tmp_path / "conf"
    d.mkdir()
    shutil.copy(REAL_DEFAULTS_CONF, d / "sanoid.defaults.conf")
    (d / "sanoid.conf").write_text(sanoid_conf)
    return d


def make_zfs_client(tmp_path: Path) -> ps.ZfsClient:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(exist_ok=True)
    return ps.ZfsClient(cache_dir, cache_ttl=1200, force_update=True)


# ---------------------------------------------------------------------------
# config parsing / templating
# ---------------------------------------------------------------------------

def test_basic_section_merges_defaults(tmp_path):
    confdir = write_confdir(tmp_path, """
[tank/data]
    daily = 30
    autosnap = yes
""")
    zfs = make_zfs_client(tmp_path)
    config = ps.load_config(confdir, zfs)

    assert "tank/data" in config
    cfg = config["tank/data"]
    assert cfg["path"] == "tank/data"
    assert cfg["daily"] == "30"
    assert cfg["autosnap"] is True          # normalized from "yes"
    assert cfg["hourly"] == "48"            # inherited from defaults, untouched
    assert cfg["autoprune"] is True         # default


def test_unknown_key_is_fatal(tmp_path):
    confdir = write_confdir(tmp_path, """
[tank/data]
    made_up_setting = 1
""")
    zfs = make_zfs_client(tmp_path)
    with pytest.raises(ps.SanoidError, match="made_up_setting"):
        ps.load_config(confdir, zfs)


def test_named_template_overrides_default_but_not_local(tmp_path):
    confdir = write_confdir(tmp_path, """
[template_production]
    daily = 60
    weekly = 4

[tank/data]
    use_template = production
    daily = 14
""")
    zfs = make_zfs_client(tmp_path)
    config = ps.load_config(confdir, zfs)
    cfg = config["tank/data"]
    assert cfg["weekly"] == "4"      # from template
    assert cfg["daily"] == "14"      # local override wins over template


def test_process_children_only_not_inherited_by_recursion_children(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, """
[tank/data]
    recursive = yes
    process_children_only = yes
    daily = 20
""")
    zfs = make_zfs_client(tmp_path)

    _fake_zfs_tree(monkeypatch, ["tank/data", "tank/data/child1"])
    config = ps.load_config(confdir, zfs)

    assert config["tank/data"]["process_children_only"] is True
    assert "process_children_only" not in config["tank/data/child1"]
    assert config["tank/data/child1"]["daily"] == "20"
    assert config["tank/data/child1"]["path"] == "tank/data/child1"


def test_zfs_recursive_disables_child_autosnap(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, """
[tank/data]
    recursive = zfs
    autosnap = yes
""")
    zfs = make_zfs_client(tmp_path)

    _fake_zfs_tree(monkeypatch, ["tank/data", "tank/data/child1"])
    config = ps.load_config(confdir, zfs)

    assert config["tank/data"]["zfs_recursion"] is True
    assert config["tank/data"]["autosnap"] is True
    assert config["tank/data/child1"]["zfs_recursion"] is True
    assert config["tank/data/child1"]["autosnap"] is False


def test_skip_children_removes_child_entries(tmp_path, monkeypatch):
    # note: skip_children is for datasets that are *only* discovered via zfs
    # recursion; one that also has its own [section] must list it after the
    # parent, where that section's own settings win (above it, the config is
    # refused: see the section-order tests further down).
    confdir = write_confdir(tmp_path, """
[tank/data]
    skip_children = yes
""")
    zfs = make_zfs_client(tmp_path)

    _fake_zfs_tree(monkeypatch, ["tank/data", "tank/data/child1"])
    config = ps.load_config(confdir, zfs)

    assert "tank/data" in config
    assert "tank/data/child1" not in config


# ---------------------------------------------------------------------------
# scheduling
# ---------------------------------------------------------------------------

DEFAULT_SCHEDULE_CFG = {
    "frequent_period": 15, "hourly_min": 0,
    "daily_hour": 23, "daily_min": 59,
    "weekly_wday": 1, "weekly_hour": 23, "weekly_min": 30,
    "monthly_mday": 1, "monthly_hour": 0, "monthly_min": 0,
    "yearly_mon": 1, "yearly_mday": 1, "yearly_hour": 0, "yearly_min": 0,
}


def test_daily_preferred_time_is_todays_2359_when_still_before_it():
    now = datetime(2026, 8, 22, 10, 0, tzinfo=TZ)
    preferred = ps._last_preferred_time(now, DEFAULT_SCHEDULE_CFG, "daily")
    assert preferred == datetime(2026, 8, 21, 23, 59, tzinfo=TZ)


def test_daily_preferred_time_is_yesterday_after_2359():
    now = datetime(2026, 8, 22, 23, 59, 30, tzinfo=TZ)
    preferred = ps._last_preferred_time(now, DEFAULT_SCHEDULE_CFG, "daily")
    assert preferred == datetime(2026, 8, 22, 23, 59, tzinfo=TZ)


def test_weekly_preferred_time_lands_on_configured_weekday():
    # 2026-08-22 is a Saturday; weekly_wday=1 means Monday (Perl wday convention: 0=Sun)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=TZ)
    preferred = ps._last_preferred_time(now, DEFAULT_SCHEDULE_CFG, "weekly")
    assert preferred.weekday() == 0  # Monday
    assert preferred <= now


def test_monthly_preferred_time_rolls_back_a_month():
    # a mid-month preferred day/time, checked from *before* that day this month
    cfg = {**DEFAULT_SCHEDULE_CFG, "monthly_mday": 15, "monthly_hour": 12, "monthly_min": 0}
    now = datetime(2026, 8, 1, 0, 0, 0, tzinfo=TZ)
    preferred = ps._last_preferred_time(now, cfg, "monthly")
    assert preferred == datetime(2026, 7, 15, 12, 0, tzinfo=TZ)


def test_yearly_preferred_time_rolls_back_a_year():
    # a mid-year preferred month/day, checked from *before* that date this year
    cfg = {**DEFAULT_SCHEDULE_CFG, "yearly_mon": 6, "yearly_mday": 15, "yearly_hour": 0, "yearly_min": 0}
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=TZ)
    preferred = ps._last_preferred_time(now, cfg, "yearly")
    assert preferred == datetime(2025, 6, 15, 0, 0, tzinfo=TZ)


def _system_zone_file(name: str) -> Path:
    """This system's compiled file for zone `name`, as the C library would open it."""
    for directory in TZPATH:
        if (path := Path(directory) / name).is_file():
            return path
    pytest.skip(f"no zone file for {name} under {TZPATH}")


def _offsets(zone: tzinfo) -> tuple[timedelta | None, ...]:
    """zone's UTC offsets at noon UTC on 1 January and 1 July 2026: a zone's rules, not one fixed offset."""
    return tuple(datetime(2026, month, 1, 12, tzinfo=UTC).astimezone(zone).utcoffset() for month in (1, 7))


NEW_YORK_OFFSETS = (timedelta(hours=-5), timedelta(hours=-4))
UTC_OFFSETS = (timedelta(0), timedelta(0))


def _move_etc_localtime(monkeypatch, tmp_path: Path) -> Path:
    """Moves pysanoid's /etc/localtime to a path in tmp_path, which the test fills or leaves missing, and unsets $TZ and
    $TZDIR. timedatectl answers Europe/Dublin: 0.1.38 asked it, and the C library never does."""
    real_run = subprocess.run

    def run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if list(cmd[:1]) == ["timedatectl"]:
            return subprocess.CompletedProcess(cmd, 0, "Europe/Dublin\n", "")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.delenv("TZDIR", raising=False)
    localtime = tmp_path / "localtime"
    monkeypatch.setattr(ps, "_ETC_LOCALTIME", localtime)
    return localtime


def _tzif(utoffs: list[int], transitions: list[tuple[int, int]]) -> bytes:
    """A version 2 zone file: one type per UTC offset, all named ZZZ, transitions as (time, type index), and an empty
    closing rule line."""
    def block(time_format: str) -> bytes:
        return (b"TZif2" + bytes(15) + struct.pack(">6l", 0, 0, 0, len(transitions), len(utoffs), 4)
                + b"".join(struct.pack(time_format, t) for t, _ in transitions) + bytes(i for _, i in transitions)
                + b"".join(struct.pack(">lbB", off, 0, 0) for off in utoffs) + b"ZZZ\0")
    return block(">l") + block(">q") + b"\n\n"


NO_ZONE = ["text", "cut short", "cut in its closing rule line", "a transition naming a type it lacks", "an offset of a day"]


def _no_zone(what: str) -> bytes:
    """A file glibc reads as no zone, or, cut in its closing rule line, reads in part (P2)."""
    dublin = _system_zone_file("Europe/Dublin").read_bytes()
    return {"text": b"not a zone file\n", "cut short": dublin[:len(dublin) // 2], "cut in its closing rule line": dublin[:-3],
            "a transition naming a type it lacks": _tzif([3600], [(0, 1)]), "an offset of a day": _tzif([86400], [])}[what]


def _local_zone_within(seconds: float = 10.0) -> tzinfo:
    """get_local_zoneinfo()'s zone, failing the test rather than hanging it: Python 3.11's zoneinfo reads on forever
    through a zone file cut short in its closing rule line."""
    result: list[tzinfo | BaseException] = []

    def target() -> None:
        try:
            result.append(ps.get_local_zoneinfo())
        except BaseException as e:  # noqa: BLE001 - raised again below, in the test's own thread
            result.append(e)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    assert result, f"get_local_zoneinfo() still running after {seconds} s"
    if isinstance(result[0], BaseException):
        raise result[0]
    return result[0]


@pytest.mark.parametrize("how", ["a link into a zoneinfo directory", "a link elsewhere", "a copy"])
def test_without_tz_etc_localtime_is_read_itself(monkeypatch, capsys, tmp_path, how: str):
    # the C library, and so upstream, reads the file, whatever links to it
    localtime = _move_etc_localtime(monkeypatch, tmp_path)
    zone_file = tmp_path / ("zoneinfo/America/New_York" if how.endswith("directory") else "zones/NYC")
    zone_file.parent.mkdir(parents=True)
    shutil.copyfile(_system_zone_file("America/New_York"), zone_file)
    if how == "a copy":
        shutil.copyfile(zone_file, localtime)
    else:
        localtime.symlink_to(zone_file)
    assert _offsets(ps.get_local_zoneinfo()) == NEW_YORK_OFFSETS
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("what", ["missing", "a dangling link", *NO_ZONE])
def test_an_etc_localtime_holding_no_zone_is_utc_with_a_warning(monkeypatch, capsys, tmp_path, what: str):
    # P2: the C library, and so upstream, reads UTC; a file cut in its closing rule line, in part. Under Python 3.11 a
    # file cut in its rule line could hang, and one cut short could read past a zone's own table.
    localtime = _move_etc_localtime(monkeypatch, tmp_path)
    if what == "a dangling link":
        localtime.symlink_to(tmp_path / "zoneinfo" / "America" / "New_York")
    elif what != "missing":
        localtime.write_bytes(_no_zone(what))
    assert _offsets(_local_zone_within()) == UTC_OFFSETS
    assert capsys.readouterr().err == f"WARN: {localtime} is missing or isn't a zone file - using UTC.\n"


@pytest.mark.parametrize("tz", ["America/New_York", ":America/New_York", ":{file}", "{file}"])
def test_tz_names_a_zone_as_the_c_library_reads_it(monkeypatch, capsys, tmp_path, tz: str):
    # A zone name or a zone file's path, with or without a leading ':' (`TZ=:/etc/localtime` is a common idiom), over
    # /etc/localtime (here Europe/Dublin's). 0.1.34 warned on all but the plain name and used the system's zone.
    shutil.copyfile(_system_zone_file("Europe/Dublin"), _move_etc_localtime(monkeypatch, tmp_path))
    monkeypatch.setenv("TZ", tz.format(file=_system_zone_file("America/New_York")))
    assert _offsets(ps.get_local_zoneinfo()) == NEW_YORK_OFFSETS
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("tz", ["", ":"], ids=["empty", "a colon alone"])
def test_a_tz_set_but_empty_is_utc_as_the_c_library_reads_it(monkeypatch, capsys, tmp_path, tz: str):
    # The C library, and so upstream's localtime(), reads a TZ that is set but empty, or ':' alone, as UTC (confirmed
    # against real Perl); 0.1.37 used the system's zone, after a warning for ':'
    shutil.copyfile(_system_zone_file("America/New_York"), _move_etc_localtime(monkeypatch, tmp_path))
    monkeypatch.setenv("TZ", tz)
    assert _offsets(ps.get_local_zoneinfo()) == UTC_OFFSETS
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("rule", ["IST-1GMT0,M10.5.0,M3.5.0/1", "EST5EDT,M3.2.0,M11.1.0", "AEST-10AEDT,M10.1.0,M4.1.0/3",
                                  "<+0330>-3:30", "<-03>3", "XST5XDT4,J60,J300"])
def test_a_posix_rule_in_tz_is_applied_as_the_c_library_applies_it(monkeypatch, capsys, tmp_path, rule: str):
    # zoneinfo reads a rule as the closing line of a zone file; 0.1.38 warned and used the system's zone (here
    # Europe/Dublin's). Checked over 2026 against this system's C library, which upstream's localtime() calls.
    shutil.copyfile(_system_zone_file("Europe/Dublin"), _move_etc_localtime(monkeypatch, tmp_path))
    monkeypatch.setenv("TZ", rule)
    zone = ps.get_local_zoneinfo()
    start = int(datetime(2026, 1, 1, tzinfo=UTC).timestamp())
    time.tzset()
    try:
        differ = [t for t in range(start, start + 366 * 86400, 7 * 3600 + 13)
                  if datetime.fromtimestamp(t, zone).timetuple()[:6] != time.localtime(t)[:6]]
    finally:
        monkeypatch.undo()
        time.tzset()
    assert differ == []
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("tz", ["Europe/Dubln", "Foo", "/nonexistent/zone", "XST24"])
def test_a_tz_naming_no_zone_or_rule_is_utc_with_a_warning(monkeypatch, capsys, tmp_path, tz: str):
    # P2: the C library, and so upstream, reads UTC. `XST24` is a rule, but its offset of a day is one glibc applies
    # and datetime can't hold.
    shutil.copyfile(_system_zone_file("America/New_York"), _move_etc_localtime(monkeypatch, tmp_path))
    monkeypatch.setenv("TZ", tz)
    assert _offsets(ps.get_local_zoneinfo()) == UTC_OFFSETS
    assert capsys.readouterr().err == (f"WARN: $TZ is set to '{tz}', which is neither a zone nor a POSIX rule pysanoid "
                                       "can read - using UTC.\n")


@pytest.mark.parametrize("what", NO_ZONE)
def test_a_tz_naming_a_file_holding_no_zone_is_utc_with_a_warning(monkeypatch, capsys, tmp_path, what: str):
    # as for /etc/localtime above; glibc then reads the path as a rule, which it isn't
    shutil.copyfile(_system_zone_file("America/New_York"), _move_etc_localtime(monkeypatch, tmp_path))
    zone = tmp_path / "zone"
    zone.write_bytes(_no_zone(what))
    monkeypatch.setenv("TZ", str(zone))
    assert _offsets(_local_zone_within()) == UTC_OFFSETS
    assert capsys.readouterr().err == (f"WARN: $TZ is set to '{zone}', which is neither a zone nor a POSIX rule "
                                       "pysanoid can read - using UTC.\n")


def test_a_zone_name_is_looked_up_under_tzdir_as_glibc_does(monkeypatch, capsys, tmp_path):
    # glibc reads $TZDIR/<name>, else /usr/share/zoneinfo/<name>. zoneinfo's own search (its TZPATH, then the tzdata
    # package), which 0.1.38 used, found neither the first name here nor missed the second.
    _move_etc_localtime(monkeypatch, tmp_path)
    tzdir = tmp_path / "tzdir"
    (tzdir / "Test").mkdir(parents=True)
    shutil.copyfile(_system_zone_file("America/New_York"), tzdir / "Test" / "Zone")
    monkeypatch.setenv("TZDIR", str(tzdir))
    monkeypatch.setenv("TZ", "Test/Zone")
    assert _offsets(ps.get_local_zoneinfo()) == NEW_YORK_OFFSETS
    monkeypatch.setenv("TZ", "Europe/Dublin")  # on every system, but not under this $TZDIR
    assert _offsets(ps.get_local_zoneinfo()) == UTC_OFFSETS
    assert capsys.readouterr().err == ("WARN: $TZ is set to 'Europe/Dublin', which is neither a zone nor a POSIX rule "
                                       "pysanoid can read - using UTC.\n")


def test_dst_transition_daily_time_has_correct_utc_offset():
    # US DST spring-forward 2026-03-08. A "yesterday 23:59" preferred time
    # computed on 2026-03-09 should carry the pre-transition UTC offset.
    now = datetime(2026, 3, 9, 10, 0, tzinfo=TZ)
    preferred = ps._last_preferred_time(now, DEFAULT_SCHEDULE_CFG, "daily")
    assert preferred.utcoffset() == datetime(2026, 3, 8, 23, 59, tzinfo=TZ).utcoffset()


def test_after_helper_correctly_accounts_for_fold():
    # Documents the actual (surprising) CPython behavior _after() exists to
    # work around: aware-datetime `>`/`-` silently ignore `fold` whenever both
    # operands share the same tzinfo object, so raw `>` gives the WRONG answer
    # for two instants that are genuinely an hour apart during a fall-back fold.
    dublin = ZoneInfo("Europe/Dublin")
    first_pass = datetime(2026, 10, 25, 1, 0, tzinfo=dublin, fold=0)   # IST
    second_pass = datetime(2026, 10, 25, 1, 0, tzinfo=dublin, fold=1)  # GMT, really 1h later
    assert second_pass.timestamp() - first_pass.timestamp() == 3600

    assert (first_pass > second_pass) is False  # the fold-blind fast path: wrong
    assert (second_pass > first_pass) is False  # ditto -- Python reports them "equal"

    assert ps._after(second_pass, first_pass) is True   # _after(): correct
    assert ps._after(first_pass, second_pass) is False


def test_plan_snapshots_no_spurious_take_during_fold_second_pass():
    # Regression test for a real bug, found by running a 2-year simulation
    # across real Dublin DST transitions: during the second pass through
    # fall-back's repeated hour (fold=1), raw datetime subtraction in
    # plan_snapshots used to compute max_age about an hour too small (since
    # `now` and `preferred` share a tzinfo object -- see test above), causing
    # a spurious extra "daily" take ~2 hours after the legitimate one instead
    # of ~24 hours later.
    config = {
        "tank/data": {
            "path": "tank/data", "autosnap": True, "process_children_only": False,
            "daily": 30, "hourly": 0, "weekly": 0, "monthly": 0, "yearly": 0, "frequently": 0,
            **DEFAULT_SCHEDULE_CFG,
        }
    }
    dublin = ZoneInfo("Europe/Dublin")
    legit_take = datetime(2026, 10, 25, 0, 0, tzinfo=dublin)               # local "00:00" IST
    second_pass = datetime(2026, 10, 25, 1, 0, tzinfo=dublin, fold=1)      # 2 real hours later
    assert second_pass.timestamp() - legit_take.timestamp() == 2 * 3600   # sanity

    by_type = {"tank/data": {"daily": {"newest": second_pass.timestamp() - legit_take.timestamp()}}}
    plan = ps.plan_snapshots(config, by_type, now=second_pass)
    assert "tank/data" not in plan


# -- preferred times across DST changes and on days some months lack ------------
# The first case of each group failed before its fix; cases marked "guard" already passed.

@pytest.mark.parametrize(("now_utc", "expected_utc"), [
    # spring-forward 2026-03-08: 03:00 and 03:15 EDT, just after the skipped hour -> 01:30 EST
    (datetime(2026, 3, 8, 7, 0, tzinfo=UTC), datetime(2026, 3, 8, 6, 30, tzinfo=UTC)),
    (datetime(2026, 3, 8, 7, 15, tzinfo=UTC), datetime(2026, 3, 8, 6, 30, tzinfo=UTC)),
    # fall-back 2026-11-01: 01:20 EST (second pass) -> 01:30 EDT (first pass); 02:00 EST -> 01:30 EST
    (datetime(2026, 11, 1, 6, 20, tzinfo=UTC), datetime(2026, 11, 1, 5, 30, tzinfo=UTC)),
    (datetime(2026, 11, 1, 7, 0, tzinfo=UTC), datetime(2026, 11, 1, 6, 30, tzinfo=UTC)),
])
def test_hourly_preferred_time_steps_back_one_real_hour_across_dst(now_utc, expected_utc):
    # A wall-clock step back landed in the skipped hour (after now: an hourly on every run) or on the
    # wrong pass through the repeated one. The spring cases match upstream (confirmed against real Perl).
    cfg = {**DEFAULT_SCHEDULE_CFG, "hourly_min": 30}
    assert ps._last_preferred_time(now_utc.astimezone(TZ), cfg, "hourly").timestamp() == expected_utc.timestamp()


@pytest.mark.parametrize(("now_utc", "cfg_over", "snap_type", "expected_utc"), [
    # Lord Howe's clocks move 30 minutes. Autumn 2026-04-05: 02:00 LHDT -> 01:30 LHST (01:30-02:00 repeats)
    (datetime(2026, 4, 4, 15, 30, tzinfo=UTC), {"hourly_min": 30}, "hourly", datetime(2026, 4, 4, 15, 0, tzinfo=UTC)),
    (datetime(2026, 4, 4, 15, 0, tzinfo=UTC), {"frequent_period": 7}, "frequently",
     datetime(2026, 4, 4, 14, 56, tzinfo=UTC)),
    # spring 2026-10-04: 02:00 LHST -> 02:30 LHDT (02:00-02:30 is skipped, so there is no 02:00)
    (datetime(2026, 10, 3, 15, 30, tzinfo=UTC), {"hourly_min": 0}, "hourly", datetime(2026, 10, 3, 14, 30, tzinfo=UTC)),
])
def test_hourly_and_frequent_times_are_exact_through_a_half_hour_clock_change(now_utc, cfg_over, snap_type, expected_utc):
    # stepping through wall-clock fields landed on the wrong minute when only part of an hour repeats or is skipped
    now = now_utc.astimezone(ZoneInfo("Australia/Lord_Howe"))
    preferred = ps._last_preferred_time(now, {**DEFAULT_SCHEDULE_CFG, **cfg_over}, snap_type)
    assert preferred.timestamp() == expected_utc.timestamp()


def test_plan_snapshots_out_of_range_hourly_min_only_loses_the_hourly():
    # guard: upstream's timelocal dies on 75, stopping every dataset's snapshots
    problems: list[str] = []
    plan = ps.plan_snapshots({"t/a": _take_cfg("t/a", hourly=24, hourly_min=75)}, {}, now=NOW_APRIL, problems=problems)
    assert plan["t/a"].types == ["daily"]
    assert len(problems) == 1 and "hourly" in problems[0]


def test_plan_snapshots_takes_no_extra_hourly_just_after_the_spring_forward_gap():
    # 03:15 EDT; the last hourly was at 01:30 EST, 45 real minutes ago: not due (upstream agrees)
    cfg = _take_cfg("t/a", daily=0, hourly=24, hourly_min=30)
    now = datetime(2026, 3, 8, 7, 15, tzinfo=UTC).astimezone(TZ)
    assert ps.plan_snapshots({"t/a": cfg}, {"t/a": {"hourly": {"newest": 45 * 60}}}, now=now) == {}


@pytest.mark.parametrize(("mday", "now", "expected"), [
    (31, datetime(2026, 5, 15, 12, 0, tzinfo=TZ), datetime(2026, 3, 31, 0, 0, tzinfo=TZ)),  # April has no 31st
    (30, datetime(2026, 3, 15, 12, 0, tzinfo=TZ), datetime(2026, 1, 30, 0, 0, tzinfo=TZ)),  # nor February a 30th
    (29, datetime(2026, 3, 15, 12, 0, tzinfo=TZ), datetime(2026, 1, 29, 0, 0, tzinfo=TZ)),
    (29, datetime(2028, 3, 15, 12, 0, tzinfo=TZ), datetime(2028, 2, 29, 0, 0, tzinfo=TZ)),  # guard: leap year
])
def test_monthly_preferred_time_steps_back_over_months_without_that_day(mday, now, expected):
    # Rolling back into a month that lacks the day raised, so each run until the day came round logged an
    # ERROR and exited non-zero although nothing was due.
    cfg = {**DEFAULT_SCHEDULE_CFG, "monthly_mday": mday}
    assert ps._last_preferred_time(now, cfg, "monthly") == expected


def test_plan_snapshots_monthly_mday_31_before_the_31st_is_not_a_problem():
    config = {"t/a": _take_cfg("t/a", daily=0, monthly=6, monthly_mday=31)}
    problems: list[str] = []
    by_type = {"t/a": {"monthly": {"newest": 45 * 86400}}}  # taken on 31 March
    plan = ps.plan_snapshots(config, by_type, now=datetime(2026, 5, 15, 12, 0, tzinfo=TZ), problems=problems)
    assert problems == []
    assert plan == {}


def test_yearly_feb_29_steps_back_to_the_last_leap_year():
    cfg = {**DEFAULT_SCHEDULE_CFG, "yearly_mon": 2, "yearly_mday": 29}
    now = datetime(2028, 1, 15, 12, 0, tzinfo=TZ)
    assert ps._last_preferred_time(now, cfg, "yearly") == datetime(2024, 2, 29, 0, 0, tzinfo=TZ)


def test_yearly_feb_29_in_a_year_without_it_still_raises():
    # guard: like monthly_mday=31 in a 30-day month, the type can't be taken this year (reported by plan_snapshots)
    cfg = {**DEFAULT_SCHEDULE_CFG, "yearly_mon": 2, "yearly_mday": 29}
    with pytest.raises(ValueError):
        ps._last_preferred_time(datetime(2026, 6, 1, 12, 0, tzinfo=TZ), cfg, "yearly")


@pytest.mark.parametrize(("wday", "now", "expected"), [
    (14, datetime(2026, 7, 19, 12, 0, tzinfo=TZ), datetime(2026, 7, 12, 23, 30, tzinfo=TZ)),  # Sunday noon
    (20, datetime(2026, 7, 15, 12, 0, tzinfo=TZ), datetime(2026, 7, 11, 23, 30, tzinfo=TZ)),  # Wednesday -> Saturday
    (7, datetime(2026, 7, 19, 12, 0, tzinfo=TZ), datetime(2026, 7, 12, 23, 30, tzinfo=TZ)),  # guard: 7 is Sunday
    (-1, datetime(2026, 7, 15, 12, 0, tzinfo=TZ), datetime(2026, 7, 11, 23, 30, tzinfo=TZ)),  # guard: as upstream
    (-3, datetime(2026, 7, 17, 12, 0, tzinfo=TZ), datetime(2026, 7, 16, 23, 30, tzinfo=TZ)),  # Friday -> Thursday
    (-7, datetime(2026, 7, 15, 12, 0, tzinfo=TZ), datetime(2026, 7, 12, 23, 30, tzinfo=TZ)),  # -7 is Sunday
    (-1, datetime(2026, 7, 18, 23, 45, tzinfo=TZ), datetime(2026, 7, 18, 23, 30, tzinfo=TZ)),  # Saturday night
    (-8, datetime(2026, 7, 18, 23, 45, tzinfo=TZ), datetime(2026, 7, 18, 23, 30, tzinfo=TZ)),  # unlike upstream (DEV-031)
    (-15, datetime(2026, 7, 15, 12, 0, tzinfo=TZ), datetime(2026, 7, 11, 23, 30, tzinfo=TZ)),  # unlike upstream (DEV-031)
])
def test_weekly_wday_is_read_modulo_7_and_never_lands_in_the_future(wday, now, expected):
    # Stepping back a single week left 14 and above in the future, so a weekly was due on every run.
    # -1 to -7 wrap as upstream's do; unwrapped, they fall a week early from the chosen day to Saturday.
    cfg = {**DEFAULT_SCHEDULE_CFG, "weekly_wday": wday}
    assert ps._last_preferred_time(now, cfg, "weekly") == expected


def test_negative_frequent_period_slices_like_its_positive_size():
    # Parity: upstream's int($min / $period) * $period gives -15 the slices of 15 (confirmed against real Perl)
    now = datetime(2026, 7, 15, 12, 20, tzinfo=TZ)
    cfg = {**DEFAULT_SCHEDULE_CFG, "frequent_period": -15}
    assert ps._last_preferred_time(now, cfg, "frequently") == datetime(2026, 7, 15, 12, 15, tzinfo=TZ)


@pytest.mark.parametrize("value", ["1_5", "١٥", "１５"])
def test_cfg_int_refuses_what_perl_and_python_read_differently(value):
    # int() reads all three as 15; Perl reads "1_5" as 1 and other scripts' digits as 0
    with pytest.raises(ps.SanoidError, match="'k'"):
        ps._cfg_int({"k": value}, "k", 0)


@pytest.mark.parametrize(("value", "expected"), [("+5", 5), ("-5", -5), ("05", 5), (" 7 ", 7), ("15 # min", 15)])
def test_cfg_int_still_accepts_what_perl_reads_the_same(value, expected):
    # guard
    assert ps._cfg_int({"k": value}, "k", 0) == expected


def test_plan_snapshots_reads_an_underscored_schedule_value_as_upstream_does():
    # upstream reads "1_5" as 1 and takes its hourlies at :01; refusing it stopped them (int() read 15)
    problems: list[str] = []
    plan = ps.plan_snapshots({"t/a": _take_cfg("t/a", daily=0, hourly=24, hourly_min="1_5")}, {},
                             now=NOW_APRIL, problems=problems)
    assert plan["t/a"].types == ["hourly"]
    assert len(problems) == 1 and "hourly_min" in problems[0] and problems[0].endswith("reading it as 1")


@pytest.mark.parametrize(("snap_type", "over", "expected"), [
    ("hourly", {"hourly_min": "1_5"}, datetime(2026, 7, 15, 12, 1, tzinfo=TZ)),
    ("hourly", {"hourly_min": "7.5"}, datetime(2026, 7, 15, 12, 7, tzinfo=TZ)),  # the fraction is dropped
    ("hourly", {"hourly_min": "30 min"}, datetime(2026, 7, 15, 11, 30, tzinfo=TZ)),
    ("daily", {"daily_hour": "1_1"}, datetime(2026, 7, 15, 1, 59, tzinfo=TZ)),
    ("weekly", {"weekly_wday": "5th"}, datetime(2026, 7, 10, 23, 30, tzinfo=TZ)),
    ("monthly", {"monthly_mday": "15th"}, datetime(2026, 7, 15, 0, 0, tzinfo=TZ)),
    ("yearly", {"yearly_mon": "3rd"}, datetime(2026, 3, 1, 0, 0, tzinfo=TZ)),
])
def test_last_preferred_time_reads_a_malformed_setting_as_upstream_does(snap_type, over, expected, capsys):
    now = datetime(2026, 7, 15, 12, 20, tzinfo=TZ)
    problems: list[str] = []
    assert ps._last_preferred_time(now, {**DEFAULT_SCHEDULE_CFG, **over}, snap_type, problems) == expected
    assert len(problems) == 1 and next(iter(over)) in problems[0]
    assert capsys.readouterr().err.startswith("ERROR: ")


@pytest.mark.parametrize(("snap_type", "over", "expected"), [
    ("daily", {"daily_hour": ""}, datetime(2026, 7, 15, 0, 59, tzinfo=TZ)),  # the default read 23:59
    ("daily", {"daily_min": "# unset"}, datetime(2026, 7, 14, 23, 0, tzinfo=TZ)),
    ("weekly", {"weekly_wday": ""}, datetime(2026, 7, 12, 23, 30, tzinfo=TZ)),  # Sunday; the default read Monday
    ("hourly", {"hourly_min": ""}, datetime(2026, 7, 15, 12, 0, tzinfo=TZ)),  # same time as the default, reported now
])
def test_last_preferred_time_reads_a_blank_setting_as_upstream_does(snap_type, over, expected):
    # upstream reads a blank or comment-only value as 0 (confirmed against real Perl)
    problems: list[str] = []
    now = datetime(2026, 7, 15, 12, 20, tzinfo=TZ)
    assert ps._last_preferred_time(now, {**DEFAULT_SCHEDULE_CFG, **over}, snap_type, problems) == expected
    assert len(problems) == 1 and next(iter(over)) in problems[0] and problems[0].endswith("reading it as 0")


@pytest.mark.parametrize(("snap_type", "key"), [("monthly", "monthly_mday"), ("yearly", "yearly_mon"), ("yearly", "yearly_mday")])
def test_plan_snapshots_a_blank_day_or_month_costs_only_its_type(snap_type, key):
    # upstream reads 0 and dies for every dataset; read as 0 here it costs this type, where the default took it
    problems: list[str] = []
    plan = ps.plan_snapshots({"t/a": _take_cfg("t/a", **{snap_type: 1, key: ""})}, {}, now=NOW_APRIL, problems=problems)
    assert plan["t/a"].types == ["daily"]
    assert len(problems) == 2 and key in problems[0]
    assert problems[1].startswith(f"not taking {snap_type} snapshots of 't/a' this run")


def test_last_preferred_time_still_reads_a_blank_frequent_period_as_15():
    # guard: frequent_period keeps its own policy (DEV-032), and is not reported
    problems: list[str] = []
    cfg = {**DEFAULT_SCHEDULE_CFG, "frequent_period": ""}
    now = datetime(2026, 7, 15, 12, 20, tzinfo=TZ)
    assert ps._last_preferred_time(now, cfg, "frequently", problems) == datetime(2026, 7, 15, 12, 15, tzinfo=TZ)
    assert problems == []


def test_last_preferred_time_still_refuses_a_malformed_frequent_period():
    # guard: read as upstream reads it, "1_5" would take a frequent snapshot every minute
    cfg = {**DEFAULT_SCHEDULE_CFG, "frequent_period": "1_5"}
    with pytest.raises(ps.SanoidError, match="frequent_period"):
        ps._last_preferred_time(datetime(2026, 7, 15, 12, 20, tzinfo=TZ), cfg, "frequently", [])


@pytest.mark.parametrize("value", ["inf", "nan"])
def test_plan_snapshots_does_not_take_a_type_whose_setting_reads_as_no_number(value):
    # guard: upstream's timelocal dies on Inf, and on NaN it never takes the type
    problems: list[str] = []
    plan = ps.plan_snapshots({"t/a": _take_cfg("t/a", daily=0, hourly=24, hourly_min=value)}, {},
                             now=NOW_APRIL, problems=problems)
    assert plan == {}
    assert len(problems) == 1 and problems[0].startswith("not taking hourly snapshots of 't/a' this run")


@pytest.mark.parametrize("value", ["99999999999999999999", "1e30"])
def test_plan_snapshots_a_huge_schedule_value_costs_only_its_type(value):
    # datetime() raised OverflowError for it, out of planning: no dataset was snapshotted
    problems: list[str] = []
    config = {"t/a": _take_cfg("t/a", monthly=6, monthly_mday=value), "t/b": _take_cfg("t/b")}
    plan = ps.plan_snapshots(config, {}, now=NOW_APRIL, problems=problems)
    assert plan["t/a"].types == ["daily"] and plan["t/b"].types == ["daily"]
    assert problems and problems[-1].startswith("not taking monthly snapshots of 't/a' this run")


@pytest.mark.parametrize(("value", "reading"), [("3o", "3"), ("30 days", "30"), ("+2", "2"), ("1_0", "1"),
                                                ("1e1", "10"), (".5", "0.5")])
def test_plan_snapshots_takes_a_malformed_count_upstream_reads_as_a_positive_number(value, reading):
    # upstream's `> 0` gate reads "3o" as 3 and takes the type; refusing it stopped that tier until fixed
    problems: list[str] = []
    plan = ps.plan_snapshots({"t/a": _take_cfg("t/a", daily=value)}, {}, now=NOW_APRIL, problems=problems)
    assert plan["t/a"].types == ["daily"]
    assert len(problems) == 1 and "'daily'" in problems[0] and problems[0].endswith(f"reading it as {reading}")


@pytest.mark.parametrize("value", ["", "-5", "abc", "nan", "0.0", "# off"])
def test_plan_snapshots_still_refuses_a_count_upstream_reads_as_zero_or_less(value):
    # guard: upstream takes nothing for these either
    problems: list[str] = []
    plan = ps.plan_snapshots({"t/a": _take_cfg("t/a", daily=value)}, {}, now=NOW_APRIL, problems=problems)
    assert plan == {}
    assert len(problems) == 1 and problems[0].startswith("not taking daily snapshots of 't/a' this run")


@pytest.mark.parametrize(("text", "expected"), [
    ("3o", 3.0), ("30 days", 30.0), (" \t\n\r\f\v12", 12.0), ("+5", 5.0), ("1_000", 1.0), ("007", 7.0),
    (".5", 0.5), ("5.", 5.0), (".", 0.0), ("1e3", 1000.0), ("1e", 1.0), ("1e+2x", 100.0), ("1..5", 1.0),
    ("0x1A", 0.0), ("0b11", 0.0), ("abc", 0.0), ("", 0.0), ("#5", 0.0), ("\xa05", 0.0), ("٣", 0.0),
    ("+-5", -5.0), ("--5", 5.0), ("- 5", -5.0), ("-- 5", 0.0), ("- 0x1A", -26.0),
    ("inf", math.inf), ("information", math.inf), ("in", 0.0), ("-inf", -math.inf), ("--inf", math.inf),
    ("-+inf", -math.inf), ("- inf", -math.inf), ("1.#INF", math.inf), ("1e999", math.inf),
])
def test_perl_number_reads_a_string_as_perl_does(text, expected):
    # expected: what perl 5.34 prints for `0 + $string`
    assert ps._perl_number(text) == expected


@pytest.mark.parametrize("text", ["nan", "NaN", "nanq", "qnan", "1.#IND", "1.#QNAN", "+nan"])
def test_perl_number_reads_nan_spellings_as_nan(text):
    assert math.isnan(ps._perl_number(text))


def test_prune_refuses_an_underscored_frequent_period(tmp_path, monkeypatch):
    case = _prune_case("tank/a", 3, frequently=2, frequent_period="1_5")
    destroyed = _record_destroys(monkeypatch)
    with pytest.raises(ps.SanoidError, match="frequent_period"):
        _run_prune(tmp_path, [case])
    assert destroyed == []


def test_plan_snapshots_wants_daily_when_none_exist():
    config = {
        "tank/data": {
            "path": "tank/data", "autosnap": True, "process_children_only": False,
            "daily": 90, "hourly": 0, "weekly": 0, "monthly": 0, "yearly": 0, "frequently": 0,
            **DEFAULT_SCHEDULE_CFG,
        }
    }
    now = datetime(2026, 8, 22, 10, 0, tzinfo=TZ)
    plan = ps.plan_snapshots(config, snaps_by_type={}, now=now)
    assert "tank/data" in plan
    assert plan["tank/data"].types == ["daily"]


def test_plan_snapshots_skips_type_when_recent_snapshot_exists():
    config = {
        "tank/data": {
            "path": "tank/data", "autosnap": True, "process_children_only": False,
            "daily": 90, "hourly": 0, "weekly": 0, "monthly": 0, "yearly": 0, "frequently": 0,
            **DEFAULT_SCHEDULE_CFG,
        }
    }
    now = datetime(2026, 8, 22, 10, 0, tzinfo=TZ)
    by_type = {"tank/data": {"daily": {"newest": 60}}}  # 60s old, well within window
    plan = ps.plan_snapshots(config, by_type, now=now)
    assert "tank/data" not in plan


# ---------------------------------------------------------------------------
# pruning
# ---------------------------------------------------------------------------

def test_prune_keeps_minimum_count(tmp_path):
    now = time.time()
    day = 86400
    names = [f"autosnap_d{i}_daily" for i in range(5)]
    snaps = {
        "tank/data": {
            n: ps.Snapshot("tank/data", n, "daily", int(now - (5 - i) * 40 * day))
            for i, n in enumerate(names)
        }
    }
    by_type = {
        "tank/data": {
            "daily": {"sorted": names, "numsnaps": 5, "newest": 4 * 40 * day, "newestname": names[-1]}
        }
    }
    config = {
        "tank/data": {"path": "tank/data", "autoprune": True, "process_children_only": False,
                       "daily": 2, "prune_defer": 0}
    }
    pruned = ps.prune_snapshots(
        config, snaps, by_type, tmp_path / "cache", tmp_path / "run", readonly=True, verbose=False
    )
    # readonly=True still selects victims but shouldn't shell out; with daily=2
    # kept, all snapshots are older than maxage, so it should prune down to 2
    assert len(pruned) == 0  # readonly mode never calls zfs destroy, so nothing lands in `pruned`


def test_prune_selection_logic_directly():
    now = time.time()
    day = 86400
    # 5 dailies, oldest first, ages 200d..1d old; policy daily=2 (keep >=2 dailies,
    # and don't keep any older than 2*1day)
    names = [f"s{i}" for i in range(5)]
    ages_days = [200, 150, 100, 50, 1]  # s4 is recent
    pairs = [(n, now - a * day) for n, a in zip(names, ages_days)]
    cfg = {"daily": 2}
    to_prune = ps.select_prune_candidates(cfg, "daily", pairs, now)
    assert to_prune == ["s0", "s1", "s2"]  # oldest 3 pruned, 2 newest (s3, s4) kept


# ---------------------------------------------------------------------------
# pruning: incremental cache flush (2026-09-16 audit finding -- prune_snapshots
# must flush destroyed snapshots out of the cache after every batch, not only
# once at the very end, or a mid-run crash leaves the cache overcounting a
# type's snapshots for up to cache_ttl, which can cause a *real*, still-wanted
# snapshot to be over-pruned one run early. See P20 in DEVIATIONS.md.
# ---------------------------------------------------------------------------

def _write_cache(cache_dir: Path, lines: list[str]) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "snapshots.txt").write_text("\n".join(lines) + "\n")


def _read_cache(cache_dir: Path) -> list[str]:
    return (cache_dir / "snapshots.txt").read_text().splitlines()


def test_prune_flushes_cache_incrementally_per_batch(tmp_path, monkeypatch):
    now = time.time()
    day = 86400
    names = [f"autosnap_d{i}_daily" for i in range(3)]
    snaps = {
        "tank/data": {
            n: ps.Snapshot("tank/data", n, "daily", int(now - (3 - i) * 40 * day))
            for i, n in enumerate(names)
        }
    }
    by_type = {
        "tank/data": {"daily": {"sorted": names, "numsnaps": 3, "newest": 2 * 40 * day, "newestname": names[-1]}}
    }
    config = {
        "tank/data": {"path": "tank/data", "autoprune": True, "process_children_only": False,
                       "daily": 1, "prune_defer": 0}
    }
    cache_dir = tmp_path / "cache"
    run_dir = tmp_path / "run"
    _write_cache(cache_dir, [f"tank/data@{n}\tcreation\t{snaps['tank/data'][n].ctime}\t-" for n in names])

    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    pruned = ps.prune_snapshots(config, snaps, by_type, cache_dir, run_dir, readonly=False, verbose=False)

    assert len(pruned) == 2  # daily=1 kept, oldest 2 of 3 pruned
    # the key assertion: the on-disk cache already reflects the prune with no
    # separate remove_cached_snapshots call from the caller -- proves
    # prune_snapshots flushes internally rather than only returning a list
    # for do_prune() to flush once, later.
    cache_lines = _read_cache(cache_dir)
    remaining_names = {line.split("@", 1)[1].split("\t", 1)[0] for line in cache_lines}
    assert remaining_names == {names[-1]}  # only the kept (newest) snapshot remains cached


def test_prune_survives_interruption_after_first_batch(tmp_path, monkeypatch):
    """Simulates a crash between two prune batches (e.g. OOM-killed mid-run).

    A real SIGKILL can't be simulated from inside the process it kills -- there's
    no code path left to run afterward to make assertions. The closest faithful
    proxy is what's done here: raise partway through a second batch, and check
    that whatever the *first* batch already did is durably on disk. That's the
    actual property being tested: each batch's cache update is committed
    independently, not deferred as one all-or-nothing step for the whole run
    that a crash could prevent from ever executing.
    """
    now = time.time()
    day = 86400

    def make_section(path: str):
        tag = path.replace("/", "_")
        names = [f"autosnap_{tag}_{i}_daily" for i in range(2)]
        snaps_for_path = {n: ps.Snapshot(path, n, "daily", int(now - (2 - i) * 40 * day)) for i, n in enumerate(names)}
        by_type_for_path = {"daily": {"sorted": names, "numsnaps": 2, "newest": 40 * day, "newestname": names[-1]}}
        return names, snaps_for_path, by_type_for_path

    names_a, snaps_a, by_type_a = make_section("tank/a")
    names_b, snaps_b, by_type_b = make_section("tank/b")

    # dict insertion order controls processing order: tank/a's batch completes
    # (destroy + internal incremental flush) before tank/b's ever starts.
    config = {
        "tank/a": {"path": "tank/a", "autoprune": True, "process_children_only": False, "daily": 1, "prune_defer": 0},
        "tank/b": {"path": "tank/b", "autoprune": True, "process_children_only": False, "daily": 1, "prune_defer": 0},
    }
    snaps = {"tank/a": snaps_a, "tank/b": snaps_b}
    by_type = {"tank/a": by_type_a, "tank/b": by_type_b}

    cache_dir = tmp_path / "cache"
    run_dir = tmp_path / "run"
    _write_cache(cache_dir, (
        [f"tank/a@{n}\tcreation\t{snaps_a[n].ctime}\t-" for n in names_a]
        + [f"tank/b@{n}\tcreation\t{snaps_b[n].ctime}\t-" for n in names_b]
    ))

    class SimulatedCrash(Exception):
        pass

    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        if cmd[:2] == [ps.ZFS, "destroy"] and "tank/b@" in cmd[-1]:
            raise SimulatedCrash("simulated kill mid-run, before tank/b's batch finishes")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SimulatedCrash):
        ps.prune_snapshots(config, snaps, by_type, cache_dir, run_dir, readonly=False, verbose=False)

    cache_lines = _read_cache(cache_dir)
    remaining = {line.split("\t", 1)[0] for line in cache_lines}
    # tank/a's batch fully completed before the simulated crash on tank/b --
    # its cache entry must already be gone even though the run never reached
    # its normal end (and the caller's final blocking flush never ran).
    assert f"tank/a@{names_a[0]}" not in remaining  # pruned + already flushed
    assert f"tank/a@{names_a[1]}" in remaining       # kept (daily=1)
    # tank/b was never actually destroyed (crashed first) -- both of its
    # snapshots must still be present. Nothing was lost, only deferred.
    assert f"tank/b@{names_b[0]}" in remaining
    assert f"tank/b@{names_b[1]}" in remaining


# ---------------------------------------------------------------------------
# pruning: strict validation of the values that gate a destructive decision
# (2026-09-21 Module 1 re-audit; see _cfg_count / _preflight_prune_config).
# The "reject"/"fatal"/"preflight"/"stderr" tests below were confirmed to FAIL
# against 0.1.9 and pass after the fix. The rest of this block are parity
# locks: they already passed before the fix and pin behavior that matches
# upstream, so tightening the validation can't quietly erode it.
# ---------------------------------------------------------------------------

def _prune_case(path, count=3, **cfg_over):
    """`count` dailies for `path`, all >= 40 days old (far past any retention window
    used in these tests), plus a matching section config (daily=1 unless overridden).
    Returns (names, snaps, by_type, cfg)."""
    now = time.time()
    day = 86400
    tag = path.replace("/", "_")
    names = [f"autosnap_{tag}_{i}_daily" for i in range(count)]
    snaps = {n: ps.Snapshot(path, n, "daily", int(now - (count - i) * 40 * day)) for i, n in enumerate(names)}
    by_type = {"daily": {"sorted": names, "numsnaps": count, "newest": 40 * day, "newestname": names[-1]}}
    cfg = {"path": path, "autoprune": True, "process_children_only": False, "daily": 1, "prune_defer": 0,
           "script_timeout": "5"}
    cfg.update(cfg_over)
    return names, snaps, by_type, cfg


def _run_prune(tmp_path, cases, **kwargs):
    """Runs prune_snapshots over `cases` (from _prune_case), in order, against a real
    on-disk cache that lists every snapshot, so the incremental cache flush has
    something to operate on."""
    cache_dir = tmp_path / "cache"
    _write_cache(cache_dir, [
        f"{cfg['path']}@{n}\tcreation\t{s[n].ctime}\t-" for names, s, _, cfg in cases for n in names
    ])
    config = {cfg["path"]: cfg for _, _, _, cfg in cases}
    snaps = {cfg["path"]: s for _, s, _, cfg in cases}
    by_type = {cfg["path"]: b for _, _, b, cfg in cases}
    return ps.prune_snapshots(config, snaps, by_type, cache_dir, tmp_path / "run", **kwargs)


def _record_destroys(monkeypatch, fail_for=()):
    """Patches subprocess.run so no real zfs is touched; returns the list that every
    `zfs destroy <snapshot>` target is appended to. Targets in `fail_for` fail."""
    destroyed = []

    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        if cmd[:2] == [ps.ZFS, "destroy"]:
            destroyed.append(cmd[-1])
            if cmd[-1] in fail_for:
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"cannot destroy: dataset is busy\n")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return destroyed


def _fake_zpool(monkeypatch, stdout="", stderr="", returncode=0):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, returncode, stdout=stdout.encode(), stderr=stderr.encode()))


# -- _cfg_count: what counts as a valid retention count / prune_defer ---------

@pytest.mark.parametrize("value", ["0", "7", "80", "00", 5, 0])
def test_cfg_count_accepts_plain_whole_numbers(value):
    assert ps._cfg_count({"k": value}, "k", 99) == int(value)


@pytest.mark.parametrize("value", ["", "-5", "+2", "1_0", " 80", "80\n", "٨٠", "7.5", "abc"])
def test_cfg_count_rejects_blank_negative_and_permissive_forms(value):
    with pytest.raises(ps.SanoidError, match="'k'"):
        ps._cfg_count({"k": value}, "k", 99, "tank/x")


def test_cfg_count_absent_key_returns_default():
    assert ps._cfg_count({}, "daily", 3) == 3


# -- F1: a blank prune_defer must be fatal (upstream dies), not "never defer" --

def test_check_prune_defer_blank_limit_is_fatal_not_disabled():
    # 0.1.9 mapped "" -> 0 ("never defer"), silently switching off the guard the
    # operator had configured. Upstream: check_capacity_limit("") fails -> die.
    with pytest.raises(ps.SanoidError, match="prune_defer"):
        ps.check_prune_defer({"path": "tank/x", "prune_defer": ""}, {}, "tank")


def test_blank_prune_defer_in_config_file_aborts_prune(tmp_path):
    confdir = write_confdir(tmp_path, """
[tank/data]
    prune_defer =
""")
    config = ps.load_config(confdir, make_zfs_client(tmp_path))
    assert config["tank/data"]["prune_defer"] == ""  # the blank really does reach the prune path
    with pytest.raises(ps.SanoidError, match="prune_defer"):
        ps.prune_snapshots(config, {}, {}, tmp_path / "cache", tmp_path / "run")


# -- prune_defer semantics (parity locks) --------------------------------------

def test_check_prune_defer_boundaries():
    # defers while pool capacity <= limit; prunes only once it strictly exceeds it
    cfg = {"path": "tank/x", "prune_defer": 80}
    assert ps.check_prune_defer(cfg, {"tank": 79}, "tank") is True
    assert ps.check_prune_defer(cfg, {"tank": 80}, "tank") is True
    assert ps.check_prune_defer(cfg, {"tank": 81}, "tank") is False


@pytest.mark.parametrize("zero", [0, "0"])
def test_check_prune_defer_zero_never_queries_the_pool(monkeypatch, zero):
    def boom(pool):
        raise AssertionError(f"zpool must not be queried for prune_defer = 0 (asked about {pool})")

    monkeypatch.setattr(ps, "get_zpool_capacity", boom)
    assert ps.check_prune_defer({"path": "tank/x", "prune_defer": zero}, {}, "tank") is False


def test_check_prune_defer_rejects_limit_over_100():
    with pytest.raises(ps.SanoidError, match="prune_defer"):
        ps.check_prune_defer({"path": "tank/x", "prune_defer": 101}, {"tank": 50}, "tank")


def test_check_prune_defer_queries_each_pool_once(monkeypatch):
    asked = []

    def fake_capacity(pool):
        asked.append(pool)
        return 50

    monkeypatch.setattr(ps, "get_zpool_capacity", fake_capacity)
    cache = {}
    cfg = {"path": "tank/x", "prune_defer": 80}
    ps.check_prune_defer(cfg, cache, "tank")
    ps.check_prune_defer(cfg, cache, "tank")
    assert asked == ["tank"]


def test_prune_snapshots_defers_until_pool_capacity_exceeds_limit(tmp_path, monkeypatch, capsys):
    case = _prune_case("tank/data", 3, prune_defer=80)
    names = case[0]
    destroyed = _record_destroys(monkeypatch)
    asked = []
    capacity = {"tank": 80}

    def fake_capacity(pool):
        asked.append(pool)
        return capacity[pool]

    monkeypatch.setattr(ps, "get_zpool_capacity", fake_capacity)

    # exactly at the limit: the whole section is deferred, nothing is destroyed
    assert _run_prune(tmp_path, [case], verbose=True) == []
    assert destroyed == []
    assert "INFO: deferring snapshot pruning (tank/data)..." in capsys.readouterr().out

    # one point over: prunes (daily=1 keeps only the newest of 3)
    capacity["tank"] = 81
    assert _run_prune(tmp_path, [case]) == [f"tank/data@{names[0]}", f"tank/data@{names[1]}"]
    assert asked == ["tank", "tank"]  # the pool comes from the section name


# -- prune_defer: upstream's `$limit eq 0`, and a missing key ----------------------

@pytest.mark.parametrize(("value", "capacity", "deferred"), [
    ("00", 0, True),  # not the string "0", so the pool is asked, and 0 >= 0% defers
    ("000", 0, True),
    ("00", 50, False),  # asked, and 0 < 50% prunes
])
def test_check_prune_defer_skips_the_pool_only_for_the_string_0(monkeypatch, value, capacity, deferred):
    # Parity: upstream's early return is `$limit eq 0`, a string comparison (confirmed against real Perl)
    asked = []

    def fake_capacity(pool):
        asked.append(pool)
        return capacity

    monkeypatch.setattr(ps, "get_zpool_capacity", fake_capacity)
    assert ps.check_prune_defer({"path": "tank/x", "prune_defer": value}, {}, "tank") is deferred
    assert asked == ["tank"]


def test_check_prune_defer_00_fails_when_the_pool_cannot_be_queried(monkeypatch):
    # [b] with path = tank/a: upstream asks zpool about "b" and dies, where pysanoid pruned without asking
    def no_such_pool(pool):
        raise ps.SanoidError(f"'zpool list -H -o cap {pool}' returned invalid capacity value (''): no such pool")

    monkeypatch.setattr(ps, "get_zpool_capacity", no_such_pool)
    with pytest.raises(ps.SanoidError, match="zpool list"):
        ps.check_prune_defer({"path": "tank/a", "prune_defer": "00"}, {}, "b")


def test_prune_snapshots_defers_prune_defer_00_on_an_empty_pool(tmp_path, monkeypatch):
    # upstream destroys nothing here (confirmed against real Perl)
    case = _prune_case("tank/data", 3, prune_defer="00")
    destroyed = _record_destroys(monkeypatch)
    monkeypatch.setattr(ps, "get_zpool_capacity", lambda pool: 0)
    assert _run_prune(tmp_path, [case]) == []
    assert destroyed == []


def test_check_prune_defer_missing_key_is_fatal():
    # Parity: upstream's check_capacity_limit(undef) fails, so it dies ("invalid prune_defer limit!")
    with pytest.raises(ps.SanoidError, match="prune_defer"):
        ps.check_prune_defer({"path": "tank/x"}, {}, "tank")


def test_a_defaults_file_without_prune_defer_aborts_prune(tmp_path):
    # the only way the key goes missing; upstream dies on every prune run (confirmed against real Perl)
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n")
    defaults = confdir / "sanoid.defaults.conf"
    defaults.write_text("".join(ln for ln in defaults.read_text().splitlines(keepends=True) if not ln.startswith("prune_defer")))
    config = ps.load_config(confdir, make_zfs_client(tmp_path))
    assert "prune_defer" not in config["tank/data"]
    with pytest.raises(ps.SanoidError, match="prune_defer"):
        ps.prune_snapshots(config, {}, {}, tmp_path / "cache", tmp_path / "run")


def test_a_missing_prune_defer_stops_prune_before_any_destroy(tmp_path, monkeypatch):
    good = _prune_case("tank/a", 3)  # valid; would destroy 2 snapshots
    missing = _prune_case("tank/b", 3)
    del missing[3]["prune_defer"]
    destroyed = _record_destroys(monkeypatch)
    with pytest.raises(ps.SanoidError, match="prune_defer"):
        _run_prune(tmp_path, [good, missing])
    assert destroyed == []


# -- F2: blank / negative retention must not mean "prune the whole tier" -------

@pytest.mark.parametrize("value", ["", "-5"])
def test_select_prune_candidates_rejects_blank_and_negative_retention(value):
    # Upstream (and 0.1.9) prune EVERY snapshot of the type for both: "" numifies
    # to 0, and -5 satisfies `$n > -5` forever. `0` is a documented feature;
    # these are typos.
    now = time.time()
    pairs = [(f"s{i}", now - (50 - i) * 86400) for i in range(3)]
    with pytest.raises(ps.SanoidError, match="daily"):
        ps.select_prune_candidates({"path": "tank/x", "daily": value}, "daily", pairs, now)


def test_zero_retention_still_prunes_every_snapshot_of_that_type():
    # documented upstream feature: "set to 0 ... immediately prune any of those
    # type snapshots already present" -- must survive the tightening above
    now = time.time()
    pairs = [(f"s{i}", now - (50 - i) * 86400) for i in range(3)]
    assert ps.select_prune_candidates({"path": "tank/x", "daily": "0"}, "daily", pairs, now) == ["s0", "s1", "s2"]


# -- F3: validate everything before the first destroy (all-or-nothing) ---------

@pytest.mark.parametrize("bad", ["3o", "", "-1"])
def test_prune_preflight_aborts_before_any_destroy(tmp_path, monkeypatch, bad):
    good = _prune_case("tank/a", 3)  # valid; would destroy 2 snapshots
    broken = _prune_case("tank/b", 3, daily=bad)  # typo in the LATER section
    destroyed = _record_destroys(monkeypatch)
    with pytest.raises(ps.SanoidError):
        _run_prune(tmp_path, [good, broken])
    assert destroyed == []  # tank/a must not be left half-applied


def test_prune_preflight_ignores_sections_pruning_would_skip(tmp_path, monkeypatch):
    good = _prune_case("tank/a", 3)
    skipped = _prune_case("tank/off", 3, autoprune=False, daily="3o")  # never touched, so never validated
    destroyed = _record_destroys(monkeypatch)
    _run_prune(tmp_path, [good, skipped])
    assert destroyed == [f"tank/a@{good[0][0]}", f"tank/a@{good[0][1]}"]


# -- F5: get_zpool_capacity ----------------------------------------------------

def test_get_zpool_capacity_parses_valid_output(monkeypatch):
    _fake_zpool(monkeypatch, stdout="42%\n")
    assert ps.get_zpool_capacity("tank") == 42


@pytest.mark.parametrize("out", ["", "-\n", "42\n", "1000%\n"])
def test_get_zpool_capacity_rejects_malformed_output(monkeypatch, out):
    _fake_zpool(monkeypatch, stdout=out)
    with pytest.raises(ps.SanoidError, match="invalid capacity"):
        ps.get_zpool_capacity("tank")


def test_get_zpool_capacity_rejects_non_ascii_digits(monkeypatch):
    # Python's \d matches other scripts' digits; upstream's [0-9] does not
    _fake_zpool(monkeypatch, stdout="٤٢%\n")
    with pytest.raises(ps.SanoidError, match="invalid capacity"):
        ps.get_zpool_capacity("tank")


def test_get_zpool_capacity_failure_reports_zpool_stderr(monkeypatch, capsys):
    # zpool's own words, as it wrote them, and then upstream's
    _fake_zpool(monkeypatch, stderr="cannot open 'nope': no such pool\n", returncode=1)
    with pytest.raises(ps.SanoidError, match=r"^'zpool list -H -o cap nope' command returned invalid capacity value \(\)!$"):
        ps.get_zpool_capacity("nope")
    assert capsys.readouterr().err == "cannot open 'nope': no such pool\n"


# -- destroy / hook / skip / lock behavior (parity locks) ----------------------

def test_prune_pre_script_veto_skips_only_that_snapshot(tmp_path, monkeypatch):
    case = _prune_case("tank/data", 3, pre_pruning_script="/bin/veto", pruning_script="/bin/notify")
    names = case[0]
    destroyed = _record_destroys(monkeypatch)
    hooks = []

    def fake_hook(cfg, key, dataset, env_extra, verbose=False, problems=None):
        hooks.append((key, env_extra["SANOID_SNAPNAME"]))
        return 1 if key == "pre_pruning_script" and env_extra["SANOID_SNAPNAME"] == names[0] else 0

    monkeypatch.setattr(ps, "run_hook_script", fake_hook)
    pruned = _run_prune(tmp_path, [case])

    assert destroyed == [f"tank/data@{names[1]}"]  # names[0] vetoed; names[1] still pruned
    assert pruned == [f"tank/data@{names[1]}"]
    # the post-destroy hook fires only for a snapshot that was really destroyed
    assert ("pruning_script", names[1]) in hooks
    assert ("pruning_script", names[0]) not in hooks


def test_prune_destroy_failure_warns_continues_and_is_not_recorded(tmp_path, monkeypatch, capsys):
    case = _prune_case("tank/data", 3)
    names = case[0]
    destroyed = _record_destroys(monkeypatch, fail_for={f"tank/data@{names[0]}"})
    pruned = _run_prune(tmp_path, [case])

    assert destroyed == [f"tank/data@{names[0]}", f"tank/data@{names[1]}"]  # kept going after the failure
    assert pruned == [f"tank/data@{names[1]}"]  # only real successes are recorded
    assert "could not remove tank/data@" in capsys.readouterr().err
    # and the cache still lists the snapshot that was NOT destroyed
    remaining = {line.split("\t", 1)[0] for line in _read_cache(tmp_path / "cache")}
    assert f"tank/data@{names[0]}" in remaining
    assert f"tank/data@{names[1]}" not in remaining


def test_prune_skips_template_prefix_autoprune_off_and_children_only(tmp_path, monkeypatch):
    cases = [
        _prune_case("templatefoo/x", 3),  # upstream's /^template/ prefix quirk, kept on purpose
        _prune_case("tank/off", 3, autoprune=False),
        _prune_case("tank/kids", 3, process_children_only=True),
        _prune_case("tank/on", 3),
    ]
    destroyed = _record_destroys(monkeypatch)
    _run_prune(tmp_path, cases)
    assert {d.split("@")[0] for d in destroyed} == {"tank/on"}


def test_prune_defers_batch_when_pruning_lock_is_held(tmp_path, monkeypatch, capsys):
    case = _prune_case("tank/data", 3)
    destroyed = _record_destroys(monkeypatch)
    with ps.try_lock(tmp_path / "run", "sanoid_pruning") as (held, _):
        assert held
        pruned = _run_prune(tmp_path, [case], verbose=True)
    assert pruned == []
    assert destroyed == []
    assert "valid pruning lock held by other pysanoid process" in capsys.readouterr().out


def test_prune_carries_on_when_a_destroy_cannot_start(tmp_path, monkeypatch, capsys):
    # DEV-043: upstream's system() returns -1, so it prints "could not remove", carries on, flushes the batch and exits
    # 0 (confirmed end to end). Stopping mid-batch would leave the snapshot just destroyed in the cache, and later
    # sections unpruned.
    cases = [_prune_case("tank/a", 3), _prune_case("tank/b", 3)]
    attempts: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if cmd[:2] == [ps.ZFS, "destroy"]:
            attempts.append(cmd[-1])
            if len(attempts) == 2:  # the second destroy can't start
                raise _cannot_start()
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    problems: list[str] = []
    pruned = _run_prune(tmp_path, cases, problems=problems)
    (names_a, *_), (names_b, *_) = cases
    assert pruned == [f"tank/a@{names_a[0]}", f"tank/b@{names_b[0]}", f"tank/b@{names_b[1]}"]
    assert problems == []  # the snapshot that wasn't destroyed is kept: not a problem, as a failed destroy isn't
    err = capsys.readouterr().err  # why it couldn't start, then upstream's line with the -1 its system() returns
    assert err.startswith(f"could not run '{ps.ZFS} destroy tank/a@{names_a[1]}': ")
    assert err.endswith(f"\ncould not remove tank/a@{names_a[1]} : -1\n") and err.count("\n") == 2
    remaining = {line.split("\t", 1)[0] for line in _read_cache(tmp_path / "cache")}
    assert remaining == {f"tank/a@{names_a[1]}", f"tank/a@{names_a[2]}", f"tank/b@{names_b[2]}"}


def _unusable_run_dir(tmp_path: Path) -> Path:
    """A run directory no lock file can be made in, even as root: a regular file stands where it should be."""
    run_dir = tmp_path / "run"
    _ = run_dir.write_text("")
    return run_dir


def test_prune_skips_a_batch_whose_lock_file_cannot_be_made(tmp_path, monkeypatch):
    # DEV-041: upstream destroys the batch unlocked and then dies. Here it fails closed, and reports it.
    names, snaps, by_type, cfg = _prune_case("tank/data", 3)
    _write_cache(tmp_path / "cache", [f"tank/data@{n}\tcreation\t{snaps[n].ctime}\t-" for n in names])
    destroyed = _record_destroys(monkeypatch)
    problems: list[str] = []
    pruned = ps.prune_snapshots({"tank/data": cfg}, {"tank/data": snaps}, {"tank/data": by_type}, tmp_path / "cache",
                                _unusable_run_dir(tmp_path), problems=problems)
    assert pruned == [] and destroyed == []
    assert len(problems) == 1 and problems[0].startswith("not pruning the daily snapshots of 'tank/data' this run: ")


def test_a_lock_file_that_cannot_be_made_is_handed_to_the_caller(tmp_path):
    # DEV-041: the error is handed to the caller, so the run doesn't end at its first lock
    run_dir = _unusable_run_dir(tmp_path)
    with ps.try_lock(run_dir, "sanoid_pruning") as (held, error):
        assert not held and isinstance(error, OSError)
    with ps.wait_lock(run_dir, "sanoid_cacheupdate") as (held, error):
        assert not held and isinstance(error, OSError)


def test_a_lock_is_still_held_when_its_pid_cannot_be_written(tmp_path, monkeypatch):
    # The PID is only a note for a person looking; a run directory too full to take it doesn't cost the lock
    class FullFile(io.FileIO):
        def write(self, b: object, /) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

    def full_disk_open(path: Path, *_: object, **__: object) -> io.FileIO:
        return FullFile(path, "w")

    monkeypatch.setattr(ps, "open", full_disk_open, raising=False)
    (tmp_path / "run").mkdir()
    with ps.try_lock(tmp_path / "run", "sanoid_pruning") as (held, error):
        assert held and error is None
    with ps.wait_lock(tmp_path / "run", "sanoid_cacheupdate") as (held, error):
        assert held and error is None


def test_a_lock_that_cannot_be_taken_leaves_the_holders_pid_in_its_file(tmp_path):
    # flock is per open file, so a second open in this process contends as another process would
    lock_file = tmp_path / "run" / "sanoid_pruning.lock"
    with ps.try_lock(tmp_path / "run", "sanoid_pruning") as (held, _):
        assert held
        assert lock_file.read_text() == str(os.getpid())
        _ = lock_file.write_text("4242")  # as a holder in another process would have noted itself
        with ps.try_lock(tmp_path / "run", "sanoid_pruning") as (contender_held, error):
            assert not contender_held and error is None
            assert lock_file.read_text() == "4242"
        assert lock_file.read_text() == "4242"


@pytest.mark.parametrize("take", [ps.try_lock, ps.wait_lock])
def test_a_lock_file_holds_only_its_new_holders_pid(tmp_path, take):
    (tmp_path / "run").mkdir()
    lock_file = tmp_path / "run" / "sanoid_cacheupdate.lock"
    _ = lock_file.write_text("4294967295 left by an earlier holder")
    with take(tmp_path / "run", "sanoid_cacheupdate") as (held, _):
        assert held
        assert lock_file.read_text() == str(os.getpid())


# ---------------------------------------------------------------------------
# taking: a config error or a failed `zfs snapshot` must never silently stop
# protection, and must never look like success (2026-09-21 Module 6 re-audit;
# DEV-028 in DEVIATIONS.md). Baseline facts about upstream: its planning
# loop dies -- before ANY snapshot is taken, for EVERY dataset -- on
# monthly_mday=31 in a 30-day month, hourly_min=75, daily_hour=24 and
# frequent_period=0 or blank (confirmed against a real Perl interpreter); and a
# failed `zfs snapshot` only warns, so the run still exits 0 (read from sanoid's
# take_snapshots and its script-level `exit 0`, not executed).
# ---------------------------------------------------------------------------

NOW_APRIL = datetime(2026, 4, 21, 5, 37, tzinfo=TZ)  # a 30-day month


def _take_cfg(path, **over):
    cfg = {"path": path, "autosnap": True, "process_children_only": False,
           "daily": 7, "hourly": 0, "weekly": 0, "monthly": 0, "yearly": 0, "frequently": 0,
           "script_timeout": "5", **DEFAULT_SCHEDULE_CFG}
    cfg.update(over)
    return cfg


# -- planning: one bad section/type must not stop protection elsewhere ---------

def test_plan_snapshots_isolates_a_bad_section_from_good_ones(capsys):
    config = {"good/a": _take_cfg("good/a"), "bad/b": _take_cfg("bad/b", daily_hour=24)}
    plan = ps.plan_snapshots(config, {}, now=NOW_APRIL)
    assert plan["good/a"].types == ["daily"]  # still protected
    assert "bad/b" not in plan
    err = capsys.readouterr().err
    assert "ERROR:" in err and "bad/b" in err and "daily" in err


def test_plan_snapshots_records_problems_for_the_caller(capsys):
    config = {"good/a": _take_cfg("good/a"), "bad/b": _take_cfg("bad/b", daily_hour=24)}
    problems = []
    ps.plan_snapshots(config, {}, now=NOW_APRIL, problems=problems)
    assert len(problems) == 1
    assert "bad/b" in problems[0] and "daily" in problems[0]


def test_plan_snapshots_isolates_a_bad_type_within_a_section(capsys):
    # a blank retention count used to silently disable the type (as upstream does);
    # it is now reported, and the section's other types are still planned
    config = {"t/a": _take_cfg("t/a", daily="", hourly=12)}
    plan = ps.plan_snapshots(config, {}, now=NOW_APRIL)
    assert plan["t/a"].types == ["hourly"]
    assert "'daily'" in capsys.readouterr().err


def test_plan_snapshots_monthly_mday_31_in_a_30_day_month_only_loses_the_monthly(capsys):
    config = {"t/a": _take_cfg("t/a", monthly=6, monthly_mday=31)}
    problems = []
    plan = ps.plan_snapshots(config, {}, now=NOW_APRIL, problems=problems)
    assert plan["t/a"].types == ["daily"]  # upstream dies here and takes NO snapshots at all
    assert len(problems) == 1 and "monthly" in problems[0]


def test_plan_snapshots_frequent_period_zero_is_a_clear_error(capsys):
    config = {"t/a": _take_cfg("t/a", daily=0, frequently=4, frequent_period=0)}
    problems = []
    plan = ps.plan_snapshots(config, {}, now=NOW_APRIL, problems=problems)
    assert "t/a" not in plan
    assert len(problems) == 1 and "frequent_period" in problems[0]


def test_plan_snapshots_zero_retention_is_silently_disabled(capsys):
    # documented: 0 means "don't take this type" -- not an error, not a problem
    problems = []
    plan = ps.plan_snapshots({"t/a": _take_cfg("t/a", daily=0)}, {}, now=NOW_APRIL, problems=problems)
    assert plan == {}
    assert problems == []
    assert capsys.readouterr().err == ""


def test_plan_snapshots_good_config_reports_no_problems(capsys):
    problems = []
    plan = ps.plan_snapshots({"t/a": _take_cfg("t/a")}, {}, now=NOW_APRIL, problems=problems)
    assert plan["t/a"].types == ["daily"]
    assert problems == []
    assert capsys.readouterr().err == ""


# -- taking: a failed `zfs snapshot` must be reported, not just warned about ---

def _cannot_start() -> BlockingIOError:
    """What subprocess.run raises when fork fails."""
    return BlockingIOError(errno.EAGAIN, os.strerror(errno.EAGAIN))


def _fake_zfs_snapshot(monkeypatch, *responses):
    """Each `zfs snapshot` call consumes the next (returncode, stderr) response, or raises it if it is
    an exception (a command that can't start)."""
    remaining = iter(responses)
    calls = []

    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        calls.append(cmd)
        response = next(remaining)
        if isinstance(response, BaseException):
            raise response
        rc, err = response
        return subprocess.CompletedProcess(cmd, rc, stdout=b"", stderr=err.encode())

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_take_snapshots_reports_failed_snapshot_creation(monkeypatch, capsys):
    _fake_zfs_snapshot(monkeypatch, (1, "cannot create snapshot: out of space\n"))
    problems = []
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a")}, {}, now=NOW_APRIL, problems=problems)
    assert taken == {}
    assert problems == ["zfs snapshot tank/a@autosnap_2026-04-21_05:37:00_daily failed, 256"]
    assert capsys.readouterr().err == ("cannot create snapshot: out of space\n"  # zfs's words, then upstream's
                                        "CRITICAL ERROR: zfs snapshot tank/a@autosnap_2026-04-21_05:37:00_daily failed, 256\n")


def test_take_snapshots_success_reports_no_problems(monkeypatch):
    _fake_zfs_snapshot(monkeypatch, (0, ""))
    problems = []
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a")}, {}, now=NOW_APRIL, problems=problems)
    assert list(taken) == ["tank/a@autosnap_2026-04-21_05:37:00_daily"]
    assert problems == []


def test_take_snapshots_dst_retry_success_is_not_a_problem(monkeypatch):
    _fake_zfs_snapshot(monkeypatch, (1, "cannot create snapshot: dataset already exists\n"), (0, ""))
    problems = []
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a")}, {}, now=NOW_APRIL, problems=problems)
    assert list(taken) == ["tank/a@autosnap_2026-04-21_05:37:00dst_daily"]
    assert problems == []


def test_take_snapshots_dst_retry_failure_is_a_problem(monkeypatch):
    _fake_zfs_snapshot(
        monkeypatch, (1, "cannot create snapshot: dataset already exists\n"), (1, "cannot create snapshot: out of space\n")
    )
    problems = []
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a")}, {}, now=NOW_APRIL, problems=problems)
    assert taken == {}
    assert problems == ["zfs snapshot tank/a@autosnap_2026-04-21_05:37:00dst_daily failed, 256"]


def test_take_snapshots_carries_on_after_a_snapshot_that_cannot_start(monkeypatch, capsys):
    # DEV-043: once fork has failed, upstream's system() returns -1, so it warns and goes on to the next dataset
    # (confirmed against real Perl); here that comes at once. Ending the run would lose what it took from the cache.
    _fake_zfs_snapshot(monkeypatch, (0, ""), _cannot_start(), (0, ""))
    problems = []
    taken = ps.take_snapshots({p: _take_cfg(p) for p in ("tank/a", "tank/b", "tank/c")}, {}, now=NOW_APRIL,
                              problems=problems)
    assert sorted(taken) == ["tank/a@autosnap_2026-04-21_05:37:00_daily", "tank/c@autosnap_2026-04-21_05:37:00_daily"]
    assert problems == ["zfs snapshot tank/b@autosnap_2026-04-21_05:37:00_daily failed, -1"]
    err = capsys.readouterr().err  # why it couldn't start, then upstream's line with the -1 its system() returns
    assert err.startswith(f"could not run '{ps.ZFS} snapshot tank/b@autosnap_2026-04-21_05:37:00_daily': ")
    assert err.endswith("\nCRITICAL ERROR: zfs snapshot tank/b@autosnap_2026-04-21_05:37:00_daily failed, -1\n")
    assert err.count("\n") == 2


def test_take_snapshots_dst_retry_that_cannot_start_is_a_problem(monkeypatch):
    _fake_zfs_snapshot(monkeypatch, (1, "cannot create snapshot: dataset already exists\n"), _cannot_start())
    problems = []
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a")}, {}, now=NOW_APRIL, problems=problems)
    assert taken == {}
    assert len(problems) == 1 and "dst_daily" in problems[0]


# -- main(): exit status is what systemd (and `systemctl --failed`) can see ----

def _run_main(tmp_path, monkeypatch, *flags, take_problem=False):
    """Runs main() against a real config dir, with take/prune and the snapshot
    listing faked out. Returns (exit_code, order in which take/prune ran)."""
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n")
    calls = []

    def fake_take(config, snaps_by_type, *, readonly=False, verbose=False, now=None, problems=None):
        calls.append("take")
        if take_problem and problems is not None:
            problems.append("zfs snapshot tank/data@x failed: out of space")
        return {}

    def fake_prune(*args, **kwargs):
        calls.append("prune")
        return []

    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    monkeypatch.setattr(ps, "get_snapshots", lambda *args, **kwargs: {})
    monkeypatch.setattr(ps, "take_snapshots", fake_take)
    monkeypatch.setattr(ps, "prune_snapshots", fake_prune)
    rc = ps.main([
        "--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"), *flags,
    ])
    return rc, calls


@pytest.mark.parametrize("option", ["--cache-dir", "--run-dir"])
def test_a_directory_option_starting_with_whitespace_is_used_as_given(tmp_path, monkeypatch, option):
    # DEV-025: upstream's two-argument open() drops the leading space where its file tests, renames and locks keep it,
    # so it exits 2 on every run (confirmed against real Perl); pysanoid uses the path as given, everywhere
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n")
    log = _script_zfs(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    dirs = {"--cache-dir": str(tmp_path / "cache"), "--run-dir": str(tmp_path / "run")}
    dirs[option] = " " + dirs[option]
    rc = ps.main(["--configdir", str(confdir), *(x for kv in dirs.items() for x in kv), "--take-snapshots"])
    assert rc == 0
    assert "snapshot tank/data@autosnap_" in log.read_text()
    padded = tmp_path / " " / dirs[option].lstrip().lstrip("/")
    assert padded.is_dir()
    stripped = Path(dirs[option].lstrip())
    assert not stripped.exists()
    if option == "--cache-dir":
        assert (padded / "snapshots.txt").is_file()
    else:
        assert (padded / "sanoid_cacheupdate.lock").is_file()


def test_main_exits_zero_when_taking_snapshots_succeeds(tmp_path, monkeypatch):
    assert _run_main(tmp_path, monkeypatch, "--take-snapshots") == (0, ["take"])


def test_main_exits_nonzero_when_taking_a_snapshot_fails(tmp_path, monkeypatch, capsys):
    rc, calls = _run_main(tmp_path, monkeypatch, "--take-snapshots", take_problem=True)
    assert rc == 1
    assert calls == ["take"]
    assert "1 problem(s) this run" in capsys.readouterr().err


def test_main_cron_still_prunes_after_take_problems_but_exits_nonzero(tmp_path, monkeypatch):
    # pruning must never be gated on taking: on a full pool the failed snapshot IS
    # the reason pruning is needed. (The systemd units use Wants=, not Requires=,
    # for the same reason.)
    rc, calls = _run_main(tmp_path, monkeypatch, "--cron", take_problem=True)
    assert rc == 1
    assert calls == ["take", "prune"]


def test_main_takes_and_prunes_when_the_caches_cannot_be_written(tmp_path, monkeypatch, capsys):
    # A full disk under the cache directory: upstream carries on when a write fails after the file opened (confirmed
    # against real Perl); pysanoid died, so nothing was snapshotted or pruned until space was freed.
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n")
    calls = []
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    _zfs_replies(monkeypatch, (0, "tank/data@autosnap_x_daily\tcreation\t5\t-\n", ""))
    _failing_write(monkeypatch)
    monkeypatch.setattr(ps, "take_snapshots", lambda config, by_type, **kwargs: calls.append("take") or {})
    monkeypatch.setattr(ps, "prune_snapshots", lambda *args, **kwargs: calls.append("prune") or [])
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--cron"])
    assert rc == 1
    assert calls == ["take", "prune"]
    err = capsys.readouterr().err
    assert err.count("ERROR: could not update") == 2  # datasets.txt and snapshots.txt
    assert "2 problem(s) this run" in err


def _fake_zfs_pool(monkeypatch, lines: list[str]) -> tuple[list[str], list[str]]:
    """Patches subprocess.run with a small zfs: `get` lists `lines` plus every snapshot taken since, `snapshot` takes
    one (its creation is now) and `destroy` removes one. Returns (taken, destroyed)."""
    taken: list[str] = []
    destroyed: list[str] = []

    def fake_run(cmd: list[str], text: bool = False, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if any("\0" in arg for arg in cmd):
            raise ValueError("embedded null byte")  # as the real one does
        verb, name = cmd[1], cmd[-1]
        if verb == "snapshot":
            taken.append(name)
            lines.append(f"{name}\tcreation\t{int(time.time())}\t-")
        elif verb == "destroy":
            destroyed.append(name)
            lines[:] = [ln for ln in lines if ln.split("\t")[0] != name]
        out = "".join(f"{ln}\n" for ln in lines) if verb == "get" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out.encode(), stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return taken, destroyed


@pytest.mark.parametrize("unusable", ["run", "cache"])
def test_main_keeps_taking_snapshots_when_a_directory_is_unusable(tmp_path, monkeypatch, capsys, unusable):
    # DEV-039d and DEV-041: a directory that can't be made or written costs only the updates that need it. Raising at
    # startup, or at the first lock, would take no snapshot while it lasted; the real upstream script kept taking them.
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n    hourly = 0\n    monthly = 0\n")
    _ = (tmp_path / unusable).write_text("")  # a regular file where the directory should be
    taken, _ = _fake_zfs_pool(monkeypatch, [f"tank/data@autosnap_old_daily\tcreation\t{int(time.time()) - 3 * 86400}\t-"])
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    argv = ["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
            "--take-snapshots"]
    assert ps.main(argv) == 1
    assert len(taken) == 1
    assert ps.main(argv) == 1
    assert len(taken) == 1  # the next run sees it in zfs, and takes it once only
    assert "ERROR:" in capsys.readouterr().err


def test_main_prunes_by_zfs_when_the_snapshot_cache_holds_a_nul_byte(tmp_path, monkeypatch):
    # End to end against the real upstream script, a damaged line made it destroy tank/b's newest daily, leaving 2 of
    # the 3 it keeps; 0.1.33 stopped FATAL at that line and pruned nothing (confirmed). zfs knows six dailies.
    confdir = write_confdir(tmp_path, "[tank/b]\n    daily = 3\n    hourly = 0\n    monthly = 0\n")
    now = int(time.time())
    real = [f"tank/b@autosnap_d{i}_daily\tcreation\t{now - int(age * 86400)}\t-"
            for i, age in enumerate((0.2, 1, 2, 3.5, 4.5, 5.5))]
    damaged = f"tank/b@autosnap_d0_daily{chr(0) * 40}_daily\tcreation\t{now - 9 * 86400}\t-"
    (tmp_path / "cache").mkdir()
    _ = (tmp_path / "cache" / "snapshots.txt").write_text("\n".join([*real[:3], damaged, *real[3:]]) + "\n")  # fresh
    _, destroyed = _fake_zfs_pool(monkeypatch, list(real))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    assert ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                    "--prune-snapshots"]) == 0
    assert destroyed == ["tank/b@autosnap_d5_daily", "tank/b@autosnap_d4_daily", "tank/b@autosnap_d3_daily"]


def _pathlib_as_through_3_13(monkeypatch) -> None:
    """Path.exists(), is_file() and is_symlink() raising PermissionError for every path, as they do through Python 3.13
    for a path under a directory that can't be searched (3.14 returns False)."""
    def refuse(self: Path, *_: object, **__: object) -> bool:
        raise PermissionError(errno.EACCES, "Permission denied", str(self))

    for method in ("exists", "is_file", "is_symlink"):
        monkeypatch.setattr(Path, method, refuse)


def test_main_never_asks_pathlib_whether_a_file_is_there(tmp_path, monkeypatch):
    # Upstream's -e and -f read false where pathlib's checks raise through Python 3.13: on 3.11, a cache directory
    # pysanoid couldn't search stopped the run with a traceback, nothing taken or pruned, where 3.14 carried on as
    # DEV-039a says (confirmed end to end). Two --cron runs pass every check: the caches missing, then fresh.
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 3\n    hourly = 0\n    monthly = 0\n")
    now = int(time.time())
    taken, destroyed = _fake_zfs_pool(monkeypatch, [f"tank/data@autosnap_d{i}_daily\tcreation\t{now - int(age * 86400)}\t-"
                                                    for i, age in enumerate((2.5, 3.5, 4.5, 5.5))])
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    monkeypatch.delenv("TZ", raising=False)  # the zone read from /etc/localtime, not named in $TZ
    _pathlib_as_through_3_13(monkeypatch)
    argv = ["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"), "--cron"]
    assert ps.main(argv) == 0
    assert ps.main(argv) == 0
    assert len(taken) == 1
    assert destroyed == ["tank/data@autosnap_d3_daily", "tank/data@autosnap_d2_daily"]


# ---------------------------------------------------------------------------
# config recursion (2026-09-21 Module 2 re-audit). load_config was differential-
# tested against upstream's REAL `init` -- extracted verbatim and run under Perl
# with only the ini reader mocked -- on ~400 random configs plus handcrafted
# cases, all on the same fake dataset tree. The real production sanoid.conf
# matched key for key. Every divergence found was ONE mechanism (a child config
# was replaced instead of updated in place); the expected values below are what
# upstream's own code produced. The "parity lock" tests pin behavior that
# already matched.
# ---------------------------------------------------------------------------

def _fake_zfs_tree(monkeypatch, datasets):
    """`zfs list -Hr <fs>` over a fixed tree: <fs> first, then its descendants."""
    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        assert cmd[0] == ps.ZFS
        fs = cmd[-1]
        out = [d for d in datasets if d == fs or d.startswith(fs + "/")]
        return subprocess.CompletedProcess(cmd, 0, stdout="".join(f"{d}\n" for d in out).encode(), stderr=b"")  # bytes, like the real capture

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_a_child_section_above_its_recursive_parent_is_refused(tmp_path, monkeypatch):
    # DEV-005: upstream's recursion replaces every setting of [tank/a] but process_children_only, so the dataset is
    # pruned by [tank]'s retention.
    confdir = write_confdir(tmp_path, """
[tank/a]
    process_children_only = yes
    daily = 3
[tank]
    recursive = yes
    daily = 9
""")
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a", "tank/a/x"])
    with pytest.raises(ps.SanoidError, match=r"^\[tank/a\] in .*sanoid\.conf must come after \[tank\]: sections are read in "
                                             r"order, and \[tank\]'s recursion would replace its settings\. Move it below "
                                             r"\[tank\]\.$"):
        ps.load_config(confdir, make_zfs_client(tmp_path))


def test_a_nested_recursive_section_above_its_parent_is_refused(tmp_path, monkeypatch):
    # upstream: [tank]'s recursion replaces tank/a's settings and its children's, [tank/a]'s own daily = 3 lost
    confdir = write_confdir(tmp_path, "[tank/a]\n    recursive = zfs\n    daily = 3\n[tank]\n    recursive = yes\n    daily = 9\n")
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a", "tank/a/x", "tank/a/y", "tank/b"])
    with pytest.raises(ps.SanoidError, match=r"^\[tank/a\] in .* must come after \[tank\]: .* recursion would replace"):
        ps.load_config(confdir, make_zfs_client(tmp_path))


def test_a_nested_recursive_section_below_its_parent_governs_its_own_children(tmp_path, monkeypatch):
    # parity guard: below its parent, [tank/a]'s own settings reach tank/a and its children
    confdir = write_confdir(tmp_path, "[tank]\n    recursive = yes\n    daily = 9\n[tank/a]\n    recursive = zfs\n    daily = 3\n")
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a", "tank/a/x", "tank/a/y", "tank/b"])
    config = ps.load_config(confdir, make_zfs_client(tmp_path))
    for ds in ("tank/a", "tank/a/x", "tank/a/y"):
        assert config[ds]["zfs_recursion"] is True
        assert config[ds]["daily"] == "3"
    assert "zfs_recursion" not in config["tank/b"]
    assert config["tank/b"]["daily"] == "9"


def test_child_listed_after_its_recursive_parent_keeps_its_own_process_children_only(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, """
[tank]
    recursive = yes
    daily = 9
[tank/a]
    process_children_only = yes
    daily = 3
""")
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a", "tank/a/x"])
    config = ps.load_config(confdir, make_zfs_client(tmp_path))
    assert config["tank/a"]["process_children_only"] is True
    assert config["tank/a"]["daily"] == "3"  # its own section is applied on top of the parent's copy
    assert config["tank/a/x"]["daily"] == "9"


def test_a_child_section_above_a_skip_children_parent_is_refused_and_below_it_is_kept(tmp_path, monkeypatch):
    # upstream deletes a [tank/a] listed above [tank], so tank/a is neither snapshotted nor pruned (confirmed against real
    # Perl); 0.1.36 did the same
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a"])
    before_dir, after_dir = tmp_path / "before", tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    with pytest.raises(ps.SanoidError, match=r"^\[tank/a\] in .* must come after \[tank\]: sections are read in order, "
                                             r"and \[tank\]'s skip_children would drop it\. Move it below \[tank\]\.$"):
        ps.load_config(write_confdir(before_dir, "[tank/a]\n    daily = 3\n[tank]\n    skip_children = yes\n"),
                       make_zfs_client(before_dir))
    after = ps.load_config(write_confdir(after_dir, "[tank]\n    skip_children = yes\n[tank/a]\n    daily = 3\n"),
                           make_zfs_client(after_dir))
    assert after["tank/a"]["daily"] == "3"  # re-created by its own section, listed later


def test_use_template_list_tolerates_a_tab_after_a_comma(tmp_path):
    # DEV-003: upstream looks up a template literally named "\tb" and applies nothing for it (weekly stays at 0).
    confdir = write_confdir(tmp_path, "[template_a]\n    daily = 1\n[template_b]\n    weekly = 2\n[tank]\n    use_template = a,\tb\n")
    cfg = ps.load_config(confdir, make_zfs_client(tmp_path))["tank"]
    assert cfg["daily"] == "1"
    assert cfg["weekly"] == "2"


@pytest.mark.parametrize(("key", "value", "expected"), [
    ("autoprune", "no ", False), ("autoprune", "0\t", False), ("process_children_only", "no ", False),
    ("no_inconsistent_snapshot", "off ", False), ("prune_defer", "80 ", "80"),
])
def test_a_template_below_its_sections_is_trimmed_before_it_is_applied(tmp_path, key, value, expected):
    # guard for a deliberate deviation: upstream trims a section's values only when its loop reaches that
    # section, so a template below the sections using it arrives untrimmed and `autoprune = no ` prunes
    # there (confirmed against real Perl)
    config = _load(tmp_path, f"[tank]\n    use_template = t\n[template_t]\n    {key} = {value}\n")
    assert config["tank"][key] == expected


def test_a_repeated_key_in_a_template_default_below_its_sections_reads_its_first_value(tmp_path):
    # guard: upstream passes Perl's array reference on there, so `autoprune` reads as ON and a count as
    # a memory address (confirmed against real Perl)
    config = _load(tmp_path, "[tank]\n    hourly = 0\n[template_default]\n    autoprune = no\n    autoprune = yes\n"
                             "    daily = 3\n    daily = 30\n")
    assert config["tank"]["autoprune"] is False
    assert config["tank"]["daily"] == "3"


def test_sanoid_defaults_conf_values_are_trimmed(tmp_path):
    # guard: upstream never trims sanoid.defaults.conf, so `autoprune = no ` there prunes (confirmed against real Perl)
    confdir = write_confdir(tmp_path, "[tank]\n    daily = 3\n")
    defaults = confdir / "sanoid.defaults.conf"
    defaults.write_text(defaults.read_text().replace("autoprune = yes\n", "autoprune = no \n", 1))
    assert "autoprune = no \n" in defaults.read_text()
    assert ps.load_config(confdir, make_zfs_client(tmp_path))["tank"]["autoprune"] is False


def test_the_example_sanoid_conf_loads(tmp_path, monkeypatch):
    # the example shipped beside pysanoid.py, with the shipped defaults: it must stay a config pysanoid loads
    _fake_zfs_tree(monkeypatch, ["tank", "tank/home", "tank/home/alice", "tank/home/bob", "tank/media", "tank/vm",
                                 "tank/backup", "zroot", "zroot/var", "zroot/var/db"])
    config = ps.load_config(write_confdir(tmp_path, (REPO_ROOT / "sanoid.conf").read_text()), make_zfs_client(tmp_path))
    assert sorted(config) == ["tank/backup", "tank/home", "tank/home/alice", "tank/home/bob", "tank/media", "tank/vm",
                              "zroot/var/db"]
    assert config["tank/home/alice"]["monthly"] == "12"  # from tank/home's recursion
    assert config["tank/backup"]["prune_defer"] == "85"
    assert config["tank/media"]["hourly"] == "12"


# characters str.strip() removes that Perl's \s on bytes does not: upstream keeps them in the value
_NOT_PERL_SPACE = ["\xa0", "\x85", "\u2003", "\u2028", "\u3000", "\x1c", "\x1f"]


@pytest.mark.parametrize("pad", _NOT_PERL_SPACE)
@pytest.mark.parametrize(("key", "word"), [
    ("autoprune", "no"), ("autosnap", "no"), ("process_children_only", "no"), ("no_inconsistent_snapshot", "off"),
    ("autoprune", "yes"),
])
@pytest.mark.parametrize("where", ["after", "before", "template"])
def test_a_toggle_padded_with_a_space_perl_keeps_is_refused(tmp_path, pad, key, word, where):
    # DEV-002: upstream's `eq` leaves `no<NBSP>` a string, which reads as true, so pruning stays on
    # (confirmed against real Perl); str.strip() read it as "no"
    value = pad + word if where == "before" else word + pad
    conf = (f"[tank]\n    use_template = t\n[template_t]\n    {key} = {value}\n" if where == "template"
            else f"[tank]\n    {key} = {value}\n")
    with pytest.raises(ps.SanoidError, match=f"'{key}' in \\[tank\\] must be one of"):
        _ = _load(tmp_path, conf.encode())


@pytest.mark.parametrize("value", ["noo", "no # off", "n0", "\xa0no".encode("latin-1").decode("utf-8", "surrogateescape")])
def test_an_unrecognized_toggle_value_is_refused(tmp_path, value):
    # DEV-002: upstream reads each of these as true, so `autoprune` stays on (a raw 0xA0 byte is not UTF-8)
    with pytest.raises(ps.SanoidError, match="'autoprune' in \\[tank\\] must be one of"):
        _ = _load(tmp_path, f"[tank]\n    autoprune = {value}\n".encode("utf-8", "surrogateescape"))


@pytest.mark.parametrize("pad", _NOT_PERL_SPACE)
@pytest.mark.parametrize("where", ["after", "before", "beside a comma"])
def test_a_template_name_padded_with_a_space_perl_keeps_is_applied(tmp_path, pad, where):
    # DEV-003: all whitespace around a name is ignored, these too, so the template meant is applied; upstream looks up
    # "home<NBSP>", finds none, and the dataset is pruned by the defaults' retention (confirmed against real Perl)
    names = {"after": f"home{pad}", "before": f"{pad}home", "beside a comma": f"home{pad},{pad}other"}[where]
    config = _load(tmp_path, (f"[template_home]\n    daily = 5\n[template_other]\n    weekly = 2\n"
                              f"[tank]\n    use_template = {names}\n").encode())
    assert config["tank"]["daily"] == "5"
    assert config["tank"]["weekly"] == ("2" if where == "beside a comma" else "0")


@pytest.mark.parametrize(("key", "value"), [
    ("recursive", "yess"), ("recursive", "."), ("recursive", "("), ("recursive", "y.s"), ("recursive", ".*"),
    ("recursive", "1|0"), ("recursive", "maybe"), ("skip_children", "yess"), ("skip_children", "."),
    ("skip_children", "zfs"), ("skip_children", "("),
])
def test_a_recursive_or_skip_children_value_that_is_no_word_is_refused(tmp_path, monkeypatch, key, value):
    # DEV-004: upstream reads `yess` as not recursive, so the children go unmanaged, and compiles the value as a regex:
    # `.` matches "1" and recurses, `(` ends the run (confirmed against real Perl)
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a"])
    with pytest.raises(ps.SanoidError, match=f"'{key}' in \\[tank\\] must be one of"):
        _ = _load(tmp_path, f"[tank]\n    {key} = {value}\n    daily = 3\n")


@pytest.mark.parametrize("value", ["zfs", "ZFS", "usezfs", "zFs-native"])
def test_recursive_holding_zfs_anywhere_in_any_case_is_native_zfs_recursion(tmp_path, monkeypatch, value):
    # guard: upstream's /zfs/i
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a"])
    config = _load(tmp_path, f"[tank]\n    recursive = {value}\n    daily = 3\n")
    assert config["tank"]["zfs_recursion"] is True
    assert config["tank/a"]["autosnap"] is False


@pytest.mark.parametrize(("value", "recurses"), [("yes", True), ("1", True), ("ON", True), ("no", False), ("0", False),
                                                 ("off", False)])
def test_recursive_reads_a_true_or_false_word_by_membership(tmp_path, monkeypatch, value, recurses):
    # guard: the words upstream's @istrue and @isfalse hold
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a"])
    config = _load(tmp_path, f"[tank]\n    recursive = {value}\n    daily = 3\n")
    assert ("tank/a" in config) is recurses
    assert "zfs_recursion" not in config["tank"]


@pytest.mark.parametrize("pad", [" ", "\t", "\r", "\x0b", "\x0c"])
def test_a_toggle_padded_with_perl_whitespace_is_read(tmp_path, pad):
    # DEV-001: every value is trimmed of Perl's \s, so these are the word itself
    config = _load(tmp_path, f"[tank]\n    autoprune = {pad}no{pad}\n    autosnap = yes{pad}\n".encode())
    assert config["tank"]["autoprune"] is False
    assert config["tank"]["autosnap"] is True


# ---------------------------------------------------------------------------
# ini grammar (2026-09-21 Module 2, second half). load_config used configparser,
# a different dialect from upstream's Config::IniFiles. The replacement is a port
# of the real grammar, fuzzed against the REAL module (both CPAN releases, 3,800
# files, zero mismatches on the raw parse) and checked end to end on 51 syntax
# cases. Expected values below are what upstream's own `init` produced.
# ---------------------------------------------------------------------------

def _load(tmp_path, conf):
    """load_config over an arbitrary sanoid.conf (str or raw bytes)."""
    confdir = write_confdir(tmp_path, "")
    (confdir / "sanoid.conf").write_bytes(conf if isinstance(conf, bytes) else conf.encode())
    return ps.load_config(confdir, make_zfs_client(tmp_path))


def test_default_section_is_an_ordinary_section_not_inherited_defaults(tmp_path):
    # configparser gives [DEFAULT] special meaning and applied its keys to EVERY dataset --
    # a silent, fleet-wide retention change. Upstream: just a section named DEFAULT.
    config = _load(tmp_path, "[DEFAULT]\n    daily = 5\n[tank]\n    monthly = 3\n")
    assert config["DEFAULT"]["daily"] == "5"
    assert config["tank"]["daily"] == "90"  # the shipped default, NOT 5
    assert config["tank"]["monthly"] == "3"


def test_spaces_inside_section_brackets_are_trimmed_like_upstream(tmp_path):
    # configparser kept " tank " -- a dataset name with spaces that zfs would reject
    config = _load(tmp_path, "[ tank ]\n    daily = 7\n")
    assert "tank" in config
    assert " tank " not in config


@pytest.mark.parametrize("conf", ["", "# only comments\n; and more\n", "\n\n", "   \n", "[tank]"])
def test_a_config_with_no_sections_is_fatal_like_upstream(tmp_path, conf):
    # Upstream's tie fails ("Empty file treated as error"; a file with no line ending at all counts
    # as empty). Silently accepting it made the units run GREEN while managing nothing -- and the
    # unit's ConditionFileNotEmpty can't catch a file that merely contains comments.
    with pytest.raises(ps.SanoidError, match="cannot load"):
        _load(tmp_path, conf)


@pytest.mark.parametrize("conf", [
    "[tank]\n    daily: 7\n",  # ':' is not a delimiter
    "[tank] # note\n    daily = 7\n",  # nothing may follow the closing bracket
    "[tank]\n    daily\n",  # a line with no '='
    "daily = 7\n[tank]\n",  # a parameter before any section
    "[tank]\n    = 7\n",  # an empty key
    "[]\n    daily = 7\n",  # an empty section name
    "[tank]\n    pre_snapshot_script = /bin/x \\\n      --flag\n",  # no backslash continuation
])
def test_syntax_upstream_refuses_is_refused_cleanly(tmp_path, conf):
    with pytest.raises(ps.SanoidError, match="cannot load"):
        _load(tmp_path, conf)


def test_utf8_bom_is_refused_like_upstream(tmp_path):
    # the released Config::IniFiles' BOM-stripping regex is itself mangled, so a BOM is an error there too
    with pytest.raises(ps.SanoidError, match="cannot load"):
        _load(tmp_path, b"\xef\xbb\xbf[tank]\n    daily = 7\n")


BOM_TWICE = b"\xc3\xaf\xc2\xbb\xc2\xbf"  # the UTF-8 BOM encoded twice: what that mangled regex does strip


@pytest.mark.parametrize("conf", [
    BOM_TWICE + b"[tank]\n    daily = 7\n",
    BOM_TWICE + b"# sanoid.conf\n[tank]\n    daily = 7\n",
    BOM_TWICE + b"\n[tank]\n    daily = 7\n",
    BOM_TWICE + b"[tank]\r\n    daily = 7\r\n",
])
def test_a_bom_encoded_twice_is_stripped_like_upstream(tmp_path, conf):
    # upstream loads these (confirmed against real Perl); refusing them stopped every snapshot and prune
    assert _load(tmp_path, conf)["tank"]["daily"] == "7"


@pytest.mark.parametrize("conf", [
    BOM_TWICE * 2 + b"[tank]\n    daily = 7\n",  # stripped once only
    BOM_TWICE + b"[tank]",  # no line ending: nothing is read at all
])
def test_a_bom_encoded_twice_is_stripped_only_where_upstream_strips_it(tmp_path, conf):
    with pytest.raises(ps.SanoidError, match="cannot load"):
        _load(tmp_path, conf)


def test_ini_lines_strips_a_bom_encoded_twice_from_the_first_line_only():
    bom = BOM_TWICE.decode()
    assert ps._ini_lines(f"{bom}[tank]\n{bom}x\n") == ["[tank]", f"{bom}x"]


@pytest.mark.parametrize("conf", [
    b"[tank/b\x00x]\n    daily = 7\n",  # a dataset name
    b"[tank]\n    pre_snapshot_script = /bin/x\x00 --flag\n",  # a hook command
    b"[tank]\n    da\x00ily = 7\n",  # a key
])
def test_a_nul_byte_in_sanoid_conf_is_refused(tmp_path, conf):
    # Upstream loads it, and each command the byte reaches is cut short there: its snapshot listing drops every dataset
    # sorted after the section, and a recursive section manages the truncated name's children (confirmed against real
    # Perl). pysanoid crashed with a traceback, mid-prune for a hook.
    with pytest.raises(ps.SanoidError, match="NUL byte"):
        _load(tmp_path, conf)


def test_a_nul_byte_in_sanoid_defaults_conf_is_refused(tmp_path):
    confdir = write_confdir(tmp_path, "[tank]\n    daily = 7\n")
    defaults = confdir / "sanoid.defaults.conf"
    defaults.write_bytes(defaults.read_bytes().replace(b"\ndaily = 90\n", b"\ndaily = 9\x000\n", 1))
    with pytest.raises(ps.SanoidError, match="NUL byte"):
        ps.load_config(confdir, make_zfs_client(tmp_path))


@pytest.mark.parametrize("version, accepted", [
    ("2", True), ("02", True), ("3", True), ("1", False),
    ("1_0", False), ("+2", False), ("\N{FULLWIDTH DIGIT TWO}", False), ("2.5", False), ("2x", False), ("", False),
])
def test_the_defaults_version_is_read_as_plain_digits(tmp_path, version, accepted):
    # DEV-050: int() also read `1_0` as 10, where upstream reads 1 and refuses the file (confirmed against real Perl)
    confdir = write_confdir(tmp_path, "[tank]\n    daily = 7\n")
    defaults = confdir / "sanoid.defaults.conf"
    defaults.write_text(defaults.read_text().replace("\nversion = 2\n", f"\nversion = {version}\n", 1))
    if accepted:
        assert "tank" in ps.load_config(confdir, make_zfs_client(tmp_path))
    else:
        with pytest.raises(ps.SanoidError, match="v1,|must be a whole number"):
            ps.load_config(confdir, make_zfs_client(tmp_path))


def test_a_nul_byte_reaching_a_zfs_command_is_a_clean_error():
    # only a corrupted cache can still supply one; subprocess raised ValueError, a traceback
    with pytest.raises(ps.SanoidError, match="could not run"):
        ps.run_zfs_bytes([ps.ZFS, "get", "tank/b\x00x"])
    with pytest.raises(ps.SanoidError, match="could not run"):
        ps.run_zfs_cmd([ps.ZFS, "destroy", "tank@a\x00b"])


@pytest.mark.parametrize("script, env", [("/usr/bin/true\x00 --flag", {}), ("/usr/bin/true", {"SANOID_TARGETS": "tank/b\x00x"})])
def test_a_hook_that_a_nul_byte_stops_from_starting_counts_as_failed(script, env, capsys):
    # like a hook that can't start (a failed fork): -1, which vetoes a prune, instead of a traceback mid-run
    cfg = {"pre_pruning_script": script, "script_timeout": "5"}
    assert ps.run_hook_script(cfg, "pre_pruning_script", "tank/b", env) == -1
    # why is Python's own words, which differ: 3.14 on Linux starts the hook with posix_spawn, and says "posix_spawn:
    # embedded null character in path" where other versions say "embedded null byte"
    assert re.fullmatch(rf"could not run '{re.escape(script)}': .*embedded null (byte|character).*\n"
                        r"WARN: pre_pruning_script failed, -1\n", capsys.readouterr().err)
    assert "SANOID_TARGETS" not in os.environ


def _hook_envs(monkeypatch) -> list[dict[str, str]]:
    """The SANOID_* variables each hook run is given, in order."""
    seen: list[dict[str, str]] = []

    def fake_run(cmd: list[str], env: dict[str, str] | None = None, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert env is not None, "only hooks run here"
        seen.append({k: v for k, v in env.items() if k.startswith("SANOID_")})
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_a_hooks_variables_are_deleted_after_it_as_upstream_deletes_them(monkeypatch):
    # Parity (P9, confirmed against real Perl): upstream sets a hook's variables in its own environment and deletes them
    # after the hook, so one it inherited is gone once a hook has set it. Before 0.1.49 every hook saw it.
    monkeypatch.setenv("SANOID_SCRIPT", "inherited")
    monkeypatch.setenv("SANOID_TARGETS", "inherited")
    seen = _hook_envs(monkeypatch)
    cfg = {"pre_pruning_script": "/bin/true", "pruning_script": "/bin/true", "script_timeout": "5"}
    env = {"SANOID_TARGET": "tank/a", "SANOID_SNAPNAME": "autosnap_x_hourly"}
    for key, extra in (("pre_pruning_script", {}), ("pruning_script", {"SANOID_SCRIPT": "prune"}),
                       ("pre_pruning_script", {})):
        assert ps.run_hook_script(cfg, key, "tank/a", {**env, **extra}) == 0
    inherited = {"SANOID_TARGETS": "inherited"}
    assert seen == [{**env, **inherited, "SANOID_SCRIPT": "inherited"}, {**env, **inherited, "SANOID_SCRIPT": "prune"},
                    {**env, **inherited}]
    assert {k: v for k, v in os.environ.items() if k.startswith("SANOID_")} == inherited


def test_a_hook_that_cannot_start_still_has_its_variables_deleted(monkeypatch):
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("SANOID_SCRIPT", "inherited")
    cfg = {"pruning_script": "/bin/true", "script_timeout": "5"}
    assert ps.run_hook_script(cfg, "pruning_script", "tank/a", {"SANOID_TARGET": "tank/a", "SANOID_SCRIPT": "prune"}) == -1
    assert "SANOID_SCRIPT" not in os.environ and "SANOID_TARGET" not in os.environ


def test_take_under_readonly_deletes_a_hooks_variables_without_running_it(monkeypatch):
    # Parity: upstream's take sets and deletes them around a hook it then doesn't run (its lines 657-674 and 735-754:
    # the set and the delete sit outside its readonly check)
    monkeypatch.setenv("SANOID_SCRIPT", "inherited")
    seen = _hook_envs(monkeypatch)
    config = {"tank/a": _take_cfg("tank/a", pre_snapshot_script="/bin/true", post_snapshot_script="/bin/true")}
    assert ps.take_snapshots(config, {}, readonly=True, now=NOW_APRIL) == {}
    assert seen == []
    assert "SANOID_SCRIPT" not in os.environ


def test_non_utf8_byte_in_a_comment_is_harmless(tmp_path):
    # upstream reads raw bytes; 0.1.12 crashed with UnicodeDecodeError and stopped ALL snapshotting
    config = _load(tmp_path, b"[tank]\n    # caf\xe9\n    daily = 7\n")
    assert config["tank"]["daily"] == "7"


def test_here_doc_value_is_accepted(tmp_path):
    config = _load(tmp_path, "[tank]\n    pre_snapshot_script = <<EOT\n/bin/x\nEOT\n    daily = 7\n")
    assert config["tank"]["pre_snapshot_script"] == "/bin/x"
    assert config["tank"]["daily"] == "7"


def test_crlf_comments_and_duplicate_section_blocks_parse_like_upstream(tmp_path):
    config = _load(tmp_path, b"[tank]\r\n    ; daily = 3\r\n    # monthly = 4\r\n    daily = 7\r\n"
                             b"[pool2]\r\n    daily = 1\r\n[tank]\r\n    monthly = 3\r\n")
    assert config["tank"]["daily"] == "7"
    assert config["tank"]["monthly"] == "3"  # a repeated [tank] block merges into the first
    assert config["pool2"]["daily"] == "1"


def test_ini_first_line_ending_is_the_separator_for_the_whole_file():
    assert ps._ini_lines("a\r\nb\nc\r\n") == ["a", "b\nc"]  # CRLF first: a lone LF later is content
    assert ps._ini_lines("a\nb\r\nc\n") == ["a", "b", "c"]  # LF first: a later CRLF loses its CR
    assert ps._ini_lines("a\x15b\x15") == ["a", "b"]  # NAK is a line ending too
    assert ps._ini_lines("a\x0cb\nc\n") == ["a\x0cb", "c"]  # splitlines() would also split on FF, VT, NEL...
    assert ps._ini_lines("[tank]") == []  # no line ending at all: upstream reads nothing


def test_empty_here_doc_makes_a_repeated_key_lose_its_later_values():
    # upstream stores an empty here-doc as undef, and its push() silently drops values added to undef
    # (found by fuzzing this parser against the real module)
    sections, errors = ps._parse_ini_text("[s]\nk = <<E\nE\nk = v\n")
    assert errors == []
    assert sections == {"s": {"k": []}}


@pytest.mark.parametrize("where", ["sanoid.conf", "sanoid.defaults.conf"])
def test_an_empty_here_doc_reads_as_a_blank_value(tmp_path, capsys, where):
    # DEV-049 and DEV-010: upstream reads it as undef. Here it is blank, so the dump shows '' where upstream's shows
    # undef, and a blank count is what prune refuses (DEV-008).
    confdir = write_confdir(tmp_path, "[tank]\n    daily = <<EOT\nEOT\n" if where == "sanoid.conf" else "[tank]\n")
    if where == "sanoid.defaults.conf":
        defaults = confdir / "sanoid.defaults.conf"
        _ = defaults.write_text(defaults.read_text().replace("daily = 90\n", "daily = <<EOT\nEOT\n", 1))
    config = ps.load_config(confdir, make_zfs_client(tmp_path))
    assert config["tank"]["daily"] == ""
    ps.print_debug_dump(config, {}, ps.snapshots_by_path({}, config))
    assert "'daily' => ''," in capsys.readouterr().out
    with pytest.raises(ps.SanoidError, match="daily"):
        ps._preflight_prune_config(config)


def test_an_empty_here_doc_path_falls_back_to_the_section_name(tmp_path):
    # upstream's `defined $ini{$section}{'path'}` is false for it, so the section name is the path; reading it
    # as "" made every snapshot of the dataset fail
    config = _load(tmp_path, "[tank]\n    path = <<EOT\nEOT\n    daily = 3\n")
    assert config["tank"]["path"] == "tank"


@pytest.mark.parametrize("value, expected", [("7 # keep a week", 7), ("7# x", 7), ("7 ; x", 7), ("7;30", 7), ("0 # off", 0)])
def test_retention_count_may_carry_a_trailing_comment_like_upstream(value, expected):
    # Config::IniFiles keeps the comment in the value; upstream numifies the leading digits (real Perl: 7)
    assert ps._cfg_count({"k": value}, "k", 99) == expected


@pytest.mark.parametrize("value", ["# off", "; x", "7d", "7 days", "3o", " # 7"])
def test_retention_count_still_rejects_comment_only_and_typo_values(value):
    # `# off` numifies to 0 upstream -- prune everything of the type -- so it must be refused like a blank
    with pytest.raises(ps.SanoidError, match="'k'"):
        ps._cfg_count({"k": value}, "k", 99)


def test_prune_defer_stays_digits_only_even_with_a_comment():
    # upstream's check_capacity_limit is /^\d+\z/, so "80 # pct" is FATAL there and stays so here
    with pytest.raises(ps.SanoidError, match="prune_defer"):
        ps.check_prune_defer({"path": "tank/x", "prune_defer": "80 # pct"}, {"tank": 50}, "tank")


def test_numeric_schedule_settings_ignore_a_trailing_comment():
    assert ps._cfg_int({"k": "15 # minutes"}, "k", 0) == 15
    assert ps._cfg_int({"k": "# unset"}, "k", 42) == 42  # a comment alone is blank -> the default, as before


def test_a_config_with_inline_comments_on_its_counts_plans_and_prunes(tmp_path):
    # 0.1.12 refused this whole config -- no snapshots, no pruning -- though upstream runs it fine
    config = _load(tmp_path, "[tank]\n    daily = 7 # keep a week\n    hourly = 0 # none\n    monthly = 0 ; none\n")
    problems = []
    plan = ps.plan_snapshots(config, {}, now=NOW_APRIL, problems=problems)
    assert plan["tank"].types == ["daily"]
    assert problems == []
    ps._preflight_prune_config(config)  # must not raise


# ---------------------------------------------------------------------------
# snapshot enumeration + cache (2026-09-21 Module 3). getsnaps, getsnapsbytype and
# the cache add/remove subs were differential-tested against upstream's REAL code
# (extracted verbatim, run under Perl against a fake `zfs` that logs every call):
# ~1,000 random scenarios plus handcrafted TTL / fallback / lock cases. Expected
# values below are what upstream itself produced; the "parity lock" tests pin
# behavior that already matched.
# ---------------------------------------------------------------------------

def _sec(path, **over):
    return {"path": path, "autosnap": True, "autoprune": True, "process_children_only": False, **over}


def _zfs_replies(monkeypatch, *replies):
    """Patches subprocess.run: each call consumes the next (returncode, stdout, stderr).
    Returns the list of commands issued."""
    remaining = iter(replies)
    commands = []

    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        commands.append(cmd)
        rc, out, err = next(remaining)
        return subprocess.CompletedProcess(cmd, rc, stdout=out.encode(), stderr=err.encode())  # bytes, like the real capture

    monkeypatch.setattr(subprocess, "run", fake_run)
    return commands


def test_snapshot_line_parsing_matches_upstreams_unanchored_regexes():
    snaps = ps._parse_snapshot_lines([
        "@autosnap_2026-10-26_13:40:53_daily\tcreation\t100\t-",  # an empty dataset name still parses
        "tank@autosnap_ly:bly\tcreation\t5\t-",  # the type regex has no end anchor: 'ly'
        "tank@autosnap_x_daily:xly\tcreation\t7\t-",  # ...and reads 'daily': the longest \w*ly after the last _
        "tank@autosnap_2026-01-01_00:00:00dst_hourly\tcreation\t9\t-",
    ])
    assert snaps[""]["autosnap_2026-10-26_13:40:53_daily"].ctime == 100
    assert snaps["tank"]["autosnap_ly:bly"].snap_type == "ly"
    assert snaps["tank"]["autosnap_x_daily:xly"].snap_type == "daily"
    assert snaps["tank"]["autosnap_2026-01-01_00:00:00dst_hourly"].snap_type == "hourly"


def test_cache_and_zfs_output_are_split_on_newline_only():
    # Perl's <FH> splits on "\n"; str.splitlines() also splits on CR, VT, FF, NEL... and silently rewrites a line
    assert ps._lines("a\r\nb\x0cc\n") == ["a\r", "b\x0cc"]
    assert ps._lines("") == []
    assert ps._lines("no newline") == ["no newline"]


# The regexes these parsers stand for: upstream's, as pysanoid ran them through 0.1.46. Python's matcher takes quadratic
# time on some lines they fail on, where Perl's rejects a snapshot line without "creation" at once.
_UPSTREAM_SNAP_LINE = re.compile(r"^(?P<fs>.*)@(?P<name>.*ly)\t+creation\t*(?P<ctime>\d*)", re.ASCII)
_UPSTREAM_SNAP_TYPE = re.compile(r".*_(\w*ly)", re.ASCII)
_UPSTREAM_PARAM = re.compile(rf"[{ps._WS}]*([^=]*?[^={ps._WS}])[{ps._WS}]*=[{ps._WS}]*(.*)$")
_UPSTREAM_TRAILING_COMMENT = re.compile(rf"[{ps._WS}]*[#;].*")
_PIECES = ["@", "l", "y", "ly", "_", "\t", "creation", "\tcreation\t", "a", "-", "0", "9", "\n", " ", "\r", "\x0b",
           "=", "#", ";", "[", "]", "\N{LATIN SMALL LETTER E WITH ACUTE}", "autosnap", "_daily", "dst_hourly"]


def _random_texts(seed: int, count: int = 20_000) -> list[str]:
    rng = __import__("random").Random(seed)
    return ["".join(rng.choice(_PIECES) for _ in range(rng.randint(0, 14))) for _ in range(count)]


def _upstream_snapshot_line(line: str) -> tuple[str, str, str] | None:
    m = _UPSTREAM_SNAP_LINE.match(line)
    return None if m is None else (m.group("fs"), m.group("name"), m.group("ctime"))


@pytest.mark.parametrize("line", [
    "tank@autosnap_x_daily\tcreation\t5\t-", "@autosnap_x_daily\tcreation\t5", "a@b@autosnap_ly\tcreation\t1",
    "tank@autosnap_x_daily\t\tcreation\t\t77x", "tank@autosnap_ly\tcreation\t", "tank@ly\tcreation\t1",
    "tank@xly\tcreation\t1@yly\tcreation\t2", "tank@ly\tcreation\tly\tcreation\t3", "@ly\tcreation\t1\n@zly\tcreation\t2",
    "tank\n@autosnap_ly\tcreation\t1", "tank@autosnap_daily creation 5", "tank@autosnap_dailycreation\t5",
    "tank@autosnap_daily\tcreation\t\N{ARABIC-INDIC DIGIT FIVE}", "", "@", "ly\tcreation", "@@ly\tcreation\t9",
])
def test_a_snapshot_line_is_read_as_upstreams_regex_reads_it(line):
    assert ps._snapshot_line(line) == _upstream_snapshot_line(line)


def test_snapshot_lines_are_read_as_upstreams_regex_reads_them_fuzzed():
    for line in _random_texts(1):
        assert ps._snapshot_line(line) == _upstream_snapshot_line(line), repr(line)


@pytest.mark.parametrize("name", [
    "autosnap_x_daily", "autosnap_ly:bly", "autosnap_x_daily:xly", "autosnap_2026-01-01_00:00:00dst_hourly",
    "autosnap_a_b_ly", "autosnap_lyx", "autosnap_x_lyly_", "autosnap__ly", "autosnap_", "autosnap", "_ly", "a_ly\n_bly",
    "a\n_ly", "autosnap_dai\N{LATIN SMALL LETTER E WITH ACUTE}ly", "autosnap_x-daily", "autosnap_a-ly_b",
])
def test_a_snapshot_type_is_read_as_upstreams_regex_reads_it(name):
    m = _UPSTREAM_SNAP_TYPE.match(name)
    assert ps._snapshot_type(name) == (m.group(1) if m else None)


def test_snapshot_types_are_read_as_upstreams_regex_reads_them_fuzzed():
    for name in _random_texts(2):
        m = _UPSTREAM_SNAP_TYPE.match(name)
        assert ps._snapshot_type(name) == (m.group(1) if m else None), repr(name)


def test_ini_parameters_and_trailing_comments_are_read_as_upstreams_regexes_read_them_fuzzed():
    for text in _random_texts(3):
        old, new = _UPSTREAM_PARAM.match(text), ps._PARAM_RE.match(text)
        assert (old and old.groups()) == (new and new.groups()), repr(text)
        assert _UPSTREAM_TRAILING_COMMENT.sub("", text, count=1) == ps._TRAILING_COMMENT_RE.sub("", text, count=1), repr(text)


def _within_a_second(call: Any) -> Any:
    started = time.perf_counter()
    try:
        return call()
    except ps.SanoidError as e:
        return e
    finally:
        assert time.perf_counter() - started < 1.0


def test_a_long_snapshot_line_built_to_backtrack_is_read_at_once():
    lines = ["@ly" * 40_000, "tank@autosnap" + "_a" * 30_000 + "-ly\tcreation\t1"]
    snaps = _within_a_second(lambda: ps._parse_snapshot_lines(lines))
    assert [s.snap_type for s in snaps["tank"].values()] == [""]  # the first isn't a snapshot, the second has no type


def test_a_long_ini_line_built_to_backtrack_is_read_at_once():
    _, errors = _within_a_second(lambda: ps._parse_ini_text("[s]\n" + " " * 48_000 + "k\n"))
    assert errors == ["line 2 is malformed: " + repr(" " * 48_000 + "k")]


def test_a_long_value_built_to_backtrack_is_read_at_once():
    assert isinstance(_within_a_second(lambda: ps._cfg_int({"k": "7" + " " * 40_000 + "x"}, "k", 0)), ps.SanoidError)


def test_get_snapshots_cache_is_byte_exact_through_a_real_pipe(tmp_path, monkeypatch):
    # Goes through a REAL subprocess on purpose: text-mode capture translates CR and CRLF to "\n" before the
    # caller sees them, so a stray CR vanished from the cache -- and a faked subprocess.run cannot show that.
    fake_zfs = tmp_path / "zfs"
    fake_zfs.write_text("#!/bin/sh\nprintf 'tank@autosnap_x_daily\\tcreation\\t5\\t-\\r\\n'\n")
    fake_zfs.chmod(0o755)
    monkeypatch.setattr(ps, "ZFS", str(fake_zfs))
    ps.get_snapshots({"t": _sec("tank")}, tmp_path / "cache", tmp_path / "run", 1200)
    assert (tmp_path / "cache" / "snapshots.txt").read_bytes() == b"tank@autosnap_x_daily\tcreation\t5\t-\r\n"


def test_get_snapshots_queries_the_configured_datasets_sorted_case_insensitively(tmp_path, monkeypatch):
    commands = _zfs_replies(monkeypatch, (0, "", ""))
    config = {
        "z": _sec("Zed"), "a": _sec("apple"), "t": _sec("tank"),
        "off": _sec("skipme", autosnap=False, autoprune=False),  # both off: not queried
        "kids": _sec("kidsonly", process_children_only=True),  # children only: not queried
        "template_x": _sec("tmpl"),  # a template: not queried
    }
    ps.get_snapshots(config, tmp_path / "cache", tmp_path / "run", 1200)
    assert commands == [[ps.ZFS, "get", "-Hrpt", "snapshot", "creation", "apple", "tank", "Zed"]]


def test_get_snapshots_retries_for_every_dataset_when_the_first_query_fails(tmp_path, monkeypatch, capsys):
    commands = _zfs_replies(
        monkeypatch,
        (1, "partial\n", "cannot open 'gone': dataset does not exist\n"),
        (0, "tank@autosnap_x_daily\tcreation\t5\t-\n", ""),
    )
    snaps = ps.get_snapshots({"t": _sec("tank"), "g": _sec("gone")}, tmp_path / "cache", tmp_path / "run", 1200)
    assert commands[1] == [ps.ZFS, "get", "-Hrpt", "snapshot", "creation"]  # no names: every dataset on the system
    assert snaps["tank"]["autosnap_x_daily"].ctime == 5
    captured = capsys.readouterr()
    assert "retrying without dataset names" in captured.out
    # upstream leaves zfs's own complaint on the terminal; captured here it must not vanish
    assert "dataset does not exist" in captured.err


def test_get_snapshots_cache_freshness_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(ps.time, "time", lambda: 1_790_000_000.0)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    cache.write_text("tank@autosnap_old_daily\tcreation\t1\t-\n")
    commands = _zfs_replies(monkeypatch, (0, "tank@autosnap_new_daily\tcreation\t2\t-\n", ""))
    config = {"t": _sec("tank")}

    os.utime(cache, (1_790_000_000 - 1200, 1_790_000_000 - 1200))  # exactly the TTL old: still fresh
    assert "autosnap_old_daily" in ps.get_snapshots(config, cache_dir, tmp_path / "run", 1200)["tank"]
    assert commands == []

    os.utime(cache, (1_790_000_000 - 1201, 1_790_000_000 - 1201))  # one second older: refreshed
    assert "autosnap_new_daily" in ps.get_snapshots(config, cache_dir, tmp_path / "run", 1200)["tank"]
    assert len(commands) == 1


def test_get_snapshots_writes_an_empty_cache_when_zfs_fails_outright(tmp_path, monkeypatch):
    # P4: both queries fail -> an EMPTY (and fresh) cache, as upstream's. Pinned on purpose: while it lasts, every
    # snapshot type looks overdue. If that is ever changed, change this test with it.
    _zfs_replies(monkeypatch, (1, "", "internal error"), (1, "", "internal error"))
    assert ps.get_snapshots({"t": _sec("tank")}, tmp_path / "cache", tmp_path / "run", 1200) == {}
    assert (tmp_path / "cache" / "snapshots.txt").read_text() == ""


def test_remove_cached_snapshots_keeps_junk_lines_and_preserves_the_mtime(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    cache.write_text("junk\n\ntank@a_daily\tcreation\t1\t-\ntank@b_daily\tcreation\t2\t-\n")
    os.utime(cache, (1_000, 1_000))
    ps.remove_cached_snapshots(["tank@a_daily"], cache_dir, tmp_path / "run", wait=True)
    assert cache.read_text() == "junk\n\ntank@b_daily\tcreation\t2\t-\n"
    assert int(cache.stat().st_mtime) == 1_000  # an incremental edit must not reset the TTL clock


def test_add_cached_snapshots_lists_every_child_of_a_recursive_snapshot(tmp_path, monkeypatch):
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a", "tank/a/x"])
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "snapshots.txt").write_text("tank@autosnap_a_daily\tcreation\t1\t-\n")
    ps.add_cached_snapshots({"tank@autosnap_c_daily": {"time": 5, "recursive": True}}, cache_dir, tmp_path / "run",
                            make_zfs_client(tmp_path))
    assert (cache_dir / "snapshots.txt").read_text() == (
        "tank@autosnap_a_daily\tcreation\t1\t-\n"
        "tank@autosnap_c_daily\tcreation\t5\t-\n"
        "tank/a@autosnap_c_daily\tcreation\t5\t-\n"
        "tank/a/x@autosnap_c_daily\tcreation\t5\t-\n"
    )


def test_add_cached_snapshots_leaves_a_missing_cache_missing(tmp_path):
    # Upstream: `copy($cache, "$cache.tmp") or die`, which ends a --cron run before it prunes. Building a cache from
    # only the new lines would give it a FRESH mtime, so for a whole TTL every run would think these are the only
    # snapshots that exist. Left missing, the next run lists zfs, which has them.
    problems = []
    ps.add_cached_snapshots({"tank@autosnap_c_daily": {"time": 5, "recursive": False}},
                            tmp_path / "cache", tmp_path / "run", make_zfs_client(tmp_path), problems=problems)
    assert not (tmp_path / "cache" / "snapshots.txt").exists()
    assert problems == []


def test_add_cached_snapshots_leaves_a_cache_cut_short_as_it_is(tmp_path):
    # DEV-036: upstream appends straight onto the last line and fuses the two. Ending that line and appending would pass
    # a line cut short off as whole; left as it is, the next run lists zfs.
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    cache.write_text("tank@a_daily\tcreation\t17")
    os.utime(cache, (1_000, 1_000))
    problems: list[str] = []
    ps.add_cached_snapshots({"tank@b_daily": {"time": 2, "recursive": False}}, cache_dir, tmp_path / "run",
                            make_zfs_client(tmp_path), problems=problems)
    assert cache.read_text() == "tank@a_daily\tcreation\t17"
    assert int(cache.stat().st_mtime) == 1_000
    assert problems == []


def _failing_write(monkeypatch):
    """Every cache write fails, as on a full disk."""
    def fail(path, text):
        raise OSError(errno.ENOSPC, "No space left on device", str(path))
    monkeypatch.setattr(ps, "_write_text", fail)


def test_get_snapshots_carries_on_when_the_cache_cannot_be_written(tmp_path, monkeypatch, capsys):
    # Upstream dies when it can't create the file, and on a full disk installs a truncated one; pysanoid died too, so a
    # full disk stopped every snapshot and every prune. The listing just read from zfs is used instead.
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "snapshots.txt").write_text("tank@autosnap_old_daily\tcreation\t1\t-\n")
    os.utime(cache_dir / "snapshots.txt", (1_000, 1_000))  # long expired
    _zfs_replies(monkeypatch, (0, "tank@autosnap_new_daily\tcreation\t2\t-\n", ""))
    _failing_write(monkeypatch)
    problems = []
    snaps = ps.get_snapshots({"t": _sec("tank")}, cache_dir, tmp_path / "run", 1200, problems=problems)
    assert list(snaps["tank"]) == ["autosnap_new_daily"]
    assert len(problems) == 1 and "No space left on device" in problems[0]
    assert "ERROR: could not update" in capsys.readouterr().err
    assert list(cache_dir.iterdir()) == []  # the stale cache goes too, so the next run reads zfs


def test_get_snapshots_reads_zfs_when_a_fresh_cache_cannot_be_read(tmp_path, monkeypatch):
    # upstream reads it as no snapshots at all, so take re-takes every type of every dataset; pysanoid crashed
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    cache.write_text("tank@autosnap_old_daily\tcreation\t1\t-\n")
    commands = _zfs_replies(monkeypatch, (0, "tank@autosnap_new_daily\tcreation\t2\t-\n", ""))
    real_read = ps._read_text

    def unreadable(path):
        if path == cache:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_read(path)

    monkeypatch.setattr(ps, "_read_text", unreadable)
    snaps = ps.get_snapshots({"t": _sec("tank")}, cache_dir, tmp_path / "run", 1200)
    assert list(snaps["tank"]) == ["autosnap_new_daily"]
    assert len(commands) == 1
    assert cache.read_text() == "tank@autosnap_new_daily\tcreation\t2\t-\n"  # rewritten from zfs


def test_get_snapshots_treats_a_directory_as_no_cache(tmp_path, monkeypatch):
    # upstream's `-f`: anything but a regular file is no cache. pysanoid read it, and crashed (a named pipe would have
    # hung the run instead).
    cache_dir = tmp_path / "cache"
    (cache_dir / "snapshots.txt").mkdir(parents=True)
    _zfs_replies(monkeypatch, (0, "tank@autosnap_new_daily\tcreation\t2\t-\n", ""))
    problems = []
    snaps = ps.get_snapshots({"t": _sec("tank")}, cache_dir, tmp_path / "run", 1200, problems=problems)
    assert list(snaps["tank"]) == ["autosnap_new_daily"]
    assert len(problems) == 1  # a directory can't be replaced either: reported until someone removes it


def test_a_snapshot_cache_holding_a_nul_byte_counts_as_no_cache(tmp_path, monkeypatch):
    # DEV-038: upstream read the damaged line and pruned it under the name before the NUL, a dataset's newest daily
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    cache.write_bytes(b"tank@autosnap_new_daily" + b"\0" * 8 + b"_daily\tcreation\t1\t-\n")  # fresh
    commands = _zfs_replies(monkeypatch, (0, "tank@autosnap_new_daily\tcreation\t2\t-\n", ""))
    snaps = ps.get_snapshots({"t": _sec("tank")}, cache_dir, tmp_path / "run", 1200)
    assert list(snaps["tank"]) == ["autosnap_new_daily"]
    assert len(commands) == 1
    assert cache.read_bytes() == b"tank@autosnap_new_daily\tcreation\t2\t-\n"  # rewritten from zfs


def test_get_snapshots_reads_zfs_when_the_lock_file_cannot_be_made(tmp_path, monkeypatch):
    # DEV-041. Raising here would mean that once the cache expired no run took a snapshot while the run directory stayed
    # unusable.
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    cache.write_text("tank@autosnap_old_daily\tcreation\t1\t-\n")
    os.utime(cache, (1_000, 1_000))  # long expired
    commands = _zfs_replies(monkeypatch, (0, "tank@autosnap_new_daily\tcreation\t2\t-\n", ""))
    problems: list[str] = []
    snaps = ps.get_snapshots({"t": _sec("tank")}, cache_dir, _unusable_run_dir(tmp_path), 1200, problems=problems)
    assert list(snaps["tank"]) == ["autosnap_new_daily"]
    assert len(commands) == 1
    assert cache.read_text() == "tank@autosnap_old_daily\tcreation\t1\t-\n"  # only the lock holder writes it
    assert len(problems) == 1 and "snapshots.txt" in problems[0]


def test_get_snapshots_reads_zfs_when_the_lock_is_held_and_there_is_no_cache(tmp_path, monkeypatch):
    # upstream reads a missing cache as no snapshots at all, so take re-takes every type of every dataset
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    commands = _zfs_replies(monkeypatch, (0, "tank@autosnap_new_daily\tcreation\t2\t-\n", ""))
    with open(run_dir / "sanoid_cacheupdate.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another process is updating the cache
        snaps = ps.get_snapshots({"t": _sec("tank")}, tmp_path / "cache", run_dir, 1200)
    assert list(snaps["tank"]) == ["autosnap_new_daily"]
    assert len(commands) == 1
    assert not (tmp_path / "cache" / "snapshots.txt").exists()  # that is the lock holder's to write


def test_add_cached_snapshots_carries_on_when_the_cache_cannot_be_written(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    cache.write_text("tank@autosnap_a_daily\tcreation\t1\t-\n")
    _failing_write(monkeypatch)
    problems = []
    ps.add_cached_snapshots({"tank@autosnap_c_daily": {"time": 5, "recursive": False}}, cache_dir, tmp_path / "run",
                            make_zfs_client(tmp_path), problems=problems)
    assert len(problems) == 1
    assert not cache.exists()  # stale now (it lacks the new snapshot): the next run lists zfs, which has it


def test_remove_cached_snapshots_leaves_a_missing_cache_missing(tmp_path):
    # upstream writes an empty cache with a fresh mtime here, which hides every snapshot from the next runs until it
    # expires: take re-takes every type of every dataset
    (tmp_path / "cache").mkdir()
    assert ps.remove_cached_snapshots(["tank@a_daily"], tmp_path / "cache", tmp_path / "run", wait=True)
    assert not (tmp_path / "cache" / "snapshots.txt").exists()


def test_remove_cached_snapshots_carries_on_when_the_cache_cannot_be_written(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    cache.write_text("tank@a_daily\tcreation\t1\t-\ntank@b_daily\tcreation\t2\t-\n")
    _failing_write(monkeypatch)
    problems = []
    assert ps.remove_cached_snapshots(["tank@a_daily"], cache_dir, tmp_path / "run", wait=True, problems=problems)
    assert len(problems) == 1
    assert not cache.exists()  # it would still list the destroyed snapshot: the next run lists zfs instead


def test_add_cached_snapshots_drops_the_cache_when_the_lock_file_cannot_be_made(tmp_path):
    # 0.1.33 raised before adding the new snapshots, so each run inside the TTL took them again (confirmed end to end)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    _ = cache.write_text("tank@autosnap_a_daily\tcreation\t1\t-\n")
    problems: list[str] = []
    ps.add_cached_snapshots({"tank@autosnap_c_daily": {"time": 5, "recursive": False}}, cache_dir,
                            _unusable_run_dir(tmp_path), make_zfs_client(tmp_path), problems=problems)
    assert not cache.exists()  # it lacks the new snapshot: the next run lists zfs, which has it
    assert len(problems) == 1


@pytest.mark.parametrize("wait", [False, True])
def test_remove_cached_snapshots_drops_the_cache_when_the_lock_file_cannot_be_made(tmp_path, wait):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    _ = cache.write_text("tank@a_daily\tcreation\t1\t-\ntank@b_daily\tcreation\t2\t-\n")
    problems: list[str] = []
    assert ps.remove_cached_snapshots(["tank@a_daily"], cache_dir, _unusable_run_dir(tmp_path), wait=wait, problems=problems)
    assert not cache.exists()  # it would still list the destroyed snapshot
    assert len(problems) == 1


@pytest.mark.parametrize(("before", "after"), [
    ("tank@a_daily\tcreation\t1\t-\ntank@b_daily\tcreation\t2\t-", "tank@b_daily\tcreation\t2\t-"),
    ("tank@a_daily\tcreation\t1\t-\ntank@a_daily\ntank@b_daily\tcreation\t2\t-\n", "tank@a_daily\ntank@b_daily\tcreation\t2\t-\n"),
    ("tank@b_daily\tcreation\t2\t-\ntank@a_daily", "tank@b_daily\tcreation\t2\t-\n"),
], ids=["kept-last-line-without-newline", "line-without-tab", "last-line-without-tab-or-newline"])
def test_remove_cached_snapshots_writes_kept_lines_back_as_read(tmp_path, before, after):
    # Parity: upstream prints each kept line exactly as `<FH>` read it, and splits a line's first column off without
    # chomping it, so a line with no tab keeps its newline there and matches no snapshot name unless it is a last line
    # without one (confirmed against real Perl). pysanoid added the missing newline and dropped the tab-less line.
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "snapshots.txt").write_text(before)
    assert ps.remove_cached_snapshots(["tank@a_daily"], cache_dir, tmp_path / "run", wait=True)
    assert (cache_dir / "snapshots.txt").read_text() == after


def test_add_cached_snapshots_carries_on_when_a_child_lookup_cannot_start(tmp_path, monkeypatch, capsys):
    # Upstream's lookup is an unchecked pipe: when `zfs list` can't start it finds no children and carries on (confirmed
    # against real Perl). pysanoid raised, which ended the run and left the cache without any of the new snapshots.
    def cannot_start(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

    monkeypatch.setattr(subprocess, "run", cannot_start)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "snapshots.txt").write_text("tank@autosnap_a_daily\tcreation\t1\t-\n")
    problems: list[str] = []
    ps.add_cached_snapshots({"tank@autosnap_c_daily": {"time": 5, "recursive": True}}, cache_dir, tmp_path / "run",
                            make_zfs_client(tmp_path), problems=problems)
    assert (cache_dir / "snapshots.txt").read_text() == (
        "tank@autosnap_a_daily\tcreation\t1\t-\ntank@autosnap_c_daily\tcreation\t5\t-\n")
    assert len(problems) == 1 and "children of 'tank'" in problems[0]
    assert "ERROR: could not run" in capsys.readouterr().err


def test_add_cached_snapshots_leaves_out_the_children_of_a_killed_lookup(tmp_path, monkeypatch):
    # A killed lookup listed part of the tree at most (see get_child_datasets): its children are left out and reported,
    # as for one that can't start. Upstream keeps the lines it printed; they are bookkeeping either way.
    _zfs_replies(monkeypatch, (-9, "tank\ntank/a\n", ""))
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "snapshots.txt").write_text("tank@autosnap_a_daily\tcreation\t1\t-\n")
    problems: list[str] = []
    ps.add_cached_snapshots({"tank@autosnap_c_daily": {"time": 5, "recursive": True}}, cache_dir, tmp_path / "run",
                            make_zfs_client(tmp_path), problems=problems)
    assert (cache_dir / "snapshots.txt").read_text() == (
        "tank@autosnap_a_daily\tcreation\t1\t-\ntank@autosnap_c_daily\tcreation\t5\t-\n")
    assert len(problems) == 1 and "killed by signal 9" in problems[0]


def test_main_cron_still_prunes_when_the_cache_add_cannot_list_children(tmp_path, monkeypatch, capsys):
    # End to end against the real upstream script, it cached the parent's line and pruned (exit 0); pysanoid stopped
    # FATAL before pruning, and its next run took the recursive snapshot again.
    confdir = write_confdir(tmp_path, "[tank]\n    recursive = zfs\n    daily = 1\n    hourly = 0\n    monthly = 0\n")
    taken: list[str] = []
    destroyed: list[str] = []
    listing = "tank/a@autosnap_old1_daily\tcreation\t1000\t-\ntank/a@autosnap_old2_daily\tcreation\t2000\t-\n"

    def fake_run(cmd: list[str], capture_output: bool = False, text: bool = False, **kwargs: object):
        if cmd[1] == "list" and taken:
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")  # the child lookup of the cache add
        out = {"list": "tank\ntank/a\n", "get": listing}.get(cmd[1], "")
        {"snapshot": taken, "destroy": destroyed}.get(cmd[1], []).append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout=out.encode(), stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--cron"])
    assert rc == 1
    assert destroyed == ["tank/a@autosnap_old1_daily"]
    assert len(taken) == 1 and taken[0].startswith("tank@autosnap_")
    assert _read_cache(tmp_path / "cache")[-1].split("\t")[0] == taken[0]  # the parent's line, for the next run
    assert "1 problem(s) this run" in capsys.readouterr().err


@pytest.mark.parametrize("op", ["add", "remove"])
def test_waiting_for_the_cache_lock_is_announced_in_upstreams_words(tmp_path, monkeypatch, capsys, op):
    # Parity with upstream's line, naming this program: only pysanoid can hold its flock. Upstream polls every 10 s and
    # repeats the line each time; flock blocks, so it is printed once.
    cache_dir, run_dir = tmp_path / "cache", tmp_path / "run"
    _write_cache(cache_dir, ["tank@a_daily\tcreation\t1\t-"])
    real_flock = fcntl.flock
    attempts: list[int] = []

    def busy_at_first(fd: int, operation: int) -> None:
        attempts.append(operation)
        if len(attempts) == 1:  # another process holds the lock when it first tries
            raise BlockingIOError(errno.EWOULDBLOCK, "Resource temporarily unavailable")
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", busy_at_first)
    if op == "add":
        ps.add_cached_snapshots({"tank@b_daily": {"time": 2, "recursive": False}}, cache_dir, run_dir,
                                make_zfs_client(tmp_path), verbose=True)
    else:
        assert ps.remove_cached_snapshots(["tank@a_daily"], cache_dir, run_dir, wait=True, verbose=True)
    out = capsys.readouterr().out
    assert out.count("INFO: waiting for cache update lock held by another pysanoid process.\n") == 1
    assert len(_read_cache(cache_dir)) == (2 if op == "add" else 0)  # and then did its work


def test_get_snapshots_retries_when_zfs_is_killed_by_a_signal(tmp_path, monkeypatch):
    # DEV-034: upstream's `$? >> 8` reads a signal as success and keeps the partial listing, so take re-takes every
    # due type of the datasets it missed
    commands = _zfs_replies(monkeypatch, (-9, "tank@autosnap_x_daily\tcreation\t5\t-\n", ""),
                            (0, "tank@autosnap_x_daily\tcreation\t5\t-\npool@autosnap_y_daily\tcreation\t6\t-\n", ""))
    snaps = ps.get_snapshots({"t": _sec("tank"), "p": _sec("pool")}, tmp_path / "cache", tmp_path / "run", 1200)
    assert commands[1] == [ps.ZFS, "get", "-Hrpt", "snapshot", "creation"]
    assert sorted(snaps) == ["pool", "tank"]


def test_get_snapshots_measures_the_cache_age_in_whole_seconds(tmp_path, monkeypatch):
    # upstream's `time() - $mtime` is whole seconds on both sides: a cache 1200.6 s old is 1200 s old, still fresh
    monkeypatch.setattr(ps.time, "time", lambda: 1_790_000_000.9)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    cache.write_text("tank@autosnap_old_daily\tcreation\t1\t-\n")
    os.utime(cache, (1_790_000_000 - 1200 + 0.3, 1_790_000_000 - 1200 + 0.3))
    commands = _zfs_replies(monkeypatch, (0, "tank@autosnap_new_daily\tcreation\t2\t-\n", ""))
    assert "autosnap_old_daily" in ps.get_snapshots({"t": _sec("tank")}, cache_dir, tmp_path / "run", 1200)["tank"]
    assert commands == []


def test_snapshot_lines_read_digits_and_word_characters_as_ascii_only():
    # upstream matches bytes, where \d and \w are ASCII only
    tilde_n = "\N{LATIN SMALL LETTER N WITH TILDE}"
    snaps = ps._parse_snapshot_lines([
        "tank@autosnap_x_daily\tcreation\t\N{ARABIC-INDIC DIGIT ONE}\N{ARABIC-INDIC DIGIT TWO}",
        f"tank@autosnap_y_{tilde_n}ly\tcreation\t5",
    ])
    assert snaps["tank"]["autosnap_x_daily"].ctime == 0
    assert snaps["tank"][f"autosnap_y_{tilde_n}ly"].snap_type == ""


@pytest.mark.parametrize("debug", [True, False])
def test_get_snapshots_prints_the_zfs_command_under_verbose_and_debug(tmp_path, monkeypatch, capsys, debug):
    _zfs_replies(monkeypatch, (0, "", ""))
    ps.get_snapshots({"a": _sec("apple"), "t": _sec("tank")}, tmp_path / "cache", tmp_path / "run", 1200,
                     verbose=True, debug=debug)
    line = "INFO: running: zfs get -Hrpt snapshot creation apple tank\n"
    assert (line in capsys.readouterr().out) == debug


def test_get_snapshots_retry_line_keeps_upstreams_spelling(tmp_path, monkeypatch, capsys):
    _zfs_replies(monkeypatch, (1, "", ""), (0, "", ""))
    ps.get_snapshots({"t": _sec("tank")}, tmp_path / "cache", tmp_path / "run", 1200)
    assert "INFO: zfs list shapshots with dataset names does not work, retrying without dataset names\n" in capsys.readouterr().out


# -- the per-type and per-path tables (upstream's getsnapsbytype and getsnapsbypath) ----

def test_snapshot_tables_measure_ages_in_whole_seconds(monkeypatch):
    # upstream's `time - ctime` is whole seconds
    monkeypatch.setattr(ps.time, "time", lambda: 1_790_000_000.9)
    snaps = ps._parse_snapshot_lines(["tank/a@autosnap_x_daily\tcreation\t1789999000\t-"])
    config = {"t": _sec("tank/a")}
    assert ps.snapshots_by_type(snaps, config)["tank/a"]["daily"]["newest"] == 1000
    assert ps.snapshots_by_path(snaps, config)["tank/a"]["newest"] == 1000


@pytest.mark.parametrize(("table_at", "plan_at", "due"), [(0.9, 1.1, False), (0.2, 0.6, True)])
def test_plan_snapshots_decides_at_the_whole_second_boundary_as_upstream(monkeypatch, table_at, plan_at, due):
    # The newest daily was made at 23:58:59, one second before the preferred 23:59. When the clock ticks over a second
    # between building the table and planning, upstream takes nothing this run and the daily on the next (confirmed
    # against real Perl); a fractional age took it at once.
    preferred = datetime(2026, 9, 24, 23, 59, tzinfo=TZ).timestamp()
    midnight = preferred + 60
    config = {"tank/a": _take_cfg("tank/a")}
    snaps = ps._parse_snapshot_lines([f"tank/a@autosnap_x_daily\tcreation\t{int(preferred) - 1}\t-"])
    monkeypatch.setattr(ps.time, "time", lambda: midnight + table_at)
    plan = ps.plan_snapshots(config, ps.snapshots_by_type(snaps, config), now=datetime.fromtimestamp(midnight + plan_at, TZ))
    assert ("tank/a" in plan) == due


def test_debug_dump_rounds_ages_as_upstream(monkeypatch, capsys):
    # upstream's whole-second age: 540 s is 0.15 h, which "%.1f" prints as 0.1 (confirmed against real Perl); 540.4 s
    # printed 0.2
    monkeypatch.setattr(ps.time, "time", lambda: 1_790_000_000.4)
    snaps = ps._parse_snapshot_lines([f"tank/a@autosnap_x_daily\tcreation\t{1_790_000_000 - 540}\t-"])
    config = {"t": _sec("tank/a")}
    ps.print_debug_dump(config, ps.snapshots_by_type(snaps, config), ps.snapshots_by_path(snaps, config))
    out = capsys.readouterr().out
    assert "(newest: 0.1 hours old)" in out
    assert "newest: 0.1 hours old, named autosnap_x_daily" in out


@pytest.mark.parametrize(("digits", "value"), [
    ("18446744073709551615", 18446744073709551615),  # 2**64 - 1, still exact
    ("18446744073709551616", 18446744073709551616.0),
    ("9" * 400, math.inf),
    ("9" * 5000, math.inf),
], ids=["2**64-1", "2**64", "400 digits", "5000 digits"])
def test_an_oversized_creation_time_reads_as_perl_reads_it(monkeypatch, digits, value):
    # Parity: exact up to 2**64 - 1, a float beyond, infinity past a float's range (confirmed against real Perl). int()
    # stopped at 4,300 digits, and an age past 308 digits overflowed: either ended the run.
    monkeypatch.setattr(ps.time, "time", lambda: 1_790_000_000.5)
    snaps = ps._parse_snapshot_lines([f"tank/a@autosnap_x_daily\tcreation\t{digits}\t-"])
    assert snaps["tank/a"]["autosnap_x_daily"].ctime == value
    assert ps.snapshots_by_type(snaps, {"t": _sec("tank/a")})["tank/a"]["daily"]["newest"] == 1_790_000_000 - value


def test_main_carries_on_past_a_cache_line_with_an_oversized_creation_time(tmp_path, monkeypatch):
    # A damaged cache line. Upstream carries on, and that dataset's dailies wait for the cache to be read from zfs again
    # (confirmed against real Perl); pysanoid once ended the run there. The line is dated far ahead of the clock, so
    # DEV-051 sets it aside, and tank/a's daily is taken too.
    others = "".join(f"    {t} = 0\n" for t in ("frequently", "hourly", "weekly", "monthly", "yearly"))
    confdir = write_confdir(tmp_path, f"[tank/a]\n    daily = 7\n{others}[tank/b]\n    daily = 7\n{others}")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "snapshots.txt").write_text(f"tank/a@autosnap_x_daily\tcreation\t{'9' * 400}\t-\n")
    planned = []
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    monkeypatch.setattr(ps, "take_snapshots",
                        lambda config, by_type, **kwargs: planned.append(sorted(ps.plan_snapshots(config, by_type))) or {})
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--take-snapshots"])
    assert rc == 0
    assert planned == [["tank/a", "tank/b"]]


def test_a_snapshot_name_holding_a_type_and_creation_is_not_read_as_that_type():
    # DEV-033: upstream's `\t*creation` reads the first two legal names as snapshots named autosnap_daily and
    # autosnap_hourly, made at the time their own digits give
    snaps = ps._parse_snapshot_lines([
        "tank/a@autosnap_dailycreation9999999999_x\tcreation\t1790000000\t-",
        "tank/a@autosnap_hourlycreation\tcreation\t1790000000\t-",
        "tank/a@autosnap_dailycreation9999999999_daily\tcreation\t1790000000\t-",  # ends in a type: read as before
    ])
    assert list(snaps["tank/a"]) == ["autosnap_dailycreation9999999999_daily"]
    assert snaps["tank/a"]["autosnap_dailycreation9999999999_daily"].ctime == 1790000000


def test_a_snapshot_named_like_a_future_daily_does_not_stop_the_dailies():
    # DEV-033: upstream reads it as a daily made in 2286, so the dataset's dailies stop for as long as it exists, and
    # prune keeps one fewer
    now = int(NOW_APRIL.timestamp())
    snaps = ps._parse_snapshot_lines([
        f"tank/a@autosnap_old_daily\tcreation\t{now - 2 * 86400}\t-",
        f"tank/a@autosnap_dailycreation9999999999_x\tcreation\t{now - 10}\t-",
    ])
    config = {"tank/a": _take_cfg("tank/a")}
    plan = ps.plan_snapshots(config, ps.snapshots_by_type(snaps, config, now=now), now=NOW_APRIL)
    assert plan["tank/a"].types == ["daily"]


def test_snapshots_made_in_the_same_second_keep_the_order_zfs_lists_them():
    # DEV-014: listing order, which is creation order; upstream's is Perl's per-process random hash order
    snaps = ps._parse_snapshot_lines([f"tank/a@autosnap_{n}_daily\tcreation\t5\t-" for n in "BAC"])
    assert ps.snapshots_by_type(snaps, {"t": _sec("tank/a")}, now=10)["tank/a"]["daily"]["sorted"] == [
        "autosnap_B_daily", "autosnap_A_daily", "autosnap_C_daily"]


# ---------------------------------------------------------------------------
# dataset selection + the dataset cache (2026-09-21 Module 4). Found by differential-testing upstream's
# own `init` (dataset-cache load at the top, rewrite at the bottom) and `getchilddatasets`, both VERBATIM
# under Perl with the real Config::IniFiles and a fake zfs, against load_config + ZfsClient on the same
# files and the same clock. Expected values are what upstream produced; the "parity lock" tests pin
# behavior that already matched. These go through a REAL executable standing in for `zfs` wherever bytes
# matter, because a faked subprocess.run cannot show CR translation or decode errors.
# ---------------------------------------------------------------------------

def _script_zfs(tmp_path, monkeypatch, out=b"", err=b"", rc=0):
    """A real executable in place of `zfs`: prints `out`, complains `err`, exits `rc`. Returns the file each call's
    arguments are appended to (it does not exist until zfs has been run)."""
    log = tmp_path / "zfs_calls.log"
    (tmp_path / "zfs_out").write_bytes(out)
    (tmp_path / "zfs_err").write_bytes(err)
    script = tmp_path / "zfs"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {log}\ncat {tmp_path}/zfs_out\ncat {tmp_path}/zfs_err >&2\nexit {rc}\n")
    script.chmod(0o755)
    monkeypatch.setattr(ps, "ZFS", str(script))
    return log


def _dataset_cache_file(tmp_path, monkeypatch, content, age=100):
    """datasets.txt holding `content`, `age` seconds old at a frozen clock. Returns (cache_dir, path)."""
    monkeypatch.setattr(ps.time, "time", lambda: 1_790_000_000.0)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    path = cache_dir / "datasets.txt"
    path.write_bytes(content)
    os.utime(path, (1_790_000_000 - age, 1_790_000_000 - age))
    return cache_dir, path


@pytest.mark.parametrize("age, rewritten", [(1200, False), (1201, True)])
def test_dataset_cache_is_left_alone_while_usable_even_when_nothing_is_recursive(tmp_path, monkeypatch, capsys, age, rewritten):
    # Upstream reads datasets.txt once, at the top of `init`, whether or not any section is recursive, and
    # rewrites it only if it was NOT usable. Reading it lazily -- only when a recursive section asked -- rewrote
    # a perfectly good cache on every run (restarting its TTL clock) and claimed in verbose mode that it had expired.
    cache_dir, path = _dataset_cache_file(tmp_path, monkeypatch, b"tank\ntank/a\ntank/a/x\n", age=age)
    zfs = ps.ZfsClient(cache_dir, 1200, force_update=False)
    ps.load_config(write_confdir(tmp_path, "[tank/b]\n    daily = 3\n[tank/c]\n    daily = 3\n"), zfs)
    zfs.flush_dataset_cache(tmp_path / "run", verbose=True)
    if rewritten:
        assert path.read_bytes() == b"tank/b\ntank/c\n"
        assert "dataset cache expired" in capsys.readouterr().out
    else:
        assert path.read_bytes() == b"tank\ntank/a\ntank/a/x\n"
        assert int(path.stat().st_mtime) == 1_790_000_000 - age
        assert "expired" not in capsys.readouterr().out


def test_a_usable_dataset_cache_answers_without_asking_zfs(tmp_path, monkeypatch):
    log = _script_zfs(tmp_path, monkeypatch, out=b"never asked\n")
    cache_dir, path = _dataset_cache_file(tmp_path, monkeypatch, b"tank\ntank/a\ntank/a/x\ntank/b\n")
    zfs = ps.ZfsClient(cache_dir, 1200, force_update=False)
    config = ps.load_config(write_confdir(tmp_path, "[tank]\n    recursive = yes\n    daily = 9\n"), zfs)
    zfs.flush_dataset_cache(tmp_path / "run")
    assert sorted(config) == ["tank", "tank/a", "tank/a/x", "tank/b"]
    assert not log.exists()
    assert path.read_bytes() == b"tank\ntank/a\ntank/a/x\ntank/b\n"


def test_a_dataset_cache_without_the_parent_falls_back_to_zfs_and_stays_as_it_was(tmp_path, monkeypatch):
    # upstream never rewrites a NON-EMPTY cache, even one it had to bypass: it stays incomplete until it expires
    _script_zfs(tmp_path, monkeypatch, out=b"tank\ntank/live\n")
    cache_dir, path = _dataset_cache_file(tmp_path, monkeypatch, b"tank/a\ntank/b\n")
    zfs = ps.ZfsClient(cache_dir, 1200, force_update=False)
    config = ps.load_config(write_confdir(tmp_path, "[tank]\n    recursive = yes\n"), zfs)
    zfs.flush_dataset_cache(tmp_path / "run")
    assert "tank/live" in config
    assert path.read_bytes() == b"tank/a\ntank/b\n"


def test_flush_dataset_cache_carries_on_when_it_cannot_write(tmp_path, monkeypatch):
    # upstream dies when it can't create the file, so a full or read-only disk stopped every snapshot and prune
    zfs = make_zfs_client(tmp_path)  # force_update: the cache is rewritten
    zfs.note_dataset("tank")
    _failing_write(monkeypatch)
    problems = []
    zfs.flush_dataset_cache(tmp_path / "run", problems=problems)
    assert len(problems) == 1
    assert not (tmp_path / "cache" / "datasets.txt").exists()


def test_flush_dataset_cache_carries_on_when_the_lock_file_cannot_be_made(tmp_path):
    zfs = make_zfs_client(tmp_path)  # force_update: the cache is rewritten
    zfs.note_dataset("tank")
    problems: list[str] = []
    zfs.flush_dataset_cache(_unusable_run_dir(tmp_path), problems=problems)
    assert len(problems) == 1 and "datasets.txt" in problems[0]
    assert not (tmp_path / "cache" / "datasets.txt").exists()


def test_dataset_cache_age_is_measured_in_whole_seconds(tmp_path, monkeypatch):
    # upstream's `time() - $mtime` is whole seconds on both sides: 1200.6 s old is 1200, still usable at a TTL of 1200
    cache_dir, path = _dataset_cache_file(tmp_path, monkeypatch, b"tank\n")
    monkeypatch.setattr(ps.time, "time", lambda: 1_790_000_000.9)
    os.utime(path, (1_790_000_000 - 1200 + 0.3, 1_790_000_000 - 1200 + 0.3))
    assert ps.ZfsClient(cache_dir, 1200, force_update=False).load_dataset_cache() == ["tank"]


def test_dataset_cache_records_each_section_path_and_the_children_of_recursive_ones(tmp_path, monkeypatch):
    _script_zfs(tmp_path, monkeypatch, out=b"tank\ntank/a\ntank/b\n")
    zfs = make_zfs_client(tmp_path)
    ps.load_config(write_confdir(tmp_path, "[tank]\n    recursive = yes\n[pool2]\n    daily = 3\n"), zfs)
    zfs.flush_dataset_cache(tmp_path / "run")
    assert (tmp_path / "cache" / "datasets.txt").read_bytes() == b"tank\ntank/a\ntank/b\npool2\n"


def test_dataset_cache_update_is_deferred_while_another_process_holds_the_lock(tmp_path, capsys):
    zfs = make_zfs_client(tmp_path)
    ps.load_config(write_confdir(tmp_path, "[tank]\n    daily = 3\n"), zfs)
    with ps.try_lock(tmp_path / "run", "sanoid_cachedatasetupdate") as (held, _):
        assert held
        zfs.flush_dataset_cache(tmp_path / "run", verbose=True)
    assert not (tmp_path / "cache" / "datasets.txt").exists()
    # upstream's words, naming this program: only pysanoid can hold its flock
    assert capsys.readouterr().out == (
        "INFO: deferring dataset cache update - valid cache update lock held by another pysanoid process.\n")


def test_dataset_cache_lines_are_split_on_newline_only(tmp_path, monkeypatch):
    # Perl's <FH> + chomp keeps a VT, NEL or U+2028 inside a name; str.splitlines() cut the name there and
    # invented a child (`tank/a`) that upstream never sees
    cache_dir, _ = _dataset_cache_file(tmp_path, monkeypatch, "tank\ntank/a\x0bb\ntank/c\x85d\ntank/e\u2028f\n".encode())
    zfs = ps.ZfsClient(cache_dir, 1200, force_update=False)
    assert zfs.get_child_datasets("tank") == ["tank/a\x0bb", "tank/c\x85d", "tank/e\u2028f"]


def test_a_dataset_cache_with_crlf_endings_never_finds_its_parent(tmp_path, monkeypatch):
    # upstream chomps "\n" only, so the parent reads "tank\r", never equals "tank", and the cache is not trusted
    log = _script_zfs(tmp_path, monkeypatch, out=b"tank\ntank/live\n")
    cache_dir, _ = _dataset_cache_file(tmp_path, monkeypatch, b"tank\r\ntank/a\r\n")
    zfs = ps.ZfsClient(cache_dir, 1200, force_update=False)
    assert zfs.get_child_datasets("tank") == ["tank/live"]
    assert log.exists()


def test_a_non_utf8_byte_in_the_dataset_cache_is_kept_not_fatal(tmp_path, monkeypatch):
    cache_dir, _ = _dataset_cache_file(tmp_path, monkeypatch, b"tank\ntank/caf\xe9\n")
    zfs = ps.ZfsClient(cache_dir, 1200, force_update=False)
    assert zfs.get_child_datasets("tank") == [b"tank/caf\xe9".decode("utf-8", "surrogateescape")]


def test_a_non_utf8_section_path_reaches_the_dataset_cache_byte_for_byte(tmp_path):
    # legal in upstream's byte-string world; the strict UTF-8 write raised at flush time, before anything was snapshotted
    confdir = write_confdir(tmp_path, "")
    (confdir / "sanoid.conf").write_bytes(b"[tank/caf\xe9]\n    daily = 3\n")
    zfs = make_zfs_client(tmp_path)
    ps.load_config(confdir, zfs)
    zfs.flush_dataset_cache(tmp_path / "run")
    assert (tmp_path / "cache" / "datasets.txt").read_bytes() == b"tank/caf\xe9\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 file")
def test_an_unreadable_dataset_cache_is_treated_as_no_cache(tmp_path, monkeypatch):
    # upstream's `open` failure is ignored: @cachedatasets just stays empty
    _script_zfs(tmp_path, monkeypatch, out=b"tank\ntank/live\n")
    cache_dir, path = _dataset_cache_file(tmp_path, monkeypatch, b"tank\ntank/a\n")
    path.chmod(0)
    zfs = ps.ZfsClient(cache_dir, 1200, force_update=False)
    assert zfs.get_child_datasets("tank") == ["tank/live"]


def test_get_child_datasets_is_byte_exact_through_a_real_pipe(tmp_path, monkeypatch):
    # chomp takes off "\n" only, so upstream keeps the CR; text-mode capture translated it away
    _script_zfs(tmp_path, monkeypatch, out=b"tank\r\ntank/a\r\n")
    assert make_zfs_client(tmp_path).get_child_datasets("tank") == ["tank/a\r"]


def test_get_child_datasets_lets_zfss_complaint_through(tmp_path, monkeypatch, capsys):
    # upstream leaves zfs's stderr on the terminal: a recursive section naming a dataset that is not there
    # says so in the journal instead of quietly managing no children
    _script_zfs(tmp_path, monkeypatch, err=b"cannot open 'tank/nope': dataset does not exist\n", rc=1)
    assert make_zfs_client(tmp_path).get_child_datasets("tank/nope") == []
    assert "cannot open 'tank/nope': dataset does not exist" in capsys.readouterr().err


def test_a_dataset_cache_holding_a_nul_byte_counts_as_no_cache(tmp_path, monkeypatch):
    # DEV-019: upstream uses it and cuts every command a child's name reaches short at the NUL.
    log = _script_zfs(tmp_path, monkeypatch, out=b"tank\ntank/a\ntank/b\n")
    cache_dir, path = _dataset_cache_file(tmp_path, monkeypatch, b"tank\ntank/a\0x\ntank/b\n")
    zfs = ps.ZfsClient(cache_dir, 1200, force_update=False)
    config = ps.load_config(write_confdir(tmp_path, "[tank]\n    recursive = yes\n"), zfs)
    zfs.flush_dataset_cache(tmp_path / "run")
    assert sorted(config) == ["tank", "tank/a", "tank/b"]
    assert log.exists()  # the children came from zfs
    assert path.read_bytes() == b"tank\ntank/a\ntank/b\n"  # and the damaged file was rewritten


@pytest.mark.parametrize(("rc", "out"), [(-9, "tank\n"), (-9, ""), (1, "tank\ntank/a\n")],
                         ids=["killed-after-the-parent", "killed-before-any-output", "failed-partway"])
def test_a_child_lookup_that_does_not_finish_cleanly_is_an_error(tmp_path, monkeypatch, rc, out):
    # DEV-018: upstream takes whatever came back for all the children. A lookup that fails having listed nothing
    # still reads as no children (see the test above): its parent isn't there.
    _zfs_replies(monkeypatch, (rc, out, ""))
    with pytest.raises(ps.SanoidError, match="so the children of 'tank' aren't known"):
        make_zfs_client(tmp_path).get_child_datasets("tank")


@pytest.mark.parametrize("lookup", ["killed after listing the parent", "cannot start"])
def test_main_does_not_prune_when_the_child_lookup_does_not_finish(tmp_path, monkeypatch, capsys, lookup):
    # [tank/a] has a section of its own, so it inherits hourly = 100 only through tank's recursion. When the lookup comes
    # back short, upstream sets tank/a up from the defaults (hourly = 48) and destroys 12 of its 60 hourlies, either
    # way (confirmed against real Perl). DEV-018: both stop the run here.
    confdir = write_confdir(tmp_path, "[tank]\n    recursive = yes\n    hourly = 100\n    daily = 0\n    monthly = 0\n"
                                      + "[tank/a]\n    daily = 0\n")
    now = 1_790_000_000
    monkeypatch.setattr(ps.time, "time", lambda: float(now))
    listing = "".join(f"tank/a@autosnap_h{i:02d}_hourly\tcreation\t{now - i * 3600}\t-\n" for i in range(1, 61))
    destroyed: list[str] = []

    def fake_run(cmd: list[str], capture_output: bool = False, text: bool = False, **kwargs: object):
        if cmd[1] == "list":
            if lookup == "cannot start":
                raise OSError(errno.EAGAIN, "Resource temporarily unavailable")
            return subprocess.CompletedProcess(cmd, -9, stdout=b"tank\n", stderr=b"")
        if cmd[1] == "destroy":
            destroyed.append(cmd[-1])
        out = listing if cmd[1] == "get" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out.encode(), stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--prune-snapshots"])
    assert destroyed == []
    assert rc == 1
    assert "FATAL:" in capsys.readouterr().err


def test_debug_says_when_the_dataset_cache_is_used(tmp_path, monkeypatch, capsys):
    # upstream's line, as its --debug prints it (confirmed against real Perl)
    cache_dir, _ = _dataset_cache_file(tmp_path, monkeypatch, b"tank\ntank/a\n", age=37)
    assert ps.ZfsClient(cache_dir, 1200, force_update=False, debug=True).load_dataset_cache() == ["tank", "tank/a"]
    assert capsys.readouterr().out == (
        "DEBUG: dataset cache not expired (37 seconds old with TTL of 1200): pulling dataset list from cache.\n")


def test_debug_shows_each_child_lookup_that_asks_zfs(tmp_path, monkeypatch, capsys):
    # upstream's line, naming zfs as upstream runs it, bare, whatever path pysanoid found it at (confirmed against real
    # Perl); pysanoid printed that path
    _script_zfs(tmp_path, monkeypatch, out=b"tank\ntank/a\n")
    zfs = ps.ZfsClient(tmp_path / "cache", 1200, force_update=True, debug=True)
    assert zfs.get_child_datasets("tank") == ["tank/a"]
    assert capsys.readouterr().out == (
        "DEBUG: getting list of child datasets on tank using  zfs list -o name -t filesystem,volume -Hr tank |...\n")


def test_config_loading_warns_in_upstreams_words(tmp_path, monkeypatch, capsys):
    # upstream's `warn` text, with no prefix (pysanoid added "WARN: ")
    _script_zfs(tmp_path, monkeypatch, out=b"tank\ntank/a\n")
    confdir = write_confdir(tmp_path, "[template_default]\n    recursive = yes\n"
                                      + "[tank]\n    recursive = zfs\n    daily = 3\n    daily = 4\n"
                                      + "[tank/a]\n    autosnap = yes\n")
    ps.load_config(confdir, make_zfs_client(tmp_path))
    assert capsys.readouterr().err.splitlines() == [
        "duplicate key 'daily' in section 'tank', using the value from the first occurence and ignoring the others.",
        "ignored key 'recursive' from user-defined default template.",
        "ignored autosnap configuration for 'tank/a' because it's part of a zfs recursion.",
    ]


def test_get_snapshots_lets_the_retrys_complaint_through_too(tmp_path, monkeypatch, capsys):
    _zfs_replies(monkeypatch, (1, "", "first: cannot open\n"), (1, "", "second: internal error\n"))
    ps.get_snapshots({"t": _sec("tank")}, tmp_path / "cache", tmp_path / "run", 1200)
    err = capsys.readouterr().err
    assert "first: cannot open" in err
    assert "second: internal error" in err


# ---------------------------------------------------------------------------
# script-level flow and option parsing (2026-09-21 Module 5). Found by differential-testing upstream's
# main body (lines 1-144, VERBATIM, its subs stubbed) and its real Getopt::Long call against main() /
# parse_args on the same argv: 24 hand cases and 400 random flag combinations for the flow, 78 dialect
# probes and 3,600 random command lines for the grammar. The "parity lock" tests pin behavior that
# already matched; DEVIATIONS.md lists the deliberate differences.
# ---------------------------------------------------------------------------

def _main_calls(tmp_path, monkeypatch, *flags):
    """main() over a real config dir with everything downstream faked. Returns (exit code, [(step, what it was given)])."""
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n")
    calls = []

    def step(name, **given):
        calls.append((name, given))

    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    monkeypatch.setattr(ps, "get_snapshots", lambda config, cache_dir, run_dir, cache_ttl, force_update=False, verbose=False,
                        debug=False, problems=None: step("snapshots", ttl=cache_ttl, force=force_update, verbose=verbose) or {})
    monkeypatch.setattr(ps, "print_debug_dump", lambda *a, **k: step("dump"))
    monkeypatch.setattr(ps, "take_snapshots", lambda config, by_type, *, readonly=False, verbose=False, problems=None:
                        step("take", verbose=verbose, readonly=readonly) or {})
    monkeypatch.setattr(ps, "prune_snapshots", lambda config, snaps, by_type, cache_dir, run_dir, *, readonly=False, verbose=False,
                        problems=None: step("prune", verbose=verbose, readonly=readonly) or [])
    monkeypatch.setattr(ps, "add_cached_snapshots", lambda *a, **k: None)
    monkeypatch.setattr(ps, "remove_cached_snapshots", lambda *a, **k: None)
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"), *flags])
    return rc, calls


@pytest.mark.parametrize("flags, expected", [
    (("--debug", "--take-snapshots"), [("take", True)]),
    (("--debug", "--prune-snapshots"), [("prune", True)]),
    (("--debug", "--cron"), [("take", True), ("prune", True)]),
    (("--debug", "--cron", "--quiet"), [("take", False), ("prune", False)]),  # quiet is applied after debug, as upstream
])
def test_debug_implies_verbose_for_the_take_and_prune_that_follow(tmp_path, monkeypatch, flags, expected):
    # upstream: `if ($args{'debug'}) { $args{'verbose'}=1; blabber(@params); }` -- the flag reaches everything after it
    rc, calls = _main_calls(tmp_path, monkeypatch, *flags)
    assert rc == 0
    assert [(name, given["verbose"]) for name, given in calls if name in ("take", "prune")] == expected


def test_a_negative_cache_ttl_is_an_error_message_not_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert ps.main(["--configdir", str(tmp_path), "--cache-dir", str(tmp_path / "c"), "--run-dir", str(tmp_path / "r"),
                    "--take-snapshots", "--cache-ttl=-3"]) == 1
    assert capsys.readouterr().err == "ERROR: cache-ttl needs to be positive!\n"  # upstream's die, word for word


def test_the_force_prune_warning_is_upstreams(capsys):
    # DEV-027: upstream's warn adds Perl's " at <script> line 59."
    _ = ps.parse_args(["--force-prune"])
    assert capsys.readouterr().err == "WARN: --force-prune argument is deprecated and its behavior is now standard\n"


@pytest.mark.parametrize("flags, expected", [
    ((), [("snapshots", 1200), ("take", None), ("prune", None)]),  # nothing but the directories: the default is --cron --verbose
    (("--take-snapshots", "--verbose"), [("snapshots", 1200), ("take", None)]),  # the production take unit
    (("--prune-snapshots", "--verbose"), [("snapshots", 1200), ("prune", None)]),  # the production prune unit
    (("--cron",), [("snapshots", 1200), ("take", None), ("prune", None)]),
    (("--take", "--prune"), [("snapshots", 1200), ("take", None), ("prune", None)]),  # unique prefixes, as Getopt::Long
    (("--force-update",), [("snapshots", 1200)]),
    (("--debug",), [("snapshots", 1200), ("dump", None)]),
    (("--verbose",), []),  # any single option switches the default off: nothing runs
    (("--quiet",), []),
    (("--readonly",), []),
    (("--cache-ttl=5",), []),
    (("--force-prune",), []),
    (("--cron", "--cache-ttl=5"), [("snapshots", 5), ("take", None), ("prune", None)]),
    (("--cron", "--cache-ttl=0"), [("snapshots", 0), ("take", None), ("prune", None)]),  # DEV-015: upstream's truthiness test keeps 1200
])
def test_main_runs_the_same_steps_as_upstream_for_each_flag_combination(tmp_path, monkeypatch, flags, expected):
    rc, calls = _main_calls(tmp_path, monkeypatch, *flags)
    assert rc == 0
    assert [(name, given.get("ttl")) for name, given in calls] == expected


@pytest.mark.parametrize("flags, verbose", [
    ((), True),  # the default cron run is verbose
    (("--cron",), False),  # naming a flag turns the default off, verbosity with it
    (("--cron", "--verbose"), True),
    (("--cron", "--verbose", "--quiet"), False),
])
def test_main_verbosity_follows_the_flags_given(tmp_path, monkeypatch, flags, verbose):
    _, calls = _main_calls(tmp_path, monkeypatch, *flags)
    assert [given["verbose"] for name, given in calls if name in ("take", "prune")] == [verbose, verbose]


@pytest.mark.parametrize("value, expected", [
    ("5", 5), ("+5", 5), ("007", 7), ("1_000", 1000), ("_5", 5), ("5_", 5), ("+_5", 5), ("1__000", 1000), ("5\n", 5),
])
def test_cache_ttl_accepts_what_getopt_long_accepts(value, expected):
    assert ps.parse_args([f"--cache-ttl={value}"]).cache_ttl == expected


@pytest.mark.parametrize("value", [" 5", "5 ", "\u0663", "\uff11\uff12", "1.5", "abc", "", "0x10", "1e3", "--5", "1,000", "_", "+"])
def test_cache_ttl_refuses_what_getopt_long_refuses(value):
    # int() alone took the padded and non-ASCII forms
    with pytest.raises(SystemExit) as exc:
        ps.parse_args([f"--cache-ttl={value}"])
    assert exc.value.code == 2


@pytest.mark.parametrize("argv", [["--configdir="], ["--cache-dir="], ["--run-dir="], ["--configdir", ""], ["--run-dir", ""]])
def test_an_empty_directory_option_is_refused(argv):
    # DEV-024: Path("") is ".": caches and locks dropped wherever the run starts. Upstream refuses the `--x=` form.
    with pytest.raises(SystemExit) as exc:
        ps.parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("argv", [
    ["-cron"], ["-verbose"], ["-take-snapshots"], ["-configdir=/x"], ["-configdir", "/x"],  # single-dash long options
    ["--Cron"], ["--VERBOSE"],  # any case
    ["+verbose"], ["-?"], ["--"], ["--v"], ["--ver"],  # ...and the other odd ones (--v/--ver: ambiguous with --version here)
    ["--configdir", "--cron"],  # upstream reads `--cron` as the directory
    ["--monitor-snapshots"], ["--monitor-health"], ["--monitor-capacity"],  # not implemented: the Nagios checks are out of scope
])
def test_spellings_only_getopt_long_accepts_are_usage_errors(argv):
    # DEV-022 and DEV-026: the real Getopt::Long accepts each of these (the --monitor-* ones by upstream's own option
    # list); here each is a usage error. Pinned so that changing it is a decision, not an accident.
    with pytest.raises(SystemExit) as exc:
        ps.parse_args(argv)
    assert exc.value.code == 2


def test_arguments_that_are_not_options_are_refused():
    # DEV-023: upstream leaves them in @ARGV unread, so `sanoid readonly --prune-snapshots` prunes for real.
    with pytest.raises(SystemExit) as exc:
        ps.parse_args(["readonly", "--prune-snapshots"])
    assert exc.value.code == 2


def test_the_production_command_lines_parse_as_upstream_would():
    take = ps.parse_args(["--configdir=/usr/local/etc/sanoid", "--take-snapshots", "--verbose"])
    assert (take.configdir, take.take_snapshots, take.prune_snapshots, take.cron, take.verbose) == ("/usr/local/etc/sanoid", True, False, False, True)
    assert (take.cache_dir, take.run_dir) == ("/var/cache/sanoid", "/var/run/sanoid")
    prune = ps.parse_args(["--configdir=/usr/local/etc/sanoid", "--prune-snapshots", "--verbose"])
    assert (prune.take_snapshots, prune.prune_snapshots, prune.cron) == (False, True, False)
    default = ps.parse_args(["--configdir", "/x", "--cache-dir", "/y", "--run-dir", "/z"])
    assert (default.cron, default.verbose) == (True, True)


def test_stdout_passes_non_utf8_bytes_through_instead_of_ending_the_run(tmp_path, monkeypatch):
    # A section named with a non-UTF-8 byte is legal in upstream's byte-string world. Printing it under a strict UTF-8
    # locale raised UnicodeEncodeError on the first verbose line, ending the run before any other dataset was snapshotted.
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="utf-8", errors="strict"))
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n")
    name = b"tank/caf\xe9".decode("utf-8", "surrogateescape")

    def fake_take(config, by_type, *, readonly=False, verbose=False, problems=None):
        print(f"taking snapshot {name}@autosnap_x_daily")
        return {}

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(ps, "get_snapshots", lambda *a, **k: {})
    monkeypatch.setattr(ps, "take_snapshots", fake_take)
    monkeypatch.setattr(ps, "add_cached_snapshots", lambda *a, **k: None)
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--take-snapshots"])
    sys.stdout.flush()
    assert rc == 0
    assert b"taking snapshot tank/caf\xe9@autosnap_x_daily\n" in raw.getvalue()


def test_stdout_is_line_buffered_so_the_journal_keeps_the_order(tmp_path, monkeypatch):
    # Perl flushes STDOUT before every command it starts. Block-buffered, pysanoid's lines reached the journal at exit,
    # after everything its hooks and zfs had printed (confirmed through a pipe).
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="utf-8"))
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n")
    written_before_the_next_command: list[bytes] = []

    def fake_take(*_: object, **__: object) -> dict[str, ps.Stats]:
        print("taking snapshot tank/data@autosnap_x_daily")
        written_before_the_next_command.append(raw.getvalue())
        return {}

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(ps, "get_snapshots", lambda *a, **k: {})
    monkeypatch.setattr(ps, "take_snapshots", fake_take)
    monkeypatch.setattr(ps, "add_cached_snapshots", lambda *a, **k: None)
    assert ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                    "--take-snapshots"]) == 0
    assert written_before_the_next_command == [b"taking snapshot tank/data@autosnap_x_daily\n"]


def _closed_pipe() -> io.TextIOWrapper:
    """A text stream on a real descriptor nobody reads, line-buffered as the interpreter's own stderr is: every line
    written fails with EPIPE (Python ignores SIGPIPE)."""
    read_end, write_end = os.pipe()
    os.close(read_end)
    return open(write_end, "w", encoding="utf-8", buffering=1)


@pytest.mark.parametrize("closed, rc", [(("stdout",), 1), (("stderr",), 0), (("stdout", "stderr"), 1)])
def test_output_that_cannot_be_written_does_not_stop_the_run(tmp_path, monkeypatch, capsys, closed: tuple[str, ...], rc: int):
    # P7: Perl's print and warn don't throw: upstream carries on, and exits 1 when its last flush of stdout fails
    # (confirmed end to end with a closed pipe and SIGPIPE ignored, as systemd sets it, and with a log that can't grow).
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n    hourly = 0\n    monthly = 0\n")
    taken, _ = _fake_zfs_pool(monkeypatch, [f"tank/data@autosnap_old_daily\tcreation\t{int(time.time()) - 3 * 86400}\t-"])
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    streams = {name: _closed_pipe() for name in closed}
    for name, stream in streams.items():
        monkeypatch.setattr(sys, name, stream)
    try:
        code = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                        "--take-snapshots", "--verbose", "--force-prune"])
    finally:
        monkeypatch.undo()  # pytest's own streams back before these descriptors close
        for stream in streams.values():
            with contextlib.suppress(OSError):
                stream.close()
    assert code == rc
    assert len(taken) == 1
    err = capsys.readouterr().err
    if closed == ("stdout",):
        assert "ERROR: could not write to stdout ([Errno 32] Broken pipe); the run carried on without it\n" in err


def test_output_is_written_as_read_whatever_the_locale(tmp_path, monkeypatch):
    # P7: Perl writes the bytes it read. Under a latin-1 locale a verbose line naming a section latin-1 can't spell
    # would raise UnicodeEncodeError, so the datasets after it would get no snapshot.
    out = tmp_path / "out"
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n")
    name = "tank/日本"

    def fake_take(*_: object, **__: object) -> dict[str, ps.Stats]:
        print(f"taking snapshot {name}@autosnap_x_daily")
        return {}

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(ps, "get_snapshots", lambda *a, **k: {})
    monkeypatch.setattr(ps, "take_snapshots", fake_take)
    monkeypatch.setattr(ps, "add_cached_snapshots", lambda *a, **k: None)
    with open(out, "w", encoding="latin-1") as stream:
        monkeypatch.setattr(sys, "stdout", stream)
        try:
            code = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir",
                            str(tmp_path / "run"), "--take-snapshots"])
        finally:
            monkeypatch.undo()
    assert code == 0
    assert f"taking snapshot {name}@autosnap_x_daily\n".encode() in out.read_bytes()


# ---------------------------------------------------------------------------
# hooks: the command line and the timeout upstream's system() and alarm() give a hook, and what a timeout that fires
# costs. Expected values were read back from real Perl; dev/diff_hooks.py fuzzes the same space.
# ---------------------------------------------------------------------------

def _hook_command(cmd: str | list[str], kwargs: dict[str, object]) -> str | None:
    """The hook command line in a subprocess.run call run_hook_script made (or a shell=True string), else None. A hook
    is the one call given env=: the words Perl execs itself, or /bin/sh -c and the line it runs."""
    if isinstance(cmd, str):
        return cmd if kwargs.get("shell") else None
    if "env" not in kwargs:
        return None
    if cmd[:2] != ["/bin/sh", "-c"]:
        return " ".join(cmd)
    return " ".join(cmd[3:]) if cmd[2].startswith('exec "$0" "$@"') else cmd[2]


def _capture_hook_run(monkeypatch, returncode: int = 0) -> list[tuple[str, object]]:
    """Patches subprocess.run: records each hook call as (command line, timeout given). Every other call (a `zfs
    snapshot` or `zfs destroy`) succeeds, so a caller further up isn't blocked by this fake."""
    calls: list[tuple[str, object]] = []

    def fake_run(cmd: str | list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        command = _hook_command(cmd, kwargs)
        if command is None:
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        calls.append((command, kwargs.get("timeout")))
        return subprocess.CompletedProcess(cmd, returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


@pytest.mark.parametrize(("value", "seconds"), [
    ("5", 5), ("05", 5), ("5 # seconds", 5), ("1e3", 1000),
    ("5s", 5), ("5,0", 5), ("1_0", 1), ("1.5", 1), ("5.9", 5),  # Perl's number, in whole seconds
    ("0.5", None), ("1e-3", None), ("abc", None), ("0x10", None), ("00", None), ("0.0", None), ("0", None),
    ("+5", None), (" 5", None), ("-5", None), ("", None),  # upstream's string gate: none of these sorts after "0"
    ("inf", None), ("nan", None),  # upstream's alarm() gets whatever number its variable last held
    ("4294967297", 1), ("4294967296", None), ("2147483648", None), ("9007199254740993", 1),  # a C int
])
def test_run_hook_script_sets_the_timeout_upstreams_alarm_sets(monkeypatch, value, seconds):
    # P5, read back from real Perl with `alarm $timeout; alarm 0`
    calls = _capture_hook_run(monkeypatch)
    ps.run_hook_script({"pre_snapshot_script": "/bin/true", "script_timeout": value}, "pre_snapshot_script", "tank/a", {})
    assert calls == [("/bin/true", seconds)]


@pytest.mark.parametrize(("value", "reading"), [
    ("5", None), ("05", None), ("5 # seconds", None), ("0", None), ("00", None),  # guards: plain whole numbers
    ("5s", "reading it as 5"), ("1_0", "reading it as 1"), ("0.5", "running its hooks without a timeout"),
    ("+5", "running its hooks without a timeout"), ("", "running its hooks without a timeout"),
])
def test_a_script_timeout_that_is_not_a_plain_whole_number_is_reported(monkeypatch, capsys, value, reading):
    # P5, reported, as a malformed retention count is: read as upstream reads it, reported once a run however many
    # hooks read it
    _ = _capture_hook_run(monkeypatch)
    problems: list[str] = []
    cfg = _take_cfg("tank/a", pre_snapshot_script="/bin/true", post_snapshot_script="/bin/true", script_timeout=value)
    ps.take_snapshots({"tank/a": cfg}, {}, now=NOW_APRIL, problems=problems)
    if reading is None:
        assert problems == []
    else:
        assert problems == [f"'script_timeout' for 'tank/a' must be a whole number of seconds, got {value!r}; {reading}"]
        assert capsys.readouterr().err.count("ERROR: 'script_timeout'") == 1


def test_a_missing_script_timeout_runs_the_hook_untimed_with_a_warning(monkeypatch, capsys):
    # P5: upstream reads undef as no timeout, with Perl's "uninitialized" warning
    calls = _capture_hook_run(monkeypatch)
    ps.run_hook_script({"pre_snapshot_script": "/bin/true"}, "pre_snapshot_script", "tank/a", {})
    assert calls == [("/bin/true", None)]
    assert "WARN: 'script_timeout' is not set for 'tank/a'" in capsys.readouterr().err


def test_a_hook_that_outlives_its_timeout_is_killed_and_counts_as_failed(capsys):
    # guard for DEV-042: upstream's alarm() kills sanoid itself instead
    start = time.monotonic()
    assert ps.run_hook_script({"pre_pruning_script": "sleep 30", "script_timeout": "1"}, "pre_pruning_script", "tank/a", {}) == -1
    assert time.monotonic() - start < 10
    assert "WARN: pre_pruning_script didn't finish in the allowed time!" in capsys.readouterr().err


@pytest.mark.parametrize("how", ["exec'd", "through the shell"])
def test_a_hook_starts_with_sigpipe_at_its_default_and_c_utf8(tmp_path, how):
    # DEV-046: started as systemd starts a service (SIGPIPE ignored, no locale), pysanoid's hooks get what Python sets
    # up: SIGPIPE at its default, so a hook's own pipeline stops quietly, and LC_CTYPE=C.UTF-8. Upstream's inherit
    # sanoid's own (dev/diff_hooks.py checks that side). The probe writes into a pipe whose reader has gone: at the
    # default it is killed there, ignored it would carry on and say so.
    probe = tmp_path / "probe"
    _ = probe.write_text(f"#!/bin/sh\n{{ sleep 0.5; echo x 2>/dev/null; echo carried on >> {tmp_path / 'seen'}; }} | true\n"
                         f"echo \"LC_CTYPE=${{LC_CTYPE-unset}}\" >> {tmp_path / 'seen'}\n")
    probe.chmod(0o755)
    command = str(probe) if how == "exec'd" else f"{probe} ; true"
    code = ("import sys; import pysanoid as ps\n"
            "ps.run_hook_script({'pre_snapshot_script': sys.argv[1], 'script_timeout': '10'}, 'pre_snapshot_script',"
            " 'tank/a', {})\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LC_", "LANG")) and k != "PYTHONCOERCECLOCALE"}
    _ = subprocess.run([sys.executable, "-c", code, command], cwd=tmp_path, check=True, timeout=30,
                       env={**env, "PYTHONPATH": str(Path(ps.__file__).parent)},
                       preexec_fn=lambda: signal.signal(signal.SIGPIPE, signal.SIG_IGN))
    assert (tmp_path / "seen").read_text() == "LC_CTYPE=C.UTF-8\n"


@pytest.mark.parametrize("where", ["a veto hook", "a zfs destroy"])
def test_ctrl_c_while_a_command_runs_stops_the_prune(tmp_path, monkeypatch, where):
    # DEV-044: upstream's system() ignores SIGINT while it waits, so Ctrl-C kills only the command and the run goes on
    # destroying. Here the run ends there: tank/b is never touched. (dev/diff_hooks.py checks upstream's side.)
    interrupt = f"kill -INT {os.getpid()}; sleep 3"
    log = tmp_path / "zfs_calls.log"
    zfs = tmp_path / "zfs"
    _ = zfs.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {log}\n" + (f"{interrupt}\n" if where == "a zfs destroy" else ""))
    zfs.chmod(0o755)
    monkeypatch.setattr(ps, "ZFS", str(zfs))
    (_, snaps_a, by_type_a, cfg_a), (_, snaps_b, by_type_b, cfg_b) = _prune_case("tank/a", 3), _prune_case("tank/b", 3)
    if where == "a veto hook":
        cfg_a = dict(cfg_a, pre_pruning_script=interrupt)
    with pytest.raises(KeyboardInterrupt):
        _ = ps.prune_snapshots({"tank/a": cfg_a, "tank/b": cfg_b}, {"tank/a": snaps_a, "tank/b": snaps_b},
                               {"tank/a": by_type_a, "tank/b": by_type_b}, tmp_path / "cache", tmp_path / "run")
    calls = log.read_text().splitlines() if log.exists() else []
    assert calls == ([f"destroy tank/a@{_prune_case('tank/a', 3)[0][0]}"] if where == "a zfs destroy" else [])


def test_take_snapshots_negative_script_timeout_does_not_silently_stop_protecting_the_dataset(monkeypatch):
    # guard: subprocess.run(timeout=<negative>) raises before the hook starts, a false failure that, with
    # no_inconsistent_snapshot, would skip the dataset every run. Upstream's gate reads -5 as no timeout.
    calls = _capture_hook_run(monkeypatch)
    cfg = _take_cfg("tank/a", pre_snapshot_script="/bin/true", no_inconsistent_snapshot=True, script_timeout="-5")
    taken = ps.take_snapshots({"tank/a": cfg}, {}, now=NOW_APRIL)
    assert list(taken) == ["tank/a@autosnap_2026-04-21_05:37:00_daily"]  # still protected, not silently skipped
    assert calls == [("/bin/true", None)]  # the hook ran, untimed


def _argv_hook(tmp_path: Path) -> tuple[Path, Path]:
    """A real hook that appends the arguments it was given, as JSON, to a log. Returns (hook, log)."""
    log = tmp_path / "hook_argv.log"
    hook = tmp_path / "argv_hook"
    _ = hook.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
    hook.chmod(0o755)
    return hook, log


@pytest.mark.parametrize(("args", "expected"), [
    ("a b", ["a", "b"]),  # guard
    ("--flag # quiesce db", ["--flag", "#", "quiesce", "db"]),  # no metacharacter: Perl execs the words itself
    ("a\x0bb\rc", ["a", "b", "c"]),  # split on Perl's whitespace, not the shell's
    ("a\tb", ["a", "b"]),  # guard
    ("'a b' # c", ["a b"]),  # guard: a quote is a metacharacter, so /bin/sh -c runs it and # starts a comment
    ("a 2>&1", ["a"]),  # guard: Perl does a final 2>&1 itself
])
def test_a_hook_command_runs_as_perls_system_runs_it(tmp_path, args, expected):
    # P6: through /bin/sh -c, a trailing comment would never reach the hook
    hook, log = _argv_hook(tmp_path)
    cfg = {"pruning_script": f"{hook} {args}", "script_timeout": "5"}
    assert ps.run_hook_script(cfg, "pruning_script", "tank/a", {}) == 0
    assert [json.loads(line) for line in log.read_text().splitlines()] == [expected]


@pytest.mark.parametrize("command", ["exit 0", ":", "! /bin/false"])
def test_a_hook_perl_cannot_start_is_a_failed_hook(command):
    # P6: upstream's system() execs the first word, a shell keyword or builtin here, so the hook can't start: a veto
    # for a pre_pruning_script. /bin/sh would run it, and it would succeed.
    assert ps.run_hook_script({"pre_pruning_script": command, "script_timeout": "5"}, "pre_pruning_script", "tank/a", {}) != 0


def test_take_snapshots_post_hook_snapname_is_prefixed_but_pre_hook_snapname_is_not(monkeypatch):
    # P3: upstream's post_snapshot_script sees SANOID_SNAPNAME as "path@name", its pre_snapshot_script bare "name".
    envs: dict[str, dict[str, str]] = {}

    def fake_run(cmd, capture_output=False, text=False, env=None, **kwargs):
        if env is not None:  # the hook calls pass env=...; the real `zfs snapshot` call doesn't
            envs["pre" if env.get("SANOID_SCRIPT") == "pre" else "post"] = dict(env)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = _take_cfg("tank/a", pre_snapshot_script="/bin/true", post_snapshot_script="/bin/true")
    ps.take_snapshots({"tank/a": cfg}, {}, now=NOW_APRIL)
    assert envs["pre"]["SANOID_SNAPNAME"] == "autosnap_2026-04-21_05:37:00_daily"
    assert envs["post"]["SANOID_SNAPNAME"] == "tank/a@autosnap_2026-04-21_05:37:00_daily"
    # SANOID_SNAPNAMES (plural) is unaffected on both sides -- frozen before upstream's mutation
    assert envs["pre"]["SANOID_SNAPNAMES"] == envs["post"]["SANOID_SNAPNAMES"] == "autosnap_2026-04-21_05:37:00_daily"


def _hook_envs_with_snapshot_failures(monkeypatch, fail: dict[str, str]) -> dict[str, dict[str, str]]:
    """Patches subprocess.run: `zfs snapshot NAME` fails with fail[NAME] as stderr (succeeds if NAME isn't
    listed); a hook call records its env under its SANOID_SCRIPT ("pre"/"post")."""
    envs: dict[str, dict[str, str]] = {}

    def fake_run(cmd, capture_output=False, text=False, env=None, **kwargs):
        if env is not None:
            envs[env["SANOID_SCRIPT"]] = dict(env)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        err = fail.get(cmd[-1])
        return subprocess.CompletedProcess(cmd, 1 if err else 0, stdout=b"", stderr=(err or "").encode())

    monkeypatch.setattr(subprocess, "run", fake_run)
    return envs


EXISTS = "cannot create snapshot: dataset already exists\n"


def test_take_snapshots_post_hook_snapname_carries_the_first_snapshots_dst_rename(monkeypatch):
    # Parity: upstream's dst_ retry renames $snapshots[0] in place, and post_snapshot_script reads it after
    envs = _hook_envs_with_snapshot_failures(monkeypatch, {"tank/a@autosnap_2026-04-21_05:37:00_daily": EXISTS})
    cfg = _take_cfg("tank/a", pre_snapshot_script="/bin/true", post_snapshot_script="/bin/true")
    taken = ps.take_snapshots({"tank/a": cfg}, {}, now=NOW_APRIL)
    assert list(taken) == ["tank/a@autosnap_2026-04-21_05:37:00dst_daily"]
    assert envs["post"]["SANOID_SNAPNAME"] == "tank/a@autosnap_2026-04-21_05:37:00dst_daily"
    assert envs["pre"]["SANOID_SNAPNAME"] == "autosnap_2026-04-21_05:37:00_daily"  # pre runs before the rename
    assert envs["post"]["SANOID_SNAPNAMES"] == "autosnap_2026-04-21_05:37:00_daily"  # joined before the rename


def test_take_snapshots_post_hook_snapname_is_renamed_even_when_the_dst_retry_fails(monkeypatch):
    # upstream rewrites the name BEFORE running the retry, so a failed retry keeps the dst_ name
    envs = _hook_envs_with_snapshot_failures(monkeypatch, {
        "tank/a@autosnap_2026-04-21_05:37:00_daily": EXISTS,
        "tank/a@autosnap_2026-04-21_05:37:00dst_daily": "cannot create snapshot: out of space\n",
    })
    problems: list[str] = []
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a", post_snapshot_script="/bin/true")}, {},
                              now=NOW_APRIL, problems=problems)
    assert taken == {} and len(problems) == 1
    assert envs["post"]["SANOID_SNAPNAME"] == "tank/a@autosnap_2026-04-21_05:37:00dst_daily"


def test_take_snapshots_post_hook_snapname_ignores_a_later_snapshots_dst_rename(monkeypatch):
    # daily is first (PERIOD_TYPES order); only the hourly collides, so $snapshots[0] keeps its name
    envs = _hook_envs_with_snapshot_failures(monkeypatch, {"tank/a@autosnap_2026-04-21_05:37:00_hourly": EXISTS})
    cfg = _take_cfg("tank/a", hourly=24, post_snapshot_script="/bin/true")
    taken = ps.take_snapshots({"tank/a": cfg}, {}, now=NOW_APRIL)
    assert sorted(taken) == ["tank/a@autosnap_2026-04-21_05:37:00_daily", "tank/a@autosnap_2026-04-21_05:37:00dst_hourly"]
    assert envs["post"]["SANOID_SNAPNAME"] == "tank/a@autosnap_2026-04-21_05:37:00_daily"


def test_take_snapshots_post_hook_snapname_is_not_renamed_by_other_failures(monkeypatch):
    # only an "already exists" collision triggers the dst_ retry (and so the rename)
    envs = _hook_envs_with_snapshot_failures(
        monkeypatch, {"tank/a@autosnap_2026-04-21_05:37:00_daily": "cannot create snapshot: out of space\n"})
    ps.take_snapshots({"tank/a": _take_cfg("tank/a", post_snapshot_script="/bin/true")}, {}, now=NOW_APRIL, problems=[])
    assert envs["post"]["SANOID_SNAPNAME"] == "tank/a@autosnap_2026-04-21_05:37:00_daily"


def _record_destroys_reproducing_a_negative_timeout(monkeypatch, fail_for=()):
    """Like _record_destroys, but also reproduces what the REAL subprocess.run does for a negative
    timeout (raises TimeoutExpired immediately -- confirmed empirically, 2026-09-22). The plain
    fake never touches the real subprocess.run, so it can't be used to catch a bug that only
    exists in that real behavior; this one encodes the confirmed fact directly."""
    destroyed = []

    def fake_run(cmd, capture_output=False, text=False, timeout=None, **kwargs):
        if timeout is not None and timeout < 0:
            raise subprocess.TimeoutExpired(cmd, timeout)
        if isinstance(cmd, list) and cmd[:2] == [ps.ZFS, "destroy"]:
            destroyed.append(cmd[-1])
            if cmd[-1] in fail_for:
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"cannot destroy: dataset is busy\n")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return destroyed


def test_prune_negative_script_timeout_does_not_block_the_destroy(tmp_path, monkeypatch):
    # the prune-side mirror: pre_pruning_script + a negative script_timeout used to make
    # run_hook_script report a false "hook failed" (subprocess.run(timeout=<negative>) raises
    # TimeoutExpired immediately, before the hook even starts), which prune reads as a veto (skip
    # this destroy) -- exactly like a real pre_pruning_script veto, but for a reason that has
    # nothing to do with the hook's own judgment. count=2, daily=1 (the _prune_case default): one
    # candidate (the older of the two) is prunable, the other is held by the retention floor.
    names, _, _, _ = case = _prune_case("tank/data", 2, pre_pruning_script="/bin/true", script_timeout="-5")
    destroyed = _record_destroys_reproducing_a_negative_timeout(monkeypatch)
    pruned = _run_prune(tmp_path, [case])
    assert destroyed == [f"tank/data@{names[0]}"]  # the destroy went ahead, not vetoed
    assert pruned == [f"tank/data@{names[0]}"]


# ---------------------------------------------------------------------------
# pruning: where hooks come from, hooks set to 0, whole-second ages and the closing cache flush.
# Tests marked "guard" pin behavior that already matched; the rest failed before their fix.
# ---------------------------------------------------------------------------

def _record_prune_calls(monkeypatch, hook_exit=None):
    """Patches subprocess.run. Returns (destroyed, hooks): each `zfs destroy` target and each hook command line,
    in order. A hook exits with hook_exit.get(command, 0), or raises it if it is an exception (a hook that can't
    start)."""
    destroyed: list[str] = []
    hooks: list[str] = []

    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        if (command := _hook_command(cmd, kwargs)) is not None:
            hooks.append(command)
            result = (hook_exit or {}).get(command, 0)
            if isinstance(result, BaseException):
                raise result
            return subprocess.CompletedProcess(cmd, result)
        if cmd[:2] == [ps.ZFS, "destroy"]:
            destroyed.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return destroyed, hooks


def _run_prune_sections(tmp_path, sections, case, **kwargs):
    """prune_snapshots over explicit `sections`, so two can share the path of one _prune_case."""
    names, snaps, by_type, cfg = case
    path = cfg["path"]
    _write_cache(tmp_path / "cache", [f"{path}@{n}\tcreation\t{snaps[n].ctime}\t-" for n in names])
    return ps.prune_snapshots(sections, {path: snaps}, {path: by_type}, tmp_path / "cache", tmp_path / "run", **kwargs)


def test_prune_honors_the_veto_hook_of_the_section_named_after_the_dataset(tmp_path, monkeypatch):
    # Parity: upstream reads hooks from $config{$dataset}. [b] prunes tank/a and the veto lives in [tank/a], a recursive
    # parent that skips itself (process_children_only; two sections that both prune it are refused at load), so upstream
    # destroys nothing (confirmed against real Perl).
    case = _prune_case("tank/a", 3)  # daily=1: the two oldest are candidates
    cfg = case[3]
    sections = {"tank/a": dict(cfg, process_children_only=True, pre_pruning_script="/bin/veto"), "b": dict(cfg)}
    destroyed, hooks = _record_prune_calls(monkeypatch, {"/bin/veto": 1})
    assert _run_prune_sections(tmp_path, sections, case) == []
    assert destroyed == []
    assert hooks == ["/bin/veto", "/bin/veto"]


@pytest.mark.parametrize(("hook_exit", "expected_hooks", "destroys"), [
    ({"/bin/named": 1}, ["/bin/named"] * 2, 0),  # upstream's hook vetoes; the section's own is not needed
    ({"/bin/own": 1}, ["/bin/named", "/bin/own"] * 2, 0),  # the section's own hook keeps its veto
    ({}, ["/bin/named", "/bin/own"] * 2, 2),
])
def test_prune_runs_both_differing_veto_hooks_and_either_can_veto(tmp_path, monkeypatch, hook_exit, expected_hooks, destroys):
    case = _prune_case("tank/a", 3)
    cfg = case[3]
    sections = {"tank/a": dict(cfg, process_children_only=True, pre_pruning_script="/bin/named"),
                "b": dict(cfg, pre_pruning_script="/bin/own")}
    destroyed, hooks = _record_prune_calls(monkeypatch, hook_exit)
    _run_prune_sections(tmp_path, sections, case)
    assert hooks == expected_hooks
    assert len(destroyed) == destroys


def test_prune_still_runs_a_custom_path_sections_own_veto_hook(tmp_path, monkeypatch):
    # guard for the deviation: with no [tank/a] section, upstream runs no hook at all
    case = _prune_case("tank/a", 3)
    destroyed, hooks = _record_prune_calls(monkeypatch, {"/bin/veto": 1})
    _run_prune_sections(tmp_path, {"b": dict(case[3], pre_pruning_script="/bin/veto")}, case)
    assert destroyed == []
    assert hooks == ["/bin/veto", "/bin/veto"]


def test_prune_runs_a_veto_hook_set_in_both_sections_once(tmp_path, monkeypatch):
    # guard: the same command in both places is one hook, not two
    case = _prune_case("tank/a", 3)
    cfg = case[3]
    sections = {"tank/a": dict(cfg, process_children_only=True, pre_pruning_script="/bin/check"),
                "b": dict(cfg, pre_pruning_script="/bin/check")}
    destroyed, hooks = _record_prune_calls(monkeypatch)
    _run_prune_sections(tmp_path, sections, case)
    assert len(destroyed) == 2
    assert hooks == ["/bin/check", "/bin/check"]


def test_prune_takes_pruning_script_from_the_section_named_after_the_dataset(tmp_path, monkeypatch):
    # Parity: upstream runs $config{$dataset}'s pruning_script, the lookup take_snapshots uses for its hooks
    case = _prune_case("tank/a", 3)
    cfg = case[3]
    sections = {"tank/a": dict(cfg, process_children_only=True, pruning_script="/bin/named_post"),
                "b": dict(cfg, pruning_script="/bin/own_post")}
    destroyed, hooks = _record_prune_calls(monkeypatch)
    _run_prune_sections(tmp_path, sections, case)
    assert len(destroyed) == 2
    assert hooks == ["/bin/named_post", "/bin/named_post"]


@pytest.mark.parametrize("key", ["pre_pruning_script", "pruning_script"])
def test_prune_treats_a_hook_set_to_0_as_unset(tmp_path, monkeypatch, key):
    # Parity: Perl's "0" is false, so upstream never runs it. Through /bin/sh it exits 127, which as a
    # pre_pruning_script vetoed every prune of the dataset (confirmed against real Perl).
    case = _prune_case("tank/data", 3, **{key: "0"})
    destroyed, hooks = _record_prune_calls(monkeypatch, {"0": 127})
    _run_prune(tmp_path, [case])
    assert len(destroyed) == 2
    assert hooks == []


def test_run_hook_script_treats_a_script_of_0_as_unset(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 127))
    assert ps.run_hook_script({"pruning_script": "0"}, "pruning_script", "tank/a", {}) == 0
    assert calls == []


def _record_take_hooks(monkeypatch, hook_exit=None):
    """Patches subprocess.run for take_snapshots: every `zfs snapshot` succeeds; each hook records
    (command line, SANOID_PRE_FAILURE) and exits with hook_exit.get(command, 0), or raises it if it is
    an exception (a hook that can't start)."""
    hooks: list[tuple[str, str | None]] = []

    def fake_run(cmd, capture_output=False, text=False, **kwargs):
        if (command := _hook_command(cmd, kwargs)) is not None:
            hooks.append((command, kwargs["env"].get("SANOID_PRE_FAILURE")))
            result = (hook_exit or {}).get(command, 0)
            if isinstance(result, BaseException):
                raise result
            return subprocess.CompletedProcess(cmd, result)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return hooks


def test_take_snapshots_treats_a_pre_snapshot_script_of_0_as_unset(monkeypatch):
    # Parity: upstream never runs it. Through /bin/sh it exits 127, and with no_inconsistent_snapshot
    # that skipped the dataset on every run (confirmed against real Perl).
    hooks = _record_take_hooks(monkeypatch, {"0": 127})
    cfg = _take_cfg("tank/a", pre_snapshot_script="0", no_inconsistent_snapshot=True)
    taken = ps.take_snapshots({"tank/a": cfg}, {}, now=NOW_APRIL)
    assert list(taken) == ["tank/a@autosnap_2026-04-21_05:37:00_daily"]
    assert hooks == []


def test_take_snapshots_runs_the_post_hook_when_the_pre_hook_is_0(monkeypatch):
    # an unset pre hook is not a failed one, so post_snapshot_script runs (confirmed against real Perl)
    hooks = _record_take_hooks(monkeypatch, {"0": 127})
    ps.take_snapshots({"tank/a": _take_cfg("tank/a", pre_snapshot_script="0", post_snapshot_script="/bin/post")}, {},
                      now=NOW_APRIL)
    assert hooks == [("/bin/post", "0")]


def test_take_snapshots_treats_a_post_snapshot_script_of_0_as_unset(monkeypatch):
    hooks = _record_take_hooks(monkeypatch, {"0": 127})
    ps.take_snapshots({"tank/a": _take_cfg("tank/a", post_snapshot_script="0")}, {}, now=NOW_APRIL)
    assert hooks == []


def test_run_hook_script_returns_minus_one_when_the_hook_cannot_start(monkeypatch, capsys):
    # DEV-043: once fork has failed, upstream's system() returns -1, a failed hook; here that comes at once.
    def fake_run(cmd, **kwargs):
        raise _cannot_start()

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = {"pre_snapshot_script": "/bin/pre", "script_timeout": "5"}
    assert ps.run_hook_script(cfg, "pre_snapshot_script", "tank/a", {}) == -1
    # upstream's line for a system() that returned -1, after a line saying why
    assert capsys.readouterr().err == (f"could not run '/bin/pre': {_cannot_start()}\n"
                                       "WARN: pre_snapshot_script failed, -1\n")


@pytest.mark.parametrize(("over", "taken_count", "hooks_after_pre", "problem_count"), [
    ({}, 1, [], 0),  # the snapshot is still taken; the post hook is skipped
    ({"force_post_snapshot_script": True}, 1, [("/bin/post", "1")], 0),
    ({"no_inconsistent_snapshot": True}, 0, [], 1),  # held back, and reported
])
def test_take_snapshots_treats_a_pre_hook_that_cannot_start_as_a_failed_one(monkeypatch, over, taken_count,
                                                                            hooks_after_pre, problem_count):
    # Parity with upstream's -1 from system() (confirmed against real Perl); this ended the whole run
    hooks = _record_take_hooks(monkeypatch, {"/bin/pre": _cannot_start()})
    cfg = _take_cfg("tank/a", pre_snapshot_script="/bin/pre", post_snapshot_script="/bin/post", **over)
    problems = []
    taken = ps.take_snapshots({"tank/a": cfg}, {}, now=NOW_APRIL, problems=problems)
    assert len(taken) == taken_count
    assert hooks == [("/bin/pre", None), *hooks_after_pre]
    assert len(problems) == problem_count


def test_take_snapshots_reports_a_snapshot_held_back_by_a_failed_pre_hook(monkeypatch, capsys):
    # Upstream skips the dataset with only its WARN and exits 0, so a hook that always fails stops these
    # snapshots for good while the unit looks healthy
    hooks = _record_take_hooks(monkeypatch, {"/bin/pre": 1})
    cfg = _take_cfg("tank/a", pre_snapshot_script="/bin/pre", no_inconsistent_snapshot=True)
    problems = []
    assert ps.take_snapshots({"tank/a": cfg}, {}, now=NOW_APRIL, problems=problems) == {}
    assert hooks == [("/bin/pre", None)]
    assert len(problems) == 1 and "pre_snapshot_script" in problems[0]
    assert "ERROR: not taking daily snapshots of 'tank/a' this run" in capsys.readouterr().err


def test_take_snapshots_a_failed_pre_hook_without_no_inconsistent_snapshot_is_not_a_problem(monkeypatch):
    # guard: the snapshot is still taken, so nothing went unprotected
    _record_take_hooks(monkeypatch, {"/bin/pre": 1})
    problems = []
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a", pre_snapshot_script="/bin/pre")}, {}, now=NOW_APRIL,
                              problems=problems)
    assert list(taken) == ["tank/a@autosnap_2026-04-21_05:37:00_daily"]
    assert problems == []


@pytest.mark.parametrize(("no_inconsistent", "taken_count", "problem_count"), [(False, 1, 0), (True, 0, 1)],
                         ids=["no_inconsistent_snapshot = no: taken", "no_inconsistent_snapshot = yes: held back"])
def test_a_pre_snapshot_script_that_times_out_is_a_failed_one_and_the_run_goes_on(monkeypatch, capsys, no_inconsistent,
                                                                                 taken_count, problem_count):
    # guard, DEV-042 (#1116): upstream's alarm kills sanoid itself, so nothing is taken whatever
    # no_inconsistent_snapshot says. pysanoid kills the hook and treats it as failed.
    monkeypatch.setattr(ps, "run_zfs_cmd", lambda cmd: subprocess.CompletedProcess(cmd, 0, b"", b""))
    cfg = _take_cfg("tank/a", pre_snapshot_script="sleep 30", script_timeout="1", no_inconsistent_snapshot=no_inconsistent)
    problems: list[str] = []
    start = time.monotonic()
    taken = ps.take_snapshots({"tank/a": cfg}, {}, now=NOW_APRIL, problems=problems)
    assert time.monotonic() - start < 10
    assert len(taken) == taken_count
    assert len(problems) == problem_count
    assert "WARN: pre_snapshot_script didn't finish in the allowed time!" in capsys.readouterr().err


def test_take_snapshots_announces_its_hooks_under_readonly_without_running_them(monkeypatch, capsys):
    # Parity: upstream prints both "executing" lines before its readonly checks (confirmed against real Perl)
    hooks = _record_take_hooks(monkeypatch)
    cfg = _take_cfg("tank/a", pre_snapshot_script="/bin/pre", post_snapshot_script="/bin/post")
    assert ps.take_snapshots({"tank/a": cfg}, {}, now=NOW_APRIL, readonly=True, verbose=True) == {}
    assert hooks == []
    assert capsys.readouterr().out.splitlines() == [
        "INFO: taking snapshots...",
        "executing pre_snapshot_script '/bin/pre' on dataset 'tank/a'",
        "taking snapshot tank/a@autosnap_2026-04-21_05:37:00_daily",
        "executing post_snapshot_script '/bin/post' on dataset 'tank/a'",
    ]


def test_take_snapshots_readonly_without_verbose_prints_nothing(monkeypatch, capsys):
    # guard
    hooks = _record_take_hooks(monkeypatch)
    cfg = _take_cfg("tank/a", pre_snapshot_script="/bin/pre", post_snapshot_script="/bin/post")
    ps.take_snapshots({"tank/a": cfg}, {}, now=NOW_APRIL, readonly=True)
    assert hooks == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(("key", "destroys"), [
    ("pre_pruning_script", 0),  # a failed veto hook: upstream's `$ret != 0` skips each destroy
    ("pruning_script", 2),  # upstream ignores its result, so both destroys go ahead
])
def test_prune_treats_a_hook_that_cannot_start_as_a_failed_one(tmp_path, monkeypatch, key, destroys):
    # Parity: when fork fails, upstream's system() returns -1 (confirmed against real Perl); this ended the prune run
    case = _prune_case("tank/data", 3, **{key: "/bin/hook"})
    destroyed, hooks = _record_prune_calls(monkeypatch, {"/bin/hook": _cannot_start()})
    _run_prune(tmp_path, [case])
    assert len(destroyed) == destroys
    assert hooks == ["/bin/hook", "/bin/hook"]


@pytest.mark.parametrize(("clock", "expected"), [
    (0.5, []),  # still inside the boundary second: kept, as upstream keeps it
    (1.0, ["tank/data@autosnap_e3_daily"]),  # guard: one whole second later it goes
])
def test_prune_measures_age_in_whole_seconds_like_upstreams_time(tmp_path, monkeypatch, clock, expected):
    # Parity: upstream's time() is whole seconds. A fractional clock counted a snapshot exactly `daily`
    # days old as older and pruned it one run early (confirmed against real Perl).
    now = 1_790_000_000
    ages = {f"autosnap_e{k}_daily": k for k in (3, 2, 1, 0)}  # oldest first
    snaps = {"tank/data": {n: ps.Snapshot("tank/data", n, "daily", now - k * 86400) for n, k in ages.items()}}
    by_type = {"tank/data": {"daily": {"sorted": list(ages)}}}
    config = {"tank/data": {"path": "tank/data", "autoprune": True, "process_children_only": False, "daily": 3, "prune_defer": 0}}
    _write_cache(tmp_path / "cache", [f"tank/data@{n}\tcreation\t{s.ctime}\t-" for n, s in snaps["tank/data"].items()])
    destroyed = _record_destroys(monkeypatch)
    monkeypatch.setattr(ps.time, "time", lambda: now + clock)
    ps.prune_snapshots(config, snaps, by_type, tmp_path / "cache", tmp_path / "run")
    assert destroyed == expected


def test_remove_cached_snapshots_reports_whether_it_flushed(tmp_path):
    cache_dir, run_dir = tmp_path / "cache", tmp_path / "run"
    _write_cache(cache_dir, ["tank/a@s1\tcreation\t1\t-"])
    assert ps.remove_cached_snapshots([], cache_dir, run_dir) is True  # nothing pending
    with ps.try_lock(run_dir, "sanoid_cacheupdate") as (held, _):
        assert held
        assert ps.remove_cached_snapshots(["tank/a@s1"], cache_dir, run_dir) is False  # busy: deferred
    assert _read_cache(cache_dir) == ["tank/a@s1\tcreation\t1\t-"]
    assert ps.remove_cached_snapshots(["tank/a@s1"], cache_dir, run_dir) is True
    assert _read_cache(cache_dir) == []


def test_prune_applies_a_deferred_batch_flush_before_returning(tmp_path, monkeypatch):
    # Parity: upstream's prune_snapshots ends with a blocking removecachedsnapshots(1), which applies
    # whatever a busy cache lock deferred
    real_try_lock = ps.try_lock

    @contextlib.contextmanager
    def cache_lock_busy(run_dir, name):
        if name == "sanoid_cacheupdate":
            yield False, None
        else:
            with real_try_lock(run_dir, name) as got:
                yield got

    monkeypatch.setattr(ps, "try_lock", cache_lock_busy)
    names, _, _, _ = case = _prune_case("tank/data", 3)
    _record_destroys(monkeypatch)
    assert len(_run_prune(tmp_path, [case])) == 2
    remaining = {line.split("\t", 1)[0] for line in _read_cache(tmp_path / "cache")}
    assert remaining == {f"tank/data@{names[2]}"}


def test_main_prune_closing_flush_is_a_no_op_once_every_batch_was_flushed(tmp_path, monkeypatch, capsys):
    # Parity: upstream clears %pruned after each successful flush, so its closing flush neither logs nor
    # waits for the cache lock. Re-flushing the whole run printed a second INFO line, and waited whenever
    # another process held the lock (confirmed against real Perl).
    names, snaps, _, _ = _prune_case("tank/data", 3)
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 1\n")
    _write_cache(tmp_path / "cache", [f"tank/data@{n}\tcreation\t{snaps[n].ctime}\t-" for n in names])
    destroyed = _record_destroys(monkeypatch)

    def no_wait(*args, **kwargs):
        raise AssertionError("waited for the cache lock with nothing left to flush")

    monkeypatch.setattr(ps, "wait_lock", no_wait)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    monkeypatch.setattr(ps, "get_snapshots", lambda *args, **kwargs: {"tank/data": snaps})
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--prune-snapshots", "--verbose"])
    assert rc == 0
    assert len(destroyed) == 2
    assert capsys.readouterr().out.count("INFO: removing destroyed snapshots from cache.") == 1


# ---------------------------------------------------------------------------
# --debug dump (2026-09-22 Module 8, the last one). Found by reading upstream's blabber verbatim
# and probing two of its Perl-isms against a real interpreter: undef stringifies to "" inside a
# double-quoted string (confirmed), and its own trailing `print "\n\n"` gives an inconsistent
# blank-line count between dataset blocks depending on which branch ran (also confirmed -- see P8
# in DEVIATIONS.md). Everything else checked out: section order is inherently
# non-deterministic upstream (Perl hash order) so this port's deterministic order is not something
# to "match"; per-section (not per-path) iteration; the dump reads the SAME pre-take/pre-prune
# snapshot data as upstream's blabber does (main() calls it before do_take()/do_prune()). The
# second pass (2026-09-26, at the end of this file) replaced the pprint config dump with
# Data::Dumper's text and added upstream's config-merge trace.
# ---------------------------------------------------------------------------

def test_debug_dump_prints_a_blank_desired_value_for_a_type_cfg_never_configured(capsys):
    # A snap_type here comes from a REAL snapshot's name (_snapshot_type reads it unanchored), not from
    # cfg's own keys -- a foreign or malformed autosnap_* name can produce a type cfg never set.
    # Upstream's equivalent ($config{$section}{$type}, undef for such a type) prints as "" in
    # string interpolation (confirmed against a real Perl interpreter, 2026-09-22); a bare
    # cfg.get(snap_type) prints the literal text "None" instead.
    cfg = {"path": "tank/a", "daily": "7"}
    snaps_by_type = {"tank/a": {"really": {"numsnaps": 1, "newest": 3600.0, "newestname": "autosnap_notautosnap_really"}}}
    ps.print_debug_dump({"tank/a": cfg}, snaps_by_type, {"tank/a": {"numsnaps": 1, "newest": 3600.0}})
    out = capsys.readouterr().out
    assert "desired: \n" in out       # blank, not "desired: None"
    assert "desired: None" not in out


def test_debug_dump_still_shows_a_real_configured_types_value(capsys):
    cfg = {"path": "tank/a", "daily": "7"}
    snaps_by_type = {"tank/a": {"daily": {"numsnaps": 3, "newest": 3600.0, "newestname": "autosnap_x_daily"}}}
    ps.print_debug_dump({"tank/a": cfg}, snaps_by_type, {"tank/a": {"numsnaps": 3, "newest": 3600.0}})
    assert "desired: 7\n" in capsys.readouterr().out


def test_debug_dump_matches_upstreams_exact_wording_for_no_and_some_snapshots(capsys):
    # parity lock: the literal text upstream's blabber produces for each branch
    config = {"a": {"path": "tank/a"}, "b": {"path": "tank/b"}}
    snaps_by_type = {"tank/b": {"daily": {"numsnaps": 2, "newest": 5400.0, "newestname": "autosnap_x_daily"}}}
    snaps_by_path = {"tank/a": {"numsnaps": 0}, "tank/b": {"numsnaps": 2, "newest": 5400.0}}
    ps.print_debug_dump(config, snaps_by_type, snaps_by_path)
    out = capsys.readouterr().out
    assert "Filesystem tank/a has:\n     0 total snapshots (no current snapshots)\n" in out
    assert "Filesystem tank/b has:\n     2 total snapshots (newest: 1.5 hours old)\n" in out
    assert "          2 daily\n              desired: \n              newest: 1.5 hours old, named autosnap_x_daily\n" in out


def test_debug_dump_reproduces_upstreams_own_inconsistent_blank_line_count(capsys):
    # Matches upstream byte for byte (at the user's request, 2026-09-22): its trailing
    # `print "\n\n"` supplies BOTH the line break and the blank line after "(no current
    # snapshots)" (that print has no newline of its own) -- 1 blank line -- but lands AFTER a
    # per-type "newest: ..." line that already ends in its own "\n" -- 2 blank lines. Confirmed
    # against a real Perl interpreter, 2026-09-22. Purely cosmetic; kept inconsistent on purpose,
    # matching upstream rather than "fixing" what looks like an unintentional upstream quirk.
    config = {"a": {"path": "tank/a"}, "b": {"path": "tank/b"}}
    snaps_by_type = {"tank/b": {"daily": {"numsnaps": 1, "newest": 3600.0, "newestname": "x"}}}
    snaps_by_path = {"tank/a": {"numsnaps": 0}, "tank/b": {"numsnaps": 1, "newest": 3600.0}}
    ps.print_debug_dump(config, snaps_by_type, snaps_by_path)
    out = capsys.readouterr().out
    # after "no current snapshots": exactly one blank line (two "\n" back to back), not zero, not two
    assert "(no current snapshots)\n\nFilesystem tank/b has:" in out
    assert "(no current snapshots)\n\n\n" not in out
    # after a block that lists snapshot types (here, the trailing one): exactly two blank lines
    assert out.endswith("named x\n\n\n")
    assert not out.endswith("named x\n\n")  # (a strict suffix check: \n\n\n does not end with \n\n)


def test_debug_dump_shows_every_section_even_when_two_share_a_path(capsys):
    # matches upstream's `foreach my $section (keys %config)`: once per SECTION, not de-duplicated
    # by path -- an alias section pointing at the same dataset as another gets its own block
    config = {"tank/b": {"path": "tank/b"}, "alias": {"path": "tank/b"}}
    snaps_by_path = {"tank/b": {"numsnaps": 0}}
    ps.print_debug_dump(config, {}, snaps_by_path)
    out = capsys.readouterr().out
    assert out.count("Filesystem tank/b has:") == 2


def test_snapshots_by_path_matches_snapshots_by_type_numsnaps_and_newest():
    # both summaries are built from the same snaps; they must agree with each other
    now = 2_000_000_000.0
    snaps = {"tank/a": {
        "autosnap_a_daily": ps.Snapshot("tank/a", "autosnap_a_daily", "daily", int(now - 3600)),
        "autosnap_b_daily": ps.Snapshot("tank/a", "autosnap_b_daily", "daily", int(now - 7200)),
    }}
    config = {"t": {"path": "tank/a"}}
    by_path = ps.snapshots_by_path(snaps, config, now=now)
    by_type = ps.snapshots_by_type(snaps, config, now=now)
    assert by_path["tank/a"]["numsnaps"] == 2
    assert by_path["tank/a"]["newest"] == 3600  # the more recent of the two, matching by_type's own newest
    assert by_type["tank/a"]["daily"]["newest"] == 3600
    assert by_type["tank/a"]["daily"]["newestname"] == "autosnap_a_daily"


def test_debug_dump_reads_the_pre_run_snapshot_state(tmp_path, monkeypatch):
    # matches upstream's sequencing: blabber runs on the SAME %snaps/%snapsbytype/%snapsbypath
    # take_snapshots()/prune_snapshots() are about to use, computed once before either runs -- not
    # recomputed afterward, so the dump never shows a snapshot this same run is about to take
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n")
    dumps = []
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(ps, "get_snapshots", lambda *a, **k: {"tank/data": {}})
    monkeypatch.setattr(ps, "print_debug_dump", lambda config, by_type, by_path: dumps.append(by_path["tank/data"]["numsnaps"]))
    monkeypatch.setattr(ps, "take_snapshots", lambda *a, **k: {"tank/data@x": {"time": 1, "recursive": False}})
    monkeypatch.setattr(ps, "add_cached_snapshots", lambda *a, **k: None)
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--take-snapshots", "--debug"])
    assert rc == 0
    assert dumps == [0]  # the dump saw zero snapshots -- the pre-run state, not what take_snapshots just created


# ---------------------------------------------------------------------------
# --debug, second pass (2026-09-26 re-audit unit 8). The real upstream script and pysanoid ran with --debug on the
# same files, and their output was compared part by part: the config-merge trace, the config dump, the summary, and
# the warnings in the order they come. Expected text below is what upstream printed for the same files.
# ---------------------------------------------------------------------------

SMALL_DEFAULTS = (
    "[version]\n    version = 2\n\n[template_default]\n    use_template =\n    recursive =\n    process_children_only =\n"
    "    skip_children =\n    path =\n    autosnap = yes\n    autoprune = yes\n    hourly = 48\n    daily = 90\n"
)


def _small_confdir(tmp_path, sanoid_conf):
    """A config dir whose sanoid.defaults.conf has only a few settings, so a --debug trace or dump stays short."""
    d = tmp_path / "conf"
    d.mkdir()
    (d / "sanoid.defaults.conf").write_text(SMALL_DEFAULTS)
    (d / "sanoid.conf").write_text(sanoid_conf)
    return d


def _zfs_trees(tmp_path, monkeypatch, trees):
    """A real executable in place of `zfs` whose `zfs list -o name -t filesystem,volume -Hr <fs>` prints trees[fs]."""
    cases = "".join(f"  {fs}) printf '%s\\n' {' '.join(names)} ;;\n" for fs, names in trees.items())
    script = tmp_path / "zfs"
    script.write_text(f'#!/bin/sh\ncase "$7" in\n{cases}esac\n')
    script.chmod(0o755)
    monkeypatch.setattr(ps, "ZFS", str(script))


def _debug_load(configdir, cache_dir):
    """load_config under --debug, stdout and stderr in one stream in the order written. Returns (the config, or the
    SanoidError raised, and the lines)."""
    out = io.StringIO()
    zfs = ps.ZfsClient(cache_dir, 1200, force_update=True, debug=True)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            result = ps.load_config(configdir, zfs)
        except ps.SanoidError as e:
            result = e
    return result, out.getvalue().splitlines()


def test_debug_traces_the_config_merge_as_upstream_does(tmp_path):
    # Upstream's DEBUG lines, with its warnings where it gives them: each section is checked when its turn comes, so
    # tank/a's repeated key is reported after tank's lines (confirmed against real Perl). pysanoid printed no trace,
    # and checked every section before loading any.
    confdir = _small_confdir(tmp_path, "[tank]\n    use_template = keep\n    daily = 3\n"
                                       "[template_keep]\n    hourly = 12\n    recursive = yes\n"
                                       "[template_default]\n    autoprune = no\n    recursive = yes\n"
                                       "[tank/a]\n    hourly = 6\n    hourly = 7\n")
    _, lines = _debug_load(confdir, tmp_path / "cache")
    defaults = f"{confdir}/sanoid.defaults.conf"
    assert lines == [
        f"DEBUG: initializing $config{{tank}} with default values from {defaults}.",
        "DEBUG: overriding autoprune on tank with value from user-defined default template.",
        "ignored key 'recursive' from user-defined default template.",
        "DEBUG: overriding hourly on tank with value from user-defined template template_keep.",
        "ignored key 'recursive' from 'keep' template.",
        "DEBUG: overriding daily on tank with value directly set in module.",
        "duplicate key 'hourly' in section 'tank/a', using the value from the first occurence and ignoring the others.",
        f"DEBUG: initializing $config{{tank/a}} with default values from {defaults}.",
        "DEBUG: overriding autoprune on tank/a with value from user-defined default template.",
        "ignored key 'recursive' from user-defined default template.",
        "DEBUG: overriding hourly on tank/a with value directly set in module.",
    ]


def test_debug_traces_recursion_as_upstream_does(tmp_path, monkeypatch):
    # every setting copied to a child, a zfs recursion's without autosnap (switched off instead), and each child
    # skip_children drops (confirmed against real Perl). Upstream lists one child's settings in Perl's hash order,
    # which changes from run to run, so those lines are compared as a set.
    _zfs_trees(tmp_path, monkeypatch, {"tank": ["tank", "tank/a"], "pool": ["pool", "pool/x"], "vault": ["vault", "vault/y"]})
    confdir = _small_confdir(tmp_path, "[tank]\n    recursive = yes\n    daily = 3\n[pool]\n    recursive = zfs\n"
                                       "[vault]\n    skip_children = yes\n")
    _, lines = _debug_load(confdir, tmp_path / "cache")

    def start(section):
        return f"DEBUG: initializing $config{{{section}}} with default values from {confdir}/sanoid.defaults.conf."

    def lookup(fs):
        return f"DEBUG: getting list of child datasets on {fs} using  zfs list -o name -t filesystem,volume -Hr {fs} |..."

    def copied(parent, child, keys):
        return {f"DEBUG: recursively setting {key} from {parent} to {child}." for key in keys}

    assert lines[:3] == [start("tank"), "DEBUG: overriding daily on tank with value directly set in module.", lookup("tank")]
    assert set(lines[3:9]) == copied("tank", "tank/a", ["autosnap", "path", "daily", "hourly", "skip_children", "autoprune"])
    assert lines[9:11] == [start("pool"), lookup("pool")]
    assert set(lines[11:17]) == copied("pool", "pool/x", ["path", "skip_children", "autoprune", "daily", "hourly",
                                                          "zfs_recursion"])
    assert lines[17:] == [start("vault"), lookup("vault"), "DEBUG: ignoring vault/y."]


def test_a_setting_upstream_does_not_know_is_refused_when_its_section_comes(tmp_path):
    # as upstream's loop meets it, after the sections before it have loaded, --debug lines and all (confirmed against
    # real Perl); pysanoid refused the file before loading any
    confdir = _small_confdir(tmp_path, "[tank]\n    daily = 3\n[tank/b]\n    dialy = 3\n")
    result, lines = _debug_load(confdir, tmp_path / "cache")
    assert isinstance(result, ps.SanoidError)
    assert str(result) == f"I don't understand the setting dialy you've set in [tank/b] in {confdir}/sanoid.conf."
    assert lines == [f"DEBUG: initializing $config{{tank}} with default values from {confdir}/sanoid.defaults.conf.",
                     "DEBUG: overriding daily on tank with value directly set in module."]


def test_a_setting_upstream_does_not_know_is_refused_in_upstreams_words(tmp_path, monkeypatch, capsys):
    # upstream dies with "FATAL ERROR: I don't understand the setting ...", the line its users quote (#645); 0.1.37
    # printed "FATAL: I don't understand ..."
    confdir = write_confdir(tmp_path, "[tank]\n    dialy = 3\n")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--take-snapshots"])
    assert rc == 1
    assert capsys.readouterr().err == (
        f"FATAL ERROR: I don't understand the setting dialy you've set in [tank] in {confdir}/sanoid.conf.\n")


def test_debug_through_main_traces_the_config_merge(tmp_path, monkeypatch, capsys):
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    monkeypatch.setattr(ps, "get_snapshots", lambda *a, **k: {})
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--debug"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith(f"DEBUG: initializing $config{{tank/data}} with default values from {confdir}/sanoid.defaults.conf.\n"
                          "DEBUG: overriding daily on tank/data with value directly set in module.\n****** CONFIGS ******\n")


def test_the_config_dump_is_data_dumpers_text(tmp_path, monkeypatch, capsys):
    # upstream's `print Dumper(\%config)` for the same files (confirmed against real Perl): a toggle, and the
    # initialized and zfs_recursion marks, as bare integers, because init stores Perl integers there; every other value
    # quoted, counts included, with \ and ' escaped. pysanoid printed Python's pprint of the dict.
    _script_zfs(tmp_path, monkeypatch, out=b"tank\ntank/a\n")
    confdir = _small_confdir(tmp_path, "[tank]\n    recursive = zfs\n    hourly = 5\n[it's\\here]\n    autoprune = no\n")
    config = ps.load_config(confdir, make_zfs_client(tmp_path))
    ps.print_debug_dump(config, {}, ps.snapshots_by_path({}, config))
    assert capsys.readouterr().out.split("\n\n")[0] == (
        "****** CONFIGS ******\n"
        "$VAR1 = {\n"
        "          'it\\'s\\\\here' => {\n"
        "                             'autoprune' => 0,\n"
        "                             'autosnap' => 1,\n"
        "                             'daily' => '90',\n"
        "                             'hourly' => '48',\n"
        "                             'path' => 'it\\'s\\\\here',\n"
        "                             'skip_children' => ''\n"
        "                           },\n"
        "          'tank' => {\n"
        "                      'autoprune' => 1,\n"
        "                      'autosnap' => 1,\n"
        "                      'daily' => '90',\n"
        "                      'hourly' => '5',\n"
        "                      'path' => 'tank',\n"
        "                      'skip_children' => '',\n"
        "                      'zfs_recursion' => 1\n"
        "                    },\n"
        "          'tank/a' => {\n"
        "                        'autoprune' => 1,\n"
        "                        'autosnap' => 0,\n"
        "                        'daily' => '90',\n"
        "                        'hourly' => '5',\n"
        "                        'initialized' => 1,\n"
        "                        'path' => 'tank/a',\n"
        "                        'skip_children' => '',\n"
        "                        'zfs_recursion' => 1\n"
        "                      }\n"
        "        };"
    )


def test_the_config_dump_sorts_and_indents_by_bytes_as_data_dumper():
    # Data::Dumper sorts keys by their bytes and indents a nested hash by its key's length in bytes (confirmed against
    # real Perl): the byte FF (read as \udcff) sorts after the four-byte emoji, and e-acute is two bytes wide
    config = {"tank/z": {}, "tank/é": {"path": "tank/é", "autosnap": True}, "tank/\udcff": {"daily": "7"},
              "tank/\U0001f600": {"hourly": "it's", "pre_snapshot_script": "a\\b", "autoprune": False}}
    assert ps._perl_dumper(config) == (
        "$VAR1 = {\n"
        "          'tank/z' => {},\n"
        "          'tank/é' => {\n"
        "                         'autosnap' => 1,\n"
        "                         'path' => 'tank/é'\n"
        "                       },\n"
        "          'tank/\U0001f600' => {\n"
        "                           'autoprune' => 0,\n"
        "                           'hourly' => 'it\\'s',\n"
        "                           'pre_snapshot_script' => 'a\\\\b'\n"
        "                         },\n"
        "          'tank/\udcff' => {\n"
        "                        'daily' => '7'\n"
        "                      }\n"
        "        };\n"
    )
    assert ps._perl_dumper({}) == "$VAR1 = {};\n"


@pytest.mark.parametrize(("ctime", "shown"), [
    ("9" * 400, "-Inf"),
    ("9223372036854775808", "-2562047787517993.0"),  # 2**63
    ("03109049318243306005523", "-863624810622643456.0"),
    ("1789999460", "0.1"),
    ("1789994600", "1.5"),
    ("1790000180", "-0.1"),
], ids=["400 digits", "2**63", "23 digits", "540 s", "5400 s", "180 s ahead"])
def test_debug_dump_ages_follow_perls_arithmetic(monkeypatch, capsys, ctime, shown):
    # upstream's sprintf("%.1f", $age / 60 / 60) on `time - $ctime` (confirmed against real Perl). A real creation time
    # prints as age / 3600 does; a damaged cache's can differ in its last digits, and infinity is "-Inf"
    monkeypatch.setattr(ps.time, "time", lambda: 1_790_000_000.0)
    snaps = ps._parse_snapshot_lines([f"tank/a@autosnap_x_daily\tcreation\t{ctime}\t-"])
    config = {"t": _sec("tank/a")}
    ps.print_debug_dump(config, ps.snapshots_by_type(snaps, config), ps.snapshots_by_path(snaps, config))
    out = capsys.readouterr().out
    assert f"(newest: {shown} hours old)" in out
    assert f"newest: {shown} hours old, named autosnap_x_daily" in out


def test_config_file_names_are_spelled_as_upstream_spells_them(tmp_path, monkeypatch, capsys):
    # "<configdir>/sanoid.conf" as given, so `--configdir=/etc/sanoid/` names "/etc/sanoid//sanoid.conf" (confirmed
    # against real Perl); pysanoid tidied the path
    confdir = write_confdir(tmp_path, "[tank]\n    daily = 3\n")
    (confdir / "sanoid.conf").unlink()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    rc = ps.main(["--configdir", f"{confdir}/", "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--take-snapshots"])
    assert rc == 1
    assert capsys.readouterr().err == (
        f"FATAL: cannot load {confdir}//sanoid.conf - please create a valid local config file before running pysanoid!\n")


def test_the_debug_trace_names_the_defaults_file_as_given(tmp_path):
    confdir = _small_confdir(tmp_path, "[tank]\n    daily = 3\n")
    _, lines = _debug_load(f"{confdir}/", tmp_path / "cache")
    assert lines[0] == f"DEBUG: initializing $config{{tank}} with default values from {confdir}//sanoid.defaults.conf."



# ---------------------------------------------------------------------------
# upstream's field reports. The history pass read upstream's GitHub issues and pull requests
# (github.com/jimsalterjrs/sanoid); each test below runs pysanoid on the configuration or event a
# sanoid user reported there. The section-order tests are the one new finding (#1021); the rest pin
# deviations the audit had already made, in the shapes users actually hit.
# ---------------------------------------------------------------------------

def _fake_zfs_world(monkeypatch, tree: list[str], lines: list[str]) -> tuple[list[str], list[str]]:
    """Patches subprocess.run with a small zfs that also knows datasets: `list` prints `tree` from the name it's given
    down, `get` lists `lines` plus every snapshot taken since, `snapshot` takes one (its creation is now) and `destroy`
    removes one. Any other command, a hook, runs for real. Returns (taken, destroyed)."""
    real_run = subprocess.run
    taken: list[str] = []
    destroyed: list[str] = []

    def fake_run(cmd: str | list[str], text: bool = False, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if isinstance(cmd, str) or cmd[0] != ps.ZFS:
            return real_run(cmd, text=text, **kwargs)
        verb, name = cmd[1], cmd[-1]
        out = ""
        if verb == "list":
            out = "".join(f"{d}\n" for d in tree if d == name or d.startswith(f"{name}/"))
        elif verb == "get":
            out = "".join(f"{ln}\n" for ln in lines)
        elif verb == "snapshot":
            taken.append(name)
            lines.append(f"{name}\tcreation\t{int(time.time())}\t-")
        elif verb == "destroy":
            destroyed.append(name)
            lines[:] = [ln for ln in lines if ln.split("\t")[0] != name]
        return subprocess.CompletedProcess(cmd, 0, stdout=out.encode(), stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return taken, destroyed


_KEEP_AND_SHORT = """
[template_keep]
    hourly = 0
    daily = 7
    monthly = 12
    yearly = 5
[template_short]
    hourly = 0
    daily = 7
    monthly = 0
    yearly = 0
"""


@pytest.mark.parametrize("child_first", [True, False], ids=["child above", "child below"])
def test_main_refuses_a_child_section_above_its_recursive_parent_before_pruning_anything(tmp_path, monkeypatch, capsys,
                                                                                          child_first):
    # #1021, end to end: [tank/a] keeps 12 monthlies and 5 yearlies, [tank] none. Listed above [tank], upstream and 0.1.36
    # destroyed all 17 (confirmed against the real upstream script); listed below, both keep them.
    child = "[tank/a]\n    use_template = keep\n"
    parent = "[tank]\n    use_template = short\n    recursive = yes\n"
    confdir = write_confdir(tmp_path, (child + parent if child_first else parent + child) + _KEEP_AND_SHORT)
    now = int(time.time())
    lines = [f"tank/a@autosnap_m{k}_monthly\tcreation\t{now - k * 30 * 86400}\t-" for k in range(1, 13)]
    lines += [f"tank/a@autosnap_y{k}_yearly\tcreation\t{now - k * 365 * 86400}\t-" for k in range(1, 6)]
    _, destroyed = _fake_zfs_world(monkeypatch, ["tank", "tank/a"], lines)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--prune-snapshots"])
    assert destroyed == []
    if child_first:
        assert rc == 1
        assert capsys.readouterr().err.startswith(f"FATAL: [tank/a] in {confdir}/sanoid.conf must come after [tank]: ")
    else:
        assert rc == 0


@pytest.mark.parametrize(("conf", "refused"), [
    ("[tank/new]\n    daily = 3\n[tank]\n    recursive = yes\n", "tank/new"),
    ("[pool2/data]\n    daily = 2\n[alias]\n    path = pool2\n    recursive = yes\n", "pool2/data"),
    ("[tank/a/x]\n    daily = 2\n[tank]\n    recursive = zfs\n", "tank/a/x"),
    ("[tank/ab]\n    daily = 2\n[tank/a]\n    recursive = yes\n", None),
    ("[tank/b]\n    daily = 2\n[alias]\n    path = tank/b\n    recursive = yes\n    process_children_only = yes\n", None),
    ("[tank/a]\n    daily = 2\n[tank]\n    recursive = no\n    skip_children = no\n", None),
    ("[tank/a]\n    daily = 2\n[template_t]\n    recursive = yes\n[tank]\n    use_template = t\n", None),
], ids=["a dataset not there yet", "the parent's path, not its name", "a grandchild, under zfs recursion",
        "a name that only starts like the path", "the path itself", "no recursion", "a template's recursive, ignored"])
def test_the_section_order_rule_follows_the_recursive_sections_path(tmp_path, monkeypatch, conf, refused):
    # The rule is the file's: a section named for a dataset under a recursive or skip_children section's path must come
    # after it, whether or not zfs has that dataset yet, so a misplaced section fails when it's written, not the day
    # its dataset appears.
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a", "tank/a/x", "tank/ab", "tank/b", "pool2", "pool2/data"])
    if refused is None:
        _ = _load(tmp_path, conf)
    else:
        with pytest.raises(ps.SanoidError, match=rf"^\[{refused}\] in .* must come after \["):
            _ = _load(tmp_path, conf)


_ONE_SECTION = "Keep one section for tank/a."
_BELOW_TANK = "To give tank/a settings of its own, name its section [tank/a] and place it below [tank]."


@pytest.mark.parametrize(("conf", "who", "advice"), [
    ("[tank/a]\n    daily = 3\n[b]\n    path = tank/a\n    daily = 3\n", "[tank/a] and [b]", _ONE_SECTION),
    ("[tank/a]\n    daily = 3\n    weekly = 4\n[b]\n    path = tank/a\n    hourly = 24\n", "[tank/a] and [b]", _ONE_SECTION),
    ("[tank/a]\n    autoprune = no\n[b]\n    path = tank/a\n    autosnap = no\n", "[tank/a] and [b]", _ONE_SECTION),
    ("[x]\n    path = tank/a\n[y]\n    path = tank/a\n", "[x] and [y]", _ONE_SECTION),
    ("[tank/a]\n    daily = 3\n[b]\n    path = tank/a\n[c]\n    path = tank/a\n", "[tank/a], [b] and [c]", _ONE_SECTION),
    ("[tank]\n    recursive = yes\n    monthly = 0\n[keep-a]\n    path = tank/a\n    monthly = 12\n",
     "[tank]'s recursion and [keep-a]", _BELOW_TANK),
    ("[tank]\n    recursive = zfs\n[x]\n    path = tank/a\n", "[tank]'s recursion and [x]", _BELOW_TANK),
    ("[inner]\n    path = tank/a\n    recursive = yes\n[tank]\n    recursive = yes\n", "[inner] and [tank]'s recursion",
     _BELOW_TANK),
    ("[tank]\n    recursive = yes\n[inner]\n    path = tank/a\n    recursive = yes\n", "[tank]'s recursion and [inner]",
     _BELOW_TANK),
], ids=["the same settings twice", "a second section for hourlies", "one takes, the other prunes", "two paths, no name",
        "three sections", "a path under a recursive section", "a path under recursive = zfs",
        "a recursive path above its parent", "a recursive path below its parent"])
def test_two_sections_that_take_or_prune_one_dataset_are_refused(tmp_path, monkeypatch, conf, who, advice):
    # DEV-007: upstream takes and prunes tank/a by both sections.
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a", "tank/a/x"])
    with pytest.raises(ps.SanoidError) as refused:
        _ = _load(tmp_path, conf)
    verb = "both" if who.count("[") == 2 else "all"
    assert str(refused.value) == (f"{who} in {tmp_path}/conf/sanoid.conf {verb} take or prune tank/a, each by its own "
                                  f"settings. {advice}")


@pytest.mark.parametrize("conf", [
    "[tank]\n    recursive = yes\n[tank/a]\n    recursive = yes\n    process_children_only = yes\n[a-itself]\n    path = tank/a\n",
    "[tank]\n    skip_children = yes\n[x]\n    path = tank/a\n",
    "[tank/a]\n    daily = 3\n[watch]\n    path = tank/a\n    autosnap = no\n    autoprune = no\n",
    "[tank]\n    recursive = yes\n[tank/a]\n    daily = 3\n",
    "[tank]\n    daily = 3\n[offsite]\n    path = tank/a\n",
    "[templates/a]\n    daily = 3\n[b]\n    path = templates/a\n",
], ids=["#1088's layout: a parent that skips itself, and a section for it", "under skip_children",
        "one that neither takes nor prunes", "a child's own section below its parent", "a path no other section reaches",
        "a name take and prune skip as a template"])
def test_one_section_that_takes_or_prunes_a_dataset_is_loaded(tmp_path, monkeypatch, conf):
    # guard: where a single entry takes and prunes the dataset, the file loads as upstream loads it
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a", "tank/a/x", "templates", "templates/a"])
    _ = _load(tmp_path, conf)


def test_main_refuses_two_sections_on_one_dataset_before_pruning_anything(tmp_path, monkeypatch, capsys):
    # End to end: [b] is added for hourlies and leaves weekly at the shipped default, 0. Upstream and 0.1.37 destroyed
    # all 4 weeklies [tank/a] keeps (confirmed against the real upstream script).
    confdir = write_confdir(tmp_path, "[tank/a]\n    hourly = 0\n    daily = 3\n    weekly = 4\n    monthly = 0\n"
                                      "[b]\n    path = tank/a\n    hourly = 24\n")
    now = int(time.time())
    lines = [f"tank/a@autosnap_w{k}_weekly\tcreation\t{now - k * 7 * 86400 - 3600}\t-" for k in range(1, 5)]
    _, destroyed = _fake_zfs_world(monkeypatch, ["tank", "tank/a"], lines)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--prune-snapshots"])
    assert destroyed == []
    assert rc == 1
    assert capsys.readouterr().err == (f"FATAL: [tank/a] and [b] in {confdir}/sanoid.conf both take or prune tank/a, each "
                                       "by its own settings. Keep one section for tank/a.\n")


@pytest.mark.parametrize(("use_template", "missing"), [
    ("template_prod", "template_template_prod"),  # #702, #938: the template's section name in place of its name
    ('"prod"', 'template_"prod"'),  # #1038: quotes around the name
], ids=["template_ prefix", "quoted"])
def test_a_use_template_spelled_as_upstreams_users_mistyped_it_is_refused(tmp_path, use_template, missing):
    # DEV-003: upstream applies nothing for it and runs the dataset on the defaults (hourly 48, monthly 6)
    conf = f"[tank]\n    use_template = {use_template}\n[template_prod]\n    hourly = 0\n    daily = 7\n"
    with pytest.raises(ps.SanoidError, match=rf"there's no \[{missing}\] section defined\.$"):
        _ = _load(tmp_path, conf)


def test_a_trailing_space_after_recursive_still_reaches_the_children(tmp_path, monkeypatch):
    # DEV-001. #834 and #901: with `recursive = yes ` (a trailing space) upstream's [nas/rpool_backup] didn't recurse, so
    # its children kept [nas]'s autosnap and took snapshots meant to come only by replication.
    conf = ("[nas]\n    use_template = nas\n    recursive = yes\n    process_children_only = yes\n"
            "[nas/rpool_backup]\n    use_template = zfsreceive\n    recursive = yes \n"
            "[template_nas]\n    hourly = 48\n    autosnap = yes\n"
            "[template_zfsreceive]\n    hourly = 48\n    autosnap = no\n")
    _fake_zfs_tree(monkeypatch, ["nas", "nas/other", "nas/rpool_backup", "nas/rpool_backup/misc"])
    config = _load(tmp_path, conf)
    assert config["nas/rpool_backup/misc"]["autosnap"] is False
    assert config["nas/other"]["autosnap"] is True


@pytest.mark.parametrize("conf", [
    ("[back-up/Ubuntu]\n    use_template = backup\n    recursive = zfs\n"
     "[template_backup]\n    autoprune = yes\n    autosnap = no\n    autosnap = no\n"),
    "[back-up/Ubuntu]\n    recursive = zfs\n[template_default]\n    autoprune = yes\n    autosnap = no\n    autosnap = no\n",
], ids=["a template (fixed upstream since)", "[template_default] (not fixed upstream)"])
def test_a_repeated_autosnap_no_keeps_autosnap_off(tmp_path, monkeypatch, conf):
    # DEV-001. #952: the repeated key became a Perl array reference, which reads as true, so a backup dataset meant never
    # to take snapshots took them.
    _fake_zfs_tree(monkeypatch, ["back-up", "back-up/Ubuntu", "back-up/Ubuntu/x"])
    config = _load(tmp_path, conf)
    assert config["back-up/Ubuntu"]["autosnap"] is False
    assert config["back-up/Ubuntu/x"]["autosnap"] is False


def test_a_hook_that_outlives_script_timeout_does_not_make_the_next_run_take_again(tmp_path, monkeypatch, capsys):
    # #1030: a post_snapshot_script running syncoid past the default 5 s. Upstream's timeout killed sanoid itself, so the
    # snapshot just taken never reached the cache, and the next runs took the daily again, two or three a day. DEV-042:
    # pysanoid kills the hook, records the snapshot and carries on.
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n    hourly = 0\n    monthly = 0\n"
                                      "    post_snapshot_script = sleep 30\n    script_timeout = 1\n")
    taken, _ = _fake_zfs_world(monkeypatch, ["tank", "tank/data"], [])
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    argv = ["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
            "--take-snapshots"]
    _ = ps.main(argv)
    assert len(taken) == 1
    assert "WARN: post_snapshot_script didn't finish in the allowed time!" in capsys.readouterr().err
    _ = ps.main(argv)
    assert len(taken) == 1  # the cache holds it: no second daily


# ---------------------------------------------------------------------------
# what a run reads must not change what it keeps (2026-10-10, the third audit). The real upstream script and pysanoid
# were run on a dataset cache older than the config, a child lookup that fails having listed nothing, caches dated in
# the future and a snapshot cache cut short: both destroyed snapshots the config keeps. Tests marked "guard" pin
# behavior that already matched; the rest failed before their fix.
# ---------------------------------------------------------------------------

_KEEP_UNDER_TANK = (
    "[tank]\n    use_template = keep\n    recursive = yes\n"
    "[tank/c]\n    daily = 30\n"
    "[template_keep]\n    frequently = 0\n    hourly = 0\n    daily = 7\n    weekly = 4\n    monthly = 12\n    yearly = 3\n"
    "    autosnap = no\n    autoprune = yes\n"
)


def _kept_history(dataset: str, now: int) -> list[str]:
    """4 weeklies, 12 monthlies and 3 yearlies of `dataset`, each an hour inside what [template_keep] keeps."""
    ages = ([("weekly", k * 7 * 86400) for k in range(1, 5)] + [("monthly", k * 31 * 86400) for k in range(1, 13)]
            + [("yearly", int(k * 365.25 * 86400)) for k in range(1, 4)])
    return [f"{dataset}@autosnap_{kind[0]}{n}_{kind}\tcreation\t{now - age + 3600}\t-" for n, (kind, age) in enumerate(ages)]


def _prune(tmp_path: Path, confdir: Path) -> int:
    return ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                    "--prune-snapshots"])


def test_main_keeps_a_childs_snapshots_when_the_dataset_cache_is_older_than_its_section(tmp_path, monkeypatch):
    # DEV-006. datasets.txt was written before tank/c arrived and got its section, and is still in use. Upstream built
    # [tank/c] from the defaults (weekly 0, monthly 6, yearly 0) and destroyed 13 of the 19 snapshots its parent keeps.
    confdir = write_confdir(tmp_path, _KEEP_UNDER_TANK)
    (tmp_path / "cache").mkdir()
    _ = (tmp_path / "cache" / "datasets.txt").write_text("tank\ntank/a\n")  # fresh: tank's children as zfs listed them then
    _, destroyed = _fake_zfs_world(monkeypatch, ["tank", "tank/a", "tank/c"], _kept_history("tank/c", int(time.time())))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    assert _prune(tmp_path, confdir) == 0
    assert destroyed == []
    assert (tmp_path / "cache" / "datasets.txt").read_text() == "tank\ntank/a\n"  # used, and left as it was


def test_main_keeps_a_childs_snapshots_when_the_child_lookup_fails_having_listed_nothing(tmp_path, monkeypatch, capsys):
    # The lookup exits 1 with nothing listed while the tree is there, and the snapshot listing after it works. Both
    # read that as no children, so upstream and 0.1.39 built [tank/c] from the defaults and destroyed the same 13
    # (confirmed against real Perl).
    confdir = write_confdir(tmp_path, _KEEP_UNDER_TANK)
    _, destroyed = _fake_zfs_world(monkeypatch, ["tank", "tank/a", "tank/c"], _kept_history("tank/c", int(time.time())))
    world = subprocess.run

    def lookup_fails(cmd: list[str], text: bool = False, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if cmd[1] == "list":
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"internal error: failed to initialize ZFS library\n")
        return world(cmd, text=text, **kwargs)

    monkeypatch.setattr(subprocess, "run", lookup_fails)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    assert _prune(tmp_path, confdir) == 0
    assert destroyed == []
    assert "failed to initialize ZFS library" in capsys.readouterr().err  # zfs's complaint still reaches the journal


@pytest.mark.parametrize("recursive", ["yes", "zfs"])
def test_a_section_under_a_recursive_section_starts_from_its_settings_though_unlisted(tmp_path, monkeypatch, recursive):
    # 0.1.39, as upstream: weekly 0, monthly 6 and yearly 0, the defaults', and under `recursive = zfs` a child that
    # took its own snapshots beside its parent's `zfs snapshot -r`
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a"])  # tank/c isn't listed: not there yet, or the list is an old one
    conf = f"[tank]\n    recursive = {recursive}\n    weekly = 4\n    monthly = 12\n    yearly = 3\n[tank/c]\n    daily = 30\n"
    config = _load(tmp_path, conf)
    child = config["tank/c"]
    assert (child["weekly"], child["monthly"], child["yearly"], child["daily"]) == ("4", "12", "3", "30")
    assert child["path"] == "tank/c"
    assert child["autosnap"] is (recursive == "yes")  # the parent's own, or off under its `zfs snapshot -r`
    assert child.get("zfs_recursion", False) is (recursive == "zfs")
    assert config["tank/a"]["weekly"] == "4"  # a listed child, as before


def test_an_unlisted_grandchild_starts_from_the_nearest_recursive_section_above_it(tmp_path, monkeypatch):
    # as when zfs lists them all: [tank]'s recursion sets up both, then [tank/a]'s sets up tank/a/b again
    _fake_zfs_tree(monkeypatch, ["tank"])
    conf = ("[tank]\n    recursive = yes\n    weekly = 4\n    monthly = 12\n"
            "[tank/a]\n    recursive = yes\n    monthly = 24\n"
            "[tank/a/b]\n    daily = 30\n")
    grandchild = _load(tmp_path, conf)["tank/a/b"]
    assert (grandchild["weekly"], grandchild["monthly"], grandchild["daily"]) == ("4", "24", "30")


def test_a_section_under_a_skip_children_section_still_starts_from_the_defaults(tmp_path, monkeypatch):
    # guard: skip_children hands nothing down, so a child's own section is built from the defaults, listed or not
    _fake_zfs_tree(monkeypatch, ["tank"])
    config = _load(tmp_path, "[tank]\n    skip_children = yes\n    weekly = 4\n[tank/c]\n    daily = 30\n")
    assert (config["tank/c"]["weekly"], config["tank/c"]["daily"]) == ("0", "30")


def test_the_dataset_cache_holds_what_zfs_listed_and_each_sections_path(tmp_path, monkeypatch):
    # guard: as upstream's does. A section the lookup didn't list is there once, as its own path, not as a child.
    _fake_zfs_tree(monkeypatch, ["tank", "tank/a"])
    zfs = make_zfs_client(tmp_path)
    _ = ps.load_config(write_confdir(tmp_path, "[tank]\n    recursive = yes\n[tank/c]\n    daily = 30\n"), zfs)
    zfs.flush_dataset_cache(tmp_path / "run")
    assert (tmp_path / "cache" / "datasets.txt").read_bytes() == b"tank\ntank/a\ntank/c\n"


@pytest.mark.parametrize(("ahead", "asked"), [(0, 0), (1, 1), (365 * 86400, 1)],
                         ids=["written this second (guard)", "a second ahead", "a year ahead"])
def test_a_snapshot_cache_dated_in_the_future_counts_as_expired(tmp_path, monkeypatch, ahead, asked):
    # DEV-037: written while the clock ran ahead, it passes upstream's age test until real time reaches its date.
    monkeypatch.setattr(ps.time, "time", lambda: 1_790_000_000.0)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    cache.write_text("tank@autosnap_old_daily\tcreation\t1\t-\n")
    os.utime(cache, (1_790_000_000 + ahead, 1_790_000_000 + ahead))
    commands = _zfs_replies(monkeypatch, (0, "tank@autosnap_new_daily\tcreation\t2\t-\n", ""))
    snaps = ps.get_snapshots({"t": _sec("tank")}, cache_dir, tmp_path / "run", 1200)
    assert len(commands) == asked
    assert list(snaps["tank"]) == (["autosnap_new_daily"] if asked else ["autosnap_old_daily"])
    assert cache.read_text() == f"tank@autosnap_{'new' if asked else 'old'}_daily\tcreation\t{2 if asked else 1}\t-\n"


@pytest.mark.parametrize(("ahead", "from_zfs"), [(0, False), (1, True), (365 * 86400, True)],
                         ids=["written this second (guard)", "a second ahead", "a year ahead"])
def test_a_dataset_cache_dated_in_the_future_counts_as_no_cache(tmp_path, monkeypatch, ahead, from_zfs):
    # DEV-020: until real time reached its date, no dataset made since would be found under its recursive section.
    log = _script_zfs(tmp_path, monkeypatch, out=b"tank\ntank/live\n")
    cache_dir, path = _dataset_cache_file(tmp_path, monkeypatch, b"tank\ntank/old\n", age=-ahead)
    zfs = ps.ZfsClient(cache_dir, 1200, force_update=False)
    config = ps.load_config(write_confdir(tmp_path, "[tank]\n    recursive = yes\n"), zfs)
    zfs.flush_dataset_cache(tmp_path / "run")
    assert sorted(config) == (["tank", "tank/live"] if from_zfs else ["tank", "tank/old"])
    assert log.exists() is from_zfs
    assert path.read_bytes() == (b"tank\ntank/live\n" if from_zfs else b"tank\ntank/old\n")  # rewritten from zfs


def _dailies(now: int, *days: int) -> list[str]:
    return [f"tank/a@autosnap_d{k}_daily\tcreation\t{now - k * 86400 - 600}\t-" for k in days]


_THREE_DAILIES = "[tank/a]\n    daily = 3\n    hourly = 0\n    monthly = 0\n    autosnap = no\n"


def test_main_prunes_by_zfs_when_the_snapshot_cache_is_dated_in_the_future(tmp_path, monkeypatch):
    # The cache, dated a year ahead, still lists two dailies a rollback destroyed. Counted toward `daily = 3`, they let
    # upstream and 0.1.39 destroy 2 of the 3 dailies the pool holds, without asking zfs (confirmed against real Perl).
    confdir = write_confdir(tmp_path, _THREE_DAILIES)
    now = int(time.time())
    (tmp_path / "cache").mkdir()
    cache = tmp_path / "cache" / "snapshots.txt"
    cache.write_text("".join(f"{line}\n" for line in _dailies(now, 5, 4, 3, 2, 1)))
    os.utime(cache, (now + 365 * 86400, now + 365 * 86400))
    _, destroyed = _fake_zfs_world(monkeypatch, ["tank", "tank/a"], _dailies(now, 5, 4, 3))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    assert _prune(tmp_path, confdir) == 0
    assert destroyed == []


_TWO_LINES = "tank@autosnap_a_daily\tcreation\t1789000000\t-\ntank@autosnap_b_daily\tcreation\t1790000000\t-\n"


@pytest.mark.parametrize(("cut", "asked"), [(0, 0), (len(_TWO_LINES) // 2, 0), (1, 1), (8, 1)],
                         ids=["whole (guard)", "one whole line (guard)", "its last newline", "inside a creation time"])
def test_a_snapshot_cache_cut_short_inside_its_last_line_counts_as_no_cache(tmp_path, monkeypatch, cut, asked):
    # DEV-036: every cache written ends its last line, so one that doesn't lost its end, and a creation time that lost
    # digits reads as decades old.
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    cache.write_text(_TWO_LINES[:len(_TWO_LINES) - cut])  # fresh
    commands = _zfs_replies(monkeypatch, (0, "tank@autosnap_new_daily\tcreation\t2\t-\n", ""))
    snaps = ps.get_snapshots({"t": _sec("tank")}, cache_dir, tmp_path / "run", 1200)
    assert len(commands) == asked
    if asked:
        assert list(snaps["tank"]) == ["autosnap_new_daily"]
        assert cache.read_text() == "tank@autosnap_new_daily\tcreation\t2\t-\n"  # rewritten from zfs
    else:
        assert "autosnap_a_daily" in snaps["tank"]


def test_an_empty_snapshot_cache_is_still_a_cache(tmp_path, monkeypatch):
    # guard: a pool with no snapshots leaves an empty file, which has no last line to lose
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "snapshots.txt").write_text("")  # fresh
    commands = _zfs_replies(monkeypatch)
    assert ps.get_snapshots({"t": _sec("tank")}, cache_dir, tmp_path / "run", 1200) == {}
    assert commands == []


def test_main_prunes_by_zfs_when_the_snapshot_cache_was_cut_short(tmp_path, monkeypatch):
    # The newest daily's creation time lost its last digits and read as decades old: upstream and 0.1.39 destroyed it
    # and the oldest, keeping one 4 days old (confirmed against real Perl). The two oldest are the ones to go.
    confdir = write_confdir(tmp_path, _THREE_DAILIES)
    now = int(time.time())
    lines = _dailies(now, 5, 4, 3, 2, 1)
    (tmp_path / "cache").mkdir()
    _ = (tmp_path / "cache" / "snapshots.txt").write_text("".join(f"{line}\n" for line in lines)[:-8])  # fresh
    _, destroyed = _fake_zfs_world(monkeypatch, ["tank", "tank/a"], lines)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    assert _prune(tmp_path, confdir) == 0
    assert destroyed == ["tank/a@autosnap_d5_daily", "tank/a@autosnap_d4_daily"]


def test_get_snapshots_reads_zfs_when_the_lock_is_held_and_the_cache_was_cut_short(tmp_path, monkeypatch):
    # as with no cache at all (above): the listing comes from zfs, and the file is the lock holder's to replace
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache = cache_dir / "snapshots.txt"
    cache.write_text("tank@autosnap_old_daily\tcreation\t17")  # fresh
    commands = _zfs_replies(monkeypatch, (0, "tank@autosnap_new_daily\tcreation\t2\t-\n", ""))
    with open(run_dir / "sanoid_cacheupdate.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another process is updating the cache
        snaps = ps.get_snapshots({"t": _sec("tank")}, cache_dir, run_dir, 1200)
    assert list(snaps["tank"]) == ["autosnap_new_daily"]
    assert len(commands) == 1
    assert cache.read_text() == "tank@autosnap_old_daily\tcreation\t17"


# ---------------------------------------------------------------------------
# 0.1.41, from the same audit: a snapshot listing cut short inside its last line, and a count too long for a number.
# The real upstream script and pysanoid were run on each. Tests marked "guard" pin behavior that already matched; the
# rest failed before their fix. A run's snapshots dated ahead of the clock are DEV-051's, tested with it.
# ---------------------------------------------------------------------------

_GONE = "cannot open 'gone': dataset does not exist\n"


@pytest.mark.parametrize(("cut", "kept"), [
    (0, ["autosnap_a_daily", "autosnap_b_daily"]), (len(_TWO_LINES) // 2, ["autosnap_a_daily"]),
    (1, ["autosnap_a_daily"]), (8, ["autosnap_a_daily"]), (len(_TWO_LINES) // 2 + 1, []),
], ids=["whole (guard)", "ends after a whole line (guard)", "its last newline", "inside a creation time", "inside its only line"])
def test_a_listing_cut_short_inside_its_last_line_goes_without_that_line(tmp_path, monkeypatch, capsys, cut, kept):
    # DEV-035. The first `zfs get` fails and the retry is killed: zfs ends every line it prints, so a listing that
    # doesn't end in one was cut short there. Upstream reads the cut line, and a creation time that lost digits reads
    # as decades old.
    commands = _zfs_replies(monkeypatch, (1, "", _GONE), (-9, _TWO_LINES[:len(_TWO_LINES) - cut], ""))
    problems: list[str] = []
    snaps = ps.get_snapshots({"t": _sec("tank"), "g": _sec("gone")}, tmp_path / "cache", tmp_path / "run", 1200,
                             problems=problems)
    assert len(commands) == 2
    assert list(snaps.get("tank", {})) == kept
    whole = _TWO_LINES.splitlines(keepends=True)[:len(kept)]
    assert (tmp_path / "cache" / "snapshots.txt").read_text() == "".join(whole)  # the cache holds whole lines only
    # as upstream: nothing but zfs's own complaint, and no problem for the exit status
    assert problems == []
    assert capsys.readouterr().err == _GONE


def test_a_first_listing_that_ends_inside_a_line_goes_without_it_too(tmp_path, monkeypatch):
    # one rule for both listings: no zfs that finished leaves a line unended
    commands = _zfs_replies(monkeypatch, (0, _TWO_LINES[:-8], ""))
    snaps = ps.get_snapshots({"t": _sec("tank")}, tmp_path / "cache", tmp_path / "run", 1200)
    assert len(commands) == 1
    assert list(snaps["tank"]) == ["autosnap_a_daily"]


def test_a_listing_cut_short_is_read_from_zfs_while_the_lock_is_held_and_no_cache_written(tmp_path, monkeypatch):
    # with no readable cache and the update lock held, the listing comes from zfs: the same rule there
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _zfs_replies(monkeypatch, (-9, "", ""), (-9, _TWO_LINES[:-8], ""))
    with open(run_dir / "sanoid_cacheupdate.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another process is updating the cache
        snaps = ps.get_snapshots({"t": _sec("tank")}, tmp_path / "cache", run_dir, 1200)
    assert list(snaps["tank"]) == ["autosnap_a_daily"]
    assert not (tmp_path / "cache" / "snapshots.txt").exists()


def test_main_keeps_the_newest_daily_when_the_snapshot_listing_is_cut_short(tmp_path, monkeypatch, capsys):
    # DEV-035. The first `zfs get` fails, a configured dataset being away, and the retry is killed 8 bytes short: the
    # newest daily's creation time lost its last digits and read as decades old. Upstream destroyed it and the oldest. Of the four dailies still listed, the oldest is the one to go. The run
    # ends as upstream's does: exit 0, with nothing said but zfs's own complaint.
    confdir = write_confdir(tmp_path, _THREE_DAILIES + "[gone/x]\n    daily = 3\n    hourly = 0\n    monthly = 0\n"
                                                       "    autosnap = no\n")
    now = int(time.time())
    lines = _dailies(now, 5, 4, 3, 2, 1)
    _, destroyed = _fake_zfs_world(monkeypatch, ["tank", "tank/a"], lines)
    world = subprocess.run

    def killed_retry(cmd: list[str], text: bool = False, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if cmd[1] == "get" and len(cmd) > 5:  # with dataset names
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"cannot open 'gone/x': dataset does not exist\n")
        if cmd[1] == "get":
            return subprocess.CompletedProcess(cmd, -9, stdout="".join(f"{ln}\n" for ln in lines).encode()[:-8], stderr=b"")
        return world(cmd, text=text, **kwargs)

    monkeypatch.setattr(subprocess, "run", killed_retry)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    assert _prune(tmp_path, confdir) == 0
    assert destroyed == ["tank/a@autosnap_d5_daily"]
    assert capsys.readouterr().err == "cannot open 'gone/x': dataset does not exist\n"


@pytest.mark.parametrize(("raw", "count"), [
    ("9" * 18, 10**18 - 1), ("0" * 30 + "5", 5), ("9" * 19, 2**63 - 1), ("9" * 400, 2**63 - 1), ("9" * 5000, 2**63 - 1),
    ("0" * 5000 + "7", 7), ("9" * 400 + " # forever", 2**63 - 1),
], ids=["18 digits (guard)", "leading zeros (guard)", "19 digits", "400 digits", "5000 digits", "5000 zeros and a 7",
        "400 digits and a comment"])
def test_a_count_too_long_for_a_number_reads_as_one_no_dataset_reaches(raw, count):
    # Parity: upstream reads it as a float, infinity past 308 digits, and carries on. int() refuses more than 4,300
    # digits, leading zeros counted, which ended the run with a traceback.
    assert ps._cfg_count({"daily": raw}, "daily", 0) == count


@pytest.mark.parametrize("snap_type", ["yearly", "daily", "frequently"])
@pytest.mark.parametrize("digits", [19, 400, 5000])
def test_prune_keeps_every_snapshot_of_a_type_whose_count_is_too_long_for_a_number(snap_type, digits):
    # Parity (confirmed against real Perl): none of that type is pruned. `yearly`'s period is a float, and a count
    # past 308 digits overflowed it: prune ended with a traceback after the sections above it were pruned.
    cfg = {"path": "tank/a", snap_type: "9" * digits, "frequent_period": "15"}
    old = [(f"autosnap_{k}_{snap_type}", float(k)) for k in range(5)]  # made in 1970
    assert ps.select_prune_candidates(cfg, snap_type, old, 1_790_000_000) == []


def test_a_prune_defer_too_long_for_a_number_is_refused_like_any_other_over_100():
    # upstream's check_capacity_limit refuses it too; here it was a traceback
    with pytest.raises(ps.SanoidError, match="invalid prune_defer limit for 'tank/a': 9{400} is not between 0 and 100"):
        _ = ps._prune_defer_limit({"path": "tank/a", "prune_defer": "9" * 400})
    with pytest.raises(ps.SanoidError, match="0150 is not between 0 and 100"):  # as written, not as read
        _ = ps._prune_defer_limit({"path": "tank/a", "prune_defer": "0150"})


@pytest.mark.parametrize("digits", [400, 5000], ids=["400 digits (guard)", "5000 digits"])
def test_take_takes_a_type_whose_count_is_too_long_for_a_number(digits):
    # Parity (confirmed against real Perl): more than 0, so the type is taken. Past 4,300 digits take reported it and
    # took none.
    problems: list[str] = []
    plan = ps.plan_snapshots({"tank/a": _take_cfg("tank/a", daily="9" * digits)}, {}, now=NOW_APRIL, problems=problems)
    assert plan["tank/a"].types == ["daily"]
    assert problems == []


@pytest.mark.parametrize("digits", [400, 5000])
def test_main_prunes_the_other_sections_when_a_count_is_too_long_for_a_number(tmp_path, monkeypatch, digits):
    # 0.1.40 ended in a traceback at [tank/b], before [tank/a] was pruned. Upstream keeps tank/b's yearly and prunes the
    # rest (confirmed against real Perl).
    confdir = write_confdir(tmp_path, f"[tank/b]\n    hourly = 0\n    monthly = 0\n    autosnap = no\n    yearly = {'9' * digits}\n"
                                      "[tank/a]\n    daily = 3\n    hourly = 0\n    monthly = 0\n    autosnap = no\n")
    now = int(time.time())
    lines = [f"tank/b@autosnap_y1_yearly\tcreation\t{now - 400 * 86400}\t-", *_dailies(now, 5, 4, 3, 2, 1)]
    _, destroyed = _fake_zfs_world(monkeypatch, ["tank", "tank/a", "tank/b"], lines)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    assert _prune(tmp_path, confdir) == 0
    assert destroyed == ["tank/a@autosnap_d5_daily", "tank/a@autosnap_d4_daily"]


def _newest_daily(ahead: int | str, now: int) -> dict[str, dict[str, Any]]:
    """The per-type table of tank/a holding a daily two days old and one dated `ahead` seconds after `now`."""
    made = ahead if isinstance(ahead, str) else now + ahead
    snaps = ps._parse_snapshot_lines([f"tank/a@autosnap_old_daily\tcreation\t{now - 2 * 86400}\t-",
                                      f"tank/a@autosnap_ahead_daily\tcreation\t{made}\t-"])
    return ps.snapshots_by_type(snaps, {"tank/a": _take_cfg("tank/a")}, now=now)


@pytest.mark.parametrize("ahead", [1, 301, 365 * 86400, "9" * 400], ids=["a second", "301 s", "a year", "a damaged line's 400 digits"])
def test_a_type_whose_newest_snapshot_is_dated_in_the_future_is_not_taken_and_nothing_is_said(capsys, ahead):
    # guard: plan_snapshots is upstream's take_snapshots here, and given a newest dated ahead it holds the type back,
    # saying nothing (confirmed against real Perl, upstream #651). main sets aside what is dated more than five
    # minutes ahead before it plans (DEV-051).
    now = int(NOW_APRIL.timestamp())
    problems: list[str] = []
    plan = ps.plan_snapshots({"tank/a": _take_cfg("tank/a")}, _newest_daily(ahead, now), now=NOW_APRIL, problems=problems)
    assert plan == {}
    assert problems == []
    assert capsys.readouterr().err == ""


def test_a_type_dated_in_the_future_does_not_hold_back_another_that_is_due():
    # guard: the hourly, none yet, is taken
    now = int(NOW_APRIL.timestamp())
    problems: list[str] = []
    plan = ps.plan_snapshots({"tank/a": _take_cfg("tank/a", hourly=24)}, _newest_daily(86400, now), now=NOW_APRIL,
                             problems=problems)
    assert plan["tank/a"].types == ["hourly"]
    assert problems == []


def _main_on(tmp_path, monkeypatch, conf: str, lines: list[str], action: str) -> tuple[int, list[str], list[str]]:
    """main() on tank/a's snapshot `lines`: (exit status, taken, destroyed)."""
    confdir = write_confdir(tmp_path, conf)
    taken, destroyed = _fake_zfs_world(monkeypatch, ["tank", "tank/a"], lines)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  action])
    return rc, taken, destroyed


_TAKE_THREE_DAILIES = "[tank/a]\n    daily = 3\n    hourly = 0\n    monthly = 0\n"


@pytest.mark.parametrize("ahead", [365 * 86400, 301, "9" * 400], ids=["a year", "301 s", "a damaged line's 400 digits"])
def test_main_takes_the_daily_when_its_newest_is_dated_more_than_five_minutes_ahead(tmp_path, monkeypatch, capsys, ahead):
    # DEV-051 (#1119): the clock ran ahead for one take and was set right. Upstream takes no daily until the clock passes
    # that snapshot, says nothing, and counts it, so its prune destroys d4 of the three real ones daily = 3 keeps.
    # pysanoid sets it aside: it takes the daily, destroys none (prune reads the listing from before the take, DEV-013),
    # and leaves the one dated ahead on disk.
    now = int(time.time())
    made = ahead if isinstance(ahead, str) else now + ahead
    lines = [f"tank/a@autosnap_ahead_daily\tcreation\t{made}\t-", *_dailies(now, 4, 3, 2)]
    rc, taken, destroyed = _main_on(tmp_path, monkeypatch, _TAKE_THREE_DAILIES, lines, "--cron")
    assert rc == 0
    assert len(taken) == 1 and taken[0].endswith("_daily")
    assert destroyed == []
    assert capsys.readouterr().err == ""


def test_prune_neither_counts_nor_destroys_a_snapshot_dated_more_than_five_minutes_ahead(tmp_path, monkeypatch):
    # DEV-051 (#1119): counted, a daily dated a year ahead holds one of daily = 3's places for that year, and prune
    # destroys a real daily the count keeps (d4), as upstream does. pysanoid leaves it out of the count, and on disk.
    now = int(time.time())
    lines = [f"tank/a@autosnap_ahead_daily\tcreation\t{now + 365 * 86400}\t-", *_dailies(now, 6, 5, 4, 2, 1)]
    rc, _, destroyed = _main_on(tmp_path, monkeypatch, _THREE_DAILIES, lines, "--prune-snapshots")
    assert rc == 0
    assert destroyed == ["tank/a@autosnap_d6_daily", "tank/a@autosnap_d5_daily"]


def test_with_the_clock_behind_the_snapshots_the_daily_is_taken_and_none_destroyed(tmp_path, monkeypatch):
    # DEV-051: a clock set back (a dead RTC battery, say) dates every real snapshot ahead of it. Upstream takes nothing
    # while the clock stays behind; pysanoid takes the daily that is due, and destroys none of the real ones.
    now = int(time.time())
    rc, taken, destroyed = _main_on(tmp_path, monkeypatch, _TAKE_THREE_DAILIES,
                                    _dailies(now + 2 * 365 * 86400, 4, 3, 2), "--cron")
    assert rc == 0
    assert len(taken) == 1 and taken[0].endswith("_daily")
    assert destroyed == []


@pytest.mark.parametrize("ahead", [1, 240, 300], ids=["a second", "four minutes", "five minutes"])
def test_a_newest_snapshot_dated_five_minutes_ahead_or_less_still_holds_its_type_back(tmp_path, monkeypatch, ahead):
    # guard, DEV-051: within five minutes, as after a clock stepped back a little, it counts, as upstream's does
    now = int(time.time())
    lines = [f"tank/a@autosnap_ahead_daily\tcreation\t{now + ahead}\t-", *_dailies(now, 4, 3, 2)]
    rc, taken, _ = _main_on(tmp_path, monkeypatch, _TAKE_THREE_DAILIES, lines, "--take-snapshots")
    assert rc == 0
    assert taken == []


# ---------------------------------------------------------------------------
# 0.1.42, the last two findings of that audit, both places where pysanoid had drifted from upstream: what zfs or zpool
# prints that isn't UTF-8, and the time in snapshot names. The real upstream script and pysanoid were run on each. The
# first half goes through real executables standing in for zfs and zpool, because the decoding is subprocess.run's own
# and a faked one can't show it. Tests marked "guard" pin behavior that already matched; the rest failed before their fix.
# ---------------------------------------------------------------------------

_NOT_UTF8 = b"warning: caf\xe9\n"  # Latin-1's e acute: no UTF-8 text has that byte there
_APRIL_DAILY = "autosnap_2026-04-21_05:37:00_daily"


def _script_zpool(tmp_path: Path, monkeypatch, out: bytes = b"", err: bytes = b"", rc: int = 0) -> None:
    """As _script_zfs, in place of `zpool`."""
    _ = (tmp_path / "zpool_out").write_bytes(out)
    _ = (tmp_path / "zpool_err").write_bytes(err)
    script = tmp_path / "zpool"
    _ = script.write_text(f"#!/bin/sh\ncat {tmp_path}/zpool_out\ncat {tmp_path}/zpool_err >&2\nexit {rc}\n")
    script.chmod(0o755)
    monkeypatch.setattr(ps, "ZPOOL", str(script))


def _stderr_keeping_bytes(monkeypatch) -> io.StringIO:
    """sys.stderr as one that takes a byte that isn't UTF-8, as main's writes it back out: pytest's own refuses it."""
    monkeypatch.setattr(sys, "stderr", err := io.StringIO())
    return err


def test_a_destroy_that_worked_counts_though_zfs_printed_a_byte_that_is_not_utf8(tmp_path, monkeypatch):
    # Parity (confirmed against real Perl): upstream never decodes what zfs prints. 0.1.41 read it as text in the
    # locale's encoding, and a message it couldn't decode made the command one that "could not run": each snapshot was
    # destroyed, reported as `could not remove`, and left in the cache.
    log = _script_zfs(tmp_path, monkeypatch, err=_NOT_UTF8)
    err = _stderr_keeping_bytes(monkeypatch)
    case = _prune_case("tank/a", count=3)
    names = case[0]
    assert _run_prune(tmp_path, [case]) == [f"tank/a@{names[0]}", f"tank/a@{names[1]}"]
    assert log.read_text().splitlines() == [f"destroy tank/a@{names[0]}", f"destroy tank/a@{names[1]}"]
    assert [line.split("\t")[0] for line in _read_cache(tmp_path / "cache")] == [f"tank/a@{names[2]}"]
    assert err.getvalue() == "warning: caf\udce9\n" * 2  # zfs's own two warnings, and nothing of pysanoid's


def test_a_snapshot_that_worked_is_taken_though_zfs_printed_a_byte_that_is_not_utf8(tmp_path, monkeypatch):
    # 0.1.41: CRITICAL ERROR and a problem for the exit status, and the snapshot never reached the cache
    log = _script_zfs(tmp_path, monkeypatch, err=_NOT_UTF8)
    err = _stderr_keeping_bytes(monkeypatch)
    problems: list[str] = []
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a")}, {}, now=NOW_APRIL, problems=problems)
    assert list(taken) == [f"tank/a@{_APRIL_DAILY}"]
    assert problems == []
    assert log.read_text() == f"snapshot tank/a@{_APRIL_DAILY}\n"
    assert err.getvalue() == "warning: caf\udce9\n"


def test_a_failed_snapshot_is_reported_in_zfss_own_words_though_they_are_not_utf8(tmp_path, monkeypatch):
    # 0.1.41 gave the decoder's complaint in place of zfs's. The byte is kept, as a config's or a listing's is.
    _ = _script_zfs(tmp_path, monkeypatch, err=b"cannot create snapshot: caf\xe9 is busy\n", rc=1)
    err = _stderr_keeping_bytes(monkeypatch)
    problems: list[str] = []
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a")}, {}, now=NOW_APRIL, problems=problems)
    assert taken == {}
    assert problems == [f"zfs snapshot tank/a@{_APRIL_DAILY} failed, 256"]
    assert err.getvalue() == f"cannot create snapshot: caf\udce9 is busy\nCRITICAL ERROR: {problems[0]}\n"


def test_a_name_already_taken_is_retried_as_dst_though_zfss_complaint_is_not_utf8(tmp_path, monkeypatch):
    # upstream looks for "already exists" in the bytes zfs wrote; 0.1.41 never got to look, and took no snapshot
    script = tmp_path / "zfs"
    _ = script.write_text("#!/bin/sh\ncase \"$*\" in *dst_*) exit 0 ;; esac\n"
                          "printf \"cannot create snapshot 'caf\\351': dataset already exists\\n\" >&2\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setattr(ps, "ZFS", str(script))
    problems: list[str] = []
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a")}, {}, now=NOW_APRIL, problems=problems)
    assert list(taken) == ["tank/a@autosnap_2026-04-21_05:37:00dst_daily"]
    assert problems == []


def test_a_failed_destroy_is_reported_in_zfss_own_words_though_they_are_not_utf8(tmp_path, monkeypatch):
    _ = _script_zfs(tmp_path, monkeypatch, err=b"cannot destroy: caf\xe9 is busy\n", rc=1)
    err = _stderr_keeping_bytes(monkeypatch)
    case = _prune_case("tank/a", count=2)
    assert _run_prune(tmp_path, [case]) == []
    assert err.getvalue() == f"cannot destroy: caf\udce9 is busy\ncould not remove tank/a@{case[0][0]} : 256\n"


def test_a_pools_capacity_is_read_though_zpool_printed_a_byte_that_is_not_utf8(tmp_path, monkeypatch):
    # 0.1.41: "could not run", FATAL
    _script_zpool(tmp_path, monkeypatch, out=b"85%\n", err=_NOT_UTF8)
    assert ps.get_zpool_capacity("tank") == 85


def test_a_capacity_that_is_not_utf8_is_an_invalid_value_as_upstream_calls_it(tmp_path, monkeypatch):
    # both stop the prune here; 0.1.41 said zpool could not run
    _script_zpool(tmp_path, monkeypatch, out=b"caf\xe9\n")
    with pytest.raises(ps.SanoidError) as refused:
        _ = ps.get_zpool_capacity("tank")
    assert str(refused.value) == "'zpool list -H -o cap tank' command returned invalid capacity value (caf\udce9)!"


def test_main_writes_zfss_complaint_out_in_the_bytes_zfs_wrote(tmp_path, monkeypatch, capfdbinary):
    # as upstream's journal has them: its zfs writes to the terminal itself
    confdir = write_confdir(tmp_path, "[tank/a]\n    daily = 3\n    hourly = 0\n    monthly = 0\n")
    script = tmp_path / "zfs"
    _ = script.write_text("#!/bin/sh\n[ \"$1\" = snapshot ] || exit 0\n"
                          "printf 'cannot create snapshot: caf\\351 is busy\\n' >&2\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setattr(ps, "ZFS", str(script))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--take-snapshots"])
    assert rc == 1
    err = capfdbinary.readouterr().err
    assert err.startswith(b"cannot create snapshot: caf\xe9 is busy\nCRITICAL ERROR: zfs snapshot tank/a@autosnap_")
    assert b"_daily failed, 256\nERROR: 1 problem(s) this run" in err


def test_main_prunes_when_zpool_prints_a_byte_that_is_not_utf8(tmp_path, monkeypatch):
    # prune_defer asks zpool how full the pool is. 0.1.41 ended FATAL at that section, nothing pruned, on every run for
    # as long as zpool printed such a byte; upstream prunes (confirmed against real Perl: 3 of 5 dailies under daily = 2).
    confdir = write_confdir(tmp_path, _THREE_DAILIES + "    prune_defer = 50\n")
    listing = "".join(f"{line}\n" for line in _dailies(int(time.time()), 5, 4, 3, 2, 1))
    log = _script_zfs(tmp_path, monkeypatch, out=listing.encode())  # the listing; a destroy's output isn't read
    _script_zpool(tmp_path, monkeypatch, out=b"85%\n", err=_NOT_UTF8)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    assert _prune(tmp_path, confdir) == 0
    assert [call for call in log.read_text().splitlines() if call.startswith("destroy")] == [
        "destroy tank/a@autosnap_d5_daily", "destroy tank/a@autosnap_d4_daily"]


def test_a_zfs_command_holding_a_nul_byte_still_could_not_run():
    # guard: what "could not run" is left for, with a command that can't start
    with pytest.raises(ps.SanoidError, match="could not run .*embedded null byte"):
        _ = ps.run_zfs_cmd([ps.ZFS, "snapshot", "tank@a\x00b"])


def test_a_missing_zfs_binary_stops_the_run_before_anything_is_taken(tmp_path, monkeypatch, capsys):
    # DEV-045: upstream's unchecked pipe reads a zfs it can't exec as a listing that worked and found nothing, and
    # writes an empty, fresh cache: take tries every type and only warns, prune sees none, and the run exits 0. Here
    # it is a FATAL.
    confdir = write_confdir(tmp_path, "[tank/data]\n    daily = 7\n    hourly = 0\n    monthly = 0\n")
    monkeypatch.setattr(ps, "ZFS", str(tmp_path / "no_such_zfs"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    code = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                    "--cron"])
    assert code == 1
    assert capsys.readouterr().err.startswith(f"FATAL: could not run '{tmp_path / 'no_such_zfs'} get -Hrpt snapshot creation")
    assert not (tmp_path / "cache" / "snapshots.txt").exists()


def _moving_clock(monkeypatch, start: datetime) -> list[float]:
    """Puts pysanoid on a clock the test moves: datetime.now() reads clock[0], in epoch seconds, in TZ. Returns it."""
    clock = [start.timestamp()]

    class Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None):
            return cls.fromtimestamp(clock[0], tz)

    monkeypatch.setattr(ps, "datetime", Clock)
    monkeypatch.setattr(ps, "get_local_zoneinfo", lambda: TZ)
    return clock


def _zfs_taking(monkeypatch, clock: list[float], seconds: float, fail: dict[str, str] | None = None) -> list[str]:
    """Patches subprocess.run: every `zfs snapshot` takes `seconds` of the clock, and fails with fail[NAME] as its
    complaint when NAME is listed. Returns the names asked for."""
    asked: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        asked.append(cmd[-1])
        clock[0] += seconds
        err = (fail or {}).get(cmd[-1], "")
        return subprocess.CompletedProcess(cmd, 1 if err else 0, stdout=b"", stderr=err.encode())

    monkeypatch.setattr(subprocess, "run", fake_run)
    return asked


def test_each_datasets_snapshot_names_carry_the_time_its_turn_came(monkeypatch):
    # Parity (confirmed against real Perl): upstream reads the clock for the names inside its loop over the datasets.
    # 0.1.41 read it once, as the run began: with 2-second snapshots upstream's names were 2 seconds apart, dataset to
    # dataset, and pysanoid's all the same.
    clock = _moving_clock(monkeypatch, NOW_APRIL)
    asked = _zfs_taking(monkeypatch, clock, 2)
    config = {"tank/a": _take_cfg("tank/a", hourly=24), "tank/b": _take_cfg("tank/b"), "tank/c": _take_cfg("tank/c")}
    taken = ps.take_snapshots(config, {})
    assert asked == list(taken) == [
        "tank/a@autosnap_2026-04-21_05:37:00_daily", "tank/a@autosnap_2026-04-21_05:37:00_hourly",  # one time for both
        "tank/b@autosnap_2026-04-21_05:37:04_daily", "tank/c@autosnap_2026-04-21_05:37:06_daily"]


def test_a_datasets_hooks_get_the_names_its_snapshots_take_however_long_the_hooks_run(monkeypatch):
    # Parity: the clock is read before pre_snapshot_script runs, so a slow hook moves the next dataset's names and not
    # its own (confirmed against real Perl). 0.1.41 gave tank/b the time the run began.
    clock = _moving_clock(monkeypatch, NOW_APRIL)
    seen: list[tuple[str, ...]] = []

    def fake_run(cmd: list[str], env: dict[str, str] | None = None, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if env is None:
            seen.append(("zfs", cmd[-1]))
        else:  # a hook: the pre one takes a minute and a half
            seen.append((env["SANOID_TARGET"], env["SANOID_SCRIPT"], env["SANOID_SNAPNAME"], env["SANOID_SNAPNAMES"]))
            clock[0] += 90 if env["SANOID_SCRIPT"] == "pre" else 0
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = {"tank/a": _take_cfg("tank/a", pre_snapshot_script="/bin/true", post_snapshot_script="/bin/true"),
              "tank/b": _take_cfg("tank/b", pre_snapshot_script="/bin/true")}
    _ = ps.take_snapshots(config, {})
    a, b = _APRIL_DAILY, "autosnap_2026-04-21_05:38:30_daily"
    assert seen == [("tank/a", "pre", a, a), ("zfs", f"tank/a@{a}"), ("tank/a", "post", f"tank/a@{a}", a),
                    ("tank/b", "pre", b, b), ("zfs", f"tank/b@{b}")]


def test_a_dataset_its_hook_holds_back_still_costs_the_next_one_the_time_it_took(monkeypatch):
    # tank/a's pre_snapshot_script fails after a minute and a half, with no_inconsistent_snapshot: none taken of it
    clock = _moving_clock(monkeypatch, NOW_APRIL)

    def fake_run(cmd: list[str], env: dict[str, str] | None = None, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        clock[0] += 90 if env is not None else 0
        return subprocess.CompletedProcess(cmd, 1 if env is not None else 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    config = {"tank/a": _take_cfg("tank/a", pre_snapshot_script="/bin/false", no_inconsistent_snapshot=True),
              "tank/b": _take_cfg("tank/b")}
    assert list(ps.take_snapshots(config, {}, problems=[])) == ["tank/b@autosnap_2026-04-21_05:38:30_daily"]


def test_a_dst_retry_keeps_the_time_of_its_datasets_turn(monkeypatch):
    # the retry renames the name it was given, as upstream's does: no second look at the clock
    clock = _moving_clock(monkeypatch, NOW_APRIL)
    asked = _zfs_taking(monkeypatch, clock, 2, fail={"tank/b@autosnap_2026-04-21_05:37:02_daily": EXISTS})
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a"), "tank/b": _take_cfg("tank/b")}, {})
    assert list(taken) == [f"tank/a@{_APRIL_DAILY}", "tank/b@autosnap_2026-04-21_05:37:02dst_daily"]
    assert asked[1] == "tank/b@autosnap_2026-04-21_05:37:02_daily"


def test_a_readonly_run_names_what_it_would_take_by_the_same_clock(monkeypatch, capsys):
    # guard: nothing runs, so the clock stands still and every name is the one the run began with
    clock = _moving_clock(monkeypatch, NOW_APRIL)
    asked = _zfs_taking(monkeypatch, clock, 2)
    config = {"tank/a": _take_cfg("tank/a"), "tank/b": _take_cfg("tank/b")}
    assert ps.take_snapshots(config, {}, readonly=True, verbose=True) == {}
    assert asked == []
    assert capsys.readouterr().out == (f"INFO: taking snapshots...\ntaking snapshot tank/a@{_APRIL_DAILY}\n"
                                       f"taking snapshot tank/b@{_APRIL_DAILY}\n")


def test_a_callers_own_clock_plans_the_run_and_names_every_snapshot_of_it(monkeypatch):
    # guard: `now` is a clock that stands still, which these tests and dev/simulate_long_horizon.py take snapshots by.
    # At 05:37 a daily 6 hours old is due (the last 23:59 has passed since); by the machine's clock, 12 hours on, it isn't.
    clock = _moving_clock(monkeypatch, NOW_APRIL + timedelta(hours=12))
    monkeypatch.setattr(ps, "get_local_zoneinfo", lambda: pytest.fail("the machine's zone, read for a caller's clock"))
    _ = _zfs_taking(monkeypatch, clock, 2)
    by_type = {path: {"daily": {"newest": 6 * 3600}} for path in ("tank/a", "tank/b")}
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a"), "tank/b": _take_cfg("tank/b")}, by_type, now=NOW_APRIL)
    assert list(taken) == [f"tank/a@{_APRIL_DAILY}", f"tank/b@{_APRIL_DAILY}"]


def test_the_local_zone_is_read_once_a_run_however_many_datasets_it_names(monkeypatch):
    # guard: a $TZ pysanoid can't read is warned about once, not once for each dataset
    clock = _moving_clock(monkeypatch, NOW_APRIL)
    _ = _zfs_taking(monkeypatch, clock, 2)
    reads: list[int] = []
    monkeypatch.setattr(ps, "get_local_zoneinfo", lambda: reads.append(1) or TZ)
    taken = ps.take_snapshots({p: _take_cfg(p) for p in ("tank/a", "tank/b", "tank/c")}, {})
    assert len(taken) == 3
    assert reads == [1]


def test_main_names_each_datasets_snapshots_by_the_time_its_turn_came(tmp_path, monkeypatch):
    # through main, and into the cache: what the next run reads back
    confdir = write_confdir(tmp_path, "".join(f"[tank/{d}]\n    daily = 3\n    hourly = 0\n    monthly = 0\n" for d in "ab"))
    clock = _moving_clock(monkeypatch, NOW_APRIL)
    taken, _ = _fake_zfs_world(monkeypatch, ["tank", "tank/a", "tank/b"], [])
    world = subprocess.run

    def slow(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        clock[0] += 2 if cmd[1] == "snapshot" else 0
        return world(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", slow)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    rc = ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                  "--take-snapshots"])
    assert rc == 0
    assert taken == [f"tank/a@{_APRIL_DAILY}", "tank/b@autosnap_2026-04-21_05:37:02_daily"]
    assert [line.split("\t")[0] for line in _read_cache(tmp_path / "cache")] == taken


# ---------------------------------------------------------------------------
# 0.1.43: zfs's and zpool's own words, shown as upstream shows them. Upstream's zfs writes to the terminal itself,
# and upstream's own line after a command that failed gives Perl's wait status. The real upstream script was run beside
# pysanoid on 29 cases and the two outputs compared byte for byte. The reading of zpool's answer is upstream's, but for
# one that holds more than a capacity, which stays refused (DEV-016). Tests marked "guard" pin behavior
# that already matched; the rest failed before the change.
# ---------------------------------------------------------------------------

def _zfs_doing(tmp_path: Path, monkeypatch, body: str) -> None:
    """A real executable in place of `zfs`: a shell script with `body`, given zfs's own arguments."""
    script = tmp_path / "zfs"
    _ = script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    monkeypatch.setattr(ps, "ZFS", str(script))


def _take(tmp_path: Path, monkeypatch, *flags: str) -> int:
    confdir = write_confdir(tmp_path, "[tank/a]\n    daily = 3\n    hourly = 0\n    monthly = 0\n")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    return ps.main(["--configdir", str(confdir), "--cache-dir", str(tmp_path / "cache"), "--run-dir", str(tmp_path / "run"),
                    "--take-snapshots", *flags])


def test_main_shows_what_zfs_writes_on_a_snapshot_that_works(tmp_path, monkeypatch, capfd):
    # Parity (confirmed against real Perl): upstream's zfs writes to the terminal itself, so a warning from a snapshot
    # that worked reaches the journal, after the line announcing the snapshot. 0.1.42 read it and showed none of it.
    _zfs_doing(tmp_path, monkeypatch, '[ "$1" = snapshot ] && { echo "out: a note"; echo "err: a warning" >&2; }; exit 0')
    assert _take(tmp_path, monkeypatch, "--verbose") == 0
    out, err = capfd.readouterr()
    lines = out.splitlines()
    assert lines[lines.index("out: a note") - 1].startswith("taking snapshot tank/a@autosnap_")
    assert err == "err: a warning\n"


def test_main_writes_a_zfs_warning_out_in_the_bytes_zfs_wrote_and_adds_none(tmp_path, monkeypatch, capfdbinary):
    # a byte that isn't UTF-8, and no newline at the end: neither is touched
    _zfs_doing(tmp_path, monkeypatch, '[ "$1" = snapshot ] && printf "warning: caf\\351, no end" >&2; exit 0')
    assert _take(tmp_path, monkeypatch) == 0
    assert capfdbinary.readouterr().err == b"warning: caf\xe9, no end"


def test_main_gives_a_failed_snapshot_as_upstream_gives_it(tmp_path, monkeypatch, capfd):
    # zfs's complaint as zfs wrote it, then upstream's line with the wait status, and pysanoid's own closing line.
    # 0.1.42 put the complaint, trimmed, inside a line of its own wording, naming zfs by its path.
    _zfs_doing(tmp_path, monkeypatch, '[ "$1" = snapshot ] || exit 0\n'
                                      'printf "cannot create snapshot: out of space\\nusage: zfs snapshot ...\\n" >&2; exit 1')
    assert _take(tmp_path, monkeypatch) == 1
    lines = capfd.readouterr().err.splitlines()
    assert lines[:2] == ["cannot create snapshot: out of space", "usage: zfs snapshot ..."]
    assert re.fullmatch(r"CRITICAL ERROR: zfs snapshot tank/a@autosnap_[-0-9_:]+_daily failed, 256", lines[2])
    assert lines[3:] == ["ERROR: 1 problem(s) this run (see above); exiting non-zero so systemd shows the failure."]


@pytest.mark.parametrize(("returncode", "status"), [(1, 256), (3, 768), (255, 65280), (-9, 9), (-15, 15)],
                         ids=["exit 1", "exit 3", "exit 255", "SIGKILL", "SIGTERM"])
def test_a_failed_snapshots_line_gives_perls_wait_status(monkeypatch, capsys, returncode, status):
    # upstream prints $?: the exit status in the high byte, or the signal's number
    _fake_zfs_snapshot(monkeypatch, (returncode, ""))
    problems: list[str] = []
    assert ps.take_snapshots({"tank/a": _take_cfg("tank/a")}, {}, now=NOW_APRIL, problems=problems) == {}
    assert problems == [f"zfs snapshot tank/a@{_APRIL_DAILY} failed, {status}"]
    assert capsys.readouterr().err == f"CRITICAL ERROR: zfs snapshot tank/a@{_APRIL_DAILY} failed, {status}\n"


def test_a_failed_recursive_snapshots_line_names_the_flag_as_upstreams_does(monkeypatch):
    _fake_zfs_snapshot(monkeypatch, (1, ""))
    problems: list[str] = []
    _ = ps.take_snapshots({"tank/a": _take_cfg("tank/a", zfs_recursion=True)}, {}, now=NOW_APRIL, problems=problems)
    assert problems == [f"zfs snapshot -r tank/a@{_APRIL_DAILY} failed, 256"]


def test_a_dst_retry_that_fails_is_given_under_its_dst_name_after_both_complaints(monkeypatch, capsys):
    _fake_zfs_snapshot(monkeypatch, (1, "cannot create snapshot: dataset already exists\n"), (2, "out of space\n"))
    problems: list[str] = []
    assert ps.take_snapshots({"tank/a": _take_cfg("tank/a")}, {}, now=NOW_APRIL, problems=problems, verbose=True) == {}
    out, err = capsys.readouterr()
    assert out.splitlines()[1:] == [f"taking snapshot tank/a@{_APRIL_DAILY}",
                                    "taking dst snapshot tank/a@autosnap_2026-04-21_05:37:00dst_daily"]
    assert err == ("cannot create snapshot: dataset already exists\nout of space\n"
                   "CRITICAL ERROR: zfs snapshot tank/a@autosnap_2026-04-21_05:37:00dst_daily failed, 512\n")


def _destroys_ending(monkeypatch, returncode: int, complaint: bytes = b"") -> None:
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode, b"", complaint))


@pytest.mark.parametrize(("returncode", "status"), [(1, 256), (2, 512), (-9, 9)], ids=["exit 1", "exit 2", "SIGKILL"])
def test_a_failed_destroy_is_given_as_upstream_gives_it(tmp_path, monkeypatch, capsys, returncode, status):
    # zfs's complaint as zfs wrote it, then `could not remove NAME : $?`. 0.1.42 put the complaint in its own line.
    _destroys_ending(monkeypatch, returncode, b"cannot destroy: dataset is busy\n")
    case = _prune_case("tank/a", count=2)
    assert _run_prune(tmp_path, [case]) == []
    assert capsys.readouterr().err == f"cannot destroy: dataset is busy\ncould not remove tank/a@{case[0][0]} : {status}\n"


def test_what_zfs_writes_on_a_destroy_that_works_is_shown(tmp_path, monkeypatch, capfd):
    # 0.1.42 showed none of it
    _ = _script_zfs(tmp_path, monkeypatch, out=b"out: a note\n", err=b"err: a warning\n")
    assert len(_run_prune(tmp_path, [_prune_case("tank/a", count=3)], verbose=True)) == 2
    out, err = capfd.readouterr()
    lines = out.splitlines()
    assert [i for i, line in enumerate(lines) if line == "out: a note"] == [
        i + 1 for i, line in enumerate(lines) if line.startswith("INFO: pruning tank/a@")]
    assert err == "err: a warning\n" * 2


@pytest.mark.parametrize(("answer", "capacity"), [
    ("85%\n", 85), ("85%", 85), ("85%  \n", 85), ("85%\n\n", 85), ("0%\n", 0), ("085%\n", 85), ("100%\n", 100), ("999%\n", 999),
])
def test_a_capacity_alone_on_its_line_is_read(monkeypatch, answer, capacity):
    # guard: what real zpool answers, and the forms upstream and pysanoid both read
    _fake_zpool(monkeypatch, stdout=answer)
    assert ps.get_zpool_capacity("tank") == capacity


@pytest.mark.parametrize(("answer", "shown"), [
    (" 85%\n", ""), ("85%\t\n", "85%\t"), ("85%\r\n", "85%\r"), ("\n85%\n", ""), ("\t85%\n", "\t85%"), ("85%\f\n", "85%\f"),
    ("", ""), ("1000%\n", "1000%"), ("85\n", "85"), ("x 85%\n", "x"), ("%\n", "%"),
], ids=["a leading space", "a trailing tab", "CRLF", "a blank first line", "a leading tab", "a form feed", "nothing (guard)",
        "four digits (guard)", "no percent sign (guard)", "a word first (guard)", "no digits (guard)"])
def test_an_answer_upstream_cannot_read_stops_the_prune_in_upstreams_words(monkeypatch, answer, shown):
    # Parity (confirmed against real Perl): upstream reads the first word of the first line, and a tab, a carriage
    # return or a leading blank is part of that word. 0.1.42 trimmed the whole answer first and pruned by the number.
    _fake_zpool(monkeypatch, stdout=answer)
    with pytest.raises(ps.SanoidError) as refused:
        _ = ps.get_zpool_capacity("tank")
    assert str(refused.value) == f"'zpool list -H -o cap tank' command returned invalid capacity value ({shown})!"
    assert refused.value.prefix == "ERROR"


@pytest.mark.parametrize("answer", ["85%\n90%\n", "85% x\n", "85% 90%\n", "85%\nx\n", "85% \t.\n"],
                         ids=["two lines", "a second word", "two capacities", "a word on a second line", "a dot after blanks"])
def test_an_answer_holding_more_than_a_capacity_stops_the_prune(monkeypatch, answer):
    # guard, DEV-016: upstream prunes by the first word whatever follows it.
    _fake_zpool(monkeypatch, stdout=answer)
    with pytest.raises(ps.SanoidError) as refused:
        _ = ps.get_zpool_capacity("tank")
    assert str(refused.value) == f"'zpool list -H -o cap tank' returned more than a capacity value ({answer!r})"
    assert refused.value.prefix == "FATAL"


@pytest.mark.parametrize("answer", [
    "85%\n\N{NO-BREAK SPACE}", "85%\n\N{EM SPACE}\n", "85%\n\N{NEXT LINE}", "85%\n\N{LINE SEPARATOR}\n",
    "85%\n\N{IDEOGRAPHIC SPACE}", "85%\n\x1c", "85%\n\x1f\n", "85% \N{NO-BREAK SPACE}\n",
], ids=["a no-break space on a second line", "an em space", "NEL", "a line separator", "an ideographic space",
        "U+001C", "U+001F", "a no-break space after a space"])
def test_what_perl_does_not_call_whitespace_after_a_capacity_stops_the_prune(monkeypatch, answer):
    # DEV-016: str.strip() trims these and Perl's \s does not, so they are more than a capacity
    _fake_zpool(monkeypatch, stdout=answer)
    with pytest.raises(ps.SanoidError) as refused:
        _ = ps.get_zpool_capacity("tank")
    assert str(refused.value) == f"'zpool list -H -o cap tank' returned more than a capacity value ({answer!r})"
    assert refused.value.prefix == "FATAL"


@pytest.mark.parametrize("answer", ["85%\n\v", "85%\n\f\n", "85%\n\r\n", "85% \t\n", "85%\n \t \n"],
                         ids=["a vertical tab", "a form feed", "a carriage return", "blanks after it", "a blank line"])
def test_perls_whitespace_after_a_capacity_is_nothing_more(monkeypatch, answer):
    # guard, DEV-016: ASCII whitespace, as Perl's \s matches it, after the capacity is no second value
    _fake_zpool(monkeypatch, stdout=answer)
    assert ps.get_zpool_capacity("tank") == 85


def test_a_warning_from_zpool_is_shown_and_its_answer_read(monkeypatch, capsys):
    # 0.1.42 showed zpool's words only inside its own error, when the answer couldn't be read
    _fake_zpool(monkeypatch, stdout="85%\n", stderr="warning: a device is missing\n")
    assert ps.get_zpool_capacity("tank") == 85
    assert capsys.readouterr().err == "warning: a device is missing\n"


def test_main_stops_a_prune_whose_pool_is_away_in_zpools_words_and_upstreams(tmp_path, monkeypatch, capfd):
    # a pool named in sanoid.conf that isn't imported: zpool's complaint as it wrote it, then upstream's error.
    # 0.1.42: one line, `FATAL: ... returned invalid capacity value (''): cannot open 'tank': no such pool`.
    confdir = write_confdir(tmp_path, _THREE_DAILIES + "    prune_defer = 50\n")
    listing = "".join(f"{line}\n" for line in _dailies(int(time.time()), 5, 4, 3, 2, 1))
    log = _script_zfs(tmp_path, monkeypatch, out=listing.encode())
    _script_zpool(tmp_path, monkeypatch, err=b"cannot open 'tank': no such pool\n", rc=1)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    assert _prune(tmp_path, confdir) == 1
    assert capfd.readouterr().err == ("cannot open 'tank': no such pool\n"
                                      "ERROR: 'zpool list -H -o cap tank' command returned invalid capacity value ()!\n")
    assert "destroy" not in log.read_text()


@pytest.mark.parametrize(("complaint", "written"), [
    (b"cannot open 'gone': dataset does not exist\n", "cannot open 'gone': dataset does not exist\n"),
    (b"half a line", "half a line"), (b"\n\tindented\n\n", "\n\tindented\n\n"), (b"warning: caf\xe9\n", "warning: caf\udce9\n"),
    (b"one\r\ntwo\n", "one\r\ntwo\n"),
], ids=["a line (guard)", "no newline", "blank lines and a tab", "a byte that isn't UTF-8", "CRLF"])
def test_a_listings_complaint_is_written_out_as_zfs_wrote_it(tmp_path, monkeypatch, complaint, written):
    # Parity (confirmed against real Perl): upstream's zfs writes it to the terminal itself. 0.1.42 trimmed it, ended it
    # with a newline, and showed a byte that isn't UTF-8 as U+FFFD.
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, b"tank@autosnap_a_daily\tcreation\t1790000000\t-\n", complaint))
    err = _stderr_keeping_bytes(monkeypatch)
    snaps = ps.get_snapshots({"t": _sec("tank")}, tmp_path / "cache", tmp_path / "run", 1200)
    assert list(snaps["tank"]) == ["autosnap_a_daily"]
    assert err.getvalue() == written


def test_a_child_lookups_complaint_is_written_out_as_zfs_wrote_it(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, b"tank\ntank/a\n", b"note: caf\xe9, no end"))
    err = _stderr_keeping_bytes(monkeypatch)
    assert make_zfs_client(tmp_path).get_child_datasets("tank") == ["tank/a"]
    assert err.getvalue() == "note: caf\udce9, no end"


def test_main_writes_a_listings_complaint_out_in_the_bytes_zfs_wrote(tmp_path, monkeypatch, capfdbinary):
    confdir = write_confdir(tmp_path, _THREE_DAILIES)
    listing = "".join(f"{line}\\n" for line in _dailies(int(time.time()), 3, 2, 1))
    _zfs_doing(tmp_path, monkeypatch, f'[ "$1" = get ] && {{ printf "warning: caf\\351, no end" >&2; printf "{listing}"; }}; exit 0')
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # main() appends to PATH; setenv makes pytest restore it
    assert _prune(tmp_path, confdir) == 0
    assert capfdbinary.readouterr().err == b"warning: caf\xe9, no end"


def test_a_snapshot_that_worked_is_not_retried_whatever_zfs_wrote(monkeypatch):
    # guard: upstream looks for "already exists" only in what a snapshot that failed wrote
    calls = _fake_zfs_snapshot(monkeypatch, (0, "warning: a snapshot of that name already exists elsewhere\n"))
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a")}, {}, now=NOW_APRIL)
    assert list(taken) == [f"tank/a@{_APRIL_DAILY}"]
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# 0.1.45: a failed hook's warning in upstream's words, and a hook command that can't be exec'd. Upstream's runscript
# warns `WARN: $key failed, $?`, the wait status, and its system() returns -1 for words it can't exec. 0.1.44 gave the
# exit code, and ran the words through /bin/sh's `exec`: the shell's complaint and its 127 or 126 for a hook that never
# started. Each line here was read back from real Perl. capfd, so that anything a shell wrote would show.
# ---------------------------------------------------------------------------

def _hook_file(tmp_path: Path, name: str, text: str, mode: int = 0o755) -> Path:
    hook = tmp_path / name
    _ = hook.write_text(text)
    hook.chmod(mode)
    return hook


@pytest.mark.parametrize(("exit_code", "status"), [(1, 256), (3, 768), (255, 65280)])
def test_a_failed_hooks_warning_gives_its_wait_status_as_upstreams_does(tmp_path, capfd, exit_code, status):
    hook = _hook_file(tmp_path, "hook", f"#!/bin/sh\nexit {exit_code}\n")
    cfg = {"pre_snapshot_script": f"{hook} arg", "script_timeout": "5"}
    assert ps.run_hook_script(cfg, "pre_snapshot_script", "tank/a", {}) == exit_code
    assert capfd.readouterr().err == f"WARN: pre_snapshot_script failed, {status}\n"


@pytest.mark.parametrize("key", ["pre_snapshot_script", "post_snapshot_script", "pre_pruning_script", "pruning_script"])
def test_the_warning_names_the_hook_that_failed(tmp_path, capfd, key):
    hook = _hook_file(tmp_path, "hook", "#!/bin/sh\nexit 2\n")
    assert ps.run_hook_script({key: str(hook), "script_timeout": "5"}, key, "tank/a", {}) == 2
    assert capfd.readouterr().err == f"WARN: {key} failed, 512\n"


@pytest.mark.parametrize(("command", "status"), [("kill -9 $$", 9), ("kill -TERM $$", 15), ("true ; exit 3", 768)])
def test_a_hook_the_shell_runs_is_warned_about_with_its_wait_status(capfd, command, status):
    # a metacharacter, so /bin/sh -c runs it; for one a signal killed, $? is the signal's number
    assert ps.run_hook_script({"pruning_script": command, "script_timeout": "5"}, "pruning_script", "tank/a", {}) != 0
    assert capfd.readouterr().err == f"WARN: pruning_script failed, {status}\n"


def test_a_hook_that_works_is_not_warned_about(tmp_path, capfd):
    hook = _hook_file(tmp_path, "hook", "#!/bin/sh\nexit 0\n")
    assert ps.run_hook_script({"pruning_script": str(hook), "script_timeout": "5"}, "pruning_script", "tank/a", {}) == 0
    assert capfd.readouterr().err == ""


@pytest.mark.parametrize(("what", "reason"), [
    ("no such file", "No such file or directory"), ("not on PATH", "No such file or directory"),
    ("not executable", "Permission denied"), ("a directory", "Permission denied"),
    ("no such interpreter", "No such file or directory"), ("exit 0", "No such file or directory"),
    (":", "No such file or directory"), ("-c", "No such file or directory"),
])
def test_a_hook_command_that_cannot_be_execd_is_a_failed_hook_with_minus_one(tmp_path, capfd, what, reason):
    # Parity (confirmed against real Perl): upstream's system() execs the words itself and returns -1 when it can't,
    # so its warning says `failed, -1`. Through /bin/sh's `exec`, 0.1.44 gave the shell's 127 or 126 (1 for a missing
    # interpreter under bash), and under a /bin/sh that is bash a hook named `-c` worked.
    plain = _hook_file(tmp_path, "plain", "#!/bin/sh\nexit 0\n", 0o644)
    stray = _hook_file(tmp_path, "stray", "#!/nonexistent/interpreter\n")
    command = {
        "no such file": f"{tmp_path}/no_such_hook arg", "not on PATH": "pysanoid_no_such_hook_command arg",
        "not executable": f"{plain} arg", "a directory": f"{tmp_path} arg", "no such interpreter": f"{stray} arg",
    }.get(what, what)
    cfg = {"pre_pruning_script": command, "script_timeout": "5"}
    assert ps.run_hook_script(cfg, "pre_pruning_script", "tank/a", {}) == -1
    first, last = capfd.readouterr().err.splitlines()
    assert first.startswith(f"could not run '{command}': ") and reason in first
    assert last == "WARN: pre_pruning_script failed, -1"


def test_a_hook_script_with_no_interpreter_line_is_run_by_sh_as_execvp_runs_it(tmp_path, capfd):
    # execve refuses such a file (ENOEXEC), and execvp, which Perl calls, hands it to /bin/sh
    ran = tmp_path / "ran"
    hook = _hook_file(tmp_path, "bare", f'echo "$1" > {ran}\nexit 4\n')
    cfg = {"pruning_script": f"{hook} the-arg", "script_timeout": "5"}
    assert ps.run_hook_script(cfg, "pruning_script", "tank/a", {}) == 4
    assert ran.read_text() == "the-arg\n"
    assert capfd.readouterr().err == "WARN: pruning_script failed, 1024\n"


def test_a_hook_named_without_a_path_is_found_on_path(tmp_path, monkeypatch, capfd):
    _ = _hook_file(tmp_path, "pysanoid_test_hook", "#!/bin/sh\nexit 5\n")
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    cfg = {"pruning_script": "pysanoid_test_hook x", "script_timeout": "5"}
    assert ps.run_hook_script(cfg, "pruning_script", "tank/a", {}) == 5
    assert capfd.readouterr().err == "WARN: pruning_script failed, 1280\n"


def test_a_final_redirect_joins_the_hooks_stderr_to_its_stdout_without_a_shell(tmp_path, capfd):
    # guard: Perl does a final ` 2>&1` itself and execs the words, so the comment still reaches the hook
    hook = _hook_file(tmp_path, "noisy", '#!/bin/sh\necho "out $*"\necho err >&2\n')
    cfg = {"pruning_script": f"{hook} # note 2>&1", "script_timeout": "5"}
    assert ps.run_hook_script(cfg, "pruning_script", "tank/a", {}) == 0
    assert capfd.readouterr() == ("out # note\nerr\n", "")


def test_a_hook_that_cannot_start_for_want_of_a_process_is_told_as_one_that_cannot_be_execd(monkeypatch, capsys):
    # fork failing: upstream's system() returns -1 for it too (it waits first: DEV-043)
    def fake_run(cmd, **kwargs):
        raise _cannot_start()

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = {"pruning_script": "/bin/hook a b", "script_timeout": "5"}
    assert ps.run_hook_script(cfg, "pruning_script", "tank/a", {}) == -1
    assert capsys.readouterr().err == f"could not run '/bin/hook a b': {_cannot_start()}\nWARN: pruning_script failed, -1\n"


def test_take_warns_of_a_failed_pre_hook_in_upstreams_words_and_still_takes(monkeypatch, capsys):
    _ = _record_take_hooks(monkeypatch, {"/bin/pre": 3})
    taken = ps.take_snapshots({"tank/a": _take_cfg("tank/a", pre_snapshot_script="/bin/pre")}, {}, now=NOW_APRIL)
    assert list(taken) == [f"tank/a@{_APRIL_DAILY}"]
    assert capsys.readouterr().err == "WARN: pre_snapshot_script failed, 768\n"


def test_prune_warns_of_a_failed_veto_hook_in_upstreams_words_and_destroys_nothing(tmp_path, monkeypatch, capsys):
    case = _prune_case("tank/data", 3, pre_pruning_script="/bin/veto", script_timeout="5")
    destroyed, hooks = _record_prune_calls(monkeypatch, {"/bin/veto": 1})
    _run_prune(tmp_path, [case])
    assert destroyed == [] and hooks == ["/bin/veto"] * 2
    assert capsys.readouterr().err == "WARN: pre_pruning_script failed, 256\n" * 2


def test_without_a_redirect_a_hooks_stderr_stays_its_own(tmp_path, capfd):
    # guard
    hook = _hook_file(tmp_path, "noisy", "#!/bin/sh\necho out\necho err >&2\n")
    assert ps.run_hook_script({"pruning_script": str(hook), "script_timeout": "5"}, "pruning_script", "tank/a", {}) == 0
    assert capfd.readouterr() == ("out\n", "err\n")


def test_a_script_with_no_interpreter_line_gets_its_final_redirect_too(tmp_path, capfd):
    hook = _hook_file(tmp_path, "bare", "echo out\necho err >&2\n")
    cfg = {"pruning_script": f"{hook} 2>&1", "script_timeout": "5"}
    assert ps.run_hook_script(cfg, "pruning_script", "tank/a", {}) == 0
    assert capfd.readouterr() == ("out\nerr\n", "")


def test_a_hook_command_of_blanks_alone_fails_as_perls_child_with_nothing_to_exec(capfd):
    # Perl's child exits -1, a wait status of 65280 (confirmed against real Perl). A value from a config file is
    # trimmed, so only a caller's own blanks get here.
    assert ps.run_hook_script({"pruning_script": " \t ", "script_timeout": "5"}, "pruning_script", "tank/a", {}) == 255
    assert capfd.readouterr().err == "WARN: pruning_script failed, 65280\n"


# ---------------------------------------------------------------------------
# properties (2026-10-25, the sixth audit). Each test draws seeded cases, biased to the edges (2**31, 2**63, 2**64,
# 10**309, 10,000-digit counts, NUL, lone surrogates, other scripts' digits, spaces Perl's \s doesn't match), and holds
# one invariant over every draw; a failure names the draw that broke it. All held on 0.1.49 and hold on 0.1.50.
# ---------------------------------------------------------------------------

_DRAWS = 3000
_EDGE_INTS = [0, 1, 2, 59, 60, 61, 99, 100, 101, 2**31 - 1, 2**31, 2**32, 2**53, 2**53 + 1, 2**63 - 1, 2**63,
              2**64 - 1, 2**64, 10**18, 10**19, 10**308, 10**309]
_JUNK = ["0", "1", "9", " ", "\t", "\r", "\n", "\x0b", "\x0c", "#", ";", "+", "-", "_", ".", "e", "x", "inf", "nan",
         "\0", chr(0xDCFF), "\N{ARABIC-INDIC DIGIT THREE}", "\N{FULLWIDTH DIGIT ONE}", "\xa0", "\x85",
         "\N{LINE SEPARATOR}", "\x1c", "=", "[", "]", "<<", "ly", "@", "a"]
_ZFS_NAME_CHARS = "abcXYZ019_-:. "  # what zfs allows in a component name
_ZONES = sorted(available_timezones())


def _holds(prop, seed, draws=_DRAWS):
    for i in range(draws):
        try:
            prop(random.Random(seed * 1_000_003 + i))
        except AssertionError as e:
            raise AssertionError(f"draw {i} of seed {seed}: {e}") from None


def _edge_digits(rng):
    pick = rng.random()
    if pick < 0.4:
        return str(rng.choice(_EDGE_INTS))
    if pick < 0.6:
        return "0" * rng.randint(1, 30) + str(rng.randint(0, 99))
    if pick < 0.7:
        return "9" * rng.choice([18, 19, 20, 308, 309, 4300, 4301, 10_000])
    return str(rng.randint(0, 10**rng.randint(1, 25)))


def _junk(rng, n=12):
    return "".join(rng.choice(_JUNK) for _ in range(rng.randint(0, n)))


def _zfs_name(rng, n=8):
    return "".join(rng.choice(_ZFS_NAME_CHARS) for _ in range(rng.randint(1, n))).strip() or "a"


def _prune_draw(rng):
    """A type, its count, and its snapshots oldest first: scattered over 400 days, or one a period as take makes them,
    reaching past the age window, with some on its boundary, dated 0, in the future, past 2**64 or infinity."""
    snap_type = rng.choice(ps.PERIOD_TYPES)
    wanted = rng.choice([0, 1, 2, 3, 7, 30, 1000, 2**63 - 1]) if rng.random() < 0.3 else rng.randint(0, 40)
    cfg = {"path": "tank/x", snap_type: str(wanted)}
    if snap_type == "frequently":
        cfg["frequent_period"] = rng.choice(["15", "1", "60", "-15", "0", "10000000000000000000000", "7"])
    now = int(time.time())
    count = rng.choice([0, 1, 2, rng.randint(0, 120)])
    period = int(ps._PERIOD_SECONDS[snap_type] or 900)  # frequently's comes from frequent_period
    if rng.random() < 0.5:
        ctimes = [now - rng.randint(-3600, 400 * 86400) for _ in range(count)]
    else:
        ctimes = [now - i * period - rng.randint(-5, 5) for i in range(min(count, 2 * wanted + 5))]
        edge = now - min(wanted, 10**6) * period
        ctimes += [edge + d for d in (-1, 0, 1) if rng.random() < 0.5]
    ctimes += [rng.choice([0, math.inf, 2**64 - 1, now + 10**6, -1]) for _ in range(count) if rng.random() < 0.05]
    return cfg, snap_type, [(f"autosnap_{i}_{snap_type}", c) for i, c in enumerate(sorted(ctimes))], now


def _prune_or_none(cfg, snap_type, snaps, now):
    try:
        return ps.select_prune_candidates(cfg, snap_type, snaps, now)
    except ps.SanoidError:  # frequent_period = 0
        return None


def test_for_any_snapshots_prune_never_goes_below_the_count():
    def prop(rng):
        cfg, t, snaps, now = _prune_draw(rng)
        out = _prune_or_none(cfg, t, snaps, now)
        assert out is None or len(snaps) - len(out) >= min(int(cfg[t]), len(snaps)), (cfg, len(snaps), out)
    _holds(prop, 1)


def test_for_any_snapshots_prune_takes_the_oldest_first_and_never_the_newest():
    def prop(rng):
        cfg, t, snaps, now = _prune_draw(rng)
        out = _prune_or_none(cfg, t, snaps, now)
        if out is None:
            return
        names = [name for name, _ in snaps]
        assert out == names[:len(out)], (cfg, out[:3])
        assert not (int(cfg[t]) >= 1 and snaps and names[-1] in out), cfg
    _holds(prop, 2)


def test_for_any_snapshots_prune_destroys_only_those_past_the_age_window():
    def prop(rng):
        cfg, t, snaps, now = _prune_draw(rng)
        period = 60 * int(cfg["frequent_period"]) if t == "frequently" else ps._PERIOD_SECONDS[t] or 0
        out = _prune_or_none(cfg, t, snaps, now)
        if out is None or period <= 0:
            return
        too_young = [(name, c) for name, c in snaps if name in out and not c < now - int(cfg[t]) * period]
        assert not too_young, (cfg, too_young[:3], now)
    _holds(prop, 3)


def test_for_any_snapshots_a_larger_count_destroys_no_more_and_a_later_clock_no_less():
    def prop(rng):
        cfg, t, snaps, now = _prune_draw(rng)
        base = _prune_or_none(cfg, t, snaps, now)
        if base is None:
            return
        later = ps.select_prune_candidates(cfg, t, snaps, now + rng.randint(0, 10**7))
        more = ps.select_prune_candidates({**cfg, t: str(int(cfg[t]) + rng.randint(1, 50))}, t, snaps, now)
        assert set(base) <= set(later), cfg
        assert set(more) <= set(base), cfg
    _holds(prop, 4)


def test_a_negative_frequent_period_leaves_only_the_count_standing():
    # parity with upstream's arithmetic: every frequent snapshot is past a negative window, even one a minute old, and
    # the keep-at-least-N floor holds
    now = int(time.time())
    snaps = [(f"s{i}", now - 60 * i) for i in range(10, 0, -1)]
    cfg = {"path": "tank/x", "frequently": "4", "frequent_period": "-15"}
    assert ps.select_prune_candidates(cfg, "frequently", snaps, now) == ["s10", "s9", "s8", "s7", "s6", "s5"]


def test_for_any_value_the_numeric_readers_raise_only_sanoid_error():
    # strings only: a config gives every one of these settings as text (toggles are its only other values)
    def prop(rng):
        pick = rng.random()
        value = (_edge_digits(rng) if pick < 0.35 else _junk(rng) if pick < 0.9 else
                 rng.choice(["", " ", "-1", "-0", "+5", "1_0", "1e3", "0x10", "3o", "7 # week", "# off", "inf", "nan"]))
        cfg = {"path": "x", "daily": value, "frequent_period": value, "prune_defer": value, "hourly_min": value}
        for call in (lambda: ps._cfg_count(cfg, "daily", 0, "x"), lambda: ps._cfg_int(cfg, "frequent_period", 15, "x"),
                     lambda: ps._prune_defer_limit(cfg), lambda: ps._take_count(cfg, "daily", "x", []),
                     lambda: ps._schedule_setting(cfg, "hourly_min", 0, "x", [])):
            with contextlib.suppress(ps.SanoidError), contextlib.redirect_stderr(io.StringIO()):
                _ = call()
    _holds(prop, 5)


def test_for_any_value_a_count_is_read_from_its_ascii_digits_whole_up_to_18():
    def prop(rng):
        raw = _edge_digits(rng) if rng.random() < 0.6 else _junk(rng)
        try:
            got = ps._cfg_count({"daily": raw}, "daily", 0)
        except ps.SanoidError:
            assert not re.fullmatch(r"[0-9]+(?:[ \t\r\n\f\v]*[#;].*)?", raw), repr(raw)
            return
        digits = re.match(r"[0-9]+", raw)
        assert digits is not None, repr(raw)
        significant = digits.group().lstrip("0")
        assert got == (int(significant or "0") if len(significant) <= 18 else 2**63 - 1), repr(raw)
    _holds(prop, 6)


def test_for_any_text_perls_number_is_a_float_and_a_hook_timeout_fits_a_c_int():
    def prop(rng):
        raw = _junk(rng, 30) if rng.random() < 0.7 else _edge_digits(rng) + _junk(rng, 4)
        assert isinstance(ps._perl_number(raw), float), repr(raw)
        assert 0 <= ps._alarm_seconds(raw) <= 2**31 - 1, repr(raw)
    _holds(prop, 7)


def test_for_any_name_a_cache_line_take_writes_reads_back_as_its_snapshot():
    def prop(rng):
        path = "/".join(_zfs_name(rng) for _ in range(rng.randint(1, 4)))
        t = rng.choice(ps.PERIOD_TYPES)
        name = ps._autosnap_name(datetime.fromtimestamp(rng.randint(0, 2**33), UTC), t)
        if rng.random() < 0.3:
            name = re.sub(r"_([a-z]+)$", r"dst_\1", name)  # take's dst_ retry
        ctime = rng.randint(0, 2**40)
        line = f"{path}@{name}\tcreation\t{ctime}\t-"
        assert ps._parse_snapshot_lines([line]) == {path: {name: ps.Snapshot(path, name, t, ctime)}}, line
    _holds(prop, 8)


def test_for_any_cache_removal_drops_exactly_the_destroyed_lines_and_keeps_every_other_byte(tmp_path):
    def prop(rng):
        lines = [f"{_zfs_name(rng)}@autosnap_{rng.randint(0, 99)}_{rng.choice(ps.PERIOD_TYPES)}"
                 + rng.choice(["\tcreation\t123\t-", "\tcreation\t", "\r", "", "\tcreation\t9\t-\r"]) + "\n"
                 for _ in range(rng.randint(0, 40))]
        if lines and rng.random() < 0.2:
            lines[-1] = lines[-1].rstrip("\n")  # a last line cut short
        text = "".join(lines)
        cache = tmp_path / "snapshots.txt"
        cache.write_bytes(text.encode("utf-8", "surrogateescape"))
        pruned = {line.split("\t", 1)[0] for line in lines if rng.random() < 0.4}
        assert ps.remove_cached_snapshots(sorted(pruned), tmp_path, tmp_path / "run", wait=True)
        expected = "".join(line for line in lines if line.split("\t", 1)[0] not in pruned) if pruned else text
        assert cache.read_bytes().decode("utf-8", "surrogateescape") == expected, (text, pruned)
    _holds(prop, 9, draws=800)


def test_for_any_text_the_ini_parser_returns_errors_and_never_raises():
    def prop(rng):
        sections, errors = ps._parse_ini_text(_junk(rng, 60))
        assert isinstance(sections, dict) and isinstance(errors, list)
    _holds(prop, 10)


def test_for_any_line_ending_a_written_config_reads_back_as_written():
    def prop(rng):
        sep = rng.choice(["\n", "\r\n", "\r", "\x15"])
        expected = {}
        out = []
        for _ in range(rng.randint(1, 5)):
            section = f"{_zfs_name(rng)}/{_zfs_name(rng)}"
            expected.setdefault(section, {})
            out.append(f"{rng.choice(['', ' ', chr(9)])}[ {section} ]")
            for _ in range(rng.randint(0, 5)):
                key = rng.choice(["daily", "hourly", "use_template", "pre_snapshot_script", "path"])
                value = rng.choice(["7", "yes", "a = b", "[x]", "x # y", "  spaced", "=", ""])
                out.append(f"{key}{rng.choice(['=', ' = ', chr(9) + '=' + chr(9)])}{value}")
                expected[section].setdefault(key, value.lstrip(ps._WS))  # a repeated key reads as its first value
        sections, errors = ps._parse_ini_text(sep.join(out) + sep)
        assert not errors, errors
        assert {s: {k: v[0] for k, v in params.items()} for s, params in sections.items()} == expected, out
    _holds(prop, 11)


def test_for_any_zone_and_time_the_last_preferred_time_is_past_recent_and_on_the_configured_clock():
    bound = {"hourly": 2 * 3600 + 60, "daily": 86400 + 2 * 3600, "weekly": 7 * 86400 + 2 * 3600,
             "monthly": 31 * 86400 + 2 * 3600, "yearly": 366 * 86400 + 2 * 3600}

    def prop(rng):
        zone = ZoneInfo(rng.choice(_ZONES))
        now = datetime.fromtimestamp(rng.randint(946684800, 2145830400) + rng.random(), zone)  # 2000 to 2037
        t = rng.choice(ps.PERIOD_TYPES)
        cfg = {"path": "x", "frequent_period": str(rng.choice([1, 5, 7, 15, 30, 59, 60])),
               "hourly_min": str(rng.randint(0, 59)), "weekly_wday": str(rng.randint(0, 7)),
               "monthly_mday": str(rng.randint(1, 28)), "yearly_mon": str(rng.randint(1, 12)),
               "yearly_mday": str(rng.randint(1, 28))}
        for kind in ("daily", "weekly", "monthly", "yearly"):
            cfg[f"{kind}_hour"], cfg[f"{kind}_min"] = str(rng.randint(0, 23)), str(rng.randint(0, 59))
        got = ps._last_preferred_time(now, cfg, t)
        gap = now.timestamp() - got.timestamp()
        where = (str(zone), now.isoformat(), t, got.isoformat())
        assert gap >= 0, where
        if t == "frequently":
            period = int(cfg["frequent_period"])
            assert got.minute % period == 0 and gap < 60 * period + 3600, where
        elif t == "hourly":
            assert got.minute == int(cfg["hourly_min"]) and gap < bound[t], where
        else:
            assert (got.hour, got.minute) == (int(cfg[f"{t}_hour"]), int(cfg[f"{t}_min"])) and gap < bound[t], where
    _holds(prop, 13, draws=2 * _DRAWS)


def test_for_any_zone_and_time_a_snapshot_just_taken_holds_its_type_back():
    def prop(rng):
        zone = ZoneInfo(rng.choice(_ZONES))
        now = datetime.fromtimestamp(rng.randint(946684800, 2145830400), zone)
        t = rng.choice(ps.PERIOD_TYPES)
        cfg = {"path": "x", "autosnap": True, t: "3", "frequent_period": "15"}
        assert not ps.plan_snapshots({"x": cfg}, {"x": {t: {"newest": 0, "numsnaps": 1}}}, now, []), (str(zone), now, t)
    _holds(prop, 14)


def test_for_any_answer_a_pools_capacity_is_its_first_line_or_a_sanoid_error(monkeypatch):
    def prop(rng):
        answer = rng.choice([b"45%\n", b"100%\n", b"1000%\n", b"", b"\xff\n", b"45 %\n", b"45%\n46%\n", b"045%\n",
                             "\N{ARABIC-INDIC DIGIT THREE}%\n".encode()])
        if rng.random() < 0.3:
            answer += bytes(rng.randint(0, 255) for _ in range(rng.randint(1, 3)))
        monkeypatch.setattr(ps, "run_zfs_bytes", lambda cmd: subprocess.CompletedProcess(cmd, 0, answer, b""))
        try:
            cap = ps.get_zpool_capacity("tank")
        except ps.SanoidError:
            return
        first = answer.split(b"\n", 1)[0]
        assert re.fullmatch(rb"[0-9]{1,3}%", first) and cap == int(first[:-1]), answer
    _holds(prop, 15, draws=2000)


# ---------------------------------------------------------------------------
# The documentation's references. Every DEV/P ID pysanoid.py names has its entry in DEVIATIONS.md, and every test,
# check, name and dev/ path that DEVIATIONS.md and the READMEs cite exists, as does every test or check a comment or
# docstring cites, so a rename can't leave a citation behind.
# ---------------------------------------------------------------------------

_DOCS = ("DEVIATIONS.md", "README.md", "HANDBOOK.adoc", "dev/README.md")
_DOC_ID_RE = re.compile(r"\b(DEV-[0-9]{3}[a-z]?|P[0-9]{1,2})\b")
_CITED_CHECK_RE = re.compile(r"\b((?:test|verify)_\w+)\b(?!\.\w)")  # not a file name such as test_pysanoid.py
_CITED_NAME_RE = re.compile(r"(?<![\w$%@{-])(_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+|_[a-z][a-z0-9]+)\b")
_MADE_BY_TOOLS = {".cpan_cache", "work"}  # dev/ directories a fresh checkout doesn't have


def _project_sources() -> list[Path]:
    """The files a cited name can live in: pysanoid, the alert script, their tests, the config files, and dev/,
    the pinned upstream script and the fake zfs included."""
    files = [REPO_ROOT / name for name in ("pysanoid.py", "pysanoid_alert.py", "test_pysanoid.py", "test_pysanoid_alert.py",
                                           "sanoid.defaults.conf", "sanoid.conf")]
    for path in sorted((REPO_ROOT / "dev").rglob("*")):
        parts = set(path.relative_to(REPO_ROOT).parts)
        if path.is_file() and path.suffix in ("", ".py", ".conf") and not parts & (_MADE_BY_TOOLS | {"__pycache__"}):
            files.append(path)
    return files


def _comments_and_docstrings(source: str) -> str:
    docstrings = [ast.get_docstring(node, clean=False) or "" for node in ast.walk(ast.parse(source))
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))]
    comments = [t.string for t in tokenize.generate_tokens(io.StringIO(source).readline) if t.type == tokenize.COMMENT]
    return "\n".join(docstrings + comments)


def _unresolved_references(docs: dict[str, str], tagged: str) -> list[str]:
    """What is cited and doesn't exist: an ID `tagged` names with no entry heading in DEVIATIONS.md; a test or verify_
    check, in a doc or in a source's comments and docstrings, that no source defines; a name in a doc's backticks that
    no source holds (upstream's, a setting's and a file's names count); a dev/ path or a relative link that isn't
    there."""
    defined: set[str] = set()
    words: set[str] = set()
    notes: dict[str, str] = {}
    for path in _project_sources():
        text = path.read_text(errors="replace")
        words.update(re.findall(r"\w+", text))
        if path.suffix == ".py" or text.startswith("#!/usr/bin/env python"):
            defined.update(n.name for n in ast.walk(ast.parse(text)) if isinstance(n, (ast.FunctionDef, ast.ClassDef)))
            notes[str(path.relative_to(REPO_ROOT))] = _comments_and_docstrings(text)
    found: set[str] = set()
    headings = set(re.findall(r"^#{3,4} (DEV-[0-9]{3}[a-z]?|P[0-9]+):", docs["DEVIATIONS.md"], re.MULTILINE))
    found.update(f"pysanoid.py: {tag} has no entry in DEVIATIONS.md" for tag in _DOC_ID_RE.findall(tagged)
                 if tag not in headings)
    for where, text in {**docs, **notes}.items():
        found.update(f"{where}: no function {name}" for name in _CITED_CHECK_RE.findall(text) if name not in defined)
    for where, text in docs.items():
        for span in re.findall(r"`([^`\n]+)`", text):
            found.update(f"{where}: no {name} in any source" for name in _CITED_NAME_RE.findall(span) if name not in words)
        for cited in re.findall(r"(?<![\w/.])(dev/[\w./*-]*\w)", text):
            if cited.split("/")[1] not in _MADE_BY_TOOLS and not (
                    any(REPO_ROOT.glob(cited)) if "*" in cited else (REPO_ROOT / cited).exists()):
                found.add(f"{where}: no {cited}")
        for link in re.findall(r"\]\(([^)#\s]+)|\blink:([^\[{\s]+)\[", text):  # Markdown's, and AsciiDoc's
            link = link[0] or link[1]
            if "://" not in link and not ((REPO_ROOT / where).parent / link).exists():
                found.add(f"{where}: no {link} (a link)")
    return sorted(found)


def _docs() -> dict[str, str]:
    return {name: (REPO_ROOT / name).read_text() for name in _DOCS}


def test_everything_the_documentation_cites_exists():
    assert _unresolved_references(_docs(), (REPO_ROOT / "pysanoid.py").read_text()) == []


def test_the_reference_check_reports_each_kind_of_stale_citation():
    # the check itself: one stale citation of each kind, planted, and each reported. The names are built here, since
    # this file is one of the sources and must not hold them whole.
    test_name, check_name, helper = ("test_a_test" + "_since_renamed", "verify_a_check" + "_since_renamed",
                                     "_a_helper" + "_since_renamed")
    docs = _docs()
    docs["DEVIATIONS.md"] += (f"\n- **Tests:** `{test_name}`, {check_name}\n- **Code:** `{helper}`\n"
                              + "- **Harness:** `dev/diff_gone.py`, `dev/gone_*.py`\n")
    docs["README.md"] += "\nSee [the page](gone.md).\n"
    docs["HANDBOOK.adoc"] += "\nSee link:gone.adoc[the page].\n"
    tagged = (REPO_ROOT / "pysanoid.py").read_text() + "\n# DEV-999: an ID with no entry\n"
    assert _unresolved_references(docs, tagged) == sorted([
        f"DEVIATIONS.md: no {helper} in any source",
        "DEVIATIONS.md: no dev/diff_gone.py",
        "DEVIATIONS.md: no dev/gone_*.py",
        f"DEVIATIONS.md: no function {test_name}",
        f"DEVIATIONS.md: no function {check_name}",
        f"DEVIATIONS.md: no {test_name} in any source",
        "HANDBOOK.adoc: no gone.adoc (a link)",
        "README.md: no gone.md (a link)",
        "pysanoid.py: DEV-999 has no entry in DEVIATIONS.md",
    ])
