"""Remote-libvirt Snapshot plane: internal domain snapshots over qemu+tls (ADR-0428).

`RemoteLibvirtSnapshotter` realizes the `kdive.providers.ports.lifecycle.Snapshotter` port against
the remote host. The domain operations (memory-vs-disk-only ``snapshotCreateXML``, running-vs-paused
``revertToSnapshot``, idempotent ``delete``/``delete_all``) match `LocalLibvirtSnapshotter`
(ADR-0378, #1254); only the connection lifecycle differs — the mutual-TLS materialize→connect→
cleanup of `remote_connection` (ADR-0077), exactly as `RemoteLibvirtControl`. No shared layer with
`local_libvirt` (ADR-0076). DB-free, keyed on the provider domain name: the snapshot/restore/delete
job handlers drive the ledger. All host seams are injected; ``libvirt.open`` runs only under the
``live_vm`` gate.

Snapshots are **internal** (stored inside the remote domain's qcow2), so ``delete``/``delete_all``
free the data with no external object-store cleanup, and a teardown ``delete_all`` + ``undefine``
leaves nothing on the remote pool. ``create``/``revert`` treat an absent domain as an
``INFRASTRUCTURE_FAILURE`` (you cannot snapshot or revert a gone guest); ``delete``/``delete_all``
treat an absent domain or an absent snapshot as success (idempotent), so teardown and cancel-cleanup
never fail on already-gone data. A missing snapshot on ``revert`` is a ``CONFIGURATION_ERROR``.
Connection-establishment faults inherit the shared transport's taxonomy (``CONFIGURATION_ERROR`` for
an unsafe URI or unresolvable TLS secret refs, ``TRANSPORT_FAILURE`` for a mutual-TLS connect).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.lifecycle import Snapshotter as Snapshotter
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, unbound_remote_config
from kdive.providers.remote_libvirt.connection.transport import (
    open_libvirt_protocol,
    remote_connection,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)


# A libvirt snapshot handle is an opaque object the snapshotter only passes back into libvirt
# (``revertToSnapshot``/``delete``); typing it ``Any`` keeps the narrow Protocols satisfiable by
# both the real bindings and test fakes (a Protocol-typed handle fails method-param contravariance).
type _LibvirtSnapshot = Any


class _SnapshotDomain(Protocol):
    def snapshotCreateXML(self, xml: str, flags: int, /) -> _LibvirtSnapshot: ...  # noqa: N802
    def revertToSnapshot(self, snap: _LibvirtSnapshot, flags: int, /) -> int: ...  # noqa: N802
    def snapshotLookupByName(self, name: str, flags: int, /) -> _LibvirtSnapshot: ...  # noqa: N802
    def listAllSnapshots(self, flags: int, /) -> list[_LibvirtSnapshot]: ...  # noqa: N802


class _SnapshotConn(Protocol):
    def lookupByName(self, name: str, /) -> _SnapshotDomain: ...  # noqa: N802
    def close(self) -> None: ...


type OpenSnapshotConnection = Callable[[str], _SnapshotConn]


def open_libvirt_snapshot(uri: str) -> _SnapshotConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return open_libvirt_protocol(uri)


class RemoteLibvirtSnapshotter:
    """The `Snapshotter` for the remote libvirt host (internal RAM+disk/disk-only snapshots)."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
        open_connection: OpenSnapshotConnection = open_libvirt_snapshot,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir

    @classmethod
    def from_env(
        cls,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
    ) -> RemoteLibvirtSnapshotter:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry, config_factory=config_factory)

    def create(self, domain_name: str, name: str, *, include_memory: bool) -> None:
        """Create a named internal snapshot; pre-deletes any same-name snapshot first.

        ``include_memory`` (running guest) yields a full system checkpoint (RAM+CPU+disk),
        written on the remote host; otherwise a disk-only snapshot
        (``VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY``).

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` for an absent domain or a libvirt
                snapshot fault; ``CONFIGURATION_ERROR`` / ``TRANSPORT_FAILURE`` from the shared
                transport for an invalid connection config or a mutual-TLS connect fault.
        """
        with self._connection() as conn:
            domain = self._lookup_required(conn, domain_name)
            self._delete_if_exists(domain, domain_name, name)
            # ``name`` is validated to ``[A-Za-z0-9._-]`` at the tool boundary, so it carries no
            # XML-special characters — the minimal snapshot XML below is injection-safe.
            xml = f"<domainsnapshot><name>{name}</name></domainsnapshot>"
            flags = 0 if include_memory else libvirt.VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY
            try:
                domain.snapshotCreateXML(xml, flags)
            except libvirt.libvirtError as exc:
                raise self._infra("creating snapshot on", domain_name) from exc

    def revert(self, domain_name: str, name: str, *, start_paused: bool) -> None:
        """Revert a domain to a named snapshot, resuming running or leaving it paused.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a missing snapshot,
                ``INFRASTRUCTURE_FAILURE`` for an absent domain or a libvirt revert fault;
                ``CONFIGURATION_ERROR`` / ``TRANSPORT_FAILURE`` from the shared transport for an
                invalid connection config or a mutual-TLS connect fault.
        """
        with self._connection() as conn:
            domain = self._lookup_required(conn, domain_name)
            snap = self._lookup_snapshot(domain, domain_name, name)
            flags = (
                libvirt.VIR_DOMAIN_SNAPSHOT_REVERT_PAUSED
                if start_paused
                else libvirt.VIR_DOMAIN_SNAPSHOT_REVERT_RUNNING
            )
            try:
                domain.revertToSnapshot(snap, flags)
            except libvirt.libvirtError as exc:
                raise self._infra("reverting", domain_name) from exc

    def delete(self, domain_name: str, name: str) -> None:
        """Delete a named snapshot; idempotent (absent domain or snapshot is a no-op).

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` for a libvirt delete fault;
                ``CONFIGURATION_ERROR`` / ``TRANSPORT_FAILURE`` from the shared transport.
        """
        with self._connection() as conn:
            domain = self._lookup_optional(conn, domain_name)
            if domain is not None:
                self._delete_if_exists(domain, domain_name, name)

    def delete_all(self, domain_name: str) -> None:
        """Delete every snapshot of a domain; idempotent. Called at teardown before ``undefine``.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` for a libvirt list/delete fault;
                ``CONFIGURATION_ERROR`` / ``TRANSPORT_FAILURE`` from the shared transport.
        """
        with self._connection() as conn:
            domain = self._lookup_optional(conn, domain_name)
            if domain is None:
                return
            try:
                snapshots = domain.listAllSnapshots(0)
            except libvirt.libvirtError as exc:
                raise self._infra("listing snapshots on", domain_name) from exc
            for snap in snapshots:
                self._delete_snapshot(snap, domain_name)

    def _connection(self) -> AbstractContextManager[_SnapshotConn]:
        return remote_connection(
            self._config_factory(),
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )

    def _lookup_required(self, conn: _SnapshotConn, domain_name: str) -> _SnapshotDomain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise self._infra("looking up", domain_name) from exc

    def _lookup_optional(self, conn: _SnapshotConn, domain_name: str) -> _SnapshotDomain | None:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            raise self._infra("looking up", domain_name) from exc

    def _lookup_snapshot(
        self, domain: _SnapshotDomain, domain_name: str, name: str
    ) -> _LibvirtSnapshot:
        try:
            return domain.snapshotLookupByName(name, 0)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_SNAPSHOT:
                raise CategorizedError(
                    f"snapshot {name!r} not found",
                    category=ErrorCategory.CONFIGURATION_ERROR,
                    details={"domain": domain_name, "snapshot": name},
                ) from exc
            raise self._infra("looking up snapshot on", domain_name) from exc

    def _delete_if_exists(self, domain: _SnapshotDomain, domain_name: str, name: str) -> None:
        try:
            snap = domain.snapshotLookupByName(name, 0)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN_SNAPSHOT:
                return
            raise self._infra("looking up snapshot on", domain_name) from exc
        self._delete_snapshot(snap, domain_name)

    def _delete_snapshot(self, snap: _LibvirtSnapshot, domain_name: str) -> None:
        try:
            snap.delete(0)
        except libvirt.libvirtError as exc:
            raise self._infra("deleting snapshot on", domain_name) from exc

    @staticmethod
    def _infra(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"domain": domain_name},
        )


__all__ = ["RemoteLibvirtSnapshotter"]
