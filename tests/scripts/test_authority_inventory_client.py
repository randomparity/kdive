"""Observer identity and deadline are enforced independently of the proof runner."""

import importlib.util
import socket
import stat
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def client(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "inventory_client", Path("deploy/authority-inventory/client.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=101, pw_gid=101))
    monkeypatch.setattr(module.os, "geteuid", lambda: 101)
    monkeypatch.setattr(module.os, "getegid", lambda: 101)
    monkeypatch.setattr(module.os, "getgroups", lambda: [101])
    monkeypatch.setattr(module.os, "listxattr", lambda *a, **kw: [])
    monkeypatch.setattr(
        module.os,
        "lstat",
        lambda path: SimpleNamespace(
            st_uid=0,
            st_gid=101 if path != "/run" else 0,
            st_mode=(stat.S_IFSOCK | 0o660)
            if path.endswith(".sock")
            else (stat.S_IFDIR | (0o755 if path == "/run" else 0o750)),
        ),
    )
    return module


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "uid",
        "groups",
        "gid",
        "peer",
        "path",
        "acl",
        "empty",
        "connect",
        "reset",
        "slow",
        "overflow",
    ],
)
def test_transport_failures(client, monkeypatch, failure):
    sock = Mock()
    sock.__enter__ = Mock(return_value=sock)
    sock.__exit__ = Mock(return_value=False)
    sock.getsockopt.return_value = struct.pack("3i", 1, 0, 0)
    sock.recv.side_effect = [b"{}", b""]
    monkeypatch.setattr(client.socket, "socket", Mock(return_value=sock))
    if failure == "uid":
        monkeypatch.setattr(client.os, "geteuid", lambda: 0)
    elif failure == "gid":
        monkeypatch.setattr(client.os, "getegid", lambda: 102)
    elif failure == "groups":
        monkeypatch.setattr(client.os, "getgroups", lambda: [101, 102])
    elif failure == "peer":
        sock.getsockopt.return_value = struct.pack("3i", 1, 102, 102)
    elif failure == "path":
        monkeypatch.setattr(
            client.os,
            "lstat",
            lambda _: SimpleNamespace(st_uid=0, st_gid=101, st_mode=stat.S_IFLNK | 0o777),
        )
    elif failure == "acl":
        monkeypatch.setattr(client.os, "listxattr", lambda *a, **kw: ["system.posix_acl_access"])
    elif failure == "empty":
        sock.recv.side_effect = [b""]
    elif failure == "connect":
        sock.connect.side_effect = OSError("connection refused")
    elif failure == "reset":
        sock.recv.side_effect = ConnectionResetError()
    elif failure == "slow":
        clock = iter([0, 1, 2, 3, 121])
        monkeypatch.setattr(client.time, "monotonic", lambda: next(clock))
        sock.recv.side_effect = [b"a", b"b", b""]
    elif failure == "overflow":
        sock.recv.side_effect = [b"x" * (client.MAX_BYTES + 1), b""]
    if failure:
        with pytest.raises((ValueError, OSError)):
            client.request_inventory(None)
    else:
        assert client.request_inventory(None) == b"{}"
        sock.sendall.assert_called_once_with(b"\n")
        sock.shutdown.assert_called_once_with(socket.SHUT_WR)
        sock.recv.side_effect = [b"x" * client.MAX_BYTES, b""]
        assert len(client.request_inventory(None)) == client.MAX_BYTES


def test_stream_framing(client, monkeypatch):
    from threading import Thread

    left, right = socket.socketpair()
    stream = Mock(wraps=left)
    stream.__enter__ = Mock(return_value=stream)
    stream.__exit__ = Mock(side_effect=lambda *_: left.close())
    stream.connect = Mock()
    stream.getsockopt = Mock(return_value=struct.pack("3i", 1, 0, 0))
    monkeypatch.setattr(client.socket, "socket", Mock(return_value=stream))
    received = []

    def server():
        with right:
            right.settimeout(2)
            received.append(right.recv(38))
            received.append(right.recv(38))
            right.sendall(b'{"domains":')
            right.sendall(b'[], "roots": {}, "system": null}')

    thread = Thread(target=server)
    thread.start()
    try:
        assert client.request_inventory(None) == b'{"domains":[], "roots": {}, "system": null}'
    finally:
        left.close()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert received == [b"\n", b""]
