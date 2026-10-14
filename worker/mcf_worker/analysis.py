"""Cover analysis: separate the vocals, transcribe the lyrics, and detect the
melody, tempo, key, chords and sections of an imported song.

The separated audio only ever exists in this job's temp folder, which MCF
deletes when the job ends.
"""

import gc
import logging
from pathlib import Path

import numpy as np

from mcf_worker import dsp, protocol

log = logging.getLogger("mcf_worker.analysis")

MAX_SECONDS = 15 * 60
MAX_BYTES = 500 * 1024 * 1024
SEP_RATE = 44100
ANALYSIS_RATE = 16000


class AnalysisError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def load_audio(path: Path):
    import soundfile as sf
    if path.stat().st_size > MAX_BYTES:
        raise AnalysisError("too_large", "The file is larger than 500 MB.")
    try:
        info = sf.info(str(path))
    except Exception as exc:
        raise AnalysisError("unreadable", f"The file can't be read as audio: {exc}")
    if info.frames / info.samplerate > MAX_SECONDS:
        raise AnalysisError("too_long", "The song is longer than 15 minutes.")
    audio, rate = sf.read(str(path), dtype="float32", always_2d=True)
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    return audio[:, :2].T.copy(), rate


def resample(x, rate_in: int, rate_out: int):
    import torch
    import torchaudio.functional as F
    if rate_in == rate_out:
        return x
    return F.resample(torch.as_tensor(x), rate_in, rate_out).numpy()


def separate(audio, rate: int, weights: Path, device: str):
    """Vocals and accompaniment, using Hybrid Demucs."""
    import torch
    from torchaudio.models import hdemucs_high
    from torchaudio.transforms import Fade

    model = hdemucs_high(sources=["drums", "bass", "other", "vocals"])
    model.load_state_dict(torch.load(str(weights), map_location="cpu", weights_only=True))
    model.to(device).eval()
    mix = torch.as_tensor(resample(audio, rate, SEP_RATE))[None]
    ref = mix.mean(dim=1)
    mix = (mix - ref.mean()) / (ref.std() + 1e-8)

    segment, overlap = 10.0, 0.1
    length = mix.shape[-1]
    chunk = int(SEP_RATE * segment * (1 + overlap))
    overlap_frames = int(overlap * SEP_RATE)
    fade = Fade(fade_in_len=0, fade_out_len=overlap_frames, fade_shape="linear")
    out = torch.zeros(1, 4, 2, length)
    start, end = 0, chunk
    with torch.inference_mode():
        while start < length - overlap_frames:
            protocol.progress(0.05 + 0.45 * start / length, "separate")
            piece = model(mix[:, :, start:end].to(device)).cpu()
            piece = fade(piece)
            out[:, :, :, start:start + piece.shape[-1]] += piece
            if start == 0:
                fade.fade_in_len = overlap_frames
                start += chunk - overlap_frames
            else:
                start += chunk
            end += chunk
            if end >= length:
                fade.fade_out_len = 0
    out = out * ref.std() + ref.mean()
    vocals = out[0, 3].numpy()
    accompaniment = out[0, :3].sum(dim=0).numpy()
    del model
    _free(device)
    return vocals, accompaniment


def transcribe(vocals_16k, whisper_dir: Path, device: str, language: str | None):
    """Timed lyric lines: [{"start", "end", "text"}]."""
    import torch
    import transformers.pipelines.automatic_speech_recognition as asr_module
    from transformers import pipeline

    # MCF passes raw audio, so the FFmpeg-based decoder is never needed, and
    # FFmpeg isn't part of the runtime
    asr_module.is_torchcodec_available = lambda: False
    dtype = torch.float16 if device == "cuda" else torch.float32
    asr = pipeline("automatic-speech-recognition", model=str(whisper_dir), torch_dtype=dtype,
                   device=0 if device == "cuda" else -1)
    kwargs = {"task": "transcribe"}
    if language:
        kwargs["language"] = language
    out = asr({"raw": vocals_16k, "sampling_rate": ANALYSIS_RATE}, return_timestamps=True,
              chunk_length_s=30, batch_size=4, generate_kwargs=kwargs)
    del asr
    _free(device)
    lines = []
    rms_hop = ANALYSIS_RATE // 100
    rms = np.sqrt((dsp.frames(vocals_16k, rms_hop, rms_hop).astype(np.float64) ** 2).mean(axis=1))
    loud = np.percentile(rms, 95) if len(rms) else 0
    for chunk in out.get("chunks", []):
        start, end = chunk.get("timestamp", (None, None))
        text = (chunk.get("text") or "").strip()
        if start is None or not text:
            continue
        end = end if end is not None else start + 3
        # Whisper sometimes invents words in instrumental parts; drop lines with no singing under them
        seg = rms[int(start * 100):max(int(start * 100) + 1, int(end * 100))]
        if len(seg) and seg.mean() < loud * 0.05:
            continue
        lines.append({"start": float(start), "end": float(end), "text": text})
    return dsp.drop_repeats(dsp.split_lines(lines))


