"""ACE-Step 1.5 adapter: loading the model, and every generation mode."""

import logging
import os
import time
from pathlib import Path

from mcf_worker import protocol

log = logging.getLogger("mcf_worker.ace")

CORE_DIRS = ("acestep-v15-turbo", "vae", "Qwen3-Embedding-0.6B")
DIT = "acestep-v15-turbo"


def vram_gb(torch) -> float:
    """The graphics card's memory in GB. Cards sold as 6 GB report a few kilobytes less
    (5.9996 GB, for example), so the figure is rounded before comparing it with limits."""
    return round(torch.cuda.get_device_properties(0).total_memory / 1024**3, 1)


class WorkerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _check_files(checkpoints: Path) -> None:
    for name in CORE_DIRS:
        folder = checkpoints / name
        if not folder.is_dir() or not any(folder.glob("*.safetensors")):
            raise WorkerError("model_missing", f"Model files are missing: {folder}")


class AceEngine:
    def __init__(self, job: dict):
        self.job = job
        self.model_dir = Path(job["model_dir"])
        self.checkpoints = self.model_dir / "checkpoints"
        perf = job.get("performance", {})
        self.mode = perf.get("mode", "auto")
        self.planner = perf.get("planner")
        self.dit = None
        self.llm = None
        self.lora_loaded = False

    # Loading

    def load(self, use_planner: bool = True, lora: bytes | None = None, lora_scale: float = 1.0) -> None:
        """Load the model. A profile's add-on is merged before any 8-bit compression."""
        protocol.stage("load")
        protocol.progress(0.0)
        _check_files(self.checkpoints)
        import torch
        from acestep import gpu_config as gc
        from acestep.handler import AceStepHandler
        from acestep.llm_inference import LLMHandler

        config = gc.get_gpu_config()
        gc.set_global_gpu_config(config)
        cuda = torch.cuda.is_available()
        if self.mode != "cpu" and not cuda:
            raise WorkerError("no_gpu", "The graphics card can't be used by the AI software.")

        device = "cpu" if self.mode == "cpu" else "cuda"
        offload = offload_dit = False
        quantization = None
        if self.mode == "auto":
            offload = config.offload_to_cpu_default
            offload_dit = config.offload_dit_to_cpu_default
            quantization = "int8_weight_only" if config.quantization_default else None
        elif self.mode == "offload":
            offload = offload_dit = True
        elif self.mode == "compressed":
            offload = offload_dit = True
            quantization = "int8_weight_only"
        log.info("Loading ACE-Step: device=%s offload=%s dit_offload=%s quantization=%s tier=%s",
                 device, offload, offload_dit, quantization, config.tier)

        dit = AceStepHandler()
        # MCF downloads and verifies model files itself. Never let the engine
        # download anything on its own.
        dit._ensure_models_present = lambda **_kwargs: None
        self.pre_ampere = device == "cuda" and torch.cuda.get_device_capability(0)[0] < 8
        if self.pre_ampere:
            # Cards older than the RTX 30 series produce NaN audio in float16,
            # and don't support bfloat16. Use float32; INT8 weights keep it small.
            load_main = dit._load_main_model_from_checkpoint

            def load_main_float32(**kwargs):
                dit.dtype = torch.float32
                return load_main(**kwargs)

            dit._load_main_model_from_checkpoint = load_main_float32
            if quantization is None and self.mode != "gpu":
                quantization = "int8_weight_only"
            log.info("Pre-Ampere GPU: using float32")
        protocol.progress(0.1)
        # ACE-Step can't add a LoRA to a compressed model, so compress after merging
        load_quantization = None if lora else quantization
        message, ok = dit.initialize_service(
            project_root=str(self.model_dir), config_path=DIT, device=device,
            use_flash_attention=False, compile_model=False,
            offload_to_cpu=offload, offload_dit_to_cpu=offload_dit, quantization=load_quantization,
        )
        if not ok:
            raise WorkerError("load_failed", message)
        self.dit = dit
        if lora:
            from mcf_worker import lora_memory
            try:
                dit.model.decoder = lora_memory.merge(dit.model.decoder, lora, lora_scale)
            except ValueError as exc:
                raise WorkerError("profile_mismatch", str(exc))
            self.lora_loaded = True
            if quantization:
                dit._apply_dit_quantization(quantization)
                dit.quantization = quantization
        protocol.progress(0.7)

        self.llm = LLMHandler()
        if use_planner and self.planner and (self.checkpoints / self.planner).is_dir():
            backend = gc.resolve_lm_backend("vllm", config) if hasattr(gc, "resolve_lm_backend") else "pt"
            if device == "cpu":
                backend = "pt"
            message, ok = self.llm.initialize(
                checkpoint_dir=str(self.checkpoints), lm_model_path=self.planner, backend=backend,
                device=device, offload_to_cpu=offload,
                dtype=torch.float32 if self.pre_ampere or device == "cpu" else None,
            )
            if not ok:
                # The planner is optional: carry on without it, and say so
                log.warning("Planner failed to load: %s", message)
                protocol.send("notice", code="planner_failed", detail=message)
                self.llm = LLMHandler()
        protocol.progress(1.0)

    # Generating

    def generate(self, spec: dict, out_dir: Path) -> dict:
        from acestep.inference import GenerationConfig, GenerationParams, generate_music

        mode = spec.get("mode", "new")
        task = {"new": "text2music", "cover": "cover", "variation": "cover", "extend": "repaint",
                "inpaint": "repaint", "reference": "text2music"}[mode]
        lyrics = spec.get("lyrics", "").strip()
        instrumental = bool(spec.get("instrumental")) or not lyrics
        kwargs = dict(
            task_type=task,
            caption=spec.get("caption", "")[:512],
            lyrics="[Instrumental]" if instrumental and not lyrics else lyrics[:4096],
            instrumental=instrumental,
            duration=float(spec.get("duration", 120)),
            bpm=spec.get("bpm") or None,
            keyscale=spec.get("key") or "",
            timesignature=str(spec.get("time_signature") or ""),
            vocal_language=spec.get("language") or "unknown",
            seed=int(spec.get("seed", -1)),
            inference_steps=int(spec.get("steps", 8)),
            guidance_scale=float(spec.get("guidance", 7.0)),
            shift=3.0,
            thinking=bool(self.llm and self.llm.llm_initialized) and task == "text2music",
        )
        if not kwargs["thinking"]:
            kwargs.update(use_cot_metas=False, use_cot_caption=False, use_cot_language=False)
        if mode in ("cover", "variation"):
            kwargs["src_audio"] = spec["src_audio"]
            kwargs["audio_cover_strength"] = float(spec.get("cover_strength", 0.8))
            # Above 0, generation starts from the noised source instead of pure noise
            kwargs["cover_noise_strength"] = float(spec.get("cover_noise", 0.0))
        elif mode == "extend":
            source_seconds = float(spec["source_seconds"])
            kwargs["src_audio"] = spec["src_audio"]
            kwargs["repainting_start"] = source_seconds
            kwargs["repainting_end"] = source_seconds + float(spec.get("extend_seconds", 30))
            kwargs["duration"] = kwargs["repainting_end"]
        elif mode == "inpaint":
            kwargs["src_audio"] = spec["src_audio"]
            kwargs["repainting_start"] = float(spec["repaint_start"])
            kwargs["repainting_end"] = float(spec["repaint_end"])
        elif mode == "reference":
            kwargs["reference_audio"] = spec["reference_audio"]
        if mode in ("cover", "variation", "extend", "inpaint"):
            kwargs.pop("duration")  # Comes from the source audio

        params = GenerationParams(**kwargs)
        config = GenerationConfig(batch_size=1, use_random_seed=kwargs["seed"] < 0,
                                  seeds=[kwargs["seed"]] if kwargs["seed"] >= 0 else None,
                                  audio_format="flac")

        stage = {"name": ""}

        def on_progress(value, desc=None, **_kwargs):
            try:
                value = float(value)
            except (TypeError, ValueError):
                return
            name = "plan" if value < 0.5 else ("decode" if value >= 0.8 else "generate")
            if name != stage["name"]:
                stage["name"] = name
                protocol.stage(name)
            protocol.progress(value, desc or "")

        started = time.monotonic()
        result = generate_music(self.dit, self.llm, params, config, save_dir=str(out_dir), progress=on_progress)
        if not result.success or not result.audios:
            raise WorkerError("generation_failed", result.error or result.status_message or "Generation failed.")
        audio = result.audios[0]
        used = audio.get("params", {})
        return {
            "file": audio["path"],
            "seed": used.get("seed"),
            "bpm": used.get("bpm") or used.get("cot_bpm"),
            "key": used.get("keyscale") or used.get("cot_keyscale"),
            "caption_used": used.get("cot_caption") or kwargs["caption"],
            "seconds": time.monotonic() - started,
        }
