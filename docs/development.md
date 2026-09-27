# Development Guide

## Prerequisites

- **Node.js** 20 or later (for frontend)
- **Python** 3.11 or later (for backend)
- **Git**
- A terminal (PowerShell on Windows, bash/zsh on macOS/Linux)

---

## Quick Start

### 1. Clone the repository

```bash
git clone <repository-url>
cd AI-Software-Mentor
```

### 2. Start the backend

```bash
cd backend
# Create and activate virtual environment
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate
# Install dependencies
pip install -r requirements.txt
# Copy environment template and fill in your values
cp .env.example .env
# Start the development server
uvicorn app.main:app --reload --port 8000
```

Backend is available at: `http://localhost:8000`  
Interactive API docs: `http://localhost:8000/docs`  
Health check: `GET http://localhost:8000/health`

### 3. Start the frontend

```bash
cd frontend
# Install dependencies
npm install
# Start the development server
npm run dev
```

Frontend is available at: `http://localhost:3000`

---

## Backend Structure

```
backend/
├── app/
│   ├── __init__.py
│   ├── main.py              FastAPI entry point and router registration
│   ├── api/                 Route handlers
│   │   ├── __init__.py
│   │   ├── health.py        GET /health
│   │   ├── repositories.py  POST /api/repositories/explore (Phase 3)
│   │   ├── analysis.py      POST /api/repositories/analyze (Phase 4)
│   │   ├── arch.py          POST /api/repositories/architecture (Phase 5)
│   │   ├── mentor.py        POST/GET /api/mentor/runs (Phase 6, 7, 8)
│   │   └── stream.py        GET /api/mentor/runs/{id}/events — SSE (Phase 6)
│   ├── agents/              AI agents
│   │   ├── __init__.py
│   │   ├── architecture.py  Architecture Agent — static analysis (Phase 5)
│   │   ├── base_agent.py    Abstract agent contract (Phase 6)
│   │   └── providers/       AI provider implementations
│   │       ├── __init__.py
│   │       ├── protocol.py  AIProvider protocol (abstract interface)
│   │       └── mock.py      Mock provider for testing
│   ├── analysis/            Deterministic code analysis
│   │   ├── __init__.py
│   │   ├── ast_analyzer.py  Functions, classes, routes, imports (Phase 4)
│   │   ├── evidence_synthesizer.py  Evidence indexing, root-cause rules (Phase 8)
│   │   ├── fix_proposer.py  Constrained reader, rewriter, diff, risk (Phase 9)
│   │   └── test_planner.py  Coverage-gap reader, scenario outlines (Phase 10)
│   ├── core/                Shared utilities
│   │   ├── __init__.py
│   │   ├── config.py        Settings (loaded from .env)
│   │   ├── db.py            SQLite engine + session factory (Phase 6)
│   │   └── logging.py       Structured logging setup
│   ├── ingestion/           Repository ingestion layer (Phase 3)
│   │   ├── __init__.py
│   │   ├── cloner.py        LocalSource validation + future remote clone
│   │   └── explorer.py      File tree walk, language detect, security guards
│   ├── investigation/       Parallel bug investigation (Phase 7)
│   │   ├── __init__.py
│   │   ├── base.py          BaseInvestigator contract
│   │   ├── context.py       Read-only repository snapshot
│   │   ├── evidence.py      Evidence policy for investigators
│   │   ├── code.py          Code Investigator
│   │   ├── tests.py         Test Investigator
│   │   ├── dependencies.py  Dependency Investigator
│   │   ├── relevance.py     Issue-term extraction and file ranking
│   │   └── runner.py        Threaded runner and result assembly
│   ├── models/              Domain types and SQLModel schemas
│   │   ├── __init__.py
│   │   ├── domain.py        Core domain types (Evidence, Hypothesis, …)
│   │   ├── investigation.py InvestigationResult and reports (Phase 7)
│   │   ├── root_cause.py    RootCauseAnalysis and synthesis models (Phase 8)
│   │   ├── fix_proposal.py FixProposal, risk, validation plan (Phase 9)
│   │   ├── test_plan.py    TestPlan, coverage gaps, scenarios (Phase 10)
│   │   ├── workflow.py      Workflow states, statuses, MentorRun (Phase 6)
│   │   └── schemas.py       SQLModel tables: runs + events (Phase 6)
│   ├── repositories/        Repository ingestion and analysis (Phase 3+)
│   │   └── __init__.py
│   └── services/            Orchestrator and workflow logic (Phase 6)
│       ├── __init__.py
│       ├── state_machine.py Transition table, terminal states (Phase 6)
│       ├── stages.py        Stage handlers + placeholders (Phase 6)
│       ├── orchestrator.py  Run lifecycle and advancement (Phase 6)
│       ├── investigation_stage.py  Phase 7 stage and shared loader
│       ├── root_cause_stage.py     Phase 8 synthesis + analysis stages
│       ├── fix_proposal_stage.py   Phase 9 hand-off gate + proposal stage
│       ├── test_plan_stage.py      Phase 10 plan stage and loader
│       ├── run_store.py     Run persistence (Phase 6)
│       └── event_store.py   Append-only event log (Phase 6)
└── tests/
    ├── conftest.py            Per-test SQLite isolation + sample_repo fixture
    ├── test_domain.py         Domain model tests
    ├── test_health.py         Health endpoint tests
    ├── test_providers.py      AI provider tests
    ├── test_ingestion_*.py    Phase 3 tests
    ├── test_ast_*.py          Phase 4 tests
    ├── test_architecture_agent.py, test_arch_*.py   Phase 5 tests
    ├── test_workflow_state_machine.py                Phase 6 state machine
    ├── test_workflow_events.py                        Phase 6 event store
    ├── test_orchestrator.py                           Phase 6 orchestrator
    ├── test_base_agent.py                             Phase 6 agent contract
    ├── test_mentor_api.py                             Phase 6 REST API
    ├── test_mentor_sse.py                             Phase 6 SSE endpoint
    ├── test_investigation.py                          Phase 7 investigators
    ├── test_evidence_synthesizer.py                   Phase 8 synthesis rules
    ├── test_root_cause_stage.py                       Phase 8 stages and events
    ├── test_fix_proposer.py                           Phase 9 rewriter and diff
    ├── test_fix_proposal_stage.py                     Phase 9 stages and security
    ├── test_fix_proposal_api.py                       Phase 9 read endpoint
    ├── test_test_planner.py                           Phase 10 gap reader + planner
    ├── test_test_plan_stage.py                         Phase 10 stage and security
    ├── test_test_plan_api.py                           Phase 10 read endpoint
    └── test_phase6_security.py                        Security controls (6, 8)
```

