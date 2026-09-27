# AI Software Mentor

> An agentic AI developer assistant for understanding, debugging, testing,
> and maintaining unfamiliar codebases.

---

## Problem

AI-assisted development makes building software fast — but the resulting codebase can
be hard to understand. When something breaks, developers may spend significant time:

- Finding the relevant files
- Understanding the architecture
- Tracing execution paths
- Understanding dependencies
- Investigating the bug
- Finding existing tests
- Writing regression tests
- Verifying the fix

This process is slow, error-prone, and especially painful in unfamiliar or
AI-generated codebases.

---

## Solution

**AI Software Mentor** replaces this manual process with an evidence-driven,
multi-step agentic workflow.

A developer reports:

> "Checkout is failing when a discount code is applied."

The system:

1. Explores the repository structure
2. Traces the relevant execution path
3. Collects evidence (observations, not guesses)
4. Synthesizes that evidence into a ranked root cause with a confidence breakdown,
   or states plainly that the evidence is insufficient
5. Proposes the minimal fix as a reviewable diff
6. Requires human approval before applying the fix
7. Generates a regression test
8. Executes the test and reports a verified result

Every finding is clearly labeled as an **OBSERVATION**, **HYPOTHESIS**,
**PROPOSED ACTION**, or **VERIFIED RESULT** — the system never treats a hypothesis
as a confirmed fact. Steps 1–5 are implemented (Phases 3–9); step 5 produces a
reviewable diff and stops at a human approval gate. Steps 6–8 arrive with Phases 10–11,
which run no tests yet and therefore report no verified result.

---

## Architecture

```
┌─────────────────────────────────────────┐
│           Frontend (Next.js)            │
│   TypeScript · React · Tailwind CSS     │
└──────────────────┬──────────────────────┘
                   │ REST + SSE
┌──────────────────▼──────────────────────┐
│           Backend (FastAPI)             │
│   Python · SQLite · Async               │
│                                         │
│  ┌─────────────────────────────────┐    │
│  │    Orchestrator (state machine) │    │
│  └──────┬──────────────────────────┘    │
│         │                               │
│  ┌──────▼──────────────────────────┐    │
│  │  Agents                         │    │
│  │  1. Repository Explorer         │    │
│  │  2. Architecture Agent          │    │
│  │  3. Bug Investigator            │    │
│  │  4. Fix Agent                   │    │
│  │  5. Test Agent                  │    │
│  │  6. Verification Agent          │    │
│  └─────────────────────────────────┘    │
│                                         │
│  AI Provider Layer (watsonx.ai / swap)  │
└─────────────────────────────────────────┘
```

See [`docs/architecture.md`](docs/architecture.md) for the full design.

---

## Technology Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 15, React 19, TypeScript, Tailwind CSS |
| Backend | Python 3.11+, FastAPI, SQLModel, SQLite |
| AI layer | IBM watsonx.ai (provider-abstracted interface) |
| Repo analysis | GitPython, Python `ast` module, tree-sitter |
| Streaming | Server-Sent Events (SSE) |
| Test execution | Sandboxed subprocess with timeout |

---

## Getting Started

### Prerequisites

- Node.js 20+
- Python 3.11+
- Git

### Start the backend

```bash
cd backend
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

The API will be available at `http://localhost:8000`.  
Health check: `GET http://localhost:8000/health`

### Try the mentor workflow

```bash
# 1. Start a run for a repository and a bug report
curl -X POST http://localhost:8000/api/mentor/runs \
  -H "Content-Type: application/json" \
  -d '{"repository_path": "../demo-repository/ShopDemo",
       "issue": "Checkout is failing when a discount code is applied."}'

# 2. Read the run state
curl http://localhost:8000/api/mentor/runs/<run_id>

# 3. Watch it work (Server-Sent Events)
curl -N "http://localhost:8000/api/mentor/runs/<run_id>/events"
```

The run advances deterministically through `CREATED → REPOSITORY_CONTEXT →
ARCHITECTURE_CONTEXT` (both backed by real, read-only analysis) and then stops at
`INVESTIGATION_PENDING`, because bug investigation is a later phase. It reports what
it actually observed and nothing more.

### Start the frontend

```bash
cd frontend
npm install
npm run dev
```

The UI will be available at `http://localhost:3000`.

---

## Project Structure

