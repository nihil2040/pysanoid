"""Tests for pysanoid_alert.py: the failure mail, the freshness check, and how the units call them."""

import configparser
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import pysanoid as ps
import pysanoid_alert as pa

REPO_ROOT = Path(__file__).parent
SCRIPT = REPO_ROOT / "pysanoid_alert.py"
NOW = 1_800_000_000
INVOCATION = "0123456789abcdef0123456789abcdef"


def write_confdir(tmp_path: Path, sanoid_conf: str) -> Path:
    """sanoid.conf beside the shipped sanoid.defaults.conf, so these tests read the file a deployed pysanoid reads."""
    d = tmp_path / "conf"
    d.mkdir()
    shutil.copy(REPO_ROOT / "sanoid.defaults.conf", d / "sanoid.defaults.conf")
    (d / "sanoid.conf").write_text(sanoid_conf)
    return d


def done(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def listing(*snapshots: tuple[str, int]) -> str:
    """`zfs list -Hp -o name,creation` output."""
    return "".join(f"{name}\t{created}\n" for name, created in snapshots)


Answer = subprocess.CompletedProcess[str] | Exception | Callable[[], subprocess.CompletedProcess[str]]


def program(cmd: list[str]) -> str:
    """The program a command runs: midclt, when it is the one the launcher runs (send_mail), else its own name."""
    return "midclt" if cmd[1:4] == ["-I", "-c", pa._MIDCLT_FROM_STDIN] else Path(cmd[0]).name


class Commands:
    """Stands in for pa._run: records each command and what it read on stdin, and answers by the program's name (zfs,
    midclt, journalctl) with its result, the error to raise, or a function to call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, **answers: Answer):
        self.calls: list[list[str]] = []
        self.stdins: list[str | None] = []
        self.answers = answers
        monkeypatch.setattr(pa, "_run", self)

    def __call__(self, cmd: list[str], what: str, timeout: int, stdin: str | None = None,
                 ) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        self.stdins.append(stdin)
        answer = self.answers.get(program(cmd), done())
        if isinstance(answer, Exception):
            raise answer
        return answer() if callable(answer) else answer

    def named(self, name: str) -> list[list[str]]:
        return [cmd for cmd in self.calls if program(cmd) == name]

    def mail(self) -> dict[str, str]:
        (stdin,) = [stdin for cmd, stdin in zip(self.calls, self.stdins, strict=True) if program(cmd) == "midclt"]
        assert stdin is not None
        return json.loads(stdin)


def fake_children(monkeypatch: pytest.MonkeyPatch, datasets: list[str]) -> None:
    """pysanoid's own child lookup (`zfs list -Hr <fs>`) over a fixed tree."""
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert cmd[:2] == [ps.ZFS, "list"]
        out = [d for d in datasets if d == cmd[-1] or d.startswith(cmd[-1] + "/")]
        return subprocess.CompletedProcess(cmd, 0, stdout="".join(f"{d}\n" for d in out).encode(), stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)


@pytest.fixture(autouse=True)
def as_a_fresh_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No failure described by systemd in the environment, and afterwards no deadline left set by main()."""
    for name in ("MONITOR_SERVICE_RESULT", "MONITOR_EXIT_CODE", "MONITOR_EXIT_STATUS", "MONITOR_INVOCATION_ID"):
        monkeypatch.delenv(name, raising=False)
    handler = signal.getsignal(signal.SIGALRM)
    yield
    _ = signal.alarm(0)
    _ = signal.signal(signal.SIGALRM, handler)


# ---------------------------------------------------------------------------
# running commands, and the mail
# ---------------------------------------------------------------------------

def test_run_reports_a_command_that_cannot_start():
    with pytest.raises(pa.AlertError, match="^could not run the thing: .*No such file"):
        pa._run(["/nonexistent/program"], "the thing", 5)


def test_run_reports_a_command_that_does_not_finish_without_quoting_its_arguments():
    with pytest.raises(pa.AlertError) as raised:
        pa._run([sys.executable, "-c", "import time; time.sleep(30)", "THE WHOLE MAIL"], "the thing", 1)
    assert str(raised.value) == "the thing did not finish in 1 seconds"


def test_run_replaces_bytes_that_are_not_utf8():
    proc = pa._run([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'caf\\xe9 ok')"], "the thing", 60)
    assert proc.stdout == "caf\N{REPLACEMENT CHARACTER} ok"


def test_the_payload_is_the_message_mail_send_takes():
    assert json.loads(pa._payload(("the subject", ["head", ""], ["one", "two"]))) == {
        "subject": "the subject", "text": "head\n\none\ntwo"}


def test_a_mail_too_long_loses_the_top_of_its_log():
    log = [f"line {n} " + "x" * 500 for n in range(1000)]
    payload = pa._payload(("subject", ["the head stays"], log))
    assert len(payload.encode()) <= pa._MAX_PAYLOAD
    text = json.loads(payload)["text"].split("\n")
    note = re.fullmatch(r"\[(\d+) earlier lines left out\]", text[1])
    assert text[0] == "the head stays"
    assert note is not None
    assert text[2:] == log[int(note.group(1)):]
    assert len(text[2:]) > 100  # halved until it fits, not cut to nothing
    assert text[-1] == log[-1]


def test_one_enormous_line_is_cut_short():
    payload = pa._payload(("subject", [], ["\0" * 1_000_000]))  # six bytes of JSON each
    assert len(payload) <= pa._MAX_PAYLOAD
    assert json.loads(payload)["text"] == "\0" * pa._MAX_LINE


def test_send_mail_has_truenas_send_it_and_waits(monkeypatch):
    commands = Commands(monkeypatch)
    pa.send_mail(("pysanoid.service failed", ["head"], ["tail"]))
    (cmd,) = commands.calls
    assert cmd == [sys.executable, "-I", "-c", pa._MIDCLT_FROM_STDIN,
                   pa.MIDCLT, "call", "-j", "-jp", "description", "mail.send"]
    assert commands.mail() == {"subject": "pysanoid.service failed", "text": "head\ntail"}


def fake_midclt(tmp_path: Path, body: str) -> Path:
    """A midclt that records the arguments it was given and the command line its process was started with."""
    path = tmp_path / "midclt"
    path.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                    "with open(os.environ['MIDCLT_SAW'], 'w') as saw:\n"
                    "    json.dump({'argv': sys.argv, 'cmdline': sys.orig_argv}, saw)\n" + body)
    path.chmod(0o755)
    return path


def test_the_mail_reaches_midclt_as_its_last_argument_and_is_in_no_command_line(tmp_path, monkeypatch):
    # a process's command line is there for every local user to read (ps, /proc/PID/cmdline)
    monkeypatch.setattr(pa, "MIDCLT", str(fake_midclt(tmp_path, "")))
    monkeypatch.setenv("MIDCLT_SAW", str(tmp_path / "saw.json"))
    mail = ("pysanoid.service failed", ["head"], ["a line only the mail holds"])
    pa.send_mail(mail)
    saw = json.loads((tmp_path / "saw.json").read_text())
    assert saw["argv"] == [pa.MIDCLT, "call", "-j", "-jp", "description", "mail.send", pa._payload(mail)]
    assert saw["cmdline"][-6:] == [pa.MIDCLT, "call", "-j", "-jp", "description", "mail.send"]
    assert not any("only the mail holds" in word for word in saw["cmdline"])


def test_what_midclt_says_and_its_exit_status_come_back_as_midclt_gives_them(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "MIDCLT", str(fake_midclt(
        tmp_path, "print('[EFAULT] Failed to send email: timed out', file=sys.stderr)\n"
                  "print('Traceback (most recent call last):', file=sys.stderr)\nsys.exit(1)\n")))
    monkeypatch.setenv("MIDCLT_SAW", str(tmp_path / "saw.json"))
    with pytest.raises(pa.AlertError, match=r"^TrueNAS did not send the mail: \[EFAULT\] Failed to send email: timed out$"):
        pa.send_mail(("subject", [], []))


def test_a_midclt_not_on_path_is_an_error_saying_so_and_one_in_the_working_directory_is_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "MIDCLT", "midclt")
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("MIDCLT_SAW", str(tmp_path / "saw.json"))
    monkeypatch.chdir(tmp_path)
    _ = fake_midclt(tmp_path, "")
    with pytest.raises(pa.AlertError, match="^TrueNAS did not send the mail: midclt was not found on PATH$"):
        pa.send_mail(("subject", [], []))
    assert not (tmp_path / "saw.json").exists()


def test_the_launcher_imports_nothing_from_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, "MIDCLT", str(fake_midclt(tmp_path, "")))
    monkeypatch.setenv("MIDCLT_SAW", str(tmp_path / "saw.json"))
    planted = tmp_path / "cwd"
    planted.mkdir()
    for module in ("runpy", "shutil"):
        (planted / f"{module}.py").write_text(f"open({str(tmp_path / 'planted')!r}, 'w').close()\n")
    monkeypatch.chdir(planted)
    pa.send_mail(("subject", [], []))
    assert (tmp_path / "saw.json").exists()
    assert not (tmp_path / "planted").exists()


def test_a_midclt_that_crashes_is_reported_by_the_last_line_of_its_traceback(tmp_path, monkeypatch):
    shell = tmp_path / "midclt"
    shell.write_text("#!/bin/sh\nexec true\n")  # not a Python script
    shell.chmod(0o755)
    monkeypatch.setattr(pa, "MIDCLT", str(shell))
    with pytest.raises(pa.AlertError, match="^TrueNAS did not send the mail: SyntaxError: "):
        pa.send_mail(("subject", [], []))


def test_a_mail_truenas_does_not_send_is_an_error_in_its_words(monkeypatch):
    refusal = "\n[EFAULT] None of the local administrators has an e-mail address configured\nTraceback (most recent ...\n"
    Commands(monkeypatch, midclt=done(stderr=refusal, returncode=1))
    with pytest.raises(pa.AlertError) as raised:
        pa.send_mail(("subject", [], []))
    assert str(raised.value) == ("TrueNAS did not send the mail: [EFAULT] None of the local administrators has an "
                                 "e-mail address configured")


def test_a_midclt_that_fails_silently_is_reported_by_its_exit_status(monkeypatch):
    Commands(monkeypatch, midclt=done(returncode=3))
    with pytest.raises(pa.AlertError, match="^TrueNAS did not send the mail: midclt exit status 3$"):
        pa.send_mail(("subject", [], []))


# ---------------------------------------------------------------------------
# failure
# ---------------------------------------------------------------------------

def test_the_log_is_the_failed_runs_own_when_systemd_names_the_run(monkeypatch):
    commands = Commands(monkeypatch, journalctl=done("line 1\nline 2\n"))
    assert pa.unit_log("pysanoid.service", INVOCATION) == ["line 1", "line 2"]
    assert commands.calls == [
        [pa.JOURNALCTL, "--sync"],
        [pa.JOURNALCTL, "--no-pager", "-o", "short-iso", "-n", "200",
         f"_SYSTEMD_INVOCATION_ID={INVOCATION}", "+", f"INVOCATION_ID={INVOCATION}"],
    ]


@pytest.mark.parametrize("invocation", [None, "", "not-an-id", INVOCATION + "0", INVOCATION.upper(), f"{INVOCATION}\n"])
def test_without_a_usable_invocation_id_the_log_is_the_units_latest(monkeypatch, invocation):
    commands = Commands(monkeypatch, journalctl=done("line\n"))
    assert pa.unit_log("pysanoid-prune.service", invocation) == ["line"]
    assert commands.calls[-1] == [pa.JOURNALCTL, "--no-pager", "-o", "short-iso", "-n", "50",
                                  "--unit=pysanoid-prune.service"]


def test_a_journal_that_cannot_be_read_is_a_line_of_the_mail(monkeypatch):
    Commands(monkeypatch, journalctl=pa.AlertError("could not run journalctl: gone"))
    assert pa.unit_log("pysanoid.service", None) == ["(could not run journalctl: gone)"]


def test_a_journalctl_that_fails_is_a_line_of_the_mail(monkeypatch):
    Commands(monkeypatch, journalctl=done(stderr="No journal files were found.\n", returncode=1))
    assert pa.unit_log("pysanoid.service", None) == ["(journalctl exit status 1: No journal files were found.)"]


def test_an_empty_log_says_so(monkeypatch):
    Commands(monkeypatch)
    assert pa.unit_log("pysanoid.service", None) == ["(the journal has no lines for it)"]


@pytest.mark.parametrize(("env", "how"), [
    ({"MONITOR_SERVICE_RESULT": "exit-code", "MONITOR_EXIT_CODE": "exited", "MONITOR_EXIT_STATUS": "1"},
     " (exit-code: exited, status 1)"),
    ({"MONITOR_SERVICE_RESULT": "timeout", "MONITOR_EXIT_CODE": "killed", "MONITOR_EXIT_STATUS": "TERM"},
     " (timeout: killed, status TERM)"),
    ({"MONITOR_SERVICE_RESULT": "resources"}, " (resources)"),
    ({}, ""),
])
def test_the_failure_mail_says_how_the_unit_failed(monkeypatch, env, how):
    commands = Commands(monkeypatch, journalctl=done("ERROR: 1 problem(s) this run\n"))
    mail = pa.failure_mail("pysanoid.service", {**env, "MONITOR_INVOCATION_ID": INVOCATION})
    assert mail == ("pysanoid.service failed", [f"pysanoid.service failed{how}. Its log:", ""],
                    ["ERROR: 1 problem(s) this run"])
    assert f"INVOCATION_ID={INVOCATION}" in commands.calls[-1]


def test_failure_mails_the_log_and_exits_zero(monkeypatch, capsys):
    commands = Commands(monkeypatch, journalctl=done("the log\n"))
    assert pa.main(["failure", "pysanoid.service"]) == 0
    assert commands.mail() == {"subject": "pysanoid.service failed",
                               "text": "pysanoid.service failed. Its log:\n\nthe log"}
    assert capsys.readouterr().out == "mailed: pysanoid.service failed\n"


def test_failure_reads_how_the_unit_failed_from_its_environment(monkeypatch):
    monkeypatch.setenv("MONITOR_SERVICE_RESULT", "exit-code")
    monkeypatch.setenv("MONITOR_INVOCATION_ID", INVOCATION)
    commands = Commands(monkeypatch, journalctl=done("the log\n"))
    assert pa.main(["failure", "pysanoid.service"]) == 0
    assert commands.mail()["text"].startswith("pysanoid.service failed (exit-code). Its log:\n")
    assert f"_SYSTEMD_INVOCATION_ID={INVOCATION}" in commands.named("journalctl")[-1]


def test_a_failure_that_cannot_be_mailed_exits_nonzero_and_leaves_the_mail_on_stderr(monkeypatch, capsys):
    Commands(monkeypatch, journalctl=done("the log\n"),
             midclt=done(stderr="Failed to run middleware call. Daemon not running?\n", returncode=1))
    assert pa.main(["failure", "pysanoid.service"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ("pysanoid_alert: TrueNAS did not send the mail: Failed to run middleware call. Daemon not "
                            "running?\n\nSubject: pysanoid.service failed\n\npysanoid.service failed. Its log:\n\n"
                            "the log\n")


def test_no_mail_prints_the_mail_and_sends_nothing(monkeypatch, capsys):
    commands = Commands(monkeypatch, journalctl=done("the log\n"))
    assert pa.main(["failure", "pysanoid.service", "--no-mail"]) == 0
    assert commands.named("midclt") == []
    assert capsys.readouterr().out == ("Subject: pysanoid.service failed\n\npysanoid.service failed. Its log:\n\n"
                                       "the log\n")


def test_failure_still_mails_when_pysanoid_cannot_be_imported(monkeypatch):
    monkeypatch.setitem(sys.modules, "pysanoid", None)  # `import pysanoid` now raises
    commands = Commands(monkeypatch, journalctl=done("SyntaxError: invalid syntax\n"))
    assert pa.main(["failure", "pysanoid.service"]) == 0
    assert commands.mail()["text"].endswith("SyntaxError: invalid syntax")


# ---------------------------------------------------------------------------
# freshness: what should exist
# ---------------------------------------------------------------------------

PRODUCTION_LIKE = """
[tank/software]
    use_template = software
    recursive = yes

[tank/home]
    use_template = home

[tank/media]
    use_template = media

[tank/paused]
    use_template = software
    autosnap = no

[tank/children-only]
    use_template = software
    process_children_only = yes

[template_software]
    hourly = 0
    daily = 30
    monthly = 0

[template_home]
    hourly = 0
    daily = 7
    weekly = 5
    monthly = 38

[template_media]
    hourly = 12
    daily = 7
    monthly = 0
"""


def test_every_type_pysanoid_takes_has_a_limit():
    assert tuple(pa.LIMITS) == ps.PERIOD_TYPES


def test_the_expected_types_are_the_ones_pysanoid_takes(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, PRODUCTION_LIKE)
    fake_children(monkeypatch, ["tank/software", "tank/software/a", "tank/software/a/b"])
    expected, skipped, said = pa.expected_types(str(confdir))
    assert expected == {
        "tank/software": ["daily"],
        "tank/software/a": ["daily"],
        "tank/software/a/b": ["daily"],
        "tank/home": ["monthly", "weekly", "daily"],
        "tank/media": ["daily", "hourly"],
    }
    assert skipped == {}  # the template_ sections are templates
    assert said == []


def test_the_expected_types_are_what_pysanoid_plans_for_a_pool_with_no_snapshots(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, PRODUCTION_LIKE)
    fake_children(monkeypatch, ["tank/software", "tank/software/a"])
    config = ps.load_config(confdir, ps.ZfsClient(tmp_path, cache_ttl=0, force_update=True))
    plan = ps.plan_snapshots(config, {})
    expected, _, _ = pa.expected_types(str(confdir))
    assert expected == {path: pending.types for path, pending in plan.items()}
    assert expected


def test_under_zfs_recursion_only_the_parent_is_expected(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, "[tank]\n recursive = zfs\n hourly = 0\n daily = 3\n monthly = 0\n")
    fake_children(monkeypatch, ["tank", "tank/a", "tank/b"])
    expected, _, _ = pa.expected_types(str(confdir))
    assert expected == {"tank": ["daily"]}


# take and prune skip every section whose name starts with "template", as upstream's do; load_config reads only the
# template_ ones as templates, so a pool named "templates" loads and is never snapshotted
TEMPLATE_NAMED = """
[tank]
    hourly = 0
    daily = 7
    monthly = 0

[templates/vm]
    hourly = 12
    daily = 7
    monthly = 0
"""


def test_a_section_named_like_a_template_is_not_expected_but_reported_as_skipped(tmp_path):
    confdir = write_confdir(tmp_path, TEMPLATE_NAMED)
    expected, skipped, said = pa.expected_types(str(confdir))
    assert expected == {"tank": ["daily"]}
    assert skipped == {"templates/vm": ["daily", "hourly"]}
    assert said == []


def test_the_skipped_sections_are_the_ones_pysanoid_skips(tmp_path):
    confdir = write_confdir(tmp_path, TEMPLATE_NAMED + "[template]\n hourly = 0\n daily = 3\n monthly = 0\n"
                                                       "[tank/templates]\n hourly = 0\n daily = 3\n monthly = 0\n")
    config = ps.load_config(confdir, ps.ZfsClient(tmp_path, cache_ttl=0, force_update=True))
    skipped_by_take = {cfg["path"] for name, cfg in config.items()
                       if name.startswith("template") and cfg.get("autosnap")}
    _, skipped, _ = pa.expected_types(str(confdir))
    assert set(skipped) == skipped_by_take == {"templates/vm", "template"}


def test_a_recursion_under_a_section_named_like_a_template_is_skipped_whole(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, "[templates]\n recursive = yes\n hourly = 0\n daily = 3\n monthly = 0\n")
    fake_children(monkeypatch, ["templates", "templates/a", "templates/a/b"])
    expected, skipped, _ = pa.expected_types(str(confdir))
    assert expected == {}
    assert skipped == {"templates": ["daily"], "templates/a": ["daily"], "templates/a/b": ["daily"]}


def test_a_type_another_section_takes_of_the_same_dataset_is_not_skipped(tmp_path):
    confdir = write_confdir(tmp_path, "[vm]\n path = templates/vm\n hourly = 0\n daily = 7\n monthly = 0\n"
                                      "[templates/vm]\n hourly = 0\n daily = 7\n weekly = 4\n monthly = 0\n")
    expected, skipped, _ = pa.expected_types(str(confdir))
    assert expected == {"templates/vm": ["daily"]}
    assert skipped == {"templates/vm": ["weekly"]}


def test_what_pysanoid_says_reading_the_config_is_kept_for_the_mail_not_printed(tmp_path, capsys):
    confdir = write_confdir(tmp_path, "[tank]\n hourly = 4\n hourly = 5\n daily = many\n monthly = 0\n")
    expected, _, said = pa.expected_types(str(confdir))
    assert expected == {"tank": ["hourly"]}
    assert len(said) == 2
    assert said[0].startswith("duplicate key 'hourly' in section 'tank'")
    assert said[1].startswith("ERROR: not taking daily snapshots of 'tank' this run: ")
    captured = capsys.readouterr()
    assert (captured.out, captured.err) == ("", "")


def test_reading_the_config_writes_no_cache(tmp_path, monkeypatch):
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("the freshness check wrote one of pysanoid's caches")

    monkeypatch.setattr(ps, "_replace_cache", refuse)
    monkeypatch.setattr(ps, "_write_text", refuse)
    confdir = write_confdir(tmp_path, "[tank]\n recursive = yes\n hourly = 0\n daily = 3\n monthly = 0\n")
    fake_children(monkeypatch, ["tank", "tank/a"])
    expected, _, _ = pa.expected_types(str(confdir))
    assert sorted(expected) == ["tank", "tank/a"]


# ---------------------------------------------------------------------------
# freshness: what does exist
# ---------------------------------------------------------------------------

def test_newest_snapshots_asks_zfs_for_each_datasets_own_snapshots(monkeypatch):
    commands = Commands(monkeypatch, zfs=done(listing(
        ("tank/a@autosnap_2026-10-04_00:00:01_daily", 100),
        ("tank/a@autosnap_2026-10-05_00:00:02_daily", 200),
        ("tank/a@autosnap_2026-10-03_00:00:02_daily", 50),
        ("tank/a@autosnap_2026-10-05_09:00:01_hourly", 300),
        ("tank/a@autosnap_2026-10-25_01:00:00dst_hourly", 400),
        ("tank/a@by-hand_daily", 900),
        ("tank/a@autosnap_2026-10-05_09:00:01_fortnightly", 900),
        ("tank/a@autosnap_daily_kept", 900),
        ("tank/a@autosnap_2026-10-05_00:00:02_daily_kept", 900),
        ("tank/b@autosnap_2026-10-01_00:00:00_monthly", 70),
    )))
    newest, complaints = pa.newest_snapshots(["tank/a", "tank/b"])
    assert commands.calls == [[pa.ZFS, "list", "-H", "-p", "-d", "1", "-t", "snapshot", "-o", "name,creation",
                               "tank/a", "tank/b"]]
    assert newest == {("tank/a", "daily"): 200, ("tank/a", "hourly"): 400, ("tank/b", "monthly"): 70}
    assert complaints == []


def test_what_zfs_complains_of_is_reported_and_what_it_listed_is_still_used(monkeypatch):
    Commands(monkeypatch, zfs=done(listing(("tank/a@autosnap_x_daily", 7)),
                                   "cannot open 'tank/gone': dataset does not exist\n", 1))
    assert pa.newest_snapshots(["tank/a", "tank/gone"]) == (
        {("tank/a", "daily"): 7}, ["cannot open 'tank/gone': dataset does not exist"])


def test_a_zfs_that_fails_silently_is_a_complaint(monkeypatch):
    Commands(monkeypatch, zfs=done(returncode=2))
    assert pa.newest_snapshots(["tank"]) == ({}, ["zfs list exit status 2"])


@pytest.mark.parametrize("line", [
    "", "no tab at all", "tank@autosnap_x_daily\t", "tank@autosnap_x_daily\tsoon", "tank@autosnap_x_daily\t-5",
    "tank@autosnap_x_daily\t\N{ARABIC-INDIC DIGIT FIVE}", "autosnap_x_daily\t5", "tank@autosnap_x_daily 5",
])
def test_a_line_that_is_not_a_snapshot_and_its_time_is_ignored(monkeypatch, line):
    Commands(monkeypatch, zfs=done(f"{line}\ntank@autosnap_y_hourly\t9\n"))
    assert pa.newest_snapshots(["tank"]) == ({("tank", "hourly"): 9}, [])


# ---------------------------------------------------------------------------
# freshness: what is stale
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("snap_type", "limit"), [
    ("yearly", 367 * 86400), ("monthly", 32 * 86400), ("weekly", 8 * 86400), ("daily", 28 * 3600),
    ("hourly", 90 * 60), ("frequently", 90 * 60),
])
def test_a_type_is_stale_once_its_newest_snapshot_is_older_than_its_limit(snap_type, limit):
    expected = {"tank": [snap_type]}
    assert pa.stale_types(expected, {("tank", snap_type): NOW - limit}, NOW) == {}
    assert list(pa.stale_types(expected, {("tank", snap_type): NOW - limit - 1}, NOW)) == ["tank"]


def test_stale_lines_name_the_dataset_the_type_and_both_ages():
    expected = {"tank/b": ["daily", "hourly"], "tank/a": ["monthly"], "tank/c": ["daily"]}
    newest = {
        ("tank/a", "monthly"): NOW - (40 * 86400 + 3 * 3600 + 59 * 60),
        ("tank/b", "daily"): NOW - 3600,
        ("tank/b", "hourly"): NOW - (5 * 3600 + 12 * 60 + 30),
        ("tank/c", "daily"): NOW - 3600,
    }
    stale = pa.stale_types(expected, newest, NOW)
    assert stale == {
        "tank/a": ["tank/a: newest monthly snapshot is 40d 3h old (limit 32d)"],
        "tank/b": ["tank/b: newest hourly snapshot is 5h 12m old (limit 1h 30m)"],
    }
    assert list(stale) == ["tank/a", "tank/b"]


def test_a_type_with_no_snapshot_is_stale():
    assert pa.stale_types({"tank": ["daily", "hourly"]}, {("tank", "hourly"): NOW}, NOW) == {
        "tank": ["tank: no daily snapshot"]}


@pytest.mark.parametrize(("ahead", "line"), [
    (0, None), (300, None),
    (301, "tank: newest hourly snapshot is dated 5m in the future: pysanoid sets it aside, neither counting nor destroying it, until the clock passes it"),
    (86400 + 7200, "tank: newest hourly snapshot is dated 1d 2h in the future: pysanoid sets it aside, neither counting nor destroying it, until the clock passes it"),
], ids=["made this second", "5 minutes ahead", "301 s ahead", "a day ahead"])
def test_a_type_whose_newest_snapshot_is_dated_in_the_future_is_reported(ahead, line):
    # pysanoid sets such a snapshot aside until the clock passes it, and says nothing (DEV-051): the check is what
    # tells that the clock went wrong, and that the snapshot stays on disk until then.
    stale = pa.stale_types({"tank": ["hourly"]}, {("tank", "hourly"): NOW + ahead}, NOW)
    assert stale == ({} if line is None else {"tank": [line]})


@pytest.mark.parametrize(("seconds", "text"), [
    (0, "0m"), (59, "0m"), (60, "1m"), (3600, "1h"), (5400, "1h 30m"), (86400, "1d"), (28 * 3600, "1d 4h"),
    (40 * 86400 + 3 * 3600 + 59 * 60, "40d 3h"), (8 * 86400 + 59 * 60, "8d"),
])
def test_an_age_reads_in_its_two_largest_units(seconds, text):
    assert pa._age(seconds) == text


# ---------------------------------------------------------------------------
# freshness: the command
# ---------------------------------------------------------------------------

TWO_TYPES = "[tank]\n hourly = 12\n daily = 7\n monthly = 0\n"


def test_fresh_snapshots_send_nothing_and_exit_zero(tmp_path, monkeypatch, capsys):
    confdir = write_confdir(tmp_path, TWO_TYPES)
    now = int(time.time())
    commands = Commands(monkeypatch, zfs=done(listing(("tank@autosnap_a_daily", now - 3600),
                                                      ("tank@autosnap_a_hourly", now - 600))))
    assert pa.main(["freshness", f"--configdir={confdir}"]) == 0
    assert commands.named("midclt") == []
    assert capsys.readouterr().out == "OK: 2 snapshot types on 1 dataset, none stale\n"


def test_stale_snapshots_are_mailed_and_exit_one(tmp_path, monkeypatch, capsys):
    confdir = write_confdir(tmp_path, TWO_TYPES + "[tank/fresh]\n hourly = 0\n daily = 7\n monthly = 0\n")
    now = int(time.time())
    commands = Commands(monkeypatch, zfs=done(listing(("tank@autosnap_a_daily", now - 2 * 86400),
                                                      ("tank/fresh@autosnap_a_daily", now - 60))))
    assert pa.main(["freshness", f"--configdir={confdir}"]) == 1
    assert commands.named("zfs")[0][-2:] == ["tank", "tank/fresh"]
    mail = commands.mail()
    assert mail["subject"] == "pysanoid: stale snapshots on 1 dataset"
    assert mail["text"].split("\n") == [
        "pysanoid is not taking these snapshots on schedule:",
        "",
        "tank: newest daily snapshot is 2d old (limit 1d 4h)",
        "tank: no hourly snapshot",
        "",
        "Start with: systemctl status pysanoid.timer pysanoid.service",
    ]
    assert capsys.readouterr().out == "mailed: pysanoid: stale snapshots on 1 dataset\n"


def test_a_snapshot_dated_in_the_future_is_mailed(tmp_path, monkeypatch):
    # the clock ran a year ahead for one take: the daily's newest is dated then, and the hourly is as it should be
    confdir = write_confdir(tmp_path, TWO_TYPES)
    now = int(time.time())
    commands = Commands(monkeypatch, zfs=done(listing(("tank@autosnap_a_daily", now - 3600),
                                                      ("tank@autosnap_b_daily", now + 365 * 86400 + 1800),
                                                      ("tank@autosnap_a_hourly", now - 600))))
    assert pa.main(["freshness", f"--configdir={confdir}"]) == 1
    mail = commands.mail()
    assert mail["subject"] == "pysanoid: stale snapshots on 1 dataset"
    assert mail["text"].split("\n")[2:4] == [
        "tank: newest daily snapshot is dated 365d in the future: pysanoid sets it aside, neither counting nor destroying it, until the clock passes it", ""]


def test_the_stale_mail_carries_what_zfs_and_pysanoid_said(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, "[tank/a]\n hourly = 0\n daily = 7\n daily = 8\n monthly = 0\n"
                                      "[tank/gone]\n hourly = 0\n daily = 7\n monthly = 0\n")
    commands = Commands(monkeypatch, zfs=done("", "cannot open 'tank/gone': dataset does not exist\n", 1))
    assert pa.main(["freshness", f"--configdir={confdir}"]) == 1
    mail = commands.mail()
    assert mail["subject"] == "pysanoid: stale snapshots on 2 datasets"
    assert mail["text"].split("\n")[2:] == [
        "tank/a: no daily snapshot",
        "tank/gone: no daily snapshot",
        "",
        "zfs said:",
        "cannot open 'tank/gone': dataset does not exist",
        "",
        "pysanoid said, reading sanoid.conf:",
        "duplicate key 'daily' in section 'tank/a', using the value from the first occurence and ignoring the others.",
        "",
        "Start with: systemctl status pysanoid.timer pysanoid.service",
    ]


def test_a_config_that_cannot_be_read_is_mailed(tmp_path, monkeypatch):
    commands = Commands(monkeypatch)
    assert pa.main(["freshness", f"--configdir={tmp_path / 'missing'}"]) == 1
    mail = commands.mail()
    assert mail["subject"] == "pysanoid: the snapshot freshness check could not run"
    assert mail["text"].startswith("The check could not compare the snapshots with sanoid.conf:\n\n"
                                   "SanoidError: cannot load ")
    assert commands.named("zfs") == []


def test_a_zfs_that_cannot_run_is_mailed(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, TWO_TYPES)
    commands = Commands(monkeypatch, zfs=pa.AlertError("could not run zfs list: [Errno 2] No such file or directory"))
    assert pa.main(["freshness", f"--configdir={confdir}"]) == 1
    assert commands.mail()["text"].endswith("AlertError: could not run zfs list: [Errno 2] No such file or directory")


def test_a_pysanoid_that_cannot_be_imported_is_mailed(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, TWO_TYPES)
    monkeypatch.setitem(sys.modules, "pysanoid", None)
    commands = Commands(monkeypatch)
    assert pa.main(["freshness", f"--configdir={confdir}"]) == 1
    mail = commands.mail()
    assert mail["subject"] == "pysanoid: the snapshot freshness check could not run"
    assert "ImportError" in mail["text"] or "ModuleNotFoundError" in mail["text"]


def test_a_stale_report_that_cannot_be_mailed_goes_to_stderr(tmp_path, monkeypatch, capsys):
    confdir = write_confdir(tmp_path, TWO_TYPES)
    Commands(monkeypatch, midclt=done(stderr="[EFAULT] Failed to send email: timed out\n", returncode=1))
    assert pa.main(["freshness", f"--configdir={confdir}"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("pysanoid_alert: TrueNAS did not send the mail: [EFAULT] Failed to send email: "
                                   "timed out\n\nSubject: pysanoid: stale snapshots on 1 dataset\n")
    assert "tank: no daily snapshot\ntank: no hourly snapshot\n" in captured.err


_SKIPPED_LINE = ("templates/vm: pysanoid never takes its daily, hourly snapshots: take and prune skip a section whose "
                 'name starts with "template", as upstream sanoid does')


def test_a_section_pysanoid_skips_is_mailed_when_everything_else_is_fresh(tmp_path, monkeypatch, capsys):
    confdir = write_confdir(tmp_path, TEMPLATE_NAMED)
    commands = Commands(monkeypatch, zfs=done(listing(("tank@autosnap_a_daily", int(time.time()) - 3600))))
    assert pa.main(["freshness", f"--configdir={confdir}"]) == 1
    assert commands.named("zfs")[0][-1:] == ["tank"]
    mail = commands.mail()
    assert mail["subject"] == "pysanoid: stale snapshots on 1 dataset"
    assert mail["text"].split("\n") == [
        "pysanoid is not taking these snapshots on schedule:",
        "",
        _SKIPPED_LINE,
        "",
        "Start with: systemctl status pysanoid.timer pysanoid.service",
    ]
    assert capsys.readouterr().out == "mailed: pysanoid: stale snapshots on 1 dataset\n"


def test_a_skipped_section_is_listed_in_order_with_the_stale_ones(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, TEMPLATE_NAMED + "[zeta]\n hourly = 0\n daily = 7\n monthly = 0\n")
    commands = Commands(monkeypatch)
    assert pa.main(["freshness", f"--configdir={confdir}"]) == 1
    mail = commands.mail()
    assert mail["subject"] == "pysanoid: stale snapshots on 3 datasets"
    assert mail["text"].split("\n")[2:5] == ["tank: no daily snapshot", _SKIPPED_LINE, "zeta: no daily snapshot"]


def test_a_config_whose_only_dataset_pysanoid_skips_is_mailed_without_asking_zfs(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, "[templates/vm]\n hourly = 12\n daily = 7\n monthly = 0\n")
    commands = Commands(monkeypatch)
    assert pa.main(["freshness", f"--configdir={confdir}"]) == 1
    assert commands.named("zfs") == []
    assert commands.mail()["text"].split("\n")[2] == _SKIPPED_LINE


def test_a_config_that_takes_no_snapshots_is_not_stale_and_lists_nothing(tmp_path, monkeypatch, capsys):
    confdir = write_confdir(tmp_path, "[tank]\n autosnap = no\n")
    commands = Commands(monkeypatch)
    assert pa.main(["freshness", f"--configdir={confdir}"]) == 0
    assert commands.calls == []  # with no dataset named, zfs would list every snapshot on the system
    assert capsys.readouterr().out == "OK: sanoid.conf has no dataset pysanoid takes snapshots of\n"


def test_freshness_no_mail_prints_the_report_and_still_exits_one(tmp_path, monkeypatch, capsys):
    confdir = write_confdir(tmp_path, TWO_TYPES)
    commands = Commands(monkeypatch)
    assert pa.main(["freshness", "--no-mail", f"--configdir={confdir}"]) == 1
    assert commands.named("midclt") == []
    assert capsys.readouterr().out.startswith("Subject: pysanoid: stale snapshots on 1 dataset\n\npysanoid is not "
                                              "taking these snapshots on schedule:\n\ntank: no daily snapshot\n")


_KEPT_BYTE = b"caf\xe9".decode("utf-8", "surrogateescape")  # as pysanoid keeps a byte that isn't UTF-8


def test_a_mail_holding_a_byte_that_is_not_utf8_is_printed_with_it_replaced(capsys):
    # pysanoid keeps such a byte as it read it, in a section's name or in what zfs said. A terminal can't take it:
    # the print raised, and the check ended in a traceback.
    mail = (f"stale: tank/{_KEPT_BYTE}", [f"tank/{_KEPT_BYTE}: no daily snapshot"], [f"zfs said: {_KEPT_BYTE}"])
    assert pa.deliver(mail, no_mail=True)
    assert capsys.readouterr().out == ("Subject: stale: tank/caf�\n\ntank/caf�: no daily snapshot\n"
                                       "zfs said: caf�\n")


def test_a_mail_holding_a_byte_that_is_not_utf8_is_sent_with_it_replaced(monkeypatch):
    # in the JSON midclt takes it went out as half a character, which no mail can carry
    commands = Commands(monkeypatch)
    assert pa.deliver((f"stale: tank/{_KEPT_BYTE}", [], [f"zfs said: {_KEPT_BYTE}"]), no_mail=False)
    assert commands.mail() == {"subject": "stale: tank/caf�", "text": "zfs said: caf�"}


def test_what_zfs_tells_pysanoid_in_bytes_that_are_not_utf8_is_in_the_stale_mail(tmp_path, monkeypatch):
    # pysanoid writes a child lookup's complaint out as zfs wrote it; the mail has it with the byte replaced
    confdir = write_confdir(tmp_path, "[tank]\n recursive = yes\n hourly = 0\n daily = 7\n monthly = 0\n")

    def lookup(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 0, stdout=b"tank\n", stderr=b"note: caf\xe9\n")

    monkeypatch.setattr(subprocess, "run", lookup)
    commands = Commands(monkeypatch)
    assert pa.main(["freshness", f"--configdir={confdir}"]) == 1
    assert commands.mail()["text"].split("\n")[4:6] == ["pysanoid said, reading sanoid.conf:", "note: caf�"]


def test_a_check_that_does_not_finish_is_mailed_and_the_process_exits_at_once(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, TWO_TYPES)

    def hang() -> subprocess.CompletedProcess[str]:
        time.sleep(60)
        raise AssertionError("the deadline did not interrupt the wait for zfs")

    exits: list[int] = []

    def exit_at_once(status: int) -> None:
        exits.append(status)
        raise SystemExit(status)

    monkeypatch.setattr(os, "_exit", exit_at_once)
    monkeypatch.setattr(pa, "_CHECK_TIMEOUT", 1)
    commands = Commands(monkeypatch, zfs=hang)
    with pytest.raises(SystemExit):
        pa.main(["freshness", f"--configdir={confdir}"])
    assert exits == [1]  # os._exit: any other way out waits for zfs first
    assert commands.mail() == {
        "subject": "pysanoid: the snapshot freshness check could not run",
        "text": "The check could not compare the snapshots with sanoid.conf:\n\n"
                "It did not finish in 0m. zfs may be hanging: see zpool status.",
    }


def test_the_deadline_is_a_quarter_of_an_hour_and_ends_with_the_check(tmp_path, monkeypatch):
    confdir = write_confdir(tmp_path, TWO_TYPES)
    set_for: list[int] = []
    monkeypatch.setattr(signal, "alarm", set_for.append)
    Commands(monkeypatch)
    assert pa.main(["freshness", "--no-mail", f"--configdir={confdir}"]) == 1
    assert set_for == [15 * 60, 0]


def test_the_default_configdir_is_pysanoids():
    assert pa.parse_args(["freshness"]).configdir == ps.parse_args([]).configdir


# ---------------------------------------------------------------------------
# the script as a TrueNAS host runs it: real processes, with stand-ins for zfs, midclt and journalctl first on PATH
# ---------------------------------------------------------------------------

FAKE_PROGRAM = """#!{python}
import json, os, sys, time

name = os.path.basename(sys.argv[0])
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
with open(os.environ["FAKE_LOG"] + ".cmdlines", "a") as cmdlines:
    cmdlines.write(json.dumps(sys.orig_argv) + "\\n")
with open(os.environ["FAKE_LOG"] + ".pids", "a") as pids:
    pids.write(str(os.getpid()) + "\\n")
time.sleep(float(os.environ.get("FAKE_" + name.upper() + "_SLEEP", "0")))
sys.stdout.write(os.environ.get("FAKE_" + name.upper() + "_OUT", ""))
sys.stderr.write(os.environ.get("FAKE_" + name.upper() + "_ERR", ""))
sys.exit(int(os.environ.get("FAKE_" + name.upper() + "_EXIT", "0")))
"""


class Host:
    """A directory of stand-in programs, the environment that puts them first on PATH, and what they were asked. Its
    python3 is the Python running these tests, which the script's `#!/usr/bin/env python3` then finds."""

    def __init__(self, tmp_path: Path):
        bindir = tmp_path / "bin"
        bindir.mkdir()
        programs = {name: FAKE_PROGRAM.format(python=sys.executable) for name in ("zfs", "midclt", "journalctl")}
        programs["python3"] = f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'
        for name, text in programs.items():
            (bindir / name).write_text(text)
            (bindir / name).chmod(0o755)
        self.log = tmp_path / "calls.jsonl"
        self.env = {name: value for name, value in os.environ.items()
                    if not name.startswith("MONITOR_") and name != "PYTHONDONTWRITEBYTECODE"}
        self.env["PATH"] = f"{bindir}{os.pathsep}{os.environ['PATH']}"
        self.env["FAKE_LOG"] = str(self.log)

    def run(self, *args: str, script: Path = SCRIPT, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(script), *args], env={**self.env, **env}, capture_output=True, text=True,
                              timeout=120, check=False)

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def stop_strays(self) -> None:
        """Ends any stand-in still running, as one the script left without waiting for is."""
        for pid in Path(f"{self.log}.pids").read_text().split():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid), signal.SIGKILL)


