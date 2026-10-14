"""Larger reusable pieces: folder picker, progress panel, background tasks,
rights confirmation and the wizard frame."""

import logging
import threading
import time
from pathlib import Path

import wx

from mcf import downloads, hardware
from mcf.announcer import Kind
from mcf.i18n import _
from mcf.ui.widgets import LABEL_BORDER, labeled, read_only_text, tag

log = logging.getLogger(__name__)


def free_space_text(folder) -> str:
    free = hardware.free_bytes(Path(folder))
    return _("unknown") if free is None else _("{gb:.1f} GB free").format(gb=free / 1024**3)


class FolderPicker(wx.Panel):
    """A labelled folder path with Browse and Use default buttons, and the free space."""

    def __init__(self, parent, label: str, value: str, default, help_key: str | None = None,
                 on_change=None):
        super().__init__(parent)
        self.default = default
        self.on_change = on_change
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.path = labeled(self, sizer, label, lambda p: wx.TextCtrl(p, value=value or str(default)), help_key)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        browse = wx.Button(self, label=_("&Browse..."))
        reset = wx.Button(self, label=_("Use de&fault"))
        buttons.Add(browse, 0, wx.ALL, LABEL_BORDER)
        buttons.Add(reset, 0, wx.ALL, LABEL_BORDER)
        sizer.Add(buttons)
        self.free = labeled(self, sizer, _("Free space:"), lambda p: wx.TextCtrl(
            p, value=free_space_text(self.path.GetValue()), style=wx.TE_READONLY))
        if help_key:
            for ctrl in (browse, reset, self.free):
                tag(ctrl, help_key)
        self.SetSizer(sizer)
        browse.Bind(wx.EVT_BUTTON, self.on_browse)
        reset.Bind(wx.EVT_BUTTON, lambda e: self.set(str(self.default)))
        self.path.Bind(wx.EVT_KILL_FOCUS, self._on_edited)

    def _on_edited(self, event):
        event.Skip()
        self.free.SetValue(free_space_text(self.path.GetValue()))
        if self.on_change:
            self.on_change()

    def on_browse(self, event):
        with wx.DirDialog(self, _("Choose a folder"), self.path.GetValue()) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.set(dialog.GetPath())
        self.path.SetFocus()

    def set(self, value: str):
        self.path.SetValue(value)
        self.free.SetValue(free_space_text(value))
        if self.on_change:
            self.on_change()

    def value(self) -> str:
        """Stored value: empty when the default folder is used."""
        text = self.path.GetValue().strip()
        return "" if not text or Path(text) == Path(self.default) else text

    def full_path(self) -> Path:
        return Path(self.path.GetValue().strip() or str(self.default))


class ProgressPanel(wx.Panel):
    """A status field and a gauge. Announces progress at the chosen verbosity."""

    def __init__(self, parent, announcer, label: str):
        super().__init__(parent)
        self.announcer = announcer
        sizer = wx.BoxSizer(wx.VERTICAL)
        # Multi-line, so a long error can be read line by line
        self.status = labeled(self, sizer, label, lambda p: read_only_text(p, height=60))
        self.gauge = labeled(self, sizer, _("Progress:"), lambda p: wx.Gauge(p, range=1000))
        self.SetSizer(sizer)
        self._last_step = -1

    def reset(self):
        self._last_step = -1
        self.gauge.SetValue(0)

    def set_status(self, text: str, announce: bool = True):
        self.status.SetValue(text)
        if announce:
            self.announcer.announce(text, Kind.PROGRESS)

    def show_error(self, text: str):
        """Show an error, move focus to it, then speak it.

        Speaking first and moving focus after lets the screen reader cut the
        error off to read the newly focused control.
        """
        self.status.SetValue(text)
        self.status.SetInsertionPoint(0)
        self.status.SetFocus()
        wx.CallLater(500, self.announcer.announce, text, Kind.ERROR)

    def focus_status(self):
        """Keep focus somewhere readable when the button that started a task is disabled."""
        self.status.SetFocus()

    def set_progress(self, fraction: float, what: str = ""):
        fraction = max(0.0, min(1.0, fraction))
        self.gauge.SetValue(int(fraction * 1000))
        percent = int(fraction * 100)
        step = percent // 10
        if step != self._last_step and 0 < percent < 100:
            self._last_step = step
            text = (_("{what} {percent} percent").format(what=what, percent=percent) if what
                    else _("{percent} percent").format(percent=percent))
            # Every 25 percent at Normal verbosity, every 10 percent at Detailed
            self.announcer.announce(text, Kind.PROGRESS, detail=(step * 10) % 25 >= 10)


