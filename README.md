# AI Web Research & Data Extraction Agent

A portfolio project for a web research and structured data extraction agent.
This initial commit provides the application skeleton, development tooling, a
FastAPI health endpoint, and a minimal Streamlit interface.

Search, crawling, AI extraction, and Google Sheets integration are intentionally
out of scope for this version.

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
