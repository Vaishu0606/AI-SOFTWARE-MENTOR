"""
Coverage-gap analysis and regression-test planning — Phase 10.

Given a diagnosis, work out which holes in the existing test suite the diagnosed
defect could have fallen through, and outline the regression test that would
close them.

No repository access
--------------------
Phase 10 reads **nothing**.  Not the constrained single-file read Phase 9 was
allowed, not a directory listing, not a test file.  Every fact it needs is
already recorded on the run:

* the Phase 8 analysis says what is wrong and where;
* Phase 7's Test Investigator already published, as ordinary evidence items, both
  halves of the coverage picture — ``import_graph`` observations naming which
  production files each test file imports, and ``coverage_analysis`` hypotheses
  naming which combinations no test exercises.

Re-deriving any of that from the repository would create a second source of truth
that could silently disagree with the evidence the diagnosis was built on.  If the
evidence needed to plan a test is missing, the honest answer is
``INSUFFICIENT_EVIDENCE`` — not a fresh guess.

Honesty rules
-------------
Three claims this module must never make, and how each is prevented:

*"This test would fail on the current code."*
    Nothing was executed, so nothing is known about what any test would return.
    A scenario instead states a ``discriminating_condition``: what the input must
    be for the test to tell the diagnosed behaviour apart from correct behaviour.
    That is a property of the two expressions, derivable from the defect's shape
    without running anything — and it is the difference between a regression test
    and a test that passes on the broken code forever.

*"The existing suite has this gap."*
    Gaps are Phase 7 *hypotheses* with their own confidence scores.  A plan copies
    them and cites their evidence IDs; it never re-asserts them as observations
    and never re-scores them.  Promoting a hypothesis to a fact is the specific
    failure the evidence type system exists to prevent, and a plan that quietly
    did it would be the most plausible place for it to happen.

*"The test belongs in ``tests/test_discount_service.py``."*
    A new test file's path is *inferred* from the naming convention of the test
    files Phase 7 observed.  The inference is recorded in ``assumptions`` and the
    file is marked ``PROPOSED_NEW``; no file is created, because creating one is
    a human decision made after reading the plan.

Determinism
-----------
No clock, no randomness, no I/O, no LLM.  Gaps keep the order of the evidence
they were read from (which is Phase 7's production order, itself deterministic),
scenarios are numbered by position, and identifiers are derived from that position
rather than generated.  The same run inputs therefore always produce a
byte-identical plan, which is what makes a plan comparable between two runs and
safe to cache.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import PurePosixPath

from app.models.domain import Evidence, EvidenceType
from app.models.fix_proposal import AnchorLocation, FixProposal
from app.models.root_cause import RootCauseAnalysis, RootCauseKind
from app.models.test_plan import (
    CoverageGap,
    CoverageGapKind,
    TestExecutionStatus,
    TestFileStatus,
    TestPlan,
    TestPlanStatus,
    TestScenario,
)

logger = logging.getLogger(__name__)

#: Phase 7 metadata ``method`` values this planner reads.  Both are the Test
#: Investigator's; nothing else in the system publishes them.
COVERAGE_METHOD = "coverage_analysis"
IMPORT_GRAPH_METHOD = "import_graph"
TEST_INVENTORY_METHOD = "ast_test_inventory"

#: Upper bound on outlined scenarios.  A plan is a review artefact: past a handful
#: of scenarios nobody reads them, and an unread plan is a silent one.
MAX_SCENARIOS = 3

#: Upper bound on how much of a Phase 7 description is copied into a gap.  Phase 7
#: already bounds its own output, so this is a backstop against an unusually long
#: description turning the plan into a wall of text.
MAX_GAP_RATIONALE_CHARS = 600


# ── What the defect means for a test ─────────────────────────────────────────
#
# Keyed by the Phase 8 root-cause kind.  Each entry answers the same two questions
# about a defect: what must a test assert, and what must its input look like to be
# able to distinguish the defect from correct behaviour.  There is deliberately no
# entry for a kind whose shape says nothing about either — the fallback says so
# instead of inventing an expectation.
#
# These are statements about arithmetic and control flow, not about any particular
# repository.  They never claim what code does when run.


def _expectation_for_truncation() -> str:
    return (
        "The returned amount must equal the unscaled total multiplied by the "
        "factor, truncated once at the end. Truncating the total *before* applying "
        "the factor is the diagnosed defect: for one input the two orders of "
        "operations cannot both be right, and the value that survives is smaller "
        "than the one the requirement describes."
    )


def _condition_for_truncation() -> str:
    return (
        "The unscaled total must not be an exact multiple of the divisor. On an "
        "exact multiple the two orders of operations agree, so such an input "
        "cannot tell the diagnosed expression from a corrected one — a test built "
        "on it would pass against the broken code and catch nothing."
    )


#: ``RootCauseKind`` → ``(expected_behavior, discriminating_condition)``.
#: ``None`` for the condition means "Phase 10 cannot derive one", which the
#: scenario states rather than implying that any input would do.
_DEFECT_EXPECTATIONS: dict[
    RootCauseKind, tuple[str, str | None]
] = {
    RootCauseKind.TRUNCATION_BEFORE_SCALING: (
        _expectation_for_truncation(),
        _condition_for_truncation(),
    ),
    RootCauseKind.UNEXERCISED_PATH: (
        "Reaching the affected code through the reported entry point must produce "
        "the same result as any other route to it. No existing test establishes "
        "either value, which is why the path could be broken unnoticed.",
        "The scenario must reach the function through the entry point the report "
        "describes. Calling it directly exercises a route the report is not "
        "about, and would pass regardless of the defect.",
    ),
    RootCauseKind.UNCOVERED_INTERACTION: (
        "The concerns named in the report must work together: exercising them in "
        "sequence must produce the combined result, not the two results "
        "separately. Each works on its own today, which is why neither test fails.",
        "Both concerns must be exercised in the same test. Either one alone is "
        "already covered, so a test that exercises only one adds nothing.",
    ),
}


class TestPlanner:
    """Builds a :class:`TestPlan` from a diagnosis and the recorded evidence.

    Pure and dependency-free: the constructor takes nothing and the plan method
    takes only already-loaded data, so a test can drive it directly with no
    repository, no database and no orchestrator.
    """

    def plan(
        self,
        *,
        run_id: str,
        issue: str,
        analysis: RootCauseAnalysis | None,
        proposal: FixProposal | None,
        evidence: list[Evidence],
    ) -> TestPlan:
        """Return the plan for one run.

        Never raises for an unplannable run: every honest negative is a
        :class:`TestPlanStatus` with a stated reason, because "I could not plan a
        test" is an answer a developer needs, not an error.
        """
        # Read the gaps before deciding anything.  A gap is a recorded finding
        # about the suite, not a proposal, and it is true whether or not a
        # scenario can be placed afterwards — so every refusal below keeps them
        # rather than discarding real information because the plan as a whole
        # could not be completed.
        gaps = self._read_gaps(evidence)

        if analysis is None or analysis.root_cause is None:
            return _declined(
                run_id,
                issue,
                TestPlanStatus.INSUFFICIENT_EVIDENCE,
                (
                    "Phase 8 did not determine a root cause "
                    f"(status {analysis.status.value if analysis else 'MISSING'}), "
                    "so there is no diagnosed behaviour for a regression test to "
                    "pin down. A test written against an undiagnosed problem would "
                    "assert whatever the author already believed, which is the "
                    "thing a regression test exists to check."
                ),
                analysis=analysis,
                proposal=proposal,
                gaps=gaps,
            )

        if analysis.anchor is None:
            return _declined(
                run_id,
                issue,
                TestPlanStatus.INVALID,
                (
                    "Phase 8 determined a root cause but recorded no anchor "
                    "location, so a scenario cannot be aimed at any code. The "
                    "remedy is to re-run the analysis; there is nothing further to "
                    "investigate."
                ),
                analysis=analysis,
                proposal=proposal,
                gaps=gaps,
            )

        anchor = AnchorLocation.from_anchor(analysis.anchor)
        test_files, covers = self._read_test_suite(evidence)

        if not test_files:
            return _declined(
                run_id,
                issue,
                TestPlanStatus.INSUFFICIENT_EVIDENCE,
                (
                    "No record of the test suite's layout is available: the "
                    "evidence on this run names no test file, so there is no way "
                    "to know where a new test would belong or what it could "
                    "import. Phase 10 does not read the repository to find out, "
                    "because a path guessed from a fresh look could disagree with "
                    "the evidence the diagnosis rests on."
                ),
                analysis=analysis,
                proposal=proposal,
                gaps=gaps,
            )

        suggestions: list[str] = []
        limitations: list[str] = [
            "No test was written and none was run: this plan is prose a developer "
            "implements, so its value is untested until they do.",
        ]

        target_path, file_status, inferred = self._choose_target(
            anchor, test_files, covers
        )
        suggestions.extend(inferred)

        scenarios = self._build_scenarios(
            issue=issue,
            analysis=analysis,
            anchor=anchor,
            gaps=gaps,
            target_path=target_path,
            file_status=file_status,
        )

        if not scenarios:  # pragma: no cover - guarded by the branches above
            return _declined(
                run_id,
                issue,
                TestPlanStatus.INSUFFICIENT_EVIDENCE,
                "No scenario could be outlined from the recorded evidence.",
                analysis=analysis,
                proposal=proposal,
            )

        if not gaps:
            limitations.append(
                "Phase 7 recorded no coverage gap for this area, so the plan "
                "outlines a direct regression test for the diagnosed line and "
                "nothing about the wider suite. That is weaker than a gap-driven "
                "plan and is not a claim that the suite is adequate."
            )

        covered = {gid for s in scenarios for gid in s.covers_gaps}
        uncovered = [g.id for g in gaps if g.id not in covered]
        if uncovered:
            limitations.append(
                f"{len(uncovered)} of {len(gaps)} recorded gap(s) "
                f"({', '.join(uncovered)}) have no outlined scenario. They are "
                "listed so the coverage picture is complete, not because a test "
                "was proposed for them: a gap with no scenario is an open item, "
                "and should not be read as a closed one."
            )

        return TestPlan(
            run_id=run_id,
            issue=issue,
            status=TestPlanStatus.READY_FOR_REVIEW,
            rationale=_plan_rationale(analysis, len(gaps), len(scenarios)),
            anchor=anchor,
            root_cause_kind=analysis.root_cause_kind,
            root_cause_confidence=analysis.confidence,
            fix_proposal_status=(
                proposal.status if proposal else None
            ),
            coverage_gaps=gaps,
            scenarios=scenarios,
            execution_status=TestExecutionStatus.NOT_RUN,
            test_source_written=False,
            assumptions=suggestions,
            limitations=limitations,
        )

    # ── Reading the recorded evidence ───────────────────────────────────

    @staticmethod
    def _read_gaps(evidence: list[Evidence]) -> list[CoverageGap]:
        """Turn Phase 7 coverage hypotheses into plan gaps, in evidence order.

        Only items whose ``method`` is ``coverage_analysis`` and whose type is
        ``HYPOTHESIS`` qualify.  The type check is the point: the same method also
        labels observational items, and copying one of those into a plan would
        present a hypothesis-derived claim as a gap that had been verified.
        """
        gaps: list[CoverageGap] = []
        for item in evidence:
            if item.evidence_type is not EvidenceType.HYPOTHESIS:
                continue
            if item.metadata.get("method") != COVERAGE_METHOD:
                continue

            metadata = item.metadata
            concepts = [str(c) for c in (metadata.get("concepts") or [])]
            untested = [str(f) for f in (metadata.get("untested_files") or [])]
            routes = [str(f) for f in (metadata.get("route_files") or [])]

            if len(concepts) == 2:
                kind = CoverageGapKind.CONCEPTS_NEVER_TESTED_TOGETHER
                modules = [
                    str(m)
                    for m in (
                        list(metadata.get("first_modules") or [])
                        + list(metadata.get("second_modules") or [])
                    )
                ]
                files: list[str] = []
            elif untested:
                kind = CoverageGapKind.RELEVANT_FILE_UNTESTED
                modules = [str(m) for m in (metadata.get("untested_modules") or [])]
                files = untested
            elif routes:
                kind = CoverageGapKind.REQUEST_BOUNDARY_NOT_COVERED
                modules = []
                files = list(routes)
            else:
                # A coverage item whose shape this planner does not recognise.
                # Skipping it is deliberate: inventing a gap from metadata that
                # does not say what the gap is would be a guess wearing a
                # citation.
                logger.debug(
                    "Skipping unrecognised coverage item %s for gap extraction",
                    item.id,
                )
                continue

            gaps.append(
                CoverageGap(
                    id=f"gap-{len(gaps) + 1}",
                    kind=kind,
                    title=item.title,
                    rationale=_truncate(item.description),
                    evidence_ids=[item.id],
                    concepts=_gap_concepts(concepts, metadata),
                    modules=sorted(set(modules)),
                    files=sorted(set(files)),
                )
            )
        return gaps

    @staticmethod
    def _read_test_suite(
        evidence: list[Evidence],
    ) -> tuple[list[str], dict[str, list[str]]]:
        """Return the observed test files and what each production file is covered by.

        Two kinds of Phase 7 item identify a test file: an ``import_graph``
        observation that lists ``files_under_test`` (which also yields the
        coverage map) and an ``ast_test_inventory`` observation (which yields only
        the file's existence).

        The ``files_under_test`` check matters: the Test Investigator also emits
        ``import_graph`` observations about *route* files, and treating one of
        those as a test file would invent a path that does not exist.
        """
        test_files: list[str] = []
        covers: dict[str, list[str]] = {}

        for item in evidence:
            path = item.source_file
            if not path:
                continue
            method = item.metadata.get("method")

            if method == IMPORT_GRAPH_METHOD:
                covered = [str(f) for f in (item.metadata.get("files_under_test") or [])]
                if not covered:
                    continue
                if path not in test_files:
                    test_files.append(path)
                for production in covered:
                    covers.setdefault(production, []).append(path)
            elif method == TEST_INVENTORY_METHOD and path not in test_files:
                test_files.append(path)

        return test_files, covers

    # ── Choosing where a scenario would live ────────────────────────────

    @staticmethod
    def _choose_target(
        anchor: AnchorLocation,
        test_files: list[str],
        covers: dict[str, list[str]],
    ) -> tuple[str, TestFileStatus, list[str]]:
        """Pick the file a scenario belongs in, and say how that was decided.

        A test file that already imports the anchor's module is preferred, and the
        lowest-sorting one wins so the choice does not depend on dict ordering.
        Only when nothing covers the anchor's module is a new path inferred — from
        the convention the observed test files share, which is an assumption about
        a repository, not a fact about it, and is returned to be recorded.
        """
        candidates = sorted(covers.get(anchor.file_path, []))
        if candidates:
            return candidates[0], TestFileStatus.EXISTING, []

        return _infer_new_test_path(anchor.file_path, test_files)

    # ── Outlining the scenarios ─────────────────────────────────────────

    @staticmethod
    def _build_scenarios(
        *,
        issue: str,
        analysis: RootCauseAnalysis,
        anchor: AnchorLocation,
        gaps: list[CoverageGap],
        target_path: str,
        file_status: TestFileStatus,
    ) -> list[TestScenario]:
        """Outline the scenarios, most important first, capped at MAX_SCENARIOS."""
        kind = analysis.root_cause_kind
        expected, condition = _DEFECT_EXPECTATIONS.get(kind, (None, None))
        if expected is None:
            expected = (
                "Pin the behaviour the report describes — "
                f"{issue.strip()} — so a change that alters it is caught. The "
                "expected value is not derivable from the analysis on record: "
                "Phase 8 did not classify the defect, so it must be taken from "
                "the specification or from the developer's own understanding. "
                "That is a real limit on this plan, not an oversight."
            )

        module = _module_name(anchor.file_path)
        where = anchor.function or anchor.file_path

        scenarios: list[TestScenario] = []

        # ── 1. The direct regression test for the diagnosed line ────────
        preconditions = [
            f"Exercise {module} through its own public entry point, not by "
            "reaching past it into another module's internals."
        ]
        if file_status is TestFileStatus.PROPOSED_NEW:
            preconditions.insert(
                0,
                f"{target_path} does not exist yet; create it following the "
                "naming and layout convention the existing test files use.",
            )

        scenarios.append(
            TestScenario(
                id="scenario-1",
                name=f"Regress {where}: {kind.value if kind else 'reported behaviour'}",
                purpose=(
                    f"Pin the behaviour Phase 8 determined to be wrong at "
                    f"{anchor.file_path}"
                    + (f":{anchor.line}" if anchor.line else "")
                    + f", so a change that reintroduces it is caught by a test "
                    "rather than by the next user who hits it."
                ),
                suggested_file=target_path,
                file_status=file_status,
                target_function=anchor.function,
                preconditions=preconditions,
                steps=[
                    f"Build the inputs {where} needs so the reported scenario is "
                    f"the one under test: {issue.strip()}",
                    f"Call {where} with those inputs.",
                    "Assert the result against the expected behaviour below.",
                ],
                expected_behavior=expected,
                discriminating_condition=condition,
                # Only gaps that are actually about this file.  A direct
                # regression test does not close the gap between two other
                # concerns, and claiming otherwise would let a reviewer believe
                # a recorded hole had been addressed by a test that never
                # touches it.
                covers_gaps=[
                    g.id
                    for g in gaps
                    if anchor.file_path in g.files
                    and g.kind is CoverageGapKind.RELEVANT_FILE_UNTESTED
                ],
                notes=(
                    "Whether this test would fail against the current code is not "
                    "established: nothing has been executed. It is written to be "
                    "capable of failing."
                ),
            )
        )

        # ── 2. The broadest interaction the suite never exercises ───────
        interaction = _broadest_interaction(gaps)
        if interaction is not None and len(scenarios) < MAX_SCENARIOS:
            gap = interaction
            first, second = gap.concepts[0], gap.concepts[1]
            scenarios.append(
                TestScenario(
                    id=f"scenario-{len(scenarios) + 1}",
                    name=f"Exercise '{first}' and '{second}' in one test",
                    purpose=(
                        f"Close the gap Phase 7 recorded: no existing test "
                        f"exercises '{first}' and '{second}' together, so a defect "
                        "that needs both could not have been caught. Each is "
                        "covered on its own."
                    ),
                    suggested_file=target_path,
                    file_status=file_status,
                    target_function=anchor.function,
                    preconditions=[
                        "Both sides of the interaction must be reachable from "
                        f"{target_path}.",
                    ],
                    steps=[
                        f"Set up state that requires '{first}'.",
                        f"Then exercise '{second}' against that state, in the same "
                        "test rather than in two.",
                        "Assert the combined result, not the two intermediate "
                        "results.",
                    ],
                    expected_behavior=(
                        f"Performing '{first}' and then '{second}' must produce the "
                        "result the application specifies for the combination. "
                        "Neither behaviour is wrong on its own, so only a test that "
                        "does both can detect a problem in the interaction."
                    ),
                    discriminating_condition=(
                        "Both steps must occur in one test. Running them as two "
                        "tests reproduces the existing coverage exactly, which is "
                        "the gap."
                    ),
                    covers_gaps=[gap.id],
                    notes=(
                        "Derived from a Phase 7 hypothesis, not an observation: the "
                        "gap is as certain as that hypothesis's confidence. Where "
                        "several pairs are untested together, this outlines the one "
                        "spanning the most production modules; the rest stay in the "
                        "gap list rather than becoming near-duplicate scenarios."
                    ),
                )
            )

        # ── 3. The request boundary, if nothing covers it ───────────────
        boundary = [g for g in gaps if g.kind is CoverageGapKind.REQUEST_BOUNDARY_NOT_COVERED]
        if boundary and len(scenarios) < MAX_SCENARIOS:
            scenarios.append(
                TestScenario(
                    id=f"scenario-{len(scenarios) + 1}",
                    name="Drive the affected path through its request boundary",
                    purpose=(
                        "No test imports a module that declares an HTTP route, so "
                        "request parsing, validation and wiring between the route "
                        "and the code it calls are unverified. If the report is "
                        "about what a request returns, the existing suite could "
                        "pass while the reported behaviour is still wrong."
                    ),
                    suggested_file=target_path,
                    file_status=file_status,
                    target_function=None,
                    preconditions=[
                        "Use whatever HTTP client the existing suite already uses; "
                        "Phase 10 does not assume one is present.",
                    ],
                    steps=[
                        "Issue the request the report describes.",
                        "Assert the response, not just the underlying function's "
                        "return value.",
                    ],
                    expected_behavior=(
                        "The request must be accepted and must produce the "
                        "behaviour the report says is missing. This is the level at "
                        "which the failure was observed, so it is the level at "
                        "which it should be asserted."
                    ),
                    discriminating_condition=(
                        "The assertion must be made on the response. A test that "
                        "calls the service function directly would pass whether or "
                        "not the route is wired correctly."
                    ),
                    covers_gaps=[g.id for g in boundary],
                    notes=(
                        "Derived from a Phase 7 hypothesis, not an observation: "
                        "the claim that the route layer is untested is as certain "
                        "as that hypothesis's confidence. Whether the report "
                        "concerns the request boundary is itself unresolved."
                    ),
                )
            )

        return scenarios


def _broadest_interaction(gaps: list[CoverageGap]) -> CoverageGap | None:
    """The untested pair spanning the most production modules, or None.

    Only one interaction scenario is outlined even when several pairs are
    untested together.  A demo issue usually names three or four terms, so the
    Test Investigator reports a pair for each combination, and a scenario per pair
    produces near-duplicates that crowd out the scenarios a developer would
    actually act on.  Every pair stays in the plan's gap list; the scenario picks
    the widest, on the reasoning that the interaction touching the most code is
    the one most likely to be the reported one.

    Breadth first, evidence order second: a plan must not depend on dictionary
    ordering, so the tie-break is the gap's position, which follows Phase 7's
    production order.
    """
    candidates = [
        gap
        for gap in gaps
        if gap.kind is CoverageGapKind.CONCEPTS_NEVER_TESTED_TOGETHER
        and len(gap.concepts) == 2
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda gap: (len(gap.modules), -_index_of(gaps, gap)))


def _index_of(gaps: list[CoverageGap], gap: CoverageGap) -> int:
    for position, candidate in enumerate(gaps):
        if candidate is gap:
            return position
    return 0  # pragma: no cover - only reachable if a gap is not from this list


# ── Helpers ───────────────────────────────────────────────────────────────────


def _infer_new_test_path(
    production_path: str, test_files: list[str]
) -> tuple[str, TestFileStatus, list[str]]:
    """Infer where a new test file would go, from the observed test files.

    The convention is read off the paths Phase 7 recorded — the most common
    directory, and whether the suite prefixes or suffixes ``test`` — because a new
    file placed outside the suite's convention will not be collected by the test
    runner, which would make it useless without looking obviously wrong.  The
    result is returned as an assumption, never as a fact.
    """
    directories = Counter(str(PurePosixPath(p).parent) for p in test_files)
    best_directory = sorted(directories.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    prefix_style = sum(1 for p in test_files if PurePosixPath(p).name.startswith("test_"))
    suffix_style = sum(1 for p in test_files if PurePosixPath(p).name.endswith("_test.py"))

    stem = PurePosixPath(production_path).stem
    if prefix_style >= suffix_style and prefix_style > 0:
        name = f"test_{stem}.py"
        style = "prefix"
    else:
        name = f"{stem}_test.py"
        style = "suffix"

    assumed = [
        f"{best_directory}/{name} is an inferred path: no observed test file "
        f"imports the module under test, so the suite's layout ({best_directory}) "
        f"and its {style}-style naming were extrapolated from "
        f"{len(test_files)} test file(s) Phase 7 recorded. No file was created."
    ]

    return str(PurePosixPath(best_directory) / name), TestFileStatus.PROPOSED_NEW, assumed


def _gap_concepts(concepts: list[str], metadata: dict) -> list[str]:
    """The issue terms a gap concerns.

    A "concepts never tested together" gap names two; a "relevant file untested"
    gap names one, under the singular ``concept`` key.  A request-boundary gap
    names none — it is about a layer, not a term — and returns an empty list
    rather than borrowing a concept it does not have.
    """
    if concepts:
        return concepts
    single = metadata.get("concept")
    return [str(single)] if single else []


def _module_name(path: str) -> str:
    """Dotted module name for a repository-relative Python path.

    A convention, not a fact: ``app/services/x.py`` is module
    ``app.services.x`` under normal Python packaging.  It is only used to tell a
    reader which module to import, so an unusual layout costs them one import fix.
    """
    parts = PurePosixPath(path).with_suffix("").parts
    return ".".join(parts)


def _truncate(text: str, limit: int = MAX_GAP_RATIONALE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " … (truncated)"


def _plan_rationale(
    analysis: RootCauseAnalysis, gap_count: int, scenario_count: int
) -> str:
    kind = analysis.root_cause_kind.value if analysis.root_cause_kind else "unclassified"
    return (
        f"Phase 8 determined a {kind} defect at {analysis.confidence_level.value} "
        f"confidence ({analysis.confidence}). {gap_count} recorded coverage "
        f"gap(s) relate to it, and {scenario_count} scenario(s) are outlined to "
        "close them. Nothing was written and nothing was executed: this is a "
        "plan for a developer to implement, and it is unproven until they run it."
    )


def _declined(
    run_id: str,
    issue: str,
    status: TestPlanStatus,
    rationale: str,
    *,
    analysis: RootCauseAnalysis | None,
    proposal: FixProposal | None,
    gaps: list[CoverageGap] | None = None,
) -> TestPlan:
    """Build a plan that declines, carrying traceability but no scenarios.

    Gaps found on the way to the refusal are kept.  They are recorded findings
    about the suite, independent of whether a scenario could be placed, and
    discarding them would lose real information purely because the plan as a whole
    could not be completed.
    """
    return TestPlan(
        run_id=run_id,
        issue=issue,
        status=status,
        rationale=rationale,
        # ``from_anchor`` accepts ``None`` and returns ``None``: a declined plan
        # keeps whatever traceability the analysis carries, and nothing more.
        anchor=AnchorLocation.from_anchor(analysis.anchor) if analysis else None,
        root_cause_kind=analysis.root_cause_kind if analysis else None,
        root_cause_confidence=analysis.confidence if analysis else 0.0,
        fix_proposal_status=proposal.status if proposal else None,
        coverage_gaps=list(gaps or []),
        scenarios=[],
        execution_status=TestExecutionStatus.NOT_RUN,
        test_source_written=False,
        assumptions=[],
        limitations=[
            "No scenario was outlined, so nothing here can be written up as a "
            "missing test without first resolving the status above."
        ],
    )


#: The shared instance.  The planner holds no state, so one is enough.
DEFAULT_TEST_PLANNER = TestPlanner()


__all__ = [
    "COVERAGE_METHOD",
    "DEFAULT_TEST_PLANNER",
    "IMPORT_GRAPH_METHOD",
    "MAX_GAP_RATIONALE_CHARS",
    "MAX_SCENARIOS",
    "TEST_INVENTORY_METHOD",
    "TestPlanner",
]
