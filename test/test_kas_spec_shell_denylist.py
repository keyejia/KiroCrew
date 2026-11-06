"""A KAS spec's ``toolsSettings.shell.deniedCommands`` is enforced at Crew's gate.

The KAS translation carries an entry as an engine rule only when it is a literal
plus ``.*``. The projection records every entry, the session handle stamps them on
its own agent's requests, and the gate's ``spec-shell-denylist`` row refuses a
match on every approval policy, so a regex entry (``git\\s+push.*``) also holds for
a scheduled job with ``approval_mode: auto``. An entry nobody can evaluate keeps
every shell call for a person: asked interactively, refused unattended.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_kas_spec_builtin_grant import _handle, _permission
from test_tool_call_classification_kas import _kas_builtin_flow

from kiro_crew import permission_floor
from kiro_crew.acp import kas_agents
from kiro_crew.acp._dispatch import attach_kas_custom_agents
from kiro_crew.acp.kas_permissions import CREW_SHELL_DENYLIST_KEY
from kiro_crew.acp.kas_shell_denylist import projected_shell_denylist
from kiro_crew.acp.types import AcpEvent
from kiro_crew.acp_backends import ACP_BACKEND_KAS
from kiro_crew.dashboard.chat_turn.tool_approval import _spec_keys_notice
from kiro_crew.hooks import TOOL_ALLOW, TOOL_DENY, HookManager, HooksConfig, hook_gate_kwargs
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp

_DENIED = [r"git\s+push.*", r"npm (publish|unpublish).*", "make clean"]


@pytest.fixture(autouse=True)
def _no_profiles(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


def _spec(denied: Any = _DENIED, **extra: Any) -> dict[str, Any]:
    return {
        "name": "probe",
        "prompt": "p",
        "tools": ["execute_bash"],
        "toolsSettings": {"shell": {"deniedCommands": denied}},
        **extra,
    }


def _own_request(command: str, spec: dict[str, Any] | None = None) -> AcpEvent:
    """The permission event the session handle builds for its own agent's shell call."""
    entry = kas_agents.to_client_custom_agent("probe", spec or _spec(), "p")
    handle = _handle(ACP_BACKEND_KAS)
    handle.kas_spec_shell_denylist = projected_shell_denylist([entry], "probe")
    stamped = handle._build_permission_event(_permission("own-sid"))
    assert stamped is not None
    event = _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )
    event.spec_shell_denylist = stamped.spec_shell_denylist
    return event


def _judge(event: AcpEvent):
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))


def _auto(event: AcpEvent) -> str | None:
    """What an ``AUTO_APPROVE`` job's floor answers: a reason refuses, ``None`` runs."""
    return permission_floor.refusal_for(event, session_key="cron:probe", security_only=False)


# ── a regex entry KAS never received ──────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "git push origin feature",
        "git\tpush origin feature",
        "cd repo && git  push origin feature",
        "echo $(npm publish --tag next)",
        "ls; make clean",
    ],
)
def test_a_denied_command_is_refused_interactive_and_under_auto_approve(command: str) -> None:
    event = _own_request(command)
    verdict = _judge(event)
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-denylist")
    reason = _auto(event)
    assert reason is not None and "deniedCommands" in reason


@pytest.mark.parametrize(
    "command", ["git status", "npm test", "make build", "git log --format=push"]
)
def test_a_command_no_entry_names_is_unchanged(command: str) -> None:
    event = _own_request(command)
    assert _judge(event).tier != "spec-shell-denylist"
    assert _auto(event) is None


# ── a literal entry KAS did receive ───────────────────────────────────────────


def test_a_literal_deny_still_reaches_kas_as_a_rule_and_crew_refuses_it_too() -> None:
    entry = kas_agents.to_client_custom_agent("probe", _spec(["git push.*"]), "p")
    rules = entry["permissions"]["rules"]
    assert {"capability": "shell", "match": ["git push*"], "effect": "deny"} in rules
    event = _own_request("git push origin x", _spec(["git push.*"]))
    assert _judge(event).tier == "spec-shell-denylist"


# ── an entry outside Crew's linear reader: the user's own pattern, via re ─────


@pytest.mark.parametrize(
    ("denied", "command"),
    [
        (r"\bgit push.*", "git push origin feature"),
        (r"rm -r{1,2}f .*", "rm -rrf build"),
        (r"(?i)GIT PUSH.*", "git push origin feature"),
        (r"git\s+push.*", "git\u00a0push origin feature"),
    ],
)
def test_an_entry_crew_s_reader_refuses_is_still_enforced(denied: str, command: str) -> None:
    event = _own_request(command, _spec([denied]))
    assert (_judge(event).action, _judge(event).tier) == (TOOL_DENY, "spec-shell-denylist")
    assert _auto(event) is not None


