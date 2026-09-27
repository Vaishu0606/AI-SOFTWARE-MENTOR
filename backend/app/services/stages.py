"""
Workflow stages — Phase 6, extended through Phase 11.

A stage is the unit of work the orchestrator executes for one workflow state.

Contract
--------
    StageHandler.execute(context) -> StageResult

A stage is registered against exactly one state, and that state is the state
the run occupies once the stage has succeeded.  ``StageResult.next_state``
declares the same thing from the handler's side; the orchestrator cross-checks
the declaration against its own state machine and refuses to advance if they
disagree, so a stage can never steer the workflow.

``StageResult`` is honest by construction:

* ``StageOutcome.COMPLETED`` — the stage really ran.
* ``StageOutcome.PENDING`` — the stage is a known-but-unimplemented
  placeholder, **or** it is a state no stage may execute because a human owns
  it.  Either way: no state change, no evidence, no repository access.
* ``StageOutcome.FAILED`` — unrecoverable error; the orchestrator fails the run,
  or moves it to the stage's declared ``failure_state``.

Implemented states
------------------
``REPOSITORY_CONTEXT``    → Phase 3 (RepositoryExplorer)       — implemented
``ARCHITECTURE_CONTEXT``  → Phase 5 (ArchitectureAgent)       — implemented
``INVESTIGATION_PENDING`` → Phase 7 parallel investigation      — implemented
``EVIDENCE_PENDING``      → Phase 8 evidence synthesis         — implemented
``ROOT_CAUSE_ANALYSIS``   → Phase 8 root-cause analysis        — implemented
``ROOT_CAUSE_READY``      → Phase 8→9 hand-off validation      — implemented
``FIX_PROPOSAL``          → Phase 9 fix proposal and diff      — implemented
``FIX_PROPOSAL_READY``    → the approval gate                  — no stage, by design
``FIX_APPLYING``          → Phase 11 write of an approved diff — implemented
``FIX_APPLIED``           → Phase 11 confirmation of the write — implemented
``TEST_PLAN``             → Phase 10 coverage gaps and plan    — implemented
``VERIFICATION``          → Phase 11 controlled test execution — implemented

``FIX_APPROVED`` has no stage at all, and neither does ``VERIFIED``: the first is
reached only by ``MentorOrchestrator.approve_fix`` and the second only by the
orchestrator resolving a recorded verification result.  A state with no handler is
a state the workflow cannot pass on its own, which is the mechanism behind both
the approval gate and the verified-result boundary.

So a default run still walks only to ``FIX_PROPOSAL_READY`` and stops there.
Phases 1–10 have really run; what happens next is a human's decision, and the
only way to make it is to call the approve endpoint.

Security
--------
Phases 1–10 stages never execute repository code, spawn a subprocess, call
``eval``/``exec``, dynamically import repository modules, or write to the
repository.  Evidence produced there is always typed ``EvidenceType.OBSERVATION``
except in Phase 7, which additionally emits clearly-labelled ``HYPOTHESIS`` and
investigative ``PROPOSED_ACTION`` items — never ``VERIFIED_RESULT``, and never a
declared root cause.  Phase 8 adds no evidence of its own: it only *reads* Phase 7
evidence and records an analysis that cites it by stable ID.  Phase 9 reads one
constrained, repository-relative source file to build a diff that reflects the
file as it is on disk, and still writes nothing anywhere.  Phase 10 reads nothing
at all: it derives the plan from evidence already on the run, and its only
evidence is ``PROPOSED_ACTION``.

Phase 11 adds the two capabilities that were previously absent, and they are
confined to two stages:

* ``FixApplicationStage`` writes the stored proposal to the repository, and only
  after an approval bound to that exact proposal by content fingerprint exists.
* ``VerificationStage`` runs the repository's existing test files through the
  controlled executor, and is the only component in the system permitted to
  produce a ``VERIFIED_RESULT``.

Both are covered by ``backend/tests/test_phase11_security.py``, which asserts
that no *other* Phase 11 module can write, and that only the executor can
subprocess.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from app.agents.architecture import ArchitectureAgent
from app.ingestion.cloner import IngestionError, LocalSource, RepositoryCloner
from app.ingestion.explorer import RepositoryExplorer
from app.models.domain import Evidence, EvidenceType
from app.models.workflow import MentorRun, StageOutcome, StageResult, WorkflowState

logger = logging.getLogger(__name__)


# ── Stage contract ────────────────────────────────────────────────────────────


#: A stage's progress reporter.  ``(event_type, message, payload) -> event_id|None``
StageEmitter = Callable[[str, str, dict[str, Any]], str | None]


@dataclass
class StageContext:
    """Everything a stage is allowed to read and write.

    ``evidence`` and ``data`` are owned by the orchestrator, which decides
    whether to persist them based on the returned ``StageResult``.

    ``emit`` is an optional reporter supplied by the orchestrator so a stage can
    publish *progress* events while it runs, not just one result at the end.
    Phase 7 needs it: a stage that runs three investigators in parallel would
    otherwise be opaque until it finished.  Its signature is
    ``(event_type_name, message, payload)`` — a name rather than a
    ``WorkflowEventType`` so this module stays free of event-type coupling, and
    it returns the event ID the orchestrator assigned, or None when the
    orchestrator is not collecting events.  A stage that reports progress must
    still return its truth in ``StageResult``; ``emit`` is a notification, never
    a substitute for the result.
    """

    run: MentorRun
    evidence: list[Evidence] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    emit: StageEmitter | None = None


@runtime_checkable
class StageHandler(Protocol):
    """The interface every stage implements."""

    state: WorkflowState

    def execute(self, context: StageContext) -> StageResult:
        """Perform this stage's work for ``context.run``.

        Implementations must not raise for expected failures — return
        ``StageResult.failed(...)`` instead.  Unexpected exceptions are still
        caught by the orchestrator, which fails the run safely.
        """
        ...


# ── Implemented stages (Phase 3 + Phase 5 reuse) ──────────────────────────────


class RepositoryContextStage:
    """Gathers repository context using the existing Phase 3 explorer.

    Read-only.  Validates the repository reference through ``RepositoryCloner``
    and records file counts, languages and test-file locations as OBSERVATION
    evidence.  No file is read for its secrets and no file is written.
    """

    state = WorkflowState.REPOSITORY_CONTEXT

    def __init__(self) -> None:
        self._cloner = RepositoryCloner()
        self._explorer = RepositoryExplorer()

    def execute(self, context: StageContext) -> StageResult:
        run = context.run
        try:
            repo_root = self._cloner.prepare(LocalSource(path=run.repository_path))
            exploration = self._explorer.explore(repo_root, read_source=False)
        except IngestionError as exc:
            return StageResult.failed(
                "Repository context could not be established.",
                exc.reason,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never silently ignored
            logger.exception("RepositoryContextStage failed for run %s", run.run_id)
            return StageResult.failed(
                "Repository context could not be established.",
                f"{type(exc).__name__}: {exc}",
            )

        agent = "repository_context"
        session_id = run.run_id
        evidence = [
            Evidence(
                session_id=session_id,
                evidence_type=EvidenceType.OBSERVATION,
                agent=agent,
                title=f"Repository '{exploration.name}' contains "
                      f"{exploration.total_files} file(s)",
                description=(
                    f"Observed {exploration.total_files} file(s) across "
                    f"{len(exploration.directories)} directory/directories in "
                    f"{exploration.root}. Languages: "
                    f"{', '.join(sorted(exploration.languages)) or 'unknown'}."
                ),
                source_file=str(repo_root),
                confidence=None,
                metadata={
                    "total_files": exploration.total_files,
                    "primary_language": exploration.primary_language,
                    "languages": dict(exploration.languages),
                },
            ),
            Evidence(
                session_id=session_id,
                evidence_type=EvidenceType.OBSERVATION,
                agent=agent,
                title=f"{len(exploration.test_files)} test file(s) identified",
                description=(
                    "Test files located during repository exploration: "
                    + (
                        ", ".join(f.relative_path for f in exploration.test_files[:20])
                        or "none"
                    )
                ),
                confidence=None,
                metadata={
                    # This item aggregates across many files, so it is a
                    # repository-level observation rather than one anchored to a
                    # single source location.
                    "repository_level": True,
                    "test_files": [
                        f.relative_path for f in exploration.test_files
                    ],
                    "skipped_files": [
                        {"relative_path": s.relative_path, "reason": s.reason}
                        for s in exploration.skipped_files
                    ],
                },
            ),
        ]

        data = {
            "repository_name": exploration.name,
            "repository_root": exploration.root,
            "total_files": exploration.total_files,
            "total_directories": len(exploration.directories),
            "primary_language": exploration.primary_language,
            "languages": dict(exploration.languages),
            "test_file_count": len(exploration.test_files),
            "source_file_count": len(exploration.source_files),
            "config_file_count": len(exploration.config_files),
            "secret_files_detected_not_read": [
                f.relative_path for f in exploration.files if f.is_secret
            ],
        }

        return StageResult.completed(
            f"Repository context established for '{exploration.name}' "
            f"({exploration.total_files} file(s)).",
            next_state=self.state,
            evidence=evidence,
            **data,
        )


class ArchitectureContextStage:
    """Builds architecture context using the existing Phase 5 agent.

    Reuses ``ArchitectureAgent.analyze_path`` (Phase 3 → Phase 4 → Phase 5).
    Read-only and static: no code from the repository is executed or imported.
    """

    state = WorkflowState.ARCHITECTURE_CONTEXT

    def __init__(self) -> None:
        self._agent = ArchitectureAgent()

    def execute(self, context: StageContext) -> StageResult:
        run = context.run
        try:
            repo_root = Path(run.repository_path).expanduser().resolve()
            analysis = self._agent.analyze_path(repo_root)
        except Exception as exc:  # noqa: BLE001 - reported, never silently ignored
            logger.exception("ArchitectureContextStage failed for run %s", run.run_id)
            return StageResult.failed(
                "Architecture context could not be established.",
                f"{type(exc).__name__}: {exc}",
            )

        session_id = run.run_id
        agent = "architecture_context"
        module_names = [m.name for m in analysis.modules]

        evidence = [
            Evidence(
                session_id=session_id,
                evidence_type=EvidenceType.OBSERVATION,
                agent=agent,
                title=(
                    f"Architecture analysis of '{analysis.repository_name}' "
                    f"({analysis.total_files} file(s), "
                    f"{analysis.total_routes} route(s))"
                ),
                description=(
                    f"Detected project type '{analysis.project_type}' with "
                    f"{len(analysis.modules)} module(s) and "
                    f"{len(analysis.entry_points)} entry point(s)."
                ),
                source_file=analysis.repository_root,
                confidence=None,
                metadata={
                    "project_type": analysis.project_type,
                    "detected_languages": analysis.detected_languages,
                    "total_files": analysis.total_files,
                    "total_functions": analysis.total_functions,
                    "total_classes": analysis.total_classes,
                    "total_routes": analysis.total_routes,
                },
            ),
            Evidence(
                session_id=session_id,
                evidence_type=EvidenceType.OBSERVATION,
                agent=agent,
                title=f"{len(analysis.modules)} architectural module(s) mapped",
                description="Modules identified: " + (", ".join(module_names) or "none"),
                confidence=None,
                metadata={
                    # Aggregates the whole module map, so this is a
                    # repository-level observation, not a single-file one.
                    "repository_level": True,
                    "modules": module_names,
                },
            ),
        ]

        data = {
            "project_type": analysis.project_type,
            "detected_languages": analysis.detected_languages,
            "modules": module_names,
            "entry_points": [
                {"file_path": e.file_path, "kind": e.kind, "framework": e.framework}
                for e in analysis.entry_points
            ],
            "route_count": analysis.total_routes,
            "routes": [
                {"http_method": r.http_method, "path": r.path, "handler": r.handler}
                for r in analysis.routes
            ],
            "dependency_edge_count": len(analysis.dependencies),
            "data_flow_count": len(analysis.data_flows),
            "finding_count": len(analysis.findings),
            "total_files": analysis.total_files,
            "total_functions": analysis.total_functions,
            "total_classes": analysis.total_classes,
        }

        return StageResult.completed(
            f"Architecture context established: {len(analysis.modules)} module(s), "
            f"{analysis.total_routes} route(s).",
            next_state=self.state,
            evidence=evidence,
            **data,
        )


# ── Placeholder stages (later phases) ────────────────────────────────────────


class PlaceholderStage:
    """A stage whose implementation belongs to a later phase.

    It is deliberately incapable of producing a result that could be mistaken
    for real work:

    * no repository access at all
    * no evidence items
    * no state change (``next_state`` is ``None``)
    * ``data["implemented"] is False`` and ``data["target_phase"]`` names the
      phase that will implement it
    """

    def __init__(self, state: WorkflowState, target_phase: int, description: str) -> None:
        self.state = state
        self._target_phase = target_phase
        self._description = description

    def execute(self, context: StageContext) -> StageResult:
        return StageResult.pending(
            f"{self.state.value} is not implemented yet. Scheduled for Phase "
            f"{self._target_phase}: {self._description}. No analysis was "
            f"performed and no findings were produced.",
            stage=self.state.value,
            target_phase=self._target_phase,
        )


def _placeholder(
    state: WorkflowState, phase: int, description: str
) -> PlaceholderStage:
    return PlaceholderStage(state, phase, description)


class ApprovalGateStage(PlaceholderStage):
    """The human approval gate. Registered for ``FIX_PROPOSAL_READY``.

    Not a placeholder in the "nobody has written this yet" sense — the gate is
    implemented, in
    :meth:`app.services.orchestrator.MentorOrchestrator.approve_fix`, and it works.
    What is deliberately *not* implemented is a stage that could cross it, because
    the whole property being asserted is that nothing does so on the server's own
    initiative.

    So this handler reports ``PENDING`` and stays silent, and its message says what
    is actually true: a proposal is waiting, and a human has to approve it.  An
    earlier version inherited ``PlaceholderStage``'s "not implemented yet,
    scheduled for Phase N" wording, which Phase 11 made false; a demo that printed
    it would have been telling the user a stage was missing while the feature they
    were using was working.

    Still a ``PlaceholderStage`` subclass, and still reports ``implemented is
    False``: no *automated* stage implements this state.
    """

    def __init__(self) -> None:
        super().__init__(
            WorkflowState.FIX_PROPOSAL_READY,
            11,
            "no stage may apply a fix; approval is a human action",
        )

    def execute(self, context: StageContext) -> StageResult:
        run = context.run
        return StageResult.pending(
            f"A fix proposal for run {run.run_id} is waiting for review. Nothing "
            f"was written and no test was run. To continue, a human must approve "
            f"this exact proposal through the approval endpoint, which moves the "
            f"run to FIX_APPROVED; the workflow cannot pass this point by itself.",
            stage=self.state.value,
            awaiting_human_approval=True,
            approval_endpoint=f"POST /api/mentor/runs/{run.run_id}/approve-fix",
        )


# ── Stage registry ────────────────────────────────────────────────────────────


def build_default_stages() -> dict[WorkflowState, StageHandler]:
    """Return the stage registry, keyed by the state it serves.

    Dependency injection point: callers (and later phases) may pass a modified
    registry to ``MentorOrchestrator`` to replace any stage.

    Note what is *absent*, because the absences are load-bearing:

    * ``FIX_APPROVED`` — no handler, so ``advance()`` from the gate stalls.  The
      gate is the orchestrator's ``approve_fix``, not a stage.
    * ``VERIFIED`` — no handler, so nothing can move a run there except the
      orchestrator resolving a recorded verification result.

    A state missing from this dict is a state the workflow cannot leave on its own.
    """
    # Imported here rather than at module scope: these stage modules import this
    # one for StageContext, so a top-level import would be circular.
    from app.services.fix_application_stage import (
        FixApplicationStage,
        FixAppliedStage,
    )
    from app.services.fix_proposal_stage import FixProposalStage, RootCauseReadyStage
    from app.services.investigation_stage import InvestigationStage
    from app.services.root_cause_stage import (
        EvidenceSynthesisStage,
        RootCauseAnalysisStage,
    )
    from app.services.test_plan_stage import TestPlanStage
    from app.services.verification_stage import VerificationStage

    return {
        RepositoryContextStage.state: RepositoryContextStage(),
        ArchitectureContextStage.state: ArchitectureContextStage(),
        InvestigationStage.state: InvestigationStage(),
        EvidenceSynthesisStage.state: EvidenceSynthesisStage(),
        RootCauseAnalysisStage.state: RootCauseAnalysisStage(),
        RootCauseReadyStage.state: RootCauseReadyStage(),
        FixProposalStage.state: FixProposalStage(),
        WorkflowState.FIX_PROPOSAL_READY: ApprovalGateStage(),
        FixApplicationStage.state: FixApplicationStage(),
        FixAppliedStage.state: FixAppliedStage(),
        TestPlanStage.state: TestPlanStage(),
        VerificationStage.state: VerificationStage(),
    }

