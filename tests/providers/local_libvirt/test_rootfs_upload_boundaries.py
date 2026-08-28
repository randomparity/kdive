"""Ownership guards for the uploaded-rootfs pipeline."""

import importlib.util

from kdive.providers.local_libvirt.lifecycle.rootfs import (
    upload_acquisition,
    upload_publication,
    upload_staging,
)


def test_upload_pipeline_has_distinct_ownership_boundaries() -> None:
    assert upload_acquisition.fetch_uploaded_rootfs.__module__ == upload_acquisition.__name__
    assert upload_staging.stage_uploaded_rootfs.__module__ == upload_staging.__name__
    assert upload_publication._durable_replace.__module__ == upload_publication.__name__


def test_upload_pipeline_has_no_legacy_monolith() -> None:
    legacy = "kdive.providers.local_libvirt.lifecycle.rootfs.rootfs_upload_fetch"
    assert importlib.util.find_spec(legacy) is None
