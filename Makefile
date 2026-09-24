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
test: ## Unit + acceptance tests (no cluster needed)
	uv run pytest

.PHONY: test-unit
test-unit: ## Unit tests only
	uv run pytest tests/unit

.PHONY: test-acceptance
test-acceptance: ## Fault-injected API acceptance tests (no cluster needed)
	uv run pytest tests/acceptance

.PHONY: test-integration
test-integration: ## Integration tests against the current kubeconfig context (use a disposable kind cluster)
	uv run pytest tests/integration

.PHONY: coverage
coverage: ## Unit + acceptance tests with an HTML coverage report
	uv run pytest --cov --cov-report=term --cov-report=html

.PHONY: docs
docs: ## Build the documentation (warnings are errors)
	uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html

.PHONY: build
build: ## Build sdist and wheel into dist/
	uv build

.PHONY: clean
clean: ## Remove build, coverage and docs output
	rm -rf dist htmlcov .coverage .coverage.* docs/_build docs/apidocs
