"""Phase 3 tests — Repository Ingestion: RepositoryCloner."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ingestion.cloner import IngestionError, LocalSource, RepositoryCloner


@pytest.fixture
def cloner() -> RepositoryCloner:
    return RepositoryCloner()


# ── Valid path ─────────────────────────────────────────────────────────────────


def test_prepare_local_returns_resolved_path(tmp_path: Path, cloner: RepositoryCloner) -> None:
    result = cloner.prepare(LocalSource(path=str(tmp_path)))
    assert result == tmp_path.resolve()


def test_prepare_local_resolves_relative_components(tmp_path: Path, cloner: RepositoryCloner) -> None:
    # Path with a trailing separator — should still resolve cleanly
    result = cloner.prepare(LocalSource(path=str(tmp_path) + "/"))
    assert result.is_dir()


# ── Error cases ────────────────────────────────────────────────────────────────


def test_prepare_nonexistent_path_raises(cloner: RepositoryCloner) -> None:
    with pytest.raises(IngestionError) as exc_info:
        cloner.prepare(LocalSource(path="/absolutely/does/not/exist/12345"))
    assert "does not exist" in exc_info.value.reason.lower()


def test_prepare_empty_path_raises(cloner: RepositoryCloner) -> None:
    with pytest.raises(IngestionError) as exc_info:
        cloner.prepare(LocalSource(path="   "))
    assert exc_info.value.reason


def test_prepare_file_instead_of_dir_raises(tmp_path: Path, cloner: RepositoryCloner) -> None:
    file = tmp_path / "somefile.txt"
    file.write_text("hello")
    with pytest.raises(IngestionError) as exc_info:
        cloner.prepare(LocalSource(path=str(file)))
    assert "not a directory" in exc_info.value.reason.lower()


def test_ingestion_error_has_reason() -> None:
    err = IngestionError("custom reason")
    assert err.reason == "custom reason"
    assert "custom reason" in str(err)
