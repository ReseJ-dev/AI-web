"""Typed synchronous API client used by the Streamlit dashboard."""

from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ValidationError

from app.api.schemas import (
    ApiErrorResponse,
    CreateResearchRunRequest,
    GoogleSheetsExportResponse,
    ResearchResultsResponse,
    ResearchRunResponse,
    SkippedSourcesResponse,
)


class DashboardApiError(RuntimeError):
    """Safe API failure suitable for direct display in Streamlit."""

    def __init__(self, message: str, *, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


class ResearchApiClient:
    """Call only the credential-free public FastAPI research endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 20.0,
        access_token: str | None = None,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("UI API base URL must use HTTP or HTTPS")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._owns_client = client is None
        self._access_token = access_token.strip() if access_token else None
        self._client = client or httpx.Client(
            base_url=normalized,
            timeout=httpx.Timeout(timeout_seconds),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    def start_research(
        self,
        request: CreateResearchRunRequest,
    ) -> ResearchRunResponse:
        """Submit a validated research request."""
        response = self._request(
            "POST",
            "/api/research-runs",
            json=request.model_dump(mode="json"),
        )
        return self._validate(ResearchRunResponse, response)

    def get_run(self, run_id: UUID | str) -> ResearchRunResponse:
        """Poll one research run."""
        response = self._request("GET", f"/api/research-runs/{run_id}")
        return self._validate(ResearchRunResponse, response)

    def get_results(self, run_id: UUID | str) -> ResearchResultsResponse:
        """Fetch the complete bounded result set for a dashboard run."""
        response = self._request(
            "GET",
            f"/api/research-runs/{run_id}/results",
            params={"offset": 0, "limit": 100},
        )
        return self._validate(ResearchResultsResponse, response)

    def get_skipped_sources(self, run_id: UUID | str) -> SkippedSourcesResponse:
        """Fetch every page of the skipped-source report."""
        items = []
        offset = 0
        total = 0
        while True:
            response = self._request(
                "GET",
                f"/api/research-runs/{run_id}/skipped-sources",
                params={"offset": offset, "limit": 100},
            )
            page = self._validate(SkippedSourcesResponse, response)
            items.extend(page.items)
            total = page.total
            offset += len(page.items)
            if offset >= total or not page.items:
                return SkippedSourcesResponse(
                    run_id=page.run_id,
                    items=items,
                    total=total,
                    offset=0,
                    limit=100,
                )

    def export_google_sheets(
        self,
        run_id: UUID | str,
        *,
        spreadsheet_id: str | None,
    ) -> GoogleSheetsExportResponse:
        """Export final or partial results to an existing or configured sheet."""
        response = self._request(
            "POST",
            f"/api/research-runs/{run_id}/export/google-sheets",
            json={"spreadsheet_id": spreadsheet_id or None},
        )
        return self._validate(GoogleSheetsExportResponse, response)

    def close(self) -> None:
        """Close an internally created HTTP client."""
        if self._owns_client:
            self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: object | None = None,
        params: dict[str, int] | None = None,
    ) -> httpx.Response:
        headers = {"X-Request-ID": f"dashboard-{uuid4().hex}"}
        if self._access_token is not None:
            headers["Authorization"] = f"Bearer {self._access_token}"
        try:
            response = self._client.request(
                method,
                path,
                json=json,
                params=params,
                headers=headers,
            )
        except httpx.RequestError as error:
            raise DashboardApiError(
                "The research API is unavailable. Start the FastAPI service and "
                "check UI_API_BASE_URL."
            ) from error
        if response.is_error:
            raise self._api_error(response)
        return response

    @staticmethod
    def _api_error(response: httpx.Response) -> DashboardApiError:
        try:
            payload = ApiErrorResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            return DashboardApiError(
                f"The research API returned HTTP {response.status_code}."
            )
        return DashboardApiError(
            payload.error.message,
            request_id=payload.error.request_id,
        )

    @staticmethod
    def _validate[ResponseModel: BaseModel](
        model: type[ResponseModel],
        response: httpx.Response,
    ) -> ResponseModel:
        try:
            result = model.model_validate(response.json())
        except (ValueError, ValidationError) as error:
            raise DashboardApiError(
                "The research API returned an unexpected response."
            ) from error
        return result
