"""Remote-libvirt Install + Boot plane: in-guest kernel install + boot-id readiness (ADR-0082).

`RemoteLibvirtInstall` realizes the `Installer` + `Booter` ports against a provisioned remote
disk-image System (ADR-0080). `install()` mints a single presigned GET for the built
vmlinuz+modules bundle (ADR-0081), then runs a constrained in-guest helper through the issue-3
registered-URL artifact channel (ADR-0078) to pull, extract, and add-or-replace the single
deterministic ``kdive`` grub slot with the method-conditional crashkernel cmdline (already
composed upstream by ``cmdline_for``). `boot()` reads the guest boot_id, runs the helper's
atomic select-slot + detached reboot, and confirms a fresh boot by the boot_id changing — the
readiness signal a console-less remote target affords.

Independent of ``local_libvirt`` (ADR-0076). All slow/host seams — the qemu+tls connection
opener, the guest-agent round-trip, the object store, the clock, sleep — are injected, so unit
tests drive the full orchestration and every error path with no libvirt host; the real
curl/tar/grub/reboot mechanics run only under the ``live_vm`` gate.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.lifecycle import InstallRequest
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, unbound_remote_config
from kdive.providers.remote_libvirt.connection.endpoint_preflight import (
    validate_guest_routable_endpoint,
)
from kdive.providers.remote_libvirt.connection.transport import (
    open_libvirt_protocol,
    remote_connection,
)
from kdive.providers.remote_libvirt.guest.agent import (
    AgentCommand,
    GuestAgentExec,
    qemu_agent_command,
)
from kdive.providers.remote_libvirt.guest.artifact_channel import InTargetArtifactChannel
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env
from kdive.store.objectstore import object_store_from_env

_log = logging.getLogger(__name__)

# The single allowlisted in-guest helper the base image carries (ADR-0082 §1); the only program
# this plane lets through GuestAgentExec.
_HELPER = "/usr/local/sbin/kdive-install-kernel"
_TRANSCRIPT_OWNER_KIND = "systems"
# Bounded tail of the redacted in-guest transcript surfaced in an install_failure so the
# in-guest reason (a grubby/dracut/curl step) is diagnosable from the worker log without a guest
# shell, while a runaway helper log cannot bloat the job error (#386). The *redacted* snippet is
# used, never the raw result streams — guest stderr echoes the presigned capability URL (a curl
# progress line), and only the registry-masked transcript is safe to log (ADR-0078).
_TRANSCRIPT_TAIL = 2048
# The presigned GET must outlive a worst-case in-guest download of a hundreds-of-MB bundle
# (ADR-0081), not the shortest possible window (ADR-0082 §2).
_DEFAULT_GET_EXPIRY_S = 3600
# The install command downloads + extracts the bundle in-guest; allow well beyond the guest-agent
# default so a large bundle does not time out mid-install.
_DEFAULT_INSTALL_TIMEOUT_S = 1800.0
_DEFAULT_BOOT_TIMEOUT_S = 300.0
_DEFAULT_BOOT_POLL_S = 2.0
# Arming the capture kernel means dracut building a kdump initramfs in-guest, which took ~90s
# on a Rocky 10 guest. It runs AFTER the guest is otherwise up, so the boot window alone does
# not cover it.
_DEFAULT_KDUMP_ARM_TIMEOUT_S = 300.0
# Reboot tears down the guest agent, so the boot command and the post-reboot boot-id polls expect
# the agent to be unreachable / its reply truncated; those categories are swallowed as "still
# rebooting" rather than treated as failures (ADR-0082 §3).
_REBOOT_EXPECTED = frozenset(
    {ErrorCategory.TRANSPORT_FAILURE, ErrorCategory.INFRASTRUCTURE_FAILURE}
)
# The helper's exit-code contract (ADR-0489). `EX_TEMPFAIL` (sysexits' 75) is the one condition
# the helper can name as transient: the bundle did not arrive from the presigned URL, which a
# retry heals by minting a fresh one. Every other code is deterministic.
_HELPER_EX_TEMPFAIL = 75
_CATEGORY_BY_HELPER_EXIT: dict[int, ErrorCategory] = {
    _HELPER_EX_TEMPFAIL: ErrorCategory.INFRASTRUCTURE_FAILURE,
}


def _category_for_helper_exit(exit_status: int) -> ErrorCategory:
    """Map a non-zero helper exit onto its failure category (ADR-0489).

    The helper is baked into an operator-built base image, so a worker that knows this contract
    still meets guests built before it that exit ``1`` for every failure — and a future helper may
    add codes this worker has never heard of. An unrecognised code therefore maps to
    ``INSTALL_FAILURE``, which is exactly the pre-#1653 behaviour: an old guest degrades to it
    rather than being misclassified, and an unknown code is never assumed retryable.

    Args:
        exit_status: The in-guest helper's non-zero exit status.

    Returns:
        The category the failure is reported as; retryable only for a recognised transient code.
    """
    return _CATEGORY_BY_HELPER_EXIT.get(exit_status, ErrorCategory.INSTALL_FAILURE)


class _StorePort(Protocol):
    # Both methods: install() mints the GET and the InTargetArtifactChannel persists the
    # redacted transcript via put_artifact, and the one injected factory serves both.
    def presign_get(self, key: str, *, expires_in: int, version_id: str | None = None) -> str: ...
    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact: ...


class _Domain(Protocol):
    def name(self) -> str: ...


class _InstallConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


type OpenInstallConnection = Callable[[str], _InstallConn]
type Sleep = Callable[[float], None]
type Monotonic = Callable[[], float]


def open_libvirt_install(uri: str) -> _InstallConn:
    """The production opener (live-host path; unit tests inject a fake)."""
    return open_libvirt_protocol(uri)


class RemoteLibvirtInstall:
    """The realized remote `Installer` + `Booter` (ADR-0082).

    Buildable without operator config (ADR-0076): the remote connection config is resolved per op
    from the ``systems.toml`` ``[[remote_libvirt]]`` instance via ``config_factory`` (ADR-0112),
    never at construction.
    """

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
        open_connection: OpenInstallConnection = open_libvirt_install,
        store_factory: Callable[[], _StorePort] = object_store_from_env,
        agent_command: AgentCommand = qemu_agent_command,
        secret_backend_factory: Callable[[], SecretBackend] | None = None,
        pki_base_dir: Path | None = None,
        get_expiry_s: int = _DEFAULT_GET_EXPIRY_S,
        install_timeout_s: float = _DEFAULT_INSTALL_TIMEOUT_S,
        boot_timeout_s: float = _DEFAULT_BOOT_TIMEOUT_S,
        boot_poll_s: float = _DEFAULT_BOOT_POLL_S,
        kdump_arm_timeout_s: float = _DEFAULT_KDUMP_ARM_TIMEOUT_S,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
    ) -> None:
        self._secret_registry = secret_registry
        self._config_factory = config_factory
        self._open_connection = open_connection
        self._store_factory = store_factory
        self._agent_command = agent_command
        self._secret_backend_factory = secret_backend_factory or (
            lambda: secret_backend_from_env(registry=secret_registry)
        )
        self._pki_base_dir = pki_base_dir
        self._get_expiry_s = get_expiry_s
        self._install_timeout_s = install_timeout_s
        self._boot_timeout_s = boot_timeout_s
        self._boot_poll_s = boot_poll_s
        self._kdump_arm_timeout_s = kdump_arm_timeout_s
        self._sleep = sleep
        self._monotonic = monotonic

    @classmethod
    def from_env(
        cls,
        *,
        secret_registry: SecretRegistry,
        store: _StorePort,
        config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
    ) -> RemoteLibvirtInstall:
        """Build from the shared worker env; opens no connection and mints no URL here."""
        return cls(
            secret_registry=secret_registry,
            store_factory=lambda: store,
            config_factory=config_factory,
        )

    def install(self, request: InstallRequest) -> None:
        """Pull the built bundle in-guest, install it, and write the boot entry.

        Mints one presigned GET for ``request.kernel_ref``, runs the allowlisted helper's
        ``install`` subcommand through the registered-URL artifact channel (so the bearer URL is
        masked in any persisted transcript), and replaces the deterministic ``kdive`` grub slot.
        Does not reboot — ``boot`` owns the power transition.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` when ``KDIVE_S3_ENDPOINT_URL`` is a
                loopback/localhost address the remote guest cannot reach (preflight, ADR-0110),
                ``INSTALL_FAILURE`` for a deterministic non-zero helper exit (a bad bundle,
                dracut, grubby, or a guest image with no curl) and for any exit code this worker
                does not recognise, ``INFRASTRUCTURE_FAILURE`` for the helper's transient exit
                code (ADR-0489 — curl ran and did not get the bundle, including an in-guest
                403/404 from a vanished object, since the worker only mints the URL and never
                fetches), from the object store, or from a malformed agent reply,
                ``TRANSPORT_FAILURE`` for an unreachable guest agent, ``CONFIGURATION_ERROR`` for
                missing operator config, propagated from the seams.
        """
        config = self._config_factory()
        validate_guest_routable_endpoint()
        versions = request.artifact_versions or {}
        url = self._store_factory().presign_get(
            request.kernel_ref,
            expires_in=self._get_expiry_s,
            version_id=versions.get("kernel"),
        )
        argv = [
            _HELPER,
            "install",
            "--url",
            url,
            "--cmdline",
            request.cmdline,
            "--method",
            request.method.value,
        ]
        channel = InTargetArtifactChannel(
            registry=self._secret_registry,
            agent_exec=self._agent_exec(self._install_timeout_s),
            store_factory=self._store_factory,
            scope=object(),
        )
        with self._connection(config) as conn:
            domain = self._lookup(conn, domain_name_for(request.system_id))
            output = channel.exec_with_capability(
                domain,
                capability_url=url,
                argv=argv,
                owner_kind=_TRANSCRIPT_OWNER_KIND,
                owner_id=str(request.system_id),
            )
        if output.result.exit_status != 0:
            # The category decides whether the queue may retry (ADR-0483), so it is read from the
            # helper's exit code rather than fixed: a lost bundle download is transient and heals
            # on attempt 2 with a fresh presigned URL, while dracut/grubby are deterministic and
            # must dead-letter on attempt 1 (ADR-0489).
            raise CategorizedError(
                "in-guest kernel install exited non-zero",
                category=_category_for_helper_exit(output.result.exit_status),
                details={
                    "system_id": str(request.system_id),
                    "exit_status": output.result.exit_status,
                    "transcript": output.transcript_snippet[-_TRANSCRIPT_TAIL:],
                },
            )

    def boot(self, system_id: UUID, *, accel: str | None = None) -> None:
        """Reboot into the installed kernel and confirm a fresh boot by boot_id change.

        Reads the guest's pre-reboot boot_id, runs the helper's atomic select-``kdive``-slot +
        detached reboot, then polls boot_id until it differs from the baseline — proving a real
        boot transition (a stale agent connection cannot fake a new boot_id, ADR-0082 §3).

        A guest that reserved crashkernel memory is a kdump System, so boot does not report
        ready until its capture kernel is actually loaded (see :meth:`_await_kdump_armed`).

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` for a domain lookup fault (retryable —
                it crosses the libvirtd socket); the helper exit-code contract's category for a
                non-zero boot-id baseline read (``INSTALL_FAILURE`` unless the helper named the
                failure transient, ADR-0489); ``TRANSPORT_FAILURE`` when the guest agent is
                unreachable before the reboot; ``BOOT_TIMEOUT`` when no fresh boot_id appears
                within the boot
                window (a panic/hang manifests as the agent never reconnecting), or when a
                crashkernel-reserving guest never arms its capture kernel.
        """
        del accel  # remote-libvirt is KVM (accel=NULL); TCG scaling is local-only (ADR-0341)
        config = self._config_factory()
        agent_exec = self._agent_exec(self._boot_timeout_s)
        with self._connection(config) as conn:
            domain = self._lookup(conn, domain_name_for(system_id))
            baseline = self._read_boot_id(agent_exec, domain, system_id)
            self._trigger_reboot(agent_exec, domain)
            self._await_fresh_boot(agent_exec, domain, baseline, system_id)
            self._await_kdump_armed(agent_exec, domain, system_id)

    @staticmethod
    def _parse_kdump_status(stdout: bytes) -> tuple[int, int] | None:
        """Parse the helper's ``crash_size=``/``crash_loaded=`` reply.

        ``None`` when the reply is not that shape, which the caller reads as "cannot determine".
        """
        fields: dict[str, int] = {}
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            try:
                fields[key.strip()] = int(value.strip())
            except ValueError:
                continue
        if "crash_size" not in fields or "crash_loaded" not in fields:
            return None
        return fields["crash_size"], fields["crash_loaded"]

    def _await_kdump_armed(
        self, agent_exec: GuestAgentExec, domain: _Domain, system_id: UUID
    ) -> None:
        """Block until a crashkernel-reserving guest has loaded its capture kernel.

        The guest itself says whether kdump is intended: a non-zero ``kexec_crash_size`` means
        the ``crashkernel=`` reservation took, so this System exists to be crashed and captured.
        Arming is what makes that possible, and it happens well after the guest is otherwise up
        — the in-guest kdump service builds a dracut initramfs first (~90s on Rocky 10).

        Without this gate ``runs.boot`` reported ready while ``kexec_crash_loaded`` was still 0,
        so a ``control.force_crash`` immediately after boot panicked a guest with no capture
        kernel behind it. Nothing then wrote a vmcore, and (with the default ``kernel.panic=0``)
        the guest hung instead of rebooting, surfacing ~15 minutes later as an opaque capture
        ``readiness_failure`` rather than at the point of the mistake (#1610).

        A guest with no reservation is not a kdump System and is skipped, so nothing here slows
        the non-kdump path. A guest whose helper cannot answer at all — an image staged before
        the ``kdump-status`` subcommand existed — is also skipped rather than failed: a newer
        server must not brick an older staged image, and skipping is no worse than the behaviour
        this gate replaces.
        """
        deadline = self._monotonic() + self._kdump_arm_timeout_s
        reservation_seen = False
        while True:
            try:
                result = agent_exec.run(domain, [_HELPER, "kdump-status"])
            except CategorizedError as exc:
                if exc.category is ErrorCategory.CONFIGURATION_ERROR:
                    raise  # a refused program is a real fault, not the agent still rebooting
                result = None  # agent still coming back after the reboot; retry
            if result is not None:
                if result.exit_status != 0:
                    # Skipping is a decision worth seeing: this is the gate declining to run.
                    _log.info("guest helper has no kdump-status; skipping the arming gate")
                    return
                status = self._parse_kdump_status(result.stdout)
                if status is None:
                    _log.info("guest helper gave no kdump status; skipping the arming gate")
                    return
                crash_size, crash_loaded = status
                if crash_size <= 0 or crash_loaded == 1:
                    return  # not a kdump System, or already armed
                reservation_seen = True
            if self._monotonic() >= deadline:
                raise self._kdump_arm_timeout(system_id, reservation_seen=reservation_seen)
            self._sleep(self._boot_poll_s)

    @staticmethod
    def _kdump_arm_timeout(system_id: UUID, *, reservation_seen: bool) -> CategorizedError:
        """The timeout fault, naming what was actually observed rather than what was assumed.

        Only a poll that returned a well-formed status establishes that the guest reserved
        crashkernel memory. If the agent never answered across the whole window, nothing about
        kdump was determined, and reporting "reserved but never armed" would send the operator
        to ``kdump.service`` for what is really a dead guest agent.
        """
        if not reservation_seen:
            return CategorizedError(
                "guest agent never answered the kdump-status probe after the boot, so whether "
                "the capture kernel is armed could not be determined",
                category=ErrorCategory.BOOT_TIMEOUT,
                details={
                    "system_id": str(system_id),
                    "hint": "the agent stopped answering after the reboot; check "
                    "qemu-guest-agent in the guest",
                },
            )
        return CategorizedError(
            "guest reserved crashkernel memory but never armed its capture kernel; "
            "a crash would produce no vmcore",
            category=ErrorCategory.BOOT_TIMEOUT,
            details={
                "system_id": str(system_id),
                "kexec_crash_loaded": 0,
                "hint": "check kdump.service in the guest (journalctl -u kdump.service)",
            },
        )

    def _read_boot_id(self, agent_exec: GuestAgentExec, domain: _Domain, system_id: UUID) -> str:
        result = agent_exec.run(domain, [_HELPER, "boot-id"])
        if result.exit_status != 0:
            # Same exit-code contract as install (ADR-0489): one mapping, so the two sites that
            # read a helper exit cannot drift into disagreeing about what a code means.
            raise CategorizedError(
                "could not read the guest boot-id baseline",
                category=_category_for_helper_exit(result.exit_status),
                details={"system_id": str(system_id), "exit_status": result.exit_status},
            )
        return result.stdout.decode("utf-8", errors="replace").strip()

    def _trigger_reboot(self, agent_exec: GuestAgentExec, domain: _Domain) -> None:
        """Run the helper's atomic select+detached-reboot; a lost agent is the expected signal."""
        try:
            agent_exec.run(domain, [_HELPER, "boot"])
        except CategorizedError as exc:
            if exc.category not in _REBOOT_EXPECTED:
                raise
            # Expected: the reboot tore down the guest agent. Log it so a later BOOT_TIMEOUT
            # can be told apart from a reboot command that genuinely failed (ADR-0082 §3).
            _log.debug("reboot command lost the guest agent as expected: %s", exc)

    def _await_fresh_boot(
        self, agent_exec: GuestAgentExec, domain: _Domain, baseline: str, system_id: UUID
    ) -> None:
        deadline = self._monotonic() + self._boot_timeout_s
        while True:
            current = self._poll_boot_id(agent_exec, domain)
            if current is not None and current != baseline:
                return
            if self._monotonic() >= deadline:
                raise CategorizedError(
                    "system did not reboot into a fresh kernel within the boot window",
                    category=ErrorCategory.BOOT_TIMEOUT,
                    details={"system_id": str(system_id), "timeout_s": self._boot_timeout_s},
                )
            self._sleep(self._boot_poll_s)

    def _poll_boot_id(self, agent_exec: GuestAgentExec, domain: _Domain) -> str | None:
        """One post-reboot boot-id read; ``None`` means "agent down / not ready, keep polling"."""
        try:
            result = agent_exec.run(domain, [_HELPER, "boot-id"])
        except CategorizedError as exc:
            if exc.category in _REBOOT_EXPECTED:
                _log.debug("boot-id poll: agent not back yet (%s)", exc.category.value)
                return None
            raise
        if result.exit_status != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace").strip()

    def _agent_exec(self, timeout_s: float) -> GuestAgentExec:
        return GuestAgentExec(
            agent_command=self._agent_command,
            allowed_programs=frozenset({_HELPER}),
            timeout_s=timeout_s,
            sleep=self._sleep,
            monotonic=self._monotonic,
        )

    def _connection(self, config: RemoteLibvirtConfig):  # noqa: ANN202 - contextmanager passthrough
        return remote_connection(
            config,
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )

    @staticmethod
    def _lookup(conn: _InstallConn, domain_name: str) -> _Domain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            # INFRASTRUCTURE_FAILURE, not INSTALL_FAILURE: this crosses the qemu+tls socket to a
            # remote libvirtd, so a daemon restart or a network blip mid-install is exactly the
            # transient the queue's retry exists for. Under ADR-0483 the category decides whether
            # the job may retry, so calling a lookup fault an install fault would strand the Run.
            raise CategorizedError(
                "remote domain lookup failed for install/boot",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"domain": domain_name},
            ) from exc


__all__ = ["RemoteLibvirtInstall"]
