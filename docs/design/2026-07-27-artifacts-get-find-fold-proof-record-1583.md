# Proof record — folding artifact text search into `artifacts.get` (#1583)

Date: 2026-07-27
Issue: #1583 · Epic: #1576
ADRs: [0462](../adr/0462-restore-folded-artifact-jump-cursor.md) (this change),
[0283](../adr/0283-artifact-get-jump-cursor.md) (the restored contract),
[0456](../adr/0456-agent-operator-mcp-exposure-profiles.md) §3 (retired-name search vocabulary),
[0268](../adr/0268-tool-gateway-dispatcher.md) §7 (real-client verification)

## What this proves

Removing `artifacts.find` deletes a tool that was never in `CORE_TOOLS`. With the gateway now on by
default (ADR-0456 / #1582), both it and `artifacts.get` were reachable only through `tools.search`
+ `tools.invoke`. So the risk this change carries is not "does the code still work" — unit tests
answer that — but **"can an agent that has never seen this server still find artifact text search
at all, now that the tool name is gone?"** A green test suite cannot answer that; only a real
client can.

This record collects, against a live local stack running the branch:

1. raw `list_tools` ground truth for both exposure profiles, showing `artifacts.find` gone;
2. a **real Claude Code cold start** that discovers artifact text search from intent alone and
   invokes `artifacts.get` with `find`, hitting the exact seeded offset;
3. live confirmation of the >1 MiB branch asymmetry ADR-0283 legislates, both branches;
4. live confirmation that the retired name is search vocabulary and **not** a callable alias;
5. the regenerated `kdivectl artifacts get --find` verb executing under the operator profile.

## Environment

- Host: x86_64 Fedora, Linux 7.1.3.
- Stack: `KDIVE_WORKER_AS_ROOT=0 scripts/live-stack/up.sh --skip-libvirt --skip-obs` —
  Postgres/MinIO/mock-OIDC backends plus host server/reconciler/worker at
  `http://127.0.0.1:8000/mcp`. Server build stamp `0.4.1-dev+gaf44bf155`, i.e. this branch.
- `KDIVE_MCP_TOOL_GATEWAY` **unset** throughout, so every run exercises the ADR-0456 default.
- Real client: Claude Code `2.1.220`, headless (`claude -p`) from an **empty** working directory
  with `--strict-mcp-config` and every non-MCP tool denied — the agent had the kdive MCP server and
  nothing else. No repository, no docs, no prior knowledge of kdive.
- Tokens minted from the local mock issuer, differing only in `azp`: agent (`azp=kdive-test`) and
  operator CLI (`azp=kdivectl`).
- Fixture: one synthetic REDACTED console artifact seeded into MinIO + `artifacts`, 169,341 bytes
  of boot chatter with a single KASAN report buried at byte **98,757**, line **1,402**; and a
  second one of 1,491,141 bytes (above the 1 MiB windowed-fetch ceiling) for the asymmetry check.

## 1. Profile ground truth — the tool is gone, on both surfaces

Raw MCP `list_tools` over the wire, no agent in the loop:

| token | tools listed | `artifacts.find` present |
|-------|--------------|--------------------------|
| agent (`azp=kdive-test`) | **9** — exactly `CORE_TOOLS` | no |
| operator CLI (`azp=kdivectl`) | **136** — the full RBAC-visible catalog | no |

136 matches the in-process registry count (`len(CLASSIFIED_TOOLS | PUBLIC_TOOLS)`), down from 137
on `main`. The `artifacts` namespace now holds five tools: `create_investigation_upload`,
`create_run_upload`, `fetch_raw`, `get`, `list`.

Gateway search, same connection:

```
tools.search("artifacts.find")        -> ["artifacts.get"]
tools.search("search text in a log")  -> ["artifacts.get", "tools.search", "postmortem.crash", ...]
```

The retired name resolves to exactly one tool, and the *intent phrase* — the query an agent that
never knew the old name would write — ranks `artifacts.get` first.

## 2. Agent cold start — intent to capability, with no tool name to go on

One `claude -p` run, 6 turns, 51 s. The client bound exactly nine kdive tools:
`allocations.request`, `allocations.wait`, `runs.create`, `runs.get`, `runs.list`,
`session.whoami`, `systems.provision`, `tools.invoke`, `tools.search`. Neither `artifacts.get` nor
anything else in the artifacts plane was among them.

The agent was told only that an artifact id held a redacted console log, and asked to locate the
KASAN report *without paging the whole log* and report the byte offset and line number.

- **Discovery was by capability, not by name.** Its first call was
  `tools.search("search inside a large artifact log for a pattern without downloading the whole
  file")`, which returned `artifacts.get` with its full input schema. This is the load-bearing
  step: had the `search`/`find`/`text` vocabulary not moved onto `artifacts.get`'s
  `TOOL_KEYWORDS` entry, this query would have returned nothing usable and the capability would
  have been unreachable despite the code being present.
- **It then probed the retired name.** `tools.search("artifacts.find")` returned only
  `artifacts.get` — and the agent read that correctly, as a ranking hit on the parameter rather
  than proof of a tool. It cross-checked with `tools.search(namespace="artifacts")`, got
  `truncated: false` and five tools, and concluded `artifacts.find` does not exist.
- **Invocation, first try, correct.** `tools.invoke(name="artifacts.get", arguments={"request":
  {"artifact_id": …, "find": "BUG: KASAN", "max_bytes": 700, "direction": "forward"}})` returned:

  ```json
  {"size_bytes": 169341, "match_found": true, "match_offset": 98757,
   "match_line": 1402, "next_offset": 98834, "content": "BUG: KASAN: slab-out-of-bounds in …"}
  ```

  `match_offset` and `match_line` are byte-exact against the seeded fixture. No prior read of the
  artifact, no paging, and the agent never saw a `find` example — the `Field` description was the
  whole contract.

The agent's own accounting: ~700 bytes pulled out of a 169,341-byte log, against the seven
round-trips and ~45k tokens a full page-through at the 24 KiB ceiling would have cost. That
token-economics argument is exactly ADR-0283's stated justification for having a server-side
filter at all, restated independently by a client that had never read the ADR.

## 3. The >1 MiB asymmetry, live on both branches

Against the 1,491,141-byte artifact, through the gateway:

| call | result |
|------|--------|
| `artifacts.get(artifact_id)` — plain | `status=available`, `data.content_omitted="artifact_too_large"`, `refs.download_uri` present |
| `artifacts.get(artifact_id, find="BUG: KASAN")` | `status=error`, `error_category=configuration_error`, `data.reason="artifact_too_large"`, **no** `match_found` |

And the negative control on the small artifact, so the two "no answer" shapes stay distinguishable:

| call | result |
|------|--------|
| `artifacts.get(small_id, find="NO_SUCH_TERM")` | `status=available`, `match_found=false`, no `content`, no `next_offset` |

This is the asymmetry ADR-0283's Consequences legislate, kept deliberately rather than smoothed
away by the fold. A caller can tell "I searched and there is no such crash" (`match_found=false`)
from "I could not search this log at all" (`configuration_error`), which is the whole point —
collapsing them would let an unsearchable log read as a clean one.

## 4. The retired name is vocabulary, not an alias

```
tools.invoke(name="artifacts.find", …)
-> status=error, error_category=configuration_error,
   detail="No tool named 'artifacts.find' is registered or enabled;
           discover available tools with tools.search."
```

`RETIRED_TOOL_NAMES` makes the old name *findable*; it does not make it *callable*, and the error
points the caller at the mechanism that will resolve it.

## 5. Operator profile — the regenerated verb

`kdivectl artifacts find` no longer parses: `invalid choice: 'find' (choose from
create-investigation-upload, create-run-upload, fetch-raw, get, list)`. Its replacement executes
under an `azp=kdivectl` token:

```
kdivectl artifacts get --artifact-id <id> --find "BUG: KASAN" --max-bytes 300 --json
-> {"data": {"size_bytes": 169341, "match_found": true, "match_offset": 98757,
             "match_line": 1402, "next_offset": 98834, "content": "BUG: KASAN: …"}}
```

Under an *agent*-profile token the same verb refuses with `'artifacts.get' is not positively
classified (read-only/mutating/destructive); it is unreachable` — the fail-closed behavior
ADR-0456 §4 documents for `kdivectl` against the gateway surface, unchanged by this issue and
recorded here only so the operator-token result is not mistaken for a profile change.

## Verdict

The capability survives the tool's removal. A real client with no prior knowledge of kdive reached
artifact text search from intent alone, invoked it correctly on the first attempt from the schema
the server handed it, and independently confirmed the old name is gone. The branch asymmetry that
keeps "could not search" distinguishable from "no such crash" holds live on both paths.
