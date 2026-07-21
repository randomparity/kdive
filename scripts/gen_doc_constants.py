"""Generate and drift-check code-derived doc constants (ADR-0410).

Some agent-facing prose restates a value whose single source of truth is a Python
constant: the effective single-PUT upload ceiling (``min`` of the S3 single-object cap and
the ``KDIVE_MAX_UPLOAD_BYTES`` policy limit) and the approximate size of the tool catalog.
Hand-copied, these drift silently as the source changes — the review that filed #1368 found
``agent-index.md`` claiming "~100 tools" while the live registry had grown well past that.

This script derives each value from source and, like ``resources-docs-check``, fails when a
committed doc disagrees. Two binding kinds:

* **Generated** (``writable``): a pure derived number in a Markdown doc the generator owns.
  ``just doc-constants`` rewrites it in place; ``--check`` verifies it.
* **Guarded** (not ``writable``): a figure embedded in a hand-authored source docstring whose
  surrounding sentence carries nuance a generator must not rewrite. ``--check`` asserts it
  equals the source-derived value; a drift is fixed by a human editing the sentence (the
  failure message names the expected value).

Run via ``just doc-constants`` (write) / ``just doc-constants-check`` (verify).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from kdive.config.core_settings import MAX_UPLOAD_BYTES
from kdive.mcp.tools.catalog.artifacts.uploads import SINGLE_PUT_MAX_BYTES
from scripts.gen_tool_reference import _registry_tools

_ROOT = Path(__file__).resolve().parents[1]
_GIB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class Binding:
    """One doc constant: where it is written and the source-derived value it must equal.

    ``pattern`` matches the constant's occurrence(s) in ``path`` with group 1 capturing the
    varying value. ``expected`` is that value freshly rendered from source. ``writable`` marks
    a generated doc the script rewrites; a non-writable binding is guarded in place.
    """

    label: str
    path: Path
    pattern: re.Pattern[str]
    expected: str
    writable: bool


def _render_gib(num_bytes: int) -> str:
    """Render an exact GiB multiple as ``"N GiB"``; raise on a non-multiple."""
    if num_bytes % _GIB != 0:
        raise ValueError(f"{num_bytes} is not an exact GiB multiple")
    return f"{num_bytes // _GIB} GiB"


def _effective_single_put_ceiling() -> str:
    """The effective single-PUT ceiling = ``min`` of the S3 cap and the policy limit."""
    policy_limit = MAX_UPLOAD_BYTES.parse(MAX_UPLOAD_BYTES.default or "")
    return _render_gib(min(SINGLE_PUT_MAX_BYTES, policy_limit))


def _approx_tool_count() -> str:
    """The live tool count rounded to the nearest ten (the ``~`` prefix is doc context)."""
    return str(round(len(_registry_tools()), -1))


def bindings() -> list[Binding]:
    """Build every doc-constant binding, computing each expected value from source."""
    return [
        Binding(
            label="agent-index tool count",
            path=_ROOT / "docs" / "guide" / "agent-index.md",
            pattern=re.compile(r"~(\d+) tools"),
            expected=_approx_tool_count(),
            writable=True,
        ),
        Binding(
            label="artifacts upload single-PUT ceiling",
            path=_ROOT
            / "src"
            / "kdive"
            / "mcp"
            / "tools"
            / "catalog"
            / "artifacts"
            / "registrar.py",
            pattern=re.compile(r"the (\d+ GiB) single-PUT size limit"),
            expected=_effective_single_put_ceiling(),
            writable=False,
        ),
    ]


def _rel(path: Path) -> Path:
    """``path`` relative to the repo root, or ``path`` itself when it lives elsewhere."""
    return path.relative_to(_ROOT) if path.is_relative_to(_ROOT) else path


def _drift(binding: Binding) -> str | None:
    """Return an actionable message when ``binding``'s committed value is stale, else ``None``."""
    rel = _rel(binding.path)
    if not binding.path.is_file():
        return f"{binding.label}: missing {rel}"
    text = binding.path.read_text(encoding="utf-8")
    found = binding.pattern.findall(text)
    if not found:
        return f"{binding.label}: no occurrence in {rel}"
    stale = sorted({value for value in found if value != binding.expected})
    if not stale:
        return None
    if binding.writable:
        fix = "run 'just doc-constants'"
    else:
        fix = f"edit {rel} to say '{binding.expected}'"
    return (
        f"{binding.label}: {rel} states {stale} but source computes "
        f"'{binding.expected}' — {fix} and commit"
    )


def write() -> None:
    """Rewrite every writable binding's value in place from source."""
    for binding in bindings():
        if not binding.writable:
            continue
        text = binding.path.read_text(encoding="utf-8")

        def _sub(match: re.Match[str], expected: str = binding.expected) -> str:
            return match.group(0).replace(match.group(1), expected, 1)

        updated = binding.pattern.sub(_sub, text)
        if updated != text:
            binding.path.write_text(updated, encoding="utf-8")


def check() -> int:
    """Verify every committed doc constant equals its source-derived value.

    Returns 0 when all bindings are in sync, 1 otherwise (printing each drift to stderr).
    """
    all_bindings = bindings()
    drifts = [msg for b in all_bindings if (msg := _drift(b)) is not None]
    if drifts:
        for msg in drifts:
            print(msg, file=sys.stderr)
        return 1
    print(f"doc constants: {len(all_bindings)} in sync.")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--check":
        return check()
    write()
    print(f"wrote {len(bindings())} doc constants.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
