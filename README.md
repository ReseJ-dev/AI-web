# AI Web Research & Data Extraction Agent

A portfolio project for a compliant web research and structured data extraction
agent. It includes source policy and robots preflight, transient search,
company-page selection, clean HTML extraction, and evidence-based structured
company extraction. Crawling orchestration and Google Sheets integration are
not implemented yet.

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
text, titles, headings, and relevant platform or industry terms. It recognizes
common English and Dutch about, services, solutions, expertise, and contact
routes.

`HtmlContentExtractor` processes supplied HTML locally; it performs no network
requests. It preserves canonical, meta, Open Graph, and Organization JSON-LD
metadata while removing executable, hidden, cookie, navigation, menu, and
footer content. Clean visible text is deduplicated and limited by
`HTML_CONTENT_MAX_CHARS` before it can be passed to an LLM provider.

## Structured company extraction

Structured extraction runs deterministic metadata and page-signal extraction
before an optional LLM pass. The composite strategy preserves deterministic
facts and uses model output only to fill unsupported fields. Every non-null
field records whether it is explicit or inferred and cites its evidence URLs;
required fields without evidence reject the extraction.

LLM integrations receive only clean page text, source URLs, requested field
names, and extraction instructions. Responses must match a strict Pydantic
schema. Core company facts cannot be inferred, foreign evidence URLs and long
copied summaries are rejected, and employee personal-data fields are never sent
to the provider. Select a registered provider and model with `LLM_PROVIDER` and
`LLM_MODEL`. This commit includes an in-memory fake provider; vendor adapters can
be registered without changing extraction services.

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
