"""Audio playback for the library, using Windows Media Player's engine through wx.media."""

import logging
from pathlib import Path

import wx
import wx.media

log = logging.getLogger(__name__)


class Player:
    def __init__(self, parent: wx.Window):
        self.ctrl = wx.media.MediaCtrl(parent, szBackend=wx.media.MEDIABACKEND_WMP10, size=(0, 0))
        self.ctrl.Hide()
        self.loaded: Path | None = None
        self.volume = 0.8
        self._pending_play = False
        self.ctrl.Bind(wx.media.EVT_MEDIA_LOADED, self._on_loaded)

    def _on_loaded(self, event):
        self.ctrl.SetVolume(self.volume)
        if self._pending_play:
            self._pending_play = False
            self.ctrl.Play()

    def load(self, path: Path) -> bool:
        if self.loaded == path:
            return True
        self.stop()
        if not self.ctrl.Load(str(path)):
            log.error("Could not load %s for playback", path)
            self.loaded = None
            return False
        self.loaded = path
        return True

    def toggle(self, path: Path) -> str:
        """Play or pause. Returns playing, paused or failed."""
        if self.loaded != path:
            if not self.load(path):
                return "failed"
            self._pending_play = True
            # Some backends load synchronously and never send the loaded event
            wx.CallLater(400, self._play_if_pending)
            return "playing"
        if self.ctrl.GetState() == wx.media.MEDIASTATE_PLAYING:
            self.ctrl.Pause()
            return "paused"
        self.ctrl.Play()
        return "playing"

    def _play_if_pending(self):
        if self._pending_play:
            self._pending_play = False
            self.ctrl.SetVolume(self.volume)
            self.ctrl.Play()

    def stop(self):
        self._pending_play = False
        if self.loaded is not None:
            self.ctrl.Stop()

    def unload(self):
        """Release the file, for example before deleting it."""
        self.stop()
        self.ctrl.Load("")
        self.loaded = None

    def seek(self, seconds: float) -> float:
        if self.loaded is None:
            return 0.0
        position = max(0, self.ctrl.Tell() + int(seconds * 1000))
        length = self.ctrl.Length()
        if length > 0:
            position = min(position, length - 100)
        self.ctrl.Seek(position)
        return position / 1000

    def position(self) -> float:
        return self.ctrl.Tell() / 1000 if self.loaded else 0.0

    def change_volume(self, step: float) -> float:
        self.volume = max(0.0, min(1.0, self.volume + step))
        self.ctrl.SetVolume(self.volume)
        return self.volume

    def playing(self) -> bool:
        return self.loaded is not None and self.ctrl.GetState() == wx.media.MEDIASTATE_PLAYING
