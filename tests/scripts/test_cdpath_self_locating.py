"""A self-locating `cd ... dirname` must neutralise CDPATH.

With CDPATH exported, `cd` through a relative script path prints the resolved directory to stdout,
so `$(cd "$(dirname ...)" && pwd)` captures it twice. Prefixing `CDPATH=''` stops the lookup.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from tests.host_capabilities import requires_bash

ROOT = Path(__file__).resolve().parents[2]
BASH = shutil.which("bash")
SELF_LOCATING = re.compile(r"\bcd\b[^#\n]*dirname")
SHELL_SHEBANG = re.compile(rb"#!.*\b(ba)?sh\b")

pytestmark = requires_bash(3, 2, "CDPATH self-location guard")


def _tracked_shell_scripts() -> list[str]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", ".", ":!docs", ":!tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    scripts = []
    for name in filter(None, tracked):
        path = ROOT / name
        if not path.is_file():
            continue
        if name.endswith(".sh") or SHELL_SHEBANG.match(path.open("rb").readline()):
            scripts.append(name)
    return scripts


def test_self_locating_cd_clears_cdpath() -> None:
    offenders = [
        f"{name}:{number}"
        for name in _tracked_shell_scripts()
        for number, line in enumerate((ROOT / name).read_text().splitlines(), 1)
        if SELF_LOCATING.search(line) and "CDPATH=''" not in line
    ]
    assert not offenders, f"self-locating cd without CDPATH='': {offenders}"


def test_exported_cdpath_does_not_double_the_script_dir() -> None:
    assert BASH is not None
    result = subprocess.run(
        [BASH, "-c", 'source scripts/live-stack/env.sh && printf "%s" "$repo_root"'],
        cwd=ROOT,
        env={**os.environ, "CDPATH": "."},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.endswith(str(ROOT)), (result.stdout, result.stderr)
    assert "\n" not in result.stdout
