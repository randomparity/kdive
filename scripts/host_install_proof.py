"""Prove the documented local-libvirt host installation on one clean host (ADR-0716).

`bundle` cuts the pinned kernel bundle where its fixture verifies, `run` drives the documented
entry points on an exclusive clean host over SSH and composes one version-1 result for its
`host-install` cell, and `merge` combines run directories for `coverage_campaign qualify`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kdive.images.rootfs.catalog import load_rootfs_catalog
from scripts import kernel_fixtures
from scripts.coverage_campaign.contract import Cell, build_contract, digest, image_family
from scripts.coverage_campaign.evidence import (
    Context,
    Evidence,
    GitSHA,
    InputBindings,
    Outcome,
    Role,
)

ROOT = Path(__file__).resolve().parents[1]
ASSERTIONS = (
    "clean-install",
    "first-boot",
    "repeat-setup",
    "second-boot",
    "confinement",
    "cleanup",
)
CLEAN_STEPS = (
    "observe-host",
    "operator-prerequisites",
    "bootstrap",
    "just",
    "copy-source",
    "clone",
    "kernel-source",
    "setup",
    "prepare",
    "preflight",
    "stack",
    "guest-image",
)
REPEAT_STEPS = ("repeat-setup", "repeat-prepare", "repeat-preflight", "repeat-stack")
PHASES = ("first-boot", "second-boot")
ROLES: tuple[Role, ...] = ("server", "worker", "reconciler", "authority")
SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "ConnectTimeout=30",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=4",
)
# Per architecture: the accelerator a native lane binds and the fixture's boot member.
ARCH_LANE = {"x86_64": ("kvm", "arch/x86/boot/bzImage"), "ppc64le": ("kvm-hv", "vmlinux")}
SHORT_STEP_S = 3600.0
LONG_STEP_S = 3 * 3600.0
NODE_ID = "tests/integration/test_host_install_live.py::test_installed_host_boots_pinned_kernel"
_PHASE_BYTES = 1024 * 1024
# No ':' in the host: scp addresses `host:path`, so a bare IPv6 literal would misroute.
_TARGET = re.compile(r"[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.-]*")
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")
# The disposable local witness-member DSN from docs/operating/runbooks/live-stack.md.
_WITNESS_DSN = (
    "postgresql://kdive-witness-member:kdive-witness-local"  # pragma: allowlist secret
    "@localhost:5432/kdive"
)
_PUBLIC_ORIGIN = "https://github.com/randomparity/kdive.git"

_OBSERVE = r"""set -u
. /etc/os-release
printf 'os=%s:%s\n' "$ID" "$VERSION_ID"
printf 'arch=%s\n' "$(uname -m)"
if command -v getenforce >/dev/null 2>&1; then
  printf 'mode=%s\n' "$(getenforce)"
elif [ -r /sys/module/apparmor/parameters/enabled ]; then
  printf 'mode=%s\n' "$(cat /sys/module/apparmor/parameters/enabled)"
else
  echo mode=none
fi
if [ -c /dev/kvm ]; then echo kvm=yes; else echo kvm=no; fi
if sudo -n true 2>/dev/null; then echo sudo=yes; else echo sudo=no; fi
[ -e "$HOME/kdive" ] && echo 'marker=~/kdive'
for path in /opt/kdive-live-worker-lifecycle /var/lib/kdive; do
  [ -e "$path" ] && echo "marker=$path"
done
systemctl list-unit-files 'kdive-live-worker*' --no-legend 2>/dev/null |
  awk 'NF { print "marker=unit:" $1 }'
true
"""
_BOOTSTRAP = r"""set -euo pipefail
if ! command -v git >/dev/null || ! command -v curl >/dev/null; then
  if command -v apt-get >/dev/null; then
    sudo -n apt-get update
    sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y git curl
  elif command -v dnf >/dev/null; then
    sudo -n dnf install -y git curl
  else
    sudo -n zypper --non-interactive install git curl
  fi
