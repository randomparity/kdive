# Response envelope

Every KDIVE tool returns a single `ToolResponse` defined in
`src/kdive/mcp/responses.py`. The shape is fixed across all planes so an agent
learns one envelope and one polling pattern
([ADR-0019](../adr/0019-tool-response-envelope.md)).

## Fields

| Field | Type | Meaning |
|---|---|---|
| `object_id` | `str` | The primary object this response concerns (e.g. the `job_id` for `jobs.*`, the `system_id` for `systems.*`). |
| `status` | `str` | The object's lifecycle status as a plain string (e.g. `running`, `ready`, `failed`). |
| `suggested_next_actions` | `list[str]` | Literal next **tool names** the agent should consider (e.g. `["jobs.wait", "jobs.cancel"]`). No inference needed. |
| `refs` | `dict[str, str]` | Artifact **references** keyed by role (e.g. `{"result": "<object-store-key>"}`). Never inline artifact bytes or log text. |
| `error_category` | `str \| None` | Present if and only if `status` is a failure status (`error` or `failed`). Carries a value from the `ErrorCategory` taxonomy. `None` otherwise. |
| `retryable` | `bool \| None` | Derived from `error_category` (ADR-0118): `True` if a bare re-invocation may succeed once a transient condition clears, `False` for a permanent failure. `None` on any non-failure response. |
| `detail` | `str \| None` | Human-readable failure reason (ADR-0123). The redacted `CategorizedError` message on a failure; present-but-`null` on success and on job-handle envelopes. |
| `data` | `dict[str, JsonValue]` | Plane-specific JSON values that are not one of the above (e.g. `{"kind": "provision"}` on a job response). Open per-tool; see "Reading an open payload". |
| `items` | `list[ToolResponse]` | One nested envelope per element of a collection response. Empty on a non-collection response. See "List responses". |

## The `error_category` invariant

`error_category` is set **iff** the response reports a failure status. The model
enforces this at construction time: a failure status without a category, or any
non-failure status carrying one, raises at the tool boundary. This means a caller
can safely check `error_category is None` to distinguish success from failure
without parsing `status`.

Two distinct statuses count as a failure, and they originate differently:

- **`error`** is what a *direct tool failure* carries. The `failure()` factory
  always sets `status="error"` plus the `error_category` — so a synchronous tool
  rejection (bad input, authorization denied, sequencing error) is always `error`,
  never `failed`.
- **`failed`** is a *job terminal state*. It appears only on job-handle envelopes
  built from a `Job` row (via `from_job`), surfaced through `jobs.get` / `jobs.wait`
  when a long-running operation fails. A direct tool call never returns `failed`.

See [errors](errors.md) for the taxonomy and recovery guidance.

## References, not log dumps

The `refs` field carries object-store keys, not raw artifact bytes or console
transcripts. The `data` field holds JSON values (`dict[str, JsonValue]`), checked
by `validate_json_value` at construction. There is no field for inline log text —
this is structural: a tool cannot accidentally return a raw transcript or vmcore
dump in the envelope. Artifact bytes are fetched separately via `artifacts.get`
after the agent inspects the reference.

All guest output, gdb/SoL transcripts, and console logs pass through the redactor
before persistence and before any response snippet. See [safety and RBAC](safety-and-rbac.md)
for the redaction contract.

## Reading an open payload

The advertised tool `outputSchema` (`tools/list`) documents these envelope fields
([ADR-0170](../adr/0170-fielded-tool-output-schema.md)), but it advertises `data`
as a generic object and `items` as an array of generic objects on purpose: the
per-tool shape of these two fields is intentionally open. Read them like this:

- **`data`** carries plane-specific scalars keyed by name. The keys depend on the
  tool — `{"kind": "provision"}` on a job handle, `{"count": 3}` on a collection,
  `{"current_status": "running"}` on a conflict. The per-plane tool docs name the
  keys a given tool sets; the envelope does not enumerate them.
- **`items`** is populated only by collection-returning tools (`*.list`); each entry
  is a full `ToolResponse` with the same fields described above. Recurse into an
  entry exactly as you read the top-level envelope.
- **`refs`** are object-store keys, never inline bytes. Resolve a reference with
  `artifacts.get` after you decide you need the artifact.

A black-box agent therefore needs only this one envelope contract plus the per-tool
input schema; it never has to special-case each tool's result shape.

## Idempotent retries

The transport-reset retry contract blesses re-invoking idempotent *reads* after a
transient drop. For *mutations*, a blind retry of the initial create/enqueue could
double-act. To make a mutation retry safe, every object-creating / job-enqueuing tool
accepts an optional `idempotency_key` ([ADR-0193](../adr/0193-uniform-mutation-idempotency.md)):

