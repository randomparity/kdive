"""The bounded TCP reachability gate on the remote-libvirt reaper opener (ADR-0565, #1980)."""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.connection.reachability_gate import (
    DEFAULT_LIBVIRT_TLS_PORT,
    ProbeSocket,
    reaper_connect_timeout,
    remote_endpoint,
    require_reachable,
)

#: Credential-looking userinfo, present only so a test can assert it is *not* logged.
_USERINFO = "admin:hunter2"  # pragma: allowlist secret


@pytest.fixture
def listening_endpoint() -> Iterator[tuple[str, int]]:
    """A real socket accepting connections on an ephemeral loopback port."""
    server = socket.socket()
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        yield server.getsockname()
    finally:
        server.close()


def test_endpoint_defaults_to_the_libvirt_tls_port() -> None:
    assert remote_endpoint("qemu+tls://host.example/system") == (
        "host.example",
        DEFAULT_LIBVIRT_TLS_PORT,
    )


def test_endpoint_honours_an_explicit_port() -> None:
    assert remote_endpoint("qemu+tls://host.example:16999/system") == ("host.example", 16999)


def test_endpoint_survives_the_query_string_the_transport_appends() -> None:
    # remote_connection composes `?pkipath=...` onto the URI before handing it to the opener, so the
    # gate must parse the spelling it actually receives, not the operator's declared one.
    assert remote_endpoint("qemu+tls://host.example/system?pkipath=/tmp/kdive-remote-pki-x") == (
        "host.example",
        DEFAULT_LIBVIRT_TLS_PORT,
    )


def test_a_host_less_uri_is_a_configuration_error_not_a_localhost_probe() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        remote_endpoint("qemu+tls:///system")
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert "names no host" in str(excinfo.value)


@pytest.mark.parametrize(
    ("uri", "problem"),
    [
        ("qemu+tls://host.example:notanum/system", "not an integer"),
        ("qemu+tls://host.example:0/system", "port 0"),
    ],
)
def test_an_unusable_port_is_a_configuration_error_naming_the_uri(uri: str, problem: str) -> None:
    # validate_remote_uri checks the scheme and two forbidden parameters, never the port, so an
    # unchecked `parsed.port` would raise a bare ValueError naming neither the URI nor the setting —
    # and _enter_host would log it as "skipped an unreachable host".
    with pytest.raises(CategorizedError) as excinfo:
        remote_endpoint(uri)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert problem in str(excinfo.value)
    assert "host.example" in str(excinfo.value)


@pytest.mark.parametrize(
    ("authority", "reportable"),
    [
        ("[2001:db8::1]:16514", "qemu+tls://[2001:db8::1]:16514/system"),
        ("[::1]", "qemu+tls://[::1]/system"),
        ("host.example:16514", "qemu+tls://host.example:16514/system"),
        ("192.0.2.1:16514", "qemu+tls://192.0.2.1:16514/system"),
    ],
)
def test_failure_reports_preserve_host_uri_syntax(authority: str, reportable: str) -> None:
    uri = (
        f"qemu+tls://{_USERINFO}@{authority}/system"
        "?pkipath=/tmp/kdive-remote-pki-secret&opaque=value#fragment"
    )
    with pytest.raises(CategorizedError) as excinfo:
        require_reachable(uri, timeout=1.0, resolve=lambda _host, _port: [])
    assert excinfo.value.details["uri"] == reportable


def test_every_resolved_address_shares_one_deadline() -> None:
    """A dual-stack host must cost the timeout once, not once per address.

    ``socket.create_connection`` applies its ``timeout`` inside its own per-address loop, so handing
    it the whole budget would spend it once per A/AAAA record and make the stated bound wrong by a
    factor nobody configured.
    """
    handed: list[float] = []

    def resolve(_host: str, port: int) -> list[tuple[str, int]]:
        return [("::1", port), ("127.0.0.1", port)]

    def connect(_address: tuple[str, int], timeout: float) -> ProbeSocket:
        handed.append(timeout)
        raise TimeoutError("timed out")

    with pytest.raises(CategorizedError):
        require_reachable(
            "qemu+tls://dual.example/system", timeout=4.0, connect=connect, resolve=resolve
        )
    assert len(handed) == 2
    assert handed[0] <= 4.0
    # The second address gets what the first left, never a fresh budget.
    assert handed[1] < handed[0]


