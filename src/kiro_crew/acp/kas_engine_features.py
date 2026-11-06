"""What the KAS engine says it accepts on the wire, and what Crew sends because of it.

An engine that announces a flag in ``initialize``'s ``agentCapabilities._meta.kiro``
takes a field older engines dropped. Each flag here turns on exactly one change
to what Crew projects; with the flag absent the projection is byte-identical to
what it was before the flag existed, so an older engine sees no difference.

- :data:`MCP_SERVER_OAUTH`: a remote MCP server's ``oauth`` block survives
  ``customAgents``. Crew then sends ``clientId``, ``redirectUri`` and
  ``clientMetadataUrl``, and ``oauthScopes`` beside them, instead of withholding
  the whole block. ``clientSecret`` is never sent over ACP: the engine does not
  accept it and keeps the raw ``customAgents`` payload in its session file.
- :data:`MCP_REGISTRY_POINTERS`: a ``{"type": "registry"}`` pointer survives both
  ``customAgents`` and the session ``mcpServers`` array (as ``_meta.kiro.type``).
  Crew then sends a user entry that carries the registry marker as a pointer
  instead of withholding it, and marks its own launched servers in the session
  array so a registry-governed account resolves them from its catalog. Outside
  registry access mode the engine runs a pointer's own command, so an ordinary
  account keeps launching them as before.

Only a literal ``True`` counts, like ``kas_wire.CAPABILITY_CHECKPOINTS``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from kiro_crew.mcp_cleanup import MCP_REGISTRY_TYPE

#: ``agentCapabilities._meta.kiro`` flag: ``customAgents`` keeps ``oauth``.
MCP_SERVER_OAUTH = "mcpServerOAuth"
#: ``agentCapabilities._meta.kiro`` flag: registry pointers survive the wire.
MCP_REGISTRY_POINTERS = "mcpRegistryPointers"
_KNOWN = (MCP_SERVER_OAUTH, MCP_REGISTRY_POINTERS)

#: The ``oauth`` keys the engine accepts over ACP. ``clientSecret`` is not one.
OAUTH_WIRE_KEYS = ("clientId", "redirectUri", "clientMetadataUrl")
_SCOPES_KEY = "oauthScopes"
_SECRET_KEY = "clientSecret"


def advertised(agent_capabilities: Any) -> frozenset[str]:
    """The flags of this module that *agent_capabilities* sets to literal ``True``."""
    meta = agent_capabilities.get("_meta") if isinstance(agent_capabilities, Mapping) else None
    kiro = meta.get("kiro") if isinstance(meta, Mapping) else None
    if not isinstance(kiro, Mapping):
        return frozenset()
    return frozenset(flag for flag in _KNOWN if kiro.get(flag) is True)


def advertised_by(handle: object) -> frozenset[str]:
    """:func:`advertised` for a session handle's ``initialize`` answer, else empty."""
    result = getattr(handle, "initialize_result", None)
    caps = result.get("agentCapabilities") if isinstance(result, Mapping) else None
    return advertised(caps)


def _scopes(value: Any) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(s, str) for s in value):
        return list(value)
    return None


def carry_oauth(
    projected: dict[str, Any], *, agent_id: str, name: str, logger: logging.Logger
) -> None:
    """Narrow *projected*'s ``oauth`` block to what the engine accepts over ACP.

    Keeps :data:`OAUTH_WIRE_KEYS` with string values, and moves a nested
    ``oauth.oauthScopes`` to the entry's own ``oauthScopes``, where it wins over a
    top-level list exactly as kiro-cli reads it. Everything else in the block is
    dropped. A ``clientSecret`` costs one log line naming the field, never its value.
    """
    oauth = projected.pop("oauth", None)
    if not isinstance(oauth, Mapping):
        return
    if oauth.get(_SECRET_KEY):
        logger.info(
            "agent %r: MCP server %r: clientSecret is not sent to this backend", agent_id, name
        )
    kept = {k: oauth[k] for k in OAUTH_WIRE_KEYS if isinstance(oauth.get(k), str) and oauth[k]}
    nested = _scopes(oauth.get(_SCOPES_KEY))
    if nested is not None:
        projected[_SCOPES_KEY] = nested
    elif _SCOPES_KEY in projected and _scopes(projected[_SCOPES_KEY]) is None:
        projected.pop(_SCOPES_KEY)
    if kept:
        projected["oauth"] = kept


def registry_pointer() -> dict[str, Any]:
    """The ``customAgents`` entry that defers a server to the account's catalog."""
    return {"type": MCP_REGISTRY_TYPE}


def mark_registry_pointers(
    servers: list[dict[str, Any]], names: Iterable[str]
) -> list[dict[str, Any]]:
    """*servers* with each launched element named in *names* marked as a pointer.

    The element keeps its own command, which the engine runs outside registry
    access mode. A new list of new dicts: the caller's elements are not mutated.
    """
    wanted = frozenset(names)
    out: list[dict[str, Any]] = []
    for element in servers:
        if isinstance(element, dict) and element.get("name") in wanted and element.get("command"):
            meta = dict(element.get("_meta") or {})
            meta["kiro"] = {**(meta.get("kiro") or {}), "type": MCP_REGISTRY_TYPE}
            element = {**element, "_meta": meta}
        out.append(element)
    return out
