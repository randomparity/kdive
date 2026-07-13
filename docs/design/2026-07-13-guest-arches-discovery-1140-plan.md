# Implementation plan — guest-arches discovery (#1140)

Spec: `docs/design/2026-07-13-guest-arches-discovery-1140.md` · ADR: `docs/adr/0338-guest-arches-discovery.md`

Branch: `feat/discover-guest-arches-1140` · Base: `main` · No migration, no schema change.

TDD throughout: write the failing test first, then the code. Commit per task with
a conventional message. Keep guardrails green at each commit:
`just lint` (ruff), `just type` (ty, whole tree), `just test`. Run `just ci` before
push. The change is unit-test-only (no `live_vm`); the acceptance criteria require
unit tests only.

## Real capabilities ground truth (for fixtures)

Verified live this session. Use these exact facts so fixtures match reality:

- **x86_64 host** (with `qemu-system-ppc` installed) advertises guest arches:
  `i686` (qemu+kvm), `ppc` (qemu), `ppc64` (qemu), `ppc64le` (qemu,
  `/usr/bin/qemu-system-ppc64`), `s390x` (qemu), `x86_64` (qemu+kvm,
  `/usr/bin/qemu-system-x86_64`). Host `<cpu><arch>` = `x86_64`.
- **ppc64le POWER10 host** advertises: `i686`, `ppc`, `ppc64`, `ppc64le`
  (qemu-only, `/usr/bin/qemu-system-ppc64le`), `x86_64` (qemu,
  `/usr/bin/qemu-system-x86_64`). Host `<cpu><arch>` = `ppc64le`. `/dev/kvm`
  present but **no** KVM domain for any arch → every arch resolves `tcg`.
- Each `<guest>` is `<os_type>hvm</os_type>` then `<arch name=X><wordsize/>
  <emulator/><machine .../>…<domain type='qemu'/>[<domain type='kvm'/>]</arch>`.

Fixtures are built from small hand-written XML mirroring this shape (the many
`<machine>` entries are irrelevant and omitted). The **KVM-HV ppc64le** case has no
real host, so it is synthetic: the real ppc64le guest block plus a
`<domain type='kvm'/>`.

## Task 1 — `SUPPORTED_ARCHES` on `arch_traits`

- **Where it fits:** the single source of truth for "arches kdive can provision",
  imported by discovery to filter `guest_arches`.
- **Code:** In `src/kdive/domain/platform/arch_traits.py`, add
  `SUPPORTED_ARCHES: frozenset[str] = frozenset(_TRAITS)` after `_TRAITS`. Add a
  one-line docstring/comment: "arches kdive can provision (one per `_TRAITS` row);
  discovery filters advertised guest arches to this set."
- **Test:** add to the existing `tests/domain/platform/test_arch_traits.py` —
  `SUPPORTED_ARCHES == frozenset(_TRAITS)` and `== {"x86_64", "ppc64le"}` (guards
  that a future arch addition updates both without a silent drift).
- **Acceptance:** `SUPPORTED_ARCHES` is a frozenset equal to the `_TRAITS` keys;
  importing it does not import anything new (no cycle).
- **Guardrails:** `just lint type`, then the new test.

## Task 2 — `parse_guest_arches` parser in `libvirt_xml`

- **Where it fits:** the shared XML contract helper that discovery calls; keeps XML
  parsing out of the provider and free of a `domain/` dependency.
- **Code:** In `src/kdive/providers/shared/libvirt_xml.py`:
  - `parse_guest_arches` returns the plain, JSON-shaped `dict[str, dict[str,
    str]]` — **no** named type and **no** import from `domain/` (the module stays
    domain-free). The named `GuestArch` `TypedDict` lives only on the read side
    (Task 3); a `dict[str, str]` with `accel`/`emulator` keys is structurally that
    type, so nothing needs to be shared across the layer. Import `Collection` from
    `collections.abc`.
  - `def parse_guest_arches(caps_xml: str, supported: Collection[str]) ->
    dict[str, dict[str, str]]:` per spec:
    1. `try: root = _safe_fromstring(caps_xml)` / `except (ET.ParseError,
       DefusedXmlException): return {}`.
    2. Iterate `root.findall("./guest")`; skip unless
       `guest.findtext("os_type") == "hvm"`.
    3. For each `guest.findall("arch")`: `name = arch.get("name")`; skip if `name
       not in supported`; `emulator = (arch.findtext("emulator") or "").strip()`;
       skip if falsy; `accel = "kvm" if arch.find("domain[@type='kvm']") is not
       None else "tcg"`.
    4. First occurrence of a `name` wins (guard: `if name in result: continue`).
  - Google-style docstring citing ADR-0338, matching the tone of
    `parse_capabilities_arch`. Note the arch-level-emulator simplification (no
    domain-nested override).
