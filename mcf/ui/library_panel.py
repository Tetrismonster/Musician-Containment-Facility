"""The Library tab: every song made, with keyboard playback and saving."""

import time
from pathlib import Path

import wx

from mcf import audio_export
from mcf.announcer import Kind
from mcf.i18n import _
from mcf.ui import widgets
from mcf.ui.components import ask, ask_text
from mcf.ui.player import Player
from mcf.ui.widgets import labeled, read_only_text, tag

MODE_NAMES = {"new": lambda: _("New song"), "cover": lambda: _("Cover"), "extend": lambda: _("Extension"),
              "variation": lambda: _("Variation"), "inpaint": lambda: _("Edited section"),
              "reference": lambda: _("From reference audio")}


def duration_text(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class LibraryPanel(wx.Panel):
    def __init__(self, parent, frame):
        super().__init__(parent)
        self.frame = frame
        self.songs = []
        self.player = Player(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.list = labeled(self, sizer, _("&Songs:"), lambda p: wx.ListCtrl(
            p, style=wx.LC_REPORT | wx.LC_SINGLE_SEL), "library_list", proportion=1)
        for title, width in ((_("Name"), 260), (_("Type"), 120), (_("Model"), 140), (_("Length"), 70),
                             (_("Created"), 150), (_("Favourite"), 80)):
            self.list.AppendColumn(title, width=width)
        self.list.Bind(wx.EVT_KEY_DOWN, self.on_key)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda e: self.show_details())
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self.toggle_play())

        buttons = wx.WrapSizer(wx.HORIZONTAL)
        for label, handler in ((_("&Play or pause"), lambda e: self.toggle_play()),
                               (_("Save &as..."), lambda e: self.save_as()),
                               (_("&Rename..."), lambda e: self.rename()),
                               (_("&Tags..."), lambda e: self.edit_tags()),
                               (_("Fa&vourite"), lambda e: self.toggle_favourite()),
                               (_("&Delete"), lambda e: self.delete()),
                               (_("&Use these settings again"), lambda e: self.reuse()),
                               (_("Use as s&ource"), lambda e: self.use_as_source())):
            button = tag(wx.Button(self, label=label), "library_list")
            button.Bind(wx.EVT_BUTTON, handler)
            buttons.Add(button, 0, wx.ALL, widgets.LABEL_BORDER)
        sizer.Add(buttons, 0, wx.EXPAND)
        self.details = labeled(self, sizer, _("D&etails:"), lambda p: read_only_text(
            p, _("No songs yet. Songs you make appear here automatically."), height=160), "library_details")
        self.SetSizer(sizer)
        self.refresh()

    # List

    def refresh(self, select_id: str | None = None):
        previous = self.selected()
        select_id = select_id or (previous.id if previous else None)
        self.songs = self.frame.services.library.all()
        self.list.DeleteAllItems()
        for index, song in enumerate(self.songs):
            self.list.InsertItem(index, song.title)
            self.list.SetItem(index, 1, MODE_NAMES.get(song.mode, lambda: song.mode)())
            self.list.SetItem(index, 2, song.model)
            self.list.SetItem(index, 3, duration_text(song.seconds))
            self.list.SetItem(index, 4, time.strftime("%Y-%m-%d %H:%M", time.localtime(song.created)))
            self.list.SetItem(index, 5, _("Yes") if song.favourite else "")
        if self.songs:
            index = next((i for i, s in enumerate(self.songs) if s.id == select_id), 0)
            self.list.Select(index)
            self.list.Focus(index)
        self.show_details()

    def selected(self):
        index = self.list.GetFirstSelected()
        return self.songs[index] if 0 <= index < len(self.songs) else None

    def _need_song(self):
        song = self.selected()
        if song is None:
            self.frame.announcer.announce(_("No song selected."), Kind.INFO)
        return song

    def show_details(self):
        song = self.selected()
        if song is None:
            self.details.SetValue(_("No songs yet. Songs you make appear here automatically."))
            return
        s = song.settings
        lines = [song.title, audio_export.credit_line(song)]
        lines.append(_("Length: {length}. Created {date}.").format(
            length=duration_text(song.seconds), date=time.strftime("%Y-%m-%d %H:%M", time.localtime(song.created))))
        if song.tags:
            lines.append(_("Tags: {tags}.").format(tags=song.tags))
        if song.style:
            lines.append(_("Style description: {style}").format(style=song.style))
        for key, label in (("seed", _("Seed")), ("steps", _("Steps")), ("guidance", _("Guidance strength")),
                           ("bpm", _("Tempo")), ("key", _("Key")), ("language", _("Lyrics language"))):
            if s.get(key) not in (None, ""):
                lines.append(f"{label}: {s[key]}")
        if s.get("follow"):
            followed = [k for k, v in s["follow"].items() if v]
            lines.append(_("Cover follows: {items}.").format(items=", ".join(followed)))
        if song.lyrics:
            lines += ["", _("Lyrics:"), song.lyrics]
        self.details.SetValue("\n".join(lines))

    # Keyboard

    def on_key(self, event: wx.KeyEvent):
        key, mods = event.GetKeyCode(), event.GetModifiers()
        if key == wx.WXK_SPACE and mods == 0:
            self.toggle_play()
        elif key in (wx.WXK_LEFT, wx.WXK_RIGHT) and mods in (0, wx.MOD_SHIFT):
            step = 5 if mods == 0 else 30
            self.seek(step if key == wx.WXK_RIGHT else -step)
        elif key in (wx.WXK_UP, wx.WXK_DOWN) and mods == wx.MOD_CONTROL:
            volume = self.player.change_volume(0.1 if key == wx.WXK_UP else -0.1)
            self.frame.announcer.announce(_("Volume {n} percent").format(n=round(volume * 100)), Kind.INFO,
                                          interrupt=True)
        elif key == wx.WXK_DELETE and mods == 0:
            self.delete()
        elif key == wx.WXK_F2 and mods == 0:
            self.rename()
        else:
            event.Skip()

    # Playback

    def toggle_play(self):
        song = self._need_song()
        if song is None:
            return
        path = self.frame.services.library.path(song)
        if not path.exists():
            self.frame.announcer.announce(_("The audio file for this song is missing."), Kind.ERROR)
            return
        state = self.player.toggle(path)
        if state == "failed":
            self.frame.announcer.announce(_("This song can't be played."), Kind.ERROR)
        elif state == "paused":
            self.frame.announcer.announce(_("Paused at {time}.").format(
                time=duration_text(self.player.position())), Kind.INFO, interrupt=True)
        else:
            self.frame.announcer.announce(_("Playing {title}.").format(title=song.title), Kind.INFO, interrupt=True)

    def seek(self, seconds: float):
        if self.player.loaded is None:
            return
        position = self.player.seek(seconds)
        self.frame.announcer.announce(duration_text(position), Kind.INFO, interrupt=True)

    def stop(self):
        self.player.stop()

    # Actions

    def save_as(self):
        song = self._need_song()
        if song is None:
            return
        settings = self.frame.settings
        fmt = settings.get("saving.default_format")
        from mcf.ui.onboarding import default_export_folder
        folder = Path(settings.get("saving.default_folder") or default_export_folder())
        name = "".join(c for c in song.title if c not in '<>:"/\\|?*').strip() or "song"
        ext = audio_export.FORMATS[fmt][2]
        if settings.get("saving.always_ask"):
            with wx.FileDialog(self, _("Save song as"), str(folder), name + ext, audio_export.wildcard(),
                               wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
                dialog.SetFilterIndex(audio_export.FORMAT_ORDER.index(fmt))
                if dialog.ShowModal() != wx.ID_OK:
                    self.list.SetFocus()
                    return
                dest = Path(dialog.GetPath())
                fmt = audio_export.FORMAT_ORDER[dialog.GetFilterIndex()]
                wanted_ext = audio_export.FORMATS[fmt][2]
                if dest.suffix.lower() != wanted_ext:
                    dest = dest.with_suffix(wanted_ext)
        else:
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / (name + ext)
            n = 2
            while dest.exists():
                dest = folder / f"{name} {n}{ext}"
                n += 1
        try:
            with wx.BusyCursor():
                audio_export.export(self.frame.services.library.path(song), dest, fmt, song,
                                    settings.get("saving.mp3_bitrate"))
        except Exception as exc:
            self.frame.announcer.announce(_("Saving failed: {error}").format(error=exc), Kind.ERROR)
        else:
            self.frame.announcer.announce(_("Saved {name} to {folder}.").format(name=dest.name, folder=dest.parent),
                                          Kind.COMPLETION)
        self.list.SetFocus()

    def rename(self):
        song = self._need_song()
        if song is None:
            return
        name = ask_text(self, _("Rename song"), _("New name:"), song.title)
        if name:
            song.title = name
            self.frame.services.library.update(song)
            self.refresh(song.id)
            self.frame.announcer.announce(_("Renamed to {name}.").format(name=name), Kind.COMPLETION)
        self.list.SetFocus()

    def edit_tags(self):
        song = self._need_song()
        if song is None:
            return
        tags = ask_text(self, _("Tags"), _("Tags, separated by commas:"), song.tags)
        if tags is not None:
            song.tags = tags
            self.frame.services.library.update(song)
            self.refresh(song.id)
            self.frame.announcer.announce(_("Tags saved."), Kind.COMPLETION)
        self.list.SetFocus()

    def toggle_favourite(self):
        song = self._need_song()
        if song is None:
            return
        song.favourite = not song.favourite
        self.frame.services.library.update(song)
        self.refresh(song.id)
        self.frame.announcer.announce(_("Marked as a favourite.") if song.favourite
                                      else _("No longer a favourite."), Kind.INFO)

    def delete(self):
        song = self._need_song()
        if song is None:
            return
        if not ask(self, _("Delete song"), _("Delete {title}? This deletes its audio file too, and can't be "
                                              "undone.").format(title=song.title), _("&Delete"), _("&Keep")):
            self.list.SetFocus()
            return
        if self.player.loaded == self.frame.services.library.path(song):
            self.player.unload()
        index = self.list.GetFirstSelected()
        self.frame.services.library.delete(song)
        self.refresh()
        if self.songs:
            index = min(index, len(self.songs) - 1)
            self.list.Select(index)
            self.list.Focus(index)
        self.frame.announcer.announce(_("Song deleted."), Kind.COMPLETION)
        self.list.SetFocus()

    def reuse(self):
        song = self._need_song()
        if song is None:
            return
        values = dict(song.settings.get("values", {}))
        values.setdefault("style", song.style)
        values.setdefault("lyrics", song.lyrics)
        self.frame.generate_panel.apply_values(values)
        self.frame.show_page(0)
        self.frame.announcer.announce(_("Settings copied to the Generate tab."), Kind.INFO)

    def use_as_source(self):
        song = self._need_song()
        if song is None:
            return
        panel = self.frame.generate_panel
        panel.source.set(self.frame.services.library.path(song), song.title, song.id)
        mode = widgets.choice_value(panel.mode)
        if mode not in ("extend", "variation", "inpaint"):
            widgets.set_choice_value(panel.mode, "variation")
        panel.update_visibility()
        self.frame.show_page(0)
        panel.mode.SetFocus()
        self.frame.announcer.announce(_("{title} is now the source song. Choose Extend, Variation or Replace a "
                                        "section in the Mode list.").format(title=song.title), Kind.INFO)
