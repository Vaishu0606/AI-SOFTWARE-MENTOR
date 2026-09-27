"""
Phase 10 tests — the coverage-gap reader and the regression-test planner.

Covers the engine rather than the stage (which ``test_test_plan_stage`` owns):
reading gaps and the test-suite layout out of Phase 7 evidence, the three ways a
plan can decline, the discriminating-condition rule, scenario selection, and the
model invariants that make a plan incapable of reporting a result.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.analysis.test_planner import (
    MAX_GAP_RATIONALE_CHARS,
    MAX_SCENARIOS,
    TestPlanner,
)
from app.models.domain import Evidence, EvidenceType
from app.models.fix_proposal import FixProposal, FixProposalStatus
from app.models.root_cause import (
    ConfidenceBreakdown,
    ConfidenceLevel,
    RootCauseAnalysis,
    RootCauseKind,
    RootCauseStatus,
    SourceAnchor,
)
from app.models.test_plan import (
    CoverageGapKind,
    TestExecutionStatus,
    TestFileStatus,
    TestPlan,
    TestPlanStatus,
    TestScenario,
)

ISSUE = "Checkout is failing when a discount code is applied."
RUN_ID = "11111111-1111-4111-8111-111111111111"
ANCHOR_FILE = "app/services/discount_service.py"

#: Distinguishes "not supplied" from an explicit ``None``, which means something
#: different for ``analysis`` and for ``anchor``.
ABSENT = object()


# ── Builders ──────────────────────────────────────────────────────────────────


def analysis(
    *,
    status: RootCauseStatus = RootCauseStatus.DETERMINED,
    kind: RootCauseKind | None = RootCauseKind.TRUNCATION_BEFORE_SCALING,
    anchor: SourceAnchor | None = ABSENT,  # type: ignore[assignment]
) -> RootCauseAnalysis:
    """A Phase 8 analysis, shaped the way the real stage persists it."""
    if anchor is ABSENT:
        anchor = SourceAnchor(
            file_path=ANCHOR_FILE,
            line=52,
            function="calculate_discount_cents",
            excerpt="return int(subtotal_cents / 100) * code.value",
        )
    determined = status in (RootCauseStatus.DETERMINED, RootCauseStatus.PARTIAL)
    return RootCauseAnalysis(
        run_id=RUN_ID,
        issue=ISSUE,
        status=status,
        # A root_cause may only be recorded alongside a qualifying status.
        root_cause="The total is truncated before it is scaled." if determined else None,
        root_cause_kind=kind,
        anchor=anchor,
        confidence=0.48,
        confidence_level=ConfidenceLevel.MEDIUM,
        confidence_rationale="One direct observation, no corroboration.",
        confidence_breakdown=ConfidenceBreakdown(
            base=0.6,
            corroboration_bonus=0.0,
            score=0.48,
            level=ConfidenceLevel.MEDIUM,
            rationale="test fixture",
        ),
    )


def evidence_item(
    *,
    evidence_id: str,
    method: str,
    evidence_type: EvidenceType,
    title: str = "t",
    description: str = "d",
    source_file: str | None = None,
    **metadata,
) -> Evidence:
    return Evidence(
        id=evidence_id,
        session_id=RUN_ID,
        evidence_type=evidence_type,
        agent="test_investigator",
        title=title,
        description=description,
        source_file=source_file,
        confidence=0.6 if evidence_type is EvidenceType.HYPOTHESIS else None,
        metadata={"method": method, **metadata},
    )


def coverage_map(test_file: str = "tests/test_discount.py", *files: str) -> Evidence:
    """An ``import_graph`` observation: this test file imports those production files."""
    return evidence_item(
        evidence_id=f"ev-import-{test_file}",
        method="import_graph",
        evidence_type=EvidenceType.OBSERVATION,
        source_file=test_file,
        files_under_test=list(files) or [ANCHOR_FILE],
    )


def untested_pair_gap(
    evidence_id: str = "ev-pair",
    concepts: list[str] | None = None,
    modules: list[str] | None = None,
) -> Evidence:
    """A ``coverage_analysis`` hypothesis: two issue terms are never tested together.

    The module list is split in half to populate ``first_modules`` /
    ``second_modules``, which is how the real Test Investigator records the two
    sides of the pair.
    """
    modules = modules or ["app.routes.checkout", "app.services.discount_service"]
    midpoint = max(1, len(modules) // 2)
    terms = concepts or ["checkout", "discount"]
    return evidence_item(
        evidence_id=evidence_id,
        method="coverage_analysis",
        evidence_type=EvidenceType.HYPOTHESIS,
        title=f"No test exercises '{terms[0]}' and '{terms[1]}' together",
        description="No test imports a production file unique to both sides.",
        concepts=terms,
        first_modules=modules[:midpoint],
        second_modules=modules[midpoint:],
    )


def untested_file_gap(
    evidence_id: str, concept: str, *files: str
) -> Evidence:
    return evidence_item(
        evidence_id=evidence_id,
        method="coverage_analysis",
        evidence_type=EvidenceType.HYPOTHESIS,
        title=f"1 file matching '{concept}' is not imported by any test",
        description="No test file imports it.",
        concept=concept,
        untested_files=list(files),
        untested_modules=[m.replace("/", ".").removesuffix(".py") for m in files],
    )


def boundary_gap(evidence_id: str, *routes: str) -> Evidence:
    return evidence_item(
        evidence_id=evidence_id,
        method="coverage_analysis",
        evidence_type=EvidenceType.HYPOTHESIS,
        title="the route layer may not be covered at the request boundary",
        description="No test imports a module that declares an HTTP route.",
        route_files=list(routes),
    )


def plan_with(
    evidence: list[Evidence],
    *,
    analysis: RootCauseAnalysis | None = ABSENT,  # type: ignore[assignment]
    proposal: FixProposal | None = None,
) -> TestPlan:
    if analysis is ABSENT:
        analysis = globals()["analysis"]()
    return TestPlanner().plan(
        run_id=RUN_ID,
        issue=ISSUE,
        analysis=analysis,
        proposal=proposal,
        evidence=evidence,
    )


def a_scenario() -> TestScenario:
    return TestScenario(
        id="scenario-1",
        name="n",
        purpose="p",
        suggested_file="tests/test_x.py",
        steps=["do the thing"],
        expected_behavior="e",
    )


# ── The happy path ────────────────────────────────────────────────────────────


def test_a_determined_defect_with_a_known_suite_yields_a_plan() -> None:
    plan = plan_with([coverage_map()])
    assert plan.status is TestPlanStatus.READY_FOR_REVIEW
    assert plan.is_ready_for_review()
    assert plan.anchor is not None
    assert plan.anchor.file_path == ANCHOR_FILE
    assert plan.anchor.line == 52
    assert plan.anchor.function == "calculate_discount_cents"
    assert len(plan.scenarios) == 1


def test_a_plan_carries_no_source_excerpt() -> None:
    """The anchor must be a location, so a plan cannot re-quote the defect."""
    plan = plan_with([coverage_map()])
    dumped = plan.model_dump(mode="json")
    assert set(dumped["anchor"]) == {"file_path", "line", "function"}
    assert "int(subtotal_cents / 100)" not in str(dumped)


def test_the_target_is_the_test_file_that_already_covers_the_anchor() -> None:
    plan = plan_with([coverage_map("tests/test_discount.py", ANCHOR_FILE)])
    scenario = plan.scenarios[0]
    assert scenario.suggested_file == "tests/test_discount.py"
    assert scenario.file_status is TestFileStatus.EXISTING
    assert plan.assumptions == []


def test_the_lowest_sorting_covering_test_file_wins() -> None:
    """A tie must not be broken by dictionary or filesystem ordering."""
    evidence = [
        coverage_map("tests/test_zzz_discount.py", ANCHOR_FILE),
        coverage_map("tests/test_aaa_discount.py", ANCHOR_FILE),
    ]
    plan = plan_with(evidence)
    assert plan.scenarios[0].suggested_file == "tests/test_aaa_discount.py"


def test_an_untested_module_produces_an_inferred_path_and_an_assumption() -> None:
    """A new test file is a guess, and the plan must say so."""
    evidence = [coverage_map("tests/test_cart.py", "app/services/cart_service.py")]
    plan = plan_with(evidence)
    scenario = plan.scenarios[0]

    assert scenario.file_status is TestFileStatus.PROPOSED_NEW
    assert scenario.suggested_file == "tests/test_discount_service.py"
    assert any("is an inferred path" in a for a in plan.assumptions)
    assert any("No file was created." in a for a in plan.assumptions)
    assert any("does not exist yet" in p for p in scenario.preconditions)


def test_a_suffix_style_suite_is_inferred_correctly() -> None:
    evidence = [
        coverage_map("tests/discount_test.py", "app/services/cart_service.py"),
        coverage_map("tests/cart_test.py", "app/services/cart_service.py"),
    ]
    plan = plan_with(evidence)
    assert plan.scenarios[0].suggested_file == "tests/discount_service_test.py"


# ── The three ways a plan declines ────────────────────────────────────────────


def test_no_determined_root_cause_is_insufficient_evidence() -> None:
    unresolved = analysis(status=RootCauseStatus.INSUFFICIENT_EVIDENCE, kind=None)
    plan = plan_with([coverage_map()], analysis=unresolved)
    assert plan.status is TestPlanStatus.INSUFFICIENT_EVIDENCE
    assert plan.scenarios == []
    assert "did not determine a root cause" in plan.rationale


def test_a_missing_analysis_is_insufficient_evidence_not_a_crash() -> None:
    plan = plan_with([coverage_map()], analysis=None)
    assert plan.status is TestPlanStatus.INSUFFICIENT_EVIDENCE
    assert plan.anchor is None
    assert plan.scenarios == []


def test_a_root_cause_without_an_anchor_is_invalid() -> None:
    """Different remedy from "investigate further": re-run the analysis."""
    plan = plan_with([coverage_map()], analysis=analysis(anchor=None))
    assert plan.status is TestPlanStatus.INVALID
    assert plan.scenarios == []
    assert "no anchor location" in plan.rationale


def test_an_unknown_test_suite_layout_is_insufficient_evidence() -> None:
    """Phase 10 refuses to read the repository to find one."""
    plan = plan_with([])
    assert plan.status is TestPlanStatus.INSUFFICIENT_EVIDENCE
    assert "no way to know where a new test would belong" in plan.rationale
    assert plan.scenarios == []


def test_a_refusal_still_keeps_the_gaps_it_found() -> None:
    """A gap is a recorded finding; losing it because planning stopped is a loss."""
    gap = untested_pair_gap("ev-pair")
    plan = plan_with([gap], analysis=analysis(anchor=None))
    assert plan.status is TestPlanStatus.INVALID
    assert [g.evidence_ids for g in plan.coverage_gaps] == [[gap.id]]


def test_a_refusal_never_offers_a_scenario() -> None:
    for plan in (
        plan_with([]),
        plan_with([coverage_map()], analysis=None),
        plan_with([coverage_map()], analysis=analysis(anchor=None)),
    ):
        assert plan.scenarios == []
        assert not plan.is_ready_for_review()


# ── Reading gaps out of Phase 7 evidence ──────────────────────────────────────


def test_each_phase7_gap_shape_maps_to_its_kind() -> None:
    evidence = [
        untested_pair_gap("ev-1"),
        untested_file_gap("ev-2", "checkout", "app/routes/checkout.py"),
        boundary_gap("ev-3", "app/routes/checkout.py", "app/routes/discount.py"),
        coverage_map(),
    ]
    plan = plan_with(evidence)
    assert [g.kind for g in plan.coverage_gaps] == [
        CoverageGapKind.CONCEPTS_NEVER_TESTED_TOGETHER,
        CoverageGapKind.RELEVANT_FILE_UNTESTED,
        CoverageGapKind.REQUEST_BOUNDARY_NOT_COVERED,
    ]
    assert [g.id for g in plan.coverage_gaps] == ["gap-1", "gap-2", "gap-3"]
    assert plan.evidence_ids() == ["ev-1", "ev-2", "ev-3"]


def test_gap_identifiers_follow_evidence_order() -> None:
    evidence = [untested_pair_gap("ev-b"), untested_pair_gap("ev-a"), coverage_map()]
    first = plan_with(evidence)
    second = plan_with([untested_pair_gap("ev-a"), untested_pair_gap("ev-b"), coverage_map()])
    # IDs are positional, not derived from content: the citation order moves with
    # the evidence order, and both runs remain internally consistent.
    assert [g.evidence_ids[0] for g in first.coverage_gaps] == ["ev-b", "ev-a"]
    assert [g.evidence_ids[0] for g in second.coverage_gaps] == ["ev-a", "ev-b"]


def test_a_scenario_never_cites_a_gap_the_plan_does_not_contain() -> None:
    """``covers_gaps`` is the plan's traceability claim, so it must resolve."""
    evidence = [
        untested_pair_gap("ev-pair"),
        untested_file_gap("ev-untested", "discount", ANCHOR_FILE),
        boundary_gap("ev-boundary", "app/routes/checkout.py"),
        coverage_map(),
    ]
    plan = plan_with(evidence)
    known = {g.id for g in plan.coverage_gaps}
    assert known == {"gap-1", "gap-2", "gap-3"}
    for scenario in plan.scenarios:
        assert set(scenario.covers_gaps) <= known


