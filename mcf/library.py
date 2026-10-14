"""The song library: a small SQLite database plus the audio files."""

import json
import logging
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    file TEXT NOT NULL,
    created REAL NOT NULL,
    model TEXT NOT NULL,
    mode TEXT NOT NULL,
    seconds REAL NOT NULL,
    style TEXT NOT NULL DEFAULT '',
    lyrics TEXT NOT NULL DEFAULT '',
    settings TEXT NOT NULL DEFAULT '{}',
    tags TEXT NOT NULL DEFAULT '',
    favourite INTEGER NOT NULL DEFAULT 0,
    credits TEXT NOT NULL DEFAULT '{}',
    profile TEXT NOT NULL DEFAULT ''
);
"""


@dataclass
class Song:
    id: str
    title: str
    file: str
    created: float
    model: str
    mode: str
    seconds: float
    style: str = ""
    lyrics: str = ""
    settings: dict = field(default_factory=dict)
    tags: str = ""
    favourite: bool = False
    credits: dict = field(default_factory=dict)
    profile: str = ""


class Library:
    def __init__(self, folder: Path):
        self.folder = folder
        self.songs_dir = folder / "songs"
        self.songs_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(folder / "library.db", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def path(self, song: Song) -> Path:
        return self.songs_dir / song.file

    def _row(self, row) -> Song:
        (sid, title, file, created, model, mode, seconds, style, lyrics, settings,
         tags, favourite, credits, profile) = row
        return Song(sid, title, file, created, model, mode, seconds, style, lyrics,
                    _json(settings), tags, bool(favourite), _json(credits), profile)

    def all(self) -> list[Song]:
        rows = self.db.execute("SELECT * FROM songs ORDER BY created DESC").fetchall()
        return [self._row(r) for r in rows]

    def get(self, song_id: str) -> Song | None:
        row = self.db.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        return self._row(row) if row else None

    def add(self, audio: Path, title: str, model: str, mode: str, seconds: float, style: str,
            lyrics: str, settings: dict, credits: dict | None = None, profile: str = "") -> Song:
        """Move a finished audio file into the library."""
        song_id = uuid.uuid4().hex[:12]
        file = f"{song_id}{audio.suffix.lower()}"
        shutil.move(str(audio), str(self.songs_dir / file))
        song = Song(song_id, title, file, time.time(), model, mode, seconds, style, lyrics,
                    settings, "", False, credits or {}, profile)
        self.db.execute("INSERT INTO songs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (song.id, song.title, song.file, song.created, song.model, song.mode, song.seconds,
                         song.style, song.lyrics, json.dumps(song.settings), song.tags, 0,
                         json.dumps(song.credits), song.profile))
        self.db.commit()
        return song

    def update(self, song: Song) -> None:
        self.db.execute("UPDATE songs SET title=?, tags=?, favourite=?, credits=? WHERE id=?",
                        (song.title, song.tags, int(song.favourite), json.dumps(song.credits), song.id))
        self.db.commit()

    def delete(self, song: Song) -> None:
        self.path(song).unlink(missing_ok=True)
        self.db.execute("DELETE FROM songs WHERE id=?", (song.id,))
        self.db.commit()

    def unique_title(self, base: str) -> str:
        titles = {s.title for s in self.all()}
        if base not in titles:
            return base
        n = 2
        while f"{base} {n}" in titles:
            n += 1
        return f"{base} {n}"


def _json(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
