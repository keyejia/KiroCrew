"""Settings -> Advanced: which config keys the page lists, and the generic PATCH rule."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.schema import SCHEMA_REGISTRY
from kiro_crew.dashboard.handlers import config_all_settings as all_settings
from kiro_crew.dashboard.handlers.core_config_schema import _EDITABLE_CONFIG, _editable_config_spec

_BY_PATH = {e.path: e for e in SCHEMA_REGISTRY}

pytestmark = pytest.mark.asyncio


@web.middleware
async def _owner_identity(request, handler):
    request["user"] = "local-app"
    request["app"] = ""
    request.app["state"].owner_id = ""
    return await handler(request)


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_config_schema, api_kirocrew_config_patch

    app = web.Application(middlewares=[_owner_identity])
    app["state"] = MagicMock()
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    app.router.add_get("/api/config/schema", api_config_schema)
    return app


@pytest.fixture
def tmp_config(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"mcp_gateway": {"idle_timeout_secs": 120}}), encoding="utf-8")
    with patch("kiro_crew.config.loader.config_path", return_value=cfg_path):
        yield cfg_path


def _stored(cfg_path) -> dict:
    return json.loads(cfg_path.read_text(encoding="utf-8"))


async def _send(body: dict) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.patch("/api/config/kirocrew", json=body)
        return resp.status, await resp.json()


# ── Which keys are listed, and how ──


def test_a_secret_a_section_and_a_map_are_not_listed() -> None:
    sensitive = next(e for e in SCHEMA_REGISTRY if e.sensitive)
    assert all_settings.descriptor(sensitive) is None
    assert all_settings.descriptor(_BY_PATH["mcp_gateway"]) is None
    assert all_settings.descriptor(_BY_PATH["agent.env"]) is None


def test_the_motivating_list_key_is_editable_and_resettable() -> None:
    record = all_settings.descriptor(_BY_PATH["mcp_gateway.keep_alive_servers"])
    assert record is not None
    assert record["mode"] == "edit"
    assert record["control"] == "str_list"
    assert record["resettable"] is True


@pytest.mark.parametrize(
    "path",
    [
        "agent.dangerously_skip_permissions",
        "agent.sandbox",
        "tunnel.enabled",
        "slack.show_thinking",
    ],
)
def test_a_security_or_channel_key_is_listed_read_only(path: str) -> None:
    assert all_settings.descriptor(_BY_PATH[path]) == {"mode": "readonly"}
    assert _editable_config_spec(path) == _EDITABLE_CONFIG.get(path)


def test_free_text_stays_read_only_unless_named() -> None:
    assert all_settings.descriptor(_BY_PATH["agent.kas_bundle_url"]) == {"mode": "readonly"}
    assert _editable_config_spec("agent.kas_bundle_url") is None


def test_every_readonly_and_text_path_names_a_real_key() -> None:
    for path in all_settings.READONLY_PATHS | all_settings.TEXT_EDIT_PATHS:
        assert path in _BY_PATH, path


def test_no_generic_rule_reaches_the_change_card_table() -> None:
    from kiro_crew.change_card_catalog import CardCatalogError
    from kiro_crew.dashboard.change_cards_settings import resolve_setting

    assert "mcp_gateway.keep_alive_servers" not in _EDITABLE_CONFIG
    with pytest.raises(CardCatalogError):
        resolve_setting({"path": "mcp_gateway.keep_alive_servers"})


#: Words that suggest a key decides who may reach the agent or what it may do.
_SECURITY_WORDS = (
    "sandbox", "jail", "approv", "permission", "trust", "allow", "auth", "exec",
    "danger", "unsafe", "insecure", "verify", "tls", "bind", "public", "expose",
    "token", "secret", "credential", "egress", "relay", "tunnel", "tailscale",
    "tailnet", "admission", "governance", "security", "redact", "hook", "skip",
    "isolation", "origin", "quarantine", "policy", "sign_in", "owner", "share",
    "sharing", "forward", "plain_http", "shell", "command",
)  # fmt: skip

#: Keys whose names match a word above, read and judged safe to edit here.
_REVIEWED_EDITABLE = frozenset(
    {
        "agent.mcp_quarantine_after_failures",
        "agent.tool_search_min_tokens",
        "agent.max_stop_hook_nudges",
        "agent.admission_gate",
        "agent.tool_approval_timeout_secs",
        "mcp_gateway.host_budget_max_procs",
        "mcp_gateway.host_budget_max_rss_mb",
        "mcp_gateway.host_budget_max_fds",
        "instances.tunnel_base_port",
        "tunnel.relay_move_grace_seconds",
        "dashboard.tailscale.keep_awake",
        "dashboard.shared_crew.principal_concurrency",
        "dashboard.shared_crew.crew_concurrency",
        "dashboard.shared_crew.typing_idle_seconds",
    }
)


def test_a_security_sounding_key_is_editable_only_after_review() -> None:
    """A new key whose name sounds like security fails here until someone decides."""
    unreviewed = []
    for entry in SCHEMA_REGISTRY:
        record = all_settings.descriptor(entry)
        if not record or record["mode"] != "edit":
            continue
        if any(word in entry.path.lower() for word in _SECURITY_WORDS):
            if entry.path not in _REVIEWED_EDITABLE:
                unreviewed.append(entry.path)
    assert unreviewed == [], (
        "add each to READONLY_PATHS, or to _REVIEWED_EDITABLE after checking it: " f"{unreviewed}"
    )


# ── The PATCH route ──


async def test_patch_writes_and_unsets_a_generic_list(tmp_config) -> None:
    status, _ = await _send({"path": "mcp_gateway.keep_alive_servers", "value": ["github"]})
    assert status == 200
    assert _stored(tmp_config)["mcp_gateway"]["keep_alive_servers"] == ["github"]

    status, _ = await _send({"path": "mcp_gateway.keep_alive_servers", "unset": True})
    assert status == 200
    stored = _stored(tmp_config)["mcp_gateway"]
    assert "keep_alive_servers" not in stored
    assert stored["idle_timeout_secs"] == 120


@pytest.mark.parametrize("value", [True, "300", 1.5, None])
async def test_patch_refuses_a_generic_int_of_the_wrong_type(tmp_config, value) -> None:
    status, body = await _send({"path": "mcp_gateway.idle_timeout_secs", "value": value})
    assert status == 400, body
    assert _stored(tmp_config)["mcp_gateway"]["idle_timeout_secs"] == 120


@pytest.mark.parametrize("value", ["Europe/Lodnon", "PST", "../etc"])
async def test_patch_refuses_a_timezone_the_scheduler_cannot_load(tmp_config, value) -> None:
    status, body = await _send({"path": "timezone", "value": value})
    assert status == 400, body
    assert "timezone" not in _stored(tmp_config)


@pytest.mark.parametrize("value", ["Europe/London", "local", ""])
async def test_patch_stores_a_loadable_timezone(tmp_config, value) -> None:
    status, body = await _send({"path": "timezone", "value": value})
    assert status == 200, body
    assert _stored(tmp_config)["timezone"] == value


def test_a_key_another_action_owns_is_read_only() -> None:
    """A field whose help says a plain config write must not set it stays read-only."""
    owned = [
        e.path
        for e in SCHEMA_REGISTRY
        if "plain config write" in e.help
        and (all_settings.descriptor(e) or {}).get("mode") == "edit"
    ]
    assert owned == []
    assert all_settings.descriptor(_BY_PATH["memory.embedding_dim"]) == {"mode": "readonly"}


def test_every_editable_top_level_key_is_saved_at_the_top_level() -> None:
    """A field saved under another section (an alias) would take a write that changes nothing."""
    from kiro_crew.config.loader import KiroCrewConfig

    saved = KiroCrewConfig().to_dict()
    misplaced = [
        e.path
        for e in SCHEMA_REGISTRY
        if "." not in e.path
        and (all_settings.descriptor(e) or {}).get("mode") == "edit"
        and e.path not in saved
    ]
    assert misplaced == []


async def test_patch_refuses_a_generic_bool_given_a_string(tmp_config) -> None:
    status, _ = await _send({"path": "mcp_gateway.read_buffer_limit_bytes", "value": "1"})
    assert status == 400


async def test_patch_refuses_a_read_only_key(tmp_config) -> None:
    status, body = await _send({"path": "agent.dangerously_skip_permissions", "value": True})
    assert status == 400
    assert "not editable" in body["error"]
    assert "agent" not in _stored(tmp_config)


async def test_patch_refuses_unset_on_a_curated_key(tmp_config) -> None:
    status, body = await _send({"path": "session.timeout_secs", "unset": True})
    assert status == 400
    assert "unset is not allowed" in body["error"]


async def test_schema_route_carries_the_descriptor() -> None:
    async with TestClient(TestServer(_make_app())) as client:
        resp = await client.get("/api/config/schema")
        entries = {e["path"]: e for e in (await resp.json())["entries"]}
    assert entries["mcp_gateway.keep_alive_servers"]["allSettings"]["mode"] == "edit"
    assert entries["agent.dangerously_skip_permissions"]["allSettings"] == {"mode": "readonly"}
    assert "allSettings" not in entries["mcp_gateway"]


def test_a_dashboard_route_key_links_to_its_control() -> None:
    record = all_settings.descriptor(_BY_PATH["dashboard.quick_send"])
    assert record is not None and record["mode"] == "control"
    assert isinstance(record["settingId"], str) and record["settingId"]


def test_a_preview_flag_is_read_only() -> None:
    assert all_settings.descriptor(_BY_PATH["dashboard.feature_previews.webhooks"]) == {
        "mode": "readonly"
    }