def _free(device: str) -> None:
    gc.collect()
    if device == "cuda":
        import torch
        torch.cuda.empty_cache()


def describe(job_audio: Path, model_dir: Path, planner: str | None, mode: str) -> dict:
    """Genre and instrumentation, described by ACE-Step's song planner."""
    if not planner or not (model_dir / "checkpoints" / planner).is_dir():
        return {}
    from acestep.inference import understand_music
    from mcf_worker.ace_engine import AceEngine
    engine = AceEngine({"model_dir": str(model_dir), "performance": {"mode": mode, "planner": planner}})
    engine.load()
    if not engine.llm.llm_initialized:
        return {}
    codes = engine.dit.convert_src_audio_to_codes(str(job_audio))
    if not isinstance(codes, str) or "<|audio_code_" not in codes:
        return {}
    result = understand_music(llm_handler=engine.llm, audio_codes=codes)
    return {"caption": getattr(result, "caption", "") or "",
            "planner_bpm": getattr(result, "bpm", None),
            "planner_key": getattr(result, "keyscale", "") or ""}


def run(spec: dict, tools_dir: Path, work_dir: Path) -> dict:
    import torch
    protocol.stage("analyse")
    protocol.progress(0.0, "load")
    source = Path(spec["audio"])
    audio, rate = load_audio(source)
    duration = audio.shape[1] / rate
    device = "cuda" if torch.cuda.is_available() and spec.get("mode") != "cpu" else "cpu"

    vocals, accompaniment = separate(audio, rate, tools_dir / "demucs" / "hdemucs_high_trained.pt", device)
    vocals_16k = resample(vocals.mean(axis=0), SEP_RATE, ANALYSIS_RATE)
    backing_16k = resample(accompaniment.mean(axis=0), SEP_RATE, ANALYSIS_RATE)
    mix_16k = vocals_16k + backing_16k

    protocol.progress(0.55, "lyrics")
    lines = transcribe(vocals_16k, tools_dir / "whisper-large-v3-turbo", device, spec.get("language"))

    protocol.progress(0.8, "melody")
    f0, times = dsp.yin(vocals_16k, ANALYSIS_RATE)
    notes = dsp.notes_from_f0(f0, times)

    protocol.progress(0.88, "rhythm")
    env = dsp.onset_envelope(backing_16k, ANALYSIS_RATE)
    bpm = dsp.tempo(env, 100)
    beat_times = dsp.beats(env, 100, bpm)
    chroma, fps = dsp.chroma(backing_16k, ANALYSIS_RATE)
    key, _tonic, _mode = dsp.detect_key(dsp.chroma(mix_16k, ANALYSIS_RATE)[0])
    chords = dsp.detect_chords(chroma, fps, beat_times)
    sections = dsp.sections_from_lines(lines, duration)

    result = {
        "duration": duration,
        "bpm": round(bpm, 1),
        "key": key,
        "time_signature": "4",
        "lyrics": dsp.lyrics_text(sections) if lines else "",
        "sections": sections,
        "notes": [[round(a, 3), round(b, 3), int(m)] for a, b, m in notes],
        "chords": [[round(a, 3), round(b, 3), n] for a, b, n in chords],
        "beats": [round(float(b), 3) for b in beat_times],
        "caption": "",
    }
    if spec.get("describe"):
        protocol.progress(0.92, "describe")
        try:
            described = describe(source, Path(spec["model_dir"]), spec.get("planner"), spec.get("mode", "auto"))
            result.update(described)
        except Exception:
            log.exception("Describing the song failed; continuing without a description")
    protocol.progress(1.0, "done")
    return result
