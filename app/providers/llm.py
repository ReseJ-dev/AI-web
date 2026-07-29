"""Vendor-neutral structured LLM provider abstraction and configuration."""

from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from app.core.settings import get_settings
from app.models.extraction import LLMExtractionRequest


@runtime_checkable
class LLMProvider(Protocol):
    """Generate JSON for a strict company-extraction request."""

    async def generate_structured(
        self,
        request: LLMExtractionRequest,
        *,
        response_schema: dict[str, JsonValue],
    ) -> JsonValue:
        """Return provider JSON without coercing it into domain objects."""
        ...


class LLMProviderConfigurationError(RuntimeError):
    """Raised when the selected LLM provider has no configured implementation."""


class LLMProviderError(RuntimeError):
    """Base error for structured-generation provider failures."""


class LLMProviderResponseError(LLMProviderError):
    """A provider returned malformed JSON or an unusable response shape."""


def resolve_llm_provider(
    factories: Mapping[str, Callable[[], LLMProvider]] | None = None,
) -> LLMProvider:
    """Instantiate the provider selected by ``LLM_PROVIDER``."""
    from app.providers.http_llm import ConfigurableHttpLLMProvider

    provider_name = get_settings().llm_provider.casefold().strip()
    available: dict[str, Callable[[], LLMProvider]] = {
        "http": ConfigurableHttpLLMProvider,
    }
    available.update(factories or {})
    factory = available.get(provider_name)
    if factory is None:
        raise LLMProviderConfigurationError(
            f"LLM provider {provider_name!r} is not configured"
        )
    return factory()
