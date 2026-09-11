VENV ?= $(CURDIR)/.venv
PYTHON ?= $(VENV)/bin/python

.PHONY: local-test-env test-local-executor test-local-tooling test-runnable-image
local-test-env: ## Bootstrap local tests with an available Python 3.12 interpreter
	@test -x "$(PYTHON)" || uv venv --python 3.12 "$(VENV)"
	@uv pip install --reinstall --require-hashes --python "$(PYTHON)" -r tests/local-requirements.lock
	@uv pip install --reinstall --no-deps --python "$(PYTHON)" -e .

test-local-executor: ## Unit and fault-injected loopback acceptance; never contacts a cluster
	@$(PYTHON) scripts/test_local_executor.py

test-local-tooling: ## Artifacts, explicit local import, fault API and real Poet restart
	@$(PYTHON) scripts/test_local_tooling.py --ih-workspace "$(IH_WORKSPACE)" --docker "$(DOCKER)" --docker-socket "$(DOCKER_SOCKET)"

test-runnable-image: ## LC-06-R offline runtime closure, import, execution and Poet restart
	@$(PYTHON) scripts/test_runnable_image.py --ih-workspace "$(IH_WORKSPACE)" --docker "$(DOCKER)" --docker-socket "$(DOCKER_SOCKET)"
