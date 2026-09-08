"""Provision-time per-System overlay customization (ADR-0289, #963).

An ordered list of customizers `provision()` runs against the per-System overlay **only when it
creates the overlay** (so a retry against a running QEMU never re-mutates a live disk). The first
consumer is the per-System SSH bootstrap key injection; future provision-time mutations append a
customizer here rather than adding parallel one-offs.
"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 - virt-customize uses fixed argv, no shell  # nosec B404
import tempfile
from collections.abc import Callable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.host_tool_search import (
    PROVIDER_TOOL_SEARCH_PATH,
    resolve_provider_tool,
)

type OverlayCustomizer = Callable[[str], None]

_VIRT_CUSTOMIZE_TIMEOUT_S = 5 * 60
_VIRT_CUSTOMIZE = "virt-customize"


def inject_authorized_key_argv(overlay_path: str, pubkey_file: str) -> list[str]:
    """Build the ``virt-customize --ssh-inject`` argv writing ``root``'s authorized_keys."""
    return [
        _VIRT_CUSTOMIZE,
        "-a",
        overlay_path,
        "--ssh-inject",
        f"root:file:{pubkey_file}",
    ]


def _real_inject_authorized_key(  # pragma: no cover - live_vm
    overlay_path: str, pubkey: str
) -> None:
    """Inject ``pubkey`` into the overlay's ``/root/.ssh/authorized_keys`` via libguestfs."""
    scratch = Path(tempfile.mkdtemp(prefix="kdive-inject-"))
    try:
        pub = scratch / "key.pub"
        pub.write_text(pubkey + "\n", encoding="utf-8")
        executable = resolve_provider_tool(_VIRT_CUSTOMIZE)
        if executable is None:
            raise CategorizedError(
                "virt-customize is not installed; cannot inject the per-System bootstrap key; "
                f"not found in any of {PROVIDER_TOOL_SEARCH_PATH}",
                category=ErrorCategory.MISSING_DEPENDENCY,
                details={"searched": PROVIDER_TOOL_SEARCH_PATH},
            )
        result = subprocess.run(  # noqa: S603 - fixed argv, kdive-owned paths  # nosec B603
            [executable, *inject_authorized_key_argv(overlay_path, str(pub))[1:]],
            capture_output=True,
            text=True,
            check=False,
            timeout=_VIRT_CUSTOMIZE_TIMEOUT_S,
        )
        if result.returncode != 0:
            raise CategorizedError(
                "virt-customize failed to inject the per-System bootstrap key",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"stderr": result.stderr[-2000:]},
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def authorized_key_customizer(pubkey: str) -> OverlayCustomizer:
    """Return an overlay customizer that injects ``pubkey`` into ``root``'s authorized_keys."""
    return lambda overlay_path: _real_inject_authorized_key(overlay_path, pubkey)
