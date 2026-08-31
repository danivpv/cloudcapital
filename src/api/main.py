"""Application entrypoint and wiring.

The app factory is the composition root: it builds Settings -> Store ->
EconomicsClient -> LLMClient -> AskPipeline and hangs them off app.state,
so every route reads its dependencies from the request. The store connects
eagerly (cheap: a view over parquet); dice build lazily on first use.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings, get_settings
from .data.economics import InProcessEconomicsClient
from .data.store import Store
from .economics.app import router as econ_router, service as economics_service
from .intelligence.agent import DspyInvestigationAgent
from .intelligence.llm import LiteLLMClient
from .intelligence.pipeline import AskPipeline
from .routes import router

# ── Structured JSON logging ───────────────────────────────────────────────────


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record. CloudWatch Log Insights parses it."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # merge any extra fields passed as keyword args
        for key, value in record.__dict__.items():
            if key not in logging.LogRecord.__dict__ and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload)


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # Silence noisy uvicorn access log; we emit our own middleware log below.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False


_configure_logging()
_log = logging.getLogger("cloudcapital.api")


# ── App factory ───────────────────────────────────────────────────────────────


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _log.info("startup", extra={"data_dir": str(settings.data_dir)})
        store = Store(settings)
        store.connect()
        economics = InProcessEconomicsClient(store, economics_service)
        llm = LiteLLMClient(settings)
        pipeline = AskPipeline(store, llm, economics)
        agent = DspyInvestigationAgent(settings, store, llm)

        app.state.store = store
        app.state.economics = economics
        app.state.pipeline = pipeline
        app.state.agent = agent

        _log.info("startup_complete", extra={"periods": len(store.periods())})
        yield
        _log.info("shutdown")

    app = FastAPI(title="Cloud Capital — surface", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _access_log(request: Request, call_next) -> Response:  # type: ignore[return]
        t0 = time.monotonic()
        response: Response = await call_next(request)
        ms = round((time.monotonic() - t0) * 1000)
        _log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": ms,
            },
        )
        return response

    app.include_router(router)
    app.include_router(econ_router)
    return app


def run() -> None:
    import uvicorn

    uvicorn.run("src.api.main:app", host="127.0.0.1", port=8001, reload=True)


app = create_app()
