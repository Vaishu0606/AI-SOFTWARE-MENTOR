"""
Phase 6 tests — the abstract agent contract.

``BaseAgent`` is the seam every later mentor agent implements.  These tests
pin down the contract itself: it is abstract, it defaults to the most
conservative evidence type, it can be constructed without an AI provider, and
it refuses to let an agent promote a HYPOTHESIS to a VERIFIED_RESULT.
"""

from __future__ import annotations

import pytest

from app.agents.base_agent import AgentError, AgentTask, BaseAgent
from app.agents.providers.protocol import AIProvider
from app.models.domain import AgentResult, Evidence, EvidenceType

TASK = AgentTask(
    run_id="11111111-1111-4111-8111-111111111111",
    objective="Checkout is failing when a discount code is applied.",
    context={"repository_name": "ShopDemo"},
)


class _StubAgent(BaseAgent):
    """Minimal concrete agent used to exercise the base class."""

    name = "stub"
    allowed_evidence_types = frozenset(
        {EvidenceType.OBSERVATION, EvidenceType.HYPOTHESIS}
    )

    def plan(self, task: AgentTask) -> str:
        return f"inspect {task.objective[:20]}"

    async def execute(self, task: AgentTask) -> AgentResult:
        return AgentResult(agent=self.name, session_id=task.run_id, success=True)


class _VerifyingAgent(_StubAgent):
    name = "verifier"
    allowed_evidence_types = frozenset(
        {
            EvidenceType.OBSERVATION,
            EvidenceType.HYPOTHESIS,
            EvidenceType.PROPOSED_ACTION,
            EvidenceType.VERIFIED_RESULT,
        }
    )


# ── Abstractness ──────────────────────────────────────────────────────────────


def test_base_agent_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        BaseAgent()  # type: ignore[abstract]


def test_incomplete_subclass_cannot_be_instantiated() -> None:
    class OnlyPlan(BaseAgent):
        name = "only-plan"

        def plan(self, task: AgentTask) -> str:
            return ""

    with pytest.raises(TypeError):
        OnlyPlan()  # type: ignore[abstract]


# ── Provider abstraction ──────────────────────────────────────────────────────


def test_agent_works_without_a_provider() -> None:
    agent = _StubAgent()
    assert agent.provider is None
    assert agent.name == "stub"


def test_agent_accepts_an_injected_provider() -> None:
    class FakeProvider(AIProvider):
        async def generate(self, prompt: str, **kwargs: object) -> str:
            return "ok"

    provider = FakeProvider()
    assert _StubAgent(provider).provider is provider


def test_phase6_ships_no_llm_backed_agent() -> None:
    """No concrete agent in Phase 6 may reach for a provider by itself."""
    from pathlib import Path

    import app.agents as agents_pkg

    root = Path(agents_pkg.__file__).parent
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "self._provider.generate(" not in source, path.name
        assert "await self._provider" not in source, path.name


# ── Evidence discipline ───────────────────────────────────────────────────────


def _result_with(evidence_type: EvidenceType) -> AgentResult:
    return AgentResult(
        agent="stub",
        session_id=TASK.run_id,
        success=True,
        evidence_items=[
            Evidence(
                session_id=TASK.run_id,
                evidence_type=evidence_type,
                agent="stub",
                title="finding",
                description="description",
            )
        ],
    )


def test_agent_may_emit_allowed_evidence_types() -> None:
    agent = _StubAgent()
    agent._assert_evidence_types(_result_with(EvidenceType.OBSERVATION))
    agent._assert_evidence_types(_result_with(EvidenceType.HYPOTHESIS))


def test_agent_cannot_escalate_a_hypothesis_to_a_verified_result() -> None:
    agent = _StubAgent()
    with pytest.raises(AgentError) as excinfo:
        agent._assert_evidence_types(_result_with(EvidenceType.VERIFIED_RESULT))
    assert "VERIFIED_RESULT" in str(excinfo.value)


def test_agent_cannot_emit_a_proposed_action_unless_allowed() -> None:
    agent = _StubAgent()
    with pytest.raises(AgentError):
        agent._assert_evidence_types(_result_with(EvidenceType.PROPOSED_ACTION))


def test_a_later_phase_agent_may_emit_a_verified_result() -> None:
    _VerifyingAgent()._assert_evidence_types(_result_with(EvidenceType.VERIFIED_RESULT))


def test_default_allowed_types_are_observations_only() -> None:
    assert BaseAgent.allowed_evidence_types == frozenset({EvidenceType.OBSERVATION})


def test_agent_error_names_the_agent() -> None:
    error = AgentError("fix", "diff was rejected")
    assert error.agent == "fix"
    assert error.reason == "diff was rejected"
    assert "[fix] diff was rejected" in str(error)


# ── Task model ────────────────────────────────────────────────────────────────


def test_task_context_defaults_to_empty() -> None:
    assert AgentTask(run_id="r", objective="o").context == {}
    assert AgentTask(run_id="r", objective="o").max_steps is None


def test_task_requires_a_run_id_and_objective() -> None:
    with pytest.raises(ValueError):
        AgentTask(objective="o")  # type: ignore[call-arg]


def test_stub_agent_round_trips_a_task() -> None:
    import asyncio

    agent = _StubAgent()
    assert agent.plan(TASK) == f"inspect {TASK.objective[:20]}"
    result = asyncio.run(agent.execute(TASK))
    assert result.success is True
    assert result.session_id == TASK.run_id
    assert result.agent == "stub"
    assert result.error is None
