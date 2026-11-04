"""test_health.py - Kiểm thử endpoint GET /health của Control Node (PC1)."""

from datetime import datetime
from fastapi.testclient import TestClient

from control.app.main import app, settings


def test_health_endpoint_public_returns_200() -> None:
    """GET /health là endpoint công khai, trả về HTTP 200 và thông tin sức khỏe của Control Node."""
    client = TestClient(app)
    response = client.get("/health")

    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "healthy"
    assert data["node"] == "control-node"
    assert data["role"] == "CONTROL"
    assert data["environment"] == settings.APP_ENV
    assert data["version"] == "1.0.0"
    assert "timestamp" in data

    # Kiểm tra timestamp là chuỗi ISO 8601 hợp lệ
    parsed_time = datetime.fromisoformat(data["timestamp"])
    assert parsed_time.tzinfo is not None  # Bắt buộc có timezone (UTC)
