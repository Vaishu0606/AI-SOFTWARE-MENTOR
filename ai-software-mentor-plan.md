# AI Software Mentor — Technical Plan
## IBM Bob 2.0 Hackathon Submission

---

## Top-Level Overview

**Goal:** Build an agentic AI Software Mentor that helps developers debug, understand, and maintain unfamiliar or AI-generated codebases. The product replaces a slow, manual, multi-step investigation process with an evidence-driven, multi-agent workflow.

**Core Demo Scenario:** A developer reports "Checkout is failing when a discount code is applied." The system autonomously explores the repository, traces execution, generates competing hypotheses, identifies the root cause, proposes the minimal fix, generates a regression test, executes it, and presents all findings with clear distinction between OBSERVATION, HYPOTHESIS, PROPOSED ACTION, and VERIFIED RESULT.

**Scope Boundaries (MVP):**
- One controlled demo repository (small e-commerce app with intentional bug)
- GitHub repository integration (clone + analyze)
- Six specialized agents orchestrated by a central Orchestrator
- Evidence-driven output; hypotheses are never promoted to verified facts
- Fix proposal with reviewable diff (human approval required before apply)
- Regression test generation and execution
- No vector database in MVP; file-system + AST analysis is sufficient
- No multi-tenancy; single-user demo environment

**Non-Goals (MVP):**
- Real payment processing integration
- Production deployment / cloud hosting
- Multi-repository cross-referencing
- Autonomous code apply without human approval

---

## 1. Recommended Architecture

```
┌─────────────────────────────────────────────┐
│                  Frontend                    │
│         Next.js + React + TypeScript         │
│              Tailwind CSS                    │
│                                              │
│  ┌──────────────┐   ┌─────────────────────┐ │
│  │  Bug Report  │   │  Evidence Timeline  │ │
│  │  Input Form  │   │  & Agent Trace UI   │ │
│  └──────────────┘   └─────────────────────┘ │
└──────────────────────┬──────────────────────┘
                       │ REST / SSE (streaming)
┌──────────────────────▼──────────────────────┐
│              Backend (FastAPI)               │
│                                             │
│  ┌──────────────────────────────────────┐   │
│  │        Orchestrator Service          │   │
│  │  Manages agent lifecycle, sequencing │   │
│  │  evidence collection, state machine  │   │
│  └────────────────┬─────────────────────┘   │
│                   │                          │
│   ┌───────────────┼───────────────────┐      │
│   ▼               ▼                   ▼      │
│ [Repo Explorer] [Bug Investigator] [Fix Agent]│
│ [Arch Agent]   [Test Agent]   [Verif. Agent] │
│                                             │
│  ┌──────────────────────────────────────┐   │
│  │           AI Provider Layer          │   │
│  │   Abstraction over LLM API calls     │   │
│  │   (provider-swappable interface)     │   │
│  └──────────────────────────────────────┘   │
│                                             │
│  ┌──────────────────────────────────────┐   │
│  │         Repository Store             │   │
│  │  Local cloned repo + analysis cache  │   │
│  │  (filesystem + SQLite session state) │   │
│  └──────────────────────────────────────┘   │
│                                             │
│  ┌──────────────────────────────────────┐   │
│  │       Sandboxed Executor             │   │
│  │  Isolated test execution environment │   │
│  └──────────────────────────────────────┘   │
└─────────────────────────────────────────────┘
```

---

## 2. Technology Stack

### Frontend
| Concern | Choice | Rationale |
|---|---|---|
| Framework | Next.js 14 (App Router) | SSE streaming, API routes, fast setup |
| Language | TypeScript | Type safety, hackathon-friendly |
| Styling | Tailwind CSS | Rapid UI development |
| State | React hooks + Context | No Redux overhead for MVP |
| Streaming UI | Server-Sent Events | Real-time agent progress updates |
| Code display | react-syntax-highlighter | Diff and code block rendering |

### Backend
| Concern | Choice | Rationale |
|---|---|---|
| Framework | FastAPI (Python) | Async, SSE, fast iteration |
| Language | Python 3.11+ | Best AI/analysis library ecosystem |
| Session state | SQLite (via SQLModel) | Zero infrastructure, sufficient for demo |
| Task queue | asyncio / background tasks | No Redis needed for single-user demo |
| Env management | python-dotenv | Simple secret handling |

