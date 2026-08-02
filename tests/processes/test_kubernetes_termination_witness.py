"""Bounded exact-UID worker Pod termination witness."""

import asyncio

from kdive.processes.kubernetes_termination_witness import KubernetesTerminationWitness


def _pod(*, uid: str, phase: str, resource_version: str = "7") -> dict[str, object]:
    return {
        "metadata": {
            "uid": uid,
            "resourceVersion": resource_version,
            "finalizers": ["other.example/finalizer", "kdive.io/worker-termination-evidence"],
        },
        "status": {"phase": phase},
    }


def test_terminal_exact_uid_commits_before_finalizer_patch() -> None:
    events: list[tuple[object, ...]] = []
    pod = _pod(uid="uid-1", phase="Failed")

    async def terminate(holder: str, outcome: str) -> None:
        events.append(("terminate", holder, outcome))

    async def patch(namespace: str, name: str, operations: list[dict[str, object]]) -> None:
        events.append(("patch", namespace, name, operations))

    witness = KubernetesTerminationWitness(
        namespace="kdive",
        worker_name="kdive-worker",
        ordinal_ceiling=1,
        read_pod=lambda namespace, name: pod,
        patch_finalizers=patch,
        terminate=terminate,
    )

    assert asyncio.run(witness.sweep_once()) == 1
    assert events[0] == (
        "terminate",
        "kubernetes:kdive:kdive-worker-0:uid-1",
        "kubernetes_pod_failed",
    )
    assert events[1][0:3] == ("patch", "kdive", "kdive-worker-0")
    assert events[1][3] == [
        {"op": "test", "path": "/metadata/resourceVersion", "value": "7"},
        {
            "op": "test",
            "path": "/metadata/finalizers/1",
            "value": "kdive.io/worker-termination-evidence",
        },
        {"op": "remove", "path": "/metadata/finalizers/1"},
    ]


def test_live_absent_replaced_and_malformed_pods_fail_closed() -> None:
    patched: list[str] = []
    terminated: list[str] = []
    replies = {
        "kdive-worker-0": _pod(uid="live", phase="Running"),
        "kdive-worker-1": None,
        "kdive-worker-2": {"metadata": {"uid": "bad"}, "status": {"phase": "Failed"}},
    }

    async def terminate(holder: str, outcome: str) -> None:
        terminated.append(holder)

    async def patch(namespace: str, name: str, operations: list[dict[str, object]]) -> None:
        patched.append(name)

    witness = KubernetesTerminationWitness(
        namespace="kdive",
        worker_name="kdive-worker",
        ordinal_ceiling=3,
        read_pod=lambda namespace, name: replies[name],
        patch_finalizers=patch,
        terminate=terminate,
    )
    assert asyncio.run(witness.sweep_once()) == 0
    assert terminated == []
    assert patched == []


def test_database_failure_preserves_finalizer() -> None:
    patched = False

    async def terminate(holder: str, outcome: str) -> None:
        raise RuntimeError("database unavailable")

    async def patch(namespace: str, name: str, operations: list[dict[str, object]]) -> None:
        nonlocal patched
        patched = True

    witness = KubernetesTerminationWitness(
        namespace="kdive",
        worker_name="kdive-worker",
        ordinal_ceiling=1,
        read_pod=lambda namespace, name: _pod(uid="uid", phase="Succeeded"),
        patch_finalizers=patch,
        terminate=terminate,
    )
    try:
        asyncio.run(witness.sweep_once())
    except RuntimeError as exc:
        assert str(exc) == "database unavailable"
    else:
        raise AssertionError("termination persistence failure must escape")
    assert patched is False
