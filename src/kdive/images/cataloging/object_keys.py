"""Image object-store key layout — the single source shared by publish, reconcile, stage-volume.

The image tenant's object keys (``images/{provider}[__{owner}]/{name}/{arch}.{suffix}``) are
computed here from plain identity fields so every writer/reader produces byte-identical keys without
a service-layer :class:`~kdive.services.images.publish.PublishRequest`. Kept in the image layer so
the inventory reconcile can import it without the layering inversion a ``kdive.services`` import
would create (ADR-0336).
"""

from __future__ import annotations

from kdive.artifacts import storage as artifact_types
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.catalog.images import ImageVisibility

#: Retention class stamped on every image object (qcow2 and its ``.config`` sibling).
IMAGE_RETENTION_CLASS = "image"


def owner_kind_segment(provider: str, visibility: ImageVisibility, owner: str | None) -> str:
    """The ``owner_kind`` key segment, owner-scoped for a private image.

    A public image keys its provider directly (``{provider}``); a private image folds the owning
    project into the segment (``{provider}__{owner}``) so two projects' private images of the same
    ``(provider, name, arch)`` never collide on one object. The ``__`` separator is illegal in a
    provider/project name, so the segment stays unambiguous and slash-free (``artifact_key`` rejects
    slashes in a component).
    """
    if visibility is ImageVisibility.PRIVATE and owner is not None:
        return f"{provider}__{owner}"
    return provider


def object_write_request(
    provider: str,
    name: str,
    arch: str,
    visibility: ImageVisibility,
    owner: str | None,
    *,
    data: bytes,
    suffix: str,
) -> artifact_types.ArtifactWriteRequest:
    """An image-tenant write request scoped to the given visibility/owner, named by ``suffix``.

    The single source of the image object layout: the qcow2 (``suffix="qcow2"``) and its
    kernel-config sibling (``suffix="config"``) share the same tenant/owner-scoped prefix and
    differ only in the object-name suffix, so a key computed from plain identity fields is
    byte-identical to the key the publish write produced.
    """
    return artifact_types.ArtifactWriteRequest(
        tenant="images",
        owner_kind=owner_kind_segment(provider, visibility, owner),
        owner_id=name,
        name=f"{arch}.{suffix}",
        data=data,
        sensitivity=Sensitivity.REDACTED,
        retention_class=IMAGE_RETENTION_CLASS,
    )


def config_object_key(
    provider: str, name: str, arch: str, visibility: ImageVisibility, owner: str | None
) -> str:
    """The object-store key for an image's ``/boot/config-<ver>`` sibling, from identity fields.

    The single source of the ``.config`` key (ADR-0317/0336): reconcile and ``stage-volume``
    compute it from a catalog row's identity, and get a key byte-identical to the one the publish
    path writes and the fetch path presigns. Staged images are public, so their key omits the owner
    segment: ``images/{provider}/{name}/{arch}.config``.
    """
    return object_write_request(
        provider, name, arch, visibility, owner, data=b"", suffix="config"
    ).key()


def config_write_request(
    provider: str,
    name: str,
    arch: str,
    visibility: ImageVisibility,
    owner: str | None,
    *,
    config: bytes,
) -> artifact_types.ArtifactWriteRequest:
    """The object-store write request for an image's kernel ``.config`` (ADR-0336).

    Carries ``config`` under the same key :func:`config_object_key` computes, so a caller can
    ``store.put_artifact(config_write_request(...))`` and persist ``.key()`` as the row's
    ``kernel_config_key`` — the uploaded object and the stored key are guaranteed to match.
    """
    return object_write_request(
        provider, name, arch, visibility, owner, data=config, suffix="config"
    )
