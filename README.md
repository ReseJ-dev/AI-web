# AI Web Research & Data Extraction Agent

A portfolio project for a compliant web research and structured data extraction
agent. It includes source policy and robots preflight, transient search,
company-page selection, clean HTML extraction, and evidence-based structured
company extraction, deduplication, scoring, and resilient research
orchestration. Concrete crawler, enrichment, and exporter adapters are injected
at deployment time; Google Sheets integration is not implemented yet.

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

Start the Streamlit UI in another terminal:

```bash
make run-ui
```

Apply database migrations:

```bash
make migrate
```

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
to the provider. Select a registered provider and model with `LLM_PROVIDER` and
`LLM_MODEL`. This commit includes an in-memory fake provider; vendor adapters can
be registered without changing extraction services.

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
alternatives, combine evidence URLs, retain both source record IDs, and include
the complete resolution and merge explanation. Conflicting official IDs keep
records separate; malformed official IDs require manual review.

## Relevance scoring

`RelevanceScoringService` calculates a reproducible 0–100 score from seven
fixed components: topic (30), location (20), relevant services (15), official
website confidence (10), contact page (10), evidence quality (10), and requested
field completeness (5). Every component includes a human-readable rationale,
and every withheld point is represented as a structured evidence penalty.

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

Progress callbacks receive ordered events from `planning` through `completed`,
`completed_with_warnings`, or `failed`. Candidate-level compliance, crawl,
extraction, enrichment, persistence, and export failures are isolated and
reported as warnings, so successful companies can still be returned and saved.
`RESEARCH_SEARCH_PAGE_SIZE` and `RESEARCH_CRAWL_PAGE_LIMIT` bound provider and
crawler work.

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
