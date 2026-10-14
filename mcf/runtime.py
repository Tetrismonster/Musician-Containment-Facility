"""The AI runtime: a private Python and one environment per model engine.

Everything lives in the runtime folder inside MCF. Nothing is installed
system-wide: uv is told not to touch the registry, PATH or the user profile,
and every cache is redirected into the MCF folder.
"""

import hashlib
import json
import logging
import os
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Callable

from mcf import downloads, paths, procs
from mcf.i18n import _

log = logging.getLogger(__name__)

Status = Callable[[str], None]
Progress = Callable[[float], None]


class RuntimeError_(Exception):
    """An install step failed. The message is ready to show the user."""


# Folders


def uv_exe() -> Path:
    return paths.runtime_dir() / "uv" / "uv.exe"


def python_dir() -> Path:
    return paths.runtime_dir() / "python"


def engine_source_dir(engine_id: str) -> Path:
    return paths.runtime_dir() / "engines" / engine_id


def engine_env_dir(engine_id: str) -> Path:
    return paths.runtime_dir() / "envs" / engine_id


def engine_python(engine_id: str) -> Path:
    """The engine's Python. MCF starts it with CREATE_NO_WINDOW, so no console appears."""
    return engine_env_dir(engine_id) / "Scripts" / "python.exe"


def cache_dir() -> Path:
    return paths.runtime_dir() / "cache"


def home_dir() -> Path:
    """Stand-in user profile for child processes, so stray writes land inside MCF."""
    return paths.runtime_dir() / "home"


def _marker(engine_id: str) -> Path:
    return engine_env_dir(engine_id) / "mcf-installed.json"


# Environment


def isolated_env(temp: Path | None = None) -> dict[str, str]:
    """Environment variables that keep a child process's files inside MCF."""
    env = procs.base_env()
    home = home_dir()
    cache = cache_dir()
    tmp = temp or paths.temp_dir()
    folders = {
        "USERPROFILE": home,
        "HOME": home,
        "APPDATA": home / "AppData" / "Roaming",
        "LOCALAPPDATA": home / "AppData" / "Local",
        "TEMP": tmp,
        "TMP": tmp,
        "XDG_CACHE_HOME": cache / "xdg",
        "HF_HOME": cache / "huggingface",
        "TORCH_HOME": cache / "torch",
        "TORCHINDUCTOR_CACHE_DIR": cache / "torchinductor",
        "TORCH_EXTENSIONS_DIR": cache / "torch_extensions",
        "TRITON_CACHE_DIR": cache / "triton",
        "CUDA_CACHE_PATH": cache / "cuda",
        "NUMBA_CACHE_DIR": cache / "numba",
        "MPLCONFIGDIR": cache / "matplotlib",
        "PIP_CACHE_DIR": cache / "pip",
        "MODELSCOPE_CACHE": cache / "modelscope",
        "PYTHONPYCACHEPREFIX": cache / "pycache",
        "UV_CACHE_DIR": cache / "uv",
        "UV_PYTHON_INSTALL_DIR": python_dir(),
        "UV_TOOL_DIR": cache / "uv-tools",
        "UV_PYTHON_BIN_DIR": cache / "uv-bin",
    }
    for name, folder in folders.items():
        Path(folder).mkdir(parents=True, exist_ok=True)
        env[name] = str(folder)
    env.update({
        # Never modify the registry, PATH, or shell profiles
        "UV_PYTHON_INSTALL_REGISTRY": "0",
        "UV_PYTHON_INSTALL_BIN": "0",
        "UV_NO_MODIFY_PATH": "1",
        "UV_PYTHON_DOWNLOADS": "manual",
        "UV_PYTHON_PREFERENCE": "only-managed",
        "UV_NO_CONFIG": "1",
        # No telemetry or analytics from any library
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "GRADIO_ANALYTICS_ENABLED": "False",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONNOUSERSITE": "1",
    })
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX"):
        env.pop(name, None)
    return env


# Status


def project_dir(engine: dict) -> Path | None:
    """MCF's own project files (pyproject.toml and uv.lock) for engines that need them."""
    return paths.data_dir() / "engines" / engine["project"] if engine.get("project") else None


