"""Updates to MCF itself, from signed releases on GitHub.

Adapted from the updater in Sensor Readout by Andre Louis
(https://github.com/OnjLouis/accessible-sensor-readout, MIT license). The
release check, the SHA-256 digest GitHub publishes for each download, the
signed list of every file in the package, and the rollback when replacing
files fails all follow its design.

How an update runs:
1. MCF finds the newest release tagged vX.Y.Z with a file named MCF-X.Y.Z.zip.
2. It downloads the zip into its temp folder and checks GitHub's SHA-256 digest.
3. It extracts the zip and checks the signed file list: every file must be
   listed, match its hash, and the list must be signed with the MCF project's
   Ed25519 key (the same key that signs the model list).
4. It starts the new, verified MCF.exe from the temp folder with
   --apply-update, and closes. The new copy waits for the old one to exit,
   moves the old MCF.exe and _internal aside, copies itself in, and restarts.
   If anything fails, the old files are moved back.

Only MCF.exe and _internal are ever replaced. Settings, the library, models,
profiles, logs and the AI software are someone's data and are never touched.
"""

import base64
import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from mcf import __version__, downloads, manifest_update, paths
from mcf.i18n import _

log = logging.getLogger(__name__)

# Filled in when the project publishes releases, for example
# https://api.github.com/repos/OWNER/REPO/releases. Empty means updates are
# switched off in this build. The releases must be public: MCF has no
# GitHub login, and must never ship one.
RELEASES_URL = ""

PACKAGE_FORMAT = "MCFUpdatePackage"
MANIFEST_NAME = "mcf-update-manifest.json"
PROGRAM_PARTS = ("MCF.exe", "_internal")
RESULT_NAME = "update-result.json"

MAX_DOWNLOAD = 1024**3
MAX_EXTRACTED = 2 * 1024**3
MAX_ENTRIES = 20000
WAIT_FOR_EXIT_SECONDS = 120

CHECK_INTERVALS = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}

_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$", re.IGNORECASE)
_HASH = re.compile(r"^[0-9a-f]{64}$")


class UpdateError(Exception):
    pass


