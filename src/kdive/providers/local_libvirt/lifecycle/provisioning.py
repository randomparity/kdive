"""Local-libvirt Provisioning plane: define/start and destroy/undefine a tagged domain (ADR-0025).

`LocalLibvirtProvisioning` renders a domain XML from a `ProvisioningProfile` (tagged with the
System id in the kdive metadata element discovery reads), `defineXML`+`create`s it on
`provision`, and `destroy`+`undefine`s it idempotently on `teardown`, over an injected
connection factory (unit tests never touch a real host; the real `libvirt.open` adapter is
`live_vm`-only). It owns no Postgres — the `systems.*` handlers drive the state machine.

Storage file lifecycle is delegated to ``lifecycle.storage`` and pure XML rendering to
``lifecycle.xml``. This facade owns materialization, libvirt define/start, and teardown
orchestration.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt

import kdive.config as config
from kdive.components.references import (
    ArtifactComponentRef,
    CatalogComponentRef,
    LocalComponentRef,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    RootfsSource,
    _UploadRootfs,
    validate_rootfs_reference,
)
from kdive.providers.local_libvirt.lifecycle.baseline_kernel import (
    BaselineKernel,
    ExtractBaselineKernel,
    _real_extract_baseline_kernel,
)
from kdive.providers.local_libvirt.lifecycle.materialize import (
    CatalogFetch,
    MaterializableRootfsRef,
    RootfsMaterializationContext,
    RootfsUploadContext,
    materialize_rootfs_base,
)
from kdive.providers.local_libvirt.lifecycle.overlay_customize import OverlayCustomizer
from kdive.providers.local_libvirt.lifecycle.rootfs_catalog_fetch import (
    rootfs_catalog_fetch_from_env,
)
from kdive.providers.local_libvirt.lifecycle.storage import (
    ROOTFS_DIR,
    ProvisioningFiles,
    baseline_dir,
    overlay_path,
)
from kdive.providers.local_libvirt.lifecycle.xml import render_domain_xml
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.shared.libvirt_xml import recorded_gdb_port, recorded_ssh_port
from kdive.providers.shared.runtime_paths import console_log_path, domain_name_for

__all__ = [
    "LocalLibvirtProvisioning",
    "ProvisioningFiles",
    "console_log_path",
    "domain_name_for",
    "overlay_path",
    "reject_rootfs_without_upload_window",
    "render_domain_xml",
]

_log = logging.getLogger(__name__)


class _LibvirtDomain(Protocol):
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def undefine(self) -> int: ...
    def XMLDesc(self, flags: int) -> str: ...  # noqa: N802 - mirrors the libvirt binding name


class _LibvirtConn(Protocol):
    def defineXML(self, xml: str) -> _LibvirtDomain: ...
    def lookupByName(self, name: str) -> _LibvirtDomain: ...
    def close(self) -> int: ...


type Connect = Callable[[], _LibvirtConn]
type FreePort = Callable[[], int]


def _bind_probe_free_port() -> int:  # pragma: no cover - live_vm
    """Bind a loopback socket to port 0 and return the OS-assigned port (then release it).

    The brief release-then-rebind window is accepted (loopback, single-host, single-attach): a
    collision surfaces as a clean domain-start failure the transactional ``provision`` already
    handles (ADR-0210 §2).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _close(conn: _LibvirtConn) -> None:
    """Close a libvirt connection, swallowing a close-time error (best-effort cleanup)."""
    try:
        conn.close()
    except libvirt.libvirtError:
        _log.warning("libvirt connection close failed; continuing", exc_info=True)


def reject_rootfs_without_upload_window(rootfs: RootfsSource) -> None:
    """Reject an ``upload`` rootfs in a lane that has no pre-provision upload window.

    An ``upload`` rootfs resolves a System-owned object that exists only after
    ``systems.define`` opens an upload window and the agent PUTs it (ADR-0048 §5). The
    one-step ``systems.provision`` *create* lane and ``systems.reprovision`` have no such
    window, so an ``upload`` reference there can never have a staged object — fail fast at the
    boundary rather than insert/replace and dead-letter (or leak a started domain) at commit.
    ``define`` and the worker do **not** call this guard.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` for an ``upload`` rootfs.
    """
    if isinstance(rootfs, _UploadRootfs):
        raise CategorizedError(
            "rootfs 'upload' kind requires systems.define + artifacts.create_system_upload first; "
            "use 'local' or 'catalog' for a one-step provision",
            category=ErrorCategory.CONFIGURATION_ERROR,
        )