### Repository Analysis
| Concern | Choice | Rationale |
|---|---|---|
| Filesystem | pathlib + os.walk | No extra dependencies |
| Python AST | ast module (stdlib) | Dependency graph, call tracing |
| JS/TS AST | tree-sitter (Python binding) | Cross-language parsing |
| Dependency map | requirements.txt / package.json parser | Simple text parse |
| Git operations | GitPython | Clone, diff, history |

### AI Layer
| Concern | Choice | Rationale |
|---|---|---|
| Abstraction | Custom AIProvider interface | Swap providers without changing agents |
| Initial provider | watsonx.ai / OpenAI-compatible API | Required for IBM submission |
| Prompt construction | Jinja2 templates | Structured, auditable prompts |

### Storage
| Concern | Choice | Rationale |
|---|---|---|
| Session state | SQLite | Zero infra, sufficient for MVP |
| Repo files | Local filesystem (cloned repo) | Direct file access |
| Analysis cache | JSON files per session | Simple, inspectable |
| Evidence store | SQLite evidence table | Queryable, preserves timestamps |

### Execution
| Concern | Choice | Rationale |
|---|---|---|
| Test runner | subprocess with timeout + cwd isolation | Controlled execution |
| Sandbox | restricted PATH, no network in subprocess | Minimal blast radius |
| Output capture | stdout/stderr capture | Full result evidence |

### Repository Connection
| Concern | Choice | Rationale |
|---|---|---|
| Integration | GitHub URL input → local clone | GitPython, no OAuth required for demo |
| Path isolation | Per-session temp directory | Sessions cannot cross-contaminate |

---

## 3. Directory Structure

