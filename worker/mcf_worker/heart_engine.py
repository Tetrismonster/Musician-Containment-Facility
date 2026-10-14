"""HeartMuLa 3B adapter.

HeartMuLa is a language model that writes audio tokens frame by frame (one
frame is 80 ms), then a codec turns the tokens into 48 kHz audio. MCF picks
how to load the language model from the performance mode:

- gpu: the whole model on the graphics card in half precision.
- compressed: 8-bit weights on the graphics card (bitsandbytes).
- offload: layers split between the graphics card and system memory.
- cpu: everything on the processor in full precision.
"""

import logging
import time
from pathlib import Path

from mcf_worker import protocol
from mcf_worker.ace_engine import WorkerError, vram_gb

log = logging.getLogger("mcf_worker.heart")

FRAME_MS = 80
RATE = 48000


class HeartEngine:
    def __init__(self, job: dict):
        self.job = job
        self.ckpt = Path(job["model_dir"]) / "ckpt"
        self.mode = job.get("performance", {}).get("mode", "auto")
        self.pipe = None

    def _pick_mode(self, torch) -> str:
        if self.mode != "auto":
            return self.mode
        if not torch.cuda.is_available():
            return "cpu"
        vram = vram_gb(torch)
        if vram >= 12:
            return "gpu"
        return "compressed" if vram >= 6 else "offload"

    def load(self, use_planner: bool = True, lora: bytes | None = None, lora_scale: float = 1.0) -> None:
        protocol.stage("load")
        protocol.progress(0.0)
        if lora:
            raise WorkerError("profile_mismatch", "HeartMuLa can't use artist profiles.")
        for name in ("gen_config.json", "tokenizer.json", "HeartMuLa-oss-3B/config.json", "HeartCodec-oss/config.json"):
            if not (self.ckpt / name).exists():
                raise WorkerError("model_missing", f"Model files are missing: {name}")
        import torch
        from heartlib.heartcodec.modeling_heartcodec import HeartCodec
        from heartlib.heartmula.modeling_heartmula import HeartMuLa
        from heartlib.pipelines.music_generation import HeartMuLaGenPipeline

        mode = self._pick_mode(torch)
        if mode != "cpu" and not torch.cuda.is_available():
            raise WorkerError("no_gpu", "The graphics card can't be used by the AI software.")
        self.device = torch.device("cpu" if mode == "cpu" else "cuda")
        bf16 = self.device.type == "cuda" and torch.cuda.get_device_capability(0)[0] >= 8
        half = torch.bfloat16 if bf16 else torch.float16
        self.mula_dtype = torch.float32 if mode == "cpu" else half
        log.info("Loading HeartMuLa: mode=%s dtype=%s", mode, self.mula_dtype)

        pipe = HeartMuLaGenPipeline.from_pretrained(
            str(self.ckpt), device={"mula": self.device, "codec": self.device},
            dtype={"mula": self.mula_dtype, "codec": torch.float32}, version="3B", lazy_load=True)
        mula_path = str(self.ckpt / "HeartMuLa-oss-3B")
        kwargs = {"dtype": self.mula_dtype, "low_cpu_mem_usage": True}
        if mode == "gpu":
            kwargs["device_map"] = {"": 0}
        elif mode == "compressed":
            from transformers import BitsAndBytesConfig
            vram = vram_gb(torch)
            if vram >= 10:
                kwargs["quantization_config"] = BitsAndBytesConfig(load_in_8bit=True)
            else:
                # 4-bit fits small cards and generates faster than 8-bit, at slightly lower quality
                kwargs["quantization_config"] = BitsAndBytesConfig(
                    load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=half)
            kwargs["device_map"] = {"": 0}
        elif mode == "offload":
            vram = vram_gb(torch)
            kwargs["device_map"] = "auto"
            kwargs["max_memory"] = {0: f"{max(1.0, vram - 2.5):.1f}GiB", "cpu": "64GiB"}
        else:
            kwargs["device_map"] = {"": "cpu"}
        protocol.progress(0.2)
        pipe._mula = HeartMuLa.from_pretrained(mula_path, **kwargs)
        pipe.mula_dtype = self.mula_dtype
        self.pipe = pipe
        self.mode_used = mode
        self._codec_class = HeartCodec
        protocol.progress(1.0)

    def _decode(self, frames, torch):
        """Codec tokens to audio. On small cards the codec runs in half precision, and falls back to the
        processor if that gives broken audio."""
        choices = []
        if self.device.type == "cuda":
            vram = vram_gb(torch)
            bf16 = torch.cuda.get_device_capability(0)[0] >= 8
            if vram >= 10:
                choices.append((torch.device("cuda"), torch.float32))
            else:
                choices.append((torch.device("cuda"), torch.bfloat16 if bf16 else torch.float16))
        choices.append((torch.device("cpu"), torch.float32))
        for device, dtype in choices:
            codec = self._codec_class.from_pretrained(str(self.ckpt / "HeartCodec-oss"), device_map=device,
                                                      dtype=dtype)
            with torch.no_grad():
                wav = codec.detokenize(frames.to(device))
            audio = wav.to(torch.float32).cpu().numpy()
            del codec
            if device.type == "cuda":
                torch.cuda.empty_cache()
            if not __import__("numpy").isfinite(audio).all():
                log.warning("Decoding in %s on %s gave broken audio; trying the next option", dtype, device)
                continue
            log.info("Decoded with %s on %s", dtype, device)
            return audio
        raise WorkerError("generation_failed", "The audio decoder produced broken audio.")

    def generate(self, spec: dict, out_dir: Path) -> dict:
        import gc

        import soundfile as sf
        import torch
        import heartlib.pipelines.music_generation as mg

        pipe = self.pipe
        seed = int(spec.get("seed", -1))
        if seed < 0:
            seed = int.from_bytes(__import__("os").urandom(4), "little") & 0x7FFFFFFF
        torch.manual_seed(seed)
        duration = float(spec.get("duration", 120))
        frames_total = int(duration * 1000 // FRAME_MS)

        def progress_tqdm(iterable, *args, **kwargs):
            for index, item in enumerate(iterable):
                if index % 25 == 0:
                    protocol.progress(index / max(1, frames_total))
                yield item

        mg.tqdm = progress_tqdm
        lyrics = spec.get("lyrics", "").strip() or "[instrumental]"
        tags = spec.get("caption", "").strip() or "pop"
        cfg = float(spec.get("guidance", 1.5))
        started = time.monotonic()
        protocol.stage("generate")
        with torch.no_grad():
            inputs = pipe.preprocess({"lyrics": lyrics, "tags": tags}, cfg_scale=cfg)
            outputs = pipe._forward(inputs, max_audio_length_ms=int(duration * 1000), temperature=1.0,
                                    topk=50, cfg_scale=cfg)
        frames = outputs["frames"]
        # Free the language model before loading the codec, so both never share the card
        pipe._mula = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        protocol.stage("decode")
        protocol.progress(0.0)
        audio = self._decode(frames, torch)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "song.flac"
        sf.write(str(path), audio.T, RATE, subtype="PCM_24")
        protocol.progress(1.0)
        return {"file": str(path), "seed": seed, "seconds": time.monotonic() - started,
                "caption_used": tags, "mode_used": self.mode_used}
