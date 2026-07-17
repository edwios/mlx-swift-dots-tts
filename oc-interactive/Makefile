SHELL := /bin/bash
VENV := .venv
BIN := $(VENV)/bin/oc-interactive
PYTHON := $(VENV)/bin/python

.PHONY: venv install run help

venv:
	@test -d $(VENV) || python3 -m venv $(VENV)

install: venv
	@$(PYTHON) -m pip install -q -U pip
	@$(PYTHON) -m pip install -q -e .
	@echo "installed -> $(BIN)"
	@echo "run: make run ARGS='-t \"Hello\" --speaker Ryan'"
	@echo "or:  $(BIN) -t \"Hello\" --speaker Ryan --instruct \"friendly\""

run: install
	@$(BIN) $(ARGS)

help:
	@echo "targets: install, run ARGS='...', venv"
	@echo "example: make run ARGS='-t /help --speaker Ryan --instruct \"upbeat\"'"
	@echo "clone:   make run ARGS='-t Hello -m mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit -r ref.wav --reftext \"transcript\"'"
