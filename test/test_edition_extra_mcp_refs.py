"""An edition-supplied MCP server reaches ``tools`` once, however late it arrives.

``McpToolingProvider.extra_mcp_servers()`` can start returning a server after the
first install (its program is installed later, or a newer edition adds one). The
rebuild seeds the entry into ``mcpServers``; these tests pin that its ``@name``
ref follows once, and that a ref the user then removes stays removed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew import agent
from kiro_crew.agent_files import AGENT_FILENAME
from kiro_crew.agent_materialization import guide_platform
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

EXTRA = "edition-late"
EXTRA_REF = f"@{EXTRA}"


@pytest.fixture()
def rebuildable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An agents dir a full ``rebuild_agent_config`` can write into."""
    directory = tmp_path / "agents"
    directory.mkdir()
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: directory)
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", directory)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: tmp_path / "mates" / "agents")
    monkeypatch.setattr(
        "kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: SPEC_PERMISSIONS_MIN_VERSION
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    launcher = bindir / "kirocrew"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setattr(agent, "_KIROCREW_BIN", str(launcher))
    monkeypatch.setattr(agent, "_KIRO_MCP_JSON", tmp_path / "kiro-global-mcp.json")
    monkeypatch.setattr(agent, "_DEFAULT_KIRO_HOOKS_DIR", tmp_path / "hooks")
    monkeypatch.setattr("kiro_crew.apps.bridges._mcp_json_path", lambda: directory / AGENT_FILENAME)
    return directory


def _supply(monkeypatch: pytest.MonkeyPatch, extras: dict[str, dict[str, Any]]) -> None:
    monkeypatch.setattr(
        agent, "_extra_mcp_servers", lambda: {k: dict(v) for k, v in extras.items()}
    )


def _extra_spec() -> dict[str, Any]:
    return {"command": agent._KIROCREW_BIN, "args": ["mcp-late"]}


def _spec(agents_dir: Path) -> dict[str, Any]:
    return json.loads((agents_dir / AGENT_FILENAME).read_text(encoding="utf-8"))


def test_a_server_supplied_after_install_gains_its_ref_once(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _supply(monkeypatch, {})
    agent.rebuild_agent_config()
    assert EXTRA_REF not in _spec(rebuildable)["tools"]

    _supply(monkeypatch, {EXTRA: _extra_spec()})
    agent.rebuild_agent_config()
    spec = _spec(rebuildable)
    assert EXTRA in spec["mcpServers"]
    assert EXTRA_REF in spec["tools"]
    # Mounted, never pre-approved: its calls still reach the PreToolUse gate.
    assert EXTRA_REF not in spec["allowedTools"]
    marker = json.loads((agent.config_dir() / guide_platform.EDITION_REFS_MARKER).read_text())
    assert marker["servers"] == [EXTRA]


def test_a_ref_the_user_removed_stays_removed(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _supply(monkeypatch, {})
    agent.rebuild_agent_config()
    _supply(monkeypatch, {EXTRA: _extra_spec()})
    agent.rebuild_agent_config()
    spec = _spec(rebuildable)
    spec["tools"].remove(EXTRA_REF)
    (rebuildable / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    agent.rebuild_agent_config()
    spec = _spec(rebuildable)
    assert EXTRA in spec["mcpServers"]
    assert EXTRA_REF not in spec["tools"]


def test_a_fresh_install_refs_the_server_and_records_it(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _supply(monkeypatch, {EXTRA: _extra_spec()})
    agent.rebuild_agent_config()
    spec = _spec(rebuildable)
    assert EXTRA_REF in spec["tools"]
    assert EXTRA_REF not in spec["allowedTools"]
    # Recorded, so a later removal is not undone by the existing-spec grant.
    spec["tools"].remove(EXTRA_REF)
    (rebuildable / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")
    agent.rebuild_agent_config()
    assert EXTRA_REF not in _spec(rebuildable)["tools"]


def test_a_gated_off_server_gains_no_ref_and_stays_unrecorded(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.agent_materialization import managed_mcp

    _supply(monkeypatch, {})
    agent.rebuild_agent_config()
    _supply(monkeypatch, {EXTRA: _extra_spec()})
    monkeypatch.setattr(managed_mcp, "_gated_off_servers", lambda: frozenset({EXTRA}))
    agent.rebuild_agent_config()
    assert EXTRA_REF not in _spec(rebuildable)["tools"]
    assert not (agent.config_dir() / guide_platform.EDITION_REFS_MARKER).exists()


def test_a_server_narrowed_to_tool_refs_keeps_them_without_a_record(
    rebuildable: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _supply(monkeypatch, {})
    agent.rebuild_agent_config()
    marker_path = agent.config_dir() / guide_platform.EDITION_REFS_MARKER
    marker_path.unlink(missing_ok=True)
    narrowed = f"{EXTRA_REF}/some_tool"
    spec = _spec(rebuildable)
    spec["tools"].append(narrowed)
    (rebuildable / AGENT_FILENAME).write_text(json.dumps(spec), encoding="utf-8")

    _supply(monkeypatch, {EXTRA: _extra_spec()})
    agent.rebuild_agent_config()
    tools = _spec(rebuildable)["tools"]
    assert EXTRA_REF not in tools
    assert tools.count(narrowed) == 1
    assert json.loads(marker_path.read_text())["servers"] == [EXTRA]
