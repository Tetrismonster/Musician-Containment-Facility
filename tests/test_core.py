import json

import pytest

from mcf import hotkeys, lyrics
from mcf.announcer import Announcer, Kind
from mcf.settings import Settings


# Settings

def test_settings_defaults_when_missing(tmp_path):
    s = Settings(tmp_path / "settings.json")
    s.load()
    assert s.get("speech.verbosity") == "normal"


def test_settings_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path)
    s.set("speech.verbosity", "quiet")
    s.save()
    t = Settings(path)
    t.load()
    assert t.get("speech.verbosity") == "quiet"


def test_settings_ignore_bad_values(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"speech": {"verbosity": 5, "unknown": 1}, "bogus": {}}))
    s = Settings(path)
    s.load()
    assert s.get("speech.verbosity") == "normal"
    assert "bogus" not in s.data


def test_settings_corrupt_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    s = Settings(path)
    s.load()
    assert s.get("keyboard.cancel_hotkey") == "ctrl+alt+shift+c"


def test_settings_unknown_key(tmp_path):
    with pytest.raises(KeyError):
        Settings(tmp_path / "s.json").set("speech.nope", 1)


# Hotkeys

def test_parse_default_hotkey():
    hk = hotkeys.parse("Ctrl+Alt+Shift+C")
    assert hk.modifiers == hotkeys.MOD_CONTROL | hotkeys.MOD_ALT | hotkeys.MOD_SHIFT
    assert hk.vk == ord("C")
    assert hk.text == "ctrl+alt+shift+c"
    assert hk.describe() == "Ctrl+Alt+Shift+C"


def test_parse_orders_modifiers():
    assert hotkeys.parse("shift+ctrl+f5").text == "ctrl+shift+f5"


@pytest.mark.parametrize("text", ["", "c", "ctrl+", "hyper+c", "ctrl+tab"])
def test_parse_rejects(text):
    with pytest.raises(ValueError):
        hotkeys.parse(text)


def test_from_key_event():
    hk = hotkeys.from_key_event(hotkeys.MOD_CONTROL | hotkeys.MOD_ALT, 0x75)
    assert hk.text == "ctrl+alt+f6"
    assert hotkeys.from_key_event(hotkeys.MOD_CONTROL, 0x11) is None  # Ctrl alone


# Lyrics tags

SONG = """{bpm: 120}
{key: A minor}
{length: 2:30}

[Verse: soft, piano only]
Walking down the road
Nothing left to say

[Chorus]
Sing it loud
"""


def test_lyrics_parse():
    parsed = lyrics.parse(SONG)
    assert parsed.settings == {"bpm": 120, "key": "A minor", "length": 150}
    assert [(s.name, s.details) for s in parsed.sections] == [
        ("Verse", "soft, piano only"), ("Chorus", "")]
    assert parsed.sections[0].lines == ["Walking down the road", "Nothing left to say"]
    assert parsed.has_words
    assert not parsed.problems


def test_lyrics_problems():
    parsed = lyrics.parse("{bpm: fast}\n{tempo: 3}\n[Verse]\nla\n{key: C}")
    assert len(parsed.problems) == 3
    assert parsed.settings == {}


def test_lyrics_instrumental():
    parsed = lyrics.parse("[Intro]\n[Solo: guitar]")
    assert not parsed.has_words
    assert "instrumental" in lyrics.describe(parsed)


@pytest.mark.parametrize("text,seconds", [("90", 90), ("90s", 90), ("1:30", 90), ("1m30s", 90), ("2 min", 120)])
def test_parse_length(text, seconds):
    assert lyrics.parse_length(text) == seconds


# Announcer filtering

@pytest.mark.parametrize("verbosity,kind,detail,expected", [
    ("quiet", Kind.ERROR, False, True),
    ("quiet", Kind.COMPLETION, False, True),
    ("quiet", Kind.PROGRESS, False, False),
    ("normal", Kind.PROGRESS, False, True),
    ("normal", Kind.PROGRESS, True, False),
    ("detailed", Kind.PROGRESS, True, True),
])
def test_announcer_filter(tmp_path, verbosity, kind, detail, expected):
    s = Settings(tmp_path / "s.json")
    s.set("speech.verbosity", verbosity)
    assert Announcer(s).should_speak(kind, detail) is expected


def test_announcer_history_without_speech(tmp_path):
    s = Settings(tmp_path / "s.json")
    s.set("speech.output", "none")
    a = Announcer(s)
    seen = []
    a.status_listeners.append(seen.append)
    a.announce("hello", Kind.INFO)
    assert seen == ["hello"]
    assert a.history[-1].text == "hello"


def test_hotkey_number_row_backspace_and_punctuation():
    ctrl_shift = hotkeys.MOD_CONTROL | hotkeys.MOD_SHIFT
    assert hotkeys.from_key_event(ctrl_shift, 0x31).text == "ctrl+shift+1"
    assert hotkeys.from_key_event(ctrl_shift, 0x08).text == "ctrl+shift+backspace"
    assert hotkeys.from_key_event(hotkeys.MOD_CONTROL, 0xBC).text == "ctrl+comma"
    assert hotkeys.parse("ctrl+shift+backspace").vk == 0x08
    assert hotkeys.describe("ctrl+shift+backspace") == "Ctrl+Shift+Backspace"
    assert hotkeys.from_key_event(ctrl_shift, 0xA0) is None  # Left Shift alone
    with pytest.raises(ValueError):
        hotkeys.from_key_event(hotkeys.MOD_CONTROL, 0x09)  # Tab isn't allowed
