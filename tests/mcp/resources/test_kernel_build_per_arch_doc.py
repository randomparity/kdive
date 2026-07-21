"""Drift guard for the per-arch kernel build-hints reference (ADR-0412, #1383).

The canonical doc ``docs/guide/kernel-build-per-arch.md`` restates arch-varying facts whose
source of truth is code (``SUPPORTED_ARCHES``, ``BOOT_MEMBER_FORMATS``,
``default_crashkernel_summary``). These tests bind the code-owned facts so a future arch — or a
changed default/container — cannot leave the doc stale. They read the **canonical** doc;
``test_doc_resources.py`` binds the served ``_content`` snapshot to it.
"""

from __future__ import annotations

import re
from pathlib import Path

from kdive.build_artifacts.validation import _ELF_MAGIC, BOOT_MEMBER_FORMATS
from kdive.domain.platform.arch_traits import (
    SUPPORTED_ARCHES,
    arch_traits,
    default_crashkernel_summary,
)
from kdive.mcp.resources import registrar
from kdive.mcp.resources.registrar import DOC_RESOURCES
from kdive.profiles.build import BuildProfile

_ROOT = Path(__file__).resolve().parents[3]
_DOC = _ROOT / "docs" / "guide" / "kernel-build-per-arch.md"
_DOC_URI = "resource://kdive/docs/guide/kernel-build-per-arch.md"

# An arch token is a bare lowercase identifier (x86_64, ppc64le, aarch64); a multi-word aux
# heading is not, so aux sections are excluded from the arch set without an allowlist (ADR-0412).
_ARCH_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")
_FENCE = re.compile(r"^```")
_H2 = re.compile(r"^##\s+(.*\S)\s*$")
# A `strip` invocation carrying a standalone `-s` flag, tolerant of the doc's
# `"${CROSS_COMPILE}strip" -s` quoting; not the bare word `strip` (which appears in "already
# stripped") and not the bare literal `strip -s` (the quote in `strip" -s` would defeat it).
_STRIP_S = re.compile(r"strip.*\s-s\b")
_RESOURCE_URI = re.compile(r"resource://kdive/docs/[^\s)\"'>]+")


def _doc_text() -> str:
    return _DOC.read_text(encoding="utf-8")


def _h2_sections(text: str) -> dict[str, str]:
    """Map each level-2 heading (outside code fences) to its **raw** body text.

    Fenced code blocks are skipped for *heading detection* only — so a ``##``-prefixed line
    inside a shell sample is not miscounted — but section bodies are cut from the raw lines, so
    the content checks still see the fenced ``strip -s`` command and prose (ADR-0412).
    """
    lines = text.splitlines()
    fence = False
    headings: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fence = not fence
            continue
        if fence:
            continue
        m = _H2.match(line)
        if m:
            headings.append((i, m.group(1)))
    sections: dict[str, str] = {}
    for idx, (start, title) in enumerate(headings):
        end = headings[idx + 1][0] if idx + 1 < len(headings) else len(lines)
        sections[title] = "\n".join(lines[start + 1 : end])
    return sections


def _elf_required_arches() -> set[str]:
    """Arches whose boot format is an ELF kernel, read structurally from the contract.

    An ELF arch declares a ``MagicPin`` at offset 0 whose bytes *start with* the ELF magic —
    a **prefix** match, because ppc64le's pin is ``\\x7fELF\\x02\\x01`` (hex ``7f454c460201``),
    not the bare ``\\x7fELF`` (``7f454c46``); an equality check would never fire (ADR-0412).
    """
    elf_prefix = _ELF_MAGIC.hex()
    return {
        arch
        for arch, fmt in BOOT_MEMBER_FORMATS.items()
        if any(pin.offset == 0 and pin.hex.startswith(elf_prefix) for pin in fmt.magic)
    }


def test_documents_exactly_supported_arches() -> None:
    # Bidirectional: collecting by arch-token *shape* (not membership) fails both a missing
    # supported arch and a spurious/misnamed `## aarch64` section.
    documented = {title for title in _h2_sections(_doc_text()) if _ARCH_TOKEN.match(title)}
    assert documented == set(SUPPORTED_ARCHES), (
        f"doc arch sections {sorted(documented)} != SUPPORTED_ARCHES {sorted(SUPPORTED_ARCHES)}"
    )


def test_boot_container_name_present_per_arch() -> None:
    sections = _h2_sections(_doc_text())
    for arch, fmt in BOOT_MEMBER_FORMATS.items():
        body = sections.get(arch, "")  # defensive: a section-less arch yields a clean failure
        assert fmt.container in body, (
            f"{arch} section is missing its boot-container name {fmt.container!r}"
        )


def test_strip_required_arches_carry_a_strip_command() -> None:
    elf = _elf_required_arches()
    assert elf, "the strip-required (ELF) arch set is empty — the ELF predicate is dead"
    sections = _h2_sections(_doc_text())
    for arch in elf:
        body = sections.get(arch, "")
        assert _STRIP_S.search(body), f"{arch} section is missing a `strip -s` invocation"


def test_crashkernel_summary_present_verbatim() -> None:
    assert default_crashkernel_summary() in _doc_text()


def test_crashkernel_default_present_in_each_arch_section() -> None:
    # The combined summary is bound above; this also binds the per-section restatement so a
    # changed _TRAITS default cannot leave an arch's section bullet stale (branch review #1383).
    sections = _h2_sections(_doc_text())
    for arch in SUPPORTED_ARCHES:
        body = sections.get(arch, "")
        expected = arch_traits(arch).default_crashkernel
        assert expected in body, f"{arch} section is missing its crashkernel default {expected!r}"


def test_doc_registered_as_all_audience_resource() -> None:
    entry = next((e for e in DOC_RESOURCES if e.uri == _DOC_URI), None)
    assert entry is not None, f"{_DOC_URI} is not registered in DOC_RESOURCES"
    assert entry.audience == "all"
    assert entry.required_kind is None
    assert (registrar._CONTENT_DIR / entry.content_file).is_file()


def test_arch_field_cites_new_doc_and_all_its_citations_resolve() -> None:
    # Neither served-doc-links nor the served-doc citation pytest scans tool Field descriptions,
    # so this is the only guard that the repointed arch citation resolves to the allowlist.
    description = BuildProfile.model_fields["arch"].description or ""
    assert _DOC_URI in description, "BuildProfile.arch Field does not cite the per-arch doc"
    allow = {entry.uri for entry in DOC_RESOURCES}
    cited = {uri.rstrip(".,;") for uri in _RESOURCE_URI.findall(description)}
    assert not (cited - allow), (
        f"BuildProfile.arch cites non-allowlisted resource URIs: {sorted(cited - allow)}"
    )
