"""Tests for core domain models."""

import pytest

from app.models.domain import (
    AgentResult,
    Evidence,
    EvidenceType,
    FixProposal,
    Hypothesis,
    Incident,
    Investigation,
    InvestigationTestResult,
    RepositoryInfo,
)


# ── EvidenceType ──────────────────────────────────────────────────────────────


def test_evidence_type_values() -> None:
    assert EvidenceType.OBSERVATION == "OBSERVATION"
    assert EvidenceType.HYPOTHESIS == "HYPOTHESIS"
    assert EvidenceType.PROPOSED_ACTION == "PROPOSED_ACTION"
    assert EvidenceType.VERIFIED_RESULT == "VERIFIED_RESULT"


def test_evidence_type_is_string_enum() -> None:
    # Ensures evidence types serialize correctly in JSON
    assert isinstance(EvidenceType.OBSERVATION.value, str)


# ── Evidence ──────────────────────────────────────────────────────────────────


def test_evidence_creates_with_required_fields() -> None:
    evidence = Evidence(
        session_id="sess-001",
        evidence_type=EvidenceType.OBSERVATION,
        agent="RepositoryExplorer",
        title="Found checkout_service.py",
        description="Located the checkout service module at backend/app/services/checkout_service.py",
    )
    assert evidence.session_id == "sess-001"
    assert evidence.evidence_type == EvidenceType.OBSERVATION
    assert evidence.id is not None
    assert evidence.timestamp is not None


def test_evidence_id_is_unique() -> None:
    e1 = Evidence(
        session_id="s1",
        evidence_type=EvidenceType.HYPOTHESIS,
        agent="BugInvestigator",
        title="H1",
        description="First hypothesis",
    )
    e2 = Evidence(
        session_id="s1",
        evidence_type=EvidenceType.HYPOTHESIS,
        agent="BugInvestigator",
        title="H2",
        description="Second hypothesis",
    )
    assert e1.id != e2.id


def test_evidence_optional_fields_default_to_none() -> None:
    evidence = Evidence(
        session_id="s1",
        evidence_type=EvidenceType.OBSERVATION,
        agent="Arch",
        title="T",
        description="D",
    )
    assert evidence.source_file is None
    assert evidence.source_line is None
    assert evidence.confidence is None


# ── Hypothesis ────────────────────────────────────────────────────────────────


def test_hypothesis_confidence_range() -> None:
    h = Hypothesis(
        title="Type error in discount calculation",
        description="The discount amount is a string, not a float",
        confidence=0.85,
    )
    assert 0.0 <= h.confidence <= 1.0


# ── TestResult ────────────────────────────────────────────────────────────────


def test_test_result_passed_when_exit_code_zero() -> None:
    result = InvestigationTestResult.from_execution(
        session_id="s1",
        test_files=["tests/test_checkout.py"],
        exit_code=0,
        stdout="1 passed",
        stderr="",
    )
    assert result.passed is True
    assert result.exit_code == 0


def test_test_result_failed_when_exit_code_nonzero() -> None:
    result = InvestigationTestResult.from_execution(
        session_id="s1",
        test_files=["tests/test_checkout.py"],
        exit_code=1,
        stdout="1 failed",
        stderr="AssertionError",
    )
    assert result.passed is False
    assert result.exit_code == 1


def test_test_result_passed_never_set_manually() -> None:
    """passed must be derived from exit_code, not set to True when exit_code != 0."""
    result = InvestigationTestResult.from_execution(
        session_id="s1",
        test_files=[],
        exit_code=2,
        stdout="",
        stderr="error",
    )
    assert result.passed is False


# ── Investigation ─────────────────────────────────────────────────────────────


def test_investigation_starts_idle() -> None:
    incident = Incident(
        title="Checkout bug",
        description="Checkout is failing when a discount code is applied.",
    )
    inv = Investigation(session_id="sess-001", incident=incident)
    assert inv.status == "IDLE"
    assert inv.evidence == []
    assert inv.hypotheses == []


# ── AgentResult ───────────────────────────────────────────────────────────────


def test_agent_result_success() -> None:
    result = AgentResult(
        agent="RepositoryExplorer",
        session_id="s1",
        success=True,
    )
    assert result.success is True
    assert result.error is None
    assert result.evidence_items == []


def test_agent_result_failure_has_error() -> None:
    result = AgentResult(
        agent="BugInvestigator",
        session_id="s1",
        success=False,
        error="Repository clone failed",
    )
    assert result.success is False
    assert result.error == "Repository clone failed"
