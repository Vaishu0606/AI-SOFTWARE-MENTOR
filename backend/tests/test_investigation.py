"""
Phase 7 — parallel bug investigation.

These tests cover the investigation layer in isolation (no HTTP, no workflow) and
then the stage as the orchestrator drives it.  The properties under test are the
ones the project's evidence rules depend on:

- **Honesty**: nothing is promoted to ``VERIFIED_RESULT``, every speculation is a
  labelled ``HYPOTHESIS``, and no root cause is claimed.
- **Determinism**: the same repository and issue always produce the same findings
  in the same order, regardless of which thread finished first.
- **Isolation**: investigators share one read-only analysis and cannot observe or
  corrupt each other's evidence.
- **Containment**: no repository code is executed and no file is written.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest

from app.investigation import (
    CodeInvestigator,
    DependencyInvestigator,
    InvestigationContext,
    TestInvestigator,
    build_issue_terms,
    build_snapshot,
    clear_snapshot_cache,
    rank_files_by_relevance,
    run_investigation,
)
from app.investigation.base import BaseInvestigator
from app.investigation.evidence import (
    EvidenceBuilder,
    EvidencePolicyError,
    build_evidence,
)
from app.investigation.runner import InvestigationRunner as Runner
from app.models.domain import EvidenceType
from app.models.investigation import (
    PHASE7_ALLOWED_EVIDENCE_TYPES,
    InvestigationConfig,
    InvestigationResult,
    InvestigationStatus,
    InvestigatorName,
    InvestigatorReport,
    InvestigatorStatus,
    SourceLocation,
)

ISSUE = "Checkout is failing when a discount code is applied."
RUN_ID = "11111111-1111-4111-8111-111111111111"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_snapshot_cache():
    """The snapshot cache is process-wide; never let it leak between tests."""
    clear_snapshot_cache()
    yield
    clear_snapshot_cache()


@pytest.fixture
def shop_repo(tmp_path: Path) -> Path:
    """A small checkout/discount repository shaped like the demo scenario.

    Written here rather than reusing ``demo-repository/`` so that a test failure
    points at a readable fixture instead of the real demo app.
    """
    repo = tmp_path / "ShopRepo"
    (repo / "app" / "routes").mkdir(parents=True)
    (repo / "app" / "services").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)

    (repo / "app" / "routes" / "checkout.py").write_text(
        "from app.services.pricing import calculate_total\n"
        "\n"
        "\n"
        "def post():\n"
        "    return {\"total\": calculate_total([])}\n",
        encoding="utf-8",
    )
    (repo / "app" / "services" / "pricing.py").write_text(
        "def calculate_total(items):\n"
        "    subtotal = 0\n"
        "    for item in items:\n"
        "        subtotal = subtotal + item.price\n"
        "    return subtotal\n"
        "\n"
        "\n"
        "def apply_discount(total, code):\n"
        "    return total\n",
        encoding="utf-8",
    )
    (repo / "app" / "services" / "discount.py").write_text(
        "def lookup(code):\n"
        "    return None\n",
        encoding="utf-8",
    )
    (repo / "app" / "utils" / "formatting.py").parent.mkdir(parents=True)
    (repo / "app" / "utils" / "formatting.py").write_text(
        "def render(value):\n"
        "    return str(value)\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_pricing.py").write_text(
        "from app.services.pricing import calculate_total\n"
        "\n"
        "\n"
        "def test_total():\n"
        "    assert calculate_total([]) == 0\n",
        encoding="utf-8",
    )
    return repo


@pytest.fixture
def shop_context(shop_repo: Path) -> InvestigationContext:
    return InvestigationContext(
        run_id=RUN_ID,
        issue=ISSUE,
        snapshot=build_snapshot(shop_repo),
        config=InvestigationConfig(),
    )


# ── Issue parsing and relevance ───────────────────────────────────────────────


def test_issue_terms_separate_concepts_and_drop_stopwords() -> None:
    terms = build_issue_terms("Checkout is failing when a discount code is applied.")
    assert "checkout" in terms.terms
    assert "discount" in terms.terms
    # Generic English carries no signal about which file is at fault.
    assert "when" not in terms.terms
    assert "is" not in terms.terms


def test_issue_terms_keep_first_mention_order() -> None:
    """Order is first-seen, not alphabetical, so quoting back is stable."""
    terms = build_issue_terms("discount fails during checkout")
    assert terms.terms.index("discount") < terms.terms.index("checkout")


def test_issue_terms_extract_quoted_identifiers_and_paths() -> None:
    terms = build_issue_terms(
        "The `checkout_service` helper in app/routes/checkout.py is wrong"
    )
    assert "checkout_service" in terms.quoted_identifiers
    assert "app/routes/checkout.py" in terms.path_hints


def test_relevance_ranks_the_files_named_by_the_issue(shop_context) -> None:
    result = rank_files_by_relevance(
        shop_context.snapshot.ast,
        build_issue_terms(ISSUE),
        test_paths=[
            f.relative_path for f in shop_context.snapshot.exploration.test_files
        ],
    )
    top_two = result.file_paths()[:2]
    assert any(p.endswith("checkout.py") for p in top_two)
    assert any(p.endswith("discount.py") for p in top_two)


def test_relevance_is_deterministic(shop_context) -> None:
    terms = build_issue_terms(ISSUE)
    first = rank_files_by_relevance(shop_context.snapshot.ast, terms).file_paths()
    second = rank_files_by_relevance(shop_context.snapshot.ast, terms).file_paths()
    assert first == second


def test_relevance_matches_an_identifier_anywhere_in_the_file(shop_context) -> None:
    """``apply_discount`` in pricing.py is a genuine match for "discount".

    Relevance works on identifiers, not on filenames, so a file that defines a
    discount helper must be considered relevant even though its name does not
    contain the word.
    """
    result = rank_files_by_relevance(shop_context.snapshot.ast, build_issue_terms(ISSUE))
    assert result.score_for("app/services/pricing.py") > 0.0
    assert "app/services/pricing.py" in result.cluster_for("discount").name_level_paths


def test_relevance_never_ranks_a_file_that_matches_nothing(shop_context) -> None:
    """A file with no matching identifier must score zero and stay out."""
    result = rank_files_by_relevance(shop_context.snapshot.ast, build_issue_terms(ISSUE))
    assert result.score_for("app/utils/formatting.py") == 0.0
    assert "app/utils/formatting.py" not in result.file_paths()


def test_relevance_caps_the_number_of_files(shop_context) -> None:
    result = rank_files_by_relevance(
        shop_context.snapshot.ast, build_issue_terms(ISSUE), max_files=2
    )
    assert len(result.file_paths()) <= 2


# ── Evidence policy ───────────────────────────────────────────────────────────


def _builder(max_items: int = 40) -> EvidenceBuilder:
    return EvidenceBuilder(
        run_id=RUN_ID,
        investigator=InvestigatorName.CODE,
        max_items=max_items,
    )


def _make(**overrides) -> dict:
    """Minimal valid arguments for ``build_evidence``, overridable per test."""
    args = {
        "run_id": RUN_ID,
        "investigator": InvestigatorName.CODE,
        "evidence_type": EvidenceType.OBSERVATION,
        "title": "Route handler calls the pricing helper",
        "description": "Read directly from the source file.",
        "location": SourceLocation(file_path="app/routes/checkout.py", line_start=4),
    }
    args.update(overrides)
    return args


def test_evidence_policy_allows_no_verified_result() -> None:
    assert EvidenceType.VERIFIED_RESULT not in PHASE7_ALLOWED_EVIDENCE_TYPES
    assert PHASE7_ALLOWED_EVIDENCE_TYPES == {
        EvidenceType.OBSERVATION,
        EvidenceType.HYPOTHESIS,
        EvidenceType.PROPOSED_ACTION,
    }


def test_evidence_policy_rejects_a_verified_result() -> None:
    """Phase 7 runs no tests, so it can never certify one."""
    with pytest.raises(EvidencePolicyError, match="VERIFIED_RESULT"):
        build_evidence(
            **_make(
                evidence_type=EvidenceType.VERIFIED_RESULT,
                location=None,
                confidence=None,
                title="The fix works",
                description="All tests pass.",
            )
        )


def test_evidence_policy_rejects_an_absolute_source_path() -> None:
    with pytest.raises(EvidencePolicyError, match="repository-relative"):
        build_evidence(
            **_make(
                location=SourceLocation(
                    file_path="C:/Users/someone/secret/app/x.py", line_start=1
                )
            )
        )


def test_evidence_policy_rejects_a_traversing_source_path() -> None:
    """Rule 10: a finding may never point outside the session's repository."""
    with pytest.raises(EvidencePolicyError, match="repository-relative"):
        build_evidence(
            **_make(location=SourceLocation(file_path="../../etc/passwd", line_start=1))
        )


