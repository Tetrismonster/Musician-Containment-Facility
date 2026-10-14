"""Online updates to the model list, accepted only with a valid signature.

The MCF project signs manifest.json with an Ed25519 private key that never
leaves the maintainer's computer (see scripts/sign_manifest.py). MCF holds
only the public key, so a tampered or forged list is rejected.
"""

import base64
import json
import logging
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from mcf import downloads
from mcf.i18n import _

log = logging.getLogger(__name__)

# Filled in when the project publishes a signed model list. Empty means online
# updates are switched off in this build.
MANIFEST_URL = ""
PUBLIC_KEY_B64 = "01fBSNvwsZQRvnKU7JPH/uxI/+lpQMOTt5qqac3t8FQ="


class UpdateError(Exception):
    pass


def available() -> bool:
    return bool(MANIFEST_URL and PUBLIC_KEY_B64)


def _verify(data: bytes, signature: bytes, public_key_b64: str = "") -> dict:
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64 or PUBLIC_KEY_B64))
    try:
        key.verify(signature, data)
    except InvalidSignature:
        raise UpdateError(_("The downloaded model list isn't signed by the MCF project, so it was rejected."))
    manifest = json.loads(data)
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        raise UpdateError(_("The downloaded model list is in a format this version of MCF can't read."))
    return manifest


def load_verified(path: Path) -> dict | None:
    """A previously downloaded list, re-checked every time it's loaded."""
    if not available() or not path.exists():
        return None
    sig = path.with_suffix(".sig")
    return _verify(path.read_bytes(), base64.b64decode(sig.read_text()))


def check(dest: Path) -> dict:
    """Download, verify and save the latest list. Returns it."""
    if not available():
        raise UpdateError(_("Online model list updates aren't set up in this version of MCF."))
    headers = {"User-Agent": downloads.USER_AGENT}
    with urllib.request.urlopen(urllib.request.Request(MANIFEST_URL, headers=headers), timeout=30) as r:
        data = r.read(5_000_000)
    with urllib.request.urlopen(urllib.request.Request(MANIFEST_URL + ".sig", headers=headers), timeout=30) as r:
        sig_text = r.read(1000).decode("ascii").strip()
    manifest = _verify(data, base64.b64decode(sig_text))
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    dest.with_suffix(".sig").write_text(sig_text)
    return manifest
