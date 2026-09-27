"""
AST-based source-code analyzer — Phase 4.

Purpose
-------
Transform raw Python source text (already collected by the Phase 3 ingestion
layer) into structured code-level information that future AI agents can consume
without re-reading files themselves.

Answers questions such as:
  - What functions / classes / methods are defined?
  - What imports does each file declare?
  - What function calls occur at each call site?
  - What FastAPI routes are registered?
  - What static relationships exist between files?

Scope
-----
Phase 4 implements PYTHON ONLY via the standard-library ``ast`` module.

For any other language the analyzer reports the file as unsupported rather than
failing.  One bad/malformed file never aborts repository-level analysis.

Security invariants
-------------------
* NEVER executes analyzed code (no eval, exec, import, subprocess).
* NEVER follows filesystem paths beyond what is passed in by the caller.
* NEVER executes decorators, bodies, or any AST node.
* Treats all source text as UNTRUSTED DATA.
* Uses only ast.parse() → pure in-process tree walking.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── HTTP methods recognised in route decorators ───────────────────────────────

_HTTP_METHODS: frozenset[str] = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
)

# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class ImportInfo:
    """A single import statement."""

    module: str          # top-level module (e.g. "os", "app.services")
    names: list[str]     # imported names; ["*"] for star-imports
    aliases: dict[str, str]   # name → alias mapping
    is_from_import: bool  # True for  ``from X import Y``
    line: int


@dataclass
class CallInfo:
    """A single function-call site."""

    callable_expr: str   # textual representation of the callee
    line: int


@dataclass
class FunctionInfo:
    """A top-level or nested function / method definition."""

    name: str
    line_start: int
    line_end: int
    arguments: list[str]       # positional + keyword argument names
    decorators: list[str]      # decorator expressions as text
    is_async: bool
    is_method: bool            # True if defined inside a class
    class_name: str | None     # owning class, if is_method
    calls: list[CallInfo] = field(default_factory=list)
    docstring: str | None = None


@dataclass
class ClassInfo:
    """A class definition."""

    name: str
    line_start: int
    line_end: int
    bases: list[str]           # base class expressions as text
    decorators: list[str]
    methods: list[FunctionInfo] = field(default_factory=list)
    docstring: str | None = None


@dataclass
class RouteInfo:
    """A detected HTTP route (FastAPI / Starlette decorator pattern)."""

    http_method: str           # GET / POST / PUT / PATCH / DELETE …
    path: str                  # route path string, e.g. "/apply"
    function_name: str
    file_path: str             # repository-relative path
    line: int
    router_var: str            # the variable used: "app", "router", etc.


@dataclass
class AnalysisError:
    """Records a non-fatal error encountered while analyzing a file."""

    file_path: str
    error_type: str            # e.g. "SyntaxError", "UnicodeDecodeError"
    message: str


@dataclass
class FileAnalysis:
    """Complete AST analysis result for a single source file."""

    relative_path: str
    language: str
    supported: bool            # False for non-Python files in Phase 4
    imports: list[ImportInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    routes: list[RouteInfo] = field(default_factory=list)
    top_level_calls: list[CallInfo] = field(default_factory=list)
    error: AnalysisError | None = None


@dataclass
class FileRelationship:
    """A static dependency relationship between two files."""

    source_file: str     # file that imports
    target_module: str   # module being imported
    names: list[str]     # specific names imported (empty = whole module)
    line: int


@dataclass
class RepositoryASTAnalysis:
    """Repository-level AST analysis result."""

    root: str
    name: str
    files: list[FileAnalysis] = field(default_factory=list)
    routes: list[RouteInfo] = field(default_factory=list)           # all routes
    relationships: list[FileRelationship] = field(default_factory=list)
    errors: list[AnalysisError] = field(default_factory=list)

    # Convenience aggregates (populated by analyze_repository)
    all_functions: list[FunctionInfo] = field(default_factory=list)
    all_classes: list[ClassInfo] = field(default_factory=list)


# ── AST visitor ───────────────────────────────────────────────────────────────


class _PythonVisitor(ast.NodeVisitor):
    """Single-pass AST visitor — extracts all interesting constructs.

    Security: this visitor ONLY reads AST nodes.  It never calls any Python
    function, never imports any module, and never evaluates any expression.
    All "execution" of the source happens inside ast.parse() which only
    produces a tree — it does not run user code.
    """

    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.imports: list[ImportInfo] = []
        self.functions: list[FunctionInfo] = []
        self.classes: list[ClassInfo] = []
        self.routes: list[RouteInfo] = []
        self.top_level_calls: list[CallInfo] = []

        # State tracking
        self._current_class: str | None = None
        self._class_stack: list[str] = []

    # ── Imports ────────────────────────────────────────────────────────────

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        aliases: dict[str, str] = {}
        names: list[str] = []
        for alias in node.names:
            names.append(alias.name)
            if alias.asname:
                aliases[alias.name] = alias.asname
        self.imports.append(
            ImportInfo(
                module=names[0] if names else "",
                names=names,
                aliases=aliases,
                is_from_import=False,
                line=node.lineno,
            )
        )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        module = node.module or ""
        aliases: dict[str, str] = {}
        names: list[str] = []
        for alias in node.names:
            names.append(alias.name)
            if alias.asname:
                aliases[alias.name] = alias.asname
        self.imports.append(
            ImportInfo(
                module=module,
                names=names,
                aliases=aliases,
                is_from_import=True,
                line=node.lineno,
            )
        )
        self.generic_visit(node)

    # ── Classes ────────────────────────────────────────────────────────────

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        bases = [_expr_to_str(b) for b in node.bases]
        decorators = [_expr_to_str(d) for d in node.decorator_list]
        docstring = ast.get_docstring(node)

        class_info = ClassInfo(
            name=node.name,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            bases=bases,
            decorators=decorators,
            docstring=docstring,
        )

        # Enter class scope for method collection
        self._class_stack.append(node.name)
        self._current_class = node.name

        # Visit children to collect methods
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method = self._extract_function(child, is_method=True)
                class_info.methods.append(method)
            else:
                self.visit(child)

        self._class_stack.pop()
        self._current_class = self._class_stack[-1] if self._class_stack else None

        self.classes.append(class_info)
        # Do NOT call generic_visit — we handled children manually above

    # ── Functions ──────────────────────────────────────────────────────────

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        if not self._class_stack:
            fn = self._extract_function(node, is_method=False)
            self.functions.append(fn)
            # Check for route decorators on top-level functions
            self._check_routes(node)
        # Don't generic_visit here when inside a class — class visitor handles it

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        if not self._class_stack:
            fn = self._extract_function(node, is_method=False)
            self.functions.append(fn)
            self._check_routes(node)

    def _extract_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        is_method: bool,
    ) -> FunctionInfo:
        args = _extract_args(node.args)
        decorators = [_expr_to_str(d) for d in node.decorator_list]
        docstring = ast.get_docstring(node)
        calls = _extract_calls(node)

        return FunctionInfo(
            name=node.name,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            arguments=args,
            decorators=decorators,
            is_async=isinstance(node, ast.AsyncFunctionDef),
            is_method=is_method,
            class_name=self._current_class if is_method else None,
            calls=calls,
            docstring=docstring,
        )

    # ── Top-level calls ────────────────────────────────────────────────────

    def visit_Expr(self, node: ast.Expr) -> None:  # noqa: N802
        """Capture top-level expression statements that are function calls."""
        if isinstance(node.value, ast.Call) and not self._class_stack:
            self.top_level_calls.append(
                CallInfo(
                    callable_expr=_expr_to_str(node.value.func),
                    line=node.lineno,
                )
            )
        self.generic_visit(node)

    # ── Route detection ────────────────────────────────────────────────────

    def _check_routes(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        """Detect FastAPI/Starlette route decorators on a function definition."""
        for dec in node.decorator_list:
            route = _parse_route_decorator(dec, node.name, self.relative_path)
            if route:
                self.routes.append(route)


# ── Helper functions ──────────────────────────────────────────────────────────


def _expr_to_str(node: ast.expr) -> str:
    """Convert an AST expression node to a readable string representation.

    Uses ast.unparse when available (Python ≥ 3.9), falls back to a manual
    walk for older interpreters.  Never executes any code.
    """
    try:
        return ast.unparse(node)
    except Exception:
        pass
    # Fallback for unusual node shapes
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_expr_to_str(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{_expr_to_str(node.func)}(...)"
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return "<expr>"


def _extract_args(args: ast.arguments) -> list[str]:
    """Extract argument names from a function's argument list."""
    names: list[str] = []
    for arg in args.posonlyargs + args.args + args.kwonlyargs:
        names.append(arg.arg)
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return names