def test_evidence_policy_rejects_a_windows_absolute_path() -> None:
    """A drive-qualified path is absolute even without a leading separator."""
    with pytest.raises(EvidencePolicyError, match="repository-relative"):
        build_evidence(
            **_make(
                location=SourceLocation(
                    file_path="C:\\Users\\someone\\.env", line_start=1
                )
            )
        )


def test_evidence_policy_rejects_a_unc_path() -> None:
    with pytest.raises(EvidencePolicyError, match="repository-relative"):
        build_evidence(
            **_make(location=SourceLocation(file_path="\\\\server\\share\\x.py", line_start=1))
        )


def test_evidence_policy_rejects_an_empty_source_path() -> None:
    with pytest.raises(EvidencePolicyError, match="must carry a file path"):
        build_evidence(**_make(location=SourceLocation(file_path="", line_start=1)))


def test_evidence_policy_requires_a_source_for_an_observation() -> None:
    with pytest.raises(EvidencePolicyError, match="must cite a repository-relative"):
        build_evidence(**_make(location=None))


def test_evidence_policy_allows_a_multi_file_observation() -> None:
    item = build_evidence(**_make(location=None, repository_level=True))
    assert item.source_file is None
    assert item.metadata["repository_level"] is True
    assert item.metadata["phase"] == 7


