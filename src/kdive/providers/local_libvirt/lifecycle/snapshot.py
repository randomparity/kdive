"""Local-libvirt Snapshot plane: create/revert/delete internal domain snapshots (ADR-0378).

`LocalLibvirtSnapshotter` looks a domain up by name over an injected connection factory and
drives libvirt's ``virDomainSnapshot*`` API. Snapshots are **internal** (stored inside the
domain's qcow2), so deleting them frees the data with no external object-store cleanup, and a
teardown ``delete_all`` + ``undefine`` leaves nothing behind. DB-free: it owns no Postgres; the
snapshot/restore/delete job handlers drive the ledger. It implements the
`kdive.providers.ports.lifecycle.Snapshotter` typed port. Unit tests inject a fake connection;
the real ``libvirt.open`` adapter is ``live_vm``-only.

``create``/``revert`` treat an absent domain as an ``INFRASTRUCTURE_FAILURE`` (you cannot
snapshot or revert a gone guest); ``delete``/``delete_all`` treat an absent domain or an absent
snapshot as success (idempotent), so teardown and cancel-cleanup never fail on already-gone data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

import libvirt

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.ports.lifecycle import Snapshotter as Snapshotter

_log = logging.getLogger(__name__)


# A libvirt snapshot handle is an opaque object the snapshotter only passes back into libvirt
# (``revertToSnapshot``/``delete``); typing it ``Any`` keeps these narrow Protocols satisfiable by
# both the real bindings and test fakes (a Protocol-typed handle would fail method-param
# contravariance). Params are positional-only (``/``) so the match ignores the bindings' parameter
# names (e.g. ``xmlDesc``).
type _LibvirtSnapshot = Any


class _LibvirtDomain(Protocol):
    def snapshotCreateXML(self, xml: str, flags: int, /) -> _LibvirtSnapshot: ...  # noqa: N802
    def revertToSnapshot(self, snap: _LibvirtSnapshot, flags: int, /) -> int: ...  # noqa: N802
    def snapshotLookupByName(self, name: str, flags: int, /) -> _LibvirtSnapshot: ...  # noqa: N802
    def listAllSnapshots(self, flags: int, /) -> list[_LibvirtSnapshot]: ...  # noqa: N802


class _LibvirtConn(Protocol):
    def lookupByName(self, name: str, /) -> _LibvirtDomain: ...  # noqa: N802
    def close(self, /) -> int: ...


type Connect = Callable[[], _LibvirtConn]


def _close(conn: _LibvirtConn) -> None:
    """Close a libvirt connection, swallowing a close-time error (best-effort cleanup)."""
    try:
        conn.close()
    except libvirt.libvirtError:
        _log.warning("libvirt connection close failed; continuing", exc_info=True)


class LocalLibvirtSnapshotter:
    """The `Snapshotter` for the local libvirt host (internal RAM+disk/disk-only snapshots)."""

    def __init__(self, *, connect: Connect) -> None:
        self._connect = connect

    @classmethod
    def from_env(cls) -> LocalLibvirtSnapshotter:
        """Build from ``KDIVE_LIBVIRT_URI`` (default ``qemu:///system``); does not connect."""
        host_uri = config.require(LIBVIRT_URI)
        return cls(connect=lambda: libvirt.open(host_uri))

    def create(self, domain_name: str, name: str, *, include_memory: bool) -> None:
        """Create a named internal snapshot; pre-deletes any same-name snapshot first.

        ``include_memory`` (running guest) yields a full system checkpoint (RAM+CPU+disk);
        otherwise a disk-only snapshot (``VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY``).

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` for an absent domain or a libvirt
                snapshot fault.
        """
        conn = self._open()
        try:
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
        finally:
            _close(conn)

    def revert(self, domain_name: str, name: str, *, start_paused: bool) -> None:
        """Revert a domain to a named snapshot, resuming running or leaving it paused.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a missing snapshot,
                ``INFRASTRUCTURE_FAILURE`` for an absent domain or a libvirt revert fault.
        """
        conn = self._open()
        try:
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
        finally:
            _close(conn)

    def delete(self, domain_name: str, name: str) -> None:
        """Delete a named snapshot; idempotent (absent domain or snapshot is a no-op).

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` for a libvirt delete fault.
        """
        conn = self._open()
        try:
            domain = self._lookup_optional(conn, domain_name)
            if domain is not None:
                self._delete_if_exists(domain, domain_name, name)
        finally:
            _close(conn)

    def delete_all(self, domain_name: str) -> None:
        """Delete every snapshot of a domain; idempotent. Called at teardown before ``undefine``.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` for a libvirt list/delete fault.
        """
        conn = self._open()
        try:
            domain = self._lookup_optional(conn, domain_name)
            if domain is None:
                return
            try:
                snapshots = domain.listAllSnapshots(0)
            except libvirt.libvirtError as exc:
                raise self._infra("listing snapshots on", domain_name) from exc
            for snap in snapshots:
                self._delete_snapshot(snap, domain_name)
        finally:
            _close(conn)

    def _open(self) -> _LibvirtConn:
        try:
            return self._connect()
        except libvirt.libvirtError as exc:
            raise self._infra("connecting to libvirt for", "snapshot") from exc

    def _lookup_required(self, conn: _LibvirtConn, domain_name: str) -> _LibvirtDomain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise self._infra("looking up", domain_name) from exc

    def _lookup_optional(self, conn: _LibvirtConn, domain_name: str) -> _LibvirtDomain | None:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            raise self._infra("looking up", domain_name) from exc

    def _lookup_snapshot(
        self, domain: _LibvirtDomain, domain_name: str, name: str
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

    def _delete_if_exists(self, domain: _LibvirtDomain, domain_name: str, name: str) -> None:
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
