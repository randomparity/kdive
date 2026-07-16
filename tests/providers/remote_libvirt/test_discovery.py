"""Remote-libvirt discovery over the injected TLS connection (ADR-0076)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.capacity.state import ResourceStatus
from kdive.domain.catalog.resource_capabilities import CONCURRENT_ALLOCATION_CAP_KEY
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.providers.remote_libvirt.discovery import RemoteLibvirtDiscovery
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.providers.remote_libvirt.conftest import FakeConn, RecordingBackend

_REFS = TlsCertRefs(
    client_cert_ref="remote/clientcert.pem",
    client_key_ref="remote/clientkey.pem",  # pragma: allowlist secret
    ca_cert_ref="remote/cacert.pem",
)


def _config(cap: int = 2) -> RemoteLibvirtConfig:
    return RemoteLibvirtConfig(
        uri="qemu+tls://host.example/system", cert_refs=_REFS, concurrent_allocation_cap=cap
    )


def test_list_resources_returns_remote_record(tmp_path: Path) -> None:
    conn = FakeConn()
    discovery = RemoteLibvirtDiscovery(
        config=_config(),
        secret_backend=RecordingBackend(),
        open_connection=lambda _uri: conn,
        pki_base_dir=tmp_path,
    )
    records = discovery.list_resources()
    assert len(records) == 1
    record = records[0]
    assert record["kind"] is ResourceKind.REMOTE_LIBVIRT
    assert record["resource_id"] == "qemu+tls://host.example/system"
    assert record["status"] is ResourceStatus.AVAILABLE
    caps = record["capabilities"]
    assert caps["arch"] == "x86_64"
    assert caps["vcpus"] == 8
    assert caps["memory_mb"] == 16384
    assert caps["transports"] == ["gdbstub"]
    assert caps["connect_uri"] == "qemu+tls://host.example/system"
    assert caps["tls_client_cert_ref"] == "remote/clientcert.pem"
    assert caps["tls_client_key_ref"] == "remote/clientkey.pem"  # pragma: allowlist secret
    assert caps["tls_ca_cert_ref"] == "remote/cacert.pem"
    assert caps[CONCURRENT_ALLOCATION_CAP_KEY] == 2
    assert conn.closed  # the discovery op closes its connection
    assert list(tmp_path.iterdir()) == []  # and deletes its pkipath


def test_list_resources_materializes_pkipath_under_configured_base_dir(tmp_path: Path) -> None:
    # The injected pki_base_dir governs where the per-op TLS materials are staged: the URI
    # handed to the opener must carry a pkipath under tmp_path (not the system temp dir).
    seen: dict[str, str] = {}

    def _opener(uri: str) -> FakeConn:
        seen["uri"] = uri
        return FakeConn()

    discovery = RemoteLibvirtDiscovery(
        config=_config(),
        secret_backend=RecordingBackend(),
        open_connection=_opener,
        pki_base_dir=tmp_path,
    )
    discovery.list_resources()

    assert f"pkipath={tmp_path}" in seen["uri"]


def test_malformed_capabilities_xml_yields_unknown_arch(tmp_path: Path) -> None:
    class _BadXmlConn(FakeConn):
        def getCapabilities(self) -> str:  # noqa: N802 - libvirt binding name
            return "<not-xml"

    discovery = RemoteLibvirtDiscovery(
        config=_config(),
        secret_backend=RecordingBackend(),
        open_connection=lambda _uri: _BadXmlConn(),
        pki_base_dir=tmp_path,
    )
    assert discovery.list_resources()[0]["capabilities"]["arch"] == "unknown"


def test_from_env_without_inventory_raises_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(tmp_path / "absent.toml"))
    with pytest.raises(CategorizedError) as excinfo:
        RemoteLibvirtDiscovery.from_env(secret_registry=SecretRegistry(), resource_name="ub24-big")
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR


_INVENTORY = """schema_version = 2

[[image]]
provider = "remote-libvirt"
name = "fedora-kdive-remote-base-43"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "public"
[image.source]
kind = "staged"
volume = "fedora-kdive-remote-base-43.qcow2"

