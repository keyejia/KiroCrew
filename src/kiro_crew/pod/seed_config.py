"""The config text a pod's seed or restart rewrite publishes into the pod home.

Split from :mod:`kiro_crew.pod.runtime_home`, whose two seeding paths (descriptor
and directory) both call :func:`_seed_config_text`. The seed config floor stays in
:mod:`kiro_crew.pod.runtime` and is read from there at call time, the namespace the
pod suite patches.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew import user_json
from kiro_crew.config import loosening_provenance
from kiro_crew.pod import runtime
from kiro_crew.pod.runtime import PodError

_SEED_CONFIG_LIMIT = 1024 * 1024


def _seed_config_text(raw: bytes | None, home: Path) -> tuple[str, bool]:
    """The floored config text to endorse in pod *home*, and whether to publish it to disk.

    *raw* is the pod's on-disk ``config.json`` (``None`` when there is none). Where the
    sandbox seals the keystone files and the pod already holds an endorsed copy, the
    rewrite starts from that COPY, not from *raw*: the pod gateway loads the copy, so a
    raw file that differs from it is an edit nobody reviewed, and parsing it here would
    endorse it on restart. Such a file is left as it is, so it stays pending in the
    pod's Settings for the owner to apply or discard. The raw file is the source only
    on a first seed (no copy yet) or where nothing seals these files, in which case
    the pod gateway loads the raw file anyway.
    """
    try:
        endorsed = (
            loosening_provenance.endorsed_bytes(loosening_provenance.LEAF_CONFIG, home=home)
            if loosening_provenance.enforcement_active()
            else None
        )
    except OSError as exc:
        raise PodError(f"could not read the pod's endorsed config.json: {exc}") from exc
    source = raw if endorsed is None else endorsed
    data: dict = {}
    if source is not None:
        if len(source) > _SEED_CONFIG_LIMIT:
            raise PodError("seeded config.json exceeds the 1 MiB pod setup limit")
        try:
            loaded = json.loads(user_json.strip_utf8_bom(source.decode("utf-8")))
        except (UnicodeError, ValueError) as exc:
            raise PodError(f"seeded config.json is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise PodError("seeded config.json must contain a JSON object")
        data = loaded
    runtime._apply_seed_config_floor(data)
    return json.dumps(data, indent=2), endorsed is None or raw == endorsed
