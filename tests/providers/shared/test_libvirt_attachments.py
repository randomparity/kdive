"""Attachment safety must load independently of concrete providers."""

import subprocess
import sys


def test_attachment_safety_import_does_not_load_concrete_providers() -> None:
    program = """
import sys
from kdive.providers.shared import libvirt_attachments

assert libvirt_attachments.HostStatDeviceIdentity.__module__ == libvirt_attachments.__name__
assert (
    libvirt_attachments.prove_no_foreign_path_references.__module__
    == libvirt_attachments.__name__
)
provider_roots = ("kdive.providers.local_libvirt", "kdive.providers.remote_libvirt")
settings_only = {*provider_roots, *(root + ".settings" for root in provider_roots)}
implementation_modules = {
    name for name in sys.modules
    if name.startswith(provider_roots) and name not in settings_only
}
assert not implementation_modules, implementation_modules
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