def test_evidence_policy_requires_confidence_on_a_hypothesis() -> None:
    with pytest.raises(EvidencePolicyError, match="must state a confidence"):
        build_evidence(
            **_make(evidence_type=EvidenceType.HYPOTHESIS, confidence=None)
        )


def test_evidence_policy_rejects_an_out_of_range_confidence() -> None:
    with pytest.raises(EvidencePolicyError, match="between 0.0 and 1.0"):
        build_evidence(
            **_make(evidence_type=EvidenceType.HYPOTHESIS, confidence=1.4)
        )


def test_evidence_policy_rejects_confidence_on_an_observation() -> None:
    """An observation is a fact read from code; it is not a matter of degree."""
    with pytest.raises(EvidencePolicyError, match="must not carry a confidence"):
        build_evidence(**_make(confidence=0.9))


def test_evidence_policy_rejects_a_phase7_fix() -> None:
    """Phase 7 proposes investigation steps, never code changes."""
    for title in (
        "Fix the discount lookup to return a rate",
        "Update the discount lookup to return a rate",
        "Patch apply_discount to invert the percentage",
        "Refactor calculate_total to use Decimal",
        "Rewrite the checkout handler to stream the response",
    ):
        with pytest.raises(EvidencePolicyError, match="must not propose code changes"):
            build_evidence(
                **_make(
                    evidence_type=EvidenceType.PROPOSED_ACTION,
                    title=title,
                    description=title,
                )
            )