def test_as_a_cron_job_stale_snapshots_are_mailed(tmp_path):
    host = Host(tmp_path)
    confdir = write_confdir(tmp_path, TWO_TYPES)
    old = int(time.time()) - 3 * 86400
    proc = host.run("freshness", f"--configdir={confdir}",
                     FAKE_ZFS_OUT=listing(("tank@autosnap_a_daily", old), ("tank@autosnap_a_hourly", old)))
    assert (proc.returncode, proc.stdout, proc.stderr) == (1, "mailed: pysanoid: stale snapshots on 1 dataset\n", "")
    zfs, midclt = host.calls()
    assert zfs == ["zfs", "list", "-H", "-p", "-d", "1", "-t", "snapshot", "-o", "name,creation", "tank"]
    assert midclt[:6] == ["midclt", "call", "-j", "-jp", "description", "mail.send"]
    assert json.loads(midclt[6]) == {
        "subject": "pysanoid: stale snapshots on 1 dataset",
        "text": "pysanoid is not taking these snapshots on schedule:\n\n"
                "tank: newest daily snapshot is 3d old (limit 1d 4h)\n"
                "tank: newest hourly snapshot is 3d old (limit 1h 30m)\n\n"
                "Start with: systemctl status pysanoid.timer pysanoid.service",
    }


