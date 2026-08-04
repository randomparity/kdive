"""Per-PR M2 portability gate (ADR-0076).

Measures the cumulative touched lines (per-commit added+removed — not a net a later
revert can zero out) of every commit since the ``pre-M2`` tag over the
provider-agnostic core (domain/db/jobs/reconciler/services/store/security and the
whole ``mcp`` package including ``mcp/tools/*``), and fails when any file outside the
named allowlist is touched. A second, net ``git diff`` check covers the per-commit
walk's blind spot: a core change introduced only in a merge commit (a conflict
resolution or evil merge), which ``--no-merges`` numstat never sees. The allowlist is
the ADR-0076 set: the ``ResourceKind`` enum value, the one M2 migration, and the
additive ``presign_get`` primitive. Extending it is a deliberate, reviewed decision —
edit this file in the same PR. Allowlist paths are matched exactly, so the gate also
reports any entry naming no file and fails on it: such an entry allowlists nothing while
the modules that replaced it count as violations (#1835). That check catches the
move-or-delete shape only. An entry naming a file is not thereby live — content carved
out of a module that stays in place leaves the entry pointing at a shell, which nothing
here detects; see the note above ``ALLOWED_FILES`` for what review has to do instead.

Exit codes: 0 gate passes; 1 a stale allowlist entry or violations found; 2 the
baseline tag is unavailable.
Stdlib-only: CI runs it without a synced environment (``just m2-gate``).
"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 - git commands use fixed argv, no shell  # nosec B404
import sys
from pathlib import Path

BASELINE_TAG = "pre-M2"
GIT_COMMAND_TIMEOUT_S = 120
# The allowlist describes this repository's tree, so entries resolve against the checkout
# the script lives in — not the working directory, which the measurement walks separately.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Per-provider advertised capture-method coverage (M2.5 capstone, #304). The capture-method
# vocabulary is CaptureMethod: console/host_dump/gdbstub/kdump/fadump (fadump added by ADR-0349).
# Remote advertises console/host_dump/gdbstub/kdump (M2.5 exit; no fadump — that is a local
# pseries opt-in). Local advertises {kdump, fadump, host_dump}: ADR-0208 narrows its capture set to
# the core-producing methods it can actually fetch a vmcore for, dropping the non-core
# console/gdbstub half-truths; HOST_DUMP's seam landed in M2.8 B4 (ADR-0211, libvirt domain core
# dump); FADUMP shares the kdump overlay harvest (ADR-0349, host support gated at admission). Local
# stays the default and remote the opt-in provider (#198). This is a pinned constant because the
# gate is stdlib-only (CI runs it without a synced env); a drift-guard unit test
# (tests/scripts/test_m2_portability_gate.py) imports the real build_*_runtime builders and fails
# if this table ever diverges from what composition.py advertises.
_CAPTURE_VOCABULARY = ("console", "host_dump", "gdbstub", "kdump", "fadump")
CAPTURE_COVERAGE: dict[str, frozenset[str]] = {
    "remote-libvirt": frozenset({"console", "host_dump", "gdbstub", "kdump"}),
    "local-libvirt": frozenset({"kdump", "fadump", "host_dump"}),
}

CORE_PREFIXES = (
    "src/kdive/domain/",
    "src/kdive/db/",
    "src/kdive/jobs/",
    "src/kdive/reconciler/",
    "src/kdive/services/",
    "src/kdive/store/",
    "src/kdive/security/",
    "src/kdive/mcp/",
)

# Entries are matched exactly against numstat paths, so every member must name a file that
# exists (``stale_entries`` enforces it) and still holds the content its comment justifies.
# When a refactor moves an allowlisted module, follow the move chain to its end — a later
# carve-out out of a successor inherits the justification as long as it stays inside that
# successor's own package — and re-point the entry at whichever modules hold the content now,
# leaving the justification comment attached to them. Drop an entry once its file holds none
# of that content: a bare package marker allowlists nothing, which is the failure #1835 was
# filed about. Two things do not inherit: a module that arrived in the new package by another
# route was never part of the reviewed decision, and content that migrates to a different
# subsystem is a separate decision needing its own reviewed entry. When the content leaves
# ``CORE_PREFIXES`` altogether, drop the entry and say where it went.
ALLOWED_FILES = frozenset(
    {
        # ResourceKind.REMOTE_LIBVIRT (ADR-0076 named touch-point).
        "src/kdive/domain/catalog/resources.py",
        # The one M2 migration: the resources.kind CHECK widen.
        "src/kdive/db/schema/0020_resources_kind_remote_libvirt.sql",
        # The additive presign_get primitive (ADR-0076, ADR-0078).
        "src/kdive/store/objectstore.py",
        # drgn-live transport generalization (#215, ADR-0085): the deliberate, reviewed core
        # touch routing remote in-guest drgn off the ssh-credential + ssh-string assumption.
        # Was debug/sessions.py (split to sessions_lifecycle.py, then both grouped into the
        # sessions package, whose init later gave up its registration to sessions/registrar.py)
        # and debug/introspect.py (split into the introspection package, then reduced to a
        # facade and removed; the package's live handler later gave up its debuginfo probe to
        # introspection/gate.py). Both package inits are bare markers now and hold nothing.
        "src/kdive/mcp/tools/debug/sessions/lifecycle.py",
        "src/kdive/mcp/tools/debug/sessions/registrar.py",
        "src/kdive/mcp/tools/debug/introspection/common.py",
        "src/kdive/mcp/tools/debug/introspection/gate.py",
        "src/kdive/mcp/tools/debug/introspection/live.py",
        "src/kdive/mcp/tools/debug/introspection/offline.py",
        "src/kdive/mcp/tools/debug/introspection/registrar.py",
        # Dead-worker gdbstub reconciler reset (#216, ADR-0086): the deliberate, reviewed core
        # touch that resets a stale session's transport through the injected TransportResetter
        # port so a dead worker's single-client gdbstub stops blocking re-attach.
        "src/kdive/reconciler/loop.py",
        # Central config-registry migration (#233, ADR-0087): a one-time, reviewed platform
        # refactor routing the scattered KDIVE_* reads in these agnostic-core modules through
        # kdive.config. This is shared-infra (platform) work, not provider work; kdive/config/
        # itself is outside CORE_PREFIXES, so only these in-place reader migrations register here.
        # domain/lease.py moved into the domain/lifecycle package; debug/ops.py became the
        # debug/operations package init, which then gave up its whole body to operations/
        # runtime.py and operations/registrar.py and is a bare marker now.
        "src/kdive/db/pool.py",
        "src/kdive/domain/lifecycle/lease.py",
        "src/kdive/mcp/auth.py",
        "src/kdive/mcp/tools/catalog/artifacts/uploads.py",
        "src/kdive/mcp/tools/debug/operations/runtime.py",
        "src/kdive/mcp/tools/debug/operations/registrar.py",
        "src/kdive/security/secrets/secrets.py",
        # Operator-CLI audit attribution (#248, ADR-0089): the milestone's only non-cli core
        # change. A provider-agnostic addition — record the caller class (operator-cli | agent
        # | unknown) resolved from the OIDC client_id on every platform_audit_log row. The
        # required `actor` field threads through the shared audit chokepoints and every inline
        # success site; none of it is provider-specific.
        # ops/_auth.py became the shared mcp/platform_auth.py; breakglass.py moved to the
        # ops/security package; reconcile.py to the ops/reconcile package; resources.py became
        # the ops/resources package init, which then gave up its body to resources/host_ops.py
        # and is a bare marker now (the register/deregister/renew modules beside it are the
        # net-new runtime tools of a later decision, not content carved out of this one).
        "src/kdive/db/schema/0021_platform_audit_actor.sql",
        "src/kdive/security/authz/actor.py",
        "src/kdive/security/authz/context.py",
        "src/kdive/security/audit.py",
        "src/kdive/mcp/platform_auth.py",
        "src/kdive/mcp/tools/ops/_reads.py",
        "src/kdive/mcp/tools/ops/security/breakglass.py",
        "src/kdive/mcp/tools/ops/queue.py",
        "src/kdive/mcp/tools/ops/reconcile/reconcile.py",
        "src/kdive/mcp/tools/ops/resources/host_ops.py",
        "src/kdive/mcp/tools/ops/tuning.py",
        "src/kdive/mcp/tools/accounting/reports.py",
        "src/kdive/mcp/tools/catalog/shapes.py",
        # M2.2 admin-CLI net-new read tools (#252, ADR-0089 §6): two provider-agnostic
        # platform reads on the agnostic core. secrets.list reports secret *presence* (the
        # scope_refs projection on SecretRegistry — never values), platform-operator gated;
        # the fixtures module is a plain authenticated catalog read (its rootfs listing folded
        # into images.list, ADR-0465; fixtures.validate remains). Their app.py registrar
        # wiring and the value-free scope_refs accessor carry no provider-specific logic.
        # ops/secrets.py moved to the ops/security package.
        "src/kdive/mcp/tools/ops/security/secrets.py",
        "src/kdive/mcp/tools/catalog/fixtures.py",
        "src/kdive/security/secrets/secret_registry.py",
        # M2.3 doctor diagnostics tool (#269, ADR-0091): a provider-agnostic platform-operator
        # tool that runs an assembled set of read-only Checks and aggregates one verdict. It holds
        # no provider-specific logic — the per-provider checks live in kdive/diagnostics/ (outside
        # CORE_PREFIXES) and reach the tool only through the injected service factory; this module
        # is the same authz-gated/audited ops surface as its siblings above.
        "src/kdive/mcp/tools/ops/diagnostics.py",
        "src/kdive/mcp/assembly/app.py",
        # Server telemetry middleware (#266, ADR-0090 §5): a provider-agnostic platform
        # change adding TelemetryMiddleware (a span per MCP tool call + per-tool RED
        # metrics) at the dispatch boundary and registering it in build_app. The labels
        # are restricted to the tool name + outcome (no provider/tenant data); none of it
        # is provider-specific. mcp/middleware.py was later split into the middleware
        # package; these are the modules that split produced, less its bare-marker init.
        # Middlewares added to the package afterwards (bare_bearer_hint, compact,
        # doc_exposure, transport_trace) are their own decisions and are not covered here.
        "src/kdive/mcp/middleware/binding_errors.py",
        "src/kdive/mcp/middleware/denial_audit.py",
        "src/kdive/mcp/middleware/exposure.py",
        "src/kdive/mcp/middleware/shared.py",
        "src/kdive/mcp/middleware/telemetry.py",
        "src/kdive/mcp/middleware/usage.py",
        # M2.3 ephemeral-probe-guest egress check (#270, ADR-0091 §3): the heartbeat-honoring
        # reaper sweep for leaked `guest_egress` probe guests and its marker table. Both are
        # provider-agnostic — the reconciler reaps a probe by domain name through its existing
        # InfraReaper (no provider-specific branch), and the table is the reaper-visible marker
        # (active-run heartbeat + hard TTL). The probe-guest provision/exec seam itself lives in
        # kdive/diagnostics/ (outside CORE_PREFIXES) and is provider-wired by the live gate.
        # The module moved into the reconciler/cleanup package.
        "src/kdive/reconciler/cleanup/provider_reaping.py",
        "src/kdive/db/schema/0022_egress_probe_guests.sql",
        # Worker/reconciler telemetry + aux health gate (#267, ADR-0090 §5): a
        # provider-agnostic platform change. worker.py gains the loop-granularity /livez
        # heartbeat tick, the not-ready dequeue pause, and a per-job span; the two
        # *_telemetry modules build the per-job/per-pass spans + duration/queue-depth/lag
        # metrics over the facade providers, labelled only by job_kind/outcome (no
        # provider/tenant data). reconciler/loop.py (already allowlisted above) gains the
        # per-pass span + heartbeat tick. queue.py gains a read-only count_claimable used
        # by the queue-depth gauge. None of it is provider-specific.
        "src/kdive/jobs/worker.py",
        "src/kdive/jobs/worker_telemetry.py",
        "src/kdive/jobs/queue.py",
        "src/kdive/reconciler/loop_telemetry.py",
        # M2.4 image_catalog (#282, ADR-0092/0093): the DB-backed image catalog that replaces the
        # read-only YAML rootfs catalog as the single source of truth. A provider-agnostic
        # platform addition — the single M2.4 migration's full public+private schema, the
        # ImageCatalogEntry model + ImageVisibility/ImageState enums in models.py (already
        # allowlisted), and the IMAGE_CATALOG repository binding (a plain Repository over the new
        # table). No provider-specific logic; the provider materialize cutover lives outside
        # CORE_PREFIXES (providers/local_libvirt/...).
        "src/kdive/db/schema/0023_image_catalog.sql",
        "src/kdive/db/repositories.py",
        # M2.4 publish/register + IMAGE_BUILD job (#285, ADR-0092): the provider-agnostic
        # row-first publish/register two-write service, the IMAGE_BUILD job kind + handler
        # (build -> guest-contract-validate -> publish), the typed ImageBuildPayload, and the
        # jobs.kind CHECK widen that admits the new kind. No provider-specific logic — the
        # handler drives an injected RootfsBuildPlane and the publish service stores whatever
        # the PublishRequest carries; the concrete build plane lives under kdive/images/
        # (outside CORE_PREFIXES).
        "src/kdive/services/images/__init__.py",
        "src/kdive/services/images/publish.py",
        "src/kdive/jobs/handlers/image_build.py",
        "src/kdive/jobs/payloads.py",
        "src/kdive/db/schema/0024_image_build_job_kind.sql",
        # M2.4 private upload path (#286, ADR-0093): the provider-agnostic project-private
        # upload registration service. Under the project advisory lock it enforces the
        # per-project count/bytes quota fail-closed, validates the quarantined object's guest
        # contract, then delegates to the publish service with visibility='private'. No
        # provider-specific logic — it reuses the IMAGE_PRIVATE_* core settings and the existing
        # publish two-write; the new settings live in config/core_settings.py (already core).
        "src/kdive/services/images/upload.py",
        # M2.4 reconciler image sweeps (#287, ADR-0092/0093): three provider-agnostic,
        # deadline-guarded drift sweeps over the image_catalog + image-prefix objects (leaked
        # objects with no row, dangling rows whose object is gone, expired private images —
        # reference-guarded + extend-fenced). The sweeps consume the narrow ImageSweepStore port
        # (an ObjectStore satisfies it) and the catalog table; no provider-specific logic.
        # reconciler/loop.py (already allowlisted) appends the three _RepairSpecs + report counts.
        # The module moved into the reconciler/cleanup package.
        "src/kdive/reconciler/cleanup/images.py",
        # M2.4 kdivectl images verbs + RBAC/break-glass (#288, ADR-0092/0093): the operator
        # image-management surface. ops/images.py wires build/publish (platform_operator) +
        # upload/delete (project-scoped operator) + prune_expired/extend (platform_admin
        # break-glass) over the shared publish/upload/reconciler services (no second source of
        # truth); catalog/images.py is the RBAC-filtered images.list read; _docmeta.py adds the
        # three new destructive tools to the reviewed set. None of it is provider-specific —
        # authz is the same gate-then-act-then-audit ops surface as its siblings above, and the
        # kdivectl CLI verbs live outside CORE_PREFIXES (src/kdive/cli/...). ops/images.py was
        # later split into the ops/images package, whose init then gave up its registration to
        # images/registrar.py and is a bare marker now.
        "src/kdive/mcp/tools/ops/images/_common.py",
        "src/kdive/mcp/tools/ops/images/build_publish.py",
        "src/kdive/mcp/tools/ops/images/delete.py",
        "src/kdive/mcp/tools/ops/images/registrar.py",
        "src/kdive/mcp/tools/ops/images/retention.py",
        "src/kdive/mcp/tools/ops/images/upload.py",
        "src/kdive/mcp/tools/catalog/images.py",
        "src/kdive/mcp/tools/_docmeta.py",
        # M2.5 reconciler-owned remote console collector (#303, ADR-0095): the two net-new core
        # modules the single-leader console hosting needs. console_hosting.py was the injectable
        # leader-locked hosting loop + attach-watcher + shared CollectorRegistry the liveness/reap
        # class drives; locks.py gains the session-scoped pg_advisory_lock leadership helper (the
        # transaction-scoped advisory_xact_lock cannot hold leadership across between-pass
        # streamers). Both are provider-agnostic platform infra — the per-System streamer and its
        # libvirt/object-store wiring live under providers/remote_libvirt/ (outside CORE_PREFIXES).
        # reconciler/loop.py (already allowlisted) gains the console liveness/reap _RepairSpec.
        # The hosting loop and its db adapters have since left the core surface entirely, to
        # providers/infra/console_hosting.py; only locks.py still needs an entry.
        "src/kdive/db/locks.py",
        # Kdump config-fragment provisioning (ADR-0096): a provider-agnostic build input. The
        # seeded build_config_catalog migration, the read-only buildconfig.get catalog tool, and
        # the runs.build default-config substitution serve BOTH the local- and remote-libvirt
        # build providers identically (the per-provider build-flow change lives under
        # providers/*/build.py + the shared providers/build_common.py, outside CORE_PREFIXES).
        # None of these three is provider-specific; the catalog is keyed by name alone.
        "src/kdive/db/schema/0025_build_config_catalog.sql",
        "src/kdive/db/schema/0034_build_config_catalog_source.sql",
        "src/kdive/db/schema/0035_build_config_catalog_source_config.sql",
    }
)


def parse_numstat(out: str) -> dict[str, int]:
    """Aggregate per-file touched lines (added+removed) from ``git log --numstat`` output.

    Binary files render as ``-\\t-\\tpath`` and count as one touched line. Only files
    under the core prefixes are the gate's subject.
    """
    touched: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if not path.startswith(CORE_PREFIXES):
            continue
        lines = 1 if added == "-" else int(added) + int(removed)
        touched[path] = touched.get(path, 0) + max(lines, 1)
    return touched


def violations(touched: dict[str, int]) -> dict[str, int]:
    """The non-allowlisted core files with any cumulative touch."""
    return {path: count for path, count in touched.items() if path not in ALLOWED_FILES}


def stale_entries(root: Path) -> list[str]:
    """The ``ALLOWED_FILES`` members naming no file under ``root``, sorted.

    An entry is matched against numstat paths exactly — there is no prefix or directory
    matching — so an entry whose module moved or became a package stops allowlisting
    anything, while the modules that replaced it register as violations (#1835). A
    directory is as stale as an absent path: numstat names files.

    This is a floor, not a liveness check, in three ways. An entry naming no file is
    definitely dead; an entry naming a file may still be dead.

    Content carved out of a module that stays in place leaves the entry pointing at a
    shell that allowlists nothing. That shape has no cheap mechanical test — ``_measure``
    runs with ``--no-renames``, so a retired path keeps its historical touch counts
    forever and a zero-touch check would not fire either — so only review catches it.

    An entry naming an untracked or ignored file also allowlists nothing, because every
    path it is matched against comes from ``git`` numstat, which never emits one. Only
    running this guard against a clean checkout catches that, which is what the unit test
    in CI does; a local run passes on a successor module you forgot to stage.

    ``root`` is the checkout this script lives in, while ``_measure`` shells ``git`` in the
    process working directory. They are the same tree for ``just m2-gate``, which runs from
    the justfile's directory, and the split is deliberate: the allowlist describes this
    repository, whereas the measurement is of whatever history it is pointed at, which is
    how the gate's own tests drive it over throwaway repositories. Invoking the script by
    absolute path from another checkout measures one tree and checks staleness in another.
    """
    return sorted(path for path in ALLOWED_FILES if not (root / path).is_file())


def render_capture_coverage() -> list[str]:
    """Render the per-provider capture-method coverage table (M2.5 capstone, #304).

    Renders each provider's coverage from the pinned ``CAPTURE_COVERAGE`` table (kept true by
    a drift-guard test). The methods column names each advertised method.
    """
    total = len(_CAPTURE_VOCABULARY)
    lines = [
        "## Capture-method coverage",
        "",
        f"Advertised capture methods per provider, of the {total}-method vocabulary "
        f"(`{'`, `'.join(_CAPTURE_VOCABULARY)}`). Remote reaches **4/5** (M2.5 exit, ADR-0084; no "
        "fadump — a local pseries opt-in). Local advertises **3/5** (`host_dump`, `kdump`, "
        "`fadump`): ADR-0208 narrowed its set to the core-producing methods it can actually fetch "
        "a vmcore for — the host-side overlay harvest (#115/ADR-0203, shared by fadump per "
        "ADR-0349) and the libvirt domain core dump (M2.8 B4/ADR-0211). Local stays the default "
        "and remote the opt-in provider (#198).",
        "",
        "| provider | coverage | advertised methods |",
        "|---|---:|---|",
    ]
    for provider in sorted(CAPTURE_COVERAGE):
        methods = CAPTURE_COVERAGE[provider]
        ordered = [m for m in _CAPTURE_VOCABULARY if m in methods]
        lines.append(f"| `{provider}` | {len(methods)} / {total} | `{'`, `'.join(ordered)}` |")
    lines.append("")
    return lines


def render_report(touched: dict[str, int], stale: list[str]) -> str:
    """Render the measurement as a markdown report (pure function over its inputs).

    Used by ``--report`` to write the committed milestone-end record (``just m2-report``). The
    verdict mirrors the gate: a non-allowlisted core touch is a violation and fails, and so
    does a stale allowlist entry.

    A stale entry renders as its own section rather than short-circuiting the render: the
    recipe redirects stdout into the committed record, so the shell truncates that file
    before this runs and printing nothing would leave it empty. A report that says which
    entries are stale, and that every classification below them is therefore unreliable, is
    a record; a zero-length file is not.
    """
    allowed = {path: count for path, count in touched.items() if path in ALLOWED_FILES}
    bad = violations(touched)
    lines = [
        "# M2 portability report",
        "",
        f"Cumulative touched lines of the M2 commit set since the `{BASELINE_TAG}` tag, over the",
        "provider-agnostic core surface (ADR-0076). Generated by `just m2-report` — do not",
        "hand-edit.",
        "",
        *render_capture_coverage(),
    ]
    if stale:
        lines += [
            "## Stale allowlist entries",
            "",
            "These entries name no file in the tree, so they allowlist nothing while the modules",
            "that replaced them fall under Violations below. Every classification in this report",
            "is unreliable until they are re-pointed (#1835).",
            "",
            *(f"- `{path}`" for path in stale),
            "",
        ]
    lines += [
        "## Allowlisted touch-points",
        "",
        "| cumulative lines | file |",
        "|---:|---|",
    ]
    lines.extend(f"| {count} | `{path}` |" for path, count in sorted(allowed.items()))
    lines.append("")
    if bad:
        lines += [
            "## Violations",
            "",
            "| cumulative lines | file |",
            "|---:|---|",
            *(f"| {count} | `{path}` |" for path, count in sorted(bad.items())),
            "",
            "**Verdict: gate FAILED** — provider-specific changes reached the core surface.",
        ]
    elif stale:
        lines.append("**Verdict: gate FAILED** — the allowlist has entries naming no file.")
    else:
        lines.append(
            "**Verdict: gate passed** — no core surface touched outside the ADR-0076 allowlist."
        )
    # No trailing blank element: ``print`` adds the single final newline the
    # end-of-file hook enforces, so the committed report regenerates byte-identically.
    return "\n".join(lines)


def _find_git() -> str | None:
    git = shutil.which("git")
    if git is None:
        print(
            "error: git executable is unavailable; install git or set PATH before running "
            "the M2 portability gate",
            file=sys.stderr,
        )
        return None
    return git


def _measure() -> dict[str, int] | None:
    """Compute the cumulative touched map, or None if the baseline tag is unavailable."""
    git = _find_git()
    if git is None:
        return None
    tag_check = subprocess.run(
        [git, "rev-parse", "--verify", f"{BASELINE_TAG}^{{commit}}"],
        capture_output=True,
        text=True,
        timeout=GIT_COMMAND_TIMEOUT_S,
    )  # git is resolved via shutil.which; args are fixed.  # nosec B603
    if tag_check.returncode != 0:
        print(
            f"error: baseline tag {BASELINE_TAG!r} is unavailable; fetch tags/history "
            "(CI: actions/checkout with fetch-depth: 0)",
            file=sys.stderr,
        )
        return None
    log = subprocess.run(
        [
            git,
            "log",
            "--numstat",
            "--no-merges",
            "--no-renames",
            "--format=",
            f"{BASELINE_TAG}..HEAD",
            "--",
            *CORE_PREFIXES,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_COMMAND_TIMEOUT_S,
    )  # git is resolved via shutil.which; args are fixed.  # nosec B603
    touched = parse_numstat(log.stdout)
    # Union in the net diff: it sees merge-commit-only changes the per-commit walk
    # misses, while the per-commit sum keeps reverted changes counted.
    net = subprocess.run(
        [
            git,
            "diff",
            "--numstat",
            "--no-renames",
            f"{BASELINE_TAG}..HEAD",
            "--",
            *CORE_PREFIXES,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_COMMAND_TIMEOUT_S,
    )  # git is resolved via shutil.which; args are fixed.  # nosec B603
    for path, count in parse_numstat(net.stdout).items():
        touched[path] = max(touched.get(path, 0), count)
    return touched


def main() -> int:
    touched = _measure()
    if touched is None:
        return 2
    stale = stale_entries(REPO_ROOT)
    if "--report" in sys.argv[1:]:
        print(render_report(touched, stale))
        return 1 if violations(touched) or stale else 0
    allowed = {path: count for path, count in touched.items() if path in ALLOWED_FILES}
    print(f"M2 portability measurement since {BASELINE_TAG} (cumulative touched lines):")
    for path, count in sorted(allowed.items()):
        print(f"  allowlisted  {count:>6}  {path}")
    if stale:
        print(
            "\ngate FAILED - these ALLOWED_FILES entries name no file in the tree, so they "
            "allowlist nothing while the modules that replaced them count as violations:"
        )
        for path in stale:
            print(f"  STALE ENTRY          {path}")
        print(
            "\nRe-point each entry at the module that now holds its allowlisted content, "
            "keeping its justification comment attached, or drop the entry when that "
            "content left the core surface."
        )
    bad = violations(touched)
    if bad:
        print("\ngate FAILED - provider-specific changes reached the core surface:")
        for path, count in sorted(bad.items()):
            print(f"  VIOLATION    {count:>6}  {path}")
        print(
            "\nRefactor the provider logic out of core (the M2 co-equal goal, "
            "docs/design/m2-remote-libvirt.md), or - for a deliberate provider-agnostic "
            "core change - extend ALLOWED_FILES in this script in the same PR."
        )
    if bad or stale:
        return 1
    print("gate passed: no core surface touched outside the ADR-0076 allowlist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
