"""The onboarding wizard, shown the first time MCF starts.

It can be run again from Preferences, General. Answers are saved as the user
goes, so cancelling keeps what was already chosen.
"""

from pathlib import Path

import wx

from mcf import hardware, hotkeys, models, paths, recommend, runtime, setup_tasks
from mcf.announcer import Kind
from mcf.estimates import describe_duration
from mcf.i18n import _
from mcf.ui import widgets
from mcf.ui.components import (BackgroundTask, FolderPicker, ProgressPanel, RightsPanel, WizardDialog, ask)
from mcf.ui.widgets import labeled, read_only_text

GB = 1024**3
SUGGESTED_STYLE = "warm acoustic pop, gentle female vocals, acoustic guitar, soft drums"
SUGGESTED_LYRICS = "[Verse]\nMorning light across the floor\nCoffee cooling by the door\n\n[Chorus]\nStay a while, stay a while\nLet the day go slow"


def default_export_folder() -> Path:
    return Path.home() / "Music"


def rights_text() -> str:
    return _(
        "Only use material you have the rights to.\n\n"
        "Songs MCF makes can still sound like existing music. Check before releasing anything.\n\n"
        "Covers of songs you don't own are usually fine for personal use. Releasing one normally needs "
        "a cover (mechanical) license from the song's publisher.\n\n"
        "Imitating a real singer's voice needs their permission.\n\n"
        "MCF gives information, not legal advice.")


def license_text(model: dict) -> str:
    lic = model["license"]
    parts = [
        _("{model} is made by {authors}. License: {name}.").format(
            model=model["name"], authors=lic["authors"], name=lic["name"]),
        _("Commercial use: {answer}.").format(answer=_("allowed") if lic.get("commercial") else _("not allowed")),
    ]
    if lic.get("notes"):
        parts.append(lic["notes"])
    parts.append(_("Website: {url}").format(url=lic["url"]))
    text_file = paths.licenses_dir() / lic.get("file", "")
    if lic.get("file") and text_file.exists():
        parts.append(_("Full license text:") + "\n" + text_file.read_text(encoding="utf-8"))
    parts.append(rights_text())
    return "\n\n".join(parts)


