"""Fault-inject device hot-plug port (ADR-0823).

An in-memory per-domain device table behind the :class:`DeviceHotplugger` port, so the plug and
un-plug paths of the rest of the stack run deterministically with no hypervisor. The resource
capability ``hotplug_behaviour`` picks how the table answers:

- ``busy``: un-plug of a present device is refused (``unplug_refused``).
- ``slow``: plug and un-plug are accepted unsettled and settle on the third ``is_attached``
  poll, that is, after two polls that still report the old state.
- ``hang``: plug and un-plug are accepted unsettled and never settle.

With no behaviour every call settles at once. Devices are live-only, as on a real provider: a
power OFF or CYCLE clears the domain's table (:meth:`FaultInjectHotplug.note_power`), and a
plug into a stopped domain raises ``domain_not_running``. Injected faults (the ``hotplug``
fault plane) wrap this class in :class:`~kdive.providers.fault_inject.lifecycle.faulted.
FaultedHotplug`.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import PowerAction
from kdive.providers.fault_inject.capabilities import HOTPLUG_BEHAVIOUR_KEY
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.ports._common import config_error
from kdive.providers.ports.lifecycle import DeviceSpec, HotplugDeviceKind, PlugOutcome

ADVERTISED_KINDS: Final = frozenset(HotplugDeviceKind)
# Polls that still report the old state before a ``slow`` device settles.
SLOW_POLLS: Final = 2
# virtio disk serials are at most 20 characters.
_SERIAL_CHARS: Final = 20


class HotplugBehaviour(StrEnum):
    """The ``hotplug_behaviour`` resource capability values."""

    BUSY = "busy"
    SLOW = "slow"
    HANG = "hang"

    @classmethod
    def from_capabilities(cls, capabilities: Mapping[str, object]) -> HotplugBehaviour | None:
        """Read the behaviour from resource capabilities; absent means settle at once.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a value other than the three names.
        """
        raw = capabilities.get(HOTPLUG_BEHAVIOUR_KEY)
        if raw is None:
            return None
        try:
            return cls(raw)
        except ValueError:
            raise config_error(
                f"{HOTPLUG_BEHAVIOUR_KEY} must be one of {sorted(cls)}, got {raw!r}"
            ) from None


@dataclass(slots=True)
class _Device:
    spec: DeviceSpec
    handle: str
    identity: dict[str, str]
    attached: bool
    # A plug or un-plug the provider accepted and has not finished; ``polls_left`` counts down
    # the ``is_attached`` polls that still report the old state (``hang`` never counts down).
    settling: bool = False
    polls_left: int = SLOW_POLLS
    removing: bool = False


@dataclass(slots=True)
class _Domain:
    running: bool = True
    devices: dict[str, _Device] = field(default_factory=dict)


def _error(message: str, category: ErrorCategory, reason: str) -> CategorizedError:
    return CategorizedError(message, category=category, details={"reason": reason})


def _digest(device_id: str) -> bytes:
    return hashlib.blake2b(device_id.encode(), digest_size=8).digest()


def _identity(spec: DeviceSpec) -> dict[str, str]:
    digest = _digest(spec.device_id)
    if spec.kind is HotplugDeviceKind.NIC:
        return {"mac": "52:54:00:" + ":".join(f"{byte:02x}" for byte in digest[:3])}
    return {"serial": f"kdive-{digest.hex()}"[:_SERIAL_CHARS]}


class FaultInjectHotplug:
    """The fault-inject :class:`DeviceHotplugger`; thread-safe, keyed on the domain name."""

    def __init__(
        self,
        inventory: FaultInjectInventory,
        behaviour: HotplugBehaviour | None = None,
        *,
        kinds: frozenset[HotplugDeviceKind] = ADVERTISED_KINDS,
    ) -> None:
        self._inventory = inventory
        self._behaviour = behaviour
        self._kinds = kinds
        self._domains: dict[str, _Domain] = {}
        self._lock = threading.Lock()

    def with_behaviour(self, behaviour: HotplugBehaviour | None) -> FaultInjectHotplug:
        """Return a port with ``behaviour`` that shares this port's device ledger and lock.

        A resource row's behaviour is read at each binding; the ledger must outlive the binding.
        """
        sibling = FaultInjectHotplug(self._inventory, behaviour, kinds=self._kinds)
        sibling._domains = self._domains  # noqa: SLF001 - one ledger across sibling ports
        sibling._lock = self._lock  # noqa: SLF001
        return sibling

    def plug(self, domain_name: str, spec: DeviceSpec) -> PlugOutcome:
        self._require_kind(spec.kind)
        with self._lock:
            domain = self._domain(domain_name)
            if not domain.running:
                raise _error(
                    f"domain {domain_name} is not running",
                    ErrorCategory.INFRASTRUCTURE_FAILURE,
                    "domain_not_running",
                )
            device = domain.devices.get(spec.device_id)
            if device is None:
                device = self._new_device(spec)
                domain.devices[spec.device_id] = device
            settled = device.attached and not device.removing
            return PlugOutcome(device.handle, dict(device.identity), settled)

    def unplug(self, domain_name: str, device_id: str, provider_handle: str | None) -> bool:
        with self._lock:
            domain = self._domain(domain_name)
            device = domain.devices.get(device_id)
            if device is None:
                if provider_handle is None:
                    raise self._handle_unknown(device_id)
                return True
            if provider_handle is not None and provider_handle != device.handle:
                raise self._handle_unknown(device_id)
            if device.removing:
                return False
            if self._behaviour is HotplugBehaviour.BUSY:
                raise _error(
                    f"guest refused to release device {device_id}",
                    ErrorCategory.CONTROL_FAILURE,
                    "unplug_refused",
                )
            if self._behaviour is None:
                del domain.devices[device_id]
                return True
            device.removing = True
            device.settling = True
            device.polls_left = SLOW_POLLS
            return False

    def is_attached(self, domain_name: str, device_id: str, provider_handle: str | None) -> bool:
        with self._lock:
            device = self._domain(domain_name).devices.get(device_id)
            if device is None:
                return False
            # A device being removed stays present until the removal settles.
            present = device.attached or device.removing
            if not device.settling or self._behaviour is HotplugBehaviour.HANG:
                return present
            if device.polls_left > 0:
                device.polls_left -= 1
                return present
            device.settling = False
            if device.removing:
                del self._domain(domain_name).devices[device_id]
                return False
            device.attached = True
            return True

    def release_all(self, domain_name: str) -> None:
        with self._lock:
            domain = self._domains.get(domain_name)
            if domain is not None:
                domain.devices.clear()

    def note_power(self, domain_name: str, action: PowerAction) -> None:
        """Mirror a power action: OFF and CYCLE drop the live devices; ON and CYCLE run."""
        if action not in (PowerAction.ON, PowerAction.OFF, PowerAction.CYCLE):
            return
        with self._lock:
            domain = self._domains.setdefault(domain_name, _Domain())
            if action is not PowerAction.ON:
                domain.devices.clear()
            domain.running = action is not PowerAction.OFF

    def _require_kind(self, kind: HotplugDeviceKind) -> None:
        if kind not in self._kinds:
            supported = sorted(self._kinds)
            raise config_error(f"device kind {kind!r} is not supported; supported: {supported}")

    def _domain(self, domain_name: str) -> _Domain:
        if self._inventory.system_id_of(domain_name) is None:
            raise _error(
                f"domain {domain_name} is not defined",
                ErrorCategory.INFRASTRUCTURE_FAILURE,
                "domain_absent",
            )
        return self._domains.setdefault(domain_name, _Domain())

    def _new_device(self, spec: DeviceSpec) -> _Device:
        settled = self._behaviour not in (HotplugBehaviour.SLOW, HotplugBehaviour.HANG)
        return _Device(
            spec=spec,
            handle=f"fault-inject:{spec.kind.value}:{spec.device_id}",
            identity=_identity(spec),
            attached=settled,
            settling=not settled,
        )

    @staticmethod
    def _handle_unknown(device_id: str) -> CategorizedError:
        return _error(
            f"no device {device_id} matches the supplied handle",
            ErrorCategory.CONTROL_FAILURE,
            "handle_unknown",
        )
