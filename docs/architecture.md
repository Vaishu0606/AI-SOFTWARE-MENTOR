# AI Software Mentor — Architecture

## System Overview

The AI Software Mentor is a multi-agent system that automates the debugging and
investigation workflow for unfamiliar or AI-generated codebases. A developer provides
a natural-language bug report and a repository URL; the system handles the rest.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────┐
│                    Frontend                      │
│          Next.js · TypeScript · Tailwind         │
│                                                  │
│  RepoInput       AgentTimeline     EvidenceCard  │
│  DiffViewer      HypothesisList    TestResults   │
│  ApprovalGate                                    │
└─────────────────────┬───────────────────────────┘
                      │ REST / SSE
┌─────────────────────▼───────────────────────────┐
│                    Backend                       │
│              FastAPI · Python 3.11+              │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │              Orchestrator                │   │
│  │   State machine: INIT → EXPLORE →        │   │
│  │   INVESTIGATE → FIX → TEST → VERIFY      │   │
│  └─────────────────┬────────────────────────┘   │
│                    │                             │
│  ┌─────────────────▼────────────────────────┐   │
│  │              Agent Layer                 │   │
│  │                                          │   │
│  │  1. Repository Explorer                  │   │
│  │  2. Architecture Agent                   │   │
│  │  3. Bug Investigator (parallel branches) │   │
│  │  4. Fix Agent                            │   │
│  │  5. Test Agent                           │   │
│  │  6. Verification Agent                   │   │
│  └─────────────────┬────────────────────────┘   │
│                    │                             │
│  ┌─────────────────▼────────────────────────┐   │
│  │           AI Provider Layer              │   │
│  │   Abstract interface → watsonx.ai        │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │           Repository Store               │   │
│  │  Cloned repo (filesystem) + SQLite       │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │          Sandboxed Executor              │   │
│  │  subprocess + timeout + captured I/O     │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

---

## Agent Responsibilities

### Repository Explorer
- Walks the repository file tree
- Detects languages, frameworks, and package managers
- Locates files relevant to the bug report
- Locates existing test files
- **Output type:** `OBSERVATION`

### Architecture Agent
- Reads relevant source files
- Identifies components and their relationships
- Traces the data/control flow for the reported feature
- **Output type:** `OBSERVATION`

### Bug Investigator
- Traces the execution path for the bug scenario
- Launches parallel sub-investigations (asyncio.gather)
- Generates ranked competing hypotheses with evidence
- **Output type:** `OBSERVATION` (findings) + `HYPOTHESIS` (inferences)

### Fix Agent
- Proposes the minimal code change addressing the root cause
- Produces a unified diff for human review
- Does NOT apply changes until explicitly approved
- **Output type:** `PROPOSED_ACTION`
- **Implemented (Phase 9)** as a deterministic proposer — no model call, no writes