def test_an_observational_coverage_item_is_never_read_as_a_gap() -> None:
    """A gap is a hypothesis.

    Reading an observation as one would invert the evidence type system: the
    observation is the more certain item, and promoting it *into* a gap would
    fabricate a missing-coverage claim on its authority.
    """
    evidence = [
        evidence_item(
            evidence_id="ev-obs",
            method="coverage_analysis",
            evidence_type=EvidenceType.OBSERVATION,
            title="1 file declares HTTP routes but no test file imports any of them",
            description="Routes are declared in app/routes/checkout.py.",
            route_files=["app/routes/checkout.py"],
        ),
        coverage_map(),
    ]
    plan = plan_with(evidence)
    assert plan.coverage_gaps == []


def test_an_unrecognised_coverage_item_is_skipped_rather_than_guessed() -> None:
    evidence = [
        evidence_item(
            evidence_id="ev-weird",
            method="coverage_analysis",
            evidence_type=EvidenceType.HYPOTHESIS,
            title="something else entirely",
            description="No recognised shape.",
        ),
        coverage_map(),
    ]
    plan = plan_with(evidence)
    assert plan.coverage_gaps == []


def test_a_long_gap_rationale_is_truncated_with_a_visible_marker() -> None:
    evidence = [
        evidence_item(
            evidence_id="ev-long",
            method="coverage_analysis",
            evidence_type=EvidenceType.HYPOTHESIS,
            title="long gap",
            description="x" * (MAX_GAP_RATIONALE_CHARS + 500),
            concepts=["a", "b"],
        ),
        coverage_map(),
    ]
    plan = plan_with(evidence)
    rationale = plan.coverage_gaps[0].rationale
    assert rationale.endswith("… (truncated)")
    assert len(rationale) < MAX_GAP_RATIONALE_CHARS + 20


