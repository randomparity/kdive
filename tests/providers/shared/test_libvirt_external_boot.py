"""Shared libvirt external-boot definition contract (ADR-0583, #2159)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable

import pytest

from kdive.providers.shared.libvirt_external_boot import (
    boot_projection_identity,
    preserved_definition_identity,
    render_target_xml,
)

_GOLDEN_SOURCE = '<domain><os><type arch="x86_64">hvm</type></os></domain>'
_GOLDEN_PRESERVED = "sha256:3e3cde0b5115867e991160f1d361fef3ec0734e8a87e2ab003d62cc0f8af4eea"
_GOLDEN_NULL_BOOT = "sha256:c48b5e5a6e9ac64b1129c1d468ce0de305288a86a6575467fb15f71d3c14b925"
_GOLDEN_UNICODE_BOOT = "sha256:06bf5b2aceb13f19b7debd17181ada54041d883f926c9c5f4c0acae4336f58fb"


def test_preserved_identity_matches_the_adr_0583_golden_vector() -> None:
    assert preserved_definition_identity(_GOLDEN_SOURCE) == _GOLDEN_PRESERVED


def test_all_null_boot_projection_matches_the_adr_0583_golden_vector() -> None:
    assert boot_projection_identity(_GOLDEN_SOURCE) == _GOLDEN_NULL_BOOT


def test_non_ascii_boot_projection_matches_the_adr_0583_golden_vector() -> None:
    projected = render_target_xml(
        _GOLDEN_SOURCE,
        kernel="/var/lib/kdive/café",
        initrd=None,
        cmdline="root=LABEL=café",
    )
    assert boot_projection_identity(projected) == _GOLDEN_UNICODE_BOOT


def test_preserved_identity_ignores_realistic_formatting_whitespace() -> None:
    formatted = """<domain>
  <name>system</name>
  <os>
    <type arch="x86_64">hvm</type>
    <boot dev="hd" />
  </os>
  <devices>
    <disk type="file"><source file="/pool/root.qcow2" /></disk>
  </devices>
</domain>"""
    compact = (
        '<domain><name>system</name><os><type arch="x86_64">hvm</type>'
        '<boot dev="hd" /></os><devices><disk type="file">'
        '<source file="/pool/root.qcow2" /></disk></devices></domain>'
    )
    assert preserved_definition_identity(formatted) == preserved_definition_identity(compact)


def test_preserved_identity_subtracts_all_three_boot_fields() -> None:
    source = (
        '<domain><name>system</name><os><type arch="x86_64">hvm</type>'
        '<boot dev="hd" /></os></domain>'
    )
    target = render_target_xml(
        source,
        kernel="/var/lib/kdive/kernel",
        initrd="/var/lib/kdive/initrd",
        cmdline="  root=/dev/vda1  console=ttyS0  ",
    )
    assert preserved_definition_identity(target) == preserved_definition_identity(source)


@pytest.mark.parametrize(
    "path",
    [
        "relative/kernel",
        "/",
        "//var/lib/kdive/kernel",
        "/var/lib/kdive/../kernel",
        "/var/lib/kdive/./kernel",
        "/var/lib//kdive/kernel",
        "/var/lib/kdive/cafe\u0301",
        "/var/lib/kdive/kernel\x00suffix",
        "/var/lib/kdive/kernel\x01suffix",
        "/" + "x" * 1_024,
    ],
    ids=[
        "relative",
        "root",
        "double-root",
        "parent",
        "dot",
        "empty-segment",
        "non-nfc",
        "nul",
        "xml-control",
        "over-1024-bytes",
    ],
)
def test_projection_rejects_a_noncanonical_kernel_path(path: str) -> None:
    with pytest.raises(ValueError, match="kernel path"):
        render_target_xml(_GOLDEN_SOURCE, kernel=path, initrd=None, cmdline="root=/dev/vda1")


def test_projection_rejects_a_noncanonical_initrd_path() -> None:
    with pytest.raises(ValueError, match="initrd path"):
        render_target_xml(
            _GOLDEN_SOURCE,
            kernel="/var/lib/kdive/kernel",
            initrd="initrd",
            cmdline="root=/dev/vda1",
        )


def test_projection_rejects_an_unrepresentable_command_line() -> None:
    with pytest.raises(ValueError, match="command line"):
        render_target_xml(
            _GOLDEN_SOURCE,
            kernel="/var/lib/kdive/kernel",
            initrd=None,
            cmdline="root=/dev/vda1\x01",
        )


def test_projection_preserves_a_decomposed_command_line_without_normalizing() -> None:
    cmdline = "root=LABEL=cafe\u0301"
    projected = render_target_xml(
        _GOLDEN_SOURCE,
        kernel="/var/lib/kdive/kernel",
        initrd=None,
        cmdline=cmdline,
    )

    assert ET.fromstring(projected).findtext("./os/cmdline") == cmdline
    assert preserved_definition_identity(projected) == preserved_definition_identity(_GOLDEN_SOURCE)
    assert boot_projection_identity(projected) != boot_projection_identity(_GOLDEN_SOURCE)


@pytest.mark.parametrize(
    "identity", [preserved_definition_identity, boot_projection_identity], ids=["preserved", "boot"]
)
def test_identity_rejects_non_nfc_text_outside_the_command_line(
    identity: Callable[[str], str],
) -> None:
    with pytest.raises(ValueError, match="NFC outside"):
        identity("<domain><name>cafe\u0301</name><os><cmdline>cafe\u0301</cmdline></os></domain>")


def test_projection_preserves_the_accepted_command_line_scalar_exactly() -> None:
    cmdline = "  root=/dev/vda1   console=ttyS0 café  "
    projected = render_target_xml(
        _GOLDEN_SOURCE,
        kernel="/var/lib/kdive/kernel",
        initrd=None,
        cmdline=cmdline,
    )
    assert ET.fromstring(projected).findtext("./os/cmdline") == cmdline