---

## Frontend Structure

```
frontend/
├── src/
│   ├── app/
│   │   ├── layout.tsx       Root layout
│   │   └── page.tsx         Landing page
│   ├── components/          Reusable UI components (Phase 13+)
│   ├── hooks/               Custom React hooks (Phase 13+)
│   └── lib/                 API client + shared types (Phase 13+)
├── public/
├── package.json
├── tsconfig.json
└── tailwind.config.ts
```

---

## Running Tests

### Backend tests

```bash
cd backend
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pytest tests/ -v
```

`tests/conftest.py` points every test at a throwaway SQLite file and deletes it
afterwards, so tests never share workflow state and never touch
`mentor_workflow.db`.

### Frontend type check

```bash
cd frontend
npm run type-check   # runs tsc --noEmit
```

### Frontend build validation

```bash
cd frontend
npm run build
```

---

## Environment Variables

Copy `backend/.env.example` to `backend/.env` and fill in the required values.

```
# AI Provider
AI_PROVIDER=mock                  # "mock" for development, "watsonx" for production
WATSONX_API_KEY=                  # IBM watsonx.ai API key (required in production)
WATSONX_PROJECT_ID=               # IBM watsonx.ai project ID
WATSONX_URL=                      # IBM watsonx.ai endpoint URL
# Application
APP_ENV=development               # development | production
SESSION_STORAGE_PATH=./sessions   # where cloned repos are stored
LOG_LEVEL=INFO
# Mentor workflow (Phase 6) — all optional
DATABASE_PATH=./mentor_workflow.db             # workflow run + event store
SSE_POLL_INTERVAL_SECONDS=0.2                  # event store poll interval
SSE_HEARTBEAT_SECONDS=15.0                     # keep-alive comment interval
SSE_TIMEOUT_SECONDS=60.0                       # default stream lifetime
```

