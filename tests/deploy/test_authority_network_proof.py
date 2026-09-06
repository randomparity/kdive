"""The live authority carrier publishes only closed, non-identifying evidence."""

import json
from pathlib import Path

import pytest
import yaml


def test_authority_tag_selects_only_complete_production_role() -> None:
    site = Path(__file__).parents[2] / "deploy" / "ansible" / "site.yml"
    plays = yaml.safe_load(site.read_text())
    selected = [
        (play["hosts"], role)
        for play in plays
        for role in play.get("roles", [])
        if isinstance(role, dict) and "provider_authority_host" in role.get("tags", [])
    ]
    assert selected == [
        (
            "remote_libvirt_hosts",
            {"role": "provider_authority_host", "tags": ["provider_authority_host"]},
        )
    ]
    assert all("provider_authority_host" not in play.get("tags", []) for play in plays)


def test_carrier_exists() -> None:
    assert (Path(__file__).parents[1] / "live_vm" / "authority_network_proof.py").is_file()


def test_config_rejects_unsafe_file_and_extra_fields(tmp_path: Path) -> None:
    from tests.live_vm import authority_network_proof as proof

    config = tmp_path / "proof.json"
    config.write_text("{}")
    config.chmod(0o644)
    with pytest.raises(ValueError, match="invalid-proof-config"):
        proof.load_config(config)
    config.chmod(0o600)
    config.write_text(json.dumps({"command": "private-input"}))
    with pytest.raises(ValueError, match="invalid-proof-config"):
        proof.load_config(config)


@pytest.mark.anyio
async def test_four_outcomes_and_failure_redaction() -> None:
    from tests.live_vm import authority_network_proof as proof

    async def success() -> None:
        return None

    async def denial() -> None:
        raise proof.CategorizedError(
            "authority: tls-rejected", category=proof.ErrorCategory.INFRASTRUCTURE_FAILURE
        )

    async def private_failure() -> None:
        raise RuntimeError("private-host private-credential private-peer-output")

    outcomes = await proof.collect_outcomes(success, denial, denial, success)
    assert outcomes == {
        "configured_success": {"passed": True, "reason": "health-acknowledged"},
        "untrusted_client_denial": {"passed": True, "reason": "connection-denied"},
        "non_configured_destination_denial": {"passed": True, "reason": "connection-denied"},
        "preserved_af_unix_success": {"passed": True, "reason": "health-acknowledged"},
    }
    failures = await proof.collect_outcomes(
        private_failure, success, private_failure, private_failure
    )
    assert [value["passed"] for value in failures.values()] == [False] * 4
    assert {value["reason"] for value in failures.values()} == {
        "probe-failed",
        "unexpected-acceptance",
    }
    assert "private" not in json.dumps(failures)


@pytest.mark.anyio
async def test_invalid_configuration_is_not_denial() -> None:
    from tests.live_vm import authority_network_proof as proof

    async def invalid() -> None:
        raise proof.CategorizedError(
            "authority: tls-material-invalid", category=proof.ErrorCategory.CONFIGURATION_ERROR
        )

    outcomes = await proof.collect_outcomes(invalid, invalid, invalid, invalid)
    assert all(value == {"passed": False, "reason": "probe-failed"} for value in outcomes.values())


def test_unix_helper_has_fixed_command_and_paths() -> None:
    from tests.live_vm import authority_network_proof as proof

    argv = proof.unix_helper_argv("proof-target")
    assert argv[:7] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        "proof-target",
    ]
    assert "/opt/kdive-provider-authority/.venv/bin/python" in argv[7]
    assert "open_unix_connection" in argv[7]
    assert "read_frame" in argv[7]
    assert "asyncio.timeout(10)" in argv[7]
    with pytest.raises(ValueError, match="invalid-proof-config"):
        proof.unix_helper_argv("proof-target; arbitrary-command")


def test_local_namespace_uses_same_fixed_unix_helper() -> None:
    from tests.live_vm import authority_network_proof as proof

    assert proof.unix_helper_argv(None) == [
        "sudo",
        "-n",
        "/opt/kdive-provider-authority/.venv/bin/python",
        "-c",
        proof._UNIX_HELPER,
    ]
