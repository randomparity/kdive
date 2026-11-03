"""Reboot readiness contracts against the real SSH helper and a deterministic clock."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tests.integration.live_stack import scenario
from tests.integration.live_stack.image_smoke import Endpoint


@dataclass
class ProbeClock:
    outcomes: list[tuple[int, str] | None] = field(default_factory=list)
    now: float = 0.0
    timeouts: list[float] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def run(self, argv: list[str], *, timeout: float, **_kwargs: object):
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if outcome is None:
            self.now += timeout
            raise subprocess.TimeoutExpired(argv, timeout)
        code, stdout = outcome
        return subprocess.CompletedProcess(argv, code, stdout, "probe error")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ProbeClock:
    clock = ProbeClock()
    monkeypatch.setattr(scenario.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(scenario.time, "sleep", clock.sleep)
    monkeypatch.setattr(subprocess, "run", clock.run)
    return clock


def test_timeout_then_new_boot_succeeds(clock: ProbeClock, tmp_path: Path) -> None:
    clock.outcomes = [None, (0, "boot_id=new\nuid=0\n")]
    probe = scenario.probe_new_boot(Endpoint("127.0.0.1", 22), tmp_path / "key", "old")
    assert probe == {"boot_id": "new", "uid": "0"}
    assert clock.timeouts == [120.0, 120.0]
    assert clock.now == 125.0


def test_transport_and_old_boot_retry(clock: ProbeClock, tmp_path: Path) -> None:
    clock.outcomes = [(255, ""), (0, "boot_id=old\n"), (0, "boot_id=new\n")]
    assert scenario.probe_new_boot(Endpoint("127.0.0.1", 22), tmp_path / "key", "old") == {
        "boot_id": "new"
    }
    assert clock.sleeps == [5.0, 5.0]


def test_timeouts_exhaust_budget_with_short_final_attempt(
    clock: ProbeClock, tmp_path: Path
) -> None:
    clock.outcomes = [None, None, None]
    with pytest.raises(AssertionError, match="readiness exceeded 300 s: SSH probe timed out"):
        scenario.probe_new_boot(Endpoint("127.0.0.1", 22), tmp_path / "key", "old")
    assert clock.timeouts == [120.0, 120.0, 50.0]
    assert clock.now == 300.0
    assert not clock.outcomes


@pytest.mark.parametrize("outcome", [(255, ""), (0, "boot_id=old\n")])
def test_retry_sleep_is_capped_at_deadline(
    clock: ProbeClock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: tuple[int, str]
) -> None:
    monkeypatch.setattr(scenario, "_REBOOT_DEADLINE_S", 3.0)
    clock.outcomes = [outcome]
    with pytest.raises(AssertionError, match="readiness exceeded 3 s"):
        scenario.probe_new_boot(Endpoint("127.0.0.1", 22), tmp_path / "key", "old")
    assert clock.timeouts == [3.0]
    assert clock.sleeps == [3.0]
    assert clock.now == 3.0


@pytest.mark.parametrize(
    "code,output,error",
    [(1, "", "ssh probe exit 1"), (0, "uid=0", "no boot_id"), (0, "boot_id=", "no boot_id")],
)
def test_command_failure_and_missing_identity_do_not_retry(
    clock: ProbeClock, tmp_path: Path, code: int, output: str, error: str
) -> None:
    clock.outcomes = [(code, output)]
    with pytest.raises(AssertionError, match=error):
        scenario.probe_new_boot(Endpoint("127.0.0.1", 22), tmp_path / "key", "old")
    assert len(clock.timeouts) == 1
    assert not clock.sleeps
