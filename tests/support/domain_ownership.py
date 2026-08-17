"""Disowning production-rendered domain XML used by transient live tests."""

from __future__ import annotations

import xml.etree.ElementTree as ET


def drop_ownership_metadata(root: ET.Element) -> None:
    """Strip the kdive ownership tag from a transient, non-production domain (#1930).

    ``render_domain_xml`` always stamps ``<metadata><kdive:system>`` with the System id it was
    handed, because every domain it renders in production *is* owned by a live ``systems`` row.
    A live test that renders that production XML for a System existing only in pytest's
    disposable database therefore makes a false ownership claim: a concurrently running
    production reconciler resolves it (the tag is authoritative in
    ``LocalLibvirtDiscovery._owned_entry``), finds no matching row, and ``repair_leaked_domains``
    destroys the domain mid-test.

    Dropping the whole ``<metadata>`` element — kdive renders nothing else inside it — sends
    ``_owned_entry`` down its ``VIR_ERR_NO_DOMAIN_METADATA`` path. That closes only *one* of the
    two ownership signals: ``repair_leaked_domains`` then falls back to
    ``system_id_from_domain_name(domain.name)``, so **the caller must also give the domain a name
    outside the ``kdive-<uuid>`` convention** or it is still resolved as kdive-owned and reaped
    (#1968). Everything else stays production XML: the gdbstub passthrough, the SSH forward, the
    direct-kernel ``<os>``, and the disk are exactly what a real System boots.
    """
    metadata = root.find("metadata")
    if metadata is not None:
        root.remove(metadata)
