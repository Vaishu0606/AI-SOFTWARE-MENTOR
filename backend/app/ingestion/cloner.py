"""
Repository cloner / ingestion abstraction.

Phase 3 supports LOCAL repositories only.
A future phase will add GitPython-based remote cloning (GitHub URL).

Design:
    RepositorySource   — union of supported source types
    LocalSource        — a path already on disk
    RepositoryCloner   — validates / prepares the repository root
    IngestionError     — application-level error (never silently continues)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# ── Source types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LocalSource:
    """A repository that already exists on the local filesystem."""

    path: str


# Future: add RemoteSource(url: str, branch: str | None) here in a later phase.
RepositorySource = LocalSource


# ── Errors ────────────────────────────────────────────────────────────────────


class IngestionError(Exception):
    """Raised when repository ingestion fails.

    Always carries a human-readable reason.  Never swallowed silently.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# ── Cloner ────────────────────────────────────────────────────────────────────


class RepositoryCloner:
    """Validates and prepares a repository root for exploration.

    For local sources, this resolves and validates the path.
    For future remote sources, this will perform the clone into an isolated
    session directory.
    """

    def prepare(self, source: RepositorySource) -> Path:
        """Validate the source and return an absolute, resolved repository root.

        Args:
            source: A ``LocalSource`` (or future ``RemoteSource``).

        Returns:
            Absolute ``Path`` to the repository root.

        Raises:
            IngestionError: If the path does not exist, is not a directory,
                            or is not readable.
        """
        if isinstance(source, LocalSource):
            return self._prepare_local(source)
        # Future: elif isinstance(source, RemoteSource): return self._clone_remote(source)
        raise IngestionError(f"Unsupported source type: {type(source).__name__}")

    # ── private ───────────────────────────────────────────────────────────────

    def _prepare_local(self, source: LocalSource) -> Path:
        raw = source.path.strip()
        if not raw:
            raise IngestionError("Repository path must not be empty.")

        path = Path(raw).expanduser().resolve()

        if not path.exists():
            raise IngestionError(
                f"Repository path does not exist: {path}"
            )
        if not path.is_dir():
            raise IngestionError(
                f"Repository path is not a directory: {path}"
            )
        if not os.access(path, os.R_OK):
            raise IngestionError(
                f"Repository path is not readable: {path}"
            )

        return path
