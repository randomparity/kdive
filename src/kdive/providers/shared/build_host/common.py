"""Shared kernel-build fragment helpers for both libvirt providers (ADR-0096).

The local and remote build planes are independent modules (ADR-0076), but the kdump
config-fragment survival check is pure text logic identical on both. Hoisting it here keeps
the two providers' fragment handling from drifting; the merge/olddefconfig orchestration that
calls these stays provider-local (it threads each provider's typed failure helper).
"""

from __future__ import annotations


def _fragment_symbols(fragment_text: str) -> list[str]:
    return list(_fragment_requests(fragment_text))


def _fragment_requests(fragment_text: str) -> dict[str, str]:
    """Map each ``=y``/``=m`` fragment symbol to its requested value, in file order."""
    requests: dict[str, str] = {}
    for raw in fragment_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if value in ("y", "m"):
            requests[name] = value
    return requests


def _final_config_values(final_config_text: str) -> dict[str, str]:
    """Map each enabled (``=y``/``=m``) symbol in a final ``.config`` to its value.

    The symbol name is the text before the first ``=`` and the value is the trailing
    ``y``/``m`` token, so a (malformed) line whose value itself contains ``=`` still
    yields the bare symbol name.
    """
    values: dict[str, str] = {}
    for raw in final_config_text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#") or not line.endswith(("=y", "=m")):
            continue
        name = line.split("=", 1)[0]
        values[name] = line[-1]
    return values


def _dropped_fragment_symbols(fragment_text: str, final_config_text: str) -> list[str]:
    """Fragment symbols the final ``.config`` failed to honor at the requested strength.

    A symbol is dropped when the final config disables it, omits it, or — for a ``=y``
    request — only builds it as a module. A ``=y`` request demands a built-in symbol
    (e.g. ``qemu_fw_cfg`` must probe at boot to write the VMCOREINFO note before a
    host_dump; #708), so a silent ``=y``→``=m`` downgrade by ``olddefconfig`` is a drop,
    not a survivor. A ``=m`` request is satisfied by either ``=m`` or a stronger ``=y``.
    """
    final = _final_config_values(final_config_text)
    dropped = []
    for sym, requested in _fragment_requests(fragment_text).items():
        actual = final.get(sym)
        if actual is None or (requested == "y" and actual != "y"):
            dropped.append(sym)
    return dropped


__all__ = [
    "_dropped_fragment_symbols",
    "_fragment_symbols",
]
