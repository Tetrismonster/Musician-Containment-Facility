"""Parse and describe keyboard shortcuts such as "ctrl+alt+shift+c"."""

from dataclasses import dataclass

from mcf.i18n import _

MOD_ALT = 0x1
MOD_CONTROL = 0x2
MOD_SHIFT = 0x4
MOD_WIN = 0x8

_MODIFIERS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_MODIFIER_ORDER = ["ctrl", "alt", "shift", "win"]

# Windows virtual key codes for keys that are not letters or digits
_NAMED_KEYS = {f"f{n}": 0x6F + n for n in range(1, 13)}
_NAMED_KEYS.update({f"numpad{n}": 0x60 + n for n in range(10)})
_NAMED_KEYS.update({
    "backspace": 0x08, "enter": 0x0D, "pause": 0x13, "space": 0x20, "home": 0x24, "end": 0x23,
    "insert": 0x2D, "delete": 0x2E, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    # Punctuation, named after the key on a US keyboard. "+" separates parts, so none use symbols.
    "semicolon": 0xBA, "equals": 0xBB, "comma": 0xBC, "minus": 0xBD, "period": 0xBE, "slash": 0xBF,
    "grave": 0xC0, "leftbracket": 0xDB, "backslash": 0xDC, "rightbracket": 0xDD, "quote": 0xDE,
})
_VK_TO_NAME = {vk: name for name, vk in _NAMED_KEYS.items()}


@dataclass(frozen=True)
class Hotkey:
    modifiers: int  # MOD_* flags, as used by RegisterHotKey
    vk: int         # Windows virtual key code
    text: str       # Canonical form, e.g. "ctrl+alt+shift+c"

    def describe(self) -> str:
        return describe(self.text)


def parse(text: str) -> Hotkey:
    """Parse a shortcut string. Raises ValueError if it is not usable."""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if not parts:
        raise ValueError(_("The shortcut is empty."))
    *mods, key = parts
    modifiers = 0
    for mod in mods:
        if mod not in _MODIFIERS:
            raise ValueError(_("Unknown modifier key: {name}").format(name=mod))
        modifiers |= _MODIFIERS[mod]
    if key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    elif len(key) == 1 and key.isalnum() and key.isascii():
        vk = ord(key.upper())
    else:
        raise ValueError(_("Unsupported key: {name}").format(name=key))
    if not modifiers and key not in {f"f{n}" for n in range(1, 13)}:
        raise ValueError(_("A system-wide shortcut needs at least one of Ctrl, Alt, Shift or Windows."))
    canonical = "+".join([m for m in _MODIFIER_ORDER if _MODIFIERS[m] & modifiers] + [key])
    return Hotkey(modifiers, vk, canonical)


# Shift, Control, Alt (each in generic, left and right forms), the Windows keys, and the
# lock keys, which screen readers often use as their modifier
_MODIFIER_VKS = {0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C, 0x14, 0x90, 0x91}


def from_key_event(modifiers: int, vk: int) -> Hotkey | None:
    """Build a hotkey from pressed keys. Returns None for modifier-only presses.

    Raises ValueError for a key that can't be part of a shortcut, so the user hears why.
    """
    if vk in _MODIFIER_VKS:
        return None
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        key = chr(vk).lower()
    elif vk in _VK_TO_NAME:
        key = _VK_TO_NAME[vk]
    else:
        raise ValueError(_("That key can't be used in a shortcut. Try a letter, number, function key or "
                           "Backspace."))
    names = [m for m in _MODIFIER_ORDER if _MODIFIERS[m] & modifiers]
    return parse("+".join(names + [key]))


def describe(text: str) -> str:
    """Readable form, e.g. "Ctrl+Alt+Shift+C"."""
    labels = {"ctrl": _("Ctrl"), "alt": _("Alt"), "shift": _("Shift"), "win": _("Windows"),
              "pageup": _("Page Up"), "pagedown": _("Page Down"), "space": _("Space"),
              "backspace": _("Backspace"), "enter": _("Enter"), "left": _("Left Arrow"), "right": _("Right Arrow"),
              "up": _("Up Arrow"), "down": _("Down Arrow"), "semicolon": _("Semicolon"), "equals": _("Equals"),
              "comma": _("Comma"), "minus": _("Minus"), "period": _("Period"), "slash": _("Slash"),
              "grave": _("Grave accent"), "leftbracket": _("Left Bracket"), "backslash": _("Backslash"),
              "rightbracket": _("Right Bracket"), "quote": _("Quote")}
    labels.update({f"numpad{n}": _("Numpad {n}").format(n=n) for n in range(10)})
    parts = []
    for part in text.lower().split("+"):
        parts.append(labels.get(part, part.upper() if len(part) <= 3 else part.capitalize()))
    return "+".join(parts)
