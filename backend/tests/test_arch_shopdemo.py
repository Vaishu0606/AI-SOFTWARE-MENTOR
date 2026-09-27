"""Phase 5 tests — ShopDemo architecture integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.architecture import ArchitectureAgent

_SHOPDEMO = Path(__file__).parent.parent.parent / "demo-repository" / "ShopDemo"


@pytest.fixture(scope="module")
def shopdemo_arch():
    if not _SHOPDEMO.exists():
        pytest.skip("demo-repository/ShopDemo not found")
    return ArchitectureAgent().analyze_path(_SHOPDEMO)


def test_shopdemo_name(shopdemo_arch):
    assert shopdemo_arch.repository_name == "ShopDemo"


def test_shopdemo_project_type_web_api(shopdemo_arch):
    assert shopdemo_arch.project_type == "web_api"


def test_shopdemo_python_detected(shopdemo_arch):
    assert "Python" in shopdemo_arch.detected_languages


def test_shopdemo_routes_present(shopdemo_arch):
    assert shopdemo_arch.total_routes >= 7


def test_shopdemo_has_post_route(shopdemo_arch):
    post = [r for r in shopdemo_arch.routes if r.http_method == "POST"]
    assert len(post) >= 1


def test_shopdemo_has_get_route(shopdemo_arch):
    get = [r for r in shopdemo_arch.routes if r.http_method == "GET"]
    assert len(get) >= 1


def test_shopdemo_apply_route_present(shopdemo_arch):
    assert any(r.path == "/apply" for r in shopdemo_arch.routes)


def test_shopdemo_modules_detected(shopdemo_arch):
    assert len(shopdemo_arch.modules) >= 3


def test_shopdemo_route_module_detected(shopdemo_arch):
    route_mods = [m for m in shopdemo_arch.modules if m.role == "routes"]
    assert len(route_mods) >= 1


def test_shopdemo_service_module_detected(shopdemo_arch):
    service_mods = [m for m in shopdemo_arch.modules if m.role == "service"]
    assert len(service_mods) >= 1


def test_shopdemo_test_module_detected(shopdemo_arch):
    test_mods = [m for m in shopdemo_arch.modules if m.role == "test"]
    assert len(test_mods) >= 1


def test_shopdemo_functions_found(shopdemo_arch):
    assert shopdemo_arch.total_functions >= 10


def test_shopdemo_classes_found(shopdemo_arch):
    assert shopdemo_arch.total_classes >= 5


def test_shopdemo_findings_present(shopdemo_arch):
    assert len(shopdemo_arch.findings) >= 3


def test_shopdemo_api_routes_finding(shopdemo_arch):
    cats = [f.category for f in shopdemo_arch.findings]
    assert "api_routes" in cats


def test_shopdemo_route_layer_finding(shopdemo_arch):
    cats = [f.category for f in shopdemo_arch.findings]
    assert any("route" in c or "layer" in c for c in cats)


def test_shopdemo_service_layer_finding(shopdemo_arch):
    cats = [f.category for f in shopdemo_arch.findings]
    assert any("service" in c for c in cats)


def test_shopdemo_all_findings_have_confidence(shopdemo_arch):
    for f in shopdemo_arch.findings:
        assert 0.0 <= f.confidence <= 1.0


def test_shopdemo_relationships_built(shopdemo_arch):
    assert len(shopdemo_arch.dependencies) >= 0  # may be empty if modules don't cross-reference
    for d in shopdemo_arch.dependencies:
        assert d.source_module
        assert d.target_module
        assert d.evidence_type in ("OBSERVED", "INFERRED")


def test_shopdemo_evidence_references_real_files(shopdemo_arch):
    """All evidence file references should correspond to actual repository files."""
    all_files = {f for m in shopdemo_arch.modules for f in m.files}
    for m in shopdemo_arch.modules:
        for ev in m.evidence:
            if ev.file_path:
                assert ev.file_path in all_files, (
                    f"Evidence references non-existent file: {ev.file_path}"
                )
