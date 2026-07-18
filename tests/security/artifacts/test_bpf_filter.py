"""BPF capture-filter hygiene, validation, and trim (ADR-0385)."""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.artifacts import bpf_filter as bf

_HAS_TCPDUMP = shutil.which("tcpdump") is not None
_needs_tcpdump = pytest.mark.skipif(not _HAS_TCPDUMP, reason="tcpdump not installed")


def test_hygiene_accepts_none_and_normal() -> None:
    assert bf.hygiene_reason(None) is None
    assert bf.hygiene_reason("tcp port 80 and host 10.0.0.5") is None


def test_hygiene_rejects_too_long() -> None:
    assert bf.hygiene_reason("a" * (bf.MAX_FILTER_LEN + 1)) == "too_long"


def test_hygiene_rejects_non_printable() -> None:
    assert bf.hygiene_reason("tcp\nport 80") == "non_printable"


@_needs_tcpdump
def test_validate_bpf_accepts_valid() -> None:
    bf.validate_bpf("tcp port 80")  # compiles → no raise


@_needs_tcpdump
def test_validate_bpf_rejects_garbage() -> None:
    with pytest.raises(CategorizedError) as excinfo:
        bf.validate_bpf("this is not a filter )(")
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details.get("reason") == "invalid_filter"
    # A rejected filter is deterministically bad: the job must dead-letter, not retry the window.
    assert excinfo.value.terminal is True


def test_missing_tcpdump_binary_is_not_terminal() -> None:
    # A missing binary / timeout may be transient/infra, so it stays retryable (non-terminal).
    with pytest.raises(CategorizedError) as excinfo:
        bf._run(["definitely-not-a-real-binary-xyz", "-d", "tcp"], "filter validation")
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.terminal is False


@_needs_tcpdump
def test_validate_bpf_metachars_are_not_shell_interpreted() -> None:
    marker = Path("/tmp/kdive_bpf_pwned_marker")  # noqa: S108 - deliberate injection probe
    marker.unlink(missing_ok=True)
    # Passed as one argv element: tcpdump -d fails to compile it; the shell never runs it.
    with pytest.raises(CategorizedError):
        bf.validate_bpf("tcp; touch /tmp/kdive_bpf_pwned_marker")
    assert not marker.exists()


@_needs_tcpdump
def test_trim_pcap_writes_filtered_output(tmp_path: Path) -> None:
    # A minimal 2-record little-endian pcap (link-type 1 = Ethernet). The filter keeps whatever
    # matches; correctness we assert is only that trim runs and writes a valid pcap file.
    header = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    record = struct.pack("<IIII", 0, 0, 4, 4) + b"\x00\x00\x00\x00"
    src = tmp_path / "in.pcap"
    src.write_bytes(header + record)
    dst = tmp_path / "out.pcap"
    bf.trim_pcap(src, dst, "ether proto 0")
    assert dst.exists()
    assert dst.read_bytes()[:4] == header[:4]  # valid pcap magic
