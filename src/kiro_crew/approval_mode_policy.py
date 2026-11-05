"""Whether the policy lets a session hold ``trust`` or ``trust_reads``. From memory.

The ``approval_modes`` scope (KGL ``permission_mode``) may deny ``trust`` and
``trust_reads``; only ``normal`` is the floor. A deny has to hold where a grant is
HONOURED, not only where it is set: a slot that was trusted before the policy landed,
a session whose stored ``approval_policy`` is ``"auto"``, a messaging session in the
Trust set. Those reads happen per tool call and on the 5s status push, so the answer
is resolved once per ceiling install and read from a module flag, like the ``yolo``
verdict in ``safety_override_grant``. A published governance-profile snapshot (a
profile can narrow the scope too) marks the answer stale, and the next read resolves
it again before answering.

Fail closed: before a verdict is resolved, while a new ceiling is being installed, and
when the policy cannot be read, both modes are denied (a person is asked instead).
With no policy (``oss``) the scope is ungoverned and both are permitted.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from kiro_crew.platform.context import (
    register_ceiling_install_hook,
    register_ceiling_invalidate_hook,
)

logger = logging.getLogger(__name__)

#: Approval modes a single session may hold. ``yolo`` is process-wide and has its
#: own pushed verdict; ``normal`` is the floor and never denied.
SESSION_TRUST_MODES = ("trust_reads", "trust")
#: Every approval-mode id Crew knows. Anything else is refused, not passed on.
KNOWN_APPROVAL_MODES = ("normal", "trust_reads", "trust", "yolo")

_denied: frozenset[str] = frozenset(SESSION_TRUST_MODES)
_resolved = False
#: Bumped by each published profile snapshot; ``_resolved_epoch`` is the one the
#: verdict was resolved under. A mismatch means a profile changed since.
_profile_epoch = 0
_resolved_epoch = 0
#: Bumped by each ceiling invalidation. A resolve started under an older value was
#: reading a ceiling that is being replaced, so its answer is dropped.
_ceiling_gen = 0
#: True from a ceiling invalidation until its install hook resolves: reads deny.
_installing = False
#: Set while no install is in progress; ``ensure_resolved`` waits on it.
_install_done = threading.Event()
_install_done.set()
#: shortcut: an install that takes longer than this leaves ensure_resolved with a
#: deny; raise it if a slow governance read ever trips it in practice.
_INSTALL_WAIT_SECS = 30.0
#: Called with the denied modes after an install resolves a deny. Each one drops the
#: grants it holds, so a later policy that allows the mode again does not bring a
#: grant back that nobody re-made. The read paths mask on their own; these make the
#: mask and the state agree.
#: Keyed by owner, so a re-registration (a second ``start_dashboard`` in one
#: process) replaces the old callback instead of keeping its state alive.
_deny_hooks: dict[str, Callable[[frozenset[str]], None]] = {}


def register_deny_hook(owner: str, cb: Callable[[frozenset[str]], None]) -> None:
    """Call ``cb(denied)`` after each install that resolves a deny; one per *owner*."""
    _deny_hooks[owner] = cb


def _run_deny_hooks(denied: frozenset[str]) -> None:
    for cb in list(_deny_hooks.values()):
        try:
            cb(denied)
        except Exception:
            logger.warning("a trust-revoke hook raised; its grants are masked only", exc_info=True)


def _resolve() -> frozenset[str]:
    from kiro_crew.safety_override_permits import approval_mode_permitted

    denied = set()
    for mode in SESSION_TRUST_MODES:
        try:
            if not approval_mode_permitted(mode):
                denied.add(mode)
        except Exception:
            logger.warning(
                "could not resolve the approval_modes policy for %s; denying it",
                mode,
                exc_info=True,
            )
            denied.add(mode)
    return frozenset(denied)


def _on_ceiling_invalidating() -> None:
    """Deny both modes before a new ceiling becomes visible. Pure memory."""
    global _denied, _resolved, _ceiling_gen, _installing
    _ceiling_gen += 1
    _installing = True
    _install_done.clear()
    _denied = frozenset(SESSION_TRUST_MODES)
    _resolved = True


def _on_ceiling_installed(ctx: object) -> None:
    global _installing
    if ctx is None:
        reset_state()
        return
    _installing = False
    try:
        _resolve_now()
    finally:
        _install_done.set()


def _resolve_now() -> None:
    """Resolve the verdict, store it, and drop denied grants. MAY BLOCK (governance read).

    The resolve is a governance read, and that read can itself publish a profile
    snapshot (the store's first load, or a file changed on disk). So it resolves again
    while the epoch moves under it, a few times at most; a verdict still unsettled
    after that is recorded under the older epoch, and the next read resolves again.
    """
    global _denied, _resolved, _resolved_epoch
    if _installing:
        # A ceiling install is in progress: the outgoing ceiling may still be the one
        # a read here would see. Its install hook resolves the new one.
        return
    gen = _ceiling_gen
    epoch = _profile_epoch
    denied = _resolve()
    for _ in range(3):
        if _profile_epoch == epoch:
            break
        epoch = _profile_epoch
        denied = _resolve()
    if gen != _ceiling_gen or _installing:
        # A ceiling install started while this resolved: its answer belongs to the
        # outgoing ceiling. Drop it; the install hook resolves the new one.
        return
    _denied, _resolved, _resolved_epoch = denied, True, epoch
    if denied:
        _run_deny_hooks(denied)


_resolve_pending = False


def _on_event_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _schedule_resolve() -> None:
    """Re-resolve on a worker thread, once at a time. Called on the event loop."""
    global _resolve_pending
    if _resolve_pending:
        return
    import asyncio

    _resolve_pending = True

    def _run() -> None:
        global _resolve_pending
        try:
            _resolve_now()
        finally:
            _resolve_pending = False

    try:
        asyncio.get_running_loop().run_in_executor(None, _run)
    except Exception:
        # No worker (loop teardown): leave the verdict stale and let a later read retry.
        _resolve_pending = False
        logger.debug("could not schedule the trust verdict resolve", exc_info=True)


def _on_profile_published() -> None:
    """A governance profile changed: the cached verdict is stale. Pure memory."""
    global _profile_epoch
    _profile_epoch += 1


def _bootstrap() -> None:
    """First read with no install pushed yet (a late import, a CLI, a test)."""
    global _denied, _resolved
    try:
        from kiro_crew.platform.context import current_context

        current_context()
    except Exception:
        logger.debug("no installed ceiling for the trust verdict; denying", exc_info=True)
        _denied, _resolved = frozenset(SESSION_TRUST_MODES), True
        return
    if not _resolved:
        _resolve_now()


def session_mode_permitted(mode: str) -> bool:
    """True when policy lets a session hold *mode*. ``normal`` always; unknown never."""
    if mode == "normal":
        return True
    if mode not in SESSION_TRUST_MODES:
        return False
    if _installing:
        return False
    if not _resolved:
        _bootstrap()
    elif _resolved_epoch != _profile_epoch:
        # A profile changed since the verdict. Re-resolving is a governance read that
        # stats the profiles dir, so on the event loop it runs on a worker thread and
        # this read answers deny until it lands (fail closed). Off the loop, inline.
        if _on_event_loop():
            _schedule_resolve()
            return False
        _resolve_now()
    return mode not in _denied


def verdict_pending() -> bool:
    """True while the verdict is not final: an install, or a profile edit unresolved.

    A read then answers deny (fail closed). A caller that would COMMIT that deny as a
    lasting choice (a crewmate's one-shot seed) waits for a final answer instead.
    """
    return _installing or (_resolved and _resolved_epoch != _profile_epoch)


def ensure_resolved() -> None:
    """Make the verdict final: wait out an install, then resolve a stale one.

    MAY BLOCK (a wait and a governance read): call it off the event loop. A caller
    that COMMITS a mode (a crewmate Profile apply) calls it first, so a passing deny
    is never stored as a choice.
    """
    if _installing:
        _install_done.wait(_INSTALL_WAIT_SECS)
    if not _installing and (not _resolved or _resolved_epoch != _profile_epoch):
        _resolve_now()


def clamp_session_mode(mode: str) -> str:
    """*mode* if the policy permits it, else the highest permitted mode below it.

    For a default nobody chose this session (a crewmate's unset Profile opens in
    ``trust``): it lands on what the policy allows rather than being refused.
    """
    order = ("normal", *SESSION_TRUST_MODES)
    if mode not in order:
        return "normal"
    for candidate in reversed(order[: order.index(mode) + 1]):
        if session_mode_permitted(candidate):
            return candidate
    return "normal"


def stored_auto_permitted() -> bool:
    """Whether an ``approval_policy="auto"`` grant may still be honoured. From memory.

    ``"auto"`` is written by a session Trust (slot trust, a messaging Trust press, a
    crewmate Profile, a spawn's ``approval_mode``) or by a session-wide YOLO grant, and
    its readers (the spawn gate, a subagent's inherited policy, a messaging session's
    own approval) trust it as is. With ``trust`` denied it holds only while a
    session-wide YOLO grant is live; YOLO revocation already clears what it wrote.
    """
    if session_mode_permitted("trust"):
        return True
    from kiro_crew.safety_override import safety_override

    return bool(safety_override().is_active())


def disabled_session_modes() -> list[str]:
    """The session modes the policy forbids, in picker order."""
    return [m for m in SESSION_TRUST_MODES if not session_mode_permitted(m)]


def reset_state() -> None:
    """Forget the verdict so the next read resolves again. Tests and ``reset_context``."""
    global _denied, _resolved, _installing
    _denied, _resolved, _installing = frozenset(SESSION_TRUST_MODES), False, False
    _install_done.set()


def register_hooks() -> None:
    """Register the mask and the push, AFTER the YOLO verdict's pair.

    Called by :mod:`kiro_crew.safety_override` right after it registers its own.
    The order is load-bearing: the YOLO suspend (``on_policy_suspend``) reads each
    slot's stored ``approval_policy`` through ``get_approval_policy``, which this
    module's mask also answers. Masked first, the suspend would read ``""``, skip a
    YOLO-inherited ``"auto"``, and this push would unmask it again before the YOLO
    verdict resolved. Registration is idempotent per callable.
    """
    register_ceiling_invalidate_hook(_on_ceiling_invalidating)
    register_ceiling_install_hook(_on_ceiling_installed)
    from kiro_crew.platform.governance_profiles import register_profile_publish_hook

    register_profile_publish_hook(_on_profile_published)


# Importing the facade registers both pairs in order, whichever module loads first.
from kiro_crew import safety_override as _safety_override  # noqa: E402,F401
