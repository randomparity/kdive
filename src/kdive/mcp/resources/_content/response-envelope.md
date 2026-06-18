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

## List responses

`*.list` tools return a sequence of `ToolResponse` objects, one envelope per item.
Batch callers isolate construction per item so a single failed row does not blank
the whole list.