def test_a_later_address_still_answers_when_an_earlier_one_does_not() -> None:
    """A host whose IPv6 route is dead is still reachable over IPv4."""
    tried: list[str] = []

    def resolve(_host: str, port: int) -> list[tuple[str, int]]:
        return [("::1", port), ("127.0.0.1", port)]

    class _Probe:
        def close(self) -> None: ...

    def connect(address: tuple[str, int], _timeout: float) -> ProbeSocket:
        tried.append(address[0])
        if address[0] == "::1":
            raise ConnectionRefusedError("refused")
        return _Probe()

    require_reachable(
        "qemu+tls://dual.example/system", timeout=4.0, connect=connect, resolve=resolve
    )
    assert tried == ["::1", "127.0.0.1"]


def test_a_name_that_does_not_resolve_is_a_transport_failure() -> None:
    def resolve(_host: str, _port: int) -> list[tuple[str, int]]:
        raise OSError("Name or service not known")

    with pytest.raises(CategorizedError) as excinfo:
        require_reachable("qemu+tls://nowhere.invalid/system", timeout=1.0, resolve=resolve)
    # To a reaper an unresolvable host and an unreachable one are the same outcome, so they take the
    # same category and _enter_host isolates both the same way.
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE


def test_a_listening_endpoint_is_reachable(listening_endpoint: tuple[str, int]) -> None:
    host, port = listening_endpoint
    require_reachable(f"qemu+tls://{host}:{port}/system", timeout=5.0)


def test_the_probe_is_closed_and_sends_nothing(listening_endpoint: tuple[str, int]) -> None:
    host, port = listening_endpoint
    closed: list[bool] = []

    class _Probe:
        def close(self) -> None:
            closed.append(True)

    def connect(address: tuple[str, int], timeout: float) -> ProbeSocket:
        assert address == (host, port)
        # The whole budget, less whatever resolution and the loop head have already consumed.
        assert 0 < timeout <= 3.0
        return _Probe()

    require_reachable(f"qemu+tls://{host}:{port}/system", timeout=3.0, connect=connect)
    assert closed == [True]


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), ConnectionRefusedError("refused")])
def test_an_unanswered_or_refused_endpoint_raises_transport_failure(failure: OSError) -> None:
    def connect(_address: tuple[str, int], _timeout: float) -> ProbeSocket:
        raise failure

    with pytest.raises(CategorizedError) as excinfo:
        require_reachable("qemu+tls://down.example/system", timeout=2.0, connect=connect)
    # TRANSPORT_FAILURE, not INFRASTRUCTURE_FAILURE: it must match the category a failed qemu+tls
    # connect already raises, so the reaper fan-out's unreachable-host isolation is unchanged.
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert "down.example" in str(excinfo.value)


def test_a_name_idna_cannot_encode_is_a_configuration_error() -> None:
    """The sibling of the malformed-port case: a ValueError, not an OSError, out of getaddrinfo.

    ``getaddrinfo`` puts the name through the 'idna' codec, which raises ``UnicodeError`` for an
    over-long label. Left uncaught it reaches ``_enter_host``'s blanket handler and is logged as one
    more unreachable host, hiding an operator typo behind a network diagnosis.
    """
    with pytest.raises(CategorizedError) as excinfo:
        require_reachable(f"qemu+tls://{'a' * 64}.example.com/system", timeout=1.0)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_a_host_resolving_to_no_address_is_a_transport_failure() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        require_reachable("qemu+tls://empty.example/system", timeout=1.0, resolve=lambda _h, _p: [])
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert "no address" in str(excinfo.value)


def test_the_refusal_carries_the_last_address_failure_rather_than_claiming_a_timeout() -> None:
    """A refused connect, an unroutable one, and an expired deadline need three different fixes."""

    def connect(_address: tuple[str, int], _timeout: float) -> ProbeSocket:
        raise ConnectionRefusedError("Connection refused")

    with pytest.raises(CategorizedError) as excinfo:
        require_reachable(
            "qemu+tls://up-but-silent.example/system",
            timeout=1.0,
            connect=connect,
            resolve=lambda _h, port: [("192.0.2.1", port)],
        )
    assert "Connection refused" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ConnectionRefusedError)