def _extract_calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[CallInfo]:
    """Extract all function call sites within a function body."""
    calls: list[CallInfo] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            calls.append(
                CallInfo(
                    callable_expr=_expr_to_str(child.func),
                    line=child.lineno,
                )
            )
    return calls


def _parse_route_decorator(
    dec: ast.expr,
    func_name: str,
    file_path: str,
) -> RouteInfo | None:
    """Detect FastAPI / Starlette route decorators.

    Recognises patterns:
        @app.get("/path")
        @router.post("/path")
        @api_router.put("/path", ...)

    Returns a RouteInfo if the decorator is a recognised route decorator,
    None otherwise.  Never executes any code.
    """
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if not isinstance(func, ast.Attribute):
        return None

    method_name = func.attr.lower()
    if method_name not in _HTTP_METHODS:
        return None

    # Extract the router variable name (app, router, api_router, …)
    router_var = _expr_to_str(func.value)

    # First positional argument is the path string
    route_path = ""
    if dec.args:
        first_arg = dec.args[0]
        if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
            route_path = first_arg.value
    # Also check for keyword argument `path=`
    for kw in dec.keywords:
        if kw.arg == "path" and isinstance(kw.value, ast.Constant):
            route_path = kw.value.value  # type: ignore[assignment]

    return RouteInfo(
        http_method=method_name.upper(),
        path=route_path,
        function_name=func_name,
        file_path=file_path,
        line=dec.lineno,
        router_var=router_var,
    )


