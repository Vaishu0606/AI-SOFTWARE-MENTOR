"""
Abstract base agent — Phase 6.

Every specialised agent in the mentor workflow (Repository Explorer, Bug
Investigator, Fix Agent, Test Agent, Verification Agent) implements the same
contract so that:

* the Orchestrator can treat all agents uniformly,
* an agent can be swapped for a deterministic stub in tests,
* the AI provider stays fully abstracted (see
  ``app.agents.providers.protocol.AIProvider``).

This module defines the contract only.  It performs no analysis, calls no
provider, and produces no findings.  Phase 6 ships no concrete LLM-backed
subclass; investigation, evidence synthesis, fix generation, test generation
and verification are later phases.

Output discipline (inherited from AGENTS.md):
an agent must never present a HYPOTHESIS as a VERIFIED_RESULT, and must never
modify repository files.  ``AgentResult.success`` is set by the agent itself;
``AgentResult.error`` is mandatory when ``success`` is False.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from app.agents.providers.protocol import AIProvider
from app.models.domain import AgentResult, EvidenceType

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """Raised when an agent cannot complete its task.

    Always carries a human-readable reason.  Never swallowed silently.
    """

    def __init__(self, agent: str, reason: str) -> None:
        self.agent = agent
        self.reason = reason
        super().__init__(f"[{agent}] {reason}")


class AgentTask(BaseModel):
    """The input handed to an agent by the orchestrator.

    ``objective`` is the agent's single goal for this invocation (for the Bug
    Investigator, the developer's issue text).  ``context`` carries
    already-verified upstream data (repository context, architecture context)
    and is treated as untrusted data, never as instructions.
    """

    run_id: str
    objective: str
    context: dict[str, Any] = Field(default_factory=dict)
    max_steps: int | None = None


class BaseAgent(ABC):
    """Abstract base for every specialised mentor agent.

    Subclasses must set :attr:`name` and implement :meth:`plan` and
    :meth:`execute`.

    Security: implementations must treat repository-derived content as
    untrusted data, must never execute repository code, and must not write to
    the repository without an explicit human approval step owned by the
    orchestrator.
    """

    #: Stable identifier used in evidence items and logs.
    name: ClassVar[str] = "base"

    #: The evidence types this agent is permitted to emit.
    allowed_evidence_types: ClassVar[frozenset[EvidenceType]] = frozenset(
        {EvidenceType.OBSERVATION}
    )

    def __init__(self, provider: AIProvider | None = None) -> None:
        """
        Args:
            provider: Optional AI provider.  Phase 6 passes ``None`` because
                no stage calls a provider.  Later phases pass the configured
                provider resolved from ``app.core.config.settings``.
        """
        self._provider = provider

    @property
    def provider(self) -> AIProvider | None:
        """The injected AI provider, if any."""
        return self._provider

    @abstractmethod
    def plan(self, task: AgentTask) -> str:
        """Return a short, deterministic description of how the task will be done.

        Planning is separate from execution so that the orchestrator can record
        intent as an event before any work happens.
        """
        ...

    @abstractmethod
    async def execute(self, task: AgentTask) -> AgentResult:
        """Perform the task and return a typed result.

        Implementations must return an ``AgentResult`` with
        ``session_id=task.run_id`` and must set ``error`` when
        ``success`` is False.
        """
        ...

    # ── Shared helpers ────────────────────────────────────────────────────

    def _assert_evidence_types(self, result: AgentResult) -> None:
        """Reject evidence this agent is not allowed to emit.

        This is the local half of the evidence-type guarantee; the orchestrator
        enforces it again when the result reaches the evidence store.
        """
        for item in result.evidence_items:
            if item.evidence_type not in self.allowed_evidence_types:
                raise AgentError(
                    self.name,
                    f"Agent emitted {item.evidence_type.value} but is only "
                    f"allowed to emit "
                    f"{sorted(e.value for e in self.allowed_evidence_types)}",
                )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r}>"
