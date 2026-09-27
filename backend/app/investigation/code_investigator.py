"""
Code Investigator — Phase 7.

Answers: *which code does the reported issue actually point at, and how is it
wired together?*

What it looks at
----------------
Only the files that :mod:`app.investigation.relevance` selected, and only through
the Phase 4 AST model plus bounded text scans of already-loaded source.

What it emits
-------------
* ``OBSERVATION`` — a file's routes and functions, call sites resolved to another
  relevant file, modules two areas of the code share, an attribute written in one
  file and read in another, and numeric operations inside relevant functions.
* ``HYPOTHESIS`` — grounded in those observations: which interactions are worth
  examining, and why.  Every hypothesis names the observation IDs it rests on via
  ``metadata.supports``.
* ``PROPOSED_ACTION`` — the next *investigative* step (reproduce this path,
  instrument this value).  Never a code change; the evidence layer rejects
  fix-shaped proposals outright.

What it refuses to do
---------------------
It does not name a root cause, does not claim a function is wrong, and does not
say anything about runtime behaviour — nothing is executed.  A function that
"looks wrong" is reported as a hypothesis with a confidence, not as a finding.
"""

from __future__ import annotations

from app.investigation.base import BaseInvestigator
from app.investigation.evidence import EvidenceBuilder
from app.investigation.modules import ModuleIndex
from app.investigation.scan import (
    cross_file_attributes,
    find_attribute_sites,
    find_precision_sites,
)
from app.models.domain import Evidence
from app.models.investigation import InvestigatorName, SourceReference

#: How many structural facts to report per file before moving on.
MAX_DETAILS_PER_FILE = 6

#: How many cross-file call sites to report in total.
MAX_CALL_SITES = 12

#: How many distinct concept pairs may become interaction hypotheses.
MAX_INTERACTION_HYPOTHESES = 3


def _quote_list(values: list[str]) -> str:
    """``['a', 'b', 'c']`` → ``'a', 'b' and 'c'`` — readable in a title."""
    quoted = [f"'{v}'" for v in values]
    if len(quoted) == 1:
        return quoted[0]
    if len(quoted) == 2:
        return f"{quoted[0]} and {quoted[1]}"
    return f"{', '.join(quoted[:-1])} and {quoted[-1]}"


