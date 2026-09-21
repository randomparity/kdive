"""Contract checks for ordinary integration-test seed documents."""

from kdive.profiles.build import BuildProfile
from tests.integration._seed import BUILD_PROFILE


def test_default_build_profile_seed_is_valid_external_upload_document() -> None:
    """The database-seed default remains acceptable at the production parse boundary."""
    assert BuildProfile.parse(BUILD_PROFILE).arch == "x86_64"