@dataclass
class Update:
    version: tuple[int, int, int]
    notes: str
    page_url: str
    download_url: str
    size: int
    sha256: str

    @property
    def version_text(self) -> str:
        return version_text(self.version)


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _TAG.match((text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def current_version() -> tuple[int, int, int]:
    return parse_version("v" + __version__) or (0, 0, 0)


def configured() -> bool:
    return bool(RELEASES_URL and manifest_update.PUBLIC_KEY_B64)


def available() -> bool:
    """Updates need a release address, a signing key, and a built MCF.exe to replace."""
    return configured() and bool(getattr(sys, "frozen", False))


def check_due(frequency: str, last_check: str, now: datetime | None = None) -> bool:
    """Whether an automatic check should run at this start. frequency: startup, daily, weekly or never."""
    if frequency == "never":
        return False
    interval = CHECK_INTERVALS.get(frequency)
    if interval is None:
        return True
    try:
        last = datetime.fromisoformat(last_check)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - last >= interval


# Finding the newest release


def _usable(release: dict) -> bool:
    return isinstance(release, dict) and not release.get("draft") and not release.get("prerelease") \
        and parse_version(release.get("tag_name", "")) is not None


def newest(releases: list[dict]) -> dict | None:
    candidates = [r for r in releases if _usable(r)]
    return max(candidates, key=lambda r: parse_version(r["tag_name"]), default=None)


def find_asset(release: dict) -> dict | None:
    """The one MCF-X.Y.Z.zip download, and only if GitHub lists its SHA-256 digest."""
    version = parse_version(release.get("tag_name", ""))
    if version is None:
        return None
    expected = f"MCF-{version_text(version)}.zip".lower()
    matches = [a for a in release.get("assets") or []
               if isinstance(a, dict) and str(a.get("name", "")).lower() == expected
               and str(a.get("browser_download_url", "")).startswith("https://")
               and _digest(a.get("digest")) is not None]
    return matches[0] if len(matches) == 1 else None


def _digest(value) -> str | None:
    text = str(value or "").strip().lower()
    if not text.startswith("sha256:"):
        return None
    text = text[len("sha256:"):]
    return text if _HASH.match(text) else None


def format_notes(markdown: str, tag: str) -> str:
    """Release notes as plain text for a read-only field: no heading marks, no repeated version heading."""
    lines = (markdown or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is not None and lines[first].lstrip().startswith("#"):
        heading = re.sub(r"[^0-9.]", "", lines[first]).strip(".")
        if heading and heading == tag.lstrip("vV"):
            del lines[first]
    result = []
    for line in lines:
        text = line.rstrip()
        stripped = text.lstrip()
        if not stripped:
            if result and result[-1]:
                result.append("")
            continue
        result.append(re.sub(r"^#{1,6}\s+", "", stripped) if stripped.startswith("#") else text)
    return "\n".join(result).strip()


def notes_between(releases: list[dict], current: tuple, latest: tuple) -> str:
    """Notes for every release newer than this copy, oldest first."""
    newer = sorted((r for r in releases if _usable(r) and current < parse_version(r["tag_name"]) <= latest),
                   key=lambda r: parse_version(r["tag_name"]))
    parts = []
    for release in newer:
        notes = format_notes(release.get("body") or "", release["tag_name"])
        parts.append(f"{release['tag_name']}\n{notes or _('No release notes were given for this version.')}")
    return "\n\n".join(parts)


def fetch_releases() -> list[dict]:
    request = urllib.request.Request(RELEASES_URL + "?per_page=100", headers={
        "User-Agent": downloads.USER_AGENT, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(5_000_000)
    releases = json.loads(data)
    if not isinstance(releases, list):
        raise UpdateError(_("GitHub sent a release list MCF can't read."))
    return releases


def check() -> Update | None:
    """The newest release if it's newer than this copy, otherwise None."""
    if not configured():
        raise UpdateError(_("Updates aren't set up in this version of MCF."))
    try:
        releases = fetch_releases()
    except (OSError, ValueError) as exc:
        raise UpdateError(_("Couldn't check for updates: {error}").format(error=exc)) from exc
    release = newest(releases)
    if release is None or parse_version(release["tag_name"]) <= current_version():
        return None
    version = parse_version(release["tag_name"])
    asset = find_asset(release)
    if asset is None:
        raise UpdateError(_("MCF version {version} is out, but its release has no verifiable download. "
                            "Download it from the release page instead: {url}").format(
            version=version_text(version), url=release.get("html_url", "")))
    return Update(version=version, notes=notes_between(releases, current_version(), version),
                  page_url=str(release.get("html_url", "")), download_url=asset["browser_download_url"],
                  size=int(asset.get("size") or 0), sha256=_digest(asset["digest"]))


# Signed package


def _payload(version: str, files: dict[str, str]) -> bytes:
    lines = [PACKAGE_FORMAT, version] + [f"{path}\t{files[path]}" for path in sorted(files)]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _file_hashes(folder: Path) -> dict[str, str]:
    files = {}
    manifest = folder / MANIFEST_NAME
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path != manifest:
            files[path.relative_to(folder).as_posix()] = downloads.sha256_file(path)
    return files


def sign_package(folder: Path, version: str, private_key: Ed25519PrivateKey) -> None:
    """Write the signed file list into a package folder. Used by scripts/make_release.py."""
    files = _file_hashes(folder)
    signature = private_key.sign(_payload(version, files))
    manifest = {"format": PACKAGE_FORMAT, "version": version, "files": files,
                "signature": base64.b64encode(signature).decode("ascii")}
    (folder / MANIFEST_NAME).write_text(json.dumps(manifest, indent=1), encoding="utf-8")


def _check_entry(path: str, digest: str) -> None:
    parts = path.split("/")
    if not path or path.startswith("/") or "\\" in path or ":" in path \
            or any(part in ("", ".", "..") for part in parts) or not _HASH.match(digest):
        raise UpdateError(_("The update's file list has an invalid entry."))
    if parts[0] not in PROGRAM_PARTS or (parts[0] == "MCF.exe" and len(parts) > 1) \
            or (parts[0] == "_internal" and len(parts) == 1):
        raise UpdateError(_("The update contains files outside the MCF program, so it was rejected."))


def verify_package(folder: Path, version: str, public_key_b64: str = "") -> None:
    """Raise UpdateError unless every file is listed, unchanged, and the list is signed by the MCF project."""
    manifest_path = folder / MANIFEST_NAME
    if not manifest_path.is_file() or not 0 < manifest_path.stat().st_size <= 5_000_000:
        raise UpdateError(_("The update has no signed file list, so it was rejected."))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise UpdateError(_("The update's file list can't be read, so it was rejected.")) from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict) or not files or manifest.get("format") != PACKAGE_FORMAT \
            or not isinstance(manifest.get("signature"), str):
        raise UpdateError(_("The update's file list is incomplete, so it was rejected."))
    if manifest.get("version") != version:
        raise UpdateError(_("The update says it's version {found}, not {expected}, so it was rejected.").format(
            found=manifest.get("version"), expected=version))
    for path, digest in files.items():
        if not isinstance(path, str) or not isinstance(digest, str):
            raise UpdateError(_("The update's file list has an invalid entry."))
        _check_entry(path, digest)
    if len({p.casefold() for p in files}) != len(files):
        raise UpdateError(_("The update's file list has an invalid entry."))

    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64 or manifest_update.PUBLIC_KEY_B64))
        key.verify(base64.b64decode(manifest["signature"]), _payload(version, files))
    except (InvalidSignature, ValueError) as exc:
        raise UpdateError(_("The update isn't signed by the MCF project, so it was rejected.")) from exc

    actual = _file_hashes(folder)
    if {p.casefold() for p in actual} != {p.casefold() for p in files}:
        raise UpdateError(_("The update's files don't match its signed list, so it was rejected."))
    for path, digest in files.items():
        if actual.get(path) != digest:
            raise UpdateError(_("A file in the update has been changed: {name}. It was rejected.").format(name=path))


def safe_extract(archive_path: Path, dest: Path) -> None:
    """Extract without letting any entry escape dest, overwrite another, or unpack to an absurd size."""
    dest = dest.resolve()
    seen = set()
    total = 0
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ENTRIES:
            raise UpdateError(_("The update has too many files, so it was rejected."))
        for info in entries:
            total += info.file_size
            if info.file_size > MAX_DOWNLOAD or total > MAX_EXTRACTED:
                raise UpdateError(_("The update would unpack to an unexpected size, so it was rejected."))
            name = info.filename.replace("\\", "/")
            parts = [p for p in name.split("/") if p]
            if not parts or name.startswith("/") or ":" in name or any(p in (".", "..") for p in parts):
                raise UpdateError(_("The update contains an unsafe file path, so it was rejected."))
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise UpdateError(_("The update contains a link, so it was rejected."))
            target = dest.joinpath(*parts).resolve()
            if dest not in target.parents or target.as_posix().casefold() in seen:
                raise UpdateError(_("The update contains an unsafe file path, so it was rejected."))
            seen.add(target.as_posix().casefold())
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "xb") as out:
                shutil.copyfileobj(src, out)


# Download and hand over


def work_dir() -> Path:
    """Inside MCF's own temp folder, which is emptied at every start."""
    return paths.temp_dir() / "update"


def prepare(update: Update, status, progress, cancel: threading.Event) -> Path:
    """Download, check and unpack an update. Returns the folder holding the new MCF.exe."""
    if update.size > MAX_DOWNLOAD:
        raise UpdateError(_("The update is unexpectedly large, so it wasn't downloaded."))
    root = work_dir()
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    archive = root / f"MCF-{update.version_text}.zip"
    status(_("Downloading MCF version {version}.").format(version=update.version_text))
    downloads.download(update.download_url, archive, update.sha256, update.size or None,
                       lambda done, total: progress(done / total if total else 0), cancel)
    status(_("Checking the update."))
    stage = root / "stage"
    safe_extract(archive, stage)
    archive.unlink()
    if cancel.is_set():
        raise downloads.Cancelled()
    verify_package(stage, update.version_text)
    return stage


def launch_installer(stage: Path, version: str) -> None:
    """Start the verified new MCF.exe to replace this copy once it has closed."""
    args = [str(stage / "MCF.exe"), "--apply-update", "--target", str(paths.app_dir()),
            "--wait-pid", str(os.getpid()), "--version", version]
    log.info("Starting the update to version %s", version)
    subprocess.Popen(args, cwd=str(stage), close_fds=True,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


# Replacing the program, run by the new copy


def _wait_for_exit(pid: int, seconds: int) -> bool:
    synchronize = 0x00100000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return True  # Already gone
    try:
        return kernel32.WaitForSingleObject(handle, seconds * 1000) == 0
    finally:
        kernel32.CloseHandle(handle)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_junction():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def replace_program(stage: Path, target: Path, rollback: Path) -> None:
    """Move the old program aside, copy the new one in, and put the old one back if anything fails."""
    for name in PROGRAM_PARTS:
        if not (stage / name).exists():
            raise UpdateError(_("The update is missing {name}.").format(name=name))
    rollback.mkdir(parents=True, exist_ok=True)
    moved = []
    try:
        for name in PROGRAM_PARTS:
            if (target / name).exists():
                os.replace(target / name, rollback / name)
                moved.append(name)
        for name in PROGRAM_PARTS:
            if (stage / name).is_dir():
                shutil.copytree(stage / name, target / name)
            else:
                shutil.copy2(stage / name, target / name)
    except Exception:
        log.exception("Replacing the program failed; restoring the previous version")
        for name in PROGRAM_PARTS:
            _remove(target / name)
        for name in moved:
            os.replace(rollback / name, target / name)
        raise
    shutil.rmtree(rollback, ignore_errors=True)


def _write_result(target: Path, ok: bool, version: str, error: str = "") -> None:
    try:
        folder = target / "settings"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / RESULT_NAME).write_text(json.dumps({"ok": ok, "version": version, "error": error}),
                                          encoding="utf-8")
    except OSError:
        log.exception("Could not record the update result")


def apply_from_command_line(argv: list[str]) -> int:
    """MCF.exe --apply-update --target FOLDER --wait-pid PID --version X.Y.Z, run from the staged copy."""
    def option(name: str) -> str:
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else ""

    target = Path(option("--target"))
    version = option("--version")
    stage = Path(sys.executable).resolve().parent
    if not option("--target") or not (target / "MCF.exe").exists() or target.resolve() == stage:
        log.error("Update started without a valid target folder: %s", target)
        return 1
    try:
        pid = int(option("--wait-pid") or 0)
        if pid and not _wait_for_exit(pid, WAIT_FOR_EXIT_SECONDS):
            raise UpdateError(_("MCF didn't close within two minutes, so the update wasn't installed."))
        replace_program(stage, target, stage.parent / "rollback")
        log.info("Updated %s to version %s", target, version)
        _write_result(target, True, version)
        code = 0
    except Exception as exc:
        log.exception("The update failed")
        _write_result(target, False, version, str(exc))
        code = 1
    try:
        subprocess.Popen([str(target / "MCF.exe")], cwd=str(target), close_fds=True,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    except OSError:
        log.exception("Could not restart MCF after updating")
    return code


def take_result() -> dict | None:
    """The outcome of an update that just ran, read once at start."""
    path = paths.settings_dir() / RESULT_NAME
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        result = None
    path.unlink(missing_ok=True)
    return result if isinstance(result, dict) else None
