"""Whole-file content provenance for the sandbox-sealed keystone config files.

The Linux namespace sandbox seals ``config.json``, ``config.local.json``,
``denied_commands.json`` and ``computer_use.json`` read-only with a per-FILE
read-only bind. Every legitimate writer publishes through ``atomic_write`` (temp
file + ``rename``), and the kernel detaches a file bind whose target is renamed
over from another mount namespace. So the seal on one of these files holds from a
sandbox's spawn only until the gateway's NEXT save of that file, after which a
prompt-injected agent that lands a direct ``open(..., "w")`` in that window can
rewrite it -- turning ``agent.sandbox`` off, clearing the denied-command deny-all,
auto-approving tools, enabling computer use -- and the gateway then reads the
agent's own file as if the operator had written it.

This module closes that WITHOUT depending on the bind holding and WITHOUT a list of
keys to keep current: every gateway write stores the exact bytes it is about to publish
as the ENDORSED COPY before the rename (:func:`endorsing`), and every read -- the load
AND the read half of a read-modify-write -- serves the endorsed copy whenever the
on-disk bytes differ from it, so a later unrelated save cannot re-endorse a tampered
file. Every key is covered, tightening and loosening alike; there is
no key table. A file that differs is pending, and an ordinary save of it is refused
(:func:`refuse_save_if_pending`) until the owner applies or discards the change, so the
change is neither endorsed nor silently overwritten.

**Where it applies.** The per-file bind, and the rename-detach that lapses it, exist
ONLY in the Linux namespace sandbox. Off Linux, and on a Linux host where that
sandbox cannot be built (a container without user namespaces), nothing ever seals
these files, so the loaders serve the on-disk file unchanged
(:func:`enforcement_active`). The decision reads HOST FACTS only (platform + userns),
never the file it is protecting, so writing ``sandbox: off`` cannot switch it off.

**A hand edit is PENDING, never silently dropped.** Where it applies, an on-disk file
that differs from its endorsed copy keeps loading as the endorsed copy, and is reported
as pending (:func:`pending_changes`: the leaf, a signature of the on-disk bytes, and a
plain changed-key diff with secret-bearing values withheld). The owner applies or
discards it from Settings (``dashboard/handlers/config_pending.py``, owner-gated and
CSRF-guarded like every Settings write, never reachable by the agent or an MCP tool);
both go through the leaf's normal endorsing writer. Users may hand-edit; nothing
requires it, and nothing an in-sandbox process writes takes effect on its own. A file
that is deleted or made unreadable is the same case: every reader serves the endorsed
copy (:func:`reconciled_bytes`) and the leaf is pending as removed until the owner
discards the removal, which puts the file back.

The endorsed copies live under the ``config-provenance`` directory of the data home.
That directory is a read-only DIRECTORY bind in every sandbox
(``sandbox_leaves._CREW_READONLY_LEAVES``), and a directory bind does NOT suffer the
rename-detach: the gateway's writes land on files INSIDE it and leave the directory's
own inode -- what the bind pins -- unchanged. So no sandboxed process can write or
replace an endorsed copy. Each copy gets the same file-tool gate as its original, and
all are written owner-only (0o600): a copy of ``config.json`` can hold an inline channel
token and stays readable like ``config.json`` (neither is on the read-gate floor), while
the copies of ``denied_commands.json`` and ``computer_use.json`` are on the read+write
floor beside their originals (``security.paths_lists``). The directory bind keeps all of
them readable to in-sandbox loaders, which read them directly in :func:`reconcile`.

``docs/system-specs/modules/security.md`` and ``config.md`` describe the seal and
this content check together.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir, peek_data_home
from kiro_crew.user_json import strict_json_equal

logger = logging.getLogger("kiro_crew.config.loader")

#: The sealed data-home directory the endorsed copies live under -- shared with
#: ``write_provenance`` (a read-only, no-follow, pre-created DIRECTORY bind in every
#: sandbox).
RECORD_DIR_LEAF = "config-provenance"
#: The sub-directory holding one endorsed copy per protected leaf.
ENDORSED_SUBDIR = "endorsed"
#: Owner-only on both directory levels and the copies: a copy can carry an inline
#: credential, exactly like the file it mirrors, and the data home leaves nothing
#: readable by other local users (test_owner_only_boot).
_DIR_MODE = 0o700
_FILE_MODE = 0o600

LEAF_CONFIG = "config.json"
LEAF_CONFIG_LOCAL = "config.local.json"
LEAF_DENIED_COMMANDS = "denied_commands.json"
LEAF_COMPUTER_USE = "computer_use.json"

PROTECTED_LEAVES: tuple[str, ...] = (
    LEAF_CONFIG,
    LEAF_CONFIG_LOCAL,
    LEAF_DENIED_COMMANDS,
    LEAF_COMPUTER_USE,
)

#: Leaves whose on-disk file has already been reported as differing from the endorsed
#: copy, so the substitution is logged once per leaf per process. A lost race only
#: duplicates a log line.
_reported: set[str] = set()

#: ``(leaf, pending signature)`` pairs whose refused migration write-back was already
#: logged, so a pending file logs once per version per process, not on every load.
_writeback_refusals_logged: set[tuple[str, str]] = set()

#: This process's answer to "can the namespace sandbox be built here", once a probe
#: gave a definite one. ``None`` until then.
_host_verdict: bool | None = None
#: After a transient probe failure, the ``time.monotonic()`` before which no read probes
#: again: every config read asks, so an uncached transient answer would re-probe on each
#: one, on whatever thread (an event loop included) did the read.
_transient_until = 0.0
_TRANSIENT_RETRY_SECS = 60.0


def enforcement_active() -> bool:
    """True only where the Linux per-file read-only bind can actually detach.

    The substitution matters only when the namespace sandbox seals these files. Off
    Linux (Windows, macOS), and on a Linux host where the namespace sandbox cannot be
    built (a container without user namespaces), there is no per-file bind to lapse, so
    the loaders serve the on-disk file unchanged. HOST FACTS only (platform + userns),
    so it never reads the file it protects and never recurses into a config load.

    Every config read calls this, so the host fact is probed once per process and
    cached (:func:`_probe_host`), never once per read. Fails toward ENFORCING on
    Linux: a probe that raises or fails transiently counts as sandbox-capable, like the
    sandbox, which keeps trying to spawn after a transient probe. That answer holds for
    :data:`_TRANSIENT_RETRY_SECS`; the first read after it probes again.
    """
    global _host_verdict, _transient_until
    if sys.platform != "linux":
        return False
    if _host_verdict is None:
        now = time.monotonic()
        if now < _transient_until:
            return True
        verdict = _probe_host()
        if verdict is None:
            _transient_until = now + _TRANSIENT_RETRY_SECS
            return True
        _host_verdict = verdict
    return _host_verdict


def _probe_host() -> bool | None:
    """One quiet namespace probe: ``True``/``False``, or ``None`` when it proved nothing.

    Calls the probe itself rather than ``sandbox.userns_available``: that entry logs
    each failure as a warning and records it as the sandbox's last failure, which the
    spawn refusal and its remedy text report. A config read must change neither.
    shortcut: a cold cache probes once on the calling thread (about 12 ms), even on an
    event loop; prime it at gateway boot if that ever shows up in a loop-lag trace.
    """
    try:
        from kiro_crew.sandbox_probe import _probe_unshare_once

        ok, transient, _reason, _remedy = _probe_unshare_once()
    except Exception:
        logger.debug("loosening provenance: userns probe failed; enforcing", exc_info=True)
        return None
    if ok:
        return True
    return None if transient else False


def _endorsed_dir(home: Path | None = None) -> Path:
    return (config_dir() if home is None else home) / RECORD_DIR_LEAF / ENDORSED_SUBDIR


def _endorsed_path(leaf: str, home: Path | None = None) -> Path:
    return _endorsed_dir(home) / leaf


def leaf_for(path: Path) -> str | None:
    """The protected leaf *path* is, or ``None`` for any other file.

    Matched by RESOLVED identity against each protected name under the data home -- the
    same rule as ``write_provenance.staging_hook`` -- so a symlinked ``config.json``
    written through to its target (``crew-settings.json`` in a dotfiles repo) is still
    endorsed as ``config.json``, and a file elsewhere that merely shares a protected
    basename is never mistaken for one.

    Locates the home through ``peek_data_home``, never ``config_dir``: this only asks
    where the home is, and ``spec_build._load_json`` calls it at import time, so a
    resolver that creates the home would turn "import the package" into "mkdir it".
    """
    try:
        target = path.resolve()
        home = peek_data_home()
        return next((leaf for leaf in PROTECTED_LEAVES if (home / leaf).resolve() == target), None)
    except OSError:
        return None


def record(leaf: str, content: bytes, *, strict: bool = False, home: Path | None = None) -> None:
    """Store *content* as the gateway-endorsed copy of *leaf*.

    *home* is the data home the copy belongs to, this process's own by default (see
    :func:`endorsing`).

    A failed write leaves the PREVIOUS copy in place (``atomic_write`` never leaves it
    half-written): removing it would hand the next load the on-disk bytes, i.e. drop
    the content check exactly when a disk is full. *strict* re-raises the failure, for a
    writer that must not publish what it could not endorse (:func:`endorsing`).
    """
    if leaf not in PROTECTED_LEAVES:
        return
    try:
        # Owner-only on BOTH levels: parents=True would create config-provenance with
        # the umask default, which test_owner_only_boot refuses.
        ((config_dir() if home is None else home) / RECORD_DIR_LEAF).mkdir(
            mode=_DIR_MODE, exist_ok=True
        )
        _endorsed_dir(home).mkdir(mode=_DIR_MODE, exist_ok=True)
        atomic_write(_endorsed_path(leaf, home), content, mode=_FILE_MODE)
    except Exception:
        if strict:
            raise
        logger.warning(
            "loosening provenance: could not record the endorsed copy for %s; "
            "the previous copy stays in effect",
            leaf,
            exc_info=True,
        )


#: The refusal every ordinary save of a pending file gets, CLI and dashboard alike. Plain
#: words, no terminal step: the dashboard shows its own translation of the same sentence
#: (keyed by :attr:`PendingEditBlocksSave.code`) with a link to the pending card.
PENDING_SAVE_REFUSED = (
    "This settings file was changed outside Kiro Crew, so this save was not made. "
    "Review the change in Settings > Security first (Apply or Discard), then try again."
)


def leaf_pending(leaf: str) -> bool:
    """Whether *leaf* is pending: :func:`pending_changes` would list it.

    Raises ``OSError`` when the endorsed copy exists but cannot be read, as
    :func:`endorsed_bytes` does.
    """
    if not enforcement_active():
        return False
    endorsed = endorsed_bytes(leaf)
    return endorsed is not None and endorsed != _disk_bytes(leaf)


def refuse_save_if_pending(leaf: str | None) -> None:
    """Raise ``PendingEditBlocksSave`` when *leaf*, in this data home, is pending.

    An ordinary save reads the endorsed copy, so publishing it would replace the
    pending on-disk edit with nothing keeping it. Only the owner's Apply or Discard
    (``resolving_pending=True`` on the leaf's writer) may write a pending leaf. Every
    other leaf saves normally. A no-op where nothing is ever pending.
    shortcut: an edit landing between this check and the rename is still replaced, as
    before; closing that needs a lock every outside editor honours.
    """
    if leaf is None or leaf not in PROTECTED_LEAVES or not leaf_pending(leaf):
        return
    # Function-local: the class subclasses ConfigWriteRefused, so every caller's
    # existing refusal path catches it, and loader_document imports this module.
    from kiro_crew.config.loader_document import PendingEditBlocksSave

    raise PendingEditBlocksSave(leaf)


@contextlib.contextmanager
def endorsing(
    leaf: str | None,
    content: bytes,
    *,
    home: Path | None = None,
    resolving_pending: bool = False,
) -> Iterator[None]:
    """Endorse *content* for *leaf* BEFORE the ``with`` body publishes it.

    A save to a leaf of this data home that is pending is refused before anything is
    endorsed or published (:func:`refuse_save_if_pending`), unless the caller is the
    owner's Apply or Discard (*resolving_pending*). A write into another *home* (the
    pod seed rewrite) is not checked here: it already leaves a pending pod file alone.

    *content* is the writer's own bytes, never a re-read of the destination, so nothing
    an in-sandbox process lands on the name around the rename can be endorsed. Where the
    sandbox seals these files (:func:`enforcement_active`) the endorsement is required:
    a copy that cannot be read or written aborts the write before anything is
    published, so the gateway never publishes a protected file it has not endorsed; a
    publish that then fails puts the previous copy back. Elsewhere it is best-effort and
    never blocks a save. ``leaf=None`` (not a protected file) is a no-op.

    *home* names ANOTHER data home whose file this process writes -- a pod home that
    ``kirocrew pod`` prepares, which that pod's gateway loads -- so the copy lands
    where that gateway reads it. ``None`` is this process's own data home.
    """
    if leaf is None or leaf not in PROTECTED_LEAVES:
        yield
        return
    if home is None and not resolving_pending:
        refuse_save_if_pending(leaf)
    enforcing = enforcement_active()
    # raises under enforcement when the copy is unreadable
    prior = endorsed_bytes(leaf, home=home)
    record(leaf, content, strict=enforcing, home=home)
    try:
        yield
    except BaseException:
        if enforcing:
            try:
                if prior is None:
                    _endorsed_path(leaf, home).unlink(missing_ok=True)
                else:
                    record(leaf, prior, strict=True, home=home)
            except Exception:
                logger.warning(
                    "loosening provenance: could not restore the endorsed copy for %s "
                    "after a failed write",
                    leaf,
                    exc_info=True,
                )
        raise


def endorsed_bytes(leaf: str, *, home: Path | None = None) -> bytes | None:
    """The gateway-endorsed copy of *leaf*, or ``None`` when there is none.

    Where the sandbox seals these files, a copy that EXISTS but cannot be read raises
    (``OSError``) rather than reading as "none": "none" serves the on-disk bytes, so a
    read error would silently drop the content check. Every reader already maps an
    ``OSError`` to its unreadable-file path (defaults, refuse the write, disabled).
    """
    try:
        return _endorsed_path(leaf, home).read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        if enforcement_active():
            raise
        logger.debug("loosening provenance: endorsed copy for %s unreadable", leaf, exc_info=True)
        return None


def reconcile(leaf: str, disk_bytes: bytes) -> bytes:
    """Return the bytes the loader should parse for *leaf*.

    Where the sandbox seals these files (:func:`enforcement_active`): when the on-disk
    bytes differ from the gateway-endorsed copy, the endorsed copy is returned and the
    substitution is logged once -- so a direct in-sandbox rewrite of ANY key is ignored.
    Elsewhere, or when there is no endorsed copy yet (the first boot before adoption, a
    window in which no sandbox has run to tamper), the on-disk bytes are returned
    unchanged. A missing copy is not reachable by a sandbox: every gateway publish of
    these files endorses first (:func:`endorsing`), and an absent name is pre-created and
    sealed until such a publish, so a file with no copy is one no rename has unsealed.
    An existing but unreadable copy raises ``OSError`` (:func:`endorsed_bytes`).
    """
    if not enforcement_active():
        return disk_bytes
    endorsed = endorsed_bytes(leaf)
    if endorsed is None or endorsed == disk_bytes:
        return disk_bytes
    _report_once(leaf, "is not the gateway's own write")
    return endorsed


def reconciled_bytes(
    leaf: str, path: Path, read: Callable[[Path], bytes] = Path.read_bytes
) -> bytes | None:
    """The bytes every reader of *leaf* at *path* parses, or ``None`` for no file at all.

    The one entry the loaders and the read half of each read-modify-write call. It reads
    *path* with *read* and :func:`reconcile`\\ s the result. Where the sandbox seals these
    files and an endorsed copy exists, a file that is MISSING or cannot be read serves the
    endorsed copy too: an in-sandbox process that can rewrite the name past a detached bind
    can also delete or ``chmod`` it, and the "no file" path of every reader is its safe
    default (no owner deny rules, default config), not the owner's settings. Such a leaf
    is pending as removed (:func:`pending_changes`). Elsewhere, or with no endorsed copy,
    a missing file returns ``None`` and any other read error is raised, as before.
    """
    try:
        content = read(path)
    except (OSError, UnicodeDecodeError) as exc:
        # A text reader that cannot decode the file has read nothing usable either.
        endorsed = endorsed_bytes(leaf) if enforcement_active() else None
        if endorsed is not None:
            _report_once(leaf, "is missing or unreadable")
            return endorsed
        if isinstance(exc, FileNotFoundError):
            return None
        raise
    return reconcile(leaf, content)


def _report_once(leaf: str, what: str) -> None:
    if leaf not in _reported:
        _reported.add(leaf)
        logger.warning(
            "%s on disk %s; using the gateway-endorsed copy until the change is applied "
            "or discarded in Settings",
            leaf,
            what,
        )


def endorse_installed(path: Path, content: bytes) -> None:
    """Endorse *content*, the bytes an owner restore just installed at *path*.

    For the snapshot restore and import paths, which install ``config.json`` /
    ``config.local.json`` by copy rather than through ``atomic_write``: a restore is an
    owner action, so what it installs takes effect instead of being held as pending.
    *content* is the SOURCE file's bytes, never a re-read of the destination. A no-op for
    a path that is not a protected leaf of this data home. Raises where the sandbox seals
    these files and the copy cannot be written, so the restore's own rollback runs.
    """
    leaf = leaf_for(path)
    if leaf is not None:
        record(leaf, content, strict=enforcement_active())


@contextlib.contextmanager
def endorsements_restored_on_error() -> Iterator[None]:
    """Put every endorsed copy back as it was when the ``with`` body raises.

    For a multi-file restore whose rollback puts the previous files back: the copies the
    body endorsed (:func:`endorse_installed`) must go back with them, or the restored-over
    files would read as pending. Raises before the body when a copy cannot be read.
    """
    prior = {leaf: endorsed_bytes(leaf) for leaf in PROTECTED_LEAVES}
    try:
        yield
    except BaseException:
        for leaf, content in prior.items():
            try:
                if content is None:
                    _endorsed_path(leaf).unlink(missing_ok=True)
                else:
                    record(leaf, content, strict=True)
            except Exception:
                logger.warning(
                    "loosening provenance: could not restore the endorsed copy for %s "
                    "after a failed restore",
                    leaf,
                    exc_info=True,
                )
        raise


# --- One-time upgrade adoption ------------------------------------------------
#
# An install that predates this feature has the protected files but no endorsed copy.
# With no copy to serve, a load returns the on-disk file unchanged, so the first gateway
# boot endorses whatever is on disk and logs it once; after that the copy exists and is
# never re-adopted, so a later in-sandbox rewrite is overridden by the adopted copy. A
# file tampered BEFORE that first boot is adopted as written -- the one case this cannot
# catch, documented as a known limit.


def adopt_if_unendorsed(leaf: str, content: bytes) -> bool:
    """Endorse *content* for *leaf* only when there is no endorsed copy yet."""
    if leaf not in PROTECTED_LEAVES:
        return False
    if _endorsed_path(leaf).exists():
        return False
    record(leaf, content)
    logger.info(
        "loosening provenance: adopted the existing %s as endorsed (one-time, on upgrade)",
        leaf,
    )
    return True


def adopt_unendorsed() -> list[str]:
    """Boot sweep: endorse each protected file that has no endorsed copy yet.

    Called once from ``GatewayOrchestrator.run`` (the entry both the CLI gateway and the
    in-process test gateway reach), BEFORE any session or sandbox runs. A missing file is
    skipped: there is nothing to endorse and a load returns the on-disk file (its safe
    default) anyway. Returns the leaves adopted.
    """
    adopted: list[str] = []
    home = config_dir()
    for leaf in PROTECTED_LEAVES:
        try:
            content = (home / leaf).read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            logger.debug(
                "loosening provenance: could not read %s for adoption", leaf, exc_info=True
            )
            continue
        if adopt_if_unendorsed(leaf, content):
            adopted.append(leaf)
    return adopted


# --- Pending changes ----------------------------------------------------------
#
# A protected file whose on-disk bytes differ from an EXISTING endorsed copy is pending:
# the loaders keep serving the endorsed copy (reconcile), and Settings shows the change
# so the owner can apply or discard it. Read-only here; the writes live with each
# leaf's own endorsing writer (dashboard/handlers/config_pending.py).

#: Most changed keys reported per leaf; the rest are summarized by ``truncated``.
_MAX_CHANGES = 100
_MAX_SHOWN_STR = 200
_MAX_SHOWN_LIST = 50
#: A key path segment that names a credential, or a section whose keys are arbitrary
#: env/header names whose values can be credentials: values under it are withheld
#: (:func:`_secret_segment`). Matched anywhere in the case-folded segment:
_SECRET_WORDS = (
    "token",
    "secret",
    "passw",
    "credential",
    "apikey",
    "api_key",
    "api-key",
    "private",
    "auth",
    "cookie",
    "bearer",
    "signing",
)
#: Matched at the end of the case-folded segment; ``key`` also matches at its start.
_SECRET_SUFFIXES = ("_key", "-key", "env", "header", "headers")
#: The non-ASCII letters a case-insensitive match equates with an ASCII letter that
#: ``str.lower`` leaves apart (dotted and dotless I, long s); the Kelvin sign lowers
#: to ``k`` by itself.
_ASCII_FOLD = str.maketrans({"\u0130": "i", "\u0131": "i", "\u017f": "s"})
_ABSENT = object()


def disk_signature(content: bytes) -> str:
    """Identity of one on-disk version: what the owner reviewed must be what is applied."""
    return hashlib.sha256(content).hexdigest()


def _parse_object(content: bytes) -> dict | None:
    """*content* as a JSON object, or ``None`` when it is not one a config reader can walk.

    A document nested past ``MAX_DOCUMENT_NESTING`` is not one: the decoder can raise
    ``RecursionError`` on it, and the diff and every config reader walk it recursively.
    """
    from kiro_crew.user_json import exceeds_nesting, loads_user_json

    try:
        doc = loads_user_json(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or exceeds_nesting(doc):
        return None
    return doc


def _secret_segment(segment: str) -> bool:
    """Whether one key path segment names a credential or a credential-bearing section."""
    folded = segment.translate(_ASCII_FOLD).lower()
    # One trailing newline does not hide a suffix: "env\n" is withheld like "env".
    tail = folded[:-1] if folded.endswith("\n") else folded
    return (
        any(word in folded for word in _SECRET_WORDS)
        or folded.startswith("key")
        or tail.endswith(_SECRET_SUFFIXES)
    )


def _secret_path(path: list[str]) -> bool:
    return any(_secret_segment(segment) for segment in path)


def _shown(path: list[str], value: Any) -> tuple[bool, Any]:
    """``(True, value)`` when *value* may be shown in the diff, else ``(False, None)``."""
    if _secret_path(path):
        return False, None
    if value is None or isinstance(value, (bool, int, float)):
        return True, value
    if isinstance(value, str):
        if len(value) > _MAX_SHOWN_STR:
            return False, None
        from kiro_crew.security.redaction import redact_credentials

        if redact_credentials(value)[0] != value:
            return False, None
        return True, value
    if isinstance(value, list) and len(value) <= _MAX_SHOWN_LIST:
        if all(not isinstance(i, (list, dict)) and _shown(path, i)[0] for i in value):
            return True, value
    return False, None


def _diff_keys(before: Any, after: Any, path: list[str], out: list[dict]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            if key not in after:
                out.append(_change(path + [str(key)], "removed", before[key], _ABSENT))
            elif key not in before:
                out.append(_change(path + [str(key)], "added", _ABSENT, after[key]))
            elif not strict_json_equal(before[key], after[key]):
                _diff_keys(before[key], after[key], path + [str(key)], out)
        return
    out.append(_change(path, "changed", before, after))


def _change(path: list[str], kind: str, before: Any, after: Any) -> dict:
    entry: dict[str, Any] = {"path": path, "change": kind}
    redacted = False
    for name, value in (("before", before), ("after", after)):
        if value is _ABSENT:
            continue
        ok, view = _shown(path, value)
        if ok:
            entry[name] = view
        elif _secret_path(path) or isinstance(value, str):
            redacted = True
    if redacted:
        entry["redacted"] = True
    return entry


#: The signature of a pending leaf whose file is missing or unreadable: no on-disk
#: bytes to hash, and never equal to a SHA-256 hex digest.
REMOVED_SIGNATURE = "removed"


def _disk_bytes(leaf: str) -> bytes | None:
    """The on-disk bytes of *leaf*, or ``None`` when it is missing or cannot be read."""
    try:
        return (config_dir() / leaf).read_bytes()
    except OSError:
        return None


def pending_changes() -> list[dict]:
    """Each protected leaf whose on-disk bytes differ from its endorsed copy.

    ``[]`` wherever the content check does not apply (:func:`enforcement_active`).
    A leaf with no endorsed copy is not pending (boot adoption endorses it), and a leaf
    whose copy cannot be read is skipped (its loaders already fail closed). A file that
    is missing or unreadable while its copy exists is pending as ``removed`` (signature
    :data:`REMOVED_SIGNATURE`): every reader serves the copy (:func:`reconciled_bytes`),
    and Discard puts the file back. Each entry is
    ``{leaf, signature, disk_valid, removed, changes, truncated}``; ``changes`` lists
    ``{path, change: added|removed|changed, before?, after?, redacted?}``, with a value
    present only when it cannot carry a credential.
    """
    if not enforcement_active():
        return []
    pending: list[dict] = []
    for leaf in PROTECTED_LEAVES:
        try:
            endorsed = endorsed_bytes(leaf)
        except OSError:
            continue
        disk = _disk_bytes(leaf)
        if endorsed is None or endorsed == disk:
            continue
        if disk is None:
            pending.append(
                {
                    "leaf": leaf,
                    "signature": REMOVED_SIGNATURE,
                    "disk_valid": False,
                    "removed": True,
                    "changes": [],
                    "truncated": False,
                }
            )
            continue
        after = _parse_object(disk)
        changes: list[dict] = []
        if after is not None:
            _diff_keys(_parse_object(endorsed) or {}, after, [], changes)
        pending.append(
            {
                "leaf": leaf,
                "signature": disk_signature(disk),
                "disk_valid": after is not None,
                "removed": False,
                "changes": changes[:_MAX_CHANGES],
                "truncated": len(changes) > _MAX_CHANGES,
            }
        )
    return pending


def check_pending(leaf: str, signature: str) -> bytes | None:
    """The on-disk bytes of pending *leaf* (``None``: removed), if still the version reviewed.

    All Discard needs: it restores the endorsed copy whatever the edit holds, so an
    unparseable or removed file can always be discarded. Raises :class:`PendingChanged`
    when the leaf is not pending or its current version is not *signature* (edited again
    after the owner looked).
    """
    disk = _disk_bytes(leaf)
    endorsed = endorsed_bytes(leaf)
    if endorsed is None or endorsed == disk or _signature_of(disk) != signature:
        raise PendingChanged(leaf)
    return disk


def _signature_of(disk: bytes | None) -> str:
    """The signature :func:`pending_changes` reports for on-disk bytes *disk*."""
    return REMOVED_SIGNATURE if disk is None else disk_signature(disk)


def log_writeback_refused_once(leaf: str, message: str) -> None:
    """Log a migration write-back refused because *leaf* is pending, once per version.

    Every config load retries the write-back and is refused again while the file is
    pending; a new edit (a new signature) is logged again.
    """
    key = (leaf, _signature_of(_disk_bytes(leaf)))
    if key in _writeback_refusals_logged:
        return
    _writeback_refusals_logged.add(key)
    logger.warning("Config write-back failed: %s", message)


def read_pending_disk(leaf: str, signature: str) -> dict:
    """The on-disk document of pending *leaf*, for Apply, if still the version reviewed.

    Raises :class:`PendingChanged` as :func:`check_pending` does, :class:`PendingRemoved`
    when the file is missing or unreadable (there is nothing to apply; Discard puts it
    back), and :class:`PendingInvalid` when the on-disk file is not a JSON object.
    """
    disk = check_pending(leaf, signature)
    if disk is None:
        raise PendingRemoved(leaf)
    doc = _parse_object(disk)
    if doc is None:
        raise PendingInvalid(leaf)
    return doc


class PendingChanged(Exception):
    """The leaf is not pending, or not at the version the owner reviewed."""


class PendingInvalid(Exception):
    """The pending on-disk file is not a JSON object, so it cannot be applied."""


class PendingRemoved(Exception):
    """The pending file is missing or unreadable, so there is no edit to apply."""