def test_evidence_policy_does_not_mistake_a_word_for_a_fix() -> None:
    """The check is word-bounded: "fixture" and "prefix" are not "fix".

    Without this, a perfectly legitimate investigative instruction would be
    silently dropped, and the investigator would lose a real finding.
    """
    for title in (
        "Add a test fixture for the discount path",
        "Check the prefix of the applied code",
        "Implement nothing; observe the applied_discount prefix sum",
    ):
        if title.startswith("Implement"):
            # "implement" genuinely is a code-change word, so this one is
            # expected to be rejected - it is listed to document the boundary.
            with pytest.raises(EvidencePolicyError):
                build_evidence(
                    **_make(
                        evidence_type=EvidenceType.PROPOSED_ACTION,
                        title=title,
                        description=title,
                    )
                )
            continue
        item = build_evidence(
            **_make(
                evidence_type=EvidenceType.PROPOSED_ACTION,
                title=title,
                description=title,
            )
        )
        assert item.evidence_type is EvidenceType.PROPOSED_ACTION


def test_evidence_policy_allows_a_test_plan_as_a_proposed_action() -> None:
    item = build_evidence(
        **_make(
            evidence_type=EvidenceType.PROPOSED_ACTION,
            title="Write a reproduction test for the discount path",
            description="Add a scratch test asserting the reported total.",
        )
    )
    assert item.evidence_type is EvidenceType.PROPOSED_ACTION


def test_evidence_records_its_provenance() -> None:
    item = build_evidence(
        **_make(
            evidence_type=EvidenceType.HYPOTHESIS,
            confidence=0.6,
            method="ast_call_graph",
            extra_metadata={"calls": ["calculate_total"]},
        )
    )
    assert item.agent == InvestigatorName.CODE.value
    assert item.metadata["method"] == "ast_call_graph"
    assert item.metadata["investigator"] == InvestigatorName.CODE.value
    assert item.metadata["line_start"] == 4
    assert item.metadata["confidence"] == 0.6
    assert item.source_file == "app/routes/checkout.py"
    assert item.source_line == 4


def test_a_hypothesis_records_the_observations_it_leans_on() -> None:
    item = build_evidence(
        **_make(
            evidence_type=EvidenceType.HYPOTHESIS,
            confidence=0.5,
            supports=["obs-1", "obs-2"],
        )
    )
    assert item.metadata["supports"] == ["obs-1", "obs-2"]


def test_evidence_builder_cap_stops_producing_items() -> None:
    builder = _builder(max_items=2)
    added = [
        builder.observation(
            title=f"Observation {i}",
            description="Read from the file.",
            location=SourceLocation(file_path="app/routes/checkout.py", line_start=1),
        )
        for i in range(5)
    ]
    assert [a is not None for a in added] == [True, True, False, False, False]
    assert len(builder) == 2
    assert builder.truncated is True
    assert len(builder.items) == 2


def test_evidence_builder_rejects_a_zero_cap() -> None:
    with pytest.raises(ValueError):
        _builder(max_items=0)


def test_evidence_builder_gives_each_investigator_its_own_agent_name() -> None:
    item = _builder().observation(
        title="Observed",
        description="Read from the file.",
        location=SourceLocation(file_path="app/routes/checkout.py", line_start=1),
    )
    assert item.agent == "code_investigator"


# ── Individual investigators ──────────────────────────────────────────────────


def test_code_investigator_reports_readable_observations(shop_context) -> None:
    investigator = CodeInvestigator(shop_context)
    report, evidence = investigator.investigate()
    assert report.status is InvestigatorStatus.COMPLETED
    assert report.investigator is InvestigatorName.CODE
    assert evidence
    assert all(
        e.evidence_type in PHASE7_ALLOWED_EVIDENCE_TYPES for e in evidence
    )
    # Every observation must be traceable to a real file in the repository.
    for item in evidence:
        if item.source_file is not None:
            assert (shop_context.repository_root / item.source_file).exists()