- **What it covers.** The create/enqueue mutations — `runs.create` /
  `runs.install` / `runs.boot`, `systems.provision` / `systems.define` /
  `systems.provision_defined` / `systems.reprovision` / `systems.teardown`,
  `vmcore.fetch`, `control.power` / `control.force_crash`, `investigations.open`, and
  `allocations.request` / `allocations.renew`. Pure state-transition mutations that act on
  an existing object by id (e.g. `runs.cancel`, `allocations.release`,
  `investigations.close`) are naturally idempotent and take no key.
- **Replay, not re-action.** A repeated `idempotency_key` returns the **identical prior
  envelope** — the same object/job, byte-for-byte the same fields — instead of creating a
  second object or enqueuing a second job. A keyed retry after a transport drop is safe.
- **Principal-scoped.** Keys are scoped to your principal; one tenant's key can never
  resolve another's envelope.
- **Success-only.** A key is recorded only when the mutation succeeds. A failed call (a
  denial or a validation error) records nothing, so you may correct the input and retry the
  same key.
- **One key per logical operation.** Reusing one key across two different tools fails closed
  with a `conflict` error — mint a fresh key per operation. A key is at most 200 characters.
- **Window.** A key replays only within the retention window (see
  [async jobs](async-jobs.md)); after it is garbage-collected, a repeat is treated as a fresh
  request.

## List responses

`*.list` tools return a sequence of `ToolResponse` objects, one envelope per item.
Batch callers isolate construction per item so a single failed row does not blank
the whole list.

### Pagination

Every `*.list` tool is opt-in keyset-paginated
([ADR-0192](../adr/0192-list-pagination-envelope.md)). The contract lives in `data`:

| Key | Type | Meaning |
|---|---|---|
| `truncated` | `bool` | `true` iff more rows match than were returned. Deterministic, never best-effort. |
| `next_cursor` | `str \| None` | An opaque continuation token, present (non-`null`) **iff** `truncated` is `true`. Pass it back as the next call's `cursor` to read the next page. |
| `total` | `int` | Present only where it is cheap to compute (the bounded single-System `artifacts.list`). |
| `count` | `int` | The per-page item count (always present on a collection). |

Paginated list tools take optional `cursor` and `limit` fields in their request
payload; `limit` defaults to 50 and is capped at 200. To read a full result set,
call the tool, then keep re-calling it with `cursor = data.next_cursor` until
`data.truncated` is `false`:

```text
page = jobs.list(request={"limit": 50})
while page.data.truncated:
    page = jobs.list(request={"limit": 50, "cursor": page.data.next_cursor})
```

Rules:

- **Cursors are opaque.** Do not parse or construct one — only echo back a
  `next_cursor` you received. The token encodes the page boundary, not a row offset.
- **Cursors are tool-specific.** A cursor minted by one list is rejected by another
  with a `configuration_error` (`data.reason = "invalid_cursor"`); a malformed cursor
  is the same error. A bad cursor is never silently treated as "first page".
- **Cursors are not security tokens.** Every page re-applies the caller's project/role
  scoping, so a cursor only shifts the page boundary within rows the caller may see.
- **Keyset, not offset.** Following a cursor is stable under concurrent inserts: a row
  added at the head never makes a later page skip or repeat a row.
- **`inventory.list` is the one non-continuable list.** It summarizes two independent
  streams (allocations + systems), so it reports `truncated` but emits no `next_cursor`;
  narrow it with the `project` / `resource_id` filters instead.

## Compact responses (opt-in)

When an operator sets `KDIVE_COMPACT_RESPONSES=on` (default `off`), the server omits
null/empty *defaulted* envelope fields from every tool response, recursively within `items`,
to cut per-call tokens on token-heavy list tools
([ADR-0314](../adr/0314-compact-response-envelope.md)). The default is unchanged and
byte-identical.

Under compaction:

- A field at its default is **omitted**: `error_category`/`retryable`/`detail` when null,
  and `suggested_next_actions`/`refs`/`items`/`data` when empty. `object_id` and `status`
  are always present.
- A failure envelope always keeps `error_category` and `retryable`. `detail` is kept only
  when non-null (a `not_found`/`authorization_denied` suppressed constant, or a reason the
  tool set); a worker-plane job-handle failure whose `detail` is null omits it.
- **Absent means default.** An omitted field is semantically identical to its documented
  default (empty list/dict, or null). A consumer must not read key-absence as a distinct
  "unknown" signal. This applies to first-party clients too — the `response.get("items", [])`
  idiom is compaction-safe, and a *populated* collection's `items` is never dropped (only an
  empty one is), so index access on a known-populated collection is unaffected.

The advertised output schema types every omittable field as optional/nullable, so compact
responses stay schema-valid.
