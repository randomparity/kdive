---
title: An assertion whose two operands trace to a single source cannot fail, whatever it is named
date: 2026-09-04
tags: [vacuous-test, false-evidence, test-design, fault-injection, review-heuristic]
components: [scripts/mutate.py, docs/solutions/2026-09-04-bite-harness-verdicts-were-never-gated.md]
---

## Problem

A single change carried three green tests that could not fail. Each was named for a real property,
each read as coverage in review, and none of them constrained the implementation at all. Two
survived three rounds of design review and were caught only by fault injection; one survived to
branch review and was caught by an adversarial reader.

The most expensive instance guarded a production wiring decision:

```python
assert factory._readiness is _real_readiness
```

That is the entire test. It holds for *any* callable bound to that slot, so it certifies the
binding exists and says nothing about whether the bound thing is correct. The probe it blessed was
in fact wrong — on the only reachable branch it reported success for a kernel that had panicked —
and the test stayed green throughout.

A second instance defeated the exact property it was written to pin. A fixture returned **the same
`Path` object** for every `config.require` call, and the test then asserted identity between two
resolved roots:

```python
assert cleanup.__self__._root is mechanisms.recovery_root
```

Because the fixture handed out one object, that identity held whether the builder resolved the
setting once or twice — which is precisely the distinction the test existed to detect.

A third, rejected during design: a check comparing an observed value against the variable that
produced it.

## Root cause

> **An assertion whose two operands trace to a single source cannot fail, whatever it is named.**

There is no input to the system under test that makes such an assertion false. The two sides are
equal by construction — the same object, or the same value flowed through two names — so the
assertion is a tautology dressed in a domain-specific test name. The name is what makes it survive
review: readers check whether the *stated property* matters, not whether the *written comparison*
could ever come out false.

Strengthening the wording never fixes it. The fix is always to make the operands independent.

### The boundary — shared source is not automatically the defect

Two authors independently hit apparent instances that turned out to be legitimate, and the
distinction they arrived at is the useful half of the rule.

**Legitimate: a needle that is deliberately the injected value.** A leak test asserted that a
rendered payload does not contain the socket path a fixture had injected. Needle and fixture share
one constant — and they *should*. If the needle were some other string, the test would assert the
absence of something never present, which is weaker, not stronger. The operands here are a
**transformed value versus a literal**: the payload went through the renderer under test.

**Legitimate: two values computed by different code from one input.** A test compared a set of
object references against an expected set built from the same activation record. Shared input,
but the two sides are produced by different implementations, and a fault proving it goes red when
the implementation drops a reference settles it.

So the rule is not "the operands share a source". It is:

> The assertion must contain a step the implementation performs. If both operands reach the
> comparison without passing through the code under test, nothing about that code can make them
> differ.

### Sibling class: the shadowed guard

A related shape, worth recognising because the fix is different. **A test that cannot distinguish
the guard under test from a different guard shadowing it.** Two instances:

- A database permission test asserted a refusal message from an in-body role check — but `EXECUTE`
  on that function was granted to one role only, so the role under test was refused by the
  **grant** and never entered the body. The in-body guard was untested and could have been deleted
  without turning anything red.
- The same in-body check uses `pg_has_role`, which is **true for a superuser against every role**.
  A role-permission test run as superuser is therefore vacuous by construction, and looks like
  thorough coverage.

The remedy there is a purpose-built principal that reaches exactly the guard under test, plus a
success arm — without one, every denial is equally consistent with a call that is broken for
everybody.

## Solution

For each of the three original instances:

- Assert identity **and then invoke the thing**, requiring the specific error. Binding plus
  behaviour, not binding alone.
- Have the fixture hand out a **fresh, equal object per call**, matching what the real accessor
  does. The identity assertion then means what it says.
- Reject the comparison and derive the expected value from a different place than the observed one.

## Prevention

**Fault injection is the detector, and review is not.** All three instances passed human review.
Each was found by committing the fix, breaking the implementation, and observing that the test
stayed green. See `docs/solutions/2026-09-04-bite-harness-verdicts-were-never-gated.md` for the
harness contract that makes those results trustworthy — a fault-injection run is only evidence if
the harness itself is gated.

The cheap review heuristic, applicable while reading a diff:

1. Find the two operands of the assertion.
2. Trace each back to where its value came from.
3. If both arrive without passing through the code under test, the test cannot fail — regardless of
   how well-named it is.

Two questions that catch most of it:

- *What wrong implementation would still pass this test?* If the answer is "any", it is a wiring
  assertion.
- *Is this fixture handing out one shared object where the real collaborator returns a new one each
  call?* Shared fixture objects silently convert identity assertions into tautologies.
