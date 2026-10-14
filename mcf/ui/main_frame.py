"""The main window: the Generate and Library tabs. Models are managed in Preferences."""

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import wx

from mcf import APP_NAME, __version__, app_update, audio_export, guidance, hardware, hotkeys, jobs, lyrics, paths, recommend
from mcf.announcer import Kind
from mcf.estimates import describe, describe_duration
from mcf.i18n import _
from mcf.ui import widgets
from mcf.ui.components import RightsPanel, ask
from mcf.ui.generate_panel import GeneratePanel
from mcf.ui.library_panel import LibraryPanel

log = logging.getLogger(__name__)

HOTKEY_CANCEL_ID = 1

STAGE_TEXT = {
    "load": lambda: _("Loading the model."),
    "analyse": lambda: _("Preparing the source audio."),
    "plan": lambda: _("Planning the song."),
    "generate": lambda: _("Generating."),
    "decode": lambda: _("Decoding the audio."),
}
# Share of the progress bar for the stages before ACE-Step reports its own progress
STAGE_RANGE = {"analyse": (0.0, 0.1), "load": (0.1, 0.35)}


def error_text(code: str, message: str) -> str:
    texts = {
        "out_of_memory": _("The graphics card ran out of memory. Try a shorter song, or set the performance "
                           "mode to Compressed or Processor only in Preferences, on the Performance tab."),
        "model_missing": _("Some model files are missing. Open Preferences, Models, and choose Verify files."),
        "no_gpu": _("The AI software can't use the graphics card. Updating the NVIDIA graphics driver usually "
                    "fixes this. You can also choose Processor only in Preferences, Performance."),
        "disk_full": _("The disk is full. Free some space, or use Preferences, General, Clean up."),
        "crashed": _("The AI software stopped unexpectedly. Details are in the logs folder."),
        "too_long": _("That song is longer than 15 minutes. Covers work with songs up to 15 minutes long."),
        "too_large": _("That file is larger than 500 MB."),
        "unreadable": _("That file can't be read as audio. Use a WAV, FLAC, MP3 or OGG file."),
        "profile_mismatch": _("That artist profile doesn't fit this model. It was probably trained for a "
                              "different version."),
        "training_failed": _("Training failed. Details are in the logs folder."),
    }
    return texts.get(code) or _("It failed: {message}").format(message=message or code)


