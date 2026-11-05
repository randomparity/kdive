"""The authority's stranded observation: quiescence, then its own facts (ADR-0816, #3574)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityStrandedObservationRequestV1,
    AuthorityStrandedObservationResponseV1,
    JournalRecordV1,
    record_digest,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityServiceError,
    ExternalBootAuthorityService,
    _Lane,
)
from kdive.providers.external_boot_authority.stranded import (
    StrandedAuthorityFacts,
    StrandedObservationRepository,
    StrandedTarget,
    stranded_composite_state,
)
from tests.providers.external_boot_authority.service_support import (
    _Adapter,
    _Repository,
    _takeover,
)

_PEER = AuthenticatedPeer(uuid4())
_ACTIVATION = uuid4()
_RECOVERY_REF = "local-recovery/ref"


def _target(system_id: UUID | None = None, *, objects: tuple[str, ...] = ()) -> StrandedTarget:
    return StrandedTarget(
        system_id=system_id or uuid4(),
        run_id=uuid4(),
        domain_name="kdive-stranded",
        object_refs=objects,
        journal_sequence=None,
        journal_digest=None,
    )


class _Observations:
    def __init__(self, target: StrandedTarget | None, *, applied: bool = True) -> None:
        self.target = target
        self.applied = applied
        self.recorded: list[tuple[UUID, StrandedTarget, str]] = []
        self.during_record: list[bool] = []
        self.lane_probe = lambda: False

    async def read_target(self, activation_id: UUID) -> StrandedTarget | None:
        return self.target

    async def record_absence(
        self, activation_id: UUID, target: StrandedTarget, composite_state: str
    ) -> bool:
        self.during_record.append(self.lane_probe())
        self.recorded.append((activation_id, target, composite_state))
        return self.applied


class _Observer(_Adapter):
    def __init__(self, *, domain_absent: bool = True, store_absent: bool = True) -> None:
        super().__init__()
        self.facts = StrandedAuthorityFacts(domain_absent, store_absent, _RECOVERY_REF)
        self.seen: list[tuple[UUID, StrandedTarget]] = []
        self.failure: Exception | None = None
        self.lane_probe = lambda: False
        self.during_read: list[bool] = []

    async def observe_stranded_absence(
        self, activation_id: UUID, target: StrandedTarget
    ) -> StrandedAuthorityFacts:
        self.during_read.append(self.lane_probe())
        self.seen.append((activation_id, target))
        if self.failure is not None:
            raise self.failure
        return self.facts


def _service(
    tmp_path: Path, observations: _Observations | None, adapter: _Adapter
) -> ExternalBootAuthorityService:
    return ExternalBootAuthorityService(
        repository=_Repository(_PEER, _takeover()),
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
        stranded_observations=cast(StrandedObservationRepository | None, observations),
    )


def _observe(
    service: ExternalBootAuthorityService, *, peer: AuthenticatedPeer | None = _PEER
) -> AuthorityStrandedObservationResponseV1:
    return asyncio.run(
        service.observe_stranded_activation(
            peer, AuthorityStrandedObservationRequestV1(activation_id=_ACTIVATION)
        )
    )


def _refusal(service: ExternalBootAuthorityService) -> AuthorityServiceError:
    with pytest.raises(AuthorityServiceError) as raised:
        _observe(service)
    return raised.value


def test_records_absence_when_quiescent_with_no_journal(tmp_path: Path) -> None:
    target = _target(objects=("held/kernel",))
    observations = _Observations(target)

    response = _observe(_service(tmp_path, observations, _Observer()))

    assert response.disposition == "recorded"
    assert observations.recorded == [
        (_ACTIVATION, target, stranded_composite_state(_ACTIVATION, target))
    ]


def test_the_lane_lock_spans_the_provider_read_and_the_record(tmp_path: Path) -> None:
    target = _target()
    observations = _Observations(target)
    adapter = _Observer()
    service = _service(tmp_path, observations, adapter)

    def lane_locked() -> bool:
        lane = service._lanes.get(target.system_id)
        return lane is not None and lane.lock.locked()

    adapter.lane_probe = lane_locked
    observations.lane_probe = lane_locked

    _observe(service)

    assert adapter.during_read == [True]
    assert observations.during_record == [True]


def _write_record(tmp_path: Path, system_id: UUID, record: JournalRecordV1) -> None:
    journal = FileAuthorityJournal(tmp_path, f"{system_id}.journal")
    journal.append(record)
    journal.close()


def _admitted_record(system_id: UUID) -> JournalRecordV1:
    """A non-terminal head, the shape an interrupted attempt leaves."""
    takeover = _takeover()
    values = takeover.model_dump(mode="json", by_alias=True)
    values |= {
        "system_id": str(system_id),
        "sequence": 1,
        "previous_digest": "sha256:" + "0" * 64,
        "phase": "watermark-installed",
        "attempt_id": str(uuid4()),
    }
    return JournalRecordV1.model_validate(values)


def test_a_non_terminal_head_equal_to_the_tail_records(tmp_path: Path) -> None:
    system_id = uuid4()
    record = _admitted_record(system_id)
    _write_record(tmp_path, system_id, record)
    target = replace(
        _target(system_id), journal_sequence=record.sequence, journal_digest=record_digest(record)
    )
    observations = _Observations(target)

    response = _observe(_service(tmp_path, observations, _Observer()))

    assert response.disposition == "recorded"


def test_a_tail_the_head_does_not_match_refuses_before_any_read(tmp_path: Path) -> None:
    system_id = uuid4()
    record = _admitted_record(system_id)
    _write_record(tmp_path, system_id, record)
    target = replace(_target(system_id), journal_sequence=9, journal_digest="sha256:" + "9" * 64)
    observations = _Observations(target)
    adapter = _Observer()

    refusal = _refusal(_service(tmp_path, observations, adapter))

    assert (refusal.category, refusal.reason) == ("journal_conflict", "stranded_head_mismatch")
    assert adapter.seen == []
    assert observations.recorded == []


def test_a_tail_with_no_head_refuses(tmp_path: Path) -> None:
    system_id = uuid4()
    _write_record(tmp_path, system_id, _admitted_record(system_id))
    adapter = _Observer()

    refusal = _refusal(_service(tmp_path, _Observations(_target(system_id)), adapter))

    assert refusal.reason == "stranded_head_mismatch"


def test_a_head_with_no_tail_refuses(tmp_path: Path) -> None:
    target = replace(_target(), journal_sequence=1, journal_digest="sha256:" + "1" * 64)

    refusal = _refusal(_service(tmp_path, _Observations(target), _Observer()))

    assert refusal.reason == "stranded_head_mismatch"


def test_an_active_operation_refuses(tmp_path: Path) -> None:
    target = _target()
    observations = _Observations(target)
    adapter = _Observer()
    service = _service(tmp_path, observations, adapter)
    lane = service._lane(target.system_id)
    lane.active = cast(Any, object())

    refusal = _refusal(service)

    assert refusal.category == "superseded"
    assert adapter.seen == []


def test_a_failed_flag_on_a_held_lane_refuses(tmp_path: Path) -> None:
    target = _target()
    adapter = _Observer()
    service = _service(tmp_path, _Observations(target), adapter)
    lane: _Lane = service._lane(target.system_id)
    lane.failed = True

    refusal = _refusal(service)

    assert (refusal.category, refusal.reason) == ("journal_conflict", "stranded_lane_failed")
    assert adapter.seen == []


def test_a_domain_on_the_authority_records_nothing(tmp_path: Path) -> None:
    observations = _Observations(_target())

    response = _observe(_service(tmp_path, observations, _Observer(domain_absent=False)))

    assert response.disposition == "domain_present"
    assert observations.recorded == []


def test_a_held_store_names_where_to_look_and_records_nothing(tmp_path: Path) -> None:
    observations = _Observations(_target(objects=("held/kernel", "held/modules")))

    response = _observe(_service(tmp_path, observations, _Observer(store_absent=False)))

    assert response.disposition == "objects_present"
    assert response.objects == (_RECOVERY_REF, "held/kernel", "held/modules")
    assert observations.recorded == []


def test_a_recorder_refusal_is_superseded(tmp_path: Path) -> None:
    observations = _Observations(_target(), applied=False)

    assert _refusal(_service(tmp_path, observations, _Observer())).category == "superseded"


def test_a_missing_target_is_superseded_without_reading(tmp_path: Path) -> None:
    adapter = _Observer()

    assert _refusal(_service(tmp_path, _Observations(None), adapter)).category == "superseded"
    assert adapter.seen == []


def test_a_target_that_vanishes_under_the_lock_is_superseded(tmp_path: Path) -> None:
    target = _target()
    adapter = _Observer()

    class Vanishing(_Observations):
        reads = 0

        async def read_target(self, activation_id: UUID) -> StrandedTarget | None:
            self.reads += 1
            return target if self.reads == 1 else None

    refusal = _refusal(_service(tmp_path, Vanishing(target), adapter))

    assert refusal.category == "superseded"
    assert adapter.seen == []


def test_no_repository_refuses(tmp_path: Path) -> None:
    assert _refusal(_service(tmp_path, None, _Observer())).category == "provider_conflict"


def test_an_adapter_without_the_read_refuses(tmp_path: Path) -> None:
    refusal = _refusal(_service(tmp_path, _Observations(_target()), _Adapter()))

    assert refusal.category == "provider_conflict"


def test_no_peer_refuses(tmp_path: Path) -> None:
    observations = _Observations(_target())
    with pytest.raises(AuthorityServiceError) as raised:
        _observe(_service(tmp_path, observations, _Observer()), peer=None)

    assert raised.value.category == "unauthenticated"


def test_a_provider_fault_records_nothing(tmp_path: Path) -> None:
    observations = _Observations(_target())
    adapter = _Observer()
    adapter.failure = RuntimeError("libvirt went away")

    refusal = _refusal(_service(tmp_path, observations, adapter))

    assert refusal.category == "provider_conflict"
    assert observations.recorded == []


def test_a_closing_service_refuses(tmp_path: Path) -> None:
    service = _service(tmp_path, _Observations(_target()), _Observer())
    asyncio.run(service.close())

    assert _refusal(service).category == "superseded"


def test_the_composite_state_binds_the_objects() -> None:
    system_id = uuid4()
    run_id = uuid4()
    first = StrandedTarget(system_id, run_id, "d", ("a",), None, None)
    second = replace(first, object_refs=("b",))

    state = stranded_composite_state(_ACTIVATION, first)

    assert state.startswith("sha256:")
    assert len(state) == 71
    assert state != stranded_composite_state(_ACTIVATION, second)
