"""The fixed inventory mediator rejects untrusted requests before touching its backend."""

import importlib.util
import json
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree.ElementTree import ParseError

import pytest

SYSTEM = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def collector(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "inventory_collector", Path("deploy/authority-inventory/collector.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.libvirt, "registerErrorHandler", Mock(return_value=1))
    return module


@pytest.fixture
def peer(collector, monkeypatch):
    monkeypatch.setattr(
        collector.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_uid=101 if name == "kdive-authority-observer" else 102),
    )
    return Mock(getsockopt=Mock(return_value=struct.pack("3i", 42, 101, 101)))


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{}\n",
        b"destroy UUID\n",
        b"\x00\n",
        SYSTEM.upper().replace("1111", "AAAA").encode() + b"\n",
        SYSTEM.encode(),
        SYSTEM.encode() + b"\n\n",
        (SYSTEM + "\n" + SYSTEM + "\n").encode(),
    ],
)
def test_request_language(collector, peer, monkeypatch, raw):
    assert collector.parse_request(b"\n") is None
    assert collector.parse_request((SYSTEM + "\n").encode()) == SYSTEM
    backend = Mock(side_effect=AssertionError("backend reached"))
    monkeypatch.setattr(collector, "collect", backend)
    peer.recv.side_effect = [raw, b""]
    with pytest.raises((ValueError, OSError)):
        collector.serve(peer)
    backend.assert_not_called()
    peer.sendall.assert_not_called()


@pytest.mark.parametrize("uid", [0, 102, 999, None])
def test_peer_identity(collector, peer, monkeypatch, uid):
    backend = Mock(side_effect=AssertionError("backend reached"))
    monkeypatch.setattr(collector, "collect", backend)
    if uid is None:
        peer.getsockopt.side_effect = OSError("missing credentials")
    else:
        peer.getsockopt.return_value = struct.pack("3i", 42, uid, 101)
    with pytest.raises((ValueError, OSError)):
        collector.serve(peer)
    backend.assert_not_called()
    peer.recv.assert_not_called()


@pytest.mark.parametrize("observer_uid", [0, 102])
def test_observer_account_colliding_with_root_or_authority(
    collector, peer, monkeypatch, observer_uid
):
    """A passwd collision is refused even when the peer matches the resolved observer."""
    monkeypatch.setattr(
        collector.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(
            pw_uid=observer_uid if name == "kdive-authority-observer" else 102
        ),
    )
    peer.getsockopt.return_value = struct.pack("3i", 42, observer_uid, observer_uid)
    backend = Mock(side_effect=AssertionError("backend reached"))
    monkeypatch.setattr(collector, "collect", backend)
    with pytest.raises(ValueError, match="peer-rejected"):
        collector.serve(peer)
    backend.assert_not_called()
    peer.recv.assert_not_called()


@pytest.mark.parametrize("failure", [None, "xml", "type", "lookup", "none", "root", "socket"])
@pytest.mark.parametrize("active", [0, 1])
def test_inventory_facts_and_failures(collector, monkeypatch, failure, active):
    domain = Mock()
    domain.name.return_value = "kdive-" + SYSTEM
    domain.isActive.return_value = active
    domain.XMLDesc.return_value = '<domain type="kvm"/>'
    other = Mock()
    other.name.return_value = "unrelated"
    conn = Mock()
    conn.listAllDomains.return_value = [other, domain]
    conn.lookupByName.return_value = domain
    monkeypatch.setattr(collector.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=102))
    monkeypatch.setattr(collector, "_provider_socket", Mock())
    monkeypatch.setattr(collector.libvirt, "openReadOnly", Mock(return_value=conn))
    monkeypatch.setattr(collector, "_root_names", Mock(return_value=["a", "\udcff", "é"]))
    if failure == "xml":
        domain.XMLDesc.return_value = "<invalid"
    elif failure == "type":
        domain.XMLDesc.return_value = "<domain/>"
    elif failure == "lookup":
        conn.lookupByName.side_effect = RuntimeError("vanished")
    elif failure == "none":
        collector.libvirt.openReadOnly.return_value = None
    elif failure == "root":
        collector._root_names.side_effect = PermissionError("unreadable")
    elif failure == "socket":
        collector._provider_socket.side_effect = ValueError("unsafe socket")
    if failure:
        with pytest.raises((ValueError, RuntimeError, PermissionError, ParseError)):
            collector.collect(SYSTEM)
    else:
        result = collector.collect(SYSTEM)
        assert result == {
            "domains": ["kdive-" + SYSTEM, "unrelated"],
            "roots": dict.fromkeys(collector.ROOTS, ["a", "\udcff", "é"]),
            "system": {"active": bool(active), "type": "kvm"},
        }
        assert collector.collect(None)["system"] is None
        assert collector.collect("22222222-2222-4333-8444-555555555555")["system"] is None
    if failure not in ("none", "socket"):
        assert conn.close.called


@pytest.mark.parametrize("extra", [0, 1])
def test_response_bound(collector, peer, monkeypatch, extra):
    data = {"value": ""}
    overhead = len(json.dumps(data, ensure_ascii=True).encode())
    data["value"] = "x" * (collector.MAX_BYTES - overhead + extra)
    monkeypatch.setattr(collector, "collect", lambda _: data)
    peer.recv.side_effect = [b"\n", b""]
    if extra:
        with pytest.raises(ValueError):
            collector.serve(peer)
        peer.sendall.assert_not_called()
    else:
        collector.serve(peer)
        assert len(peer.sendall.call_args.args[0]) == collector.MAX_BYTES


