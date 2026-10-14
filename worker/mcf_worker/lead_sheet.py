"""Turn MCF's cover analysis into a lead sheet on a sixteenth-note grid: melody notes and chord tones.

Models that follow symbolic music (MuLaCover) get only this, so the original
recording never reaches them. The grid follows the analysed beats, so a song
whose tempo drifts still lines up with its bars.
"""

import numpy as np

from mcf_worker import dsp

STEPS_PER_BEAT = 4
STEPS_PER_BAR = 16


class Grid:
    """Seconds to sixteenth-note steps, following the analysed beats."""

    def __init__(self, analysis: dict):
        beats = np.asarray(analysis.get("beats") or [], dtype=float)
        bpm = float(analysis.get("bpm") or 120)
        if len(beats) < 2:
            beats = np.arange(0.0, float(analysis["duration"]) + 1, 60.0 / bpm)
        interval = float(np.median(np.diff(beats)))
        # Whole beats before the first detected beat, so the grid starts at or before 0 seconds
        lead = int(np.ceil(beats[0] / interval))
        self.beats = np.concatenate([beats[0] - interval * np.arange(lead, 0, -1), beats])
        self.interval = interval

    def step(self, t: float) -> int:
        b = self.beats
        if t <= b[0]:
            beat = (t - b[0]) / self.interval
        elif t >= b[-1]:
            beat = len(b) - 1 + (t - b[-1]) / self.interval
        else:
            beat = float(np.interp(t, b, np.arange(len(b))))
        return max(0, round(beat * STEPS_PER_BEAT))


def build(analysis: dict, follow: dict, transpose: int = 0, sections: list[int] | None = None,
          max_seconds: float | None = None) -> dict:
    """{"melody": [[step, midi, steps], ...], "chords": [[step, pitch_class, steps], ...], "steps": n}.

    Structure and length choices cut whole bars out; the key choice transposes.
    Chords are left out when the cover shouldn't follow them.
    """
    grid = Grid(analysis)
    shift = transpose if not follow.get("key", True) else 0
    all_sections = analysis.get("sections") or [{"name": "Song", "start": 0.0, "end": analysis["duration"]}]
    if follow.get("structure", True) or not sections:
        chosen = list(range(len(all_sections)))
    else:
        chosen = [i for i in sections if 0 <= i < len(all_sections)] or list(range(len(all_sections)))
    if follow.get("structure", True):
        # The whole song, including any instrumental parts between sections
        spans = [(0, -(-grid.step(float(analysis["duration"])) // STEPS_PER_BAR))]
    else:
        spans = []
        for index in chosen:
            section = all_sections[index]
            first = grid.step(section["start"]) // STEPS_PER_BAR
            spans.append((first, max(first + 1, -(-grid.step(section["end"]) // STEPS_PER_BAR))))
    if max_seconds and not follow.get("length", True):
        bar_seconds = grid.interval * 4
        room = max(1, int(max_seconds // bar_seconds))
        cut = []
        for first, last in spans:
            if room <= 0:
                break
            cut.append((first, min(last, first + room)))
            room -= cut[-1][1] - first
        spans = cut

    notes = [(grid.step(a), max(grid.step(a) + 1, grid.step(b)), int(m)) for a, b, m in analysis.get("notes", [])]
    chords = []
    if follow.get("chords", True):
        for a, b, name in analysis.get("chords", []):
            if name != "N":
                chords.append((grid.step(a), max(grid.step(a) + 1, grid.step(b)), name))

    melody_out, chords_out = [], []
    cursor = 0
    for first, last in spans:
        lo, hi = first * STEPS_PER_BAR, last * STEPS_PER_BAR
        for a, b, midi in notes:
            if a >= lo and a < hi:
                melody_out.append([cursor + a - lo, midi + shift, min(b, hi) - a])
        for a, b, name in chords:
            a2, b2 = max(a, lo), min(b, hi)
            if a2 < b2:
                for pc in dsp.chord_pitches(name):
                    chords_out.append([cursor + a2 - lo, (pc + shift) % 12, b2 - a2])
        cursor += hi - lo
    return {"melody": melody_out, "chords": chords_out, "steps": cursor}
