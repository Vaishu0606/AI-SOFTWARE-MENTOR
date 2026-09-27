"""
Dependency Investigator — Phase 7.

Answers: *what is the execution path behind the reported issue, and where do its
parts meet?*

Source of truth
---------------
This investigator is a strict re-presenter of the Phase 5 ``ArchitectureAnalysis``
and the Phase 4 import/call graph.  It adds no new judgement about quality, and
above all it never invents runtime behaviour: every claim it makes is explicitly
labelled as static, and it emits a standing observation saying so.

What it emits
-------------
* ``OBSERVATION`` — the dependency edges touching issue-relevant modules, the
  request path from an HTTP route down to the functions it calls, and the shared
  collaborators and data flows that Phase 5 already identified.
* ``HYPOTHESIS`` — which orderings along that path are worth examining, tied to
  the observations that justify it.
* ``PROPOSED_ACTION`` — the next investigative step along the traced path.

Static versus runtime
---------------------
Imports, decorators, and call sites are all that was read.  The investigator
states this once, explicitly, in an ``OBSERVATION`` that is always emitted, and
labels every evidence item with ``analysis_kind: "static"``.  A reader can
therefore never mistake "the code calls X" for "X ran and produced Y".
"""

from __future__ import annotations

from app.investigation.base import BaseInvestigator
from app.investigation.evidence import EvidenceBuilder
from app.investigation.modules import ModuleIndex
from app.models.investigation import InvestigatorName

#: How many dependency edges to report in full.
MAX_EDGES = 20

#: How many request paths to trace.
MAX_PATHS = 6


