"""Regression guard: domain-model digest stability (ADR-0269, ADR-0038).

ADR-0269 narrowed the AGENT-FACING surfaces without touching the domain models
(ProvisioningProfile / ProviderSection). This test pins the SHA-256 digest of a stored
remote-libvirt profile so a future change that accidentally reaches into the domain model
fails loudly here before it silently breaks reprovision dedup (ADR-0038 §3).
"""

from __future__ import annotations

from kdive.profiles.provisioning import ProvisioningProfile, profile_digest

_REMOTE_PROFILE = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 2,
    "memory_mb": 2048,
    "disk_gb": 20,
    "boot_method": "disk-image",
    "provider": {"remote-libvirt": {"base_image_volume": "vol-1"}},
}

# Pin the digest of a stored remote-libvirt profile. If this value changes, the boundary
# projection has leaked into the domain model and broken reprovision dedup (ADR-0038).
_EXPECTED_DIGEST = (
    "f6a183376d8f78d50c128e161271bcc8561f64e18b0991708ccd9196591a2e5b"  # pragma: allowlist secret
)


def test_remote_profile_parses_and_digest_is_stable() -> None:
    """The domain model is untouched by ADR-0269; its digest must stay byte-identical."""
    parsed = ProvisioningProfile.parse(_REMOTE_PROFILE)
    digest = profile_digest(parsed)
    assert digest == _EXPECTED_DIGEST
