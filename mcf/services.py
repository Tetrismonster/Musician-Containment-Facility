"""Shared state for the whole app: the model list, library, profiles and estimates."""

import logging
from pathlib import Path

from mcf import hardware, models, paths, recommend, runtime
from mcf.estimates import Estimator
from mcf.library import Library
from mcf.profiles import ProfileStore

log = logging.getLogger(__name__)


class Services:
    def __init__(self, settings):
        self.settings = settings
        self.manifest = models.load_manifest()
        self.estimator = Estimator()
        self.estimator.load()
        self.hardware = hardware.load_scan()
        self.library = Library(settings.library_dir())
        self.profiles = ProfileStore(paths.profiles_dir())
        self.busy = False  # A job or install is running

    @property
    def store(self) -> models.ModelStore:
        return models.ModelStore(self.settings.models_dir(), self.manifest)

    def reopen_library(self) -> None:
        self.library.close()
        self.library = Library(self.settings.library_dir())

    # Models

    def engine_for(self, model: dict) -> dict:
        return self.manifest["engines"][model["engine"]]

    def runtime_ready(self, model: dict) -> bool:
        return runtime.is_installed(self.engine_for(model))

    def ready_models(self) -> list[dict]:
        """Models that are downloaded, with their AI software installed."""
        store = self.store
        return [m for m in self.manifest["models"] if store.is_installed(m) and self.runtime_ready(m)]

    def model(self, model_id: str) -> dict | None:
        return models.model_by_id(self.manifest, model_id)

    def assessment(self, model: dict) -> recommend.Assessment | None:
        if self.hardware is None:
            return None
        mode = self.settings.get("performance.mode")
        return recommend.assess(model, self.hardware, None if mode == "auto" else mode)

    def fingerprint(self) -> str:
        return self.hardware.fingerprint() if self.hardware else "unknown"

    def performance_for(self, model: dict) -> dict:
        """The worker's performance settings: mode and song planner."""
        mode = self.settings.get("performance.mode")
        assessment = self.assessment(model)
        planner = None
        if assessment and assessment.planner:
            component = next(c for c in model["components"] if c["id"] == assessment.planner)
            if self.store.is_component_installed(model["id"], component):
                planner = component["planner"]
        if planner is None:
            # Fall back to any installed planner the card can hold
            for component in model["components"]:
                if component.get("planner") and self.store.is_component_installed(model["id"], component) \
                        and assessment and assessment.mode not in (None, "cpu") \
                        and self.hardware.best_gpu and self.hardware.best_gpu.vram_gb >= component["min_vram_gb"]:
                    planner = component["planner"]
        worker_mode = mode
        if mode == "auto" and assessment and assessment.mode == "cpu":
            worker_mode = "cpu"
        return {"mode": worker_mode, "planner": planner}

    def job_payload(self, model: dict) -> dict:
        store = self.store
        return {
            "engine": model["engine"],
            "engine_source": str(runtime.engine_source_dir(model["engine"])),
            "model_dir": str(store.folder(model["id"])),
            "cover_tools_dir": str(store.folder(models.COVER_TOOLS_ID)),
            "performance": self.performance_for(model),
        }

    def cover_tools_ready(self) -> bool:
        tools = self.manifest["cover_tools"]
        return self.store.cover_tools_installed() and runtime.is_installed(self.manifest["engines"][tools["engine"]])

    def runtime_credits_file(self, engine_id: str) -> Path:
        return runtime.engine_env_dir(engine_id) / "mcf-credits.txt"
