"""Phase 4 tests — AST Analysis API endpoint."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

_TESTS_DIR = Path(__file__).parent
_SHOPDEMO = _TESTS_DIR.parent.parent / "demo-repository" / "ShopDemo"


# ── Valid analysis ─────────────────────────────────────────────────────────────


def test_analyze_endpoint_returns_200(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def hello(): pass\n")
    response = client.post(
        "/api/repositories/analyze",
        json={"path": str(tmp_path)},
    )
    assert response.status_code == 200


def test_analyze_endpoint_response_structure(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("def hello(): pass\n")
    response = client.post(
        "/api/repositories/analyze",
        json={"path": str(tmp_path)},
    )
    body = response.json()
    assert "files" in body
    assert "routes" in body
    assert "relationships" in body
    assert "errors" in body
    assert "summary" in body


def test_analyze_endpoint_summary_counts(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f(): pass\n")
    (tmp_path / "b.py").write_text("def g(): pass\n")
    response = client.post(
        "/api/repositories/analyze",
        json={"path": str(tmp_path)},
    )
    body = response.json()
    assert body["summary"]["total_files"] >= 2
    assert body["summary"]["functions"] >= 2


def test_analyze_detects_routes(tmp_path: Path) -> None:
    (tmp_path / "routes.py").write_text(
        "@router.get('/items')\nasync def list_items(): pass\n"
    )
    response = client.post(
        "/api/repositories/analyze",
        json={"path": str(tmp_path)},
    )
    body = response.json()
    routes = body["routes"]
    assert any(r["path"] == "/items" for r in routes)


def test_analyze_detects_imports(tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text("from pathlib import Path\nimport os\n")
    response = client.post(
        "/api/repositories/analyze",
        json={"path": str(tmp_path)},
    )
    body = response.json()
    file_data = next(f for f in body["files"] if f["relative_path"] == "mod.py")
    modules = [i["module"] for i in file_data["imports"]]
    assert "pathlib" in modules
    assert "os" in modules


# ── Error cases ────────────────────────────────────────────────────────────────


def test_analyze_nonexistent_path_returns_400() -> None:
    response = client.post(
        "/api/repositories/analyze",
        json={"path": "/this/does/not/exist/xyz123"},
    )
    assert response.status_code == 400


def test_analyze_empty_path_returns_422() -> None:
    response = client.post(
        "/api/repositories/analyze",
        json={"path": "   "},
    )
    assert response.status_code in (400, 422)


def test_analyze_syntax_error_file_recorded(tmp_path: Path) -> None:
    """A syntax-error file must not abort analysis — error must be in response."""
    (tmp_path / "bad.py").write_text("def broken(:\n    pass\n")
    (tmp_path / "good.py").write_text("def ok(): pass\n")
    response = client.post(
        "/api/repositories/analyze",
        json={"path": str(tmp_path)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["errors"] >= 1
    # good.py must still be analyzed
    good_file = next(
        (f for f in body["files"] if f["relative_path"] == "good.py"), None
    )
    assert good_file is not None
    assert good_file["error"] is None


# ── ShopDemo via API ───────────────────────────────────────────────────────────


def test_shopdemo_analyze_via_api() -> None:
    if not _SHOPDEMO.exists():
        pytest.skip("demo-repository/ShopDemo not found")

    response = client.post(
        "/api/repositories/analyze",
        json={"path": str(_SHOPDEMO)},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["name"] == "ShopDemo"
    assert body["summary"]["routes"] > 0
    assert body["summary"]["functions"] > 0

    # Discount apply route must be present
    routes = body["routes"]
    assert any(r["path"] == "/apply" for r in routes)

    # calculate_discount_cents must be in functions
    all_fns = []
    for f in body["files"]:
        all_fns.extend(f["functions"])
    fn_names = [f["name"] for f in all_fns]
    assert "calculate_discount_cents" in fn_names
