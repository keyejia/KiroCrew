"""What the KAS projection sends when the engine announces ``mcpServerOAuth`` or
``mcpRegistryPointers`` in ``initialize``, and that it sends nothing new otherwise.

The initialize answers here are stubs shaped like the engine's; no engine runs.
"""

from __future__ import annotations

import logging

import pytest
from kas_agents_helpers import _spec

from kiro_crew.acp import kas_agents
from kiro_crew.acp import kas_engine_features as features
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_agents import to_client_custom_agent

_SECRET = "placeholder-client-secret-value"
_CLIENT_ID = "placeholder-client-id"
_MANAGED = sorted(kas_agents.MANAGED_MCP_SERVER_NAMES)[0]


def _initialize(**kiro: object) -> dict:
    """A stub ``initialize`` result carrying *kiro* under ``agentCapabilities._meta``."""
    return {
        "protocolVersion": 1,
        "agentCapabilities": {"loadSession": True, "_meta": {"kiro": kiro}},
    }


def _caps(**kiro: object) -> dict:
    return _initialize(**kiro)["agentCapabilities"]


OAUTH = features.advertised(_caps(mcpServerOAuth=True))
POINTERS = features.advertised(_caps(mcpRegistryPointers=True))


@pytest.fixture(autouse=True)
def _not_registry_governed(_floor_monkeypatch):
    _floor_monkeypatch.setattr(kas_agents, "_registry_governed", lambda: False)


def _remote(**oauth_extra: object) -> dict:
    oauth = {
        "clientId": _CLIENT_ID,
        "clientSecret": _SECRET,
        "redirectUri": "http://localhost:8080/callback",
        "oauthScopes": ["channels:history"],
    }
    oauth.update(oauth_extra)
    return _spec(
        mcpServers={
            "remote": {"type": "http", "url": "https://mcp.example.com/mcp", "oauth": oauth}
        }
    )


class TestAdvertised:
    @pytest.mark.parametrize(
        "caps",
        [
            None,
            {},
            {"_meta": None},
            {"_meta": {"kiro": None}},
            _caps(mcpServerOAuth="true", mcpRegistryPointers=1),
            _caps(somethingElse=True),
        ],
    )
    def test_only_a_literal_true_counts(self, caps):
        assert features.advertised(caps) == frozenset()

    def test_both_flags_are_read(self):
        caps = _caps(mcpServerOAuth=True, mcpRegistryPointers=True)
        assert features.advertised(caps) == {
            features.MCP_SERVER_OAUTH,
            features.MCP_REGISTRY_POINTERS,
        }

    def test_a_handle_is_read_through_its_initialize_result(self):
        class Handle:
            initialize_result = _initialize(mcpServerOAuth=True)

        assert features.advertised_by(Handle()) == OAUTH
        assert features.advertised_by(object()) == frozenset()


class TestOAuth:
    def test_without_the_flag_the_block_is_withheld_as_before(self):
        out = to_client_custom_agent("a", _remote(), "p")
        assert "oauth" not in out["mcpServers"]["remote"]
        assert "oauthScopes" not in out["mcpServers"]["remote"]

    def test_with_the_flag_the_accepted_keys_are_sent(self):
        out = to_client_custom_agent("a", _remote(), "p", engine_features=OAUTH)
        entry = out["mcpServers"]["remote"]
        assert entry["oauth"] == {
            "clientId": _CLIENT_ID,
            "redirectUri": "http://localhost:8080/callback",
        }
        assert entry["oauthScopes"] == ["channels:history"]

    def test_client_metadata_url_is_sent_and_unknown_keys_are_not(self):
        out = to_client_custom_agent(
            "a",
            _remote(clientMetadataUrl="https://app.example/client.json", extra="x"),
            "p",
            engine_features=OAUTH,
        )
        oauth = out["mcpServers"]["remote"]["oauth"]
        assert oauth["clientMetadataUrl"] == "https://app.example/client.json"
        assert "extra" not in oauth

    def test_the_client_secret_never_reaches_the_wire_or_the_log(self, caplog):
        with caplog.at_level(logging.DEBUG):
            out = to_client_custom_agent("a", _remote(), "p", engine_features=OAUTH)
        assert _SECRET not in repr(out)
        assert "clientSecret" not in out["mcpServers"]["remote"]["oauth"]
        assert _SECRET not in caplog.text
        assert "clientSecret is not sent" in caplog.text

    def test_the_registry_flag_alone_does_not_send_oauth(self):
        out = to_client_custom_agent("a", _remote(), "p", engine_features=POINTERS)
        assert "oauth" not in out["mcpServers"]["remote"]


