# Internal docstring cleanup design

## Goal

Make internal documentation proportional to the code it explains. Keep concise contracts,
non-obvious invariants, and failure semantics near implementation; leave historical scenarios and
proofs in their cited ADRs. Correct stale claims found while consolidating the prose.

## Scope

The desloppify finding names four production modules. The same repeated offline-drgn narrative also
exists in the remote-libvirt sibling, so the pattern-wide fix covers:

- `artifacts/upload_manifest.py`;
- `artifacts/read_model.py`;
- `jobs/queue.py` and its stale fallback comment in `mcp/tools/lifecycle/systems/view.py`;
- `providers/local_libvirt/debug/introspect.py`; and
- `providers/remote_libvirt/debug/introspect.py`.

None of these docstrings is a decorated FastMCP wrapper or an input to schema/document generation.
Agent-facing MCP wrapper documentation is explicitly out of scope and must remain byte-for-byte
unchanged.

## Editing rules

Internal functions and data carriers should normally have one contract sentence or paragraph.
Retain detail only when the name, signature, types, and adjacent code do not already express it.
Keep these non-obvious contracts:

- upload deadline refresh is monotonic, uses the Postgres transaction clock, is capped from mint,
  and distinguishes a missing/expired row from a spent cap;
- equality at an upload deadline remains open;
- lowered TTL configuration may leave an already-later deadline to expire naturally;
- failure attribution selects the newest terminal System-lifecycle job before accepting only a
  failure, so newer success/cancellation masks stale failures;
- raw artifact lookups enforce ownership or sibling exclusion not evident from their names;
- reusable artifact refs require immutable version pins;
- offline drgn verifies provenance before fetching debuginfo, stages temporary inputs, delegates
  redaction and byte capping to the shared assembler, and preserves its error taxonomy; and
- production drgn imports lazily, so absence is reported on first use rather than composition.

Remove repeated examples, implementation histories, test strategy, hypothetical failure stories,
obvious `Args`/`Returns` restatements, and prose already represented by SQL or field names. Replace
standalone string expressions used as comments with real comments or delete them.

Correct two stale statements while editing:

- System rows now normally record their own failure category; job lookup is a fallback for NULL
  paths, and the matching payload expression is indexed.
- raw vmcores are addressed directly through the Run, not indirectly through its System.

Do not add a docstring-length test. An arbitrary numeric limit would reward shorter text without
protecting meaning. Existing behavior tests, lint, type checking, review, and the subsequent
desloppify assessment provide the appropriate proof for this prose-only change.

## Work split

The artifact/job pass owns upload-manifest, artifact read-model, queue, and the systems-view
fallback comment. It may trim other clearly repetitive internal docstrings in those files when the
same contract is preserved, but it must not remove functions or change SQL.

The introspection pass owns local and remote offline-drgn module/class/factory/operation docstrings.
Detailed operation and error semantics live on `from_vmcore`; lazy dependency semantics live on
`from_env`; module and class docstrings state ownership only. Live remote introspection text is left
alone unless needed to separate it cleanly from the offline narrative.

## Verification

Each pass runs focused behavior tests for the modules it documents, then repository lint and type
checks. Independent review compares the old and new prose against implementation and ADRs to catch
dropped invariants or stale claims. The integrated branch runs full CI before the desloppify issue
is resolved.
