"""Fixed external-boot authority host settings (ADR-0584)."""

from __future__ import annotations

import re
from ipaddress import IPv4Address
from pathlib import Path

from kdive.config.registry import Setting

DEFAULT_DENIED_IDENTITIES = tuple([f"kdive-worker-{slot}" for slot in range(1, 9)] + ["kdive"])


def _denied_identities(raw: str) -> tuple[str, ...]:
    names = tuple(raw.split(","))
    if (
        not 1 <= len(names) <= 32
        or len(set(names)) != len(names)
        or any(re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", name) is None for name in names)
    ):
        raise ValueError("must contain 1 through 32 unique canonical account names")
    return names


def _nonempty(raw: str) -> str:
    value = raw.strip()
    if not value or len(value.encode("utf-8")) > 255:
        raise ValueError("must contain 1 through 255 UTF-8 bytes")
    return value


def _bounded_opaque(raw: str) -> str:
    value = raw.strip()
    if not value or len(value.encode("utf-8")) > 1024 or "\0" in value:
        raise ValueError("must contain 1 through 1024 UTF-8 bytes without NUL")
    return value


def _positive_bytes(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise ValueError("must be a positive byte count")
    return value


def _positive_unix_id(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise ValueError("must be a positive Unix uid or gid")
    return value


def _absolute_path(raw: str) -> Path:
    if "\0" in raw:
        raise ValueError("must not contain a NUL byte")
    value = Path(raw)
    if not value.is_absolute():
        raise ValueError("must be an absolute path")
    return value


def _authority_socket_path(raw: str) -> Path:
    value = _absolute_path(raw)
    if len(raw.encode()) > 107:
        raise ValueError("must fit in a 107-byte AF_UNIX pathname")
    return value


def _always(_env: object) -> bool:
    return True


def _network_address(raw: str) -> str:
    address = IPv4Address(raw)
    if str(address) != raw or address.is_multicast:
        raise ValueError("must be a canonical numeric IPv4 bind address")
    return raw


def _network_port(raw: str) -> int:
    value = int(raw)
    if not 1 <= value <= 65535:
        raise ValueError("must be a TCP port from 1 through 65535")
    return value


AUTHORITY_INSTANCE = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE",
    parse=_nonempty,
    group="external-boot-authority",
    required_when=_always,
    help="Stable authority-instance identifier bound into the local TLS server identity.",
    suggest="a nonblank stable provider-host authority identifier",
)
AUTHORITY_DENIED_IDENTITIES = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_DENIED_IDENTITIES",
    parse=_denied_identities,
    default=",".join(DEFAULT_DENIED_IDENTITIES),
    group="external-boot-authority",
    help="Comma-separated list of 1 through 32 unique canonical local account names whose "
    "exclusion the authority proves; each must exist and be outside the authority UID and groups.",
)
AUTHORITY_UID = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_UID",
    parse=_positive_unix_id,
    group="external-boot-authority",
    required_when=_always,
    help="Unix uid that owns and runs the external-boot authority host boundary.",
    suggest="the positive uid assigned to kdive-provider-authority",
)
AUTHORITY_GID = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_GID",
    parse=_positive_unix_id,
    group="external-boot-authority",
    required_when=_always,
    help="Unix gid that owns the external-boot authority provider endpoint.",
    suggest="the positive gid assigned to kdive-provider-authority",
)
AUTHORITY_CLIENT_GID = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_CLIENT_GID",
    parse=_positive_unix_id,
    group="external-boot-authority",
    required_when=_always,
    help="Unix gid allowed to traverse the external-boot authority request endpoint.",
    suggest="the positive gid assigned to kdive-provider-authority-client",
)
AUTHORITY_JOURNAL_DIR = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_JOURNAL_DIR",
    parse=_absolute_path,
    default="/var/lib/kdive/provider-authority/journal",
    group="external-boot-authority",
    help="Private root containing one exact external-boot authority journal lane per System.",
)
AUTHORITY_REQUEST_SOCKET = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET",
    parse=_absolute_path,
    default="/run/kdive/provider-authority/request/authority.sock",
    group="external-boot-authority",
    help="Mutual-TLS AF_UNIX request socket owned by the external-boot authority.",
)
AUTHORITY_PROVIDER_SOCKET = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_PROVIDER_SOCKET",
    parse=_absolute_path,
    default="/run/kdive/provider-authority/libvirt/libvirt-sock",
    group="external-boot-authority",
    help="Dormant authority-owned provider mutation socket checked for local reachability.",
)
AUTHORITY_STORE_IDENTITY = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_STORE_IDENTITY",
    parse=_bounded_opaque,
    group="external-boot-authority",
    help="Opaque durable identity of the authority-owned recovery store.",
)
AUTHORITY_RECOVERY_RESERVE_BYTES = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_RESERVE_BYTES",
    parse=_positive_bytes,
    group="external-boot-authority",
    help="Fixed bytes reserved by each external-boot activation.",
)
AUTHORITY_RECOVERY_MAX_BYTES = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_MAX_BYTES",
    parse=_positive_bytes,
    group="external-boot-authority",
    help="Maximum retained reservation bytes in this authority recovery store.",
)

