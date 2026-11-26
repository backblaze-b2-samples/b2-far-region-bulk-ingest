# The make contract (see AGENTS.md): setup, test (offline), demo (real B2).
# demo reads .env when present; test never does.
LOAD_ENV = set -a; if [ -f .env ]; then . ./.env; fi; set +a;
B2_VARS = B2_APPLICATION_KEY_ID B2_APPLICATION_KEY B2_BUCKET_NAME B2_REGION B2_PUBLIC_URL_BASE
SCALE ?= default

.PHONY: setup test demo

setup:
	uv sync
	uv run --frozen python scripts/install_rclone.py

test:
	env $(addprefix -u ,$(B2_VARS)) uv run --frozen pytest -q

demo:
	@$(LOAD_ENV) SCALE="$(SCALE)" uv run --frozen python -m app.demo
