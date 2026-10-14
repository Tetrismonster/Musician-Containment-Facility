"""The dialog offering a new version of MCF: what's new, then download, check and install."""

import logging
import webbrowser

import wx

from mcf import __version__, app_update
from mcf.announcer import Kind
from mcf.i18n import _
from mcf.ui.components import BackgroundTask, ProgressPanel, ask
from mcf.ui.widgets import labeled, read_only_text

log = logging.getLogger(__name__)


class UpdateDialog(wx.Dialog):
    """Ends with wx.ID_OK once the new version is ready and its installer has started; MCF must then close."""

    def __init__(self, frame, update: app_update.Update):
        super().__init__(frame, title=_("MCF update"), size=(720, 560),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.frame = frame
        self.update = update
        self.announcer = frame.announcer
        self.services = frame.services
        self.task: BackgroundTask | None = None

        sizer = wx.BoxSizer(wx.VERTICAL)
        heading = _("MCF version {new} is available. You have version {current}. What's new:").format(
            new=update.version_text, current=__version__)
        self.notes = labeled(self, sizer, heading, lambda p: read_only_text(
            p, update.notes or _("No release notes were given for this version."), height=260), proportion=1)
        self.progress = ProgressPanel(self, self.announcer, _("Update status:"))
        sizer.Add(self.progress, 0, wx.EXPAND | wx.ALL, 4)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.install = wx.Button(self, label=_("&Download and install"))
        self.page = wx.Button(self, label=_("Open the &release page"))
        self.later = wx.Button(self, wx.ID_CANCEL, label=_("&Later"))
        for button in (self.install, self.page, self.later):
            buttons.Add(button, 0, wx.ALL, 4)
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 4)
        self.SetSizer(sizer)

        self.install.Bind(wx.EVT_BUTTON, self.on_install)
        self.page.Bind(wx.EVT_BUTTON, self.on_page)
        self.later.Bind(wx.EVT_BUTTON, self.on_later)
        self.Bind(wx.EVT_CLOSE, self.on_later)
        self.SetEscapeId(wx.ID_CANCEL)
        self.page.Enable(update.page_url.startswith("https://"))
        self.notes.SetInsertionPoint(0)
        self.notes.SetFocus()

    def on_page(self, event):
        webbrowser.open(self.update.page_url)

    def on_install(self, event):
        if self.frame.job is not None or self.services.busy:
            self.announcer.announce(_("Wait for the running job, download or install to finish first."), Kind.INFO)
            return
        size = _("{size} MB").format(size=max(1, round(self.update.size / 1024**2))) if self.update.size \
            else _("an unknown size")
        text = _("MCF downloads version {version} ({size}) and checks that it's signed by the MCF project. "
                 "Then MCF closes, replaces its program files, and starts again.\n\n"
                 "Your settings, library, models, profiles and AI software are kept.").format(
            version=self.update.version_text, size=size)
        if not ask(self, _("Install the update?"), text, _("&Install"), _("&Not now")):
            return
        self.services.busy = True
        self.install.Disable()
        self.progress.reset()
        self.progress.focus_status()
        self.task = BackgroundTask(lambda status, progress, cancel: app_update.prepare(
            self.update, status, progress, cancel), self._status, self._progress, self._done)

    # Callbacks can arrive after Later has closed the dialog

    def _status(self, text: str):
        if self:
            self.progress.set_status(text)

    def _progress(self, fraction: float):
        if self:
            self.progress.set_progress(fraction, _("Downloading"))

    def _done(self, ok: bool, cancelled: bool, error: str):
        if not self:
            return
        self.services.busy = False
        self.task = None
        if cancelled:
            self.progress.set_status(_("Update cancelled."))
            self.install.Enable()
            return
        if not ok:
            self.progress.show_error(_("The update wasn't installed. {error}").format(error=error))
            self.install.Enable()
            return
        try:
            app_update.launch_installer(app_update.work_dir() / "stage", self.update.version_text)
        except OSError as exc:
            log.exception("Could not start the update")
            self.progress.show_error(_("The update couldn't be started: {error}").format(error=exc))
            self.install.Enable()
            return
        self.announcer.announce(_("The update is ready. MCF is closing to install it, and will start again."),
                                Kind.COMPLETION)
        self.EndModal(wx.ID_OK)

    def on_later(self, event):
        if self.task is not None:
            self.task.cancel()
            self.task.thread.join(timeout=10)
            self.services.busy = False
            self.task = None
        self.EndModal(wx.ID_CANCEL)