class DependencyInvestigator(BaseInvestigator):
    """Traces the static execution path behind the reported issue."""

    name = InvestigatorName.DEPENDENCY

    def _run(self, builder: EvidenceBuilder) -> dict[str, object]:
        architecture = self.context.architecture
        index = ModuleIndex.build(self.context.ast)
        relevance = self.relevance

        summary: dict[str, object] = {
            "dependency_edge_count": len(architecture.dependencies),
            "data_flow_count": len(architecture.data_flows),
            "route_count": architecture.total_routes,
            "analysis_kind": "static",
        }

        static_id = self._report_static_boundary(builder)
        edge_ids = self._report_edges(builder, index)
        path_ids = self._report_request_paths(builder, index)
        flow_ids = self._report_data_flows(builder)
        self._report_hypotheses(
            builder,
            static_id=static_id,
            edge_ids=edge_ids,
            path_ids=path_ids,
            flow_ids=flow_ids,
        )
        self._report_next_steps(builder, relevance.files)

        summary["edges_reported"] = len(edge_ids)
        summary["paths_reported"] = len(path_ids)
        return summary

    # ── 1. The honesty anchor ─────────────────────────────────────────────

    def _report_static_boundary(self, builder: EvidenceBuilder):
        """Always state that nothing was executed.  Never omit this."""
        architecture = self.context.architecture
        return builder.observation(
            title=(
                "All dependency findings are static; no repository code was executed"
            ),
            description=(
                f"Every relationship in this investigation was derived from reading "
                f"{len(self.context.ast.files)} file(s): "
                f"{len(self.context.ast.relationships)} import relationship(s) and "
                f"{len(architecture.dependencies)} architecture dependency edge(s). "
                f"Nothing was run, imported, or evaluated, so no statement here "
                f"establishes what the code does at runtime — only how it is wired "
                f"together."
            ),
            repository_level=True,
            method="static_boundary",
            extra_metadata={
                "analysis_kind": "static",
                "runtime_observed": False,
                "import_relationship_count": len(self.context.ast.relationships),
                "ast_error_count": len(self.context.ast.errors),
            },
        )

    # ── 2. Dependency edges around the relevant modules ───────────────────

    def _report_edges(self, builder: EvidenceBuilder, index: ModuleIndex):
        """Report the import edges touching issue-relevant files.

        Two levels are reported, because they answer different questions and it
        would be misleading to conflate them:

        * **file level** (from Phase 4) — "this file imports that file", which is
          precise and actionable;
        * **group level** (from Phase 5) — "this group of files imports that
          group", which is a coarse directory grouping and is labelled as such.
        """
        ids: list[str] = []
        relevance = self.relevance
        relevant = set(relevance.file_paths())
        test_paths = {f.relative_path for f in self.context.exploration.test_files}

        # ── File-level internal imports ────────────────────────────────────
        # Deduplicated on (source, line, target): a single line importing two
        # names from one module is one import statement, not two.
        seen: set[tuple[str, int, str]] = set()
        reported = 0
        for source in sorted(relevant):
            if reported >= MAX_EDGES:
                break
            if source in test_paths:
                continue
            for edge in index.imports_of(source):
                if reported >= MAX_EDGES:
                    break
                if not edge.internal or edge.target_path == source:
                    continue
                key = (source, edge.line, edge.target_path)
                if key in seen:
                    continue
                seen.add(key)
                evidence = builder.observation(
                    title=(
                        f"{source} imports {edge.base_module} "
                        f"({edge.target_path})"
                    ),
                    description=(
                        f"Line {edge.line} of {source} imports "
                        f"{', '.join(edge.names) or edge.target_module} from "
                        f"{edge.base_module}, which resolves to the file "
                        f"{edge.target_path} inside this repository. Read from the "
                        f"import graph; no import was performed."
                    ),
                    location=self.location(source, edge.line),
                    method="ast_import_graph",
                    extra_metadata={
                        "source_file": source,
                        "import_line": edge.line,
                        "base_module": edge.base_module,
                        "target_module": edge.target_module,
                        "target_file": edge.target_path,
                        "names": list(edge.names),
                        "level": "file",
                        "analysis_kind": "static",
                    },
                )
                if evidence:
                    ids.append(evidence.id)
                    reported += 1

        # ── Group-level architecture summary, honestly labelled ────────────
        architecture = self.context.architecture
        group_ids = self._report_module_groups(builder, relevant, reported)
        ids.extend(group_ids)
        return ids

    def _report_module_groups(
        self, builder: EvidenceBuilder, relevant: set[str], already_reported: int
    ) -> list[str]:
        """Summarise the Phase 5 module grouping and its group-level edges."""
        architecture = self.context.architecture
        if not architecture.modules or already_reported >= MAX_EDGES:
            return []

        module_names = [m.name for m in architecture.modules]
        edges = [
            {
                "source_module": e.source_module,
                "target_module": e.target_module,
                "evidence_type": e.evidence_type,
                "source_files": e.source_files,
            }
            for e in architecture.dependencies
            if set(e.source_files) & relevant
        ][:MAX_EDGES]

        edge_lines = [
            f"{e['source_module']} imports from {e['target_module']} "
            f"(evidence type {e['evidence_type']}, declared in "
            f"{', '.join(e['source_files']) or 'unknown file'})"
            for e in edges
        ]

        return_ids: list[str] = []
        evidence = builder.observation(
            title=(
                f"Architecture analysis groups the repository into "
                f"{len(module_names)} module(s)"
            ),
            description=(
                f"Phase 5 grouped {architecture.total_files} file(s) into "
                f"{len(module_names)} module(s): {', '.join(module_names)}. "
                f"Dependencies between those groups: "
                + (" | ".join(edge_lines) if edge_lines else "none touching the issue")
                + ". These are directory-level groupings, not module-level "
                "dependencies; the file-level import edges above are the precise form."
            ),
            repository_level=True,
            method="architecture_module_grouping",
            extra_metadata={
                "modules": module_names,
                "group_edges": edges,
                "grouping_level": "directory",
                "analysis_kind": "static",
            },
        )
        if evidence:
            return_ids.append(evidence.id)
        return return_ids

    # ── 3. Request paths ──────────────────────────────────────────────────

    def _report_request_paths(self, builder: EvidenceBuilder, index: ModuleIndex):
        """Trace route → calls → target files for issue-relevant routes."""
        ids: list[str] = []
        relevance = self.relevance
        relevant = set(relevance.file_paths())
        configuration = self.context.config

        for analysis in self.context.ast.files:
            if len(ids) >= MAX_PATHS:
                break
            if not analysis.routes:
                continue
            if analysis.relative_path not in relevant and relevance.files:
                continue

            calls = index.calls_from(analysis.relative_path)
            edges = []
            for call in calls:
                if call.target_path is None:
                    continue
                edges.append(
                    {
                        "line": call.line,
                        "callee": call.callable_expr,
                        "target_file": call.target_path,
                        "target_is_relevant": call.target_path in relevant,
                    }
                )
            if not edges:
                continue

            for route in analysis.routes:
                if len(ids) >= MAX_PATHS:
                    break
                window = self.window(analysis.relative_path)
                steps = "; ".join(
                    f"line {e['line']} → {e['callee']}() in {e['target_file']}"
                    for e in edges[:MAX_DETAIL_STEPS]
                )
                evidence = builder.observation(
                    title=(
                        f"Static request path for {route.http_method} {route.path}: "
                        f"{analysis.relative_path} calls {len(edges)} resolved target(s)"
                    ),
                    description=(
                        f"The handler {route.function_name}() declared at line "
                        f"{route.line} of {analysis.relative_path} statically calls: "
                        f"{steps}. "
                        f"{sum(1 for e in edges if e['target_is_relevant'])} of those "
                        f"target(s) are files this investigation also considers "
                        f"relevant to the issue. The order shown is source order; no "
                        f"call was actually made."
                    ),
                    location=self.location(analysis.relative_path, route.line),
                    snippet=window.snippet(route.line) if window else None,
                    method="ast_call_graph",
                    extra_metadata={
                        "http_method": route.http_method,
                        "route_path": route.path,
                        "handler": route.function_name,
                        "steps": edges[: configuration.max_relationships],
                        "analysis_kind": "static",
                    },
                )
                if evidence:
                    ids.append(evidence.id)
        return ids

    # ── 4. Data flows recorded by Phase 5 ─────────────────────────────────

    def _report_data_flows(self, builder: EvidenceBuilder):
        """Re-present the Phase 5 data flows that touch relevant modules."""
        ids: list[str] = []
        relevance = self.relevance
        relevant = set(relevance.file_paths())
        configuration = self.context.config

        for flow in self.context.architecture.data_flows:
            if len(ids) >= MAX_EDGES:
                break
            if relevant and not (set(flow.source_files) & relevant):
                continue

            steps = list(flow.steps)
            evidence = builder.observation(
                title=f"Statically inferred data flow: {flow.name}",
                description=(
                    f"Ordered path inferred by the architecture analysis: "
                    f"{' -> '.join(steps[:6])}"
                    + (" …" if len(steps) > 6 else "")
                    + f". Evidence type reported upstream: {flow.evidence_type}. "
                    f"Files involved: {', '.join(flow.source_files[:4])}. "
                    f"An inferred flow describes structure, not an observed transfer "
                    f"of a value at runtime."
                ),
                location=(
                    self.location(flow.source_files[0])
                    if flow.source_files
                    else None
                ),
                method="architecture_data_flow",
                extra_metadata={
                    "flow_name": flow.name,
                    "steps": steps[: configuration.max_relationships],
                    "source_files": flow.source_files,
                    "architecture_evidence_type": flow.evidence_type,
                    "analysis_kind": "static",
                },
            )
            if evidence:
                ids.append(evidence.id)
        return ids

    # ── 5. Hypotheses about orderings worth examining ─────────────────────

    def _report_hypotheses(
        self,
        builder: EvidenceBuilder,
        *,
        static_id,
        edge_ids: list[str],
        path_ids: list[str],
        flow_ids: list[str],
    ) -> None:
        relevance = self.relevance
        primary = self.primary_source_file()
        supports = ([static_id.id] if static_id else []) + path_ids + edge_ids[:2]

        if path_ids and primary:
            builder.hypothesis(
                title=(
                    "The order in which the traced path executes may matter for the "
                    "reported behaviour"
                ),
                description=(
                    f"A static call path was traced from the route handler in "
                    f"{primary} through "
                    f"{len(path_ids)} call(s) into files that match the issue text. "
                    f"The order of those calls and of the state updates between them "
                    f"is visible statically, but whether the order is correct is a "
                    f"question about intent that reading imports cannot answer. This "
                    f"hypothesis identifies where to look; it does not assert an "
                    f"ordering defect."
                ),
                location=self.location(primary),
                confidence=0.4,
                supports=supports,
                method="relation_reasoning",
                extra_metadata={"analysis_kind": "static"},
            )

        if flow_ids and primary:
            builder.hypothesis(
                title=(
                    "A value crossing a module boundary is a place where an assumption "
                    "could be violated"
                ),
                description=(
                    f"{len(flow_ids)} data flow(s) recorded by the architecture "
                    f"analysis connect modules that match the reported issue. Where a "
                    f"value crosses a module boundary, each side may assume something "
                    f"about the other's representation that is not stated anywhere. "
                    f"No such disagreement has been observed here."
                ),
                location=self.location(primary),
                confidence=0.35,
                supports=([static_id.id] if static_id else []) + flow_ids[:2],
                method="relation_reasoning",
                extra_metadata={"analysis_kind": "static"},
            )

        if not relevance.files:
            builder.hypothesis(
                title=(
                    "No static path could be traced for the reported behaviour"
                ),
                description=(
                    "No file in the repository matched the terms in the issue, so no "
                    "request path or dependency edge could be associated with the "
                    "report. The report may describe behaviour reached through a name "
                    "that does not appear in the code, or through configuration the "
                    "static analysis cannot see."
                ),
                repository_level=True,
                confidence=0.5,
                supports=([static_id.id] if static_id else []),
                method="relation_reasoning",
            )

    # ── 6. Investigative next steps ───────────────────────────────────────

    def _report_next_steps(self, builder: EvidenceBuilder, files: list[str]) -> None:
        primary = self.primary_source_file() or (files[0] if files else None)
        if not primary:
            return
        builder.proposed_action(
            title=(
                "Trace the traced request path with real values at each call site"
            ),
            description=(
                f"The static path through {primary} is known, but the values passed "
                f"along it are not. Recording those values for the reported scenario "
                f"would show which hand-off first deviates from what the receiving "
                f"side expects. This is an observation-gathering step; it changes no "
                f"repository file and requires execution, which this phase does not "
                f"perform."
            ),
            location=self.location(primary),
            method="static_analysis",
            extra_metadata={"entry_file": primary},
        )


#: How many call steps to name inline in a request-path description.
MAX_DETAIL_STEPS = 6


__all__ = ["DependencyInvestigator"]
