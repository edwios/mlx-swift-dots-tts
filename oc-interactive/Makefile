SHELL := /bin/bash
VENV := .venv
BIN := $(VENV)/bin/oc-interactive
PYTHON := $(VENV)/bin/python

.PHONY: venv install run help

venv:
	@test -d $(VENV) || python3 -m venv $(VENV)

install: venv
	@$(PYTHON) -m pip install -q -e .
	@echo "installed -> $(BIN)"
	@echo "run: make run ARGS='-t \"Hello\" -r ref.wav --reftext \"transcript\"'"
	@echo "or:  $(BIN) -t \"Hello\" -r ref.wav --reftext \"transcript\""

run: install
	@$(BIN) $(ARGS)

help:
	@echo "targets: install, run ARGS='...', venv"
	@echo "example: make run ARGS='-t /help -r ref.wav --reftext \"transcript\" -m ../dots.tts-soar-mlx/4bit'"
