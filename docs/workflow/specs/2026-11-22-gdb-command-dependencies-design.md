# GDB command-family dependencies

## Problem

Host protocols duplicate private methods implemented by command mixins. Watchpoints
also acquire target resolution through a sibling mixin rather than an explicit dependency.

## Scope

Keep eight command mixins and existing modules. Move private implementations to module
functions: modules' thirteen helpers, stack's three, disassembly's four, watchpoints'
three, breakpoint_ref/ref_from and evaluate_symbol. These last three have the same
verified family-local self-contract duplication and no external consumers.
Pure classifiers/parsers drop self; others take host with their existing Host protocol.
Preserve argument names, keyword-only markers and returns. Replace helper dispatch with
direct function calls. Host protocols retain only execute_mi_command, redactor and their
actual memory/path/resolver/thread collaborators; remove helper declarations.

Move disassembly's target resolver to existing symbols.py, taking attachment and
Callable[[GdbMiAttachment, str], int], followed by the current symbol/address keywords.
Disassembly and watchpoints import it directly and supply their bound resolve_symbol.
The engine imports stack's pure frame converter directly for stop_record_from.
Retain public signatures, eight mixin bases, validation precedence, MI bytes, unsigned
64-bit/zero targets, window shrinking, redaction, module traversal/identity/cache and
watchpoint resume classification. Add no module, adapter, base or configuration.

### Failure model

- Actors: the existing GDB engine supplies typed collaborators to each command family.
- Invariants: public methods require real engine collaborators, never family internals;
  bounds, commands, results, error precedence and redaction preserve current algorithms.
- Accepted: private helper lookup/stack frames change; direct private tests migrate.
- Covered elsewhere: existing controller, transport, concurrency, transcript and staging
  policies remain their owners; no new calls or policy changes are introduced.

## Success

1. Six affected family hosts declare only supplied engine collaborators, with helpers
   owned at module scope; public commands work on a host without inherited helpers.
2. Target resolution has explicit callback inputs and two direct family users.
3. Public/helper algorithm ASTs remain exact after mechanical dependency substitutions;
   engine stop conversion and the eight-mixin composition retain their behavior.

## Validation

- Mode: focused-test: add bare-host public-command cases for six affected families;
  current calls must fail on missing private helpers. Script actual MI responses and
  remove target/module-walk stubs; preserve full command/error assertions.
- Mode: task-test-not-applicable: no new algorithm; compare moved bodies, public signatures,
  command literals and untouched engine methods after dependency rebinding.
- Run focused shared command and local engine suites, including stop records, target
  boundaries, redaction, module identity/stale cache, memory transport failures, unreadable
  windows, thread selection and watchpoint resume. Controlled dependency, target and
  redaction faults must bite. Run lint, whole-tree types, documentation and hooks.
  Exercise a real local GDB path if installed; claim native coverage only for arms run.
