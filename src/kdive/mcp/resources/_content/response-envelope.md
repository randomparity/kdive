# Response envelope

KDIVE tool results use `ToolResponse`, defined in `src/kdive/mcp/responses.py`. This guide owns
its common fields and how clients interpret them. Each tool's description still owns the meaning
of its payload and references; the [tool reference](reference/index.md) documents those contracts.
Transport and MCP protocol failures can occur before a tool produces an envelope.

## Fields

| Field | Type | Meaning |
|---|---|---|
| `object_id` | `str` | Primary identifier or tool-defined label. A job handle uses the job ID; an entity read uses the entity ID. Failure and collection responses can use labels instead of UUIDs. |
| `status` | `str` | Tool result or object state, such as `ok`, `running`, `ready`, `error`, or `failed`. Interpret it using the tool's contract. |
| `suggested_next_actions` | `list[str]` | Suggested tool names. These are hints; each call still enforces its permissions and preconditions. |
| `refs` | `dict[str, str]` | Named references. Their values can be artifact IDs, object-store keys, or URLs; use the producing tool's contract to choose how to consume them. |
| `error_category` | `str` or `null` | An `ErrorCategory` value exactly when `status` is `error` or `failed`; otherwise `null`. |
| `retryable` | `bool` or `null` | Derived from the error category. `true` identifies a potentially transient failure; `false` requires another recovery path. It does not make an arbitrary mutation safe to repeat. |
| `detail` | `str` or `null` | Optional human-readable failure information. It can be absent or suppressed; do not parse it as a machine contract. |
| `data` | JSON object | Tool-specific values, including nested objects, arrays, and strings. |
| `items` | `list[ToolResponse]` | Nested result envelopes, for example the entries of a collection or diagnostic checks. |

Optional fields have empty-container or `null` defaults. They may be omitted when response
compaction is enabled; see [compact responses](#compact-responses-opt-in).

## The `error_category` invariant

The model requires an error category for `error` and `failed`, and rejects one on other
statuses. It derives `retryable` from the category. `failure()` produces `error`; a failed job
rendered by `from_job()` carries `failed`. Inspect each envelope, including nested items.

No error category means **no classified failure in that envelope**, not that the requested work
has completed. A job can be `queued` or `running`, and a `canceled` job also has no error category.
An outer collection can be `ok` while a nested item reports a failure. Tool-specific results can
also describe failed checks or intermediate steps inside `data`.

See the errors guide (resource://kdive/docs/guide/errors.md) for categories and recovery.

## References, not log dumps

The `refs` field carries artifact identifiers, object-store keys, or download URLs, depending on
the tool; it does not carry the artifact bytes. `data` holds JSON values and can include strings,
such as the bounded `data.content` returned by `artifacts.get`. JSON validation does not redact
those strings or structurally prevent inline logs.

Use `artifacts.get` to read redacted artifact content and `artifacts.fetch_raw` for explicitly
requested raw debug assets. Their role and sensitivity rules differ. See the safety-and-RBAC
guide (resource://kdive/docs/guide/safety-and-rbac.md) for those access rules and the limits of
redaction.

## Reading an open payload

The advertised `outputSchema` describes the common envelope fields. Its `data` object and
`items` objects are intentionally open, so it does not enumerate every tool's returned keys.
Clients need the producing tool's result description as well as the common schema.

For a job handle, pass `object_id` as `job_id` to `jobs.wait`. `data.kind` describes the job kind;
some producers additionally include target IDs such as `data.run_id` or `data.system_id`.
Those extra IDs are not guaranteed by the common job renderer. Do not treat `data.kind` as a
universal discriminator for every tool, or assume `refs.result` always names the target entity.

For a collection, read the outer envelope first and then each entry in `items`. Collections are
not confined to `*.list`: diagnostics and accounting reports also return nested envelopes.
The collection factory sets `data.count` to the number of items returned, not the total number
of matching objects across all pages.

## Idempotent retries

Response interpretation and safe retries are separate contracts. Poll a returned job handle to
learn its current state; a replayed mutation result can describe an earlier state. See the
async-jobs guide (resource://kdive/docs/guide/async-jobs.md) for initial-call retries,
idempotency keys, and their retention boundary.

## List responses

A collection response is one envelope with nested `items`, not a bare sequence of envelopes.
Pagination support and request shape belong to the individual tool. For example, `jobs.list`
supports a cursor, while `shapes.list` returns its catalog without pagination parameters.

### Pagination

For tools using the common keyset pagination contract:

| `data` field | Meaning |
|---|---|
| `count` | Items in this response. |
| `truncated` | More matching results were observed beyond the returned page. |
| `next_cursor` | Continuation token when the tool supports continuation and has another page; otherwise `null` or absent. |

Read the tool's schema for its limit and cursor placement. This pseudocode shows `jobs.list`;
process every page's items and keep any filters unchanged:

```text
request = {"limit": 50}
loop:
    page = jobs.list(request=request)
    if page.get("error_category") is not null: handle failure and stop
    process(page.get("items", []))
    if not page.data.truncated: stop
    request.cursor = page.data.next_cursor
```

Treat a cursor as opaque and return it only to its producing tool. The common decoder rejects
malformed or wrong-tool cursors with `configuration_error` and `data.reason = "invalid_cursor"`.
A cursor describes a position, not an authorization grant or a snapshot: each request applies
its own filters and permissions, and data can change between pages.

A truncated result does not always support continuation. `inventory.list`, for example,
summarizes separate allocation and system streams without a next cursor; narrow its filters
instead. Follow each tool's truncation contract rather than applying the loop above to every list.

## Compact responses (opt-in)

`KDIVE_COMPACT_RESPONSES=on` enables omission of defaulted envelope fields; the default is `off`.
Compaction also applies to nested `items`. Clients should accept both forms:

- `object_id` and `status` remain present.
- Null `error_category`, `retryable`, and `detail` can be omitted. Failures keep their category
  and retryability; a non-null detail remains present.
- Empty `suggested_next_actions`, `refs`, `items`, and `data` can be omitted. A nonempty `data`
  object retains its tool-specific contents; this is not a general cleanup of null payload values.

For these optional envelope fields, absence means the field's default. For example,
`response.get("items", [])` handles an empty collection in either form. Do not interpret an
omitted field as a new state. Compaction leaves non-envelope or invalid result shapes untouched;
it is not a validator that repairs producer errors.
