"""The Generate tab: every setting for a new song, cover or edit."""

from pathlib import Path

import wx

from mcf import audio_export, lyrics, presets
from mcf.announcer import Kind
from mcf.i18n import _
from mcf.ui import widgets
from mcf.ui.components import ProgressPanel, ask_text
from mcf.ui.widgets import labeled, read_only_text, tag

MODES = [
    ("new", lambda: _("New song")),
    ("cover", lambda: _("Cover of a song")),
    ("extend", lambda: _("Extend a song")),
    ("variation", lambda: _("Variation of a song")),
    ("inpaint", lambda: _("Replace a section")),
    ("reference", lambda: _("From reference audio")),
]
LANGUAGES = [("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"), ("it", "Italian"),
             ("pt", "Portuguese"), ("nl", "Dutch"), ("sv", "Swedish"), ("pl", "Polish"), ("ru", "Russian"),
             ("uk", "Ukrainian"), ("tr", "Turkish"), ("ar", "Arabic"), ("hi", "Hindi"), ("zh", "Chinese"),
             ("ja", "Japanese"), ("ko", "Korean"), ("unknown", "Other or mixed")]
VARIATION_AMOUNTS = [("0.8", lambda: _("Slightly different")), ("0.6", lambda: _("Moderately different")),
                     ("0.4", lambda: _("Very different"))]
AUDIO_WILDCARD = "Audio files (*.wav;*.flac;*.mp3;*.ogg)|*.wav;*.flac;*.mp3;*.ogg"


