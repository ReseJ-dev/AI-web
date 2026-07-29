"""FastAPI application entry point and dependency composition."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.dependencies import build_research_service
from app.api.errors import install_error_handling
from app.api.routes import router
from app.core.logging import configure_logging
from app.models.health import HealthResponse
from app.services.research_api import ResearchRunApplicationService

configure_logging()


def create_app(
    research_service: ResearchRunApplicationService | None = None,
) -> FastAPI:
    """Create an API application with an injectable application service."""
    service = research_service or build_research_service()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.research_service = service
        yield
        await service.shutdown()

    application = FastAPI(
        title="AI Web Research & Data Extraction Agent",
        version="0.1.0",
        description=(
            "Asynchronous, compliance-aware company research with pollable "
            "progress and evidence-based results."
        ),
        lifespan=lifespan,
    )
    application.state.research_service = service
    install_error_handling(application)
    application.include_router(router)

    @application.get(
        "/health",
        response_model=HealthResponse,
        include_in_schema=False,
    )
    async def legacy_health() -> HealthResponse:
        """Retain the original health endpoint for compatibility."""
        return HealthResponse(status="ok")

    return application


app = create_app()
