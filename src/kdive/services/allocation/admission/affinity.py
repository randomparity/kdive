"""Resource project-affinity and guest-architecture predicates."""

from __future__ import annotations

from kdive.domain.catalog.resources import Resource


def project_may_place(resource: Resource, project: str) -> bool:
    """Return whether the resource is global, project-owned, or explicitly allowed."""
    if resource.owner_project is None:
        return True
    return project == resource.owner_project or project in resource.affinity_allowlist


def resource_visible_to_projects(resource: Resource, projects: tuple[str, ...]) -> bool:
    """Report whether any of ``projects`` can see or place on ``resource``."""
    if resource.owner_project is None:
        return True
    return any(project_may_place(resource, project) for project in projects)


def resource_supports_arch(resource: Resource, arch: str) -> bool:
    """Return architecture compatibility, failing open when none are advertised (ADR-0362)."""
    guest_arches = resource.capability_view.guest_arches()
    if not guest_arches:
        return True
    return arch in guest_arches