fi
curl -LsSf https://astral.sh/uv/install.sh | sh
"""
_JUST = "set -euo pipefail\nuv tool install rust-just\njust --version\n"
_CLONE = """set -euo pipefail
git clone --quiet "$HOME/kdive-candidate.bundle" "$HOME/kdive"
cd "$HOME/kdive"
git checkout --quiet --detach {candidate}
git remote set-url origin {origin}
git fetch --quiet origin main
rm -f "$HOME/kdive-candidate.bundle"
git rev-parse HEAD
"""
# examples/local-libvirt/README.md lists a kernel tree at KDIVE_KERNEL_SRC (default ~/src/linux)
# as a prerequisite; host preparation grants workers traversal only to a tree that exists.
_KERNEL_SOURCE = """set -euo pipefail
cd "$HOME/kdive"
KDIVE_KERNEL_REF={commit} scripts/fetch-kernel-tree.sh "$HOME/src/linux"
"""
_SETUP = 'set -euo pipefail\ncd "$HOME/kdive"\njust setup\n'
_PREPARE = """set -euo pipefail
cd "$HOME/kdive"
export KDIVE_LIFECYCLE_WITNESS_DATABASE_URL={dsn}
printf '\\n' | just prepare-local-libvirt-host
"""
_PREFLIGHT = 'set -euo pipefail\ncd "$HOME/kdive"\njust check-local-libvirt\n'
_STACK = """set -euo pipefail
mkdir -p "$HOME/kdive-demo"
cd "$HOME/kdive"
KDIVE_DEMO_WORKSPACE="$HOME/kdive-demo" examples/local-libvirt/demo-up.sh
"""
_IMAGE = """set -euo pipefail
cd "$HOME/kdive"
examples/local-libvirt/build-image.sh {image}
printf 'image_sha256=%s\\n' "$(sha256sum /var/lib/kdive/rootfs/local/{image}.qcow2 | cut -d' ' -f1)"
mkdir -p "$HOME/host-install-<run>"
"""
_NODE = """set -euo pipefail
cd "$HOME/kdive"
source examples/local-libvirt/env.sh
export KDIVE_GUEST_IMAGE=/var/lib/kdive/rootfs/local/{image}.qcow2
export HOST_INSTALL_PHASE={phase} HOST_INSTALL_CANDIDATE={candidate} HOST_INSTALL_IMAGE={image}
export HOST_INSTALL_OUTPUT="$HOME/host-install-<run>/<phase>"
export HOST_INSTALL_BUNDLE="$HOME/host-install-<run>/bundle"
uv run python -m pytest -p no:cacheprovider -m live_stack -q -rs {node}
"""


@dataclass(frozen=True)
class Step:
    name: str
    exit_code: int | Literal["timeout"]
    seconds: float
    transcript_sha256: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class HostFacts:
    os: str
    arch: str
    mode: str
    kvm: bool
    sudo: bool
    markers: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.markers

    @property
    def enforcing(self) -> bool:
        return self.mode in ("Enforcing", "Y")

    @property
    def prepared(self) -> bool:
        return self.clean and self.kvm and self.sudo


class PhaseRecord(BaseModel):
    """One boot phase, as written by the on-host node."""

    model_config = ConfigDict(extra="forbid", strict=True)
    phase: Literal["first-boot", "second-boot"]
    passed: bool
    booted: bool
    deployed: dict[Role, GitSHA | None]
    context: Context
    host_enforcing: bool
    guest_label: Annotated[str, Field(max_length=512)]
    guest_confined: bool
    prerequisites: dict[Annotated[str, Field(max_length=64)], bool] = Field(max_length=32)
    cleanup: dict[Annotated[str, Field(max_length=64)], bool] = Field(max_length=32)
    failures: list[Annotated[str, Field(max_length=512)]] = Field(max_length=64)


def validate_target(value: str) -> str:
    if not _TARGET.fullmatch(value):
        raise ValueError("invalid target; use USER@HOST without options or shell text")
    return value


def validate_name(value: str) -> str:
    if not _NAME.fullmatch(value):
        raise ValueError("invalid name; use a catalog image name")
    return value


def step_script(template: str, **values: str) -> str:
    return template.format(**{key: shlex.quote(value) for key, value in values.items()})


def _in_run(script: str, run_id: str, phase: str = "") -> str:
    """Place a generated run id and a code-literal phase inside already-quoted scripts."""
    if not re.fullmatch(r"[0-9a-f]{12}", run_id) or phase not in ("", *PHASES):
        raise ValueError("invalid run id or phase")
    return script.replace("<run>", run_id).replace("<phase>", phase)


def _transport(known_hosts: Path) -> list[str]:
    return [*SSH_OPTIONS, "-o", f"UserKnownHostsFile={known_hosts}"]


def ssh_argv(target: str, known_hosts: Path, script: str) -> list[str]:
    return ["ssh", *_transport(known_hosts), target, "bash -lc " + shlex.quote(script)]


def scp_argv(known_hosts: Path, source: str, dest: str) -> list[str]:
    return ["scp", "-q", "-r", *_transport(known_hosts), source, dest]


def _file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run_step(name: str, argv: list[str], log: Path, timeout_s: float) -> Step:
    """Run one step with no stdin, so no command it starts can consume a later command."""
    started = time.monotonic()
    with log.open("wb") as transcript:
        try:
            code: int | Literal["timeout"] = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=transcript,
                stderr=subprocess.STDOUT,
                timeout=timeout_s,
                check=False,
            ).returncode
        except subprocess.TimeoutExpired:
            code = "timeout"
    return Step(name, code, round(time.monotonic() - started, 3), _file_digest(log))


def parse_host(text: str) -> HostFacts | None:
    values: dict[str, str] = {}
    markers: list[str] = []
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "marker":
            markers.append(value)
        else:
            values.setdefault(key, value)
    try:
        Context.model_validate(
            {"host_os": values.get("os"), "host_arch": values.get("arch"), "accelerator": "none"}
        )
    except ValidationError:
        return None
    return HostFacts(
        values["os"],
        values["arch"],
        values.get("mode", "none"),
        values.get("kvm") == "yes",
        values.get("sudo") == "yes",
        tuple(markers),
    )


def family_matches(os_id: str, family: str) -> bool:
    images = load_rootfs_catalog().values()
    return any(image_family(image) == family for image in images if image.distro == os_id)


def read_phase(path: Path) -> PhaseRecord | None:
    try:
        if path.stat().st_size > _PHASE_BYTES:
            return None
        return PhaseRecord.model_validate_json(path.read_bytes())
    except OSError, ValidationError:
        return None


def console_has_release(text: str, release: str) -> bool:
    return re.search(rf"Linux version {re.escape(release)} ", text) is not None


def qemu_pid(ps_rows: str, domain: str) -> int | None:
    """The single process whose `pid args` row runs the named domain's guest, if exactly one."""
    pids = [
        int(fields[0])
        for row in ps_rows.splitlines()
        if len(fields := row.split(None, 1)) == 2
        and fields[0].isdecimal()
        and f"-name guest={domain}," in fields[1]
    ]
    return pids[0] if len(pids) == 1 else None


