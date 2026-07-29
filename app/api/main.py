"""FastAPI application entry point."""

from fastapi import FastAPI

from app.core.logging import configure_logging
from app.models.health import HealthResponse

configure_logging()

app = FastAPI(
    title="AI Web Research & Data Extraction Agent",
    version="0.1.0",
)


@app.get("/health", response_model=HealthResponse, tags=["system"])
async def health() -> HealthResponse:
    """Report whether the API is ready to accept requests."""
    return HealthResponse(status="ok")
