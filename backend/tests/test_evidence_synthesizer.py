"""
Phase 8 — deterministic evidence synthesis and root-cause analysis.

These tests cover the synthesizer in isolation (no HTTP, no workflow state
machine) and then the two stages as the orchestrator drives them.  The
properties under test are the ones Phase 8 is allowed to claim:

- **Epistemic honesty**: no ``VERIFIED_RESULT`` is ever produced or consumed,
  ``tests_executed`` is always False, and insufficient evidence yields a named
  non-conclusion instead of a guess.
- **Traceability**: every conclusion cites stored evidence ids and is anchored
  to a real file and line.
- **Determinism**: identical evidence always yields an identical verdict, and
  the analysis itself carries no timestamp.
- **Containment**: the synthesizer reads the Phase 7 result only.  It never
  opens a repository file, executes code or writes anything.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.analysis.evidence_synthesizer import (
    DEFAULT_SYNTHESIS_CONFIG,
    EvidenceIndex,
    EvidenceSynthesisError,
    EvidenceSynthesizer,
    SynthesisConfig,
    deduplicate,
    detect_truncation_before_scaling,
    normalize_title,
)
from app.investigation import (
    InvestigationContext,
    build_snapshot,
    clear_snapshot_cache,
    run_investigation,
)
from app.models.domain import Evidence, EvidenceType
from app.models.investigation import (
    InvestigationConfig,
    InvestigationResult,
    InvestigationStatus,
    InvestigatorName,
    InvestigatorReport,
    InvestigatorStatus,
)
from app.models.root_cause import (
    ChainNodeKind,
    ConfidenceLevel,
    RootCauseKind,
    RootCauseStatus,
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
    """A checkout/discount repository carrying the real demo defect.

    ``calculate_discount_cents`` truncates a division before scaling it, which is
    the exact shape Phase 8 must detect.  The comment records the correct
    expression so the synthesizer can be checked against ground truth.
    """
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
        "    # correct: int(subtotal_cents * code.value / 100)\n"
        "    return int(subtotal_cents / 100) * code.value\n",
        encoding="utf-8",
    )
    (repo / "app" / "services" / "checkout_service.py").write_text(
        "def checkout(items, code):\n"
        "    if not items:\n"
        "        raise ValueError('empty cart')\n"
        "    subtotal = 0\n"
        "    for item in items:\n"
        "        subtotal = subtotal + item.price_cents\n"
        "    return subtotal\n",
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
def investigation(shop_repo: Path) -> InvestigationResult:
    return run_investigation(
        InvestigationContext(
            run_id=RUN_ID,
            issue=ISSUE,
            snapshot=build_snapshot(shop_repo),
            config=InvestigationConfig(),
        )
    )


def _evidence(
    evidence_id: str,
    title: str,
    *,
    evidence_type: EvidenceType = EvidenceType.OBSERVATION,
    agent: str = "code_investigator",
    source_file: str | None = "app/services/discount_service.py",
    source_line: int | None = 7,
    description: str = "",
    metadata: dict | None = None,
) -> Evidence:
    return Evidence(
        id=evidence_id,
        session_id=RUN_ID,
        evidence_type=evidence_type,
        agent=agent,
        title=title,
        description=description or title,
        source_file=source_file,
        source_line=source_line,
        metadata=metadata or {},
    )


def _result(
    evidence: list[Evidence],
    *,
    status: InvestigationStatus = InvestigationStatus.COMPLETED,
    reports: list[InvestigatorReport] | None = None,
) -> InvestigationResult:
    return InvestigationResult(
        run_id=RUN_ID,
        issue=ISSUE,
        status=status,
        evidence=evidence,
        investigators=reports
        if reports is not None
        else [
            InvestigatorReport(
                investigator=InvestigatorName.CODE,
                status=InvestigatorStatus.COMPLETED,
                evidence_count=len(evidence),
            )
        ],
    )


# ── The truncation rule ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "expression",
    [
        # cast wraps the division, multiplication happens outside: truncation
        "int(subtotal / 100) * code.value",
        "round(subtotal / 100) * rate",
        "int(a / b) * c",
        "int(subtotal_cents / 100) * code.value  # trailing comment",
    ],
)
def test_truncation_rule_fires_on_a_cast_around_a_division(expression: str) -> None:
    assert detect_truncation_before_scaling(expression) is True


@pytest.mark.parametrize(
    "expression",
    [
        # scaling inside the cast is the correct order
        "int(subtotal * code.value / 100)",
        "round(subtotal * rate / 100)",
        # no division at all
        "int(subtotal) * code.value",
        "subtotal * code.value",
        # no cast at all
        "subtotal / 100 * code.value",
        # unrelated expression
        "return subtotal_cents",
    ],
)
def test_truncation_rule_stays_quiet_on_correct_or_unrelated_code(
    expression: str,
) -> None:
    assert detect_truncation_before_scaling(expression) is False


def test_truncation_rule_ignores_a_cast_inside_a_comment() -> None:
    """A comment describing the bug must not be read as the bug."""
    text = "# int(subtotal / 100) * code.value\nexact = subtotal * rate / 100"
    assert detect_truncation_before_scaling(text) is False


# ── Normalisation and deduplication ───────────────────────────────────────────


def test_normalize_title_collapses_case_punctuation_and_spacing() -> None:
    assert normalize_title("  Numeric  cast:  detail!  ") == normalize_title(
        "numeric cast detail"
    )


def test_deduplicate_merges_the_same_finding_from_two_agents() -> None:
    """Independent agreement is corroboration, not duplication."""
    findings = deduplicate(
        [
            _evidence("a", "Numeric cast in discount_service", agent="code_investigator"),
            _evidence("b", "Numeric cast in discount_service", agent="relation_reasoning"),
        ]
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.agents == ("code_investigator", "relation_reasoning")
    assert finding.corroborated is True
    # A corroborated finding keeps one accurate citation per reporter.
    refs = finding.refs()
    assert [(r.evidence_id, r.agent) for r in refs] == [
        ("a", "code_investigator"),
        ("b", "relation_reasoning"),
    ]


def test_deduplicate_never_launders_a_hypothesis_into_an_observation() -> None:
    """Two reports of one site stay separate unless they agree on their type."""
    findings = deduplicate(
        [
            _evidence("a", "Discount rounding", agent="code_investigator"),
            _evidence(
                "b",
                "Discount rounding",
                agent="relation_reasoning",
                evidence_type=EvidenceType.HYPOTHESIS,
            ),
        ]
    )
    assert len(findings) == 2
    assert {f.representative.evidence_type for f in findings} == {
        EvidenceType.OBSERVATION,
        EvidenceType.HYPOTHESIS,
    }


def test_deduplicate_keeps_findings_that_differ_only_in_their_anchor() -> None:
    """Two hypotheses about different sites are two findings, not one."""
    findings = deduplicate(
        [
            _evidence(
                "a",
                "Discount rounding is suspicious",
                evidence_type=EvidenceType.HYPOTHESIS,
                source_line=7,
                metadata={"anchored_on": "app/services/discount_service.py:7"},
            ),
            _evidence(
                "b",
                "Discount rounding is suspicious",
                evidence_type=EvidenceType.HYPOTHESIS,
                source_line=9,
                metadata={"anchored_on": "app/services/cart.py:9"},
            ),
        ]
    )
    assert len(findings) == 2


def test_deduplicate_is_order_independent() -> None:
    items = [
        _evidence("a", "First"),
        _evidence("b", "Second"),
        _evidence("c", "First", agent="relation_reasoning"),
    ]
    forward = {f.representative.title: sorted(f.evidence_ids) for f in deduplicate(items)}
    reverse = {f.representative.title: sorted(f.evidence_ids) for f in deduplicate(items[::-1])}
    assert forward == reverse


# ── Unsupported evidence is refused, not trusted ───────────────────────────────


def test_synthesis_rejects_verified_results_and_records_the_rejection() -> None:
    """Phase 8 executes nothing, so a verified result cannot reach it at all."""
    result = _result(
        [
            _evidence("a", "Numeric cast detail"),
            _evidence(
                "b",
                "tests pass",
                evidence_type=EvidenceType.VERIFIED_RESULT,
                source_file="tests/test_discount.py",
            ),
        ]
    )
    synthesis, index = EvidenceSynthesizer().synthesize_evidence(result, issue=ISSUE)
    assert index.rejected_count == 1
    assert index.rejected_types == ("VERIFIED_RESULT",)
    assert [f.representative.id for f in index.findings] == ["a"]
    assert synthesis.rejected_count == 1
    assert synthesis.rejected_types == ["VERIFIED_RESULT"]


def test_analysis_never_reports_a_verified_result_or_execution() -> None:
    result = _result(
        [
            _evidence("a", "Numeric cast detail"),
            _evidence(
                "b",
                "tests pass",
                evidence_type=EvidenceType.VERIFIED_RESULT,
                source_file="tests/test_discount.py",
            ),
        ]
    )
    analysis = EvidenceSynthesizer().synthesize(result, issue=ISSUE)
    assert analysis.tests_executed is False
    # The count is reported, not a claim that something was verified.
    assert analysis.verified_result_count == 1
    assert all(ref.evidence_type is not EvidenceType.VERIFIED_RESULT
               for ref in analysis.supporting_evidence)


def test_synthesis_requires_an_investigation() -> None:
    with pytest.raises(EvidenceSynthesisError):
        EvidenceSynthesizer().synthesize_evidence(None, issue=ISSUE)  # type: ignore[arg-type]
    with pytest.raises(EvidenceSynthesisError):
        EvidenceSynthesizer().analyze(None, EvidenceIndex(), issue=ISSUE)  # type: ignore[arg-type]


# ── The real demo defect ──────────────────────────────────────────────────────


def test_synthesis_finds_the_truncation_defect_in_the_fixture(
    investigation: InvestigationResult,
) -> None:
    synthesis, index = EvidenceSynthesizer().synthesize_evidence(
        investigation, issue=ISSUE
    )
    analysis = EvidenceSynthesizer().analyze(investigation, index, issue=ISSUE)

    assert analysis.status is RootCauseStatus.DETERMINED
    assert analysis.root_cause_kind is RootCauseKind.TRUNCATION_BEFORE_SCALING
    assert analysis.anchor is not None
    assert analysis.anchor.file_path == "app/services/discount_service.py"
    assert analysis.anchor.excerpt is not None
    # The cited line really is the defective one.
    assert "int(subtotal_cents / 100) * code.value" in analysis.anchor.excerpt
    assert analysis.root_cause is not None
    assert "app/services/discount_service.py" in analysis.root_cause
    # The intermediate product is what the next stage consumes.
    assert synthesis.numeric_sites
    assert synthesis.findings
    assert index.raw_evidence_count == len(investigation.evidence)
    assert analysis.deduplicated_evidence_count == len(index.findings)


def test_conclusion_cites_stored_evidence_ids(investigation: InvestigationResult) -> None:
    analysis = EvidenceSynthesizer().synthesize(investigation, issue=ISSUE)
    stored = {e.id for e in investigation.evidence}
    assert analysis.supporting_evidence
    for ref in analysis.supporting_evidence:
        assert ref.evidence_id in stored
        assert ref.agent
        assert ref.evidence_type in (
            EvidenceType.OBSERVATION,
            EvidenceType.HYPOTHESIS,
        )
    for link in analysis.evidence_chain:
        for evidence_id in link.evidence_ids:
            assert evidence_id in stored


def test_evidence_chain_reaches_the_defect_site(investigation: InvestigationResult) -> None:
    analysis = EvidenceSynthesizer().synthesize(investigation, issue=ISSUE)
    kinds = [link.kind for link in analysis.evidence_chain]
    assert ChainNodeKind.SITE in kinds
    assert ChainNodeKind.ROUTE in kinds
    assert kinds.index(ChainNodeKind.ROUTE) < kinds.index(ChainNodeKind.SITE)
    # The chain is a path, not a bag: every hop is labelled and anchored.
    for link in analysis.evidence_chain:
        assert link.label
        assert link.detail


def test_weak_conclusion_is_never_labelled_high(investigation: InvestigationResult) -> None:
    """Confidence must be earned, and the label must follow the number."""
    analysis = EvidenceSynthesizer().synthesize(investigation, issue=ISSUE)
    assert 0.0 <= analysis.confidence <= 1.0
    if analysis.confidence_level is ConfidenceLevel.HIGH:
        assert analysis.confidence >= 0.75
    if analysis.confidence_level is ConfidenceLevel.MEDIUM:
        assert 0.45 <= analysis.confidence < 0.75
    if analysis.confidence_level is ConfidenceLevel.LOW:
        assert analysis.confidence < 0.45


def test_confidence_breakdown_explains_the_score(
    investigation: InvestigationResult,
) -> None:
    analysis = EvidenceSynthesizer().synthesize(investigation, issue=ISSUE)
    breakdown = analysis.confidence_breakdown
    assert breakdown.rationale
    assert breakdown.base >= 0.0
    assert breakdown.corroboration_bonus >= 0.0
    assert breakdown.score == pytest.approx(
        max(0.0, min(1.0, breakdown.base + breakdown.corroboration_bonus
                     - sum(breakdown.penalties.values()))),
        abs=0.011,
    )
    assert breakdown.level is analysis.confidence_level


def test_identical_evidence_produces_an_identical_verdict(
    investigation: InvestigationResult,
) -> None:
    """Same evidence in, same verdict out — including no timestamp."""
    first = EvidenceSynthesizer().synthesize(investigation, issue=ISSUE)
    second = EvidenceSynthesizer(config=SynthesisConfig()).synthesize(
        investigation, issue=ISSUE
    )
    assert first.model_dump() == second.model_dump()


def test_synthesizer_holds_no_state_between_runs(
    investigation: InvestigationResult,
) -> None:
    shared = EvidenceSynthesizer()
    warm = shared.synthesize(investigation, issue=ISSUE)
    empty = shared.synthesize(_result([]), issue=ISSUE)
    cold = shared.synthesize(investigation, issue=ISSUE)
    assert empty.root_cause is None
    assert warm.model_dump() == cold.model_dump()


# ── Insufficient, partial and absent evidence ─────────────────────────────────


def test_no_evidence_yields_a_named_non_conclusion() -> None:
    analysis = EvidenceSynthesizer().synthesize(_result([]), issue=ISSUE)
    assert analysis.status is RootCauseStatus.NO_EVIDENCE
    assert analysis.root_cause is None
    assert analysis.root_cause_kind is None
    assert analysis.anchor is None
    assert analysis.confidence == 0.0
    assert analysis.confidence_level is ConfidenceLevel.LOW
    assert analysis.tests_executed is False
    # It must still say what it would need to answer the question.
    assert analysis.unresolved_questions
    assert analysis.recommended_next_investigation


def test_unrelated_evidence_yields_insufficient_evidence_not_a_guess() -> None:
    analysis = EvidenceSynthesizer().synthesize(
        _result(
            [
                _evidence("a", "README documents the setup steps",
                          source_file="README.md", source_line=1),
                _evidence("b", "License is MIT",
                          source_file="LICENSE", source_line=1),
            ]
        ),
        issue=ISSUE,
    )
    assert analysis.status is RootCauseStatus.INSUFFICIENT_EVIDENCE
    assert analysis.root_cause is None


def test_failed_investigation_yields_failed_and_zero_confidence() -> None:
    analysis = EvidenceSynthesizer().synthesize(
        _result([], status=InvestigationStatus.FAILED), issue=ISSUE
    )
    assert analysis.status is RootCauseStatus.FAILED
    assert analysis.root_cause is None
    assert analysis.confidence == 0.0


def _report(name: InvestigatorName, status: InvestigatorStatus) -> InvestigatorReport:
    return InvestigatorReport(
        investigator=name, status=status, evidence_count=0
    )


def test_a_failed_investigator_downgrades_a_conclusion_to_partial() -> None:
    """One blind spot must be visible in the verdict, not hidden in a log."""
    result = _result(
        [_evidence("a", "Numeric cast detail")],
        reports=[
            _report(InvestigatorName.CODE, InvestigatorStatus.COMPLETED),
            _report(InvestigatorName.TEST, InvestigatorStatus.FAILED),
            _report(InvestigatorName.DEPENDENCY, InvestigatorStatus.COMPLETED),
        ],
    )
    analysis = EvidenceSynthesizer().synthesize(result, issue=ISSUE)
    assert analysis.status is RootCauseStatus.PARTIAL
    assert analysis.partial_investigation is True
    assert analysis.missing_evidence == ["test_investigator"]
    assert any(f.category.value == "PARTIAL_INVESTIGATION"
               for f in analysis.contributing_factors)


def test_a_symbol_resolved_to_two_files_is_reported_as_a_conflict() -> None:
    """Contradictory evidence must be reported, not silently resolved.

    Two static call-graph observations resolve the same symbol to different
    files, so at most one can be right and any location derived from it is
    ambiguous.  Phase 8 surfaces that and takes a confidence penalty.
    """
    result = _result(
        [
            _evidence(
                "a",
                "Call graph edge from routes/discount.py",
                source_file="app/routes/discount.py",
                source_line=27,
                metadata={
                    "method": "ast_call_graph",
                    "callee": "discount_service.calculate_discount_cents",
                    "target_symbol": "calculate_discount_cents",
                    "target_file": "app/services/discount_service.py",
                },
            ),
            _evidence(
                "b",
                "Call graph edge from routes/cart.py",
                source_file="app/routes/cart.py",
                source_line=11,
                metadata={
                    "method": "ast_call_graph",
                    "callee": "pricing.calculate_discount_cents",
                    "target_symbol": "calculate_discount_cents",
                    "target_file": "app/services/pricing.py",
                },
            ),
        ]
    )
    analysis = EvidenceSynthesizer().synthesize(result, issue=ISSUE)
    assert analysis.conflicting_evidence
    conflict = analysis.conflicting_evidence[0]
    assert "calculate_discount_cents" in conflict.description
    assert "discount_service.py" in conflict.description
    assert "pricing.py" in conflict.description
    assert set(conflict.evidence_ids) == {"a", "b"}
    assert analysis.confidence_breakdown.penalties["conflicting_evidence"] > 0


def test_a_coverage_contradiction_is_reported_as_a_conflict() -> None:
    """A file claimed untested and covered at once makes the coverage claim useless."""
    result = _result(
        [
            _evidence(
                "a",
                "Coverage: discount_service is not imported by any test",
                agent="test_investigator",
                source_file="tests/test_discount.py",
                metadata={
                    "method": "coverage_analysis",
                    "untested_files": ["app/services/discount_service.py"],
                },
            ),
            _evidence(
                "b",
                "Import graph: discount_service is exercised",
                agent="code_investigator",
                source_file="tests/test_discount.py",
                metadata={
                    "method": "ast_import_graph",
                    "files_under_test": ["app/services/discount_service.py"],
                },
            ),
        ]
    )
    analysis = EvidenceSynthesizer().synthesize(result, issue=ISSUE)
    assert analysis.conflicting_evidence
    assert "discount_service.py" in analysis.conflicting_evidence[0].description


# ── Containment ───────────────────────────────────────────────────────────────


def test_synthesizer_reads_no_repository_file() -> None:
    """Phase 8 consumes the Phase 7 result, nothing else.

    The synthesiser module must not import filesystem, subprocess or dynamic
    execution machinery: if it did, static analysis would be able to touch the
    analysed repository or run its code.
    """
    import app.analysis.evidence_synthesizer as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    banned_modules = {
        "subprocess", "os", "shutil", "socket", "importlib", "pickle",
    }
    banned_calls = {"eval", "exec", "compile", "__import__", "open"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned_modules, alias.name
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root not in banned_modules, node.module
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in banned_calls, node.func.id


def test_synthesis_does_not_modify_the_repository(
    shop_repo: Path, investigation: InvestigationResult
) -> None:
    before = sorted((p.name, p.read_bytes()) for p in shop_repo.rglob("*") if p.is_file())
    EvidenceSynthesizer().synthesize(investigation, issue=ISSUE)
    after = sorted((p.name, p.read_bytes()) for p in shop_repo.rglob("*") if p.is_file())
    assert before == after


def test_config_bounds_are_respected(investigation: InvestigationResult) -> None:
    analysis = EvidenceSynthesizer(
        SynthesisConfig(
            max_chain_links=2, max_supporting_evidence=1, max_questions=1
        )
    ).synthesize(investigation, issue=ISSUE)
    assert len(analysis.evidence_chain) <= 2
    assert len(analysis.supporting_evidence) <= 1
    assert len(analysis.unresolved_questions) <= 1


def test_default_config_bounds_are_declared() -> None:
    for name in (
        "max_chain_links", "max_supporting_evidence", "max_candidates", "max_questions"
    ):
        assert getattr(DEFAULT_SYNTHESIS_CONFIG, name) > 0
