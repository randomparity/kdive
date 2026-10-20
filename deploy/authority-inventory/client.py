"""Stdlib-only client for the fixed local proof inventory endpoint (ADR-0800)."""

import os
import pwd
import socket
import stat
import struct
import sys
import time
from uuid import UUID

OBSERVER = "kdive-authority-observer"
DIRECTORY = "/run/kdive-authority-inventory"
SOCKET = DIRECTORY + "/inventory.sock"
MAX_BYTES = 1_048_576


def _prerequisites() -> None:
    account = pwd.getpwnam(OBSERVER)
    if (
        account.pw_uid == 0
        or account.pw_gid == 0
        or os.geteuid() != account.pw_uid
        or os.getegid() != account.pw_gid
        or set(os.getgroups()) - {account.pw_gid}
    ):
        raise ValueError("observer-identity")
    for path, mode, gid in (
        ("/run", stat.S_IFDIR | 0o755, 0),
        (DIRECTORY, stat.S_IFDIR | 0o750, account.pw_gid),
        (SOCKET, stat.S_IFSOCK | 0o660, account.pw_gid),
    ):
        metadata = os.lstat(path)
        if metadata.st_uid != 0 or metadata.st_gid != gid or metadata.st_mode != mode:
            raise ValueError("endpoint-metadata")
        if {"system.posix_acl_access", "system.posix_acl_default"} & set(
            os.listxattr(path, follow_symlinks=False)
        ):
            raise ValueError("endpoint-acl")


def request_inventory(system_id: str | None) -> bytes:
    """Return a bounded EOF-framed response within one total monotonic deadline."""
    deadline = time.monotonic() + 120
    raw = b"\n" if system_id is None else (str(UUID(system_id)) + "\n").encode("ascii")
    _prerequisites()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:

        def remaining() -> None:
            seconds = deadline - time.monotonic()
            if seconds <= 0:
                raise TimeoutError("inventory-deadline")
            sock.settimeout(seconds)

        remaining()
        sock.connect(SOCKET)
        _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != 0:
            raise ValueError("listener-identity")
        remaining()
        sock.sendall(raw)
        sock.shutdown(socket.SHUT_WR)
        output = bytearray()
        while True:
            remaining()
            chunk = sock.recv(min(65536, MAX_BYTES + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > MAX_BYTES:
                raise ValueError("inventory-too-large")
        if not output:
            raise ValueError("inventory-empty")
        return bytes(output)


if __name__ == "__main__":
    try:
        if len(sys.argv) > 2:
            raise ValueError("request-rejected")
        sys.stdout.buffer.write(request_inventory(sys.argv[1] if len(sys.argv) == 2 else None))
    except Exception:
        print("inventory-client-failed", file=sys.stderr)
        sys.exit(1)
