.DEFAULT_GOAL := help

.PHONY: help
help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

.PHONY: install
install: ## Create .venv with all extras and the dev dependency group
	uv sync --all-extras

.PHONY: install-pre-commit
install-pre-commit: install ## Install git hooks (pre-commit and commit-msg)
	uv run pre-commit install --hook-type pre-commit --hook-type commit-msg

.PHONY: lint
lint: ## Run every pre-commit hook on all files
	uv run pre-commit run --all-files

.PHONY: typecheck
typecheck: ## Run mypy
	uv run mypy

.PHONY: test
test: ## Unit + acceptance tests in parallel (no cluster needed)
	uv run pytest -n auto

.PHONY: test-unit
test-unit: ## Unit tests only
	uv run pytest -n auto tests/unit

.PHONY: test-acceptance
test-acceptance: ## Fault-injected API acceptance tests (no cluster needed)
	uv run pytest -n auto tests/acceptance

.PHONY: test-integration
test-integration: ## Integration tests on the kind cluster named by PICELI_KIND_KUBECONFIG/PICELI_KIND_CONTEXT
	# kind tests share one cluster: they run serially
	uv run pytest tests/integration

.PHONY: skill-check
skill-check: ## Fresh-agent check: run the agent skill's walkthrough from a copy of skills/piceli (fake API)
	uv run python scripts/skill_check.py

.PHONY: evals-check
evals-check: ## Self-tests of the cross-model eval harness (mock models, no keys, no network)
	uv run --frozen pytest -n auto evals/tests

.PHONY: coverage
coverage: ## Unit + acceptance tests with an HTML coverage report
	uv run pytest -n auto --cov --cov-report=term --cov-report=html

.PHONY: docs-reference
docs-reference: ## Regenerate docs/reference/{errors,cli}.md from code
	uv run python -m piceli.reference_docs

.PHONY: docs
docs: ## Build the documentation (warnings are errors)
	uv run --group docs sphinx-build -j auto -W --keep-going -b html docs docs/_build/html

.PHONY: docs-inventories
docs-inventories: ## Refresh the committed intersphinx fallbacks in docs/_inventories
	curl -sfL https://docs.python.org/3/objects.inv -o docs/_inventories/python.inv
	curl -sfL https://docs.pynenc.org/en/latest/objects.inv -o docs/_inventories/pynenc.inv

.PHONY: build
build: ## Build sdist and wheel into dist/
	uv build

.PHONY: ui-contract ui-contract-check ui-install ui-check ui-build ui-fake-serve ui-clips test-ui test-ui-fake test-ui-composition test-ui-package test-browser test-browser-cluster-oidc test-browser-delivery test-ui-kind-delivery test-ui-kind-renderer test-ui-kind-runtime test-ui-kind-e2e test-ui-performance test-ui-browser-performance test-ui-soak test-ui-access-retention-smoke test-ui-access-retention-bound test-ui-access-retention

PICELI_UI_FAKE_PORT ?= 4177
PICELI_UI_FAKE_TEST_PORT ?= 4184
ui-contract: ## Regenerate browser types from the public service contract
	uv run --frozen python scripts/ui_contract.py
	uv run --frozen --extra ui python scripts/ui_openapi.py
	cd ui && npm exec -- openapi-typescript openapi.json -o src/api/openapi.ts

ui-contract-check: ## Verify generated browser types are current
	uv run --frozen python scripts/ui_contract.py --check
	uv run --frozen --extra ui python scripts/ui_openapi.py --check

ui-install: ## Install the locked contributor frontend toolchain
	cd ui && npm ci

ui-check: ## Check the browser contract and TypeScript application
	$(MAKE) ui-contract-check
	cd ui && npm run typecheck && npm run lint

ui-build: ## Bundle offline browser assets into the Python package
	cd ui && npm run build

ui-fake-serve: ## Open a read-only local UI with a disposable fake Kubernetes API (prints its launch URL)
	uv run --frozen --extra ui python tests/browser/serve_ui.py --port $(PICELI_UI_FAKE_PORT)

