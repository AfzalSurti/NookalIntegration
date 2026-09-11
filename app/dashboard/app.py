"""FastAPI application factory for the clinic dashboard."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.dashboard.api import (
    approvals,
    appointments,
    audit,
    auth,
    cases,
    communication,
    documents,
    finance,
    marketing,
    patients,
    referrers,
    system,
    templates,
)
from app.dashboard.container import DashboardContainer
from app.dashboard.views import pages


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response


def create_app(container: DashboardContainer | None = None) -> FastAPI:
    """
    Build the dashboard app.

    Production/dev entrypoints must inject a DashboardContainer.
    Tests inject an offline container with mocks.
    """
    app = FastAPI(
        title="Back to Ease Dashboard",
        version="0.1.0",
        docs_url=None,  # disable public OpenAPI in clinic UI by default
        redoc_url=None,
    )
    if container is not None:
        app.state.container = container

    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ARG001
        if isinstance(exc, HTTPException):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        # Never leak stack traces to clients.
        return JSONResponse(status_code=500, content={"detail": "internal_error"})

    app.include_router(auth.router)
    app.include_router(patients.router)
    app.include_router(approvals.router)
    app.include_router(documents.router)
    app.include_router(referrers.router)
    app.include_router(appointments.router)
    app.include_router(audit.router)
    app.include_router(marketing.router)
    app.include_router(system.router)
    app.include_router(finance.router)
    app.include_router(cases.router)
    app.include_router(communication.router)
    app.include_router(templates.router)
    app.include_router(pages.router)

    jinja_templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates = jinja_templates

    @app.get("/healthz", response_class=JSONResponse, include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
