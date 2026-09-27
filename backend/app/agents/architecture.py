"""
Architecture Agent — Phase 5.

Purpose
-------
Transform the deterministic repository information produced by:
  - Phase 3 (RepositoryExploration — file tree, language detection)
  - Phase 4 (RepositoryASTAnalysis — functions, classes, routes, imports)

into a structured ArchitectureAnalysis that answers:
  - What are the major modules?
  - What are the entry points?
  - What API routes exist?
  - Which modules depend on which other modules?
  - Which services are involved?
  - What are the important execution/data flows?
  - How is the application organised?
  - Which files are likely relevant to a particular workflow?

Design constraints
------------------
* DETERMINISTIC — no LLM, no random state.  Same inputs always produce the
  same outputs.  The interface is designed to be replaced/augmented with an
  LLM in a future phase without changing the callers.
* READ-ONLY — no filesystem writes, no subprocess calls, no code execution.
* EVIDENCE-BASED — every finding references the source files or AST nodes
  that justify it.  Inferences are labelled INFERRED; direct observations
  are labelled OBSERVED.
* GENERAL-PURPOSE — operates on RepositoryASTAnalysis, not on ShopDemo
  knowledge.  ShopDemo is only used for validation.

Evidence classification used in this module
-------------------------------------------
  OBSERVED  — directly present in AST / filesystem data
  INFERRED  — derived from patterns in the data; may be wrong
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.analysis.ast_analyzer import (
    FileAnalysis,
    FileRelationship,
    RepositoryASTAnalysis,
    RouteInfo,
)
from app.ingestion.explorer import RepositoryExploration

logger = logging.getLogger(__name__)

# ── Module-role heuristics (name substrings → role) ──────────────────────────
# These are generic programming patterns, NOT ShopDemo-specific names.

_ROUTE_KEYWORDS: frozenset[str] = frozenset(
    {"route", "routes", "router", "view", "views", "endpoint", "endpoints",
     "controller", "controllers", "handler", "handlers"}
)
_SERVICE_KEYWORDS: frozenset[str] = frozenset(
    {"service", "services", "usecase", "usecases", "use_case", "use_cases",
     "manager", "managers", "business", "logic", "processor", "processors"}
)
_MODEL_KEYWORDS: frozenset[str] = frozenset(
    {"model", "models", "schema", "schemas", "entity", "entities",
     "domain", "types", "dto", "dtos"}
)
_DATA_KEYWORDS: frozenset[str] = frozenset(
    {"database", "db", "data", "store", "storage", "repository",
     "repositories", "dao", "seed", "fixtures", "catalog", "catalog"}
)
_TEST_KEYWORDS: frozenset[str] = frozenset(
    {"test", "tests", "spec", "specs", "e2e", "integration", "unit"}
)
_ENTRY_KEYWORDS: frozenset[str] = frozenset(
    {"main", "app", "application", "server", "wsgi", "asgi", "entrypoint"}
)

# FastAPI / Starlette app-creation call signatures (lowercase)
_FRAMEWORK_APP_CONSTRUCTORS: frozenset[str] = frozenset(
    {"fastapi", "flask", "starlette", "quart", "sanic", "falcon", "django",
     "tornado"}
)


# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class EvidenceRef:
    """A traceable reference to the source of a finding."""

    file_path: str
    detail: str              # short description of what was observed
    evidence_type: str       # "OBSERVED" | "INFERRED"


@dataclass
class ModuleInfo:
    """An identified module or package within the repository."""

    name: str                    # human-readable name (e.g. "cart routes")
    role: str                    # "routes" | "service" | "model" | "data" |
                                 # "test" | "entry_point" | "utility" | "unknown"
    files: list[str]             # repository-relative file paths
    primary_language: str
    function_count: int
    class_count: int
    route_count: int
    evidence: list[EvidenceRef] = field(default_factory=list)


@dataclass
class EntryPoint:
    """A detected application entry point."""

    file_path: str
    kind: str                    # "web_app" | "cli" | "script" | "unknown"
    framework: str | None        # "FastAPI" | "Flask" | "Django" | None
    symbol: str | None           # variable/function name, if determinable
    line: int | None
    evidence: list[EvidenceRef] = field(default_factory=list)


@dataclass
class DependencyEdge:
    """A static dependency relationship between two modules."""

    source_module: str
    target_module: str
    source_files: list[str]
    evidence_type: str           # "OBSERVED" (direct import) | "INFERRED"
    description: str


@dataclass
class RouteSummary:
    """A summarised API route."""

    http_method: str
    path: str
    handler: str
    file_path: str
    module_name: str             # which ModuleInfo this belongs to
    line: int


@dataclass
class DataFlow:
    """A statically-inferred data-flow path through the application."""

    name: str                    # e.g. "checkout flow"
    steps: list[str]             # ordered list of module/file names
    evidence_type: str           # always "INFERRED" in Phase 5
    source_files: list[str]


@dataclass
class ArchitectureFinding:
    """A significant architectural observation or inference."""

    category: str                # e.g. "entry_point", "api_layer", "service_layer"
    description: str
    evidence: list[EvidenceRef]
    confidence: float            # 0.0 – 1.0
    related_files: list[str] = field(default_factory=list)


@dataclass
class ArchitectureAnalysis:
    """Complete architecture analysis of a repository.

    All fields are populated by ArchitectureAgent.analyze().
    No field requires an LLM — all data is deterministic.
    """

    repository_name: str
    repository_root: str
    project_type: str            # "web_api" | "library" | "script" | "mixed" | "unknown"
    detected_languages: list[str]

    modules: list[ModuleInfo]
    entry_points: list[EntryPoint]
    routes: list[RouteSummary]
    dependencies: list[DependencyEdge]
    data_flows: list[DataFlow]
    findings: list[ArchitectureFinding]

    # Summary statistics
    total_files: int
    total_functions: int
    total_classes: int
    total_routes: int

    notes: list[str] = field(default_factory=list)


# ── Architecture Agent ────────────────────────────────────────────────────────


class ArchitectureAgent:
    """Deterministic, static architecture agent.

    Consumes Phase 3 (RepositoryExploration) and Phase 4
    (RepositoryASTAnalysis) and produces an ArchitectureAnalysis.

    This implementation is intentionally LLM-free.  Future phases may
    enhance findings by passing them through the AI provider layer, but the
    data contract (ArchitectureAnalysis) remains stable.

    Security: never executes code, never imports modules, never calls
    subprocesses, never follows arbitrary filesystem paths.
    """

    # ── Public API ────────────────────────────────────────────────────────

    def analyze(
        self,
        ast_analysis: RepositoryASTAnalysis,
        exploration: RepositoryExploration | None = None,
    ) -> ArchitectureAnalysis:
        """Produce an ArchitectureAnalysis from AST + exploration data.

        Args:
            ast_analysis: Output of Phase 4 ASTAnalyzer.
            exploration:  Output of Phase 3 RepositoryExplorer (optional but
                          enriches module grouping and language detection).

        Returns:
            ArchitectureAnalysis — never raises; errors become notes.
        """
        try:
            return self._analyze(ast_analysis, exploration)
        except Exception as exc:
            logger.exception("ArchitectureAgent.analyze() failed")
            # Return a minimal but valid result rather than propagating
            return ArchitectureAnalysis(
                repository_name=ast_analysis.name,
                repository_root=ast_analysis.root,
                project_type="unknown",
                detected_languages=[],
                modules=[],
                entry_points=[],
                routes=[],
                dependencies=[],
                data_flows=[],
                findings=[],
                total_files=len(ast_analysis.files),
                total_functions=len(ast_analysis.all_functions),
                total_classes=len(ast_analysis.all_classes),
                total_routes=len(ast_analysis.routes),
                notes=[f"Analysis error: {exc}"],
            )

    # ── High-level convenience ─────────────────────────────────────────────

    def analyze_path(self, repo_root_path: "Path") -> ArchitectureAnalysis:  # type: ignore[name-defined]
        """High-level helper: ingest + AST-analyze + architecture-analyze.

        Delegates to Phase 3 + Phase 4 layers then calls analyze().
        """
        from pathlib import Path

        from app.analysis.ast_analyzer import ASTAnalyzer
        from app.ingestion.explorer import RepositoryExplorer

        explorer = RepositoryExplorer()
        exploration = explorer.explore(Path(repo_root_path), read_source=True)

        analyzer = ASTAnalyzer()
        ast_result = analyzer.analyze_repository(
            root=str(repo_root_path),
            name=Path(repo_root_path).name,
            file_sources=[
                (ev.relative_path, ev.content, ev.language)
                for ev in exploration.source_evidence
            ],
        )

        return self.analyze(ast_result, exploration)

    # ── Implementation ─────────────────────────────────────────────────────

    def _analyze(
        self,
        ast: RepositoryASTAnalysis,
        exploration: RepositoryExploration | None,
    ) -> ArchitectureAnalysis:

        # ── Detected languages ─────────────────────────────────────────────
        langs = _collect_languages(ast, exploration)

        # ── Group files into logical modules ──────────────────────────────
        modules = self._detect_modules(ast, exploration)

        # ── Entry points ───────────────────────────────────────────────────
        entry_points = self._detect_entry_points(ast)

        # ── Route summaries ────────────────────────────────────────────────
        routes = self._summarise_routes(ast.routes, modules)

        # ── Dependency edges ───────────────────────────────────────────────
        deps = self._build_dependencies(ast.relationships, modules)

        # ── Project type ───────────────────────────────────────────────────
        project_type = _infer_project_type(entry_points, ast.routes, modules)

        # ── Data flows ─────────────────────────────────────────────────────
        flows = self._infer_data_flows(modules, deps)

        # ── Findings ───────────────────────────────────────────────────────
        findings = self._generate_findings(
            ast, modules, entry_points, routes, deps, project_type
        )

        return ArchitectureAnalysis(
            repository_name=ast.name,
            repository_root=ast.root,
            project_type=project_type,
            detected_languages=langs,
            modules=modules,
            entry_points=entry_points,
            routes=routes,
            dependencies=deps,
            data_flows=flows,
            findings=findings,
            total_files=len(ast.files),
            total_functions=len(ast.all_functions),
            total_classes=len(ast.all_classes),
            total_routes=len(ast.routes),
        )

    # ── Module detection ───────────────────────────────────────────────────

    def _detect_modules(
        self,
        ast: RepositoryASTAnalysis,
        exploration: RepositoryExploration | None,
    ) -> list[ModuleInfo]:
        """Group source files into logical modules based on directory structure
        and naming patterns.  No knowledge of ShopDemo is encoded here.
        """
        # Build a mapping: directory path → list of files
        dir_to_files: dict[str, list[FileAnalysis]] = defaultdict(list)
        for fa in ast.files:
            if not fa.supported:
                continue
            parts = fa.relative_path.split("/")
            # Use the immediate parent directory (or root for top-level files)
            parent = "/".join(parts[:-1]) if len(parts) > 1 else ""
            dir_to_files[parent].append(fa)

        modules: list[ModuleInfo] = []
        for dir_path, files in dir_to_files.items():
            role = _infer_role(dir_path, files)
            func_count = sum(len(f.functions) for f in files)
            cls_count = sum(len(f.classes) for f in files)
            route_count = sum(len(f.routes) for f in files)

            # Build evidence refs
            evidence: list[EvidenceRef] = [
                EvidenceRef(
                    file_path=f.relative_path,
                    detail=f"Contains {len(f.functions)} function(s), "
                           f"{len(f.classes)} class(es), "
                           f"{len(f.routes)} route(s)",
                    evidence_type="OBSERVED",
                )
                for f in files
            ]

            name = _module_display_name(dir_path, files, role)
            lang = _dominant_language(files)

            modules.append(
                ModuleInfo(
                    name=name,
                    role=role,
                    files=[f.relative_path for f in files],
                    primary_language=lang,
                    function_count=func_count,
                    class_count=cls_count,
                    route_count=route_count,
                    evidence=evidence,
                )
            )

        # Sort: entry_point first, then by route_count desc, then alphabetically
        modules.sort(key=lambda m: (m.role != "entry_point", -m.route_count, m.name))
        return modules

    # ── Entry point detection ──────────────────────────────────────────────

    def _detect_entry_points(
        self, ast: RepositoryASTAnalysis
    ) -> list[EntryPoint]:
        entry_points: list[EntryPoint] = []

        for fa in ast.files:
            if not fa.supported or fa.error:
                continue

            # Check for web-framework app instantiation in top-level calls
            # OR assignments at module scope recognised from class names
            framework, symbol, line = _detect_framework_app(fa)
            if framework:
                file_key = fa.relative_path.lower()
                kind = "web_app"
                entry_points.append(
                    EntryPoint(
                        file_path=fa.relative_path,
                        kind=kind,
                        framework=framework,
                        symbol=symbol,
                        line=line,
                        evidence=[
                            EvidenceRef(
                                file_path=fa.relative_path,
                                detail=f"{framework} application instance detected "
                                       f"(symbol: {symbol}, line: {line})",
                                evidence_type="OBSERVED",
                            )
                        ],
                    )
                )
                continue

            # Check for if __name__ == "__main__": blocks (heuristic via file name)
            # Only flag TOP-LEVEL files (no subdirectory) — "app/routes/cart.py"
            # contains "app" in its path but is not an entry point.
            parts = fa.relative_path.split("/")
            filename_only = parts[-1].lower().replace(".py", "")
            is_top_level = len(parts) == 1
            if is_top_level and any(kw == filename_only for kw in _ENTRY_KEYWORDS):
                # Top-level file whose name exactly matches an entry-point keyword
                if fa.functions or fa.classes:
                    entry_points.append(
                        EntryPoint(
                            file_path=fa.relative_path,
                            kind="script",
                            framework=None,
                            symbol=None,
                            line=None,
                            evidence=[
                                EvidenceRef(
                                    file_path=fa.relative_path,
                                    detail="File name matches entry-point pattern "
                                           f"and contains {len(fa.functions)} function(s)",
                                    evidence_type="INFERRED",
                                )
                            ],
                        )
                    )

        return entry_points

    # ── Route summaries ────────────────────────────────────────────────────

    def _summarise_routes(
        self,
        routes: list[RouteInfo],
        modules: list[ModuleInfo],
    ) -> list[RouteSummary]:
        summaries: list[RouteSummary] = []
        for r in routes:
            module_name = _find_module_for_file(r.file_path, modules)
            summaries.append(
                RouteSummary(
                    http_method=r.http_method,
                    path=r.path,
                    handler=r.function_name,
                    file_path=r.file_path,
                    module_name=module_name,
                    line=r.line,
                )
            )
        return summaries

    # ── Dependency edges ───────────────────────────────────────────────────

    def _build_dependencies(
        self,
        relationships: list[FileRelationship],
        modules: list[ModuleInfo],
    ) -> list[DependencyEdge]:
        # Aggregate file-level imports into module-level edges (deduplicate)
        seen: set[tuple[str, str]] = set()
        edges: list[DependencyEdge] = []

        for rel in relationships:
            source_mod = _find_module_for_file(rel.source_file, modules)
            # target_module is a dotted module name — convert to display name
            target_mod = _module_name_from_dotted(rel.target_module, modules)

            if source_mod == target_mod:
                continue  # intra-module import — skip
            if target_mod is None:
                continue  # external dependency — skip for now

            key = (source_mod, target_mod)
            if key in seen:
                continue
            seen.add(key)

            edges.append(
                DependencyEdge(
                    source_module=source_mod,
                    target_module=target_mod,
                    source_files=[rel.source_file],
                    evidence_type="OBSERVED",
                    description=f"{source_mod} imports from {target_mod}",
                )
            )

        return edges

    # ── Data flow inference ────────────────────────────────────────────────

    def _infer_data_flows(
        self,
        modules: list[ModuleInfo],
        deps: list[DependencyEdge],
    ) -> list[DataFlow]:
        """Build lightweight flow chains: route → service → model/data.

        These are INFERRED by following dependency edges from route modules
        to their transitive dependencies.  Labelled INFERRED throughout.
        """
        flows: list[DataFlow] = []

        route_modules = [m for m in modules if m.role == "routes" and m.route_count > 0]
        for rm in route_modules:
            chain, visited_files = _trace_module_chain(rm.name, deps, modules, depth=3)
            if len(chain) > 1:
                flows.append(
                    DataFlow(
                        name=f"{rm.name} flow",
                        steps=chain,
                        evidence_type="INFERRED",
                        source_files=list(visited_files),
                    )
                )

        return flows

    # ── Architecture findings ──────────────────────────────────────────────

    def _generate_findings(
        self,
        ast: RepositoryASTAnalysis,
        modules: list[ModuleInfo],
        entry_points: list[EntryPoint],
        routes: list[RouteSummary],
        deps: list[DependencyEdge],
        project_type: str,
    ) -> list[ArchitectureFinding]:
        findings: list[ArchitectureFinding] = []

        # ── Entry point finding ────────────────────────────────────────────
        for ep in entry_points:
            findings.append(
                ArchitectureFinding(
                    category="entry_point",
                    description=(
                        f"Application entry point detected in {ep.file_path}. "
                        f"Framework: {ep.framework or 'unknown'}. "
                        f"Kind: {ep.kind}."
                    ),
                    evidence=ep.evidence,
                    confidence=0.90 if ep.framework else 0.60,
                    related_files=[ep.file_path],
                )
            )

        # ── Layer findings ─────────────────────────────────────────────────
        for role, label, conf in [
            ("routes",      "API / Route layer",  0.85),
            ("service",     "Service layer",       0.80),
            ("model",       "Model / Schema layer", 0.80),
            ("data",        "Data / Catalog layer", 0.75),
            ("test",        "Test layer",           0.85),
        ]:
            layer_modules = [m for m in modules if m.role == role]
            if not layer_modules:
                continue
            all_files = [f for m in layer_modules for f in m.files]
            evidence = [
                EvidenceRef(
                    file_path=f,
                    detail=f"Role inferred as '{role}' from file name and content",
                    evidence_type="INFERRED",
                )
                for f in all_files[:5]  # cap evidence refs for readability
            ]
            findings.append(
                ArchitectureFinding(
                    category=f"{role}_layer",
                    description=(
                        f"{label} identified: "
                        f"{len(layer_modules)} module(s), "
                        f"{len(all_files)} file(s)."
                    ),
                    evidence=evidence,
                    confidence=conf,
                    related_files=all_files,
                )
            )

        # ── Route count finding ────────────────────────────────────────────
        if routes:
            route_files = list({r.file_path for r in routes})
            findings.append(
                ArchitectureFinding(
                    category="api_routes",
                    description=(
                        f"{len(routes)} HTTP route(s) detected across "
                        f"{len(route_files)} file(s)."
                    ),
                    evidence=[
                        EvidenceRef(
                            file_path=r.file_path,
                            detail=f"{r.http_method} {r.path} → {r.handler}",
                            evidence_type="OBSERVED",
                        )
                        for r in routes[:8]
                    ],
                    confidence=0.95,
                    related_files=route_files,
                )
            )

        # ── Dependency finding ─────────────────────────────────────────────
        if deps:
            findings.append(
                ArchitectureFinding(
                    category="dependencies",
                    description=(
                        f"{len(deps)} inter-module dependency edge(s) detected."
                    ),
                    evidence=[
                        EvidenceRef(
                            file_path=d.source_files[0] if d.source_files else "",
                            detail=d.description,
                            evidence_type="OBSERVED",
                        )
                        for d in deps[:5]
                    ],
                    confidence=0.80,
                )
            )

        # ── Highly-connected module finding ────────────────────────────────
        import_counts: dict[str, int] = defaultdict(int)
        for d in deps:
            import_counts[d.target_module] += 1
        if import_counts:
            most_imported = max(import_counts, key=lambda k: import_counts[k])
            count = import_counts[most_imported]
            if count >= 2:
                findings.append(
                    ArchitectureFinding(
                        category="highly_connected_module",
                        description=(
                            f"Module '{most_imported}' is imported by {count} "
                            f"other module(s) — likely a shared dependency."
                        ),
                        evidence=[
                            EvidenceRef(
                                file_path="",
                                detail=f"Import count: {count}",
                                evidence_type="INFERRED",
                            )
                        ],
                        confidence=0.70,
                    )
                )

        return findings


# ── Pure helper functions ─────────────────────────────────────────────────────


def _collect_languages(
    ast: RepositoryASTAnalysis,
    exploration: RepositoryExploration | None,
) -> list[str]:
    if exploration:
        return [lang for lang in sorted(exploration.languages.keys())
                if exploration.languages[lang] > 0]
    langs: set[str] = {
        fa.language for fa in ast.files if fa.language and fa.language != "Unknown"
    }
    return sorted(langs)


def _infer_role(dir_path: str, files: list[FileAnalysis]) -> str:
    """Infer the architectural role of a directory from its name and content."""
    path_lower = dir_path.lower()
    parts = set(path_lower.replace("/", " ").split())

    # Test: check early before other role patterns
    if parts & _TEST_KEYWORDS:
        return "test"
    if any(f.relative_path.lower().startswith(("test_", "tests/")) for f in files):
        return "test"
    if any("test_" in f.relative_path for f in files):
        return "test"

    # Route layer
    if parts & _ROUTE_KEYWORDS:
        return "routes"
    if any(f.routes for f in files):
        return "routes"

    # Service layer
    if parts & _SERVICE_KEYWORDS:
        return "service"

    # Model layer
    if parts & _MODEL_KEYWORDS:
        # Exclude if no classes — might be misnamed
        if any(f.classes for f in files):
            return "model"

    # Data layer
    if parts & _DATA_KEYWORDS:
        return "data"

    # Entry-point heuristic
    if not dir_path:  # top-level files
        for f in files:
            name = f.relative_path.lower()
            if any(kw in name for kw in _ENTRY_KEYWORDS):
                if f.top_level_calls or f.functions:
                    return "entry_point"

    return "utility"


def _detect_framework_app(fa: FileAnalysis) -> tuple[str | None, str | None, int | None]:
    """Return (framework_name, variable_name, line) if a web framework app is found.

    Detects both patterns:
      app = FastAPI()        <- module-level assignment (most common)
      FastAPI()              <- top-level expression call (rare)
    
    Strategy: if the file imports a known framework constructor we treat it as
    an entry-point candidate.  This covers the ``app = FastAPI()`` pattern which
    the AST visitor records as an import + top-level assignment (not a call Expr),
    as well as the explicit call form.
    """
    # ── Build a set of locally-imported framework names ────────────────────
    # Only the application-level constructors are entry-point signals.
    # Importing APIRouter, HTTPException, etc. from fastapi does NOT count.
    _APP_CONSTRUCTOR_NAMES: frozenset[str] = frozenset(
        {"fastapi", "flask", "starlette", "application", "django",
         "quart", "sanic", "falcon", "tornado"}
    )

    imported_frameworks: set[str] = set()
    for imp in fa.imports:
        # Pattern: from fastapi import FastAPI  (name matches constructor)
        for name in imp.names:
            if name.lower() in _FRAMEWORK_APP_CONSTRUCTORS:
                imported_frameworks.add(name.lower())
        # Pattern: import fastapi  (whole-module import used as fastapi.FastAPI())
        mod_low = imp.module.lower()
        if not imp.is_from_import and mod_low in _FRAMEWORK_APP_CONSTRUCTORS:
            imported_frameworks.add(mod_low)

    def _fw_match(expr: str) -> str | None:
        low = expr.lower()
        for fw in _FRAMEWORK_APP_CONSTRUCTORS:
            if low == fw or low.endswith("." + fw):
                return fw
        for fw in imported_frameworks:
            if low == fw or low.endswith("." + fw):
                return fw
        return None

    # ── Explicit top-level call: FastAPI() as a statement ─────────────────
    for call in fa.top_level_calls:
        fw = _fw_match(call.callable_expr)
        if fw:
            return _capitalise_framework(fw), None, call.line

    # ── Function-body calls (unusual but possible) ─────────────────────────
    for fn in fa.functions:
        for call in fn.calls:
            fw = _fw_match(call.callable_expr)
            if fw:
                return _capitalise_framework(fw), fn.name, call.line

    # ── Import-based heuristic: file imports a framework constructor ───────
    # This covers ``app = FastAPI()`` because the Phase 4 AST visitor records
    # ``from fastapi import FastAPI`` as an import but does NOT capture the
    # right-hand side of assignments as a top_level_call (only bare Expr
    # statements are captured that way).  If the file imports a constructor
    # and has an entry-point-style name, it is almost certainly the app file.
    if imported_frameworks:
        first_fw = next(iter(imported_frameworks))
        return _capitalise_framework(first_fw), None, None

    return None, None, None


def _capitalise_framework(name: str) -> str:
    """Convert internal lowercase framework key to display name."""
    _display = {
        "fastapi": "FastAPI", "flask": "Flask", "django": "Django",
        "starlette": "Starlette", "quart": "Quart", "sanic": "Sanic",
        "falcon": "Falcon", "tornado": "Tornado",
    }
    return _display.get(name, name.capitalize())


def _module_display_name(
    dir_path: str, files: list[FileAnalysis], role: str
) -> str:
    """Produce a human-readable module name from its directory path."""
    if not dir_path:
        return "root"
    # Use the last component of the directory path
    return dir_path.split("/")[-1]


def _dominant_language(files: list[FileAnalysis]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for f in files:
        if f.language and f.language != "Unknown":
            counts[f.language] += 1
    return max(counts, key=lambda k: counts[k]) if counts else "Unknown"


def _infer_project_type(
    entry_points: list[EntryPoint],
    routes: list[RouteInfo],
    modules: list[ModuleInfo],
) -> str:
    if any(ep.kind == "web_app" for ep in entry_points) or routes:
        return "web_api"
    route_modules = [m for m in modules if m.role == "routes"]
    if route_modules:
        return "web_api"
    if any(m.role == "entry_point" for m in modules):
        return "script"
    return "unknown"


def _find_module_for_file(file_path: str, modules: list[ModuleInfo]) -> str:
    for m in modules:
        if file_path in m.files:
            return m.name
    # Fall back: derive from the file's parent directory
    parts = file_path.split("/")
    return parts[-2] if len(parts) > 1 else "root"


def _module_name_from_dotted(
    dotted: str, modules: list[ModuleInfo]
) -> str | None:
    """Map a Python dotted-module name to one of our module display names.

    Returns None for external/stdlib packages.
    """
    # Only consider modules that are sub-packages of the local project
    # Heuristic: if the dotted name's parts overlap with module names, match it
    parts = dotted.lower().split(".")
    for m in modules:
        for part in parts:
            if part == m.name.lower():
                return m.name
    # If the module name appears as a segment in any file path, it's local
    for m in modules:
        for f in m.files:
            for part in parts:
                if part in f.lower():
                    return m.name
    return None


def _trace_module_chain(
    start: str,
    deps: list[DependencyEdge],
    modules: list[ModuleInfo],
    depth: int,
) -> tuple[list[str], set[str]]:
    """Follow dependency edges from `start` up to `depth` hops."""
    chain = [start]
    visited: set[str] = set()
    visited.add(start)
    source_files: set[str] = set()

    current = start
    for _ in range(depth):
        next_edges = [d for d in deps if d.source_module == current
                      and d.target_module not in visited]
        if not next_edges:
            break
        # Prefer edges whose target has a "deeper" role (service > model > data)
        role_priority = {"routes": 0, "service": 1, "model": 2, "data": 3,
                         "utility": 4, "entry_point": 5, "test": 6, "unknown": 7}
        next_edge = min(
            next_edges,
            key=lambda e: role_priority.get(
                _module_role_from_name(e.target_module, modules), 7
            ),
        )
        visited.add(next_edge.target_module)
        source_files.update(next_edge.source_files)
        chain.append(next_edge.target_module)
        current = next_edge.target_module

    return chain, source_files


def _module_role_from_name(name: str, modules: list[ModuleInfo]) -> str:
    for m in modules:
        if m.name == name:
            return m.role
    return "unknown"