ui-clips: ## Record UI journeys as WebP/GIF clips and PNGs into .ui-clips/ (needs ffmpeg, Chromium)
	uv run --frozen --extra ui python tests/browser/run_clips.py

test-ui: ## Run service and legacy UI acceptance checks against the fake API
	uv run --frozen --extra ui pytest -n auto tests/unit/test_ui_contracts.py tests/unit/services/test_environment_control.py tests/unit/services/test_composition_control.py tests/unit/server/test_launch_token.py tests/unit/server/test_ui_state_archive.py tests/acceptance/test_ui_truthfulness.py tests/acceptance/test_ui_service.py tests/acceptance/test_ui_pipeline.py tests/acceptance/test_ui_composition.py

test-ui-fake: ## Run fake-API UI service and browser journeys (requires Chromium)
	$(MAKE) test-ui
	PICELI_UI_TEST_PORT=$(PICELI_UI_FAKE_TEST_PORT) $(MAKE) test-browser
	$(MAKE) test-ui-composition

test-ui-composition: ## Run the in-cluster composition UI journeys (desktop, phone) on the fake API
	uv run --frozen --extra ui python tests/browser/run.py --config ../tests/browser/composition.config.cjs

test-ui-package: ## Verify installed wheel/sdist offline assets without Node
	uv run --frozen --extra ui pytest -n auto tests/acceptance/test_ui_packaging.py

test-browser: ## Run real-service browser journeys with temporary artifacts
	uv run --frozen --extra ui python tests/browser/run.py

test-browser-cluster-oidc: ## Run signed OIDC login and grant revocation in Chromium over disposable HTTPS
	uv run --frozen --extra ui python tests/browser/run_cluster_oidc.py

test-browser-delivery: ## Run approved deploy/recovery browser journeys at three viewports
	uv run --frozen --extra ui python tests/browser/run_delivery.py

test-ui-kind-delivery: ## Run a browser deploy/change/rollback in a disposable kind cluster
	uv run --frozen --extra ui python scripts/ui_kind.py uv run --frozen --extra ui python tests/browser/run_delivery.py --project=desktop

test-ui-kind-renderer: ## Run the token-free renderer Job in a disposable kind cluster
	uv run --frozen --extra ui python scripts/ui_kind.py uv run --frozen --extra ui python -m pytest -q tests/integration/test_ui_renderer_job_kind.py

test-ui-kind-runtime: ## Boot the installed UI and verify PVC survives a Pod restart in disposable kind
	uv run --frozen --extra ui python scripts/ui_kind.py uv run --frozen --extra ui python -m pytest -q tests/integration/test_ui_cluster_runtime_kind.py

test-ui-kind-e2e: ## Exercise OIDC, build, deploy, recovery, access and egress in one disposable kind cluster
	uv run --frozen --extra ui python scripts/ui_kind.py uv run --frozen --extra ui python -m pytest -q tests/integration/test_ui_installed_e2e_kind.py

test-ui-performance: ## Measure the shared-observation 50-app/5,000-object/10-client fixture
	uv run --frozen python tests/performance/ui_observation_fixture.py

test-ui-browser-performance: ## Measure ten browser sessions under API latency and CPU throttling
	uv run --frozen --extra ui python tests/browser/run.py --config ../tests/browser/performance.config.cjs

test-ui-soak: ## Run the 30-minute shared-watch retention fixture
	uv run --frozen python -m tests.performance.ui_observation_soak

test-ui-access-retention-smoke: ## Run a five-second local forward lifecycle smoke test
	uv run --frozen python tests/performance/ui_access_retention.py --smoke

test-ui-access-retention-bound: ## Cross the 128-record local forward history bound
	uv run --frozen python tests/performance/ui_access_retention.py --sessions 129

test-ui-access-retention: ## Run the 30-minute local forward/process retention gate
	uv run --frozen python tests/performance/ui_access_retention.py

.PHONY: clean
clean: ## Remove build, coverage and docs output
	rm -rf dist htmlcov .coverage .coverage.* docs/_build docs/apidocs