def test_as_a_cron_job_a_snapshot_dated_in_the_future_is_mailed(tmp_path):
    host = Host(tmp_path)
    confdir = write_confdir(tmp_path, TWO_TYPES)
    now = int(time.time())
    proc = host.run("freshness", f"--configdir={confdir}", FAKE_ZFS_OUT=listing(
        ("tank@autosnap_a_daily", now + 40 * 86400 + 1800), ("tank@autosnap_a_hourly", now - 120)))
    assert (proc.returncode, proc.stdout, proc.stderr) == (1, "mailed: pysanoid: stale snapshots on 1 dataset\n", "")
    assert json.loads(host.calls()[1][6])["text"].startswith(
        "pysanoid is not taking these snapshots on schedule:\n\n"
        "tank: newest daily snapshot is dated 40d in the future: pysanoid sets it aside, neither counting nor destroying it, until the clock passes it\n\n")


def test_as_a_cron_job_fresh_snapshots_are_quiet(tmp_path):
    host = Host(tmp_path)
    confdir = write_confdir(tmp_path, TWO_TYPES)
    recent = int(time.time()) - 120
    proc = host.run("freshness", f"--configdir={confdir}",
                     FAKE_ZFS_OUT=listing(("tank@autosnap_a_daily", recent), ("tank@autosnap_a_hourly", recent)))
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "OK: 2 snapshot types on 1 dataset, none stale\n", "")
    assert [call[0] for call in host.calls()] == ["zfs"]


