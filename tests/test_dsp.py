"""Cover analysis signal processing, tested on synthetic songs with known answers."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
from mcf_worker import dsp  # noqa: E402

SR = 16000


def tone(freq, seconds, sr=SR, harmonics=(1.0, 0.4, 0.2)):
    t = np.arange(int(seconds * sr)) / sr
    return sum(a * np.sin(2 * np.pi * freq * (k + 1) * t) for k, a in enumerate(harmonics)) * 0.3


def midi_hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def test_yin_finds_melody_notes():
    melody = [60, 62, 64, 65, 67, 69]
    x = np.concatenate([np.concatenate([tone(midi_hz(m), 0.4), np.zeros(int(0.1 * SR))]) for m in melody])
    f0, times = dsp.yin(x, SR)
    notes = dsp.notes_from_f0(f0, times)
    assert [int(n[2]) for n in notes] == melody
    assert notes[0][0] == pytest.approx(0.0, abs=0.06)
    assert notes[1][0] == pytest.approx(0.5, abs=0.06)


def clicks(bpm, seconds, sr=SR):
    x = np.zeros(int(seconds * sr))
    period = 60 / bpm
    burst = np.random.default_rng(0).standard_normal(int(0.02 * sr)) * np.exp(-np.linspace(0, 6, int(0.02 * sr)))
    t = 0.5
    while t < seconds - 0.05:
        i = int(t * sr)
        x[i:i + len(burst)] += burst
        t += period
    return x


@pytest.mark.parametrize("bpm", [84, 100, 128, 150])
def test_tempo_and_beats(bpm):
    x = clicks(bpm, 20)
    env = dsp.onset_envelope(x, SR)
    found = dsp.tempo(env, 100)
    assert found == pytest.approx(bpm, rel=0.03)
    b = dsp.beats(env, 100, found)
    assert np.median(np.diff(b)) == pytest.approx(60 / bpm, rel=0.04)


def chord_audio(names, seconds_each):
    parts = []
    for name in names:
        parts.append(sum(tone(midi_hz(p), seconds_each) for p in dsp.chord_pitches(name)))
    return np.concatenate(parts)


def test_chords_and_key():
    progression = ["C", "G", "Am", "F"] * 2
    x = chord_audio(progression, 2.0)
    c, fps = dsp.chroma(x, SR)
    beat_times = np.arange(0, 16.01, 0.5)
    chords = dsp.detect_chords(c, fps, beat_times, beats_per_chord=2)
    assert [ch[2] for ch in chords] == progression
    assert dsp.detect_key(c)[0] in ("C major", "A minor")


def test_sections_label_repeated_blocks_as_chorus():
    lines = [
        {"start": 6, "end": 9, "text": "Walking down the street"},
        {"start": 9.5, "end": 12, "text": "Lights are shining bright"},
        {"start": 20, "end": 23, "text": "We keep on moving on"},
        {"start": 23.5, "end": 26, "text": "Dancing till the dawn"},
        {"start": 30, "end": 33, "text": "Second verse is here"},
        {"start": 40, "end": 43, "text": "We keep on moving on"},
        {"start": 43.5, "end": 46, "text": "Dancing till the dawn"},
    ]
    sections = dsp.sections_from_lines(lines, 60)
    names = [s["name"] for s in sections]
    assert names == ["Intro", "Verse 1", "Chorus", "Verse 2", "Chorus", "Outro"]
    assert sections[0]["start"] == 0 and sections[-1]["end"] == 60
    text = dsp.lyrics_text(sections)
    assert text.startswith("[Intro]\n\n[Verse 1]\nWalking down the street")


def test_split_lines_at_punctuation():
    lines = [{"start": 0.0, "end": 10.0, "text": "Walking down the avenue tonight, city lights are shining bright."}]
    split = dsp.split_lines(lines)
    assert [l["text"] for l in split] == ["Walking down the avenue tonight", "city lights are shining bright."]
    assert split[0]["start"] == 0 and split[-1]["end"] == pytest.approx(10.0)
    assert split[0]["end"] == split[1]["start"]


def test_abc_score_from_analysis():
    from mcf_worker import abc
    assert abc.pitch(60) == "C" and abc.pitch(72) == "c" and abc.pitch(84) == "c'" and abc.pitch(48) == "C,"
    assert abc.pitch(61) == "^C" and abc.pitch(71) == "B"
    # 120 BPM: one sixteenth is 0.125 s, one bar is 2 s
    analysis = {"bpm": 120, "duration": 4.0, "beats": [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5],
                "notes": [[0.0, 0.5, 64], [0.5, 1.0, 67], [1.5, 2.5, 69]],
                "chords": [[0.0, 2.0, "C"], [2.0, 4.0, "Am"]],
                "sections": [{"name": "Verse 1", "start": 0.0, "end": 4.0}]}
    everything = {"melody": True, "lyrics": True, "structure": True, "tempo": True, "key": True, "chords": True,
                  "phrasing": True, "length": True}
    score = abc.build(analysis, everything)
    assert "Q:1/4=120" in score and "% verse 1" in score
    melody = score.split("V: Vocal\n")[-1].split("\n")[0]
    assert melody == '"C"E4G4z4A4-|"Am"A4z12|'
    moved = abc.build(analysis, dict(everything, key=False, tempo=False, chords=False), bpm=90, transpose=2)
    assert "Q:1/4=90" in moved and '"' not in moved.split("K:C")[1]
    assert moved.split("V: Vocal\n")[-1].startswith("^F4A4z4B4-|B4z12|")


def test_melody_check_scores_followed_and_wrong_melodies():
    from mcf_worker import melody_check
    melody = [60, 62, 64, 65, 67, 69, 71, 72]
    notes = [[i * 0.5, i * 0.5 + 0.4, m] for i, m in enumerate(melody)]

    def sung(pitches):
        return np.concatenate([np.concatenate([tone(midi_hz(m), 0.4), np.zeros(int(0.1 * SR))]) for m in pitches])

    same = melody_check.score(notes, *melody_check.pitch_track(sung(melody)), transpose=0)
    assert same["percent"] > 90
    # A key change counts as followed when the shift is allowed for
    up = melody_check.score(notes, *melody_check.pitch_track(sung([m + 3 for m in melody])))
    assert up["percent"] > 90 and up["transpose"] == 3
    # A different tune doesn't
    other = melody_check.score(notes, *melody_check.pitch_track(sung([67, 64, 60, 69, 62, 71, 65, 72])), transpose=0)
    assert other["percent"] < 40


def test_lead_sheet_follows_beats_and_choices():
    from mcf_worker import lead_sheet
    # 120 bpm: one beat is 0.5 seconds, one sixteenth 0.125
    analysis = {"duration": 16.0, "bpm": 120, "beats": [i * 0.5 for i in range(32)],
                "notes": [[0.0, 0.5, 60], [2.0, 2.25, 62], [8.0, 9.0, 64]],
                "chords": [[0.0, 2.0, "C"], [2.0, 4.0, "Am"]],
                "sections": [{"name": "Verse 1", "start": 0.0, "end": 8.0},
                             {"name": "Chorus", "start": 8.0, "end": 16.0}]}
    everything = {a: True for a in ("melody", "lyrics", "structure", "tempo", "key", "chords", "length")}
    sheet = lead_sheet.build(analysis, everything)
    assert sheet["melody"] == [[0, 60, 4], [16, 62, 2], [64, 64, 8]]
    assert sorted(p for s, p, d in sheet["chords"] if s == 0) == [0, 4, 7]
    assert sorted(p for s, p, d in sheet["chords"] if s == 16) == [0, 4, 9]
    # Only the chorus, a tone higher, without chords
    chorus = lead_sheet.build(analysis, {**everything, "structure": False, "key": False, "chords": False},
                              transpose=2, sections=[1])
    assert chorus["melody"] == [[0, 66, 8]] and chorus["chords"] == []


def test_stuck_transcription_repeats_are_cut():
    lines = [{"start": float(i), "end": i + 1.0, "text": t} for i, t in enumerate(
        ["We sing praise", "We sing praise", "to the Lord"] + ["oh"] * 200 + ["Praise to the Lord"])]
    kept = dsp.drop_repeats(lines)
    assert [line["text"] for line in kept] == ["We sing praise", "We sing praise", "to the Lord", "oh", "oh", "oh",
                                               "Praise to the Lord"]
    assert kept[5]["end"] == 203.0  # The last kept "oh" covers the whole run