def label_confined(label: str, domain_uuid: str) -> bool:
    """A per-domain sVirt SELinux type, or libvirt's per-domain AppArmor profile in enforce."""
    fields = label.split(":")
    if len(fields) >= 4:
        return fields[2] == "svirt_t"
    return label == f"libvirt-{domain_uuid} (enforce)"


def package_digest(root: Path) -> str:
    entries = sorted(
        (path.relative_to(root).as_posix(), _file_digest(path))
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    )
    return hashlib.sha256("".join(f"{rel}\0{sha}\n" for rel, sha in entries).encode()).hexdigest()


def kernel_member_digest(tar_path: Path) -> str:
    with tarfile.open(tar_path) as archive:
        member = archive.extractfile("boot/vmlinuz")
        if member is None:
            raise ValueError("kernel bundle has no boot/vmlinuz file")
        checksum = hashlib.sha256()
        while chunk := member.read(1024 * 1024):
            checksum.update(chunk)
        return checksum.hexdigest()


def _artifact(directory: Path, payload: dict[str, object]) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    value = hashlib.sha256(data).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{value}.json").write_bytes(data)
    return value


def _steps_hold(steps: list[Step], required: tuple[str, ...]) -> bool:
    by_name = {step.name: step for step in steps}
    return all(name in by_name and by_name[name].ok for name in required)


