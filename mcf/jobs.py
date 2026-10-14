"""Running jobs in the hidden worker process.

Each job gets its own folder in temp. The folder is deleted when the job
finishes, fails or is cancelled, and anything left over from a crash is
deleted the next time MCF starts.
"""

import json
import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mcf import paths, procs, runtime

log = logging.getLogger(__name__)

MARKER = "\x1eMCF "


@dataclass
class JobOutcome:
    ok: bool
    cancelled: bool = False
    result: dict = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    error_detail: str = ""
    notices: list[dict] = field(default_factory=list)
    stages: dict[str, float] = field(default_factory=dict)
    total_seconds: float = 0.0


def sweep_temp() -> None:
    """Delete leftovers from a crash. Only called when no job is running."""
    root = paths.temp_dir()
    if not root.exists():
        return
    for item in root.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except OSError:
            log.warning("Could not delete leftover temp item %s", item)


class WorkerJob:
    """One job in one worker process. Callbacks run on the reader thread."""

    def __init__(self, engine_id: str, task: str, payload: dict, priority: str,
                 on_message: Callable[[dict], None], on_finish: Callable[[JobOutcome], None],
                 secret: bytes | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.folder = paths.temp_dir() / f"job-{self.id}"
        self.folder.mkdir(parents=True)
        (self.folder / "tmp").mkdir()
        self.on_message = on_message
        self.on_finish = on_finish
        self.cancelled = False
        self.outcome = JobOutcome(ok=False)
        self._stage = ""
        self._stage_start = 0.0
        self._started = time.monotonic()

        job = dict(payload, task=task, work_dir=str(self.folder))
        if secret:
            job["lora_bytes"] = len(secret)
        job_file = self.folder / "job.json"
        job_file.write_text(json.dumps(job), encoding="utf-8")

        env = runtime.isolated_env(temp=self.folder / "tmp")
        env["PYTHONPATH"] = str(paths.worker_dir())
        env["ACESTEP_PROJECT_ROOT"] = payload.get("model_dir", str(self.folder))
        # ACE-Step gives up after 10 minutes by default, which slow cards and long songs exceed.
        # MCF has its own cancel, so the limit is set to a week.
        env["ACESTEP_GENERATION_TIMEOUT"] = str(7 * 24 * 3600)
        env["HF_HUB_OFFLINE"] = "1"  # The worker never downloads anything
        env["TRANSFORMERS_OFFLINE"] = "1"
        python = runtime.engine_python(engine_id)
        self.process = procs.Process([str(python), "-X", "utf8", "-m", "mcf_worker", str(job_file)],
                                     env=env, cwd=self.folder, on_line=self._line,
                                     priority=priority, stdin_data=secret)
        threading.Thread(target=self._wait, daemon=True).start()

    def _line(self, line: str) -> None:
        if not line.startswith(MARKER):
            log.info("worker: %s", line)
            return
        try:
            message = json.loads(line[len(MARKER):])
        except ValueError:
            return
        kind = message.get("type")
        if kind == "stage":
            self._end_stage()
            self._stage = str(message.get("name", ""))
            self._stage_start = time.monotonic()
        elif kind == "result":
            self.outcome.result = message
        elif kind == "error":
            self.outcome.error_code = str(message.get("code", "failed"))
            self.outcome.error_message = str(message.get("message", ""))
            self.outcome.error_detail = str(message.get("detail", ""))
            log.error("Worker error %s: %s\n%s", self.outcome.error_code, self.outcome.error_message,
                      self.outcome.error_detail)
        elif kind == "notice":
            self.outcome.notices.append(message)
        self.on_message(message)

    def _end_stage(self) -> None:
        if self._stage:
            elapsed = time.monotonic() - self._stage_start
            self.outcome.stages[self._stage] = self.outcome.stages.get(self._stage, 0.0) + elapsed

    def _wait(self) -> None:
        code = self.process.wait()
        self._end_stage()
        self.outcome.total_seconds = time.monotonic() - self._started
        self.outcome.cancelled = self.cancelled
        self.outcome.ok = code == 0 and not self.cancelled and bool(self.outcome.result)
        if not self.outcome.ok and not self.cancelled and not self.outcome.error_code:
            self.outcome.error_code = "crashed"
            self.outcome.error_detail = self.process.tail()
            log.error("Worker ended with code %s:\n%s", code, self.process.tail())
        try:
            self.on_finish(self.outcome)
        finally:
            self.cleanup()

    def cancel(self) -> None:
        self.cancelled = True
        self.process.kill()

    def cleanup(self) -> None:
        # The worker may still be releasing files for a moment after exiting
        for _attempt in range(10):
            shutil.rmtree(self.folder, ignore_errors=True)
            if not self.folder.exists():
                return
            time.sleep(0.5)
        log.warning("Could not delete %s; it will be deleted next time MCF starts", self.folder)
