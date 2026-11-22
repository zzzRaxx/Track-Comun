import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from pathlib import Path

from app import config
from app.channels import webhook
from app.channels.evolution import EvolutionClient
from app.core import analysis
from app.db import init_db, make_engine, make_session_factory
from app.web import routes

SESSION_DAYS = 30


def create_app(
    database_url: str | None = None,
    validate: bool = True,
    evolution: EvolutionClient | None = None,
    run_analysis: bool = True,
) -> FastAPI:
    """`evolution` permite inyectar un cliente falso en las pruebas."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if validate:
            config.validate()
        engine = make_engine(database_url or config.DATABASE_URL)
        init_db(engine)
        logging.getLogger(__name__).info("Base de datos: %s", engine.dialect.name)
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        owns_client = evolution is None and bool(config.EVOLUTION_URL)
        app.state.evolution = evolution or (
            EvolutionClient(config.EVOLUTION_URL, config.EVOLUTION_API_KEY) if owns_client else None
        )
        task = None
        if run_analysis and config.GEMINI_API_KEY:
            task = asyncio.create_task(analysis.run_loop(app.state.session_factory))
        elif run_analysis:
            logging.getLogger(__name__).warning("Sin GEMINI_API_KEY: las conversaciones no se analizarán")
        yield
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if owns_client:
            await app.state.evolution.aclose()
        engine.dispose()

    app = FastAPI(title="Agente de seguimiento de chats", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.SESSION_SECRET or "solo-para-pruebas-sin-validar",
        max_age=SESSION_DAYS * 24 * 3600,
        same_site="lax",
        https_only=config.PUBLIC_URL.startswith("https://"),
    )

    @app.exception_handler(routes.NotLoggedIn)
    async def _not_logged_in(request: Request, exc: routes.NotLoggedIn):
        return RedirectResponse("/login", status_code=303)

    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")
    app.include_router(webhook.router)
    app.include_router(routes.router)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    return app


def app_factory() -> FastAPI:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # evita registrar claves en las URLs
    return create_app()


if __name__ == "__main__":
    import os

    import uvicorn

    uvicorn.run(app_factory(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
