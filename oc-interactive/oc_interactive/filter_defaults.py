"""Defaults for the local LLM text-filter (OpenAI-compatible endpoint, e.g. LM Studio).

The filter routes text that would otherwise go straight to TTS through a
local chat model first (see ``llm_filter.py`` and the ``/filter`` slash
command in ``slash.py`` / ``daemon.py``). Both values below can be
overridden per-deployment via ``filterBaseURL`` / ``filterModel`` in
oc-interactive.json.
"""

from __future__ import annotations

DEFAULT_FILTER_BASE_URL = "http://10.0.1.29:22206"
DEFAULT_FILTER_MODEL = "qwen3.5-2b-uncensored-hauhaucs-aggressive-mlx"
