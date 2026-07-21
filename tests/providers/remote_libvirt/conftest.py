"""Shared test doubles for the remote-libvirt provider suite."""

from __future__ import annotations

import libvirt

# Re-export the disposable-Postgres fixtures so console-wiring tests can register an
# artifacts row against a migrated schema (ADR-0095 part-store assembly).
from tests.db.conftest import _migrated_db, migrated_url, pg_conn, postgres_url

__all__ = ["_migrated_db", "migrated_url", "pg_conn", "postgres_url"]


def libvirt_error(code: int) -> libvirt.libvirtError:
    """Build a libvirtError whose get_error_code() returns ``code``.

    Duplicated from the local-libvirt fakes deliberately — no shared layer
    (ADR-0076).
    """
    err = libvirt.libvirtError("synthetic")
    # get_error_code() reads self.err[0]; libvirtError leaves err=None with no live error.
    err.err = (code, 0, "synthetic", 0, "", None, None, 0, 0)
    return err


class RecordingBackend:
    """SecretBackend test double returning a distinct PEM body per ref."""

    def __init__(self) -> None:
        self.resolved: list[str] = []

    def resolve(self, ref: str) -> str:
        self.resolved.append(ref)
        return f"PEM::{ref}"


# A host-model domain-capabilities block for a v3 Skylake host with avx512f (a v4 feature) disabled
# — exercises the ADR-0368 host_cpu advertisement and the disable-guard (v3 survives, v4 would not).
_DEFAULT_DOMCAPS = (
    "<domainCapabilities><cpu>"
    "<mode name='host-passthrough' supported='yes'/>"
    "<mode name='host-model' supported='yes'>"
    "<model fallback='forbid'>Skylake-Client-IBRS</model>"
    "<vendor>Intel</vendor>"
    "<feature policy='require' name='ssse3'/>"
    "<feature policy='disable' name='avx512f'/>"
    "</mode></cpu></domainCapabilities>"
)


class FakeConn:
    """The slice of a libvirt connection the remote provider uses.

    ``domcaps_xml``/``domcaps_error`` configure the ADR-0368 host-model advertisement: the default
    advertises a Skylake (x86-64-v3) host; pass a custom string to vary it (e.g. a host-model block
    with no ``<model>``), or ``domcaps_error=True`` to make ``getDomainCapabilities`` raise.
    """

    def __init__(self, *, domcaps_xml: str = _DEFAULT_DOMCAPS, domcaps_error: bool = False) -> None:
        self.closed = False
        self._domcaps_xml = domcaps_xml
        self._domcaps_error = domcaps_error
        self.domcaps_call: tuple[object, ...] | None = None

    def getInfo(self) -> list[object]:  # noqa: N802 - libvirt binding name
        return ["x86_64", 16384, 8, 2400, 1, 1, 8, 1]

    def getCapabilities(self) -> str:  # noqa: N802 - libvirt binding name
        return "<capabilities><host><cpu><arch>x86_64</arch></cpu></host></capabilities>"

    def getDomainCapabilities(  # noqa: N802 - libvirt binding name
        self,
        emulatorbin: str | None = None,
        arch: str | None = None,
        machine: str | None = None,
        virttype: str | None = None,
        flags: int = 0,
    ) -> str:
        self.domcaps_call = (emulatorbin, arch, machine, virttype, flags)
        if self._domcaps_error:
            raise libvirt.libvirtError("synthetic getDomainCapabilities failure")
        return self._domcaps_xml

    def close(self) -> None:
        self.closed = True
