"""Remote vmlinux proof selection at the inventory and lifecycle boundaries; not live proof."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration import test_authority_vmlinux_live as carrier
from tests.integration.live_stack.scenario import CellRun
from tests.live_vm.installed_remote_authority_support import (
    RemoteAuthorityInventory,
    RemoteAuthorityProofConfig,
)


@pytest.mark.parametrize("arch", ["x86_64", "ppc64le", "aarch64"])
def test_carrier_uses_the_configured_remote_host_architecture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arch: str
) -> None:
    monkeypatch.setenv(carrier.REMOTE_CONFIG_ENV, "unused-config.json")
    monkeypatch.setenv("KDIVE_DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(tmp_path))
    (tmp_path / "vmlinux").write_bytes(b"unread by the frame boundary")
    config = RemoteAuthorityProofConfig.model_construct()
    monkeypatch.setattr(carrier, "select_remote_lane", lambda: config, raising=False)
    monkeypatch.setattr(
        carrier,
        "remote_authority_inventory",
        lambda _config: RemoteAuthorityInventory(arch, [], [], None),
        raising=False,
    )
    monkeypatch.setattr(carrier, "require_stack", lambda: "http://unused")
    monkeypatch.setattr(carrier, "require_issuer", object)
    entered: list[CellRun] = []

    async def frame(run: CellRun, *_args: object, **_kwargs: object) -> None:
        entered.append(run)
        assert run.cell.provider == "remote-libvirt"
        assert run.cell.host_arch == run.cell.guest_arch == arch
        for assertion in ("effect", "cleanup"):
            run.prove(assertion, {})

    monkeypatch.setattr(carrier, "on_authority_system", frame)
    if arch == "aarch64":
        with pytest.raises(
            pytest.fail.Exception, match="unsupported remote authority architecture"
        ):
            carrier.test_remote_authority_stages_the_uploaded_vmlinux(tmp_path)
        assert entered == []
    else:
        carrier.test_remote_authority_stages_the_uploaded_vmlinux(tmp_path)
        assert len(entered) == 1
