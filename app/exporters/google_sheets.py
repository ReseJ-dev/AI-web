"""Compliance-conscious research result export through the Google Sheets API."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, overload, runtime_checkable
from urllib.parse import urlsplit

import httpx
from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from pydantic import JsonValue, SecretStr, TypeAdapter, ValidationError

from app.core.settings import get_settings
from app.models.domain import CompanyRecord, ExtractedField, ResearchRun, SkippedSource
from app.models.orchestration import (
    ExportArtifact,
    ExportContext,
    RankedCompanyRecord,
)

GOOGLE_SHEETS_API_URL = "https://sheets.googleapis.com/v4/spreadsheets"
GOOGLE_SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
_SPREADSHEET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_FORMULA_PREFIXES = ("=", "+", "-", "@")
_SHEET_COLUMNS = {
    "Research Results": (
        "Company name",
        "Website",
        "Country",
        "Services",
        "Contact page",
        "Short summary",
        "Relevance score",
        "Relevance explanation",
        "Evidence URLs",
        "Compliance status",
        "Validation warnings",
        "Retrieved at",
    ),
    "Skipped Sources": (
        "Domain",
        "URL",
        "Decision",
        "Reason",
        "Robots status",
        "Terms status",
        "Checked at",
    ),
    "Run Metadata": ("Field", "Value"),
}
_COLUMN_WIDTHS = {
    "Research Results": (190, 220, 120, 220, 220, 320, 110, 320, 300, 140, 260, 180),
    "Skipped Sources": (170, 260, 140, 320, 130, 130, 180),
    "Run Metadata": (210, 480),
}
_QUOTA_REASONS = {
    "quotaExceeded",
    "rateLimitExceeded",
    "userRateLimitExceeded",
}


class GoogleSheetsExporterError(RuntimeError):
    """Base error raised by the Google Sheets exporter."""


class GoogleSheetsConfigurationError(GoogleSheetsExporterError):
    """Raised when exporter configuration is absent or invalid."""


class GoogleSheetsAuthenticationError(GoogleSheetsExporterError):
    """Raised when service-account authentication is rejected."""


class GoogleSheetsQuotaError(GoogleSheetsExporterError):
    """Raised when quota retries are exhausted."""


class GoogleSheetsUnavailableError(GoogleSheetsExporterError):
    """Raised when transient API retries are exhausted."""


class GoogleSheetsResponseError(GoogleSheetsExporterError):
    """Raised when Google returns a malformed or unexpected response."""


@runtime_checkable
class GoogleAccessTokenProvider(Protocol):
    """Provide OAuth access tokens without exposing credentials to the exporter."""

    async def get_token(self) -> str:
        """Return a non-empty access token."""
        ...


class ServiceAccountTokenProvider:
    """Refresh Google service-account credentials outside the event loop."""

    def __init__(
        self,
        *,
        credentials_file: Path | None = None,
        credentials_json: SecretStr | str | None = None,
    ) -> None:
        if credentials_file is not None and credentials_json is not None:
            raise GoogleSheetsConfigurationError(
                "Configure one Google service-account credential source, not both"
            )
        try:
            if credentials_file is not None:
                self._credentials = Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
                    str(credentials_file),
                    scopes=[GOOGLE_SHEETS_SCOPE],
                )
            elif credentials_json is not None:
                raw = (
                    credentials_json.get_secret_value()
                    if isinstance(credentials_json, SecretStr)
                    else credentials_json
                )
                details = json.loads(raw)
                if not isinstance(details, dict):
                    raise ValueError("credentials JSON must be an object")
                self._credentials = Credentials.from_service_account_info(  # type: ignore[no-untyped-call]
                    details,
                    scopes=[GOOGLE_SHEETS_SCOPE],
                )
            else:
                raise GoogleSheetsConfigurationError(
                    "GOOGLE_SERVICE_ACCOUNT_FILE or GOOGLE_SERVICE_ACCOUNT_JSON "
                    "is required"
                )
        except GoogleSheetsConfigurationError:
            raise
        except (OSError, ValueError, TypeError) as error:
            raise GoogleSheetsConfigurationError(
                "Google service-account credentials are invalid or unreadable"
            ) from error
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        """Return a cached valid token or refresh it safely."""
        async with self._lock:
            if not self._credentials.valid or not self._credentials.token:
                try:
                    await asyncio.to_thread(self._credentials.refresh, Request())
                except Exception as error:
                    raise GoogleSheetsAuthenticationError(
                        "Could not authenticate the Google service account"
                    ) from error
            token = self._credentials.token
            if not isinstance(token, str) or not token:
                raise GoogleSheetsAuthenticationError(
                    "Google authentication returned an empty access token"
                )
            return token


class GoogleSheetsExporter:
    """Export allowlisted research fields through Google's official Sheets API."""

    format_name = "google_sheets"

    def __init__(
        self,
        *,
        spreadsheet_id: str | None = None,
        create_allowed: bool | None = None,
        spreadsheet_title: str | None = None,
        token_provider: GoogleAccessTokenProvider | None = None,
        service_account_file: Path | None = None,
        service_account_json: SecretStr | str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        backoff_seconds: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        settings = get_settings()
        configured_id = (
            spreadsheet_id
            if spreadsheet_id is not None
            else settings.google_sheets_spreadsheet_id
        )
        if configured_id is not None:
            configured_id = configured_id.strip()
            if not _SPREADSHEET_ID_PATTERN.fullmatch(configured_id):
                raise GoogleSheetsConfigurationError(
                    "Google spreadsheet ID contains invalid characters"
                )
        self._spreadsheet_id = configured_id
        self._create_allowed = (
            create_allowed
            if create_allowed is not None
            else settings.google_sheets_create_allowed
        )
        self._spreadsheet_title = (
            spreadsheet_title
            if spreadsheet_title is not None
            else settings.google_sheets_title
        ).strip()
        if not self._spreadsheet_title:
            raise GoogleSheetsConfigurationError("Spreadsheet title must not be blank")
        if self._spreadsheet_id is None and not self._create_allowed:
            raise GoogleSheetsConfigurationError(
                "Set GOOGLE_SHEETS_SPREADSHEET_ID or explicitly permit creation"
            )

        self._timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.google_sheets_timeout_seconds
        )
        self._max_retries = (
            max_retries
            if max_retries is not None
            else settings.google_sheets_max_retries
        )
        self._backoff_seconds = (
            backoff_seconds
            if backoff_seconds is not None
            else settings.google_sheets_backoff_seconds
        )
        if self._timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if not 0 <= self._max_retries <= 10:
            raise ValueError("max_retries must be between zero and ten")
        if self._backoff_seconds < 0:
            raise ValueError("backoff_seconds must not be negative")

        self._token_provider = token_provider or ServiceAccountTokenProvider(
            credentials_file=service_account_file
            or settings.google_service_account_file,
            credentials_json=service_account_json
            or settings.google_service_account_json,
        )
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    async def export(
        self,
        run: ResearchRun,
        records: Sequence[RankedCompanyRecord],
        *,
        context: ExportContext | None = None,
    ) -> ExportArtifact:
        """Write final records, skipped-source audit data, and run metadata."""
        export_context = context or ExportContext()
        spreadsheet_id, sheet_ids = await self._prepare_spreadsheet()
        values = self._build_values(run, records, export_context)
        await self._request(
            "POST",
            f"{GOOGLE_SHEETS_API_URL}/{spreadsheet_id}/values:batchClear",
            json_body={"ranges": [f"'{title}'!A:ZZ" for title in _SHEET_COLUMNS]},
        )
        await self._request(
            "POST",
            f"{GOOGLE_SHEETS_API_URL}/{spreadsheet_id}/values:batchUpdate",
            json_body={
                "valueInputOption": "RAW",
                "data": [
                    {
                        "range": f"'{title}'!A1",
                        "majorDimension": "ROWS",
                        "values": rows,
                    }
                    for title, rows in values.items()
                ],
            },
        )
        await self._request(
            "POST",
            f"{GOOGLE_SHEETS_API_URL}/{spreadsheet_id}:batchUpdate",
            json_body={"requests": self._formatting_requests(sheet_ids, values)},
        )
        return ExportArtifact(
            format_name=self.format_name,
            location=f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}",
            record_count=len(records),
        )

    async def aclose(self) -> None:
        """Close only the internally owned HTTP client."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "GoogleSheetsExporter":
        """Enter an async resource context."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        """Close internally owned resources."""
        await self.aclose()

    async def _prepare_spreadsheet(self) -> tuple[str, dict[str, int]]:
        if self._spreadsheet_id is None:
            response = await self._request(
                "POST",
                GOOGLE_SHEETS_API_URL,
                json_body={
                    "properties": {"title": self._spreadsheet_title},
                    "sheets": [
                        {"properties": {"title": title}} for title in _SHEET_COLUMNS
                    ],
                },
            )
            payload = self._response_object(response)
            spreadsheet_id = payload.get("spreadsheetId")
            if not isinstance(spreadsheet_id, str) or not spreadsheet_id:
                raise GoogleSheetsResponseError(
                    "Google did not return the created spreadsheet ID"
                )
            return spreadsheet_id, self._sheet_ids(payload)

        spreadsheet_id = self._spreadsheet_id
        response = await self._request(
            "GET",
            f"{GOOGLE_SHEETS_API_URL}/{spreadsheet_id}",
            params={"fields": "sheets.properties"},
        )
        sheet_ids = self._sheet_ids(self._response_object(response))
        missing = [title for title in _SHEET_COLUMNS if title not in sheet_ids]
        if missing:
            response = await self._request(
                "POST",
                f"{GOOGLE_SHEETS_API_URL}/{spreadsheet_id}:batchUpdate",
                json_body={
                    "requests": [
                        {"addSheet": {"properties": {"title": title}}}
                        for title in missing
                    ]
                },
            )
            payload = self._response_object(response)
            replies = payload.get("replies")
            if not isinstance(replies, list):
                raise GoogleSheetsResponseError(
                    "Google did not return added sheet identifiers"
                )
            added_ids = self._sheet_ids_from_replies(replies)
            sheet_ids.update(added_ids)
        if not set(_SHEET_COLUMNS).issubset(sheet_ids):
            raise GoogleSheetsResponseError(
                "Could not resolve all required Google sheet identifiers"
            )
        return spreadsheet_id, sheet_ids

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: Mapping[str, object] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            token = await self._token_provider.get_token()
            try:
                response = await self._client.request(
                    method,
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    json=json_body,
                    params=params,
                    timeout=self._timeout_seconds,
                )
            except httpx.RequestError as error:
                if attempt == self._max_retries:
                    raise GoogleSheetsUnavailableError(
                        "Google Sheets API remained unavailable"
                    ) from error
                await self._sleep(self._backoff_seconds * (2**attempt))
                continue

            quota_limited = response.status_code == 429 or (
                response.status_code == 403 and self._is_quota_response(response)
            )
            transient = response.status_code >= 500
            if quota_limited or transient:
                if attempt == self._max_retries:
                    if quota_limited:
                        raise GoogleSheetsQuotaError(
                            "Google Sheets quota remained unavailable after retries"
                        )
                    raise GoogleSheetsUnavailableError(
                        "Google Sheets API remained unavailable after retries"
                    )
                delay = self._retry_delay(response, attempt)
                await self._sleep(delay)
                continue
            if response.status_code in {401, 403}:
                raise GoogleSheetsAuthenticationError(
                    "Google rejected the service account or spreadsheet permission"
                )
            if response.is_error:
                raise GoogleSheetsExporterError(
                    f"Google Sheets API returned HTTP {response.status_code}"
                )
            return response
        raise AssertionError("unreachable retry loop")

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After")
        if value is not None:
            try:
                return min(60.0, max(0.0, float(value.strip())))
            except ValueError:
                pass
        return float(min(60.0, self._backoff_seconds * (2**attempt)))

    @staticmethod
    def _is_quota_response(response: httpx.Response) -> bool:
        try:
            payload = _JSON_OBJECT.validate_python(response.json())
        except (ValueError, ValidationError):
            return False
        error = payload.get("error")
        if not isinstance(error, dict):
            return False
        errors = error.get("errors")
        if not isinstance(errors, list):
            return False
        return any(
            isinstance(item, dict) and item.get("reason") in _QUOTA_REASONS
            for item in errors
        )

    @staticmethod
    def _response_object(response: httpx.Response) -> dict[str, JsonValue]:
        try:
            return _JSON_OBJECT.validate_python(response.json())
        except (ValueError, ValidationError) as error:
            raise GoogleSheetsResponseError(
                "Google Sheets API returned invalid JSON"
            ) from error

    @staticmethod
    def _sheet_ids(payload: Mapping[str, JsonValue]) -> dict[str, int]:
        sheets = payload.get("sheets")
        if not isinstance(sheets, list):
            raise GoogleSheetsResponseError(
                "Google Sheets API response omitted sheet metadata"
            )
        result: dict[str, int] = {}
        for item in sheets:
            if not isinstance(item, dict):
                continue
            properties = item.get("properties")
            if not isinstance(properties, dict):
                continue
            title = properties.get("title")
            sheet_id = properties.get("sheetId")
            if isinstance(title, str) and isinstance(sheet_id, int):
                result[title] = sheet_id
        return result

    @staticmethod
    def _sheet_ids_from_replies(replies: list[JsonValue]) -> dict[str, int]:
        result: dict[str, int] = {}
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            added = reply.get("addSheet")
            if not isinstance(added, dict):
                continue
            properties = added.get("properties")
            if not isinstance(properties, dict):
                continue
            title = properties.get("title")
            sheet_id = properties.get("sheetId")
            if isinstance(title, str) and isinstance(sheet_id, int):
                result[title] = sheet_id
        return result

    def _build_values(
        self,
        run: ResearchRun,
        records: Sequence[RankedCompanyRecord],
        context: ExportContext,
    ) -> dict[str, list[list[str | int | bool]]]:
        research_rows: list[list[str | int | bool]] = [
            list(_SHEET_COLUMNS["Research Results"])
        ]
        research_rows.extend(self._research_row(record, context) for record in records)
        skipped_rows: list[list[str | int | bool]] = [
            list(_SHEET_COLUMNS["Skipped Sources"])
        ]
        skipped_rows.extend(
            self._skipped_row(source) for source in context.skipped_sources
        )
        metadata_rows: list[list[str | int | bool]] = [
            list(_SHEET_COLUMNS["Run Metadata"])
        ]
        metadata_rows.extend(self._metadata_rows(run, len(records), context))
        return {
            "Research Results": research_rows,
            "Skipped Sources": skipped_rows,
            "Run Metadata": metadata_rows,
        }

    def _research_row(
        self,
        ranked: RankedCompanyRecord,
        context: ExportContext,
    ) -> list[str | int]:
        company = ranked.company
        country = self._field_text(company, ("country", "location", "jurisdiction"))
        contact = self._field_text(
            company,
            ("contact_page", "contact_page_url", "contact"),
        )
        evidence_urls = sorted(
            {
                str(url)
                for field in company.extracted_fields
                for evidence in field.evidence
                for url in evidence.urls
            }
        )
        explanation = "\n".join(ranked.relevance.explanation)
        warnings = [
            warning
            for warning in context.warnings
            if company.name.casefold() in warning.casefold()
            or (
                company.website_url is not None
                and str(company.website_url).casefold() in warning.casefold()
            )
        ]
        return [
            self._cell(company.name, 300),
            self._cell(str(company.website_url or ""), 2_048),
            self._cell(country, 300),
            self._cell("\n".join(company.services), 2_000),
            self._cell(contact, 2_048),
            self._cell(company.description or "", 500),
            ranked.relevance.total_score,
            self._cell(explanation, 5_000),
            self._cell("\n".join(evidence_urls), 5_000),
            "approved",
            self._cell("\n".join(warnings), 2_000),
            company.updated_at.isoformat(),
        ]

    def _skipped_row(self, source: SkippedSource) -> list[str | int | bool]:
        reason = source.reason
        normalized_reason = reason.casefold()
        decision = (
            "rejected"
            if any(
                marker in normalized_reason
                for marker in ("blocked", "rejected", "not approved", "not allowed")
            )
            else "skipped"
        )
        return [
            self._cell(urlsplit(str(source.url)).hostname or "", 300),
            self._cell(str(source.url), 2_048),
            decision,
            self._cell(reason, 1_000),
            "see reason" if "robot" in normalized_reason else "not recorded",
            "see reason" if "term" in normalized_reason else "not recorded",
            source.skipped_at.isoformat(),
        ]

    def _metadata_rows(
        self,
        run: ResearchRun,
        completed_count: int,
        context: ExportContext,
    ) -> list[list[str | int | bool]]:
        values: tuple[tuple[str, str | int | bool], ...] = (
            ("topic", run.request.query),
            ("requested result count", run.request.result_count),
            ("completed result count", completed_count),
            ("generated queries", "\n".join(context.generated_queries)),
            ("start time", run.created_at.isoformat()),
            ("completion time", context.completion_time.isoformat()),
            ("providers", "\n".join(context.providers)),
            ("strict compliance mode", context.strict_compliance_mode),
            ("warnings", "\n".join(context.warnings)),
        )
        return [[key, self._cell(value, 5_000)] for key, value in values]

    @staticmethod
    def _field_text(company: CompanyRecord, names: tuple[str, ...]) -> str:
        candidates: list[tuple[float, str]] = []
        normalized_names = set(names)
        for field in company.extracted_fields:
            if field.name not in normalized_names or not field.evidence:
                continue
            value = GoogleSheetsExporter._json_text(field)
            if value:
                candidates.append((field.confidence or 0.0, value))
        return max(candidates, default=(0.0, ""))[1]

    @staticmethod
    def _json_text(field: ExtractedField) -> str:
        value = field.value
        if value is None:
            return ""
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (str, int, float)):
            return str(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    @overload
    def _cell(value: str, maximum: int) -> str: ...

    @staticmethod
    @overload
    def _cell(value: bool, maximum: int) -> bool: ...

    @staticmethod
    @overload
    def _cell(value: int, maximum: int) -> int: ...

    @staticmethod
    def _cell(value: str | int | bool, maximum: int) -> str | int | bool:
        if not isinstance(value, str):
            return value
        without_nulls = value.replace("\x00", "")
        if "\n" in without_nulls:
            cleaned = "\n".join(
                " ".join(line.split()) for line in without_nulls.splitlines()
            )
        else:
            cleaned = " ".join(without_nulls.split())
        cleaned = cleaned[:maximum]
        if cleaned.startswith(_FORMULA_PREFIXES):
            return f"'{cleaned}"
        return cleaned

    @staticmethod
    def _formatting_requests(
        sheet_ids: Mapping[str, int],
        values: Mapping[str, Sequence[Sequence[object]]],
    ) -> list[dict[str, object]]:
        requests: list[dict[str, object]] = []
        for title, columns in _SHEET_COLUMNS.items():
            sheet_id = sheet_ids[title]
            row_count = max(1, len(values[title]))
            column_count = len(columns)
            grid_range = {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": row_count,
                "startColumnIndex": 0,
                "endColumnIndex": column_count,
            }
            requests.extend(
                [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet_id,
                                "gridProperties": {"frozenRowCount": 1},
                            },
                            "fields": "gridProperties.frozenRowCount",
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                **grid_range,
                                "endRowIndex": 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColor": {
                                        "red": 0.18,
                                        "green": 0.35,
                                        "blue": 0.55,
                                    },
                                    "textFormat": {
                                        "bold": True,
                                        "foregroundColor": {
                                            "red": 1,
                                            "green": 1,
                                            "blue": 1,
                                        },
                                    },
                                }
                            },
                            "fields": "userEnteredFormat(backgroundColor,textFormat)",
                        }
                    },
                    {
                        "setBasicFilter": {
                            "filter": {"range": grid_range},
                        }
                    },
                    {
                        "repeatCell": {
                            "range": grid_range,
                            "cell": {
                                "userEnteredFormat": {
                                    "wrapStrategy": "WRAP",
                                    "verticalAlignment": "TOP",
                                }
                            },
                            "fields": (
                                "userEnteredFormat.wrapStrategy,"
                                "userEnteredFormat.verticalAlignment"
                            ),
                        }
                    },
                ]
            )
            for index, width in enumerate(_COLUMN_WIDTHS[title]):
                requests.append(
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": sheet_id,
                                "dimension": "COLUMNS",
                                "startIndex": index,
                                "endIndex": index + 1,
                            },
                            "properties": {"pixelSize": width},
                            "fields": "pixelSize",
                        }
                    }
                )
        return requests
