from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Codebase Intelligence")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        # Liveness only; readiness (DB reachability) is added with the first DB-backed endpoint.
        return {"status": "ok"}

    return app