```
AI-Software-Mentor/
├── frontend/              Next.js application
├── backend/               FastAPI application
├── demo-repository/       Controlled demo e-commerce app (Phase 2)
├── docs/                  Architecture, development, and demo documentation
├── bob_sessions/          IBM Bob hackathon session evidence
├── scripts/               Developer utility scripts
├── AGENTS.md              Persistent AI agent instructions
├── README.md              This file
└── ai-software-mentor-plan.md  Full technical plan
```

---

## Workflow

The mentor workflow is a linear state machine. The server owns every transition.

```
CREATED
  → REPOSITORY_CONTEXT      repository structure, languages, test files
  → ARCHITECTURE_CONTEXT    modules, entry points, routes, data flows
  → INVESTIGATION_PENDING   bug investigation and hypothesis generation
  → EVIDENCE_PENDING        evidence normalisation, indexing, conflict detection
  → ROOT_CAUSE_ANALYSIS     candidate ranking, confidence scoring, evidence chain
  → ROOT_CAUSE_READY        root cause reported and traceable
  → FIX_PROPOSAL             minimal fix proposal and unified diff, in memory
  → FIX_PROPOSAL_READY      proposal waiting for human review — Phase 9 boundary
  → TEST_PLAN               coverage gaps and outlined regression tests — Phase 10
  → VERIFICATION_PENDING    sandboxed test execution and verification
  → COMPLETED
```

`FAILED` and `CANCELLED` are terminal escape hatches from any non-terminal state.
A run reaches `COMPLETED` only through an explicit server-side `complete_run()` after
real verification — never by reaching the end of the state list.

`FIX_PROPOSAL_READY` is where a run stops today, and that is deliberate: the human
approval gate is a real state in the machine, not a UI convention, so a run physically
cannot move past it on the server's own initiative. `TEST_PLAN` is implemented, but
the gate is in front of it, so no run reaches it without a human.

Each transition appends an event (`RUN_CREATED`, `STAGE_STARTED`, `STAGE_COMPLETED`,
`STAGE_PENDING`, `STATE_CHANGED`, `EVIDENCE_SYNTHESIS_COMPLETED`,
`ROOT_CAUSE_ANALYSIS_COMPLETED`, `FIX_PROPOSAL_COMPLETED`, `TEST_PLAN_COMPLETED`, …) to
an append-only log, which is what the SSE endpoint streams.