class TestRegistryPointers:
    @staticmethod
    def _governed(monkeypatch) -> None:
        monkeypatch.setattr(kas_agents, "_registry_governed", lambda: True)

    @staticmethod
    def _marked() -> dict:
        return _spec(
            mcpServers={
                "governed": {"command": "x", "type": "registry", "env": {"K": "v"}},
                "plain": {"command": "y"},
            }
        )

    def test_without_the_flag_a_marked_entry_is_withheld_as_before(self):
        out = to_client_custom_agent("a", self._marked(), "p")
        assert out["mcpServers"] == {"plain": {"command": "y"}}

    def test_with_the_flag_a_marked_entry_is_sent_as_a_bare_pointer(self):
        out = to_client_custom_agent("a", self._marked(), "p", engine_features=POINTERS)
        assert out["mcpServers"]["governed"] == {"type": "registry"}
        assert out["mcpServers"]["plain"] == {"command": "y"}

    def test_registry_mode_still_withholds_an_unmarked_third_party_entry(self, monkeypatch):
        self._governed(monkeypatch)
        out = to_client_custom_agent("a", self._marked(), "p", engine_features=POINTERS)
        assert out["mcpServers"] == {"governed": {"type": "registry"}}

    def test_the_no_slot_warning_is_not_logged_when_the_engine_has_one(self, monkeypatch, caplog):
        self._governed(monkeypatch)
        spec = _spec(mcpServers={_MANAGED: {"command": "kirocrew", "args": ["mcp"]}})
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent("a", spec, "p", engine_features=POINTERS)
        assert "no slot" not in caplog.text
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent("a", spec, "p")
        assert "no slot" in caplog.text


class TestSessionArray:
    @staticmethod
    def _array() -> list[dict]:
        return [
            {"name": _MANAGED, "command": "kirocrew", "args": ["mcp"], "env": []},
            {"name": "third-party", "command": "tp", "args": [], "env": []},
            {"name": "remote", "type": "http", "url": "https://x.example", "headers": []},
        ]

    def test_without_the_flag_the_array_is_the_same_object(self):
        array = self._array()
        assert KasHarness().session_mcp_servers(array, agent_capabilities=_caps()) is array

    def test_with_the_flag_only_crews_own_launched_servers_are_marked(self):
        array = self._array()
        out = KasHarness().session_mcp_servers(
            array, agent_capabilities=_caps(mcpRegistryPointers=True)
        )
        assert out[0]["_meta"] == {"kiro": {"type": "registry"}}
        assert out[0]["command"] == "kirocrew" and out[0]["args"] == ["mcp"]
        assert out[1:] == array[1:]
        assert "_meta" not in array[0], "the caller's element must not be mutated"


class TestRuntimeSeam:
    """The runtime hands the harness exactly what its own handshake announced."""

    @staticmethod
    async def _seen(caps: dict) -> frozenset[str]:
        from types import SimpleNamespace

        from kiro_crew.acp import runtime_session_setup
        from kiro_crew.acp.harness import SessionExtras

        seen: dict = {}

        async def session_extras(agent, **kw):
            seen.update(kw)
            return SessionExtras()

        runtime = SimpleNamespace(
            _agent_capabilities=caps,
            _harness=SimpleNamespace(session_extras=session_extras),
            _mcp_gateway_overlay=None,
            _work_dir=None,
            _refuse_if_loader_unreachable=lambda *_a: None,
        )
        await runtime_session_setup._kas_custom_agents(runtime, "a")
        return seen["engine_features"]

    @pytest.mark.asyncio
    async def test_the_handshake_flags_reach_session_extras(self):
        assert await self._seen(_caps(mcpServerOAuth=True)) == OAUTH

    @pytest.mark.asyncio
    async def test_an_engine_that_announces_nothing_hands_nothing(self):
        assert await self._seen({}) == frozenset()


class TestOAuthNotice:
    """The "sign-in will not work here" notice follows what this engine announced."""

    @staticmethod
    def _discards(monkeypatch, initialize_result: dict) -> bool:
        import asyncio
        from types import SimpleNamespace

        from kiro_crew.acp.types import ACP_BACKEND_KAS
        from kiro_crew.agent_sdk import spec_hooks
        from kiro_crew.agent_sdk.capabilities import capabilities_for
        from kiro_crew.dashboard import chat_runner

        seen: dict = {}

        def lost(_spec, **kw):
            seen.update(kw)
            return []

        monkeypatch.setattr(spec_hooks, "_agent_spec_with_origin", lambda *_a: ({}, "user"))
        monkeypatch.setattr(kas_agents, "spec_keys_without_carrier", lost)
        client = SimpleNamespace(
            capabilities=capabilities_for(ACP_BACKEND_KAS),
            cwd="",
            initialize_result=initialize_result,
        )
        asyncio.run(
            chat_runner._prepare_spec_hooks(
                SimpleNamespace(), SimpleNamespace(), client, "a", is_new=False
            )
        )
        return seen["discards_mcp_oauth"]

    def test_an_engine_without_the_flag_still_gets_the_notice(self, monkeypatch):
        assert self._discards(monkeypatch, _initialize()) is True

    def test_an_engine_that_keeps_oauth_gets_no_notice(self, monkeypatch):
        assert self._discards(monkeypatch, _initialize(mcpServerOAuth=True)) is False
