"""Preferences dialog, opened with Ctrl+comma. It also holds the Model Manager and artist profiles."""

import logging
import threading
import time
from pathlib import Path

import wx

from mcf import hardware, hotkeys, jobs, models, paths, recommend, runtime, setup_tasks
from mcf.announcer import Kind
from mcf.i18n import _
from mcf.ui import widgets
from mcf.ui.components import BackgroundTask, FolderPicker, ProgressPanel, ask, ask_text
from mcf.ui.widgets import labeled, read_only_text, tag

log = logging.getLogger(__name__)

GB = 1024**3


def _checkbox(parent, sizer, label, value, help_key=None):
    box = wx.CheckBox(parent, label=label)
    box.SetValue(value)
    if help_key:
        tag(box, help_key)
    sizer.Add(box, 0, wx.ALL, widgets.LABEL_BORDER)
    return box


class HotkeyCaptureDialog(wx.Dialog):
    """Waits for the user to press a new shortcut."""

    def __init__(self, parent):
        super().__init__(parent, title=_("Press the new shortcut"))
        self.hotkey = None
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.prompt = labeled(self, sizer, _("Instructions:"), lambda p: widgets.read_only_text(
            p, _("Press the key combination you want, for example Control+Alt+Shift+C. "
                 "Use at least one of Control, Alt, Shift or Windows together with a letter, "
                 "number or function key. Press Escape to keep the current shortcut."), height=80))
        self.SetSizer(sizer)
        self.Fit()
        # The dialog's key hook sees every key first. Listening on the text field missed
        # combinations that Windows or the field handled before passing them on.
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self.prompt.SetFocus()
        # While capturing, the current cancel shortcut must reach this dialog instead of cancelling
        self.frame = getattr(wx.GetApp(), "frame", None)
        if self.frame is not None:
            self.frame.suspend_cancel_hotkey()

    def Destroy(self):
        if self.frame is not None:
            self.frame.resume_cancel_hotkey()
            self.frame = None
        return super().Destroy()

    def on_key(self, event: wx.KeyEvent):
        modifiers, vk = event.GetModifiers(), event.GetRawKeyCode()
        log.info("Shortcut capture: modifiers=%s key code=%s", modifiers, vk)
        if event.GetKeyCode() == wx.WXK_ESCAPE and modifiers == 0:
            self.EndModal(wx.ID_CANCEL)
            return
        try:
            hotkey = hotkeys.from_key_event(modifiers, vk)
        except ValueError as exc:
            wx.GetApp().announcer.speak(str(exc), interrupt=True)
            return
        if hotkey is None:
            return  # Only a modifier so far
        self.hotkey = hotkey
        self.EndModal(wx.ID_OK)