### Mentor API

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/mentor/runs` | Create a run and drive it to the first unimplemented stage |
| `GET` | `/api/mentor/runs/{run_id}` | Current state, status, evidence, context, event counts |
| `GET` | `/api/mentor/runs/{run_id}/investigation` | Phase 7 investigation view: evidence, counts, per-investigator reports |
| `GET` | `/api/mentor/runs/{run_id}/root-cause` | Phase 8 analysis: verdict, anchor, confidence breakdown, evidence chain |
| `GET` | `/api/mentor/runs/{run_id}/fix-proposal` | Phase 9 proposal: unified diff, risk, validation plan |
| `GET` | `/api/mentor/runs/{run_id}/test-plan` | Phase 10 plan: coverage gaps, outlined regression-test scenarios |
| `GET` | `/api/mentor/runs/{run_id}/events` | SSE stream of this run's events |

There is deliberately no apply or verify endpoint. `applied` and `tests_executed` are
hard-wired `false` in the fix-proposal response.

The request body accepts **only** `repository_path` and `issue`. Any attempt to supply
`current_state`, `status`, `run_id` or other workflow fields is rejected with `422` —
a client can never drive a transition.

---

## Evidence Synthesis and Root-Cause Analysis (Phase 8)

Phase 8 turns the unranked Phase 7 evidence into a stated root cause, or states
plainly that it cannot. It is deterministic, read-only, and executes nothing.

**Two steps, two states.** `EVIDENCE_PENDING` normalises and indexes the evidence
that no implemented phase can produce — `VERIFIED_RESULT` is refused and counted in
`rejected_types` so the exclusion is visible instead of silent. `ROOT_CAUSE_ANALYSIS`
then ranks candidates, scores confidence and builds a traceable chain.

**No model, no guessing.** The rules are static pattern matchers over evidence that
Phase 7 already read out of the source. `int(a / 100) * b` is reported as a
truncation-before-scaling defect; `int(a * b / 100)` is not. The same evidence always
produces the same verdict, and the analysis model deliberately contains no timestamp
so two runs compare equal.

**Honest non-conclusions are first-class.** The `status` field is the primary output,
not a footnote:

| Status | Meaning |
|---|---|
| `DETERMINED` | A root cause was identified and is anchored to a file and line |
| `PARTIAL` | Identified, but at least one investigator failed — the blind spot is named |
| `INSUFFICIENT_EVIDENCE` | The evidence does not support any candidate; nothing is claimed |
| `NO_EVIDENCE` / `FAILED` | Nothing to reason from, or the investigation produced none |

`root_cause` is `null` unless the status says otherwise, so a qualified and an
unqualified reading of the response can never drift apart.

**Confidence must be earned and explained.** The score is
`base(kind) + corroboration − penalties`, clamped to `[0, 1]`, and every term is
returned in `confidence_breakdown` alongside a prose `rationale`. Conflicting
evidence, an untested code path, a failed investigator, and a conclusion resting only
on inferences each apply a named penalty, and an incomplete or contradicted
investigation is capped below `HIGH` by construction. Bands: `HIGH ≥ 0.75`,
`MEDIUM ≥ 0.45`, otherwise `LOW`.

**Everything is traceable.** A conclusion cites the stored evidence IDs that support
it, and the `evidence_chain` walks `ROUTE → CALL → SITE → DATA_FLOW → COVERAGE` from
the entry point to the defect. `contributing_factors`, `conflicting_evidence` and
`unresolved_questions` are reported alongside it — contradictions are surfaced, never
silently resolved.

**Nothing was executed.** `tests_executed` is always `false` and
`verified_result_count` is always `0`, so a static reading of the source can never be
presented as a reproduced defect. Proving a fix is Phase 11's job.

---

## Fix Proposal and Reviewable Diff (Phase 9)

Phase 9 turns the Phase 8 root cause into a minimal, reviewable change. It is
deterministic, read-only, and applies nothing.

**The diff is built in memory.** For the demo scenario the tool produces exactly
this, against `app/services/discount_service.py:52`:

```diff
--- a/app/services/discount_service.py
+++ b/app/services/discount_service.py
@@ -49,7 +49,7 @@
-        return int(subtotal_cents / 100) * code.value
+        return int(subtotal_cents * code.value / 100)
```

The file on disk is unchanged afterwards. A run against ShopDemo leaves the tree
byte-for-byte identical (19 files, unchanged digest) with the bug still in place, which
is the whole point: a proposal is not a repair.

**Refusal is the default.** The rewriter handles one narrow shape — a single
quantising cast of a single division, scaled by one simple operand — and declines
everything else rather than approximating. A recognised defect it cannot rewrite
safely yields `INSUFFICIENT_EVIDENCE`; an anchor that is missing, stale, ambiguous,
outside the repository or in a file it will not read yields `INVALID`:

| Status | Meaning |
|---|---|
| `READY_FOR_REVIEW` | A concrete diff exists and is waiting for a human |
| `INSUFFICIENT_EVIDENCE` | Understood the problem, but not enough to change code safely |
| `INVALID` | The source the root cause points at could not be trusted to diff against |

`unified_diff` is `null` — never an empty string — whenever the status is not
`READY_FOR_REVIEW`, so "no proposal" can never render as a change that removes nothing.

**The file is re-read, and re-verified.** Phase 9 reads the live repository rather
than the Phase 7 snapshot, because a diff must be built against the code as it is
*now*, but it still requires the Phase 8 excerpt to match exactly one current line. A
file that changed after the investigation yields no diff rather than a stale one. If
the same excerpt appears more than once, no diff is offered: attributing a change to
one of two identical lines would be a guess.

**One reader, and nothing else.** `read_source_text()` in `app/analysis/fix_proposer.py`
is the only filesystem access in Phase 9. It reuses the Phase 3 secret-file and
path-safety rules and adds a size and a binary check. The proposal stores an
`AnchorLocation` — a file and line — rather than a quoted excerpt, so a refusal against
a secret path cannot echo the secret back to the client.

**Nothing is claimed.** `applied` and `tests_executed` are hard-wired `false` in the
API response, the validation plan is a set of instructions for a person rather than
results, and the risk assessment is a mechanical rating derived from the changed text
that names the specific signal it matched — so a reviewer can argue with a factor
rather than a score.

---

## Test Plan and Coverage Gaps (Phase 10)

Phase 10 answers a narrower question than the roadmap name suggests: *given the defect
Phase 8 found, what would a regression test have to do?* It produces a coverage-gap
report and outlines the tests — in prose. It writes no test file and runs no test.

**A plan, not test source.** The roadmap calls this "Test Generation". It deliberately
does not generate code, and that is a decision rather than a gap: generated test source
is a plausible artefact nobody has run, and handing it to a developer as "your
regression test" invites exactly the confusion this project exists to prevent. A plan
can be reviewed on its merits and then written by a person who runs it — the first point
at which anything can honestly be claimed.

**No repository access whatsoever.** Phase 9 was allowed one constrained file read, to
build a diff against the code as it is now. Phase 10 does not need one, so it has none:
every fact it uses is already on the run. Gaps come from Phase 7's `coverage_analysis`
hypotheses and the test-suite layout from its `import_graph` and `ast_test_inventory`
observations. Re-deriving any of it from the repository would create a second source of
truth that could silently disagree with the evidence the diagnosis rests on.

**Gaps are copied, never re-derived.** Each gap cites the Phase 7 item it came from and
inherits that hypothesis's confidence. Phase 10 does not re-score a gap and does not
promote a `HYPOTHESIS` to an `OBSERVATION` — so the coverage picture cannot quietly
become more certain just because it was restated. Gaps that no outlined scenario
addresses stay in the list and are named in `limitations`: an unclosed gap is an open
item, not a closed one.

**Every scenario says what would distinguish the defect.** Each one carries a
`discriminating_condition` — what the input must be for the test to tell the diagnosed
behaviour apart from correct behaviour. Without it, a regression test can pass against
the broken code forever, which is how real suites end up with a test that would never
have caught the bug. `expected_behavior` is the violated invariant, not a numeric
literal, because a number would be a guess about values nobody has run.

**Refusal is still a first-class answer:**

| Status | Meaning |
|---|---|
| `READY_FOR_REVIEW` | Scenarios are outlined and waiting for a developer to write and run them |
| `INSUFFICIENT_EVIDENCE` | No defect was determined, or the test suite is too poorly understood to aim a scenario at anything |
| `INVALID` | A root cause *was* determined but carries no usable anchor location — re-run the analysis |

A proposed new test file is an inference from the naming convention of the files Phase 7
observed, so it is marked `PROPOSED_NEW`, recorded in `assumptions`, and never created.

**Nothing is claimed.** `execution_status` is an enum with exactly one member,
`NOT_RUN` — the type cannot express a pass, so no later edit can make the plan claim one.
`tests_executed` and `test_source_written` are hard-wired `false` in the API response
from literals rather than from the model. Phase 10's only evidence type is
`PROPOSED_ACTION`; `VERIFIED_RESULT` is impossible.

**The approval gate is still in front of it.** `TEST_PLAN` is implemented but no run
reaches it, because passing `FIX_PROPOSAL_READY` requires a human who has looked at the
diff. The read endpoint therefore returns `409` for every run the system produces today;
a genuine negative returns `200` with no scenarios. There is no endpoint that runs,
writes or applies a test.

---

## Development Roadmap

| Phase | Description | Status |
|---|---|---|
| 1 | Project foundation | ✅ Complete |
| 2 | Controlled demo repository — ShopDemo | ✅ Complete |
| 3 | Repository ingestion | ✅ Complete |
| 4 | Repository understanding (AST) | ✅ Complete |
| 5 | Architecture Agent | ✅ Complete |
| 6 | Orchestrator + base agent framework | ✅ Complete |
| 7 | Bug Investigation agents | ✅ Complete |
| 8 | Evidence synthesis + root cause | ✅ Complete |
| 9 | Fix proposal + reviewable diff | ✅ Complete |
| 10 | Test plan + coverage gaps | ✅ Complete — plan only, no test source |
| 11 | Verification | 🔲 Planned |
| 12 | End-to-end agentic workflow | 🔲 Planned |
| 13 | UI / demo polish | 🔲 Planned |
| 14 | Impact measurement | 🔲 Planned |
| 15 | Bob evidence + final submission | 🔲 Planned |

> **Current scope.** Phases 1–10 are implemented, so a run advances through
> `FIX_PROPOSAL_READY` on its own and reports what Phases 3, 5, 7, 8, 9 and 10 actually
> observed — including, where they can justify one, a reviewable diff and a test plan.
> A run still stops at `FIX_PROPOSAL_READY`, because the human approval gate is a real
> state rather than a UI convention. Phase 11 remains a deliberate placeholder: it runs
> no tests and produces no `VERIFIED_RESULT`, so the system never claims work it did not
> do.

---

## IBM Bob 2.0 Hackathon

This project is a submission for the IBM Bob 2.0 Hackathon.

IBM Bob is used throughout the development process for:
- Architecture planning (Plan mode)
- Implementation (Agent mode)
- Parallel investigation (subagents)
- Code review and verification
- Session evidence collection (`bob_sessions/`)

---

## License

MIT
