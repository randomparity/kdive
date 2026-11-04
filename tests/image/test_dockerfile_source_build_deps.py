"""Execute the image's architecture branches with package-manager effects captured."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


@pytest.mark.parametrize("arch", ["amd64", "arm64", "ppc64le"])
def test_source_built_drgn_image_packages(tmp_path: Path, arch: str) -> None:
    blocks = re.findall(r"^RUN (?:.*\\\n)*.*", DOCKERFILE.read_text(), re.MULTILINE)
    apt = [block.removeprefix("RUN ") for block in blocks if "apt-get update" in block]
    assert len(apt) == 2
    log = tmp_path / "packages"
    for command in ("apt-get", "sed", "rm"):
        executable = tmp_path / command
        executable.write_text(
            "#!/bin/sh\n"
            + ('printf "%s\\n" "$*" >> "$PACKAGE_LOG"\n' if command == "apt-get" else "exit 0\n")
        )
        executable.chmod(0o755)
    packages = []
    for block in apt:
        log.write_text("")
        subprocess.run(
            ["/bin/sh", "-ec", block],
            check=True,
            env={**os.environ, "PATH": str(tmp_path), "TARGETARCH": arch, "PACKAGE_LOG": str(log)},
        )
        packages.append(set(log.read_text().split()))
    source_build = arch in {"arm64", "ppc64le"}
    for package in (
        "libelf-dev",
        "libdw-dev",
        "libkdumpfile-dev",
        "autoconf",
        "automake",
        "libtool",
    ):
        assert (package in packages[0]) == source_build, (arch, package, packages[0])
    assert ("libkdumpfile10" in packages[1]) == source_build
