"""Parse the tags people type in the lyrics field.

Two kinds of tags are supported:

- Section tags on their own line: [Verse], [Chorus: whispered, add strings]
- Song-wide settings at the top: {bpm: 120}, {key: A minor}, {length: 3:30}

Each model backend later turns the parsed result into whatever format that
model understands.
"""

import re
from dataclasses import dataclass, field

from mcf.i18n import _

SECTION_RE = re.compile(r"^\[\s*([^\]:]+?)\s*(?::\s*([^\]]*?))?\s*\]$")
SETTING_RE = re.compile(r"^\{\s*([A-Za-z_ ]+?)\s*:\s*([^}]*?)\s*\}$")

# Common section names offered by the Insert tag menu
SECTION_NAMES = ["Intro", "Verse", "Pre-Chorus", "Chorus", "Bridge",
                 "Instrumental break", "Solo", "Outro"]


def _parse_int(low: int, high: int):
    def parse(value: str) -> int:
        number = int(value)
        if not low <= number <= high:
            raise ValueError
        return number
    return parse


def _parse_float(low: float, high: float):
    def parse(value: str) -> float:
        number = float(value)
        if not low <= number <= high:
            raise ValueError
        return number
    return parse


def parse_length(value: str) -> int:
    """Seconds from "90", "90s", "1:30" or "1m30s"."""
    value = value.strip().lower()
    if match := re.fullmatch(r"(\d+):([0-5]\d)", value):
        seconds = int(match[1]) * 60 + int(match[2])
    elif match := re.fullmatch(r"(?:(\d+)\s*m(?:in)?)?\s*(?:(\d+)\s*s(?:ec)?)?", value):
        if not match[1] and not match[2]:
            seconds = int(value)  # plain number, or raises ValueError
        else:
            seconds = int(match[1] or 0) * 60 + int(match[2] or 0)
    else:
        seconds = int(value)
    if not 1 <= seconds <= 60 * 60:
        raise ValueError
    return seconds


def _parse_bool(value: str) -> bool:
    value = value.strip().lower()
    if value in ("yes", "true", "on", "1"):
        return True
    if value in ("no", "false", "off", "0"):
        return False
    raise ValueError


# name: (parser, description of valid values)
SETTINGS = {
    "bpm": (_parse_int(20, 400), "a whole number from 20 to 400"),
    "key": (str, "a musical key, such as A minor"),
    "time signature": (str, "such as 4/4 or 3/4"),
    "length": (parse_length, "seconds, or minutes and seconds such as 3:30"),
    "seed": (_parse_int(-1, 2**32 - 1), "a whole number, or -1 for random"),
    "steps": (_parse_int(1, 1000), "a whole number"),
    "guidance": (_parse_float(0.0, 50.0), "a number such as 7.5"),
    "instrumental": (_parse_bool, "yes or no"),
    "model": (str, "a model name"),
    "language": (str, "a language name, such as English"),
    "profile": (str, "the name of one of your artist profiles"),
}


@dataclass
class Section:
    name: str
    details: str = ""
    lines: list[str] = field(default_factory=list)
    line_number: int = 0


@dataclass
class ParsedLyrics:
    settings: dict[str, object] = field(default_factory=dict)
    sections: list[Section] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def has_words(self) -> bool:
        return any(line.strip() for section in self.sections for line in section.lines)


def parse(text: str) -> ParsedLyrics:
    result = ParsedLyrics()
    current: Section | None = None
    seen_content = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            if current is not None:
                current.lines.append("")
            continue
        if match := SETTING_RE.match(line):
            name = " ".join(match[1].lower().split())
            if seen_content:
                result.problems.append(
                    _("Line {line}: the setting {{{name}}} is ignored because settings must "
                      "come before any lyrics or sections.").format(line=number, name=name))
                continue
            if name not in SETTINGS:
                result.problems.append(
                    _("Line {line}: unknown setting \"{name}\". Known settings are: {known}.")
                    .format(line=number, name=name, known=", ".join(SETTINGS)))
                continue
            parser, valid = SETTINGS[name]
            try:
                result.settings[name] = parser(match[2])
            except ValueError:
                result.problems.append(
                    _("Line {line}: \"{value}\" is not a valid {name}. Use {valid}.")
                    .format(line=number, value=match[2], name=name, valid=_(valid)))
            continue
        seen_content = True
        if match := SECTION_RE.match(line):
            current = Section(match[1], (match[2] or "").strip(), line_number=number)
            result.sections.append(current)
            continue
        if current is None:
            current = Section("", line_number=number)
            result.sections.append(current)
        current.lines.append(line)
    for section in result.sections:
        while section.lines and not section.lines[-1]:
            section.lines.pop()
    return result


def describe(parsed: ParsedLyrics) -> str:
    """A plain-language summary for the Check lyrics command."""
    parts = []
    if parsed.settings:
        items = ", ".join(f"{name} {value}" for name, value in parsed.settings.items())
        parts.append(_("Settings: {items}.").format(items=items))
    named = [s.name if not s.details else f"{s.name} ({s.details})"
             for s in parsed.sections if s.name]
    if named:
        parts.append(_("{count} sections: {names}.").format(count=len(named), names=", ".join(named)))
    if not parsed.has_words:
        parts.append(_("No sung words found, so the song will be instrumental."))
    if parsed.problems:
        parts.append(_("{count} problems:").format(count=len(parsed.problems)))
        parts.extend(parsed.problems)
    else:
        parts.append(_("No problems found."))
    return "\n".join(parts)


# Languages the models understand, by the names people type
LANGUAGE_CODES = {
    "english": "en", "chinese": "zh", "mandarin": "zh", "japanese": "ja", "korean": "ko", "spanish": "es",
    "french": "fr", "german": "de", "italian": "it", "portuguese": "pt", "russian": "ru", "dutch": "nl",
    "polish": "pl", "turkish": "tr", "arabic": "ar", "hindi": "hi", "swedish": "sv", "norwegian": "no",
    "danish": "da", "finnish": "fi", "greek": "el", "czech": "cs", "hungarian": "hu", "romanian": "ro",
    "ukrainian": "uk", "vietnamese": "vi", "thai": "th", "indonesian": "id", "malay": "ms", "hebrew": "he",
    "cantonese": "yue", "bengali": "bn", "tamil": "ta", "urdu": "ur", "persian": "fa", "tagalog": "tl",
}


def language_code(name: str) -> str | None:
    name = name.strip().lower()
    if name in LANGUAGE_CODES.values():
        return name
    return LANGUAGE_CODES.get(name)


def for_ace_step(parsed: ParsedLyrics) -> tuple[str, list[str]]:
    """Lyrics in ACE-Step's format, and the tags it had to ignore."""
    lines, ignored = [], []
    for section in parsed.sections:
        if section.name:
            if section.details:
                ignored.append(f"[{section.name}: {section.details}]")
            lines.append(f"[{section.name}]")
        lines.extend(section.lines)
        lines.append("")
    return "\n".join(lines).strip(), ignored