def test_as_a_cron_job_a_mail_that_fails_leaves_the_report_on_stderr(tmp_path):
    host = Host(tmp_path)
    confdir = write_confdir(tmp_path, TWO_TYPES)
    proc = host.run("freshness", f"--configdir={confdir}", FAKE_MIDCLT_EXIT="1",
                     FAKE_MIDCLT_ERR="[EFAULT] None of the local administrators has an e-mail address configured\n")
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr.startswith("pysanoid_alert: TrueNAS did not send the mail: [EFAULT] None of the local "
                                  "administrators has an e-mail address configured\n\nSubject: pysanoid: stale ")


def test_as_the_failure_handler_the_run_log_is_mailed(tmp_path):
    host = Host(tmp_path)
    proc = host.run("failure", "pysanoid.service", MONITOR_SERVICE_RESULT="exit-code", MONITOR_EXIT_CODE="exited",
                     MONITOR_EXIT_STATUS="1", MONITOR_INVOCATION_ID=INVOCATION,
                     FAKE_JOURNALCTL_OUT="ERROR: could not take a snapshot\nERROR: 1 problem(s) this run\n")
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "mailed: pysanoid.service failed\n", "")
    sync, query, midclt = host.calls()
    assert sync == ["journalctl", "--sync"]
    assert query == ["journalctl", "--no-pager", "-o", "short-iso", "-n", "200",
                     f"_SYSTEMD_INVOCATION_ID={INVOCATION}", "+", f"INVOCATION_ID={INVOCATION}"]
    assert json.loads(midclt[6]) == {
        "subject": "pysanoid.service failed",
        "text": "pysanoid.service failed (exit-code: exited, status 1). Its log:\n\n"
                "ERROR: could not take a snapshot\nERROR: 1 problem(s) this run",
    }


