"""Shared TTS defaults for Qwen3-TTS via mlx-audio."""

from __future__ import annotations

DEFAULT_TTS_MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"
DEFAULT_SPEAKER = "Ryan"
DEFAULT_LANGUAGE = "English"
DEFAULT_MODE = "custom_voice"

MODE_CUSTOM_VOICE = "custom_voice"
MODE_CLONE = "clone"
MODE_VOICE_DESIGN = "voice_design"
