"""Time estimates that learn from real runs on this computer.

Only timing numbers and hardware details are recorded: never prompts, lyrics,
file names or audio. The records are a plain JSON file. Every value is
checked when loaded, so a damaged or edited file can only make an estimate
less accurate. It can never run code.
"""

import json
import logging
import math
import statistics
import time
from dataclasses import dataclass
from pathlib import Path

from mcf import paths
from mcf.i18n import _

log = logging.getLogger(__name__)

MAX_RUNS_PER_KEY = 200
MAX_SECONDS = 7 * 24 * 3600
TASKS = ("generate", "cover", "analyse", "train")
# Recent runs count more: a run's weight halves every this many runs
HALF_LIFE_RUNS = 10


@dataclass
class Estimate:
    low: float
    high: float
    runs: int

    @property
    def middle(self) -> float:
        return (self.low + self.high) / 2


def _valid_run(run) -> bool:
    if not isinstance(run, dict):
        return False
    try:
        return (isinstance(run["model"], str) and len(run["model"]) < 100
                and run["task"] in TASKS
                and isinstance(run["mode"], str) and len(run["mode"]) < 40
                and isinstance(run["hw"], str) and len(run["hw"]) < 40
                and 0 < float(run["size"]) <= 100_000
                and 0 < float(run["total"]) <= MAX_SECONDS
                and 0 <= float(run.get("time", 0)) < 10**11
                and isinstance(run.get("stages", {}), dict)
                and all(isinstance(k, str) and len(k) < 40 and 0 <= float(v) <= MAX_SECONDS
                        for k, v in run.get("stages", {}).items()))
    except (KeyError, TypeError, ValueError):
        return False


class Estimator:
    def __init__(self, path: Path | None = None):
        self.path = path or paths.settings_dir() / "estimates.json"
        self.runs: list[dict] = []
        self.download_bps: list[float] = []
        self.problem: str | None = None

    # Storage

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self._set_aside(f"unreadable: {exc}")
            return
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("runs"), list):
            self._set_aside("wrong structure")
            return
        runs = data["runs"]
        self.runs = [r for r in runs if _valid_run(r)]
        if len(self.runs) != len(runs):
            log.warning("Ignored %d invalid timing records", len(runs) - len(self.runs))
        speeds = data.get("download_bps", [])
        if isinstance(speeds, list):
            self.download_bps = [float(s) for s in speeds[-20:]
                                 if isinstance(s, (int, float)) and 0 < s < 10**11]

    def _set_aside(self, reason: str) -> None:
        log.warning("Timing records set aside (%s)", reason)
        try:
            self.path.replace(self.path.with_suffix(".damaged.json"))
        except OSError:
            pass
        self.runs = []
        self.problem = _("The saved timing records were damaged, so MCF set them aside and started fresh. "
                         "Time estimates will be less accurate for a while.")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "runs": self.runs, "download_bps": self.download_bps}),
                       encoding="utf-8")
        tmp.replace(self.path)

    def reset(self) -> None:
        self.runs = []
        self.download_bps = []
        self.save()

    # Recording

    def record(self, model: str, task: str, mode: str, hw: str, size: float, total: float,
               stages: dict[str, float] | None = None) -> None:
        """Record a finished run. size is the audio length in seconds, or songs for training."""
        run = {"model": model, "task": task, "mode": mode, "hw": hw, "size": round(float(size), 2),
               "total": round(float(total), 2), "time": int(time.time()),
               "stages": {k: round(float(v), 2) for k, v in (stages or {}).items()}}
        if not _valid_run(run):
            return
        self.runs.append(run)
        same = [r for r in self.runs if self._key(r) == self._key(run)]
        if len(same) > MAX_RUNS_PER_KEY:
            oldest = same[0]
            self.runs.remove(oldest)
        try:
            self.save()
        except OSError:
            log.exception("Could not save timing records")

    def record_download(self, bytes_done: int, seconds: float) -> None:
        if bytes_done > 50_000_000 and seconds > 1:
            self.download_bps.append(bytes_done / seconds)
            del self.download_bps[:-20]
            try:
                self.save()
            except OSError:
                pass

    @staticmethod
    def _key(run: dict) -> tuple:
        return run["model"], run["task"], run["mode"], run["hw"]

    # Estimating

    def runs_for(self, model: str, task: str, mode: str, hw: str) -> list[dict]:
        return [r for r in self.runs if self._key(r) == (model, task, mode, hw)]

    def estimate(self, model: str, task: str, mode: str, hw: str, size: float, baseline: float) -> Estimate:
        """baseline is the manifest's guess for this job, used until MCF has learned enough."""
        runs = self.runs_for(model, task, mode, hw)
        if not runs:
            return Estimate(baseline * 0.7, baseline * 1.6, 0)
        # How much slower or faster than the baseline this computer really is,
        # scaled per second of audio so different lengths can be compared
        ratios = []
        weights = []
        for age, run in enumerate(reversed(runs)):
            ratios.append(run["total"] / max(1.0, self._baseline_for(run, baseline, size)))
            weights.append(0.5 ** (age / HALF_LIFE_RUNS))
        median = statistics.median(ratios)
        kept = [(r, w) for r, w in zip(ratios, weights) if median / 3 <= r <= median * 3]
        total_weight = sum(w for _, w in kept)
        ratio = sum(r * w for r, w in kept) / total_weight
        spread = math.sqrt(sum(w * (r - ratio) ** 2 for r, w in kept) / total_weight) / ratio
        # Fewer runs means less certainty
        margin = max(0.08, spread * 1.5, 0.5 / math.sqrt(len(kept)))
        middle = baseline * ratio
        return Estimate(middle * (1 - margin), middle * (1 + margin), len(kept))

    @staticmethod
    def _baseline_for(run: dict, baseline: float, size: float) -> float:
        # Baselines are roughly proportional to size
        return baseline * (run["size"] / size) if size else baseline

    def download_estimate(self, size_bytes: int) -> Estimate | None:
        if not self.download_bps:
            return None
        speed = statistics.median(self.download_bps)
        middle = size_bytes / speed
        return Estimate(middle * 0.8, middle * 1.4, len(self.download_bps))

    def counts(self) -> list[tuple[str, str, str, int]]:
        """(model, task, mode, number of runs) for the current records."""
        result: dict[tuple, int] = {}
        for run in self.runs:
            key = (run["model"], run["task"], run["mode"])
            result[key] = result.get(key, 0) + 1
        return [(*k, n) for k, n in sorted(result.items())]


def describe_duration(seconds: float) -> str:
    seconds = max(1, round(seconds))
    if seconds < 90:
        return _("{n} seconds").format(n=seconds)
    minutes = seconds / 60
    if minutes < 90:
        return _("{n} minutes").format(n=round(minutes))
    hours = minutes / 60
    if hours < 48:
        return _("{n:.1f} hours").format(n=hours).replace(".0 ", " ")
    return _("{n:.0f} days").format(n=hours / 24)


def describe(estimate: Estimate) -> str:
    low, high = describe_duration(estimate.low), describe_duration(estimate.high)
    if low == high:
        return _("about {time}").format(time=low)
    # "about 3 to 4 minutes" rather than "3 minutes to 4 minutes"
    low_n, _sep, low_unit = low.partition(" ")
    high_n, _sep, high_unit = high.partition(" ")
    if low_unit == high_unit:
        return _("about {low} to {high} {unit}").format(low=low_n, high=high_n, unit=high_unit)
    return _("about {low} to {high}").format(low=low, high=high)
