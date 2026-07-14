"""``live_vm``-gated real-bytes proof that drgn opens a ppc64le vmcore (#1150, ADR-0348).

The offline drgn vmcore path is arch-opaque by construction (ADR-0348): drgn reads the target
architecture from the core's ELF header + DWARF, and the arch-parameterized unit tests prove the
*orchestration* is arch-blind. Those fakes cannot prove drgn actually decodes real ppc64le bytes.
This test does: it opens the retained real #1148 ppc64le vmcore with drgn and asserts drgn
identifies it as ppc64le **specifically** (``Architecture.PPC64`` + little-endian), that its
VMCOREINFO ``BUILD-ID=`` note reads, and that the bytes are the exact pinned #1148 artifact.

It needs **no debuginfo** — the platform arch and VMCOREINFO note are read from the core alone.
The full structural read (task list / by-name symbols) requires a DWARF ``vmlinux`` and is
deferred (spec AC1b); this proves only the open + identification.

**Durability is within the live suite, not CI.** The 86 MiB core cannot ship to CI, so — like
every ``live_vm`` test here — this runs in ``just test-live`` on a host holding the retained core.
Run it with ``KDIVE_PPC64LE_VMCORE`` pointing at the retained core; see
``docs/design/2026-07-14-drgn-vmcore-ppc64le-proof-record-1150.md``.

**Skip vs. fail discipline (a skip must be distinguishable from a pass):** it skips **only** when
``KDIVE_PPC64LE_VMCORE`` is unset. When the env is set but the file is missing/unreadable or its
digest/size mismatch the pins, it **fails loudly** — a mis-provisioned runner is a failure, not a
silent "no core".
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from kdive.providers.shared.debug_common.drgn_program import read_vmcoreinfo_build_id

_CORE_ENV = "KDIVE_PPC64LE_VMCORE"

# Authoritative pins for the retained #1148 ppc64le core (run 9359253e-…), captured under TCG.
# The size independently corroborates #1148's own recorded core size. On re-capture, recompute
# both and update them here (authoritative) and in the proof record, in one commit.
_PINNED_SHA256 = "bd322c68c540542484cde32df94d3e074874374a1eb2ca50551e808f4c7190fa"  # noqa: E501  # pragma: allowlist secret
_PINNED_SIZE = 90463884
_EXPECTED_BUILD_ID = "06466f9617cff9e5a762af9216bfc23837310b9c"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.mark.live_vm
def test_ppc64le_vmcore_opens_and_is_identified_as_ppc64le() -> None:  # pragma: no cover - live_vm
    """drgn opens the real #1148 ppc64le core and identifies it as ppc64le (no debuginfo).

    Skips only when ``KDIVE_PPC64LE_VMCORE`` is unset; fails loudly on a set-but-wrong fixture.
    """
    core_path = os.environ.get(_CORE_ENV)
    if not core_path:
        pytest.skip(
            f"{_CORE_ENV} unset; set it to the retained #1148 ppc64le core (see "
            "docs/design/2026-07-14-drgn-vmcore-ppc64le-proof-record-1150.md)"
        )

    # Env is set → any problem below is a failure, not a skip: a mis-provisioned runner must not
    # masquerade as "no core".
    core = Path(core_path)
    assert core.is_file(), f"{_CORE_ENV}={core_path} does not point at a readable file"

    actual_size = core.stat().st_size
    assert actual_size == _PINNED_SIZE, (
        f"core size {actual_size} != pinned {_PINNED_SIZE}: if you just re-captured the core, "
        "recompute and update _PINNED_SIZE/_PINNED_SHA256; otherwise the core at this path is "
        "swapped or truncated"
    )
    actual_sha = _sha256(core)
    assert actual_sha == _PINNED_SHA256, (
        f"core SHA-256 {actual_sha} != pinned {_PINNED_SHA256}: if you just re-captured the core, "
        "recompute and update _PINNED_SHA256/_PINNED_SIZE; otherwise the core at this path is "
        "swapped or corrupt"
    )

    import drgn  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided

    prog = drgn.Program()  # ty: ignore[unresolved-attribute]  # drgn ships no stubs (C extension)
    prog.set_core_dump(os.fspath(core))

    # drgn identifies the target as ppc64le specifically: the arch enum has no LE/BE variant, so
    # the little-endian flag is what separates ppc64le from the out-of-scope big-endian ppc64.
    platform = prog.platform
    assert platform is not None
    assert platform.arch == drgn.Architecture.PPC64
    assert drgn.PlatformFlags.IS_LITTLE_ENDIAN in platform.flags

    # The VMCOREINFO BUILD-ID note reads via the production helper (raises on absence).
    build_id = read_vmcoreinfo_build_id(bytes(prog["VMCOREINFO"].value_()))
    assert build_id == _EXPECTED_BUILD_ID
