# Demo Repository

**Status: Planned (Phase 2)**

This directory will contain the controlled demonstration e-commerce application
used for the AI Software Mentor hackathon demo.

## Planned Contents

The demo application will be a small but realistic e-commerce system containing:

- A product catalog
- A shopping cart
- Discount code handling
- A checkout flow
- Order processing
- A test suite

## Demo Scenario

The primary demonstration scenario is:

> "Checkout is failing when a discount code is applied."

The repository will contain an intentionally reproducible bug in the discount +
checkout interaction path. The bug is:

- **Deterministic** — same inputs always reproduce the same failure
- **Traceable** — the failure path passes through multiple modules
- **Fixable** — a minimal targeted change resolves it
- **Testable** — a regression test covering the specific scenario catches it

The existing test suite will contain tests for discount handling and tests for
checkout individually, but will be missing the combined scenario test — which is
precisely the coverage gap the AI Software Mentor's Test Agent will identify and fill.

## Note

The exact bug implementation is intentionally not described in public documentation.
It will be revealed as part of the demo investigation workflow.

## Implementation

This directory will be built in **Phase 2** of the development roadmap.
