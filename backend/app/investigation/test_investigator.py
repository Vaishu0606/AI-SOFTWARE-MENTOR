"""
Test Investigator — Phase 7.

Answers: *what does the existing test suite already cover for the reported
issue, and what does it not?*

What it emits
-------------
* ``OBSERVATION`` — which test files exist, which tests each declares, which
  production modules each test file imports, and therefore which issue-relevant
  modules are exercised directly.
* ``HYPOTHESIS`` — coverage gaps derived from those facts: a concept whose module
  no test imports, two issue-relevant concepts that no single test exercises
  together, or a route layer with no test at all.
* ``PROPOSED_ACTION`` — the next investigative step, e.g. "reproduce the reported
  combination in a scratch test".  It never proposes editing production code.

The hard limit
--------------
**Phase 7 does not run tests.**  No test is executed, so this investigator must
not claim that a test passes, fails, or is flaky.  ``tests_executed`` is
``False`` in the report summary and is asserted by the test suite, because
mistaking "a test exists" for "the behaviour is correct" is precisely the
confusion this phase must not introduce.
"""

from __future__ import annotations

from app.investigation.base import BaseInvestigator
from app.investigation.evidence import EvidenceBuilder
from app.investigation.modules import ModuleIndex
from app.models.investigation import InvestigatorName

#: How many test files to describe in full.
MAX_TEST_FILES_DESCRIBED = 10

#: How many distinct concept pairs may become "never tested together" hypotheses.
MAX_GAP_HYPOTHESES = 4


