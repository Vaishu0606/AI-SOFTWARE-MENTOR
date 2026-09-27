# Demo Scenario

## Overview

The primary demonstration scenario for the AI Software Mentor is:

> A developer reports: **"Checkout is failing when a discount code is applied."**

This scenario is chosen because it:

- Requires tracing execution across multiple modules (not a single-file bug)
- Has a plausible root cause that is non-obvious without tracing
- Has an existing partial test suite that misses the combined scenario
- Is reproducible deterministically from specific inputs
- Can be fixed with a minimal, targeted change
- Produces a clearly verifiable result

---

## Demo Flow

The demonstration walks through the full agentic investigation workflow:

```
Bug report submitted
        │
        ▼
Repository Explorer
  → Identifies: checkout route, discount service, cart service
  → Identifies: existing test files
  → Evidence type: OBSERVATION

        │
        ▼
Architecture Agent
  → Traces: POST /checkout → checkout_service → discount_service → cart total
  → Produces: call graph for the relevant path
  → Evidence type: OBSERVATION

        │
        ▼
Bug Investigator (3 parallel branches)
  → Branch A: Traces discount calculation logic
  → Branch B: Traces cart total calculation
  → Branch C: Inspects input validation
  → Generates: ranked hypotheses with supporting code evidence
  → Evidence type: OBSERVATION + HYPOTHESIS

        │
        ▼
Hypothesis Review (UI)
  → Developer reviews ranked hypotheses
  → System proceeds with highest-confidence hypothesis

        │
        ▼
Fix Agent
  → Proposes: minimal unified diff
  → Explains: why this addresses the root cause
  → Evidence type: PROPOSED_ACTION

        │
        ▼
Human Approval Gate (UI)
  → Developer reviews the diff
  → Developer clicks "Approve"

        │
        ▼
Test Agent
  → Identifies: coverage gap in existing test suite
  → Outlines: regression scenarios for the diagnosed behaviour
  → Writes: no test source — a plan for a human to review
  → Evidence type: PROPOSED_ACTION

        │
        ▼
Verification Agent
  → Executes: regression test in sandboxed environment
  → Captures: stdout, stderr, exit code
  → Reports: PASS with actual execution evidence
  → Evidence type: VERIFIED_RESULT

        │
        ▼
Final Summary
  → All evidence items displayed with type badges
  → Root cause reported
  → Fix proposed as a reviewable diff — NOT applied
  → Test plan: outlined, not written (Phase 10)
```

> **Where the demo stops today (Phases 9–10 implemented, gate not open).** The run
> walks the real stages through `FIX_PROPOSAL` and parks at `FIX_PROPOSAL_READY`, the
> human approval gate. For this scenario it produces exactly this diff, and the file on
> disk is unchanged afterwards:
>
> ```diff
> --- a/app/services/discount_service.py
> +++ b/app/services/discount_service.py
> @@ -49,7 +49,7 @@
> -        return int(subtotal_cents / 100) * code.value
> +        return int(subtotal_cents * code.value / 100)
> ```
>
> The `TEST_PLAN` stage behind that gate is implemented and deterministic: given an
> approved run it copies the Phase 7 coverage gaps and outlines the regression scenarios
> that would close them. It is still unreachable in a normal run, because nothing
> approves a fix on the server's own initiative — so
> `GET /api/mentor/runs/{run_id}/test-plan` returns `409` for every run the system
> produces today. When the stage does run it deliberately writes **no test source**: a
> plausible test nobody has executed is exactly the artefact this project must not hand
> over as "your regression test". Only sandboxed verification remains a placeholder, so
> the demo reports `tests_executed: false` and produces no `VERIFIED_RESULT` rather than
> claiming a fix it did not try.

---

## Demo Repository

The demo repository (`demo-repository/`) is a controlled application called **ShopDemo**.

### Application Components

| Component | Technology | Purpose |
|---|---|---|
| Backend API | Python / FastAPI | Products, cart, discount, checkout |
| Frontend | Next.js | Product listing, cart, checkout pages |
| Test suite | pytest | Unit and integration tests |

### Key Modules

- `cart_service` — manages cart items and totals
- `discount_service` — applies discount codes (percent or fixed amount)
- `checkout_service` — validates cart and processes the order
- `discount route` — `POST /cart/{id}/discount`
- `checkout route` — `POST /checkout`

### Intentional Bug

The bug is located in the interaction between the discount application and the
checkout validation logic. It is:

- Deterministic: the same discount code + cart value always triggers the failure
- Traceable: the failure path crosses at least two service modules
- Fixable: a single targeted change resolves it
- Testable: a regression unit test covering the combined scenario catches it

**The exact bug implementation is intentionally withheld from public documentation.**
It is revealed during the live demo investigation by the Bug Investigator agent.

### Existing Test Suite

The test suite that ships with the demo repository:

- `test_cart.py` — covers basic cart operations and totals
- `test_discount.py` — covers discount code validation and application in isolation
- `test_checkout.py` — covers the checkout flow without discount codes

**The combined discount + checkout scenario is intentionally absent** — this is the
coverage gap the Test Agent identifies and fills.

---

## Demo Setup (Phase 2, when ready)

```bash
cd demo-repository/backend
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate
pip install -r requirements.txt
pytest tests/ -v            # existing tests pass; bug scenario not yet tested
```

To reproduce the bug manually (after Phase 2 is complete):

```bash
# Start the demo backend
uvicorn app.main:app --reload --port 8001

# Apply a discount code and attempt checkout
# (specific reproduction steps documented in Phase 2)
```

---

## Impact Measurement

After the demo is complete, we will record:

| Metric | Manual | Automated |
|---|---|---|
| Time to identify root cause | (measured) | (measured) |
| Files opened | (counted) | (counted from agent log) |
| Investigation steps | (counted) | (counted from evidence store) |
| Time to write regression test | (measured) | (measured) |
| Verification time | (measured) | (measured) |

**All values will be actual measurements — no invented percentages.**
