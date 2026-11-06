"""Whole-file content provenance for the sandbox-sealed keystone config files.

The Linux per-file read-only bind stops sealing ``config.json`` (and the other
protected files) after the gateway's next rename-save, so a prompt-injected agent can
rewrite it directly. ``loosening_provenance`` stores the exact bytes of every gateway
write as the endorsed copy and, where the namespace sandbox seals these files, serves
the endorsed copy whenever the on-disk bytes differ -- covering every key with no key
list. These tests pin: the host-facts-only scope gate, record/serve, the one-time
adoption, and the end-to-end substitution through the real load pipeline.

The conftest pins ``KIROCREW_HOME`` per test, so ``config_dir()`` is a fresh temp
directory and every writer/reader below resolves to the same home with no patching.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.config import loosening_provenance as lp
from kiro_crew.config.paths import config_dir


def _config(**agent: object) -> bytes:
    return (json.dumps({"agent": agent}, indent=2) + "\n").encode("utf-8")


def test_enforcement_active_is_false_off_linux(monkeypatch) -> None:
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    assert lp.enforcement_active() is False
    monkeypatch.setattr(sys, "platform", "win32")
    assert lp.enforcement_active() is False


def test_reconcile_is_a_noop_when_not_enforcing(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: False)
    lp.record("config.json", _config(sandbox="auto"))
    tampered = _config(sandbox="off")
    assert lp.reconcile("config.json", tampered) == tampered  # on-disk honored


def test_reconcile_serves_endorsed_copy_on_tamper_when_enforcing(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    safe = _config(sandbox="auto")
    lp.record("config.json", safe)
    assert lp.reconcile("config.json", _config(sandbox="off")) == safe
    assert lp.reconcile("config.json", safe) == safe  # equal -> unchanged


def test_reconcile_returns_disk_when_no_endorsed_copy(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    disk = _config(sandbox="off")
    assert lp.reconcile("config.json", disk) == disk  # pre-first-boot window


def test_endorsed_copy_is_owner_only() -> None:
    import sys

    lp.record("config.json", _config(sandbox="off"))
    if sys.platform == "win32":
        pytest.skip("POSIX mode bits do not apply on Windows")
    assert (lp._endorsed_dir().stat().st_mode & 0o777) == 0o700
    assert ((config_dir() / lp.RECORD_DIR_LEAF).stat().st_mode & 0o777) == 0o700
    assert (lp._endorsed_path("config.json").stat().st_mode & 0o777) == 0o600


def test_adoption_endorses_existing_once_and_never_re_adopts(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    home = config_dir()
    (home / "config.json").write_bytes(_config(sandbox="off"))
    assert lp.adopt_unendorsed() == ["config.json"]
    # After adoption a later in-sandbox rewrite is overridden by the adopted copy.
    assert lp.adopt_unendorsed() == []
    (home / "config.json").write_bytes(_config(sandbox="off", model="tampered"))
    assert lp.reconcile("config.json", (home / "config.json").read_bytes()) == _config(
        sandbox="off"
    )


def test_adoption_skips_absent_files() -> None:
    assert lp.adopt_unendorsed() == []


def test_scope_decision_uses_host_facts_not_the_file(monkeypatch) -> None:
    """The enforce decision reads HOST FACTS only, never the file it is serving.

    If it read ``agent.sandbox`` from the file, writing ``sandbox: off`` would switch
    the substitution off along with the sandbox. Force a Linux host whose namespace
    sandbox can be built, endorse a safe copy, tamper the disk with ``sandbox: off``,
    and confirm the endorsed copy still wins.
    """
    import sys

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(lp, "_host_verdict", None)
    monkeypatch.setattr(lp, "_probe_host", lambda: True)
    assert lp.enforcement_active() is True
    lp.record("config.json", _config(sandbox="auto"))
    assert lp.reconcile("config.json", _config(sandbox="off")) == _config(sandbox="auto")


@pytest.mark.parametrize(
    ("verdict", "answer", "cached"),
    [
        ((True, False, "ok", ""), True, True),
        ((False, False, "unshare(CLONE_NEWUSER) failed with errno 1 (EPERM)", ""), False, True),
        ((False, True, "fork failed with errno 11 (EAGAIN)", ""), True, True),
    ],
    ids=["capable", "permanent-refusal", "transient"],
)
def test_the_host_probe_is_quiet_and_cached_per_process(
    monkeypatch, caplog, verdict, answer, cached
) -> None:
    """Every config read asks this, so it must not probe, log or touch sandbox state per read.

    A definite verdict is probed once and cached; a transient one enforces and is not
    probed again inside its retry window. The sandbox's own recorded failure (what a spawn refusal reports)
    is left exactly as it was.
    """
    import logging
    import sys

    import kiro_crew.sandbox as sb
    import kiro_crew.sandbox_wrap as sw

    calls: list[int] = []

    def fake_probe() -> tuple[bool, bool, str, str]:
        calls.append(1)
        return verdict

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(lp, "_host_verdict", None)
    monkeypatch.setattr(lp, "_transient_until", 0.0)
    monkeypatch.setattr(sb, "_probe_unshare_once", fake_probe)
    planted = (True, "planted by the test", "")
    monkeypatch.setattr(sw, "_last_unshare_failure", planted)
    with caplog.at_level(logging.DEBUG):
        assert lp.enforcement_active() is answer
        assert lp.enforcement_active() is answer
    assert len(calls) == (1 if cached else 2)
    assert sw._last_unshare_failure is planted
    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


def test_a_transient_probe_is_retried_once_per_window(monkeypatch) -> None:
    """A transient failure is not re-probed by every read; the window's end probes again."""
    import sys
    import types

    import kiro_crew.sandbox as sb

    clock = [1000.0]
    calls: list[int] = []

    def fake_probe() -> tuple[bool, bool, str, str]:
        calls.append(1)
        return (False, True, "unshare(CLONE_NEWUSER) failed with errno 28 (ENOSPC)", "")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(lp, "_host_verdict", None)
    monkeypatch.setattr(lp, "_transient_until", 0.0)
    monkeypatch.setattr(lp, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(sb, "_probe_unshare_once", fake_probe)
    for _ in range(20):
        assert lp.enforcement_active() is True
    assert len(calls) == 1
    clock[0] += lp._TRANSIENT_RETRY_SECS
    assert lp.enforcement_active() is True
    assert len(calls) == 2


@pytest.fixture
def _enforcing(monkeypatch):
    """Force the Linux-namespace scope gate on, so the substitution is deterministic."""
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    yield loader
    loader._invalidate_config_cache()


def _load_data(loader) -> dict:
    loader._invalidate_config_cache()
    return loader.read_config_document().data


def test_tampered_file_loads_the_endorsed_copy_whole(_enforcing) -> None:
    """Real-pipeline proof: a tampered config loads EQUAL to the endorsed copy.

    The tamper flips keys across the whole file -- ``agent.sandbox`` off,
    ``agent.apps_allow_third_party`` on, and ``hooks.auto_approve_tools`` (a key no
    per-key clamp covered) -- and every one is overridden by the endorsed copy.
    """
    loader = _enforcing
    home = config_dir()
    cfg_path = home / "config.json"

    endorsed = {"agent": {"sandbox": "auto"}, "hooks": {}}
    loader.write_config_atomically(cfg_path, endorsed)  # endorses the safe copy
    cfg_path.write_text(
        json.dumps(
            {
                "agent": {"sandbox": "off", "apps_allow_third_party": True},
                "hooks": {"auto_approve_tools": ["rm"]},
            }
        ),
        encoding="utf-8",
    )
    data = _load_data(loader)
    assert data.get("agent", {}).get("sandbox") == "auto"
    assert "apps_allow_third_party" not in data.get("agent", {})
    assert "auto_approve_tools" not in data.get("hooks", {})

    # An ordinary save would publish over the pending edit, so it is refused; the
    # owner's Apply (resolving_pending) is the writer that takes effect.
    with pytest.raises(loader.PendingEditBlocksSave):
        loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "off"}})
    assert _load_data(loader).get("agent", {}).get("sandbox") == "auto"
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "off"}}, resolving_pending=True)
    assert _load_data(loader).get("agent", {}).get("sandbox") == "off"


