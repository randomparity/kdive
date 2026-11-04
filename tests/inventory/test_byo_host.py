"""BYO declarations are local, refs-only host descriptions (ADR-0813)."""

from __future__ import annotations

import copy
import json
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from kdive.inventory import cli
from kdive.inventory.errors import InventoryError
from kdive.inventory.model import InventoryDoc


def _host(**changes: Any) -> dict[str, Any]:
    return {
        "name": "target-x",
        "arch": "x86_64",
        "ssh_target": "root@target-x.example",
        "ssh_key_ref": "target-ssh-key",
        "baseline_kernel": "6.17.0-baseline",
        "console_device": "ttyS0",
        "vcpus": 8,
        "memory_mb": 32768,
        "oob": {
            "kind": "redfish",
            "endpoint": "https://bmc-x.example",
            "username_ref": "bmc-user",
            "password_ref": "bmc-password",  # pragma: allowlist secret
        },
        **changes,
    }


def _power(**changes: Any) -> dict[str, Any]:
    return _host(
        arch="ppc64le",
        console_device="hvc0",
        oob={
            "kind": "hmc",
            "endpoint": "hmc.example",
            "username_ref": "hmc-user",
            "password_ref": "hmc-password",  # pragma: allowlist secret
            "managed_system": "managed-a",
            "lpar_name": "partition-a",
        },
        **changes,
    )


def test_x86_and_power_declarations() -> None:
    for raw in (_host(), _power()):
        host = InventoryDoc.parse({"schema_version": 2, "byo_host": [raw]}).byo_host[0]
        assert host.arch == raw["arch"]
        assert host.concurrent_allocation_cap == 1
        assert host.cost_class == "byo"
        assert host.pool == "default"
        assert host.pseries_fadump is False
        assert host.ssh_key_ref == raw["ssh_key_ref"]


def test_two_lpars_share_hmc() -> None:
    first = _power()
    second = _power(name="target-y", ssh_target="root@target-y.example")
    second["oob"]["lpar_name"] = "partition-b"
    doc = InventoryDoc.parse({"schema_version": 2, "byo_host": [first, second]})
    assert len(doc.byo_host) == 2


@pytest.mark.parametrize("field", ["name", "ssh_target", "oob"])
def test_duplicate_coordinates_rejected(field: str) -> None:
    first = _power()
    second = _power(name="target-y", ssh_target="root@target-y.example")
    second["oob"]["lpar_name"] = "partition-b"
    second[field] = copy.deepcopy(first[field])
    with pytest.raises(InventoryError, match=field):
        InventoryDoc.parse({"schema_version": 2, "byo_host": [first, second]})


def test_bmc_endpoint_is_unique_across_redfish_and_ipmi() -> None:
    first = _host()
    second = _host(name="target-y", ssh_target="root@target-y.example")
    second["oob"]["kind"] = "ipmi"
    with pytest.raises(InventoryError, match="oob"):
        InventoryDoc.parse({"schema_version": 2, "byo_host": [first, second]})


@pytest.mark.parametrize(
    "changes",
    [
        {"arch": "aarch64"},
        {"arch": "ppc64le"},
        {"name": " "},
        {"ssh_key_ref": ""},
        {"baseline_kernel": " "},
        {"console_device": ""},
        {"vcpus": 0},
        {"vcpus": True},
        {"memory_mb": -1},
        {"memory_mb": "32768"},
        {"concurrent_allocation_cap": 2},
        {"concurrent_allocation_cap": 0},
        {"concurrent_allocation_cap": True},
        {"pseries_fadump": True},
        {"pseries_fadump": "false"},
        {"password": "DO_NOT_ECHO_MATERIAL"},  # pragma: allowlist secret
        {"ssh_target": "ssh://user:DO_NOT_ECHO_MATERIAL@x.example"},  # pragma: allowlist secret
    ],
)
def test_byo_rejects_invalid_fields(changes: dict[str, Any]) -> None:
    with pytest.raises(InventoryError) as exc:
        InventoryDoc.parse({"schema_version": 2, "byo_host": [_host(**changes)]})
    assert "byo_host" in str(exc.value)
    assert "DO_NOT_ECHO_MATERIAL" not in str(exc.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "unknown"},
        {"username_ref": " "},
        {"password_ref": ""},
        {"managed_system": "unexpected"},
        {"lpar_name": "unexpected"},
        {"password": "DO_NOT_ECHO_MATERIAL"},  # pragma: allowlist secret
        {"endpoint": "https://user:DO_NOT_ECHO_MATERIAL@x.example"},  # pragma: allowlist secret
    ],
)
def test_byo_rejects_invalid_oob(changes: dict[str, Any]) -> None:
    raw = _host()
    raw["oob"].update(changes)
    with pytest.raises(InventoryError) as exc:
        InventoryDoc.parse({"schema_version": 2, "byo_host": [raw]})
    assert "byo_host[0].oob" in str(exc.value)
    assert "DO_NOT_ECHO_MATERIAL" not in str(exc.value)


