"""Exact local worker-incarnation identity and death verification."""

from pathlib import Path

import pytest

import kdive.config as config
import kdive.processes.lifecycle.worker_incarnation as worker_incarnations
from kdive.processes.lifecycle.worker_incarnation import (
    DockerWorkerDeathVerifier,
    KubernetesWorkerDeathVerifier,
    LocalWorkerDeathVerifier,
    worker_death_verifier_from_env,
    worker_incarnation_id,
)


def test_worker_incarnation_id_includes_boot_and_process_start(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "boot_id").write_text("boot-123\n")
    (tmp_path / "stat").write_text("1 (python worker) S " + "0 " * 18 + "987 0\n")
    monkeypatch.setattr(
        "kdive.processes.lifecycle.worker_incarnation.socket.gethostname", lambda: "host-a"
    )

    identity = worker_incarnation_id(
        42, boot_id_path=tmp_path / "boot_id", stat_path=tmp_path / "stat"
    )
    assert identity == "host-a:42:boot-123:987"


def test_verifier_proves_exact_incarnation_dead_when_pid_start_changed(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "boot_id").write_text("boot-123\n")
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "42" / "stat").write_text("42 (new worker) S " + "0 " * 18 + "999 0\n")
    monkeypatch.setattr(
        "kdive.processes.lifecycle.worker_incarnation.socket.gethostname", lambda: "host-a"
    )
    verifier = LocalWorkerDeathVerifier(boot_id_path=tmp_path / "boot_id", proc_root=proc)

    evidence = verifier.verify_dead("host-a:42:boot-123:987")

    assert evidence == "local-proc: exact worker incarnation absent (pid start changed)"


def test_verifier_refuses_live_or_foreign_incarnation(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "boot_id").write_text("boot-123\n")
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "42" / "stat").write_text("42 (python worker) S " + "0 " * 18 + "987 0\n")
    monkeypatch.setattr(
        "kdive.processes.lifecycle.worker_incarnation.socket.gethostname", lambda: "host-a"
    )
    verifier = LocalWorkerDeathVerifier(boot_id_path=tmp_path / "boot_id", proc_root=proc)

    assert verifier.verify_dead("host-a:42:boot-123:987") is None
    assert verifier.verify_dead("host-b:42:boot-123:987") is None


def test_docker_identity_binds_container_and_verifier_requires_actual_stop(monkeypatch) -> None:
    monkeypatch.setenv("KDIVE_WORKER_INCARNATION_KIND", "docker")
    monkeypatch.setenv("KDIVE_WORKER_INCARNATION_ID", "docker:nonce-123")
    assert worker_incarnation_id(42) == "docker:nonce-123"

    stopped = DockerWorkerDeathVerifier(
        inspect=lambda container: {"Id": "a" * 64, "State": {"Running": False}}
    )
    live = DockerWorkerDeathVerifier(
        inspect=lambda container: {"Id": container, "State": {"Running": True}}
    )
    assert (
        stopped.verify_dead(f"docker:{'a' * 64}") == "docker: exact container incarnation stopped"
    )
    assert live.verify_dead(f"docker:{'a' * 64}") is None
    assert stopped.verify_dead(f"docker:{'b' * 64}") is None


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


def test_kubernetes_identity_binds_pod_uid_and_accepts_only_same_uid_terminal_pod(
    monkeypatch,
) -> None:
    monkeypatch.setenv("KDIVE_WORKER_INCARNATION_KIND", "kubernetes")
    monkeypatch.setenv("KDIVE_POD_NAMESPACE", "kdive")
    monkeypatch.setenv("KDIVE_POD_NAME", "kdive-worker-0")
    monkeypatch.setenv("KDIVE_POD_UID", "4a86b0a3-4ba2-4a25-9c3c-12d385e3bcaa")
    holder = "kubernetes:kdive:kdive-worker-0:4a86b0a3-4ba2-4a25-9c3c-12d385e3bcaa"
    assert worker_incarnation_id(42) == holder

    replaced = KubernetesWorkerDeathVerifier(
        read_pod=lambda namespace, name: {
            "metadata": {"uid": "different"},
            "status": {"phase": "Running"},
        }
    )
    live = KubernetesWorkerDeathVerifier(
        read_pod=lambda namespace, name: {
            "metadata": {"uid": "4a86b0a3-4ba2-4a25-9c3c-12d385e3bcaa"},
            "status": {"phase": "Running"},
        }
    )
    terminal = KubernetesWorkerDeathVerifier(
        read_pod=lambda namespace, name: {
            "metadata": {"uid": "4a86b0a3-4ba2-4a25-9c3c-12d385e3bcaa"},
            "status": {"phase": "Failed"},
        }
    )
    absent = KubernetesWorkerDeathVerifier(read_pod=lambda namespace, name: None)
    assert replaced.verify_dead(holder) is None
    assert absent.verify_dead(holder) is None
    assert live.verify_dead(holder) is None
    assert terminal.verify_dead(holder) == "kubernetes: exact pod incarnation terminated"


def test_deployment_authority_errors_and_docker_absence_fail_closed() -> None:
    docker_absent = DockerWorkerDeathVerifier(inspect=lambda container: None)

    def unavailable(namespace: str, name: str):
        raise OSError("authority unavailable")

    kubernetes_unavailable = KubernetesWorkerDeathVerifier(read_pod=unavailable)
    holder = "kubernetes:kdive:kdive-worker-0:4a86b0a3-4ba2-4a25-9c3c-12d385e3bcaa"
    assert docker_absent.verify_dead(f"docker:{'a' * 64}") is None
    assert kubernetes_unavailable.verify_dead(holder) is None


def test_verifier_factory_fails_closed_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("KDIVE_WORKER_DEATH_VERIFIER", raising=False)
    assert worker_death_verifier_from_env() is None
    monkeypatch.setenv("KDIVE_WORKER_DEATH_VERIFIER", "local")
    config.reset()
    assert isinstance(worker_death_verifier_from_env(), LocalWorkerDeathVerifier)
