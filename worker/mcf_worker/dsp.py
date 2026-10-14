"""Signal processing for cover analysis, in plain NumPy.

Pitch (YIN), tempo and beats (onset autocorrelation plus dynamic
programming), key (Krumhansl-Schmuckler profiles) and chords (triad templates
over beat-synchronous chroma).
"""

import difflib
import re

import numpy as np

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def frames(x: np.ndarray, size: int, hop: int) -> np.ndarray:
    if len(x) < size:
        x = np.pad(x, (0, size - len(x)))
    count = 1 + (len(x) - size) // hop
    idx = np.arange(size)[None, :] + hop * np.arange(count)[:, None]
    return x[idx]


# Pitch


def yin(x: np.ndarray, sr: int, fmin: float = 70.0, fmax: float = 1000.0, hop: int = 160,
        threshold: float = 0.15) -> tuple[np.ndarray, np.ndarray]:
    """Fundamental frequency per hop (0 where unvoiced), and the frame times."""
    tau_min = max(2, int(sr / fmax))
    tau_max = int(sr / fmin)
    window = 2 ** int(np.ceil(np.log2(tau_max * 2)))
    size = window + tau_max
    f = frames(x.astype(np.float64), size, hop)
    head = f[:, :window]
    n_fft = 2 ** int(np.ceil(np.log2(size + window)))
    # r(tau) = sum_j head_j * f_{j+tau}, by FFT cross-correlation
    corr = np.fft.irfft(np.conj(np.fft.rfft(head, n_fft)) * np.fft.rfft(f, n_fft), n_fft)[:, :tau_max + 1]
    sq = np.cumsum(np.pad(f ** 2, ((0, 0), (1, 0))), axis=1)
    energy0 = sq[:, window][:, None]
    taus = np.arange(tau_max + 1)
    energy_tau = sq[:, taus + window] - sq[:, taus]
    diff = np.maximum(energy0 + energy_tau - 2 * corr, 0)
    diff[:, 0] = 0
    cumulative = np.cumsum(diff[:, 1:], axis=1)
    cmnd = np.ones_like(diff)
    cmnd[:, 1:] = diff[:, 1:] * taus[1:] / np.maximum(cumulative, 1e-12)

    f0 = np.zeros(len(f))
    for i, row in enumerate(cmnd):
        region = row[tau_min:tau_max]
        below = np.nonzero(region < threshold)[0]
        if len(below):
            t = below[0]
            while t + 1 < len(region) and region[t + 1] < region[t]:
                t += 1
        else:
            t = int(np.argmin(region))
            if region[t] > 0.35:
                continue
        tau = t + tau_min
        if 1 <= tau < tau_max:
            a, b, c = row[tau - 1], row[tau], row[tau + 1]
            denom = a - 2 * b + c
            tau = tau + (0.5 * (a - c) / denom if abs(denom) > 1e-12 else 0)
        f0[i] = sr / tau
    # Silence can't be voiced
    rms = np.sqrt((head ** 2).mean(axis=1))
    loud = np.percentile(rms, 95) if len(rms) else 0
    f0[rms < loud * 0.03] = 0
    times = (np.arange(len(f0)) * hop + window / 2) / sr
    return f0, times


def median_filter(x: np.ndarray, width: int) -> np.ndarray:
    pad = width // 2
    padded = np.pad(x, pad, mode="edge")
    return np.median(frames(padded, width, 1), axis=1)[:len(x)]


def notes_from_f0(f0: np.ndarray, times: np.ndarray, min_seconds: float = 0.08) -> list[list[float]]:
    """[[start, end, midi], ...] from a pitch track."""
    hop = times[1] - times[0] if len(times) > 1 else 0.01
    midi = np.where(f0 > 0, 69 + 12 * np.log2(np.maximum(f0, 1e-6) / 440.0), 0)
    voiced = f0 > 0
    smooth = median_filter(midi, 5)
    notes = []
    start = None
    values: list[float] = []
    for i in range(len(midi) + 1):
        end_note = i == len(midi) or not voiced[i]
        if not end_note and start is not None and abs(smooth[i] - np.median(values)) > 0.8:
            end_note = True
        if end_note and start is not None:
            if (i - start) * hop >= min_seconds:
                notes.append([float(times[start] - hop / 2), float(times[i - 1] + hop / 2),
                              float(round(np.median(values)))])
            start, values = None, []
        if i < len(midi) and voiced[i]:
            if start is None:
                start, values = i, []
            values.append(smooth[i])
    merged: list[list[float]] = []
    for note in notes:
        if merged and merged[-1][2] == note[2] and note[0] - merged[-1][1] < 0.05:
            merged[-1][1] = note[1]
        else:
            merged.append(note)
    return merged


# Rhythm


