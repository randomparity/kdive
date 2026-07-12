"""Provider-neutral baseline-kernel classification of a ``/boot`` listing (ADR-0272/0295).

The pure ``/boot`` classifier both the local-libvirt provision selection and the build/stage config
capture (ADR-0336) share. Kept out of any provider package so provider-neutral callers (the
image-plane build capture, ``stage-volume``) can use it without a provider-layer import.
"""

from __future__ import annotations

import os

VMLINUZ_PREFIX = "vmlinuz-"


def baseline_kernel_names(boot_entries: list[str]) -> list[str]:
    """The non-rescue ``vmlinuz-<ver>`` basenames in a ``/boot`` listing — the baseline candidates.

    Accepts full paths or bare basenames (each is reduced to its basename). Non-``vmlinuz`` entries
    and rescue images are excluded. This is the single classifier the fail-closed provision
    selection and the build-time ``boot_kernel_count`` capture use, so the recorded count predicts
    the provision-time selection outcome: exactly one candidate is the only provisionable case
    (ADR-0272/0295).
    """
    names = [os.path.basename(entry) for entry in boot_entries]
    return [n for n in names if n.startswith(VMLINUZ_PREFIX) and "rescue" not in n]
