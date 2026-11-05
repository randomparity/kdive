"""The worker half of the stranded observation (ADR-0816, #3574)."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import SecretStr

from kdive.db.external_boot_activations import CasStatus
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.external_boot import stranded
from kdive.providers.assembly.composition import build_remote_runtime
from kdive.providers.external_boot_authority.protocol import (
    AuthorityStrandedObservationResponseV1,
)
from kdive.security.secrets.secret_registry import SecretRegistry

_SYSTEM = uuid4()
_ACTIVATION = uuid4()
_DOMAIN = "kdive-stranded"


class _Conn:
    @asynccontextmanager
    async def transaction(self) -> Any:
        yield


class _Presence:
    def __init__(self, *, defined: bool = True) -> None:
        self.defined = defined
        self.asked: list[str] = []

    def is_defined(self, domain_name: str) -> bool:
        self.asked.append(domain_name)
        return self.defined


class _Sender:
    def __init__(self, response: AuthorityStrandedObservationResponseV1 | None = None) -> None:
        self.response = response or AuthorityStrandedObservationResponseV1(disposition="recorded")
        self.requests: list[Any] = []

    async def observe_stranded_activation(
        self, request: Any, *, deadline: float
    ) -> AuthorityStrandedObservationResponseV1:
        self.requests.append(request)
        return self.response


class _Activations:
    def __init__(self, status: CasStatus = CasStatus.APPLIED) -> None:
        self.status = status
        self.recorded: list[dict[str, Any]] = []

    async def record_stranded_worker_ownership(self, conn: Any, **kwargs: Any) -> CasStatus:
        self.recorded.append(kwargs)
        return self.status


def _job() -> Any:
    return SimpleNamespace(
        id=uuid4(),
        attempt=1,
        kind=JobKind.OBSERVE_STRANDED_ACTIVATION,
        payload={
            "schema": "observe-stranded-activation-v1",
            "system_id": str(_SYSTEM),
            "activation_id": str(_ACTIVATION),
        },
    )


def _resolver(presence: Any, capability: Any) -> Any:
    runtime = SimpleNamespace(domain_presence=presence, authority=capability)

    class Resolver:
        async def binding_for_system(self, conn: Any, system_id: Any) -> Any:
            return SimpleNamespace(runtime=runtime)

    return Resolver()


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    presence: Any = None,
    sender: _Sender | None = None,
    activations: _Activations | None = None,
    domain_name: str | None = _DOMAIN,
    capability: Any = "default",
) -> str:
    async def get(conn: Any, system_id: Any) -> Any:
        return SimpleNamespace(domain_name=domain_name)

    monkeypatch.setattr(stranded, "SYSTEMS", SimpleNamespace(get=get))
    monkeypatch.setattr(stranded, "_ACTIVATIONS", activations or _Activations())
    route = (
        SimpleNamespace(sender=sender or _Sender(), missing_route_reason=None)
        if capability == "default"
        else capability
    )
    return asyncio.run(
        stranded.observe_stranded_activation_handler(
            cast(Any, _Conn()),
            _job(),
            resolver=_resolver(presence or _Presence(), route),
            incarnation_credential=SecretStr("credential"),
        )
    )


def test_records_the_worker_then_asks_the_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = _Sender()
    activations = _Activations()

    result = _run(monkeypatch, sender=sender, activations=activations)

    assert json.loads(result)["disposition"] == "recorded"
    assert activations.recorded[0]["domain_name"] == _DOMAIN
    assert activations.recorded[0]["activation_id"] == _ACTIVATION
    assert sender.requests[0].activation_id == _ACTIVATION


def _refusal(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> CategorizedError:
    with pytest.raises(CategorizedError) as raised:
        _run(monkeypatch, **kwargs)
    assert raised.value.terminal is True
    return raised.value


def test_a_runtime_without_presence_is_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get(conn: Any, system_id: Any) -> Any:
        raise AssertionError

    monkeypatch.setattr(stranded, "SYSTEMS", SimpleNamespace(get=get))
    with pytest.raises(CategorizedError) as raised:
        asyncio.run(
            stranded.observe_stranded_activation_handler(
                cast(Any, _Conn()),
                _job(),
                resolver=_resolver(None, None),
                incarnation_credential=SecretStr("credential"),
            )
        )

    assert raised.value.details["reason"] == "stranded_observation_unsupported"
    assert raised.value.category is ErrorCategory.CONFIGURATION_ERROR


def test_the_real_remote_runtime_refuses_by_name() -> None:
    runtime = build_remote_runtime(secret_registry=SecretRegistry())

    class Resolver:
        async def binding_for_system(self, conn: Any, system_id: Any) -> Any:
            return SimpleNamespace(runtime=runtime)

    with pytest.raises(CategorizedError) as raised:
        asyncio.run(
            stranded.observe_stranded_activation_handler(
                cast(Any, _Conn()),
                _job(),
                resolver=cast(Any, Resolver()),
                incarnation_credential=SecretStr("credential"),
            )
        )

    assert raised.value.details["reason"] == "stranded_observation_unsupported"
    assert raised.value.terminal is True


def test_no_resolver_is_unsupported() -> None:
    with pytest.raises(CategorizedError) as raised:
        asyncio.run(
            stranded.observe_stranded_activation_handler(
                cast(Any, _Conn()),
                _job(),
                resolver=None,
                incarnation_credential=SecretStr("credential"),
            )
        )

    assert raised.value.details["reason"] == "stranded_observation_unsupported"


def test_no_route_names_the_missing_route(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = _refusal(monkeypatch, capability=None)

    assert refusal.details["reason"] == "authority_route_missing"


def test_an_unconfigured_sender_names_its_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    capability = SimpleNamespace(sender=None, missing_route_reason="authority_route_unconfigured")

    refusal = _refusal(monkeypatch, capability=capability)

    assert refusal.details["reason"] == "authority_route_unconfigured"


@pytest.mark.parametrize("arrange", [{"domain_name": None}, {"presence": _Presence(defined=False)}])
def test_an_absent_worker_domain_records_nothing_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, arrange: dict[str, Any]
) -> None:
    sender = _Sender()
    activations = _Activations()

    refusal = _refusal(monkeypatch, sender=sender, activations=activations, **arrange)

    assert refusal.details["reason"] == "worker_domain_absent"
    assert activations.recorded == []
    assert sender.requests == []


@pytest.mark.parametrize("status", [CasStatus.SUPERSEDED, CasStatus.NOT_FOUND])
def test_a_refused_worker_record_never_reaches_the_authority(
    monkeypatch: pytest.MonkeyPatch, status: CasStatus
) -> None:
    sender = _Sender()

    refusal = _refusal(monkeypatch, sender=sender, activations=_Activations(status))

    assert refusal.details["reason"] == "worker_evidence_superseded"
    assert sender.requests == []


def test_a_domain_on_the_authority_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = _Sender(AuthorityStrandedObservationResponseV1(disposition="domain_present"))

    assert _refusal(monkeypatch, sender=sender).details["reason"] == "authority_domain_present"


def test_a_held_store_refusal_names_the_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = _Sender(
        AuthorityStrandedObservationResponseV1(
            disposition="objects_present", objects=("recovery/ref", "held/kernel")
        )
    )

    refusal = _refusal(monkeypatch, sender=sender)

    assert refusal.details["reason"] == "authority_objects_present"
    assert "recovery/ref, held/kernel" in str(refusal)
