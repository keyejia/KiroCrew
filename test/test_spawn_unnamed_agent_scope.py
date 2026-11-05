"""A spawn that names no agent and inherits none answers the ``agents`` scope.

The empty name is final there, so an allow-list of agent names refuses it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from subagent_continuable_helpers import _manager, _mock_sessions


def _permits_only_named(scope, item="", **kw):
    if scope == "capabilities.spawn" and item == "agents:":
        return SimpleNamespace(permitted=False, reason="agent not in spawn scope")
    return SimpleNamespace(permitted=True, reason="")


@pytest.mark.asyncio
async def test_an_unnamed_spawn_is_refused_by_a_named_allow_list(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.governance_permits", _permits_only_named
    )
    sessions = _mock_sessions()
    manager = _manager(sessions)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn("task")
        assert info is not None
        await manager._tasks[info.id]
    assert "unnamed agent is not permitted" in (info.error or ""), info.error
    sessions.get_or_create.assert_not_called()


@pytest.mark.asyncio
async def test_an_unnamed_spawn_runs_when_the_scope_permits(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.governance_permits",
        lambda scope, item="", **kw: SimpleNamespace(permitted=True, reason=""),
    )
    sessions = _mock_sessions()
    manager = _manager(sessions)
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn("task")
        assert info is not None and not info.error
        await manager._tasks[info.id]
    assert not info.error
