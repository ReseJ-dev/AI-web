"""Tests for evidence-based deterministic and LLM company extraction."""

from collections.abc import Iterable, Sequence

import pytest

from app.core.settings import reload_settings
from app.models import (
    CompanyExtraction,
    ExtractedPageContent,
    ExtractionStatus,
    FactBasis,
    NavigationLink,
    RequestedField,
)
from app.providers import (
    FakeLLMProvider,
    LLMProvider,
    resolve_llm_provider,
)
from app.services import (
    CompositeCompanyExtractor,
    DeterministicCompanyExtractor,
    LLMCompanyExtractor,
    StructuredDataExtractor,
)


def _page(
    *,
    source_url: str = "https://example.com/",
    main_text: str = (
        "Example Commerce\nOur services\nStrategy, development and optimization"
    ),
) -> ExtractedPageContent:
    """Build a clean page carrying both visible and deterministic metadata."""
    return ExtractedPageContent(
        source_url=source_url,
        canonical_url="https://example.com/",
        title="Example Commerce — Shopify Experts",
        meta_description="Commerce strategy and development in Amsterdam.",
        open_graph={"og:site_name": "Example Commerce"},
        organization_data=[
            {
                "@type": "Organization",
                "name": "Example Commerce B.V.",
                "knowsAbout": ["Shopify development", "Commerce strategy"],
                "address": {"addressCountry": "NL"},
            }
        ],
        main_text=main_text,
        headings=["Example Commerce", "Our services"],
        navigation_links=[],
        contact_page_candidates=[
            NavigationLink(
                url="https://directory.example/contact",
                text="Directory contact",
            ),
            NavigationLink(
                url="https://example.com/contact",
                text="Contact",
            ),
        ],
        source_html_length=4_000,
        extracted_text_length=len(main_text),
    )


@pytest.mark.anyio
async def test_deterministic_extractor_uses_structured_and_page_signals() -> None:
    """Structured metadata, canonical URL, services, and links carry evidence."""
    result = await DeterministicCompanyExtractor().extract(
        [_page()],
        [
            RequestedField(name="services"),
            RequestedField(name="country"),
            RequestedField(name="description"),
        ],
    )

    assert result.status is ExtractionStatus.ACCEPTED
    assert result.field("company_name").value == "Example Commerce B.V."  # type: ignore[union-attr]
    assert result.field("website_url").value == "https://example.com/"  # type: ignore[union-attr]
    assert result.field("services").value == [  # type: ignore[union-attr]
        "Shopify development",
        "Commerce strategy",
    ]
    assert result.field("country").value == "NL"  # type: ignore[union-attr]
    assert result.field("description").value == (  # type: ignore[union-attr]
        "Commerce strategy and development in Amsterdam."
    )
    assert result.field("contact_page_url").value == (  # type: ignore[union-attr]
        "https://example.com/contact"
    )
    for name in ("company_name", "website_url", "services", "country"):
        field = result.field(name)
        assert field is not None
        assert [str(url) for url in field.evidence_urls] == ["https://example.com/"]
        assert field.basis is FactBasis.EXPLICIT


@pytest.mark.anyio
async def test_deterministic_extractor_falls_back_to_title_and_service_section() -> (
    None
):
    """Visible headings and sections work when Organization JSON-LD is absent."""
    page = _page()
    page = page.model_copy(
        update={
            "organization_data": [],
            "open_graph": {},
        }
    )

    result = await DeterministicCompanyExtractor().extract(
        [page],
        [RequestedField(name="services")],
    )

    assert result.status is ExtractionStatus.ACCEPTED
    assert result.field("company_name").value == "Example Commerce"  # type: ignore[union-attr]
    assert result.field("services").value == [  # type: ignore[union-attr]
        "Strategy",
        "development",
        "optimization",
    ]


@pytest.mark.anyio
async def test_llm_gets_only_clean_text_urls_and_returns_strict_facts() -> None:
    """Provider input omits metadata while output preserves basis and evidence."""
    provider = FakeLLMProvider(
        {
            "fields": [
                {
                    "name": "company_name",
                    "value": "Example Commerce",
                    "evidence_urls": ["https://example.com/"],
                    "basis": "explicit",
                },
                {
                    "name": "summary",
                    "value": "Example Commerce provides commerce strategy services.",
                    "evidence_urls": ["https://example.com/"],
                    "basis": "inference",
                },
                {
                    "name": "country",
                    "value": None,
                    "evidence_urls": [],
                    "basis": None,
                },
            ]
        }
    )
    page = _page(main_text="Example Commerce\nCommerce strategy services.")

    result = await LLMCompanyExtractor(provider, model="test-model").extract(
        [page],
        [RequestedField(name="country")],
        required_fields=(),
    )

    assert isinstance(provider, LLMProvider)
    call = provider.calls[0]
    assert call.model == "test-model"
    assert call.pages[0].cleaned_text == page.main_text
    assert call.pages[0].model_dump() == {
        "source_url": page.source_url,
        "cleaned_text": page.main_text,
    }
    serialized_call = call.model_dump_json()
    assert "Commerce strategy and development in Amsterdam" not in serialized_call
    assert "organization_data" not in serialized_call
    assert provider.response_schemas[0]["type"] == "object"
    assert result.field("summary").basis is FactBasis.INFERENCE  # type: ignore[union-attr]
    assert result.status is ExtractionStatus.REJECTED
    assert "country" in result.rejection_reasons[0]


