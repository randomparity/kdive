"""Exact local worker-incarnation identity and death verification."""

from pathlib import Path

from kdive.processes.worker_incarnation import (
    LocalWorkerDeathVerifier,
    worker_incarnation_id,
)


def test_worker_incarnation_id_includes_boot_and_process_start(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "boot_id").write_text("boot-123\n")
    (tmp_path / "stat").write_text("1 (python worker) S " + "0 " * 18 + "987 0\n")
    monkeypatch.setattr("kdive.processes.worker_incarnation.socket.gethostname", lambda: "host-a")

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
    monkeypatch.setattr("kdive.processes.worker_incarnation.socket.gethostname", lambda: "host-a")
    verifier = LocalWorkerDeathVerifier(boot_id_path=tmp_path / "boot_id", proc_root=proc)

    evidence = verifier.verify_dead("host-a:42:boot-123:987")

    assert evidence == "local-proc: exact worker incarnation absent (pid start changed)"


def test_verifier_refuses_live_or_foreign_incarnation(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "boot_id").write_text("boot-123\n")
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "42" / "stat").write_text("42 (python worker) S " + "0 " * 18 + "987 0\n")
    monkeypatch.setattr("kdive.processes.worker_incarnation.socket.gethostname", lambda: "host-a")
    verifier = LocalWorkerDeathVerifier(boot_id_path=tmp_path / "boot_id", proc_root=proc)

    assert verifier.verify_dead("host-a:42:boot-123:987") is None
    assert verifier.verify_dead("host-b:42:boot-123:987") is None