class SourcePicker(wx.Panel):
    """The song an edit starts from: a library song or a file."""

    def __init__(self, parent, frame, label: str, help_key: str, allow_library: bool = True):
        super().__init__(parent)
        self.frame = frame
        self.path: Path | None = None
        self.song_id: str | None = None
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.field = labeled(self, sizer, label, lambda p: wx.TextCtrl(
            p, value=_("None chosen"), style=wx.TE_READONLY), help_key)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        if allow_library:
            library = tag(wx.Button(self, label=_("From the &library...")), help_key)
            library.Bind(wx.EVT_BUTTON, self.on_library)
            buttons.Add(library, 0, wx.ALL, widgets.LABEL_BORDER)
        browse = tag(wx.Button(self, label=_("Choose a &file...")), help_key)
        browse.Bind(wx.EVT_BUTTON, self.on_file)
        buttons.Add(browse, 0, wx.ALL, widgets.LABEL_BORDER)
        sizer.Add(buttons)
        self.SetSizer(sizer)
        self.on_changed = None

    def on_library(self, event):
        songs = self.frame.services.library.all()
        if not songs:
            self.frame.announcer.announce(_("The library is empty."), Kind.INFO)
            return
        with wx.SingleChoiceDialog(self, _("Choose a song:"), _("Library"),
                                   [s.title for s in songs]) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                song = songs[dialog.GetSelection()]
                self.set(self.frame.services.library.path(song), song.title, song.id)
        self.field.SetFocus()

    def on_file(self, event):
        with wx.FileDialog(self, _("Choose an audio file"), wildcard=_(AUDIO_WILDCARD),
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                path = Path(dialog.GetPath())
                self.set(path, path.name, None)
        self.field.SetFocus()

    def set(self, path: Path | None, title: str = "", song_id: str | None = None):
        self.path = path
        self.song_id = song_id
        if path is None:
            self.field.SetValue(_("None chosen"))
        else:
            try:
                seconds = audio_export.duration(path)
                self.field.SetValue(_("{title}, {seconds:.0f} seconds").format(title=title, seconds=seconds))
            except Exception:
                self.field.SetValue(title)
        if self.on_changed:
            self.on_changed()


class CoverPanel(wx.Panel):
    """Cover options: song details, what to follow, and the analysis."""

    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self.analysis: dict | None = None
        self.analysed_path: Path | None = None
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.source = SourcePicker(self, frame, _("Song to co&ver:"), "cover_source", allow_library=False)
        self.source.on_changed = self.on_source_changed
        sizer.Add(self.source, 0, wx.EXPAND)

        self.title = labeled(self, sizer, _("Original &title:"), lambda p: wx.TextCtrl(p), "cover_title")
        self.writers = labeled(self, sizer, _("Song&writers:"), lambda p: wx.TextCtrl(p), "cover_writers")
        self.artist = labeled(self, sizer, _("Original &artist:"), lambda p: wx.TextCtrl(p), "cover_artist")

        self.preset = labeled(self, sizer, _("Cover &preset:"), lambda p: wx.Choice(p), "cover_preset")
        self.preset.Bind(wx.EVT_CHOICE, lambda e: self.apply_preset())
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        save = tag(wx.Button(self, label=_("Save cover pr&eset...")), "cover_preset")
        delete = tag(wx.Button(self, label=_("Delete cover prese&t")), "cover_preset")
        save.Bind(wx.EVT_BUTTON, self.on_save_preset)
        delete.Bind(wx.EVT_BUTTON, self.on_delete_preset)
        buttons.Add(save, 0, wx.ALL, widgets.LABEL_BORDER)
        buttons.Add(delete, 0, wx.ALL, widgets.LABEL_BORDER)
        sizer.Add(buttons)

        sizer.Add(wx.StaticText(self, label=_("The cover follows:")), 0, wx.ALL, widgets.LABEL_BORDER)
        self.checks: dict[str, wx.CheckBox] = {}
        for aspect in presets.COVER_ASPECTS:
            box = tag(wx.CheckBox(self, label=presets.aspect_label(aspect)), f"follow_{aspect}")
            box.Bind(wx.EVT_CHECKBOX, lambda e, a=aspect: self.on_check(a))
            sizer.Add(box, 0, wx.LEFT | wx.RIGHT, 12)
            self.checks[aspect] = box
        self.checks["melody"].SetValue(True)

        self.bpm = labeled(self, sizer, _("New tempo in beats per &minute:"), lambda p: wx.SpinCtrl(
            p, min=40, max=240, initial=120), "cover_bpm", expand=False)
        self.transpose = labeled(self, sizer, _("Change &key by semitones (negative is lower):"),
                                 lambda p: wx.SpinCtrl(p, min=-12, max=12, initial=0), "cover_transpose", expand=False)
        self.sections = labeled(self, sizer, _("&Sections to keep:"), lambda p: wx.CheckListBox(p, size=(-1, 110)),
                                "cover_sections")
        self.max_seconds = labeled(self, sizer, _("Maximum length in seco&nds:"), lambda p: wx.SpinCtrl(
            p, min=10, max=600, initial=180), "cover_length", expand=False)

        self.analyse = tag(wx.Button(self, label=_("Anal&yse the song")), "cover_analyse")
        self.analyse.Bind(wx.EVT_BUTTON, lambda e: frame.analyse_cover())
        sizer.Add(self.analyse, 0, wx.ALL, widgets.LABEL_BORDER)
        self.summary = labeled(self, sizer, _("Analysis res&ult:"), lambda p: read_only_text(
            p, _("Not analysed yet."), height=90), "cover_summary")
        self.SetSizer(sizer)
        self.refresh_presets(frame.settings.get("cover.preset"))

    def set_method(self, method: str | None, free: list[str] | None = None):
        """audio: the model reads the prepared recording. score: it follows a written score only.

        free: what the model decides for itself and can't be asked to follow (from the model list).
        Those checkboxes are unticked and unavailable. A score never carries the instrumentation.
        """
        self.method = method or "audio"
        self.free = set(free or []) | ({"instrumentation"} if self.method == "score" else set())
        for aspect, box in self.checks.items():
            if aspect in self.free:
                box.SetValue(False)
                box.Disable()
            else:
                box.Enable()
        self.update_enabled()

    # Presets

    def refresh_presets(self, select: str | None = None):
        self.preset_keys: list[str] = []
        self.preset.Clear()
        for key, preset in presets.builtin_cover_presets().items():
            self.preset.Append(preset["name"])
            self.preset_keys.append(key)
        for name in presets.cover_presets().names():
            self.preset.Append(name)
            self.preset_keys.append("custom:" + name)
        index = self.preset_keys.index(select) if select in self.preset_keys else 0
        self.preset.SetSelection(index)
        self.apply_preset()

    def apply_preset(self):
        key = self.preset_keys[self.preset.GetSelection()]
        if key.startswith("custom:"):
            follow = presets.cover_presets().load(key[7:]) or {}
        else:
            follow = presets.builtin_cover_presets()[key]["follow"]
        for aspect, box in self.checks.items():
            box.SetValue(bool(follow.get(aspect, False)) or aspect == "melody")
        for aspect in getattr(self, "free", set()):
            self.checks[aspect].SetValue(False)
        self.frame.settings.set("cover.preset", key)
        self.update_enabled()

    def on_save_preset(self, event):
        name = ask_text(self, _("Save cover preset"), _("Name for this cover preset:"))
        if name:
            presets.cover_presets().save(name, self.follow())
            self.refresh_presets("custom:" + name)
            self.frame.announcer.announce(_("Cover preset {name} saved.").format(name=name), Kind.COMPLETION)
        self.preset.SetFocus()

    def on_delete_preset(self, event):
        key = self.preset_keys[self.preset.GetSelection()]
        if not key.startswith("custom:"):
            self.frame.announcer.announce(_("Built-in cover presets can't be deleted."), Kind.INFO)
            return
        presets.cover_presets().delete(key[7:])
        self.refresh_presets()
        self.frame.announcer.announce(_("Cover preset deleted."), Kind.COMPLETION)
        self.preset.SetFocus()

    # Checkboxes

    def on_check(self, aspect: str):
        if aspect == "melody":
            self.checks["melody"].SetValue(True)
            self.frame.announcer.announce(_("The melody is always followed. A cover that doesn't follow the "
                                            "melody would be a different song."), Kind.INFO, interrupt=True)
        self.update_enabled()

    def follow(self) -> dict:
        return {aspect: box.GetValue() for aspect, box in self.checks.items()}

    def update_enabled(self):
        follow = self.follow()
        self.bpm.Enable(not follow["tempo"] and "tempo" not in getattr(self, "free", set()))
        self.transpose.Enable(not follow["key"])
        self.sections.Enable(not follow["structure"] and self.sections.GetCount() > 0)
        self.max_seconds.Enable(not follow["length"])

    # Source and analysis

    def on_source_changed(self):
        self.analysis = None
        self.analysed_path = None
        self.summary.SetValue(_("Not analysed yet."))
        self.sections.Clear()
        path = self.source.path
        if path is not None:
            try:
                import soundfile as sf
                with sf.SoundFile(str(path)) as f:
                    self.title.SetValue(f.title or path.stem)
                    self.artist.SetValue(f.artist or "")
            except Exception:
                self.title.SetValue(path.stem)
        self.update_enabled()

    def set_analysis(self, result: dict):
        self.analysis = result
        self.analysed_path = self.source.path
        self.sections.Clear()
        for section in result.get("sections", []):
            self.sections.Append(_("{name}, {start:.0f} to {end:.0f} seconds").format(
                name=section["name"], start=section["start"], end=section["end"]))
        self.sections.SetCheckedItems(list(range(self.sections.GetCount())))
        self.bpm.SetValue(int(round(result.get("bpm") or 120)))
        self.max_seconds.SetValue(int(min(600, max(10, result.get("duration", 180)))))
        lines = [
            _("Length: {seconds:.0f} seconds.").format(seconds=result.get("duration", 0)),
            _("Tempo: about {bpm:.0f} beats per minute.").format(bpm=result.get("bpm") or 0),
            _("Key: {key}.").format(key=result.get("key", "")),
            _("Sections: {names}.").format(names=", ".join(s["name"] for s in result.get("sections", []))
                                           or _("none found")),
            _("Chords found: {count}.").format(count=len(result.get("chords", []))),
        ]
        if result.get("caption"):
            lines.append(_("Description: {caption}.").format(caption=result["caption"]))
        if not result.get("lyrics"):
            lines.append(_("No sung words were found, so the song is treated as instrumental."))
        else:
            lines.append(_("The transcribed lyrics are in the Lyrics field. Transcription makes mistakes, so "
                           "check them before generating."))
        self.summary.SetValue("\n".join(lines))
        self.update_enabled()

    def cover_spec(self) -> dict:
        follow = self.follow()
        spec = {"analysis": self.analysis, "follow": follow}
        if not follow["tempo"]:
            spec["bpm"] = self.bpm.GetValue()
        if not follow["key"]:
            spec["transpose"] = self.transpose.GetValue()
        if not follow["structure"]:
            spec["sections"] = list(self.sections.GetCheckedItems())
        if not follow["length"]:
            spec["max_seconds"] = self.max_seconds.GetValue()
        return spec

    def credits(self) -> dict:
        return {"original_title": self.title.GetValue().strip(), "writers": self.writers.GetValue().strip(),
                "original_artist": self.artist.GetValue().strip()}


class GeneratePanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        settings = frame.settings
        outer = wx.BoxSizer(wx.VERTICAL)
        self.scroll = wx.ScrolledWindow(self)
        self.scroll.SetScrollRate(0, 20)
        p = self.scroll
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.model = labeled(p, sizer, _("&Model:"), lambda q: wx.Choice(q), "model")
        self.model.Bind(wx.EVT_CHOICE, lambda e: self.on_model_changed())

        self.preset = labeled(p, sizer, _("&Preset:"), lambda q: wx.Choice(q), "preset")
        self.preset.Bind(wx.EVT_CHOICE, lambda e: self.on_preset_chosen())
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        save = tag(wx.Button(p, label=_("Save preset...")), "preset")
        delete = tag(wx.Button(p, label=_("Delete preset")), "preset")
        save.Bind(wx.EVT_BUTTON, self.on_save_preset)
        delete.Bind(wx.EVT_BUTTON, self.on_delete_preset)
        buttons.Add(save, 0, wx.ALL, widgets.LABEL_BORDER)
        buttons.Add(delete, 0, wx.ALL, widgets.LABEL_BORDER)
        sizer.Add(buttons)

        self.mode = labeled(p, sizer, _("M&ode:"), lambda q: widgets.choice(
            q, [(k, f()) for k, f in MODES], "new"), "mode")
        self.mode.Bind(wx.EVT_CHOICE, lambda e: self.on_mode_changed())

        # Mode-specific settings
        self.source = SourcePicker(p, frame, _("Source so&ng:"), "source")
        sizer.Add(self.source, 0, wx.EXPAND)
        self.extend_seconds = labeled(p, sizer, _("Seconds to a&dd:"), lambda q: wx.SpinCtrl(
            q, min=5, max=300, initial=30), "extend_seconds", expand=False)
        self.inpaint_start = labeled(p, sizer, _("Replace from second:"), lambda q: wx.SpinCtrlDouble(
            q, min=0, max=3600, initial=0, inc=0.5), "inpaint_range", expand=False)
        self.inpaint_end = labeled(p, sizer, _("Replace until second:"), lambda q: wx.SpinCtrlDouble(
            q, min=0, max=3600, initial=10, inc=0.5), "inpaint_range", expand=False)
        self.variation = labeled(p, sizer, _("How &different:"), lambda q: widgets.choice(
            q, [(k, f()) for k, f in VARIATION_AMOUNTS], "0.6"), "variation_amount")
        self.reference = SourcePicker(p, frame, _("Reference a&udio:"), "reference")
        sizer.Add(self.reference, 0, wx.EXPAND)
        self.cover = CoverPanel(p, frame)
        sizer.Add(self.cover, 0, wx.EXPAND)
        self.mode_controls = {
            "source": [self.source],
            "extend": [self.extend_seconds],
            "inpaint": [self.inpaint_start, self.inpaint_end],
            "variation": [self.variation],
            "reference": [self.reference],
            "cover": [self.cover],
        }

        self.profile = labeled(p, sizer, _("Artist pro&file:"), lambda q: wx.Choice(q), "profile")
        self.profile.Bind(wx.EVT_CHOICE, lambda e: self.on_profile_changed())
        self.profile_strength = labeled(p, sizer, _("Profile stren&gth:"), lambda q: wx.SpinCtrlDouble(
            q, min=0.1, max=1.5, initial=1.0, inc=0.1), "profile_strength", expand=False)

        self.style = labeled(p, sizer, _("&Style description:"), lambda q: wx.TextCtrl(q), "style")
        self.lyrics = labeled(p, sizer, _("&Lyrics and tags:"), lambda q: wx.TextCtrl(
            q, size=(-1, 160), style=wx.TE_MULTILINE | wx.TE_RICH2), "lyrics")
        self.lyrics.Bind(wx.EVT_KEY_DOWN, self.on_lyrics_key)
        self.lyrics.Bind(wx.EVT_CONTEXT_MENU, lambda e: self.show_tag_menu())
        self.language = labeled(p, sizer, _("Lyrics lang&uage:"), lambda q: widgets.choice(
            q, LANGUAGES, settings.get("generation.language")), "language")

        self.instrumental = tag(wx.CheckBox(p, label=_("&Instrumental only")), "instrumental")
        sizer.Add(self.instrumental, 0, wx.ALL, widgets.LABEL_BORDER)
        self.length = labeled(p, sizer, _("Len&gth in seconds:"), lambda q: wx.SpinCtrl(
            q, min=10, max=600, initial=settings.get("generation.length_seconds")), "length", expand=False)

        self.show_advanced = tag(wx.CheckBox(p, label=_("Show &advanced settings")), "show_advanced")
        self.show_advanced.Bind(wx.EVT_CHECKBOX, lambda e: self.update_visibility())
        sizer.Add(self.show_advanced, 0, wx.ALL, widgets.LABEL_BORDER)
        self.seed = labeled(p, sizer, _("S&eed (-1 for random):"), lambda q: wx.SpinCtrl(
            q, min=-1, max=2**31 - 1, initial=-1), "seed", expand=False)
        self.steps = labeled(p, sizer, _("S&teps:"), lambda q: wx.SpinCtrl(
            q, min=1, max=200, initial=settings.get("generation.steps")), "steps", expand=False)
        self.guidance_scale = labeled(p, sizer, _("Guidance st&rength:"), lambda q: wx.SpinCtrlDouble(
            q, min=1, max=15, initial=settings.get("generation.guidance"), inc=0.5), "guidance_scale", expand=False)
        self.advanced = [self.seed, self.steps, self.guidance_scale]

        self.generate = tag(wx.Button(p, label=_("Ge&nerate")), "generate")
        self.generate.Bind(wx.EVT_BUTTON, lambda e: frame.generate_or_cancel())
        sizer.Add(self.generate, 0, wx.ALL, widgets.LABEL_BORDER)
        self.progress = ProgressPanel(p, frame.announcer, _("Stat&us:"))
        tag(self.progress.status, "status")
        sizer.Add(self.progress, 0, wx.EXPAND)
        self.progress.status.SetValue(_("Ready."))
        self.explanation = labeled(p, sizer, _("E&xplanation:"),
                                   lambda q: read_only_text(q, height=100), "explanation")
        p.SetSizer(sizer)
        outer.Add(self.scroll, 1, wx.EXPAND)
        self.SetSizer(outer)

        self.refresh_models()
        self.refresh_presets()
        self.refresh_profiles()
        self.update_visibility()

    # Visibility

    def _show(self, ctrl, visible: bool):
        """Show or hide a control and its label."""
        ctrl.Show(visible)
        previous = ctrl.GetPrevSibling()
        if isinstance(previous, wx.StaticText):
            previous.Show(visible)

    def current_model(self) -> dict | None:
        model_id = self.model_id()
        return self.frame.services.model(model_id) if model_id else None

    def supports(self, tag_name: str) -> bool:
        """Whether the selected model uses this setting. Everything shows when no model is installed."""
        model = self.current_model()
        return model is None or tag_name in model.get("tags", [])

    def update_visibility(self):
        mode = widgets.choice_value(self.mode)
        model = self.current_model()
        self._show(self.source, mode in ("extend", "variation", "inpaint"))
        for key, ctrls in self.mode_controls.items():
            if key == "source":
                continue
            for ctrl in ctrls:
                self._show(ctrl, key == mode)
        self._show(self.length, mode in ("new", "reference") and self.supports("length"))
        self._show(self.language, self.supports("language"))
        self._show(self.instrumental, self.supports("instrumental"))
        advanced = self.show_advanced.IsChecked()
        self._show(self.seed, advanced)
        self._show(self.steps, advanced and self.supports("steps"))
        self._show(self.guidance_scale, advanced and self.supports("guidance"))
        profiles_allowed = model is None or model.get("profiles", False)
        has_profiles = self.profile.GetCount() > 1
        self._show(self.profile, profiles_allowed)
        self._show(self.profile_strength, profiles_allowed and has_profiles and self.profile.GetSelection() > 0)
        self.cover.set_method(model.get("cover_method") if model else "audio",
                              model.get("cover_free") if model else None)
        self.scroll.FitInside()
        self.scroll.Layout()

    def apply_model_features(self):
        """Offer only the modes and limits the selected model supports."""
        model = self.current_model()
        current = widgets.choice_value(self.mode)
        supported = model["modes_supported"] if model else [k for k, _f in MODES]
        options = [(k, f()) for k, f in MODES if k in supported]
        self.mode.Clear()
        for _key, label in options:
            self.mode.Append(label)
        self.mode.mcf_values = [k for k, _label in options]
        widgets.set_choice_value(self.mode, current if current in self.mode.mcf_values else "new")
        if model:
            self.length.SetMax(int(model.get("max_seconds", 600)))
            defaults = model.get("defaults", {})
            if "steps" in defaults:
                self.steps.SetValue(defaults["steps"])
            if "guidance" in defaults:
                self.guidance_scale.SetValue(defaults["guidance"])
        self.update_visibility()

    def on_mode_changed(self):
        self.update_visibility()
        mode = widgets.choice_value(self.mode)
        if mode == "cover" and not self.frame.services.cover_tools_ready():
            self.frame.announcer.announce(
                _("Covers need the cover analysis tools, about 2 GB. Install them in Preferences, on the "
                  "Models tab."), Kind.INFO)

    # Models, presets and profiles

    def refresh_models(self):
        services = self.frame.services
        self.model_ids = [m["id"] for m in services.ready_models()]
        self.model.Clear()
        for model_id in self.model_ids:
            self.model.Append(services.model(model_id)["name"])
        if not self.model_ids:
            self.model.Append(_("No models installed: open Preferences, Models"))
            self.model.SetSelection(0)
            return
        wanted = self.frame.settings.get("generation.model")
        self.model.SetSelection(self.model_ids.index(wanted) if wanted in self.model_ids else 0)
        self.apply_model_features()

    def model_id(self) -> str | None:
        index = self.model.GetSelection()
        return self.model_ids[index] if self.model_ids and index >= 0 else None

    def on_model_changed(self):
        model_id = self.model_id()
        if model_id:
            self.frame.settings.set("generation.model", model_id)
        self.refresh_profiles()
        self.apply_model_features()
        model = self.current_model()
        if model:
            modes = ", ".join(f() for k, f in MODES if k in model["modes_supported"])
            self.frame.announcer.announce(_("{model} can make: {modes}.").format(model=model["name"], modes=modes),
                                          Kind.HINT)

    def refresh_presets(self, select: str | None = None):
        self.preset.Clear()
        self.preset.Append(_("Default settings"))
        names = presets.generation_presets().names()
        for name in names:
            self.preset.Append(name)
        self.preset.SetSelection(names.index(select) + 1 if select in names else 0)

    def refresh_profiles(self):
        services = self.frame.services
        profiles, _damaged = services.profiles.list()
        model_id = self.model_id()
        self.profile_list = [p for p in profiles if p.model == model_id]
        current = self.profile.GetStringSelection() if self.profile.GetCount() else ""
        self.profile.Clear()
        self.profile.Append(_("None"))
        for profile in self.profile_list:
            self.profile.Append(profile.name)
        index = self.profile.FindString(current)
        self.profile.SetSelection(index if index != wx.NOT_FOUND else 0)
        self.update_visibility()

    def selected_profile(self):
        index = self.profile.GetSelection()
        return self.profile_list[index - 1] if index > 0 else None

    def on_profile_changed(self):
        profile = self.selected_profile()
        if profile:
            self.profile_strength.SetValue(profile.strength)
            if profile.style and not self.style.GetValue().strip():
                self.style.SetValue(profile.style)
        self.update_visibility()

    def values(self) -> dict:
        """Everything a preset stores."""
        return {
            "mode": widgets.choice_value(self.mode),
            "style": self.style.GetValue(),
            "lyrics": self.lyrics.GetValue(),
            "language": widgets.choice_value(self.language),
            "instrumental": self.instrumental.GetValue(),
            "length": self.length.GetValue(),
            "seed": self.seed.GetValue(),
            "steps": self.steps.GetValue(),
            "guidance": self.guidance_scale.GetValue(),
            "profile": self.profile.GetStringSelection() if self.profile.GetSelection() > 0 else "",
            "profile_strength": self.profile_strength.GetValue(),
        }

    def apply_values(self, values: dict):
        if "mode" in values:
            widgets.set_choice_value(self.mode, values["mode"])
        self.style.SetValue(values.get("style", self.style.GetValue()))
        self.lyrics.SetValue(values.get("lyrics", self.lyrics.GetValue()))
        if "language" in values:
            widgets.set_choice_value(self.language, values["language"])
        self.instrumental.SetValue(bool(values.get("instrumental", self.instrumental.GetValue())))
        for ctrl, key in ((self.length, "length"), (self.seed, "seed"), (self.steps, "steps")):
            if key in values:
                ctrl.SetValue(int(values[key]))
        if "guidance" in values:
            self.guidance_scale.SetValue(float(values["guidance"]))
        if values.get("profile"):
            index = self.profile.FindString(values["profile"])
            if index != wx.NOT_FOUND:
                self.profile.SetSelection(index)
                self.profile_strength.SetValue(float(values.get("profile_strength", 1.0)))
        self.update_visibility()

    def on_preset_chosen(self):
        index = self.preset.GetSelection()
        if index <= 0:
            self.apply_values({"seed": -1, "steps": self.frame.settings.get("generation.steps"),
                               "guidance": self.frame.settings.get("generation.guidance"),
                               "length": self.frame.settings.get("generation.length_seconds")})
            return
        values = presets.generation_presets().load(self.preset.GetStringSelection())
        if values is None:
            self.frame.announcer.announce(_("That preset can't be read."), Kind.ERROR)
            return
        self.apply_values(values)
        self.frame.announcer.announce(_("Preset loaded."), Kind.INFO)

    def on_save_preset(self, event):
        current = self.preset.GetStringSelection() if self.preset.GetSelection() > 0 else ""
        name = ask_text(self, _("Save preset"), _("Name for this preset:"), current)
        if name:
            presets.generation_presets().save(name, self.values())
            self.refresh_presets(name)
            self.frame.announcer.announce(_("Preset {name} saved.").format(name=name), Kind.COMPLETION)
        self.preset.SetFocus()

    def on_delete_preset(self, event):
        if self.preset.GetSelection() <= 0:
            self.frame.announcer.announce(_("Default settings can't be deleted."), Kind.INFO)
            return
        presets.generation_presets().delete(self.preset.GetStringSelection())
        self.refresh_presets()
        self.frame.announcer.announce(_("Preset deleted."), Kind.COMPLETION)
        self.preset.SetFocus()

    # Lyrics field

    def on_lyrics_key(self, event: wx.KeyEvent):
        key = event.GetKeyCode()
        mods = event.GetModifiers()
        if key == ord("T") and mods == wx.MOD_CONTROL:
            self.show_tag_menu()
        elif key == ord("K") and mods == wx.MOD_CONTROL | wx.MOD_SHIFT:
            self.frame.check_lyrics()
        else:
            event.Skip()

    def show_tag_menu(self):
        menu = wx.Menu()
        sections = wx.Menu()
        for name in lyrics.SECTION_NAMES:
            item = sections.Append(wx.ID_ANY, name)
            self.Bind(wx.EVT_MENU, lambda e, n=name: self.insert_line(f"[{n}]"), item)
        menu.AppendSubMenu(sections, _("&Section"))
        settings_menu = wx.Menu()
        examples = {"bpm": "120", "key": "A minor", "time signature": "4/4", "length": "3:00",
                    "seed": "42", "instrumental": "yes", "language": "English"}
        profiles = [p.name for p in self.profile_list]
        if profiles:
            examples["profile"] = profiles[0]
        for name, example in examples.items():
            item = settings_menu.Append(wx.ID_ANY, f"{{{name}: {example}}}")
            self.Bind(wx.EVT_MENU, lambda e, n=name, x=example: self.insert_line(f"{{{n}: {x}}}", top=True), item)
        menu.AppendSubMenu(settings_menu, _("Song se&tting (goes at the top)"))
        menu.AppendSeparator()
        check = menu.Append(wx.ID_ANY, _("&Check lyrics\tCtrl+Shift+K"))
        self.Bind(wx.EVT_MENU, lambda e: self.frame.check_lyrics(), check)
        reference = menu.Append(wx.ID_ANY, _("Tag &reference\tF1"))
        self.Bind(wx.EVT_MENU, lambda e: self.frame.explain_focused(), reference)
        self.lyrics.PopupMenu(menu)
        menu.Destroy()

    def insert_line(self, text: str, top: bool = False):
        ctrl = self.lyrics
        if top:
            ctrl.SetInsertionPoint(0)
            ctrl.WriteText(text + "\n")
        else:
            found, _col, line = ctrl.PositionToXY(ctrl.GetInsertionPoint())
            line_text = ctrl.GetLineText(line) if found else ""
            if line_text.strip():
                ctrl.SetInsertionPoint(ctrl.XYToPosition(len(line_text), line))
                ctrl.WriteText("\n" + text)
            else:
                ctrl.WriteText(text)
            pos = ctrl.GetInsertionPoint()
            if pos >= ctrl.GetLastPosition():
                ctrl.WriteText("\n")
            else:
                ctrl.SetInsertionPoint(pos + 1)
        ctrl.SetFocus()
        self.frame.announcer.announce(_("Inserted {tag}").format(tag=text), Kind.INFO)

    def set_running(self, running: bool):
        self.generate.SetLabel(_("Ca&ncel") if running else _("Ge&nerate"))
