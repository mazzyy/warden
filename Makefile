.PHONY: help install test lint fmt demo demo-live demo-oom probe check registry-sync

help:
	@echo "  make install    create .venv and install dependencies"
	@echo "  make test       run the suite — no cloud, no key, no spend"
	@echo "  make probe      print the policy matrix (agent x tool)"
	@echo "  make demo       one incident end to end, offline and free"
	@echo "  make demo-live  the same, against real Gemini (costs tokens)"
	@echo "  make check      lint + test + probe assertion. run before every commit"

install:
	python3 -m venv .venv
	./.venv/bin/pip install -U pip
	./.venv/bin/pip install -r requirements-dev.txt
	@echo "\nNow: cp .env.example .env  &&  make demo"

test:
	./.venv/bin/python -m pytest -q

lint:
	./.venv/bin/ruff check warden tests

fmt:
	./.venv/bin/ruff check --fix warden tests
	./.venv/bin/ruff format warden tests

probe:
	./.venv/bin/python -m warden.probe --explain

demo:
	./.venv/bin/python -m warden.agents.demo

demo-oom:
	./.venv/bin/python -m warden.agents.demo --mode oom

demo-live:
	./.venv/bin/python -m warden.agents.demo --live

# The gate. `no agent can write to the cluster` is a build failure if it
# stops being true, not a claim in a README.
check: lint test
	./.venv/bin/python -m warden.probe --assert-no-cluster-writes

registry-sync:
	./.venv/bin/python -c "import asyncio; from warden.control_plane.registry import load_all, sync_to_store; from warden.config import settings; asyncio.run(sync_to_store(load_all(settings().manifest_dir), settings().gcp_project))"

models:
	./.venv/bin/python -m warden.doctor --models
