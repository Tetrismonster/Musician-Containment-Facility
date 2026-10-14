"""Saving library songs in the format the user picks, with credits in the file's tags."""

from pathlib import Path

import soundfile as sf

from mcf import APP_SHORT_NAME
from mcf.i18n import _

FORMATS = {
    # key: (libsndfile format, subtype, extension)
    "wav": ("WAV", "PCM_24", ".wav"),
    "flac": ("FLAC", "PCM_24", ".flac"),
    "mp3": ("MP3", "MPEG_LAYER_III", ".mp3"),
    "ogg": ("OGG", "VORBIS", ".ogg"),
}
# libsndfile sets MP3 bitrate through a compression level
MP3_LEVELS = {320: 0.0, 256: 0.2, 192: 0.4, 128: 0.7}
BLOCK = 48000 * 10


def wildcard() -> str:
    """For the Save As dialog's "Save as type" list."""
    return "|".join([
        _("WAV, full quality (*.wav)|*.wav"),
        _("FLAC, full quality, smaller (*.flac)|*.flac"),
        _("MP3 (*.mp3)|*.mp3"),
        _("OGG Vorbis (*.ogg)|*.ogg"),
    ])


FORMAT_ORDER = ["wav", "flac", "mp3", "ogg"]


def credit_line(song) -> str:
    """For example: "Cover of Song, written by Writer, originally by Artist. Made with MCF and ACE-Step 1.5."."""
    parts = []
    credits = song.credits or {}
    if credits.get("original_title"):
        text = _("Cover of {title}").format(title=credits["original_title"])
        if credits.get("writers"):
            text += _(", written by {writers}").format(writers=credits["writers"])
        if credits.get("original_artist"):
            text += _(", originally by {artist}").format(artist=credits["original_artist"])
        parts.append(text + ".")
    made = _("Made with {app} and {model}.").format(app=APP_SHORT_NAME, model=song.model)
    if song.profile:
        made = _("Made with {app} and {model}, using the artist profile {profile}.").format(
            app=APP_SHORT_NAME, model=song.model, profile=song.profile)
    parts.append(made)
    return " ".join(parts)


def export(source: Path, dest: Path, fmt: str, song, mp3_bitrate: int = 320) -> None:
    """Convert source to dest. Writes to a temporary name first, so a failure leaves nothing behind."""
    major, subtype, _ext = FORMATS[fmt]
    extra = {}
    if fmt == "mp3":
        extra = {"compression_level": MP3_LEVELS.get(mp3_bitrate, 0.0), "bitrate_mode": "CONSTANT"}
    tmp = dest.with_name(dest.name + ".part")
    try:
        with sf.SoundFile(source) as src, sf.SoundFile(
                tmp, "w", src.samplerate, src.channels, format=major, subtype=subtype, **extra) as out:
            out.title = song.title
            out.software = APP_SHORT_NAME
            out.comment = credit_line(song)
            while True:
                block = src.read(BLOCK, dtype="float32", always_2d=True)
                if not len(block):
                    break
                out.write(block)
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


def duration(path: Path) -> float:
    info = sf.info(str(path))
    return info.frames / info.samplerate if info.samplerate else 0.0
