"""Long setup jobs: installing the AI runtime and downloading models.

Each function returns a task: a callable taking (status, progress, cancel)
that runs on a background thread. Progress is reported from 0 to 1 over the
whole job, weighted by download size.
"""

import logging
import threading
import time

from mcf import downloads, jobs, models, runtime
from mcf.i18n import _

log = logging.getLogger(__name__)

GB = 1024**3


class SetupError(Exception):
    pass


def run_worker_sync(engine_id: str, task: str, payload: dict, cancel: threading.Event) -> jobs.JobOutcome:
    """Run a worker task and wait for it. There's no time limit: slow computers can take
    many minutes just to start the AI software, and the user can always cancel."""
    done = threading.Event()
    box: dict = {}

    def finished(outcome):
        box["outcome"] = outcome
        done.set()

    job = jobs.WorkerJob(engine_id, task, payload, "normal", lambda _m: None, finished)
    while not done.wait(0.3):
        if cancel.is_set():
            job.cancel()
            done.wait(30)
            raise downloads.Cancelled()
    return box["outcome"]


def verify_runtime(services, model: dict, cancel: threading.Event) -> None:
    """Prove the runtime works, and that it can use the graphics card when it should."""
    engine_id = model["engine"]
    outcome = run_worker_sync(engine_id, "selftest", {}, cancel)
    if not outcome.ok:
        raise SetupError(_("The AI software was installed but doesn't start. {detail}").format(
            detail=outcome.error_message or outcome.error_detail[-500:]))
    result = outcome.result
    assessment = services.assessment(model)
    if assessment and assessment.mode in ("gpu", "offload", "compressed") and not result.get("cuda"):
        raise SetupError(_("The AI software can't use the graphics card. Updating the NVIDIA graphics "
                           "driver usually fixes this."))
    if result.get("cuda") and not result.get("ok"):
        raise SetupError(_("The graphics card gave a wrong answer in a simple test calculation. "
                           "Updating the graphics driver may fix this."))
    # The credits list for the AI software, shown in Help, Licenses and attribution
    engine = services.manifest["engines"][engine_id]
    run_worker_sync(engine_id, "licenses", {"output": str(services.runtime_credits_file(engine_id)),
                                            "engine_name": engine["name"]}, cancel)


def install_model(services, model: dict, components: list[dict]):
    """Install the runtime if needed, then download the chosen components."""

    def task(status, progress, cancel):
        engine = services.engine_for(model)
        store = services.store
        need_runtime = not runtime.is_installed(engine)
        runtime_weight = engine["download_gb"] * GB if need_runtime else 0
        to_download = [c for c in components if not store.is_component_installed(model["id"], c)]
        download_weight = sum(models.component_size(c) for c in to_download)
        total = max(1, runtime_weight + download_weight)

        if need_runtime:
            runtime.install_engine(services.manifest, model["engine"], status,
                                   lambda f: progress(f * runtime_weight / total), cancel)
            status(_("Checking that the AI software works on this computer."))
            verify_runtime(services, model, cancel)
        if to_download:
            started = time.monotonic()
            store.install(model["id"], to_download, status,
                          lambda f: progress((runtime_weight + f * download_weight) / total), cancel)
            services.estimator.record_download(download_weight, time.monotonic() - started)
        progress(1.0)

    return task


def install_cover_tools(services):
    def task(status, progress, cancel):
        tools = services.manifest["cover_tools"]
        store = services.store
        engine = services.manifest["engines"][tools["engine"]]
        need_runtime = not runtime.is_installed(engine)
        size = sum(models.component_size(c) for c in tools["components"])
        runtime_weight = engine["download_gb"] * GB if need_runtime else 0
        total = max(1, size + runtime_weight)
        if need_runtime:
            # The analysis runs in this engine's AI software
            runtime.install_engine(services.manifest, engine["id"], status,
                                   lambda f: progress(f * runtime_weight / total), cancel)
        started = time.monotonic()
        store.install(models.COVER_TOOLS_ID, tools["components"], status,
                      lambda f: progress((runtime_weight + f * size) / total), cancel)
        services.estimator.record_download(size, time.monotonic() - started)

    return task


def repair_runtime(services, model: dict):
    def task(status, progress, cancel):
        runtime.remove_engine(model["engine"])
        runtime.install_engine(services.manifest, model["engine"], status, progress, cancel)
        status(_("Checking that the AI software works on this computer."))
        verify_runtime(services, model, cancel)

    return task


def verify_model(services, model: dict, result: list):
    def task(status, progress, cancel):
        status(_("Checking every file of {name}. This reads several gigabytes, so it takes a few minutes.").format(
            name=model["name"]))
        store = services.store
        result.extend(store.verify(model["id"], store.installed_components(model), progress, cancel))

    return task