class OnboardingWizard(WizardDialog):
    def __init__(self, parent, app):
        super().__init__(parent, _("Welcome to MCF"), app.announcer)
        self.app = app
        self.settings = app.settings
        self.services = app.services
        self.hw: hardware.Hardware | None = self.services.hardware
        self.assessments: dict[str, recommend.Assessment] = {}
        self.model: dict | None = None
        self.task: BackgroundTask | None = None
        self.installed = False
        self.new_hotkey = self.settings.get("keyboard.cancel_hotkey")

        self.add_page(_("Welcome"), self._build_welcome, can_leave=self._leave_welcome)
        self.add_page(_("Where files go"), self._build_storage, on_enter=self._enter_storage,
                      can_leave=self._leave_storage)
        self.add_page(_("Hardware scan"), self._build_scan, can_leave=self._leave_scan)
        self.add_page(_("What this computer can do"), self._build_results, on_enter=self._enter_results,
                      can_leave=self._leave_results)
        self.add_page(_("What to expect"), self._build_expect, on_enter=self._enter_expect,
                      skip=lambda: not self.settings.get("guidance.beginner"))
        self.add_page(_("Speech"), self._build_speech, can_leave=self._leave_speech)
        self.add_page(_("Cancel shortcut"), self._build_hotkey, can_leave=self._leave_hotkey)
        self.add_page(_("License and rights"), self._build_license, on_enter=self._enter_license,
                      skip=lambda: self.model is None)
        self.add_page(_("Download and install"), self._build_install, on_enter=self._enter_install,
                      can_leave=self._leave_install, skip=lambda: self.model is None)
        self.add_page(_("Finished"), self._build_finish, on_enter=self._enter_finish)
        self.start()

    def _save(self):
        try:
            self.settings.save()
        except OSError as exc:
            self.announcer.announce(_("Could not save settings: {error}").format(error=exc), Kind.ERROR)

    # 1. Welcome

    def _build_welcome(self, panel, sizer):
        text = _(
            "MCF makes music with AI models that run on this computer.\n\n"
            "This wizard sets where files go, checks what this computer can do, and installs a model. "
            "Every choice here can be changed later in Preferences, which opens with Control+comma.\n\n"
            "MCF tells you plainly when something won't work, will take a long time, or will give poor "
            "results, together with the reason. It doesn't soften that or apologise for it. This is the only "
            "place MCF mentions it.")
        self.welcome_text = labeled(panel, sizer, _("About MCF:"), lambda p: read_only_text(p, text, 190))
        sizer.Add(wx.StaticText(panel, label=_("Are you familiar with using AI models?")), 0, wx.ALL, 4)
        self.familiar_yes = wx.RadioButton(panel, label=_("&Yes"), style=wx.RB_GROUP)
        self.familiar_no = wx.RadioButton(panel, label=_("N&o, I'm new to this"))
        beginner = self.settings.get("guidance.beginner")
        self.familiar_yes.SetValue(not beginner)
        self.familiar_no.SetValue(beginner)
        sizer.Add(self.familiar_yes, 0, wx.ALL, 4)
        sizer.Add(self.familiar_no, 0, wx.ALL, 4)

    def _leave_welcome(self) -> bool:
        self.settings.set("guidance.beginner", self.familiar_no.GetValue())
        self._save()
        return True

    # 2. Storage

    def _build_storage(self, panel, sizer):
        self.models_picker = FolderPicker(panel, _("&Models folder:"), self.settings.get("storage.models_dir"),
                                          paths.default_models_dir(), "models_dir")
        sizer.Add(self.models_picker, 0, wx.EXPAND)
        self.export_picker = FolderPicker(panel, _("&Exported audio folder:"),
                                          self.settings.get("saving.default_folder"),
                                          default_export_folder(), "export_dir")
        sizer.Add(self.export_picker, 0, wx.EXPAND)
        self.storage_advice = labeled(panel, sizer, _("Advice:"), lambda p: read_only_text(p, "", 200),
                                      proportion=1)

    def _enter_storage(self):
        if self.settings.get("guidance.beginner"):
            advice = _(
                "Models folder: where the AI models are stored.\n"
                "Models are large, typically 4 to 20 GB each. Use an internal drive with plenty of free space, "
                "preferably a solid-state drive (SSD), because models load much faster from one. Avoid USB "
                "sticks and network drives: models load slowly from them, and a generation fails if the drive "
                "disconnects.\n"
                "The default, the models folder inside MCF, keeps everything together and portable. Choose "
                "another drive only if this one is short of space.\n\n"
                "Exported audio folder: where your finished songs go when you save them with Control+S. Your "
                "Music folder is the usual choice.\n\n"
                "The AI software itself, about {runtime} GB, always goes in the runtime folder inside MCF.").format(
                runtime=self._runtime_gb())
        else:
            advice = _("The AI software, about {runtime} GB, goes in the runtime folder inside MCF. Models are "
                       "typically 4 to 20 GB each.").format(runtime=self._runtime_gb())
        self.storage_advice.SetValue(advice)

    def _runtime_gb(self) -> int:
        return max(e["installed_gb"] for e in self.services.manifest["engines"].values())

    def _leave_storage(self) -> bool:
        problems = []
        models_path = self.models_picker.full_path()
        kind = hardware.drive_kind(models_path)
        if kind == "removable":
            problems.append(_("The models folder is on a removable drive. Models will load slowly, and "
                              "generations fail if the drive is disconnected."))
        elif kind == "network":
            problems.append(_("The models folder is on a network drive. Models will load slowly, and "
                              "generations fail if the connection drops."))
        need = max(sum(models.component_size(c) for c in m["components"] if c["required"])
                   for m in self.services.manifest["models"])
        free = hardware.free_bytes(models_path)
        if free is not None and free < need * 1.1:
            problems.append(_("The drive with the models folder has {free:.1f} GB free. The smallest model "
                              "needs {need:.1f} GB.").format(free=free / GB, need=need / GB))
        runtime_free = hardware.free_bytes(paths.runtime_dir())
        if runtime_free is not None and runtime_free < self._runtime_gb() * GB * 1.1:
            problems.append(_("The drive MCF is on has {free:.1f} GB free. The AI software needs about "
                              "{need} GB there.").format(free=runtime_free / GB, need=self._runtime_gb()))
        if problems and not ask(self, _("Check these folders"), "\n\n".join(problems),
                                _("&Keep these folders"), _("&Choose other folders")):
            self.models_picker.path.SetFocus()
            return False
        old_models = self.settings.models_dir()
        self.settings.set("storage.models_dir", self.models_picker.value())
        self.settings.set("saving.default_folder", self.export_picker.value())
        if self.settings.models_dir() != old_models and old_models.exists() and any(old_models.iterdir()):
            self.announcer.announce(_("Models already in the old folder weren't moved. Use Preferences, Models, "
                                      "Move models to move them."), Kind.INFO)
        self._save()
        return True

    # 3. Hardware scan

    def _build_scan(self, panel, sizer):
        text = _("MCF can scan this computer to find out what it can run. The scan looks at the graphics "
                 "card and its memory, the system memory (RAM), free disk space and the processor.\n\n"
                 "It takes about a second. Nothing is sent online and nothing is installed.")
        labeled(panel, sizer, _("About the scan:"), lambda p: read_only_text(p, text, 120))
        sizer.Add(wx.StaticText(panel, label=_("Can MCF scan this computer's hardware?")), 0, wx.ALL, 4)
        self.scan_yes = wx.RadioButton(panel, label=_("&Yes, scan it"), style=wx.RB_GROUP)
        self.scan_no = wx.RadioButton(panel, label=_("N&o, skip the scan"))
        self.scan_yes.SetValue(True)
        sizer.Add(self.scan_yes, 0, wx.ALL, 4)
        sizer.Add(self.scan_no, 0, wx.ALL, 4)

    def _leave_scan(self) -> bool:
        if self.scan_yes.GetValue():
            with wx.BusyCursor():
                self.hw = hardware.scan()
            hardware.save_scan(self.hw)
            self.services.hardware = self.hw
            self.announcer.announce(_("Scan complete."), Kind.COMPLETION)
        else:
            self.hw = None
        return True

    # 4. Results

    def _build_results(self, panel, sizer):
        self.results = labeled(panel, sizer, _("&Results:"), lambda p: read_only_text(p, "", 300), proportion=1)
        self.model_choice = labeled(panel, sizer, _("Model to &install:"), lambda p: wx.Choice(p), "install_choice")

    def _enter_results(self):
        self.model_choice.Clear()
        self.model_choice_ids: list[str | None] = []
        if self.hw is None:
            self.results.SetValue(_(
                "The hardware scan was skipped, so MCF can't tell which models this computer can run or how "
                "long songs will take.\n\n"
                "You can still install a model, but it may not run. You can run the scan later from the Help "
                "menu, or by running this wizard again from Preferences, General."))
            for model in self.services.manifest["models"]:
                self.model_choice.Append(model["name"])
                self.model_choice_ids.append(model["id"])
        else:
            lines = [self.hw.summary(), ""]
            self.assessments = {m["id"]: recommend.assess(m, self.hw) for m in self.services.manifest["models"]}
            lines.append(recommend.overall(list(self.assessments.values())))
            lines.append("")
            for model in self.services.manifest["models"]:
                a = self.assessments[model["id"]]
                lines.append(f"{model['name']}: {a.summary()}.")
                lines.extend(a.reasons)
                if a.mode:
                    lines.append(_("Performance mode: {mode}.").format(mode=recommend.mode_label(a.mode)))
                lines.append("")
                if a.mode:
                    self.model_choice.Append(f"{model['name']}: {a.summary()}")
                    self.model_choice_ids.append(model["id"])
            if not self.model_choice_ids:
                lines.append(self._nothing_runs_text())
            self.results.SetValue("\n".join(lines).strip())
        self.model_choice.Append(_("Don't install a model now"))
        self.model_choice_ids.append(None)
        self.model_choice.SetSelection(0)
        self.results.SetInsertionPoint(0)

    def _nothing_runs_text(self) -> str:
        model = self.services.manifest["models"][0]
        req = model["requirements"]
        return _(
            "What would be needed: an NVIDIA graphics card from the GTX 16 series, RTX 20 series or newer, with "
            "at least {vram} GB of graphics memory, and at least {ram} GB of system memory (RAM). Without a "
            "suitable graphics card, at least {cpu_ram} GB of system memory lets the processor do the work, "
            "very slowly.\n\n"
            "MCF will still open, but it can't generate music on this computer.").format(
            vram=req["offload_min_vram_gb"], ram=req["offload_min_ram_gb"], cpu_ram=req["cpu_min_ram_gb"])

    def _leave_results(self) -> bool:
        index = self.model_choice.GetSelection()
        model_id = self.model_choice_ids[index] if index >= 0 else None
        self.model = self.services.model(model_id) if model_id else None
        if self.model:
            self.settings.set("generation.model", self.model["id"])
            self._save()
        return True

    # 5. What to expect

    def _build_expect(self, panel, sizer):
        self.expect = labeled(panel, sizer, _("&What happens next:"), lambda p: read_only_text(p, "", 360),
                              proportion=1)

    def _enter_expect(self):
        parts = []
        model = self.model
        if model:
            size = self._install_size(model)
            parts.append(_("1. Downloads. MCF downloads the AI software and {model}: about {gb:.1f} GB in total. "
                           "On a 50 megabit connection that takes about {time}.").format(
                model=model["name"], gb=size / GB, time=describe_duration(size * 8 / 50e6)))
            a = self.assessments.get(model["id"])
            if a and a.mode:
                parts.append(_("2. Each song takes time. On this computer, a 2-minute song takes {time}. "
                               "Longer songs take longer. MCF tells you the estimate before each song, and the "
                               "estimates get more accurate as MCF learns how fast this computer is.").format(
                    time=describe_duration(a.song_seconds)))
                if a.verdict == "slow":
                    parts.append(_("That is slow. Expect to wait several minutes for every song."))
            parts.append(_("3. Loading comes first. Every generation starts by loading the model into memory, "
                           "which takes one to three minutes. MCF unloads it afterwards, so the computer "
                           "stays responsive between songs."))
            parts.append(_("4. Results vary. The same settings give a different song each time. It's normal to "
                           "make several versions and keep the best one. Models sometimes mispronounce or skip "
                           "lyrics, and the first attempt at a style is often not the best."))
            parts.append(_("5. The computer works hard. While a song generates, the graphics card is busy, "
                           "the computer may feel slower, and the fans may get loud. MCF runs at low priority "
                           "so your screen reader stays responsive."))
            parts.append(_("6. You can always stop. Escape cancels a generation, and so does the system-wide "
                           "cancel shortcut on the next page, even when MCF isn't the active window."))
        else:
            parts.append(_("No model will be installed now, so MCF can't make music yet. You can install one "
                           "later in Preferences, on the Models tab."))
        self.expect.SetValue("\n\n".join(parts))
        self.expect.SetInsertionPoint(0)

    def _install_components(self, model: dict) -> list[dict]:
        wanted = {c["id"] for c in model["components"] if c["required"]}
        a = self.assessments.get(model["id"])
        if a and a.planner:
            wanted.add(a.planner)
        return [c for c in model["components"] if c["id"] in wanted]

    def _install_size(self, model: dict) -> int:
        store = self.services.store
        size = sum(models.component_size(c) for c in self._install_components(model)
                   if not store.is_component_installed(model["id"], c))
        if not runtime.is_installed(self.services.engine_for(model)):
            size += self.services.engine_for(model)["download_gb"] * GB
        return size

    # 6. Speech

    def _build_speech(self, panel, sizer):
        self.verbosity = labeled(panel, sizer, _("How much MCF &speaks:"), lambda p: widgets.choice(p, [
            ("quiet", _("Quiet: only errors and when things finish")),
            ("normal", _("Normal")),
            ("detailed", _("Detailed: includes every progress step")),
        ], self.settings.get("speech.verbosity")), "verbosity")
        test = wx.Button(panel, label=_("&Test speech"))
        test.Bind(wx.EVT_BUTTON, lambda e: self.announcer.speak(
            _("This is how MCF sounds. Speaking through: {name}").format(name=self.announcer.output_name()), True))
        sizer.Add(test, 0, wx.ALL, 4)
        labeled(panel, sizer, _("Note:"), lambda p: read_only_text(
            p, _("Errors are always announced. Everything MCF announces is also kept in View, Announcement "
                 "history."), 60))

    def _leave_speech(self) -> bool:
        self.settings.set("speech.verbosity", widgets.choice_value(self.verbosity))
        self._save()
        return True

    # 7. Cancel shortcut

    def _build_hotkey(self, panel, sizer):
        labeled(panel, sizer, _("About the cancel shortcut:"), lambda p: read_only_text(
            p, _("This shortcut stops a generation even when MCF isn't the active window, for example if your "
                 "screen reader becomes slow while the computer is busy. The default is Control+Alt+Shift+C. "
                 "Choose a combination your screen reader and other programs don't use."), 100))
        self.hotkey_text = labeled(panel, sizer, _("Current &shortcut:"), lambda p: wx.TextCtrl(
            p, value=hotkeys.describe(self.new_hotkey), style=wx.TE_READONLY))
        change = wx.Button(panel, label=_("C&hange..."))
        change.Bind(wx.EVT_BUTTON, self._change_hotkey)
        sizer.Add(change, 0, wx.ALL, 4)

    def _change_hotkey(self, event):
        from mcf.ui.preferences import HotkeyCaptureDialog
        with HotkeyCaptureDialog(self) as dialog:
            if dialog.ShowModal() == wx.ID_OK and dialog.hotkey:
                self.new_hotkey = dialog.hotkey.text
                self.hotkey_text.SetValue(hotkeys.describe(self.new_hotkey))
                self.announcer.announce(_("Cancel shortcut set to {key}.").format(
                    key=hotkeys.describe(self.new_hotkey)), Kind.INFO)
        self.hotkey_text.SetFocus()

    def _leave_hotkey(self) -> bool:
        self.settings.set("keyboard.cancel_hotkey", self.new_hotkey)
        self._save()
        return True

    # 8. License

    def _build_license(self, panel, sizer):
        self.rights = RightsPanel(panel, _("&License and rights:"), "",
                                  _("I've read this, and I'll only use material I have the rights to"),
                                  on_change=self.update_buttons, height=330)
        sizer.Add(self.rights, 1, wx.EXPAND)

    def _enter_license(self):
        self.rights.set_text(license_text(self.model))

    # 9. Install

    def _build_install(self, panel, sizer):
        self.install_summary = labeled(panel, sizer, _("What will be installed:"),
                                       lambda p: read_only_text(p, "", 150))
        self.install_button = wx.Button(panel, label=_("&Start download"))
        self.install_button.Bind(wx.EVT_BUTTON, lambda e: self._start_install())
        sizer.Add(self.install_button, 0, wx.ALL, 4)
        self.progress = ProgressPanel(panel, self.announcer, _("&Status:"))
        sizer.Add(self.progress, 0, wx.EXPAND)

    def _enter_install(self):
        model = self.model
        comps = self._install_components(model)
        lines = []
        engine = self.services.engine_for(model)
        if not runtime.is_installed(engine):
            lines.append(_("The AI software for {name}: about {gb} GB to download, {installed} GB installed.").format(
                name=engine["name"], gb=engine["download_gb"], installed=engine["installed_gb"]))
        store = self.services.store
        for c in comps:
            state = _("already installed") if store.is_component_installed(model["id"], c) else models.gb(
                models.component_size(c))
            lines.append(f"{c['name']}: {state}.")
        estimate = self.services.estimator.download_estimate(self._install_size(model))
        if estimate:
            from mcf.estimates import describe
            lines.append(_("Estimated download time: {time}.").format(time=describe(estimate)))
        lines.append(_("Every file is checked against a known fingerprint before it's used. Downloads resume "
                       "where they stopped if you cancel or the connection drops."))
        self.install_summary.SetValue("\n".join(lines))
        if self.installed or (store.is_installed(model) and self.services.runtime_ready(model)
                              and all(store.is_component_installed(model["id"], c) for c in comps)):
            self.installed = True
            self.progress.set_status(_("Everything is installed."), announce=False)
            self.install_button.Disable()

    def _start_install(self):
        self.progress.reset()
        self.install_button.Disable()
        self.services.busy = True
        task = setup_tasks.install_model(self.services, self.model, self._install_components(self.model))
        self.task = BackgroundTask(task, self.progress.set_status,
                                   lambda f: self.progress.set_progress(f, _("Installing")), self._install_done)
        self.update_buttons()
        # The focused button was just disabled, which would leave the screen reader with nothing focused
        self.progress.focus_status()
        self.progress.set_status(_("Starting download."))

    def _install_done(self, ok: bool, cancelled: bool, error: str):
        self.task = None
        self.services.busy = False
        if ok:
            self.installed = True
            self.progress.set_progress(1.0)
            self.progress.status.SetValue(_("Everything is installed."))
            self.announcer.announce(_("{model} is installed and ready.").format(model=self.model["name"]),
                                    Kind.COMPLETION)
        elif cancelled:
            self.progress.status.SetValue(_("Download cancelled. Start it again to resume."))
            self.announcer.announce(_("Download cancelled."), Kind.COMPLETION, interrupt=True)
            self.install_button.Enable()
        else:
            self.install_button.SetLabel(_("&Try again"))
            self.install_button.Enable()
            self.update_buttons()
            self.progress.show_error(_("Installing failed: {error}").format(error=error))
            return
        self.update_buttons()
        (self.next if ok else self.install_button).SetFocus()

    def _leave_install(self) -> bool:
        if self.installed:
            return True
        return ask(self, _("Not installed"),
                   _("{model} isn't installed yet, so MCF can't make music until it is. You can install it later "
                     "in Preferences, on the Models tab.").format(model=self.model["name"]),
                   _("&Continue without installing"), _("&Stay on this page"))

    def busy(self) -> bool:
        return self.task is not None

    def cancel_busy(self):
        if self.task:
            self.task.cancel()
            self.progress.set_status(_("Cancelling."))

    def next_allowed(self) -> bool:
        page = self.pages[self.index]["title"] if 0 <= self.index < len(self.pages) else ""
        if page == _("License and rights"):
            return self.rights.confirmed
        return True

    # 10. Finished

    def _build_finish(self, panel, sizer):
        self.finish_text = labeled(panel, sizer, _("&Summary:"), lambda p: read_only_text(p, "", 300),
                                   proportion=1)

    def _enter_finish(self):
        if self.installed:
            text = _(
                "MCF is ready.\n\n"
                "To make your first song, go to the Generate tab, fill in the style description and lyrics, then "
                "press Generate or Control+Enter. Here's something to try:\n\n"
                "Style description: {style}\n\n"
                "Lyrics:\n{lyrics}\n\n"
                "Set the length to 60 seconds for a quicker first result.\n\n"
                "Pressing Finish fills these in for you.").format(style=SUGGESTED_STYLE, lyrics=SUGGESTED_LYRICS)
        elif self.model is None and self.hw is not None and not any(
                a.mode and a.verdict != "impractical" for a in self.assessments.values()):
            text = _("MCF will open, but this computer can't run any of its models, so it can't make music here. "
                     "The Library tab still works for songs made elsewhere.")
        else:
            text = _("MCF will open without a model, so it can't make music yet. Install one in Preferences, on "
                     "the Models tab. Preferences opens with Control+comma.")
        self.finish_text.SetValue(text)

    def finish(self):
        self.settings.set("general.first_run_complete", True)
        self._save()
        self.EndModal(wx.ID_OK)

    def on_cancel(self, event):
        if self.busy():
            self.cancel_busy()
            return
        if ask(self, _("Close the wizard?"),
               _("Setup isn't finished. What you've chosen so far is saved. You can run this wizard again from "
                 "Preferences, General."), _("&Close the wizard"), _("&Keep going")):
            self.settings.set("general.first_run_complete", True)
            self._save()
            self.EndModal(wx.ID_CANCEL)
