"""
Shared investigation context — Phase 7.

Responsibilities
----------------
1. Build **one** repository snapshot per repository root by reusing the existing
   Phase 3 / 4 / 5 layers, and cache it so the repository is walked and parsed
   once per process rather than once per investigator.
2. Wrap that snapshot in an :class:`InvestigationContext` that investigators can
   read but not mutate, so three investigators running in parallel threads can
   safely share it.

Why a shared snapshot
---------------------
"Investigators must be independent" and "do not copy the entire repository into
every agent" point at the same design: build the expensive analysis once, hand
the same read-only object to all three investigators, and let each investigator
own only its own output (evidence list + report).  Independence is then a
property of the investigators, not of duplicated inputs.

Security
--------
* The repository root is resolved through the Phase 3 ``RepositoryCloner``, so a
  bad path fails before anything is read.
* Source text comes from the Phase 3 explorer, which never reads secret files,
  skips binaries, and enforces a size limit.  Nothing here bypasses that.
* The snapshot exposes source text through a read-only mapping, so no
  investigator can mutate shared state.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from app.agents.architecture import ArchitectureAgent, ArchitectureAnalysis
from app.analysis.ast_analyzer import ASTAnalyzer, RepositoryASTAnalysis
from app.ingestion.cloner import LocalSource, RepositoryCloner
from app.ingestion.explorer import RepositoryExploration, RepositoryExplorer
from app.models.investigation import InvestigationConfig

logger = logging.getLogger(__name__)


# ── Repository snapshot ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class RepositorySnapshot:
    """Immutable bundle of every pre-existing analysis of one repository.

    ``source_by_path`` maps repository-relative POSIX paths to the file text that
    the Phase 3 explorer already read.  It is exposed as a read-only mapping so
    parallel investigators cannot write to shared state.
    """

    root: Path
    name: str
    exploration: RepositoryExploration
    ast: RepositoryASTAnalysis
    architecture: ArchitectureAnalysis
    source_by_path: Mapping[str, str] = field(repr=False)

    @property
    def file_count(self) -> int:
        return self.exploration.total_files

    def source_for(self, relative_path: str) -> str | None:
        """Return already-loaded source text, or None when unavailable.

        Never touches the filesystem: the text was read (and size-checked, and
        secret-checked) once by the Phase 3 explorer.
        """
        return self.source_by_path.get(relative_path)

    def lines_for(self, relative_path: str) -> list[str]:
        """Return the file's lines, or an empty list when unavailable."""
        text = self.source_for(relative_path)
        return text.splitlines() if text is not None else []


# ── Snapshot cache ─────────────────────────────────────────────────────────────

_SNAPSHOT_CACHE: dict[str, RepositorySnapshot] = {}
_SNAPSHOT_LOCK = threading.Lock()


def clear_snapshot_cache() -> None:
    """Drop every cached snapshot.

    Called by the test suite so a repository rebuilt inside a test is never
    served from a previous test's analysis.
    """
    with _SNAPSHOT_LOCK:
        _SNAPSHOT_CACHE.clear()


def build_snapshot(
    repository_path: str | Path,
    *,
    explorer: RepositoryExplorer | None = None,
    analyzer: ASTAnalyzer | None = None,
    architecture_agent: ArchitectureAgent | None = None,
    cloner: RepositoryCloner | None = None,
    use_cache: bool = True,
) -> RepositorySnapshot:
    """Explore, AST-analyze and architecture-analyze a repository exactly once.

    The three layers are the existing Phase 3 / 4 / 5 implementations; nothing
    here re-implements them.

    Raises:
        IngestionError: When the path is not a readable directory.
    """
    cloner = cloner or RepositoryCloner()
    root = cloner.prepare(LocalSource(path=str(repository_path)))

    key = str(root)
    if use_cache:
        with _SNAPSHOT_LOCK:
            cached = _SNAPSHOT_CACHE.get(key)
        if cached is not None:
            return cached

    explorer = explorer or RepositoryExplorer()
    # read_source=True is what makes the text available to the investigators.
    # The explorer applies every guard here: secrets are never read, binaries
    # are skipped, and oversized files are skipped.
    exploration = explorer.explore(root, read_source=True)

    analyzer = analyzer or ASTAnalyzer()
    ast_result = analyzer.analyze_repository(
        root=str(root),
        name=root.name,
        file_sources=[
            (ev.relative_path, ev.content, ev.language)
            for ev in exploration.source_evidence
        ],
    )

    architecture_agent = architecture_agent or ArchitectureAgent()
    architecture = architecture_agent.analyze(ast_result, exploration)

    snapshot = RepositorySnapshot(
        root=root,
        name=root.name,
        exploration=exploration,
        ast=ast_result,
        architecture=architecture,
        source_by_path=MappingProxyType(
            {ev.relative_path: ev.content for ev in exploration.source_evidence}
        ),
    )

    if use_cache:
        with _SNAPSHOT_LOCK:
            _SNAPSHOT_CACHE[key] = snapshot
    logger.info(
        "Built repository snapshot for %s (%d file(s), %d with source)",
        root,
        snapshot.file_count,
        len(snapshot.source_by_path),
    )
    return snapshot


# ── Investigation context ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class InvestigationContext:
    """Everything the three investigators are allowed to see.

    The issue text is carried as **data**, never as instructions: a repository
    or a bug report may both contain prompt-injection text, and nothing in
    ``app.investigation`` feeds text to an LLM.
    """

    run_id: str
    issue: str
    snapshot: RepositorySnapshot
    config: InvestigationConfig = field(default_factory=InvestigationConfig)

    @property
    def repository_root(self) -> Path:
        return self.snapshot.root

    @property
    def repository_name(self) -> str:
        return self.snapshot.name

    @property
    def exploration(self) -> RepositoryExploration:
        return self.snapshot.exploration

    @property
    def ast(self) -> RepositoryASTAnalysis:
        return self.snapshot.ast

    @property
    def architecture(self) -> ArchitectureAnalysis:
        return self.snapshot.architecture

    @classmethod
    def from_run(
        cls,
        run_id: str,
        issue: str,
        repository_path: str | Path,
        config: InvestigationConfig | None = None,
    ) -> "InvestigationContext":
        """Build a context for one run.

        Raises:
            IngestionError: When the repository path cannot be validated.
        """
        return cls(
            run_id=run_id,
            issue=issue,
            snapshot=build_snapshot(repository_path),
            config=config or InvestigationConfig(),
        )


__all__ = [
    "InvestigationContext",
    "RepositorySnapshot",
    "build_snapshot",
    "clear_snapshot_cache",
]
