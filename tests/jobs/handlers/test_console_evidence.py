"""Tests for the shared redacted console read and bounded tail helper (ADR-0235, ADR-0306)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.jobs.handlers.console import console_evidence
from kdive.jobs.handlers.console.console_evidence import (
    _CONSOLE_TAIL_MAX_CHARS,
    read_redacted_console,
    redacted_console_tail,
)
from kdive.security.secrets.secret_registry import SecretRegistry


def _point_at(monkeypatch: pytest.MonkeyPatch, log: Path) -> None:
    monkeypatch.setattr(console_evidence, "console_log_path", lambda _sid: log)


def test_read_redacted_console_reads_whole_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_id = uuid4()
    log = tmp_path / f"{system_id}.log"
    log.write_bytes(b"line one\nthis boot panic\n")
    _point_at(monkeypatch, log)

    redacted = asyncio.run(read_redacted_console(system_id, SecretRegistry()))

    assert redacted == b"line one\nthis boot panic\n"


def test_read_redacted_console_absent_log_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_id = uuid4()
    _point_at(monkeypatch, tmp_path / f"{system_id}.log")  # never written

    assert asyncio.run(read_redacted_console(system_id, SecretRegistry())) is None


def test_tail_returns_last_chars_not_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system_id = uuid4()
    log = tmp_path / f"{system_id}.log"
    # A console longer than the cap: the tail must be the RECENT end (sshd status), not the head.
    body = ("noise line\n" * 400) + "systemd[1]: Started OpenSSH server daemon.\n"
    log.write_bytes(body.encode())
    _point_at(monkeypatch, log)

    tail = asyncio.run(redacted_console_tail(system_id, SecretRegistry()))

    assert tail is not None
    assert len(tail) == _CONSOLE_TAIL_MAX_CHARS
    assert tail.endswith("systemd[1]: Started OpenSSH server daemon.\n")
    assert tail == body[-_CONSOLE_TAIL_MAX_CHARS:]


def test_tail_shorter_than_cap_is_whole_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_id = uuid4()
    log = tmp_path / f"{system_id}.log"
    log.write_bytes(b"sshd did not start\n")
    _point_at(monkeypatch, log)

    assert asyncio.run(redacted_console_tail(system_id, SecretRegistry())) == "sshd did not start\n"


def test_tail_is_none_for_empty_or_absent_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_id = uuid4()
    _point_at(monkeypatch, tmp_path / f"{system_id}.log")  # never written

    assert asyncio.run(redacted_console_tail(system_id, SecretRegistry())) is None


def test_tail_is_best_effort_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A read error (e.g. non-root PermissionError raised as CategorizedError) must degrade to None,
    # never propagate — a missing console cannot mask the primary transport failure it decorates.
    async def _boom(_sid: object, _reg: object) -> bytes:
        raise RuntimeError("console read exploded")

    monkeypatch.setattr(console_evidence, "read_redacted_console", _boom)

    assert asyncio.run(redacted_console_tail(uuid4(), SecretRegistry())) is None


def test_read_locates_log_by_system_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The read must resolve the log from THIS system_id; a wrong/None id finds a different,
    # absent path and returns None — so the returned bytes prove the id reached console_log_path.
    system_id = uuid4()
    monkeypatch.setattr(console_evidence, "console_log_path", lambda sid: tmp_path / f"{sid}.log")
    (tmp_path / f"{system_id}.log").write_bytes(b"real console\n")

    assert asyncio.run(read_redacted_console(system_id, SecretRegistry())) == b"real console\n"


def test_read_decodes_invalid_utf8_with_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A console with an invalid UTF-8 byte must decode via the "replace" error handler (U+FFFD),
    # never "strict" (which raises) — the raw log is arbitrary bytes off a worker-local file.
    system_id = uuid4()
    log = tmp_path / f"{system_id}.log"
    log.write_bytes(b"good\xffbad\n")
    _point_at(monkeypatch, log)

    redacted = asyncio.run(read_redacted_console(system_id, SecretRegistry()))

    assert redacted is not None
    assert "�" in redacted.decode("utf-8")


def test_tail_locates_log_by_system_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system_id = uuid4()
    monkeypatch.setattr(console_evidence, "console_log_path", lambda sid: tmp_path / f"{sid}.log")
    (tmp_path / f"{system_id}.log").write_bytes(b"tail body\n")

    assert asyncio.run(redacted_console_tail(system_id, SecretRegistry())) == "tail body\n"


def test_tail_is_redacted_by_the_same_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    system_id = uuid4()
    log = tmp_path / f"{system_id}.log"
    log.write_bytes(b"authorized_keys: AAAAsecretkeymaterial\n")
    _point_at(monkeypatch, log)
    registry = SecretRegistry()
    registry.register("AAAAsecretkeymaterial", scope=None)

    tail = asyncio.run(redacted_console_tail(system_id, registry))

    assert tail is not None
    assert "AAAAsecretkeymaterial" not in tail
