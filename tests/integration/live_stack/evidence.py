"""Coverage evidence seam for live carriers (ADR-0715 over ADR-0686).

A live carrier reads :func:`run_identity` for each required cell, proves its assertions, and
writes one version-1 ``Evidence`` record through :class:`EvidenceWriter`. Assertion artifacts
are content-addressed files beside the records. ``python -m tests.integration.live_stack.evidence
assemble DIR --candidate SHA --out FILE`` merges one candidate's records into the results array
that ``python -m scripts.coverage_campaign qualify`` reads. The qualifier stays the judge; this
module records observations and never decides qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess  # noqa: S404 - fixed argv, no shell  # nosec B404
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from scripts.coverage_campaign.contract import Cell, build_contract, digest
from scripts.coverage_campaign.evidence import Context, Evidence, Outcome, read_results
from tests.integration.live_stack.skew import _fetch_version, _resolve, readyz_urls
from tests.integration.live_stack.spine import report_artifact_dir
from tests.live_vm.installed_local_authority_support import _active_worker_readyz_urls

AUTHORITY_REVISION = "/opt/kdive-provider-authority/revision"
_ROOT = Path(__file__).resolve().parents[3]
_TIMEOUT_S = 10.0


def _output(*argv: str) -> str | None:
    """A fixed command's stripped stdout, or ``None`` when it cannot run or exits non-zero."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell  # nosec B603
            argv, capture_output=True, text=True, timeout=_TIMEOUT_S, check=True
        )
    except (OSError, subprocess.SubprocessError) as _exc:
        return None
    return result.stdout.strip()


def _git(*args: str) -> str | None:
    return _output("git", "-C", str(_ROOT), *args)


def _read_privileged(path: str) -> str | None:
    """Read a root-owned identity file without prompting; ``None`` when it cannot be read."""
    return _output("sudo", "-n", "cat", path)


def _running_workers() -> str:
    units = ("systemctl", "list-units", "kdive-live-worker@*.service", "--state=running")
    return _output(*units, "--no-legend") or ""


@cache
def _matrix() -> str:
    return build_contract().matrix_sha256


def key_values(text: str) -> dict[str, str]:
    """``KEY=VALUE`` lines (``/etc/os-release`` style) with surrounding quotes removed."""
    fields = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip().strip("\"'")
    return fields


def os_identity(os_release: str) -> str:
    """Return the public ``ID:VERSION_ID`` identity from ``/etc/os-release`` text."""
    fields = key_values(os_release)
    if not fields.get("ID") or not fields.get("VERSION_ID"):
        raise ValueError("os-release lacks ID or VERSION_ID; cannot name the platform")
    return f"{fields['ID']}:{fields['VERSION_ID']}"


@dataclass(frozen=True)
class RunIdentity:
    """The candidate and deployed revisions one cell's evidence is bound to (ADR-0715)."""

    candidate_sha: str
    matrix_sha256: str
    host_os: str
    host_arch: str
    clean: bool
    deployed_roles: dict[str, str]


def _resolved(
    version: dict[str, object] | None, resolve: Callable[[str], str | None]
) -> str | None:
    commit = version.get("commit") if version is not None else None
    return resolve(commit) if isinstance(commit, str) else None


def _worker_revision(
    base_url: str,
    running: str,
    head: str,
    fetch: Callable[[str], dict[str, object] | None],
    resolve: Callable[[str], str | None],
) -> str | None:
    """Every running slot's revision; the first non-candidate one when slots disagree."""
    try:
        urls = _active_worker_readyz_urls(base_url, running)
    except AssertionError:
        return None  # no running fixed slot: the worker role is unknown
    revisions = [_resolved(fetch(url), resolve) for _, url in urls]
    if any(revision is None for revision in revisions):
        return None
    return next((r for r in revisions if r != head), revisions[0])


