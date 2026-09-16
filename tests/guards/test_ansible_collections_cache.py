"""Pin offline Ansible lint and its requirements-keyed CI collection cache (#2225).

Also pins the single definition both the developer path and CI install from (#2499). CI
used to carry the `ansible-galaxy` invocation inline, so nothing in `just setup` installed
the collections and a clean host could not finish it: `install-hooks` runs `prek run -a`,
whose `lint-ansible` hook then failed with `syntax-check[unknown-module]`. Moving the
command into a recipe both callers invoke is what keeps the two paths from drifting, so
these tests assert the delegation itself — that the workflow names no `ansible-galaxy`
anywhere — not just that both happen to install the same file today.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_CI = _ROOT / ".github" / "workflows" / "ci.yml"
_JUSTFILE = _ROOT / "justfile"
_LINT_CONFIG = _ROOT / "deploy" / "ansible" / ".ansible-lint"
_REQUIREMENTS = "deploy/ansible/requirements.yml"
_SOURCE_LOCK = "deploy/ansible/requirements-ci.lock.yml"
_COLLECTIONS_PATH = "~/.ansible/collections"
_INSTALL_RECIPE = "install-ansible-collections"

_JUST = shutil.which("just")
#: CI drives every gate through `just`, so this never skips there; the guard is for a
#: `just`-less direct-pytest invocation.
_needs_just = pytest.mark.skipif(_JUST is None, reason="just is required to expand a recipe")

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


def _expand(recipe: str) -> str:
    """Return the command lines ``just`` would run for ``recipe``, without running them."""
    assert _JUST is not None
    result = subprocess.run(
        [
            _JUST,
            "--justfile",
            str(_JUSTFILE),
            "--working-directory",
            str(_ROOT),
            "--dry-run",
            recipe,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    # --dry-run echoes the expanded lines to stderr, leaving stdout for the recipe's output.
    return result.stderr


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


@_needs_just
def test_the_install_recipe_installs_the_source_lock_and_nothing_else() -> None:
    command = _expand(_INSTALL_RECIPE)

    assert "ansible-galaxy collection install" in command
    assert f"--requirements-file {_SOURCE_LOCK}" in command
    assert f"--collections-path {_COLLECTIONS_PATH}" in command
    # --no-deps keeps a transitive dependency from reaching past the pinned set, and
    # --no-cache keeps a previously resolved artifact from standing in for the pinned commit.
    assert "--no-deps" in command
    assert "--no-cache" in command
    # The install resolves from the lock alone. requirements.yml is the set CI checks the
    # result against, so naming it here would let the install satisfy its own check.
    assert _REQUIREMENTS not in command


@_needs_just
def test_setup_installs_the_collections_before_running_the_hooks() -> None:
    # The developer-path half of #2499. `install-hooks` runs `prek run -a`, whose lint-ansible
    # hook syntax-checks the playbooks: without the collections already installed it fails with
    # syntax-check[unknown-module] and `just setup` cannot finish on a clean host.
    command = _expand("setup")

    assert "ansible-galaxy collection install" in command, (
        "`just setup` must install the pinned collections; nothing else in the developer "
        "path does, so a clean host cannot complete it (#2499)"
    )
    assert command.index("ansible-galaxy collection install") < command.index("prek run -a")
    # It needs the synced venv: both the install and the verifier run under `uv run`.
    assert command.index("uv sync --locked") < command.index("ansible-galaxy collection install")


def test_ci_installs_source_lock_only_on_cache_miss_with_galaxy_dead() -> None:
    install = _step("Install source-locked Ansible collections")

    assert install["if"] == "steps.ansible-collections-cache.outputs.cache-hit != 'true'"
    assert install["env"]["ANSIBLE_GALAXY_SERVER"] == "http://127.0.0.1:9"
    assert install["run"].strip() == f"just {_INSTALL_RECIPE}"


def test_ci_never_spells_the_install_command_out_itself() -> None:
    # The drift acceptance criterion of #2499. CI carrying its own copy of the invocation is
    # exactly how the developer path went uncovered: CI stayed green from a command `just
    # setup` never ran. Asserted over the whole workflow, not the one step, because a second
    # copy anywhere in it reintroduces the same divergence.
    assert "ansible-galaxy" not in _CI.read_text(encoding="utf-8"), (
        "the workflow must invoke the justfile recipe rather than restate the install "
        "command, so the developer path and CI cannot resolve different pinned sets"
    )


def test_collection_restore_install_and_verification_precede_lint() -> None:
    names = [step.get("name") for step in _ci_steps()]
    assert (
        names.index("Restore pinned Ansible collections")
        < names.index("Install source-locked Ansible collections")
        < names.index("Verify pinned Ansible collections")
        < names.index("Lint Ansible")
    )

    # Unconditional, unlike the install: a restored cache is the path the install step skips,
    # and it is the one a drifted lock would otherwise reach lint through.
    verify = _step("Verify pinned Ansible collections")
    assert "if" not in verify
    assert "scripts/guards/check_ansible_collections.py" in verify["run"]
    assert _REQUIREMENTS in verify["run"]
    assert _COLLECTIONS_PATH in verify["run"]
