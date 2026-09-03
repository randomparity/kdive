"""ADR-0583's three published golden identity vectors, against local-libvirt (#2159).

ADR-0583 publishes three digests as the conformance vectors for the versioned two-part libvirt
definition identity — the preserved digest over the inactive definition minus
``/domain/os/{kernel,initrd,cmdline}``, and the boot projection digest over exactly those three
fields. They are normative: the algorithm exists to reproduce them, and a constant nobody asserts
is a decision that can stop being true without anything failing.

Local-libvirt carries one implementation and remote-libvirt carries a second, which
``tests/providers/remote_libvirt/lifecycle/test_external_boot.py`` pins against the same three
literals. Each copy is held to the record rather than to the other, so the two agree on these
vectors without either being the other's oracle.

The literals below are transcribed from the record and are never computed here. A digest compared
against a value the implementation under test produced would assert nothing.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from kdive.providers.local_libvirt.lifecycle.boot.external_boot import render_target_xml
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    _boot_identity,
    _preserved_identity,
)

# Transcribed from docs/adr/0583-external-run-boot-uses-prepared-recovery-points.md, the
# "Golden vector:" sentence through the non-ASCII projection digest that closes that paragraph.
_GOLDEN_SOURCE = '<domain><os><type arch="x86_64">hvm</type></os></domain>'
_GOLDEN_PRESERVED = "sha256:3e3cde0b5115867e991160f1d361fef3ec0734e8a87e2ab003d62cc0f8af4eea"
_GOLDEN_NULL_BOOT = "sha256:c48b5e5a6e9ac64b1129c1d468ce0de305288a86a6575467fb15f71d3c14b925"
_GOLDEN_UNICODE_BOOT = "sha256:06bf5b2aceb13f19b7debd17181ada54041d883f926c9c5f4c0acae4336f58fb"

# The two non-ASCII fields of the ADR's third vector. Both carry U+00E9, so the source is NFC and
# `render_target_xml` admits it; a decomposed e + U+0301 would change the digest and fail loudly.
_GOLDEN_KERNEL = "/var/lib/kdive/café"
_GOLDEN_CMDLINE = "root=LABEL=café"


def test_preserved_identity_matches_the_adr_golden_vector() -> None:
    assert _preserved_identity(ET.fromstring(_GOLDEN_SOURCE)) == _GOLDEN_PRESERVED


def test_all_null_boot_projection_matches_the_adr_golden_vector() -> None:
    assert _boot_identity(ET.fromstring(_GOLDEN_SOURCE)) == _GOLDEN_NULL_BOOT


def test_non_ascii_boot_projection_matches_the_adr_golden_vector() -> None:
    """The provider's own renderer must produce the projection the ADR digests."""
    projected = render_target_xml(
        _GOLDEN_SOURCE, kernel=_GOLDEN_KERNEL, initrd=None, cmdline=_GOLDEN_CMDLINE
    )
    assert _boot_identity(ET.fromstring(projected)) == _GOLDEN_UNICODE_BOOT
