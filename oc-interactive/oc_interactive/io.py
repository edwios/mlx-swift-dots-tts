"""CLI stderr helpers."""

from __future__ import annotations

import sys


class CliError(Exception):
    """User-facing CLI validation error."""


def eprint(*args, **kwargs) -> None:
    kwargs.setdefault("file", sys.stderr)
    print(*args, **kwargs)