class RightsDialog(wx.Dialog):
    def __init__(self, parent, title: str, text: str, checkbox: str):
        super().__init__(parent, title=title, size=(640, 480), style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.panel = RightsPanel(self, title + ":", text, checkbox, on_change=self._update, height=280)
        sizer.Add(self.panel, 1, wx.EXPAND | wx.ALL, 6)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(sizer)
        self.ok = self.FindWindowById(wx.ID_OK)
        self.ok.SetLabel(_("&Continue"))
        self._update()
        self.panel.text.SetFocus()

    def _update(self):
        self.ok.Enable(self.panel.confirmed)


class MainFrame(wx.Frame):
    def __init__(self, app):
        super().__init__(None, title=APP_NAME, size=(960, 800))
        self.app = app
        self.settings = app.settings
        self.announcer = app.announcer
        self.services = app.services
        self.job: jobs.WorkerJob | None = None
        self.job_kind = ""
        self.job_model: dict | None = None
        self.request: dict = {}
        self._stage = ""
        self._hotkey_registered = False
        self._hotkey_suspended = False
        self._checking_updates = False

        self.CreateStatusBar()
        self.announcer.status_listeners.append(self.on_status)
        self.announcer.hwnd_provider = self.GetHandle

        self.notebook = wx.Notebook(self)
        self.generate_panel = GeneratePanel(self.notebook, self)
        self.library_panel = LibraryPanel(self.notebook, self)
        self.notebook.AddPage(self.generate_panel, _("Generate"))
        self.notebook.AddPage(self.library_panel, _("Library"))

        self._build_menus()
        self.Bind(wx.EVT_CHILD_FOCUS, self.on_child_focus)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self.Bind(wx.EVT_HOTKEY, lambda e: self.cancel(from_hotkey=True), id=HOTKEY_CANCEL_ID)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.apply_settings()
        self.generate_panel.model.SetFocus()

    # Menus

    def _build_menus(self):
        bar = wx.MenuBar()
        file_menu = wx.Menu()
        save = file_menu.Append(wx.ID_SAVEAS, _("&Save song as...\tCtrl+S"))
        prefs = file_menu.Append(wx.ID_PREFERENCES, _("&Preferences...\tCtrl+,"))
        file_menu.AppendSeparator()
        exit_item = file_menu.Append(wx.ID_EXIT, _("E&xit"))
        bar.Append(file_menu, _("&File"))

        gen_menu = wx.Menu()
        generate = gen_menu.Append(wx.ID_ANY, _("&Generate\tCtrl+Enter"))
        # Escape is handled in on_char_hook so it still closes lists and menus normally
        cancel = gen_menu.Append(wx.ID_ANY, _("&Cancel (Escape)"))
        check = gen_menu.Append(wx.ID_ANY, _("C&heck lyrics\tCtrl+Shift+K"))
        analyse = gen_menu.Append(wx.ID_ANY, _("&Analyse the song to cover\tCtrl+Shift+A"))
        estimate = gen_menu.Append(wx.ID_ANY, _("How &long will it take?\tCtrl+Shift+E"))
        bar.Append(gen_menu, _("&Generation"))

        view_menu = wx.Menu()
        pages = []
        for index, name in enumerate((_("&Generate tab"), _("&Library tab"))):
            pages.append((view_menu.Append(wx.ID_ANY, f"{name}\tCtrl+{index + 1}"), index))
        view_menu.AppendSeparator()
        repeat = view_menu.Append(wx.ID_ANY, _("&Repeat last announcement\tCtrl+Shift+R"))
        history = view_menu.Append(wx.ID_ANY, _("Announcement &history...\tCtrl+Shift+H"))
        bar.Append(view_menu, _("&View"))

        help_menu = wx.Menu()
        explain = help_menu.Append(wx.ID_HELP, _("&Explain focused control\tF1"))
        scan = help_menu.Append(wx.ID_ANY, _("&Hardware scan and model recommendations..."))
        speech = help_menu.Append(wx.ID_ANY, _("Which &speech output is in use?"))
        licenses = help_menu.Append(wx.ID_ANY, _("&Licenses and attribution..."))
        updates = help_menu.Append(wx.ID_ANY, _("Check for &updates...\tShift+F1"))
        about = help_menu.Append(wx.ID_ABOUT, _("&About {name}...").format(name="MCF"))
        bar.Append(help_menu, _("&Help"))

        self.SetMenuBar(bar)
        self.Bind(wx.EVT_MENU, lambda e: self.save_song(), save)
        self.Bind(wx.EVT_MENU, self.on_preferences, prefs)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), exit_item)
        self.Bind(wx.EVT_MENU, lambda e: self.generate(), generate)
        self.Bind(wx.EVT_MENU, lambda e: self.cancel(from_hotkey=True), cancel)
        self.Bind(wx.EVT_MENU, lambda e: self.check_lyrics(), check)
        self.Bind(wx.EVT_MENU, lambda e: self.analyse_cover(), analyse)
        self.Bind(wx.EVT_MENU, lambda e: self.announce_estimate(), estimate)
        for item, index in pages:
            self.Bind(wx.EVT_MENU, lambda e, i=index: self.show_page(i), item)
        self.Bind(wx.EVT_MENU, lambda e: self.announcer.repeat_last(), repeat)
        self.Bind(wx.EVT_MENU, self.on_history, history)
        self.Bind(wx.EVT_MENU, lambda e: self.explain_focused(), explain)
        self.Bind(wx.EVT_MENU, self.on_scan, scan)
        self.Bind(wx.EVT_MENU, lambda e: self.announcer.speak(
            _("Speech output: {name}").format(name=self.announcer.output_name()), True), speech)
        self.Bind(wx.EVT_MENU, self.on_licenses, licenses)
        self.Bind(wx.EVT_MENU, lambda e: self.check_for_updates(manual=True), updates)
        self.Bind(wx.EVT_MENU, self.on_about, about)

    def save_song(self):
        """Control+S saves the selected library song, or the newest one from the Generate tab."""
        library = self.library_panel
        if not library.songs:
            self.announcer.announce(_("There are no songs to save yet."), Kind.INFO)
            return
        if self.notebook.GetSelection() != 1:
            library.list.Select(0)
            library.list.Focus(0)
        library.save_as()

    # Settings

    def apply_settings(self):
        self._register_cancel_hotkey()

    def suspend_cancel_hotkey(self):
        """Release the system-wide shortcut while the user picks a new one."""
        self._hotkey_suspended = self._hotkey_registered
        if self._hotkey_registered:
            self.UnregisterHotKey(HOTKEY_CANCEL_ID)
            self._hotkey_registered = False

    def resume_cancel_hotkey(self):
        if self._hotkey_suspended:
            self._hotkey_suspended = False
            self._register_cancel_hotkey()

    def _register_cancel_hotkey(self):
        if self._hotkey_registered:
            self.UnregisterHotKey(HOTKEY_CANCEL_ID)
            self._hotkey_registered = False
        text = self.settings.get("keyboard.cancel_hotkey")
        try:
            hotkey = hotkeys.parse(text)
        except ValueError as exc:
            self.announcer.announce(str(exc), Kind.ERROR)
            return
        self._hotkey_registered = self.RegisterHotKey(HOTKEY_CANCEL_ID, hotkey.modifiers, hotkey.vk)
        if not self._hotkey_registered:
            self.announcer.announce(
                _("The cancel shortcut {key} is already used by another program. "
                  "Choose a different one in Preferences, on the Keyboard tab.")
                .format(key=hotkey.describe()), Kind.ERROR)

    # Startup

    def after_show(self):
        """Runs once the window is visible."""
        jobs.sweep_temp()
        if self.services.estimator.problem:
            self.announcer.announce(self.services.estimator.problem, Kind.ERROR)
        _profiles, damaged = self.services.profiles.list()
        if damaged:
            self.announcer.announce(_("{count} artist profiles can't be opened because they've been changed or "
                                      "damaged.").format(count=len(damaged)), Kind.ERROR)
        self._announce_update_result()
        if not self.settings.get("general.first_run_complete"):
            self.run_onboarding()
        elif app_update.available() and app_update.check_due(self.settings.get("general.update_check"),
                                                              self.settings.get("general.last_update_check")):
            self.settings.set("general.last_update_check", datetime.now(timezone.utc).isoformat())
            self.check_for_updates(manual=False)

    # Updates to MCF itself

    def _announce_update_result(self):
        result = app_update.take_result()
        if result is None:
            return
        if result.get("ok"):
            self.announcer.announce(_("MCF was updated to version {version}.").format(version=__version__),
                                    Kind.COMPLETION)
        else:
            self.announcer.announce(_("The update to version {version} failed, so this copy wasn't changed. "
                                      "{error} Details are in the logs folder, in update.log.").format(
                version=result.get("version", ""), error=result.get("error", "")), Kind.ERROR)

    def check_for_updates(self, manual: bool):
        """manual: from the Help menu or Shift+F1, so every outcome is announced. Automatic checks only
        announce a new version."""
        if self._checking_updates:
            if manual:
                self.announcer.announce(_("Still checking for updates."), Kind.INFO)
            return
        if not app_update.available():
            if manual:
                self.announcer.announce(_("Updates aren't set up in this version of MCF."), Kind.INFO)
            return
        if manual:
            self.announcer.announce(_("Checking for updates."), Kind.INFO)
        self._checking_updates = True

        def work():
            update, error = None, ""
            try:
                update = app_update.check()
            except app_update.UpdateError as exc:
                error = str(exc)
            wx.CallAfter(self._update_checked, update, error, manual)

        threading.Thread(target=work, daemon=True).start()

    def _update_checked(self, update, error: str, manual: bool):
        if not self:
            return
        self._checking_updates = False
        if error:
            log.warning("Update check: %s", error)
            if manual:
                self.announcer.announce(error, Kind.ERROR)
            return
        if update is None:
            if manual:
                self.announcer.announce(_("MCF is up to date. This is version {version}.").format(
                    version=__version__), Kind.COMPLETION)
            return
        if not manual:
            self.announcer.announce(_("MCF version {version} is available. Press Shift+F1 to hear what's new "
                                      "and install it.").format(version=update.version_text), Kind.INFO)
            return
        from mcf.ui.update_dialog import UpdateDialog
        with UpdateDialog(self, update) as dialog:
            installing = dialog.ShowModal() == wx.ID_OK
        if installing:
            self.Close()

    def run_onboarding(self):
        from mcf.ui.onboarding import SUGGESTED_LYRICS, SUGGESTED_STYLE, OnboardingWizard
        wizard = OnboardingWizard(self, self.app)
        result = wizard.ShowModal()
        installed = wizard.installed
        wizard.Destroy()
        self.refresh_everything()
        if result == wx.ID_OK and installed:
            panel = self.generate_panel
            panel.style.SetValue(SUGGESTED_STYLE)
            panel.lyrics.SetValue(SUGGESTED_LYRICS)
            panel.length.SetValue(60)
        self.show_page(0)

    def refresh_everything(self):
        self.generate_panel.refresh_models()
        self.generate_panel.refresh_profiles()
        self.library_panel.refresh()
        self.apply_settings()

    # Events

    def on_status(self, text: str):
        self.SetStatusText(text)

    def on_child_focus(self, event):
        event.Skip()
        if not self.settings.get("guidance.explain_on_focus"):
            return
        key = widgets.help_key_for(wx.Window.FindFocus())
        if key and key != "explanation":
            self.generate_panel.explanation.SetValue(self._explanation(key))

    def on_char_hook(self, event: wx.KeyEvent):
        if event.GetKeyCode() == wx.WXK_ESCAPE and event.GetModifiers() == 0 and self.job is not None:
            self.cancel()
            return
        event.Skip()

    def on_close(self, event):
        if self.services.busy and event.CanVeto():
            self.announcer.announce(_("A download or install is running. Cancel it first."), Kind.ERROR)
            event.Veto()
            return
        if self.job is not None:
            if event.CanVeto() and not ask(self, _("Exit MCF?"), _("A job is running. Exiting cancels it."),
                                           _("&Exit"), _("&Keep running")):
                event.Veto()
                return
            self.job.cancel()
        self.library_panel.player.unload()
        if self._hotkey_registered:
            self.UnregisterHotKey(HOTKEY_CANCEL_ID)
        try:
            self.settings.save()
        except OSError:
            log.exception("Could not save settings on exit")
        self.services.library.close()
        event.Skip()

    def on_preferences(self, event):
        from mcf.ui.preferences import PreferencesDialog
        if self.job is not None:
            self.announcer.announce(_("Preferences can't be changed while a job is running."), Kind.INFO)
            return
        library_dir = self.settings.library_dir()
        with PreferencesDialog(self, self.app) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.announcer.announce(_("Preferences saved."), Kind.COMPLETION)
            rerun = dialog.run_onboarding
        if self.settings.library_dir() != library_dir:
            self.library_panel.player.unload()
            self.services.reopen_library()
        self.refresh_everything()
        if rerun:
            self.run_onboarding()

    def on_history(self, event):
        lines = [f"{a.time:%H:%M:%S}  {a.text}" for a in self.announcer.history]
        text = "\n".join(lines) or _("No announcements yet.")
        with widgets.TextDialog(self, _("Announcement history"), text,
                                _("Announcements, oldest first:"), scroll_to_end=True) as dialog:
            dialog.ShowModal()

    def on_scan(self, event):
        with wx.BusyCursor():
            hw = hardware.scan()
        hardware.save_scan(hw)
        self.services.hardware = hw
        lines = [hw.summary(), ""]
        assessments = [recommend.assess(m, hw) for m in self.services.manifest["models"]]
        lines += [recommend.overall(assessments), ""]
        for model, a in zip(self.services.manifest["models"], assessments):
            lines.append(f"{model['name']}: {a.summary()}.")
            lines += a.reasons
            lines.append("")
        with widgets.TextDialog(self, _("What this computer can do"), "\n".join(lines).strip()) as dialog:
            dialog.ShowModal()

    def on_licenses(self, event):
        parts = []
        folder = paths.licenses_dir()
        for path in sorted(folder.glob("*.txt")) if folder.exists() else []:
            parts.append(f"{path.stem}\n{'=' * len(path.stem)}\n{path.read_text(encoding='utf-8')}")
        for engine_id in self.services.manifest["engines"]:
            credits = self.services.runtime_credits_file(engine_id)
            if credits.exists():
                parts.append(credits.read_text(encoding="utf-8"))
        text = "\n\n".join(parts) or _("License files are missing from this copy of MCF.")
        with widgets.TextDialog(self, _("Licenses and attribution"), text) as dialog:
            dialog.ShowModal()

    def on_about(self, event):
        text = _("{name}, version {version}.\n\n"
                 "Generate music with AI models that run on your own computer.\n\n"
                 "Released under the MIT license. See Help, Licenses and attribution.\n\n"
                 "The updater is adapted from Sensor Readout by Andre Louis, with permission."
                 ).format(name=APP_NAME, version=__version__)
        with widgets.TextDialog(self, _("About MCF"), text) as dialog:
            dialog.ShowModal()

    # Helpers

    def show_page(self, index: int):
        self.notebook.SetSelection(index)
        first = {0: self.generate_panel.model, 1: self.library_panel.list}[index]
        first.SetFocus()

    def _explanation(self, key: str) -> str:
        return guidance.explain(key, self.settings.get("guidance.beginner"))

    def explain_focused(self):
        key = widgets.help_key_for(wx.Window.FindFocus())
        if not key:
            self.announcer.speak(_("No explanation for this control."), interrupt=True)
            return
        text = self._explanation(key)
        self.generate_panel.explanation.SetValue(text)
        self.announcer.speak(text, interrupt=True)

    def check_lyrics(self):
        panel = self.generate_panel
        parsed = lyrics.parse(panel.lyrics.GetValue())
        report = lyrics.describe(parsed)
        _text, ignored = lyrics.for_ace_step(parsed)
        if ignored:
            report += "\n" + _("The selected model ignores section details, so these tags are sent as plain "
                               "section names: {tags}.").format(tags=", ".join(ignored))
        panel.explanation.SetValue(report)
        self.announcer.speak(report, interrupt=True)

    # Generation

    def generate_or_cancel(self):
        if self.job is not None:
            self.cancel()
        else:
            self.generate()

    def _current_model(self):
        model_id = self.generate_panel.model_id()
        model = self.services.model(model_id) if model_id else None
        if model is None:
            self.announcer.announce(_("No model is installed. Install one in Preferences, on the Models tab."),
                                    Kind.ERROR)
        return model

    def build_request(self) -> dict | None:
        """Everything the worker needs, or None after announcing what's wrong."""
        panel = self.generate_panel
        model = self._current_model()
        if model is None:
            return None
        parsed = lyrics.parse(panel.lyrics.GetValue())
        if parsed.problems:
            panel.explanation.SetValue(lyrics.describe(parsed))
            self.announcer.announce(_("The lyrics field has problems. They're listed in the Explanation field."),
                                    Kind.ERROR)
            return None
        tags = parsed.settings
        if "model" in tags:
            wanted = str(tags["model"]).lower()
            match = next((m for m in self.services.ready_models()
                          if wanted in (m["id"].lower(), m["name"].lower())), None)
            if match is None:
                self.announcer.announce(_("The model {name} in the lyrics isn't installed.").format(
                    name=tags["model"]), Kind.ERROR)
                return None
            model = match
        text, ignored = lyrics.for_ace_step(parsed)
        unsupported = [f"{{{name}}}" for name in tags if name not in model.get("tags", [])]
        ignored += unsupported
        mode = widgets.choice_value(panel.mode)
        if mode not in model["modes_supported"]:
            self.announcer.announce(_("{model} can't do this mode. Choose another mode or model.").format(
                model=model["name"]), Kind.ERROR)
            return None
        language = widgets.choice_value(panel.language)
        if "language" in tags:
            code = lyrics.language_code(str(tags["language"]))
            if code is None:
                self.announcer.announce(_("MCF doesn't know the language {name}.").format(name=tags["language"]),
                                        Kind.ERROR)
                return None
            language = code
        instrumental = bool(tags.get("instrumental", panel.instrumental.GetValue())) or not parsed.has_words
        spec = {
            "mode": mode,
            "caption": panel.style.GetValue().strip(),
            "lyrics": "" if instrumental else text,
            "instrumental": instrumental,
            "language": language,
            "duration": int(tags.get("length", panel.length.GetValue())),
            "seed": int(tags.get("seed", panel.seed.GetValue())),
            "steps": int(tags.get("steps", panel.steps.GetValue())),
            "guidance": float(tags.get("guidance", panel.guidance_scale.GetValue())),
            "bpm": tags.get("bpm"),
            "key": tags.get("key", ""),
            "time_signature": str(tags.get("time signature", "")).split("/")[0],
        }
        if not spec["caption"] and not text:
            self.announcer.announce(_("Write a style description or lyrics first."), Kind.ERROR)
            panel.style.SetFocus()
            return None
        credits = {}
        audio_seconds = float(spec["duration"])
        if mode in ("extend", "variation", "inpaint"):
            if panel.source.path is None or not panel.source.path.exists():
                self.announcer.announce(_("Choose a source song first."), Kind.ERROR)
                panel.source.field.SetFocus()
                return None
            spec["src_audio"] = str(panel.source.path)
            source_seconds = audio_export.duration(panel.source.path)
            audio_seconds = source_seconds
            if mode == "extend":
                spec["source_seconds"] = source_seconds
                spec["extend_seconds"] = panel.extend_seconds.GetValue()
                audio_seconds += spec["extend_seconds"]
            elif mode == "inpaint":
                start, end = panel.inpaint_start.GetValue(), panel.inpaint_end.GetValue()
                if end <= start or start >= source_seconds:
                    self.announcer.announce(_("The section to replace must end after it starts, and start within "
                                              "the song, which is {seconds:.0f} seconds long.").format(
                        seconds=source_seconds), Kind.ERROR)
                    return None
                spec["repaint_start"], spec["repaint_end"] = start, min(end, source_seconds)
            else:
                spec["cover_strength"] = float(widgets.choice_value(panel.variation))
        elif mode == "reference":
            if panel.reference.path is None:
                self.announcer.announce(_("Choose a reference recording first."), Kind.ERROR)
                return None
            spec["reference_audio"] = str(panel.reference.path)
        elif mode == "cover":
            cover = panel.cover
            if cover.source.path is None:
                self.announcer.announce(_("Choose the song to cover first."), Kind.ERROR)
                cover.source.field.SetFocus()
                return None
            if cover.analysis is None or cover.analysed_path != cover.source.path:
                self.announcer.announce(_("Analyse the song first, with the Analyse the song button or "
                                          "Control+Shift+A."), Kind.ERROR)
                cover.analyse.SetFocus()
                return None
            if not self._confirm_cover_rights(model):
                return None
            spec["source"] = str(cover.source.path)
            spec["cover"] = cover.cover_spec()
            credits = cover.credits()
            follow = spec["cover"]["follow"]
            audio_seconds = cover.analysis["duration"]
            if not follow["length"]:
                audio_seconds = min(audio_seconds, spec["cover"]["max_seconds"])
            limit = model.get("max_seconds")
            if limit and audio_seconds > limit + 1:
                self.announcer.announce(
                    _("This song is {song} seconds long, but {model} makes at most {limit} seconds. Untick Length "
                      "and set a maximum length of {limit} seconds or less.").format(
                        song=round(audio_seconds), model=model["name"], limit=limit), Kind.ERROR)
                cover.checks["length"].SetFocus()
                return None
            if follow["instrumentation"] and not spec["caption"] and cover.analysis.get("caption"):
                spec["caption"] = cover.analysis["caption"]
            if follow["key"] and not spec["key"]:
                spec["key"] = cover.analysis.get("key", "")

        profile = None
        profile_name = tags.get("profile")
        if profile_name:
            profile = next((p for p in panel.profile_list if p.name.lower() == str(profile_name).lower()), None)
            if profile is None:
                self.announcer.announce(_("There's no artist profile called {name} for {model}.").format(
                    name=profile_name, model=model["name"]), Kind.ERROR)
                return None
        else:
            profile = panel.selected_profile()

        if ignored:
            self.announcer.announce(_("{model} doesn't use these, so they're ignored: {tags}.").format(
                model=model["name"], tags=", ".join(ignored)), Kind.INFO)
        return {"model": model, "spec": spec, "credits": credits, "profile": profile,
                "profile_strength": panel.profile_strength.GetValue(), "audio_seconds": audio_seconds,
                "values": panel.values()}

    def estimate_for(self, request: dict):
        model = request["model"]
        perf = self.services.performance_for(model)
        assessment = self.services.assessment(model)
        timing_mode = perf["mode"]
        if timing_mode == "auto":
            timing_mode = assessment.mode if assessment and assessment.mode else "offload"
        spec = request["spec"]
        planner = perf["planner"] is not None and spec["mode"] in ("new", "reference")
        baseline = recommend.baseline_seconds(model, timing_mode, request["audio_seconds"], planner)
        task = "generate"
        if spec["mode"] == "cover":
            task = "cover"
            baseline += recommend.analysis_baseline(request["audio_seconds"], timing_mode) / 2
        estimate = self.services.estimator.estimate(model["id"], task, perf["mode"], self.services.fingerprint(),
                                                    request["audio_seconds"], baseline)
        return estimate, task

    def announce_estimate(self):
        request = self.build_request()
        if request is None:
            return
        estimate, _task = self.estimate_for(request)
        text = _("This will take {time}.").format(time=describe(estimate))
        if estimate.runs == 0:
            text += " " + _("This is a first estimate. MCF learns this computer's speed from real runs.")
        self.announcer.announce(text, Kind.INFO, interrupt=True)

    def _confirm_cover_rights(self, model: dict) -> bool:
        if model.get("cover_method") == "score":
            how = _("{model} follows a written score of the melody and chords that MCF makes from its analysis. "
                    "The original recording never reaches the model.").format(model=model["name"])
        else:
            how = _("MCF gives the model the original vocal line, reduced to coarse codes, so it can follow the "
                    "melody and lyrics. The model then sings the song again from scratch, with the voice and "
                    "instruments your style description asks for. If you also keep the instrumentation, the full "
                    "original mix is used.")
        self.announcer.announce(how, Kind.HINT)
        if self.settings.get("cover.rights_confirmed"):
            return True
        text = _(
            "A cover is a new performance of an existing song. The song itself still belongs to its writers and "
            "publisher.\n\n"
            "Making a cover for personal use is usually fine. Releasing one, including online, normally needs a "
            "cover (mechanical) license from the song's publisher, and the original writers must be credited. "
            "MCF puts the credits you enter into the library and into saved files.\n\n"
            "{how}\n\n"
            "MCF gives information, not legal advice.").format(how=how)
        with RightsDialog(self, _("Covers and rights"), text,
                          _("I understand, and I'll only release covers I have a license for")) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return False
        self.settings.set("cover.rights_confirmed", True)
        self.settings.save()
        return True

    def generate(self):
        if self.job is not None:
            self.announcer.announce(_("A job is already running."), Kind.INFO)
            return
        if self.services.busy:
            self.announcer.announce(_("Wait for the download or install to finish first."), Kind.INFO)
            return
        request = self.build_request()
        if request is None:
            return
        model = request["model"]
        estimate, task = self.estimate_for(request)
        threshold = self.settings.get("performance.long_generation_warning_minutes")
        if threshold and estimate.high > threshold * 60:
            if not ask(self, _("This will take a long time"),
                       _("This will take {time} on this computer.").format(time=describe(estimate)),
                       _("&Start"), _("&Don't start")):
                return
        secret = None
        profile = request["profile"]
        if profile is not None:
            try:
                with wx.BusyCursor():
                    _profile, secret = self.services.profiles.adapter(profile.id)
            except Exception as exc:
                self.announcer.announce(str(exc), Kind.ERROR)
                return
        payload = self.services.job_payload(model)
        payload["generate"] = request["spec"]
        if secret:
            payload["lora_scale"] = request["profile_strength"]
        request.update(estimate=estimate, task=task)
        self.request = request
        self._start_job("generate", model, payload, secret)
        del secret
        self.announcer.announce(_("Generating. This will take {time}.").format(time=describe(estimate)),
                                Kind.INFO)

    def _start_job(self, kind: str, model: dict, payload: dict, secret: bytes | None = None):
        panel = self.generate_panel
        panel.progress.reset()
        self._stage = ""
        self.job_kind = kind
        self.job_model = model
        self.job = jobs.WorkerJob(model["engine"], kind, payload, self.settings.get("performance.worker_priority"),
                                  lambda m: wx.CallAfter(self._on_job_message, m), self._on_job_finished,
                                  secret=secret)
        panel.set_running(True)

    def _on_job_message(self, message: dict):
        panel = self.generate_panel
        kind = message.get("type")
        if kind == "stage":
            self._stage = message.get("name", "")
            if self.job_kind == "analyse":
                return
            text = STAGE_TEXT.get(self._stage, lambda: "")()
            if text:
                panel.progress.set_status(text)
        elif kind == "progress":
            fraction = float(message.get("fraction", 0))
            if self.job_kind == "analyse":
                panel.progress.set_progress(fraction, _("Analysing"))
                return
            if self._stage in STAGE_RANGE:
                low, high = STAGE_RANGE[self._stage]
                overall = low + (high - low) * fraction
            else:
                overall = 0.35 + 0.65 * fraction  # ACE-Step reports its own overall progress
            panel.progress.set_progress(overall)
        elif kind == "notice" and message.get("code") == "planner_failed":
            self.announcer.announce(_("The song planner couldn't be loaded, so this song is made without it."),
                                    Kind.INFO)

    def _on_job_finished(self, outcome: jobs.JobOutcome):
        """Runs on the worker's reader thread, before the job folder is deleted."""
        song = None
        error = ""
        if outcome.ok and self.job_kind == "generate":
            try:
                song = self._add_to_library(outcome)
            except Exception as exc:
                log.exception("Adding the song to the library failed")
                error = _("The song was made, but adding it to the library failed: {error}").format(error=exc)
        if outcome.ok:
            try:
                self._record_timing(outcome)
            except Exception:
                log.exception("Recording the timing failed")
        wx.CallAfter(self._job_done, outcome, song, error)

    def _add_to_library(self, outcome: jobs.JobOutcome):
        request = self.request
        spec = request["spec"]
        result = outcome.result
        path = Path(result["file"])
        seconds = audio_export.duration(path)
        credits = request["credits"]
        if spec["mode"] == "cover" and credits.get("original_title"):
            base = _("Cover of {title}").format(title=credits["original_title"])
        else:
            first = next((l for l in spec["lyrics"].splitlines() if l.strip() and not l.startswith("[")), "")
            base = first[:60] or spec["caption"][:60] or _("Untitled")
        settings = {"seed": result.get("seed"), "steps": spec["steps"], "guidance": spec["guidance"],
                    "bpm": result.get("bpm") or spec.get("bpm"), "key": result.get("key") or spec.get("key"),
                    "language": spec["language"], "values": request["values"]}
        if spec["mode"] == "cover":
            settings["follow"] = spec["cover"]["follow"]
        profile = request["profile"]
        library = self.services.library
        return library.add(path, library.unique_title(base), request["model"]["name"], spec["mode"], seconds,
                           spec["caption"], spec["lyrics"], settings, credits, profile.name if profile else "")

    def _record_timing(self, outcome: jobs.JobOutcome):
        if self.job_kind == "generate":
            request = self.request
            self.services.estimator.record(request["model"]["id"], request["task"],
                                           self.services.performance_for(request["model"])["mode"],
                                           self.services.fingerprint(), request["audio_seconds"],
                                           outcome.total_seconds, outcome.stages)
        elif self.job_kind == "analyse":
            self.services.estimator.record(self.job_model["id"], "analyse", "any", self.services.fingerprint(),
                                           max(1.0, outcome.result.get("duration", 1.0)), outcome.total_seconds,
                                           outcome.stages)

    def _job_done(self, outcome: jobs.JobOutcome, song, error: str):
        panel = self.generate_panel
        kind = self.job_kind
        self.job = None
        panel.set_running(False)
        if outcome.cancelled:
            panel.progress.status.SetValue(_("Cancelled."))
            return
        if not outcome.ok:
            text = error_text(outcome.error_code, outcome.error_message)
            panel.progress.status.SetValue(text)
            self.announcer.announce(text, Kind.ERROR)
            return
        panel.progress.set_progress(1.0)
        if kind == "analyse":
            self._analysis_done(outcome.result)
            return
        if error:
            panel.progress.status.SetValue(error)
            self.announcer.announce(error, Kind.ERROR)
            return
        took = describe_duration(outcome.total_seconds)
        panel.progress.status.SetValue(_("Finished in {time}: {title}.").format(time=took, title=song.title))
        self.library_panel.refresh(song.id)
        self.announcer.announce(_("Song added to the library: {title}. It took {time}.").format(
            title=song.title, time=took), Kind.COMPLETION)

    def cancel(self, from_hotkey: bool = False):
        if self.job is None:
            if from_hotkey:
                self.announcer.announce(_("Nothing is running."), Kind.INFO, interrupt=True)
            return
        self.job.cancel()
        what = _("Analysis cancelled.") if self.job_kind == "analyse" else _("Generation cancelled.")
        self.announcer.announce(what, Kind.COMPLETION, interrupt=True)

    # Cover analysis

    def analyse_cover(self):
        panel = self.generate_panel
        cover = panel.cover
        if self.job is not None:
            self.announcer.announce(_("A job is already running."), Kind.INFO)
            return
        if widgets.choice_value(panel.mode) != "cover":
            widgets.set_choice_value(panel.mode, "cover")
            panel.update_visibility()
        if not self.services.cover_tools_ready():
            self.announcer.announce(_("Covers need the cover analysis tools. Install them in Preferences, on the "
                                      "Models tab."), Kind.ERROR)
            return
        if cover.source.path is None:
            self.announcer.announce(_("Choose the song to cover first."), Kind.ERROR)
            cover.source.field.SetFocus()
            return
        model = self._current_model()
        if model is None:
            return
        payload = self.services.job_payload(model)
        perf = payload["performance"]
        payload["analyse"] = {"audio": str(cover.source.path), "mode": perf["mode"],
                              "describe": cover.follow()["instrumentation"], "model_dir": payload["model_dir"],
                              "planner": perf["planner"],
                              "language": widgets.choice_value(panel.language)}
        try:
            duration = audio_export.duration(cover.source.path)
        except Exception:
            duration = 200
        baseline = recommend.analysis_baseline(duration, perf["mode"])
        estimate = self.services.estimator.estimate(model["id"], "analyse", "any", self.services.fingerprint(),
                                                    duration, baseline)
        # The analysis always runs in the cover tools' AI software, whichever model makes the cover
        tools_engine = self.services.manifest["cover_tools"]["engine"]
        payload["engine"] = tools_engine
        self._start_job("analyse", dict(model, engine=tools_engine), payload)
        panel.progress.set_status(_("Analysing the song. This takes {time}.").format(time=describe(estimate)))

    def _analysis_done(self, result: dict):
        panel = self.generate_panel
        panel.cover.set_analysis(result)
        follow = panel.cover.follow()
        if follow["lyrics"] and result.get("lyrics"):
            panel.lyrics.SetValue(result["lyrics"])
        if not result.get("lyrics"):
            panel.instrumental.SetValue(True)
        if follow["instrumentation"] and result.get("caption") and not panel.style.GetValue().strip():
            panel.style.SetValue(result["caption"])
        panel.progress.status.SetValue(_("Analysis finished."))
        self.announcer.announce(_("Analysis finished. Tempo about {bpm:.0f}, key {key}. The results are in the "
                                  "Analysis result field.").format(bpm=result.get("bpm") or 0,
                                                                   key=result.get("key", "")), Kind.COMPLETION)
        panel.cover.summary.SetFocus()