**Never commit `.env` to version control.** It is in `.gitignore`. These are the
field names in `app/core/config.py`; `Settings` sets no `env_prefix`, so a `MENTOR_`
prefix on any of them would be silently ignored.

---

## The Mentor Workflow (Phases 6–10)

### States

```
CREATED → REPOSITORY_CONTEXT → ARCHITECTURE_CONTEXT → INVESTIGATION_PENDING
        → EVIDENCE_PENDING → ROOT_CAUSE_ANALYSIS → ROOT_CAUSE_READY
        → FIX_PROPOSAL → FIX_PROPOSAL_READY
        → TEST_PLAN → VERIFICATION_PENDING → COMPLETED
```

`COMPLETED`, `FAILED` and `CANCELLED` are terminal. The transition table lives
in `app/services/state_machine.py` and is the only place a transition is
permitted; `WorkflowStateMachine.assert_transition()` is the single choke point.

A run stops at `FIX_PROPOSAL_READY`, where the placeholder keeps it parked. That
is the human approval gate: nothing may move a run past it on the server's own
initiative. `TEST_PLAN` is implemented, but the gate is in front of it, so no
run reaches it without a human — which is why `test_test_plan_stage.py` drives
the stage directly and `test_orchestrator.py` advances past the gate explicitly.

### Advancing a run

```python
from app.services.orchestrator import MentorOrchestrator
orch = MentorOrchestrator()
run = orch.create_run("../demo-repository/ShopDemo", "Checkout fails with a discount")
result = orch.run_until_stalled(run.run_id)
print(result.run.current_state, result.stalled)   # FIX_PROPOSAL_READY True
```

`advance()` runs exactly one stage. `run_until_stalled()` loops until a
placeholder stage returns `StageOutcome.PENDING`, a stage is missing from the
registry, or the run reaches a terminal state.

### Reading the Phase 8 analysis

```python
from app.services.root_cause_stage import load_root_cause
analysis = load_root_cause(orch.get_run(run.run_id))
print(analysis.status.value, analysis.confidence_level.value)
print(analysis.root_cause)          # None unless the status supports a claim
print(analysis.anchor.file_path, analysis.anchor.line)
```

### Reading the Phase 9 proposal

```python
from app.services.fix_proposal_stage import load_fix_proposal
proposal = load_fix_proposal(orch.get_run(run.run_id))
print(proposal.status.value, proposal.is_ready_for_review())
if proposal.unified_diff:
    print(proposal.unified_diff)   # never applied, never verified
```

Three points worth internalising before you change this code:

- **`tests_executed` is always `false`** and `applied` is not on the model at all.
  A diff that has not been run is not a fix.
- **`unified_diff` is `None`, never `""`**, unless the status is
  `READY_FOR_REVIEW`. That is what stops "no fix" rendering as a change that
  removes nothing.
- **The proposal is deterministic and timestamp-free.** The wall clock is in
  `run.context["fix_proposal_created_at"]`; keep it out of the model or replay
  breaks.

`GET /api/mentor/runs/{run_id}/fix-proposal` returns `409` when no proposal has
been recorded yet and `200` with a null diff when one was honestly declined.
There is no apply endpoint, and adding one is a later phase's job behind the
approval gate.

Over HTTP:

```
GET /api/mentor/runs/{run_id}/root-cause
```

Returns `400` for a malformed id, `404` for an unknown run, and `409` when the
run has no analysis yet. `tests_executed` and `root_cause_claimed` are always
present so a client cannot present a static reading of the source as a
reproduced defect.

