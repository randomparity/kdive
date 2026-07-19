"""Direct unit tests for the shared ``host_cpu`` dict composer (ADR-0369)."""

from __future__ import annotations

from kdive.providers.shared.host_cpu import host_cpu_dict
from kdive.providers.shared.libvirt_xml import ParsedHostCpu


def _parsed(
    *,
    model: str = "Cascadelake",
    vendor: str | None = None,
    arch: str | None = None,
    disabled: frozenset[str] = frozenset(),
) -> ParsedHostCpu:
    return ParsedHostCpu(model=model, vendor=vendor, arch=arch, disabled_features=disabled)


def test_no_vendor_no_baseline_yields_model_and_arch_only() -> None:
    # The edge folded in by #1304: an unmapped model with no vendor keeps the dict minimal.
    result = host_cpu_dict(_parsed(model="Cascadelake", vendor=None, arch=None), "x86_64")
    assert result == {"model": "Cascadelake", "arch": "x86_64"}


def test_arch_falls_back_when_parsed_block_carries_none() -> None:
    result = host_cpu_dict(_parsed(arch=None), "aarch64")
    assert result["arch"] == "aarch64"


def test_parsed_arch_wins_over_fallback() -> None:
    result = host_cpu_dict(_parsed(arch="x86_64"), "aarch64")
    assert result["arch"] == "x86_64"


def test_vendor_included_only_when_present() -> None:
    assert host_cpu_dict(_parsed(vendor="Intel"), "x86_64")["vendor"] == "Intel"
    assert "vendor" not in host_cpu_dict(_parsed(vendor=None), "x86_64")


def test_baseline_level_added_for_a_mapped_x86_model() -> None:
    # Nehalem maps to x86-64-v2 in the model table; the level is folded in.
    result = host_cpu_dict(_parsed(model="Nehalem"), "x86_64")
    assert result["baseline_level"] == "x86-64-v2"


def test_baseline_level_omitted_for_an_unmapped_model() -> None:
    assert "baseline_level" not in host_cpu_dict(_parsed(model="not-a-real-model"), "x86_64")
