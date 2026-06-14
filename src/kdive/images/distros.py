"""Distro → virt-builder base-template resolution (the rootfs-build extensibility seam).

``build-fs`` builds a rootfs by customizing a ``virt-builder`` base template. The mapping from
an operator-facing ``--distro`` to that template id lives here, in one place, so both the CLI
(which records ``virt-builder:<template>`` as provenance) and the build plane (which passes the
template to ``virt-builder``) agree. Only ``fedora`` is implemented today; ``distro`` is the seam
for future base OSes (other distros, or a minimal from-scratch ``"bare"`` rootfs).
"""

from __future__ import annotations

SUPPORTED_DISTROS: tuple[str, ...] = ("fedora",)


def resolve_base_template(distro: str, releasever: str) -> str:
    """Return the ``virt-builder`` base-template id for ``distro``/``releasever``.

    Args:
        distro: The base-OS family (only ``"fedora"`` is implemented).
        releasever: The base-OS release the image is built from (e.g. ``"43"``).

    Returns:
        The ``virt-builder`` template id (e.g. ``"fedora-43"``).

    Raises:
        NotImplementedError: ``distro`` is not one of :data:`SUPPORTED_DISTROS`.
    """
    if distro == "fedora":
        return f"fedora-{releasever}"
    raise NotImplementedError(
        f"--distro {distro!r} is not implemented; supported distros: {', '.join(SUPPORTED_DISTROS)}"
    )
