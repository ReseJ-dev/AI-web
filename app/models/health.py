"""Health endpoint models."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """API health status."""

    status: Literal["ok"]
