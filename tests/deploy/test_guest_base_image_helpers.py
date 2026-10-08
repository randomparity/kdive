"""Contracts for installing repository-managed helpers into guest base images."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from jinja2 import Environment

BUILD_ONE = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "ansible"
    / "roles"
    / "guest_base_image"
    / "tasks"
    / "build_one.yml"
)


def _helper_args(distro: str = "fedora", helper: str = "kdive-helper") -> list[str]:
    tasks: list[dict[str, Any]] = yaml.safe_load(BUILD_ONE.read_text(encoding="utf-8"))
    task = next(
        task for task in tasks if "Build the virt-customize helper arguments" in task["name"]
    )
    expression = task["ansible.builtin.set_fact"]["guest_base_image_helper_args"]
    rendered = (
        Environment(autoescape=False)
        .from_string(expression)
        .render(
            guest_base_image_helper_args=[],
            guest_base_image_helper_dir="/tmp/helpers",
            item=helper,
            image={"distro": distro},
        )
    )
    return yaml.safe_load(rendered)


def _relabel_command() -> str:
    argv = _helper_args()
    run_commands = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--run-command"]
    return run_commands[-1]


def test_helper_install_succeeds_without_restorecon(tmp_path: Path) -> None:
    """Ubuntu catalog images do not install SELinux tooling."""
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir()

    completed = subprocess.run(
        ["/bin/sh", "-c", _relabel_command()],
        env={**os.environ, "PATH": str(empty_bin)},
        check=False,
    )

    assert completed.returncode == 0


def test_helper_install_runs_restorecon_when_present(tmp_path: Path) -> None:
    """SELinux-capable images retain their helper-labeling behavior."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invocation = tmp_path / "restorecon.args"
    restorecon = fake_bin / "restorecon"
    restorecon.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {invocation}\n", encoding="utf-8")
    restorecon.chmod(0o755)

    completed = subprocess.run(
        ["/bin/sh", "-c", _relabel_command()],
        env={**os.environ, "PATH": str(fake_bin)},
        check=False,
    )

    assert completed.returncode == 0
    assert invocation.read_text(encoding="utf-8").splitlines() == [
        "-v",
        "/usr/local/sbin/kdive-helper",
    ]


def test_helper_install_propagates_restorecon_failure(tmp_path: Path) -> None:
    """A broken SELinux relabel remains a failed image build."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    restorecon = fake_bin / "restorecon"
    restorecon.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
    restorecon.chmod(0o755)

    completed = subprocess.run(
        ["/bin/sh", "-c", _relabel_command()],
        env={**os.environ, "PATH": str(fake_bin)},
        check=False,
    )

    assert completed.returncode == 23


@pytest.mark.parametrize("distro", ["ubuntu", "debian", "fedora", "rocky", "opensuse-leap"])
@pytest.mark.parametrize("helper", ["kdive-install-kernel", "kdive-drgn"])
def test_family_selection_preserves_installed_helper_path(distro: str, helper: str) -> None:
    args = _helper_args(distro, helper)
    family = (
        "debian/" if distro in {"debian", "ubuntu"} and helper == "kdive-install-kernel" else ""
    )
    if distro == "opensuse-leap" and helper == "kdive-install-kernel":
        family = "suse/"
    assert args[:2] == ["--copy-in", f"/tmp/helpers/{family}{helper}:/usr/local/sbin/"]
    assert f"chown root:root /usr/local/sbin/{helper}" in args
    assert f"chmod 0755 /usr/local/sbin/{helper}" in args


def test_leap_source_pin_reaches_existing_downloader() -> None:
    catalog_path = BUILD_ONE.parents[3] / "inventory/group_vars/all.yml"
    catalog = yaml.safe_load(catalog_path.read_text())["kdive_image_catalog"]
    leap = next(row for row in catalog if row["distro"] == "opensuse-leap")
    assert leap["arches"] == ["x86_64"]
    checksum = "sha256:0a5720416d423f98aacaa793a57d56ec045e3dd25cd88713952660ad00da53bd"
    assert leap["cloud_image_checksum"] == checksum
    tasks = yaml.safe_load(BUILD_ONE.read_text())
    download = next(
        t["ansible.builtin.get_url"] for t in tasks if "Download the cloud" in t["name"]
    )
    expression = Environment(autoescape=False).from_string(download["checksum"])
    assert expression.render(image=leap, omit="omitted") == checksum
    assert expression.render(image={}, omit="omitted") == "omitted"


@pytest.fixture
def growth(tmp_path: Path):
    import hashlib
    import json
    import sys

    tasks = yaml.safe_load(BUILD_ONE.read_text())
    task = next(t for t in tasks if t["name"].startswith("Grow the pinned Leap"))
    assert "image.name == 'opensuse-leap-15.6-kdive-remote-base'" in task["when"]
    script = task["ansible.builtin.command"]["argv"][2]
    source = tmp_path / "image.qcow2.source"
    source.write_bytes(b"pinned source")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    catalog = yaml.safe_load((BUILD_ONE.parents[3] / "inventory/group_vars/all.yml").read_text())
    entry = next(row for row in catalog["kdive_image_catalog"] if row["distro"] == "opensuse-leap")
    script = script.replace(entry["cloud_image_checksum"].removeprefix("sha256:"), digest)
    destination = tmp_path / "image.qcow2"
    bins = tmp_path / "bin"
    bins.mkdir()
    tool = bins / "tool"
    tool.write_text(r"""#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