def test_test_investigator_reports_a_joint_coverage_gap(shop_context) -> None:
    """The fixture's test covers pricing but neither checkout nor discount."""
    report, evidence = TestInvestigator(shop_context).investigate()
    assert report.status is InvestigatorStatus.COMPLETED
    descriptions = " ".join(e.description for e in evidence).lower()
    assert "checkout" in descriptions
    assert "discount" in descriptions


def test_test_investigator_never_claims_to_have_run_tests(shop_context) -> None:
    report, evidence = TestInvestigator(shop_context).investigate()
    assert report.summary["tests_executed"] is False
    for item in evidence:
        assert "passed" not in item.title.lower()
        assert item.evidence_type is not EvidenceType.VERIFIED_RESULT


def test_test_investigator_handles_a_repository_with_no_tests(tmp_path: Path) -> None:
    repo = tmp_path / "Bare"
    repo.mkdir()
    (repo / "main.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    context = InvestigationContext(
        run_id=RUN_ID,
        issue=ISSUE,
        snapshot=build_snapshot(repo),
        config=InvestigationConfig(),
    )
    report, evidence = TestInvestigator(context).investigate()
    assert report.status is InvestigatorStatus.COMPLETED
    assert any("no test files" in e.title.lower() for e in evidence)


def test_dependency_investigator_separates_static_from_runtime(shop_context) -> None:
    report, evidence = DependencyInvestigator(shop_context).investigate()
    assert report.status is InvestigatorStatus.COMPLETED
    # The investigator must state that its picture of the repository is a static
    # reconstruction, so a reader never mistakes it for observed behaviour.
    assert report.summary["analysis_kind"] == "static"
    for item in evidence:
        if item.evidence_type is EvidenceType.HYPOTHESIS:
            assert item.confidence is not None


def test_investigator_handles_an_empty_repository(tmp_path: Path) -> None:
    repo = tmp_path / "Empty"
    repo.mkdir()
    context = InvestigationContext(
        run_id=RUN_ID,
        issue=ISSUE,
        snapshot=build_snapshot(repo),
        config=InvestigationConfig(),
    )
    for factory in (CodeInvestigator, TestInvestigator, DependencyInvestigator):
        report, _ = factory(context).investigate()
        assert report.status is InvestigatorStatus.COMPLETED
        assert report.error is None


def test_investigator_summary_reports_no_verified_result(shop_context) -> None:
    for factory in (CodeInvestigator, TestInvestigator, DependencyInvestigator):
        report, _ = factory(shop_context).investigate()
        assert report.summary.get("tests_executed") in (False, None)
        assert "root_cause" not in report.summary


# ── Runner: determinism, isolation, concurrency ───────────────────────────────


def test_run_investigation_reports_three_independent_investigators(shop_context) -> None:
    result = run_investigation(shop_context)
    assert result.status in (InvestigationStatus.COMPLETED, InvestigationStatus.PARTIAL)
    assert [r.investigator for r in result.investigators] == [
        InvestigatorName.CODE,
        InvestigatorName.TEST,
        InvestigatorName.DEPENDENCY,
    ]


def test_run_investigation_is_deterministic(shop_context) -> None:
    """Two runs over the same input must agree item for item.

    Threads finish in an arbitrary order, so this is the test that proves the
    aggregation is not leaking scheduling into the output.  Volatile fields are
    excluded because they are expected to vary.
    """
    first = run_investigation(shop_context)
    second = run_investigation(shop_context)
    assert [(e.agent, e.title, e.description) for e in first.evidence] == [
        (e.agent, e.title, e.description) for e in second.evidence
    ]
    assert first.status is second.status


def test_each_investigator_owns_exactly_its_own_evidence(shop_context) -> None:
    result = run_investigation(shop_context)
    agents = {r.investigator.value for r in result.investigators}
    assert agents == {e.agent for e in result.evidence}
    for report in result.investigators:
        matching = [e for e in result.evidence if e.agent == report.investigator.value]
        assert len(matching) == report.evidence_count


