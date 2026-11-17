"""Guard: a pydantic dump without ``by_alias`` is reviewed before it ships (#3676).

An input key spelled as an aliased field's Python name (``schema_`` for ``Field(alias="schema")``)
was dropped silently by closed (``extra="forbid"``) models up to pydantic 2.14.0 and raises
``extra_forbidden`` from 2.14.3. A dump that omits ``by_alias=True`` emits those Python names, so a
dump of an alias-bearing model that reaches a closed model or the wire fails only at runtime.

The scanner is syntactic: it cannot see a receiver's type. Every ``model_dump``-family call under
``src/`` and ``deploy/`` that carries neither a ``by_alias=`` keyword nor a ``**`` argument is
therefore listed in ``_ALLOWED`` by file, with a count and a reason a reviewer has checked. A new
dump in any file turns the guard red until its receiver is read; the fix is to pass
``by_alias=True`` or to extend the entry with the reason the dump is safe.

Known gap, accepted in the spec's failure model: the guard compares counts, so adding an alias to a
model already dumped bare in a listed file, or swapping one dump for another in the same file,
stays green.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_ROOTS = ("src", "deploy")
_DUMP_NAMES = frozenset({"model_dump", "model_dump_json", "dump_python", "dump_json"})

_NO_ALIAS = "receivers, and the models nested in them, declare no alias"
_MIXED_LOCAL = (
    "the anchor dump in execute_system_teardown is alias-bearing, dumped by Python names "
    "on purpose "
    "(exclude schema_, same-class validate); the other dumps receive models with no alias"
)
_MIXED_REMOTE = (
    "the anchor dumps in matches_anchor and execute_system_teardown are alias-bearing, dumped "
    "by Python names on purpose "
    "(exclude schema_, same-class validate); the other dumps receive models with no alias"
)

_ALLOWED: dict[str, tuple[int, str]] = {
    "src/kdive/admin/default_fixtures.py": (1, _NO_ALIAS),
    "src/kdive/components/records.py": (2, _NO_ALIAS),
    "src/kdive/db/repositories.py": (2, _NO_ALIAS),
    "src/kdive/domain/external_boot_activation.py": (1, _NO_ALIAS),
    "src/kdive/inventory/reconcile/overrides.py": (1, _NO_ALIAS),
    "src/kdive/inventory/reconcile/resources.py": (1, _NO_ALIAS),
    "src/kdive/inventory/serialize.py": (2, _NO_ALIAS),
    "src/kdive/jobs/authority_sender.py": (1, _NO_ALIAS),
    "src/kdive/jobs/capture_operations/protocol.py": (1, _NO_ALIAS),
    "src/kdive/jobs/payloads.py": (1, _NO_ALIAS),
    "src/kdive/mcp/middleware/binding_errors.py": (1, _NO_ALIAS),
    "src/kdive/mcp/middleware/compact.py": (1, _NO_ALIAS),
    "src/kdive/mcp/middleware/denial_audit.py": (1, _NO_ALIAS),
    "src/kdive/mcp/middleware/server_fault.py": (1, _NO_ALIAS),
    "src/kdive/mcp/tools/debug/operations/breakpoints.py": (1, _NO_ALIAS),
    "src/kdive/mcp/tools/debug/operations/modules.py": (1, _NO_ALIAS),
    "src/kdive/mcp/tools/debug/operations/stack.py": (3, _NO_ALIAS),
    "src/kdive/mcp/tools/debug/operations/watchpoints.py": (1, _NO_ALIAS),
    "src/kdive/mcp/tools/gateway.py": (5, _NO_ALIAS),
    "src/kdive/mcp/tools/lifecycle/investigations/view.py": (1, _NO_ALIAS),
    "src/kdive/mcp/tools/lifecycle/support/_idempotency.py": (1, _NO_ALIAS),
    "src/kdive/processes/lifecycle/systemd/systemd_worker_contract.py": (3, _NO_ALIAS),
    "src/kdive/processes/lifecycle/systemd/systemd_worker_state.py": (3, _NO_ALIAS),
    "src/kdive/profiles/build.py": (1, _NO_ALIAS),
    "src/kdive/providers/external_boot_authority/device_identity.py": (1, _NO_ALIAS),
    "src/kdive/providers/external_boot_authority/module_volume_sweep.py": (1, _NO_ALIAS),
    "src/kdive/providers/external_boot_authority/service.py": (2, _NO_ALIAS),
    "src/kdive/providers/external_boot_authority/transport.py": (1, _NO_ALIAS),
    "src/kdive/providers/local_libvirt/external_boot_authority.py": (7, _MIXED_LOCAL),
    "src/kdive/providers/local_libvirt/lifecycle/boot/recovery.py": (1, _NO_ALIAS),
    "src/kdive/providers/ports/traffic.py": (2, _NO_ALIAS),
    "src/kdive/providers/remote_libvirt/authority_client.py": (3, _NO_ALIAS),
    "src/kdive/providers/remote_libvirt/external_boot_authority.py": (5, _MIXED_REMOTE),
    "src/kdive/providers/shared/debug_common/gdbmi/commands/disassembly.py": (1, _NO_ALIAS),
    "src/kdive/providers/shared/debug_common/gdbmi/commands/modules.py": (1, _NO_ALIAS),
    "src/kdive/providers/shared/debug_common/gdbmi/commands/stack.py": (3, _NO_ALIAS),
    "src/kdive/providers/shared/debug_common/gdbmi/core/engine.py": (1, _NO_ALIAS),
    "src/kdive/providers/shared/debug_common/gdbmi/core/transcript.py": (1, _NO_ALIAS),
    "src/kdive/providers/system_authority/manifest.py": (1, _NO_ALIAS),
    "src/kdive/providers/system_authority/service.py": (2, _NO_ALIAS),
    "src/kdive/services/investigations/common.py": (1, _NO_ALIAS),
    "src/kdive/services/runs/admission.py": (1, _NO_ALIAS),
    "src/kdive/services/systems/admission.py": (1, _NO_ALIAS),
}


def collect_unaliased_dumps(source: str) -> list[int]:
    """Return the line numbers of dump calls with no ``by_alias=`` and no ``**`` argument."""
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _DUMP_NAMES
        and not any(kw.arg in (None, "by_alias") for kw in node.keywords)
    )


def _tree_sites() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for root in _ROOTS:
        for path in sorted((_REPO / root).rglob("*.py")):
            lines = collect_unaliased_dumps(path.read_text(encoding="utf-8"))
            if lines:
                found[path.relative_to(_REPO).as_posix()] = lines
    return found


def _problems(found: dict[str, list[int]], allowed: dict[str, tuple[int, str]]) -> list[str]:
    problems = []
    for path, lines in found.items():
        entry = allowed.get(path)
        if entry is None:
            problems.append(f"{path}:{lines}: unreviewed dump without by_alias")
        elif entry[0] != len(lines):
            problems.append(f"{path}:{lines}: {len(lines)} dumps, but _ALLOWED reviews {entry[0]}")
    problems.extend(
        f"{path}: _ALLOWED entry matches no dump" for path in allowed if path not in found
    )
    problems.extend(f"{path}: empty reason" for path, (_, reason) in allowed.items() if not reason)
    return problems


def test_every_unaliased_dump_is_reviewed() -> None:
    problems = _problems(_tree_sites(), _ALLOWED)
    assert not problems, (
        "pydantic dump(s) without by_alias: closed models reject Python alias names. "
        "Pass by_alias=True, or read the receiver and update _ALLOWED with the reason it is safe:\n"
        + "\n".join(problems)
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("m.model_dump()", [1]),
        ("m.model_dump(mode='json', exclude_none=True)", [1]),
        ("m.model_dump_json()", [1]),
        ("adapter.dump_python(m)", [1]),
        ("m.model_dump(**opts)", []),
        ("m.model_dump(by_alias=True)", []),
        ("m.model_dump(\n    mode='json',\n    by_alias=False,\n)", []),
        ("json.dumps(m)", []),
        ("x = 1\nm.model_dump()\nn.model_dump()", [2, 3]),
    ],
)
def test_scanner_flags_only_dumps_with_no_stated_alias_choice(
    source: str, expected: list[int]
) -> None:
    assert collect_unaliased_dumps(source) == expected


def test_scanner_rejects_unparsable_source() -> None:
    with pytest.raises(SyntaxError):
        collect_unaliased_dumps("def (")


def test_stale_or_unreviewed_entries_fail() -> None:
    found = {"a.py": [3], "b.py": [4, 9]}
    assert _problems(found, {"a.py": (1, "r"), "b.py": (2, "r")}) == []
    assert any("unreviewed" in p for p in _problems(found, {"a.py": (1, "r")}))
    assert any("reviews 1" in p for p in _problems(found, {"a.py": (1, "r"), "b.py": (1, "r")}))
    assert any("matches no dump" in p for p in _problems({}, {"gone.py": (1, "r")}))
    assert any("empty reason" in p for p in _problems({"a.py": [1]}, {"a.py": (1, "")}))
