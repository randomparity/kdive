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

from collections.abc import Mapping

from kdive.config.registry import RUNNABLE, Setting

_SERVER = frozenset({"server"})
_STORE_USERS = frozenset({"server", "worker", "reconciler"})
_WORKER = frozenset({"worker"})
_DISCOVERY = frozenset({"worker", "reconciler"})
# Processes that read the on-disk provider fixture catalog: the worker/reconciler build paths
# plus the server's fixtures.validate read (ADR-0120).
_CATALOG_READERS = frozenset({"server", "worker", "reconciler"})


def _int(raw: str) -> int:
    return int(raw)


def _str(raw: str) -> str:
    return raw


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


def _always(env: Mapping[str, str]) -> bool:
    return True


DATABASE_URL = Setting(
    name="KDIVE_DATABASE_URL",
    parse=_str,
    group="database",
    processes=RUNNABLE,
    required_when=_always,
    help="Postgres DSN for the system-of-record.",
    suggest="a Postgres DSN, e.g. postgresql://host:5432/kdive",
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
    parse=_str,
    group="oidc",
    processes=_SERVER,
    required_when=_always,
    help="JWKS URI the bearer-token verifier fetches signing keys from.",
    suggest="the issuer's JWKS endpoint, e.g. http://oidc:8080/default/jwks",
)
OIDC_ISSUER = Setting(
    name="KDIVE_OIDC_ISSUER",
    parse=_str,
    group="oidc",
    processes=_SERVER,
    required_when=_always,
    help="Expected token issuer (iss), enforced natively.",
    suggest="the OIDC issuer URL, e.g. http://oidc:8080/default",
)
OIDC_AUDIENCE = Setting(
    name="KDIVE_OIDC_AUDIENCE",
    parse=_str,
    group="oidc",
    processes=_SERVER,
    required_when=_always,
    help="Expected token audience (aud), enforced natively.",
    suggest="the audience this server accepts, e.g. kdive",
)

S3_ENDPOINT_URL = Setting(
    name="KDIVE_S3_ENDPOINT_URL",
    parse=_str,
    group="objectstore",
    processes=_STORE_USERS,
    help="S3-compatible endpoint URL for bulk artifacts.",
)
S3_BUCKET = Setting(
    name="KDIVE_S3_BUCKET",
    parse=_str,
    group="objectstore",
    processes=_STORE_USERS,
    help="Bucket holding vmcores, transcripts, and uploads.",
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
        "(systems.provision / systems.define: validation, lock acquisition, rootfs check). "
        "On exceed, the tool returns a transport_failure envelope instead of dropping the "
        "socket (ADR-0126)."
    ),
    suggest="a positive number of seconds, e.g. 30",
)

UPLOAD_TTL_SECONDS = Setting(
    name="KDIVE_UPLOAD_TTL_SECONDS",
    parse=_int,
    default="86400",
    group="upload",
    processes=_SERVER,
    help="Presigned upload-URL TTL in seconds.",
    suggest="an integer number of seconds, e.g. 86400",
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

MAX_BUILD_CONFIG_BYTES = Setting(
    name="KDIVE_MAX_BUILD_CONFIG_BYTES",
    parse=_int,
    default=str(256 * 1024),
    group="upload",
    processes=_SERVER,
    help=(
        "Maximum accepted build-config fragment size in bytes for buildconfig.set "
        "(ADR-0119). Kernel-config fragments are a few KiB; the cap bounds a hostile or "
        "accidental large upload."
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
        "Lease window in seconds for a runtime-registered resource (resources.register_*). "
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
    default="off",
    group="mcp",
    processes=_SERVER,
    help=(
        "Enable the core-set tool gateway (ADR-0268): when set to on/1/true, list_tools "
        "returns only the CORE_TOOLS set (intersected with RBAC), so agents discover "
        "tools.search and tools.invoke first. Default off — full ADR-0148 RBAC catalog."
    ),
)

SETTINGS = [
    DATABASE_URL,
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
    MAX_UPLOAD_BYTES,
    ARTIFACT_INLINE_MAX_BYTES,
    ARTIFACT_DOWNLOAD_TTL_SECONDS,
    REPORT_INLINE_MAX_BYTES,
    REPORT_ARTIFACT_RETENTION_DAYS,
    INVESTIGATION_CLEANUP_GRACE_DAYS,
    BUILD_ARTIFACT_RETENTION_DAYS,
    MAX_BUILD_CONFIG_BYTES,
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
]
