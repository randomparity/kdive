"""Remote-libvirt runtime composition assertions (ADR-0183, ADR-0187)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from kdive.providers.remote_libvirt import composition
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, TlsCertRefs
from kdive.security.secrets.secret_registry import SecretRegistry


def test_remote_runtime_owns_no_platform_root_cmdline() -> None:
    # The remote base image is partitioned and boots via in-guest GRUB (root=UUID, inherited by
    # grubby --copy-default). The platform must not inject a root device or it overrides that — so
    # the remote runtime carries platform_root_cmdline=None, unlike local's "root=/dev/vda" (#587).
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    assert runtime.platform_root_cmdline is None


def test_remote_runtime_sets_rebind_for_resource() -> None:
    # ADR-0187: the remote runtime carries a per-resource rebind hook so the resolver can bind it
    # to the granted host; the base runtime is buildable without operator config (ADR-0076).
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    assert runtime.rebind_for_resource is not None


def test_rebind_for_resource_threads_resource_name_into_provisioner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ADR-0187: for_resource('host-b') must resolve the provisioner's config for host-b — proving
    # the resource name is threaded through build_runtime into the port's config_factory.
    seen: list[str] = []

    def fake_for_resource(name: str) -> RemoteLibvirtConfig:
        seen.append(name)
        return RemoteLibvirtConfig(
            uri=f"qemu+tls://{name}.example/system",
            cert_refs=TlsCertRefs(
                client_cert_ref="c", client_key_ref="k", ca_cert_ref="ca"
            ),  # pragma: allowlist secret
            concurrent_allocation_cap=1,
            gdb_addr="10.0.0.1",
        )

    monkeypatch.setattr(composition, "remote_config_for_resource", fake_for_resource)
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    bound = runtime.for_resource("host-b")
    # The provisioner resolves its connection config lazily; pull it to trigger the factory.
    cfg = bound.provisioner._connections.config()  # ty: ignore[unresolved-attribute]
    assert cfg.uri == "qemu+tls://host-b.example/system"
    assert seen == ["host-b"]


def test_for_resource_is_identity_without_rebind_hook() -> None:
    # A runtime with no rebind hook (single-host providers like local-libvirt) returns itself.
    runtime = composition.build_runtime(secret_registry=SecretRegistry())
    plain = replace(runtime, rebind_for_resource=None)
    assert plain.for_resource("anything") is plain