def stft_magnitude(x: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    f = frames(x, n_fft, hop) * np.hanning(n_fft)
    return np.abs(np.fft.rfft(f, axis=1))


def onset_envelope(x: np.ndarray, sr: int, hop: int = 160) -> np.ndarray:
    mag = np.log1p(100 * stft_magnitude(x, 1024, hop))
    flux = np.maximum(np.diff(mag, axis=0), 0).sum(axis=1)
    flux = np.concatenate([[0], flux])
    local = np.convolve(flux, np.ones(31) / 31, mode="same")
    env = np.maximum(flux - local, 0)
    return env / (env.std() + 1e-9)


def tempo(env: np.ndarray, fps: float, lo: float = 60.0, hi: float = 200.0) -> float:
    env = env - env.mean()
    n = len(env)
    ac = np.fft.irfft(np.abs(np.fft.rfft(env, 2 * n)) ** 2)[:n]
    best, best_score = 120.0, -np.inf
    for bpm in np.arange(lo, hi, 0.25):
        lag = 60 * fps / bpm
        # Sum of the autocorrelation at the beat period and its multiples
        score = 0.0
        for k, weight in ((1, 1.0), (2, 0.5), (4, 0.25)):
            pos = lag * k
            i = int(pos)
            if i + 1 < n:
                frac = pos - i
                score += weight * ((1 - frac) * ac[i] + frac * ac[i + 1])
        # Prefer common tempos, as people would tap them
        score *= np.exp(-0.5 * (np.log2(bpm / 115.0) / 0.9) ** 2)
        if score > best_score:
            best, best_score = bpm, score
    return float(best)


def beats(env: np.ndarray, fps: float, bpm: float, tightness: float = 100.0) -> np.ndarray:
    """Beat times in seconds (Ellis' dynamic programming beat tracker)."""
    period = 60 * fps / bpm
    n = len(env)
    score = env.copy().astype(np.float64)
    back = np.full(n, -1)
    lo, hi = int(round(period / 2)), int(round(period * 2))
    for t in range(lo, n):
        start = max(0, t - hi)
        end = t - lo
        if end <= start:
            continue
        prev = np.arange(start, end)
        penalty = -tightness * np.log((t - prev) / period) ** 2
        candidates = score[prev] + penalty
        k = int(np.argmax(candidates))
        score[t] = env[t] + candidates[k]
        back[t] = prev[k]
    # Start from the best-scoring frame in the last beat period
    tail = max(0, n - int(period))
    t = tail + int(np.argmax(score[tail:])) if n else 0
    result = []
    while t >= 0:
        result.append(t)
        t = back[t]
    return np.array(result[::-1]) / fps


# Harmony


def chroma(x: np.ndarray, sr: int, hop: int = 1024, n_fft: int = 8192) -> tuple[np.ndarray, float]:
    """Pitch-class energy per frame (frames x 12), and frames per second."""
    mag = stft_magnitude(x, n_fft, hop)
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    use = (freqs >= 60) & (freqs <= 2100)
    pitch_class = np.round(12 * np.log2(freqs[use] / 440.0) + 69).astype(int) % 12
    weights = np.zeros((use.sum(), 12))
    weights[np.arange(use.sum()), pitch_class] = 1
    c = (mag[:, use] ** 2) @ weights
    return c, sr / hop


def detect_key(c: np.ndarray) -> tuple[str, int, str]:
    """Key name like "A minor", with the tonic pitch class and the mode."""
    total = c.sum(axis=0)
    if total.sum() == 0:
        return "C major", 0, "major"
    best = (-2.0, 0, "major")
    for tonic in range(12):
        for mode, profile in (("major", MAJOR_PROFILE), ("minor", MINOR_PROFILE)):
            r = np.corrcoef(total, np.roll(profile, tonic))[0, 1]
            if r > best[0]:
                best = (r, tonic, mode)
    _r, tonic, mode = best
    return f"{NOTE_NAMES[tonic]} {mode}", tonic, mode


def chord_templates() -> tuple[np.ndarray, list[str]]:
    templates, names = [], []
    for root in range(12):
        for suffix, intervals in (("", (0, 4, 7)), ("m", (0, 3, 7))):
            t = np.zeros(12)
            for i in intervals:
                t[(root + i) % 12] = 1
            t[root] += 0.5  # The root usually sounds strongest
            templates.append(t / np.linalg.norm(t))
            names.append(NOTE_NAMES[root] + suffix)
    return np.array(templates), names


def detect_chords(c: np.ndarray, fps: float, beat_times: np.ndarray, beats_per_chord: int = 2) -> list[list]:
    """[[start, end, name], ...] with one chord per group of beats."""
    templates, names = chord_templates()
    if len(beat_times) < 2:
        return []
    bounds = list(beat_times[::beats_per_chord])
    if bounds[-1] < beat_times[-1]:
        bounds.append(float(beat_times[-1]))
    loudness = np.sqrt(c.sum(axis=1))
    quiet = np.percentile(loudness, 95) * 0.1 if len(loudness) else 0
    labels = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        i, j = int(a * fps), max(int(a * fps) + 1, int(b * fps))
        seg = c[i:j]
        if not len(seg) or np.sqrt(seg.sum(axis=1)).mean() < quiet:
            labels.append("N")
            continue
        v = seg.mean(axis=0)
        v = v / (np.linalg.norm(v) + 1e-12)
        labels.append(names[int(np.argmax(templates @ v))])
    # Remove one-segment blips
    for k in range(1, len(labels) - 1):
        if labels[k - 1] == labels[k + 1] != labels[k]:
            labels[k] = labels[k - 1]
    chords: list[list] = []
    for (a, b), name in zip(zip(bounds[:-1], bounds[1:]), labels):
        if chords and chords[-1][2] == name:
            chords[-1][1] = float(b)
        else:
            chords.append([float(a), float(b), name])
    return chords


def chord_pitches(name: str) -> list[int]:
    """MIDI notes for a chord name like "F#m", voiced around middle C."""
    if name == "N":
        return []
    minor = name.endswith("m")
    root = NOTE_NAMES.index(name[:-1] if minor else name)
    base = 48 + root
    return [base, base + (3 if minor else 4), base + 7]


# Song sections from lyrics


def split_lines(lines: list[dict], max_words: int = 12) -> list[dict]:
    """Split long transcribed chunks into lyric lines at punctuation, sharing out the time."""
    result = []
    for line in lines:
        parts = [p.strip() for p in re.split(r"(?<=[,.!?;])\s+", line["text"]) if p.strip()]
        # Long parts without punctuation are split by word count
        pieces = []
        for part in parts:
            words = part.split()
            for i in range(0, len(words), max_words):
                pieces.append(" ".join(words[i:i + max_words]))
        total = sum(len(p) for p in pieces) or 1
        start = line["start"]
        span = line["end"] - line["start"]
        for piece in pieces:
            end = start + span * len(piece) / total
            result.append({"start": start, "end": end, "text": piece.rstrip(",;")})
            start = end
    return result


def drop_repeats(lines: list[dict], keep: int = 3) -> list[dict]:
    """Whisper can get stuck on held notes and ad-libs, writing the same line hundreds of
    times. Songs do repeat lines, so up to `keep` identical lines in a row stay; longer runs
    are cut to that, with the last kept line covering the rest of the run's time."""
    result: list[dict] = []
    run = 0
    for line in lines:
        same = bool(result) and _normal(line["text"]) == _normal(result[-1]["text"])
        run = run + 1 if same else 1
        if run <= keep:
            result.append(dict(line))
        else:
            result[-1]["end"] = line["end"]
    return result


def _normal(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def sections_from_lines(lines: list[dict], duration: float, gap: float = 2.5) -> list[dict]:
    """Group timed lyric lines into sections, and label repeated blocks as choruses.

    lines: [{"start", "end", "text"}]. Returns [{"name", "start", "end", "lines"}].
    """
    blocks: list[list[dict]] = []
    for line in lines:
        if blocks and line["start"] - blocks[-1][-1]["end"] <= gap:
            blocks[-1].append(line)
        else:
            blocks.append([line])
    texts = [_normal(" ".join(l["text"] for l in b)) for b in blocks]
    repeated = [False] * len(blocks)
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            if texts[i] and difflib.SequenceMatcher(None, texts[i], texts[j]).ratio() > 0.6:
                repeated[i] = repeated[j] = True
    sections: list[dict] = []
    if blocks and blocks[0][0]["start"] > 4:
        sections.append({"name": "Intro", "start": 0.0, "end": blocks[0][0]["start"], "lines": []})
    verse = 0
    for k, block in enumerate(blocks):
        start, end = block[0]["start"], block[-1]["end"]
        if sections and start - sections[-1]["end"] > 8:
            sections.append({"name": "Instrumental break", "start": sections[-1]["end"], "end": start, "lines": []})
        if repeated[k]:
            name = "Chorus"
        else:
            verse += 1
            name = f"Verse {verse}"
        sections.append({"name": name, "start": start, "end": end, "lines": [l["text"] for l in block]})
    last = sections[-1]["end"] if sections else 0.0
    if duration - last > 4:
        sections.append({"name": "Outro" if sections else "Instrumental", "start": last, "end": duration, "lines": []})
    # Close small gaps so sections cover the whole song
    for a, b in zip(sections[:-1], sections[1:]):
        a["end"] = b["start"]
    if sections:
        sections[0]["start"] = 0.0
        sections[-1]["end"] = duration
    return sections


def lyrics_text(sections: list[dict]) -> str:
    parts = []
    for s in sections:
        parts.append(f"[{s['name']}]")
        parts.extend(s["lines"])
        parts.append("")
    return "\n".join(parts).strip() + "\n"
