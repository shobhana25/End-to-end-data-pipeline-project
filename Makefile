# Health capacity pipeline
#
#   make setup      install dependencies
#   make all        run the whole pipeline (fetch -> publish)
#   make offline    rebuild from the existing landing zone, no network
#   make test       unit + integration tests (no network)
#   make lint       ruff check and format check
#   make dashboard  regenerate docs/index.html only
#   make app        launch the interactive Streamlit app
#   make clean      remove generated data, keep the landing zone
#   make distclean  remove everything generated, landing zone included

PYTHON ?= python3
CLI     = $(PYTHON) -m pipelines.cli

.DEFAULT_GOAL := help
.PHONY: help setup setup-dashboard all offline ingest stage transform quality dashboard docs app test lint format clean distclean

help:
	@grep -E '^#   ' $(MAKEFILE_LIST) | sed 's/^#   //'

setup:
	$(PYTHON) -m pip install -r requirements-dev.txt

setup-dashboard:
	$(PYTHON) -m pip install -r requirements-dashboard.txt

all:
	$(CLI) all

offline:
	$(CLI) all --offline

ingest:
	$(CLI) ingest

stage:
	$(CLI) stage

transform:
	$(CLI) transform

quality:
	$(CLI) quality

dashboard:
	$(CLI) dashboard

docs: dashboard
	$(PYTHON) scripts/build_data_dictionary.py

app:
	streamlit run dashboard/app.py

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:
	$(PYTHON) -m ruff format .

clean:
	rm -rf data/staged/* data/warehouse/* .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	@echo "Landing zone kept - run 'make offline' to rebuild without refetching."

distclean: clean
	rm -rf data/raw/*
	@git checkout -- data/raw/.gitkeep data/staged/.gitkeep data/warehouse/.gitkeep 2>/dev/null || true
