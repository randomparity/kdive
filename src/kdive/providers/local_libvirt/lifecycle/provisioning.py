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
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

import kdive.config as config
from kdive.components.references import (
    ArtifactComponentRef,
    CatalogComponentRef,
    LocalComponentRef,
)
from kdive.domain.catalog.resource_capabilities import (
    GUEST_ARCHES_KEY,
    ResourceCapabilities,
    resolve_accel_emulator,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.platform.arch_traits import SUPPORTED_ARCHES
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    RootfsSource,
    _UploadRootfs,
    validate_rootfs_reference,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.baseline_kernel import (
    BaselineKernel,
    ExtractBaselineKernel,
    _real_extract_baseline_kernel,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.materialize import (
    CatalogFetch,
    MaterializableRootfsRef,
    RootfsMaterializationContext,
    RootfsUploadContext,
    UploadFetch,
    materialize_rootfs_base,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.overlay_customize import OverlayCustomizer
from kdive.providers.local_libvirt.lifecycle.rootfs.rootfs_catalog_fetch import (
    rootfs_catalog_fetch_from_env,
)
from kdive.providers.local_libvirt.lifecycle.rootfs.rootfs_upload_fetch import (
    rootfs_upload_fetch_from_env,
)
from kdive.providers.local_libvirt.lifecycle.storage import (
    ROOTFS_DIR,
    UPLOADS_DIR,
    ProvisioningFiles,
    baseline_dir,
    overlay_path,
)
from kdive.providers.local_libvirt.lifecycle.xml import render_domain_xml
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.shared.host_cpu import host_cpu_dict
from kdive.providers.shared.libvirt_xml import (
    parse_domain_resolved_cpu,
    parse_guest_arches,
    parse_host_capabilities_cpu,
    recorded_gdb_port_from_root,
    recorded_ssh_port_from_root,
)
from kdive.providers.shared.runtime_paths import console_log_path, domain_name_for
from kdive.serialization import JsonValue

__all__ = [
    "LocalLibvirtProvisioning",
    "ProvisioningFiles",
    "console_log_path",
    "domain_name_for",
    "overlay_path",
    "render_domain_xml",
]

_log = logging.getLogger(__name__)


class _LibvirtDomain(Protocol):
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def undefine(self) -> int: ...
    def undefineFlags(self, flags: int) -> int: ...  # noqa: N802 - mirrors the binding name
    def XMLDesc(self, flags: int) -> str: ...  # noqa: N802 - mirrors the libvirt binding name


class _LibvirtConn(Protocol):
    def defineXML(self, xml: str) -> _LibvirtDomain: ...
    def lookupByName(self, name: str) -> _LibvirtDomain: ...
    def getCapabilities(self) -> str: ...
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


def _parse_recorded_domain_xml(xml: str, *, domain_name: str, port_name: str) -> ET.Element:
    try:
        return _safe_fromstring(xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise CategorizedError(
            f"malformed libvirt domain XML reading the recorded {port_name} port",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"domain": domain_name},
        ) from exc


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
        upload_fetch: UploadFetch | None = None,
        free_port: FreePort | None = None,
        extract_baseline_kernel: ExtractBaselineKernel | None = None,
        guest_egress: bool = False,
    ) -> None:
        self._connect = connect
        self._files = files or ProvisioningFiles()
        self._allowed_roots = allowed_roots or [Path(ROOTFS_DIR)]
        self._catalog_fetch = catalog_fetch
        self._upload_fetch = upload_fetch
        self._materialize_rootfs = materialize_rootfs or self._materialize_rootfs_base
        self._free_port = free_port or _bind_probe_free_port
        self._extract_baseline_kernel = extract_baseline_kernel or _real_extract_baseline_kernel
        # Operator-resolved egress policy for the SSH-forward NIC (ADR-0313, #1031). Default False
        # keeps restrict=on; composition binds the per-Resource value via rebind_for_resource.
        self._guest_egress = guest_egress

    @classmethod
    def from_env(cls, *, guest_egress: bool = False) -> LocalLibvirtProvisioning:
        """Build from ``KDIVE_LIBVIRT_URI`` (default ``qemu:///system``); does not connect.

        Wires the ``catalog`` rootfs lane (ADR-0228): the catalog fetch lazily opens its own DB
        connection + object store per call, so constructing the provisioner opens nothing.

        ``guest_egress`` (ADR-0313, #1031) is the operator-resolved egress opt-in for the local
        Resource this provisioner serves; the host-agnostic default is ``False`` (``restrict=on``).
        """
        host_uri = config.require(LIBVIRT_URI)
        allowed_roots = [Path(ROOTFS_DIR)]
        # `virConnect` structurally satisfies the narrow `_LibvirtConn` Protocol (only
        # `defineXML`/`lookupByName`), so no suppression is needed at this seam.
        return cls(
            connect=lambda: libvirt.open(host_uri),
            allowed_roots=allowed_roots,
            catalog_fetch=rootfs_catalog_fetch_from_env(allowed_roots),
            upload_fetch=rootfs_upload_fetch_from_env(),
            guest_egress=guest_egress,
        )

    def provision(
        self,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        overlay_customizers: tuple[OverlayCustomizer, ...] = (),
        bootstrap_pubkey: str | None = None,
    ) -> str:
        """Define and start the tagged domain; return its name.

        ``bootstrap_pubkey`` (ADR-0291) is ignored here: local-libvirt injects the bootstrap key
        pre-boot via ``overlay_customizers`` (the ``virt-customize`` path), not into the running
        guest. It is accepted only to satisfy the ``Provisioner`` port.

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
        del bootstrap_pubkey  # local injects pre-boot via overlay_customizers (ADR-0291)
        section = profile.provider.local_libvirt
        # Resolve accel/emulator from live capabilities BEFORE creating any artifact (ADR-0340):
        # a fail-closed arch drift or a caps-read fault rejects with zero overlay/baseline and
        # skips the expensive rootfs materialization for a System that cannot be provisioned.
        accel, emulator = self._resolve_guest_arch(profile.arch)
        base = self._materialize_rootfs(section.rootfs, system_id, profile.arch)
        baseline = self._prepare_baseline_kernel(system_id, base, section.baseline_kernel)
        overlay = self._files.prepare_overlay(system_id, base=base, disk_gb=profile.disk_gb)
        gdb_port = self._gdb_port_for(system_id) if section.debug.gdbstub else None
        # The SSH forward is rendered on every domain (ADR-0281, #937), so the port is always
        # allocated. drgn-live no longer needs a profile credential — it authenticates with the
        # per-System bootstrap key (ADR-0289/0315). Reuse-on-retry (_ssh_port_for) is unchanged.
        ssh_port = self._ssh_port_for(system_id)
        if self._guest_egress:
            # Positive, greppable signal for a security-relevant state: the operator opted this
            # resource into guest egress, so the guest NIC renders restrict=off (ADR-0313, #1031).
            _log.info(
                "provisioning System %s with guest egress enabled (restrict=off): the guest can "
                "reach the network; the network-zone firewall is the enforcement boundary",
                system_id,
            )
        xml = render_domain_xml(  # validates the profile
            system_id,
            profile,
            disk_path=overlay.path,
            gdb_port=gdb_port,
            ssh_port=ssh_port,
            kernel_path=baseline.kernel,
            initrd_path=baseline.initrd,
            guest_egress=self._guest_egress,
            accel=accel,
            emulator=emulator,
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

    def _prepare_baseline_kernel(
        self, system_id: UUID, base: str, baseline_kernel: str | None
    ) -> BaselineKernel:
        """Extract the rootfs's baseline kernel once; reuse an already-extracted directory.

        Mirrors the overlay's create-only-when-absent contract (ADR-0060/0272): a present baseline
        directory (the atomic all-or-nothing marker) is reused so a provision retry never re-mounts
        the base. Presence is checked through the injected ``baseline_exists`` seam (like
        ``overlay_exists``), so the reuse path is unit-testable without touching the real FS.

        ``baseline_kernel`` is the optional profile hint (ADR-0310) that disambiguates a
        multi-kernel ``/boot``; it is consulted only on a fresh extraction — a reused directory
        already holds the resolved kernel, so an idempotent retry stays stable.
        """
        dest = Path(baseline_dir(system_id))
        if self._files.baseline_exists(str(dest)):
            initrd = dest / "initrd"
            present_initrd = initrd if self._files.baseline_exists(str(initrd)) else None
            return BaselineKernel(kernel=dest / "kernel", initrd=present_initrd)
        return self._extract_baseline_kernel(Path(base), dest, baseline_kernel)

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
        name = domain_name_for(system_id)
        try:
            domain = conn.lookupByName(name)
            root = _parse_recorded_domain_xml(
                domain.XMLDesc(0), domain_name=name, port_name="gdbstub"
            )
            return recorded_gdb_port_from_root(root)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            raise self._infra("reading the recorded gdbstub port", name) from exc
        finally:
            _close(conn)

    def _resolve_guest_arch(self, arch: str) -> tuple[str, str | None]:
        """Resolve ``(accel, emulator)`` for ``arch`` from live libvirt capabilities (ADR-0340).

        Reads ``getCapabilities`` and reuses the shared :func:`resolve_accel_emulator` branch
        (the one admission uses), so the provider and admission cannot drift:

        - empty ``guest_arches`` (this host not re-discovered since ADR-0338) → fail **open** to
          ``("kvm", None)``, today's legacy x86-KVM path;
        - non-empty but ``arch`` absent (the host lost the arch's qemu binary after admission
          validated it) → fail **closed** with ``CONFIGURATION_ERROR`` naming the supported set;
        - a ``getCapabilities`` / connection ``libvirtError`` → ``INFRASTRUCTURE_FAILURE``,
          grouping this read with the other pre-define host-state reads (``_recorded_ssh_port`` /
          ``_recorded_gdb_port``) rather than the mutating define/start.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` for a libvirt fault reading capabilities;
                ``CONFIGURATION_ERROR`` when the host advertises guest arches but not ``arch``.
        """
        try:
            conn = self._connect()
        except libvirt.libvirtError as exc:
            raise self._infra("connecting to libvirt to read capabilities of", "") from exc
        try:
            caps_xml = conn.getCapabilities()
        except libvirt.libvirtError as exc:
            raise self._infra("reading capabilities of", "") from exc
        finally:
            _close(conn)
        # Route the raw parser output through the typed reader (as admission does), so both
        # resolution sites feed resolve_accel_emulator the same GuestArch shape (ADR-0338/0340).
        guest_arches = ResourceCapabilities.from_mapping(
            {GUEST_ARCHES_KEY: parse_guest_arches(caps_xml, SUPPORTED_ARCHES)}
        ).guest_arches()
        resolved = resolve_accel_emulator(guest_arches, arch)
        return resolved if resolved is not None else ("kvm", None)

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
        name = domain_name_for(system_id)
        try:
            domain = conn.lookupByName(name)
            root = _parse_recorded_domain_xml(domain.XMLDesc(0), domain_name=name, port_name="SSH")
            return recorded_ssh_port_from_root(root)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            raise self._infra("reading the recorded SSH port", name) from exc
        finally:
            _close(conn)

    def read_resolved_cpu(self, system_id: UUID) -> dict[str, JsonValue] | None:
        """The running domain's resolved guest CPU (ADR-0369), best-effort — never raises.

        Reads the domain XML with ``VIR_DOMAIN_XML_UPDATE_CPU`` (libvirt expands host-model / a
        ``custom`` pin to a concrete ``<model>``). Returns the ``{model, vendor?, arch,
        baseline_level?}`` dict, or ``None`` when nothing concrete can be read:

        - a concrete ``<model>`` → that CPU (host-model / a pin / an expanding passthrough);
        - an unexpanded ``host-passthrough`` (no ``<model>``) → the host's ``getCapabilities``
          ``<cpu>`` (the passthrough guest **is** the host CPU) — the default local-x86 case;
        - a TCG machine-default (no ``<cpu>``, or a non-passthrough mode with no model) → ``None``.

        Any ``libvirt.libvirtError`` (domain gone, connection fault) or parse fault yields ``None``
        and is logged — the caller records NULL and provisioning is unaffected.
        """
        name = domain_name_for(system_id)
        try:
            conn = self._connect()
        except libvirt.libvirtError:
            _log.warning("connecting to libvirt to read resolved_cpu for %s failed", name)
            return None
        try:
            domain = conn.lookupByName(name)
            domain_xml = domain.XMLDesc(libvirt.VIR_DOMAIN_XML_UPDATE_CPU)
            mode, parsed = parse_domain_resolved_cpu(domain_xml)
            if parsed is not None:
                return host_cpu_dict(parsed, "")
            if mode == "host-passthrough":
                host = parse_host_capabilities_cpu(conn.getCapabilities())
                return host_cpu_dict(host, "") if host is not None else None
            return None
        except libvirt.libvirtError:
            _log.warning("reading resolved_cpu for %s failed; recording null", name, exc_info=True)
            return None
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
        a connection this admission-time validator does not hold). An ``upload`` reference is
        deferred to provision the same way (ADR-0434): the object exists only after the upload
        window, so materializing it here — with no real ``system_id`` — would issue a bogus HEAD.
        A ``local`` reference is validated by materializing it within the provider roots.
        """
        if isinstance(rootfs, CatalogComponentRef | _UploadRootfs):
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
        bootstrap_pubkey: str | None = None,
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
        del bootstrap_pubkey  # local injects pre-boot via overlay_customizers (ADR-0291)
        self.teardown(domain_name_for(system_id))
        return self.provision(system_id, profile, overlay_customizers=overlay_customizers)

    def teardown(self, domain_name: str) -> None:
        """Destroy+undefine the domain and reclaim its overlay, baseline dir, and uploaded rootfs.

        The overlay, the per-System baseline-kernel directory (ADR-0272), and any staged uploaded
        rootfs (ADR-0434) are removed after the libvirt teardown — including the already-absent-
        domain path — so a torn-down System leaves no orphaned disk, kernel, or uploaded-image
        files (ADR-0060). An absent file/directory is a no-op; idempotent.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` on any libvirt error other than the
                achieved post-states.
        """
        self._teardown_domain(domain_name)
        self._files.remove_overlay_for_domain(domain_name)
        self._files.remove_baseline_for_domain(domain_name)
        self._files.remove_uploaded_rootfs_for_domain(domain_name)

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
                    upload=RootfsUploadContext("local", system_id, Path(UPLOADS_DIR)),
                    catalog_fetch=self._catalog_fetch,
                    upload_fetch=self._upload_fetch,
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
                # SNAPSHOTS_METADATA so a snapshotted domain (ADR-0378) undefines cleanly rather
                # than libvirt refusing on residual snapshot metadata; internal snapshot *data*
                # is freed with the overlay qcow2 this teardown reclaims.
                domain.undefineFlags(libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA)
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
