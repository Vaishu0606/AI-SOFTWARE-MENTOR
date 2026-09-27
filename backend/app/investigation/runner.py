"""
Parallel investigation runner — Phase 7.

Runs the three investigators **concurrently** and aggregates their output into a
single :class:`~app.models.investigation.InvestigationResult`.

Concurrency model
-----------------
:class:`concurrent.futures.ThreadPoolExecutor` with one worker per investigator.
Threads (not processes) are the right tool because the work is I/O-light and
read-only, and because it keeps the shared repository snapshot usable without
copying it.  The three investigators hold a *reference* to one read-only context
and own nothing but their own evidence list, so no lock is needed on the data.

Determinism despite concurrency
-------------------------------
Three deliberate choices make the output independent of thread timing:

1. results are collected in **canonical investigator order** (code, test,
   dependency), never in completion order;
2. nothing an investigator returns affects what another computes — the shared
   context is frozen, and the builder is per-investigator;
3. timestamps and durations are recorded in the reports but never used for
   ordering, filtering, or deduplication.

Failure model
-------------
One investigator failing must not lose the other two.  Each future is therefore
inspected individually, a failed investigator is recorded as
``InvestigatorStatus.FAILED`` with its reason, and the overall status becomes
``PARTIAL`` — never silently reported as success.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Iterable, Sequence

from app.investigation.base import BaseInvestigator
from app.investigation.code_investigator import CodeInvestigator
from app.investigation.context import InvestigationContext
from app.investigation.dependency_investigator import DependencyInvestigator
from app.investigation.test_investigator import TestInvestigator
from app.models.domain import Evidence
from app.models.investigation import (
    InvestigationResult,
    InvestigationStatus,
    InvestigatorName,
    InvestigatorReport,
    InvestigatorStatus,
)
from app.models.workflow import utc_now

logger = logging.getLogger(__name__)

#: Canonical presentation order.  Index is the sort key, so adding an
#: investigator later cannot reshuffle the others.
CANONICAL_ORDER: tuple[InvestigatorName, ...] = (
    InvestigatorName.CODE,
    InvestigatorName.TEST,
    InvestigatorName.DEPENDENCY,
)

#: Called for each lifecycle transition so the stage can emit workflow events.
#: ``detail`` is empty for ``RUNNING`` and carries the finished report's
#: ``evidence_count``/``error`` for terminal statuses, so a listener never has to
#: wait for the aggregate completion event to learn how much one agent found.
ProgressHook = Callable[[InvestigatorName, InvestigatorStatus, dict], None]


def default_investigator_factories() -> list[
    Callable[[InvestigationContext], BaseInvestigator]
]:
    """The three investigators, in canonical order."""
    return [CodeInvestigator, TestInvestigator, DependencyInvestigator]


class InvestigationRunner:
    """Executes investigators concurrently and aggregates their evidence."""

    def __init__(
        self,
        context: InvestigationContext,
        factories: Sequence[Callable[[InvestigationContext], BaseInvestigator]] | None = None,
        on_progress: ProgressHook | None = None,
    ) -> None:
        self._context = context
        self._factories = list(
            factories if factories is not None else default_investigator_factories()
        )
        if not self._factories:
            raise ValueError("at least one investigator factory is required")
        self._on_progress = on_progress

    def run(self) -> InvestigationResult:
        """Run every investigator and return the aggregated result."""
        started = utc_now()
        result = InvestigationResult(run_id=self._context.run_id, started_at=started)

        # Build the investigators up front so a construction error is reported
        # against a named investigator rather than aborting the whole run.
        investigators: list[BaseInvestigator] = []
        reports_by_name: dict[InvestigatorName, InvestigatorReport] = {}
        evidence_by_name: dict[InvestigatorName, list[Evidence]] = {}
        seen_names: set[InvestigatorName] = set()

        for factory in self._factories:
            investigator = factory(self._context)
            if not isinstance(investigator, BaseInvestigator):
                raise TypeError(
                    f"{type(investigator).__name__} is not a BaseInvestigator"
                )
            if investigator.name in seen_names:
                # Two investigators answering to one name would overwrite each
                # other's results in the per-name maps below.
                raise ValueError(
                    f"duplicate investigator name: {investigator.name.value}"
                )
            seen_names.add(investigator.name)
            investigators.append(investigator)

        # ── Announce every start before any thread runs ────────────────────
        # Emitted in canonical order so the event log is deterministic even
        # though the work below is not.
        for investigator in self._sorted(investigators):
            self._notify(investigator.name, InvestigatorStatus.RUNNING)

        # ── Run them concurrently ─────────────────────────────────────────
        with ThreadPoolExecutor(
            max_workers=len(investigators),
            thread_name_prefix="investigator",
        ) as pool:
            futures: dict[Future, BaseInvestigator] = {
                pool.submit(investigator.investigate): investigator
                for investigator in self._sorted(investigators)
            }
            for future, investigator in futures.items():
                try:
                    report, evidence = future.result()
                except Exception as exc:  # noqa: BLE001 - one bad agent, not a bad run
                    logger.exception(
                        "Investigator %s crashed", investigator.name.value
                    )
                    report = InvestigatorReport(
                        investigator=investigator.name,
                        status=InvestigatorStatus.FAILED,
                        evidence_count=0,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    evidence = []
                reports_by_name[investigator.name] = report
                evidence_by_name[investigator.name] = list(evidence)
                self._notify(
                    investigator.name,
                    (
                        report.status
                        if report.status is InvestigatorStatus.COMPLETED
                        else InvestigatorStatus.FAILED
                    ),
                    {
                        "evidence_count": report.evidence_count,
                        "error": report.error,
                        "duration_seconds": report.duration_seconds,
                    },
                )

        # ── Aggregate deterministically ───────────────────────────────────
        for name in self._canonical_names(investigators):
            report = reports_by_name.get(name)
            if report is None:  # pragma: no cover - every name gets a report
                report = InvestigatorReport(
                    investigator=name,
                    status=InvestigatorStatus.FAILED,
                    error="Investigator produced no report",
                )
            result.investigators.append(report)
            result.evidence.extend(evidence_by_name.get(name, []))
            if report.status is InvestigatorStatus.FAILED:
                result.errors.append(
                    f"{name.value}: {report.error or 'unknown error'}"
                )

        result.status = self._overall_status(result.investigators, result.evidence)
        result.completed_at = utc_now()
        logger.info(
            "Investigation for run %s finished: %s (%d evidence item(s) from %d/%d "
            "investigator(s))",
            self._context.run_id,
            result.status.value,
            len(result.evidence),
            len(result.completed_investigators),
            len(result.investigators),
        )
        return result

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _sorted(
        investigators: Iterable[BaseInvestigator],
    ) -> list[BaseInvestigator]:
        """Order investigators canonically, unknown names last by value."""
        order = {name: i for i, name in enumerate(CANONICAL_ORDER)}
        return sorted(
            investigators,
            key=lambda inv: (order.get(inv.name, len(order)), inv.name.value),
        )

    @classmethod
    def _canonical_names(
        cls, investigators: Iterable[BaseInvestigator]
    ) -> list[InvestigatorName]:
        return [inv.name for inv in cls._sorted(investigators)]

    @staticmethod
    def _overall_status(
        reports: Sequence[InvestigatorReport], evidence: Sequence[Evidence]
    ) -> InvestigationStatus:
        """Decide the aggregate status from the per-investigator reports.

        The rules, in order:

        * every investigator failed → ``FAILED``
        * some succeeded and some failed → ``PARTIAL`` (never silently
          ``COMPLETED``: a partial investigation must stay visible as partial)
        * nobody failed but nothing was found → ``FAILED``, because a run that
          produced no evidence has told the developer nothing and must not be
          allowed to present as a successful investigation
        * otherwise → ``COMPLETED``
        """
        if not reports:
            return InvestigationStatus.FAILED
        completed = [r for r in reports if r.status is InvestigatorStatus.COMPLETED]
        failed = [r for r in reports if r.status is InvestigatorStatus.FAILED]
        if failed and completed:
            return InvestigationStatus.PARTIAL
        if failed and not completed:
            return InvestigationStatus.FAILED
        if not evidence:
            return InvestigationStatus.FAILED
        return InvestigationStatus.COMPLETED

    def _notify(
        self,
        name: InvestigatorName,
        status: InvestigatorStatus,
        detail: dict | None = None,
    ) -> None:
        if self._on_progress is None:
            return
        try:
            self._on_progress(name, status, detail or {})
        except Exception:  # noqa: BLE001 - reporting must never break the run
            logger.exception("Investigation progress hook failed for %s", name.value)


def run_investigation(
    context: InvestigationContext,
    factories: Sequence[Callable[[InvestigationContext], BaseInvestigator]] | None = None,
    on_progress: ProgressHook | None = None,
) -> InvestigationResult:
    """Convenience wrapper: build a runner and run it."""
    return InvestigationRunner(context, factories=factories, on_progress=on_progress).run()


__all__ = [
    "CANONICAL_ORDER",
    "ProgressHook",
    "InvestigationRunner",
    "default_investigator_factories",
    "run_investigation",
]
