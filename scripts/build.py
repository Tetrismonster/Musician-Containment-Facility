"""Build the portable MCF folder: dist/MCF/MCF.exe

Run from the project root:  .venv\\Scripts\\python scripts\\build.py

PyInstaller empties its output folder before building, so it builds into a
staging folder. Only MCF.exe and _internal are then replaced in dist/MCF.
Everything else there (settings, runtime, models, library, profiles and logs)
is someone's data and is never touched.
"""

import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "MCF"
STAGING = ROOT / "build" / "staging"
IMAGE_SUBSYSTEM_WINDOWS_GUI = 2
PROGRAM_PARTS = ("MCF.exe", "_internal")


def pe_subsystem(exe: Path) -> int:
    data = exe.read_bytes()
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    # Subsystem sits 68 bytes into the optional header, after the 24-byte PE header
    return struct.unpack_from("<H", data, pe_offset + 24 + 68)[0]


def main() -> int:
    # The credits list is regenerated on every build, and the build stops if a
    # bundled package has no license information
    subprocess.run([sys.executable, str(ROOT / "scripts" / "credits.py")], check=True, cwd=ROOT)
    sep = ";"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",           # No console window
        "--onedir",             # A portable folder, with no unpacking to temp on every start
        "--name", "MCF",
        "--distpath", str(STAGING),
        "--workpath", str(ROOT / "build" / "work"),
        "--specpath", str(ROOT / "build"),
        "--collect-all", "accessible_output2",
        "--hidden-import", "win32com.client",
        "--add-data", f"{ROOT / 'licenses'}{sep}licenses",
        "--add-data", f"{ROOT / 'mcf' / 'data'}{sep}data",
        # The worker runs in the AI runtime's own Python, so it ships as plain source
        "--add-data", f"{ROOT / 'worker' / 'mcf_worker'}{sep}worker/mcf_worker",
        "--hidden-import", "wx.media",
        "--collect-all", "soundfile",
        "--exclude-module", "tkinter",
        str(ROOT / "mcf" / "__main__.py"),
    ]
    subprocess.run(cmd, check=True, cwd=ROOT)
    built = STAGING / "MCF"
    # Python caches from running the tests don't belong in the portable folder
    for cache in built.rglob("__pycache__"):
        if "worker" in cache.parts:
            shutil.rmtree(cache)
    exe = built / "MCF.exe"
    subsystem = pe_subsystem(exe)
    if subsystem != IMAGE_SUBSYSTEM_WINDOWS_GUI:
        print(f"ERROR: {exe} has subsystem {subsystem}; it would open a console window.")
        return 1

    # Replace only the program itself
    DIST.mkdir(parents=True, exist_ok=True)
    for name in PROGRAM_PARTS:
        target = DIST / name
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        except OSError as exc:
            print(f"ERROR: {target} is in use ({exc}). Close MCF and build again.")
            return 1
        shutil.move(str(built / name), str(target))
    shutil.rmtree(STAGING, ignore_errors=True)
    print(f"Built {DIST / 'MCF.exe'} (windowed, no console). Other files in {DIST} were left alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
