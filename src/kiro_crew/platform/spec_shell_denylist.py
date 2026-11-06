"""A KAS spec's ``toolsSettings.shell.deniedCommands``, enforced at Crew's own gate.

The KAS projection carries a ``deniedCommands`` entry to the engine only when it
is a literal plus ``.*`` (``kas_permissions._regex_to_glob``); any other entry has
no KAS rule. Crew records every entry instead and its gate's
``spec-shell-denylist`` row refuses a shell call one names, on every approval
policy.

kiro-cli's own reading, which this mirrors: each entry is compiled as
``Regex::new(&format!(r"\\A{dc}\\z"))`` and matched against the command, so an entry
must match the WHOLE command (the same anchoring it gives ``allowedCommands``,
and the one :mod:`kiro_crew.platform.allowed_commands` documents). Crew matches the
same anchored pattern against the whole command AND against every piece the
command chains, pipes, backgrounds or substitutes, so it never refuses less than
kiro-cli does.

Each entry is the user's own pattern, compiled from the spec at runtime. It is
read first by :func:`allowed_commands.parse`, which matches in linear time; an
entry outside that subset (``\\b``, ``\\s``, ``{n}``, ``(?i)``) is compiled with
Python's :mod:`re` and fullmatched. An entry that compiles in neither, or uses
syntax Python reads but kiro-cli's Rust engine refuses or reads differently
(look-around, backreferences, possessive or atomic groups, POSIX ``[:class:]``),
is UNKNOWN: kiro-cli would refuse every command for it, so Crew's gate lets no
auto-approve path run any shell call of the agent, and a person decides.

A leaf: stdlib and :mod:`kiro_crew.platform.allowed_commands` only.
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew.platform import allowed_commands
from kiro_crew.platform.allowed_commands import _MAX_MATCH_STEPS, _match

#: What ends one command and starts another to a shell, plus the substitution and
#: grouping openers. Splitting at every one, quoted or not, only adds pieces, so
#: the refusal reads wider, never narrower.
_BREAKS = ("&&", "||", "$(", ";", "|", "&", "`", "(", ")", "{", "}", "\n", "\r")

#: Bound on the pieces one command is split into; past it the verdict is unknown.
_MAX_PIECES = 256

#: Pieces of syntax Python's ``re`` accepts that Rust's ``regex`` crate refuses or
#: reads differently. An entry holding one is not evaluated: kiro-cli would treat
#: it as invalid (deny all), so Crew asks a person instead. Plain substring tests,
#: never a pattern.
_RUST_INCOMPATIBLE = ("(?=", "(?!", "(?<=", "(?<!", "(?>", "(?P=", "[:", "*+", "++", "?+", "}+")


def _python_pattern(raw: str) -> re.Pattern[str] | None:
    """*raw* compiled as kiro-cli anchors it, with Python's :mod:`re`, or ``None``."""
    if any(piece in raw for piece in _RUST_INCOMPATIBLE):
        return None
    if any(raw[i] == "\\" and raw[i + 1 : i + 2].isdigit() for i in range(len(raw))):
        return None  # a backreference, which Rust has no meaning for
    # Rust's end-of-text anchor ``\z`` is ``\Z`` in Python.
    body = raw.replace("\\z", "\\Z")
    # Leading inline flags (``(?i)``) stay in front: Python refuses them inside the
    # group that keeps the user's alternation whole; they still apply to all of it.
    flags = ""
    while body.startswith("(?") and ")" in body:
        head = body[2 : body.index(")")]
        if not head or not set(head) <= set("imsx"):
            break
        flags += head
        body = body[len(head) + 3 :]
    try:
        return re.compile(f"(?{flags})(?:{body})" if flags else f"(?:{body})")
    except (re.error, RecursionError, OverflowError, ValueError):
        return None


def _pieces(command: str) -> list[str] | None:
    """The whole *command* and every piece of it, or ``None`` past the bound."""
    pieces = [command]
    current = [command]
    for brk in _BREAKS:
        current = [part for chunk in current for part in chunk.split(brk)]
        if len(current) > _MAX_PIECES:
            return None
    pieces.extend(p.strip(" \t") for p in current if p.strip(" \t"))
    return list(dict.fromkeys(pieces))


def unreadable_entries(entries: Any) -> list[str]:
    """The entries of *entries* the gate can judge on no command, for the notice."""
    if not isinstance(entries, (list, tuple)):
        return [repr(entries)]
    bad: list[str] = []
    for raw in entries:
        if not isinstance(raw, str):
            bad.append(repr(raw))
        elif raw.strip() and allowed_commands.parse(raw) is None and not _python_pattern(raw):
            bad.append(raw)
    return bad


def denylist_verdict(entries: Any, command: Any) -> tuple[str, str]:
    """How the spec's *entries* judge the shell *command*: ``(verdict, entry)``.

    ``("denied", entry)`` when an entry matches the whole command or a piece of
    it; ``("unknown", entry)`` when an entry, or the command, cannot be judged;
    ``("clear", "")`` when every entry was matched against every piece and none
    matched.
    """
    if not isinstance(command, str) or not command:
        return "unknown", "(no command)"
    if not isinstance(entries, (list, tuple)):
        return "unknown", repr(entries)
    pieces = _pieces(command)
    if pieces is None:
        return "unknown", "(command too long to split)"
    budget = [_MAX_MATCH_STEPS]
    unknown = ""
    for raw in entries:
        if not isinstance(raw, str):
            unknown = unknown or repr(raw)
            continue
        if not raw.strip():
            continue
        parsed = allowed_commands.parse(raw)
        if parsed is not None:
            for piece in pieces:
                if _match(parsed, piece, budget):
                    return "denied", raw
                if budget[0] < 0:
                    unknown = unknown or raw
                    break
            continue
        compiled = _python_pattern(raw)
        if compiled is None:
            unknown = unknown or raw
            continue
        # shortcut: Python's re backtracks; a nested-quantifier entry can be slow on
        # a long command. The entry is the spec author's own; a linear engine is
        # the upgrade if one is ever written that way.
        if any(compiled.fullmatch(piece) for piece in pieces):
            return "denied", raw
    if unknown:
        return "unknown", unknown
    return "clear", ""


#: The ``toolsSettings`` keys kiro-cli reads its shell settings under.
_SHELL_KEYS = ("execute_bash", "shell")


def spec_denied_commands(tools_settings: Any) -> list[Any] | None:
    """Every ``deniedCommands`` entry of both shell keys, as written, or ``None``.

    ``None`` when neither key sets the field. A value that is not a list is kept
    as one entry that is not a string, so the gate cannot read it and keeps the
    call for a person rather than drop it.
    """
    if not isinstance(tools_settings, dict):
        return None
    out: list[Any] = []
    found = False
    for key in _SHELL_KEYS:
        settings = tools_settings.get(key)
        if not isinstance(settings, dict) or "deniedCommands" not in settings:
            continue
        value = settings["deniedCommands"]
        if value is None:
            continue
        found = True
        out.extend(value if isinstance(value, list) else [{"not_a_list": value}])
    return out if found else None
