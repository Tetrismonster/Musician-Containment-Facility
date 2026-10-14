"""Train an artist profile's add-on (LoRA) with ACE-Step's trainer.

Every intermediate file (the dataset description, preprocessed tensors, logs
and checkpoints) lives in this job's temp folder, which MCF deletes when the
job ends. The training audio is read where it is and never copied.
"""

import json
import logging
import re
import subprocess
import sys
from pathlib import Path

from mcf_worker import protocol

log = logging.getLogger("mcf_worker.training")

EPOCH_RE = re.compile(r"Epoch (\d+)/(\d+)")
PREPROCESS_RE = re.compile(r"\[(\d+)/(\d+)\]")


class TrainingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _run(args: list[str], cwd: Path, on_line) -> None:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen(args, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               creationflags=flags)
    tail: list[str] = []
    for raw in process.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip()
        print(line, file=sys.stderr)
        tail = (tail + [line])[-40:]
        on_line(line)
    if process.wait() != 0:
        text = "\n".join(tail)
        code = "out_of_memory" if "out of memory" in text.lower() else "training_failed"
        raise TrainingError(code, text[-2000:])


def run(job: dict, work_dir: Path) -> dict:
    spec = job["train"]
    train_py = Path(job["engine_source"]) / "train.py"
    checkpoints = Path(job["model_dir"]) / "checkpoints"
    samples = []
    for item in spec["files"]:
        lyrics = item.get("lyrics", "").strip()
        samples.append({
            "audio_path": item["path"],
            "filename": Path(item["path"]).name,
            "caption": item.get("caption") or spec.get("style", ""),
            "lyrics": lyrics or "[Instrumental]",
            "language": item.get("language", "en"),
            "is_instrumental": not lyrics,
            "custom_tag": spec.get("trigger", ""),
        })
    dataset = work_dir / "dataset.json"
    dataset.write_text(json.dumps({"metadata": {"name": "mcf-profile", "custom_tag": spec.get("trigger", ""),
                                                "tag_position": "prepend"}, "samples": samples}), encoding="utf-8")
    audio_dir = work_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    tensors = work_dir / "tensors"
    output = work_dir / "output"
    common = [sys.executable, str(train_py), "fixed", "--checkpoint-dir", str(checkpoints),
              "--model-variant", "turbo", "--device", spec.get("device", "auto"),
              "--precision", spec.get("precision", "auto")]

    protocol.stage("preprocess")
    protocol.progress(0.0)

    def preprocess_line(line: str):
        match = PREPROCESS_RE.search(line)
        if match:
            done, total = int(match[1]), int(match[2])
            protocol.progress(done / max(1, total))

    _run(common + ["--preprocess", "--audio-dir", str(audio_dir), "--dataset-json", str(dataset),
                   "--tensor-output", str(tensors), "--max-duration", str(spec.get("max_duration", 240))],
         work_dir, preprocess_line)
    if not any(tensors.glob("*.pt")):
        raise TrainingError("training_failed", "Preprocessing produced no training data.")

    protocol.stage("train")
    protocol.progress(0.0)

    def train_line(line: str):
        match = EPOCH_RE.search(line)
        if match:
            protocol.progress(int(match[1]) / max(1, int(match[2])), f"epoch {match[1]}")

    args = common + ["--dataset-dir", str(tensors), "--output-dir", str(output),
                     "--epochs", str(spec["epochs"]), "--rank", str(spec["rank"]), "--alpha", str(spec["rank"] * 2),
                     "--optimizer-type", spec.get("optimizer", "adamw"), "--save-every", str(spec["epochs"] + 1),
                     "--log-dir", str(work_dir / "logs")]
    if spec.get("offload_encoder"):
        args.append("--offload-encoder")
    _run(args, work_dir, train_line)
    final = output / "final"
    if not (final / "adapter_model.safetensors").exists():
        raise TrainingError("training_failed", "Training finished without saving the add-on.")
    return {"adapter_dir": str(final)}
