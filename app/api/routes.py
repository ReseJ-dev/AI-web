"""Thin FastAPI route handlers for research application services."""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.api.schemas import (
    ApiErrorResponse,
    CreateResearchRunRequest,
    GoogleSheetsExportResponse,
    ProvidersResponse,
    ResearchResultsResponse,
    ResearchRunResponse,
    SkippedSourcesResponse,
)
from app.models.health import HealthResponse
from app.services.research_api import ResearchRunApplicationService

router = APIRouter(prefix="/api")


def get_research_service(request: Request) -> ResearchRunApplicationService:
    """Resolve the application-scoped research service."""
    return cast(ResearchRunApplicationService, request.app.state.research_service)


ResearchService = Annotated[
    ResearchRunApplicationService,
    Depends(get_research_service),
]
PageOffset = Annotated[int, Query(ge=0, description="Zero-based result offset.")]
PageLimit = Annotated[
    int,
    Query(ge=1, le=100, description="Maximum records returned."),
]
ERROR_RESPONSES = {
    404: {"model": ApiErrorResponse, "description": "Research run not found."},
    422: {"model": ApiErrorResponse, "description": "Request validation failed."},
    503: {"model": ApiErrorResponse, "description": "Provider unavailable."},
}


@router.post(
    "/research-runs",
    response_model=ResearchRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={422: ERROR_RESPONSES[422], 503: ERROR_RESPONSES[503]},
    tags=["research"],
)
async def create_research_run(
    payload: CreateResearchRunRequest,
    service: ResearchService,
) -> ResearchRunResponse:
    """Start a run asynchronously and return its pollable identifier."""
    return await service.submit(payload)


@router.get(
    "/research-runs/{run_id}",
    response_model=ResearchRunResponse,
    responses={404: ERROR_RESPONSES[404], 422: ERROR_RESPONSES[422]},
    tags=["research"],
)
async def get_research_run(
    run_id: UUID,
    service: ResearchService,
) -> ResearchRunResponse:
    """Poll lifecycle and latest progress for a research run."""
    return service.get_run(run_id)


@router.get(
    "/research-runs/{run_id}/results",
    response_model=ResearchResultsResponse,
    responses={404: ERROR_RESPONSES[404], 422: ERROR_RESPONSES[422]},
    tags=["research"],
)
async def get_research_results(
    run_id: UUID,
    service: ResearchService,
    offset: PageOffset = 0,
    limit: PageLimit = 25,
) -> ResearchResultsResponse:
    """Return paginated final or partial independently verified records."""
    return service.get_results(run_id, offset=offset, limit=limit)


@router.get(
    "/research-runs/{run_id}/skipped-sources",
    response_model=SkippedSourcesResponse,
    responses={404: ERROR_RESPONSES[404], 422: ERROR_RESPONSES[422]},
    tags=["research"],
)
async def get_skipped_sources(
    run_id: UUID,
    service: ResearchService,
    offset: PageOffset = 0,
    limit: PageLimit = 25,
) -> SkippedSourcesResponse:
    """Return paginated source exclusion audit details."""
    return service.get_skipped_sources(run_id, offset=offset, limit=limit)


@router.post(
    "/research-runs/{run_id}/export/google-sheets",
    response_model=GoogleSheetsExportResponse,
    responses={
        404: ERROR_RESPONSES[404],
        409: {
            "model": ApiErrorResponse,
            "description": "The run has not reached a terminal state.",
        },
        422: ERROR_RESPONSES[422],
        503: ERROR_RESPONSES[503],
    },
    tags=["exports"],
)
async def export_google_sheets(
    run_id: UUID,
    service: ResearchService,
) -> GoogleSheetsExportResponse:
    """Export a terminal run using the configured Google Sheets exporter."""
    return await service.export_google_sheets(run_id)


@router.get(
    "/config/providers",
    response_model=ProvidersResponse,
    tags=["configuration"],
)
async def get_provider_config(service: ResearchService) -> ProvidersResponse:
    """Return provider readiness without any credential values."""
    return service.provider_config()


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def api_health() -> HealthResponse:
    """Report whether the API process is operational."""
    return HealthResponse(status="ok")