class CodeInvestigator(BaseInvestigator):
    """Traces issue-relevant code and the relationships between it."""

    name = InvestigatorName.CODE

    def _run(self, builder: EvidenceBuilder) -> dict[str, object]:
        relevance = self.relevance
        snapshot = self.context.snapshot
        index = ModuleIndex.build(self.context.ast)
        source_files = [
            p for p in relevance.source_paths() if self.analysis_for(p) is not None
        ]

        summary: dict[str, object] = {
            "issue_terms": list(relevance.terms.terms),
            "candidates_considered": relevance.candidates_considered,
            "relevant_files": len(relevance.files),
            "source_files_examined": len(source_files),
        }

        relevance_evidence = self._report_relevance(builder)
        structure_ids = self._report_structure(builder, source_files)
        call_ids = self._report_call_sites(builder, index, source_files)
        shared_ids, shared_pairs = self._report_shared_modules(
            builder, index, source_files
        )
        flow_ids = self._report_value_flows(builder, source_files)
        precision_ids = self._report_precision(builder, source_files)
        self._report_hypotheses(
            builder,
            relevance_evidence=relevance_evidence,
            structure_ids=structure_ids,
            call_ids=call_ids,
            shared_ids=shared_ids,
            shared_pairs=shared_pairs,
            flow_ids=flow_ids,
            precision_ids=precision_ids,
        )
        self._report_next_steps(builder, source_files)

        summary["attribute_flows"] = len(flow_ids)
        summary["precision_sites"] = len(precision_ids)
        summary["repository"] = snapshot.name
        return summary

    # ── 1. Relevance ──────────────────────────────────────────────────────

    def _report_relevance(self, builder: EvidenceBuilder) -> Evidence | None:
        """State which files were opened for this issue, and why."""
        relevance = self.relevance
        if not relevance.files:
            return builder.observation(
                title="No repository file matched the terms in the reported issue",
                description=(
                    "Issue terms considered: "
                    f"{', '.join(relevance.terms.terms) or 'none'}. "
                    f"All {len(self.context.ast.files)} analysed file(s) scored below "
                    "the relevance threshold, so no file-level investigation was "
                    "possible. This is an observation about the search, not a "
                    "conclusion about the bug."
                ),
                repository_level=True,
                method="relevance_scoring",
                extra_metadata={
                    "issue_terms": list(relevance.terms.terms),
                    "candidates_considered": relevance.candidates_considered,
                },
            )

        detail_lines = []
        for entry in relevance.files:
            reasons = "; ".join(entry.reasons) or "term match"
            detail_lines.append(f"{entry.file_path} (score {entry.score:g}: {reasons})")

        anchor = self.primary_source_file() or top.file_path
        return builder.observation(
            title=(
                f"{len(relevance.files)} file(s) selected as relevant to the issue"
            ),
            description=(
                "Selected by deterministic term matching against the issue text "
                f"({', '.join(relevance.terms.terms)}). "
                + " | ".join(detail_lines)
            ),
            location=self.location(anchor),
            method="relevance_scoring",
            extra_metadata={
                "ranked_files": [
                    {
                        "file_path": e.file_path,
                        "score": e.score,
                        "matched_terms": e.matched_terms,
                        "reasons": e.reasons,
                        "is_test": e.is_test,
                    }
                    for e in relevance.files
                ],
                "issue_terms": list(relevance.terms.terms),
            },
        )

    # ── 2. Structure of the relevant files ────────────────────────────────

    def _report_structure(
        self, builder: EvidenceBuilder, source_files: list[str]
    ) -> list[str]:
        """Report routes and functions defined by each relevant source file."""
        ids: list[str] = []
        for file_path in source_files:
            analysis = self.analysis_for(file_path)
            if analysis is None:
                continue

            if analysis.routes:
                for route in analysis.routes[:MAX_DETAILS_PER_FILE]:
                    window = self.window(file_path)
                    snippet = window.snippet(route.line) if window else None
                    evidence = builder.observation(
                        title=(
                            f"{route.http_method} {route.path} is served by "
                            f"{route.function_name}() in {file_path}"
                        ),
                        description=(
                            f"Phase 4 detected the route decorator at line "
                            f"{route.line}. The handler is {route.function_name}()."
                        ),
                        location=self.location(file_path, route.line),
                        snippet=snippet,
                        method="ast_route_detection",
                        extra_metadata={
                            "http_method": route.http_method,
                            "route_path": route.path,
                            "handler": route.function_name,
                            "router_var": route.router_var,
                        },
                    )
                    if evidence:
                        ids.append(evidence.id)

            function_names = [f.name for f in analysis.functions]
            for cls in analysis.classes:
                function_names.extend(f"{cls.name}.{m.name}" for m in cls.methods)

            if function_names and len(ids) < MAX_DETAILS_PER_FILE * len(source_files):
                listed = ", ".join(function_names[:MAX_DETAILS_PER_FILE])
                more = (
                    f" (+{len(function_names) - MAX_DETAILS_PER_FILE} more)"
                    if len(function_names) > MAX_DETAILS_PER_FILE
                    else ""
                )
                evidence = builder.observation(
                    title=f"{file_path} defines {len(function_names)} function(s)",
                    description=(
                        f"Functions and methods declared in this file: {listed}{more}."
                    ),
                    location=self.location(file_path),
                    method="ast_symbol_table",
                    extra_metadata={
                        "functions": function_names,
                        "classes": [c.name for c in analysis.classes],
                    },
                )
                if evidence:
                    ids.append(evidence.id)
        return ids

    # ── 3. Call sites between relevant files ──────────────────────────────

    def _report_call_sites(
        self, builder: EvidenceBuilder, index: ModuleIndex, source_files: list[str]
    ) -> list[str]:
        """Report calls that cross from one relevant file into another."""
        ids: list[str] = []
        relevant = set(source_files)

        for file_path in source_files:
            analysis = self.analysis_for(file_path)
            if analysis is None:
                continue
            for call in index.calls_from(file_path):
                if len(ids) >= MAX_CALL_SITES:
                    return ids
                if call.target_path is None or call.target_path == file_path:
                    continue
                # Only report calls that land in a file we also consider
                # relevant, so the trace stays a trace of the issue's subject.
                if call.target_path not in relevant:
                    continue

                target_names = index.functions_named(call.target_path)
                symbol = call.target_symbol or ""
                definition_line = target_names.get(symbol)
                caller_window = self.window(file_path)
                target_window = self.window(call.target_path)

                references = []
                if definition_line is not None:
                    references.append(
                        self.reference(
                            call.target_path,
                            definition_line,
                            f"definition of {symbol}",
                        )
                    )

                evidence = builder.observation(
                    title=(
                        f"{file_path}:{call.line} calls {call.callable_expr}() "
                        f"defined in {call.target_path}"
                    ),
                    description=(
                        f"Static call trace: a call to {call.callable_expr}() appears "
                        f"at line {call.line} of {file_path}, and the name resolves "
                        f"through the imports of that file to {call.target_path}"
                        + (
                            f" where {symbol}() is defined at line {definition_line}."
                            if definition_line is not None
                            else "."
                        )
                        + " This is a static import/call relationship; no code was "
                        "executed to confirm it runs."
                    ),
                    location=self.location(file_path, call.line),
                    snippet=caller_window.snippet(call.line) if caller_window else None,
                    references=references,
                    method="ast_call_graph",
                    extra_metadata={
                        "callee": call.callable_expr,
                        "target_file": call.target_path,
                        "target_module": call.target_module,
                        "target_symbol": symbol,
                        "target_definition_line": definition_line,
                        "target_snippet": (
                            target_window.snippet(definition_line)
                            if target_window and definition_line is not None
                            else None
                        ),
                        "static_only": True,
                    },
                )
                if evidence:
                    ids.append(evidence.id)
        return ids

    # ── 4. Modules shared between areas of the code ───────────────────────

    def _report_shared_modules(
        self, builder: EvidenceBuilder, index: ModuleIndex, source_files: list[str]
    ) -> tuple[list[str], list[tuple[list[str], str]]]:
        """Report repository modules that more than one relevant file imports.

        This is how "these two features meet here" is established without
        knowing what the features are.  Only *internal* modules count: a shared
        dependency on a third-party package is not a finding about this
        repository.
        """
        ids: list[str] = []
        pairs: list[tuple[list[str], str]] = []
        shared = index.shared_internal_imports(source_files)

        for target_path, importers in shared.items():
            if len(importers) < 2:
                continue
            module = index.path_to_module.get(target_path, target_path)
            references = [
                self.reference(path, None, f"imports {module}")
                for path in importers
            ]
            evidence = builder.observation(
                title=f"{len(importers)} relevant file(s) import {module}",
                description=(
                    f"The module {module} ({target_path}) is imported by "
                    f"{len(importers)} of the files considered relevant to this "
                    f"issue: {', '.join(importers)}. Code handling different parts of "
                    f"the reported behaviour therefore passes through the same module."
                ),
                location=self.location(target_path),
                references=references,
                method="import_graph",
                extra_metadata={
                    "shared_module": module,
                    "resolved_path": target_path,
                    "importers": importers,
                },
            )
            if evidence:
                ids.append(evidence.id)
                pairs.append((importers, module))
        return ids, pairs

    # ── 5. Values handed between files ────────────────────────────────────

    def _report_value_flows(
        self, builder: EvidenceBuilder, source_files: list[str]
    ) -> list[str]:
        """Report attributes written in one relevant file and read in another."""
        ids: list[str] = []
        sites_by_file: dict[str, list] = {}

        for file_path in source_files:
            window = self.window(file_path)
            if window is None:
                continue
            analysis = self.analysis_for(file_path)
            rows: list[tuple[int, str]] = []
            if analysis is not None:
                for function in analysis.functions:
                    rows.extend(window.slice(function.line_start, function.line_end))
                for cls in analysis.classes:
                    for method in cls.methods:
                        rows.extend(window.slice(method.line_start, method.line_end))
            if not rows:
                continue
            sites_by_file[file_path] = find_attribute_sites(file_path, rows)

        for attribute, writer_file, writer_line, sites in cross_file_attributes(
            sites_by_file
        ):
            readers = [
                s for s in sites if not s.is_write and s.file_path != writer_file
            ]
            writer = next(
                (s for s in sites if s.is_write and s.file_path == writer_file), None
            )
            if not readers or writer is None:
                continue

            window = self.window(writer_file)
            references = [
                self.reference(s.file_path, s.line, "reads the attribute")
                for s in readers[:5]
            ]
            evidence = builder.observation(
                title=(
                    f"'{attribute}' is assigned in {writer_file} and read in "
                    f"{readers[0].file_path}"
                ),
                description=(
                    f"The attribute {attribute} is assigned at line {writer_line} of "
                    f"{writer_file} and is read at line {readers[0].line} of "
                    f"{readers[0].file_path}"
                    + (
                        f" (and {len(readers) - 1} other place(s))."
                        if len(readers) > 1
                        else "."
                    )
                    + " A value is therefore produced in one file and consumed in "
                    "another, so the two files are coupled through this value."
                ),
                location=self.location(writer_file, writer_line),
                snippet=window.snippet(writer_line) if window else None,
                references=references,
                method="text_scan",
                extra_metadata={
                    "attribute": attribute,
                    "written_in": writer_file,
                    "read_in": sorted({s.file_path for s in readers}),
                    "sites": [
                        {
                            "file_path": s.file_path,
                            "line": s.line,
                            "is_write": s.is_write,
                            "text": s.text,
                        }
                        for s in sites[:8]
                    ],
                },
            )
            if evidence:
                ids.append(evidence.id)
        return ids

    # ── 6. Numeric operations in relevant functions ───────────────────────

    def _report_precision(
        self, builder: EvidenceBuilder, source_files: list[str]
    ) -> list[str]:
        """Report divisions, numeric casts and boundary checks in relevant code.

        Generic heuristics, deliberately phrased as "this is present" so the
        accompanying hypothesis stays a hypothesis.
        """
        ids: list[str] = []
        for file_path in source_files:
            analysis = self.analysis_for(file_path)
            window = self.window(file_path)
            if analysis is None or window is None:
                continue
            for function in analysis.functions:
                rows = window.slice(function.line_start, function.line_end)
                sites = find_precision_sites(file_path, rows, limit=6)
                if not sites:
                    continue
                kinds = sorted({s.kind for s in sites})
                evidence = builder.observation(
                    title=(
                        f"{function.name}() in {file_path} performs "
                        f"{', '.join(kinds)}"
                    ),
                    description=(
                        f"Within lines {function.line_start}-{function.line_end} of "
                        f"{file_path}, the function body contains: "
                        + "; ".join(
                            f"line {s.line} {s.detail}" for s in sites[:4]
                        )
                        + ". Numeric operations of this shape can change a computed "
                        "value depending on the operand types and values used."
                    ),
                    location=self.location(
                        file_path, function.line_start, function.line_end
                    ),
                    snippet=window.snippet(sites[0].line, context=2),
                    method="text_scan",
                    extra_metadata={
                        "function": function.name,
                        "kinds": kinds,
                        "sites": [
                            {
                                "line": s.line,
                                "kind": s.kind,
                                "detail": s.detail,
                                "text": s.text,
                            }
                            for s in sites
                        ],
                    },
                )
                if evidence:
                    ids.append(evidence.id)
        return ids

    # ── 7. Hypotheses, each tied to the observations behind it ────────────

    def _report_hypotheses(
        self,
        builder: EvidenceBuilder,
        *,
        relevance_evidence: Evidence | None,
        structure_ids: list[str],
        call_ids: list[str],
        shared_ids: list[str],
        shared_pairs: list[tuple[list[str], str]],
        flow_ids: list[str],
        precision_ids: list[str],
    ) -> None:
        relevance = self.relevance
        clusters = relevance.clusters
        supports_all = (
            [e.id for e in (relevance_evidence,) if e is not None]
            + structure_ids
            + call_ids
        )
        # Anchor code hypotheses on a production file, never on a test file.
        anchor = self.primary_source_file()

        # ── H: the issue names several concepts that share a module ────────
        # A concept pair is reported once even when several shared modules
        # connect it, so the report does not repeat itself.
        reported_pairs: set[frozenset[str]] = set()
        for importers, module in shared_pairs:
            if len(reported_pairs) >= MAX_INTERACTION_HYPOTHESES:
                break
            cluster_terms = [
                cluster.term
                for cluster in clusters
                if set(cluster.file_paths) & set(importers)
            ]
            if len(cluster_terms) < 2:
                # Not a cross-concept interaction; the observation stands alone.
                continue
            key = frozenset(cluster_terms)
            if key in reported_pairs:
                continue
            reported_pairs.add(key)

            ordered = [t for t in relevance.terms.terms if t in cluster_terms]
            names = _quote_list(ordered[:3])
            builder.hypothesis(
                title=f"Interaction between {names} may warrant investigation",
                description=(
                    f"The reported issue names {len(cluster_terms)} distinct concepts "
                    f"({', '.join(ordered)}). The code that matches those concepts "
                    f"both reach the shared module {module}, so their behaviour is "
                    f"coupled there. That coupling is a plausible place to look for an "
                    f"interaction defect, but nothing read so far shows an actual fault."
                ),
                location=self.location(importers[0]),
                confidence=0.55,
                supports=shared_ids + supports_all[:2],
                method="relation_reasoning",
                extra_metadata={
                    "concepts": ordered,
                    "shared_module": module,
                    "importers": importers,
                },
            )

        # ── H: a value crosses a file boundary near the reported behaviour ─
        seen_anchor: set[str] = set()
        for evidence_id in flow_ids[:2]:
            if evidence_id in seen_anchor:
                continue
            seen_anchor.add(evidence_id)
            builder.hypothesis(
                title=(
                    "A value produced in one module and consumed in another may be "
                    "involved"
                ),
                description=(
                    "At least one attribute is assigned in a file that matched the "
                    "issue and read in another such file. Because the producing and "
                    "consuming sides live in different modules, a disagreement about "
                    "that value's expected form would not be visible from either file "
                    "alone. This is a direction to investigate, not a defect that has "
                    "been found."
                ),
                location=self.location(anchor) if anchor else None,
                confidence=0.45,
                supports=[evidence_id]
                + ([relevance_evidence.id] if relevance_evidence else []),
                method="relation_reasoning",
                extra_metadata={"anchored_on": evidence_id},
            )

        # ── H: numeric operations in the relevant path ────────────────────
        for evidence_id in precision_ids[:1]:
            builder.hypothesis(
                title=(
                    "A numeric operation in the relevant code path may change the "
                    "reported result"
                ),
                description=(
                    "A division, numeric cast, or numeric boundary check was observed "
                    "inside a function that matched the reported issue. Such "
                    "operations can alter or discard part of a value, so if the report "
                    "is about a wrong number this location deserves checking against "
                    "the expected arithmetic. The observation alone does not show the "
                    "value is wrong."
                ),
                location=self.location(anchor) if anchor else None,
                confidence=0.35,
                supports=[evidence_id],
                method="relation_reasoning",
                extra_metadata={"anchored_on": evidence_id},
            )

        if not clusters:
            builder.hypothesis(
                title="The issue text could not be mapped onto repository identifiers",
                description=(
                    "No term in the reported issue matched a file name, route, symbol, "
                    "or parameter in this repository, so the investigation could not "
                    "narrow the search. A more specific report, naming a route, a "
                    "function, or a file, would allow a targeted investigation."
                ),
                repository_level=True,
                confidence=0.6,
                supports=[],
                method="relation_reasoning",
                extra_metadata={"issue_terms": list(relevance.terms.terms)},
            )

    # ── 8. Investigative next steps ───────────────────────────────────────

    def _report_next_steps(
        self, builder: EvidenceBuilder, source_files: list[str]
    ) -> None:
        """Propose the next *investigative* actions.  Never a code change."""
        relevance = self.relevance
        route_files = [
            path
            for path in source_files
            if (self.analysis_for(path) is not None)
            and self.analysis_for(path).routes  # type: ignore[union-attr]
        ]

        if route_files:
            first = route_files[0]
            analysis = self.analysis_for(first)
            route = analysis.routes[0] if analysis and analysis.routes else None
            if route is not None:
                builder.proposed_action(
                    title=(
                        f"Reproduce the report through {route.http_method} "
                        f"{route.path} in an isolated environment"
                    ),
                    description=(
                        f"The report describes behaviour, and {route.http_method} "
                        f"{route.path} is the entry point whose code matches the "
                        f"issue text. Driving that route with the reported inputs would "
                        f"turn this static investigation into a runtime observation. "
                        f"This action requires execution and is therefore out of scope "
                        f"for this phase, which runs no repository code."
                    ),
                    location=self.location(first, route.line),
                    method="static_analysis",
                    extra_metadata={"route": route.path, "http_method": route.http_method},
                )

        if len(relevance.clusters) >= 2:
            builder.proposed_action(
                title=(
                    "Capture the actual values exchanged across the modules the issue "
                    "names"
                ),
                description=(
                    "Several concepts in the report map to different modules that "
                    "share a collaborator. Recording the concrete values passed between "
                    "them for the reported scenario would show whether the values are "
                    "what the receiving side expects. This is an observation-gathering "
                    "step and does not change any repository file."
                ),
                location=None,
                repository_level=True,
                method="static_analysis",
                extra_metadata={
                    "concepts": [c.term for c in relevance.clusters[:5]],
                },
            )


__all__ = ["CodeInvestigator"]