def _phase_view(record: PhaseRecord | None) -> dict[str, object] | None:
    return None if record is None else record.model_dump(mode="json")


def _holds(
    steps: list[Step],
    host: HostFacts,
    phases: tuple[PhaseRecord | None, PhaseRecord | None],
    operator_sha256: str | None,
) -> dict[str, bool]:
    first, second = phases
    clean = tuple(s for s in CLEAN_STEPS if operator_sha256 or s != "operator-prerequisites")
    both = [p for p in phases if p is not None]
    complete = len(both) == 2
    return {
        "clean-install": host.prepared and _steps_hold(steps, clean),
        "first-boot": first is not None
        and first.booted
        and _steps_hold(steps, ("copy-bundle", "first-boot", "fetch-first-boot")),
        "repeat-setup": _steps_hold(steps, REPEAT_STEPS),
        "second-boot": second is not None
        and second.booted
        and _steps_hold(steps, ("second-boot", "fetch-second-boot")),
        "confinement": host.enforcing
        and complete
        and all(
            p.host_enforcing
            and p.guest_confined
            and p.prerequisites
            and all(p.prerequisites.values())
            for p in both
        ),
        "cleanup": complete and all(p.cleanup and all(p.cleanup.values()) for p in both),
    }


def _deployed(phases: list[PhaseRecord]) -> dict[Role, str]:
    if len(phases) != 2:
        return {}
    first, second = phases
    return {
        role: value
        for role in ROLES
        if (value := first.deployed.get(role)) is not None and second.deployed.get(role) == value
    }


def compose(
    *,
    cell: Cell,
    candidate: str,
    matrix: str,
    binding: Context,
    steps: list[Step],
    first: PhaseRecord | None,
    second: PhaseRecord | None,
    host: HostFacts,
    operator_sha256: str | None,
    seconds: float,
    artifacts: Path,
) -> Evidence:
    """Compose the cell's single result; only a complete clean run at the candidate succeeds."""
    if first is not None and first.phase != "first-boot":
        first = None
    if second is not None and second.phase != "second-boot":
        second = None
    holds = _holds(steps, host, (first, second), operator_sha256)
    present = [p for p in (first, second) if p is not None]
    payloads: dict[str, dict[str, object]] = {
        "clean-install": {
            "host": {
                "os": host.os,
                "arch": host.arch,
                "mode": host.mode,
                "kvm": host.kvm,
                "sudo": host.sudo,
                "markers": list(host.markers),
            },
            "operator_prerequisites_sha256": operator_sha256,
            "steps": [s.__dict__ for s in steps if s.name in CLEAN_STEPS],
        },
        "first-boot": {"phase": _phase_view(first)},
        "repeat-setup": {"steps": [s.__dict__ for s in steps if s.name in REPEAT_STEPS]},
        "second-boot": {"phase": _phase_view(second)},
        "confinement": {
            "host_mode": host.mode,
            "phases": [
                {
                    "phase": p.phase,
                    "host_enforcing": p.host_enforcing,
                    "guest_label": p.guest_label,
                    "guest_confined": p.guest_confined,
                    "prerequisites": p.prerequisites,
                }
                for p in present
            ],
        },
        "cleanup": {"phases": [{"phase": p.phase, "cleanup": p.cleanup} for p in present]},
    }
    assertions = {
        name: _artifact(artifacts, {"assertion": name, "holds": holds[name], **payloads[name]})
        for name in ASSERTIONS
    }
    deployed = _deployed(present)
    context = present[0].context if present else binding
    if not host.prepared:
        outcome = Outcome.BLOCKED
        impediments: list[Literal["known-defect", "missing-prerequisite"]] = [
            "missing-prerequisite"
        ]
    else:
        failed = (
            not all(holds.values())
            or set(deployed) != set(ROLES)
            or any(value != candidate for value in deployed.values())
            or any(p.context != binding for p in present)
            or not all(p.passed for p in present)
        )
        outcome, impediments = (Outcome.FAILURE if failed else Outcome.SUCCESS), []
    if cell.node_id is None:
        raise ValueError("host-install cell has no implemented node; bind it before running")
    return Evidence(
        version=1,
        cell_id=cell.id,
        scenario_id=cell.scenario_id,
        node_id=cell.node_id,
        outcome=outcome,
        candidate_sha=candidate,
        matrix_sha256=matrix,
        input_sha256=digest(binding),
        deployed_roles=deployed,
        context=context,
        duration_seconds=round(seconds, 3),
        assertions=assertions,
        artifacts=sorted(set(assertions.values())),
        impediments=impediments,
    )