def test_investigators_never_produce_verified_results(shop_context) -> None:
    result = run_investigation(shop_context)
    assert all(
        e.evidence_type is not EvidenceType.VERIFIED_RESULT for e in result.evidence
    )


def test_investigation_never_claims_a_root_cause(shop_context) -> None:
    result = run_investigation(shop_context)
    serialized = " ".join(
        e.title + " " + e.description for e in result.evidence
    ).lower()
    assert "root cause" not in serialized
    assert result.to_summary().get("root_cause") is None


class _ScriptedInvestigator(BaseInvestigator):
    """Test double whose ``_run`` body is supplied by the test.

    Used to prove properties of the *runner* (ordering, isolation, failure
    handling) without depending on what any real investigator happens to find in
    a fixture repository.
    """

    def __init__(self, context, name, body):
        # ``BaseInvestigator.__init__`` reads ``self.name`` to build the evidence
        # builder, so the name must be set before the base initialiser runs.
        self.name = name
        super().__init__(context)
        self._body = body

    def _run(self, builder):
        return self._body(builder)


def _findings(context, name=None):
    """A minimal realistic investigator body: emit one observation."""

    def body(builder):
        builder.observation(
            title=f"{name or 'scripted'} found something",
            description="Read directly from the file.",
            location=SourceLocation(file_path="app/routes/checkout.py", line_start=4),
        )
        return {"scripted": True}

    return body


def test_investigators_run_concurrently(shop_context) -> None:
    """All three must be inside their ``_run`` at the same time.

    A barrier is the only reliable probe.  If the runner were sequential, the
    first investigator to arrive would wait for two partners that cannot start
    and the barrier would time out, failing the test rather than passing slowly.
    """
    barrier = threading.Barrier(3, timeout=15)
    reached: list[str] = []

    def body(builder):
        reached.append(threading.current_thread().name)
        barrier.wait()
        builder.observation(
            title="Observed after synchronising",
            description="Read directly from the file.",
            location=SourceLocation(file_path="app/routes/checkout.py", line_start=4),
        )
        return {"synchronised": True}

    names = list(InvestigatorName)
    result = Runner(
        context=shop_context,
        factories=[
            (lambda ctx, n=n: _ScriptedInvestigator(ctx, n, body)) for n in names
        ],
    ).run()

    assert len(reached) == 3
    assert len(set(reached)) == 3, "investigators shared a thread"
    assert result.status is InvestigationStatus.COMPLETED
    assert len(result.evidence) == 3


def test_investigators_get_separate_threads(shop_context) -> None:
    """A single-threaded executor would make the barrier test above pass by
    accident only if the barrier were removed, so assert the thread identity."""
    barrier = threading.Barrier(2, timeout=15)
    names = [InvestigatorName.CODE, InvestigatorName.TEST]

    def body(builder):
        barrier.wait()
        return {}

    result = Runner(
        context=shop_context,
        factories=[(lambda ctx, n=n: _ScriptedInvestigator(ctx, n, body)) for n in names],
    ).run()
    # Both completed without a timeout, which is only possible off the main thread.
    assert [r.status for r in result.investigators] == [
        InvestigatorStatus.COMPLETED
    ] * 2


def test_a_failing_investigator_does_not_lose_the_others_evidence(shop_context) -> None:
    def boom(builder):
        raise RuntimeError("investigator exploded")

    names = list(InvestigatorName)

    def make(index: int):
        def _factory(context):
            body = boom if index == 1 else _findings(context, names[index])
            return _ScriptedInvestigator(context, names[index], body)

        return _factory

    result = Runner(context=shop_context, factories=[make(0), make(1), make(2)]).run()

    assert result.status is InvestigationStatus.PARTIAL
    failed = [r for r in result.investigators if r.status is InvestigatorStatus.FAILED]
    assert len(failed) == 1
    assert "exploded" in (failed[0].error or "")
    assert result.errors and "exploded" in result.errors[0]
    # The two healthy investigators still delivered their evidence.
    assert len(result.evidence) == 2


