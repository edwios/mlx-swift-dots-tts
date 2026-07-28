#!/bin/bash
# Restart the oc-interactive orchestration daemon so it picks up code
# changes (see README.md "Troubleshooting" -> "After code changes"). It
# auto-restarts on the next turn.

pkill -f "oc_interactive --daemon"