### Reading the Phase 10 test plan

```python
from app.services.test_plan_stage import load_test_plan
plan = load_test_plan(orch.get_run(run.run_id))
print(plan.status.value, len(plan.coverage_gaps), len(plan.scenarios))
for scenario in plan.scenarios:
    print(scenario.suggested_file, "|", scenario.discriminating_condition)
```

For a run that has not passed the approval gate this returns `None`, because the
stage never ran — the same 409/200 split as Phase 9. Four points worth
internalising:

- **Phase 10 has no repository access at all**, not even the one constrained read
  Phase 9 was granted. Every fact it uses is already on the run. Do not add a
  read to "just check" a path;
  `test_no_phase10_module_can_read_write_or_execute_anything` will fail.
- **It delivers a plan, not test code.** The roadmap calls it "Test Generation";
  it deliberately does not generate source. A plausible test nobody has run is
  exactly the artefact this project must not hand over as "your regression
  test".
- **Gaps are copied, not re-derived.** They are Phase 7 hypotheses with their
  evidence IDs, and the planner never promotes one to an `OBSERVATION`.
- **Keep `discriminating_condition`.** Without it a regression test can pass on
  the broken code forever. A scenario's `covers_gaps` must only name gaps about
  its own file, and gaps no scenario closes stay in `coverage_gaps` and are
  named in `limitations` — an unclosed gap is an open item, not a closed one.

`TestExecutionStatus` has exactly one member, `NOT_RUN`. Phase 11's executor has
to add members deliberately; do not add a `PASSED` here.

```
GET /api/mentor/runs/{run_id}/test-plan
```

Returns `400` for a malformed id, `404` for an unknown run, `409` when the stage
has not run, and `200` with no scenarios for an honest negative.
`tests_executed` and `test_source_written` are hard-wired `false` from literals.
There is no run, apply or write endpoint.

### Adding a stage in a later phase

1. Implement `StageHandler` (set `state`, implement `execute`) in
   `app/services/stages.py`.
2. Return `StageResult.completed(..., next_state=self.state, evidence=[...])`.
   `next_state` must equal the state the stage is registered under — the
   orchestrator rejects a mismatch, so a stage can never steer the workflow.
3. Replace the placeholder in `build_default_stages()`.
4. Evidence must be typed `EvidenceType.OBSERVATION` until a later phase is
   genuinely able to produce the stronger type.
5. If the phase needs repository facts the run does not already carry, they have to
   be recorded as evidence by an earlier phase. Reading the repository again
   creates a second source of truth that can disagree with the diagnosis.

### Replacing a stage in tests

`MentorOrchestrator` accepts an injected registry, so a test can substitute a
stub that raises, returns no evidence, or never finishes — no monkeypatching
required.

---

## SSE Streaming

`GET /api/mentor/runs/{run_id}/events` streams the run's events.

| Query parameter | Default | Notes |
| --- | --- | --- |
| `last_event_id` | — | Resume cursor (falls back to the `Last-Event-ID` header) |
| `timeout` | `60` | Stream lifetime in seconds; must be `> 0` |
| `max_events` | — | Stop after N events; must be `1…5000` |

The stream replays the run's whole event log in sequence order, then follows new
events by polling the event store. It always terminates: on `max_events`, on
`timeout`, or as soon as the run is terminal. Streams are scoped to one run — a
cursor from another run is ignored, and no other run's events are ever emitted.

---

## Development Conventions

- **Backend:** Follow PEP 8. Use type hints throughout. Keep functions small.
- **Frontend:** Use TypeScript strictly. Prefer functional components with hooks.
- **Evidence types:** Always use the `EvidenceType` enum — never raw strings.
- **Tests:** Write tests for every new agent, service, and endpoint.
- **Commits:** One logical change per commit. Include the phase number in the
  message.
- **No secrets:** Never hard-code API keys, tokens, or passwords.
- **State:** Only the orchestrator may change `current_state`, always through
  `WorkflowStateMachine.assert_transition()`. Never accept state from a request
  body.