def test_an_import_graph_item_about_routes_is_not_mistaken_for_a_test_file() -> None:
    """The Test Investigator publishes ``import_graph`` items about routes too."""
    evidence = [
        coverage_map(),
        evidence_item(
            evidence_id="ev-routes",
            method="import_graph",
            evidence_type=EvidenceType.OBSERVATION,
            source_file="app/routes/checkout.py",
            route_files=["app/routes/checkout.py"],
        ),
    ]
    plan = plan_with(evidence)
    assert plan.scenarios[0].suggested_file == "tests/test_discount.py"


def test_an_ast_inventory_item_counts_as_a_test_file_without_claiming_coverage() -> None:
    evidence = [
        coverage_map("tests/test_cart.py", "app/services/cart_service.py"),
        evidence_item(
            evidence_id="ev-inv",
            method="ast_test_inventory",
            evidence_type=EvidenceType.OBSERVATION,
            source_file="tests/test_discount.py",
            test_functions=["test_one"],
        ),
    ]
    plan = plan_with(evidence)
    # The anchor is covered by nothing, so a new file is proposed — but it is
    # named after the observed convention, not invented from thin air.
    assert plan.scenarios[0].file_status is TestFileStatus.PROPOSED_NEW
    assert plan.scenarios[0].suggested_file == "tests/test_discount_service.py"


