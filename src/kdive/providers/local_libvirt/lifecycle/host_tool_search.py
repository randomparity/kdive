"""Explicit host-tool resolution shared by local-libvirt provider call sites.

``virsh``, ``qemu-img``, and ``virt-customize`` are invoked from under the same PATH-less
worker gate that motivated ``guest_kernel_writer._resolve_depmod`` (#2300): the gate execs the
worker from an environment allowlist that omits ``PATH``, so a bare ``shutil.which`` call
silently falls back to ``os.defpath`` (``:/bin:/usr/bin``) instead of failing loudly when a host
lays tools out differently. Resolution here never consults ``PATH``.

Unlike ``depmod`` (an sbin tool needing the four-directory
``module_staging_tools.DEPMOD_SEARCH_DIRS``), virsh,
qemu-img, and virt-customize all ship under ``/usr/bin`` on the distributions in use, so this is
the narrower two-directory subset. ``/usr/local/{sbin,bin}`` stay excluded for the same reason
``DEPMOD_SEARCH_DIRS`` excludes them: part of the Debian family makes ``/usr/local``
group-writable by default, and a binary planted there would run as the worker slot account,
inheriting its authority over guest overlays.
"""

from __future__ import annotations

import os
import shutil

PROVIDER_TOOL_SEARCH_DIRS = ("/usr/bin", "/bin")
PROVIDER_TOOL_SEARCH_PATH = os.pathsep.join(PROVIDER_TOOL_SEARCH_DIRS)


def resolve_provider_tool(name: str) -> str | None:
    """Resolve ``name`` to an absolute path against the provider-tool search dirs, never PATH."""
    return shutil.which(name, path=PROVIDER_TOOL_SEARCH_PATH)
