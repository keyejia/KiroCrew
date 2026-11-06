"""Restarting a seeded pod rewrites the pod's config.json as Kiro Crew's own write.

``kirocrew pod`` (running in the operator's own data home) re-applies the seed floor to
the POD's ``config.json`` on every restart. Where the sandbox seals the keystone files,
the pod's gateway serves its endorsed copy when the file differs from it, so an
unendorsed rewrite would load as a change made outside Kiro Crew and wait in Settings.
The rewrite therefore endorses the same bytes in the pod's data home. Once the pod holds
an endorsed copy, the rewrite starts from that copy, so a raw file edited outside Kiro
Crew is not endorsed by a restart: it stays on disk, pending.

The conftest pins ``KIROCREW_HOME`` per test; the pod home here is a separate directory,
and the checks switch ``KIROCREW_HOME`` to it to read it as the pod's gateway would.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew import pinned_fs
from kiro_crew.config import loosening_provenance as lp
from kiro_crew.pod import runtime_home


@pytest.fixture(autouse=True)
def _enforcing(_floor_monkeypatch):
    _floor_monkeypatch.setattr(lp, "enforcement_active", lambda: True)


def _prepare_fd(home: Path) -> None:
    fd = os.open(home, pinned_fs.dir_flags())
    try:
        runtime_home._prepare_seeded_home_fd(fd)
    finally:
        os.close(fd)


_PREPARERS = pytest.mark.parametrize(
    "prepare",
    [
        pytest.param(
            _prepare_fd,
            id="posix_fd",
            marks=pytest.mark.skipif(os.name != "posix", reason="dir_fd"),
        ),
        pytest.param(runtime_home._prepare_seeded_home_dir, id="by_name"),
    ],
)


def _pod_home(tmp_path: Path) -> Path:
    home = tmp_path / "pod"
    home.mkdir(mode=0o700)
    return home


def _write_raw(home: Path, data: dict) -> bytes:
    raw = json.dumps(data).encode("utf-8")
    (home / "config.json").write_bytes(raw)
    return raw


def _load_as_pod(home: Path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    return loader.KiroCrewConfig.load()


@_PREPARERS
def test_a_first_seed_rewrites_the_raw_file_and_is_not_pending(tmp_path, monkeypatch, prepare):
    home = _pod_home(tmp_path)
    _write_raw(home, {"tunnel": {"enabled": True}, "agent": {"bot_name": "Seeded"}})

    prepare(home)

    cfg = _load_as_pod(home, monkeypatch)
    assert lp.pending_changes() == []
    assert cfg.agent.bot_name == "Seeded"
    assert cfg.tunnel.enabled is False


@_PREPARERS
def test_a_restart_floors_the_endorsed_config_and_is_not_pending(tmp_path, monkeypatch, prepare):
    home = _pod_home(tmp_path)
    # The pod gateway's own save: the file and its copy agree, and the tunnel is on.
    raw = _write_raw(home, {"tunnel": {"enabled": True}, "agent": {"bot_name": "Seeded"}})
    lp.record(lp.LEAF_CONFIG, raw, strict=True, home=home)

    prepare(home)

    cfg = _load_as_pod(home, monkeypatch)
    assert lp.pending_changes() == []
    assert cfg.agent.bot_name == "Seeded"
    assert cfg.tunnel.enabled is False


@_PREPARERS
def test_a_restart_does_not_endorse_an_unreviewed_edit(tmp_path, monkeypatch, prepare):
    """An out-of-band edit to an already-seeded pod stays pending across a restart."""
    home = _pod_home(tmp_path)
    endorsed = json.dumps({"agent": {"bot_name": "Seeded"}}).encode("utf-8")
    lp.record(lp.LEAF_CONFIG, endorsed, strict=True, home=home)
    edited = _write_raw(
        home, {"agent": {"bot_name": "Seeded"}, "hooks": {"auto_approve_tools": ["*"]}}
    )

    prepare(home)

    assert (home / "config.json").read_bytes() == edited
    assert b"auto_approve_tools" not in (lp.endorsed_bytes(lp.LEAF_CONFIG, home=home) or b"")
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    assert [entry["leaf"] for entry in lp.pending_changes()] == [lp.LEAF_CONFIG]
    cfg = _load_as_pod(home, monkeypatch)
    assert "auto_approve_tools" not in cfg.hooks
    assert cfg.agent.bot_name == "Seeded"
    assert cfg.tunnel.enabled is False


@_PREPARERS
def test_without_the_seal_a_restart_reads_the_raw_file(tmp_path, monkeypatch, prepare):
    """Where nothing seals these files the pod gateway loads the raw file, hand edits too."""
    monkeypatch.setattr(lp, "enforcement_active", lambda: False)
    home = _pod_home(tmp_path)
    lp.record(lp.LEAF_CONFIG, b'{"agent": {"bot_name": "Old"}}', strict=True, home=home)
    _write_raw(home, {"agent": {"bot_name": "HandEdited"}, "tunnel": {"enabled": True}})

    prepare(home)

    # The seed floor reaches the raw file the pod loads.
    assert json.loads((home / "config.json").read_bytes())["tunnel"]["enabled"] is False
    cfg = _load_as_pod(home, monkeypatch)
    assert cfg.agent.bot_name == "HandEdited"
    assert cfg.tunnel.enabled is False


@_PREPARERS
def test_a_pending_edit_in_the_operators_own_home_does_not_block_a_pod_restart(
    tmp_path, monkeypatch, prepare
):
    """The refusal of a save to a pending file is about THIS data home's file. The pod
    rewrite writes another home's config.json, so it still runs and is endorsed there."""
    from kiro_crew.config.paths import config_dir

    own = config_dir() / lp.LEAF_CONFIG
    lp.record(lp.LEAF_CONFIG, b'{"agent": {"bot_name": "Owner"}}', strict=True)
    own.write_bytes(b'{"agent": {"bot_name": "HandEdited"}}')
    assert lp.leaf_pending(lp.LEAF_CONFIG)
    home = _pod_home(tmp_path)
    _write_raw(home, {"tunnel": {"enabled": True}, "agent": {"bot_name": "Seeded"}})

    prepare(home)

    assert own.read_bytes() == b'{"agent": {"bot_name": "HandEdited"}}'
    cfg = _load_as_pod(home, monkeypatch)
    assert lp.pending_changes() == []
    assert cfg.agent.bot_name == "Seeded"
    assert cfg.tunnel.enabled is False