def test_a_test_file_recorded_twice_appears_once() -> None:
    evidence = [
        coverage_map("tests/test_discount.py", ANCHOR_FILE),
        evidence_item(
            evidence_id="ev-inv",
            method="ast_test_inventory",
            evidence_type=EvidenceType.OBSERVATION,
            source_file="tests/test_discount.py",
        ),
    ]
    plan = plan_with(evidence)
    assert plan.scenarios[0].suggested_file == "tests/test_discount.py"
    assert plan.scenarios[0].file_status is TestFileStatus.EXISTING


# ── Discriminating conditions ─────────────────────────────────────────────────


def test_a_truncation_defect_gets_a_discriminating_condition() -> None:
    """Without this rule a regression test can pass on the broken code."""
    plan = plan_with([coverage_map()])
    condition = plan.scenarios[0].discriminating_condition
    assert condition is not None
    assert "exact multiple" in condition
    assert "pass against the broken code" in condition


def test_an_unclassified_defect_says_it_cannot_derive_a_condition() -> None:
    plan = plan_with([coverage_map()], analysis=analysis(kind=RootCauseKind.UNRESOLVED))
    scenario = plan.scenarios[0]
    assert scenario.discriminating_condition is None
    assert "not derivable from the analysis on record" in scenario.expected_behavior
    assert "a real limit on this plan" in scenario.expected_behavior