# ── ASTAnalyzer — main public interface ───────────────────────────────────────


class ASTAnalyzer:
    """Static AST analyzer for Python source files.

    Public API
    ----------
    analyze_file(relative_path, source, language) -> FileAnalysis
        Parse and analyze a single source file from its text content.

    analyze_repository(root, file_sources) -> RepositoryASTAnalysis
        Analyze all files in a repository, using source text already
        provided by the Phase 3 ingestion layer.

    Security: this class never reads files from disk, never imports
    analyzed modules, never executes any code.  It works exclusively
    on source text passed in by the caller.
    """

    # ── Single-file analysis ───────────────────────────────────────────────

    def analyze_file(
        self,
        relative_path: str,
        source: str,
        language: str = "Python",
    ) -> FileAnalysis:
        """Analyze a single source file.

        Args:
            relative_path: Repository-relative POSIX path (for identification).
            source: The full source text of the file.
            language: Language as identified by Phase 3 explorer.

        Returns:
            FileAnalysis — never raises; errors are recorded in .error.
        """
        if language != "Python":
            return FileAnalysis(
                relative_path=relative_path,
                language=language,
                supported=False,
                error=AnalysisError(
                    file_path=relative_path,
                    error_type="UnsupportedLanguage",
                    message=f"AST analysis not supported for {language} in Phase 4.",
                ),
            )

        if not source.strip():
            return FileAnalysis(
                relative_path=relative_path,
                language=language,
                supported=True,
                # Empty file is valid — just nothing to report
            )

        try:
            tree = ast.parse(source, filename=relative_path)
        except SyntaxError as exc:
            return FileAnalysis(
                relative_path=relative_path,
                language=language,
                supported=True,
                error=AnalysisError(
                    file_path=relative_path,
                    error_type="SyntaxError",
                    message=str(exc),
                ),
            )
        except Exception as exc:
            return FileAnalysis(
                relative_path=relative_path,
                language=language,
                supported=True,
                error=AnalysisError(
                    file_path=relative_path,
                    error_type=type(exc).__name__,
                    message=str(exc),
                ),
            )

        visitor = _PythonVisitor(relative_path)
        try:
            visitor.visit(tree)
        except Exception as exc:
            # Visitor errors must not propagate — record and return partial result
            logger.warning("Visitor error in %s: %s", relative_path, exc)
            return FileAnalysis(
                relative_path=relative_path,
                language=language,
                supported=True,
                imports=visitor.imports,
                functions=visitor.functions,
                classes=visitor.classes,
                routes=visitor.routes,
                top_level_calls=visitor.top_level_calls,
                error=AnalysisError(
                    file_path=relative_path,
                    error_type=type(exc).__name__,
                    message=str(exc),
                ),
            )

        return FileAnalysis(
            relative_path=relative_path,
            language=language,
            supported=True,
            imports=visitor.imports,
            functions=visitor.functions,
            classes=visitor.classes,
            routes=visitor.routes,
            top_level_calls=visitor.top_level_calls,
        )

    # ── Repository-level analysis ──────────────────────────────────────────

    def analyze_repository(
        self,
        root: str,
        name: str,
        file_sources: list[tuple[str, str, str]],
    ) -> RepositoryASTAnalysis:
        """Analyze all provided source files.

        Args:
            root: Absolute path to the repository root (for identification).
            name: Repository name.
            file_sources: List of (relative_path, source_text, language) tuples.
                          Typically produced by RepositoryExplorer with
                          read_source=True and then mapped to this format.

        Returns:
            RepositoryASTAnalysis — never raises; per-file errors are recorded.
        """
        result = RepositoryASTAnalysis(root=root, name=name)

        for relative_path, source, language in file_sources:
            file_analysis = self.analyze_file(relative_path, source, language)
            result.files.append(file_analysis)

            if file_analysis.error:
                result.errors.append(file_analysis.error)

            # Aggregate routes
            result.routes.extend(file_analysis.routes)

            # Aggregate functions and classes
            result.all_functions.extend(file_analysis.functions)
            for cls in file_analysis.classes:
                result.all_classes.append(cls)
                result.all_functions.extend(cls.methods)

            # Build static relationships from imports
            for imp in file_analysis.imports:
                if imp.module:
                    result.relationships.append(
                        FileRelationship(
                            source_file=relative_path,
                            target_module=imp.module,
                            names=imp.names,
                            line=imp.line,
                        )
                    )

        return result

    # ── Convenience: analyze from filesystem ──────────────────────────────

    def analyze_repository_path(
        self,
        repo_root: Path,
        max_file_bytes: int = 1 * 1024 * 1024,
    ) -> RepositoryASTAnalysis:
        """High-level helper: explore a repository path and run AST analysis.

        This method uses the Phase 3 ingestion layer internally rather than
        reimplementing file discovery.  It is the recommended entry point for
        the API layer and for integration tests.

        Args:
            repo_root: Validated, resolved absolute path to the repository root.
            max_file_bytes: Passed through to RepositoryExplorer.

        Returns:
            RepositoryASTAnalysis with all Python files analyzed.
        """
        from app.ingestion.explorer import RepositoryExplorer

        explorer = RepositoryExplorer(max_file_bytes=max_file_bytes)
        exploration = explorer.explore(repo_root, read_source=True)

        file_sources: list[tuple[str, str, str]] = [
            (ev.relative_path, ev.content, ev.language)
            for ev in exploration.source_evidence
        ]

        return self.analyze_repository(
            root=str(repo_root),
            name=repo_root.name,
            file_sources=file_sources,
        )