def run_identity(
    base_url: str,
    *,
    git: Callable[..., str | None] = _git,
    fetch: Callable[[str], dict[str, object] | None] = _fetch_version,
    resolve: Callable[[str], str | None] = _resolve,
    read: Callable[[str], str | None] = _read_privileged,
    running_workers: Callable[[], str] = _running_workers,
    matrix: Callable[[], str] = _matrix,
    os_release: Callable[[], str] = lambda: Path("/etc/os-release").read_text(encoding="utf-8"),
) -> RunIdentity:
    """Read the candidate, matrix, host and deployed role revisions for one cell."""
    head = git("rev-parse", "HEAD")
    if not head:
        raise RuntimeError("cannot read the test checkout's git HEAD; run from a git checkout")
    urls = readyz_urls(base_url, {})
    roles: dict[str, str] = {}
    for role in ("server", "reconciler"):
        revision = _resolved(fetch(urls[role]), resolve)
        if revision:
            roles[role] = revision
    worker = _worker_revision(base_url, running_workers(), head, fetch, resolve)
    if worker:
        roles["worker"] = worker
    installed = read(AUTHORITY_REVISION)
    authority = resolve(installed) if installed else None
    if authority:
        roles["authority"] = authority
    return RunIdentity(
        candidate_sha=head,
        matrix_sha256=matrix(),
        host_os=os_identity(os_release()),
        host_arch=platform.machine(),
        clean=git("status", "--porcelain", "--untracked-files=all") == "",
        deployed_roles=roles,
    )


def identity_problems(identity: RunIdentity, roles: Iterable[str]) -> list[str]:
    """Name a dirty checkout and each required role that is missing or not the candidate.

    ``missing:<role>`` lets a scenario still run and record its assertions; ``mismatch:`` and
    ``dirty-checkout`` mean the stack or checkout is not the candidate, so nothing should run.
    """
    problems = [] if identity.clean else ["dirty-checkout"]
    for role in roles:
        revision = identity.deployed_roles.get(role)
        if revision is None:
            problems.append(f"missing:{role}")
        elif revision != identity.candidate_sha:
            problems.append(f"mismatch:{role}:{revision}")
    return problems


def build_record(
    cell: Cell,
    identity: RunIdentity,
    *,
    outcome: Outcome,
    context: Context,
    duration_s: float,
    assertions: dict[str, str],
    impediments: Iterable[str] = (),
    artifacts: Iterable[str] = (),
) -> Evidence:
    """One version-1 record for ``cell``; identity fields come from the cell, never the caller.

    ``artifacts`` adds retained evidence that backs no assertion, such as a cleanup attempt.
    """
    return Evidence.model_validate(
        {
            "version": 1,
            "cell_id": cell.id,
            "scenario_id": cell.scenario_id,
            "node_id": cell.node_id,
            "outcome": outcome.value,
            "candidate_sha": identity.candidate_sha,
            "matrix_sha256": identity.matrix_sha256,
            "input_sha256": digest(context),
            "deployed_roles": {
                role: identity.deployed_roles[role]
                for role in cell.roles
                if role in identity.deployed_roles
            },
            "context": context.model_dump(),
            "duration_seconds": duration_s,
            "assertions": assertions,
            "artifacts": sorted(set(assertions.values()) | set(artifacts)),
            "impediments": list(impediments),
        }
    )


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


class EvidenceWriter:
    """Write content-addressed artifacts and one record per cell under ``root``."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def artifact(self, payload: object) -> str:
        """Store ``payload`` as canonical JSON and return its SHA-256 digest."""
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        sha = hashlib.sha256(data).hexdigest()
        path = self.root / "artifacts" / sha
        if not path.exists():
            _atomic_write(path, data)
        return sha

    def record(self, evidence: Evidence) -> Path:
        """Write ``evidence``, replacing an earlier record for the same cell."""
        name = hashlib.sha256(evidence.cell_id.encode()).hexdigest()
        path = self.root / "records" / f"{name}.json"
        _atomic_write(path, evidence.model_dump_json().encode())
        return path


def evidence_root() -> Path:
    return report_artifact_dir() / "coverage-evidence"


def assemble(root: Path, candidate: str, out: Path) -> tuple[int, int]:
    """Write ``candidate``'s records to ``out`` as one array; return ``(kept, dropped)``.

    The written file is re-read through the qualifier's own reader, so an invalid record fails
    here with its closed ``EvidenceError`` category rather than at qualification.
    """
    records = [json.loads(path.read_bytes()) for path in sorted((root / "records").glob("*.json"))]
    kept = sorted(
        (r for r in records if isinstance(r, dict) and r.get("candidate_sha") == candidate),
        key=lambda r: str(r.get("cell_id")),
    )
    _atomic_write(out, json.dumps(kept, sort_keys=True, indent=1).encode())
    read_results(out)
    return len(kept), len(records) - len(kept)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble one candidate's coverage evidence.")
    commands = parser.add_subparsers(dest="command", required=True)
    merge = commands.add_parser("assemble")
    merge.add_argument("root", type=Path)
    merge.add_argument("--candidate", required=True)
    merge.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    kept, dropped = assemble(args.root, args.candidate, args.out)
    print(f"assembled {kept} record(s); dropped {dropped} for other candidates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
