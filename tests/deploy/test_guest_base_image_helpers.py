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


# Native checksum/lstatns verify these bytes, including one terminal newline.
# guestfish cat display adds a newline; it is not part of the unit file.
JEOS_UNITS = {
    "jeos-firstboot.service": (
        "# SPDX-License-Identifier: MIT\n"
        "# SPDX-FileCopyrightText: Copyright 2015-2022 SUSE LLC\n"
        "\n"
        "[Unit]\n"
        "Description=SUSE JeOS First Boot Wizard\n"
        "\n"
        "# Same as YaST2-Firstboot.service here\n"
        "After=apparmor.service local-fs.target plymouth-start.service YaST2-Second-Sta"
        "ge.service\n"
        "Conflicts=plymouth-start.service\n"
        "Before=getty@tty1.service serial-getty@hvc0.service serial-getty@ttyS0.service"
        " serial-getty@ttyS1.service serial-getty@ttyS2.service serial-getty@ttyAMA0.se"
        "rvice\n"
        "Before=display-manager.service\n"
        # Stock native systemd marker path contains no credential.
        "ConditionPathExists=/var/lib/YaST2/reconfig_system\n"  # pragma: allowlist secret
        "OnFailure=poweroff.target\n"
        "\n"
        "# jeos-firstboot starts before wicked and login though.\n"
        "# It writes wicked configuration manually\n"
        "Before=wicked.service systemd-user-sessions.service\n"
        "# For NM it uses nmcli, so NM needs to be running\n"
        "After=NetworkManager.service\n"
        "\n"
        "# If cloud-init is used on the system, wait until it's done to be able\n"
        "# to check whether it performed any configuration.\n"
        "After=cloud-init-local.service\n"
        "\n"
        "# jeos-firstboot-snapshot.service deletes the flag file, but starts after us\n"
        "Wants=jeos-firstboot-snapshot.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "Environment=TERM=linux\n"
        "RemainAfterExit=yes\n"
        'ExecStartPre=/bin/sh -c "/usr/bin/plymouth quit 2>/dev/null || :"\n'
        "ExecStart=/usr/sbin/jeos-firstboot\n"
        "StandardOutput=tty\n"
        "StandardInput=tty\n"
        "#StandardError=tty\n"
        "# enable accessing global keyring to get data from eg. initrd\n"
        "KeyringMode=shared\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ),
    "jeos-firstboot-snapshot.service": (
        "# SPDX-License-Identifier: MIT\n"
        "# SPDX-FileCopyrightText: Copyright 2015-2022 SUSE LLC\n"
        "\n"
        "[Unit]\n"
        "Description=SUSE JeOS First Boot Wizard - create system snapshot\n"
        "\n"
        "# Same as YaST2-Firstboot.service here\n"
        "After=apparmor.service local-fs.target plymouth-start.service YaST2-Second-Sta"
        "ge.service\n"
        "Conflicts=plymouth-start.service\n"
        "Before=getty@tty1.service serial-getty@ttyS0.service serial-getty@ttyS1.servic"
        "e serial-getty@ttyS2.service\n"
        "Before=display-manager.service\n"
        # Stock native systemd marker path contains no credential.
        "ConditionPathExists=/var/lib/YaST2/reconfig_system\n"  # pragma: allowlist secret
        "# The configuration is already done - so this doesn't make much sense\n"
        "#OnFailure=poweroff.target\n"
        "\n"
        "# jeos-firstboot-snapshot starts after jeos-firstboot, time got synced\n"
        "# and dbus became available\n"
        "Wants=time-sync.target\n"
        "Requires=jeos-firstboot.service dbus.service\n"
        "After=jeos-firstboot.service time-sync.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        "# In Pre - if creation fails, don't do the configuration again\n"
        "ExecStartPre=/usr/bin/rm -f /var/lib/YaST2/reconfig_system\n"
        "ExecStart=/usr/sbin/jeos-firstboot-snapshot\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ),
}
UNIT_ROOTS = (
    "/etc/systemd/system.control",
    "/run/systemd/system.control",
    "/run/systemd/transient",
    "/run/systemd/generator.early",
    "/etc/systemd/system",
    "/etc/systemd/system.attached",
    "/run/systemd/system",
    "/run/systemd/system.attached",
    "/run/systemd/generator",
    "/usr/local/lib/systemd/system",
    "/usr/lib/systemd/system",
    "/lib/systemd/system",
    "/run/systemd/generator.late",
)


def _firstboot_task() -> dict[str, Any]:
    return next(
        t
        for t in yaml.safe_load(BUILD_ONE.read_text())
        if t["name"].startswith("Complete the pinned Leap native firstboot")
    )


@pytest.fixture
def firstboot(tmp_path: Path):
    import re
    import shutil

    root = tmp_path / "guest"
    root.mkdir()
    vendor = root / "usr/lib/systemd/system"
    vendor.mkdir(parents=True)
    for name, content in JEOS_UNITS.items():
        (vendor / name).write_text(content)
    marker = root / "var/lib/YaST2/reconfig_system"
    marker.parent.mkdir(parents=True)
    marker.touch(mode=0o644)
    unrelated = root / "var/lib/YaST2/unrelated"
    unrelated.write_text("preserve")
    bins = tmp_path / "bin"
    bins.mkdir()
    # Only privilege metadata is adapted; actual stat type/mode/size and hashes execute.
    native_stat = shutil.which("stat")
    assert native_stat is not None
    (bins / "stat").write_text(
        "#!/bin/sh\nset -eu\n"
        f'value=$({native_stat} "$@")\n'
        'if [ "$2" = "%u:%g:%a" ] && [ "' + str(os.getuid()) + '" != 0 ]; then\n'
        '  value="0:0:${value##*:}"\nfi\n'
        'if [ "${BAD_OWNER:-}" = "${4:-}" ]; then value="23:0:${value##*:}"; fi\n'
        'printf "%s\\n" "$value"\n'
    )
    (bins / "stat").chmod(0o755)

    def invoke(bad_owner: Path | None = None, failed_command: str = ""):
        if bad_owner is not None and os.getuid() == 0:
            os.chown(bad_owner, 23, 0)
        task = _firstboot_task()
        code = task["ansible.builtin.command"]["argv"][-1]
        # Map the guest filesystem boundary, not validation logic.
        code = re.sub(
            r"(?<![\w/])/(?:etc|run|usr|lib|var)(?=/|\s|$)",
            lambda match: str(root) + match.group(),
            code,
        )
        code = code.replace('[ "$dir" != / ]', f'[ "$dir" != "{root}" ]')
        if failed_command:
            (bins / failed_command).write_text("#!/bin/sh\nexit 23\n")
            (bins / failed_command).chmod(0o755)
        return subprocess.run(
            ["/bin/sh", "-c", code],
            capture_output=True,
            text=True,
            timeout=3,
            env={
                **os.environ,
                "PATH": f"{bins}:{os.environ['PATH']}",
                "BAD_OWNER": str(bad_owner) if bad_owner and os.getuid() != 0 else "",
            },
            check=False,
        )

    return root, marker, unrelated, invoke


def test_native_firstboot_completes_and_repeats_without_other_changes(firstboot) -> None:
    root, marker, unrelated, invoke = firstboot
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert invoke().returncode == 0
    assert not marker.exists()
    assert invoke().returncode == 0
    for path, content in before.items():
        if path != marker:
            assert path.read_bytes() == content
    assert unrelated.read_text() == "preserve"


@pytest.mark.parametrize("root_path", UNIT_ROOTS)
@pytest.mark.parametrize("kind", ["shadow", "prefix", "unsafe-root"])
def test_native_firstboot_refuses_all_native_load_roots(firstboot, root_path, kind) -> None:
    root, marker, _, invoke = firstboot
    load_root = root / root_path.lstrip("/")
    load_root.mkdir(parents=True, exist_ok=True)
    if kind == "shadow":
        (load_root / "jeos-firstboot.service").write_text("foreign unit")
    elif kind == "prefix":
        dropin = load_root / "jeos-firstboot-.service.d"
        dropin.mkdir()
        (dropin / "50-foreign.conf").write_text("[Unit]\nConditionPathExists=\n")
    else:
        load_root.chmod(0o777)
    assert invoke().returncode != 0
    assert marker.exists()


@pytest.mark.parametrize(
    "dropin",
    [
        "service.d",
        "jeos-.service.d",
        "jeos-firstboot.service.d",
        "jeos-firstboot-snapshot.service.d",
    ],
)
def test_native_firstboot_refuses_other_dropin_prefixes(firstboot, dropin) -> None:
    root, marker, _, invoke = firstboot
    path = root / "etc/systemd/system.control" / dropin
    path.mkdir(parents=True)
    (path / "50-foreign.conf").write_text("foreign")
    assert invoke().returncode != 0
    assert marker.exists()


@pytest.mark.parametrize(
    "kind", ["symlink", "fifo", "directory", "nonempty", "writable", "owner", "parent", "lib-alias"]
)
def test_native_firstboot_refuses_unsafe_marker_and_ancestors(firstboot, kind) -> None:
    root, marker, unrelated, invoke = firstboot
    bad_owner = None
    if kind in {"symlink", "fifo", "directory"}:
        marker.unlink()
        if kind == "symlink":
            marker.symlink_to(unrelated)
        elif kind == "fifo":
            os.mkfifo(marker)
        else:
            marker.mkdir()
    elif kind == "nonempty":
        marker.write_text("pending configuration")
    elif kind == "writable":
        marker.chmod(0o666)
    elif kind == "owner":
        bad_owner = marker
    elif kind == "parent":
        marker.parent.chmod(0o777)
    else:
        (root / "lib").symlink_to(root / "usr/lib")
    assert invoke(bad_owner).returncode != 0
    assert marker.exists() or marker.is_symlink()
    assert unrelated.read_text() == "preserve"


@pytest.mark.parametrize("command", ["stat", "sha256sum", "rm"])
def test_native_firstboot_propagates_command_failure(firstboot, command) -> None:
    _, marker, _, invoke = firstboot
    assert invoke(failed_command=command).returncode == 23
    assert marker.exists()


def test_native_firstboot_exact_row_and_preparation_before_promotion() -> None:
    tasks = yaml.safe_load(BUILD_ONE.read_text())
    task = _firstboot_task()
    assert task["when"] == [
        "image.name == 'opensuse-leap-15.6-kdive-remote-base'",
        "image.source == 'cloud-image'",
        "image.cloud_image_checksum | default('') == "
        "'sha256:0a5720416d423f98aacaa793a57d56ec045e3dd25cd88713952660ad00da53bd'",
        "(not guest_base_image_staged.stat.exists) or guest_base_image_force | bool",
    ]
    names = [entry["name"] for entry in tasks]
    current = names.index(task["name"])
    assert current > next(
        i
        for i, name in enumerate(names)
        if name.startswith("Verify the external-boot guest userland")
    )
    assert current < next(
        i for i, name in enumerate(names) if name.startswith("Stage the finished image")
    )
    assert task["ansible.builtin.command"]["argv"][:4] == [
        "virt-customize",
        "-a",
        "{{ guest_base_image_qcow2 }}",
        "--run-command",
    ]


@pytest.mark.parametrize("kind", ["fifo", "symlink", "owner", "writable", "snapshot-change"])
def test_native_firstboot_refuses_unsafe_vendor_units(firstboot, kind) -> None:
    root, marker, _, invoke = firstboot
    unit = root / "usr/lib/systemd/system/jeos-firstboot-snapshot.service"
    bad_owner = None
    if kind in {"fifo", "symlink"}:
        unit.unlink()
        if kind == "fifo":
            os.mkfifo(unit)
        else:
            target = root / "native-unit-copy"
            target.write_text(JEOS_UNITS[unit.name])
            unit.symlink_to(target)
    elif kind == "owner":
        bad_owner = unit
    elif kind == "writable":
        unit.chmod(0o666)
    else:
        unit.write_text("changed native semantics")
    assert invoke(bad_owner).returncode != 0
    assert marker.exists()


@pytest.mark.parametrize(
    "kind", ["root-file", "root-symlink", "ancestor-writable", "dropin-file", "dropin-symlink"]
)
def test_native_firstboot_refuses_unsafe_load_paths(firstboot, kind) -> None:
    root, marker, _, invoke = firstboot
    path = root / "etc/systemd/system.attached"
    path.parent.mkdir(parents=True)
    if kind == "root-file":
        path.write_text("foreign")
    elif kind == "root-symlink":
        path.symlink_to(root / "usr/lib/systemd/system")
    elif kind == "ancestor-writable":
        path.parent.chmod(0o777)
    else:
        path.mkdir()
        dropin = path / "jeos-.service.d"
        if kind == "dropin-file":
            dropin.write_text("foreign")
        else:
            dropin.symlink_to(root / "usr/lib/systemd/system")
    assert invoke().returncode != 0
    assert marker.exists()
