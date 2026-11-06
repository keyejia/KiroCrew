"""Kiro Crew's own writes of config.json take effect at once and are never pending.

Where the namespace sandbox seals the keystone files, a config.json that differs from
its endorsed copy loads as the endorsed copy and is held as pending
(``config.loosening_provenance``). So every Kiro Crew writer must publish through the
endorsing config writer: ``kirocrew config set`` (base and ``--local``),
``kirocrew config edit`` (which edits a private copy and saves it through the writer),
and the Weixin dashboard savers. Each test here turns enforcement on, starts from an
endorsed file, writes through one of those paths, and checks the next load sees the
value with nothing pending.

The conftest pins ``KIROCREW_HOME`` per test, so ``config_dir()`` is a fresh temp dir.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.loader import KiroCrewConfig, write_config_atomically
from kiro_crew.config.paths import config_dir


@pytest.fixture(autouse=True)
def enforcing(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    _floor_monkeypatch.setattr(lp, "enforcement_active", lambda: True)


@pytest.fixture
def endorsed_config() -> Path:
    """config.json and an identical endorsed copy: in force, nothing pending."""
    path = config_dir() / "config.json"
    content = (json.dumps({"agent": {"bot_name": "Kiro"}, "keep": {"a": 1}}) + "\n").encode()
    lp.record(lp.LEAF_CONFIG, content, strict=True)
    path.write_bytes(content)
    return path


def _bot_name() -> str:
    return KiroCrewConfig.load().agent.bot_name


def _config_cmd(args: argparse.Namespace) -> None:
    from kiro_crew.cli_config import _config_cmd as run

    with patch("kiro_crew.cli_config.sel"):
        run(args)


@pytest.mark.parametrize("local", [False, True])
def test_config_set_takes_effect_at_once_and_is_not_pending(endorsed_config, local):
    _config_cmd(
        argparse.Namespace(
            config_action="set", key="agent.bot_name", value="Nova", file=None, local=local
        )
    )

    assert lp.pending_changes() == []
    assert _bot_name() == "Nova"


def _edit(write_copy) -> None:
    """Run ``config edit`` with an editor that applies *write_copy* to the copy it opens."""

    def editor(argv: list[str]) -> int:
        write_copy(Path(argv[1]))
        return 0

    with patch("kiro_crew.cli_config.subprocess.call", side_effect=editor):
        _config_cmd(argparse.Namespace(config_action="edit"))


def _rename_bot(copy: Path) -> None:
    doc = json.loads(copy.read_text(encoding="utf-8"))
    doc["agent"]["bot_name"] = "Nova"
    copy.write_text(json.dumps(doc), encoding="utf-8")


def test_config_edit_takes_effect_at_once_and_is_not_pending(endorsed_config):
    _edit(_rename_bot)

    assert lp.pending_changes() == []
    assert _bot_name() == "Nova"
    assert json.loads(endorsed_config.read_text(encoding="utf-8"))["keep"] == {"a": 1}
    assert list(config_dir().glob("config-edit-*")) == []


def test_config_edit_refuses_while_config_json_has_a_pending_change(endorsed_config, capsys):
    endorsed_config.write_text(json.dumps({"agent": {"bot_name": "Hand"}}), encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        _edit(_rename_bot)

    assert exc.value.code == 1
    assert "waiting for review in Settings > Security" in capsys.readouterr().err
    assert json.loads(endorsed_config.read_text(encoding="utf-8")) == {
        "agent": {"bot_name": "Hand"}
    }


def test_config_edit_keeps_an_unparseable_edit_and_changes_nothing(endorsed_config, capsys):
    before = endorsed_config.read_bytes()

    with pytest.raises(SystemExit) as exc:
        _edit(lambda copy: copy.write_text("{not json", encoding="utf-8"))

    assert exc.value.code == 1
    assert endorsed_config.read_bytes() == before
    (kept,) = config_dir().glob("config-edit-*")
    assert kept.read_text(encoding="utf-8") == "{not json"
    assert str(kept) in capsys.readouterr().err


def test_config_edit_does_not_save_over_a_change_made_while_the_editor_was_open(
    endorsed_config, capsys
):
    def edit_while_someone_else_saves(copy: Path) -> None:
        write_config_atomically(endorsed_config, {"agent": {"bot_name": "Other"}})
        _rename_bot(copy)

    with pytest.raises(SystemExit) as exc:
        _edit(edit_while_someone_else_saves)

    assert exc.value.code == 1
    assert "changed while it was open in the editor" in capsys.readouterr().err
    assert _bot_name() == "Other"
    assert lp.pending_changes() == []


def test_weixin_config_save_takes_effect_at_once_and_is_not_pending(
    endorsed_config, _floor_monkeypatch
):
    from kiro_crew.dashboard.handlers import weixin_qr as qr

    _floor_monkeypatch.setattr(qr, "is_direct_local_request", lambda request: True)
    _floor_monkeypatch.setattr(qr, "_hot_apply_after_write", AsyncMock())
    request: Any = SimpleNamespace(json=AsyncMock(return_value={"dm_policy": "allowlist"}), app={})

    response = asyncio.run(qr.weixin_config_save(request))

    assert response.status == 200
    assert lp.pending_changes() == []
    assert KiroCrewConfig.load().weixin.dm_policy == "allowlist"


def test_weixin_sign_in_takes_effect_at_once_and_is_not_pending(
    endorsed_config, _floor_monkeypatch
):
    from kiro_crew.dashboard.handlers import weixin_qr as qr

    _floor_monkeypatch.setattr(qr, "apply_env_credential_changes", lambda *a, **k: None)
    cp, staged = qr._stage_weixin_config(account_id="acct1@im.bot", base_url="https://x")

    qr._commit_credential_and_config(cp, staged, "brand-new")

    assert lp.pending_changes() == []
    assert KiroCrewConfig.load().weixin.account_id == "acct1@im.bot"