```
ai-software-mentor/
├── AGENTS.md                        # Bob project context file
├── README.md                        # Project overview
├── ai-software-mentor-plan.md       # This plan file
├── bob_sessions/                    # Bob session evidence (screenshots/logs)
│
├── demo-repo/                       # Controlled demo e-commerce repository
│   ├── README.md
│   ├── backend/
│   │   ├── app/
│   │   │   ├── main.py
│   │   │   ├── models/
│   │   │   ├── routes/
│   │   │   │   ├── cart.py
│   │   │   │   ├── checkout.py
│   │   │   │   └── discount.py
│   │   │   ├── services/
│   │   │   │   ├── cart_service.py
│   │   │   │   ├── checkout_service.py
│   │   │   │   └── discount_service.py
│   │   │   └── utils/
│   │   ├── tests/
│   │   │   ├── test_cart.py
│   │   │   ├── test_checkout.py
│   │   │   └── test_discount.py
│   │   └── requirements.txt
│   └── frontend/
│       ├── src/
│       │   ├── pages/
│       │   │   ├── cart.tsx
│       │   │   └── checkout.tsx
│       │   └── components/
│       │       └── DiscountInput.tsx
│       └── package.json
│
├── backend/                         # Mentor backend (FastAPI)
│   ├── pyproject.toml
│   ├── requirements.txt
│   ├── .env.example
│   ├── app/
│   │   ├── main.py                  # FastAPI entry point
│   │   ├── config.py                # Settings and env loading
│   │   ├── orchestrator/
│   │   │   ├── __init__.py
│   │   │   ├── orchestrator.py      # Central workflow controller
│   │   │   ├── state_machine.py     # Investigation state transitions
│   │   │   └── evidence_store.py    # Evidence collection and storage
│   │   ├── agents/
│   │   │   ├── __init__.py
│   │   │   ├── base_agent.py        # Abstract base: run(), emit_evidence()
│   │   │   ├── repo_explorer.py     # Agent 1: Repository Explorer
│   │   │   ├── architecture_agent.py# Agent 2: Architecture Agent
│   │   │   ├── bug_investigator.py  # Agent 3: Bug Investigator
│   │   │   ├── fix_agent.py         # Agent 4: Fix Agent
│   │   │   ├── test_agent.py        # Agent 5: Test Agent
│   │   │   └── verification_agent.py# Agent 6: Verification Agent
│   │   ├── ai/
│   │   │   ├── __init__.py
│   │   │   ├── provider.py          # AIProvider abstract interface
│   │   │   ├── watsonx_provider.py  # watsonx.ai implementation
│   │   │   └── prompts/             # Jinja2 prompt templates
│   │   │       ├── repo_explore.j2
│   │   │       ├── bug_investigate.j2
│   │   │       ├── fix_propose.j2
│   │   │       ├── test_generate.j2
│   │   │       └── verify.j2
│   │   ├── repository/
│   │   │   ├── __init__.py
│   │   │   ├── cloner.py            # GitHub clone + isolation
│   │   │   ├── explorer.py          # File tree, language detection
│   │   │   ├── ast_analyzer.py      # AST-based code analysis
│   │   │   └── dependency_mapper.py # Dependency graph extraction
│   │   ├── executor/
│   │   │   ├── __init__.py
│   │   │   └── sandbox.py           # Sandboxed subprocess execution
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── session.py           # Session SQLModel
│   │   │   ├── evidence.py          # Evidence item SQLModel
│   │   │   └── workflow.py          # Workflow state SQLModel
│   │   └── api/
│   │       ├── __init__.py
│   │       ├── sessions.py          # Session CRUD endpoints
│   │       ├── workflow.py          # Workflow trigger + status endpoints
│   │       └── stream.py            # SSE streaming endpoint
│   └── tests/
│       ├── test_orchestrator.py
│       ├── test_agents.py
│       └── test_repository.py
│
└── frontend/                        # Mentor frontend (Next.js)
    ├── package.json
    ├── tsconfig.json
    ├── tailwind.config.ts
    ├── next.config.ts
    ├── src/
    │   ├── app/
    │   │   ├── layout.tsx
    │   │   ├── page.tsx             # Landing + repo input
    │   │   └── session/
    │   │       └── [id]/
    │   │           └── page.tsx     # Investigation session view
    │   ├── components/
    │   │   ├── RepoInput.tsx        # GitHub URL + bug report form
    │   │   ├── AgentTimeline.tsx    # Live agent progress
    │   │   ├── EvidenceCard.tsx     # OBSERVATION / HYPOTHESIS badges
    │   │   ├── DiffViewer.tsx       # Reviewable fix diff
    │   │   ├── HypothesisList.tsx   # Competing hypotheses display
    │   │   ├── TestResults.tsx      # Test execution results
    │   │   └── ApprovalGate.tsx     # Human approval before fix apply
    │   ├── hooks/
    │   │   ├── useSSE.ts            # Server-Sent Events consumer
    │   │   └── useSession.ts        # Session state management
    │   └── lib/
    │       ├── api.ts               # Backend API client
    │       └── types.ts             # Shared TypeScript types
    └── public/
```

---

## 4. Agent Architecture

### Orchestrator (Central Controller)

The Orchestrator is not an AI agent itself — it is a deterministic state machine that:
- Receives the bug report and repository URL
- Triggers agents in the correct sequence
- Manages dependencies between agent outputs
- Collects all evidence items into the Evidence Store
- Emits SSE events to the frontend for each state transition
- Enforces the OBSERVATION → HYPOTHESIS → PROPOSED ACTION → VERIFIED RESULT distinction
- Requires human approval before the Fix Agent applies any change

### Agent Execution Model

Each agent:
1. Receives a typed `AgentInput` context (evidence so far + task description)
2. Has read access to the local repository clone
3. Calls the AI Provider with a structured prompt
4. Returns a typed `AgentOutput` containing evidence items with explicit types
5. Emits progress events during execution
6. Never writes to the repository unless the Fix Agent is explicitly approved

### Parallelism

The Bug Investigator runs parallel sub-investigations:
- Parallel hypothesis branches are launched concurrently using `asyncio.gather`
- Each branch reports findings independently
- The Orchestrator then synthesizes competing hypotheses

---

## 5. Agent Responsibilities

### Agent 1 — Repository Explorer
**Input:** Repository path, bug report text
**Responsibilities:**
- Walk the repository file tree
- Detect languages, frameworks, and package managers
- Identify entry points (main.py, index.ts, etc.)
- Locate files relevant to the bug report keywords
- Locate existing test files
- Produce a structured repository map

**Output:** Repository map, file index, relevant file list, test file list
**Evidence type:** OBSERVATION

