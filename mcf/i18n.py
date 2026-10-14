"""Translation support.

All user-facing text is wrapped in _() so the community can translate MCF
later with standard gettext tools. Until translations exist, text is shown
as written.
"""

import gettext

from mcf import paths

_translation = gettext.NullTranslations()


def set_language(language: str) -> None:
    """Load a translation. "system" uses the Windows display language."""
    global _translation
    languages = None if language in ("", "system") else [language]
    _translation = gettext.translation(
        "mcf", localedir=str(paths.locale_dir()), languages=languages, fallback=True
    )


def _(message: str) -> str:
    return _translation.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _translation.ngettext(singular, plural, n)
