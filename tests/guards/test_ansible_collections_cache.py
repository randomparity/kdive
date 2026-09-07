"""Pin offline Ansible lint and its requirements-keyed CI collection cache (#2225)."""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_LINT_CONFIG = _ROOT / "deploy" / "ansible" / ".ansible-lint"
_REQUIREMENTS = "deploy/ansible/requirements.yml"
_SOURCE_LOCK = "deploy/ansible/requirements-ci.lock.yml"

_EXPECTED_SOURCES = {
    "https://github.com/ansible-collections/ansible.posix.git": (
        "d9af430396cd12fce473d29182305b1617125da7"  # pragma: allowlist secret
    ),
    "https://github.com/ansible-collections/community.crypto.git": (
        "39f8c847ebbfee966b00c9d3c9a22cb819df8316"  # pragma: allowlist secret
    ),
    "https://github.com/ansible-collections/community.general.git": (
        "73be10786b529acac83267edaaacda2cb4f627c9"  # pragma: allowlist secret
    ),
    "https://github.com/ansible-collections/community.libvirt.git": (
        "25427a148e5b61b71a97bcb9904ff904d5df9d0d"  # pragma: allowlist secret
    ),
}


def _ci_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    return workflow["jobs"]["lint-type-test"]["steps"]


def _step(name: str) -> dict[str, Any]:
    return next(step for step in _ci_steps() if step.get("name") == name)


def test_ansible_lint_is_offline() -> None:
    config = yaml.safe_load(_LINT_CONFIG.read_text(encoding="utf-8"))
    assert config["offline"] is True


def test_ci_restores_collections_by_exact_requirements_hash() -> None:
    cache = _step("Restore pinned Ansible collections")
    cache_inputs = cache["with"]

    assert cache["id"] == "ansible-collections-cache"
    assert cache["uses"].startswith("actions/cache@")
    assert cache_inputs["path"] == "~/.ansible/collections"
    assert cache_inputs["key"] == (
        "${{ runner.os }}-ansible-collections-"
        f"${{{{ hashFiles('{_REQUIREMENTS}', '{_SOURCE_LOCK}') }}}}"
    )
    assert "restore-keys" not in cache_inputs


def test_source_lock_pins_the_expected_github_commits() -> None:
    lock = yaml.safe_load((_ROOT / _SOURCE_LOCK).read_text(encoding="utf-8"))
    assert all(entry["type"] == "git" for entry in lock["collections"])
    actual = {entry["name"]: entry["version"] for entry in lock["collections"]}

    assert actual == _EXPECTED_SOURCES


def test_ci_installs_source_lock_only_on_cache_miss_with_galaxy_dead() -> None:
    install = _step("Install source-locked Ansible collections")
    command = install["run"]

    assert install["if"] == "steps.ansible-collections-cache.outputs.cache-hit != 'true'"
    assert install["env"]["ANSIBLE_GALAXY_SERVER"] == "http://127.0.0.1:9"
    assert "ansible-galaxy collection install" in command
    assert f"--requirements-file {_SOURCE_LOCK}" in command
    assert _REQUIREMENTS not in command
    assert "--no-deps" in command
    assert "--no-cache" in command


def test_collection_restore_install_and_verification_precede_lint() -> None:
    names = [step.get("name") for step in _ci_steps()]
    assert (
        names.index("Restore pinned Ansible collections")
        < names.index("Install source-locked Ansible collections")
        < names.index("Verify pinned Ansible collections")
        < names.index("Lint Ansible")
    )

    verify = _step("Verify pinned Ansible collections")
    assert "scripts/guards/check_ansible_collections.py" in verify["run"]
    assert _REQUIREMENTS in verify["run"]
    assert "~/.ansible/collections" in verify["run"]
