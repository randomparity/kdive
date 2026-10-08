"""Run the native Wicked adapter with only filesystem and external tools isolated."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[2] / "deploy/remote-libvirt-guest-helpers"
ADAPTER = ASSETS / "kdive-wicked-ssh-return-route"


@pytest.fixture
def guest(tmp_path: Path):
    state = tmp_path / "wicked"
    state.mkdir()
    bins = tmp_path / "bin"
    bins.mkdir()
    calls = tmp_path / "calls"
    source = ADAPTER.read_text()
    for name, value in {
        "STATE_ROOT": str(state),
        "LOCK": str(tmp_path / "lock"),
        "DELEGATE": str(bins / "delegate"),
        "HELPER": str(bins / "helper"),
        "POLICY": str(tmp_path / "policy.xml"),
    }.items():
        line = next(line for line in source.splitlines() if line.startswith(name + " ="))
        source = source.replace(line, name + " = " + repr(value), 1)
    script = tmp_path / "netconfig"
    script.write_text(source)
    tool = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as output:
    output.write(json.dumps([name, *args]) + "\\n")
if os.environ.get("FAIL") == name:
    sys.exit(23)
if name == "ip":
    if "route" in args and "show" in args and "2291" in args:
        sys.exit(2)  # Linux has no FIB table before the first return route.
    if "address" in args: print(os.environ["ADDRESSES"])
    elif "link" in args:
        print(json.dumps([
            {"ifname":"lo", "link_type":"[772]" if "-N" in args else "loopback"},
            {"ifname":"ethA", "link_type":"[1]" if "-N" in args else "ether"},
        ]))
    elif "rule" in args: print(os.environ.get("RULES", "[]"))
    elif "all" in args:
        print(json.dumps([dict(r, table=2291) for r in json.loads(os.environ.get("ROUTES", "[]"))]))
    elif "show" in args: print(os.environ.get("DEFAULTS", "[]"))
"""
    for name in ["ip", "delegate", "helper", "wicked"]:
        p = bins / name
        p.write_text(tool)
        p.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bins}:{Path(sys.executable).parent}:/usr/bin:/bin",
        "CALLS": str(calls),
        "ADDRESSES": json.dumps(
            [{"ifname": "ethA", "addr_info": [{"family": "inet", "local": "10.0.2.15"}]}]
        ),
    }
    return script, state, calls, env


def lease(guest, address="10.0.2.15/24", iface="ethA"):
    path = guest[1] / f"leaseinfo.{iface}.dhcp.ipv4"
    path.write_text(f"INTERFACE='{iface}'\nTYPE='dhcp'\nFAMILY='ipv4'\nIPADDR='{address}'\n")
    return path


