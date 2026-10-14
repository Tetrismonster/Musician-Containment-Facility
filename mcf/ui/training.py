"""The training wizard: trains an add-on on a set of songs and saves it as an artist profile."""

import logging
import time
from pathlib import Path

import wx

from mcf import jobs, recommend
from mcf.announcer import Kind
from mcf.estimates import describe
from mcf.i18n import _
from mcf.ui import widgets
from mcf.ui.components import ProgressPanel, RightsPanel, WizardDialog, ask
from mcf.ui.generate_panel import AUDIO_WILDCARD, LANGUAGES
from mcf.ui.widgets import labeled, read_only_text

log = logging.getLogger(__name__)

AUDIO_TYPES = {".wav", ".flac", ".mp3", ".ogg"}
LENGTHS = [("50", lambda: _("Short: 50 passes over the songs. Quicker, captures less.")),
           ("100", lambda: _("Standard: 100 passes (recommended)")),
           ("200", lambda: _("Long: 200 passes. Captures more, and risks copying the songs too closely."))]


class TrainingWizard(WizardDialog):
    def __init__(self, parent, app):
        super().__init__(parent, _("New artist profile"), app.announcer)
        self.app = app
        self.settings = app.settings
        self.services = app.services
        self.model = None
        ready = self.services.ready_models()
        model_id = self.settings.get("generation.model")
        self.model = next((m for m in ready if m["id"] == model_id), ready[0] if ready else None)
        self.check = recommend.assess_training(self.model, self.services.hardware) if self.model else None
        self.files: list[dict] = []
        self.job: jobs.WorkerJob | None = None
        self.done = False
        self.profile_name = ""

        self.add_page(_("About training"), self._build_intro)
        self.add_page(_("Rights"), self._build_rights, skip=self._cant_train)
        self.add_page(_("Songs to learn from"), self._build_files, can_leave=self._leave_files, skip=self._cant_train)
        self.add_page(_("Name the profile"), self._build_name, can_leave=self._leave_name, skip=self._cant_train)
        self.add_page(_("Training length"), self._build_length, on_enter=self._enter_length, skip=self._cant_train)
        self.add_page(_("Train"), self._build_train, can_leave=lambda: self.done, skip=self._cant_train)
        self.start()

    def _cant_train(self) -> bool:
        return self.check is None or not self.check.possible

    # 1. Intro

    def _build_intro(self, panel, sizer):
        text = _(
            "Training teaches a model the sound of a set of songs: the style, the instruments and the voice. The "
            "result is an artist profile you can pick when making songs and covers.\n\n"
            "You need at least 8 songs, and 12 or more give better results. Training takes hours, and the "
            "computer works hard the whole time.\n\n"
            "Profiles stay inside the MCF folder, encrypted. They can't be exported.")
        if self.model is None:
            text += "\n\n" + _("No model is installed, so there's nothing to train. Install one in Preferences, "
                               "on the Models tab.")
        elif not self.check.possible:
            text += "\n\n" + _("This computer can't train:") + "\n" + "\n".join(self.check.reasons) + "\n\n" + _(
                "Profiles trained on another computer work here: train there, then copy the whole MCF folder back.")
        else:
            text += "\n\n" + _("This computer can train {model}.").format(model=self.model["name"])
            if self.check.reasons:
                text += "\n" + "\n".join(self.check.reasons)
        labeled(panel, sizer, _("About training:"), lambda p: read_only_text(p, text, 330), proportion=1)

    # 2. Rights

    def _build_rights(self, panel, sizer):
        text = _(
            "Only train on audio you own or have permission to use.\n\n"
            "Training on someone's recordings teaches the model their voice and sound. Imitating a real singer "
            "or artist needs their permission, including for their voice and likeness, especially if you share "
            "the results.\n\n"
            "Good uses: your own music, your own voice, a band you're in, or recordings you have a license for.\n\n"
            "MCF gives information, not legal advice.")
        self.rights = RightsPanel(panel, _("&Rights:"), text,
                                  _("I own this audio or have permission to use it, including the voice and "
                                    "likeness of the people performing on it"),
                                  on_change=self.update_buttons, height=260)
        sizer.Add(self.rights, 1, wx.EXPAND)

    # 3. Files

    def _build_files(self, panel, sizer):
        self.file_list = labeled(panel, sizer, _("&Songs:"), lambda p: wx.ListBox(p, size=(-1, 150)), "train_files")
        self.file_list.Bind(wx.EVT_LISTBOX, lambda e: self._show_file())
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in ((_("Add &files..."), self._add_files), (_("Add a f&older..."), self._add_folder),
                               (_("&Remove"), self._remove_file)):
            button = wx.Button(panel, label=label)
            button.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(button, 0, wx.ALL, widgets.LABEL_BORDER)
        sizer.Add(buttons)
        self.file_caption = labeled(panel, sizer, _("&Description of the selected song (optional):"),
                                    lambda p: wx.TextCtrl(p), "train_caption")
        self.file_lyrics = labeled(panel, sizer, _("L&yrics of the selected song (optional):"),
                                   lambda p: wx.TextCtrl(p, style=wx.TE_MULTILINE, size=(-1, 90)), "train_lyrics")
        self.file_caption.Bind(wx.EVT_TEXT, lambda e: self._store_file())
        self.file_lyrics.Bind(wx.EVT_TEXT, lambda e: self._store_file())
        self.language = labeled(panel, sizer, _("Lyrics lang&uage:"), lambda p: widgets.choice(
            p, LANGUAGES, self.settings.get("generation.language")), "language")
        self.file_count = labeled(panel, sizer, _("Count:"), lambda p: wx.TextCtrl(p, style=wx.TE_READONLY))
        self._refresh_files()

    def _add_paths(self, found: list[Path]):
        known = {f["path"] for f in self.files}
        added = 0
        for path in found:
            if path.suffix.lower() in AUDIO_TYPES and str(path) not in known:
                self.files.append({"path": str(path), "caption": "", "lyrics": ""})
                added += 1
        self._refresh_files()
        self.announcer.announce(_("{n} songs added.").format(n=added), Kind.INFO)

    def _add_files(self, event):
        with wx.FileDialog(self, _("Choose songs"), wildcard=_(AUDIO_WILDCARD),
                           style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self._add_paths([Path(p) for p in dialog.GetPaths()])
        self.file_list.SetFocus()

    def _add_folder(self, event):
        with wx.DirDialog(self, _("Choose a folder of songs")) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self._add_paths(sorted(Path(dialog.GetPath()).rglob("*")))
        self.file_list.SetFocus()

    def _remove_file(self, event):
        index = self.file_list.GetSelection()
        if index != wx.NOT_FOUND:
            del self.files[index]
            self._refresh_files()
        self.file_list.SetFocus()

    def _refresh_files(self):
        self.file_list.Set([Path(f["path"]).name for f in self.files])
        if self.files:
            self.file_list.SetSelection(0)
        self._show_file()
        req = self.model["training"] if self.model else {"min_songs": 8, "recommended_songs": 12}
        self.file_count.SetValue(_("{n} songs. At least {min} are needed, {rec} or more recommended.").format(
            n=len(self.files), min=req["min_songs"], rec=req["recommended_songs"]))

    def _show_file(self):
        index = self.file_list.GetSelection()
        item = self.files[index] if index != wx.NOT_FOUND else None
        self._loading = True
        self.file_caption.ChangeValue(item["caption"] if item else "")
        self.file_lyrics.ChangeValue(item["lyrics"] if item else "")
        self.file_caption.Enable(item is not None)
        self.file_lyrics.Enable(item is not None)
        self._loading = False

    def _store_file(self):
        index = self.file_list.GetSelection()
        if index != wx.NOT_FOUND and not getattr(self, "_loading", False):
            self.files[index]["caption"] = self.file_caption.GetValue().strip()
            self.files[index]["lyrics"] = self.file_lyrics.GetValue().strip()

    def _leave_files(self) -> bool:
        need = self.model["training"]["min_songs"]
        if len(self.files) < need:
            self.announcer.announce(_("Add at least {need} songs. There are {n} now.").format(
                need=need, n=len(self.files)), Kind.ERROR)
            return False
        missing = [f for f in self.files if not Path(f["path"]).exists()]
        if missing:
            self.announcer.announce(_("{n} of the songs can't be found any more.").format(n=len(missing)), Kind.ERROR)
            return False
        return True

    # 4. Name

    def _build_name(self, panel, sizer):
        self.name = labeled(panel, sizer, _("Profile &name:"), lambda p: wx.TextCtrl(p), "profile_name")
        self.style = labeled(panel, sizer, _("Default &style description:"), lambda p: wx.TextCtrl(p), "style")
        self.notes = labeled(panel, sizer, _("N&otes:"), lambda p: wx.TextCtrl(
            p, style=wx.TE_MULTILINE, size=(-1, 100)))
        labeled(panel, sizer, _("Note:"), lambda p: read_only_text(
            p, _("The style description is used for songs that have no description of their own, and is filled "
                 "in on the Generate tab when you pick this profile."), 60))

    def _leave_name(self) -> bool:
        name = self.name.GetValue().strip()
        if not name:
            self.announcer.announce(_("Give the profile a name."), Kind.ERROR)
            self.name.SetFocus()
            return False
        existing, _damaged = self.services.profiles.list()
        if any(p.name.lower() == name.lower() for p in existing):
            self.announcer.announce(_("There's already a profile called {name}.").format(name=name), Kind.ERROR)
            self.name.SetFocus()
            return False
        return True

    # 5. Length

    def _build_length(self, panel, sizer):
        self.length = labeled(panel, sizer, _("Training &length:"), lambda p: widgets.choice(
            p, [(k, f()) for k, f in LENGTHS], "100"), "train_length")
        self.length.Bind(wx.EVT_CHOICE, lambda e: self._enter_length())
        self.estimate = labeled(panel, sizer, _("&Estimate:"), lambda p: read_only_text(p, "", 140))

    def _estimate(self):
        epochs = int(widgets.choice_value(self.length))
        baseline = recommend.training_baseline(self.model, len(self.files), epochs)
        return self.services.estimator.estimate(self.model["id"], "train", "gpu", self.services.fingerprint(),
                                                len(self.files) * epochs, baseline)

    def _enter_length(self):
        estimate = self._estimate()
        lines = [_("Training {n} songs will take {time} on this computer.").format(
            n=len(self.files), time=describe(estimate))]
        if estimate.runs == 0:
            lines.append(_("This is a first estimate. MCF hasn't timed training on this computer yet."))
        lines.append(_("The graphics card is fully busy the whole time. Leave the computer on and plugged in, "
                       "and don't start other heavy work."))
        lines.append(_("Cancelling stops training, and nothing is saved."))
        self.estimate.SetValue("\n".join(lines))

    # 6. Train

    def _build_train(self, panel, sizer):
        self.start_button = wx.Button(panel, label=_("&Start training"))
        self.start_button.Bind(wx.EVT_BUTTON, lambda e: self._start())
        sizer.Add(self.start_button, 0, wx.ALL, widgets.LABEL_BORDER)
        self.progress = ProgressPanel(panel, self.announcer, _("Stat&us:"))
        sizer.Add(self.progress, 0, wx.EXPAND)

    def _start(self):
        epochs = int(widgets.choice_value(self.length))
        estimate = self._estimate()
        threshold = self.settings.get("performance.long_generation_warning_minutes")
        if threshold and estimate.high > threshold * 60 and not ask(
                self, _("Training takes a long time"), _("Training will take {time}.").format(time=describe(estimate)),
                _("&Start"), _("&Don't start")):
            return
        language = widgets.choice_value(self.language)
        spec = dict(self.check.settings, epochs=epochs, style=self.style.GetValue().strip(),
                    files=[dict(f, language=language) for f in self.files])
        payload = self.services.job_payload(self.model)
        payload["train"] = spec
        self.profile_name = self.name.GetValue().strip()
        self.started = time.monotonic()
        self.epochs = epochs
        self.start_button.Disable()
        self.progress.reset()
        self.job = jobs.WorkerJob(self.model["engine"], "train", payload, self.settings.get("performance.worker_priority"),
                                  lambda m: wx.CallAfter(self._on_message, m), self._on_finished)
        self.update_buttons()
        self.progress.set_status(_("Training started. This will take {time}.").format(time=describe(estimate)))

    def _on_message(self, message: dict):
        kind = message.get("type")
        if kind == "stage":
            name = message.get("name")
            if name == "preprocess":
                self.progress.set_status(_("Preparing the songs."))
            elif name == "train":
                self.progress.set_status(_("Training."))
        elif kind == "progress":
            self.progress.set_progress(float(message.get("fraction", 0)))

    def _on_finished(self, outcome: jobs.JobOutcome):
        """Runs on the reader thread, before the job folder (and the plain add-on) is deleted."""
        error = ""
        if outcome.ok:
            try:
                self.services.profiles.create(
                    self.profile_name, self.model["id"], Path(outcome.result["adapter_dir"]),
                    style=self.style.GetValue().strip(), notes=self.notes.GetValue().strip(),
                    songs=len(self.files), rights_confirmed=time.time())
                self.services.estimator.record(self.model["id"], "train", "gpu", self.services.fingerprint(),
                                               len(self.files) * self.epochs, outcome.total_seconds, outcome.stages)
            except Exception as exc:
                log.exception("Saving the profile failed")
                error = str(exc)
        wx.CallAfter(self._finished_ui, outcome, error)

    def _finished_ui(self, outcome: jobs.JobOutcome, error: str):
        self.job = None
        if outcome.cancelled:
            self.progress.status.SetValue(_("Training cancelled. Nothing was saved."))
            self.start_button.Enable()
        elif not outcome.ok or error:
            from mcf.ui.main_frame import error_text
            text = error or error_text(outcome.error_code, outcome.error_message)
            self.progress.status.SetValue(text)
            self.announcer.announce(_("Training failed: {error}").format(error=text), Kind.ERROR)
            self.start_button.Enable()
        else:
            self.done = True
            self.progress.set_progress(1.0)
            text = _("The profile {name} is saved. Pick it in the Artist profile list on the Generate tab.").format(
                name=self.profile_name)
            self.progress.status.SetValue(text)
            self.announcer.announce(text, Kind.COMPLETION)
        self.update_buttons()

    # Wizard hooks

    def busy(self) -> bool:
        return self.job is not None

    def cancel_busy(self):
        if self.job and ask(self, _("Cancel training?"), _("Cancelling stops training, and nothing is saved."),
                            _("&Cancel training"), _("&Keep training")):
            self.job.cancel()
            self.progress.set_status(_("Cancelling."))

    def next_allowed(self) -> bool:
        title = self.pages[self.index]["title"] if 0 <= self.index < len(self.pages) else ""
        if title == _("Rights"):
            return self.rights.confirmed
        if title == _("Train"):
            return self.done
        return True
