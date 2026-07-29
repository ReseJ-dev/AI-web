# AI Web Research & Data Extraction Agent

A portfolio project for a compliant web research and structured data extraction
agent. It includes source policy and robots preflight, transient search,
company-page selection, clean HTML extraction, and evidence-based structured
company extraction, deduplication, scoring, and resilient research
orchestration. Concrete crawler, enrichment, and exporter adapters are injected
at deployment time, including an official Google Sheets API exporter.

## Requirements

- Python 3.12
- GNU Make (optional)

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Copy the environment template for local configuration:

```bash
cp .env.example .env
```

## Run the applications

Start the API:

```bash
make run-api
```

The health endpoint is available at `http://localhost:8000/health`.
The versioned health endpoint is available at
`http://localhost:8000/api/health`, and interactive OpenAPI documentation is
available at `http://localhost:8000/docs`.

Start the Streamlit UI in another terminal:

```bash
make run-ui
```

Apply database migrations:

```bash
make migrate
```

## Research API

Research runs are asynchronous. `POST /api/research-runs` validates the topic,
requested fields, result count, and locale, returns `202 Accepted`, and provides
a UUID for progress polling through `GET /api/research-runs/{run_id}`.
Verified results and skipped-source audit records are available from the
run-specific `/results` and `/skipped-sources` endpoints. Results support
`offset` and `limit` pagination and remain available when a run finishes with
partial success.

```bash
curl -X POST http://localhost:8000/api/research-runs \
  -H 'Content-Type: application/json' \
  -H 'X-Request-ID: portfolio-demo-1' \
  -d '{
    "topic": "Shopify agencies in the Netherlands",
    "requested_fields": [
      {"name": "country"},
      {"name": "services"},
      {"name": "contact page"}
    ],
    "result_count": 30,
    "location": "Netherlands",
    "country": "NL",
    "language": "en",
    "country_tld": "nl"
  }'
```

Every response includes `X-Request-ID`; a safe caller-provided value is
preserved. Validation, missing-run, lifecycle-conflict, provider, and internal
failures use a consistent `error` envelope. Error responses and
`GET /api/config/providers` expose provider readiness but never credential
values.

After a run reaches a terminal state,
`POST /api/research-runs/{run_id}/export/google-sheets` exports its final or
partial records using the configured Google Sheets service account.

## Source policies

Source decisions are configured in `config/approved_domains.yaml`,
`config/blocked_domains.yaml`, and `config/source_policies.yaml`. Exact rules
match only one host, while `include_subdomains` rules match both the configured
host and its descendants. Candidate and unknown domains require manual review.

Set `SOURCE_POLICY_CONFIG_DIR` to load policy files from another directory.
Configuration changes can be applied at runtime with
`SourcePolicyService.reload()`.

## Search provider and result retention

Candidate discovery uses the replaceable asynchronous `SearchProvider`
contract. Set `BRAVE_SEARCH_API_KEY` to use `BraveSearchProvider`; tests and
offline development can use `FakeSearchProvider`.

Brave search candidates are transient process-memory objects. Raw API responses
and search snippets are never persisted, and the candidate model intentionally
has no snippet field. `SEARCH_RESULT_RETENTION_ALLOWED` defaults to `false`.