### Test Agent
- Identifies existing test coverage for the affected path
- Identifies the coverage gap that allowed the bug
- Outlines the regression test that would close it
- **Output type:** `PROPOSED_ACTION`
- **Implemented (Phase 10)** as a deterministic planner — no model call, no repository
  access, and it outlines scenarios in prose rather than emitting test source (see
  [Phase 10](#phase-10-test-plan-and-coverage-gaps))

### Verification Agent
- Executes tests via the sandboxed executor
- Captures actual stdout/stderr/exit code
- Reports pass/fail based on actual execution
- **Output type:** `VERIFIED_RESULT`

---

## Evidence Type System

```
OBSERVATION      ← directly read from files, test output, or tool results
HYPOTHESIS       ← reasoned inference; may be wrong; labeled clearly
PROPOSED_ACTION  ← fix or test awaiting human review and approval
VERIFIED_RESULT  ← confirmed by actual execution with recorded exit code
```

A `HYPOTHESIS` must never be promoted to `VERIFIED_RESULT` without actual test
execution. The Orchestrator enforces this distinction in the evidence store.

---

## Orchestrator State Machine

```
IDLE
  │  (bug report submitted)
  ▼
CLONING
  │  (repo cloned to isolated session dir)
  ▼
EXPLORING        ← Repository Explorer
  │
  ▼
MAPPING          ← Architecture Agent
  │
  ▼
INVESTIGATING    ← Bug Investigator (parallel branches)
  │
  ▼
AWAITING_APPROVAL  ← Fix Agent produces diff; UI gate
  │  (user approves)
  ▼
FIXING           ← Fix applied to working copy
  │
  ▼
GENERATING_TESTS ← Test Agent
  │
  ▼
VERIFYING        ← Verification Agent
  │
  ▼
COMPLETE
```

If the user rejects the fix at `AWAITING_APPROVAL`, the workflow returns to
`INVESTIGATING` with updated context.

### As implemented through Phase 10

The design above is the target end state. Phases 1–10 ship the subset that can honestly
be executed today, with states that name *what is done* rather than *what is planned*,
and a `_PENDING` state for every later phase that is wired in but not implemented:

```
CREATED
  → REPOSITORY_CONTEXT     ← Phase 3 explorer
  → ARCHITECTURE_CONTEXT   ← Phase 5 architecture agent
  → INVESTIGATION_PENDING  ← Phase 7 investigators
  → EVIDENCE_PENDING       ← Phase 8 evidence synthesis
  → ROOT_CAUSE_ANALYSIS    ← Phase 8 root-cause analysis
  → ROOT_CAUSE_READY       ← Phase 9 hand-off gate
  → FIX_PROPOSAL           ← Phase 9 fix proposer and diff
  → FIX_PROPOSAL_READY     ← Phase 9 approval gate (placeholder)
  → TEST_PLAN              ← Phase 10 test planner
  → VERIFICATION_PENDING   ← Phase 11 (placeholder)
  → COMPLETED              ← only via explicit server-side completion
```

| Design state | Implemented state |
|---|---|
| `IDLE` / `CLONING` / `EXPLORING` | `REPOSITORY_CONTEXT` |
| `MAPPING` | `ARCHITECTURE_CONTEXT` |
| `INVESTIGATING` | `INVESTIGATION_PENDING` |
| evidence synthesis | `EVIDENCE_PENDING` |
| root-cause ranking | `ROOT_CAUSE_ANALYSIS` |
| — | `ROOT_CAUSE_READY` |
| `FIXING` | `FIX_PROPOSAL` |
| `AWAITING_APPROVAL` | `FIX_PROPOSAL_READY` |
| `GENERATING_TESTS` | `TEST_PLAN` |
| `VERIFYING` | `VERIFICATION_PENDING` |
| `COMPLETE` | `COMPLETED` |

`FAILED` and `CANCELLED` are terminal escape hatches from any non-terminal state.
A run still stops at `FIX_PROPOSAL_READY` even though the phase after it is implemented,
because the gate in front of it is still a placeholder: the planner exists and is
exercised by tests, but no run reaches it on the server's own initiative. The
`_PENDING` states past the gate exist so later phases have a stable contract, not as
evidence that the work happened.

`FIX_PROPOSAL` and `FIX_PROPOSAL_READY` are two states rather than one because "we
drafted a proposal" and "a proposal is waiting for a human" are different claims, and
collapsing them would let a run report a fix as done the moment it was written. The
split also makes the human approval gate a *state in the machine* rather than a UI
convention: a run physically cannot move past `FIX_PROPOSAL_READY` on the server's own
initiative, which is the only way "never applied without explicit approval" is a
property of the system instead of a promise about it.

Phase 9 split a single planned `FIX_PENDING` state in two, and did not keep the old
name as an alias. An alias would let a client depend on a name that no longer describes
anything, and would blur the boundary between the states that exist and the one that
never did. Phase 10 renamed `TEST_PENDING` to `TEST_PLAN` for the same reason: a *plan*
and an *executed test* are different claims, and Phase 10 produces only the first.

The transition table is `ALLOWED_TRANSITIONS` in
`backend/app/services/state_machine.py`; `WorkflowStateMachine.assert_transition()` is
the only sanctioned way to change state.

---

## Phase 8: Evidence Synthesis and Root-Cause Analysis

Phase 8 is the first component that *concludes*. It consumes the unranked evidence
Phase 7 produced and returns either a stated root cause or a named reason it has none.

### Two steps, two states

`EVIDENCE_PENDING` (`EvidenceSynthesisStage`) normalises the input and builds a
structural index. It refuses `VERIFIED_RESULT` outright — no implemented phase can
produce one, so accepting it would mean reasoning from a result nothing ran — and
records the refusal in `rejected_types` rather than dropping it silently. Only
`OBSERVATION` items are mined for structure, because a hypothesis may inform
confidence but must never be used to assert a *location*.

The index (`EvidenceSynthesis`) is persisted in `run.context`, which is what makes the
two stages independently inspectable and replayable. `ROOT_CAUSE_ANALYSIS`
(`RootCauseAnalysisStage`) rebuilds the index from it and ranks candidates.

```
InvestigationResult
   │  reject VERIFIED_RESULT, deduplicate, index
   ▼
EvidenceSynthesis  ── run.context["evidence_synthesis"]
   │  detect candidates, score confidence, build chain
   ▼
RootCauseAnalysis  ── run.context["root_cause_summary"]
```

The wall-clock time is stored separately in `run.context["root_cause_synthesized_at"]`
and is deliberately absent from the analysis model, so two runs over identical
evidence produce comparable objects.

### Deterministic, not generative

The rules in `app/analysis/evidence_synthesizer.py` are static pattern matchers over
evidence that Phase 7 already read out of the source. No model is consulted, so the
same evidence always yields the same verdict and every claim is traceable to a stored
evidence ID. The current rules cover truncation-before-scaling
(`int(a / 100) * b` — the demo defect), unexercised code paths, and uncovered
interactions between modules.

This is a deliberate scope choice for the MVP, not an accident: a conclusion that
cannot be re-derived from the evidence store cannot be reviewed, and a reviewable
conclusion is the project's core promise.

### Honest non-conclusions

`RootCauseStatus` is the primary output, not a footnote. `DETERMINED` requires a
candidate; `PARTIAL` means one was found but an investigator failed; `INSUFFICIENT_
EVIDENCE` means the evidence supports nothing; `NO_EVIDENCE` and `FAILED` mean there
was nothing to reason from. `root_cause` is `null` unless the status says otherwise,
so a client cannot display a conclusion that contradicts the qualification attached
to it.

### Confidence is earned and explained

```
score = base(kind) + min(corroborating_investigators - 1, 2) × 0.08 − penalties
```

Penalties are named and additive: `conflicting_evidence` (0.15 per conflict),
`reported_path_untested` (0.12), `partial_investigation` (0.20 per failed
investigator), `no_direct_observation` and `inference_only` (0.10 each). The result is
clamped to `[0, 1]` and then capped below `HIGH` whenever the investigation was partial
or the evidence conflicts — an incomplete picture cannot be reported as a strong
conclusion, by construction rather than by convention. `ConfidenceBreakdown` returns
every term and a prose rationale, and the API publishes the thresholds so a client can
re-derive the band instead of trusting a label.

### Traceability

`evidence_chain` walks `ROUTE → CALL → SITE → DATA_FLOW → COVERAGE` from the entry
point to the defect site, and `supporting_evidence` cites the stored evidence IDs
behind the verdict. `contributing_factors` records real conditions that are not the
root cause, `conflicting_evidence` records claims that cannot all be true, and
`unresolved_questions` records what static analysis could not settle with a
recommended next step. Contradictions are surfaced, never silently resolved.

### What Phase 8 cannot do

`tests_executed` is always `false` and `verified_result_count` is always `0`. Phase 8
reads source; it does not run it. A static reading of the source can therefore never
be presented as a reproduced defect — only Phase 11's sandboxed executor can produce a
`VERIFIED_RESULT`, and until then the system has no proof that the reported defect
manifests as described.

---

## Phase 9: Fix Proposal and Reviewable Diff

Phase 9 is the first component that *proposes changing code*. It produces a minimal
diff and stops at a human approval gate. It writes nothing and executes nothing.

### Two steps, two states

`ROOT_CAUSE_READY` (`RootCauseReadyStage`) is a gate, not a step. It asserts that the
Phase 8 analysis is present and readable, and refuses to continue otherwise. Making the
hand-off a real stage rather than a check keeps the "one stage, one state" contract
intact and turns a missing Phase 8 result into a loud failure — otherwise a run would
stall on the old placeholder here and never reach the proposer. A missing analysis is a
*stage failure*, not an "insufficient evidence" proposal, because reaching this state
without one means the workflow was driven out of order, and reporting that as a
considered opinion about the repository would misrepresent a wiring fault as a finding.

`FIX_PROPOSAL` (`FixProposalStage`) runs the deterministic proposer and records the
result. An honest "I cannot responsibly change this" is a *completed* stage carrying an
`INSUFFICIENT_EVIDENCE` or `INVALID` proposal, not a failure: the run still has to reach
the approval gate so the developer can see why nothing was proposed.

### The three outcomes are the design

| Status | When | Why it is separate |
|---|---|---|
| `READY_FOR_REVIEW` | A diff was built | The only case carrying a `unified_diff` |
| `INSUFFICIENT_EVIDENCE` | The problem is understood but not actionable enough | The remedy is to investigate further or design by hand |
| `INVALID` | The anchor itself is untrustworthy | The remedy is to re-run the analysis |

Collapsing the two negative statuses would lose the distinction between "we looked and
found nothing to do" and "we could not trust what we were looking at", which lead to
opposite next steps for the reviewer. `unified_diff` is `null` — never an empty string
— unless the status is `READY_FOR_REVIEW`, so a client cannot render "no fix" as a
change that removes nothing.

### One reader, and nothing else

`read_source_text()` in `app/analysis/fix_proposer.py` is the only filesystem access in
Phase 9. It reuses the Phase 3 secret-file and path-safety rules and adds a size and a
binary check. The stage and the model are statically forbidden from reaching the
filesystem at all, which `test_phase9_uses_the_constrained_reader_and_nothing_else`
enforces by parsing them with `ast`.

Phase 9 re-reads the **live** repository rather than the Phase 7 snapshot, because a
diff must be built against the code as it is *now* — a snapshot is a point-in-time view
that may be hours old. It compensates by requiring the Phase 8 excerpt to match exactly
one current line, so a file that changed after the investigation yields no diff rather
than a stale one. If the excerpt matches more than once, no diff is offered either:
attributing a change to one of two identical lines would be a guess.

The proposal stores an `AnchorLocation` (file, line, function) rather than a
`SourceAnchor`, so it never holds quoted source. That is what keeps a refusal against a
secret path from echoing the secret back to the client, and it removes the need to
re-audit a proposal for leaks every time it is rendered or logged.

### Refusal over approximation

The rewriter handles one narrow shape: a single quantising cast (`int`, `float`,
`round`) of a single division, scaled by one simple operand. Anything else is declined.
`int(a + b / c) * d` is the instructive case — mechanically "move the `d` inside", it
produces `int(a + b * d / c)`, which is a *different program*. The rewriter refuses
rather than approximates, and the refusal is reported honestly.

A recognised defect that the rewriter will not touch is `INSUFFICIENT_EVIDENCE`, not
`INVALID`: the anchor matched, so the evidence is current and the source has not moved
on. Claiming otherwise would send the reviewer to repeat an investigation whose evidence
is still accurate.

### Nothing is claimed

`FixProposal` carries no timestamp, so two runs over the same repository compare equal;
the wall clock lives in `run.context["fix_proposal_created_at"]`, the same separation
Phase 8 uses. The API hard-wires `applied = False` and `tests_executed = False` from
literals rather than from the model, so no future edit to the proposal can quietly make
the endpoint claim the fix was tried or applied. The validation plan is phrased
imperatively — instructions for a person, not results — and the risk assessment names
the specific signal it matched, so a reviewer can argue with a factor rather than a
score.

---

## Phase 10: Test Plan and Coverage Gaps

Phase 10 takes the Phase 8 diagnosis and records what a regression test would have to
do. It is deterministic, reads nothing, writes nothing, and runs nothing.

### One state

```
TEST_PLAN   ← TestPlanStage → plan + one PROPOSED_ACTION per scenario
```

The stage loads the Phase 8 analysis and the Phase 9 proposal from `run.context`, hands
the recorded Phase 7 evidence to `TestPlanner`, and persists the resulting `TestPlan`
under `run.context["test_plan"]`. A decline is a *completed* stage carrying an
`INSUFFICIENT_EVIDENCE` or `INVALID` plan, because a reviewer still needs to be told that
no plan exists and why. Only a missing diagnosis fails the stage: reaching `TEST_PLAN`
without one means the workflow was driven out of order, and reporting a wiring fault as
an opinion about the repository would be the same mistake Phase 9's gate avoids.

### No repository access at all

Phase 9 was granted one constrained read, because a diff has to be built against the code
as it is *now*. Phase 10 needs no such thing, so it has none. Every input is already on
the run:

| Fact | Source |
|---|---|
| what is wrong, and where | the Phase 8 analysis |
| whether a fix was proposed | the Phase 9 proposal |
| which production files each test file imports | Phase 7 `import_graph` observations |
| which concepts no test exercises together | Phase 7 `coverage_analysis` hypotheses |

Re-deriving any of it by reading the repository again would create a second source of
truth that could silently disagree with the evidence the diagnosis was built on. If the
evidence needed to plan a test is missing, the honest answer is `INSUFFICIENT_EVIDENCE` —
not a fresh guess. `test_no_phase10_module_can_read_write_or_execute_anything` fails if
any Phase 10 module reaches the filesystem, so do not add a read to "just check" a path.

### Copied gaps, not re-derived ones

A plan's `coverage_gaps` are Phase 7 *hypotheses* with their evidence IDs attached. The
planner never re-derives a gap, never re-scores one, and never promotes a `HYPOTHESIS`
to an `OBSERVATION`; its only evidence output is `PROPOSED_ACTION`. Re-emitting a
hypothesis as an observation would quietly promote an inference to a fact, and a plan is
the most plausible place for that to happen — it is the component that *reuses* other
phases' conclusions and is therefore closest to being tempted to upgrade them.

Gaps that no scenario addresses stay in the list and are named in `limitations`. A gap
with no scenario is an open item; trimming the list to the covered ones would make the
coverage picture look better than the investigation found it to be.

### The discriminating condition

Each scenario states what the input must be for the test to tell the diagnosed behaviour
apart from correct behaviour. For the demo defect that means the unscaled total must not
be an exact multiple of the divisor, because on an exact multiple the two orders of
operations agree and the test would pass against the broken code. Without this field a
regression test can pass on broken code forever, which is how a real suite ends up with
a test that never would have caught the bug.

`expected_behavior` is the violated *invariant* rather than a numeric literal: a number
would be a guess about input values nobody has run, and a wrong one would turn a correct
plan into a failing test.

### Scenarios, and only scenarios

- **1. A direct regression test** for the diagnosed line, aimed at the anchor. It claims
  only the gaps that are about the anchor's own file.
- **2. The broadest untested interaction.** A demo issue usually names three or four
  terms, so Phase 7 reports a pair per combination; one scenario per pair produces
  near-duplicates that crowd out the ones a developer would act on. Every pair stays in
  the gap list, and the scenario picks the one spanning the most production modules.
- **3. The request boundary**, when Phase 7 recorded that no test imports a route module.

Capped at three, because a plan is a review artefact and an unread plan is a silent one.

### Nothing is claimed

`TestPlan` carries no timestamp, like the Phase 8 analysis and the Phase 9 proposal; the
wall clock lives in `run.context["test_plan_created_at"]`. The API hard-wires
`tests_executed = False` and `test_source_written = False` from literals rather than from
the model, and `TestExecutionStatus` has exactly one member, `NOT_RUN` — the *type*
cannot express a pass, so Phase 11's executor has to add members deliberately rather
than flip a flag. A proposed new test file is marked `PROPOSED_NEW` and recorded in
`assumptions`, because the path is inferred from the observed naming convention and
nobody has created it.

The read endpoint is `GET /api/mentor/runs/{run_id}/test-plan`. Because the approval gate
is in front of the stage it returns `409` for every run the system produces today; a
genuine negative returns `200` with no scenarios. There is deliberately no run, apply or
write endpoint.

---

## Communication: SSE Streaming

The backend emits Server-Sent Events for every state transition and evidence item.
The frontend consumes these via a `useSSE` hook and renders the agent timeline in
real time. This means the UI is live and responsive during long investigations —
the user sees each agent's progress as it happens.

---

## Data Persistence

| Data | Storage |
|---|---|
| Investigation sessions | SQLite (`sessions` table) |
| Evidence items | SQLite (`evidence` table) |
| Cloned repository files | Local filesystem (`/tmp/mentor-sessions/{id}/`) |
| Analysis cache | JSON files per session |

No vector database is used in the MVP. File-system + AST analysis is sufficient
for the demo scenario.

---

## Security Boundaries

| Threat | Control |
|---|---|
| Prompt injection from source files | Data-delimiter enforcement in all prompts |
| Path traversal | `PathValidator` enforces session-root containment |
| Secret exposure | Blocklist prevents reading `.env`, `*.key`, `*secret*` files |
| Arbitrary command execution | Only the Verification Agent may execute; sandboxed subprocess |
| Unreviewed code modification | Human approval gate is a workflow state; Phase 9 writes no file |
| Unreviewed test creation | Phase 10 has no repository access at all and writes no file; `test_source_written` is invariant-locked to `False` |
| Secret echoed in a refusal | Proposals store an `AnchorLocation`, never a source excerpt; plans reuse it |
| Cross-session contamination | Each session has an isolated temp directory |

---

## AI Provider Abstraction

All agent LLM calls go through the `AIProvider` protocol:

```python
class AIProvider(Protocol):
    async def generate(self, prompt: str, **kwargs) -> str: ...
    async def generate_structured(self, prompt: str, schema: type[T], **kwargs) -> T: ...
    async def generate_stream(self, prompt: str, **kwargs) -> AsyncIterator[str]: ...
```

This allows swapping providers (watsonx.ai, OpenAI-compatible, local models) without
modifying any agent logic.