@pytest.mark.parametrize(
    ("kind", "expected_fragment"),
    [
        (RootCauseKind.UNEXERCISED_PATH, "entry point"),
        (RootCauseKind.UNCOVERED_INTERACTION, "in the same test"),
    ],
)
def test_each_classified_defect_gets_its_own_condition(
    kind: RootCauseKind, expected_fragment: str
) -> None:
    plan = plan_with([coverage_map()], analysis=analysis(kind=kind))
    condition = plan.scenarios[0].discriminating_condition
    assert condition is not None and expected_fragment in condition


def test_no_scenario_claims_to_know_how_the_current_code_behaves() -> None:
    """Nothing ran, so no scenario may assert an outcome it cannot know.

    The direct scenario says plainly that failure is unestablished; the
    gap-derived ones must attribute their gap to the Phase 7 hypothesis rather
    than presenting it as verified.
    """
    plan = plan_with(
        [untested_pair_gap("ev-p"), boundary_gap("ev-b", "app/routes/checkout.py"), coverage_map()]
    )
    assert len(plan.scenarios) == 3
    assert "not established" in plan.scenarios[0].notes
    for scenario in plan.scenarios[1:]:
        assert scenario.notes.startswith("Derived from a Phase 7 hypothesis")
        assert "observation" in scenario.notes


# ── Scenario selection ────────────────────────────────────────────────────────


