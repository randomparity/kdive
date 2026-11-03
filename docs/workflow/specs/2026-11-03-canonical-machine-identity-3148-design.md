# Local authority identity tolerates libvirt's versioned machine name (#3148)

## Problem

libvirt rewrites the machine alias kdive renders (`q35`, `pseries`) to a versioned name when it
defines the domain (`pc-q35-10.2`, `pseries-9.0`). `owned_system_semantic_identity`
(`providers/local_libvirt/lifecycle/boot/session.py`) hashes `os/type` attributes raw, so the
stored domain never matches the intent's `xml_digest`; `domain_owned` is false, the readiness
probe never runs and every generation ends `retained-quarantine`. Test fakes never canonicalize
the machine, so no test caught it.

## Design

Choice (criterion 5): map the stored machine back to kdive's alias inside the identity. Rendering
the canonical name instead would need a libvirt capabilities query per provision, would put a
host-specific version into the rendered intent, and would change the digest of every stored
intent. Mapping leaves the digest of an alias-rendered domain unchanged, so persisted intents
still match and `schema` stays `kdive-local-authority-domain-semantics-v2`.

- `ArchTraits` (`domain/platform/arch_traits.py`) gains `machine_family`: `pc-q35` for x86_64,
  `pseries` for ppc64le.
- New `rendered_machine_alias(arch, machine) -> str` there: returns `traits.machine` when
  `machine` equals the alias or starts with `machine_family + "-"`; otherwise returns `machine`
  unchanged. An arch with no traits row returns `machine` unchanged.
- The identity projects `os/type` as `(text, attrib)` with `attrib["machine"]` passed through
  `rendered_machine_alias(attrib.get("arch"), ...)`.

A different machine type (`pc-i440fx-10.2`, `pseries`'s sibling families, a different arch) is
not collapsed, so it still changes the identity. A versioned machine kdive itself pinned would
collapse to the alias; kdive renders aliases only, so this is accepted below.

## Success

1. The identity of a `q35` domain equals that of the same domain with `pc-q35-10.2` (and
   `pc-q35-rhel9.4.0`); likewise `pseries` and `pseries-9.0`.
2. `pc-i440fx-10.2` on x86_64 and `pc-q35-10.2` on ppc64le differ from the rendered identity.
3. The new identity test fails on main.
4. Live: one authority-owned local System provision reaches `ready` and release plus teardown
   complete on an x86_64 KVM host (q35) and under TCG on ppc64le (pseries). Native POWER9 KVM-HV
   is an open item for the operator.

## Validation

- Machine alias mapping: focused-test `tests/domain/platform/test_arch_traits.py`, red
  `AttributeError`/`ImportError`, green `just test-verbose tests/domain/platform/test_arch_traits.py`.
- Identity tolerance and sensitivity: focused-test in
  `tests/providers/local_libvirt/lifecycle/boot/test_session.py`, red on identity inequality,
  green `just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_session.py`.
- Live proof: task-test-not-applicable, the real libvirt rewrite is the contract and only a
  live host produces it; the unit tests encode the observed rewrite.

## Failure model

- Actors and deployments: the local authority provider on a worker host, x86_64 and ppc64le
  guests, KVM and TCG.
- Invariants at stake: ownership fence (a foreign domain must not be adopted as owned); stored
  intent digests remain valid.
- Accepted: a kdive-pinned versioned machine is indistinguishable from the alias (kdive renders
  aliases only); an unknown libvirt machine family on these arches is not collapsed and fails
  closed as not owned.
- Covered elsewhere: remote authority machine handling (`remote_libvirt/system_authority.py`
  keeps a concrete machine in its intent); lapsed job recovery #3143.