@pytest.mark.parametrize(
    "uri",
    ["qemu+tcp://plain.example/system", "qemu+tls://host.example/system?no_verify=1"],
)
def test_a_weakened_transport_is_refused_before_anything_is_probed(uri: str) -> None:
    """The gate re-validates rather than trusting its caller.

    ``remote_connection`` does validate first, but this is a public seam #1947's capture reaper is
    told to open through, and the scheme plus ``no_verify`` are what keep the probe destination
    inside the operator's declared, TLS-only inventory.
    """
    probed: list[object] = []

    def connect(address: tuple[str, int], _timeout: float) -> ProbeSocket:
        probed.append(address)
        raise AssertionError("must not be reached")

    with pytest.raises(CategorizedError) as excinfo:
        require_reachable(uri, timeout=1.0, connect=connect)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert probed == []


def _raise_oserror(_host: str, _port: int) -> list[tuple[str, int]]:
    raise OSError("Name or service not known")


def test_no_failure_path_reports_the_composed_pkipath_or_userinfo() -> None:
    """Every raise the gate can take, not just the timeout one.

    ``_enter_host`` logs the raise with ``exc_info=True``, so one unstripped path is one leak.
    """
    pki = "/tmp/kdive-remote-pki-secret"  # noqa: S108 - a literal path, not a real one
    base = f"qemu+tls://{_USERINFO}@down.example/system?pkipath={pki}"

    def refuse(_address: tuple[str, int], _timeout: float) -> ProbeSocket:
        raise TimeoutError("timed out")

    cases = [
        # no address resolved, resolver failed, every address failed, unusable port
        lambda: require_reachable(base, timeout=1.0, resolve=lambda _h, _p: []),
        lambda: require_reachable(base, timeout=1.0, resolve=_raise_oserror),
        lambda: require_reachable(
            base, timeout=1.0, connect=refuse, resolve=lambda _h, port: [("192.0.2.1", port)]
        ),
        lambda: remote_endpoint(
            f"qemu+tls://{_USERINFO}@down.example:notanum/system?pkipath={pki}"
        ),
    ]
    for case in cases:
        with pytest.raises(CategorizedError) as excinfo:
            case()
        rendered = str(excinfo.value) + repr(excinfo.value.details)
        assert pki not in rendered
        assert _USERINFO not in rendered
        assert "down.example" in rendered


def test_the_timeout_comes_from_the_operator_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS", "9")
    config.load()
    assert reaper_connect_timeout() == 9.0


def test_the_timeout_default_is_five_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KDIVE_REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS", raising=False)
    config.load()
    assert reaper_connect_timeout() == 5.0


@pytest.mark.parametrize("raw", ["0", "-1"])
def test_a_non_positive_timeout_is_rejected(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    # A zero or negative connect timeout would make every declared host permanently unreachable,
    # which reads as a clean sweep that reclaims nothing. The parser is the guard.
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS", raw)
    config.load()
    with pytest.raises(CategorizedError) as excinfo:
        reaper_connect_timeout()
    assert "KDIVE_REMOTE_LIBVIRT_CONNECT_TIMEOUT_SECONDS" in str(excinfo.value)


def test_the_reaper_opener_gates_before_it_calls_libvirt(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable host must never reach ``libvirt.open`` — that is the whole bound."""
    from kdive.providers.remote_libvirt.reaping import connections

    order: list[str] = []

    def gate(uri: str, *, timeout: float, connect: object = None) -> None:
        del connect
        order.append(f"gate:{uri}:{timeout}")
        raise CategorizedError("unreachable", category=ErrorCategory.TRANSPORT_FAILURE)

    def opener(uri: str) -> object:
        order.append(f"libvirt:{uri}")
        return object()

    monkeypatch.setattr(connections, "require_reachable", gate)
    monkeypatch.setattr(connections, "reaper_connect_timeout", lambda: 4.0)
    monkeypatch.setattr(connections, "open_libvirt_protocol", opener)

    with pytest.raises(CategorizedError):
        connections.open_libvirt_reaper("qemu+tls://down.example/system")
    assert order == ["gate:qemu+tls://down.example/system:4.0"]
