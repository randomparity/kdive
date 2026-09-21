"""Local push wiring and its checkout semantics (ADR-0670)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_hook_stage_contract() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]
    push = [hook for hook in hooks if hook["id"] == "pre-push-ci"]
    assert len(push) == 1
    assert config["default_stages"] == ["pre-commit"]
    assert push[0]["entry"] == "just ci"
    assert push[0]["stages"] == ["pre-push"]
    assert push[0]["always_run"] is True
    assert push[0]["pass_filenames"] is False
    assert push[0]["language"] == "system"
    for hook in hooks:
        if hook["id"] != "pre-push-ci":
            assert hook.get("stages", config["default_stages"]) == ["pre-commit"]


def run(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, env=env, text=True, capture_output=True, check=False)


def checked(root: Path, env: dict[str, str], *args: str) -> str:
    result = run(root, env, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    for tool in ("git", "just", "prek"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is required for functional hook proof")
    actual_just = shutil.which("just")
    assert actual_just is not None
    root = tmp_path / "repo"
    root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = tmp_path / "probe"
    stub = bin_dir / "just"
    stub.write_text(
        '#!/bin/sh\n[ "$#" = 1 ] && [ "$1" = ci ] || exit 91\n'
        'cat payload > "$HOOK_PROBE"\nexit "${HOOK_EXIT:-0}"\n'
    )
    stub.chmod(0o755)
    env = dict(
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        PREK_HOME=str(tmp_path / "prek"),
        HOOK_PROBE=str(probe),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test",
        GIT_COMMITTER_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    checked(root, env, "git", "init", "-b", "test")
    checked(root, env, "git", "init", "--bare", str(tmp_path / "remote.git"))
    checked(root, env, "git", "remote", "add", "origin", str(tmp_path / "remote.git"))
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    push = [
        hook for repo in config["repos"] for hook in repo["hooks"] if hook["id"] == "pre-push-ci"
    ]
    commit = {
        "id": "commit-check",
        "name": "commit check",
        "entry": "git diff --check",
        "language": "system",
        "stages": ["pre-commit"],
        "pass_filenames": False,
    }
    config["repos"] = [{"repo": "local", "hooks": [commit, *push]}]
    (root / ".pre-commit-config.yaml").write_text(yaml.safe_dump(config))
    shutil.copyfile(ROOT / "justfile", root / "justfile")
    (root / "payload").write_text("initial")
    checked(root, env, "git", "add", ".")
    checked(root, env, "git", "commit", "-m", "initial")
    checked(root, env, actual_just, "install-hooks")
    assert (root / ".git/hooks/pre-commit").is_file()
    assert (root / ".git/hooks/pre-push").is_file()
    assert not probe.exists(), "installation must not run the push gate"
    (root / "payload").write_text("checkout")
    checked(root, env, "git", "add", "payload")
    checked(root, env, "git", "commit", "-m", "checkout")
    assert not probe.exists(), "commit must not run the push gate"
    return root, env, probe


@pytest.mark.parametrize("exit_code", [0, 7])
def test_push_exit(repository: tuple[Path, dict[str, str], Path], exit_code: int) -> None:
    root, env, probe = repository
    env["HOOK_EXIT"] = str(exit_code)
    result = run(root, env, "git", "push", "origin", "HEAD:refs/heads/check")
    assert (result.returncode == 0) == (exit_code == 0), result.stderr
    assert probe.read_text() == "checkout"
    refs = checked(root, env, "git", "ls-remote", "origin", "refs/heads/check")
    assert bool(refs) == (exit_code == 0)


def test_push_checks_checkout(repository: tuple[Path, dict[str, str], Path]) -> None:
    root, env, probe = repository
    checked(root, env, "git", "push", "origin", "HEAD^:refs/heads/older")
    assert probe.read_text() == "checkout"
    assert checked(root, env, "git", "show", "HEAD^:payload") == "initial"


def test_empty_commit_and_delete(repository: tuple[Path, dict[str, str], Path]) -> None:
    root, env, probe = repository
    checked(root, env, "git", "push", "origin", "HEAD:refs/heads/check")
    probe.unlink()
    checked(root, env, "git", "commit", "--allow-empty", "-m", "empty")
    assert not probe.exists()
    checked(root, env, "git", "push", "origin", "HEAD:refs/heads/check")
    assert probe.read_text() == "checkout"
    probe.unlink()
    checked(root, env, "git", "push", "origin", ":refs/heads/check")
    assert not probe.exists()