- **Honesty:** A stage that did not do the work must return `StageOutcome.PENDING`
  and no evidence. Never let a placeholder advance a run or report a result.

---

## Adding a New AI Provider

1. Create `backend/app/agents/providers/<name>_provider.py`
2. Implement the `AIProvider` protocol (see
   `backend/app/agents/providers/protocol.py`)
3. Register the provider in `backend/app/core/config.py`
4. Add the required environment variables to `.env.example`

---

## Phase-Gated Features

Many modules are scaffolded but empty. They will be implemented in their
designated phase. Do not implement future-phase logic early — keep the codebase
at the correct abstraction level for the current phase.

| Phase | What gets implemented |
| --- | --- |
| Phase 2 | `demo-repository/` application |
| Phase 3 | `ingestion/cloner.py`, `ingestion/explorer.py` |
| Phase 4 | `analysis/ast_analyzer.py` |
| Phase 5 | `agents/architecture.py` |
| Phase 6 | `services/state_machine.py`, `services/stages.py`, `services/orchestrator.py`, `api/mentor.py`, `api/stream.py` |
| Phase 7 | `investigation/` investigators, `services/investigation_stage.py` |
| Phase 8 | `analysis/evidence_synthesizer.py`, `models/root_cause.py`, `services/root_cause_stage.py` |
| Phase 9 | `analysis/fix_proposer.py`, `models/fix_proposal.py`, `services/fix_proposal_stage.py` |
| Phase 10 | `analysis/test_planner.py`, `models/test_plan.py`, `services/test_plan_stage.py` |
| Phase 11 | `agents/verification_agent.py`, `executor/sandbox.py` |
| Phase 12 | End-to-end integration |
| Phase 13 | Full frontend UI |

Phases 1–10 are implemented. Phase 11 ships as a placeholder: it is wired into
the state machine so the later phase has a stable contract, but it produces no
evidence, runs nothing and cannot advance a run. `VERIFICATION_PENDING` is the
only remaining placeholder. Do not implement a future phase's behaviour inside a
placeholder — replace the placeholder.

### Adding a root-cause rule

Phase 8 rules live in `app/analysis/evidence_synthesizer.py` and must satisfy
three constraints at once:

1. **Deterministic.** No model call, no clock, no iteration-order dependence. The
   same evidence must produce the same verdict, and the analysis model must stay
   free of timestamps so two runs compare equal.
2. **Traceable.** A rule may only assert a *location* from `OBSERVATION` evidence.
   If it cites a `HYPOTHESIS`, it is reasoning from an inference and must not
   claim a fact.
3. **Non-repository.** Phase 8 reasons over evidence Phase 7 already extracted and
   must not open a file. `test_phase6_security.py` enforces this by parsing the
   Phase 8 modules with `ast`, including a check that they never call `open()`.

Add the rule, a test that fires it, and a test that proves it stays quiet on the
correct form of the same code.

### Widening the Phase 9 rewriter

The rewriter in `app/analysis/fix_proposer.py` is the highest-risk code in the
system, because a wrong rewrite is a silently wrong *fix* rather than a wrong
report. Three constraints hold for any change to it:

1. **Refuse rather than approximate.** A pattern may only be added if you can state
   why the rewrite is correct for *every* input it matches. `int(a + b / c) * d`
   is the standing counter-example: the mechanical move changes the program's
   meaning.
2. **Never fire where Phase 8 is silent.** The rewrite target and the detector must
   agree, or Phase 9 would be editing a line nobody diagnosed.
   `test_rewriter_never_fires_where_the_phase8_detector_is_silent` asserts the
   invariant directly.
3. **Re-check the arithmetic.** A new pattern needs a test showing the rewritten
   expression returns the *intended* value, and that it leaves already-correct
   inputs unchanged. `test_rewritten_expression_actually_fixes_the_arithmetic`
   is the model.

If you cannot satisfy all three, do not add the pattern. `INSUFFICIENT_EVIDENCE`
is an acceptable, honest outcome; a plausible-looking wrong diff is not.
