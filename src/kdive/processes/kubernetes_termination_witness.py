"""Bounded Kubernetes worker-Pod termination witness (#1519)."""

from __future__ import annotations

import asyncio
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FINALIZER = "kdive.io/worker-termination-evidence"

type ReadPod = Callable[[str, str], Mapping[str, Any] | None]
type PatchFinalizers = Callable[[str, str, list[dict[str, object]]], Awaitable[None]]
type TerminateIncarnation = Callable[[str, str], Awaitable[None]]

_TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
_CA = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


def read_pod(namespace: str, name: str) -> Mapping[str, Any] | None:
    """Read one exact namespaced Pod with the witness service account."""
    request = _pod_request(namespace, name)
    try:
        with urllib.request.urlopen(
            request, timeout=3, context=ssl.create_default_context(cafile=str(_CA))
        ) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    if not isinstance(value, Mapping):
        raise RuntimeError("Kubernetes Pod response must be an object")
    return value


def _pod_request(
    namespace: str,
    name: str,
    *,
    data: bytes | None = None,
    method: str = "GET",
    content_type: str | None = None,
) -> urllib.request.Request:
    token = _TOKEN.read_text(encoding="utf-8").strip()
    quoted_namespace = urllib.parse.quote(namespace, safe="")
    quoted_name = urllib.parse.quote(name, safe="")
    headers = {"Authorization": f"Bearer {token}"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return urllib.request.Request(
        f"https://kubernetes.default.svc/api/v1/namespaces/{quoted_namespace}/pods/{quoted_name}",
        headers=headers,
        data=data,
        method=method,
    )


def patch_finalizers(namespace: str, name: str, operations: list[dict[str, object]]) -> None:
    """Apply one resource-version-fenced JSON Patch to an exact Pod."""
    request = _pod_request(
        namespace,
        name,
        data=json.dumps(operations, separators=(",", ":")).encode(),
        method="PATCH",
        content_type="application/json-patch+json",
    )
    with urllib.request.urlopen(
        request, timeout=3, context=ssl.create_default_context(cafile=str(_CA))
    ) as response:
        response.read(1)


async def run_witness(witness: KubernetesTerminationWitness, stop: asyncio.Event) -> None:
    """Run bounded sweeps until process shutdown; failures retain finalizers."""
    while not stop.is_set():
        await witness.sweep_once()
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=5)


def _terminal_claim(pod: Mapping[str, Any]) -> tuple[str, str, str, int] | None:
    metadata = pod.get("metadata")
    status = pod.get("status")
    if not isinstance(metadata, Mapping) or not isinstance(status, Mapping):
        return None
    uid = metadata.get("uid")
    resource_version = metadata.get("resourceVersion")
    phase = status.get("phase")
    finalizers = metadata.get("finalizers")
    if (
        not isinstance(uid, str)
        or not uid
        or not isinstance(resource_version, str)
        or not resource_version
        or phase not in {"Succeeded", "Failed"}
        or not isinstance(finalizers, list)
    ):
        return None
    try:
        index = finalizers.index(FINALIZER)
    except ValueError:
        return None
    if not all(isinstance(value, str) for value in finalizers):
        return None
    return uid, resource_version, str(phase).lower(), index


@dataclass(frozen=True, slots=True)
class KubernetesTerminationWitness:
    """Poll fixed ordinal names and remove finalizers only after durable evidence."""

    namespace: str
    worker_name: str
    ordinal_ceiling: int
    read_pod: ReadPod
    patch_finalizers: PatchFinalizers
    terminate: TerminateIncarnation

    async def sweep_once(self) -> int:
        """Process at most one bounded configured page of exact worker names."""
        completed = 0
        for ordinal in range(self.ordinal_ceiling):
            name = f"{self.worker_name}-{ordinal}"
            pod = self.read_pod(self.namespace, name)
            if pod is None or (claim := _terminal_claim(pod)) is None:
                continue
            uid, resource_version, phase, index = claim
            holder = f"kubernetes:{self.namespace}:{name}:{uid}"
            await self.terminate(holder, f"kubernetes_pod_{phase}")
            operations: list[dict[str, object]] = [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": resource_version,
                },
                {
                    "op": "test",
                    "path": f"/metadata/finalizers/{index}",
                    "value": FINALIZER,
                },
                {"op": "remove", "path": f"/metadata/finalizers/{index}"},
            ]
            await self.patch_finalizers(self.namespace, name, operations)
            completed += 1
        return completed
