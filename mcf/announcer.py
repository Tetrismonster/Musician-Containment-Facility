"""Send messages straight to the user's screen reader instead of popups.

Every message also goes to the status bar and the announcement history,
so nothing is lost if speech is turned off or the user missed it.
"""

import enum
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from mcf import uia_notify
from mcf.i18n import _

log = logging.getLogger(__name__)


class Kind(enum.Enum):
    ERROR = "error"            # Always spoken
    COMPLETION = "completion"  # Something finished
    PROGRESS = "progress"      # Progress updates during long tasks
    HINT = "hint"              # Beginner tips and guidance
    INFO = "info"              # Everything else


@dataclass
class Announcement:
    text: str
    kind: Kind
    time: datetime = field(default_factory=datetime.now)


class Announcer:
    HISTORY_SIZE = 200

    def __init__(self, settings, call_on_ui_thread: Callable[[Callable], None] | None = None):
        self.settings = settings
        self._call_on_ui_thread = call_on_ui_thread
        self._ui_thread = threading.current_thread()
        self.history: deque[Announcement] = deque(maxlen=self.HISTORY_SIZE)
        self.status_listeners: list[Callable[[str], None]] = []
        self.hwnd_provider: Callable[[], int] = lambda: 0
        self._auto = None
        self._sapi = None

    # Filtering

    def should_speak(self, kind: Kind, detail: bool = False) -> bool:
        if kind is Kind.ERROR:
            return True
        verbosity = self.settings.get("speech.verbosity")
        if kind is Kind.COMPLETION:
            return self.settings.get("speech.announce_completion")
        if verbosity == "quiet":
            return False
        if detail and verbosity != "detailed":
            return False
        if kind is Kind.PROGRESS:
            return self.settings.get("speech.announce_progress")
        if kind is Kind.HINT:
            return self.settings.get("speech.announce_hints")
        return True

    # Public API

    def announce(self, text: str, kind: Kind = Kind.INFO, detail: bool = False,
                 interrupt: bool | None = None) -> None:
        """Announce text. Safe to call from any thread."""
        if threading.current_thread() is not self._ui_thread and self._call_on_ui_thread:
            self._call_on_ui_thread(lambda: self.announce(text, kind, detail, interrupt))
            return
        self.history.append(Announcement(text, kind))
        for listener in self.status_listeners:
            listener(text)
        if self.should_speak(kind, detail):
            if interrupt is None:
                interrupt = self.settings.get("speech.interrupt")
            self.speak(text, interrupt)

    def speak(self, text: str, interrupt: bool = False) -> None:
        """Speak unconditionally through the configured output."""
        try:
            self._speak(text, interrupt)
        except Exception:
            log.exception("Speech output failed")

    def repeat_last(self) -> None:
        if self.history:
            self.speak(self.history[-1].text, interrupt=True)
        else:
            self.speak(_("No announcements yet."), interrupt=True)

    def output_name(self) -> str:
        """Human-readable name of the output currently in use."""
        mode = self.settings.get("speech.output")
        if mode == "none":
            return _("Status bar only")
        if mode == "sapi":
            return _("Windows voice")
        screen_reader = self._active_screen_reader()
        if screen_reader is not None:
            return screen_reader.name
        if uia_notify.available() and uia_notify.narrator_running():
            return "Narrator"
        if self.settings.get("speech.fallback") == "sapi":
            return _("Windows voice (no screen reader found)")
        return _("Status bar only (no screen reader found)")

    # Outputs

    def _speak(self, text: str, interrupt: bool) -> None:
        mode = self.settings.get("speech.output")
        if mode == "none":
            return
        if mode == "sapi":
            self._speak_sapi(text, interrupt)
            return
        screen_reader = self._active_screen_reader()
        if screen_reader is not None:
            screen_reader.speak(text, interrupt=interrupt)
            if self.settings.get("speech.braille"):
                try:
                    screen_reader.braille(text)
                except Exception:
                    pass
            return
        if uia_notify.available() and uia_notify.narrator_running():
            if uia_notify.notify(self.hwnd_provider(), text, interrupt):
                return
        if self.settings.get("speech.fallback") == "sapi":
            self._speak_sapi(text, interrupt)

    def _active_screen_reader(self):
        if self._auto is None:
            try:
                from accessible_output2.outputs.auto import Auto
                self._auto = Auto()
            except Exception:
                log.exception("Could not load screen reader support")
                return None
        for output in self._auto.outputs:
            # Windows voices are handled separately so they are never
            # mistaken for a screen reader.
            if output.name.lower().startswith("sapi"):
                continue
            try:
                if output.is_active():
                    return output
            except Exception:
                continue
        return None

    def _speak_sapi(self, text: str, interrupt: bool) -> None:
        if self._sapi is None:
            from accessible_output2.outputs.sapi5 import SAPI5
            self._sapi = SAPI5()
        self._sapi.speak(text, interrupt=interrupt)
