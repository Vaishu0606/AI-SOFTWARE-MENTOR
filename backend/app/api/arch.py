"""
Architecture analysis API — Phase 5.

POST /api/repositories/architecture
    Accepts a local repository path, runs Phase 3 ingestion → Phase 4 AST
    analysis → Phase 5 Architecture Agent, and returns a structured
    ArchitectureAnalysis.

Reuses the Phase 3 ingestion layer for path validation.  No code is executed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.agents.architecture import (
    ArchitectureAgent,
    ArchitectureAnalysis,
    ArchitectureFinding,
    DataFlow,
    DependencyEdge,
    EntryPoint,
    EvidenceRef,
    ModuleInfo,
    RouteSummary,
)
from app.ingestion.cloner import IngestionError, LocalSource, RepositoryCloner

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/repositories", tags=["repositories"])


# ── Request schema ────────────────────────────────────────────────────────────


class ArchitectureRequest(BaseModel):
    path: str

    @field_validator("path")
    @classmethod
    def path_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("path must not be empty")
        return v


# ── Response schemas (Pydantic mirrors of the dataclasses) ───────────────────


class EvidenceRefResponse(BaseModel):
    file_path: str
    detail: str
    evidence_type: str


class ModuleInfoResponse(BaseModel):
    name: str
    role: str
    files: list[str]
    primary_language: str
    function_count: int
    class_count: int
    route_count: int
    evidence: list[EvidenceRefResponse]


class EntryPointResponse(BaseModel):
    file_path: str
    kind: str
    framework: str | None
    symbol: str | None
    line: int | None
    evidence: list[EvidenceRefResponse]


class DependencyEdgeResponse(BaseModel):
    source_module: str
    target_module: str
    source_files: list[str]
    evidence_type: str
    description: str


class RouteSummaryResponse(BaseModel):
    http_method: str
    path: str
    handler: str
    file_path: str
    module_name: str
    line: int


class DataFlowResponse(BaseModel):
    name: str
    steps: list[str]
    evidence_type: str
    source_files: list[str]


class ArchitectureFindingResponse(BaseModel):
    category: str
    description: str
    evidence: list[EvidenceRefResponse]
    confidence: float
    related_files: list[str]


class ArchitectureAnalysisResponse(BaseModel):
    repository_name: str
    repository_root: str
    project_type: str
    detected_languages: list[str]
    modules: list[ModuleInfoResponse]
    entry_points: list[EntryPointResponse]
    routes: list[RouteSummaryResponse]
    dependencies: list[DependencyEdgeResponse]
    data_flows: list[DataFlowResponse]
    findings: list[ArchitectureFindingResponse]
    total_files: int
    total_functions: int
    total_classes: int
    total_routes: int
    notes: list[str]


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post("/architecture", response_model=ArchitectureAnalysisResponse)
async def architecture_analysis(body: ArchitectureRequest) -> ArchitectureAnalysisResponse:
    """Run the Architecture Agent on a local repository.

    Performs Phase 3 ingestion → Phase 4 AST analysis → Phase 5 architecture
    analysis.  No code is executed.  All analysis is static.
    """
    cloner = RepositoryCloner()
    try:
        repo_root = cloner.prepare(LocalSource(path=body.path))
    except IngestionError as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from exc

    agent = ArchitectureAgent()
    try:
        result = agent.analyze_path(repo_root)
    except Exception as exc:
        logger.exception("Unexpected error during architecture analysis")
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc

    return _to_response(result)


# ── Mapping helpers ───────────────────────────────────────────────────────────


def _ev(e: EvidenceRef) -> EvidenceRefResponse:
    return EvidenceRefResponse(
        file_path=e.file_path, detail=e.detail, evidence_type=e.evidence_type
    )


def _to_response(a: ArchitectureAnalysis) -> ArchitectureAnalysisResponse:
    return ArchitectureAnalysisResponse(
        repository_name=a.repository_name,
        repository_root=a.repository_root,
        project_type=a.project_type,
        detected_languages=a.detected_languages,
        modules=[
            ModuleInfoResponse(
                name=m.name,
                role=m.role,
                files=m.files,
                primary_language=m.primary_language,
                function_count=m.function_count,
                class_count=m.class_count,
                route_count=m.route_count,
                evidence=[_ev(e) for e in m.evidence],
            )
            for m in a.modules
        ],
        entry_points=[
            EntryPointResponse(
                file_path=ep.file_path,
                kind=ep.kind,
                framework=ep.framework,
                symbol=ep.symbol,
                line=ep.line,
                evidence=[_ev(e) for e in ep.evidence],
            )
            for ep in a.entry_points
        ],
        routes=[
            RouteSummaryResponse(
                http_method=r.http_method,
                path=r.path,
                handler=r.handler,
                file_path=r.file_path,
                module_name=r.module_name,
                line=r.line,
            )
            for r in a.routes
        ],
        dependencies=[
            DependencyEdgeResponse(
                source_module=d.source_module,
                target_module=d.target_module,
                source_files=d.source_files,
                evidence_type=d.evidence_type,
                description=d.description,
            )
            for d in a.dependencies
        ],
        data_flows=[
            DataFlowResponse(
                name=f.name,
                steps=f.steps,
                evidence_type=f.evidence_type,
                source_files=f.source_files,
            )
            for f in a.data_flows
        ],
        findings=[
            ArchitectureFindingResponse(
                category=f.category,
                description=f.description,
                evidence=[_ev(e) for e in f.evidence],
                confidence=f.confidence,
                related_files=f.related_files,
            )
            for f in a.findings
        ],
        total_files=a.total_files,
        total_functions=a.total_functions,
        total_classes=a.total_classes,
        total_routes=a.total_routes,
        notes=a.notes,
    )
