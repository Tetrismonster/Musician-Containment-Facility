"""The model manifest, and installing, verifying and removing models."""

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Callable

from mcf import downloads, paths
from mcf.i18n import _

log = logging.getLogger(__name__)

COVER_TOOLS_ID = "cover-tools"

Status = Callable[[str], None]
Progress = Callable[[float], None]


def load_manifest() -> dict:
    """The bundled manifest, or a newer verified copy downloaded by MCF."""
    bundled = json.loads((paths.data_dir() / "manifest.json").read_text(encoding="utf-8"))
    updated_path = paths.settings_dir() / "manifest.json"
    try:
        from mcf import manifest_update
        updated = manifest_update.load_verified(updated_path)
    except Exception:
        log.exception("Ignoring the downloaded model list")
        updated = None
    manifest = updated if updated and updated.get("version", "") > bundled.get("version", "") else bundled
    for engine_id, engine in manifest["engines"].items():
        engine["id"] = engine_id
    return manifest


def model_by_id(manifest: dict, model_id: str) -> dict | None:
    return next((m for m in manifest["models"] if m["id"] == model_id), None)


def component_size(component: dict) -> int:
    return sum(f["size"] for f in component["files"])


def gb(size: int) -> str:
    return _("{gb:.1f} GB").format(gb=size / 1024**3)


class ModelStore:
    def __init__(self, models_dir: Path, manifest: dict):
        self.root = models_dir
        self.manifest = manifest

    def folder(self, item_id: str) -> Path:
        return self.root / item_id

    # Status

    def is_component_installed(self, item_id: str, component: dict) -> bool:
        """Fast check: every file present with the right size. Verify() checks contents."""
        base = self.folder(item_id)
        for f in component["files"]:
            path = base / f["path"]
            try:
                if path.stat().st_size != f["size"]:
                    return False
            except OSError:
                return False
        return bool(component["files"])

    def is_installed(self, model: dict) -> bool:
        return all(self.is_component_installed(model["id"], c) for c in model["components"] if c["required"])

    def installed_components(self, model: dict) -> list[dict]:
        return [c for c in model["components"] if self.is_component_installed(model["id"], c)]

    def installed_models(self) -> list[dict]:
        return [m for m in self.manifest["models"] if self.is_installed(m)]

    def cover_tools_installed(self) -> bool:
        tools = self.manifest["cover_tools"]
        return all(self.is_component_installed(COVER_TOOLS_ID, c) for c in tools["components"])

    def size_on_disk(self, item_id: str) -> int:
        from mcf.runtime import installed_size
        folder = self.folder(item_id)
        return installed_size(folder) if folder.exists() else 0

    # Changes

    def install(self, item_id: str, components: list[dict], status: Status, progress: Progress,
                cancel: threading.Event) -> None:
        files = [f for c in components for f in c["files"]]
        total = sum(f["size"] for f in files) or 1
        done_before = 0
        base = self.folder(item_id)
        for index, f in enumerate(files, 1):
            status(_("Downloading file {n} of {count}: {name}, {size}.").format(
                n=index, count=len(files), name=Path(f["path"]).name, size=gb(f["size"])))
            downloads.download(
                f["url"], base / f["path"], f["sha256"], f["size"],
                lambda done, _total, before=done_before: progress((before + done) / total), cancel)
            done_before += f["size"]
        progress(1.0)

    def verify(self, item_id: str, components: list[dict], progress: Progress,
               cancel: threading.Event) -> list[str]:
        """Check every file's contents. Returns the names of damaged or missing files."""
        files = [f for c in components for f in c["files"]]
        total = sum(f["size"] for f in files) or 1
        checked = 0
        bad = []
        base = self.folder(item_id)
        for f in files:
            path = base / f["path"]
            if not path.exists() or path.stat().st_size != f["size"] \
                    or downloads.sha256_file(path, cancel) != f["sha256"]:
                bad.append(f["path"])
            checked += f["size"]
            progress(checked / total)
        return bad

    def remove(self, item_id: str) -> None:
        shutil.rmtree(self.folder(item_id), ignore_errors=True)

    def remove_component(self, item_id: str, component: dict) -> None:
        base = self.folder(item_id)
        for f in component["files"]:
            (base / f["path"]).unlink(missing_ok=True)

    def move_to(self, new_root: Path, status: Status, cancel: threading.Event) -> None:
        """Move every installed model to another folder."""
        new_root.mkdir(parents=True, exist_ok=True)
        if not self.root.exists():
            self.root = new_root
            return
        for item in list(self.root.iterdir()):
            if cancel.is_set():
                raise downloads.Cancelled()
            status(_("Moving {name}.").format(name=item.name))
            target = new_root / item.name
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(item), str(target))
        try:
            self.root.rmdir()
        except OSError:
            pass
        self.root = new_root
