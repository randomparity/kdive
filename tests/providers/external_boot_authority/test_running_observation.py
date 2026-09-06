"""Authority-owned running-kernel reads preserve bytes without widening journal values."""

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityRunningObservationV1,
)
from kdive.providers.external_boot_authority.transport import _dispatch
from kdive.providers.ports.external_boot import KernelIdentity, RunningKernelObservation
from tests.providers.external_boot_authority.service_support import _mutation, _service


def _observation(cmdline: bytes = b"console=ttyS0\xff") -> RunningKernelObservation:
    return RunningKernelObservation(
        identity=KernelIdentity(architecture="x86_64", release="6.12.0", gnu_build_id="abcd1234"),
        cmdline=cmdline,
        expected_cmdline=b"console=ttyS0",
    )


def test_running_observation_preserves_non_utf8_bytes_in_closed_wire() -> None:
    value = AuthorityRunningObservationV1.from_observation(_observation())
    decoded = AuthorityRunningObservationV1.model_validate_json(value.model_dump_json())
    assert decoded.to_observation() == _observation()


def test_running_observation_bounds_command_line_bytes() -> None:
    AuthorityRunningObservationV1.from_observation(_observation(b"x" * 2048))
    with pytest.raises(ValidationError):
        AuthorityRunningObservationV1.from_observation(_observation(b"x" * 2049))


@pytest.mark.parametrize("invalid", ["0", "AA", "gg", "00\n", " 00"])
def test_running_observation_rejects_noncanonical_hex(invalid: str) -> None:
    value = AuthorityRunningObservationV1.from_observation(_observation()).model_dump()
    with pytest.raises(ValidationError):
        AuthorityRunningObservationV1.model_validate(value | {"cmdline_hex": invalid})


@pytest.mark.anyio
async def test_running_read_uses_current_authority_without_journal_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)

    async def read(request: AuthorityMutationRequestV1) -> RunningKernelObservation:
        adapter.calls.append("observe-running")
        return _observation()

    monkeypatch.setattr(adapter, "observe_running", read, raising=False)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    records = list(repository.records)
    result = await service.observe_running(peer, _mutation(takeover))
    assert result.to_observation() == _observation()
    assert adapter.calls == ["observe-running"]
    assert repository.records == records


@pytest.mark.anyio
async def test_running_read_rejects_supersession_after_provider_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.providers.external_boot_authority.service import AuthorityServiceError

    service, repository, adapter, peer, takeover = _service(tmp_path)

    async def read(request: AuthorityMutationRequestV1) -> RunningKernelObservation:
        repository.current = False
        return _observation()

    monkeypatch.setattr(adapter, "observe_running", read, raising=False)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    with pytest.raises(AuthorityServiceError, match="superseded"):
        await service.observe_running(peer, _mutation(takeover))


@pytest.mark.anyio
async def test_typed_sender_dispatches_running_read_with_borrowed_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, adapter, peer, takeover = _service(tmp_path)

    async def read(request: AuthorityMutationRequestV1) -> RunningKernelObservation:
        return _observation()

    async def authenticate(credential: SecretStr):
        assert credential.get_secret_value() == "test-active-incarnation"
        return peer

    class Backend:
        async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
            assert deadline == 123.0
            return await _dispatch(envelope, authenticate, service)

    monkeypatch.setattr(adapter, "observe_running", read, raising=False)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    sender = AuthorityRequestSender(Backend, lambda: SecretStr("test-active-incarnation"))
    result = await sender.observe_running(_mutation(takeover), deadline=123.0)
    assert result.to_observation() == _observation()
