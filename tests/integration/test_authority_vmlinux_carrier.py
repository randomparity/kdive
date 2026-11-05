"""Remote vmlinux proof selection at the inventory and lifecycle boundaries; not live proof."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.integration.live_stack import authority_vmlinux as carrier
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
            carrier.run_remote_authority_vmlinux(tmp_path)
        assert entered == []
    else:
        carrier.run_remote_authority_vmlinux(tmp_path)
        assert len(entered) == 1


def test_power_inventory_reaches_real_activation_and_prebuilt_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import base64
    import hashlib
    import json
    from typing import Any, cast

    from kdive.mcp.responses import ToolResponse
    from tests.integration.live_stack import spine
    from tests.integration.live_stack.authority_lane import AuthoritySystem
    from tests.integration.live_stack.spine_test_support import (
        _INITRD_CONFIG,
        _kernel_tree,
        _prebuilt_power_bundle,
    )
    from tests.live_vm import installed_remote_authority_support as remote
    from tests.live_vm.installed_local_authority_support import ResourceLedger

    tree = _kernel_tree(tmp_path, _INITRD_CONFIG)
    (tree / "vmlinux").write_bytes(b"\x7fELFpower-boot")
    bundle = _prebuilt_power_bundle(monkeypatch, tmp_path)
    monkeypatch.setenv(carrier.REMOTE_CONFIG_ENV, "unused-config.json")
    monkeypatch.setenv("KDIVE_DATABASE_URL", "postgresql://unused")
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(tree))
    config = RemoteAuthorityProofConfig.model_construct(
        ownership_prefix="kdive-2151-aaaaaaaaaaaa-0123abcd"
    )
    monkeypatch.setattr(carrier, "select_remote_lane", lambda: config)
    inventory_reads = []

    def ssh(*args: object, **kwargs: object) -> bytes:
        inventory_reads.append(args)
        return json.dumps(
            {"arch": "ppc64le", "domains": [], "volumes": [], "system": None}
        ).encode()

    monkeypatch.setattr(remote, "_ssh", ssh)
    monkeypatch.setattr(carrier, "require_stack", lambda: "http://unused")
    monkeypatch.setattr(carrier, "require_issuer", object)
    calls: list[tuple[str, dict[str, object]]] = []
    uploaded: dict[str, bytes] = {}
    released = False

    class Client:
        async def read_text_resource(self, _uri: str) -> str:
            return json.dumps(
                {
                    "upload_contracts": {
                        "run": {
                            "accepted_names": ["kernel", "effective_config", "vmlinux", "initrd"]
                        }
                    }
                }
            )

        async def call_tool(self, name: str, **kwargs: object) -> ToolResponse:
            nonlocal released
            calls.append((name, kwargs))
            if name == "artifacts.create_run_upload":
                artifacts = cast(list[dict[str, object]], kwargs["artifacts"])
                return ToolResponse.collection(
                    "run",
                    "pending",
                    [
                        ToolResponse.success("upload", "pending", data={"name": str(item["name"])})
                        for item in artifacts
                    ],
                )
            if name == "runs.release_external_boot":
                released = True
            return ToolResponse.success("run", "succeeded")

    async def put(item: ToolResponse, path: Path) -> None:
        uploaded[str(item.data["name"])] = path.read_bytes()

    monkeypatch.setattr(spine, "put_presigned", put)

    def guest(_config: object, script: str, *_args: str) -> str:
        if script == carrier._DOMAIN_POWER:
            return ""
        argv = json.loads(_args[-1])
        if argv[0] == "/usr/bin/sha256sum":
            return json.dumps(
                {"exitcode": int(released), "out": hashlib.sha256(b"\x7fELFpower-boot").hexdigest()}
            )
        return json.dumps({"exitcode": 0, "out": "7.1.0-spine"})

    monkeypatch.setattr(carrier, "run_as_authority", guest)

    async def frame(run: CellRun, *_args: object, **kwargs: object) -> None:
        body = cast(Any, kwargs["body"])
        system = AuthoritySystem(
            cast(Any, Client()),
            "project",
            "allocation",
            "system",
            config,
            ResourceLedger("kdive-2151-aaaaaaaaaaaa-0123abcd"),
        )
        await body(system)
        for assertion in ("effect", "cleanup"):
            run.prove(assertion, {})

    monkeypatch.setattr(carrier, "on_authority_system", frame)
    carrier.run_remote_authority_vmlinux(tmp_path)
    assert len(inventory_reads) == 1
    created = next(args for name, args in calls if name == "runs.create")
    assert created["build_profile"] == {"schema_version": 1, "arch": "ppc64le"}
    assert uploaded["kernel"] == (bundle / "kernel.tar.gz").read_bytes()
    assert uploaded["initrd"] == (bundle / "initrd.img").read_bytes()
    completed = next(args for name, args in calls if name == "runs.complete_build")
    assert completed["build_id"] == "ab1234"
    declarations = next(
        args["artifacts"] for name, args in calls if name == "artifacts.create_run_upload"
    )
    for item in cast(list[dict[str, object]], declarations):
        content = uploaded[str(item["name"])]
        assert item["size_bytes"] == len(content)
        assert item["sha256"] == base64.b64encode(hashlib.sha256(content).digest()).decode()
