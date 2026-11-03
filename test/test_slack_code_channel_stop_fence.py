"""Slack's Stop leaves work already in flight sending nothing more.

A native Stop (or ``!stop``) marks the channel's stop fence. A tool call, a
wrap-up and a command sync are admitted at the fence when they start, and
``SurfaceClient._call`` re-checks it right before every send, so a Stop landing
in any await of theirs leaves Slack untouched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from slack_code_channel_surface_helpers import (  # noqa: F401 - fixtures by name
    CH,
    SESSION,
    _bind,
    _calls,
    _run,
    env_fixture,
)
from slack_code_channels_helpers import OWNER

from kiro_crew.slack.code_channels import routing, surface, tools


@pytest.fixture(autouse=True)
def _stopping_slot(_floor_monkeypatch: Any) -> None:
    """The dashboard's own stop is out of scope here: the fence is the subject."""
    from kiro_crew.dashboard import chat_handlers

    async def stop_slot_turn(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"info": "stopping"}

    _floor_monkeypatch.setattr(chat_handlers, "stop_slot_turn", stop_slot_turn)


async def _stop(env: Any) -> None:
    """The Stop handler's own path (idle slot here: it still marks the fence)."""
    await routing._stop_bound(
        env.orch, env.store.get_binding(CH), OWNER, CH, source="test.stop", say_idle=False
    )


def test_a_stop_during_caller_resolution_blocks_the_rename(env: Any, monkeypatch: Any) -> None:
    _bind(env)
    real = tools.resolve_caller

    async def resolve(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        await _stop(env)  # the person presses Stop while the call resolves
        return out

    monkeypatch.setattr(tools, "resolve_caller", resolve)
    out = _run(env, tools.RENAME, {"title": "new title"})
    assert not out.ok and out.code == "stopped"  # run_tool's own stopped check
    assert _calls(env, "agents.sessions.rename") == []


def test_a_stop_during_the_html_redaction_blocks_set_view(env: Any, monkeypatch: Any) -> None:
    _bind(env)
    env.api.responses["agents.conversations.setView"] = {"ok": True, "view_id": "V1"}
    real = tools._redact_html

    def redact(content: str) -> str:
        # Runs off the loop (to_thread): the Stop lands meanwhile. Its first
        # step, the fence mark, is all the fence reads (the handler path itself
        # is the rename test above).
        surface.mark_stopped(CH)
        return real(content)

    monkeypatch.setattr(tools, "_redact_html", redact)
    out = asyncio.run(
        tools.run_tool(env.state, SESSION, tools.SET_VIEW, {"view_key": "r", "content": "<p>x</p>"})
    )
    assert not out.ok and out.code == "stopped_by_person"  # refused at the send
    assert _calls(env, "agents.conversations.setView") == []


def test_a_command_sync_after_a_stop_in_the_same_turn_sends_nothing(env: Any) -> None:
    from kiro_crew.slack.code_channels import commands

    binding = _bind(env)
    asyncio.run(surface.note_turn_start(binding, None))
    asyncio.run(_stop(env))  # pressed while an earlier start hook ran
    asyncio.run(commands.sync_commands(binding))
    assert _calls(env, "agents.conversations.setCommands") == []


def test_the_refresh_after_a_stopped_turn_sends_nothing(env: Any, monkeypatch: Any) -> None:
    from kiro_crew.slack.code_channels.workspace.service import WorkspaceService

    binding = _bind(env)
    asyncio.run(surface.note_turn_start(binding, None))
    asyncio.run(_stop(env))
    ran: list[int] = []

    async def inner(self: Any, _b: Any) -> None:
        ran.append(1)

    monkeypatch.setattr(WorkspaceService, "_refresh", inner)
    asyncio.run(WorkspaceService.refresh(object.__new__(WorkspaceService), binding))
    assert ran == []


def test_work_admitted_after_the_stop_still_sends(env: Any) -> None:
    _bind(env)
    asyncio.run(_stop(env))
    assert _run(env, tools.RENAME, {"title": "after"}).ok
    assert _calls(env, "agents.sessions.rename") == [{"channel_id": CH, "title": "after"}]


def test_a_new_turn_is_not_stopped_by_an_earlier_stop(env: Any) -> None:
    binding = _bind(env)
    asyncio.run(_stop(env))
    asyncio.run(surface.note_turn_start(binding, None))
    assert not surface.channel_stopped(CH)


def test_the_stopped_turn_counts_as_stopped_until_the_next_one(env: Any) -> None:
    binding = _bind(env)
    asyncio.run(surface.note_turn_start(binding, None))
    asyncio.run(_stop(env))
    assert surface.channel_stopped(CH)
