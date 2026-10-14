"""Verified, resumable HTTPS downloads.

Every file is checked against the SHA-256 hash from the manifest before it
is used. A partial download is kept as NAME.part so it can resume, and a file
that fails the check is deleted.
"""

import hashlib
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from mcf import __version__
from mcf.i18n import _

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024
USER_AGENT = f"MCF/{__version__} (Musician Containment Facility)"

Progress = Callable[[int, int], None]


class Cancelled(Exception):
    pass


class DownloadError(Exception):
    pass


def sha256_file(path: Path, cancel: threading.Event | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, sha256: str, size: int | None = None,
             progress: Progress | None = None, cancel: threading.Event | None = None,
             retries: int = 3) -> None:
    """Download url to dest, verify it, and only then move it into place."""
    if not url.startswith("https://"):
        raise DownloadError(_("Refusing to download over an insecure connection: {url}").format(url=url))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == (size or dest.stat().st_size) \
            and sha256_file(dest, cancel) == sha256:
        if progress:
            progress(dest.stat().st_size, dest.stat().st_size)
        return
    part = dest.with_name(dest.name + ".part")
    attempt = 0
    while True:
        try:
            _fetch(url, part, size, progress, cancel)
            break
        except Cancelled:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            attempt += 1
            log.warning("Download of %s failed (attempt %d): %s", url, attempt, exc)
            if attempt > retries:
                raise DownloadError(_("Download failed: {error}").format(error=exc)) from exc
            time.sleep(2 * attempt)
    actual = sha256_file(part, cancel)
    if actual != sha256:
        part.unlink(missing_ok=True)
        raise DownloadError(_("A downloaded file failed its safety check and was deleted: {name}. "
                              "It doesn't match the expected contents.").format(name=dest.name))
    part.replace(dest)


def _fetch(url: str, part: Path, size: int | None, progress: Progress | None,
           cancel: threading.Event | None) -> None:
    done = part.stat().st_size if part.exists() else 0
    if size is not None and done > size:
        part.unlink()
        done = 0
    if size is not None and done == size:
        return
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if done:
        request.add_header("Range", f"bytes={done}-")
    with urllib.request.urlopen(request, timeout=60) as response:
        if not response.url.startswith("https://"):
            raise DownloadError(_("The download was redirected to an insecure address."))
        if done and response.status != 206:
            done = 0  # Server ignored the range; start again
        total = size or (done + int(response.headers.get("Content-Length") or 0))
        with open(part, "ab" if done else "wb") as f:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    if size is not None and done != size:
        raise OSError(f"incomplete download: {done} of {size} bytes")