type MaterializeRootfs = Callable[[RootfsSource, UUID, str], str]


def _materializable_rootfs(rootfs: RootfsSource) -> MaterializableRootfsRef:
    if isinstance(rootfs, LocalComponentRef | CatalogComponentRef | _UploadRootfs):
        return rootfs
    if isinstance(rootfs, ArtifactComponentRef):
        raise CategorizedError(
            "artifact-backed rootfs materialization is not wired yet",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    raise CategorizedError(
        "unsupported rootfs component reference",
        category=ErrorCategory.CONFIGURATION_ERROR,
    )


class LocalLibvirtProvisioning:
    """The realized provisioning port for the local libvirt host."""

    def __init__(
        self,
        *,
        connect: Connect,
        files: ProvisioningFiles | None = None,
        allowed_roots: list[Path] | None = None,
        materialize_rootfs: MaterializeRootfs | None = None,
        catalog_fetch: CatalogFetch | None = None,
        free_port: FreePort | None = None,
        extract_baseline_kernel: ExtractBaselineKernel | None = None,
    ) -> None:
        self._connect = connect
        self._files = files or ProvisioningFiles()
        self._allowed_roots = allowed_roots or [Path(ROOTFS_DIR)]
        self._catalog_fetch = catalog_fetch
        self._materialize_rootfs = materialize_rootfs or self._materialize_rootfs_base
        self._free_port = free_port or _bind_probe_free_port
        self._extract_baseline_kernel = extract_baseline_kernel or _real_extract_baseline_kernel

    @classmethod
    def from_env(cls) -> LocalLibvirtProvisioning:
        """Build from ``KDIVE_LIBVIRT_URI`` (default ``qemu:///system``); does not connect.

        Wires the ``catalog`` rootfs lane (ADR-0228): the catalog fetch lazily opens its own DB
        connection + object store per call, so constructing the provisioner opens nothing.
        """
        host_uri = config.require(LIBVIRT_URI)
        allowed_roots = [Path(ROOTFS_DIR)]
        # `virConnect` structurally satisfies the narrow `_LibvirtConn` Protocol (only
        # `defineXML`/`lookupByName`), so no suppression is needed at this seam.
        return cls(
            connect=lambda: libvirt.open(host_uri),
            allowed_roots=allowed_roots,
            catalog_fetch=rootfs_catalog_fetch_from_env(allowed_roots),
        )

    def provision(
        self,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        overlay_customizers: tuple[OverlayCustomizer, ...] = (),
    ) -> str:
        """Define and start the tagged domain; return its name.

        Idempotent: ``defineXML`` redefines an existing domain, and a ``create`` that reports
        the domain is **already running** (``VIR_ERR_OPERATION_INVALID``) is the desired
        post-state, not a failure — so a handler retry after a partial provision does not mark a
        running System failed. The overlay is created only when **absent**: a retry must never
        recreate the overlay a running QEMU holds open (qemu-img would fail the lock or truncate
        the live disk), so a present overlay is left in place (ADR-0060).

        ``overlay_customizers`` (ADR-0289, #963) run in order against the overlay **only when
        this call created it** — never on the reuse/retry path, so a retry against a running QEMU
        never re-mutates a live disk. A customizer failure reclaims the just-created overlay, like
        any other ``CategorizedError`` raised inside this call.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid profile/rootfs input,
                ``MISSING_DEPENDENCY`` for unavailable rootfs materialization or ``qemu-img``,
                ``PROVISIONING_FAILURE`` for domain/rootfs creation failures, or
                ``INFRASTRUCTURE_FAILURE`` for provider control-plane or overlay IO faults.
        """
        section = profile.provider.local_libvirt
        base = self._materialize_rootfs(section.rootfs, system_id, profile.arch)
        baseline = self._prepare_baseline_kernel(system_id, base)
        overlay = self._files.prepare_overlay(system_id, base=base)
        gdb_port = self._gdb_port_for(system_id) if section.debug.gdbstub else None
        # The SSH forward is rendered on every domain (ADR-0281, #937), so the port is always
        # allocated — no longer gated on ssh_credential_ref, which now controls only the
        # drgn-live introspection credential. Reuse-on-retry (_ssh_port_for) is unchanged.
        ssh_port = self._ssh_port_for(system_id)
        xml = render_domain_xml(  # validates the profile
            system_id,
            profile,
            disk_path=overlay.path,
            gdb_port=gdb_port,
            ssh_port=ssh_port,
            kernel_path=baseline.kernel,
            initrd_path=baseline.initrd,
        )
        try:
            if overlay.created:
                for customize in overlay_customizers:
                    customize(overlay.path)
            self._files.prepare_console(system_id)
            self._define_and_start(xml, system_id)
        except CategorizedError:
            self._files.cleanup_overlay_if_created(overlay)
            raise
        return domain_name_for(system_id)

    def _prepare_baseline_kernel(self, system_id: UUID, base: str) -> BaselineKernel:
        """Extract the rootfs's baseline kernel once; reuse an already-extracted directory.

        Mirrors the overlay's create-only-when-absent contract (ADR-0060/0272): a present baseline
        directory (the atomic all-or-nothing marker) is reused so a provision retry never re-mounts
        the base. Presence is checked through the injected ``baseline_exists`` seam (like
        ``overlay_exists``), so the reuse path is unit-testable without touching the real FS.
        """
        dest = Path(baseline_dir(system_id))
        if self._files.baseline_exists(str(dest)):
            initrd = dest / "initrd"
            present_initrd = initrd if self._files.baseline_exists(str(initrd)) else None
            return BaselineKernel(kernel=dest / "kernel", initrd=present_initrd)
        return self._extract_baseline_kernel(Path(base), dest)

    def _gdb_port_for(self, system_id: UUID) -> int:
        """Reuse the System's recorded gdbstub port if its domain already records one; else a
        fresh loopback port (ADR-0210 §2).

        Reuse keeps an idempotent provision retry stable — the live QEMU still listens on the
        port the first define recorded — so the resolver and the running stub never diverge.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` for a libvirt fault that is not the
                domain simply being absent (``VIR_ERR_NO_DOMAIN``).
        """
        existing = self._recorded_gdb_port(system_id)
        return existing if existing is not None else self._free_port()

    def _recorded_gdb_port(self, system_id: UUID) -> int | None:
        """The gdbstub port the System's already-defined domain records, or ``None`` if absent."""
        try:
            conn = self._connect()
        except libvirt.libvirtError as exc:
            raise self._infra("connecting to libvirt to read the gdbstub port", "") from exc
        try:
            domain = conn.lookupByName(domain_name_for(system_id))
            return recorded_gdb_port(domain.XMLDesc(0))
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            name = domain_name_for(system_id)
            raise self._infra("reading the recorded gdbstub port", name) from exc
        finally:
            _close(conn)

    def _ssh_port_for(self, system_id: UUID) -> int:
        """Reuse the System's recorded forwarded SSH port if its domain already records one; else a
        fresh loopback port (ADR-0218 §3).

        Mirrors ``_gdb_port_for``: reuse keeps an idempotent provision retry stable so the resolver
        and the running QEMU's forwarded port never diverge.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` for a libvirt fault that is not the
                domain simply being absent (``VIR_ERR_NO_DOMAIN``).
        """
        existing = self._recorded_ssh_port(system_id)
        return existing if existing is not None else self._free_port()

    def _recorded_ssh_port(self, system_id: UUID) -> int | None:
        """The forwarded SSH port the System's already-defined domain records, or ``None``."""
        try:
            conn = self._connect()
        except libvirt.libvirtError as exc:
            raise self._infra("connecting to libvirt to read the SSH port", "") from exc
        try:
            domain = conn.lookupByName(domain_name_for(system_id))
            return recorded_ssh_port(domain.XMLDesc(0))
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            name = domain_name_for(system_id)
            raise self._infra("reading the recorded SSH port", name) from exc
        finally:
            _close(conn)

    def _define_and_start(self, xml: str, system_id: UUID) -> None:
        try:
            conn = self._connect()
        except libvirt.libvirtError as exc:
            raise self._provisioning_failure(system_id) from exc
        try:
            domain = conn.defineXML(xml)
            try:
                domain.create()
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_OPERATION_INVALID:
                    return
                # Not "already running" — a real start failure. Undefine the domain we just
                # defined so provision stays transactional (a started domain or none). The
                # overlay is reclaimed by provision(), which catches this re-raise.
                try:
                    domain.undefine()
                except libvirt.libvirtError:
                    _log.warning(
                        "failed to undefine domain after a failed start; continuing",
                        exc_info=True,
                    )
                raise
        except libvirt.libvirtError as exc:
            raise self._provisioning_failure(system_id) from exc
        finally:
            _close(conn)

    @staticmethod
    def _provisioning_failure(system_id: UUID) -> CategorizedError:
        return CategorizedError(
            "libvirt failed to define/start the domain",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"system_id": str(system_id)},
        )

    def validate_rootfs_ref(self, rootfs: RootfsSource) -> None:
        """Validate that a rootfs ref is statically resolvable.

        A ``catalog`` reference is validated by name against the baseline catalog (its object is
        resolved at provision time through the DB-backed materialize fetch, ADR-0092, which needs
        a connection this admission-time validator does not hold); a ``local``/``upload``
        reference is validated by materializing it within the provider roots.
        """
        if isinstance(rootfs, CatalogComponentRef):
            validate_rootfs_reference(rootfs)
            return
        self._materialize_rootfs_base(rootfs, UUID(int=0))

    def reprovision(
        self,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        overlay_customizers: tuple[OverlayCustomizer, ...] = (),
    ) -> str:
        """Wipe the System's current install and define+start the new profile in place.

        Destructive (ADR-0038 §3): destroys+undefines the System's current domain, then
        defines+starts the new profile under the **same** deterministic domain name (the
        ``system_id`` is stable). Built from the idempotent ``teardown``/``provision``
        primitives — an absent prior domain is swallowed by ``teardown`` (so a retry after a
        partial wipe still provisions), and a ``provision`` failure surfaces as
        ``PROVISIONING_FAILURE`` (so the handler drives ``reprovisioning -> failed``).

        ``overlay_customizers`` (ADR-0289, #963) are forwarded to the internal ``provision``
        call: since ``teardown`` above always removes the prior overlay, ``provision`` always
        recreates it here, so the customizers always run.

        Raises:
            CategorizedError: ``PROVISIONING_FAILURE`` if the new domain cannot be
                defined/started; ``INFRASTRUCTURE_FAILURE`` if the wipe cannot be completed.
        """
        self.teardown(domain_name_for(system_id))
        return self.provision(system_id, profile, overlay_customizers=overlay_customizers)

    def teardown(self, domain_name: str) -> None:
        """Destroy+undefine the domain and reclaim its overlay + baseline kernel dir; idempotent.

        The overlay and the per-System baseline-kernel directory (ADR-0272) are removed after the
        libvirt teardown — including the already-absent-domain path — so a torn-down System leaves
        no orphaned disk or kernel files (ADR-0060). An absent overlay/directory is a no-op.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` on any libvirt error other than the
                achieved post-states.
        """
        self._teardown_domain(domain_name)
        self._files.remove_overlay_for_domain(domain_name)
        self._files.remove_baseline_for_domain(domain_name)

    def _materialize_rootfs_base(
        self, rootfs: RootfsSource, system_id: UUID, arch: str = "x86_64"
    ) -> str:
        rootfs = _materializable_rootfs(rootfs)
        return str(
            materialize_rootfs_base(
                rootfs,
                context=RootfsMaterializationContext(
                    allowed_roots=self._allowed_roots,
                    arch=arch,
                    upload=RootfsUploadContext("local", system_id, Path(ROOTFS_DIR)),
                    catalog_fetch=self._catalog_fetch,
                ),
            )
        )

    def _teardown_domain(self, domain_name: str) -> None:
        """Destroy and undefine the domain; idempotent over an already-absent domain.

        "No such domain" on lookup/undefine and "not running" on destroy are the achieved
        post-state, so they are swallowed; any other libvirt error fails.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` on any other libvirt error.
        """
        try:
            conn = self._connect()
        except libvirt.libvirtError as exc:
            raise self._infra("connecting to libvirt to tear down", domain_name) from exc
        try:
            try:
                domain = conn.lookupByName(domain_name)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                    return  # already gone
                raise self._infra("looking up", domain_name) from exc
            try:
                domain.destroy()
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                    raise self._infra("destroying", domain_name) from exc
            try:
                domain.undefine()
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                    raise self._infra("undefining", domain_name) from exc
        finally:
            _close(conn)

    @staticmethod
    def _infra(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"domain": domain_name},
        )
