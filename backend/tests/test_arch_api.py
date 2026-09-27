"""Phase 5 tests — Architecture API endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
_SHOPDEMO = Path(__file__).parent.parent.parent / "demo-repository" / "ShopDemo"


def test_architecture_endpoint_returns_200(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n"
    )
    resp = client.post("/api/repositories/architecture", json={"path": str(tmp_path)})
    assert resp.status_code == 200


def test_architecture_endpoint_response_structure(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def run(): pass\n")
    resp = client.post("/api/repositories/architecture", json={"path": str(tmp_path)})
    body = resp.json()
    for key in ("repository_name", "project_type", "modules", "routes",
                "findings", "total_files", "total_functions", "total_routes"):
        assert key in body


def test_architecture_endpoint_detects_routes(tmp_path: Path) -> None:
    (tmp_path / "routes.py").write_text(
        "@router.get('/items')\nasync def list_items(): pass\n"
    )
    resp = client.post("/api/repositories/architecture", json={"path": str(tmp_path)})
    body = resp.json()
    assert any(r["path"] == "/items" for r in body["routes"])


def test_architecture_endpoint_bad_path_returns_400() -> None:
    resp = client.post(
        "/api/repositories/architecture",
        json={"path": "/absolutely/nonexistent/12345"},
    )
    assert resp.status_code == 400


def test_architecture_endpoint_empty_path_returns_422() -> None:
    resp = client.post("/api/repositories/architecture", json={"path": "   "})
    assert resp.status_code in (400, 422)


def test_shopdemo_via_architecture_api() -> None:
    if not _SHOPDEMO.exists():
        pytest.skip("demo-repository/ShopDemo not found")
    resp = client.post(
        "/api/repositories/architecture",
        json={"path": str(_SHOPDEMO)},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["repository_name"] == "ShopDemo"
    assert body["project_type"] == "web_api"
    assert body["total_routes"] >= 7
    assert any(r["path"] == "/apply" for r in body["routes"])
    assert len(body["findings"]) >= 3
