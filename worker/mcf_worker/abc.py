"""Write MCF's cover analysis as an ABC score, for models that follow a written melody (YuE2).

Only the melody, chord names, tempo and section names go into the score, so
the original recording never reaches the model.
"""

SHARP_NAMES = ["C", "^C", "D", "^D", "E", "F", "^F", "G", "^G", "A", "^A", "B"]
STEPS_PER_BEAT = 4  # L:1/16 in 4/4 time
STEPS_PER_BAR = 16


def pitch(midi: int) -> str:
    """ABC pitch: C is middle C (MIDI 60), c the octave above, C, the octave below."""
    name = SHARP_NAMES[midi % 12]
    octave = midi // 12 - 1  # MIDI 60 is octave 4
    accidental, letter = (name[0], name[1]) if name.startswith("^") else ("", name)
    if octave >= 5:
        return accidental + letter.lower() + "'" * (octave - 5)
    return accidental + letter + "," * (4 - octave)


def _duration(steps: int) -> str:
    return "" if steps == 1 else str(steps)


def _transpose_chord(name: str, shift: int) -> str:
    if name == "N" or not shift:
        return name
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    minor = name.endswith("m")
    root = names.index(name[:-1] if minor else name)
    return names[(root + shift) % 12] + ("m" if minor else "")


def build(analysis: dict, follow: dict, bpm: float | None = None, transpose: int = 0,
          sections: list[int] | None = None, max_seconds: float | None = None) -> str:
    """The score, with one ABC line of bars per section."""
    source_bpm = float(analysis.get("bpm") or 120)
    beats = analysis.get("beats") or []
    start_time = float(beats[0]) if beats else 0.0
    step_seconds = 60.0 / source_bpm / STEPS_PER_BEAT
    shift = transpose if not follow.get("key", True) else 0
    tempo = round(bpm) if bpm and not follow.get("tempo", True) else round(source_bpm)

    all_sections = analysis.get("sections") or [{"name": "Song", "start": 0.0, "end": analysis["duration"]}]
    if follow.get("structure", True) or not sections:
        chosen = list(range(len(all_sections)))
    else:
        chosen = [i for i in sections if 0 <= i < len(all_sections)]

    def to_step(t: float) -> int:
        return max(0, round((t - start_time) / step_seconds))

    lines = ["X:1", "T:", "M:4/4", "L:1/16", f"Q:1/4={tempo}",
             'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"',
             'V: Ins clef=treble name="Ins Melody" snm="Inst."', "K:C"]
    used_seconds = 0.0
    for index in chosen:
        section = all_sections[index]
        first_bar = to_step(section["start"]) // STEPS_PER_BAR
        last_bar = max(first_bar + 1, -(-to_step(section["end"]) // STEPS_PER_BAR))
        if max_seconds and not follow.get("length", True):
            bar_seconds = STEPS_PER_BAR * step_seconds
            room = int((max_seconds - used_seconds) // bar_seconds)
            if room <= 0:
                break
            last_bar = min(last_bar, first_bar + room)
            used_seconds += (last_bar - first_bar) * bar_seconds
        lo, hi = first_bar * STEPS_PER_BAR, last_bar * STEPS_PER_BAR
        grid: list[int | None] = [None] * (hi - lo)
        starts = [False] * (hi - lo)
        for note_start, note_end, midi in analysis.get("notes", []):
            a, b = to_step(note_start), max(to_step(note_start) + 1, to_step(note_end))
            if b <= lo or a >= hi:
                continue
            a, b = max(a, lo), min(b, hi)
            for s in range(a, b):
                grid[s - lo] = int(midi) + shift
            starts[a - lo] = True
        chords = {}
        if follow.get("chords", True):
            for chord_start, _chord_end, name in analysis.get("chords", []):
                bar = to_step(chord_start) // STEPS_PER_BAR
                if first_bar <= bar < last_bar and name != "N":
                    chords.setdefault(bar, _transpose_chord(name, shift))
        bars = []
        previous_chord = None
        for bar in range(first_bar, last_bar):
            offset = (bar - first_bar) * STEPS_PER_BAR
            text = ""
            chord = chords.get(bar)
            if chord and chord != previous_chord:
                text += f'"{chord}"'
                previous_chord = chord
            s = 0
            while s < STEPS_PER_BAR:
                value = grid[offset + s]
                length = 1
                while s + length < STEPS_PER_BAR and grid[offset + s + length] == value \
                        and not (value is not None and starts[offset + s + length]):
                    length += 1
                if value is None:
                    text += "z" + _duration(length)
                else:
                    text += pitch(value) + _duration(length)
                    # Tie into the next bar when the note continues
                    continued = offset + s + length < len(grid) and s + length == STEPS_PER_BAR \
                        and grid[offset + s + length] == value and not starts[offset + s + length]
                    if continued:
                        text += "-"
                s += length
            bars.append(text)
        lines.append(f"% {section['name'].lower()}")
        lines.append("V: Vocal")
        lines.append("|".join(bars) + "|")
        lines.append("V: Ins")
        lines.append(f"Z{len(bars)}|")
    return "\n".join(lines) + "\n"