def engine_version(engine: dict) -> str:
    """Changes whenever the engine's pinned sources or lock file change."""
    h = hashlib.sha256()
    for source in engine["sources"]:
        h.update(source["tree_sha256"].encode())
    project = project_dir(engine)
    if project is not None:
        for name in ("pyproject.toml", "uv.lock"):
            h.update(hashlib.sha256((project / name).read_bytes()).digest())
    return h.hexdigest()


def is_installed(engine: dict) -> bool:
    try:
        data = json.loads(_marker(engine["id"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("version") == engine_version(engine) and engine_python(engine["id"]).exists()


def installed_size(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


# Installing


def tree_sha256(folder: Path) -> str:
    """Hash of every file path and its contents, independent of zip packaging."""
    h = hashlib.sha256()
    files = sorted(p for p in folder.rglob("*") if p.is_file())
    for path in files:
        rel = path.relative_to(folder).as_posix()
        h.update(rel.encode("utf-8") + b"\0")
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def _safe_extract(archive: zipfile.ZipFile, target: Path, strip_top: bool) -> None:
    """Extract without letting any entry escape the target folder."""
    target = target.resolve()
    for info in archive.infolist():
        name = info.filename
        if strip_top:
            name = name.split("/", 1)[1] if "/" in name else ""
        if not name or name.endswith("/"):
            continue
        dest = (target / name).resolve()
        if target not in dest.parents:
            raise RuntimeError_(_("The download contains an unsafe file path and was rejected."))
        dest.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)


def install_temp() -> Path:
    """Scratch folder for installs, deleted when the install ends."""
    return paths.temp_dir() / "install"


def install_uv(tool: dict, status: Status, progress: Progress, cancel: threading.Event) -> None:
    if uv_exe().exists():
        return
    status(_("Downloading the installer tool, uv."))
    archive = install_temp() / "uv.zip"
    downloads.download(tool["url"], archive, tool["sha256"], tool.get("size"),
                       lambda done, total: progress(done / total if total else 0), cancel)
    with zipfile.ZipFile(archive) as z:
        _safe_extract(z, uv_exe().parent, strip_top=False)
    archive.unlink(missing_ok=True)


def _run_uv(args: list[str], status_line: Callable[[str], None] | None, cancel: threading.Event,
            cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    code, tail = procs.run([str(uv_exe()), *args], env=env or isolated_env(install_temp()), cwd=cwd,
                           on_line=status_line, cancel=cancel)
    if cancel.is_set():
        raise downloads.Cancelled()
    if code != 0:
        log.error("uv %s failed with code %s:\n%s", " ".join(args), code, tail)
        raise RuntimeError_(_("Installing the AI software failed. The last lines of output were:\n{tail}").format(
            tail=tail))


def install_engine(manifest: dict, engine_id: str, status: Status, progress: Progress,
                   cancel: threading.Event) -> None:
    """Install uv, Python and one engine's environment. Safe to run again to repair."""
    install_temp().mkdir(parents=True, exist_ok=True)
    try:
        _install_engine(manifest, engine_id, status, progress, cancel)
    finally:
        shutil.rmtree(install_temp(), ignore_errors=True)


def _install_engine(manifest: dict, engine_id: str, status: Status, progress: Progress,
                    cancel: threading.Event) -> None:
    engine = manifest["engines"][engine_id]
    install_uv(manifest["tools"]["uv"], status, progress, cancel)

    status(_("Installing a private copy of Python {version}.").format(version=engine["python"]))
    progress(0.0)
    _install_python(engine["python"], cancel)

    root = engine_source_dir(engine_id)
    root.mkdir(parents=True, exist_ok=True)
    for index, source in enumerate(engine["sources"]):
        _install_source(engine, source, root, index, status, progress, cancel)
    project = project_dir(engine)
    if project is not None:
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copyfile(project / name, root / name)
    source = root

    status(_("Installing the AI libraries for {name}. This is the biggest step: about {size} GB. "
             "It can take a long time on a slow connection.").format(
        name=engine["name"], size=engine["download_gb"]))
    progress(0.0)
    env = isolated_env(install_temp())
    env["UV_PROJECT_ENVIRONMENT"] = str(engine_env_dir(engine_id))
    python = _find_python(engine["python"])
    counter = {"n": 0}
    expected = max(1, engine.get("package_count", 150))

    def on_line(line: str) -> None:
        # uv prints one line per package as it prepares and installs them
        text = line.strip()
        if text.startswith(("Downloaded", "Prepared", "+ ", " + ")):
            counter["n"] += 1
            progress(min(0.99, counter["n"] / (expected * 2)))
        log.info("uv: %s", text)

    # --frozen installs exactly what uv.lock lists, and uv checks every package's hash
    _run_uv(["sync", "--frozen", "--no-dev", "--python", str(python)], on_line, cancel, cwd=source, env=env)

    status(_("Clearing the installer's download cache."))
    _run_uv(["cache", "clean"], None, cancel)
    _marker(engine_id).write_text(json.dumps({"version": engine_version(engine)}), encoding="utf-8")
    progress(1.0)


def _install_source(engine: dict, source: dict, root: Path, index: int, status: Status, progress: Progress,
                    cancel: threading.Event) -> None:
    """Download one pinned source archive and check every file against its fingerprint."""
    folder = root if source["folder"] == "." else root / source["folder"]
    if folder.exists() and tree_sha256(folder) == source["tree_sha256"]:
        return
    status(_("Downloading the {name} program code.").format(name=engine["name"]))
    archive = install_temp() / f"{engine['id']}-{index}.zip"
    downloads.download(source["url"], archive, source["source_sha256"], None,
                       lambda done, total: progress(done / total if total else 0), cancel)
    staging = install_temp() / f"source-{index}"
    shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(archive) as z:
        _safe_extract(z, staging, strip_top=True)
    archive.unlink(missing_ok=True)
    if tree_sha256(staging) != source["tree_sha256"]:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError_(_("The {name} program code failed its safety check and was deleted.").format(
            name=engine["name"]))
    if source["folder"] == ".":
        shutil.rmtree(root, ignore_errors=True)
        shutil.move(str(staging), str(root))
    else:
        shutil.rmtree(folder, ignore_errors=True)
        shutil.move(str(staging), str(folder))


def _install_python(version: str, cancel: threading.Event) -> None:
    """Install Python. A failed shortcut folder is ignored when Python itself installed.

    After installing, uv adds a shortcut folder (a junction) named after the minor
    version. Drives formatted as exFAT or FAT32, common on USB drives, can't hold
    junctions, so that last step fails with "Incorrect function". MCF doesn't need it.
    """
    try:
        _run_uv(["python", "install", version, "--no-bin", "--no-registry"], None, cancel)
    except RuntimeError_ as exc:
        if "minor version link" not in str(exc) or not _patch_pythons(version):
            raise
        log.warning("uv couldn't create the Python version link on this drive; using the full install")


def _patch_pythons(version: str) -> list[Path]:
    """Installed Pythons in folders named with the full version, such as cpython-3.12.14-..."""
    found = []
    for exe in python_dir().glob(f"cpython-{version}.*/python.exe"):
        folder = exe.parent
        if not folder.is_junction() and not folder.is_symlink():
            found.append(exe)
    return sorted(found, key=lambda p: _version_key(p.parent.name), reverse=True)


def _version_key(folder_name: str) -> tuple[int, ...]:
    version = folder_name.split("-")[1]
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


def _find_python(version: str) -> Path:
    # The real folder rather than uv's shortcut to it, which some drives can't hold
    for exe in _patch_pythons(version):
        return exe
    for exe in sorted(python_dir().glob(f"cpython-{version}*/python.exe")):
        return exe
    raise RuntimeError_(_("The private copy of Python wasn't found after installing it."))


def remove_engine(engine_id: str) -> None:
    shutil.rmtree(engine_env_dir(engine_id), ignore_errors=True)
    shutil.rmtree(engine_source_dir(engine_id), ignore_errors=True)


def clean_caches() -> int:
    """Delete caches that can be rebuilt. Returns the number of bytes freed."""
    freed = 0
    for folder in (cache_dir(), home_dir()):
        if folder.exists():
            freed += installed_size(folder)
            shutil.rmtree(folder, ignore_errors=True)
    return freed
