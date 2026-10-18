"""Fixed one-request proof inventory collector (ADR-0800); invoked only by systemd."""

import json
import os
import pwd
import socket
import stat
import struct
import sys
from uuid import UUID

try:
    import libvirt
    from defusedxml.ElementTree import fromstring
except Exception:
    print("inventory-prerequisite-failed", file=sys.stderr)
    raise SystemExit(1) from None

OBSERVER = "kdive-authority-observer"
AUTHORITY = "kdive-provider-authority"
SOCKET = "/run/kdive/provider-authority/libvirt/libvirt-sock"
ROOTS = ("/var/lib/kdive/provider-authority/rootfs", "/var/lib/kdive/provider-authority/console")
MAX_BYTES = 1_048_576


class RequestRejected(ValueError):
    """Only this fixed category is safe to report from untrusted framing."""


def parse_request(raw: bytes) -> str | None:
    """Accept only LF or one canonical UUID plus LF, after EOF framing."""
    if raw == b"\n":
        return None
    if len(raw) != 37 or raw[-1:] != b"\n":
        raise RequestRejected("request-rejected")
    try:
        value = raw[:-1].decode("ascii")
        canonical = str(UUID(value))
    except ValueError as exc:
        raise RequestRejected("request-rejected") from exc
    if canonical != value:
        raise RequestRejected("request-rejected")
    return value


def _provider_socket(uid: int) -> None:
    directory = os.lstat(os.path.dirname(SOCKET))
    endpoint = os.lstat(SOCKET)
    if (
        directory.st_uid != uid
        or not stat.S_ISDIR(directory.st_mode)
        or stat.S_IMODE(directory.st_mode) != 0o700
        or endpoint.st_uid != uid
        or not stat.S_ISSOCK(endpoint.st_mode)
    ):
        raise ValueError("provider-prerequisite")


def _root_names(root: str, uid: int) -> list[str]:
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError("root-prerequisite")
        return sorted(os.listdir(fd))
    finally:
        os.close(fd)


def collect(system_id: str | None) -> dict[str, object]:
    """Read the complete existing facts, never child contents or mutable operations."""
    uid = pwd.getpwnam(AUTHORITY).pw_uid
    _provider_socket(uid)
    # Native diagnostics otherwise reach stderr before Python exceptions are caught.
    if libvirt.registerErrorHandler(lambda _context, _error: None, None) != 1:
        raise ValueError("error-handler-unavailable")
    conn = libvirt.openReadOnly("qemu+unix:///session?socket=" + SOCKET)
    if conn is None:
        raise ValueError("provider-unavailable")
    try:
        domains = sorted(domain.name() for domain in conn.listAllDomains(0))
        name = "kdive-" + system_id if system_id is not None else None
        system = None
        if name is not None and name in domains:
            domain = conn.lookupByName(name)
            kind = fromstring(domain.XMLDesc(0)).get("type", "")
            if not kind:
                raise ValueError("domain-type-missing")
            system = {"active": domain.isActive() == 1, "type": kind}
        roots = {root: _root_names(root, uid) for root in ROOTS}
        return {"domains": domains, "roots": roots, "system": system}
    finally:
        conn.close()


def serve(sock: socket.socket) -> None:
    """Authenticate before parsing, and finish encoding before sending any response."""
    observer = pwd.getpwnam(OBSERVER).pw_uid
    authority = pwd.getpwnam(AUTHORITY).pw_uid
    _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if observer in (0, authority) or uid != observer:
        raise ValueError("peer-rejected")
    sock.settimeout(120)
    raw = bytearray()
    while chunk := sock.recv(38 - len(raw)):
        raw.extend(chunk)
        if len(raw) > 37:
            raise RequestRejected("request-rejected")
    selected = parse_request(bytes(raw))
    output = bytearray()
    for fragment in json.JSONEncoder(ensure_ascii=True).iterencode(collect(selected)):
        # ASCII escaping makes character and encoded byte lengths identical.
        if len(output) + len(fragment) > MAX_BYTES:
            raise ValueError("response-too-large")
        output.extend(fragment.encode("ascii"))
    sock.sendall(output)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 1:
            raise RequestRejected("request-rejected")
        with socket.socket(fileno=0) as connection:
            serve(connection)
    except RequestRejected:
        print("request-rejected", file=sys.stderr)
        sys.exit(1)
    except Exception:  # No backend diagnostics or private values in the journal.
        print("inventory-failed", file=sys.stderr)
        sys.exit(1)