def run(guest, *args, **env):
    script, _, calls, base = guest
    calls.write_text("")
    result = subprocess.run(
        [sys.executable, str(script), *args],
        env={**base, **env},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, [json.loads(line) for line in calls.read_text().splitlines()]


def install(guest, path, **env):
    return run(guest, "install", "-i", "ethA", "-t", "dhcp", "-f", "ipv4", str(path), "info", **env)


def test_native_install_delegates_then_applies_only_matching_dhcp_default(guest):
    p = lease(guest)
    result, calls = install(guest, p, DEFAULTS='[{"dst":"default"}]')
    assert result.returncode == 0, result.stderr
    assert calls[0][0] == "delegate"
    assert ["helper", "ethA", "up"] in calls
    assert [
        "ip",
        "-4",
        "route",
        "del",
        "default",
        "proto",
        "dhcp",
        "via",
        "10.0.2.2",
        "dev",
        "ethA",
        "table",
        "main",
    ] in calls


def test_empty_native_batch_probe_does_not_query_or_mutate_routes(guest):
    batch = guest[1] / "batch.empty"
    batch.write_text("")
    result, calls = run(guest, "batch", str(batch), "info")
    assert result.returncode == 0, result.stderr
    assert calls == [["delegate", "batch", str(batch), "info"]]


def test_native_batch_modify_reconciles_current_lease(guest):
    p = lease(guest)
    batch = guest[1] / "batch.native"
    batch.write_text(f"modify -i ethA -s wicked-dhcp-ipv4 -I {p}\nupdate\n")
    result, calls = run(guest, "batch", str(batch), "info")
    assert result.returncode == 0, result.stderr
    assert calls[0] == ["delegate", "batch", str(batch), "info"]
    assert ["helper", "ethA", "up"] in calls


@pytest.mark.parametrize("change", ["malformed", "duplicate", "mismatch", "symlink", "stale"])
def test_invalid_lease_never_mutates_routes(guest, change):
    p = lease(guest)
    env = {}
    if change == "malformed":
        p.write_text(p.read_text().replace("10.0.2.15/24", "10.0.2.999/24"))
    elif change == "duplicate":
        p.write_text(p.read_text() + "IPADDR='10.0.2.16/24'\n")
    elif change == "mismatch":
        p.write_text(p.read_text().replace("INTERFACE='ethA'", "INTERFACE='other'"))
    elif change == "symlink":
        target = p.with_name("actual")
        p.rename(target)
        p.symlink_to(target)
    else:
        env["ADDRESSES"] = "[]"
    result, calls = install(guest, p, **env)
    assert result.returncode != 0
    assert not any(c[0] == "helper" or "del" in c for c in calls)


def test_delegate_failure_stops_before_kernel_queries(guest):
    result, calls = install(guest, lease(guest), FAIL="delegate")
    assert result.returncode == 23
    assert len(calls) == 1


def test_foreign_table_is_preserved(guest):
    result, calls = install(guest, lease(guest), ROUTES='[{"dst":"192.0.2.0/24","dev":"other"}]')
    assert result.returncode != 0
    assert not any(c[0] == "helper" or "del" in c for c in calls)


def test_unrelated_remove_preserves_existing_owner(guest):
    lease(guest)
    result, calls = run(guest, "remove", "-i", "other", "-t", "dhcp", "-f", "ipv4")
    assert result.returncode == 0, result.stderr
    assert ["helper", "other", "down"] not in calls


def test_startup_registers_then_enables_actual_ethernet_before_replay(guest):
    result, calls = run(guest, "startup")
    assert result.returncode == 0, result.stderr
    assert calls[0] == ["wicked", "nanny", "addpolicy", str(guest[0].parent / "policy.xml")]
    assert ["wicked", "nanny", "enable", "ethA"] in calls
    assert ["wicked", "nanny", "enable", "lo"] not in calls
    assert not any("recheck" in c for c in calls)


def test_batch_injection_is_data(guest):
    batch = guest[1] / "batch.bad"
    marker = guest[1] / "injected"
    batch.write_text(f"modify -i ethA;touch {shlex.quote(str(marker))} -s wicked-dhcp-ipv4\n")
    result, calls = run(guest, "batch", str(batch), "info")
    assert result.returncode != 0
    assert not marker.exists()
    assert not any(c[0] == "helper" for c in calls)


def test_rendered_native_batch_command_survives_pinned_selector():
    import xml.etree.ElementTree as ET

    config = ET.parse(ASSETS / "kdive-wicked-server-local.xml")
    commands = {e.attrib["name"]: e.attrib["command"] for e in config.findall(".//action")}
    assert Path(commands["batch"]).name == "netconfig batch"
    assert commands["batch"].split()[-1] == "batch"
    assert commands["install"].endswith("/netconfig install")


def test_policy_uses_link_type_and_existing_native_service():
    policy = (ASSETS / "kdive-wicked-policy.xml").read_text()
    assert "<link-type>ethernet</link-type>" in policy
    assert "<ipv4:dhcp>" in policy
    assert "<ipv6>" not in policy and "<device>" not in policy
    startup = (ASSETS / "kdive-wicked-startup.conf").read_text()
    assert "ExecStartPost=/usr/local/libexec/kdive-wicked/netconfig startup" in startup


def test_batch_remove_after_modify_uses_final_native_state(guest):
    path = guest[1] / "leaseinfo.ethA.dhcp.ipv4"
    batch = guest[1] / "batch.remove"
    batch.write_text(
        f"modify -i ethA -s wicked-dhcp-ipv4 -I {path}\n"
        "remove -i ethA -s wicked-dhcp-ipv4\nupdate\n"
    )
    result, calls = run(
        guest,
        "batch",
        str(batch),
        "info",
        ROUTES='[{"dst":"default","gateway":"10.0.2.2","dev":"ethA"}]',
    )
    assert result.returncode == 0, result.stderr
    assert ["helper", "ethA", "down"] in calls


@pytest.mark.parametrize("action", ["remove", "startup"])
def test_obsolete_rule_cleanup_requires_no_current_address(guest, action):
    args = (
        ["startup"] if action == "startup" else ["remove", "-i", "ethA", "-t", "dhcp", "-f", "ipv4"]
    )
    rule = '[{"priority":2291,"src":"10.0.2.15","table":2291}]'
    result, calls = run(guest, *args, RULES=rule)
    assert result.returncode != 0
    assert not any("del" in c for c in calls)
    result, calls = run(guest, *args, RULES=rule, ADDRESSES="[]")
    assert result.returncode == 0, result.stderr
    assert [
        "ip",
        "-4",
        "rule",
        "del",
        "pref",
        "2291",
        "from",
        "10.0.2.15",
        "table",
        "2291",
    ] in calls


@pytest.mark.parametrize("family,kind", [("ipv6", "dhcp"), ("ipv4", "static")])
def test_other_native_families_delegate_without_our_kernel_queries(guest, family, kind):
    result, calls = run(guest, "remove", "-i", "ethA", "-t", kind, "-f", family)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1 and calls[0][0] == "delegate"


def test_ambiguous_interfaces_fail_before_shared_helper(guest):
    lease(guest)
    lease(guest, "10.0.2.16/24", "ethB")
    addresses = json.dumps(
        [
            {"ifname": name, "addr_info": [{"family": "inet", "local": addr}]}
            for name, addr in [("ethA", "10.0.2.15"), ("ethB", "10.0.2.16")]
        ]
    )
    result, calls = run(guest, "startup", ADDRESSES=addresses)
    assert result.returncode != 0
    assert not any(c[0] == "helper" for c in calls)


def test_missing_default_preserves_static_routes(guest):
    result, calls = install(guest, lease(guest), DEFAULTS="[]")
    assert result.returncode == 0, result.stderr
    assert not any("del" in c for c in calls)


@pytest.mark.parametrize("failure", ["ip", "helper"])
def test_native_route_failures_are_visible(guest, failure):
    result, _ = install(guest, lease(guest), FAIL=failure)
    assert result.returncode == 23
    assert "kdive Wicked return route" in result.stderr


def prepare_image(tmp_path, *, foreign="", fail=""):
    import re
    import shutil

    import yaml

    role = ASSETS.parent / "ansible/roles/guest_base_image/tasks/build_one.yml"
    tasks = yaml.safe_load(role.read_text())
    task = next(t for t in tasks if t["name"].startswith("Install the Leap native network"))
    assert "image.distro == 'opensuse-leap'" in task["when"]
    argv = task["ansible.builtin.command"]["argv"]
    script = argv[-1]
    root = tmp_path / "image"
    for directory in ["tmp", "etc/wicked/extensions", "etc/sysconfig/network", "bin"]:
        (root / directory).mkdir(parents=True, exist_ok=True)
    for file in [
        "kdive-ssh-return-route",
        "kdive-wicked-ssh-return-route",
        "kdive-wicked-policy.xml",
        "kdive-wicked-server-local.xml",
        "kdive-wicked-startup.conf",
    ]:
        shutil.copyfile(ASSETS / file, root / "tmp" / file)
    delegate = root / "etc/wicked/extensions/netconfig"
    delegate.write_text("#!/bin/sh\nexit 0\n")
    delegate.chmod(0o755)
    (root / "etc/sysconfig/network/ifcfg-lo").write_text("stock loopback")
    if foreign:
        p = root / foreign
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("foreign policy")
    script = re.sub(
        r"/(?:etc|usr/local|usr/lib|run|var/lib|tmp)/", lambda match: str(root) + match[0], script
    )
    bins = root / "bin"
    for name in ["wicked", "ip", "systemctl", "sha256sum"]:
        p = bins / name
        p.write_text(f"#!/bin/sh\n[ '{name}' != '{fail}' ] || exit 23\n")
        p.chmod(0o755)
    installer = bins / "install"
    installer.write_text("""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
for option in ['-o', '-g']:
    if option in args:
        i = args.index(option)
        del args[i:i+2]
os.execv('/usr/bin/install', ['install', *args])
""")
    installer.chmod(0o755)
    result = subprocess.run(
        ["/bin/sh", "-c", script],
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": f"{bins}:{Path(sys.executable).parent}:/usr/bin:/bin"},
        check=False,
    )
    return result, root


def test_canonical_policy_installation_and_idempotent_owned_paths(tmp_path):
    for _ in range(2):
        result, root = prepare_image(tmp_path)
        assert result.returncode == 0, result.stderr
    assert (
        root / "etc/cloud/cloud.cfg.d/99-kdive-network-policy.cfg"
    ).read_text() == "network: {config: disabled}\n"
    assert (root / "usr/local/libexec/kdive-wicked/netconfig").stat().st_mode & 0o777 == 0o755
    assert not (root / "usr/local/libexec/kdive-wicked-ssh-return-route").exists()
    assert (root / "etc/sysconfig/network/ifcfg-lo").read_text() == "stock loopback"


@pytest.mark.parametrize(
    "foreign",
    [
        "etc/sysconfig/network/ifcfg-ethA",
        "etc/wicked/client-local.xml",
        "etc/wicked/server-local.xml",
        "etc/wicked/ifconfig/foreign.xml",
        "etc/wicked/kdive-ethernet-policy.xml",
        "etc/systemd/system/wickedd-nanny.service.d/foreign.conf",
    ],
)
def test_foreign_policy_refuses_before_owned_installation(tmp_path, foreign):
    result, root = prepare_image(tmp_path, foreign=foreign)
    assert result.returncode != 0
    assert "foreign" in result.stderr
    assert (root / foreign).read_text() == "foreign policy"
    assert not (root / "usr/local/libexec/kdive-wicked/netconfig").exists()


@pytest.mark.parametrize("failure", ["sha256sum", "systemctl"])
def test_image_native_failures_are_not_hidden(tmp_path, failure):
    result, _ = prepare_image(tmp_path, fail=failure)
    assert result.returncode == 23


def test_coalesced_batch_transfers_only_obsolete_source_ownership(guest):
    new = lease(guest, "10.0.2.16/24", "ethB")
    batch = guest[1] / "batch.transfer"
    batch.write_text(
        f"remove -i ethA -s wicked-dhcp-ipv4\nmodify -i ethB -s wicked-dhcp-ipv4 -I {new}\nupdate\n"
    )
    env = {
        "ADDRESSES": json.dumps(
            [
                {"ifname": "ethA", "addr_info": [{"family": "inet", "local": "192.0.2.9"}]},
                {"ifname": "ethB", "addr_info": [{"family": "inet", "local": "10.0.2.16"}]},
            ]
        ),
        "ROUTES": '[{"dst":"default","gateway":"10.0.2.2","dev":"ethA"}]',
        "RULES": '[{"priority":2291,"src":"10.0.2.15","table":2291}]',
    }
    result, calls = run(guest, "batch", str(batch), "info", **env)
    assert result.returncode == 0, result.stderr
    assert ["helper", "ethB", "up"] in calls
    env["ADDRESSES"] = env["ADDRESSES"].replace("192.0.2.9", "10.0.2.15")
    result, calls = run(guest, "batch", str(batch), "info", **env)
    assert result.returncode != 0
    assert not any(c[0] == "helper" for c in calls)


@pytest.mark.parametrize(
    "records",
    [
        "update extra\n",
        "update\nupdate\n",
        "update\nremove -i ethA -s wicked-dhcp-ipv4\n",
        "remove -i ethA -s wicked-dhcp-ipv4\n",
    ],
)
def test_invalid_batch_update_directives_refuse_route_changes(guest, records):
    batch = guest[1] / "batch.invalid-control"
    batch.write_text(records)
    result, calls = run(guest, "batch", str(batch), "info")
    assert result.returncode != 0
    assert calls == [["delegate", "batch", str(batch), "info"]]


def test_native_batch_remove_terminal_update(guest):
    batch = guest[1] / "batch.remove-only"
    batch.write_text("remove -i ethA -s wicked-dhcp-ipv4\nupdate\n")
    result, calls = run(guest, "batch", str(batch), "info")
    assert result.returncode == 0, result.stderr
    assert calls[0] == ["delegate", "batch", str(batch), "info"]
