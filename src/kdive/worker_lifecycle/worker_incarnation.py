"""Non-reusable worker identity and recovery-tool registration configuration."""

from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr

import kdive.config as config
from kdive.config.core_settings import (
    DOCKER_DEATH_API,
    POD_NAME,
    POD_NAMESPACE,
    POD_UID,
    WORKER_DEATH_VERIFIER,
    WORKER_INCARNATION_ID,
    WORKER_INCARNATION_KIND,
)
from kdive.worker_lifecycle.contracts import WorkerDeathVerifier

_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_PROC_ROOT = Path("/proc")
_INCARNATION_CREDENTIAL = Path("/run/kdive/worker-incarnation-credential")
_LOCAL_SYSTEMD_ID = re.compile(r"local-systemd:kdive-live-worker@[1-8]\.service:[0-9a-f]{32}")


def _start_ticks(stat: str) -> str:
    """Extract Linux ``/proc/PID/stat`` field 22 despite spaces in ``comm``."""
    close = stat.rfind(")")
    fields = stat[close + 2 :].split() if close >= 0 else []
    if len(fields) <= 19 or not fields[19].isdigit():
        raise RuntimeError("worker process stat has no valid start-time field")
    return fields[19]


def _configured_local_systemd_id() -> str | None:
    incarnation = config.get(WORKER_INCARNATION_ID)
    if incarnation is None:
        return None
    if not _LOCAL_SYSTEMD_ID.fullmatch(incarnation):
        raise RuntimeError("local worker incarnation must be a fixed local-systemd identity")
    return incarnation


def worker_incarnation_id(
    pid: int,
    *,
    boot_id_path: Path = _BOOT_ID,
    stat_path: Path | None = None,
) -> str:
    """Return the configured deployment's immutable worker-incarnation identity."""
    kind = config.require(WORKER_INCARNATION_KIND)
    if kind == "docker":
        incarnation = config.require(WORKER_INCARNATION_ID)
        if not incarnation.startswith("docker:") or len(incarnation) > 512:
            raise RuntimeError("Docker worker incarnation must be a bounded docker: identity")
        return incarnation
    if kind == "kubernetes":
        namespace = config.get(POD_NAMESPACE) or ""
        name = config.get(POD_NAME) or ""
        uid = config.get(POD_UID) or ""
        if not namespace or not name or not uid or ":" in uid:
            raise RuntimeError("kubernetes worker identity requires pod namespace, name, and UID")
        return f"kubernetes:{namespace}:{name}:{uid}"
    if kind != "local":
        raise RuntimeError(f"unsupported worker incarnation kind: {kind}")
    if incarnation := _configured_local_systemd_id():
        return incarnation
    boot_id = boot_id_path.read_text(encoding="utf-8").strip()
    stat = (stat_path or (_PROC_ROOT / str(pid) / "stat")).read_text(encoding="utf-8")
    return f"{socket.gethostname()}:{pid}:{boot_id}:{_start_ticks(stat)}"


def worker_incarnation_credential(path: Path | None = None) -> SecretStr:
    """Load the authority-delivered credential from systemd LoadCredential or the handoff."""
    if path is not None:
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise RuntimeError("worker incarnation credential handoff is empty")
        return SecretStr(value)
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    locations = [Path(directory) / "worker-incarnation" if directory else _INCARNATION_CREDENTIAL]
    if directory:
        locations.append(_INCARNATION_CREDENTIAL)
    failures: list[str] = []
    for location in locations:
        try:
            value = location.read_text(encoding="utf-8").strip()
        except OSError as exc:
            failures.append(f"{location}: {exc.strerror or exc}")
            continue
        if not value:
            failures.append(f"{location}: empty")
            continue
        return SecretStr(value)
    raise RuntimeError(
        "worker incarnation credential not found at any checked location: " + "; ".join(failures)
    )


@dataclass(frozen=True, slots=True)
class LocalWorkerDeathVerifier(WorkerDeathVerifier):
    """Legacy local configuration marker; production assembly leaves recovery tools out."""


@dataclass(frozen=True, slots=True)
class DockerWorkerDeathVerifier(WorkerDeathVerifier):
    """Docker configuration marker enabling recovery-tool registration, not death proof."""

    endpoint: str = "http://worker-death-api:2375"


@dataclass(frozen=True, slots=True)
class KubernetesWorkerDeathVerifier(WorkerDeathVerifier):
    """Kubernetes configuration marker enabling recovery-tool registration, not death proof."""


def worker_death_verifier_from_env() -> WorkerDeathVerifier | None:
    """Build the registration marker, preserving the existing configuration validation.

    ADR-0722's recovery-evidence amendment assigns death proof to the witness's
    exact-holder termination row. These markers never verify a worker directly.
    """
    kind = config.require(WORKER_DEATH_VERIFIER)
    if kind == "disabled":
        return None
    if kind == "local":
        return LocalWorkerDeathVerifier()
    if kind == "docker":
        endpoint = config.require(DOCKER_DEATH_API)
        if not endpoint.startswith("http://"):
            raise RuntimeError("KDIVE_DOCKER_DEATH_API must use http:// on a private network")
        return DockerWorkerDeathVerifier(endpoint=endpoint)
    if kind == "kubernetes":
        return KubernetesWorkerDeathVerifier()
    raise RuntimeError(f"unsupported KDIVE_WORKER_DEATH_VERIFIER: {kind}")