@pytest.mark.parametrize("field", ["managed_system", "lpar_name"])
def test_hmc_requires_both_partition_coordinates(field: str) -> None:
    raw = _power()
    del raw["oob"][field]
    with pytest.raises(InventoryError, match="oob"):
        InventoryDoc.parse({"schema_version": 2, "byo_host": [raw]})


def test_explicit_power_fadump_is_a_declaration() -> None:
    host = InventoryDoc.parse({"schema_version": 2, "byo_host": [_power(pseries_fadump=True)]})
    assert host.byo_host[0].pseries_fadump is True


def test_byo_schema_check_uses_no_external_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("schema check attempted external I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(cli, "AsyncConnectionPool", forbidden)
    path = tmp_path / "systems.toml"
    valid = """schema_version = 2
[[byo_host]]
name = "target-x"
arch = "x86_64"
ssh_target = "root@target-x.example"
ssh_key_ref = "target-ssh-key"
baseline_kernel = "6.17.0-baseline"
console_device = "ttyS0"
vcpus = 8
memory_mb = 32768
[byo_host.oob]
kind = "redfish"
endpoint = "https://bmc-x.example"
username_ref = "bmc-user"
password_ref = "bmc-password"  # pragma: allowlist secret
"""
    path.write_text(valid)
    assert cli.validate_systems(path) == 0
    path.write_text(valid.replace("vcpus = 8", "vcpus = 0"))
    assert cli.validate_systems(path) == 1
    assert "byo_host[0].vcpus:" in capsys.readouterr().err


def test_documented_byo_examples_validate() -> None:
    import tomllib

    source = (Path(__file__).resolve().parents[2] / "systems.toml.example").read_text()
    examples = []
    for block in source.split("# [[byo_host]]")[1:]:
        lines = block.split("\n\n", 1)[0].splitlines()
        text = "schema_version = 2\n[[byo_host]]\n" + "\n".join(
            line.removeprefix("# ") for line in lines if line.startswith("# ")
        )
        examples.extend(InventoryDoc.parse(tomllib.loads(text)).byo_host)
    assert {host.arch for host in examples} == {"x86_64", "ppc64le"}


def test_mixed_errors_hide_rejected_byo_secret() -> None:
    raw = _host()
    raw["oob"]["password"] = "DO_NOT_ECHO_MATERIAL"  # pragma: allowlist secret
    with pytest.raises(InventoryError) as exc:
        InventoryDoc.parse({"schema_version": 1, "byo_host": [raw]})
    assert "DO_NOT_ECHO_MATERIAL" not in str(exc.value)


def test_cli_mixed_errors_hide_rejected_byo_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "systems.toml"
    path.write_text(
        "schema_version=1\n[[byo_host]]\n"
        'password="DO_NOT_ECHO_MATERIAL"\n'  # pragma: allowlist secret
    )
    assert cli.validate_systems(path) == 1
    assert "DO_NOT_ECHO_MATERIAL" not in capsys.readouterr().err


@pytest.mark.parametrize("field", ["ssh_target", "endpoint"])
@pytest.mark.parametrize("diagnostic", ["parser", "cli"])
def test_malformed_authority_hides_rejected_secret(
    field: str,
    diagnostic: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = _host()
    target = raw["oob"] if field == "endpoint" else raw
    target[field] = "https://user:DO_NOT_ECHO_MATERIAL@x.example\uff0f"  # pragma: allowlist secret
    if diagnostic == "parser":
        with pytest.raises(InventoryError) as exc:
            InventoryDoc.parse({"schema_version": 2, "byo_host": [raw]})
        message = str(exc.value)
    else:
        lines = ["schema_version=2", "[[byo_host]]"]
        lines.extend(f"{key}={json.dumps(value)}" for key, value in raw.items() if key != "oob")
        lines.append("[byo_host.oob]")
        lines.extend(f"{key}={json.dumps(value)}" for key, value in raw["oob"].items())
        path = tmp_path / "systems.toml"
        path.write_text("\n".join(lines))
        assert cli.validate_systems(path) == 1
        message = capsys.readouterr().err
    assert field in message
    assert "DO_NOT_ECHO_MATERIAL" not in message
