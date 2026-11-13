"""Guard: directory listings in src/kdive go through the rewinding helper (#3601).

``os.listdir(fd)`` misses entries on btrfs, so every ``os.listdir``/``os.scandir`` call goes
through ``list_directory_descriptor`` or a named allowlist entry. The allowlist holds only path
listings, which the descriptor defect cannot affect; it never exempts a descriptor. The scan sees
calls spelled ``os.listdir``/``os.scandir`` or through a name imported from ``os``; an aliased
module, ``posix.listdir``, or a stored function reference is left to code review.
"""

from __future__ import annotations

import ast
from collections import Counter
from functools import cache
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "kdive"
_HELPER = Path("providers/shared/directory_listing.py")
_LISTERS = frozenset({"listdir", "scandir"})
_ALLOWLIST = frozenset(
    {
        (Path("jobs/handlers/artifacts/rootfs_reclaim.py"), "_live_writer_holds_a_partial"),
    }
)


def _os_lister_names(tree: ast.Module) -> set[str]:
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "os"
        for alias in node.names
        if alias.name in _LISTERS
    }


def _is_lister(call: ast.Call, imported: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr in _LISTERS and isinstance(func.value, ast.Name) and func.value.id == "os"
    return isinstance(func, ast.Name) and func.id in imported


@cache
def _listing_sites() -> frozenset[tuple[Path, str, int]]:
    sites: set[tuple[Path, str, int]] = set()
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC)
        if relative == _HELPER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = _os_lister_names(tree)
        calls = {
            id(node): node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _is_lister(node, imported)
        }
        enclosed: set[int] = set()
        for function in ast.walk(tree):
            if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                for node in ast.walk(function):
                    if id(node) in calls:
                        enclosed.add(id(node))
                        sites.add((relative, function.name, calls[id(node)].lineno))
        sites.update((relative, "<module>", calls[key].lineno) for key in calls.keys() - enclosed)
    return frozenset(sites)


def test_no_bare_directory_listings() -> None:
    offenders = sorted(
        f"{path}:{line} in {function}"
        for path, function, line in _listing_sites()
        if (path, function) not in _ALLOWLIST
    )
    assert not offenders, (
        "list a directory descriptor with kdive.providers.shared.directory_listing."
        "list_directory_descriptor; for a path listing use Path.iterdir() or add a "
        f"(file, function) allowlist entry with a reason: {offenders}"
    )


def test_each_allowlist_entry_matches_exactly_one_listing() -> None:
    counts = Counter((path, function) for path, function, _line in _listing_sites())
    wrong = {entry: counts[entry] for entry in _ALLOWLIST if counts[entry] != 1}
    assert not wrong, f"each allowlist entry must match exactly one listing call: {wrong}"
