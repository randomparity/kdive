"""Bounded exact-UID worker Pod termination witness."""

import asyncio
import hashlib
import ssl
import urllib.error
from email.message import Message
from typing import cast

import pytest
from psycopg import AsyncConnection, sql

import kdive.processes.lifecycle.kubernetes.kubernetes_credential_broker as credential_broker
import kdive.processes.lifecycle.lifecycle_witness as lifecycle_witness
from kdive.processes.lifecycle.kubernetes.kubernetes_credential_broker import (
    KubernetesCredentialBroker,
)
from kdive.processes.lifecycle.kubernetes.kubernetes_termination_witness import (
    KubernetesTerminationWitness,
    run_witness,
)
from kdive.worker_lifecycle.authority_store import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    KubernetesAuthorityBinding,
    register_worker_incarnation,
    terminate_worker_incarnation,
)
from kdive.worker_lifecycle.contracts import TerminationOutcome
from tests.reconciler.conftest import connect


def test_lifecycle_witness_process_exposes_its_runner() -> None:
    assert lifecycle_witness.run_lifecycle_witness_body.__module__ == (
        "kdive.processes.lifecycle.lifecycle_witness"
    )


def test_lifecycle_witness_propagates_broker_bind_failure_and_cleans_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaned: set[str] = set()

    def fail_bind(host: str, port: int) -> None:
        del host, port
        raise OSError("address already in use")

    monkeypatch.setattr(credential_broker, "_listening_socket", fail_bind)

    async def sibling(name: str) -> None:
        try:
            await asyncio.Future()
        finally:
            cleaned.add(name)

    async def run() -> None:
        with pytest.raises(OSError, match="address already in use"):
            await lifecycle_witness._supervise_authority_tasks(
                asyncio.Event(),
                (
                    "broker",
                    credential_broker.serve_broker(
                        cast(KubernetesCredentialBroker, object()),
                        asyncio.Event(),
                        host="127.0.0.1",
                        port=7443,
                        ssl_context=cast(ssl.SSLContext, object()),
                    ),
                ),
                ("pre-registration", sibling("pre-registration")),
                ("termination-witness", sibling("termination-witness")),
            )

    asyncio.run(run())
    assert cleaned == {"pre-registration", "termination-witness"}


def test_lifecycle_witness_rejects_unexpected_clean_child_exit() -> None:
    cleaned = asyncio.Event()

    async def completed() -> None:
        return

    async def sibling() -> None:
        try:
            await asyncio.Future()
        finally:
            cleaned.set()

    async def run() -> None:
        with pytest.raises(RuntimeError, match="broker exited unexpectedly"):
            await lifecycle_witness._supervise_authority_tasks(
                asyncio.Event(), ("broker", completed()), ("sibling", sibling())
            )

    asyncio.run(run())
    assert cleaned.is_set()


def test_lifecycle_witness_normal_stop_cancels_and_awaits_children() -> None:
    stop = asyncio.Event()
    started = asyncio.Event()
    cleaned: set[str] = set()

    async def child(name: str) -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaned.add(name)

    async def run() -> None:
        task = asyncio.create_task(
            lifecycle_witness._supervise_authority_tasks(
                stop, ("broker", child("broker")), ("witness", child("witness"))
            )
        )
        await started.wait()
        stop.set()
        await task

    asyncio.run(run())
    assert cleaned == {"broker", "witness"}


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

    async def terminate(
        holder: str, binding: KubernetesAuthorityBinding, outcome: TerminationOutcome
    ) -> bool:
        events.append(("terminate", holder, binding, outcome))
        return True

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
        {"namespace": "kdive", "name": "kdive-worker-0", "uid": "uid-1"},
        "failed",
    )
    assert events[1][0:3] == ("patch", "kdive", "kdive-worker-0")
    assert events[1][3] == [
        {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "7"},
        {
            "op": "test",
            "path": "/metadata/finalizers/1",
            "value": "kdive.io/worker-termination-evidence",
        },
        {"op": "remove", "path": "/metadata/finalizers/1"},
    ]


