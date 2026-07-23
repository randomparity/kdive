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

# Pin the digest of a stored remote-libvirt profile. If this value changes without a
# deliberate domain-model change, the boundary projection has leaked into the domain model
# and broken reprovision dedup (ADR-0038). ADR-0426 intentionally added the
# ``remote-libvirt.host_dump`` opt-in (default ``False``, serialized because ``dump_profile``
# uses ``exclude_none``), which legitimately shifts this digest — the pinned value is updated
# to match.
_EXPECTED_DIGEST = (
    "0871a16c79a76a976e8d0c50f6dc7eed185bb751afd799cccf556dfbcb78a4cf"  # pragma: allowlist secret
)


def test_remote_profile_parses_and_digest_is_stable() -> None:
    """The domain model is untouched by ADR-0269; its digest must stay byte-identical."""
    parsed = ProvisioningProfile.parse(_REMOTE_PROFILE)
    digest = profile_digest(parsed)
    assert digest == _EXPECTED_DIGEST