def test_as_the_failure_handler_the_log_is_in_no_command_line(tmp_path):
    host = Host(tmp_path)
    proc = host.run("failure", "pysanoid.service", FAKE_JOURNALCTL_OUT="ERROR: a line only the journal holds\n")
    assert (proc.returncode, proc.stderr) == (0, "")
    assert "a line only the journal holds" in json.loads(host.calls()[-1][6])["text"]
    cmdlines = Path(f"{host.log}.cmdlines").read_text().splitlines()
    assert len(cmdlines) == 3  # journalctl twice, midclt
    assert not any("only the journal holds" in line for line in cmdlines)


def test_the_check_leaves_no_bytecode_beside_pysanoid(tmp_path):
    host = Host(tmp_path)
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    for name in ("pysanoid.py", "pysanoid_alert.py"):
        shutil.copy(REPO_ROOT / name, deployed / name)
    confdir = write_confdir(tmp_path, TWO_TYPES)
    proc = host.run("freshness", "--no-mail", f"--configdir={confdir}", script=deployed / "pysanoid_alert.py")
    assert proc.returncode == 1
    assert proc.stdout.startswith("Subject: pysanoid: stale snapshots on 1 dataset\n")
    assert sorted(path.name for path in deployed.iterdir()) == ["pysanoid.py", "pysanoid_alert.py"]


