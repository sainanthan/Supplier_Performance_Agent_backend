from fastapi.testclient import TestClient

from app.main import app


def test_deployed_frontend_origin_is_allowed():
    origin = "https://supplier-performance-agent-frontend-sable.vercel.app"

    response = TestClient(app).options(
        "/api/config/scorecard",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
