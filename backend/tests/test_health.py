"""Tests for the GET /health endpoint."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_200() -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_health_response_structure() -> None:
    response = client.get("/health")
    body = response.json()
    assert body["status"] == "healthy"
    assert body["service"] == "AI Software Mentor"
    assert "version" in body


def test_health_content_type() -> None:
    response = client.get("/health")
    assert "application/json" in response.headers["content-type"]
