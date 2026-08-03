"""Core (provider-agnostic) ``KDIVE_*`` settings (ADR-0087).

Platform settings the server/worker/reconciler/migrate processes consume directly:
database, HTTP bind, logging, OIDC, object store, lease bounds, upload limits, the
worker storage paths (build/install/crash/debug/secrets), the fixture catalog, and the
fault-injection enable gate. Provider-specific knobs are co-located with their provider
(``providers/*/…``) and aggregated through the manifest, not declared here.

Readers that apply their own domain parsing (lease windows, paths) declare ``parse=str``
and keep that parsing at the call site; this preserves their existing validation and
error details while still routing the read through the registry.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from kdive.config.registry import RUNNABLE, Setting

_SERVER = frozenset({"server"})
_STORE_USERS = frozenset({"server", "worker", "reconciler"})
_WORKER = frozenset({"worker"})
_RECONCILER = frozenset({"reconciler"})
_DISCOVERY = frozenset({"worker", "reconciler"})
# The upload-orphan sweep's two knobs are read by both processes (ADR-0455 §2, §8): the reconciler
# runs the sweep on its loop, and the server runs a full `reconcile_once` on demand via
# `ops.reconcile_now` — so a brake set on only one of them still lets the other delete. The TTL is
# additionally *set* by the server at mint time and merely read by the reconciler. ``processes``
# does not gate resolution — ``Registry.get`` reads the environment regardless — so declaring both
# here buys two things: ``config validate`` checks a malformed value at startup instead of raising
# from inside a repair on every pass, and the generated operator reference tells whoever provisions
# each process's environment that it needs these variables.
_UPLOAD_RECLAIM_READERS = frozenset({"server", "reconciler"})
# Processes that read the on-disk provider fixture catalog: the worker/reconciler build paths
# plus the server's fixtures.validate read (ADR-0120).
_CATALOG_READERS = frozenset({"server", "worker", "reconciler"})


def _int(raw: str) -> int:
    return int(raw)


def _str(raw: str) -> str:
    return raw


def _nonempty(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


def _ratio(raw: str) -> float:
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"must be in [0, 1], got {value}")
    return value


def _positive_float(raw: str) -> float:
    value = float(raw)
    if value <= 0.0:
        raise ValueError(f"must be > 0, got {value}")
    return value


def _nonnegative_int(raw: str) -> int:
    """Parse a grace window, rejecting a negative that would invert it into an immediate delete."""
    value = int(raw)
    if value < 0:
        raise ValueError(f"must be >= 0, got {value}")
    return value


def _nonnegative_int_at_most(maximum: int) -> Callable[[str], int]:
    """Parse a nonnegative row count with an unbypassable pass ceiling."""

    def parse(raw: str) -> int:
        value = _nonnegative_int(raw)
        if value > maximum:
            raise ValueError(f"must be <= {maximum}, got {value}")
        return value

    return parse


def _positive_int(raw: str) -> int:
    """Parse a count that is meaningless at zero or below."""
    value = int(raw)
    if value < 1:
        raise ValueError(f"must be >= 1, got {value}")
    return value


def _choice(*allowed: str) -> Callable[[str], str]:
    def parse(raw: str) -> str:
        if raw not in allowed:
            raise ValueError(f"must be one of {', '.join(allowed)}")
        return raw

    return parse


def _always(env: Mapping[str, str]) -> bool:
    return True


DATABASE_URL = Setting(
    name="KDIVE_DATABASE_URL",
    parse=_nonempty,
    group="database",
    processes=RUNNABLE,
    required_when=_always,
    help="Postgres DSN for the system-of-record.",
    suggest="a Postgres DSN, e.g. postgresql://host:5432/kdive",
)

MIGRATION_DATABASE_URL = Setting(
    name="KDIVE_MIGRATION_DATABASE_URL",
    parse=_nonempty,
    secret=True,
    group="database",
    help="Compose supervisor input containing the migration-owner Postgres DSN.",
    suggest="a migration-owner Postgres DSN",
)
WORKER_DATABASE_URL = Setting(
    name="KDIVE_WORKER_DATABASE_URL",
    parse=_nonempty,
    secret=True,
    group="database",
    help="Compose supervisor input containing the worker-role Postgres DSN.",
    suggest="a worker-role Postgres DSN",
)
LIFECYCLE_WITNESS_DATABASE_URL = Setting(
    name="KDIVE_LIFECYCLE_WITNESS_DATABASE_URL",
    parse=_nonempty,
    secret=True,
    group="database",
    help="Postgres DSN used only by the Compose lifecycle witness authority.",
    suggest="a lifecycle-witness Postgres DSN",
)

HTTP_HOST = Setting(
    name="KDIVE_HTTP_HOST",
    parse=_str,
    default="127.0.0.1",
    group="http",
    processes=_SERVER,
    help="Bind host for the MCP server.",
)
HTTP_PORT = Setting(
    name="KDIVE_HTTP_PORT",
    parse=_int,
    default="8000",
    group="http",
    processes=_SERVER,
    help="Bind port for the MCP server.",
    suggest="an integer port, e.g. 8000",
)

LOG_LEVEL = Setting(
    name="KDIVE_LOG_LEVEL",
    parse=_str,
    default="INFO",
    group="logging",
    processes=RUNNABLE,
    help="Structured-logging level (overridable by --log-level).",
)

OIDC_JWKS_URI = Setting(
    name="KDIVE_OIDC_JWKS_URI",
    parse=_nonempty,
    group="oidc",
    processes=_SERVER,
    required_when=_always,
    help="JWKS URI the bearer-token verifier fetches signing keys from.",
    suggest="the issuer's JWKS endpoint, e.g. http://oidc:8080/default/jwks",
)
OIDC_ISSUER = Setting(
    name="KDIVE_OIDC_ISSUER",
    parse=_nonempty,
    group="oidc",
    processes=_SERVER,
    required_when=_always,
    help="Expected token issuer (iss), enforced natively.",
    suggest="the OIDC issuer URL, e.g. http://oidc:8080/default",
)
OIDC_AUDIENCE = Setting(
    name="KDIVE_OIDC_AUDIENCE",
    parse=_nonempty,
    group="oidc",
    processes=_SERVER,
    required_when=_always,
    help="Expected token audience (aud), enforced natively.",
    suggest="the audience this server accepts, e.g. kdive",
)

S3_ENDPOINT_URL = Setting(
    name="KDIVE_S3_ENDPOINT_URL",
    parse=_nonempty,
    group="objectstore",
    processes=_STORE_USERS,
    required_when=_always,
    help="S3-compatible endpoint URL for bulk artifacts (required, ADR-0337).",
    suggest="an S3-compatible endpoint URL, e.g. http://minio:9000",
)
S3_BUCKET = Setting(
    name="KDIVE_S3_BUCKET",
    parse=_nonempty,
    group="objectstore",
    processes=_STORE_USERS,
    required_when=_always,
    help="Bucket holding vmcores, transcripts, and uploads (required, ADR-0337).",
    suggest="a bucket name, e.g. kdive",
)
S3_REGION = Setting(
    name="KDIVE_S3_REGION",
    parse=_str,
    default="us-east-1",
    group="objectstore",
    processes=_STORE_USERS,
    help="Region for the object-store client.",
)

LEASE_DEFAULT = Setting(
    name="KDIVE_LEASE_DEFAULT",
    parse=_str,
    group="lease",
    processes=_SERVER,
    help="Default lease window (hours) when a request omits one (built-in 4).",
)
LEASE_MAX = Setting(
    name="KDIVE_LEASE_MAX",
    parse=_str,
    group="lease",
    processes=_SERVER,
    help="Hard cap (hours) on a lease window / renewal (built-in 24).",
)

PROVISION_PREMUTATION_TIMEOUT_S = Setting(
    name="KDIVE_PROVISION_PREMUTATION_TIMEOUT_S",
    parse=_positive_float,
    default="30.0",
    group="lifecycle",
    processes=_SERVER,
    help=(
        "Seconds to bound the synchronous pre-mutation segment of the systems create lane "
        "(systems.provision: validation, lock acquisition, rootfs check). "
        "On exceed, the tool returns a transport_failure envelope instead of dropping the "
        "socket (ADR-0126)."
    ),
    suggest="a positive number of seconds, e.g. 30",
)

UPLOAD_TTL_SECONDS = Setting(
    name="KDIVE_UPLOAD_TTL_SECONDS",
    # Bounded for the same reason as its twin below, and it needs saying separately because the
    # threshold is their *sum*: a negative TTL cancels the grace and pushes the mtime cutoff into
    # the future just as effectively. A negative presigned-URL TTL is nonsense on the server too,
    # so nothing is given up by rejecting it in both processes.
    parse=_nonnegative_int,
    default="86400",
    group="upload",
    # Read by the reconciler as well as *set* by the minting server: the orphan sweep's reclaim
    # threshold is stacked on top of it (ADR-0455 §2), so a reconciler provisioned without this
    # variable falls back to the default and reclaims a window's bytes in the pass that reaped them.
    processes=_UPLOAD_RECLAIM_READERS,
    help="Presigned upload-URL TTL in seconds. Also read by the reconciler (ADR-0455).",
    suggest="a non-negative integer number of seconds, e.g. 86400",
)
UPLOAD_ORPHAN_GRACE = Setting(
    name="KDIVE_UPLOAD_ORPHAN_GRACE_SECONDS",
    # Not ``_int``: this is the only brake on a repair that deletes irreversibly, and a negative
    # value moves the cutoff into the future, making every rowless object under both roots
    # reclaimable on the next pass — including one whose PUT landed seconds ago. The per-key
    # re-read cannot catch it, because it re-evaluates the same inverted predicate. Rejecting it
    # in the parser puts the failure at `config validate` instead of at the first delete.
    parse=_nonnegative_int,
    default="86400",
    group="upload",
    processes=_UPLOAD_RECLAIM_READERS,
    help=(
        "Grace window in seconds protecting an unreferenced object under an upload prefix from "
        "the reconciler's orphan sweep (ADR-0455). Measured from the object's store mtime and "
        "applied on top of KDIVE_UPLOAD_TTL_SECONDS, so an object is reclaimed only well after "
        "the window it could have belonged to was reaped. Raise it to stall the sweep — on the "
        "server as well as the reconciler, since ops.reconcile_now runs the sweep too. Takes "
        "effect when the process restarts; config is snapshotted at startup."
    ),
    suggest="a non-negative integer number of seconds, e.g. 86400",
)
UPLOAD_WINDOW_MAX_TTL_MULTIPLE = Setting(
    name="KDIVE_UPLOAD_WINDOW_MAX_TTL_MULTIPLE",
    # A multiple of the TTL rather than an absolute number of seconds, so the cap can never be
    # configured *below* the window it bounds. An absolute cap smaller than KDIVE_UPLOAD_TTL_SECONDS
    # would clamp every refresh to the deadline the mint already stamped, silently disabling the
    # reassembly protection the refresh exists to provide — the failure mode a bound must not have.
    # Rejecting 0 and negatives for the same reason: they would put the cap at or before the mint.
    parse=_positive_int,
    default="3",
    group="upload",
    processes=_SERVER,
    help=(
        "Cap on how long one minted upload window may live, as a multiple of "
        "KDIVE_UPLOAD_TTL_SECONDS measured from the mint (ADR-0511). The chunked "
        "runs.complete_build extends its window by a full TTL before server-side reassembly, and "
        "that extension commits even when the finalize then fails, so repeated failing retries "
        "would otherwise roll the window forward without bound. An extension is clamped to this "
        "multiple and never shortens an open window; 1 forbids extension entirely. Re-minting via "
        "artifacts.create_run_upload starts a new window and a fresh budget, so this bounds "
        "silent drift, not the agent's reach."
    ),
    suggest="an integer >= 1, e.g. 3",
)
MAX_UPLOAD_BYTES = Setting(
    name="KDIVE_MAX_UPLOAD_BYTES",
    parse=_int,
    default=str(50 * 1024 * 1024 * 1024),
    group="upload",
    processes=_SERVER,
    help=(
        "Maximum accepted per-artifact upload size in bytes. A single-PUT artifact still binds "
        "at the 5 GiB S3 single-PUT ceiling; this cap governs a chunked artifact's total "
        "(ADR-0104)."
    ),
    suggest="an integer number of bytes, e.g. 53687091200 (50 GiB)",
)

ARTIFACT_INLINE_MAX_BYTES = Setting(
    name="KDIVE_ARTIFACT_INLINE_MAX_BYTES",
    parse=_int,
    default=str(64 * 1024),
    group="artifacts",
    processes=_SERVER,
    help=(
        "Upper bound in bytes on the `artifacts.get` inline window in `data.content`. The "
        "returned window is the smaller of this, the caller's `max_bytes`, and a fixed "
        "24 KiB token-safe ceiling (ADR-0257) — so raising this above 24 KiB has no effect; "
        "lowering it narrows the window further. Objects above the 1 MiB fetch ceiling omit "
        "inline content and are retrieved via the presigned `refs.download_uri` (ADR-0140, "
        "ADR-0247)."
    ),
    suggest="an integer number of bytes, e.g. 65536 (64 KiB)",
)
ARTIFACT_DOWNLOAD_TTL_SECONDS = Setting(
    name="KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS",
    parse=_int,
    default="900",
    group="artifacts",
    processes=_SERVER,
    help=(
        "Expiry in seconds of the presigned download URL `artifacts.get` mints in "
        "`refs.download_uri` for a redacted artifact (ADR-0140)."
    ),
    suggest="an integer number of seconds, e.g. 900",
)
REPORT_INLINE_MAX_BYTES = Setting(
    name="KDIVE_REPORT_INLINE_MAX_BYTES",
    parse=_int,
    default=str(64 * 1024),
    group="reports",
    processes=_SERVER,
    help=(
        "Total byte budget for the inline report payload `reports.generate_*` returns in "
        "`items[].data.rows_json`. A section whose serialized rows exceed the remaining "
        "budget degrades to a bounded preview plus `inline_truncated`; the full set is in "
        "the spreadsheet artifact (ADR-0212)."
    ),
    suggest="an integer number of bytes, e.g. 65536 (64 KiB)",
)
REPORT_ARTIFACT_RETENTION_DAYS = Setting(
    name="KDIVE_REPORT_ARTIFACT_RETENTION_DAYS",
    parse=_int,
    default="7",
    group="reports",
    processes=_STORE_USERS,
    help=(
        "Age in days after which the reconciler `gc_report_artifacts` sweep deletes a "
        "generated report's spreadsheet artifact (object + row). Reports are ephemeral and "
        "re-runnable (ADR-0212)."
    ),
    suggest="an integer number of days, e.g. 7",
)
INVESTIGATION_CLEANUP_GRACE_DAYS = Setting(
    name="KDIVE_INVESTIGATION_CLEANUP_GRACE_DAYS",
    parse=_int,
    default="1",
    group="reports",
    processes=_STORE_USERS,
    help=(
        "Grace window in days between an investigation closing and the reconciler "
        "`gc_investigation_artifacts` sweep reclaiming its run-owned uploaded build artifacts "
        "(kernel/vmlinux/initrd; never console or crash evidence). ADR-0234 §4."
    ),
    suggest="an integer number of days, e.g. 1",
)
BUILD_ARTIFACT_RETENTION_DAYS = Setting(
    name="KDIVE_BUILD_ARTIFACT_RETENTION_DAYS",
    parse=_int,
    default="30",
    group="reports",
    processes=_STORE_USERS,
    help=(
        "Age in days after which the reconciler `gc_expired_build_artifacts` sweep deletes a "
        "run-owned uploaded build artifact regardless of investigation close — the backstop for "
        "investigations that never close. ADR-0234 §4."
    ),
    suggest="an integer number of days, e.g. 30",
)

INVESTIGATION_ROOTFS_RETENTION_DAYS = Setting(
    name="KDIVE_INVESTIGATION_ROOTFS_RETENTION_DAYS",
    parse=_int,
    default="30",
    group="reports",
    processes=_STORE_USERS,
    help=(
        "Age in days after which the reconciler's TTL rootfs sweep enqueues a "
        "`reclaim_investigation_rootfs` worker job, which reclaims an investigation-scoped "
        "uploaded rootfs base (staged file + object + row) on an investigation that never "
        "closed — the TTL backstop to the close+grace reclaim, both gated on per-System "
        "overlay-file absence. The reconciler only enqueues; the worker that created the "
        "staging tree performs the reclaim. ADR-0441 §6, ADR-0442."
    ),
    suggest="an integer number of days, e.g. 30",
)

MAX_INVENTORY_EXPORT_BYTES = Setting(
    name="KDIVE_MAX_INVENTORY_EXPORT_BYTES",
    parse=_int,
    default=str(256 * 1024),
    group="upload",
    processes=_SERVER,
    help=(
        "Maximum accepted systems.toml document size in bytes for "
        "ops.export_systems_toml (persist/writeback). A whole live inventory is a few "
        "KiB; the cap bounds a hostile or accidental large document."
    ),
    suggest="an integer number of bytes, e.g. 262144 (256 KiB)",
)

DEBUG_DIR = Setting(
    name="KDIVE_DEBUG_DIR",
    parse=_str,
    default="/var/lib/kdive/debug",
    group="debug",
    processes=_WORKER,
    help="Directory for debug-session transcripts.",
)
CRASH_DIR = Setting(
    name="KDIVE_CRASH_DIR",
    parse=_str,
    group="debug",
    processes=_WORKER,
    help="Directory for local kdump crash captures (live_vm path).",
)
LIVE_SCRIPT_MAX_TIMEOUT_SECONDS = Setting(
    name="KDIVE_LIVE_SCRIPT_MAX_TIMEOUT_SECONDS",
    parse=_int,
    default="600",
    group="debug",
    processes=_SERVER,
    help=(
        "Upper bound (seconds) the server clamps an agent-chosen `introspect.script` "
        "`timeout_sec` to before it drives the in-guest `timeout drgn -k` wrapper. A "
        "deployment policy bounding how long one live drgn script can hold a server "
        "thread-pool slot; single-tenant operators may set it high (ADR-0240)."
    ),
    suggest="an integer number of seconds, e.g. 600",
)

SECRETS_ROOT = Setting(
    name="KDIVE_SECRETS_ROOT",  # pragma: allowlist secret - env var name, not a value
    parse=_str,
    default="/var/lib/kdive/secrets",
    group="secrets",
    processes=_STORE_USERS,
    help="Root directory for the file-ref secret backend.",
)

BUILD_WORKSPACE = Setting(
    name="KDIVE_BUILD_WORKSPACE",
    parse=_str,
    default="/var/lib/kdive/build",
    group="build",
    processes=_WORKER,
    help="Worker scratch root for kernel builds.",
)
KERNEL_SRC = Setting(
    name="KDIVE_KERNEL_SRC",
    parse=_str,
    default="",
    group="build",
    processes=_WORKER,
    help="Kernel source tree the worker builds from.",
)
BUILD_COMPONENT_ROOTS = Setting(
    name="KDIVE_BUILD_COMPONENT_ROOTS",
    parse=_str,
    group="build",
    processes=_WORKER,
    help="Colon-separated extra component roots merged into a build.",
)
LOCAL_BUILD_REMOTE_ALLOWLIST = Setting(
    name="KDIVE_LOCAL_BUILD_REMOTE_ALLOWLIST",
    parse=_str,
    group="build",
    processes=_WORKER,
    help=(
        "Comma-separated allowlist of git remotes the local (worker-local) build host may "
        "clone for a git kernel_source_ref. Each entry is a host (github.com) or host/path "
        "prefix (github.com/myorg). Empty/unset disables local git builds (deny by default)."
    ),
)
BUILD_USER = Setting(
    name="KDIVE_BUILD_USER",
    parse=_str,
    group="build",
    processes=_WORKER,
    help=(
        "Name of an unprivileged passwd account the worker drops to for local kernel "
        "builds (git clone + make) when it runs as root (ADR-0214). Empty/unset: a root "
        "worker refuses the local build lane (deny by default); a non-root worker ignores it."
    ),
)
INSTALL_STAGING = Setting(
    name="KDIVE_INSTALL_STAGING",
    parse=_str,
    default="/var/lib/kdive/install",
    group="install",
    processes=_WORKER,
    help=(
        "Worker staging root for install artifacts. Must be writable by the run user; the "
        "default's parent (/var/lib/kdive) is root-owned, so on a source checkout pre-create "
        "it (or repoint this var) — on SELinux hosts with the virt_image_t label. An "
        "unwritable root fails install with a configuration_error (ADR-0204)."
    ),
)

INSTALL_SCRATCH = Setting(
    name="KDIVE_INSTALL_SCRATCH",
    parse=_str,
    group="install",
    processes=_WORKER,
    help=(
        "Worker scratch root for transient install intermediates (the fetched combined kernel "
        "tar, the repacked modules tar, and a debuginfo run's fetched vmlinux). Unset defaults "
        "to KDIVE_INSTALL_STAGING, so the intermediates share the staging dir (unchanged "
        "behavior). Point this at a separate mount (e.g. tmpfs) to keep the large, short-lived "
        "intermediates off the staging disk while the persistent kernel/initrd stay in staging. "
        "tmpfs trades disk for RAM: the object store already materializes the whole tar in "
        "memory, so a tmpfs scratch holds those bytes plus the repacked modules tar resident for "
        "the extract+inject window (up to a few GB on a DWARF-heavy tar), multiplied by "
        "concurrent installs. Size tmpfs against host RAM and worker concurrency, or leave it "
        "unset; the RAM-free path is streaming fetch-and-extract (tracked as a follow-up to "
        "ADR-0399)."
    ),
)

FIXTURE_CATALOG_PATH = Setting(
    name="KDIVE_FIXTURE_CATALOG_PATH",
    parse=_str,
    group="catalog",
    processes=_CATALOG_READERS,
    help="Override path to the provider fixture catalog (operator override, ADR-0120).",
)

IMAGE_PUBLISH_GRACE = Setting(
    name="KDIVE_IMAGE_PUBLISH_GRACE_SECONDS",
    parse=_int,
    default="3600",
    group="images",
    processes=_DISCOVERY,
    help=(
        "Image publish-deadline grace window in seconds. A pending image row (or an orphan "
        "object with no row) is protected from the reconciler's leaked/dangling sweeps until "
        "pending_since + this window elapses, so an in-flight publish is not reaped."
    ),
    suggest="an integer number of seconds, e.g. 3600",
)

IMAGE_PRIVATE_LIFETIME_DEFAULT = Setting(
    name="KDIVE_IMAGE_PRIVATE_LIFETIME_DEFAULT_SECONDS",
    parse=_int,
    default=str(7 * 24 * 3600),
    group="images",
    processes=_SERVER,
    help=(
        "Default lifetime in seconds applied to a project-private uploaded image when the "
        "caller does not request an explicit expiry; the registered row's expires_at is set to "
        "now() + this window."
    ),
    suggest="an integer number of seconds, e.g. 604800 (7 days)",
)
IMAGE_PRIVATE_LIFETIME_MAX = Setting(
    name="KDIVE_IMAGE_PRIVATE_LIFETIME_MAX_SECONDS",
    parse=_int,
    default=str(30 * 24 * 3600),
    group="images",
    processes=_SERVER,
    help=(
        "Hard ceiling in seconds on a project-private image lifetime. A requested expiry beyond "
        "now() + this window is clamped to the ceiling so a private upload cannot outlive the "
        "milestone TTL policy."
    ),
    suggest="an integer number of seconds, e.g. 2592000 (30 days)",
)
IMAGE_PRIVATE_MAX_COUNT = Setting(
    name="KDIVE_IMAGE_PRIVATE_MAX_COUNT",
    parse=_int,
    default="50",
    group="images",
    processes=_SERVER,
    help=(
        "Per-project cap on the number of live (pending or registered) private images. An upload "
        "that would exceed the cap is denied fail-closed under the held project lock and audited."
    ),
    suggest="an integer count, e.g. 50",
)
IMAGE_PRIVATE_MAX_BYTES = Setting(
    name="KDIVE_IMAGE_PRIVATE_MAX_BYTES",
    parse=_int,
    default=str(50 * 1024 * 1024 * 1024),
    group="images",
    processes=_SERVER,
    help=(
        "Per-project cap in bytes on the total size of live (pending or registered) private "
        "images. An upload whose size would push the project total past the cap is denied "
        "fail-closed under the held project lock and audited."
    ),
    suggest="an integer number of bytes, e.g. 53687091200 (50 GiB)",
)

SYSTEMS_TOML = Setting(
    name="KDIVE_SYSTEMS_TOML",
    parse=_str,
    default=None,
    group="inventory",
    processes=frozenset({"reconciler", "worker"}),
    help=(
        "Path to the declarative systems inventory file reconciled into the catalog "
        "(ADR-0112). The reconciler's inventory pass reads it each loop; the worker resolves "
        "the per-op remote-libvirt connection config from it (ADR-0112 §connection). When unset "
        "the path defaults to the per-user XDG location $XDG_CONFIG_HOME/kdive/systems.toml "
        "(falling back to ~/.config/kdive/systems.toml) — a CWD-independent default, never a "
        "working-directory-relative ./systems.toml. An absent default file is the normal "
        "pre-config state (systems.toml is gitignored) and is a quiet no-op, while a "
        "present-but-malformed file fails that pass without aborting siblings."
    ),
    suggest=(
        "a path to a systems.toml, e.g. ~/.config/kdive/systems.toml or /etc/kdive/systems.toml; "
        "leave unset for the XDG default"
    ),
)

INVENTORY_WRITEBACK = Setting(
    name="KDIVE_INVENTORY_WRITEBACK",
    parse=_str,
    group="inventory",
    processes=_SERVER,
    help=(
        "Opt-in target for ops.export_systems_toml(persist=true), which persists the exported "
        "inventory back to the live source the reconciler re-reads (ADR-0199, M2.7). Unset or "
        "'off' disables writeback (the export tool returns text only). 'configmap' patches the "
        "kdive-systems ConfigMap via the Kubernetes API (needs an RBAC Role granting patch on "
        "that one ConfigMap). 'file' writes the KDIVE_SYSTEMS_TOML path directly (only for a "
        "deployment whose inventory file is a writable volume shared with the reconciler)."
    ),
    suggest="one of: off, configmap, file",
)

INVENTORY_WRITEBACK_CONFIGMAP = Setting(
    name="KDIVE_INVENTORY_WRITEBACK_CONFIGMAP",
    parse=_str,
    default="kdive-systems",
    group="inventory",
    processes=_SERVER,
    help=(
        "Name of the ConfigMap ops.export_systems_toml(persist=true) patches when "
        "KDIVE_INVENTORY_WRITEBACK=configmap. The patched key is the inventory file name "
        "(systems.toml). The required RBAC Role must scope patch to this name only."
    ),
    suggest="the ConfigMap name, e.g. kdive-systems",
)

RESOURCE_LEASE_TTL_SECONDS = Setting(
    name="KDIVE_RESOURCE_LEASE_TTL_SECONDS",
    parse=_int,
    default=str(24 * 3600),
    group="inventory",
    processes=_SERVER,
    help=(
        "Lease window in seconds for a runtime-registered resource (resources.register). "
        "register_* sets lease_expires_at = now() + this window and resources.renew extends it "
        "by the same window; the reconciler reaps a runtime resource once its lease expires "
        "(ADR-0112). Tunes the leak-resistance horizon for imperatively-registered capacity."
    ),
    suggest="an integer number of seconds, e.g. 86400 (24 hours)",
)

FAULT_INJECT = Setting(
    name="KDIVE_FAULT_INJECT",
    parse=_str,
    group="fault-inject",
    processes=RUNNABLE,
    help="Presence (1/true/yes) registers the fault-injection provider.",
)

LOCAL_LIBVIRT_ENABLED = Setting(
    name="KDIVE_LOCAL_LIBVIRT_ENABLED",
    parse=_str,
    default="true",
    group="local-libvirt",
    processes=RUNNABLE,
    help=(
        "Whether the local-libvirt provider is composed (default on): its reconciler "
        "leaked-domain reaper and its provider-discovery registration and resolver runtime. "
        "Set to false on deployments with no local libvirt host (e.g. a remote-libvirt-only "
        "k8s deploy) so neither the leaked-domain sweep nor startup discovery fails on a "
        "missing socket."
    ),
)

OTEL_ENABLED = Setting(
    name="KDIVE_OTEL_ENABLED",
    parse=_str,
    group="otel",
    processes=RUNNABLE,
    help="Presence (1/true/yes) enables OTLP export of logs/metrics/traces (default off).",
)
OTEL_EXPORTER_OTLP_ENDPOINT = Setting(
    name="KDIVE_OTEL_EXPORTER_OTLP_ENDPOINT",
    parse=_str,
    group="otel",
    processes=RUNNABLE,
    help="OTLP/gRPC collector endpoint; required when KDIVE_OTEL_ENABLED is set.",
    suggest="a gRPC collector endpoint, e.g. http://otel-collector:4317",
)
OTEL_TRACES_SAMPLER_RATIO = Setting(
    name="KDIVE_OTEL_TRACES_SAMPLER_RATIO",
    parse=_ratio,
    default="0.1",
    group="otel",
    processes=RUNNABLE,
    help="Parent-based ratio trace sampler ratio in [0, 1] (default 0.1).",
    suggest="a float in [0, 1], e.g. 0.1",
)
OTEL_SERVICE_NAMESPACE = Setting(
    name="KDIVE_OTEL_SERVICE_NAMESPACE",
    parse=_str,
    default="kdive",
    group="otel",
    processes=RUNNABLE,
    help="service.namespace resource attribute on all emitted telemetry.",
)

HEALTH_BIND_ADDR = Setting(
    name="KDIVE_HEALTH_BIND_ADDR",
    parse=_str,
    default="127.0.0.1:9464",
    group="health",
    processes=frozenset({"server", "worker", "reconciler"}),
    help=(
        "host:port for the aux health/metrics listener (/livez /readyz /metrics), "
        "distinct from the MCP port. Loopback by default — the network boundary is its "
        "access control; widening it is an explicit act. When unset the port defaults "
        "per process (server 9464, worker 9465, reconciler 9466) so three processes on "
        "one host do not collide; an explicit value wins for every process."
    ),
    suggest="a host:port, e.g. 127.0.0.1:9464 (loopback) or 0.0.0.0:9464 (pod-local)",
)

MCP_TOOL_GATEWAY = Setting(
    name="KDIVE_MCP_TOOL_GATEWAY",
    parse=_str,
    default="on",
    group="mcp",
    processes=_SERVER,
    help=(
        "Enable the core-set tool gateway (ADR-0268): when set to on/1/true, list_tools "
        "returns only the CORE_TOOLS set for agent-profile callers (intersected with RBAC), "
        "so agents discover tools.search and tools.invoke first. Operator-CLI callers keep "
        "the direct RBAC catalog. Set off to restore the full ADR-0148 RBAC catalog."
    ),
)

COMPACT_RESPONSES = Setting(
    name="KDIVE_COMPACT_RESPONSES",
    parse=_str,
    default="off",
    group="mcp",
    processes=_SERVER,
    help=(
        "When on/1/true, the server omits null/empty defaulted fields from every tool "
        "response envelope (recursively within items) to cut per-call tokens (ADR-0314). "
        "Default off — the full ADR-0019 envelope. A failure envelope always keeps "
        "error_category and retryable; detail is kept when a reason exists."
    ),
)

MCP_TRACE = Setting(
    name="KDIVE_MCP_TRACE",
    parse=_str,
    group="logging",
    processes=_SERVER,
    help="Presence (1/true/yes) enables opt-in ASGI transport-trace logging (default off).",
)

WORKER_INCARNATION_KIND = Setting(
    name="KDIVE_WORKER_INCARNATION_KIND",
    parse=_choice("local", "docker", "kubernetes"),
    default="local",
    group="worker-death",
    processes=_WORKER,
    help="Immutable worker identity source: local process, Docker container, or Kubernetes Pod.",
)


def _docker_worker(env: Mapping[str, str]) -> bool:
    return env.get("KDIVE_WORKER_INCARNATION_KIND", "local") == "docker"


WORKER_INCARNATION_ID = Setting(
    name="KDIVE_WORKER_INCARNATION_ID",
    parse=_nonempty,
    group="worker-death",
    processes=_WORKER,
    required_when=_docker_worker,
    help="Lifecycle-gate-injected immutable Docker worker incarnation nonce.",
)
WORKER_DEATH_VERIFIER = Setting(
    name="KDIVE_WORKER_DEATH_VERIFIER",
    parse=_choice("disabled", "local", "docker", "kubernetes"),
    default="disabled",
    group="worker-death",
    processes=_SERVER,
    help="Authoritative worker-death verifier; disabled omits build-use recovery tools.",
)
DOCKER_DEATH_API = Setting(
    name="KDIVE_DOCKER_DEATH_API",
    parse=_nonempty,
    default="http://worker-death-api:2375",
    group="worker-death",
    processes=_SERVER,
    help="Private inspect-only Docker authority endpoint used by the Docker death verifier.",
)


def _kubernetes_worker(env: Mapping[str, str]) -> bool:
    return env.get("KDIVE_WORKER_INCARNATION_KIND", "local") == "kubernetes"


def _kubernetes_witness(env: Mapping[str, str]) -> bool:
    return bool(env.get("KDIVE_KUBERNETES_WITNESS_NAMESPACE"))


POD_NAMESPACE = Setting(
    name="KDIVE_POD_NAMESPACE",
    parse=_nonempty,
    group="worker-death",
    processes=_WORKER,
    required_when=_kubernetes_worker,
    help="Kubernetes worker Pod namespace supplied by the downward API.",
)
POD_NAME = Setting(
    name="KDIVE_POD_NAME",
    parse=_nonempty,
    group="worker-death",
    processes=_WORKER,
    required_when=_kubernetes_worker,
    help="Kubernetes worker Pod name supplied by the downward API.",
)
POD_UID = Setting(
    name="KDIVE_POD_UID",
    parse=_nonempty,
    group="worker-death",
    processes=_WORKER,
    required_when=_kubernetes_worker,
    help="Immutable Kubernetes worker Pod UID supplied by the downward API.",
)
KUBERNETES_WITNESS_NAMESPACE = Setting(
    name="KDIVE_KUBERNETES_WITNESS_NAMESPACE",
    parse=_str,
    default="",
    group="worker-death",
    processes=_RECONCILER,
    help="Namespace watched by the bounded worker termination witness; blank disables it.",
)
KUBERNETES_WITNESS_WORKER_NAME = Setting(
    name="KDIVE_KUBERNETES_WITNESS_WORKER_NAME",
    parse=_str,
    default="",
    group="worker-death",
    processes=_RECONCILER,
    help="StatefulSet worker name prefix used with bounded ordinal Pod reads.",
)
KUBERNETES_WITNESS_ORDINAL_CEILING = Setting(
    name="KDIVE_KUBERNETES_WITNESS_ORDINAL_CEILING",
    parse=_nonnegative_int_at_most(1_000),
    default="0",
    group="worker-death",
    processes=_RECONCILER,
    help=(
        "Maximum exclusive worker ordinal polled by the Kubernetes termination witness. The unit "
        "is one exact Kubernetes Pod name per ordinal; this count limit has no reference clock. "
        "Each reconciler pass observes the Kubernetes API for every configured Pod name and "
        "applies this limit per reconciler pass, processing at most 1,000 Pods. The valid "
        "inclusive range is 0..1,000; every out-of-range value (negative or above 1,000) "
        "is rejected at "
        "reconciler startup. No cursor is published: remaining finalized Pods are retained for the "
        "next scheduled invocation. To recover, set KDIVE_KUBERNETES_WITNESS_ORDINAL_CEILING to an "
        "integer in the inclusive range 0..1,000 and restart the reconciler."
    ),
)
KUBERNETES_CREDENTIAL_BROKER_HOST = Setting(
    name="KDIVE_KUBERNETES_CREDENTIAL_BROKER_HOST",
    parse=_nonempty,
    group="worker-death",
    processes=_RECONCILER,
    required_when=_kubernetes_witness,
    help="Private reconciler bind host for the Kubernetes worker credential broker.",
    suggest="the internal broker bind host, e.g. 0.0.0.0",
)
KUBERNETES_CREDENTIAL_BROKER_PORT = Setting(
    name="KDIVE_KUBERNETES_CREDENTIAL_BROKER_PORT",
    parse=_positive_int,
    group="worker-death",
    processes=_RECONCILER,
    required_when=_kubernetes_witness,
    help="Private TLS port for the Kubernetes worker credential broker.",
    suggest="a TCP port between 1 and 65535",
)
KUBERNETES_CREDENTIAL_BROKER_TLS_CERT = Setting(
    name="KDIVE_KUBERNETES_CREDENTIAL_BROKER_TLS_CERT",
    parse=_nonempty,
    group="worker-death",
    processes=_RECONCILER,
    required_when=_kubernetes_witness,
    help="Reconciler-only file reference for the broker TLS certificate.",
    suggest="a readable TLS certificate file path",
)
KUBERNETES_CREDENTIAL_BROKER_TLS_KEY = Setting(
    name="KDIVE_KUBERNETES_CREDENTIAL_BROKER_TLS_KEY",
    parse=_nonempty,
    secret=True,
    group="worker-death",
    processes=_RECONCILER,
    required_when=_kubernetes_witness,
    help="Reconciler-only file reference for the broker TLS private key.",
    suggest="a readable TLS private-key file path",
)
KUBERNETES_CREDENTIAL_BROKER_CA = Setting(
    name="KDIVE_KUBERNETES_CREDENTIAL_BROKER_CA",
    parse=_nonempty,
    group="worker-death",
    processes=_RECONCILER,
    required_when=_kubernetes_witness,
    help="Certificate-authority file reference trusted by the broker and init client.",
    suggest="a readable TLS CA certificate file path",
)
KUBERNETES_CREDENTIAL_ENVELOPE_KEY = Setting(
    name="KDIVE_KUBERNETES_CREDENTIAL_ENVELOPE_KEY",
    parse=_nonempty,
    secret=True,
    group="worker-death",
    processes=_RECONCILER,
    required_when=_kubernetes_witness,
    help="Reconciler-only Fernet key file for transient worker credential envelopes.",
    suggest="a readable Fernet envelope-key file path",
)

SETTINGS = [
    DATABASE_URL,
    MIGRATION_DATABASE_URL,
    WORKER_DATABASE_URL,
    LIFECYCLE_WITNESS_DATABASE_URL,
    HTTP_HOST,
    HTTP_PORT,
    LOG_LEVEL,
    OIDC_JWKS_URI,
    OIDC_ISSUER,
    OIDC_AUDIENCE,
    S3_ENDPOINT_URL,
    S3_BUCKET,
    S3_REGION,
    LEASE_DEFAULT,
    LEASE_MAX,
    PROVISION_PREMUTATION_TIMEOUT_S,
    UPLOAD_TTL_SECONDS,
    UPLOAD_ORPHAN_GRACE,
    UPLOAD_WINDOW_MAX_TTL_MULTIPLE,
    MAX_UPLOAD_BYTES,
    ARTIFACT_INLINE_MAX_BYTES,
    ARTIFACT_DOWNLOAD_TTL_SECONDS,
    REPORT_INLINE_MAX_BYTES,
    REPORT_ARTIFACT_RETENTION_DAYS,
    INVESTIGATION_CLEANUP_GRACE_DAYS,
    BUILD_ARTIFACT_RETENTION_DAYS,
    INVESTIGATION_ROOTFS_RETENTION_DAYS,
    MAX_INVENTORY_EXPORT_BYTES,
    DEBUG_DIR,
    CRASH_DIR,
    LIVE_SCRIPT_MAX_TIMEOUT_SECONDS,
    SECRETS_ROOT,
    BUILD_WORKSPACE,
    KERNEL_SRC,
    BUILD_COMPONENT_ROOTS,
    LOCAL_BUILD_REMOTE_ALLOWLIST,
    BUILD_USER,
    INSTALL_STAGING,
    INSTALL_SCRATCH,
    FIXTURE_CATALOG_PATH,
    IMAGE_PUBLISH_GRACE,
    IMAGE_PRIVATE_LIFETIME_DEFAULT,
    IMAGE_PRIVATE_LIFETIME_MAX,
    IMAGE_PRIVATE_MAX_COUNT,
    IMAGE_PRIVATE_MAX_BYTES,
    SYSTEMS_TOML,
    INVENTORY_WRITEBACK,
    INVENTORY_WRITEBACK_CONFIGMAP,
    RESOURCE_LEASE_TTL_SECONDS,
    FAULT_INJECT,
    LOCAL_LIBVIRT_ENABLED,
    OTEL_ENABLED,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_TRACES_SAMPLER_RATIO,
    OTEL_SERVICE_NAMESPACE,
    HEALTH_BIND_ADDR,
    MCP_TOOL_GATEWAY,
    COMPACT_RESPONSES,
    MCP_TRACE,
    WORKER_INCARNATION_KIND,
    WORKER_INCARNATION_ID,
    WORKER_DEATH_VERIFIER,
    DOCKER_DEATH_API,
    POD_NAMESPACE,
    POD_NAME,
    POD_UID,
    KUBERNETES_WITNESS_NAMESPACE,
    KUBERNETES_WITNESS_WORKER_NAME,
    KUBERNETES_WITNESS_ORDINAL_CEILING,
    KUBERNETES_CREDENTIAL_BROKER_HOST,
    KUBERNETES_CREDENTIAL_BROKER_PORT,
    KUBERNETES_CREDENTIAL_BROKER_TLS_CERT,
    KUBERNETES_CREDENTIAL_BROKER_TLS_KEY,
    KUBERNETES_CREDENTIAL_BROKER_CA,
    KUBERNETES_CREDENTIAL_ENVELOPE_KEY,
]
