"""What the Settings -> Advanced page may show and change, derived from the config schema.

The Advanced page lists every scalar or string-list config key, so a person can
change a setting that has no control of its own without editing ``config.json``.
This module answers, per schema path:

* whether the page shows the key at all (``descriptor`` returns ``None`` for a
  secret, a retired key, a whole section, a map or a list of non-strings);
* whether the page may change it (``mode`` ``"edit"``) or only shows it
  (``"readonly"``), and with which control;
* the PATCH rule for a key that has no curated rule in ``_EDITABLE_CONFIG``
  (``generic_spec``), so ``PATCH /api/config/kirocrew`` keeps validating every
  value it stores.

Booleans, numbers and enums are editable unless :data:`READONLY_PATHS` names
them, a channel tag marks them, or this build locks them. Free text and string
lists stay read-only unless :data:`TEXT_EDIT_PATHS` names them, because a path,
a URL or a command line is where a careless value does the most harm. A key that
changes who may reach the agent, what it may run or what it may skip asking is
read-only here: its own page does the checks a plain write cannot.

A generic rule is never added to ``_EDITABLE_CONFIG``, so a change card (which
reads only that table) cannot propose one of these keys.
"""

from __future__ import annotations

import functools
from typing import Any

#: Keys the page shows but never changes. Each one either decides a security or
#: trust question (sandbox, approvals, sign-in storage, who may message the
#: agent, cross-session sharing) or is a marker the product sets for itself.
READONLY_PATHS: frozenset[str] = frozenset(
    {
        # Sandbox, approvals and what the agent may run.
        "agent.sandbox",
        "agent.sandbox_allow_no_isolation",
        "agent.sandbox_allow_unsandboxed_exec",
        "agent.jail",
        "agent.dangerously_skip_permissions",
        "agent.approval_mode",
        "agent.interactive_command_policy",
        "mcp.honour_auto_approve",
        "hooks.batch_write_guard",
        "skills.project_skills_enabled",
        "skills.generate_scripts",
        "skills.approval_required",
        "dashboard.terminal.shell",
        # Sign-ins, credentials and governance.
        "agent.kas_persist_mcp_sign_in",
        "agent.kas_refuse_plain_http_mcp_headers",
        "agent.mcp_registry_mode",
        "browser.credentials.enabled",
        "browser.credentials.domains",
        "dashboard.browser_local_origins",
        "dashboard.sso_login_flags",
        "dashboard.qr_session_until_restart",
        "dashboard.qr_session_persist_across_restart",
        # The decision judge's provider, and the history budget the keystone caps:
        # their owner controls live behind the consent route, not in config.json.
        "decisions.provider.timeout_ms",
        "decisions.history_budget_chars",
        # What one session or Bot may reach.
        "agent.session_sharing",
        "agent.session_control",
        "agent.session_manager_chain",
        "agent.member_dispatch",
        "agent.crew_panel",
        "instances.crew_reads_enabled",
        "mcp_gateway.enabled",
        "mcp_gateway.forward_declared_env",
        # Who can reach this machine.
        "tunnel.enabled",
        "tunnel.name_mode",
        "tunnel.relay_enabled",
        "dashboard.tailscale.enabled",
        "dashboard.tailscale.trust_identity",
        "dashboard.tailscale.bind_refresh_chains",
        "dashboard.tailscale.pin_scope",
        "dashboard.shared_crew.admission_enabled",
        "dashboard.shared_crew.tailnet_direct",
        "dashboard.shared_crew.containment_enabled",
        "dashboard.shared_crew.hard_limits",
        "dashboard.shared_crew.ssm",
        "dashboard.shared_crew.ssm_port",
        # Outbound activity reports.
        "wakatime.enabled",
        "wakatime.send_heartbeats",
        # Markers the product sets; flipping one by hand skips a step a person must see.
        "dashboard.privacy_acked",
        "dashboard.onboarded",
        "dashboard.import_onboarded",
        "dashboard.crewmates_onboarded",
        "dashboard.model_picker_configured",
        "memory.migrated",
        # Owned by the Settings embedding-model action, which changes the model and
        # its width together and re-embeds; a width alone breaks the loaded model.
        "memory.embedding_dim",
        "mcp_gateway.apps_enabled",
        # Top-level fields the loader reads, and to_dict saves, under slack.*: a
        # write to the top-level path would change nothing.
        "observe_max_messages",
        "observe_ttl_hours",
    }
)

