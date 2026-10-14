"""What each model can do on this computer, stated plainly."""

from dataclasses import dataclass, field

from mcf.estimates import describe_duration
from mcf.hardware import Hardware
from mcf.i18n import _

MODES = ("gpu", "offload", "compressed", "cpu")
SAMPLE_SONG_SECONDS = 120
WELL_LIMIT = 5 * 60  # A 2-minute song within 5 minutes counts as running well
IMPRACTICAL = 4 * 3600  # Still offered, with the time stated


def mode_label(mode: str) -> str:
    return {
        "auto": _("Automatic (recommended)"),
        "gpu": _("Graphics card only: fastest"),
        "offload": _("Graphics card plus system memory: slower, fits bigger models"),
        "compressed": _("Compressed (8-bit) on the graphics card plus system memory: smallest"),
        "cpu": _("Processor only: very slow, no graphics card needed"),
    }[mode]


def mode_name(mode: str) -> str:
    """The mode as the subject of a sentence."""
    return {
        "gpu": _("Running entirely on the graphics card"),
        "offload": _("Splitting the model between the graphics card and system memory"),
        "compressed": _("Running the compressed model on the graphics card"),
        "cpu": _("Running on the processor"),
    }[mode]


@dataclass
class Assessment:
    model_id: str
    mode: str | None  # Best mode, or None if the model can't run
    verdict: str  # well, slow, impractical or no
    reasons: list[str] = field(default_factory=list)
    planner: str | None = None  # Planner component id to use
    song_seconds: float = 0.0  # Baseline time for a 2-minute song
    possible_modes: list[str] = field(default_factory=list)

    def verdict_text(self) -> str:
        if self.verdict == "well":
            return _("Runs well")
        if self.verdict == "slow":
            return _("Runs slowly")
        if self.verdict == "impractical":
            return _("Runs, but far too slowly to be practical")
        return _("Won't run on this computer")

    def summary(self) -> str:
        text = self.verdict_text()
        if self.mode:
            text += _(": about {time} for a 2-minute song").format(time=describe_duration(self.song_seconds))
        return text


def _gpu_problem(model: dict, hw: Hardware) -> str:
    """Why the graphics card can't be used at all, or an empty string."""
    gpu = hw.best_gpu
    if gpu is None:
        return _("This computer has no graphics card that MCF can use.")
    if gpu.vendor != "nvidia":
        return _("{model} can only use NVIDIA graphics cards on Windows. This computer has {gpu}.").format(
            model=model["name"], gpu=gpu.describe())
    if gpu.compute and gpu.compute < model.get("min_compute", 7.5):
        return _("{gpu} is too old for the AI software. It needs an NVIDIA GTX 16 series, RTX 20 series or "
                 "newer card.").format(gpu=gpu.name)
    return ""


def mode_problem(model: dict, mode: str, hw: Hardware) -> str:
    """Why this performance mode won't work here, or an empty string if it will."""
    spec = model["modes"].get(mode)
    if spec is None:
        return _("{model} doesn't have this mode.").format(model=model["name"])
    if hw.ram_gb < spec.get("min_ram_gb", 0):
        return _("{mode} needs {need} GB of system memory (RAM), and this computer has {ram:.0f} GB.").format(
            mode=mode_name(mode), need=spec["min_ram_gb"], ram=hw.ram_gb)
    if mode == "cpu":
        return ""
    problem = _gpu_problem(model, hw)
    if problem:
        return problem
    gpu = hw.best_gpu
    if gpu.compute and gpu.compute < spec.get("min_compute", 0):
        return _("{mode} needs an NVIDIA RTX 30 series or newer card, because {model} relies on bfloat16 "
                 "maths. {gpu} is older.").format(mode=mode_name(mode), model=model["name"], gpu=gpu.name)
    if gpu.vram_gb < spec.get("min_vram_gb", 0):
        return _("{mode} needs {need} GB of graphics memory, and {gpu} has {vram:.0f} GB.").format(
            mode=mode_name(mode), need=spec["min_vram_gb"], gpu=gpu.name, vram=gpu.vram_gb)
    return ""


def possible_modes(model: dict, hw: Hardware) -> list[str]:
    return [m for m in model["preference"] if not mode_problem(model, m, hw)]


def pick_planner(model: dict, hw: Hardware, mode: str | None) -> str | None:
    if mode in (None, "cpu") or hw.best_gpu is None:
        return None
    vram = hw.best_gpu.vram_gb
    best = None
    for component in model["components"]:
        if component.get("planner") and vram >= component.get("min_vram_gb", 99):
            if best is None or component["min_vram_gb"] > best["min_vram_gb"]:
                best = component
    return best["id"] if best else None


def baseline_seconds(model: dict, mode: str, audio_seconds: float, planner: bool) -> float:
    timing = model["timing"]
    key = mode if mode in timing["load_seconds"] else "offload"
    total = timing["load_seconds"][key] + audio_seconds * timing["seconds_per_audio_second"][key]
    if planner:
        total += audio_seconds * timing["planner_seconds_per_audio_second"].get(key, 0)
    return total


def analysis_baseline(duration: float, mode: str) -> float:
    return 20 + duration * (0.15 if mode != "cpu" else 2.5)


