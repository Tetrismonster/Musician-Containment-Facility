"""MiniMax Music 3 adapter, through the diffusers modular pipeline.

An 8B language model writes 25 audio frames per second, which dominates the
time, then a 2.4B flow-matching transformer and a vocoder produce 44.1 kHz audio.

- gpu: everything on the graphics card (about 24 GB).
- offload: the language model's layers are streamed to the graphics card one
  at a time, and other parts move on and off as needed. Slow, but it fits
  small cards; system memory holds the weights.
- cpu: everything on the processor. Very slow.
"""

import logging
import os
import time
from pathlib import Path

from mcf_worker import protocol
from mcf_worker.ace_engine import WorkerError, vram_gb

log = logging.getLogger("mcf_worker.minimax")


class MinimaxEngine:
    def __init__(self, job: dict):
        self.job = job
        self.root = Path(job["model_dir"])
        self.mode = job.get("performance", {}).get("mode", "auto")
        self.pipe = None

    def load(self, use_planner: bool = True, lora: bytes | None = None, lora_scale: float = 1.0) -> None:
        protocol.stage("load")
        protocol.progress(0.0)
        if lora:
            raise WorkerError("profile_mismatch", "MiniMax Music 3 can't use artist profiles.")
        if not (self.root / "modular_model_index.json").exists():
            raise WorkerError("model_missing", "Model files are missing: modular_model_index.json")
        import torch
        from diffusers import ComponentsManager, ModularPipeline

        mode = self.mode
        cuda = torch.cuda.is_available()
        if mode == "auto":
            if not cuda:
                mode = "cpu"
            else:
                vram = vram_gb(torch)
                mode = "gpu" if vram >= 24 else "offload"
        if mode != "cpu" and not cuda:
            raise WorkerError("no_gpu", "The graphics card can't be used by the AI software.")
        bf16 = cuda and torch.cuda.get_device_capability(0)[0] >= 8
        dtype = torch.bfloat16 if (bf16 or mode == "cpu") else torch.float16
        log.info("Loading MiniMax Music 3: mode=%s dtype=%s", mode, dtype)
        local = str(self.root)
        if mode == "offload":
            from diffusers.hooks import apply_group_offloading
            manager = ComponentsManager()
            manager.enable_auto_cpu_offload(device="cuda")
            pipe = ModularPipeline.from_pretrained(local, components_manager=manager)
            pipe.load_components(dtype=dtype, pretrained_model_name_or_path=local)
            apply_group_offloading(pipe.language_model, onload_device=torch.device("cuda"),
                                   offload_type="leaf_level", use_stream=True)
        else:
            pipe = ModularPipeline.from_pretrained(local)
            pipe.load_components(dtype=dtype, pretrained_model_name_or_path=local)
            if mode == "cpu":
                torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
            pipe.to("cuda" if mode == "gpu" else "cpu")
        self.pipe = pipe
        self.device = "cpu" if mode == "cpu" else "cuda"
        self.mode_used = mode
        protocol.progress(1.0)

    def generate(self, spec: dict, out_dir: Path) -> dict:
        import soundfile as sf
        import torch

        seed = int(spec.get("seed", -1))
        if seed < 0:
            seed = int.from_bytes(os.urandom(4), "little") & 0x7FFFFFFF
        parts = [spec.get("caption", "").strip() or "Pop song with a clear lead vocal."]
        if spec.get("bpm"):
            parts.append(f"BPM: {spec['bpm']}.")
        if spec.get("key"):
            parts.append(f"Key: {spec['key']}.")
        prompt = " ".join(parts)
        lyrics = spec.get("lyrics", "").strip()
        if not lyrics or spec.get("instrumental"):
            lyrics = "[instrumental]"
        # Structure tags must be lowercase and alone on their line
        lines = []
        for line in lyrics.splitlines():
            stripped = line.strip()
            lines.append(stripped.lower() if stripped.startswith("[") and stripped.endswith("]") else line)
        duration = min(300.0, float(spec.get("duration", 120)))
        started = time.monotonic()
        protocol.stage("generate")
        generator = torch.Generator(self.device).manual_seed(seed)
        with torch.no_grad():
            audio = self.pipe(prompt=prompt, lyrics="\n".join(lines), audio_duration=duration,
                              generator=generator, output="audios")[0]
        if hasattr(audio, "cpu"):
            audio = audio.float().cpu().numpy()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "song.flac"
        sf.write(str(path), audio.T, int(self.pipe.sampling_rate), subtype="PCM_24")
        protocol.progress(1.0)
        return {"file": str(path), "seed": seed, "seconds": time.monotonic() - started,
                "caption_used": prompt, "mode_used": self.mode_used}