[[remote_libvirt]]
name = "ub24-big"
uri = "qemu+tls://host.example/system"
gdb_addr = "192.168.10.20"
gdbstub_range = "47000:47099"
client_cert_ref = "remote/clientcert.pem"
client_key_ref = "remote/clientkey.pem"  # pragma: allowlist secret
ca_cert_ref = "remote/cacert.pem"
base_image = "fedora-kdive-remote-base-43"
cost_class = "remote"
vcpus = 16
memory_mb = 65536
"""


def test_from_env_wires_named_instance_with_live_collaborators(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kdive.config as config
    from kdive.providers.remote_libvirt.connection.transport import open_libvirt

    path = tmp_path / "systems.toml"
    path.write_text(_INVENTORY)
    monkeypatch.setenv("KDIVE_SYSTEMS_TOML", str(path))
    config.load()

    discovery = RemoteLibvirtDiscovery.from_env(
        secret_registry=SecretRegistry(), resource_name="ub24-big"
    )

    # The named instance's URI is resolved into the config (not some other instance/None).
    assert discovery.host_uri == "qemu+tls://host.example/system"
    # The production opener and a real secret backend are wired, not left unset.
    assert discovery._open_connection is open_libvirt
    assert discovery._secret_backend is not None


def test_capabilities_advertise_provisioning_knobs(tmp_path: Path) -> None:
    discovery = RemoteLibvirtDiscovery(
        config=RemoteLibvirtConfig(
            uri="qemu+tls://host.example/system",
            cert_refs=_REFS,
            concurrent_allocation_cap=2,
            storage_pool="kdive-pool",
            gdb_addr="10.0.0.5",
            gdb_port_min=48000,
            gdb_port_max=48010,
        ),
        secret_backend=RecordingBackend(),
        open_connection=lambda _uri: FakeConn(),
        pki_base_dir=tmp_path,
    )
    caps = discovery.list_resources()[0]["capabilities"]
    assert caps["storage_pool"] == "kdive-pool"
    assert caps["gdbstub_addr"] == "10.0.0.5"
    assert caps["gdbstub_port_min"] == 48000
    assert caps["gdbstub_port_max"] == 48010


def test_capabilities_omit_gdb_addr_when_unset(tmp_path: Path) -> None:
    discovery = RemoteLibvirtDiscovery(
        config=_config(),
        secret_backend=RecordingBackend(),
        open_connection=lambda _uri: FakeConn(),
        pki_base_dir=tmp_path,
    )
    caps = discovery.list_resources()[0]["capabilities"]
    assert "gdbstub_addr" not in caps
    assert caps["storage_pool"] == "default"
    assert caps["gdbstub_port_min"] == 47000
    assert caps["gdbstub_port_max"] == 47099


def _discovery(conn: FakeConn, tmp_path: Path) -> RemoteLibvirtDiscovery:
    return RemoteLibvirtDiscovery(
        config=_config(),
        secret_backend=RecordingBackend(),
        open_connection=lambda _uri: conn,
        pki_base_dir=tmp_path,
    )


def test_capabilities_advertise_host_cpu(tmp_path: Path) -> None:
    conn = FakeConn()  # default host-model Skylake block, avx512f (v4) disabled -> v3 survives
    record = _discovery(conn, tmp_path).list_resources()[0]
    assert record["capabilities"]["host_cpu"] == {
        "model": "Skylake-Client-IBRS",
        "vendor": "Intel",
        "arch": "x86_64",
        "baseline_level": "x86-64-v3",
    }
    # The getDomainCapabilities call is pinned to the renderer's config: kvm / machine / host arch.
    assert conn.domcaps_call == (None, "x86_64", "pc", "kvm", 0)


def test_host_cpu_absent_when_getdomaincapabilities_raises(tmp_path: Path) -> None:
    conn = FakeConn(domcaps_error=True)
    record = _discovery(conn, tmp_path).list_resources()[0]
    assert "host_cpu" not in record["capabilities"]
    # A raised advisory call never drops the pre-feature capabilities.
    assert record["capabilities"]["arch"] == "x86_64"
    assert record["capabilities"]["vcpus"] == 8
    assert record["capabilities"]["memory_mb"] == 16384


def test_host_cpu_absent_when_domcaps_has_no_model(tmp_path: Path) -> None:
    conn = FakeConn(
        domcaps_xml=(
            "<domainCapabilities><cpu>"
            "<mode name='host-model' supported='yes'/></cpu></domainCapabilities>"
        )
    )
    record = _discovery(conn, tmp_path).list_resources()[0]
    assert "host_cpu" not in record["capabilities"]


def test_host_cpu_omits_baseline_level_for_unmapped_model(tmp_path: Path) -> None:
    conn = FakeConn(
        domcaps_xml=(
            "<domainCapabilities><cpu>"
            "<mode name='host-model' supported='yes'>"
            "<model>SomeFutureModel-v9</model><vendor>Intel</vendor>"
            "</mode></cpu></domainCapabilities>"
        )
    )
    record = _discovery(conn, tmp_path).list_resources()[0]
    assert record["capabilities"]["host_cpu"] == {
        "model": "SomeFutureModel-v9",
        "vendor": "Intel",
        "arch": "x86_64",
    }


def test_host_cpu_disable_guard_omits_level_end_to_end(tmp_path: Path) -> None:
    # A v3 model (Skylake) whose v3-defining `avx2` is host-model-disabled must not advertise v3.
    # Pins the exact libvirt <feature policy='disable' name='avx2'> spelling through
    # parse_host_cpu -> baseline_level, so a renamed token would fail here rather than silently
    # advertising a level the host cannot deliver.
    conn = FakeConn(
        domcaps_xml=(
            "<domainCapabilities><cpu>"
            "<mode name='host-model' supported='yes'>"
            "<model>Skylake-Client-IBRS</model><vendor>Intel</vendor>"
            "<feature policy='disable' name='avx2'/>"
            "</mode></cpu></domainCapabilities>"
        )
    )
    record = _discovery(conn, tmp_path).list_resources()[0]
    assert record["capabilities"]["host_cpu"] == {
        "model": "Skylake-Client-IBRS",
        "vendor": "Intel",
        "arch": "x86_64",
    }
