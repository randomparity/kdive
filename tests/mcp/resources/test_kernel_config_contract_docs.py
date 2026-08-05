"""The served external-build doc must state the kernel-config contract (#993).

The upload validator constrains artifact *structure* (bzImage magic, gzip layout, a
``lib/modules`` member, build-id match) and never which Kconfig symbols were enabled — so the
kernel config is the agent's to choose. Stating this keeps an agent from assuming it cannot get,
say, a KASAN kernel on the build lane. This guard keeps the contract from being dropped by a
later edit.

The assertion reads the **served snapshot** an agent receives over MCP, so it also fails if
the packaged snapshot falls out of sync with the source doc.

The second guard here covers both that snapshot and the operator-facing four-method runbook, and
keeps either from naming a symbol the reader cannot actually set (#1853):
``FEATURE_REQUIREMENTS.advertised`` deliberately omits prompt-less and auto-``select``ed symbols
such as ``VMCORE_INFO`` and ``KEXEC_CORE``, because ``make olddefconfig`` discards them out of a
config fragment. Prose that keeps naming one contradicts the manifest served beside it at
``resource://kdive/contracts/external-build``.

It matches only ``CONFIG_``-prefixed mentions on purpose. That prefix is how both docs say *build
this in*; the bare backticked name is how they refer to a symbol the reader must not try to set,
which is exactly what the fix for #1853 needed to keep saying.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from kdive.kernel_config.requirements import FEATURE_REQUIREMENTS
from kdive.mcp.resources.registrar import DOC_RESOURCES
from tests.kernel_config.unsettable_symbols import UNSETTABLE_SYMBOLS

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTENT_DIR = _REPO_ROOT / "src/kdive/mcp/resources/_content"

_CONFIG_SYMBOL = re.compile(r"CONFIG_([A-Z0-9_]+)")

_ADVERTISED: frozenset[str] = frozenset(
    symbol for f in FEATURE_REQUIREMENTS for clause in f.advertised for symbol in clause.symbols
)

# The prompt-less, auto-selected class no fragment can set: what the docs must never spell
# CONFIG_*. Read from the curated list rather than computed as "gated but not advertised", which
# is how #1853 wrote it. That subtraction stopped describing anything once #1854's rule
# barred an unsettable symbol from *every* clause: #1854 dropped the last two members
# (KEXEC_CORE, VMCORE_INFO) out of `gate_required`, so the computed set is now empty by
# construction and the bound below would hold vacuously. The curated list is also strictly wider
# than the subtraction ever was - DEBUG_INFO is unsettable and was never gated, so it could
# always have been allowlisted freely.
_UNSETTABLE: frozenset[str] = frozenset(UNSETTABLE_SYMBOLS)

# Prompted symbols the prose names as an optional refinement rather than a manifest requirement.
# KASAN_INLINE is a prompted member of lib/Kconfig.kasan's "Instrumentation type" choice, so a
# fragment sets it fine; the manifest simply requires neither instrumentation form.
_PROMPTED_BUT_UNREQUIRED: frozenset[str] = frozenset({"KASAN_INLINE"})

# Symbols every crash-capture doc must keep naming: the load selectors an agent sets to get
# KEXEC_CORE and VMCORE_INFO selected in turn, plus the /proc/vmcore the capture kernel reads.
_REQUIRED_MENTIONS: frozenset[str] = frozenset({"KEXEC", "KEXEC_FILE", "CRASH_DUMP", "PROC_VMCORE"})


def _served(name: str) -> str:
    entry = next(e for e in DOC_RESOURCES if e.name == name)
    return (_CONTENT_DIR / entry.content_file).read_text(encoding="utf-8")


def _served_external_build() -> str:
    return _served("external-build-upload")


def _four_method_runbook() -> str:
    return (_REPO_ROOT / "docs/operating/runbooks/four-method-live-run.md").read_text(
        encoding="utf-8"
    )


def test_external_build_doc_states_config_is_yours_to_choose() -> None:
    body = _served("external-build-upload")
    lowered = body.lower()
    assert "kasan" in lowered, (
        "served external-build doc gives no debug-config example (KASAN) (#993)"
    )
    assert "structure" in lowered, (
        "served external-build doc does not say the validator constrains structure only (#993)"
    )
    # The whole point: no Kconfig symbol is disallowed — the config is the agent's choice.
    assert "symbol" in lowered, (
        "served external-build doc does not say the validator never restricts config symbols (#993)"
    )


@pytest.mark.parametrize(
    ("label", "read"),
    [
        ("served external-build doc", _served_external_build),
        ("four-method live-run runbook", _four_method_runbook),
    ],
)
def test_crash_capture_docs_name_only_settable_symbols(label: str, read: Callable[[], str]) -> None:
    body = read()
    named = set(_CONFIG_SYMBOL.findall(body))

    assert named, f"{label} names no CONFIG_* symbol at all, so this guard would pass vacuously"
    assert named >= _REQUIRED_MENTIONS, (
        f"{label} stopped naming the settable crash-capture symbols "
        f"{sorted(_REQUIRED_MENTIONS - named)} (#1853)"
    )

    unsettable = sorted(named - _ADVERTISED - _PROMPTED_BUT_UNREQUIRED)
    assert not unsettable, (
        f"{label} tells a reader to build CONFIG_* symbols that FEATURE_REQUIREMENTS does not "
        f"advertise, so no config fragment can set them and olddefconfig drops the lines: "
        f"{unsettable}. Name the symbol that selects them instead (#1853)."
    )


def test_prompt_less_symbols_cannot_be_allowlisted_into_the_docs() -> None:
    """The allowlist is the one way to silence the guard above, so bound what it may hold.

    Without this, the cheapest way out of a #1853 failure is to add the offending symbol to
    ``_PROMPTED_BUT_UNREQUIRED`` and ship a green suite with the defect intact.
    """
    # Non-vacuity, restated for the same reason #1853 wrote it: the bound must be over a set
    # that really holds symbols, and thinning the curated list is the way to defeat it. The six
    # are spelled here rather than imported beside the list, so deleting a row costs an edit in
    # this file too and cannot pass as one hunk.
    named_unsettable_by_the_registry = {
        "KEXEC_CORE",
        "VMCORE_INFO",
        "DEBUG_INFO",
        "BPF_EVENTS",
        "TRACING",
        "DYNAMIC_FTRACE",
    }
    assert named_unsettable_by_the_registry <= _UNSETTABLE, sorted(
        named_unsettable_by_the_registry - _UNSETTABLE
    )
    smuggled = sorted(_PROMPTED_BUT_UNREQUIRED & _UNSETTABLE)
    assert not smuggled, (
        f"{smuggled} are prompt-less or auto-selected, so no config fragment can set them; "
        f"allowlisting them would let the docs demand them again (#1853, #1854)"
    )
