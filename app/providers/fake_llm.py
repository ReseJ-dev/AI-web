"""Deterministic in-memory LLM provider for extraction tests."""

from copy import deepcopy

from pydantic import JsonValue

from app.models.extraction import LLMExtractionRequest


class FakeLLMProvider:
    """Return configured JSON and record calls without external I/O."""

    def __init__(self, response: dict[str, JsonValue]) -> None:
        self._response = deepcopy(response)
        self.calls: list[LLMExtractionRequest] = []
        self.response_schemas: list[dict[str, JsonValue]] = []

    async def generate_structured(
        self,
        request: LLMExtractionRequest,
        *,
        response_schema: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Record the request and return an independent response copy."""
        self.calls.append(request)
        self.response_schemas.append(deepcopy(response_schema))
        return deepcopy(self._response)
