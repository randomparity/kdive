"""Remote-libvirt Control plane: power + force_crash over qemu+tls (ADR-0084).

`RemoteLibvirtControl` realizes the `Controller` port against the remote host. The
domain operations (create/destroy/reset/reboot/injectNMI) match `LocalLibvirtControl`;
only the connection lifecycle differs — the mutual-TLS materialize->connect->cleanup of
`remote_connection` (ADR-0077). DB-free, keyed on the provider domain name. No shared
layer with `local_libvirt` (ADR-0076). All host seams are injected; `libvirt.open` runs
only under the live gate.

``force_crash`` injects an NMI; the disk-image base OS is configured to panic on an
unknown NMI (``kernel.unknown_nmi_panic=1``) so the NMI drives the panic->kdump path — a
base-image obligation (ADR-0084 §1).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import PowerAction
from kdive.providers.ports.lifecycle import Controller as Controller
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, unbound_remote_config
from kdive.providers.remote_libvirt.connection.transport import (
    open_libvirt_protocol,
    remote_connection,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import SecretBackend, secret_backend_from_env

_log = logging.getLogger(__name__)


# Linux input-event keycodes for the magic-SysRq combination (VIR_KEYCODE_SET_LINUX), as
# `virsh send-key <dom> --codeset linux KEY_LEFTALT KEY_SYSRQ KEY_<X>` sends them (ADR-0285). The
# table is duplicated from local-libvirt rather than shared: each provider realizes the Control
# port independently, with no shared layer (ADR-0076), the same way power/injectNMI are duplicated.
_KEY_LEFTALT = 56
_KEY_SYSRQ = 99
_SYSRQ_KEYCODES: dict[str, int] = {
    "t": 20,  # KEY_T
    "w": 17,  # KEY_W
    "m": 50,  # KEY_M
    "d": 32,  # KEY_D
    "p": 25,  # KEY_P
    "l": 38,  # KEY_L
    "q": 16,  # KEY_Q
}
# Hold the Alt+SysRq+key chord briefly so the guest input layer registers the combination.
_SYSRQ_HOLDTIME_MS = 100


class _Domain(Protocol):
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def reset(self, flags: int) -> int: ...
    def reboot(self, flags: int) -> int: ...
    def injectNMI(self, flags: int) -> int: ...  # noqa: N802 - libvirt binding name
    def sendKey(  # noqa: N802 - libvirt binding name
        self, codeset: int, holdtime: int, keycodes: list[int], nkeycodes: int, flags: int
    ) -> int: ...


class _ControlConn(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - libvirt binding name
    def close(self) -> None: ...


type OpenControlConnection = Callable[[str], _ControlConn]


def open_libvirt_control(uri: str) -> _ControlConn:
    """Production opener (live-host path; unit tests inject a fake)."""
    return open_libvirt_protocol(uri)


class RemoteLibvirtControl:
    """The `Controller` for the remote libvirt host (power + force_crash)."""

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
        open_connection: OpenControlConnection = open_libvirt_control,
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
    ) -> RemoteLibvirtControl:
        """Build from the shared worker env; opens no connection here."""
        return cls(secret_registry=secret_registry, config_factory=config_factory)

    def power(self, domain_name: str, action: PowerAction) -> None:
        """Drive the domain's power state; idempotent ``on``/``off`` swallow the post-state.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` if the domain is absent or a
                non-idempotent libvirt error occurs, ``CONFIGURATION_ERROR`` for invalid
                remote connection configuration, ``INFRASTRUCTURE_FAILURE`` for TLS
                materialization faults, or ``TRANSPORT_FAILURE`` when the libvirt TLS
                connection fails.
        """
        with self._connection() as conn:
            domain = self._lookup(conn, domain_name)
            self._apply_power(domain, domain_name, action)

    def force_crash(self, domain_name: str) -> None:
        """Panic the guest via NMI (``injectNMI``); the base OS panics on unknown NMI.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` if the domain is absent or libvirt errors,
                ``CONFIGURATION_ERROR`` for invalid remote connection configuration,
                ``INFRASTRUCTURE_FAILURE`` for TLS materialization faults, or
                ``TRANSPORT_FAILURE`` when the libvirt TLS connection fails.
        """
        with self._connection() as conn:
            domain = self._lookup(conn, domain_name)
            try:
                domain.injectNMI(0)
            except libvirt.libvirtError as exc:
                raise self._control_failure("injecting NMI into", domain_name) from exc

    def diagnostic_sysrq(self, domain_name: str, trigger: str) -> None:
        """Inject a magic-SysRq keystroke via ``sendKey`` (Alt+SysRq+<trigger>) over qemu+tls.

        The injection is a plain transport-agnostic libvirt domain call — the same ``sendKey``
        local-libvirt uses (ADR-0285) — so it works unchanged on the remote host (ADR-0433). This
        port only injects; the resulting console dump is read back out-of-band via the console read
        seam (ADR-0429), mirroring ``force_crash``'s single-libvirt-call shape.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` if ``trigger`` is not an allowlisted SysRq
                character (a programming error — the tool validates it), or ``CONTROL_FAILURE`` if
                the domain is absent or libvirt errors; ``CONFIGURATION_ERROR`` for invalid remote
                connection configuration, ``INFRASTRUCTURE_FAILURE`` for TLS materialization
                faults, or ``TRANSPORT_FAILURE`` when the libvirt TLS connection fails.
        """
        keycode = _SYSRQ_KEYCODES.get(trigger)
        if keycode is None:
            raise CategorizedError(
                f"unsupported SysRq trigger {trigger!r}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"domain": domain_name, "trigger": trigger},
            )
        with self._connection() as conn:
            domain = self._lookup(conn, domain_name)
            keycodes = [_KEY_LEFTALT, _KEY_SYSRQ, keycode]
            try:
                domain.sendKey(libvirt.VIR_KEYCODE_SET_LINUX, _SYSRQ_HOLDTIME_MS, keycodes, 3, 0)
            except libvirt.libvirtError as exc:
                raise self._control_failure("sending SysRq to", domain_name) from exc

    def _connection(self) -> AbstractContextManager[_ControlConn]:
        return remote_connection(
            self._config_factory(),
            self._secret_backend_factory(),
            open_connection=self._open_connection,
            pki_base_dir=self._pki_base_dir,
        )

    @staticmethod
    def _lookup(conn: _ControlConn, domain_name: str) -> _Domain:
        try:
            return conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            raise RemoteLibvirtControl._control_failure("looking up", domain_name) from exc

    def _apply_power(self, domain: _Domain, domain_name: str, action: PowerAction) -> None:
        try:
            if action is PowerAction.ON:
                self._idempotent(domain.create, "starting", domain_name)
            elif action is PowerAction.OFF:
                self._idempotent(domain.destroy, "stopping", domain_name)
            elif action is PowerAction.RESET:
                domain.reset(0)
            else:  # PowerAction.CYCLE
                domain.reboot(0)
        except libvirt.libvirtError as exc:
            raise self._control_failure(f"{action.value}-ing", domain_name) from exc

    @staticmethod
    def _idempotent(call: Callable[[], int], verb: str, domain_name: str) -> None:
        """Run an on/off call, swallowing the "already in target state" error as success."""
        try:
            call()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                raise
            _log.info("%s domain %s: already in target state; treating as ok", verb, domain_name)

    @staticmethod
    def _control_failure(verb: str, domain_name: str) -> CategorizedError:
        return CategorizedError(
            f"libvirt error {verb} domain",
            category=ErrorCategory.CONTROL_FAILURE,
            details={"domain": domain_name},
        )


__all__ = ["RemoteLibvirtControl"]