def merge(runs: list[Path], output: Path) -> None:
    """Combine run directories into one `qualify` input pair; collisions are errors.

    A run that could not identify its host wrote no binding and is skipped, so its cell
    stays `not-run` under `qualify`.
    """
    cells: dict[str, Context] = {}
    results: list[object] = []
    identity: tuple[str, str] | None = None
    for run in runs:
        if not (run / "binding.json").exists():
            print(f"skipping {run}: no binding (host not identified)", file=sys.stderr)
            continue
        inputs = InputBindings.model_validate_json((run / "binding.json").read_text())
        if identity is None:
            identity = (inputs.candidate_sha, inputs.matrix_sha256)
        elif identity != (inputs.candidate_sha, inputs.matrix_sha256):
            raise ValueError(
                "runs carry different candidate or matrix identities; merge one candidate"
            )
        for cell_id, context in inputs.cells.items():
            if cell_id in cells:
                raise ValueError("two runs bind the same cell; keep one run per cell")
            cells[cell_id] = context
        result = run / "result.json"
        if result.exists():
            results.extend(json.loads(result.read_text()))
    if identity is None:
        raise ValueError("no runs supplied")
    output.mkdir(parents=True)
    merged = InputBindings(
        version=1, candidate_sha=identity[0], matrix_sha256=identity[1], cells=cells
    )
    (output / "inputs.json").write_text(merged.model_dump_json(indent=2) + "\n")
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")


