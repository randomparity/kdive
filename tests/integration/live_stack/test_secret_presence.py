"""False-positive and disclosure guards for the real secret-presence carrier."""

from pathlib import Path

import pytest

from kdive.mcp.responses import ToolResponse
from tests.integration.live_stack.secret_presence import verify_presence


def test_known_registered_presence() -> None:
    answer = ToolResponse.success("secrets", "ok", data={"secrets": ["<process-global>"]})
    assert verify_presence(answer, {"private-key-material"}) == {
        "labels": ["<process-global>"],
        "known_value_leaks": 0,
    }


@pytest.mark.parametrize("labels", [[], ["<scoped>"], ["<process-global>"] * 2, ["unknown"]])
def test_empty_or_unexpected_presence_fails(labels: list[str]) -> None:
    answer = ToolResponse.success("secrets", "ok", data={"secrets": [*labels]})
    with pytest.raises(AssertionError, match="expected exact process-global presence"):
        verify_presence(answer, {"private-key-material"})


def test_leaked_value_is_not_in_failure_message() -> None:
    secret = "private-key-material"  # pragma: allowlist secret - synthetic test marker
    answer = ToolResponse.success(
        "secrets", "ok", data={"secrets": ["<process-global>"], "extra": secret}
    )
    with pytest.raises(AssertionError, match="1 known secret value") as error:
        verify_presence(answer, {secret})
    assert secret not in str(error.value)


def test_secret_in_label_is_checked_before_label_diagnostics() -> None:
    secret = "private-key-material"  # pragma: allowlist secret - synthetic test marker
    answer = ToolResponse.success("secrets", "ok", data={"secrets": [secret]})
    with pytest.raises(AssertionError, match="1 known secret value") as error:
        verify_presence(answer, {secret})
    assert secret not in str(error.value)


@pytest.mark.parametrize("fault", ["none", "dirty", "attach", "empty", "retained-key"])
def test_source_sequence_and_owned_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str
) -> None:
    import asyncio
    from dataclasses import replace
    from typing import Any, cast

    from kdive.mcp.dev_harness import OidcIssuer
    from scripts.coverage_campaign.contract import build_contract
    from scripts.coverage_campaign.evidence import Outcome
    from scripts.coverage_campaign.results import qualify
    from tests.integration.live_stack import secret_presence as proof
    from tests.integration.live_stack.evidence import EvidenceWriter, RunIdentity, build_record
    from tests.integration.live_stack.scenario import CellRun, ScenarioStop
    from tests.integration.live_stack.tool_cells import bindings

    events: list[str] = []
    reads = 0

    class Caller:
        exposure = "direct"

        def __init__(self, *_args: object) -> None:
            pass

        def token(self, _grants: object) -> str:
            return "unit-token"

        async def call(self, *_args: object, **_kwargs: object) -> ToolResponse:
            nonlocal reads
            reads += 1
            labels = (
                []
                if (reads <= 2 and fault != "dirty") or fault == "empty"
                else ["<process-global>"]
            )
            return ToolResponse.success("secrets", "ok", data={"secrets": [*labels]})

    async def scalar(_op: object, tool: str, **_kwargs: object) -> ToolResponse:
        events.append(tool)
        if tool == "debug.start_session" and fault == "attach":
            raise RuntimeError("attach failed")
        status = {"debug.get_session": "detached", "investigations.close": "closed"}.get(tool, "ok")
        return ToolResponse.success("owned-id", status)

    async def boot(*_args: object) -> str:
        events.append("boot")
        return "run-id"

    async def key(*_args: object) -> str | None:
        if "frame-cleanup" not in events or fault == "retained-key":
            return "private-key-material"
        return None

    async def frame(fixture: CellRun, *_args: object, **kwargs: Any) -> None:
        events.append("frame")
        fixture.observed.update(accelerator="kvm", image_sha256="a" * 64)
        try:
            await kwargs["body"](object(), "system-id", [])
            fixture.prove("cleanup", {"domain_absent": True})
        finally:
            events.append("frame-cleanup")

    contract = build_contract()
    cell = next(
        c for c in contract.cells if c.operation == "secrets.list" and c.kind == "functional"
    )
    contract = replace(contract, cells=(cell,))
    run = CellRun(cell, EvidenceWriter(tmp_path))
    monkeypatch.setattr(proof, "HttpCaller", Caller)
    monkeypatch.setattr(proof, "lane_secrets", lambda: set())
    monkeypatch.setattr(proof, "lane_image", lambda: ("catalog-image", None))
    monkeypatch.setattr(proof, "scalar", scalar)
    monkeypatch.setattr(proof, "_booted_run", boot)
    monkeypatch.setattr(proof, "_source_key", key)
    monkeypatch.setattr(proof, "on_catalog_system", frame)
    call = proof.prove_secret_presence(run, "http://unused", cast(OidcIssuer, object()), "unused")
    if fault == "none":
        asyncio.run(call)
        assert set(run.assertions) == {"effect", "cleanup"}
        identity = RunIdentity(
            "b" * 40,
            contract.matrix_sha256,
            "ubuntu:26.04",
            "x86_64",
            True,
            {"server": "b" * 40, "worker": "b" * 40, "reconciler": "b" * 40},
        )
        inputs = bindings(
            identity.candidate_sha,
            host_os=identity.host_os,
            host_arch=identity.host_arch,
            matrix=identity.matrix_sha256,
            cells=(cell,),
        )
        record = build_record(
            cell,
            identity,
            outcome=Outcome.SUCCESS,
            context=run.context(identity),
            duration_s=1,
            assertions=run.assertions,
            artifacts=run.artifacts,
        )
        assert qualify(contract, inputs, [record]).passed
        assert reads == 4  # empty before setup/attach, present after attach and resource cleanup
    else:
        expected = {"dirty": ScenarioStop, "attach": RuntimeError}.get(fault, AssertionError)
        with pytest.raises(expected):
            asyncio.run(call)
        assert "cleanup" not in run.assertions
    if fault == "dirty":
        assert events == []
    else:
        assert events[-1] == "frame-cleanup"
        assert "investigations.close" in events
        if fault != "attach":
            assert events.index("debug.end_session") < events.index("investigations.close")
            assert "debug.get_session" in events


@pytest.mark.parametrize("value", ["line-one\nline-two\n", 'quoted"key\\value', "café\nkey"])
def test_json_escaped_value_is_detected_without_disclosure(value: str) -> None:
    answer = ToolResponse.success(
        "secrets", "ok", data={"secrets": ["<process-global>"], "extra": value}
    )
    with pytest.raises(AssertionError, match="1 known secret value") as error:
        verify_presence(answer, {value})
    assert value not in str(error.value)