#: Free-text and string-list keys the page may change. Each value is a plain name
#: or label, never a path, URL, host or command.
TEXT_EDIT_PATHS: frozenset[str] = frozenset(
    {
        "mcp_gateway.keep_alive_servers",
        "knowledge.auto_ingest_artifact_kinds",
        "timezone",
    }
)

#: Bounds for a generic rule. The loader still clamps each field to its own range
#: at read time; these only keep an absurd value out of the file.
_INT_LIMIT = 10**12
_STR_MAX_LEN = 256
_LIST_MAX_ITEMS = 64
_LIST_MAX_ITEM_LEN = 256

_SCALAR_TYPES = ("boolean", "integer", "number", "string")


@functools.cache
def _entries() -> dict[str, Any]:
    from kiro_crew.config.schema import SCHEMA_REGISTRY

    return {e.path: e for e in SCHEMA_REGISTRY}


def _is_string_list(entry: Any) -> bool:
    item = _entries().get(entry.path + ".*")
    return entry.type == "array" and item is not None and item.type == "string"


def _shown(entry: Any) -> bool:
    if "*" in entry.path or entry.sensitive or entry.deprecated:
        return False
    if entry.type in _SCALAR_TYPES and not entry.has_children:
        return True
    return _is_string_list(entry)


def _control_from_schema(entry: Any) -> dict[str, Any] | None:
    """The control and PATCH rule the schema alone supports, or ``None``."""
    if entry.other_types:
        return None
    if entry.enum_values:
        return {"type": "enum", "values": list(entry.enum_values)}
    if entry.type == "boolean":
        return {"type": "bool"}
    default = entry.default_value
    if entry.type == "integer":
        low = default if isinstance(default, int) and default < 0 else 0
        return {"type": "int", "min": low, "max": _INT_LIMIT}
    if entry.type == "number":
        low_f = float(default) if isinstance(default, (int, float)) and default < 0 else 0.0
        return {"type": "float", "min": low_f, "max": float(_INT_LIMIT)}
    if entry.path not in TEXT_EDIT_PATHS:
        return None
    if entry.type == "string":
        return {"type": "str", "max_len": _STR_MAX_LEN}
    if _is_string_list(entry):
        return {
            "type": "str_list",
            "max_items": _LIST_MAX_ITEMS,
            "max_item_len": _LIST_MAX_ITEM_LEN,
        }
    return None


@functools.cache
def _dashboard_setting_ids() -> dict[str, str]:
    """Config path -> Settings registry id, for the controls that save through
    ``PUT /api/dashboard/config`` rather than this route."""
    from kiro_crew.change_card_catalog import DASHBOARD_LIST_SETTINGS, DASHBOARD_SETTINGS

    out = {f"dashboard.{key}": sid for sid, (key, _values) in DASHBOARD_SETTINGS.items()}
    out.update({f"dashboard.{spec[0]}": sid for sid, spec in DASHBOARD_LIST_SETTINGS.items()})
    return out


#: Prefixes of keys another control owns and mirrors elsewhere (a preview flag is
#: also cached in the browser), so a write here would leave that copy stale.
_READONLY_PREFIXES = ("dashboard.feature_previews.",)


def _blocked(entry: Any) -> bool:
    """A key the page never changes, whatever rule could write it."""
    # Through the facade: only the core_* family imports its owners.
    from kiro_crew.dashboard.handlers.core import _MOVED_CONFIG_FIELDS
    from kiro_crew.edition_locks import config_path_locked

    # A channel's settings carry its tag; its own page runs the sign-in and
    # allow-list checks that decide who may message the agent.
    return (
        entry.path in READONLY_PATHS
        or entry.path.startswith(_READONLY_PREFIXES)
        or bool(entry.tags)
        or entry.path in _MOVED_CONFIG_FIELDS
        or config_path_locked(entry.path)
    )


