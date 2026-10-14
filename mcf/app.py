"""Application entry point."""

import logging
import logging.handlers
import sys
import traceback

import wx

from mcf import APP_NAME, i18n, paths
from mcf.announcer import Announcer, Kind
from mcf.settings import Settings

log = logging.getLogger("mcf")


def _setup_logging() -> None:
    folder = paths.logs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        folder / "mcf.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])


class McfApp(wx.App):
    def OnInit(self):
        self.SetAppName(APP_NAME)
        self.instance_checker = wx.SingleInstanceChecker(f"{APP_NAME}-{wx.GetUserId()}")
        if self.instance_checker.IsAnotherRunning():
            # Only speech here; a second window would confuse screen reader users
            Announcer(self.settings).speak(f"{APP_NAME} is already running.")
            return False

        from mcf.services import Services
        from mcf.ui.main_frame import MainFrame
        self.services = Services(self.settings)
        self.frame = MainFrame(self)
        self.frame.Show()
        self.SetTopWindow(self.frame)
        wx.CallAfter(self.frame.after_show)
        return True

    def __init__(self, settings: Settings):
        self.settings = settings
        self.announcer = Announcer(settings, call_on_ui_thread=wx.CallAfter)
        super().__init__(redirect=False)


def self_check() -> int:
    """Load every part of MCF without opening a window. Used to test a build: MCF.exe --self-check"""
    import importlib
    problems = []
    for name in ("mcf.ui.main_frame", "mcf.ui.preferences", "mcf.ui.onboarding", "mcf.ui.training",
                 "mcf.ui.player", "mcf.services", "mcf.setup_tasks", "mcf.audio_export", "mcf.profiles",
                 "mcf.manifest_update", "wx.media", "soundfile", "numpy", "cryptography"):
        try:
            importlib.import_module(name)
        except Exception as exc:
            problems.append(f"{name}: {exc}")
    try:
        from mcf import models
        manifest = models.load_manifest()
        if not manifest["models"]:
            problems.append("The model list is empty")
    except Exception as exc:
        problems.append(f"model list: {exc}")
    if not (paths.worker_dir() / "mcf_worker" / "__main__.py").exists():
        problems.append("The worker files are missing")
    if not list(paths.licenses_dir().glob("*.txt")):
        problems.append("The license files are missing")
    for problem in problems:
        log.error("Self-check: %s", problem)
    log.info("Self-check %s", "failed" if problems else "passed")
    return 1 if problems else 0


def install_runtime_only() -> int:
    """Install or repair the AI software without a window: MCF.exe --install-runtime. Used to test builds."""
    import threading
    from mcf import models, runtime, setup_tasks
    manifest = models.load_manifest()
    cancel = threading.Event()
    for engine_id in manifest["engines"]:
        try:
            runtime.install_engine(manifest, engine_id, lambda s: log.info("Install: %s", s), lambda f: None, cancel)
            outcome = setup_tasks.run_worker_sync(engine_id, "selftest", {}, cancel)
        except Exception:
            log.exception("Installing the AI software failed")
            return 1
        log.info("Self-test result: ok=%s %s", outcome.ok, outcome.result)
        if not outcome.ok:
            return 1
    return 0


def main() -> None:
    _setup_logging()
    if "--self-check" in sys.argv:
        sys.exit(self_check())
    if "--install-runtime" in sys.argv:
        sys.exit(install_runtime_only())
    settings = Settings()
    settings.load()
    i18n.set_language(settings.get("general.language"))
    app = McfApp(settings)

    def excepthook(exc_type, exc, tb):
        log.error("Unhandled error:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        app.announcer.announce(
            i18n._("Something went wrong: {error}. Details are in the logs folder.").format(error=exc),
            Kind.ERROR)

    sys.excepthook = excepthook
    log.info("Starting %s", APP_NAME)
    app.MainLoop()
