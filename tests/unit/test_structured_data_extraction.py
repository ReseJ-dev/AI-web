"""Tests for evidence-based deterministic and LLM company extraction."""

from collections.abc import Iterable, Sequence

import pytest

from app.core.settings import reload_settings
from app.models import (
    CompanyExtraction,
    ExtractedPageContent,
    ExtractedTextBlock,
    ExtractionMethod,
    ExtractionStatus,
    FactBasis,
    NavigationLink,
    RequestedField,
    ServiceSection,
    TextBlockKind,
)
from app.providers import (
    FakeLLMProvider,
    LLMProvider,
    LLMProviderResponseError,
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
        "Strategy",
        "development",
        "optimization",
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
        assert field.evidence_fragment
        assert field.extraction_method is not None
        assert field.confidence is not None


@pytest.mark.anyio
async def test_deterministic_extractor_builds_evidence_based_summary() -> None:
    """The default offline extraction path can satisfy the dashboard summary field."""
    result = await DeterministicCompanyExtractor().extract(
        [_page()],
        [RequestedField(name="summary")],
    )

    summary = result.field("summary")
    assert result.status is ExtractionStatus.ACCEPTED
    assert summary is not None
    assert summary.value == (
        "Example Commerce B.V. provides Shopify development, Commerce strategy, "
        "Strategy and is based in NL."
    )
    assert summary.basis is FactBasis.INFERENCE
    assert [str(url) for url in summary.evidence_urls] == ["https://example.com/"]


@pytest.mark.anyio
async def test_cross_domain_identity_metadata_cannot_replace_fetched_site() -> None:
    """Canonical, Open Graph, and Organization URLs stay inside the fetched site."""
    page = _page().model_copy(
        update={
            "canonical_url": "https://attacker.example/",
            "open_graph": {
                "og:site_name": "Example Commerce",
                "og:url": "https://attacker.example/",
            },
            "organization_data": [
                {
                    "@type": "Organization",
                    "name": "Example Commerce B.V.",
                    "url": "https://attacker.example/",
                }
            ],
        }
    )

    result = await DeterministicCompanyExtractor().extract([page])

    website = result.field("website_url")
    assert result.status is ExtractionStatus.ACCEPTED
    assert website is not None
    assert website.value is None


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
async def test_deterministic_complete_data_has_compact_provenance() -> None:
    """Every complete deterministic fact carries method, fragment, URL, and score."""
    page = _page().model_copy(
        update={
            "service_sections": [
                ServiceSection(
                    source_url="https://example.com/",
                    heading="Our services",
                    text_blocks=[
                        ExtractedTextBlock(
                            source_url="https://example.com/",
                            text="Our services",
                            kind=TextBlockKind.HEADING,
                        ),
                        ExtractedTextBlock(
                            source_url="https://example.com/",
                            text="Shopify Plus implementation",
                            kind=TextBlockKind.HEADING,
                        ),
                    ],
                )
            ]
        }
    )

    result = await DeterministicCompanyExtractor().extract(
        [page],
        [
            RequestedField(name="country"),
            RequestedField(name="services"),
            RequestedField(name="description"),
        ],
    )

    assert result.status is ExtractionStatus.ACCEPTED
    expected = {
        "company_name",
        "website_url",
        "country",
        "services",
        "contact_page_url",
        "description",
    }
    for name in expected:
        field = result.field(name)
        assert field is not None
        assert field.value is not None
        assert [str(url) for url in field.evidence_urls] == ["https://example.com/"]
        assert field.evidence_fragment
        assert len(field.evidence_fragment) <= 500
        assert field.extraction_method is not None
        assert field.confidence is not None
        assert 0 <= field.confidence <= 1
    assert result.field("company_name").extraction_method is (  # type: ignore[union-attr]
        ExtractionMethod.JSON_LD_ORGANIZATION
    )
    assert result.field("website_url").extraction_method is (  # type: ignore[union-attr]
        ExtractionMethod.CANONICAL_URL
    )
    assert result.field("contact_page_url").value == (  # type: ignore[union-attr]
        "https://example.com/contact"
    )
    assert "Shopify Plus implementation" in result.field("services").value  # type: ignore[operator,union-attr]


@pytest.mark.anyio
async def test_deterministic_partial_data_returns_null_without_guessing() -> None:
    """A title can support identity while absent optional facts remain null."""
    page = _page(main_text="Northstar Commerce").model_copy(
        update={
            "title": "Northstar Commerce | Home",
            "open_graph": {},
            "organization_data": [],
            "headings": ["Welcome"],
            "meta_description": None,
            "contact_page_candidates": [],
        }
    )

    result = await DeterministicCompanyExtractor().extract([page])

    assert result.status is ExtractionStatus.ACCEPTED
    assert result.field("company_name").value == "Northstar Commerce"  # type: ignore[union-attr]
    assert result.field("company_name").extraction_method is (  # type: ignore[union-attr]
        ExtractionMethod.PAGE_TITLE
    )
    assert result.field("website_url").value == "https://example.com/"  # type: ignore[union-attr]
    for name in ("country", "services", "contact_page_url", "summary"):
        field = result.field(name)
        assert field is not None
        assert field.value is None
        assert field.evidence_urls == []
        assert field.evidence_fragment is None
        assert field.extraction_method is None
        assert field.confidence is None


@pytest.mark.anyio
async def test_deterministic_conflicting_authoritative_names_are_rejected() -> None:
    """Conflicting Organization names are surfaced rather than selected by order."""
    page = _page().model_copy(
        update={
            "organization_data": [
                {"@type": "Organization", "name": "Alpha Commerce"},
                {"@type": "Organization", "name": "Beta Commerce"},
            ],
        }
    )

    result = await DeterministicCompanyExtractor().extract([page])

    assert result.status is ExtractionStatus.REJECTED
    assert result.field("company_name").value is None  # type: ignore[union-attr]
    assert any(
        "Conflicting authoritative values" in reason
        for reason in result.rejection_reasons
    )


@pytest.mark.anyio
async def test_deterministic_malformed_data_is_ignored_without_personal_data() -> None:
    """Malformed metadata and personal contact details never become output facts."""
    page = _page(
        main_text=("Welcome\nJane Doe jane@example.com +31 6 12345678\nOur services")
    ).model_copy(
        update={
            "title": "Welcome",
            "open_graph": {
                "og:title": "jane@example.com",
                "og:url": "not a URL",
            },
            "organization_data": [
                {
                    "@type": "Organization",
                    "name": ["Malformed Company"],
                    "url": "javascript:alert(1)",
                    "address": {"addressCountry": {"name": "NL"}},
                    "knowsAbout": [
                        "jane@example.com",
                        "+31 6 12345678",
                    ],
                    "email": "jane@example.com",
                    "telephone": "+31 6 12345678",
                }
            ],
            "headings": ["Jane Doe", "Welcome", "Services"],
            "meta_description": "Call Jane on +31 6 12345678",
            "contact_page_candidates": [
                NavigationLink(
                    url="https://example.com/team/jane-contact",
                    text="Contact Jane",
                )
            ],
        }
    )

    result = await DeterministicCompanyExtractor().extract(
        [page],
        [
            RequestedField(name="email"),
            RequestedField(name="telephone"),
        ],
    )

    assert result.status is ExtractionStatus.REJECTED
    assert result.field("company_name").value is None  # type: ignore[union-attr]
    assert result.field("services").value is None  # type: ignore[union-attr]
    assert result.field("contact_page_url").value is None  # type: ignore[union-attr]
    assert result.field("email").value is None  # type: ignore[union-attr]
    assert result.field("telephone").value is None  # type: ignore[union-attr]
    serialized = result.model_dump_json()
    assert "jane@example.com" not in serialized
    assert "+31 6 12345678" not in serialized
    assert any("personal data" in reason for reason in result.rejection_reasons)


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
async def test_llm_retries_invalid_json_then_accepts_strict_response() -> None:
    """Malformed model output gets one bounded schema-repair attempt."""
    delays: list[float] = []
    provider = FakeLLMProvider(
        "not-an-object",
        additional_responses=[
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
        ],
    )

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    result = await LLMCompanyExtractor(
        provider,
        max_response_retries=1,
        retry_backoff_seconds=0.25,
        sleep=record_sleep,
    ).extract([_page()])

    assert result.status is ExtractionStatus.ACCEPTED
    assert result.field("company_name").value == "Example Commerce"  # type: ignore[union-attr]
    assert len(provider.calls) == 2
    assert delays == [0.25]
    assert "previous response was malformed" in provider.calls[1].instructions


@pytest.mark.anyio
async def test_llm_retries_provider_invalid_json_error() -> None:
    """Provider JSON decoding failures use the same repair retry budget."""
    provider = FakeLLMProvider(
        LLMProviderResponseError("invalid JSON"),
        additional_responses=[{"fields": []}],
    )

    result = await LLMCompanyExtractor(
        provider,
        max_response_retries=1,
        retry_backoff_seconds=0,
    ).extract([_page()])

    assert len(provider.calls) == 2
    assert result.status is ExtractionStatus.REJECTED
    assert "company_name" in result.rejection_reasons[0]


@pytest.mark.anyio
async def test_llm_input_has_an_aggregate_clean_text_limit() -> None:
    """Multiple clean pages cannot exceed the configured aggregate budget."""
    provider = FakeLLMProvider({"fields": []})
    first = _page(main_text="A" * 800)
    second_text = "B" * 800
    second = _page(
        source_url="https://example.com/about",
        main_text=second_text,
    ).model_copy(
        update={
            "canonical_url": "https://example.com/about",
            "extracted_text_length": len(second_text),
        }
    )

    await LLMCompanyExtractor(
        provider,
        max_input_chars=1_000,
        max_response_retries=0,
    ).extract([first, second])

    sent = provider.calls[0].pages
    assert sum(len(page.cleaned_text) for page in sent) <= 1_000
    assert sent[0].cleaned_text == "A" * 800
    assert sent[1].cleaned_text == "B" * 200
    serialized = provider.calls[0].model_dump_json()
    assert "organization_data" not in serialized
    assert "meta_description" not in serialized


@pytest.mark.anyio
async def test_llm_rejects_hallucinated_explicit_fields() -> None:
    """A valid evidence URL cannot support a value absent from clean text."""
    provider = FakeLLMProvider(
        {
            "fields": [
                {
                    "name": "company_name",
                    "value": "Hallucinated Labs",
                    "evidence_urls": ["https://example.com/"],
                    "basis": "explicit",
                },
                {
                    "name": "country",
                    "value": "Belgium",
                    "evidence_urls": ["https://example.com/"],
                    "basis": "explicit",
                },
                {
                    "name": "services",
                    "value": ["Machine learning consulting"],
                    "evidence_urls": ["https://example.com/"],
                    "basis": "explicit",
                },
            ]
        }
    )

    result = await LLMCompanyExtractor(
        provider,
        max_response_retries=0,
    ).extract(
        [_page()],
        [RequestedField(name="country"), RequestedField(name="services")],
    )

    assert result.status is ExtractionStatus.REJECTED
    assert result.field("company_name").value is None  # type: ignore[union-attr]
    assert result.field("country").value is None  # type: ignore[union-attr]
    assert result.field("services").value is None  # type: ignore[union-attr]
    assert (
        sum(
            "absent from its cited clean text" in reason
            for reason in result.rejection_reasons
        )
        == 3
    )


@pytest.mark.anyio
async def test_llm_rejects_unsupported_summary_claims() -> None:
    """An original-sounding summary still fails when its facts are unsupported."""
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
                    "value": (
                        "Example Commerce won international awards and "
                        "employs 900 engineers."
                    ),
                    "evidence_urls": ["https://example.com/"],
                    "basis": "inference",
                },
            ]
        }
    )

    result = await LLMCompanyExtractor(
        provider,
        max_response_retries=0,
    ).extract([_page()])

    assert result.field("summary").value is None  # type: ignore[union-attr]
    assert any("numeric fact absent" in reason for reason in result.rejection_reasons)


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
                    "value": (
                        "Example Commerce provides strategy and development services."
                    ),
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
    assert result.field("summary").value == (  # type: ignore[union-attr]
        "Example Commerce provides strategy and development services."
    )


@pytest.mark.anyio
async def test_composite_keeps_deterministic_value_on_llm_conflict() -> None:
    """Conflicting model data cannot replace a deterministic supported fact."""
    page = _page(
        main_text=(
            "Example Commerce\nWrong Commerce\nOur services\n"
            "Strategy, development and optimization"
        )
    )
    provider = FakeLLMProvider(
        {
            "fields": [
                {
                    "name": "company_name",
                    "value": "Wrong Commerce",
                    "evidence_urls": ["https://example.com/"],
                    "basis": "explicit",
                }
            ]
        }
    )
    composite = CompositeCompanyExtractor(
        DeterministicCompanyExtractor(),
        LLMCompanyExtractor(provider, max_response_retries=0),
    )

    result = await composite.extract([page])

    assert result.status is ExtractionStatus.ACCEPTED
    assert result.field("company_name").value == "Example Commerce B.V."  # type: ignore[union-attr]


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