class TestInvestigator(BaseInvestigator):
    """Maps the reported issue onto the existing test suite."""

    name = InvestigatorName.TEST

    #: This is an investigator, not a pytest test class.  Without this, pytest
    #: tries to collect it by name and warns about its constructor.
    __test__ = False

    def _run(self, builder: EvidenceBuilder) -> dict[str, object]:
        snapshot = self.context.snapshot
        index = ModuleIndex.build(self.context.ast)
        test_files = [
            f.relative_path
            for f in snapshot.exploration.test_files[: self.context.config.max_test_files]
        ]

        summary: dict[str, object] = {
            "test_file_count": len(snapshot.exploration.test_files),
            "test_files_examined": len(test_files),
            "tests_executed": False,
            "execution_justification": (
                "Phase 7 executes no repository code, so no pass/fail claim is made."
            ),
        }

        if not test_files:
            builder.observation(
                title="The repository contains no test files",
                description=(
                    f"Repository exploration of {snapshot.name} found "
                    f"{len(snapshot.exploration.test_files)} test file(s). With no "
                    "existing tests, the reported behaviour is unverified by the "
                    "repository itself, so any investigation starts without a "
                    "passing reference point. This is a statement about the "
                    "repository, not about the bug."
                ),
                repository_level=True,
                method="test_inventory",
                extra_metadata={"test_file_count": 0},
            )
            builder.hypothesis(
                title="The reported behaviour has no existing test coverage",
                description=(
                    "No test file exists, so nothing in the repository pins the "
                    "expected behaviour of the reported area. Whether the reported "
                    "behaviour is correct or not cannot be judged from the test suite."
                ),
                repository_level=True,
                confidence=0.7,
                supports=[],
                method="coverage_analysis",
            )
            summary["tests_found"] = 0
            return summary

        inventory_ids, declared_tests = self._report_inventory(builder, test_files)
        covered_ids, covered_files = self._report_coverage(builder, index, test_files)
        self._report_gaps(builder, test_files, declared_tests, covered_files)
        self._report_next_steps(builder, test_files)

        summary["tests_found"] = sum(len(t) for t in declared_tests.values())
        summary["production_files_under_test"] = len(covered_files)
        summary["evidence_inventory"] = len(inventory_ids)
        summary["evidence_coverage"] = len(covered_ids)
        return summary

    # ── 1. What tests exist ────────────────────────────────────────────────

    def _report_inventory(
        self, builder: EvidenceBuilder, test_files: list[str]
    ) -> tuple[list[str], dict[str, list[str]]]:
        ids: list[str] = []
        declared: dict[str, list[str]] = {}

        for file_path in test_files[:MAX_TEST_FILES_DESCRIBED]:
            analysis = self.analysis_for(file_path)
            if analysis is None:
                continue
            test_names = [f.name for f in analysis.functions if f.name.startswith("test")]
            declared[file_path] = test_names
            if not test_names:
                continue

            window = self.window(file_path)
            first_line = analysis.functions[0].line_start
            evidence = builder.observation(
                title=f"{file_path} declares {len(test_names)} test function(s)",
                description=(
                    f"Test functions found in {file_path}: "
                    f"{', '.join(test_names)}."
                ),
                location=self.location(file_path, first_line),
                snippet=window.snippet(first_line) if window else None,
                method="ast_test_inventory",
                extra_metadata={
                    "test_functions": test_names,
                    "test_count": len(test_names),
                },
            )
            if evidence:
                ids.append(evidence.id)
        return ids, declared

    # ── 2. Which production modules the tests touch ───────────────────────

    def _report_coverage(
        self, builder: EvidenceBuilder, index: ModuleIndex, test_files: list[str]
    ) -> tuple[list[str], dict[str, list[str]]]:
        """Report which *production files* each test file imports.

        Keyed by resolved repository-relative path, not by module string, so that
        ``from a.b import c`` and ``import a.b`` count as the same coverage and a
        symbol name can never be mistaken for a module.
        """
        ids: list[str] = []
        covered: dict[str, list[str]] = {}
        test_paths = {f.relative_path for f in self.context.exploration.test_files}

        for file_path in test_files:
            production_imports = sorted(
                {
                    edge.target_path
                    for edge in index.imports_of(file_path)
                    if edge.internal and edge.target_path not in test_paths
                }
            )
            for target in production_imports:
                covered.setdefault(target, []).append(file_path)

            if not production_imports:
                continue

            modules = [
                index.path_to_module.get(p, p) for p in production_imports
            ]
            evidence = builder.observation(
                title=(
                    f"{file_path} exercises {len(production_imports)} production "
                    f"file(s) directly"
                ),
                description=(
                    f"{file_path} imports the production module(s) "
                    f"{', '.join(modules)}. The tests therefore call these modules "
                    f"directly rather than driving them through an HTTP layer."
                ),
                location=self.location(file_path),
                snippet=None,
                method="import_graph",
                extra_metadata={
                    "modules_under_test": modules,
                    "files_under_test": production_imports,
                },
            )
            if evidence:
                ids.append(evidence.id)

        return ids, covered

    # ── 3. Coverage gaps ──────────────────────────────────────────────────

    def _report_gaps(
        self,
        builder: EvidenceBuilder,
        test_files: list[str],
        declared: dict[str, list[str]],
        covered: dict[str, list[str]],
    ) -> None:
        """Derive hypotheses about what the suite does not exercise."""
        index = ModuleIndex.build(self.context.ast)
        supports = [e.id for e in builder.items][-3:]
        relevance = self.relevance

        # How many test files import each production file. A file imported by
        # several of them is shared test infrastructure (a data model, a
        # fixture helper): importing it says nothing about which concept a test
        # covers, so it is not attributed to any concept below.
        test_paths = {f.relative_path for f in self.context.exploration.test_files}
        importers_by_file: dict[str, set[str]] = {}
        for test_path in test_files:
            for edge in index.imports_of(test_path):
                if edge.internal and edge.target_path not in test_paths:
                    importers_by_file.setdefault(edge.target_path, set()).add(
                        test_path
                    )

        # Production files backing each issue concept, restricted to
        # name-level matches so that a shared data model is not attributed to
        # every concept that merely appears somewhere inside it.
        concept_files: list[tuple[str, set[str]]] = [
            (
                cluster.term,
                self._distinctive_files(
                    self._production_files_for(cluster.name_level_paths),
                    importers_by_file,
                ),
            )
            for cluster in relevance.clusters
        ]
        concept_files = [(term, files) for term, files in concept_files if files]

        # ── Gap A: two issue concepts never exercised together ─────────────
        # Only files unique to one side discriminate between the concepts. A
        # file in both sets (a shared data model, say) says nothing about
        # whether the two concerns are covered together, and treating it as
        # coverage would hide the very gap being looked for.
        reported_pairs: set[frozenset[str]] = set()
        for i, (first_term, first_files) in enumerate(concept_files):
            for second_term, second_files in concept_files[i + 1 :]:
                if len(reported_pairs) >= MAX_GAP_HYPOTHESES:
                    break
                key = frozenset((first_term, second_term))
                if key in reported_pairs:
                    continue

                first_only = first_files - second_files
                second_only = second_files - first_files
                if not first_only or not second_only:
                    continue

                shared_test = None
                for test_path in test_files:
                    imported = {
                        edge.target_path
                        for edge in index.imports_of(test_path)
                        if edge.internal
                    }
                    if (first_only & imported) and (second_only & imported):
                        shared_test = test_path
                        break
                if shared_test is not None:
                    continue
                reported_pairs.add(key)

                first_modules = sorted(self._modules_of(index, first_only))
                second_modules = sorted(self._modules_of(index, second_only))
                builder.hypothesis(
                    title=(
                        f"No test exercises '{first_term}' and '{second_term}' "
                        f"together"
                    ),
                    description=(
                        f"The issue names '{first_term}' and '{second_term}' as "
                        f"separate concerns, handled by "
                        f"{', '.join(first_modules[:3])} and "
                        f"{', '.join(second_modules[:3])} respectively. "
                        f"{len(test_files)} test file(s) were examined and none of them "
                        f"imports a production file unique to both sides, so no "
                        f"existing test covers the two concerns in combination. A "
                        f"defect that only appears when both are involved would not be "
                        f"caught by the current suite. This is a gap in coverage, not "
                        f"evidence of a defect."
                    ),
                    location=None,
                    repository_level=True,
                    confidence=0.6,
                    supports=supports,
                    method="coverage_analysis",
                    extra_metadata={
                        "concepts": [first_term, second_term],
                        "first_modules": first_modules,
                        "second_modules": second_modules,
                        "test_files_examined": len(test_files),
                    },
                )

        # ── Gap B: a relevant production file no test imports at all ───────
        for term, files in concept_files[:3]:
            untested = sorted(files - set(covered))
            if not untested:
                continue
            untested_modules = sorted(self._modules_of(index, untested))
            plural = "s" if len(untested) != 1 else ""
            verb = "are" if len(untested) != 1 else "is"
            builder.hypothesis(
                title=(
                    f"{len(untested)} file{plural} matching '{term}' {verb} not "
                    f"imported by any test"
                ),
                description=(
                    f"The issue text matches production file(s) "
                    f"{', '.join(untested[:4])}, and no test file imports them. "
                    f"Whatever the tests in this repository assert, they do not assert "
                    f"anything about {', '.join(untested_modules[:3])} directly. This "
                    f"is a statement about the test suite, not about the correctness "
                    f"of the code."
                ),
                location=None,
                repository_level=True,
                confidence=0.5,
                supports=supports,
                method="coverage_analysis",
                extra_metadata={
                    "concept": term,
                    "untested_files": untested,
                    "untested_modules": untested_modules,
                },
            )

        # ── Gap C: no test drives the HTTP layer ───────────────────────────
        route_files = sorted(
            {f.relative_path for f in self.context.ast.files if f.routes}
        )
        if route_files and not (set(route_files) & set(covered)):
            builder.observation(
                title=(
                    f"{len(route_files)} file(s) declare HTTP routes but no test file "
                    f"imports any of them"
                ),
                description=(
                    f"Routes are declared in {', '.join(route_files[:4])}, yet none of "
                    f"the {len(test_files)} test file(s) imports a route module. The "
                    f"tests therefore exercise the code the routes call, but not the "
                    f"routes themselves - request parsing, validation, and the wiring "
                    f"between the two are not covered by the existing suite."
                ),
                location=self.location(route_files[0]),
                method="import_graph",
                extra_metadata={"route_files": route_files},
            )
            builder.hypothesis(
                title=(
                    "The reported behaviour may not be covered at the request "
                    "boundary"
                ),
                description=(
                    "No test file imports a module that declares an HTTP route, so the "
                    "route layer is exercised only indirectly. If the report concerns "
                    "what a request returns rather than what a function computes, the "
                    "existing suite could pass while the reported behaviour is still "
                    "wrong. Whether that is the case here is not established."
                ),
                location=self.location(route_files[0]),
                confidence=0.4,
                supports=supports,
                method="coverage_analysis",
                extra_metadata={"route_files": route_files},
            )

    # ── 4. Investigative next steps ───────────────────────────────────────

    def _report_next_steps(
        self, builder: EvidenceBuilder, test_files: list[str]
    ) -> None:
        """Propose the next investigative step: a scratch reproduction."""
        if not test_files:
            return
        builder.proposed_action(
            title=(
                "Write a scratch test that reproduces the reported scenario in "
                "isolation"
            ),
            description=(
                "The reported scenario can be expressed as a test without touching "
                "any existing file. Running such a test would convert this static "
                "coverage analysis into a runtime observation, which is what would "
                "distinguish a real defect from a misreading of the code. Execution "
                "is out of scope for this phase, which runs no repository code."
            ),
            location=self.location(test_files[0]),
            method="coverage_analysis",
            extra_metadata={"example_test_files": test_files[:3]},
        )

    # ── helpers ───────────────────────────────────────────────────────────

    def _production_files_for(self, file_paths: list[str]) -> set[str]:
        """Filter ``file_paths`` down to the production files behind them.

        Test files are excluded: a concept matching a test function name is not
        evidence that the concept has a production module.
        """
        test_paths = {f.relative_path for f in self.context.exploration.test_files}
        analysis = self.context.ast
        known = {f.relative_path for f in analysis.files}
        return {
            path
            for path in file_paths
            if path not in test_paths and path in known
        }

    @staticmethod
    def _distinctive_files(
        files: set[str], importers_by_file: dict[str, set[str]]
    ) -> set[str]:
        """Drop files that several test files import (shared infrastructure)."""
        return {
            path
            for path in files
            if len(importers_by_file.get(path, ())) <= 1
        }

    @staticmethod
    def _modules_of(index: ModuleIndex, file_paths) -> list[str]:
        """Dotted module names for ``file_paths``, for human-readable output."""
        return sorted(
            {
                index.path_to_module[path]
                for path in file_paths
                if path in index.path_to_module
            }
        )


__all__ = ["TestInvestigator"]
