"""
Phase 8 — the two synthesis stages as the orchestrator drives them.

The synthesizer tests in ``test_evidence_synthesizer.py`` cover the rules.  This
file covers the *wiring*: that each stage owns the right workflow state, that
its work is persisted where the next stage and the API can find it, that the
Phase 8 events are emitted in order, and that a missing prerequisite is reported
as a failure instead of quietly producing a weaker answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.root_cause import RootCauseStatus
from app.models.workflow import (
    RunStatus,
    StageOutcome,
    WorkflowEventType,
    WorkflowState,
)
from app.services.orchestrator import MentorOrchestrator
from app.services.root_cause_stage import (
    EVIDENCE_SYNTHESIS_KEY,
    ROOT_CAUSE_KEY,
    ROOT_CAUSE_SYNTHESIZED_AT_KEY,
    EvidenceSynthesisStage,
    RootCauseAnalysisStage,
    load_root_cause,
    load_synthesis,
)

ISSUE = "Checkout is failing when a discount code is applied."


@pytest.fixture
def shop_repo(tmp_path: Path) -> Path:
    """A repository with the demo defect and a test that imports the module."""
    repo = tmp_path / "ShopRepo"
    (repo / "app" / "routes").mkdir(parents=True)
    (repo / "app" / "services").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)

    (repo / "app" / "routes" / "discount.py").write_text(
        "from fastapi import APIRouter\n"
        "from app.services.discount_service import calculate_discount_cents\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        '@router.post("/apply")\n'
        "def apply_discount(body):\n"
        "    return {\"discount\": calculate_discount_cents(body.cents, body.code)}\n",
        encoding="utf-8",
    )
    (repo / "app" / "services" / "discount_service.py").write_text(
        "class Code:\n"
        "    value = 10\n"
        "\n"
        "\n"
        "def calculate_discount_cents(subtotal_cents, code):\n"
        "    return int(subtotal_cents / 100) * code.value\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_discount.py").write_text(
        "from app.services.discount_service import calculate_discount_cents\n"
        "\n"
        "\n"
        "def test_discount():\n"
        "    assert calculate_discount_cents(1000, None) is not None\n",
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def orchestrator() -> MentorOrchestrator:
    return MentorOrchestrator()


@pytest.fixture
def walked(orchestrator: MentorOrchestrator, shop_repo: Path):
    run = orchestrator.create_run(str(shop_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    return orchestrator.get_run(run.run_id)


# ── Stage ownership ───────────────────────────────────────────────────────────


def test_each_phase_8_stage_owns_its_own_state() -> None:
    """A stage declares ``next_state = self.state``; a stage may not steer."""
    assert EvidenceSynthesisStage().state is WorkflowState.EVIDENCE_PENDING
    assert RootCauseAnalysisStage().state is WorkflowState.ROOT_CAUSE_ANALYSIS


def test_a_run_advances_through_both_phase_8_states(
    walked,
) -> None:
    """Phase 8's two states are real and are passed through in order.

    Since Phase 9 the walk continues past ``ROOT_CAUSE_READY``, so this asserts
    the *position* of the run rather than pinning it to the old Phase 8
    boundary: the run must have left both Phase 8 states behind it.
    """
    from app.services.state_machine import LINEAR_PROGRESSION

    position = LINEAR_PROGRESSION.index(walked.current_state)
    assert position >= LINEAR_PROGRESSION.index(WorkflowState.ROOT_CAUSE_READY)
    assert walked.status is RunStatus.PENDING


# ── Persistence ───────────────────────────────────────────────────────────────


def test_synthesis_is_persisted_where_the_next_stage_can_find_it(walked) -> None:
    synthesis = load_synthesis(walked)
    assert synthesis is not None
    assert synthesis.raw_evidence_count > 0
    assert synthesis.finding_count > 0
    # Structural facts, not just a blob of text.
    assert synthesis.numeric_sites


def test_analysis_is_persisted_and_round_trips(walked) -> None:
    analysis = load_root_cause(walked)
    assert analysis is not None
    assert analysis.run_id == walked.run_id
    assert analysis.status is RootCauseStatus.DETERMINED
    assert analysis.root_cause_kind is not None
    # The round trip must not lose or invent anything.
    assert analysis.to_summary() == walked.context[ROOT_CAUSE_KEY]


def test_the_wall_clock_lives_outside_the_analysis(walked) -> None:
    """Two runs on identical evidence must compare equal; only the stamp differs."""
    assert walked.context[ROOT_CAUSE_SYNTHESIZED_AT_KEY]
    stored = walked.context[ROOT_CAUSE_KEY]
    assert ROOT_CAUSE_SYNTHESIZED_AT_KEY not in stored
    # No timestamp field smuggled into the model either.
    assert "synthesized_at" not in stored
    assert "timestamp" not in stored


def test_a_missing_analysis_reads_as_absent_not_as_an_error(
    orchestrator: MentorOrchestrator, shop_repo: Path
) -> None:
    """Before the analysis stage runs there is simply nothing to read."""
    run = orchestrator.create_run(str(shop_repo), ISSUE)
    for _ in range(4):  # stop after the synthesis stage, before the analysis
        orchestrator.advance(run.run_id)
    current = orchestrator.get_run(run.run_id)
    assert current.current_state is WorkflowState.EVIDENCE_PENDING
    assert load_synthesis(current) is not None  # the prerequisite did run
    assert load_root_cause(current) is None


def test_a_corrupt_summary_reads_as_absent(walked) -> None:
    walked.context[ROOT_CAUSE_KEY] = {"not": "a summary"}
    assert load_root_cause(walked) is None
    walked.context[EVIDENCE_SYNTHESIS_KEY] = "garbage"
    assert load_synthesis(walked) is None


# ── Events ────────────────────────────────────────────────────────────────────


def test_phase_8_events_are_emitted_in_order(
    orchestrator: MentorOrchestrator, shop_repo: Path
) -> None:
    run = orchestrator.create_run(str(shop_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    types = [e.event_type for e in orchestrator.list_events(run.run_id)]
    ordered = [
        WorkflowEventType.EVIDENCE_SYNTHESIS_STARTED,
        WorkflowEventType.EVIDENCE_SYNTHESIS_COMPLETED,
        WorkflowEventType.ROOT_CAUSE_ANALYSIS_STARTED,
        WorkflowEventType.ROOT_CAUSE_ANALYSIS_COMPLETED,
    ]
    positions = [types.index(t) for t in ordered]
    assert positions == sorted(positions)
    # Each substep is announced before its stage completes.
    for start, done in (
        (ordered[0], ordered[1]),
        (ordered[2], ordered[3]),
    ):
        assert types.index(start) < types.index(done)


def test_phase_8_events_carry_no_repository_content(
    orchestrator: MentorOrchestrator, shop_repo: Path
) -> None:
    """Event payloads summarise counts; they must not embed file contents."""
    run = orchestrator.create_run(str(shop_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    phase8 = [
        e
        for e in orchestrator.list_events(run.run_id)
        if e.event_type
        in (
            WorkflowEventType.EVIDENCE_SYNTHESIS_STARTED,
            WorkflowEventType.EVIDENCE_SYNTHESIS_COMPLETED,
            WorkflowEventType.ROOT_CAUSE_ANALYSIS_STARTED,
            WorkflowEventType.ROOT_CAUSE_ANALYSIS_COMPLETED,
        )
    ]
    assert phase8
    for event in phase8:
        blob = repr(event.payload)
        assert "calculate_discount_cents(" not in blob
        assert "APIPASSWORD" not in blob.upper()


def test_synthesis_events_report_counts(
    orchestrator: MentorOrchestrator, shop_repo: Path
) -> None:
    run = orchestrator.create_run(str(shop_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    completed = next(
        e
        for e in orchestrator.list_events(run.run_id)
        if e.event_type is WorkflowEventType.EVIDENCE_SYNTHESIS_COMPLETED
    )
    assert completed.payload["raw_evidence_count"] > 0
    assert completed.payload["finding_count"] > 0
    # Nothing may claim to have been verified.
    assert completed.payload["rejected_count"] == 0


def test_analysis_event_reports_the_verdict_without_claiming_execution(
    orchestrator: MentorOrchestrator, shop_repo: Path
) -> None:
    run = orchestrator.create_run(str(shop_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    completed = next(
        e
        for e in orchestrator.list_events(run.run_id)
        if e.event_type is WorkflowEventType.ROOT_CAUSE_ANALYSIS_COMPLETED
    )
    assert completed.payload["status"] == RootCauseStatus.DETERMINED.value
    assert completed.payload["tests_executed"] is False
    assert completed.payload["partial_investigation"] is False
    assert 0.0 <= completed.payload["confidence"] <= 1.0


# ── Honesty ───────────────────────────────────────────────────────────────────


def test_phase_8_contributes_no_evidence_of_its_own(walked) -> None:
    """Phase 8 only reads Phase 7 evidence, so the evidence store is unchanged."""
    assert walked.evidence
    assert all(e.agent != "evidence_synthesizer" for e in walked.evidence)
    assert all(e.agent != "root_cause_analysis" for e in walked.evidence)


def test_no_verified_result_ever_appears(walked) -> None:
    assert all(e.evidence_type.value != "VERIFIED_RESULT" for e in walked.evidence)
    analysis = load_root_cause(walked)
    assert analysis is not None
    assert analysis.tests_executed is False
    assert analysis.verified_result_count == 0


def test_conclusion_cites_evidence_that_is_actually_stored(walked) -> None:
    analysis = load_root_cause(walked)
    assert analysis is not None
    stored = {e.id for e in walked.evidence}
    cited = {ref.evidence_id for ref in analysis.supporting_evidence}
    assert cited
    assert cited <= stored


def test_the_anchor_points_at_a_real_file_and_line(walked, shop_repo: Path) -> None:
    analysis = load_root_cause(walked)
    assert analysis is not None
    anchor = analysis.anchor
    assert anchor is not None
    source = shop_repo / anchor.file_path
    assert source.is_file()
    lines = source.read_text(encoding="utf-8").splitlines()
    assert 1 <= anchor.line <= len(lines)
    assert anchor.excerpt in lines[anchor.line - 1]


def test_the_identified_line_really_contains_the_defect(walked) -> None:
    """The reported line must match the known ground truth of the demo defect."""
    analysis = load_root_cause(walked)
    assert analysis is not None
    assert analysis.anchor is not None
    assert analysis.anchor.file_path == "app/services/discount_service.py"
    assert "int(subtotal_cents / 100) * code.value" in (analysis.anchor.excerpt or "")


# ── Containment ───────────────────────────────────────────────────────────────


def test_phase_8_does_not_modify_the_repository(
    orchestrator: MentorOrchestrator, shop_repo: Path
) -> None:
    before = sorted(
        (str(p.relative_to(shop_repo)), p.read_bytes())
        for p in shop_repo.rglob("*")
        if p.is_file()
    )
    run = orchestrator.create_run(str(shop_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    after = sorted(
        (str(p.relative_to(shop_repo)), p.read_bytes())
        for p in shop_repo.rglob("*")
        if p.is_file()
    )
    assert before == after


def test_a_missing_synthesis_fails_the_stage_loudly(
    orchestrator: MentorOrchestrator, shop_repo: Path
) -> None:
    """A broken hand-off must not yield a quietly weaker conclusion."""
    from app.services.stages import StageContext

    run = orchestrator.create_run(str(shop_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    final = orchestrator.get_run(run.run_id)
    # Strip the prerequisite and any earlier verdict, then re-run the analysis.
    final.context.pop(EVIDENCE_SYNTHESIS_KEY)
    final.context.pop(ROOT_CAUSE_KEY, None)
    emitted: list[tuple[str, str, dict]] = []
    result = RootCauseAnalysisStage().execute(
        StageContext(
            run=final,
            emit=lambda t, m, p: emitted.append((t, m, p)),
        )
    )
    assert result.outcome is StageOutcome.FAILED
    assert result.error
    # It announced itself first, then reported the failure.
    assert [t for t, _m, _p in emitted] == [
        WorkflowEventType.ROOT_CAUSE_ANALYSIS_STARTED.value,
        WorkflowEventType.ROOT_CAUSE_ANALYSIS_FAILED.value,
    ]
    assert "EVIDENCE_PENDING" in emitted[-1][1]
    # And no analysis is recorded for the failed attempt.
    assert ROOT_CAUSE_KEY not in final.context