def test_only_the_broadest_untested_pair_becomes_a_scenario() -> None:
    """Near-duplicate scenarios crowd out the ones a developer would act on."""
    evidence = [
        untested_pair_gap(
            "ev-narrow",
            concepts=["checkout", "code"],
            modules=["app.routes.checkout", "app.services.checkout_service"],
        ),
        untested_pair_gap(
            "ev-broad",
            concepts=["checkout", "discount"],
            modules=[
                "app.routes.checkout",
                "app.services.cart_service",
                "app.services.discount_service",
            ],
        ),
        coverage_map(),
    ]
    plan = plan_with(evidence)
    interactions = [s for s in plan.scenarios if s.id != "scenario-1"]
    assert len(interactions) == 1
    assert interactions[0].covers_gaps == ["gap-2"]
    # Both pairs remain visible as gaps, so the narrower one is not hidden.
    assert len(plan.coverage_gaps) == 2


def test_scenarios_never_exceed_the_cap() -> None:
    evidence = [coverage_map(), boundary_gap("ev-boundary", "app/routes/checkout.py")]
    for n in range(MAX_SCENARIOS + 3):
        evidence.append(untested_pair_gap(f"ev-{n}", concepts=[f"a{n}", f"b{n}"]))
    plan = plan_with(evidence)
    assert len(plan.scenarios) <= MAX_SCENARIOS


def test_a_scenario_only_claims_gaps_about_its_own_file() -> None:
    """A direct regression test does not close a gap between two other concerns."""
    evidence = [
        untested_pair_gap("ev-pair"),
        untested_file_gap("ev-untested-anchor", "discount", ANCHOR_FILE),
        untested_file_gap("ev-untested-other", "cart", "app/services/cart_service.py"),
        coverage_map(),
    ]
    plan = plan_with(evidence)
    assert plan.scenarios[0].covers_gaps == ["gap-2"]  # the anchor's own file only


def test_the_direct_scenario_covers_an_untested_gap_on_the_anchor() -> None:
    evidence = [
        untested_file_gap("ev-untested-anchor", "discount", ANCHOR_FILE),
        coverage_map("tests/test_cart.py", "app/services/cart_service.py"),
    ]
    plan = plan_with(evidence)
    assert plan.scenarios[0].covers_gaps == ["gap-1"]


def test_gaps_without_a_scenario_are_reported_as_open() -> None:
    evidence = [
        untested_pair_gap("ev-pair"),
        untested_file_gap("ev-other", "cart", "app/services/cart_service.py"),
        coverage_map(),
    ]
    plan = plan_with(evidence)
    assert any("no outlined scenario" in limitation for limitation in plan.limitations)
    assert any("gap-2" in limitation for limitation in plan.limitations)


def test_a_plan_with_no_recorded_gap_says_the_suite_was_not_assessed() -> None:
    plan = plan_with([coverage_map()])
    assert plan.coverage_gaps == []
    assert any("not a claim that the suite is adequate" in l for l in plan.limitations)


def test_every_plan_states_that_nothing_was_written_or_run() -> None:
    plan = plan_with([coverage_map()])
    assert plan.execution_status is TestExecutionStatus.NOT_RUN
    assert plan.test_source_written is False
    assert any("No test was written and none was run" in l for l in plan.limitations)


# ── Traceability ──────────────────────────────────────────────────────────────


def test_the_plan_reports_the_phase9_proposal_status_for_context() -> None:
    proposal = FixProposal(
        run_id=RUN_ID,
        issue=ISSUE,
        status=FixProposalStatus.READY_FOR_REVIEW,
        rationale="r",
    )
    plan = plan_with([coverage_map()], proposal=proposal)
    assert plan.fix_proposal_status is FixProposalStatus.READY_FOR_REVIEW


def test_a_plan_is_valid_without_any_proposal() -> None:
    """A regression test is worth writing *before* the fix exists."""
    plan = plan_with([coverage_map()], proposal=None)
    assert plan.status is TestPlanStatus.READY_FOR_REVIEW
    assert plan.fix_proposal_status is None


