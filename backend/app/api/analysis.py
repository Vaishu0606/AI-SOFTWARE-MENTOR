"""
AST analysis API — Phase 4.

POST /api/repositories/analyze
    Accepts a local repository path, runs Phase 3 ingestion + Phase 4 AST
    analysis, and returns structured code-level information.

The endpoint reuses the Phase 3 ingestion layer for path validation and file
discovery — it does not re-implement those concerns.

Security: same rules as the Phase 3 explore endpoint.  Source files are
analyzed statically.  No code is executed.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.analysis.ast_analyzer import (
    ASTAnalyzer,
    AnalysisError,
    CallInfo,
    ClassInfo,
    FileAnalysis,
    FileRelationship,
    FunctionInfo,
    ImportInfo,
    RepositoryASTAnalysis,
    RouteInfo,
)
from app.ingestion.cloner import IngestionError, LocalSource, RepositoryCloner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/repositories", tags=["repositories"])


# ── Request / Response schemas ────────────────────────────────────────────────


class AnalyzeRequest(BaseModel):
    """Request body for POST /api/repositories/analyze."""

    path: str

    @field_validator("path")
    @classmethod
    def path_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("path must not be empty")
        return v


# ── Pydantic response models (mirrors dataclasses for JSON serialization) ─────


class ImportInfoResponse(BaseModel):
    module: str
    names: list[str]
    aliases: dict[str, str]
    is_from_import: bool
    line: int


class CallInfoResponse(BaseModel):
    callable_expr: str
    line: int


class FunctionInfoResponse(BaseModel):
    name: str
    line_start: int
    line_end: int
    arguments: list[str]
    decorators: list[str]
    is_async: bool
    is_method: bool
    class_name: str | None
    calls: list[CallInfoResponse]
    docstring: str | None


class ClassInfoResponse(BaseModel):
    name: str
    line_start: int
    line_end: int
    bases: list[str]
    decorators: list[str]
    methods: list[FunctionInfoResponse]
    docstring: str | None


class RouteInfoResponse(BaseModel):
    http_method: str
    path: str
    function_name: str
    file_path: str
    line: int
    router_var: str


class AnalysisErrorResponse(BaseModel):
    file_path: str
    error_type: str
    message: str


class FileAnalysisResponse(BaseModel):
    relative_path: str
    language: str
    supported: bool
    imports: list[ImportInfoResponse]
    functions: list[FunctionInfoResponse]
    classes: list[ClassInfoResponse]
    routes: list[RouteInfoResponse]
    top_level_calls: list[CallInfoResponse]
    error: AnalysisErrorResponse | None


class FileRelationshipResponse(BaseModel):
    source_file: str
    target_module: str
    names: list[str]
    line: int


class AnalyzeResponse(BaseModel):
    """Response body for POST /api/repositories/analyze."""

    root: str
    name: str
    files: list[FileAnalysisResponse]
    routes: list[RouteInfoResponse]
    relationships: list[FileRelationshipResponse]
    errors: list[AnalysisErrorResponse]
    summary: dict[str, int]  # quick stats: total_files, analyzed, errors, routes


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze_repository(body: AnalyzeRequest) -> AnalyzeResponse:
    """Run AST analysis on a local repository.

    The repository is first validated and explored via the Phase 3 ingestion
    layer, then each Python source file is analyzed with the Phase 4 AST
    analyzer.  No code is executed.
    """
    cloner = RepositoryCloner()
    try:
        repo_root = cloner.prepare(LocalSource(path=body.path))
    except IngestionError as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from exc

    analyzer = ASTAnalyzer()
    try:
        result = analyzer.analyze_repository_path(repo_root)
    except Exception as exc:
        logger.exception("Unexpected error during AST analysis")
        raise HTTPException(
            status_code=500, detail=f"Analysis failed: {exc}"
        ) from exc

    return _to_response(result)


# ── Mapping helpers ───────────────────────────────────────────────────────────


def _map_call(c: CallInfo) -> CallInfoResponse:
    return CallInfoResponse(callable_expr=c.callable_expr, line=c.line)


def _map_function(f: FunctionInfo) -> FunctionInfoResponse:
    return FunctionInfoResponse(
        name=f.name,
        line_start=f.line_start,
        line_end=f.line_end,
        arguments=f.arguments,
        decorators=f.decorators,
        is_async=f.is_async,
        is_method=f.is_method,
        class_name=f.class_name,
        calls=[_map_call(c) for c in f.calls],
        docstring=f.docstring,
    )


def _map_class(cls: ClassInfo) -> ClassInfoResponse:
    return ClassInfoResponse(
        name=cls.name,
        line_start=cls.line_start,
        line_end=cls.line_end,
        bases=cls.bases,
        decorators=cls.decorators,
        methods=[_map_function(m) for m in cls.methods],
        docstring=cls.docstring,
    )


def _map_route(r: RouteInfo) -> RouteInfoResponse:
    return RouteInfoResponse(
        http_method=r.http_method,
        path=r.path,
        function_name=r.function_name,
        file_path=r.file_path,
        line=r.line,
        router_var=r.router_var,
    )


def _map_error(e: AnalysisError) -> AnalysisErrorResponse:
    return AnalysisErrorResponse(
        file_path=e.file_path,
        error_type=e.error_type,
        message=e.message,
    )


def _map_file(fa: FileAnalysis) -> FileAnalysisResponse:
    return FileAnalysisResponse(
        relative_path=fa.relative_path,
        language=fa.language,
        supported=fa.supported,
        imports=[
            ImportInfoResponse(
                module=i.module,
                names=i.names,
                aliases=i.aliases,
                is_from_import=i.is_from_import,
                line=i.line,
            )
            for i in fa.imports
        ],
        functions=[_map_function(f) for f in fa.functions],
        classes=[_map_class(c) for c in fa.classes],
        routes=[_map_route(r) for r in fa.routes],
        top_level_calls=[_map_call(c) for c in fa.top_level_calls],
        error=_map_error(fa.error) if fa.error else None,
    )


def _to_response(result: RepositoryASTAnalysis) -> AnalyzeResponse:
    analyzed = sum(1 for f in result.files if f.supported and f.error is None)
    return AnalyzeResponse(
        root=result.root,
        name=result.name,
        files=[_map_file(f) for f in result.files],
        routes=[_map_route(r) for r in result.routes],
        relationships=[
            FileRelationshipResponse(
                source_file=rel.source_file,
                target_module=rel.target_module,
                names=rel.names,
                line=rel.line,
            )
            for rel in result.relationships
        ],
        errors=[_map_error(e) for e in result.errors],
        summary={
            "total_files": len(result.files),
            "analyzed": analyzed,
            "errors": len(result.errors),
            "routes": len(result.routes),
            "functions": len(result.all_functions),
            "classes": len(result.all_classes),
        },
    )