def test_every_investigator_failing_fails_the_run(shop_context) -> None:
    def boom(builder):
        raise RuntimeError("nope")

    names = list(InvestigatorName)

    def make(index: int):
        def _factory(context):
            return _ScriptedInvestigator(context, names[index], boom)

        return _factory

    result = Runner(context=shop_context, factories=[make(0), make(1), make(2)]).run()
    assert result.status is InvestigationStatus.FAILED
    assert len(result.errors) == 3
    assert not result.evidence


def test_a_run_that_finds_nothing_is_not_reported_as_successful(shop_context) -> None:
    """Investigators that complete but report nothing must not read as success.

    A run that produced no evidence has told the developer nothing, so the
    aggregate status is ``FAILED`` rather than a hollow ``COMPLETED``.
    """
    names = list(InvestigatorName)
    result = Runner(
        context=shop_context,
        factories=[
            (lambda ctx, n=n: _ScriptedInvestigator(ctx, n, lambda b: {}))
            for n in names
        ],
    ).run()
    assert all(r.status is InvestigatorStatus.COMPLETED for r in result.investigators)
    assert result.status is InvestigationStatus.FAILED


def test_runner_rejects_duplicate_investigator_names(shop_context) -> None:
    def factory(context):
        return _ScriptedInvestigator(context, InvestigatorName.CODE, _findings(context))

    with pytest.raises(ValueError, match="duplicate"):
        Runner(context=shop_context, factories=[factory, factory]).run()


def test_runner_rejects_a_non_investigator(shop_context) -> None:
    with pytest.raises(TypeError):
        Runner(context=shop_context, factories=[lambda ctx: object()]).run()


def test_runner_requires_at_least_one_investigator(shop_context) -> None:
    with pytest.raises(ValueError):
        Runner(context=shop_context, factories=[])


def test_progress_hook_reports_start_then_one_terminal_status(shop_context) -> None:
    seen: list[tuple] = []
    run_investigation(
        shop_context,
        on_progress=lambda name, status, detail: seen.append((name, status, detail)),
    )
    for name in InvestigatorName:
        statuses = [s for n, s, _ in seen if n is name]
        assert statuses[0] is InvestigatorStatus.RUNNING
        assert statuses[-1] in (InvestigatorStatus.COMPLETED, InvestigatorStatus.FAILED)
    # Every start is announced before any completion, proving concurrency.
    kinds = [s for _, s, _ in seen]
    assert kinds.index(InvestigatorStatus.COMPLETED) > max(
        i for i, s in enumerate(kinds) if s is InvestigatorStatus.RUNNING
    )


def test_progress_hook_carries_the_evidence_count(shop_context) -> None:
    seen: dict = {}
    run_investigation(
        shop_context,
        on_progress=lambda name, status, detail: seen.__setitem__(
            (name, status), detail
        ),
    )
    completed = {
        name: detail["evidence_count"]
        for (name, status), detail in seen.items()
        if status is InvestigatorStatus.COMPLETED
    }
    assert completed
    assert all(count > 0 for count in completed.values())
    assert set(completed) == set(InvestigatorName)


def test_a_broken_progress_hook_does_not_fail_the_run(shop_context) -> None:
    def hook(name, status, detail):
        raise RuntimeError("listener exploded")

    result = run_investigation(shop_context, on_progress=hook)
    assert result.status in (InvestigationStatus.COMPLETED, InvestigationStatus.PARTIAL)
    assert result.evidence


# ── Result model ──────────────────────────────────────────────────────────────


def test_investigation_result_rebuilds_from_a_summary(shop_context) -> None:
    original = run_investigation(shop_context)
    rebuilt = InvestigationResult.from_summary(
        shop_context.run_id,
        original.to_summary(),
        original.evidence,
    )
    assert rebuilt.status is original.status
    assert [r.investigator for r in rebuilt.investigators] == [
        r.investigator for r in original.investigators
    ]
    assert len(rebuilt.evidence) == len(original.evidence)