### Agent 2 — Architecture Agent
**Input:** Repository map, relevant file list
**Responsibilities:**
- Read relevant source files
- Identify components and their relationships
- Trace data flow for the reported feature (checkout + discount)
- Identify call chains between modules
- Produce an architecture summary

**Output:** Component map, call graph for relevant paths, architecture summary
**Evidence type:** OBSERVATION

### Agent 3 — Bug Investigator
**Input:** Architecture summary, relevant file list, bug report
**Responsibilities:**
- Read source files in the relevant execution path
- Trace the discount code application flow
- Identify suspicious code patterns, edge cases, type coercions
- Generate 2–4 competing hypotheses with supporting evidence
- Rank hypotheses by confidence

**Output:** Evidence items, hypothesis list (ranked), suspected root cause
**Evidence type:** OBSERVATION (findings) + HYPOTHESIS (hypotheses)

**Parallelism:** The Bug Investigator spawns parallel sub-investigations:
- One branch traces the discount calculation path
- One branch traces the cart total calculation path
- One branch inspects input validation
- Findings are merged and ranked

### Agent 4 — Fix Agent
**Input:** Highest-confidence hypothesis, relevant source files
**Responsibilities:**
- Propose the minimal code change that addresses the root cause
- Produce a unified diff (no unrelated refactoring)
- Explain why this change addresses the root cause
- Wait for human approval before applying

**Output:** Proposed diff, explanation, files affected
**Evidence type:** PROPOSED ACTION
**Gate:** Requires explicit human approval in the UI before applying

### Agent 5 — Test Agent
**Input:** Fix diff, relevant source files, existing test files
**Responsibilities:**
- Review existing tests for coverage of the affected path
- Identify the coverage gap that allowed the bug
- Generate a regression test that would have caught the bug
- Generate a test that validates the fix

**Output:** New test file content, explanation of what is validated
**Evidence type:** PROPOSED ACTION

### Agent 6 — Verification Agent
**Input:** Applied fix, generated tests, repository path
**Responsibilities:**
- Execute the regression tests in the sandboxed executor
- Capture stdout/stderr/exit code
- Report actual pass/fail status
- Never claim a test passed unless the exit code confirms it

**Output:** Test execution results, pass/fail status, stdout/stderr evidence
**Evidence type:** VERIFIED RESULT

---

## 6. Data Flow

```
User Input
  Bug report text + GitHub repo URL
           │
           ▼
    Orchestrator
    ┌─────────────────────────────────────────────────┐
    │  State: INITIALIZING                            │
    │  → Clone repository to isolated temp dir        │
    │  → Create session in SQLite                     │
    └───────────────────┬─────────────────────────────┘
                        │
                        ▼
    Agent 1: Repository Explorer
    ┌─────────────────────────────────────────────────┐
    │  OBSERVATION: Repository map                    │
    │  OBSERVATION: Relevant files identified          │
    │  OBSERVATION: Test files identified              │
    └───────────────────┬─────────────────────────────┘
                        │
                        ▼
    Agent 2: Architecture Agent
    ┌─────────────────────────────────────────────────┐
    │  OBSERVATION: Component relationships           │
    │  OBSERVATION: Execution path for checkout       │
    └───────────────────┬─────────────────────────────┘
                        │
                        ▼
    Agent 3: Bug Investigator (parallel branches)
    ┌──────────────────────────────────────────────────┐
    │  Branch A        Branch B        Branch C        │
    │  Discount calc   Cart totals     Input validation │
    │       │               │               │          │
    │       └───────────────┴───────────────┘          │
    │                       │                          │
    │  OBSERVATION: Code findings                      │
    │  HYPOTHESIS: Ranked list (1 = most likely)       │
    └────────────────────────┬─────────────────────────┘
                             │
                             ▼
    Human Review of Hypotheses
    ┌─────────────────────────────────────────────────┐
    │  UI: Hypothesis list displayed                  │
    │  User optionally selects preferred hypothesis   │
    │  (system uses top-ranked by default)            │
    └───────────────────┬─────────────────────────────┘
                        │
                        ▼
    Agent 4: Fix Agent
    ┌─────────────────────────────────────────────────┐
    │  PROPOSED ACTION: Minimal diff                  │
    │  PROPOSED ACTION: Root cause explanation        │
    └───────────────────┬─────────────────────────────┘
                        │
                        ▼
    Human Approval Gate
    ┌─────────────────────────────────────────────────┐
    │  UI: Diff viewer + Approve / Reject buttons     │
    │  No code is applied until approved              │
    └───────────────────┬─────────────────────────────┘
                        │ (on Approve)
                        ▼
    Agent 5: Test Agent
    ┌─────────────────────────────────────────────────┐
    │  PROPOSED ACTION: Regression test               │
    │  PROPOSED ACTION: Coverage gap explanation      │
    └───────────────────┬─────────────────────────────┘
                        │
                        ▼
    Agent 6: Verification Agent
    ┌─────────────────────────────────────────────────┐
    │  VERIFIED RESULT: Test execution output         │
    │  VERIFIED RESULT: Pass/fail with evidence       │
    └───────────────────┬─────────────────────────────┘
                        │
                        ▼
    Final Evidence Summary
    ┌─────────────────────────────────────────────────┐
    │  All evidence items with types displayed        │
    │  Full timeline of agent actions                 │
    │  Impact measurement captured (timing, steps)   │
    └─────────────────────────────────────────────────┘
```

