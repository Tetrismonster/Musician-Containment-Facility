"""Package and sign the built MCF for a GitHub release.

    python scripts/make_release.py PRIVATE_KEY_FILE

Build first with scripts/build.py. This copies MCF.exe and _internal from
dist/MCF (nothing else, so no settings, models or library), writes the signed
file list, and zips it all as build/release/MCF-X.Y.Z.zip.

Upload that zip to a GitHub release tagged vX.Y.Z, for example:
    gh release create vX.Y.Z build/release/MCF-X.Y.Z.zip --notes-file NOTES.md

The key is the one made by scripts/sign_manifest.py newkey. Keep it out of the
repository.
"""

import shutil
import sys
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcf import __version__, app_update  # noqa: E402

DIST = ROOT / "dist" / "MCF"
OUT = ROOT / "build" / "release"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    key = serialization.load_pem_private_key(Path(sys.argv[1]).read_bytes(), password=None)
    for name in app_update.PROGRAM_PARTS:
        if not (DIST / name).exists():
            print(f"ERROR: {DIST / name} is missing. Run scripts/build.py first.")
            return 1
    package = OUT / f"MCF-{__version__}"
    shutil.rmtree(package, ignore_errors=True)
    package.mkdir(parents=True)
    shutil.copy2(DIST / "MCF.exe", package / "MCF.exe")
    shutil.copytree(DIST / "_internal", package / "_internal")
    app_update.sign_package(package, __version__, key)
    app_update.verify_package(package, __version__, base64_public_key(key))

    archive = OUT / f"MCF-{__version__}.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(package).as_posix())
    shutil.rmtree(package)
    print(f"Wrote {archive}. Upload it to a GitHub release tagged v{__version__}.")
    return 0


def base64_public_key(key) -> str:
    import base64
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


if __name__ == "__main__":
    sys.exit(main())