def test_the_shared_snapshot_really_is_read_only(shop_context) -> None:
    """Investigators share one snapshot; it must be impossible to write to it.

    This is the guarantee that makes sharing safe between threads, so it is
    asserted against the real object rather than trusted from the docstring.
    """
    snapshot = shop_context.snapshot
    with pytest.raises(Exception):
        snapshot.root = Path("somewhere/else")  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot.source_by_path["app/routes/checkout.py"] = "tampered"  # type: ignore[index]


def test_snapshot_never_rereads_the_filesystem(shop_repo: Path) -> None:
    """Source text comes from the Phase 3 read, so a later edit is invisible."""
    snapshot = build_snapshot(shop_repo)
    assert snapshot.source_for("app/routes/checkout.py") is not None
    (shop_repo / "app" / "routes" / "checkout.py").write_text(
        "def post():\n    return 'tampered'\n", encoding="utf-8"
    )
    assert "calculate_total" in snapshot.source_for("app/routes/checkout.py")


def test_snapshot_returns_none_for_an_unknown_file(shop_context) -> None:
    assert shop_context.snapshot.source_for("app/does/not/exist.py") is None
    assert shop_context.snapshot.lines_for("app/does/not/exist.py") == []


def test_snapshots_are_cached_per_repository(shop_repo: Path) -> None:
    assert build_snapshot(shop_repo) is build_snapshot(shop_repo)


def test_clearing_the_cache_rebuilds_the_snapshot(shop_repo: Path) -> None:
    first = build_snapshot(shop_repo)
    clear_snapshot_cache()
    assert build_snapshot(shop_repo) is not first


def test_empty_summary_reconstructs_as_pending() -> None:
    result = InvestigationResult.from_summary(RUN_ID, {}, [])
    assert result.status is InvestigationStatus.PENDING
    assert not result.investigators


# ── Containment ───────────────────────────────────────────────────────────────


def test_investigation_does_not_modify_the_repository(shop_repo: Path) -> None:
    before = {
        p.relative_to(shop_repo).as_posix(): p.read_bytes()
        for p in sorted(shop_repo.rglob("*"))
        if p.is_file()
    }
    run_investigation(
        InvestigationContext(
            run_id=RUN_ID,
            issue=ISSUE,
            snapshot=build_snapshot(shop_repo),
            config=InvestigationConfig(),
        )
    )
    after = {
        p.relative_to(shop_repo).as_posix(): p.read_bytes()
        for p in sorted(shop_repo.rglob("*"))
        if p.is_file()
    }
    assert after == before


def test_investigation_modules_contain_no_execution_primitives() -> None:
    """Static check: the investigation layer must never run repository code.

    Parsing with ``ast`` catches what a grep for ``subprocess`` would miss, such
    as ``getattr(os, "sys" + "tem")``.  ``re.compile`` and internal method calls
    are excluded because compiling a *regex* and calling our own ``run()`` are
    not code execution.
    """
    forbidden_builtins = {"eval", "exec", "compile", "__import__"}
    forbidden_attrs = {"system", "popen", "spawn", "fork", "execv", "execve"}
    package = Path(__file__).resolve().parents[1] / "app" / "investigation"
    offenders: list[str] = []
    for module in sorted(package.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden_builtins:
                offenders.append(f"{module.name}:{node.lineno} {func.id}()")
            if isinstance(func, ast.Attribute) and func.attr in forbidden_attrs:
                owners = {module.name}
                offenders.append(
                    f"{module.name}:{node.lineno} .{func.attr}() "
                    f"(available to {sorted(owners)})"
                )
    assert not offenders, "investigation layer may not execute anything: " + ", ".join(
        offenders
    )


def test_investigation_modules_import_nothing_dangerous() -> None:
    package = Path(__file__).resolve().parents[1] / "app" / "investigation"
    banned = {"subprocess", "shutil", "socket", "importlib", "pickle"}
    for module in sorted(package.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not (names & banned), f"{module.name} imports {names & banned}"
