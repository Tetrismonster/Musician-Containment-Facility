"""Portable folder layout.

Everything MCF writes lives next to the app, so the whole folder can be
copied to another drive or computer.
"""

import sys
from pathlib import Path


def app_dir() -> Path:
    """Folder containing MCF.exe, or the project root when run from source."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Folder containing bundled read-only resources."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_dir()))
    return app_dir()


def settings_dir() -> Path:
    return app_dir() / "settings"


def presets_dir() -> Path:
    return settings_dir() / "presets"


def logs_dir() -> Path:
    return app_dir() / "logs"


def default_models_dir() -> Path:
    return app_dir() / "models"


def default_library_dir() -> Path:
    return app_dir() / "library"


def runtime_dir() -> Path:
    return app_dir() / "runtime"


def profiles_dir() -> Path:
    return app_dir() / "profiles"


def temp_dir() -> Path:
    return app_dir() / "temp"


def licenses_dir() -> Path:
    return resource_dir() / "licenses"


def locale_dir() -> Path:
    return resource_dir() / "locale"


def data_dir() -> Path:
    """Bundled data files, such as the model manifest."""
    return resource_dir() / "mcf" / "data" if not getattr(sys, "frozen", False) else resource_dir() / "data"


def worker_dir() -> Path:
    """Folder holding the worker package, run by the runtime's Python."""
    return resource_dir() / "worker"
