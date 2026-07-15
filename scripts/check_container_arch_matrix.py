"""Guard the container arch-support matrix against docker-compose drift (ADR-0356).

The developer stack (`docker-compose.yml`) must stay in lockstep with the authoritative
arch-support matrix embedded in ADR-0356. This guard fences that, and makes the ppc64le
core-loop invariant machine-checkable rather than a prose promise, by asserting a
per-handling-token obligation on every matrix row:

1. **Set equality** — the compose `image:` set equals the matrix image set (drift in either
   direction is named).
2. **Handling validity** — every row's handling is one of ``rely-on-upstream`` / ``mirror`` /
   ``build-local`` / ``accept-gap``.
3. **Arch alphabet + ``rely-on-upstream`` ⟹ ppc64le** — each arch cell is one of ``✅`` / ``❌``
   / ``—``, and a ``rely-on-upstream`` row's ppc64le cell is exactly ``✅`` (fail-closed).
4. **``accept-gap`` ⟹ opt-in only** — the image is used by no default-profile (un-profiled)
   service, so a knowingly-accepted gap cannot mask a broken core-loop image.
5. **``mirror`` ⟹ tracked** — the row cites a tracking issue (``#NNNN``), so a default-profile
   gap under a ``mirror`` label is a visible follow-up, not a silent bypass of assertion 3.
6. **``build-local`` ⟹ actually built** — a compose service using the image has a ``build:``
   key, so the token cannot be borrowed by a pulled upstream image to dodge assertion 3.

Compose is parsed with ``yaml.safe_load`` (PyYAML is a hard dependency), which resolves the
file's anchors, merge keys, and block scalars; the guard reads only the ``services`` mapping.
The matrix is the ``<!-- arch-matrix:begin -->`` … ``<!-- arch-matrix:end -->`` block in the
ADR, parsed as a Markdown table. Run via ``uv run python scripts/check_container_arch_matrix.py``
(``just container-arch-check``). Exit 0 clean, 1 on any violation or a malformed matrix.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = _ROOT / "docker-compose.yml"
ADR_PATH = _ROOT / "docs" / "adr" / "0356-cross-platform-dev-containers.md"

_BEGIN = "<!-- arch-matrix:begin -->"
_END = "<!-- arch-matrix:end -->"

HANDLING = frozenset({"rely-on-upstream", "mirror", "build-local", "accept-gap"})
ARCHES = ("amd64", "arm64", "ppc64le")  # the three arch columns, one source for the checks
ARCH_ALPHABET = frozenset({"✅", "❌", "—"})  # published / not published / not applicable
_ISSUE_REF = re.compile(r"#\d+")


@dataclass(frozen=True)
class ImageInfo:
    """How a compose image is used, aggregated across the services that reference it."""

    default_profile: bool  # some using service has no ``profiles:`` key (`docker compose up`)
    built: bool  # some using service has a ``build:`` key


@dataclass(frozen=True)
class MatrixRow:
    """One data row of the ADR arch-support matrix."""

    image: str
    amd64: str
    arm64: str
    ppc64le: str
    handling: str
    raw: str  # the whole row text, scanned for a ``mirror`` tracking-issue reference


def parse_compose(text: str) -> dict[str, ImageInfo]:
    """Map each compose image reference to how its services use it.

    Reads only the top-level ``services`` mapping, so top-level ``x-*`` anchors and
    ``volumes:`` are ignored by construction.
    """
    data = yaml.safe_load(text) or {}
    services = data.get("services", {}) if isinstance(data, dict) else {}
    infos: dict[str, ImageInfo] = {}
    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        image = svc.get("image")
        if not image:
            continue
        # Docker Compose starts a service on a bare `up` when its profiles list is empty or
        # absent (len==0). A falsy `profiles` (missing / None / []) therefore means
        # default-profile; only a non-empty list gates it opt-in.
        prev = infos.get(image, ImageInfo(False, False))
        infos[image] = ImageInfo(
            default_profile=prev.default_profile or not svc.get("profiles"),
            built=prev.built or "build" in svc,
        )
    return infos


def _matrix_block(adr_text: str) -> str:
    """Return the text between the arch-matrix markers, or raise if they are absent."""
    if _BEGIN not in adr_text or _END not in adr_text:
        raise ValueError("arch matrix: begin/end markers not found in the ADR")
    start = adr_text.index(_BEGIN) + len(_BEGIN)
    end = adr_text.index(_END)
    if end < start:
        raise ValueError("arch matrix: end marker precedes begin marker")
    return adr_text[start:end]


def _cells(row: str) -> list[str]:
    """Split a Markdown table row into its trimmed cells."""
    return [c.strip() for c in row.strip().strip("|").split("|")]


def parse_matrix(adr_text: str) -> list[MatrixRow]:
    """Parse the ADR arch-support matrix block into rows.

    A data row is one whose first cell is a backtick-wrapped image ref; header and separator
    rows are skipped. Raises ``ValueError`` on any malformed shape (missing markers, missing
    required column, a short data row, or no data rows) so the guard fails loudly, never
    vacuously.
    """
    lines = [ln for ln in _matrix_block(adr_text).splitlines() if ln.strip().startswith("|")]
    header = next((ln for ln in lines if "Handling" in ln), None)
    if header is None:
        raise ValueError("arch matrix: no header row with a 'Handling' column")
    index = {name: i for i, name in enumerate(_cells(header))}
    for required in (*ARCHES, "Handling"):
        if required not in index:
            raise ValueError(f"arch matrix: header is missing the '{required}' column")
    rows: list[MatrixRow] = []
    for line in lines:
        cells = _cells(line)
        if "`" not in cells[0]:  # header / separator, not a data row
            continue
        if len(cells) <= index["Handling"]:
            raise ValueError(f"arch matrix: data row has too few cells: {line.strip()!r}")
        rows.append(
            MatrixRow(
                image=cells[0].strip("`").strip(),
                amd64=cells[index["amd64"]],
                arm64=cells[index["arm64"]],
                ppc64le=cells[index["ppc64le"]],
                handling=cells[index["Handling"]],
                raw=line,
            )
        )
    if not rows:
        raise ValueError("arch matrix: no data rows found between the markers")
    return rows


def _check_set_equality(images: dict[str, ImageInfo], rows: list[MatrixRow]) -> list[str]:
    compose_set = set(images)
    matrix_set = {r.image for r in rows}
    out = [
        f"compose image {img!r} is missing from the ADR-0356 arch matrix"
        for img in sorted(compose_set - matrix_set)
    ]
    out += [
        f"matrix row {img!r} has no matching compose service"
        for img in sorted(matrix_set - compose_set)
    ]
    return out


def _check_arch_alphabet(row: MatrixRow) -> list[str]:
    out: list[str] = []
    for label in ARCHES:
        cell = getattr(row, label)
        if cell not in ARCH_ALPHABET:
            out.append(f"{row.image}: {label} cell {cell!r} not in {sorted(ARCH_ALPHABET)}")
    return out


def _check_obligation(row: MatrixRow, images: dict[str, ImageInfo]) -> list[str]:
    if row.handling not in HANDLING:
        return [
            f"{row.image}: unknown handling token {row.handling!r} (allowed: {sorted(HANDLING)})"
        ]
    if row.handling == "rely-on-upstream" and row.ppc64le != "✅":
        return [f"{row.image}: rely-on-upstream requires ppc64le ✅, found {row.ppc64le!r}"]
    info = images.get(row.image)
    if info is None:  # drift already reported by set-equality; skip usage-based checks
        return []
    if row.handling == "accept-gap" and info.default_profile:
        return [
            f"{row.image}: accept-gap is allowed only behind an opt-in profile "
            "(image is default-profile)"
        ]
    if row.handling == "mirror" and not _ISSUE_REF.search(row.raw):
        return [f"{row.image}: mirror row must cite a tracking issue (#NNNN)"]
    if row.handling == "build-local" and not info.built:
        return [f"{row.image}: build-local requires a compose service with a build: key"]
    return []


def evaluate(compose_text: str, adr_text: str) -> list[str]:
    """Return the list of matrix/compose violations (empty means the contract holds).

    Raises ``ValueError`` when the matrix block itself is malformed (a hard error distinct from
    an ordinary violation).
    """
    images = parse_compose(compose_text)
    rows = parse_matrix(adr_text)
    violations = _check_set_equality(images, rows)
    for row in rows:
        violations += _check_arch_alphabet(row)
        violations += _check_obligation(row, images)
    return violations


def main() -> int:
    try:
        violations = evaluate(
            COMPOSE_PATH.read_text(encoding="utf-8"),
            ADR_PATH.read_text(encoding="utf-8"),
        )
    except (ValueError, yaml.YAMLError) as exc:
        print(f"container-arch-check: {exc}", file=sys.stderr)
        return 1
    for violation in violations:
        print(f"container-arch-check: {violation}", file=sys.stderr)
    if violations:
        print(
            f"container-arch-check: {len(violations)} violation(s); reconcile "
            "docker-compose.yml with the ADR-0356 arch matrix",
            file=sys.stderr,
        )
        return 1
    print("container-arch-check: compose image set matches the ADR-0356 arch matrix.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
