"""Shared session doubles for the local external-boot session tests.

A domain support module, not a collected test module: `test_test_module_dependencies.py`
forbids a test importing helpers defined inside another test, so the doubles two test modules
need are defined here exactly once rather than duplicated.
"""

from __future__ import annotations

from uuid import UUID

from kdive.providers.ports.external_boot import ExternalBootActivationBinding

SYSTEM_ID = UUID("11111111-1111-1111-1111-111111111111")
BINDING = ExternalBootActivationBinding(
    system_id=str(SYSTEM_ID),
    run_id="22222222-2222-2222-2222-222222222222",
    activation_id="33333333-3333-3333-3333-333333333333",
)
OVERLAY = f"/var/lib/kdive/rootfs/{SYSTEM_ID}-overlay.qcow2"
ACTIVATION_ID = UUID(BINDING.activation_id)


def _xml(
    *,
    overlay: str = OVERLAY,
    system_id: UUID = SYSTEM_ID,
    channel: str = "valid",
) -> str:
    channels = {
        "valid": (
            '<channel type="unix"><target type="virtio" name="org.qemu.guest_agent.0"/></channel>'
        ),
        "absent": "",
        "duplicate": (
            '<channel type="unix"><target type="virtio" '
            'name="org.qemu.guest_agent.0"/></channel>' * 2
        ),
        "malformed": (
            '<channel type="unix"><target type="pty" name="org.qemu.guest_agent.0"/></channel>'
        ),
    }[channel]
    return (
        "<domain><name>kdive-" + str(system_id) + "</name><metadata>"
        '<kdive:system xmlns:kdive="https://kdive.dev/libvirt/1">'
        + str(system_id)
        + "</kdive:system></metadata><os><kernel>/old</kernel><cmdline>root=x</cmdline></os>"
        '<devices><disk type="file" device="disk"><driver name="qemu" type="qcow2"/>'
        f'<source file="{overlay}"/><target dev="vda" bus="virtio"/></disk>'
        f"{channels}</devices></domain>"
    )


class Domain:
    def __init__(
        self,
        events: list[str],
        xml: str | None = None,
        *,
        inactive_xml: str | None = None,
    ) -> None:
        self.events = events
        self.xml = xml or _xml()
        self.inactive_xml = inactive_xml or self.xml
        self.active = False

    def name(self) -> str:
        start = self.xml.index("<name>") + len("<name>")
        return self.xml[start : self.xml.index("</name>", start)]

    def XMLDesc(self, flags: int) -> str:  # noqa: N802
        self.events.append(f"domain.xml:{flags}")
        return self.inactive_xml if flags == 2 else self.xml

    def isActive(self) -> int:  # noqa: N802
        self.events.append("domain.active")
        return int(self.active)

    def destroy(self) -> int:
        self.events.append("domain.destroy")
        self.active = False
        return 0

    def create(self) -> int:
        self.events.append("domain.create")
        self.active = True
        return 0

    def free(self) -> None:
        self.events.append("domain.close")


class Conn:
    def __init__(self, events: list[str], domain: Domain) -> None:
        self.events = events
        self.domain = domain

    def lookupByName(self, name: str) -> Domain:  # noqa: N802
        self.events.append(f"domain.open:{name}")
        return self.domain

    def defineXML(self, xml: str) -> Domain:  # noqa: N802
        self.events.append("domain.define")
        self.domain.xml = xml
        return self.domain

    def close(self) -> None:
        self.events.append("connection.close")


class Guest:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def add_drive_opts(self, overlay: str, *, format: str) -> None:
        self.events.append(f"guest.drive:{overlay}:{format}")

    def launch(self) -> None:
        self.events.append("guest.launch")

    def inspect_os(self) -> list[str]:
        self.events.append("guest.inspect")
        return ["/dev/sda1"]

    def mount(self, device: str, mountpoint: str) -> None:
        self.events.append(f"guest.mount:{device}:{mountpoint}")

    def shutdown(self) -> None:
        self.events.append("guest.shutdown")

    def close(self) -> None:
        self.events.append("guest.close")

    def exists(self, path: str) -> int:
        self.events.append(f"guest.exists:{path}")
        return 1

    def is_dir(self, path: str, *, followsymlinks: bool) -> int:
        return int(bool(path) and not followsymlinks)

    def lstatns(self, path: str) -> dict[str, int]:
        return {"st_mode": len(path)}

    def readlink(self, path: str) -> str:
        return path

    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]:
        return [{"attrname": path}]

    def download(self, remotefilename: str, filename: str) -> None:
        self.events.append(f"download:{remotefilename}:{filename}")

    def mkdir(self, path: str) -> None:
        self.events.append(f"mkdir:{path}")

    def upload(self, filename: str, remotefilename: str) -> None:
        self.events.append(f"upload:{filename}:{remotefilename}")

    def ln_s(self, target: str, linkname: str) -> None:
        self.events.append(f"ln:{target}:{linkname}")

    def chmod(self, mode: int, path: str) -> None:
        self.events.append(f"chmod:{mode}:{path}")

    def chown(self, owner: int, group: int, path: str) -> None:
        self.events.append(f"chown:{owner}:{group}:{path}")

    def lsetxattr(self, xattr: str, val: bytes, vallen: int, path: str) -> None:
        self.events.append(f"xattr:{xattr}:{val!r}:{vallen}:{path}")

    def rm_rf(self, path: str) -> None:
        self.events.append(f"rm:{path}")

    def mv(self, source: str, destination: str) -> None:
        self.events.append(f"mv:{source}:{destination}")

    def sync(self) -> None:
        self.events.append("guest.sync")

    def find0(self, directory: str, files: str) -> None:
        del directory, files
        raise AssertionError("test must provide an instrumented find0 producer")

    def user_cancel(self) -> None:
        self.events.append("guest.cancel")

    def last_errno(self) -> int:
        return 0
