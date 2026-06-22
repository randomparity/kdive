"""Writeback adapter seam: persist the exported inventory to the live source (#641, ADR-0199).

Sub-issue C (#640) added ``ops.export_systems_toml``, which serializes the live inventory to a
``systems.toml`` document. That text is inert. This module turns it into a write against the source
the reconciler re-reads (``KDIVE_SYSTEMS_TOML``), behind the explicit ``KDIVE_INVENTORY_WRITEBACK``
opt-in, so an operator-invoked writeback updates the live source and a pod restart reproduces the
inventory from it.

The seam is a port (:class:`WritebackTarget`) with two real implementations and a fake:

* :class:`ConfigMapWriteback` — patches the ``kdive-systems`` ConfigMap via the Kubernetes API using
  the in-cluster service-account token + CA (needs an RBAC Role granting ``patch`` on that one
  ConfigMap). The ConfigMap mount is read-only in the pod, so the API patch is the only write path;
  kubelet propagates the change to the mount.
* :class:`MountedFileWriteback` — writes the ``KDIVE_SYSTEMS_TOML`` file directly (atomic replace).
  Only usable where that path is a writable volume shared with the reconciler.
* :class:`FakeWriteback` — records the last write for the tool tests.

:func:`assert_persistable` is the skeleton guard: a document still carrying a ``REPLACE_ME_*``
placeholder (an unedited ``remote_libvirt`` skeleton or a ``defined`` image) does not parse, so
persisting it would silently stall the reconciler's inventory pass. It is refused before any write.
The marker is :data:`kdive.inventory.serialize.REMOTE_PLACEHOLDER_PREFIX`, shared with the
serializer so the two cannot drift.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path
from typing import Protocol

import httpx

import kdive.config as config
from kdive.config.core_settings import (
    INVENTORY_WRITEBACK,
    INVENTORY_WRITEBACK_CONFIGMAP,
    SYSTEMS_TOML,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.inventory.path import systems_toml_path
from kdive.inventory.serialize import REMOTE_PLACEHOLDER_PREFIX

__all__ = [
    "WRITEBACK_PLACEHOLDER_MARKER",
    "WritebackTarget",
    "FakeWriteback",
    "MountedFileWriteback",
    "ConfigMapWriteback",
    "assert_persistable",
    "resolve_writeback_target",
]

# The marker an incomplete export carries: a quote immediately followed by the serializer's
# placeholder prefix, i.e. the *emitted value* form (``key = "REPLACE_ME_..."``). The leading quote
# is load-bearing — the export header explains the placeholders in prose ("Replace every
# REPLACE_ME_* value"), so matching the bare prefix would wrongly flag every clean export. A drift
# test asserts this marker appears in a freshly-serialized skeleton but not in a clean one.
WRITEBACK_PLACEHOLDER_MARKER = f'"{REMOTE_PLACEHOLDER_PREFIX}'

# The inventory ConfigMap key is the file name the reconciler reads; it matches the chart's
# `systems.fileName` default.
_CONFIGMAP_KEY = "systems.toml"

# The standard in-cluster service-account mount; overridable in tests.
_SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")

_WRITE_TIMEOUT_SECONDS = 15.0


def assert_persistable(toml_text: str) -> None:
    """Refuse to persist a document that still carries a skeleton placeholder.

    A ``remote_libvirt`` block (and a ``defined`` image) is exported with ``REPLACE_ME_*``
    placeholders for fields not stored in the DB (ADR-0199). Those are required fields, so an
    unedited skeleton does not parse; persisting it would feed the reconciler a malformed
    ``systems.toml`` that silently stalls the inventory pass. The operator must complete every
    placeholder first.

    Args:
        toml_text: The document about to be written (a live serialization or an operator-supplied
            completed document).

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` if ``toml_text`` still contains the placeholder
            marker.
    """
    if WRITEBACK_PLACEHOLDER_MARKER in toml_text:
        raise CategorizedError(
            f"refusing to persist a document containing the skeleton placeholder "
            f"{WRITEBACK_PLACEHOLDER_MARKER!r}: complete every placeholder (the file-only "
            f"remote_libvirt connection/debug fields and any defined-image object_key) before "
            f"persisting it as a live source",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"marker": WRITEBACK_PLACEHOLDER_MARKER},
        )


class WritebackTarget(Protocol):
    """A port that persists a ``systems.toml`` document to the live source."""

    target_kind: str

    async def write(self, toml_text: str) -> None:
        """Persist ``toml_text``, or raise :class:`CategorizedError` on failure."""
        ...


class FakeWriteback:
    """An in-memory :class:`WritebackTarget` for tests; records the last write."""

    target_kind = "fake"

    def __init__(self, *, fail: CategorizedError | None = None) -> None:
        self._fail = fail
        self.written: str | None = None

    async def write(self, toml_text: str) -> None:
        if self._fail is not None:
            raise self._fail
        self.written = toml_text


class MountedFileWriteback:
    """Write the inventory file directly via an atomic temp-file + replace."""

    target_kind = "file"

    def __init__(self, path: Path) -> None:
        self._path = path

    async def write(self, toml_text: str) -> None:
        await asyncio.to_thread(self._write_atomic, toml_text)

    def _write_atomic(self, toml_text: str) -> None:
        directory = self._path.parent
        try:
            fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".systems-", suffix=".tmp")
        except OSError as exc:
            raise CategorizedError(
                f"cannot write the inventory file at {self._path}: {exc.strerror}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"path": str(self._path)},
            ) from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(toml_text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self._path)
        except OSError as exc:
            _unlink_quietly(tmp_name)
            raise CategorizedError(
                f"cannot write the inventory file at {self._path}: {exc.strerror}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"path": str(self._path)},
            ) from exc


class ConfigMapWriteback:
    """Patch the ``data`` of a named ConfigMap via the Kubernetes API (least-privilege RBAC)."""

    target_kind = "configmap"

    def __init__(
        self,
        *,
        namespace: str,
        name: str,
        key: str,
        token: str,
        api_base: str,
        verify: str | bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._namespace = namespace
        self._name = name
        self._key = key
        self._token = token
        self._api_base = api_base.rstrip("/")
        self._verify = verify
        self._transport = transport

    @classmethod
    def from_in_cluster(
        cls, *, name: str, key: str, service_account_dir: Path | None = None
    ) -> ConfigMapWriteback:
        """Build the adapter from the in-cluster service-account mount + the API env vars.

        Args:
            name: The ConfigMap name to patch.
            key: The ConfigMap data key (the inventory file name).
            service_account_dir: Override for the service-account mount (tests).

        Returns:
            A ready adapter.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` when not running in a pod (token, namespace,
                CA, or API host missing) — the operator opted in outside Kubernetes.
        """
        sa_dir = service_account_dir or _SERVICE_ACCOUNT_DIR
        token = _read_sa_file(sa_dir / "token", "service-account token")
        namespace = _read_sa_file(sa_dir / "namespace", "pod namespace")
        ca_path = sa_dir / "ca.crt"
        if not ca_path.is_file():
            raise _not_in_cluster("service-account CA (ca.crt)")
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise _not_in_cluster("KUBERNETES_SERVICE_HOST")
        return cls(
            namespace=namespace,
            name=name,
            key=key,
            token=token,
            api_base=f"https://{host}:{port}",
            verify=str(ca_path),
        )

    async def write(self, toml_text: str) -> None:
        url = f"{self._api_base}/api/v1/namespaces/{self._namespace}/configmaps/{self._name}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/strategic-merge-patch+json",
            "Accept": "application/json",
        }
        body = {"data": {self._key: toml_text}}
        try:
            async with self._client() as client:
                response = await client.patch(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise CategorizedError(
                f"ConfigMap writeback transport failure ({type(exc).__name__})",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"target": self.target_kind, "error": type(exc).__name__},
            ) from exc
        self._raise_for_status(response.status_code)

    def _client(self) -> httpx.AsyncClient:
        if self._transport is not None:
            return httpx.AsyncClient(transport=self._transport, timeout=_WRITE_TIMEOUT_SECONDS)
        return httpx.AsyncClient(verify=self._verify, timeout=_WRITE_TIMEOUT_SECONDS)

    def _raise_for_status(self, status: int) -> None:
        if 200 <= status < 300:
            return
        if status in (401, 403):
            raise CategorizedError(
                f"ConfigMap writeback denied ({status}): the RBAC Role is missing or does not "
                f"grant patch on the {self._name!r} ConfigMap",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"configmap": self._name, "status": status},
            )
        # The response body can echo cluster internals; surface the status only, never the body.
        raise CategorizedError(
            f"ConfigMap writeback failed with HTTP {status}",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"target": self.target_kind, "status": status},
        )


def resolve_writeback_target() -> WritebackTarget | None:
    """Select the writeback adapter from ``KDIVE_INVENTORY_WRITEBACK`` (the opt-in).

    Returns:
        ``None`` when writeback is off (unset / ``off``); otherwise the configured adapter.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` on an unknown value, for ``file`` when
            ``KDIVE_SYSTEMS_TOML`` is unset (the reconciler does not read the per-user XDG
            default, so a writeback there would be silently lost), or (for ``configmap``) when
            not running in a pod.
    """
    selected = (config.get(INVENTORY_WRITEBACK) or "off").strip().lower()
    if selected in ("", "off"):
        return None
    if selected == "configmap":
        name = config.get(INVENTORY_WRITEBACK_CONFIGMAP) or "kdive-systems"
        return ConfigMapWriteback.from_in_cluster(name=name, key=_CONFIGMAP_KEY)
    if selected == "file":
        if config.get(SYSTEMS_TOML) is None:
            raise CategorizedError(
                f"{INVENTORY_WRITEBACK.name}=file requires {SYSTEMS_TOML.name} to name the "
                "writable inventory volume shared with the reconciler; refusing to fall back to "
                "the per-user XDG default the reconciler does not read",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"variable": SYSTEMS_TOML.name},
            )
        return MountedFileWriteback(systems_toml_path())
    raise CategorizedError(
        f"unknown {INVENTORY_WRITEBACK.name} value {selected!r}",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={
            "variable": INVENTORY_WRITEBACK.name,
            "accepted_values": ["off", "configmap", "file"],
        },
    )


def _read_sa_file(path: Path, what: str) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise _not_in_cluster(what) from exc
    if not value:
        raise _not_in_cluster(what)
    return value


def _not_in_cluster(what: str) -> CategorizedError:
    return CategorizedError(
        f"ConfigMap writeback requires running in a Kubernetes pod, but the {what} is not "
        f"available; set KDIVE_INVENTORY_WRITEBACK=off or run in-cluster",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"variable": INVENTORY_WRITEBACK.name},
    )


def _unlink_quietly(name: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(name)