class BackgroundTask:
    """Runs task(status, progress, cancel) on a thread. Callbacks come back on the UI thread."""

    def __init__(self, task, on_status, on_progress, on_done):
        self.cancel_event = threading.Event()
        self._on_status = on_status
        self._on_progress = on_progress
        self._on_done = on_done
        self._last_progress = 0.0
        self.thread = threading.Thread(target=self._run, args=(task,), daemon=True)
        self.thread.start()

    def _run(self, task):
        ok, cancelled, error = False, False, ""
        try:
            task(lambda s: wx.CallAfter(self._on_status, s), self._progress, self.cancel_event)
            ok = True
        except downloads.Cancelled:
            cancelled = True
        except Exception as exc:
            log.exception("Background task failed")
            error = str(exc)
        if self.cancel_event.is_set():
            ok, cancelled = False, True
        wx.CallAfter(self._on_done, ok, cancelled, error)

    def _progress(self, fraction: float):
        now = time.monotonic()
        # A few updates a second is plenty
        if fraction >= 1.0 or now - self._last_progress > 0.25:
            self._last_progress = now
            wx.CallAfter(self._on_progress, fraction)

    def cancel(self):
        self.cancel_event.set()


class RightsPanel(wx.Panel):
    """Rights or license text in a read-only field, and a checkbox that must be ticked."""

    def __init__(self, parent, label: str, text: str, checkbox_label: str, on_change=None, height: int = 220):
        super().__init__(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.text = labeled(self, sizer, label, lambda p: read_only_text(p, text, height=height), proportion=1)
        self.check = wx.CheckBox(self, label=checkbox_label)
        sizer.Add(self.check, 0, wx.ALL, LABEL_BORDER)
        self.SetSizer(sizer)
        if on_change:
            self.check.Bind(wx.EVT_CHECKBOX, lambda e: on_change())

    def set_text(self, text: str):
        self.text.SetValue(text)
        self.text.SetInsertionPoint(0)

    @property
    def confirmed(self) -> bool:
        return self.check.IsChecked()


class WizardDialog(wx.Dialog):
    """Pages shown one at a time, with Back, Next and Cancel.

    When a page is shown, its title is announced and focus moves to its first control.
    """

    def __init__(self, parent, title: str, announcer, size=(760, 660)):
        super().__init__(parent, title=title, size=size, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.announcer = announcer
        self.pages: list[dict] = []
        self.index = -1
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.book = wx.Simplebook(self)
        sizer.Add(self.book, 1, wx.EXPAND | wx.ALL, 8)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.back = wx.Button(self, label=_("< &Back"))
        self.next = wx.Button(self, label=_("&Next >"))
        self.cancel = wx.Button(self, wx.ID_CANCEL, label=_("Cancel"))
        buttons.Add(self.back, 0, wx.ALL, 4)
        buttons.Add(self.next, 0, wx.ALL, 4)
        buttons.Add(self.cancel, 0, wx.ALL, 4)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
        self.SetSizer(sizer)
        self.back.Bind(wx.EVT_BUTTON, lambda e: self.go(-1))
        self.next.Bind(wx.EVT_BUTTON, lambda e: self.go(+1))
        self.cancel.Bind(wx.EVT_BUTTON, self.on_cancel)
        self.Bind(wx.EVT_CLOSE, self.on_cancel)
        self.SetEscapeId(wx.ID_NONE)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)

    def _on_key(self, event: wx.KeyEvent):
        if event.GetKeyCode() == wx.WXK_ESCAPE and event.GetModifiers() == 0:
            self.on_cancel(None)
            return
        event.Skip()

    def on_cancel(self, event):
        if self.busy():
            self.cancel_busy()
            return
        self.EndModal(wx.ID_CANCEL)

    def cancel_busy(self):
        pass

    def add_page(self, title: str, build, on_enter=None, can_leave=None, skip=None):
        """build(panel, sizer) fills the page. skip() returns True to pass over it."""
        panel = wx.ScrolledWindow(self.book)
        panel.SetScrollRate(0, 20)
        sizer = wx.BoxSizer(wx.VERTICAL)
        heading = wx.StaticText(panel, label=title)
        font = heading.GetFont()
        font.MakeBold().MakeLarger()
        heading.SetFont(font)
        sizer.Add(heading, 0, wx.ALL, 6)
        build(panel, sizer)
        panel.SetSizer(sizer)
        self.book.AddPage(panel, title)
        self.pages.append({"title": title, "panel": panel, "on_enter": on_enter,
                           "can_leave": can_leave, "skip": skip})

    def start(self):
        self.go(+1)

    def _skipped(self, page) -> bool:
        return bool(page["skip"] and page["skip"]())

    def go(self, step: int):
        if step > 0 and 0 <= self.index < len(self.pages):
            can_leave = self.pages[self.index]["can_leave"]
            if can_leave and not can_leave():
                return
        target = self.index + step
        while 0 <= target < len(self.pages) and self._skipped(self.pages[target]):
            target += step
        if target >= len(self.pages):
            self.finish()
            return
        if target < 0:
            return
        self.index = target
        page = self.pages[target]
        self.book.SetSelection(target)
        if page["on_enter"]:
            page["on_enter"]()
        self.update_buttons()
        self.announcer.speak(page["title"], interrupt=True)
        self.focus_first(page["panel"])

    def focus_first(self, panel):
        for child in self._walk(panel):
            if child.IsShownOnScreen() and child.IsEnabled() and child.AcceptsFocus() \
                    and not isinstance(child, (wx.StaticText, wx.Panel)):
                child.SetFocus()
                return
        self.next.SetFocus()

    def _walk(self, window):
        for child in window.GetChildren():
            yield child
            yield from self._walk(child)

    def update_buttons(self):
        busy = self.busy()
        self.back.Enable(self.index > 0 and not busy)
        last = all(self._skipped(p) for p in self.pages[self.index + 1:])
        self.next.SetLabel(_("&Finish") if last else _("&Next >"))
        self.next.Enable(not busy and self.next_allowed())

    def busy(self) -> bool:
        return False

    def next_allowed(self) -> bool:
        return True

    def finish(self):
        self.EndModal(wx.ID_OK)


def ask(parent, title: str, text: str, yes: str, no: str) -> bool:
    """A decision dialog. The message is in a read-only field so it can be reviewed line by line."""
    dialog = wx.Dialog(parent, title=title, size=(600, 380), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
    sizer = wx.BoxSizer(wx.VERTICAL)
    field = labeled(dialog, sizer, title, lambda p: read_only_text(p, text), proportion=1)
    buttons = wx.BoxSizer(wx.HORIZONTAL)
    yes_button = wx.Button(dialog, wx.ID_YES, label=yes)
    no_button = wx.Button(dialog, wx.ID_NO, label=no)
    buttons.Add(yes_button, 0, wx.ALL, 4)
    buttons.Add(no_button, 0, wx.ALL, 4)
    sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
    dialog.SetSizer(sizer)
    yes_button.Bind(wx.EVT_BUTTON, lambda e: dialog.EndModal(wx.ID_YES))
    no_button.Bind(wx.EVT_BUTTON, lambda e: dialog.EndModal(wx.ID_NO))
    dialog.SetEscapeId(wx.ID_NO)
    field.SetInsertionPoint(0)
    field.SetFocus()
    result = dialog.ShowModal() == wx.ID_YES
    dialog.Destroy()
    return result


def ask_text(parent, title: str, label: str, value: str = "") -> str | None:
    with wx.TextEntryDialog(parent, label, title, value) as dialog:
        if dialog.ShowModal() == wx.ID_OK:
            return dialog.GetValue().strip()
    return None
