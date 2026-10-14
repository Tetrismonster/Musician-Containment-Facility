"""MCF worker: runs one job in the AI runtime, then exits.

Started hidden by MCF as:  python -m mcf_worker JOB.json
MCF ends this process to cancel, so nothing here needs to handle cancelling.
"""

import json
import logging
import os
import sys
import traceback
from pathlib import Path

from mcf_worker import protocol


def _setup_logging() -> None:
    # Library output goes to stderr, which MCF writes to its log
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        from loguru import logger
        logger.remove()
        logger.add(sys.stderr, level="INFO", filter=lambda r: "<|audio_code_" not in r["message"])
    except ImportError:
        pass
    os.environ.setdefault("TQDM_DISABLE", "1")


def _read_stdin_bytes(count: int) -> bytes:
    data = bytearray()
    stream = sys.stdin.buffer
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            break
        data += chunk
    return bytes(data)


ENGINES = {
    "ace-step-1.5": ("mcf_worker.ace_engine", "AceEngine"),
    "heartmula": ("mcf_worker.heart_engine", "HeartEngine"),
    "yue2": ("mcf_worker.yue_engine", "YueEngine"),
    "mulacover": ("mcf_worker.mulacover_engine", "MuLaCoverEngine"),
    "minimax-music3": ("mcf_worker.minimax_engine", "MinimaxEngine"),
}


def _engine_class(engine_id: str):
    import importlib
    module, name = ENGINES[engine_id]
    return getattr(importlib.import_module(module), name)


def _selftest(job: dict) -> None:
    import torch
    info = {"torch": torch.__version__, "cuda": torch.cuda.is_available()}
    if info["cuda"]:
        info["device"] = torch.cuda.get_device_name(0)
        info["vram_gb"] = round(torch.cuda.get_device_properties(0).total_memory / 1024**3, 1)
        # A tiny calculation proves the card really works, not just that it's visible
        x = torch.ones(256, 256, device="cuda")
        info["ok"] = float((x @ x).sum().item()) == 256.0 ** 3
    else:
        info["ok"] = True
    protocol.result(**info)


def main() -> int:
    _setup_logging()
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    work_dir = Path(job["work_dir"])
    task = job["task"]
    try:
        if task == "selftest":
            _selftest(job)
        elif task == "generate":
            spec = job["generate"]
            engine_id = job.get("engine", "ace-step-1.5")
            lora = _read_stdin_bytes(job["lora_bytes"]) if job.get("lora_bytes") else None
            if spec.get("mode") == "cover" and engine_id == "ace-step-1.5":
                import torch
                from mcf_worker import cover_source
                protocol.stage("analyse")
                cover = dict(spec["cover"], tools_dir=job["cover_tools_dir"])
                cpu = job.get("performance", {}).get("mode") == "cpu" or not torch.cuda.is_available()
                spec["src_audio"] = str(cover_source.prepare(cover, Path(spec["source"]), work_dir,
                                                             "cpu" if cpu else "cuda"))
                spec["cover_strength"] = cover_source.strength(cover.get("follow", {}))
            engine = _engine_class(engine_id)(job)
            # The song planner only helps new songs; covers and edits follow existing audio
            engine.load(use_planner=spec.get("mode", "new") in ("new", "reference"),
                        lora=lora, lora_scale=float(job.get("lora_scale", 1.0)))
            del lora
            protocol.result(**engine.generate(spec, work_dir / "out"))
        elif task == "analyse":
            from mcf_worker import analysis
            protocol.result(**analysis.run(job["analyse"], Path(job["cover_tools_dir"]), work_dir))
        elif task == "licenses":
            from mcf_worker import credits
            protocol.result(count=credits.write(Path(job["output"]), job.get("engine_name", "")))
        elif task == "train":
            from mcf_worker import training
            protocol.result(**training.run(job, work_dir))
        else:
            protocol.error("bad_job", f"Unknown task {task}")
            return 2
    except Exception as exc:  # Report every failure to MCF instead of dying silently
        code = getattr(exc, "code", "")
        detail = traceback.format_exc()
        print(detail, file=sys.stderr)
        text = str(exc)
        if not code:
            lowered = text.lower()
            if "out of memory" in lowered:
                code = "out_of_memory"
            elif "no space left" in lowered or "errno 28" in lowered:
                code = "disk_full"
            else:
                code = "failed"
        protocol.error(code, text, detail)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
