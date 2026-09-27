"""
Test plan — Phase 10.

One stage, at the state Phase 10 owns:

``TEST_PLAN``
    :class:`TestPlanStage` — read the Phase 8 diagnosis and Phase 7's recorded
    coverage evidence, and record what a regression test would have to do.

What this stage can and cannot reach
-------------------------------------
Nothing.  That is not a limitation of the implementation, it is the design:
Phase 10 has no repository access at all, not even the single constrained read
Phase 9 was granted.  Every fact it uses is already on the run, which is what
makes the plan reproducible from stored context alone — and what means a client
cannot use it as a way to make the server touch a repository.

The stage is also *unreachable in a normal run*, because ``FIX_PROPOSAL_READY``
holds the human approval gate and nothing may pass it server-side.  That is
correct rather than inconvenient: a regression test should be outlined by
somebody who has looked at the diff and agreed the diagnosis is the right one.
The stage is therefore exercised directly by tests rather than end to end, and the
gate itself has its own test asserting it is still a placeholder.

Evidence
--------
Every item this stage emits is ``PROPOSED_ACTION``.  Not one ``OBSERVATION``,
and that is the load-bearing decision: the coverage gaps are Phase 7
*hypotheses* with their own confidence scores, and re-emitting them here as
observations would quietly promote an inference to a fact.  The gaps are
therefore *copied* into the plan with the evidence IDs they came from, and the
scenarios that act on them are labelled as proposals.  ``VERIFIED_RESULT`` is
impossible: no member of :class:`app.models.test_plan.TestExecutionStatus` can
express one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.analysis.test_planner import DEFAULT_TEST_PLANNER, TestPlanner
from app.models.domain import Evidence, EvidenceType
from app.models.test_plan import (
    TestExecutionStatus,
    TestFileStatus,
    TestPlan,
    TestPlanStatus,
)
from app.models.workflow import StageResult, WorkflowState, utc_now
from app.services.fix_proposal_stage import load_fix_proposal
from app.services.root_cause_stage import load_root_cause
from app.services.stages import StageContext

logger = logging.getLogger(__name__)

#: ``run.context`` key holding the Phase 10 plan.
TEST_PLAN_KEY = "test_plan"

#: ``run.context`` key holding when the plan was recorded.  Kept out of the model
#: for the Phase 8/9 reason: a wall-clock value inside the model would make two
#: identical plans compare unequal, breaking replay.
TEST_PLAN_CREATED_AT_KEY = "test_plan_created_at"

#: ``agent`` name on the evidence this stage produces.
PLANNER_AGENT = "test_planner"


class TestPlanStage:
    """Records a coverage-gap analysis and test plan. Registered for ``TEST_PLAN``.

    An honest "I cannot responsibly plan a test here" is a *completed* stage with
    an ``INSUFFICIENT_EVIDENCE`` or ``INVALID`` plan, not a failure: a reviewer
    still needs to be told that no plan exists and why.  Only an unexpected error
    fails the run.

    A *missing* diagnosis, by contrast, is a stage failure rather than an
    ``INSUFFICIENT_EVIDENCE`` plan.  Reaching this state without one means the
    workflow was driven out of order, and reporting a wiring fault as a considered
    opinion about the repository would be the same mistake Phase 9's gate avoids.
    """

    state = WorkflowState.TEST_PLAN

    #: This is a workflow stage, not a pytest class.  Without this, pytest tries
    #: to collect it by name and warns about its constructor — the same reason
    #: ``TestInvestigator`` carries the attribute.
    __test__ = False

    def __init__(self, planner: TestPlanner | None = None) -> None:
        self._planner = planner or DEFAULT_TEST_PLANNER

    def execute(self, context: StageContext) -> StageResult:
        run = context.run
        emit = context.emit

        if emit is not None:
            emit(
                "TEST_PLAN_STARTED",
                (
                    f"Planning a regression test for run {run.run_id}. The plan is "
                    f"derived from the recorded evidence only: no file is read, "
                    f"written or executed."
                ),
                {"run_id": run.run_id, "issue": run.issue},
            )

        analysis = load_root_cause(run)
        if analysis is None:
            return self._fail(
                emit,
                run.run_id,
                "No root-cause analysis is recorded for this run. "
                "ROOT_CAUSE_ANALYSIS must run before TEST_PLAN.",
            )

        try:
            plan = self._planner.plan(
                run_id=run.run_id,
                issue=run.issue,
                analysis=analysis,
                proposal=load_fix_proposal(run),
                evidence=list(context.evidence or []),
            )
            if plan is None:
                # A planner contract violation, not a repository problem.  Named
                # explicitly so the failure says which half is broken instead of
                # surfacing as an ``AttributeError`` three frames later.
                return self._fail(
                    emit,
                    run.run_id,
                    f"{type(self._planner).__name__}.plan returned None instead of "
                    "a TestPlan; no plan was recorded.",
                )
            # Reference only.  Deliberately not part of the plan model.
            created_at = datetime.now(timezone.utc).isoformat()
            evidence = _scenario_evidence(run.run_id, plan)
            summary = plan.to_summary()
            message = _plan_message(plan)
        except Exception as exc:  # noqa: BLE001 - reported, never silently ignored
            logger.exception("Test planning failed for run %s", run.run_id)
            return self._fail(emit, run.run_id, f"{type(exc).__name__}: {exc}")

        if emit is not None:
            emit(
                "TEST_PLAN_COMPLETED",
                message,
                {
                    "run_id": run.run_id,
                    "status": plan.status.value,
                    "gap_count": len(plan.coverage_gaps),
                    "scenario_count": len(plan.scenarios),
                    "root_cause_kind": (
                        plan.root_cause_kind.value if plan.root_cause_kind else None
                    ),
                    "execution_status": plan.execution_status.value,
                    "tests_executed": False,
                    "test_source_written": False,
                },
            )

        return StageResult.completed(
            message,
            next_state=self.state,
            evidence=evidence,
            **{
                TEST_PLAN_KEY: summary,
                TEST_PLAN_CREATED_AT_KEY: created_at,
                "test_plan_status": plan.status.value,
                "test_plan_gap_count": len(plan.coverage_gaps),
                "test_plan_scenario_count": len(plan.scenarios),
                "test_plan_execution_status": plan.execution_status.value,
            },
        )

    def _fail(self, emit, run_id: str, error: str) -> StageResult:
        if emit is not None:
            emit(
                "TEST_PLAN_FAILED",
                f"Test planning failed for run {run_id}: {error}",
                {"run_id": run_id, "error": error},
            )
        return StageResult.failed("A test plan could not be produced.", error)


# ── Persistence helpers ───────────────────────────────────────────────────────


def load_test_plan(run) -> TestPlan | None:
    """Return the plan stored for ``run``, or None if absent/unparseable.

    A malformed plan is reported as ``None`` so a read-only endpoint can say "not
    available" instead of raising a 500 on a client that only wants to look.
    """
    raw = run.context.get(TEST_PLAN_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return TestPlan.from_summary(run.run_id, raw)
    except Exception as exc:  # noqa: BLE001 - a bad summary must not crash a read
        logger.warning("Test plan for run %s could not be parsed: %s", run.run_id, exc)
        return None


# ── Evidence ──────────────────────────────────────────────────────────────────


def _scenario_evidence(run_id: str, plan: TestPlan) -> list[Evidence]:
    """One ``PROPOSED_ACTION`` per outlined scenario.

    Only ``PROPOSED_ACTION``, and the type is set explicitly rather than inferred
    from the plan's status: a client filtering the evidence feed by type would
    otherwise have to know this stage's internals to tell a proposal from a
    finding.  ``execution_status`` travels in the metadata so the item itself
    cannot be read as a result.
    """
    items: list[Evidence] = []
    for scenario in plan.scenarios:
        items.append(
            Evidence(
                session_id=run_id,
                evidence_type=EvidenceType.PROPOSED_ACTION,
                agent=PLANNER_AGENT,
                title=(
                    f"Outline a regression test: {scenario.name} "
                    f"({scenario.suggested_file})"
                ),
                description=(
                    f"{scenario.purpose} Suggested location: "
                    f"{scenario.suggested_file} "
                    + (
                        "(an existing test file)"
                        if scenario.file_status is TestFileStatus.EXISTING
                        else "(a new file, not created)"
                    )
                    + f". Expected behaviour: {scenario.expected_behavior} "
                    "This is a proposal: no test source was written and no test "
                    "was executed."
                ),
                source_file=scenario.suggested_file,
                confidence=None,
                timestamp=utc_now(),
                metadata={
                    "investigator": PLANNER_AGENT,
                    "method": "test_planning",
                    "phase": 10,
                    "scenario_id": scenario.id,
                    "target_function": scenario.target_function,
                    "file_status": scenario.file_status.value,
                    "covers_gaps": list(scenario.covers_gaps),
                    "execution_status": plan.execution_status.value,
                    "tests_executed": False,
                },
            )
        )
    return items


# ── Message helpers ───────────────────────────────────────────────────────────


def _plan_message(plan: TestPlan) -> str:
    if plan.status is not TestPlanStatus.READY_FOR_REVIEW:
        return (
            f"No test plan produced ({plan.status.value}). {plan.rationale} "
            f"Execution status is {plan.execution_status.value}."
        )
    files = sorted({s.suggested_file for s in plan.scenarios})
    return (
        f"Test plan recorded: {len(plan.scenarios)} scenario(s) across "
        f"{len(plan.coverage_gaps)} coverage gap(s), to be written in "
        f"{', '.join(files)}. The plan is prose only — no test source was "
        f"written, no test was executed, and execution status is "
        f"{TestExecutionStatus.NOT_RUN.value}."
    )


__all__ = [
    "PLANNER_AGENT",
    "TEST_PLAN_CREATED_AT_KEY",
    "TEST_PLAN_KEY",
    "TestPlanStage",
    "load_test_plan",
]