- **Test:** in `tests/providers/test_libvirt_xml.py`, add a small XML builder
  helper and cases (use `supported={"x86_64", "ppc64le"}` unless testing the filter
  itself):
  - x86_64-host six-arch fixture → `{"x86_64": {"accel": "kvm", "emulator":
    "/usr/bin/qemu-system-x86_64"}, "ppc64le": {"accel": "tcg", "emulator":
    "/usr/bin/qemu-system-ppc64"}}` (the four unsupported arches dropped).
  - x86_64 host without a ppc block → only `x86_64`.
  - real ppc64le-host fixture (no KVM domain) → both arches `tcg`, emulators
    `qemu-system-ppc64le` / `qemu-system-x86_64`.
  - synthetic KVM-HV ppc64le fixture (ppc64le arch has a `<domain type='kvm'/>`) →
    `ppc64le` accel `kvm`.
  - `os_type='xen'` (non-hvm) guest block containing a supported arch → dropped.
  - supported arch with no `<emulator>` element (and with an empty
    `<emulator></emulator>`) → dropped.
  - filter: `supported={"x86_64"}` over the six-arch fixture → only `x86_64`.
  - malformed XML (`"<capabilities"`) → `{}`; the XXE/defused-entity document
    (reuse the pattern from the existing
    `test_parse_capabilities_arch_returns_unknown_for_defused_xml_exception`) →
    `{}`.
  - duplicate `<arch name='x86_64'>` across two hvm guest blocks with different
    emulators → first wins.
- **Acceptance:** all cases pass; the function never raises on any input; no import
  of `domain/` added to `libvirt_xml`.
- **Guardrails:** `just lint type`, then the new tests.

## Task 3 — `GUEST_ARCHES_KEY` + reader on `ResourceCapabilities`

- **Where it fits:** the typed read side admission (issue 2) will consume; also
  keeps the key out of `extras()`.
- **Code:** In `src/kdive/domain/catalog/resource_capabilities.py`:
  - `GUEST_ARCHES_KEY = "guest_arches"`; add it to the `_KNOWN_KEYS` frozenset.
  - Define `GuestArch` `TypedDict` here (`accel: str`, `emulator: str`) — this is
    the read-side type and the only named definition. `parse_guest_arches` returns
    the structurally-identical plain dict (Task 2), so no cross-layer import exists
    in either direction.
  - `def guest_arches(self) -> dict[str, GuestArch]:` mirroring `pcie_descriptors`:
    read `self._values.get(GUEST_ARCHES_KEY)`; return `{}` unless it is a `dict`;
    for each `(arch, entry)` include it only when `entry` is a `dict` and
    `entry.get("accel")`/`entry.get("emulator")` are both `str`; build the returned
    `GuestArch` explicitly (`{"accel": entry["accel"], "emulator":
    entry["emulator"]}`), dropping any extra keys.
- **Test:** add to the existing `tests/domain/catalog/test_resource_capabilities.py`:
  absent key → `{}`; non-dict value → `{}`; an entry whose
  value is not a dict → dropped; an entry with a non-string `accel` or `emulator`
  → dropped; a well-formed two-arch mapping passes through with exactly
  `accel`/`emulator`; `GUEST_ARCHES_KEY` is in `_KNOWN_KEYS` so a capabilities
  mapping carrying it does not surface it in `extras()`.
- **Acceptance:** reader never raises on hostile/stale JSON; key excluded from
  `extras()`.
