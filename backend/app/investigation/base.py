"""
Shared investigator base — Phase 7.

Each investigator is a small, focused, **read-only** analysis pass over the
shared :class:`~app.investigation.context.InvestigationContext`.  The base class
provides the boilerplate that would otherwise be copied three times:

* a capped :class:`~app.investigation.evidence.EvidenceBuilder` owned by this
  investigator alone (which is what keeps the three independent),
* a relevance ranking computed once and reused by the investigator's own passes,
* a safe bounded source reader, and
* the guarantee that an investigator can never raise: expected failures become
  ``InvestigatorStatus.FAILED`` with a reason, so one broken investigator can
  never abort the run or take down its siblings.

A :class:`SourceWindow` is the only way an investigator may read code.  It reads
*already-loaded* text from the Phase 3 explorer and returns a numbered line
range, so every statement can cite ``file:line`` and no investigator can
accidentally walk outside the repository.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.analysis.ast_analyzer import FileAnalysis
from app.investigation.context import InvestigationContext
from app.investigation.evidence import EvidenceBuilder
from app.investigation.relevance import (
    RelevanceResult,
    build_issue_terms,
    rank_files_by_relevance,
    split_identifier,
)
from app.models.investigation import (
    InvestigationResult,
    InvestigatorName,
    InvestigatorReport,
    InvestigatorStatus,
    SourceLocation,
    SourceReference,
)

logger = logging.getLogger(__name__)


# ── Bounded source access ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceWindow:
    """A numbered view of one already-loaded source file.

    The text comes from the Phase 3 explorer, which never read secret files and
    skipped anything oversized.  Nothing here touches the filesystem, so a
    repository cannot smuggle in a path outside its own root.
    """

    file_path: str
    lines: tuple[str, ...]

    def line(self, number: int) -> str | None:
        """Return 1-indexed line ``number``, or None when out of range."""
        if 1 <= number <= len(self.lines):
            return self.lines[number - 1]
        return None

    def slice(self, start: int, end: int) -> list[tuple[int, str]]:
        """Return ``(line_number, text)`` for the inclusive range, clamped."""
        lo = max(1, start)
        hi = min(len(self.lines), end)
        return [(n, self.lines[n - 1]) for n in range(lo, hi + 1)]

    def snippet(self, line: int, context: int = 1, max_lines: int = 12) -> str | None:
        """Return a bounded, numbered snippet centred on ``line``.

        Returns None when the line does not exist, so an investigator can never
        quote a location it did not actually read.
        """
        if self.line(line) is None:
            return None
        half = max(0, context)
        rows = self.slice(line - half, line + half)
        if len(rows) > max_lines:
            rows = rows[:max_lines]
        return "\n".join(f"{n:>4} | {text}" for n, text in rows)


# ── Base investigator ──────────────────────────────────────────────────────────


class BaseInvestigator(ABC):
    """Base class for the three Phase 7 investigators.

    Subclasses implement :meth:`_run` and return their evidence.  ``investigate``
    wraps that call with timing, the evidence cap, and the no-raise guarantee.
    """

    #: Canonical identity.  Also the value written to ``Evidence.agent``.
    name: InvestigatorName

    def __init__(self, context: InvestigationContext) -> None:
        self.context = context
        self._builder = EvidenceBuilder(
            run_id=context.run_id,
            investigator=self.name,
            max_items=context.config.max_evidence_per_investigator,
        )
        self._relevance: RelevanceResult | None = None
        self._analysis_by_path: dict[str, FileAnalysis] | None = None
        self._windows: dict[str, SourceWindow] = {}

    # ── Public entry point ────────────────────────────────────────────────

    def investigate(self) -> tuple[InvestigatorReport, list]:
        """Run the investigator and return ``(report, evidence)``.

        Never raises.  A failure is reported as ``InvestigatorStatus.FAILED`` so
        the other two investigators still deliver their evidence.
        """
        from time import perf_counter  # local import keeps the hot path cheap

        started = perf_counter()
        summary: dict[str, object] = {}
        try:
            self._build_indexes()
            summary = self._run(self._builder) or {}
            status = InvestigatorStatus.COMPLETED
            error: str | None = None
        except Exception as exc:  # noqa: BLE001 - reported, never silently ignored
            logger.exception(
                "%s failed for run %s", self.name.value, self.context.run_id
            )
            status = InvestigatorStatus.FAILED
            error = f"{type(exc).__name__}: {exc}"
            summary = {}

        duration = perf_counter() - started
        if self._builder.truncated:
            summary["truncated"] = True
            summary["evidence_cap"] = self.context.config.max_evidence_per_investigator

        report = InvestigatorReport(
            investigator=self.name,
            status=status,
            duration_seconds=round(duration, 6),
            evidence_count=len(self._builder),
            error=error,
            summary=summary,
        )
        # On failure the partial evidence is dropped rather than presented as a
        # complete answer; the report still records that it happened.
        evidence = self._builder.items if status is InvestigatorStatus.COMPLETED else []
        return report, evidence

    # ── Template method ───────────────────────────────────────────────────

    @abstractmethod
    def _run(self, builder: EvidenceBuilder) -> dict[str, object]:
        """Do this investigator's work and return a summary dict."""

    # ── Shared helpers ────────────────────────────────────────────────────

    def _build_indexes(self) -> None:
        """Prepare lookup tables used by every investigator."""
        if self._analysis_by_path is None:
            self._analysis_by_path = {
                f.relative_path: f for f in self.context.ast.files
            }
        if self._relevance is None:
            terms = build_issue_terms(self.context.issue)
            self._relevance = rank_files_by_relevance(
                self.context.ast,
                terms,
                test_paths=[
                    f.relative_path for f in self.context.exploration.test_files
                ],
                max_files=self.context.config.max_relevant_files,
            )

    @property
    def relevance(self) -> RelevanceResult:
        """Relevance ranking for the issue (built on first access)."""
        self._build_indexes()
        assert self._relevance is not None  # noqa: S101 - narrowed by _build_indexes
        return self._relevance

    @property
    def builder(self) -> EvidenceBuilder:
        return self._builder

    def analysis_for(self, file_path: str) -> FileAnalysis | None:
        """Return the Phase 4 analysis of ``file_path``, or None."""
        self._build_indexes()
        assert self._analysis_by_path is not None  # noqa: S101
        return self._analysis_by_path.get(file_path)

    def window(self, file_path: str) -> SourceWindow | None:
        """Return a bounded source window, or None when unavailable.

        ``None`` means the file was never loaded: skipped as a secret, skipped
        as a binary, or too large.  That distinction is preserved rather than
        worked around.
        """
        if file_path in self._windows:
            return self._windows[file_path]
        text = self.context.snapshot.source_for(file_path)
        if text is None:
            return None
        window = SourceWindow(file_path=file_path, lines=tuple(text.splitlines()))
        self._windows[file_path] = window
        return window

    def location(
        self, file_path: str, line_start: int | None = None, line_end: int | None = None
    ) -> SourceLocation:
        """Build a :class:`SourceLocation` with a normalised end line."""
        end = line_end if line_end is not None else line_start
        return SourceLocation(
            file_path=file_path, line_start=line_start, line_end=end
        )

    def relevant_files(self, *, include_tests: bool = True) -> list[str]:
        """Issue-relevant file paths, most relevant first."""
        paths = self.relevance.file_paths()
        if include_tests:
            return paths
        return self.relevance.source_paths()

    def primary_source_file(self) -> str | None:
        """The most relevant *production* file, used to anchor hypotheses.

        A test file often scores highest overall, but a statement about how the
        product behaves belongs on the code that implements it.  Falls back to
        any relevant file so a repository of only tests still gets an anchor.
        """
        source_paths = self.relevance.source_paths()
        if source_paths:
            return source_paths[0]
        files = self.relevance.file_paths()
        return files[0] if files else None

    def term_parts(self, term: str) -> set[str]:
        """Sub-words of ``term`` (see :func:`split_identifier`)."""
        return {term} | split_identifier(term)

    @staticmethod
    def reference(file_path: str, line: int | None, detail: str) -> SourceReference:
        return SourceReference(file_path=file_path, line=line, detail=detail)


__all__ = [
    "BaseInvestigator",
    "SourceWindow",
    "InvestigationResult",
]
