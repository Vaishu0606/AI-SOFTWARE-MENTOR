"""Phase 3 tests — Repository API endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


# ── Valid exploration ──────────────────────────────────────────────────────────


def test_explore_endpoint_returns_200(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n")
    response = client.post(
        "/api/repositories/explore",
        json={"path": str(tmp_path)},
    )
    assert response.status_code == 200


def test_explore_endpoint_response_structure(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n")
    response = client.post(
        "/api/repositories/explore",
        json={"path": str(tmp_path)},
    )
    body = response.json()
    assert "repository" in body
    assert "skipped_files" in body


def test_explore_endpoint_repository_name(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("x = 1\n")
    response = client.post(
        "/api/repositories/explore",
        json={"path": str(tmp_path)},
    )
    body = response.json()
    assert body["repository"]["name"] == tmp_path.name


def test_explore_endpoint_detects_python_files(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text("def run(): pass\n")
    response = client.post(
        "/api/repositories/explore",
        json={"path": str(tmp_path)},
    )
    body = response.json()
    all_files = body["repository"]["all_files"]
    py_files = [f for f in all_files if f["extension"] == ".py"]
    assert len(py_files) > 0


def test_explore_endpoint_detects_test_files(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_x(): pass\n")
    response = client.post(
        "/api/repositories/explore",
        json={"path": str(tmp_path)},
    )
    body = response.json()
    test_files = body["repository"]["test_files"]
    assert any("test_main.py" in t for t in test_files)


def test_explore_endpoint_languages_present(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1")
    (tmp_path / "b.ts").write_text("const y = 2;")
    response = client.post(
        "/api/repositories/explore",
        json={"path": str(tmp_path)},
    )
    body = response.json()
    langs = body["repository"]["languages"]
    assert "Python" in langs
    assert "TypeScript" in langs


# ── Error cases ────────────────────────────────────────────────────────────────


def test_explore_nonexistent_path_returns_400() -> None:
    response = client.post(
        "/api/repositories/explore",
        json={"path": "/this/path/does/not/exist/xyz123"},
    )
    assert response.status_code == 400
    assert "detail" in response.json()


def test_explore_empty_path_returns_422() -> None:
    response = client.post(
        "/api/repositories/explore",
        json={"path": "   "},
    )
    # Either 400 (IngestionError) or 422 (Pydantic validation)
    assert response.status_code in (400, 422)


def test_explore_file_path_returns_400(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("hello")
    response = client.post(
        "/api/repositories/explore",
        json={"path": str(f)},
    )
    assert response.status_code == 400


# ── .env protection via API ───────────────────────────────────────────────────


def test_env_contents_not_in_api_response(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=exposed_in_api_12345\n")
    (tmp_path / "app.py").write_text("x = 1\n")

    response = client.post(
        "/api/repositories/explore",
        json={"path": str(tmp_path), "read_source": True},
    )
    assert response.status_code == 200
    # The secret value must not appear anywhere in the response body
    assert "exposed_in_api_12345" not in response.text


# ── ShopDemo integration via API ──────────────────────────────────────────────


def test_shopdemo_via_api() -> None:
    """Call the API against the actual ShopDemo repository."""
    import os
    from pathlib import Path as P

    tests_dir = P(__file__).parent
    shopdemo_path = tests_dir.parent.parent / "demo-repository" / "ShopDemo"

    if not shopdemo_path.exists():
        pytest.skip("demo-repository/ShopDemo not found")

    response = client.post(
        "/api/repositories/explore",
        json={"path": str(shopdemo_path)},
    )
    assert response.status_code == 200
    body = response.json()

    repo = body["repository"]
    assert repo["name"] == "ShopDemo"
    assert repo["primary_language"] == "Python"
    assert any("test_cart.py" in t for t in repo["test_files"])
    assert any("requirements.txt" in c for c in repo["config_files"])
