"""Fixed external-boot authority host settings (ADR-0584)."""

from __future__ import annotations

from pathlib import Path

from kdive.config.registry import Setting


def _nonempty(raw: str) -> str:
    value = raw.strip()
    if not value or len(value.encode("utf-8")) > 255:
        raise ValueError("must contain 1 through 255 UTF-8 bytes")
    return value


def _positive_unix_id(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise ValueError("must be a positive Unix uid or gid")
    return value


def _absolute_path(raw: str) -> Path:
    value = Path(raw)
    if not value.is_absolute():
        raise ValueError("must be an absolute path")
    return value


def _always(_env: object) -> bool:
    return True


AUTHORITY_INSTANCE = Setting(
    name="KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE",
    parse=_nonempty,
    group="external-boot-authority",
    required_when=_always,
    help="Stable authority-instance identifier bound into the local TLS server identity.",
    suggest="a nonblank stable provider-host authority identifier",
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

SETTINGS = [
    AUTHORITY_INSTANCE,
    AUTHORITY_UID,
    AUTHORITY_GID,
    AUTHORITY_CLIENT_GID,
    AUTHORITY_JOURNAL_DIR,
    AUTHORITY_REQUEST_SOCKET,
    AUTHORITY_PROVIDER_SOCKET,
]