class ProfileDetailsDialog(wx.Dialog):
    def __init__(self, parent, profile):
        super().__init__(parent, title=_("Profile details"), size=(560, 460),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.name = labeled(self, sizer, _("&Name:"), lambda p: wx.TextCtrl(p, value=profile.name))
        self.style = labeled(self, sizer, _("Default &style description:"), lambda p: wx.TextCtrl(
            p, value=profile.style))
        self.strength = labeled(self, sizer, _("Default s&trength:"), lambda p: wx.SpinCtrlDouble(
            p, min=0.1, max=1.5, initial=profile.strength, inc=0.1), expand=False)
        self.notes = labeled(self, sizer, _("N&otes:"), lambda p: wx.TextCtrl(
            p, value=profile.notes, style=wx.TE_MULTILINE, size=(-1, 120)), proportion=1)
        sizer.Add(self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, 6)
        self.SetSizer(sizer)
        self.name.SetFocus()


class PreferencesDialog(wx.Dialog):
    def __init__(self, parent, app):
        super().__init__(parent, title=_("Preferences"), size=(780, 700),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.app = app
        self.settings = app.settings
        self.announcer = app.announcer
        self.services = app.services
        self.cancel_hotkey = self.settings.get("keyboard.cancel_hotkey")
        self.run_onboarding = False
        self.task: BackgroundTask | None = None

        sizer = wx.BoxSizer(wx.VERTICAL)
        self.notebook = wx.Notebook(self)
        self._general_page()
        self._models_page()
        self._profiles_page()
        self._speech_page()
        self._storage_page()
        self._saving_page()
        self._performance_page()
        self._generation_page()
        self._keyboard_page()
        sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 8)
        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizer(sizer)
        self.Bind(wx.EVT_BUTTON, self.on_ok, id=wx.ID_OK)
        self.Bind(wx.EVT_BUTTON, self.on_cancel, id=wx.ID_CANCEL)
        self.Bind(wx.EVT_CLOSE, self.on_cancel)
        self.notebook.SetFocus()

    def _page(self, title):
        panel = wx.ScrolledWindow(self.notebook)
        panel.SetScrollRate(0, 20)
        sizer = wx.BoxSizer(wx.VERTICAL)
        panel.SetSizer(sizer)
        self.notebook.AddPage(panel, title)
        return panel, sizer

    # General

    def _general_page(self):
        s = self.settings
        panel, sizer = self._page(_("General"))
        self.language = labeled(panel, sizer, _("&Language:"), lambda p: widgets.choice(p, [
            ("system", _("Windows default")),
        ], s.get("general.language")))
        from mcf import manifest_update
        if manifest_update.available():
            label = _("&Check online for updates to the list of available models")
        else:
            label = _("&Check online for updates to the list of available models (not available in this version)")
        self.check_updates = _checkbox(panel, sizer, label, s.get("general.check_model_list_updates"))
        self.check_updates.Enable(manifest_update.available())
        from mcf import app_update
        self.update_check = labeled(panel, sizer, _("Check for new versions of &MCF:"), lambda p: widgets.choice(p, [
            ("startup", _("Every time MCF starts")),
            ("daily", _("Once a day")),
            ("weekly", _("Once a week")),
            ("never", _("Never")),
        ], s.get("general.update_check")), "update_check")
        self.update_check.Enable(app_update.available())
        self.beginner = _checkbox(panel, sizer, _("I'm &new to AI models: include beginner explanations"),
                                  s.get("guidance.beginner"), "beginner")
        wizard = wx.Button(panel, label=_("Run the &onboarding wizard again"))
        wizard.Bind(wx.EVT_BUTTON, self.on_run_onboarding)
        sizer.Add(wizard, 0, wx.ALL, widgets.LABEL_BORDER)
        self.usage = labeled(panel, sizer, _("Disk space &used by MCF:"), lambda p: read_only_text(p, "", 150),
                             "clean_up")
        clean = tag(wx.Button(panel, label=_("Clean &up temporary files and caches")), "clean_up")
        clean.Bind(wx.EVT_BUTTON, self.on_clean_up)
        sizer.Add(clean, 0, wx.ALL, widgets.LABEL_BORDER)
        self.update_usage()

    def update_usage(self):
        """Add up disk usage on a background thread.

        The AI software is tens of thousands of files. Right after a generation
        Windows has usually dropped them from its cache, and counting them on the
        UI thread froze the dialog.
        """
        self.usage.SetValue(_("Calculating..."))
        folders = {
            "runtime": paths.runtime_dir(), "cache": runtime.cache_dir(), "home": runtime.home_dir(),
            "models": self.settings.models_dir(), "library": self.settings.library_dir(),
            "profiles": paths.profiles_dir(), "temp": paths.temp_dir(), "logs": paths.logs_dir(),
        }

        def work():
            sizes = {key: runtime.installed_size(folder) if folder.exists() else 0
                     for key, folder in folders.items()}
            wx.CallAfter(self._show_usage, sizes)

        threading.Thread(target=work, daemon=True).start()

    def _show_usage(self, sizes: dict[str, int]):
        if not self:  # The dialog closed while counting
            return
        caches = sizes["cache"] + sizes["home"]
        rows = [
            (_("AI software (runtime folder)"), sizes["runtime"] - caches),
            (_("Models"), sizes["models"]),
            (_("Library"), sizes["library"]),
            (_("Artist profiles"), sizes["profiles"]),
            (_("Caches that can be rebuilt"), caches),
            (_("Temporary files"), sizes["temp"]),
            (_("Logs"), sizes["logs"]),
        ]
        self.usage.SetValue("\n".join(f"{name}: {amount / GB:.2f} GB" for name, amount in rows))

    def on_clean_up(self, event):
        if self.task is not None or self.app.frame.job is not None:
            self.announcer.announce(_("Wait for the running job to finish first."), Kind.INFO)
            return
        with wx.BusyCursor():
            before = runtime.installed_size(paths.temp_dir()) if paths.temp_dir().exists() else 0
            freed = runtime.clean_caches()
            jobs.sweep_temp()
            freed += before
        self.update_usage()
        self.announcer.announce(_("Cleaned up. Freed {gb:.2f} GB.").format(gb=freed / GB), Kind.COMPLETION)

    def on_run_onboarding(self, event):
        if self.task is not None:
            self.announcer.announce(_("Wait for the download to finish, or cancel it first."), Kind.INFO)
            return
        if self._save():
            self.run_onboarding = True
            self.EndModal(wx.ID_OK)

    # Models

    def _models_page(self):
        panel, sizer = self._page(_("Models"))
        self.model_list = labeled(panel, sizer, _("&Models and tools:"), lambda p: wx.ListCtrl(
            p, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, size=(-1, 150)), "models_list")
        for title, width in ((_("Name"), 200), (_("Status"), 150), (_("On this computer"), 300)):
            self.model_list.AppendColumn(title, width=width)
        self.model_list.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda e: self.show_model_details())
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        self.model_buttons = []
        for label, handler in ((_("&Install..."), self.on_install), (_("&Remove"), self.on_remove),
                               (_("&Verify files"), self.on_verify), (_("Repair the AI so&ftware"), self.on_repair),
                               (_("View &license"), self.on_view_license), (_("Mo&ve models..."), self.on_move)):
            button = tag(wx.Button(panel, label=label), "models_list")
            button.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(button, 0, wx.ALL, widgets.LABEL_BORDER)
            self.model_buttons.append(button)
        sizer.Add(buttons, 0, wx.EXPAND)
        self.cancel_task_button = wx.Button(panel, label=_("&Cancel the download"))
        self.cancel_task_button.Bind(wx.EVT_BUTTON, lambda e: self.cancel_task())
        self.cancel_task_button.Disable()
        sizer.Add(self.cancel_task_button, 0, wx.ALL, widgets.LABEL_BORDER)
        self.model_progress = ProgressPanel(panel, self.announcer, _("Stat&us:"))
        sizer.Add(self.model_progress, 0, wx.EXPAND)
        self.model_details = labeled(panel, sizer, _("&Details:"), lambda p: read_only_text(p, "", 220),
                                     proportion=1)
        self.refresh_models()

    def _model_rows(self) -> list[tuple[str, dict | None]]:
        rows = [(m["id"], m) for m in self.services.manifest["models"]]
        rows.append((models.COVER_TOOLS_ID, None))
        return rows

    def refresh_models(self):
        selected = max(0, self.model_list.GetFirstSelected())
        self.model_list.DeleteAllItems()
        store = self.services.store
        for index, (item_id, model) in enumerate(self._model_rows()):
            if model is None:
                tools = self.services.manifest["cover_tools"]
                installed = self.services.cover_tools_ready()
                size = sum(models.component_size(c) for c in tools["components"])
                self.model_list.InsertItem(index, tools["name"])
                self.model_list.SetItem(index, 1, _("Installed") if installed else _("Not installed"))
                self.model_list.SetItem(index, 2, _("Needed for covers. {size} download.").format(
                    size=models.gb(size)))
                continue
            self.model_list.InsertItem(index, model["name"])
            if store.is_installed(model) and self.services.runtime_ready(model):
                status = _("Installed")
            elif store.is_installed(model):
                status = _("AI software missing")
            elif store.installed_components(model):
                status = _("Partly installed")
            else:
                status = _("Not installed")
            self.model_list.SetItem(index, 1, status)
            assessment = self.services.assessment(model)
            self.model_list.SetItem(index, 2, assessment.summary() if assessment else _("Hardware not scanned"))
        if self.model_list.GetItemCount():
            self.model_list.Select(min(selected, self.model_list.GetItemCount() - 1))
        self.show_model_details()

    def selected_item(self) -> tuple[str, dict | None] | None:
        index = self.model_list.GetFirstSelected()
        rows = self._model_rows()
        return rows[index] if 0 <= index < len(rows) else None

    def show_model_details(self):
        item = self.selected_item()
        if item is None:
            return
        item_id, model = item
        store = self.services.store
        lines = []
        if model is None:
            tools = self.services.manifest["cover_tools"]
            lines += [tools["name"], tools["summary"], ""]
            for c in tools["components"]:
                state = _("installed") if store.is_component_installed(item_id, c) else _("not installed")
                lines.append(f"{c['name']}: {models.gb(models.component_size(c))}, {state}.")
                lines.append(_("License: {name}, by {authors}.").format(**c["license"]))
                if c["license"].get("notes"):
                    lines.append(c["license"]["notes"])
            self.model_details.SetValue("\n".join(lines))
            return
        lic = model["license"]
        lines += [model["name"], model["summary"], _("Languages: {languages}").format(languages=model["languages"]),
                  _("License: {name}, by {authors}. Commercial use {allowed}.").format(
                      name=lic["name"], authors=lic["authors"],
                      allowed=_("allowed") if lic.get("commercial") else _("not allowed")), ""]
        assessment = self.services.assessment(model)
        if assessment:
            lines.append(_("On this computer: {summary}.").format(summary=assessment.summary()))
            lines += assessment.reasons
        else:
            lines.append(_("The hardware hasn't been scanned, so MCF can't say how well this runs. Use Help, "
                           "Hardware scan."))
        lines.append("")
        engine = self.services.engine_for(model)
        lines.append(_("AI software: {state}, about {gb} GB installed.").format(
            state=_("installed") if self.services.runtime_ready(model) else _("not installed"),
            gb=engine["installed_gb"]))
        for c in model["components"]:
            state = _("installed") if store.is_component_installed(model["id"], c) else _("not installed")
            lines.append(f"{c['name']}: {models.gb(models.component_size(c))}, {state}.")
        lines.append(_("Location: {folder}").format(folder=store.folder(model["id"])))
        self.model_details.SetValue("\n".join(lines))

    def _set_task_running(self, running: bool):
        for button in self.model_buttons:
            button.Enable(not running)
        self.cancel_task_button.Enable(running)
        self.services.busy = running

    def _run_task(self, task, done_message: str, on_done=None):
        self.model_progress.reset()
        self._set_task_running(True)

        def finished(ok, cancelled, error):
            self.task = None
            self._set_task_running(False)
            if ok:
                self.model_progress.set_progress(1.0)
                self.model_progress.status.SetValue(done_message)
                self.announcer.announce(done_message, Kind.COMPLETION)
            elif cancelled:
                self.model_progress.status.SetValue(_("Cancelled. Starting again resumes where it stopped."))
                self.announcer.announce(_("Cancelled."), Kind.COMPLETION, interrupt=True)
            if on_done:
                on_done(ok)
            self.refresh_models()
            self.update_usage()
            if ok or cancelled:
                self.model_list.SetFocus()
            else:
                self.model_progress.show_error(error)

        self.task = BackgroundTask(task, self.model_progress.set_status,
                                   lambda f: self.model_progress.set_progress(f), finished)
        self.cancel_task_button.SetFocus()

    def cancel_task(self):
        if self.task:
            self.task.cancel()
            self.model_progress.set_status(_("Cancelling."))

    def on_install(self, event):
        item = self.selected_item()
        if item is None:
            return
        item_id, model = item
        if model is None:
            tools = self.services.manifest["cover_tools"]
            text = "\n\n".join(f"{c['name']}\n" + _("License: {name}, by {authors}. {url}").format(**c["license"])
                               + ("\n" + c["license"]["notes"] if c["license"].get("notes") else "")
                               for c in tools["components"])
            if not self._confirm_license(tools["name"], text):
                return
            self._run_task(setup_tasks.install_cover_tools(self.services),
                           _("The cover analysis tools are installed."))
            return
        assessment = self.services.assessment(model)
        if assessment and assessment.mode is None:
            text = _("{model} won't run on this computer.").format(model=model["name"]) + "\n\n" + "\n".join(
                assessment.reasons)
            if not ask(self, _("Install anyway?"), text, _("&Install anyway"), _("&Don't install")):
                return
        from mcf.ui.onboarding import license_text
        if not self._confirm_license(model["name"], license_text(model)):
            return
        optional = [c for c in model["components"] if not c["required"]]
        chosen = [c for c in model["components"] if c["required"]]
        if optional:
            recommended = assessment.planner if assessment else None
            with wx.MultiChoiceDialog(self, _("Optional parts to install. The one recommended for this computer is "
                                              "already ticked."), _("Optional parts"),
                                      [f"{c['name']} {models.gb(models.component_size(c))}" for c in optional]
                                      ) as dialog:
                dialog.SetSelections([i for i, c in enumerate(optional)
                                      if c["id"] == recommended or self.services.store.is_component_installed(
                                          model["id"], c)])
                if dialog.ShowModal() != wx.ID_OK:
                    return
                chosen += [optional[i] for i in dialog.GetSelections()]
        self._run_task(setup_tasks.install_model(self.services, model, chosen),
                       _("{model} is installed and ready.").format(model=model["name"]))

    def _confirm_license(self, name: str, text: str) -> bool:
        from mcf.ui.main_frame import RightsDialog
        with RightsDialog(self, _("License for {name}").format(name=name), text,
                          _("I've read this, and I'll only use material I have the rights to")) as dialog:
            return dialog.ShowModal() == wx.ID_OK

    def on_remove(self, event):
        item = self.selected_item()
        if item is None:
            return
        item_id, model = item
        name = model["name"] if model else self.services.manifest["cover_tools"]["name"]
        size = self.services.store.size_on_disk(item_id)
        if size == 0:
            self.announcer.announce(_("{name} isn't installed.").format(name=name), Kind.INFO)
            return
        if not ask(self, _("Remove"), _("Remove {name}? This frees {gb:.1f} GB. You can download it again later.")
                   .format(name=name, gb=size / GB), _("&Remove"), _("&Keep")):
            return
        with wx.BusyCursor():
            self.services.store.remove(item_id)
            if model and not any(self.services.store.installed_components(m)
                                 for m in self.services.manifest["models"] if m["engine"] == model["engine"]):
                runtime.remove_engine(model["engine"])
        self.announcer.announce(_("{name} removed.").format(name=name), Kind.COMPLETION)
        self.refresh_models()
        self.update_usage()

    def on_verify(self, event):
        item = self.selected_item()
        if item is None:
            return
        item_id, model = item
        if model is None:
            tools = self.services.manifest["cover_tools"]
            model = {"id": item_id, "name": tools["name"], "components": tools["components"]}
        bad: list[str] = []

        def done(ok):
            if ok and bad:
                text = _("{count} files are damaged or missing: {files}. They were deleted. Choose Install to "
                         "download them again.").format(count=len(bad), files=", ".join(bad))
                self.model_progress.status.SetValue(text)
                self.announcer.announce(text, Kind.ERROR)
                for name in bad:
                    (self.services.store.folder(item_id) / name).unlink(missing_ok=True)

        self._run_task(setup_tasks.verify_model(self.services, model, bad), _("Every file checked."), done)

    def on_repair(self, event):
        item = self.selected_item()
        if item is None or item[1] is None:
            self.announcer.announce(_("Select a model to repair its AI software."), Kind.INFO)
            return
        model = item[1]
        if not ask(self, _("Repair the AI software"),
                   _("This deletes and reinstalls the AI software for {model}, about {gb} GB to download. Models "
                     "aren't affected.").format(model=model["name"],
                                                gb=self.services.engine_for(model)["download_gb"]),
                   _("&Repair"), _("&Don't repair")):
            return
        self._run_task(setup_tasks.repair_runtime(self.services, model), _("The AI software is repaired."))

    def on_view_license(self, event):
        item = self.selected_item()
        if item is None:
            return
        _id, model = item
        if model is None:
            files = [c["license"]["file"] for c in self.services.manifest["cover_tools"]["components"]]
        else:
            files = [model["license"]["file"]]
        text = "\n\n".join((paths.licenses_dir() / f).read_text(encoding="utf-8")
                           for f in files if (paths.licenses_dir() / f).exists())
        with widgets.TextDialog(self, _("License"), text or _("The license file is missing.")) as dialog:
            dialog.ShowModal()

    def on_move(self, event):
        with wx.DirDialog(self, _("Choose the new models folder")) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            new_root = Path(dialog.GetPath())
        store = self.services.store
        need = runtime.installed_size(store.root) if store.root.exists() else 0
        free = hardware.free_bytes(new_root)
        if free is not None and free < need:
            self.announcer.announce(_("That drive has {free:.1f} GB free, and the models need {need:.1f} GB.")
                                    .format(free=free / GB, need=need / GB), Kind.ERROR)
            return

        def task(status, progress, cancel):
            store.move_to(new_root, status, cancel)

        def done(ok):
            if ok:
                self.settings.set("storage.models_dir", "" if new_root == paths.default_models_dir()
                                  else str(new_root))
                self.models_dir.set(str(new_root))
                self.settings.save()

        self._run_task(task, _("Models moved to {folder}.").format(folder=new_root), done)

    # Profiles

    def _profiles_page(self):
        panel, sizer = self._page(_("Profiles"))
        self.profile_list = labeled(panel, sizer, _("&Artist profiles:"), lambda p: wx.ListCtrl(
            p, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, size=(-1, 170)), "profiles_list")
        for title, width in ((_("Name"), 240), (_("Model"), 160), (_("Created"), 160)):
            self.profile_list.AppendColumn(title, width=width)
        self.profile_list.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda e: self.show_profile_details())
        buttons = wx.WrapSizer(wx.HORIZONTAL)
        for label, handler in ((_("&New profile..."), self.on_new_profile),
                               (_("&Rename..."), self.on_rename_profile),
                               (_("&Edit details..."), self.on_edit_profile),
                               (_("D&uplicate..."), self.on_duplicate_profile),
                               (_("&Delete"), self.on_delete_profile)):
            button = tag(wx.Button(panel, label=label), "profiles_list")
            button.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(button, 0, wx.ALL, widgets.LABEL_BORDER)
        sizer.Add(buttons, 0, wx.EXPAND)
        self.profile_details = labeled(panel, sizer, _("De&tails:"), lambda p: read_only_text(p, "", 180),
                                       proportion=1)
        labeled(panel, sizer, _("About profiles:"), lambda p: read_only_text(
            p, _("Profiles are stored encrypted in the profiles folder inside MCF, and move with MCF when you copy "
                 "its folder. They can't be exported. Copying just the profiles folder gives nothing usable, but "
                 "MCF is open source, so a determined programmer with the whole MCF folder could still extract "
                 "one."), 90))
        self.refresh_profiles()

    def refresh_profiles(self):
        self.profiles, damaged = self.services.profiles.list()
        self.profile_list.DeleteAllItems()
        for index, profile in enumerate(self.profiles):
            model = self.services.model(profile.model)
            self.profile_list.InsertItem(index, profile.name)
            self.profile_list.SetItem(index, 1, model["name"] if model else profile.model)
            self.profile_list.SetItem(index, 2, time.strftime("%Y-%m-%d", time.localtime(profile.created)))
        if self.profiles:
            self.profile_list.Select(0)
        self.show_profile_details(damaged)

    def selected_profile(self):
        index = self.profile_list.GetFirstSelected()
        return self.profiles[index] if 0 <= index < len(self.profiles) else None

    def show_profile_details(self, damaged=None):
        profile = self.selected_profile()
        if profile is None:
            text = _("No profiles yet. Choose New profile to train one.")
        else:
            text = "\n".join([
                profile.name,
                _("Default style description: {style}").format(style=profile.style or _("none")),
                _("Default strength: {strength:.1f}").format(strength=profile.strength),
                _("Trained on {count} songs, on {date}.").format(
                    count=profile.songs, date=time.strftime("%Y-%m-%d", time.localtime(profile.created))),
                _("Rights confirmed on {date}.").format(
                    date=time.strftime("%Y-%m-%d", time.localtime(profile.rights_confirmed or profile.created))),
                _("Notes: {notes}").format(notes=profile.notes or _("none")),
            ])
        if damaged:
            text += "\n\n" + _("These profile files are damaged or were changed, so they can't be used: "
                               "{files}.").format(files=", ".join(damaged))
        self.profile_details.SetValue(text)

    def _need_profile(self):
        profile = self.selected_profile()
        if profile is None:
            self.announcer.announce(_("No profile selected."), Kind.INFO)
        return profile

    def on_new_profile(self, event):
        from mcf.ui.training import TrainingWizard
        if self.task is not None:
            self.announcer.announce(_("Wait for the download to finish first."), Kind.INFO)
            return
        wizard = TrainingWizard(self, self.app)
        wizard.ShowModal()
        wizard.Destroy()
        self.refresh_profiles()
        self.profile_list.SetFocus()

    def on_rename_profile(self, event):
        profile = self._need_profile()
        if profile is None:
            return
        name = ask_text(self, _("Rename profile"), _("New name:"), profile.name)
        if name:
            profile.name = name
            with wx.BusyCursor():
                self.services.profiles.update(profile)
            self.refresh_profiles()
            self.announcer.announce(_("Profile renamed."), Kind.COMPLETION)
        self.profile_list.SetFocus()

    def on_edit_profile(self, event):
        profile = self._need_profile()
        if profile is None:
            return
        with ProfileDetailsDialog(self, profile) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                profile.name = dialog.name.GetValue().strip() or profile.name
                profile.style = dialog.style.GetValue().strip()
                profile.strength = dialog.strength.GetValue()
                profile.notes = dialog.notes.GetValue().strip()
                with wx.BusyCursor():
                    self.services.profiles.update(profile)
                self.refresh_profiles()
                self.announcer.announce(_("Profile saved."), Kind.COMPLETION)
        self.profile_list.SetFocus()

    def on_duplicate_profile(self, event):
        profile = self._need_profile()
        if profile is None:
            return
        name = ask_text(self, _("Duplicate profile"), _("Name for the copy:"),
                        _("{name} copy").format(name=profile.name))
        if name:
            with wx.BusyCursor():
                self.services.profiles.duplicate(profile.id, name)
            self.refresh_profiles()
            self.announcer.announce(_("Profile duplicated."), Kind.COMPLETION)
        self.profile_list.SetFocus()

    def on_delete_profile(self, event):
        profile = self._need_profile()
        if profile is None:
            return
        if ask(self, _("Delete profile"), _("Delete the profile {name}? Its trained add-on is erased, and this "
                                            "can't be undone.").format(name=profile.name), _("&Delete"), _("&Keep")):
            self.services.profiles.delete(profile.id)
            self.refresh_profiles()
            self.announcer.announce(_("Profile deleted."), Kind.COMPLETION)
        self.profile_list.SetFocus()

    # Speech and guidance

    def _speech_page(self):
        s = self.settings
        panel, sizer = self._page(_("Speech and guidance"))
        self.verbosity = labeled(panel, sizer, _("How much MCF &speaks:"), lambda p: widgets.choice(p, [
            ("quiet", _("Quiet: only errors and when things finish")),
            ("normal", _("Normal")),
            ("detailed", _("Detailed: includes every progress step")),
        ], s.get("speech.verbosity")))
        self.output = labeled(panel, sizer, _("Speak &through:"), lambda p: widgets.choice(p, [
            ("auto", _("My screen reader (recommended)")),
            ("sapi", _("Windows voice")),
            ("none", _("Nothing, status bar only")),
        ], s.get("speech.output")))
        self.fallback = labeled(panel, sizer, _("When no screen reader is &running:"), lambda p: widgets.choice(p, [
            ("none", _("Show messages in the status bar only")),
            ("sapi", _("Speak with the Windows voice")),
        ], s.get("speech.fallback")))
        self.interrupt = _checkbox(panel, sizer, _("New messages &interrupt current speech"),
                                   s.get("speech.interrupt"))
        self.braille = _checkbox(panel, sizer, _("Also show messages on a &braille display"), s.get("speech.braille"))
        self.progress = _checkbox(panel, sizer, _("Announce &progress during long tasks"),
                                  s.get("speech.announce_progress"))
        self.completion = _checkbox(panel, sizer, _("Announce when tasks &finish"),
                                    s.get("speech.announce_completion"))
        self.hints = _checkbox(panel, sizer, _("Announce &tips and hints"), s.get("speech.announce_hints"))
        self.explain_on_focus = _checkbox(panel, sizer,
                                          _("Show an e&xplanation of the focused control in the Explanation field"),
                                          s.get("guidance.explain_on_focus"))
        test = wx.Button(panel, label=_("T&est speech"))
        test.Bind(wx.EVT_BUTTON, self.on_test_speech)
        sizer.Add(test, 0, wx.ALL, widgets.LABEL_BORDER)
        labeled(panel, sizer, _("Note:"), lambda p: read_only_text(
            p, _("Errors are always announced, whatever you choose here. Everything MCF announces is also kept in "
                 "View, Announcement history."), height=60))

    # Storage

    def _storage_page(self):
        panel, sizer = self._page(_("Storage"))
        self.models_dir = FolderPicker(panel, _("&Models folder:"), self.settings.get("storage.models_dir"),
                                       paths.default_models_dir(), "models_dir")
        sizer.Add(self.models_dir, 0, wx.EXPAND)
        self.library_dir = FolderPicker(panel, _("&Library folder:"), self.settings.get("storage.library_dir"),
                                        paths.default_library_dir(), "library_dir")
        sizer.Add(self.library_dir, 0, wx.EXPAND)
        labeled(panel, sizer, _("Note:"), lambda p: read_only_text(
            p, _("Changing a folder here doesn't move what's already in it. To move installed models, use Move "
                 "models on the Models tab. The AI software always stays in the runtime folder inside MCF."),
            height=70))

    # Saving

    def _saving_page(self):
        s = self.settings
        panel, sizer = self._page(_("Saving"))
        self.save_format = labeled(panel, sizer, _("Default &format:"), lambda p: widgets.choice(p, [
            ("wav", _("WAV: full quality, large files")),
            ("flac", _("FLAC: full quality, smaller files")),
            ("mp3", _("MP3: smaller, plays everywhere")),
            ("ogg", _("OGG Vorbis: smaller, open format")),
        ], s.get("saving.default_format")))
        self.mp3_bitrate = labeled(panel, sizer, _("MP3 &quality:"), lambda p: widgets.choice(p, [
            ("320", _("320 kbps (best)")), ("256", _("256 kbps")),
            ("192", _("192 kbps")), ("128", _("128 kbps (smallest)")),
        ], str(s.get("saving.mp3_bitrate"))))
        self.always_ask = labeled(panel, sizer, _("When &saving a song:"), lambda p: widgets.choice(p, [
            ("ask", _("Always show the Save As dialog")),
            ("direct", _("Save straight to the exported audio folder")),
        ], "ask" if s.get("saving.always_ask") else "direct"))
        from mcf.ui.onboarding import default_export_folder
        self.save_folder = FolderPicker(panel, _("E&xported audio folder:"), s.get("saving.default_folder"),
                                        default_export_folder(), "export_dir")
        sizer.Add(self.save_folder, 0, wx.EXPAND)

    # Performance

    def _performance_page(self):
        s = self.settings
        panel, sizer = self._page(_("Performance"))
        modes = [("auto", recommend.mode_label("auto"))] + [(m, recommend.mode_label(m)) for m in recommend.MODES]
        self.mode = labeled(panel, sizer, _("Performance &mode:"), lambda p: widgets.choice(
            p, modes, s.get("performance.mode")), "performance_mode")
        self.mode_note = labeled(panel, sizer, _("For this computer:"), lambda p: read_only_text(p, "", 90))
        self.mode.Bind(wx.EVT_CHOICE, lambda e: self.update_mode_note())
        self.priority = labeled(panel, sizer, _("Generation &priority:"), lambda p: widgets.choice(p, [
            ("below_normal", _("Below normal: keeps your screen reader responsive (recommended)")),
            ("idle", _("Lowest: only uses spare time, slowest")),
            ("normal", _("Normal: fastest, but the computer may feel slow")),
        ], s.get("performance.worker_priority")))
        self.warn_minutes = labeled(
            panel, sizer, _("&Warn before jobs longer than this many minutes (0 never warns):"),
            lambda p: wx.SpinCtrl(p, min=0, max=24 * 60, initial=s.get("performance.long_generation_warning_minutes")),
            expand=False)
        self.estimates_text = labeled(panel, sizer, _("&Learned time estimates:"),
                                      lambda p: read_only_text(p, "", 110), "estimates")
        reset = tag(wx.Button(panel, label=_("&Reset learned estimates")), "estimates")
        reset.Bind(wx.EVT_BUTTON, self.on_reset_estimates)
        sizer.Add(reset, 0, wx.ALL, widgets.LABEL_BORDER)
        self.update_mode_note()
        self.update_estimates()

    def update_mode_note(self):
        mode = widgets.choice_value(self.mode)
        hw = self.services.hardware
        if hw is None:
            self.mode_note.SetValue(_("The hardware hasn't been scanned. Use Help, Hardware scan."))
            return
        lines = []
        for model in self.services.manifest["models"]:
            a = recommend.assess(model, hw, None if mode == "auto" else mode)
            if mode != "auto" and mode not in a.possible_modes:
                lines.append(_("{model}: this mode won't work on this computer.").format(model=model["name"]))
            else:
                lines.append(f"{model['name']}: {a.summary()}.")
        self.mode_note.SetValue("\n".join(lines))

    def update_estimates(self):
        counts = self.services.estimator.counts()
        if not counts:
            self.estimates_text.SetValue(_("No runs recorded yet. Estimates use built-in figures until MCF has "
                                           "timed a few jobs on this computer."))
            return
        task_names = {"generate": _("songs"), "cover": _("covers"), "analyse": _("cover analyses"),
                      "train": _("training")}
        lines = []
        for model_id, task, mode, count in counts:
            model = self.services.model(model_id)
            lines.append(_("{model}, {task}, {mode} mode: learned from {count} runs.").format(
                model=model["name"] if model else model_id, task=task_names.get(task, task), mode=mode,
                count=count))
        self.estimates_text.SetValue("\n".join(lines))

    def on_reset_estimates(self, event):
        if ask(self, _("Reset estimates"), _("Forget every recorded run? Estimates go back to the built-in "
                                             "figures."), _("&Reset"), _("&Keep")):
            self.services.estimator.reset()
            self.update_estimates()
            self.announcer.announce(_("Learned estimates reset."), Kind.COMPLETION)

    # Generation defaults

    def _generation_page(self):
        s = self.settings
        panel, sizer = self._page(_("Generation defaults"))
        self.default_length = labeled(panel, sizer, _("Default &length in seconds:"), lambda p: wx.SpinCtrl(
            p, min=10, max=600, initial=s.get("generation.length_seconds")), "length", expand=False)
        self.default_steps = labeled(panel, sizer, _("Default &steps:"), lambda p: wx.SpinCtrl(
            p, min=1, max=200, initial=s.get("generation.steps")), "steps", expand=False)
        self.default_guidance = labeled(panel, sizer, _("Default &guidance strength:"), lambda p: wx.SpinCtrlDouble(
            p, min=1, max=15, initial=s.get("generation.guidance"), inc=0.5), "guidance_scale", expand=False)
        from mcf.ui.generate_panel import LANGUAGES
        self.default_language = labeled(panel, sizer, _("Default lyrics l&anguage:"), lambda p: widgets.choice(
            p, LANGUAGES, s.get("generation.language")), "language")

    # Keyboard

    def _keyboard_page(self):
        panel, sizer = self._page(_("Keyboard"))
        self.hotkey_text = labeled(panel, sizer, _("System-wide &cancel shortcut:"), lambda p: wx.TextCtrl(
            p, value=hotkeys.describe(self.cancel_hotkey), style=wx.TE_READONLY))
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        change = wx.Button(panel, label=_("C&hange..."))
        reset = wx.Button(panel, label=_("&Reset to Ctrl+Alt+Shift+C"))
        buttons.Add(change, 0, wx.ALL, widgets.LABEL_BORDER)
        buttons.Add(reset, 0, wx.ALL, widgets.LABEL_BORDER)
        sizer.Add(buttons)
        labeled(panel, sizer, _("Note:"), lambda p: read_only_text(
            p, _("This shortcut stops a generation even when MCF isn't the active window, for example if your "
                 "screen reader becomes slow. Choose a combination that your screen reader and other programs "
                 "don't use. Inside MCF, Escape also cancels.\n\n"
                 "In the Library: Space plays and pauses, Left and Right arrows move 5 seconds, Shift with the "
                 "arrows moves 30 seconds, Control+Up and Down change the volume, F2 renames and Delete deletes."),
            height=140))
        change.Bind(wx.EVT_BUTTON, self.on_change_hotkey)
        reset.Bind(wx.EVT_BUTTON, lambda e: self._set_hotkey("ctrl+alt+shift+c"))

    # Events

    def on_test_speech(self, event):
        # Temporarily use the chosen output, without saving it
        saved = self.settings.get("speech.output"), self.settings.get("speech.fallback")
        self.settings.set("speech.output", widgets.choice_value(self.output))
        self.settings.set("speech.fallback", widgets.choice_value(self.fallback))
        try:
            name = self.announcer.output_name()
            self.announcer.speak(_("This is how MCF sounds. Speaking through: {name}").format(name=name), True)
        finally:
            self.settings.set("speech.output", saved[0])
            self.settings.set("speech.fallback", saved[1])

    def on_change_hotkey(self, event):
        with HotkeyCaptureDialog(self) as dialog:
            if dialog.ShowModal() == wx.ID_OK and dialog.hotkey:
                self._set_hotkey(dialog.hotkey.text)
        self.hotkey_text.SetFocus()

    def _set_hotkey(self, text: str):
        self.cancel_hotkey = text
        self.hotkey_text.SetValue(hotkeys.describe(text))
        self.announcer.announce(_("Cancel shortcut set to {key}.").format(key=hotkeys.describe(text)), Kind.INFO)

    def on_cancel(self, event):
        if self.task is not None:
            if not ask(self, _("A download is running"),
                       _("Closing Preferences cancels the running download. It resumes where it stopped when you "
                         "start it again."), _("&Cancel the download and close"), _("&Keep downloading")):
                return
            self.task.cancel()
            self.task.thread.join(timeout=10)
            self.services.busy = False
        self.EndModal(wx.ID_CANCEL)

    def _save(self) -> bool:
        s = self.settings
        s.set("speech.verbosity", widgets.choice_value(self.verbosity))
        s.set("speech.output", widgets.choice_value(self.output))
        s.set("speech.fallback", widgets.choice_value(self.fallback))
        s.set("speech.interrupt", self.interrupt.GetValue())
        s.set("speech.braille", self.braille.GetValue())
        s.set("speech.announce_progress", self.progress.GetValue())
        s.set("speech.announce_completion", self.completion.GetValue())
        s.set("speech.announce_hints", self.hints.GetValue())
        s.set("guidance.beginner", self.beginner.GetValue())
        s.set("guidance.explain_on_focus", self.explain_on_focus.GetValue())
        s.set("storage.models_dir", self.models_dir.value())
        s.set("storage.library_dir", self.library_dir.value())
        s.set("saving.default_format", widgets.choice_value(self.save_format))
        s.set("saving.mp3_bitrate", int(widgets.choice_value(self.mp3_bitrate)))
        s.set("saving.always_ask", widgets.choice_value(self.always_ask) == "ask")
        s.set("saving.default_folder", self.save_folder.value())
        s.set("performance.mode", widgets.choice_value(self.mode))
        s.set("performance.long_generation_warning_minutes", self.warn_minutes.GetValue())
        s.set("performance.worker_priority", widgets.choice_value(self.priority))
        s.set("generation.length_seconds", self.default_length.GetValue())
        s.set("generation.steps", self.default_steps.GetValue())
        s.set("generation.guidance", self.default_guidance.GetValue())
        s.set("generation.language", widgets.choice_value(self.default_language))
        s.set("keyboard.cancel_hotkey", self.cancel_hotkey)
        s.set("general.language", widgets.choice_value(self.language))
        s.set("general.check_model_list_updates", self.check_updates.GetValue())
        s.set("general.update_check", widgets.choice_value(self.update_check))
        try:
            s.save()
        except OSError as exc:
            self.announcer.announce(_("Could not save preferences: {error}").format(error=exc), Kind.ERROR)
            return False
        return True

    def on_ok(self, event):
        if self.task is not None:
            self.announcer.announce(_("Wait for the download to finish, or cancel it first."), Kind.INFO)
            return
        if self._save():
            self.EndModal(wx.ID_OK)
