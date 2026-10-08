"""False-positive and disclosure guards for the real secret-presence carrier."""

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
def test_source_sequence_and_owned_cleanup(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    import asyncio
    from types import SimpleNamespace
    from typing import Any, cast

    from kdive.mcp.dev_harness import OidcIssuer
    from tests.integration.live_stack import secret_presence as proof
    from tests.integration.live_stack.scenario import CellRun, ScenarioStop

    events: list[str] = []
    observations: dict[str, object] = {}
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

    async def frame(*_args: object, **kwargs: Any) -> None:
        events.append("frame")
        try:
            await kwargs["body"](object(), "system-id", [])
        finally:
            events.append("frame-cleanup")

    run = cast(
        CellRun,
        SimpleNamespace(
            cell=SimpleNamespace(exposure="direct"),
            prove=lambda name, observation: observations.update({name: observation}),
        ),
    )
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
        assert set(observations) == {"effect", "source-cleanup"}
        assert reads == 4  # empty before setup/attach, present after attach and resource cleanup
    else:
        expected = {"dirty": ScenarioStop, "attach": RuntimeError}.get(fault, AssertionError)
        with pytest.raises(expected):
            asyncio.run(call)
        assert "source-cleanup" not in observations
    if fault == "dirty":
        assert events == []
    else:
        assert events[-1] == "frame-cleanup"
        assert "investigations.close" in events
        if fault != "attach":
            assert events.index("debug.end_session") < events.index("investigations.close")
            assert "debug.get_session" in events