---

## 7. Main User Workflow

1. User opens the Mentor UI
2. User enters a GitHub repository URL and a bug report ("Checkout is failing when a discount code is applied")
3. User clicks "Investigate"
4. System clones the repository into an isolated session directory
5. Agent Timeline panel activates — live SSE updates show each agent working
6. Repository Explorer reports: files found, relevant files highlighted
7. Architecture Agent reports: execution path for checkout + discount traced
8. Bug Investigator (3 parallel branches) reports: code findings and hypotheses
9. UI shows ranked hypotheses with evidence — HYPOTHESIS badges clearly visible
10. Fix Agent proposes the minimal diff — PROPOSED ACTION badge
11. UI shows reviewable diff panel with Approve / Reject buttons
12. User reviews and clicks Approve
13. Fix is applied to the session's working copy
14. Test Agent generates regression test — PROPOSED ACTION badge
15. Verification Agent executes tests — VERIFIED RESULT badge
16. Final summary screen: evidence timeline, root cause confirmed, test passing

---

## 8. Demo Repository Design

### Application: ShopDemo

A minimal but realistic e-commerce backend + frontend.

**Backend (Python/FastAPI):**
- `GET /products` — product catalog
- `POST /cart` — add item to cart
- `GET /cart/{id}` — get cart with line items and totals
- `POST /cart/{id}/discount` — apply discount code to cart
- `POST /checkout` — process checkout with cart total
- `GET /orders/{id}` — order status

**Frontend (Next.js):**
- Product listing page
- Cart page with discount code input
- Checkout confirmation page

**Data Models:**
- `Product`: id, name, price (integer cents)
- `CartItem`: product_id, quantity, unit_price
- `Cart`: id, items, subtotal, discount_amount, total
- `DiscountCode`: code, type (percent / fixed), value, min_order_value

**Tests:**
- `test_cart.py` — basic cart add/remove/total
- `test_discount.py` — discount code validation
- `test_checkout.py` — checkout flow (intentionally missing the combined case)

**The Bug (design — exact implementation kept private):**

The bug is located in the discount + checkout interaction path. It is:
- Deterministic: same inputs always reproduce the same failure
- Traceable: the failure path passes through checkout_service → discount_service → cart total calculation
- Fixable: a single-line or small targeted fix resolves it
- Testable: a unit test covering the specific input combination catches it

The bug category (to be kept out of public docs) is a type or calculation error in how the discount amount is applied to the cart total before checkout validation. The test suite contains tests for discount application and tests for checkout individually, but no test for the combined scenario — which is exactly the gap the Test Agent will identify and fill.

---

## 9. Security Boundaries

### Repository Isolation
- Each analysis session clones the repository to a unique temp directory (`/tmp/mentor-sessions/{session_id}/`)
- The Orchestrator only reads from this directory; agents receive scoped paths
- Sessions are cleaned up after completion or timeout