Persistent retention of Brave Search results requires a subscription or
agreement that explicitly grants storage rights. Setting
`SEARCH_RESULT_RETENTION_ALLOWED=true` does not itself grant those rights and
does not enable a persistence implementation. Confirm applicable rights under
the [Brave Search API terms](https://api-dashboard.search.brave.com/documentation/resources/terms-of-service)
and your plan before adding any storage path.

## Compliance preflight

`CompliancePreflightService` applies source-domain policy first, then checks
`/robots.txt` before fetching any permitted content page. Robots checks use
`PROJECT_USER_AGENT`, cache responses for `ROBOTS_CACHE_TTL_SECONDS` (up to 24
hours), and reject unreachable robots files when `ROBOTS_STRICT_MODE=true`.
Rules follow RFC 9309 wildcard, terminal-anchor, and most-specific-match
semantics.

Public terms links are identified and scanned only for automated-access risk
language. Terms scanning is an advisory compliance signal, not legal advice;
explicit or ambiguous language requires manual review. Terms signals never
override a blocked domain or rejected target-path robots decision.

## Page selection and content extraction

`PageSelectionService` ranks same-domain company pages using URL paths, anchor
text, titles, headings, navigation position, and relevant platform or industry
terms. Its local `discover()` method reads anchors from supplied, already
approved HTML without making requests or inspecting form actions. It recognizes
common English and Dutch about, services, solutions, expertise, and contact
routes, strictly filters other domains, and preserves the priority order from
homepage through topic-specific service pages.

`HtmlContentExtractor` processes supplied HTML locally; it performs no network
requests. BeautifulSoup preserves canonical, meta, Open Graph, and Organization
JSON-LD metadata while removing executable, hidden, cookie, navigation, menu,
and footer content. Semantic DOM blocks are deduplicated; body-only layouts use
trafilatura as a precision-oriented main-content fallback.

Every retained text block records its source URL and semantic kind. English and
Dutch service-related sections group their own source-attributed blocks, while
contact links remain structured candidates. Clean visible text and service
content are limited by `HTML_CONTENT_MAX_CHARS` before any text can be passed to
an LLM provider.

## Structured company extraction

Structured extraction runs deterministic metadata and page-signal extraction
before an optional LLM pass. The composite strategy preserves deterministic
facts and uses model output only to fill unsupported fields. Every non-null
field records whether it is explicit or inferred and cites its evidence URLs;
required fields without evidence reject the extraction.

`DeterministicCompanyExtractor` resolves company name, canonical website,
possible country, services, and a public business contact-page URL from
Organization JSON-LD, canonical metadata, titles, meta and Open Graph values,
headings, and service sections. Each deterministic value carries a compact
sanitized evidence fragment, extraction method, confidence, and source URL.
Equally authoritative conflicts are rejected rather than selected by page
order. Employee fields, person-like page identities, email addresses, phone
numbers, and personal profile contact links are excluded.

LLM integrations receive only clean page text, source URLs, requested field
names, and extraction instructions. Responses must match a strict Pydantic
schema. Core company facts cannot be inferred, foreign evidence URLs and long
copied summaries are rejected, and employee personal-data fields are never sent
to the provider. `LLM_MAX_INPUT_CHARS` applies an aggregate input limit across
all pages, and malformed model responses are retried according to
`LLM_RESPONSE_MAX_RETRIES`.

Select a provider and model with `LLM_PROVIDER` and `LLM_MODEL`. The built-in
`http` provider sends a vendor-neutral structured request to `LLM_API_URL`,
optionally authenticated by `LLM_API_KEY`, and retries transient failures.
`FakeLLMProvider` provides deterministic response sequences for tests. Other
vendor adapters can be registered without changing extraction services.

## Company deduplication

`CompanyDeduplicationService` resolves company pairs in a fixed, explainable
order: registrable website domain, redirect-derived canonical domain, exact
normalized legal name, high-confidence fuzzy name, then shared OpenCorporates
or Wikidata identifiers. Exact or fuzzy name matches without corroborating
evidence always require manual review and never merge records automatically.

URL normalization removes scheme differences, `www`, trailing slashes,
fragments, and common tracking parameters while preserving meaningful query
parameters. Internationalized hosts use IDNA, and registrable domains use the
bundled Public Suffix List snapshot without network access. Company names are
case-, punctuation-, whitespace-, and common legal-suffix-insensitive.

Merge decisions select values by confidence, retain losing values as
alternatives, preserve every distinct evidence URL (including audit-relevant
fragments), retain both source record IDs, and include the complete resolution
and merge explanation. Official-identifier URLs must use the authoritative
Wikidata or OpenCorporates host. Conflicting IDs keep records separate, while
malformed or internally contradictory official IDs require manual review.

## Relevance scoring

`RelevanceScoringService` calculates a reproducible integer score from 0–100
using seven fixed components: topic (30), country (20), relevant services (15),
official website confidence (10), contact page (10), evidence quality (10), and
requested field completeness (5). Every component includes a human-readable
rationale, and every withheld integer point is represented as a structured
evidence penalty; the serialized component key is `country_match`.

For research such as “Shopify agencies in the Netherlands,” full topic and
service points require explicit cited Shopify service evidence. Full location
points require explicit Netherlands evidence; an `.nl` domain is never treated
as proof of location. Contradictory, inferred, low-confidence, or uncited facts
reduce evidence quality. Unsupported criteria score zero, and model-proposed
relevance numbers are ignored—the final numerical score is always computed by
deterministic application code.

## Research orchestration

`ResearchOrchestrator` coordinates request validation, query planning,
transient search, source filtering, compliance preflight, crawling, page
selection, structured extraction, optional enrichment, deduplication,
deterministic scoring, persistence, and optional export. Search continues until
the requested independently verified record count is reached or
`RESEARCH_SEARCH_BUDGET` is exhausted.

The crawler, OpenCorporates/Wikidata/GeoNames enrichment providers, and result
exporters are replaceable asynchronous interfaces. Only configured, injected
providers run. Search candidates remain transient; final verified records and
skipped-source reports use repository interfaces.

`OpenCorporatesProvider` uses only the official JSON API for company
verification and enrichment; it never fetches or scrapes OpenCorporates HTML
pages. It keeps the independently verified website identity unchanged and adds
only unoccupied official-name, jurisdiction, company-number, status, registered
location, and registry-URL fields. Every added value cites the OpenCorporates
company URL and carries OpenCorporates and registry-publisher attribution.

Set `OPENCORPORATES_API_KEY` and explicitly set
`OPENCORPORATES_LICENSED_DATA_USE_ALLOWED=true` only when the configured API
account and licence permit this enrichment and retention. The provider is
disabled otherwise. Authentication uses the API token header; timeout,
exponential retry, and maximum `Retry-After` behavior are configurable.

`WikidataProvider` queries the official Wikidata SPARQL endpoint with the
configured project user agent. An entity is accepted only when its exact
label/alias result includes an official website matching the independently
verified company site. Wikidata identity, website corroboration, country,
headquarters, and industry values cite the entity page and carry Wikidata
attribution. Conflicting or ambiguous values produce warnings and never replace
stronger website evidence. Enable it with `WIKIDATA_ENABLED=true`.

`GeoNamesProvider` uses the official secure country-info and populated-place
JSON services. It normalizes evidence-backed country and city values, adds ISO
country codes and GeoNames identifiers, and reports geographic contradictions
without rewriting source fields. Stable country and place lookups are cached
for `GEONAMES_CACHE_TTL_SECONDS`. Set `GEONAMES_USERNAME` to an application
account; the documented `demo` account is not used. Every geographic addition
links to GeoNames and retains GeoNames attribution.

Progress callbacks receive ordered pipeline events, including a distinct
`enriching` stage for configured providers, followed by `completed`,
`completed_with_warnings`, or `failed`. Candidate-level compliance, crawl,
extraction, enrichment, persistence, and export failures are isolated and
reported as warnings, so successful companies can still be returned and saved.
`RESEARCH_SEARCH_PAGE_SIZE` and `RESEARCH_CRAWL_PAGE_LIMIT` bound provider and
crawler work.

## Google Sheets export

`GoogleSheetsExporter` uses service-account OAuth and the official Google
Sheets API. Configure exactly one of `GOOGLE_SERVICE_ACCOUNT_FILE` or
`GOOGLE_SERVICE_ACCOUNT_JSON`. Set `GOOGLE_SHEETS_SPREADSHEET_ID` to update an
existing spreadsheet that has been shared with the service-account email.
Spreadsheet creation is disabled by default; omit the ID and set
`GOOGLE_SHEETS_CREATE_ALLOWED=true` only when the service account is permitted
to create a new spreadsheet.

The exporter creates or reuses `Research Results`, `Skipped Sources`, and `Run
Metadata` tabs. Values and formatting are sent with batch API calls, and quota
or service failures use bounded exponential backoff. Headers are formatted,
the first row is frozen, filters and wrapped text are enabled, and columns use
content-appropriate widths.

Only final structured values, evidence URLs, audit decisions, and run metadata
are exported. Raw search responses, search snippets, evidence excerpts, and
long copied page fragments are excluded. Cell text is bounded and
formula-like prefixes are escaped. Register the exporter under
`google_sheets` in the orchestrator's exporter mapping and request that format
for a run.

## Safe website crawler

`AsyncWebsiteCrawler` uses a pooled asynchronous HTTP client and performs only
bounded GET requests. Every initial target, discovered page, and redirect hop
must receive an `approved` result from `CompliancePreflightService` before it
is fetched. Redirects are followed manually, and an unapproved cross-domain
redirect stops the crawl before contacting that host.

The crawler permits at most two concurrent responses per domain and spaces
request starts using `CRAWLER_REQUEST_DELAY_SECONDS`. It streams HTML, XHTML,
or plain text up to `CRAWLER_MAX_RESPONSE_BYTES`; other content types, binary
paths, oversized bodies, authentication barriers, rate limits, CAPTCHA pages,
Cloudflare challenges, and bot-protection pages stop processing. `Retry-After`
is observed up to `CRAWLER_MAX_RETRY_AFTER_SECONDS` without retrying a blocked
request.

Account, login, administration, checkout, cart, customer-data, internal-search,
and API routes are excluded before network access. The crawler does not submit
forms, execute JavaScript, authenticate, or attempt to bypass restrictions.
`RESEARCH_CRAWL_PAGE_LIMIT` defaults each company crawl to five pages, while
`CRAWLER_TIMEOUT_SECONDS` bounds individual requests.

## Quality checks

```bash
make check
```

Or run each check independently:

```bash
make lint
make typecheck
make test
```
