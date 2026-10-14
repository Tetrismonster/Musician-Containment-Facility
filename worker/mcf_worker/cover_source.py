"""Prepare the source audio for a cover, according to what the cover should follow.

ACE-Step's cover mode reads the source through its semantic tokenizer: about
five coarse codes per second describing melody, rhythm and lyric timing. The
model then performs the song again from scratch in the new style. What the
cover follows is controlled by what goes into that source:

- Instrumentation off: only the separated vocal line is used, so the original
  arrangement never reaches the model. This is also the most reliable way to
  keep the vocals when the style changes. The vocal line carries the harmony
  well enough: a synthesised chord pad under it made the model follow the
  melody less closely in every test (17 percent of notes against 26), so
  there isn't one.
- Tempo and key: the source is time-stretched or pitch-shifted.
- Structure and length: sections are cut out or the song is shortened.
- Phrasing off: the model stops following the source for its last step, so it
  can loosen the timing.

Strength is the share of the model's steps that follow the source. The rest
are generated from the text alone, and at 8 steps even two free steps were
enough to lose much of the melody (tests on 2026-10-02: 0.7 followed 22
percent of the notes, 1.0 followed 26 percent, a song with no source 12).
"""

import logging
from pathlib import Path

import numpy as np

from mcf_worker import analysis, protocol

log = logging.getLogger("mcf_worker.cover_source")

RATE = 48000

# How strongly the cover follows the prepared source: the share of steps that follow it
BASE_STRENGTH = 1.0
LOOSE_PHRASING = 0.1


def strength(follow: dict) -> float:
    value = BASE_STRENGTH
    if not follow.get("phrasing", True):
        value -= LOOSE_PHRASING
    return round(value, 2)


def _crossfade_join(pieces: list[np.ndarray], fade: int) -> np.ndarray:
    out = pieces[0]
    for piece in pieces[1:]:
        if len(out) > fade and len(piece) > fade:
            ramp = np.linspace(0, 1, fade)[None, :]
            overlap = out[:, -fade:] * (1 - ramp) + piece[:, :fade] * ramp
            out = np.concatenate([out[:, :-fade], overlap, piece[:, fade:]], axis=1)
        else:
            out = np.concatenate([out, piece], axis=1)
    return out


def _keep_ranges(cover: dict) -> list[tuple[float, float]]:
    """Which parts of the original to keep, after structure and length choices."""
    a = cover["analysis"]
    follow = cover.get("follow", {})
    duration = float(a["duration"])
    sections = a.get("sections") or []
    keep = [(0.0, duration)]
    chosen = cover.get("sections")
    if not follow.get("structure", True) and chosen and sections:
        keep = [(sections[i]["start"], sections[i]["end"]) for i in chosen if 0 <= i < len(sections)] or keep
    max_seconds = cover.get("max_seconds")
    if not follow.get("length", True) and max_seconds:
        total, cut = 0.0, []
        for start, end in keep:
            if total >= max_seconds:
                break
            end = min(end, start + max_seconds - total)
            cut.append((start, end))
            total += end - start
        keep = cut or keep
    return keep


def prepare(cover: dict, source: Path, work_dir: Path, device: str) -> Path:
    import soundfile as sf
    import torch
    import torchaudio.functional as F

    follow = cover.get("follow", {})
    protocol.status("prepare")
    audio, rate = analysis.load_audio(source)
    audio = analysis.resample(audio, rate, RATE)

    use_vocals = not follow.get("instrumentation", False)
    if use_vocals:
        vocals, _backing = analysis.separate(
            audio, RATE, Path(cover["tools_dir"]) / "demucs" / "hdemucs_high_trained.pt", device)
        audio = analysis.resample(vocals, analysis.SEP_RATE, RATE)

    # Structure and length
    ranges = _keep_ranges(cover)
    if ranges != [(0.0, cover["analysis"]["duration"])]:
        pieces = [audio[:, int(a * RATE):int(b * RATE)] for a, b in ranges]
        audio = _crossfade_join([p for p in pieces if p.shape[1]], int(0.05 * RATE))
        tail = min(audio.shape[1], int(1.5 * RATE))
        audio[:, -tail:] *= np.linspace(1, 0, tail)[None, :]

    tensor = torch.as_tensor(audio, dtype=torch.float32)
    # Tempo: stretch time without changing pitch
    target_bpm = cover.get("bpm")
    original_bpm = float(cover["analysis"].get("bpm") or 0)
    if not follow.get("tempo", True) and target_bpm and original_bpm and abs(target_bpm - original_bpm) > 0.5:
        rate_factor = float(target_bpm) / original_bpm
        n_fft, hop = 2048, 512
        spec = torch.stft(tensor, n_fft, hop, window=torch.hann_window(n_fft), return_complex=True)
        stretched = F.phase_vocoder(spec, rate_factor, torch.linspace(0, np.pi * hop, spec.shape[-2])[..., None])
        tensor = torch.istft(stretched, n_fft, hop, window=torch.hann_window(n_fft))
    # Key: shift pitch without changing speed
    shift = int(cover.get("transpose", 0)) if not follow.get("key", True) else 0
    if shift:
        tensor = F.pitch_shift(tensor, RATE, shift)

    audio = tensor.numpy()
    peak = float(np.abs(audio).max()) or 1.0
    audio = audio / peak * 0.9
    path = work_dir / "cover-source.wav"
    sf.write(str(path), audio.T.astype(np.float32), RATE, subtype="PCM_16")
    return path
