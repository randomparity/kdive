"""Per-project resource affinity predicate (ADR-0112, M2.6 Task 4.2).

A project may place only on a **global** resource (``owner_project IS NULL``) or one it
owns (``owner_project == project``) or one that lists the project in its
``affinity_allowlist``. Every pre-existing discovered + config-declared resource is global
(Phase-1 backfill leaves ``owner_project`` NULL with an empty allowlist), so the predicate
is a strict no-op for current behavior — no allocation that works today regresses.

The same predicate gates both layers: it filters the any-available candidate set in
``placement.py`` (so a disallowed scoped instance is never selected and an any-available
request falls through to a legal global one) and backstops the explicit ``resource_id``
path in ``admission.py``.
"""

from __future__ import annotations

from kdive.domain.catalog.resources import Resource


def project_may_place(resource: Resource, project: str) -> bool:
    """Report whether ``project`` is allowed to place on ``resource``.

    A global resource (``owner_project is None``) admits any project; a scoped resource
    admits only its owner or a project on its ``affinity_allowlist``.

    Args:
        resource: The candidate resource host.
        project: The placing project.

    Returns:
        ``True`` if the affinity predicate permits placement, else ``False``.
    """
    if resource.owner_project is None:
        return True
    return project == resource.owner_project or project in resource.affinity_allowlist


def resource_visible_to_projects(resource: Resource, projects: tuple[str, ...]) -> bool:
    """Report whether any of ``projects`` can see or place on ``resource``."""
    if resource.owner_project is None:
        return True
    return any(project_may_place(resource, project) for project in projects)


def resource_supports_arch(resource: Resource, arch: str) -> bool:
    """Report whether ``resource`` can boot guest architecture ``arch`` (ADR-0362).

    Fail-open, mirroring the accel-resolution rule (ADR-0339): a resource that advertises a
    non-empty ``guest_arches`` set is admitted only when it contains ``arch``; a resource that
    advertises **no** ``guest_arches`` (remote-libvirt, fault-inject, a host not re-discovered
    since ADR-0338) is admitted, because it cannot prove it does not support the arch and must
    behave exactly as before ADR-0362. This routes a ``ppc64le`` request to a ``ppc64le``-capable
    host and falls through a host that advertises only ``x86_64``.

    Args:
        resource: The candidate resource host.
        arch: The requested guest architecture.

    Returns:
        ``True`` if the resource may place the requested arch, else ``False``.
    """
    guest_arches = resource.capability_view.guest_arches()
    if not guest_arches:
        return True
    return arch in guest_arches