### Prompt Injection Defense
- Repository file contents are inserted into prompts as clearly-delimited data blocks
- System prompt explicitly instructs the model to treat file contents as untrusted data, not instructions
- Agent outputs are parsed as structured JSON, not interpreted as free instructions
- Source code strings are never concatenated directly into instruction sections of prompts

### Secret Protection
- `.env` files in the cloned repository are never read or surfaced in responses
- A blocklist prevents reading `*.env`, `*.pem`, `*secret*`, `*credential*`, `*key*` files
- Mentor's own secrets are in `.env` (backend) and never committed

### Execution Sandbox
- Tests are executed via `subprocess` with:
  - Explicit working directory set to the isolated session clone
  - `PATH` restricted to only the session virtual environment
  - Network access not blocked at OS level in MVP (acceptable for controlled demo; note as future hardening)
  - stdout/stderr captured; never directly executed or eval'd
  - Hard timeout (30s) to prevent runaway processes

### Path Traversal
- All file paths resolved through a session-scoped `PathValidator` that enforces all paths remain within the session root
- `..` traversal attempts are rejected before any file read

### Fix Application
- Diffs are presented to the user for review
- No automatic git commit; changes applied only to the working copy
- User must explicitly approve before the Fix Agent writes to disk

### Malicious Dependencies
- The demo repository uses pinned, known-good dependencies only
- For production: dependency scanning would be added before installation

---

## 10. Implementation Phases

### Phase 1 — Project Foundation
**Status:** [ ] pending

- Create `AGENTS.md` with project context for Bob
- Scaffold backend directory structure (FastAPI, no logic yet)
- Scaffold frontend directory structure (Next.js, no logic yet)
- Create `backend/.env.example`
- Create `backend/requirements.txt` (pinned versions)
- Create `frontend/package.json`
- Create `README.md`
- Create `bob_sessions/` directory with placeholder

### Phase 2 — Controlled Demo Repository
**Status:** [ ] pending

- Create `demo-repo/` with full ShopDemo application
- Implement all models, routes, services cleanly
- Implement discount code logic correctly first
- Introduce the intentional bug in a targeted, isolated location
- Write the existing test suite (missing the combined case)
- Verify the bug is deterministically reproducible
- Verify the passing tests pass and the bug scenario fails

### Phase 3 — Repository Ingestion
**Status:** [ ] pending

- Implement `repository/cloner.py`: GitHub URL → local clone via GitPython
- Implement `repository/explorer.py`: file tree walk, language detection, relevant-file identification
- Implement `repository/dependency_mapper.py`: parse requirements.txt / package.json
- Unit test: given the demo-repo path, produce correct file map

### Phase 4 — Repository Understanding
**Status:** [ ] pending

- Implement `repository/ast_analyzer.py`: Python AST analysis (function list, call graph, import graph)
- Implement basic tree-sitter analysis for TypeScript files
- Integrate with Repository Explorer agent output
- Unit test: given checkout_service.py, produce correct call graph

### Phase 5 — Architecture Explorer
**Status:** [ ] pending

- Implement `agents/architecture_agent.py`
- Build prompt template `prompts/repo_explore.j2`
- Implement AI Provider interface + first concrete provider
- Test: Architecture Agent produces correct component map for demo-repo

### Phase 6 — AI Mentor (Orchestrator + Base Agent)
**Status:** [ ] pending

- Implement `orchestrator/orchestrator.py` state machine
- Implement `orchestrator/evidence_store.py`
- Implement `agents/base_agent.py` abstract class
- Implement `orchestrator/state_machine.py` transitions
- Implement SSE streaming endpoint `api/stream.py`
- Test: Orchestrator transitions through states correctly

### Phase 7 — Bug Investigation Agents
**Status:** [ ] pending

- Implement `agents/repo_explorer.py`
- Implement `agents/bug_investigator.py` with parallel branches via asyncio.gather
- Build prompt templates for investigation
- Test: Bug Investigator on demo-repo produces hypotheses that include the actual root cause

### Phase 8 — Evidence Synthesis and Root Cause
**Status:** [ ] pending

- Implement hypothesis ranking logic in Bug Investigator
- Implement evidence type enforcement (OBSERVATION / HYPOTHESIS)
- Implement evidence summary in Orchestrator
- Test: Root cause hypothesis is ranked #1 for the demo bug