def test_unregistered_terminal_pod_retains_finalizer_and_cannot_publish_evidence() -> None:
    patched = False
    terminated: list[str] = []

    async def terminate(
        holder: str, binding: KubernetesAuthorityBinding, outcome: TerminationOutcome
    ) -> bool:
        terminated.append(holder)
        return False

    async def patch(namespace: str, name: str, operations: list[dict[str, object]]) -> None:
        nonlocal patched
        patched = True

    witness = KubernetesTerminationWitness(
        namespace="kdive",
        worker_name="kdive-worker",
        ordinal_ceiling=1,
        read_pod=lambda namespace, name: _pod(uid="unregistered", phase="Failed"),
        patch_finalizers=patch,
        terminate=terminate,
    )
    assert asyncio.run(witness.sweep_once()) == 0
    assert terminated == ["kubernetes:kdive:kdive-worker-0:unregistered"]
    assert patched is False


def test_live_absent_replaced_and_malformed_pods_fail_closed() -> None:
    patched: list[str] = []
    terminated: list[str] = []
    replies = {
        "kdive-worker-0": _pod(uid="live", phase="Running"),
        "kdive-worker-1": None,
        "kdive-worker-2": {"metadata": {"uid": "bad"}, "status": {"phase": "Failed"}},
    }

    async def terminate(
        holder: str, binding: KubernetesAuthorityBinding, outcome: TerminationOutcome
    ) -> bool:
        terminated.append(holder)
        return False

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

    async def terminate(
        holder: str, binding: KubernetesAuthorityBinding, outcome: TerminationOutcome
    ) -> bool:
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


def test_patch_conflict_rereads_fresh_resource_version_before_removal() -> None:
    reads = 0
    patches: list[list[dict[str, object]]] = []

    def read(namespace: str, name: str) -> dict[str, object]:
        nonlocal reads
        reads += 1
        return _pod(uid="uid", phase="Failed", resource_version=str(reads))

    async def terminate(
        holder: str, binding: KubernetesAuthorityBinding, outcome: TerminationOutcome
    ) -> bool:
        return True

    async def patch(namespace: str, name: str, operations: list[dict[str, object]]) -> None:
        patches.append(operations)
        if len(patches) == 1:
            raise urllib.error.HTTPError("https://kubernetes", 409, "conflict", Message(), None)

    witness = KubernetesTerminationWitness(
        namespace="kdive",
        worker_name="kdive-worker",
        ordinal_ceiling=1,
        read_pod=read,
        patch_finalizers=patch,
        terminate=terminate,
    )
    assert asyncio.run(witness.sweep_once()) == 1
    assert reads == 2
    assert patches[0][1]["value"] == "1"
    assert patches[1][1]["value"] == "2"


def test_witness_loop_survives_authority_failure_and_retries() -> None:
    stop = asyncio.Event()
    reads = 0

    def read(namespace: str, name: str):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise OSError("API unavailable")
        stop.set()
        return None

    async def terminate(
        holder: str, binding: KubernetesAuthorityBinding, outcome: TerminationOutcome
    ) -> bool:
        return False

    witness = KubernetesTerminationWitness(
        namespace="kdive",
        worker_name="kdive-worker",
        ordinal_ceiling=1,
        read_pod=read,
        patch_finalizers=lambda namespace, name, operations: asyncio.sleep(0),
        terminate=terminate,
    )
    asyncio.run(run_witness(witness, stop, interval=0))
    assert reads == 2


async def _witness_connection(url: str) -> AsyncConnection:
    connection = await connect(url)
    await connection.execute(
        sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier("kdive_lifecycle_witness"))
    )
    return connection


