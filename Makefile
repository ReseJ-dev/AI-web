.PHONY: install run-api run-ui migrate migration docker-up docker-down docker-logs docker-ps format lint typecheck test test-live check

install:
	python -m pip install -e ".[dev]"

run-api:
	uvicorn app.api.main:app --reload --env-file .env

run-ui:
	streamlit run app/ui/main.py

migrate:
	alembic upgrade head

migration:
	alembic revision --autogenerate -m "$(message)"

docker-up:
	docker compose up --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs --follow

docker-ps:
	docker compose ps

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

test-live:
	@test "$$RUN_LIVE_WEBSITE_SMOKE_TESTS" = "true" || ( \
		echo "Set RUN_LIVE_WEBSITE_SMOKE_TESTS=true explicitly to run live tests."; \
		exit 1 \
	)
	pytest -m live -ra

check: lint typecheck test
