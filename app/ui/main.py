"""Streamlit portfolio dashboard for the research API."""

import time

import streamlit as st
from pydantic import ValidationError

from app.api.schemas import (
    CreateResearchRunRequest,
    ResearchResultsResponse,
    ResearchRunResponse,
    SkippedSourcesResponse,
)
from app.core.settings import get_ui_settings
from app.models import RequestedField, ResearchRunStatus
from app.ui.api_client import DashboardApiError, ResearchApiClient
from app.ui.presentation import (
    DEFAULT_COUNTRY_HINT,
    DEFAULT_FIELDS,
    DEFAULT_LANGUAGE_HINT,
    DEFAULT_RESULT_COUNT,
    DEFAULT_TOPIC,
    FIELD_OPTIONS,
    country_code_from_hint,
    filter_results,
    progress_fraction,
    result_rows,
    rows_to_csv,
)

TERMINAL_STATUSES = {
    ResearchRunStatus.COMPLETED,
    ResearchRunStatus.FAILED,
    ResearchRunStatus.CANCELLED,
}


@st.cache_resource
def _api_client(base_url: str, access_token: str | None) -> ResearchApiClient:
    """Reuse pooled API connections across Streamlit reruns."""
    return ResearchApiClient(base_url, access_token=access_token)


def _initialize_state() -> None:
    defaults: dict[str, object] = {
        "run_id": None,
        "run": None,
        "results": None,
        "skipped_sources": None,
        "dashboard_error": None,
        "sheet_export_url": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _show_api_error(error: DashboardApiError) -> None:
    message = str(error)
    if error.request_id:
        message = f"{message} Request ID: `{error.request_id}`"
    st.session_state.dashboard_error = message


def _start_research(
    client: ResearchApiClient,
    *,
    topic: str,
    result_count: int,
    country_hint: str,
    language_hint: str,
    selected_fields: list[str],
) -> None:
    try:
        country_code = country_code_from_hint(country_hint)
        extraction_fields = [
            RequestedField(name=FIELD_OPTIONS[label])
            for label in selected_fields
            if FIELD_OPTIONS[label] != "relevance_score"
        ]
        if not extraction_fields:
            extraction_fields = [RequestedField(name="company_name")]
        request = CreateResearchRunRequest(
            topic=topic,
            requested_fields=extraction_fields,
            result_count=result_count,
            location=country_hint,
            country=country_code,
            language=language_hint,
            country_tld=country_code.lower(),
        )
        run = client.start_research(request)
    except (DashboardApiError, ValidationError, ValueError) as error:
        if isinstance(error, DashboardApiError):
            _show_api_error(error)
        else:
            st.session_state.dashboard_error = str(error)
        return

    st.session_state.run_id = str(run.id)
    st.session_state.run = run
    st.session_state.results = None
    st.session_state.skipped_sources = None
    st.session_state.dashboard_error = None
    st.session_state.sheet_export_url = None


def _poll_run(client: ResearchApiClient) -> None:
    run_id = st.session_state.run_id
    if not isinstance(run_id, str):
        return
    try:
        run = client.get_run(run_id)
        st.session_state.run = run
        if run.status in TERMINAL_STATUSES:
            if st.session_state.results is None:
                st.session_state.results = client.get_results(run_id)
            if st.session_state.skipped_sources is None:
                st.session_state.skipped_sources = client.get_skipped_sources(run_id)
    except DashboardApiError as error:
        _show_api_error(error)


def _render_status(run: ResearchRunResponse) -> None:
    st.subheader("Research progress")
    st.progress(
        progress_fraction(run),
        text=run.progress_message or "Waiting for the workflow to start…",
    )
    status_label = run.status.value.replace("_", " ").title()
    st.caption(
        f"Run `{run.id}` · **{status_label}**"
        + (
            f" · Stage: `{run.progress_stage.value}`"
            if run.progress_stage is not None
            else ""
        )
    )

    metrics = st.columns(4)
    metrics[0].metric(
        "Discovered candidates",
        run.discovered_candidate_count,
    )
    metrics[1].metric(
        "Approved candidates",
        run.approved_candidate_count,
    )
    metrics[2].metric("Skipped sources", run.skipped_source_count)
    metrics[3].metric("Completed results", run.completed_result_count)

    if run.status is ResearchRunStatus.FAILED:
        st.error(
            run.error_message
            or "The run stopped early. Any site-verified partial "
            "results remain available below."
        )


def _render_results(
    results: ResearchResultsResponse,
    selected_fields: list[str],
) -> None:
    st.subheader("Research results")
    if results.partial:
        st.info(
            "This is a partial result set. Every displayed company still passed "
            "the configured verification and compliance workflow."
        )
    if not results.items:
        st.info("No site-verified results are available yet.")
        return

    minimum_score = st.slider(
        "Minimum relevance score",
        min_value=0,
        max_value=100,
        value=0,
        step=5,
    )
    filtered = filter_results(results.items, minimum_score)
    rows = result_rows(filtered, selected_fields)
    st.caption(
        f"Showing {len(rows)} of {results.total} completed result(s) at a "
        f"minimum score of {minimum_score}."
    )
    st.dataframe(
        rows,
        use_container_width=True,
        hide_index=True,
        column_config={
            key: value
            for key, value in {
                "Website": st.column_config.LinkColumn("Website"),
                "Contact page": st.column_config.LinkColumn("Contact page"),
                "Relevance score": st.column_config.ProgressColumn(
                    "Relevance score",
                    min_value=0,
                    max_value=100,
                    format="%d",
                ),
            }.items()
            if key in selected_fields
        },
    )
    st.download_button(
        "Download filtered results as CSV",
        data=rows_to_csv(rows),
        file_name="research-results.csv",
        mime="text/csv",
        disabled=not rows,
        use_container_width=True,
    )


def _render_export(client: ResearchApiClient, run_id: str) -> None:
    st.subheader("Google Sheets export")
    st.caption(
        "Use the spreadsheet ID from its URL, or leave this blank to use the "
        "server-configured spreadsheet or permitted creation behavior."
    )
    if st.button(
        "Export results to Google Sheets",
        type="secondary",
        use_container_width=True,
    ):
        try:
            response = client.export_google_sheets(
                run_id,
                spreadsheet_id=st.session_state.google_sheet_id.strip() or None,
            )
            st.session_state.sheet_export_url = response.artifact.location
            st.session_state.dashboard_error = None
        except DashboardApiError as error:
            _show_api_error(error)
    export_url = st.session_state.sheet_export_url
    if isinstance(export_url, str):
        st.success("Google Sheets export completed.")
        st.link_button(
            "Open Google Sheet",
            export_url,
            use_container_width=True,
        )


def _render_skipped_sources(skipped: SkippedSourcesResponse) -> None:
    st.subheader("Skipped-source report")
    st.caption(
        "Blocked or ambiguous websites are skipped. Compliance checks are "
        "operational risk controls and do not provide legal advice."
    )
    if not skipped.items:
        st.info("No skipped sources were recorded for this run.")
        return
    st.dataframe(
        [
            {
                "Domain": item.domain,
                "URL": str(item.url),
                "Reason": item.reason,
                "Skipped at": item.skipped_at.isoformat(),
            }
            for item in skipped.items
        ],
        use_container_width=True,
        hide_index=True,
        column_config={"URL": st.column_config.LinkColumn("URL")},
    )


def _render_warnings(
    run: ResearchRunResponse,
    results: ResearchResultsResponse | None,
) -> None:
    warnings = list(run.warnings)
    if results is not None:
        warnings.extend(
            warning for item in results.items for warning in item.validation_warnings
        )
    warnings = list(dict.fromkeys(warnings))
    if not warnings:
        return
    st.subheader("Validation warnings")
    for warning in warnings:
        st.warning(warning)


def main() -> None:
    """Render the complete portfolio dashboard."""
    st.set_page_config(
        page_title="AI Web Research & Data Extraction Agent",
        page_icon="🔎",
        layout="wide",
    )
    _initialize_state()
    settings = get_ui_settings()
    access_token = (
        settings.api_access_token.get_secret_value()
        if settings.api_access_token is not None
        else None
    )
    client = _api_client(settings.api_base_url, access_token)

    st.title("AI Web Research & Data Extraction Agent")
    st.markdown(
        "A compliance-aware portfolio workflow that discovers likely official "
        "company websites, verifies public evidence, extracts structured facts, "
        "and ranks results with deterministic relevance scoring."
    )
    st.info(
        "Blocked or ambiguous websites are skipped. The compliance preflight is "
        "an operational risk signal and is not legal advice."
    )

    with st.form("research_request"):
        topic = st.text_input(
            "Research topic",
            value=DEFAULT_TOPIC,
        )
        form_columns = st.columns(3)
        result_count = form_columns[0].number_input(
            "Required number of results",
            min_value=1,
            max_value=100,
            value=DEFAULT_RESULT_COUNT,
            step=1,
        )
        country_hint = form_columns[1].text_input(
            "Country hint",
            value=DEFAULT_COUNTRY_HINT,
            help="Use a country name or two-letter country code.",
        )
        language_hint = form_columns[2].text_input(
            "Language hint",
            value=DEFAULT_LANGUAGE_HINT,
            help="Two- or three-letter language code, such as en or nl.",
        )
        selected_fields = st.multiselect(
            "Required fields",
            options=list(FIELD_OPTIONS),
            default=DEFAULT_FIELDS,
            key="requested_fields",
        )
        st.caption(
            "Strict compliance mode is enforced by the API server; blocked and "
            "ambiguous sources are skipped."
        )
        start = st.form_submit_button(
            "Start Research",
            type="primary",
            disabled=not selected_fields,
            use_container_width=True,
        )

    st.text_input(
        "Google Sheet ID",
        value="",
        key="google_sheet_id",
        help=(
            "Optional. Enter the ID from a Google Sheets URL before exporting, "
            "or leave blank to use server configuration."
        ),
    )

    if start:
        _start_research(
            client,
            topic=topic,
            result_count=int(result_count),
            country_hint=country_hint,
            language_hint=language_hint,
            selected_fields=selected_fields,
        )

    if isinstance(st.session_state.run_id, str):
        _poll_run(client)

    if isinstance(st.session_state.dashboard_error, str):
        st.error(st.session_state.dashboard_error)

    run = st.session_state.run
    results = st.session_state.results
    skipped = st.session_state.skipped_sources
    if isinstance(run, ResearchRunResponse):
        _render_status(run)
        if isinstance(results, ResearchResultsResponse):
            _render_results(results, st.session_state.requested_fields)
        if run.status in TERMINAL_STATUSES and isinstance(
            st.session_state.run_id,
            str,
        ):
            _render_export(client, st.session_state.run_id)
        if isinstance(skipped, SkippedSourcesResponse):
            _render_skipped_sources(skipped)
        _render_warnings(
            run,
            results if isinstance(results, ResearchResultsResponse) else None,
        )

        if (
            run.status not in TERMINAL_STATUSES
            and st.session_state.dashboard_error is None
        ):
            time.sleep(1)
            st.rerun()


if __name__ == "__main__":
    main()
