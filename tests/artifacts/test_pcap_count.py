"""Endianness-aware pcap record counting (ADR-0385)."""

from __future__ import annotations

import struct

from kdive.artifacts.pcap_count import count_pcap_packets


def _header(magic: int, endian: str) -> bytes:
    # magic, version_major, version_minor, thiszone, sigfigs, snaplen, network
    return struct.pack(endian + "IHHiIII", magic, 2, 4, 0, 0, 65535, 1)


def _record(endian: str, n: int) -> bytes:
    # ts_sec, ts_usec, incl_len, orig_len, then incl_len bytes of payload
    return struct.pack(endian + "IIII", 0, 0, n, n) + b"\x00" * n


def test_header_only_is_zero() -> None:
    assert count_pcap_packets(_header(0xA1B2C3D4, "<")) == 0


def test_little_endian_two_records() -> None:
    data = _header(0xA1B2C3D4, "<") + _record("<", 4) + _record("<", 8)
    assert count_pcap_packets(data) == 2


def test_big_endian_two_records() -> None:
    data = _header(0xA1B2C3D4, ">") + _record(">", 4) + _record(">", 8)
    assert count_pcap_packets(data) == 2


def test_nanosecond_magic_counts() -> None:
    data = _header(0xA1B23C4D, "<") + _record("<", 4)
    assert count_pcap_packets(data) == 1


def test_truncated_tail_counts_whole_records_only() -> None:
    data = _header(0xA1B2C3D4, "<") + _record("<", 4) + b"\x00\x00\x00"
    assert count_pcap_packets(data) == 1


def test_not_a_pcap_is_zero() -> None:
    assert count_pcap_packets(b"garbage bytes that are not a pcap") == 0


def test_empty_is_zero() -> None:
    assert count_pcap_packets(b"") == 0
