# tests/scripts/test_generated_doc_gates_working_tree.py
"""The generated-artifact gates must judge the working tree, not whatever kdive is importable.

``uv sync`` installs the project editable, and the editable install's ``kdive.pth``
merely *appends* ``src/`` to ``sys.path`` — after ambient ``PYTHONPATH`` and
site-packages. A stale ``kdive`` earlier on ``sys.path`` therefore shadows the
working tree. Two realistic paths reach that state:

- an exported ``PYTHONPATH`` pointing at another checkout (``uv run`` passes the
  ambient value through untouched), or
- a non-editable ``kdive`` left in the venv while ``UV_NO_SYNC=1`` suppresses
  ``uv run``'s auto-repair (the workflow ADR-0229 sanctions for ``just mutate``).

Reproduced on #1987: with either shadow in place, ``gen_config_reference`` rendered
the stale registry, the render matched the stale committed ``config.md``, and
``just config-docs-check`` exited 0 on a tree CI rejected at the same commit.

The recipes now prepend the working tree's ``src/`` to ``PYTHONPATH``. Each test
here rebuilds the shadow in a tmp directory and drives the real recipe through
``just``, asserting the gate's verdict is unchanged by the shadow's presence. The
shadow is a copy of the working tree with one value altered, so a stale render can
no longer agree with the committed artifact: before the pin, the poisoned gates
went red (or crashed on the poisoned import); with the pin they stay green. The
data-level case mirrors the issue's divergence — a registry the doc generator
reads that no longer matches the committed doc — inverted so the repo files stay
untouched: the shadow *lost* the last external variable instead of the working
tree *gaining* one, which flips the verdict identically.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _ROOT / "justfile"
_JUST = shutil.which("just")

# The whole repo is driven through `just` (CI runs `just lint` / `just type` / `just test`), so
# this gate does not fire in CI; it keeps a `just`-less direct-pytest invocation from erroring.
pytestmark = pytest.mark.skipif(_JUST is None, reason="just is required to drive a justfile recipe")

_SHADOW_NOTE = "stale shadow kdive must never be imported by the doc gates (#1987)"

# Every generated-artifact gate named by #1987, plus the two sibling gates whose
# generators also import kdive (rbac-matrix-check, cli-verbs-check) and each pinned
# gate's mutating counterpart where one exists: a stale render that passes a check
# would also *write* a stale artifact.
_GATES = [
    "config-docs-check",
    "docs-check",
    "resources-docs-check",
    "doc-constants-check",
    "env-docs-check",
    "mcp-spec-check",
    "rbac-matrix-check",
    "cli-verbs-check",
]


def _shadow_kdive(tmp_path: Path, mutate: Callable[[Path], None]) -> Path:
    """Copy the working tree's ``src/kdive`` into ``tmp_path`` and apply ``mutate`` to the copy."""
    shadow_root = tmp_path / "shadow"
    shutil.copytree(
        _ROOT / "src" / "kdive",
        shadow_root / "kdive",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    mutate(shadow_root / "kdive")
    return shadow_root


def _run_gate(recipe: str, pythonpath: str) -> subprocess.CompletedProcess[str]:
    """Drive the real ``recipe`` with ``pythonpath`` as the ambient ``PYTHONPATH``."""
    assert _JUST is not None
    env = dict(os.environ)
    env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        [_JUST, "--justfile", str(_JUSTFILE), "--working-directory", str(_ROOT), recipe],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.mark.parametrize("recipe", _GATES)
def test_gates_ignore_a_stale_kdive_shadow(tmp_path: Path, recipe: str) -> None:
    """A kdive earlier on sys.path must not feed the gate: the pin keeps the verdict green."""

    def poison(pkg: Path) -> None:
        init = pkg / "__init__.py"
        poison_line = f"raise ImportError({_SHADOW_NOTE!r})\n"
        init.write_text(init.read_text(encoding="utf-8") + poison_line, encoding="utf-8")

    shadow = _shadow_kdive(tmp_path, poison)
    result = _run_gate(recipe, pythonpath=str(shadow))
    assert result.returncode == 0, (
        f"{recipe} reached for a stale kdive instead of the working tree: a gate that imports "
        f"whatever is first on sys.path flips verdict on an unchanged tree ({_SHADOW_NOTE}); "
        f"{result.stderr}"
    )


def test_config_docs_check_judges_the_working_tree_data(tmp_path: Path) -> None:
    """A registry divergence must not flip the verdict when a stale kdive is first on sys.path.

    The shadow drops the last ``EXTERNAL_ENV_VARS`` entry, so a generator reading the
    shadow renders one row fewer than the committed ``config.md`` and the check goes
    red on inputs whose honest verdict is green — the verdict instability #1987
    observed. The pinned recipe reads the working tree and stays green.
    """

    def drop_last_external_var(pkg: Path) -> None:
        ext = pkg / "config" / "external_env.py"
        text = ext.read_text(encoding="utf-8")
        ext.write_text(text + "\nEXTERNAL_ENV_VARS = EXTERNAL_ENV_VARS[:-1]\n", encoding="utf-8")

    shadow = _shadow_kdive(tmp_path, drop_last_external_var)

    # Control: the shadow really bites an unpinned generator — rendering from it
    # loses the dropped row, so a check fed this render goes red on a tree whose
    # honest verdict is green. If this ever stops holding, the tree no longer
    # constructs the divergence and the assertion below proves nothing.
    stale_render = tmp_path / "stale-render.md"
    gen = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-c",
            "from pathlib import Path;"
            " from scripts.gen_config_reference import write_reference;"
            f" write_reference(Path({str(stale_render)!r}))",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=_ROOT,
        env={**os.environ, "PYTHONPATH": str(shadow)},
    )
    assert gen.returncode == 0, gen.stderr
    assert "KDIVE_RELEASE" not in stale_render.read_text(encoding="utf-8"), (
        "the shadowed render still matches the working tree, so this test no longer "
        "constructs the #1987 divergence and the assertion below proves nothing"
    )

    result = _run_gate("config-docs-check", pythonpath=str(shadow))
    assert result.returncode == 0, (
        "config-docs-check rendered a stale registry: its verdict describes whatever kdive "
        f"is first on sys.path instead of the working tree being checked; {result.stderr}"
    )
