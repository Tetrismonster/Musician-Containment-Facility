"""Generate licenses/02 Third-party software.txt from the packages MCF bundles.

Run by scripts/build.py on every build. Exits with an error if any bundled
package has no license information, so nothing can be left out by accident.
"""

import importlib.metadata as md
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "licenses" / "02 Third-party software.txt"

# Parts of MCF that aren't Python packages, or that packages bundle inside them
EXTRA = [
    ("Python", "Python Software Foundation", "Python Software Foundation License", "https://www.python.org"),
    ("wxWidgets", "the wxWidgets team", "wxWindows Library Licence", "https://www.wxwidgets.org"),
    ("NVDA Controller Client, included with accessible_output2", "NV Access",
     "GNU Lesser General Public License 2.1", "https://www.nvaccess.org"),
    ("libsndfile, included with soundfile", "Erik de Castro Lopo and contributors",
     "GNU Lesser General Public License 2.1", "https://libsndfile.github.io/libsndfile/"),
    ("LAME, mpg123, libFLAC, libogg and libvorbis, included with libsndfile", "their authors",
     "LGPL 2.0 (LAME), LGPL 2.1 (mpg123), BSD 3-Clause (FLAC, Ogg, Vorbis)", ""),
    ("OpenSSL, included with cryptography", "The OpenSSL Project", "Apache License 2.0", "https://www.openssl.org"),
]
# Only needed to build MCF.exe, not bundled in it
BUILD_ONLY = {"pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile", "pytest", "pluggy",
              "iniconfig", "packaging", "setuptools", "pip", "colorama", "pygments", "pywin32-ctypes"}


def _name(requirement: str) -> str:
    return re.split(r"[\s<>=!~;\[(]", requirement, maxsplit=1)[0].strip().lower().replace("_", "-")


def bundled() -> list[md.Distribution]:
    """Every package reachable from requirements.txt."""
    wanted = [_name(line) for line in (ROOT / "requirements.txt").read_text().splitlines()
              if line.strip() and not line.startswith("#")]
    seen: dict[str, md.Distribution] = {}
    while wanted:
        name = wanted.pop()
        if name in seen or name in BUILD_ONLY:
            continue
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            print(f"ERROR: {name} is required but not installed")
            sys.exit(1)
        seen[name] = dist
        for requirement in dist.requires or []:
            if "extra ==" in requirement:
                continue
            marker = requirement.split(";", 1)[1] if ";" in requirement else ""
            if "sys_platform" in marker and "win32" not in marker and "!=" not in marker:
                continue
            wanted.append(_name(requirement))
    return sorted(seen.values(), key=lambda d: d.metadata["Name"].lower())


def license_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::") and not c.endswith("OSI Approved")]
    if classifiers:
        return ", ".join(classifiers)
    text = (meta.get("License") or "").strip()
    if text and len(text) < 100 and "\n" not in text:
        return text
    if text:
        return text.splitlines()[0][:100]
    return ""


def main() -> int:
    lines = ["MCF is built with the following software. Thank you to everyone who made it.", "",
             "This list is generated automatically every time MCF is built.",
             "The AI software that MCF installs into its runtime folder has its own list,",
             "shown after this one once the runtime is installed.", ""]
    for name, author, lic, url in EXTRA:
        lines += [f"{name}, by {author}", f"  {lic}." + (f" {url}" if url else ""), ""]
    missing = []
    for dist in bundled():
        meta = dist.metadata
        lic = license_of(dist)
        if not lic:
            missing.append(meta["Name"])
            continue
        author = (meta.get("Author") or meta.get("Maintainer") or "").strip('"')
        if not author:
            email = meta.get("Author-email") or meta.get("Maintainer-email") or ""
            author = re.sub(r"\s*<[^>]*>", "", email).strip(" ,")
        url = meta.get("Home-page") or ""
        if url.upper() == "UNKNOWN":
            url = ""
        if not url:
            urls = {}
            for entry in meta.get_all("Project-URL") or []:
                label, _sep, link = entry.partition(",")
                urls[label.strip().lower()] = link.strip()
            url = next((urls[k] for k in ("homepage", "home", "source", "repository", "documentation") if k in urls), "")
        lines.append(f"{meta['Name']} {dist.version}" + (f", by {author}" if author else ""))
        lines.append(f"  {lic}." + (f" {url}" if url else ""))
        lines.append("")
    lines += ["PyInstaller, used to build MCF.exe", "  GNU General Public License 2.0 with an exception that allows "
              "building programs under any license. https://pyinstaller.org", ""]
    if missing:
        print("ERROR: no license information for:", ", ".join(missing))
        return 1
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
