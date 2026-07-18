"""Provider runtime path helper tests."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.shared.runtime_paths import (
    WORKER_READABILITY_REMEDIATION,
    build_domain_name,
    console_log_path,
    domain_name_for,
    pcap_dir,
    pcap_path,
    read_console_log,
    read_pcap_bytes,
    system_id_from_domain_name,
)

_SYSTEM_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_domain_name_for_uses_kdive_prefix() -> None:
    assert domain_name_for(_SYSTEM_ID) == "kdive-11111111-1111-1111-1111-111111111111"


def test_system_id_from_domain_name_parses_convention() -> None:
    assert system_id_from_domain_name("kdive-11111111-1111-1111-1111-111111111111") == _SYSTEM_ID


def test_system_id_from_domain_name_round_trips_domain_name_for() -> None:
    assert system_id_from_domain_name(domain_name_for(_SYSTEM_ID)) == _SYSTEM_ID


def test_system_id_from_domain_name_excludes_build_vm_form() -> None:
    # kdive-build-<uuid> belongs to the ephemeral build-VM reaper, not the System sweep.
    assert system_id_from_domain_name("kdive-build-11111111-1111-1111-1111-111111111111") is None


def test_build_domain_name_is_reconciler_safe() -> None:
    # The transient customization-boot domain carries the build UUID under a distinct prefix so
    # the System name-fallback reaper never mistakes it for a System (ADR-0345).
    name = build_domain_name(_SYSTEM_ID)
    assert name == f"kdive-build-{_SYSTEM_ID}"
    assert system_id_from_domain_name(name) is None


@pytest.mark.parametrize(
    "name",
    [
        "kdive-foo",
        "vm-leak",
        "11111111-1111-1111-1111-111111111111",  # no kdive- prefix
        "kdive-",
        "kdive-11111111-1111-1111-1111-111111111111-extra",  # trailing junk
        "prefix-kdive-11111111-1111-1111-1111-111111111111",  # not anchored at start
        "",
    ],
)
def test_system_id_from_domain_name_rejects_non_convention(name: str) -> None:
    assert system_id_from_domain_name(name) is None


def test_console_log_path_uses_provider_console_directory() -> None:
    assert console_log_path(_SYSTEM_ID) == Path(
        "/var/lib/kdive/console/11111111-1111-1111-1111-111111111111.log"
    )


def test_read_console_log_returns_existing_bytes(tmp_path: Path) -> None:
    path = tmp_path / "console.log"
    path.write_bytes(b"booted\n")

    assert read_console_log(path) == b"booted\n"


def test_read_console_log_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_console_log(tmp_path / "missing.log") == b""


def test_read_console_log_permission_failure_is_configuration_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A root-owned (qemu:///system virtlogd) console log is a host config problem, not a
    retryable infrastructure failure (ADR-0223). The error names the operator fix."""
    path = tmp_path / "console.log"

    def fail_read_bytes(self: Path) -> bytes:
        assert self == path
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    with pytest.raises(CategorizedError) as caught:
        read_console_log(path)

    assert caught.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert caught.value.details == {
        "operation": "read_console_log",
        "path": str(path),
        "error": "PermissionError",
        "remediation": WORKER_READABILITY_REMEDIATION,
    }


def test_read_console_log_other_oserror_is_infrastructure_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "console.log"

    def fail_read_bytes(self: Path) -> bytes:
        assert self == path
        raise OSError("short read")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    with pytest.raises(CategorizedError) as caught:
        read_console_log(path)

    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
    assert str(caught.value) == "failed to read console log"
    assert caught.value.details == {
        "operation": "read_console_log",
        "path": str(path),
        "error": "OSError",
    }


_JOB_ID = UUID("22222222-2222-2222-2222-222222222222")


def test_pcap_path_and_dir() -> None:
    assert pcap_dir(_SYSTEM_ID) == Path("/var/lib/kdive/pcap") / str(_SYSTEM_ID)
    assert pcap_path(_SYSTEM_ID, _JOB_ID) == pcap_dir(_SYSTEM_ID) / f"{_JOB_ID}.pcap"


def test_read_pcap_bytes_missing_is_empty(tmp_path: Path) -> None:
    assert read_pcap_bytes(tmp_path / "absent.pcap") == b""


def test_read_pcap_bytes_permission_error_is_configuration_error(monkeypatch, tmp_path) -> None:
    target = tmp_path / "denied.pcap"
    target.write_bytes(b"\xd4\xc3\xb2\xa1")

    def _deny(_self: Path) -> bytes:
        raise PermissionError("root-owned")

    monkeypatch.setattr(Path, "read_bytes", _deny)
    with pytest.raises(CategorizedError) as excinfo:
        read_pcap_bytes(target)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["remediation"] == WORKER_READABILITY_REMEDIATION
