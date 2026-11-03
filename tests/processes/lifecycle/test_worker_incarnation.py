"""Exact worker-incarnation identity and recovery registration configuration."""

from pathlib import Path

import pytest

import kdive.config as config
import kdive.worker_lifecycle.worker_incarnation as worker_incarnations
from kdive.worker_lifecycle.worker_incarnation import (
    LocalWorkerDeathVerifier,
    worker_death_verifier_from_env,
    worker_incarnation_id,
)


def test_worker_incarnation_id_includes_boot_and_process_start(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "boot_id").write_text("boot-123\n")
    (tmp_path / "stat").write_text("1 (python worker) S " + "0 " * 18 + "987 0\n")
    monkeypatch.setattr(
        "kdive.worker_lifecycle.worker_incarnation.socket.gethostname", lambda: "host-a"
    )

    identity = worker_incarnation_id(
        42, boot_id_path=tmp_path / "boot_id", stat_path=tmp_path / "stat"
    )
    assert identity == "host-a:42:boot-123:987"


def test_local_systemd_identity_is_exactly_the_configured_holder(monkeypatch) -> None:
    holder = "local-systemd:kdive-live-worker@3.service:" + "a" * 32
    monkeypatch.setenv("KDIVE_WORKER_INCARNATION_KIND", "local")
    monkeypatch.setenv("KDIVE_WORKER_INCARNATION_ID", holder)

    assert worker_incarnation_id(42) == holder


@pytest.mark.parametrize(
    "holder",
    [
        "local-systemd:kdive-live-worker@0.service:" + "a" * 32,
        "local-systemd:other@1.service:" + "a" * 32,
        "local-systemd:kdive-live-worker@1.service:not-hex",
    ],
)
def test_local_systemd_identity_rejects_malformed_holder(monkeypatch, holder: str) -> None:
    monkeypatch.setenv("KDIVE_WORKER_INCARNATION_KIND", "local")
    monkeypatch.setenv("KDIVE_WORKER_INCARNATION_ID", holder)

    with pytest.raises(RuntimeError, match="local-systemd"):
        worker_incarnation_id(42)


def test_docker_identity_binds_container(monkeypatch) -> None:
    monkeypatch.setenv("KDIVE_WORKER_INCARNATION_KIND", "docker")
    monkeypatch.setenv("KDIVE_WORKER_INCARNATION_ID", "docker:nonce-123")
    assert worker_incarnation_id(42) == "docker:nonce-123"


def test_worker_incarnation_credential_is_loaded_as_a_secret(tmp_path: Path) -> None:
    path = tmp_path / "incarnation-credential"
    path.write_text("authority-delivered-credential\n", encoding="utf-8")

    credential = worker_incarnations.worker_incarnation_credential(path)

    assert credential.get_secret_value() == "authority-delivered-credential"
    assert "authority-delivered-credential" not in repr(credential)


def test_worker_incarnation_credential_rejects_blank_handoff(tmp_path: Path) -> None:
    path = tmp_path / "incarnation-credential"
    path.write_text(" \n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="incarnation credential handoff is empty"):
        worker_incarnations.worker_incarnation_credential(path)


def test_worker_incarnation_credential_prefers_explicit_path_over_environment(
    tmp_path: Path, monkeypatch
) -> None:
    explicit = tmp_path / "explicit-credential"
    explicit.write_text("explicit-credential\n", encoding="utf-8")
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    (systemd / "worker-incarnation").write_text("systemd-credential\n", encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(systemd))

    credential = worker_incarnations.worker_incarnation_credential(explicit)

    assert credential.get_secret_value() == "explicit-credential"


def test_worker_incarnation_credential_reads_systemd_credential_directory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    (tmp_path / "worker-incarnation").write_text("systemd-secret\n", encoding="utf-8")

    credential = worker_incarnations.worker_incarnation_credential()

    assert credential.get_secret_value() == "systemd-secret"


def test_worker_incarnation_credential_fails_loud_when_no_location_yields(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))

    with pytest.raises(RuntimeError, match="checked location") as exc_info:
        worker_incarnations.worker_incarnation_credential()

    message = str(exc_info.value)
    assert str(tmp_path / "worker-incarnation") in message
    assert "/run/kdive/worker-incarnation-credential" in message


def test_kubernetes_identity_binds_pod_uid(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KDIVE_WORKER_INCARNATION_KIND", "kubernetes")
    monkeypatch.setenv("KDIVE_POD_NAMESPACE", "kdive")
    monkeypatch.setenv("KDIVE_POD_NAME", "kdive-worker-0")
    monkeypatch.setenv("KDIVE_POD_UID", "4a86b0a3-4ba2-4a25-9c3c-12d385e3bcaa")
    holder = "kubernetes:kdive:kdive-worker-0:4a86b0a3-4ba2-4a25-9c3c-12d385e3bcaa"
    assert worker_incarnation_id(42) == holder


def test_verifier_factory_fails_closed_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("KDIVE_WORKER_DEATH_VERIFIER", raising=False)
    assert worker_death_verifier_from_env() is None
    monkeypatch.setenv("KDIVE_WORKER_DEATH_VERIFIER", "local")
    config.reset()
    assert isinstance(worker_death_verifier_from_env(), LocalWorkerDeathVerifier)


@pytest.mark.parametrize("kind", ["local", "docker", "kubernetes"])
def test_configured_recovery_gate_has_no_direct_death_verification(monkeypatch, kind: str) -> None:
    monkeypatch.setenv("KDIVE_WORKER_DEATH_VERIFIER", kind)
    gate = worker_death_verifier_from_env()

    assert gate is not None
    assert not hasattr(gate, "verify_dead")


def test_docker_registration_gate_preserves_endpoint_validation(monkeypatch) -> None:
    monkeypatch.setenv("KDIVE_WORKER_DEATH_VERIFIER", "docker")
    monkeypatch.setenv("KDIVE_DOCKER_DEATH_API", "https://authority.example")

    with pytest.raises(RuntimeError, match="must use http:// on a private network"):
        worker_death_verifier_from_env()
