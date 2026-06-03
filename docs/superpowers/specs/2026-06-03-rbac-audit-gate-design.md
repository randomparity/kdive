# RBAC, audit log & destructive-op gate — Design

**Issue:** #11 (M0) · **Depends on:** #7 (repository layer / `audit_log` table —
merged), #10 (MCP skeleton / `RequestContext` — merged) · **Decisions:**
[ADR-0006](../../adr/0006-oidc-rbac-attribution.md) (OIDC/RBAC + attribution),
[ADR-0020](../../adr/0020-rbac-audit-gate-implementation.md) (the M0 implementation
shapes this spec realizes), [ADR-0019](../../adr/0019-tool-response-envelope.md)
(response envelope) · **Parent spec:**
[`docs/specs/m0-walking-skeleton.md`](../../specs/m0-walking-skeleton.md) ("Auth,
RBAC & attribution", "Cross-cutting in M0 → Audit", exit criterion 6)

## Goal

The three security primitives every later plane tool composes:

- `src/kdive/security/rbac.py` — the `Role` enum (`viewer`/`operator`/`admin`) with a
  total rank, `roles_from_claims` (parse a `roles` token claim), and
  `require_role(ctx, project, role)`.
- `src/kdive/security/audit.py` — `record(...)`: one append-only `audit_log` row per
  call, inside the caller's transaction, with a one-way `args_digest`.
- `src/kdive/security/gate.py` — `assert_destructive_allowed(ctx, allocation, op)`:
  the three-check destructive gate (capability scope, `admin` role, profile opt-in).

Plus the minimal plumbing to thread roles through the request context:
`RequestContext` (`src/kdive/mcp/auth.py`) gains a `roles: Mapping[str, Role]` field,
and `context_from_claims` populates it via `rbac.roles_from_claims`.

This layer sits **above** the repository/auth layers (#7, #10) and **below** the
plane handlers (#13+) that call it. It owns *who may do what* and *what gets
audited*; it does **not** own *when* a transition happens (the handler) or the
wire-level response mapping of a denial (the handler, see Non-goals).

## Non-goals

- **No repository or worker wiring.** Per the issue's Files list and the scoping
  decision on #11, `record`/`require_role`/the gate are **not** called from
  `db/repositories.py` or `jobs/worker.py` in this issue. The
  "every transition writes exactly one audit row" property is delivered as the
  *contract* of `record` (transactional, append-only, one row) and **proven** by a
  test that performs a real `StatefulRepository.update_state` and a `record` in one
  transaction and asserts exactly one `audit_log` row. The per-handler wiring is owned
  by the plane-tool issues (#13+) that introduce the transitions. (Stated so the
  unused-at-the-repository-layer primitives are not mistaken for dead code.)
- **No `ErrorCategory` for denials.** `require_role`/the gate **raise**
  (`AuthorizationError`/`DestructiveOpDenied`); they do not build a `ToolResponse`.
  The M0 taxonomy has no authorization category and "do not invent strings" forbids
  adding one with no producer ([ADR-0020](../../adr/0020-rbac-audit-gate-implementation.md)).
  The first destructive handler (a later issue) maps the denial onto a response.
- **No IdP / claim-issuance work.** This consumes a `roles` claim from an
  already-verified token (#10 owns verification). The claim *name/shape* is pinned
  here as the provisional contract #13/IdP integration must honor (ADR-0020).
- **No redaction port.** `args_digest` is a hash, so secrets in `args` are never
  *revealed* by the audit row regardless of redaction. The redactor
  (`security/redaction.py`, #23) is a separate concern for guest-output bytes, not the
  audit digest.
- **No `operator`-with-opt-in relaxation.** ADR-0006 allows `operator` to perform a
  destructive op where the profile opt-in permits; M0 requires `admin`
  unconditionally (m0 spec factor (b)). Deferred (ADR-0020 Alternatives).
- **No capability-scope typed model.** The gate reads one documented key
  (`destructive_ops`) from the `dict[str, Any]` `capability_scope`; the typed interior
  lands with the allocation issue that owns it.

## Components

### `rbac.py` — roles & enforcement

```python
class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

_RANK: dict[Role, int] = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}

def roles_from_claims(claims: Mapping[str, object]) -> dict[str, Role]: ...
def require_role(ctx: RequestContext, project: str, role: Role) -> None: ...
```

- **`Role`** — the three M0 roles as stable wire strings (they match the claim's role
  values and the spec vocabulary). The `_RANK` map encodes the total order
  `viewer < operator < admin`; a higher role satisfies a lower requirement.
- **`roles_from_claims(claims)`** reads the `roles` claim, a JSON object mapping a
  project name to a single role string (`{"proj-a": "admin", "proj-b": "operator"}`).
  - Missing/`None` `roles` claim → `{}` (a token may grant membership via `projects`
    without any role; such a principal is effectively `viewer`-less and fails every
    `require_role`).
  - The claim must be a `dict`; a non-object (`list`, `str`, …) raises `AuthError`
    (malformed token, consistent with `context_from_claims`'s other claim checks).
  - Each value must be a known `Role` string; an unknown role (`"superadmin"`) raises
    `AuthError` (fail closed — never silently drop an unrecognized grant to nothing,
    and never treat it as a known role). Keys are coerced to `str`.
  - Returns a plain `dict[str, Role]`.
- **`require_role(ctx, project, role)`** raises `AuthorizationError` unless **both**:
  (1) `project in ctx.projects` — membership; and (2)
  `_RANK[ctx.roles.get(project)] >= _RANK[role]` — the principal holds at least the
  required role on that project. Returns `None` on success. Re-checking membership
  here makes a stray `roles` entry on a non-granted project unusable without trusting
  claim consistency. The message names the principal, project, required vs. held role
  (no secret material).

### `audit.py` — the append-only record

```python
async def record(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    tool: str,
    object_kind: str,
    object_id: UUID,
    transition: str,
    args: Mapping[str, object],
    project: str,
) -> UUID: ...

def args_digest(args: Mapping[str, object]) -> str: ...   # sha256 hex
```

- **`args_digest(args)`** — `hashlib.sha256` of a canonical JSON encoding:
  `json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)`, UTF-8
  encoded, hex digest. Canonicalization makes the digest stable across key order;
  `default=str` keeps non-JSON-native values (UUID, datetime) encodable. The digest is
  one-way, so any secret value present in `args` is committed-to but never
  **revealed** by the stored row — satisfying "args_digest never contains secret
  material".
- **`record(...)`** issues exactly one
  `INSERT INTO audit_log (principal, agent_session, project, tool, object_kind,
  object_id, transition, args_digest) VALUES (…) RETURNING id`. `id`/`ts` are
  DB-generated (defaults). `principal`/`agent_session` come from `ctx`; `project` is
  the explicit argument (the audited object's project, which the caller authorized via
  `require_project`/`require_role`), **not** `ctx.projects` (the granted *set*).
  Returns the new row's `id`.
- **Transactionality.** `record` runs its `INSERT` on the passed `conn` and does
  **not** open a transaction. The caller composes the state transition and `record`
  inside one `conn.transaction()` so both commit atomically (or neither does). This is
  how "exactly one audit row per transition" holds under a mid-operation crash.
- **Append-only.** `record` only `INSERT`s; the module exposes no update/delete, and
  `audit_log` has no `updated_at`/trigger (schema `0001_init.sql`). Append-only is
  structural, not enforced by a runtime guard.

### `gate.py` — the three-check destructive gate

```python
@dataclass(frozen=True)
class DestructiveOp:
    kind: str               # "force_crash" | "power" | "teardown" | …
    profile_opt_in: bool = False

_DESTRUCTIVE_OPS_KEY = "destructive_ops"

def assert_destructive_allowed(
    ctx: RequestContext, allocation: Allocation, op: DestructiveOp
) -> None: ...
```

`assert_destructive_allowed` evaluates **all three** checks and raises
`DestructiveOpDenied(missing=[…])` if any failed (listing every missing check, so an
audit/log line shows the full reason, not just the first failure):

| # | check | passes when | data source |
|---|-------|-------------|-------------|
| a | capability scope | `op.kind in allocation.capability_scope.get("destructive_ops", ())` | `allocation.capability_scope` (jsonb) |
| b | admin role | `require_role(ctx, allocation.project, Role.ADMIN)` does not raise | `ctx.roles` |
| c | profile opt-in | `op.profile_opt_in is True` | `op` (handler-resolved) |

- Check (b) calls `require_role` and converts its `AuthorizationError` into the
  `"admin_role"` missing-check entry, so the gate raises one uniform
  `DestructiveOpDenied` rather than two exception types.
- All three present → returns `None`. `op.profile_opt_in` defaults to `False`, so a
  handler that forgets to resolve and pass the opt-in is denied (deny-by-default).
- `capability_scope.get` tolerates a non-dict/absent scope as "no destructive ops
  granted" → check (a) fails closed.

### `auth.py` — context plumbing (minimal change)

```python
@dataclass(frozen=True)
class RequestContext:
    principal: str
    agent_session: str | None
    projects: tuple[str, ...]
    roles: Mapping[str, Role] = field(default_factory=dict)
```

`context_from_claims` sets `roles=roles_from_claims(claims)`. The default keeps every
existing direct construction of `RequestContext` (tests, handlers) valid with an empty
role map. To avoid a runtime import cycle, `rbac.py` imports `RequestContext` only
under `TYPE_CHECKING`; `auth.py` imports `Role`/`roles_from_claims` from `rbac` at
runtime.

### Errors

```python
class AuthorizationError(Exception): ...                 # rbac.py
class DestructiveOpDenied(AuthorizationError):           # gate.py
    def __init__(self, missing: list[str]) -> None: ...
    missing: list[str]
```

`AuthError` (auth.py, unchanged) = authentication-adjacent / membership failures.
`AuthorizationError` = RBAC denial. `DestructiveOpDenied` = gate denial, carrying the
missing checks (a subset of `{"capability_scope", "admin_role", "profile_opt_in"}`).
Neither authz error carries an `ErrorCategory` (ADR-0020).

## Failure modes & edges (drives the tests)

**rbac**
- `roles_from_claims`: absent claim → `{}`; `{"p":"admin"}` → `{"p": Role.ADMIN}`;
  non-dict claim → `AuthError`; unknown role value → `AuthError`; non-str keys coerced.
- `require_role`: held > required (admin for operator-required) → ok; held == required
  → ok; held < required (operator for admin) → `AuthorizationError`; project not in
  `ctx.projects` → `AuthorizationError`; project granted but absent from `roles` →
  `AuthorizationError`.

**audit**
- `args_digest`: deterministic across key reorder; differs for differing args; a known
  secret string in `args` does **not** appear in the digest (hex, secret not a
  substring); UUID/datetime values encode without error.
- `record`: one call → exactly one `audit_log` row with the expected
  principal/agent_session/project/tool/object_kind/object_id/transition and a digest
  matching `args_digest(args)`; `agent_session=None` persists as SQL `NULL`; returns
  the row id.
- transition+audit atomicity: a real `update_state` plus `record` in one transaction →
  exactly one audit row (the "per transition" property); rolling back the transaction
  leaves **zero** audit rows (proves enlistment in the caller's transaction).
- append-only: the module exposes no update/delete entry point (asserted structurally
  in the test/by inspection).

**gate**
- all three present → returns `None` (allowed).
- scope absent (op.kind not in `destructive_ops`) → `DestructiveOpDenied(["capability_scope"])`.
- not admin (operator role) → `DestructiveOpDenied(["admin_role"])`.
- opt-in false (default) → `DestructiveOpDenied(["profile_opt_in"])`.
- multiple absent → `missing` lists all of them.
- `capability_scope` missing the key / not a dict → scope check fails closed.

## Testing strategy

Primitives are the unit of testing (repo contract): call `roles_from_claims`,
`require_role`, `args_digest`, `record`, `assert_destructive_allowed` directly with
hand-built `RequestContext`/`Allocation`/`DestructiveOp` values. `rbac` and `gate` are
pure and need **no** DB. `audit.record` and the transition-atomicity test use the
existing testcontainers Postgres fixtures (`migrated_url`, the async idiom
`asyncio.run(_run())`) from `tests/db/conftest.py` — promoted to a shared location (or
imported) so `tests/security/` can reuse them. The three destructive-gate acceptance
tests flip exactly one factor each. No new gated integration tests; nothing here needs
libvirt/gdb/drgn.

Tests live in `tests/security/` mirroring the package: `test_rbac.py`,
`test_audit.py`, `test_gate.py`.