def test_tampered_file_honored_when_not_enforcing(monkeypatch) -> None:
    """Off Linux / no namespace sandbox: the loader serves the on-disk file as written."""
    monkeypatch.setattr(lp, "enforcement_active", lambda: False)
    from kiro_crew.config import loader

    loader._invalidate_config_cache()
    (config_dir() / "config.json").write_bytes(_config(sandbox="off"))
    try:
        assert loader.read_config_document().data.get("agent", {}).get("sandbox") == "off"
    finally:
        loader._invalidate_config_cache()


# --- Read-modify-write writers start from the endorsed copy (no laundering) ----------


def test_unrelated_config_update_does_not_endorse_a_tampered_file(_enforcing) -> None:
    """A later, unrelated save must not re-endorse what an agent wrote past the seal.

    Nor may it publish over that file: the save is refused before anything is written,
    the endorsed copy is unchanged, and the on-disk edit stays pending for the owner.
    """
    loader = _enforcing
    cfg_path = config_dir() / "config.json"
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "auto"}})
    cfg_path.write_text(json.dumps({"agent": {"sandbox": "off"}}), encoding="utf-8")

    def _patch(doc: dict) -> dict:
        doc.setdefault("ui", {})["theme"] = "dark"
        return doc

    with pytest.raises(loader.PendingEditBlocksSave):
        loader.update_config_locked(cfg_path, mutate=_patch, stamp_meta=False)

    assert json.loads(lp.endorsed_bytes("config.json") or b"{}") == {"agent": {"sandbox": "auto"}}
    assert json.loads(cfg_path.read_bytes()) == {"agent": {"sandbox": "off"}}
    assert _load_data(loader)["agent"]["sandbox"] == "auto"
    assert [entry["leaf"] for entry in lp.pending_changes()] == ["config.json"]


