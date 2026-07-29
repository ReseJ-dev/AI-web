"""Deterministic in-memory LLM provider for extraction tests."""

from collections.abc import Sequence
from copy import deepcopy

from pydantic import JsonValue

from app.models.extraction import LLMExtractionRequest


class FakeLLMProvider:
    """Return configured JSON and record calls without external I/O."""

    def __init__(
        self,
        response: JsonValue | Exception,
        *,
        additional_responses: Sequence[JsonValue | Exception] = (),
    ) -> None:
        self._responses = [
            deepcopy(response),
            *(deepcopy(item) for item in additional_responses),
        ]
        self._response_index = 0
        self.calls: list[LLMExtractionRequest] = []
        self.response_schemas: list[dict[str, JsonValue]] = []

    async def generate_structured(
        self,
        request: LLMExtractionRequest,
        *,
        response_schema: dict[str, JsonValue],
    ) -> JsonValue:
        """Record the request and return an independent response copy."""
        self.calls.append(request)
        self.response_schemas.append(deepcopy(response_schema))
        response = self._responses[min(self._response_index, len(self._responses) - 1)]
        self._response_index += 1
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)