def bundle(fixture: Path, baseline: str, output: Path) -> dict[str, object]:
    """Cut the documented combined kernel tar on the host where the fixture verifies in place."""
    arch = platform.machine()
    if arch not in ARCH_LANE:
        raise ValueError(f"bundle runs on a native x86_64 or ppc64le fixture host, not {arch}")
    manifest = kernel_fixtures.verify(fixture, baseline=baseline, arch=arch)
    output.mkdir(parents=True)
    member = Path(ARCH_LANE[arch][1])
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG")}
    with tempfile.TemporaryDirectory(prefix="kdive-host-install-bundle-") as scratch:
        work = Path(scratch)
        subprocess.run(
            [
                "make",
                "-C",
                str(fixture),
                "modules_install",
                f"INSTALL_MOD_PATH={work / 'mod'}",
                f"ARCH={kernel_fixtures.ARCH[arch]}",
                "INSTALL_MOD_STRIP=1",
            ],
            check=True,
            env=env,
            stdout=subprocess.DEVNULL,
        )
        boot_root = fixture
        if arch == "ppc64le":
            subprocess.run(
                ["strip", "-s", str(fixture / member), "-o", str(work / "vmlinuz")], check=True
            )
            boot_root, member = work, Path("vmlinuz")
        subprocess.run(
            [
                "tar",
                "-czf",
                str(output / "kernel.tar.gz"),
                "--exclude=*/build",
                "--exclude=*/source",
                f"--transform=s|^{member}$|boot/vmlinuz|",
                "-C",
                str(boot_root),
                str(member),
                "-C",
                str(work / "mod"),
                "lib/modules",
            ],
            check=True,
        )
    shutil.copyfile(fixture / ".config", output / "effective_config")
    (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest


def bundle_inputs(bundle_dir: Path) -> tuple[dict[str, Any], str]:
    """Check a bundle's own consistency and return its manifest and boot-member digest."""
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(artifacts, dict) or manifest.get("arch") not in ARCH_LANE:
        raise ValueError("bundle manifest lacks arch or artifacts; recut it with `bundle`")
    for key in ("release", "build_id", "toolchain", "source"):
        if not manifest.get(key):
            raise ValueError(f"bundle manifest lacks {key}; recut it with `bundle`")
    if _file_digest(bundle_dir / "effective_config") != artifacts.get(".config"):
        raise ValueError("bundle effective_config differs from its manifest; recut it")
    kernel_sha256 = kernel_member_digest(bundle_dir / "kernel.tar.gz")
    try:
        fields = kernel_fields(manifest, kernel_sha256)
        Context.model_validate(
            {"host_os": "fedora:44", "host_arch": "x86_64", "accelerator": "none", **fields}
        )
    except (KeyError, TypeError, ValidationError) as error:
        raise ValueError("bundle manifest kernel identity is malformed; recut it") from error
    return manifest, kernel_sha256


def kernel_fields(manifest: dict[str, Any], kernel_sha256: str) -> dict[str, str]:
    """The six-input kernel identity a bundle binds; the node observes the same fields."""
    return {
        "kernel_sha256": kernel_sha256,
        "kernel_source_sha": manifest["source"]["commit"],
        "kernel_config_sha256": manifest["artifacts"][".config"],
        "compiler_id": kernel_fixtures.identity(manifest["toolchain"]),
        "kernel_build_id": manifest["build_id"],
    }


def _binding(
    host: HostFacts, row_os: str, arch: str, manifest: dict[str, Any], kernel_sha256: str
) -> Context:
    return Context.model_validate(
        {
            "host_os": host.os,
            "host_arch": host.arch,
            "guest_os": row_os,
            "guest_arch": arch,
            "accelerator": ARCH_LANE[arch][0],
            **kernel_fields(manifest, kernel_sha256),
        }
    )


@dataclass
class _Remote:
    target: str
    known_hosts: Path
    logs: Path
    steps: list[Step]

    def step(self, name: str, script: str, timeout_s: float = SHORT_STEP_S) -> Step:
        return self._record(name, ssh_argv(self.target, self.known_hosts, script), timeout_s)

    def copy(self, name: str, source: str, dest: str) -> Step:
        return self._record(name, scp_argv(self.known_hosts, source, dest), SHORT_STEP_S)

    def _record(self, name: str, argv: list[str], timeout_s: float) -> Step:
        result = run_step(
            name, argv, self.logs / f"{len(self.steps) + 1:02d}-{name}.log", timeout_s
        )
        self.steps.append(result)
        return result

    def text(self, step: Step) -> str:
        log = self.logs / f"{self.steps.index(step) + 1:02d}-{step.name}.log"
        with log.open("rb") as stream:
            return stream.read(_PHASE_BYTES).decode(errors="replace")


def _summary(output: Path, steps: list[Step], outcome: str | None) -> None:
    payload = {
        "outcome": outcome,
        "steps": [{"name": s.name, "exit_code": s.exit_code, "seconds": s.seconds} for s in steps],
    }
    (output / "summary.json").write_text(json.dumps(payload, indent=2) + "\n")


def _checked_candidate(candidate: str) -> str:
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if not re.fullmatch(r"[0-9a-f]{40}", candidate) or candidate != head or dirty:
        raise ValueError("candidate must be the clean controller checkout's full HEAD SHA")
    return candidate


def _run_sequence(remote: _Remote, sequence: list[tuple[str, str, float]]) -> bool:
    return all(remote.step(name, script, timeout_s).ok for name, script, timeout_s in sequence)


def run(args: argparse.Namespace) -> int:
    target = validate_target(args.target)
    image = validate_name(args.guest_image)
    candidate = _checked_candidate(args.candidate)
    manifest, kernel_sha256 = bundle_inputs(args.bundle)
    arch = str(manifest["arch"])
    row = load_rootfs_catalog().get(image)
    if row is None or row.arch != arch:
        raise ValueError("guest image must be a catalog row of the bundle's architecture")
    contract = build_contract()
    cells = [
        c for c in contract.cells if c.id == f"host-install/local-libvirt/{arch}/{args.family}"
    ]
    if len(cells) != 1 or cells[0].node_id is None:
        raise ValueError("no implemented host-install cell for this family and architecture")
    cell = cells[0]
    operator = args.operator_prerequisites
    operator_sha256 = _file_digest(operator) if operator else None
    output: Path = args.output
    output.mkdir(parents=True)
    output.chmod(0o700)  # private transcripts and phase records, whatever the umask
    (output / "steps").mkdir()
    # Every local failure that can exit 2 happens before the first remote step.
    subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "bundle",
            "create",
            str(output / "kdive-candidate.bundle"),
            "HEAD",
        ],
        check=True,
        capture_output=True,
    )
    remote = _Remote(target, args.known_hosts, output / "steps", [])
    started = time.monotonic()
    observe = remote.step("observe-host", _OBSERVE)
    host = parse_host(remote.text(observe))
    if not observe.ok or host is None:
        _summary(output, remote.steps, None)
        (output / "kdive-candidate.bundle").unlink()
        return 3
    if not family_matches(host.os.split(":", 1)[0], args.family) or host.arch != arch:
        _summary(output, remote.steps, None)
        (output / "kdive-candidate.bundle").unlink()
        print("target host family or architecture differs from the selected cell", file=sys.stderr)
        return 2
    binding = _binding(host, f"{row.distro}:{row.version}", arch, manifest, kernel_sha256)
    phases: dict[str, PhaseRecord | None] = {"first-boot": None, "second-boot": None}
    if host.prepared:
        binding = _drive(remote, args, candidate, image, operator, binding, phases, output)
    (output / "kdive-candidate.bundle").unlink(missing_ok=True)
    inputs = InputBindings(
        version=1,
        candidate_sha=candidate,
        matrix_sha256=contract.matrix_sha256,
        cells={cell.id: binding},
    )
    (output / "binding.json").write_text(inputs.model_dump_json(indent=2) + "\n")
    evidence = compose(
        cell=cell,
        candidate=candidate,
        matrix=contract.matrix_sha256,
        binding=binding,
        steps=remote.steps,
        first=phases["first-boot"],
        second=phases["second-boot"],
        host=host,
        operator_sha256=operator_sha256,
        seconds=time.monotonic() - started,
        artifacts=output / "artifacts",
    )
    (output / "result.json").write_text(
        json.dumps([evidence.model_dump(mode="json")], indent=2) + "\n"
    )
    _summary(output, remote.steps, evidence.outcome.value)
    return {Outcome.SUCCESS: 0, Outcome.BLOCKED: 3}.get(evidence.outcome, 1)