@pytest.mark.parametrize("failure", [None, "owner", "mode", "list"])
def test_root_fd_closure(collector, monkeypatch, tmp_path, failure):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "link").symlink_to("/nonexistent")
    uid = root.stat().st_uid
    if failure == "mode":
        root.chmod(0o755)
    if failure == "owner":
        uid += 1
    close = Mock(wraps=collector.os.close)
    monkeypatch.setattr(collector.os, "close", close)
    if failure == "list":
        monkeypatch.setattr(collector.os, "listdir", Mock(side_effect=OSError("denied")))
    if failure:
        with pytest.raises((ValueError, OSError)):
            collector._root_names(str(root), uid)
    else:
        assert collector._root_names(str(root), uid) == ["link"]
    close.assert_called_once()
    with pytest.raises(OSError):
        collector.os.fstat(close.call_args.args[0])
    link = tmp_path / "alias"
    link.symlink_to(root)
    with pytest.raises(OSError):
        collector._root_names(str(link), uid)


@pytest.mark.parametrize("fault", [None, "owner", "mode", "directory-link", "socket-link"])
def test_provider_socket_metadata(collector, monkeypatch, fault):
    import stat

    directory = SimpleNamespace(st_uid=102, st_mode=stat.S_IFDIR | 0o700)
    endpoint = SimpleNamespace(st_uid=102, st_mode=stat.S_IFSOCK | 0o600)
    if fault == "owner":
        endpoint.st_uid = 0
    elif fault == "mode":
        directory.st_mode = stat.S_IFDIR | 0o755
    elif fault == "directory-link":
        directory.st_mode = stat.S_IFLNK | 0o777
    elif fault == "socket-link":
        endpoint.st_mode = stat.S_IFLNK | 0o777
    monkeypatch.setattr(
        collector.os, "lstat", lambda path: endpoint if path == collector.SOCKET else directory
    )
    if fault:
        with pytest.raises(ValueError):
            collector._provider_socket(102)
    else:
        collector._provider_socket(102)


def test_request_requires_eof(collector, peer, monkeypatch):
    backend = Mock(side_effect=AssertionError("backend reached"))
    monkeypatch.setattr(collector, "collect", backend)
    peer.recv.side_effect = [b"\n", TimeoutError()]
    with pytest.raises(TimeoutError):
        collector.serve(peer)
    backend.assert_not_called()


def test_startup_failure_has_fixed_diagnostics(monkeypatch, capsys):
    import builtins
    import runpy

    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name == "libvirt":
            raise ImportError("private runtime path")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)
    with pytest.raises(SystemExit) as caught:
        runpy.run_path("deploy/authority-inventory/collector.py", run_name="__main__")
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "inventory-prerequisite-failed\n"


@pytest.mark.parametrize("failure", ["open", "lookup", "close"])
def test_native_diagnostics_are_suppressed(collector, monkeypatch, capsys, failure):
    import sys

    events = []
    handler = None

    def register(callback, context):
        nonlocal handler
        events.append("register")
        assert context is None
        handler = callback
        return 1

    def backend(operation):
        events.append(operation)
        if operation == failure:
            if handler is None:
                print("private native diagnostic", file=sys.stderr)
            else:
                handler(None, ["private native diagnostic"])
            raise RuntimeError("backend failure")
        return conn

    domain = Mock()
    domain.name.return_value = "kdive-" + SYSTEM
    domain.XMLDesc.return_value = '<domain type="kvm"/>'
    conn = Mock()
    conn.listAllDomains.return_value = [domain]

    def lookup(_):
        backend("lookup")
        return domain

    conn.lookupByName.side_effect = lookup
    conn.close.side_effect = lambda: backend("close")
    monkeypatch.setattr(
        collector,
        "libvirt",
        SimpleNamespace(registerErrorHandler=register, openReadOnly=lambda _: backend("open")),
    )
    monkeypatch.setattr(collector.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=102))
    monkeypatch.setattr(collector, "_provider_socket", Mock())
    monkeypatch.setattr(collector, "_root_names", Mock(return_value=[]))
    with pytest.raises(RuntimeError, match="backend failure"):
        collector.collect(SYSTEM)
    assert capsys.readouterr().err == ""
    assert events == (
        ["register", "open"] if failure == "open" else ["register", "open", "lookup", "close"]
    )


@pytest.mark.parametrize("result", [0, None, RuntimeError("registration failed")])
def test_error_handler_registration_fails_closed(collector, monkeypatch, result):
    register = Mock(return_value=result)
    if isinstance(result, Exception):
        register.side_effect = result
    opened = Mock(side_effect=AssertionError("backend reached"))
    monkeypatch.setattr(
        collector, "libvirt", SimpleNamespace(registerErrorHandler=register, openReadOnly=opened)
    )
    monkeypatch.setattr(collector.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=102))
    monkeypatch.setattr(collector, "_provider_socket", Mock())
    with pytest.raises((ValueError, RuntimeError)):
        collector.collect(None)
    opened.assert_not_called()
