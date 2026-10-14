"""Helpers for building accessible forms.

Screen readers on Windows name a control after the label created directly
before it, so every helper creates the label first, then the control.
"""

import wx

LABEL_BORDER = 4


def tag(ctrl: wx.Window, help_key: str) -> wx.Window:
    """Link a control to its explanation in mcf.guidance."""
    ctrl.mcf_help = help_key
    return ctrl


def help_key_for(window: wx.Window | None) -> str | None:
    """Explanation key of a window or the nearest parent that has one."""
    while window is not None:
        key = getattr(window, "mcf_help", None)
        if key:
            return key
        window = window.GetParent()
    return None


def labeled(parent: wx.Window, sizer: wx.Sizer, label: str, factory, help_key: str | None = None,
            expand: bool = True, proportion: int = 0):
    """Add a label and the control built by factory(parent) to sizer."""
    text = wx.StaticText(parent, label=label)
    ctrl = factory(parent)
    sizer.Add(text, 0, wx.LEFT | wx.RIGHT | wx.TOP, LABEL_BORDER)
    flags = wx.ALL | (wx.EXPAND if expand else 0)
    sizer.Add(ctrl, proportion, flags, LABEL_BORDER)
    if help_key:
        tag(ctrl, help_key)
    return ctrl


def read_only_text(parent: wx.Window, value: str = "", height: int = 120) -> wx.TextCtrl:
    """Multi-line read-only text that can be read line by line with arrow keys."""
    return wx.TextCtrl(
        parent, value=value, size=(-1, height),
        style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2,
    )


def choice(parent: wx.Window, options: list[tuple[str, str]], selected: str) -> wx.Choice:
    """A wx.Choice holding (value, label) pairs. Read the value with choice_value()."""
    ctrl = wx.Choice(parent, choices=[label for _, label in options])
    ctrl.mcf_values = [value for value, _ in options]
    set_choice_value(ctrl, selected)
    return ctrl


def choice_value(ctrl: wx.Choice) -> str:
    index = ctrl.GetSelection()
    return ctrl.mcf_values[index] if index != wx.NOT_FOUND else ctrl.mcf_values[0]


def set_choice_value(ctrl: wx.Choice, value: str) -> None:
    values = ctrl.mcf_values
    ctrl.SetSelection(values.index(value) if value in values else 0)


class TextDialog(wx.Dialog):
    """A dialog showing read-only text with a Close button. Focus starts in the text."""

    def __init__(self, parent: wx.Window, title: str, text: str, label: str | None = None,
                 scroll_to_end: bool = False):
        super().__init__(parent, title=title, size=(640, 480),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.text = labeled(self, sizer, label or title, lambda p: read_only_text(p, text),
                            proportion=1)
        close = wx.Button(self, wx.ID_CLOSE)
        close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        self.SetEscapeId(wx.ID_CLOSE)
        sizer.Add(close, 0, wx.ALL | wx.ALIGN_RIGHT, 8)
        self.SetSizer(sizer)
        self.text.SetFocus()
        if scroll_to_end:
            self.text.SetInsertionPointEnd()
        else:
            self.text.SetInsertionPoint(0)
