"""Unit tests for the runs.get liveness derivation (ADR-0373, #1237)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from psycopg import AsyncConnection

from kdive.domain.operations.jobs import JobKind
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.runs import liveness as liveness_mod
from kdive.services.runs.liveness import (
    _STORM_TAIL_CHARS,
    STATE_DEGRADED,
    STATE_HEALTHY,
    STATE_UNKNOWN,
    Liveness,
    _parse_ssh_verdict,
    derive_liveness,
    derive_state,
    detect_console_storm,
)

# --- console-storm heuristic ---------------------------------------------------------------


def test_healthy_console_tail_is_not_a_storm() -> None:
    tail = "systemd: Reached target Multi-User System.\nkdive-ready\nlogin:"
    assert detect_console_storm(tail) is False


def test_empty_or_missing_tail_is_not_a_storm() -> None:
    assert detect_console_storm(None) is False
    assert detect_console_storm("") is False


def test_printk_suppression_marker_flags_a_storm() -> None:
    # The kernel self-reports it dropped a message flood — an unambiguous storm hallmark.
    tail = "net_ratelimit: 214 callbacks suppressed"
    assert detect_console_storm(tail) is True


def test_single_benign_oom_line_is_not_a_storm() -> None:
    # One app OOM stays below the repetition threshold and must not flag a storm.
    tail = "Out of memory: Killed process 900 (stress) total-vm:1kB"
    assert detect_console_storm(tail) is False


def test_repeated_oom_retry_storm_flags_degraded() -> None:
    # A livelocked VM_FAULT_OOM retry loop fills the window with the same line.
    tail = "\n".join("VM_FAULT_OOM retrying allocation order=0" for _ in range(6))
    assert detect_console_storm(tail) is True


def test_matches_are_case_insensitive() -> None:
    tail = "\n".join(["SOFT LOCKUP - CPU#0 stuck", "Hung Task detected stall", "OUT OF MEMORY"])
    assert detect_console_storm(tail) is True


def test_exactly_min_hits_flags_a_storm() -> None:
    # Boundary: exactly _STORM_MIN_HITS (3) signatures must trip the storm (>= not >).
    tail = "\n".join("soft lockup" for _ in range(3))
    assert detect_console_storm(tail) is True


def test_one_below_min_hits_is_not_a_storm() -> None:
    # Boundary: one fewer than the threshold must stay below it.
    tail = "\n".join("soft lockup" for _ in range(2))
    assert detect_console_storm(tail) is False


# --- state derivation ----------------------------------------------------------------------


def test_state_degraded_when_console_storms() -> None:
    state = derive_state(console_storm=True, ssh_reachable=True, console_read=True)
    assert state == STATE_DEGRADED


def test_state_degraded_when_ssh_unreachable_after_ready_boot() -> None:
    state = derive_state(console_storm=False, ssh_reachable=False, console_read=True)
    assert state == STATE_DEGRADED


def test_state_healthy_when_no_signal_is_bad() -> None:
    state = derive_state(console_storm=False, ssh_reachable=True, console_read=True)
    assert state == STATE_HEALTHY


def test_state_healthy_when_console_clean_and_ssh_unprobed() -> None:
    # A readable clean console is a positive signal even with no SSH probe yet.
    state = derive_state(console_storm=False, ssh_reachable=None, console_read=True)
    assert state == STATE_HEALTHY


def test_state_unknown_when_no_console_and_no_probe() -> None:
    state = derive_state(console_storm=False, ssh_reachable=None, console_read=False)
    assert state == STATE_UNKNOWN


# --- ssh verdict parsing -------------------------------------------------------------------


def test_parse_reachable_verdict() -> None:
    ref = json.dumps({"reachable": True, "checked_at": "2026-07-16T00:00:00+00:00"})
    assert _parse_ssh_verdict(ref) == (True, "2026-07-16T00:00:00+00:00")


def test_parse_unreachable_verdict() -> None:
    ref = json.dumps({"reachable": False, "checked_at": "2026-07-16T00:00:00+00:00"})
    assert _parse_ssh_verdict(ref) == (False, "2026-07-16T00:00:00+00:00")


@pytest.mark.parametrize(
    "ref",
    [None, "not json", json.dumps({"checked_at": "x"}), json.dumps(["reachable"]), json.dumps(42)],
)
def test_parse_absent_or_malformed_verdict_yields_none(ref: str | None) -> None:
    # An absent, unparsable, or reachable-less verdict is never a fabricated False.
    assert _parse_ssh_verdict(ref) == (None, None)


def test_parse_verdict_without_checked_at() -> None:
    ref = json.dumps({"reachable": True})
    assert _parse_ssh_verdict(ref) == (True, None)


# --- data shape ----------------------------------------------------------------------------


def _patch_signals(
    monkeypatch,
    *,
    console_tail: str | None,
    ssh_result_ref: str | None,
    expected_conn: object,
    expected_system_id,
    expected_registry: SecretRegistry,
) -> None:
    # The fakes assert every argument the production calls forward: a mutant that drops or nulls
    # conn / system_id / kind / registry / max_chars reaches an assertion and is killed.
    async def _fake_tail(system_id, registry, *, max_chars: int = 0) -> str | None:
        assert system_id == expected_system_id
        assert registry is expected_registry
        assert max_chars == _STORM_TAIL_CHARS
        return console_tail

    async def _fake_job(conn, kind, system_id):
        assert conn is expected_conn
        assert kind is JobKind.CHECK_SSH_REACHABLE
        assert system_id == expected_system_id
        return None if ssh_result_ref is None else SimpleNamespace(result_ref=ssh_result_ref)

    monkeypatch.setattr(liveness_mod, "redacted_console_tail", _fake_tail)
    monkeypatch.setattr(liveness_mod.queue, "latest_succeeded_job_for_system", _fake_job)


def _run_derive(monkeypatch, *, console_tail: str | None, ssh_result_ref: str | None) -> Liveness:
    conn = object()
    system_id = uuid4()
    registry = SecretRegistry()
    _patch_signals(
        monkeypatch,
        console_tail=console_tail,
        ssh_result_ref=ssh_result_ref,
        expected_conn=conn,
        expected_system_id=system_id,
        expected_registry=registry,
    )
    return asyncio.run(derive_liveness(cast(AsyncConnection, conn), system_id, registry))


def test_derive_liveness_degraded_on_console_storm(monkeypatch: pytest.MonkeyPatch) -> None:
    # A livelocked guest (printk storm) reads state=degraded even with no SSH probe (#1237).
    result = _run_derive(
        monkeypatch, console_tail="foo\n214 callbacks suppressed\nbar", ssh_result_ref=None
    )
    assert result.state == STATE_DEGRADED
    assert result.console_storm is True
    assert result.ssh_reachable is None
    assert result.checked_at is None


def test_derive_liveness_degraded_on_unreachable_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_derive(
        monkeypatch,
        console_tail="clean boot\nkdive-ready",
        ssh_result_ref=json.dumps({"reachable": False, "checked_at": "2026-07-16T00:00:00+00:00"}),
    )
    assert result.state == STATE_DEGRADED
    assert result.console_storm is False
    assert result.ssh_reachable is False
    assert result.checked_at == "2026-07-16T00:00:00+00:00"


def test_derive_liveness_healthy_when_clean_and_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_derive(
        monkeypatch,
        console_tail="clean boot\nkdive-ready\nlogin:",
        ssh_result_ref=json.dumps({"reachable": True, "checked_at": "2026-07-16T00:00:00+00:00"}),
    )
    assert result.state == STATE_HEALTHY
    assert result.ssh_reachable is True


def test_derive_liveness_healthy_when_console_empty_and_unprobed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty (readable) console is a positive signal: console_read is True, so an unprobed
    # guest stays healthy not unknown. Pins console_read=(console_tail is not None), not a literal.
    result = _run_derive(monkeypatch, console_tail="", ssh_result_ref=None)
    assert result.state == STATE_HEALTHY
    assert result.console_storm is False
    assert result.ssh_reachable is None


def test_derive_liveness_unknown_when_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run_derive(monkeypatch, console_tail=None, ssh_result_ref=None)
    assert result.state == STATE_UNKNOWN
    assert result.console_storm is False
    assert result.ssh_reachable is None


def test_liveness_as_data_shape() -> None:
    liveness = Liveness(
        state=STATE_DEGRADED,
        console_storm=True,
        ssh_reachable=False,
        checked_at="2026-07-16T00:00:00+00:00",
    )
    assert liveness.as_data() == {
        "state": "degraded",
        "console_storm": True,
        "ssh_reachable": False,
        "checked_at": "2026-07-16T00:00:00+00:00",
    }