AUTHORITY_NETWORK_ADDRESS = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_NETWORK_ADDRESS",
    parse=_network_address,
    group="external-boot-authority",
    help="Optional numeric IPv4 listener address; requires the network port setting.",
)
AUTHORITY_NETWORK_PORT = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_NETWORK_PORT",
    parse=_network_port,
    group="external-boot-authority",
    help="Optional mutual-TLS TCP listener port; requires the network address setting.",
)

_WORKER = frozenset({"worker"})
WORKER_AUTHORITY_INSTANCE = Setting(
    name="KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE",
    parse=_nonempty,
    processes=_WORKER,
    group="worker-authority-client",
    help="Optional local authority instance for the worker-only AF_UNIX client route.",
    suggest="set all worker authority client settings or leave all unset to disable the route",
)
WORKER_AUTHORITY_REQUEST_SOCKET = Setting(
    name="KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET",
    parse=_authority_socket_path,
    processes=_WORKER,
    group="worker-authority-client",
    help="Optional fixed absolute AF_UNIX authority request socket for worker client calls.",
    suggest="set all worker authority client settings or leave all unset to disable the route",
)
WORKER_AUTHORITY_SERVER_CA_REF = Setting(
    name="KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF",
    parse=_nonempty,
    processes=_WORKER,
    group="worker-authority-client",
    help="Optional secret reference for the local authority server CA.",
    suggest="set all worker authority client settings or leave all unset to disable the route",
)
WORKER_AUTHORITY_CLIENT_CERT_REF = Setting(
    name="KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_CERT_REF",
    parse=_nonempty,
    processes=_WORKER,
    group="worker-authority-client",
    help="Optional secret reference for the worker local-authority TLS certificate.",
    suggest="set all worker authority client settings or leave all unset to disable the route",
)
WORKER_AUTHORITY_CLIENT_KEY_REF = Setting(
    name="KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF",
    parse=_nonempty,
    secret=True,
    processes=_WORKER,
    group="worker-authority-client",
    help="Optional secret reference for the worker local-authority TLS private key.",
    suggest="set all worker authority client settings or leave all unset to disable the route",
)

SETTINGS = [
    AUTHORITY_INSTANCE,
    AUTHORITY_DENIED_IDENTITIES,
    AUTHORITY_UID,
    AUTHORITY_GID,
    AUTHORITY_CLIENT_GID,
    AUTHORITY_JOURNAL_DIR,
    AUTHORITY_REQUEST_SOCKET,
    AUTHORITY_PROVIDER_SOCKET,
    AUTHORITY_NETWORK_ADDRESS,
    AUTHORITY_NETWORK_PORT,
    WORKER_AUTHORITY_INSTANCE,
    WORKER_AUTHORITY_REQUEST_SOCKET,
    WORKER_AUTHORITY_SERVER_CA_REF,
    WORKER_AUTHORITY_CLIENT_CERT_REF,
    WORKER_AUTHORITY_CLIENT_KEY_REF,
]
