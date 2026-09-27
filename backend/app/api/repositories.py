"""Repository ingestion API — Phase 3.

POST /api/repositories/explore
    Accepts a local repository path and returns structured exploration results.

Security notes:
- The supplied path is validated to be an existing directory.
- The explorer enforces all path-traversal, secret, binary, and size guards.
- This endpoint is intended for local development; it does not expose arbitrary
  files — it returns metadata and optionally source content, never .env contents.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from app.ingestion.cloner import IngestionError, LocalSource, RepositoryCloner
from app.ingestion.explorer import (
    RepositoryExplorer,
    RepositoryExploration,
    SkippedFile,
)
from app.models.domain import FileInfo, RepositoryInfo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/repositories", tags=["repositories"])


# ── Request / Response schemas ────────────────────────────────────────────────


class ExploreRequest(BaseModel):
    """Request body for POST /api/repositories/explore."""

    path: str
    read_source: bool = False  # if True, also return source file contents

    @field_validator("path")
    @classmethod
    def path_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("path must not be empty")
        return v


class SkippedFileResponse(BaseModel):
    relative_path: str
    reason: str


class ExploreResponse(BaseModel):
    """Response body for POST /api/repositories/explore."""

    repository: RepositoryInfo
    # Source evidence is separate to keep RepositoryInfo slim
    source_evidence: list[dict[str, str | int]] = []
    skipped_files: list[SkippedFileResponse] = []


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post("/explore", response_model=ExploreResponse)
async def explore_repository(body: ExploreRequest) -> ExploreResponse:
    """Explore a local repository and return structured metadata.

    The path must be an existing directory on the local filesystem.
    All exploration is read-only; no files are modified.
    """
    cloner = RepositoryCloner()

    try:
        repo_root = cloner.prepare(LocalSource(path=body.path))
    except IngestionError as exc:
        raise HTTPException(status_code=400, detail=exc.reason) from exc

    explorer = RepositoryExplorer()

    try:
        exploration = explorer.explore(repo_root, read_source=body.read_source)
    except Exception as exc:
        logger.exception("Unexpected error during repository exploration")
        raise HTTPException(
            status_code=500, detail=f"Exploration failed: {exc}"
        ) from exc

    return _to_response(exploration)


# ── Mapping helpers ───────────────────────────────────────────────────────────


def _to_response(e: RepositoryExploration) -> ExploreResponse:
    """Convert the internal RepositoryExploration to the API response model."""
    file_infos = [
        FileInfo(
            relative_path=f.relative_path,
            extension=f.extension,
            language=f.language,
            size_bytes=f.size_bytes,
            is_test=f.is_test,
            is_config=f.is_config,
            is_secret=f.is_secret,
        )
        for f in e.files
    ]

    repo_info = RepositoryInfo(
        local_path=e.root,
        name=e.name,
        primary_language=e.primary_language,
        test_files=[f.relative_path for f in e.test_files],
        relevant_files=[f.relative_path for f in e.source_files],
        total_files=e.total_files,
        directories=e.directories,
        all_files=file_infos,
        config_files=[f.relative_path for f in e.config_files],
        languages=e.languages,
        skipped_files=[
            {"relative_path": s.relative_path, "reason": s.reason}
            for s in e.skipped_files
        ],
    )

    source_evidence = [
        {
            "relative_path": ev.relative_path,
            "language": ev.language,
            "content": ev.content,
            "line_count": str(ev.line_count),
            "size_bytes": str(ev.size_bytes),
        }
        for ev in e.source_evidence
    ]

    skipped = [
        SkippedFileResponse(relative_path=s.relative_path, reason=s.reason)
        for s in e.skipped_files
    ]

    return ExploreResponse(
        repository=repo_info,
        source_evidence=source_evidence,
        skipped_files=skipped,
    )
