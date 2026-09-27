from httpx import ASGITransport, AsyncClient

from codebase_intelligence.api.app import create_app


async def test_healthz() -> None:
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://t") as c:
        response = await c.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
