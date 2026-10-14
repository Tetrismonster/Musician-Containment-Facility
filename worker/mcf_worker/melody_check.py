"""Measure how closely a cover follows the original's vocal melody.

The original's notes come from MCF's cover analysis. The cover's vocals are
separated and pitch-tracked the same way. Each original note counts as
followed when the cover sings the same pitch class there (octave and key
changes don't matter). The score is the share of notes followed, weighted by
note length so short ornaments count less than held notes.
"""

import numpy as np

from mcf_worker import dsp

# How far the cover's timing may drift from the original before a note counts as missed
TIME_TOLERANCE = 0.15
# Notes shorter than this are ornaments, not melody
MIN_NOTE = 0.12


def pitch_track(vocals_16k: np.ndarray, rate: int = 16000) -> tuple[np.ndarray, np.ndarray]:
    """MIDI pitch per frame (0 where unvoiced), and the frame times."""
    f0, times = dsp.yin(vocals_16k, rate)
    midi = np.where(f0 > 0, 69 + 12 * np.log2(np.maximum(f0, 1e-6) / 440.0), 0.0)
    return midi, times


def _pitch_class_distance(a: np.ndarray, b: float) -> np.ndarray:
    d = np.abs((a - b) % 12)
    return np.minimum(d, 12 - d)


def score(notes: list, midi: np.ndarray, times: np.ndarray, time_scale: float = 1.0,
          transpose: int | None = None, offset: float = 0.0) -> dict:
    """How much of the melody the cover follows.

    notes: the original's [[start, end, midi], ...]. time_scale stretches the
    original's times to the cover's tempo (original bpm / cover bpm), and
    offset moves them (seconds), for a cover with a longer or shorter intro.
    transpose: the known key change in semitones, or None to find the best one.
    """
    notes = [(s * time_scale + offset, e * time_scale + offset, m) for s, e, m in notes if e - s >= MIN_NOTE]
    if not notes or not len(times):
        return {"percent": 0.0, "notes": 0, "transpose": 0, "sung_percent": 0.0}
    hop = times[1] - times[0] if len(times) > 1 else 0.01
    windows = []
    for start, end, pitch in notes:
        lo = np.searchsorted(times, start - TIME_TOLERANCE)
        hi = np.searchsorted(times, end + TIME_TOLERANCE)
        sung = midi[lo:hi][midi[lo:hi] > 0]
        windows.append((end - start, pitch, sung))
    total = sum(w for w, _p, _s in windows)
    sung_weight = sum(w for w, _p, s in windows if len(s) * hop >= 0.5 * w)

    def followed(shift: int) -> float:
        hit = 0.0
        for weight, pitch, sung in windows:
            if len(sung) * hop < 0.5 * weight:
                continue  # Nothing sung here
            # Followed when at least 40 percent of the sung frames are within half a semitone
            close = _pitch_class_distance(sung, pitch + shift) <= 0.5
            if close.mean() >= 0.4:
                hit += weight
        return hit / total

    shifts = [transpose % 12] if transpose is not None else range(12)
    best = max(shifts, key=followed)
    return {"percent": round(100 * followed(best), 1), "notes": len(notes),
            "transpose": int(best if best <= 6 else best - 12),
            "sung_percent": round(100 * sung_weight / total, 1)}


def best_alignment(notes: list, midi: np.ndarray, times: np.ndarray, time_scale: float = 1.0,
                   transpose: int | None = None, max_offset: float = 4.0) -> dict:
    """score() at the start offset that fits best, for covers whose intro length changed.
    Searching offsets raises chance agreement a little, so compare against a baseline scored the same way."""
    offsets = np.arange(-max_offset, max_offset + 1e-9, 0.05)
    results = [dict(score(notes, midi, times, time_scale, transpose, float(o)), offset=round(float(o), 2))
               for o in offsets]
    return max(results, key=lambda r: r["percent"])
