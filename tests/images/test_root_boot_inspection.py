"""Bounded remote root inspection tests (ADR-0583, #2106)."""

from __future__ import annotations

import sys
from xml.etree.ElementTree import ParseError

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.planes.provenance_probes import (
    ROOT_INSPECTION_MAX_OUTPUT_BYTES,
    _parse_root_boot,
    _run_bounded_inspector,
)


def test_bounded_runner_accepts_exact_combined_cap() -> None:
    code = f"import os; os.write(1, b'x' * {ROOT_INSPECTION_MAX_OUTPUT_BYTES})"
    stdout, stderr = _run_bounded_inspector([sys.executable, "-c", code], timeout_s=5)
    assert len(stdout) + len(stderr) == ROOT_INSPECTION_MAX_OUTPUT_BYTES


def test_bounded_runner_kills_cap_plus_one() -> None:
    code = f"import os; os.write(1, b'x' * {ROOT_INSPECTION_MAX_OUTPUT_BYTES + 1})"
    with pytest.raises(CategorizedError) as exc:
        _run_bounded_inspector([sys.executable, "-c", code], timeout_s=5)
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert exc.value.details["reason"] == "output_limit"


def test_bounded_runner_kills_timeout() -> None:
    with pytest.raises(CategorizedError) as exc:
        _run_bounded_inspector([sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=0.01)
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert exc.value.details["reason"] == "timeout"


def test_bounded_runner_kills_partial_output_then_timeout() -> None:
    code = "import os,time; os.write(1,b'prefix'); time.sleep(5)"
    with pytest.raises(CategorizedError) as exc:
        _run_bounded_inspector([sys.executable, "-c", code], timeout_s=0.01)
    assert exc.value.details["reason"] == "timeout"


def test_parser_builds_digest_bound_root_spec() -> None:
    xml = b"""<operatingsystems><operatingsystem><mountpoints>
      <mountpoint dev='/dev/sda2'>/</mountpoint></mountpoints><filesystems>
      <filesystem dev='/dev/sda2'><type>xfs</type><uuid>abc</uuid></filesystem>
      </filesystems></operatingsystem></operatingsystems>"""
    result = _parse_root_boot(xml, "x86_64", "sha256:" + "a" * 64)
    assert result.root == "UUID=abc"
    assert result.source.identity == "sha256:" + "a" * 64


@pytest.mark.parametrize("xml", [b"not xml", b"<x/>", b"<!DOCTYPE x><x/>"])
def test_parser_rejects_malformed_or_incomplete_output(xml: bytes) -> None:
    with pytest.raises((ValueError, ParseError)):
        _parse_root_boot(xml, "x86_64", "sha256:" + "a" * 64)
