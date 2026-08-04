"""The explicit manifest of setting-bearing module paths (ADR-0087 decision 2).

The registry force-loads each module here and aggregates its ``SETTINGS`` list, so the
full ``KDIVE_*`` set is available regardless of which provider a given process happened
to import. Providers are opt-in and lazily imported, so "whatever happened to import" is
not a complete set; this manifest is.

A new provider adds **one line per setting-bearing module** (not per variable); its
``SETTINGS`` live co-located in the provider package.
"""

from __future__ import annotations

SETTING_MODULES: tuple[str, ...] = (
    "kdive.config.core_settings",
    "kdive.config.cli_settings",
    "kdive.providers.local_libvirt.settings",
    "kdive.providers.fault_inject.settings",
    "kdive.providers.remote_libvirt.settings",
)
