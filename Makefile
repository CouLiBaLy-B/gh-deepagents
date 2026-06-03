.PHONY: install dev test lint fmt run-fix run-evolve

install:
	pip install -e ".[ollama,dev]"

dev: install
	pre-commit install || true

test:
	pytest -q

lint:
	ruff check src tests
	mypy src --ignore-missing-imports || true

fmt:
	ruff format src tests
	ruff check --fix src tests

# Quick helpers (need .env populated)
run-fix:
	gh-deepagent fix "$$ISSUE_URL"

run-evolve:
	gh-deepagent evolve --repo "$$REPO" --instruction "$$INSTRUCTION"