def test_committed_termination_retries_finalizer_patch_on_second_sweep(
    migrated_url: str,
) -> None:
    async def run() -> None:
        holder = "kubernetes:kdive:kdive-worker-0:uid-retry"
        binding = KubernetesAuthorityBinding(
            namespace="kdive", name="kdive-worker-0", uid="uid-retry"
        )
        witness_connection = await _witness_connection(migrated_url)
        admin = await connect(migrated_url)
        patch_attempts = 0
        try:
            await register_worker_incarnation(
                witness_connection,
                holder,
                "kubernetes",
                binding,
                hashlib.sha256(b"retry-credential").digest(),
                CURRENT_WORKER_FENCE_PROTOCOL,
            )

            async def terminate(
                incarnation: str,
                exact_binding: KubernetesAuthorityBinding,
                outcome: TerminationOutcome,
            ) -> bool:
                return await terminate_worker_incarnation(
                    witness_connection, incarnation, "kubernetes", exact_binding, "failed"
                )

            async def patch(namespace: str, name: str, operations: list[dict[str, object]]) -> None:
                nonlocal patch_attempts
                patch_attempts += 1
                if patch_attempts == 1:
                    raise RuntimeError("Kubernetes API unavailable after database commit")

            witness = KubernetesTerminationWitness(
                namespace="kdive",
                worker_name="kdive-worker",
                ordinal_ceiling=1,
                read_pod=lambda namespace, name: _pod(uid="uid-retry", phase="Failed"),
                patch_finalizers=patch,
                terminate=terminate,
            )
            with pytest.raises(RuntimeError, match="API unavailable"):
                await witness.sweep_once()
            first_evidence = await (
                await admin.execute(
                    "SELECT state, authority_binding, outcome, terminated_at "
                    "FROM worker_incarnations WHERE incarnation = %s",
                    (holder,),
                )
            ).fetchone()

            assert await witness.sweep_once() == 1
            second_evidence = await (
                await admin.execute(
                    "SELECT state, authority_binding, outcome, terminated_at "
                    "FROM worker_incarnations WHERE incarnation = %s",
                    (holder,),
                )
            ).fetchone()
            assert patch_attempts == 2
            assert second_evidence == first_evidence
        finally:
            await admin.close()
            await witness_connection.close()

    asyncio.run(run())


def test_termination_confirmation_requires_exact_binding_and_outcome(migrated_url: str) -> None:
    async def run() -> None:
        holder = "kubernetes:kdive:kdive-worker-0:uid-exact"
        binding = KubernetesAuthorityBinding(
            namespace="kdive", name="kdive-worker-0", uid="uid-exact"
        )
        witness = await _witness_connection(migrated_url)
        admin = await connect(migrated_url)
        try:
            await register_worker_incarnation(
                witness,
                holder,
                "kubernetes",
                binding,
                hashlib.sha256(b"exact-credential").digest(),
                CURRENT_WORKER_FENCE_PROTOCOL,
            )
            assert await terminate_worker_incarnation(
                witness, holder, "kubernetes", binding, "failed"
            )
            evidence = await (
                await admin.execute(
                    "SELECT state, authority_binding, outcome, terminated_at "
                    "FROM worker_incarnations WHERE incarnation = %s",
                    (holder,),
                )
            ).fetchone()
            assert await terminate_worker_incarnation(
                witness, holder, "kubernetes", binding, "failed"
            )
            assert not await terminate_worker_incarnation(
                witness,
                holder,
                "kubernetes",
                KubernetesAuthorityBinding(
                    namespace=binding["namespace"], name=binding["name"], uid="other-uid"
                ),
                "failed",
            )
            assert not await terminate_worker_incarnation(
                witness, holder, "kubernetes", binding, "killed"
            )
            unchanged = await (
                await admin.execute(
                    "SELECT state, authority_binding, outcome, terminated_at "
                    "FROM worker_incarnations WHERE incarnation = %s",
                    (holder,),
                )
            ).fetchone()
            assert unchanged == evidence
        finally:
            await admin.close()
            await witness.close()

    asyncio.run(run())