fault = os.environ.get('FAULT', '')
with open(os.environ['TRACE'], 'a') as f:
    f.write(json.dumps([name, *args]) + '\n')
if name == fault:
    sys.exit(23)
if name == 'virt-resize':
    if args == ['--machine-readable']:
        print('virt-resize\n' + ('none' if fault == 'capability' else 'xfs'))
    else:
        if fault == 'resize':
            sys.exit(23)
        pathlib.Path(args[-1]).write_bytes(b'expanded')
        if fault == 'source-write':
            pathlib.Path(args[-2]).chmod(0o644)
            pathlib.Path(args[-2]).write_bytes(b'changed')
elif name == 'qemu-img':
    if args[0] == 'create':
        pathlib.Path(args[-2]).write_bytes(b'fresh output')
    else:
        expanded = pathlib.Path(args[-1]).read_bytes() == b'expanded'
        size = (10737418240 if fault != 'output-size' else 1024) if expanded else 879755264
        print(json.dumps({'format':'qcow2', 'virtual-size':size,
                          **({'backing-filename':'foreign'} if fault == 'backing' else {})}))
elif name == 'guestfish':
    expanded = pathlib.Path(args[args.index('-a')+1]).read_bytes() == b'expanded'
    starts = [1048576,3145728,37748736]
    ends = [3145727,37748735,10737401343 if expanded else 879738367]
    if fault == 'layout':
        starts[2] += 512
    for i,(start,end) in enumerate(zip(starts,ends),1):
        print(('[%d] = {\n  part_num: %d\n  part_start: %d\n'
               '  part_end: %d\n  part_size: %d\n}') % (i-1,i,start,end,end-start+1))
    print('KDIVE_METADATA')
    print('gpt\nvfat\n' + ('ext4' if fault == 'filesystem' else 'xfs'))
    print('changed' if expanded and fault == 'uuid' else 'root-uuid')
    print('changed' if expanded and fault == 'part-uuid' else 'part-uuid')
    print('changed' if expanded and fault == 'boot-region' else 'bios-hash')
    print('efi-hash')
    print('KDIVE_SPACE')
    print('bsize: 4096\nbavail: ' + ('1' if fault == 'root-space' else '2200000'))
""")
    tool.chmod(0o755)
    for name in ["virt-resize", "qemu-img", "guestfish"]:
        (bins / name).symlink_to(tool)
    trace = tmp_path / "trace"

    def invoke(fault=""):
        code = script
        if fault == "host-space":
            code = code.replace("shutil.disk_usage(source.parent).free", "0")
        result = subprocess.run(
            [sys.executable, "-c", code, str(source), str(destination)],
            env={
                **os.environ,
                "PATH": f"{bins}:{os.environ['PATH']}",
                "FAULT": fault,
                "TRACE": str(trace),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        lines = trace.read_text().splitlines() if trace.exists() else []
        calls = [json.loads(line) for line in lines]
        return result, calls

    return invoke, source, destination


def test_pinned_leap_growth_preserves_source_and_promotes_only_verified_output(growth):
    invoke, source, destination = growth
    result, calls = invoke()
    assert result.returncode == 0, result.stderr
    assert source.read_bytes() == b"pinned source"
    assert source.stat().st_mode & 0o777 == 0o444
    assert destination.read_bytes() == b"expanded"
    assert not Path(str(destination) + ".growing").exists()
    resize = next(c for c in calls if c[0] == "virt-resize" and "--expand" in c)
    assert resize[resize.index("--expand") + 1] == "/dev/sda3"
    assert resize[resize.index("--unknown-filesystems") + 1] == "error"


@pytest.mark.parametrize(
    "fault",
    [
        "capability",
        "backing",
        "layout",
        "filesystem",
        "uuid",
        "boot-region",
        "root-space",
        "source-write",
        "virt-resize",
        "host-space",
        "resize",
        "part-uuid",
        "output-size",
    ],
)
def test_growth_failure_never_promotes_or_replaces_source(growth, fault):
    invoke, source, destination = growth
    result, _ = invoke(fault)
    assert result.returncode != 0
    assert not destination.exists()
    assert "SyntaxError" not in result.stderr
    assert "kdive Leap growth:" in result.stderr or "exit status 23" in result.stderr
    if fault == "resize":
        assert Path(str(destination) + ".growing").read_bytes() == b"fresh output"
    if fault != "source-write":
        assert source.read_bytes() == b"pinned source"


@pytest.mark.parametrize("existing", ["destination", "partial", "symlink", "checksum"])
def test_growth_refuses_existing_output_or_untrusted_input(growth, existing):
    invoke, source, destination = growth
    if existing == "checksum":
        source.write_bytes(b"wrong source")
    elif existing == "symlink":
        saved = source.with_suffix(".saved")
        source.rename(saved)
        source.symlink_to(saved)
    else:
        path = destination if existing == "destination" else Path(str(destination) + ".growing")
        path.write_bytes(b"retained evidence")
    result, calls = invoke()
    assert result.returncode != 0
    assert not any(c[:2] == ["qemu-img", "create"] for c in calls)
    if existing in {"destination", "partial"}:
        assert path.read_bytes() == b"retained evidence"