- **Guardrails:** `just lint type`, then the new tests.

## Task 4 — Wire discovery

- **Where it fits:** the one production call site that populates the capability.
- **Code:** In `src/kdive/providers/local_libvirt/discovery.py`:
  - Import `parse_guest_arches` from `kdive.providers.shared.libvirt_xml` (extend
    the existing import block) and `SUPPORTED_ARCHES` from
    `kdive.domain.platform.arch_traits`, and `GUEST_ARCHES_KEY` from
    `kdive.domain.catalog.resource_capabilities`.
  - In `list_resources`, capture `caps_xml = conn.getCapabilities()` once, feed it
    to both `parse_capabilities_arch(caps_xml)` (existing `arch` key) and
    `parse_guest_arches(caps_xml, SUPPORTED_ARCHES)` (new `GUEST_ARCHES_KEY` entry).
    Calling `getCapabilities()` once (instead of twice) is a minor cleanup that
    keeps a single source; acceptable and preferred.
- **Test:** in `tests/providers/local_libvirt/test_discovery.py`:
  - Add a **new** named caps fixture constant (e.g. `_CAPS_XML_WITH_GUESTS`) with an
    x86_64 `<guest>` block, and a test that constructs
    `FakeLibvirtConn(caps_xml=_CAPS_XML_WITH_GUESTS)` and asserts
    `caps[GUEST_ARCHES_KEY]` contains the expected arch(es). Do **not** change the
    shared `FakeLibvirtConn` default `caps_xml` in `fakes.py` — that fake is used by
    discovery *and* MCP tests, and a new default key would ripple into any test that
    asserts on the full capabilities mapping.
  - Keep a case for the arch-only default caps → `guest_arches == {}` (degradation),
    and do **not** weaken the existing
    `test_list_resources_advertises_host_capabilities` — extend it or add a sibling.
- **Acceptance:** discovery advertises `guest_arches` derived from the connection's
  caps XML; a caps XML with no guest blocks yields `{}` and does not break the other
  advertised keys. `guest_arches` reaches the persisted row via the **same
  capability-agnostic inventory writeback** that already carries `pcie_devices`,
  `vcpus`, and `disk_gb` verbatim. Verified: `providers/core/resource_registration.py`
  writes `Jsonb(resource.capabilities)` as a **whole blob** on both INSERT and
  UPDATE (no per-key allowlist), and `inventory/reconcile/resources.py`
  `_overlay_one_local` preserves `**row.capabilities` (merging only the allocation
  cap). So no new end-to-end test is required — the writeback does not filter
  capability keys.
- **Guardrails:** `just lint type`, then `tests/providers/local_libvirt/`.

## Task 5 — Full guardrails + PR

- Run `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, docs
  guards, test) — all green.
- No migration, no generated-doc regen expected (no MCP tool/schema/env change).
  Confirm `just adr-status-check docs-check docs-links docs-paths` still pass
  (already do; the ADR/README landed in the design commits).
- Open the PR referencing #1140 and ADR-0338; note it is issue 1 of epic #1139,
  unit-test-only, and that `guest_arches` is an additive capability with no consumer
  until issue 2 — so it is inert-but-correct on merge.

## Risk / review notes

- **Inert additive key.** Between this PR and issue 2, `guest_arches` is written but
  unread. Confirm nothing iterates raw capabilities in a way that a new key breaks
  (e.g. a strict serializer). `_KNOWN_KEYS` membership keeps it out of `extras()`.
- **Layering.** `libvirt_xml` must not import `domain/`; the `supported` set is
  injected and `parse_guest_arches` returns a plain `dict[str, dict[str, str]]`.
  The `GuestArch` `TypedDict` is defined only on the read side
  (`resource_capabilities`), so no cross-layer type import exists.
- **Degradation.** Malformed caps XML → `{}`, never a discovery crash — same
  contract as `parse_capabilities_arch`. Covered by tests.
- **Accel correctness.** `accel=kvm` is driven solely by a `<domain type='kvm'>`
  advertisement (ADR-0338); the real POWER10 host (KVM present, no KVM domain →
  `tcg`) is the regression anchor.