def test_the_plan_carries_the_phase8_confidence_untouched() -> None:
    """A plan must not re-score the diagnosis; it reports what Phase 8 concluded."""
    plan = plan_with([coverage_map()])
    assert plan.root_cause_confidence == 0.48
    assert plan.root_cause_kind is RootCauseKind.TRUNCATION_BEFORE_SCALING


def test_the_planner_is_deterministic() -> None:
    evidence = [untested_pair_gap("ev-p"), coverage_map()]
    assert plan_with(evidence).to_summary() == plan_with(evidence).to_summary()


def test_a_stored_plan_round_trips_losslessly() -> None:
    plan = plan_with([untested_pair_gap("ev-p"), coverage_map()])
    assert TestPlan.from_summary(RUN_ID, plan.to_summary()) == plan


def test_the_planner_needs_no_repository_database_or_clock() -> None:
    """Pure by construction: the same inputs must always give the same plan."""
    planner = TestPlanner()
    first = planner.plan(
        run_id=RUN_ID, issue=ISSUE, analysis=analysis(), proposal=None, evidence=[coverage_map()]
    )
    second = planner.plan(
        run_id=RUN_ID, issue=ISSUE, analysis=analysis(), proposal=None, evidence=[coverage_map()]
    )
    assert first == second


# ── Model invariants ──────────────────────────────────────────────────────────


def test_a_ready_plan_needs_scenarios_and_an_anchor() -> None:
    with pytest.raises(ValidationError, match="at least one scenario"):
        TestPlan(
            run_id=RUN_ID,
            issue=ISSUE,
            status=TestPlanStatus.READY_FOR_REVIEW,
            rationale="r",
        )
    with pytest.raises(ValidationError, match="requires an anchor"):
        TestPlan(
            run_id=RUN_ID,
            issue=ISSUE,
            status=TestPlanStatus.READY_FOR_REVIEW,
            rationale="r",
            scenarios=[a_scenario()],
        )


def test_a_declined_plan_may_not_carry_scenarios() -> None:
    with pytest.raises(ValidationError, match="no scenarios"):
        TestPlan(
            run_id=RUN_ID,
            issue=ISSUE,
            status=TestPlanStatus.INSUFFICIENT_EVIDENCE,
            rationale="r",
            scenarios=[a_scenario()],
        )


def test_a_declined_plan_may_still_carry_gaps() -> None:
    """Documented behaviour, asserted so it cannot be "tidied up" by accident."""
    plan = plan_with([untested_pair_gap("ev-p")])
    assert plan.coverage_gaps
    assert plan.scenarios == []


def test_a_plan_may_not_claim_a_test_was_written() -> None:
    with pytest.raises(ValidationError, match="test_source_written must be False"):
        TestPlan(
            run_id=RUN_ID,
            issue=ISSUE,
            status=TestPlanStatus.READY_FOR_REVIEW,
            rationale="r",
            anchor={"file_path": ANCHOR_FILE},
            scenarios=[a_scenario()],
            test_source_written=True,
        )


def test_the_execution_enum_cannot_express_a_result() -> None:
    """The strongest guarantee in the module, asserted directly.

    Phase 8 and Phase 9 both carry a ``tests_executed`` boolean that is always
    False.  Here the *type* has no other member, so no future edit — here or in
    the API — can set it to something that reads as a pass.
    """
    assert [member.value for member in TestExecutionStatus] == ["NOT_RUN"]
    with pytest.raises(ValueError):
        TestExecutionStatus("PASSED")


def test_a_scenario_must_state_what_it_asserts() -> None:
    with pytest.raises(ValidationError, match="behaviour it asserts"):
        TestScenario(
            id="s1",
            name="n",
            purpose="p",
            suggested_file="tests/test_x.py",
            steps=["do the thing"],
        )


def test_a_scenario_must_have_steps() -> None:
    with pytest.raises(ValidationError, match="at least one step"):
        TestScenario(
            id="s1",
            name="n",
            purpose="p",
            suggested_file="tests/test_x.py",
            expected_behavior="e",
        )
