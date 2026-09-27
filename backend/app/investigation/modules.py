"""
Static module resolution for Phase 7.

Phase 4 records *what* was imported (``ImportInfo``) and Phase 5 records module
grouping, but neither maps an import back to the file that satisfies it.  The
investigators need that mapping to say "this route calls that service", so it
lives here — one small, deterministic resolver, shared by the Code and Dependency
investigators so they cannot disagree.

Method
------
Purely static, name-based resolution over Phase 4 data:

1. every analysed file is registered under the dotted module path it would have
   (also under each of its parents, so ``pkg/mod.py`` is reachable as ``pkg``),
2. a call expression such as ``cart_service.get_cart`` is resolved by matching
   its first segment against the importing file's own imports,
3. the resulting dotted module is looked up, walking up the package chain until
   something matches.

Limitations, stated honestly
----------------------------
This is *name-based* resolution, not a type checker.  Dynamic imports,
``importlib`` tricks, and re-exports through ``__init__.py`` are not followed.
Every result is therefore an ``OBSERVATION`` about static structure, and the
investigators phrase it as such — never as proof of runtime behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.analysis.ast_analyzer import FileAnalysis, RepositoryASTAnalysis


@dataclass(frozen=True)
class ImportEdge:
    """One import statement, resolved to the file that provides the target."""

    source_file: str
    #: The module the import statement names, e.g. ``app.services.discount_service``.
    target_module: str
    #: The package/module the statement came from, e.g. ``app.services``.  Equal
    #: to ``target_module`` for plain ``import x.y`` statements.
    base_module: str
    #: Repository-relative file providing ``target_module``, when resolvable to
    #: a file *inside this repository*.  Third-party and standard-library imports
    #: deliberately resolve to None.
    target_path: str | None
    names: tuple[str, ...]
    line: int
    is_from_import: bool

    @property
    def resolved(self) -> bool:
        return self.target_path is not None

    @property
    def internal(self) -> bool:
        """True when the import resolves to a file inside the repository."""
        return self.target_path is not None


@dataclass(frozen=True)
class ResolvedCall:
    """A call site resolved to a file (and, when known, a symbol) in it."""

    source_file: str
    line: int
    callable_expr: str
    target_path: str | None
    target_module: str | None
    target_symbol: str | None
    #: The name the caller used, e.g. ``cart_service.get_cart``.
    callee_expr: str

    @property
    def resolved(self) -> bool:
        return self.target_path is not None


@dataclass
class ModuleIndex:
    """Lookup tables for one repository, built once per snapshot."""

    path_to_module: dict[str, str] = field(default_factory=dict)
    module_to_path: dict[str, str] = field(default_factory=dict)
    analysis_by_path: dict[str, FileAnalysis] = field(default_factory=dict)

    @classmethod
    def build(cls, ast: RepositoryASTAnalysis) -> "ModuleIndex":
        index = cls(analysis_by_path={f.relative_path: f for f in ast.files})
        for relative_path in index.analysis_by_path:
            module = module_name_for(relative_path)
            index.path_to_module[relative_path] = module
            index.module_to_path.setdefault(module, relative_path)
            # Register parents so `from pkg import mod` and `import pkg` both
            # resolve, without claiming a file *is* its own parent package.
            parts = module.split(".")
            for depth in range(1, len(parts)):
                parent = ".".join(parts[:depth])
                index.module_to_path.setdefault(parent, relative_path)
        return index

    def path_for(self, module: str) -> str | None:
        """Resolve a dotted module to a file, walking up the package chain."""
        candidate = module
        while candidate:
            path = self.module_to_path.get(candidate)
            if path is not None:
                return path
            if "." not in candidate:
                return None
            candidate = candidate.rsplit(".", 1)[0]
        return None

    def imports_of(self, file_path: str) -> list[ImportEdge]:
        """Every import of ``file_path``, with targets resolved where possible."""
        analysis = self.analysis_by_path.get(file_path)
        if analysis is None:
            return []
        edges: list[ImportEdge] = []
        for imp in analysis.imports:
            if not imp.module:
                continue
            for name in imp.names or [""]:
                if imp.is_from_import:
                    target_module = f"{imp.module}.{name}" if name else imp.module
                else:
                    target_module = name or imp.module
                resolved = self.path_for(target_module)
                edges.append(
                    ImportEdge(
                        source_file=file_path,
                        target_module=target_module,
                        base_module=imp.module,
                        target_path=(
                            resolved if resolved in self.analysis_by_path else None
                        ),
                        names=tuple(imp.names),
                        line=imp.line,
                        is_from_import=imp.is_from_import,
                    )
                )
        return edges

    def resolve_callee(self, file_path: str, callable_expr: str, line: int) -> ResolvedCall:
        """Resolve a call expression written in ``file_path`` to a target file.

        Unresolvable calls return a :class:`ResolvedCall` with
        ``target_path=None`` rather than raising or guessing.
        """
        cleaned = callable_expr.strip()
        symbol: str | None = None
        head = cleaned
        if "." in cleaned:
            head, _, symbol = cleaned.partition(".")

        base_module: str | None = None
        analysis = self.analysis_by_path.get(file_path)
        if analysis is not None:
            for imp in analysis.imports:
                # `import a.b as c` binds the name `c`; aliases maps a.b -> c.
                for original, alias in imp.aliases.items():
                    if alias == head:
                        base_module = original
                        break
                if base_module is None and imp.is_from_import and head in imp.names:
                    base_module = f"{imp.module}.{head}" if imp.module else head
                    break
                if base_module is None and not imp.is_from_import:
                    for name in imp.names:
                        if name.split(".")[0] == head:
                            base_module = name
                            break
                    if base_module is not None:
                        break

        target_path = self.path_for(base_module) if base_module else None
        return ResolvedCall(
            source_file=file_path,
            line=line,
            callable_expr=cleaned,
            target_path=target_path,
            target_module=base_module,
            target_symbol=symbol,
            callee_expr=cleaned,
        )

    def calls_from(self, file_path: str) -> list[ResolvedCall]:
        """Every call site in ``file_path`` (functions and top level)."""
        analysis = self.analysis_by_path.get(file_path)
        if analysis is None:
            return []
        call_infos = list(analysis.top_level_calls)
        for fn in analysis.functions:
            call_infos.extend(fn.calls)
        for cls in analysis.classes:
            for method in cls.methods:
                call_infos.extend(method.calls)
        # Deduplicate: the same call can appear in both a function and the
        # aggregate list.
        seen: set[tuple[str, int, str]] = set()
        resolved: list[ResolvedCall] = []
        for call in sorted(call_infos, key=lambda c: (c.line, c.callable_expr)):
            key = (call.callable_expr, call.line)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(
                self.resolve_callee(file_path, call.callable_expr, call.line)
            )
        return resolved

    def shared_imports(self, file_paths: list[str]) -> dict[str, list[str]]:
        """Map a target module to the files importing it (≥1 importer).

        Used to answer "do these two areas of the code meet anywhere?" without
        knowing anything about the domain.
        """
        shared: dict[str, list[str]] = {}
        for file_path in file_paths:
            seen_here: set[str] = set()
            for edge in self.imports_of(file_path):
                if edge.target_module in seen_here:
                    continue
                seen_here.add(edge.target_module)
                shared.setdefault(edge.target_module, []).append(file_path)
        return {m: sorted(importers) for m, importers in sorted(shared.items())}

    def shared_internal_imports(self, file_paths: list[str]) -> dict[str, list[str]]:
        """Map a **repository-internal** file to the files importing it.

        Third-party and standard-library imports are excluded, and dunder
        modules such as ``__future__`` are excluded, because "these files all
        import pydantic" tells a bug investigation nothing.  Keyed by resolved
        file path rather than module string, so ``from a.b import c`` and
        ``import a.b`` agree.
        """
        shared: dict[str, list[str]] = {}
        for file_path in file_paths:
            seen_here: set[str] = set()
            for edge in self.imports_of(file_path):
                if not edge.internal or edge.target_path in seen_here:
                    continue
                if any(part.startswith("__") for part in edge.base_module.split(".")):
                    continue
                if edge.target_path == file_path:
                    continue
                seen_here.add(edge.target_path)
                shared.setdefault(edge.target_path, []).append(file_path)
        return {p: sorted(importers) for p, importers in sorted(shared.items())}

    def functions_named(self, file_path: str) -> dict[str, int]:
        """Map every function/method name in ``file_path`` to its line."""
        analysis = self.analysis_by_path.get(file_path)
        if analysis is None:
            return {}
        names: dict[str, int] = {}
        for fn in analysis.functions:
            names.setdefault(fn.name, fn.line_start)
        for cls in analysis.classes:
            for method in cls.methods:
                names.setdefault(f"{cls.name}.{method.name}", method.line_start)
                names.setdefault(method.name, method.line_start)
        return names


def module_name_for(relative_path: str) -> str:
    """Return the dotted module path a repository file would be imported as.

    ``app/services/cart_service.py`` → ``app.services.cart_service``
    ``app/models.py``              → ``app.models``
    ``tests/test_cart.py``         → ``tests.test_cart``
    """
    path = relative_path
    if path.endswith(".py"):
        path = path[: -len(".py")]
    if path.endswith("/__init__") or path == "__init__":
        path = path[: -len("/__init__")] if path.endswith("/__init__") else ""
    return path.replace("/", ".")


__all__ = [
    "ImportEdge",
    "ModuleIndex",
    "ResolvedCall",
    "module_name_for",
]
