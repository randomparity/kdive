"""Compatibility facade for installed-package admin helpers.

The concrete one-shot command implementations live in focused admin modules:
fixtures, migrations, build-config seeding, and project onboarding/verification.
"""

from __future__ import annotations

from kdive.admin.build_configs import seed_build_configs_step
from kdive.admin.fixtures import default_fixture_files, install_fixtures
from kdive.admin.migrations import migrate
from kdive.admin.projects import (
    ProjectFundingStatus,
    format_verify_result,
    redact_database_url,
    register_discovered_resources,
    seed_project,
    seed_project_statements,
    verify_project,
)

__all__ = [
    "ProjectFundingStatus",
    "default_fixture_files",
    "format_verify_result",
    "install_fixtures",
    "migrate",
    "redact_database_url",
    "register_discovered_resources",
    "seed_build_configs_step",
    "seed_project",
    "seed_project_statements",
    "verify_project",
]