def test_denied_commands_save_does_not_endorse_a_tampered_file(_enforcing) -> None:
    import asyncio

    from kiro_crew.dashboard.handlers import security

    path = config_dir() / "denied_commands.json"
    asyncio.run(security._write_denied_state(lambda d: d.update({"disable_all": False})))
    path.write_text(json.dumps({"disable_all": True}), encoding="utf-8")

    from kiro_crew.config.loader import PendingEditBlocksSave

    with pytest.raises(PendingEditBlocksSave):
        asyncio.run(security._write_denied_state(lambda d: d.update({"disabled_ids": ["x"]})))

    endorsed = json.loads(lp.endorsed_bytes("denied_commands.json") or b"{}")
    assert endorsed == {"disable_all": False}
    assert json.loads(path.read_bytes()) == {"disable_all": True}


def test_computer_use_save_does_not_endorse_a_tampered_file(_enforcing) -> None:
    from kiro_crew.computer_use import enable_state
    from kiro_crew.dashboard.handlers import computer_use

    enable_state.save_state({"enabled": False})
    enable_state.computer_use_state_path().write_text(
        json.dumps({"enabled": True}), encoding="utf-8"
    )

    from kiro_crew.config.loader import PendingEditBlocksSave

    with pytest.raises(PendingEditBlocksSave):
        computer_use._write_state({"allowed_apps": ["Calculator"]})

    assert enable_state.load_state() == {"enabled": False}
    assert json.loads(enable_state.computer_use_state_path().read_bytes()) == {"enabled": True}


# --- Writers endorse their own bytes, before the publish -----------------------------


def test_writer_endorses_its_own_bytes_not_a_reread(_enforcing, monkeypatch) -> None:
    """Bytes an agent lands on the name right after the rename are never endorsed."""
    loader = _enforcing
    cfg_path = config_dir() / "config.json"
    real_write = loader.atomic_write

    def _write_then_race(path, *args, **kwargs):
        real_write(path, *args, **kwargs)
        path.write_text(json.dumps({"agent": {"sandbox": "off"}}), encoding="utf-8")

    # Through the loader facade: atomic_write is shared by its owners, and the
    # mirror lands the patch on loader_document, which publishes the file.
    monkeypatch.setattr(loader, "atomic_write", _write_then_race)
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "auto"}})

    assert json.loads(lp.endorsed_bytes("config.json") or b"{}") == {"agent": {"sandbox": "auto"}}


# --- A failed record never drops the content check ------------------------------------


