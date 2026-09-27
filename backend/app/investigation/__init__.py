"""
Phase 7 parallel bug investigation.

Three independent, read-only investigators run **concurrently** against a shared,
immutable repository snapshot and emit typed evidence:

* :class:`~app.investigation.code_investigator.CodeInvestigator` — which code the
  issue points at, and how it is wired together
* :class:`~app.investigation.test_investigator.TestInvestigator` — what the
  existing suite covers, and what it does not
* :class:`~app.investigation.dependency_investigator.DependencyInvestigator` —
  the static execution path and where its parts meet

:func:`~app.investigation.runner.run_investigation` executes them in parallel and
returns one :class:`~app.models.investigation.InvestigationResult`.

Properties this package is built to guarantee
---------------------------------------------
deterministic
    Same repository plus same issue ⇒ same result.  No LLM, no clock-dependent
    branching, no randomness, and canonical ordering of results.
read-only
    Nothing here executes, imports, or writes repository code, and no path
    outside the validated repository root is ever read.
parallel
    The three investigators run on separate threads and share one frozen context.
honest
    Only ``OBSERVATION``, ``HYPOTHESIS`` and ``PROPOSED_ACTION`` may be emitted.
    ``VERIFIED_RESULT`` is rejected, no root cause is declared, and no test is
    run — declaring a root cause is Phase 8's job, verification is Phase 11's.
"""

from app.investigation.base import BaseInvestigator, SourceWindow
from app.investigation.code_investigator import CodeInvestigator
from app.investigation.context import (
    InvestigationContext,
    RepositorySnapshot,
    build_snapshot,
    clear_snapshot_cache,
)
from app.investigation.dependency_investigator import DependencyInvestigator
from app.investigation.evidence import (
    EvidenceBuilder,
    EvidencePolicyError,
    build_evidence,
)
from app.investigation.modules import ModuleIndex, module_name_for
from app.investigation.relevance import (
    ConceptCluster,
    IssueTerms,
    RelevanceResult,
    build_issue_terms,
    rank_files_by_relevance,
)
from app.investigation.runner import (
    CANONICAL_ORDER,
    InvestigationRunner,
    run_investigation,
)
from app.investigation.test_investigator import TestInvestigator

__all__ = [
    "CANONICAL_ORDER",
    "BaseInvestigator",
    "CodeInvestigator",
    "ConceptCluster",
    "DependencyInvestigator",
    "EvidenceBuilder",
    "EvidencePolicyError",
    "InvestigationContext",
    "InvestigationRunner",
    "IssueTerms",
    "ModuleIndex",
    "RelevanceResult",
    "RepositorySnapshot",
    "SourceWindow",
    "TestInvestigator",
    "build_evidence",
    "build_issue_terms",
    "build_snapshot",
    "clear_snapshot_cache",
    "module_name_for",
    "rank_files_by_relevance",
    "run_investigation",
]
