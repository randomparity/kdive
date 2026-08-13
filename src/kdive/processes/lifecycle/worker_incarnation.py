"""Non-reusable local worker identity and authoritative death verification."""

from __future__ import annotations

import json
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import psycopg
from psycopg.types.json import Jsonb
from pydantic import SecretStr

import kdive.config as config
from kdive.config.core_settings import (
    DATABASE_URL,
    DOCKER_DEATH_API,
    POD_NAME,
    POD_NAMESPACE,
    POD_UID,
    WORKER_DEATH_VERIFIER,
    WORKER_INCARNATION_ID,
    WORKER_INCARNATION_KIND,
)

_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
_PROC_ROOT = Path("/proc")
_INCARNATION_CREDENTIAL = Path("/run/kdive/worker-incarnation-credential")
_CONTAINER_ID = re.compile(r"[0-9a-f]{12}(?:[0-9a-f]{52})?")
_KUBE_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")


class WorkerDeathVerifier(Protocol):
    """Deployment authority capable of proving an immutable worker incarnation dead."""

    def verify_dead(self, worker_incarnation: str) -> str | None: ...


class CutoverConnection(Protocol):
    """Synchronous authority connection used by the offline local cutover."""

    def execute(self, query: str, params: object = None) -> Any: ...


def _start_ticks(stat: str) -> str:
    """Extract Linux ``/proc/PID/stat`` field 22 despite spaces in ``comm``."""
    close = stat.rfind(")")
    fields = stat[close + 2 :].split() if close >= 0 else []
    if len(fields) <= 19 or not fields[19].isdigit():
        raise RuntimeError("worker process stat has no valid start-time field")
    return fields[19]


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
    boot_id = boot_id_path.read_text(encoding="utf-8").strip()
    stat = (stat_path or (_PROC_ROOT / str(pid) / "stat")).read_text(encoding="utf-8")
    return f"{socket.gethostname()}:{pid}:{boot_id}:{_start_ticks(stat)}"


def worker_incarnation_credential(path: Path = _INCARNATION_CREDENTIAL) -> SecretStr:
    """Load the authority-delivered credential from its init-only runtime handoff."""
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("worker incarnation credential handoff is empty")
    return SecretStr(value)


@dataclass(frozen=True, slots=True)
class LocalWorkerDeathVerifier:
    """Prove a local incarnation absent from Linux process identity, never from heartbeat age."""

    boot_id_path: Path = _BOOT_ID
    proc_root: Path = _PROC_ROOT

    def verify_dead(self, worker_incarnation: str) -> str | None:
        """Return bounded authoritative evidence, or ``None`` when death is not proven."""
        try:
            host, raw_pid, expected_boot, expected_start = worker_incarnation.rsplit(":", 3)
            pid = int(raw_pid)
        except TypeError, ValueError:
            return None
        if host != socket.gethostname() or pid <= 0:
            return None
        current_boot = self.boot_id_path.read_text(encoding="utf-8").strip()
        if current_boot != expected_boot:
            return "local-proc: exact worker incarnation absent (host rebooted)"
        try:
            current_start = _start_ticks(
                (self.proc_root / str(pid) / "stat").read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return "local-proc: exact worker incarnation absent (pid absent)"
        except OSError, RuntimeError:
            return None
        if current_start != expected_start:
            return "local-proc: exact worker incarnation absent (pid start changed)"
        return None


def terminate_local_cutover_incarnations(
    conn: CutoverConnection,
    verifier: WorkerDeathVerifier,
) -> list[tuple[str, str]]:
    """Verify and terminate every recorded legacy local worker as one DB transaction.

    Every candidate is checked before the first security-definer authority call. This keeps an
    unreadable or still-live process from producing a partial local termination set that an
    operator could mistake for a completed precondition.
    """
    rows = conn.execute(
        "SELECT incarnation, authority_binding FROM public.worker_incarnations "
        "WHERE fence_protocol < 3 AND authority_kind = 'local' AND state = 'active' "
        "ORDER BY incarnation"
    ).fetchall()
    verified: list[tuple[str, dict[str, str], str]] = []
    for incarnation, raw_binding in rows:
        binding = raw_binding if isinstance(raw_binding, dict) else {}
        host = binding.get("host")
        if not isinstance(incarnation, str) or not isinstance(host, str):
            raise RuntimeError(
                f"local protocol-2 incarnation has invalid authority facts: {incarnation!r}"
            )
        try:
            identity_host, _pid, _boot_id, _start_ticks = incarnation.rsplit(":", 3)
            evidence = verifier.verify_dead(incarnation)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                f"local protocol-2 incarnation is not provably terminated: {incarnation}"
            ) from exc
        if identity_host != host or evidence is None:
            raise RuntimeError(
                f"local protocol-2 incarnation is not provably terminated: {incarnation}"
            )
        verified.append((incarnation, {"host": host}, evidence))

    persisted: list[tuple[str, str]] = []
    for incarnation, binding, evidence in verified:
        row = conn.execute(
            "SELECT public.terminate_worker_incarnation(%s, %s, %s, %s)",
            (incarnation, "local", Jsonb(binding), "killed"),
        ).fetchone()
        if row is None or row[0] is not True:
            raise RuntimeError(
                f"local lifecycle authority refused exact termination: {incarnation}"
            )
        persisted.append((incarnation, evidence))
    return persisted


