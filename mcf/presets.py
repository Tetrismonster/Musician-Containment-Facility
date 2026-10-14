"""Generation presets and cover presets, stored as JSON in the settings folder."""

import json
import logging
import re
from pathlib import Path

from mcf import paths
from mcf.i18n import _

log = logging.getLogger(__name__)

# What a cover can follow. The melody is always followed.
COVER_ASPECTS = ["melody", "lyrics", "structure", "tempo", "key", "chords", "phrasing", "length", "instrumentation"]


def aspect_label(aspect: str) -> str:
    return {
        "melody": _("&Melody (always followed)"),
        "lyrics": _("Original &lyrics"),
        "structure": _("Song s&tructure: the order of verses, choruses and so on"),
        "tempo": _("T&empo"),
        "key": _("&Key"),
        "chords": _("C&hord progression"),
        "phrasing": _("Vocal &phrasing: how the words sit on the beat"),
        "length": _("Len&gth"),
        "instrumentation": _("&Instrumentation"),
    }[aspect]


def builtin_cover_presets() -> dict[str, dict]:
    everything = {a: True for a in COVER_ASPECTS}
    return {
        "faithful": {"name": _("Faithful: follows everything except the instrumentation"),
                     "follow": {**everything, "instrumentation": False}},
        "arrangement": {"name": _("New arrangement: new tempo, key and chords are allowed"),
                        "follow": {**everything, "tempo": False, "key": False, "chords": False,
                                   "instrumentation": False}},
        "genre": {"name": _("Genre swap: keeps melody, lyrics and structure; the style description decides the rest"),
                  "follow": {a: a in ("melody", "lyrics", "structure") for a in COVER_ASPECTS}},
        "loose": {"name": _("Loose: follows only the melody and lyrics"),
                  "follow": {a: a in ("melody", "lyrics") for a in COVER_ASPECTS}},
    }


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\- ]", "_", name).strip()[:80] or "preset"


class PresetStore:
    """Named JSON documents in one folder."""

    def __init__(self, folder: Path):
        self.folder = folder

    def names(self) -> list[str]:
        if not self.folder.exists():
            return []
        result = []
        for path in sorted(self.folder.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result.append(str(data.get("name", path.stem)))
            except (OSError, ValueError):
                log.warning("Skipping unreadable preset %s", path)
        return sorted(result, key=str.lower)

    def load(self, name: str) -> dict | None:
        path = self.folder / f"{_safe_name(name)}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data.get("values") if isinstance(data, dict) and isinstance(data.get("values"), dict) else None

    def save(self, name: str, values: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{_safe_name(name)}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"name": name, "values": values}, indent=2), encoding="utf-8")
        tmp.replace(path)

    def delete(self, name: str) -> None:
        (self.folder / f"{_safe_name(name)}.json").unlink(missing_ok=True)


def generation_presets() -> PresetStore:
    return PresetStore(paths.presets_dir())


def cover_presets() -> PresetStore:
    return PresetStore(paths.settings_dir() / "cover-presets")
