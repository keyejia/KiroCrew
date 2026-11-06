"""A data home another account owns: refused root runs, found leftovers, the fix line."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kiro_crew import data_home_owner
from kiro_crew.platform_compat import IS_POSIX

pytestmark = pytest.mark.skipif(not IS_POSIX, reason="ownership by uid is POSIX only")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "crew"
    (home / "members").mkdir(parents=True)
    (home / "config.json").write_text("{}", encoding="utf-8")
    return home


def test_a_home_this_account_owns_is_ok(tmp_path):
    access = data_home_owner.check_data_home(_home(tmp_path))
    assert access.ok
    assert access.foreign == ()
    assert access.fix_command == ""


def test_missing_entries_are_not_reported(tmp_path):
    home = tmp_path / "crew"
    home.mkdir()
    assert data_home_owner.check_data_home(home).ok


def test_files_another_account_owns_are_reported_with_the_fix(tmp_path, monkeypatch):
    home = _home(tmp_path)
    real = os.geteuid()
    # Seen from another account, every existing entry belongs to someone else.
    monkeypatch.setattr(data_home_owner.os, "geteuid", lambda: real + 1)
    access = data_home_owner.check_data_home(home)
    assert not access.ok
    assert access.foreign == (str(home), str(home / "config.json"), str(home / "members"))
    assert access.fix_command.startswith("sudo chown -R ")
    assert access.fix_command.endswith(f"{home} && chmod -R u+rwX {home}")
    assert access.to_json()["ok"] is False


def test_an_unusable_file_this_account_owns_is_reported(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setattr(
        data_home_owner.os, "access", lambda path, mode: Path(path).name != "config.json"
    )
    assert data_home_owner.check_data_home(home).foreign == (str(home / "config.json"),)


def test_the_fix_line_quotes_a_path_with_spaces(tmp_path):
    home = tmp_path / "my crew"
    assert f"'{home}'" in data_home_owner.fix_command(home)


@pytest.mark.skipif(IS_POSIX and os.geteuid() == 0, reason="needs a home root does not own")
def test_root_aimed_at_a_persons_home_is_refused(tmp_path, monkeypatch):
    home = tmp_path / "not-yet" / "crew"
    monkeypatch.setattr(data_home_owner.os, "geteuid", lambda: 0)
    refusal = data_home_owner.root_run_refusal(home, "gateway", ["gateway", "--port", "auto"])
    assert "running as root" in refusal
    assert str(home) in refusal
    assert refusal.endswith("Run it without sudo: kirocrew gateway --port auto")


def test_a_normal_account_is_never_refused(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("this host runs the suite as root")
    assert data_home_owner.root_run_refusal(_home(tmp_path), "gateway", ["gateway"]) == ""


def test_root_in_roots_own_home_is_left_alone(monkeypatch):
    monkeypatch.setattr(data_home_owner.os, "geteuid", lambda: 0)
    # The closest existing folder is "/", which root owns.
    home = Path("/") / "kirocrew-test-absent" / "crew"
    assert data_home_owner.root_run_refusal(home, "gateway", ["gateway"]) == ""


@pytest.mark.skipif(IS_POSIX and os.geteuid() == 0, reason="needs a home root does not own")
@pytest.mark.parametrize("command", sorted(data_home_owner.ROOT_ALLOWED_COMMANDS))
def test_the_documented_root_commands_are_left_alone(tmp_path, monkeypatch, command):
    monkeypatch.setattr(data_home_owner.os, "geteuid", lambda: 0)
    assert data_home_owner.root_run_refusal(_home(tmp_path), command, [command]) == ""


def test_the_cli_refuses_before_it_touches_the_data_home(monkeypatch, capsys):
    from kiro_crew import cli

    def touched():
        raise AssertionError("the data home was resolved before the refusal")

    from kiro_crew.platform import tree_liveness

    monkeypatch.setattr(cli, "ensure_data_home", touched)
    # The first step after the refusal for a non-gateway command.
    monkeypatch.setattr(tree_liveness, "hold_running_tree_lock", touched)
    monkeypatch.setattr(
        data_home_owner, "root_run_refusal", lambda home, command, argv: "refused: " + command
    )
    monkeypatch.setattr(sys, "argv", ["kirocrew", "status"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "refused: status" in capsys.readouterr().err


def test_doctor_reports_the_files_and_the_fix(monkeypatch, capsys, tmp_path):
    from kiro_crew.doctor_checks import install

    found = data_home_owner.DataHomeAccess(
        home=str(tmp_path), foreign=(str(tmp_path / "config.json"),), fix_command="FIX"
    )
    monkeypatch.setattr(data_home_owner, "check_data_home", lambda home: found)
    issues: list[str] = []
    install._doctor_data_home_owner(issues)
    out = capsys.readouterr().out
    assert str(tmp_path / "config.json") in out
    assert "fix: FIX" in out
    assert issues == ["data home files belong to another user; run: FIX"]


def test_doctor_is_quiet_when_this_account_owns_the_home(monkeypatch, capsys, tmp_path):
    from kiro_crew.doctor_checks import install

    monkeypatch.setattr(
        data_home_owner, "check_data_home", lambda home: data_home_owner.DataHomeAccess(str(home))
    )
    issues: list[str] = []
    install._doctor_data_home_owner(issues)
    assert issues == []
    assert "✅" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_api_status_carries_the_data_home_check(monkeypatch, tmp_path):
    from test_handlers_system_meminfo_unavailable import _status_body

    from kiro_crew.dashboard import handlers_system

    found = data_home_owner.DataHomeAccess(
        home=str(tmp_path), foreign=(str(tmp_path / "config.json"),), fix_command="FIX"
    )
    seen: list[Path] = []

    def check(home):
        seen.append(home)
        return found

    monkeypatch.setattr(handlers_system, "data_home", lambda: tmp_path)
    monkeypatch.setattr(data_home_owner, "check_data_home", check)
    body = await _status_body(monkeypatch, tmp_path, {"os": "Linux", "arch": "x86_64"})
    assert seen == [tmp_path]
    assert body["data_home_access"] == {
        "ok": False,
        "home": str(tmp_path),
        "foreign": [str(tmp_path / "config.json")],
        "fix_command": "FIX",
    }