def test_record_failure_keeps_the_prior_endorsed_copy(monkeypatch) -> None:
    safe = _config(sandbox="auto")
    lp.record("config.json", safe)

    def _fail(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(lp, "atomic_write", _fail)
    lp.record("config.json", _config(sandbox="off"))
    assert lp.endorsed_bytes("config.json") == safe


def test_unendorsable_write_is_not_published_when_enforcing(_enforcing, monkeypatch) -> None:
    loader = _enforcing
    cfg_path = config_dir() / "config.json"
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "auto"}})
    before = cfg_path.read_bytes()

    def _fail(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(lp, "atomic_write", _fail)
    with pytest.raises(OSError):
        loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "off"}})
    assert cfg_path.read_bytes() == before


def test_failed_publish_restores_the_prior_endorsed_copy(_enforcing, monkeypatch) -> None:
    loader = _enforcing
    cfg_path = config_dir() / "config.json"
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "auto"}})
    prior = lp.endorsed_bytes("config.json")

    def _fail(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(loader, "atomic_write", _fail)  # facade: the name is shared
    with pytest.raises(OSError):
        loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "off"}})
    assert lp.endorsed_bytes("config.json") == prior


def test_unreadable_endorsed_copy_fails_closed_when_enforcing(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    lp._endorsed_dir().mkdir(parents=True)
    lp._endorsed_path("config.json").mkdir()  # exists, but read_bytes() raises
    with pytest.raises(OSError):
        lp.reconcile("config.json", _config(sandbox="off"))


# --- A symlinked config is endorsed under its logical name ---------------------------


def test_symlinked_config_save_is_endorsed_and_loaded(_enforcing, tmp_path) -> None:
    loader = _enforcing
    target = tmp_path / "dotfiles" / "crew-settings.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"agent": {"sandbox": "auto"}}), encoding="utf-8")
    cfg_path = config_dir() / "config.json"
    cfg_path.symlink_to(target)
    lp.adopt_unendorsed()

    def _flip(doc: dict) -> dict:
        doc["agent"]["sandbox"] = "off"
        return doc

    loader.update_config_locked(cfg_path, mutate=_flip, stamp_meta=False)

    assert cfg_path.is_symlink()
    assert lp.endorsed_bytes("config.json") == target.read_bytes()
    assert _load_data(loader)["agent"]["sandbox"] == "off"


def test_leaf_for_matches_by_resolved_location(tmp_path) -> None:
    assert lp.leaf_for(config_dir() / "config.json") == "config.json"
    assert lp.leaf_for(tmp_path / "config.json") is None  # same basename, other file


# --- The endorsed copy is the published bytes, on every platform --------------------


def test_every_endorsing_writer_publishes_the_bytes_it_endorses(_enforcing, monkeypatch) -> None:
    """The writers hand ``atomic_write`` the endorsed bytes themselves, not a ``str`` it
    would encode again: text mode writes ``\\r\\n`` on Windows, and a copy of the ``\\n``
    form would differ from the file, so every load would serve the copy and report the
    gateway's own save as pending."""
    import asyncio

    from kiro_crew import atomic_write as aw
    from kiro_crew.computer_use import enable_state
    from kiro_crew.dashboard.handlers import security

    loader = _enforcing
    published: dict[str, object] = {}

    def _recording(real):
        def _write(path, content, *args, **kwargs):
            published[lp.leaf_for(Path(path)) or str(path)] = content
            return real(path, content, *args, **kwargs)

        return _write

    monkeypatch.setattr(loader, "atomic_write", _recording(loader.atomic_write))
    monkeypatch.setattr(aw, "atomic_write", _recording(aw.atomic_write))
    monkeypatch.setattr(enable_state, "atomic_write", _recording(enable_state.atomic_write))

    loader.write_config_atomically(config_dir() / "config.json", {"agent": {"sandbox": "auto"}})
    asyncio.run(security._write_denied_state(lambda d: d.update({"disable_all": False})))
    enable_state.save_state({"enabled": False})

    for leaf in ("config.json", "denied_commands.json", "computer_use.json"):
        assert isinstance(published[leaf], bytes), leaf
        assert published[leaf] == lp.endorsed_bytes(leaf) == (config_dir() / leaf).read_bytes()
    assert lp.pending_changes() == []


def test_text_bytes_is_what_text_mode_writes(tmp_path) -> None:
    from kiro_crew.atomic_write import atomic_write, text_bytes

    text = '{\n  "a": 1\n}\n'
    atomic_write(tmp_path / "as_text.json", text)
    assert text_bytes(text) == (tmp_path / "as_text.json").read_bytes()


# --- A missing or unreadable file serves the endorsed copy --------------------------


def _remove(leaf: str) -> None:
    (config_dir() / leaf).unlink()


def test_a_deleted_config_loads_the_endorsed_copy(_enforcing) -> None:
    loader = _enforcing
    for leaf in ("config.json", "config.local.json"):
        loader.write_config_atomically(config_dir() / leaf, {"agent": {"sandbox": "strict"}})
        _remove(leaf)
        assert loader.read_config_for_update(config_dir() / leaf)["agent"]["sandbox"] == "strict"
        assert _load_data(loader)["agent"]["sandbox"] == "strict"
        # Discard of the removal (an owner action) puts the file back.
        loader.write_config_atomically(config_dir() / leaf, {}, resolving_pending=True)


def test_an_undecodable_config_loads_the_endorsed_copy(_enforcing) -> None:
    loader = _enforcing
    cfg_path = config_dir() / "config.json"
    loader.write_config_atomically(cfg_path, {"agent": {"sandbox": "strict"}})
    cfg_path.write_bytes(b'{"agent": {"sandbox": "off\xff"}}')
    assert _load_data(loader)["agent"]["sandbox"] == "strict"
    assert loader.read_config_for_update(cfg_path)["agent"]["sandbox"] == "strict"


def test_a_deleted_denied_commands_file_keeps_the_owner_rules(_enforcing) -> None:
    import asyncio

    from kiro_crew.dashboard.handlers import security
    from kiro_crew.hook_runtime.denied_commands import load_denied_commands_state

    rule = {"id": "user-1", "pattern": "^rm -rf", "enabled": True}
    asyncio.run(security._write_denied_state(lambda d: d.update({"user_added": [rule]})))
    _remove("denied_commands.json")
    for read in (load_denied_commands_state, security._read_denied_data):
        assert read()["user_added"] == [rule], read
    assert security._read_denied_strict()["user_added"] == [rule]


def test_a_deleted_computer_use_file_keeps_the_owner_allow_list(_enforcing) -> None:
    from kiro_crew.computer_use import enable_state
    from kiro_crew.dashboard.handlers import computer_use

    enable_state.save_state({"enabled": True, "allowed_apps": ["calculator"]})
    _remove("computer_use.json")
    assert enable_state.load_state()["allowed_apps"] == ["calculator"]
    assert computer_use._read_state_strict()["allowed_apps"] == ["calculator"]


def test_a_deleted_file_is_pending_as_removed(_enforcing) -> None:
    _enforcing.write_config_atomically(config_dir() / "config.json", {"a": 1})
    _remove("config.json")
    [entry] = lp.pending_changes()
    assert entry["leaf"] == "config.json"
    assert entry["removed"] is True and entry["signature"] == lp.REMOVED_SIGNATURE
    assert entry["disk_valid"] is False


def test_a_deleted_file_reads_as_absent_when_not_enforcing(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: False)
    from kiro_crew.config import loader

    loader.write_config_atomically(config_dir() / "config.json", {"agent": {"sandbox": "strict"}})
    _remove("config.json")
    assert loader.read_config_for_update(config_dir() / "config.json") == {}
    assert lp.pending_changes() == []


def test_endorsements_restored_on_error_puts_every_copy_back(monkeypatch) -> None:
    monkeypatch.setattr(lp, "enforcement_active", lambda: True)
    lp.record("config.json", _config(sandbox="strict"))
    with pytest.raises(RuntimeError):
        with lp.endorsements_restored_on_error():
            lp.endorse_installed(config_dir() / "config.json", _config(sandbox="off"))
            lp.endorse_installed(config_dir() / "config.local.json", _config(sandbox="off"))
            raise RuntimeError("restore failed")
    assert lp.endorsed_bytes("config.json") == _config(sandbox="strict")
    assert lp.endorsed_bytes("config.local.json") is None
