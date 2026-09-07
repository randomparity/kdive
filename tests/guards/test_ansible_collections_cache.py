"""Pin offline Ansible lint and its requirements-keyed CI collection cache (#2225)."""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_LINT_CONFIG = _ROOT / "deploy" / "ansible" / ".ansible-lint"
_REQUIREMENTS = "deploy/ansible/requirements.yml"


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
    assert f"hashFiles('{_REQUIREMENTS}')" in cache_inputs["key"]
    assert "restore-keys" not in cache_inputs


def test_ci_installs_exact_manifest_only_on_cache_miss_without_response_cache() -> None:
    install = _step("Install pinned Ansible collections")
    command = install["run"]

    assert install["if"] == "steps.ansible-collections-cache.outputs.cache-hit != 'true'"
    assert "ansible-galaxy collection install" in command
    assert f"--requirements-file {_REQUIREMENTS}" in command
    assert "--no-deps" in command
    assert "--no-cache" in command


def test_collection_restore_and_install_precede_lint() -> None:
    names = [step.get("name") for step in _ci_steps()]
    assert (
        names.index("Restore pinned Ansible collections")
        < names.index("Install pinned Ansible collections")
        < names.index("Lint Ansible")
    )
