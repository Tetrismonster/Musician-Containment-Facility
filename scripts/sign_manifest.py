"""Sign mcf/data/manifest.json for publishing as an online model list update.

    python scripts/sign_manifest.py newkey PRIVATE_KEY_FILE
        Creates a key pair. Keep the private key file out of the repository.
        Put the printed public key in mcf/manifest_update.py.

    python scripts/sign_manifest.py sign PRIVATE_KEY_FILE
        Writes manifest.json.sig next to the manifest. Upload both files.
"""

import base64
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

MANIFEST = Path(__file__).resolve().parent.parent / "mcf" / "data" / "manifest.json"


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("newkey", "sign"):
        print(__doc__)
        return 1
    key_file = Path(sys.argv[2])
    if sys.argv[1] == "newkey":
        key = Ed25519PrivateKey.generate()
        key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        print("Public key:", base64.b64encode(public).decode())
        return 0
    key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    signature = key.sign(MANIFEST.read_bytes())
    MANIFEST.with_name("manifest.json.sig").write_text(base64.b64encode(signature).decode())
    print("Signed", MANIFEST)
    return 0


if __name__ == "__main__":
    sys.exit(main())
