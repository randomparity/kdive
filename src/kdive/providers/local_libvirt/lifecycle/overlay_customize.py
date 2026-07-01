"""Provision-time per-System overlay customization (ADR-0289, #963).

An ordered list of customizers `provision()` runs against the per-System overlay **only when it
creates the overlay** (so a retry against a running QEMU never re-mutates a live disk). The first
consumer is the per-System SSH bootstrap key injection; future provision-time mutations append a
customizer here rather than adding parallel one-offs.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory

type OverlayCustomizer = Callable[[str], None]

_VIRT_CUSTOMIZE_TIMEOUT_S = 5 * 60


def inject_authorized_key_argv(overlay_path: str, pubkey_file: str) -> list[str]:
    """Build the ``virt-customize --ssh-inject`` argv writing ``root``'s authorized_keys."""
    return [
        "virt-customize",
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
        executable = shutil.which("virt-customize")
        if executable is None:
            raise CategorizedError(
                "virt-customize is not installed; cannot inject the per-System bootstrap key",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        result = subprocess.run(  # noqa: S603 - fixed argv, kdive-owned paths
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
