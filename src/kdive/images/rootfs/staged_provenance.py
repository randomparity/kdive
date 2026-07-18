"""The build-fs → reconcile provenance sidecar (#977, ADR-0296).

``build-fs`` produces a staged rootfs qcow2 at a path and discards the build's recorded
provenance; a later inventory reconcile registers the ``staged-path`` catalog row but never
carried provenance, so the ``direct_kernel``/``kdump`` capability signals read ``unverified`` for
local fixtures. This module is the file-based bridge: ``write_sidecar`` records
``RootfsBuildOutput.provenance`` beside the qcow2, and ``read_sidecar`` reads it back for reconcile.

The sidecar is a **validated boundary, not a trusted input**: unlike the publish path (provenance
computed server-side), the sidecar is a file on disk and ``images.describe`` echoes a row's
``provenance`` verbatim to agents. ``read_sidecar`` therefore bounds the read (a byte cap and an
object-shape check) and degrades to ``None`` on anything malformed, so a junk or oversized sidecar
can neither bloat the row nor the agent-facing response, and a missing sidecar simply keeps the row
at its honest ``unverified``. The bound is a byte cap plus object-shape check — **not** a per-key
type allowlist — so a future provenance operand flows through without a reconcile change.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

_log = logging.getLogger(__name__)

#: The sidecar format discriminator; an unrecognized value degrades to "no sidecar" on read.
SIDECAR_SCHEMA = "kdive.staged-provenance.v1"

#: Upper bound on a sidecar's size. Provenance is a dozen keys plus a small package map, far under
#: this; the cap is a defense against an oversized on-disk file reaching the row / agent response.
_SIDECAR_MAX_BYTES = 64 * 1024

#: Upper bound on a captured ``.config`` sibling. A ``/boot/config-<ver>`` is ~250 KiB; cap
#: generously so a legitimate config always passes while a runaway on-disk file is still rejected
#: before it lands in memory (ADR-0336).
_CONFIG_MAX_BYTES = 4 * 1024 * 1024


def sidecar_path(qcow2: Path) -> Path:
    """The sidecar path for ``qcow2``: ``<qcow2-path>.provenance.json``.

    The suffix is appended (not substituted) so the sidecar is unambiguously bound to a specific
    qcow2 filename rather than colliding with a differently-suffixed sibling.
    """
    return Path(f"{qcow2}.provenance.json")


def write_sidecar(qcow2: Path, *, provenance: Mapping[str, object]) -> None:
    """Atomically write ``provenance`` to ``qcow2``'s sidecar as ``{schema, provenance}``.

    Writes to a temporary file in the destination directory and ``os.replace``\\ s it onto the
    sidecar path, so a concurrent reader never observes a partially-written document.

    Raises:
        OSError: The temporary write or the rename failed. The caller decides whether to degrade
            (``build-fs`` treats a sidecar-write failure as advisory and does not fail the build).
    """
    target = sidecar_path(qcow2)
    document = json.dumps({"schema": SIDECAR_SCHEMA, "provenance": provenance})
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".provenance-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(document)
        # mkstemp creates 0600; make readable before the atomic replace so readers
        # (e.g. reconcile-systems run as a different user) never see a 0600 sidecar.
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def read_sidecar(qcow2: Path) -> dict[str, object] | None:
    """Return ``qcow2``'s sidecar provenance dict, or ``None`` if there is no usable sidecar.

    A usable sidecar is present, at most :data:`_SIDECAR_MAX_BYTES`, a JSON object with
    ``schema == SIDECAR_SCHEMA`` and a ``provenance`` that is itself a JSON object. Anything else —
    absent, unreadable, over-cap, non-JSON, non-object, wrong/missing schema, or a non-object
    ``provenance`` — returns ``None`` and never raises. A present-but-invalid sidecar is logged at
    warning (an absent one is silent, so a legitimately pre-feature row does not spam the log).
    """
    path = sidecar_path(qcow2)
    raw = _read_bounded(path)
    if raw is None:
        return None
    try:
        document = json.loads(raw)
    except ValueError, UnicodeDecodeError:
        _log.warning("staged-provenance sidecar %s is not valid JSON; ignoring", path)
        return None
    if not isinstance(document, dict) or document.get("schema") != SIDECAR_SCHEMA:
        _log.warning("staged-provenance sidecar %s has an unrecognized schema; ignoring", path)
        return None
    provenance = document.get("provenance")
    if not isinstance(provenance, dict):
        _log.warning("staged-provenance sidecar %s has a non-object provenance; ignoring", path)
        return None
    return provenance


def _read_bounded(path: Path) -> bytes | None:
    """Read up to the cap from ``path``; ``None`` if absent, unreadable, or over the cap.

    Reads at most ``_SIDECAR_MAX_BYTES + 1`` bytes so an oversized sidecar is rejected without ever
    landing fully in memory.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(_SIDECAR_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        _log.warning("staged-provenance sidecar %s could not be read; ignoring", path)
        return None
    if len(data) > _SIDECAR_MAX_BYTES:
        _log.warning(
            "staged-provenance sidecar %s exceeds %d bytes; ignoring", path, _SIDECAR_MAX_BYTES
        )
        return None
    return data


def config_sibling_path(qcow2: Path) -> Path:
    """The kernel-config sibling path for ``qcow2``: ``<qcow2-path>.config`` (ADR-0336).

    Like :func:`sidecar_path`, the suffix is appended (not substituted) so the sibling is bound to a
    specific qcow2 filename. Carries the build's captured ``/boot/config-<ver>`` bytes for the
    reconcile to read and upload.
    """
    return Path(f"{qcow2}.config")


def write_config_sibling(qcow2: Path, *, config: bytes) -> None:
    """Atomically write the captured kernel ``config`` bytes to ``qcow2``'s ``.config`` sibling.

    Writes to a temp file in the destination directory and ``os.replace``\\ s it onto the sibling
    path, so a concurrent reader never sees a partial file — the same durability the provenance
    sidecar uses.

    Raises:
        OSError: The temporary write or the rename failed. The caller decides whether to degrade
            (``build-fs`` treats a sibling-write failure as advisory and does not fail the build).
    """
    target = config_sibling_path(qcow2)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(config)
        # mkstemp creates 0600; make readable before the atomic replace so readers
        # (e.g. reconcile-systems run as a different user) never see a 0600 sibling.
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
    except OSError:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def read_config_sibling(qcow2: Path) -> bytes | None:
    """Return ``qcow2``'s ``.config`` sibling bytes, or ``None`` if there is no usable sibling.

    A usable sibling is present, readable, and at most :data:`_CONFIG_MAX_BYTES`. Anything else —
    absent, unreadable, or over-cap — returns ``None`` and never raises (an absent sibling is
    silent; an unreadable or oversized one is logged at warning). The reconcile treats ``None`` as
    "no config captured" and preserves the row's existing offer.
    """
    path = config_sibling_path(qcow2)
    try:
        with path.open("rb") as handle:
            data = handle.read(_CONFIG_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        _log.warning("kernel-config sibling %s could not be read; ignoring", path)
        return None
    if len(data) > _CONFIG_MAX_BYTES:
        _log.warning("kernel-config sibling %s exceeds %d bytes; ignoring", path, _CONFIG_MAX_BYTES)
        return None
    return data