def _drive(
    remote: _Remote,
    args: argparse.Namespace,
    candidate: str,
    image: str,
    operator: Path | None,
    binding: Context,
    phases: dict[str, PhaseRecord | None],
    output: Path,
) -> Context:
    """Run the documented sequence; stop at the first failed step. Returns the final binding."""
    run_id = uuid.uuid4().hex[:12]
    kernel_commit = binding.kernel_source_sha or ""
    host_dir = f"{remote.target}:host-install-{run_id}"
    if (
        operator is not None
        and not remote.step("operator-prerequisites", operator.read_text(), LONG_STEP_S).ok
    ):
        return binding
    source = output / "kdive-candidate.bundle"
    copied = (
        _run_sequence(
            remote, [("bootstrap", _BOOTSTRAP, SHORT_STEP_S), ("just", _JUST, SHORT_STEP_S)]
        )
        and remote.copy("copy-source", str(source), f"{remote.target}:kdive-candidate.bundle").ok
    )
    source.unlink(missing_ok=True)
    if not copied:
        return binding
    clone = step_script(_CLONE, candidate=candidate, origin=_PUBLIC_ORIGIN)
    prepare = step_script(_PREPARE, dsn=_WITNESS_DSN)
    setup = [
        ("setup", _SETUP, LONG_STEP_S),
        ("prepare", prepare, LONG_STEP_S),
        ("preflight", _PREFLIGHT, SHORT_STEP_S),
        ("stack", _STACK, SHORT_STEP_S),
    ]
    kernel = step_script(_KERNEL_SOURCE, commit=kernel_commit)
    first = [("clone", clone, SHORT_STEP_S), ("kernel-source", kernel, SHORT_STEP_S)]
    if not _run_sequence(remote, [*first, *setup]):
        return binding
    built = remote.step(
        "guest-image", _in_run(step_script(_IMAGE, image=image), run_id), LONG_STEP_S
    )
    match = re.search(r"^image_sha256=([0-9a-f]{64})$", remote.text(built), re.MULTILINE)
    if not built.ok or match is None:
        return binding
    binding = binding.model_copy(update={"image_sha256": match.group(1)})
    if not remote.copy("copy-bundle", str(args.bundle), f"{host_dir}/bundle").ok:
        return binding
    phases["first-boot"] = _boot(remote, "first-boot", candidate, image, run_id, output)
    if phases["first-boot"] is None or not _run_sequence(
        remote, [(f"repeat-{name}", script, timeout_s) for name, script, timeout_s in setup]
    ):
        return binding
    phases["second-boot"] = _boot(remote, "second-boot", candidate, image, run_id, output)
    return binding


