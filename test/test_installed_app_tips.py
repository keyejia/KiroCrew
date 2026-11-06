"""An installed app from a versionless index-tier registry is told of a newer release.

Such a registry leaves each app's version to its own ``app.json``. The listing
reads an installed app's ``app.json`` from its checkout, so nothing but these
tip reads tells ``update_status`` (and so the app's banner and the automatic
update) what the branch publishes now.
"""

from __future__ import annotations

import asyncio
import json
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from external_registry_helpers import cache_dir_fixture  # noqa: F401

from kiro_crew.apps import installed_app_tips
from kiro_crew.apps import registry as reg_mod
from kiro_crew.apps import update_status
from kiro_crew.apps.registry import (
    _manifest_cache_path,
    _read_manifest_cache,
    refresh_registries,
)

REGISTRY = "acme"
APP_URL = "https://github.com/acme/cool-app"


def _reg() -> Any:
    return types.SimpleNamespace(name=REGISTRY, repo="https://github.com/acme/apps", branch="main")


def _row(name: str = "cool-app", **extra: Any) -> dict[str, Any]:
    """A fresh index row as the fetch returns it: tagged with its registry, no version."""
    return {
        "name": name,
        "repo": f"acme/{name}",
        "gitUrl": f"https://github.com/acme/{name}",
        "branch": "main",
        "_registry": REGISTRY,
        **extra,
    }


def _installed(name: str = "cool-app", **extra: Any) -> dict[str, Any]:
    """An installed record that kept the row it came from, as an install writes it."""
    return {
        "name": name,
        "version": "1.0.0",
        "source": f"registry:{name}",
        "sourceUrl": f"https://github.com/acme/{name}",
        "sourceRegistry": REGISTRY,
        **extra,
    }


@pytest.fixture(name="tips")
def tips_fixture(monkeypatch, cache_dir):  # noqa: F811 - the shared cache_dir fixture
    """Index tier, the given installed records, and a tip fetch that records its calls."""
    import kiro_crew.apps.manager as manager_mod

    state: dict[str, Any] = {
        "installed": [_installed()],
        "tip": {"name": "cool-app", "version": "1.2.0"},
        "calls": [],
    }

    async def _fetch(repo, branch, subdirectory="", app_name="", git_url="", **kwargs):
        state["calls"].append({"app_name": app_name, "git_url": git_url, "branch": branch})
        tip = state["tip"]
        if isinstance(tip, BaseException):
            raise tip
        return dict(tip) if isinstance(tip, dict) else tip

    monkeypatch.setattr(reg_mod, "_registry_trust_tier_of", lambda reg: "index")
    monkeypatch.setattr(reg_mod, "_fetch_app_manifest", _fetch)
    monkeypatch.setattr(manager_mod, "list_apps", lambda: list(state["installed"]))
    return state


def _run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


class TestWhichRowsAreRead:
    def test_a_versionless_row_of_an_installed_app_gets_its_tip_cached(self, tips):
        row = _row()
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 1
        # No app name: the fetch skips the installed checkout and clones the tip.
        assert tips["calls"] == [{"app_name": "", "git_url": APP_URL, "branch": "main"}]
        assert json.loads(_manifest_cache_path(row).read_text())["version"] == "1.2.0"

    def test_a_row_with_its_own_version_is_not_read(self, tips):
        row = _row(version="1.1.0")
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 0
        assert tips["calls"] == []

    def test_an_app_that_is_not_installed_is_not_read(self, tips):
        tips["installed"] = []
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [_row()])) == 0
        assert tips["calls"] == []

    @pytest.mark.parametrize(
        "record",
        [
            _installed(sourceRegistry="someone-else"),
            _installed(sourceUrl="https://github.com/someone-else/cool-app"),
            _installed(source="/home/me/cool-app"),
            {"name": "cool-app", "version": "1.0.0", "source": "registry:cool-app"},
        ],
        ids=["other-registry", "other-repository", "local-install", "no-provenance"],
    )
    def test_an_app_installed_from_another_source_is_not_read(self, tips, record):
        tips["installed"] = [record]
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [_row()])) == 0
        assert tips["calls"] == []

    def test_a_row_tagged_for_another_registry_is_not_read(self, tips):
        row = {**_row(), "_registry": "someone-else"}
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 0
        assert tips["calls"] == []

    def test_an_owner_tier_registry_is_left_to_the_prewarm(self, tips, monkeypatch):
        monkeypatch.setattr(reg_mod, "_registry_trust_tier_of", lambda reg: "owner")
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [_row()])) == 0
        assert tips["calls"] == []

    def test_one_refresh_reads_at_most_the_cap(self, tips, monkeypatch):
        monkeypatch.setattr(installed_app_tips, "MAX_ROWS", 2)
        names = ["app-a", "app-b", "app-c"]
        tips["installed"] = [_installed(n) for n in names]
        tips["tip"] = None
        _run(installed_app_tips.refresh_installed_tips(_reg(), [_row(n) for n in names]))
        assert len(tips["calls"]) == 2


class TestWhatIsWritten:
    def test_a_manifest_for_another_app_is_not_cached(self, tips):
        tips["tip"] = {"name": "another-app", "version": "9.0.0"}
        row = _row()
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 0
        assert not _manifest_cache_path(row).exists()

    def test_a_failed_fetch_writes_nothing_and_does_not_raise(self, tips):
        tips["tip"] = OSError("clone failed")
        row = _row()
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 0
        assert not _manifest_cache_path(row).exists()

    def test_a_tip_still_running_at_the_budget_is_cut_off(self, tips, monkeypatch):
        monkeypatch.setattr(installed_app_tips, "BATCH_BUDGET_SECS", 0)
        never = asyncio.Event()

        async def _hang(*args, **kwargs):
            await never.wait()

        monkeypatch.setattr(reg_mod, "_fetch_app_manifest", _hang)
        row = _row()
        assert _run(installed_app_tips.refresh_installed_tips(_reg(), [row])) == 0
        assert not _manifest_cache_path(row).exists()


class TestRefreshReadsTheTip:
    @pytest.mark.asyncio
    async def test_the_tip_survives_the_refreshs_own_expiry_and_reaches_update_status(
        self, tips, monkeypatch
    ):
        reg = MagicMock()
        reg.name = REGISTRY
        reg.repo = "https://github.com/acme/apps"
        reg.branch = "main"
        config = MagicMock()
        config.registries = [reg]
        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", lambda: config)
        monkeypatch.setattr(
            "kiro_crew.apps.registry._fetch_external_registry_index",
            AsyncMock(return_value=[{k: v for k, v in _row().items() if k != "_registry"}]),
        )
        monkeypatch.setattr(
            "kiro_crew.apps.registry.list_registry", AsyncMock(return_value=[{"name": "cool-app"}])
        )

        result = await refresh_registries()

        assert result["refreshed"] == [REGISTRY]
        assert tips["calls"] and tips["calls"][0]["app_name"] == ""
        # Written after the refresh expired the manifest caches: the TTL-checked
        # read returns it, which an expired file would not.
        assert _read_manifest_cache(_row())["version"] == "1.2.0"
        detail = update_status.attach_update("cool-app", _installed())
        assert detail["update"]["available"] is True
        assert detail["update"]["latestVersion"] == "1.2.0"
