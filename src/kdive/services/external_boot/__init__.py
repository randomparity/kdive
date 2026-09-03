"""External-boot admission, shared by every surface that mutates a bootable System."""

from kdive.services.external_boot.admission import (
    ExternalBootDenied,
    ExternalBootOperation,
    check_external_boot_admission,
)

__all__ = ["ExternalBootDenied", "ExternalBootOperation", "check_external_boot_admission"]
