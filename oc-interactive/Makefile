SHELL := /bin/bash
VENV := .venv
BIN := $(VENV)/bin/oc-interactive
CHAT_BIN := $(VENV)/bin/oc-interactive-chat
PYTHON := $(VENV)/bin/python

.PHONY: venv install run run-chat help

venv:
	@test -d $(VENV) || python3 -m venv $(VENV)

install: venv
	@$(PYTHON) -m pip install -q -U pip
	@$(PYTHON) -m pip install -q -e .
	@echo "installed -> $(BIN)"
	@echo "run: make run ARGS='-t \"Hello\" --speaker Ryan'"
	@echo "or:  $(BIN) -t \"Hello\" --speaker Ryan --instruct \"friendly\""
	@echo "chat UI: make run-chat ARGS='--speaker Ryan'"

run: install
	@$(BIN) $(ARGS)

run-chat: install
	@$(CHAT_BIN) $(ARGS)

help:
	@echo "targets: install, run ARGS='...', run-chat ARGS='...', venv"
	@echo "example: make run ARGS='-t /help --speaker Ryan --instruct \"upbeat\"'"
	@echo "clone:   make run ARGS='-t Hello -m mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit -r ref.wav --reftext \"transcript\"'"
	@echo "chat UI: make run-chat ARGS='--speaker Ryan --instruct \"warm\"'"