def generic_spec(path: str) -> dict[str, Any] | None:
    """The schema-derived PATCH rule for *path*, or ``None`` when it has none.

    Only for a key with no curated rule in ``_EDITABLE_CONFIG``: the caller asks
    that table first. The rule carries ``"generic": True`` so the handler applies
    the strict type check and allows ``unset``.
    """
    entry = _entries().get(path)
    if entry is None or not _shown(entry) or _blocked(entry):
        return None
    control = _control_from_schema(entry)
    if control is None:
        return None
    return {**control, "generic": True}


#: Curated rule types the page can draw a control for.
_CURATED_CONTROLS = ("bool", "enum", "int", "float", "str", "str_list")


def descriptor(entry: Any) -> dict[str, Any] | None:
    """The ``allSettings`` record for *entry* in ``GET /api/config/schema``.

    ``None`` when the page does not list the key. Otherwise ``mode`` is
    ``"edit"``, ``"readonly"``, or ``"control"`` with the ``settingId`` of the
    Settings control that owns it; an editable key also names its ``control`` and,
    where the rule has them, its ``values`` / ``min`` / ``max``. ``resettable`` is
    true when ``unset`` may remove the stored value.
    """
    if not _shown(entry):
        return None
    from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG

    curated = _EDITABLE_CONFIG.get(entry.path)
    setting_id = _dashboard_setting_ids().get(entry.path)
    if setting_id is not None:
        # The Settings page writes it through PUT /api/dashboard/config: link there.
        return {"mode": "control", "settingId": setting_id}
    if _blocked(entry):
        return {"mode": "readonly"}
    if curated is not None:
        if curated["type"] not in _CURATED_CONTROLS or "values_fn" in curated:
            return {"mode": "readonly"}
        spec: dict[str, Any] = curated
        resettable = False
    else:
        generic = generic_spec(entry.path)
        if generic is None:
            return {"mode": "readonly"}
        spec, resettable = generic, True
    out: dict[str, Any] = {"mode": "edit", "control": spec["type"], "resettable": resettable}
    for key in ("values", "min", "max", "max_len", "max_items", "max_item_len"):
        if key in spec:
            out[key] = spec[key]
    return out


def _timezone_error(value: str) -> str | None:
    """Refuse a zone the cron scheduler cannot load; ``""`` clears it to the host's."""
    from kiro_crew.cron_service.schedule import is_valid_timezone

    if value == "" or is_valid_timezone(value):
        return None
    return "must be an IANA timezone name such as America/Los_Angeles, or local"


#: Free-text keys whose value must also mean something to its reader.
_TEXT_CHECKS = {"timezone": _timezone_error}


def generic_value_error(path: str, value: object) -> str | None:
    """Why *value* cannot be stored at *path* under its generic rule, or ``None``.

    The type check is ``kiro_crew config set``'s own (``cli_config._declared_type_error``):
    the curated branches of the PATCH route coerce (``int("3")``, ``float(True)``)
    for their older callers, and a generic key gets exactly the type its schema
    declares. A free-text key named in ``_TEXT_CHECKS`` is checked by its reader's
    own validator too.
    """
    from kiro_crew.cli_config import _declared_type_error

    entry = _entries().get(path)
    if entry is None:
        return f"unknown config key {path}"
    reason = _declared_type_error(entry, value)
    if reason:
        return reason
    check = _TEXT_CHECKS.get(path)
    return check(value) if check and isinstance(value, str) else None


def attach_descriptor(serialized: dict[str, Any], entry: Any) -> None:
    """In place: add *entry*'s ``allSettings`` record to its schema-route dict."""
    record = descriptor(entry)
    if record is not None:
        serialized["allSettings"] = record
