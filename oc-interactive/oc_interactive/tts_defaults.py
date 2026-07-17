"""Shared TTS defaults for Qwen3-TTS via mlx-audio."""

from __future__ import annotations

DEFAULT_TTS_MODEL = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"
DEFAULT_SPEAKER = "Ryan"
DEFAULT_LANGUAGE = "English"
DEFAULT_MODE = "custom_voice"

MODE_CUSTOM_VOICE = "custom_voice"
MODE_CLONE = "clone"
MODE_VOICE_DESIGN = "voice_design"

# One HF id per mode; mlx-audio downloads on first use.
DEFAULT_MODEL_BY_MODE: dict[str, str] = {
    MODE_CUSTOM_VOICE: DEFAULT_TTS_MODEL,
    MODE_VOICE_DESIGN: "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit",
    MODE_CLONE: "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit",
}