def beside_a_pysanoid_that_does_not_load(tmp_path: Path) -> Path:
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    shutil.copy(SCRIPT, deployed / "pysanoid_alert.py")
    (deployed / "pysanoid.py").write_text("def half_copied(:\n")
    return deployed / "pysanoid_alert.py"


def test_the_failure_of_a_pysanoid_that_does_not_load_is_still_mailed(tmp_path):
    host = Host(tmp_path)
    proc = host.run("failure", "pysanoid.service", script=beside_a_pysanoid_that_does_not_load(tmp_path),
                     FAKE_JOURNALCTL_OUT="SyntaxError: invalid syntax\n")
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "mailed: pysanoid.service failed\n", "")
    assert json.loads(host.calls()[-1][6])["text"].endswith("SyntaxError: invalid syntax")


def test_a_pysanoid_that_does_not_load_is_what_the_check_mails(tmp_path):
    host = Host(tmp_path)
    confdir = write_confdir(tmp_path, TWO_TYPES)
    proc = host.run("freshness", f"--configdir={confdir}", script=beside_a_pysanoid_that_does_not_load(tmp_path))
    assert (proc.returncode, proc.stderr) == (1, "")
    (midclt,) = host.calls()
    mail = json.loads(midclt[6])
    assert mail["subject"] == "pysanoid: the snapshot freshness check could not run"
    assert "SyntaxError: " in mail["text"]


