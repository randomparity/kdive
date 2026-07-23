"""Offline shell completion over the merged curated + generated verb surface (ADR-0424).

Three layers of proof:

- The static walk (:func:`build_completion_tree`) reaches the root, curated verbs, generated
  verbs, and per-verb flags, and keys the root under the ``/`` sentinel bash needs.
- The emitted subcommand resolves with **no token and no server** — it is dispatched before any
  ``Session`` is built, so a ``Session`` constructor that raises does not stop it.
- The generated bash and zsh scripts, sourced in a real shell, resolve groups → verbs → flags and
  degrade gracefully past a positional argument value.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from kdive.cli.__main__ import build_parser, main
from kdive.cli.completion import _ROOT, _ROOT_KEY, build_completion_tree, render_completion


def _tree() -> dict[str, list[str]]:
    return build_completion_tree(build_parser())


def test_tree_root_lists_subcommands_and_top_flag() -> None:
    root = _tree()[_ROOT]
    assert "--json" in root
    assert {"login", "tool", "doctor", "completion"} <= set(root)
    # Groups from the merged surface (curated + generated) appear at the root.
    assert {"resources", "allocations", "images"} <= set(root)


def test_tree_curated_verb_flags() -> None:
    tree = _tree()
    assert "list" in tree["resources"]
    assert set(tree["resources list"]) == {"--help", "--json", "--kind"}


def test_tree_generated_verb_reachable_with_its_flags() -> None:
    tree = _tree()
    # A schema-generated verb (no curated override) is reachable at its path with its flags.
    assert "estimate" in tree["accounting"]
    estimate = tree["accounting estimate"]
    assert "--json" in estimate
    assert "--request-json" in estimate  # the #1449 non-scalar escape flag


def test_tree_only_long_flags() -> None:
    for tokens in _tree().values():
        assert all(not (t.startswith("-") and not t.startswith("--")) for t in tokens)


def test_render_rejects_unknown_shell_is_argparse_gated() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["completion", "fish"])


def test_bash_script_uses_root_sentinel_key() -> None:
    script = render_completion("bash")
    assert f'["{_ROOT_KEY}"]=' in script
    assert "complete -F _kdivectl kdivectl" in script
    assert '[""]=' not in script  # bash rejects an empty subscript


def test_zsh_script_is_a_compdef() -> None:
    script = render_completion("zsh")
    assert script.startswith("#compdef kdivectl")
    assert "compadd -a candidates" in script
    assert "compdef _kdivectl kdivectl" in script  # registers when sourced, not autoloaded


def test_completion_dispatches_offline_without_a_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `completion` verb prints its script without ever building a `Session` (no token)."""

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("completion must not build a Session")

    monkeypatch.setattr("kdive.cli.transport.Session.from_env", _boom)
    monkeypatch.delenv("KDIVE_TOKEN", raising=False)
    monkeypatch.delenv("KDIVE_SERVER_URL", raising=False)

    code = main(["completion", "bash"])

    assert code == 0
    assert "complete -F _kdivectl kdivectl" in capsys.readouterr().out


def _bash_complete(script: str, words: list[str]) -> set[str]:
    prog = (
        f'source "{script}"\n'
        'COMP_WORDS=("$@")\n'
        "COMP_CWORD=$(( ${#COMP_WORDS[@]} - 1 ))\n"
        "_kdivectl\n"
        'printf "%s\\n" "${COMPREPLY[@]}"\n'
    )
    out = subprocess.run(
        ["bash", "--norc", "-c", prog, "_", *words],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(out.stdout.split())


def _zsh_complete(script: str, words: list[str]) -> set[str]:
    # Source in the sourced (not autoloaded) mode: the script registers via `compdef` without
    # self-executing, so only the explicit `_kdivectl` call below emits candidates.
    prog = (
        "autoload -Uz compinit; compinit -u -D >/dev/null 2>&1\n"
        f'source "{script}"\n'
        "compadd() { [[ $1 == -a ]] && print -rl -- ${(P)2}; }\n"
        'words=("$@")\n'
        "CURRENT=$#\n"
        "_kdivectl\n"
    )
    out = subprocess.run(
        ["zsh", "-f", "-c", prog, "_", *words],
        capture_output=True,
        text=True,
        check=True,
    )
    return set(out.stdout.split())


@pytest.fixture
def bash_script(tmp_path) -> str:
    path = tmp_path / "kdivectl.bash"
    path.write_text(render_completion("bash"), encoding="utf-8")
    return str(path)


@pytest.fixture
def zsh_script(tmp_path) -> str:
    path = tmp_path / "_kdivectl.zsh"
    path.write_text(render_completion("zsh"), encoding="utf-8")
    return str(path)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_bash_script_resolves_groups_verbs_flags(bash_script: str) -> None:
    assert {"resources", "login", "completion"} <= _bash_complete(bash_script, ["kdivectl", ""])
    assert "list" in _bash_complete(bash_script, ["kdivectl", "resources", ""])
    assert _bash_complete(bash_script, ["kdivectl", "resources", "list", "--"]) == {
        "--help",
        "--json",
        "--kind",
    }
    # A positional argument value leaves the path unchanged, so the verb's flags still complete.
    assert "--json" in _bash_complete(
        bash_script, ["kdivectl", "resources", "describe", "res-1", "--"]
    )


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_zsh_script_resolves_groups_verbs_flags(zsh_script: str) -> None:
    assert {"resources", "login", "completion"} <= _zsh_complete(zsh_script, ["kdivectl", ""])
    assert "list" in _zsh_complete(zsh_script, ["kdivectl", "resources", ""])
    assert _zsh_complete(zsh_script, ["kdivectl", "resources", "list", ""]) == {
        "--help",
        "--json",
        "--kind",
    }
    assert "--json" in _zsh_complete(zsh_script, ["kdivectl", "resources", "describe", "res-1", ""])
