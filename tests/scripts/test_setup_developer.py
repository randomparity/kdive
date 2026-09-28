"""Developer setup installs tool dependencies and cannot succeed with a partial environment."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_LIBRARY = _ROOT / "scripts/lib/setup-developer.sh"


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\nset -eu\n" + body)
    path.chmod(0o755)


def _run(tmp_path: Path, *, failure: str = "", arch: str = "x86_64"):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "install.log"
    _stub(bindir / "uname", 'if [[ "$1" == -s ]]; then echo Linux; else echo "$TEST_ARCH"; fi\n')
    _stub(
        bindir / "uv",
        """
if [[ "$*" == 'tool dir --bin' ]]; then echo "$TEST_BIN"; exit; fi
printf 'uv %s\\n' "$*" >> "$TEST_LOG"
[[ "$TEST_FAILURE" != uv ]]
""",
    )
    _stub(bindir / "shellcheck", 'echo "version: 0.10.0"\n')
    _stub(
        bindir / "docker",
        """
if [[ "$1" == compose ]]; then echo 'Docker Compose version v2'; exit; fi
[[ "$TEST_FAILURE" != docker ]]
""",
    )
    _stub(bindir / "pkg-config", "exit 0\n")
    _stub(bindir / "go", 'echo "mod example.test/module v1.0.0"\n')
    # No host cabal must run when exercising the POWER path.
    _stub(
        bindir / "cabal",
        (
            'printf "cabal %s\\n" "$*" >> "$TEST_LOG"\n'
            'printf "#!/bin/sh\\necho version: 0.11.0\\n" > "$TEST_BIN/shellcheck"\n'
        ),
    )
    script = tmp_path / "run.sh"
    script.write_text(f"""#!/bin/bash
set -euo pipefail
source '{_LIBRARY}'
distro=debian
distro_exact=ubuntu
host_arch="$TEST_ARCH"
required_packages=(libvirt-dev)
recommended_packages=(make)
command_exists() {{ [[ "$1" != promtool ]] && command -v "$1" >/dev/null 2>&1; }}
package_for() {{ printf '%s' "$1"; }}
arch_needs_rust() {{ [[ "$1" == ppc64le ]]; }}
probe_all() {{ :; }}
dev_install_packages() {{
  printf 'packages %s\\n' "$*" >> "$TEST_LOG"
  [[ "$TEST_FAILURE" != packages ]]
}}
dev_install_archive() {{
  printf 'archive %s\\n' "$*" >> "$TEST_LOG"
  [[ "$TEST_FAILURE" != checksum ]]
  if [[ "$1" == shellcheck ]]; then
    printf '#!/bin/sh\\necho version: 0.11.0\\n' > "$TEST_BIN/shellcheck"
  fi
}}
dev_install_go_tool() {{
  printf 'go %s\\n' "$*" >> "$TEST_LOG"
  [[ "$TEST_FAILURE" != go ]]
}}
install_developer_dependencies
echo complete
""")
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "TEST_BIN": str(bindir),
            "TEST_LOG": str(log),
            "TEST_FAILURE": failure,
            "TEST_ARCH": arch,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    return result, log.read_text()


def test_installs_all_developer_tool_families(tmp_path: Path) -> None:
    result, log = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "uv tool install prek==0.4.3" in log
    for tool in ("shfmt", "actionlint", "helm", "gitleaks"):
        assert f"go {tool} " in log
    assert "archive shellcheck " in log
    assert "archive promtool " in log
    assert "packages libvirt-dev make" in log
    assert "complete" in result.stdout
    assert "guestfs" not in log


@pytest.mark.parametrize("failure", ["packages", "uv", "checksum", "go", "docker"])
def test_install_or_verification_failure_prevents_success(tmp_path: Path, failure: str) -> None:
    result, _ = _run(tmp_path, failure=failure)
    assert result.returncode != 0
    assert "complete" not in result.stdout
    if failure == "docker":
        assert "unavailable to this session" in result.stderr


def test_power_builds_shellcheck_natively(tmp_path: Path) -> None:
    result, log = _run(tmp_path, arch="ppc64le")
    assert result.returncode == 0, result.stderr
    assert "cabal install ShellCheck-0.11.0" in log
    assert "archive shellcheck" not in log


def test_archive_checksum_failure_cannot_install_a_binary(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir / "curl", 'while [[ "$1" != --output ]]; do shift; done\necho bad > "$2"\n')
    marker = tmp_path / "extracted"
    _stub(bindir / "tar", f'touch "{marker}"\n')
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'source "$1"; dev_bin="$2"; '
            'dev_install_archive tool https://example.test/tool "$3" tool',
            "test",
            str(_LIBRARY),
            str(bindir),
            "0" * 64,
        ],
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert not (bindir / "tool").exists()


def test_setup_pins_match_ci_and_hooks() -> None:
    source = _LIBRARY.read_text()
    ci = (_ROOT / ".github/workflows/ci.yml").read_text()
    hooks = (_ROOT / ".pre-commit-config.yaml").read_text()
    body_scan = (_ROOT / ".github/workflows/pr-body-scan.yml").read_text()
    for version in ("v3.13.1", "v3.21.0", "0.11.0", "0.4.3"):
        assert version in source and version in ci
    assert "v1.7.12" in source and "v1.7.12" in hooks
    assert "8.30.1" in source and "8.30.1" in body_scan


def test_installed_go_tool_is_reused(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    selected = bindir / "tool"
    selected.touch()
    selected.chmod(0o755)
    _stub(bindir / "go", '[[ "$1" == version ]]\necho "mod example.test/tool v1.2.3"\n')
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'source "$1"; dev_install_go_tool tool example.test/tool v1.2.3',
            "test",
            str(_LIBRARY),
        ],
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_helm_rebuilds_matching_version_without_release_flags(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir / "helm", "exit 0\n")
    metadata = tmp_path / "metadata"
    metadata.write_text("mod helm.sh/helm/v3 v3.21.0\n")
    _stub(
        bindir / "go",
        "\n".join(
            [
                'if [[ "$1" == version ]]; then cat "$TEST_METADATA"; exit; fi',
                '[[ "$1" == install && "$2" == -ldflags ]]',
                'printf "%s\\n" "$3" > "$TEST_METADATA"',
            ]
        ),
    )
    result = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'source "$1"; dev_bin="$2"; dev_install_helm',
            "test",
            str(_LIBRARY),
            str(bindir),
        ],
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "TEST_METADATA": str(metadata),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    flags = metadata.read_text()
    assert "internal/version.version=v3.21.0" in flags
    for package in ("pkg/lint/rules", "pkg/chartutil"):
        assert f"{package}.k8sVersionMajor=1" in flags
        assert f"{package}.k8sVersionMinor=35" in flags