### Phase 9 — Fix Proposal and Reviewable Diff
**Status:** [ ] pending

- Implement `agents/fix_agent.py`
- Implement diff generation (unified diff format)
- Build fix proposal prompt template
- Implement human approval gate in Orchestrator (suspend workflow until approved)
- Test: Fix Agent produces correct minimal diff for demo bug

### Phase 10 — Test Generation
**Status:** [ ] pending

- Implement `agents/test_agent.py`
- Build test generation prompt template
- Implement coverage gap detection (compare existing tests vs. execution path)
- Test: Test Agent generates a regression test that would catch the demo bug

### Phase 11 — Verification
**Status:** [ ] pending

- Implement `executor/sandbox.py`
- Implement `agents/verification_agent.py`
- Wire exit code → VERIFIED RESULT evidence items
- Test: Verification Agent correctly reports pass after fix is applied

### Phase 12 — Complete Agentic Workflow
**Status:** [ ] pending

- Wire all agents through the Orchestrator end-to-end
- Run the full demo scenario: bug report → verified fix
- Fix integration issues
- Ensure evidence types are correctly propagated throughout

### Phase 13 — UI / Demo Polish
**Status:** [ ] pending

- Implement frontend: `RepoInput`, `AgentTimeline`, `EvidenceCard`, `DiffViewer`, `HypothesisList`, `TestResults`, `ApprovalGate`
- Implement SSE consumer hook `useSSE.ts`
- Style with Tailwind: clear visual distinction of evidence types
- Final demo flow walkthrough

### Phase 14 — Impact Measurement
**Status:** [ ] pending

- Time the full automated workflow end-to-end
- Count agent steps, files read, hypotheses generated
- Manually perform the same task (find bug, fix, write test) and record time + steps
- Record actual observed values only — no invented numbers

### Phase 15 — Bob Evidence and Final Submission
**Status:** [ ] pending

- Populate `bob_sessions/` with actual session screenshots/summaries
- Update `AGENTS.md` with final project context
- Write final `README.md` with submission narrative
- Record Bob usage evidence for all phases
- Final review

---

## 11. IBM Bob Usage at Each Phase

| Phase | Bob Mode | How Bob Is Used |
|---|---|---|
| Phase 1 | **Plan mode** | Generate this plan; review and confirm architecture before any code is written |
| Phase 1 | **Agent mode** | Scaffold directory structure, create config files, AGENTS.md |
| Phase 2 | **Agent mode** | Build the demo repository application code and intentional bug |
| Phase 2 | **Agent mode** | Bob reviews the bug design to confirm it is traceable and testable |
| Phase 3–4 | **Agent mode** | Implement repository analysis modules |
| Phase 3–4 | **Explore subagent** | Explore existing Python AST / tree-sitter patterns before implementing |
| Phase 5–6 | **Plan mode** | Review agent prompt design before implementation |
| Phase 5–6 | **Agent mode** | Implement Orchestrator and base agent framework |
| Phase 7 | **Agent mode** | Implement Bug Investigator with parallel asyncio branches |
| Phase 7 | **Parallel subagents** | Explore multiple agent patterns and hypothesis-generation approaches simultaneously |
| Phase 8 | **Agent mode** | Implement evidence synthesis; Bob reviews evidence type enforcement |
| Phase 9 | **Agent mode** | Implement Fix Agent; Bob performs code review of the diff generation logic |
| Phase 10 | **Agent mode** | Implement Test Agent; Bob verifies the generated tests are correct |
| Phase 11 | **Agent mode** | Implement Verification Agent; Bob reviews sandbox security |
| Phase 12 | **Agent mode** | Integration pass; Bob runs the full workflow and reviews output |
| Phase 13 | **Agent mode** | Build frontend components; Bob reviews UI evidence-type labeling |
| Phase 14 | **Agent mode** | Bob helps design the measurement script |
| Phase 15 | **Plan mode** | Final review of submission materials |
| All phases | **AGENTS.md** | Bob reads project context at the start of every session |
| All phases | **Code review** | Bob reviews each agent's prompt templates for prompt injection risk |
| All phases | **Verification** | Bob runs tests after each implementation phase |

### Key Bob Workflow Demonstrations

