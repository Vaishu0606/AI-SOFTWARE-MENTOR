"""
Test-plan domain types — Phase 10 (coverage-gap analysis and regression-test plan).

These are the *domain* half of Phase 10.  Pure Pydantic models with no I/O, no
clock and no LLM dependency, following the convention established by
``app.models.domain``, ``app.models.workflow``, ``app.models.investigation``,
``app.models.root_cause`` and ``app.models.fix_proposal``.

What Phase 10 actually delivers
-------------------------------
Phase 10 is named "Test Generation" in the roadmap, and it is worth being precise
about what this module can hold, because the gap between the name and the artefact
is exactly where a demo would start lying.

It delivers a **plan**: which existing coverage gaps relate to the diagnosed
defect, and what a regression test for it would have to do — where it would live,
what it would set up, and what behaviour it would assert.  It does **not** deliver
test source code, and the reason is not a missing feature but an epistemic one.
Generated test code would be a plausible-looking artefact that nobody has run.
Handing that to a developer as "your regression test" invites exactly the
confusion this project exists to prevent: a test that has never executed cannot
demonstrate that it catches anything. A plan can be reviewed on its merits and
written by a human who then runs it, which is the first point at which anything
can honestly be claimed.

The three honesty rules this model enforces
-------------------------------------------
* **A plan is not a result.**  ``execution_status`` is an enum with exactly one
  member, :attr:`TestExecutionStatus.NOT_RUN`, so there is no value this model
  could hold that would report a test as passed, failed or flaky.  A ``bool``
  would work today and be one careless edit away from lying; an enum with no
  ``PASSED`` member cannot be.  Phase 11's executor adds the members it needs,
  and it will have to add them deliberately.
* **A plan is not a written test.**  ``test_source_written`` is invariant-locked
  to ``False``.  Phase 10 writes no file, so the model is the only place the claim
  could be made, and it refuses.
* **A declined plan is a real answer.**  ``INSUFFICIENT_EVIDENCE`` and ``INVALID``
  are first-class statuses that carry no scenarios, exactly as Phase 9's
  proposal does.  A model that could only say "nothing" by leaving a list empty
  would let "I could not plan a test" render as "there is nothing to test".

Determinism
-----------
Like the Phase 8 analysis and the Phase 9 proposal, the plan carries **no
timestamp** and no random identifier: two runs that reach the same diagnosis
against the same evidence produce an identical model, so a plan can be compared,
cached and replayed.  Wall-clock information is reported separately, in the
workflow event payload and in ``run.context["test_plan_created_at"]``.

Where the inputs come from
--------------------------
Phase 10 performs **no repository access at all** — not even the constrained
single-file read Phase 9 was allowed.  Every fact it uses is already on the run:
the Phase 8 analysis says what is wrong and where, and Phase 7's Test Investigator
already recorded, as ``coverage_analysis`` and ``import_graph`` evidence, which
production files each test file imports and which concepts no test exercises
together.  Re-deriving any of it by reading the repository again would be a
second source of truth that could disagree with the evidence the analysis was
built on.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models.fix_proposal import AnchorLocation, FixProposalStatus
from app.models.root_cause import RootCauseKind

# ── Status ────────────────────────────────────────────────────────────────────


class TestPlanStatus(str, Enum):
    """What Phase 10 was able to plan.

    The negative outcomes are the interesting ones and are named so a client
    cannot collapse them into a single "no test needed":

    * ``INSUFFICIENT_EVIDENCE`` — there is nothing to pin down, or not enough
      known about the test suite to aim a scenario at anything.  The remedy is to
      investigate further.
    * ``INVALID`` — a root cause *was* determined, but it points nowhere a test
      could be aimed.  The remedy is to re-run the analysis.
    """

    #: Scenarios were outlined and are ready for a developer to write and run.
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    #: No defect was determined, or the test suite is too poorly understood to
    #: place a scenario.  No scenarios.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    #: A root cause was determined but carries no usable anchor location.  No
    #: scenarios.
    INVALID = "INVALID"

    #: This is a domain enum, not a pytest class.  Several Phase 10 types are
    #: named ``Test*`` because "test plan" is what the domain calls them, and
    #: pytest would otherwise try to collect each one and warn about its
    #: constructor.  Same reason ``TestInvestigator`` carries this attribute.
    __test__ = False


# ── Execution status ──────────────────────────────────────────────────────────


class TestExecutionStatus(str, Enum):
    """Whether anything in this plan has been executed.

    ``NOT_RUN`` is the only member in Phase 10, and that is the point.  The whole
    project rule is that ``VERIFIED_RESULT`` may only come from a real exit code
    (AGENTS.md rule 4), and this enum is the type that would have to carry it.
    Making the type incapable of expressing "passed" means the rule is enforced by
    the shape of the data rather than by remembering to set a flag correctly.
    Phase 11's sandboxed executor is the only component that may add a member.
    """

    NOT_RUN = "NOT_RUN"

    #: Not a pytest class — see ``TestPlanStatus``.
    __test__ = False


# ── Coverage gaps ─────────────────────────────────────────────────────────────


class CoverageGapKind(str, Enum):
    """The shape of the gap between the suite and the diagnosed defect.

    These mirror the three gap patterns Phase 7's Test Investigator reports, so a
    gap in a plan can always be traced back to the specific evidence item that
    produced it rather than to a fresh, unauditable judgement.
    """

    #: Two concerns named in the issue are handled by different modules and no
    #: test imports a production file unique to both sides.
    CONCEPTS_NEVER_TESTED_TOGETHER = "CONCEPTS_NEVER_TESTED_TOGETHER"
    #: A production file matching an issue term is imported by no test at all.
    RELEVANT_FILE_UNTESTED = "RELEVANT_FILE_UNTESTED"
    #: Routes exist but no test imports a module that declares one, so the
    #: request boundary is unverified.
    REQUEST_BOUNDARY_NOT_COVERED = "REQUEST_BOUNDARY_NOT_COVERED"


class CoverageGap(BaseModel):
    """A hole in the existing suite that the diagnosed defect fell through.

    ``evidence_ids`` cites the Phase 7 items this gap was read from.  A gap is
    therefore not a fresh claim about the test suite — it is a pointer to a
    recorded, confidence-scored hypothesis, and it inherits that hypothesis's
    uncertainty rather than upgrading it.  Nothing in Phase 10 re-derives whether
    the gap is real.
    """

    #: Stable, order-derived identifier (``gap-1``, ``gap-2``, …).  Derived rather
    #: than random so two runs over the same evidence name the same gaps.
    id: str
    kind: CoverageGapKind
    title: str
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    #: Issue terms the gap concerns, e.g. ``["checkout", "discount"]``.
    concepts: list[str] = Field(default_factory=list)
    #: Production modules named by the gap.
    modules: list[str] = Field(default_factory=list)
    #: Production *files* the gap is about, when the gap is about files rather
    #: than terms — the untested files of a ``RELEVANT_FILE_UNTESTED`` gap.  Kept
    #: so a scenario can be matched to a gap precisely instead of by assumption:
    #: "does this gap concern the line I am regressing?" is a question about paths,
    #: and answering it any other way would let a scenario claim credit for a gap
    #: it has nothing to do with.
    files: list[str] = Field(default_factory=list)


# ── Scenarios ─────────────────────────────────────────────────────────────────


class TestFileStatus(str, Enum):
    """Where a scenario would live.

    ``PROPOSED_NEW`` is deliberately not a promise.  It means the plan infers, from
    the paths Phase 7 observed, that the repository's convention is
    ``tests/test_<module>.py`` and that no such file exists yet.  The convention
    is an inference from observed paths, so it is recorded in the plan's
    ``assumptions`` rather than presented as a fact about the repository.
    """

    #: A test file Phase 7 recorded as importing production code.
    EXISTING = "EXISTING"
    #: A path derived from the observed naming convention.  Not created.
    PROPOSED_NEW = "PROPOSED_NEW"

    #: Not a pytest class — see ``TestPlanStatus``.
    __test__ = False


class TestScenario(BaseModel):
    """One outlined regression test, in prose.

    Deliberately *not* test source code — see the module docstring.  What it
    carries instead is everything a person needs to write the test and the two
    things that make the plan checkable:

    * ``expected_behavior`` states the *invariant* that was violated, not a
      numeric literal.  A number would be a guess about input values nobody has
      run, and a wrong one would turn a correct plan into a failing test.
    * ``discriminating_condition`` states what the input must be for the test to
      tell the diagnosed behaviour apart from correct behaviour.  Without it, a
      regression test can pass on the broken code and prove nothing — which is
      how a real suite ends up with a test that never would have caught the bug.
    """

    id: str
    name: str
    purpose: str

    #: The file this scenario belongs in, and whether it already exists.
    suggested_file: str
    file_status: TestFileStatus = TestFileStatus.PROPOSED_NEW
    #: The function under test, when Phase 8 named one.
    target_function: str | None = None

    preconditions: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    expected_behavior: str = ""
    #: What the input must be for this scenario to distinguish the diagnosed
    #: behaviour from correct behaviour.  ``None`` when Phase 10 cannot derive it,
    #: in which case the scenario says so rather than implying any input works.
    discriminating_condition: str | None = None
    #: IDs of the :class:`CoverageGap` items this scenario addresses.
    covers_gaps: list[str] = Field(default_factory=list)
    notes: str = ""

    #: Not a pytest class — see ``TestPlanStatus``.
    __test__ = False

    @model_validator(mode="after")
    def _a_scenario_must_be_actionable(self) -> "TestScenario":
        if not self.expected_behavior:
            raise ValueError(
                "a test scenario must state the behaviour it asserts; a scenario "
                "with no expectation cannot be reviewed"
            )
        if not self.steps:
            raise ValueError(
                f"scenario {self.id!r} must list at least one step"
            )
        return self


# ── The plan ──────────────────────────────────────────────────────────────────


class TestPlan(BaseModel):
    """Phase 10's plan for one run.

    Invariants, re-checked here rather than trusted from the planner:

    * ``status is READY_FOR_REVIEW`` if and only if ``scenarios`` is non-empty
      and an ``anchor`` is present.  A ready plan with nothing to do, or scenarios
      aimed at nothing, is always a bug.
    * A non-ready plan has no scenarios.  It may still carry gaps: a gap is a
      recorded finding, not a proposal, and dropping gaps that were genuinely
      found would lose information for no honesty gain.  What a declined plan
      must never carry is a scenario, because a scenario is the actionable part.
    * ``execution_status`` is ``NOT_RUN`` and ``test_source_written`` is ``False``.
    """

    run_id: str
    issue: str
    status: TestPlanStatus
    rationale: str

    # ── Traceability ────────────────────────────────────────────────────
    #: Where the diagnosed defect is, reduced to a location.  Reuses Phase 9's
    #: :class:`AnchorLocation` rather than a new type, so a plan can never
    #: accidentally reintroduce a quoted source excerpt.
    anchor: AnchorLocation | None = None
    root_cause_kind: RootCauseKind | None = None
    root_cause_confidence: float = 0.0
    #: The Phase 9 proposal's status, for context.  ``None`` when no proposal was
    #: recorded.  A plan does not require a fix to be valid: a regression test is
    #: worth writing precisely *before* the fix exists, because it is what proves
    #: the fix worked.
    fix_proposal_status: FixProposalStatus | None = None

    # ── The plan ────────────────────────────────────────────────────────
    coverage_gaps: list[CoverageGap] = Field(default_factory=list)
    scenarios: list[TestScenario] = Field(default_factory=list)

    #: Always ``NOT_RUN`` in Phase 10.  See :class:`TestExecutionStatus`.
    execution_status: TestExecutionStatus = TestExecutionStatus.NOT_RUN
    #: Always ``False`` in Phase 10.  Locked by the validator below, because this
    #: is the single field through which a plan could claim a file exists that it
    #: never wrote.
    test_source_written: bool = False

    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    # ── Invariants ──────────────────────────────────────────────────────
    #: Not a pytest class — see ``TestPlanStatus``.
    __test__ = False

    @model_validator(mode="after")
    def _status_matches_content(self) -> "TestPlan":
        if self.test_source_written:
            raise ValueError(
                "test_source_written must be False: Phase 10 emits a plan and "
                "writes no file, so a True here would report a test that does "
                "not exist"
            )
        if self.execution_status is not TestExecutionStatus.NOT_RUN:
            raise ValueError(
                "Phase 10 executes nothing; only a real test run may set an "
                "execution status"
            )

        if self.status is TestPlanStatus.READY_FOR_REVIEW:
            if not self.scenarios:
                raise ValueError(
                    "READY_FOR_REVIEW requires at least one scenario; use "
                    "INSUFFICIENT_EVIDENCE or INVALID for 'nothing to plan'"
                )
            if self.anchor is None:
                raise ValueError(
                    "READY_FOR_REVIEW requires an anchor: a scenario with no "
                    "diagnosed defect to target is not a regression test"
                )
        elif self.scenarios:
            raise ValueError(
                f"status {self.status.value} must carry no scenarios: a scenario "
                f"is the actionable part, and a declined plan has none to offer"
            )
        return self

    # ── Persistence helpers ─────────────────────────────────────────────
    #
    # Stored in ``MentorRun.context`` alongside the Phase 8 analysis and the
    # Phase 9 proposal.  The model is timestamp-free by design, so this is a
    # total, lossless representation and re-reading it yields an identical
    # object.

    def to_summary(self) -> dict[str, Any]:
        """Serialise the plan for ``run.context``."""
        return self.model_dump(mode="json")

    @classmethod
    def from_summary(cls, run_id: str, summary: dict[str, Any]) -> "TestPlan":
        """Rebuild a plan previously written by :meth:`to_summary`."""
        data = dict(summary)
        data["run_id"] = run_id
        return cls.model_validate(data)

    def is_ready_for_review(self) -> bool:
        """True when there are scenarios waiting for a developer."""
        return self.status is TestPlanStatus.READY_FOR_REVIEW and bool(self.scenarios)

    def evidence_ids(self) -> list[str]:
        """Phase 7 evidence IDs cited by this plan's gaps, in citation order."""
        seen: list[str] = []
        for gap in self.coverage_gaps:
            for evidence_id in gap.evidence_ids:
                if evidence_id not in seen:
                    seen.append(evidence_id)
        return seen


__all__ = [
    "CoverageGap",
    "CoverageGapKind",
    "TestExecutionStatus",
    "TestFileStatus",
    "TestPlan",
    "TestPlanStatus",
    "TestScenario",
]