@pytest.mark.anyio
async def test_llm_rejects_missing_and_foreign_evidence() -> None:
    """A required fact cannot survive without evidence from a supplied page."""
    missing = FakeLLMProvider(
        {
            "fields": [
                {
                    "name": "company_name",
                    "value": "Unsupported Company",
                    "evidence_urls": [],
                    "basis": "explicit",
                }
            ]
        }
    )
    missing_result = await LLMCompanyExtractor(missing).extract([_page()])

    assert missing_result.status is ExtractionStatus.REJECTED
    assert "strict extraction schema" in missing_result.rejection_reasons[0]

    foreign = FakeLLMProvider(
        {
            "fields": [
                {
                    "name": "company_name",
                    "value": "Unsupported Company",
                    "evidence_urls": ["https://invented.example/"],
                    "basis": "explicit",
                }
            ]
        }
    )
    foreign_result = await LLMCompanyExtractor(foreign).extract([_page()])

    assert foreign_result.status is ExtractionStatus.REJECTED
    assert any(
        "was not supplied" in reason for reason in foreign_result.rejection_reasons
    )
    assert foreign_result.field("company_name").value is None  # type: ignore[union-attr]


@pytest.mark.anyio
async def test_llm_rejects_invented_core_facts_and_long_copied_summary() -> None:
    """Core facts must be explicit, while summaries cannot copy long passages."""
    copied = "A supported but directly copied source sentence. " * 5
    provider = FakeLLMProvider(
        {
            "fields": [
                {
                    "name": "company_name",
                    "value": "Invented Company",
                    "evidence_urls": ["https://example.com/"],
                    "basis": "inference",
                },
                {
                    "name": "contact_page_url",
                    "value": "https://example.com/invented-contact",
                    "evidence_urls": ["https://example.com/"],
                    "basis": "explicit",
                },
                {
                    "name": "summary",
                    "value": copied,
                    "evidence_urls": ["https://example.com/"],
                    "basis": "inference",
                },
            ]
        }
    )

    result = await LLMCompanyExtractor(provider).extract(
        [_page(main_text=f"Example Commerce\n{copied}")],
    )

    assert result.status is ExtractionStatus.REJECTED
    assert result.field("company_name").value is None  # type: ignore[union-attr]
    assert result.field("contact_page_url").value is None  # type: ignore[union-attr]
    assert result.field("summary").value is None  # type: ignore[union-attr]
    assert any("may not be inferred" in reason for reason in result.rejection_reasons)
    assert any("was not supplied" in reason for reason in result.rejection_reasons)
    assert any("copies a long" in reason for reason in result.rejection_reasons)


@pytest.mark.anyio
async def test_employee_personal_fields_are_not_sent_or_returned() -> None:
    """Employee personal-data requests are blocked before provider invocation."""
    provider = FakeLLMProvider(
        {
            "fields": [
                {
                    "name": "company_name",
                    "value": "Example Commerce",
                    "evidence_urls": ["https://example.com/"],
                    "basis": "explicit",
                }
            ]
        }
    )

    result = await LLMCompanyExtractor(provider).extract(
        [_page()],
        [RequestedField(name="employee email")],
    )

    assert "employee_email" not in provider.calls[0].requested_fields
    assert result.field("employee_email").value is None  # type: ignore[union-attr]
    assert result.status is ExtractionStatus.REJECTED
    assert any("personal data" in reason for reason in result.rejection_reasons)


class _RecordingExtractor:
    """Minimal strategy spy used to prove composite invocation ordering."""

    def __init__(
        self,
        label: str,
        calls: list[str],
        result: CompanyExtraction,
    ) -> None:
        self._label = label
        self._calls = calls
        self._result = result

    async def extract(
        self,
        pages: Sequence[ExtractedPageContent],
        requested_fields: Sequence[RequestedField] = (),
        *,
        required_fields: Iterable[str] = (),
    ) -> CompanyExtraction:
        """Record invocation and return the configured result."""
        self._calls.append(self._label)
        return self._result


@pytest.mark.anyio
async def test_composite_runs_deterministic_first_and_preserves_its_facts() -> None:
    """LLM values fill nulls but never replace deterministic supported facts."""
    page = _page()
    deterministic_result = await DeterministicCompanyExtractor().extract([page])
    llm_provider = FakeLLMProvider(
        {
            "fields": [
                {
                    "name": "company_name",
                    "value": "Wrong LLM Name",
                    "evidence_urls": ["https://example.com/"],
                    "basis": "inference",
                },
                {
                    "name": "summary",
                    "value": "An original supported summary.",
                    "evidence_urls": ["https://example.com/"],
                    "basis": "inference",
                },
            ]
        }
    )
    llm_result = await LLMCompanyExtractor(llm_provider).extract([page])
    calls: list[str] = []
    deterministic = _RecordingExtractor("deterministic", calls, deterministic_result)
    llm = _RecordingExtractor("llm", calls, llm_result)

    assert isinstance(deterministic, StructuredDataExtractor)
    result = await CompositeCompanyExtractor(deterministic, llm).extract([page])

    assert calls == ["deterministic", "llm"]
    assert result.status is ExtractionStatus.ACCEPTED
    assert result.field("company_name").value == "Example Commerce B.V."  # type: ignore[union-attr]
    assert result.field("summary").value == "An original supported summary."  # type: ignore[union-attr]


def test_environment_selects_llm_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider registry resolves the implementation selected by settings."""
    configured = FakeLLMProvider({"fields": []})
    with monkeypatch.context() as environment:
        environment.setenv("LLM_PROVIDER", "fake")
        reload_settings()

        provider = resolve_llm_provider({"fake": lambda: configured})

        assert provider is configured
    reload_settings()