**Multi-step sequential workflow (Phases 5–12):**
Bob manages each phase as a sub-task: reads the plan, implements, runs tests, marks the phase done, confirms with the user, moves to the next phase. This demonstrates Bob managing a multi-phase software engineering project rather than individual one-shot prompts.

**Parallel subagent investigation (Phase 7):**
Bob spawns parallel Explore subagents to simultaneously research: (a) asyncio parallel agent patterns, (b) hypothesis-ranking approaches, (c) existing Python call-graph libraries. Results are synthesized before implementation begins.

**Document understanding (Phase 1 / Phase 15):**
Bob reads this plan file at the start of each session to maintain full context across sessions.

**Evidence-based code review (Phase 9, 10, 11):**
Bob reviews diffs and test output — treating its own reviews as OBSERVATIONS, never as VERIFIED RESULTS until tests actually pass.

---

## 12. Impact Measurement

We will measure the following with **actual observed values** (no invented numbers):

| Metric | Manual Baseline | Automated (Mentor) | Method |
|---|---|---|---|
| Time from bug report to identified root cause | Timed manually | Timed automatically | Stopwatch / session timestamps |
| Files manually opened/read | Counted manually | Count from Explorer agent log | Manual count vs. agent file-read log |
| Number of investigation steps | Counted manually | Count from agent evidence items | Manual step count vs. evidence store count |
| Time to write regression test | Timed manually | Timed automatically | Stopwatch vs. session timestamps |
| Verification time (test execution) | Timed manually | Timed automatically | Stopwatch vs. session timestamps |
| Number of investigation iterations | Counted manually | Count of hypothesis branches | Manual vs. parallel branch count |

**Measurement protocol:**
1. A developer unfamiliar with the demo-repo is given the bug report and timed manually performing the full workflow
2. The same workflow is run through the Mentor and all session timestamps are recorded
3. Actual values are compared and reported — with explicit acknowledgment that the controlled demo environment makes the automated path faster by design

---

## 13. Major Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| LLM produces hallucinated file paths | Medium | High | All file paths validated against actual filesystem before use |
| LLM promotes hypothesis to fact | Medium | High | Enforced evidence type system; UI badges prevent ambiguity |
| Parallel asyncio branches produce conflicting hypotheses | Low | Medium | Synthesis step always selects highest-confidence with evidence |
| Sandbox escape in demo execution | Low | Medium | Isolated temp directory; hard timeout; acceptable for demo |
| Demo bug is too obvious / found without agents | Low | High | Bug is in the interaction between two modules, not in a single obvious location |
| Demo bug is too obscure / agents miss it | Low | High | Bug is in a direct execution path that the Architecture Agent will trace |
| Prompt injection from demo repo source files | Low | High | Data delimiter enforcement in all prompts; structured output parsing |
| watsonx.ai API latency makes demo slow | Medium | Medium | Streaming UI with per-step updates makes wait time feel productive |
| Session scope creep (over-engineering) | Medium | High | Strict MVP boundary enforced; vector DB explicitly excluded |
| Bob session evidence insufficient for judging | Low | High | `bob_sessions/` directory planned from Phase 1; document continuously |

---

## 14. Recommended Phase 1 Implementation Tasks

The first implementation task (to be done in Agent mode after this plan is approved) is:

**Task: Project Foundation**

1. Create `AGENTS.md` in the workspace root — include project name, goal, tech stack, directory layout, and agent responsibilities so Bob has full context at the start of every future session
2. Create `README.md` — project overview and demo scenario description
3. Scaffold `backend/` — empty FastAPI project with `pyproject.toml`, `requirements.txt` (pinned), `.env.example`, and the full `app/` directory structure (empty `__init__.py` files)
4. Scaffold `frontend/` — empty Next.js project with `package.json`, `tsconfig.json`, `tailwind.config.ts`, `next.config.ts`, and the full `src/` directory structure (empty placeholder files)
5. Create `bob_sessions/` with a `README.md` explaining its purpose
6. Create `demo-repo/` as an empty placeholder directory with a `README.md` describing its planned contents

All files created should be minimal scaffolding only — no application logic.

---

*Plan authored in IBM Bob Plan mode. Workspace confirmed empty before planning. No implementation has been performed.*
