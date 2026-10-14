"""User preferences, stored as JSON in the portable settings folder."""

import copy
import json
import logging
from pathlib import Path
from typing import Any

from mcf import paths

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "general": {
        "first_run_complete": False,
        "language": "system",
        "check_model_list_updates": True,
    },
    "speech": {
        # quiet, normal or detailed
        "verbosity": "normal",
        # auto (screen reader), sapi (Windows voice) or none (status bar only)
        "output": "auto",
        # What to do in auto mode when no screen reader is running: sapi or none
        "fallback": "none",
        "interrupt": False,
        "braille": True,
        "announce_progress": True,
        "announce_completion": True,
        "announce_hints": True,
        "sounds": False,
    },
    "guidance": {
        "beginner": True,
        "explain_on_focus": True,
    },
    "storage": {
        # Empty means the default folder inside the MCF folder
        "models_dir": "",
        "library_dir": "",
    },
    "saving": {
        # wav, flac, mp3 or ogg
        "default_format": "wav",
        "default_folder": "",
        "always_ask": True,
        "mp3_bitrate": 320,
    },
    "performance": {
        "long_generation_warning_minutes": 5,
        # below_normal, normal or idle
        "worker_priority": "below_normal",
        # auto, gpu, offload, compressed or cpu
        "mode": "auto",
    },
    "generation": {
        # Empty means the first installed model
        "model": "",
        "length_seconds": 120,
        "steps": 8,
        "guidance": 7.0,
        # Language code of the lyrics; models sing more reliably when they know it
        "language": "en",
    },
    "cover": {
        "preset": "faithful",
        "rights_confirmed": False,
    },
    "keyboard": {
        "cancel_hotkey": "ctrl+alt+shift+c",
    },
}


def _merge(defaults: dict, stored: dict) -> dict:
    """Stored values over defaults, ignoring unknown keys and wrong types."""
    result = copy.deepcopy(defaults)
    for key, default in defaults.items():
        if key not in stored:
            continue
        value = stored[key]
        if isinstance(default, dict):
            if isinstance(value, dict):
                result[key] = _merge(default, value)
        elif isinstance(value, type(default)) and not (
            isinstance(value, bool) and not isinstance(default, bool)
        ):
            result[key] = value
    return result


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or paths.settings_dir() / "settings.json"
        self.data = copy.deepcopy(DEFAULTS)

    def load(self) -> None:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.exception("Could not read settings; using defaults")
            return
        if isinstance(stored, dict):
            self.data = _merge(DEFAULTS, stored)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, key: str) -> Any:
        section, name = key.split(".", 1)
        return self.data[section][name]

    def set(self, key: str, value: Any) -> None:
        section, name = key.split(".", 1)
        if name not in self.data[section]:
            raise KeyError(key)
        self.data[section][name] = value

    def models_dir(self) -> Path:
        return Path(self.get("storage.models_dir") or paths.default_models_dir())

    def library_dir(self) -> Path:
        return Path(self.get("storage.library_dir") or paths.default_library_dir())