def _boot(
    remote: _Remote, phase: str, candidate: str, image: str, run_id: str, output: Path
) -> PhaseRecord | None:
    script = step_script(_NODE, phase=phase, candidate=candidate, image=image, node=NODE_ID)
    remote.step(phase, _in_run(script, run_id, phase), SHORT_STEP_S)
    local = output / "phases" / phase
    local.mkdir(parents=True)
    remote.copy(
        f"fetch-{phase}",
        f"{remote.target}:host-install-{run_id}/{phase}/phase.json",
        str(local / "phase.json"),
    )
    return read_phase(local / "phase.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    cut = commands.add_parser("bundle", help="cut the kernel bundle on the fixture's own host")
    cut.add_argument("--fixture", type=Path, required=True)
    cut.add_argument("--baseline", choices=("longterm", "stable"), default="longterm")
    cut.add_argument("--output", type=Path, required=True)
    prove = commands.add_parser("run", help="prove one exclusive clean host for its family cell")
    prove.add_argument("--target", required=True)
    prove.add_argument("--known-hosts", type=Path, required=True)
    prove.add_argument("--family", choices=("debian", "fedora", "enterprise"), required=True)
    prove.add_argument("--candidate", required=True)
    prove.add_argument("--bundle", type=Path, required=True)
    prove.add_argument("--guest-image", required=True)
    prove.add_argument("--operator-prerequisites", type=Path)
    prove.add_argument("--output", type=Path, required=True)
    combine = commands.add_parser("merge", help="combine run directories for coverage qualify")
    combine.add_argument("--output", type=Path, required=True)
    combine.add_argument("runs", type=Path, nargs="+")
    args = parser.parse_args(argv)
    try:
        if args.command == "bundle":
            bundle(args.fixture, args.baseline, args.output)
            return 0
        if args.command == "merge":
            merge(args.runs, args.output)
            return 0
        return run(args)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
