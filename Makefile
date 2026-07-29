.PHONY: install run-api run-ui migrate migration format lint typecheck test check

install:
	python -m pip install -e ".[dev]"

run-api:
	uvicorn app.api.main:app --reload

run-ui:
	streamlit run app/ui/main.py

migrate:
	alembic upgrade head

migration:
	alembic revision --autogenerate -m "$(message)"

format:
	ruff format .
	ruff check --fix .

lint:
	ruff format --check .
	ruff check .

typecheck:
	mypy app tests

test:
	pytest

check: lint typecheck test