def check_local_cutover_authority(conn: CutoverConnection) -> None:
    """Reject malformed or foreign legacy authority rows before stopping host processes."""
    rows = conn.execute(
        "SELECT incarnation, authority_kind, authority_binding "
        "FROM public.worker_incarnations WHERE fence_protocol < 3 ORDER BY incarnation"
    ).fetchall()
    blockers: list[str] = []
    for incarnation, authority_kind, raw_binding in rows:
        binding = raw_binding if isinstance(raw_binding, dict) else {}
        try:
            identity_host, raw_pid, boot_id, start_ticks = str(incarnation).rsplit(":", 3)
        except ValueError:
            blockers.append(str(incarnation))
            continue
        if (
            authority_kind != "local"
            or binding.get("host") != identity_host
            or not raw_pid.isdigit()
            or int(raw_pid) <= 0
            or not boot_id
            or not start_ticks.isdigit()
            or int(start_ticks) <= 0
        ):
            blockers.append(str(incarnation))
    if blockers:
        raise RuntimeError(
            "host cutover lacks exact local lifecycle authority for protocol-2 incarnations: "
            + ", ".join(blockers)
        )


type InspectContainer = Callable[[str], Mapping[str, Any] | None]


def _docker_inspect(endpoint: str, container_id: str) -> Mapping[str, Any] | None:
    quoted = urllib.parse.quote(container_id, safe="")
    try:
        with urllib.request.urlopen(
            f"{endpoint.rstrip('/')}/containers/{quoted}/json", timeout=3
        ) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


@dataclass(frozen=True, slots=True)
class DockerWorkerDeathVerifier:
    """Prove container termination through a read-only Docker API authority."""

    endpoint: str = "http://worker-death-api:2375"
    inspect: InspectContainer | None = None

    def verify_dead(self, worker_incarnation: str) -> str | None:
        prefix = "docker:"
        container_id = worker_incarnation.removeprefix(prefix)
        if not worker_incarnation.startswith(prefix) or not _CONTAINER_ID.fullmatch(container_id):
            return None
        try:
            state = (self.inspect or (lambda value: _docker_inspect(self.endpoint, value)))(
                container_id
            )
        except OSError, ValueError:
            return None
        if state is None:
            # Absence is not process-termination evidence: daemon state may have been lost.
            return None
        inspected_id = state.get("Id")
        if not isinstance(inspected_id, str) or not inspected_id.startswith(container_id):
            return None
        container_state = state.get("State")
        if isinstance(container_state, Mapping) and container_state.get("Running") is False:
            return "docker: exact container incarnation stopped"
        return None


type ReadPod = Callable[[str, str], Mapping[str, Any] | None]


def _read_kubernetes_pod(namespace: str, name: str) -> Mapping[str, Any] | None:
    token_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    ca_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    token = token_path.read_text(encoding="utf-8").strip()
    quoted_namespace = urllib.parse.quote(namespace, safe="")
    quoted_name = urllib.parse.quote(name, safe="")
    request = urllib.request.Request(
        f"https://kubernetes.default.svc/api/v1/namespaces/{quoted_namespace}/pods/{quoted_name}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=3, context=ssl.create_default_context(cafile=str(ca_path))
        ) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


@dataclass(frozen=True, slots=True)
class KubernetesWorkerDeathVerifier:
    """Prove pod-incarnation termination through namespaced Kubernetes pod reads."""

    read_pod: ReadPod = _read_kubernetes_pod

    def verify_dead(self, worker_incarnation: str) -> str | None:
        try:
            kind, namespace, name, uid = worker_incarnation.split(":", 3)
        except ValueError:
            return None
        if (
            kind != "kubernetes"
            or not _KUBE_NAME.fullmatch(namespace)
            or not _KUBE_NAME.fullmatch(name)
            or not uid
        ):
            return None
        try:
            pod = self.read_pod(namespace, name)
        except OSError, ValueError:
            return None
        if pod is None:
            # A force-delete or API partition can hide a Pod while its process still runs.
            return None
        metadata = pod.get("metadata")
        if not isinstance(metadata, Mapping) or metadata.get("uid") != uid:
            # Name reuse proves only that the API object changed, not that the old process died.
            return None
        status = pod.get("status")
        if isinstance(status, Mapping) and status.get("phase") in {"Succeeded", "Failed"}:
            return "kubernetes: exact pod incarnation terminated"
        return None


def worker_death_verifier_from_env() -> WorkerDeathVerifier | None:
    """Build the explicitly configured authority, or disable recovery when absent."""
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


def _terminate_local_cutover() -> None:
    with psycopg.connect(config.require(DATABASE_URL)) as conn:
        evidence = terminate_local_cutover_incarnations(
            cast(CutoverConnection, conn), LocalWorkerDeathVerifier()
        )
    for incarnation, observation in evidence:
        print(f"terminated {incarnation}: {observation}")


def _check_local_cutover_authority() -> None:
    with psycopg.connect(config.require(DATABASE_URL)) as conn:
        check_local_cutover_authority(cast(CutoverConnection, conn))


def main(argv: list[str] | None = None) -> None:
    """Run the narrow operator-only local lifecycle cutover authority."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["check-local-cutover-authority"]:
        _check_local_cutover_authority()
        return
    if arguments != ["terminate-local-cutover"]:
        raise SystemExit(
            "usage: python -m kdive.processes.lifecycle.worker_incarnation "
            "{check-local-cutover-authority|terminate-local-cutover}"
        )
    _terminate_local_cutover()


if __name__ == "__main__":
    main()