def test_a_re_entry_is_anchored_like_kiro_cli() -> None:
    """kiro-cli compiles ``\\A{entry}\\z``: ``(?i)rm`` denies ``rm`` alone, not ``rm x``."""
    assert _judge(_own_request("rm x", _spec(["(?i)rm"]))).tier != "spec-shell-denylist"
    assert _judge(_own_request("RM", _spec(["(?i)rm"]))).tier == "spec-shell-denylist"


# ── an entry nobody can evaluate: a person decides, nothing runs unattended ──


@pytest.mark.parametrize(
    "denied",
    [["^(?!ls$).*"], ["git push (unclosed"], ["[[:alpha:]]+"], [r"(a)\1"], [7], "x"],
    ids=["look-ahead", "invalid", "posix-class", "backreference", "not-a-string", "not-a-list"],
)
def test_an_unevaluable_entry_asks_interactively_and_refuses_under_auto(denied: Any) -> None:
    event = _own_request("git status", _spec(denied))
    verdict = _judge(event)
    assert (verdict.action, verdict.tier) == (TOOL_ALLOW, "spec-shell-denylist")
    assert verdict.human_only and "never runs unattended" in verdict.reason
    reason = _auto(event)
    assert reason is not None and "never runs unattended" in reason


@pytest.mark.asyncio
async def test_the_shared_approver_never_auto_approves_a_person_only_call() -> None:
    """Hook-based surface with no approver: refused. With an approver: marked, so
    no trust or YOLO shortcut on it may answer."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission

    event = _own_request("git status", _spec(["^(?!ls$).*"]))
    event.request_id = "r1"
    provider = MagicMock(reject_tool=AsyncMock(), approve_tool=AsyncMock(return_value=True))
    hooks = HookManager(HooksConfig())
    ok = await _resolve_permission(
        provider, event, ToolApprovalPolicy.HOOK_BASED, hooks, None, session_key="s"
    )
    assert ok is False and provider.reject_tool.await_count == 1
    seen: list[bool] = []

    async def approver(ev: Any) -> bool:
        seen.append(ev.needs_person)
        return False

    await _resolve_permission(
        provider, event, ToolApprovalPolicy.HOOK_BASED, hooks, approver, session_key="s"
    )
    assert seen == [True]


# ── plumbing and notice ───────────────────────────────────────────────────────


def test_no_denied_commands_means_no_record_and_no_refusal() -> None:
    spec = {"name": "probe", "prompt": "p", "tools": ["execute_bash"]}
    entry = kas_agents.to_client_custom_agent("probe", spec, "p")
    assert CREW_SHELL_DENYLIST_KEY not in entry
    event = _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": "npm publish x"}
    )
    assert _judge(event).action == TOOL_ALLOW


def test_the_record_never_reaches_the_wire() -> None:
    entry = kas_agents.to_client_custom_agent("probe", _spec(), "p")
    assert entry[CREW_SHELL_DENYLIST_KEY] == _DENIED
    params: dict[str, Any] = {}
    attach_kas_custom_agents(params, [entry])
    (wire,) = params["_meta"]["kiro"]["customAgents"]
    assert CREW_SHELL_DENYLIST_KEY not in wire


def test_the_notice_names_each_dropped_entry_and_what_crew_does() -> None:
    spec = _spec([r"git\s+push.*", "make clean", "^(?!ls$).*"])
    lost = kas_agents.spec_keys_without_carrier(spec, translates_tools_settings=True)
    assert not [k for k in lost if k.endswith(".deniedCommands")]
    notice = _spec_keys_notice("probe", lost)
    assert "'git\\\\s+push.*'" in notice and "Kiro Crew enforces them itself" in notice
    assert "'make clean'" not in notice, "a literal entry reaches KAS as a rule"
    assert "'^(?!ls$).*'" in notice and "none runs unattended" in notice
    assert "no effect" not in notice


@pytest.mark.asyncio
async def test_a_trusted_slot_still_asks_a_person_for_a_person_only_call() -> None:
    """The gateway approver's trust shortcut must not answer a call the gate kept
    for a person: it reaches the human prompt instead."""
    from unittest.mock import patch

    from test_scoped_trust_reaches_children import _SO, _event, _orchestrator, _slot

    from kiro_crew.slack import gateway as gw

    orch = _orchestrator(_slot(trust=True))
    callback = orch._interactive_approval("subagent", slot_resolver=lambda _rid: "crew-slot")
    event = _event()
    event.needs_person = True
    with (
        patch.object(_SO, "is_scope_active", return_value=False),
        patch.object(_SO, "is_active", return_value=False),
        patch("kiro_crew.slack.handler.is_yolo_mode", return_value=False),
        patch.object(gw, "sel"),
    ):
        approved = await callback(event, "")
    assert approved is False
    orch.dashboard_state.request_approval.assert_awaited_once()
