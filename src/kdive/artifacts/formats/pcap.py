"""Endianness-aware libpcap record counter (ADR-0385).

Reads the 4-byte magic to pick byte order (``0xa1b2c3d4`` native / ``0xd4c3b2a1`` swapped, plus the
nanosecond variants ``0xa1b23c4d`` / ``0x4d3cb2a1``), then walks 16-byte record headers by
``incl_len``. Counts only whole records: a truncated final record (a capture cut off mid-record)
is ignored, so a header-only file is zero. Non-pcap input is zero, never an exception — the count
only drives a telemetry signal, so it must never fail the capture.
"""

from __future__ import annotations

import struct

_GLOBAL_HEADER_LEN = 24
_RECORD_HEADER_LEN = 16
_LITTLE_ENDIAN_MAGICS = {0xA1B2C3D4, 0xA1B23C4D}
_BIG_ENDIAN_MAGICS = {0xD4C3B2A1, 0x4D3CB2A1}


def count_pcap_packets(data: bytes) -> int:
    """Count whole libpcap records in ``data``; 0 for header-only or non-pcap input."""
    if len(data) < _GLOBAL_HEADER_LEN:
        return 0
    magic = struct.unpack("<I", data[:4])[0]
    if magic in _LITTLE_ENDIAN_MAGICS:
        endian = "<"
    elif magic in _BIG_ENDIAN_MAGICS:
        endian = ">"
    else:
        return 0
    offset = _GLOBAL_HEADER_LEN
    count = 0
    while offset + _RECORD_HEADER_LEN <= len(data):
        incl_len = struct.unpack(endian + "I", data[offset + 8 : offset + 12])[0]
        end = offset + _RECORD_HEADER_LEN + incl_len
        if end > len(data):
            break  # truncated final record — not counted
        count += 1
        offset = end
    return count