def assess(model: dict, hw: Hardware, forced_mode: str | None = None) -> Assessment:
    modes = possible_modes(model, hw)
    result = Assessment(model_id=model["id"], mode=None, verdict="no", possible_modes=modes)
    if forced_mode and forced_mode in modes:
        mode = forced_mode
    else:
        mode = modes[0] if modes else None
    if mode is None:
        seen = set()
        for m in model["preference"]:
            problem = mode_problem(model, m, hw)
            if problem not in seen:
                seen.add(problem)
                result.reasons.append(problem)
        return result

    result.mode = mode
    result.planner = pick_planner(model, hw, mode)
    result.song_seconds = baseline_seconds(model, mode, SAMPLE_SONG_SECONDS, result.planner is not None)
    if result.song_seconds > IMPRACTICAL:
        result.verdict = "impractical"
    elif result.song_seconds > WELL_LIMIT:
        result.verdict = "slow"
    else:
        result.verdict = "well"

    # Say why the faster modes aren't used
    for faster in model["preference"][:model["preference"].index(mode)]:
        problem = mode_problem(model, faster, hw)
        if problem and problem not in result.reasons:
            result.reasons.append(problem)
            break
    if mode == "cpu":
        result.reasons.append(_("The processor does all the work. A 2-minute song takes {time}.").format(
            time=describe_duration(result.song_seconds)))
    elif mode == "offload":
        result.reasons.append(_("Parts of the model are kept in system memory and moved to the graphics card "
                                "when needed."))
    elif mode == "compressed":
        result.reasons.append(_("The model is compressed to 8 bits so it fits in the graphics card's memory. "
                                "Quality is slightly lower."))
    if model.get("full_precision_on_old_cards") and hw.best_gpu and hw.best_gpu.vendor == "nvidia" \
            and 0 < hw.best_gpu.compute < 8 and mode != "cpu":
        result.reasons.append(_("This graphics card is older than the RTX 30 series, so MCF uses slower "
                                "full-precision maths to avoid broken audio."))
    if any(c.get("planner") for c in model["components"]) and result.planner is None and mode != "cpu":
        result.reasons.append(_("The song planner is left out: it needs more graphics memory than this "
                                "card has. Songs still work, with slightly less structured arrangements."))
    return result


def overall(assessments: list[Assessment]) -> str:
    """One plain sentence about the computer as a whole."""
    runnable = [a for a in assessments if a.verdict in ("well", "slow")]
    if not runnable:
        if any(a.verdict == "impractical" for a in assessments):
            return _("This computer can technically run MCF's models, but so slowly that generating "
                     "a song isn't practical.")
        return _("This computer can't run any of MCF's models.")
    if any(a.verdict == "well" for a in runnable):
        return _("This computer can run MCF's models.")
    return _("This computer can run MCF's models, but slowly.")


@dataclass
class TrainingAssessment:
    possible: bool
    reasons: list[str] = field(default_factory=list)
    settings: dict = field(default_factory=dict)


def assess_training(model: dict, hw: Hardware | None) -> TrainingAssessment:
    req = model.get("training")
    if not req:
        return TrainingAssessment(False, [_("{model} can't be trained.").format(model=model["name"])])
    if hw is None:
        return TrainingAssessment(False, [_("The hardware hasn't been scanned, so MCF can't tell whether this "
                                            "computer can train. Use Help, Hardware scan.")])
    gpu = hw.best_gpu
    reasons = []
    if gpu is None or gpu.vendor != "nvidia":
        reasons.append(_("Training needs an NVIDIA graphics card. This computer doesn't have one."))
    elif gpu.compute and gpu.compute < req["min_compute"]:
        reasons.append(_("Training needs an NVIDIA RTX 30 series or newer card, because it relies on bfloat16 "
                         "maths. {gpu} is older, and training in full precision needs far more graphics memory "
                         "than it has.").format(gpu=gpu.name))
    elif gpu.vram_gb < req["min_vram_gb"]:
        reasons.append(_("Training needs at least {need} GB of graphics memory. {gpu} has {vram:.0f} GB.").format(
            need=req["min_vram_gb"], gpu=gpu.name, vram=gpu.vram_gb))
    if hw.ram_gb < req["min_ram_gb"]:
        reasons.append(_("Training needs at least {need} GB of system memory (RAM). This computer has "
                         "{ram:.0f} GB.").format(need=req["min_ram_gb"], ram=hw.ram_gb))
    if reasons:
        return TrainingAssessment(False, reasons)
    vram = gpu.vram_gb
    if vram >= 20:
        settings = {"rank": 64, "optimizer": "adamw", "offload_encoder": False}
    elif vram >= 12:
        settings = {"rank": 32, "optimizer": "adafactor", "offload_encoder": True}
    else:
        settings = {"rank": 16, "optimizer": "adafactor", "offload_encoder": True}
        reasons.append(_("With {vram:.0f} GB of graphics memory, MCF trains a smaller add-on, which captures less "
                         "detail. Songs longer than about 3 minutes may run out of memory.").format(vram=vram))
    settings.update(device="cuda", precision="bf16")
    return TrainingAssessment(True, reasons, settings)


def training_baseline(model: dict, songs: int, epochs: int) -> float:
    req = model["training"]
    return songs * req["preprocess_seconds_per_song"] + songs * epochs * req["seconds_per_song_epoch"] + 90
