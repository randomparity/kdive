"""Authority recovery fault orchestration with its barrier, database and HTTP boundaries faked."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from kdive.mcp.responses import ToolResponse
from scripts.coverage_campaign.contract import Cell
from scripts.coverage_campaign.evidence import Outcome
from tests.integration.live_stack import authority_faults
from tests.integration.live_stack.authority_faults import Barrier
from tests.integration.live_stack.authority_lane import AuthoritySystem
from tests.integration.live_stack.evidence import EvidenceWriter
from tests.integration.live_stack.scenario import CellRun, ScenarioStop
from tests.integration.live_stack.spine import SpinePhaseError
from tests.live_vm.installed_local_authority_support import (
    ActivationJob,
    NativeAuthorityConfig,
    ResourceLedger,
)
from tests.live_vm.installed_remote_authority_support import RemoteAuthorityProofConfig

_SYSTEM = "11111111-2222-4333-8444-555555555555"
_ACTIVATION = "22222222-2222-4333-8444-555555555555"
_PREFIX = "kdive-2151-aaaaaaaaaaaa-0123abcd"


class _FakeBarrier:
    """The ADR-0787 barrier protocol, held in memory; ``arrive`` plays the authority."""

    def __init__(self, *, busy: bool = False) -> None:
        self.state = "armed" if busy else "idle"
        self.log: list[str] = []

    def arrive(self) -> None:
        assert self.state == "armed"
        self.state = "reached"

    def __call__(self, document: dict[str, str]) -> dict[str, str]:
        action = document["action"]
        self.log.append(action)
        if action == "arm":
            if self.state != "idle":
                return {"state": "busy"}
            self.state = "armed"
            return {"state": "armed"}
        if action == "release":
            if self.state == "idle":
                return {"state": "idle"}
            self.state = "idle"
            return {"state": "released"}
        if action == "abort":
            if self.state != "reached":
                return {"error": "invalid-request"}
            self.state = "idle"
            return {"state": "aborted"}
        return {"state": self.state}


def _barrier(fake: _FakeBarrier) -> Barrier:
    return Barrier(fake, _SYSTEM)


def _system(remote: bool = False) -> AuthoritySystem:
    config: NativeAuthorityConfig | RemoteAuthorityProofConfig = cast(
        Any, type("Config", (), {"ownership_prefix": _PREFIX})()
    )
    if remote:
        config = RemoteAuthorityProofConfig.model_construct(ownership_prefix=_PREFIX)
    return AuthoritySystem(
        cast(Any, object()), "cov-t", "alloc", _SYSTEM, config, ResourceLedger(_PREFIX)
    )


async def _held_body(fake: _FakeBarrier, failure: BaseException | None) -> None:
    async with authority_faults.held(_barrier(fake), "run", "cleanup", "before-finalize"):
        fake.arrive()
        if failure is not None:
            raise failure


@pytest.mark.parametrize("failure", [None, AssertionError("body"), KeyboardInterrupt()])
def test_held_releases_and_requires_idle(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException | None
) -> None:
    monkeypatch.setattr(authority_faults, "HOLD_TIMEOUT_S", 1.0)
    fake = _FakeBarrier()
    if failure is None:
        asyncio.run(_held_body(fake, failure))
    else:
        with pytest.raises(type(failure)):
            asyncio.run(_held_body(fake, failure))
    assert fake.log[0] == "arm"
    assert "release" in fake.log
    assert fake.state == "idle"


def test_held_releases_an_arm_that_was_never_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authority_faults, "HOLD_TIMEOUT_S", 1.0)
    fake = _FakeBarrier()

    async def body() -> None:
        async with authority_faults.held(_barrier(fake), "run", "teardown", "after-provider"):
            raise AssertionError("refused before the teardown arrived")

    with pytest.raises(AssertionError, match="refused"):
        asyncio.run(body())
    assert fake.state == "idle"


def test_busy_arm_fails_loud_without_restarting() -> None:
    fake = _FakeBarrier(busy=True)
    with pytest.raises(AssertionError, match="restart the authority once"):
        asyncio.run(_held_body(fake, None))
    assert fake.log == ["arm"]


def test_abort_refused_unless_reached() -> None:
    with pytest.raises(AssertionError, match="refused the abort"):
        asyncio.run(_barrier(_FakeBarrier()).abort())


def _rows_returning(result: list[tuple[object, ...]]) -> Callable[..., Any]:
    async def rows(*_args: object) -> list[tuple[object, ...]]:
        return result

    return rows


@pytest.mark.parametrize("row", [("queued", 1, False), ("running", 2, True), ("running", 1, False)])
def test_hold_budget_rejects_a_lapsed_or_retried_attempt(
    monkeypatch: pytest.MonkeyPatch, row: tuple[object, ...]
) -> None:
    monkeypatch.setattr(authority_faults, "_rows", _rows_returning([row]))
    with pytest.raises(AssertionError, match="hold budget exceeded"):
        asyncio.run(authority_faults.require_hold_budget("db", "job", 1))
    monkeypatch.setattr(authority_faults, "_rows", _rows_returning([("running", 1, True)]))
    asyncio.run(authority_faults.require_hold_budget("db", "job", 1))


def _async(result: object = None) -> Callable[..., Any]:
    async def coroutine(*_args: object, **_kwargs: object) -> object:
        return result

    return coroutine


def _activation() -> ActivationJob:
    return ActivationJob("inv", "run", "activate-job")


def test_remote_activation_blocker_names_the_envelope_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refused(*_args: object, **_kwargs: object) -> ActivationJob:
        raise SpinePhaseError(
            "boot",
            "error envelope",
            error_category="configuration_error",
            data={"reason": "remote_external_boot_initrd_required"},
        )

    monkeypatch.setattr(authority_faults, "start_external_boot_activation", refused)
    with pytest.raises(ScenarioStop) as stop:
        asyncio.run(authority_faults._activate(_system(remote=True)))
    assert stop.value.outcome is Outcome.BLOCKED
    assert str(stop.value) == (
        "remote external-boot activation failed at boot (error envelope; "
        "configuration_error: remote_external_boot_initrd_required)"
    )


@pytest.mark.parametrize("remote", [False, True])
def test_only_a_remote_activation_uploads_an_initrd(
    monkeypatch: pytest.MonkeyPatch, remote: bool
) -> None:
    started: list[dict[str, object]] = []

    async def start(*_args: object, **kwargs: object) -> ActivationJob:
        started.append(kwargs)
        return _activation()

    monkeypatch.setattr(authority_faults, "start_external_boot_activation", start)
    monkeypatch.setattr(authority_faults, "drain_job", _async())
    asyncio.run(authority_faults._activate(_system(remote=remote)))
    assert [(kwargs["with_initrd"], kwargs["require_kdump"]) for kwargs in started] == [
        (remote, not remote)
    ]


def _fake_arm_a(monkeypatch: pytest.MonkeyPatch, fake: _FakeBarrier) -> list[str]:
    heads = iter([("mutation-started", "teardown"), ("terminal", "teardown")])
    allocations: list[str] = []

    async def scalar(_op: object, name: str, **_args: object) -> ToolResponse:
        if name == "systems.teardown":
            fake.arrive()
            return ToolResponse(object_id="teardown-job", status="queued")
        if name == "allocations.wait":
            allocations.append("active")
            return ToolResponse(object_id="alloc", status="active")
        return ToolResponse(object_id=_SYSTEM, status="ready")

    patches: dict[str, Any] = {
        "require_evidence_read_access": _async(),
        "_activate": _async(_activation()),
        "release_external_boot": _async(ToolResponse(object_id="release-job", status="queued")),
        "drain_job": _async(),
        "assert_root_release_completion": _async(),
        "capacity_in_use": _async(1),
        "barrier_for": lambda _system: _barrier(fake),
        "scalar": scalar,
        "job_claim": _async(("running", 1)),
        "journal_head": lambda *_a: _async(next(heads))(),
        "_domain_present": lambda _system: False,
        "require_hold_budget": _async(),
        "await_system_state": _async(),
    }
    for name, value in patches.items():
        monkeypatch.setattr(authority_faults, name, value)
    return allocations


def _force_release(refusal: ToolResponse) -> Callable[..., Any]:
    answers = iter([refusal, ToolResponse(object_id="alloc", status="released")])

    async def call(_tool: str, _args: dict[str, object]) -> ToolResponse:
        return next(answers)

    return call


def _refusal(reason: str) -> ToolResponse:
    return ToolResponse(
        object_id="alloc",
        status="error",
        error_category="conflict",
        data={"reason": reason, "system_id": _SYSTEM},
    )


def test_force_release_arm_requires_refusal_then_release(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBarrier()
    _fake_arm_a(monkeypatch, fake)
    observed = asyncio.run(
        authority_faults.held_teardown_release(
            _system(), "db", _force_release(_refusal(authority_faults.TEARDOWN_REQUIRED))
        )
    )
    assert observed["refusal"] == authority_faults.TEARDOWN_REQUIRED
    assert observed["release_after_quiescence"] == "released"
    assert fake.state == "idle"


def test_force_release_arm_rejects_another_refusal_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBarrier()
    _fake_arm_a(monkeypatch, fake)
    with pytest.raises(AssertionError, match="external_boot_restricted"):
        asyncio.run(
            authority_faults.held_teardown_release(
                _system(), "db", _force_release(_refusal("external_boot_restricted"))
            )
        )
    assert fake.state == "idle"


def _row(
    row_id: str, status: str = "quarantined", provider: str = "local-libvirt"
) -> dict[str, object]:
    return {
        "id": row_id,
        "object_identity": f"sha256:{row_id}",
        "system_id": _SYSTEM,
        "activation_id": _ACTIVATION,
        "object_kind": "recovery-record",
        "provider_kind": provider,
        "status": status,
        "operation_identity": "cleanup-op",
        "mutation_journal_digest": "sha256:journal",
        "disposition_job_id": "resolve-job" if status == "deleted" else None,
        "resolved_at": "now" if status == "deleted" else None,
    }


def _quarantine_sequence(
    monkeypatch: pytest.MonkeyPatch, snapshots: list[dict[str, dict[str, object]]]
) -> None:
    remaining = iter(snapshots)
    last: list[dict[str, dict[str, object]]] = []

    async def rows(_db: str) -> dict[str, dict[str, object]]:
        last[:] = [next(remaining, last[0] if last else {})]
        return last[0]

    monkeypatch.setattr(authority_faults, "quarantine_rows", rows)


def test_published_row_requires_exactly_one_new_row(monkeypatch: pytest.MonkeyPatch) -> None:
    other = {"o": _row("o")}
    _quarantine_sequence(monkeypatch, [other, {**other, "a": _row("a"), "b": _row("b")}])
    with pytest.raises(AssertionError, match="published 2 quarantine rows"):
        asyncio.run(authority_faults._published_row("db", other))


def test_published_row_detects_an_unrelated_row_change(monkeypatch: pytest.MonkeyPatch) -> None:
    other = {"o": _row("o")}
    _quarantine_sequence(monkeypatch, [{"o": _row("o", "deleted"), "a": _row("a")}])
    with pytest.raises(AssertionError, match="unrelated quarantine row"):
        asyncio.run(authority_faults._published_row("db", other))


def _fake_arm_b(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeBarrier, drain: Callable[..., Any]
) -> list[dict[str, object]]:
    resolved: list[dict[str, object]] = []
    published = {"a": _row("a")}
    _quarantine_sequence(monkeypatch, [{}, {}, published, published, {"a": _row("a", "deleted")}])
    receipts = iter(
        [
            authority_faults.Receipt(True, "f" * 64, "cleanup-op", "sha256:journal"),
            authority_faults.Receipt(False),
        ]
    )

    async def release(_op: object, _run: str) -> ToolResponse:
        fake.arrive()
        return ToolResponse(object_id="release-job", status="queued")

    claims = iter([("running", 1), ("succeeded", 2)])
    patches: dict[str, Any] = {
        "require_evidence_read_access": _async(),
        "_activate": _async(_activation()),
        "activation_of": _async(_ACTIVATION),
        "barrier_for": lambda _system: _barrier(fake),
        "release_external_boot": release,
        "job_claim": lambda *_a: _async(next(claims))(),
        "journal_head": _async(("terminal", "cleanup")),
        "receipt_state": lambda *_a: next(receipts),
        "require_hold_budget": _async(),
        "drain_job": drain,
        "attempt_failure_contexts": _async([{"authority_reason": "provider-conflict"}]),
        "orphan_request": _async([(["a"], True)]),
    }
    for name, value in patches.items():
        monkeypatch.setattr(authority_faults, name, value)
    return resolved


def _resolver(resolved: list[dict[str, object]]) -> Callable[..., Any]:
    async def call(tool: str, args: dict[str, object]) -> ToolResponse:
        assert tool == "ops.resolve_recovery_orphan"
        resolved.append(args)
        return ToolResponse(object_id="resolve-job", status="queued")

    return call


def test_orphan_arm_publishes_and_resolves_exactly_its_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBarrier()
    resolved = _fake_arm_b(monkeypatch, fake, _async())
    observed = asyncio.run(
        authority_faults.quarantined_orphan(_system(), "db", _resolver(resolved))
    )
    assert resolved == [
        {"system_id": _SYSTEM, "object_identities": ["sha256:a"], "disposition": "delete"}
    ]
    assert observed["resolved"] == "deleted"
    assert observed["present_receipt_delete"] == authority_faults.LOCAL_PRESENT_ORPHAN_BLOCKED
    assert fake.log.count("abort") == 1
    assert fake.state == "idle"


def test_orphan_failure_after_publication_resolves_its_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def drain(_op: object, phase: str, _job: str) -> None:
        if phase == "release":
            raise AssertionError("release did not finish")

    fake = _FakeBarrier()
    resolved = _fake_arm_b(monkeypatch, fake, drain)
    with pytest.raises(AssertionError, match="release did not finish"):
        asyncio.run(authority_faults.quarantined_orphan(_system(), "db", _resolver(resolved)))
    assert [args["object_identities"] for args in resolved] == [["sha256:a"]]


def test_orphan_failure_before_the_row_is_seen_resolves_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBarrier()
    resolved = _fake_arm_b(monkeypatch, fake, _async())
    unseen = {"a": _row("a")}
    _quarantine_sequence(monkeypatch, [{}, {}, unseen])

    async def interrupted(*_args: object) -> object:
        raise KeyboardInterrupt

    monkeypatch.setattr(authority_faults, "_published_row", interrupted)
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(authority_faults.quarantined_orphan(_system(), "db", _resolver(resolved)))
    assert [args["object_identities"] for args in resolved] == [["sha256:a"]]


def test_remote_orphan_arm_observes_the_remote_receipt_and_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBarrier()
    resolved = _fake_arm_b(monkeypatch, fake, _async())
    seen: list[tuple[str, str]] = []
    receipts = iter(
        [
            authority_faults.Receipt(True, "f" * 64, "cleanup-op", "sha256:journal"),
            authority_faults.Receipt(False),
        ]
    )

    def remote_receipt(_config: object, ids: tuple[str, str]) -> authority_faults.Receipt:
        seen.append(ids)
        return next(receipts)

    monkeypatch.setattr(authority_faults, "remote_receipt_state", remote_receipt)
    monkeypatch.setattr(
        authority_faults, "receipt_state", lambda *_a: pytest.fail("local observer used")
    )
    _quarantine_sequence(
        monkeypatch,
        [
            {},
            {},
            {"a": _row("a", provider="remote-libvirt")},
            {"a": _row("a", provider="remote-libvirt")},
            {"a": _row("a", "deleted", provider="remote-libvirt")},
        ],
    )
    observed = asyncio.run(
        authority_faults.quarantined_orphan(_system(remote=True), "db", _resolver(resolved))
    )
    assert seen == [(_SYSTEM, _ACTIVATION)] * 2
    assert observed["resolved"] == "deleted"


@pytest.mark.parametrize("provider", ["local-libvirt", "remote-libvirt"])
def test_blocked_arm_passes_the_lane_gates_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: str
) -> None:
    gates: list[str] = []
    lane = authority_faults.authority_lane
    monkeypatch.setattr(lane, "select_lane", lambda: gates.append("select") or "lane")
    monkeypatch.setattr(lane, "select_remote_lane", lambda: gates.append("select") or "lane")
    monkeypatch.setattr(lane, "require_lane", lambda *_a: gates.append("gate"))
    monkeypatch.setattr(lane, "require_remote_lane", lambda *_a: gates.append("gate"))
    cell = Cell("c", "tool/c", 3185, "c", "c", ("effect",), provider=provider, guest_arch="x86_64")
    run = CellRun(cell, EvidenceWriter(tmp_path))
    with pytest.raises(ScenarioStop) as stop:
        authority_faults.blocked_after_gates(run, "u", authority_faults.REMOTE_ORPHAN_BLOCKED)
    assert gates == ["select", "gate"]
    assert str(stop.value) == authority_faults.REMOTE_ORPHAN_BLOCKED


_COMPOSITE = "sha256:" + "c" * 64


def _fake_conflict_arm(
    monkeypatch: pytest.MonkeyPatch,
    failures: dict[str, str],
    heads: list[tuple[object, ...]],
    states: tuple[str, ...] = ("recovery_conflict", "recovered"),
) -> list[str]:
    """Fake every boundary of the conflict arm; return the ordered calls it made."""
    calls: list[str] = []
    remaining = iter(heads)
    activation_states = iter(states)

    async def drain(_op: object, phase: str, _job: str, **_kwargs: object) -> ToolResponse:
        calls.append(f"drain:{phase}")
        category = failures.get(phase)
        if category is None:
            return ToolResponse(object_id=_job, status="succeeded")
        raise SpinePhaseError(phase, "job failed", error_category=category)

    async def scalar(_op: object, name: str, **args: object) -> ToolResponse:
        calls.append(f"{name}:{args.get('observed_identity')}")
        return ToolResponse(object_id="resolve-job", status="queued")

    patches: dict[str, Any] = {
        "require_evidence_read_access": _async(),
        "_activate": _async(_activation()),
        "activation_of": _async(_ACTIVATION),
        "perturb_definition": lambda _system: calls.append("perturb"),
        "release_external_boot": _async(ToolResponse(object_id="release-job", status="queued")),
        "drain_job": drain,
        "conflict_head": lambda *_a: _async(next(remaining))(),
        "_activation_state": lambda *_a: _async(next(activation_states))(),
        "scalar": scalar,
    }
    for name, value in patches.items():
        monkeypatch.setattr(authority_faults, name, value)
    return calls


def test_conflict_arm_records_then_resolves_by_restoring_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _fake_conflict_arm(
        monkeypatch,
        {"release": "conflict"},
        [
            ("terminal", "recover", "conflict", "conflict", _COMPOSITE),
            ("terminal", "resolve-conflict", "source", "source", _COMPOSITE),
        ],
    )
    observed = asyncio.run(authority_faults.recorded_conflict(_system(), "db"))
    assert calls == [
        "perturb",
        "drain:release",
        f"systems.resolve_external_boot_conflict:{_COMPOSITE}",
        "drain:resolve-conflict",
    ]
    assert observed["recorded"] == "recovery_conflict"
    assert observed["activation"] == "recovered"


def test_conflict_arm_refuses_a_release_that_succeeded(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_conflict_arm(monkeypatch, {}, [])
    with pytest.raises(AssertionError, match="succeeded over a third identity"):
        asyncio.run(authority_faults.recorded_conflict(_system(), "db"))


def test_conflict_arm_refuses_another_failure_category(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_conflict_arm(monkeypatch, {"release": "infrastructure_failure"}, [])
    with pytest.raises(AssertionError, match="infrastructure_failure"):
        asyncio.run(authority_faults.recorded_conflict(_system(), "db"))


def test_conflict_arm_requires_the_authority_conflict_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_conflict_arm(
        monkeypatch,
        {"release": "conflict"},
        [("terminal", "recover", "source", "source", _COMPOSITE)],
    )
    with pytest.raises(AssertionError, match="source"):
        asyncio.run(authority_faults.recorded_conflict(_system(), "db"))


def test_conflict_arm_refuses_a_resolution_that_stays_conflicted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_conflict_arm(
        monkeypatch,
        {"release": "conflict", "resolve-conflict": "conflict"},
        [("terminal", "recover", "conflict", "conflict", _COMPOSITE)],
    )
    with pytest.raises(SpinePhaseError, match="resolve-conflict"):
        asyncio.run(authority_faults.recorded_conflict(_system(), "db"))


def test_conflict_arm_requires_the_resolution_head_to_be_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_conflict_arm(
        monkeypatch,
        {"release": "conflict"},
        [
            ("terminal", "recover", "conflict", "conflict", _COMPOSITE),
            ("terminal", "resolve-conflict", "conflict", "conflict", _COMPOSITE),
        ],
    )
    with pytest.raises(AssertionError, match="resolve-conflict"):
        asyncio.run(authority_faults.recorded_conflict(_system(), "db"))


def test_conflict_arm_requires_the_activation_recovered(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_conflict_arm(
        monkeypatch,
        {"release": "conflict"},
        [
            ("terminal", "recover", "conflict", "conflict", _COMPOSITE),
            ("terminal", "resolve-conflict", "source", "source", _COMPOSITE),
        ],
        states=("recovery_conflict", "recovery_conflict"),
    )
    with pytest.raises(AssertionError):
        asyncio.run(authority_faults.recorded_conflict(_system(), "db"))
