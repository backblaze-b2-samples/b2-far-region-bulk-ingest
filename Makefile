# The make contract (see AGENTS.md): setup, test (offline), demo (real B2).
# demo reads .env when present; test never does.
LOAD_ENV = set -a; if [ -f .env ]; then . ./.env; fi; set +a;

.PHONY: setup test demo

setup:
	uv sync

test:
	uv run --frozen pytest -q

demo:
	@$(LOAD_ENV) uv run --frozen python -m app.demo
