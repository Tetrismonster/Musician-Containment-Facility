"""YuE2 3B adapter.

YuE2 writes a score (ABC notation) for the melody and chords, then performs
it. For covers, MCF writes the score itself from its analysis, so only the
melody, chords, tempo and section names reach the model.

YuE2 needs bfloat16 on the graphics card, which NVIDIA cards have from the
RTX 30 series. On older cards and without a graphics card it runs on the
processor, where MCF loads it in full precision because most processors are
very slow at bfloat16.
"""

import logging
import os
import time
from pathlib import Path

from mcf_worker import protocol
from mcf_worker.ace_engine import WorkerError, vram_gb

log = logging.getLogger("mcf_worker.yue")

RATE = 48000
# Semantic tokens per second of audio, for the progress estimate
TOKENS_PER_SECOND = 25


class YueEngine:
    def __init__(self, job: dict):
        self.job = job
        self.root = Path(job["model_dir"]) / "models"
        self.mode = job.get("performance", {}).get("mode", "auto")
        self.pipe = None

    def load(self, use_planner: bool = True, lora: bytes | None = None, lora_scale: float = 1.0) -> None:
        protocol.stage("load")
        protocol.progress(0.0)
        if lora:
            raise WorkerError("profile_mismatch", "YuE2 can't use artist profiles.")
        for name in ("YuE2-3B/model.safetensors", "YuE2-3B/qwen.tiktoken", "YuE2-Vae/model.safetensors"):
            if not (self.root / name).exists():
                raise WorkerError("model_missing", f"Model files are missing: {name}")
        import torch
        import yue2.pipeline as pipeline_module
        from yue2.pipeline import YuE2Pipeline

        mode = self.mode
        cuda_ok = torch.cuda.is_available() and torch.cuda.get_device_capability(0)[0] >= 8
        if mode == "auto":
            mode = "gpu" if cuda_ok else "cpu"
        if mode in ("gpu", "offload") and not cuda_ok:
            raise WorkerError("no_gpu", "YuE2 needs an NVIDIA RTX 30 series or newer card to use the graphics card.")
        device = "cpu" if mode == "cpu" else "cuda"
        if device == "cpu":
            # Load the language model in float32: most processors are very slow at bfloat16
            original = pipeline_module.YuE2Pipeline._load_model

            def load_float32(pipe, for_nar=False):
                if pipe._model is None:
                    from yue2.modeling_yue2 import YuE2ForCausalLM
                    pipe._model = YuE2ForCausalLM.from_pretrained(
                        pipe.model_dir, local_files_only=True, torch_dtype=torch.float32,
                        low_cpu_mem_usage=True).eval()
                return original(pipe, for_nar)

            pipeline_module.YuE2Pipeline._load_model = load_float32
            torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
        budget = 64.0
        if device == "cuda":
            budget = vram_gb(torch)
        log.info("Loading YuE2: mode=%s device=%s budget=%.1f GiB", mode, device, budget)
        # MCF has already checked every file against its fingerprint
        self.pipe = YuE2Pipeline(self.root / "YuE2-3B", self.root / "YuE2-Vae", device=device,
                                 memory_budget_gib=budget, backend="torch-eager" if device == "cpu" else "torch",
                                 verify_hashes=False, offload_ar=mode == "offload", progress=False)
        self.pipe._load_model()
        self.mode_used = mode
        protocol.progress(1.0)

    def generate(self, spec: dict, out_dir: Path) -> dict:
        import soundfile as sf

        from mcf_worker import abc

        seed = int(spec.get("seed", -1))
        if seed < 0:
            seed = int.from_bytes(os.urandom(4), "little") & 0x7FFFFFFF
        request = {"style": spec.get("caption", "").strip() or "pop song", "lyrics": spec.get("lyrics", "").strip(),
                   "seed": seed, "id": "song", "cot": "full"}
        if not request["lyrics"]:
            request["lyrics"] = "[Instrumental]"
        if spec.get("mode") == "cover":
            cover = spec["cover"]
            follow = cover.get("follow", {})
            request["abc"] = abc.build(cover["analysis"], follow, bpm=cover.get("bpm"),
                                       transpose=int(cover.get("transpose", 0)), sections=cover.get("sections"),
                                       max_seconds=cover.get("max_seconds"))
            # With the chords followed, the score has chord names and YuE2 keeps the harmony.
            # Without them, YuE2's melody mode writes its own accompaniment.
            request["cot"] = "full" if follow.get("chords", True) else "melody"
        pipe = self.pipe
        started = time.monotonic()
        counts = {"abc": 0, "semantic": 0}
        expected = {"abc": 2000, "semantic": TOKENS_PER_SECOND * float(spec.get("duration", 180))}

        def on_token(phase, _token):
            counts[phase] = counts.get(phase, 0) + 1
            if counts[phase] % 50 == 0:
                protocol.progress(min(0.95, counts[phase] / max(1, expected.get(phase, 3000))))

        protocol.stage("plan")
        req = pipe._request(**request)
        plan = pipe.plan(request=req, on_token=on_token)
        protocol.stage("generate")
        semantic = pipe.generate_semantic(plan, on_token=on_token)
        protocol.stage("decode")
        protocol.progress(0.0)
        latents = pipe.synthesize(semantic)
        audio = pipe.decode(latents)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "song.flac"
        sf.write(str(path), audio, RATE, subtype="PCM_24")
        protocol.progress(1.0)
        return {"file": str(path), "seed": seed, "seconds": time.monotonic() - started,
                "caption_used": request["style"], "mode_used": self.mode_used,
                "truncated": bool(plan.truncated or semantic.truncated)}