def test_a_zfs_that_hangs_is_reported_without_waiting_for_it(tmp_path):
    host = Host(tmp_path)
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    shutil.copy(REPO_ROOT / "pysanoid.py", deployed / "pysanoid.py")
    quarter_hour = SCRIPT.read_text()
    one_second = quarter_hour.replace("_CHECK_TIMEOUT = 15 * _MINUTE", "_CHECK_TIMEOUT = 1")
    assert one_second != quarter_hour
    (deployed / "pysanoid_alert.py").write_text(one_second)
    (deployed / "pysanoid_alert.py").chmod(0o755)
    confdir = write_confdir(tmp_path, TWO_TYPES)

    started = time.monotonic()
    try:
        proc = host.run("freshness", f"--configdir={confdir}", script=deployed / "pysanoid_alert.py",
                         FAKE_ZFS_SLEEP="90")
        took = time.monotonic() - started
    finally:
        host.stop_strays()
    assert (proc.returncode, proc.stderr) == (1, "")
    assert proc.stdout == "mailed: pysanoid: the snapshot freshness check could not run\n"
    assert took < 45  # the stand-in zfs was still asleep
    zfs, midclt = host.calls()
    assert zfs[:2] == ["zfs", "list"]
    assert json.loads(midclt[6])["text"].endswith("It did not finish in 0m. zfs may be hanging: see zpool status.")


def test_the_script_can_be_run_as_the_units_run_it():
    assert os.access(SCRIPT, os.X_OK)
    assert SCRIPT.read_text().startswith("#!/usr/bin/env python3\n")


# ---------------------------------------------------------------------------
# the units
# ---------------------------------------------------------------------------

def unit(name: str) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=False)
    assert parser.read(REPO_ROOT / name) == [str(REPO_ROOT / name)]
    return parser


@pytest.mark.parametrize("name", ["pysanoid.service", "pysanoid-prune.service"])
def test_both_units_name_the_failure_handler(name):
    assert unit(name)["Unit"]["onfailure"] == "pysanoid-alert@%n.service"


def test_the_handler_runs_the_script_beside_pysanoid_and_cannot_hang():
    handler = unit("pysanoid-alert@.service")["Service"]
    pysanoid = Path(unit("pysanoid.service")["Service"]["execstart"].split()[0])
    assert pysanoid.name == "pysanoid.py"
    assert handler["execstart"] == f"{pysanoid.parent}/pysanoid_alert.py failure %i"
    assert handler["type"] == "oneshot"
    assert handler["timeoutstartsec"] == "10min"  # longer than the mail's own time limit; a oneshot unit has none
